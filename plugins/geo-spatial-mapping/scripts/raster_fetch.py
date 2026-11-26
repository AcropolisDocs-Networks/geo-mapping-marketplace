#!/usr/bin/env python3
"""raster_fetch: the population rasters a map or an op names by library id, fetched for a window and cached (M12 integration).

    import raster_fetch as rf
    r = rf.raster_for("ref.statistic.world.population.ghsl", (-112.3, 33.3, -111.8, 33.7))
    r["path"]      # a local GeoTIFF (the GHSL tile, or a mosaic of up to four tiles; or the WorldPop country file)
    r["record"]    # entry, urls, bytes, sha256, retrieved_at, cached, attribution (CC BY 4.0), the tiles or the country

Two entries are known (their download patterns are in the library entries):
  ref.statistic.world.population.ghsl       GHS-POP R2023A epoch 2020, 100 m, Mollweide (ESRI:54009), one zip per 1,000 km tile. The tile grid starts at
                                            x = -18,041,000 m, y = 9,000,000 m; row r and column c cover x0 + (c - 1) * 1e6 .. + 1e6, y0 - r * 1e6 .. + 1e6
                                            (checked against tile R5_C9 on 2026-11-05: -10,041,000 .. -9,041,000, 4,000,000 .. 5,000,000).
                                            A window over two to four tiles is merged into one GeoTIFF; more than four is refused (use a smaller window).
  ref.statistic.world.population.worldpop   WorldPop constrained 2020, 100 m, EPSG:4326, one file per country (ISO3 from the bundled Natural Earth countries
                                            at the window's centre). A window over two countries takes the centre's country and says so (GHSL is the one grid).
  ref.statistic.world.population.hrsl       3.1.0: Meta's High Resolution Settlement Layer (Data for Good), about 30 m (1 arc second), EPSG:4326: one VRT
                                            of Cloud-Optimized GeoTIFFs on AWS, so only the window is read, by GDAL range reads. People a cell, 2020
                                            estimates on buildings seen from satellite imagery. At most HRSL_MAX_PX pixels a side (about 1.1 degrees);
                                            a larger window is refused with GHSL (100 m) named. CC BY 4.0.

Rules: every request goes through ogc_http.SafeHTTP (public hosts, GET, the plugin's User-Agent, at most 200 MB a file: the client's ceiling, so the
WorldPop file for the United States, 494 MB, is not fetched whole: since 1.11.0 the map's window is read from it by HTTP byte range, through
the same client, and kept as a small GeoTIFF; a server that sends no ranges gets the old refusal with the GHSL alternative named). Files are kept under `<workdir>/fetch/rasters/<entry>/` with a
JSON record beside each; a second call for the same tile or country reads nothing from the network. GMAP_OFFLINE=1 refuses a fetch that is not cached.

5.2.0 (open data portals P4.2): any library raster entry whose `delivery.cog` block names a Cloud-Optimized GeoTIFF is read the same way, with no
code of its own. The block is data:

    delivery:
      cog:
        url: https://host/file.tif                 one file, or
        url_template: https://host/..._{tile}_Map.tif
        tiles: {size_deg: 3, corner: lower-left, name: "{NS}{lat:02d}{EW}{lon:03d}"}     the tile grid and how a tile is named by its corner
        max_tiles: 4                                a window over more tiles is refused
        max_px: 3000                                a window wider than this is read from the file's overviews (decimated), never at full size
        resampling: nearest                         nearest for classes, average for a measure

The window is read by HTTP byte range through SafeHTTP (the same client and ceilings as above), from as many tiles as it touches, and kept as one
GeoTIFF with a record naming every tile and the bytes each cost. A tile the publisher does not hold (open sea) is passed over and said.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import raster_io as rio  # noqa: E402

GHSL = "ref.statistic.world.population.ghsl"
WORLDPOP = "ref.statistic.world.population.worldpop"
HRSL = "ref.statistic.world.population.hrsl"
HRSL_VRT = "https://dataforgood-fb-data.s3.amazonaws.com/hrsl-cogs/hrsl_general/hrsl_general-latest.vrt"
HRSL_MAX_PX = 4096
ENTRIES = (GHSL, WORLDPOP, HRSL)
GHSL_X0, GHSL_Y0, GHSL_TILE_M = -18_041_000.0, 9_000_000.0, 1_000_000.0
GHSL_MAX_TILES = 4
MAX_BYTES = 200_000_000                  # SafeHTTP's ceiling (CEILING["max_bytes"])


class RasterFetchError(rio.RasterError):
    """A refusal or failure with a message safe to show."""


def offline() -> bool:
    return os.environ.get("GMAP_OFFLINE") == "1"


def today() -> str:
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


def cache_root(cache_dir=None) -> Path:
    return (Path(cache_dir) if cache_dir else gc.workdir(create=True)) / "fetch" / "rasters"


def library_entry(entry_id: str) -> dict:
    import ref_library as rl
    for e in rl.all_entries("all"):
        if e.get("id") == entry_id:
            return e
    raise RasterFetchError(f"the reference library has no entry '{entry_id}'")


def cog_block(entry) -> dict | None:
    """The entry's `delivery.cog` block (5.2.0), or None. `entry` is an id or the entry itself."""
    if isinstance(entry, str):
        try:
            entry = library_entry(entry)
        except RasterFetchError:
            return None
    c = ((entry or {}).get("delivery") or {}).get("cog")
    return c if isinstance(c, dict) and (c.get("url") or c.get("url_template")) else None


