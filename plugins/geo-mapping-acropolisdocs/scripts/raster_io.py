#!/usr/bin/env python3
"""raster_io: the plugin's raster reader (gap analysis P1-4; M12).

Everything a map needs from a raster, with rasterio (a SOFT dependency: without it every call raises RasterError naming
`pip install rasterio`). Nothing here draws a map or knows a brand: colours come in as a ramp, pixels go out as a PNG with
the legend entries that explain it.

    import raster_io as rio
    info = rio.open_info("dem.tif")                                       # driver, crs, bounds, res, bands, nodata, dtype, is_cog, overviews, size
    g = rio.read_window("dem.tif", bounds, "EPSG:32612", max_px=2048)    # Grid: masked array + transform in the target CRS (overviews used)
    png, legend = rio.to_png(g.array, brand_ramp, vmin=0, vmax=3000)     # bytes + legend entries; the ramp is the brand's, never hard-coded
    s = rio.stats("rsrp.tif", bounds=bounds, crs="EPSG:4326")             # min, max, mean, std, percentiles, sampled
    zoned, prov = rio.zonal("ghsl_pop.tif", tracts_gdf, "sum")          # a field per polygon with a provenance record
    hs = rio.hillshade(g.array, g.transform, crs=g.crs)                  # Horn method, 0..1
    lines = rio.contours(g.array, g.transform, g.crs, interval=50)       # GeoDataFrame of LineStrings with `elevation` and `index`
    img = rio.read_wms_image(url, ["layer"], bounds, "EPSG:3857", (1024, 768))   # a GetMap picture as a Grid (bands, rows, cols)
    img = rio.read_arcgis_export(url, bounds, 3857, cell=30)                  # a MapServer export, placed on the extent the server drew

Rules:
  * nodata is honoured everywhere: a window comes back masked, a statistic ignores it, a zonal count says how many cells it skipped,
    a PNG makes it transparent (`nodata_alpha`).
  * a window is read at the resolution it is asked for, so a 10 m DEM under a state map is read from its overviews, never in full.
    `max_px` caps the longer side (default 4096).
  * every outbound request goes through ogc_http.SafeHTTP (public hosts only, GET, byte and time caps, the plugin's User-Agent).
  * the measurement in `zonal` is said plainly in the provenance: the pixel area, how nodata was handled and the partial-pixel policy
    (a cell counts when its centre is inside the polygon; `all_touched=True` counts every cell the polygon touches; nothing is
    area-weighted).
"""
from __future__ import annotations

import io
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import geo_io  # noqa: E402

MAX_PX_DEFAULT = 4096
STATS_SAMPLE_PX = 1_000_000
RESAMPLING = ("nearest", "bilinear", "cubic", "average", "mode", "max", "min", "med", "q1", "q3", "sum", "rms", "lanczos", "cubic_spline", "gauss")
ZONAL_STATS = ("sum", "mean", "min", "max", "count", "majority", "median", "std")
R_EARTH = 6378137.0
M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON_EQ = 111_320.0


class RasterError(geo_io.GeoIOError):
    """A refusal or a failure with a message that is safe to show."""


def _rasterio():
    try:
        import rasterio  # noqa: F401
        return rasterio
    except ImportError:
        raise RasterError("raster input needs the rasterio package (python3 -m pip install rasterio, or requirements-optional.txt)") from None


def _resampling(name: str | None):
    from rasterio.enums import Resampling
    n = (name or "bilinear").lower()
    if n not in RESAMPLING:
        raise RasterError(f"unknown resampling '{name}'; use one of {', '.join(RESAMPLING)}")
    return getattr(Resampling, n)


# --------------------------------------------------------------------------------------------------------------- the grid

@dataclass
class Grid:
    """A raster window in memory: `array` (masked, 2-D for one band or 3-D bands x rows x cols), its `transform` and `crs`."""
    array: object
    transform: object
    crs: str
    bounds: tuple
    nodata: object = None
    source: dict = field(default_factory=dict)

    def __iter__(self):                      # a, t = read_window(...)
        yield self.array
        yield self.transform

    @property
    def shape(self) -> tuple:
        return self.array.shape[-2:]

    @property
    def res(self) -> tuple[float, float]:
        return abs(self.transform.a), abs(self.transform.e)


def crs_string(crs) -> str:
    try:
        from rasterio.crs import CRS
        c = CRS.from_user_input(crs)
        auth = c.to_authority()
        return f"{auth[0]}:{auth[1]}" if auth else c.to_wkt()
    except Exception:  # noqa: BLE001
        return str(crs)


def is_geographic(crs) -> bool:
    from rasterio.crs import CRS
    try:
        return bool(CRS.from_user_input(crs).is_geographic)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------------------------- open_info

