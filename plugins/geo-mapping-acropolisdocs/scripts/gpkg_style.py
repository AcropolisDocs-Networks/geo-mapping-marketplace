#!/usr/bin/env python3
"""gpkg_style: a GeoPackage that opens styled in QGIS (M14, gap analysis P2-7). The OGC `layer_styles` table QGIS reads on its own, with a
QML style (what QGIS itself writes) and an SLD 1.1 style beside it, built from a style_dict style.

    import gpkg_style as gs
    rep = gs.write_styled([("grid", gdf, style), ("sites", gdf2, style2)], "map.gpkg")      # the layers and their default styles
    gs.write_styles("existing.gpkg", "grid", style)                                         # a default style for a table already there
    gs.qml(style) -> str;  gs.sld(style, "grid") -> str;  gs.renderer_type(qml_text) -> str
    gs.check("map.gpkg", ["grid", "sites"])                                                 # MAP-OUT-010

The QML renderers, the same classes the map draws:
  * single     -> singleSymbol: one fill, line or marker symbol (outline, widths and sizes in millimetres, the fill opacity in the colour);
                  proportional points carry a data-defined size, area proportional to the value with the map's minimum size;
  * categorical-> categorizedSymbol on the field: one category per class, in the legend's order with its label. The catch-all category
                  (an empty value, which QGIS uses for every value not listed) takes the "Other" class when there is one, else the no-data
                  colour; when there are both, missing values are told apart with coalesce(field, '(no data)');
  * classed    -> graduatedSymbol on the field: one range per class with its legend label. QGIS puts a value equal to a break in the lower
                  range, the map in the upper one, so each upper bound is written a hair (one floating-point step) below the next break; the
                  first and last ranges reach the smallest and largest value, as the map clamps values beyond the edges into them; features
                  with no value get a "No data" range through coalesce(field, -1e99), so they are drawn in the no-data colour as on the map.
  * widths     -> (M16) a classed lines layer drawn in graduated widths: the graduated renderer on width (graduatedMethod GraduatedSize),
                  each range's line symbol at its class's width; the SLD rules carry the same widths;
  * bivariate  -> (M16) RuleRenderer: one rule per n x n class on both fields (the map's edges, a value on an inner edge in the upper class,
                  values beyond the outer edges in the end classes) with its colour and label, and a no-data rule; the SLD has the same rules.
Point shapes: circle, square, triangle, diamond, cross (QGIS cross_fill). A label (field and size) becomes simple labelling in Inter with a
halo. The SLD (SE 1.1) has one rule per class with ogc:Filter (PropertyIsEqualTo, ranges, PropertyIsNull, ElseFilter), sizes and widths in
pixels as QGIS exports them.

`layer_styles` is created as QGIS creates it (an attribute table registered in gpkg_contents, FID column `id`), with f_table_catalog and
f_table_schema empty strings (QGIS matches on f_table_schema='') and f_geometry_column the table's geometry column, `useAsDefault` true.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import style_dict as sd  # noqa: E402

QGIS_VERSION = "3.28.0-Firenze"                 # the oldest QGIS the plugin's QGIS paths are written for
MARKERS = {"circle": "circle", "square": "square", "triangle": "triangle", "diamond": "diamond", "cross": "cross_fill"}
SLD_MARKS = {"circle": ("circle", 0), "square": ("square", 0), "triangle": ("triangle", 0), "diamond": ("square", 45), "cross": ("cross", 0)}
NODATA_NUMBER = -1e99
NODATA_TEXT = "(no data)"
GEOM_TYPE = {"point": "0", "line": "1", "polygon": "2"}
METHODS = {"quantile": "Quantile", "equal": "EqualInterval", "equal_interval": "EqualInterval", "jenks": "Jenks", "std_dev": "StdDev", "pretty": "Pretty"}
SLD_NS = "http://www.opengis.net/sld"
SE_NS = "http://www.opengis.net/se"
OGC_NS = "http://www.opengis.net/ogc"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


class GpkgStyleError(ValueError):
    """A refusal with a message that is safe to show the person."""


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _E():
    from lxml import etree
    return etree


# ---------------------------------------------------------------------------------------------------------------- QML
def _opt(parent, name: str, value, typ: str = "QString"):
    E = _E()
    return E.SubElement(parent, "Option", type=typ, name=name, value=str(value))


def _ddp(parent, size_expr: str | None = None):
    """<data_defined_properties>: empty, or a data-defined marker size."""
    E = _E()
    d = E.SubElement(parent, "data_defined_properties")
    o = E.SubElement(d, "Option", type="Map")
    _opt(o, "name", "")
    props = E.SubElement(o, "Option", name="properties", type="Map" if size_expr else "invalid")
    if size_expr:
        s = E.SubElement(props, "Option", type="Map", name="size")
        _opt(s, "active", "true", "bool")
        _opt(s, "expression", size_expr)
        _opt(s, "type", "3", "int")
    _opt(o, "type", "collection")
    return d


def _num(x: float) -> str:
    x = float(x)
    return str(int(x)) if x.is_integer() and abs(x) < 1e15 else repr(x)


def _class_width(style: dict, i: int | None) -> dict:
    """The style with class i's own width when it has per-class `widths` (graduated lines, M16)."""
    w = style.get("widths")
    return {**style, "width": w[i]} if w and i is not None and 0 <= i < len(w) else style