def file_block(entry) -> dict | None:
    """The entry's `delivery.raster` block (5.2.0): one GeoTIFF fetched whole, plain or inside a zip (`url`, `zipped`), small enough to keep
    (200 MB at most). `files: {name: url}` names several, chosen by source.variant; the first is the default."""
    if isinstance(entry, str):
        try:
            entry = library_entry(entry)
        except RasterFetchError:
            return None
    r = ((entry or {}).get("delivery") or {}).get("raster")
    return r if isinstance(r, dict) and (r.get("url") or r.get("files")) else None


def known(entry_id: str) -> bool:
    return entry_id in ENTRIES or cog_block(entry_id) is not None or file_block(entry_id) is not None


# --------------------------------------------------------------------------------------------------------------- COG tiles (5.2.0)
COG_MAX_TILES = 4
COG_MAX_PX = 3000


def cog_tile_name(pattern: str, lon: float, lat: float) -> str:
    """A tile's name from its corner: {NS} {EW} {ns} {ew} are the hemisphere letters, {lat} and {lon} the whole degrees without sign
    (with a format: {lat:02d}), {slat} and {slon} the signed degrees."""
    la, lo = int(round(lat)), int(round(lon))
    return pattern.format(NS="N" if la >= 0 else "S", EW="E" if lo >= 0 else "W", ns="n" if la >= 0 else "s", ew="e" if lo >= 0 else "w",
                          lat=abs(la), lon=abs(lo), slat=la, slon=lo)


def cog_variant(block: dict, variant: str | None) -> str | None:
    """The product of the entry to read (delivery.cog.variants lists them; the first is the default), or None when the entry has one."""
    vs = [str(v) for v in (block.get("variants") or [])]
    if not vs:
        return None
    v = str(variant or block.get("default_variant") or vs[0])
    if v not in vs:
        raise RasterFetchError(f"this library raster has the products {', '.join(vs)}; '{v}' is not one (source.variant)")
    return v


def cog_tiles(block: dict, bounds_wgs84, variant: str | None = None) -> list[tuple[str, str]]:
    """[(tile name, url)] of the files a lon/lat window touches: the one file, or every tile of the grid under the window."""
    import math
    v = cog_variant(block, variant)
    if block.get("url"):
        u = str(block["url"]).replace("{variant}", v or "")
        return [(Path(u).stem, u)]
    g = block.get("tiles") or {}
    size = float(g.get("size_deg") or 0)
    pattern = str(g.get("name") or "")
    tpl = str(block.get("url_template") or "").replace("{variant}", v or "")
    if size <= 0 or not pattern or "{tile}" not in tpl:
        raise RasterFetchError("this library raster's delivery.cog names a url_template but no tile grid (tiles: {size_deg, name}) or no {tile} in the address")
    upper = str(g.get("corner") or "lower-left").lower().startswith("upper")
    x0, y0, x1, y1 = (float(v) for v in bounds_wgs84)
    eps = 1e-9
    out = []
    for i in range(math.floor(x0 / size), math.floor((x1 - eps) / size) + 1):
        for j in range(math.floor(y0 / size), math.floor((y1 - eps) / size) + 1):
            lon, lat = i * size, (j + 1) * size if upper else j * size
            name = cog_tile_name(pattern, lon, lat)
            out.append((name, tpl.replace("{tile}", name)))
    return out


