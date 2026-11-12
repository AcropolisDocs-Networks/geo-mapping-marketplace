"""symbology: the classed-layer extras of the remaining legend types (gap P1-11, M15), applied after a layer's classes and colours are set.

    graduated line widths   a lines layer classed by a value (`classification:` on `value_field`) with `symbol: {graduated: true}` is drawn in
                            one colour, each class a wider line; `symbol.widths_mm: [0.3, 0.7, 1.2, 1.8]` sets the widths (one per class,
                            increasing). The legend shows a line sample of each width. MAP-CART-019 reads the widths back from the SVG.
    patterns                `classification.patterns: auto | true | false | [names]` on a classed area layer: hatch fills over the class colours
                            (patterns.py). `auto` hatches only when MAP-A11Y-001 would find two classes too close.
    line swatches           a classed lines layer's legend shows line samples in the class colours, not boxes (`legend.swatch: rect` keeps boxes).
    nested circles          `legend: {nested: true}` on a proportional layer draws the size key as nested circles with leader lines.

Called once per layer from map_plan.style_layer; changes the layer plan in place and never reads data the plan does not already hold.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))

GRAD_MIN_MM, GRAD_MAX_MM, GRAD_STEP_MM = 0.3, 1.8, 0.45      # default graduated widths: from 0.3 mm in steps of 0.45 mm, at most 1.8 mm
GRAD_MIN_STEP_MM = 0.25                                      # MAP-CART-019: neighbouring widths closer than this read as one
GRAD_THINNEST_MM = 0.2


def graduated_widths(k: int, given=None, look: str = "classic") -> list[float]:
    """k increasing widths in mm: symbol.widths_mm as given, else the look's range (map_look: classic 0.3 to 1.8 mm, auto 0.5 to 2.2 mm)."""
    if given:
        w = [float(x) for x in given]
        if len(w) != k:
            raise ValueError(f"symbol.widths_mm has {len(w)} widths but there are {k} classes")
        if any(b <= a for a, b in zip(w, w[1:])):
            raise ValueError("symbol.widths_mm must increase from the lowest class to the highest")
        return w
    import map_look
    lo_, hi_, st_ = map_look.params(look)["grad"]
    step = min(st_, (hi_ - lo_) / max(k - 1, 1))
    return [round(lo_ + i * step, 3) for i in range(k)]


CLASS_DASHES = ("", "1.6 0.8", "0.3 0.7", "2.4 0.7 0.3 0.7", "3.2 1.4", "0.8 0.8")     # 1.4.0: solid, dashed, dotted, dash-dot, long, short


def after_classes(plan, l) -> None:
    import map_plan as mp
    t, ly = plan.tokens, l.spec
    sym = l.symbol or {}
    lgc = ly.get("legend") or {}
    # ---- graduated line widths
    if l.kind == "line" and l.cls is not None and getattr(l.cls, "kind", "") == "numeric" and sym.get("graduated"):
        try:
            widths = graduated_widths(l.cls.k, sym.get("widths_mm"), getattr(plan, "look", "classic"))
        except ValueError as ex:
            raise mp.PlanError(f"layer '{l.id}': {ex}", 2)
        col = sym.get("_color") or t["emphasis"]
        l.colors = [col] * l.cls.k
        l.palette_kind = "graduated"
        sym["_widths"] = widths
        items = [{"label": lab, "stroke": col, "width_mm": w, "count": n} for lab, w, n in zip(l.cls.labels, widths, l.cls.counts)]
        if l.cls.nodata:
            items.append({"label": "No data", "stroke": t["no_data"], "width_mm": widths[0], "nodata": True, "count": l.cls.nodata})
        l.legend_block = {"layer": l.id, "title": l.title, "kind": "lines", "items": items, "graduated": True, "note": lgc.get("note")}
        l.notes.append(f"graduated line widths {', '.join(f'{w:g}' for w in widths)} mm for the {l.cls.k} classes of {l.value_field}, in one colour")
        plan.palette_reports.pop(l.id, None)
    # ---- line swatches for any other classed lines layer
    elif l.kind == "line" and l.legend_block and l.legend_block.get("kind") in ("classes", "categories") and lgc.get("swatch") != "rect":
        w = max(0.5, float(l.stroke_w_mm or 0.5))
        l.legend_block = {**l.legend_block, "kind": "lines", "classed": l.legend_block["kind"],
                          "items": [{"label": it["label"], "stroke": it["fill"], "width_mm": w, **({"nodata": True} if it.get("nodata") else {})}
                                    for it in l.legend_block["items"]]}
    # ---- 1.4.0 (R2-TREAT): a pack that tells categories apart by pattern as well as tint (blueprint, monochrome)
    c = ly.get("classification") or {}
    if (getattr(plan, "treatments", None) or {}).get("category_patterns") and l.cls is not None and l.role != "context":
        if l.kind == "line" and l.legend_block and l.legend_block.get("kind") == "lines" and not sym.get("_widths"):
            dashes = [CLASS_DASHES[i % len(CLASS_DASHES)] for i in range(l.cls.k)]
            sym["_class_dash"] = dashes
            its = [({**it, "dash": dashes[i]} if (i < len(dashes) and dashes[i] and not it.get("nodata")) else it)
                   for i, it in enumerate(l.legend_block["items"])]
            l.legend_block = {**l.legend_block, "items": its}
            l.notes.append("classes told apart by dash pattern as well as colour (the style pack's category patterns)")
        elif l.kind == "polygon" and c.get("patterns") is None:
            c = {**c, "patterns": "on"}
    # ---- patterns on a classed area layer (a bivariate layer sets its own in bivariate.style)
    if l.kind == "polygon" and l.cls is not None and l.palette_kind != "bivariate" and c.get("patterns") not in (None, False):
        import a11y_lint
        import patterns as pt
        try:
            pats, note = pt.resolve(c.get("patterns"), l.cls.k, a11y_lint.weak_pairs(l.colors, l.palette_kind))
        except pt.PatternError as ex:
            raise mp.PlanError(f"layer '{l.id}': classification.patterns: {ex}", 2)
        if pats:
            sym["_patterns"] = pats
            l.notes.append(note)
            if l.legend_block and l.legend_block.get("kind") in ("classes", "categories"):
                its = list(l.legend_block["items"])
                for i, it in enumerate(its):
                    if not it.get("nodata") and i < len(pats):
                        its[i] = {**it, "pattern": pats[i]}
                l.legend_block = {**l.legend_block, "items": its}
    l.symbol = sym