QML_HATCH = {"hatch": ((45,), 1.6), "hatch_dense": ((45,), 0.9), "back_hatch": ((135,), 1.6), "horizontal": ((0,), 1.6), "vertical": ((90,), 1.6),
             "cross": ((45, 135), 1.6)}
SLD_HATCH = {"hatch": "shape://slash", "hatch_dense": "shape://slash", "back_hatch": "shape://backslash", "horizontal": "shape://horline",
             "vertical": "shape://vertline", "cross": "shape://times", "dots": "shape://dot"}


def _class_pattern(style: dict, i: int | None):
    """(pattern, ink) of class i, or (None, None)."""
    pats, inks = style.get("patterns") or [], style.get("pattern_inks") or []
    if i is None or i >= len(pats) or pats[i] in (None, "none"):
        return None, None
    return pats[i], (inks[i] if i < len(inks) else None)


def _hatch_layers(sym, name: str, pattern: str, ink: str) -> None:
    """1.0.0 (TODO M15): the class's hatch as QGIS line-pattern (or point-pattern) fill layers over its colour, at the map's spacing, width and
    opacity (patterns.py), so the QGIS project reads like the printed map."""
    import patterns as pt_
    E = _E()
    line_style = {"geometry": "line", "width": pt_.LINE_W_MM, "opacity": pt_.OPACITY}
    if pattern == "dots":
        lay = E.SubElement(sym, "layer", **{"class": "PointPatternFill", "enabled": "1", "locked": "0", "pass": "0"})
        o = E.SubElement(lay, "Option", type="Map")
        for k, v in (("distance_x", "1.4"), ("distance_y", "1.4"), ("distance_x_unit", "MM"), ("distance_y_unit", "MM"), ("displacement_x", "0"),
                     ("displacement_y", "0"), ("offset_x", "0"), ("offset_y", "0")):
            _opt(o, k, v)
        _ddp(lay)
        lay.append(symbol({"geometry": "point", "width": 0.0, "size": 0.48, "opacity": min(1.0, pt_.OPACITY + 0.15), "shape": "circle"}, ink,
                          name=f"@{name}@{len(sym.findall('layer')) - 1}"))
        return
    angles, dist = QML_HATCH[pattern]
    for a in angles:
        lay = E.SubElement(sym, "layer", **{"class": "LinePatternFill", "enabled": "1", "locked": "0", "pass": "0"})
        o = E.SubElement(lay, "Option", type="Map")
        for k, v in (("angle", str(a)), ("distance", _num(dist)), ("distance_unit", "MM"), ("line_width", _num(pt_.LINE_W_MM)), ("line_width_unit", "MM"),
                     ("offset", "0"), ("offset_unit", "MM"), ("color", sd.qgis_color(ink, pt_.OPACITY))):
            _opt(o, k, v)
        _ddp(lay)
        lay.append(symbol(line_style, ink, name=f"@{name}@{len(sym.findall('layer')) - 1}"))


