#!/usr/bin/env python3
"""tiles_out: write layers as a vector tile set, PMTiles or MBTiles (M14, gap analysis P2-7), through GDAL's own PMTiles and MBTiles drivers.

    import tiles_out as to
    rep = to.write_pmtiles({"sites": gdf, "grid": gdf2}, "out.pmtiles", minzoom=0, maxzoom="auto", simplify=True, attribution="FCC")
    rep = to.write_mbtiles(... same ...)
    rep["findings"]        # MAP-OUT-009: the read-back (layers present, features at the top and bottom zoom)
    rep["command"]         # the ogr2ogr command that writes the same tile set

What it does:
  * one source layer per input layer (the MVT layer name is the input name, made safe: letters, digits, _ and -), each with its own zoom range
    (GDAL's CONF option: minzoom, maxzoom, description), in one file. The layers are reprojected to Web Mercator (EPSG:3857), the CRS the
    tiles use, after cutting them to the Web Mercator latitude limit (85.0511 degrees);
  * maxzoom='auto' picks each layer's top zoom from its own data (the table below); the file's maxzoom is the largest;
  * a dense layer's bottom zoom is raised until no tile holds more than 20,000 points (4,000 lines or polygons), because GDAL thins a tile
    over its 500 kB limit; a note says so;
  * simplify=True simplifies lines and polygons by one tile unit (1/4096 of a tile) at every zoom but the top one, which keeps full detail;
  * the metadata JSON carries the attribution, a description and the plugin and its version (`generator`);
  * a size cap (250 MB by default) and a tile-count cap (about 2 million tiles) refuse a tile set that would be too big, with what to change;
  * after writing, the file is opened again with pyogrio: every layer must be present, and every feature must be in the tiles at the layer's
    top zoom (MAP-OUT-009 BLOCK when one is missing; a WARN when features are missing at the bottom zoom, where tiny shapes can vanish).

Auto maxzoom. The rule is "the typical distance must stay visible at the top zoom": for points the median distance from a point to its nearest
neighbour must span 16 screen pixels (two symbols side by side); for lines and polygons the median vertex spacing must span 4 pixels (the
shape reads at full detail). Distances are measured in Web Mercator units, the units the tile pixels are in, so the rule holds at any
latitude. The top zoom is also never below the zoom at which the layer's whole extent spans 1,024 pixels (a tile unit is 1/4096 of a tile, so
positions then stay within a screen pixel for four zooms past the top one, and a coarse grid does not drift against finer layers). The
result is capped at zoom 15 and is never below the minzoom. In ground distance at the equator:

    top zoom   points: median spacing at least   lines and polygons: median vertex spacing at least
    6          39 km                             9.8 km
    8          9.8 km                            2.4 km
    10         2.4 km                            610 m
    12         610 m                             150 m
    14         150 m                             38 m
    15         below 150 m                       below 38 m

GDAL writes the tiles; tippecanoe is not used (it is faster on millions of features and thins dense points by zoom, and can be run by hand on
the same data when that matters). Reading a tile set back as data is not offered: a tile set holds clipped, generalised copies of the features,
so it is read only as a basemap (basemap-render).
"""
from __future__ import annotations

import ctypes
import gzip
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import style_dict as sd  # noqa: E402

WEB_MERCATOR = "EPSG:3857"
MERC_LAT = 85.0511287798
MERC_HALF = 20037508.342789244
PX0 = 2 * MERC_HALF / 256                 # metres (Web Mercator units) per screen pixel at zoom 0
MAX_AUTO_ZOOM = 15
MAX_ZOOM = 22                             # GDAL's limit
PX_POINTS, PX_SHAPES = 16, 4              # the auto-maxzoom rule (see the docstring table)
FIT_PX = 1024                             # ...and the top zoom is at least the one where the layer's extent spans this many pixels
TILE_FEATURES_POINTS, TILE_FEATURES_SHAPES = 20_000, 4_000
DEFAULT_MAX_BYTES = 250 * 1024 * 1024
MAX_TILE_ESTIMATE = 2_000_000
DRIVERS = {"pmtiles": "PMTiles", "mbtiles": "MBTiles"}


class TilesError(Exception):
    """A refusal or failure with a message that is safe to show the person."""


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- zooms
def tile_px(z: int) -> float:
    return PX0 / (2 ** z)


