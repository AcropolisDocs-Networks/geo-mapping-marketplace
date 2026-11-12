"""webmap_3d: 3D on the MapLibre page (M15 web half, gap analysis P2-1). `webmap.build_page` calls it when a page asks for 3D; nothing here
touches the network.

What a page can show in 3D, each from data the build already has:

  terrain     a raster-DEM source made from the DEM the print relief uses (3DEP in the United States; Terrarium only with
              terrain.terms_acknowledged, MAP-WEB-011), read for the page's window, written as Terrarium PNG tiles and embedded in the page
              (one base64 block, served to MapLibre through `maplibregl.addProtocol('gmdem', ...)`; nothing is fetched when the page opens).
              The tiles go to the zoom whose pixels are as fine as the DEM's cells, and lower until they fit DEM_MAX_BYTES; the page says
              which zoom and how many metres a pixel that is. Elevations are carried relative to the window's lowest ground and ease down to
              it beyond the DEM's edge, so the terrain meets the flat map without a cliff. A hillshade layer is drawn from the same tiles.
  extrusion   `extrude: {height_field | height_m, base_field | base_m, floors_field, floor_m, default_m}` on a polygon layer (a person's own
              Overture or Microsoft footprints, read with the plugin's own readers) is drawn as `fill-extrusion`, in one brand neutral unless
              the layer is classified; `extrude: {at_field | at_m, slab_m}` draws a slab centred at a height (sector wedges at antenna height).
  masts       `mast: {height_field | height_m}` on a point layer: a thin column per site, square in metres, with a head at the antenna
              height. A column a few metres wide is a pixel at a city zoom, so each mast is drawn at four widths, each shown at the zooms
              where it is about three pixels wide; the site's point stays on the map at every zoom.
  links       a layer drawn by the `line_of_sight` op: each link is drawn as a 3D line from antenna top to antenna top, as short extruded
              segments at the ray's clearance above the ground the page shows (so an obstructed link goes into the hill it hits), in the
              same clear and obstructed colours as on paper, and its profile chart (profile_chart.draw, size web) is embedded for the popup.

MapLibre counts 512-pixel tiles: the zooms here are MapLibre's (the page's minus one) unless a name says otherwise; the terrain tiles are
512-pixel tiles on the same grid.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import functools
import math
import re
from pathlib import Path

import numpy as np

DEM_MAX_BYTES = 4 * 1024 * 1024        # the embedded terrain tiles (PNG bytes, before base64) are kept under this
DEM_TILE_PX = 512                      # the terrain tiles are cut like MapLibre's own (512 px), so a terrain tile and a data tile share z/x/y
DEM_MAX_ZOOM = 14                      # the finest terrain tile zoom (about 4 m a pixel at mid latitudes)
TAPER_SHARE = 0.12                     # beyond the DEM's window the ground eases down over this share of the window's width
DEFAULT_PITCH = 50.0
MAX_PITCH = 85.0
MAST_HEAD_M = 3.0                      # the antenna head: the top this many metres of the mast, a little wider
MAST_PX = 4.5                          # a mast column is about this many pixels wide in the middle of its zoom band
TUBE_PX = 3.0                          # a link is about this many pixels wide
MAX_ZOOM_3D = 18                       # a 3D page with buildings or masts zooms to street level (the page's zoom numbers)
BANDS = (15.0, 13.5, 12.0, 10.5)       # MapLibre zooms where each width band starts; one more band covers every zoom below the last
M_EARTH = 40075016.686
EXAGGERATION = {"site": 1.0, "local": 1.0, "metro": 1.5}      # terrain exaggeration by the map's scale tier; 2 for coarser maps


class Web3DError(ValueError):
    pass


def finding(fid: str, sev: str, message: str, detail=None, layer: str | None = None) -> dict:
    d = {"id": fid, "severity": sev, "message": message, "detail": detail}
    if layer:
        d["layer"] = layer
    return d


# ---------------------------------------------------------------------------------------------------------------- settings
def settings(spec: dict) -> dict | None:
    """The page's 3D settings, or None when the page is flat. 3D is on when `webmap.view` has a pitch or a bearing, `webmap.3d` is set, or
    a layer has `extrude` or `mast`. Terrain is on when `webmap.3d.terrain` says so, or (unless it says false) when the map already has a DEM:
    `terrain: {on: true}` or a `role: hillshade` layer."""
    wm = spec.get("webmap") or {}
    view = dict(wm.get("view") or {})
    d3 = wm.get("3d")
    if d3 is False:
        return None
    d3 = {} if d3 in (None, True) else dict(d3)
    layers = [l for l in spec.get("layers") or [] if isinstance(l, dict)]
    extruded = [l["id"] for l in layers if l.get("extrude")]
    masts = [l["id"] for l in layers if l.get("mast")]
    links = [l["id"] for l in layers if any(isinstance(o, dict) and str(o.get("op", "")).lower() == "line_of_sight" for o in l.get("ops") or [])]
    pitch = view.get("pitch")
    bearing = float(view.get("bearing") or 0.0)
    asked = bool(wm.get("3d")) or bool(extruded or masts) or bool(pitch) or bool(bearing)
    if not asked:
        return None
    has_dem = bool((spec.get("terrain") or {}).get("on")) or any(l.get("role") == "hillshade" for l in layers)
    t = d3.get("terrain")
    terrain = bool(t) if t is not None else has_dem
    tcfg = t if isinstance(t, dict) else {}
    p = float(pitch) if pitch is not None else DEFAULT_PITCH
    return {"pitch": max(0.0, min(MAX_PITCH, p)), "bearing": ((bearing + 180.0) % 360.0) - 180.0, "terrain": terrain,
            "exaggeration": tcfg.get("exaggeration", d3.get("exaggeration", "auto")), "toggle": d3.get("toggle", True) is not False,
            "max_dem_mb": float(tcfg.get("max_mb", d3.get("max_dem_mb", DEM_MAX_BYTES / 1048576))), "extruded": extruded, "masts": masts,
            "links": links, "start": "3d" if d3.get("start", "3d") != "2d" else "2d", "pitch_asked": pitch is not None,
            "sky": str(d3.get("sky", "auto")).lower() if d3.get("sky") not in (True, False) else ("on" if d3.get("sky") else "off")}


# ---------------------------------------------------------------------------------------------------------------- 1.6.0 (R4-SKY)
SKY_PITCH = 30.0                     # sky: auto is on above this pitch
SKY_PROPS = ("sky-color", "sky-horizon-blend", "horizon-color", "horizon-fog-blend", "fog-color", "fog-ground-blend", "atmosphere-blend")
FOV_DEG = 36.8699                    # MapLibre's vertical field of view (0.6435 rad)
MARGIN_MAX = 3.0                     # the DEM margin reaches at most this many window widths beyond each side


@functools.lru_cache(maxsize=1)
def sky_properties() -> tuple:
    """The sky properties the vendored MapLibre's style specification knows (read from the bundle at build, not assumed)."""
    import gmap_common as gc
    js = (gc.plugin_root() / "vendor" / "maplibre" / "maplibre-gl.js").read_text(encoding="utf-8", errors="replace")
    return tuple(p for p in SKY_PROPS if f'"{p}"' in js)


