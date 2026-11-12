"""Accessibility lint, MAP-A11Y-* (spec 12.4). Reads the finished plan and the render; no network, no writes."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import colorlib as cl  # noqa: E402

MIN_CVD_DELTA_E = {"sequential": 5.0, "diverging": 5.0, "categorical": 10.0,     # CIELAB distance under the worst colour-vision type
                   "bivariate": 8.0}                                               # every cell against every other (any two can be neighbours) (M15)

MIN_GREY_DELTA_L = 6.0      # lightness step between neighbouring classes once colour is removed
MIN_TEXT_CONTRAST = 4.5


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _pairs(colors: list[str]):
    return list(zip(colors, colors[1:]))


def weak_pairs(colors: list[str], kind: str | None) -> list[tuple[str, str, float]]:
    """The class pairs closer than the floor for this palette kind under the worst colour-vision type. Categorical and bivariate classes are
    compared all against all; sequential and diverging ones with their neighbours. The plan uses the same test to decide `patterns: auto`."""
    cols = list(colors or [])
    if kind in ("categorical", "bivariate"):
        pairs = [(a, b) for i, a in enumerate(cols) for b in cols[i + 1:]]
    else:
        pairs = _pairs(cols)
    floor = MIN_CVD_DELTA_E.get(kind, 5.0)
    return [(a, b, round(cl.cvd_min_delta_e(a, b), 1)) for a, b in pairs if cl.cvd_min_delta_e(a, b) < floor]


def svg_patterns(svg: str, layer_id: str) -> dict[int, str]:
    """{class index: pattern name} as the rendered map draws them for one layer: the hatch groups map_render writes over each class
    (`<g class="gm-pattern" data-layer=.. data-class=.. data-pattern=..>`). Read from the output, never from the spec."""
    import re
    out: dict[int, str] = {}
    for m in re.finditer(r'<g class="gm-pattern" data-layer="([^"]*)" data-class="(-?\d+)" data-pattern="([a-z_]+)"', svg or ""):
        if m.group(1) == layer_id:
            out[int(m.group(2))] = m.group(3)
    return out


def check_001(plan, rendered=None) -> list[dict]:
    """MAP-A11Y-001: classes that colour-vision deficiency would merge. A layer whose close classes the rendered map also hatches differently
    (patterns, M15) is told so as INFO: the patterns are the second difference the warning asks for. The patterns are read from the rendered
    SVG when there is one, so a pattern the renderer did not draw does not count."""
    out = []
    for l in plan.layers:
        if not l.colors or l.cls is None or getattr(l, "class_shapes", None):         # shapes carry the difference; MAP-A11Y-003 checks they are all different
            continue
        if getattr(l, "palette_kind", None) == "graduated":       # one colour on purpose: the widths carry the classes (MAP-CART-019 checks them)
            continue
        weak = weak_pairs(l.colors, l.palette_kind)
        if weak:
            worst = min(weak, key=lambda x: x[2])
            if rendered is not None:
                drawn = svg_patterns(rendered.svg, l.id)
                pats = [drawn.get(i, "none") for i in range(len(l.colors))] if drawn else []
            else:
                pats = list((getattr(l, "symbol", None) or {}).get("_patterns") or [])
            idx = {c: i for i, c in enumerate(l.colors)}
            if pats and all(pats[idx[a]] != pats[idx[b]] for a, b, _ in weak):
                out.append(finding("MAP-A11Y-001", "INFO", f"layer '{l.id}': {len(weak)} pair(s) of class colours are close under colour-vision "
                                   f"simulation (closest {worst[0]} and {worst[1]}, difference {worst[2]}); hatch patterns tell them apart", weak[:6]))
                continue
            out.append(finding("MAP-A11Y-001", "WARN",
                               f"layer '{l.id}': {len(weak)} pair(s) of classes are hard to tell apart with colour-vision deficiency "
                               f"(closest {worst[0]} and {worst[1]}, difference {worst[2]}); add direct labels, fewer classes, or patterns "
                               f"(classification.patterns: auto)", weak[:6]))
    return out


def check_002(plan) -> list[dict]:
    t = plan.tokens
    out = []
    halo = t.get("label_halo")
    for name in ("land", "water", "background"):
        ratio = cl.contrast(t["label"], t[name])
        if ratio < MIN_TEXT_CONTRAST and not halo:
            out.append(finding("MAP-A11Y-002", "WARN", f"label color on {name} has contrast {ratio:.1f}:1 (needs {MIN_TEXT_CONTRAST}:1) and no halo is declared", ratio))
    for l in plan.layers:
        if not l.colors or not (plan.spec.get("labels") or []):
            continue
        for c in l.colors:
            best = max(cl.contrast(t["label"], c), cl.contrast(t["legend_bg"], c))
            if best < MIN_TEXT_CONTRAST and not halo:
                out.append(finding("MAP-A11Y-002", "WARN", f"layer '{l.id}': no label color reaches {MIN_TEXT_CONTRAST}:1 on {c}", best))
                break
    return out


def check_003(plan) -> list[dict]:
    """Status colors never carry meaning alone: every status-colored legend entry needs its own shape."""
    status = {cl.norm_hex(v) for v in plan.tokens["status"].values()}
    out = []
    for l in plan.layers:
        blk = l.legend_block or {}
        items = [i for i in blk.get("items", []) if i.get("fill") and cl.norm_hex(i["fill"]) in status]
        if len(items) >= 2:
            shapes = [i.get("shape") for i in items]
            if None in shapes or len(set(shapes)) < len(shapes):
                out.append(finding("MAP-A11Y-003", "BLOCK",
                                   f"layer '{l.id}': status colors are the only difference between legend entries; give each status its own shape or label", shapes))
    return out


def check_004(plan) -> list[dict]:
    out = []
    for l in plan.layers:
        if l.palette_kind not in ("sequential", "diverging") or len(l.colors or []) < 3:
            continue
        ls = [cl.lab(c)[0] for c in l.colors]
        steps = [abs(b - a) for a, b in zip(ls, ls[1:])]
        if l.palette_kind == "sequential" and (min(steps) < MIN_GREY_DELTA_L or not (all(s >= 0 for s in [b - a for a, b in zip(ls, ls[1:])]) or all(s <= 0 for s in [b - a for a, b in zip(ls, ls[1:])]))):
            out.append(finding("MAP-A11Y-004", "INFO", f"layer '{l.id}': neighbouring classes are close in lightness; in grayscale print some will look alike", [round(x, 1) for x in steps]))
    return out


def check_005(plan, rendered) -> list[dict]:
    svg = rendered.svg
    out = []
    if not (rendered.alt_text or "").strip() or len(rendered.alt_text) < 30:
        out.append(finding("MAP-A11Y-005", "WARN", "no usable alt text was generated for the map", rendered.alt_text))
    if 'role="img"' not in svg or "<title" not in svg or "<desc" not in svg:
        out.append(finding("MAP-A11Y-005", "WARN", "the SVG lacks role=\"img\", <title> or <desc>", None))
    return out


# The theme's grounds (0.2.1). The same minimums tools/import_brand.py applies when it derives the tokens, re-checked here because a pack
# may carry hand overrides.
DARK_LAND_MIN = 1.2          # land against the page (dark theme): the map's shapes must read on the page
DARK_WATER_DE_MIN = 10.0     # water against land and page (dark theme), CIE delta E: the locator's land must read on its water
DARK_FRAME_MIN = 1.8         # map frame and locator outline against the page (dark theme)
LOCATOR_BOX_MIN = 3.0        # the locator's box against its land and water (WCAG 1.4.11, non-text contrast)
DATA_ON_LAND_MIN = 3.0       # a single-symbol line or point layer against the land it is drawn on


def check_006(plan) -> list[dict]:
    """MAP-A11Y-006: the map's grounds and the locator are visible in the chosen theme."""
    t, out = plan.tokens, []
    dark = cl.luminance(t["background"]) < 0.2
    box = min(cl.contrast(t["emphasis"], t["land"]), cl.contrast(t["emphasis"], t["water"]))
    if box < LOCATOR_BOX_MIN:
        out.append(finding("MAP-A11Y-006", "WARN", f"the locator box (emphasis) has {box:.1f}:1 contrast on its land or water; it needs {LOCATOR_BOX_MIN}:1", round(box, 2)))
    if dark:
        lb = cl.contrast(t["land"], t["background"])
        if lb < DARK_LAND_MIN:
            out.append(finding("MAP-A11Y-006", "WARN", f"dark theme: land has {lb:.2f}:1 contrast with the page (needs {DARK_LAND_MIN}:1); the map's shapes disappear", round(lb, 2)))
        wd = min(cl.delta_e(t["water"], t["land"]), cl.delta_e(t["water"], t["background"]))
        if wd < DARK_WATER_DE_MIN:
            out.append(finding("MAP-A11Y-006", "WARN", f"dark theme: water is only delta E {wd:.0f} from land or page (needs {DARK_WATER_DE_MIN:.0f}); the locator's land will not show", round(wd, 1)))
        fb = cl.contrast(t["frame"], t["background"])
        if fb < DARK_FRAME_MIN:
            out.append(finding("MAP-A11Y-006", "WARN", f"dark theme: the frame has {fb:.1f}:1 contrast with the page (needs {DARK_FRAME_MIN}:1)", round(fb, 2)))
    return out


