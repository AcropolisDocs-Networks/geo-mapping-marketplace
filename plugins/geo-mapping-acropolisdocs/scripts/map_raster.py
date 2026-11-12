#!/usr/bin/env python3
"""map_raster: raster and terrain on the map (M12 integration). The plan-time half of `role: raster`, `role: hillshade`, `role: contour`, the
`terrain:` block (terrain as context) and a WMS or ArcGIS picture drawn as a print layer.

Everything here runs in map_plan.prepare after the extent and the display CRS are known, so the window read is the map's own (the frame
plus a margin, in the display CRS, at the export's pixel size, at most 4096 px a side). It is the only place a build reads the network, and
only for three things, each cached so a second build is offline: a DEM (terrain_fetch.dem_for), a service picture (GetMap or a MapServer
export, tiled into 1,024 px requests for WMS and 2,048 px for ArcGIS), and a population raster named by library id (raster_fetch). With
GMAP_OFFLINE=1 nothing is fetched: a context layer is left out with a note, an explicit layer that is not cached stops the build.

Colours come from the brand: a raster is coloured on the brand's sequential, diverging or categorical tokens; relief is drawn in two brand
neutrals (the darkest and the lightest of the neutral ramp and the page) whose strength follows the slope's departure from flat ground, so
flat land keeps the land colour; contours are thin neutral lines with heavier index lines. Nothing here writes a hex literal.

    import map_raster as mr_
    mr_.normalize(layer_spec)                         # raster.path / raster.ref -> source
    gdf, entry, info = mr_.load_layer(layer_spec, base)   # the footprint (a box in the raster's CRS; empty for a fetched or service raster)
    mr_.prepare(plan)                                 # after the extent: read, colour, place; hillshade to the basemap; contours to line layers
    mr_.web_overlay(rgba, transform, crs)             # the same picture warped to Web Mercator for a Leaflet imageOverlay
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

RASTER_ROLES = ("raster", "hillshade", "contour")
IMAGE_KIND = "raster"                     # LayerPlan.kind of a layer drawn as a picture
PRINT_MAX_PX = 4096                       # the longer side of a print raster window
RELIEF_MAX_PX = 2048                      # relief is soft by nature: 2,048 px over a letter frame is about 200 dpi
CONTOUR_MAX_PX = 1024                     # contours are traced on a coarser grid so they are smooth on the sheet
WEB_MAX_PX = 2048                         # the page carries at most this many pixels a side per overlay
RELIEF_OPACITY = 0.35                     # 30 to 40 percent: relief under data must not compete with it
FLAT_SHADE = math.sin(math.radians(45.0)) # a flat cell's multidirectional hillshade at altitude 45
SERVICE_TILE_PX = {"wms": 1024, "arcgis": 2048}
SERVICE_MAX_TILES = 36
CONTEXT_TIERS = ("regional", "metro", "local")
EXAGGERATION = {"local": 1.5, "metro": 2.0, "regional": 3.0, "continental": 5.0, "world": 8.0}
MIN_DEM_M = 10.0                          # never ask for a DEM finer than 3DEP's 1/3 arc-second
CONTOUR_STEPS = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000)


class RasterLayerError(Exception):
    """A usage or data problem with a raster layer; map_plan turns it into a PlanError."""

    def __init__(self, message: str, code: int = gc.EXIT_USAGE):
        super().__init__(message)
        self.code = code


def offline() -> bool:
    return os.environ.get("GMAP_OFFLINE") == "1"


def _rio():
    import raster_io as rio
    return rio


def finding(fid: str, sev: str, message: str, detail=None, layer: str | None = None) -> dict:
    d = {"id": fid, "severity": sev, "message": message, "detail": detail}
    if layer:
        d["layer"] = layer
    return d


# --------------------------------------------------------------------------------------------------------------- spec
def is_raster_role(role) -> bool:
    return role in RASTER_ROLES


def normalize(ly: dict) -> None:
    """`raster: {path: x.tif}` or `raster: {ref: <library id>}` (the M12 shorthand) becomes the layer's `source`; a `dem:` key on a hillshade or
    contour layer the same. The spec in memory is changed; the file never is."""
    src = ly.get("source") or {}
    if src.get("cds"):                                 # 1.3.0 (R1-CDS): a fetched CDS job's variable and time, as its GeoTIFF and band
        import cds_retrieve as cdr
        c = src["cds"]
        try:
            tif, band = cdr.source_path(str(c.get("job")), str(c.get("variable")), str(c["time"]) if c.get("time") is not None else None)
        except cdr.CDSError as ex:
            raise RasterLayerError(f"layer '{ly.get('id')}': {ex.rule}: {ex}") from None
        ly["source"] = {"type": "file", "path": tif}
        if band:
            ly["raster"] = {**(ly.get("raster") or {}), "band": band}
        rec = next((json.loads(p_.read_text()) for p_ in Path(tif).parent.glob(f"cds-{c.get('job')}.json")), None)
        if rec and not ly.get("provenance"):
            pv = rec.get("provenance") or {}
            ly["provenance"] = {"source": pv.get("source"), "license": "attribution", "attribution": pv.get("attribution"), "retrieved_at": rec.get("retrieved_at"),
                                "library_ref": pv.get("library_ref")}
        return
    if ly.get("source"):
        return
    r = ly.get("raster") or {}
    if r.get("path"):
        ly["source"] = {"type": "file", "path": r["path"]}
    elif r.get("ref"):
        ly["source"] = {"type": "ref", "id": r["ref"]}
    elif ly.get("dem"):
        ly["source"] = {"type": "file", "path": ly["dem"]}
    else:
        raise RasterLayerError(f"layer '{ly.get('id')}': a {ly.get('role')} layer needs a source (source: {{type: file, path: grid.tif}}, raster: {{path: ...}} or a library ref)")


def raster_cfg(ly: dict) -> dict:
    r = dict(ly.get("raster") or {})
    ramp = str(r.get("ramp") or "sequential").lower()
    if ramp not in ("sequential", "diverging", "categorical"):
        raise RasterLayerError(f"layer '{ly['id']}': raster.ramp is sequential, diverging or categorical (the brand's ramps), not '{ramp}'")
    op = float(r.get("opacity", 1.0))
    return {"band": int(r.get("band", 1)), "ramp": ramp, "vmin": r.get("vmin"), "vmax": r.get("vmax"), "classes": r.get("classes"),
            "labels": r.get("labels"), "opacity": max(0.0, min(1.0, op)), "resampling": str(r.get("resampling") or ("nearest" if ramp == "categorical" else "bilinear")),
            "nodata": r.get("nodata"), "midpoint": r.get("midpoint"), "reverse": bool(r.get("reverse")), "unit": r.get("unit"),
            "from_land": r.get("from_land", True) is not False}


def terrain_config(spec: dict) -> dict:
    """The `terrain` block with its defaults; `elements.terrain.on` is the same toggle (every element is individually toggleable)."""
    t = dict(spec.get("terrain") or {})
    el = ((spec.get("elements") or {}).get("terrain") or {})
    on = t.get("on")
    if "on" in el:
        on = el["on"]
    c = dict(t.get("contours") or {})
    return {"on": on, "source": str(t.get("source") or "auto"), "terms_acknowledged": bool(t.get("terms_acknowledged")),
            "opacity": float(t.get("opacity", RELIEF_OPACITY)), "exaggeration": t.get("exaggeration", "auto"),
            "resolution_m": t.get("resolution_m"),
            "contours": {"on": bool(c.get("on", False)), "interval": c.get("interval", "auto"), "index_every": c.get("index_every"),
                         "labels": c.get("labels", True) is not False}}


def terrain_wanted(spec: dict, tier: str | None = None) -> tuple[bool, str, bool]:
    """(drawn, why, explicit). Terrain is off by default. `terrain: {on: true}` turns it on (explicit: a failed fetch is a WARN);
    `context: {preset: full}` (or preset terrain, or context.terrain: true) turns it on for regional, metro and local sheets (a failed fetch is a
    note). `terrain: {on: false}`, `elements.terrain: {on: false}` or `context: {terrain: false}` keep it off whatever the preset says."""
    import map_plan as mp
    cfg = terrain_config(spec)
    ctx = mp.context_config(spec)
    if cfg["on"] is False or ctx.get("terrain") is False:
        return False, "terrain is off", False
    if cfg["on"] is True:
        return True, "terrain: {on: true}", True
    if not ctx["on"]:
        return False, "context is off", False
    asked = ctx["preset"] in ("full", "terrain") or ctx.get("terrain") is True
    if not asked:
        return False, "terrain is off by default", False
    if tier is not None and tier not in CONTEXT_TIERS:
        return False, f"terrain context is drawn on regional, metro and local sheets; this map is at the {tier} tier", False
    return True, f"context preset '{ctx['preset']}'" if ctx.get("terrain") is not True else "context.terrain: true", False


def needs_ground(spec: dict) -> bool:
    """A map with relief needs the land under it (basemap provider none would leave the page bare)."""
    if any((ly or {}).get("role") in ("hillshade", "contour") for ly in spec.get("layers") or []):
        return True
    return terrain_wanted(spec)[0]


def export_dpi(plan) -> float:
    return float((plan.spec.get("export") or {}).get("dpi") or plan.tpl.get("raster_default_dpi") or 300)


# --------------------------------------------------------------------------------------------------------------- loading (before the extent)
def _box_gdf(bounds, crs, lid: str):
    import geopandas as gpd
    from shapely.geometry import box
    return gpd.GeoDataFrame({"layer": [lid]}, geometry=[box(*bounds)], crs=crs)


def _empty_gdf(lid: str):
    import geopandas as gpd
    return gpd.GeoDataFrame({"layer": []}, geometry=[], crs="EPSG:4326")


def is_dem_entry(entry_id: str) -> bool:
    import terrain_fetch as tf
    return entry_id in (tf.THREEDEP_ENTRY, tf.TERRARIUM_ENTRY, tf.COP_ENTRY)


def load_layer(ly: dict, base: Path):
    """(footprint GeoDataFrame, library entry or None, info) for a raster, hillshade or contour layer, before the extent is known.
    info: {src: file | dem | pop | service, path?, open_info?, entry_id?}. A file raster's footprint is its bounds; a fetched DEM, a population
    raster and a service picture have none (they are read for the map's window)."""
    import map_plan as mp
    lid, role = ly["id"], ly["role"]
    src = ly.get("source") or {}
    t = src.get("type")
    if ly.get("ops"):
        raise RasterLayerError(f"layer '{lid}': a {role} layer takes no ops (raster_zonal reads a raster from a polygon layer's ops)")
    if t == "file":
        p = Path(src["path"])
        p = p if p.is_absolute() else base / p
        if not p.is_file():
            raise RasterLayerError(f"layer '{lid}': file not found: {src['path']}")
        rio = _rio()
        try:
            info = rio.open_info(p)
        except rio.RasterError as ex:
            raise RasterLayerError(f"layer '{lid}': {ex}", gc.EXIT_RUNTIME)
        if not info.get("crs"):
            raise RasterLayerError(f"layer '{lid}': {p.name} has no CRS; say which it uses first (gdal_edit -a_srs)", gc.EXIT_LINT)
        return _box_gdf(info["bounds"], info["crs"], lid), None, {"src": "file", "path": str(p), "open_info": info}
    if t == "ref":
        eid = src.get("id")
        e = mp.library_entry(eid)
        if e.get("status") in ("blocked", "retired", "draft"):
            raise RasterLayerError(f"MAP-REF-002: library entry '{eid}' is {e.get('status')}", gc.EXIT_REFUSED)
        if is_dem_entry(eid):
            return _empty_gdf(lid), e, {"src": "dem", "entry_id": eid}
        import raster_fetch as rf
        if rf.known(eid):
            return _empty_gdf(lid), e, {"src": "pop", "entry_id": eid}
        d = e.get("delivery") or {}
        path = None
        if d.get("mode") == "bundled":
            path = (gc.plugin_root() / d["path"]) if e["_scope"] == "shipped" else (gc.workdir() / d["path"])
        elif d.get("mode") == "fetch":
            import ref_fetch
            path = ref_fetch.cached_path(e, src.get("variant"))
        if path is None or not Path(path).is_file():
            raise RasterLayerError(f"layer '{lid}': library entry '{eid}' has no raster on this machine; fetch it first (ref_fetch.py fetch {eid})", gc.EXIT_REFUSED)
        rio = _rio()
        info = rio.open_info(path)
        return _box_gdf(info["bounds"], info["crs"], lid), e, {"src": "file", "path": str(path), "open_info": info}
    if t == "service":
        if role != "raster":
            raise RasterLayerError(f"layer '{lid}': a service picture is drawn with role raster (source: {{type: service, mode: image, ...}})")
        if src.get("mode") == "live":
            raise RasterLayerError(f"layer '{lid}': a live service layer is drawn only in a web page (map_web.py, webmap-create), by the viewer's browser; "
                                   "a printed map draws a picture fetched for its window (MAP-SVC-014): use source mode: image")
        if src.get("mode") != "image":
            raise RasterLayerError(f"layer '{lid}': a raster layer from a service needs source mode: image (a GetMap or MapServer export picture)")
        if not (src.get("url") or src.get("profile")):
            raise RasterLayerError(f"layer '{lid}': a service picture needs source.url (the WMS or MapServer address) or a saved profile")
        if not src.get("layers") and _protocol(src) == "wms":
            raise RasterLayerError(f"layer '{lid}': a WMS picture needs source.layers (the layer names to draw)")
        return _empty_gdf(lid), None, {"src": "service", "protocol": _protocol(src)}
    raise RasterLayerError(f"layer '{lid}': a {role} layer reads a file, a library raster or a service picture, not source type '{t}'")


def _protocol(src: dict) -> str:
    p = str(src.get("protocol") or "").lower()
    if p in ("wms", "arcgis"):
        return p
    u = str(src.get("url") or "").lower()
    return "arcgis" if ("/mapserver" in u or "/imageserver" in u) else "wms"


# --------------------------------------------------------------------------------------------------------------- the window
def window(plan) -> tuple:
    """The display-CRS window every raster of the map is read for: the frame the map will show, plus a tenth on each side."""
    import map_plan as mp
    return tuple(float(v) for v in mp.plan_visible_pad(plan))


def window_shape(plan, win, max_px: int = PRINT_MAX_PX) -> tuple[int, int]:
    """(rows, cols) of the window at the export's dots per inch, the longer side capped at max_px."""
    import layout as lo
    rect = lo.rect_of(plan.tpl["map"])
    fr = lo.Frame.fit(rect, plan.extent, float((plan.spec.get("extent") or {}).get("padding_pct", 5)))
    dpi = export_dpi(plan)
    w_mm, h_mm = (win[2] - win[0]) * fr.scale, (win[3] - win[1]) * fr.scale
    W, H = max(8, int(round(w_mm / 25.4 * dpi))), max(8, int(round(h_mm / 25.4 * dpi)))
    k = max(W, H) / float(max_px)
    if k > 1:
        W, H = max(8, int(round(W / k))), max(8, int(round(H / k)))
    return H, W


def window_wgs84(plan, win) -> tuple:
    from pyproj import Transformer
    t = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    xs = np.linspace(win[0], win[2], 9)
    ys = np.linspace(win[1], win[3], 9)
    X, Y = np.meshgrid(xs, ys)
    lon, lat = t.transform(X.ravel(), Y.ravel())
    ok = np.isfinite(lon) & np.isfinite(lat)
    return (max(-180.0, float(lon[ok].min())), max(-89.9, float(lat[ok].min())), min(180.0, float(lon[ok].max())), min(89.9, float(lat[ok].max())))


def ground_m_per_px(plan, win, shape) -> float:
    """Metres of ground per output pixel at the window's centre (a projected display CRS in metres; Web Mercator scaled by latitude)."""
    wgs = window_wgs84(plan, win)
    lat = (wgs[1] + wgs[3]) / 2
    w_m = (wgs[2] - wgs[0]) * 111_320.0 * math.cos(math.radians(lat))
    return max(w_m / max(shape[1], 1), 0.5)


def native_limited(shape, win, info: dict | None, crs: str, factor: float = 2.0) -> tuple[int, int]:
    """No finer than `factor` times the source's own cells: a 1 km grid is not read at 30 m, which only blurs it and bloats the file."""
    if not info:
        return shape
    try:
        rio = _rio()
        from rasterio.transform import Affine
        t = Affine(*info["transform"])
        lat = rio.centre_lat(t, (info["size"][1], info["size"][0]), info["crs"])
        dx, _ = rio.cell_size_m(t, info["crs"], lat)
        from pyproj import CRS
        c = CRS.from_user_input(crs)
        unit_m = 1.0
        if c.is_geographic:
            unit_m = 111_320.0 * math.cos(math.radians(lat or 0.0))
        win_w_m = (win[2] - win[0]) * unit_m
        native_cols = max(8, int(win_w_m / max(dx, 1e-9) * factor))
    except Exception:  # noqa: BLE001 - a CRS without metres keeps the export shape
        return shape
    if native_cols >= shape[1]:
        return shape
    k = native_cols / shape[1]
    return max(8, int(round(shape[0] * k))), native_cols


# --------------------------------------------------------------------------------------------------------------- pictures
def png_bytes(rgba: np.ndarray) -> bytes:
    """bands x rows x cols uint8 RGBA -> PNG bytes."""
    from PIL import Image
    a = np.ascontiguousarray(np.moveaxis(np.asarray(rgba, dtype=np.uint8), 0, -1))
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def png_rgba(data: bytes) -> np.ndarray:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    return np.moveaxis(np.asarray(im, dtype=np.uint8), -1, 0).copy()


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _hex_rgb(h: str) -> tuple[int, int, int]:
    s = str(h).lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _lum(h: str) -> float:
    r, g, b = _hex_rgb(h)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def relief_colors(tokens: dict) -> tuple[str, str]:
    """(shadow, light): the darkest and the lightest of the brand's neutrals (the neutral ramp, the page and the text colour). In the light theme
    that is the text's near-black and the page white; in the dark theme the page's near-black and the ramp's lightest grey."""
    cands = list(tokens.get("neutral_ramp") or []) + [tokens["background"], tokens["label"]]
    return min(cands, key=_lum), max(cands, key=_lum)


RELIEF_STRETCH_MIN = 0.10      # the shade departure that reaches full strength is the window's 98th percentile, never less than this


def relief_rgba(hs: np.ndarray, tokens: dict) -> np.ndarray:
    """Hillshade (0..1, nan = nodata) to RGBA in two brand neutrals: shadow where the slope faces away from the light, light where it faces it,
    transparent on flat ground (so the land colour shows unchanged) and on nodata. Strength grows with the departure from a flat cell's shade,
    stretched to the window (the 98th percentile is full strength) so gentle and steep country both read; the image's own opacity (30 to 40
    percent) keeps the strongest shadow well under the data."""
    shadow, light = relief_colors(tokens)
    sr, sg, sb = _hex_rgb(shadow)
    lr, lg_, lb = _hex_rgb(light)
    h = np.asarray(hs, dtype=float)
    ok = np.isfinite(h)
    d = np.where(ok, h, FLAT_SHADE) - FLAT_SHADE
    s = max(float(np.nanpercentile(np.abs(d[ok]), 98)) if ok.any() else 0.0, RELIEF_STRETCH_MIN)
    a_dark = np.clip(-d / s, 0.0, 1.0) ** 0.8
    a_light = np.clip(d / s, 0.0, 1.0) ** 0.8 * 0.6             # highlights are softer than shadows: light from the north-west reads as relief
    dark = d < 0
    out = np.zeros((4,) + h.shape, np.uint8)
    out[0] = np.where(dark, sr, lr)
    out[1] = np.where(dark, sg, lg_)
    out[2] = np.where(dark, sb, lb)
    out[3] = np.where(ok, np.round(255 * np.where(dark, a_dark, a_light)), 0).astype(np.uint8)
    return out


def _scale_alpha(png: bytes, factor) -> bytes:
    """The picture with its alpha multiplied by `factor` (an array the picture's shape, nan = keep), for value by alpha (0.20.0)."""
    if factor is None:
        return png
    rgba = png_rgba(png)
    f = np.asarray(factor, dtype=float)
    if f.shape != rgba.shape[1:]:
        return png
    f = np.where(np.isfinite(f), np.clip(f, 0.0, 1.0), 1.0)
    rgba = rgba.copy()
    rgba[3] = np.round(rgba[3].astype(float) * f).astype(np.uint8)
    return png_bytes(rgba)


def tinted_relief(shade: np.ndarray, dem, tokens: dict) -> np.ndarray:
    """0.20.0 soft relief: an elevation tint (the brand's neutral ramp from its lightest step on low ground to its third on the highest, its
    strength growing with height, map_look.RELIEF_TINT_ALPHA at the top) with the hillshade laid over it a little stronger
    (map_look.RELIEF_SHADE_GAIN). One RGBA picture, alpha-composited here: the PDF route has no blend modes."""
    import map_look
    nr = list(tokens.get("neutral_ramp") or [])
    if len(nr) < 3:
        return shade
    lo_c, hi_c = np.array(_hex_rgb(nr[0]), float), np.array(_hex_rgb(nr[2]), float)
    z = np.ma.filled(np.ma.asarray(dem, dtype=float), np.nan)
    ok = np.isfinite(z)
    if not ok.any() or z.shape != shade.shape[1:]:
        return shade
    p2, p98 = np.nanpercentile(z[ok], 2), np.nanpercentile(z[ok], 98)
    tz = np.where(ok, np.clip((z - p2) / ((p98 - p2) or 1.0), 0.0, 1.0), 0.0)        # nodata: no tint (and no nan in the sums)
    tint_rgb = lo_c[:, None, None] * (1 - tz) + hi_c[:, None, None] * tz
    tint_a = np.where(ok, map_look.RELIEF_TINT_ALPHA * tz, 0.0)
    sa = np.clip(shade[3].astype(float) / 255.0 * map_look.RELIEF_SHADE_GAIN, 0.0, 1.0)
    out_a = sa + tint_a * (1 - sa)
    rgb = (shade[:3].astype(float) * sa + tint_rgb * tint_a * (1 - sa)) / np.where(out_a > 0, out_a, 1.0)
    out = np.zeros_like(shade)
    out[:3] = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    out[3] = np.clip(np.round(out_a * 255), 0, 255).astype(np.uint8)
    return out


def web_overlay(rgba: np.ndarray, transform, crs, max_px: int = WEB_MAX_PX) -> dict:
    """The picture warped to Web Mercator for a Leaflet imageOverlay: {url (data URI), bounds [[south, west], [north, east]], width, height,
    downsampled}. The colours are the print picture's own; only the grid changes."""
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.transform import array_bounds
    from pyproj import Transformer
    from rasterio.transform import from_bounds
    bands, H, W = rgba.shape
    left, bottom, right, top = array_bounds(H, W, transform)
    dst_t, dw, dh = calculate_default_transform(crs, "EPSG:3857", W, H, left, bottom, right, top)
    down = False
    k = max(dw, dh) / float(max_px)
    if k > 1:
        down = True
        b = array_bounds(dh, dw, dst_t)
        dw, dh = max(8, int(round(dw / k))), max(8, int(round(dh / k)))
        dst_t = from_bounds(b[0], b[1], b[2], b[3], dw, dh)
    dst = np.zeros((4, dh, dw), np.uint8)
    for i in range(4):
        reproject(rgba[i], dst[i], src_transform=transform, src_crs=crs, dst_transform=dst_t, dst_crs="EPSG:3857",
                  resampling=Resampling.bilinear if i < 3 else Resampling.nearest)
    mb = array_bounds(dh, dw, dst_t)
    tr = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    w_, s_ = tr.transform(mb[0], mb[1])
    e_, n_ = tr.transform(mb[2], mb[3])
    return {"url": data_uri(png_bytes(dst)), "bounds": [[round(s_, 6), round(w_, 6)], [round(n_, 6), round(e_, 6)]], "width": int(dw), "height": int(dh), "downsampled": down}


def grid_transform(win, shape):
    from rasterio.transform import from_bounds
    return from_bounds(win[0], win[1], win[2], win[3], shape[1], shape[0])


# --------------------------------------------------------------------------------------------------------------- the plan step
def prepare(plan) -> None:
    """After the extent: every raster, hillshade and contour layer is read for the window, coloured and placed; then terrain as context."""
    import map_plan as mp
    todo = [l for l in plan.layers if l.role in RASTER_ROLES]
    tier = mp.scale_tier(mp.scale_denominator(plan))["tier"]
    want, why, explicit = terrain_wanted(plan.spec, tier)
    if not todo and not want:
        why_off = terrain_wanted(plan.spec, tier)[1]
        if "tier" in why_off:
            plan.notes.append(f"terrain: not drawn ({why_off})")
        return
    win = window(plan)
    shape = window_shape(plan, win)
    for l in todo:
        if l.role == "raster":
            raster_layer(plan, l, win, shape)
        elif l.role == "hillshade":
            hillshade_layer(plan, l, win, shape)
        else:
            contour_layer(plan, l, win, shape)
    if want:
        terrain_context(plan, win, shape, why, explicit, tier)
    elif "tier" in why:
        plan.notes.append(f"terrain: not drawn ({why})")


def _source_path(plan, l, win, shape=(512, 512)) -> tuple[str, dict | None, list]:
    """(local path, fetch record or None, findings) for a raster-role layer: its file, the population raster fetched for the window, or the DEM."""
    info = l.raster or {}
    src = info.get("src")
    if src == "file":
        return info["path"], None, []
    if src == "pop":
        import raster_fetch as rf
        try:
            r = rf.raster_for(info["entry_id"], window_wgs84(plan, win))
        except Exception as ex:  # noqa: BLE001 - RasterFetchError, or a service failure under it
            raise RasterLayerError(f"layer '{l.id}': {ex}", gc.EXIT_RUNTIME)
        return r["path"], r["record"], r["findings"]
    if src == "dem":
        import terrain_fetch as tf
        cfg = terrain_config(plan.spec)
        source = {tf.THREEDEP_ENTRY: "3dep", tf.COP_ENTRY: "copernicus"}.get(info["entry_id"], "terrarium")
        r = fetch_dem(plan, win, shape, source, cfg["terms_acknowledged"] or bool((l.spec.get("source") or {}).get("terms_acknowledged")), cfg.get("resolution_m"),
                      max_px=PRINT_MAX_PX // 2)
        if not r["ok"]:
            raise RasterLayerError(f"layer '{l.id}': " + "; ".join(f["message"] for f in r["findings"] if f["severity"] == "BLOCK"), gc.EXIT_REFUSED)
        return r["path"], r["record"], r["findings"]
    raise RasterLayerError(f"layer '{l.id}': no raster source")


def raster_layer(plan, l, win, shape) -> None:
    """`role: raster`: the window read at the export's pixel size, coloured on the brand's ramp, placed by its box; legend from the entries."""
    if (l.raster or {}).get("src") == "service":
        return service_layer(plan, l, win, shape)
    import classify as cf
    import legend as lg
    rio = _rio()
    t = plan.tokens
    cfg = raster_cfg(l.spec)
    path, fetch_rec, fs = _source_path(plan, l, win, shape)
    plan.findings += [{**f, "layer": l.id} for f in fs]
    info = rio.open_info(path)
    shp = native_limited(shape, win, info, plan.crs)
    try:
        g = rio.read_window(path, win, plan.crs, out_shape=shp, band=cfg["band"], resampling=cfg["resampling"], nodata=cfg["nodata"])
    except rio.RasterError as ex:
        raise RasterLayerError(f"layer '{l.id}': {ex}", gc.EXIT_RUNTIME)
    a = np.ma.masked_invalid(np.ma.asarray(g.array, dtype=float))
    if cfg["nodata"] is not None:
        a = np.ma.masked_where(np.ma.filled(a, np.nan) == float(cfg["nodata"]), a)
    if a.count() == 0:
        raise RasterLayerError(f"layer '{l.id}': the raster holds no value inside the map window (it lies elsewhere, or it is all nodata)")
    alpha = 255
    title = lg._layer_title(l.spec)
    unit = cfg["unit"]
    legend_title = title + (f" ({unit})" if unit and unit not in title else "")
    lspec = l.spec | {"title": legend_title, "legend": {**(l.spec.get("legend") or {}), "title": legend_title}}
    if cfg["ramp"] == "categorical":
        vals = np.unique(a.compressed())
        if len(vals) > len(t["categorical"]):
            raise RasterLayerError(f"layer '{l.id}': {len(vals)} distinct values is more than the brand's {len(t['categorical'])} categorical colours; "
                                   "classify it (raster.classes) on the sequential ramp instead")
        import map_look
        colors = map_look.categorical_pick(t, len(vals))
        breaks = [float(x + y) / 2 for x, y in zip(vals, vals[1:])]
        labels_map = {str(k): str(v) for k, v in (cfg["labels"] or {}).items()} if isinstance(cfg["labels"], dict) else {}
        labels = [labels_map.get(str(_num(v)), str(_num(v))) for v in vals]
        if len(vals) == 1:
            png, entries = rio.to_png(a, [colors[0], colors[0]], vmin=float(vals[0]), vmax=float(vals[0]) + 1, alpha=alpha)
        else:
            png, entries = rio.to_png(a, colors, classes=breaks, class_labels=labels, alpha=alpha)
        items = [{"label": lab, "fill": col, "stroke": t["legend_frame"], "opacity": cfg["opacity"]} for lab, col in zip(labels, colors)]
        l.legend_block = {"layer": l.id, "title": legend_title, "kind": "categories", "items": items, "note": (l.spec.get("legend") or {}).get("note")}
        l.colors, l.palette_kind = colors, "categorical"
        how = f"{len(vals)} categories in the brand's categorical colours"
    elif cfg["classes"]:
        breaks, ends = class_edges(cfg["classes"], a)
        pk = "diverging" if cfg["ramp"] == "diverging" else "sequential"
        colors = cf.palette(t, pk, len(breaks) + 1, cfg["reverse"])["colors"]
        labels = list(cfg["labels"]) if isinstance(cfg["labels"], list) and len(cfg["labels"]) == len(breaks) + 1 else None
        if labels is None and isinstance(cfg["labels"], list):
            raise RasterLayerError(f"layer '{l.id}': raster.labels has {len(cfg['labels'])} entries but raster.classes makes {len(breaks) + 1} classes"
                                   + (" (the first and last values given are the data's own ends, so they are class edges)" if any(ends) else ""))
        if labels is None and any(ends):                  # 1.2.0 (Day 8 issue 5): edges given at the data's ends label the end classes as ranges
            given = sorted(float(x) for x in cfg["classes"])
            lo_e, hi_e = (given[0] if ends[0] else None), (given[-1] if ends[1] else None)
            labels = _edge_labels(breaks, lo_e, hi_e, _fmt)
        png, entries = rio.to_png(a, colors, classes=breaks, class_labels=labels, alpha=alpha, fmt=_fmt)
        cal = None
        if getattr(plan, "look", "classic") != "classic" and pk == "sequential":        # 0.20.0: value by alpha, class by class
            import map_look
            cal = map_look.class_alphas(len(breaks) + 1)
            png = _scale_alpha(png, np.asarray(cal)[np.digitize(np.ma.filled(a, np.nan), breaks)] if cal else None)
        items = [{"label": e["label"], "fill": col, "stroke": t["legend_frame"], "opacity": cfg["opacity"] * (cal[i] if cal else 1.0)}
                 for i, (e, col) in enumerate(zip(entries, colors))]   # the token, not the LUT's rounding
        l.legend_block = {"layer": l.id, "title": legend_title, "kind": "classes", "items": items, "note": (l.spec.get("legend") or {}).get("note")}
        l.colors, l.palette_kind = colors, pk
        how = f"{len(breaks) + 1} classes at {', '.join(_fmt(b) for b in breaks)} on the brand's {pk} ramp"
    else:
        pk = cfg["ramp"]
        ramp = list(t["diverging_ramp" if pk == "diverging" else "sequential_ramp"])
        if pk == "sequential" and cfg["from_land"]:
            ramp = [t["land"]] + ramp                     # a continuous surface starts at the land colour, so its low values fade into the ground
        if cfg["reverse"]:
            ramp = ramp[::-1]
        vmin, vmax = cfg["vmin"], cfg["vmax"]
        data = a.compressed()
        lo = float(vmin) if vmin is not None else float(np.percentile(data, 2))
        hi = float(vmax) if vmax is not None else float(np.percentile(data, 98))
        if pk == "diverging":
            mid = float(cfg["midpoint"]) if cfg["midpoint"] is not None else (0.0 if data.min() < 0 < data.max() else float(np.median(data)))
            r = max(abs(lo - mid), abs(hi - mid), 1e-9)
            if cfg["vmin"] is None and cfg["vmax"] is None:
                r = nice_round(r, "up")
                lo, hi = mid - r, mid + r
        else:
            if cfg["vmin"] is None:
                lo = 0.0 if 0 <= lo < 0.02 * max(hi - lo, 1e-9) else nice_round(lo, "down")
            if cfg["vmax"] is None:
                hi = nice_round(hi, "nearest")
            if hi <= lo:
                hi = lo + 1.0
        png, entries = rio.to_png(a, ramp, vmin=lo, vmax=hi, alpha=alpha, fmt=_fmt)
        vba = getattr(plan, "look", "classic") != "classic" and pk == "sequential" and not cfg["reverse"]
        if vba:                                       # 0.20.0: value by alpha: the low end lets the ground show, the high values stand out
            import map_look
            png = _scale_alpha(png, map_look.surface_alpha((np.ma.filled(a, np.nan) - lo) / ((hi - lo) or 1.0)))
        lo_v, hi_v = float(entries[0]["value"]), float(entries[-1]["value"])
        entries[0]["label"] = _fmt(lo_v) + (" or less" if float(data.min()) < lo_v - 1e-9 else "")
        entries[-1]["label"] = _fmt(hi_v) + (" or more" if float(data.max()) > hi_v + 1e-9 else "")
        stops = [{"label": e["label"], "value": float(e["value"]), "pos": (float(e["value"]) - lo_v) / ((hi_v - lo_v) or 1.0)} for e in entries]
        if len(stops) == 5:                               # a bar reads best with its two ends and its middle labelled
            stops = [stops[0], stops[2], stops[4]]
        l.legend_block = lg.ramp_block(lspec, stops, ramp, t, opacity=cfg["opacity"], note=(l.spec.get("legend") or {}).get("note"))
        if vba:
            import map_look
            l.legend_block["alpha_full_at"] = map_look.SURFACE_FULL_AT
        l.colors, l.palette_kind = ramp, pk
        how = f"continuous on the brand's {pk} ramp from {_fmt(lo_v)} to {_fmt(hi_v)}" + (" (vmin and vmax as given)" if cfg["vmin"] is not None or cfg["vmax"] is not None else " (the 2nd to 98th percentile of the window)")
    if (l.spec.get("legend") or {}).get("include") is False:
        l.legend_block = None
    rgba = png_rgba(png)
    l.raster.update({"image": {"png": png, "rgba": rgba, "bbox": list(g.bounds), "transform": g.transform, "crs": plan.crs, "opacity": cfg["opacity"],
                               "shape": [int(rgba.shape[1]), int(rgba.shape[2])]},
                     "path": path, "band": cfg["band"], "resampling": cfg["resampling"]})
    src_res = info["res"]
    l.transformations.append(f"band {cfg['band']} of {Path(path).name} ({info['crs']}, cells of {src_res[0]:g} x {src_res[1]:g} units) read through a warped window in "
                             f"{plan.crs} at {rgba.shape[2]} x {rgba.shape[1]} px ({cfg['resampling']}); coloured {how}; nodata transparent")
    l.src_file, l.src_sha256 = Path(path).name, _sha(path)
    rec = {"path": Path(path).name, "sha256": l.src_sha256, "band": cfg["band"], "resampling": cfg["resampling"], "window": [round(v, 3) for v in g.bounds],
           "window_crs": plan.crs, "pixels": [int(rgba.shape[2]), int(rgba.shape[1])], "source_crs": info["crs"], "source_res": src_res, "nodata": info.get("nodata"),
           "colouring": how, "opacity": cfg["opacity"]}
    if fetch_rec:
        rec["fetch"] = {k: v for k, v in fetch_rec.items() if k not in ("files",)} | {"files": [{k: v for k, v in f.items()} for f in fetch_rec.get("files", [])]}
        l.transformations.append(_fetch_step(fetch_rec))
    _finish(plan, l, rec)


def _fetch_step(rec: dict) -> str:
    if rec.get("tiles"):
        what = f"GHSL tile(s) {', '.join(rec['tiles'])}"
    elif rec.get("country"):
        what = f"the WorldPop file for {rec['country']}"
    else:
        what = "the raster"
    how = "read from the cache" if rec.get("cached") else f"fetched {rec.get('retrieved_at')} ({rec.get('request_count', 0)} request(s), {rec.get('request_bytes', 0) / 1e6:.1f} MB)"
    return f"{what} {how}; sha256 {str(rec.get('sha256'))[:16]}"


def _finish(plan, l, rec: dict) -> None:
    import map_plan as mp
    l.bbox = [round(v, 5) for v in window_wgs84(plan, rec.get("window") or window(plan))]
    l.provenance = mp.layer_provenance(l)
    l.provenance["raster"] = rec
    l.raster["record"] = rec


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def nice_round(v: float, how: str = "nearest") -> float:
    """v rounded to two significant figures (up, down or to the nearest): a ramp's ends read 2,500 and 0, not 2,501 and 0.00294."""
    if v == 0 or not math.isfinite(v):
        return 0.0
    e = math.floor(math.log10(abs(v))) - 1
    q = 10.0 ** e
    f = {"up": math.ceil, "down": math.floor}.get(how, round)
    return float(f(v / q) * q)


def _fmt(v: float) -> str:
    v = float(v)
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    return f"{v:,.3g}"


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------------------------------- DEM and relief
def fetch_dem(plan, win, shape, source: str, terms: bool, resolution_m=None, max_px: int = RELIEF_MAX_PX) -> dict:
    """terrain_fetch.dem_for for the map's window at the ground size of a relief pixel (the print shape capped at max_px; never finer than 10 m).
    Terrarium without the terms acknowledged falls back to 3DEP inside the United States. -> dem_for's result."""
    import terrain_fetch as tf
    wgs = window_wgs84(plan, win)
    res = float(resolution_m) if resolution_m else max(MIN_DEM_M, ground_m_per_px(plan, win, _cap(shape, max_px)))
    max_px = int(max_px * 1.25)                       # the lon/lat box is a little wider than the window: room for it, so the cap is not hit
    fs: list[dict] = []
    src = source                                     # 0.23.0: Terrarium needs no acknowledgment; its credit is printed with the map
    if offline():
        cache = _dem_cached(wgs, res, src)
        if cache:
            return cache
        return {"ok": False, "findings": fs + [finding("MAP-TER-004", "INFO", "offline (GMAP_OFFLINE=1): no DEM is fetched and none is cached for this window")], "offline": True}
    r = tf.dem_for(wgs, res, src, gc.workdir(create=True), terms_acknowledged=terms, max_px=max_px, surface_ok=True)   # relief: 1.5.0 Copernicus outside the US
    r["findings"] = fs + list(r.get("findings") or [])
    return r


def _dem_cached(wgs, res, src):
    import terrain_fetch as tf
    for s in ([src] if src != "auto" else (["3dep"] if tf.in_us(wgs) else ["copernicus", "terrarium"])):
        out = tf.cache_root(gc.workdir()) / s / f"{tf.cache_key(s, wgs, res)}.tif"
        if out.is_file() and out.with_suffix(".json").is_file():
            rec = json.loads(out.with_suffix(".json").read_text())
            rec["cached"], rec["requests"] = True, []
            return {"ok": True, "findings": [], "path": str(out), "record": rec}
    return None


def dem_from_config(plan, win, shape, cfg: dict, explicit: bool) -> tuple[dict | None, list[dict]]:
    """The DEM the terrain block names: a path (nothing fetched) or a source to fetch. -> (result or None, findings). Never raises: terrain is context."""
    import terrain_fetch as tf
    src = cfg["source"]
    sev = "WARN" if explicit else "INFO"
    if src not in ("auto", "3dep", "terrarium", "copernicus"):
        p = Path(src)
        p = p if p.is_absolute() else plan.spec_path.parent / p
        if not p.is_file():
            return None, [finding("MAP-TER-004", sev, f"terrain.source {src} is not a file; the map was built without relief")]
        try:
            return tf.dem_for(None, dem_path=str(p)), []
        except Exception as ex:  # noqa: BLE001
            return None, [finding("MAP-TER-004", sev, f"terrain.source {p.name} could not be read ({ex}); the map was built without relief")]
    try:
        r = fetch_dem(plan, win, shape, src, cfg["terms_acknowledged"], cfg.get("resolution_m"))
    except Exception as ex:  # noqa: BLE001 - never block a map on a context layer
        import ogc_http as oh
        msg = oh.untrusted(ex, 200)
        return None, [finding("MAP-TER-004", sev, f"the DEM could not be fetched ({msg}); the map was built without relief")]
    if not r.get("ok"):
        fs = []
        for f in r.get("findings") or []:
            if f["severity"] == "BLOCK":                                  # a refusal (terms, 3DEP outside the US) becomes a WARN or a note: the map still builds
                fs.append({**f, "severity": sev, "message": f["message"] + "; the map was built without relief"})
            else:
                fs.append(f)
        return None, fs
    return r, list(r.get("findings") or [])


def exaggeration(cfg_value, tier: str) -> float:
    if cfg_value in (None, "auto"):
        return EXAGGERATION.get(tier, 1.0)
    return max(0.1, float(cfg_value))


def apply_dem_provenance(item: dict, prov: dict) -> None:
    """A person's DEM credited as they say: `provenance: {source | product, publisher, license, attribution}` on a hillshade layer or on the
    terrain block (1.2.0 for the terrain block). The record, the methods note and the consolidated source line read it."""
    prov = dict(prov or {})
    pub = prov.get("publisher")
    pub_name = pub.get("name") if isinstance(pub, dict) else pub
    product = (pub.get("product") if isinstance(pub, dict) else None) or prov.get("product") or prov.get("source")
    item["record"].update({k: v for k, v in {"source_name": prov.get("source") or product, "license": prov.get("license"),
                                              "attribution_text": prov.get("attribution")}.items() if v})
    ent = dict(item.get("entry") or {})
    if prov.get("attribution"):
        ent["license"] = dict(ent.get("license") or {}, attribution_text=prov["attribution"])
    if pub_name or product:
        ent["source"] = dict(ent.get("source") or {}, **({"publisher": pub_name} if pub_name else {}), **({"product": product} if product else {}))
        if pub_name:
            ent["title"] = pub_name
    item["entry"] = ent


def relief_item(plan, dem_path: str, rec: dict, win, shape, *, opacity: float, z: float, layer_id: str, entry: dict | None, title: str) -> dict:
    """A plan.basemap item {kind: hillshade}: the DEM read for the window (at most 2,048 px), multidirectional hillshade, drawn in brand neutrals."""
    import terrain_fetch as tf
    rio = _rio()
    target = window_shape(plan, win, RELIEF_MAX_PX) if shape is None else _cap(shape, RELIEF_MAX_PX)
    native = native_limited(target, win, rio.open_info(dem_path), plan.crs, factor=1.0)
    g = rio.read_window(dem_path, win, plan.crs, out_shape=native, resampling="bilinear")      # shaded on the DEM's own cells: no interpolation facets
    dem = np.ma.masked_invalid(np.ma.asarray(g.array, dtype=float))
    dem = np.ma.masked_where(np.ma.filled(dem, np.nan) <= tf.NODATA + 1, dem)
    if dem.count() == 0:
        raise RasterLayerError("the DEM holds no elevation inside the map window")
    hs = rio.hillshade(dem, g.transform, z_factor=z, multidirectional=True, crs=g.crs)
    hs[_grow(np.ma.getmaskarray(dem))] = np.nan       # a cell beside nodata has no true slope: no line along the DEM's edge
    rgba = relief_rgba(hs, plan.tokens)
    if getattr(plan, "look", "classic") != "classic":
        rgba = tinted_relief(rgba, dem, plan.tokens)      # 0.20.0: an elevation tint in the brand's neutrals under a slightly stronger shading
    valid = ~np.ma.getmaskarray(dem)
    if not valid.all() and valid.any():               # 0.26.0 (TODO M16): a DEM that ends inside the map fades out instead of stopping in a straight edge
        rgba, faded = feather_edge(rgba, valid)
        if faded and hasattr(plan, "notes"):
            plan.notes.append(f"relief: the DEM ends inside the map window ({1 - valid.mean():.0%} of it has no elevation); the shading fades out over "
                              f"{faded} px at its edge rather than stopping in a straight line")
    if native[1] < target[1] * 0.8:                   # then the shading (not the DEM) is enlarged smoothly to the print size
        rgba = _resize_rgba(rgba, target)
    png = png_bytes(rgba)
    attribution = rec.get("attribution")
    src_name = {"3dep": "USGS 3D Elevation Program (3DEP) bare-earth DEM", "terrarium": "Terrain Tiles on AWS (Mapzen Terrarium)",
                "copernicus": "Copernicus DEM GLO-30 (surface model)"}.get(rec.get("source"), f"DEM supplied by the person ({Path(dem_path).name})")
    step = (f"multidirectional hillshade (225, 270, 315 and 360 degrees, Horn method) of the {src_name} read for the map window at "
            f"{rgba.shape[2]} x {rgba.shape[1]} px in {plan.crs}, vertical exaggeration {z:g}; drawn in two brand neutrals at {opacity:.0%} opacity above the land "
            f"and under everything else; never in the legend or the extent")
    pub = tf.public_record(rec)
    entry_rec = entry or {"id": f"user.terrain.{layer_id}", "title": src_name, "rev": None, "source": {}, "license": {"class": rec.get("license") or "user-supplied"}}
    if attribution:
        entry_rec = dict(entry_rec, license=dict(entry_rec.get("license") or {}, attribution_text=attribution))
    record = {"layer_id": f"relief:{layer_id}", "source_name": src_name, "source_url": (entry_rec.get("source") or {}).get("url"),
              "retrieved_at": str(rec.get("retrieved_at") or "unknown"), "license": str((entry_rec.get("license") or {}).get("class") or rec.get("license") or "unknown"),
              "attribution_text": attribution, "crs_original": str(rec.get("crs") or "unknown"), "crs_used": plan.crs,
              "transformations": [step] + ([f"DEM fetched {rec.get('retrieved_at')}: {pub.get('request_count', 0)} request(s), {pub.get('request_bytes', 0) / 1e6:.1f} MB"
                                            + (" (from the cache)" if rec.get("cached") else "")] if rec.get("source") in ("3dep", "terrarium", "copernicus") else
                                           [f"a DEM supplied by the person ({Path(dem_path).name}); nothing was fetched"]),
              "row_count": 0, "library_ref": entry_rec.get("id") if entry else None, "library_rev": entry_rec.get("rev") if entry else None,
              "sha256": rec.get("sha256") or _sha(dem_path), "terrain": pub}
    return {"kind": "hillshade", "gdf": None, "entry": entry_rec, "layer_id": layer_id, "title": title,
            "image": {"png": png, "rgba": rgba, "bbox": list(g.bounds), "transform": g.transform, "crs": plan.crs, "opacity": opacity, "shape": [int(rgba.shape[1]), int(rgba.shape[2])]},
            "record": record, "dem_path": dem_path, "z": z}


RELIEF_FEATHER_FRAC = 0.035        # 0.26.0: the fade at a DEM's edge inside the map, as a share of the picture's longer side


def feather_edge(rgba: np.ndarray, valid: np.ndarray, frac: float = RELIEF_FEATHER_FRAC) -> tuple[np.ndarray, int]:
    """The relief picture (4 x rows x cols) with its alpha eased to 0 over the last `frac` of the picture's longer side inside the DEM's valid
    cells, so the shading ends softly where the DEM ends. Returns (picture, fade width in px; 0 when nothing was faded)."""
    v = np.asarray(valid, bool)
    px = max(2, int(round(frac * max(v.shape))))
    try:
        from scipy.ndimage import distance_transform_edt
        dist = distance_transform_edt(v)
    except ImportError:                               # without scipy: city-block steps, at most 24 of them
        px = min(px, 24)
        dist = np.zeros(v.shape, float)
        m = ~v
        for k in range(1, px + 1):
            m = _grow(m)
            dist[(~m) & (dist == 0)] = k
        dist[(~m)] = px
    f = np.clip(dist / px, 0.0, 1.0)
    f = f * f * (3 - 2 * f)                           # smoothstep: no visible ring where the fade begins
    out = rgba.copy()
    out[3] = np.round(out[3].astype(float) * f).astype(np.uint8)
    return out, px


def _grow(mask: np.ndarray, n: int = 1) -> np.ndarray:
    """The mask grown by n cells in the 8 directions."""
    m = np.asarray(mask, bool).copy()
    for _ in range(n):
        p = np.pad(m, 1, mode="edge")
        m = (p[:-2, :-2] | p[:-2, 1:-1] | p[:-2, 2:] | p[1:-1, :-2] | p[1:-1, 1:-1] | p[1:-1, 2:] | p[2:, :-2] | p[2:, 1:-1] | p[2:, 2:])
    return m


def _resize_rgba(rgba: np.ndarray, shape) -> np.ndarray:
    """bands x rows x cols RGBA resized to (rows, cols) with a bicubic filter on premultiplied colour (so transparent edges do not darken)."""
    from PIL import Image
    a = np.moveaxis(rgba, 0, -1).astype(np.float32)
    al = a[..., 3:4] / 255.0
    pm = np.concatenate([a[..., :3] * al, a[..., 3:4]], axis=-1)
    chans = [np.asarray(Image.fromarray(pm[..., i]).resize((shape[1], shape[0]), Image.BICUBIC)) for i in range(4)]
    out = np.stack(chans, axis=-1)
    alpha = np.clip(out[..., 3], 0, 255)
    col = np.where(alpha[..., None] > 0, out[..., :3] / np.maximum(alpha[..., None] / 255.0, 1e-6), 0)
    res = np.concatenate([np.clip(col, 0, 255), alpha[..., None]], axis=-1).round().astype(np.uint8)
    return np.moveaxis(res, -1, 0)


def _cap(shape, mx):
    h, w = shape
    k = max(h, w) / float(mx)
    if k <= 1:
        return shape
    return max(8, int(round(h / k))), max(8, int(round(w / k)))


def insert_relief(plan, item: dict) -> None:
    """Above the land, the countries and the states; under the lakes (water is flat) and under every layer."""
    i = next((k for k, b in enumerate(plan.basemap) if b.get("kind") in ("lake", "ocean")), len(plan.basemap))
    plan.basemap.insert(i, item)


def hillshade_layer(plan, l, win, shape) -> None:
    """`role: hillshade`: relief from the layer's DEM (a file: no network; a 3DEP or Terrarium library ref: fetched for the window). The layer moves
    to the basemap: drawn above the land and under everything else, never in the legend or the extent."""
    import map_plan as mp
    import terrain_fetch as tf
    cfg = terrain_config(plan.spec)
    hs = dict(l.spec.get("hillshade") or {})
    opacity = float(hs.get("opacity", (l.spec.get("symbol") or {}).get("opacity", cfg["opacity"])))
    tier = mp.scale_tier(mp.scale_denominator(plan))["tier"]
    z = exaggeration(hs.get("exaggeration", cfg["exaggeration"]), tier)
    plan.layers.remove(l)
    info = l.raster or {}
    try:
        if info.get("src") == "file":
            r = tf.dem_for(None, dem_path=info["path"])
        else:
            source = {tf.THREEDEP_ENTRY: "3dep", tf.COP_ENTRY: "copernicus"}.get(info.get("entry_id"), "terrarium")
            terms = cfg["terms_acknowledged"] or bool((l.spec.get("source") or {}).get("terms_acknowledged"))
            r = fetch_dem(plan, win, shape, source, terms, cfg.get("resolution_m"))
            if not r.get("ok"):
                fs = [{**f, "severity": "WARN" if f["severity"] == "BLOCK" else f["severity"], "layer": l.id,
                       "message": f["message"] + ("; the map was built without this hillshade" if f["severity"] == "BLOCK" else "")} for f in r.get("findings") or []]
                plan.findings += fs
                plan.notes.append(f"layer '{l.id}': hillshade not drawn (" + "; ".join(f["message"] for f in fs if f["id"] in ("MAP-WEB-011", "MAP-TER-001", "MAP-TER-004"))[:300] + ")")
                return
            plan.findings += [{**f, "layer": l.id} for f in r.get("findings") or []]
        item = relief_item(plan, r["path"], r["record"], win, None, opacity=opacity, z=z, layer_id=l.id, entry=l.entry, title=l.title)
    except Exception as ex:  # noqa: BLE001 - a relief never blocks a map
        import ogc_http as oh
        plan.findings.append(finding("MAP-TER-004", "WARN", f"layer '{l.id}': the hillshade could not be made ({oh.untrusted(ex, 200)}); the map was built without it", layer=l.id))
        plan.notes.append(f"layer '{l.id}': hillshade not drawn ({oh.untrusted(ex, 160)})")
        return
    if l.spec.get("provenance"):
        apply_dem_provenance(item, l.spec["provenance"])
    insert_relief(plan, item)
    plan.notes.append(f"layer '{l.id}': hillshade from {Path(r['path']).name}, exaggeration {z:g}, {opacity:.0%} opacity, under every layer")


def terrain_context(plan, win, shape, why: str, explicit: bool, tier: str) -> None:
    """Terrain as context (`terrain: {on: true}` or `context: {preset: full}` on a regional, metro or local sheet): the DEM is fetched for the window
    (or read from terrain.source), drawn as relief under everything, with contours when terrain.contours.on. A failure leaves the map without it
    and says so; it never blocks."""
    if any(b.get("kind") == "hillshade" for b in plan.basemap):
        plan.notes.append("terrain: a hillshade layer is already drawn; the terrain context adds no second one")
        return
    cfg = terrain_config(plan.spec)
    r, fs = dem_from_config(plan, win, shape, cfg, explicit)
    plan.findings += fs
    if r is None:
        why_not = "; ".join(f["message"] for f in fs) or "no DEM"
        plan.notes.append(f"terrain ({why}): not drawn: {why_not[:300]}")
        return
    z = exaggeration(cfg["exaggeration"], tier)
    try:
        entry = _dem_entry(r["record"])
        item = relief_item(plan, r["path"], r["record"], win, None, opacity=cfg["opacity"], z=z, layer_id="terrain", entry=entry, title="Relief")
        tprov = (plan.spec.get("terrain") or {}).get("provenance")
        if tprov:                                         # 1.2.0 (Day 10 issue 11): a DEM file under terrain: is credited from its provenance
            apply_dem_provenance(item, tprov)
    except Exception as ex:  # noqa: BLE001
        import ogc_http as oh
        plan.findings.append(finding("MAP-TER-004", "WARN" if explicit else "INFO", f"the relief could not be drawn ({oh.untrusted(ex, 200)}); the map was built without it"))
        plan.notes.append(f"terrain ({why}): not drawn ({oh.untrusted(ex, 160)})")
        return
    insert_relief(plan, item)
    rec = r["record"]
    plan.notes.append(f"terrain ({why}): relief from {item['record']['source_name']}" + (f" at about {rec['res_m']:.0f} m" if rec.get("res_m") else "")
                      + f", exaggeration {z:g}, {cfg['opacity']:.0%} opacity" + (" (DEM from the cache)" if rec.get("cached") else ""))
    if cfg["contours"]["on"]:
        try:
            cl = make_contours(plan, "terrain-contours", r["path"], rec, win, shape, cfg["contours"], entry, "Contours")
        except Exception as ex:  # noqa: BLE001
            import ogc_http as oh
            plan.notes.append(f"terrain: contours not drawn ({oh.untrusted(ex, 160)})")
            return
        if cl is not None:
            plan.layers.append(cl)


def _dem_entry(rec: dict) -> dict | None:
    import map_plan as mp
    eid = rec.get("entry")
    if not eid:
        return None
    try:
        return mp.library_entry(eid)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------------------------------------------- contours
def auto_interval(lo: float, hi: float, want: int = 12) -> float:
    span = max(hi - lo, 1.0)
    return float(min(CONTOUR_STEPS, key=lambda s: abs(span / s - want)))


def contour_layer(plan, l, win, shape) -> None:
    """`role: contour`: contour lines through the layer's DEM for the window, as a line layer drawn in neutrals (index lines heavier and labelled)."""
    import terrain_fetch as tf
    info = l.raster or {}
    cfg_t = terrain_config(plan.spec)
    c = dict(l.spec.get("contours") or {})
    ccfg = {"interval": c.get("interval", "auto"), "index_every": c.get("index_every"), "labels": c.get("labels", True) is not False}
    idx = plan.layers.index(l)
    try:
        if info.get("src") == "file":
            r = tf.dem_for(None, dem_path=info["path"])
        else:
            source = {tf.THREEDEP_ENTRY: "3dep", tf.COP_ENTRY: "copernicus"}.get(info.get("entry_id"), "terrarium")
            r = fetch_dem(plan, win, shape, source, cfg_t["terms_acknowledged"], cfg_t.get("resolution_m"))
            if not r.get("ok"):
                raise RasterLayerError("; ".join(f["message"] for f in r.get("findings") or []))
            plan.findings += [{**f, "layer": l.id} for f in r.get("findings") or []]
        new = make_contours(plan, l.id, r["path"], r["record"], win, shape, ccfg, l.entry, l.title, spec=l.spec)
    except RasterLayerError:
        raise
    except Exception as ex:  # noqa: BLE001
        raise RasterLayerError(f"layer '{l.id}': contours could not be made: {ex}", gc.EXIT_RUNTIME)
    plan.layers.remove(l)
    if new is not None:
        plan.layers.insert(idx, new)


def make_contours(plan, lid: str, dem_path: str, rec: dict, win, shape, ccfg: dict, entry, title: str, spec: dict | None = None):
    """A LayerPlan (kind line, role contour) of contour lines in the display CRS, or None when the window holds no level."""
    import map_plan as mp
    import terrain_fetch as tf
    rio = _rio()
    cshape = _cap(shape, CONTOUR_MAX_PX)
    g = rio.read_window(dem_path, win, plan.crs, out_shape=cshape, resampling="bilinear")
    dem = np.ma.masked_invalid(np.ma.asarray(g.array, dtype=float))
    dem = np.ma.masked_where(np.ma.filled(dem, np.nan) <= tf.NODATA + 1, dem)
    if dem.count() == 0:
        raise RasterLayerError("the DEM holds no elevation inside the map window")
    lo, hi = float(dem.min()), float(dem.max())
    interval = auto_interval(lo, hi) if ccfg.get("interval") in (None, "auto") else float(ccfg["interval"])
    every = max(1, int(ccfg.get("index_every") or 5))
    if interval in (25, 250, 2.5) and ccfg.get("index_every") is None:
        every = 4                                      # 25 m lines: index every 100 m, not every 125 m
    if (hi - lo) / interval > 400:
        raise RasterLayerError(f"an interval of {interval:g} m over {hi - lo:,.0f} m of relief makes more than 400 levels; use a larger interval")
    lines = rio.contours(dem, g.transform, plan.crs, interval=interval, index_every=every, min_points=4)
    if len(lines):                                     # a closed ring smaller than a few millimetres on the sheet is noise, not a hill
        closed = lines.geometry.apply(lambda q: q.is_ring if q.geom_type == "LineString" else False)
        lines = lines[~(closed & (lines["length"] < 0.025 * (win[2] - win[0])))].reset_index(drop=True)
    if not len(lines):
        plan.notes.append(f"contours: no {interval:g} m level crosses the map window ({lo:,.0f} to {hi:,.0f} m)")
        return None
    sp = dict(spec or {"id": lid, "role": "contour", "source": {"type": "ref", "id": (entry or {}).get("id", "terrain")}})
    sp.setdefault("legend", {})
    sp["legend"] = {**sp["legend"], "include": False}
    l = mp.LayerPlan(id=lid, spec=sp, role="contour", gdf=lines, kind="line", title=title, entry=entry)
    t = plan.tokens
    nr = list(t.get("neutral_ramp") or [t["frame"]])
    l.stroke = nr[min(3, len(nr) - 1)]
    import map_look
    cmm = map_look.params(getattr(plan, "look", "classic"))["contour_mm"] or (0.1, 0.22)     # 0.22.0 terrain look: 0.15 and 0.3 mm
    l.stroke_w_mm = cmm[0]
    l.stroke_opacity = 0.9
    l.symbol = {"index_stroke": nr[-1], "index_w_mm": cmm[1], "labels": bool(ccfg.get("labels", True)), "interval": interval, "index_every": every}
    l.crs_original = str(rec.get("crs") or "unknown")
    l.rows_loaded = int(len(lines))
    l.src_file, l.src_sha256 = Path(dem_path).name, rec.get("sha256") or _sha(dem_path)
    l.bbox = [round(v, 5) for v in window_wgs84(plan, win)]
    l.transformations.append(f"contours every {interval:g} m (index every {every * interval:g} m) traced with contourpy through the DEM read for the map window at "
                             f"{cshape[1]} x {cshape[0]} px in {plan.crs}; {len(lines)} line(s) from {lo:,.0f} to {hi:,.0f} m; never in the legend or the extent")
    l.provenance = mp.layer_provenance(l)
    if not entry:
        l.provenance.setdefault("source", rec.get("source") == "user" and f"DEM supplied ({Path(dem_path).name})" or "DEM")
        l.provenance["source_declared"] = True
        l.provenance.setdefault("license", rec.get("license") or "user-supplied")
        l.provenance.setdefault("retrieved_at", rec.get("retrieved_at"))
    if rec.get("attribution"):
        l.provenance.setdefault("attribution", rec["attribution"])
    l.provenance["contours"] = {"interval_m": interval, "index_every": every, "lines": int(len(lines)), "min_m": lo, "max_m": hi}
    l.raster = {"src": "contour", "interval": interval}
    plan.notes.append(f"contours every {interval:g} m (index lines every {every * interval:g} m), {len(lines)} line(s)")
    return l


# --------------------------------------------------------------------------------------------------------------- service pictures
def service_http():
    import ogc_http as oh
    return oh.SafeHTTP(limits={"timeout_s": 60, "max_bytes": 50_000_000})


def _service_src(l) -> dict:
    src = dict(l.spec.get("source") or {})
    if src.get("profile") and not src.get("url"):
        import service_registry as sr
        try:
            prof = sr.load(src["profile"])
        except Exception as ex:  # noqa: BLE001
            raise RasterLayerError(f"layer '{l.id}': service profile '{src['profile']}' cannot be read ({ex})")
        eps = prof.get("endpoints") or {}
        src["url"] = eps.get("wms") or eps.get("arcgis") or prof.get("base_url")
    return src


def service_cache_dir() -> Path:
    return gc.workdir(create=True) / "cache" / "service-images"


def service_layer(plan, l, win, shape) -> None:
    """A WMS GetMap or ArcGIS MapServer export picture as a print layer: requested in Web Mercator (any server draws it) in tiles of at most
    1,024 px (WMS) or 2,048 px (ArcGIS), each placed on the box it was drawn for, warped into the map's own grid; cached by request, so a second
    build reads nothing. The service's legend (GetLegendGraphic, or the MapServer's legend swatches with Inter labels) goes in the map legend."""
    import ogc_http as oh
    src = _service_src(l)
    proto = _protocol(src)
    url = src["url"]
    req_crs = str(src.get("crs") or "EPSG:3857")
    layers = src.get("layers")
    if isinstance(layers, (int, str)):
        layers = [layers]
    layers = [str(x) for x in (layers or [])]
    op = float((l.spec.get("raster") or {}).get("opacity", src.get("opacity", 1.0)))
    key_d = {"url": oh.redact_url(url), "proto": proto, "layers": layers, "styles": src.get("styles"), "crs": req_crs, "display": plan.crs,
             "win": [round(v, 2) for v in win], "shape": list(shape), "fmt": src.get("format"), "version": src.get("version")}
    key = hashlib.sha1(json.dumps(key_d, sort_keys=True).encode()).hexdigest()[:16]
    cdir = service_cache_dir()
    pp, rp = cdir / f"{key}.png", cdir / f"{key}.json"
    cached = False
    if pp.is_file() and rp.is_file():
        rec = json.loads(rp.read_text())
        if rec.get("sha256") == hashlib.sha256(pp.read_bytes()).hexdigest():
            rgba = png_rgba(pp.read_bytes())
            cached = True
    if not cached:
        if offline():
            raise RasterLayerError(f"layer '{l.id}': the service picture is not cached and GMAP_OFFLINE=1 forbids fetching it", gc.EXIT_REFUSED)
        rgba, rec = fetch_service_picture(plan, l, src, proto, url, layers, req_crs, win, shape)
        png = png_bytes(rgba)
        cdir.mkdir(parents=True, exist_ok=True)
        pp.write_bytes(png)
        rec["sha256"] = hashlib.sha256(png).hexdigest()
        rec["retrieved_at"] = _today()
        rp.write_text(json.dumps(rec, indent=2, default=str))
    rec["cached"] = cached
    if int(rgba[3].max()) == 0:
        plan.findings.append(finding("MAP-SVC-013", "WARN", f"layer '{l.id}': the service drew nothing in the map window (every pixel is transparent); check the layer names and the extent", layer=l.id))
    png = png_bytes(rgba)
    l.raster.update({"image": {"png": png, "rgba": rgba, "bbox": list(win), "transform": grid_transform(win, rgba.shape[1:]), "crs": plan.crs, "opacity": max(0.0, min(1.0, op)),
                               "shape": [int(rgba.shape[1]), int(rgba.shape[2])]}})
    host = Path(oh.redact_url(url).split("://", 1)[-1]).parts[0] if "://" in url else url
    title = src.get("title") or rec.get("title") or l.title
    l.transformations.append(f"{'WMS GetMap' if proto == 'wms' else 'ArcGIS MapServer export'} of {', '.join(layers) or 'the default layers'} from {host}, "
                             f"requested in {req_crs} as {rec.get('tiles', 1)} tile(s) of at most {SERVICE_TILE_PX[proto]} px, each placed on the box the server drew, "
                             f"warped into {plan.crs} at {rgba.shape[2]} x {rgba.shape[1]} px" + ("; read from the cache" if cached else f"; fetched {rec.get('retrieved_at')}"))
    leg = None
    if (l.spec.get("legend") or {}).get("include", True) is not False and src.get("legend", True) is not False:
        leg = service_legend(plan, l, src, proto, url, layers, key, cached)
    l.legend_block = leg
    l.src_file, l.src_sha256 = f"{key}.png", rec.get("sha256")
    prov_src = dict(l.spec.get("provenance") or {})
    p = {"source": prov_src.get("source") or f"{title} ({host})", "retrieved_at": prov_src.get("retrieved_at") or rec.get("retrieved_at"),
         "vintage": prov_src.get("vintage"), "license": prov_src.get("license"), "attribution": prov_src.get("attribution") or rec.get("copyright"),
         "url": oh.redact_url(url), "source_declared": True, "declared": bool(prov_src)}
    l.spec = dict(l.spec, provenance={k: v for k, v in p.items() if v is not None and k not in ("source_declared", "declared")})
    import map_plan as mp
    l.provenance = mp.layer_provenance(l)
    l.provenance["source_declared"] = True
    srec = {"protocol": proto, "url": oh.redact_url(url), "layers": layers, "request_crs": req_crs, "tiles": rec.get("tiles"), "requests": rec.get("request_count"),
            "bytes": rec.get("request_bytes"), "cache_key": key, "cached": cached, "window": [round(v, 3) for v in win], "window_crs": plan.crs,
            "pixels": [int(rgba.shape[2]), int(rgba.shape[1])], "sha256": rec.get("sha256"), "opacity": op}
    l.provenance["raster"] = srec
    l.raster["record"] = srec
    l.bbox = [round(v, 5) for v in window_wgs84(plan, win)]
    if not l.provenance.get("license"):
        plan.notes.append(f"layer '{l.id}': say the service's licence in provenance.license (the source note credits it)")


def _today() -> str:
    import datetime as dt
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


def fetch_service_picture(plan, l, src, proto, url, layers, req_crs, win, shape):
    """The tiles requested, placed and warped into the display grid. -> (RGBA bands x rows x cols, record)."""
    from pyproj import Transformer
    from rasterio.warp import reproject, Resampling
    rio = _rio()
    tr = Transformer.from_crs(plan.crs, req_crs, always_xy=True)
    xs = np.linspace(win[0], win[2], 21)
    ys = np.linspace(win[1], win[3], 21)
    X, Y = np.meshgrid(xs, ys)
    px, py = tr.transform(X.ravel(), Y.ravel())
    ok = np.isfinite(px) & np.isfinite(py)
    bx0, by0, bx1, by1 = float(px[ok].min()), float(py[ok].min()), float(px[ok].max()), float(py[ok].max())
    H, W = shape
    aspect = (by1 - by0) / max(bx1 - bx0, 1e-9)
    RW = max(W, 16)
    RH = max(16, int(round(RW * aspect)))
    tile = SERVICE_TILE_PX[proto]
    nx, ny = math.ceil(RW / tile), math.ceil(RH / tile)
    if nx * ny > SERVICE_MAX_TILES:
        k = math.sqrt(nx * ny / SERVICE_MAX_TILES)
        RW, RH = int(RW / k), int(RH / k)
        nx, ny = math.ceil(RW / tile), math.ceil(RH / tile)
    dst = np.zeros((4, H, W), np.uint8)
    dst_t = grid_transform(win, (H, W))
    h = service_http()
    tw, th = math.ceil(RW / nx), math.ceil(RH / ny)
    cw, ch = (bx1 - bx0) / (nx * tw), (by1 - by0) / (ny * th)
    count, nbytes, title, copyright_ = 0, 0, None, None
    for j in range(ny):
        for i in range(nx):
            tb = (bx0 + i * tw * cw, by1 - (j + 1) * th * ch, bx0 + (i + 1) * tw * cw, by1 - j * th * ch)
            try:
                if proto == "wms":
                    g = rio.read_wms_image(url, layers, tb, req_crs, (tw, th), http=h, version=src.get("version"), fmt=src.get("format") or "image/png",
                                           styles=src.get("styles"), transparent=src.get("transparent", True) is not False, time=src.get("time"))
                else:
                    lay = src.get("layers")
                    lay_s = None if not lay else (str(lay) if isinstance(lay, str) else "show:" + ",".join(str(x) for x in (lay if isinstance(lay, list) else [lay])))
                    sr = int(str(req_crs).split(":")[-1])
                    g = rio.read_arcgis_export(url, tb, sr, size=(tw, th), layers=lay_s, http=h, transparent=src.get("transparent", True) is not False)
            except Exception as ex:  # noqa: BLE001
                import ogc_http as oh
                raise RasterLayerError(f"layer '{l.id}': the service picture could not be fetched (tile {j * nx + i + 1} of {nx * ny}): {oh.untrusted(ex, 200)}", gc.EXIT_RUNTIME)
            a = np.ma.filled(np.ma.asarray(g.array), 0).astype(np.uint8)
            if a.shape[0] == 3:
                a = np.concatenate([a, np.full((1,) + a.shape[1:], 255, np.uint8)])
            tile_dst = np.zeros_like(dst)
            for b in range(4):
                reproject(a[b], tile_dst[b], src_transform=g.transform, src_crs=g.crs, dst_transform=dst_t, dst_crs=plan.crs,
                          resampling=Resampling.bilinear if b < 3 else Resampling.nearest, src_nodata=None, dst_nodata=0)
            put = tile_dst[3] > 0
            dst[:, put] = tile_dst[:, put]
            count += 1
            nbytes += int((g.source or {}).get("bytes") or 0)
            title = title or (g.source or {}).get("title")
    calls = list(getattr(getattr(h, "audit", None), "calls", []) or [])     # every HTTP request (a MapServer tile is two: f=json, then the picture)
    if calls:
        count, nbytes = len(calls), int(sum(int(c.get("bytes", 0)) for c in calls))
    rec = {"protocol": proto, "tiles": nx * ny, "grid": [nx, ny], "tile_px": [tw, th], "request_crs": req_crs, "request_bbox": [bx0, by0, bx1, by1],
           "request_count": count, "request_bytes": nbytes, "title": title, "copyright": copyright_}
    return dst, rec


def fetch_service_legend(src: dict, proto: str, url: str, layers: list, http) -> dict:
    """The service's own legend: {kind: image, png} from WMS GetLegendGraphic, or {kind: swatches, entries: [(png, label)]} from a MapServer."""
    import ogc_http as oh
    if proto == "wms":
        import ogc_client as oc
        prof = {"base_url": url, "endpoints": {"wms": url}, "protocols": {"wms": src.get("version")} if src.get("version") else {}}
        ver = src.get("version") or "1.3.0"
        r = http.get(oc.endpoint(prof, "wms"), {"service": "WMS", "version": ver, "request": "GetLegendGraphic", "layer": layers[0] if layers else "", "format": "image/png"},
                     expect="image")
        return {"kind": "image", "png": r.content}
    r = http.get(url.rstrip("/") + "/legend", {"f": "json"}, expect="json")
    d = json.loads(r.content)
    want = {str(x) for x in (src.get("layers") or [])} if isinstance(src.get("layers"), list) else None
    entries = []
    for lay in d.get("layers", []):
        if want and str(lay.get("layerId")) not in want:
            continue
        for lg_ in lay.get("legend", [])[:24]:
            if lg_.get("imageData") and (lg_.get("contentType") or "image/png") == "image/png":
                entries.append((base64.b64decode(lg_["imageData"]), oh.untrusted(lg_.get("label"), 60)))
    return {"kind": "swatches", "entries": entries}


def service_legend(plan, l, src, proto, url, layers, key, cached: bool) -> dict | None:
    """The legend block for a service picture, cached beside it; None (with a note) when the service gives none."""
    import legend as lg
    import ogc_http as oh
    cdir = service_cache_dir()
    lp = cdir / f"{key}.legend.json"
    data = None
    if lp.is_file():
        try:
            raw = json.loads(lp.read_text())
            data = {"kind": raw["kind"], "png": base64.b64decode(raw["png"]) if raw.get("png") else None,
                    "entries": [(base64.b64decode(p), lab) for p, lab in raw.get("entries", [])]}
        except (ValueError, KeyError):
            data = None
    if data is None:
        if offline():
            plan.notes.append(f"layer '{l.id}': the service legend is not cached (offline); the map legend names the layer only")
            return lg.image_block(l.spec | {"title": l.title}, None, plan.tokens, note="legend not available")
        try:
            data = fetch_service_legend(src, proto, url, layers, service_http())
        except Exception as ex:  # noqa: BLE001 - a legend is a courtesy; the picture is drawn without it
            plan.notes.append(f"layer '{l.id}': the service legend could not be read ({oh.untrusted(ex, 120)}); the map legend names the layer only")
            return lg.image_block(l.spec | {"title": l.title}, None, plan.tokens, note="legend not available from the service")
        cdir.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"kind": data["kind"], "png": base64.b64encode(data["png"]).decode() if data.get("png") else None,
                                  "entries": [(base64.b64encode(p).decode(), lab) for p, lab in data.get("entries", [])]}))
    title = (l.spec.get("legend") or {}).get("title") or l.title
    if data["kind"] == "swatches" and data.get("entries"):
        return lg.swatches_block(l.spec | {"title": title}, [(data_uri(p), lab) for p, lab in data["entries"]], plan.tokens, opacity=l.raster["image"]["opacity"],
                                 note=(l.spec.get("legend") or {}).get("note") or "symbols from the service")
    if data.get("png"):
        return lg.image_block(l.spec | {"title": title}, data_uri(data["png"]), plan.tokens, px=_png_size(data["png"]), note=(l.spec.get("legend") or {}).get("note") or "legend from the service")
    return lg.image_block(l.spec | {"title": title}, None, plan.tokens, note="legend not available from the service")


def _png_size(png: bytes) -> tuple[int, int]:
    import struct
    if png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR":
        return struct.unpack(">II", png[16:24])
    try:
        from PIL import Image
        return Image.open(io.BytesIO(png)).size
    except Exception:  # noqa: BLE001
        return (100, 100)


def class_edges(classes, a) -> tuple[list[float], tuple[bool, bool]]:
    """1.2.0 (Day 8 issue 5): raster.classes given with the data's own ends ([1, 5, 10, 20, 100] over values 1 to 100) are class edges, not
    breaks: a first value at or below the data's minimum and a last at or above its maximum are dropped from the breaks, so there is no
    empty "below 1" or "100 and above" class. Returns (the interior breaks, (first dropped, last dropped))."""
    b = sorted(float(x) for x in classes)
    data = np.ma.compressed(np.ma.masked_invalid(np.ma.asarray(a, dtype="float64")))
    if not len(data) or len(b) < 2:
        return b, (False, False)
    lo, hi = float(np.min(data)), float(np.max(data))
    lead, trail = b[0] <= lo, b[-1] >= hi
    inner = b[(1 if lead else 0):(len(b) - 1 if trail else len(b))]
    if not inner:                                         # every value given is an end: keep them as breaks
        return b, (False, False)
    return inner, (lead, trail)


def _edge_labels(breaks: list[float], lo, hi, fmt) -> list[str]:
    """Class labels for interior breaks with known end edges: "1 to 5", "5 to 10", ..., "20 to 100"; an open end keeps the usual words."""
    edges = ([lo] if lo is not None else [None]) + list(breaks) + ([hi] if hi is not None else [None])
    out = []
    n = len(breaks) + 1
    for i in range(n):
        f_, t_ = edges[i], edges[i + 1]
        if f_ is None:
            out.append(f"below {fmt(t_)}")
        elif t_ is None:
            out.append(f"{fmt(f_)} and above")
        else:
            out.append(f"{fmt(f_)} to {fmt(t_)}")
    return out