def sky_wanted(s3: dict) -> bool:
    v = s3.get("sky", "auto")
    return v == "on" or (v == "auto" and float(s3["pitch"]) > SKY_PITCH)


def sky_block(t: dict) -> dict:
    """The style's sky from the page's tokens, never a colour of its own (MAP-BRAND-001): the sky the water token (the background with a little
    of the emphasis in every brand pack), the horizon the land, the fog the background; a dark page has dark tokens, so its sky is dark."""
    vals = {"sky-color": t.get("water") or t["background"], "sky-horizon-blend": 0.6, "horizon-color": t.get("land") or t["background"],
            "horizon-fog-blend": 0.7, "fog-color": t["background"], "fog-ground-blend": 0.35, "atmosphere-blend": 0.0}
    return {k: vals[k] for k in sky_properties()}


def margin_dem(item: dict, bounds) -> dict | None:
    """A DEM for the ground the camera can see, at four times the map DEM's cell (two zooms coarser): the same source as the relief (3DEP in the
    US, the Copernicus DEM elsewhere), fetched once and cached; None offline or for a DEM the person supplied (nothing is fetched for it)."""
    import map_raster
    import terrain_fetch as tf
    import gmap_common as gc
    rec = item.get("record") or {}
    src = ((rec.get("terrain") or {}).get("source")) or None
    if map_raster.offline() or src not in ("3dep", "copernicus", "terrarium"):
        return None
    res = float((rec.get("terrain") or {}).get("res_m") or 30.0) * 4
    r = tf.dem_for(tuple(bounds), max(60.0, res), src, gc.workdir(create=True), surface_ok=True)
    if not r.get("ok"):
        return None
    return {"path": r["path"], "bounds": list(bounds), "source": src}


def ground_footprint(window, pitch: float, *, view_px=(1280, 720), zoom: float | None = None) -> dict:
    """The ground a camera at `pitch` can see when it frames the window (lon/lat), any bearing: a radius around the centre, in metres, from the
    camera geometry (altitude from the field of view; the top screen ray meets the ground at h * tan(pitch + fov/2)), capped at MARGIN_MAX window
    widths beyond each side. -> {radius_m, capped, bounds (lon/lat), far_m}."""
    x0, y0, x1, y1 = window
    lat = (y0 + y1) / 2
    mx = 111_320.0 * math.cos(math.radians(lat))
    w_m, h_m = (x1 - x0) * mx, (y1 - y0) * 110_574.0
    W, H = view_px
    m_px = max(w_m / W, h_m / H) if zoom is None else 40_075_016.7 * math.cos(math.radians(lat)) / (512 * 2 ** zoom)
    half = math.radians(FOV_DEG / 2)
    cam = (H / 2) / math.tan(half)                       # camera to centre, in pixels
    alt = cam * math.cos(math.radians(pitch))
    near_dist = alt * math.tan(math.radians(pitch))
    top = math.radians(pitch) + half
    far = (alt * math.tan(top) - near_dist) if top < math.radians(89.5) else math.inf
    dist_far = alt / math.cos(top) if math.isfinite(far) else math.inf     # camera to the top screen row's ground point
    side = (W / 2) * dist_far / cam                                         # the half-width of the ground seen along that row
    r_px = max(math.hypot(far, side) if math.isfinite(far) else math.inf, math.hypot(W, H) / 2)
    cap_m = MARGIN_MAX * max(w_m, h_m) + max(w_m, h_m) / 2
    r_m = r_px * m_px
    capped = not math.isfinite(r_m) or r_m > cap_m
    r_m = min(r_m, cap_m)
    cx, cy = (x0 + x1) / 2, lat
    dx, dy = r_m / mx, r_m / 110_574.0
    return {"radius_m": round(r_m, 1), "capped": capped, "far_m": round(far * m_px, 1) if math.isfinite(far) else None,
            "bounds": [round(cx - dx, 6), round(max(-85.0, cy - dy), 6), round(cx + dx, 6), round(min(85.0, cy + dy), 6)]}


def max_pitch_covered(window, cover_bounds, *, view_px=(1280, 720), start: float = MAX_PITCH) -> float:
    """The highest pitch whose ground footprint the DEM's cover holds (in whole degrees, down to 45)."""
    p = float(start)
    while p > 45:
        fp = ground_footprint(window, p, view_px=view_px)["bounds"]
        if cover_bounds[0] <= fp[0] and cover_bounds[1] <= fp[1] and cover_bounds[2] >= fp[2] and cover_bounds[3] >= fp[3]:
            return p
        p -= 1.0
    return 45.0


def web_edit(spec: dict) -> None:
    """Before the plan: a page that asks for terrain gets the print relief too (terrain on), so both draw the same DEM; an extruded layer with
    no colour of its own is drawn in a brand neutral on paper and on the page."""
    s = settings(spec)
    if not s:
        return
    if s["terrain"] and not any(isinstance(l, dict) and l.get("role") == "hillshade" for l in spec.get("layers") or []):
        spec["terrain"] = dict(spec.get("terrain") or {}, on=True)
    for l in spec.get("layers") or []:
        if isinstance(l, dict) and l.get("extrude") and not l.get("classification") and not (l.get("symbol") or {}).get("fill"):
            l["symbol"] = dict(l.get("symbol") or {}, fill="@brand.neutral.2")


def exaggeration(value, tier: str) -> float:
    if value in (None, "auto"):
        return EXAGGERATION.get(tier, 2.0)
    return max(0.1, min(10.0, float(value)))


