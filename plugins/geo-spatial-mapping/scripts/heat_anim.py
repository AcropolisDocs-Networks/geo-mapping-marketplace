"""heat_anim: heat in a video, on one fixed scale (2.1.0, Day 19 video issues 11, 14 and 15).

Day 19's video was drawn by hand: 47,000 events as solid dots that never faded blanketed the city, so the month-by-month heat, the chart of
complaint shares and the average-high line were drawn by a script of the session's own. This module gives the events video three things:

- **settle** (`events_anim --trail settle`): each event flashes and fades, and what it leaves behind is a heat surface that grows on one scale,
  fixed at the end's peak, so the city warms up as the events come in, never re-scaled from frame to frame.
- **periods** (`events_anim --periods COLUMN`): one heat surface per value of a column (a month, a year), every one on the same scale (the
  peak of the busiest period), held and cross-faded in order. An atlas by time re-scales each period to its own peak and hides the seasons.
- **chart** (a timeline's `chart:` block): bars, and a line on its own axis, one row per period or moment, the current row marked as the
  video reaches it; the panel keeps clear of the map's legend, inset, title and logo.

The surface is drawn in the frame's own pixels: the events are counted on a grid of `cell_px` pixels and smoothed with a Gaussian kernel of
the bandwidth asked for (metres on the map), kept on land when the map has a ground, and coloured from the brand's sequential ramp, clear at
the low end, so the base map shows through where there is little.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

CELL_PX = 3                  # the grid the events are counted on, in frame pixels
LOW_CLEAR = 0.04             # below this share of the scale nothing is drawn
MAX_ALPHA = 0.86             # the strongest heat, as opacity
FLASH_S = 1.0                # settle: how long an event's flash takes to fade
CHART_W, CHART_H = 0.36, 0.22    # the chart panel's share of the frame


class HeatError(ValueError):
    pass


def _blur(h, sigma: float):
    try:
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(h, sigma=sigma, mode="constant")
    except ImportError:                                            # numpy only: a separable kernel along each axis
        r = int(math.ceil(4 * sigma))
        k = np.exp(-0.5 * (np.arange(-r, r + 1) / max(sigma, 1e-6)) ** 2)
        k /= k.sum()
        z = np.apply_along_axis(lambda a: np.convolve(a, k, mode="same"), 0, h)
        return np.apply_along_axis(lambda a: np.convolve(a, k, mode="same"), 1, z)


def _hex(h: str) -> tuple[int, int, int]:
    h = str(h).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def ramp_of(tokens: dict, n: int = 64) -> np.ndarray:
    """n colours (n x 3, 0-255) from the brand's sequential ramp, light to dark, interpolated; the emphasis colour alone when there is none."""
    r = tokens.get("sequential_ramp") or tokens.get("_brand_sequential_ramp") or []       # the ramp a raster reads under the map's look
    r = [c for c in (r if isinstance(r, (list, tuple)) else []) if isinstance(c, str) and c.startswith("#")]
    if len(r) < 2:
        e = _hex(tokens["emphasis"])                       # the brand's tokens always carry these two
        bg = _hex(tokens["background"])
        r = ["#%02x%02x%02x" % tuple(int(bg[i] + (e[i] - bg[i]) * f) for i in range(3)) for f in (0.25, 1.0)]
    cs = np.array([_hex(c) for c in r], dtype=float)
    t = np.linspace(0, 1, len(cs))
    u = np.linspace(0, 1, n)
    return np.stack([np.interp(u, t, cs[:, i]) for i in range(3)], axis=1)