def check_007(plan) -> list[dict]:
    """MAP-A11Y-007: a single-symbol line or point layer stands out from the land it is drawn on."""
    out = []
    land = plan.tokens["land"]
    for l in plan.layers:
        if l.role not in ("lines", "points") or getattr(l, "cls", None) is not None:
            continue
        c = l.stroke if l.kind == "line" else l.fill
        if not c:
            continue
        r = cl.contrast(c, land)
        if r < DATA_ON_LAND_MIN:
            out.append(finding("MAP-A11Y-007", "WARN", f"layer '{l.id}' ({c}) has {r:.1f}:1 contrast on the land (needs {DATA_ON_LAND_MIN}:1); pick a stronger palette color", round(r, 2)))
    return out


def check_008(plan, rendered=None) -> list[dict]:
    """MAP-A11Y-008 (M15): a bivariate grid as the rendered key draws it: every cell a distinct colour (no two the same), the low-low cell
    distinguishable from No data and from the land (or a reader takes 'low on both' for 'nothing here'), and a label colour that reads on every
    cell (the label or the page colour at 4.5:1, or a declared halo). Read from the key's cells in the SVG when there is one."""
    import re
    out = []
    t = plan.tokens
    for l in plan.layers:
        if getattr(l, "palette_kind", None) != "bivariate" or not l.colors:
            continue
        cells = list(l.colors)
        if rendered is not None:
            m = re.search(r'<g class="gm-legend-bivariate" data-layer="' + re.escape(l.id) + r'">(.*?)</g>', rendered.svg, re.S)
            drawn = re.findall(r'<rect [^>]*fill="(#[0-9A-Fa-f]{6})"[^>]*data-cell="(\d+),(\d+)"', m.group(1)) if m else []
            if drawn:
                n = int(round(len(drawn) ** 0.5))
                by = {(int(i), int(j)): c for c, i, j in drawn}
                cells = [by.get((i, j), "") for j in range(n) for i in range(n)]
        if len({c.upper() for c in cells}) < len(cells):
            out.append(finding("MAP-A11Y-008", "BLOCK", f"layer '{l.id}': two cells of the bivariate key have the same colour", cells))
        low = cells[0]
        for name in ("no_data", "land"):
            d = cl.cvd_min_delta_e(low, t[name])
            if d < MIN_CVD_DELTA_E["bivariate"]:
                out.append(finding("MAP-A11Y-008", "WARN", f"layer '{l.id}': the low-low cell {low} is only delta E {d:.1f} from the {name.replace('_', ' ')} colour "
                                   f"under colour-vision simulation; readers may take low values for {'no data' if name == 'no_data' else 'empty land'}", round(d, 1)))
        if not t.get("label_halo"):
            bad = [c for c in cells if max(cl.contrast(t["label"], c), cl.contrast(t["background"], c)) < MIN_TEXT_CONTRAST]
            if bad:
                out.append(finding("MAP-A11Y-008", "WARN", f"layer '{l.id}': no label colour reaches {MIN_TEXT_CONTRAST}:1 on bivariate cell(s) {', '.join(bad)} and no halo is declared", bad))
    return out


def lint(plan, rendered) -> list[dict]:
    return check_001(plan, rendered) + check_008(plan, rendered) + check_002(plan) + check_003(plan) + check_004(plan) + check_005(plan, rendered) + check_006(plan) + check_007(plan)
