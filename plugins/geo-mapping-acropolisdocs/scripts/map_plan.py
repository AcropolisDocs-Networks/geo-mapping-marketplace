#!/usr/bin/env python3
"""Prepare a map: read the MapSpec, load and check every layer, choose the display CRS and extent, classify, and pick colors.

The result (a `Plan`) holds everything the renderer needs, so rendering is pure drawing: no data is read, no choice is made,
no color is computed there. Findings from the spec, data and CRS lints are collected here; a BLOCK stops the build.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import brandlib as bl  # noqa: E402
import classify as cf  # noqa: E402
import crs_engine as ce  # noqa: E402
import data_lint as dl  # noqa: E402
import geo_io  # noqa: E402
import gmap_common as gc  # noqa: E402
import layout as lo  # noqa: E402
import legend as lg  # noqa: E402
import map_look  # noqa: E402
import mapspec_lint as ml  # noqa: E402

PRESET_BBOX = {          # WGS84 lon/lat boxes for extent.preset
    "conus": (-125.0, 24.3, -66.7, 49.6), "us": (-171.0, 15.5, -65.0, 71.6), "us_states": (-171.0, 15.5, -65.0, 71.6),
    "alaska": (-170.0, 51.0, -129.0, 71.6), "hawaii": (-160.6, 18.8, -154.7, 22.4), "world": (-180.0, -58.0, 180.0, 84.0),
    "north_america": (-170.0, 7.0, -50.0, 72.0), "europe": (-25.0, 34.0, 45.0, 72.0),
}
BASEMAP_REFS = {"countries": "ref.boundary.world.country.ne110", "states": "ref.boundary.us.state.cb20m", "states5m": "ref.boundary.us.state.cb5m",
                "lakes": "ref.context.world.lakes.ne110", "admin1": "ref.boundary.world.state.ne50",
                "ocean": "ref.context.world.ocean.ne10", "lakes10": "ref.context.world.lakes.ne10",
                "admin1_loc": "ref.boundary.world.state.ne10-locator"}      # 1.5.0: the admin-1 locator anywhere (R3-LOC)
NE10, NE110 = "Natural Earth 1:10m", "Natural Earth 1:110m"   # ground resolutions (MAP-CART-024: land and water always share one)
FINE_GROUND_MAX_DENOMINATOR = 10_000_000  # finer than this the ground is the 10m ocean cut from the window, not the 110m countries (0.3.0)
CONTEXT_REFS = {"urban": "ref.context.world.urban.ne10", "lakes": "ref.context.world.lakes.ne10", "rivers": "ref.context.world.rivers.ne10",
                "roads": "ref.infrastructure.na.road.ne10"}                                  # the bundled context pack (M10, P0-2)
TIGER_ROADS = {"tiger": "ref.infrastructure.us.road.tiger-primary", "tiger-prisec": "ref.infrastructure.us.road.tiger-prisec"}   # 0.25.0: context.roads
TIGER_ROAD_TYPES = {"S1100": "Major Highway", "S1200": "Secondary Highway"}         # MTFCC: primary, secondary; the rest of a file are local roads
NAMES_REFS = {"places": "ref.gazetteer.world.place.ne10", "lakes": "ref.context.world.lakes.ne10", "rivers": "ref.context.world.rivers.ne10",
              "admin1": "ref.boundary.world.state.ne50", "states": "ref.boundary.us.state.cb20m"}  # toponymy (M10, P0-1)
DATA_ROLES = ("choropleth", "points", "lines", "polygons", "proportional", "heat", "hexbin", "grid", "raster")
AREA_ROLES = ("choropleth", "hexbin", "grid")       # a classed fill over areas: equal-area display, water-only context, background-coloured outlines
AGG_ROLES = ("hexbin", "heat", "grid")              # the layer's points are aggregated into cells before anything else looks at them (M11)
RASTER_ROLES = ("raster", "hillshade", "contour")   # read from a raster for the map's window after the extent is known (map_raster, M12)
CONTEXT_PRESETS = {"none": (), "roads": ("roads",), "urban": ("urban",), "water": ("lakes", "rivers"), "full": ("urban", "lakes", "rivers", "roads"),
                   "terrain": ()}                  # terrain: relief only (map_raster.terrain_wanted); full also brings relief on regional to local sheets
ROADS_MAX_DENOMINATOR = 5_000_000       # roads are drawn only on maps larger than 1:5,000,000; smaller than that they are a grey smear
CONTEXT_WINDOW_PAD_DEG = 3.0            # context layers are cut this far around the map before reprojection (the map's own window plus a margin)
NE_ZOOM0_DENOMINATOR = 591_657_550.0    # 1:591,657,550 is Web Mercator zoom 0 at the equator; Natural Earth's min_zoom is in that numbering

# ---------------------------------------------------------------------------------------------------------------- scale tiers
# What a map's scale can carry, by its denominator (the map is 1:denominator). Each row: up to this denominator (exclusive), the tier
# name, the largest Natural Earth scalerank a place may have (None: every place, subject to names.min_pop), the largest scalerank for a
# water name, and the context pack's ranks for urban areas, lakes, rivers and roads (None: all, -1: the kind is left out).
# Natural Earth's scaleranks are monotonic with its min_zoom, so a rank threshold is a zoom threshold; the paper reader sits closer than
# a screen reader, so each tier takes about one zoom more than the web page would at the same scale.
SCALE_TIERS = (
    #  up to           tier           places  water  urban  lakes  rivers  roads
    (500_000,         "local",        None,   None,  None,  None,  None,   None),
    (2_000_000,       "metro",        None,   9,     None,  8,     8,      7),
    (5_000_000,       "regional",     7,      7,     6,     7,     7,      5),
    (10_000_000,      "regional",     6,      6,     5,     6,     6,      -1),
    (50_000_000,      "continental",  4,      4,     3,     4,     4,      -1),
    (float("inf"),    "world",        2,      2,     -1,    2,     2,      -1),
)


def scale_tier(denominator: float) -> dict:
    """The row of SCALE_TIERS for a scale denominator, as a dict: {tier, places, water, urban, lakes, rivers, roads, denominator}."""
    den = float(denominator or 0) or NE_ZOOM0_DENOMINATOR
    for upto, tier, places, water, urban, lakes, rivers, roads in SCALE_TIERS:
        if den < upto:
            return {"tier": tier, "places": places, "water": water, "urban": urban, "lakes": lakes, "rivers": rivers, "roads": roads, "denominator": den}
    raise AssertionError("SCALE_TIERS ends with infinity")


def zoom_for_denominator(denominator: float) -> float:
    """The Web Mercator zoom (equator) whose tiles are at this scale, so a print scale can be read against Natural Earth's min_zoom."""
    import math
    den = max(float(denominator or 0), 1.0)
    return math.log2(NE_ZOOM0_DENOMINATOR / den)


def denominator_for_zoom(zoom: float, lat: float = 0.0) -> float:
    """The scale denominator of a 96 dpi screen at a Web Mercator zoom and latitude (the web page's equivalent of the print scale)."""
    import math
    return NE_ZOOM0_DENOMINATOR * math.cos(math.radians(max(min(lat, 85.0), -85.0))) / (2 ** float(zoom))


def names_config(spec: dict) -> dict:
    """The `names` block with its defaults, `elements.names.on` honoured as the toggle (every element is individually toggleable)."""
    n = dict(spec.get("names") or {})
    el = ((spec.get("elements") or {}).get("names") or {})
    on = n.get("on", True)
    if "on" in el:
        on = el["on"]
    return {"on": on is not False, "places": str(n.get("places") or "auto"), "water": n.get("water", True) is not False,
            "admin": n.get("admin", True) is not False, "max": n.get("max"), "min_pop": int(n.get("min_pop") or 0),
            "exclude": {str(x).strip().lower() for x in (n.get("exclude") or [])},
            "language": str(n.get("language") or "auto"), "second_line": str(n.get("second_line") or "none")}


PLACE_NAMES = "assets/basemaps/ne_10m_place_names.parquet"     # 1.5.0: the 26 other-language names of the 10m places, by ne_id
LATIN_GREEK_CYRILLIC = {"Latn", "Grek", "Cyrl"}


def place_names_table():
    """The other-language names of the bundled places (name_ar ... name_zht), or None when the file is not in this edition."""
    p = gc.plugin_root() / PLACE_NAMES
    if not p.is_file():
        return None
    import pandas as pd
    return pd.read_parquet(p)


def upper_name(text: str) -> str:
    """A name in capitals as its script writes them: Greek capitals carry no accent (ΑΤΤΙΚΗ, not ΑΤΤΙΚΉ)."""
    import unicodedata
    u = str(text).upper()
    if any("\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff" for ch in u):
        u = unicodedata.normalize("NFC", "".join(ch for ch in unicodedata.normalize("NFD", u) if unicodedata.combining(ch) == 0 or ch == "\u0308"))
    return u


def admin_unit_name(row, cfg: dict, col: str = "name") -> str:
    """1.12.0: the name of a first-level unit (a state, a region) by `names.language`, as the place names are chosen: `en` the English name
    (Attica); a language code that language's name; `auto` the unit's own name in the country's language where that is written in Greek or
    Cyrillic (Αττική), the local spelling where it is Latin, English otherwise; `local` the country's language always. A name this plugin
    has no font or shaper for, or a table without the language columns (the US states), gives the table's own name."""
    import geo_locale as gl
    import script_text as st

    def val(k):
        v = row.get(k) if hasattr(row, "get") else None
        return str(v) if isinstance(v, str) and v.strip() and v.strip().lower() not in ("nan", "none") else None

    own = val(col) or ""
    lang_cfg = str((cfg or {}).get("language") or "auto")
    en = val("name_en") or own
    if lang_cfg == "en":
        return en
    iso = val("iso_a2")
    clang = (gl.country(iso) or {}).get("lang") if iso else None
    want = clang if lang_cfg in ("auto", "local") else lang_cfg.lower().split("-")[0]
    local = val(f"name_{want}") if want else None
    script = st.LANG_SCRIPT.get(want or "", "Latn")
    if lang_cfg in ("auto", "local") and script == "Latn":
        return val("name_local") or own or en                 # the unit's own spelling (Vilniaus, Bayern)
    if not local:
        return en if lang_cfg not in ("auto", "local") or script != "Latn" else own
    if lang_cfg == "auto" and script not in LATIN_GREEK_CYRILLIC:
        return en
    st.use_lang(want)
    _k, miss = st.plan_for(local)
    return en if miss else local


def choose_place_names(g, cfg: dict) -> tuple[list[str], list[str | None], set]:
    """1.5.0 (R3-SCRIPT): (the name to draw, the second line or None, the scripts left out) for each place row, by `names.language`:
    auto: the local name where the place's country writes in Latin, Greek or Cyrillic (Athína is Αθήνα, Munich is München), English otherwise;
    local: the local name always; en: English; a language code: that language's name. A name in a script with no font or shaper here falls back
    to English (MAP-LOC-003). `names.second_line: local | en` adds the other form under it."""
    import geo_locale as gl
    import script_text as st
    lang_cfg = cfg.get("language", "auto")
    second = cfg.get("second_line", "none")
    names = list(g["name"].astype(str))
    if lang_cfg in ("en",) and second == "none":
        return names, [None] * len(names), set()
    tbl = place_names_table()
    if tbl is None or "ne_id" not in g.columns:
        return names, [None] * len(names), set()
    m = g[["ne_id"]].merge(tbl, on="ne_id", how="left")
    isos = list(g["iso_a2"].astype(str)) if "iso_a2" in g.columns else [""] * len(g)
    out, alt, left = [], [], set()
    for i, en in enumerate(names):
        en_name = m["name_en"].iloc[i] if "name_en" in m.columns and isinstance(m["name_en"].iloc[i], str) else en
        clang = (gl.country(isos[i]) or {}).get("lang")
        want = clang if lang_cfg in ("auto", "local") else (None if lang_cfg == "en" else lang_cfg.lower().replace("-hant", "t").replace("-tw", "t").split("-")[0])
        local = m[f"name_{want}"].iloc[i] if want and f"name_{want}" in m.columns and isinstance(m[f"name_{want}"].iloc[i], str) else None
        script = st.LANG_SCRIPT.get(want or "", "Latn")
        if lang_cfg == "en" or not local:
            first, other = en_name, local
        elif lang_cfg == "auto" and script not in LATIN_GREEK_CYRILLIC:
            first, other = en_name, local
        else:
            first, other = local, en_name
        st.use_lang(want)
        k, miss = st.plan_for(first)
        if miss:
            left |= set(miss)
            first, other = en_name, None
        if other is not None and other != first and second in ("local", "en"):
            k2, miss2 = st.plan_for(other)
            if miss2:
                left |= set(miss2)
                other = None
        st.use_lang(None)
        out.append(first)
        alt.append(other if second in ("local", "en") and other and other != first else None)
    return out, alt, left


def name_excluded(name, patterns) -> bool:
    """`names.exclude` (0.23.0): a whole name, or a pattern with * and ? (`Fort *`, `* Air Force Base`), matched without regard to case."""
    import fnmatch
    n = str(name).strip().lower()
    return any(fnmatch.fnmatchcase(n, p) if any(c in p for c in "*?[") else n == p for p in patterns)


def context_config(spec: dict) -> dict:
    """The `context` block with its defaults, `elements.context.on` honoured as the toggle."""
    c = dict(spec.get("context") or {})
    el = ((spec.get("elements") or {}).get("context") or {})
    on = c.get("on", True)
    if "on" in el:
        on = el["on"]
    return {"on": on is not False, "preset": str(c.get("preset") or "auto"), "roads": c.get("roads"), "urban": c.get("urban"), "water": c.get("water"),
            "opacity": float(c.get("opacity", 1.0)), "terrain": c.get("terrain")}


def _context_roads_tiger(plan, cfg: dict):
    """0.25.0 (TODO M10): the Census TIGER roads for `context.roads: tiger | tiger-prisec`, read from the library cache, with a `type` column in
    the Natural Earth words (S1100 primary as Major Highway, S1200 secondary as Secondary Highway) and `scalerank` 0, so the drawing below is the
    same. (None, None) when the file is not cached: the Natural Earth roads are drawn and the notes say how to fetch the TIGER file."""
    rid = TIGER_ROADS[cfg["roads"]]
    try:
        gdf, e = load_ref_layer(rid)
    except PlanError as ex:
        plan.notes.append(f"context pack: TIGER roads not drawn ({str(ex)[:160]}); the Natural Earth roads stand in")
        return None, None
    col = next((c for c in ("MTFCC", "mtfcc") if c in gdf.columns), None)
    if col is None:
        plan.notes.append(f"context pack: {rid} has no MTFCC column; the Natural Earth roads stand in")
        return None, None
    g = gdf[gdf[col].isin(list(TIGER_ROAD_TYPES))].copy()
    g["type"] = g[col].map(TIGER_ROAD_TYPES)
    g["scalerank"] = 0
    plan.notes.append(f"context pack: roads from {rid} ({len(g):,} primary and secondary road lines in the file)")
    return g, e


def context_preset_auto(roles, has_tiles: bool) -> str:
    """The context preset a map gets when it names none: `none` under a tile composite (the tiles carry their own roads and water), `water`
    when a choropleth, polygon or raster layer is present (roads under fills make them hard to read), `full` for point, line and proportional maps.
    An auto preset never brings relief (terrain is off by default; map_raster.terrain_wanted)."""
    if has_tiles:
        return "none"
    data = [r for r in roles if r in DATA_ROLES]
    if any(r in AREA_ROLES or r in ("polygons", "raster") for r in data):
        return "water"
    return "full"


def context_kinds(cfg: dict, roles, has_tiles: bool) -> tuple[str, list[str]]:
    """(preset used, kinds drawn) for a context config: the preset (auto resolved), then the roads/urban/water switches on top of it."""
    if not cfg["on"]:
        return "none", []
    preset = cfg["preset"]
    if preset == "auto":
        preset = context_preset_auto(roles, has_tiles)
    kinds = list(CONTEXT_PRESETS[preset])
    for key, members in (("roads", ("roads",)), ("urban", ("urban",)), ("water", ("lakes", "rivers"))):
        v = cfg.get(key)
        if v is True or (key == "roads" and v in TIGER_ROADS):
            kinds += [m for m in members if m not in kinds]
        elif v is False:
            kinds = [k for k in kinds if k not in members]
    return preset, [k for k in ("urban", "lakes", "rivers", "roads") if k in kinds]
TOKEN_ALIASES = {"accent": "emphasis", "emphasis": "emphasis", "land": "land", "water": "water", "frame": "frame", "label": "label",
                 "background": "background", "no_data": "no_data", "legend_bg": "legend_bg", "legend_frame": "legend_frame"}


class PlanError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME):
        super().__init__(message)
        self.code = code


def crs_label(crs) -> str | None:
    """A short, readable name for a CRS: the authority code when there is one ('EPSG:4269'), otherwise its name. Never the full definition."""
    if crs is None:
        return None
    try:
        if not hasattr(crs, "to_authority"):
            from pyproj import CRS
            crs = CRS.from_user_input(crs)
        a = crs.to_authority(min_confidence=70)
        if a:
            return f"{a[0]}:{a[1]}"
    except Exception:  # noqa: BLE001
        pass
    return getattr(crs, "name", None) or "an unnamed coordinate system"


@dataclass
class LayerPlan:
    id: str
    spec: dict
    role: str
    gdf: object
    kind: str                                   # polygon | line | point
    title: str
    value_field: str | None = None
    cls: object = None
    colors: list = field(default_factory=list)
    palette_kind: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_w_mm: float = 0.15
    fill_opacity: float = 1.0
    symbol: dict = field(default_factory=dict)
    confidentiality: str = "public"
    provenance: dict = field(default_factory=dict)
    entry: dict | None = None
    notes: list = field(default_factory=list)
    legend_block: dict | None = None
    assumed_crs: bool = False
    crs_original: str | None = None
    rows_loaded: int = 0
    src_file: str | None = None
    src_sha256: str | None = None
    bbox: list | None = None
    transformations: list = field(default_factory=list)
    capture: dict | None = None                 # the capture record of a service or user-run layer (M4b)
    class_shapes: list | None = None            # one symbol shape per category (status maps: shape plus color, never color alone)
    privacy: dict | None = None                 # the coordinate reduction applied to a confidential point layer (M6)
    join_report: dict | None = None             # what a table join matched (M6)
    wedge_info: dict | None = None              # sector wedges drawn from sites (M6)
    stroke_opacity: float = 1.0                 # lines of the context pack can be faded (context.opacity)
    context_kind: str | None = None             # urban | lakes | rivers | roads for a layer the context pack added (M10)
    draw: bool = True                           # False: a helper layer, loaded for other layers' ops and never drawn (M11)
    ops_provenance: list = field(default_factory=list)   # one record per `ops:` step (geo_ops.Ctx.provenance) (M11)
    aggregation: dict | None = None             # how a hexbin, heat or grid layer's points were binned into its cells (M11)
    used_by: list = field(default_factory=list) # the ids of the layers whose ops name this one (M11)
    raster: dict | None = None                  # a raster, hillshade or contour layer: its source, then the picture placed by box and its record (M12)
    group_opacity: float = 1.0                  # 0.17.0: the whole layer drawn as one group at this opacity (overlapping wedges do not darken)
    casing: dict | None = None                  # 0.17.0: {color, extra_mm, opacity}: a band of the land colour under a data line layer
    highlight: object = None                    # 0.22.0: a boolean per feature: the `highlight:` subset (map_render.highlight_groups)
    highlight_rest: dict | None = None          # 1.13.0: how the rest is drawn: {dim, opacity} (highlight.rest, highlight.rest_opacity)
    edge_in_fill: bool = False                  # 0.22.0: polygons drawn see-through with the edge in each fill colour at full strength
    outline: bool = False                       # 1.2.0: a polygons layer with no fill, drawn as its edge only
    stroke_named: bool = False                  # 1.2.0: the spec names the edge colour, so it is drawn at full strength
    class_opacity: list | None = None           # 0.20.0: one opacity per class (value by alpha on a hexbin): low classes let the ground show
    emphasis: str = "primary"                   # 1.13.0: primary | supporting | background (map_emphasis): how strongly the layer is drawn
    nodata_opacity: float = 1.0                 # 1.13.0: the No data class drawn see-through (map_emphasis.NODATA_OPACITY) so it recedes


