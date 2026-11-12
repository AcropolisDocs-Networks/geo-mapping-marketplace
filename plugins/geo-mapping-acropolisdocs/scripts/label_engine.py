#!/usr/bin/env python3
"""label-engine: collision-free label placement with halos (spec 7.3, 12.3b MAP-CART-007).

Works in sheet millimetres. A label is a rectangle sized with real Inter metrics; it is placed only if it stays inside the map
frame and overlaps neither an obstacle (a legend, a symbol, another label) nor a label already placed. Higher-priority labels are
placed first, so when space runs out it is the least important ones that are dropped, and every drop is counted and reasoned.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import textmeasure as tm  # noqa: E402

PAD_MM = 0.35
POINT_OFFSET_MM = 1.4


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def grown(self, d: float) -> "Rect":
        return Rect(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def hits(self, o: "Rect") -> bool:
        return self.x0 < o.x1 and o.x0 < self.x1 and self.y0 < o.y1 and o.y0 < self.y1

    def inside(self, o: "Rect") -> bool:
        return self.x0 >= o.x0 and self.y0 >= o.y0 and self.x1 <= o.x1 and self.y1 <= o.y1

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class Candidate:
    text: str
    kind: str                       # "area" | "point"
    x: float                        # anchor in mm (area: label center; point: the symbol center)
    y: float
    priority: float = 0.0
    avail_w: float | None = None    # area: width of the feature on the sheet, to skip labels far wider than their shape
    radius: float = 0.0             # point: symbol radius in mm
    fill: str | None = None         # the color under the label, so its text color can be chosen for contrast
    weight: int | None = None       # this label's own weight (a capital is bold where the layer's labels are regular)
    tracking: float = 0.0           # extra letter-spacing in mm (admin names are set loose), counted in the width
    style: dict | None = None       # free-form styling the renderer reads back (tier, slant, colour); carried to Placed untouched


@dataclass
class Placed:
    text: str
    x: float                        # text anchor (start for point labels, middle for area labels)
    y: float                        # baseline
    anchor: str
    rect: Rect
    fill: str | None = None
    style: dict | None = None
    weight: int | None = None


@dataclass
class Result:
    placed: list[Placed] = field(default_factory=list)
    dropped_collision: int = 0
    dropped_size: int = 0
    dropped_outside: int = 0
    dropped_lines: list = field(default_factory=list)      # 1.2.0 (Day 9 issue 6): names left out only because every free position crossed a line

    @property
    def total(self) -> int:
        return len(self.placed) + self.dropped_collision + self.dropped_size + self.dropped_outside

    def as_dict(self) -> dict:
        return {"placed": len(self.placed), "dropped_collision": self.dropped_collision, "dropped_size": self.dropped_size,
                "dropped_outside": self.dropped_outside}


def _text_rect(text: str, size_pt: float, weight: int, cx: float, cy: float, anchor: str, tracking: float = 0.0) -> tuple[Rect, float, float]:
    """Rect for text with its anchor at (cx, cy) as the middle (area) or start (point) of the baseline box."""
    w = tm.width_mm(text, size_pt, weight) + tracking * max(len(text) - 1, 0)
    h = size_pt * tm.PT_MM * 1.0                       # cap-ish height box, tight enough for map labels
    asc = h * 0.78
    if anchor == "middle":
        x0 = cx - w / 2
    elif anchor == "end":
        x0 = cx - w
    else:
        x0 = cx
    return Rect(x0, cy - asc, x0 + w, cy - asc + h * 1.12), x0, cy


MAX_CANDIDATES = 2000
INDEX_CELL_MM = 4.0


class RectIndex:
    """Rectangles filed in a uniform grid of INDEX_CELL_MM cells, so a hit test looks only at the rectangles that share a cell with it (M16).
    The answer is the same as testing every rectangle: two rectangles that overlap share at least one cell. A rectangle with a coordinate
    that is not finite is tested every time."""

    def __init__(self, rects=(), cell: float = INDEX_CELL_MM):
        self.cell = float(cell)
        self.cells: dict = {}
        self.rects: list[Rect] = []
        self.always: list[int] = []
        for r in rects:
            self.add(r)

    @staticmethod
    def _regular(r: Rect) -> bool:
        return all(math.isfinite(v) for v in (r.x0, r.y0, r.x1, r.y1)) and r.x0 <= r.x1 and r.y0 <= r.y1 and (r.x1 - r.x0) < 1e5 and (r.y1 - r.y0) < 1e5

    def _span(self, r: Rect):
        c = self.cell
        return range(math.floor(r.x0 / c), math.floor(r.x1 / c) + 1), range(math.floor(r.y0 / c), math.floor(r.y1 / c) + 1)

    def add(self, r: Rect) -> None:
        i = len(self.rects)
        self.rects.append(r)
        if not self._regular(r):
            self.always.append(i)
            return
        xs, ys = self._span(r)
        for gx in xs:
            for gy in ys:
                self.cells.setdefault((gx, gy), []).append(i)

    def hits(self, r: Rect) -> bool:
        for i in self.always:
            if r.hits(self.rects[i]):
                return True
        if not self._regular(r):
            return any(r.hits(o) for o in self.rects)
        xs, ys = self._span(r)
        seen: set = set()
        for gx in xs:
            for gy in ys:
                for i in self.cells.get((gx, gy), ()):
                    if i not in seen:
                        seen.add(i)
                        if r.hits(self.rects[i]):
                            return True
        return False

    def __iter__(self):
        return iter(self.rects)

    def __len__(self) -> int:
        return len(self.rects)


def place(cands: list[Candidate], frame: Rect, obstacles: list[Rect], size_pt: float, weight: int = 400, min_pt: float = 6.0,
          max_candidates: int = MAX_CANDIDATES, avoid=None) -> Result:
    """Place labels in priority order. Area labels sit centered on the anchor (nudged up to 2 label-heights to fit); point labels try
    right, left, above and below the symbol, then the diagonals. At most `max_candidates` are tried, and every test is against a spatial
    index of what is already taken, so a large layer costs its candidates, not their square (M16)."""
    res = Result()
    if size_pt < min_pt:
        res.dropped_size = len(cands)
        return res
    taken = RectIndex(o.grown(PAD_MM) for o in obstacles)
    for c in sorted(cands, key=lambda c: -c.priority)[:max_candidates]:
        wt = c.weight if c.weight is not None else weight
        w = tm.width_mm(c.text, size_pt, wt) + c.tracking * max(len(c.text) - 1, 0)
        if c.kind == "area" and c.avail_w is not None and w > c.avail_w * 1.6 and c.avail_w < 20:
            res.dropped_size += 1
            continue
        tries: list[tuple[float, float, str]] = []
        if c.kind == "area":
            hh = size_pt * tm.PT_MM
            tries = [(c.x, c.y + hh * 0.35, "middle")] + [(c.x, c.y + hh * (0.35 + d), "middle") for d in (-0.9, 0.9, -1.8, 1.8)]
        else:
            o = c.radius + POINT_OFFSET_MM
            hh = size_pt * tm.PT_MM
            tries = [(c.x + o, c.y + hh * 0.35, "start"), (c.x - o, c.y + hh * 0.35, "end"), (c.x, c.y - o - hh * 0.2, "middle"),
                     (c.x, c.y + o + hh * 0.9, "middle"), (c.x + o * 0.8, c.y - o * 0.8, "start"), (c.x - o * 0.8, c.y - o * 0.8, "end"),
                     (c.x + o * 0.8, c.y + o * 0.8 + hh * 0.6, "start"), (c.x - o * 0.8, c.y + o * 0.8 + hh * 0.6, "end")]
        outside_only = True
        done = False
        line_blocked = False
        for tx, ty, anchor in tries:
            r, x0, base = _text_rect(c.text, size_pt, wt, tx, ty, anchor, c.tracking)
            if not r.inside(frame):
                continue
            outside_only = False
            if taken.hits(r):
                continue
            if avoid is not None and avoid(r):          # 1.0.0: a shape the label may not sit on (a data line, for place names)
                line_blocked = True
                continue
            ax = tx
            res.placed.append(Placed(c.text, ax, base, anchor, r, c.fill, c.style, wt))
            taken.add(r.grown(PAD_MM))
            done = True
            break
        if not done:
            if outside_only:
                res.dropped_outside += 1
            else:
                res.dropped_collision += 1
                if line_blocked:
                    res.dropped_lines.append(c.text)
    return res
