"""ornaments: the ornament options of a style pack (1.4.0, R2-ORN), drawn as vector paths in the resolved tokens, deterministic, no filters.

    elements.frame.style      single (the plain neat line) | double (two neat lines) | bar-border (alternating bars between two lines at
                              graticule intervals, with degree ticks and labels on the outer edge; bars.interval_deg: auto picks 1, 2, 5,
                              10 or 15 degrees by extent)
    elements.title.box        plain | cartouche (an ornamental frame as a 9-slice: corners keep their shape, edges stretch to the title's
                              measured size) | title-block (an engineering drawing block: title, drawing number, date, scale, revision, drawn by)
    elements.north_arrow.style  compass-rose (8 or 16 points, in pack ink; svg_draw.north_arrow)
    elements.scale_bar.style    ladder (map_render.draw_scale_bar)
    elements.legend.frame       plain | ruled | cartouche

A frame other than `single` takes its band from the map region (the map is drawn inside it), so nothing placed in a corner ever sits on it.
"""
from __future__ import annotations

import math

import svg_draw as sd
import textmeasure as tm
from layout import Rect

PT = tm.PT_MM
BORDER_MM = {"single": 0.0, "double": 1.6, "bar-border": 3.2}
BAR_STEPS = (1, 2, 5, 10, 15, 30)
CORNER_MM = 3.6                      # a cartouche corner's size: the same on every box (the 9-slice rule)
CARTOUCHE_PAD_MM = 2.6               # the room a cartouche adds round its contents


def border_mm(style: str | None) -> float:
    return BORDER_MM.get(str(style or "single"), 0.0)


# ---------------------------------------------------------------------------------------------------------------- frame
def _edge_crossings(frame, inner: Rect, edge: str, to_ll, step: float, n: int = 480):
    """Where the edge meets the meridians (top, bottom) or parallels (left, right) every `step` degrees: [(pos_mm, value)]."""
    import numpy as np
    if edge in ("top", "bottom"):
        y = inner.y0 if edge == "top" else inner.y1
        xs = np.linspace(inner.x0, inner.x1, n)
        ys = np.full(n, y)
        along = xs
    else:
        x = inner.x0 if edge == "left" else inner.x1
        ys = np.linspace(inner.y0, inner.y1, n)
        xs = np.full(n, x)
        along = ys
    dx, dy = frame.to_display(xs, ys)
    lon, lat = to_ll(dx, dy)
    v = np.asarray(lon if edge in ("top", "bottom") else lat, dtype="float64")
    k = np.floor(v / step)
    out = []
    for i in range(1, n):
        if not (np.isfinite(v[i]) and np.isfinite(v[i - 1])) or k[i] == k[i - 1]:
            continue
        val = max(k[i], k[i - 1]) * step
        f = (val - v[i - 1]) / (v[i] - v[i - 1]) if v[i] != v[i - 1] else 0.0
        out.append((float(along[i - 1] + f * (along[i] - along[i - 1])), float(val)))
    return out, (float(along[0]), float(along[-1]))


def _deg_label(val: float, axis: str) -> str:
    hemi = ("E" if val > 0 else "W" if val < 0 else "") if axis == "lon" else ("N" if val > 0 else "S" if val < 0 else "")
    a = abs(val)
    s = f"{a:.0f}" if float(a).is_integer() else f"{a:g}"
    return f"{s}°{hemi}"


def bar_interval(frame, inner: Rect, to_ll, asked) -> float:
    if asked not in (None, "auto"):
        return float(asked)
    for step in BAR_STEPS:
        c, _ = _edge_crossings(frame, inner, "top" if inner.w >= inner.h else "left", to_ll, step, 240)
        if len(c) <= 10:
            return float(step)
    return float(BAR_STEPS[-1])


