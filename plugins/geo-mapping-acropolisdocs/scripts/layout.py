#!/usr/bin/env python3
"""map-layout: the sheet. Templates, the map frame transform, and placement of overlay elements (spec 7.3, 10, 12.1 MAP-SPEC-005).

The frame maps display-CRS coordinates to sheet millimetres (y flipped) with the extent centred and fitted. Overlay elements
(legend, inset, north arrow, scale bar) sit in the frame's corners. Each prefers the corner the spec names, or its default; if
that corner is taken or would cover much of the data, the next corner is tried, and the move is reported.

Templates (M16 export): the five landscape sheets, their portrait variants (letter-portrait, tabloid-portrait, arch-d-portrait, story for the 16:9
slide, square stays square), ISO A4 and A3 in both orientations, and `template: {custom: {w_mm, h_mm}}` (100 to 1,500 mm a side, at most 3:1),
scaled from the nearest built-in template. A template with `footer_fit: true` (square-social, the portrait and A-series sheets, every custom
sheet) fits its footer to its notes at render time and gives the map the room that frees (`fit_footer`).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
from label_engine import Rect  # noqa: E402

CORNERS = ("lower-left", "upper-right", "lower-right", "upper-left")
CORNER_INSET_MM = 3.0
DATA_COVER_LIMIT = 0.30           # an element may cover up to this share of the data before it is moved
GRID = (72, 48)


class LayoutError(ValueError):
    pass


def template_names() -> list[str]:
    d = gc.plugin_root() / "templates"
    return sorted(p.stem for p in d.glob("*.yaml"))


# ---------------------------------------------------------------------------------------------------------------- custom sizes (M16 export)
# `template: {custom: {w_mm, h_mm}}` (or `--template custom:500x700`): a sheet of any size between these bounds. Below 100 mm a side the
# title, legend, inset and scale bar at the smallest legal type sizes leave no room for the map; above 1,500 mm (a 60-inch plotter roll) the
# PNG at even 72 dpi is past the rasterizer's ceiling. A sheet longer than three times its width has no corner room for two overlays.
CUSTOM_MIN_MM, CUSTOM_MAX_MM, CUSTOM_MAX_ASPECT = 100.0, 1500.0, 3.0
CUSTOM_MAX_PIXELS = 100_000_000          # the default PNG dpi of a custom sheet is lowered until the picture is at most this many pixels
MIN_FONT_PT = {"print": 6.0, "slide": 10.0, "social": 10.0}


def custom_size(value) -> tuple[float, float] | None:
    """(w_mm, h_mm) when `value` asks for a custom sheet: {custom: {w_mm, h_mm}} or the string 'custom:WxH' (millimetres); else None."""
    if isinstance(value, dict):
        c = value.get("custom")
        if not isinstance(c, dict) or "w_mm" not in c or "h_mm" not in c:
            raise LayoutError("a custom template is written {custom: {w_mm: N, h_mm: N}}")
        try:
            return float(c["w_mm"]), float(c["h_mm"])
        except (TypeError, ValueError):
            raise LayoutError("custom template: w_mm and h_mm must be numbers (millimetres)") from None
    if isinstance(value, str) and value.lower().startswith("custom:"):
        import re
        m = re.fullmatch(r"custom:\s*([0-9]+(?:\.[0-9]+)?)\s*[x×]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:mm)?", value.strip(), re.I)
        if not m:
            raise LayoutError(f"'{value}': a custom size is written custom:WIDTHxHEIGHT in millimetres, for example custom:500x700")
        return float(m.group(1)), float(m.group(2))
    return None


def _mm_text(v: float) -> str:
    return (f"{v:.1f}".rstrip("0").rstrip(".")).replace(".", "p")


def template_label(value) -> str:
    """The name a template goes by in file names and reports: the template's own name, or custom-<w>x<h>mm (a decimal point written p)."""
    cs = custom_size(value)
    if cs is None:
        return str(value)
    return f"custom-{_mm_text(cs[0])}x{_mm_text(cs[1])}mm"


