"""legend_draw: drawing for the M15 legend types and the hatch overlays on the map (gap P1-11, P2-5).

map_render calls these beside its own drawing so its hot loops stay as they are:

    bivariate_key(out, block, cx, cy, lpt, t)      the bivariate grid with arrowed axes, ticks and the two titles -> y below it
    nested_key(out, block, cx, cy, lpt, t)         nested proportional circles with leaders -> y below it
    swatch_pattern(it, x, y, w, h, t, layer_id)    a class swatch's hatch, over its colour (classes legends)
    layer_patterns(l, frame, t, to_sheet)          the hatch over each patterned class of an area layer, inside the layer's group

Geometry comes from legend.bivariate_layout and legend.nested_layout, so the size the legend was fitted to is the size drawn.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import legend as lg  # noqa: E402
import patterns as pt  # noqa: E402
import svg_draw as sd  # noqa: E402
import textmeasure as tm  # noqa: E402

PT = sd.PT


def _pattern_ref(layer_id: str, name: str | None, fill: str, t: dict) -> tuple[str, str]:
    """(defs, url) for a pattern over `fill`; ("", "") for none."""
    if not name or name == "none":
        return "", ""
    ink = pt.ink_for(fill, t)
    pid = pt.pattern_id(layer_id, name, ink)
    return pt.defs(pid, name, ink), f"url(#{pid})"


def swatch_pattern(it: dict, x: float, y: float, w: float, h: float, t: dict, layer_id: str) -> str:
    d, url = _pattern_ref(layer_id, it.get("pattern"), it["fill"], t)
    if not url:
        return ""
    return f"<defs>{d}</defs>" + sd.rect(x, y, w, h, url, "none", 0, f'data-pattern="{it["pattern"]}"')


def _arrow(x0, y0, x1, y1, col, w=0.22, head=1.1) -> str:
    """A line from (x0, y0) to (x1, y1) with a filled arrowhead at the far end."""
    import math
    a = math.atan2(y1 - y0, x1 - x0)
    bx, by = x1 - head * math.cos(a), y1 - head * math.sin(a)
    px, py = -math.sin(a) * head * 0.42, math.cos(a) * head * 0.42
    return (f'<path d="M{sd.num(x0)} {sd.num(y0)}L{sd.num(bx)} {sd.num(by)}" stroke="{col}" stroke-width="{sd.num(w)}" fill="none"/>'
            f'<path d="M{sd.num(x1)} {sd.num(y1)}L{sd.num(bx + px)} {sd.num(by + py)}L{sd.num(bx - px)} {sd.num(by - py)}Z" fill="{col}"/>')


def bivariate_key(out: list, b: dict, cx: float, cy: float, lpt: float, t: dict) -> float:
    g = lg.bivariate_layout(b, lpt)
    n, c = g["n"], g["cell"]
    col = t["label"]
    ox, oy = cx, cy
    out.append(f'<g class="gm-legend-bivariate" data-layer="{sd.esc(b["layer"])}">')
    # the y field's title on the first line, then its axis up the left side with the arrow pointing up
    out.append(sd.text(ox + g["axis_x"], oy + g["y_title_base"], b["y"]["title"], lpt, col, weight=600, extra=' data-axis="y"'))
    out.append(_arrow(ox + g["axis_x"], oy + g["x_axis_y"], ox + g["axis_x"], oy + g["arrow_top"], col))
    for k, lab in enumerate(b["y"]["ticks"]):
        y = oy + g["grid_bottom"] - k * c
        out.append(f'<path d="M{sd.num(ox + g["axis_x"] - lg.BIV_TICK_MM)} {sd.num(y)}h{sd.num(lg.BIV_TICK_MM)}" stroke="{col}" stroke-width="0.18"/>')
        out.append(sd.text(ox + g["axis_x"] - lg.BIV_TICK_MM - 0.4, y + lpt * PT * 0.35, lab, lpt, col, anchor="end", num_cls=True))
    # the grid: row j = the y class from the bottom, column i = the x class from the left
    for j in range(n):
        for i in range(n):
            cell = b["cells"][j][i]
            x, y = ox + g["grid_left"] + i * c, oy + g["grid_bottom"] - (j + 1) * c
            out.append(sd.rect(x, y, c, c, cell["fill"], b.get("stroke") or t["background"], 0.3, f'data-cell="{i},{j}"'))
            if cell.get("pattern") and cell["pattern"] != "none":
                out.append(swatch_pattern({"fill": cell["fill"], "pattern": cell["pattern"]}, x, y, c, c, t, b["layer"]))
    out.append(sd.rect(ox + g["grid_left"], oy + g["grid_top"], n * c, n * c, "none", t["legend_frame"], 0.2))
    # the x axis along the bottom, arrow to the right, its ticks and the x field's title under them
    out.append(_arrow(ox + g["axis_x"], oy + g["x_axis_y"], ox + g["arrow_right"], oy + g["x_axis_y"], col))
    for k, lab in enumerate(b["x"]["ticks"]):
        x = ox + g["grid_left"] + k * c
        out.append(f'<path d="M{sd.num(x)} {sd.num(oy + g["x_axis_y"])}v{sd.num(lg.BIV_TICK_MM)}" stroke="{col}" stroke-width="0.18"/>')
        out.append(sd.text(x, oy + g["x_tick_base"], lab, lpt, col, anchor="middle", num_cls=True))
    out.append(sd.text(ox + g["grid_left"], oy + g["x_title_base"], b["x"]["title"], lpt, col, weight=600, extra=' data-axis="x"'))
    if g["nodata"]:
        y = oy + g["nodata"]["y"]
        out.append(sd.rect(ox, y, lg.SWATCH_W_MM, lg.SWATCH_H_MM, b["nodata"]["fill"], t["legend_frame"], 0.15))
        out.append(sd.text(ox + lg.SWATCH_W_MM + 2.0, y + lg.SWATCH_H_MM / 2 + lpt * PT * 0.35, b["nodata"]["label"], lpt, col))
    bottom = oy + g["h"]
    if b.get("note"):
        npt = lpt - 1
        bottom -= tm.line_height_mm(npt)
        out.append(sd.text(ox, bottom + npt * PT * 0.85, b["note"], npt, col, num_cls=True))
        bottom += tm.line_height_mm(npt)
    out.append("</g>")
    return bottom


def nested_key(out: list, b: dict, cx: float, cy: float, lpt: float, t: dict) -> float:
    g = lg.nested_layout(b, lpt)
    col = t["label"]
    ring = b.get("ring") or t["label"]
    x0 = cx + g["cx"]
    out.append(f'<g class="gm-legend-nested" data-layer="{sd.esc(b["layer"])}">')
    for it, top in zip(g["items"], g["tops"]):               # largest first, so the smaller circles sit in front
        r = float(it["size_mm"]) / 2
        out.append(f'<circle cx="{sd.num(x0)}" cy="{sd.num(cy + top + r)}" r="{sd.num(r)}" fill="{it["fill"]}" fill-opacity="0.78" '
                   f'stroke="{ring}" stroke-width="0.18"/>')
    for it, top, ly in zip(g["items"], g["tops"], g["label_ys"]):
        y_top, y_lab = cy + top, cy + ly
        out.append(f'<path d="M{sd.num(x0)} {sd.num(y_top)}H{sd.num(cx + g["leader_x"] - 0.9)}L{sd.num(cx + g["leader_x"])} {sd.num(y_lab)}" '
                   f'stroke="{col}" stroke-width="0.15" fill="none" stroke-dasharray="0.5 0.35"/>')
        out.append(sd.text(cx + g["label_x"], y_lab + lpt * PT * 0.35, it["label"], lpt, col, num_cls=True))
    out.append("</g>")
    return cy + g["h"]


def layer_patterns(l, frame, t: dict, to_sheet) -> str:
    """The hatch over each patterned class of an area layer, drawn inside the layer's group right after its fills, one group per class marked
    with its class and pattern so the accessibility lint can read back what was drawn (a11y_lint.svg_patterns)."""
    pats = (l.symbol or {}).get("_patterns")
    if not pats or l.kind != "polygon" or l.cls is None:
        return ""
    geoms, keep = to_sheet(l.gdf, frame, True)
    assigned = np.asarray(l.cls.assigned)
    out = []
    for ci, name in enumerate(pats):
        if not name or name == "none" or ci >= len(l.colors):
            continue
        d, url = _pattern_ref(l.id, name, l.colors[ci], t)
        sel = np.where(keep & (assigned == ci))[0]
        out.append(f"<defs>{d}</defs>")
        paths = "".join(f'<path d="{p}"/>' for p in (sd.polygon_path(geoms[i]) for i in sel) if p)
        out.append(f'<g class="gm-pattern" data-layer="{sd.esc(l.id)}" data-class="{ci}" data-pattern="{name}" fill="{url}" stroke="none" fill-rule="evenodd">'
                   f"{paths}</g>")
    return "".join(out)