def symbol(style: dict, color, *, name: str, shape: str | None = None, size_expr: str | None = None, cls: int | None = None):
    """One QGIS symbol element (fill, line or marker) for a class colour; `cls` (1.0.0) adds that class's hatch to a fill."""
    E = _E()
    kind = style["geometry"]
    op = float(style.get("opacity", 1.0))
    typ = {"polygon": "fill", "line": "line", "point": "marker"}[kind]
    sym = E.Element("symbol", type=typ, name=name, alpha="1", clip_to_extent="1", force_rhr="0", is_animated="0", frame_rate="10")
    _ddp(sym)
    if kind == "polygon":
        lay = E.SubElement(sym, "layer", **{"class": "SimpleFill", "enabled": "1", "locked": "0", "pass": "0"})
        o = E.SubElement(lay, "Option", type="Map")
        stroke = style.get("stroke")
        _opt(o, "border_width_map_unit_scale", "3x:0,0,0,0,0,0")
        _opt(o, "color", sd.qgis_color(color, op) if color else "0,0,0,0")
        _opt(o, "joinstyle", "round")
        _opt(o, "offset", "0,0")
        _opt(o, "offset_unit", "MM")
        _opt(o, "outline_color", sd.qgis_color(stroke) if stroke else "0,0,0,0")
        _opt(o, "outline_style", "solid" if stroke else "no")
        _opt(o, "outline_width", _num(style["width"]))
        _opt(o, "outline_width_unit", "MM")
        _opt(o, "style", "solid" if color else "no")
        _ddp(lay)
        pat, ink = _class_pattern(style, cls)
        if pat and ink:
            _hatch_layers(sym, name, pat, ink)
    elif kind == "line":
        lay = E.SubElement(sym, "layer", **{"class": "SimpleLine", "enabled": "1", "locked": "0", "pass": "0"})
        o = E.SubElement(lay, "Option", type="Map")
        dash = [x for x in str(style.get("dash") or "").replace(",", " ").split() if x]
        _opt(o, "capstyle", "round")
        _opt(o, "customdash", ";".join(dash) if dash else "5;2")
        _opt(o, "customdash_unit", "MM")
        _opt(o, "joinstyle", "round")
        _opt(o, "line_color", sd.qgis_color(color, op))
        _opt(o, "line_style", "solid")
        _opt(o, "line_width", _num(style["width"]))
        _opt(o, "line_width_unit", "MM")
        _opt(o, "offset", "0")
        _opt(o, "offset_unit", "MM")
        _opt(o, "use_custom_dash", "1" if dash else "0")
        _ddp(lay)
    else:
        lay = E.SubElement(sym, "layer", **{"class": "SimpleMarker", "enabled": "1", "locked": "0", "pass": "0"})
        o = E.SubElement(lay, "Option", type="Map")
        stroke = style.get("stroke")
        _opt(o, "angle", "0")
        _opt(o, "color", sd.qgis_color(color, op))
        _opt(o, "horizontal_anchor_point", "1")
        _opt(o, "joinstyle", "miter")
        _opt(o, "name", MARKERS[shape or style.get("shape", "circle")])
        _opt(o, "offset", "0,0")
        _opt(o, "offset_unit", "MM")
        _opt(o, "outline_color", sd.qgis_color(stroke) if stroke else "0,0,0,0")
        _opt(o, "outline_style", "solid" if stroke else "no")
        _opt(o, "outline_width", _num(style["width"]))
        _opt(o, "outline_width_unit", "MM")
        _opt(o, "scale_method", "diameter")
        _opt(o, "size", _num(style["size"]))
        _opt(o, "size_unit", "MM")
        _opt(o, "vertical_anchor_point", "1")
        _ddp(lay, size_expr)
    return sym


def _q(field: str) -> str:
    return '"' + str(field).replace('"', '""') + '"'


def _qgis_text(v: str, numeric_float: bool) -> str:
    """A category value as QGIS writes a feature's value as text: a whole float has no '.0'."""
    if numeric_float:
        try:
            x = float(v)
            if x.is_integer():
                return str(int(x))
        except ValueError:
            pass
    return v


def proportional_expr(style: dict) -> str:
    p = style["proportional"]
    f = _q(p["field"])
    return (f"CASE WHEN {f} > 0 THEN max({_num(p['min_size'])}, {_num(p['max_size'])} * sqrt({f} / {_num(p['max_value'])})) ELSE 0 END")


def bivariate_rules(style: dict) -> list[tuple[str, str, str]]:
    """(QGIS filter expression, label, colour) for each of the n x n bivariate classes, k = y class * n + x class, with the map's rule: a value
    on an inner edge goes to the upper class, values beyond the outer edges to the end classes; then the no-data rule (either field empty)."""
    b = style["bivariate"]
    n = b["n"]

    def cond(field: str, edges: list, i: int) -> str:
        f = _q(field)
        parts = [f"{f} IS NOT NULL"]
        if i > 0:
            parts.append(f"{f} >= {_num(edges[i])}")
        if i < n - 1:
            parts.append(f"{f} < {_num(edges[i + 1])}")
        return " AND ".join(parts)
    out = []
    for j in range(n):
        for i in range(n):
            k = j * n + i
            out.append((f"({cond(b['x']['field'], b['x']['edges'], i)}) AND ({cond(b['y']['field'], b['y']['edges'], j)})", b["labels"][k], b["colors"][k]))
    out.append((f"{_q(b['x']['field'])} IS NULL OR {_q(b['y']['field'])} IS NULL", style.get("nodata_label", sd.NODATA_LABEL), style["nodata_color"]))
    return out