def zoom_for(distance: float, px: int) -> int:
    """The first zoom at which `distance` (Web Mercator units) spans `px` screen pixels."""
    if not distance or not math.isfinite(distance) or distance <= 0:
        return 14
    return max(0, int(math.ceil(math.log2(PX0 * px / distance) - 1e-9)))


def spacing(gdf3857, kind: str, sample: int = 20_000) -> float | None:
    """The distance the auto-maxzoom rule reads: the median nearest-neighbour distance of the points, or the median vertex spacing of the
    lines and polygons (Web Mercator units); None when it cannot be measured (one point, no segments)."""
    import numpy as np
    import shapely
    geoms = gdf3857.geometry.values
    geoms = geoms[~(shapely.is_missing(geoms) | shapely.is_empty(geoms))]
    if not len(geoms):
        return None
    if len(geoms) > sample:
        rng = np.random.default_rng(7)
        geoms = geoms[np.sort(rng.choice(len(geoms), sample, replace=False))]
    if kind == "point":
        xy = shapely.get_coordinates(geoms)
        xy = np.unique(np.round(xy, 3), axis=0)
        if len(xy) < 2:
            return None
        pts = shapely.points(xy)
        tree = shapely.STRtree(pts)
        _, d = tree.query_nearest(pts, exclusive=True, return_distance=True, all_matches=False)
        d = d[np.isfinite(d) & (d > 0)]
        return float(np.median(d)) if len(d) else None
    coords, idx = shapely.get_coordinates(shapely.boundary(geoms) if kind == "polygon" else geoms, return_index=True)
    if len(coords) < 2:
        return None
    seg = np.hypot(np.diff(coords[:, 0]), np.diff(coords[:, 1]))
    same = idx[1:] == idx[:-1]
    seg = seg[same & (seg > 0)]
    return float(np.median(seg)) if len(seg) else None


def fit_zoom(gdf3857) -> int | None:
    """The zoom at which the layer's whole extent (its longer side) spans FIT_PX screen pixels; None for a single point."""
    x0, y0, x1, y1 = gdf3857.total_bounds
    ext = max(x1 - x0, y1 - y0)
    if not math.isfinite(ext) or ext <= 0:
        return None
    return max(0, int(math.floor(math.log2(PX0 * FIT_PX / ext))))


def auto_maxzoom(gdf3857, kind: str, minzoom: int = 0) -> tuple[int, str]:
    """(zoom, why) by the table in the module docstring, and never below the zoom at which the whole layer fills a screen."""
    d = spacing(gdf3857, kind)
    px = PX_POINTS if kind == "point" else PX_SHAPES
    if d is None:
        z = 14 if kind == "point" else 12
        why = "one feature or no measurable spacing: the default top zoom"
    else:
        z = zoom_for(d, px)
        what = "median distance to the nearest point" if kind == "point" else "median vertex spacing"
        why = f"{what} {_dist(d)} (Web Mercator) spans {px} pixels at zoom {z}"
    zf = fit_zoom(gdf3857)
    if zf is not None and zf > z:
        why += f"; raised to {zf}, where the whole layer spans {FIT_PX} pixels, so positions stay within a pixel when the map zooms in"
        z = zf
    z2 = max(minzoom, min(MAX_AUTO_ZOOM, z))
    if z2 != z:
        why += f"; held to {z2}" + (" (the cap)" if z > MAX_AUTO_ZOOM else " (the minzoom)")
    return z2, why


def _dist(d: float) -> str:
    return f"{d / 1000:.1f} km" if d >= 1000 else f"{d:.0f} m"


def _tile_index(x, y, z: int):
    import numpy as np
    n = 2 ** z
    size = 2 * MERC_HALF / n
    tx = np.clip(np.floor((x + MERC_HALF) / size), 0, n - 1).astype("int64")
    ty = np.clip(np.floor((MERC_HALF - y) / size), 0, n - 1).astype("int64")
    return tx, ty