def frame_border(style: str, outer: Rect, inner: Rect, frame, crs: str, t: dict, label_pt: float, bars: dict | None = None,
                 sheet: tuple[float, float] | None = None) -> tuple[str, list[str]]:
    """The band between the map region (`outer`) and the drawn map (`inner`): `double` adds an outer neat line (the inner one is the map's
    own neat line); `bar-border` adds bars between them that alternate at each meridian and parallel, ticks on the outer edge, and degree
    labels on the top, left and right edges (the footer sits under the bottom one)."""
    notes: list[str] = []
    ink = t["frame"] if style == "double" else t.get("ornament") or t["label"]
    out = [f'<g class="gm-frame-border" data-style="{sd.esc(style)}">',
           sd.rect(outer.x0, outer.y0, outer.w, outer.h, "none", ink, 0.5)]
    if style != "bar-border":
        out.append("</g>")
        return "".join(out), notes
    from pyproj import Transformer
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    to_ll = tr.transform
    step = bar_interval(frame, inner, to_ll, (bars or {}).get("interval_deg", "auto"))
    gap = border_mm("bar-border")
    bw = gap * 0.42                                # the bar strip, against the outer line
    light = t.get("ornament_light") or t["background"]
    tick = 1.1
    lbl = []
    for edge in ("top", "bottom", "left", "right"):
        cross, (a0, a1) = _edge_crossings(frame, inner, edge, to_ll, step)
        # the strip runs the full outer edge: stops at the map's crossings, carried out to the outer corners
        stops = [a0 - gap] + [p for p, _ in cross] + [a1 + gap]
        for i in range(len(stops) - 1):
            s0, s1 = stops[i], stops[i + 1]
            fill = ink if i % 2 == 0 else light
            if edge == "top":
                out.append(sd.rect(s0, outer.y0, s1 - s0, bw, fill, ink, 0.12))
            elif edge == "bottom":
                out.append(sd.rect(s0, outer.y1 - bw, s1 - s0, bw, fill, ink, 0.12))
            elif edge == "left":
                out.append(sd.rect(outer.x0, s0, bw, s1 - s0, fill, ink, 0.12))
            else:
                out.append(sd.rect(outer.x1 - bw, s0, bw, s1 - s0, fill, ink, 0.12))
        for p, val in cross:
            s = _deg_label(val, "lon" if edge in ("top", "bottom") else "lat")
            if edge == "top":
                out.append(f'<path d="M{sd.num(p)} {sd.num(outer.y0)}v{sd.num(-tick)}" stroke="{ink}" stroke-width="0.2"/>')
                lbl.append(sd.text(p, outer.y0 - tick - 0.5, s, label_pt, t["label"], anchor="middle", num_cls=True))
            elif edge == "bottom":                   # ticks only: the footer sits under the bottom edge
                out.append(f'<path d="M{sd.num(p)} {sd.num(outer.y1)}v{sd.num(tick)}" stroke="{ink}" stroke-width="0.2"/>')
            elif edge == "left":
                out.append(f'<path d="M{sd.num(outer.x0)} {sd.num(p)}h{sd.num(-tick)}" stroke="{ink}" stroke-width="0.2"/>')
                if outer.x0 - tick - 0.5 - tm.width_mm(s, label_pt, 400, tnum=True) >= 1.0:      # only where the margin holds it
                    lbl.append(sd.text(outer.x0 - tick - 0.5, p + label_pt * PT * 0.32, s, label_pt, t["label"], anchor="end", num_cls=True))
            else:
                out.append(f'<path d="M{sd.num(outer.x1)} {sd.num(p)}h{sd.num(tick)}" stroke="{ink}" stroke-width="0.2"/>')
                if sheet is None or outer.x1 + tick + 0.5 + tm.width_mm(s, label_pt, 400, tnum=True) <= sheet[0] - 1.0:
                    lbl.append(sd.text(outer.x1 + tick + 0.5, p + label_pt * PT * 0.32, s, label_pt, t["label"], anchor="start", num_cls=True))
    out.append(sd.rect(outer.x0 + bw, outer.y0 + bw, outer.w - 2 * bw, outer.h - 2 * bw, "none", ink, 0.18))
    out += lbl
    out.append("</g>")
    notes.append(f"frame: bar border, bars every {step:g} degrees")
    return "".join(out), notes


