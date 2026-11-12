"""Small SVG drawing helpers in sheet millimetres: paths from geometries, symbols, text, and escaping.

Colors are always passed in (they come from brand tokens); nothing here invents one.
"""
from __future__ import annotations

import html
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import textmeasure as tm  # noqa: E402

PT = tm.PT_MM


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _ring(coords) -> str:
    c = np.round(np.asarray(coords), 2)
    if len(c) < 2:
        return ""
    pts = c[:-1] if len(c) > 2 and (c[0] == c[-1]).all() else c
    out = [f"M{num(pts[0][0])} {num(pts[0][1])}"]
    prev = pts[0]
    for p in pts[1:]:
        if (p == prev).all():
            continue
        out.append(f"L{num(p[0])} {num(p[1])}")
        prev = p
    return "".join(out) + "Z"


def polygon_path(geom) -> str:
    """Path data for a (Multi)Polygon in sheet mm. Rings of the same feature share one path (even-odd fill)."""
    import shapely
    parts = []
    for g in shapely.get_parts(geom):
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        parts.append(_ring(g.exterior.coords))
        parts.extend(_ring(i.coords) for i in g.interiors)
    return "".join(p for p in parts if p)


def line_path(geom) -> str:
    import shapely
    out = []
    for g in shapely.get_parts(geom):
        if g.geom_type != "LineString" or g.is_empty:
            continue
        c = np.round(np.asarray(g.coords), 2)
        out.append(f"M{num(c[0][0])} {num(c[0][1])}" + "".join(f"L{num(x)} {num(y)}" for x, y in c[1:]))
    return "".join(out)


def symbol_path(shape: str, x: float, y: float, d: float) -> str:
    """Path data for one symbol of diameter/size d centred at (x, y). Compact, so thousands of points stay small."""
    r = d / 2
    if shape == "circle":
        return f"M{num(x - r)} {num(y)}a{num(r)} {num(r)} 0 1 0 {num(d)} 0a{num(r)} {num(r)} 0 1 0 {num(-d)} 0Z"
    if shape == "square":
        return f"M{num(x - r)} {num(y - r)}h{num(d)}v{num(d)}h{num(-d)}Z"
    if shape == "triangle":
        h = d * 0.9
        return f"M{num(x)} {num(y - h * 0.62)}L{num(x + r)} {num(y + h * 0.38)}L{num(x - r)} {num(y + h * 0.38)}Z"
    if shape == "diamond":
        return f"M{num(x)} {num(y - r * 1.2)}L{num(x + r)} {num(y)}L{num(x)} {num(y + r * 1.2)}L{num(x - r)} {num(y)}Z"
    if shape == "pointer":                        # the direction-of-travel symbol, pointing up (legend keys; the map rotates each one)
        return pointer_path(x, y, d, 0.0)
    if shape == "cross":
        t = d * 0.18
        return (f"M{num(x - t)} {num(y - r)}h{num(2 * t)}v{num(r - t)}h{num(r - t)}v{num(2 * t)}h{num(-(r - t))}v{num(r - t)}"
                f"h{num(-2 * t)}v{num(-(r - t))}h{num(-(r - t))}v{num(-2 * t)}h{num(r - t)}Z")
    raise ValueError(f"unknown symbol shape '{shape}'")


def pointer_path(x: float, y: float, d: float, angle_deg: float) -> str:
    """A direction-of-travel symbol (0.11.0): a round body of diameter d with a nose, pointing `angle_deg` clockwise from sheet-up, as ONE outline
    (so the outline stroke does not cross the body). The nose reaches 0.95 d from the centre, so the direction reads at 2 mm; the body keeps the
    dot's footprint, so a map of dots and pointers lines up."""
    r = d / 2
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)

    def pt(px, py):                                   # rotate (px, py), given with up = -y, about the centre
        return x + px * ca - py * sa, y + px * sa + py * ca
    sh = math.radians(48.0)                           # where the nose leaves the body, either side of the forward direction
    lft = pt(-r * math.sin(sh), -r * math.cos(sh))
    tip = pt(0.0, -0.95 * d)
    rgt = pt(r * math.sin(sh), -r * math.cos(sh))
    # left shoulder -> tip -> right shoulder, then the long way round the back of the body to the left shoulder
    return (f"M{num(lft[0])} {num(lft[1])}L{num(tip[0])} {num(tip[1])}L{num(rgt[0])} {num(rgt[1])}"
            f"A{num(r)} {num(r)} 0 1 1 {num(lft[0])} {num(lft[1])}Z")