def dense_minzoom(gdf3857, kind: str, minzoom: int, maxzoom: int) -> tuple[int, int]:
    """The lowest zoom from minzoom up at which no tile holds more than the per-tile feature limit, and that tile count."""
    import numpy as np
    import shapely
    cap = TILE_FEATURES_POINTS if kind == "point" else TILE_FEATURES_SHAPES
    if len(gdf3857) <= cap:
        return minzoom, len(gdf3857)
    rp = shapely.point_on_surface(gdf3857.geometry.values) if kind != "point" else shapely.centroid(gdf3857.geometry.values)
    xy = shapely.get_coordinates(rp)
    worst = len(gdf3857)
    for z in range(minzoom, maxzoom + 1):
        tx, ty = _tile_index(xy[:, 0], xy[:, 1], z)
        _, counts = np.unique(tx * (2 ** z) + ty, return_counts=True)
        worst = int(counts.max()) if len(counts) else 0
        if worst <= cap:
            return z, worst
    return maxzoom, worst


def estimate_tiles(gdf3857, minzoom: int, maxzoom: int) -> int:
    """About how many tiles GDAL will cut: for every zoom, the tiles each feature's box touches (a feature can only be in those)."""
    import numpy as np
    b = gdf3857.geometry.bounds.to_numpy()
    b = b[np.isfinite(b).all(axis=1)]
    total = 0
    for z in range(minzoom, maxzoom + 1):
        x0, y1 = _tile_index(b[:, 0], b[:, 1], z)
        x1, y0 = _tile_index(b[:, 2], b[:, 3], z)
        total += int(((x1 - x0 + 1) * (y1 - y0 + 1)).sum())
        if total > MAX_TILE_ESTIMATE * 4:
            break
    return total


# ---------------------------------------------------------------------------------------------------------------- GDAL
_GDAL = None


def _gdal_candidates() -> list[str]:
    """Every libgdal that could be pyogrio's, best first: the ones loaded from pyogrio's own wheel folders, then any other libgdal in
    /proc/self/maps (rasterio and others bundle their own GDAL, often another version), then the wheel's folders on disk. gdal_lib() takes
    the first whose version matches pyogrio's; taking the first libgdal in the process picked rasterio's when a raster had been read first."""
    import glob
    import pyogrio
    import pyogrio._io  # noqa: F401 - loads libgdal
    base = Path(pyogrio.__file__).resolve().parent
    own_dirs = [str(base), str(base.parent / "pyogrio.libs")]
    loaded: list[str] = []
    maps = Path("/proc/self/maps")
    if maps.is_file():
        for line in maps.read_text(errors="ignore").splitlines():
            part = line.split()[-1] if line.split() else ""
            if "libgdal" in Path(part).name and part not in loaded:
                loaded.append(part)
    own = [x for x in loaded if any(x.startswith(d) for d in own_dirs)]
    other = [x for x in loaded if x not in own]
    pats = [base.parent / "pyogrio.libs" / "*gdal*", base / ".dylibs" / "libgdal*", base.parent / "pyogrio.libs" / "gdal*.dll",
            Path(sys.prefix) / "lib" / "libgdal.*", Path(sys.prefix) / "Library" / "bin" / "gdal*.dll"]
    on_disk = [h for pat in pats for h in sorted(glob.glob(str(pat)))]
    out: list[str] = []
    for x in own + other + on_disk:
        if x not in out:
            out.append(x)
    return out


def _locate_gdal() -> str | None:
    """The best candidate (kept for callers and tests); gdal_lib() checks the version of each in turn."""
    c = _gdal_candidates()
    return c[0] if c else None


def gdal_lib():
    """ctypes access to GDALVectorTranslate (ogr2ogr as a library) in pyogrio's own GDAL, or None when it cannot be found or is another
    version. The call keeps the GIL (PyDLL), so GDAL's messages reach pyogrio's handler as Python warnings."""
    global _GDAL
    if _GDAL is not None:
        return _GDAL or None
    _GDAL = False
    try:
        import pyogrio
        lib = None
        for path in _gdal_candidates():               # the first libgdal of pyogrio's own version (one process can hold several GDALs)
            try:
                cand = ctypes.PyDLL(path)
                cand.GDALVersionInfo.restype = ctypes.c_char_p
                cand.GDALVersionInfo.argtypes = [ctypes.c_char_p]
                if cand.GDALVersionInfo(b"RELEASE_NAME").decode() == pyogrio.__gdal_version_string__:
                    lib = cand
                    break
            except OSError:
                continue
        if lib is None:
            return None
        lib.GDALOpenEx.restype = ctypes.c_void_p
        lib.GDALOpenEx.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.GDALVectorTranslateOptionsNew.restype = ctypes.c_void_p
        lib.GDALVectorTranslateOptionsNew.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_void_p]
        lib.GDALVectorTranslateOptionsFree.argtypes = [ctypes.c_void_p]
        lib.GDALVectorTranslateOptionsFree.restype = None
        lib.GDALVectorTranslate.restype = ctypes.c_void_p
        lib.GDALVectorTranslate.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_int)]
        lib.GDALClose.argtypes = [ctypes.c_void_p]
        lib.GDALClose.restype = ctypes.c_int
        lib.CPLGetLastErrorMsg.restype = ctypes.c_char_p
        lib.CPLErrorReset.restype = None
        _GDAL = lib
        return lib
    except (OSError, AttributeError, ImportError):
        return None