# --------------------------------------------------------------------------------------------------------------- GHSL tiles
def ghsl_tiles(bounds_wgs84) -> list[tuple[int, int]]:
    """(row, col) of the 1,000 km Mollweide tiles a lon/lat window touches (its corners and edge midpoints, projected)."""
    from pyproj import Transformer
    x0, y0, x1, y1 = (float(v) for v in bounds_wgs84)
    t = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    lons = [x0, (x0 + x1) / 2, x1]
    lats = [y0, (y0 + y1) / 2, y1]
    out = set()
    for lo in lons:
        for la in lats:
            x, y = t.transform(lo, la)
            out.add((int((GHSL_Y0 - y) // GHSL_TILE_M) + 1, int((x - GHSL_X0) // GHSL_TILE_M) + 1))
    for r in range(min(o[0] for o in out), max(o[0] for o in out) + 1):           # every tile between the extremes
        for c in range(min(o[1] for o in out), max(o[1] for o in out) + 1):
            out.add((r, c))
    return sorted(out)


def ghsl_url(entry: dict, row: int, col: int) -> str:
    tpl = (entry.get("delivery") or {}).get("url_template") or ""
    if "{row}" not in tpl or "{col}" not in tpl:
        raise RasterFetchError(f"library entry {entry.get('id')} has no tile url_template naming the row and the column")
    return tpl.replace("{row}", str(int(row))).replace("{col}", str(int(col)))


# --------------------------------------------------------------------------------------------------------------- WorldPop country
def worldpop_iso3(bounds_wgs84) -> tuple[str, list[str]]:
    """(ISO3 of the country at the window's centre, every country the window touches) from the bundled Natural Earth countries."""
    import geopandas as gpd
    from shapely.geometry import Point, box
    g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
    g = g.to_crs("EPSG:4326") if g.crs is not None and not g.crs.equals("EPSG:4326") else g
    col = next((c for c in ("ADM0_A3", "ISO_A3", "SOV_A3") if c in g.columns), None)
    if col is None:
        raise RasterFetchError("the bundled countries layer has no ISO3 column")
    x0, y0, x1, y1 = bounds_wgs84
    c = Point((x0 + x1) / 2, (y0 + y1) / 2)
    hit = g[g.geometry.contains(c)]
    if not len(hit):
        hit = g.iloc[[int(g.geometry.distance(c).values.argmin())]]
    touch = sorted(set(str(v) for v in g[g.geometry.intersects(box(x0, y0, x1, y1))][col]))
    return str(hit[col].iloc[0]), touch


def worldpop_url(entry: dict, iso3: str) -> str:
    tpl = (entry.get("delivery") or {}).get("url_template") or ""
    if "{ISO3}" not in tpl:
        raise RasterFetchError(f"library entry {entry.get('id')} has no url_template naming the country code")
    return tpl.replace("{ISO3}", iso3.upper()).replace("{iso3}", iso3.lower())


# --------------------------------------------------------------------------------------------------------------- download
def _http(http=None):
    return http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": MAX_BYTES})


def _download(url: str, dest: Path, http, *, zipped: bool) -> dict:
    """GET url (through SafeHTTP) into dest; a zip is opened and its one GeoTIFF kept. -> {url, bytes, sha256, member?}."""
    if offline():
        raise RasterFetchError(f"offline (GMAP_OFFLINE=1): {dest.name} is not in the cache and nothing is fetched")
    try:
        r = http.get(url, None, expect="any", max_bytes=MAX_BYTES)
    except oh.ServiceError as e:
        msg = oh.untrusted(e, 200)
        if "larger than" in msg:
            raise RasterFetchError(f"{Path(url).name} is larger than the {MAX_BYTES // 1_000_000} MB a fetch may take; use the GHSL tiles "
                                   f"({GHSL}) for a large country, or download the file and give its path") from None
        raise RasterFetchError(f"the raster could not be fetched: {msg}") from None
    body = r.content
    dest.parent.mkdir(parents=True, exist_ok=True)
    rec = {"url": oh.redact_url(r.url), "bytes": len(body), "sha256_download": r.sha256}
    if zipped:
        if body[:4] != b"PK\x03\x04":
            raise RasterFetchError(f"{Path(url).name} is not a zip file (the server sent {oh.untrusted(r.content_type, 40) or 'something else'})")
        z = zipfile.ZipFile(io.BytesIO(body))
        tifs = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff")) and not n.startswith(("/", "..")) and ".." not in n]
        if not tifs:
            raise RasterFetchError(f"{Path(url).name} holds no GeoTIFF")
        with z.open(tifs[0]) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        rec["member"] = tifs[0]
    else:
        if body[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            raise RasterFetchError(f"{Path(url).name} is not a GeoTIFF")
        dest.write_bytes(body)
    rec["sha256"] = _sha(dest)
    return rec


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached(dest: Path) -> dict | None:
    rp = dest.with_suffix(".json")
    if dest.is_file() and rp.is_file():
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if rec.get("sha256") == _sha(dest):
            return rec
    return None


def _one(url: str, dest: Path, http, zipped: bool) -> tuple[dict, bool]:
    rec = _cached(dest)
    if rec:
        return rec, True
    rec = _download(url, dest, http, zipped=zipped)
    rec["retrieved_at"] = today()
    dest.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec, False


# --------------------------------------------------------------------------------------------------------------- a window of a large file
RANGE_BLOCK = 1 << 20                    # byte ranges are asked for in whole 1 MB blocks, neighbours in one request
RANGE_MAX_REQUESTS = 400                 # a window that needs more requests than this is refused (the file is not laid out for ranged reads)
WINDOW_PAD_DEG = 0.05                    # the window kept is the one asked for plus this margin, so a small move of the map reads the cache


class RangedFile:
    """A read-only file whose bytes are fetched as HTTP byte ranges through SafeHTTP (public hosts, GET, the plugin's User-Agent), in whole
    blocks kept in memory. GDAL reads the GeoTIFF's directory and only the strips or tiles of the window through it, so a 500 MB country file
    costs the megabytes the window needs. The total fetched is held under MAX_BYTES and RANGE_MAX_REQUESTS."""

    def __init__(self, url: str, size: int, http):
        self.url, self.size, self.http = url, int(size), http
        self.pos = 0
        self.blocks: dict[int, bytes] = {}
        self.requests = 0
        self.fetched = 0
        self.final_url = url

    def _fetch(self, b0: int, b1: int) -> None:
        if self.requests >= RANGE_MAX_REQUESTS or self.fetched > MAX_BYTES:
            raise RasterFetchError(f"reading this window of {Path(self.url).name} needs more than {RANGE_MAX_REQUESTS} requests or {MAX_BYTES // 1_000_000} MB; "
                                   f"use a smaller window or the GHSL tiles ({GHSL})")
        lo, hi = b0 * RANGE_BLOCK, min(self.size, (b1 + 1) * RANGE_BLOCK) - 1
        try:
            r = self.http.get(self.url, None, expect="any", headers={"Range": f"bytes={lo}-{hi}"}, max_bytes=hi - lo + 1 + 1024)
        except oh.ServiceError as e:
            raise RasterFetchError(f"a part of the raster could not be fetched: {oh.untrusted(e, 200)}") from None
        if r.status != 206 or len(r.content) != hi - lo + 1:
            raise RasterFetchError(f"the server did not send the byte range asked for (HTTP {r.status}); {Path(self.url).name} cannot be read by window")
        self.requests += 1
        self.fetched += len(r.content)
        self.final_url = r.url
        for i, b in enumerate(range(b0, b1 + 1)):
            self.blocks[b] = r.content[i * RANGE_BLOCK:(i + 1) * RANGE_BLOCK]

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if not n:
            return b""
        b0, b1 = self.pos // RANGE_BLOCK, (self.pos + n - 1) // RANGE_BLOCK
        need = [b for b in range(b0, b1 + 1) if b not in self.blocks]
        while need:                                                   # neighbouring missing blocks go in one request
            a = z = need[0]
            while z + 1 in need:
                z += 1
            self._fetch(a, z)
            need = [b for b in need if b > z]
        out = b"".join(self.blocks[b] for b in range(b0, b1 + 1))
        off = self.pos - b0 * RANGE_BLOCK
        self.pos += n
        return out[off:off + n]

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def tell(self) -> int:
        return self.pos

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def view(self) -> "RangedFile":
        """Another handle on the same file: its own position, the same blocks and counters (GDAL opens a file more than once)."""
        v = _RangedView.__new__(_RangedView)
        v.parent, v.pos, v.size, v.url = self, 0, self.size, self.url
        return v

    def opener(self, name: str):
        """A rasterio opener for the one file: sidecar names GDAL probes for (.aux.xml, .msk, .ovr) are not there."""
        def open_(path, mode="rb", **kw):
            if Path(str(path)).name != name or "r" not in mode:
                raise FileNotFoundError(str(path))
            return self.view()
        return open_


class _RangedView(RangedFile):
    def read(self, n: int = -1) -> bytes:
        self.parent.pos = self.pos
        out = RangedFile.read(self.parent, n)
        self.pos = self.parent.pos
        return out


def remote_size(url: str, http) -> int | None:
    """The file's size, when the server serves byte ranges (asked with a one-byte range: the answer's Content-Range carries the total)."""
    if offline():
        return None
    try:
        r = http.get(url, None, expect="any", headers={"Range": "bytes=0-0"}, max_bytes=4096)
    except oh.ServiceError:
        return None
    cr = str(r.headers.get("content-range") or "")
    if r.status != 206 or "/" not in cr:
        return None
    try:
        return int(cr.rsplit("/", 1)[1])
    except ValueError:
        return None


def _window_cached(root: Path, stem: str, bounds) -> tuple[Path, dict] | None:
    for rp in sorted(root.glob(f"{stem}_window_*.json")):
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        w = rec.get("window_wgs84") or []
        tif = rp.with_suffix(".tif")
        if len(w) == 4 and tif.is_file() and w[0] <= bounds[0] and w[1] <= bounds[1] and w[2] >= bounds[2] and w[3] >= bounds[3] and rec.get("sha256") == _sha(tif):
            return tif, rec
    return None


def window_download(url: str, size: int, bounds_wgs84, root: Path, http, *, max_px: int | None = None, full_bounds=None, resampling: str = "nearest",
                    pad: float | None = None) -> tuple[Path, dict]:
    """The window of a remote GeoTIFF as a local GeoTIFF: the file's own CRS, nodata and compression kept. -> (path, record).
    Full resolution, unless `max_px` is given (5.2.0): then a window wider than max_px pixels (measured over `full_bounds`, the whole window when
    this file is one tile of several) is read decimated, which GDAL serves from the file's overviews, so a country costs what a city does."""
    rasterio = rio._rasterio()
    from rasterio.windows import from_bounds as window_from_bounds
    if offline():
        raise RasterFetchError(f"offline (GMAP_OFFLINE=1): this window of {Path(url).name} is not in the cache and nothing is fetched")
    pad = WINDOW_PAD_DEG if pad is None else float(pad)
    b = (float(bounds_wgs84[0]) - pad, float(bounds_wgs84[1]) - pad, float(bounds_wgs84[2]) + pad, float(bounds_wgs84[3]) + pad)
    rf_ = RangedFile(url, size, http)
    factor = 1
    stem = Path(url).stem
    try:
        with rasterio.open(Path(url).name, opener=rf_.opener(Path(url).name)) as src:
            if src.crs is None:
                raise RasterFetchError(f"{Path(url).name} has no CRS; it cannot be read by window here")
            degrees = src.crs.to_epsg() == 4326
            if not degrees and max_px is None:
                raise RasterFetchError(f"{Path(url).name} is not in EPSG:4326; it cannot be read by window here")
            import math
            from rasterio.windows import Window
            from rasterio.warp import transform_bounds
            to_src = (lambda q: q) if degrees else (lambda q: transform_bounds("EPSG:4326", src.crs, *q, densify_pts=21))
            sb = src.bounds
            q = to_src(b)
            bb = (max(q[0], sb.left), max(q[1], sb.bottom), min(q[2], sb.right), min(q[3], sb.top))
            if bb[0] >= bb[2] or bb[1] >= bb[3]:
                raise RasterFetchError(f"the window is outside {Path(url).name}")
            w0 = window_from_bounds(*bb, transform=src.transform)
            c0, r0 = max(0, math.floor(w0.col_off)), max(0, math.floor(w0.row_off))
            c1, r1 = min(src.width, math.ceil(w0.col_off + w0.width)), min(src.height, math.ceil(w0.row_off + w0.height))
            win = Window(c0, r0, c1 - c0, r1 - r0)
            if max_px:
                fq = to_src(tuple(float(v) for v in full_bounds)) if full_bounds else bb
                span = max((fq[2] - fq[0]) / abs(src.res[0]), (fq[3] - fq[1]) / abs(src.res[1]))
                factor = max(1, math.ceil(span / float(max_px)))
            if factor > 1:
                from rasterio.enums import Resampling
                oh_, ow_ = max(1, math.ceil(win.height / factor)), max(1, math.ceil(win.width / factor))
                data = src.read(window=win, out_shape=(src.count, oh_, ow_), resampling=getattr(Resampling, resampling, Resampling.nearest))
                base, sc = src.window_transform(win), rasterio.Affine.scale(win.width / ow_, win.height / oh_)
                tr = base.__matmul__(sc) if hasattr(base, "__matmul__") else base * sc
            else:
                data = src.read(window=win)
                tr = src.window_transform(win)
            prof = src.profile.copy()
            prof.update(driver="GTiff", height=data.shape[1], width=data.shape[2], transform=tr, compress="deflate", tiled=True,
                        blockxsize=256, blockysize=256)
            prof.pop("photometric", None)
            wb = rasterio.windows.bounds(win, src.transform)
            if not degrees:                                           # the record's window is what was asked for (the read covers it), never a larger guess
                fb = transform_bounds(src.crs, "EPSG:4326", *sb, densify_pts=21)
                wb = (max(b[0], fb[0]), max(b[1], fb[1]), min(b[2], fb[2]), min(b[3], fb[3]))
            cmap = None
            try:
                cmap = src.colormap(1)
            except ValueError:
                pass
            layout = "tiled" if src.profile.get("tiled") else "strips"
            overviews = list(src.overviews(1))
    except RasterFetchError:
        raise
    except Exception as ex:  # noqa: BLE001 - GDAL's message through the opener; the reason is passed on
        raise RasterFetchError(f"this window of {Path(url).name} could not be read by byte range ({type(ex).__name__}: {str(ex)[:160]})") from None
    key = hashlib.sha256(json.dumps([round(v, 5) for v in wb] + ([factor] if factor > 1 else [])).encode()).hexdigest()[:12]
    dest = root / f"{stem}_window_{key}.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest, "w", **prof) as dst:
        dst.write(data)
        if cmap and prof.get("dtype") == "uint8" and prof.get("count") == 1:
            dst.write_colormap(1, cmap)
    rec = {"url": oh.redact_url(rf_.final_url), "bytes": rf_.fetched, "file_bytes": int(size), "requests": rf_.requests, "ranged": True, "layout": layout,
           "window_wgs84": [float(v) for v in wb], "pixels": [int(data.shape[2]), int(data.shape[1])], "sha256": _sha(dest), "retrieved_at": today()}
    if max_px:
        rec.update({"factor": int(factor), "overviews": overviews, "max_px": int(max_px)})
    dest.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return dest, rec


# --------------------------------------------------------------------------------------------------------------- the one call
def raster_for(entry_id: str, bounds_wgs84, *, cache_dir=None, http=None, variant: str | None = None) -> dict:
    """A local GeoTIFF of the library raster covering the window. -> {path, record, findings, entry}."""
    block = None if entry_id in ENTRIES else cog_block(entry_id)
    whole = None if (entry_id in ENTRIES or block is not None) else file_block(entry_id)
    if entry_id not in ENTRIES and block is None and whole is None:
        raise RasterFetchError(f"'{entry_id}' is not a raster this fetcher knows ({', '.join(ENTRIES)}, or a library raster with a delivery.cog block); give a file path instead")
    e = library_entry(entry_id)
    if e.get("status") in ("blocked", "retired", "draft"):
        raise RasterFetchError(f"library entry '{entry_id}' is {e.get('status')}")
    root = cache_root(cache_dir) / entry_id
    h = _http(http)
    lic = e.get("license") or {}
    findings: list[dict] = []
    fetched_from_cache = []
    if whole is not None:                                  # 5.2.0: one small GeoTIFF kept whole (a world raster at a coarse grain)
        files = {str(k): str(v) for k, v in (whole.get("files") or {}).items()}
        name = str(variant or whole.get("default") or (next(iter(files)) if files else ""))
        if files and name not in files:
            raise RasterFetchError(f"'{entry_id}' has the files {', '.join(files)}; '{name}' is not one (source.variant)")
        url = files[name] if files else str(whole["url"])
        zipped = bool(whole.get("zipped", url.lower().split("?")[0].endswith(".zip")))
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", name or Path(url.split("?")[0]).stem)
        path = root / f"{stem}.tif"
        r, cached = _one(url, path, h, zipped=zipped)
        fetched_from_cache.append(cached)
        rec = {"entry": entry_id, "files": [{**r, "cached": cached}]}
        if name:
            rec["variant"] = name
    elif block is not None:
        path, rec, findings, cached = cog_window(entry_id, block, bounds_wgs84, root, h, variant)
        fetched_from_cache.append(cached)
    elif entry_id == HRSL:
        path, r, cached = hrsl_window(bounds_wgs84, root)
        fetched_from_cache.append(cached)
        rec = {"entry": entry_id, "files": [{**r, "cached": cached}]}
    elif entry_id == GHSL:
        tiles = ghsl_tiles(bounds_wgs84)
        if len(tiles) > GHSL_MAX_TILES:
            raise RasterFetchError(f"the window touches {len(tiles)} GHSL tiles (1,000 km each); at most {GHSL_MAX_TILES} are fetched for one map. Use a smaller window")
        recs, paths = [], []
        for row, col in tiles:
            dest = root / f"R{row}_C{col}.tif"
            try:
                r, cached = _one(ghsl_url(e, row, col), dest, h, zipped=True)
            except RasterFetchError as ex:
                if len(tiles) > 1 and "404" in str(ex):                     # an all-sea tile is not published; its neighbours carry the land
                    findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": f"R{row}_C{col}",
                                     "message": f"GHSL tile R{row}_C{col} is not published (open sea); the window is read from the other tile(s)"})
                    continue
                raise
            recs.append({"tile": f"R{row}_C{col}", **r, "cached": cached})
            paths.append(dest)
            fetched_from_cache.append(cached)
        if not paths:
            raise RasterFetchError("no GHSL tile covers this window")
        tiles = [(int(x["tile"][1:].split("_C")[0]), int(x["tile"].split("_C")[1])) for x in recs]
        if len(paths) == 1:
            path = paths[0]
        else:
            path = root / ("mosaic_" + "_".join(f"R{r}C{c}" for r, c in tiles) + ".tif")
            if not path.is_file():
                _mosaic(paths, path)
        rec = {"entry": entry_id, "tiles": [x["tile"] for x in recs], "files": recs}
    else:
        iso3, touch = worldpop_iso3(bounds_wgs84)
        if len(touch) > 1:
            findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": touch,
                             "message": f"the window touches {', '.join(touch)}; WorldPop is one file per country, so {iso3} (the country at the window's centre) is used. "
                                        f"GHSL ({GHSL}) is one grid across borders"})
        url = worldpop_url(e, iso3)
        if variant == "unadj" or variant == "UNadj":
            url = url.replace("_ppp_2020_constrained", "_ppp_2020_UNadj_constrained")
        path = root / Path(url).name
        win = None if _cached(path) else _window_cached(root, Path(url).stem, bounds_wgs84)
        size = None if (win or _cached(path)) else remote_size(url, h)
        if win:                                            # 1.11.0: a window of a large country file, read by byte range earlier
            path, r, cached = win[0], win[1], True
        elif size is not None and size > MAX_BYTES:
            path, r = window_download(url, size, bounds_wgs84, root, h)
            cached = False
            findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": {"file_bytes": size, "fetched_bytes": r["bytes"], "requests": r["requests"]},
                             "message": f"the WorldPop file for {iso3} is {size / 1e6:,.0f} MB, more than a fetch may take whole; the map's window was read from it by "
                                        f"byte range ({r['bytes'] / 1e6:,.1f} MB in {r['requests']} request(s))"})
        else:
            r, cached = _one(url, path, h, zipped=False)
        fetched_from_cache.append(cached)
        rec = {"entry": entry_id, "country": iso3, "files": [{**r, "cached": cached}]}
    rec.update({"retrieved_at": today() if not all(fetched_from_cache) else (rec["files"][0].get("retrieved_at") or today()),
                "cached": all(fetched_from_cache), "attribution": lic.get("attribution_text"), "license": lic.get("class"),
                "request_count": sum(0 if f.get("cached") else int(f.get("requests") or 1) for f in rec["files"]), "request_bytes": sum(0 if f.get("cached") else int(f.get("bytes", 0)) for f in rec["files"]),
                "sha256": _sha(path), "path": str(path), "bounds_wgs84": [float(v) for v in bounds_wgs84]})
    return {"path": str(path), "record": rec, "findings": findings, "entry": e}