@dataclass
class Plan:
    spec: dict
    spec_path: Path
    manifest: dict
    brand: str
    brand_name: str
    pack: dict
    tokens: dict
    mode: str
    tpl: dict
    tpl_name: str
    crs: str
    crs_info: dict
    extent: tuple
    layers: list
    basemap: list
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    palette_reports: dict = field(default_factory=dict)
    helpers: list = field(default_factory=list)      # `draw: false` layers: loaded for ops, with provenance, never drawn (M11)
    look: str = "classic"                            # 0.17.0: style.look (map_look); prepare() sets it from the spec (0.22.0: the look drawn, auto resolved)
    look_asked: str = "classic"                      # 0.22.0: what the MapSpec asked for (auto, a named look, classic)
    focus_sheet: object = None                       # 0.18.0: the study area on the sheet (map_focus), set while the layers are drawn
    style: object = None                             # 1.4.0 (R2-PACK): the style pack, fixed for the whole build (map_style.Style)
    treatments: dict = field(default_factory=dict)  # 1.4.0 (R2-TREAT): the pack's treatments with the MapSpec's style.treatments over them
    vector_ground: dict | None = None                # 1.10.0: roads, rivers and built-up land read from the person's own vector tile file


THEME_MODES = {"light": "print", "dark": "screen"}     # the MapSpec says light or dark; the brand pack calls them print and screen


def theme_mode(spec: dict) -> str:
    """The token mode for a spec: light (the default) is print, dark is screen."""
    return THEME_MODES.get(str(spec.get("theme") or "light"), "print")


# ---------------------------------------------------------------------------------------------------------------- colors
def resolve_color(value, tokens: dict, what: str = "color") -> str:
    """'@brand.accent' and friends become a declared token; a literal hex must itself be a declared token (palette guard)."""
    if value is None:
        raise PlanError(f"{what}: no color given")
    v = str(value)
    if v.startswith("@brand."):
        key = v[len("@brand."):]
        if key in TOKEN_ALIASES:
            return tokens[TOKEN_ALIASES[key]]
        if key.startswith("status."):
            return tokens["status"][key.split(".", 1)[1]]
        for fam, ramp in (("categorical", tokens.get("categorical")), ("sequential", tokens.get("_brand_sequential_ramp") or tokens.get("sequential_ramp")),
                          ("neutral", tokens.get("neutral_ramp"))):          # neutral: planned features on an atlas-by-time page (M15)
            if key.startswith(fam + "."):
                # 1.2.0 (Day 10 issue 1): an index past the ramp is a plain message, not an IndexError in the build
                n = len(ramp or [])
                try:
                    i = int(key.split(".", 1)[1])
                except ValueError:
                    raise PlanError(f"{what}: '{v}' needs a number after {fam}. (@brand.{fam}.0 to .{max(n - 1, 0)})", gc.EXIT_USAGE)
                if not 0 <= i < n:
                    raise PlanError(f"{what}: '{v}' is not in the brand's {fam} ramp, which has {n} steps (@brand.{fam}.0 to .{max(n - 1, 0)})", gc.EXIT_USAGE)
                return ramp[i]
        raise PlanError(f"{what}: unknown brand color '{v}' (try @brand.accent, .land, .water, .status.success, .categorical.N)")
    allowed = {c.upper() for c in _all_token_colors(tokens)}
    if v.upper() in allowed:
        return v
    raise PlanError(f"{what}: '{v}' is not one of the brand's colors; use an @brand.* token", gc.EXIT_LINT)


def _all_token_colors(tokens: dict):
    stack = list(tokens.values())
    while stack:
        x = stack.pop()
        if isinstance(x, str) and x.startswith("#"):
            yield x
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            stack.extend(x.values())


# ---------------------------------------------------------------------------------------------------------------- loading
_LIBRARY_INDEX: dict = {}


def _library_index() -> dict:
    """The library's entries by id (the shipped entry wins over a local one with the same id, as the scan always did), read once per process
    and again only when an entry file is added, removed or changed. A map build asks for ten or more entries (the basemap, the context
    pack, the names), and each ask used to parse every entry file (M16)."""
    import ref_library as rl
    dirs = [rl.shipped_lib() / "entries", rl.local_lib() / "entries"]
    sig = tuple((str(p), st.st_mtime_ns, st.st_size) for d in dirs if d.is_dir() for p in sorted(d.rglob("*.yaml")) for st in [p.stat()])
    if _LIBRARY_INDEX.get("sig") != sig:
        by_id: dict = {}
        for e in rl.all_entries("all"):
            by_id.setdefault(e.get("id"), e)
        _LIBRARY_INDEX.clear()
        _LIBRARY_INDEX.update(sig=sig, by_id=by_id)
    return _LIBRARY_INDEX["by_id"]


def library_entry(entry_id: str) -> dict:
    import copy
    e = _library_index().get(entry_id)
    if e is not None:
        return copy.deepcopy(e)
    raise PlanError(f"MAP-REF-001: the reference library has no entry '{entry_id}'; run geo-find-data or ref-library list", gc.EXIT_USAGE)


# ---------------------------------------------------------------------------------------------------------------- one plan for many frames
_SESSION: dict | None = None


@contextlib.contextmanager
def frame_session():
    """1.11.0: maps built inside this block share what does not change from one to the next: a library layer is read from disk once
    (load_ref_layer), and the ground of a frame (load_basemap: the land cut from the ocean, the detailed ground, the prepared tiles) is
    worked out once per projection, window and basemap settings. An animation's frames and an atlas's pages differ in their data only, so
    every frame after the first skips that work. Nothing is kept after the block."""
    global _SESSION
    outer = _SESSION
    _SESSION = outer if outer is not None else {"ref": {}, "ground": {}, "hits": {"ref": 0, "ground": 0}}
    try:
        yield _SESSION
    finally:
        _SESSION = outer


def load_ref_layer(entry_id: str, variant: str | None = None):
    if _SESSION is None:
        return _load_ref_layer(entry_id, variant)
    key = (entry_id, variant)
    if key not in _SESSION["ref"]:
        _SESSION["ref"][key] = _load_ref_layer(entry_id, variant)
    else:
        _SESSION["hits"]["ref"] += 1
    gdf, e = _SESSION["ref"][key]
    return gdf.copy(), e


def _load_ref_layer(entry_id: str, variant: str | None = None):
    e = library_entry(entry_id)
    st = e.get("status")
    if st in ("blocked", "retired", "draft"):
        why = (e.get("blocked") or {}).get("why") if st == "blocked" else None
        raise PlanError(f"MAP-REF-002: library entry '{entry_id}' is {st}" + (f": {why}" if why else "; it carries no verified payload"), gc.EXIT_REFUSED)
    d = e.get("delivery") or {}
    mode = d.get("mode")
    if mode == "user-supplied":
        raise PlanError(f"library entry '{entry_id}' is user-supplied (licensed): supply your own copy as a file layer; the library holds no data for it", gc.EXIT_REFUSED)
    if mode == "service":
        import map_sources
        raise PlanError(map_sources.load_refusal(e), gc.EXIT_REFUSED)
    if mode == "bundled":
        path = (gc.plugin_root() / d["path"]) if e["_scope"] == "shipped" else (gc.workdir() / d["path"])
    elif mode == "fetch":
        import ref_fetch
        path = ref_fetch.cached_path(e, variant)
        if path is None:
            raise PlanError(f"library entry '{entry_id}' is fetched on demand and is not in the cache yet; run: ref_fetch.py fetch {entry_id}" + (f" --variant {variant}" if variant else ""), gc.EXIT_REFUSED)
    else:
        raise PlanError(f"library entry '{entry_id}' cannot be loaded as a layer (delivery: {mode})", gc.EXIT_REFUSED)
    if not path.is_file():
        raise PlanError(f"library entry '{entry_id}' points at {path}, which is missing", gc.EXIT_RUNTIME)
    return geo_io.read(path).gdf, e


def load_source(layer: dict, base: Path, window=None):
    """`window` (a map build passes it) gives the lon/lat box a library service layer is fetched for (map_sources.window)."""
    src = layer["source"]
    t = src.get("type")
    if t in ("ref", "geocode"):
        import map_sources                    # M13: a library entry served by a data service, and a table geocoded into points
        if t == "geocode":
            return map_sources.load_geocode(layer, base)
        if map_sources.service_entry(src.get("id")) is not None:
            return map_sources.load_service_layer(layer, base, window)
    if t == "ref":
        gdf, e = load_ref_layer(src["id"], src.get("variant"))
        return gdf, e, False, [], None
    if t == "file":
        p = Path(src["path"])
        p = p if p.is_absolute() else (base / p)
        if not p.is_file():
            raise PlanError(f"layer '{layer['id']}': file not found: {src['path']}", gc.EXIT_USAGE)
        try:
            r = geo_io.read(p, layer=src.get("layer"), crs_assume=src.get("crs"), lat_col=src.get("y"), lon_col=src.get("x"),
                            wkt_col=src.get("wkt"), bbox=src.get("bbox"))       # bbox: a footprint file read for the map's window only (M15)
        except geo_io.AmbiguousColumns as ex:
            raise PlanError(f"layer '{layer['id']}': {ex}", gc.EXIT_USAGE)
        except geo_io.GeoIOError as ex:
            raise PlanError(f"layer '{layer['id']}': {ex}", gc.EXIT_RUNTIME)
        # 1.1.3: provenance.library_entry names the library source the file came from (weather fetched from OpenWeather and joined to sites):
        # the layer then carries that source's credit, licence and logo
        le = (layer.get("provenance") or {}).get("library_entry") if isinstance(layer.get("provenance"), dict) else None
        ent = library_entry(str(le)) if le else None
        if ent is not None and ent.get("status") == "blocked":
            raise PlanError(f"layer '{layer['id']}': library entry '{le}' is blocked by its terms; its data cannot be drawn", gc.EXIT_REFUSED)
        return r.gdf, ent, r.crs_assumed, [n.as_dict() for n in r.notes], None
    if t in ("service", "user-run"):
        return load_capture(layer, base)
    raise PlanError(f"layer '{layer['id']}': source type '{t}' is not a source this plugin knows (file, ref, service, user-run)", gc.EXIT_USAGE)


# 0.15.0 MAP-LIC-013: a layer the person supplied that looks like people's home addresses (a person's name beside an address, or a column
# that says it is a home address). Library and service layers are published data and are not read.
PERSON_COL = re.compile(r"^(first|last|given|family|full|sur|middle)_?name$|^(resident|customer|subscriber|patient|tenant|homeowner|occupant|"
                        r"person|account_?holder)(_?name)?$|^(dob|date_?of_?birth|birth_?date|ssn|social_?security(_?number)?)$", re.I)
ADDRESS_COL = re.compile(r"addr|street|house_?(no|num|number)|apt|apartment|unit_?no|zip|postcode|postal", re.I)
HOME_COL = re.compile(r"^(home|residential|residence|mailing|personal)_?(address|addr|street)$", re.I)


def home_address_findings(layers, audience: str) -> list[dict]:
    out = []
    for l in layers:
        src = (l.spec.get("source") or {}) if isinstance(l.spec.get("source"), dict) else {}
        if src.get("type") not in ("file", "user-run", "geocode") or l.entry:
            continue
        cols = [str(c) for c in l.gdf.columns if c != "geometry"]
        home = [c for c in cols if HOME_COL.match(c)]
        person = [c for c in cols if PERSON_COL.match(c)]
        addr = [c for c in cols if ADDRESS_COL.search(c)]
        if not (home or (person and addr)):
            continue
        why = (f"column(s) {', '.join(home)}" if home else f"a person's details ({', '.join(person[:3])}) beside an address ({', '.join(addr[:3])})")
        reviewed = str(l.spec.get("home_addresses_reviewed") or "").strip()
        if len(reviewed) >= 12:
            out.append({"id": "MAP-LIC-013", "severity": "INFO", "message": f"layer '{l.id}' holds {why}; reviewed: {reviewed[:160]}", "detail": None, "layer": l.id})
            continue
        sev = "BLOCK" if audience in ("external", "regulatory", "any") else "WARN"
        out.append({"id": "MAP-LIC-013", "severity": sev, "message": f"layer '{l.id}' looks like private individuals' home addresses ({why}). A map never "
                    "shows where people live: drop those columns and points, aggregate them (hexbin, counts per area), or, if they are business sites, "
                    "say so in the layer's home_addresses_reviewed (a reason of a dozen characters or more)", "detail": {"columns": home or person + addr}, "layer": l.id})
    return out


def find_capture(ref: str, base: Path) -> Path:
    p = Path(ref)
    cands = [p] if p.is_absolute() else [base / p, Path.cwd() / p]
    cands.append(gc.workdir() / "captures" / ref)
    for c in cands:
        c = c / "capture.json" if c.is_dir() else c
        if c.is_file():
            return c
    raise PlanError(f"capture '{ref}' was not found (looked beside the MapSpec and in {gc.workdir() / 'captures'}); run service_query.py fetch or request_kit.py ingest first", gc.EXIT_USAGE)


def load_capture(layer: dict, base: Path):
    """A service or user-run layer is drawn from a capture, never from the network: a build is offline and repeatable (spec 6.4, 6.5)."""
    import hashlib
    import json
    src = layer["source"]
    t = src["type"]
    lid = layer["id"]
    if src.get("mode") == "live":
        raise PlanError(f"layer '{lid}': a live service layer is drawn only in a web page (map_web.py, webmap-create), by the viewer's browser; a printed map draws a capture (MAP-SVC-014): capture the data and draw the capture", gc.EXIT_REFUSED)
    ref = src.get("capture")
    if not ref:
        how = "service_query.py fetch --profile P --layer L" if t == "service" else "request_kit.py build, run the kit, then request_kit.py ingest"
        raise PlanError(f"layer '{lid}': a {t} layer is drawn from a capture, and the build never fetches. Capture the data first ({how}) and give source: {{type: {t}, capture: <capture id or path>}}", gc.EXIT_REFUSED)
    cp = find_capture(str(ref), base)
    try:
        rec = json.loads(cp.read_text())
    except (OSError, ValueError) as ex:
        raise PlanError(f"layer '{lid}': the capture record {cp} cannot be read: {ex}", gc.EXIT_RUNTIME)
    want = "mcp" if t == "service" else "user-run"
    if t == "user-run" and rec.get("route") == "device-run":
        want = "device-run"                                      # an approved run on the person's computer is drawn like data the user supplied
    if rec.get("route") != want:
        raise PlanError(f"layer '{lid}': source type '{t}' needs a capture made by route '{want}', but this one was made by '{rec.get('route')}'"
                        + (" (data from a request kit is a user-run source)" if t == "service" and rec.get("route") == "user-run" else ""), gc.EXIT_USAGE)
    res = rec.get("result") or {}
    if not res.get("path"):
        raise PlanError(f"layer '{lid}': capture {rec.get('id')} holds no data (it received {rec.get('received')} features)", gc.EXIT_USAGE)
    rp = Path(res["path"])
    if not rp.is_file():
        rp = cp.parent / rp.name
    if not rp.is_file():
        raise PlanError(f"layer '{lid}': the data file of capture {rec.get('id')} is missing: {res['path']}", gc.EXIT_RUNTIME)
    if res.get("sha256") and hashlib.sha256(rp.read_bytes()).hexdigest() != res["sha256"]:
        raise PlanError(f"MAP-SVC-008: layer '{lid}': the data file changed after the capture was recorded (checksum differs); capture it again", gc.EXIT_LINT)
    try:
        r = geo_io.read(rp)
    except geo_io.GeoIOError as ex:
        raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_RUNTIME)
    rec["_record_path"] = str(cp)
    rec["_data_path"] = str(rp)
    return r.gdf, None, False, [n.as_dict() for n in r.notes], rec


def unpack_collections(gdf) -> tuple[object, int]:
    """A GeometryCollection row (what repairing a polygon often returns: polygons plus stray lines or points) becomes its polygons, else its lines,
    else its points, so the layer is not read as empty. Returns (gdf, rows changed)."""
    import shapely
    from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon
    gc_rows = gdf.geometry.geom_type == "GeometryCollection"
    if not gc_rows.any():
        return gdf, 0
    def best(g):
        parts = list(shapely.get_parts(shapely.get_parts(g)))
        flat = [q for p in parts for q in (getattr(p, "geoms", None) or [p])]
        for types, cls in ((("Polygon",), MultiPolygon), (("LineString", "LinearRing"), MultiLineString), (("Point",), MultiPoint)):
            got = [p for p in flat if p.geom_type in types and not p.is_empty]
            if got:
                return cls(got) if cls is not MultiLineString else MultiLineString([list(p.coords) for p in got])
        return g
    gdf = gdf.copy()
    gdf.loc[gc_rows, gdf.geometry.name] = [best(g) for g in gdf.geometry[gc_rows]]
    return gdf, int(gc_rows.sum())


def geom_kind(gdf) -> str:
    gt = gdf.geometry[~gdf.geometry.isna()].geom_type
    polys = gt.isin(["Polygon", "MultiPolygon"]).sum()
    lines = gt.isin(["LineString", "MultiLineString"]).sum()
    pts = gt.isin(["Point", "MultiPoint"]).sum()
    best = max((polys, "polygon"), (lines, "line"), (pts, "point"))
    return best[1]


def keep_kind(gdf, kind: str):
    want = {"polygon": ["Polygon", "MultiPolygon"], "line": ["LineString", "MultiLineString"], "point": ["Point", "MultiPoint"]}[kind]
    keep = gdf.geometry.geom_type.isin(want) & ~gdf.geometry.is_empty & ~gdf.geometry.isna()
    return gdf[keep].copy(), int((~keep).sum())


# ---------------------------------------------------------------------------------------------------------------- extent
def _bounds_of(layers: list) -> tuple:
    bs = [l.gdf.total_bounds for l in layers if len(l.gdf)]
    if not bs:
        raise PlanError("there is no data to put on the map", gc.EXIT_USAGE)
    a = np.array(bs)
    return (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 2].max()), float(a[:, 3].max()))


def place_bbox(name: str):
    """A named state, country or admin-1 area from the bundled layers, as a WGS84 bbox."""
    n = name.strip().lower()
    for ref, cols in (("ref.boundary.us.state.cb20m", ("NAME", "STUSPS")), ("ref.boundary.world.country.ne110", ("NAME", "NAME_LONG", "ISO_A2", "ISO_A3")),
                      ("ref.boundary.world.state.ne50", ("name", "NAME"))):
        gdf, _ = load_ref_layer(ref)
        for c in cols:
            if c in gdf.columns:
                hit = gdf[gdf[c].astype(str).str.lower() == n]
                if len(hit):
                    g = hit.to_crs("EPSG:4326") if hit.crs is not None else hit
                    return tuple(float(v) for v in g.total_bounds)
    raise PlanError(f"extent.place: '{name}' is not a state or country in the bundled layers; give a bbox instead", gc.EXIT_USAGE)


def project_bbox(bbox, crs: str) -> tuple:
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs = np.linspace(bbox[0], bbox[2], 12)
    ys = np.linspace(bbox[1], bbox[3], 12)
    X, Y = np.meshgrid(xs, ys)
    px, py = t.transform(X.ravel(), Y.ravel())
    ok = np.isfinite(px) & np.isfinite(py)
    return (float(px[ok].min()), float(py[ok].min()), float(px[ok].max()), float(py[ok].max()))


def source_file_hash(layer_spec: dict, entry: dict | None, base: Path) -> tuple[str | None, str | None]:
    """(file name, SHA-256) of the data file a layer was read from; a library layer uses the entry's recorded payload."""
    import hashlib
    src = layer_spec["source"]
    path = None
    if src.get("type") == "file":
        p = Path(src["path"])
        path = p if p.is_absolute() else base / p
    elif src.get("type") == "ref" and entry:
        d = entry.get("delivery") or {}
        if d.get("mode") == "bundled":
            path = (gc.plugin_root() / d["path"]) if entry["_scope"] == "shipped" else (gc.workdir() / d["path"])
        elif d.get("mode") == "fetch":
            import ref_fetch
            path = ref_fetch.cached_path(entry, src.get("variant"))
    if path is None or not path.is_file():
        return None, None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return path.name, h.hexdigest()


def bbox_wgs84(gdf) -> list | None:
    try:
        if gdf.crs is None or not len(gdf):
            return None
        b = gdf.to_crs("EPSG:4326").total_bounds if not gdf.crs.equals("EPSG:4326") else gdf.total_bounds
        return [round(float(v), 5) for v in b]
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------------------------- after loading (M6)
def join_numeric(layer: dict) -> list[str]:
    """The table columns a join reads as numbers whatever their names look like: the layer's value_field and the classification's denominator
    (a value column named like a code, `zip_rate` or `fips_score`, must still be a number), plus whatever `join.numeric` names (M10)."""
    c = layer.get("classification") or {}
    want = [layer.get("value_field"), c.get("denominator"), layer.get("denominator")]
    return [str(x) for x in want if x]