# ---------------------------------------------------------------------------------------------------------------- heights
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def extrude_heights(lid: str, ex: dict, gdf) -> tuple[list, list, list, dict]:
    """(heights, bases, notes, stats) in metres above the ground for every row of the layer, from its `extrude` block:
    height_field | height_m (the top), base_field | base_m (the bottom, default 0), floors_field x floor_m when the height is missing,
    default_m when both are; at_field | at_m with slab_m: a slab of that thickness centred at the height."""
    ex = dict(ex or {})
    n = len(gdf)
    cols = set(gdf.columns)
    for k in ("height_field", "base_field", "floors_field", "at_field"):
        if ex.get(k) and ex[k] not in cols:
            raise Web3DError(f"layer '{lid}': extrude.{k} '{ex[k]}' is not a field of the layer (fields: {', '.join(sorted(c for c in cols if c != gdf.geometry.name))})")
    def col(k):
        return gdf[ex[k]].tolist() if ex.get(k) else [None] * n
    notes = []
    stats = {"from_field": 0, "from_floors": 0, "default": 0, "not_numeric": 0}
    if ex.get("at_field") or ex.get("at_m") is not None:
        slab = float(ex.get("slab_m", 4.0))
        at = col("at_field") if ex.get("at_field") else [ex.get("at_m")] * n
        hs, bs = [], []
        for v in at:
            f = _num(v)
            if f is None:
                stats["not_numeric"] += 1
                f = float(ex.get("default_m", 30.0))
            hs.append(round(f + slab / 2, 2))
            bs.append(round(max(0.5, f - slab / 2), 2))           # a slab near the ground rests on it rather than reaching under it
        notes.append(f"layer '{lid}': drawn in 3D as {slab:g} m slabs at " + (f"the height in '{ex['at_field']}'" if ex.get("at_field") else f"{float(ex['at_m']):g} m"))
        return hs, bs, notes, stats
    top = col("height_field")
    floors = col("floors_field")
    base = col("base_field") if ex.get("base_field") else [ex.get("base_m", 0.0)] * n
    floor_m = float(ex.get("floor_m", 3.2))
    default = ex.get("default_m")
    if not ex.get("height_field") and ex.get("height_m") is None and not ex.get("floors_field"):
        raise Web3DError(f"layer '{lid}': extrude needs height_field, height_m or floors_field (or at_field with slab_m for a slab at a height)")
    hs, bs = [], []
    for i in range(n):
        h = _num(top[i]) if ex.get("height_field") else (_num(ex.get("height_m")) if ex.get("height_m") is not None else None)
        if h is not None:
            stats["from_field"] += 1
        else:
            if ex.get("height_field") and top[i] not in (None, "") and not (isinstance(top[i], float) and math.isnan(top[i])):
                stats["not_numeric"] += 1
            fl = _num(floors[i])
            if fl is not None:
                h = fl * floor_m
                stats["from_floors"] += 1
            else:
                h = float(default if default is not None else 8.0)
                stats["default"] += 1
        b = _num(base[i]) or 0.0
        hs.append(round(h, 2))
        bs.append(round(b, 2))
    if stats["from_floors"] or stats["default"]:
        bits = []
        if stats["from_floors"]:
            bits.append(f"{stats['from_floors']} from floors x {floor_m:g} m")
        if stats["default"]:
            bits.append(f"{stats['default']} at the default {float(default if default is not None else 8.0):g} m")
        notes.append(f"layer '{lid}': {n - stats['from_floors'] - stats['default']} of {n} heights from the data; " + ", ".join(bits))
    return hs, bs, notes, stats


def mast_heights(lid: str, m: dict, gdf) -> list[float]:
    m = dict(m or {})
    if m.get("height_field"):
        if m["height_field"] not in gdf.columns:
            raise Web3DError(f"layer '{lid}': mast.height_field '{m['height_field']}' is not a field of the layer")
        out = []
        for v in gdf[m["height_field"]].tolist():
            f = _num(v)
            out.append(round(f, 2) if f is not None else float(m.get("default_m", 30.0)))
        return out
    return [float(m.get("height_m", 30.0))] * len(gdf)


# ---------------------------------------------------------------------------------------------------------------- geometry in metres
def m_per_px(z: float, lat: float) -> float:
    """Ground metres per screen pixel at MapLibre zoom z (512-pixel tiles)."""
    return M_EARTH * math.cos(math.radians(lat)) / (512.0 * 2 ** z)


def band_widths(lat: float, px: float) -> list[tuple[float, float | None, float]]:
    """[(minzoom, maxzoom, width_m)]: each band shows between two zooms of BANDS and is `px` pixels wide in the middle of them (the band
    under the last zoom is sized for a zoom and a half below it)."""
    out = []
    edges = list(BANDS) + [0.0]
    for i, lo in enumerate(edges):
        hi = BANDS[i - 1] if i > 0 else None
        mid = (lo + (hi if hi is not None else lo + 1.5)) / 2 if lo > 0 else BANDS[-1] - 0.75
        out.append((lo, hi, round(px * m_per_px(mid, lat), 2)))
    return out


def _square(lon: float, lat: float, w: float) -> list:
    dx = w / 2 / (111_320.0 * math.cos(math.radians(lat)))
    dy = w / 2 / 110_540.0
    return [[[round(lon - dx, 7), round(lat - dy, 7)], [round(lon + dx, 7), round(lat - dy, 7)], [round(lon + dx, 7), round(lat + dy, 7)],
             [round(lon - dx, 7), round(lat + dy, 7)], [round(lon - dx, 7), round(lat - dy, 7)]]]


