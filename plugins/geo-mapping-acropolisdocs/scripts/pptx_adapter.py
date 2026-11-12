#!/usr/bin/env python3
"""pptx_adapter: a map for a slide, in pieces a person can edit (spec 7.3 map-pptx-adapter).

    pptx_adapter.py parts MAP.yaml --out DIR [--template 16x9-slide] [--dpi 200]
    pptx_adapter.py slide MAP.yaml --out MAP.pptx [--deck DECK.pptx --layout "Blank"] [--check]
    pptx_adapter.py check MAP.pptx                      1.12.0: draw the slide with LibreOffice and compare it with the map and its legend

`parts` writes a folder: map.png and map.svg (the map only, cropped to its frame), text.json (title, subtitle, the message, each legend entry with its color and shape, the
source and trademark lines) and notes.md. Title, legend and notes are NOT in the picture, so they stay text.

`slide` writes one slide with the picture, the title and subtitle as text boxes, the legend as native shapes with text boxes, the source lines as a text box, and the message in the
speaker notes and the picture's alt text. With `--deck` the slide is added to a COPY of that deck (the original is never changed) using one of its own layouts; nothing of the deck's theme,
fonts or other slides is touched. The map is branded; the deck is not restyled.

Everything is set in Inter by name; PowerPoint shows Inter only where it is installed (the font is not embedded in a .pptx).

Legend parity with the map (M16): a continuous ramp is a rectangle with a native linear gradient of the ramp's colours, its values under it; a
picture legend (a WMS GetLegendGraphic) is a picture and a MapServer's swatches are pictures with their labels; a bivariate key is its n x n grid
of native boxes with the two axes as arrows, their ticks, class edges and titles; graduated line widths are native lines of those widths; nested
proportional circles are native ellipses sharing their bottom point with dotted leaders to the values; a hatched class is a native pattern fill
(PATTERN_OF) in the hatch's ink over the class colour. What falls back is listed in the slide's `legend_fallbacks`: PowerPoint's pattern fills are
fixed 8-pixel presets, so a hatch keeps its direction and rough density but not the map's 1.6 mm spacing or its 55 percent strength.

Findings
  MAP-PPT-001 BLOCK  the map has a blocking finding (the slide-size build is linted like any other map)
  MAP-PPT-002 INFO   Inter is not embedded in a .pptx
  MAP-PPT-003 INFO   the deck's slide shape differs from 16:9; the map was fitted inside it
  MAP-PPT-004 BLOCK  --deck cannot be read, or the output would overwrite the deck or an existing file
  MAP-PPT-005 WARN   (1.12.0, `check` or `slide --check`) drawn by LibreOffice, the map on the slide differs from the picture in the file
  MAP-PPT-006 WARN   (1.12.0) on the drawn slide a legend key is not in its class colour
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import text_lint  # noqa: E402

EMU_PER_MM = 36000
PT_MM = 25.4 / 72.0
DEFAULT_TEMPLATE = "16x9-slide"
FONT = "Inter"
LEGEND_GAP_MM = 6.0
LEGEND_MIN_W_MM = 40.0


class AdapterError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-PPT-001"):
        super().__init__(message)
        self.fid = fid


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _hex(c: str):
    from pptx.dml.color import RGBColor
    c = str(c).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return RGBColor(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


# ---------------------------------------------------------------------------------------------------------------- the map, without its title, legend and notes
def _map_only(spec: dict) -> None:
    els = spec.setdefault("elements", {})
    for name in ("title", "legend", "source_note", "logo", "date_stamp", "trademark_line", "crs_note", "disclaimer"):
        els[name] = {**(els.get(name) or {}), "on": False}


def render_parts(spec_path, template: str = DEFAULT_TEMPLATE, brand: str | None = None):
    """(plan, rendered, text, findings). The plan is for the map-only render with the legend column reserved; `text` holds the pieces that stay text."""
    import build_map as bm
    import legend as lg
    import map_plan as mp
    import map_render as mr
    full = bm.build(str(spec_path), tempfile.mkdtemp(prefix="gmap-pptx-"), ["svg"], brand=brand, template=template, strict=False, keep=True)
    fs = [dict(f) for f in full["findings"]]
    obj = full.pop("_objects")
    if full["blocked"]:
        raise AdapterError("the map has blocking findings: " + "; ".join(f"{f['id']} {f['message'][:120]}" for f in fs if f["severity"] == "BLOCK")[:600])
    fplan = obj["plan"]
    blocks = [l.legend_block for l in fplan.layers if l.legend_block]
    plan = mp.prepare(spec_path, brand=brand, template=template, spec_edit=_map_only, classic_layout=True)
    tpl = plan.tpl
    tpl["footer_fit"] = False             # the slide's source lines go in a text box in the template's footer band, so it keeps its height (M16)
    m = tpl["map"]
    legend_w = 0.0
    if blocks:
        ms = lg.measure(blocks, 1, float(tpl["fonts"]["legend"]), float(tpl["fonts"]["legend_title"]))
        legend_w = min(max(float(ms.get("width", LEGEND_MIN_W_MM)), LEGEND_MIN_W_MM), float(tpl.get("legend_max", {}).get("w", 92)))
        tpl["map"] = {**m, "w": float(m["w"]) - legend_w - LEGEND_GAP_MM}
    rendered = mr.render(plan)
    import provenance
    foot = list(provenance.source_lines(plan))
    tm_line = plan.pack["pack"].get("trademark_line")
    tm_on = ((fplan.spec.get("elements") or {}).get("trademark_line") or {}).get("on", "auto")
    if tm_line and tm_on in ("auto", True):
        foot.append(tm_line)
    disc = (fplan.spec.get("elements") or {}).get("disclaimer") or {}
    if disc.get("on") and disc.get("text"):
        foot.append(str(disc["text"]))
    title_el = (fplan.spec.get("elements") or {}).get("title") or {}
    text = {"title": text_lint.title_text(fplan.spec), "subtitle": title_el.get("subtitle"), "message": fplan.spec["message"],
            "footer": foot, "legend": blocks, "legend_width_mm": legend_w, "alt": rendered.alt_text,
            "fonts": dict(tpl["fonts"]), "label_color": plan.tokens["label"], "template": tpl, "brand": plan.brand,
            "tokens": {k_: plan.tokens[k_] for k_ in ("label", "background", "legend_frame", "legend_bg", "label_halo") if k_ in plan.tokens}}
    fs.append(finding("MAP-PPT-002", "INFO", "text is set in Inter by name; a .pptx cannot embed it, so PowerPoint shows Inter only where it is installed"))
    return plan, rendered, text, fs


def crop_svg(svg: str, rect: dict, pad_mm: float = 0.3) -> str:
    """The sheet cropped to the map frame. The viewBox stays at the origin (the exporters expect that), and the content is moved instead."""
    x, y, w, h = float(rect["x"]) - pad_mm, float(rect["y"]) - pad_mm, float(rect["w"]) + 2 * pad_mm, float(rect["h"]) + 2 * pad_mm
    m = re.search(r"<svg\b[^>]*>", svg)
    if not m or "</svg>" not in svg:
        raise AdapterError("the map SVG has an unexpected root; cannot crop it")
    root = re.sub(r'viewBox="[^"]*"', f'viewBox="0 0 {w:g} {h:g}"', m.group(0), count=1)
    root = re.sub(r'\bwidth="[^"]*"', f'width="{w:g}mm"', root, count=1)
    root = re.sub(r'\bheight="[^"]*"', f'height="{h:g}mm"', root, count=1)
    body = svg[m.end():svg.rindex("</svg>")]
    return svg[:m.start()] + root + f'<g transform="translate({-x:g} {-y:g})">' + body + "</g></svg>"


def write_parts(spec_path, out_dir, template: str = DEFAULT_TEMPLATE, brand: str | None = None, dpi: float = 200.0) -> dict:
    import map_brand
    import map_export as mx
    plan, rendered, text, fs = render_parts(spec_path, template, brand)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svg = crop_svg(rendered.svg, plan.tpl["map"])
    stem = rendered.filename_stem + "_slide-map"
    (out / "map.svg").write_text(svg, encoding="utf-8")
    props = {f: map_brand.props_for(plan.brand, plan.manifest, f) for f in ("pdf", "png")}
    ex = mx.export(svg, out, stem, ["png"], props=props, title=text["title"], alt=rendered.alt_text, dpi=dpi, subject=text["message"])
    png = Path(ex["files"][0])
    shutil.move(str(png), out / "map.png")
    pub = {k: v for k, v in text.items() if k not in ("template", "legend")}
    pub["legend"] = text["legend"]
    pub["map_frame_mm"] = {k: round(float(v), 2) for k, v in plan.tpl["map"].items()}
    (out / "text.json").write_text(json.dumps(pub, indent=2, default=str))
    notes = [f"# {text['title']}", "", text["message"], ""]
    if text["footer"]:
        notes += ["## Source and notes", ""] + [f"- {x}" for x in text["footer"]]
    (out / "notes.md").write_text("\n".join(notes) + "\n")
    return {"dir": str(out), "files": ["map.png", "map.svg", "text.json", "notes.md"], "plan": plan, "rendered": rendered, "text": text, "findings": fs, "png": str(out / "map.png")}


# ---------------------------------------------------------------------------------------------------------------- the slide
SHAPE_OF = {"circle": "OVAL", "square": "RECTANGLE", "triangle": "ISOSCELES_TRIANGLE", "diamond": "DIAMOND", "cross": "MATH_PLUS",
            "pointer": "TEAR"}                    # the direction-of-travel key (0.11.0): PowerPoint's teardrop is the nearest built-in shape


def _mm(v: float) -> int:
    return int(round(v * EMU_PER_MM))


def _textbox(slide, x, y, w, h, lines: list[str], size: float, color: str, bold: bool = False, name: str | None = None, anchor_top: bool = True):
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Pt
    tb = slide.shapes.add_textbox(_mm(x), _mm(y), _mm(w), _mm(h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.TOP if anchor_top else MSO_ANCHOR.MIDDLE
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = ln
        r.font.name = FONT
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = _hex(color)
    return tb


def _swatch(slide, kind: str, item: dict, x: float, y: float, w: float, h: float, label_color: str, k: float = 1.0, ink: str | None = None):
    """One legend key: a native line at the class's own width (graduated widths), a symbol shape at its own size, or a box; a hatched class
    gets a native pattern fill over its colour (PATTERN_OF)."""
    from pptx.enum.shapes import MSO_SHAPE
    shp_name = SHAPE_OF.get(item.get("shape") or "", None)
    if kind == "lines":
        ln = _plain(slide.shapes.add_connector(1, _mm(x), _mm(y + h / 2), _mm(x + w), _mm(y + h / 2)))
        ln.line.color.rgb = _hex(item.get("stroke") or label_color)
        ln.line.width = _mm(max(0.1, float(item.get("width_mm") or 0.5) * k))
        if item.get("dash"):
            from pptx.enum.dml import MSO_LINE
            ln.line.dash_style = MSO_LINE.DASH
        return ln
    if shp_name:
        d = float(item.get("size_mm") or 3.0) * k if kind == "proportional" else min(h, max(float(item.get("size_mm") or 3.0) * k, 2.5 * k))
        sh = slide.shapes.add_shape(getattr(MSO_SHAPE, shp_name), _mm(x + (w - d) / 2), _mm(y + (h - d) / 2), _mm(d), _mm(d))
    else:
        sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _mm(x), _mm(y), _mm(w), _mm(h))
    _fill(sh, item.get("fill") or label_color, item.get("pattern"), ink or label_color, alpha=0.78 if kind == "proportional" else None)
    if item.get("stroke"):
        sh.line.color.rgb = _hex(item["stroke"])
        sh.line.width = _mm(0.15)
    else:
        sh.line.fill.background()
    sh.shadow.inherit = False
    return sh


# ---------------------------------------------------------------------------------------------------------------- M16: legend parity
# PowerPoint's preset patterns are 8 x 8 pixel tiles drawn at screen resolution: their spacing does not follow the map's 1.6 mm and does not grow
# with zoom, so each hatch maps to the preset of the same direction and about the same density, drawn in the same ink over the class colour.
PATTERN_OF = {"hatch": "WIDE_UPWARD_DIAGONAL", "hatch_dense": "DARK_UPWARD_DIAGONAL", "back_hatch": "WIDE_DOWNWARD_DIAGONAL", "cross": "DIAGONAL_CROSS",
              "horizontal": "LIGHT_HORIZONTAL", "vertical": "LIGHT_VERTICAL", "dots": "PERCENT_20"}     # dotGrid reads as a coarse grid in LibreOffice


def _plain(sh):
    """Drop the theme style python-pptx gives every new shape (`p:style`: its effect reference draws a soft shadow in LibreOffice even with an
    empty effect list), so the key is exactly the fill and line set here."""
    st = sh._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}style")
    if st is not None:
        sh._element.remove(st)
    return sh


def _fill(sh, color: str, pattern: str | None = None, ink: str | None = None, alpha: float | None = None) -> None:
    """A solid fill, or a native pattern fill (the hatch in `ink` over `color`), with an optional opacity. The shape's theme style goes."""
    from pptx.enum.dml import MSO_PATTERN
    _plain(sh)
    if pattern and pattern != "none" and pattern in PATTERN_OF:
        sh.fill.patterned()
        sh.fill.pattern = getattr(MSO_PATTERN, PATTERN_OF[pattern])
        sh.fill.fore_color.rgb = _hex(ink or color)
        sh.fill.back_color.rgb = _hex(color)
        return
    sh.fill.solid()
    sh.fill.fore_color.rgb = _hex(color)
    if alpha is not None:
        from pptx.oxml.ns import qn
        clr = sh.fill._xPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
        a = clr.makeelement(qn("a:alpha"), {"val": str(int(round(alpha * 100000)))})
        clr.append(a)