def open_info(path) -> dict:
    """What the raster is: driver, crs, bounds, res, bands, nodata, dtype, size, overviews, whether it is a Cloud Optimized GeoTIFF."""
    rasterio = _rasterio()
    p = Path(path)
    if not p.exists():
        raise RasterError(f"raster not found: {p}")
    try:
        ds = rasterio.open(p)
    except Exception as ex:  # noqa: BLE001
        raise RasterError(f"{p.name} could not be opened as a raster: {type(ex).__name__}: {str(ex)[:160]}") from None
    with ds:
        ovr = [list(ds.overviews(b)) for b in ds.indexes]
        struct = ds.tags(ns="IMAGE_STRUCTURE") or {}
        tiled = bool(ds.block_shapes and ds.block_shapes[0][1] < ds.width) if ds.count else False      # a strip raster has blocks as wide as the image
        layout_cog = str(struct.get("LAYOUT", "")).upper() == "COG"
        is_cog = ds.driver == "GTiff" and (layout_cog or (tiled and all(o for o in ovr)))
        why = ("written with the COG layout" if layout_cog else "tiled with overviews on every band" if is_cog else
               "not a GeoTIFF" if ds.driver != "GTiff" else "not tiled" if not tiled else "no overviews")
        crs = crs_string(ds.crs) if ds.crs else None
        info = {"path": str(p), "driver": ds.driver, "crs": crs, "geographic": bool(ds.crs.is_geographic) if ds.crs else None,
                "bounds": [float(v) for v in ds.bounds], "res": [float(abs(ds.res[0])), float(abs(ds.res[1]))], "size": [int(ds.width), int(ds.height)],
                "bands": int(ds.count), "dtype": list(ds.dtypes)[0] if ds.count else None, "dtypes": list(ds.dtypes), "nodata": _py(ds.nodata),
                "nodatavals": [_py(v) for v in ds.nodatavals], "overviews": ovr, "is_cog": bool(is_cog), "cog_reason": why, "tiled": tiled,
                "block_shapes": [list(b) for b in ds.block_shapes], "compression": struct.get("COMPRESSION"), "interleave": struct.get("INTERLEAVE"),
                "descriptions": list(ds.descriptions), "units": list(ds.units), "colorinterp": [c.name for c in ds.colorinterp],
                "transform": list(ds.transform)[:6], "tags": {k: v for k, v in (ds.tags() or {}).items() if len(str(v)) < 200}}
    info["pixels"] = info["size"][0] * info["size"][1]
    return info


