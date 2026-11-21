"""lidar: LAS and LAZ point clouds as elevation grids (3.1.0). A lidar file becomes a GeoTIFF the rest of the plugin reads as any DEM:
hillshade, contours, profiles, line of sight and viewsheds.

    lidar.py info FILE [FILE ...]                      points, classes, bounds, CRS, point density
    lidar.py grid FILE [FILE ...] --out dtm.tif [--cell 1] [--surface dtm|dsm] [--crs EPSG:31370] [--fill 12]
    lidar.py flanders --bbox W,S,E,N [--out dtm.tif] [--cell 1] [--surface dtm|dsm]   Flanders' DHMV II tiles (openlidar), keyless

The surface a grid stands on:
    dtm (bare earth)   the ground (class 2) and water (class 9) points; the lowest point a cell where several fall
    dsm (surface)      every point that is not noise (classes 7 and 18 are left out); the highest point a cell
Cells no point fell in are filled from their neighbours up to `fill` cells away (an inverse-distance mean, ring by ring); further than that
they stay nodata, so a gap in the survey shows as a gap, not as invented ground.

A file is read in chunks of 2 million points (laspy's chunk iterator), so a 500 m tile of 2.4 million points needs about 60 MB, not the whole
cloud at once. LAZ needs the lazrs backend (`pip install laspy[lazrs]`, in `doctor.py --fix --profile raster`). A file whose header names no
CRS takes --crs (Flanders' tiles carry none: Belgian Lambert 72, EPSG:31370).

Flanders (openlidar): the tile index is the WFS of remotesensing.vlaanderen.be (layer LiDAR_DHMV_II_LAZtiles, 500 m tiles in EPSG:31370);
each tile downloads keylessly from /download/openlidar/<tile_location>. At most FLANDERS_MAX_TILES tiles a request (16, about 4 km2 at the
survey's 500 m tiling) and 60 MB a tile; the tiles are kept in <cache>/lidar/flanders so a second map of the same place downloads nothing.
Licence: Modellicentie Gratis Hergebruik (Flemish free reuse licence, CC BY 4.0 compatible): credit Digitaal Vlaanderen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.parse
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

NODATA = -9999.0
CHUNK = 2_000_000
DTM_CLASSES = (2, 9)
NOISE_CLASSES = (7, 18)
MAX_CELLS = 4096 * 4096
FLANDERS_WFS = "https://remotesensing.vlaanderen.be/services/openlidar/wfs"
FLANDERS_DOWNLOAD = "https://remotesensing.vlaanderen.be/download/openlidar/"
FLANDERS_LAYER = "LiDAR_DHMV_II_LAZtiles"
FLANDERS_CRS = "EPSG:31370"
FLANDERS_MAX_TILES = 16
FLANDERS_TILE_BYTES = 60_000_000
FLANDERS_ATTRIBUTION = ("Elevation: LiDAR Digitaal Hoogtemodel Vlaanderen II (DHMV II) point cloud, Digitaal Vlaanderen, "
                        "Modellicentie Gratis Hergebruik (free reuse with credit).")
FLANDERS_ENTRY = "ref.terrain.be-vlg.lidar.dhmv-openlidar"


class LidarError(Exception):
    def __init__(self, message: str, code: int = 2, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


def _laspy():
    try:
        import laspy
    except ImportError:
        raise LidarError("reading LAS or LAZ needs laspy (and lazrs for LAZ): doctor.py --fix --profile raster installs them", 4) from None
    return laspy


def is_lidar(path) -> bool:
    return Path(str(path)).suffix.lower() in (".las", ".laz")


def _open(path: Path):
    laspy = _laspy()
    try:
        return laspy.open(str(path))
    except Exception as ex:  # noqa: BLE001 - laspy raises its own errors for a bad header or a missing LAZ backend
        msg = str(ex)
        if "LazBackend" in msg or "lazrs" in msg or "laszip" in msg.lower():
            raise LidarError(f"{path.name}: a LAZ file needs the lazrs backend: doctor.py --fix --profile raster installs it", 4) from None
        raise LidarError(f"{path.name} is not a LAS or LAZ file laspy can read: {msg[:160]}", 3) from None


def header_crs(path: Path) -> str | None:
    with _open(path) as f:
        try:
            c = f.header.parse_crs()
        except Exception:  # noqa: BLE001 - a malformed VLR is read as no CRS
            c = None
    if c is None:
        return None
    try:
        e = c.to_epsg()
        return f"EPSG:{e}" if e else c.to_wkt()
    except Exception:  # noqa: BLE001
        return None


def info(paths) -> dict:
    """What is in the files: points, classes, bounds, the CRS the header names, points per square metre."""
    out = []
    for p in [Path(x) for x in paths]:
        with _open(p) as f:
            h = f.header
            counts: dict[int, int] = {}
            for pts in f.chunk_iterator(CHUNK):
                u, n = np.unique(np.asarray(pts.classification), return_counts=True)
                for a, b in zip(u.tolist(), n.tolist(), strict=True):
                    counts[int(a)] = counts.get(int(a), 0) + int(b)
            mins, maxs = [float(v) for v in h.mins], [float(v) for v in h.maxs]
        area = max(1e-9, (maxs[0] - mins[0]) * (maxs[1] - mins[1]))
        out.append({"path": str(p), "version": str(h.version), "point_format": int(h.point_format.id), "points": int(h.point_count),
                    "classes": {str(k): v for k, v in sorted(counts.items())}, "bounds": [mins[0], mins[1], maxs[0], maxs[1]], "z": [mins[2], maxs[2]],
                    "crs": header_crs(p), "density_per_m2": round(int(h.point_count) / area, 2)})
    return {"files": out, "points": sum(f["points"] for f in out)}


def _keep(cls: np.ndarray, surface: str) -> np.ndarray:
    if surface == "dtm":
        return np.isin(cls, DTM_CLASSES)
    return ~np.isin(cls, NOISE_CLASSES)


def _bounds_of(paths, bbox=None) -> tuple[float, float, float, float]:
    if bbox is not None:
        return tuple(float(v) for v in bbox)
    W = S = math.inf
    E = N = -math.inf
    for p in paths:
        with _open(p) as f:
            W, S = min(W, float(f.header.mins[0])), min(S, float(f.header.mins[1]))
            E, N = max(E, float(f.header.maxs[0])), max(N, float(f.header.maxs[1]))
    return W, S, E, N


def fill_gaps(a: np.ndarray, rings: int) -> np.ndarray:
    """Empty cells (NaN) take the inverse-distance mean of the filled cells around them, one ring at a time, up to `rings` cells away."""
    a = a.copy()
    for _ in range(max(0, int(rings))):
        miss = np.isnan(a)
        if not miss.any():
            break
        tot = np.zeros_like(a)
        wt = np.zeros_like(a)
        valid = np.pad(~miss, 1).astype(float)          # the frame beyond the edge carries nothing
        filled = np.pad(np.where(~miss, a, 0.0), 1)
        h, w_ = a.shape
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                w = 1.0 / math.hypot(dx, dy)
                tot += w * filled[1 + dy:1 + dy + h, 1 + dx:1 + dx + w_]
                wt += w * valid[1 + dy:1 + dy + h, 1 + dx:1 + dx + w_]
        new = miss & (wt > 0)
        if not new.any():
            break
        a[new] = tot[new] / wt[new]
    return a


def grid(paths, *, cell: float = 1.0, surface: str = "dtm", crs: str | None = None, bbox=None, fill: int = 12) -> dict:
    """The points of the files on a grid of `cell` metres in their own CRS. -> {array (NaN where empty), transform, crs, cell, counts}."""
    from rasterio.transform import from_origin
    if surface not in ("dtm", "dsm"):
        raise LidarError("surface is dtm (bare earth) or dsm (everything above it)", 2)
    paths = [Path(p) for p in paths]
    if not paths:
        raise LidarError("no lidar files", 2)
    for p in paths:
        if not p.is_file():
            raise LidarError(f"file not found: {p}", 2)
    own = {header_crs(p) for p in paths} - {None}
    if len(own) > 1:
        raise LidarError(f"the files name different CRSs ({', '.join(sorted(own))}); grid each set on its own", 3)
    use_crs = next(iter(own), None) or crs
    if not use_crs:
        raise LidarError("the files name no CRS: say which they use with --crs (Flanders' tiles are EPSG:31370)", 3)
    W, S, E, N = _bounds_of(paths, bbox)
    cell = float(cell)
    if cell <= 0:
        raise LidarError("cell must be positive", 2)
    cols, rows = max(1, int(math.ceil((E - W) / cell))), max(1, int(math.ceil((N - S) / cell)))
    if cols * rows > MAX_CELLS:
        k = math.sqrt(cols * rows / MAX_CELLS)
        cell *= k
        cols, rows = max(1, int(math.ceil((E - W) / cell))), max(1, int(math.ceil((N - S) / cell)))
    best = np.full(rows * cols, np.inf if surface == "dtm" else -np.inf)
    counts = np.zeros(rows * cols, dtype=np.int64)
    used = 0
    for p in paths:
        with _open(p) as f:
            for pts in f.chunk_iterator(CHUNK):
                x, y, z = np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z)
                k = _keep(np.asarray(pts.classification), surface) & (x >= W) & (x < E) & (y > S) & (y <= N)
                if not k.any():
                    continue
                c = np.clip(((x[k] - W) / cell).astype(np.int64), 0, cols - 1)
                r = np.clip(((N - y[k]) / cell).astype(np.int64), 0, rows - 1)
                idx = r * cols + c
                if surface == "dtm":
                    np.minimum.at(best, idx, z[k])
                else:
                    np.maximum.at(best, idx, z[k])
                np.add.at(counts, idx, 1)
                used += int(k.sum())
    a = best.reshape(rows, cols)
    a[~np.isfinite(a)] = np.nan
    empty = float(np.isnan(a).mean())
    a = fill_gaps(a, fill)
    return {"array": a, "transform": from_origin(W, N, cell, cell), "crs": use_crs, "cell": cell, "points_used": used,
            "empty_before_fill": empty, "empty_after_fill": float(np.isnan(a).mean()), "bounds": [W, S, E, N], "surface": surface}


def write(g: dict, out: Path, *, sources: list[str], attribution: str | None = None, entry: str | None = None) -> dict:
    import raster_io as rio
    a = np.where(np.isfinite(g["array"]), g["array"], NODATA).astype("float32")
    w = rio.write_geotiff(out, np.ma.masked_equal(a, NODATA), g["transform"], g["crs"], nodata=NODATA, overviews=True)
    rec = {"source": "lidar", "entry": entry, "surface": "bare earth (DTM)" if g["surface"] == "dtm" else "surface model (DSM)", "crs": g["crs"],
           "res_m": g["cell"], "path": str(out), "bytes": w["bytes"], "sha256": w["sha256"], "pixels": [int(a.shape[1]), int(a.shape[0])],
           "nodata": NODATA, "points_used": g["points_used"], "empty_before_fill": round(g["empty_before_fill"], 4),
           "missing_share": round(g["empty_after_fill"], 4), "files": [Path(s).name for s in sources], "attribution": attribution,
           "license": "attribution" if attribution else None, "requests": []}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return rec


def as_raster(path, *, cell: float | None = None, surface: str = "dtm", crs: str | None = None, cache_dir=None) -> Path:
    """A LAS or LAZ file the person supplied, gridded once into <cache>/lidar/<sha>.tif (keyed by the file, the cell and the surface);
    any other path is returned as it is. The map build and terrain analysis call this before they open a raster."""
    p = Path(str(path))
    if not is_lidar(p):
        return p
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    cell = float(cell or 1.0)
    root = Path(cache_dir) if cache_dir else gc.cache_roots()[0] / "lidar"
    out = root / f"{h.hexdigest()[:16]}_{surface}_{cell:g}m.tif"
    if out.is_file():
        return out
    g = grid([p], cell=cell, surface=surface, crs=crs)
    write(g, out, sources=[str(p)])
    return out


# ---------------------------------------------------------------------------------------------------------------- Flanders openlidar
def flanders_tiles(bbox_lambert, http) -> list[dict]:
    """The DHMV II LAZ tiles that touch a box in EPSG:31370 (the openlidar WFS). -> [{location, year, points, density}]."""
    W, S, E, N = bbox_lambert
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": FLANDERS_LAYER, "outputFormat": "application/json",
              "bbox": f"{W},{S},{E},{N},{FLANDERS_CRS}", "count": str(FLANDERS_MAX_TILES * 4)}
    r = http.get(FLANDERS_WFS, params=params, expect="json", max_bytes=20_000_000)
    feats = json.loads(r.content).get("features") or []
    out, seen = [], set()
    for f in feats:
        pr = f.get("properties") or {}
        loc = str(pr.get("tile_location") or "")
        if not loc.lower().endswith(".laz") or ".." in loc or loc.startswith("/") or loc in seen:
            continue
        seen.add(loc)
        out.append({"location": loc, "year": pr.get("tile_year"), "points": pr.get("tile_totalpoints"),
                    "density": pr.get("tile_point_density_allreturn")})
    return out


def flanders(bounds_wgs84, out: Path, *, cell: float = 1.0, surface: str = "dtm", http=None, cache_dir=None) -> dict:
    """Flanders' DHMV II point cloud for a lon/lat window, gridded to `out` (EPSG:31370). -> the fetch record."""
    from pyproj import Transformer
    if http is None:
        import terrain_fetch as tf
        http = tf.safe_http()
    t = Transformer.from_crs("EPSG:4326", FLANDERS_CRS, always_xy=True)
    W, S, E, N = bounds_wgs84
    xs, ys = zip(*[t.transform(W + (E - W) * i / 4, S + (N - S) * j / 4) for i in range(5) for j in range(5)])
    box = (min(xs), min(ys), max(xs), max(ys))
    tiles = flanders_tiles(box, http)
    if not tiles:
        raise LidarError("the openlidar index has no DHMV II tile in this window (Flanders only)", 3, "MAP-TER-003")
    if len(tiles) > FLANDERS_MAX_TILES:
        raise LidarError(f"the window needs {len(tiles)} lidar tiles; at most {FLANDERS_MAX_TILES} are fetched a map (about 4 km2): a smaller window, "
                         "or the 1 m DHMV II DTM (terrain.source: flanders-dhmv) for a larger one", 3, "MAP-TER-003")
    root = Path(cache_dir) if cache_dir else gc.cache_roots()[0] / "lidar" / "flanders"
    root.mkdir(parents=True, exist_ok=True)
    files, reqs = [], []
    for tl in tiles:
        local = root / Path(tl["location"]).name
        if not local.is_file():
            url = FLANDERS_DOWNLOAD + urllib.parse.quote(tl["location"])
            r = http.get(url, expect="any", max_bytes=FLANDERS_TILE_BYTES)
            if r.content[:4] != b"LASF":
                raise LidarError(f"openlidar answered something that is not a LAS file for {Path(tl['location']).name}", 3, "MAP-TER-003")
            local.write_bytes(r.content)
            reqs.append({"url": url, "status": 200, "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()})
        files.append(local)
    g = grid(files, cell=cell, surface=surface, crs=FLANDERS_CRS, bbox=box)
    rec = write(g, out, sources=[str(f) for f in files], attribution=FLANDERS_ATTRIBUTION, entry=FLANDERS_ENTRY)
    rec.update({"source": "flanders-lidar", "requests": reqs, "tiles": len(tiles), "years": sorted({t["year"] for t in tiles if t.get("year")})})
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    a1 = sub.add_parser("info")
    a1.add_argument("files", nargs="+")
    a2 = sub.add_parser("grid")
    a2.add_argument("files", nargs="+")
    a2.add_argument("--out", required=True)
    a3 = sub.add_parser("flanders")
    a3.add_argument("--bbox", required=True, help="W,S,E,N in lon/lat")
    a3.add_argument("--out", default="flanders-dtm.tif")
    for a in (a2, a3):
        a.add_argument("--cell", type=float, default=1.0)
        a.add_argument("--surface", choices=["dtm", "dsm"], default="dtm")
    a2.add_argument("--crs")
    a2.add_argument("--fill", type=int, default=12)
    a = ap.parse_args(argv)
    try:
        if a.op == "info":
            res = info(a.files)
        elif a.op == "grid":
            g = grid(a.files, cell=a.cell, surface=a.surface, crs=a.crs, fill=a.fill)
            res = write(g, Path(a.out), sources=a.files)
        else:
            res = flanders([float(v) for v in a.bbox.split(",")], Path(a.out), cell=a.cell, surface=a.surface)
    except LidarError as ex:
        print(json.dumps({"ok": False, "error": str(ex), "rule": ex.rule}), file=sys.stderr)
        return ex.code
    print(json.dumps({"ok": True, **res}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