def _arrowhead(conn) -> None:
    from pptx.oxml.ns import qn
    ln = conn.line._get_or_add_ln()
    ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))


def _line(slide, x0, y0, x1, y1, color: str, w_mm: float, *, arrow: bool = False, dash: bool = False, name: str | None = None):
    conn = _plain(slide.shapes.add_connector(1, _mm(x0), _mm(y0), _mm(x1), _mm(y1)))
    conn.line.color.rgb = _hex(color)
    conn.line.width = _mm(w_mm)
    if dash:
        from pptx.enum.dml import MSO_LINE
        conn.line.dash_style = MSO_LINE.SQUARE_DOT
    if arrow:
        _arrowhead(conn)
    if name:
        conn.name = name
    return conn


def _label(slide, x, y_base, s: str, pt: float, color: str, *, align: str = "left", bold: bool = False, name: str | None = None, w: float | None = None):
    """A one-line text box whose baseline sits at y_base (mm), left-, centre- or right-aligned on x."""
    import textmeasure as tm
    from pptx.enum.text import PP_ALIGN
    tw = (w or tm.width_mm(s, pt, 700 if bold else 400, tnum=True)) + 1.0
    h = tm.line_height_mm(pt)
    x0 = x - tw / 2 if align == "center" else (x - tw if align == "right" else x)
    tb = _textbox(slide, x0, y_base - pt * PT_MM * 0.95, tw, h, [s], pt, color, bold=bold, name=name or f"Legend: {s}")
    tb.text_frame.word_wrap = False
    tb.text_frame.paragraphs[0].alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
    return tb