def _py(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------------------------------------------- read_window

def _bounds_ok(b):
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        raise RasterError("bounds must be four numbers: minx miny maxx maxy") from None
    if not (x1 > x0 and y1 > y0):
        raise RasterError(f"bounds are empty or inverted: {b}")
    return x0, y0, x1, y1


def read_window(path, bounds, crs, out_shape: tuple | None = None, max_px: int = MAX_PX_DEFAULT, band=1, resampling: str = "bilinear",
                nodata=None) -> Grid:
    """The part of the raster inside `bounds` (in `crs`), reprojected to `crs` through a WarpedVRT, as a masked array with its transform.

    `out_shape` (rows, cols) sets the pixel count; otherwise the source resolution is kept, capped so the longer side is `max_px` (the
    read then comes from the overview nearest that scale). `band` is one index or a list for a bands x rows x cols result.
    Nodata (the file's, or `nodata` when the file declares none) is masked."""
    rasterio = _rasterio()
    from rasterio.vrt import WarpedVRT
    from rasterio.transform import from_bounds
    from rasterio.windows import from_bounds as window_from_bounds
    x0, y0, x1, y1 = _bounds_ok(bounds)
    rs = _resampling(resampling)
    p = Path(path)
    bands = [int(band)] if isinstance(band, (int, np.integer)) else [int(b) for b in band]
    with rasterio.open(p) as src:
        if src.crs is None:
            raise RasterError(f"{p.name} has no CRS; say which it uses first (gdal_edit -a_srs, or raster_io.assign)")
        nd = src.nodata if src.nodata is not None else nodata
        kw = {"crs": crs, "resampling": rs}
        if nd is not None:
            kw["src_nodata"] = nd
            kw["nodata"] = nd
        with WarpedVRT(src, **kw) as probe:                      # the source's own resolution in the target CRS
            win = window_from_bounds(x0, y0, x1, y1, transform=probe.transform)
            vcrs = crs_string(probe.crs)
        if out_shape:
            h, w = int(out_shape[0]), int(out_shape[1])
        else:
            w, h = max(1, int(round(win.width))), max(1, int(round(win.height)))
            k = max(w, h) / float(max_px)
            if k > 1:
                w, h = max(1, int(round(w / k))), max(1, int(round(h / k)))
        if w * h > max_px * max_px * 4:
            raise RasterError(f"a {w} x {h} window is too large; lower out_shape or max_px")
        t = from_bounds(x0, y0, x1, y1, w, h)
        if nd is None:                                                      # no nodata value: an alpha band marks what lies outside the source
            kw["add_alpha"] = True
        with WarpedVRT(src, transform=t, width=w, height=h, **kw) as vrt:   # the VRT's grid is the window itself; outside the source is nodata
            if nd is None:
                raw = vrt.read(bands)
                outside = vrt.read(vrt.count) == 0
                data = np.ma.masked_array(raw, mask=np.broadcast_to(outside, raw.shape).copy())
            else:
                data = vrt.read(bands, masked=True)
    arr = data[0] if len(bands) == 1 else data
    if np.issubdtype(np.asarray(arr).dtype, np.floating):
        arr = np.ma.masked_invalid(arr)
    return Grid(arr, t, vcrs, (x0, y0, x1, y1), nd, {"path": str(p), "bands": bands, "resampling": (resampling or "bilinear"), "src_crs": crs_string(src.crs),
                                                      "src_res": [float(abs(src.res[0])), float(abs(src.res[1]))], "out_shape": [h, w]})


def sample(path, lonlats, crs_of_points: str = "EPSG:4326", band: int = 1, method: str = "bilinear"):
    """Elevation (or any band) at points, bilinear by default, read from the raster's own grid. -> float array (nan where nodata)."""
    rasterio = _rasterio()
    from pyproj import Transformer
    pts = np.asarray(lonlats, dtype=float).reshape(-1, 2)
    with rasterio.open(path) as src:
        if src.crs is None:
            raise RasterError("the raster has no CRS")
        tr = Transformer.from_crs(crs_of_points, src.crs, always_xy=True)
        xs, ys = tr.transform(pts[:, 0], pts[:, 1])
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        inv = ~src.transform
        cols, rows = inv @ (xs, ys)
        cols, rows = np.asarray(cols) - 0.5, np.asarray(rows) - 0.5       # pixel centres at integer coordinates
        pad = 2
        c0, c1 = int(max(0, math.floor(np.nanmin(cols)) - pad)), int(min(src.width, math.ceil(np.nanmax(cols)) + pad + 1))
        r0, r1 = int(max(0, math.floor(np.nanmin(rows)) - pad)), int(min(src.height, math.ceil(np.nanmax(rows)) + pad + 1))
        if c1 <= c0 or r1 <= r0:
            return np.full(len(pts), np.nan)
        from rasterio.windows import Window
        a = src.read(band, window=Window(c0, r0, c1 - c0, r1 - r0), masked=True).astype(float).filled(np.nan)
    return bilinear(a, cols - c0, rows - r0) if method == "bilinear" else nearest(a, cols - c0, rows - r0)


def bilinear(a: np.ndarray, cols, rows) -> np.ndarray:
    """Bilinear interpolation of `a` (nan = nodata) at fractional pixel-centre coordinates; nan outside or next to nodata."""
    cols, rows = np.asarray(cols, float), np.asarray(rows, float)
    h, w = a.shape
    out = np.full(cols.shape, np.nan)
    ok = (cols >= -0.5) & (cols <= w - 0.5) & (rows >= -0.5) & (rows <= h - 0.5)      # anywhere inside the edge cells counts
    if not ok.any():
        return out
    c, r = np.clip(cols[ok], 0, w - 1), np.clip(rows[ok], 0, h - 1)                  # the edge half-cell takes the edge value
    c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    fc, fr = c - c0, r - r0
    v = (a[r0, c0] * (1 - fc) * (1 - fr) + a[r0, c1] * fc * (1 - fr) + a[r1, c0] * (1 - fc) * fr + a[r1, c1] * fc * fr)
    out[ok] = v
    return out


def nearest(a: np.ndarray, cols, rows) -> np.ndarray:
    cols, rows = np.asarray(cols, float), np.asarray(rows, float)
    h, w = a.shape
    out = np.full(cols.shape, np.nan)
    c, r = np.rint(cols).astype(int), np.rint(rows).astype(int)
    ok = (c >= 0) & (c < w) & (r >= 0) & (r < h)
    out[ok] = a[r[ok], c[ok]]
    return out


# --------------------------------------------------------------------------------------------------------------- to_png

def _hex_rgb(h: str) -> tuple[int, int, int]:
    s = str(h).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise RasterError(f"'{h}' is not a hex colour")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(round(float(v))) for v in rgb[:3]))


def ramp_lut(ramp, n: int = 256) -> np.ndarray:
    """A list of hex colours (interpolated) or a callable t in 0..1 -> (r, g, b) as an n x 3 uint8 table."""
    t = np.linspace(0.0, 1.0, n)
    if callable(ramp):
        rows = []
        for v in t:
            c = ramp(float(v))
            c = c[:3]
            if all(isinstance(x, float) and 0.0 <= x <= 1.0 for x in c) and max(c) <= 1.0:
                c = tuple(x * 255.0 for x in c)
            rows.append(c)
        return np.clip(np.asarray(rows, float), 0, 255).astype(np.uint8)
    cols = [_hex_rgb(h) for h in (ramp or [])]
    if len(cols) < 2:
        raise RasterError("a ramp needs at least two colours (from the brand's sequential or diverging ramp)")
    stops = np.linspace(0.0, 1.0, len(cols))
    arr = np.asarray(cols, float)
    lut = np.stack([np.interp(t, stops, arr[:, i]) for i in range(3)], axis=1)
    return np.clip(lut, 0, 255).astype(np.uint8)


