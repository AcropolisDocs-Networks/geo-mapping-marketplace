#!/usr/bin/env python3
"""webmap: MapSpec in, one self-contained web map page out (spec 7.2 webmap-render and webmap-controls, 10.1).

    webmap.py --spec map.yaml [--out DIR] [--brand SLUG]

The page is a single .html file: its map engine and style inlined with their licences, Inter embedded, brand chrome from the brand pack's
screen or print tokens, the data embedded, a bundled background that works with no network, and, when the MapSpec names a tile provider
the registry allows, tiles from that provider's host only. A Content-Security-Policy in the page lets it call nothing but the hosts in its
own manifest. The accreditation is hidden metadata only. `build_map.py --formats html` calls this.

Two engines (M14): Leaflet (BSD-2) draws GeoJSON in the page; MapLibre GL JS (BSD-3, with the `pmtiles` reader) draws GeoJSON and, for
large layers, PMTiles archives embedded in the page, labels as real symbol layers in Inter, and a Protomaps vector basemap. `webmap.engine:
auto` (the default) picks MapLibre when the page carries more than AUTO_MAPLIBRE_FEATURES features, a layer goes into the page as tiles, the
basemap is vector tiles, or the time slider or the swipe compare is on; Leaflet otherwise. The page's controls are the same on both: one
controller (assets/web/page.js) and one small adapter per engine (assets/web/engine-leaflet.js, engine-maplibre.js).
"""
from __future__ import annotations

import argparse
import base64
import functools
import datetime as dt
import hashlib
import html
import json
import math
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import geo_locale as gl  # noqa: E402
import map_look  # noqa: E402
import map_plan as mp  # noqa: E402
import provenance  # noqa: E402
import tile_registry as tr  # noqa: E402
import text_lint  # noqa: E402

LEAFLET_VERSION = "1.9.4"
OPEN_LICENSES = ("public-domain", "open", "attribution", "share-alike")
BUNDLED_MAX_ZOOM = 8            # the bundled background (Natural Earth) holds its shape to about this zoom; past it, it is faded, not a limit
BUNDLED_ZOOM_IN = 4             # with only the bundled background, the page still zooms this far past the zoom that fits the data
OVERZOOM_LEVELS = 2             # past a provider's last zoom the page enlarges its last tiles, up to this many levels (4x), and never asks for tiles that do not exist
LARGE_LAYER_FEATURES = 2000     # above this many line or polygon features, the page carries only those within the map window plus a margin
SIMPLIFY_DIV = 1500             # large layers are simplified to a tolerance of this fraction of the map's width
WINDOW_PAD = 0.5                # the margin, as a fraction of the map's width and height on each side
DEFAULT_MAX_ZOOM = 16
COORD_DECIMALS = 5
CONTROL_DEFAULTS = {"title_panel": True, "legend": True, "layer_switcher": True, "basemap_switcher": False, "popups": True, "scale": True,
                    "compass": False, "search": False, "fullscreen": True, "data_download": False, "share_state_in_url": True,
                    "print_view": True, "error_banners": True, "logo": True,
                    # M14: an opacity slider per layer, clustering of large point layers, measure, the time slider and the swipe compare (each
                    # only when webmap.time / webmap.compare is set), and image export (MapLibre; Leaflet saves through the print view)
                    "opacity": True, "cluster": True, "measure": True, "time_slider": True, "compare": True, "export_image": True}
ENGINES = ("auto", "leaflet", "maplibre")
AUTO_MAPLIBRE_FEATURES = 20_000   # engine auto: above this many features on the page, MapLibre
SEARCH_TILED_MAX = 50_000         # a tiled layer's search index is carried as plain JSON up to this many features
SEARCH_PACKED_MAX = 500_000       # 1.0.0 (TODO M14): above it, packed (texts and positions, gzip) and unpacked by the browser, up to this many
TIME_STEPS = ("day", "week", "month", "quarter", "year")


def finding(fid: str, sev: str, message: str, detail=None, layer: str | None = None) -> dict:
    d = {"id": fid, "severity": sev, "message": message, "detail": detail}
    if layer:
        d["layer"] = layer
    return d


@dataclass
class WebPage:
    html: str
    findings: list
    report: dict
    stem: str
    plan: object = None
    rendered: object = None
    hosts: list = field(default_factory=list)
    basemaps: list = field(default_factory=list)
    records: list = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------------- configuration
def config_of(spec: dict) -> dict:
    wm = dict(spec.get("webmap") or {})
    controls = dict(CONTROL_DEFAULTS)
    controls.update(wm.get("controls") or {})
    fb = {"static_image": True, "error_banners": True} | (wm.get("fallbacks") or {})
    controls["error_banners"] = bool(controls["error_banners"] and fb["error_banners"])
    return {"engine": wm.get("engine", "auto"), "theme": wm.get("theme") or spec.get("theme") or "light", "delivery": wm.get("delivery") or ["file"],
            "basemaps": wm.get("basemaps"), "controls": controls, "view": wm.get("initial_view") or {}, "fallbacks": fb,
            "search_field": wm.get("search_field"), "confirm_publish": bool(wm.get("confirm_publish")),
            "time": wm.get("time"), "compare": wm.get("compare"), "basemap_file": wm.get("basemap_file") or "embed",
            "asked_controls": dict(wm.get("controls") or {}), "story": wm.get("story"), "panel": wm.get("panel")}


def web_edit(spec: dict) -> None:
    """The in-memory edits the web build makes to the spec before planning: the static fallback has no scale bar (the page has its own, true at
    the centre of the view) and draws the bundled basemap; a 3D page with terrain draws the print relief from the same DEM (webmap_3d)."""
    print_edit(spec)
    els = spec.setdefault("elements", {})
    els["scale_bar"] = {"on": False}


def print_edit(spec: dict) -> None:
    """The edits a story chapter's print picture shares with the fallback: the bundled basemap (nothing is fetched) and the 3D page's relief."""
    import webmap_3d as w3
    if (spec.get("basemap") or {}).get("provider") not in (None, "none"):
        spec["basemap"] = dict(spec["basemap"], provider="bundled")
    w3.web_edit(spec)


def fit_zoom(bbox, view_px=(960, 600)) -> int:
    """The Web Mercator zoom at which a lon/lat box fits a typical window (256-pixel tiles, Leaflet's numbering)."""
    lo0, la0, lo1, la1 = (float(v) for v in bbox)
    def my(lat):
        lat = max(min(lat, 85.0511), -85.0511)
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    wx = max((lo1 - lo0) / 360.0, 1e-9)
    wy = max((my(la1) - my(la0)) / (2 * math.pi), 1e-9)
    return max(0, int(math.floor(math.log2(min(view_px[0] / (256 * wx), view_px[1] / (256 * wy))))))


def requested_providers(spec: dict, cfg: dict) -> list[str]:
    if cfg["basemaps"]:
        return list(cfg["basemaps"])
    p = (spec.get("basemap") or {}).get("provider", "bundled")
    return [p]


# ---------------------------------------------------------------------------------------------------------------- data
def round_coords(obj, nd: int = COORD_DECIMALS):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(v), nd) for v in obj[:2]]
        return [round_coords(o, nd) for o in obj]
    return obj


def to_wgs84(gdf):
    return gdf if gdf.crs is None or gdf.crs.to_epsg() == 4326 else gdf.to_crs(4326)


def jsonable(v):
    import numpy as np
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if v != v else (int(v) if float(v).is_integer() and abs(v) < 1e15 else round(float(v), 6))
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def popup_fields(l, label_field: str | None) -> dict:
    pop = dict((l.spec.get("popup") or {}))
    cols = [c for c in l.gdf.columns if c != l.gdf.geometry.name]
    fields = [c for c in (pop.get("fields") or []) if c in cols]
    if not fields:
        pref = [label_field, l.value_field] + [c for c in ("NAME", "name", "NAMELSAD", "STUSPS", "GEOID") if c in cols]
        fields = []
        for c in pref:
            if c and c in cols and c not in fields:
                fields.append(c)
        fields = fields[:6]
    title = pop.get("title") if pop.get("title") in cols else (label_field if label_field in cols else next((c for c in ("NAME", "name", "NAMELSAD") if c in cols), None))
    return {"fields": fields, "title": title}


def layer_collection(plan, l, label_field: str | None, px: dict, time_field: str | None = None, extra: dict | None = None,
                     extra_fields: list | None = None) -> tuple[dict, dict]:
    """(GeoJSON FeatureCollection in lon/lat with only the fields the page uses, popup definition). A time field's value travels as `_tv`
    (turned into the period number `_t` by time_periods); a contour carries `_i` (1 on an index line) and `_e` (its elevation as text).
    `extra` is {property: one value per row} (3D heights `_h`, `_b`); `extra_fields` are more columns to keep (the KPI panel's, M15)."""
    import numpy as np
    from shapely.geometry import mapping
    gdf = to_wgs84(l.gdf)
    pop = popup_fields(l, label_field)
    keep = list(dict.fromkeys(pop["fields"] + ([pop["title"]] if pop["title"] else []) + ([l.value_field] if l.value_field else [])
                              + ([label_field] if label_field else []) + [c for c in (extra_fields or []) if c in gdf.columns]))
    tvals = gdf[time_field].tolist() if time_field and time_field in gdf.columns else None
    contour = l.role == "contour" and "elevation" in gdf.columns
    if contour:
        c_idx = gdf["index"].to_numpy(dtype=bool) if "index" in gdf.columns else np.zeros(len(gdf), bool)
        c_elev = gdf["elevation"].to_numpy(dtype=float)
    assigned = np.asarray(l.cls.assigned) if l.cls is not None else None
    vals = None
    vmax = 1.0
    if l.role == "proportional":
        vals = gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan)
        ok = np.isfinite(vals) & (vals > 0)
        vmax = float(np.nanmax(vals[ok])) if ok.any() else 1.0
    feats = []
    cols = {c: gdf[c].tolist() for c in keep if c in gdf.columns}
    hl = np.asarray(l.highlight, dtype=bool) if getattr(l, "highlight", None) is not None and len(l.highlight) == len(gdf) else None
    if hl is not None:                       # 0.23.0: the print's highlight on the page: the rest dimmed and faded, the subset larger
        import map_look
        base_c = l.stroke if l.kind == "line" else l.fill
    geoms = list(gdf.geometry)
    window = None
    if len(geoms) > LARGE_LAYER_FEATURES:      # a layer far bigger than the map (a national road or tower file under a state map) would bloat the page
        w = mp._wgs84_extent(plan)
        pw, ph = (w[2] - w[0]) * WINDOW_PAD, (w[3] - w[1]) * WINDOW_PAD
        window = (w[0] - pw, w[1] - ph, w[2] + pw, w[3] + ph)
        tol = (w[2] - w[0]) / SIMPLIFY_DIV
    skipped = 0
    for i, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        if window is not None:
            b = g.bounds
            if b[2] < window[0] or b[0] > window[2] or b[3] < window[1] or b[1] > window[3]:
                skipped += 1
                continue
            if l.kind != "point":                   # points are windowed, never simplified: a site is where it is
                g = g.simplify(tol, preserve_topology=True)
                if g.is_empty:
                    continue
        props = {c: jsonable(cols[c][i]) for c in cols}
        if l.cls is not None and assigned is not None and i < len(assigned):
            ci = int(assigned[i])
            props["_c"] = plan.tokens["no_data"] if ci < 0 else l.colors[ci]
            props["_k"] = "no data" if ci < 0 else l.cls.labels[ci]
        if vals is not None:
            v = vals[i]
            if not (v == v) or v <= 0:
                continue
            props["_r"] = round(max(px["min_r"], px["max_r"] * (v / vmax) ** 0.5), 2)        # radius ~ sqrt(value): area is proportional (MAP-CART-010)
        if tvals is not None:
            props["_tv"] = tvals[i]
        if contour:
            props["_i"] = int(bool(c_idx[i]))
            props["_e"] = f"{c_elev[i]:,.0f}"
        for k, vs in (extra or {}).items():
            props[k] = vs[i]
        if hl is not None:
            props["_hl"] = int(hl[i])
            if not hl[i]:
                c0 = props.get("_c") or base_c
                if isinstance(c0, str) and c0.startswith("#"):
                    props["_c"] = map_look.dim(c0)
        m = mapping(g)
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": m["type"], "coordinates": round_coords(m["coordinates"])}})
    if window is not None and skipped:
        plan.notes.append(f"layer '{l.id}': the page carries the {len(feats):,} of {len(geoms):,} features near the map window"
                          + ("" if l.kind == "point" else ", simplified") + "; the rest lie far outside it")
    if hl is not None:
        feats.sort(key=lambda f: f["properties"].get("_hl", 1))         # the rest first, so the highlighted features are drawn over it
    return {"type": "FeatureCollection", "features": feats}, pop


NAMES_MAX_FEATURES = 900        # the page carries at most this many place, water and admin names (the highest tiers first)
NAMES_ID = "gm-names"
ADMIN_NAME_ZOOM = (4, 8)        # state and province names are shown between these zooms


def names_collection(plan, bbox, fit: int, max_zoom: int) -> tuple[dict, dict]:
    """The place, water and admin names a page may show, as GeoJSON points in lon/lat (the same selection as the print map at the page's
    first zoom, extended to what its deepest zoom can carry). Each feature carries `tier`, `min_zoom` (Natural Earth's own, so zooming in
    reveals the next tier), `max_zoom` for admin names, `prio` for the page's collision pass and `kind`. Returns (collection, stats)."""
    import shapely
    from shapely.geometry import box as sbox
    cfg = mp.names_config(plan.spec)
    stats = {"on": cfg["on"], "places": 0, "water": 0, "admin": 0}
    if not cfg["on"]:
        return {"type": "FeatureCollection", "features": []}, stats
    lat = (bbox[1] + bbox[3]) / 2
    den_first = mp.denominator_for_zoom(fit, lat)
    den_deep = mp.denominator_for_zoom(max_zoom, lat)
    tier_first, tier_deep = mp.scale_tier(den_first), mp.scale_tier(den_deep)
    stats["tier"] = tier_first["tier"]
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    win = (max(-180.0, bbox[0] - w * WINDOW_PAD), max(-85.0, bbox[1] - h * WINDOW_PAD), min(180.0, bbox[2] + w * WINDOW_PAD), min(85.0, bbox[3] + h * WINDOW_PAD))
    wbox = sbox(*win)
    feats: list[dict] = []

    def load(key):
        try:
            gdf, _e = mp.load_ref_layer(mp.NAMES_REFS[key])
        except mp.PlanError:
            return None
        g = to_wgs84(gdf)
        return g[g.geometry.intersects(wbox)]

    def feat(x, y, name, tier, min_zoom, prio, kind, max_zoom_=None):
        p = {"name": name, "tier": tier, "min_zoom": round(float(min_zoom), 1), "prio": round(float(prio), 1), "kind": kind}
        if max_zoom_ is not None:
            p["max_zoom"] = max_zoom_
        return {"type": "Feature", "properties": p, "geometry": {"type": "Point", "coordinates": [round(float(x), COORD_DECIMALS), round(float(y), COORD_DECIMALS)]}}

    import map_render as mr
    places = load("places") if cfg["places"] != "none" else None
    if places is not None and len(places):
        sel = mr.select_places(places, cfg, tier_deep)
        # 1.5.0 (R3-SCRIPT): the same name choice as the print map. 1.9.0: the page's label glyphs are Inter's Latin, Greek and Cyrillic
        # ranges; a name in another script is given in English there and said (MAP-LOC-003)
        import webmap_maplibre as wml
        try:
            web_names, _alt, _left = mp.choose_place_names(sel, cfg) if len(sel) else ([], [], set())
        except Exception:  # noqa: BLE001 - the plain names
            web_names = list(sel["name"].astype(str))
        for i in range(len(sel)):
            g = sel.geometry.iloc[i]
            tr = str(sel["_tier"].iloc[i])
            mz = float(sel["min_zoom"].iloc[i]) if "min_zoom" in sel.columns and sel["min_zoom"].iloc[i] == sel["min_zoom"].iloc[i] else 6.0
            if tr == "capital":
                mz = min(mz, 3.0)
            nm = str(web_names[i])
            # 1.9.0: Greek and Cyrillic names are drawn as they are (the page's glyphs cover them now); a name with a character the
            # glyph files do not hold still falls back to English
            if not wml.glyphs_cover(nm):
                from fontTools import unicodedata as ud_
                stats.setdefault("script_fallback", set()).update({ud_.script(ch) for ch in nm} - {"Zyyy", "Zinh", "Zzzz", "Latn"})
                nm = str(sel["name"].iloc[i])
            if not nm.isascii():                             # which Inter range files the page carries beside Latin
                from fontTools import unicodedata as ud2_
                plan.web_name_scripts = set(getattr(plan, "web_name_scripts", ())) | ({ud2_.script(ch) for ch in nm} & set(INTER_SCRIPT_FACES))
            feats.append(feat(g.x, g.y, nm, tr, mz, sel["_prio"].iloc[i], "place"))
            stats["places"] += 1
    if cfg["water"]:
        lakes = load("lakes")
        if lakes is not None and len(lakes):
            lakes = lakes[lakes["name"].notna()]
            if tier_deep["water"] is not None:
                lakes = lakes[lakes["scalerank"].astype(float) <= tier_deep["water"]]
            for i in range(len(lakes)):
                g = lakes.geometry.iloc[i]
                p = shapely.point_on_surface(g)
                feats.append(feat(p.x, p.y, str(lakes["name"].iloc[i]), "water", lakes["min_zoom"].iloc[i], 1e6 - float(lakes["scalerank"].iloc[i]) * 1e4, "lake"))
                stats["water"] += 1
        rivers = load("rivers")
        if rivers is not None and len(rivers):
            rivers = rivers[rivers["name"].notna() & (rivers["featurecla"].astype(str) != "Lake Centerline")]
            if tier_deep["water"] is not None:
                rivers = rivers[rivers["scalerank"].astype(float) <= tier_deep["water"]]
            for i in range(len(rivers)):
                g = rivers.geometry.iloc[i].intersection(wbox)
                if g.is_empty:
                    continue
                ln = max(g.geoms, key=lambda q: q.length) if hasattr(g, "geoms") else g
                if ln.geom_type != "LineString":
                    continue
                p = ln.interpolate(0.5, normalized=True)
                feats.append(feat(p.x, p.y, str(rivers["name"].iloc[i]), "water", rivers["min_zoom"].iloc[i], 5e5 - float(rivers["scalerank"].iloc[i]) * 1e4, "river"))
                stats["water"] += 1
    if cfg["admin"] and tier_first["tier"] != "world":
        in_us = -180 <= bbox[0] and bbox[2] <= -60 and 15 <= bbox[1] and bbox[3] <= 72
        adm = load("states" if in_us else "admin1")
        name_col = "NAME" if in_us else "name"
        if adm is not None and len(adm) and name_col in adm.columns:
            vbox = sbox(*bbox)
            for i in range(len(adm)):
                g = adm.geometry.iloc[i].intersection(vbox)
                g = g if not g.is_empty else adm.geometry.iloc[i]
                p = shapely.point_on_surface(g)
                feats.append(feat(p.x, p.y, str(adm[name_col].iloc[i]).upper(), "admin", ADMIN_NAME_ZOOM[0], 2e6 + g.area, "admin", ADMIN_NAME_ZOOM[1]))
                stats["admin"] += 1
    feats.sort(key=lambda f: -f["properties"]["prio"])
    if len(feats) > NAMES_MAX_FEATURES:
        feats = feats[:NAMES_MAX_FEATURES]
        stats["capped"] = NAMES_MAX_FEATURES
    return {"type": "FeatureCollection", "features": feats}, stats