def cog_window(entry_id: str, block: dict, bounds_wgs84, root: Path, http, variant: str | None = None) -> tuple[Path, dict, list, bool]:
    """5.2.0: the window of a library raster kept as Cloud-Optimized GeoTIFFs (one file, or a grid of tiles), read by byte range and kept as one
    GeoTIFF. -> (path, record, findings, cached)."""
    bounds = tuple(float(v) for v in bounds_wgs84)
    max_px = int(block.get("max_px") or COG_MAX_PX)
    max_tiles = int(block.get("max_tiles") or COG_MAX_TILES)
    resampling = str(block.get("resampling") or "nearest")
    findings: list[dict] = []
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    variant = cog_variant(block, variant)
    if variant:
        root = root / variant
    for rp in sorted(root.glob("cog_*.json")):                         # an earlier read that holds this window at no coarser a grain
        try:
            old = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        w = old.get("window_wgs84") or []
        tif = rp.with_suffix(".tif")
        if (len(w) == 4 and tif.is_file() and w[0] <= bounds[0] and w[1] <= bounds[1] and w[2] >= bounds[2] and w[3] >= bounds[3]
                and old.get("max_px") == max_px and (old.get("factor", 1) == 1 or max(w[2] - w[0], w[3] - w[1]) <= 1.5 * span) and old.get("sha256") == _sha(tif)):
            return tif, {**old, "files": [{**f, "cached": True} for f in old.get("files", [])]}, findings, True
    if offline():
        raise RasterFetchError(f"offline (GMAP_OFFLINE=1): this window of {entry_id} is not in the cache and nothing is fetched")
    tiles = cog_tiles(block, bounds, variant)
    if len(tiles) > max_tiles:
        g = block.get("tiles") or {}
        raise RasterFetchError(f"the window touches {len(tiles)} tiles of {entry_id} ({g.get('size_deg')} degrees each); at most {max_tiles} are read for one map. "
                               "Use a smaller window")
    pad = min(WINDOW_PAD_DEG, 0.05 * span)
    parts, recs = [], []
    for name, url in tiles:
        size = remote_size(url, http)
        if size is None:
            if len(tiles) > 1:                                          # a tile of open sea is not published; its neighbours carry the land
                findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": name,
                                 "message": f"tile {name} of {entry_id} is not published or does not serve byte ranges (open sea, most often); the window is read from the other tile(s)"})
                continue
            raise RasterFetchError(f"{Path(url).name} did not answer a byte-range request; this library raster cannot be read by window from here")
        try:
            p, r = window_download(url, size, bounds, root / "parts", http, max_px=max_px, full_bounds=bounds, resampling=resampling, pad=pad)
        except RasterFetchError as ex:
            if len(tiles) > 1 and "outside" in str(ex):
                continue
            raise
        parts.append(p)
        recs.append({"tile": name, **r, "cached": False})
    if not parts:
        raise RasterFetchError(f"no tile of {entry_id} covers this window")
    key = hashlib.sha256(json.dumps([[round(v, 5) for v in bounds], max_px, [x["tile"] for x in recs]]).encode()).hexdigest()[:12]
    out = root / f"cog_{key}.tif"
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        out.write_bytes(parts[0].read_bytes())
    else:
        _mosaic(parts, out)
    ws = [x["window_wgs84"] for x in recs]
    window = [min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws)]
    if len(recs) < len(tiles):
        window = [max(window[0], bounds[0]), max(window[1], bounds[1]), min(window[2], bounds[2]), min(window[3], bounds[3])]
    factor = max(int(x.get("factor") or 1) for x in recs)
    rec = {"entry": entry_id, "tiles": [x["tile"] for x in recs], "files": recs, "window_wgs84": window, "max_px": max_px, "factor": factor,
           "sha256": _sha(out), "retrieved_at": today()}
    if variant:
        rec["variant"] = variant
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    for p in parts:
        for q in (p, p.with_suffix(".json")):
            q.unlink(missing_ok=True)
    if factor > 1:
        findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": {"factor": factor, "max_px": max_px},
                         "message": f"the window is wider than {max_px} cells of {entry_id}, so one cell in every {factor} was read (from the file's overviews); "
                                    "a smaller window reads it at full grain"})
    return out, rec, findings, False