def to_png(array, ramp, vmin=None, vmax=None, nodata_alpha: int = 0, classes: list | None = None, class_labels: list | None = None,
           alpha: int = 255, fmt=None) -> tuple[bytes, list[dict]]:
    """Colour a 2-D array with a brand ramp and return (PNG bytes, legend entries).

    Continuous: values from vmin to vmax (the data's 2nd and 98th percentiles when not given) are mapped along the ramp; the legend
    holds five stops {value, hex, label}. Classed: `classes` is the list of ascending break values; values below the first break take the
    first colour, above the last the last; the legend has one entry per class {from, to, hex, label}. Masked or nan cells get
    `nodata_alpha` (0 = transparent)."""
    from PIL import Image
    a = np.ma.masked_invalid(np.ma.asarray(array, dtype=float))
    if a.ndim != 2:
        raise RasterError("to_png takes one band (a 2-D array)")
    valid = ~np.ma.getmaskarray(a)
    data = np.ma.filled(a, np.nan)
    lut = ramp_lut(ramp)
    rgba = np.zeros(a.shape + (4,), np.uint8)
    legend: list[dict] = []
    fmt = fmt or (lambda v: f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.3g}")
    if classes:
        breaks = [float(b) for b in classes]
        if any(b2 < b1 for b1, b2 in zip(breaks, breaks[1:])):
            raise RasterError("classes must be ascending break values")
        n = len(breaks) + 1
        idx = np.digitize(np.nan_to_num(data, nan=breaks[0]), breaks)
        picks = np.linspace(0, len(lut) - 1, n).round().astype(int)
        colors = lut[picks]
        rgba[..., :3] = colors[idx]
        lo = float(np.nanmin(data)) if valid.any() else breaks[0]
        hi = float(np.nanmax(data)) if valid.any() else breaks[-1]
        edges = [lo] + breaks + [hi]
        for i in range(n):
            f, t = edges[i], edges[i + 1]
            lab = (class_labels[i] if class_labels and i < len(class_labels) else
                   ((f"{fmt(f)} to {fmt(t)}" if f == 0 else f"below {fmt(t)}") if i == 0      # 0.23.0: a minimum of 0 is meaningful (counts)
                    else f"{fmt(f)} and above" if i == n - 1 else f"{fmt(f)} to {fmt(t)}"))
            legend.append({"from": f, "to": t, "hex": _rgb_hex(colors[i]), "label": lab, "kind": "class"})
    else:
        if valid.any():
            lo = float(np.nanpercentile(data[valid], 2)) if vmin is None else float(vmin)
            hi = float(np.nanpercentile(data[valid], 98)) if vmax is None else float(vmax)
        else:
            lo, hi = (0.0 if vmin is None else float(vmin)), (1.0 if vmax is None else float(vmax))
        if hi <= lo:
            hi = lo + 1.0
        t = np.clip((np.nan_to_num(data, nan=lo) - lo) / (hi - lo), 0, 1)
        idx = (t * (len(lut) - 1)).round().astype(int)
        rgba[..., :3] = lut[idx]
        for q in (0.0, 0.25, 0.5, 0.75, 1.0):
            v = lo + q * (hi - lo)
            legend.append({"value": v, "hex": _rgb_hex(lut[int(round(q * (len(lut) - 1)))]), "label": fmt(v), "kind": "stop"})
        legend[0]["label"] = fmt(lo) + (" or less" if vmin is not None else "")
        legend[-1]["label"] = fmt(hi) + (" or more" if vmax is not None else "")
    rgba[..., 3] = np.where(valid, alpha, nodata_alpha).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), legend


# --------------------------------------------------------------------------------------------------------------- stats

def stats(path, bounds=None, crs: str | None = None, band: int = 1, sample_px: int = STATS_SAMPLE_PX, percentiles=(2, 5, 25, 50, 75, 95, 98)) -> dict:
    """min, max, mean, std, percentiles and counts, sampled: at most `sample_px` pixels are read (from overviews when the raster is larger),
    so the figures are of a resampled picture of the raster and the record says so."""
    rasterio = _rasterio()
    p = Path(path)
    with rasterio.open(p) as src:
        full = (src.width * src.height)
        if bounds is None:
            b = tuple(float(v) for v in src.bounds)
            c = crs_string(src.crs)
        else:
            b, c = _bounds_ok(bounds), (crs or crs_string(src.crs))
    side = int(math.sqrt(sample_px))
    g = read_window(p, b, c, max_px=side, band=band, resampling="average" if full > sample_px else "nearest")
    a = np.ma.masked_invalid(np.ma.asarray(g.array, dtype=float))
    v = a.compressed()
    n_all = int(a.size)
    out = {"path": str(p), "band": int(band), "crs": g.crs, "bounds": list(b), "pixels_read": n_all, "pixels_valid": int(v.size), "pixels_nodata": n_all - int(v.size),
           "sampled": bool(full > n_all), "sample_resampling": g.source["resampling"], "source_pixels": int(full)}
    if v.size == 0:
        out.update({"min": None, "max": None, "mean": None, "std": None, "percentiles": {}})
        return out
    pc = np.percentile(v, list(percentiles))
    out.update({"min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "std": float(v.std()), "sum": float(v.sum()),
                "percentiles": {f"p{int(q)}": float(x) for q, x in zip(percentiles, pc)}})
    return out


# --------------------------------------------------------------------------------------------------------------- zonal