def bundled_background(plan) -> tuple[list[dict], dict, list[dict]]:
    """Countries, US states when the data is in the US, and lakes, from the bundled library entries: (defs, collections, records)."""
    from shapely.geometry import mapping
    t = plan.tokens
    wgs = mp._wgs84_extent(plan)
    in_us = -180 <= wgs[0] and wgs[2] <= -60 and 15 <= wgs[1] and wgs[3] <= 72
    todo = [("countries", mp.BASEMAP_REFS["countries"], {"fillColor": t["land"], "fillOpacity": 1, "color": t["frame"], "weight": 0.8, "opacity": 1}, 3)]
    if in_us:
        todo.append(("states", mp.BASEMAP_REFS["states"], {"fillColor": t["land"], "fillOpacity": 1, "color": t["frame"], "weight": 0.6, "opacity": 1}, 3))
    todo.append(("lakes", mp.BASEMAP_REFS["lakes"], {"fillColor": t["water"], "fillOpacity": 1, "color": t["water"], "weight": 0.5, "opacity": 1}, 3))
    defs, cols, recs = [], {}, []
    for name, ref, style, nd in todo:
        gdf, e = mp.load_ref_layer(ref)
        gdf = to_wgs84(gdf)
        feats = []
        for g in gdf.geometry:
            if g is None or g.is_empty:
                continue
            m = mapping(g)
            feats.append({"type": "Feature", "properties": {}, "geometry": {"type": m["type"], "coordinates": round_coords(m["coordinates"], nd)}})
        cid = f"base-{name}"
        defs.append({"id": cid, "style": style})
        cols[cid] = {"type": "FeatureCollection", "features": feats}
        recs.append(e)
    return defs, cols, recs


# ---------------------------------------------------------------------------------------------------------------- markup helpers
def esc(s) -> str:
    return html.escape(str(s), quote=True)


def script_json(obj) -> str:
    """JSON safe to place inside a <script> element."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("<!--", "<\\!--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def logo_html(plan, ground: str, height_px: int = 28) -> str:
    logo = plan.pack["brand"]["logo"]
    key = "dark" if luminance(ground) < 0.18 else "light"
    svg = logo.get(key) or logo.get("light") or logo.get("dark")
    if not svg:
        return ""
    m = re.search(r'viewBox="([\d.\-\s]+)"', svg)
    vb = [float(v) for v in m.group(1).split()] if m else [0, 0, 100, 100]
    w = round(height_px * vb[2] / vb[3])
    inner = re.sub(r"^<\?xml[^>]*>\s*", "", svg.strip())
    return re.sub(r"<svg\b([^>]*)>", lambda mm: f'<svg width="{w}" height="{height_px}" viewBox="{m.group(1) if m else "0 0 100 100"}" class="gm-logo" role="img" '
                  f'aria-label="{esc(plan.brand_name)} logo" xmlns="http://www.w3.org/2000/svg">', inner, count=1)


def swatch_html(block: dict, item: dict) -> str:
    kind = block["kind"]
    if kind == "swatches":                           # a MapServer legend swatch, as the server drew it (M12)
        return f'<img class="gm-sw-img" alt="" src="{item["href"]}">'
    if kind in ("classes", "categories", "polygons"):
        sw = block.get("swatch")
        if sw == "hexagon":                      # a hexbin key in the cell's own shape (M11); the border becomes a same-colour outline by the clip
            return f'<span class="gm-sw gm-hex" style="background:{item["fill"]}"></span>'
        if sw == "square":
            return f'<span class="gm-sw gm-sq" style="background:{item["fill"]};border-color:{item.get("stroke", item["fill"])}"></span>'
        if item.get("outline") or item.get("dash"):     # 1.2.0: an outline (or dashed) polygon layer's swatch is its edge
            style = "dashed" if item.get("dash") else "solid"
            bg = "transparent" if item.get("outline") else item["fill"]
            return (f'<span class="gm-sw" style="background:{bg};border-color:{item.get("stroke", item["fill"])};border-style:{style};'
                    f'border-width:2px"></span>')
        return f'<span class="gm-sw" style="background:{item["fill"]};border-color:{item.get("stroke", item["fill"])}"></span>'
    if kind == "lines":
        dash = "dashed" if item.get("dash") else "solid"
        return f'<span class="gm-ln" style="border-top-color:{item["stroke"]};border-top-style:{dash}"></span>'
    d = max(8, round(float(item.get("size_mm", 2.5)) * 3.78))
    shape = item.get("shape", "circle")
    rad = "50%" if shape == "circle" else "0"
    rot = "transform:rotate(45deg);" if shape == "diamond" else ""
    return f'<span class="gm-pt" style="width:{d}px;height:{d}px;border-radius:{rad};{rot}background:{item["fill"]};border-color:{item.get("stroke", item["fill"])}"></span>'


def bivariate_key_html(block: dict) -> str:
    """1.0.0 (TODO M15): a bivariate layer's key on a web page as on the print map: the n x n grid (y up, x to the right), each cell titled
    with its two classes, the fields' titles with arrows, the low and high edges; no-data below it."""
    n = int(block["n"])
    cells, items = block["cells"], block.get("items") or []
    rows = []
    for j in reversed(range(n)):
        for i in range(n):
            k = j * n + i
            lab = items[k]["label"] if k < len(items) else ""
            rows.append(f'<span class="gm-bv-c" style="background:{cells[j][i]["fill"]}" title="{esc(lab)}"></span>')
    x, y = block.get("x") or {}, block.get("y") or {}
    xt, yt = list(x.get("ticks") or []), list(y.get("ticks") or [])
    out = (f'<div class="gm-bv" role="img" aria-label="Bivariate key: {esc(x.get("title") or "x")} increases to the right, {esc(y.get("title") or "y")} '
           f'increases upward, {n} classes each">'
           f'<div class="gm-bv-yt">&uarr; {esc(y.get("title") or "")}</div>'
           f'<div class="gm-bv-body"><div class="gm-bv-yl"><span>{esc(yt[-1] if yt else "")}</span><span>{esc(yt[0] if yt else "")}</span></div>'
           f'<div class="gm-bv-g" style="grid-template-columns:repeat({n},18px)">{"".join(rows)}</div></div>'
           f'<div class="gm-bv-xl"><span>{esc(xt[0] if xt else "")}</span><span>{esc(xt[-1] if xt else "")}</span></div>'
           f'<div class="gm-bv-xt">{esc(x.get("title") or "")} &rarr;</div></div>')
    nd = block.get("nodata")
    if nd:
        out += (f'<ul><li><span class="gm-sw" style="background:{nd["fill"]};border-color:{nd["fill"]}"></span>'
                f'<span class="gm-lb">{esc(nd["label"])}</span></li></ul>')
    return out


def legend_block_html(block: dict, hidden: bool = False) -> str:
    title = f'<h3>{esc(block["title"])}</h3>' if block.get("title") else ""
    note = f'<p class="gm-note">{esc(block["note"])}</p>' if block.get("note") else ""
    head = f'<section class="gm-lg" data-legend-for="{esc(block["layer"])}"{" hidden" if hidden else ""}>{title}'
    if block["kind"] == "ramp":                      # a continuous bar through the brand ramp's tokens, the values under it (M12)
        stops = block["stops"]
        grad = ",".join(f"{c} {100 * i / (len(stops) - 1):.1f}%" for i, c in enumerate(stops))
        labs = "".join(f'<span class="gm-ramp-lb" style="left:{100 * float(it["pos"]):.1f}%">{esc(it["label"])}</span>' for it in block["items"])
        op = float(block.get("opacity", 1.0))
        return (head + f'<div class="gm-ramp" role="img" aria-label="{esc(block.get("title") or "")}: {esc(block["items"][0]["label"])} to {esc(block["items"][-1]["label"])}">'
                f'<div class="gm-ramp-bar" style="background:linear-gradient(to right,{grad});opacity:{op:g}"></div><div class="gm-ramp-lbs">{labs}</div></div>{note}</section>')
    if block["kind"] == "image":                     # the service's own legend picture
        imgs = "".join(f'<img class="gm-lg-img" alt="Legend from the service" src="{it["href"]}">' for it in block["items"])
        return head + imgs + note + "</section>"
    if block["kind"] == "bivariate" and block.get("cells"):
        return head + bivariate_key_html(block) + note + "</section>"
    rows = "".join(f'<li>{swatch_html(block, it)}<span class="gm-lb">{esc(it["label"])}</span></li>' for it in block["items"])
    return f'{head}<ul>{rows}</ul>{note}</section>'


def proportional_block(l, vmax: float, px: dict, color: str, stroke: str) -> dict:
    import numpy as np
    vals = l.gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan)
    vals = vals[np.isfinite(vals) & (vals > 0)]
    if not len(vals):
        return {}
    lo, hi = float(vals.min()), float(vals.max())
    mid = (lo * hi) ** 0.5 if lo > 0 else (lo + hi) / 2

    def fmt(v):
        return f"{v:,.0f}" if v >= 100 else f"{v:,.3g}"
    items = [{"label": fmt(v), "shape": "circle", "size_mm": 2 * max(px["min_r"], px["max_r"] * (v / hi) ** 0.5) / 3.78, "fill": color, "stroke": stroke} for v in (lo, mid, hi)]
    title = (l.spec.get("legend") or {}).get("title") or l.title
    return {"layer": l.id, "title": title, "kind": "proportional", "items": items}


PX_PER_MM = 3.78                 # CSS pixels per millimetre: a print width becomes a page width


def layer_style(plan, l) -> tuple[dict, dict]:
    """(leaflet style dict, radius info for point layers) from the same plan the static map uses."""
    t = plan.tokens
    px_w = max(0.5, round(l.stroke_w_mm * 3.78, 2))
    no_stroke = str(l.stroke or "").lower() == "none"        # the context pack's urban tint has no outline
    s = {"fill": l.fill or t["land"], "fillOpacity": round(float(l.fill_opacity), 2) if l.kind == "polygon" else 0.85,
         "stroke": (l.fill or t["land"]) if no_stroke else (l.stroke or t["frame"]), "weight": 0 if no_stroke else px_w,
         "dash": (l.symbol or {}).get("dash") and "6 4", "radius": max(3, round(float((l.symbol or {}).get("size", 3)) * 1.333 * 0.9, 1)),
         "opacity": round(float(getattr(l, "stroke_opacity", 1.0)), 2)}
    if getattr(l, "outline", False):                          # 1.2.0: an outline polygon layer has no fill on the page either
        s["fillOpacity"] = 0.0
    if l.kind == "point" and str((l.symbol or {}).get("shape", "")).lower() == "none":
        s.update(radius=0, weight=0, fillOpacity=0.0, opacity=0.0)          # 1.2.0: a label-only point layer
    px = {"min_r": 2.5, "max_r": max(6.0, round(float((l.symbol or {}).get("max_size", 14)) * 1.333 / 2, 1))}
    # 0.22.0 (web parity with the print look): the casing under data lines, points at the print size with the print's light outline
    cas = getattr(l, "casing", None)
    if cas and l.kind == "line":
        s["casing"] = {"color": cas["color"], "weight": round((float(l.stroke_w_mm) + float(cas["extra_mm"])) * PX_PER_MM, 2), "opacity": float(cas["opacity"])}
    if l.kind == "point" and l.role != "proportional" and getattr(plan, "look", "classic") != "classic" and (l.symbol or {}).get("_size_mm"):
        s["radius"] = round(max(3.0, float(l.symbol["_size_mm"]) * PX_PER_MM / 2), 1)
        s["pointWeight"] = round(max(1.0, float(l.symbol.get("_stroke_mm", 0.3)) * PX_PER_MM), 2)
    if getattr(l, "highlight", None) is not None:              # 0.23.0: the print's highlight (map_look), carried to Leaflet and MapLibre
        import map_look
        s["highlight"] = {"rest": map_look.HIGHLIGHT_REST_OPACITY, "size": 1.0 if l.role == "proportional" else map_look.HIGHLIGHT_SIZE}
    return s, px


def source_note_lines(plan) -> list[str]:
    return provenance.source_lines(plan)


def raster_overlay(im: dict) -> dict:
    """A print picture (raster layer or relief) as a Leaflet imageOverlay: warped to Web Mercator, at most map_raster.WEB_MAX_PX a side, a data URI
    (the page's CSP already allows data: images; no host is added)."""
    import map_raster
    return map_raster.web_overlay(im["rgba"], im["transform"], im["crs"])


# ---------------------------------------------------------------------------------------------------------------- M14: time, engine, units
def _as_time(v):
    """A time value as a pandas Timestamp, or None (numbers are not dates)."""
    import pandas as pd
    if v is None or isinstance(v, bool) or isinstance(v, (int, float)):
        return None
    try:
        ts = pd.Timestamp(v)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts


def time_periods(tcfg: dict, cols: dict[str, dict]) -> dict:
    """The time slider's periods from the `_tv` values of the time layers' features, and each feature's period number `_t` (-1: no value).
    Numbers step by `step` (default 1); dates by day, week, month, quarter or year (default month). Returns {labels, step, kind, missing}."""
    import pandas as pd
    vals = [f["properties"].get("_tv") for c in cols.values() for f in c["features"]]
    present = [v for v in vals if v is not None and v == v]
    numeric = bool(present) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present)
    step = tcfg.get("step")
    out = {"labels": [], "kind": "number" if numeric else "date", "missing": 0}
    if not present:
        for c in cols.values():
            for f in c["features"]:
                f["properties"].pop("_tv", None)
                f["properties"]["_t"] = -1
        out["missing"] = len(vals)
        return out
    if numeric:
        step = float(step or 1)
        lo = math.floor(min(present) / step) * step
        hi = max(present)
        n = int(math.floor((hi - lo) / step)) + 1
        if n > 400:
            raise ValueError(f"the time field spans {n} steps of {step:g}; set webmap.time.step larger (at most 400 periods)")
        fmt = (lambda x: f"{x:g}") if float(step).is_integer() else (lambda x: f"{x:.6g}")
        out["labels"] = [fmt(lo + k * step) if step == 1 else f"{fmt(lo + k * step)} to {fmt(lo + (k + 1) * step)}" for k in range(n)]
        key = lambda v: int(math.floor((float(v) - lo) / step))        # noqa: E731
        out["num"] = {"lo": lo, "step": step}
    elif any(_as_time(v) is None for v in present):
        # 1.3.0 (R1-TIME): named periods ("Phase 1", "Wave 2"): the periods and their order come from timekeys.periods, the reader the print atlas
        # uses, so the same values give the same periods on paper and on the page. Order: webmap.time.order, else the layer's time_order, else
        # natural order (MAP-WEB-042). With an order given, values it does not list are hidden while the slider is on (MAP-WEB-043).
        import timekeys
        given = tcfg.get("order") or tcfg.get("_layer_order")
        out["order_from"] = "webmap.time.order" if tcfg.get("order") else ("time_order" if tcfg.get("_layer_order") else "natural")
        texts = [str(v).strip() for v in present]
        listed = [str(x) for x in given] if given else None
        per = timekeys.periods(texts, unit="auto", order=listed)
        if per.unit != "period":                                 # years, months or quarters written as text: their own keys, in time order
            rank = {}
            for t, k in zip(texts, per.keys):
                rank.setdefault(t, per.order.index(k))
            out.update({"kind": "named", "labels": [per.label(k) for k in per.order], "step": "named", "unlisted": []})
            out["key"] = lambda v: rank[str(v).strip()]          # noqa: E731
            return _assign(out, cols, out["key"])
        names = [per.labels[k] for k in per.order]
        out["unlisted"] = [n for n in names if n not in listed] if listed else []
        names = [n for n in names if n not in out["unlisted"]]
        index = {n: k for k, n in enumerate(names)}
        out.update({"kind": "named", "labels": names, "step": "named"})

        def key(v):
            return index[str(v).strip()]
        out["key"] = key
        return _assign(out, cols, key)
    else:
        step = str(step or "month")
        if step not in TIME_STEPS:
            raise ValueError(f"webmap.time.step '{step}' is not one of {', '.join(TIME_STEPS)} (for a date field) or a number")
        ts = [_as_time(v) for v in present]
        freq = {"day": "D", "week": "W-MON", "month": "M", "quarter": "Q", "year": "Y"}[step]
        per = lambda t: pd.Timestamp(t).tz_localize(None).to_period(freq)     # noqa: E731
        ps = sorted({per(t) for t in ts})
        rng = pd.period_range(ps[0], ps[-1], freq=freq)
        if len(rng) > 400:
            raise ValueError(f"the time field spans {len(rng)} {step}s; choose a longer step (at most 400 periods)")
        index = {p: k for k, p in enumerate(rng)}

        def label(p):
            if step == "year":
                return str(p.year)
            if step == "quarter":
                return f"{p.year} Q{p.quarter}"
            if step == "month":
                return p.start_time.strftime("%b %Y")
            if step == "week":
                return "Week of " + p.start_time.strftime("%Y-%m-%d")
            return p.start_time.strftime("%Y-%m-%d")
        out["labels"] = [label(p) for p in rng]
        out["starts"] = [p.start_time.strftime("%Y-%m-%d") for p in rng]
        key = lambda v: index[per(_as_time(v))]                        # noqa: E731
    out["step"] = step
    out["key"] = key                                                     # a raw value's period number (a story chapter's print filter, M15)
    return _assign(out, cols, key)


