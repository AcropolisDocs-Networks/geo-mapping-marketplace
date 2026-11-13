#!/usr/bin/env python3
"""classify-and-color: classification, number formatting, normalization checks and palette picking (spec 7.3, 12.3b, 12.4).

    classify.py breaks  FILE --field F [--method quantile] [--classes 5] [--manual 1,2,3] [--layer L] [--normalize area_km2] [--midpoint auto|0]
    classify.py palette --spec map.yaml --kind sequential|diverging|categorical --classes 5 [--reverse]
    classify.py check   FILE --field F        (is the field safe to shade as a choropleth?)

--normalize area_km2 (area_mi2, area_m2, area_ha, area_acre) divides the field by each feature's geodesic area, the alternative to a
denominator column; --midpoint auto picks a diverging palette around 0 when the values straddle zero, else around the median.

Classification is pure (numpy only). Breaks are snapped to the decimals the labels show, so a legend label states exactly which
values fall in a class. Class colors are picked from the brand's declared ramps, never interpolated: the palette guard
(MAP-BRAND-001) allowlists declared colors only, so a computed color would fail it.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colorlib as cl  # noqa: E402
import gmap_common as gc  # noqa: E402

METHODS = ("quantile", "equal_interval", "pretty", "geometric", "jenks", "std_dev", "log", "head_tail", "manual", "categorical")
ALIASES = {"equal": "equal_interval", "natural_breaks": "jenks", "natural": "jenks", "stddev": "std_dev", "sd": "std_dev",
           "quantiles": "quantile", "fisher_jenks": "jenks", "unique": "categorical", "categories": "categorical",
           "equal_count": "quantile", "equal_counts": "quantile", "equal_frequency": "quantile", "count": "quantile",
           "nice": "pretty", "rounded": "pretty", "round": "pretty", "even": "pretty", "even_interval": "pretty",
           "geometric_interval": "geometric", "geo": "geometric",
           "log10": "log", "logarithmic": "log", "log_scale": "log", "headtail": "head_tail", "head-tail": "head_tail", "head_tail_breaks": "head_tail",
           "ht": "head_tail"}
# rounding of interior breaks for the data-driven methods: "nice" snaps each break to a round number (1, 2, 2.5, 5 x 10^n) when that
# moves it by no more than a sixth of the class beside it; "none" keeps the exact value; a number snaps to a multiple of that number.
ROUNDING_MODES = ("nice", "none")
DEFAULT_ROUNDING = "nice"
NICE_MULTIPLES = (1.0, 2.0, 2.5, 5.0, 10.0)
MIN_CLASSES, MAX_CLASSES = 3, 7
MAX_CATEGORIES = 8
JENKS_MAX_POINTS = 1500
HEAD_TAIL_SHARE = 0.4          # head/tail breaks stop when the head (values above the mean) is this share of what is left or more (Jiang 2013)
LOG_LADDER = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5)   # round numbers per decade a log break may snap to
LOG_SNAP_DECADES = 0.07        # ...when that moves it by no more than this many decades (about 17 percent)


class ClassifyError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------------------------- number formatting
@dataclass
class NumberFormat:
    decimals: int | None = None          # None = chosen from the breaks
    prefix: str = ""                      # "$"
    suffix: str = ""                      # "%" or " sites"
    thousands: bool = True
    compact: bool = False                 # 1.2M, 340k

    def fmt(self, v: float, decimals: int | None = None) -> str:
        d = self.decimals if decimals is None else decimals
        d = 0 if d is None else d
        if self.compact and abs(v) >= 1000:
            for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
                if abs(v) >= div:
                    return f"{self.prefix}{_trim(v / div, max(d, 0) if d else 1)}{unit}{self.suffix}"
        s = f"{v:,.{d}f}" if self.thousands else f"{v:.{d}f}"
        if s.startswith("-") and float(s.replace(",", "")) == 0:
            s = s[1:]                     # never "-0.0"
        sign = "-" if s.startswith("-") else ""
        return f"{sign}{self.prefix}{s.lstrip('-')}{self.suffix}"


def _trim(x: float, d: int) -> str:
    s = f"{x:.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def number_format(spec: dict | None) -> NumberFormat:
    spec = spec or {}
    return NumberFormat(decimals=spec.get("decimals"), prefix=spec.get("prefix", ""), suffix=spec.get("suffix", ""),
                        thousands=spec.get("thousands", True), compact=spec.get("compact", False))


def _decimals_exact(edges: list[float], cap: int = 4) -> int:
    """The fewest decimals that show every edge exactly (round-number breaks: 0.5, 1, 1.5 need one; 25, 50 need none)."""
    for d in range(0, cap + 1):
        if all(abs(round(e, d) - e) < 1e-9 for e in edges):
            return d
    return cap


def _decimals_needed(edges: list[float], cap: int = 4) -> int:
    """The fewest decimals at which the interior edges stay distinct and ordered."""
    for d in range(0, cap + 1):
        r = [round(e, d) for e in edges]
        if all(r[i] < r[i + 1] for i in range(len(r) - 1)):
            return d
    return cap


# ---------------------------------------------------------------------------------------------------------------- results
@dataclass
class Classification:
    method: str
    kind: str                            # "numeric" | "categorical"
    edges: list[float] = field(default_factory=list)       # k+1 numeric edges (class i is [e_i, e_i+1); the last is closed)
    categories: list[str] = field(default_factory=list)    # categorical: class i is categories[i]; the last may be "Other"
    labels: list[str] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    assigned: np.ndarray | None = None                     # per input value: class index, or -1 for no data
    decimals: int = 0
    integer_data: bool = False
    nodata: int = 0
    notes: list[str] = field(default_factory=list)
    fixed: bool = False                                    # edges were given (shared across an atlas), not derived from these values

    @property
    def k(self) -> int:
        return len(self.labels)

    def as_dict(self) -> dict:
        return {"method": self.method, "kind": self.kind, "classes": self.k, "edges": self.edges, "categories": self.categories,
                "labels": self.labels, "counts": self.counts, "nodata": self.nodata, "decimals": self.decimals, "notes": self.notes}


# ---------------------------------------------------------------------------------------------------------------- algorithms
def _quantile_edges(x: np.ndarray, k: int) -> list[float]:
    return [float(v) for v in np.quantile(x, np.linspace(0, 1, k + 1))]


def _equal_edges(x: np.ndarray, k: int) -> list[float]:
    return [float(v) for v in np.linspace(x.min(), x.max(), k + 1)]


def _std_edges(x: np.ndarray, k: int) -> list[float]:
    mu, sd = float(x.mean()), float(x.std())
    if sd == 0:
        return [float(x.min()), float(x.max())]
    if k % 2:                                              # odd: the middle class is mean +- 0.5 sd
        half = (k - 1) // 2
        inner = [(-half + 0.5) + i for i in range(k - 1)]
    else:                                                  # even: a break at the mean
        half = k // 2
        inner = [float(i) for i in range(-half + 1, half)]
    edges = [mu + s * sd for s in inner]
    return [float(x.min())] + [e for e in edges if x.min() < e < x.max()] + [float(x.max())]


def _jenks_edges(x: np.ndarray, k: int) -> list[float]:
    """Fisher-Jenks optimal breaks, on a weighted, capped sample so it stays fast and deterministic."""
    vals, counts = np.unique(x, return_counts=True)
    if len(vals) > JENKS_MAX_POINTS:                       # pre-bin the distinct values by quantile, keep weights
        idx = np.unique(np.linspace(0, len(vals) - 1, JENKS_MAX_POINTS).astype(int))
        bins = np.searchsorted(idx, np.arange(len(vals)), side="right") - 1
        w = np.bincount(bins, weights=counts)
        v = np.bincount(bins, weights=vals * counts) / w
        vals, counts = v, w
    n = len(vals)
    if n <= k:
        return [float(vals[0])] + [float(v) for v in vals[1:]]
    w = counts.astype(float)
    cw = np.concatenate([[0.0], np.cumsum(w)])
    cs = np.concatenate([[0.0], np.cumsum(w * vals)])
    cs2 = np.concatenate([[0.0], np.cumsum(w * vals * vals)])

    def ssd(i: np.ndarray | int, j: int):                  # sum of squared deviations of vals[i:j]
        sw = cw[j] - cw[i]
        s = cs[j] - cs[i]
        return (cs2[j] - cs2[i]) - s * s / sw

    inf = float("inf")
    dp = np.full((k + 1, n + 1), inf)
    back = np.zeros((k + 1, n + 1), dtype=int)
    for j in range(1, n + 1):
        dp[1, j] = ssd(0, j)
    for c in range(2, k + 1):
        for j in range(c, n + 1):
            i = np.arange(c - 1, j)
            cost = dp[c - 1, i] + ssd(i, j)
            m = int(np.argmin(cost))
            dp[c, j] = cost[m]
            back[c, j] = int(i[m])
    cuts, j = [], n
    for c in range(k, 1, -1):
        j = back[c, j]
        cuts.append(j)
    cuts = sorted(cuts)
    edges = [float(vals[0])] + [float(vals[c]) for c in cuts] + [float(x.max())]
    return edges


def nice_number(x: float, mode: str = "round") -> float:
    """A round number (1, 2, 5 or 10 times a power of ten) near x. mode: round | floor | ceil."""
    if x <= 0 or not math.isfinite(x):
        return x
    e = math.floor(math.log10(x))
    f = x / 10 ** e
    steps = (1.0, 2.0, 5.0, 10.0)
    if mode == "floor":
        n = max(c for c in steps if c <= f + 1e-12)
    elif mode == "ceil":
        n = min(c for c in steps if c >= f - 1e-12)
    else:
        n = 1.0 if f < 1.5 else 2.0 if f < 3.0 else 5.0 if f < 7.0 else 10.0
    return n * 10 ** e


def _pretty_edges(x: np.ndarray, k: int) -> list[float]:
    """Equal-width classes on round numbers (an axis that 'could be the size'): the step is 1, 2, 2.5 or 5 x 10^n, the first and last edge are
    the round numbers at or beyond the data's minimum and maximum, and the step is the one whose class count lands nearest k (3-7 classes)."""
    lo, hi = float(x.min()), float(x.max())
    base = (hi - lo) / k
    best = None
    for e in range(math.floor(math.log10(base)) - 1, math.floor(math.log10(base)) + 2):
        for m in (1.0, 2.0, 2.5, 5.0):
            step = m * 10 ** e
            a0, b0 = math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step
            n = int(round((b0 - a0) / step))
            if not (MIN_CLASSES <= n <= MAX_CLASSES):
                continue
            key = (abs(n - k), -step)                       # nearest class count; the larger step when tied
            if best is None or key < best[0]:
                best = (key, a0, step, n)
    if best is None:                                        # a tiny range (all but one value alike): fall back to one nice step
        step = nice_number(base, "round")
        a0 = math.floor(lo / step + 1e-9) * step
        n = max(int(round((math.ceil(hi / step - 1e-9) * step - a0) / step)), 1)
        return [round(a0 + i * step, 10) for i in range(n + 1)]
    _, a0, step, n = best
    return [round(a0 + i * step, 10) for i in range(n + 1)]


