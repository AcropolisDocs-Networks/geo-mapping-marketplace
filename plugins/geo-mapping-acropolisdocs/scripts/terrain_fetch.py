#!/usr/bin/env python3
"""terrain_fetch: a DEM for a map window, from USGS 3DEP or the AWS Terrain Tiles, cached on disk with a fetch record (M12).

    import terrain_fetch as tf
    r = tf.dem_for((-112.2, 33.3, -111.9, 33.6), target_m_per_px=30, source="auto", cache_dir=cache)
    r["path"]          # a local GeoTIFF (float32 metres, nodata -9999)
    r["record"]        # source, requests, bytes, attribution, terms_acknowledged, retrieved_at, sha256, cached

Sources
  3dep        USGS 3D Elevation Program ImageServer (public domain, US only). One `exportImage` request, `format=tiff`, F32 metres,
              at most 4096 x 4096 pixels (a wider window is read at a coarser cell and the record says so). No terms to acknowledge;
              the credit line "USGS 3DEP" goes on the map.
  terrarium   AWS Terrain Tiles (Mapzen/Nextzen Terrarium PNG, hosted by AWS Open Data). Elevation = (R * 256 + G + B / 256) - 32768.
              Attribution is required (the sources named in tilezen's attribution document); the text is returned in the record and
              printed as the map's terrain credit, so there is nothing to acknowledge (0.23.0). The zoom is the one whose cells are no coarser than `target_m_per_px`
              at the window's latitude, lowered until at most `max_tiles` (64) tiles are needed. The mosaic is in EPSG:3857.
  auto        3dep inside the United States (the lower 48, Alaska, Hawaii, Puerto Rico), terrarium elsewhere, with a note.

Rules: every request goes through ogc_http.SafeHTTP (public hosts, GET, caps, the plugin's User-Agent); at most 4 tile requests at a
time; 429 and 5xx back off; raw tiles are cached under `<cache>/terrain/terrarium-tiles/z/x/y.png` and the finished DEM under
`<cache>/terrain/<source>/<key>.tif` beside its record, keyed by (source, bounds, resolution), so a second call for the same window reads
nothing from the network. A DEM the person already has (`dem_path`) is recorded and nothing is fetched. Every request made is listed
in the record (`requests`: redacted URL, status, bytes, time, sha256).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import raster_io as rio  # noqa: E402

THREEDEP_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
THREEDEP_ENTRY = "ref.terrain.us.dem.3dep"
TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRARIUM_ENTRY = "ref.terrain.world.dem.terrarium"
TERRARIUM_MAX_ZOOM = 15
COP_ENTRY = "ref.terrain.world.dem.copernicus-glo30"                 # 1.5.0 (R3-DEM): the default outside the United States for relief
COP_BUCKETS = {30: "https://copernicus-dem-30m.s3.amazonaws.com/", 90: "https://copernicus-dem-90m.s3.amazonaws.com/"}
COP_ARCSEC = {30: 10, 90: 30}                                          # the file name carries the cell size in arc seconds, not metres
COP_MAX_TILES = 16                     # whole tiles; 2.5.0: a larger window reads COG overviews within a byte budget (T-04)
COP_TILE_BYTES = 60_000_000
COP_TILE_MB = {30: 25.0, 90: 4.0}      # the typical size of one whole tile, for the byte estimate
COP_BYTE_BUDGET_MB = 40.0              # whole tiles past this estimate (or past COP_MAX_TILES) are read as overviews by range reads
COP_COG_MAX_TILES = 400                # at most this many 1-degree tiles a map even by range reads
COP_COG_MIN_M = 100.0                  # a coarser target than this reads overviews once whole tiles would pass the budget
COP_REGISTRY = "https://registry.opendata.aws/copernicus-dem"
COP_NOTICE = {30: ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by "
                   "the European Union and ESA; all rights reserved"),
              90: ("produced using Copernicus WorldDEM-90 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by "
                   "the European Union and ESA; all rights reserved")}
TERRARIUM_MAX_TILES = 64
TERRARIUM_CONCURRENCY = 4
THREEDEP_MAX_PX = 4096
NODATA = -9999.0
R = 6378137.0
ORIGIN = math.pi * R
THREEDEP_ATTRIBUTION = "Elevation: USGS 3D Elevation Program (3DEP), public domain. 3DEP data courtesy of the U.S. Geological Survey."
TERRARIUM_ATTRIBUTION = ("Elevation: Terrain Tiles on AWS (Mapzen Terrarium). ArcticDEM terrain data DEM(s) were created from DigitalGlobe, Inc., imagery and funded "
                         "under National Science Foundation awards 1043681, 1559691, and 1542736; Australia terrain data (c) Commonwealth of Australia (Geoscience "
                         "Australia) 2017; Austria terrain data (c) offene Daten Osterreichs - Digitales Gelandemodell (DGM) Osterreich; Canada terrain data contains "
                         "information licensed under the Open Government Licence - Canada; Europe terrain data produced using Copernicus data and information funded by "
                         "the European Union - EU-DEM layers; Global ETOPO1 terrain data U.S. National Oceanic and Atmospheric Administration; Mexico terrain data "
                         "source: INEGI, Continental relief, 2016; New Zealand terrain data Copyright 2011 Crown copyright (c) Land Information New Zealand and the "
                         "New Zealand Government (All rights reserved); Norway terrain data (c) Kartverket; United Kingdom terrain data (c) Environment Agency copyright "
                         "and/or database right 2015. All rights reserved; United States 3DEP (formerly NED) and global GMTED2010 and SRTM terrain data courtesy of "
                         "the U.S. Geological Survey.")
TERRARIUM_TERMS_URL = "https://github.com/tilezen/joerd/blob/master/docs/attribution.md"
# Where 3DEP has data: the lower 48, Alaska, Hawaii, Puerto Rico and the US Virgin Islands (lon0, lat0, lon1, lat1).
US_BOXES = [(-125.5, 24.3, -66.5, 49.5), (-180.0, 51.0, -129.0, 71.6), (172.0, 51.0, 180.0, 53.5), (-161.0, 18.7, -154.5, 22.5), (-68.0, 17.6, -64.4, 18.6)]


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def safe_http(**limits) -> oh.SafeHTTP:
    """The plugin's guarded client with the plugin's User-Agent. Behind a proxy SafeHTTP does not pin the address (its own rule, M12)."""
    return oh.SafeHTTP(limits=limits or None)


def today() -> str:
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


# --------------------------------------------------------------------------------------------------------------- geometry helpers

def lonlat_to_merc(lon: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    return lon * ORIGIN / 180.0, math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * R


def tile_res_m(z: int, lat: float) -> float:
    """Ground metres per pixel of a 256 px Terrarium tile at zoom z and latitude lat."""
    return 2 * ORIGIN / (256 * (1 << z)) * math.cos(math.radians(lat))


def tile_of(x: float, y: float, z: int) -> tuple[int, int]:
    n = 1 << z
    tx = int(math.floor((x + ORIGIN) / (2 * ORIGIN) * n))
    ty = int(math.floor((ORIGIN - y) / (2 * ORIGIN) * n))
    return min(max(tx, 0), n - 1), min(max(ty, 0), n - 1)


def tile_grid(bounds_wgs84, z: int) -> dict:
    x0, y0 = lonlat_to_merc(bounds_wgs84[0], bounds_wgs84[1])
    x1, y1 = lonlat_to_merc(bounds_wgs84[2], bounds_wgs84[3])
    c0, r0 = tile_of(x0, y1, z)
    c1, r1 = tile_of(x1, y0, z)
    cols, rows = list(range(c0, c1 + 1)), list(range(r0, r1 + 1))
    size = 2 * ORIGIN / (1 << z)
    bbox = (-ORIGIN + cols[0] * size, ORIGIN - (rows[-1] + 1) * size, -ORIGIN + (cols[-1] + 1) * size, ORIGIN - rows[0] * size)
    return {"z": z, "cols": cols, "rows": rows, "tiles": [(z, c, r) for r in rows for c in cols], "bbox_3857": bbox, "tile_m": size}


def choose_zoom(bounds_wgs84, target_m_per_px: float, max_tiles: int = TERRARIUM_MAX_TILES, max_zoom: int = TERRARIUM_MAX_ZOOM) -> dict:
    """The highest zoom whose cells are no coarser than the target, lowered until the window needs at most max_tiles tiles."""
    lat = (bounds_wgs84[1] + bounds_wgs84[3]) / 2.0
    z = next((z for z in range(0, max_zoom + 1) if tile_res_m(z, lat) <= target_m_per_px), max_zoom)
    ideal = z
    g = tile_grid(bounds_wgs84, z)
    while len(g["tiles"]) > max_tiles and z > 0:
        z -= 1
        g = tile_grid(bounds_wgs84, z)
    g["ideal_zoom"], g["limited_by_cap"], g["res_m"] = ideal, z < ideal, tile_res_m(z, lat)
    return g


_US_SHAPE = None


def us_shape():
    """The United States outline (bundled Natural Earth 110m countries, buffered 0.25 degrees for the coast) plus Puerto Rico and the
    US Virgin Islands as a box; None when the bundled layer is not there (the boxes alone decide then)."""
    global _US_SHAPE
    if _US_SHAPE is None:
        try:
            import geopandas as gpd
            from shapely.geometry import box
            from shapely.ops import unary_union
            g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
            us = g[g["ADM0_A3"].eq("USA")].geometry.union_all() if "ADM0_A3" in g.columns else None
            _US_SHAPE = unary_union([us.buffer(0.25), box(*US_BOXES[-1])]) if us is not None else False
        except Exception:  # noqa: BLE001
            _US_SHAPE = False
    return _US_SHAPE or None


# ------------------------------------------------------------------------------------------- 2.5.0 (T-01, T-08): coverage, not boxes
_SOURCES = None
_COVER: dict = {}
NATIONAL = ("canada-mrdem", "ign-rge-alti", "flanders-dhmv", "lantmateriet", "ga-lidar-5m", "gsi-japan")   # 3.1.0 (T-08): bare-earth national models
POLAR_SOURCES = ("arcticdem", "rema")                                       # the polar authorities, their voids filled by the next source
CHAINS = {"relief": ("3dep",) + NATIONAL + POLAR_SOURCES + ("copernicus", "terrarium"),       # T-09: a picture may use a surface model
          "analysis": ("3dep",) + NATIONAL + ("terrarium",) + POLAR_SOURCES + ("copernicus",)}  # profiles, line of sight: bare earth first, a surface model last
SOURCE_NAMES = ("auto", "3dep", "terrarium", "copernicus", "canada-mrdem", "gsi-japan", "ga-lidar-5m", "ign-rge-alti", "flanders-dhmv", "flanders-lidar",
                "lantmateriet", "arcticdem", "rema", "gebco")
FULL_SHARE = 0.995
SEAM_PX = 2


def sources() -> list[dict]:
    """data/terrain-sources.yaml: every elevation source with its coverage, licence and status (T-08)."""
    global _SOURCES
    if _SOURCES is None:
        try:
            _SOURCES = list(gc.load_yaml(gc.plugin_root() / "data" / "terrain-sources.yaml") or [])
        except Exception:  # noqa: BLE001
            _SOURCES = [{"id": "3dep", "coverage": "us+border", "buffer_deg": 0.3, "status": "active"},
                        {"id": "copernicus", "coverage": "world", "status": "active"}, {"id": "terrarium", "coverage": "world", "status": "active"}]
    return _SOURCES


def source_info(sid: str) -> dict:
    return next((s for s in sources() if s.get("id") == sid), {"id": sid, "coverage": "world"})


def coverage_shape(sid: str):
    """The coverage polygon of a source in lon/lat, or None for the whole world."""
    if sid in _COVER:
        return _COVER[sid]
    info = source_info(sid)
    shape = None
    cov = str(info.get("coverage") or "world")
    if cov.startswith("country:"):                # 3.1.0: a national source's own country (Natural Earth 110 m, grown by buffer_deg)
        from shapely.ops import unary_union
        try:
            import geopandas as gpd
            g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
            codes = [c.strip().upper() for c in cov.split(":", 1)[1].split(",")]
            sel = g[g["ISO_A2_EH"].isin(codes)]
            shape = unary_union(list(sel.geometry)).buffer(float(info.get("buffer_deg", 0.05))) if len(sel) else None
        except Exception:  # noqa: BLE001
            shape = None
        if shape is None:
            from shapely.geometry import Polygon
            shape = Polygon()
    elif cov.startswith("bbox:"):                 # 3.1.0: a regional source (Flanders) by its envelope; partial: true lets the next fill the rest
        from shapely.geometry import box
        W, S, E, N = (float(v) for v in cov.split(":", 1)[1].split(","))
        shape = box(W, S, E, N)
    elif cov in ("arctic", "antarctica"):
        from shapely.geometry import box
        shape = box(-180.0, 60.0, 180.0, 90.0) if cov == "arctic" else box(-180.0, -90.0, 180.0, -60.0)
    elif info.get("coverage") == "us+border":
        from shapely.geometry import box
        from shapely.ops import unary_union
        try:
            import geopandas as gpd
            g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
            us = g[g["ADM0_A3"].eq("USA")].geometry.union_all() if "ADM0_A3" in g.columns else None
        except Exception:  # noqa: BLE001
            us = None
        if us is not None:
            shape = unary_union([us.buffer(float(info.get("buffer_deg", 0.3))), box(*US_BOXES[-1])])
        else:
            shape = unary_union([box(*b) for b in US_BOXES])
    _COVER[sid] = shape
    return shape


def coverage_share(sid: str, bounds_wgs84) -> float:
    """The share of the window (lon/lat box) inside the source's coverage, 0 to 1 (T-01: a coverage test, not a box and a centre test)."""
    from shapely.geometry import box
    shape = coverage_shape(sid)
    if shape is None:
        return 1.0
    w = box(*bounds_wgs84)
    return float(shape.intersection(w).area / w.area) if w.area > 0 else 0.0