def _assign(out: dict, cols: dict, key) -> dict:
    """Each feature's period number `_t`: -1 with no value (counted in `missing`) or with a named value the given order leaves out (`hidden`)."""
    out.setdefault("hidden", 0)
    for c in cols.values():
        for f in c["features"]:
            v = f["properties"].pop("_tv", None)
            if v is None or v != v:
                f["properties"]["_t"] = -1
                out["missing"] += 1
                continue
            try:
                f["properties"]["_t"] = key(v)
            except KeyError:
                f["properties"]["_t"] = -1
                out["hidden"] += 1
    return out


_LABEL_FMT = re.compile(r"\{value(?::([^{}]*))?\}")


def format_period_label(template: str, value) -> str:
    """webmap.time.label_format (1.3.0): "km {value:.1f}". Only {value}, with an optional format spec, is allowed; nothing else is evaluated."""
    rest = _LABEL_FMT.sub("", str(template))
    if "{" in rest or "}" in rest:
        raise ValueError("webmap.time.label_format may hold only the value placeholder ('{value}' with an optional format spec), not " + repr(template))
    return _LABEL_FMT.sub(lambda m: format(float(value), m.group(1) or "g"), str(template))


class _PeriodValues:
    """The values of a layer's time field (as the layer filter compares them, text) whose period is a chapter's, or every period up to it on a
    cumulative slider: a story chapter's print picture is filtered by them (M15)."""

    def __init__(self, field: str, pairs: list, key, cumulative: bool):
        self.field, self.pairs, self.key, self.cumulative = field, pairs, key, cumulative

    def __call__(self, t: int) -> list[str]:
        out = []
        for raw, text in self.pairs:
            try:
                k = self.key(raw)
            except Exception:  # noqa: BLE001 - a value with no period is in no chapter
                continue
            if (0 <= k <= t) if self.cumulative else k == t:
                out.append(text)
        return out


def time_values(plan, time_layers: dict, time_cfg: dict | None, tp: dict | None) -> dict:
    if not time_cfg or not tp or not tp.get("key"):
        return {}
    out = {}
    for l in plan.layers:
        f = time_layers.get(l.id)
        if f and f in l.gdf.columns:
            pairs = list(dict.fromkeys((r, s) for r, s in zip(l.gdf[f].tolist(), l.gdf[f].astype(str).tolist()) if r is not None and r == r))
            out[l.id] = _PeriodValues(f, pairs, tp["key"], time_cfg["mode"] == "cumulative")
    return out


# 0.16.0: providers whose terms require their logo on every map (Mapbox). The page loads the logo from the provider's own published stylesheet, on
# the tile host, when the basemap is shown; the plugin ships no copy of it. The pinned file is Mapbox GL JS's, whose a.mapboxgl-ctrl-logo rule
# carries the wordmark Mapbox's own maps show (a white mark with a dark outline, readable on any ground), 88 x 23 px as there.
WORDMARKS = {"ref.basemap-provider.mapbox": {"css": "https://api.mapbox.com/mapbox-gl-js/v3.9.4/mapbox-gl.css", "selector": "a.mapboxgl-ctrl-logo",
                                             "link": "https://www.mapbox.com/", "alt": "Mapbox", "w": 88, "h": 23},
             # 1.1.0: MapTiler's own published logo on its tile host (docs.maptiler.com, add attribution: a free account shows it, linked, bottom left)
             "ref.basemap-provider.maptiler": {"img": "https://api.maptiler.com/resources/logo.svg", "link": "https://www.maptiler.com", "alt": "MapTiler", "w": 0, "h": 24}}


def choose_engine(asked: str, *, features: int, tiled: list[str], vector_base: bool, time_on: bool, compare_on: bool, three_d: bool = False,
                  story: bool = False, live_vector: list[str] | None = None) -> tuple[str, str]:
    """(engine, why) for webmap.engine (auto, leaflet, maplibre)."""
    reasons = []
    if live_vector:
        reasons.append(f"live vector tile layer(s) {', '.join(live_vector)}")
    if three_d:
        reasons.append("the page is in 3D")
    if story:
        reasons.append("the page tells a story in chapters")
    if features > AUTO_MAPLIBRE_FEATURES:
        reasons.append(f"the page carries {features:,} features (over {AUTO_MAPLIBRE_FEATURES:,})")
    if tiled:
        reasons.append(f"layer(s) {', '.join(tiled)} go into the page as tiles")
    if vector_base:
        reasons.append("the basemap is vector tiles")
    if time_on:
        reasons.append("the time slider is on")
    if compare_on:
        reasons.append("the swipe compare is on")
    if asked == "leaflet":
        return "leaflet", "webmap.engine is leaflet"
    if asked == "maplibre":
        return "maplibre", "webmap.engine is maplibre"
    if reasons:
        return "maplibre", "auto: " + "; ".join(reasons)
    return "leaflet", f"auto: a small page ({features:,} features, no tiles, no time slider or compare)"


def measure_units(spec_in: dict, wgs) -> str:
    """'imperial' or 'metric' for the measure tool, from the MapSpec's scale bar units (auto: miles in the United States)."""
    u = (((spec_in.get("elements") or {}).get("scale_bar") or {}).get("units") or "auto")
    if u in ("mi", "ft", "imperial"):
        return "imperial"
    if u in ("km", "m", "metric"):
        return "metric"
    try:                                                       # 1.5.0 (R3-UNITS): the units of the country under the centre (no US box)
        import geo_locale as gl
        return "imperial" if gl.units_at((wgs[0] + wgs[2]) / 2, (wgs[1] + wgs[3]) / 2) == "mi" else "metric"
    except Exception:  # noqa: BLE001
        return "metric"


def export_legend(blocks: list[dict]) -> list[dict]:
    """The legend blocks as plain data for the image export (the page draws them onto the picture): swatches, lines, points and ramps."""
    out = []
    for b in blocks:
        kind = b.get("kind")
        items = []
        for it in b.get("items") or []:          # 1.0.0: picture legends (a service's image, MapServer swatches) keep their address
            items.append({k: it.get(k) for k in ("label", "fill", "stroke", "dash", "shape", "size_mm", "pos", "href") if it.get(k) is not None})
        row = {"layer": b.get("layer"), "title": b.get("title"), "kind": kind, "items": items, "stops": b.get("stops"), "note": b.get("note")}
        if kind == "bivariate" and b.get("cells"):  # 1.0.0: the grid, not the list of nine
            row.update(n=int(b["n"]), cells=[[c["fill"] for c in r] for r in b["cells"]], x_title=(b.get("x") or {}).get("title"),
                       y_title=(b.get("y") or {}).get("title"), nodata=(b.get("nodata") or {}).get("fill"), nodata_label=(b.get("nodata") or {}).get("label"))
        out.append(row)
    return out


def search_index(col: dict, field: str) -> dict:
    """A compact search index for a tiled layer: the field's text and a point on each feature."""
    import shapely
    from shapely.geometry import shape
    t, x, y = [], [], []
    for f in col["features"]:
        v = f["properties"].get(field)
        if v is None:
            continue
        g = shape(f["geometry"])
        p = g if g.geom_type == "Point" else shapely.point_on_surface(g)
        t.append(str(v))
        x.append(round(p.x, 5))
        y.append(round(p.y, 5))
    return {"t": t, "x": x, "y": y}


def search_index_packed(col: dict, field: str) -> tuple[str, int, int]:
    """1.0.0 (TODO M14): the search index of a very large tiled layer, packed: a little-endian header (count, text bytes), the texts joined by
    U+001F (padded to 4 bytes), then the longitudes and latitudes as int32 at 1e-5 degree; gzip, then base64 for a <script> block. The page
    unpacks it with the browser's DecompressionStream. -> (base64 text, entries, packed bytes)."""
    import gzip
    import struct
    import numpy as np
    si = search_index(col, field)
    txt = "\x1f".join(s.replace("\x1f", " ") for s in si["t"]).encode("utf-8")
    txt += b"\0" * ((4 - len(txt) % 4) % 4)
    xs = np.round(np.asarray(si["x"], float) * 1e5).astype("<i4")
    ys = np.round(np.asarray(si["y"], float) * 1e5).astype("<i4")
    raw = struct.pack("<II", len(si["t"]), len(txt)) + txt + xs.tobytes() + ys.tobytes()
    gz = gzip.compress(raw, 6, mtime=0)
    return base64.b64encode(gz).decode("ascii"), len(si["t"]), len(gz)


# ---------------------------------------------------------------------------------------------------------------- assemble
CSS = """
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;height:100%;background:@@background@@;color:@@label@@;font-family:Inter,system-ui,sans-serif;font-size:14px;line-height:1.4}
body{overflow:hidden}
#gm-map{position:absolute;inset:0;background:@@water@@}
#gm-fallback{position:absolute;inset:0;margin:0;background:@@background@@;display:flex;align-items:center;justify-content:center}
#gm-fallback img{max-width:100%;max-height:100%}
.gm-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

.gm-lg li{display:flex;align-items:center;gap:8px;font-size:12px;padding:1px 0}
.gm-lb{font-variant-numeric:tabular-nums}
.gm-sw{width:22px;height:12px;border:1px solid;flex:none}
.gm-sw.gm-hex{width:14px;height:12px;border:0;clip-path:polygon(25% 0,75% 0,100% 50%,75% 100%,25% 100%,0 50%)}
.gm-sw.gm-sq{width:12px;height:12px}
.gm-ln{width:22px;border-top-width:3px;flex:none}
.gm-pt{border:1px solid;flex:none;display:inline-block}
.gm-note{margin:2px 0 0;font-size:11px}
.gm-ramp{width:220px;max-width:100%;margin:2px 8px 4px}.gm-ramp-bar{height:12px;border:1px solid @@legend_frame@@}
.gm-ramp-lbs{position:relative;height:16px;font-size:11px;font-variant-numeric:tabular-nums}.gm-ramp-lb{position:absolute;top:2px;transform:translateX(-50%);white-space:nowrap}
.gm-lg-img{display:block;max-width:220px;max-height:240px}.gm-sw-img{width:16px;height:16px;flex:none;object-fit:contain}.gm-bv{display:inline-block;font-size:11px;margin:2px 0 4px}.gm-bv-body{display:flex;align-items:stretch;gap:4px}.gm-bv-yl{display:flex;flex-direction:column;justify-content:space-between;text-align:right;font-variant-numeric:tabular-nums}.gm-bv-g{display:grid;gap:1px}.gm-bv-c{width:18px;height:18px;display:block}.gm-bv-xl{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;margin-left:var(--gm-bv-yl,26px)}.gm-bv-xt,.gm-bv-yt{font-weight:600}
.gm-conf{position:absolute;z-index:850;top:0;left:50%;transform:translateX(-50%);background:@@emphasis@@;color:@@background@@;font-weight:700;font-size:12px;letter-spacing:.04em;padding:3px 14px;border-radius:0 0 6px 6px}
.gm-banner{margin:0;padding:6px 10px;background:@@legend_bg@@;color:@@label@@;border:1px solid @@status_warning@@;border-left-width:4px;border-radius:4px;font-size:12px;pointer-events:auto}
.gm-popup{font-family:Inter,system-ui,sans-serif;font-size:13px}
.gm-faint path.gm-bundled{opacity:.45}
.gm-name-icon{background:none;border:0;pointer-events:none}
.gm-name{position:absolute;white-space:nowrap;font-family:Inter,system-ui,sans-serif;font-size:11px;line-height:1;color:@@label@@;pointer-events:none;
  text-shadow:0 0 2px @@land@@,0 0 2px @@land@@,0 0 3px @@land@@,0 0 3px @@land@@}
.gm-name-dot{position:absolute;left:-2px;top:-2px;width:4px;height:4px;border-radius:50%;background:@@label@@}
.gm-name-capital .gm-name-dot{left:-3.5px;top:-3.5px;width:5px;height:5px;background:@@legend_bg@@;border:1.5px solid @@label@@}
.gm-name-text{position:absolute;left:6px;top:-6px}
.gm-name-capital .gm-name-text{font-weight:700;text-transform:uppercase;letter-spacing:.06em;font-size:12px}
.gm-name-city .gm-name-text{font-weight:700;font-size:12px}
.gm-name-village .gm-name-text{font-size:10px}
.gm-name-water .gm-name-text,.gm-name-admin .gm-name-text{left:auto;top:auto;transform:translate(-50%,-50%)}
.gm-name-water .gm-name-text{color:@@water_label@@;font-style:oblique 12deg}
.gm-name-admin .gm-name-text{color:@@admin_label@@;font-weight:500;text-transform:uppercase;letter-spacing:.16em}
.gm-hidden{visibility:hidden!important}
.leaflet-tooltip.gm-elev{background:none;border:0;box-shadow:none;padding:0;font-family:Inter,system-ui,sans-serif;font-size:10px;color:@@label@@;text-shadow:0 0 2px @@land@@,0 0 2px @@land@@}
.leaflet-tooltip.gm-elev::before{display:none}
.leaflet-tooltip.gm-flabel{background:none;border:0;box-shadow:none;padding:0 0 0 2px;font-family:Inter,system-ui,sans-serif;font-size:11px;color:@@label@@;
  text-shadow:0 0 2px @@land@@,0 0 2px @@land@@,0 0 3px @@land@@,0 0 3px @@land@@;pointer-events:none}
.leaflet-tooltip.gm-flabel::before{display:none}
.gm-popup dl{margin:4px 0 0;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
.gm-popup dt{font-weight:600;text-transform:capitalize}.gm-popup dd{margin:0;font-variant-numeric:tabular-nums}
.leaflet-container{background:@@water@@;color:@@label@@;font-family:Inter,system-ui,sans-serif}
.leaflet-container a{color:@@emphasis@@}
.leaflet-control-scale-line{background:@@legend_bg@@;color:@@label@@;border-color:@@frame@@;font-size:11px}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:@@legend_bg@@;color:@@label@@;box-shadow:0 1px 4px @@frame@@}
.leaflet-container .leaflet-popup-close-button{color:@@label@@}
.maplibregl-map{font-family:Inter,system-ui,sans-serif}
.maplibregl-ctrl-scale{background:@@legend_bg@@;color:@@label@@;border-color:@@frame@@;font-size:11px}
.maplibregl-popup-content{background:@@legend_bg@@;color:@@label@@;box-shadow:0 1px 4px @@frame@@;font-family:Inter,system-ui,sans-serif;padding:10px 12px}
.maplibregl-popup-anchor-top .maplibregl-popup-tip,.maplibregl-popup-anchor-top-left .maplibregl-popup-tip,.maplibregl-popup-anchor-top-right .maplibregl-popup-tip{border-bottom-color:@@legend_bg@@}
.maplibregl-popup-anchor-bottom .maplibregl-popup-tip,.maplibregl-popup-anchor-bottom-left .maplibregl-popup-tip,.maplibregl-popup-anchor-bottom-right .maplibregl-popup-tip{border-top-color:@@legend_bg@@}
.maplibregl-popup-anchor-left .maplibregl-popup-tip{border-right-color:@@legend_bg@@}.maplibregl-popup-anchor-right .maplibregl-popup-tip{border-left-color:@@legend_bg@@}
.maplibregl-popup-close-button{color:@@label@@;font-size:18px;width:28px;height:28px}
#gm-map-b{position:absolute;inset:0}
#gm-ctl-host{position:absolute;inset:0;pointer-events:none;z-index:792}
#gm-compare{position:absolute;z-index:790;top:0;bottom:var(--gm-bottom,0px);width:0;left:50%;border-left:2px solid @@emphasis@@;touch-action:none}
#gm-compare button{position:absolute;top:30%;left:-17px;width:32px;height:44px;margin-top:-22px;border:1px solid @@emphasis@@;border-radius:6px;background:@@legend_bg@@;color:@@label@@;
  font:inherit;font-size:14px;font-weight:700;cursor:ew-resize;touch-action:none}
.gm-cmp-tag{position:absolute;z-index:791;top:calc(30% + 30px);margin:0;max-width:40vw;padding:2px 8px;font-size:11px;font-weight:600;background:@@legend_bg@@;
  color:@@label@@;border:1px solid @@legend_frame@@;border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
#gm-cmp-tag-l{right:calc(50% + 8px)}#gm-cmp-tag-r{left:calc(50% + 8px)}
.gm-link-popup .maplibregl-popup-content{padding:8px 10px}
.gm-link-pop strong{display:block;margin:0 26px 4px 0}
.gm-profile{display:block;width:min(600px,84vw);height:auto}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
@page{size:landscape;margin:10mm}@media print{
  html,body{height:auto;overflow:visible}
  #gm-root{height:auto;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  #gm-banners,#gm-measure,#gm-compare button{display:none!important}
  #gm-root.gm-story-page{display:none!important}
  #gm-story-print{display:block!important;font-size:11pt}#gm-story-print h1{font-size:20pt;margin:0 0 4mm}
  .gm-pch{break-inside:avoid;page-break-inside:avoid;margin:0 0 8mm}.gm-pch h2{font-size:14pt;margin:0 0 3mm}
  .gm-pch img{display:block;width:100%;max-height:150mm;object-fit:contain;margin:0 0 3mm}
}
#gm-root{position:relative;width:100%;height:100vh;height:100dvh;background:@@water@@;font-family:Inter,system-ui,sans-serif}
.gm-lg ul,#gm-layer-list,#gm-base-list{list-style:none;margin:0;padding:0}
.gm-banner{margin:0;padding:6px 10px;background:@@legend_bg@@;color:@@label@@;border:1px solid @@status_warning@@;border-left-width:4px;border-radius:4px;font-size:12px;pointer-events:auto}
.maplibregl-ctrl-bottom-left,.leaflet-bottom.leaflet-left{left:var(--gm-bl,8px)!important;bottom:8px!important}
.maplibregl-ctrl-bottom-left .maplibregl-ctrl,.leaflet-bottom.leaflet-left .leaflet-control{margin:0!important;float:none}
#gm-story-print{display:none}
"""