def _data_image(href: str):
    import base64
    import io
    m = re.match(r"data:image/[a-z+.-]+;base64,(.*)$", str(href or ""), re.S)
    return io.BytesIO(base64.b64decode(m.group(1))) if m else None


def legend_block(slide, b: dict, lx: float, y: float, lw: float, k: float, fonts: dict, tokens: dict, fallbacks: list) -> float:
    """Draw one legend block's keys as native PowerPoint shapes from slide mm (lx, y); returns the y under it. M16 parity with the map:
    a ramp as a gradient fill, a picture legend as a picture, a bivariate key as its n x n grid with the axes, nested circles as native ellipses
    with leaders, graduated widths as lines of those widths, hatches as pattern fills. What cannot be native is appended to `fallbacks`."""
    import legend as lg
    import textmeasure as tm
    lc = tokens["label"]
    lpt = float(fonts["legend"])                     # template pt; everything below is template mm times k
    lp = lpt * k
    S = lambda v: float(v) * k                       # noqa: E731
    kind = b["kind"]
    if kind == "ramp":
        n_ = b.get("stops") or []
        bar = _plain(slide.shapes.add_shape(1, _mm(lx), _mm(y), _mm(S(lg.RAMP_W_MM)), _mm(S(lg.RAMP_H_MM))))
        bar.name = f"Legend ramp: {b.get('title') or b['layer']}"
        _gradient(bar, n_, float(b.get("opacity", 1.0)))
        bar.line.color.rgb = _hex(tokens.get("legend_frame") or lc)
        bar.line.width = _mm(0.15)
        bar.shadow.inherit = False
        base = y + S(lg.RAMP_H_MM + 0.8) + S(lpt * PT_MM * 0.95)
        for it in b["items"]:
            _label(slide, lx + S(lg.RAMP_W_MM) * float(it["pos"]), base, str(it["label"]), lp, lc, align="center")
        y = y + S(lg.RAMP_H_MM + 0.8 + tm.line_height_mm(lpt))
    elif kind == "image":
        for it in b["items"]:
            img = _data_image(it.get("href"))
            if img is None:
                continue
            pic = slide.shapes.add_picture(img, _mm(lx), _mm(y), _mm(S(it["w_mm"])), _mm(S(it["h_mm"])))
            pic.name = f"Legend picture: {b.get('title') or b['layer']}"
            y += S(float(it["h_mm"]) + lg.ROW_GAP_MM)
    elif kind == "bivariate":
        y = _bivariate(slide, b, lx, y, k, lpt, tokens)
        if any(c.get("pattern") not in (None, "none") for row in b["cells"] for c in row):
            fallbacks.append(f"{b.get('title') or b['layer']}: the hatched rows are PowerPoint pattern fills of the same direction, drawn at full "
                             "strength; their spacing is the preset's (8 screen pixels), not the map's 1.6 mm")
    elif kind == "proportional" and b.get("nested"):
        y = _nested(slide, b, lx, y, k, lpt, tokens)
    else:
        sw_w, sw_h = S(lg.SWATCH_W_MM), S(lg.SWATCH_H_MM)
        for it in b["items"]:
            size = float(it.get("size_mm") or 0.0)
            row_h = max(sw_h, S(tm.line_height_mm(lpt)), S(size + 0.8) if kind in ("proportional", "points") else 0.0)
            icon_w = max(sw_w, S(size)) if kind == "proportional" else sw_w
            if kind == "swatches" and it.get("href"):
                img = _data_image(it["href"])
                d = S(float(it.get("size_mm") or lg.SWATCH_IMG_MM))
                if img is not None:
                    slide.shapes.add_picture(img, _mm(lx + (icon_w - d) / 2), _mm(y + (row_h - d) / 2), _mm(d), _mm(d)).name = f"Legend swatch: {it['label']}"
            else:
                import patterns as pt_
                ink = pt_.ink_for(it.get("fill") or lc, tokens) if it.get("pattern") else None
                _swatch(slide, "lines" if kind == "lines" else kind, it, lx, y, icon_w, row_h, lc, k, ink)
            _textbox(slide, lx + icon_w + S(2.0), y, lw - icon_w - S(2.0), row_h, [str(it["label"])], lp, lc, name=f"Legend: {it['label']}", anchor_top=False)
            y += row_h + S(lg.ROW_GAP_MM)
        if any(it.get("pattern") not in (None, "none") for it in b["items"]):
            fallbacks.append(f"{b.get('title') or b['layer']}: hatches are PowerPoint pattern fills of the same direction; their spacing is "
                             "the preset's (8 screen pixels), not the map's 1.6 mm")
    if b.get("note"):
        nh = S(tm.line_height_mm(lpt - 1))
        _textbox(slide, lx, y, lw, nh, [str(b["note"])], (lpt - 1) * k, lc, name="Legend note")
        y += nh
    return y