def _translate(lib, src: Path, dst: Path, args: list[str]) -> None:
    lib.CPLErrorReset()
    h = lib.GDALOpenEx(str(src).encode(), 0x04, None, None, None)          # GDAL_OF_VECTOR
    if not h:
        raise TilesError("the scratch layers could not be opened for tiling")
    try:
        raw = [a.encode() for a in args]
        argv = (ctypes.c_char_p * (len(raw) + 1))(*raw, None)
        opts = lib.GDALVectorTranslateOptionsNew(argv, None)
        if not opts:
            raise TilesError("GDAL refused the tiling options: " + (lib.CPLGetLastErrorMsg() or b"").decode(errors="replace"))
        try:
            err = ctypes.c_int(0)
            srcs = (ctypes.c_void_p * 1)(h)
            out = lib.GDALVectorTranslate(str(dst).encode(), None, 1, srcs, opts, ctypes.byref(err))
            if not out:
                raise TilesError("GDAL could not write the tile set: " + (lib.CPLGetLastErrorMsg() or b"").decode(errors="replace"))
            if lib.GDALClose(out) != 0:                                    # the tiles are cut when the dataset closes
                raise TilesError("GDAL failed while cutting the tiles: " + (lib.CPLGetLastErrorMsg() or b"").decode(errors="replace"))
        finally:
            lib.GDALVectorTranslateOptionsFree(opts)
    finally:
        lib.GDALClose(h)


# ---------------------------------------------------------------------------------------------------------------- metadata
def _pmtiles_header(b: bytes) -> dict:
    if len(b) < 127 or b[:7] != b"PMTiles":
        raise TilesError("not a PMTiles file")
    if b[7] != 3:
        raise TilesError(f"PMTiles version {b[7]}; version 3 is expected")
    keys = ("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset", "leaf_length", "data_offset", "data_length",
            "addressed_tiles", "tile_entries", "tile_contents")
    h = dict(zip(keys, struct.unpack_from("<11Q", b, 8), strict=True))
    h.update(clustered=b[96], internal_compression=b[97], tile_compression=b[98], tile_type=b[99], min_zoom=b[100], max_zoom=b[101])
    h["bounds"] = [v / 1e7 for v in struct.unpack_from("<4i", b, 102)]
    h["center_zoom"] = b[118]
    return h


def pmtiles_metadata(path: str | Path) -> dict:
    """The JSON metadata of a PMTiles v3 file (read from its header; gzip and uncompressed are handled)."""
    with open(path, "rb") as f:
        head = f.read(127)
        h = _pmtiles_header(head)
        f.seek(h["metadata_offset"])
        raw = f.read(h["metadata_length"])
    if h["internal_compression"] == 2 or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif h["internal_compression"] not in (0, 1):
        raise TilesError("the PMTiles metadata uses a compression this reader does not know")
    return json.loads(raw.decode("utf-8")) if raw else {}