class Field:
    """Events in frame pixels -> heat images on one scale. `px`: (n, 2) pixel positions; `size`: (w, h); `sigma_px`: the kernel in pixels;
    `mask`: an (h, w) array of 0..1 (land) or None."""

    def __init__(self, px, size, sigma_px: float, tokens: dict, mask=None, weights=None):
        self.w, self.h = int(size[0]), int(size[1])
        self.gw, self.gh = max(1, math.ceil(self.w / CELL_PX)), max(1, math.ceil(self.h / CELL_PX))
        p = np.asarray(px, dtype=float).reshape(-1, 2)
        ok = np.isfinite(p).all(axis=1)
        self.gx = np.clip((p[:, 0] / CELL_PX).astype(int), 0, self.gw - 1)
        self.gy = np.clip((p[:, 1] / CELL_PX).astype(int), 0, self.gh - 1)
        self.ok = ok & (p[:, 0] >= 0) & (p[:, 0] < self.w) & (p[:, 1] >= 0) & (p[:, 1] < self.h)
        self.wt = np.ones(len(p)) if weights is None else np.asarray(weights, dtype=float)
        self.sigma = max(0.6, sigma_px / CELL_PX)
        self.ramp = ramp_of(tokens)
        self.mask = None
        if mask is not None:
            from PIL import Image
            m = Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8)).resize((self.gw, self.gh), Image.BILINEAR)
            self.mask = np.asarray(m, dtype=float) / 255.0
        self.peak = None

    def grid(self, keep) -> np.ndarray:
        """The smoothed count of the events `keep` selects (a boolean array), on the grid."""
        k = np.asarray(keep, dtype=bool) & self.ok
        h = np.zeros((self.gh, self.gw), dtype=float)
        np.add.at(h, (self.gy[k], self.gx[k]), self.wt[k])
        z = _blur(h, self.sigma)
        if self.mask is not None:
            z = z * self.mask
        return z

    def fix_scale(self, grids) -> float:
        """The scale every frame shares: the highest cell of the given grids (the end of a settle, the busiest period)."""
        self.peak = max(float(np.max(g)) for g in grids) if grids else 0.0
        if self.peak <= 0:
            raise HeatError("the heat has no weight: no event falls inside the map")
        return self.peak

    def image(self, z, strength: float = 1.0):
        """An RGBA image of the frame's size for grid z on the fixed scale."""
        from PIL import Image
        if self.peak is None:
            self.fix_scale([z])
        u = np.clip(z / self.peak, 0, 1)
        u = np.sqrt(u)                                             # a perceptual lift: the low end is not lost against the ground
        idx = np.clip((u * (len(self.ramp) - 1)).astype(int), 0, len(self.ramp) - 1)
        rgb = self.ramp[idx]
        a = np.where(u < LOW_CLEAR, 0.0, np.clip((u - LOW_CLEAR) / (1 - LOW_CLEAR), 0, 1) ** 0.7 * MAX_ALPHA) * float(strength)
        arr = np.dstack([rgb, a[..., None] * 255]).astype(np.uint8)
        return Image.fromarray(arr, "RGBA").resize((self.w, self.h), Image.BILINEAR)


def land_mask(plan, win: dict, ppm: float, size) -> np.ndarray | None:
    """The map's land in frame pixels (1 on land, 0 on water), from the plan's ground; None when the map has no land ground."""
    import shapely
    from PIL import Image, ImageDraw

    import events_anim as ea
    land, lakes = [], []
    for b in plan.basemap or []:
        g = b.get("gdf")
        if g is None or not len(g):
            continue
        if b.get("kind") in ("land", "country", "state"):
            land += [x for x in g.geometry.values if x is not None and not x.is_empty]
        elif b.get("kind") == "lake":
            lakes += [x for x in g.geometry.values if x is not None and not x.is_empty]
    if not land:
        return None
    ex0, ey0, ex1, ey1 = win["extent"]
    box = shapely.box(ex0, ey0, ex1, ey1)
    geom = shapely.intersection(shapely.union_all(shapely.make_valid(np.asarray(land, dtype=object))), box)
    if lakes:
        geom = shapely.difference(geom, shapely.union_all(shapely.make_valid(np.asarray(lakes, dtype=object))))
    w, h = int(size[0]), int(size[1])
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    for poly in shapely.get_parts(np.asarray([geom], dtype=object)):
        if poly.geom_type != "Polygon":
            continue
        rings = [np.asarray(poly.exterior.coords)] + [np.asarray(r.coords) for r in poly.interiors]
        for j, r in enumerate(rings):
            xs, ys = ea.to_px(r[:, 0], r[:, 1], win, ppm)
            pts = list(zip(np.asarray(xs).tolist(), np.asarray(ys).tolist(), strict=True))
            if len(pts) >= 3:
                d.polygon(pts, fill=255 if j == 0 else 0)
    m = np.asarray(im, dtype=float) / 255.0
    return m if m.any() else None


def sigma_px(bandwidth_m: float, win: dict, ppm: float, crs_is_geographic: bool) -> float:
    """A kernel in metres as frame pixels, through the map window (the extent in the map's CRS over the rectangle it fills)."""
    ex0, _ey0, ex1, _ey1 = win["extent"]
    rx0, _ry0, rx1, _ry1 = win["rect_mm"]
    px_per_unit = (rx1 - rx0) * ppm / max(ex1 - ex0, 1e-12)
    units_per_m = 1.0 / 111_320.0 if crs_is_geographic else 1.0
    return max(0.8, float(bandwidth_m) * units_per_m * px_per_unit)


# ---------------------------------------------------------------------------------------------------------------- periods
def period_plan(values, *, hold_s: float, fade_s: float, intro_s: float = 0.0) -> list[dict]:
    """The periods in order, each held `hold_s` after a cross-fade of `fade_s` from the one before: [{value, start, end}] in seconds."""
    keys = [v for v in dict.fromkeys(list(values)) if v is not None and not (isinstance(v, float) and math.isnan(v))]
    try:
        keys = sorted(keys)
    except TypeError:
        pass
    if not 1 <= len(keys) <= 120:
        raise HeatError(f"{len(keys)} periods; a period video takes 1 to 120 of them")
    out, t = [], float(intro_s)
    for i, v in enumerate(keys):
        start = t
        t += (fade_s if i else 0.0) + hold_s
        out.append({"value": v.item() if hasattr(v, "item") else v, "start": round(start, 3), "end": round(t, 3)})
    return out