def repair_layer(layer: dict, gdf):
    """`repair: true` on a layer: geo_convert.repair (the one repair path, shared with geo-convert --repair) before anything else looks at the
    geometry. Returns (gdf, note). A repair that moves more than 1 percent of the area is a data problem, not a fix: MAP-DATA-002 blocks (M10)."""
    import geo_convert as gcv
    fixed, note = gcv.repair(gdf)
    detail = note.detail if isinstance(note.detail, dict) else {}
    if detail.get("block"):
        raise PlanError(f"MAP-DATA-002 layer '{layer['id']}': {note.msg} (area change {detail.get('area_change', 0.0) * 100:.3f} percent, "
                        f"over the {dl.REPAIR_AREA_BLOCK * 100:g} percent limit); look at the source geometry rather than drawing the repaired shape", gc.EXIT_LINT)
    return fixed, note


def geo_ops_fixed_ids(ops) -> set:
    """The MAP-DATA ids the layer's ops are the fix for (geo_ops.FIXES): dropped from the input lint, since the op's finding replaces them."""
    if not ops:
        return set()
    import geo_ops
    return geo_ops.fixed_ids(ops)


def load_order(layers: list) -> list[dict]:
    """The spec's layers in the order they must be loaded: a layer named by another layer's ops (`layer:`, `clip_layer:`) loads first; otherwise
    the spec's own order is kept. A cycle is a PlanError (the MapSpec lint reports it as MAP-SPEC-007 before this runs)."""
    by_id = {ly["id"]: ly for ly in layers}
    refs = {ly["id"]: [r for r in ml.op_layer_refs(ly) if r in by_id and r != ly["id"]] for ly in layers}
    cycle = ml.ops_cycle(refs)
    if cycle:
        raise PlanError(f"MAP-SPEC-007: layers' ops depend on each other in a cycle ({' -> '.join(cycle)}); no load order can satisfy them", gc.EXIT_LINT)
    done: list[str] = []

    def add(lid: str):
        if lid in done:
            return
        for r in refs[lid]:
            add(r)
        done.append(lid)

    for ly in layers:
        add(ly["id"])
    return [by_id[i] for i in done]


def ops_dem_resolver(spec: dict, base: Path):
    """The DEM a line_of_sight or viewshed op reads when it names none: the terrain block's file, else a DEM fetched for the sites' window
    (terrain_fetch.dem_for: 3DEP in the United States, Terrarium elsewhere with terrain.terms_acknowledged). -> callable(bounds_wgs84, res_m) -> (path, record)."""
    def resolve(bounds_wgs84, res_m: float = 30.0):
        import map_raster
        import terrain_fetch as tf
        cfg = map_raster.terrain_config(spec)
        src = cfg["source"]
        if src not in ("auto", "3dep", "terrarium", "copernicus"):
            p = Path(src)
            p = p if p.is_absolute() else base / p
            r = tf.dem_for(None, dem_path=str(p))
            return r["path"], r["record"]
        if map_raster.offline():
            raise PlanError("offline (GMAP_OFFLINE=1): the op needs a DEM; give `dem: <file>` on the op or terrain.source", gc.EXIT_REFUSED)
        r = tf.dem_for(bounds_wgs84, max(10.0, float(res_m)), src, gc.workdir(create=True), terms_acknowledged=cfg["terms_acknowledged"])
        if not r["ok"]:
            raise PlanError("the op's DEM could not be fetched: " + "; ".join(f["message"] for f in r["findings"] if f["severity"] == "BLOCK"), gc.EXIT_REFUSED)
        return r["path"], r["record"]
    return resolve


def apply_layer_ops(layer: dict, gdf, ctx_layers: dict | None, measure_crs: str | None, spec: dict | None = None, base: Path | None = None):
    """The layer's `ops:` list through geo_ops.apply_ops, after its join and repair. -> (gdf, steps, notes, findings, provenance records).
    Each step is one sentence for the methods page: what the op did, its rows in and out, the CRS it measured in, and the GeoPandas equivalent.
    A measurement that would be made in degrees (MAP-OPS-001) is a plugin defect and an EXIT_RUNTIME; a missing h3 is EXIT_RUNTIME too; a
    usage error in the op (an unknown field, a bad unit) is EXIT_USAGE."""
    import geo_ops
    import hexbin as hb
    lid = layer["id"]
    ops = layer.get("ops") or []
    if not ops:
        return gdf, [], [], [], []
    ctx = geo_ops.Ctx(layers={k: v for k, v in (ctx_layers or {}).items() if k != lid}, layer_id=lid, measure_crs=measure_crs, base=base,
                      dem=ops_dem_resolver(spec or {}, base or Path.cwd()), engine=layer.get("engine"))
    try:
        gdf, notes, findings = geo_ops.apply_ops(gdf, ops, ctx)
    except geo_ops.MeasureCRSError as ex:
        raise PlanError(f"MAP-OPS-001 layer '{lid}': {ex}", gc.EXIT_RUNTIME)
    except hb.HexbinError as ex:
        raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_RUNTIME)
    except geo_ops.OpsError as ex:
        raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_USAGE)
    steps = []
    for spec, prov in zip(ops, ctx.provenance, strict=True):
        name = prov.get("op") or str(spec.get("op")).lower()
        eq = geo_ops.equivalent(name, dict(spec), prov)["geopandas"]
        where = f" in {prov['measure_crs']}" if prov.get("measure_crs") and name not in ("hexbin",) else ""
        extra = ""
        if prov.get("other_layer"):
            extra += f" against layer '{prov['other_layer']}'"
        if (prov.get("engine") or {}).get("engine") == "duckdb":
            extra += " (run in DuckDB)"
        if prov.get("covered_total") is not None and prov.get("source_total") is not None:
            extra += f"; {prov['covered_total']:,.0f} of {prov['source_total']:,.0f} {prov.get('field') or spec.get('value') or ''} apportioned".rstrip()
        steps.append(f"op {name}{extra}: {prov.get('rows_in', len(gdf))} feature(s) in, {prov.get('rows_out', len(gdf))} out{where}; GeoPandas: {eq}")
    return gdf, steps, list(notes), findings, ctx.provenance


def postload(layer: dict, gdf, base: Path, ctx_layers: dict | None = None, measure_crs: str | None = None, spec: dict | None = None):
    """Table join, then the layer's ops, then coordinate reduction, then sector wedges, in that order: an op works on the joined attributes,
    and wedges are drawn from the already-reduced positions. `ctx_layers` holds the other layers already loaded (by id) for the ops."""
    out = {"steps": [], "notes": [], "findings": [], "privacy": None, "join": None, "wedges": None, "table_sha256": None, "ops": [], "op_notes": []}
    lid = layer["id"]
    flts = layer.get("filter")
    for flt in ([flts] if isinstance(flts, dict) else list(flts or [])):          # a list of filters all apply (an atlas adds its own)
        col, vals = flt.get("column"), flt.get("in")
        if col not in gdf.columns or not isinstance(vals, list) or not vals:
            raise PlanError(f"layer '{lid}': filter needs column (one of {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}) and a list in:", gc.EXIT_USAGE)
        keep = gdf[col].astype(str).isin([str(v) for v in vals])
        if not keep.any():
            raise PlanError(f"layer '{lid}': filter {col} in {vals} keeps no feature", gc.EXIT_USAGE)
        out["steps"].append(f"kept {int(keep.sum())} of {len(gdf)} feature(s) where {col} is {', '.join(map(str, vals))}")
        gdf = gdf[keep].copy()
    if layer.get("join"):
        import table_join as tj
        try:
            gdf, steps, notes, fs, rep, sha = tj.apply_spec(gdf, layer["join"], base, lid, numeric=join_numeric(layer), engine=layer.get("engine"))
        except tj.JoinError as ex:
            out_f = {"id": ex.fid, "severity": "BLOCK", "message": f"layer '{lid}': {ex}", "detail": None}
            raise PlanError(f"{out_f['id']} {out_f['message']}", gc.EXIT_LINT)
        out["steps"] += steps
        out["notes"] += notes
        out["findings"] += fs
        out["join"], out["table_sha256"] = rep, sha
    if layer.get("ops"):
        gdf, steps, notes, fs, prov = apply_layer_ops(layer, gdf, ctx_layers, measure_crs, spec=spec, base=base)
        out["steps"] += steps
        out["notes"] += notes
        out["op_notes"] += [f"layer '{lid}': {n}" for n in notes]
        out["findings"] += fs
        out["ops"] = prov
        if len(gdf) == 0 and layer.get("draw", True) is not False:
            out["findings"].append({"id": "MAP-OPS-003", "severity": "BLOCK", "detail": {"rows_out": 0},
                                    "message": f"layer '{lid}': nothing is left after its ops ({', '.join(str(o.get('op')) for o in layer['ops'])}); a drawn layer cannot be empty. "
                                               "Check the clip or join layer lies in the same place, the distance and the predicate (a helper layer, draw: false, may be empty)"})
    if layer.get("centroids"):
        gdf = gdf.copy()
        gdf.geometry = gdf.geometry.representative_point()
        out["steps"].append("each area is drawn as a point inside it (symbols sized by value)")
    if layer.get("privacy"):
        import site_privacy as sp
        try:
            gdf, steps, notes, fs, rec = sp.apply(gdf, layer["privacy"], lid)
        except sp.PrivacyError as ex:
            raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_USAGE)
        out["steps"] += steps
        out["notes"] += notes
        out["findings"] += fs
        out["privacy"] = rec
    if layer.get("wedges"):
        import telecom_geo as tg
        w = layer["wedges"]
        try:
            df = gdf.to_crs("EPSG:4326")
            import pandas as pd
            tab = pd.DataFrame(df.drop(columns=df.geometry.name))
            tab["lon"], tab["lat"] = df.geometry.x.to_numpy(), df.geometry.y.to_numpy()
            if w.get("azimuth") and w["azimuth"] != "azimuth":
                tab = tab.rename(columns={w["azimuth"]: "azimuth"})
            if isinstance(w.get("beamwidth"), str) and w["beamwidth"] != "beamwidth":
                tab = tab.rename(columns={w["beamwidth"]: "beamwidth"})
            fixed_bw = None if isinstance(w.get("beamwidth"), str) else (float(w["beamwidth"]) if w.get("beamwidth") else None)
            rf = w.get("radius_field")
            if rf and rf not in tab.columns:
                raise ValueError(f"radius_field '{rf}' is not a column of the layer")
            gdf, wfs = tg.make_wedges(tab, radius_m=float(w.get("radius_m", tg.DEFAULT_RADIUS_M)), beamwidth=fixed_bw, radius_field=rf)
        except (ValueError, AttributeError) as ex:
            raise PlanError(f"layer '{lid}': wedges: {ex}", gc.EXIT_USAGE)
        out["wedges"] = {"sectors": int(len(gdf)), "radius_m": float(w.get("radius_m", tg.DEFAULT_RADIUS_M)), "radius_field": rf}
        rad = (f"radius from '{rf}' ({out['wedges']['radius_m']:g} m where it is empty)" if rf else f"radius {out['wedges']['radius_m']:g} m")
        out["steps"].append(f"{len(gdf)} sector wedge(s) drawn from site positions: {rad}, azimuth and beamwidth from the table (display geometry, not coverage)")
        out["notes"].append(tg.WEDGE_NOTE)
        out["findings"] += wfs
    return gdf, out


def time_window(gdf, ly: dict, lid: str):
    """1.0.0 (TODO M15): `time: {at: 2026-06, cumulative: true}` with the layer's `time_field`: only the features of that period (or, cumulative,
    of every period up to it), read with the same period rules as the atlas and the web time slider. -> (gdf, note)."""
    import timekeys as tk
    t = ly["time"]
    fld = ly.get("time_field")
    if not fld or fld not in gdf.columns:
        raise PlanError(f"layer '{lid}': time.at needs the layer's time_field (a column of dates, years or periods)", gc.EXIT_USAGE)
    unit = str(t.get("unit") or "auto")
    try:
        per = tk.periods(gdf[fld].tolist(), unit, order=ly.get("time_order"))
        wp = tk.periods([t["at"]], per.unit, order=ly.get("time_order"))
        want = wp.keys[0]
    except (tk.TimeError, IndexError) as ex:
        raise PlanError(f"layer '{lid}': time.at '{t['at']}': {ex}", gc.EXIT_USAGE)
    if want is None:
        raise PlanError(f"layer '{lid}': time.at '{t['at']}' is not a {per.word}", gc.EXIT_USAGE)
    cum = bool(t.get("cumulative", False))
    keep = per.upto(want) if cum else per.at(want)
    import numpy as np
    out = gdf[np.asarray(keep, bool)].copy()
    lab = per.labels.get(want) or wp.label(want)
    if not len(out):
        raise PlanError(f"layer '{lid}': no feature of '{fld}' is " + ("in or before " if cum else "in ") + lab, gc.EXIT_USAGE)
    return out, (f"time: {len(out):,} of {len(gdf):,} features, " + (f"everything up to and including {lab}" if cum else f"those of {lab}")
                 + f" ('{fld}')")


HEX_FEATURES_PER_CELL = 4.0     # `resolution: auto` on a map: cells average about this many features, so a count map has classes to show


def map_hex_resolution(gdf) -> tuple[int, str]:
    """The H3 resolution a `hexbin: {resolution: auto}` map layer gets: the one whose cells would average about HEX_FEATURES_PER_CELL features
    over the data's extent (nearest in log area), never finer than hexbin.auto_resolution's pick for that extent (which aims at a few hundred to
    a few thousand cells and is right for a file, not for a map of 200 points where every point would be its own cell). -> (resolution, why)."""
    import math
    import hexbin as hb
    g = gdf if gdf.crs is None or gdf.crs.equals("EPSG:4326") else gdf.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = [float(v) for v in g.total_bounds]
    n = int(len(g))
    finest = hb.auto_resolution(hb._extent_km(g))
    if n < 2 or not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        return finest, "chosen from the data extent"
    lat = math.radians((miny + maxy) / 2)
    w_km = max((maxx - minx) * 111.32 * math.cos(lat), 0.5)
    h_km = max((maxy - miny) * 110.57, 0.5)
    want = HEX_FEATURES_PER_CELL * w_km * h_km / n            # km2 per cell
    areas = [2.598 * e * e for e in hb.EDGE_KM]
    res = min(range(16), key=lambda r: abs(math.log(areas[r]) - math.log(want)))
    res = min(res, finest)
    return res, f"chosen for the map: about {HEX_FEATURES_PER_CELL:g} features per cell over the {w_km:,.0f} by {h_km:,.0f} km extent (resolution {res})"


def _fmt_km(km: float) -> str:
    """0.53 -> '530 m', 1.407 -> '1.4 km', 26.07 -> '26 km'."""
    if km < 1:
        return f"{km * 1000:.0f} m"
    return f"{km:.2g} km" if km < 10 else f"{km:,.0f} km"


def aggregate_layer(layer: dict, gdf, measure_crs: str | None) -> tuple[object, dict]:
    """A `role: hexbin`, `heat` or `grid` layer: its points (or a point inside each other feature) counted into H3 hexagons or square cells
    through the geo_ops `hexbin` / `grid` op. -> (cells, info) where info names the value field the map classes (`count`, or `<value>_<agg>`),
    the cell shape, the legend title and note, and the op's provenance record. `heat` is hexbin at automatic resolution with a count: hex density,
    never a kernel density surface (a KDE's bandwidth is a choice the reader cannot see; a cell count is a number they can check)."""
    import geo_ops
    import hexbin as hb
    lid, role = layer["id"], layer["role"]
    basis = None
    if role in ("hexbin", "heat"):
        p = dict(layer.get("hexbin") or {})
        if role == "heat":
            p = {"resolution": "auto", "agg": "count"}
        res = p.get("resolution", "auto")
        if res in (None, "auto"):
            res, basis = map_hex_resolution(gdf)
        spec = {"op": "hexbin", "resolution": res, "agg": p.get("agg", "count"), "min_count": int(p.get("min_count", 1))}
        if p.get("value"):
            spec["value"] = p["value"]
    else:
        p = dict(layer.get("grid") or {})
        if p.get("size") is None:
            raise PlanError(f"layer '{lid}': role grid needs grid: {{size: <distance with units>}}", gc.EXIT_USAGE)
        spec = {"op": "grid", "shape": "square", "size": p["size"], "agg": p.get("agg", "count"), "min_count": int(p.get("min_count", 1))}
        for k in ("units", "value", "extent"):
            if p.get(k) is not None:
                spec[k] = p[k]
    ctx = geo_ops.Ctx(layer_id=lid, measure_crs=measure_crs, engine=layer.get("engine"))
    try:
        cells, notes, findings = geo_ops.apply_ops(gdf, [spec], ctx)
    except geo_ops.MeasureCRSError as ex:
        raise PlanError(f"MAP-OPS-001 layer '{lid}': {ex}", gc.EXIT_RUNTIME)
    except hb.HexbinError as ex:
        raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_RUNTIME)
    except geo_ops.OpsError as ex:
        raise PlanError(f"layer '{lid}': {ex}", gc.EXIT_USAGE)
    if len(cells) == 0:
        raise PlanError(f"layer '{lid}': no cell holds a feature (the layer has {len(gdf)} feature(s); min_count {spec['min_count']})", gc.EXIT_USAGE)
    prov = ctx.provenance[0]
    if basis:
        prov["resolution_basis"] = basis
    agg, val = spec["agg"], spec.get("value")
    vf = "count" if agg == "count" else f"{val}_{agg}"
    if vf not in cells.columns:
        raise PlanError(f"layer '{lid}': the aggregation made no '{vf}' field (fields: {', '.join(c for c in cells.columns if c != cells.geometry.name)})", gc.EXIT_RUNTIME)
    what = lg._layer_title(layer) if agg == "count" else f"{agg.capitalize()} {val}"
    if spec["op"] == "hexbin":
        res = int(prov.get("resolution", 0))
        edge_km = hb.EDGE_KM[res]
        title = f"{what} per hex (about {_fmt_km(2 * edge_km)} across)"
        note = f"H3 resolution {res}; hex edge about {_fmt_km(edge_km)}"
        info = {"shape": "hexagon", "resolution": res, "edge_km": edge_km, "cells": int(len(cells)), "value_field": vf, "agg": agg, "value": val,
                "title": title, "note": note, "equivalent": geo_ops.equivalent("hexbin", spec, prov)["geopandas"]}
        step = f"{len(gdf)} feature(s) counted into {len(cells)} H3 hexagon(s) at resolution {res} (edge about {_fmt_km(edge_km)}); the map classes `{vf}` per cell"
    else:
        size_m = float(prov.get("size_m") or 0)
        side = _fmt_km(size_m / 1000.0)
        title = f"{what} per {side} cell"
        note = None
        info = {"shape": "square", "size_m": size_m, "cells": int(len(cells)), "value_field": vf, "agg": agg, "value": val, "title": title, "note": None,
                "equivalent": geo_ops.equivalent("grid", spec, prov)["geopandas"]}
        step = f"{len(gdf)} feature(s) counted into {len(cells)} square cell(s) of {side} laid in {prov.get('measure_crs')}; the map classes `{vf}` per cell"
    info.update({"op": spec, "provenance": prov, "notes": list(notes), "findings": list(findings), "step": step})
    return cells, info


def _agg_spec(ly: dict, info: dict) -> dict:
    """The layer spec as the planner, the legend and the lints read an aggregated layer: the cells' value field, a sequential classification of
    round-number classes unless the spec says otherwise, and a legend title that names the cell size (the spec's own title wins)."""
    cls = {"method": "pretty", "classes": 5, **(ly.get("classification") or {})}
    legend = dict(ly.get("legend") or {})
    legend.setdefault("title", info["title"])
    if info.get("note") and not legend.get("note"):
        legend["note"] = info["note"]
    return {**ly, "value_field": info["value_field"], "classification": cls, "legend": legend}