def build_css(t: dict) -> str:
    css = CSS
    import map_render as mr
    vals = {"background": t["background"], "label": t["label"], "legend_bg": t["legend_bg"], "legend_frame": t["legend_frame"], "frame": t["frame"],
            "emphasis": t["emphasis"], "water": t["water"], "land": t["land"], "status_warning": t["status"]["warning"],
            "water_label": mr.water_label_color(t), "admin_label": mr.admin_label_color(t)[0]}
    for k, v in vals.items():
        css = css.replace(f"@@{k}@@", v)
    assert "@@" not in css
    return css


# 1.9.0: the Inter range files a page adds beside Latin when its names are written in Greek or Cyrillic (the browser takes each only for
# the characters in its unicode-range, so the page's own text and controls stay on the Latin file)
INTER_SCRIPT_FACES = {"Grek": (("inter-greek-wght-normal.woff2", "U+0370-03FF"), ("inter-greek-ext-wght-normal.woff2", "U+1F00-1FFF")),
                      "Cyrl": (("inter-cyrillic-wght-normal.woff2", "U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116"),
                               ("inter-cyrillic-ext-wght-normal.woff2", "U+0460-052F,U+1C80-1C88,U+20B4,U+2DE0-2DFF,U+A640-A69F,U+FE2E-FE2F"))}


def inter_face(scripts=()) -> str:
    d = gc.plugin_root() / "assets" / "fonts" / "inter"
    b = base64.b64encode((d / "inter-latin-wght-normal.woff2").read_bytes()).decode("ascii")
    out = ("@font-face{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;"
           f"src:url(data:font/woff2;base64,{b}) format('woff2')}}")
    for sc in sorted(set(scripts) & set(INTER_SCRIPT_FACES)):
        for name, rng in INTER_SCRIPT_FACES[sc]:
            b = base64.b64encode((d / name).read_bytes()).decode("ascii")
            out += ("@font-face{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;"
                    f"src:url(data:font/woff2;base64,{b}) format('woff2');unicode-range:{rng}}}")
    return out


# ---------------------------------------------------------------------------------------------------------------- 1.4.0: style packs
def _web_fonts(plan) -> bool:
    st = getattr(plan, "style", None)
    return st is not None and st.fonts != "brand" and st.web in ("full", "palette-and-fonts")