def custom_problems(w: float, h: float) -> list[str]:
    out = []
    for name, v in (("w_mm", w), ("h_mm", h)):
        if not (CUSTOM_MIN_MM <= v <= CUSTOM_MAX_MM):
            out.append(f"custom template: {name} {v:g} is outside {CUSTOM_MIN_MM:g} to {CUSTOM_MAX_MM:g} mm")
    if w > 0 and h > 0 and max(w / h, h / w) > CUSTOM_MAX_ASPECT:
        out.append(f"custom template: {w:g} x {h:g} mm is longer than {CUSTOM_MAX_ASPECT:g} times its width; the overlays have no corner room")
    return out


def nearest_template(w: float, h: float) -> str:
    """The built-in print template closest in shape (first) and size: the distance is |log aspect| weighted 4 to 1 against |log area|.
    A custom sheet is a print sheet (its type follows the print minimum, 6 pt); the slide, story and square sizes are screen pixels."""
    import math
    best, bd = None, None
    for n in template_names():
        t = gc.load_yaml(gc.plugin_root() / "templates" / f"{n}.yaml")
        if t.get("kind", "print") != "print":
            continue
        W, H = (float(v) for v in t["size_mm"])
        d = 4.0 * abs(math.log((w / h) / (W / H))) + abs(math.log((w * h) / (W * H)))
        if bd is None or d < bd:
            best, bd = n, d
    return best


def custom_template(w: float, h: float) -> dict:
    """A template for a w x h mm sheet, scaled from the nearest built-in one. Margins, the title band, the footer band, the legend's and the
    inset's room, the logo and the type sizes scale by k = sqrt(w h / W H) (type never below the kind's legal minimum); the map region takes
    the rest of the height, so the bands keep their proportions whatever the aspect."""
    import math
    probs = custom_problems(w, h)
    if probs:
        raise LayoutError("; ".join(probs) + " (MAP-SPEC-005)")
    base = nearest_template(w, h)
    t = gc.load_yaml(gc.plugin_root() / "templates" / f"{base}.yaml")
    W, H = (float(v) for v in t["size_mm"])
    k = math.sqrt((w * h) / (W * H))
    r1 = lambda v: round(float(v), 1)                               # noqa: E731
    m = r1(float(t.get("margin_mm", 10)) * k)
    ti, mp_, ft = t["title"], t["map"], t["footer"]
    mf = t.get("map_full") or {**mp_, "y": ti["y"], "h": mp_["y"] + mp_["h"] - ti["y"]}
    gap = (float(ft["y"]) - (float(mp_["y"]) + float(mp_["h"]))) * k
    foot_h = r1(float(ft["h"]) * k)
    foot_y = r1(h - (H - float(ft["y"])) * k)
    title = {"x": m, "y": r1(float(ti["y"]) * k), "w": r1(w - 2 * m), "h": r1(float(ti["h"]) * k)}
    map_y = r1(float(mp_["y"]) * k)
    map_bottom = foot_y - gap
    kind = t.get("kind", "print")
    floor = MIN_FONT_PT.get(kind, 6.0)
    fonts = {key: max(floor, round(float(v) * k * 2) / 2) for key, v in t["fonts"].items()}
    dpi = float(t.get("raster_default_dpi", 300))
    while dpi > 72 and (w / 25.4 * dpi) * (h / 25.4 * dpi) > CUSTOM_MAX_PIXELS:
        dpi -= 10
    return {"name": template_label({"custom": {"w_mm": w, "h_mm": h}}), "label": f"Custom sheet ({w:g} x {h:g} mm, scaled from {base})",
            "kind": kind, "size_mm": [w, h], "margin_mm": m, "custom_from": base, "custom_scale": round(k, 4),
            "title": title, "map": {"x": m, "y": map_y, "w": r1(w - 2 * m), "h": r1(map_bottom - map_y)},
            "map_full": {"x": m, "y": r1(float(mf["y"]) * k), "w": r1(w - 2 * m), "h": r1(map_bottom - float(mf["y"]) * k)},
            "footer": {"x": m, "y": foot_y, "w": r1(w - 2 * m), "h": foot_h}, "footer_fit": True, "fonts": fonts,
            # 0.24.0 (TODO M16): the legend's room follows the sheet's own width and height (within half to twice the area scale), so a long
            # sheet (3:1) gives the legend width rather than height it cannot use
            "legend_max": {"w": r1(float(t["legend_max"]["w"]) * min(max(w / W, k / 2), k * 2)),
                           "h": r1(float(t["legend_max"]["h"]) * min(max(h / H, k / 2), k * 2))},
            "inset_size": {"w": r1(float((t.get("inset_size") or {}).get("w", 52)) * k), "h": r1(float((t.get("inset_size") or {}).get("h", 36)) * k)},
            "logo_height_mm": r1(float(t.get("logo_height_mm", 10)) * k), "raster_default_dpi": int(max(72, dpi))}