def pixel_area_m2(transform, crs, lat: float | None = None) -> tuple[float, str]:
    """The ground area of one pixel in square metres and how it was found."""
    dx, dy = abs(transform.a), abs(transform.e)
    if is_geographic(crs):
        la = math.radians(lat if lat is not None else 0.0)
        return dx * M_PER_DEG_LON_EQ * math.cos(la) * dy * M_PER_DEG_LAT, f"degrees converted at latitude {lat if lat is not None else 0:.2f}"
    if is_web_mercator(crs):
        la = math.radians(lat if lat is not None else 0.0)
        return dx * dy * math.cos(la) ** 2, f"Web Mercator metres scaled by cos^2 of latitude {lat if lat is not None else 0:.2f}"
    return dx * dy, "projected CRS units squared"


def is_web_mercator(crs) -> bool:
    try:
        from rasterio.crs import CRS
        c = CRS.from_user_input(crs)
        a = c.to_authority()
        return bool(a and a[1] in ("3857", "900913", "3785", "102100"))
    except Exception:  # noqa: BLE001
        return False


def zonal(path, polygons, stat: str = "sum", band: int = 1, all_touched: bool = False, field: str | None = None, nodata=None):
    """A statistic of the raster inside each polygon, as a new field. -> (GeoDataFrame, provenance dict).

    stat: sum | mean | min | max | count | majority | median | std. The polygons are projected to the raster's CRS; each is rasterized
    onto the raster's own grid (cells whose centre falls inside, or every cell touched with all_touched) and the statistic is taken over
    the valid cells. `count` is the number of valid cells; `<field>_cells` and `<field>_nodata` are written beside the statistic."""
    rasterio = _rasterio()
    from rasterio import features
    from rasterio.windows import Window
    import geopandas as gpd
    st = str(stat).lower()
    if st not in ZONAL_STATS:
        raise RasterError(f"zonal stat must be one of {', '.join(ZONAL_STATS)}")
    if polygons.crs is None:
        raise RasterError("the polygon layer has no CRS; say which it uses first")
    fld = field or f"{Path(path).stem}_{st}"[:60]
    p = Path(path)
    with rasterio.open(p) as src:
        if src.crs is None:
            raise RasterError(f"{p.name} has no CRS")
        nd = src.nodata if src.nodata is not None else nodata
        polys = polygons.to_crs(src.crs) if str(polygons.crs) != str(src.crs) else polygons
        T = src.transform
        inv = ~T
        vals, cells, nds = [], [], []
        for geom in polys.geometry:
            if geom is None or geom.is_empty:
                vals.append(np.nan); cells.append(0); nds.append(0)
                continue
            gx0, gy0, gx1, gy1 = geom.bounds
            ca, ra = inv @ (gx0, gy1)
            cb, rb = inv @ (gx1, gy0)
            c0, r0 = int(max(0, math.floor(min(ca, cb)))), int(max(0, math.floor(min(ra, rb))))
            c1, r1 = int(min(src.width, math.ceil(max(ca, cb)) + 1)), int(min(src.height, math.ceil(max(ra, rb)) + 1))
            if c1 <= c0 or r1 <= r0:
                vals.append(np.nan); cells.append(0); nds.append(0)
                continue
            win = Window(c0, r0, c1 - c0, r1 - r0)
            wt = src.window_transform(win)
            a = src.read(band, window=win, masked=True)
            if nd is not None and not np.ma.is_masked(a):
                a = np.ma.masked_equal(a, nd)
            m = features.rasterize([(geom, 1)], out_shape=(int(win.height), int(win.width)), transform=wt, fill=0, all_touched=all_touched, dtype="uint8").astype(bool)
            inside = m & ~np.ma.getmaskarray(a)
            if np.issubdtype(a.dtype, np.floating):
                inside &= np.isfinite(np.ma.filled(a, np.nan))
            n_nd = int(m.sum() - inside.sum())
            v = np.ma.filled(a, 0)[inside]
            cells.append(int(inside.sum())); nds.append(n_nd)
            if v.size == 0:
                vals.append(0.0 if st in ("sum", "count") else np.nan)
                continue
            if st == "sum":
                vals.append(float(v.sum(dtype=np.float64)))
            elif st == "mean":
                vals.append(float(v.mean(dtype=np.float64)))
            elif st == "min":
                vals.append(float(v.min()))
            elif st == "max":
                vals.append(float(v.max()))
            elif st == "count":
                vals.append(int(v.size))
            elif st == "median":
                vals.append(float(np.median(v)))
            elif st == "std":
                vals.append(float(v.std()))
            else:
                u, cnt = np.unique(v, return_counts=True)
                vals.append(_py(u[int(np.argmax(cnt))]))
        lat = None
        try:
            from pyproj import Transformer
            cx, cy = (src.bounds.left + src.bounds.right) / 2, (src.bounds.top + src.bounds.bottom) / 2
            lat = float(Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True).transform(cx, cy)[1])
        except Exception:  # noqa: BLE001
            pass
        area, how = pixel_area_m2(T, src.crs, lat)
        prov = {"op": "raster_zonal", "raster": str(p), "band": int(band), "stat": st, "field": fld, "raster_crs": crs_string(src.crs), "measure_crs": crs_string(src.crs),
                "pixel_size": [float(abs(T.a)), float(abs(T.e))], "pixel_area_m2": area, "pixel_area_how": how, "nodata": _py(nd),
                "nodata_handling": "cells equal to nodata (or nan) are left out of every statistic and counted in <field>_nodata",
                "partial_pixel_policy": ("every cell the polygon touches counts in full (all_touched)" if all_touched else
                                         "a cell counts in full when its centre is inside the polygon; a cell whose centre is outside does not count (no area weighting)"),
                "polygons": int(len(polys)), "cells_total": int(sum(cells)), "cells_nodata": int(sum(nds)), "all_touched": bool(all_touched)}
    out = polygons.copy()
    out[fld] = vals
    out[f"{fld}_cells"] = cells
    out[f"{fld}_nodata"] = nds
    note = (f"`{fld}`: the {st} of band {band} of {p.name} over the cells inside each polygon, measured on the raster's own grid ({prov['raster_crs']}, "
            f"cells of {abs(T.a):g} x {abs(T.e):g} units, about {area:,.0f} m2 each). {prov['partial_pixel_policy'].capitalize()}. "
            f"{sum(nds):,} nodata cell(s) were skipped." if sum(nds) else
            f"`{fld}`: the {st} of band {band} of {p.name} over the cells inside each polygon, measured on the raster's own grid ({prov['raster_crs']}, "
            f"cells of {abs(T.a):g} x {abs(T.e):g} units, about {area:,.0f} m2 each). {prov['partial_pixel_policy'].capitalize()}.")
    prov["note"] = note
    return gpd.GeoDataFrame(out, geometry=polygons.geometry.name, crs=polygons.crs), prov


