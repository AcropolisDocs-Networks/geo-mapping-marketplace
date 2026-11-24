"""patterns: hatch fills for classed areas, drawn as SVG patterns in brand tokens (gap P1-11, M15).

A pattern is drawn OVER a class's colour fill, so the colour still says what it says and the hatch adds a second difference that survives
colour-vision deficiency and greyscale print. That makes patterns the accessibility fallback MAP-A11Y-001 asks for: when two class colours
are too close under the CVD simulation, `classification.patterns: auto` (or `bivariate.patterns: auto`, the bivariate default) gives the
classes patterns and the finding becomes an INFO saying the patterns carry the difference.

    none         no hatch
    hatch        thin lines at 45 degrees, 1.6 mm apart
    hatch_dense  the same, 0.9 mm apart
    back_hatch   thin lines at 135 degrees
    cross        both diagonals
    horizontal   horizontal lines
    vertical     vertical lines
    dots         a dot grid

The line colour is the brand's label colour on a light fill and its background colour on a dark one (whichever reads better, the same rule
labels use), at partial opacity: no colour that is not a token is ever written.
"""
from __future__ import annotations

import re

PATTERNS = ("none", "hatch", "hatch_dense", "back_hatch", "cross", "horizontal", "vertical", "dots")
AUTO_ORDER = ("none", "hatch", "cross", "back_hatch", "dots", "horizontal", "vertical", "hatch_dense")   # for k classes, the first k
BIVARIATE_ORDER = {2: ("none", "hatch"), 3: ("none", "hatch", "cross")}                                    # one per y class (rows)
LINE_W_MM = 0.18
OPACITY = 0.55


class PatternError(ValueError):
    pass


def check(name: str) -> str:
    if name not in PATTERNS:
        raise PatternError(f"unknown pattern '{name}' (one of {', '.join(PATTERNS)})")
    return name


def auto(k: int) -> list[str]:
    if k > len(AUTO_ORDER):
        raise PatternError(f"{k} classes but only {len(AUTO_ORDER)} patterns; use fewer classes")
    return list(AUTO_ORDER[:k])


def pattern_id(layer_id: str, name: str, ink: str) -> str:
    return f"gm-pat-{re.sub(r'[^A-Za-z0-9_-]', '-', str(layer_id))}-{name}-{ink.lstrip('#').lower()}"


def ink_for(fill: str, tokens: dict) -> str:
    """The token the hatch is drawn in on this fill: the label colour or the page background, whichever contrasts more."""
    import colorlib as cl
    a, b = tokens["label"], tokens["background"]
    return a if cl.contrast(a, fill) >= cl.contrast(b, fill) else b


def defs(pid: str, name: str, ink: str) -> str:
    """One <pattern> in sheet millimetres (userSpaceOnUse, so the spacing is the same on the map and in the legend)."""
    sw = f'stroke="{ink}" stroke-width="{LINE_W_MM}" stroke-opacity="{OPACITY}" fill="none"'
    if name == "none":
        return ""
    if name in ("hatch", "hatch_dense", "back_hatch", "horizontal", "vertical"):
        s = 0.9 if name == "hatch_dense" else 1.6
        rot = {"hatch": 45, "hatch_dense": 45, "back_hatch": -45, "horizontal": 0, "vertical": 90}[name]
        return (f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{s}" height="{s}" patternTransform="rotate({rot})">'
                f'<path d="M0 {s / 2:g}H{s:g}" {sw}/></pattern>')
    if name == "cross":
        s = 1.6
        return (f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{s}" height="{s}" patternTransform="rotate(45)">'
                f'<path d="M0 {s / 2:g}H{s:g}M{s / 2:g} 0V{s:g}" {sw}/></pattern>')
    if name == "dots":
        s = 1.4
        return (f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{s}" height="{s}">'
                f'<circle cx="{s / 2:g}" cy="{s / 2:g}" r="0.24" fill="{ink}" fill-opacity="{OPACITY + 0.15:g}"/></pattern>')
    raise PatternError(f"unknown pattern '{name}'")


def resolve(cfg, k: int, weak: list, order=None) -> tuple[list[str] | None, str | None]:
    """The patterns a classed layer gets. `cfg` is the spec's `patterns:` (False/None: none; True: the automatic order; `auto`: the automatic
    order only when `weak`, the class pairs MAP-A11Y-001 finds too close under the CVD simulation, is not empty; a list: one name per class).
    `order` replaces the automatic order (a bivariate layer hatches by row). -> (patterns or None, note)."""
    if cfg in (None, False, "none", "off"):
        return None, None
    if isinstance(cfg, (list, tuple)):
        names = [check(str(x)) for x in cfg]
        if len(names) != k:
            raise PatternError(f"patterns lists {len(names)} names but there are {k} classes")
        return names, "hatch patterns as given in the spec"
    chosen = list(order) if order is not None else auto(k)
    if cfg is True or cfg == "on":
        return chosen, "hatch patterns on the classes (patterns: true)"
    if cfg == "auto":
        if weak:
            a, b, d = min(weak, key=lambda w: w[2])
            return chosen, (f"{len(weak)} pair(s) of class colours are hard to tell apart under colour-vision simulation (closest {a} and {b}, "
                            f"delta E {d:.1f}); hatch patterns were added so the classes differ by more than colour")
        return None, None
    raise PatternError(f"patterns is true, false, auto or a list of names, not {cfg!r}")