# ---------------------------------------------------------------------------------------------------------------- vectorised (M16)
# The same path text as polygon_path, line_path, symbol_path and num, for a whole layer at once: shapely 2 hands over every coordinate in one
# array (get_parts, get_rings, get_coordinates with return_index), the numbers are formatted in one pass and the strings joined per feature.
# Each function gives exactly the text its one-feature counterpart gives (tests/test_scale.py compares them), so a map drawn either way is
# the same file.
_S = getattr(np, "strings", None) or np.char          # numpy 2 string ufuncs; numpy 1.26 has the same functions under np.char


def num_array(values) -> np.ndarray:
    """num() for every value of an array: two decimals, trailing zeros and point dropped, never "-0"."""
    v = np.asarray(values, dtype="float64").ravel()
    if not len(v):
        return np.array([], dtype=str)
    s = np.array(list(map("{:.2f}".format, v.tolist())))
    s = _S.rstrip(_S.rstrip(s, "0"), ".")
    return np.where((s == "-0") | (s == ""), "0", s)


def _join_by(tokens: np.ndarray, owner: np.ndarray, n: int) -> list[str]:
    """Concatenate tokens per owner (owner sorted ascending, values in 0..n-1); owners without tokens get ''."""
    out = [""] * n
    if not len(tokens):
        return out
    toks = tokens.tolist()
    starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
    ends = np.r_[starts[1:], len(owner)]
    for o, s, e in zip(owner[starts].tolist(), starts.tolist(), ends.tolist()):
        out[o] = "".join(toks[s:e])
    return out


def polygon_paths(geoms) -> list[str]:
    """polygon_path for each geometry (None or empty gives '')."""
    import shapely
    geoms = np.asarray(geoms, dtype=object)
    n = len(geoms)
    if not n:
        return []
    parts, pidx = shapely.get_parts(geoms, return_index=True)
    keep = (shapely.get_type_id(parts) == 3) & ~shapely.is_empty(parts)          # 3: Polygon
    parts, pidx = parts[keep], pidx[keep]
    rings, ridx = shapely.get_rings(parts, return_index=True)
    rowner = pidx[ridx]                                                         # ring -> geometry
    xy, cidx = shapely.get_coordinates(rings, return_index=True)
    if not len(xy):
        return [""] * n
    c = np.round(xy, 2)
    nring = len(rings)
    counts = np.bincount(cidx, minlength=nring)
    first = np.r_[0, np.cumsum(counts)[:-1]]
    last = first + counts - 1
    keep_c = np.ones(len(c), dtype=bool)
    # _ring: a closed ring (more than two points, last equal to first) loses its last point; rings of fewer than two points draw nothing
    top = len(c) - 1
    closed = (counts > 2) & np.all(c[np.minimum(first, top)] == c[np.clip(last, 0, top)], axis=1)
    keep_c[last[closed]] = False
    keep_c[np.repeat(counts < 2, counts)] = False
    # then a point equal to the one before it is skipped (within the kept points of the ring)
    same = np.zeros(len(c), dtype=bool)
    same[1:] = np.all(c[1:] == c[:-1], axis=1) & (cidx[1:] == cidx[:-1])
    same[first] = False
    keep_c &= ~same
    c, cidx = c[keep_c], cidx[keep_c]
    is_first = np.r_[True, cidx[1:] != cidx[:-1]]
    is_last = np.r_[cidx[1:] != cidx[:-1], True]
    xs, ys = num_array(c[:, 0]), num_array(c[:, 1])
    tok = _S.add(_S.add(_S.add(np.where(is_first, "M", "L"), xs), " "), ys)
    tok = _S.add(tok, np.where(is_last, "Z", ""))
    return _join_by(tok, rowner[cidx], n)


def line_paths(geoms) -> list[str]:
    """line_path for each geometry (None or empty gives '')."""
    import shapely
    geoms = np.asarray(geoms, dtype=object)
    n = len(geoms)
    if not n:
        return []
    parts, pidx = shapely.get_parts(geoms, return_index=True)
    keep = (shapely.get_type_id(parts) == 1) & ~shapely.is_empty(parts)          # 1: LineString
    parts, pidx = parts[keep], pidx[keep]
    xy, cidx = shapely.get_coordinates(parts, return_index=True)
    if not len(xy):
        return [""] * n
    c = np.round(xy, 2)
    is_first = np.r_[True, cidx[1:] != cidx[:-1]]
    xs, ys = num_array(c[:, 0]), num_array(c[:, 1])
    tok = _S.add(_S.add(_S.add(np.where(is_first, "M", "L"), xs), " "), ys)
    return _join_by(tok, pidx[cidx], n)


