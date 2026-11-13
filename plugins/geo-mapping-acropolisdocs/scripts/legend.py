#!/usr/bin/env python3
"""legend-builder: a legend model from layer symbology, sized with real Inter metrics (spec 7.3, 10, 12.3b MAP-CART-005/006).

A layer that carries meaning gets a block; basemap layers never do. A block is one of:
  classes     choropleth, hexbin or grid bins, with a No data swatch when values are null (a hexbin block carries swatch: hexagon and a grid
              block swatch: square, so the key is drawn in the cell's own shape; a choropleth's rectangle is the default)
  categories  one swatch per category
  points      symbol shapes (and proportional-symbol size keys; `nested: true` draws the size key as nested circles with leaders, M15)
  lines       line samples (a classed lines layer's classes, and graduated widths: one sample per class at its width, M15)
  polygons    a single fill/outline swatch
  bivariate   a bivariate key: the n x n grid as a square, the two fields' titles and class edges on arrowed axes (M15, bivariate.py)
  ramp        a continuous colour bar (a raster on the brand's sequential or diverging ramp, gap P1-11): `stops` are the ramp's token colours
              (drawn as one gradient), `items` the labelled values along it (each with the colour at its place, so a renderer that cannot draw a
              gradient shows steps) (M12)
  image       a picture the map cannot redraw in its own symbols: a WMS GetLegendGraphic image, scaled to fit (M12)
  swatches    picture swatches with Inter labels: a MapServer's legend, one small image per class (M12)
The model is data; `layout.py` draws it. `fit_legend` sizes it against the room the template gives and says what it cannot fit.
"""
from __future__ import annotations

import math

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import textmeasure as tm  # noqa: E402

SWATCH_W_MM = 6.0
SWATCH_H_MM = 3.6
ROW_GAP_MM = 1.4
BLOCK_GAP_MM = 3.0
PAD_MM = 3.0
COL_GAP_MM = 5.0
TITLE_PT, LABEL_PT = 8.5, 7.5
NO_DATA_LABEL = "No data"
MIN_LABEL_PT = {"print": 6.0, "slide": 10.0}
SHAPES = ("circle", "square", "triangle", "diamond", "cross")


class LegendError(ValueError):
    pass


def _layer_title(layer: dict) -> str:
    lg = layer.get("legend") or {}
    return str(lg.get("title") or layer.get("title") or layer["id"].replace("_", " ").replace("-", " ").capitalize())


SWATCHES = ("rect", "hexagon", "square")


def class_block(layer: dict, cls, colors: list[str], tokens: dict, *, stroke: str | None = None, swatch: str | None = None) -> dict:
    """A classed-bins block. colors has one entry per class; nulls add a No data swatch. `swatch` names the key's shape: hexagon for a hexbin
    layer, square for a grid layer; unset (a rectangle) for a choropleth."""
    if len(colors) != cls.k:
        raise LegendError(f"{cls.k} classes but {len(colors)} colors")
    if swatch is not None and swatch not in SWATCHES:
        raise LegendError(f"unknown swatch shape '{swatch}' (one of {', '.join(SWATCHES)})")
    items = [{"label": lab, "fill": col, "stroke": stroke or tokens["legend_frame"], "count": cls.counts[i]}
             for i, (lab, col) in enumerate(zip(cls.labels, colors, strict=True))]
    if cls.nodata:
        items.append({"label": NO_DATA_LABEL, "fill": tokens["no_data"], "stroke": stroke or tokens["legend_frame"], "count": cls.nodata, "nodata": True})
    out = {"layer": layer["id"], "title": _layer_title(layer), "kind": "classes" if cls.kind == "numeric" else "categories",
           "items": items, "note": (layer.get("legend") or {}).get("note")}
    if swatch and swatch != "rect":
        out["swatch"] = swatch
    return out


def _note(layer: dict) -> str | None:
    """legend.note under any layer's key (1.13.0, Day 12 issue 5: 1.12 drew it under class keys only)."""
    n = (layer.get("legend") or {}).get("note")
    return str(n) if n not in (None, "") else None