def _gradient(sh, colors: list[str], opacity: float = 1.0) -> None:
    """A left-to-right linear gradient with the ramp's colours evenly spaced, as on the map's legend bar."""
    from pptx.oxml.ns import qn
    sh.fill.gradient()
    sh.fill.gradient_angle = 0
    gs_lst = sh.fill._xPr.find(qn("a:gradFill")).find(qn("a:gsLst"))
    for g in list(gs_lst):
        gs_lst.remove(g)
    n = len(colors)
    for i, c in enumerate(colors):
        gs = gs_lst.makeelement(qn("a:gs"), {"pos": str(int(round(100000 * i / max(n - 1, 1))))})
        clr = gs.makeelement(qn("a:srgbClr"), {"val": str(c).lstrip("#").upper()[:6]})
        if opacity < 1.0:
            clr.append(clr.makeelement(qn("a:alpha"), {"val": str(int(round(opacity * 100000)))}))
        gs.append(clr)
        gs_lst.append(gs)


def _bivariate(slide, b: dict, lx: float, y: float, k: float, lpt: float, tokens: dict) -> float:
    """The bivariate key as on the map (legend.bivariate_layout): the n x n grid of native boxes (row j = y class from the bottom), hatched rows
    as pattern fills, the two axes as arrows with their ticks, class edges and titles."""
    import legend as lg
    import patterns as pt_
    g = lg.bivariate_layout(b, lpt)
    n, c = g["n"], g["cell"]
    lc = tokens["label"]
    lp = lpt * k
    X = lambda v: lx + float(v) * k                  # noqa: E731
    Y = lambda v: y + float(v) * k                   # noqa: E731
    _label(slide, X(g["axis_x"]), Y(g["y_title_base"]), b["y"]["title"], lp, lc, bold=True, name=f"Legend axis: {b['y']['title']}")
    _line(slide, X(g["axis_x"]), Y(g["x_axis_y"]), X(g["axis_x"]), Y(g["arrow_top"]), lc, 0.22 * k, arrow=True, name="Legend y axis")
    for kk, lab in enumerate(b["y"]["ticks"]):
        yy = g["grid_bottom"] - kk * c
        _line(slide, X(g["axis_x"] - lg.BIV_TICK_MM), Y(yy), X(g["axis_x"]), Y(yy), lc, 0.18 * k)
        _label(slide, X(g["axis_x"] - lg.BIV_TICK_MM - 0.4), Y(yy + lpt * PT_MM * 0.35), lab, lp, lc, align="right")
    for j in range(n):
        for i in range(n):
            cell = b["cells"][j][i]
            sh = slide.shapes.add_shape(1, _mm(X(g["grid_left"] + i * c)), _mm(Y(g["grid_bottom"] - (j + 1) * c)), _mm(c * k), _mm(c * k))
            sh.name = f"Legend cell {i},{j}"
            pat = cell.get("pattern")
            _fill(sh, cell["fill"], pat, pt_.ink_for(cell["fill"], tokens) if pat and pat != "none" else None)
            sh.line.color.rgb = _hex(b.get("stroke") or tokens.get("background") or tokens["label"])
            sh.line.width = _mm(0.3 * k)
            sh.shadow.inherit = False
    _line(slide, X(g["axis_x"]), Y(g["x_axis_y"]), X(g["arrow_right"]), Y(g["x_axis_y"]), lc, 0.22 * k, arrow=True, name="Legend x axis")
    for kk, lab in enumerate(b["x"]["ticks"]):
        xx = g["grid_left"] + kk * c
        _line(slide, X(xx), Y(g["x_axis_y"]), X(xx), Y(g["x_axis_y"] + lg.BIV_TICK_MM), lc, 0.18 * k)
        _label(slide, X(xx), Y(g["x_tick_base"]), lab, lp, lc, align="center")
    _label(slide, X(g["grid_left"]), Y(g["x_title_base"]), b["x"]["title"], lp, lc, bold=True, name=f"Legend axis: {b['x']['title']}")
    if g["nodata"]:
        nd = b["nodata"]
        sh = slide.shapes.add_shape(1, _mm(lx), _mm(Y(g["nodata"]["y"])), _mm(lg.SWATCH_W_MM * k), _mm(lg.SWATCH_H_MM * k))
        _fill(sh, nd["fill"])
        sh.line.color.rgb = _hex(tokens.get("legend_frame") or lc)
        sh.line.width = _mm(0.15)
        sh.shadow.inherit = False
        _label(slide, X(lg.SWATCH_W_MM + 2.0), Y(g["nodata"]["y"] + lg.SWATCH_H_MM / 2 + lpt * PT_MM * 0.35), nd["label"], lp, lc)
    return Y(g["h"] - (__import__("textmeasure").line_height_mm(lpt - 1) if b.get("note") else 0.0))