def load_template(name) -> dict:
    """A template by name (templates/<name>.yaml), or a custom sheet ({custom: {w_mm, h_mm}} or 'custom:WxH'), checked (MAP-SPEC-005)."""
    cs = custom_size(name)
    if cs is not None:
        return check_template(custom_template(*cs), template_label(name))
    p = gc.plugin_root() / "templates" / f"{name}.yaml"
    if not p.is_file():
        raise LayoutError(f"template '{name}' does not exist; choose one of {', '.join(template_names())}, or custom:WIDTHxHEIGHT in mm")
    return check_template(gc.load_yaml(p), name)


def check_template(t: dict, name: str) -> dict:
    for key in ("size_mm", "title", "map", "footer", "fonts", "legend_max"):
        if key not in t:
            raise LayoutError(f"template '{name}' lacks '{key}'")
    W, H = t["size_mm"]
    for region in ("title", "map", "footer"):
        r = t[region]
        if r["x"] < 0 or r["y"] < 0 or r["x"] + r["w"] > W + 1e-6 or r["y"] + r["h"] > H + 1e-6:
            raise LayoutError(f"template '{name}': region '{region}' lies outside the sheet (MAP-SPEC-005)")
    tm_, mp, ft = t["title"], t["map"], t["footer"]
    if tm_["y"] + tm_["h"] > mp["y"] + 1e-6 or mp["y"] + mp["h"] > ft["y"] + 1e-6:
        raise LayoutError(f"template '{name}': title, map and footer regions overlap (MAP-SPEC-005)")
    mf = t.get("map_full")
    if mf is not None:
        if mf["x"] < 0 or mf["y"] < 0 or mf["x"] + mf["w"] > W + 1e-6 or mf["y"] + mf["h"] > ft["y"] + 1e-6:
            raise LayoutError(f"template '{name}': region 'map_full' lies outside the sheet or over the footer (MAP-SPEC-005)")
    return t


FOOTER_FIT_PAD_MM = 1.0          # room kept under the last footer line when the footer is fitted to its notes
MAX_FOOTER_GROWTH = 0.18         # the map region grows by at most this share of its height: the plan's raster and context windows are padded
                                 # by a tenth of the window on each side (map_plan.plan_visible_pad), so the grown frame stays inside them


def fit_footer(tpl: dict, need_mm: float) -> float:
    """`footer_fit: true` templates (M16): the footer band shrinks to what its notes need (at least `footer_min_mm`), and the map region,
    with `map_full`, grows down into the room it frees. Changes `tpl` in place; returns the millimetres the map grew (0 when nothing moved)."""
    if not tpl.get("footer_fit"):
        return 0.0
    ft, mp_ = tpl["footer"], tpl["map"]
    want = max(float(tpl.get("footer_min_mm", 0.0)), float(need_mm) + FOOTER_FIT_PAD_MM)
    grow = min(float(ft["h"]) - want, float(mp_["h"]) * MAX_FOOTER_GROWTH)
    if grow <= 0.5:
        return 0.0
    grow = round(grow, 2)
    tpl["footer"] = {**ft, "y": float(ft["y"]) + grow, "h": float(ft["h"]) - grow}
    tpl["map"] = {**mp_, "h": float(mp_["h"]) + grow}
    if tpl.get("map_full"):
        tpl["map_full"] = {**tpl["map_full"], "h": float(tpl["map_full"]["h"]) + grow}
    tpl["footer_grown_mm"] = grow
    return grow


def title_placement(spec: dict) -> str:
    """'in-map' (the default: the title sits in a box in the map's upper-left) or 'outside' (the title band above the map)."""
    v = (((spec.get("elements") or {}).get("title")) or {}).get("placement", "in-map")
    return "outside" if v == "outside" else "in-map"