def _single_title(layer: dict) -> str | None:
    """0.19.0 (Day 7 issue 25): a one-entry block keeps its heading when the layer gives both a legend title and an item label that differ
    (0.18 printed the label alone and dropped the title)."""
    lgd = layer.get("legend") or {}
    ti, la = lgd.get("title"), lgd.get("label")
    return str(ti) if ti and la and str(ti).strip() != str(la).strip() else None


def points_block(layer: dict, tokens: dict) -> dict:
    sym = layer.get("symbol") or {}
    shape = sym.get("shape", "circle")
    if shape not in SHAPES:
        raise LegendError(f"unknown symbol shape '{shape}'")
    item = {"label": (layer.get("legend") or {}).get("label") or _layer_title(layer), "shape": "pointer" if sym.get("rotate_field") else shape,
            "size_mm": float(sym["_size_mm"]) if sym.get("_size_mm") else float(sym.get("size", 3)) * 0.35,
            "fill": sym.get("_color") or tokens["emphasis"], "stroke": tokens["background"]}
    return {"layer": layer["id"], "title": _single_title(layer), "kind": "points", "items": [item], "note": _note(layer)}


def _ladder(e_lo: int, e_hi: int, mult=(1.0, 2.0, 5.0)) -> list[float]:
    return sorted(m * 10 ** k for k in range(e_lo, e_hi + 1) for m in mult)


def nice_key_values(lo: float, hi: float, n: int = 3) -> list[float]:
    """Round values for a proportional-symbol key, on the 1-2-5 ladder: the largest round number at or below the data's maximum (2.5 or 7.5
    only when that is the only way to stay within half of it), then round values near a half and a tenth of it that sit within the data's
    range. The key reads 10 / 50 / 100 rather than 5 / 25 / 126."""
    if not (hi > 0) or lo >= hi:                           # one value (or none): the key is that value, not a round number below it
        return [hi]
    e = math.floor(math.log10(hi))
    base = _ladder(e - 4, e + 1)
    top = max(c for c in base if c <= hi * (1 + 1e-9))
    if top < 0.5 * hi:
        top = max(c for c in _ladder(e - 4, e + 1, (1.0, 2.0, 2.5, 5.0, 7.5)) if c <= hi * (1 + 1e-9))
    out = [top]
    for ratio in (0.5, 0.1, 0.25):
        v = min(base, key=lambda c: abs(math.log(c / (top * ratio))))
        if v >= lo * 0.5 and all(v * 1.8 <= u for u in out):
            out.append(v)
        if len(out) == n:
            break
    out = sorted(out)
    if len(out) < 2 and hi > lo:                           # a narrow range still gets a second size to compare
        v = min(base, key=lambda c: abs(math.log(c / (top / 2))))
        out = sorted({top, v}) if v < top else out
    return out


def proportional_block(layer: dict, tokens: dict, values: list[float], radius_for, fmt, color: str) -> dict:
    """Size key for area-scaled symbols, drawn nested or in a row by the layout.

    legend.values in the layer: a list of numbers (used as given), "nice" (the default: round values such as 10, 50, 100), or "exact"
    (the data's smallest, middle and largest value, which gives numbers like 5, 25, 126)."""
    lo, hi = min(values), max(values)
    cfg = (layer.get("legend") or {}).get("values", "nice")
    if isinstance(cfg, (list, tuple)) and cfg:
        keys = sorted({float(v) for v in cfg if float(v) > 0})
    elif cfg == "exact":
        mid = (lo * hi) ** 0.5 if lo > 0 else (lo + hi) / 2
        keys = [lo, mid, hi]
    else:
        keys = nice_key_values(lo, hi, int((layer.get("legend") or {}).get("count", 3)))
    items = [{"label": fmt(v), "shape": "circle", "size_mm": 2 * radius_for(v), "fill": color, "stroke": tokens["background"], "value": v}
             for v in keys]
    out = {"layer": layer["id"], "title": _layer_title(layer), "kind": "proportional", "items": items, "note": _note(layer)}
    if (layer.get("legend") or {}).get("nested"):          # nested circles with leaders (M15)
        out["nested"] = True
        out["ring"] = tokens["label"]
    return out


