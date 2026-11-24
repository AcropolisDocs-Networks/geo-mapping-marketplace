"""Text width in millimetres, measured from the bundled Inter (spec 11.8), so layout can place text before anything renders.

    width_mm("Sites per 100k pop.", 9, weight=600)

Advance widths come from the variable font's `hmtx`, instanced at the weight asked for (fontTools; cached). Kerning is not
applied, so a small safety margin is added. Without fontTools the measure falls back to a conservative average advance, and
`exact()` says so; layout then leaves more room.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

PT_MM = 25.4 / 72.0
FALLBACK_EM = 0.60            # a conservative average advance for Inter, in em
MARGIN = 1.02
LINE_HEIGHT = 1.25


def font_file() -> Path:
    return gc.plugin_root() / "assets" / "fonts" / "inter" / "inter-latin-wght-normal.woff2"


@functools.lru_cache(maxsize=8)
def _metrics(weight: int):
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        f = TTFont(str(font_file()))
        if "fvar" in f:
            # only the advance widths are read: the layout tables (kerning, substitutions) are dropped before instancing, and the outlines
            # too when HVAR carries the width deltas, which gives the same hmtx in a hundredth of the time (M16; instancing GPOS and gvar
            # took about a second per weight)
            drop = ["GPOS", "GSUB", "GDEF"] + (["glyf", "gvar", "loca"] if "HVAR" in f else [])
            for tag in drop:
                if tag in f:
                    del f[tag]
            f = instancer.instantiateVariableFont(f, {"wght": weight}, inplace=False)
        upm = f["head"].unitsPerEm
        cmap = f.getBestCmap()
        hmtx = f["hmtx"]
        widths = {cp: hmtx[g][0] for cp, g in cmap.items()}
        digits = max(widths[ord(c)] for c in "0123456789")
        return upm, widths, digits
    except Exception:  # noqa: BLE001
        return None


def exact() -> bool:
    return _metrics(400) is not None


# ---------------------------------------------------------------------------------------------------------------- 1.4.0: style-pack type
# A style pack (map_style) may set its own faces for three roles: title, label and body. The plan stage makes them the active type
# (use_type); the code that draws a title or a label says so (`with role("title"):`), and both the measure here and svg_draw.text read
# the same role, so the layout measures the face that is drawn. With no active type (the `default` pack, or a pack that keeps the
# brand's fonts) every width is Inter's, exactly as before 1.4.0.
_ACTIVE: dict | None = None
_ROLE: list[str] = ["body"]


def use_type(type_map: dict | None) -> None:
    """Set the active type: {role: {family, file, style, variable}} for the roles a pack sets, or None for the brand's Inter."""
    global _ACTIVE
    _ACTIVE = dict(type_map) if type_map else None


def active_type() -> dict | None:
    return _ACTIVE


class role:
    """`with role("title"):` measures and draws the title role's face (body when the pack sets none for it)."""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        _ROLE.append(self.name)
        return self

    def __exit__(self, *exc):
        _ROLE.pop()
        return False


def current_role() -> str:
    return _ROLE[-1]


def face_for(role_name: str | None = None) -> dict | None:
    """The active face for a role (the body face when the pack sets none for it); None under the brand's Inter."""
    if not _ACTIVE:
        return None
    r = role_name or current_role()
    return _ACTIVE.get(r) or _ACTIVE.get("body")


@functools.lru_cache(maxsize=32)
def _face_metrics(path: str, weight: int):
    """Advance widths of a pack face (a TTF, static or variable). A variable face is instanced at the weight in memory only (the file is
    never changed); a static face keeps its one weight (a bolder weight is synthesized by the renderer, which widens it a little: FAUX_BOLD)."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        f = TTFont(path)
        if "fvar" in f:
            axes = {a.axisTag: a for a in f["fvar"].axes}
            loc = {}
            if "wght" in axes:
                loc["wght"] = max(axes["wght"].minValue, min(axes["wght"].maxValue, weight))
            for tag in ("GPOS", "GSUB", "GDEF") + (("glyf", "gvar", "loca") if "HVAR" in f else ()):
                if tag in f:
                    del f[tag]
            f = instancer.instantiateVariableFont(f, loc, inplace=False) if loc else f
        upm = f["head"].unitsPerEm
        cmap = f.getBestCmap()
        hmtx = f["hmtx"]
        widths = {cp: hmtx[g][0] for cp, g in cmap.items()}
        digits = max(widths.get(ord(c), 0) for c in "0123456789") or upm * 0.56
        static_w = int((f["OS/2"].usWeightClass if "OS/2" in f else 400) or 400)
        return upm, widths, digits, ("fvar" not in TTFont(path, lazy=True)), static_w
    except Exception:  # noqa: BLE001
        return None


FAUX_BOLD = 1.04          # a static face drawn bolder than it is (synthesized emboldening) is measured this much wider


def width_em(text: str, weight: int = 400, tnum: bool = False) -> float:
    face = face_for()
    if face is not None:
        m = _face_metrics(str(face["file"]), int(round(weight / 100.0)) * 100)
        if m is not None:
            upm, widths, digit_w, static, static_w = m
            total = 0
            for ch in text:
                cp = ord(ch)
                total += digit_w if (tnum and ch.isdigit()) else widths.get(cp, widths.get(ord("n"), upm * 0.56))
            k = FAUX_BOLD if (static and weight >= static_w + 200) else 1.0
            return total / upm * k
    m = _metrics(int(round(weight / 100.0)) * 100 if weight not in (400, 500, 600, 700) else weight)
    if m is None:
        return len(text) * FALLBACK_EM
    upm, widths, digit_w = m
    total = 0
    for ch in text:
        cp = ord(ch)
        if tnum and ch.isdigit():
            total += digit_w
        else:
            total += widths.get(cp, widths.get(ord("n"), upm * 0.56))
    return total / upm


def width_mm(text: str, size_pt: float, weight: int = 400, tnum: bool = False) -> float:
    if not str(text).isascii():                      # 1.5.0 (R3-SCRIPT): a label in a script Inter lacks is measured in its own Noto face
        import script_text as st
        if st.needs_font(text):
            k, _missing = st.plan_for(text)
            if k:
                return st.width_mm(text, size_pt * PT_MM, k, weight) * MARGIN
    return width_em(text, weight, tnum) * size_pt * PT_MM * MARGIN


def line_height_mm(size_pt: float) -> float:
    return size_pt * PT_MM * LINE_HEIGHT


def wrap(text: str, max_mm: float, size_pt: float, weight: int = 400) -> list[str]:
    """Greedy word wrap to a width. A single word wider than the line is left whole (layout flags the overflow)."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = word if not cur else cur + " " + word
        if cur and width_mm(trial, size_pt, weight) > max_mm:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def fit(text: str, max_mm: float, size_pt: float, weight: int = 400, ellipsis: str = "…") -> str:
    """Shorten with an ellipsis so the text is at most max_mm wide."""
    if width_mm(text, size_pt, weight) <= max_mm:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if width_mm(text[:mid].rstrip() + ellipsis, size_pt, weight) <= max_mm:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis
