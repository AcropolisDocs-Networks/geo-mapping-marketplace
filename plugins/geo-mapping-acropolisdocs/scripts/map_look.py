#!/usr/bin/env python3
"""map_look: the map's look (0.17.0; visual styling spec, docs/visual-styling-spec in the project).

    style:
      look: auto          # the default: a restrained visual hierarchy (below)
      look: classic       # exactly the 0.16 look, so a map built before 0.17 can be rebuilt unchanged

What `auto` changes, and nothing else:

  colour      Sequential and diverging class colours are generated from a brand colour, not taken from the brand's ramp as it is. The anchor (the
              brand's emphasis colour, or `palette: {anchor: "@brand.categorical.1"}` on a layer) seeds a ramp in Oklch that keeps the anchor's hue
              and runs the full range of tints and shades, so the classes span about 70 L* where the brand's own ramp spanned 45. Chroma peaks in
              the middle and eases off at both ends: the light end is not washed out and the dark end does not go muddy. Diverging ramps are two
              such arms meeting at the brand's light neutral. Categorical colours keep the brand's order, except that a colour too close to one already
              handed out (colour-vision safe) waits its turn, so three bands never get two navies.
  hierarchy   Context (roads) is drawn in the brand's light neutrals at widths below the thinnest data line; data lines and points get a casing
              (a band of the land colour under them) so crossings and clusters separate; point symbols are sized by how many there are; sector
              wedges and other overlapping area symbols are drawn as one group with a little transparency, so overlaps do not darken.

Every colour here is computed from the brand's tokens and nothing else, on every build, so the brand lint (MAP-BRAND-001) recomputes the same
set to allow them (`derived_colors`), the way bivariate grids are allowed. Pure functions; no I/O.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colorlib as cl  # noqa: E402

LOOKS = ("auto", "classic", "focus", "clean", "network", "terrain", "surface", "bold", "dark")
AUTO_LOOKS = ("focus", "clean", "network", "terrain", "surface")    # what `auto` resolves to, from the map's main data (resolve)
DEFAULT_LOOK = "auto"
RAMP_STEPS = 9                  # the generated ramp kept as the `sequential_ramp` / `diverging_ramp` token (raster ramps and legends read it)
CLASS_COUNTS = range(1, 10)     # class counts a palette is generated for (and the brand lint allows)

# Oklab lightness of the two ends of a generated sequential ramp, light ground and dark ground. 0.94 is about L* 93, 0.36 about L* 25.
SEQ_L = {"light": (0.94, 0.36), "dark": (0.90, 0.45)}
SEQ_L_BOLD = {"light": (0.96, 0.30), "dark": (0.93, 0.40)}      # bold: a little more span at both ends
SEQ_L_DARK_LOOK = (0.36, 0.90)  # the dark look: low values sit near the dark ground and the high ones glow (the light-ground order flipped)
CHROMA_MAX_BOLD = 0.20
CHROMA_MIN = 0.07               # a near-grey anchor still gives a ramp with a hue
CHROMA_MAX = 0.16               # restrained: never more saturated than this, whatever the anchor
CHROMA_BOOST = 1.15             # the peak sits a little above the anchor's own chroma (the anchor is usually a mid-dark brand colour)
CHROMA_ENDS = (0.35, 0.70)      # share of the peak chroma kept at the light end and at the dark end (the dark end keeps the hue, not grey)
DIV_ARM_L = {"light": (0.88, 0.36), "dark": (0.80, 0.48)}   # each diverging arm, from next to the neutral middle out to its end

# What each look sets. `classic` holds the 0.16 constants exactly.
PARAMS = {
    "classic": {
        "point_size_mm": None,                 # symbol.size (pt) or 3 pt
        "point_stroke_mm": 0.12, "proportional_stroke_mm": 0.15,
        "line_width_mm": 1.4 * 0.3528,
        "casing_extra_mm": 0.0, "casing_opacity": 0.0,
        "grad": (0.3, 1.8, 0.45),
        "roads": None,                         # the 0.16 road colours and widths in map_plan.add_context
        "roads_under_lines": 1.0,
        "wedge_opacity": 1.0, "wedge_stroke_mm": None,
        "line_nodes": False, "contour_mm": None, "context_over_surface": 1.0, "casing_color": "land", "ramp": "std",
    },
    "auto": {
        "point_size_mm": ((100, 3.0), (500, 2.5), (2000, 2.0), (float("inf"), 1.5)),   # by the layer's feature count
        "point_stroke_mm": 0.3, "proportional_stroke_mm": 0.25,
        "line_width_mm": 0.6,
        "casing_extra_mm": 0.4, "casing_opacity": 0.9,
        "grad": (0.5, 2.2, 0.5),
        "roads": {"major": (2, 0.25), "secondary": (1, 0.15), "ferry": (2, 0.12)},     # (neutral ramp step, width mm)
        "roads_under_lines": 0.6,              # roads fade further when the data itself is lines
        "wedge_opacity": 0.85, "wedge_stroke_mm": 0.25,
        "line_nodes": False,                   # network: a dot at each line end
        "contour_mm": None,                    # terrain: (minor, index) contour widths; None keeps 0.1 and 0.22
        "context_over_surface": 1.0,           # surface: the context's opacity over a raster surface
        "casing_color": "land",                # dark: the page colour
        "ramp": "std",                         # bold: a wider ramp; dark: the ramp flipped
    },
}
# 0.22.0 (stage 4): the named looks are the auto look with these changes. `auto` itself resolves to one of AUTO_LOOKS (resolve).
_LOOK_CHANGES = {
    "focus": {},                                                     # choropleth, hexbin, grid: mask and lift (map_focus), water-only context
    "clean": {},                                                     # points, sites, sectors: what auto already draws
    "network": {"roads_under_lines": 0.5, "line_nodes": True},
    "terrain": {"contour_mm": (0.15, 0.3)},
    "surface": {"context_over_surface": 0.7},
    "bold": {"point_size_mm": ((100, 3.75), (500, 3.1), (2000, 2.5), (float("inf"), 1.9)), "point_stroke_mm": 0.35, "line_width_mm": 0.75,
             "casing_extra_mm": 0.6, "grad": (0.62, 2.75, 0.62), "wedge_stroke_mm": 0.3, "ramp": "bold"},
    "dark": {"casing_color": "background", "ramp": "flip"},
}
for _k, _v in _LOOK_CHANGES.items():
    PARAMS[_k] = {**PARAMS["auto"], **_v}
POINT_OVERLAP_SHARE = 0.15      # more than this share of points touching a neighbour at their size: one size smaller
POINT_OVERLAP_OPACITY = 0.85    # ...and the stepped-down points drawn as one group at this opacity, so clusters do not go solid (0.22.0)


def look_of(spec: dict) -> str:
    """The look the MapSpec asks for (auto when it names none or one that does not exist)."""
    v = str(((spec or {}).get("style") or {}).get("look") or DEFAULT_LOOK).strip().lower()
    return v if v in LOOKS else DEFAULT_LOOK


def resolve(spec: dict) -> tuple[str, str]:
    """(look drawn, why). A named look is drawn as asked. `auto` picks by the map's main data: `focus` for a choropleth, hexbin or grid,
    `surface` for a raster or heat surface, `terrain` when relief or contours are drawn, `network` for line data, `clean` otherwise (points,
    sites, sectors). `bold` and `dark` are never picked: the person asks for them."""
    asked = look_of(spec)
    if asked != "auto":
        return asked, "asked for"
    layers = [l for l in (spec or {}).get("layers") or [] if isinstance(l, dict)]
    roles = [str(l.get("role") or "") for l in layers]
    if any(r in ("choropleth", "hexbin", "grid") for r in roles):
        return "focus", "a classed fill over areas"
    if any(r in ("raster", "heat") for r in roles):
        return "surface", "a surface"
    terrain = (spec or {}).get("terrain") or {}
    ctx = (spec or {}).get("context") or {}
    if any(r in ("hillshade", "contour") for r in roles) or (isinstance(terrain, dict) and terrain.get("on") is True) or ctx.get("preset") == "terrain":
        return "terrain", "relief or contours"
    if any(r == "lines" for r in roles):
        return "network", "line data"
    return "clean", "points, sites or other data"


def params(look: str) -> dict:
    return PARAMS.get(look, PARAMS[DEFAULT_LOOK])


# ------------------------------------------------------------------------------------------------------------------ colour
def _oklch(h: str) -> tuple[float, float, float]:
    L, a, b = cl.oklab(h)
    return L, math.hypot(a, b), math.atan2(b, a)


def _in_gamut(L: float, a: float, b: float) -> bool:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return all(-1e-6 <= v <= 1 + 1e-6 for v in (r, g, bb))


def _lch_hex(L: float, C: float, H: float) -> str:
    """Oklch to hex, chroma reduced (hue and lightness kept) until the colour is inside sRGB."""
    for _ in range(60):
        a, b = C * math.cos(H), C * math.sin(H)
        if _in_gamut(L, a, b):
            return cl.from_oklab(L, a, b)
        C *= 0.93
    return cl.from_oklab(L, 0.0, 0.0)


def ground_kind(tokens: dict) -> str:
    return "light" if cl.oklab(tokens.get("land") or tokens["background"])[0] > 0.5 else "dark"


def _arm(anchor: str, n: int, l_from: float, l_to: float, cmax: float = CHROMA_MAX) -> list[str]:
    """n colours of the anchor's hue from lightness l_from to l_to; chroma peaks in the middle and keeps CHROMA_ENDS of the peak at the ends."""
    _L0, C0, H = _oklch(anchor)
    peak = min(max(C0 * CHROMA_BOOST, CHROMA_MIN), cmax)
    light_end, dark_end = CHROMA_ENDS if l_from > l_to else CHROMA_ENDS[::-1]
    out = []
    for i in range(n):
        t = 0.5 if n == 1 else i / (n - 1)
        L = l_from + (l_to - l_from) * t
        floor = light_end + (dark_end - light_end) * t           # the share kept at this end, eased between the two
        C = peak * max(floor, math.sin(math.pi * t) ** 0.7)
        out.append(_lch_hex(L, C, H))
    return out