# --------------------------------------------------------------------------------------------------------------- terrain derivatives

def cell_size_m(transform, crs=None, lat: float | None = None) -> tuple[float, float]:
    """The ground size of one cell in metres (dx, dy): projected units as they are, Web Mercator scaled by cos(lat), degrees converted."""
    dx, dy = abs(transform.a), abs(transform.e)
    if crs is None:
        return dx, dy
    la = math.radians(lat if lat is not None else 0.0)
    if is_geographic(crs):
        return dx * M_PER_DEG_LON_EQ * math.cos(la), dy * M_PER_DEG_LAT
    if is_web_mercator(crs):
        return dx * math.cos(la), dy * math.cos(la)
    return dx, dy


def centre_lat(transform, shape, crs) -> float | None:
    try:
        from pyproj import Transformer
        h, w = shape[-2:]
        cx, cy = transform @ (w / 2.0, h / 2.0)
        return float(Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(cx, cy)[1])
    except Exception:  # noqa: BLE001
        return None


def _gradients(dem, transform, z_factor: float, crs=None, lat=None):
    """Horn (1981) 3 x 3 gradients dz/dx, dz/dy in metres per metre; nodata filled by its neighbours' mean so edges do not spike."""
    a = np.ma.masked_invalid(np.ma.asarray(dem, dtype=float))
    mask = np.ma.getmaskarray(a)
    z = np.ma.filled(a, np.nan)
    if mask.any():
        fill = np.nanmean(z) if np.isfinite(z).any() else 0.0
        z = np.where(mask, fill, z)
    z = np.pad(z, 1, mode="edge")
    if lat is None and crs is not None:
        lat = centre_lat(transform, a.shape, crs)
    dx, dy = cell_size_m(transform, crs, lat)
    zf = float(z_factor)
    a_, b_, c_ = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d_, f_ = z[1:-1, :-2], z[1:-1, 2:]
    g_, h_, i_ = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dzdx = ((c_ + 2 * f_ + i_) - (a_ + 2 * d_ + g_)) / (8.0 * dx) * zf
    dzdy = ((g_ + 2 * h_ + i_) - (a_ + 2 * b_ + c_)) / (8.0 * dy) * zf
    return dzdx, dzdy, mask


def hillshade(dem, transform, azimuth: float = 315.0, altitude: float = 45.0, z_factor: float = 1.0, multidirectional: bool = False, *, crs=None, lat=None) -> np.ndarray:
    """Shaded relief by the Horn method, 0 (shadow) to 1 (lit). `azimuth` is clockwise from north, `altitude` above the horizon.
    `multidirectional` blends 225, 270, 315 and 360 degrees (weights 0.1, 0.25, 0.5, 0.15) the way GDAL does. `crs` (with the centre `lat`)
    turns degree or Web Mercator cells into metres so slopes are right; `z_factor` scales the heights. Nodata is nan."""
    dzdx, dzdy, mask = _gradients(dem, transform, z_factor, crs, lat)
    # the surface normal (east, north, up): dzdy is per row, rows run south, so dz/dnorth = -dzdy
    norm = np.sqrt(dzdx * dzdx + dzdy * dzdy + 1.0)
    nx, ny, nz = -dzdx / norm, dzdy / norm, 1.0 / norm
    alt = math.radians(altitude)

    def shade(az_deg):
        az = math.radians(az_deg)
        lx, ly, lz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
        return np.clip(nx * lx + ny * ly + nz * lz, 0.0, 1.0)
    if multidirectional:
        out = 0.1 * shade(225) + 0.25 * shade(270) + 0.5 * shade(315) + 0.15 * shade(360)
    else:
        out = shade(float(azimuth))
    out = np.clip(out, 0.0, 1.0)
    out[mask] = np.nan
    return out


