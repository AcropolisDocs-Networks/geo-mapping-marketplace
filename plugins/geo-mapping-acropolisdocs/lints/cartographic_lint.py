"""Cartographic lint, MAP-CART-* (spec 12.3b). Reads the finished plan and the render report; no network, no writes.

    lint_plan(plan, report) -> [{"id", "severity", "message", "detail"}]

MAP-CART-005/006 (legend) and the minimum text size half of 007 are enforced where the legend and labels are built
(legend.check_blocks, label_engine); this module adds the rest, including 017 (place names that did not fit, M10). A BLOCK rule can be overridden for one layer with a
written justification: `layers[].override: {MAP-CART-001: "why"}`; the override is reported as an INFO finding.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import classify as cf  # noqa: E402
import colorlib as cl  # noqa: E402

MIN_JUSTIFICATION_CHARS = 12


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _override(layer_spec: dict, rule: str) -> str | None:
    j = (layer_spec.get("override") or {}).get(rule)
    return str(j).strip() if j and len(str(j).strip()) >= MIN_JUSTIFICATION_CHARS else None


def _gate(out: list, layer, rule: str, sev: str, message: str, detail=None):
    """Add a finding unless the layer carries a written justification for this rule (then add an INFO saying so)."""
    j = _override(layer.spec, rule) if sev == "BLOCK" else None
    if j:
        out.append(finding(rule, "INFO", f"layer '{layer.id}': {message} (overridden: {j})", detail))
    else:
        out.append(finding(rule, sev, message, detail))


def _areas(layer, crs: str):
    try:
        return layer.gdf.to_crs(crs).geometry.area.to_numpy()
    except Exception:  # noqa: BLE001
        return None


def check_001(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        if l.role != "choropleth" or not l.value_field or l.value_field not in l.gdf:
            continue
        if (l.spec.get("classification") or {}).get("method") == "categorical":
            continue
        unit = str(((l.spec.get("legend") or {}).get("title")) or "")
        res = cf.normalization_check(l.value_field, l.gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan), areas=_areas(l, plan.crs))
        if res["raw_count_suspected"] and not re.search(r"\b(per|%|percent|share|rate|ratio|density|average|median|mean)\b", unit, re.I):
            _gate(out, l, "MAP-CART-001", "BLOCK",
                  f"layer '{l.id}': '{l.value_field}' looks like a raw count over units of unequal size; {res['suggest']}", res["reasons"])
    return out


RATE_WORDS = re.compile(r"\b(per|%|percent|share|rate|ratio|density|average|median|mean)\b", re.I)


def check_001_bivariate(plan) -> list[dict]:
    """MAP-CART-001 for both fields of a bivariate map (M15): each axis must be a rate, not a count, by the same rules as a choropleth's value
    field (the field's name, whole numbers that rise with unit area), unless its axis title states the unit as a rate."""
    out = []
    for l in plan.layers:
        b = (l.symbol or {}).get("_bivariate")
        if l.role != "choropleth" or not b:
            continue
        areas = _areas(l, plan.crs)
        for ax in ("x", "y"):
            f_ = b[ax]["field"]
            res = cf.normalization_check(f_, np.asarray(b["values"][ax], dtype="float64"), areas=areas)
            if res["raw_count_suspected"] and not RATE_WORDS.search(str(b[ax]["title"])):
                _gate(out, l, "MAP-CART-001", "BLOCK", f"layer '{l.id}': bivariate {ax} '{f_}' looks like a raw count over units of unequal size; a bivariate "
                      f"map compares two rates. {res['suggest']}", res["reasons"])
    return out


def check_018(plan) -> list[dict]:
    """MAP-CART-018 (M15): a bivariate map needs enough areas for its grid. With n x n cells and fewer than MIN_FEATURES_PER_CELL features per
    cell on average, most cells hold a handful of areas and the pattern is noise; WARN, and say how many cells are empty."""
    import bivariate as bv
    out = []
    for l in plan.layers:
        b = (l.symbol or {}).get("_bivariate")
        if not b or l.cls is None:
            continue
        cells = b["n"] * b["n"]
        n = int(sum(l.cls.counts))
        empty = sum(1 for c in l.cls.counts if c == 0)
        if n / cells < bv.MIN_FEATURES_PER_CELL:
            out.append(finding("MAP-CART-018", "WARN", f"layer '{l.id}': {n} areas over {cells} bivariate cells is {n / cells:.1f} per cell (fewer than "
                               f"{bv.MIN_FEATURES_PER_CELL}); {empty} cell(s) are empty. Use classes: 2 (a 2 x 2 grid), or more, smaller areas", {"features": n, "cells": cells, "empty": empty}))
        elif empty:
            out.append(finding("MAP-CART-018", "INFO", f"layer '{l.id}': {empty} of {cells} bivariate cells hold no area; they stay in the key", empty))
    return out


def check_019(plan, rendered_svg: str | None) -> list[dict]:
    """MAP-CART-019 (M15): graduated line widths read back from the rendered SVG: each class wider than the one before by at least
    symbology.GRAD_MIN_STEP_MM, the thinnest at least GRAD_THINNEST_MM, so the widths can be told apart on paper."""
    import symbology as sy
    out = []
    if not rendered_svg:
        return out
    for l in plan.layers:
        if l.palette_kind != "graduated":
            continue
        m = re.search(r'<g id="gm-layer-' + re.escape(l.id) + r'"[^>]*>(.*?)</g></g>', rendered_svg, re.S)
        body = m.group(1) if m else ""
        widths = sorted({float(w) for w in re.findall(r'data-gm-class-width="([0-9.]+)"', body)})          # 0.24.0: the data attribute first
        if not widths:
            widths = sorted({float(w) for w in re.findall(r'<g fill="none" stroke="[^"]+" stroke-width="([0-9.]+)"', body)})
        if not widths:
            out.append(finding("MAP-CART-019", "WARN", f"layer '{l.id}': graduated widths were asked for but no class line was drawn", None))
            continue
        steps = [b - a for a, b in zip(widths, widths[1:])]
        if widths[0] < sy.GRAD_THINNEST_MM or any(s_ < sy.GRAD_MIN_STEP_MM - 1e-6 for s_ in steps):
            out.append(finding("MAP-CART-019", "WARN", f"layer '{l.id}': graduated widths {', '.join(f'{w:g}' for w in widths)} mm are too close or too thin to tell apart "
                               f"(at least {sy.GRAD_MIN_STEP_MM:g} mm between classes, the thinnest {sy.GRAD_THINNEST_MM:g} mm); set symbol.widths_mm or use fewer classes", widths))
    return out


def check_002(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        c = l.cls
        if l.role not in ("choropleth", "hexbin", "grid") or c is None or c.kind != "numeric":   # hex and grid cells are classed fills too (0.4.0)
            continue
        e = list(c.edges)
        spec_c = l.spec.get("classification") or {}
        asked = spec_c.get("classes")
        if c.k < cf.MIN_CLASSES or c.k > cf.MAX_CLASSES:
            ht = getattr(c, "method", "") == "head_tail"                 # 0.23.0: head/tail breaks stop early on data that is not heavy-tailed
            out.append(finding("MAP-CART-002", "BLOCK", f"layer '{l.id}': a choropleth needs {cf.MIN_CLASSES} to {cf.MAX_CLASSES} classes, this has {c.k}"
                               + ("; head/tail breaks found only this many because the values are not heavy-tailed: use method quantile (or jenks) with 5 classes"
                                  if ht else ""), c.k))
        elif asked is not None and spec_c.get("method") != "manual" and not (cf.MIN_CLASSES <= int(asked) <= cf.MAX_CLASSES):
            out.append(finding("MAP-CART-002", "WARN", f"layer '{l.id}': {asked} classes were requested; a choropleth needs {cf.MIN_CLASSES} to {cf.MAX_CLASSES}, so {c.k} are used", asked))
        # Jenks may isolate the top value as a one-value class, which repeats the last edge on purpose
        body = e[:-1] if len(e) >= 3 and e[-1] == e[-2] else e
        if any(b <= a for a, b in zip(body, body[1:])):
            out.append(finding("MAP-CART-002", "BLOCK", f"layer '{l.id}': the class breaks are not unique and ascending", e))
        empty = [lab for lab, n in zip(c.labels, c.counts, strict=True) if n == 0]
        if empty and not getattr(c, "fixed", False):          # an atlas page may hold nothing in a class the other pages need
            out.append(finding("MAP-CART-002", "WARN", f"layer '{l.id}': empty class(es) {', '.join(empty)}; use fewer classes or another method", empty))
    return out


def check_003(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        if l.role not in ("choropleth", "hexbin", "grid") or l.cls is None:
            continue
        c = l.spec.get("classification") or {}
        if l.palette_kind == "diverging" and c.get("midpoint") is None:
            out.append(finding("MAP-CART-003", "BLOCK", f"layer '{l.id}': a diverging palette needs a meaningful midpoint (classification.midpoint)", None))
        elif l.palette_kind == "sequential" and l.value_field in l.gdf:
            v = l.gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan)
            v = v[np.isfinite(v)]
            if v.size and v.min() < 0 < v.max() and c.get("midpoint") is None:
                out.append(finding("MAP-CART-003", "INFO",
                                   f"layer '{l.id}': values run below and above zero on a sequential ramp; consider a diverging palette with midpoint 0", None))
    return out


def check_004(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        c = l.cls
        if c is None or c.kind != "categorical":
            continue
        total = sum(c.counts) or 1
        if "Other" in c.categories:
            share = c.counts[c.categories.index("Other")] / total
            out.append(finding("MAP-CART-004", "WARN" if share > 0.25 else "INFO",
                               f"layer '{l.id}': {share:.0%} of features are grouped into 'Other' to stay within {cf.MAX_CATEGORIES} colors", share))
    return out


def check_007(report: dict) -> list[dict]:
    lab = (report or {}).get("labels") or {}
    out = []
    for lid, s in (lab.get("layers") or {}).items():
        tot = s["placed"] + s["dropped_collision"] + s["dropped_size"] + s["dropped_outside"]
        if tot >= 8 and (s["dropped_collision"] + s["dropped_size"]) / tot > 0.35:
            out.append(finding("MAP-CART-007", "WARN", f"labels for '{lid}': {s['dropped_collision'] + s['dropped_size']} of {tot} could not be placed "
                               "without collisions or below the minimum text size; label fewer features or use a larger template", s))
    return out


NAMES_TIER1_DROP_WARN = 0.6     # more than this share of capitals and big cities lost to collisions: the sheet is too small for its names


def check_017(report: dict) -> list[dict]:
    """Place names (M10): INFO when names are on but none fit; WARN when most of the first tier (capitals, cities of a million) were dropped."""
    names = ((report or {}).get("labels") or {}).get("names") or {}
    if not names or not names.get("on"):
        return []
    out = []
    counts = names.get("counts") or {}
    p = names.get("places") or {}
    total_c = (p.get("candidates") or 0) + ((names.get("water") or {}).get("candidates") or 0) + ((names.get("admin") or {}).get("candidates") or 0)
    if total_c and not any(counts.values()):
        out.append(finding("MAP-CART-017", "INFO", f"place names are on but none of the {total_c} candidate(s) fit beside the data and its labels; "
                           "a larger template, fewer data labels or names.max would make room", names))
    t1, t1d = p.get("tier1") or 0, p.get("tier1_dropped") or 0
    if t1 >= 3 and t1d / t1 > NAMES_TIER1_DROP_WARN:
        out.append(finding("MAP-CART-017", "WARN", f"{t1d} of {t1} capitals and major cities could not be named without a collision; "
                           "use a larger sheet, or names.max to keep only the names that matter", {"tier1": t1, "dropped": t1d}))
    return out


def _chroma(h: str) -> float:
    L, a, b = cl.lab(h)
    return (a * a + b * b) ** 0.5


def check_008(plan) -> list[dict]:
    """The basemap must stay quieter than the data: lower chroma and lighter or equal contrast against the ground."""
    out = []
    t = plan.tokens
    data_fills = [c for l in plan.layers if l.role in ("choropleth", "hexbin", "grid", "points", "proportional", "polygons") for c in (l.colors or ([l.fill] if l.fill else []))]
    if not data_fills or not [b for b in plan.basemap if b["kind"] != "tiles"]:
        return out
    base_c = max(_chroma(t["land"]), _chroma(t["water"]))
    top = max(_chroma(c) for c in data_fills if c)
    if base_c > top * 0.9 and base_c > 12:
        out.append(finding("MAP-CART-008", "WARN", "the basemap colors are as saturated as the data; the basemap should stay quieter", {"basemap": round(base_c, 1), "data": round(top, 1)}))
    if cl.contrast(t["land"], t["background"]) > 2.5:
        out.append(finding("MAP-CART-008", "WARN", "the land color contrasts strongly with the page; the basemap may compete with the data", None))
    return out


def check_009(plan, report) -> list[dict]:
    out = []
    cover = (report or {}).get("data_cover")
    ext = (report or {}).get("extent_fill")
    if ext is not None and ext < 0.6:
        out.append(finding("MAP-CART-009", "WARN", f"the data fills only {ext:.0%} of the map frame; zoom the extent or choose a template that fits the shape better", ext))
    elif cover is not None and cover < 0.03:
        out.append(finding("MAP-CART-009", "INFO", "the data covers very little of the frame", cover))
    clipped = (report or {}).get("clipped_features")
    if clipped:
        out.append(finding("MAP-CART-009", "WARN", f"{clipped} feature(s) are cut by the frame; widen the extent if that is not intended", clipped))
    return out


def check_010(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        if l.role != "proportional":
            continue
        scaling = (l.symbol or {}).get("scaling", "area")
        if scaling != "area":
            _gate(out, l, "MAP-CART-010", "BLOCK", f"layer '{l.id}': proportional symbols must be scaled by area, not {scaling}")
    return out


def check_011(plan, report) -> list[dict]:
    out = []
    t = plan.tpl
    map_cm2 = (t["map"]["w"] * t["map"]["h"]) / 100.0
    for l in plan.layers:
        if l.kind != "point" or l.role in ("context", "labels"):
            continue
        n = len(l.gdf)
        if n / map_cm2 > 12 and l.fill_opacity >= 0.999:
            out.append(finding("MAP-CART-011", "WARN", f"layer '{l.id}': {n:,} points on {map_cm2:.0f} cm² will overplot; use transparency, a smaller symbol, or aggregate with role: hexbin (or ops grid/hexbin)", n))
    return out


def check_012(plan, report) -> list[dict]:
    den = ((report or {}).get("scale") or {}).get("scale_denominator")
    gd = (getattr(plan, "crs_info", None) or {}).get("ground_detail") or {}
    if gd.get("drawn"):
        return []                         # 0.11.0: Census 1:500,000 land and TIGER water were drawn for this window
    if getattr(plan, "vector_ground", None):
        return []                         # 1.10.0: the ground is the person's own vector tile file, read at the zoom of this scale
    if den and [b for b in plan.basemap if b["kind"] != "tiles"] and den < 1_000_000:
        hint = ("run ground_detail.py prepare --spec on this MapSpec (detailed land and water: TIGER in the US, Overture elsewhere, fetched once) and build again" if gd
                else "draw boundaries from a detailed data layer, or put a tile basemap under it (basemap-render)")
        prov = ((plan.spec or {}).get("basemap") or {}).get("provider", "none") if hasattr(plan, "spec") else None
        why = ""
        if prov == "none":
            # 1.2.0 (Day 5 issue 4): with no basemap the bundled land, water and state lines still come with the relief or the context pack
            why = (" (basemap provider is none, but the bundled land, water and boundary lines are still drawn under the "
                   + ("relief" if any(b.get("kind") == "hillshade" for b in plan.basemap) else "context pack")
                   + "; context: {on: false} and no relief keep the page bare)")
        return [finding("MAP-CART-012", "WARN", f"the bundled basemap is generalized for scales smaller than about 1:1,000,000; this map is about 1:{den:,.0f}{why}; {hint}", den)]
    return []


def check_024(plan, report=None) -> list[dict]:
    """Rule (0.12.0): land and water come from one source at one resolution, so their edges agree: a 1:500,000 coast beside full-resolution TIGER
    water, or Natural Earth lakes over TIGER land, draws two shorelines a few hundred metres apart. Raised as a BLOCK: the map is not written."""
    gr = (getattr(plan, "crs_info", None) or {}).get("ground_resolution") or {}
    out = []
    if gr and gr.get("land") != gr.get("water"):
        out.append(finding("MAP-CART-024", "BLOCK", f"the land ({gr.get('land')}) and the water ({gr.get('water')}) are at different resolutions; "
                           "draw both from one source (ground_detail.py prepare for TIGER, or basemap ground_detail: false for Natural Earth)", gr))
    ctx_water = [l.id for l in getattr(plan, "layers", []) if l.role == "context" and str(l.id).startswith(("context-lakes", "context-rivers"))]
    if getattr(plan, "vector_ground", None):
        ctx_water = []                                                                # 1.10.0: the river lines come from the same tile file as the ground
    if gr and ctx_water and "Natural Earth 1:10m" not in str(gr.get("water")):       # 1.5.0: OSM simplified land keeps the 10m lakes
        out.append(finding("MAP-CART-024", "BLOCK", f"context water ({', '.join(ctx_water)}) is Natural Earth 1:10m on {gr.get('water')} ground", ctx_water))
    return out


NORTH_TOLERANCE_DEG = 0.25


def check_025(plan, report=None) -> list[dict]:
    """Rule (0.12.0): the locator inset's north points the same way as the main map's, measured as PROJ's meridian convergence at the main map's
    centre in both projections. A BLOCK when they differ by more than a quarter of a degree, or when the inset could not be drawn north-matched
    while the main map has a north (a world-scale or polar map, where north is not one direction, is exempt: MAP-CART-020 hides its arrow)."""
    ins = (report or {}).get("inset") or {}
    n = ins.get("north")
    if not ins or n is None:
        return []
    if ((report or {}).get("north") or {}).get("show") is False:
        return []
    if not n.get("matched"):
        return [finding("MAP-CART-025", "BLOCK", "the locator inset is not drawn with the main map's north (match_north is off or could not be met); "
                        "turn match_north on, or the inset off", n)]
    d = abs(float(n.get("main_convergence") or 0) - float(n.get("inset_convergence") or 0))
    if d > NORTH_TOLERANCE_DEG:
        return [finding("MAP-CART-025", "BLOCK", f"the locator inset's north differs from the main map's by {d:.2f} degrees", n)]
    return []


def check_013(plan) -> list[dict]:
    title = (((plan.spec.get("elements") or {}).get("title")) or {})
    if title.get("on") is False:
        return [finding("MAP-CART-013", "INFO", "the title is off, so the map does not state its message", None)]
    text = " ".join(str(title.get(k) or "") for k in ("text", "subtitle")).lower()
    words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s.lower())}  # noqa: E731
    stop = {"that", "with", "this", "from", "have", "were", "which", "their", "map", "more", "than"}
    if text.strip() and not ((words(text) & words(plan.spec.get("message", ""))) - stop):
        return [finding("MAP-CART-013", "INFO", "the title shares no key words with the message; check that it states what the map shows", None)]
    return []


UI_TOKENS = ("label", "label_halo", "background", "land", "water", "frame", "legend_bg", "legend_frame", "no_data")
DATA_ROLES = ("lines", "points", "polygons", "proportional")      # never "context": the bundled context pack is drawn in page colours on purpose
DISTINCT_DELTA_E = 15.0     # two data layers closer than this (worst colour-vision type) read as the same layer


def _data_color(l) -> tuple[str | None, str | None]:
    """(the color a single-symbol data layer is drawn in, the token name the spec asked for) or (None, None) for classified layers."""
    if l.role not in DATA_ROLES or getattr(l, "cls", None) is not None:
        return None, None
    sym = l.spec.get("symbol") or {}
    raw = sym.get("color") or sym.get("fill")
    color = getattr(l, "stroke", None) if l.kind == "line" else getattr(l, "fill", None)
    return color, (str(raw) if raw else None)


def _drawn_color(l, tokens) -> str | None:
    """The color as the reader sees it: a see-through polygon fill is mixed with the land under it."""
    color, _ = _data_color(l)
    if color and l.kind == "polygon" and float(getattr(l, "fill_opacity", 1.0) or 1.0) < 1:
        return cl.mix(tokens["land"], color, float(l.fill_opacity))
    return color


def check_015(plan) -> list[dict]:
    """A data layer is drawn in a page or text color (label, background, land, frame, no data...), not the brand palette."""
    out = []
    t = plan.tokens
    ui = {t[k].upper(): k for k in UI_TOKENS if isinstance(t.get(k), str)}
    for l in plan.layers:
        color, raw = _data_color(l)
        if not color or (not raw and l.kind == "polygon"):        # an unfilled polygon layer is an outline over the land on purpose
            continue
        name = raw[len("@brand."):] if raw and raw.startswith("@brand.") else None
        hit = name if name in UI_TOKENS else ui.get(color.upper()) if not name else None
        if hit:
            out.append(finding("MAP-CART-015", "WARN", f"layer '{l.id}' is drawn in the brand's {hit} color, which is a page or text color, not a data color; "
                               "use the palette: @brand.categorical.N, @brand.status.*, @brand.accent, or leave the color out and the next free palette color is used", hit))
    return out


def brand_alternative(tokens: dict, against: list[str], floor: float = DISTINCT_DELTA_E) -> tuple[str, str] | None:
    """1.14.0 (Day 13 issue 6): the brand colour (a categorical or status token) that stays most distinct from every colour in `against` under
    normal vision and the three colour-vision types, as (token, hex), when it clears `floor`; else None. The warnings name it, so the fix is
    one line of the MapSpec, not a search through the palette."""
    cands = [(f"@brand.categorical.{i}", c) for i, c in enumerate(tokens.get("categorical") or [])]
    cands += [(f"@brand.status.{k}", c) for k, c in (tokens.get("status") or {}).items()]
    taken = {str(a).upper() for a in against}
    best = None
    for tok, c in cands:
        if str(c).upper() in taken:
            continue
        d = min((cl.cvd_min_delta_e(c, a) for a in against), default=99.0)
        if best is None or d > best[2]:
            best = (tok, c, d)
    return (best[0], best[1]) if best and best[2] >= floor else None


def check_016(plan) -> list[dict]:
    """Two single-symbol data layers share a color or are too close to tell apart. 1.14.0: the message names a brand colour that would not."""
    out = []
    seen: list[tuple[str, str]] = []
    every = [c for c in (_drawn_color(l, plan.tokens) for l in plan.layers) if c]
    for l in plan.layers:
        color = _drawn_color(l, plan.tokens)
        if not color:
            continue
        for other_id, other in seen:
            d = cl.cvd_min_delta_e(color, other)
            if d < DISTINCT_DELTA_E:
                alt = brand_alternative(plan.tokens, [c for c in every if c.upper() != color.upper()])
                hint = (f"give '{l.id}' {alt[0]} ({alt[1]}), which stays distinct from the map's other colours" if alt
                        else "give one of them another palette color")
                out.append(finding("MAP-CART-016", "WARN", f"layers '{other_id}' and '{l.id}' have colors too close to tell apart (delta E {d:.0f} under colour-vision simulation); "
                                   + hint, [other_id, l.id]))
        seen.append((l.id, color))
    return out


def lint_plan(plan, report: dict | None = None, svg: str | None = None) -> list[dict]:
    """`svg` is the rendered map, for the rules that read the output back (MAP-CART-019)."""
    out: list[dict] = []
    for fn in (check_001, check_001_bivariate, check_002, check_003, check_004, check_008, check_010, check_013, check_015, check_016, check_018):
        out += fn(plan)
    for fn in (check_009, check_011, check_012, check_024, check_025):
        out += fn(plan, report)
    out += check_007(report) + check_017(report) + check_019(plan, svg)
    import look_lint                                      # 0.22.0: the visual hierarchy checks (MAP-CART-026 to 032), not under classic
    out += look_lint.lint(plan, report, svg)
    return out


def worst(findings: list[dict]) -> int:
    import gmap_common as gc
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in findings) else gc.EXIT_OK