def _geometric_edges(x: np.ndarray, k: int) -> list[float]:
    """Each class is a constant multiple wider than the one below it; suits strongly right-skewed counts (sites per county, users per cell)."""
    lo, hi = float(x.min()), float(x.max())
    if lo > 0:
        return [float(v) for v in np.geomspace(lo, hi, k + 1)]
    pos = x[x > 0]
    if pos.size == 0:
        return _equal_edges(x, k)
    s0 = float(pos.min())                                  # zero or negative values: build the series on the values shifted to start at the smallest positive one
    return [float(v) + lo - s0 for v in np.geomspace(s0, hi - lo + s0, k + 1)]


def _log_edges(x: np.ndarray, k: int) -> list[float]:
    """Classes of equal width on a log scale (each class spans the same ratio), for positive data that runs over several orders of magnitude.
    The interior breaks snap to a round number of their decade (1, 1.5, 2, 2.5, 3, 4, 5, 7.5 x 10^n) when that moves them less than
    LOG_SNAP_DECADES. Zero or negative values are refused, not shifted (the geometric method shifts them)."""
    if x.min() <= 0:
        n = int((x <= 0).sum())
        raise ClassifyError(f"log classes need values above zero; {n} value(s) are zero or negative. Use geometric (it shifts the series), "
                            "quantile, or filter the zeros out first")
    lo, hi = float(x.min()), float(x.max())
    raw = list(np.geomspace(lo, hi, k + 1))
    out = [lo]
    for e in raw[1:-1]:
        d = math.floor(math.log10(e))
        cands = [m * 10 ** dd for dd in (d - 1, d, d + 1) for m in LOG_LADDER]
        c = min(cands, key=lambda v: abs(math.log10(v) - math.log10(e)))
        out.append(float(c) if abs(math.log10(c) - math.log10(e)) <= LOG_SNAP_DECADES and out[-1] < c < hi else float(e))
    return out + [hi]