def period_at(periods: list[dict], t: float, fade_s: float) -> tuple[int, int, float]:
    """(i, j, f): the period before, the one after and the blend at t (f = 0 shows i alone)."""
    for j, p in enumerate(periods):
        if t < p["end"]:
            if j == 0:
                return 0, 0, 0.0
            f = (t - p["start"]) / fade_s if fade_s > 0 else 1.0
            return (j - 1, j, float(min(1.0, max(0.0, f)))) if f < 1 else (j, j, 0.0)
    return len(periods) - 1, len(periods) - 1, 0.0


# ---------------------------------------------------------------------------------------------------------------- the chart panel
CHART_KINDS = ("bars", "profile")
MIN_TEXT_1080 = 20           # 5.5.0 (D26-31): the smallest chart text on a 1080-line frame, in pixels (scaled to the frame's height)


BASELINES = ("first", "last")
LABEL_MODES = ("all", "ends")


def _rows_from_csv(c: dict, base) -> list[dict]:
    """5.8.0 (Day 27): bars read from a table, never typed: {source: areas.csv, label: year, bar: area_km2, line: col?}. A relative path is
    read from the timeline's own folder."""
    import csv
    p = Path(str(c["source"]))
    if not p.is_absolute() and base is not None:
        p = Path(base) / p
    if not p.is_file():
        raise HeatError(f"chart source {p} was not found")
    lab, bar, line = c.get("label"), c.get("bar"), c.get("line")
    if not lab or not bar:
        raise HeatError("a chart with a source names its columns: label: <column>, bar: <column> (line: <column> optional)")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        cols = rd.fieldnames or []
        for col in [x for x in (lab, bar, line) if x]:
            if col not in cols:
                raise HeatError(f"chart source {p.name} has no column '{col}' (it has {', '.join(cols)})")
        rows = []
        for i, r in enumerate(rd):
            try:
                rows.append({"label": str(r[lab]), "bar": float(r[bar]), "line": float(r[line]) if line and str(r.get(line, "")).strip() else None})
            except ValueError:
                raise HeatError(f"chart source {p.name} row {i + 2}: '{r[bar]}' in {bar} is not a number") from None
    if not rows:
        raise HeatError(f"chart source {p.name} has no rows")
    return rows