def slope(dem, transform, z_factor: float = 1.0, units: str = "degrees", *, crs=None, lat=None) -> np.ndarray:
    """Slope by the Horn method in degrees (default) or percent; nodata is nan."""
    dzdx, dzdy, mask = _gradients(dem, transform, z_factor, crs, lat)
    g = np.hypot(dzdx, dzdy)
    out = np.degrees(np.arctan(g)) if units == "degrees" else g * 100.0
    out[mask] = np.nan
    return out


def aspect(dem, transform, z_factor: float = 1.0, *, crs=None, lat=None, flat=np.nan) -> np.ndarray:
    """Aspect in degrees clockwise from north (0 north, 90 east); flat cells get `flat`; nodata is nan."""
    dzdx, dzdy, mask = _gradients(dem, transform, z_factor, crs, lat)
    out = np.degrees(np.arctan2(-dzdx, dzdy)) % 360.0     # downhill direction (east, north) = (-dzdx, -dz/dnorth) = (-dzdx, dzdy); rows grow southward
    out = np.where((np.abs(dzdx) < 1e-12) & (np.abs(dzdy) < 1e-12), flat, out)
    out = out.astype(float)
    out[mask] = np.nan
    return out


def contour_levels(dem, interval: float | None = None, levels=None, base: float = 0.0) -> list[float]:
    a = np.ma.masked_invalid(np.ma.asarray(dem, dtype=float))
    if levels is not None:
        return [float(v) for v in levels]
    if not interval or interval <= 0:
        raise RasterError("contours need an interval (metres) or explicit levels")
    if a.count() == 0:
        return []
    lo, hi = float(a.min()), float(a.max())
    first = math.floor((lo - base) / interval) * interval + base
    out = []
    v = first
    while v <= hi + 1e-9:
        if v >= lo:
            out.append(round(v, 6))
        v += interval
    if len(out) > 2000:
        raise RasterError(f"{len(out)} contour levels at {interval} m over a range of {hi - lo:,.0f} m is too many; use a larger interval")
    return out