def _head_tail_edges(x: np.ndarray, k: int) -> list[float]:
    """Head/tail breaks (Jiang 2013) for heavy-tailed data (a few very large values, many small ones): split at the mean, keep the head (the
    values above it) and split it again at its own mean, while the head stays under HEAD_TAIL_SHARE of what it was cut from and there are
    classes left to make. The class count follows the data; `k` is the most it may reach."""
    edges = [float(x.min())]
    cur = x
    while len(edges) < k:
        m = float(cur.mean())
        head = cur[cur > m]
        if head.size == 0 or head.size == cur.size or m <= edges[-1]:
            break
        edges.append(m)
        if head.size / cur.size >= HEAD_TAIL_SHARE or head.size < 2:
            break
        cur = head
    return edges + [float(x.max())]


def _snap_edges(edges: list[float], rounding) -> list[float]:
    """Round the interior breaks of a data-driven classification to numbers a reader expects. The outer edges stay the data's own range."""
    if rounding in (None, "none", False) or len(edges) < 3:
        return edges
    out = list(edges)
    for i in range(1, len(edges) - 1):
        e = edges[i]
        left, right = edges[i] - edges[i - 1], edges[i + 1] - edges[i]
        width = min(left, right) if min(left, right) > 0 else max(left, right)
        if width <= 0:
            continue
        if isinstance(rounding, (int, float)) and not isinstance(rounding, bool):
            step = float(rounding)
            cand = round(e / step) * step
        else:
            mag = 10 ** math.floor(math.log10(width))
            tol = width / 6.0
            cand = e
            for m in (10.0, 5.0, 2.5, 2.0, 1.0, 0.5, 0.25, 0.1):          # coarsest round number that does not move the break far
                c = round(e / (m * mag)) * m * mag
                if abs(c - e) <= tol:
                    cand = c
                    break
        out[i] = round(cand, 10)
    for i in range(1, len(out) - 1):                       # a snap must keep the breaks inside the data range and in order; otherwise keep the exact one
        if not (out[i - 1] < out[i] < edges[-1]):
            out[i] = edges[i] if edges[i] > out[i - 1] else out[i - 1]
    return out


