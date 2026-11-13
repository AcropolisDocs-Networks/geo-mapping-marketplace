#!/usr/bin/env python3
"""look_lint: the visual hierarchy checks of the visual styling spec, section 8 (0.22.0, stage 4).

Run on every map whose look is not `classic` (classic draws what 0.16 drew and is held to the 0.16 rules). All from the plan; the visual
weight check reads the plan's geometry on the sheet, which is what the renderer draws. None of them blocks: each finding says what to change.

    MAP-CART-029  weight budget: no context line wider than 0.6 x the thinnest data line
    MAP-CART-030  contrast budget: context lines no stronger than 1.8:1 against the land while data is drawn
    MAP-CART-031  point symbols at least 1.4 mm across
    MAP-CART-032  point symbols overlapping on more than 15 percent of them after the size step-down
    MAP-CART-033  a sequential palette spans at least 50 L* from its lightest class to its darkest
    MAP-CART-034  categorical colours of small symbols (points under 3 mm, lines) at least delta E 25 apart under colour-vision simulation
    MAP-CART-035  the data carries more of the frame's visual weight than the context
"""
from __future__ import annotations

import math

WEIGHT_RATIO = 0.6
CONTEXT_CONTRAST_MAX = 1.8
POINT_MIN_MM = 1.4
OVERLAP_MAX = 0.15
SEQ_SPAN_MIN = 50.0
SMALL_SYMBOL_MM = 3.0
SMALL_DELTA_E = 25.0


def finding(fid: str, sev: str, message: str, detail=None, layer: str | None = None) -> dict:
    out = {"id": fid, "severity": sev, "message": message, "detail": detail}
    if layer:
        out["layer"] = layer
    return out


def _data(plan):
    return [l for l in plan.layers if l.role not in ("context", "contour") and l.kind != "raster" and l.spec.get("draw", True) is not False]


def _context(plan):
    return [l for l in plan.layers if l.role == "context"]


def _line_widths(l) -> list[float]:
    w = [float(x) for x in (l.symbol or {}).get("_widths") or []]
    return w or [float(l.stroke_w_mm or 0)]


HIDDEN_SHARE = 0.95


def hidden_under_fill(plan, ctx_layer) -> bool:
    """Whether a context line layer lies (95 percent of its sampled lines) under an opaque area fill drawn after it: a choropleth, grid or hexbin
    at full opacity covers it, so the reader never sees its weight (1.2.0, Day 9 issue 12)."""
    try:
        import numpy as np
        import map_plan as mp
        drawn = mp.draw_order(plan.layers)                # 1.13.0 (Day 12 issue 11): the order the layers are drawn in, not the order of the list
        order = [l.id for l in drawn]                     # (the context pack is listed after the data but drawn under it, so no fill ever covered it)
        i0 = order.index(ctx_layer.id)
        covers = [l for l in drawn[i0 + 1:] if l.role in ("choropleth", "grid", "hexbin") and l.kind == "polygon"
                  and float(getattr(l, "fill_opacity", 1.0) or 1.0) >= 1.0 and float(getattr(l, "group_opacity", 1.0) or 1.0) >= 1.0]
        g = ctx_layer.gdf
        if not covers or g is None or g.empty:
            return False
        geoms = g.geometry[~g.geometry.isna() & ~g.geometry.is_empty]
        if len(geoms) > 400:
            geoms = geoms.iloc[np.linspace(0, len(geoms) - 1, 400).astype(int)]
        pts = geoms.interpolate(0.5, normalized=True)
        inside = np.zeros(len(pts), bool)
        for c in covers:
            cg = c.gdf if c.gdf.crs == g.crs else c.gdf.to_crs(g.crs)
            hit = cg.sindex.query(pts.values, predicate="within")
            inside[np.unique(hit[0])] = True
        return bool(len(pts)) and bool(inside.mean() >= HIDDEN_SHARE)
    except Exception:                                     # noqa: BLE001 - a check that cannot decide keeps the line in the budget
        return False