def _nested(slide, b: dict, lx: float, y: float, k: float, lpt: float, tokens: dict) -> float:
    """Nested proportional circles as native ellipses sharing their bottom point, largest at the back, each joined to its value by a dotted
    leader (legend.nested_layout)."""
    import legend as lg
    g = lg.nested_layout(b, lpt)
    lc = tokens["label"]
    ring = b.get("ring") or lc
    x0 = lx + g["cx"] * k
    for it, top in zip(g["items"], g["tops"]):
        d = float(it["size_mm"]) * k
        sh = slide.shapes.add_shape(9, _mm(x0 - d / 2), _mm(y + top * k), _mm(d), _mm(d))      # 9 = MSO_SHAPE.OVAL
        sh.name = f"Legend circle: {it['label']}"
        _fill(sh, it.get("fill") or lc, alpha=0.78)
        sh.line.color.rgb = _hex(ring)
        sh.line.width = _mm(0.18 * k)
        sh.shadow.inherit = False
    for it, top, ly in zip(g["items"], g["tops"], g["label_ys"]):
        yt, yl = y + top * k, y + ly * k
        knee = lx + (g["leader_x"] - 0.9) * k
        _line(slide, x0, yt, knee, yt, lc, 0.15 * k, dash=True)
        _line(slide, knee, yt, lx + g["leader_x"] * k, yl, lc, 0.15 * k, dash=True)
        _label(slide, lx + g["label_x"] * k, yl + lpt * PT_MM * 0.35 * k, it["label"], lpt * k, lc)
    return y + g["h"] * k


