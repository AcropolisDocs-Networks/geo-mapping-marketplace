"""timekeys: read a layer's `time_field` as periods (gap P2-2, M15): dates, years, months, quarters or named periods, in order.

    periods(values, unit="auto") -> Periods(keys, unit, order, labels, unreadable)

`keys[i]` is the period of row i (None when it cannot be read); `order` lists the periods from first to last; `labels` gives each period's
reader-facing name ("March 2026", "Q1 2026", "2026", "March 15, 2026", or the value itself for a named period). Keys sort as text in time
order (2026-03, 2026-Q1, 2026), so "up to this period" is `key <= period`.

    unit auto     whole numbers 1800 to 2200 are years; YYYY-MM is a month; YYYY-Qn (or Qn YYYY) a quarter; YYYY a year; anything else
                  pandas reads as a date is a date, grouped by month when the dates span more than one month, else by day; what is left
                  are named periods ("Phase 1", "Wave 2"), in natural order (or the order the spec gives)
    unit year | quarter | month | day   group dates (or months, quarters) into that unit
"""
from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, field

UNITS = ("auto", "year", "quarter", "month", "day")
_MONTH = re.compile(r"^\s*(\d{4})[-/](\d{1,2})\s*$")
_QUARTER = re.compile(r"^\s*(\d{4})\s*[-/ ]?\s*Q([1-4])\s*$|^\s*Q([1-4])\s*[-/ ]?\s*(\d{4})\s*$", re.I)
_YEAR = re.compile(r"^\s*(\d{4})\s*$")


class TimeError(ValueError):
    pass


@dataclass
class Periods:
    keys: list
    unit: str
    order: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    unreadable: int = 0

    def upto(self, period) -> list[bool]:
        return [k is not None and k <= period for k in self.keys]

    def at(self, period) -> list[bool]:
        return [k == period for k in self.keys]

    def label(self, period) -> str:
        return self.labels.get(period, str(period))

    @property
    def word(self) -> str:
        """'month', 'quarter', 'year', 'day' or 'period', for legend labels ('New this month')."""
        return self.unit if self.unit in ("month", "quarter", "year", "day") else "period"


def _isnull(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() in ("", "NaT", "nan", "None")


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _label(key: str, unit: str) -> str:
    if unit == "year":
        return key
    if unit == "quarter":
        y, q = key.split("-Q")
        return f"Q{q} {y}"
    if unit == "month":
        y, m = key.split("-")
        return f"{calendar.month_name[int(m)]} {y}"
    if unit == "day":
        y, m, d = key.split("-")
        return f"{calendar.month_name[int(m)]} {int(d)}, {y}"
    return key


def _from_date(ts, unit: str) -> str:
    if unit == "year":
        return f"{ts.year:04d}"
    if unit == "quarter":
        return f"{ts.year:04d}-Q{(ts.month - 1) // 3 + 1}"
    if unit == "month":
        return f"{ts.year:04d}-{ts.month:02d}"
    return f"{ts.year:04d}-{ts.month:02d}-{ts.day:02d}"


def _coarsen(key: str, from_unit: str, to_unit: str) -> str:
    """A month or quarter key in a coarser unit (2026-05 -> 2026-Q2 or 2026)."""
    y = key[:4]
    if to_unit == "year":
        return y
    if to_unit == "quarter" and from_unit == "month":
        return f"{y}-Q{(int(key[5:7]) - 1) // 3 + 1}"
    if to_unit == from_unit:
        return key
    raise TimeError(f"cannot read {from_unit} values by {to_unit}")


def periods(values, unit: str = "auto", order=None) -> Periods:
    if unit not in UNITS:
        raise TimeError(f"time unit is one of {', '.join(UNITS)}, not '{unit}'")
    vals = list(values)
    present = [v for v in vals if not _isnull(v)]
    if not present:
        raise TimeError("the time field holds no values")
    import pandas as pd
    texts = [str(v).strip() for v in present]
    kind = None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() and 1800 <= float(v) <= 2200 for v in present):
        kind, parse = "year", (lambda v: f"{int(float(v)):04d}")
    elif all(_YEAR.match(t) for t in texts):
        kind, parse = "year", (lambda v: f"{int(str(v).strip()):04d}")
    elif all(_MONTH.match(t) for t in texts):
        kind = "month"

        def parse(v):
            m = _MONTH.match(str(v))
            if not m or not 1 <= int(m.group(2)) <= 12:
                return None
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    elif all(_QUARTER.match(t) for t in texts):
        kind = "quarter"

        def parse(v):
            m = _QUARTER.match(str(v))
            y, q = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            return f"{int(y):04d}-Q{int(q)}"
    if kind:
        keys = [None if _isnull(v) else parse(v) for v in vals]
        if unit not in ("auto", kind):
            keys = [None if k is None else _coarsen(k, kind, unit) for k in keys]
            kind = unit
    else:
        ts = pd.to_datetime(pd.Series([None if _isnull(v) else v for v in vals], dtype="object"), errors="coerce", format="mixed")
        ok = ts.notna()
        if ok.sum() >= 0.95 * len(present):
            u = unit
            if u == "auto":
                good = ts[ok]
                u = "month" if (good.dt.year * 12 + good.dt.month).nunique() > 1 else "day"
            kind = u
            keys = [_from_date(t, u) if isinstance(t, pd.Timestamp) and not pd.isna(t) else None for t in ts]
        else:
            if unit not in ("auto",):
                raise TimeError(f"the time field holds values that are not dates ({', '.join(sorted(set(texts))[:4])}); use unit auto for named periods")
            kind = "period"
            keys = [None if _isnull(v) else str(v).strip() for v in vals]
    distinct = sorted({k for k in keys if k is not None}, key=(lambda s: _natural(s)) if kind == "period" else None)
    if order and kind == "period":
        given = [str(x) for x in order]
        distinct = [g for g in given if g in distinct] + [d for d in distinct if d not in given]
        rank = {k: f"{i:05d}" for i, k in enumerate(distinct)}       # named periods sort by the given order
        labels = {rank[k]: k for k in distinct}
        keys = [None if k is None else rank[k] for k in keys]
        distinct = [rank[k] for k in distinct]
        return Periods(keys=keys, unit=kind, order=distinct, labels=labels, unreadable=sum(1 for k in keys if k is None) - (len(vals) - len(present)))
    if kind == "period":                                            # natural order ("Wave 2" before "Wave 10") as sortable keys
        rank = {k: f"{i:05d}" for i, k in enumerate(distinct)}
        labels = {rank[k]: k for k in distinct}
        keys = [None if k is None else rank[k] for k in keys]
        distinct = [rank[k] for k in distinct]
    else:
        labels = {k: _label(k, kind) for k in distinct}
    return Periods(keys=keys, unit=kind, order=distinct, labels=labels, unreadable=sum(1 for k in keys if k is None) - (len(vals) - len(present)))