def load_chart(c, base=None) -> dict | None:
    """A timeline's `chart:` block, checked. Two kinds:

    bars (the default): {rows: [{label, bar, line?, start?}], bar_label, line_label, title, position, line_format, bar_format}. `start`
      (seconds) is when a row becomes the current one; a period video fills it in from its periods.
      5.8.0 (Day 27): the rows may come from a table instead (`source: areas.csv, label: year, bar: area_km2`, read from the timeline's
      folder); `baseline: first | last | <number>` draws each bar as its difference from that value, rising above a zero line for a gain and
      hanging below it in the brand's danger colour for a loss (`baseline_label` names the line; the first row's label by default);
      `reveal: true` draws only the rows up to the current one, on the whole run's scale; `labels: ends` keeps the first, the last and the
      current row's label (for a long run of years) instead of every label that fits.
    profile (5.5.0, D26-23): a value along the timeline's flow, the marker at the flow's front on every frame (no row times):
      {kind: profile, along: flow, raster: FILE | column: NAME, invert: true, y_label: "Depth (m)", x_label: "Day at sea",
       x_map: {from: 0, to: 64}, samples: 240}

    Both take (5.5.0, D26-31) w and h (shares of the frame, or pixels on a 1080 frame when above 1), value_label (the words beside the
    current value), y_axis (2 or 3 ticks; on by default for a profile), invert (bars hang from the top; a profile's values grow downward),
    min_text_px (the smallest text on a 1080 frame, 20 by default) and position (auto keeps inside the map frame, clear of the credits band,
    legend, locator, logo and counters)."""
    if not c:
        return None
    if not isinstance(c, dict):
        raise HeatError("chart is a mapping: {rows: [...]} or {kind: profile, along: flow, raster: FILE}")
    kind = str(c.get("kind") or "bars")
    if kind not in CHART_KINDS:
        raise HeatError(f"chart kind is {' or '.join(CHART_KINDS)}, not '{kind}'")
    rows = []
    if kind == "bars" and c.get("source") is not None:
        if c.get("rows"):
            raise HeatError("a chart takes rows: or source:, not both")
        rows = [{**r, "start": None} for r in _rows_from_csv(c, base)]
    elif kind == "bars":
        if not isinstance(c.get("rows"), list) or not c["rows"]:
            raise HeatError("chart needs rows: [{label, bar, line, start}, ...] or source: FILE with label: and bar: columns")
        for i, r in enumerate(c["rows"]):
            if not isinstance(r, dict) or "label" not in r or "bar" not in r:
                raise HeatError(f"chart row {i + 1} needs label and bar")
            rows.append({"label": str(r["label"]), "bar": float(r["bar"]), "line": None if r.get("line") is None else float(r["line"]),
                         "start": None if r.get("start") is None else float(r["start"])})
    else:
        if str(c.get("along") or "flow") != "flow":
            raise HeatError("a profile chart runs along: flow (the timeline's flow layer)")
        if not c.get("raster") and not c.get("column"):
            raise HeatError("a profile chart needs raster: FILE or column: NAME (the value along the flow)")
    pos = str(c.get("position", "auto"))
    if pos not in ("auto", "top-left", "top-right", "bottom-left", "bottom-right", "left", "right"):
        raise HeatError(f"chart position is auto, top-left, top-right, bottom-left, bottom-right, left or right, not '{pos}'")
    xm = c.get("x_map")
    if xm is not None and not (isinstance(xm, dict) and "from" in xm and "to" in xm):
        raise HeatError("chart.x_map is {from: <value at the start>, to: <value at the end>}")

    def _size(v, d):
        if v is None:
            return d
        v = float(v)
        if v <= 0:
            raise HeatError("chart w and h are above 0")
        return v
    base_v, base_lab = None, ""
    bl = c.get("baseline")
    if bl is not None:
        if kind != "bars":
            raise HeatError("baseline is for a bars chart")
        if bl in BASELINES:
            r0 = rows[0] if bl == "first" else rows[-1]
            base_v, base_lab = r0["bar"], r0["label"]
        else:
            try:
                base_v = float(bl)
            except (TypeError, ValueError):
                raise HeatError(f"chart baseline is first, last or a number, not '{bl}'") from None
            base_lab = f"{base_v:,g}"
        if c.get("invert"):
            raise HeatError("a chart with a baseline draws gains up and losses down; leave out invert")
        if any(r.get("line") is not None for r in rows):
            raise HeatError("a chart with a baseline draws bars only; leave out the line series")
    labels_mode = str(c.get("labels") or "all")
    if labels_mode not in LABEL_MODES:
        raise HeatError(f"chart labels is {' or '.join(LABEL_MODES)}, not '{labels_mode}'")
    return {"kind": kind, "rows": rows, "title": str(c.get("title") or ""), "bar_label": str(c.get("bar_label") or ""),
            "baseline": base_v, "baseline_label": str(c.get("baseline_label") or base_lab), "reveal": bool(c.get("reveal")), "labels": labels_mode,
            "line_label": str(c.get("line_label") or ""), "position": pos,
            "bar_format": str(c.get("bar_format") or ("{:+,.0f}" if base_v is not None else "{:,.0f}")),
            "line_format": str(c.get("line_format") or "{:,.0f}"), "key": c.get("key", True) is not False,
            "w": _size(c.get("w"), CHART_W), "h": _size(c.get("h"), CHART_H), "value_label": str(c.get("value_label") or c.get("y_label") or ""),
            "y_axis": bool(c.get("y_axis", kind == "profile")), "invert": bool(c.get("invert")), "min_text_px": float(c.get("min_text_px") or MIN_TEXT_1080),
            "raster": c.get("raster"), "column": c.get("column"), "y_label": str(c.get("y_label") or ""), "x_label": str(c.get("x_label") or ""),
            "x_map": xm, "samples": int(c.get("samples") or 240), "value_format": str(c.get("value_format") or "{:,.0f}")}


def chart_current(ch: dict, t: float) -> int:
    cur = 0
    for i, r in enumerate(ch["rows"]):
        if r["start"] is not None and r["start"] <= t:
            cur = i
    return cur


def panel_size(ch: dict, W: int, H: int) -> tuple[int, int]:
    """The panel's pixels: w and h as shares of the frame, or (above 1) pixels on a 1080-line frame, scaled."""
    f = H / 1080.0
    w = ch.get("w", CHART_W)
    h = ch.get("h", CHART_H)
    return (int(W * w) if w <= 1 else int(w * f)), (int(H * h) if h <= 1 else int(h * f))