def check_029(plan) -> list[dict]:
    lines = [l for l in _data(plan) if l.kind == "line"]
    ctx = [l for l in _context(plan) if l.kind == "line"]
    if not lines or not ctx:
        return []
    thin = min(min(_line_widths(l)) for l in lines)
    ctx = [l for l in ctx if not hidden_under_fill(plan, l)]           # 1.2.0 (Day 9 issue 12): a line no one can see has no weight
    over = [(l.id, float(l.stroke_w_mm)) for l in ctx if float(l.stroke_w_mm or 0) > WEIGHT_RATIO * thin + 1e-9]
    if not over:
        return []
    names = ", ".join(f"{i} ({w:g} mm)" for i, w in over[:3])
    return [finding("MAP-CART-029", "WARN", f"context lines as heavy as the data: {names} against the thinnest data line at {thin:g} mm (the budget is "
                    f"{WEIGHT_RATIO:g} x, {WEIGHT_RATIO * thin:.2f} mm); thicken the data lines or set context: {{roads: false}}",
                    {"thinnest_data_mm": thin, "context": dict(over)})]


def check_030(plan) -> list[dict]:
    import colorlib as cl
    if not _data(plan):
        return []
    land = plan.tokens["land"]
    out = []
    for l in _context(plan):
        if l.kind != "line" or not str(l.stroke or "").startswith("#"):
            continue
        c = cl.contrast(l.stroke, land)
        eff = 1 + (c - 1) * float(getattr(l, "stroke_opacity", 1.0) or 1.0)       # a see-through line reads about this strong
        if eff > CONTEXT_CONTRAST_MAX + 1e-9:
            out.append(finding("MAP-CART-030", "WARN", f"context layer '{l.id}' stands at {eff:.1f}:1 against the land (the budget is {CONTEXT_CONTRAST_MAX:g}:1 "
                               "while data is drawn): it competes with the data; use context: {opacity: 0.6} or a lighter neutral",
                               {"contrast": round(eff, 2)}, l.id))
    return out


def _point_d(l) -> float:
    sym = l.symbol or {}
    return float(sym["_size_mm"]) if sym.get("_size_mm") else float(sym.get("size", 3)) * 0.3528


def check_031(plan) -> list[dict]:
    out = []
    for l in _data(plan):
        if l.kind == "point" and l.role != "proportional" and _point_d(l) < POINT_MIN_MM - 1e-9:
            out.append(finding("MAP-CART-031", "WARN", f"layer '{l.id}': point symbols {_point_d(l):.2f} mm across, under the {POINT_MIN_MM:g} mm a reader "
                               "can find and tell apart; drop symbol.size (the look sizes points by count) or raise it", {"diameter_mm": round(_point_d(l), 3)}, l.id))
    return out


def check_032(plan) -> list[dict]:
    out = []
    for l in _data(plan):
        share = (l.symbol or {}).get("_overlap")
        if l.kind == "point" and share is not None and share > OVERLAP_MAX:
            out.append(finding("MAP-CART-032", "INFO", f"layer '{l.id}': {share:.0%} of the points still touch a neighbour at {_point_d(l):g} mm after the size "
                               "step-down; a hexbin (role: hexbin), thin: true or a larger sheet would show the pattern better", {"overlap": round(share, 3)}, l.id))
    return out


def check_033(plan) -> list[dict]:
    import colorlib as cl
    out = []
    for l in _data(plan):
        cols = [c for c in (l.colors or []) if isinstance(c, str) and c.startswith("#")]
        if getattr(l, "palette_kind", None) != "sequential" or len(cols) < 3:
            continue
        ls = [cl.lab(c)[0] for c in cols]
        span = max(ls) - min(ls)
        if span < SEQ_SPAN_MIN:
            out.append(finding("MAP-CART-033", "WARN", f"layer '{l.id}': the classes span {span:.0f} L* from lightest to darkest (at least {SEQ_SPAN_MIN:g} keeps "
                               "neighbouring classes apart); leave the palette to the look, which generates a full ramp from the brand colour, or pick a ramp "
                               "with a lighter low end", {"span": round(span, 1)}, l.id))
    return out