def renderer(style: dict, *, field_is_float: bool = False):
    E = _E()
    mode = style["mode"]
    common = {"forceraster": "0", "symbollevels": "0", "enableorderby": "0", "referencescale": "-1"}
    size_expr = proportional_expr(style) if style.get("proportional") else None
    if style.get("bivariate"):                                    # M16: the n x n classes as a rule-based renderer on the two fields
        import hashlib
        r = E.Element("renderer-v2", type="RuleRenderer", **common)
        rules = E.SubElement(r, "rules", key="{" + hashlib.md5(b"gm-rules").hexdigest() + "}")
        syms = E.SubElement(r, "symbols")
        for i, (expr, lab, col) in enumerate(bivariate_rules(style)):
            E.SubElement(rules, "rule", filter=expr, symbol=str(i), label=str(lab), key="{" + hashlib.md5(f"gm-rule-{i}".encode()).hexdigest() + "}")
            syms.append(symbol(style, col, name=str(i), cls=i))
        E.SubElement(r, "rotation")
        E.SubElement(r, "sizescale")
        return r
    if mode == "single":
        r = E.Element("renderer-v2", type="singleSymbol", **common)
        syms = E.SubElement(r, "symbols")
        syms.append(symbol(style, style.get("color"), name="0", size_expr=size_expr))
    elif mode == "categorical":
        cats = [str(c) for c in style["categories"]]
        other = bool(style.get("other"))
        nodata = int(style.get("nodata") or 0) > 0
        attr = style["field"]
        if other and nodata:
            attr = f"coalesce({_q(style['field'])}, '{NODATA_TEXT}')"
        r = E.Element("renderer-v2", type="categorizedSymbol", attr=attr, **common)
        ce = E.SubElement(r, "categories")
        syms = E.SubElement(r, "symbols")
        n = 0
        listed = cats[:-1] if other else cats
        for i, (cat, lab, col) in enumerate(zip(listed, style["labels"], style["colors"])):
            E.SubElement(ce, "category", value=_qgis_text(cat, field_is_float), symbol=str(n), label=str(lab), render="true", type="string")
            syms.append(symbol(style, col, name=str(n), shape=sd.class_shape(style, i), cls=i))
            n += 1
        if other and nodata:
            E.SubElement(ce, "category", value=NODATA_TEXT, symbol=str(n), label=style.get("nodata_label", sd.NODATA_LABEL), render="true", type="string")
            syms.append(symbol(style, style["nodata_color"], name=str(n)))
            n += 1
        if other:
            i = len(cats) - 1
            E.SubElement(ce, "category", value="", symbol=str(n), label=str(style["labels"][i]), render="true", type="string")
            syms.append(symbol(style, style["colors"][i], name=str(n), shape=sd.class_shape(style, i), cls=i))
        elif nodata:
            E.SubElement(ce, "category", value="", symbol=str(n), label=style.get("nodata_label", sd.NODATA_LABEL), render="true", type="string")
            syms.append(symbol(style, style["nodata_color"], name=str(n)))
        src = E.SubElement(r, "source-symbol")
        src.append(symbol(style, style["colors"][0], name="0"))
    else:
        edges = [float(e) for e in style["edges"]]
        k = len(edges) - 1
        lo_all, hi_all = edges[0], edges[-1]
        if style.get("value_range"):
            lo_all = min(lo_all, float(style["value_range"][0]))
            hi_all = max(hi_all, float(style["value_range"][1]))
        nodata = int(style.get("nodata") or 0) > 0
        attr = f"coalesce({_q(style['field'])}, {NODATA_NUMBER:g})" if nodata else style["field"]
        sized = bool(style.get("widths"))                         # M16: graduated line widths are a graduated renderer on width
        r = E.Element("renderer-v2", type="graduatedSymbol", attr=attr, graduatedMethod="GraduatedSize" if sized else "GraduatedColor", **common)
        re_ = E.SubElement(r, "ranges")
        syms = E.SubElement(r, "symbols")
        for i in range(k):
            lo = lo_all if i == 0 else edges[i]
            hi = hi_all if i == k - 1 else math.nextafter(edges[i + 1], -math.inf)
            E.SubElement(re_, "range", lower=repr(float(lo)), upper=repr(float(hi)), symbol=str(i), label=str(style["labels"][i]), render="true")
            syms.append(symbol(_class_width(style, i), style["colors"][i], name=str(i), shape=sd.class_shape(style, i), cls=i))
        if nodata:
            E.SubElement(re_, "range", lower=repr(NODATA_NUMBER), upper=repr(NODATA_NUMBER), symbol=str(k), label=style.get("nodata_label", sd.NODATA_LABEL),
                         render="true")
            syms.append(symbol(_class_width(style, 0), style["nodata_color"], name=str(k)))
        src = E.SubElement(r, "source-symbol")
        src.append(symbol(style, style["colors"][0], name="0"))
        m = METHODS.get(str(style.get("method") or "").lower().replace(" ", "_"))
        if m:
            E.SubElement(r, "classificationMethod", id=m)
    E.SubElement(r, "rotation")
    E.SubElement(r, "sizescale")
    return r


def labeling(style: dict):
    E = _E()
    lb = style["label"]
    color = sd.qgis_color(lb.get("color") or sd.FALLBACK_RGB["label"])
    halo = sd.qgis_color(lb.get("halo") or sd.FALLBACK_RGB["legend_bg"])
    el = E.Element("labeling", type="simple")
    s = E.SubElement(el, "settings", calloutType="simple")
    ts = E.SubElement(s, "text-style", fieldName=str(lb["field"]), isExpression="0", fontFamily="Inter", fontSize=_num(lb.get("size_pt", 7)),
                      fontSizeUnit="Point", textColor=color, textOpacity="1", fontWeight="50", fontItalic="0", namedStyle="Regular")
    E.SubElement(ts, "text-buffer", bufferDraw="1", bufferSize="0.8", bufferSizeUnits="MM", bufferColor=halo, bufferOpacity="1", bufferJoinStyle="128")
    E.SubElement(s, "placement", placement={"point": "0", "line": "2", "polygon": "0"}[style["geometry"]], dist="1", distUnits="MM")
    E.SubElement(s, "rendering", obstacle="1", scaleVisibility="0", displayAll="0")
    return el


def qml_root(style: dict, *, field_is_float: bool = False):
    """The <qgis> element of a QML style for a resolved style dict."""
    E = _E()
    labels = bool(style.get("label"))
    root = E.Element("qgis", version=QGIS_VERSION, styleCategories="Symbology|Labeling", labelsEnabled="1" if labels else "0")
    root.append(renderer(style, field_is_float=field_is_float))
    if labels:
        root.append(labeling(style))
    E.SubElement(root, "blendMode").text = "0"
    E.SubElement(root, "featureBlendMode").text = "0"
    E.SubElement(root, "layerGeometryType").text = GEOM_TYPE[style["geometry"]]
    return root


def qml(style: dict, *, field_is_float: bool = False) -> str:
    E = _E()
    body = E.tostring(qml_root(style, field_is_float=field_is_float), pretty_print=True, encoding="unicode")
    return "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" + body


def renderer_type(qml_text: str) -> str | None:
    E = _E()
    root = E.fromstring(qml_text.split("\n", 1)[1].encode() if qml_text.startswith("<!DOCTYPE") else qml_text.encode(),
                        E.XMLParser(resolve_entities=False, no_network=True))
    r = root.find("renderer-v2")
    return r.get("type") if r is not None else None


# ---------------------------------------------------------------------------------------------------------------- SLD
def _se(tag: str) -> str:
    return "{%s}%s" % (SE_NS, tag)


def _ogc(tag: str) -> str:
    return "{%s}%s" % (OGC_NS, tag)


def _param(parent, name: str, value):
    E = _E()
    p = E.SubElement(parent, _se("SvgParameter"), name=name)
    p.text = str(value)
    return p


def _px(mm: float) -> str:
    return f"{float(mm) * sd.MM_PX:.2f}".rstrip("0").rstrip(".")


def _sld_hatch(rule, pattern: str, ink: str) -> None:
    """1.0.0 (TODO M15): the class's hatch as a second PolygonSymbolizer with a GraphicFill of GeoServer's shape:// marks (other servers draw
    the colour fill and ignore the hatch)."""
    import patterns as pt_
    E = _E()
    ps = E.SubElement(rule, _se("PolygonSymbolizer"))
    g = E.SubElement(E.SubElement(E.SubElement(ps, _se("Fill")), _se("GraphicFill")), _se("Graphic"))
    mk = E.SubElement(g, _se("Mark"))
    E.SubElement(mk, _se("WellKnownName")).text = SLD_HATCH[pattern]
    if pattern == "dots":
        f = E.SubElement(mk, _se("Fill"))
        _param(f, "fill", sd.hex_rgb(ink))
        _param(f, "fill-opacity", f"{pt_.OPACITY:g}")
    else:
        s = E.SubElement(mk, _se("Stroke"))
        _param(s, "stroke", sd.hex_rgb(ink))
        _param(s, "stroke-width", _px(pt_.LINE_W_MM))
        _param(s, "stroke-opacity", f"{pt_.OPACITY:g}")
    E.SubElement(g, _se("Size")).text = _px(0.9 if pattern == "hatch_dense" else (1.4 if pattern == "dots" else 1.6))