def lines_block(layer: dict, tokens: dict) -> dict:
    sym = layer.get("symbol") or {}
    return {"layer": layer["id"], "title": _single_title(layer), "kind": "lines",
            "items": [{"label": (layer.get("legend") or {}).get("label") or _layer_title(layer), "stroke": sym.get("_color") or tokens["emphasis"],
                       "width_mm": float(sym["_width_mm"]) if sym.get("_width_mm") and tokens.get("_look", "classic") != "classic"
                       else float(sym.get("width", 0.8)) * 0.35, "dash": sym.get("dash")}], "note": _note(layer)}


def polygons_block(layer: dict, tokens: dict) -> dict:
    sym = layer.get("symbol") or {}
    return {"layer": layer["id"], "title": _single_title(layer), "kind": "polygons",
            "items": [{"label": (layer.get("legend") or {}).get("label") or _layer_title(layer), "fill": sym.get("_fill") or tokens["land"],
                       "stroke": sym.get("_stroke") or tokens["frame"], "opacity": float(sym.get("opacity", 1.0))}], "note": _note(layer)}


RAMP_W_MM = 42.0              # a continuous bar's length
RAMP_H_MM = 3.6
IMAGE_MAX_W_MM = 48.0         # a service legend picture is scaled to at most this width and height
IMAGE_MAX_H_MM = 60.0
IMAGE_MM_PER_PX = 25.4 / 96   # a GetLegendGraphic picture is drawn for a 96 dpi screen
SWATCH_IMG_MM = 4.2


def ramp_block(layer: dict, stops: list[dict], colors: list[str], tokens: dict, *, opacity: float = 1.0, note: str | None = None) -> dict:
    """A continuous ramp: `colors` are the brand ramp's tokens, evenly spaced along the bar; `stops` the labelled values {label, value, pos 0..1}."""
    if len(colors) < 2:
        raise LegendError("a ramp needs at least two colours")
    import numpy as np
    pos = np.linspace(0.0, 1.0, len(colors))
    items = []
    for s in stops:
        k = int(np.argmin(np.abs(pos - float(s["pos"]))))
        items.append({"label": str(s["label"]), "value": s.get("value"), "pos": float(s["pos"]), "fill": colors[k], "stroke": tokens["legend_frame"]})
    return {"layer": layer["id"], "title": _layer_title(layer), "kind": "ramp", "items": items, "stops": list(colors), "opacity": float(opacity), "note": note}


def image_block(layer: dict, href: str | None, tokens: dict, *, px: tuple[int, int] | None = None, note: str | None = None) -> dict:
    """A legend picture (data URI) scaled from its pixel size at 96 dpi to fit IMAGE_MAX_W_MM x IMAGE_MAX_H_MM; no picture leaves the title and the note."""
    if href is None:
        return {"layer": layer["id"], "title": _layer_title(layer), "kind": "image", "items": [], "note": note}
    w_px, h_px = px or (100, 100)
    w, h = w_px * IMAGE_MM_PER_PX, h_px * IMAGE_MM_PER_PX
    k = min(1.0, IMAGE_MAX_W_MM / max(w, 1e-6), IMAGE_MAX_H_MM / max(h, 1e-6))
    return {"layer": layer["id"], "title": _layer_title(layer), "kind": "image",
            "items": [{"label": "", "href": href, "w_mm": round(w * k, 2), "h_mm": round(h * k, 2), "size_mm": round(h * k, 2)}], "note": note}


def swatches_block(layer: dict, entries: list[tuple[str, str]], tokens: dict, *, opacity: float = 1.0, note: str | None = None) -> dict:
    """Picture swatches (data URIs) with labels: a MapServer legend, its symbols as the server drew them and its labels set in Inter."""
    items = [{"label": str(lab), "href": href, "size_mm": SWATCH_IMG_MM, "opacity": float(opacity)} for href, lab in entries]
    return {"layer": layer["id"], "title": _layer_title(layer), "kind": "swatches", "items": items, "note": note}


