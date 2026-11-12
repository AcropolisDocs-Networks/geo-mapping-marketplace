#!/usr/bin/env python3
"""style_dict: the one style description the styled data outputs share (M14): KML/KMZ styles and icons (kml_style), the GeoPackage
`layer_styles` table with QML and SLD (gpkg_style), and the QGIS project (qgis_project).

A style dict says how one layer is drawn, in the units the map uses (millimetres on the sheet):

    {"mode": "single" | "categorical" | "classed",
     "geometry": "polygon" | "line" | "point",       # optional: taken from the data when absent
     "title": "Events per 1,000 people",            # optional: folder, legend and layer name shown to people
     "field": "rate",                               # categorical and classed: the field the classes read
     "color": "@brand.accent",                      # single: the fill (polygon, point) or the line colour; null = no fill
     "colors": [...],                               # categorical and classed: one per class (brand palette when absent)
     "categories": ["urban", "rural"],              # categorical: the value of each class, as text (from the data when absent)
     "other": "Other",                              # categorical: the label of the catch-all last class, when there is one
     "edges": [0, 5, 10, 20],                       # classed: k+1 numbers; class i holds e_i <= v < e_i+1, the last is closed;
                                                    #   values outside the edges fall in the first or last class (as on the map)
     "method": "quantile", "classes": 5,            # classed without edges: computed with the plugin's classifier
     "labels": [...],                               # the legend label of each class
     "nodata_color": "@brand.no_data", "nodata_label": "No data",
     "stroke": "@brand.background",                 # polygon outline and point outline; null = none
     "width": 0.12,                                 # mm: the line width, or the polygon or point outline width
     "size": 1.06,                                  # mm: the point symbol's diameter
     "shape": "circle", "shapes": [...],            # point symbol: circle, square, triangle, diamond, cross; one per class
     "opacity": 1.0,                                # polygon fill opacity; the line or point symbol opacity
     "dash": "1.2 0.8",                             # line dash in mm (dash, gap, ...)
     "proportional": {"field": "load", "max_value": 480, "max_size": 4.9, "min_size": 0.8},
     "label": {"field": "name", "size_pt": 7},
     "balloon_fields": ["name", "rate"],            # KML: the fields in the description balloon (default: every field, up to 25)
     "name_field": "name"}                          # KML: the placemark name (default: the "name" field when there is one)

Colours are `@brand.*` tokens (accent, land, water, frame, label, background, no_data, legend_bg, legend_frame, status.NAME,
categorical.N, sequential.N), resolved against the brand's tokens, or literal colours in the form `#rrggbb` (also `#rgb`, `#rrggbbaa`,
`rgb(r, g, b)` or `r,g,b[,a]`). A map's own layers always come with brand colours (`style_from_layerplan`); a style file handed to
geo-convert may name literal colours, which are the person's own choice for their data file.

    import style_dict as sd
    st = sd.resolve(style, gdf, tokens)        # a complete, checked copy: classes, colours, widths, sizes filled in
    idx = sd.assign(gdf, st)                   # the class of each row (-1 = no data), the same rule the map uses
    st = sd.style_from_layerplan(layer_plan, plan.tokens, label=...)
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

MODES = ("single", "categorical", "classed")
GEOMETRIES = ("polygon", "line", "point")
SHAPES = ("circle", "square", "triangle", "diamond", "cross")
PT_MM = 0.3528
MM_PX = 96.0 / 25.4                     # CSS pixels per millimetre: KML widths and SLD sizes are in pixels
MIN_SYMBOL_MM = 0.8                     # the smallest proportional symbol (the renderer's floor)
MAX_CATEGORIES = 8
NODATA_LABEL = "No data"
# Used only when no brand tokens are at hand (a library call with literal colours): neutral greys, never a brand colour
FALLBACK_RGB = {"no_data": (192, 192, 192), "stroke": (255, 255, 255), "frame": (160, 160, 160), "label": (32, 32, 32),
                "legend_bg": (255, 255, 255), "legend_frame": (200, 200, 200), "fill": (128, 128, 128)}
TOKEN_ALIASES = {"accent": "emphasis", "emphasis": "emphasis", "land": "land", "water": "water", "frame": "frame", "label": "label",
                 "background": "background", "no_data": "no_data", "legend_bg": "legend_bg", "legend_frame": "legend_frame"}


class StyleError(ValueError):
    """A style that cannot be used, with a message that is safe to show the person."""


# ---------------------------------------------------------------------------------------------------------------- colours
def token(value: str, tokens: dict | None):
    """An `@brand.*` token's colour from the tokens, or None when the value is not a token."""
    v = str(value)
    if not v.startswith("@brand."):
        return None
    if not tokens:
        raise StyleError(f"'{v}' is a brand colour, but no brand tokens are at hand; name the brand or give the colour")
    key = v[len("@brand."):]
    try:
        if key in TOKEN_ALIASES:
            return tokens[TOKEN_ALIASES[key]]
        if key.startswith("status."):
            return tokens["status"][key.split(".", 1)[1]]
        if key.startswith("categorical."):
            return tokens["categorical"][int(key.split(".", 1)[1])]
        if key.startswith("sequential."):
            return tokens["sequential_ramp"][int(key.split(".", 1)[1])]
    except (KeyError, IndexError, ValueError):
        pass
    raise StyleError(f"unknown brand colour '{v}' (try @brand.accent, .land, .water, .no_data, .status.success, .categorical.N, .sequential.N)")