def sequential(anchor: str, n: int, ground: str = "light", ramp: str = "std") -> list[str]:
    """n class colours, low to high, from one anchor colour: the full range of its tints and shades, light to dark on a light ground.
    ramp `bold` spans a little more and is a little more saturated; `flip` (the dark look) runs from near the dark ground up to light."""
    if ramp == "flip" and ground != "dark":                   # the flipped order is for a dark ground only
        ramp = "std"
    hi, lo = SEQ_L_BOLD[ground] if ramp == "bold" else (SEQ_L_DARK_LOOK if ramp == "flip" else SEQ_L[ground])
    cmax = CHROMA_MAX_BOLD if ramp == "bold" else CHROMA_MAX
    if n == 1:
        return _arm(anchor, 3, hi, lo, cmax)[1:2]
    return _arm(anchor, n, hi, lo, cmax)


def diverging(low_anchor: str, high_anchor: str, middle: str, n: int, ground: str = "light") -> list[str]:
    """n class colours from the low anchor's hue through the neutral middle (odd n) to the high anchor's hue."""
    near, far = DIV_ARM_L[ground]
    k = n // 2
    if k == 0:
        return [middle]
    lo_arm = _arm(low_anchor, k, near, far)[::-1]            # darkest first
    hi_arm = _arm(high_anchor, k, near, far)
    return lo_arm + ([middle] if n % 2 else []) + hi_arm