def chain_for(source: str | None, purpose: str) -> list[str]:
    """T-03, T-09: the ordered sources for a purpose; a source named in the spec is first, never the only one."""
    base = list(CHAINS.get(purpose, CHAINS["relief"]))
    src = (source or "auto").lower()
    return ([src] + [s for s in base if s != src]) if src != "auto" else base


def credential_missing(info: dict, store=None) -> str | None:
    """3.1.0: the credential a source needs (Lantmäteriet's Geotorget login) when it is not registered on this computer, else None."""
    name = info.get("credential")
    if not name:
        return None
    try:
        import credentials as cr
        return None if (store or cr.Store()).get(name) else name
    except Exception:  # noqa: BLE001 - no credential store: the source is not used
        return name


def plan_sources(bounds_wgs84, source: str | None, purpose: str, skipped: list | None = None, store=None) -> list[tuple[str, float]]:
    """[(source, share of the window it covers)] in the order they fill the window, ending with the first that covers all of it. A source
    whose credential is not registered is passed over and named in `skipped` (3.1.0), so the map says what a login would add."""
    out = []
    for s in chain_for(source, purpose):
        info = source_info(s)
        if str(info.get("status", "active")) != "active" and s != str(source or "").lower():      # GEBCO only when it is asked for by name
            continue
        if credential_missing(info, store) and s != str(source or "").lower():
            if skipped is not None and coverage_share(s, bounds_wgs84) > 0:
                skipped.append(s)
            continue
        sh = coverage_share(s, bounds_wgs84)
        if sh <= 0.0:
            continue
        out.append((s, sh))
        if sh >= FULL_SHARE and not info.get("partial"):        # 3.1.0: a partial source (GA's LiDAR) is always followed by one that fills its gaps
            break
    return out