def _pmtiles_set_metadata(path: Path, extra: dict) -> None:
    """Add keys to a PMTiles v3 file's metadata JSON. The sections are written back in the usual order (header, root directory, metadata,
    leaf directories, tile data); directory offsets are relative to their own section, so only the header offsets change."""
    b = path.read_bytes()
    h = _pmtiles_header(b)
    meta = pmtiles_metadata(path)
    meta.update(extra)
    raw = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if h["internal_compression"] == 2:
        raw = gzip.compress(raw, mtime=0)
    root = b[h["root_offset"]:h["root_offset"] + h["root_length"]]
    leaf = b[h["leaf_offset"]:h["leaf_offset"] + h["leaf_length"]]
    data = b[h["data_offset"]:h["data_offset"] + h["data_length"]]
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(raw)
    data_off = leaf_off + len(leaf)
    head = bytearray(b[:127])
    struct.pack_into("<8Q", head, 8, root_off, len(root), meta_off, len(raw), leaf_off, len(leaf), data_off, len(data))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(bytes(head))
        f.write(root)
        f.write(raw)
        f.write(leaf)
        f.write(data)
    os.replace(tmp, path)


def mbtiles_metadata(path: str | Path) -> dict:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return {k: v for k, v in con.execute("SELECT name, value FROM metadata")}
    finally:
        con.close()


def _mbtiles_set_metadata(path: Path, extra: dict) -> None:
    con = sqlite3.connect(str(path))
    try:
        with con:
            for k, v in extra.items():
                con.execute("DELETE FROM metadata WHERE name = ?", (k,))
                con.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", (k, v if isinstance(v, str) else json.dumps(v)))
    finally:
        con.close()


def metadata(path: str | Path) -> dict:
    p = Path(path)
    return pmtiles_metadata(p) if p.suffix.lower() == ".pmtiles" else mbtiles_metadata(p)


# ---------------------------------------------------------------------------------------------------------------- write
def safe_layer_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_") or "layer"
    return s[:64]


def _prepare(layers: dict, notes: list) -> dict:
    """{safe name: (gdf in 3857, kind, n)}; empty and missing geometry dropped, cut to the Web Mercator latitude limit."""
    from shapely.geometry import box
    out: dict = {}
    if not layers:
        raise TilesError("no layers to tile")
    for name, gdf in layers.items():
        safe = safe_layer_name(name)
        if safe in out:
            raise TilesError(f"two layers become the tile layer name '{safe}'; rename one")
        if safe != name:
            notes.append(f"layer '{name}' is '{safe}' in the tiles (tile layer names keep to letters, digits, _ and -)")
        if gdf.crs is None:
            raise TilesError(f"layer '{name}' has no CRS; say which one it uses first")
        g = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)]
        if len(g) < len(gdf):
            notes.append(f"layer '{name}': {len(gdf) - len(g)} feature(s) with no geometry are not in the tiles")
        if not len(g):
            raise TilesError(f"layer '{name}' has no features with geometry to tile")
        w = g.to_crs("EPSG:4326")
        x0, y0, x1, y1 = w.total_bounds
        if y0 < -MERC_LAT or y1 > MERC_LAT:
            n0 = len(w)
            w = w.clip(box(-180, -MERC_LAT, 180, MERC_LAT))
            w = w[~w.geometry.is_empty]
            notes.append(f"layer '{name}' was cut to the Web Mercator limit (latitude {MERC_LAT:.4f}); {n0} features became {len(w)}")
        g3 = w.to_crs(WEB_MERCATOR)
        out[safe] = (g3.reset_index(drop=True), sd.geometry_kind(g3), len(g3))
    return out


def _fields_ok(gdf):
    """MVT keeps strings, numbers and booleans: other columns (dates, lists) become text."""
    import pandas as pd
    g = gdf.copy()
    for c in g.columns:
        if c == g.geometry.name:
            continue
        s = g[c]
        if pd.api.types.is_datetime64_any_dtype(s) or (s.dtype == object and s.map(lambda v: isinstance(v, (list, dict, tuple))).any()):
            g[c] = s.astype(str).where(s.notna(), None)
    return g


def ogr2ogr_command(src: str, dst: str, driver: str, minzoom: int, maxzoom: int, conf: dict, simplify: bool, name: str | None,
                    description: str | None) -> str:
    args = ["ogr2ogr", "-f", driver, "-t_srs", WEB_MERCATOR, "-preserve_fid", "-dsco", f"MINZOOM={minzoom}", "-dsco", f"MAXZOOM={maxzoom}"]
    if name:
        args += ["-dsco", f"NAME={name}"]
    if description:
        args += ["-dsco", f"DESCRIPTION={description}"]
    if simplify:
        args += ["-dsco", "SIMPLIFICATION=1", "-dsco", "SIMPLIFICATION_MAX_ZOOM=0"]
    args += ["-dsco", "CONF=" + json.dumps(conf, separators=(",", ":")), dst, src]
    return " ".join(shlex.quote(a) for a in args)