def _sld_symbolizer(rule, style: dict, color, shape: str | None = None, cls: int | None = None):
    E = _E()
    kind = style["geometry"]
    op = float(style.get("opacity", 1.0))
    stroke = style.get("stroke")
    if kind == "polygon":
        ps = E.SubElement(rule, _se("PolygonSymbolizer"))
        if color:
            f = E.SubElement(ps, _se("Fill"))
            _param(f, "fill", sd.hex_rgb(color))
            if op < 1:
                _param(f, "fill-opacity", f"{op:g}")
        if stroke:
            s = E.SubElement(ps, _se("Stroke"))
            _param(s, "stroke", sd.hex_rgb(stroke))
            _param(s, "stroke-width", _px(style["width"]))
            _param(s, "stroke-linejoin", "round")
        pat, ink = _class_pattern(style, cls)
        if pat and ink:
            _sld_hatch(rule, pat, ink)
    elif kind == "line":
        ls = E.SubElement(rule, _se("LineSymbolizer"))
        s = E.SubElement(ls, _se("Stroke"))
        _param(s, "stroke", sd.hex_rgb(color))
        if op < 1:
            _param(s, "stroke-opacity", f"{op:g}")
        _param(s, "stroke-width", _px(style["width"]))
        _param(s, "stroke-linejoin", "round")
        _param(s, "stroke-linecap", "round")
        dash = [x for x in str(style.get("dash") or "").replace(",", " ").split() if x]
        if dash:
            _param(s, "stroke-dasharray", " ".join(_px(float(x)) for x in dash))
    else:
        pt = E.SubElement(rule, _se("PointSymbolizer"))
        g = E.SubElement(pt, _se("Graphic"))
        mk = E.SubElement(g, _se("Mark"))
        wkn, rot = SLD_MARKS[shape or style.get("shape", "circle")]
        E.SubElement(mk, _se("WellKnownName")).text = wkn
        f = E.SubElement(mk, _se("Fill"))
        _param(f, "fill", sd.hex_rgb(color))
        if op < 1:
            _param(f, "fill-opacity", f"{op:g}")
        if stroke:
            s = E.SubElement(mk, _se("Stroke"))
            _param(s, "stroke", sd.hex_rgb(stroke))
            _param(s, "stroke-width", _px(style["width"]))
        size = E.SubElement(g, _se("Size"))
        p = style.get("proportional")
        if p:
            mul = E.SubElement(size, _ogc("Mul"))
            E.SubElement(mul, _ogc("Literal")).text = _px(p["max_size"])
            fn = E.SubElement(mul, _ogc("Function"), name="sqrt")
            div = E.SubElement(fn, _ogc("Div"))
            E.SubElement(div, _ogc("PropertyName")).text = p["field"]
            E.SubElement(div, _ogc("Literal")).text = _num(p["max_value"])
        else:
            size.text = _px(style["size"])
        if rot:
            E.SubElement(g, _se("Rotation")).text = str(rot)


def _cmp(parent, op: str, field: str, value):
    E = _E()
    c = E.SubElement(parent, _ogc(op))
    E.SubElement(c, _ogc("PropertyName")).text = field
    E.SubElement(c, _ogc("Literal")).text = _num(value) if isinstance(value, (int, float)) else str(value)
    return c