CATEGORICAL_FLOOR = 25.0        # colour-vision-safe delta E two hand-out colours must keep at symbol size (MAP-CART-015's 15 is for areas)


def categorical_order(colors: list[str], floor: float = CATEGORICAL_FLOOR) -> list[int]:
    """Indices of the brand's categorical colours in the order they are handed out: the brand's own order, except that a colour closer than
    `floor` (smallest delta E across normal vision and the three colour-vision types) to one already handed out waits until the ones that are
    not have been used. Restrained: the brand's sequence is kept wherever it is distinct; only the near-twins move back."""
    order: list[int] = []
    waiting: list[int] = []
    for i, c in enumerate(colors):
        if all(cl.cvd_min_delta_e(c, colors[j]) >= floor for j in order):
            order.append(i)
        else:
            waiting.append(i)
    return order + waiting


def anchors(tokens: dict) -> list[str]:
    """Every colour a ramp may be seeded from: the emphasis colour, the categorical colours, the status colours, the diverging ends."""
    out = [tokens["emphasis"]] + list(tokens.get("categorical") or []) + list((tokens.get("status") or {}).values())
    d = tokens.get("_brand_diverging_ramp") or tokens.get("diverging_ramp") or []
    if d:
        out += [d[0], d[-1]]
    seen, uniq = set(), []
    for c in out:
        if c.upper() not in seen:
            seen.add(c.upper())
            uniq.append(c)
    return uniq