@functools.lru_cache(maxsize=16)
def _face_woff2(path: str) -> str:
    """A pack face as WOFF2 for a web page (the whole font: popups show data the page cannot know in advance)."""
    import io
    from fontTools.ttLib import TTFont
    f = TTFont(path)
    f.flavor = "woff2"
    buf = io.BytesIO()
    f.save(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def page_fonts(plan) -> str:
    """The page's @font-face: Inter, and the style pack's faces when its web level carries fonts (full, palette-and-fonts)."""
    scripts = getattr(plan, "web_name_scripts", ())            # set while the names were chosen (names_collection)
    if not _web_fonts(plan):
        return inter_face(scripts)
    import map_style
    st = plan.style
    out, seen = [inter_face(scripts)], set()                # Inter stays for the map engine's own labels and controls
    for r in map_style.ROLES:
        f = st.type.get(r) or st.type.get("body")
        if f is None or f.get("brand") or (f["family"], f.get("style")) in seen:
            continue
        seen.add((f["family"], f.get("style")))
        out.append(f"@font-face{{{map_style.FF}:'{f['family']}';font-style:{f.get('style', 'normal')};font-weight:100 900;font-display:swap;"
                   f"src:url(data:font/woff2;base64,{_face_woff2(f['file'])}) format('woff2')}}")
    return "".join(out)


def pack_css(plan, css: str) -> str:
    """The page's CSS with the pack's body face in place of Inter (its title face on the title, its label face on the names)."""
    if not _web_fonts(plan):
        return css
    import map_style
    st = plan.style
    body = st.type.get("body") or {}
    fam = "Inter" if (not body or body.get("brand")) else body["family"]
    gen = body.get("generic") or "sans-serif"
    css = css.replace("font-family:Inter,system-ui,sans-serif", f"{map_style.FF}:'{fam}',{gen}")
    extra = []
    for r, sel in (("title", ".gm-title,#gm-title,h1"), ("label", ".gm-name,.leaflet-tooltip.gm-flabel")):
        f = st.type.get(r)
        if f and not f.get("brand"):
            extra.append(f"{sel}{{{map_style.FF}:'{f['family']}',{f.get('generic') or 'serif'};font-style:{f.get('style', 'normal')}}}")
    return css + "".join(extra)


def pack_web_findings(plan) -> list[dict]:
    """MAP-STYLE-008 WARN: what of the pack a web page cannot carry (its `web:` level; ornaments and treatments are print only)."""
    st = getattr(plan, "style", None)
    if st is None or st.is_default:
        return []
    left = []
    if st.fonts != "brand" and not _web_fonts(plan):
        left.append("its fonts (web level " + st.web + ")")
    if st.fonts != "brand" and _web_fonts(plan) and (st.type.get("label") or {}).get("file"):
        import webmap_maplibre as wml_
        if wml_.pack_stack(st) is None:                              # 1.10.0: a pack with glyph files (glyphs/) labels in its own face
            left.append("its label face on labels the vector map engine draws (they stay in Inter: the pack has no glyphs/ folder for it)")
    orn = [k for k, v in st.elements.items() if k != "callouts" and v and not (isinstance(v, dict) and v.get("style") == "single")]
    if orn:
        left.append("the ornaments (" + ", ".join(sorted(orn)) + ")")
    tr = [k for k in (getattr(plan, "treatments", None) or {}) if k != "category_patterns"]
    if tr:
        left.append("the treatments (" + ", ".join(sorted(tr)) + ")")
    if not left:
        return []
    return [finding("MAP-STYLE-008", "WARN", f"the web page carries the {st.id} pack's palette" + (" and fonts" if _web_fonts(plan) else "")
                    + " but not " + "; ".join(left) + ": those are print only")]


def vendor(name: str) -> str:
    return (gc.plugin_root() / "vendor" / "leaflet" / name).read_text(encoding="utf-8")


def asset_web(name: str) -> str:
    return (gc.plugin_root() / "assets" / "web" / name).read_text(encoding="utf-8")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def today() -> dt.date:
    import map_render as mr
    return mr.today()


def _page_basemaps(audience, cfg, env, findings, plan, rl, spec_in, spec_path):
    """build_page, stage 2 (1.9.0): the basemaps, each through the tile registry: terms, keys, attribution, the relief under the page. Split from build_page() unchanged."""
    # ---- basemap(s) through the registry
    entries = [e for e in rl.all_entries("all") if e.get("type") == "basemap-provider"]
    chosen = []
    asked = spec_in.get("basemap") or {}                                # the spec as written: plan.spec has the static fallback's bundled basemap
    for name in requested_providers(spec_in, cfg):
        mine = name == asked.get("provider")
        r = tr.resolve(name, entries, style=asked.get("style") if mine else None, audience=audience, env=env,
                       terms_acknowledged=bool(asked.get("terms_acknowledged")), source=asked.get("source") if mine else None,
                       base_dir=Path(spec_path).parent, time=asked.get("time") if mine else None)
        findings += r["findings"]
        if not r["ok"]:
            continue
        chosen.append((name, r))
    base_defs = []
    for name, r in list(chosen):
        if r.get("wordmark") and r.get("entry") not in WORDMARKS:     # a provider that requires its logo, and no source of it we know: not used
            findings.append(finding("MAP-WEB-011", "BLOCK", f"{r['title']} requires its logo on the map and this page has no source for it; use another basemap"))
            chosen.remove((name, r))
    for name, r in chosen:
        if r["mode"] == "tiles":
            off = int(r.get("zoom_offset") or 0)
            native_max = int(r["max_zoom"]) - off           # in the page's zoom numbering: 512 tiles (offset -1) reach one zoom further
            base_defs.append({"id": name, "mode": "tiles", "title": r["title"], "url": r["url"], "attribution": r["attribution"],
                              "nativeMaxZoom": native_max, "nativeMinZoom": max(0, int(r.get("min_zoom") or 0) - off), "zoomOffset": off,
                              "maxZoom": native_max + OVERZOOM_LEVELS, "tileSize": r["tile_size"], "host": r["hosts"][0], "entry": r["entry"], "rev": r.get("rev"),
                              "wordmark": WORDMARKS.get(r["entry"]) if r.get("wordmark") else None})
            how = map_look.tile_treatment(plan.spec, getattr(plan, "look", "classic"))
            if how != "none":                              # 0.22.0: the print's tile treatment (grey, then a share of the land colour under it)
                desat, toward = map_look.TILE_TREATMENT[how]
                base_defs[-1]["treatment"] = {"grayscale": desat, "opacity": round(1 - toward, 2)}
        elif r["mode"] in ("vector", "vector-file"):
            mz = int(r.get("max_zoom") or 15)
            base_defs.append({"id": name, "mode": r["mode"], "title": r["title"], "url": r.get("url"), "attribution": r["attribution"],
                              "nativeMaxZoom": mz, "nativeMinZoom": 0, "zoomOffset": 0, "maxZoom": min(DEFAULT_MAX_ZOOM + 2, mz + 4), "tileSize": 512,
                              "host": (r.get("hosts") or [None])[0], "entry": r["entry"], "rev": r.get("rev"), "file": r.get("file")})
            if r["mode"] == "vector-file":                 # 1.10.0: the file's own tile schema decides the page's basemap layers
                import vector_ground as vgr
                try:
                    vi = vgr.inspect(r["file"])
                except vgr.VectorGroundError:
                    vi = {"schema": None, "layers": []}
                base_defs[-1]["schema"] = vi["schema"]
                if vi["schema"] is None:
                    findings.append(finding("MAP-WEB-011", "WARN", f"the basemap file {Path(r['file']).name} is not in a tile schema the page styles (Protomaps, "
                                            f"OpenMapTiles, Shortbread); its layers are {', '.join(vi['layers'][:10]) or 'none'}, so the basemap may draw nothing"))
    return base_defs, entries, findings


def _page_layers(cfg, ctl, findings, live_specs, notes, pfields, plan, s3, spec, spec_path, t, w3, wlv):
    """build_page, stage 3 (1.9.0): the data layers in draw order: each collection, its popups, legend block and layer definition; live layers. Split from build_page() unchanged."""
    # ---- layers
    label_by_layer = {lb.get("layer"): lb.get("field") for lb in (spec.get("labels") or []) if isinstance(lb, dict)}
    collections: dict[str, dict] = {}
    flat_links: list[str] = []                       # 1.0.0: line-of-sight layers on a flat page (their profiles go in the popups)
    defs: list[dict] = []
    blocks: list[dict] = []
    conf = [l.id for l in plan.layers if l.confidentiality == "client-confidential"]
    non_ctx = [l for l in plan.layers if l.role != "context"]
    search_field = cfg["search_field"] or next((label_by_layer.get(l.id) for l in non_ctx if label_by_layer.get(l.id)), None) or \
        next((c for l in non_ctx for c in ("NAME", "name", "NAMELSAD") if c in l.gdf.columns), None)
    tcfg = dict(cfg["time"] or {})
    time_layers = {l.id: l.spec["time_field"] for l in plan.layers if l.spec.get("time_field")}
    if tcfg and not time_layers and tcfg.get("field"):
        time_layers = {l.id: tcfg["field"] for l in non_ctx if l.kind != "raster" and tcfg["field"] in l.gdf.columns}
    images = 0
    for b in plan.basemap:                           # relief: the same shading as the print map, warped to Web Mercator, under every layer (M12)
        if b.get("kind") != "hillshade":
            continue
        ov = raster_overlay(b["image"])
        if ov["downsampled"]:
            notes.append(f"relief: the page carries the shading at {ov['width']} x {ov['height']} px (the print picture is larger)")
        defs.append({"id": f"relief-{b.get('layer_id')}", "alias": b.get("layer_id"), "title": "Relief", "kind": "image", "role": "hillshade", "visible": True, "style": {}, "popup": None,
                     "searchable": False, "downloadable": False, "context": False, "url": ov["url"], "bounds": ov["bounds"], "opacity": float(b["image"].get("opacity", 0.35)),
                     "slider": False, "pane": "gm-relief"})
        collections[f"relief-{b.get('layer_id')}"] = {"type": "FeatureCollection", "features": []}
        images += 1
    for l in mp.draw_order(plan.layers):             # the page adds layers in this order: the context pack under the data
        if l.kind == "raster":                       # a raster layer: the print picture warped to Web Mercator as an image overlay with an opacity slider (M12)
            im = (l.raster or {}).get("image")
            if not im:
                continue
            ov = raster_overlay(im)
            if ov["downsampled"]:
                notes.append(f"layer '{l.id}': the page carries the picture at {ov['width']} x {ov['height']} px (the print picture is larger)")
            defs.append({"id": l.id, "title": l.title, "kind": "image", "role": l.role, "visible": l.spec.get("visible", True) is not False, "style": {},
                         "popup": None, "searchable": False, "downloadable": False, "context": False, "url": ov["url"], "bounds": ov["bounds"],
                         "opacity": float(im.get("opacity", 1.0)), "slider": bool(ctl["opacity"]), "pane": "gm-raster"})
            collections[l.id] = {"type": "FeatureCollection", "features": []}
            images += 1
            if l.legend_block:
                blocks.append(l.legend_block)
            continue
        style, px = layer_style(plan, l)
        extra, d3 = {}, None
        if s3 and l.spec.get("extrude") and l.kind == "polygon":
            try:
                hs, bs, xnotes, _st = w3.extrude_heights(l.id, l.spec["extrude"], l.gdf)
            except w3.Web3DError as ex:
                raise mp.PlanError(str(ex), gc.EXIT_USAGE) from None
            extra, d3 = {"_h": hs, "_b": bs}, "extrude"             # MAP-WEB-024 reads the heights back from the page (web_lint.check_3d)
            notes += xnotes
        elif s3 and l.spec.get("mast") and l.kind == "point":
            try:
                hs = w3.mast_heights(l.id, l.spec["mast"], l.gdf)
            except w3.Web3DError as ex:
                raise mp.PlanError(str(ex), gc.EXIT_USAGE) from None
            extra, d3 = {"_h": hs}, "mast"
        elif s3 and l.kind == "line" and w3.los_op(l) is not None and "a" in l.gdf.columns:
            d3 = "links"
        elif l.kind == "line" and w3.los_op(l) is not None and "a" in l.gdf.columns:
            flat_links.append(l.id)                 # 1.0.0 (TODO M15): a flat MapLibre page gives its links their profile charts too
        keep_more = list(pfields) + (["a", "b", "status", "verdict", "h_a_m", "h_b_m", "length_m"] if d3 == "links" or l.id in flat_links else [])
        col, pop = layer_collection(plan, l, label_by_layer.get(l.id), px, time_layers.get(l.id), extra=extra, extra_fields=keep_more)
        collections[l.id] = col
        lic = ((l.entry or {}).get("license") or {})
        redistribute = (bool(lic.get("redistribute_ok", True)) if l.entry
                        else (l.spec.get("provenance") or {}).get("redistribute_ok", (l.spec.get("provenance") or {}).get("license") in OPEN_LICENSES))
        d = {"id": l.id, "title": l.title, "kind": l.kind, "role": l.role, "visible": l.spec.get("visible", True) is not False, "style": style,
             "popup": pop if (pop["fields"] or pop["title"]) else None, "searchable": l.role != "context" and bool(search_field and search_field in l.gdf.columns),
             "downloadable": l.role != "context" and l.confidentiality != "client-confidential" and redistribute, "context": l.role == "context",
             "slider": bool(ctl["opacity"]) and l.role != "context",
             "credit": ((l.entry or {}).get("license") or {}).get("attribution_text") or (l.spec.get("provenance") or {}).get("source")}
        if l.id in time_layers:
            d["time"] = True
        if d3:
            d["d3"] = d3
        if l.role == "contour" and "elevation" in l.gdf.columns:             # index lines heavier, elevations along them at the deeper zooms (TODO M12)
            d["contour"] = {"index_stroke": l.symbol.get("index_stroke") or style["stroke"],
                            "index_weight": round(max(1.0, float(l.symbol.get("index_w_mm") or l.stroke_w_mm * 2) * 3.78), 2),
                            "labels": bool(l.symbol.get("labels", True)), "label_zoom": 12}
        defs.append(d)
        if l.role == "proportional":
            b = proportional_block(l, 1.0, px, l.fill or t["emphasis"], l.stroke or t["background"])
            if b:
                blocks.append(b)
        elif l.legend_block:
            blocks.append(l.legend_block)
    live = wlv.resolve(live_specs, tokens=t, base_dir=Path(spec_path).parent, controls=ctl) if live_specs else None
    if live:                                         # pictures under the data (after the relief and rasters), vector layers on top
        findings += live["findings"]
        notes += live["notes"]
        blocks += live["blocks"]
        lr = [d for d in live["defs"] if d["raster"]]
        lv = [d for d in live["defs"] if not d["raster"]]
        for d in lr:
            d["pane"] = "gm-raster"
        imgs = [d for d in defs if d["kind"] == "image"]
        defs = imgs + lr + [d for d in defs if d["kind"] != "image"] + lv
        for d in live["defs"]:
            collections[d["id"]] = {"type": "FeatureCollection", "features": []}
        images += len(lr)
    live_defs = live["defs"] if live else []
    bdefs, bcols, brecs = bundled_background(plan)
    collections.update(bcols)
    vector_defs = [d for d in defs if d["kind"] not in ("image", "live")]
    source_counts = {d["id"]: len(collections[d["id"]]["features"]) for d in vector_defs}
    payload_features = sum(source_counts.values())
    return bdefs, blocks, brecs, collections, conf, defs, findings, flat_links, images, label_by_layer, live, live_defs, notes, payload_features, search_field, source_counts, tcfg, time_layers, vector_defs


def _page_engine_view(base_defs, cfg, collections, compare_cfg, ctl, defs, findings, live, live_defs, notes, payload_features, plan, s3, spec, time_cfg, vector_defs, w3, wlv, wml):
    """build_page, stage 5 (1.9.0): the engine (Leaflet or MapLibre, and why), the first view and the zoom range. Split from build_page() unchanged."""
    # ---- engine
    tiled_candidates = [d["id"] for d in vector_defs if not d["context"] and wml.needs_tiles(collections[d["id"]])]
    vector_base = any(b["mode"] in ("vector", "vector-file") for b in base_defs)
    live_mvt = [d["id"] for d in live_defs if d["live"] == "mvt"]
    engine, engine_why = choose_engine(cfg["engine"], features=payload_features, tiled=tiled_candidates, vector_base=vector_base,
                                       time_on=bool(time_cfg), compare_on=bool(compare_cfg), three_d=bool(s3), story=bool(cfg["story"]), live_vector=live_mvt)
    if engine == "maplibre":
        why_not = wml.available()
        if why_not:
            findings.append(finding("MAP-WEB-015", "WARN", f"the MapLibre engine cannot be built here ({why_not}); the page is built with Leaflet"))
            engine, engine_why = "leaflet", f"MapLibre is not available ({why_not})"
    if engine == "leaflet" and s3:
        what = [w for w, on in (("a pitch or bearing", s3["pitch_asked"] or s3["bearing"]), ("terrain", s3["terrain"]), ("extruded layers " + ", ".join(s3["extruded"]), s3["extruded"]),
                                ("masts " + ", ".join(s3["masts"]), s3["masts"])) if on]
        findings.append(finding("MAP-WEB-025", "WARN", f"3D is asked for ({'; '.join(what) or 'webmap.3d'}) but the page is drawn with Leaflet ({engine_why}), which is flat and north up: "
                                "the page is 2D. Leave webmap.engine at auto (or maplibre) for 3D", what))
        s3 = None
        for d in defs:
            d.pop("d3", None)
    if engine == "leaflet" and cfg["story"] and any(isinstance(c, dict) and ((c.get("view") or {}).get("pitch") or (c.get("view") or {}).get("bearing"))
                                                   for c in (cfg["story"].get("chapters") or [])):
        findings.append(finding("MAP-WEB-025", "INFO", "the story's chapters have a pitch or bearing, which the Leaflet page does not draw: its chapters fly flat and north up"))
    if engine == "leaflet" and live_mvt:
        findings.append(finding("MAP-SVC-043", "BLOCK", f"live vector tile layer(s) {', '.join(live_mvt)} are drawn by the MapLibre engine only; this page is built with Leaflet "
                                f"({engine_why}). Leave webmap.engine at auto or maplibre"))
    if engine == "maplibre":
        for d in live_defs:
            if d["raster"] and not d.get("cors_checked"):
                findings.append(finding("MAP-WEB-005", "WARN", f"live layer '{d['id']}': the MapLibre page fetches every tile with fetch(), which works only if {d['host']} allows "
                                        "requests from web pages (CORS); check it with ogc_cors_check and record the day as source.cors_checked, or use webmap.engine: leaflet", layer=d["id"]))
                d["needs_cors"] = True
    if engine == "leaflet":
        for b in [b for b in base_defs if b["mode"] in ("vector", "vector-file")]:
            findings.append(finding("MAP-WEB-015", "WARN", f"{b['title']} is a vector basemap, which only the MapLibre engine draws; the page is built with Leaflet "
                                    "(webmap.engine), so the bundled background is used"))
            base_defs.remove(b)
        big_pts = [d["id"] for d in vector_defs if d["kind"] == "point" and len(collections[d["id"]]["features"]) > wml.CLUSTER_MIN_POINTS]
        if big_pts and cfg["asked_controls"].get("cluster"):
            findings.append(finding("MAP-WEB-015", "INFO", f"clustering is drawn by the MapLibre engine; this Leaflet page draws every point of {', '.join(big_pts)} "
                                    "(webmap.engine: auto picks MapLibre for a large point layer)"))
        if cfg["asked_controls"].get("export_image") and ctl["print_view"]:
            findings.append(finding("MAP-WEB-015", "INFO", "the Leaflet page saves a picture through Print (choose Save as PDF there); the MapLibre engine saves a PNG"))
        if payload_features > AUTO_MAPLIBRE_FEATURES:
            notes.append(f"the page draws {payload_features:,} features with Leaflet; webmap.engine: auto would use MapLibre for this many")
    base_defs.append({"id": "bundled", "mode": "bundled", "title": "Bundled background (works offline)"})
    notes.append(f"engine: {engine} ({engine_why})")
    hosts = []
    for b in base_defs:
        if b["mode"] in ("tiles", "vector") and b.get("host") and b["host"] not in [h["host"] for h in hosts]:
            hosts.append({"host": b["host"], "purpose": f"basemap tiles ({b['title']})", "provider": b["id"], "entry": b["entry"]})
    for hh in (live["hosts"] if live else []):
        if hh["host"] not in [h["host"] for h in hosts]:
            hosts.append(hh)

    # ---- view
    wgs = mp._wgs84_extent(plan)
    iv = cfg["view"]
    bbox = list(iv["bbox"]) if iv.get("mode") == "bbox" and iv.get("bbox") else [float(v) for v in wgs]
    pad_pct = float((spec.get("extent") or {}).get("padding_pct", 6))
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    mb = [max(-180.0, bbox[0] - w), max(-85.0, bbox[1] - h), min(180.0, bbox[2] + w), min(85.0, bbox[3] + h)]
    for d in live_defs:                              # a live WFS is read once, for the area the page can show
        if d["live"] == "wfs":
            d["url"] = d["url"].replace("{bbox}", urllib.parse.quote(wlv.bbox_text(mb), safe=",:"))
    tiles_active = base_defs and base_defs[0]["mode"] in ("tiles", "vector", "vector-file")
    fit = fit_zoom(bbox)
    bundled_max = max(BUNDLED_MAX_ZOOM, min(DEFAULT_MAX_ZOOM, fit + BUNDLED_ZOOM_IN))
    asked_max = int(iv["max_zoom"]) if iv.get("max_zoom") else None
    if engine == "maplibre" and s3 and (s3["extruded"] or s3["masts"]) and not asked_max:
        bundled_max = max(bundled_max, w3.MAX_ZOOM_3D)              # buildings and masts are seen from street level (M15)
    if engine == "maplibre" and not asked_max:                        # a dense point layer: the page zooms until its points stand apart
        import tiles_out as to
        for d in vector_defs:
            col = collections[d["id"]]
            if d["kind"] == "point" and len(col["features"]) > wml.CLUSTER_MIN_POINTS:
                g3 = wml.collection_gdf(col).to_crs("EPSG:3857")
                top, _why = to.auto_maxzoom(g3, "point")
                if top > bundled_max:
                    notes.append(f"layer '{d['id']}': the page zooms to {min(DEFAULT_MAX_ZOOM, top)}, where its points stand apart")
                    bundled_max = max(bundled_max, min(DEFAULT_MAX_ZOOM, top))
    if tiles_active:
        b0 = base_defs[0]
        max_zoom = min(asked_max, b0["maxZoom"]) if asked_max else min(DEFAULT_MAX_ZOOM, b0["nativeMaxZoom"] if b0["mode"] == "tiles" else b0["maxZoom"])
        if asked_max and asked_max > b0["maxZoom"]:
            findings.append(finding("MAP-WEB-010", "WARN", f"webmap.initial_view.max_zoom {asked_max} is past what {b0['title']} serves (zoom {b0['nativeMaxZoom']}, enlarged up to {b0['maxZoom']}); "
                                    f"the page stops at {b0['maxZoom']}"))
        elif max_zoom > b0["nativeMaxZoom"]:
            findings.append(finding("MAP-WEB-010", "INFO", f"past zoom {b0['nativeMaxZoom']} the page enlarges {b0['title']}'s last tiles (up to {max_zoom}); they look soft but never blank"))
    else:
        max_zoom = asked_max or bundled_max
        if max_zoom > BUNDLED_MAX_ZOOM:
            findings.append(finding("MAP-WEB-010", "INFO", f"the data needs zoom {fit} to fit, so the page zooms to {max_zoom}; the bundled background is generalized and is faded past zoom "
                                    f"{BUNDLED_MAX_ZOOM}, where its coasts and borders are approximate. For street-level context use a tile basemap"))
    min_zoom = int(iv.get("min_zoom") if iv.get("min_zoom") is not None else 2)
    if fit < min_zoom:
        findings.append(finding("MAP-WEB-010", "WARN", f"the data needs zoom {fit} to fit in a window, below the page's minimum zoom {min_zoom}; the first view shows only part of it"))
    if fit > max_zoom:
        findings.append(finding("MAP-WEB-010", "WARN", f"the data needs zoom {fit} to fit, above the page's maximum zoom {max_zoom}; the first view shows it smaller than the window"))
    return bbox, bundled_max, engine, engine_why, fit, h, hosts, max_zoom, mb, min_zoom, pad_pct, s3, vector_base, wgs


def _page_names_tiles(base_defs, bbox, bdefs, blocks, brecs, cfg, collections, ctl, defs, engine, findings, fit, flat_links, label_by_layer, max_zoom, min_zoom, mr, notes, pfields, plan, s3, search_field, t, tempfile, vector_base, vector_defs, w3, wml):
    """build_page, stage 6 (1.9.0): place, water and admin names; on MapLibre the large layers as embedded PMTiles, clusters, glyphs and the style. Split from build_page() unchanged."""
    # ---- names: place, water and admin names as text labels, tiered by zoom, and feature labels for the layers the spec labels
    names_col, names_stats = names_collection(plan, bbox, fit, max_zoom)
    notes += [n for n in plan.notes if n not in notes]              # what the layers said while they were packed (a windowed layer)
    names_on = bool(names_stats.get("on")) and bool(names_col["features"])
    if names_stats.get("script_fallback"):                   # 1.5.0: the page's glyphs are Inter's; such names are in English here
        sc_ = sorted(names_stats.pop("script_fallback"))
        findings.append({"id": "MAP-LOC-003", "severity": "WARN", "message": f"place names in {', '.join(sc_)} are given in English on the web page: "
                         "its label glyphs cover Latin, Greek and Cyrillic (the print map outlines the others in their Noto face)", "detail": {"scripts": sc_}})
    if names_on:
        collections[NAMES_ID] = names_col
        notes.append(f"{names_stats['places']} place names, {names_stats['water']} water names, {names_stats['admin']} admin names on the page, shown by zoom"
                     + (f" (capped at {names_stats['capped']})" if names_stats.get("capped") else ""))
    feature_labels = [{"layer": l.id, "field": label_by_layer[l.id]} for l in plan.layers
                      if l.kind == "point" and label_by_layer.get(l.id) and label_by_layer[l.id] in l.gdf.columns]
    label_zoom = max(min_zoom, fit)

    # ---- MapLibre: large layers as embedded PMTiles (clusters baked in), clusters, the style and the glyphs it needs
    ml = None
    p3 = None
    flat_profiles: dict = {}
    tiles: dict = {}
    clusters: dict = {}
    search_idx: dict = {}
    search_packed: dict = {}                         # 1.0.0: {layer id: base64 of the packed index} for tiled layers over SEARCH_TILED_MAX
    if engine == "maplibre":
        scratch = Path(tempfile.mkdtemp(prefix="gmap_web_"))
        zmin_ml, zmax_ml = int(wml.ml_zoom(min_zoom)), max(int(wml.ml_zoom(min_zoom)), int(math.ceil(wml.ml_zoom(max_zoom))))
        tiles_text: dict[str, set[str]] = {}
        try:
            for d in vector_defs:
                col = collections[d["id"]]
                n = len(col["features"])
                pts = d["kind"] == "point"
                cluster_on = bool(ctl["cluster"]) and pts and n > wml.CLUSTER_MIN_POINTS and not d.get("time")
                has_classes = any("_c" in f["properties"] for f in col["features"][:50])
                cl_fill = t["emphasis"] if has_classes else d["style"]["fill"]
                cl_text = t["legend_bg"] if contrast(t["legend_bg"], cl_fill) >= contrast(t["label"], cl_fill) else t["label"]
                if not d["context"] and wml.needs_tiles(col):
                    lspec = next((x.spec for x in plan.layers if x.id == d["id"]), {}) or {}
                    import lod as lod_
                    lcfg = (lod_.config(lspec["extrude"]) if isinstance(lspec.get("extrude"), dict) else
                            lod_.config(lspec["lod"]) if isinstance(lspec.get("lod"), dict) else None)    # 1.6.0 (R4-BLD): a level-of-detail pyramid
                    tl = wml.tile_layer(d["id"], col, d["kind"], zmin=zmin_ml, zmax=zmax_ml, cluster=cluster_on, scratch=scratch, attribution=d.get("credit"),
                                        lod_cfg=lcfg)
                    for z_, r_ in ((tl.get("lod") or {}).get("report") or {}).items():
                        if r_["area_kept"] < lod_.KEEP_AREA_MIN:     # MAP-WEB-018 extended: the share of the footprint area each zoom keeps
                            findings.append(finding("MAP-WEB-018", "WARN", f"layer '{d['id']}': at zoom {z_ + 1} its blocks keep {r_['area_kept']:.1%} of the footprint "
                                                    f"area (under {lod_.KEEP_AREA_MIN:.0%}); raise extrude.merge_gap_m or min_zoom",
                                                    {"layer": d["id"], "zoom": z_ + 1, "area_kept": r_["area_kept"]}))
                    tl["b64"] = wml.b64_file(tl["path"])
                    tiles[d["id"]] = tl
                    notes += tl["notes"]
                    notes.append(f"layer '{d['id']}': {n:,} features go into the page as PMTiles ({tl['bytes'] / 1048576:.1f} MB, zooms {tl['zmin'] + 1} to {tl['zmax'] + 1})")
                    if d["searchable"] and search_field:
                        if n <= SEARCH_TILED_MAX:
                            search_idx[d["id"]] = search_index(col, search_field)
                        elif n <= SEARCH_PACKED_MAX:
                            b64_, k_, nb_ = search_index_packed(col, search_field)
                            search_packed[d["id"]] = b64_
                            notes.append(f"layer '{d['id']}': search covers its {k_:,} features through a packed index ({nb_ / 1048576:.1f} MB), unpacked by the "
                                         "browser when the page opens")
                        else:
                            notes.append(f"layer '{d['id']}': search does not cover its {n:,} features (over {SEARCH_PACKED_MAX:,}); it covers the other layers and the place names")
                    lf = label_by_layer.get(d["id"])
                    if lf:
                        tiles_text[d["id"]] = {str(f["properties"].get(lf)) for f in col["features"] if f["properties"].get(lf) is not None}
                    d["tiled"] = True
                    if d["downloadable"]:
                        d["downloadable"] = False
                        notes.append(f"layer '{d['id']}': its data is in tiles, so the page offers no download of it; the data package carries it")
                    collections[d["id"]] = {"type": "FeatureCollection", "features": []}
                    if cluster_on:
                        clusters[d["id"]] = {"points_zoom": tl["points_zoom"], "fill": cl_fill, "stroke": t["legend_bg"], "text": cl_text}
                elif cluster_on:
                    lon = [f["geometry"]["coordinates"][0] for f in col["features"]]
                    lat = [f["geometry"]["coordinates"][1] for f in col["features"]]
                    clusters[d["id"]] = {"points_zoom": wml.points_zoom(lon, lat, zmin_ml, zmax_ml), "fill": cl_fill, "stroke": t["legend_bg"], "text": cl_text}
                if d["id"] in clusters:
                    d["cluster"] = {"pointsZoom": clusters[d["id"]]["points_zoom"] + 1}
                    for i, b in enumerate(blocks):
                        if b.get("layer") == d["id"]:
                            nb = dict(b)
                            nb["note"] = ((b.get("note") or "") + " " if b.get("note") else "") + "A circle with a number is a group of nearby points; zoom in or click it to see them."
                            blocks[i] = nb
        finally:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)
        bundled_attr = "; ".join(a for a in ((((r or {}).get("license") or {}).get("attribution_text") or "").rstrip(".") for r in brecs) if a)
        st = wml.style(t=t, bdefs=bdefs, bundled_attr=bundled_attr, base_defs=[b for b in base_defs if b["mode"] != "bundled"],
                       defs=defs, tiles=tiles, label_fields={fl["layer"]: fl["field"] for fl in feature_labels}, clusters=clusters, names_on=names_on, label_zoom=label_zoom,
                       admin_color=mr.admin_label_color(t)[0], water_color=mr.water_label_color(t), detail_zoom=BUNDLED_MAX_ZOOM)
        for b in base_defs:                          # a basemap file is read through the same in-memory PMTiles source as the layers
            if b["mode"] == "vector-file":
                fpath = Path(b["file"])
                if cfg["basemap_file"] == "beside" or fpath.stat().st_size > wml.PMTILES_EMBED_MAX:
                    b["beside"] = fpath.name
                    st["sources"][f"base-{b['id']}"]["url"] = f"pmtiles://{fpath.name}"
                    findings.append(finding("MAP-WEB-020", "INFO", f"the basemap file {fpath.name} is not embedded ({fpath.stat().st_size / 1048576:.0f} MB); copy it beside the page and "
                                            "serve both from a web server (a page opened from a file cannot read a file beside it)"))
                else:
                    b["key"] = f"gm-base-{b['id']}"
                    b["b64"] = wml.b64_file(fpath)
                    st["sources"][f"base-{b['id']}"]["url"] = f"pmtiles://{b['key']}"
        if s3:                                       # 3D (M15): terrain tiles, extrusions, masts, links in the air, their profiles
            chp = [float((c.get("view") or {}).get("pitch")) for c in ((cfg["story"] or {}).get("chapters") or [])
                   if isinstance(c, dict) and isinstance((c.get("view") or {}).get("pitch"), (int, float))]
            p3 = w3.page(plan, dict(s3, top_pitch=max([float(s3["pitch"])] + chp)), st, defs, collections, bbox, pfields)   # a chapter may tilt further
            notes += p3["notes"]
            findings += p3["findings"]
        elif flat_links:                             # 1.0.0: a flat page: each link's profile chart in its popup, no tubes in the air
            for lid in flat_links:
                lk = next(x for x in plan.layers if x.id == lid)
                if len(collections[lid]["features"]) > FLAT_PROFILE_MAX:
                    notes.append(f"layer '{lid}': {len(collections[lid]['features']):,} links is more than {FLAT_PROFILE_MAX} profile charts (about 35 KB each); "
                                 "the popups show the verdict only")
                    continue
                _f, profs, _n, fs_ = w3.links(plan, lk, collections[lid], plan.brand)
                findings += fs_
                if profs:
                    flat_profiles.update(profs)
                    notes.append(f"layer '{lid}': {len(profs)} link(s) with their profile chart in the popup")
        for lid_, tl_ in tiles.items():                # 1.6.0: a pyramid's style layers, one zoom each, under the top zoom's
            if tl_.get("lod") and tl_["lod"]["zooms"]:
                st["layers"] = wml.lod_style(st["layers"], tl_["source_layer"], tl_["lod"])
        pstack = wml.pack_stack(plan.style) if _web_fonts(plan) else None
        if pstack:                                     # 1.10.0: the pack's label face on every label the engine draws
            nl = wml.use_pack_stack(st, pstack["stack"])
            notes.append(f"labels drawn by the map engine are in the pack's label face ({pstack['stack']}; {nl} label layer(s))")
        texts = wml.style_texts(st, collections, tiles_text)
        ranges = wml.glyph_ranges_for(texts)
        if vector_base and any(b["mode"] in ("vector", "vector-file") for b in base_defs):
            ranges.setdefault(pstack["stack"] if pstack else "Inter Regular", set()).update(wml.BASEMAP_GLYPH_RANGES)
        glyphs, gmissing = wml.glyph_payload(ranges, pstack)
        ml = {"style": st, "glyphs": glyphs, "glyph_missing": gmissing, "texts": texts}
    return clusters, feature_labels, findings, flat_profiles, label_zoom, ml, names_on, names_stats, notes, p3, search_idx, search_packed, tiles


