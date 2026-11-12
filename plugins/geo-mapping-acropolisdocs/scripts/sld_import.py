#!/usr/bin/env python3
"""sld_import: read an SLD style and say how it maps onto a MapSpec layer (spec `sld-style-import`).

    sld_import.py read  FILE.sld [--style NAME] [--role auto|polygons|lines|points|choropleth] [--layer-id ID] [--out snippet.yaml]
    sld_import.py fetch --profile P --layer L [--out file.sld]          (WMS GetStyles from a public service profile, GET only)

What it keeps: what is styled by which attribute (one symbol, unique values, or numeric ranges), widths, sizes, opacity, dashes, point shapes, and the rule titles
as the legend title. What it does not keep: **the colors**. A map takes its colors from the brand (design principle: the palette guard), so an SLD color is replaced
by a brand color (with `tokens`, the nearest one per category, 1.0.0) and the report lists each replacement. A TextSymbolizer's field and size
become a `labels:` entry, growing widths on ranged line rules become graduated widths, and scale ranges are reported with their web zooms (1.0.0).
Anything the MapSpec cannot say (raster symbolizers, external graphics, filters on several attributes, functions) is listed under `unsupported` and never dropped silently. The SLD is read as data: entities are refused
and nothing in it is followed or run.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402

PT_MM = 0.3528
MAX_RULES = 200
SHAPES = {"circle": "circle", "square": "square", "triangle": "triangle", "star": "diamond", "cross": "cross", "x": "cross", "diamond": "diamond"}
COMPARE = {"PropertyIsLessThan": ("<", "hi"), "PropertyIsLessThanOrEqualTo": ("<=", "hi"), "PropertyIsGreaterThan": (">", "lo"), "PropertyIsGreaterThanOrEqualTo": (">=", "lo")}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def ln(el) -> str:
    t = el.tag
    return t.split("}", 1)[1] if isinstance(t, str) and "}" in t else (t if isinstance(t, str) else "")


def kids(el, *names):
    return [c for c in el if ln(c) in names] if el is not None else []


def first(el, *names):
    k = kids(el, *names)
    return k[0] if k else None


def text_of(el) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return oh.untrusted(t, 200) if t else None


def param(el, name: str):
    """The value of a CssParameter or SvgParameter called `name` under `el` (Fill, Stroke, Graphic Mark...)."""
    if el is None:
        return None
    for c in el:
        if ln(c) in ("CssParameter", "SvgParameter") and c.get("name") == name:
            return text_of(c)
    return None


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------------------------- filters
def literal_number(el):
    v = text_of(first(el, "Literal"))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def prop_name(el) -> str | None:
    p = first(el, "PropertyName", "ValueReference")
    return text_of(p)


def parse_filter(flt):
    """One of ('none',), ('else',), ('eq', prop, [values]), ('range', prop, lo, hi, lo_incl, hi_incl), or ('unsupported', why)."""
    if flt is None:
        return ("none",)
    parts = [c for c in flt]
    if len(parts) != 1:
        return ("unsupported", "an empty or multi-part filter")
    f = parts[0]
    n = ln(f)
    if n == "PropertyIsEqualTo":
        p, lit = prop_name(f), text_of(first(f, "Literal"))
        if p and lit is not None and first(f, "Function") is None:
            return ("eq", p, [lit])
        return ("unsupported", "an equality filter that is not attribute = value")
    if n == "PropertyIsBetween":
        p = prop_name(f)
        lo, hi = num(text_of(first(first(f, "LowerBoundary"), "Literal"))), num(text_of(first(first(f, "UpperBoundary"), "Literal")))
        if p and lo is not None and hi is not None:
            return ("range", p, lo, hi, True, True)
        return ("unsupported", "a between filter that is not attribute and two numbers")
    if n in COMPARE:
        p, v = prop_name(f), literal_number(f)
        if p and v is not None:
            op, side = COMPARE[n]
            return ("range", p, v if side == "lo" else None, v if side == "hi" else None, op == ">=", op == "<=")
        return ("unsupported", "a comparison that is not attribute and a number")
    if n == "Or":
        subs = [parse_filter(_W(c)) for c in f]
        if subs and all(s[0] == "eq" for s in subs) and len({s[1] for s in subs}) == 1:
            return ("eq", subs[0][1], [v for s in subs for v in s[2]])
        return ("unsupported", "an Or of different attributes or kinds")
    if n == "And":
        subs = [parse_filter(_W(c)) for c in f]
        if len(subs) == 2 and all(s[0] == "range" for s in subs) and subs[0][1] == subs[1][1]:
            a, b = subs
            lo = a[2] if a[2] is not None else b[2]
            hi = a[3] if a[3] is not None else b[3]
            lo_i = a[4] if a[2] is not None else b[4]
            hi_i = a[5] if a[3] is not None else b[5]
            if lo is not None and hi is not None:
                return ("range", a[1], lo, hi, lo_i, hi_i)
        return ("unsupported", "an And that is not a range on one attribute")
    return ("unsupported", f"a {n} filter")


class _W:
    """A one-child stand-in so that parse_filter can read a sub-expression as if it were a whole Filter."""

    def __init__(self, c):
        self.c = c

    def __iter__(self):
        return iter([self.c])


# ---------------------------------------------------------------------------------------------------------------- symbolizers
def read_symbolizer(sym) -> dict:
    kind = ln(sym)
    out = {"kind": kind, "unsupported": []}
    if kind == "PolygonSymbolizer":
        fill, stroke = first(sym, "Fill"), first(sym, "Stroke")
        out.update(fill=param(fill, "fill"), fill_opacity=num(param(fill, "fill-opacity")), stroke=param(stroke, "stroke"), stroke_width=num(param(stroke, "stroke-width")),
                   stroke_opacity=num(param(stroke, "stroke-opacity")), dash=param(stroke, "stroke-dasharray"))
        if first(fill, "GraphicFill") is not None:
            out["unsupported"].append("a pattern (GraphicFill) in a polygon fill")
        if first(sym, "PerpendicularOffset") is not None or first(sym, "Displacement") is not None:
            out["unsupported"].append("an offset or displacement")
    elif kind == "LineSymbolizer":
        stroke = first(sym, "Stroke")
        out.update(stroke=param(stroke, "stroke"), stroke_width=num(param(stroke, "stroke-width")), stroke_opacity=num(param(stroke, "stroke-opacity")), dash=param(stroke, "stroke-dasharray"))
        if first(stroke, "GraphicStroke") is not None:
            out["unsupported"].append("a line made of repeated symbols (GraphicStroke)")
        if first(sym, "PerpendicularOffset") is not None:
            out["unsupported"].append("a line offset")
    elif kind == "PointSymbolizer":
        g = first(sym, "Graphic")
        mark = first(g, "Mark")
        out.update(size=num(text_of(first(g, "Size"))), shape=text_of(first(mark, "WellKnownName")), fill=param(first(mark, "Fill"), "fill"), fill_opacity=num(param(first(mark, "Fill"), "fill-opacity")),
                   stroke=param(first(mark, "Stroke"), "stroke"), stroke_width=num(param(first(mark, "Stroke"), "stroke-width")))
        if first(g, "ExternalGraphic") is not None:
            out["unsupported"].append("an external graphic (an image file) as a point symbol")
        if first(g, "Rotation") is not None:
            out["unsupported"].append("a rotation")
    elif kind == "TextSymbolizer":
        lab = first(sym, "Label")
        out["label_field"] = prop_name(lab) if lab is not None else None
        out["label_size"] = num(param(first(sym, "Font"), "font-size"))         # 1.0.0 (TODO M4c): the label field and size go to labels:
        if not out["label_field"]:
            out["unsupported"].append("a text symbolizer whose label is not one attribute")
    elif kind == "RasterSymbolizer":
        out["unsupported"].append("a raster symbolizer")
    elif kind:
        out["unsupported"].append(f"a {kind}")
    return out


def read_rules(root, style: str | None):
    """(style name, rules) with each rule as {name, title, filter, symbolizers, scale, else}. Picks the named UserStyle or the default or the first."""
    styles = [e for e in root.iter() if ln(e) in ("UserStyle", "Style")]
    chosen = None
    names = []
    for s in styles:
        nm = text_of(first(s, "Name"))
        names.append(nm)
        if style and nm == style:
            chosen = s
    if style and chosen is None:
        return None, names, []
    if chosen is None:
        chosen = next((s for s in styles if text_of(first(s, "IsDefault")) in ("1", "true")), styles[0] if styles else None)
    scope = chosen if chosen is not None else root
    rules = []
    for r in [e for e in scope.iter() if ln(e) == "Rule"][:MAX_RULES]:
        has_else = first(r, "ElseFilter") is not None
        rules.append({"name": text_of(first(r, "Name")), "title": text_of(first(r, "Title")), "filter": ("else",) if has_else else parse_filter(first(r, "Filter")),
                      "scale": bool(first(r, "MinScaleDenominator") is not None or first(r, "MaxScaleDenominator") is not None),
                      "scale_range": (num(text_of(first(r, "MinScaleDenominator"))), num(text_of(first(r, "MaxScaleDenominator")))),
                      "symbolizers": [read_symbolizer(s) for s in r if ln(s).endswith("Symbolizer")]})
    return (text_of(first(chosen, "Name")) if chosen is not None else None), names, rules


# ---------------------------------------------------------------------------------------------------------------- mapping
def dash_mm(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return " ".join(f"{float(x) * PT_MM:.2f}".rstrip("0").rstrip(".") for x in re.split(r"[\s,]+", v.strip()) if x)
    except ValueError:
        return None


def luminance(c: str | None) -> float | None:
    if not c or not re.fullmatch(r"#?[0-9a-fA-F]{6}", c.strip()):
        return None
    h = c.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def convert(root, *, style: str | None = None, role: str = "auto", layer_id: str = "styled", source_name: str = "sld", tokens: dict | None = None) -> dict:
    """The mapping. Returns {ok, layer, report, findings}. `layer` is the part of a MapSpec layer this style decides."""
    fs: list[dict] = []
    sname, snames, rules = read_rules(root, style)
    if sname is None and style:
        return {"ok": False, "findings": [finding("MAP-SLD-001", "BLOCK", f"the SLD has no style called '{oh.untrusted(style, 60)}' (it has: {', '.join(oh.untrusted(n, 40) for n in snames if n) or 'no named style'})")]}
    if not rules:
        return {"ok": False, "findings": [finding("MAP-SLD-001", "BLOCK", "no style rules were found in this file; it may not be an SLD")]}
    unsupported: list[dict] = []
    colors_replaced: list[dict] = []
    notes: list[str] = []
    scale_rules: list[dict] = []
    kinds = []
    for r in rules:
        for s in r["symbolizers"]:
            if s["kind"] in ("PolygonSymbolizer", "LineSymbolizer", "PointSymbolizer") and s["kind"] not in kinds:
                kinds.append(s["kind"])
            for u in s["unsupported"]:
                unsupported.append({"rule": r["title"] or r["name"], "what": u})
        if r["scale"]:
            lo_, hi_ = r["scale_range"]
            z = zoom_range(lo_, hi_)
            notes.append(f"rule '{r['title'] or r['name']}' applies " + (f"from 1:{lo_:,.0f} " if lo_ else "") + (f"up to 1:{hi_:,.0f}" if hi_ else "")
                         + f" (web zooms {z[0]} to {z[1]}); a printed map has one scale, so it is drawn when the map's scale is in that range"
                         " and the build says when it is not")
            scale_rules.append({"rule": r["title"] or r["name"], "min_denominator": lo_, "max_denominator": hi_, "zooms": list(z)})
        if r["filter"][0] == "unsupported":
            unsupported.append({"rule": r["title"] or r["name"], "what": f"{r['filter'][1]}; the rule is not used"})
    drawable = [r for r in rules if r["filter"][0] != "unsupported" and any(s["kind"] in ("PolygonSymbolizer", "LineSymbolizer", "PointSymbolizer") for s in r["symbolizers"])]
    if not drawable:
        return {"ok": False, "findings": [finding("MAP-SLD-002", "BLOCK", "no rule in this style draws polygons, lines or points that the MapSpec can express")], "report": {"unsupported": unsupported}}
    if len(kinds) > 1:
        notes.append(f"the style draws {' and '.join(k.replace('Symbolizer', '').lower() + 's' for k in kinds)}; the first kind sets the layer's role. Use a separate layer for the others")
    kind = kinds[0] if kinds else "PolygonSymbolizer"
    base_role = {"PolygonSymbolizer": "polygons", "LineSymbolizer": "lines", "PointSymbolizer": "points"}[kind]
    # --- how the rules divide the data
    cond = [r for r in drawable if r["filter"][0] in ("eq", "range")]
    elses = [r for r in drawable if r["filter"][0] == "else"]
    plain = [r for r in drawable if r["filter"][0] == "none"]
    layer: dict = {"role": base_role}
    mode = "single"
    props = {r["filter"][1] for r in cond}
    kinds_f = {r["filter"][0] for r in cond}
    if cond and (len(props) > 1 or len(kinds_f) > 1):
        fs.append(finding("MAP-SLD-003", "WARN", f"the rules test different attributes or mix values and ranges ({', '.join(sorted(props))}); the MapSpec classifies one attribute per layer. The first rule's look is used for the whole layer; split the layer by filter to keep the rest"))
        unsupported.append({"rule": None, "what": "rules on more than one attribute"})
        cond, mode = [], "single"
    elif cond:
        mode = "categorical" if kinds_f == {"eq"} else "ranges"
        layer["value_field"] = next(iter(props))
    use = cond if cond else (plain or drawable)[:1]
    if mode == "categorical":
        order = []
        for r in cond:
            for v in r["filter"][2]:
                if v not in order:
                    order.append(v)
        grouped = [r for r in cond if len(r["filter"][2]) > 1]
        if grouped:
            notes.append("rules that cover several values each (" + ", ".join(f"'{r['title'] or r['name']}'" for r in grouped[:3]) + ") became one category per value, each with its own brand color")
        layer["classification"] = {"method": "categorical", "order": order, "max_categories": max(len(order), 2)}
        near = nearest_brand_colors(cond, order, tokens) if tokens else None
        if near:                                         # 1.0.0 (TODO M4c): each category takes the brand colour nearest its SLD colour
            layer["classification"]["colors"] = near
            notes.append("each category takes the brand colour nearest to its SLD colour, so a meaning a colour carried (red for down, green for up) "
                         "survives as far as the brand's palette allows")
        if elses:
            notes.append("the rule for everything else (ElseFilter) becomes the 'Other' category")
        layer["palette"] = {"name": "brand_categorical"}
        if len(order) > 8:
            fs.append(finding("MAP-SLD-004", "WARN", f"{len(order)} categories are more than the brand palette keeps apart; the build groups the smallest as Other"))
        if base_role == "polygons":
            layer["role"] = "choropleth"
    elif mode == "ranges":
        rs = sorted(cond, key=lambda r: r["filter"][2] if r["filter"][2] is not None else float("-inf"))
        inner = []
        for i, r in enumerate(rs[:-1]):
            b = r["filter"][3] if r["filter"][3] is not None else rs[i + 1]["filter"][2]
            if b is not None and b not in inner:
                inner.append(b)
        inner = sorted(inner)
        layer["classification"] = {"method": "manual", "breaks": inner}
        lums = [luminance(next((s.get("fill") or s.get("stroke") for s in r["symbolizers"] if s.get("fill") or s.get("stroke")), None)) for r in rs]
        lums = [l for l in lums if l is not None]
        rev = bool(len(lums) >= 2 and lums[0] < lums[-1])
        layer["palette"] = {"name": "brand_sequential", **({"reverse": True} if rev else {})}
        notes.append("the class limits come from the rules; whether a limit is open or closed is not kept (the MapSpec decides, and says in the legend)")
        if base_role == "polygons":
            layer["role"] = "choropleth"
        elif base_role == "lines" and _increasing([_rule_width(r) for r in rs]):
            # 1.0.0 (TODO M4c): ranged line rules whose widths grow are graduated line widths, one per class, in one brand colour
            layer.pop("palette")
            layer["symbol"] = {"graduated": True, "widths_mm": [round(_rule_width(r), 2) for r in rs], "color": "@brand.emphasis"}
            notes.append("the rules' line widths grow with the value: they become graduated widths, one per class, in one brand colour")
        else:
            notes.append("numeric classes color polygons only in this release; for lines or points the first rule's look is used")
            layer.pop("classification"), layer.pop("palette")
            layer.pop("value_field", None)
    # --- one look for the whole layer: widths, sizes, opacity, dash
    syms = [s for r in use for s in r["symbolizers"] if s["kind"] == kind]
    sym: dict = {}
    for s in syms:
        for key in ("fill", "stroke"):
            if s.get(key):
                colors_replaced.append({"sld": s[key], "role": key, "rule": next((r["title"] or r["name"] for r in use if s in r["symbolizers"]), None)})
    widths = [s["stroke_width"] for s in syms if s.get("stroke_width") is not None]
    if base_role == "lines":
        w = statistics.median(widths) if widths else None
        if w is not None:
            sym["width"] = round(w, 2)
        if len({round(x, 2) for x in widths}) > 1 and not (layer.get("symbol") or {}).get("graduated"):
            fs.append(finding("MAP-SLD-005", "INFO", f"line widths differ by rule ({min(widths):g} to {max(widths):g}); the layer uses one width, {sym['width']:g}, the middle one"))
        d = next((dash_mm(s.get("dash")) for s in syms if s.get("dash")), None)
        if d:
            sym["dash"] = d
        sym["color"] = "@brand.emphasis"
    elif base_role == "points":
        sizes = [s["size"] for s in syms if s.get("size") is not None]
        if sizes:
            sym["size"] = round(statistics.median(sizes), 2)
            if len({round(x, 2) for x in sizes}) > 1:
                fs.append(finding("MAP-SLD-005", "INFO", f"point sizes differ by rule ({min(sizes):g} to {max(sizes):g}); the layer uses one size, {sym['size']:g}"))
        raw = next((s.get("shape") for s in syms if s.get("shape")), None)
        shape = SHAPES.get((raw or "").lower())
        if shape:
            sym["shape"] = shape
            if raw.lower() in ("star", "x"):
                notes.append(f"point shape '{raw}' is drawn as a {shape}")
        elif raw:
            unsupported.append({"rule": None, "what": f"the point shape '{raw}'; a circle is used"})
        sym["color"] = "@brand.accent"
    else:
        if widths:
            sym["stroke_width"] = round(statistics.median(widths), 2)
        ops = [s["fill_opacity"] for s in syms if s.get("fill_opacity") is not None]
        if ops and mode == "single":
            sym["opacity"] = round(statistics.median(ops), 2)
        if mode == "single":
            sym["fill"] = "@brand.accent"
        sym["stroke"] = "@brand.frame"
        d = next((dash_mm(s.get("dash")) for s in syms if s.get("dash")), None)
        if d:
            notes.append("a dashed polygon outline is not drawn; the outline is solid")
    if sym and not (layer.get("symbol") or {}).get("graduated"):
        layer["symbol"] = sym
    titles = [r["title"] for r in rules if r["title"]]
    if len(rules) == 1 and titles:
        layer["legend"] = {"title": titles[0]}
    if colors_replaced:
        fs.append(finding("MAP-SLD-006", "INFO", f"{len(colors_replaced)} color(s) in the SLD were not used; the map takes its colors from the brand palette", sorted({c['sld'] for c in colors_replaced})[:10]))
    texts = [s for r in rules for s in r["symbolizers"] if s["kind"] == "TextSymbolizer" and s.get("label_field")]
    labels = []
    if texts:                                            # 1.0.0: the label field and size, as a MapSpec labels: entry
        t0 = texts[0]
        labels = [{"layer": layer_id, "field": t0["label_field"], **({"size": round(float(t0["label_size"]) * 0.75, 1)} if t0.get("label_size") else {})}]
        notes.append(f"labels: the SLD labels each feature with '{t0['label_field']}'" + (f" at {t0['label_size']:g} px ({labels[0]['size']:g} pt)" if t0.get("label_size") else "")
                     + "; the label colour and halo come from the brand")
    if unsupported:
        fs.append(finding("MAP-SLD-007", "WARN", f"{len(unsupported)} part(s) of the SLD cannot be carried into a MapSpec: " + "; ".join(f"{u['what']}" + (f" (rule '{u['rule']}')" if u.get('rule') else "") for u in unsupported[:6]) + (" ..." if len(unsupported) > 6 else "")))
    if role != "auto":
        if role in ("polygons", "lines", "points", "choropleth"):
            layer["role"] = role
    report = {"style": sname, "styles_in_file": snames, "rules": len(rules), "mode": mode, "role": layer["role"], "colors_replaced": colors_replaced, "unsupported": unsupported, "notes": notes,
              "rules_used": [r["title"] or r["name"] for r in use][:20], "scale_rules": scale_rules}
    return {"ok": True, "layer": {"id": layer_id, **layer}, "labels": labels, "report": report, "findings": fs}


def nearest_brand_colors(cond: list, order: list, tokens: dict) -> list[str] | None:
    """For a categorical style: '@brand.categorical.N' per category, each the unused brand categorical colour nearest (Oklab) to the category's
    SLD fill or stroke, nearest pairs first. None when a category has no colour or there are more categories than brand colours."""
    import colorlib as cl
    pal = list(tokens.get("categorical") or [])
    want = {}
    for r in cond:
        c = next((s.get("fill") or s.get("stroke") for s in r["symbolizers"] if s.get("fill") or s.get("stroke")), None)
        for v in r["filter"][2]:
            if c and v not in want:
                want[v] = c
    if len(want) != len(order) or len(order) > len(pal):
        return None
    try:
        pairs = sorted((cl.delta_e(want[v], pc), k, j) for k, v in enumerate(order) for j, pc in enumerate(pal))
    except Exception:  # noqa: BLE001 - a colour the SLD wrote in a form colorlib does not read
        return None
    got: dict = {}
    used = set()
    for _d, k, j in pairs:
        if k in got or j in used:
            continue
        got[k], _ = j, used.add(j)
    return [f"@brand.categorical.{got[k]}" for k in range(len(order))]


def zoom_range(min_den, max_den) -> tuple[int, int]:
    """Web zooms (0 to 22) for an SLD scale range: the OGC standard pixel is 0.28 mm, so zoom z at the equator is about 559,082,264 / 2**z."""
    import math as _m

    def z(den):
        return max(0, min(22, int(round(_m.log2(559_082_264.0 / den))))) if den else None
    lo, hi = z(max_den), z(min_den)
    return (lo if lo is not None else 0, hi if hi is not None else 22)


def _rule_width(r) -> float | None:
    w = [s.get("stroke_width") for s in r["symbolizers"] if s["kind"] == "LineSymbolizer" and s.get("stroke_width") is not None]
    return float(w[0]) * 0.28 if w else None              # SLD widths are pixels of 0.28 mm


def _increasing(ws) -> bool:
    return len(ws) >= 2 and all(w is not None for w in ws) and all(b > a for a, b in zip(ws, ws[1:]))


def snippet(result: dict) -> str:
    import yaml
    doc = {"layers": [{k: v for k, v in result["layer"].items()}]}
    if result.get("labels"):
        doc["labels"] = result["labels"]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def read_file(path: str | Path, **kw) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "findings": [finding("MAP-SLD-001", "BLOCK", f"{p.name} was not found")]}
    data = p.read_bytes()
    if len(data) > 2_000_000:
        return {"ok": False, "findings": [finding("MAP-SLD-001", "BLOCK", "the file is larger than 2 MB; an SLD this size is not a map style")]}
    try:
        root = oh.safe_xml(data)
    except oh.ServiceError as ex:
        return {"ok": False, "findings": [finding("MAP-SLD-001", "BLOCK", str(ex))]}
    r = convert(root, **kw)
    r.setdefault("source", {"file": p.name})
    return r


def fetch_sld(http, profile: dict, layer: str, out: Path | None = None) -> dict:
    """WMS GetStyles for a layer of a public profile. GET only; the SLD is saved and then read like any other."""
    import ogc_client as oc
    ver = "1.1.1"
    r = http.get(oc.endpoint(profile, "wms"), {"service": "WMS", "version": ver, "request": "GetStyles", "layers": layer}, expect="xml")
    out = Path(out) if out else gc.workdir(True) / "styles" / (re.sub(r"[^a-z0-9]+", "-", f"{profile['profile']}-{layer}".lower()).strip("-") + ".sld")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return {"ok": True, "path": str(out), "bytes": len(r.content), "sha256": r.sha256}


def main(argv=None, http=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    r = sub.add_parser("read")
    r.add_argument("file")
    r.add_argument("--style")
    r.add_argument("--role", default="auto", choices=["auto", "polygons", "lines", "points", "choropleth"])
    r.add_argument("--layer-id", default="styled")
    r.add_argument("--brand", help="a brand pack: each category takes the brand colour nearest its SLD colour (1.0.0)")
    r.add_argument("--out")
    f = sub.add_parser("fetch")
    f.add_argument("--profile", required=True)
    f.add_argument("--layer", required=True)
    f.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        if a.op == "fetch":
            import service_query as sq
            prof = sq.need_mcp_profile(a.profile)
            res = fetch_sld(http or sq.make_http(prof), prof, a.layer, a.out)
            print(json.dumps(res, indent=2))
            return 0
        toks = None
        if a.brand:
            import brandlib as bl
            toks = bl.load_pack(a.brand)["tokens"]["modes"]["print"]
        res = read_file(a.file, style=a.style, role=a.role, layer_id=a.layer_id, tokens=toks)
        if res.get("ok") and a.out:
            Path(a.out).write_text(snippet(res))
            res["snippet"] = a.out
        elif res.get("ok"):
            res["snippet_text"] = snippet(res)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 3
    except oh.ServiceError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule or "MAP-SVC-018", "BLOCK", oh.untrusted(ex, 300))]}))
        return 5 if ex.auth else 3
    except FileNotFoundError as ex:
        print(json.dumps({"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", oh.untrusted(ex, 300))]}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