def rgba(value, tokens: dict | None = None) -> tuple[int, int, int, int]:
    """(r, g, b, a) from a colour in any accepted form; a = 255 unless the colour carries its own alpha."""
    if value is None:
        raise StyleError("no colour given")
    if isinstance(value, (tuple, list)):
        vals = [int(round(float(x))) for x in value]
        if len(vals) == 3:
            vals.append(255)
        if len(vals) != 4 or any(not 0 <= x <= 255 for x in vals):
            raise StyleError(f"colour {value!r} needs three or four numbers from 0 to 255")
        return tuple(vals)  # type: ignore[return-value]
    v = str(value).strip()
    t = token(v, tokens)
    if t is not None:
        return rgba(t)
    m = re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", v)
    if m and (v.startswith("#") or len(m.group(1)) in (6, 8)):
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) if len(h) == 8 else 255
        return (r, g, b, a)
    m = re.fullmatch(r"(?:rgba?\()?\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)?", v)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        a = m.group(4)
        av = 255 if a is None else (int(round(float(a) * 255)) if float(a) <= 1 and "." in a else int(float(a)))
        if max(r, g, b, av) > 255:
            raise StyleError(f"colour '{v}' has a component above 255")
        return (r, g, b, av)
    raise StyleError(f"'{v}' is not a colour (use an @brand.* token or #rrggbb)")


def hex_rgb(value, tokens: dict | None = None) -> str:
    r, g, b, _ = rgba(value, tokens)
    return "#" + f"{r:02X}{g:02X}{b:02X}"


def kml_color(value, opacity: float = 1.0, tokens: dict | None = None) -> str:
    """KML's aabbggrr."""
    r, g, b, a = rgba(value, tokens)
    a = int(round(a * max(0.0, min(1.0, float(opacity)))))
    return f"{a:02x}{b:02x}{g:02x}{r:02x}"