# ---------------------------------------------------------------------------------------------------------------- cartouche
def _corner(cx: float, cy: float, sx: int, sy: int, s: float) -> str:
    """One cartouche corner: a scroll that turns into the corner and a small square boss, `s` mm on a side; mirrored by (sx, sy)."""
    def p(x, y):
        return f"{sd.num(cx + sx * x * s)} {sd.num(cy + sy * y * s)}"
    return (f"M{p(0, 1)}L{p(0, 0.32)}C{p(0, 0.12)} {p(0.12, 0)} {p(0.32, 0)}L{p(1, 0)}"
            f"M{p(0.22, 1)}L{p(0.22, 0.48)}C{p(0.22, 0.3)} {p(0.3, 0.22)} {p(0.48, 0.22)}L{p(1, 0.22)}"
            f"M{p(0.36, 0.36)}m{sd.num(-0.07 * s)} 0a{sd.num(0.07 * s)} {sd.num(0.07 * s)} 0 1 0 {sd.num(0.14 * s)} 0a{sd.num(0.07 * s)} {sd.num(0.07 * s)} 0 1 0 {sd.num(-0.14 * s)} 0")


def cartouche(x: float, y: float, w: float, h: float, t: dict, *, fill: str | None = None, corner: float = CORNER_MM) -> str:
    """An ornamental box as a 9-slice: four corners of fixed size `corner` (whatever the box's size) joined by two parallel rules that
    stretch along each edge, over a panel in the ground colour."""
    ink = t.get("ornament") or t["label"]
    c = min(corner, w / 3, h / 3)
    out = ['<g class="gm-cartouche">', sd.rect(x, y, w, h, fill or t["background"], "none", 0, 'fill-opacity="0.94"')]
    d = []
    inset = c * 0.22
    for (cx, cy, sx, sy) in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        d.append(_corner(cx, cy, sx, sy, c))
    # the edges between the corners: an outer and an inner rule
    for off, lw in ((0.0, 0.32), (inset, 0.16)):
        d.append(f"M{sd.num(x + c)} {sd.num(y + off)}H{sd.num(x + w - c)}M{sd.num(x + c)} {sd.num(y + h - off)}H{sd.num(x + w - c)}"
                 f"M{sd.num(x + off)} {sd.num(y + c)}V{sd.num(y + h - c)}M{sd.num(x + w - off)} {sd.num(y + c)}V{sd.num(y + h - c)}")
    out.append(f'<path d="{"".join(d)}" fill="none" stroke="{ink}" stroke-width="0.3" stroke-linecap="round" stroke-linejoin="round"/>')
    out.append("</g>")
    return "".join(out)


def corner_size(w: float, h: float, corner: float = CORNER_MM) -> float:
    """The corner a cartouche of this size draws (the 9-slice keeps it unless the box is smaller than three corners)."""
    return min(corner, w / 3, h / 3)


# ---------------------------------------------------------------------------------------------------------------- title block
def title_block_size(title: str, pt: float) -> tuple[float, float]:
    with tm.role("title"):
        w = max(72.0, tm.width_mm(title, pt, 700) + 6.0)
    return w, pt * PT * 1.6 + 2 * 7.2 + 1.0


def title_block(x: float, y: float, w: float, h: float, title: str, pt: float, cells: list[tuple[str, str]], t: dict) -> str:
    """An engineering drawing block: the title across the top, then two rows of three cells (a small caption over its value)."""
    ink = t.get("ornament") or t["label"]
    out = ['<g class="gm-title gm-title-block">', sd.rect(x, y, w, h, t["background"], ink, 0.35, 'fill-opacity="0.96"')]
    th = pt * PT * 1.6
    with tm.role("title"):
        out.append(sd.text(x + 3.0, y + th * 0.72, title, pt, t["label"], weight=700, extra=' id="gm-title-text"'))
    out.append(f'<path d="M{sd.num(x)} {sd.num(y + th)}H{sd.num(x + w)}" stroke="{ink}" stroke-width="0.25"/>')
    cw, rh = w / 3, 7.2
    for i, (cap, val) in enumerate(cells[:6]):
        r, c = divmod(i, 3)
        cx, cy = x + c * cw, y + th + r * rh
        if c:
            out.append(f'<path d="M{sd.num(cx)} {sd.num(cy)}v{sd.num(rh)}" stroke="{ink}" stroke-width="0.18"/>')
        out.append(sd.text(cx + 1.2, cy + 2.4, cap.upper(), 4.2, t["label"], weight=400))
        out.append(sd.text(cx + 1.2, cy + 5.9, tm_fit(val, cw - 2.4, 6.0), 6.0, t["label"], weight=600, num_cls=True))
    out.append(f'<path d="M{sd.num(x)} {sd.num(y + th + rh)}H{sd.num(x + w)}" stroke="{ink}" stroke-width="0.18"/>')
    out.append("</g>")
    return "".join(out)