def status_block(layer: dict, tokens: dict, categories: list[dict]) -> dict:
    """RAG-style status: every entry carries a shape AND a color, never color alone (MAP-A11Y-003)."""
    items = []
    for c in categories:
        if not c.get("shape"):
            raise LegendError(f"status '{c['label']}' needs a shape as well as a color")
        items.append({"label": c["label"], "shape": c["shape"], "size_mm": 3.0, "fill": c["fill"], "stroke": tokens["background"], **({"nodata": True} if c.get("nodata") else {})})
    return {"layer": layer["id"], "title": _layer_title(layer), "kind": "points", "items": items}


# ---------------------------------------------------------------------------------------------------------------- M15 layouts
BIV_CELL_MM = 8.0             # a bivariate key's cell, at least; wider when the x ticks need it
BIV_AXIS_GAP_MM = 0.8         # between the grid and an axis line
BIV_ARROW_MM = 2.8            # how far an axis runs past the grid to its arrowhead
BIV_TICK_MM = 0.7
NESTED_LEADER_MM = 2.4        # a nested circle key: from the largest circle's edge to the labels


def bivariate_layout(block: dict, label_pt: float = LABEL_PT) -> dict:
    """Where everything in a bivariate key goes, in mm from the block's top-left (below its title): the y field's title on the first line, the y
    axis with its arrow up the left side and the class edges at its ticks, the n x n grid, the x axis with its arrow along the bottom and its
    ticks, the x field's title under them, then a No data swatch when there is one. Drawing (map_render) and sizing share this."""
    n = int(block["n"])
    PTMM = tm.PT_MM
    lh = tm.line_height_mm(label_pt)
    xt, yt = block["x"]["ticks"], block["y"]["ticks"]
    xw = [tm.width_mm(s, label_pt, 400, tnum=True) for s in xt]
    yw = [tm.width_mm(s, label_pt, 400, tnum=True) for s in yt]
    cell = max(BIV_CELL_MM, max(a + b for a, b in zip(xw, xw[1:])) / 2 + 1.2, lh + 0.6)
    axis_x = max(yw) + 1.0 + BIV_TICK_MM
    grid_left = axis_x + BIV_AXIS_GAP_MM
    grid_right = grid_left + n * cell
    y_title_base = label_pt * PTMM * 0.85
    arrow_top = lh + 0.6
    grid_top = arrow_top + BIV_ARROW_MM
    grid_bottom = grid_top + n * cell
    x_axis_y = grid_bottom + BIV_AXIS_GAP_MM
    x_tick_base = x_axis_y + BIV_TICK_MM + label_pt * PTMM * 0.9
    x_title_base = x_tick_base + lh + 0.2
    bottom = x_title_base + label_pt * PTMM * 0.3
    nodata = None
    if block.get("nodata"):
        nodata = {"y": bottom + 1.2, "h": SWATCH_H_MM}
        bottom = nodata["y"] + SWATCH_H_MM
    arrow_right = grid_right + BIV_ARROW_MM
    w = max(arrow_right + 0.6, grid_right + xw[-1] / 2 + 0.2,
            axis_x + tm.width_mm(block["y"]["title"], label_pt, 600), grid_left + tm.width_mm(block["x"]["title"], label_pt, 600))
    if nodata:
        w = max(w, SWATCH_W_MM + 2.0 + tm.width_mm(block["nodata"]["label"], label_pt, 400))
    if block.get("note"):
        w = max(w, tm.width_mm(block["note"], label_pt - 1, 400))
    return {"n": n, "cell": cell, "axis_x": axis_x, "grid_left": grid_left, "grid_right": grid_right, "grid_top": grid_top, "grid_bottom": grid_bottom,
            "arrow_top": arrow_top, "arrow_right": arrow_right, "x_axis_y": x_axis_y, "x_tick_base": x_tick_base, "x_title_base": x_title_base,
            "y_title_base": y_title_base, "nodata": nodata, "w": w, "h": bottom + (tm.line_height_mm(label_pt - 1) if block.get("note") else 0.0)}