def sld(style: dict, layer_name: str) -> str:
    """An SLD 1.1 (Symbology Encoding 1.1) document for a resolved style dict: one rule per class, the no-data rule last."""
    E = _E()
    nsmap = {None: SLD_NS, "se": SE_NS, "ogc": OGC_NS, "xlink": XLINK_NS, "xsi": XSI_NS}
    root = E.Element("{%s}StyledLayerDescriptor" % SLD_NS, nsmap=nsmap, version="1.1.0")
    root.set("{%s}schemaLocation" % XSI_NS, "http://www.opengis.net/sld http://schemas.opengis.net/sld/1.1.0/StyledLayerDescriptor.xsd")
    nl = E.SubElement(root, "{%s}NamedLayer" % SLD_NS)
    E.SubElement(nl, _se("Name")).text = layer_name
    us = E.SubElement(nl, "{%s}UserStyle" % SLD_NS)
    E.SubElement(us, _se("Name")).text = layer_name
    fts = E.SubElement(us, _se("FeatureTypeStyle"))
    mode = style["mode"]
    f = style.get("field")

    def rule(name: str, title: str):
        r = E.SubElement(fts, _se("Rule"))
        E.SubElement(r, _se("Name")).text = name
        d = E.SubElement(r, _se("Description"))
        E.SubElement(d, _se("Title")).text = title
        return r

    if style.get("bivariate"):                                    # M16: one rule per bivariate class, on both fields
        b = style["bivariate"]
        n = b["n"]
        for j in range(n):
            for i in range(n):
                kk = j * n + i
                r = rule(f"class {kk + 1}", b["labels"][kk])
                a = E.SubElement(E.SubElement(r, _ogc("Filter")), _ogc("And"))
                for fld, edges, c in ((b["x"]["field"], b["x"]["edges"], i), (b["y"]["field"], b["y"]["edges"], j)):
                    nn = E.SubElement(E.SubElement(a, _ogc("Not")), _ogc("PropertyIsNull"))
                    E.SubElement(nn, _ogc("PropertyName")).text = fld
                    if c > 0:
                        _cmp(a, "PropertyIsGreaterThanOrEqualTo", fld, edges[c])
                    if c < n - 1:
                        _cmp(a, "PropertyIsLessThan", fld, edges[c + 1])
                _sld_symbolizer(r, style, b["colors"][kk], cls=kk)
        r = rule("no data", style.get("nodata_label", sd.NODATA_LABEL))
        o = E.SubElement(E.SubElement(r, _ogc("Filter")), _ogc("Or"))
        for fld in (b["x"]["field"], b["y"]["field"]):
            E.SubElement(E.SubElement(o, _ogc("PropertyIsNull")), _ogc("PropertyName")).text = fld
        _sld_symbolizer(r, style, style["nodata_color"])
    elif mode == "single":
        r = rule("single", str(style.get("title") or layer_name))
        _sld_symbolizer(r, style, style.get("color"))
    elif mode == "categorical":
        cats = [str(c) for c in style["categories"]]
        other = bool(style.get("other"))
        listed = cats[:-1] if other else cats
        for i, (cat, lab, col) in enumerate(zip(listed, style["labels"], style["colors"], strict=True)):
            r = rule(f"class {i + 1}", str(lab))
            flt = E.SubElement(r, _ogc("Filter"))
            _cmp(flt, "PropertyIsEqualTo", f, cat)
            _sld_symbolizer(r, style, col, sd.class_shape(style, i), cls=i)
        if int(style.get("nodata") or 0):
            r = rule("no data", style.get("nodata_label", sd.NODATA_LABEL))
            flt = E.SubElement(r, _ogc("Filter"))
            n = E.SubElement(flt, _ogc("PropertyIsNull"))
            E.SubElement(n, _ogc("PropertyName")).text = f
            _sld_symbolizer(r, style, style["nodata_color"])
        if other:
            i = len(cats) - 1
            r = rule(f"class {i + 1}", str(style["labels"][i]))
            E.SubElement(r, _se("ElseFilter"))
            _sld_symbolizer(r, style, style["colors"][i], sd.class_shape(style, i), cls=i)
    else:
        edges = [float(e) for e in style["edges"]]
        k = len(edges) - 1
        for i in range(k):
            r = rule(f"class {i + 1}", str(style["labels"][i]))
            flt = E.SubElement(r, _ogc("Filter"))
            if k == 1:
                n = E.SubElement(E.SubElement(flt, _ogc("Not")), _ogc("PropertyIsNull"))
                E.SubElement(n, _ogc("PropertyName")).text = f
            elif i == 0:
                _cmp(flt, "PropertyIsLessThan", f, edges[1])
            elif i == k - 1:
                _cmp(flt, "PropertyIsGreaterThanOrEqualTo", f, edges[i])
            else:
                a = E.SubElement(flt, _ogc("And"))
                _cmp(a, "PropertyIsGreaterThanOrEqualTo", f, edges[i])
                _cmp(a, "PropertyIsLessThan", f, edges[i + 1])
            _sld_symbolizer(r, _class_width(style, i), style["colors"][i], sd.class_shape(style, i), cls=i)
        if int(style.get("nodata") or 0):
            r = rule("no data", style.get("nodata_label", sd.NODATA_LABEL))
            flt = E.SubElement(r, _ogc("Filter"))
            n = E.SubElement(flt, _ogc("PropertyIsNull"))
            E.SubElement(n, _ogc("PropertyName")).text = f
            _sld_symbolizer(r, style, style["nodata_color"])
    return E.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")