def apply_layout(tpl: dict, spec: dict, classic: bool = False) -> dict:
    """The template as the build uses it. In the default layout the map region is `map_full` (it runs up over the title band) and `layout` is
    'in-map'; `classic` (the .pptx and QGIS paths, which place the title and logo themselves) or `elements.title.placement: outside` keeps the
    title band above the map and `layout` is 'classic'."""
    if not classic and tpl.get("map_full") and title_placement(spec) == "in-map":
        return {**tpl, "map": dict(tpl["map_full"]), "layout": "in-map"}
    return {**tpl, "layout": "classic"}


def rect_of(region: dict) -> Rect:
    return Rect(region["x"], region["y"], region["x"] + region["w"], region["y"] + region["h"])


@dataclass
class Frame:
    rect: Rect
    scale: float                 # sheet mm per display unit
    cx: float
    cy: float
    inner: Rect | None = None    # the part of `rect` the data is fitted into (the rest is room for overlays)

    def __post_init__(self):
        if self.inner is None:
            self.inner = self.rect

    @property
    def _ox(self) -> float:
        return (self.inner.x0 + self.inner.x1) / 2

    @property
    def _oy(self) -> float:
        return (self.inner.y0 + self.inner.y1) / 2

    @classmethod
    def fit(cls, rect: Rect, extent, padding_pct: float = 5.0, reserve: tuple = (0.0, 0.0, 0.0, 0.0)) -> "Frame":
        """Fit the extent into `rect`, centred. `reserve` = (left, top, right, bottom) mm kept free of data for overlays."""
        xmin, ymin, xmax, ymax = (float(v) for v in extent)
        ew, eh = max(xmax - xmin, 1e-9), max(ymax - ymin, 1e-9)
        l, t, r, b = reserve
        inner = Rect(rect.x0 + l, rect.y0 + t, rect.x1 - r, rect.y1 - b)
        pad = 1.0 - 2 * padding_pct / 100.0
        scale = min(inner.w * pad / ew, inner.h * pad / eh)
        return cls(rect, scale, (xmin + xmax) / 2, (ymin + ymax) / 2, inner)

    def panned(self, dx_mm: float, dy_mm: float) -> "Frame":
        """1.3.0 (R1-FIT): the same frame with the data moved dx mm right and dy mm down on the sheet (a shift of cx, cy: `to_mm`, `visible` and
        the scale bar's arithmetic are unchanged)."""
        if not dx_mm and not dy_mm:
            return self
        return Frame(self.rect, self.scale, self.cx - dx_mm / self.scale, self.cy + dy_mm / self.scale, self.inner)

    @property
    def visible(self) -> tuple[float, float, float, float]:
        """The display-coordinate window the whole rect shows (the reserved strips show more map, not less)."""
        x0, y0 = self.to_display(self.rect.x0, self.rect.y1)
        x1, y1 = self.to_display(self.rect.x1, self.rect.y0)
        return (float(x0), float(y0), float(x1), float(y1))

    def to_mm(self, x, y):
        mx = self._ox + (np.asarray(x, dtype="float64") - self.cx) * self.scale
        my = self._oy - (np.asarray(y, dtype="float64") - self.cy) * self.scale
        return mx, my

    def to_display(self, mx, my):
        x = self.cx + (np.asarray(mx, dtype="float64") - self._ox) / self.scale
        y = self.cy - (np.asarray(my, dtype="float64") - self._oy) / self.scale
        return x, y


def occupancy(frame: Frame, geoms, tree=None) -> np.ndarray:
    """A coarse boolean grid over the frame: True where a data feature lies under the cell centre. `tree` is an STRtree of `geoms` the
    caller already has (the renderer builds one for every layout it tries, M16)."""
    import shapely
    gx, gy = GRID
    xs = frame.rect.x0 + (np.arange(gx) + 0.5) * frame.rect.w / gx
    ys = frame.rect.y0 + (np.arange(gy) + 0.5) * frame.rect.h / gy
    X, Y = np.meshgrid(xs, ys)
    dx, dy = frame.to_display(X.ravel(), Y.ravel())
    occ = np.zeros(gx * gy, dtype=bool)
    if len(geoms):
        tree = tree if tree is not None else shapely.STRtree(np.asarray(geoms, dtype=object))
        pts = shapely.points(dx, dy)
        # a point or a thin line rarely passes under a cell centre, so a cell counts as data when a feature lies within half a cell of its centre
        half_cell = 0.5 * frame.rect.w / gx / frame.scale
        pairs = tree.query(pts, predicate="dwithin", distance=half_cell)
        occ[np.unique(pairs[0])] = True
    return occ.reshape(gy, gx)