def _page_frame(base_defs, blocks, brecs, cfg, collections, ctl, defs, engine, findings, names_on, panel_cfg, plan, rl, s3, search_field, source_counts, spec_in, story_cfg, t, tiles, time_cfg):
    """build_page, stage 8 (1.9.0): the frame: webmap.frame, the credits corner, the brand mark, search and the panel. Split from build_page() unchanged."""
    # ---- the frame (1.1.0): webmap.frame, the credits corner, the brand chip, the layer names and the playback label
    import webmap_frame as wf
    fc, ffs = wf.resolve(spec_in.get("webmap") or {}, story=bool(story_cfg), time_on=bool(time_cfg), three_d=bool(s3), delivery=cfg["delivery"], engine=engine)
    findings += ffs
    findings += wf.layer_names(defs, blocks, spec_in.get("layers") or [])
    entries_by_id = {e.get("id"): e for e in rl.all_entries("all")}
    credit_list, cfs = wf.credit_items(plan, base_defs, defs, entries_by_id, brecs)
    findings += cfs
    findings += wf.check_required(credit_list, provenance.required_credit_lines(plan))
    bd = wf.brand(plan, fc, t)
    shown_ids = [d for d in defs if not d["context"]]
    has_details = bool(panel_cfg) or (fc["popups"] != "off" and any(d.get("popup") for d in defs))
    layers_on = not (set(fc["_layers_off"]) >= {"layer_switcher"} and (len(base_defs) <= 1 or "basemap_switcher" in fc["_layers_off"]))
    want_tabs = [x for x in (fc["panel"].get("tabs") or wf.TABS) if x in wf.TABS and x not in fc["_drop_tabs"]]
    have = {"story": bool(story_cfg), "legend": bool(blocks), "layers": layers_on and (bool(shown_ids) or len(base_defs) > 1 or names_on), "details": has_details, "about": True}
    tabs = [x for x in want_tabs if have.get(x)][:5]
    default_tab = fc["panel"].get("default_tab") or "auto"
    if default_tab == "auto" or default_tab not in tabs:
        default_tab = "story" if "story" in tabs else ("legend" if "legend" in tabs else (tabs[0] if tabs else "about"))
    fc["panel"]["default_tab"] = default_tab
    named = sum(len(collections[d["id"]]["features"]) for d in defs if d.get("searchable") and d["id"] in collections) + sum(
        (tiles.get(d["id"]) or {}).get("features", 0) for d in defs if d.get("searchable"))
    named = max(named, sum(source_counts.get(d["id"], 0) for d in defs if d.get("searchable")))
    if fc["search"] == "auto":
        tsearch = bool(search_field or names_on) and (named > wf.SEARCH_AUTO_MIN or bool(cfg["asked_controls"].get("search")))
    else:
        tsearch = fc["search"] in ("on", True) and bool(search_field or names_on)
    ctl["search"] = tsearch
    ctl["scale"] = bool(fc["scale_bar"])
    ctl["popups"] = fc["popups"] != "off"
    any_dl = any(d["downloadable"] for d in defs)
    ctl["data_download"] = bool(any_dl and "download" in fc["tools"])
    tools, tfs, tool_notes = wf.tools_for(fc, engine=engine, downloadable=bool(any_dl), share=bool(ctl["share_state_in_url"]))
    findings += tfs
    if engine != "maplibre" and "save_image" in tools and "print" in tools:
        tools.remove("save_image")                        # the Leaflet page saves its picture through Print (Save as PDF)
    pbk = fc["playback"]
    tdisp = None
    if time_cfg:
        tdisp = wf.period_display(time_cfg, pbk.get("label"), (spec_in.get("webmap") or {}).get("time", {}).get("decimals"),
                                  (spec_in.get("webmap") or {}).get("time", {}).get("format"))
        if not pbk.get("label") and time_cfg.get("num") and any(" to " in x for x in time_cfg["labels"]):
            findings.append(finding("MAP-WEB-022", "INFO", "the playback label shows each period as a range ('6 to 6.2'); set webmap.frame.playback.label (for example "
                                    "\"km {end}\") to say what the numbers are"))
        tw = (spec_in.get("webmap") or {}).get("time") or {}
        if tw.get("label_format") and time_cfg.get("num") and not pbk.get("label"):     # 1.3.0: "km {value:.1f}" for a numeric field
            lo_, st_ = float(time_cfg["num"]["lo"]), float(time_cfg["num"]["step"])
            try:
                tdisp = [format_period_label(tw["label_format"], lo_ + k * st_) for k in range(len(time_cfg["labels"]))]
            except ValueError as ex:
                raise mp.PlanError(str(ex), gc.EXIT_USAGE) from None
        if tw.get("labels"):                              # 1.3.0: {value: label} renames periods for display; a chapter still names the value
            ren = {str(k): str(v) for k, v in tw["labels"].items()}
            tdisp = [ren.get(str(lab), d) for lab, d in zip(time_cfg["labels"], tdisp)]
        time_cfg["display"] = tdisp
        if pbk["mode"] == "slider":
            time_cfg["play"] = False
        if not pbk["on"]:                                  # no dock: the time is fixed at its first period (time.start)
            findings.append(finding("MAP-WEB-022", "INFO", f"the playback dock is off (webmap.frame.playback); the page shows period {time_cfg['labels'][max(0, min(len(time_cfg['labels']) - 1, time_cfg['start']))]} only"))
    return bd, credit_list, default_tab, fc, findings, pbk, tabs, tdisp, tool_notes, tools, tsearch, wf


def _page_config_tabs(attributions, base_defs, bbox, bd, bdefs, blocks, bundled_max, cfg, clusters, compare_cfg, conf, credit_list, ctl, default_tab, defs, els, engine, fc, feature_labels, findings, images, label_zoom, max_zoom, mb, min_zoom, names_on, names_stats, notes, notes_lines, p3, pad_pct, panel_cfg, pbk, plan, print_pics, rendered, s3, search_field, search_idx, search_packed, source_counts, spec, spec_in, story_cfg, subtitle, t, tabs, tdisp, tiles, time_cfg, title, title_el, tool_notes, tools, tsearch, w3, wf, wgs):
    """build_page, stage 9 (1.9.0): the page configuration the script reads, and the tabs of the panel. Split from build_page() unchanged."""
    # ---- page configuration
    import map_brand
    page_cfg = {
        "engine": engine, "minZoom": min_zoom, "maxZoom": max_zoom, "bundledMaxZoom": bundled_max, "bundledDetailZoom": BUNDLED_MAX_ZOOM, "bounds": [round(v, 5) for v in bbox],
        "maxBounds": [[mb[1], mb[0]], [mb[3], mb[2]]], "padding": int(max(8, min(80, pad_pct * 6))), "controls": ctl, "layers": defs, "bundled": bdefs,
        "attributions": attributions,
        "basemaps": [{k: v for k, v in b.items() if k in ("id", "mode", "url", "attribution", "maxZoom", "nativeMaxZoom", "nativeMinZoom", "zoomOffset", "tileSize", "host", "wordmark", "treatment")}
                     for b in base_defs],
        "searchField": search_field, "searchNames": bool(tsearch and names_on), "imperial": True, "downloadName": f"{plan.spec['id']}.geojson",
        "names": {"id": NAMES_ID, "on": names_on, "tier": names_stats.get("tier")}, "featureLabels": feature_labels, "labelZoom": label_zoom,
        "imagePanes": images > 0, "units": measure_units(spec_in, wgs), "time": time_cfg, "compare": compare_cfg,
        "legend": export_legend(blocks), "searchIndex": search_idx, "searchPacked": {k: f"gmap-search-{k}" for k in search_packed},
        "tokens": {k: t[k] for k in ("label", "land", "water", "legend_bg", "legend_frame", "frame", "emphasis", "background")},
        # the PNG text chunks of the print PNG, base64 so the page text holds none of their words (the brand lint reads the page for them)
        "export": {"fileName": f"{plan.spec['id']}.png", "text64": base64.b64encode(json.dumps(map_brand.props_for(plan.brand, plan.manifest, "png")["tEXt"]).encode("ascii")).decode("ascii")},
        "frame": {"preset": fc["preset"], "panel": {k: fc["panel"][k] for k in ("show", "side", "width", "start", "remember", "default_tab")},
                  "playback": {k: pbk.get(k) for k in ("mode", "position", "speeds", "loop", "autoplay")} | {"on": pbk["on"]},
                  "legend": {"float": bool(fc["legend"]["float"]), "toggles": bool(fc["legend"].get("toggles", True))},
                  "brand": {"show": bd["show"], "name": bd["name"]}, "attribution": fc["attribution"], "popups": fc["popups"], "shortcuts": fc["shortcuts"],
                  "fade_rail": fc["fade_rail"], "tools": tools, "delivery": fc["delivery"],
                  "credits": [{k: it[k] for k in ("key", "corner", "links", "full", "required", "collapse", "export", "short", "dynamic", "bases", "layers", "logo")} for it in credit_list]},
    }
    if engine == "maplibre":
        page_cfg["ml"] = {"pmtiles": [{"key": tl["key"], "el": f"gmap-pmtiles-{tl['key']}"} for tl in tiles.values()]
                          + [{"key": b["key"], "el": f"gmap-pmtiles-{b['key']}"} for b in base_defs if b.get("key")],
                          "tiles": {lid: {"key": tl["key"], "sourceLayer": tl["source_layer"], "pointsZoom": tl["points_zoom"],
                                          "layers": tl["layers"], "features": source_counts[lid]} for lid, tl in tiles.items()},
                          "clusters": {lid: {"pointsZoom": c["points_zoom"]} for lid, c in clusters.items()}}
        if p3 and p3["dem"]:
            page_cfg["ml"]["dem"] = w3.dem_config(p3["dem"])
    if p3:
        page_cfg["view3d"] = p3["cfg"]
    story_in = cfg["story"] or {}
    if story_cfg:
        page_cfg["story"] = {"chapters": [{k: c[k] for k in ("title", "view", "layers", "time", "select", "id")} for c in story_cfg["chapters"]],
                             "navigation": story_in.get("navigation") or "buttons", "follow_time": bool(story_in.get("follow_time")),
                             "printPictures": print_pics}
    if panel_cfg:
        page_cfg["panel"] = panel_cfg

    # ---- the panel's tabs
    wid = esc
    legend_inner = "".join(wf.legend_block(b, toggles=bool(fc["legend"].get("toggles", True)), legend_html=legend_block_html(b)) for b in blocks)
    float_inner = "".join(legend_block_html(b) for b in blocks) if fc["panel"]["show"] == "off" else ("" if not blocks else " ")   # with a panel, frame.js copies its Legend tab
    panes = {}
    if "story" in tabs:
        panes["story"] = wf.story_tab(story_cfg, message=spec["message"], cards=story_in.get("cards") or "compact", time_display=tdisp, panel_cfg=panel_cfg)
    if "legend" in tabs:
        panes["legend"] = legend_inner
    if "layers" in tabs:
        panes["layers"] = wf.layers_tab(defs, blocks, base_defs if len(base_defs) > 1 and "basemap_switcher" not in fc["_layers_off"] else [],
                                        names_id=NAMES_ID if names_on else None, three_d=(p3["cfg"] if p3 and (p3["cfg"] or {}).get("terrain") else None),
                                        rows=("layer_switcher" not in fc["_layers_off"]))
    if "details" in tabs:
        panes["details"] = (f'<p id="gm-details-hint" class="gm-hint">{wid((panel_cfg or {}).get("hint") or "Click a feature on the map to see its details here.")}</p>'
                            f'<div id="gm-details-feature"></div>'
                            + (f'<h2>{wid(panel_cfg["title"])}</h2><p id="gm-kpi-what" aria-live="polite">{wid(panel_cfg["hint"])}</p><dl id="gm-kpi-dl"></dl>' if panel_cfg else ""))
    import credits as crd
    try:
        sources_line, _crep = crd.consolidated(plan, label="")
    except Exception:  # noqa: BLE001 - the About tab still lists every source
        sources_line = ""
    notes_lines = list(notes_lines) + tool_notes
    if "about" in tabs:
        panes["about"] = wf.about_tab(plan, message=spec["message"], sources_line=sources_line, items=credit_list, notes_lines=notes_lines,
                                      base_defs=base_defs, brand=bd, house=plan.manifest.get("house_brand") == plan.brand,
                                      made=f'{plan.manifest.get("plugin_name", "geo-mapping")} {plan.manifest.get("version", "")}'.strip(),
                                      date=gl.plan_date_text(plan, today()), shortcuts=fc["shortcuts"])
    frame_parts = wf.markup(fc=fc, tabs=tabs, panes=panes, default_tab=default_tab, title=title, subtitle=subtitle,
                            tagline=(title_el.get("tagline") or ""), logo=logo_html(plan, t["legend_bg"]) if bd["show"] == "chip" else "",
                            small_logo=logo_html(plan, t["legend_bg"], 20) if bd["show"] == "chip" else "", brand=bd, items=credit_list,
                            tools=tools, search=tsearch, search_field=search_field, names_on=names_on, three_d=bool(s3), engine=engine,
                            story=story_cfg, time_cfg=time_cfg, tdisp=tdisp, float_legend=float_inner, has_wordmark=any(b.get("wordmark") for b in base_defs),
                            share=bool(ctl["share_state_in_url"]))
    extra_ui = ""
    if "measure" in tools:
        extra_ui += '<div id="gm-measure" role="status" aria-live="polite" hidden></div>\n'
    if compare_cfg:
        pos = max(5.0, min(95.0, 100 * compare_cfg["position"]))
        extra_ui += (f'<div id="gm-compare" role="separator" aria-orientation="vertical" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pos:.0f}" style="left:{pos:.2f}%">'
                     f'<button type="button" aria-label="Drag, or use the arrow keys, to move the divider. Left: {esc(compare_cfg["leftLabel"])}. Right: {esc(compare_cfg["rightLabel"])}">&#8249;&#8250;</button></div>\n'
                     f'<p class="gm-cmp-tag" id="gm-cmp-tag-l">{esc(compare_cfg["leftLabel"])}</p><p class="gm-cmp-tag" id="gm-cmp-tag-r">{esc(compare_cfg["rightLabel"])}</p>\n')
    conf_el = els.get("confidentiality_banner") or {}
    conf_html = ""
    if conf and conf_el.get("on", "auto") is not False:
        conf_html = f'<div class="gm-conf" role="note">{esc(conf_el.get("text") or "CLIENT CONFIDENTIAL")}</div>'
    elif conf:
        findings.append(finding("MAP-LIC-007", "BLOCK", f"layer(s) {', '.join(conf)} are client-confidential but the confidentiality banner is off"))
    alt = rendered.alt_text
    summary = (f"<p>{esc(alt)}</p><ul>" + "".join(f"<li>{esc(d['title'])}: {esc(d['role'])}, {source_counts.get(d['id'], 0) if d['kind'] != 'image' else 0} features</li>" for d in defs) + "</ul>")
    fb_img = noscript = ""
    if cfg["fallbacks"]["static_image"]:
        # one copy of the picture: shown when the engine cannot start, and, with scripts off, by the noscript style (M14: it was carried twice)
        fb_uri = fallback_uri(rendered, title, notes)
        fb_img = f'<figure id="gm-fallback" hidden><img alt="{esc(alt)}" src="{fb_uri}"></figure>'
        noscript = '<noscript><style>#gm-fallback[hidden]{display:flex!important}#gm-map{display:none}</style></noscript>'
    return conf_html, extra_ui, fb_img, frame_parts, map_brand, noscript, page_cfg, summary