def symbol_paths(shape: str, xs, ys, d: float) -> list[str]:
    """symbol_path(shape, x, y, d) for every (x, y): the same arithmetic on arrays, the same text."""
    x = np.asarray(xs, dtype="float64")
    y = np.asarray(ys, dtype="float64")
    if not len(x):
        return []
    r = d / 2
    A = _S.add

    def cat(*pieces):
        out = pieces[0]
        for p in pieces[1:]:
            out = A(out, p)
        return out
    if shape == "circle":
        arc = f"a{num(r)} {num(r)} 0 1 0 "
        out = cat("M", num_array(x - r), " ", num_array(y), arc + f"{num(d)} 0" + arc + f"{num(-d)} 0Z")
    elif shape == "square":
        out = cat("M", num_array(x - r), " ", num_array(y - r), f"h{num(d)}v{num(d)}h{num(-d)}Z")
    elif shape == "triangle":
        h = d * 0.9
        y1 = num_array(y + h * 0.38)
        out = cat("M", num_array(x), " ", num_array(y - h * 0.62), "L", num_array(x + r), " ", y1, "L", num_array(x - r), " ", y1, "Z")
    elif shape == "diamond":
        out = cat("M", num_array(x), " ", num_array(y - r * 1.2), "L", num_array(x + r), " ", num_array(y), "L", num_array(x), " ",
                  num_array(y + r * 1.2), "L", num_array(x - r), " ", num_array(y), "Z")
    elif shape == "cross":
        t = d * 0.18
        out = cat("M", num_array(x - t), " ", num_array(y - r),
                  f"h{num(2 * t)}v{num(r - t)}h{num(r - t)}v{num(2 * t)}h{num(-(r - t))}v{num(r - t)}"
                  f"h{num(-2 * t)}v{num(-(r - t))}h{num(-(r - t))}v{num(-2 * t)}h{num(r - t)}Z")
    else:
        raise ValueError(f"unknown symbol shape '{shape}'")
    return out.tolist()


def dot_path(xs, ys) -> str:
    """Round point symbols as zero-length segments ("M x yh0"): drawn with a round line cap of the symbol's diameter, each is a filled circle in
    SVG, in PDF and on screen, at two path commands a point instead of two arcs (the PDF writer turns each arc into curves, in Python)."""
    x = np.asarray(xs, dtype="float64")
    if not len(x):
        return ""
    return "".join(_S.add(_S.add(_S.add(_S.add("M", num_array(x)), " "), num_array(ys)), "h0").tolist())


def text(x: float, y: float, s: str, size_pt: float, fill: str, *, weight: int = 400, anchor: str = "start", num_cls: bool = False,
         halo: str | None = None, halo_w: float = 0.0, rotate: float | None = None, slant: float = 0.0, extra: str = "", tracking: float = 0.0) -> str:
    """A text element. With a halo it is drawn twice (stroke underneath, fill on top) because paint-order is not honored everywhere.
    `tracking` is extra letter-spacing in mm (uppercase admin names are set loose); it goes on both passes so the halo stays under the letters."""
    size = size_pt * PT
    if not str(s).isascii():                          # 1.5.0 (R3-SCRIPT): outlined in the script's Noto face, the words in <title>
        import script_text as st
        if st.needs_font(s):
            k, _missing = st.plan_for(s)
            if k:
                tr = (f"rotate({num(rotate)} {num(x)} {num(y)})" if rotate else
                      f"translate({num(x)} {num(y)}) skewX({num(-slant)}) translate({num(-x)} {num(-y)})" if slant else "")
                return st.svg_label(s, x, y, size, k, fill, anchor=anchor, halo=halo, halo_w=halo_w, extra=extra, transform=tr, weight=weight,
                                    tracking=tracking)
    cls = "gm-t" + (" gm-num" if num_cls else "")
    if tm.active_type() and tm.current_role() != "body" and tm.current_role() in tm.active_type():
        cls += f" gm-r-{tm.current_role()}"               # 1.4.0: a style pack's title or label face (map_style.font_css)
    attrs = f'class="{cls}" x="{num(x)}" y="{num(y)}" font-size="{num(size)}" font-weight="{weight}" text-anchor="{anchor}"'
    if tracking:
        attrs += f' letter-spacing="{num(tracking)}"'
    if rotate:
        attrs += f' transform="rotate({num(rotate)} {num(x)} {num(y)})"'
    elif slant:                                            # Inter ships no italic face: a true slant about the baseline point
        attrs += f' transform="translate({num(x)} {num(y)}) skewX({num(-slant)}) translate({num(-x)} {num(-y)})"'
    body = esc(s)
    out = ""
    if halo and halo_w > 0:
        out += f'<text {attrs} fill="{halo}" stroke="{halo}" stroke-width="{num(halo_w)}" stroke-linejoin="round">{body}</text>'
    out += f'<text {attrs} fill="{fill}"{extra}>{body}</text>'
    return out