def nested_layout(block: dict, label_pt: float = LABEL_PT) -> dict:
    """A nested proportional key: the circles share their bottom point, largest at the back; each value sits to the right at the height of its
    circle's top, joined to it by a leader, the labels pushed apart to at least a line's height."""
    items = sorted(block["items"], key=lambda it: -float(it["size_mm"]))
    lh = tm.line_height_mm(label_pt)
    dmax = float(items[0]["size_mm"]) if items else 0.0
    cx = dmax / 2
    bottom = max(dmax, lh)
    tops = [bottom - float(it["size_mm"]) for it in items]
    ys = []
    for t_ in tops:                                         # from the largest (highest top) down, keep a line apart; small circles let the
        ys.append(max(t_, (ys[-1] + lh * 0.95) if ys else t_))   # labels run below their bottom rather than above the largest
    shift = -min(0.0, min(ys) - lh * 0.6) if ys else 0.0   # the first label's cap height above the largest top pushes everything down
    tops = [t_ + shift for t_ in tops]
    ys = [y + shift for y in ys]
    bottom += shift
    lx = cx + dmax / 2 + NESTED_LEADER_MM
    lw = max((tm.width_mm(it["label"], label_pt, 400, tnum=True) for it in items), default=0.0)
    return {"items": items, "cx": cx, "bottom": bottom, "tops": tops, "label_ys": ys, "label_x": lx + 0.6, "leader_x": lx,
            "w": lx + 0.6 + lw, "h": max(bottom, (ys[-1] + lh * 0.4) if ys else 0.0)}


# ---------------------------------------------------------------------------------------------------------------- sizing
TITLE_WRAP_MM = 48.0          # 1.2.0 (Days 3 and 4): a block title wraps at its content's width or this, whichever is wider


def title_lines(block: dict, label_pt: float = LABEL_PT, title_pt: float = TITLE_PT, wrap: bool = False) -> list[str]:
    """The block title as drawn. With `wrap` (fit_legend's second pass, 1.2.0): a title wider than the block's own content (or TITLE_WRAP_MM)
    wraps onto as many lines as it needs, so a long legend title no longer widens the legend until MAP-SPEC-005 blocks the map."""
    t = block.get("title")
    if not t:
        return []
    if not wrap or tm.width_mm(t, title_pt, 600) <= TITLE_WRAP_MM:
        return [t]
    content_w, _ = _content_size(block, label_pt)
    return tm.wrap(t, max(content_w, TITLE_WRAP_MM), title_pt, 600)


def block_size(block: dict, label_pt: float = LABEL_PT, title_pt: float = TITLE_PT, wrap: bool = False) -> tuple[float, float]:
    """(width, height) in mm for one block drawn in a single column."""
    w, h = 0.0, 0.0
    lines = title_lines(block, label_pt, title_pt, wrap)
    if lines:
        w = max(tm.width_mm(x, title_pt, 600) for x in lines)
        h += tm.line_height_mm(title_pt) * len(lines) + 1.0
    cw, ch = _content_size(block, label_pt)
    return max(w, cw), h + ch