def _page_document(audience, base_defs, bd, cfg, clusters, collections, compare_cfg, conf, conf_html, credit_list, ctl, defs, engine, engine_why, entries, extra_ui, fb_img, fc, findings, fit, flat_profiles, frame_parts, h, hosts, live_defs, map_brand, max_zoom, min_zoom, ml, noscript, notes, p3, page_cfg, panel_cfg, payload_features, plan, rendered, search_field, search_packed, source_counts, spec, story_cfg, story_pics, summary, t, tabs, tiles, time_cfg, title, tools, w3, wf, wlv, wml, ws):
    """build_page, stage 10 (1.9.0): the head (stamps, hidden accreditation, host manifest, CSP), the vendor scripts and data blobs, and the document. Split from build_page() unchanged."""
    # ---- head: stamps, hidden accreditation, host manifest, CSP
    props = map_brand.props_for(plan.brand, plan.manifest, "html")
    root_attrs = " ".join(f'{k}="{esc(v)}"' for k, v in props["root_attributes"].items())
    metas = "".join(f'<meta name="{esc(m["name"])}" content="{esc(m["content"])}">' for m in props["meta"])
    tile_hosts = [h["host"] for h in hosts]
    raster_hosts = [b["host"] for b in base_defs if b["mode"] == "tiles"]
    fetch_hosts = [b["host"] for b in base_defs if b["mode"] == "vector" and b.get("host")]
    if engine == "maplibre":
        fetch_hosts += raster_hosts                  # MapLibre fetches raster tiles with fetch(): connect-src, and no img-src host (test_m14_web proves both)
    logo_hosts = []
    for b in base_defs:                              # 0.16.0: a provider's logo, read from its stylesheet on the tile host; 1.1.0: or its own picture
        if b.get("wordmark") and b["wordmark"].get("css"):
            fetch_hosts.append(urllib.parse.urlsplit(b["wordmark"]["css"]).hostname)
        elif b.get("wordmark") and b["wordmark"].get("img"):
            logo_hosts.append(urllib.parse.urlsplit(b["wordmark"]["img"]).hostname)
    live_img, live_conn = wlv.hosts_for(live_defs, engine)
    csp = wml.csp(engine, list(dict.fromkeys((raster_hosts if engine == "leaflet" else []) + live_img + logo_hosts)), list(dict.fromkeys(fetch_hosts + live_conn)),
                  self_fetch=any(b.get("beside") for b in base_defs))
    robots = '<meta name="robots" content="noindex,nofollow">' if (conf or audience in ("internal",)) else ""
    page_js = asset_web("page.js")
    if engine == "maplibre":
        lib_js, lib_css, pm_js = wml.vendor_text("maplibre-gl.js"), wml.vendor_text("maplibre-gl.css"), wml.vendor_text("pmtiles.js")
        libs = [{"name": "maplibre-gl", "version": wml.MAPLIBRE_VERSION, "inlined": True, "license": "BSD-3-Clause",
                 "sha256": {"maplibre-gl.js": sha256_text(lib_js), "maplibre-gl.css": sha256_text(lib_css)}},
                {"name": "pmtiles", "version": wml.PMTILES_VERSION, "inlined": True, "license": "BSD-3-Clause", "sha256": {"pmtiles.js": sha256_text(pm_js)}}]
        lic_txt = (wml.vendor_text("LICENSE-maplibre-gl.txt") + "\n\npmtiles " + wml.PMTILES_VERSION + "\n" + wml.vendor_text("LICENSE-pmtiles.txt")).replace("*/", "* /").replace("-->", "- ->")
        head_vendor = (f'<!-- gmap:vendor:begin maplibre-gl {wml.MAPLIBRE_VERSION}, pmtiles {wml.PMTILES_VERSION} (BSD-3-Clause)\n{lic_txt}\n-->\n'
                       f'<style id="gm-maplibre-css">{lib_css}</style>\n<!-- gmap:vendor:end -->\n')
        body_vendor = (f'<!-- gmap:vendor:begin maplibre-gl {wml.MAPLIBRE_VERSION} -->\n<script id="gm-maplibre-js">{lib_js}</script>\n'
                       f'<script id="gm-pmtiles-js">{pm_js}</script>\n<!-- gmap:vendor:end -->\n')
        engine_js = asset_web("engine-maplibre.js")
        blobs = (f'<script type="application/json" id="gmap-style">{script_json(ml["style"])}</script>\n'
                 f'<script type="application/json" id="gmap-glyphs">{script_json(ml["glyphs"])}</script>\n'
                 + (f'<script type="application/octet-stream" id="gmap-dem">{base64.b64encode(p3["dem"]["blob"]).decode("ascii")}</script>\n' if p3 and p3["dem"] else "")
                 + (f'<script type="application/octet-stream" id="gmap-profiles">{w3.b64_json(p3["profiles"])}</script>\n' if p3 and p3["profiles"] else "")
                 + (f'<script type="application/octet-stream" id="gmap-profiles">{w3.b64_json(flat_profiles)}</script>\n' if flat_profiles else "")
                 + "".join(f'<script type="application/octet-stream" id="gmap-search-{esc(k)}">{v}</script>\n' for k, v in search_packed.items())
                 + "".join(f'<script type="application/octet-stream" id="gmap-pmtiles-{tl["key"]}">{tl["b64"]}</script>\n' for tl in tiles.values())
                 + "".join(f'<script type="application/octet-stream" id="gmap-pmtiles-{b["key"]}">{b["b64"]}</script>\n' for b in base_defs if b.get("key")))
    else:
        lib_js, lib_css = vendor("leaflet.js"), vendor("leaflet.css")
        libs = [{"name": "leaflet", "version": LEAFLET_VERSION, "inlined": True, "license": "BSD-2-Clause",
                 "sha256": {"leaflet.js": sha256_text(lib_js), "leaflet.css": sha256_text(lib_css)}}]
        lic_txt = vendor("LICENSE").replace("*/", "* /")
        head_vendor = f'<!-- gmap:vendor:begin leaflet {LEAFLET_VERSION} (BSD-2-Clause)\n{lic_txt}\n-->\n<style id="gm-leaflet-css">{lib_css}</style>\n<!-- gmap:vendor:end -->\n'
        body_vendor = f'<!-- gmap:vendor:begin leaflet {LEAFLET_VERSION} -->\n<script id="gm-leaflet-js">{lib_js}</script>\n<!-- gmap:vendor:end -->\n'
        engine_js = asset_web("engine-leaflet.js")
        blobs = ""
    manifest = {"schema": "gmap-host-manifest/1", "hosts": hosts, "libraries": libs, "analytics": False}
    map_div = '<div id="gm-map" role="application" aria-label="Interactive map: {t}. {m}"></div>'.format(t=esc(title), m=esc(spec["message"]))
    story_print = ws.print_html(story_cfg, story_pics, title=title, message=spec["message"]) + "\n" if story_cfg else ""
    root_cls = f' class="gm-frame gm-preset-{fc["preset"]} gm-side-{fc["panel"]["side"]}{" gm-story-page" if story_cfg else ""}{" gm-nopanel" if fc["panel"]["show"] == "off" else ""}"'
    frame_css = wf.css(t, fc["panel"]["width"])
    frame_js = asset_web("frame.js")
    doc = (
        f'<!doctype html>\n<html lang="en" {root_attrs} data-map-id="{esc(plan.spec["id"])}" data-engine="{engine}">\n<head>\n<!-- adocs:no-furniture reason="a map page with its own frame, brand mark and credits" -->\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{esc(title)}</title>\n'
        f'<meta name="description" content="{esc(spec["message"])}">\n{robots}{metas}\n'
        f'<meta http-equiv="Content-Security-Policy" content="{esc(csp)}">\n'
        f'<style id="gm-font">{page_fonts(plan)}</style>\n'
        f'{head_vendor}'
        f'<style id="gm-style">{pack_css(plan, build_css(t))}</style>\n<style id="gm-frame-css">{pack_css(plan, frame_css)}</style>\n</head>\n<body>\n{wf.sprite()}\n'
        f'<div id="gm-root" role="region" aria-label="{esc(title)}"{root_cls} data-panel="{"off" if fc["panel"]["show"] == "off" else "open"}" data-sheet="peek">\n'
        f'{frame_parts["panel"]}\n'
        f'<div id="gm-stage">\n{map_div}\n{fb_img}\n<div id="gm-banners" role="status" aria-live="polite"></div>\n{conf_html}\n{extra_ui}'
        f'{frame_parts["overlays"]}\n</div>\n'
        f'{frame_parts["menus"]}\n'
        f'<section class="gm-sr" id="gm-summary" aria-label="Text summary of the map">{summary}</section>\n</div>\n{noscript}\n'
        f'{story_print}'
        f'<script type="application/json" id="gmap-host-manifest">{script_json(manifest)}</script>\n'
        f'<script type="application/json" id="gmap-config">{script_json(page_cfg)}</script>\n'
        f'<script type="application/json" id="gmap-data">{script_json(collections)}</script>\n'
        f'{blobs}'
        f'{body_vendor}'
        f'<script id="gm-page-js">{engine_js}\n{frame_js}\n{page_js}</script>\n</body>\n</html>\n'
    )
    data_bytes = len(script_json(collections).encode("utf-8")) + sum(len(tl["b64"]) for tl in tiles.values())
    report = {"map_window": rendered.report.get("map_window"),          # 0.26.0 (TODO M8): the print frame's window, as the GeoPDF and GeoTIFF carry it
              "engine": engine, "engine_why": engine_why, "features": payload_features, "source_features": source_counts, "data_bytes": data_bytes,
              "page_bytes": len(doc.encode("utf-8")), "hosts": tile_hosts, "controls": ctl,
              "layers": [d["id"] for d in defs], "basemaps": [b["id"] for b in base_defs], "min_zoom": min_zoom, "max_zoom": max_zoom, "confidential": conf,
              "audience": audience, "delivery": cfg["delivery"], "contrast": round(contrast(t["label"], t["legend_bg"]), 2),
              "credits_contrast": round(contrast(t["label"], t["legend_bg"]), 2), "frame": {"preset": fc["preset"], "tabs": tabs, "tools": tools,
              "credits": [it["corner"] for it in credit_list if it["required"]], "brand": bd["show"]},
              "provider_limits": {b["id"]: b["nativeMaxZoom"] for b in base_defs if b["mode"] == "tiles"}, "fit_zoom": fit,
              "theme": cfg["theme"], "has_noindex": bool(robots), "confirm_publish": cfg["confirm_publish"], "search_field": search_field,
              "downloadable": [d["id"] for d in defs if d["downloadable"]], "notes": notes, "static_svg_bytes": len(rendered.svg.encode("utf-8")),
              "tiled": {lid: {"bytes": tl["bytes"], "layers": tl["layers"], "points_zoom": tl["points_zoom"]} for lid, tl in tiles.items()},
              "clusters": sorted(clusters), "time": time_cfg, "compare": compare_cfg, "csp": csp, "units": page_cfg["units"],
              "glyph_ranges": sorted(ml["glyphs"]) if ml else [], "glyph_missing": ml["glyph_missing"] if ml else [],
              "three_d": p3["report"] if p3 else None,
              "story": ({"chapters": len(story_cfg["chapters"]), "pictures": sum(1 for x in story_pics if x)} if story_cfg else None),
              "panel": [f["field"] for f in panel_cfg["fields"]] if panel_cfg else None,
              "live": [{"id": d["id"], "protocol": d["live"], "host": d["host"], "profile": d["profile"], "cors": bool(d.get("cors_checked"))} for d in live_defs],
              "live_hosts": sorted({d["host"] for d in live_defs})}
    import web_lint
    findings += web_lint.lint_page(doc, report, plan, [b for b in base_defs if b["mode"] in ("tiles", "vector")])
    recs = tile_records(base_defs, entries)
    return doc, findings, recs, report