def place_panel(size, w: int, h: int, avoid, want: str = "auto", pad: int = 16, frame=None) -> tuple[int, int]:
    """Where a w x h panel goes: the asked corner, else the free place (corners, then the middle of each side) with the least overlap with
    the overlays in `avoid` (pixel rectangles). 5.5.0 (D26-26): `frame` ([x0, y0, x1, y1] pixels, the map frame) keeps the panel inside the
    map, so the credits band under it is never covered, whatever corner is asked."""
    W, H = size
    fx0, fy0, fx1, fy1 = (frame if frame else (0, 0, W, H))
    fx0, fy0, fx1, fy1 = int(fx0), int(fy0), int(fx1), int(fy1)
    w, h = min(w, max(8, fx1 - fx0 - 2 * pad)), min(h, max(8, fy1 - fy0 - 2 * pad))
    spots = {"top-left": (fx0 + pad, fy0 + pad), "top-right": (fx1 - w - pad, fy0 + pad), "bottom-left": (fx0 + pad, fy1 - h - pad),
             "bottom-right": (fx1 - w - pad, fy1 - h - pad), "left": (fx0 + pad, (fy0 + fy1 - h) // 2), "right": (fx1 - w - pad, (fy0 + fy1 - h) // 2)}
    if want in spots:
        return spots[want]

    def overlap(x, y):
        return sum(max(0, min(x + w, a[2]) - max(x, a[0])) * max(0, min(y + h, a[3]) - max(y, a[1])) for a in avoid or [])
    order = ["bottom-left", "top-left", "bottom-right", "top-right", "left", "right"]
    best = min(order, key=lambda k: (overlap(*spots[k]), order.index(k)))
    return spots[best]


def _fonts(font_path: str, H: int, ch: dict):
    from PIL import ImageFont
    k = H / 720.0
    floor_px = max(9, int(round(float(ch.get("min_text_px") or MIN_TEXT_1080) * H / 1080.0)))
    return k, ImageFont.truetype(font_path, max(floor_px, int(11 * k))), ImageFont.truetype(font_path, max(floor_px + 2, int(13 * k)))


def _ticks(lo: float, hi: float, n: int = 3) -> list[float]:
    """Two or three round values from lo to hi (the y axis)."""
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return [lo]
    raw = span / max(1, n - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    first = math.ceil(lo / step) * step
    out = []
    v = first
    while v <= hi + 1e-9 and len(out) < 4:
        out.append(round(v, 6))
        v += step
    return out[:3] if len(out) > 3 else out


def tick_labels_kept(boxes: list[tuple[float, float]], cur: int, gap: float) -> list[int]:
    """5.5.0 (D26-31): which of the tick labels (x0, x1 each) are drawn: the current one always, then the others from left to right when they
    keep `gap` pixels from every one already kept. -> indices."""
    keep = [cur] if 0 <= cur < len(boxes) else []
    for i, (a0, a1) in enumerate(boxes):
        if i == cur:
            continue
        if all(a1 + gap <= boxes[j][0] or a0 >= boxes[j][1] + gap for j in keep):
            keep.append(i)
    return sorted(keep)


def paint_chart(img, t: float, ch: dict, tokens: dict, *, font_path: str, avoid=None, ramp=None, frame=None):
    """The chart panel on a frame: bars (the current row in the emphasis colour, the others lighter), the line on its own axis with a dot on
    the current row, the row labels, the two series' names, and the heat key (Fewer, More) when `ramp` is given; or (5.5.0) a profile along
    the flow with its marker at the flow's front. -> (image, panel rect)."""
    from PIL import Image, ImageDraw
    W, H = img.size
    k, f_s, f_t = _fonts(font_path, H, ch)
    pw, ph = panel_size(ch, W, H)
    x0, y0 = place_panel((W, H), pw, ph, avoid, ch["position"], pad=int(16 * k), frame=frame)
    if frame:
        pw, ph = min(pw, int(frame[2] - frame[0] - 32 * k)), min(ph, int(frame[3] - frame[1] - 32 * k))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    bg = _hex(tokens.get("legend_bg") or tokens["background"])
    ink = _hex(tokens["label"])
    emph = _hex(tokens["emphasis"])
    danger = _hex(((tokens.get("status") or {}).get("danger")) or tokens["emphasis"])
    soft = tuple(int(bg[i] + (emph[i] - bg[i]) * 0.38) for i in range(3))
    d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=int(6 * k), fill=(*bg, 225))
    pad = int(10 * k)
    top = y0 + pad
    if ch["title"]:
        d.text((x0 + pad, top), ch["title"], font=f_t, fill=(*ink, 255))
        top += int(f_t.size * 1.35)
    if ch.get("kind") == "profile":
        return _paint_profile(img, over, d, t, ch, x0, y0, pw, ph, top, pad, k, f_s, ink, emph, danger, bg)
    names = " · ".join(x for x in (ch["bar_label"], ch["line_label"]) if x)
    if names:
        d.text((x0 + pad, top), names, font=f_s, fill=(*ink, 200))
        top += int(f_s.size * 1.35)
    key_h = int(f_s.size * 1.6) if ramp is not None and ch.get("key") else 0
    if ch.get("baseline") is not None:
        _paint_diverging(d, t, ch, x0, y0, pw, ph, top, pad, k, f_s, ink, emph, danger, bg, key_h)
        im = img.convert("RGBA")
        im.alpha_composite(over)
        return im.convert("RGB"), [x0, y0, x0 + pw, y0 + ph]
    rows = ch["rows"]
    n = len(rows)
    cur = chart_current(ch, t)
    bmax = max(r["bar"] for r in rows) or 1.0
    axis_w = 0
    if ch.get("y_axis"):
        ticks = _ticks(0.0, bmax)
        axis_w = int(max(d.textlength(ch["bar_format"].format(v), font=f_s) for v in ticks) + 6 * k)
    left = x0 + pad + axis_w
    bw_all = pw - 2 * pad - axis_w
    slot = bw_all / n
    inv = bool(ch.get("invert"))
    lab_h = int(f_s.size * 1.4)
    if inv:                                            # 5.5.0: bars hang from a baseline at the top (depth reads downward); labels above it
        base_y = top + lab_h
        bottom = y0 + ph - pad - key_h
    else:
        base_y = y0 + ph - pad - lab_h - key_h
        bottom = base_y
    hh = max(4, (bottom - base_y) if inv else (base_y - top))
    lv = [r["line"] for r in rows if r["line"] is not None]
    lmin, lmax = (min(lv), max(lv)) if lv else (0.0, 1.0)
    span = (lmax - lmin) or 1.0
    pts = []
    centers = []
    for i, r in enumerate(rows):
        bx0 = left + i * slot + slot * 0.15
        bx1 = left + (i + 1) * slot - slot * 0.15
        bh = hh * r["bar"] / bmax
        box = [bx0, base_y, bx1, base_y + bh] if inv else [bx0, base_y - bh, bx1, base_y]
        d.rectangle(box, fill=(*(emph if i == cur else soft), 255))
        if r["line"] is not None:
            pts.append(((bx0 + bx1) / 2, base_y - hh * 0.08 - (hh * 0.84) * (r["line"] - lmin) / span))
        centers.append((bx0 + bx1) / 2)
    if ch.get("y_axis"):
        for v in _ticks(0.0, bmax):
            yv = base_y + hh * v / bmax if inv else base_y - hh * v / bmax
            lab = ch["bar_format"].format(v)
            d.text((left - 4 * k - d.textlength(lab, font=f_s), yv - f_s.size * 0.6), lab, font=f_s, fill=(*ink, 170))
            d.line([(left - 2 * k, yv), (x0 + pw - pad, yv)], fill=(*ink, 40), width=1)
    labs = [r["label"][:3] if n > 6 and not r["label"].strip().replace(".", "").isdigit() else r["label"] for r in rows]   # 5.8.0: a year stays whole
    wid = [d.textlength(lb, font=f_s) for lb in labs]
    boxes = [(c - w_ / 2, c + w_ / 2) for c, w_ in zip(centers, wid)]
    ly = (top if inv else base_y + int(2 * k))
    for i in tick_labels_kept(boxes, cur, gap=4 * k):            # 5.5.0 (D26-31): the current label never overprints its neighbours
        d.text((boxes[i][0], ly), labs[i], font=f_s, fill=(*ink, 255 if i == cur else 170))
    if len(pts) >= 2:
        d.line(pts, fill=(*danger, 255), width=max(2, int(2 * k)))
    if pts and rows[cur]["line"] is not None:
        cx, cy = pts[min(cur, len(pts) - 1)]
        rr = max(3, int(3.5 * k))
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(*danger, 255), outline=(*bg, 255))
    vals = ch["bar_format"].format(rows[cur]["bar"]) + (("  ·  " + ch["line_format"].format(rows[cur]["line"])) if rows[cur]["line"] is not None else "")
    if ch.get("value_label"):
        vals = f"{ch['value_label']}: {vals}"
    tw = d.textlength(vals, font=f_s)
    d.text((x0 + pw - pad - tw, y0 + pad), vals, font=f_s, fill=(*ink, 255))
    if key_h:
        ky = y0 + ph - pad - int(f_s.size * 1.1)
        lw = d.textlength("Fewer", font=f_s)
        kx0, kx1 = x0 + pad + lw + int(6 * k), x0 + pw - pad - d.textlength("More", font=f_s) - int(6 * k)
        d.text((x0 + pad, ky - int(f_s.size * 0.15)), "Fewer", font=f_s, fill=(*ink, 255))
        d.text((kx1 + int(6 * k), ky - int(f_s.size * 0.15)), "More", font=f_s, fill=(*ink, 255))
        for i in range(int(kx1 - kx0)):
            c = ramp[int(i / max(1, kx1 - kx0 - 1) * (len(ramp) - 1))]
            a = int(255 * (0.25 + 0.75 * i / max(1, kx1 - kx0 - 1)))
            d.line([(kx0 + i, ky), (kx0 + i, ky + int(f_s.size * 0.7))], fill=(int(c[0]), int(c[1]), int(c[2]), a))
    im = img.convert("RGBA")
    im.alpha_composite(over)
    return im.convert("RGB"), [x0, y0, x0 + pw, y0 + ph]


def _signed(fmt: str, v: float) -> str:
    """A formatted value with a true minus sign (U+2212) for a loss, never a hyphen; zero reads as 0, not +0."""
    import re
    s = re.sub(r"^[+\-]0(?=$|[^\d.,])", "0", fmt.format(v))          # +0 and -0 (a rounded small value) read 0
    return s.replace("-", "−")


def diverging_geometry(rows: list[dict], base: float, top: float, bottom: float) -> dict:
    """5.8.0: where the zero line and each bar's ends fall in a panel from `top` to `bottom` (pixels): the scale runs from the largest loss to
    the largest gain of the whole run (zero always inside it), so a revealed bar never rescales the chart. -> {zero_y, y(v), lo, hi, diffs}."""
    diffs = [r["bar"] - base for r in rows]
    lo, hi = min(0.0, min(diffs)), max(0.0, max(diffs))
    span = (hi - lo) or 1.0
    h = max(4.0, bottom - top)

    def y(v: float) -> float:
        return top + (hi - v) / span * h
    return {"zero_y": y(0.0), "y": y, "lo": lo, "hi": hi, "diffs": diffs}


def _paint_diverging(d, t, ch, x0, y0, pw, ph, top, pad, k, f_s, ink, emph, danger, bg, key_h):
    """5.8.0 (Day 27): bars from a baseline: a gain rises from the zero line in the emphasis colour, a loss hangs below it in the danger colour;
    the current row at full strength, the others lighter, the rows after it left out when `reveal` is on; the zero line carries the baseline's
    name, and the current difference is printed top right."""
    rows = ch["rows"]
    n = len(rows)
    cur = chart_current(ch, t)
    base = float(ch["baseline"])
    lab_h = int(f_s.size * 1.4)
    bottom = y0 + ph - pad - lab_h - key_h
    top = top + int(f_s.size * 0.4)
    g = diverging_geometry(rows, base, top, bottom)
    fmt = ch["bar_format"]
    ticks = sorted(set([0.0] + (_ticks(g["lo"], g["hi"]) if ch.get("y_axis") else [])))
    tick_txt = {v: (ch["baseline_label"] if abs(v) < 1e-9 and ch["baseline_label"] else _signed(fmt, v)) for v in ticks}
    axis_w = int(max(d.textlength(s_, font=f_s) for s_ in tick_txt.values()) + 6 * k)
    left = x0 + pad + axis_w
    slot = (pw - 2 * pad - axis_w) / n

    def lighter(c):
        return tuple(int(bg[i] + (c[i] - bg[i]) * 0.42) for i in range(3))
    for v in ticks:
        yv = g["y"](v)
        s_ = tick_txt[v]
        d.text((left - 4 * k - d.textlength(s_, font=f_s), yv - f_s.size * 0.6), s_, font=f_s, fill=(*ink, 255 if abs(v) < 1e-9 else 170))
        if abs(v) >= 1e-9:
            d.line([(left - 2 * k, yv), (x0 + pw - pad, yv)], fill=(*ink, 40), width=1)
    centers = []
    for i, r in enumerate(rows):
        bx0 = left + i * slot + slot * 0.12
        bx1 = left + (i + 1) * slot - slot * 0.12
        centers.append((bx0 + bx1) / 2)
        if ch.get("reveal") and i > cur:
            continue
        dv = g["diffs"][i]
        col = emph if dv >= 0 else danger
        ya, yb = sorted((g["zero_y"], g["y"](dv)))
        if yb - ya < 1:
            yb = ya + 1
        d.rectangle([bx0, ya, bx1, yb], fill=(*(col if i == cur else lighter(col)), 255))
    d.line([(left - 2 * k, g["zero_y"]), (x0 + pw - pad, g["zero_y"])], fill=(*ink, 200), width=max(1, int(1.5 * k)))
    labs = [r["label"] for r in rows]
    keep = sorted({0, n - 1, cur}) if ch.get("labels") == "ends" else list(range(n))
    wid = {i: d.textlength(labs[i], font=f_s) for i in keep}
    boxes = [(centers[i] - wid[i] / 2, centers[i] + wid[i] / 2) for i in keep]
    for j in tick_labels_kept(boxes, keep.index(cur), gap=6 * k):
        i = keep[j]
        bx = min(max(boxes[j][0], left), x0 + pw - pad - wid[i])
        d.text((bx, bottom + int(2 * k)), labs[i], font=f_s, fill=(*ink, 255 if i == cur else 170))
    vals = _signed(fmt, g["diffs"][cur])
    if ch.get("value_label"):
        vals = f"{ch['value_label']}: {vals}"
    tw = d.textlength(vals, font=f_s)
    d.text((x0 + pw - pad - tw, y0 + pad), vals, font=f_s, fill=(*(danger if g["diffs"][cur] < 0 else ink), 255))


def profile_value_at(ch: dict, u: float) -> float:
    """The profile's value at progress u (0 to 1)."""
    xs, ys = ch["_profile"]
    if not len(xs):
        return float("nan")
    x = xs[0] + (xs[-1] - xs[0]) * float(u)
    ok = np.isfinite(ys)
    return float(np.interp(x, xs[ok], ys[ok])) if ok.any() else float("nan")


def x_text(ch: dict, u: float) -> str:
    xm = ch.get("x_map")
    if not xm:
        return ""
    v = float(xm["from"]) + (float(xm["to"]) - float(xm["from"])) * float(u)
    return f"{ch['x_label'] + ' ' if ch.get('x_label') else ''}{int(math.floor(v + 1e-9))}"


def _paint_profile(img, over, d, t, ch, x0, y0, pw, ph, top, pad, k, f_s, ink, emph, danger, bg):
    """5.5.0 (D26-23): the profile: the area under the line (or hanging from the top when inverted), the marker at the flow's front."""
    xs, ys = ch["_profile"]
    u = float(ch["_progress"](t)) if ch.get("_progress") else 0.0
    ok = np.isfinite(ys)
    lo_, hi_ = (float(np.nanmin(ys)), float(np.nanmax(ys))) if ok.any() else (0.0, 1.0)
    inv = bool(ch.get("invert"))
    vals = np.abs(ys) if inv and hi_ <= 0 else ys                   # depths stored as negative elevations read as metres down
    vlo, vhi = (float(np.nanmin(vals)), float(np.nanmax(vals))) if ok.any() else (0.0, 1.0)
    if inv:
        vlo = min(0.0, vlo)
    ticks = _ticks(vlo, vhi) if ch.get("y_axis") else []
    axis_w = int(max([d.textlength(ch["value_format"].format(v), font=f_s) for v in ticks] or [0]) + (6 * k if ticks else 0))
    bottom_lab = int(f_s.size * 1.5) if ch.get("x_map") or ch.get("x_label") else 0
    cx0, cx1 = x0 + pad + axis_w, x0 + pw - pad
    cy0, cy1 = top + int(f_s.size * 0.2), y0 + ph - pad - bottom_lab
    span = (vhi - vlo) or 1.0
    sx = lambda x: cx0 + (x - xs[0]) / max(xs[-1] - xs[0], 1e-9) * (cx1 - cx0)       # noqa: E731
    sy = (lambda v: cy0 + (v - vlo) / span * (cy1 - cy0)) if inv else (lambda v: cy1 - (v - vlo) / span * (cy1 - cy0))   # noqa: E731
    pts = [(sx(x), sy(v)) for x, v in zip(xs, vals) if np.isfinite(v)]
    if len(pts) >= 2:
        edge = cy0 if inv else cy1
        d.polygon([(pts[0][0], edge)] + pts + [(pts[-1][0], edge)], fill=(*emph, 70))
        d.line(pts, fill=(*emph, 255), width=max(2, int(2 * k)))
    for v in ticks:
        yv = sy(v)
        lab = ch["value_format"].format(v)
        d.text((cx0 - 4 * k - d.textlength(lab, font=f_s), yv - f_s.size * 0.6), lab, font=f_s, fill=(*ink, 170))
        d.line([(cx0, yv), (cx1, yv)], fill=(*ink, 40), width=1)
    mx = cx0 + u * (cx1 - cx0)
    v_now = profile_value_at({"_profile": (xs, vals)}, u)
    d.line([(mx, cy0), (mx, cy1)], fill=(*ink, 150), width=max(1, int(k)))
    if np.isfinite(v_now):
        my = sy(v_now)
        rr = max(4, int(4.5 * k))
        d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=(*danger, 255), outline=(*bg, 255), width=max(1, int(1.5 * k)))
        txt = (f"{ch['y_label']}: " if ch.get("y_label") else (f"{ch['value_label']}: " if ch.get("value_label") else "")) + ch["value_format"].format(v_now)
        tw = d.textlength(txt, font=f_s)
        d.text((x0 + pw - pad - tw, y0 + pad), txt, font=f_s, fill=(*ink, 255))
    if bottom_lab:
        a = x_text(ch, 0.0) if ch.get("x_map") else ""
        b = x_text(ch, 1.0) if ch.get("x_map") else ""
        now = x_text(ch, u) if ch.get("x_map") else ch.get("x_label", "")
        yb = cy1 + int(2 * k)
        boxes = [(cx0, cx0 + d.textlength(a, font=f_s)), (mx - d.textlength(now, font=f_s) / 2, mx + d.textlength(now, font=f_s) / 2),
                 (cx1 - d.textlength(b, font=f_s), cx1)]
        texts = [a, now, b]
        for i in tick_labels_kept(boxes, 1, gap=4 * k):
            if texts[i]:
                d.text((boxes[i][0], yb), texts[i], font=f_s, fill=(*ink, 255 if i == 1 else 170))
    im = img.convert("RGBA")
    im.alpha_composite(over)
    return im.convert("RGB"), [x0, y0, x0 + pw, y0 + ph]