def _content_size(block: dict, label_pt: float = LABEL_PT) -> tuple[float, float]:
    """(width, height) of a block below its title."""
    w, h = 0.0, 0.0
    if block["kind"] == "bivariate":
        g = bivariate_layout(block, label_pt)
        return max(w, g["w"]), h + g["h"]
    if block["kind"] == "proportional" and block.get("nested"):
        g = nested_layout(block, label_pt)
        return max(w, g["w"]), h + g["h"]
    if block["kind"] == "ramp":                              # the bar, then its labels under it (the end labels may overhang half their width)
        labs = [it["label"] for it in block["items"]]
        overhang = (tm.width_mm(labs[0], label_pt, 400, tnum=True) + tm.width_mm(labs[-1], label_pt, 400, tnum=True)) / 2 if labs else 0.0
        w = max(w, RAMP_W_MM + overhang)
        h += RAMP_H_MM + 0.8 + tm.line_height_mm(label_pt)
        if block.get("note"):
            w = max(w, tm.width_mm(block["note"], label_pt - 1, 400))
            h += tm.line_height_mm(label_pt - 1)
        return w, h
    if block["kind"] == "image":
        for it in block["items"]:
            w = max(w, float(it["w_mm"]))
            h += float(it["h_mm"]) + ROW_GAP_MM
        if block.get("note"):
            w = max(w, tm.width_mm(block["note"], label_pt - 1, 400))
            h += tm.line_height_mm(label_pt - 1)
        return w, h
    row_h = max(SWATCH_H_MM, tm.line_height_mm(label_pt))
    for it in block["items"]:
        lw = tm.width_mm(it["label"], label_pt, 400, tnum=True)
        icon = max(SWATCH_W_MM, it.get("size_mm", 0) + 1.5) if block["kind"] != "proportional" else max(SWATCH_W_MM, it.get("size_mm", 0))
        w = max(w, icon + 2.0 + lw)
        h += max(row_h, it.get("size_mm", 0) + 0.8) + ROW_GAP_MM
    if block.get("note"):
        w = max(w, tm.width_mm(block["note"], label_pt - 1, 400))
        h += tm.line_height_mm(label_pt - 1)
    return w, h - ROW_GAP_MM if block["items"] else h


def gap_after(blocks: list[dict], i: int) -> float:
    """The space under block i: a row gap when the next block continues it (`legend: {continues: <layer>}`, no heading of its own), so the
    two read as one list; the block gap otherwise."""
    return ROW_GAP_MM if i + 1 < len(blocks) and blocks[i + 1].get("continues") else BLOCK_GAP_MM


def measure(blocks: list[dict], columns: int = 1, label_pt: float = LABEL_PT, title_pt: float = TITLE_PT, header: str | None = None,
            wrap: bool = False) -> dict:
    """Total size for the blocks stacked into `columns` columns (blocks are never split across columns)."""
    sizes = [block_size(b, label_pt, title_pt, wrap) for b in blocks]
    cols: list[list[int]] = [[] for _ in range(max(1, columns))]
    heights = [0.0] * len(cols)
    for i, (w, h) in enumerate(sizes):                       # fill the shortest column first; a continuation stays with the block above
        j = cols.index(next(c for c in cols if c and c[-1] == i - 1)) if blocks[i].get("continues") and i else heights.index(min(heights))
        cols[j].append(i)
        heights[j] += h + gap_after(blocks, i)
    widths = [max((sizes[i][0] for i in c), default=0.0) for c in cols]
    head_w = tm.width_mm(header, title_pt + 0.5, 700) if header else 0.0
    head_h = (tm.line_height_mm(title_pt + 0.5) + 1.5) if header else 0.0
    inner_w = max(sum(widths) + COL_GAP_MM * (len(cols) - 1), head_w)
    inner_h = max(heights, default=0.0) - BLOCK_GAP_MM + head_h
    return {"width": inner_w + 2 * PAD_MM, "height": max(inner_h, 0.0) + 2 * PAD_MM, "columns": cols, "col_widths": widths,
            "label_pt": label_pt, "title_pt": title_pt, "wrap": wrap}