# ---------------------------------------------------------------------------------------------------------------- layer_styles
def geometry_column(gpkg, table: str) -> str:
    con = sqlite3.connect(f"file:{Path(gpkg).as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def _now():
    import pandas as pd
    fixed = os.environ.get("GMAP_FIXED_DATE")
    t = dt.datetime.fromisoformat(fixed + "T00:00:00+00:00") if fixed else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return pd.Timestamp(t)


def _float_field(gpkg, table: str, field: str | None) -> bool:
    if not field:
        return False
    try:
        import pyogrio
        info = pyogrio.read_info(gpkg, layer=table)
        types = dict(zip(info["fields"], info["dtypes"], strict=True))
        return str(types.get(field, "")).startswith("float")
    except Exception:  # noqa: BLE001
        return False


def write_styles(gpkg, table: str, style: dict, *, style_name: str | None = None, description: str = "", default: bool = True,
                 owner: str = "") -> dict:
    """Add (or replace) the style of `table` in the GeoPackage's layer_styles table. `style` must be resolved (style_dict.resolve)."""
    import pandas as pd
    import pyogrio
    p = Path(gpkg)
    if not p.is_file():
        raise GpkgStyleError(f"{p.name} does not exist")
    layers = [x[0] for x in pyogrio.list_layers(p)]
    if table not in layers:
        raise GpkgStyleError(f"{p.name} has no table '{table}'")
    name = style_name or table
    q = qml(style, field_is_float=_float_field(p, table, style.get("field")))
    s = sld(style, table)
    row = pd.DataFrame([{"f_table_catalog": "", "f_table_schema": "", "f_table_name": table, "f_geometry_column": geometry_column(p, table),
                         "styleName": name, "styleQML": q, "styleSLD": s, "useAsDefault": bool(default),
                         "description": description or str(style.get("title") or table), "owner": owner, "ui": "", "update_time": _now()}])
    if "layer_styles" in layers:
        con = sqlite3.connect(str(p))
        try:
            with con:
                con.execute("DELETE FROM layer_styles WHERE f_table_name = ? AND styleName = ?", (table, name))
                if default:
                    con.execute("UPDATE layer_styles SET useAsDefault = 0 WHERE f_table_name = ?", (table,))
        finally:
            con.close()
        pyogrio.write_dataframe(row, p, layer="layer_styles", driver="GPKG", append=True)
    else:
        pyogrio.write_dataframe(row, p, layer="layer_styles", driver="GPKG", append=True, layer_options={"FID": "id"})
    return {"table": table, "style": name, "renderer": renderer_type(q), "qml_bytes": len(q), "sld_bytes": len(s)}


def write_styled(items: list[tuple], path, *, tokens: dict | None = None, crs: str | None = "EPSG:4326", hidden: dict | None = None,
                 overwrite: bool = True) -> dict:
    """One GeoPackage with every layer (a table named after the layer) and its default style. items: [(name, GeoDataFrame, style dict)].
    Returns {'path', 'layers': {name: {'features', 'renderer', 'style'}}, 'notes'}."""
    import pyogrio
    p = Path(path)
    if p.exists():
        if not overwrite:
            raise GpkgStyleError(f"{p.name} exists; choose another name or allow overwrite")
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    if hidden is None:
        ident = sd.plugin_ident()
        hidden = {"generator": sd.generator_text(), "accreditation": ident["house"]}
    rep: dict = {"path": str(p), "layers": {}, "notes": []}
    seen: set[str] = set()
    for name, gdf, style in items:
        table = str(name)
        if table.lower() in seen or table.lower() == "layer_styles":
            raise GpkgStyleError(f"two layers are named '{table}' (or one is named layer_styles); rename one")
        seen.add(table.lower())
        g = gdf
        if crs and g.crs is not None and not g.crs.equals(crs):
            g = g.to_crs(crs)
        try:
            st = sd.resolve(style or {}, g, tokens)
        except sd.StyleError as ex:
            raise GpkgStyleError(f"layer '{name}': {ex}") from None
        kw = {"dataset_metadata": {k: str(v) for k, v in hidden.items() if v}} if not seen - {table.lower()} else {}
        pyogrio.write_dataframe(g, p, layer=table, driver="GPKG", append=p.exists(), **kw)
        info = write_styles(p, table, st)
        rep["layers"][name] = {"features": len(g), "renderer": info["renderer"], "style": st}
    return rep


def read_styles(gpkg) -> list[dict]:
    """The rows of layer_styles (without the long text unless asked by renderer_type)."""
    p = Path(gpkg)
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT f_table_catalog, f_table_schema, f_table_name, f_geometry_column, styleName, styleQML, styleSLD, useAsDefault "
                          "FROM layer_styles ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def check(gpkg, tables: list[str] | dict) -> list[dict]:
    """MAP-OUT-010 on a styled GeoPackage: layer_styles exists and is a registered table, every table has one default style whose QML parses
    with a renderer and whose SLD parses, and its f_geometry_column is the table's geometry column. `tables`: names, or {name: renderer}."""
    E = _E()
    p = Path(gpkg)
    fs: list[dict] = []
    rows = read_styles(p)
    if not rows:
        return [finding("MAP-OUT-010", "BLOCK", f"{p.name}: no layer_styles table, so QGIS opens the layers unstyled")]
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        reg = con.execute("SELECT data_type FROM gpkg_contents WHERE table_name = 'layer_styles'").fetchone()
    finally:
        con.close()
    if not reg:
        fs.append(finding("MAP-OUT-010", "WARN", f"{p.name}: layer_styles is not registered in gpkg_contents"))
    want = tables if isinstance(tables, dict) else {t: None for t in tables}
    parser = E.XMLParser(resolve_entities=False, no_network=True)
    for t, rtype in want.items():
        mine = [r for r in rows if r["f_table_name"] == t and r["useAsDefault"]]
        if len(mine) != 1:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: table '{t}' has {len(mine)} default style(s); QGIS needs one"))
            continue
        r = mine[0]
        if r["f_table_schema"] != "" or r["f_geometry_column"] != geometry_column(p, t):
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: the style row of '{t}' does not name the table the way QGIS looks it up"))
        try:
            got = renderer_type(r["styleQML"])
            E.fromstring(r["styleSLD"].encode("utf-8"), parser)
        except Exception as ex:  # noqa: BLE001 - a style that does not parse is the finding
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: the style of '{t}' does not parse: {str(ex)[:120]}"))
            continue
        if got is None or (rtype and got != rtype):
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: the style of '{t}' has renderer {got}, expected {rtype}"))
    if not fs:
        fs.append(finding("MAP-OUT-010", "INFO", f"{p.name}: {len(want)} layer(s) with a default QGIS style (QML and SLD) in layer_styles"))
    return fs