def build_page(spec_path, *, brand: str | None = None, template: str | None = None, env=None) -> WebPage:
    """Plan, resolve the basemap, choose the engine, assemble and return the page text with its findings. Writes nothing."""
    import tempfile
    import ref_library as rl
    import map_render as mr
    import webmap_maplibre as wml
    spec_in = gc.load_mapspec(Path(spec_path))
    cfg = config_of(spec_in)
    mode = "screen" if cfg["theme"] == "dark" else "print"
    import webmap_live as wlv
    live_specs: list[dict] = []

    def edit(sp):                                  # 0.15.0: live service layers go to the page only; the plan and the static picture never see them
        web_edit(sp)
        live_specs.extend(wlv.split(sp))
    plan = mp.prepare(spec_path, mode=mode, brand=brand, template=template, spec_edit=edit, use_tiles=False)
    spec = plan.spec
    t = plan.tokens
    findings: list[dict] = list(plan.findings) + pack_web_findings(plan)
    notes = list(plan.notes)
    audience = spec.get("audience") or "external"
    ctl = cfg["controls"]
    import webmap_3d as w3
    import webmap_story as ws
    s3 = w3.settings(spec_in)                     # 3D (M15): terrain, extrusions, masts, links in the air; None on a flat page
    if ctl.get("compass") and not s3:
        findings.append(finding("MAP-WEB-015", "WARN", "the compass control is drawn on a 3D page only (webmap.view pitch or bearing, or webmap.3d): this page is north-up, so no arrow is needed"))
    pfields = [f["field"] for f in ws.panel_fields(cfg["panel"])]
    pfields += [str(((c or {}).get("select") or {}).get("field")) for c in ((cfg["story"] or {}).get("chapters") or []) if isinstance(c, dict) and (c.get("select") or {}).get("field")]

    base_defs, entries, findings = _page_basemaps(audience, cfg, env, findings, plan, rl, spec_in, spec_path)

    if base_defs and base_defs[0]["mode"] in ("tiles", "vector", "vector-file") and str((spec_in.get("context") or {}).get("preset") or "auto") == "auto":
        # 1.10.0: as on paper, a tile or vector basemap carries its own roads, water and built-up land; the bundled context pack over it
        # drew them twice. `context.preset` named in the MapSpec keeps it.
        ctx_n = len([l for l in plan.layers if l.role == "context"])
        if ctx_n:
            plan.layers = [l for l in plan.layers if l.role != "context"]
            plan.notes.append(f"context pack left out of the page: {base_defs[0]['title']} carries its own roads and water (name context.preset to keep it)")
    bdefs, blocks, brecs, collections, conf, defs, findings, flat_links, images, label_by_layer, live, live_defs, notes, payload_features, search_field, source_counts, tcfg, time_layers, vector_defs = _page_layers(cfg, ctl, findings, live_specs, notes, pfields, plan, s3, spec, spec_path, t, w3, wlv)

    # ---- time slider: periods from the time layers' values; the classes stay those of the whole layer (no re-classing per period)
    time_cfg = None
    if tcfg and time_layers and ctl["time_slider"]:
        lorder = next((l.spec.get("time_order") for l in plan.layers if l.id in time_layers and l.spec.get("time_order")), None)
        try:
            tp = time_periods({**tcfg, "_layer_order": lorder}, {lid: collections[lid] for lid in time_layers if lid in collections})
        except ValueError as ex:
            raise mp.PlanError(f"webmap.time: {ex}", gc.EXIT_USAGE) from None
        if tp.get("kind") == "named":                # 1.3.0 (R1-TIME)
            if tp.get("order_from") == "natural":
                findings.append(finding("MAP-WEB-042", "INFO", f"webmap.time: the {len(tp['labels'])} named periods are in natural order ({', '.join(tp['labels'][:4])}"
                                        + (", ..." if len(tp["labels"]) > 4 else "") + "); set the layer's time_order or webmap.time.order to give another"))
            if tp.get("hidden"):
                findings.append(finding("MAP-WEB-043", "WARN", f"webmap.time: {tp['hidden']} feature(s) have a period the order does not list "
                                        f"({', '.join(tp.get('unlisted', [])[:4])}); they are hidden while the slider is on", tp.get("unlisted")))
        if tp["labels"]:
            mode_t = tcfg.get("mode") or "period"
            time_cfg = {"labels": tp["labels"], "layers": sorted(time_layers), "mode": mode_t, "play": bool(tcfg.get("play", True)),
                        "intervalMs": int(tcfg.get("interval_ms") or 1200), "field": next(iter(time_layers.values())), "start": int(tcfg.get("start", 0) or 0)}
            if tp.get("num"):
                time_cfg["num"] = {"lo": tp["num"]["lo"], "step": tp["num"]["step"]}
            if tp.get("starts"):
                time_cfg["starts"] = tp["starts"]
            if tp.get("kind") == "named":
                time_cfg["named"] = True
            notes.append(f"time slider: {len(tp['labels'])} periods ({tp['labels'][0]} to {tp['labels'][-1]}) on {', '.join(sorted(time_layers))}"
                         + (f"; {tp['missing']} feature(s) have no time and are hidden while the slider is on" if tp["missing"] else ""))
        else:
            findings.append(finding("MAP-WEB-022", "WARN", f"webmap.time: the time field has no values on layer(s) {', '.join(sorted(time_layers))}, so the page has no time slider"))
    elif tcfg and not time_layers:
        findings.append(finding("MAP-WEB-022", "WARN", "webmap.time is set but no layer has a time_field (or the field webmap.time.field names), so the page has no time slider"))
    for c in collections.values():                   # a time value never travels without its period number
        for f in c["features"]:
            f["properties"].pop("_tv", None)

    # ---- swipe compare
    compare_cfg = None
    cmp_in = cfg["compare"]
    if cmp_in and ctl["compare"]:
        ids_all = {d["id"] for d in defs}
        left, right = list(cmp_in.get("left") or []), list(cmp_in.get("right") or [])
        unknown = [x for x in left + right if x not in ids_all]
        if unknown:
            findings.append(finding("MAP-WEB-022", "BLOCK", f"webmap.compare names layer(s) the page does not have: {', '.join(unknown)}; the layers are {', '.join(sorted(ids_all))}", unknown))
        both = sorted(set(left) & set(right))
        if both:
            findings.append(finding("MAP-WEB-022", "BLOCK", f"webmap.compare puts {', '.join(both)} on both sides; a layer on both sides needs no listing"))
        if not unknown and not both and (left or right):
            title_of = {d["id"]: d["title"] for d in defs}
            compare_cfg = {"left": left, "right": right, "leftLabel": cmp_in.get("left_label") or ", ".join(title_of[x] for x in left) or "Both sides",
                           "rightLabel": cmp_in.get("right_label") or ", ".join(title_of[x] for x in right) or "Both sides", "position": float(cmp_in.get("position", 0.5))}
            for d in defs:
                d["side"] = "left" if d["id"] in left else ("right" if d["id"] in right else None)

    bbox, bundled_max, engine, engine_why, fit, h, hosts, max_zoom, mb, min_zoom, pad_pct, s3, vector_base, wgs = _page_engine_view(base_defs, cfg, collections, compare_cfg, ctl, defs, findings, live, live_defs, notes, payload_features, plan, s3, spec, time_cfg, vector_defs, w3, wlv, wml)

    clusters, feature_labels, findings, flat_profiles, label_zoom, ml, names_on, names_stats, notes, p3, search_idx, search_packed, tiles = _page_names_tiles(base_defs, bbox, bdefs, blocks, brecs, cfg, collections, ctl, defs, engine, findings, fit, flat_links, label_by_layer, max_zoom, min_zoom, mr, notes, pfields, plan, s3, search_field, t, tempfile, vector_base, vector_defs, w3, wml)

    # ---- story and KPI panel (M15): the chapters checked against the page's layers, periods and features
    story_cfg, sfs, snotes = ws.plan(cfg["story"], defs, collections, time_cfg, bbox)
    findings += sfs
    notes += snotes
    panel_cfg, pfs = ws.panel_config(cfg["panel"], defs, {l.id: [c for c in l.gdf.columns if c != l.gdf.geometry.name] for l in plan.layers})
    findings += pfs

    # ---- text
    els = spec.get("elements") or {}
    title_el = els.get("title") or {}
    title = text_lint.title_text(spec)
    subtitle = title_el.get("subtitle") or ""
    rendered = mr.render(plan)          # the static map: the fallback image, and the same checks the print build makes
    findings += [f for f in rendered.findings if f["id"].startswith(("MAP-DATA", "MAP-CART", "MAP-LIC", "MAP-CRS"))]
    story_pics = []
    print_pics = str((cfg["story"] or {}).get("print_pictures", "on")).lower() if story_cfg else "on"
    if print_pics in ("true", "false"):
        print_pics = "on" if print_pics == "true" else "off"
    if story_cfg and print_pics != "off":   # a static picture per chapter through the print path, for the print view (1.1.0: on, off or first)
        tvals = time_values(plan, time_layers, time_cfg, tp if time_cfg else None)
        chs = story_cfg["chapters"][:1] if print_pics == "first" else story_cfg["chapters"]
        story_pics, pnotes = ws.pictures(spec_path, chs, brand=brand, template=template, mode=mode, base_edit=print_edit,
                                         time_cfg=time_cfg, time_values=tvals, title=title,
                                         uri=lambda r, t_, n_: fallback_uri(r, t_, n_, dpi=int((cfg["story"] or {}).get("print_dpi") or 96)))
        story_pics += [None] * (len(story_cfg["chapters"]) - len(story_pics))
        notes += pnotes
    elif story_cfg:
        story_pics = [None] * len(story_cfg["chapters"])
        notes.append("story: chapter print pictures are off (webmap.story.print_pictures: off); the print view carries each chapter's words")
    notes_lines = source_note_lines(plan) if (els.get("source_note") or {}).get("on", True) else []
    tm_line = plan.pack["pack"].get("trademark_line")
    if tm_line and (els.get("trademark_line") or {}).get("on", "auto") in ("auto", True):
        notes_lines.append(tm_line)
    ds = els.get("date_stamp") or {}
    if ds.get("on", True):
        notes_lines.append(ds.get("text") if ds.get("text") not in (None, "auto") else f"Map produced {gl.plan_date_text(plan, today())}")
    attributions = []
    for r in brecs:                      # layer credits are in the source note; the attribution control carries the basemap's
        a = ((r or {}).get("license") or {}).get("attribution_text")
        a = esc(a.rstrip(".")) if a else None
        if a and a not in attributions:
            attributions.append(a)

    bd, credit_list, default_tab, fc, findings, pbk, tabs, tdisp, tool_notes, tools, tsearch, wf = _page_frame(base_defs, blocks, brecs, cfg, collections, ctl, defs, engine, findings, names_on, panel_cfg, plan, rl, s3, search_field, source_counts, spec_in, story_cfg, t, tiles, time_cfg)

    conf_html, extra_ui, fb_img, frame_parts, map_brand, noscript, page_cfg, summary = _page_config_tabs(attributions, base_defs, bbox, bd, bdefs, blocks, bundled_max, cfg, clusters, compare_cfg, conf, credit_list, ctl, default_tab, defs, els, engine, fc, feature_labels, findings, images, label_zoom, max_zoom, mb, min_zoom, names_on, names_stats, notes, notes_lines, p3, pad_pct, panel_cfg, pbk, plan, print_pics, rendered, s3, search_field, search_idx, search_packed, source_counts, spec, spec_in, story_cfg, subtitle, t, tabs, tdisp, tiles, time_cfg, title, title_el, tool_notes, tools, tsearch, w3, wf, wgs)

    doc, findings, recs, report = _page_document(audience, base_defs, bd, cfg, clusters, collections, compare_cfg, conf, conf_html, credit_list, ctl, defs, engine, engine_why, entries, extra_ui, fb_img, fc, findings, fit, flat_profiles, frame_parts, h, hosts, live_defs, map_brand, max_zoom, min_zoom, ml, noscript, notes, p3, page_cfg, panel_cfg, payload_features, plan, rendered, search_field, search_packed, source_counts, spec, story_cfg, story_pics, summary, t, tabs, tiles, time_cfg, title, tools, w3, wf, wlv, wml, ws)
    return WebPage(html=doc, findings=findings, report=report, stem=rendered.filename_stem, plan=plan, rendered=rendered, hosts=hosts, basemaps=base_defs, records=recs)


FLAT_PROFILE_MAX = 200           # 1.0.0: profile charts carried on a flat page (about 35 KB each)
FALLBACK_SVG_MAX = 1_500_000      # a static fallback picture larger than this as SVG goes into the page as a PNG


def fallback_uri(rendered, title: str, notes: list, dpi: int = 96) -> str:
    """The static picture as a data URI: the print SVG, or for a dense map (100,000 sites make an SVG of many megabytes) a PNG of it, at `dpi`
    (96; a story's chapters take `webmap.story.print_dpi`, 0.25.0)."""
    raw = rendered.svg.encode("utf-8")
    if len(raw) > FALLBACK_SVG_MAX:
        import map_export as mx
        png, why = None, ""
        try:                    # headless Chromium draws a dense SVG in seconds; the PDF route takes minutes for 100,000 circles
            png = mx._chromium_png(rendered.svg, dpi)
        except Exception as ex:  # noqa: BLE001 - Playwright is optional: the PDF route next
            why = str(ex)[:80]
        if png is None:
            try:
                png = mx.rasterize(mx.to_pdf_bytes(rendered.svg, title), dpi, rendered.svg)
            except Exception as ex:  # noqa: BLE001 - the SVG still works, only larger
                why = str(ex)[:80]
        if png:
            notes.append(f"the fallback picture is a PNG at {dpi} dpi ({len(png) / 1048576:.1f} MB): as SVG it would be {len(raw) / 1048576:.1f} MB")
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        notes.append(f"the fallback picture stays SVG ({len(raw) / 1048576:.1f} MB): it could not be made a PNG here ({why})")
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def preview(spec_path, env=None) -> dict:
    """What a page from this MapSpec will be, without building it (the webmap-build plan): its features, the page size to expect, the engine
    and why, the basemap, and what the page calls when it is opened. Reads the layer files' feature counts only; fetches nothing."""
    import pyogrio
    import ref_library as rl
    import webmap_maplibre as wml
    spec = gc.load_mapspec(Path(spec_path))
    cfg = config_of(spec)
    base = Path(spec_path).parent
    feats, big, unknown = 0, [], []
    for l in spec.get("layers") or []:
        s = l.get("source") or {}
        if l.get("role") in ("raster", "hillshade"):
            continue
        n = None
        if s.get("type") == "file" and s.get("path"):
            try:
                n = int(pyogrio.read_info(base / s["path"], layer=s.get("layer"))["features"])
            except Exception:  # noqa: BLE001 - an unreadable file is the build's to report
                n = None
        if n is None:
            unknown.append(l.get("id"))
            continue
        feats += n
        if n > wml.TILE_LAYER_FEATURES:
            big.append(l.get("id"))
    entries = [e for e in rl.all_entries("all") if e.get("type") == "basemap-provider"]
    asked = spec.get("basemap") or {}
    basemap, hosts, vector_base = "the bundled background (works offline)", [], False
    for name in requested_providers(spec, cfg):
        mine = name == asked.get("provider")
        r = tr.resolve(name, entries, style=asked.get("style") if mine else None, audience=spec.get("audience") or "external", env=env,
                       terms_acknowledged=bool(asked.get("terms_acknowledged")), source=asked.get("source") if mine else None, base_dir=base,
                       time=asked.get("time") if mine else None)
        if r["ok"] and r["mode"] in ("tiles", "vector", "vector-file"):
            basemap = r.get("title") or name
            hosts = list(r.get("hosts") or [])
            vector_base = r["mode"] != "tiles"
            break
    ctl = cfg["controls"]
    import webmap_3d as w3
    s3 = w3.settings(spec)
    n_ch = len(((cfg["story"] or {}).get("chapters") or []))
    engine, why = choose_engine(cfg["engine"], features=feats, tiled=big, vector_base=vector_base, time_on=bool(cfg["time"]) and ctl["time_slider"],
                                compare_on=bool(cfg["compare"]) and ctl["compare"], three_d=bool(s3), story=bool(n_ch))
    per = 70 if (engine == "leaflet" or not big) else 40
    page_mb = (1.6 if engine == "leaflet" else 2.8) + feats * per / 1048576 + (0.4 if feats > 20000 else 0)
    if s3 and engine == "maplibre":                  # M15: terrain tiles (capped), profile charts; a print picture per story chapter
        page_mb += (min(s3["max_dem_mb"], 2.0) if s3["terrain"] else 0) + (0.8 if s3["links"] else 0)
    page_mb += 0.5 * n_ch
    return {"features": feats, "unknown_layers": unknown, "engine": engine, "engine_why": why, "basemap": basemap, "hosts": hosts,
            "network": ("none (everything is inside the page)" if not hosts else f"basemap tiles from {', '.join(hosts)} in the viewer's browser"),
            "page_mb": round(page_mb, 1), "three_d": bool(s3 and engine == "maplibre"), "story_chapters": n_ch}


def tile_records(base_defs: list[dict], entries: list[dict]) -> list[dict]:
    """Provenance records for the tile providers a page can call (the viewer's browser fetches them live)."""
    out = []
    for b in base_defs:
        if b["mode"] not in ("tiles", "vector", "vector-file"):
            continue
        e = next((x for x in entries if x.get("id") == b["entry"]), None) or {}
        src = e.get("source") or {}
        out.append({"layer_id": f"basemap:tiles:{b['id']}", "source_name": f"{src.get('publisher', '')}, {src.get('product', e.get('title', ''))}".strip(", "),
                    "source_url": src.get("url"), "retrieved_at": str(e.get("url_verified_on") or (e.get("maintenance") or {}).get("reviewed_on")),
                    "license": (e.get("license") or {}).get("class"), "attribution_text": (e.get("license") or {}).get("attribution_text"),
                    "crs_original": "EPSG:3857", "crs_used": "EPSG:3857",
                    "transformations": [f"{'raster' if b['mode'] == 'tiles' else 'vector'} tiles requested live from {b['host']} by the viewer's browser; nothing is stored in the page"
                                        if b.get("host") else "vector tiles from the person's own PMTiles file, " + ("served beside the page" if b.get("beside") else "embedded in the page")],
                    "row_count": 0,
                    "library_ref": e.get("id"), "library_rev": e.get("rev")})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", default=".")
    ap.add_argument("--brand")
    a = ap.parse_args(argv)
    import build_map as bm
    try:
        res = bm.build(a.spec, a.out, ["html"], brand=a.brand)
    except mp.PlanError as ex:
        print(f"webmap: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(res, indent=2, default=str))
    return gc.EXIT_LINT if res["blocked"] else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