def _dedupe(edges: list[float]) -> list[float]:
    out = [edges[0]]
    for e in edges[1:]:
        if e > out[-1]:
            out.append(e)
    return out


# ---------------------------------------------------------------------------------------------------------------- numeric classification
def classify_numeric(values, method: str = "quantile", classes: int = 5, manual_breaks=None, fmt: NumberFormat | None = None,
                     label_style: str = "auto", rounding=DEFAULT_ROUNDING) -> Classification:
    method = ALIASES.get(method, method)
    if method not in METHODS or method == "categorical":
        raise ClassifyError(f"unknown numeric method '{method}' (use one of {', '.join(m for m in METHODS if m != 'categorical')})")
    fmt = fmt or NumberFormat()
    arr = np.asarray(values, dtype="float64")
    ok = np.isfinite(arr)
    x = arr[ok]
    res = Classification(method=method, kind="numeric", nodata=int((~ok).sum()))
    if x.size == 0:
        raise ClassifyError("no numeric values to classify")
    k = int(classes)
    if method != "manual" and not (MIN_CLASSES <= k <= MAX_CLASSES):
        res.notes.append(f"class count {k} is outside {MIN_CLASSES}-{MAX_CLASSES}; using {min(max(k, MIN_CLASSES), MAX_CLASSES)}")
        k = min(max(k, MIN_CLASSES), MAX_CLASSES)
    nuniq = int(np.unique(x).size)
    top_single = False
    if nuniq == 1:
        res.notes.append("every value is the same; one class")
        edges = [float(x[0]), float(x[0])]
    elif method == "manual":
        if not manual_breaks:
            raise ClassifyError("manual classification needs breaks")
        mb = sorted(float(b) for b in manual_breaks)
        if len(set(mb)) != len(mb):
            raise ClassifyError("manual breaks must be unique")
        edges = [min(float(x.min()), mb[0])] + [b for b in mb if x.min() < b < x.max()] + [max(float(x.max()), mb[-1])]
        edges = _dedupe(edges)
        if len(edges) < len(mb) + 2:
            res.notes.append("some manual breaks fall outside the data range and were dropped")
    else:
        if nuniq < k:
            res.notes.append(f"only {nuniq} distinct values; using {nuniq} classes")
            k = max(nuniq, 2)
        edges = {"quantile": _quantile_edges, "equal_interval": _equal_edges, "pretty": _pretty_edges, "geometric": _geometric_edges,
                 "jenks": _jenks_edges, "std_dev": _std_edges, "log": _log_edges, "head_tail": _head_tail_edges}[method](x, k)
        if method == "head_tail" and len(edges) - 1 < k:
            res.notes.append(f"head/tail breaks found {len(edges) - 1} classes (the head reached {HEAD_TAIL_SHARE:.0%} of the values or ran out); "
                             "the data is less heavy-tailed than this method expects" if len(edges) - 1 < MIN_CLASSES else
                             f"head/tail breaks found {len(edges) - 1} classes; the class count follows the data's tail")
        before = len(edges) - 1
        top_single = method == "jenks" and len(edges) >= 3 and edges[-1] == edges[-2]    # Jenks may isolate the single highest value
        if top_single:
            edges = edges[:-1]
        edges = _dedupe(edges)
        if top_single and len(edges) >= 3:
            res.notes.append("the highest value forms a class of its own")
        else:
            top_single = False
        if method not in ("pretty", "std_dev", "log") and not top_single and rounding not in (None, "none", False) and len(edges) > 2:
            snapped_nice = _dedupe(_snap_edges(edges, rounding))
            if snapped_nice != edges:
                edges = snapped_nice
                res.notes.append("breaks are rounded to round numbers" + ("" if method in ("equal_interval", "geometric") else "; class sizes are close to, not exactly, the method's"))
        if method == "std_dev":
            pass
        elif method == "head_tail":
            pass
        elif len(edges) - 1 + (1 if top_single else 0) < before:
            res.notes.append(f"tied values collapsed {before} classes to {len(edges) - 1}")
    res.integer_data = bool(np.all(x == np.round(x)))
    span = max(float(x.max() - x.min()), 1e-12)
    if fmt.decimals is not None:
        d = fmt.decimals
    elif res.integer_data:
        d = 0
    elif method == "pretty" or any("rounded to round" in n for n in res.notes):
        d = _decimals_exact(edges if method == "pretty" else edges[1:-1])
    else:                                    # fine enough that rounding moves a break by under a hundredth of a class width
        d = max(_decimals_needed(edges), min(4, max(0, math.ceil(-math.log10(span / (max(len(edges) - 1, 1) * 100))))))
    # Snap the interior edges to the shown precision so a label states exactly which values it holds.
    # Whole-number data: ceil keeps every value in the class it was already in.
    if len(edges) > 2:
        interior = [math.ceil(e - 1e-9) if (res.integer_data and d == 0) else round(e, d) for e in edges[1:-1]]
        snapped = _dedupe([edges[0]] + interior + [edges[-1]])
        if len(snapped) < len(edges):
            res.notes.append("breaks that rounded to the same value were merged")
        edges = snapped
    if top_single:
        edges = edges + [edges[-1]]
    res.edges, res.decimals = edges, d
    res.assigned = np.full(arr.shape, -1, dtype=int)
    kk = len(edges) - 1
    res.assigned[ok] = np.clip(np.searchsorted(np.array(edges[1:-1]), x, side="right"), 0, max(kk - 1, 0)) if kk > 1 else 0
    res.counts = [int((res.assigned == i).sum()) for i in range(kk)]
    empty = [i + 1 for i, c in enumerate(res.counts) if c == 0]
    if empty:
        res.notes.append(f"class {', '.join(map(str, empty))} holds no features")
    res.labels = range_labels(edges, d, fmt, res.integer_data, label_style)
    return res