def cover(frame: Frame, occ: np.ndarray, r: Rect) -> float:
    gy, gx = occ.shape
    i0 = max(0, int((r.x0 - frame.rect.x0) / frame.rect.w * gx))
    i1 = min(gx, int(np.ceil((r.x1 - frame.rect.x0) / frame.rect.w * gx)))
    j0 = max(0, int((r.y0 - frame.rect.y0) / frame.rect.h * gy))
    j1 = min(gy, int(np.ceil((r.y1 - frame.rect.y0) / frame.rect.h * gy)))
    sub = occ[j0:j1, i0:i1]
    return float(sub.mean()) if sub.size else 0.0


def covered_fraction(frame: Frame, occ: np.ndarray | None, rects: list[Rect]) -> float:
    """Share of the data grid cells that any of the rects lies over (0 when there is no data)."""
    if occ is None or not occ.any():
        return 0.0
    gy, gx = occ.shape
    mask = np.zeros_like(occ, dtype=bool)
    for r in rects:
        i0 = max(0, int((r.x0 - frame.rect.x0) / frame.rect.w * gx))
        i1 = min(gx, int(np.ceil((r.x1 - frame.rect.x0) / frame.rect.w * gx)))
        j0 = max(0, int((r.y0 - frame.rect.y0) / frame.rect.h * gy))
        j1 = min(gy, int(np.ceil((r.y1 - frame.rect.y0) / frame.rect.h * gy)))
        mask[j0:j1, i0:i1] = True
    return float((mask & occ).sum() / occ.sum())


def corner_rect(frame_rect: Rect, corner: str, w: float, h: float, inset: float = CORNER_INSET_MM) -> Rect:
    x0 = frame_rect.x0 + inset if corner.endswith("left") else frame_rect.x1 - inset - w
    y0 = frame_rect.y0 + inset if corner.startswith("upper") else frame_rect.y1 - inset - h
    return Rect(x0, y0, x0 + w, y0 + h)


def _stacked(area: Rect, corner: str, w: float, h: float, blockers: list[Rect], gap: float = 1.5) -> list[Rect]:
    """Candidates in the same corner column as `corner`, moved clear of the elements already there (up from a lower corner, down from an upper one)."""
    base = corner_rect(area, corner, w, h)
    out = []
    for b in blockers:
        if not (base.x0 < b.x1 + gap and base.x1 > b.x0 - gap):
            continue
        y0 = b.y0 - gap - h if corner.startswith("lower") else b.y1 + gap
        out.append(Rect(base.x0, y0, base.x1, y0 + h))
    return out