def sheet_open(w_mm: float, h_mm: float, label: str) -> str:
    """1.0.0: the root element of a sheet made of other sheets (the atlas's contact sheet), in millimetres; the caller closes it with </svg>."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {num(w_mm)} {num(h_mm)}" width="{num(w_mm)}mm" height="{num(h_mm)}mm" lang="en" '
            f'role="img" aria-label="{esc(label)}">')


def rect(x, y, w, h, fill="none", stroke="none", sw=0.0, extra="") -> str:
    s = f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" fill="{fill}"'
    if stroke != "none":
        s += f' stroke="{stroke}" stroke-width="{num(sw)}"'
    return s + (" " + extra if extra else "") + "/>"


NORTH_STYLES = ("needle", "arrow", "triangle", "compass", "compass-rose")    # 0.15.0: elements.north_arrow.style (1.4.0: compass-rose, ornaments.py)


def north_arrow(cx: float, cy: float, size: float, rotation: float, fill: str, bg: str, text_color: str, size_pt: float, style: str = "needle") -> str:
    """A north arrow, drawn upright and rotated about its center. `needle` (the default): a half-filled needle over an N. `arrow`: a plain
    arrow with an open head and the N above. `triangle`: a filled triangle with the N inside its base line. `compass`: a four-point star,
    north half filled, with the N above."""
    h = size
    top, bot = cy - h / 2 + size_pt * PT * 1.1, cy + h / 2
    n_text = text(cx, cy - h / 2 + size_pt * PT * 0.85, "N", size_pt, text_color, weight=600, anchor="middle")
    if style == "arrow":
        hw = size * 0.22
        body = (f'<path d="M{num(cx)} {num(bot)}V{num(top + 0.4)}" stroke="{fill}" stroke-width="{num(max(0.35, size * 0.04))}" fill="none" stroke-linecap="round"/>'
                f'<path d="M{num(cx - hw)} {num(top + hw * 1.3)}L{num(cx)} {num(top)}L{num(cx + hw)} {num(top + hw * 1.3)}" stroke="{fill}" '
                f'stroke-width="{num(max(0.35, size * 0.04))}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    elif style == "triangle":
        w = size * 0.5
        body = f'<path d="M{num(cx)} {num(top)}L{num(cx - w / 2)} {num(bot)}L{num(cx + w / 2)} {num(bot)}Z" fill="{fill}"/>'
    elif style == "compass":
        r = (bot - top) / 2
        mid = top + r
        k = r * 0.28
        pts_n = f"M{num(cx)} {num(top)}L{num(cx + k)} {num(mid - k)}L{num(cx)} {num(mid)}L{num(cx - k)} {num(mid - k)}Z"
        rest = (f"M{num(cx)} {num(mid)}L{num(cx + k)} {num(mid - k)}L{num(cx + r)} {num(mid)}L{num(cx + k)} {num(mid + k)}L{num(cx)} {num(mid + r)}"
                f"L{num(cx - k)} {num(mid + k)}L{num(cx - r)} {num(mid)}L{num(cx - k)} {num(mid - k)}Z")
        body = (f'<path d="{rest}" fill="{bg}" stroke="{fill}" stroke-width="0.2" stroke-linejoin="round"/>'
                f'<path d="{pts_n}" fill="{fill}" stroke="{fill}" stroke-width="0.2" stroke-linejoin="round"/>')
    else:
        w = size * 0.34
        needle_l = f"M{num(cx)} {num(top)}L{num(cx - w / 2)} {num(bot)}L{num(cx)} {num(bot - h * 0.22)}Z"
        needle_r = f"M{num(cx)} {num(top)}L{num(cx + w / 2)} {num(bot)}L{num(cx)} {num(bot - h * 0.22)}Z"
        body = f'<path d="{needle_l}" fill="{fill}"/><path d="{needle_r}" fill="{bg}" stroke="{fill}" stroke-width="0.2" stroke-linejoin="round"/>'
    return f'<g class="gm-north" data-style="{esc(style)}" transform="rotate({num(rotation)} {num(cx)} {num(cy)})">' + body + n_text + "</g>"


def circle_for_area(area_mm2: float) -> float:
    return math.sqrt(max(area_mm2, 0) / math.pi)