def hrsl_window(bounds_wgs84, root: Path, opener=None) -> tuple[Path, dict, bool]:
    """3.1.0: the window of Meta's HRSL (people a 1-arc-second cell) read from its public VRT of COGs, kept as a GeoTIFF. -> (path, record, cached)."""
    import math
    import numpy as np
    W, S, E, N = (float(v) for v in bounds_wgs84)
    W, S, E, N = (math.floor(W * 3600) / 3600, math.floor(S * 3600) / 3600, math.ceil(E * 3600) / 3600, math.ceil(N * 3600) / 3600)
    px_w, px_h = int(round((E - W) * 3600)), int(round((N - S) * 3600))
    if max(px_w, px_h) > HRSL_MAX_PX:
        raise RasterFetchError(f"the window is {px_w} x {px_h} HRSL cells (1 arc second); at most {HRSL_MAX_PX} a side are read for one map "
                               f"(about 1.1 degrees). Use a smaller window, or GHSL ({GHSL}, 100 m) for a region")
    stem = f"hrsl_{W:.4f}_{S:.4f}_{E:.4f}_{N:.4f}".replace("-", "m")
    out = root / f"{stem}.tif"
    hit = _cached(out)
    if hit:
        return out, hit, True
    if offline():
        raise RasterFetchError("offline (GMAP_OFFLINE=1): the HRSL window is not cached")
    import rasterio
    from rasterio.windows import from_bounds
    import terrain_fetch as tf
    op = opener or (lambda u: rasterio.open("/vsicurl/" + u))
    try:
        with rasterio.Env(**{**tf._gdal_http_env(), "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".vrt,.tif"}):
            with op(HRSL_VRT) as ds:
                win = from_bounds(W, S, E, N, ds.transform).round_offsets().round_lengths()
                a = ds.read(1, window=win, boundless=True, fill_value=np.nan).astype("float32")
                tr = ds.window_transform(win)
                nd = ds.nodata
    except rasterio.errors.RasterioIOError as ex:
        raise RasterFetchError(f"the HRSL layer could not be read ({str(ex)[:160]})") from None
    if nd is not None:
        a[a == nd] = np.nan
    w = rio.write_geotiff(out, np.ma.masked_invalid(a), tr, "EPSG:4326", nodata=-9999.0)
    rec = {"url": HRSL_VRT, "bytes": 0, "range_reads": True, "sha256": w["sha256"], "retrieved_at": today(), "pixels": [int(a.shape[1]), int(a.shape[0])],
           "people": float(np.nansum(a))}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return out, rec, False


def _mosaic(paths: list[Path], out: Path) -> None:
    import rasterio
    from rasterio.merge import merge
    srcs = [rasterio.open(p) for p in paths]
    try:
        arr, t = merge(srcs)
        prof = srcs[0].profile.copy()
        prof.update({"height": arr.shape[1], "width": arr.shape[2], "transform": t, "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER"})
        with rasterio.open(out, "w", **prof) as dst:
            dst.write(arr)
    finally:
        for s in srcs:
            s.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fetch a library raster (a population grid, or any entry kept as Cloud-Optimized GeoTIFFs) for a lon/lat window")
    ap.add_argument("entry", help="a library raster id: " + ", ".join(ENTRIES) + ", or any entry with a delivery.cog block")
    ap.add_argument("--bounds", required=True, help="lon0,lat0,lon1,lat1")
    ap.add_argument("--variant", help="the product of an entry that has several (delivery.cog.variants), or WorldPop's unadj")
    a = ap.parse_args(argv)
    try:
        r = raster_for(a.entry, tuple(float(v) for v in a.bounds.split(",")), variant=a.variant)
    except rio.RasterError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_RUNTIME
    print(json.dumps({"path": r["path"], "record": r["record"], "findings": r["findings"]}, indent=2, default=str))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