# ---------------------------------------------------------------------------------------------------------------- main
def prepare(spec_path, *, mode: str | None = None, brand: str | None = None, template: str | None = None, spec_edit=None, use_tiles: bool = True, classic_layout: bool = False) -> Plan:
    """`spec_edit(spec)` may adjust the loaded spec in memory (the web page turns the static scale bar off); the file is never changed."""
    spec_path = Path(spec_path)
    spec = gc.load_mapspec(spec_path)
    if spec_edit:
        spec_edit(spec)
    findings: list[dict] = []
    findings += ml.lint_spec(spec)
    if ml.worst(findings):
        raise PlanError("the MapSpec has blocking problems:\n" + "\n".join(f"  {f['id']} {f['message']}" for f in findings if f["severity"] == "BLOCK"), gc.EXIT_LINT)
    manifest = gc.build_manifest()
    try:
        r = bl.resolve(manifest, spec.get("brand"), brand)
    except bl.BrandError as ex:
        raise PlanError(str(ex), gc.EXIT_REFUSED)
    slug = r["brand"]
    pack = bl.load_pack(slug)
    import map_look
    look, look_why = map_look.resolve(spec)              # 0.22.0: auto resolves to a named look from the map's main data
    if look == "dark" and not mode:
        mode = THEME_MODES["dark"]                        # the dark look is a dark ground (an explicit --theme still wins)
    mode = mode or theme_mode(spec)
    import map_style                                      # 1.4.0 (R2-PACK): the pack sets the base tokens and the type; fixed for this build
    try:
        style, base_tokens, style_notes = map_style.resolve_for_plan(spec, pack, mode)
    except map_style.StyleError as ex:
        raise PlanError(f"{ex.rule}: {ex}", gc.EXIT_LINT)
    tokens = map_style.after_look(style, map_look.apply_tokens(base_tokens, look))      # 0.17.0: generated ramps and the categorical order (auto)
    import style_lint
    findings += style_lint.lint_palette(style, pack["tokens"]["modes"])     # MAP-STYLE-005: the pack's colours for this brand
    tpl_value = template or spec.get("template") or "letter-landscape"
    tpl_auto = tpl_value == "auto"                       # 1.5.0 (R3-UNITS): Letter or A-series by the country under the map, chosen once the extent is known
    if tpl_auto:
        tpl_value = str((pack.get("pack") or {}).get("default_template") or "letter-landscape")
    try:
        tpl_name = lo.template_label(tpl_value)              # a custom sheet goes by custom-<w>x<h>mm (M16)
        tpl = lo.apply_layout(lo.load_template(tpl_value), spec, classic_layout)
    except lo.LayoutError as ex:
        raise PlanError(str(ex), gc.EXIT_LINT)
    base = spec_path.parent
    crs_spec = spec.get("crs") or {}
    measure_crs = str(crs_spec["measure"]).strip() if crs_spec.get("measure") else None
    plans: dict[str, LayerPlan] = {}
    loaded: dict[str, object] = {}            # each layer's frame after its own steps, by id: what another layer's ops see (M11)
    used_by: dict[str, list[str]] = {}
    for ly in spec["layers"]:
        for ref in ml.op_layer_refs(ly):
            used_by.setdefault(ref, []).append(ly["id"])
    pre_notes: list[str] = []                 # notes made before the Plan exists (a layer repair, an op); appended to plan.notes below
    import map_sources                        # M13: library service layers load after the layers their window is fitted to
    for ly in map_sources.defer(load_order(spec["layers"]), spec):     # a layer named by another layer's ops loads first
        lid = ly["id"]
        draw = ly.get("draw", True) is not False
        if ly["role"] in RASTER_ROLES:        # read for the map's window once the extent is known (map_raster.prepare, M12)
            plans[lid] = raster_layer_plan(ly, base)
            continue
        gdf, entry, assumed, io_notes, cap = load_source(ly, base, window=lambda ly=ly: map_sources.window(spec, tpl, ly, loaded, base))
        repair_note = None
        if ly.get("repair"):
            gdf, repair_note = repair_layer(ly, gdf)
            pre_notes.append(f"layer '{lid}': {repair_note.msg}")
        gdf, post = postload(ly, gdf, base, ctx_layers=loaded, measure_crs=measure_crs, spec=spec)
        los_defaults(ly, gdf)
        pre_notes += post["op_notes"]
        role = ly["role"]
        agg_info = None
        layer_spec = ly
        if role in AGG_ROLES:
            if not draw:
                raise PlanError(f"layer '{lid}': a {role} layer is drawn from its cells; a helper (draw: false) keeps its points, so give it role points", gc.EXIT_USAGE)
            gdf, agg_info = aggregate_layer(ly, gdf, measure_crs)
            if role == "heat":
                pre_notes.append(f"layer '{lid}': heat maps are drawn as hex density (H3 cells at automatic resolution, one count per cell), not as a kernel density surface; role hexbin with a `hexbin:` block sets the resolution")
            role = "grid" if role == "grid" else "hexbin"
            layer_spec = _agg_spec(ly, agg_info)
            pre_notes += [f"layer '{lid}': {n}" for n in agg_info["notes"]]
            post["findings"] += agg_info["findings"]
        gdf, unpacked = unpack_collections(gdf)
        kind = geom_kind(gdf)
        gdf, dropped = keep_kind(gdf, kind)
        time_note = None
        if isinstance(ly.get("time"), dict) and ly["time"].get("at") is not None:
            gdf, time_note = time_window(gdf, ly, lid)          # 1.0.0 (TODO M15): one period (or everything up to it) on a single map
        if role == "choropleth" and kind != "polygon":
            raise PlanError(f"layer '{lid}': a choropleth needs polygons, but this layer holds {kind}s", gc.EXIT_USAGE)
        lp = LayerPlan(id=lid, spec=layer_spec, role=role, gdf=gdf, kind=kind, title=lg._layer_title(layer_spec), value_field=layer_spec.get("value_field"),
                       confidentiality=ly.get("confidentiality") or ("client-confidential" if cap and cap.get("confidential") else "public"), entry=entry, assumed_crs=assumed, capture=cap)
        lp.draw, lp.aggregation, lp.ops_provenance, lp.used_by = draw, agg_info, list(post["ops"]), list(used_by.get(lid, []))
        lp.crs_original = crs_label(gdf.crs)
        if unpacked:
            lp.transformations.append(f"{unpacked} geometry collection(s) reduced to their {kind}s")
        lp.rows_loaded = int(len(gdf)) + int(dropped or 0)
        lp.src_file, lp.src_sha256 = source_file_hash(ly, entry, base)
        if cap:
            lp.src_file, lp.src_sha256 = Path(cap["_data_path"]).name, (cap.get("result") or {}).get("sha256")
            lp.transformations.append(capture_step(cap))
        lp.bbox = bbox_wgs84(gdf)
        if assumed:
            lp.transformations.append(f"the file states no coordinate system; {lp.crs_original} was assumed")
        if repair_note is not None:
            lp.transformations.append(repair_note.msg)
            lp.notes.append(repair_note.msg)
        if dropped:
            lp.notes.append(f"{dropped} feature(s) of another geometry type or with empty geometry were left out")
            lp.transformations.append(f"{dropped} feature(s) of another geometry type or with empty geometry were left out")
        if time_note:
            lp.notes.append(time_note)
            lp.transformations.append(time_note)
        lp.notes += [n["message"] if isinstance(n, dict) and "message" in n else str(n) for n in io_notes]
        lp.transformations += [n["step"] for n in io_notes if isinstance(n, dict) and n.get("step")]     # a loader's methods line (M13)
        lp.transformations += post["steps"]
        lp.notes += post["notes"]
        if agg_info:
            lp.transformations.append(agg_info["step"] + f"; GeoPandas: {agg_info['equivalent']}")
            lp.notes += agg_info["notes"]
            if ly["role"] == "heat":
                lp.notes.append("heat maps are drawn as hex density (one count per H3 cell), not as a kernel density surface")
        if not draw:
            who = ", ".join(f"'{u}'" for u in lp.used_by) or "no layer"
            lp.transformations.append(f"helper layer (draw: false): loaded for the ops of layer(s) {who}; never drawn, not in the legend, the extent, the web page or the data package")
        lp.privacy, lp.join_report, lp.wedge_info = post["privacy"], post["join"], post["wedges"]
        if post["table_sha256"]:
            lp.notes.append(f"joined table fingerprint (SHA-256): {post['table_sha256'][:16]}")
        findings += [{**f, "layer": lid} for f in post["findings"]]
        fixed = geo_ops_fixed_ids(ly.get("ops"))
        for f in dl.lint_data(gdf, assumed=assumed, id_field=ly.get("id_field")):
            if f["id"] in fixed:
                continue                      # the op that fixes it ran; its own finding replaces the input's (geo_ops.FIXES)
            if not draw and f["id"] == "MAP-DATA-008":
                continue                      # a helper is never drawn, so the renderer guard does not apply to it
            if f["id"] == "MAP-DATA-002" and ly.get("repair") is True:
                continue                      # 1.2.0 (Day 4 issue 16): the layer's own repair: true ran; its note replaces the input's finding
            msg = f["message"]
            if f["id"] in ("MAP-DATA-002",) and isinstance(f.get("detail"), list) and f["detail"]:
                msg = msg.replace(" feature(s) have", f" feature(s) (rows {', '.join(map(str, f['detail'][:5]))}{', ...' if len(f['detail']) > 5 else ''}) have", 1)
            if not msg.startswith(f"layer '{lid}'"):
                msg = f"layer '{lid}': {msg}"          # 1.2.0 (Day 4 issue 16): every data finding names its layer in the message, not only in its field
            findings.append({**f, "layer": lid, "message": msg})
        plans[lid] = lp
        loaded[lid] = gdf
    layers: list[LayerPlan] = [plans[ly["id"]] for ly in spec["layers"] if plans[ly["id"]].draw]       # the spec's order is the draw order
    helpers: list[LayerPlan] = [plans[ly["id"]] for ly in spec["layers"] if not plans[ly["id"]].draw]
    for h in helpers:
        h.provenance = layer_provenance(h)
    if not layers:
        raise PlanError("every layer is a helper (draw: false); a map needs at least one layer to draw", gc.EXIT_USAGE)
    findings += home_address_findings(layers, spec.get("audience") or "external")       # 0.15.0: MAP-LIC-013
    if dl.worst([f for f in findings if f["id"].startswith(("MAP-DATA", "MAP-OPS"))]):
        raise PlanError("the data has blocking problems:\n" + "\n".join(f"  {f.get('layer', '')}: {f['id']} {f['message']}" for f in findings if f["severity"] == "BLOCK"), gc.EXIT_LINT)

    # ---- extent in WGS84 first, to choose the display CRS
    ext = spec.get("extent") or {}
    mode_e = ext.get("mode", "fit_layers")
    placed = [l for l in layers if len(l.gdf)]               # a fetched or service raster has no footprint until it is read for the window
    # a library service layer is fetched for the window, not fitted to (M13)
    data_layers = [l for l in placed if l.role in DATA_ROLES and l.role not in ("hillshade", "contour") and not (l.entry and l.capture)] or placed
    if not data_layers and mode_e == "fit_layers":
        raise PlanError("the map's layers have no extent of their own (a service picture or a fetched raster is read for the map's window); "
                        "give extent: {mode: bbox, bbox: [...]} or {mode: place, place: ...}", gc.EXIT_USAGE)
    wgs = None
    if mode_e == "bbox" and ext.get("bbox"):
        wgs = tuple(float(v) for v in ext["bbox"])
    elif mode_e == "place":
        wgs = place_bbox(ext.get("place", ""))
    elif mode_e == "preset":
        if ext.get("preset") not in PRESET_BBOX:
            raise PlanError(f"extent.preset '{ext.get('preset')}' is unknown; use one of {', '.join(PRESET_BBOX)}", gc.EXIT_USAGE)
        wgs = PRESET_BBOX[ext["preset"]]
    else:
        parts = []
        for l in data_layers:
            if l.gdf.crs is None:
                raise PlanError(f"layer '{l.id}' has no coordinate reference system; say which one in the layer source (crs:)", gc.EXIT_LINT)
            parts.append(l.gdf.to_crs("EPSG:4326").total_bounds)
        a = np.array(parts)
        wgs = (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 2].max()), float(a[:, 3].max()))
    area_stat = any(l.role in AREA_ROLES for l in layers)
    if tpl_auto:
        try:
            import geo_locale as gl
            iso = gl.country_at((wgs[0] + wgs[2]) / 2, (wgs[1] + wgs[3]) / 2)
            paper = (gl.country(iso) or {}).get("paper", "a")
            orient = "portrait" if tpl_value.endswith("portrait") else "landscape"
            want = f"{'letter' if paper == 'letter' else 'a4'}-{orient}"
            if want != tpl_value:
                tpl_value, tpl_name = want, want
                tpl = lo.apply_layout(lo.load_template(want), spec, classic_layout)
            pre_notes.append(f"template auto: {want}, the paper of {(gl.country(iso) or {}).get('name', 'the country under the map') if iso else 'the default'}")
        except (lo.LayoutError, Exception) as ex:  # noqa: BLE001 - the brand default stays
            pre_notes.append(f"template auto: kept {tpl_value} ({type(ex).__name__})")
    tiles_rec, tiles_notes = prepared_tiles(spec, use_tiles, wgs, base)
    choice = ce.choose_display(wgs, "thematic" if area_stat else "reference", area_stat=area_stat or None, tiles=bool(tiles_rec), prefer=crs_spec.get("prefer"))
    tiles_own_crs = bool(tiles_rec) and crs_spec.get("display") not in (None, "auto", "EPSG:3857")     # 1.11.0: the composite is warped to it
    cs = ce.mapspec_crs(choice, "EPSG:3857" if tiles_rec and not tiles_own_crs else crs_spec.get("display"))
    display = cs["display"]
    crs_info = {"display": display, "rationale": cs["rationale"], "name": choice["name"], "equal_area": ce.is_equal_area(display),
                "antimeridian": choice.get("antimeridian", False)}
    elements = spec.get("elements") or {}
    sb_on = (elements.get("scale_bar") or {}).get("on", True) is not False
    vec = [l for l in layers if l.kind != "raster"]          # a raster is warped into the display grid when it is read; the vector checks are not for it
    findings += dl.lint_crs(display, map_type="choropleth" if area_stat else spec.get("map_type"), extent=wgs, has_tiles=bool(tiles_rec),
                            user_chosen=bool(crs_spec.get("display") and crs_spec.get("display") != "auto"),
                            datum_transforms_declared=len(str(crs_spec.get("datum_note") or "").strip()) >= 12,
                            layer_crs=[l.gdf.crs for l in vec], scale_bar=sb_on, rationale=cs["rationale"])
    # each layer against its own CRS (area of use) and against the display CRS (datum transform accuracy), before anything moves
    findings += dl.lint_layer_crs([(l.id, l.gdf, bool(l.assumed_crs)) for l in vec], display, extent=wgs,
                                  map_width_mm=float((tpl.get("map") or {}).get("w") or dl.MAP_WIDTH_MM))
    if dl.worst([f for f in findings if f["id"].startswith("MAP-CRS")]):
        raise PlanError("the projection has blocking problems:\n" + "\n".join(f"  {f['id']} {f['message']}" for f in findings if f["severity"] == "BLOCK" and f["id"].startswith("MAP-CRS")), gc.EXIT_LINT)
    # ---- reproject and style
    for l in layers:
        if l.gdf.crs is None:
            raise PlanError(f"layer '{l.id}' has no coordinate reference system", gc.EXIT_LINT)
        if not l.gdf.crs.equals(display):
            l.transformations.append(f"reprojected from {l.crs_original} to {crs_label(display)}")
        l.gdf = l.gdf.to_crs(display)
    # the layers against each other, now that they share one CRS (MAP-CRS-007); crs.alignment_check: false turns it off
    if crs_spec.get("alignment_check", True) is not False:
        findings += dl.lint_alignment([(l.id, l.gdf, l.kind, bool(l.assumed_crs)) for l in vec], display,
                                      skip_outlines={l.id for l in vec if dl.not_an_outline(l.id, l.role, l.title)},
                                      bbox_extent=mode_e == "bbox")
    if mode_e == "fit_layers":
        extent = _bounds_of(data_layers)
    else:
        extent = project_bbox(wgs, display)
    plan = Plan(spec=spec, spec_path=spec_path, manifest=manifest, brand=slug, brand_name=bl.brand_name(slug), pack=pack, tokens=tokens, mode=mode,
                tpl=tpl, tpl_name=tpl_name, crs=display, crs_info=crs_info, extent=extent, layers=layers, basemap=[], findings=findings, helpers=helpers,
                look=look, style=style)
    plan.look_asked = map_look.look_of(spec)
    plan.notes += style_notes
    import treatments as treat_mod
    plan.treatments = treat_mod.merged(style, spec)
    if look != "classic":
        how = f"chosen by style.look auto for {look_why}" if plan.look_asked == "auto" else "asked for"
        plan.notes.append(f"look '{look}' ({how}): class colours generated from the brand colour, context drawn lighter than the data, casings under "
                          "data lines and points, point sizes by feature count (style.look: classic keeps the 0.16 look)")
    if measure_crs:
        plan.notes.append(f"measurement CRS pinned to {measure_crs} (crs.measure) for every layer op")
    if str(crs_spec.get("datum_note") or "").strip():
        plan.notes.append(str(crs_spec["datum_note"]).strip())
    plan.notes.append(f"brand '{slug}' chosen by rule '{r['rule']}'" + (f" ({r['note']})" if r.get("note") else ""))
    plan.notes += pre_notes
    assign_palette(plan)
    for l in layers:
        if l.role in RASTER_ROLES:
            continue                          # coloured from the brand when it is read for the window (map_raster.prepare)
        style_layer(plan, l)
    import map_emphasis
    map_emphasis.assign(plan)                 # 1.13.0: the layers the message names at full strength, the others quieter
    plan.notes += tiles_notes
    load_basemap(plan, tiles_rec, explained=bool(tiles_notes), vector_file=vector_source(spec, base) if use_tiles else None)
    add_context(plan)
    import map_raster
    try:
        map_raster.prepare(plan)              # rasters read for the window, relief under the land's layers, contours as line layers (M12)
    except map_raster.RasterLayerError as ex:
        raise PlanError(str(ex), ex.code)
    return plan


def raster_layer_plan(ly: dict, base: Path) -> LayerPlan:
    """A raster, hillshade or contour layer before the extent: its footprint (a file's bounds, or nothing for a fetched or service raster), the
    library entry and what map_raster needs to read it for the window."""
    import map_raster
    lid = ly["id"]
    try:
        map_raster.normalize(ly)
        gdf, entry, info = map_raster.load_layer(ly, base)
    except map_raster.RasterLayerError as ex:
        raise PlanError(str(ex), ex.code)
    role = ly["role"]
    lp = LayerPlan(id=lid, spec=ly, role=role, gdf=gdf, kind="raster", title=lg._layer_title(ly),
                   confidentiality=ly.get("confidentiality") or "public", entry=entry)
    lp.raster = dict(info)
    lp.crs_original = crs_label((info.get("open_info") or {}).get("crs")) or ("EPSG:4326" if info["src"] != "file" else None)
    lp.rows_loaded = 0
    if info["src"] == "file":
        lp.src_file, lp.src_sha256 = source_file_hash({"source": {"type": "file", "path": info["path"]}}, None, base)
    if role != "raster":
        lp.legend_block = None
    return lp


LOS_CLASSES = (("clear", "@brand.categorical.1", "Clear of the terrain"), ("obstructed", "@brand.categorical.5", "Obstructed by the terrain"))


def los_defaults(ly: dict, gdf=None) -> None:
    """A layer whose ops end in line_of_sight is drawn as clear and obstructed lines in two brand colours (categorical 1 and 5), unless the spec
    classifies it itself; a viewshed layer is a polygon of visible ground in a see-through brand colour. Runs after the ops, so the classes are the
    ones the links have."""
    ops = [o for o in (ly.get("ops") or []) if isinstance(o, dict)]
    last = str(ops[-1].get("op", "")).lower() if ops else ""
    if last == "line_of_sight" and not ly.get("classification") and not ly.get("value_field"):
        have = set(str(v) for v in gdf["status"]) if gdf is not None and "status" in gdf.columns else {"clear", "obstructed"}
        rows = [r for r in LOS_CLASSES if r[0] in have]
        ly["value_field"] = "status"
        ly["classification"] = {"method": "categorical", "order": [r[0] for r in rows], "colors": [r[1] for r in rows], "labels": [r[2] for r in rows]}
        ly["legend"] = {"title": "Line of sight (bare earth)", "swatch": "line", **(ly.get("legend") or {})}
    elif last == "viewshed" and not ly.get("symbol"):
        ly["symbol"] = {"fill": "@brand.categorical.1", "stroke": "@brand.categorical.1", "opacity": 0.35}
        ly.setdefault("legend", {})
        ly["legend"] = {"title": "Visible ground (bare earth)", **ly["legend"]}