def mast_collection(col: dict, keep: list[str]) -> dict:
    """Columns and heads for the mast points of a page collection (each feature carries `_h`, its antenna height), at every width band."""
    feats = []
    for f in col["features"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        h = float(f["properties"].get("_h") or 0.0)
        props = {k: f["properties"][k] for k in keep if k in f["properties"]}
        for bi, (_lo, _hi, w) in enumerate(band_widths(lat, MAST_PX)):
            head = min(MAST_HEAD_M, max(h * 0.25, 0.5))
            feats.append({"type": "Feature", "properties": {**props, "_band": bi, "_part": "column", "_h": round(max(h - head, 0.0), 2), "_b": 0.0},
                          "geometry": {"type": "Polygon", "coordinates": _square(lon, lat, w)}})
            feats.append({"type": "Feature", "properties": {**props, "_band": bi, "_part": "head", "_h": round(h, 2), "_b": round(max(h - head, 0.0), 2)},
                          "geometry": {"type": "Polygon", "coordinates": _square(lon, lat, w * 1.8)}})
    return {"type": "FeatureCollection", "features": feats}


def tube_features(pts: list, d: np.ndarray, clear: np.ndarray, props: dict, lat: float) -> list[dict]:
    """A link as short segments, each a flat rectangle as wide as the band's line and as thick, at the ray's clearance above the ground
    (MapLibre raises every extrusion by the ground under its centre). Segments wholly under the ground are left out: the link goes into the hill."""
    out = []
    lon = np.array([p[0] for p in pts], float)
    la = np.array([p[1] for p in pts], float)
    D = float(d[-1])
    kx = 111_320.0 * math.cos(math.radians(lat))
    ky = 110_540.0
    for bi, (_lo, _hi, w) in enumerate(band_widths(lat, TUBE_PX)):
        # a segment ends where the clearance has moved by half the line's width (so the steps between flat segments stay small) or at
        # a length of 25 widths: a link over flat ground needs few segments, one over a ridge many
        ds = [0.0]
        c0, s0 = float(clear[0]), 0.0
        for k in range(1, len(d)):
            if abs(float(clear[k]) - c0) > w / 2 or d[k] - s0 > max(60.0, 25 * w):
                ds.append(float(d[k - 1]) if d[k - 1] > s0 else float(d[k]))
                s0 = ds[-1]
                c0 = float(np.interp(s0, d, clear))
        if ds[-1] < D:
            ds.append(D)
        ds = np.asarray(ds)
        n = len(ds) - 1
        x = np.interp(ds, d, lon)
        y = np.interp(ds, d, la)
        for i in range(n):
            sel = (d >= ds[i]) & (d <= ds[i + 1])
            c = float(np.mean(clear[sel])) if sel.any() else float(np.interp((ds[i] + ds[i + 1]) / 2, d, clear))
            lo_m, hi_m = c - w / 2, c + w / 2
            if hi_m <= 0.5:
                continue
            lo_m = max(0.5, lo_m)          # where the line is thicker than its clearance (a coarse zoom, or the link meeting the hill) it rests
            #                                on the ground rather than reaching under it
            ex, ey = (x[i + 1] - x[i]) * kx, (y[i + 1] - y[i]) * ky
            L = math.hypot(ex, ey) or 1.0
            nx, ny = -ey / L * w / 2, ex / L * w / 2           # the half-width, across the segment, in metres
            # a little overlap so the segments read as one line
            ox, oy = ex / L * w * 0.15 / kx, ey / L * w * 0.15 / ky
            x0, y0, x1, y1 = x[i] - ox, y[i] - oy, x[i + 1] + ox, y[i + 1] + oy
            ring = [[x0 + nx / kx, y0 + ny / ky], [x1 + nx / kx, y1 + ny / ky], [x1 - nx / kx, y1 - ny / ky], [x0 - nx / kx, y0 - ny / ky]]
            ring = [[round(a, 6), round(b, 6)] for a, b in ring]
            ring.append(ring[0])
            out.append({"type": "Feature", "properties": {**props, "_band": bi, "_b": round(lo_m, 2), "_h": round(hi_m, 2)},
                        "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return out


# ---------------------------------------------------------------------------------------------------------------- line of sight
def los_op(l) -> dict | None:
    ops = [o for o in (l.spec.get("ops") or []) if isinstance(o, dict)]
    return ops[-1] if ops and str(ops[-1].get("op", "")).lower() == "line_of_sight" else None


def link_dem(plan, l, op: dict, bounds) -> tuple[str, dict]:
    """The DEM the link's op read: its `dem:` file, else the MapSpec's terrain source (offline: a file only)."""
    import map_plan as mp
    base = Path(plan.spec_path).parent
    if op.get("dem") and str(op["dem"]) != "auto":
        p = Path(op["dem"])
        p = p if p.is_absolute() else base / p
        return str(p), {"source": "user", "path": p.name}
    return mp.ops_dem_resolver(plan.spec, base)(bounds, float(op.get("dem_res_m", 30)))


def links(plan, l, col: dict, brand: str) -> tuple[list[dict], dict, list[str], list[dict]]:
    """For a line_of_sight layer's links on the page: (tube features, {key: profile SVG}, notes, findings). Each link's profile is computed
    again from the op's own DEM, heights, k and frequency (terrain.line_of_sight), so the 3D line and the chart show the same ray."""
    import profile_chart as pc
    import terrain as te
    op = los_op(l) or {}
    feats, profiles, notes, fs = [], {}, [], []
    lons = [c[0] for f in col["features"] for c in f["geometry"]["coordinates"]]
    lats = [c[1] for f in col["features"] for c in f["geometry"]["coordinates"]]
    if not lons:
        return feats, profiles, notes, fs
    try:
        dem, rec = link_dem(plan, l, op, (min(lons) - 0.02, min(lats) - 0.02, max(lons) + 0.02, max(lats) + 0.02))
    except Exception as ex:  # noqa: BLE001 - the links stay flat; the page still builds
        fs.append(finding("MAP-WEB-023", "WARN", f"layer '{l.id}': the DEM of its line-of-sight links could not be read ({str(ex)[:160]}); the links are drawn flat", layer=l.id))
        return feats, profiles, notes, fs
    k = float(op.get("k", te.K_DEFAULT))
    freq = float(op["freq_ghz"]) if op.get("freq_ghz") is not None else None
    credit = pc.dem_credit(rec, dem)
    for i, f in enumerate(col["features"]):
        p = f["properties"]
        cs = f["geometry"]["coordinates"]
        a, b = cs[0], cs[-1]
        ha, hb = float(p.get("h_a_m") or 30.0), float(p.get("h_b_m") or 30.0)
        try:
            r = te.line_of_sight(dem, (a[0], a[1]), (b[0], b[1]), ha, hb, k, freq)
        except te.TerrainError as ex:
            fs.append(finding("MAP-WEB-023", "WARN", f"layer '{l.id}': link {p.get('a')}-{p.get('b')}: {ex}; drawn flat", layer=l.id))
            continue
        key = f"{l.id}/{i}"
        p["_p"] = key
        prof = np.array(r["profile"], float)
        pts, d, _D = te.geodesic_points(tuple(a[:2]), tuple(b[:2]), len(prof))
        keep = {kk: p[kk] for kk in ("status", "_c", "_p") if kk in p}           # the popup reads the rest from the link itself (_p)
        feats += tube_features(pts, prof[:, 0], prof[:, 2], keep, (a[1] + b[1]) / 2)
        names = (str(p.get("a", "A")), str(p.get("b", "B")))
        ch = pc.draw(r, brand=brand, names=names, credit=credit, size="web", chart_id=f"profile-{names[0]}-{names[1]}")
        fs += [{**x, "layer": l.id} for x in ch["findings"] if x["severity"] == "BLOCK"]
        profiles[key] = page_svg(ch["svg"], f"gmp{i}")
    notes.append(f"layer '{l.id}': {len(profiles)} link(s) drawn in 3D between antenna tops at the ray's clearance above the ground, each with its profile "
                 f"chart in the popup ({_dem_label(rec, dem)}, k = {k:.3g}" + (f", {freq:g} GHz" if freq else "") + ")")
    return feats, profiles, notes, fs


def _dem_label(rec: dict, dem: str) -> str:
    return {"3dep": "USGS 3DEP", "terrarium": "Terrain Tiles on AWS"}.get((rec or {}).get("source"), Path(str(dem)).name)


def page_svg(svg: str, prefix: str) -> str:
    """A chart SVG made fit to sit inside the page: the page has Inter already (the chart's embedded copy is dropped), and its ids get a
    prefix so they cannot clash with the page's own (gm-title is the page's title)."""
    s = re.sub(r"@font-face\{[^}]*\}", "", svg, count=1)
    s = s.replace('id="gm-', f'id="{prefix}-').replace("url(#gm-", f"url(#{prefix}-")
    s = re.sub(r'aria-labelledby="([^"]*)"', lambda m: 'aria-labelledby="' + " ".join(
        prefix + "-" + x[3:] if x.startswith("gm-") else x for x in m.group(1).split()) + '"', s)
    s = re.sub(r'(<svg\b[^>]*?)\swidth="[^"]*" height="[^"]*"', r'\1 width="100%"', s, count=1)
    return s


def b64_json(obj) -> str:
    return base64.b64encode(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------------------------------------------- terrain tiles
def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    o = math.pi * 6378137.0
    s = 2 * o / (1 << z)
    return -o + x * s, o - (y + 1) * s, -o + (x + 1) * s, o - y * s


def _merc(lon, lat):
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    return lon * math.pi * 6378137.0 / 180.0, math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * 6378137.0


def _unmerc(x, y):
    return x * 180.0 / (math.pi * 6378137.0), math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)


def encode_terrarium(v: np.ndarray) -> bytes:
    """Elevations (metres) as a Terrarium PNG: (R * 256 + G + B / 256) - 32768, the fraction kept to 1/8 m (smaller files, no visible step)."""
    from PIL import Image
    e = np.clip(np.asarray(v, float) + 32768.0, 0, 65535.99)
    whole = np.floor(e)
    frac = np.clip(np.round((e - whole) * 8.0) * 32.0, 0, 224)
    rgb = np.stack([np.floor(whole / 256.0), np.mod(whole, 256.0), frac], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def decode_terrarium(png: bytes) -> np.ndarray:
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64)
    return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0


def dem_info(dem_path) -> dict:
    import rasterio
    from rasterio.warp import transform_bounds
    with rasterio.open(dem_path) as ds:
        b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
        res = abs(ds.transform.a)
        if ds.crs and ds.crs.is_geographic:
            res = res * 111_320.0 * math.cos(math.radians((b[1] + b[3]) / 2))
    return {"bounds": b, "res_m": float(res)}


def dem_tiles(dem_path, window_wgs84, *, max_bytes: int = DEM_MAX_BYTES, max_zoom: int = DEM_MAX_ZOOM, margin: dict | None = None) -> dict:
    """Terrarium tiles of the DEM for the window (lon/lat). -> {index {"z/x/y": [offset, length]}, blob, zmin, zmax, ideal_zoom, capped,
    bounds (what the tiles cover, lon/lat), res_m (a pixel at zmax), dem_res_m, tiles, bytes, base_m, min_m, max_m, sha256}.

    The window is the DEM's extent inside the page's window; around it the tiles reach a margin of TAPER_SHARE of its width, where the
    ground eases from the nearest DEM value down to the window's lowest ground. Elevations are carried relative to that lowest ground
    (base_m), so beyond the tiles the flat map meets the terrain at the same height."""
    try:
        import rasterio
        from rasterio.warp import reproject, Resampling
        from rasterio.transform import from_bounds
        from scipy import ndimage
    except ImportError as e:                      # both SOFT: the page is then 3D on flat ground (MAP-WEB-023) with this as the reason
        raise Web3DError(f"terrain tiles need rasterio and scipy (pip install rasterio scipy); {e.name or 'one'} is missing") from None
    info = dem_info(dem_path)
    db = info["bounds"]
    w = (max(window_wgs84[0], db[0]), max(window_wgs84[1], db[1]), min(window_wgs84[2], db[2]), min(window_wgs84[3], db[3]))
    if w[0] >= w[2] or w[1] >= w[3]:
        raise Web3DError("the DEM does not cover the page's window")
    lat = (w[1] + w[3]) / 2
    x0, y0 = _merc(w[0], w[1])
    x1, y1 = _merc(w[2], w[3])
    taper = TAPER_SHARE * max(x1 - x0, y1 - y0)
    cover = (x0 - taper, y0 - taper, x1 + taper, y1 + taper)
    res = max(info["res_m"], 1.0)
    ideal = int(min(max_zoom, max(0, math.ceil(math.log2(M_EARTH * math.cos(math.radians(lat)) / (DEM_TILE_PX * res))))))
    o = math.pi * 6378137.0
    fine_cover = cover
    msrc = None
    if margin:                                     # 1.6.0 (R4-SKY): the ground the camera can see, two zooms coarser than the window's
        mx0, my0 = _merc(margin["bounds"][0], margin["bounds"][1])
        mx1, my1 = _merc(margin["bounds"][2], margin["bounds"][3])
        mtaper = 0.05 * max(mx1 - mx0, my1 - my0)
        cover = (min(mx0, cover[0]) - mtaper, min(my0, cover[1]) - mtaper, max(mx1, cover[2]) + mtaper, max(my1, cover[3]) + mtaper)
        fine_cover = (x0 - 0.02 * (x1 - x0), y0 - 0.02 * (y1 - y0), x1 + 0.02 * (x1 - x0), y1 + 0.02 * (y1 - y0))
        x0, y0, x1, y1 = mx0, my0, mx1, my1          # the taper starts beyond the footprint
        taper = mtaper
    zlow = max(0, min(ideal, int(math.floor(math.log2(2 * o / max(cover[2] - cover[0], cover[3] - cover[1]))))))
    zmargin = max(zlow, ideal - 2)
    base = None
    index, parts, off = {}, [], 0
    zmax = zlow
    capped = False
    vmin, vmax = math.inf, -math.inf
    if margin:
        with rasterio.open(margin["path"]) as ms:
            msrc = (ms.read(1, masked=True).astype("float64").filled(np.nan), ms.transform, ms.crs, ms.nodata)
            if msrc[3] is not None:
                msrc[0][msrc[0] == msrc[3]] = np.nan
            msrc[0][(msrc[0] < -1000) | (msrc[0] > 9000)] = np.nan
    with rasterio.open(dem_path) as ds:
        src = ds.read(1, masked=True).astype("float64").filled(np.nan)
        if ds.nodata is not None:
            src[src == ds.nodata] = np.nan
        src[(src < -1000) | (src > 9000)] = np.nan
        for z in range(zlow, ideal + 1):
            n = 1 << z
            cz = cover if (not margin or z <= zmargin) else fine_cover     # finer than the margin's zooms, the window only
            c0 = int(math.floor((cz[0] + o) / (2 * o) * n))
            c1 = int(math.floor((cz[2] + o) / (2 * o) * n - 1e-9))
            r0 = int(math.floor((o - cz[3]) / (2 * o) * n))
            r1 = int(math.floor((o - cz[1]) / (2 * o) * n - 1e-9))
            c0, r0, c1, r1 = max(c0, 0), max(r0, 0), min(c1, n - 1), min(r1, n - 1)
            W, H = DEM_TILE_PX * (c1 - c0 + 1), DEM_TILE_PX * (r1 - r0 + 1)
            bx0, _, _, by1 = tile_bounds_3857(z, c0, r0)
            _, by0, bx1, _ = tile_bounds_3857(z, c1, r1)
            dst = np.full((H, W), np.nan)
            reproject(src, dst, src_transform=ds.transform, src_crs=ds.crs, dst_transform=from_bounds(bx0, by0, bx1, by1, W, H), dst_crs="EPSG:3857",
                      resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
            if msrc is not None and np.isnan(dst).any():         # the margin DEM where the map's own DEM stops
                mdst = np.full((H, W), np.nan)
                reproject(msrc[0], mdst, src_transform=msrc[1], src_crs=msrc[2], dst_transform=from_bounds(bx0, by0, bx1, by1, W, H), dst_crs="EPSG:3857",
                          resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
                dst = np.where(np.isnan(dst), mdst, dst)
            px = bx0 + (np.arange(W) + 0.5) * (bx1 - bx0) / W
            py = by1 - (np.arange(H) + 0.5) * (by1 - by0) / H
            X, Y = np.meshgrid(px, py)
            inside = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
            valid = np.isfinite(dst) & inside
            if not valid.any():
                continue
            if base is None:
                base = float(np.nanpercentile(dst[valid], 1))
            filled = dst.copy()
            if (~valid).any():
                idx = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
                filled = dst[idx[0], idx[1]]
            dist = np.hypot(np.maximum(np.maximum(x0 - X, X - x1), 0), np.maximum(np.maximum(y0 - Y, Y - y1), 0))
            k = np.clip(1.0 - dist / taper, 0.0, 1.0)
            k = k * k * (3 - 2 * k)                               # smoothstep: the ground leaves the DEM level and arrives flat
            rel = np.maximum(filled - base, 0.0) * k
            vmin, vmax = min(vmin, float(rel[valid].min())), max(vmax, float(rel[valid].max()))
            zparts, zindex, zbytes = [], {}, 0
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    a = rel[(r - r0) * DEM_TILE_PX:(r - r0 + 1) * DEM_TILE_PX, (c - c0) * DEM_TILE_PX:(c - c0 + 1) * DEM_TILE_PX]
                    png = encode_terrarium(a)
                    zindex[f"{z}/{c}/{r}"] = [off + zbytes, len(png)]
                    zparts.append(png)
                    zbytes += len(png)
            if off + zbytes > max_bytes and z > zlow:
                capped = True
                break
            index.update(zindex)
            parts += zparts
            off += zbytes
            zmax = z
    blob = b"".join(parts)
    cw, cs = _unmerc(cover[0], cover[1])
    ce, cn = _unmerc(cover[2], cover[3])
    return {"index": index, "blob": blob, "zmin": zlow, "zmax": zmax, "ideal_zoom": ideal, "capped": capped,
            "bounds": [round(cw, 6), round(cs, 6), round(ce, 6), round(cn, 6)], "res_m": round(M_EARTH * math.cos(math.radians(lat)) / (DEM_TILE_PX * 2 ** zmax), 2),
            "dem_res_m": round(info["res_m"], 2), "tiles": len(index), "bytes": len(blob), "base_m": round(base or 0.0, 2),
            "min_m": round(vmin, 2) if math.isfinite(vmin) else 0.0, "max_m": round(vmax, 2) if math.isfinite(vmax) else 0.0,
            "sha256": hashlib.sha256(blob).hexdigest(), "window": [round(v, 6) for v in w], "margin": bool(margin), "zmargin": zmargin}


def read_back(cfg_dem: dict, blob: bytes) -> list[dict]:
    """MAP-WEB-023 read-back of the embedded terrain: the blob's hash, every tile a 512 x 512 Terrarium PNG, elevations within the range
    the page states."""
    out = []
    if hashlib.sha256(blob).hexdigest() != cfg_dem.get("sha256"):
        out.append(finding("MAP-WEB-023", "BLOCK", "the embedded terrain does not match its record (hash); the page was changed after it was built"))
        return out
    lo, hi = min(0.0, float(cfg_dem.get("min_m", 0))) - 1.0, float(cfg_dem.get("max_m", 0)) + 1.0
    bad = []
    for key, (o, n) in (cfg_dem.get("index") or {}).items():
        part = blob[o:o + n]
        try:
            v = decode_terrarium(part)
        except Exception:  # noqa: BLE001 - an unreadable tile is the finding
            bad.append(f"{key}: not a PNG")
            continue
        if v.shape != (DEM_TILE_PX, DEM_TILE_PX):
            bad.append(f"{key}: {v.shape[1]} x {v.shape[0]} px")
        elif v.min() < lo - 0.5 or v.max() > hi + 0.5:
            bad.append(f"{key}: {v.min():.0f} to {v.max():.0f} m, outside {lo:.0f} to {hi:.0f}")
    if bad:
        out.append(finding("MAP-WEB-023", "BLOCK", f"{len(bad)} embedded terrain tile(s) do not read back: {'; '.join(bad[:4])}", bad[:20]))
    return out


# ---------------------------------------------------------------------------------------------------------------- the style
def _vis(on: bool) -> dict:
    return {"visibility": "visible" if on else "none"}


def band_opacity(lo: float, hi: float | None):
    """A width band is shown by its opacity at the map's zoom, not by the layer's minzoom and maxzoom: on a tilted view MapLibre cuts the
    near tiles a zoom deeper than the map's zoom, and a layer outside its zoom range is not built for those tiles (the near masts and
    links went missing)."""
    if lo <= 0:
        return ["step", ["zoom"], 1, hi, 0]
    if hi is None:
        return ["step", ["zoom"], 0, lo, 1]
    return ["step", ["zoom"], 0, lo, 1, hi, 0]


def style_3d(st: dict, *, t: dict, s3: dict, defs: list[dict], dem: dict | None, relief_id: str | None, relief_opacity: float,
             masts: dict, tubes: dict, extruded: dict) -> None:
    """Adds the 3D sources and layers to the page's MapLibre style, in place: the raster-DEM sources and the hillshade (above the bundled
    background, under the data), the extrusions, mast columns and links (above the data, under every label). Layers drawn only in 2D get
    `gm:mode: 2d`, the 3D ones `gm:mode: 3d`; the page shows one set or the other."""
    import map_raster
    start3d = s3["start"] == "3d"
    layers = st["layers"]
    first_data = next((i for i, l in enumerate(layers) if (l.get("metadata") or {}).get("gm:role") not in ("bundled", "basemap", "basemap-label")), len(layers))
    first_sym = next((i for i, l in enumerate(layers) if l.get("type") == "symbol"), len(layers))
    under, over = [], []
    if dem:
        src = {"type": "raster-dem", "tiles": ["gmdem://{z}/{x}/{y}"], "tileSize": DEM_TILE_PX, "encoding": "terrarium", "minzoom": dem["zmin"],
               "maxzoom": dem["zmax"], "bounds": dem["bounds"], "attribution": ""}
        st["sources"]["gm-dem"] = dict(src)
        st["sources"]["gm-dem-hs"] = dict(src)             # MapLibre asks for a second source for the hillshade; both read the same tiles
        for s in st["sources"].values():                   # a GeoJSON source cut coarser than the terrain cannot be placed on it (MapLibre warns)
            if s.get("type") == "geojson" and s.get("maxzoom") is not None and s["maxzoom"] < dem["zmax"]:
                s["maxzoom"] = dem["zmax"]
        shadow, light = map_raster.relief_colors(t)
        if relief_id:
            d = next((x for x in defs if x["id"] == relief_id), None)
            under.append({"id": "gm-hillshade", "type": "hillshade", "source": "gm-dem-hs", "layout": _vis(bool(d and d["visible"])),
                          "paint": {"hillshade-shadow-color": shadow, "hillshade-highlight-color": light, "hillshade-accent-color": shadow,
                                    "hillshade-exaggeration": round(max(0.3, min(0.75, relief_opacity * 1.8)), 2), "hillshade-illumination-direction": 315},
                          "metadata": {"gm:layer": relief_id, "gm:role": "hillshade"}})
        if start3d:
            st["terrain"] = {"source": "gm-dem", "exaggeration": s3["exaggeration_value"]}
    if relief_id and dem:                                  # the print relief's picture gives way to the hillshade drawn from the terrain tiles
        layers[:] = [l for l in layers if not ((l.get("metadata") or {}).get("gm:layer") == relief_id and (l.get("metadata") or {}).get("gm:role") == "image")]
        st["sources"].pop(f"img-{relief_id}", None)
        first_data = next((i for i, l in enumerate(layers) if (l.get("metadata") or {}).get("gm:role") not in ("bundled", "basemap", "basemap-label")), len(layers))
        first_sym = next((i for i, l in enumerate(layers) if l.get("type") == "symbol"), len(layers))
    by_id = {d["id"]: d for d in defs}
    for lid, e in extruded.items():
        d = by_id[lid]
        src = {"source": f"data-{lid}"}
        for l in layers:
            md = l.get("metadata") or {}
            if md.get("gm:layer") == lid and md.get("gm:role") in ("data",):
                src = {k: l[k] for k in ("source", "source-layer") if k in l}           # a tiled layer's extrusion reads its tiles
                md["gm:mode"] = "2d"
                l["metadata"] = md
                if not start3d:
                    continue
                l.setdefault("layout", {})["visibility"] = "none"
        color = ["coalesce", ["get", "_c"], e["color"]]
        over.append({"id": f"{lid}--ext", "type": "fill-extrusion", **src, "layout": _vis(d["visible"] and start3d),
                     "paint": {"fill-extrusion-color": color, "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": ["get", "_b"],
                               "fill-extrusion-opacity": e["opacity"], "fill-extrusion-vertical-gradient": True},
                     "metadata": {"gm:layer": lid, "gm:role": "extrusion", "gm:mode": "3d"}})
    for lid, m in masts.items():
        d = by_id[lid]
        for bi, (lo, hi, _w) in enumerate(m["bands"]):
            for part, color in (("column", t["label"]), ("head", t["emphasis"])):
                ly = {"id": f"{lid}--mast{bi}{part[0]}", "type": "fill-extrusion", "source": f"mast-{lid}",
                      "filter": ["all", ["==", ["get", "_band"], bi], ["==", ["get", "_part"], part]], "layout": _vis(d["visible"] and start3d),
                      "paint": {"fill-extrusion-color": color, "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": ["get", "_b"],
                                "fill-extrusion-opacity": band_opacity(lo, hi), "fill-extrusion-vertical-gradient": False},
                      "metadata": {"gm:layer": lid, "gm:role": "extrusion", "gm:mode": "3d", "gm:part": "mast"}}
                over.append(ly)
    for lid, tb in tubes.items():
        d = by_id[lid]
        for l in layers:
            md = l.get("metadata") or {}
            if md.get("gm:layer") == lid and md.get("gm:role") == "data":
                md["gm:mode"] = "2d"
                l["metadata"] = md
                if start3d:
                    l.setdefault("layout", {})["visibility"] = "none"
        for bi, (lo, hi, _w) in enumerate(tb["bands"]):
            ly = {"id": f"{lid}--tube{bi}", "type": "fill-extrusion", "source": f"tube-{lid}", "filter": ["==", ["get", "_band"], bi],
                  "layout": _vis(d["visible"] and start3d),
                  "paint": {"fill-extrusion-color": ["coalesce", ["get", "_c"], tb["color"]], "fill-extrusion-height": ["get", "_h"],
                            "fill-extrusion-base": ["get", "_b"], "fill-extrusion-opacity": band_opacity(lo, hi), "fill-extrusion-vertical-gradient": False},
                  "metadata": {"gm:layer": lid, "gm:role": "extrusion", "gm:mode": "3d", "gm:part": "link"}}
            over.append(ly)
    for k, v in masts.items():
        st["sources"][f"mast-{k}"] = {"type": "geojson", "data": {"gm": f"mast-{k}"}, "attribution": "", "tolerance": 0}
    for k, v in tubes.items():
        st["sources"][f"tube-{k}"] = {"type": "geojson", "data": {"gm": f"tube-{k}"}, "attribution": "", "tolerance": 0}
    st["layers"] = layers[:first_data] + under + layers[first_data:first_sym] + over + layers[first_sym:]
    st.setdefault("metadata", {})["gm:3d"] = True


# ---------------------------------------------------------------------------------------------------------------- the page
def page(plan, s3: dict, st: dict, defs: list[dict], collections: dict, bbox, keep_fields: list[str]) -> dict:
    """Everything 3D on one MapLibre page: the terrain tiles, the extrusions, the masts, the links and their profiles, added to the style
    `st` and to `collections` in place. -> {cfg (the page's view3d), dem (the tiles, or None), profiles {key: svg}, notes, findings, report}."""
    import map_plan as mp
    out = {"notes": [], "findings": [], "dem": None, "profiles": {}, "cfg": None, "report": {}}
    tier = mp.scale_tier(mp.scale_denominator(plan))["tier"]
    ex = exaggeration(s3["exaggeration"], tier)
    s3 = dict(s3, exaggeration_value=ex)
    lat = (bbox[1] + bbox[3]) / 2
    dem, relief_id, relief_op, item = None, None, 0.35, None
    if s3["terrain"]:
        item = next((b for b in plan.basemap if b.get("kind") == "hillshade" and b.get("dem_path")), None)
        if item is None:
            why = "; ".join(n for n in plan.notes if ("terrain" in n or "hillshade" in n) and "not drawn" in n)[:300] or "no terrain block or hillshade layer reached a DEM"
            out["findings"].append(finding("MAP-WEB-023", "WARN", f"terrain is asked for but the map has no DEM ({why}); the page is 3D on flat ground"))
        else:
            w = list(bbox)
            pw, ph = (w[2] - w[0]) * 0.35, (w[3] - w[1]) * 0.35
            win = (w[0] - pw, w[1] - ph, w[2] + pw, w[3] + ph)
            margin = None
            top = float(s3.get("top_pitch") or s3["pitch"])             # the page's highest pitch: its own or a story chapter's
            mp_ = max(60.0, min(MAX_PITCH, top + 10.0))
            fp = ground_footprint(tuple(w), mp_)
            s3["footprint"] = fp
            try:                                     # 1.6.0 (R4-SKY): a DEM wider than the view, two zooms coarser, for the ground the camera sees
                margin = margin_dem(item, fp["bounds"])
            except Exception as e:  # noqa: BLE001 - the page keeps the window's DEM and a lower pitch
                out["notes"].append(f"terrain: no DEM margin beyond the window ({str(e)[:120]})")
            if margin is None:
                covered = max(top, max_pitch_covered(tuple(w), win))     # never below the page's own pitch or a chapter's
                if covered < mp_:
                    s3["max_pitch_override"] = covered
                    out["findings"].append(finding("MAP-WEB-044", "INFO", f"the ground seen at a pitch of {mp_:g} degrees reaches beyond the terrain the page "
                                                   f"carries, so the page tilts to {covered:g} degrees at most", {"max_pitch": covered, "asked": mp_}))
            try:
                dem = dem_tiles(item["dem_path"], win, max_bytes=int(s3["max_dem_mb"] * 1048576), margin=margin)
            except Exception as e:  # noqa: BLE001 - the page is still 3D, on flat ground
                out["findings"].append(finding("MAP-WEB-023", "WARN", f"the terrain could not be made from {Path(item['dem_path']).name} ({str(e)[:160]}); the page is 3D on flat ground"))
                dem = None
    if dem:
        relief_id = f"relief-{item.get('layer_id')}"
        relief_op = float((item.get("image") or {}).get("opacity", 0.35))
        src = (item.get("record") or {}).get("source_name") or Path(item["dem_path"]).name
        out["notes"].append(f"terrain: {src}, {dem['dem_res_m']:g} m cells, as {dem['tiles']} Terrarium tiles in the page ({dem['bytes'] / 1048576:.1f} MB), "
                            f"zooms {dem['zmin']} to {dem['zmax']}: about {dem['res_m']:g} m a pixel at zoom {dem['zmax']}; exaggeration {ex:g}; heights drawn "
                            f"from the window's lowest ground ({dem['base_m']:,.0f} m)")
        if dem["capped"]:
            out["findings"].append(finding("MAP-WEB-023", "WARN", f"the terrain is capped at zoom {dem['zmax']} (about {dem['res_m']:g} m a pixel) to keep it under "
                                           f"{s3['max_dem_mb']:g} MB; the DEM's {dem['dem_res_m']:g} m cells would need zoom {dem['ideal_zoom']}. Past it MapLibre enlarges "
                                           "the last tiles; webmap.3d.max_dem_mb raises the cap", {"zoom": dem["zmax"], "ideal": dem["ideal_zoom"]}))
        for d in defs:
            if d["id"] == relief_id:
                d["kind"] = "hillshade"
                d.pop("url", None)
        flat = encode_terrarium(np.zeros((DEM_TILE_PX, DEM_TILE_PX)))
        dem["index"]["flat"] = [len(dem["blob"]), len(flat)]
        dem["blob"] += flat
        dem["sha256"] = hashlib.sha256(dem["blob"]).hexdigest()
        dem["bytes"] = len(dem["blob"])
    extruded = {d["id"]: {"color": d["style"]["fill"], "opacity": 0.92} for d in defs if d.get("d3") == "extrude"}
    masts, tubes = {}, {}
    for d in defs:
        if d.get("d3") == "mast":
            keep = list(dict.fromkeys(((d.get("popup") or {}).get("fields") or []) + [(d.get("popup") or {}).get("title")] + list(keep_fields) + ["_t", "_c", "_k"]))
            collections[f"mast-{d['id']}"] = mast_collection(collections[d["id"]], [k for k in keep if k])
            masts[d["id"]] = {"bands": band_widths(lat, MAST_PX)}
        elif d.get("d3") == "links":
            l = next(x for x in plan.layers if x.id == d["id"])
            feats, profs, n, f = links(plan, l, collections[d["id"]], plan.brand)
            out["notes"] += n
            out["findings"] += f
            if feats:
                collections[f"tube-{d['id']}"] = {"type": "FeatureCollection", "features": feats}
                tubes[d["id"]] = {"bands": band_widths(lat, TUBE_PX), "color": d["style"]["stroke"]}
                out["profiles"].update(profs)
    style_3d(st, t=plan.tokens, s3=s3, defs=defs, dem=dem, relief_id=relief_id, relief_opacity=relief_op, masts=masts, tubes=tubes, extruded=extruded)
    out["dem"] = dem
    out["cfg"] = {"pitch": s3["pitch"], "bearing": s3["bearing"], "terrain": bool(dem), "exaggeration": ex, "toggle": s3["toggle"], "start": s3["start"],
                  "maxPitch": s3.get("max_pitch_override") or max(60.0, min(MAX_PITCH, float(s3.get("top_pitch") or s3["pitch"]) + 10.0))}
    s3["maxPitch"] = out["cfg"]["maxPitch"]
    if sky_wanted(s3):                                    # 1.6.0 (R4-SKY): a sky from the page's tokens above the horizon
        st["sky"] = sky_block(plan.tokens)
        out["notes"].append("sky: the background with a little emphasis above the horizon, haze toward it (webmap.3d.sky)")
    out["report"] = {"pitch": s3["pitch"], "bearing": s3["bearing"], "exaggeration": ex, "extruded": sorted(extruded), "masts": sorted(masts), "links": sorted(tubes),
                     "profiles": len(out["profiles"]), "terrain": ({k: dem[k] for k in ("tiles", "bytes", "zmin", "zmax", "res_m", "dem_res_m", "capped", "base_m", "ideal_zoom")}
                                                                    if dem else None)}
    return out


def dem_config(dem: dict) -> dict:
    """What the page's config says about its terrain (the tiles themselves go in the #gmap-dem block)."""
    return {"el": "gmap-dem", "index": dem["index"], "zmin": dem["zmin"], "zmax": dem["zmax"], "bounds": dem["bounds"], "res_m": dem["res_m"],
            "dem_res_m": dem["dem_res_m"], "tiles": dem["tiles"], "bytes": dem["bytes"], "sha256": dem["sha256"], "min_m": dem["min_m"], "max_m": dem["max_m"],
            "base_m": dem["base_m"], "capped": dem["capped"], "margin": bool(dem.get("margin"))}