def from_kml_color(text: str) -> tuple[str, float]:
    """('#RRGGBB', opacity) from KML's aabbggrr."""
    t = str(text or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{8}", t):
        raise StyleError(f"'{text}' is not a KML colour (aabbggrr)")
    a, b, g, r = (int(t[i:i + 2], 16) for i in (0, 2, 4, 6))
    return hex_rgb((r, g, b)), round(a / 255.0, 3)


def qgis_color(value, opacity: float = 1.0, tokens: dict | None = None) -> str:
    """QGIS's 'r,g,b,a'."""
    r, g, b, a = rgba(value, tokens)
    return f"{r},{g},{b},{int(round(a * max(0.0, min(1.0, float(opacity)))))}"


# ---------------------------------------------------------------------------------------------------------------- data
def geometry_kind(gdf) -> str:
    """polygon | line | point, by the most common geometry family (a mixed layer is drawn as its commonest kind)."""
    fams = {"Polygon": "polygon", "MultiPolygon": "polygon", "LineString": "line", "MultiLineString": "line", "LinearRing": "line",
            "Point": "point", "MultiPoint": "point"}
    counts: dict[str, int] = {}
    for t in gdf.geometry.geom_type.dropna():
        k = fams.get(t)
        if k:
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        raise StyleError("the layer has no geometry to style")
    return max(counts, key=lambda k: (counts[k], k))


def value_text(v) -> str:
    """A category value as text, the way the classifier reads it: '' for a missing value."""
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def assign(gdf, style: dict):
    """The class index of every row (-1 = no data), by the rule the map uses: a categorical value matches its category, or falls in the
    catch-all last class when there is one; a numeric value falls in [e_i, e_i+1), values outside the edges in the first or last class."""
    import numpy as np
    n = len(gdf)
    mode = style.get("mode", "single")
    if mode == "single":
        return np.zeros(n, dtype=int)
    f = style["field"]
    if f not in gdf.columns:
        raise StyleError(f"the style reads field '{f}', which the data does not have")
    if mode == "categorical":
        cats = [str(c) for c in style["categories"]]
        idx = {c: i for i, c in enumerate(cats)}
        other = len(cats) - 1 if style.get("other") else None
        out = np.full(n, -1, dtype=int)
        for i, v in enumerate(gdf[f].tolist()):
            s = value_text(v)
            if s == "":
                continue
            out[i] = idx.get(s, other if other is not None else -1)
        return out
    edges = [float(e) for e in style["edges"]]
    k = len(edges) - 1
    import pandas as pd
    vals = pd.to_numeric(gdf[f], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    out = np.full(n, -1, dtype=int)
    ok = np.isfinite(vals)
    if k > 1:
        out[ok] = np.clip(np.searchsorted(np.array(edges[1:-1]), vals[ok], side="right"), 0, k - 1)
    else:
        out[ok] = 0
    return out


# ---------------------------------------------------------------------------------------------------------------- resolve
def _num(v, what: str, lo: float | None = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise StyleError(f"{what} must be a number, not {v!r}") from None
    if not math.isfinite(x) or (lo is not None and x < lo):
        raise StyleError(f"{what} must be a number of at least {lo}")
    return x


def _fallback(name: str) -> str:
    return hex_rgb(FALLBACK_RGB[name])


def _tok(tokens: dict | None, key: str, fallback: str) -> str:
    if tokens and tokens.get(key):
        return hex_rgb(tokens[key])
    return _fallback(fallback)


def resolve(style: dict | None, gdf=None, tokens: dict | None = None) -> dict:
    """A complete, checked copy of a style: the mode, the geometry, the classes (categories from the data; edges and labels from the
    plugin's classifier), class colours from the brand palette, outline, widths, sizes and opacity. Colours come back as '#RRGGBB'."""
    st = dict(style or {})
    unknown = sorted(set(st) - KNOWN_KEYS)
    if unknown:
        raise StyleError(f"unknown style key(s): {', '.join(unknown)}. Known: {', '.join(sorted(KNOWN_KEYS))}")
    geom = st.get("geometry") or (geometry_kind(gdf) if gdf is not None else None)
    if geom not in GEOMETRIES:
        raise StyleError(f"geometry must be one of {', '.join(GEOMETRIES)}" + (f", not '{geom}'" if geom else " (the style has no data to read it from)"))
    st["geometry"] = geom
    mode = st.get("mode")
    if mode is None:
        mode = "single"
        if st.get("field") and gdf is not None and st["field"] in gdf.columns:
            import pandas as pd
            mode = "categorical" if (st.get("categories") or not pd.api.types.is_numeric_dtype(gdf[st["field"]])) else "classed"
        elif st.get("edges"):
            mode = "classed"
        elif st.get("categories"):
            mode = "categorical"
    if mode not in MODES:
        raise StyleError(f"mode must be one of {', '.join(MODES)}, not '{mode}'")
    st["mode"] = mode
    if mode != "single":
        if not st.get("field"):
            raise StyleError(f"a {mode} style needs `field`")
        if gdf is not None and st["field"] not in gdf.columns:
            raise StyleError(f"field '{st['field']}' is not in the data. Fields: {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
    import classify as cf
    if mode == "categorical":
        if not st.get("categories"):
            if gdf is None:
                raise StyleError("a categorical style needs `categories` when there is no data to read them from")
            res = cf.classify(gdf[st["field"]].tolist(), "categorical", max_categories=int(st.get("max_categories") or MAX_CATEGORIES))
            st["categories"] = list(res.categories)
            if any("grouped as" in n for n in res.notes):
                st["other"] = res.categories[-1]
        st["categories"] = [str(c) for c in st["categories"]]
        st.setdefault("labels", list(st["categories"]))
        k = len(st["categories"])
        if not st.get("colors"):
            if not tokens:
                raise StyleError("give `colors` for the categories, or style with a brand")
            try:
                st["colors"] = cf.palette(tokens, "categorical", k)["colors"]
            except cf.ClassifyError as ex:
                raise StyleError(str(ex)) from None
    elif mode == "classed":
        if not st.get("edges"):
            if gdf is None:
                raise StyleError("a classed style needs `edges` when there is no data to compute them from")
            import numpy as np
            import pandas as pd
            vals = pd.to_numeric(gdf[st["field"]], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            if not np.isfinite(vals).any():
                raise StyleError(f"field '{st['field']}' holds no numbers to class")
            try:
                res = cf.classify(vals, st.get("method", "quantile"), int(st.get("classes", 5)))
            except cf.ClassifyError as ex:
                raise StyleError(str(ex)) from None
            st["edges"] = [float(e) for e in res.edges]
            st.setdefault("labels", list(res.labels))
            st.setdefault("method", res.method)
        edges = [_num(e, "an edge", None) for e in st["edges"]]
        if len(edges) < 2 or any(b <= a for a, b in zip(edges, edges[1:])):
            raise StyleError("`edges` must be at least two numbers in increasing order")
        st["edges"] = edges
        k = len(edges) - 1
        if not st.get("labels"):
            st["labels"] = [f"{_fmt(a)} to {'' if i == k - 1 else '<'}{_fmt(b)}" for i, (a, b) in enumerate(zip(edges, edges[1:]))]
        if not st.get("colors"):
            if not tokens:
                raise StyleError("give `colors` for the classes, or style with a brand")
            try:
                st["colors"] = cf.palette(tokens, "sequential", k)["colors"]
            except cf.ClassifyError as ex:
                raise StyleError(str(ex)) from None
        if gdf is not None:
            import numpy as np
            import pandas as pd
            vals = pd.to_numeric(gdf[st["field"]], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            fin = vals[np.isfinite(vals)]
            if len(fin):
                st["value_range"] = [float(fin.min()), float(fin.max())]
    if mode != "single":
        k = len(st["categories"]) if mode == "categorical" else len(st["edges"]) - 1
        if len(st["colors"]) != k:
            raise StyleError(f"{len(st['colors'])} colour(s) for {k} class(es); give one colour per class")
        if len(st["labels"]) != k:
            raise StyleError(f"{len(st['labels'])} label(s) for {k} class(es); give one label per class")
        st["labels"] = [str(x) for x in st["labels"]]
        st["colors"] = [hex_rgb(c, tokens) for c in st["colors"]]
        if gdf is not None:
            st["nodata"] = int((assign(gdf, st) < 0).sum())
    # colours, widths and sizes, with the renderer's defaults (map_plan.style_layer)
    area = mode != "single" and geom == "polygon"
    if "color" not in st or (st["color"] is None and geom != "polygon"):
        st["color"] = (_tok(tokens, "land", "fill") if geom == "polygon" else _tok(tokens, "emphasis", "fill")) if mode == "single" else None
    elif st["color"] is not None:
        st["color"] = hex_rgb(st["color"], tokens)
    if "stroke" not in st:
        st["stroke"] = (_tok(tokens, "background", "stroke") if (area or geom == "point") else _tok(tokens, "frame", "frame")) if geom != "line" else None
    elif st["stroke"] is not None:
        st["stroke"] = hex_rgb(st["stroke"], tokens)
    st["nodata_color"] = hex_rgb(st["nodata_color"], tokens) if st.get("nodata_color") else _tok(tokens, "no_data", "no_data")
    st.setdefault("nodata_label", NODATA_LABEL)
    default_w = {"polygon": 0.12 if area else 0.2, "line": 1.4 * PT_MM, "point": 0.12}[geom]
    st["width"] = round(_num(st.get("width", default_w), "width"), 4)
    st["size"] = round(_num(st.get("size", 3 * PT_MM), "size"), 4)
    st["opacity"] = _num(st.get("opacity", 1.0), "opacity")
    if st["opacity"] > 1:
        raise StyleError("opacity runs from 0 to 1")
    st.setdefault("shape", "circle")
    if st["shape"] not in SHAPES:
        raise StyleError(f"shape must be one of {', '.join(SHAPES)}")
    if st.get("shapes"):
        k = len(st.get("colors") or [])
        if mode == "single" or len(st["shapes"]) != k or any(s not in SHAPES for s in st["shapes"]):
            raise StyleError(f"`shapes` needs one of {', '.join(SHAPES)} for each class")
    if st.get("proportional"):
        p = dict(st["proportional"])
        if not p.get("field"):
            raise StyleError("`proportional` needs a field")
        if gdf is not None and p["field"] not in gdf.columns:
            raise StyleError(f"proportional field '{p['field']}' is not in the data")
        if not p.get("max_value") and gdf is not None:
            import numpy as np
            import pandas as pd
            v = pd.to_numeric(gdf[p["field"]], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            v = v[np.isfinite(v) & (v > 0)]
            p["max_value"] = float(v.max()) if len(v) else 1.0
        p["max_value"] = _num(p.get("max_value", 1.0), "proportional max_value")
        p["max_size"] = _num(p.get("max_size", 14 * PT_MM), "proportional max_size")
        p["min_size"] = _num(p.get("min_size", MIN_SYMBOL_MM), "proportional min_size")
        st["proportional"] = p
    if st.get("label"):
        lb = dict(st["label"]) if isinstance(st["label"], dict) else {"field": str(st["label"])}
        if gdf is not None and lb.get("field") not in gdf.columns:
            raise StyleError(f"label field '{lb.get('field')}' is not in the data")
        lb["size_pt"] = _num(lb.get("size_pt", 7), "label size_pt")
        st["label"] = lb
    if gdf is not None:
        for key in ("balloon_fields",):
            miss = [x for x in st.get(key) or [] if x not in gdf.columns]
            if miss:
                raise StyleError(f"{key}: field(s) not in the data: {', '.join(miss)}")
        if st.get("name_field") and st["name_field"] not in gdf.columns:
            raise StyleError(f"name_field '{st['name_field']}' is not in the data")
    _resolve_extras(st, gdf, tokens)
    return st


KNOWN_KEYS = {"mode", "geometry", "title", "layer", "field", "color", "colors", "categories", "other", "edges", "method", "classes", "labels",
              "nodata_color", "nodata_label", "nodata", "stroke", "width", "size", "shape", "shapes", "opacity", "dash", "proportional", "label",
              "balloon_fields", "name_field", "value_range", "max_categories", "widths", "bivariate", "patterns", "pattern_inks"}


def _resolve_extras(st: dict, gdf=None, tokens: dict | None = None) -> None:
    """M16: `widths` (graduated line widths, one per class, mm) and `bivariate` ({n, x: {field, edges}, y: {field, edges}, colors, labels}, the
    n x n classes with colour k = y class * n + x class), checked. The QGIS writers draw them (gpkg_style); KML keeps the class colours only."""
    if st.get("widths") is not None:
        k = len(st.get("colors") or [])
        w = [_num(x, "a class width", None) for x in st["widths"]]
        if st["mode"] == "single" or len(w) != k or any(x <= 0 for x in w):
            raise StyleError(f"`widths` needs one positive width (mm) for each of the {k} classes")
        st["widths"] = [round(x, 4) for x in w]
    if st.get("patterns") is not None:                  # 1.0.0: one hatch per class (patterns.PATTERNS), drawn by the QML and SLD
        import patterns as pt_
        k = len((st.get("bivariate") or {}).get("colors") or st.get("colors") or [])
        pats = [str(x) for x in st["patterns"]]
        inks = [hex_rgb(c, tokens) for c in (st.get("pattern_inks") or [])]
        if len(pats) != k or len(inks) != k or any(x not in pt_.PATTERNS for x in pats):
            raise StyleError(f"`patterns` needs one of {', '.join(pt_.PATTERNS)} and one ink colour (`pattern_inks`) for each of the {k} classes")
        st["patterns"], st["pattern_inks"] = pats, inks
    b = st.get("bivariate")
    if b is not None:
        n = int(b.get("n") or 0)
        if n not in (2, 3):
            raise StyleError("a bivariate style is 2 x 2 or 3 x 3 (`n`)")
        for ax in ("x", "y"):
            a = b.get(ax) or {}
            e = [_num(x, "a bivariate edge", None) for x in a.get("edges") or []]
            if not a.get("field") or len(e) != n + 1 or any(q <= p for p, q in zip(e, e[1:])):
                raise StyleError(f"bivariate {ax}: a field and {n + 1} increasing edges")
            if gdf is not None and a["field"] not in gdf.columns:
                raise StyleError(f"bivariate {ax} field '{a['field']}' is not in the data")
        cols = list(b.get("colors") or [])
        labs = [str(x) for x in (b.get("labels") or [])]
        if len(cols) != n * n or len(labs) != n * n:
            raise StyleError(f"a {n} x {n} bivariate style needs {n * n} colours and labels")
        st["bivariate"] = {"n": n, "x": {"field": b["x"]["field"], "edges": [float(v) for v in b["x"]["edges"]]},
                           "y": {"field": b["y"]["field"], "edges": [float(v) for v in b["y"]["edges"]]},
                           "colors": [hex_rgb(c, tokens) for c in cols], "labels": labs}


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.4g}"


def class_count(style: dict) -> int:
    if style.get("mode") == "categorical":
        return len(style["categories"])
    if style.get("mode") == "classed":
        return len(style["edges"]) - 1
    return 1


def class_colors(style: dict) -> list[str | None]:
    return list(style["colors"]) if style.get("mode") in ("categorical", "classed") else [style.get("color")]


def class_labels(style: dict) -> list[str]:
    return list(style["labels"]) if style.get("mode") in ("categorical", "classed") else [str(style.get("title") or style.get("layer") or "")]


def class_shape(style: dict, i: int) -> str:
    sh = style.get("shapes")
    return sh[i] if sh and 0 <= i < len(sh) else style.get("shape", "circle")


def proportional_size(style: dict, v) -> float:
    """The diameter (mm) of a proportional symbol: area proportional to the value, never under the floor (the renderer's rule)."""
    p = style["proportional"]
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x) or x <= 0:
        return 0.0
    return max(p["min_size"], p["max_size"] * math.sqrt(x / max(p["max_value"], 1e-12)))


def load_style(path: str | Path) -> dict:
    """A style dict from a JSON (or YAML) file; one style, or {layer: style} for several layers."""
    p = Path(path)
    if not p.is_file():
        raise StyleError(f"style file not found: {p.name}")
    try:
        if p.suffix.lower() in (".yaml", ".yml"):
            data = gc.load_yaml(p)
        else:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as ex:  # noqa: BLE001 - any parse failure is the same message
        raise StyleError(f"{p.name} could not be read as a style: {str(ex)[:160]}") from None
    if not isinstance(data, dict):
        raise StyleError(f"{p.name} must hold a style object")
    return data


def brand_tokens(brand: str | None = None, mode: str = "print") -> dict | None:
    """The edition's brand tokens (the resolved default brand, or the one named), or None outside a built edition."""
    try:
        import brandlib as bl
        manifest = gc.build_manifest()
        slug = bl.resolve(manifest, None, brand)["brand"]
        return bl.load_pack(slug)["tokens"]["modes"][mode]
    except Exception:  # noqa: BLE001 - outside an edition there are no tokens; literal colours still work
        return None


def plugin_ident() -> dict:
    """{'plugin', 'version', 'house', 'house_url'}: what the hidden metadata of a data output names. Never drawn or shown."""
    out = {"plugin": "geo-mapping", "version": None, "house": "AcropolisDocs", "house_url": None}
    try:
        m = gc.build_manifest()
        out["plugin"], out["version"] = m.get("plugin_name") or out["plugin"], m.get("version")
        try:
            import brandlib as bl
            acc = bl.accreditation_props(m, bl.resolve(m)["brand"])
            out["house"], out["house_url"] = acc["house"], acc["house_url"]
        except Exception:  # noqa: BLE001
            pass
    except (FileNotFoundError, ValueError):
        v = gc.plugin_root().parent / "VERSION"
        if v.is_file():
            out["version"] = v.read_text().strip() + "+source"
    return out


def generator_text() -> str:
    p = plugin_ident()
    return f"{p['plugin']} {p['version'] or ''}".strip()


# ---------------------------------------------------------------------------------------------------------------- from the map
def style_from_layerplan(layerplan, tokens: dict | None = None, label: dict | None = None) -> dict:
    """The style dict of one of a built map's layers (a map_plan.LayerPlan), so the data outputs draw it as the map does: the class field,
    categories or edges, labels and colours the legend shows, the outline, the widths and sizes in millimetres, the opacity, the point shapes,
    the proportional scale. `tokens` (plan.tokens) gives the no-data colour; `label` is the map's label entry for the layer, if any."""
    import numpy as np
    l = layerplan
    sym = dict(l.symbol or {})
    st: dict = {"geometry": l.kind, "title": l.title, "layer": l.id}
    cls = l.cls
    if cls is not None and l.colors and l.value_field:
        if cls.kind == "categorical":
            st.update(mode="categorical", field=l.value_field, categories=[str(c) for c in cls.categories], labels=[str(x) for x in cls.labels],
                      colors=list(l.colors))
            if cls.categories and any("grouped as" in n for n in (cls.notes or [])):
                st["other"] = cls.categories[-1]
        else:
            st.update(mode="classed", field=l.value_field, edges=[float(e) for e in cls.edges], labels=[str(x) for x in cls.labels],
                      colors=list(l.colors), method=cls.method)
            if l.value_field in l.gdf.columns:
                import pandas as pd
                v = pd.to_numeric(l.gdf[l.value_field], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
                v = v[np.isfinite(v)]
                if len(v):
                    st["value_range"] = [float(v.min()), float(v.max())]
        st["nodata"] = int((np.asarray(cls.assigned) < 0).sum()) if cls.assigned is not None else 0
        if l.class_shapes:
            st["shapes"] = list(l.class_shapes)
    else:
        st["mode"] = "single"
        st["color"] = l.stroke if l.kind == "line" else (l.fill or l.stroke)
    if l.palette_kind == "graduated" and sym.get("_widths") and st["mode"] == "classed":       # M16: graduated line widths, one per class
        st["widths"] = [round(float(w), 4) for w in sym["_widths"]]
    bv = sym.get("_bivariate")
    if l.palette_kind == "bivariate" and bv and l.colors:                                     # M16: the n x n classes as rules on both fields
        st["bivariate"] ={"n": int(bv["n"]), "x": {"field": bv["x"]["field"], "edges": list(bv["x"]["edges"])},
                           "y": {"field": bv["y"]["field"], "edges": list(bv["y"]["edges"])}, "colors": list(l.colors),
                           "labels": [str(x) for x in (cls.labels if cls is not None else [])]}
    if tokens and tokens.get("no_data"):
        st["nodata_color"] = tokens["no_data"]
    pats = sym.get("_patterns")
    if pats and l.kind == "polygon" and tokens:          # 1.0.0 (TODO M15): the hatch over each class, for the QML and SLD (KML cannot hatch)
        import patterns as pt_
        cols_ = (st.get("bivariate") or {}).get("colors") or st.get("colors") or []
        if len(pats) == len(cols_):
            st["patterns"] = [str(p) for p in pats]
            st["pattern_inks"] = [pt_.ink_for(c, tokens) for c in cols_]
    if l.kind == "polygon":
        st["stroke"] = l.stroke
        st["width"] = round(float(l.stroke_w_mm), 4)
        st["opacity"] = 0.0 if getattr(l, "outline", False) else float(l.fill_opacity)       # 1.2.0: an outline has no fill
    elif l.kind == "line":
        st["stroke"] = None
        st["width"] = round(float(l.stroke_w_mm), 4)
        st["opacity"] = float(getattr(l, "stroke_opacity", 1.0) or 1.0)
        if sym.get("dash"):
            st["dash"] = str(sym["dash"])
    else:
        st["stroke"] = l.stroke
        st["shape"] = str(sym.get("shape") or "circle")
        st["size"] = round(float(sym.get("size", 3)) * PT_MM, 4)
        st["width"] = 0.15 if l.role == "proportional" else 0.12
        if l.role == "proportional" and l.value_field and l.value_field in l.gdf.columns:
            import pandas as pd
            v = pd.to_numeric(l.gdf[l.value_field], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            v = v[np.isfinite(v) & (v > 0)]
            st["proportional"] = {"field": l.value_field, "max_value": float(v.max()) if len(v) else 1.0,
                                  "max_size": round(float(sym.get("max_size", 14)) * PT_MM, 4), "min_size": MIN_SYMBOL_MM}
            st["opacity"] = 0.78
    if label and label.get("field") and label["field"] in l.gdf.columns:
        st["label"] = {"field": label["field"], "size_pt": float(label.get("size") or 7)}
    return st