# ---------------------------------------------------------------------------------------------------------------- scale and context pack
def draw_order(layers) -> list:
    """The layers in drawing order: raster pictures first (a raster is the bottom of the data, over the ground and the relief), then the context
    pack, then contours, then the other data layers as the spec lists them."""
    pics = [l for l in layers if l.kind == "raster"]
    ctx = [l for l in layers if l.role == "context"]
    cont = [l for l in layers if l.role == "contour"]
    rest = [l for l in layers if l.kind != "raster" and l.role not in ("context", "contour")]
    return pics + ctx + cont + rest


def scale_denominator(plan: Plan) -> float:
    """The main map's scale denominator as the frame will fit it (before any strip is reserved for overlays): what the tiers are read against."""
    import scalebar as sb
    rect = lo.rect_of(plan.tpl["map"])
    fr = lo.Frame.fit(rect, plan.extent, float((plan.spec.get("extent") or {}).get("padding_pct", 5)))
    try:
        den = sb.scale_info(plan.crs, fr.visible, rect.w).get("scale_denominator")
    except Exception:  # noqa: BLE001 - an exotic CRS without factors still gets context, at the world tier
        den = None
    return float(den) if den else NE_ZOOM0_DENOMINATOR


def context_window(plan: Plan):
    """The lon/lat window a context layer is cut to: the map's own window plus a margin, never the 25-degree basemap window (the pack is dense)."""
    wgs = _wgs84_extent(plan)
    return _lonlat_window(wgs, CONTEXT_WINDOW_PAD_DEG)


def _context_layer(plan: Plan, lid: str, kind: str, gdf, entry: dict, *, geom_kind_: str, fill=None, stroke=None, width_mm=0.15, fill_opacity=1.0,
                   stroke_opacity=1.0, dash=None, title: str = "") -> LayerPlan:
    spec = {"id": lid, "role": "context", "source": {"type": "ref", "id": entry["id"]}, "legend": {"include": False}}
    l = LayerPlan(id=lid, spec=spec, role="context", gdf=gdf, kind=geom_kind_, title=title or lid, entry=entry, context_kind=kind)
    l.fill, l.stroke, l.stroke_w_mm, l.fill_opacity, l.stroke_opacity = fill, stroke, width_mm, fill_opacity, stroke_opacity
    l.symbol = {"dash": dash} if dash else {}
    l.crs_original = crs_label(entry.get("crs")) or "EPSG:4326"
    l.rows_loaded = int(len(gdf))
    l.src_file, l.src_sha256 = source_file_hash(spec, entry, plan.spec_path.parent)
    l.bbox = bbox_wgs84(gdf)
    l.transformations.append(f"cut to the map window and reprojected to {crs_label(plan.crs)}; drawn as context, never in the legend or the extent")
    l.provenance = layer_provenance(l)
    return l


def add_context(plan: Plan) -> None:
    """The bundled context pack under the data layers: urban areas as a faint tint of the land, lakes and rivers in the water colour, roads in a
    muted grey (two widths; ferries dashed), each kind included or left out by the map's scale (SCALE_TIERS). Context layers go in front of
    plan.layers after the data layers (so plan.layers[0] is still the first data layer) and are drawn first (draw_order: above the land fill,
    under every data layer); they are never in the legend, never part of the extent, and the colour checks for data layers (MAP-CART-015/016)
    do not read them."""
    cfg = context_config(plan.spec)
    has_tiles = any(b.get("kind") == "tiles" for b in plan.basemap)
    roles = ["points" if l.wedge_info else l.role for l in plan.layers]       # sector wedges are site geometry, not an area fill
    preset, kinds = context_kinds(cfg, roles, has_tiles)
    if plan.vector_ground:
        return _context_from_tiles(plan, cfg, preset, kinds)
    if not kinds:
        if cfg["on"] and preset == "none":
            plan.notes.append("context pack: none" + (" (the tile basemap carries its own roads and water)" if has_tiles else ""))
        return
    t = plan.tokens
    den = scale_denominator(plan)
    tier = scale_tier(den)
    if "roads" in kinds and den >= ROADS_MAX_DENOMINATOR:
        kinds = [k for k in kinds if k != "roads"]
        plan.notes.append(f"context pack: roads left out at 1:{den:,.0f} (drawn only on maps larger than 1:{ROADS_MAX_DENOMINATOR:,})")
    window = context_window(plan)
    from shapely.geometry import box
    vis = box(*plan_visible_pad(plan))
    op = max(0.0, min(1.0, cfg["opacity"]))
    over = map_look.params(plan.look)["context_over_surface"]
    if over < 1 and any(l.kind == "raster" and l.role == "raster" for l in plan.layers) and "opacity" not in (plan.spec.get("context") or {}):
        op *= over                                        # 0.22.0 surface look: the context sits over the surface a little see-through
        plan.notes.append(f"context pack at {over:.0%} over the surface (look 'surface')")
    added: list[LayerPlan] = []
    what: list[str] = []
    gres = (plan.crs_info.get("ground_resolution") or {}).get("water")
    if gres and NE10 not in gres and any(k in ("lakes", "rivers") for k in kinds):
        # Rule (0.12.0, MAP-CART-024): the context pack's water is Natural Earth 1:10m; on ground of another resolution it would draw a second
        # shoreline beside the ground's, so the ground's own water stands for it (TIGER holds the lakes and wide rivers; 1:110m maps are national)
        kinds = [k for k in kinds if k not in ("lakes", "rivers")]
        plan.notes.append(f"context pack: lakes and rivers left out; the ground's {gres} water is drawn instead (land and water at one resolution)")
    for kind in kinds:
        rank = tier[kind]
        if rank == -1:
            continue
        try:
            gdf, e = _context_roads_tiger(plan, cfg) if kind == "roads" and cfg.get("roads") in TIGER_ROADS else (None, None)
            if gdf is None:
                gdf, e = load_ref_layer(CONTEXT_REFS[kind])
        except PlanError as ex:
            plan.notes.append(f"context pack: {kind} not drawn ({ex})")
            continue
        if rank is not None and "scalerank" in gdf.columns:
            gdf = gdf[gdf["scalerank"].astype(float) <= rank]
        gdf = window_reproject(gdf, window, plan.crs)
        gdf = gdf[gdf.geometry.intersects(vis)].copy() if len(gdf) else gdf
        if not len(gdf):
            continue
        if kind == "urban":
            # a light tint of the land: the brand's second neutral step, see-through, reads as land darkened by about 6 percent
            added.append(_context_layer(plan, "context-urban", kind, gdf, e, geom_kind_="polygon", fill=t["neutral_ramp"][1], stroke="none",
                                        width_mm=0.0, fill_opacity=0.45 * op, title="Urban areas"))
            what.append(f"urban areas ({len(gdf)})")
        elif kind == "lakes":
            added.append(_context_layer(plan, "context-lakes", kind, gdf, e, geom_kind_="polygon", fill=t["water"], stroke=t["water"], width_mm=0.1,
                                        fill_opacity=op, stroke_opacity=op, title="Lakes"))
            what.append(f"lakes ({len(gdf)})")
        elif kind == "rivers":
            major = gdf[gdf["scalerank"].astype(float) <= 4]
            minor = gdf[gdf["scalerank"].astype(float) > 4]
            n = 0
            if len(major):
                added.append(_context_layer(plan, "context-rivers-major", kind, major, e, geom_kind_="line", stroke=t["water"], width_mm=0.3, stroke_opacity=op, title="Rivers"))
                n += len(major)
            if len(minor):
                added.append(_context_layer(plan, "context-rivers", kind, minor, e, geom_kind_="line", stroke=t["water"], width_mm=0.18, stroke_opacity=op, title="Rivers"))
                n += len(minor)
            what.append(f"rivers ({n})")
        elif kind == "roads":
            ty = gdf["type"].astype(str)
            rd = map_look.params(plan.look)["roads"]
            if rd:      # 0.17.0 auto: the brand's light neutrals, thinner than the thinnest data line, fainter still when the data is lines
                nr = t["neutral_ramp"]
                tier_f = 0.8 if tier["tier"] in ("regional", "continental", "world") else 1.0
                fade = map_look.params(plan.look)["roads_under_lines"] if any(l.kind == "line" and l.role != "context" for l in plan.layers) else 1.0
                styles = {k: (nr[min(i, len(nr) - 1)], round(w * tier_f, 3)) for k, (i, w) in rd.items()}
            else:
                styles = {"major": (t["frame"], 0.35), "secondary": (t["legend_frame"], 0.2), "ferry": (t["frame"], 0.15)}
                fade = 1.0
            groups = (("context-roads-major", gdf[ty.isin(["Major Highway", "Beltway", "Bypass"])], *styles["major"], None),
                      ("context-roads-secondary", gdf[ty.isin(["Secondary Highway", "Road", "Unknown"])], *styles["secondary"], None),
                      ("context-ferry", gdf[ty == "Ferry Route"], *styles["ferry"], "0.8 0.6"))
            n = 0
            for lid, g, col, w, dash in groups:
                if len(g):
                    added.append(_context_layer(plan, lid, kind, g, e, geom_kind_="line", stroke=col, width_mm=w, stroke_opacity=op * fade, dash=dash, title="Roads"))
                    n += len(g)
            what.append(f"roads ({n})")
    if not added:
        plan.notes.append(f"context pack '{preset}': nothing of it falls inside this map")
        return
    fit_context_weight(plan, added)
    plan.layers += added                     # after the data layers in the list (plan.layers[0] stays the first data layer); drawn first (draw_order)
    plan.notes.append(f"context pack '{preset}' at the {tier['tier']} tier (1:{den:,.0f}): " + ", ".join(what)
                      + (f"; opacity {op:g}" if op < 1 else ""))


CONTEXT_LINE_FLOOR_MM = 0.12                 # 1.13.0: a context line is never thinned below this


def fit_context_weight(plan: Plan, added: list) -> None:
    """1.13.0 (Day 12 issue 11): the context pack's lines are kept inside the weight budget MAP-CART-029 checks (0.6 times the thinnest data
    line), so the plugin's own context never trips its own check: a rate map with 1 pt transmission lines got 0.3 mm rivers beside them."""
    data_lines = [l for l in plan.layers if l.kind == "line" and l.role not in ("context", "contour")]
    if not data_lines or plan.look == "classic":
        return
    thin = min(min([float(x) for x in (l.symbol or {}).get("_widths") or []] or [float(l.stroke_w_mm or 0)]) for l in data_lines)
    if thin <= 0:
        return
    cap = max(CONTEXT_LINE_FLOOR_MM, 0.6 * thin)
    eased = [l for l in added if l.kind == "line" and float(l.stroke_w_mm or 0) > cap + 1e-9]
    for l in eased:
        l.stroke_w_mm = cap
    if eased:
        plan.notes.append(f"context lines ({', '.join(sorted({l.context_kind or l.id for l in eased}))}) thinned to {cap:.2f} mm, under the thinnest data line")


def _context_from_tiles(plan: Plan, cfg: dict, preset: str, kinds: list) -> None:
    """1.10.0: the context pack on a vector-tile ground comes from the same file as the land and the water, so nothing is drawn at two
    resolutions: the file's built-up land, river lines, roads (two classes, a third on large-scale maps) and ferry routes, in the styles the
    bundled pack uses. The preset still decides which kinds are drawn; lakes are already in the ground's water."""
    vg_ = plan.vector_ground
    e = vg_["entry"]
    if not kinds:
        if cfg["on"] and preset == "none":
            plan.notes.append("context pack: none")
        return
    t = plan.tokens
    op = max(0.0, min(1.0, cfg["opacity"]))
    over = map_look.params(plan.look)["context_over_surface"]
    if over < 1 and any(l.kind == "raster" and l.role == "raster" for l in plan.layers) and "opacity" not in (plan.spec.get("context") or {}):
        op *= over
    added: list[LayerPlan] = []
    what: list[str] = []
    if vg_.get("buildings") is not None and ("urban" in kinds or "roads" in kinds):      # 1.12.0: footprints on maps at 1:25,000 and larger
        added.append(_context_layer(plan, "context-buildings", "urban", vg_["buildings"], e, geom_kind_="polygon", fill=t["neutral_ramp"][min(2, len(t["neutral_ramp"]) - 1)],
                                    stroke="none", width_mm=0.0, fill_opacity=0.7 * op, title="Buildings"))
        what.append("buildings")
    if "urban" in kinds and vg_.get("urban") is not None:
        added.append(_context_layer(plan, "context-urban", "urban", vg_["urban"], e, geom_kind_="polygon", fill=t["neutral_ramp"][1], stroke="none",
                                    width_mm=0.0, fill_opacity=0.45 * op, title="Urban areas"))
        what.append("built-up land")
    if "rivers" in kinds and vg_.get("rivers") is not None:
        added.append(_context_layer(plan, "context-rivers", "rivers", vg_["rivers"], e, geom_kind_="line", stroke=t["water"], width_mm=0.22, stroke_opacity=op, title="Rivers"))
        what.append("rivers")
    if "roads" in kinds and (vg_.get("roads") or vg_.get("ferries") is not None):
        rd = map_look.params(plan.look)["roads"]
        if rd:
            nr = t["neutral_ramp"]
            fade = map_look.params(plan.look)["roads_under_lines"] if any(l.kind == "line" and l.role != "context" for l in plan.layers) else 1.0
            styles = {k: (nr[min(i, len(nr) - 1)], w) for k, (i, w) in rd.items()}
        else:
            styles = {"major": (t["frame"], 0.35), "secondary": (t["legend_frame"], 0.2), "ferry": (t["frame"], 0.15)}
            fade = 1.0
        minor = (styles["secondary"][0], round(styles["secondary"][1] * 0.6, 3))
        rows = (("context-roads-minor", (vg_.get("roads") or {}).get("minor"), *minor, None),
                ("context-roads-secondary", (vg_.get("roads") or {}).get("secondary"), *styles["secondary"], None),
                ("context-roads-major", (vg_.get("roads") or {}).get("major"), *styles["major"], None),
                ("context-ferry", vg_.get("ferries"), *styles["ferry"], "0.8 0.6"))
        n = 0
        for lid, gdf, colour, w, dash in rows:
            if gdf is not None and len(gdf):
                added.append(_context_layer(plan, lid, "roads", gdf, e, geom_kind_="line", stroke=colour, width_mm=w, stroke_opacity=op * fade, dash=dash, title="Roads"))
                n += 1
        if n:
            what.append("roads" + (" with the smaller roads" if (vg_.get("roads") or {}).get("minor") is not None else ""))
    if not added:
        plan.notes.append(f"context pack '{preset}': the basemap file holds none of it inside this map")
        return
    fit_context_weight(plan, added)
    plan.layers += added
    plan.notes.append(f"context pack '{preset}' from {vg_['file']}: " + ", ".join(what) + (f"; opacity {op:g}" if op < 1 else ""))


# ---------------------------------------------------------------------------------------------------------------- styling
UI_TOKENS = ("label", "label_halo", "background", "land", "water", "frame", "legend_bg", "legend_frame", "no_data")   # page and text colors, never data
AUTO_ROLES = ("lines", "points")      # a polygon layer that names no fill stays an outline over the land, so it never buries what is drawn on it


CONTEXT_LINE_NEUTRAL = 2                # 1.2.0: the neutral ramp step a context line layer with no colour is drawn in
CONTEXT_LINE_MM = 0.25


def _neutral(t: dict, i: int) -> str:
    ramp = t.get("neutral_ramp") or [t["frame"]]
    return ramp[min(i, len(ramp) - 1)]


OUTLINE_EDGE_MM = 0.4                    # 1.2.0: the edge of an outline polygon layer (role polygons with no fill)


DASHES = {"dashed": "1.2 0.8", "dotted": "0.2 0.6", "dash-dot": "1.2 0.6 0.2 0.6"}


def dash_array(d) -> str:
    """symbol.dash as an SVG dash list in mm: a name (dashed, dotted, dash-dot), a list of numbers, or a string of numbers (1.2.0 on polygons)."""
    if isinstance(d, (list, tuple)):
        return " ".join(f"{float(x):g}" for x in d)
    return DASHES.get(str(d).strip().lower(), str(d))


def _is_outline(ly: dict) -> bool:
    """1.2.0 (Days 5, 8, 9): a polygons layer with no fill (or fill: none) and no classes is an outline: drawn as its edge only, so it never
    buries the relief, the context or the data under it. 0.2.1 said so; until 1.2.0 the land colour was used as an opaque fill."""
    sym = ly.get("symbol") or {}
    f = sym.get("fill")
    return (f is None or str(f).lower() == "none") and not ly.get("classification") and not ly.get("value_field")


def _explicit_colors(ly: dict) -> list[str]:
    sym = ly.get("symbol") or {}
    return [sym[k] for k in ("color", "fill") if sym.get(k)]


def _colors_taken(plan: Plan) -> set:
    """Palette colors other layers already show: named colors, categorical class colors, and the accent a proportional layer uses."""
    import map_look
    t = plan.tokens
    used = set()
    for l in plan.layers:
        ly = l.spec
        for v in _explicit_colors(ly) + list((ly.get("classification") or {}).get("colors") or []):
            try:
                used.add(resolve_color(v, t).upper())
            except PlanError:
                pass
        c = ly.get("classification") or {}
        vf = ly.get("value_field")
        if c and cf.ALIASES.get(c.get("method", "quantile"), c.get("method")) == "categorical" and not c.get("colors") and vf and vf in l.gdf.columns:
            n = min(int(l.gdf[vf].nunique()), int(c.get("max_categories", cf.MAX_CATEGORIES)), len(t["categorical"]))
            used |= {x.upper() for x in map_look.categorical_pick(t, n)}
        if l.role == "proportional" and not _explicit_colors(ly):
            used.add(t["emphasis"].upper())
        if l.role == "raster" and (l.raster or {}).get("src") != "service":     # a raster's ramp: points over it must not share its hue (M12)
            ramp = str((ly.get("raster") or {}).get("ramp") or "sequential")
            used |= {x.upper() for x in t["categorical" if ramp == "categorical" else "diverging_ramp" if ramp == "diverging" else "sequential_ramp"]}
    return used


def assign_palette(plan: Plan) -> None:
    """Every line or point layer that names no color gets the next brand palette color no other layer shows, so two layers never share a
    color by accident and the whole categorical palette is used before any color repeats. Layers that name a color keep it; classified,
    proportional, polygon and context layers are not touched. The choice is written into the layer's symbol and noted."""
    t = plan.tokens
    used = _colors_taken(plan)
    palette = list(t["categorical"])
    order = list(t.get("categorical_order") or range(len(palette)))     # 0.17.0 auto: near-twin colours wait their turn (map_look)
    k = 0
    for l in plan.layers:
        ly = l.spec
        outline = (l.role == "polygons" and l.kind == "polygon" and not l.wedge_info and _is_outline(ly) and plan.look != "classic")
        if (l.role not in AUTO_ROLES and not outline) or ly.get("classification") or (_explicit_colors(ly) and not outline):
            continue
        if outline and (ly.get("symbol") or {}).get("stroke"):
            continue
        free = [i for i in order if palette[i].upper() not in used]
        i = free[0] if free else order[k % len(order)]
        k += 1
        used.add(palette[i].upper())
        if outline:                                   # 1.2.0: an outline takes its edge colour from the palette
            ly["symbol"] = dict(ly.get("symbol") or {}, stroke=f"@brand.categorical.{i}")
            plan.notes.append(f"layer '{l.id}' is an outline (no fill), so its edge takes brand palette color categorical.{i} ({palette[i]})")
            continue
        ly["symbol"] = dict(ly.get("symbol") or {}, color=f"@brand.categorical.{i}")
        plan.notes.append(f"layer '{l.id}' named no color, so it takes brand palette color categorical.{i} ({palette[i]})")


def titled(ly: dict, l: LayerPlan) -> dict:
    """The layer spec as the legend reads it, with the layer's (possibly unit-suffixed) title in both places the legend looks."""
    out = ly | {"title": l.title}
    if l.title != lg._layer_title(ly):
        out["legend"] = {**(ly.get("legend") or {}), "title": l.title}
    return out