def classify_fixed(values, edges, decimals: int = 0, fmt: NumberFormat | None = None, integer_data: bool = False, label_style: str = "auto", method: str = "fixed") -> Classification:
    """Classes whose edges are given, not derived from these values: the same edges, labels and class count in every map of an atlas (MAP-CART-014).
    A value below the first edge or above the last falls in the end class; a class that holds nothing stays in the legend."""
    fmt = fmt or NumberFormat()
    e = [float(x) for x in edges]
    if len(e) < 3 or any(b <= a for a, b in zip(e, e[1:])):
        raise ClassifyError("fixed edges need at least three increasing values")
    arr = np.asarray(values, dtype="float64")
    ok = np.isfinite(arr)
    res = Classification(method=method, kind="numeric", nodata=int((~ok).sum()))
    res.edges, res.decimals, res.integer_data, res.fixed = e, int(decimals), bool(integer_data), True
    kk = len(e) - 1
    res.assigned = np.full(arr.shape, -1, dtype=int)
    res.assigned[ok] = np.clip(np.searchsorted(np.array(e[1:-1]), arr[ok], side="right"), 0, kk - 1)
    res.counts = [int((res.assigned == i).sum()) for i in range(kk)]
    res.labels = range_labels(e, res.decimals, fmt, res.integer_data, label_style)
    return res


HALF_OPEN_NOTE = "class ranges include their lower value and stop below the next class's lower value"


def classic_labels(labels: list[str], integer_style: bool = False) -> list[str]:
    """The 0.16 wording for the classic look: 'a to under b' for the inner continuous classes, 'a – b' (spaced) for the last one and for
    integer classes; 'Under b' stays."""
    out = []
    for i, x in enumerate(labels):
        parts = re.split(r"\s?–\s?", x, maxsplit=1)
        if len(parts) != 2 or len(labels) == 1:
            out.append(x)
        elif integer_style or i == len(labels) - 1:
            out.append(f"{parts[0]} – {parts[1]}")
        else:
            out.append(f"{parts[0]} to under {parts[1]}")
    return out


def dash(a: str, b: str) -> str:
    """A range in the map-text style (1.13.0, Day 12 issue 4): an en dash with no spaces ('5–8'); spaced (' – ') when either end has a space
    or a sign of its own, so '-5 – 0' and '1.2 M – 3 M' still read as two numbers."""
    tight = not any(c in x for x in (a, b) for c in (" ", "-", "−", "+"))
    return f"{a}–{b}" if tight else f"{a} – {b}"


def range_labels(edges: list[float], d: int, fmt: NumberFormat, integer_data: bool, style: str = "auto") -> list[str]:
    """Labels that never overlap. Integer data: 'a–b' with b = next edge - 1. Continuous data: 'Under b', 'a–b', ..., 'a–b' (1.2.0, Day 3:
    no more 'a to under b'; the classes are still half-open, which the methods note says once). 1.13.0 (Day 12 issue 4): every range is
    written the one way the map-text rule asks, an en dash ('5–8'), where 1.12 mixed '5 to 8' with '11 – 17' in one legend. The classic look
    keeps the old words (map_plan.classic_range_labels)."""
    k = len(edges) - 1
    if k == 1 and edges[0] == edges[1]:
        return [fmt.fmt(edges[0], d)]
    style = ("integer" if integer_data and d == 0 else "continuous") if style == "auto" else style
    out = []
    for i in range(k):
        lo, hi = edges[i], edges[i + 1]
        last = i == k - 1
        if style == "integer":
            top = hi if last else hi - 1
            out.append(fmt.fmt(lo, 0) if lo == top else dash(fmt.fmt(lo, 0), fmt.fmt(top, 0)))
        else:
            if last and lo == hi:
                out.append(fmt.fmt(lo, d))
            elif last:
                out.append(dash(fmt.fmt(lo, d), fmt.fmt(hi, d)))
            elif i == 0:
                out.append(f"Under {fmt.fmt(hi, d)}")
            else:
                out.append(dash(fmt.fmt(lo, d), fmt.fmt(hi, d)))
    return out