def place_overlays(frame: Frame, occ: np.ndarray | None, items: list[dict], banner_h: float = 0.0) -> dict:
    """items: [{name, w, h, prefer, locked, beside?, side?}] in priority order. Returns {name: {rect, corner, moved_from, reason, cover}}.

    `prefer` is a corner name. `locked` means the corner was named (by the spec or the default layout): it is kept unless it overlaps another element.
    An element whose corner is taken stacks next to what is there (above it in a lower corner, below it in an upper one) before another corner is tried.
    `beside` names an element already placed; this one goes on its `side` ('left' or 'right'), aligned with the anchor's top (bottom in a lower corner), when it fits there.
    """
    placed: dict[str, dict] = {}
    area = Rect(frame.rect.x0, frame.rect.y0 + banner_h, frame.rect.x1, frame.rect.y1)

    def free(r: Rect) -> bool:
        return r.inside(frame.rect) and not any(r.grown(1.0).hits(p["rect"]) for p in placed.values())

    for it in items:
        order = [it["prefer"]] + [c for c in CORNERS if c != it["prefer"]]
        best = None
        pref_issue = None
        cands: list[tuple[str, Rect, bool]] = []
        anchor = placed.get(it.get("beside") or "")
        if anchor is not None:
            a = anchor["rect"]
            low = anchor["corner"].startswith("lower")
            y0 = a.y1 - it["h"] if low else a.y0
            if it.get("side", "left") == "left":
                cands.append((anchor["corner"], Rect(a.x0 - 2.0 - it["w"], y0, a.x0 - 2.0, y0 + it["h"]), True))
            else:
                cands.append((anchor["corner"], Rect(a.x1 + 2.0, y0, a.x1 + 2.0 + it["w"], y0 + it["h"]), True))
        for k, corner in enumerate(order):
            cands.append((corner, corner_rect(area, corner, it["w"], it["h"]), k == 0))
            if k == 0:
                for r in _stacked(area, corner, it["w"], it["h"], [p["rect"] for p in placed.values() if p["corner"] == corner]):
                    cands.append((corner, r, True))
        for n, (corner, r, is_pref) in enumerate(cands):
            if not r.inside(frame.rect):
                if is_pref and pref_issue is None:
                    pref_issue = "does not fit there"
                continue
            if any(r.grown(1.0).hits(p["rect"]) for p in placed.values()):
                if is_pref and pref_issue is None:
                    pref_issue = "is taken by another element"
                continue
            cv = cover(frame, occ, r) if occ is not None else 0.0
            if is_pref and (cv <= DATA_COVER_LIMIT or it.get("locked")):
                best = (corner, r, cv)
                break
            if is_pref and pref_issue is None:
                pref_issue = f"would cover {cv:.0%} of the data"
            if best is None or cv < best[2]:
                best = (corner, r, cv)
        if best is None:
            raise LayoutError(f"no room for the {it['name']} ({it['w']:.0f} x {it['h']:.0f} mm) in the map frame (MAP-SPEC-005)")
        corner, r, cv = best
        moved = corner != it["prefer"]
        placed[it["name"]] = {"rect": r, "corner": corner, "cover": round(cv, 3), "moved_from": it["prefer"] if moved else None,
                              "reason": f"{it['prefer']} {pref_issue}" if moved and pref_issue else None}
    return placed


# ---------------------------------------------------------------------------------------------------------------- 1.3.0 (R1-FIT): move first
FOCUS_RES = 2           # the pan search's grid is twice the occupancy grid's resolution


def pan_room(frame: Frame, data_bbox_mm: tuple, padding_pct: float) -> tuple[float, float, float, float]:
    """(dx_min, dx_max, dy_min, dy_max) mm: how far the data may move and keep its box, with the padding, inside the map frame."""
    bx0, by0, bx1, by1 = data_bbox_mm
    r = frame.rect
    px, py = r.w * padding_pct / 100.0, r.h * padding_pct / 100.0
    lo_x, hi_x = r.x0 + px - bx0, r.x1 - px - bx1
    lo_y, hi_y = r.y0 + py - by0, r.y1 - py - by1
    return (min(0.0, lo_x), max(0.0, hi_x), min(0.0, lo_y), max(0.0, hi_y))


def focus_grid(frame: Frame, geoms, room: tuple, tree=None) -> dict | None:
    """The focus area's cells (the `focus:` layer, or the data) on a grid FOCUS_RES times finer than GRID, over the frame grown by the pan room, as
    a summed-area table, built once per frame. -> {sat, x0, y0, cw, ch, nx, ny, total} or None when there is nothing to cover."""
    import shapely
    if geoms is None or not len(geoms):
        return None
    gx, gy = GRID[0] * FOCUS_RES, GRID[1] * FOCUS_RES
    cw, ch = frame.rect.w / gx, frame.rect.h / gy
    dx0, dx1, dy0, dy1 = room
    # the data moves by (dx, dy) on the sheet; an element at r then lies over the data cells at r - (dx, dy): the grid reaches that far
    x0 = frame.rect.x0 - max(0.0, dx1) - cw
    x1 = frame.rect.x1 - min(0.0, dx0) + cw
    y0 = frame.rect.y0 - max(0.0, dy1) - ch
    y1 = frame.rect.y1 - min(0.0, dy0) + ch
    nx, ny = max(1, int(np.ceil((x1 - x0) / cw))), max(1, int(np.ceil((y1 - y0) / ch)))
    xs = x0 + (np.arange(nx) + 0.5) * cw
    ys = y0 + (np.arange(ny) + 0.5) * ch
    X, Y = np.meshgrid(xs, ys)
    dx_, dy_ = frame.to_display(X.ravel(), Y.ravel())
    tree = tree if tree is not None else shapely.STRtree(np.asarray(geoms, dtype=object))
    pairs = tree.query(shapely.points(dx_, dy_), predicate="dwithin", distance=0.5 * cw / frame.scale)
    occ = np.zeros(nx * ny, dtype=np.int32)
    occ[np.unique(pairs[0])] = 1
    occ = occ.reshape(ny, nx)
    total = int(occ.sum())
    if not total:
        return None
    sat = np.zeros((ny + 1, nx + 1), dtype=np.int64)
    sat[1:, 1:] = occ.cumsum(0).cumsum(1)
    return {"sat": sat, "x0": x0, "y0": y0, "cw": cw, "ch": ch, "nx": nx, "ny": ny, "total": total}