def normalize_layer(plan: Plan, l: LayerPlan, vf: str, unit: str):
    """`classification.normalize: area_km2` (area_mi2, area_m2, area_ha, area_acre): the value field divided by each feature's geodesic area, in
    place of a denominator column. The quotient becomes a column of its own (`<field>_per_<unit>`) that the classes, the renderer, the web page
    and the cartographic lints all read, so MAP-CART-001 sees a density and not the count; the legend title carries the unit ("per km²") and the
    methods page records the division. Returns (values, the new value field)."""
    try:
        vals, info = cf.normalize_by_area(l.gdf, vf, unit)
    except cf.ClassifyError as ex:
        raise PlanError(f"layer '{l.id}': {ex}", gc.EXIT_USAGE)
    if not np.isfinite(vals).any():
        raise PlanError(f"layer '{l.id}': '{vf}' divided by area leaves no numbers (no feature has a measurable area)", gc.EXIT_USAGE)
    new_vf = f"{vf}_per_{info['unit']}"
    l.gdf = l.gdf.copy()
    l.gdf[new_vf] = vals
    l.value_field = new_vf
    suffix = info["suffix"]
    if suffix.lower() not in l.title.lower():
        l.title = f"{l.title}, {suffix}"
    note = f"{info['note']} ({new_vf}); the legend reads {suffix}"
    l.notes.append(note)
    l.transformations.append(info["note"])
    plan.notes.append(f"layer '{l.id}': {note}")
    return vals, new_vf


def centre_on_midpoint(plan: Plan, l: LayerPlan, res, vals, mid, fmt, c: dict):
    """A diverging palette turns on a midpoint (`classification.midpoint: auto` or a number): the class break nearest the midpoint is moved onto
    it, so no class holds values from both sides, and a note says which midpoint was used. Edges handed down by an atlas, manual breaks and a
    midpoint outside the data are left as they are (noted)."""
    if mid is None:
        return res
    mid = float(mid)
    x = np.asarray(vals, dtype="float64")
    x = x[np.isfinite(x)]
    how = f"{mid:g}"
    if isinstance(c.get("midpoint"), str):          # auto: say how it was chosen
        how += " (midpoint: auto; the values run below and above zero)" if bool(x.min() < 0 < x.max()) else " (midpoint: auto; the median, since the values lie on one side of zero)"
    edges = [float(e) for e in res.edges]
    note = f"diverging palette centred on midpoint {how}"
    if res.fixed or res.method == "manual" or len(edges) < 3:
        note += "; the class breaks were kept as given"
    elif not (x.min() < mid < x.max()):
        note += f"; every value lies on one side of it (the data runs {x.min():g} to {x.max():g})"
    elif mid in edges:
        note += ", which is already a class break"
    else:
        interior = edges[1:-1]
        i = int(np.argmin([abs(e - mid) for e in interior]))
        moved = interior[i]
        interior[i] = mid
        new_edges = cf._dedupe(sorted([edges[0]] + interior + [edges[-1]]))
        try:
            res2 = cf.classify_fixed(vals, new_edges, int(res.decimals), fmt, bool(res.integer_data), method=res.method)
        except cf.ClassifyError:
            return res
        res2.fixed = False
        res2.notes = list(res.notes) + [f"the class break at {moved:g} was moved to the midpoint {mid:g}"]
        res = res2
        note += f" (the break at {moved:g} was moved to it)"
    l.notes.append(note)
    plan.notes.append(f"layer '{l.id}': {note}")
    return res


def point_size(plan: Plan, l: LayerPlan, lk: dict) -> float | None:
    """A point layer's symbol diameter in mm under the auto look: symbol.size (points) when given, else by the layer's feature count, one size
    smaller when more than POINT_OVERLAP_SHARE of the symbols would touch a neighbour at the sheet scale (noted). None under classic: the renderer
    and the legend keep their 0.16 arithmetic."""
    sym = l.symbol
    if plan.look == "classic":
        return None
    if "size" in sym:
        return float(sym["size"]) * 0.3528
    d = map_look.point_size_mm(plan.look, len(l.gdf))
    if d is None:
        return None
    try:
        x0, y0, x1, y1 = plan.extent
        m = plan.tpl.get("map") or {}
        mm_per_unit = min(float(m.get("w") or 0) / max(x1 - x0, 1e-9), float(m.get("h") or 0) / max(y1 - y0, 1e-9))
        pts = l.gdf.geometry.representative_point() if l.kind != "point" else l.gdf.geometry
        xy = np.column_stack([pts.x.to_numpy(), pts.y.to_numpy()]) * mm_per_unit if mm_per_unit > 0 else None
    except Exception:  # noqa: BLE001 - a multipoint layer or an unusual template: the count rule alone
        xy = None
    if xy is not None and len(xy) > 1 and np.isfinite(xy).all():
        share = map_look.overlap_share(xy, d)
        if share > map_look.POINT_OVERLAP_SHARE:
            smaller = map_look.smaller_point(plan.look, d)
            if smaller < d:
                l.notes.append(f"points drawn at {smaller:g} mm, one size down from {d:g} mm: {share:.0%} of them would touch a neighbour")
                d = smaller
                share = map_look.overlap_share(xy, d)
                if "opacity" not in sym and getattr(l, "group_opacity", 1.0) >= 1:
                    l.group_opacity = map_look.POINT_OVERLAP_OPACITY      # 0.22.0: the stepped-down points as one see-through group
        sym["_overlap"] = round(float(share), 4)          # what MAP-CART-029 reads
    return d


def style_layer(plan: Plan, l: LayerPlan) -> None:
    t, ly = plan.tokens, l.spec
    sym = dict(ly.get("symbol") or {})
    l.symbol = sym
    if "color" in sym:
        sym["_color"] = resolve_color(sym["color"], t, f"layer '{l.id}' symbol color")
    if "fill" in sym and str(sym["fill"]).lower() != "none":
        sym["_fill"] = resolve_color(sym["fill"], t, f"layer '{l.id}' fill")
    if "stroke" in sym:
        sym["_stroke"] = resolve_color(sym["stroke"], t, f"layer '{l.id}' stroke")
    vf = l.value_field
    if vf and vf not in l.gdf.columns:
        raise PlanError(f"layer '{l.id}': value_field '{vf}' is not a column; columns are: "
                        f"{', '.join(c for c in l.gdf.columns if c != l.gdf.geometry.name)}", gc.EXIT_USAGE)
    if l.role == "choropleth" and ly.get("bivariate"):        # two fields at once on a 3 x 3 grid of brand-derived colours (bivariate.py, M15)
        import bivariate
        bivariate.style(plan, l)
        vf = None
    if l.role in (*AREA_ROLES, "points", "polygons", "lines") and vf and ly.get("classification"):
        c = ly["classification"]
        method = cf.ALIASES.get(c.get("method", "quantile"), c.get("method", "quantile"))
        fmt = cf.number_format(c.get("format") or ly.get("legend", {}).get("format"))
        if method == "categorical":
            res = cf.classify(l.gdf[vf].tolist(), "categorical", order=c.get("order"), max_categories=int(c.get("max_categories", cf.MAX_CATEGORIES)),
                              keep_absent=bool(c.get("keep_absent")))
            kind = "categorical"
            if c.get("labels"):                      # display names for the categories, in the order of `order`
                if len(c["labels"]) != res.k:
                    raise PlanError(f"layer '{l.id}': classification.labels has {len(c['labels'])} entries but there are {res.k} categories", gc.EXIT_USAGE)
                res.labels = [str(x) for x in c["labels"]]
        else:
            vals = l.gdf[vf].to_numpy(dtype="float64", na_value=np.nan)
            if not np.isfinite(vals).any():
                raise PlanError(f"layer '{l.id}': '{vf}' holds no numbers", gc.EXIT_USAGE)
            if c.get("normalize"):            # value / geodesic area, in place of a denominator column (M10)
                vals, vf = normalize_layer(plan, l, vf, str(c["normalize"]))
            if c.get("edges"):                # an atlas hands every map the same edges (MAP-CART-014)
                try:
                    res = cf.classify_fixed(vals, c["edges"], int(c.get("decimals", 0)), fmt, bool(c.get("integer_data")), method=f"{method.replace('_', ' ')}, edges shared across the atlas")
                except cf.ClassifyError as ex:
                    raise PlanError(f"layer '{l.id}': {ex}", gc.EXIT_USAGE)
            else:
                res = cf.classify(vals, method, int(c.get("classes", 5)), c.get("breaks"), fmt, rounding=c.get("rounding", cf.DEFAULT_ROUNDING))
            mid_spec = c.get("midpoint")
            kind = cf.choose_kind(method, mid_spec, vals if mid_spec is not None else None)
            if kind == "diverging":
                res = centre_on_midpoint(plan, l, res, vals, cf.diverging_midpoint(vals, mid_spec), fmt, c)
        pal = ly.get("palette") or {}
        pk = {"brand_sequential": "sequential", "brand_diverging": "diverging", "brand_categorical": "categorical", "brand_neutral": "neutral"}.get(pal.get("name"), kind)
        if res.kind == "categorical":
            pk = "categorical"
        anchor = resolve_color(pal["anchor"], t, f"layer '{l.id}' palette anchor") if pal.get("anchor") else None
        try:
            colors = cf.palette(t, pk, res.k, bool(pal.get("reverse")), anchor=anchor)["colors"]
        except cf.ClassifyError as ex:
            raise PlanError(f"layer '{l.id}': {ex}", gc.EXIT_USAGE)
        if res.kind == "categorical" and c.get("colors"):
            cc = list(c["colors"])
            if len(cc) != res.k:
                raise PlanError(f"layer '{l.id}': classification.colors has {len(cc)} entries but there are {res.k} categories ({', '.join(res.labels)})", gc.EXIT_USAGE)
            colors = [resolve_color(x, t, f"layer '{l.id}' class color") for x in cc]
            pk = "status"
        integer_style = bool(getattr(res, "integer_data", False)) and int(getattr(res, "decimals", 0) or 0) == 0 and c.get("label_style") != "continuous"
        if res.kind != "categorical" and plan.look == "classic":
            res.labels = cf.classic_labels(res.labels, integer_style)       # 1.2.0: the classic look keeps "a to under b"
        elif res.kind != "categorical" and not integer_style and res.k > 1 and cf.HALF_OPEN_NOTE not in l.notes:
            l.notes.append(cf.HALF_OPEN_NOTE)
        if res.kind != "categorical" and c.get("labels"):
            # 1.2.0 (Day 9 issue 11): numeric classes take their own labels too ("40 – 83" where the method wrote another form), one per class
            if len(c["labels"]) != res.k:
                raise PlanError(f"layer '{l.id}': classification.labels has {len(c['labels'])} entries but there are {res.k} classes "
                                f"({'; '.join(res.labels)}) (MAP-SPEC-006)", gc.EXIT_USAGE)
            res.labels = [str(x) for x in c["labels"]]
        if res.kind == "categorical" and c.get("shapes"):
            sh = list(c["shapes"])
            if len(sh) != res.k or any(x not in lg.SHAPES for x in sh):
                raise PlanError(f"layer '{l.id}': classification.shapes needs one of {', '.join(lg.SHAPES)} for each of the {res.k} categories", gc.EXIT_USAGE)
            if l.kind != "point":
                raise PlanError(f"layer '{l.id}': classification.shapes is for point layers", gc.EXIT_USAGE)
            l.class_shapes = sh
        l.cls, l.colors, l.palette_kind = res, colors, pk
        plan.palette_reports[l.id] = cf.palette_report(colors, t["background"])
        l.notes += res.notes
        if l.class_shapes:
            cats = [{"label": lab, "fill": col, "shape": shp} for lab, col, shp in zip(res.labels, colors, l.class_shapes, strict=True)]
            if res.nodata:                       # a symbol drawn with no class must still be in the legend (MAP-CART-005)
                cats.append({"label": lg.NO_DATA_LABEL, "fill": t["no_data"], "shape": str(l.symbol.get("shape") or "circle"), "nodata": True})
            l.legend_block = lg.status_block(titled(ly, l), t, cats)
        else:
            swatch = (l.aggregation or {}).get("shape") if l.role in ("hexbin", "grid") else None
            l.legend_block = lg.class_block(titled(ly, l), res, colors, t, swatch=swatch)
    import map_look
    lk = map_look.params(plan.look)
    if l.kind == "polygon":
        outline = l.role == "polygons" and not l.wedge_info and l.cls is None and _is_outline(ly) and plan.look != "classic"
        if l.role == "context" and str(sym.get("fill", "")).lower() == "none":
            outline = True
        l.fill = None if outline else (sym.get("_fill") or (t["land"] if l.role in ("polygons", "context") else None))
        l.stroke = sym.get("_stroke") or (t["emphasis"] if outline else t["background"] if l.role in AREA_ROLES else t["frame"])
        l.stroke_w_mm = (float(sym["stroke_width"]) * 0.3528 if "stroke_width" in sym else
                         OUTLINE_EDGE_MM if outline else 0.12 if l.role in (*AREA_ROLES, "context") else 0.2)
        l.fill_opacity = float(sym.get("opacity", 1.0))
        l.outline = outline
        if "stroke_opacity" in sym:                       # 1.2.0 (Day 8 issue 4): the edge's own strength
            l.stroke_opacity = float(sym["stroke_opacity"])
        l.stroke_named = "_stroke" in sym and "stroke" in (ly.get("symbol") or {})
        if l.wedge_info and lk["wedge_stroke_mm"]:        # 0.17.0: sector wedges as one see-through group with a light edge, overlaps not darkened
            if "_stroke" not in sym:
                l.stroke = t["background"]
            if "stroke_width" not in sym:
                l.stroke_w_mm = lk["wedge_stroke_mm"]
            if "opacity" not in sym:
                l.group_opacity = lk["wedge_opacity"]
        if (l.role == "polygons" and plan.look != "classic" and "opacity" not in sym and not l.wedge_info and not outline
                and (l.cls is not None or "_fill" in sym)):
            # 0.22.0 (visual styling spec 3.1): an area layer that is not a choropleth lets the context show: the fill at 40 percent, the edge
            # in the fill's own colour at full strength, so the edge carries the shape
            l.fill_opacity = map_look.POLYGON_FILL_OPACITY
            l.edge_in_fill = True
            if l.cls is None and l.fill and "_stroke" not in sym:
                l.stroke = l.fill                                   # one colour: the edge is the fill's (the web page reads it too)
            elif "_stroke" in sym:
                l.edge_in_fill = False                              # 1.2.0 (Day 5 issue 2): an edge the spec names is drawn as named
            if "stroke_width" not in sym:
                l.stroke_w_mm = map_look.POLYGON_EDGE_MM
            sym["opacity"] = map_look.POLYGON_FILL_OPACITY          # what the legend swatch reads
            if l.legend_block and l.legend_block.get("items"):
                for it in l.legend_block["items"]:
                    it["opacity"] = map_look.POLYGON_FILL_OPACITY
                    it["stroke"] = it.get("fill") or it.get("stroke")
        if l.role == "polygons" and l.cls is None:
            l.legend_block = lg.polygons_block(ly | {"title": l.title, "symbol": sym}, t)
            if getattr(l, "edge_in_fill", False):
                l.legend_block["items"][0]["stroke"] = l.fill
            it0 = l.legend_block["items"][0]
            if outline:                                   # 1.2.0 (Day 9 issue 7): an outline's swatch is an outline
                it0.update(fill=l.stroke, stroke=l.stroke, outline=True, width_mm=l.stroke_w_mm)       # drawn unfilled (legend_draw reads outline)
            if sym.get("dash"):
                it0["dash"] = dash_array(sym["dash"])
            if l.stroke_opacity < 1:
                it0["stroke_opacity"] = l.stroke_opacity
    elif l.kind == "line":
        ctx_line = l.role == "context" and plan.look != "classic"
        # 1.2.0 (Day 10 issue 8): a context line layer with no colour is a quiet mid neutral, never the emphasis colour
        l.stroke = sym.get("_color") or (_neutral(t, CONTEXT_LINE_NEUTRAL) if ctx_line else t["emphasis"])
        l.stroke_w_mm = float(sym["width"]) * 0.3528 if "width" in sym else (CONTEXT_LINE_MM if ctx_line else lk["line_width_mm"])
        sym["_width_mm"] = l.stroke_w_mm
        if lk["casing_extra_mm"] and l.role not in ("context", "contour") and sym.get("casing", True) is not False:
            l.casing = {"color": t[lk["casing_color"]], "extra_mm": lk["casing_extra_mm"], "opacity": lk["casing_opacity"]}
        if l.legend_block is None and l.role != "context":
            l.legend_block = lg.lines_block(ly | {"title": l.title, "symbol": sym}, t)
        elif l.legend_block and l.legend_block["kind"] in ("classes", "categories") and (ly.get("legend") or {}).get("swatch") == "line":
            # classed lines keyed as line samples (line of sight: clear and obstructed), not as boxes (M12)
            l.legend_block = {**l.legend_block, "kind": "lines", "items": [{"label": it["label"], "stroke": it["fill"], "width_mm": max(0.5, l.stroke_w_mm)}
                                                                           for it in l.legend_block["items"]]}
    else:
        l.fill = sym.get("_color") or t["emphasis"]
        l.stroke = t["background"]
        sym["_stroke_mm"] = lk["proportional_stroke_mm"] if l.role == "proportional" else lk["point_stroke_mm"]
        if l.role != "proportional":
            d_ = point_size(plan, l, lk)
            if d_:
                sym["_size_mm"] = d_
        if l.role == "proportional":
            if not vf:
                raise PlanError(f"layer '{l.id}': proportional symbols need value_field", gc.EXIT_USAGE)
        elif str(sym.get("shape", "")).lower() == "none":
            # 1.2.0 (Day 9 issue 8): a label-only point layer: no symbol, no legend entry; its labels are placed as usual
            l.legend_block = None
            sym["_size_mm"] = 0.0
        elif l.legend_block is None:
            l.legend_block = lg.points_block(ly | {"title": l.title, "symbol": sym}, t | {"emphasis": l.fill})
    if sym.get("parallel") and (l.kind != "line" or l.cls is None or getattr(l.cls, "kind", "categorical") != "categorical"):
        plan.findings.append({"id": "MAP-CART-023", "severity": "WARN", "layer": l.id, "detail": l.id,
                              "message": f"layer '{l.id}': symbol.parallel sets lines that share a path side by side, so it needs a lines layer "
                                         "classed by category (value_field with classification method categorical); drawn as they are"})
    if l.role == "hexbin" and l.cls is not None and plan.look != "classic" and l.palette_kind == "sequential" and "opacity" not in sym:
        # 0.20.0: value by alpha: the sparse cells fade toward the ground, the dense ones stand out; the legend swatches fade the same way
        l.class_opacity = map_look.class_alphas(l.cls.k)
        if l.legend_block and l.legend_block.get("items"):
            for it, a_ in zip(l.legend_block["items"], l.class_opacity, strict=True):
                if a_ < 1:
                    it["opacity"] = a_
    import symbology
    symbology.after_classes(plan, l)          # class patterns, graduated line widths, line swatches for classed lines, nested circles (M15)
    hl = ly.get("highlight")
    if hl and l.kind in ("polygon", "line", "point") and l.role != "context":
        m, what = highlight_mask(l, hl)
        if not m.any():
            plan.findings.append({"id": "MAP-CART-036", "severity": "WARN", "layer": l.id, "detail": dict(hl),
                                  "message": f"layer '{l.id}': highlight {what} matches no feature; the layer is drawn as usual"})
        elif m.all():
            plan.notes.append(f"layer '{l.id}': highlight {what} matches every feature; nothing is dimmed")
        else:
            l.highlight = m
            l.highlight_rest = highlight_rest(hl)
            plan.notes.append(f"layer '{l.id}': {int(m.sum())} of {len(m)} feature(s) highlighted ({what}); the rest "
                              + ("desaturated" if l.highlight_rest["dim"] else "faded") + f" at {l.highlight_rest['opacity']:.0%}")
    if l.role == "context":
        l.legend_block = None
    if (ly.get("legend") or {}).get("include") is False:
        l.legend_block = None
    cont = (ly.get("legend") or {}).get("continues")
    if cont and l.legend_block:                # this layer's key is drawn as more rows of the block above, with no heading (0.10.0)
        prev = [p for p in plan.layers[:plan.layers.index(l)] if p.legend_block]
        if not prev or prev[-1].id != cont:
            raise PlanError(f"layer '{l.id}': legend.continues names '{cont}', but the legend block above this one is "
                            f"{('layer ' + repr(prev[-1].id)) if prev else 'none'}; list '{l.id}' right after '{cont}'", gc.EXIT_USAGE)
        l.legend_block = {**l.legend_block, "title": None, "continues": cont}
    l.provenance = layer_provenance(l)


