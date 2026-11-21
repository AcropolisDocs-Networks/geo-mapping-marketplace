"""dem_sources: the national and polar elevation sources beyond 3DEP, Copernicus and Terrarium (3.1.0, T-08), each fetched keylessly
from its publisher's own service and returned as a GeoTIFF with the same fetch record the other sources write.

    canada-mrdem   NRCan CanElevation MRDEM-30 (DTM), one Cloud-Optimized GeoTIFF for Canada on AWS; overview range reads. OGL-Canada.
    gsi-japan      GSI elevation tiles (dem_png, zoom 14, about 10 m), PNG tiles decoded as GSI documents. GSI content terms (attribution).
    ga-lidar-5m    Geoscience Australia 5 m LiDAR DEM, one WCS 1.0.0 GetCoverage (GeoTIFF, GDA94). CC BY 4.0. Covers the populated coast and
                   the Murray-Darling floodplains, not all of Australia: where it has no data the next source fills the window.
    ign-rge-alti   IGN RGE ALTI (France), the Geoplateforme WMS-R as 32-bit floats (image/x-bil), Lambert-93. Licence Ouverte 2.0.
    arcticdem      PGC ArcticDEM mosaic v4.1, 32 m tiles (100 km, EPSG:3413) on AWS. CC BY 4.0.
    rema           PGC REMA mosaic v2.0, 32 m tiles (100 km, EPSG:3031) on AWS. CC BY 4.0.
    gebco          GEBCO grid (15 arc seconds, the sea floor and the land), one COG on Source Cooperative. Public domain, credit asked.
    flanders-dhmv  Digitaal Vlaanderen's DHMV II DTM (1 m, bare earth), one WCS 1.0.0 GetCoverage in Belgian Lambert 72. Free reuse with credit.
    flanders-lidar The DHMV II point cloud itself (openlidar LAZ tiles, gridded by lidar.py): named only, for a site where the 1 m grid is not
                   enough or a surface model (terrain.surface: dsm) is wanted.
    lantmateriet   Lantmäteriet's Markhöjdmodell (1 m DTM, Sweden), STAC items of COGs read by range; CC BY 4.0, but the files need a free
                   Geotorget login (the person's own credential `lantmateriet`, HTTP Basic, sent only to dl1.lantmateriet.se). Without it
                   the chain goes on to Copernicus, with an INFO saying how to add the login.

Each fetch writes `<cache>/terrain/<source>/<key>.tif` and returns a record (entry, crs, res_m, bytes, sha256, requests, attribution, licence).
Every request goes through ogc_http.SafeHTTP or GDAL's range reads of a fixed public address built here; nothing takes a URL from a spec.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import urllib.parse
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import raster_io as rio  # noqa: E402

NODATA = -9999.0
MRDEM_URL = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.tif"
GSI_URL = "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png"
GSI_MAX_ZOOM = 14
GSI_MAX_TILES = 64
GA_WCS = "https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer"
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
IGN_TILE_PX = 2048
PGC = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com/"
POLAR = {"arcticdem": {"crs": "EPSG:3413", "origin": -4_000_000.0, "path": "arcticdem/mosaics/v4.1/32m/{t}/{t}_32m_v4.1_dem.tif"},
         "rema": {"crs": "EPSG:3031", "origin": -3_000_000.0, "path": "rema/mosaics/v2.0/32m/{t}/{t}_32m_v2.0_dem.tif"}}
POLAR_TILE_M = 100_000.0
POLAR_MAX_TILES = 64
GEBCO_URL = "https://data.source.coop/ausantarctic/gebco/GEBCO_2026.tif"
FLANDERS_WCS = "https://geo.api.vlaanderen.be/el-dtm/wcs"
FLANDERS_COVERAGE = "EL.GridCoverage.DTM"
LM_STAC = "https://api.lantmateriet.se/stac-hojd/v1/collections/dtm-cog/items"
LM_HOST = "dl1.lantmateriet.se"
LM_MAX_ITEMS = 16
MAX_PX = 4096

INFO = {
    "canada-mrdem": {"entry": "ref.terrain.ca.dem.mrdem", "native_m": 30, "surface": "bare earth (DTM)", "license": "attribution",
                     "attribution": "Elevation: Medium Resolution Digital Elevation Model (MRDEM), Natural Resources Canada; contains information licensed under the "
                                    "Open Government Licence - Canada."},
    "gsi-japan": {"entry": "ref.terrain.jp.dem.gsi", "native_m": 10, "surface": "bare earth (DTM)", "license": "attribution",
                  "attribution": "Elevation: Geospatial Information Authority of Japan (GSI) elevation tiles (国土地理院)."},
    "ga-lidar-5m": {"entry": "ref.terrain.au.dem.ga-lidar-5m", "native_m": 5, "surface": "bare earth (DTM)", "license": "attribution",
                    "attribution": "Elevation: Digital Elevation Model of Australia derived from LiDAR 5 Metre Grid, (c) Commonwealth of Australia (Geoscience Australia), CC BY 4.0."},
    "ign-rge-alti": {"entry": "ref.terrain.fr.dem.rge-alti", "native_m": 1, "surface": "bare earth (DTM)", "license": "attribution",
                     "attribution": "Elevation: IGN RGE ALTI, Institut national de l'information géographique et forestière, Licence Ouverte 2.0."},
    "arcticdem": {"entry": "ref.terrain.arctic.dem.arcticdem", "native_m": 32, "surface": "surface model (DSM)", "license": "attribution",
                  "attribution": "Elevation: ArcticDEM v4.1 mosaic, Polar Geospatial Center (University of Minnesota), CC BY 4.0; DEMs created from Maxar imagery."},
    "rema": {"entry": "ref.terrain.antarctica.dem.rema", "native_m": 32, "surface": "surface model (DSM)", "license": "attribution",
             "attribution": "Elevation: Reference Elevation Model of Antarctica (REMA) v2.0 mosaic, Polar Geospatial Center (University of Minnesota), CC BY 4.0."},
    "gebco": {"entry": "ref.terrain.world.bathymetry.gebco", "native_m": 450, "surface": "sea floor and land (bathymetry)", "license": "public-domain",
              "attribution": "Bathymetry: GEBCO Compilation Group (2026) GEBCO 2026 Grid."},
    "flanders-dhmv": {"entry": "ref.terrain.be-vlg.dem.dhmv", "native_m": 1, "surface": "bare earth (DTM)", "license": "attribution",
                      "attribution": "Elevation: Digitaal Hoogtemodel Vlaanderen II, DTM, 1 m, Digitaal Vlaanderen, Modellicentie Gratis Hergebruik."},
    "flanders-lidar": {"entry": "ref.terrain.be-vlg.lidar.dhmv-openlidar", "native_m": 1, "surface": "bare earth (DTM)", "license": "attribution",
                       "attribution": "Elevation: LiDAR Digitaal Hoogtemodel Vlaanderen II (DHMV II) point cloud, Digitaal Vlaanderen, "
                                      "Modellicentie Gratis Hergebruik (free reuse with credit)."},
    "lantmateriet": {"entry": "ref.terrain.se.dem.lantmateriet-mhm", "native_m": 1, "surface": "bare earth (DTM)", "license": "attribution",
                     "attribution": "Elevation: Markhöjdmodell, © Lantmäteriet, CC BY 4.0."},
}
FETCHERS = tuple(INFO)


class DemSourceError(Exception):
    pass


def _deg(res_m: float, lat: float) -> tuple[float, float]:
    dy = res_m / 111_320.0
    return res_m / (111_320.0 * max(0.05, math.cos(math.radians(lat)))), dy


def grid_4326(bounds, res_m: float, max_px: int = MAX_PX) -> tuple:
    """The output grid in lon/lat for a window and a cell size, capped at max_px a side (a coarser cell when the cap binds)."""
    from rasterio.transform import from_origin
    W, S, E, N = bounds
    dx, dy = _deg(res_m, (S + N) / 2)
    w, h = max(1, int(round((E - W) / dx))), max(1, int(round((N - S) / dy)))
    k = max(1.0, w / max_px, h / max_px)
    if k > 1:
        w, h, dx, dy = int(w / k), int(h / k), dx * k, dy * k
    return from_origin(W, N, dx, dy), (h, w), res_m * k


def _finish(src: str, arr, transform, crs, out: Path, res_m: float, requests: list, extra: dict | None = None) -> dict:
    a = np.where(np.isfinite(arr), arr, NODATA).astype("float32")
    w = rio.write_geotiff(out, np.ma.masked_equal(a, NODATA), transform, crs, nodata=NODATA)
    info = INFO[src]
    rec = {"source": src, "entry": info["entry"], "crs": rio.crs_string(crs), "res_m": float(res_m), "bytes": w["bytes"], "sha256": w["sha256"],
           "path": str(out), "requests": requests, "pixels": [int(a.shape[1]), int(a.shape[0])], "nodata": NODATA, "surface": info["surface"],
           "attribution": info["attribution"], "license": info["license"], "missing_share": float((a == NODATA).mean())}
    rec.update(extra or {})
    return rec


# ---------------------------------------------------------------------------------------------------------------- COG sources
def cog_open(url: str, overview_level: int | None = None):
    import rasterio
    kw = {"overview_level": overview_level} if overview_level is not None else {}
    return rasterio.open(("/vsicurl/" + url) if url.startswith("https://") else url, **kw)


def _overview_for(ds, want_m: float) -> int | None:
    """The coarsest overview whose cells are still no larger than the target (None: the full resolution)."""
    native = abs(ds.res[0])
    if str(ds.crs).upper() in ("EPSG:4326", "OGC:CRS84") or (ds.crs and ds.crs.is_geographic):
        native *= 111_320.0
    best = None
    for i, f in enumerate(ds.overviews(1) or []):
        if native * f <= want_m:
            best = i
    return best


def _warp_into(url: str, dst, transform, crs, res_m: float, opener=None) -> int:
    """Read one COG into the destination grid (bilinear, overviews by range reads). -> 1 when it was read, 0 when it is not there."""
    import rasterio
    from rasterio.warp import reproject, Resampling
    op = opener or cog_open
    try:
        with rasterio.Env(**_gdal_env()):
            with op(url) as ds0:
                lvl = _overview_for(ds0, res_m)
            with op(url, lvl) if lvl is not None else op(url) as ds:
                tmp = np.full(dst.shape, np.nan)
                reproject(rasterio.band(ds, 1), tmp, src_transform=ds.transform, src_crs=ds.crs, dst_transform=transform, dst_crs=crs,
                          src_nodata=ds.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear)
    except rasterio.errors.RasterioIOError:
        return 0
    ok = np.isfinite(tmp) & np.isnan(dst)
    dst[ok] = tmp[ok]
    return 1


def _gdal_env() -> dict:
    import terrain_fetch as tf
    return tf._gdal_http_env()


def fetch_cog(src: str, url: str, bounds, res_m: float, out: Path, *, opener=None) -> dict:
    tr, shape, got_m = grid_4326(bounds, max(res_m, float(INFO[src]["native_m"])))
    arr = np.full(shape, np.nan)
    n = _warp_into(url, arr, tr, "EPSG:4326", got_m, opener)
    if not n:
        raise DemSourceError(f"{src}: {url} could not be read")
    return _finish(src, arr, tr, "EPSG:4326", out, got_m, [{"url": url, "bytes": 0, "range_reads": True}])


def polar_tiles(src: str, bounds) -> list[str]:
    """The 100 km mosaic tiles (row_col, 1-based from the grid origin) a lon/lat window touches, in the source's polar stereographic grid."""
    from pyproj import Transformer
    p = POLAR[src]
    t = Transformer.from_crs("EPSG:4326", p["crs"], always_xy=True)
    W, S, E, N = bounds
    xs, ys = [], []
    for i in range(9):
        for j in range(9):
            x, y = t.transform(W + (E - W) * i / 8, S + (N - S) * j / 8)
            xs.append(x)
            ys.append(y)
    o = p["origin"]
    c0, c1 = int((min(xs) - o) // POLAR_TILE_M) + 1, int((max(xs) - o) // POLAR_TILE_M) + 1
    r0, r1 = int((min(ys) - o) // POLAR_TILE_M) + 1, int((max(ys) - o) // POLAR_TILE_M) + 1
    return [f"{r:02d}_{c:02d}" for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def fetch_polar(src: str, bounds, res_m: float, out: Path, *, opener=None) -> dict:
    tiles = polar_tiles(src, bounds)
    if len(tiles) > POLAR_MAX_TILES:
        raise DemSourceError(f"{src}: the window needs {len(tiles)} mosaic tiles; the limit is {POLAR_MAX_TILES}")
    tr, shape, got_m = grid_4326(bounds, max(res_m, 32.0))
    arr = np.full(shape, np.nan)
    reqs, found = [], 0
    for t in tiles:
        url = PGC + POLAR[src]["path"].format(t=t)
        n = _warp_into(url, arr, tr, "EPSG:4326", got_m, opener)
        found += n
        reqs.append({"url": url, "bytes": 0, "range_reads": True, "found": bool(n)})
    if not found:
        raise DemSourceError(f"{src}: none of the {len(tiles)} mosaic tile(s) for this window exist (open sea, or outside the coverage)")
    return _finish(src, arr, tr, "EPSG:4326", out, got_m, reqs, {"tiles": tiles})


# ---------------------------------------------------------------------------------------------------------------- tiles and services
def decode_gsi(png: bytes) -> np.ndarray:
    """GSI dem_png: x = R * 2^16 + G * 2^8 + B; x < 2^23 -> x * 0.01 m; x > 2^23 -> (x - 2^24) * 0.01 m; x = 2^23 is no data."""
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.int64)
    x = a[..., 0] * 65536 + a[..., 1] * 256 + a[..., 2]
    h = np.where(x < 2 ** 23, x * 0.01, (x - 2 ** 24) * 0.01).astype("float64")
    h[x == 2 ** 23] = np.nan
    return h


def fetch_gsi(bounds, res_m: float, out: Path, *, http) -> dict:
    import terrain_fetch as tf
    from rasterio.transform import from_origin
    g = tf.choose_zoom(bounds, max(res_m, 10.0), max_tiles=GSI_MAX_TILES, max_zoom=GSI_MAX_ZOOM)
    z, cols, rows = g["z"], g["cols"], g["rows"]
    arr = np.full((len(rows) * 256, len(cols) * 256), np.nan)
    reqs = []
    for i, ty in enumerate(rows):
        for j, tx in enumerate(cols):
            url = GSI_URL.format(z=z, x=tx, y=ty)
            try:
                r = http.get(url, expect="any", max_bytes=2_000_000)
            except Exception as ex:  # noqa: BLE001 - a 404 is open sea or outside Japan
                reqs.append({"url": url, "status": getattr(ex, "status", None), "bytes": 0})
                continue
            reqs.append({"url": url, "status": 200, "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()})
            arr[i * 256:(i + 1) * 256, j * 256:(j + 1) * 256] = decode_gsi(r.content)
    if not any(q.get("status") == 200 for q in reqs):
        raise DemSourceError("gsi-japan: no elevation tile was found for this window (outside Japan, or open sea)")
    x0, _y0, _x1, y1 = g["bbox_3857"]
    px = g["tile_m"] / 256
    tr = from_origin(x0, y1, px, px)
    return _finish("gsi-japan", arr, tr, "EPSG:3857", out, g["res_m"], reqs, {"zoom": z, "limited_by_cap": g["limited_by_cap"]})


def fetch_ga(bounds, res_m: float, out: Path, *, http) -> dict:
    tr, (h, w), got_m = grid_4326(bounds, max(res_m, 5.0))
    W, S, E, N = bounds
    params = {"service": "WCS", "version": "1.0.0", "request": "GetCoverage", "coverage": "1", "CRS": "EPSG:4283",
              "BBOX": f"{W},{S},{E},{N}", "WIDTH": str(w), "HEIGHT": str(h), "FORMAT": "GeoTIFF"}
    r = http.get(GA_WCS, params=params, expect="any", max_bytes=400_000_000)
    if not r.content[:4] in (b"II*\x00", b"MM\x00*"):
        raise DemSourceError("ga-lidar-5m: the WCS did not return a GeoTIFF")
    import rasterio
    with rasterio.MemoryFile(r.content) as mf, mf.open() as ds:
        a = ds.read(1).astype("float64")
        if ds.nodata is not None:
            a[a == ds.nodata] = np.nan
        a[a < -1000] = np.nan
        t2, crs = ds.transform, ds.crs or "EPSG:4283"
    return _finish("ga-lidar-5m", a, t2, crs, out, got_m, [{"url": GA_WCS, "params": params, "status": 200, "bytes": len(r.content),
                                                            "sha256": hashlib.sha256(r.content).hexdigest()}])


def fetch_ign(bounds, res_m: float, out: Path, *, http) -> dict:
    """RGE ALTI in Lambert-93 from the WMS-R as raw float32 (image/x-bil;bits=32), in tiles of at most 2048 px, at most 4096 px a side."""
    from pyproj import Transformer
    from rasterio.transform import from_origin
    t = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    W, S, E, N = bounds
    xs, ys = zip(*[t.transform(W + (E - W) * i / 4, S + (N - S) * j / 4) for i in range(5) for j in range(5)])
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    cell = max(float(res_m), 1.0)
    cols, rows = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    k = max(1.0, cols / MAX_PX, rows / MAX_PX)
    if k > 1:
        cell *= k
        cols, rows = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    arr = np.full((rows, cols), np.nan)
    reqs = []
    for r0 in range(0, rows, IGN_TILE_PX):
        for c0 in range(0, cols, IGN_TILE_PX):
            hh, ww = min(IGN_TILE_PX, rows - r0), min(IGN_TILE_PX, cols - c0)
            bx0, by1 = x0 + c0 * cell, y1 - r0 * cell
            bbox = f"{bx0},{by1 - hh * cell},{bx0 + ww * cell},{by1}"
            params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": IGN_LAYER, "STYLES": "", "CRS": "EPSG:2154", "BBOX": bbox,
                      "WIDTH": str(ww), "HEIGHT": str(hh), "FORMAT": "image/x-bil;bits=32"}
            r = http.get(IGN_WMS, params=params, expect="any", max_bytes=ww * hh * 4 + 1024)
            if len(r.content) != ww * hh * 4:
                raise DemSourceError(f"ign-rge-alti: the WMS-R answered {len(r.content)} bytes, not {ww * hh * 4} (a {ww} x {hh} float grid)")
            a = np.frombuffer(r.content, dtype="<f4").reshape(hh, ww).astype("float64")
            a[a <= -99_000] = np.nan
            arr[r0:r0 + hh, c0:c0 + ww] = a
            reqs.append({"url": IGN_WMS, "params": params, "status": 200, "bytes": len(r.content)})
    return _finish("ign-rge-alti", arr, from_origin(x0, y1, cell, cell), "EPSG:2154", out, cell, reqs)


def _wcs10_projected(src: str, url: str, coverage: str, crs: str, bounds, res_m: float, out: Path, *, http) -> dict:
    """One WCS 1.0.0 GetCoverage as GeoTIFF on a square grid in a projected CRS (the window's corners carried into it), at most MAX_PX a side."""
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    W, S, E, N = bounds
    xs, ys = zip(*[t.transform(W + (E - W) * i / 4, S + (N - S) * j / 4) for i in range(5) for j in range(5)])
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    cell = max(float(res_m), float(INFO[src]["native_m"]))
    cols, rows = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    k = max(1.0, cols / MAX_PX, rows / MAX_PX)
    if k > 1:
        cell *= k
        cols, rows = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    params = {"service": "WCS", "version": "1.0.0", "request": "GetCoverage", "coverage": coverage, "crs": crs,
              "bbox": f"{x0},{y1 - rows * cell},{x0 + cols * cell},{y1}", "width": str(cols), "height": str(rows), "format": "GeoTIFF"}
    r = http.get(url, params=params, expect="any", max_bytes=cols * rows * 4 + 4_000_000)
    if r.content[:4] not in (b"II*\x00", b"MM\x00*"):
        raise DemSourceError(f"{src}: the WCS did not return a GeoTIFF")
    import rasterio
    with rasterio.MemoryFile(r.content) as mf, mf.open() as ds:
        a = ds.read(1).astype("float64")
        if ds.nodata is not None:
            a[a == ds.nodata] = np.nan
        a[a < -1000] = np.nan
        t2, crs2 = ds.transform, ds.crs or crs
    return _finish(src, a, t2, crs2, out, cell, [{"url": url, "params": params, "status": 200, "bytes": len(r.content),
                                                 "sha256": hashlib.sha256(r.content).hexdigest()}])


def fetch_flanders_lidar(bounds, res_m: float, out: Path, *, http) -> dict:
    import lidar
    try:
        rec = lidar.flanders(bounds, out, cell=max(1.0, float(res_m)), http=http)
    except lidar.LidarError as ex:
        raise DemSourceError(f"flanders-lidar: {ex}") from None
    rec.update({"entry": INFO["flanders-lidar"]["entry"], "license": "attribution"})
    return rec


def lantmateriet_items(bounds, http) -> list[dict]:
    """The Markhöjdmodell STAC items that touch a lon/lat window (keyless search). -> [{id, href}] of the COG data assets."""
    W, S, E, N = bounds
    r = http.get(LM_STAC, params={"bbox": f"{W},{S},{E},{N}", "limit": str(LM_MAX_ITEMS + 1)}, expect="json", max_bytes=10_000_000)
    feats = json.loads(r.content).get("features") or []
    out = []
    for f in feats:
        href = str(((f.get("assets") or {}).get("data") or {}).get("href") or "")
        u = urllib.parse.urlsplit(href)
        if u.scheme == "https" and (u.hostname or "").lower() == LM_HOST and href.lower().endswith(".tif"):
            out.append({"id": f.get("id"), "href": href})
    return out


def _basic_env(auth) -> dict:
    """GDAL's own HTTP Basic for /vsicurl/ reads, from the person's registered credential (never logged; the env lives for one read)."""
    url, headers = auth.apply("https://" + LM_HOST + "/", {})
    h = headers.get("Authorization") or ""
    return {"GDAL_HTTP_HEADERS": f"Authorization: {h}"} if h else {}


def fetch_lantmateriet(bounds, res_m: float, out: Path, *, http, store=None) -> dict:
    import credentials as cr
    try:
        auth = cr.resolve("cred:lantmateriet", store=store, host=LM_HOST)
    except cr.CredentialError as ex:
        raise DemSourceError("lantmateriet: the Markhöjdmodell files need your free Geotorget login, registered as map-credentials add lantmateriet "
                             f"--kind basic --host {LM_HOST} ({ex.findings[0]['message']})") from None
    items = lantmateriet_items(bounds, http)
    if not items:
        auth.close()
        raise DemSourceError("lantmateriet: no Markhöjdmodell tile in this window (Sweden only)")
    if len(items) > LM_MAX_ITEMS:
        auth.close()
        raise DemSourceError(f"lantmateriet: the window needs more than {LM_MAX_ITEMS} tiles; Copernicus covers a window this large")
    tr, shape, got_m = grid_4326(bounds, max(res_m, 1.0))
    arr = np.full(shape, np.nan)
    import rasterio
    n = 0
    try:
        env = {**_gdal_env(), **_basic_env(auth)}
        with rasterio.Env(**env):
            for it in items:
                n += _warp_into(it["href"], arr, tr, "EPSG:4326", got_m)
    finally:
        auth.close()
    if not n:
        raise DemSourceError("lantmateriet: the tiles could not be read (the login may lack the Markhöjdmodell order in Geotorget)")
    return _finish("lantmateriet", arr, tr, "EPSG:4326", out, got_m, [{"url": it["href"], "bytes": 0, "range_reads": True} for it in items],
                   {"credential": "lantmateriet", "host": LM_HOST})


def fetch(src: str, bounds, res_m: float, out: Path, *, http=None, opener=None) -> dict:
    """One of FETCHERS for the window, written to `out`. -> the fetch record."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if src == "canada-mrdem":
        return fetch_cog(src, MRDEM_URL, bounds, res_m, out, opener=opener)
    if src == "gebco":
        return fetch_cog(src, GEBCO_URL, bounds, res_m, out, opener=opener)
    if src in POLAR:
        return fetch_polar(src, bounds, res_m, out, opener=opener)
    if http is None:
        import terrain_fetch as tf
        http = tf.safe_http()
    if src == "gsi-japan":
        return fetch_gsi(bounds, res_m, out, http=http)
    if src == "ga-lidar-5m":
        return fetch_ga(bounds, res_m, out, http=http)
    if src == "ign-rge-alti":
        return fetch_ign(bounds, res_m, out, http=http)
    if src == "flanders-dhmv":
        return _wcs10_projected(src, FLANDERS_WCS, FLANDERS_COVERAGE, "EPSG:31370", bounds, res_m, out, http=http)
    if src == "flanders-lidar":
        return fetch_flanders_lidar(bounds, res_m, out, http=http)
    if src == "lantmateriet":
        return fetch_lantmateriet(bounds, res_m, out, http=http)
    raise DemSourceError(f"no elevation source '{src}'")