def write_pmtiles(layers: dict, out, minzoom: int = 0, maxzoom: int | str = "auto", simplify: bool = True, attribution: str | None = None, **kw) -> dict:
    return write_tiles("pmtiles", layers, out, minzoom=minzoom, maxzoom=maxzoom, simplify=simplify, attribution=attribution, **kw)


def write_mbtiles(layers: dict, out, minzoom: int = 0, maxzoom: int | str = "auto", simplify: bool = True, attribution: str | None = None, **kw) -> dict:
    return write_tiles("mbtiles", layers, out, minzoom=minzoom, maxzoom=maxzoom, simplify=simplify, attribution=attribution, **kw)


def write_tiles(fmt: str, layers: dict, out, *, minzoom: int = 0, maxzoom: int | str = "auto", simplify: bool = True, attribution: str | None = None,
                name: str | None = None, description: str | None = None, layer_zooms: dict | None = None, layer_descriptions: dict | None = None,
                max_bytes: int = DEFAULT_MAX_BYTES, overwrite: bool = True, check: bool = True, mvt_limits: dict | None = None,
                fixed_zooms: bool = False) -> dict:
    """Write `layers` ({name: GeoDataFrame}) as one vector tile set. Returns {'path', 'paths', 'format', 'layers': {name: {...}}, 'minzoom',
    'maxzoom', 'bytes', 'notes', 'findings', 'command'}. `layer_zooms` ({name: (min, max)}) overrides the zooms of single layers."""
    if fmt not in DRIVERS:
        raise TilesError(f"'{fmt}' is not a tile format; use pmtiles or mbtiles")
    driver = DRIVERS[fmt]
    outp = Path(out)
    if outp.exists() and not overwrite:
        raise TilesError(f"{outp.name} exists; choose another name or allow overwrite")
    try:
        minzoom = int(minzoom)
    except (TypeError, ValueError):
        raise TilesError(f"minzoom must be a whole number from 0 to {MAX_ZOOM}") from None
    if not 0 <= minzoom <= MAX_ZOOM:
        raise TilesError(f"minzoom must be from 0 to {MAX_ZOOM}")
    if maxzoom != "auto" and maxzoom is not None:
        try:
            maxzoom = int(maxzoom)
        except (TypeError, ValueError):
            raise TilesError(f"maxzoom must be 'auto' or a whole number from 0 to {MAX_ZOOM}") from None
        if not minzoom <= maxzoom <= MAX_ZOOM:
            raise TilesError(f"maxzoom must be from minzoom ({minzoom}) to {MAX_ZOOM}")
    notes: list[str] = []
    prep = _prepare(layers, notes)
    layer_zooms = {safe_layer_name(k): v for k, v in (layer_zooms or {}).items()}
    layer_descriptions = {safe_layer_name(k): v for k, v in (layer_descriptions or {}).items()}
    info: dict = {}
    conf: dict = {}
    est = 0
    for lname, (g3, kind, n) in prep.items():
        lo, hi = layer_zooms.get(lname, (minzoom, maxzoom))
        lo = int(lo)
        if hi in ("auto", None):
            hi, why = auto_maxzoom(g3, kind, lo)
            notes.append(f"layer '{lname}': top zoom {hi} ({why})")
        hi = int(hi)
        lo2, worst = (lo, 0) if fixed_zooms else dense_minzoom(g3, kind, lo, hi)     # 1.6.0: zooms set by a level-of-detail pyramid stay
        if lo2 > lo:
            notes.append(f"layer '{lname}' starts at zoom {lo2}, not {lo}: below it one tile would hold {worst:,}+ features and GDAL would thin it")
            lo = lo2
        est += estimate_tiles(g3, lo, hi)
        info[lname] = {"features": n, "kind": kind, "minzoom": lo, "maxzoom": hi}
        conf[lname] = {"minzoom": lo, "maxzoom": hi, "description": str(layer_descriptions.get(lname) or lname)}
    zmin = min(v["minzoom"] for v in info.values())
    zmax = max(v["maxzoom"] for v in info.values())
    if est > MAX_TILE_ESTIMATE:
        raise TilesError(f"the tile set would cut about {est:,} tiles (zoom {zmin} to {zmax}), over the limit of {MAX_TILE_ESTIMATE:,}. Lower the maximum zoom, "
                         "raise the minimum zoom or clip the data to the area needed; tippecanoe, run by hand, handles very large sets faster")
    ident = sd.plugin_ident()
    extra = {"name": name or outp.stem, "description": description or name or outp.stem, "generator": sd.generator_text()}
    if attribution:
        extra["attribution"] = str(attribution)
    scratch = Path(tempfile.mkdtemp(prefix="gmap_tiles_"))
    paths: list[Path] = []
    try:
        import pyogrio
        src = scratch / "layers.gpkg"
        for lname, (g3, _, _) in prep.items():
            pyogrio.write_dataframe(_fields_ok(g3), src, layer=lname, driver="GPKG", append=src.exists())
        args = ["-f", driver, "-preserve_fid", "-dsco", f"MINZOOM={zmin}", "-dsco", f"MAXZOOM={zmax}", "-dsco", f"NAME={extra['name']}",
                "-dsco", f"DESCRIPTION={extra['description']}", "-dsco", "CONF=" + json.dumps(conf, separators=(",", ":"))]
        if simplify:
            args += ["-dsco", "SIMPLIFICATION=1", "-dsco", "SIMPLIFICATION_MAX_ZOOM=0"]
        for k_, v_ in (mvt_limits or {}).items():           # 1.6.0 (R4-BLD): MAX_SIZE and MAX_FEATURES raised so no footprint is dropped
            args += ["-dsco", f"{k_}={int(v_)}"]
        command = ogr2ogr_command("layers.gpkg", outp.name, driver, zmin, zmax, conf, simplify, extra["name"], description)
        tmp_out = scratch / f"out{outp.suffix}"
        lib = gdal_lib()
        if lib is not None:
            _translate(lib, src, tmp_out, args)
            outp.parent.mkdir(parents=True, exist_ok=True)
            if outp.exists():
                outp.unlink()
            shutil.move(str(tmp_out), outp)
            paths = [outp]
        else:
            # pyogrio alone writes one layer per dataset: each layer goes to its own file
            if len(prep) > 1:
                notes.append("GDAL's tiling library call is not reachable from this Python, so each layer is in its own tile set; run the ogr2ogr command "
                             "in the report to get one file with every layer")
            for lname, (g3, _, _) in prep.items():
                target = outp if len(prep) == 1 else outp.with_name(f"{outp.stem}-{lname}{outp.suffix}")
                if target.exists():
                    target.unlink()
                z = info[lname]
                lopts = {"MINZOOM": str(z["minzoom"]), "MAXZOOM": str(z["maxzoom"]), "NAME": lname, "DESCRIPTION": conf[lname]["description"]}
                dopts = {"MINZOOM": str(z["minzoom"]), "MAXZOOM": str(z["maxzoom"]), "NAME": extra["name"], "DESCRIPTION": extra["description"]}
                dopts.update({k_: str(int(v_)) for k_, v_ in (mvt_limits or {}).items()})
                if simplify:
                    dopts.update(SIMPLIFICATION="1", SIMPLIFICATION_MAX_ZOOM="0")
                pyogrio.write_dataframe(_fields_ok(g3), target, layer=lname, driver=driver, layer_options=lopts, dataset_options=dopts)
                paths.append(target)
        for p in paths:
            if fmt == "pmtiles":
                _pmtiles_set_metadata(p, extra)
            else:
                _mbtiles_set_metadata(p, extra)
        size = sum(p.stat().st_size for p in paths)
        if size > max_bytes:
            for p in paths:
                p.unlink()
            raise TilesError(f"the tile set came to {size / 1024 ** 2:.0f} MB, over the {max_bytes / 1024 ** 2:.0f} MB cap, and was removed. Raise the minimum zoom, "
                             "lower the maximum zoom, keep fewer fields, or clip the data; tippecanoe, run by hand, thins dense points by zoom")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    rep = {"path": str(paths[0]), "paths": [str(p) for p in paths], "format": fmt, "layers": info, "minzoom": zmin, "maxzoom": zmax,
           "bytes": size, "notes": notes, "findings": [], "command": command, "generator": extra["generator"], "plugin_version": ident["version"]}
    if check:
        for p in paths:
            want = {k: v for k, v in info.items() if len(paths) == 1 or p.stem.endswith(f"-{k}")}
            rep["findings"] += read_back(p, want)
    return rep