def highlight_mask(l: LayerPlan, hl: dict):
    """The features a `highlight:` keeps at full strength (0.22.0; 1.13.0 adds min, max and top). -> (mask, a short description).
    column with in: [values] (those values); min / max (numbers at or above / at or below, both for a range); top: N (the N largest)."""
    col = hl.get("column")
    cols = [c for c in l.gdf.columns if c != l.gdf.geometry.name]
    keys = [k for k in ("in", "min", "max", "top") if hl.get(k) is not None]
    if col not in l.gdf.columns or not keys:
        raise PlanError(f"layer '{l.id}': highlight needs column (one of {', '.join(cols)}) and in: [values], min:, max: or top:", gc.EXIT_USAGE)
    if "in" in keys:
        vals = hl["in"]
        if not isinstance(vals, list) or not vals:
            raise PlanError(f"layer '{l.id}': highlight in: needs a list of values", gc.EXIT_USAGE)
        return l.gdf[col].astype(str).isin([str(v) for v in vals]).to_numpy(), f"{col} in {vals}"
    import pandas as pd
    v = pd.to_numeric(l.gdf[col], errors="coerce").to_numpy(dtype="float64")
    if not np.isfinite(v).any():
        raise PlanError(f"layer '{l.id}': highlight min, max or top needs a numeric column; '{col}' holds no numbers", gc.EXIT_USAGE)
    if "top" in keys:
        n = int(hl["top"])
        if n < 1:
            raise PlanError(f"layer '{l.id}': highlight top: needs a whole number of at least 1", gc.EXIT_USAGE)
        ok = np.isfinite(v)
        order = np.argsort(-np.where(ok, v, -np.inf), kind="stable")
        m = np.zeros(len(v), dtype=bool)
        m[order[:n]] = True
        return m & ok, f"the {n} largest by {col}"
    m = np.isfinite(v)
    parts = []
    if hl.get("min") is not None:
        m &= v >= float(hl["min"])
        parts.append(f"{col} >= {hl['min']:g}" if isinstance(hl["min"], (int, float)) else f"{col} >= {hl['min']}")
    if hl.get("max") is not None:
        m &= v <= float(hl["max"])
        parts.append(f"{col} <= {hl['max']:g}" if isinstance(hl["max"], (int, float)) else f"{col} <= {hl['max']}")
    return m, " and ".join(parts)


def highlight_rest(hl: dict) -> dict:
    """How the rest of a highlighted layer is drawn: rest: dim (the default: desaturated and see-through) or fade (see-through, its own
    colours), at rest_opacity (default map_look.HIGHLIGHT_REST_OPACITY)."""
    import map_look
    op = hl.get("rest_opacity")
    op = map_look.HIGHLIGHT_REST_OPACITY if op is None else max(0.05, min(0.95, float(op)))
    return {"dim": str(hl.get("rest") or "dim") != "fade", "opacity": op}


def capture_step(cap: dict) -> str:
    n, hits = cap.get("received"), cap.get("hits_expected")
    count = f"{n} of {hits} features" if hits is not None else f"{n} features"
    when = str(cap.get("finished_at") or "")[:10]
    if cap.get("route") == "device-run":
        return (f"fetched on {when} from a client network service by an approved, read-only run on the person's computer: {count}; checked against its manifest ({cap.get('verification')}), not verified at the source; capture {cap.get('id')}"
                + ("; INCOMPLETE, accepted: " + str(cap["accepted_incomplete"])[:120] if cap.get("truncated") else ""))
    if cap.get("route") == "user-run":
        return (f"supplied by the user from a request kit on {when}: {count}; checked against its manifest ({cap.get('verification')}), not verified at the source; capture {cap.get('id')}"
                + ("; INCOMPLETE, accepted: " + str(cap["accepted_incomplete"])[:120] if cap.get("truncated") else ""))
    return f"fetched on {when} through the plugin's read-only service client: {count}, paged in stable order; capture {cap.get('id')}"


def capture_provenance(l: LayerPlan, p: dict) -> dict:
    cap = l.capture
    lic = cap.get("license") or {}
    srv = cap.get("server") or {}
    host = cap.get("host")
    user_run = cap.get("route") in ("user-run", "device-run")
    device_run = cap.get("route") == "device-run"
    title = srv.get("title") or lic.get("service_title") or l.title
    p["source_declared"] = True
    p.setdefault("source", f"{title} (client network service; address withheld)" if device_run else f"{title} (data supplied by the user)" if user_run else f"{title}, {host}" if host and host != "withheld" else str(title))
    p.setdefault("license", lic.get("profile_default") or "unknown")
    p.setdefault("retrieved_at", str(cap.get("finished_at") or "")[:10] or None)
    p.setdefault("vintage", str(cap.get("finished_at") or "")[:10] or None)
    if lic.get("copyright"):
        p.setdefault("attribution", lic["copyright"])
    p["capture_id"] = cap.get("id")
    p["capture_route"] = cap.get("route")
    p["attested"] = cap.get("attested")
    p["verification"] = cap.get("verification")
    p["declared"] = True
    return p


def layer_provenance(l: LayerPlan) -> dict:
    p = dict(l.spec.get("provenance") or {})
    e = l.entry
    if l.ops_provenance:                                    # one record per `ops:` step: op, params, rows in and out, the CRS it measured in (M11)
        p["ops"] = [dict(r) for r in l.ops_provenance]
    if l.aggregation:
        p["aggregation"] = dict(l.aggregation["provenance"])
    if l.capture:
        if e:                                               # a library service layer (M13): the entry names the source, the capture says when it was fetched
            import map_sources
            map_sources.entry_provenance(p, e)
        return capture_provenance(l, p)
    if e:
        src = e.get("source") or {}
        p.setdefault("source", f"{src.get('publisher', '')}, {src.get('product', e.get('title', ''))}".strip(", "))
        p.setdefault("vintage", (e.get("vintage") or {}).get("current"))
        p.setdefault("license", (e.get("license") or {}).get("class"))
        p.setdefault("attribution", (e.get("license") or {}).get("attribution_text"))
        p["library_ref"] = e["id"]
        p["library_rev"] = e.get("rev")
    else:
        src = l.spec.get("source") or {}
        name = Path(src.get("path", "")).name if src.get("path") else l.id
        p["source_declared"] = bool(p.get("source"))
        p.setdefault("source", f"supplied data ({name})")
        p["declared"] = bool(l.spec.get("provenance"))
    return p


def prepared_tiles(spec: dict, use_tiles: bool, wgs, base: Path | None = None) -> tuple[dict | None, list[str]]:
    """The prepared tile composite for this map (written by basemap_render), if the map asks for a tile basemap and the composite covers it.
    The build never fetches a tile; without a usable composite it draws the bundled basemap and says why."""
    bm = spec.get("basemap") or {}
    prov = bm.get("provider", "none")
    if prov in ("none", "bundled"):
        return None, []
    if not use_tiles:
        return None, []                                  # a web page loads its own tiles (webmap + tile_registry)
    if vector_source(spec, base) is not None:
        return None, []                                  # 1.10.0: a vector tile file is read as the ground itself (load_vector_ground), in any projection
    crs_req = (spec.get("crs") or {}).get("display")
    own = bool(crs_req) and crs_req not in ("auto", "EPSG:3857")     # 1.11.0: the composite is warped to the map's own projection
    if own:
        try:
            import rasterio  # noqa: F401
        except ImportError:
            return None, [f"basemap provider '{prov}': warping tiles to {crs_req} needs the rasterio package; drawing the bundled basemap instead"]
    import basemap_render as br
    rec = br.load_prepared(bm.get("prepared_as") or spec.get("id", ""))     # 1.12.0: an atlas page reads the composite its extent shares
    hint = f"run basemap_render.py render --spec <map>.yaml to prepare the '{prov}' tiles"
    if rec is None:
        return None, [f"basemap provider '{prov}' has no prepared tiles yet ({hint}); drawing the bundled basemap instead"]
    if "error" in rec:
        return None, [f"basemap provider '{prov}': {rec['error']}; drawing the bundled basemap instead"]
    if rec.get("provider") != prov:
        return None, [f"the prepared tiles are from '{rec.get('provider')}', not '{prov}' ({hint}); drawing the bundled basemap instead"]
    if (bm.get("style") and rec.get("style") and bm["style"] != rec["style"]):
        return None, [f"the prepared tiles use style '{rec['style']}', not '{bm['style']}' ({hint}); drawing the bundled basemap instead"]
    cb = rec["bbox_wgs84"]
    if not (cb[0] <= wgs[0] and cb[1] <= wgs[1] and cb[2] >= wgs[2] and cb[3] >= wgs[3]):
        return None, [f"the prepared tiles do not cover this map's extent any more ({hint}); drawing the bundled basemap instead"]
    return rec, []


def vector_source(spec: dict, base: Path | None):
    """The person's own vector tile file named by basemap.source (pmtiles:FILE, or mbtiles:FILE when its tiles are vector tiles), or None.
    A file that is named but missing is returned too, so the build can say so."""
    bm = spec.get("basemap") or {}
    src = str(bm.get("source") or "")
    if bm.get("provider", "none") in ("none", "bundled") or ":" not in src:
        return None
    kind, name = src.split(":", 1)
    if kind not in ("pmtiles", "mbtiles"):
        return None
    f = Path(name).expanduser()
    if not f.is_absolute() and base is not None:
        f = Path(base) / f
    if kind == "mbtiles":
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
            try:
                fmt = con.execute("select value from metadata where name = 'format'").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if not fmt or str(fmt[0]).lower() != "pbf":
            return None                                   # picture tiles: a tile basemap (basemap_render)
    return f


def _wgs84_of_box(plan: Plan, b):
    from pyproj import Transformer
    t = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    X, Y = np.meshgrid(np.linspace(b[0], b[2], 12), np.linspace(b[1], b[3], 12))
    lon, lat = t.transform(X.ravel(), Y.ravel())
    ok = np.isfinite(lon) & np.isfinite(lat)
    if not ok.any():
        return _wgs84_extent(plan)
    return (float(lon[ok].min()), float(lat[ok].min()), float(lon[ok].max()), float(lat[ok].max()))


def load_vector_ground(plan: Plan, path: Path) -> bool:
    """1.10.0: the ground from the person's own vector tile file (vector_ground): land, water and border lines into plan.basemap, and the
    file's roads, rivers and built-up land kept for the context pack (add_context), so one file is the whole ground at one resolution. The
    colours are the brand's map roles. Returns False, with a note that says why, when the file cannot be the ground of this map; the
    bundled ground is drawn then."""
    import vector_ground as vg
    import geopandas as gpd
    import ref_library as rl
    name = Path(path).name
    den = scale_denominator(plan)
    rect = lo.rect_of(plan.tpl["map"])
    fr = lo.Frame.fit(rect, plan.extent, float((plan.spec.get("extent") or {}).get("padding_pct", 5)))
    seen = _wgs84_of_box(plan, fr.visible)
    window = _wgs84_of_box(plan, plan_visible_pad(plan))
    partial = None
    try:
        info = vg.inspect(path)
        b = info["bounds"]
        eps = 1e-6
        if not (b[0] <= seen[0] + eps and b[1] <= seen[1] + eps and b[2] >= seen[2] - eps and b[3] >= seen[3] - eps):
            # 1.12.0: an extract that covers part of the window is still drawn: the file inside its own box, the bundled ground past its edge.
            # Under a fifth of the window it is not worth a seam; the bundled ground is drawn alone.
            ix = (max(b[0], seen[0]), max(b[1], seen[1]), min(b[2], seen[2]), min(b[3], seen[3]))
            share = (max(0.0, ix[2] - ix[0]) * max(0.0, ix[3] - ix[1])) / max(1e-12, (seen[2] - seen[0]) * (seen[3] - seen[1]))
            if share < 0.2:
                raise vg.VectorGroundError(f"{name} covers {b[0]:.2f},{b[1]:.2f} to {b[2]:.2f},{b[3]:.2f} (lon/lat), which is not the whole map window "
                                           f"({seen[0]:.2f},{seen[1]:.2f} to {seen[2]:.2f},{seen[3]:.2f}) nor a fifth of it; use a larger extract or a narrower map")
            partial = {"share": share, "box": [float(v) for v in b]}
            window = (max(b[0], window[0]), max(b[1], window[1]), min(b[2], window[2]), min(b[3], window[3]))
        g = vg.read(path, window, den)
    except vg.VectorGroundError as ex:
        plan.notes.append(f"basemap file: {ex}; drawing the bundled basemap instead")
        plan.findings.append({"id": "MAP-CART-037", "severity": "WARN", "message": f"The vector basemap file is not drawn: {ex}. The bundled ground is drawn instead.",
                              "detail": {"file": name}})
        return False
    if g["land"] is None:
        plan.notes.append(f"basemap file {name}: no land in the map window; drawing the bundled basemap instead")
        return False
    e = next((x for x in rl.all_entries("all") if x.get("id") == "ref.basemap-provider.pmtiles-user"), None)
    e = dict(e) if e else {"id": "user.vector-tiles", "title": "vector tiles supplied by the user", "rev": None, "license": {"class": "user-supplied"}}
    lic = dict(e.get("license") or {})
    if g.get("attribution"):
        lic["attribution_text"] = g["attribution"]                # the file's own credit, read from its metadata
    e["license"] = lic
    e["source"] = {"product": f"vector tiles ({name})"}
    e["title"] = f"Vector tiles: {name}"

    def gdf_of(geom, **cols):
        s = gpd.GeoSeries([geom], crs=g["crs"]).to_crs(plan.crs)
        return gpd.GeoDataFrame({**{k: [v] for k, v in cols.items()}, "geometry": list(s.values)}, crs=plan.crs)

    if partial:
        # the bundled ground first, cut away inside the file's box; the file's own ground is drawn over that hole
        import shapely
        from shapely.geometry import box as _sbox
        hole = gpd.GeoSeries([shapely.segmentize(_sbox(*partial["box"]), 0.05)], crs="EPSG:4326").to_crs(plan.crs).iloc[0]
        _load_basemap(plan, None, True, None)
        for item in plan.basemap:
            gd = item.get("gdf")
            if gd is not None and len(gd):
                gd = gd.copy()
                gd["geometry"] = shapely.make_valid(shapely.difference(np.asarray(gd.geometry.values, dtype=object), hole))
                item["gdf"] = gd[~gd.geometry.is_empty]
        plan.findings.append({"id": "MAP-CART-037", "severity": "INFO", "message": f"The vector basemap file covers {partial['share']:.0%} of the map window; "
                              "past its edge the bundled ground is drawn, so the detail changes along that edge.", "detail": {"file": name, "share": round(partial["share"], 3)}})
    plan.basemap.append({"kind": "land", "gdf": gdf_of(g["land"]), "entry": e})
    for cls, geom in (g["boundaries"] or {}).items():
        plan.basemap.append({"kind": "boundary", "class": cls, "gdf": gdf_of(geom), "entry": e})
    if g["water"] is not None:
        plan.basemap.append({"kind": "lake", "gdf": gdf_of(g["water"]), "entry": e})
    plan.vector_ground = {"entry": e, "file": name, "schema": g["schema"], "zoom": g["zoom"],
                          "rivers": gdf_of(g["rivers"]) if g["rivers"] is not None else None,
                          "urban": gdf_of(g["urban"]) if g["urban"] is not None else None,
                          "ferries": gdf_of(g["ferries"]) if g["ferries"] is not None else None,
                          "roads": {k: gdf_of(v) for k, v in (g["roads"] or {}).items()},
                          "buildings": gdf_of(g["buildings"]) if g.get("buildings") is not None else None,
                          "places": g.get("places"),                                   # lon/lat points, the gazetteer's columns (draw_names)
                          "road_names": g["road_names"].to_crs(plan.crs) if g.get("road_names") is not None else None,
                          "partial": partial}
    res = f"vector tiles ({name}, zoom {g['zoom']})" + (" and the bundled ground past its edge" if partial else "")
    plan.crs_info["ground_resolution"] = {"land": res, "water": res}
    plan.crs_info["vector_ground"] = {"file": name, "schema": g["schema"], "zoom": g["zoom"], "format": g["format"]}
    extras = [w for w, k in (("its place names", "places"), ("its road names", "road_names"), ("its buildings", "buildings")) if g.get(k) is not None]
    plan.notes.append(f"ground: {name} ({g['schema']} schema), read at zoom {g['zoom']} for 1:{den:,.0f}, drawn in the brand's map colours"
                      + (f", with {', '.join(extras)}" if extras else "")
                      + (f"; it covers {partial['share']:.0%} of the window, the bundled ground is drawn past its edge" if partial else "")
                      + ("; " + "; ".join(g["notes"]) if g["notes"] else ""))
    return True


def load_basemap(plan: Plan, tiles_rec: dict | None = None, explained: bool = False, vector_file: Path | None = None) -> None:
    if _SESSION is None:
        return _load_basemap(plan, tiles_rec, explained, vector_file)
    import map_raster
    rect = lo.rect_of(plan.tpl["map"])
    key = json.dumps([plan.crs, [round(float(v), 4) for v in plan.extent], [rect.x0, rect.y0, rect.w, rect.h], plan.spec.get("basemap"), plan.spec.get("context"),
                      (plan.spec.get("elements") or {}).get("context"), (plan.spec.get("extent") or {}).get("padding_pct"),
                      ["points" if l.wedge_info else l.role for l in plan.layers], bool(map_raster.needs_ground(plan.spec)), (tiles_rec or {}).get("sha256"),
                      str(vector_file) if vector_file else None, bool(explained)], sort_keys=True, default=str)
    hit = _SESSION["ground"].get(key)
    if hit is None:
        n0, f0, c0 = len(plan.notes), len(plan.findings), dict(plan.crs_info)
        _load_basemap(plan, tiles_rec, explained, vector_file)
        _SESSION["ground"][key] = {"basemap": list(plan.basemap), "notes": plan.notes[n0:], "findings": plan.findings[f0:],
                                   "crs_info": {k: v for k, v in plan.crs_info.items() if k not in c0 or c0[k] is not v}, "vector_ground": plan.vector_ground}
        return
    _SESSION["hits"]["ground"] += 1
    plan.basemap += [dict(b) for b in hit["basemap"]]
    plan.notes += list(hit["notes"])
    plan.findings += [dict(f) for f in hit["findings"]]
    plan.crs_info.update(hit["crs_info"])
    plan.vector_ground = hit["vector_ground"]