def build_slide(slide, text: dict, png: Path, rect_scale: tuple[float, float, float], map_frame: dict, alt: str) -> None:
    """Add every piece to `slide`. rect_scale = (scale, offset_x_mm, offset_y_mm) from template mm to slide mm."""
    k, ox, oy = rect_scale
    tpl = text["template"]
    fonts = text["fonts"]
    lc = text["label_color"]

    def X(v): return ox + float(v) * k

    def Y(v): return oy + float(v) * k

    def S(v): return float(v) * k

    t = tpl["title"]
    title_h = S(float(fonts["title"]) * 25.4 / 72 * 1.25)
    _textbox(slide, X(t["x"]), Y(t["y"]), S(t["w"]), title_h, [text["title"]], float(fonts["title"]) * k, lc, bold=True, name="Map title")
    if text.get("subtitle"):
        _textbox(slide, X(t["x"]), Y(t["y"]) + title_h, S(t["w"]), S(float(fonts["subtitle"]) * 25.4 / 72 * 1.3), [str(text["subtitle"])], float(fonts["subtitle"]) * k, lc, name="Map subtitle")
    pic = slide.shapes.add_picture(str(png), _mm(X(map_frame["x"] - 0.3)), _mm(Y(map_frame["y"] - 0.3)), _mm(S(map_frame["w"] + 0.6)), _mm(S(map_frame["h"] + 0.6)))
    pic.name = "Map"
    pic._element.nvPicPr.cNvPr.set("descr", alt)
    # legend: native shapes and text, in the column beside the map
    if text["legend"]:
        lx = X(float(map_frame["x"]) + float(map_frame["w"]) + LEGEND_GAP_MM)
        lw = S(text["legend_width_mm"])
        y = Y(map_frame["y"])
        lt, _lp = float(fonts["legend_title"]) * k, float(fonts["legend"]) * k
        _sw_w, _sw_h = S(6.0), S(3.6)
        for b in text["legend"]:
            if b.get("title"):
                import textmeasure as tm
                n_lines = max(1, len(tm.wrap(b["title"], float(text["legend_width_mm"]) - 2.0, float(fonts["legend_title"]), 700)))
                th = S(n_lines * tm.line_height_mm(float(fonts["legend_title"])))
                _textbox(slide, lx, y, lw, th, [b["title"]], lt, lc, bold=True, name=f"Legend title: {b['title']}")
                y += th + S(1.2)
            y = legend_block(slide, b, lx, y, lw, k, fonts, text.get("tokens") or {"label": lc}, text.setdefault("fallbacks", []))
            y += S(3.0)
    f = tpl["footer"]
    if text["footer"]:
        np_ = float(fonts["note"]) * k
        _textbox(slide, X(f["x"]), Y(f["y"]), S(f["w"]), S(f["h"]), list(text["footer"]), np_, lc, name="Source and notes")
    slide.notes_slide.notes_text_frame.text = text["message"] + ("\n\n" + "\n".join(text["footer"]) if text["footer"] else "")