def check_034(plan) -> list[dict]:
    import colorlib as cl
    out = []
    for l in _data(plan):
        if getattr(l, "palette_kind", None) != "categorical" or not l.colors or len(l.colors) < 2:
            continue
        small = l.kind == "line" or (l.kind == "point" and l.role != "proportional" and _point_d(l) < SMALL_SYMBOL_MM)
        if not small:
            continue
        d, pair = cl.min_pair_delta_e([c for c in l.colors if isinstance(c, str) and c.startswith("#")])
        if pair and d < SMALL_DELTA_E:
            out.append(finding("MAP-CART-034", "WARN", f"layer '{l.id}': {pair[0]} and {pair[1]} are delta E {d:.0f} apart under colour-vision simulation; on "
                               f"symbols this small they need {SMALL_DELTA_E:g} (areas need 15); give the classes distinct shapes (class_shapes) or fewer classes",
                               {"delta_e": round(d, 1), "pair": list(pair)}, l.id))
    return out


def _mm_per_unit(plan) -> float:
    x0, y0, x1, y1 = plan.extent
    m = plan.tpl.get("map") or {}
    try:
        return min(float(m.get("w") or 0) / max(x1 - x0, 1e-9), float(m.get("h") or 0) / max(y1 - y0, 1e-9))
    except (TypeError, ValueError):
        return 0.0


def weight(l, plan, k: float) -> float:
    """A layer's ink inside the frame: area (mm²) times how far its colour stands from the land (L* difference / 100) times its opacity."""
    import colorlib as cl
    import numpy as np
    land_l = cl.lab(plan.tokens["land"])[0]
    g = l.gdf.geometry
    try:
        g = g.clip_by_rect(*plan.extent)
    except Exception:  # noqa: BLE001 - an unusual geometry: the whole layer
        pass
    g = g[~(g.is_empty | g.isna())]
    if not len(g):
        return 0.0
    cols = l.colors if (l.cls is not None and l.colors and len(l.cls.assigned) == len(l.gdf)) else None
    base = l.fill if l.kind != "line" else l.stroke
    dark = abs(land_l - cl.lab(base)[0]) / 100.0 if isinstance(base, str) and base.startswith("#") else 0.5
    if cols:
        ds = [abs(land_l - cl.lab(c)[0]) / 100.0 for c in cols if isinstance(c, str) and c.startswith("#")]
        dark = float(np.mean(ds)) if ds else dark
    op = float(getattr(l, "group_opacity", 1.0) or 1.0)
    if l.kind == "polygon":
        op *= float(l.fill_opacity or 1.0)
        return float(g.area.sum()) * k * k * dark * op
    if l.kind == "line":
        op *= float(getattr(l, "stroke_opacity", 1.0) or 1.0)
        return float(g.length.sum()) * k * float(np.mean(_line_widths(l))) * dark * op
    d = _point_d(l)
    return len(g) * math.pi * (d / 2) ** 2 * dark * op


def check_035(plan) -> list[dict]:
    if any(l.kind == "raster" for l in plan.layers):
        return []                                         # a surface's ink is a picture's; not weighed here
    data, ctx = _data(plan), _context(plan)
    if not data or not ctx:
        return []
    k = _mm_per_unit(plan)
    if k <= 0:
        return []
    try:
        wd = sum(weight(l, plan, k) for l in data)
        wc = sum(weight(l, plan, k) for l in ctx)
    except Exception:  # noqa: BLE001 - a layer the weighing cannot read: no finding rather than a wrong one
        return []
    if wc > wd:
        return [finding("MAP-CART-035", "INFO", f"the context carries more of the map's visual weight than the data ({wc:,.0f} against {wd:,.0f}, ink in mm² "
                        "weighted by contrast with the land): the eye goes to the roads and water first; thin the context (context: {preset: water}) or "
                        "strengthen the data", {"context": round(wc, 1), "data": round(wd, 1)})]
    return []


def lint(plan, report: dict | None = None, svg: str | None = None) -> list[dict]:
    if getattr(plan, "look", "classic") == "classic":
        return []
    out: list[dict] = []
    for fn in (check_029, check_030, check_031, check_032, check_033, check_034, check_035):
        out += fn(plan)
    return out