def _load_basemap(plan: Plan, tiles_rec: dict | None = None, explained: bool = False, vector_file: Path | None = None) -> None:
    bm = (plan.spec.get("basemap") or {})
    prov = bm.get("provider", "none")
    if vector_file is not None and not tiles_rec:
        if load_vector_ground(plan, vector_file):
            return
        explained = True
    if prov == "none":
        # The context pack brings the ground with it (0.3.0): roads and city names over page-white, with the sea the same white as the
        # desert, read as nothing. A map that draws any context kind gets the bundled land and water under it; `context: {on: false}`
        # or `preset: none` keeps the page bare.
        cfg = context_config(plan.spec)
        roles = ["points" if l.wedge_info else l.role for l in plan.layers]
        _preset, kinds = context_kinds(cfg, roles, False)
        import map_raster
        relief = map_raster.needs_ground(plan.spec)              # relief needs land under it (M12)
        if not kinds and not relief:
            return
        plan.notes.append("ground: the bundled land and water are drawn under the " + ("context pack" if kinds else "relief") + " (basemap provider is none)")
    if tiles_rec:
        vis = lo.Frame.fit(lo.rect_of(plan.tpl["map"]), plan.extent, float((plan.spec.get("extent") or {}).get("padding_pct", 5))).visible
        bb = tiles_rec["bbox_3857"]
        vis_m = vis if plan.crs == "EPSG:3857" else box_to_crs(vis, plan.crs, "EPSG:3857")
        item = None
        why = "the prepared tiles do not cover the whole map window"
        if vis_m is not None and bb[0] <= vis_m[0] and bb[1] <= vis_m[1] and bb[2] >= vis_m[2] and bb[3] >= vis_m[3]:
            item = tiles_item(tiles_rec, plan)
            if plan.crs != "EPSG:3857":
                try:
                    warp_tiles(item, plan)
                except Exception as ex:  # noqa: BLE001 - rasterio's own message is the reason; the bundled ground is drawn
                    item, why = None, f"the tiles could not be warped to {crs_label(plan.crs)} ({type(ex).__name__}: {str(ex)[:120]})"
        if item is not None:
            plan.basemap.append(item)
            size = f" (labels about {tiles_rec['label_pt']:g} pt, {tiles_rec.get('paper_dpi')} dpi on paper)" if tiles_rec.get("label_pt") else ""
            warped = f", warped from Web Mercator to {crs_label(plan.crs)}" if item["image"].get("warped") else ""
            plan.notes.append(f"basemap: {tiles_rec.get('title') or tiles_rec['provider']} tiles, zoom {tiles_rec['zoom']}{size}{warped}, prepared {tiles_rec.get('retrieved_at')}")
            return
        plan.notes.append(f"{why}; drawing the bundled basemap instead (prepare them again)")
        plan.crs_info["tiles_unused"] = True
    elif prov != "bundled" and not explained:
        plan.notes.append(f"basemap provider '{prov}': tiles are loaded by the web page itself; the bundled basemap is the fallback")
    from shapely.geometry import box
    wgs = _wgs84_extent(plan)
    layers = []
    in_us = -180 <= wgs[0] and wgs[2] <= -60 and 15 <= wgs[1] and wgs[3] <= 72
    vis = box(*plan_visible_pad(plan))
    window = _lonlat_window(wgs)
    fine = scale_denominator(plan) < FINE_GROUND_MAX_DENOMINATOR
    if fine:
        # Finer than 1:10M the 110m countries draw a coast that is kilometres off. The frame is already water-coloured, so the ground is
        # one land polygon: a window well past the frame (8 degrees) with the 10m ocean cut out of it, built in lon/lat where the ocean
        # is exact, then projected; the country and state polygons are trimmed to that land so their coasts follow the same shoreline.
        try:
            ocean, oe = load_ref_layer(BASEMAP_REFS["ocean"])
            from shapely.geometry import box as _box
            import shapely as _sh
            import geopandas as gpd
            lw = _lonlat_window(wgs, 8.0)
            wbox = _box(max(-180.0, lw[0]), max(-89.0, lw[1]), min(180.0, lw[2]), min(89.0, lw[3]))
            ocean = ocean.to_crs("EPSG:4326") if ocean.crs is not None and not ocean.crs.equals("EPSG:4326") else ocean
            # cut each ocean polygon to the window first, then merge them: the same sea as merging the world's ocean and cutting it, 25
            # times faster (M16: the 10 m ocean has 370,000 vertices; the merge took 3 s of every regional map)
            sea_ll = _sh.make_valid(_sh.union_all(_sh.intersection(ocean.geometry.values, wbox)))
            land_ll = _sh.make_valid(wbox.difference(sea_ll))
            detail = choose_ground(plan, land_ll)                          # 1.5.0: the finest provider that covers the whole window
            if detail and detail.get("land_ll") is not None:
                land_ll = detail["land_ll"]
            land = gpd.GeoSeries([land_ll], crs="EPSG:4326").to_crs(plan.crs)
            land_geom = _sh.make_valid(land.iloc[0])
            if detail and detail.get("water") is not None and len(detail["water"]):
                # the land's edge follows TIGER's detailed shoreline where the water reaches inside the 1:500,000 coast (0.11.0)
                land_geom = _sh.make_valid(_sh.difference(land_geom, _sh.union_all(detail["water"].geometry.values)))
            plan.basemap.append({"kind": "land", "gdf": gpd.GeoDataFrame({"geometry": [land_geom]}, crs=plan.crs),
                                 "entry": (detail or {}).get("land_entry") or oe})
            layers.append(("country", BASEMAP_REFS["countries"]))
            if in_us:
                layers.append(("state", BASEMAP_REFS["states5m" if detail else "states"]))
            for kind, ref in layers:
                gdf, e = load_ref_layer(ref)
                gdf = window_reproject(gdf, window, plan.crs)
                gdf = gdf[gdf.geometry.intersects(vis)].copy() if len(gdf) else gdf
                if len(gdf):
                    gdf["geometry"] = _sh.make_valid(gdf.geometry.intersection(land_geom).values)
                    gdf = gdf[~gdf.geometry.is_empty]
                plan.basemap.append({"kind": kind, "gdf": gdf, "entry": e})
            if detail and detail.get("water") is not None:
                plan.basemap.append({"kind": "lake", "gdf": detail["water"], "entry": detail["water_entry"]})
                return
            lakes, le = load_ref_layer(BASEMAP_REFS["lakes10"])
            lakes = window_reproject(lakes, window, plan.crs)
            lakes = lakes[lakes.geometry.intersects(vis)].copy() if len(lakes) else lakes
            plan.basemap.append({"kind": "lake", "gdf": lakes, "entry": le})
            res = (detail or {}).get("resolution") or NE10                   # 1.5.0: the OSM simplified land with these lakes is one ground
            plan.crs_info["ground_resolution"] = {"land": res, "water": res}
            return
        except PlanError as ex:
            plan.notes.append(f"ground: the 10m ocean is not available ({ex}); drawing the 110m countries instead")
    layers.append(("country", BASEMAP_REFS["countries"]))
    if in_us:
        layers.append(("state", BASEMAP_REFS["states"]))
    layers.append(("lake", BASEMAP_REFS["lakes"]))
    for kind, ref in layers:
        gdf, e = load_ref_layer(ref)
        gdf = window_reproject(gdf, window, plan.crs)
        gdf = gdf[gdf.geometry.intersects(vis)].copy() if len(gdf) else gdf
        plan.basemap.append({"kind": kind, "gdf": gdf, "entry": e})
    plan.crs_info["ground_resolution"] = {"land": NE110, "water": NE110}


def detail_window(plan: Plan) -> tuple:
    """The lon/lat box of the main map's visible frame plus a margin: what detailed ground covers (ground_detail prepare and the build use the same)."""
    from pyproj import Transformer
    x0, y0, x1, y1 = plan_visible_pad(plan)
    xs, ys = np.meshgrid(np.linspace(x0, x1, 12), np.linspace(y0, y1, 12))
    lon, lat = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True).transform(xs.ravel(), ys.ravel())
    return (round(float(np.min(lon)) - 0.02, 4), round(float(np.min(lat)) - 0.02, 4), round(float(np.max(lon)) + 0.02, 4), round(float(np.max(lat)) + 0.02, 4))


def choose_ground(plan: Plan, land_ne_ll):
    """1.5.0 (R3-GND): the ground provider for the map's window (data/ground-providers.yaml): the finest whose coverage holds the whole window,
    whose scales include the map's and whose files are cached. Records MAP-GND-001 (the choice and why), MAP-GND-002 (detailed ground finer
    than 1:1,500,000 is not cached: the build stays offline) and MAP-GND-004 (the pinned Overture release is past its review date). -> the
    detail dict the ground code draws ({land_ll, water, land_entry, water_entry, resolution}) or None for Natural Earth."""
    import ground_detail as gd
    den = scale_denominator(plan)
    bm = plan.spec.get("basemap") or {}
    if bm.get("ground_detail") is False:
        plan.findings.append({"id": "MAP-GND-001", "severity": "INFO", "message": "ground: Natural Earth, bundled (basemap ground_detail: false)",
                              "detail": {"provider": "bundled-ne"}})
        plan.crs_info["ground_provider"] = {"provider": "bundled-ne", "why": "asked (basemap ground_detail: false)"}
        return None
    win = detail_window(plan)
    try:
        sel = gd.choose(win, den, asked=bm.get("ground"))
    except Exception as ex:  # noqa: BLE001 - the bundled ground is always there
        plan.notes.append(f"ground: the providers could not be read ({type(ex).__name__}); drawing the bundled ground")
        return None
    pid = sel["provider"]
    plan.crs_info["ground_provider"] = {"provider": pid, "why": sel["why"], "skipped": sel["skipped"]}
    if den < gd.DETAIL_MAX_DENOMINATOR:
        plan.crs_info.setdefault("ground_detail", {"provider": pid, "missing": [m for k in sel["skipped"] for m in (k.get("missing") or [])][:12]})
    plan.findings.append({"id": "MAP-GND-001", "severity": "INFO", "message": f"ground: {pid}: {sel['why']}",
                          "detail": {"provider": pid, "skipped": [k["provider"] for k in sel["skipped"]]}})
    if den < gd.DETAIL_MAX_DENOMINATOR:
        for k in sel["skipped"]:
            if k.get("missing"):
                plan.findings.append({"id": "MAP-GND-002", "severity": "WARN",
                                      "message": (f"detailed ground ({k['provider']}) is not cached for this window; run ground_detail.py prepare --spec on this "
                                                  f"MapSpec once (the build stays offline), then build again; {pid} is drawn meanwhile"),
                                      "detail": {"provider": k["provider"], "missing": k["missing"][:12]}})
                plan.notes.append(f"ground: detailed land and water are not prepared for this window ({k['provider']}); run "
                                  "ground_detail.py prepare --spec on this MapSpec once, then build again")
                break
    if pid == "us-tiger":
        return ground_detail(plan, land_ne_ll)
    if pid == "bundled-ne":
        return None
    from shapely.geometry import box
    import shapely
    x0, y0, x1, y1 = plan_visible_pad(plan)
    rect = lo.rect_of(plan.tpl["map"])
    try:
        got = gd.load_overture(win, plan.crs, rect.w, (x1 - x0) * float(gd_units(plan.crs))) if pid == "global-overture" else gd.load_osm(win)
    except Exception as ex:  # noqa: BLE001 - an unreadable cache never stops a map
        plan.notes.append(f"ground: the {pid} cache could not be read ({type(ex).__name__}); drawing the bundled ground")
        return None
    if got.get("land_ll") is None:
        return None
    wbox = box(*win)
    e = got["entries"][0] if got.get("entries") else None
    if pid == "global-overture":
        import datetime as _dt
        vb = str(((e or {}).get("maintenance") or {}).get("verify_by") or "")
        try:
            if vb and _dt.date.fromisoformat(vb) < _dt.date.today():
                plan.findings.append({"id": "MAP-GND-004", "severity": "WARN",
                                      "message": f"the pinned Overture release {(e.get('delivery') or {}).get('release')} is past the entry's review date ({vb}); "
                                                 "refresh the library entry to a current release", "detail": {"verify_by": vb}})
        except ValueError:
            pass
    plan.notes.append(f"ground: {pid} land" + (" and water" if got.get("water") is not None else " with the Natural Earth 10m lakes") + " for the window (cached), "
                      "one source for both")
    plan.crs_info["ground_resolution"] = {"land": got["resolution"], "water": got["resolution"]}
    if pid == "global-overture":
        plan.crs_info.setdefault("ground_detail", {})["drawn"] = True
    return {"water": got.get("water"), "water_entry": e if got.get("water") is not None else None,
            "land_ll": shapely.make_valid(shapely.union(shapely.difference(land_ne_ll, wbox), got["land_ll"])),
            "land_entry": e, "resolution": got["resolution"]}


def ground_detail(plan: Plan, land_ne_ll):
    """Detailed ground for a US map finer than 1:1,500,000, from the library cache (ground_detail.py prepare fills it). -> dict or None.

    Inside the window the land is the Census 1:500,000 counties; outside it, beyond the frame, the Natural Earth land stays, so the seam is never seen.
    A window with land outside the US keeps the Natural Earth land throughout (the border country stays) and takes the TIGER water only."""
    import ground_detail as gd
    import shapely
    from shapely.geometry import box
    den = scale_denominator(plan)
    if den >= gd.DETAIL_MAX_DENOMINATOR or (plan.spec.get("basemap") or {}).get("ground_detail") is False:
        return None
    win = detail_window(plan)
    x0, y0, x1, y1 = plan_visible_pad(plan)
    rect = lo.rect_of(plan.tpl["map"])
    try:
        got = gd.load(win, plan.crs, den, rect.w, (x1 - x0) * float(gd_units(plan.crs)))
    except Exception as ex:  # noqa: BLE001 - an unreadable cache never stops a map: the bundled ground is drawn and the reason noted
        plan.notes.append(f"ground: the detailed land and water could not be read ({type(ex).__name__}); drawing the bundled ground")
        return None
    plan.crs_info["ground_detail"] = {"counties": len(got["counties"]), "missing": got["missing"]}
    if got["land_ll"] is None and got["water"] is None:
        plan.notes.append(f"ground: detailed land and water are not prepared for this window ({len(got['counties'])} counties); run "
                          "ground_detail.py prepare --spec on this MapSpec once, then build again")
        return None
    # Rule (0.12.0, MAP-CART-024): land and water come from one source at one resolution. Detailed ground is all or nothing: TIGER land with TIGER
    # water, or Natural Earth for both. Never TIGER water on Natural Earth land (a border window, or land files not cached).
    if got["land_ll"] is None or got["water"] is None or got["missing"]:
        why = ("detail file(s) are not cached (" + "; ".join(got["missing"][:3]) + "); run ground_detail.py prepare --spec again") if got["missing"] else \
              "the TIGER land or water is empty for this window"
        plan.notes.append(f"ground: detailed land and water are not drawn: {why}; Natural Earth land and water are drawn together instead")
        return None
    wbox = box(*win)
    states, _ = load_ref_layer(BASEMAP_REFS["states"])
    us = shapely.union_all(states.to_crs("EPSG:4326").geometry.values).buffer(0.02)
    ne_here = shapely.intersection(land_ne_ll, wbox)
    abroad = shapely.difference(ne_here, us).area / max(ne_here.area, 1e-12)
    if abroad > 0.005:
        plan.notes.append("ground: the window holds land outside the US, which TIGER does not cover, so Natural Earth land and water are drawn together "
                          "(one resolution for both)")
        return None
    out = {"water": got["water"], "water_entry": next((e for e in got["entries"] if e["id"] == gd.WATER_REF), None),
           "land_ll": shapely.make_valid(shapely.union(shapely.difference(land_ne_ll, wbox), got["land_ll"])),
           "land_entry": next((e for e in got["entries"] if e["id"] == gd.LAND_REF), None), "resolution": gd.RESOLUTION}
    plan.notes.append(f"ground: TIGER/Line land and water for {len(got['counties'])} counties (cached), one source for both, not the 1:10,000,000 Natural Earth coast")
    plan.crs_info["ground_detail"]["drawn"] = True
    plan.crs_info["ground_resolution"] = {"land": gd.RESOLUTION, "water": gd.RESOLUTION}
    return out


def gd_units(crs) -> float:
    """Metres per CRS unit (1 for a metric projection, 0.3048 for feet)."""
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    try:
        return float(c.axis_info[0].unit_conversion_factor or 1.0)
    except Exception:  # noqa: BLE001
        return 1.0


def box_to_crs(b, src: str, dst: str, n: int = 24):
    """The box (x0, y0, x1, y1) of a box of `src` in `dst`: its edges and inside are sampled, since a straight edge in one projection is a
    curve in another. None when a sample has no image there."""
    from pyproj import Transformer
    t = Transformer.from_crs(src, dst, always_xy=True)
    X, Y = np.meshgrid(np.linspace(b[0], b[2], n), np.linspace(b[1], b[3], n))
    x, y = t.transform(X.ravel(), Y.ravel())
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        return None
    return (float(x.min()), float(y.min()), float(x.max()), float(y.max()))


def warp_tiles(item: dict, plan: Plan) -> None:
    """1.11.0: a prepared composite (Web Mercator) warped to the map's own CRS for the map window: a PNG beside the composite, see-through
    where the composite has no pixels, named by the CRS and the window so a rebuild reads it again. The item's image then carries `bbox`
    (display CRS) and `warped`."""
    import hashlib
    import rasterio
    from PIL import Image
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject
    im = item["image"]
    src = Path(im["path"])
    win = plan_visible_pad(plan)
    key = hashlib.sha256(f"{im.get('sha256')}|{plan.crs}|{[round(v, 3) for v in win]}".encode()).hexdigest()[:16]
    out = src.with_name(f"composite-warped-{key}.png")
    sx0, sy0, sx1, sy1 = im["bbox_3857"]
    sw, sh = im["pixels"]
    # as many pixels across the window as the composite has across the same ground (never more than 6,000 a side)
    mw = box_to_crs(win, plan.crs, "EPSG:3857")
    share = min(1.0, (mw[2] - mw[0]) / (sx1 - sx0)) if mw else 1.0
    w = int(max(64, min(6000, round(sw * share))))
    h = int(max(64, min(6000, round(w * (win[3] - win[1]) / (win[2] - win[0])))))
    if not out.is_file():
        a = np.asarray(Image.open(src).convert("RGB"))
        dst = np.zeros((4, h, w), dtype=np.uint8)
        st = from_bounds(sx0, sy0, sx1, sy1, sw, sh)
        dt_ = from_bounds(win[0], win[1], win[2], win[3], w, h)
        for i in range(3):
            reproject(a[:, :, i], dst[i], src_transform=st, src_crs="EPSG:3857", dst_transform=dt_, dst_crs=plan.crs, resampling=Resampling.bilinear)
        reproject(np.full((sh, sw), 255, dtype=np.uint8), dst[3], src_transform=st, src_crs="EPSG:3857", dst_transform=dt_, dst_crs=plan.crs,
                  resampling=Resampling.nearest)
        Image.fromarray(np.moveaxis(dst, 0, 2), "RGBA").save(out, optimize=True)
    im["source_path"], im["path"] = str(src), str(out)
    im["bbox"] = [float(v) for v in win]
    im["warped"] = {"from": "EPSG:3857", "to": plan.crs, "pixels": [w, h], "resampling": "bilinear"}
    _ = rasterio


def tiles_item(rec: dict, plan: Plan) -> dict:
    """A plan.basemap item for a prepared composite. The entry carries the attribution that goes into the source note and the provenance."""
    import ref_library as rl
    e = None
    if rec.get("entry"):
        e = next((x for x in rl.all_entries("all") if x.get("id") == rec["entry"]), None)
    e = dict(e) if e else {"id": f"user.tiles.{rec.get('provider')}", "title": rec.get("title") or "tiles supplied by the user", "rev": None, "source": {}, "license": {"class": rec.get("license") or "user-supplied"}}
    lic = dict(e.get("license") or {})
    if rec.get("attribution"):
        lic["attribution_text"] = rec["attribution"]
    e["license"] = lic
    return {"kind": "tiles", "gdf": None, "entry": e, "image": {k: rec[k] for k in ("path", "bbox_3857", "pixels", "zoom", "sha256", "retrieved_at", "source", "tiles", "fetched", "from_cache", "terms_acknowledged", "customization") if k in rec}}


def _lonlat_window(wgs, pad_deg: float = 25.0):
    return (max(wgs[0] - pad_deg, -180.0), max(wgs[1] - pad_deg, -85.0), min(wgs[2] + pad_deg, 180.0), min(wgs[3] + pad_deg, 85.0))


def window_reproject(gdf, window, crs):
    """Clip lon/lat geometry to a window around the map, then reproject: far-away parts of a world layer can have no valid image
    in a regional projection, and would turn into infinities."""
    import shapely
    g = gdf.to_crs("EPSG:4326") if gdf.crs is not None and not gdf.crs.equals("EPSG:4326") else gdf.copy()
    clipped = shapely.clip_by_rect(np.asarray(g.geometry.values, dtype=object), *window)
    g = g.set_geometry(clipped)
    g = g[~g.geometry.is_empty & ~g.geometry.isna()]
    g = g.to_crs(crs)
    ok = np.isfinite(shapely.get_coordinates(np.asarray(g.geometry.values, dtype=object))).all() if len(g) else True
    if not ok:
        g = g[[bool(np.isfinite(shapely.get_coordinates(geom)).all()) for geom in g.geometry.values]]
    return g


def _wgs84_extent(plan: Plan):
    from pyproj import Transformer
    t = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    xs = np.linspace(plan.extent[0], plan.extent[2], 8)
    ys = np.linspace(plan.extent[1], plan.extent[3], 8)
    X, Y = np.meshgrid(xs, ys)
    lon, lat = t.transform(X.ravel(), Y.ravel())
    ok = np.isfinite(lon) & np.isfinite(lat)
    return (float(lon[ok].min()), float(lat[ok].min()), float(lon[ok].max()), float(lat[ok].max()))


def plan_visible_pad(plan: Plan):
    rect = lo.rect_of(plan.tpl["map"])
    fr = lo.Frame.fit(rect, plan.extent, float((plan.spec.get("extent") or {}).get("padding_pct", 5)))
    x0, y0, x1, y1 = fr.visible
    pad = 0.1 * max(x1 - x0, y1 - y0)
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