def in_us(bounds_wgs84) -> bool:
    """Whether 3DEP covers the whole window (2.5.0: by its coverage polygon; kept for older callers)."""
    return coverage_share("3dep", bounds_wgs84) >= FULL_SHARE


def _in_us_old(bounds_wgs84) -> bool:
    """Whether 3DEP covers the window: its centre lies in the United States (the lower 48, Alaska, Hawaii, Puerto Rico, the Virgin Islands)."""
    x0, y0, x1, y1 = bounds_wgs84
    if not any(x0 >= bx0 - 0.5 and x1 <= bx1 + 0.5 and y0 >= by0 - 0.5 and y1 <= by1 + 0.5 for bx0, by0, bx1, by1 in US_BOXES):
        return False
    shape = us_shape()
    if shape is None:
        return True
    from shapely.geometry import Point
    return bool(shape.contains(Point((x0 + x1) / 2.0, (y0 + y1) / 2.0)))


def _bounds(b):
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        raise rio.RasterError("bounds must be four numbers: lon0 lat0 lon1 lat1 (WGS84)") from None
    if not (-180 <= x0 < x1 <= 180 and -90 <= y0 < y1 <= 90):
        raise rio.RasterError(f"bounds are not a lon/lat box: {b}")
    return x0, y0, x1, y1