def diverging_parts(tokens: dict) -> tuple[str, str, str]:
    d = list(tokens.get("_brand_diverging_ramp") or tokens["diverging_ramp"])
    return d[0], d[-1], d[len(d) // 2]


def class_colors(tokens: dict, kind: str, n: int, anchor: str | None = None) -> list[str] | None:
    """The generated class colours for an `auto` look (None under classic, where the brand ramps are used as they are)."""
    if tokens.get("_look", "classic") == "classic":
        return None
    g = ground_kind(tokens)
    if kind == "sequential":
        return sequential(anchor or tokens["emphasis"], n, g, params(tokens["_look"])["ramp"])
    if kind == "diverging":
        lo, hi, mid = diverging_parts(tokens)
        if anchor:
            hi = anchor
        return diverging(lo, hi, mid, n, g)
    return None


def apply_tokens(tokens: dict, look: str) -> dict:
    """A copy of a mode's tokens for this look. Under `auto` the sequential and diverging ramp tokens become the generated RAMP_STEPS-step ramps
    (what a raster ramp and a ramp legend read), the brand's own ramps are kept under _brand_* (an explicit @brand.sequential.N still means the
    brand's colour), and the categorical colours get a hand-out order. `classic` returns the tokens unchanged (a copy)."""
    t = dict(tokens)
    t["_look"] = look
    if look == "classic":
        return t
    g = ground_kind(t)
    t["_brand_sequential_ramp"] = list(tokens["sequential_ramp"])
    t["_brand_diverging_ramp"] = list(tokens["diverging_ramp"])
    t["sequential_ramp"] = sequential(t["emphasis"], RAMP_STEPS, g, params(look)["ramp"])
    lo, hi, mid = diverging_parts(t)
    t["diverging_ramp"] = diverging(lo, hi, mid, RAMP_STEPS, g)
    t["categorical_order"] = categorical_order(list(t.get("categorical") or []))
    return t


def categorical_pick(tokens: dict, n: int) -> list[str]:
    """The first n categorical colours in hand-out order (the brand's order under classic)."""
    cats = list(tokens.get("categorical") or [])
    order = tokens.get("categorical_order") or list(range(len(cats)))
    return [cats[i] for i in order[:n]]


def derived_colors(tokens: dict) -> set[str]:
    """Every colour an `auto` look can draw for these (brand) tokens: the generated ramps for every anchor and class count, both grounds' rules
    applied to this mode. What MAP-BRAND-001 allows beside the declared tokens."""
    base = dict(tokens)
    g = ground_kind(base)
    out: set[str] = set()
    ramps = ("std", "bold") + (("flip",) if g == "dark" else ())      # every look's ramp for this ground (the dark look's only on a dark one)
    for a in anchors(base):
        for r in ramps:
            for n in list(CLASS_COUNTS) + [RAMP_STEPS]:
                out |= {c.upper() for c in sequential(a, n, g, r)}
    lo, hi, mid = diverging_parts(base)
    for h in [hi] + anchors(base):
        for n in list(CLASS_COUNTS) + [RAMP_STEPS]:
            out |= {c.upper() for c in diverging(lo, h, mid, n, g)}
    return out


POLYGON_FILL_OPACITY = 0.4      # 0.22.0: an area layer that is not a choropleth (spec 3.1)...
POLYGON_EDGE_MM = 0.5           # ...with its edge in the fill colour at this width
HIGHLIGHT_DIM_CHROMA = 0.35     # the rest of a highlighted layer keeps this share of its colour's chroma...
HIGHLIGHT_REST_OPACITY = 0.3    # ...and is drawn at this opacity
HIGHLIGHT_SIZE = 1.2            # the highlighted features: symbols and lines this much larger


def dim(h: str) -> str:
    """A colour desaturated for the rest of a highlighted layer: same lightness and hue, HIGHLIGHT_DIM_CHROMA of the chroma."""
    L, C, H = _oklch(h)
    return _lch_hex(L, C * HIGHLIGHT_DIM_CHROMA, H).upper()


def dim_colors(colors) -> set[str]:
    """The dimmed twin of every colour given: what MAP-BRAND-001 allows for a highlight (computed, never typed)."""
    return {dim(c) for c in colors if isinstance(c, str) and c.startswith("#") and len(c) == 7}


# ------------------------------------------------------------------------------------------------------------------ sizes
def point_size_mm(look: str, count: int) -> float | None:
    """The default point diameter for a layer of `count` features (None: the classic symbol.size default)."""
    steps = params(look)["point_size_mm"]
    if not steps:
        return None
    for upto, d in steps:
        if count < upto:
            return d
    return steps[-1][1]


def smaller_point(look: str, d: float) -> float:
    steps = [s for _u, s in params(look)["point_size_mm"] or []]
    lower = [s for s in steps if s < d - 1e-9]
    return max(lower) if lower else d


def overlap_share(xy_mm, d: float) -> float:
    """Share of points with another point's centre closer than one symbol diameter (they touch or overlap on the sheet)."""
    import numpy as np
    xy = np.asarray(xy_mm, dtype="float64")
    if len(xy) < 2:
        return 0.0
    try:
        from scipy.spatial import cKDTree
        dist, _ = cKDTree(xy).query(xy, k=2)
        return float((dist[:, 1] < d).mean())
    except Exception:  # noqa: BLE001 - no scipy: a grid of cells one diameter wide, neighbours in the same or the next cell
        cells: dict = {}
        for i, (x, y) in enumerate(xy):
            cells.setdefault((int(x // d), int(y // d)), []).append(i)
        hit = 0
        for (cx, cy), idx in cells.items():
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cells.get((cx + dx, cy + dy), [])]
            for i in idx:
                if any(j != i and (xy[j, 0] - xy[i, 0]) ** 2 + (xy[j, 1] - xy[i, 1]) ** 2 < d * d for j in near):
                    hit += 1
        return hit / len(xy)


# ------------------------------------------------------------------------------------------------------------------ surfaces (0.20.0, stage 3)
SURFACE_FULL_AT = 0.35          # a continuous sequential surface is fully opaque from this share of its ramp up; below it fades to transparent
CLASS_ALPHAS = (0.5, 0.75)      # the first classes of a hexbin or a classed sequential raster; every class after them is opaque
RELIEF_TINT_ALPHA = 0.55        # the elevation tint's strength at the highest ground (before the relief image's own opacity)
RELIEF_SHADE_GAIN = 1.25        # the shading a little stronger than 0.19's, now that the tint carries the broad shape
TILE_TREATMENT = {"none": (0.0, 0.0), "muted": (0.7, 0.3), "knockout": (0.85, 0.5)}    # (desaturate, blend toward the land colour)


def surface_alpha(t):
    """Opacity (0..1) of a continuous sequential surface at ramp position t (0 = the low end): value by alpha, so low values let the ground
    show and the high values stand out."""
    import numpy as np
    return np.clip(np.asarray(t, dtype=float) / SURFACE_FULL_AT, 0.0, 1.0)


def class_alphas(k: int) -> list[float]:
    """Opacities for k ordered classes, low to high: the first two fade, the rest are opaque."""
    if k <= 1:
        return [1.0] * k
    if k == 2:
        return [0.5, 1.0]
    return [CLASS_ALPHAS[i] if i < len(CLASS_ALPHAS) else 1.0 for i in range(k)]


def tile_treatment(spec: dict, look: str) -> str:
    v = str(((spec or {}).get("basemap") or {}).get("treatment") or ("none" if look == "classic" else "muted")).lower()
    return v if v in TILE_TREATMENT else "muted"


def treat_tiles(png_bytes: bytes, land: str, how: str) -> bytes:
    """The tile composite desaturated and pulled toward the brand's land colour (the tiles step back behind the data, in the brand's ground)."""
    import io
    from PIL import Image
    desat, toward = TILE_TREATMENT.get(how, TILE_TREATMENT["muted"])
    if not desat and not toward:
        return png_bytes
    im = Image.open(io.BytesIO(png_bytes))
    alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") else None
    rgb = im.convert("RGB")
    grey = rgb.convert("L").convert("RGB")
    out = Image.blend(rgb, grey, desat)
    h = land.lstrip("#")
    out = Image.blend(out, Image.new("RGB", out.size, tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))), toward)
    if alpha is not None:
        out.putalpha(alpha)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
