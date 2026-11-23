#!/usr/bin/env python3
"""route_strip: a line's stops drawn straight, with one stacked bar per stop (3.7.0, spec B4; the Day 23 7-train panel).

    elements:
      strip:
        on: true
        layer: stops                 # the stops (points)
        label_field: stop_name
        order_field: seq             # or line: <line layer>, to order the stops along it; else west to east
        line_label: "7 train"
        stack: {layer: restaurants, by: family, normalize: true, join: stop_name, max_m: 500}
        callout_field: standout      # a word above each bar (the stop's stand-out cuisine)
        marks: [{between: [Grand Central, Vernon Blvd], text: East River}]
        position: lower-right        # or rect_mm; w_mm, h_mm
        key: true                    # draw the key under the strip (the strip then carries the layer's key: legend may be off)

Each point of the stack layer goes to the stop its `join` column names, else to its nearest stop within `max_m` (straight line, in the map's
projection). The bars use the stack layer's own classes and colours, in the key's order from the bottom, with the classes named in its
`classification.background` on top. Drawn by the figure composer (map_extras.place_figures) like a section.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_draw as sd  # noqa: E402

PT = 0.3528


class StripError(Exception):
    pass


def _layer(plan, lid: str):
    l = next((x for x in plan.layers if x.id == lid), None)
    if l is None:
        raise StripError(f"elements.strip: there is no layer '{lid}'")
    return l


def stop_order(plan, cfg: dict, stops) -> list[int]:
    of = cfg.get("order_field")
    if of:
        if of not in stops.columns:
            raise StripError(f"elements.strip: the stops have no column '{of}'")
        return list(np.argsort(np.asarray(stops[of], dtype="float64"), kind="stable"))
    if cfg.get("line"):
        ln = _layer(plan, cfg["line"]).gdf
        from shapely.ops import linemerge, unary_union
        geom = unary_union(list(ln.geometry))
        try:
            geom = linemerge(geom)
        except Exception:  # noqa: BLE001
            pass
        if geom.geom_type == "MultiLineString":
            geom = max(geom.geoms, key=lambda g: g.length)
        d = [geom.project(p.centroid) for p in stops.geometry]
        return list(np.argsort(d, kind="stable"))
    return list(np.argsort([p.centroid.x for p in stops.geometry], kind="stable"))


def shares(plan, cfg: dict, stops, order: list[int]) -> tuple[list[list[int]], list[str], list[str], list[int]]:
    """(counts per stop per class in the drawing order, class labels, colours, totals)."""
    st = cfg.get("stack") or {}
    pl = _layer(plan, st.get("layer") or "")
    if pl.cls is None:
        raise StripError(f"elements.strip: the stack layer '{pl.id}' has no classes (give it a categorical classification)")
    labels = list(pl.cls.labels)
    colors = list(pl.colors)
    assigned = np.asarray(pl.cls.assigned) if pl.cls.assigned is not None and len(pl.cls.assigned) == len(pl.gdf) else np.full(len(pl.gdf), -1)
    bg = [str(x) for x in ((pl.spec.get("classification") or {}).get("background") or (pl.symbol or {}).get("background") or [])]
    cls_order = [i for i in range(len(labels)) if labels[i] not in bg] + [i for i in range(len(labels)) if labels[i] in bg]
    lf = cfg.get("label_field")
    names = [str(stops.iloc[i][lf]) if lf else str(i + 1) for i in order]
    pick = np.full(len(pl.gdf), -1)
    join = st.get("join")
    if join:
        if join not in pl.gdf.columns:
            raise StripError(f"elements.strip: the stack layer has no column '{join}'")
        pos = {n: k for k, n in enumerate(names)}
        pick = np.array([pos.get(str(v), -1) for v in pl.gdf[join]])
    else:
        max_m = float(st.get("max_m") or 500.0)
        sx = np.array([stops.geometry.iloc[i].centroid.x for i in order])
        sy = np.array([stops.geometry.iloc[i].centroid.y for i in order])
        cx = np.array([g.centroid.x if g is not None and not g.is_empty else np.nan for g in pl.gdf.geometry])
        cy = np.array([g.centroid.y if g is not None and not g.is_empty else np.nan for g in pl.gdf.geometry])
        unit = 1.0
        try:
            if pl.gdf.crs is not None and pl.gdf.crs.is_geographic:
                unit = 111_320.0
        except Exception:  # noqa: BLE001
            pass
        for j in range(len(cx)):
            if not np.isfinite(cx[j]):
                continue
            d = np.hypot(sx - cx[j], sy - cy[j]) * unit
            k = int(np.argmin(d))
            if d[k] <= max_m:
                pick[j] = k
    counts = [[int(((pick == s) & (assigned == ci)).sum()) for ci in cls_order] for s in range(len(order))]
    totals = [sum(c) for c in counts]
    return counts, [labels[i] for i in cls_order], [colors[i] for i in cls_order], totals


def svg(plan, cfg: dict, w_mm: float, h_mm: float, label_pt: float) -> tuple[str, list[float], list[str], dict]:
    t = plan.tokens
    ink, bg, line_col = t["label"], t.get("background") or t.get("land") or "white", t.get("emphasis") or t["label"]
    stops = _layer(plan, cfg.get("layer") or "").gdf
    order = stop_order(plan, cfg, stops)
    counts, clabels, colors, totals = shares(plan, cfg, stops, order)
    lf = cfg.get("label_field")
    names = [str(stops.iloc[i][lf]) if lf else str(k + 1) for k, i in enumerate(order)]
    cf = cfg.get("callout_field")
    callouts = [("" if cf is None or stops.iloc[i][cf] is None or (isinstance(stops.iloc[i][cf], float) and math.isnan(stops.iloc[i][cf]))
                 else str(stops.iloc[i][cf])) for i in order]
    n = len(order)
    pad = 2.5
    key_on = cfg.get("key", True) is not False
    key_h = (label_pt * PT * 1.6 * math.ceil(len(clabels) / 4)) + 1.5 if key_on else 0.0
    longest = max((len(x) for x in names), default=4)
    name_h = min(label_pt * PT * 0.9 * 6.5, (longest * (label_pt - 1.5) * PT * 0.55) * math.sin(math.radians(55)) + 2.5)   # rotated names
    call_h = label_pt * PT * 1.4 if any(callouts) else 0.0
    title_h = label_pt * PT * 1.5 if cfg.get("title") else 0.0
    plot_top = pad + title_h + call_h
    plot_bot = h_mm - pad - key_h - name_h - 2.0
    plot_h = max(8.0, plot_bot - plot_top)
    x0, x1 = pad + 2.0, w_mm - pad - 2.0
    step = (x1 - x0) / max(n - 1, 1)
    bw = max(0.6, min(step * 0.62, 6.0))
    out = [f'<rect x="0" y="0" width="{sd.num(w_mm)}" height="{sd.num(h_mm)}" fill="{bg}" fill-opacity="0.94" rx="1.2"/>']
    if cfg.get("title"):
        out.append(sd.text(pad, pad + label_pt * PT, str(cfg["title"]), label_pt, ink, weight=600))
    norm = (cfg.get("stack") or {}).get("normalize", True) is not False
    tmax = max(totals) if totals else 1
    xs = [x0 + k * step for k in range(n)]
    for k in range(n):
        tot = totals[k]
        y = plot_bot
        if tot <= 0:
            continue
        scale = plot_h / tot if norm else plot_h / max(tmax, 1)
        for ci, c in enumerate(counts[k]):
            if c <= 0:
                continue
            hh = c * scale
            out.append(f'<rect x="{sd.num(xs[k] - bw / 2)}" y="{sd.num(y - hh)}" width="{sd.num(bw)}" height="{sd.num(hh)}" fill="{colors[ci]}"/>')
            y -= hh
        if callouts[k]:
            top = plot_bot - (plot_h if norm else tot * scale)
            out.append(sd.text(xs[k], top - 1.0, callouts[k], label_pt - 1.5, ink, anchor="start", rotate=-55, weight=600, halo=bg, halo_w=0.4))
    yl = plot_bot + 1.2
    out.append(f'<line x1="{sd.num(x0)}" y1="{sd.num(yl)}" x2="{sd.num(x1)}" y2="{sd.num(yl)}" stroke="{line_col}" stroke-width="0.9" stroke-linecap="round"/>')
    for k in range(n):
        out.append(f'<circle cx="{sd.num(xs[k])}" cy="{sd.num(yl)}" r="0.75" fill="{bg}" stroke="{line_col}" stroke-width="0.35"/>')
        out.append(sd.text(xs[k] + 0.4, yl + 1.8, names[k], label_pt - 1.5, ink, anchor="end", rotate=-55))
    for mk in cfg.get("marks") or []:
        a, b = (mk.get("between") or [None, None])[:2]
        if a in names and b in names:
            xm = (xs[names.index(a)] + xs[names.index(b)]) / 2
            out.append(f'<line x1="{sd.num(xm)}" y1="{sd.num(plot_top)}" x2="{sd.num(xm)}" y2="{sd.num(yl)}" stroke="{ink}" stroke-width="0.25" stroke-dasharray="0.8 0.6"/>')
            out.append(sd.text(xm, plot_top - 0.6, str(mk.get("text") or ""), label_pt - 2, ink, anchor="middle"))
    if cfg.get("line_label"):
        out.append(sd.text(x1, yl - 1.0, str(cfg["line_label"]), label_pt - 1.5, line_col, anchor="end", weight=600))
    if key_on:
        ky = h_mm - pad - key_h + label_pt * PT
        cw = (w_mm - 2 * pad) / 4
        for i, (lab, col) in enumerate(zip(clabels, colors)):
            cx, cy = pad + (i % 4) * cw, ky + (i // 4) * label_pt * PT * 1.6
            out.append(f'<rect x="{sd.num(cx)}" y="{sd.num(cy - label_pt * PT * 0.75)}" width="2.4" height="{sd.num(label_pt * PT * 0.8)}" fill="{col}"/>')
            out.append(sd.text(cx + 3.2, cy, lab, label_pt - 1.5, ink))
    notes = [f"strip: {n} stops, {sum(totals):,} points of '{(cfg.get('stack') or {}).get('layer')}' in {len(clabels)} classes"]
    info = {"stops": names, "totals": totals, "classes": clabels,
            "shares": [[round(c / t_, 4) if t_ else 0.0 for c in row] for row, t_ in zip(counts, totals)]}
    return "".join(out), [0.0, 0.0, w_mm, h_mm], notes, info


def for_map(plan, cfg: dict, map_rect=None, label_pt: float = 8.0) -> dict:
    """{figure: a place_figures item, notes, info, carries_key: [layer ids]}"""
    w = float(cfg.get("w_mm") or (min(150.0, map_rect.w * 0.55) if map_rect is not None else 120.0))
    h = float(cfg.get("h_mm") or max(38.0, w * 0.32))
    inner, box, notes, info = svg(plan, cfg, w, h, float(cfg.get("label_pt") or label_pt))
    item = {"_svg": inner, "_box": box, "title": str(cfg.get("title") or "Strip"), "position": cfg.get("position", "lower-right"), "w_mm": w, "h_mm": h}
    if cfg.get("rect_mm"):
        item["rect_mm"] = cfg["rect_mm"]
    carries = [(cfg.get("stack") or {}).get("layer")] if cfg.get("key", True) is not False else []
    return {"figure": item, "notes": notes, "info": info, "carries_key": [x for x in carries if x]}