def cache_key(source: str, bounds, res: float) -> str:
    s = json.dumps({"source": source, "bounds": [round(float(v), 5) for v in bounds], "res": round(float(res), 2)}, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def cache_root(cache_dir=None) -> Path:
    return (Path(cache_dir) if cache_dir else gc.workdir(create=True)) / "terrain"


# --------------------------------------------------------------------------------------------------------------- terrarium

def decode_terrarium(png_bytes: bytes) -> np.ndarray:
    """Terrarium PNG -> float32 metres: (R * 256 + G + B / 256) - 32768."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0


class TileFetcher:
    """Fetches z/x/y Terrarium tiles through SafeHTTP with a raw-tile cache; at most `concurrency` requests at once."""

    def __init__(self, http=None, cache: Path | None = None, concurrency: int = TERRARIUM_CONCURRENCY, url_template: str = TERRARIUM_URL):
        self.http = http or safe_http(timeout_s=30, max_bytes=2_000_000)
        self.cache, self.concurrency, self.tpl = cache, max(1, min(int(concurrency), TERRARIUM_CONCURRENCY)), url_template
        self.fetched = self.cached = 0
        self.errors: list[str] = []

    def path(self, z, x, y) -> Path | None:
        return (self.cache / str(z) / str(x) / f"{y}.png") if self.cache else None

    def get(self, z: int, x: int, y: int) -> bytes | None:
        p = self.path(z, x, y)
        if p is not None and p.is_file():
            self.cached += 1
            return p.read_bytes()
        url = self.tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        try:
            r = self.http.get(url, None, expect="tile")
        except oh.ServiceError as e:
            if e.auth:
                raise
            self.errors.append(f"{z}/{x}/{y}: {oh.untrusted(e, 100)}")
            return None
        if r.status != 200 or not r.content.startswith(b"\x89PNG"):
            self.errors.append(f"{z}/{x}/{y}: HTTP {r.status}")
            return None
        self.fetched += 1
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
        return r.content

    def get_many(self, tiles: list[tuple[int, int, int]]) -> dict:
        if self.concurrency <= 1 or len(tiles) <= 1:
            return {t: self.get(*t) for t in tiles}
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            return dict(zip(tiles, ex.map(lambda t: self.get(*t), tiles)))


def mosaic_terrarium(grid: dict, tiles: dict) -> tuple[np.ndarray, object, list[str]]:
    """Decoded tiles into one float32 array on the grid's EPSG:3857 bbox; missing tiles are nodata."""
    from rasterio.transform import from_bounds
    cols, rows = grid["cols"], grid["rows"]
    W, H = 256 * len(cols), 256 * len(rows)
    out = np.full((H, W), NODATA, np.float32)
    missing = []
    for (z, x, y), data in tiles.items():
        if data is None:
            missing.append(f"{z}/{x}/{y}")
            continue
        try:
            a = decode_terrarium(data)
        except Exception:  # noqa: BLE001
            missing.append(f"{z}/{x}/{y}")
            continue
        i, j = cols.index(x), rows.index(y)
        out[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = a
    x0, y0, x1, y1 = grid["bbox_3857"]
    return out, from_bounds(x0, y0, x1, y1, W, H), missing


def fetch_terrarium(bounds, target_m_per_px: float, out: Path, *, fetcher: TileFetcher | None = None, max_tiles: int = TERRARIUM_MAX_TILES, http=None,
                    tile_cache: Path | None = None) -> dict:
    g = choose_zoom(bounds, target_m_per_px, max_tiles)
    f = fetcher or TileFetcher(http, tile_cache)
    tiles = f.get_many(g["tiles"])
    arr, t, missing = mosaic_terrarium(g, tiles)
    if len(missing) > max(1, 0.1 * len(g["tiles"])):                   # one tile, or a tenth of them, may be blank; more is a failed fetch
        raise oh.ServiceError(f"{len(missing)} of {len(g['tiles'])} terrain tiles could not be read ({'; '.join(f.errors[:3]) or 'missing'}); no DEM was written", "MAP-SVC-010")
    marr = np.ma.masked_equal(arr, NODATA)
    w = rio.write_geotiff(out, marr, t, "EPSG:3857", nodata=NODATA)
    return {"source": "terrarium", "entry": TERRARIUM_ENTRY, "crs": "EPSG:3857", "zoom": g["z"], "ideal_zoom": g["ideal_zoom"], "limited_by_cap": g["limited_by_cap"],
            "res_m": g["res_m"], "tiles": len(g["tiles"]), "fetched": f.fetched, "from_cache": f.cached, "missing": missing, "bbox_3857": list(g["bbox_3857"]),
            "bytes": w["bytes"], "sha256": w["sha256"], "path": str(out), "requests": list(getattr(f.http, "audit", oh.Audit()).calls),
            "errors": f.errors[:10], "pixels": [arr.shape[1], arr.shape[0]], "nodata": NODATA}


# --------------------------------------------------------------------------------------------------------------- 3DEP

def threedep_size(bounds, target_m_per_px: float, max_px: int = THREEDEP_MAX_PX) -> tuple[int, int, float, bool]:
    """(width, height, metres per pixel, capped) for an exportImage of the lon/lat box at the target resolution."""
    x0, y0, x1, y1 = bounds
    lat = (y0 + y1) / 2.0
    w_m = (x1 - x0) * rio.M_PER_DEG_LON_EQ * math.cos(math.radians(lat))
    h_m = (y1 - y0) * rio.M_PER_DEG_LAT
    W, H = max(1, int(round(w_m / target_m_per_px))), max(1, int(round(h_m / target_m_per_px)))
    capped = max(W, H) > max_px
    if capped:
        k = max_px / float(max(W, H))
        W, H = max(1, int(round(W * k))), max(1, int(round(H * k)))
    return W, H, w_m / W, capped


THREEDEP_FALLBACK_STEPS = 3      # 1.2.0 (Days 5 and 8): a failed exportImage is asked again at 2x, 4x and 8x the cell size


def _retryable(ex: Exception) -> bool:
    """A server failure or a timeout is worth one coarser try; a refusal (4xx) is not."""
    import httpx as _hx
    st = getattr(ex, "status", None)
    if isinstance(ex, oh.ServiceError):
        return getattr(ex, "auth", False) is False and (st is None or st >= 500)
    return isinstance(ex, (_hx.TimeoutException, _hx.TransportError, TimeoutError))


def fetch_3dep_with_fallback(bounds, target_m_per_px: float, out: Path, *, http=None, max_px: int = THREEDEP_MAX_PX, url: str = THREEDEP_URL,
                             steps: int = THREEDEP_FALLBACK_STEPS) -> tuple[dict, list[tuple[float, str]]]:
    """fetch_3dep, and when the service fails (HTTP 5xx, a timeout, a body that is not a TIFF) the same window at twice the cell size, up to
    `steps` times. Returns (record, [(res_m, error) for each attempt..., (res_m, 'ok')]). The last error is raised when every attempt fails."""
    tried: list[tuple[float, str]] = []
    res = float(target_m_per_px)
    for k in range(steps + 1):
        try:
            rec = fetch_3dep(bounds, res, out, http=http, max_px=max_px, url=url)
            tried.append((res, "ok"))
            return rec, tried
        except Exception as ex:  # noqa: BLE001
            tried.append((res, oh.untrusted(str(ex), 120)))
            if k == steps or not _retryable(ex):
                raise
            if k == 0:
                # 1.19.0 (Day 17 issue B2): before giving up resolution, the same window in 2 x 2 overlapping quarters at the asked cell size
                # (the Grand Canyon at 12 m failed whole and came back at 24 m, while four 12 m quarters all succeeded)
                try:
                    rec = fetch_3dep_tiled(bounds, res, out, http=http, max_px=max_px, url=url)
                    tried.append((res, "ok in 4 quarters"))
                    return rec, tried
                except Exception as ex2:  # noqa: BLE001
                    tried.append((res, "quarters: " + oh.untrusted(str(ex2), 100)))
                    if not _retryable(ex2):
                        raise
            res *= 2.0
    raise RuntimeError("unreachable")


THREEDEP_SPLIT = 2               # 1.19.0: quarters (2 x 2) before a coarser cell size
THREEDEP_OVERLAP_CELLS = 4       # each part reaches this many cells into its neighbours, so the mosaic has no seam


def fetch_3dep_tiled(bounds, target_m_per_px: float, out: Path, *, http=None, max_px: int = THREEDEP_MAX_PX, url: str = THREEDEP_URL,
                     n: int = THREEDEP_SPLIT) -> dict:
    """The window as n x n overlapping exportImage parts at the same cell size, mosaicked into one GeoTIFF at `out` (the first value where parts
    overlap; the cells are the service's own, so overlaps agree). A part that fails raises, and the caller then coarsens the whole window."""
    import tempfile
    x0, y0, x1, y1 = bounds
    _W, _H, res_m, _capped = threedep_size(bounds, target_m_per_px, max_px * n)
    lat = (y0 + y1) / 2.0
    ox = THREEDEP_OVERLAP_CELLS * res_m / (rio.M_PER_DEG_LON_EQ * math.cos(math.radians(lat)))
    oy = THREEDEP_OVERLAP_CELLS * res_m / rio.M_PER_DEG_LAT
    xs = [x0 + (x1 - x0) * i / n for i in range(n + 1)]
    ys = [y0 + (y1 - y0) * j / n for j in range(n + 1)]
    recs, paths = [], []
    with tempfile.TemporaryDirectory(prefix="gm-3dep-") as td:
        for j in range(n):
            for i in range(n):
                b = (max(x0, xs[i] - ox), max(y0, ys[j] - oy), min(x1, xs[i + 1] + ox), min(y1, ys[j + 1] + oy))
                q = Path(td) / f"part_{j}_{i}.tif"
                recs.append(fetch_3dep(b, res_m, q, http=http, max_px=max_px, url=url))
                paths.append(q)
        import rasterio
        from rasterio.merge import merge
        srcs = [rasterio.open(q) for q in paths]
        try:
            arr, tr = merge(srcs, nodata=NODATA)
            crs = srcs[0].crs
        finally:
            for d in srcs:
                d.close()
    a = arr[0].astype(np.float32)
    marr = np.ma.masked_array(a, mask=(a == NODATA) | ~np.isfinite(a))
    w = rio.write_geotiff(out, marr, tr, crs or "EPSG:4326", nodata=NODATA)
    first = recs[0]
    return {**first, "res_m": res_m, "res_deg": [float(abs(tr.a)), float(abs(tr.e))], "pixels": [int(a.shape[1]), int(a.shape[0])],
            "requests": [c for r_ in recs for c in r_.get("requests", [])], "bytes": w["bytes"], "sha256": w["sha256"], "path": str(out),
            "bbox_wgs84": list(bounds), "parts": n * n, "missing_share": float(marr.mask.mean()) if np.ndim(marr.mask) else 0.0}


def fetch_3dep(bounds, target_m_per_px: float, out: Path, *, http=None, max_px: int = THREEDEP_MAX_PX, url: str = THREEDEP_URL) -> dict:
    x0, y0, x1, y1 = bounds
    W, H, res_m, capped = threedep_size(bounds, target_m_per_px, max_px)
    h = http or safe_http(timeout_s=120, max_bytes=200_000_000)
    params = {"bbox": f"{x0!r},{y0!r},{x1!r},{y1!r}", "bboxSR": "4326", "imageSR": "4326", "size": f"{W},{H}", "format": "tiff", "pixelType": "F32",
              "noDataInterpretation": "esriNoDataMatchAny", "interpolation": "RS_BilinearInterpolation", "f": "image"}
    r = h.get(url.rstrip("/") + "/exportImage", params, expect="any")
    if r.content[:4] not in (b"II*\x00", b"MM\x00*"):
        raise oh.ServiceError(f"the elevation service did not return a TIFF (HTTP {getattr(r, 'status', '?')}): {oh.untrusted(r.content[:300].decode('utf-8', 'replace'), 160)}",
                              "MAP-SVC-013", status=getattr(r, "status", None) if getattr(r, "status", 200) != 200 else None)
    from rasterio.io import MemoryFile
    with MemoryFile(r.content) as mem, mem.open() as ds:
        a = ds.read(1).astype(np.float32)
        t, crs = ds.transform, ds.crs
        nd = ds.nodata
    bad = ~np.isfinite(a) | (a < -1000) | (a > 9000)
    if nd is not None:
        bad |= a == nd
    marr = np.ma.masked_array(a, mask=bad)
    w = rio.write_geotiff(out, marr, t, crs or "EPSG:4326", nodata=NODATA)
    return {"source": "3dep", "entry": THREEDEP_ENTRY, "crs": rio.crs_string(crs or "EPSG:4326"), "res_m": res_m, "res_deg": [float(abs(t.a)), float(abs(t.e))],
            "pixels": [W, H], "capped_to": max_px if capped else None, "requests": list(h.audit.calls), "bytes": w["bytes"], "sha256": w["sha256"], "path": str(out),
            "nodata": NODATA, "nodata_rule": "cells the service marked nodata, or outside -1000 to 9000 m, are nodata", "bbox_wgs84": list(bounds),
            "missing_share": float(bad.mean())}


# --------------------------------------------------------------------------------------------------------------- the one call

def dem_for(bounds_wgs84, target_m_per_px: float = 30.0, source: str = "auto", cache_dir=None, *, terms_acknowledged: bool = False, dem_path=None,
            http=None, fetcher: TileFetcher | None = None, max_tiles: int = TERRARIUM_MAX_TILES, max_px: int = THREEDEP_MAX_PX,
            surface_ok: bool = False, purpose: str | None = None, lidar_crs: str | None = None) -> dict:
    """A local DEM for the window. -> {ok, findings, path, record}.

    2.5.0 (T-01, T-03, T-09): the sources come from the purpose's chain (relief: 3DEP, Copernicus, Terrarium; analysis: 3DEP, Terrarium,
    Copernicus), a source named in the spec first and never the only one. Each source covers what its coverage polygon says
    (data/terrain-sources.yaml); a window one source covers only in part is mosaicked from the next, the seam feathered over 2 cells, so a
    Washington window that reaches British Columbia draws relief from 3DEP and Copernicus and credits both. `surface_ok` is the old name of
    purpose relief. terms_acknowledged is kept for older callers and ignored (0.23.0)."""
    fs: list[dict] = []
    if dem_path:
        import lidar
        if lidar.is_lidar(dem_path):                 # 3.1.0: a LAS or LAZ file is gridded once (bare earth, 1 m) and read as the DEM
            dem_path = lidar.as_raster(dem_path, cell=max(1.0, float(target_m_per_px or 1.0)) if bounds_wgs84 else 1.0, crs=lidar_crs)
            fs.append(finding("MAP-TER-001", "INFO", f"the lidar file was gridded to a bare-earth DEM ({Path(dem_path).name}; ground and water points)"))
        info = rio.open_info(dem_path)
        rec = {"source": "user", "path": str(dem_path), "crs": info["crs"], "bounds": info["bounds"], "res": info["res"], "nodata": info["nodata"], "requests": [],
               "bytes": Path(dem_path).stat().st_size, "sha256": hashlib.sha256(Path(dem_path).read_bytes()).hexdigest(), "retrieved_at": today(),
               "attribution": None, "terms_acknowledged": None, "cached": False, "note": "a DEM the person supplied; nothing was fetched"}
        return {"ok": True, "findings": fs, "path": str(dem_path), "record": rec}
    b = _bounds(bounds_wgs84)
    res = float(target_m_per_px)
    if res <= 0:
        raise rio.RasterError("target_m_per_px must be positive")
    src = (source or "auto").lower()
    import dem_sources as ds_
    if src not in SOURCE_NAMES:
        raise rio.RasterError("source is auto, 3dep, copernicus, terrarium or one of " + ", ".join(ds_.FETCHERS))

    purpose = purpose or ("relief" if surface_ok else "analysis")
    skipped: list = []
    plan = plan_sources(b, src, purpose, skipped)
    for s in skipped:
        si = source_info(s)
        fs.append(finding("MAP-TER-001", "INFO", f"{si.get('title') or s} covers this window at {si.get('native_m')} m but needs your free login "
                          f"(map-credentials add {si.get('credential')}); the next source is used"))
    kw = dict(http=http, fetcher=fetcher, max_tiles=max_tiles, max_px=max_px)
    if src != "auto" and (not plan or plan[0][0] != src):
        fs.append(finding("MAP-TER-001", "INFO", f"{source_info(src).get('title') or src} has no data for this window (its coverage does not reach it); "
                          f"{plan[0][0] if plan else 'terrarium'} is used, the next source for {purpose}"))
    if len(plan) == 1:
        s = plan[0][0]
        if src == "auto":
            us = s == "3dep"
            fs.append(finding("MAP-TER-001", "INFO", f"terrain source {s}: " + ("the window is inside the United States, where USGS 3DEP is the authority (public domain)" if us else
                                                                              "the window is outside the United States, so the Copernicus DEM GLO-30 is used (keyless; its notice is printed)"
                                                                              if s == "copernicus" else
                                                                              "the window is outside the United States, so the AWS Terrain Tiles (Terrarium) are used; attribution is required")))
        out = _single(s, b, res, cache_root(cache_dir), fs, named=src != "auto", source_arg=source, cache_dir=cache_dir, target=target_m_per_px, **kw)
    else:
        out = _mosaic(plan, b, res, cache_root(cache_dir), fs, cache_dir=cache_dir, target=target_m_per_px, **kw)
    if purpose == "analysis" and out.get("ok"):               # T-09: an analysis says which elevation it stands on, and whether it is bare earth
        used = [x["source"] for x in (out["record"].get("sources") or [])] or [out["record"].get("source")]
        kinds = "; ".join(f"{u}: {source_info(u).get('surface') or 'unknown surface'}" for u in used)
        bare = all("bare earth" in str(source_info(u).get("surface") or "") for u in used)
        out["record"]["bare_earth"] = bare
        out["findings"].append(finding("MAP-TER-001", "INFO", f"analysis DEM from {kinds}" + ("" if bare else
                                       "; not bare earth everywhere: buildings, trees or mixed sources are in the heights, so a profile or a line of sight is optimistic or pessimistic there")))
    return out


def _single(src: str, b, res: float, root: Path, fs: list, *, named: bool, source_arg, cache_dir, target, http=None, fetcher=None,
            max_tiles: int = TERRARIUM_MAX_TILES, max_px: int = THREEDEP_MAX_PX) -> dict:
    """One source for the whole window (the 2.4 route), cached by (source, bounds, resolution)."""
    if src == "terrarium":                      # 0.23.0: a default outside the US; its credit is printed for you, so there is nothing to acknowledge
        fs.append(finding("MAP-WEB-011", "INFO", "the AWS Terrain Tiles need their sources credited; the credit is in the record and is printed as the map's terrain credit"))
    key = cache_key(src, b, res)
    out = root / src / f"{key}.tif"
    recp = out.with_suffix(".json")
    if out.is_file() and recp.is_file():
        rec = json.loads(recp.read_text(encoding="utf-8"))
        if hashlib.sha256(out.read_bytes()).hexdigest() == rec.get("sha256"):
            rec["cached"], rec["requests"] = True, []
            fs.append(finding("MAP-TER-002", "INFO", f"DEM read from the cache ({out.name}, fetched {rec.get('retrieved_at')}); nothing was requested"))
            return {"ok": True, "findings": fs, "path": str(out), "record": rec}
    out.parent.mkdir(parents=True, exist_ok=True)
    if src == "copernicus":
        try:
            rec = fetch_copernicus(b, max(res, 30.0), out, root, http=http)
        except oh.ServiceError as ex:
            if "limit is" in str(ex) and source_arg in (None, "auto"):          # a wide window: Terrarium chooses its zoom by the tile cap
                fs.append(finding("MAP-TER-003", "INFO", f"{ex}; the AWS Terrain Tiles are used for this window instead"))
                return _single("terrarium", b, res, root, fs, named=False, source_arg="terrarium", cache_dir=cache_dir, target=target, http=http,
                               fetcher=fetcher, max_tiles=max_tiles, max_px=max_px)
            raise
        if rec.get("fallback_glo90"):
            fs.append(finding("MAP-TER-003", "INFO", f"{len(rec['fallback_glo90'])} tile(s) are not in the public GLO-30 release, so GLO-90 was used there: "
                              + ", ".join(rec["fallback_glo90"][:4])))
        if rec.get("route") == "cog-overviews":
            fs.append(finding("MAP-TER-003", "INFO", f"{rec['tiles']} Copernicus tiles read as Cloud-Optimized GeoTIFF overviews at {rec['res_m']:g} m "
                              f"(range reads, about {rec.get('estimated_mb', 0):.0f} MB), not as whole tiles"))
        fs.append(finding("MAP-TER-001", "INFO", "the Copernicus DEM is a surface model: buildings and trees are in it (fine for relief, not a bare-earth profile)"))
    elif src not in ("3dep", "terrarium"):                                  # 3.1.0 (T-08): national and polar sources, GEBCO
        import dem_sources as ds_
        try:
            rec = ds_.fetch(src, b, res, out, http=http)
        except ds_.DemSourceError as ex:
            nxt = next((s for s in chain_for(None, "relief") if s not in (src, "3dep") and coverage_share(s, b) > 0), "terrarium")
            fs.append(finding("MAP-TER-003", "INFO", f"{ex}; {nxt} is used for this window instead"))
            return _single(nxt, b, res, root, fs, named=False, source_arg=nxt, cache_dir=cache_dir, target=target, http=http,
                           fetcher=fetcher, max_tiles=max_tiles, max_px=max_px)
        if rec.get("missing_share", 0) > 0.05:
            fs.append(finding("MAP-TER-003", "INFO", f"{source_info(src).get('title') or src} has no elevation for {rec['missing_share']:.0%} of the window"))
    elif src == "terrarium":
        rec = fetch_terrarium(b, res, out, fetcher=fetcher, max_tiles=max_tiles, http=http, tile_cache=root / "terrarium-tiles")
        rec["attribution"] = terrarium_attribution(b)                       # 2.5.0 (T-05): only the regional sources this window uses
        rec["terms_url"] = TERRARIUM_TERMS_URL
        rec["license"] = "attribution"
        if rec["limited_by_cap"]:
            fs.append(finding("MAP-TER-003", "WARN", f"the tile cap ({max_tiles}) holds the terrain at zoom {rec['zoom']} (about {rec['res_m']:.0f} m per pixel); "
                              f"zoom {rec['ideal_zoom']} would reach {res:g} m. A smaller window, or a coarser target, keeps the detail"))
        if rec["missing"]:
            fs.append(finding("MAP-TER-003", "WARN", f"{len(rec['missing'])} terrain tile(s) are nodata: {', '.join(rec['missing'][:5])}"))
    else:
        rec, tried = fetch_3dep_with_fallback(b, res, out, http=http, max_px=max_px)
        if len(tried) > 1:
            fs.append(finding("MAP-TER-003", "WARN", f"the 3DEP elevation service failed at {tried[0][0]:g} m ({tried[0][1]}); the DEM was read at {tried[-1][0]:g} m "
                              f"after {len(tried) - 1} coarser attempt(s). A smaller window, or terrain.resolution_m, asks for less"))
        rec["fallback"] = [{"res_m": r_, "error": e_} for r_, e_ in tried[:-1]]
        rec["attribution"] = THREEDEP_ATTRIBUTION
        rec["license"] = "public-domain"
        if rec["capped_to"]:
            fs.append(finding("MAP-TER-003", "WARN", f"the 3DEP request is capped at {max_px} pixels a side, so the DEM is about {rec['res_m']:.0f} m per pixel, not {res:g}; "
                              "a smaller window keeps the detail"))
        if rec["missing_share"] > 0.05:
            fs.append(finding("MAP-TER-003", "WARN", f"{rec['missing_share']:.0%} of the window has no 3DEP elevation (water, or outside the coverage)"))
    rec.update({"terms_acknowledged": None, "retrieved_at": today(), "bounds_wgs84": list(b), "target_m_per_px": res,
                "cache_key": key, "cached": False, "user_agent": getattr(http or getattr(fetcher, "http", None), "user_agent", None) or oh.default_user_agent()})
    rec.setdefault("request", {"source": src, "bbox_wgs84": list(b), "res_m": res})      # 2.5.0 (I-22): enough to fetch the same DEM again where it is needed
    recp.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "findings": fs, "path": str(out), "record": rec}


def _mosaic(plan: list[tuple[str, float]], b, res: float, root: Path, fs: list, *, cache_dir, target, **kw) -> dict:
    """T-03: one DEM from several sources: each part of the window from the first source that covers it (by coverage, and where that source
    returned nodata, R3), the next source warped onto the first one's grid, a 2-cell feathered seam. Cached as a mosaic of its own."""
    import rasterio
    from rasterio.warp import reproject, Resampling
    names = [s for s, _ in plan]
    mkey = cache_key("mosaic-" + "+".join(names), b, res)
    out = root / "mosaic" / f"{mkey}.tif"
    recp = out.with_suffix(".json")
    if out.is_file() and recp.is_file():
        rec = json.loads(recp.read_text(encoding="utf-8"))
        if hashlib.sha256(out.read_bytes()).hexdigest() == rec.get("sha256"):
            rec["cached"], rec["requests"] = True, []
            fs.append(finding("MAP-TER-002", "INFO", f"DEM mosaic read from the cache ({out.name}); nothing was requested"))
            return {"ok": True, "findings": fs, "path": str(out), "record": rec}
    parts = []
    for s, share in plan:
        sub: list = []
        r = _single(s, b, res, root, sub, named=False, source_arg=s, cache_dir=cache_dir, target=target, **kw)
        fs += [f for f in sub if f["id"] != "MAP-TER-001" or "surface model" in f["message"]]
        parts.append((s, share, r))
    with rasterio.open(parts[0][2]["path"]) as ds:
        base = ds.read(1).astype("float64")
        tr, crs = ds.transform, ds.crs
        bnd = ds.bounds
    valid = np.isfinite(base) & (base > NODATA + 1)
    cov = coverage_shape(parts[0][0])
    if cov is not None:
        from rasterio.features import geometry_mask
        from rasterio.warp import transform_geom
        from shapely.geometry import box, mapping
        g = cov.intersection(box(*b))
        if not g.is_empty:
            gg = transform_geom("EPSG:4326", crs, mapping(g)) if str(crs).upper() not in ("EPSG:4326", "OGC:CRS84") else mapping(g)
            valid &= ~geometry_mask([gg], out_shape=base.shape, transform=tr)
    used = {parts[0][0]: float(valid.mean())}
    result = np.where(valid, base, np.nan)
    for s, _share, r in parts[1:]:
        if valid.all():
            break
        with rasterio.open(r["path"]) as ds:
            nxt = np.full(base.shape, np.nan)
            reproject(rasterio.band(ds, 1), nxt, src_transform=ds.transform, src_crs=ds.crs, dst_transform=tr, dst_crs=crs,
                      src_nodata=ds.nodata if ds.nodata is not None else NODATA, dst_nodata=np.nan, resampling=Resampling.bilinear)
        ok = np.isfinite(nxt) & (nxt > NODATA + 1)
        w = _seam_weights(valid, SEAM_PX)
        blend = np.where(ok & valid, w * result + (1.0 - w) * nxt, result)
        fill = ~valid & ok
        used[s] = float(fill.mean())
        result = np.where(fill, nxt, blend)
        valid = valid | ok
    marr = np.ma.masked_invalid(result.astype("float32"))
    out.parent.mkdir(parents=True, exist_ok=True)
    w_ = rio.write_geotiff(out, marr, tr, crs or "EPSG:4326", nodata=NODATA)
    srcs = [{"source": s, "share": round(used.get(s, 0.0), 4), "entry": r["record"].get("entry"), "attribution": r["record"].get("attribution"),
             "cache_key": r["record"].get("cache_key")} for s, _sh, r in parts if used.get(s, 0.0) > 0]
    attr = " ".join(x["attribution"] for x in srcs if x.get("attribution"))
    rec = {"source": "mosaic", "sources": srcs, "entry": srcs[0]["entry"] if srcs else None, "crs": rio.crs_string(crs or "EPSG:4326"),
           "res_m": float(parts[0][2]["record"].get("res_m") or res), "bytes": w_["bytes"], "sha256": w_["sha256"], "path": str(out),
           "requests": [q for _s, _sh, r in parts for q in (r["record"].get("requests") or [])], "nodata": NODATA, "seam_px": SEAM_PX,
           "attribution": attr or None, "license": "public-domain" if all(x["source"] == "3dep" for x in srcs) else "attribution",
           "surface": "mixed: " + ", ".join(f"{x['source']} {x['share']:.0%}" for x in srcs), "bounds_wgs84": list(b), "target_m_per_px": res,
           "retrieved_at": today(), "cache_key": mkey, "cached": False, "terms_acknowledged": None,
           "request": {"sources": names, "bbox_wgs84": list(b), "res_m": res}, "missing_share": float(1.0 - valid.mean()), "grid_bounds": list(bnd)}
    recp.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    fs.append(finding("MAP-TER-001", "INFO", "terrain mosaic: " + "; ".join(f"{x['source']} for {x['share']:.0%} of the window" for x in srcs)
                      + " (each part from the first source that covers it, the seam feathered over 2 cells)"))
    return {"ok": True, "findings": fs, "path": str(out), "record": rec}


def _seam_weights(valid, px: int):
    """1 deep inside the first source's area, falling to 0 over `px` cells toward its edge (the feathered seam)."""
    if px <= 0 or valid.all() or not valid.any():
        return valid.astype(float)
    try:
        from scipy.ndimage import distance_transform_edt
        d = distance_transform_edt(valid)
    except Exception:  # noqa: BLE001 - without scipy the seam is a hard edge
        return valid.astype(float)
    return np.clip(d / float(px + 1), 0.0, 1.0)


# 2.5.0 (T-05): Terrarium's sources by region (tilezen joerd attribution), so a map credits only those its window uses
TERRARIUM_PARTS = [
    ("ARCTIC", "ArcticDEM terrain data DEM(s) were created from DigitalGlobe, Inc., imagery and funded under National Science Foundation awards 1043681, 1559691, and 1542736"),
    ("AUS", "Australia terrain data (c) Commonwealth of Australia (Geoscience Australia) 2017"),
    ("AUT", "Austria terrain data (c) offene Daten Osterreichs - Digitales Gelandemodell (DGM) Osterreich"),
    ("CAN", "Canada terrain data contains information licensed under the Open Government Licence - Canada"),
    ("EUROPE", "Europe terrain data produced using Copernicus data and information funded by the European Union - EU-DEM layers"),
    ("SEA", "Global ETOPO1 terrain data U.S. National Oceanic and Atmospheric Administration"),
    ("MEX", "Mexico terrain data source: INEGI, Continental relief, 2016"),
    ("NZL", "New Zealand terrain data Copyright 2011 Crown copyright (c) Land Information New Zealand and the New Zealand Government (All rights reserved)"),
    ("NOR", "Norway terrain data (c) Kartverket"),
    ("GBR", "United Kingdom terrain data (c) Environment Agency copyright and/or database right 2015. All rights reserved"),
    ("USA", "United States 3DEP (formerly NED)"),
]
TERRARIUM_GLOBAL = "global GMTED2010 and SRTM terrain data courtesy of the U.S. Geological Survey"


def terrarium_attribution(bounds_wgs84) -> str:
    """The Terrarium credit for the window: the regional sources it touches, then the global SRTM and GMTED2010 line. Falls back to the whole
    text when the country shapes cannot be read."""
    try:
        import geopandas as gpd
        from shapely.geometry import box
        g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
        w = box(*bounds_wgs84)
        hit = g[g.geometry.intersects(w)]
        codes = set(hit["ADM0_A3"]) if "ADM0_A3" in hit.columns else set()
        if "CONTINENT" in hit.columns and (hit["CONTINENT"] == "Europe").any():
            codes.add("EUROPE")
        land = hit.geometry.union_all() if len(hit) else None
        if land is None or land.area < w.area * 0.98:
            codes.add("SEA")
        if bounds_wgs84[3] > 60:
            codes.add("ARCTIC")
    except Exception:  # noqa: BLE001
        return TERRARIUM_ATTRIBUTION
    parts = [txt for code, txt in TERRARIUM_PARTS if code in codes]
    if "USA" in codes:
        parts = [p for p in parts if not p.startswith("United States")] + ["United States 3DEP (formerly NED) and " + TERRARIUM_GLOBAL]
        return "Elevation: Terrain Tiles on AWS (Mapzen Terrarium). " + "; ".join(parts) + "."
    return "Elevation: Terrain Tiles on AWS (Mapzen Terrarium). " + "; ".join(parts + [TERRARIUM_GLOBAL[0].upper() + TERRARIUM_GLOBAL[1:]]) + "."


# --------------------------------------------------------------------------------------------------------------- Copernicus DEM (1.5.0)

def cop_tile_name(lat: int, lon: int, glo: int = 30) -> str:
    """The bucket's name for the 1-degree tile whose south-west corner is (lat, lon): Copernicus_DSM_COG_10_N37_00_E023_00_DEM (readme.html)."""
    ns = f"{'N' if lat >= 0 else 'S'}{abs(int(lat)):02d}_00"
    ew = f"{'E' if lon >= 0 else 'W'}{abs(int(lon)):03d}_00"
    return f"Copernicus_DSM_COG_{COP_ARCSEC[glo]}_{ns}_{ew}_DEM"


def cop_tiles_for(bounds) -> list[tuple[int, int]]:
    W, S, E, N = bounds
    return [(la, lo) for la in range(math.floor(S), math.ceil(N)) for lo in range(math.floor(W), math.ceil(E))]


def cop_tile_list(glo: int, root: Path, http=None) -> set[str]:
    """The bucket's tileList.txt (the tiles that exist; the sea and the unreleased GLO-30 tiles are absent), read once and kept."""
    p = root / "copernicus" / f"tileList-{glo}.txt"
    if p.is_file():
        return set(p.read_text(encoding="utf-8").split())
    h = http or safe_http(max_bytes=5_000_000)
    r = h.get(COP_BUCKETS[glo] + "tileList.txt", max_bytes=5_000_000)
    if r.status != 200:
        raise oh.ServiceError(f"the Copernicus tile list answered HTTP {r.status}", "MAP-SVC-010")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    return set(r.content.decode("ascii", "replace").split())


def fetch_copernicus(bounds, target_m_per_px: float, out: Path, root: Path, *, http=None, max_tiles: int = COP_MAX_TILES) -> dict:
    """The window's DEM from the Copernicus GLO-30 Public tiles (whole 1-degree COGs through SafeHTTP, cached), mosaicked and resampled to the
    target cell size. A tile absent from GLO-30 Public but present in GLO-90 is taken from GLO-90 (recorded); a tile in neither is sea (0 m).
    At most `max_tiles` tiles; a coarse target (90 m or more) reads GLO-90 directly."""
    import rasterio
    from rasterio.merge import merge
    tiles = cop_tiles_for(bounds)
    if len(tiles) > COP_COG_MAX_TILES:
        raise oh.ServiceError(f"the window needs {len(tiles)} Copernicus tiles; the limit is {COP_COG_MAX_TILES} a map", "MAP-TER-003")
    h = http or safe_http(max_bytes=COP_TILE_BYTES)
    first = 90 if target_m_per_px >= 90 else 30
    lists = {g: cop_tile_list(g, root, h) for g in sorted({first, 90})}
    uncached = [x for x in tiles if not (root / "copernicus-tiles" / f"{cop_tile_name(x[0], x[1], first)}.tif").is_file()]
    est = len(uncached) * COP_TILE_MB[first]
    if len(tiles) > max_tiles or (est > COP_BYTE_BUDGET_MB and target_m_per_px >= COP_COG_MIN_M):
        return fetch_copernicus_cog(bounds, target_m_per_px, out, lists, first)
    paths, used, sea, fell_back, reqs = [], {}, [], [], []
    for la, lo in tiles:
        glo = None
        for g in ([first] if first == 90 else [30, 90]):
            if cop_tile_name(la, lo, g) in lists[g]:
                glo = g
                break
        if glo is None:
            sea.append(cop_tile_name(la, lo, first))
            continue
        if glo != first:
            fell_back.append(cop_tile_name(la, lo, glo))
        name = cop_tile_name(la, lo, glo)
        tp = root / "copernicus-tiles" / f"{name}.tif"
        if not tp.is_file():
            r = h.get(f"{COP_BUCKETS[glo]}{name}/{name}.tif", max_bytes=COP_TILE_BYTES)
            if r.status != 200:
                raise oh.ServiceError(f"Copernicus tile {name} answered HTTP {r.status}", "MAP-SVC-010")
            tp.parent.mkdir(parents=True, exist_ok=True)
            tmp = tp.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(tp)
            reqs.append({"url": r.url, "bytes": len(r.content), "sha256": r.sha256})
        paths.append(tp)
        used[name] = glo
    W, S, E, N = bounds
    lat_c = (S + N) / 2
    dy = target_m_per_px / 111_320.0
    dx = target_m_per_px / (111_320.0 * max(0.05, math.cos(math.radians(lat_c))))
    if paths:
        srcs = [rasterio.open(p) for p in paths]
        try:
            arr, t = merge(srcs, bounds=(W, S, E, N), res=(dx, dy), nodata=NODATA, resampling=rasterio.enums.Resampling.average)
        finally:
            for x in srcs:
                x.close()
        a = arr[0].astype("float32")
    else:
        from rasterio.transform import from_origin
        w_, h_ = max(1, int(round((E - W) / dx))), max(1, int(round((N - S) / dy)))
        a, t = np.full((h_, w_), NODATA, dtype="float32"), from_origin(W, N, dx, dy)
    a[a == NODATA] = 0.0 if sea else NODATA          # the readme: sea areas have no tiles; height zero there
    w = rio.write_geotiff(out, np.ma.masked_equal(a, NODATA), t, "EPSG:4326", nodata=NODATA)
    glo_used = sorted(set(used.values()))
    return {"source": "copernicus", "entry": COP_ENTRY, "crs": "EPSG:4326", "res_m": target_m_per_px, "tiles": len(paths), "tile_names": sorted(used),
            "glo": glo_used, "fallback_glo90": fell_back, "sea_tiles": sea, "surface": "DSM", "bytes": w["bytes"], "sha256": w["sha256"], "path": str(out),
            "requests": reqs, "pixels": [int(a.shape[1]), int(a.shape[0])], "nodata": NODATA,
            "attribution": "Elevation: " + "; ".join(COP_NOTICE[g] for g in (glo_used or [first])) + f". Copernicus DEM accessed {today()} from {COP_REGISTRY}.",
            "license": "attribution", "terms_url": "https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM"}


def cog_open(url: str):
    """Open a public COG by range reads (GDAL /vsicurl/). The address is built from this module's fixed bucket names only."""
    import rasterio
    return rasterio.open("/vsicurl/" + url)


def _gdal_http_env() -> dict:
    env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif", "GDAL_HTTP_USERAGENT": oh.default_user_agent(),
           "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "1", "VSI_CACHE": "TRUE"}
    ca = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca:
        env["CURL_CA_BUNDLE"] = ca
    return env


def fetch_copernicus_cog(bounds, target_m_per_px: float, out: Path, lists: dict, first: int) -> dict:
    """2.5.0 (T-04): a large window from the Copernicus COGs by HTTP range reads of the overview that matches the target cell size, not whole
    tiles: a state-sized window (60 one-degree tiles) at 250 m reads a few MB instead of 1.5 GB. GLO-90 for targets of 90 m and more."""
    import rasterio
    from rasterio.merge import merge
    W, S, E, N = bounds
    lat_c = (S + N) / 2
    dy = target_m_per_px / 111_320.0
    dx = target_m_per_px / (111_320.0 * max(0.05, math.cos(math.radians(lat_c))))
    names, sea, fell = [], [], []
    for la, lo in cop_tiles_for(bounds):
        glo = next((g for g in ([first] if first == 90 else [30, 90]) if cop_tile_name(la, lo, g) in lists.get(g, set())), None)
        if glo is None:
            sea.append(cop_tile_name(la, lo, first))
            continue
        if glo != first:
            fell.append(cop_tile_name(la, lo, glo))
        names.append((cop_tile_name(la, lo, glo), glo))
    with rasterio.Env(**_gdal_http_env()):
        srcs = [cog_open(f"{COP_BUCKETS[g]}{n}/{n}.tif") for n, g in names]
        try:
            if srcs:
                arr, tr = merge(srcs, bounds=(W, S, E, N), res=(dx, dy), nodata=NODATA, resampling=rasterio.enums.Resampling.average)
                a = arr[0].astype("float32")
            else:
                from rasterio.transform import from_origin
                w_, h_ = max(1, int(round((E - W) / dx))), max(1, int(round((N - S) / dy)))
                a, tr = np.full((h_, w_), NODATA, dtype="float32"), from_origin(W, N, dx, dy)
        finally:
            for x in srcs:
                x.close()
    a[a == NODATA] = 0.0 if sea else NODATA
    w = rio.write_geotiff(out, np.ma.masked_equal(a, NODATA), tr, "EPSG:4326", nodata=NODATA)
    glo_used = sorted({g for _n, g in names}) or [first]
    est = float(a.size) * 4 / 1e6 * 1.5
    return {"source": "copernicus", "entry": COP_ENTRY, "crs": "EPSG:4326", "res_m": target_m_per_px, "tiles": len(names), "tile_names": sorted(n for n, _ in names),
            "glo": glo_used, "fallback_glo90": fell, "sea_tiles": sea, "surface": "DSM", "bytes": w["bytes"], "sha256": w["sha256"], "path": str(out),
            "requests": [{"url": f"{COP_BUCKETS[g]}{n}/{n}.tif", "bytes": 0, "range_reads": True} for n, g in names], "pixels": [int(a.shape[1]), int(a.shape[0])],
            "nodata": NODATA, "route": "cog-overviews", "estimated_mb": est,
            "attribution": "Elevation: " + "; ".join(COP_NOTICE[g] for g in glo_used) + f". Copernicus DEM accessed {today()} from {COP_REGISTRY}.",
            "license": "attribution", "terms_url": "https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM"}


def public_record(rec: dict) -> dict:
    """The record without the request list (for a map's provenance: the counts stay)."""
    r = dict(rec or {})
    reqs = r.pop("requests", []) or []
    r["request_count"] = len(reqs)
    r["request_bytes"] = int(sum(int(q.get("bytes", 0)) for q in reqs))
    return r