def _rect_cells(fg: dict, r: Rect, dx: float, dy: float) -> int:
    i0 = int(np.clip(np.floor((r.x0 - dx - fg["x0"]) / fg["cw"] + 0.5), 0, fg["nx"]))
    i1 = int(np.clip(np.floor((r.x1 - dx - fg["x0"]) / fg["cw"] + 0.5), 0, fg["nx"]))
    j0 = int(np.clip(np.floor((r.y0 - dy - fg["y0"]) / fg["ch"] + 0.5), 0, fg["ny"]))
    j1 = int(np.clip(np.floor((r.y1 - dy - fg["y0"]) / fg["ch"] + 0.5), 0, fg["ny"]))
    if i1 <= i0 or j1 <= j0:
        return 0
    s = fg["sat"]
    return int(s[j1, i1] - s[j0, i1] - s[j1, i0] + s[j0, i0])


def focus_cover(fg: dict | None, rects: list[Rect], dx: float = 0.0, dy: float = 0.0) -> float:
    """The share of the focus area the rects lie over with the data moved by (dx, dy). The elements never overlap (place_overlays keeps them 1 mm
    apart), so the sum of their cells is the cells of their union."""
    if fg is None:
        return 0.0
    return sum(_rect_cells(fg, r, dx, dy) for r in rects) / fg["total"]


def pan_offsets(frame: Frame, data_bbox_mm: tuple, overlay_rects: list[Rect], fg: dict | None, limit: float, room: tuple) -> dict:
    """The smallest move of the data that brings the elements' cover of the focus area to `limit` or under (R1-FIT step 2).

    Candidates: a 9 x 9 lattice over the room, plus the exact moves that clear each element's inner edge (the data box beside or below it), each
    with the other axis at 0 and at its own clearing moves. Each is scored by cover; the nearest one at the limit wins. When none reaches it, the
    one with the least cover (then the shortest move). -> {dx, dy, cover_before, cover_after, reached, tried}."""
    before = focus_cover(fg, overlay_rects)
    out = {"dx": 0.0, "dy": 0.0, "cover_before": before, "cover_after": before, "reached": before <= limit, "tried": 1}
    if fg is None or before <= limit:
        return out
    dx0, dx1, dy0, dy1 = room
    if dx1 - dx0 < 0.05 and dy1 - dy0 < 0.05:
        return out
    bx0, by0, bx1, by1 = data_bbox_mm
    gap = 1.0
    xs = set(np.linspace(dx0, dx1, 9).tolist()) | {0.0}
    ys = set(np.linspace(dy0, dy1, 9).tolist()) | {0.0}
    for r in overlay_rects:
        xs |= {r.x1 + gap - bx0, r.x0 - gap - bx1}
        ys |= {r.y1 + gap - by0, r.y0 - gap - by1}
    xs = {min(max(x, dx0), dx1) for x in xs}
    ys = {min(max(y, dy0), dy1) for y in ys}
    cands = sorted({(round(x, 3), round(y, 3)) for x in xs for y in ys}, key=lambda c: (c[0] ** 2 + c[1] ** 2, abs(c[0]), abs(c[1])))
    best = None
    for x, y in cands:
        cv = focus_cover(fg, overlay_rects, x, y)
        if cv <= limit:
            return {"dx": float(x), "dy": float(y), "cover_before": before, "cover_after": cv, "reached": True, "tried": len(cands)}
        if best is None or cv < best[0] - 1e-12:
            best = (cv, x, y)
    cv, x, y = best
    if cv >= before:
        return {**out, "tried": len(cands)}
    return {"dx": float(x), "dy": float(y), "cover_before": before, "cover_after": cv, "reached": False, "tried": len(cands)}