# ---------------------------------------------------------------------------------------------------------------- categorical
def classify_categorical(values, max_categories: int = MAX_CATEGORIES, order=None, other_label: str = "Other", keep_absent: bool = False) -> Classification:
    """Most frequent categories first (or the given order); the rest become one 'Other' class so colors stay distinguishable.
    keep_absent (0.24.0): every category of `order` keeps its place and colour even when no feature here has it (an atlas page)."""
    s = ["" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v) for v in values]
    present = [v for v in s if v != ""]
    res = Classification(method="categorical", kind="categorical", nodata=len(s) - len(present))
    if not present:
        raise ClassifyError("no category values to classify")
    freq: dict[str, int] = {}
    for v in present:
        freq[v] = freq.get(v, 0) + 1
    ranked = [c for c in (order or []) if c in freq or keep_absent] + sorted((c for c in freq if c not in (order or [])), key=lambda c: (-freq[c], c))
    if len(ranked) > max_categories:
        keep = ranked[:max_categories - 1]
        res.notes.append(f"{len(ranked)} categories; the {len(ranked) - len(keep)} smallest are grouped as '{other_label}'")
        res.categories = keep + [other_label]
    else:
        res.categories = ranked
    idx = {c: i for i, c in enumerate(res.categories)}
    other = idx.get(other_label)
    res.assigned = np.array([-1 if v == "" else idx.get(v, other if other is not None else -1) for v in s], dtype=int)
    res.labels = list(res.categories)
    res.counts = [int((res.assigned == i).sum()) for i in range(len(res.categories))]
    return res


def classify(values, method="quantile", classes=5, manual_breaks=None, fmt: NumberFormat | None = None, **kw) -> Classification:
    m = ALIASES.get(method, method)
    if m == "categorical":
        return classify_categorical(values, kw.get("max_categories", MAX_CATEGORIES), kw.get("order"), kw.get("other_label", "Other"), bool(kw.get("keep_absent")))
    return classify_numeric(values, m, classes, manual_breaks, fmt, kw.get("label_style", "auto"), kw.get("rounding", DEFAULT_ROUNDING))


# ---------------------------------------------------------------------------------------------------------------- normalization
RAW_COUNT_HINT = re.compile(r"(^|[_\s])(count|cnt|total|num|number|n|pop|population|sites|towers|sum|households|hh|jobs|employees)([_\s]|$)", re.I)
RATE_HINT = re.compile(r"(per|pct|percent|ratio|rate|share|density|avg|average|mean|median|index|score|_pc$|per_)", re.I)


def normalization_check(name: str, values, areas=None, populations=None) -> dict:
    """MAP-CART-001: is this field a raw count that would make big units look 'high' just by being big?

    Returns {raw_count_suspected, reasons, suggest}. It is a heuristic: a field named like a rate passes, a non-negative integer
    field named like a count over units of unequal size fails, and a field that tracks unit area closely fails whatever it is called.
    """
    x = np.asarray(values, dtype="float64")
    x = x[np.isfinite(x)]
    reasons: list[str] = []
    named_rate = bool(RATE_HINT.search(name))
    named_count = bool(RAW_COUNT_HINT.search(name))
    integer = x.size > 0 and bool(np.all(x == np.round(x))) and bool(np.all(x >= 0))
    if integer and named_count and not named_rate:
        reasons.append(f"'{name}' reads like a count and holds whole numbers")
    elif integer and not named_rate and x.size > 5 and x.max() > 50:
        reasons.append(f"'{name}' holds whole numbers with a range up to {int(x.max()):,}; counts are common here")
    if areas is not None:
        a = np.asarray(areas, dtype="float64")
        if a.shape == np.asarray(values).shape:
            m = np.isfinite(np.asarray(values, dtype="float64")) & np.isfinite(a) & (a > 0)
            if m.sum() > 5:
                r = np.corrcoef(np.log1p(np.asarray(values, dtype="float64")[m].clip(min=0)), np.log(a[m]))[0, 1]
                if np.isfinite(r) and r > 0.6 and not named_rate:
                    reasons.append(f"the values rise with unit area (log correlation {r:.2f})")
    spread = (a.max() / a.min()) if areas is not None and np.isfinite(np.asarray(areas, dtype=float)).all() and np.min(areas) > 0 else None
    suspected = bool(reasons) and not named_rate
    return {"raw_count_suspected": suspected, "reasons": reasons, "area_spread": None if spread is None else float(spread),
            "suggest": "divide by population, households or area (for example sites per 100,000 people or per sq mi)" if suspected else None}