def fit_legend(blocks: list[dict], max_w_mm: float, max_h_mm: float, *, columns: int = 1, template_kind: str = "print",
               header: str | None = None, start_pt: float | None = None) -> dict:
    """Fit the legend into the room the template allows. Tries more columns, then a smaller font down to the minimum.

    Returns {fits, size, columns, label_pt, notes}. If it cannot fit above the minimum font size, fits is False and the caller
    (layout) reports MAP-SPEC-005 or moves the legend to a larger region; text is never shrunk below the template minimum.
    """
    floor = MIN_LABEL_PT["print" if template_kind == "print" else "slide"]
    start = max(float(start_pt or LABEL_PT), floor)       # a template's own legend size, never below the minimum
    notes: list[str] = []
    # 1.2.0 (Days 3 and 4): the first pass keeps every block title on one line, as before; when nothing fits, a second pass wraps long titles
    # before the legend is given up on, so a 70-character title costs a line, not the map
    for wrap in (False, True):
        if wrap and not any(b.get("title") and tm.width_mm(b["title"], start + 1.0, 600) > TITLE_WRAP_MM for b in blocks):
            break
        for cols in range(max(1, columns), 5):
            pt = start
            while pt >= max(floor, 6.0) - 1e-9:
                m = measure(blocks, cols, pt, pt + 1.0, header, wrap)
                if m["width"] <= max_w_mm and m["height"] <= max_h_mm:
                    if wrap:
                        notes.append("long legend titles wrapped onto more lines to fit")
                    if cols != columns:
                        notes.append(f"legend set in {cols} columns to fit")
                    if pt != start:
                        notes.append(f"legend text reduced to {pt:g} pt to fit")
                    return {"fits": True, "size": m, "columns": cols, "label_pt": pt, "notes": notes, "wrap": wrap}
                pt = round(pt - 0.5, 2)
    m = measure(blocks, 4, floor, floor + 1.0, header, True)
    return {"fits": False, "size": m, "columns": 4, "label_pt": floor, "wrap": True,
            "notes": [f"legend needs {m['width']:.0f} x {m['height']:.0f} mm; the template allows {max_w_mm:.0f} x {max_h_mm:.0f} mm"]}


# ---------------------------------------------------------------------------------------------------------------- checks
def check_blocks(blocks: list[dict], layers: list[dict], classified: dict) -> list[dict]:
    """MAP-CART-005 / 006 findings for a built legend. `classified` maps layer id -> Classification for symbolized layers."""
    out = []

    def f(i, sev, msg, detail=None):
        out.append({"id": i, "severity": sev, "message": msg, "detail": detail})

    have = {b["layer"] for b in blocks}
    for ly in layers:
        lg = ly.get("legend") or {}
        meaningful = ly.get("role") in ("choropleth", "heat", "proportional", "points", "lines", "polygons", "raster") and lg.get("include", True)
        if ly.get("role") in ("context", "labels", "contour", "hillshade"):
            continue
        if str((ly.get("symbol") or {}).get("shape", "")).lower() == "none":
            continue                                     # 1.2.0: a label-only point layer draws no symbol, so it needs no key
        if meaningful and ly["id"] not in have:
            f("MAP-CART-005", "BLOCK", f"layer '{ly['id']}' is symbolized but has no legend entry", ly["id"])
    for b in blocks:
        if b["kind"] in ("classes", "categories"):
            if not b.get("title") and not b.get("continues"):
                f("MAP-CART-005", "BLOCK", f"legend for '{b['layer']}' has no title (name the measure and unit)", b["layer"])
            c = classified.get(b["layer"])
            if c is not None and c.nodata and not any(i.get("nodata") for i in b["items"]):
                f("MAP-CART-005", "BLOCK", f"'{b['layer']}' has {c.nodata} features with no data but the legend has no No data class", b["layer"])
            labels = [i["label"] for i in b["items"] if not i.get("nodata")]
            if len(set(labels)) != len(labels):
                f("MAP-CART-006", "WARN", f"legend labels for '{b['layer']}' repeat", labels)
            if b["kind"] == "classes":
                decs = {len(x.split(".")[1]) if "." in x else 0 for lab in labels for x in _numbers(lab)}
                if len(decs) > 1:
                    f("MAP-CART-006", "WARN", f"legend labels for '{b['layer']}' mix decimal places", labels)
    return out


def _numbers(label: str) -> list[str]:
    import re
    return re.findall(r"\d[\d,]*\.?\d*", label)