def write_slide(spec_path, out_pptx, *, deck: str | None = None, layout: str | None = None, template: str = DEFAULT_TEMPLATE, brand: str | None = None,
                dpi: float = 200.0, overwrite: bool = False) -> dict:
    from pptx import Presentation
    out_pptx = Path(out_pptx)
    fs = []
    if deck and Path(deck).resolve() == out_pptx.resolve():
        raise AdapterError("the output would overwrite the deck; choose another file name", "MAP-PPT-004")
    if out_pptx.exists() and not overwrite:
        raise AdapterError(f"{out_pptx} already exists; choose another name or allow overwrite", "MAP-PPT-004")
    tmp = Path(tempfile.mkdtemp(prefix="gmap-pptx-parts-"))
    parts = write_parts(spec_path, tmp, template, brand, dpi)
    text, plan = parts["text"], parts["plan"]
    fs += parts["findings"]
    tpl_w, tpl_h = (float(v) for v in plan.tpl["size_mm"])
    if deck:
        try:
            prs = Presentation(str(deck))
        except Exception as ex:  # noqa: BLE001 - a bad deck is a finding, not a crash
            raise AdapterError(f"the deck could not be opened: {type(ex).__name__}: {str(ex)[:160]}", "MAP-PPT-004")
        lay = None
        if layout:
            lay = next((l for l in prs.slide_layouts if l.name.strip().lower() == layout.strip().lower()), None)
            if lay is None:
                raise AdapterError(f"the deck has no layout named '{layout}' (layouts: {', '.join(l.name for l in prs.slide_layouts)})", "MAP-PPT-004")
        else:
            lay = next((l for l in prs.slide_layouts if l.name.strip().lower() in ("blank", "empty")), None) or min(prs.slide_layouts, key=lambda l: len(l.placeholders))
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = _mm(tpl_w), _mm(tpl_h)
        lay = min(prs.slide_layouts, key=lambda l: len(l.placeholders))
    sw, sh = prs.slide_width / EMU_PER_MM, prs.slide_height / EMU_PER_MM
    k = min(sw / tpl_w, sh / tpl_h)
    off = ((sw - tpl_w * k) / 2, (sh - tpl_h * k) / 2)
    if abs((sw / sh) - (tpl_w / tpl_h)) > 0.02:
        fs.append(finding("MAP-PPT-003", "INFO", f"the deck's slide is {sw / 25.4:.2f} x {sh / 25.4:.2f} in, not 16:9; the map was fitted inside it and centered"))
    slide = prs.slides.add_slide(lay)
    for ph in list(slide.placeholders):                         # the map slide carries its own title; an empty placeholder would show "Click to add title"
        ph._element.getparent().remove(ph._element)
    build_slide(slide, text, Path(parts["png"]), (k, off[0], off[1]), plan.tpl["map"], text["alt"])
    out_pptx.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_pptx))
    shutil.rmtree(tmp, ignore_errors=True)
    return {"file": str(out_pptx), "findings": fs, "slides": len(prs.slides), "deck": bool(deck), "legend_fallbacks": list(text.get("fallbacks") or [])}


# ---------------------------------------------------------------------------------------------------------------- the drawn slide (1.12.0)
PICTURE_TOLERANCE = 14.0                # mean absolute difference per channel (of 255) between the drawn map and the picture in the file
SWATCH_TOLERANCE = 28                   # the most a legend key's drawn colour may differ from its class colour in any channel


