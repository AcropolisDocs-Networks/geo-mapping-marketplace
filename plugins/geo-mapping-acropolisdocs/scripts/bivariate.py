#!/usr/bin/env python3
"""bivariate: a 3 x 3 (or 2 x 2) bivariate choropleth, two numeric fields shaded at once (gap P2-5, M15).

A layer asks for it with `role: choropleth` and a `bivariate:` block (not a role of its own):

    - id: tracts
      role: choropleth
      bivariate:
        x: {field: sites_per_10k, title: "Sites per 10,000 people"}          # or just the field name
        y: {field: income_k, title: "Median household income ($k)", method: quantile}
        classes: 3                                                             # 3 (default) or 2, the same on both axes
        patterns: auto                                                         # auto (default), true, false: hatch the rows (y classes)
        swap_colors: false                                                     # put the y hue on the x axis instead

Why a block on a choropleth and not a role: a bivariate map is a choropleth (one fill per area), so every rule about areas holds unchanged:
the equal-area projection, background-coloured outlines, the water-only context pack, admin names off the data, MAP-CART-001 (rates, not
counts, now for both fields), and every reader of `l.cls` (the web page, the QGIS project) draws it as nine classes without knowing more.

Each axis is classed on its own (quantile by default; any classify method, `breaks:` for manual, `edges:` from an atlas) into the same number
of classes. A feature's class is y * n + x. The colours are `colorlib.bivariate_grid` from the two arms of the brand's diverging ramp (its
neutral middle to its two ends), computed from the tokens on every build; the brand lint recomputes the same grid to allow them.

The legend is the grid as a square (not rotated: the class edges read along the two axes), the y field's title above it with an arrow up the
left side, the x field's title under it with an arrow along the bottom, and the class edges at the ticks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import classify as cf  # noqa: E402
import colorlib as cl  # noqa: E402

CLASSES = (2, 3)
DEFAULT_CLASSES = 3
MIN_FEATURES_PER_CELL = 9          # MAP-CART-018: fewer than this many features per cell on average and the grid shows noise


class BivariateError(ValueError):
    pass


def grid_for(tokens: dict, n: int = DEFAULT_CLASSES, swap: bool = False) -> list[list[str]]:
    """The n x n grid for a theme's tokens: the diverging ramp's middle as the low-low base, its last colour (the sequential hue) along x and its
    first colour along y; `swap` exchanges the two hues."""
    d = list(tokens.get("_brand_diverging_ramp") or tokens["diverging_ramp"])     # the brand's own ends under every look (0.17.0)
    base, hi, lo = d[len(d) // 2], d[-1], d[0]
    x_end, y_end = (lo, hi) if swap else (hi, lo)
    if cl.oklab(base)[0] <= 0.5:
        return _dark_grid(tokens, base, x_end, y_end, n)
    return cl.bivariate_grid(base, x_end, y_end, n)


DARK_LIFTS = (0.06, 0.08, 0.10, 0.12, 0.14, 0.18, 0.22)
DARK_FLOORS = (0.1, 0.0, -0.2, -0.4, -0.5, -0.6)


def _dark_grid(tokens: dict, base: str, x_end: str, y_end: str, n: int) -> list[list[str]]:
    """1.0.0 (TODO M15): on a dark ground the grid's start (lift) and its light ceiling (floor) are chosen, from a small fixed set, to make
    the closest pair of cells as far apart as possible under the worst colour-vision type, with the low-low cell kept off the land (8) and
    No data (4). Deterministic, so the brand lint recomputes the same grid. The light grid keeps its one construction."""
    import itertools
    best = None
    for lift in DARK_LIFTS:
        for floor in DARK_FLOORS:
            g = cl.bivariate_grid(base, x_end, y_end, n, lift=lift, floor=floor)
            flat = [c for row in g for c in row]
            if cl.cvd_min_delta_e(flat[0], tokens["land"]) < 8 or cl.cvd_min_delta_e(flat[0], tokens["no_data"]) < 4 or len(set(flat)) < n * n:
                continue
            m = min(cl.cvd_min_delta_e(a, b) for a, b in itertools.combinations(flat, 2))
            if best is None or m > best[0] + 1e-9:
                best = (m, g)
    return best[1] if best else cl.bivariate_grid(base, x_end, y_end, n)


def derived_colors(tokens: dict) -> set[str]:
    """Every colour a bivariate grid can take for these tokens (2 x 2 and 3 x 3, either hue on either axis): what MAP-BRAND-001 allows."""
    out = set()
    for n in CLASSES:
        for swap in (False, True):
            for row in grid_for(tokens, n, swap):
                out |= {c.upper() for c in row}
    return out


def _axis(cfg, key: str) -> dict:
    v = cfg.get(key)
    if isinstance(v, str):
        v = {"field": v}
    if not isinstance(v, dict) or not v.get("field"):
        raise BivariateError(f"bivariate.{key} needs a field (bivariate: {{x: field_a, y: field_b}})")
    return dict(v)


def config(ly: dict) -> dict:
    cfg = ly.get("bivariate") or {}
    if not isinstance(cfg, dict):
        raise BivariateError("bivariate is a block: {x: field, y: field}")
    x, y = _axis(cfg, "x"), _axis(cfg, "y")
    if x["field"] == y["field"]:
        raise BivariateError(f"bivariate.x and bivariate.y are both '{x['field']}'; a bivariate map needs two different fields")
    n = int(cfg.get("classes", DEFAULT_CLASSES))
    if n not in CLASSES:
        raise BivariateError(f"bivariate.classes is 2 or 3 (a 2 x 2 or 3 x 3 grid), not {n}")
    return {"x": x, "y": y, "classes": n, "patterns": cfg.get("patterns", "auto"), "swap": bool(cfg.get("swap_colors", False))}


def _title(ax: dict) -> str:
    return str(ax.get("title") or ax["field"].replace("_", " ").replace("-", " ").capitalize())


def classify_axis(gdf, ax: dict, n: int, which: str):
    """One axis: its field's numbers classed into n classes by the axis's method (quantile by default), or by the edges an atlas hands down."""
    f = ax["field"]
    if f not in gdf.columns:
        raise BivariateError(f"bivariate.{which}: '{f}' is not a column; columns are: {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
    try:
        vals = gdf[f].to_numpy(dtype="float64", na_value=np.nan)
    except (TypeError, ValueError):
        raise BivariateError(f"bivariate.{which}: '{f}' is not numeric; a bivariate map shades two numbers") from None
    if not np.isfinite(vals).any():
        raise BivariateError(f"bivariate.{which}: '{f}' holds no numbers")
    fmt = cf.number_format(ax.get("format"))
    method = cf.ALIASES.get(ax.get("method", "quantile"), ax.get("method", "quantile"))
    if method == "categorical":
        raise BivariateError(f"bivariate.{which}: a bivariate axis is numeric; '{method}' is not a numeric method")
    try:
        if ax.get("edges"):
            res = cf.classify_fixed(vals, ax["edges"], int(ax.get("decimals", 0)), fmt, bool(ax.get("integer_data")),
                                    method=f"{method.replace('_', ' ')}, edges shared across the atlas")
        else:
            res = cf.classify_numeric(vals, method, n, ax.get("breaks"), fmt, rounding=ax.get("rounding", cf.DEFAULT_ROUNDING))
    except cf.ClassifyError as ex:
        raise BivariateError(f"bivariate.{which} ('{f}'): {ex}") from None
    if res.k != n:
        raise BivariateError(f"bivariate.{which} ('{f}'): {res.k} classes came out, not {n} ({'; '.join(res.notes) or 'too few distinct values'}); "
                             f"use classes: {max(2, min(res.k, 3))} or another method")
    return res, vals, fmt


def edge_labels(res, fmt) -> list[str]:
    """The class edges as the axis ticks show them: n + 1 numbers, at the precision the class labels use."""
    d = 0 if (res.integer_data and res.decimals == 0) else res.decimals
    return [fmt.fmt(float(e), d) for e in res.edges]


def style(plan, l) -> None:
    """Classify both axes, build the combined classes and the grid's colours, the legend block and the notes. Called from map_plan.style_layer."""
    import legend as lg
    import map_plan as mp
    import patterns as pt
    t, ly = plan.tokens, l.spec
    try:
        cfg = config(ly)
        n = cfg["classes"]
        xr, xv, xf = classify_axis(l.gdf, cfg["x"], n, "x")
        yr, yv, yf = classify_axis(l.gdf, cfg["y"], n, "y")
    except BivariateError as ex:
        raise mp.PlanError(f"layer '{l.id}': {ex}", 2)
    if ly.get("value_field") or ly.get("classification"):
        plan.notes.append(f"layer '{l.id}': bivariate classes come from bivariate.x and bivariate.y; value_field and classification are not used")
    ok = (xr.assigned >= 0) & (yr.assigned >= 0)
    assigned = np.where(ok, yr.assigned * n + xr.assigned, -1)
    cells = grid_for(t, n, cfg["swap"])
    colors = [cells[j][i] for j in range(n) for i in range(n)]
    xt, yt = _title(cfg["x"]), _title(cfg["y"])
    labels = [f"{xt}: {xr.labels[i]}; {yt}: {yr.labels[j]}" for j in range(n) for i in range(n)]
    res = cf.Classification(method=f"bivariate {n} x {n} ({xr.method.replace('_', ' ')} x {yr.method.replace('_', ' ')})", kind="bivariate",
                            labels=labels, counts=[int((assigned == k).sum()) for k in range(n * n)], assigned=assigned,
                            nodata=int((~ok).sum()), notes=[f"x: {m}" for m in xr.notes] + [f"y: {m}" for m in yr.notes])
    res.fixed = bool(cfg["x"].get("edges") and cfg["y"].get("edges"))
    l.cls, l.colors, l.palette_kind = res, colors, "bivariate"
    l.value_field = None
    import a11y_lint
    weak = a11y_lint.weak_pairs(colors, "bivariate")
    try:
        row_pats, pnote = pt.resolve(cfg["patterns"], n, weak, order=pt.BIVARIATE_ORDER[n])
    except pt.PatternError as ex:
        raise mp.PlanError(f"layer '{l.id}': bivariate.patterns: {ex}", 2)
    pats = [row_pats[j] for j in range(n) for i in range(n)] if row_pats else None
    if pats:
        l.symbol["_patterns"] = pats
        l.notes.append(pnote + " (one pattern per row of the grid: the y classes)")
    l.symbol["_bivariate"] = {"n": n, "x": {"field": cfg["x"]["field"], "title": xt, "edges": [float(e) for e in xr.edges], "method": xr.method,
                                            "decimals": int(xr.decimals), "integer_data": bool(xr.integer_data), "labels": list(xr.labels)},
                              "y": {"field": cfg["y"]["field"], "title": yt, "edges": [float(e) for e in yr.edges], "method": yr.method,
                                    "decimals": int(yr.decimals), "integer_data": bool(yr.integer_data), "labels": list(yr.labels)},
                              "values": {"x": xv, "y": yv}, "swap": cfg["swap"]}
    lg_cfg = ly.get("legend") or {}
    block = {"layer": l.id, "title": lg_cfg.get("title"), "kind": "bivariate", "n": n,
             "cells": [[{"fill": cells[j][i], "pattern": (row_pats[j] if row_pats else None), "count": res.counts[j * n + i]} for i in range(n)] for j in range(n)],
             "x": {"title": xt, "ticks": edge_labels(xr, xf)}, "y": {"title": yt, "ticks": edge_labels(yr, yf)},
             "stroke": t["background"], "note": lg_cfg.get("note"),
             # the same classes as a plain list, for a reader that does not draw the grid (the web page, the .pptx adapter)
             "items": [{"label": labels[k], "fill": colors[k], "stroke": t["legend_frame"], "count": res.counts[k]} for k in range(n * n)]}
    if res.nodata:
        block["nodata"] = {"label": lg.NO_DATA_LABEL, "fill": t["no_data"], "count": res.nodata}
        block["items"].append({"label": lg.NO_DATA_LABEL, "fill": t["no_data"], "stroke": t["legend_frame"], "count": res.nodata, "nodata": True})
    l.legend_block = block
    msg = (f"bivariate {n} x {n}: x = {cfg['x']['field']} ({xr.method.replace('_', ' ')}: {', '.join(edge_labels(xr, xf))}), "
           f"y = {cfg['y']['field']} ({yr.method.replace('_', ' ')}: {', '.join(edge_labels(yr, yf))}); colours from the brand's diverging ramp, blended in Oklab")
    l.notes.append(msg)
    l.transformations.append(msg)
    plan.palette_reports[l.id] = cf.palette_report(colors, t["background"])


def summary(l) -> str:
    """One sentence for the alt text."""
    b = (l.symbol or {}).get("_bivariate") or {}
    if not b:
        return ""
    n = b["n"]
    return (f"{l.title}: a {n} by {n} bivariate shading of {b['x']['title']} (left to right, {b['x']['labels'][0].lower()} to {b['x']['labels'][-1].lower()}) "
            f"and {b['y']['title']} (bottom to top, {b['y']['labels'][0].lower()} to {b['y']['labels'][-1].lower()}); the top right of the key is high on both.")