# ---------------------------------------------------------------------------------------------------------------- palettes
def ramp_pick(ramp: list[str], n: int, reverse: bool = False) -> list[str]:
    """n evenly spaced steps picked from a declared ramp (never interpolated; spec 11.3)."""
    if not (1 <= n <= len(ramp)):
        raise ClassifyError(f"cannot pick {n} classes from a {len(ramp)}-step ramp")
    if n == 1:
        idx = [len(ramp) // 2]
    else:
        idx = [int(round(i * (len(ramp) - 1) / (n - 1))) for i in range(n)]
    out = [ramp[i] for i in idx]
    return out[::-1] if reverse else out


def palette(tokens: dict, kind: str, n: int, reverse: bool = False, anchor: str | None = None) -> dict:
    """Class colors from a mode's tokens. kind: sequential | diverging | categorical | neutral | status.

    0.17.0: under the `auto` look (map_look) sequential and diverging classes are generated for exactly n classes from a brand colour (the
    emphasis colour, or `anchor`), and categorical colours are handed out in map_look's order; under `classic` the brand ramps are picked from."""
    import map_look
    gen = map_look.class_colors(tokens, kind, n, anchor) if kind in ("sequential", "diverging") else None
    if gen and tokens.get("_chroma_cap") is not None:          # 1.4.0: a brand-derived style pack's chroma cap (monochrome stays grey)
        import map_style
        gen = [map_style.cap_color(c, tokens["_chroma_cap"]) for c in gen]
    if gen is not None:
        cols = gen[::-1] if reverse else gen
    elif kind == "sequential":
        cols = ramp_pick(tokens["sequential_ramp"], n, reverse)
    elif kind == "diverging":
        cols = ramp_pick(tokens["diverging_ramp"], n, reverse)
    elif kind == "neutral":
        cols = ramp_pick(tokens["neutral_ramp"], n, reverse)
    elif kind == "categorical":
        cat = tokens["categorical"]
        if n > len(cat):
            raise ClassifyError(f"{n} categories exceed the {len(cat)} distinguishable colors; group the smallest as Other")
        cols = map_look.categorical_pick(tokens, n)
    else:
        raise ClassifyError(f"unknown palette kind '{kind}'")
    return {"kind": kind, "colors": cols, "no_data": tokens.get("no_data"), "reverse": reverse}


def diverging_midpoint(values, midpoint=None) -> float | None:
    """The midpoint a diverging palette turns on, or None for a sequential one.

    A number is taken as given. `auto` (or nothing, when the values straddle zero) works it out: 0 when the values run from negative to
    positive (a change, a difference from plan, a net add), else the median (a deviation from a typical value). Nothing is diverging on
    its own when the values are all one sign and no midpoint was asked for: a count or a rate reads as a sequence."""
    if midpoint is not None and not (isinstance(midpoint, str) and midpoint.strip().lower() == "auto"):
        return float(midpoint)
    if values is None:
        return None if midpoint is None else 0.0
    x = np.asarray(values, dtype="float64")
    x = x[np.isfinite(x)]
    if x.size == 0:
        return None
    straddles = bool(x.min() < 0 < x.max())
    if straddles:
        return 0.0
    if midpoint is None:
        return None
    return float(np.median(x))


def choose_kind(method: str, midpoint=None, values=None) -> str:
    """sequential | diverging | categorical for a classification. `midpoint` is the spec's `classification.midpoint` (a number, `auto`,
    or absent); `values` lets an absent midpoint still pick diverging when the values straddle zero (see diverging_midpoint)."""
    if ALIASES.get(method, method) == "categorical":
        return "categorical"
    if midpoint is not None and not (isinstance(midpoint, str) and midpoint.strip().lower() == "auto"):
        return "diverging"
    return "diverging" if diverging_midpoint(values, midpoint) is not None else "sequential"


# ---------------------------------------------------------------------------------------------------------------- area normalization
AREA_UNITS = {"area_km2": ("km2", 1e-6, "per km²"), "area_m2": ("m2", 1.0, "per m²"), "area_mi2": ("mi2", 1 / 2_589_988.110336, "per sq mi"),
              "area_ha": ("ha", 1e-4, "per hectare"), "area_acre": ("acre", 1 / 4046.8564224, "per acre")}


def geodesic_area(gdf):
    """Each feature's area in square metres on the ellipsoid (pyproj Geod, WGS 84), so the figure does not depend on the display projection.
    Data in a projected CRS is taken back to degrees first; a layer with no CRS cannot be measured."""
    from pyproj import Geod
    if gdf.crs is None:
        raise ClassifyError("the layer has no CRS, so its areas cannot be measured; state the CRS first")
    g = gdf if gdf.crs.is_geographic else gdf.to_crs("EPSG:4326")
    geod = Geod(ellps="WGS84")
    out = np.full(len(g), np.nan)
    for i, geom in enumerate(g.geometry):
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        out[i] = abs(geod.geometry_area_perimeter(geom)[0])
    return out


def normalize_by_area(gdf, field: str, unit: str = "area_km2"):
    """value / area, as a numpy array aligned with the frame: `normalize: area_km2` (or area_mi2, area_m2, area_ha, area_acre) in place of a
    denominator column. The area is geodesic (see geodesic_area), so a county in Alaska and one in Florida are measured the same way.
    Returns (values, info) where info names the unit and the label suffix for the legend ("per km²")."""
    if unit not in AREA_UNITS:
        raise ClassifyError(f"unknown area unit '{unit}'; use one of {', '.join(AREA_UNITS)}")
    if field not in gdf.columns:
        raise ClassifyError(f"no field '{field}' to normalize")
    key, factor, suffix = AREA_UNITS[unit]
    area = geodesic_area(gdf) * factor
    vals = np.asarray(gdf[field].to_numpy(dtype="float64", na_value=np.nan), dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(area > 0, vals / area, np.nan)
    n_bad = int(np.sum(np.isfinite(vals) & ~(area > 0)))
    return out, {"unit": key, "suffix": suffix, "no_area": n_bad, "note": f"{field} divided by each feature's geodesic area in {key}" + (f"; {n_bad} feature(s) have no measurable area and are No data" if n_bad else "")}


def palette_report(colors: list[str], ground: str) -> dict:
    """Adjacent-class distinctness under normal vision and the three color-vision deficiencies, and contrast with the ground."""
    pairs = []
    for a, b in zip(colors, colors[1:]):
        pairs.append({"normal": round(cl.delta_e(a, b), 1), "min_cvd": round(cl.cvd_min_delta_e(a, b), 1)})
    return {"adjacent": pairs, "min_normal": min((p["normal"] for p in pairs), default=None),
            "min_cvd": min((p["min_cvd"] for p in pairs), default=None),
            "contrast_with_ground": [round(cl.contrast(c, ground), 2) for c in colors]}


def text_on(fill: str, dark: str, light: str) -> str:
    """The label color (a declared token) that reads better on this fill."""
    return dark if cl.contrast(dark, fill) >= cl.contrast(light, fill) else light


# ---------------------------------------------------------------------------------------------------------------- CLI
def _load(path: str, layer, field_name: str):
    import geo_io
    r = geo_io.read(path, layer=layer)
    if field_name not in r.gdf.columns:
        raise SystemExit(f"no field '{field_name}'; fields: {', '.join(c for c in r.gdf.columns if c != r.gdf.geometry.name)}")
    return r.gdf


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("breaks")
    b.add_argument("file")
    b.add_argument("--field", required=True)
    b.add_argument("--layer")
    b.add_argument("--method", default="quantile")
    b.add_argument("--classes", type=int, default=5)
    b.add_argument("--manual")
    b.add_argument("--decimals", type=int)
    b.add_argument("--rounding", default=DEFAULT_ROUNDING, help="nice (default), none, or a number to snap breaks to a multiple of")
    b.add_argument("--prefix", default="")
    b.add_argument("--suffix", default="")
    b.add_argument("--normalize", choices=list(AREA_UNITS), help="divide the field by each feature's geodesic area")
    b.add_argument("--midpoint", help="a number, or auto: the value a diverging palette turns on")
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--field", required=True)
    c.add_argument("--layer")
    p = sub.add_parser("palette")
    p.add_argument("--spec", required=True)
    p.add_argument("--kind", default="sequential")
    p.add_argument("--classes", type=int, default=5)
    p.add_argument("--mode", default="print")
    p.add_argument("--reverse", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "breaks":
            g = _load(a.file, a.layer, a.field)
            manual = [float(v) for v in a.manual.split(",")] if a.manual else None
            categorical = ALIASES.get(a.method, a.method) == "categorical"
            info = None
            if a.normalize and not categorical:
                vals, info = normalize_by_area(g, a.field, a.normalize)
            else:
                vals = g[a.field].tolist() if categorical else g[a.field].to_numpy(dtype="float64", na_value=np.nan)
            res = classify(vals, a.method, a.classes, manual, NumberFormat(a.decimals, a.prefix, a.suffix or (f" {info['suffix']}" if info else "")),
                           rounding=(float(a.rounding) if a.rounding.replace(".", "", 1).isdigit() else a.rounding))
            rep = res.as_dict()
            if info:
                rep["normalization"] = info
                res.notes.append(info["note"])
                rep["notes"] = res.notes
            if not categorical:
                mid = diverging_midpoint(vals, a.midpoint)
                rep["palette_kind"] = choose_kind(a.method, a.midpoint, vals)
                rep["midpoint"] = mid
            print(json.dumps(rep, indent=2))
        elif a.cmd == "check":
            g = _load(a.file, a.layer, a.field)
            areas = None
            try:
                import crs_engine as ce
                areas = g.to_crs(ce.choose_display(g, "thematic")["crs"]).area.to_numpy() if g.crs is not None else None
            except Exception:  # noqa: BLE001
                pass
            print(json.dumps(normalization_check(a.field, g[a.field].to_numpy(dtype="float64", na_value=np.nan), areas), indent=2))
        else:
            import brandlib as bl
            spec = gc.load_mapspec(Path(a.spec)) or {}
            slug = spec.get("brand") or bl.resolve(gc.build_manifest(), None, None)["brand"]
            toks = bl.load_pack(slug)["tokens"]["modes"][a.mode]
            out = palette(toks, a.kind, a.classes, a.reverse)
            out["report"] = palette_report(out["colors"], toks["background"])
            print(json.dumps(out, indent=2))
    except (ClassifyError, FileNotFoundError) as e:
        print(f"classify: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