def render_slide(pptx_path, page: int, dpi: float = 110.0, timeout: int = 180):
    """The slide as LibreOffice draws it (an RGB image), or (None, why) when LibreOffice or pdftoppm is not here. Nothing leaves the computer."""
    import subprocess
    from PIL import Image
    office = shutil.which("soffice") or shutil.which("libreoffice")
    ppm = shutil.which("pdftoppm")
    if not office or not ppm:
        return None, "LibreOffice (soffice) and pdftoppm are needed to draw the slide; one of them is not installed here"
    tmp = Path(tempfile.mkdtemp(prefix="gmap-slide-check-"))
    try:
        r = subprocess.run([office, f"-env:UserInstallation=file://{tmp / 'profile'}", "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(pptx_path)],
                           capture_output=True, text=True, timeout=timeout)
        pdf = tmp / (Path(pptx_path).stem + ".pdf")
        if not pdf.is_file():
            return None, f"LibreOffice did not draw the slide ({(r.stderr or r.stdout).strip()[-160:] or 'no output'})"
        subprocess.run([ppm, "-r", str(dpi), "-f", str(page), "-l", str(page), "-png", str(pdf), str(tmp / "s")], capture_output=True, timeout=timeout)
        png = next(iter(sorted(tmp.glob("s*.png"))), None)
        if png is None:
            return None, "the drawn slide could not be read as a picture"
        im = Image.open(png).convert("RGB")
        im.load()
        return im, None
    except (OSError, subprocess.SubprocessError) as ex:
        return None, f"the slide could not be drawn ({type(ex).__name__})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_slide(pptx_path, slide_index: int = -1, dpi: float = 110.0) -> dict:
    """1.12.0: an automated look at the slide. LibreOffice draws it; the place of the map picture on the drawn slide is compared with the picture
    inside the file (MAP-PPT-005), and each solid legend key with its class colour (MAP-PPT-006). -> {ok, checked, findings, picture_difference,
    keys_checked}. Without LibreOffice the slide is not checked and the answer says so."""
    import io
    import numpy as np
    from PIL import Image
    from pptx import Presentation
    from pptx.enum.dml import MSO_FILL
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = Presentation(str(pptx_path))
    n = len(prs.slides)
    idx = slide_index if slide_index >= 0 else n + slide_index
    slide = prs.slides[idx]
    im, why = render_slide(pptx_path, idx + 1, dpi)
    if im is None:
        return {"ok": True, "checked": False, "findings": [finding("MAP-PPT-005", "INFO", f"the slide was not drawn and compared: {why}")]}
    a = np.asarray(im).astype(float)
    kx, ky = a.shape[1] / prs.slide_width, a.shape[0] / prs.slide_height
    fs: list[dict] = []

    def box(sh, inner: float = 0.0):
        x0, y0, x1, y1 = sh.left * kx, sh.top * ky, (sh.left + sh.width) * kx, (sh.top + sh.height) * ky
        dx, dy = (x1 - x0) * inner, (y1 - y0) * inner
        return int(round(x0 + dx)), int(round(y0 + dy)), int(round(x1 - dx)), int(round(y1 - dy))

    pic = next((s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE and s.name == "Map"), None)
    diff = None
    if pic is None:
        fs.append(finding("MAP-PPT-005", "WARN", "the slide has no picture named 'Map'; the map may have been replaced or renamed"))
    else:
        x0, y0, x1, y1 = box(pic)
        crop = a[max(0, y0):y1, max(0, x0):x1]
        if crop.shape[0] < 20 or crop.shape[1] < 20 or x0 < -2 or y0 < -2 or x1 > a.shape[1] + 2 or y1 > a.shape[0] + 2:
            fs.append(finding("MAP-PPT-005", "WARN", "the map picture lies partly off the slide"))
        else:
            src = Image.open(io.BytesIO(pic.image.blob)).convert("RGB").resize((crop.shape[1], crop.shape[0]), Image.LANCZOS)
            diff = float(np.abs(crop - np.asarray(src).astype(float)).mean())
            if diff > PICTURE_TOLERANCE:
                fs.append(finding("MAP-PPT-005", "WARN", f"on the drawn slide the map differs from the picture the plugin made (mean difference {diff:.0f} of 255): "
                                  "it may be stretched, cropped or covered by another shape", {"difference": round(diff, 1)}))
    keys = bad = 0
    for sh in slide.shapes:
        if sh.shape_type != MSO_SHAPE_TYPE.AUTO_SHAPE or sh.has_text_frame and sh.text_frame.text.strip():
            continue
        try:
            if sh.fill.type != MSO_FILL.SOLID:
                continue
            rgb = sh.fill.fore_color.rgb
        except (AttributeError, TypeError):
            continue
        if "alpha" in sh.fill._xPr.xml:                     # a see-through key (nested circles) shows what is under it
            continue
        x0, y0, x1, y1 = box(sh, 0.3)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        got = np.median(a[y0:y1, x0:x1].reshape(-1, 3), axis=0)
        keys += 1
        if np.abs(got - np.array([rgb[0], rgb[1], rgb[2]], dtype=float)).max() > SWATCH_TOLERANCE:
            bad += 1
            fs.append(finding("MAP-PPT-006", "WARN", f"the legend key '{sh.name}' is not drawn in its class colour on the slide (another shape may cover it)",
                              {"shape": sh.name, "drawn": [int(v) for v in got]}))
    if not fs:
        fs.append(finding("MAP-PPT-005", "INFO", f"the slide was drawn and compared: the map matches the picture in the file (mean difference {diff:.1f} of 255) and "
                          f"{keys} legend key(s) show their class colours"))
    return {"ok": not any(f["severity"] == "WARN" for f in fs), "checked": True, "findings": fs, "picture_difference": None if diff is None else round(diff, 2),
            "keys_checked": keys, "keys_wrong": bad, "slide": idx + 1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["parts", "slide", "check"])
    ap.add_argument("--check", action="store_true", help="1.12.0: after writing the slide, draw it with LibreOffice and compare it with the map and its legend")
    ap.add_argument("spec")
    ap.add_argument("--out")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--brand")
    ap.add_argument("--dpi", type=float, default=200.0)
    ap.add_argument("--deck")
    ap.add_argument("--layout")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    if a.op != "check" and not a.out:
        ap.error("--out is required")
    try:
        if a.op == "check":                              # `spec` is the .pptx here
            r = check_slide(a.spec)
            print(json.dumps(r, indent=2, default=str))
            return 0 if r["ok"] else 3
        if a.op == "parts":
            r = write_parts(a.spec, a.out, a.template, a.brand, a.dpi)
            print(json.dumps({"dir": r["dir"], "files": r["files"], "findings": r["findings"]}, indent=2, default=str))
        else:
            r = write_slide(a.spec, a.out, deck=a.deck, layout=a.layout, template=a.template, brand=a.brand, dpi=a.dpi, overwrite=a.overwrite)
            if a.check:
                c = check_slide(r["file"])
                r["findings"] = list(r["findings"]) + c["findings"]
                r["slide_check"] = {k: c.get(k) for k in ("checked", "picture_difference", "keys_checked", "keys_wrong")}
            print(json.dumps(r, indent=2, default=str))
        return 0
    except AdapterError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.fid, "BLOCK", str(ex))]}, indent=2))
        return 3
    except Exception as ex:  # noqa: BLE001 - a plan error or an unreadable spec is reported as a finding, not a traceback
        print(json.dumps({"ok": False, "findings": [finding("MAP-PPT-001", "BLOCK", f"{type(ex).__name__}: {str(ex)[:300]}")]}, indent=2))
        return 3


if __name__ == "__main__":
    sys.exit(main())