# ---------------------------------------------------------------------------------------------------------------- read back
def _count_at(path: Path, layer: str, z: int) -> tuple[int, bool]:
    """(distinct features at zoom z, counted by feature id). A feature cut by tile edges comes back once per tile, so features are counted by
    their id; a tile set written without ids is counted by its pieces."""
    import pyogrio
    df = pyogrio.read_dataframe(path, layer=layer, ZOOM_LEVEL=str(z), read_geometry=False)
    if "mvt_id" in df.columns and df["mvt_id"].notna().any():
        return int(df["mvt_id"].nunique()), True
    return int(len(df)), False


def read_back(path: str | Path, expected: dict) -> list[dict]:
    """MAP-OUT-009 on a written tile set: open it with pyogrio, list its layers, count every layer's features at its top and bottom zoom.
    `expected`: {layer: {'features', 'minzoom', 'maxzoom', 'kind'}}."""
    import pyogrio
    p = Path(path)
    fs: list[dict] = []
    try:
        names = [x[0] for x in pyogrio.list_layers(p)]
    except Exception as ex:  # noqa: BLE001 - an unreadable file is the finding
        return [finding("MAP-OUT-009", "BLOCK", f"{p.name} could not be opened again: {str(ex)[:160]}")]
    missing = [k for k in expected if k not in names]
    if missing:
        fs.append(finding("MAP-OUT-009", "BLOCK", f"{p.name}: layer(s) missing from the tiles: {', '.join(missing)}", missing))
    parts = []
    for lname, e in expected.items():
        if lname not in names:
            continue
        top, by_id = _count_at(p, lname, int(e["maxzoom"]))
        low, _ = _count_at(p, lname, int(e["minzoom"]))
        n = int(e["features"])
        parts.append(f"{lname} {top} of {n} at zoom {e['maxzoom']}")
        if by_id and top < n:
            fs.append(finding("MAP-OUT-009", "BLOCK", f"{p.name}: layer '{lname}' has {top} of its {n} features at zoom {e['maxzoom']}; GDAL left some out "
                              "(a tile over its size limit). Raise the minimum zoom or simplify the data", {"layer": lname, "zoom": e["maxzoom"], "found": top, "expected": n}))
        elif not by_id and e.get("kind") == "point" and top < n:
            fs.append(finding("MAP-OUT-009", "BLOCK", f"{p.name}: layer '{lname}' has {top} of its {n} points at zoom {e['maxzoom']}", {"layer": lname}))
        if by_id and low < n and e["minzoom"] != e["maxzoom"]:
            fs.append(finding("MAP-OUT-009", "WARN", f"{p.name}: layer '{lname}' shows {low} of its {n} features at its lowest zoom ({e['minzoom']}); shapes "
                              "smaller than a tile unit vanish there and come back as the map zooms in", {"layer": lname, "zoom": e["minzoom"], "found": low}))
    meta = metadata(p)
    if not meta.get("generator"):
        fs.append(finding("MAP-OUT-009", "WARN", f"{p.name}: the metadata does not carry the generator"))
    fs.append(finding("MAP-OUT-009", "INFO", f"{p.name} read back: {len(names)} layer(s); " + "; ".join(parts), {"layers": names}))
    return fs


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sources", nargs="+", help="layer files; NAME=PATH names a layer")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--maxzoom", default="auto")
    ap.add_argument("--no-simplify", action="store_true")
    ap.add_argument("--attribution")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    import geo_io
    try:
        layers = {}
        for s in a.sources:
            nm, _, path = s.partition("=") if "=" in s else (Path(s).stem, "", s)
            layers[nm] = geo_io.read(path).gdf
        fmt = Path(a.out).suffix.lower().lstrip(".")
        rep = write_tiles(fmt, layers, a.out, minzoom=a.minzoom, maxzoom=a.maxzoom, simplify=not a.no_simplify, attribution=a.attribution, overwrite=a.overwrite)
    except (TilesError, geo_io.GeoIOError) as ex:
        print(f"tiles-out: {ex}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in rep["findings"]) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