def tm_fit(s: str, w: float, pt: float) -> str:
    return tm.fit(str(s), w, pt, 600)


# ---------------------------------------------------------------------------------------------------------------- legend frames
def legend_frame(style: str, x: float, y: float, w: float, h: float, t: dict) -> str:
    if style == "ruled":
        ink = t.get("ornament") or t["label"]
        panel = sd.rect(x, y, w, h, t["legend_bg"], "none", 0, 'fill-opacity="0.9"')
        return (f'<g class="gm-legend-frame" data-style="ruled">{panel}'
                f'<path d="M{sd.num(x)} {sd.num(y)}H{sd.num(x + w)}M{sd.num(x)} {sd.num(y + 0.9)}H{sd.num(x + w)}M{sd.num(x)} {sd.num(y + h)}H{sd.num(x + w)}" '
                f'stroke="{ink}" stroke-width="0.25"/></g>')
    if style == "cartouche":
        return cartouche(x, y, w, h, t, fill=t["legend_bg"], corner=3.0)
    return sd.rect(x, y, w, h, t["legend_bg"], t["legend_frame"], 0.2)


# ---------------------------------------------------------------------------------------------------------------- compass rose
def compass_rose(cx: float, cy: float, size: float, rotation: float, ink: str, ground: str, size_pt: float, points: int = 8) -> str:
    """An 8- or 16-point compass rose with a ring, the north point filled, in pack ink; rotated with the map's north (MAP-CART-025)."""
    r = size / 2 - size_pt * PT * 0.6
    cyr = cy + size_pt * PT * 0.5
    k = r * 0.16
    out = [f'<g class="gm-north" data-style="compass-rose" transform="rotate({sd.num(rotation)} {sd.num(cx)} {sd.num(cy)})">']
    out.append(f'<circle cx="{sd.num(cx)}" cy="{sd.num(cyr)}" r="{sd.num(r * 0.72)}" fill="none" stroke="{ink}" stroke-width="0.2"/>')
    out.append(f'<circle cx="{sd.num(cx)}" cy="{sd.num(cyr)}" r="{sd.num(r * 0.66)}" fill="none" stroke="{ink}" stroke-width="0.12"/>')

    def point(ang: float, length: float, width: float, half_fill: bool) -> str:
        a = math.radians(ang)
        tipx, tipy = cx + math.sin(a) * length, cyr - math.cos(a) * length
        lx, ly = cx + math.sin(a - math.pi / 2) * width, cyr - math.cos(a - math.pi / 2) * width
        rx, ry = cx + math.sin(a + math.pi / 2) * width, cyr - math.cos(a + math.pi / 2) * width
        left = f'<path d="M{sd.num(cx)} {sd.num(cyr)}L{sd.num(lx)} {sd.num(ly)}L{sd.num(tipx)} {sd.num(tipy)}Z" fill="{ink}" stroke="{ink}" stroke-width="0.12" stroke-linejoin="round"/>'
        right = (f'<path d="M{sd.num(cx)} {sd.num(cyr)}L{sd.num(rx)} {sd.num(ry)}L{sd.num(tipx)} {sd.num(tipy)}Z" fill="{ink if not half_fill else ground}" '
                 f'stroke="{ink}" stroke-width="0.12" stroke-linejoin="round"/>')
        return left + right
    if points >= 16:
        for i in range(8):
            out.append(point(22.5 + 45 * i, r * 0.5, k * 0.45, True))
    for i in range(4):
        out.append(point(45 + 90 * i, r * 0.68, k * 0.7, True))
    for i in range(4):
        out.append(point(90 * i, r, k, i != 0))
    out.append(sd.text(cx, cy - size / 2 + size_pt * PT * 0.85, "N", size_pt, ink, weight=600, anchor="middle"))
    out.append("</g>")
    return "".join(out)