def contours(dem, transform, crs, interval: float | None = None, levels=None, index_every: int = 5, min_points: int = 3, base: float = 0.0):
    """Contour lines through the DEM with contourpy. -> GeoDataFrame (LineString) with `elevation`, `index` (every `index_every`-th level
    counted from `base`), `length` in CRS units. Nodata is left out (contours stop at it)."""
    try:
        import contourpy
    except ImportError:
        raise RasterError("contours need the contourpy package (installed with matplotlib; python3 -m pip install contourpy)") from None
    import geopandas as gpd
    from shapely.geometry import LineString
    a = np.ma.masked_invalid(np.ma.asarray(dem, dtype=float))
    h, w = a.shape
    lv = contour_levels(a, interval, levels, base)
    xs = transform.c + transform.a * (np.arange(w) + 0.5)
    ys = transform.f + transform.e * (np.arange(h) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    gen = contourpy.contour_generator(X, Y, a, name="serial", line_type=contourpy.LineType.Separate, corner_mask=True)
    rows = []
    for k, z in enumerate(lv):
        is_index = bool(interval and index_every and (round((z - base) / interval)) % index_every == 0)
        for pts in gen.lines(z):
            if pts is None or len(pts) < min_points:
                continue
            ls = LineString(pts)
            if ls.is_empty or ls.length == 0:
                continue
            rows.append({"elevation": float(z), "index": is_index, "length": float(ls.length), "geometry": ls})
    if not rows:
        return gpd.GeoDataFrame({"elevation": [], "index": [], "length": []}, geometry=[], crs=crs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


# --------------------------------------------------------------------------------------------------------------- service images

def _image_grid(png_bytes: bytes, bounds, crs, source: dict) -> Grid:
    from PIL import Image
    from rasterio.transform import from_bounds
    im = Image.open(io.BytesIO(png_bytes))
    im = im.convert("RGBA")
    arr = np.asarray(im).transpose(2, 0, 1)       # bands x rows x cols
    x0, y0, x1, y1 = bounds
    t = from_bounds(x0, y0, x1, y1, im.width, im.height)
    alpha = arr[3]
    masked = np.ma.masked_array(arr, mask=np.broadcast_to(alpha == 0, arr.shape))
    return Grid(masked, t, crs_string(crs), tuple(bounds), None, source)


def _http(http=None):
    import ogc_http as oh
    return http or oh.SafeHTTP(limits={"timeout_s": 60, "max_bytes": 50_000_000})      # SafeHTTP itself skips pinning behind a proxy (M12)


def read_wms_image(url: str, layers, bounds, crs: str, size: tuple[int, int] = (1024, 1024), *, http=None, version: str | None = None,
                   fmt: str = "image/png", styles=None, transparent: bool = True, time: str | None = None) -> Grid:
    """A WMS GetMap picture as a Grid (RGBA bands x rows x cols, uint8, transparent pixels masked) on `bounds` in `crs`.
    The request goes through ogc_client.wms_getmap_params (axis order handled for 1.3.0) and SafeHTTP; each side is capped at 1024 px
    by the GetMap helper, so a larger print area is drawn softer or in tiles."""
    import ogc_client as oc
    import ogc_http as oh
    x0, y0, x1, y1 = _bounds_ok(bounds)
    profile = {"base_url": url, "endpoints": {"wms": url}, "protocols": {"wms": version} if version else {}}
    layers = [layers] if isinstance(layers, str) else list(layers)
    crs_name = "CRS:84" if crs_string(crs) == "EPSG:4326" else crs_string(crs)
    params = oc.wms_getmap_params(profile, layers, [x0, y0, x1, y1], width=int(size[0]), height=int(size[1]), fmt=fmt, crs=crs_name, version=version,
                                  styles=styles, transparent=transparent, time=time)
    h = _http(http)
    r = h.get(oc.endpoint(profile, "wms"), params, expect="image")
    src = {"kind": "wms", "url": oh.redact_url(r.url), "layers": layers, "bytes": len(r.content), "sha256": r.sha256, "content_type": r.content_type,
           "width": int(params["width"]), "height": int(params["height"]), "crs": crs_string(crs), "requests": list(h.audit.calls), **({"time": params["time"]} if params.get("time") else {})}
    return _image_grid(r.content, (x0, y0, x1, y1), crs, src)


def read_arcgis_export(url: str, bounds, sr: int = 3857, *, cell: float | None = None, size: tuple[int, int] | None = None, max_px: int = 4096,
                       layers: str | None = None, http=None, out: Path | None = None, transparent: bool = True) -> Grid:
    """A MapServer `export` picture as a Grid, placed on the extent the server says it drew (arcgis_client.export_image)."""
    import arcgis_client as ac
    import tempfile
    x0, y0, x1, y1 = _bounds_ok(bounds)
    base, _, tail = url.rstrip("/").rpartition("/")
    if tail.lower() == "export":
        url = base
    profile = {"base_url": url, "type": "arcgis", "endpoints": {"arcgis": url}}
    h = _http(http)
    tmp = Path(out) if out else Path(tempfile.mkstemp(suffix=".png")[1])
    rec = ac.export_image(h, profile, "", [x0, y0, x1, y1], int(sr), tmp, cell=cell, max_px=max_px, layers=layers, fmt="png32", transparent=transparent, size=size)
    png = tmp.read_bytes()
    if not out:
        tmp.unlink(missing_ok=True)
    rec.update({"kind": "arcgis-export", "requests": list(h.audit.calls)})
    rec.pop("path", None) if not out else None
    return _image_grid(png, tuple(rec["extent"]), f"EPSG:{int(sr)}", rec)


# --------------------------------------------------------------------------------------------------------------- write

def write_geotiff(path, array, transform, crs, nodata=None, dtype=None, compress: str = "deflate", tiled: bool = True, overviews: bool = False) -> dict:
    """Write a 2-D or bands x rows x cols array as a GeoTIFF (deflate, tiled, optional overviews). Masked cells become nodata."""
    rasterio = _rasterio()
    a = np.ma.asarray(array)
    if a.ndim == 2:
        a = a[np.newaxis]
    dt = np.dtype(dtype) if dtype else (np.dtype("float32") if np.issubdtype(a.dtype, np.floating) else a.dtype)
    nd = nodata
    if nd is None and np.ma.getmaskarray(a).any():
        nd = -9999.0 if np.issubdtype(dt, np.floating) else 0
    data = np.ma.filled(a.astype(dt), nd if nd is not None else 0)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    prof = {"driver": "GTiff", "height": int(a.shape[1]), "width": int(a.shape[2]), "count": int(a.shape[0]), "dtype": dt.name, "crs": crs, "transform": transform,
            "nodata": nd, "compress": compress}
    if tiled and a.shape[1] >= 256 and a.shape[2] >= 256:
        prof.update({"tiled": True, "blockxsize": 256, "blockysize": 256})
    with rasterio.open(p, "w", **prof) as dst:
        dst.write(data)
        if overviews:
            from rasterio.enums import Resampling
            facs = [f for f in (2, 4, 8, 16, 32) if a.shape[1] // f >= 64 and a.shape[2] // f >= 64]
            if facs:
                dst.build_overviews(facs, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")
    import hashlib
    return {"path": str(p), "crs": crs_string(crs), "shape": [int(a.shape[1]), int(a.shape[2])], "bands": int(a.shape[0]), "dtype": dt.name, "nodata": _py(nd),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="raster_io: info and stats for a raster")
    ap.add_argument("op", choices=["info", "stats"])
    ap.add_argument("path")
    ap.add_argument("--band", type=int, default=1)
    ap.add_argument("--bounds", nargs=4, type=float)
    ap.add_argument("--crs")
    a = ap.parse_args(argv)
    try:
        if a.op == "info":
            print(json.dumps(open_info(a.path), indent=2, default=str))
        else:
            print(json.dumps(stats(a.path, a.bounds, a.crs, a.band), indent=2, default=str))
    except RasterError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
