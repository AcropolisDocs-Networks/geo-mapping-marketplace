#!/usr/bin/env python3
"""scalebar-northarrow: map scale, scale bar and north arrow math for the display projection (spec 7.3, 10, 12.3 MAP-CRS-003).

    scalebar.py info --crs EPSG:5070 --bbox xmin ymin xmax ymax --frame-mm 250 [--units auto|mi|km] [--rotation 0]

A scale bar is true only where the projection's local scale factor is 1. This module measures the factor with PROJ at the
center and the corners of the extent, corrects the bar for it, and reports how much the scale varies across the map so the
cartographic lint can warn (or block) instead of drawing a bar that lies. Nothing here draws; layout renders the model.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

M_PER_MI = 1609.344
NICE = (1, 2, 2.5, 5)
HIDE_BELOW_SCALE = 50_000_000            # smaller scales than 1:50M: no scale bar (spec 10)
WARN_VARIATION = 0.01                    # local scale varies more than 1% across the frame
BLOCK_VARIATION = 0.25                   # more than 25%: a bar would mislead
NORTH_UPRIGHT_TOLERANCE = 0.5            # degrees of grid convergence ignored


def _proj(crs):
    from pyproj import CRS, Proj
    c = CRS.from_user_input(crs)
    return c, Proj(c)


def _lonlat_of(crs, xs, ys):
    from pyproj import CRS, Transformer
    t = Transformer.from_crs(CRS.from_user_input(crs), "EPSG:4326", always_xy=True)
    return t.transform(xs, ys)


def local_factors(crs, x: float, y: float) -> dict:
    """PROJ's scale factors at a point given in the CRS: meridional, parallel, areal, and meridian convergence (degrees)."""
    c, p = _proj(crs)
    if c.is_geographic:
        lat = y
        k = 1.0 / max(math.cos(math.radians(lat)), 1e-6)       # a degree grid drawn linearly: east-west stretch
        return {"h": 1.0, "k": k, "areal": k, "convergence": 0.0, "geographic": True}
    lon, lat = _lonlat_of(c, [x], [y])
    f = p.get_factors(lon[0], lat[0])
    return {"h": float(f.meridional_scale), "k": float(f.parallel_scale), "areal": float(f.areal_scale),
            "convergence": float(f.meridian_convergence), "geographic": False}


def to_meters_factor(crs) -> float:
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    if c.is_geographic:
        return 111_320.0                                       # per degree at the equator; corrected by cos(lat) above
    try:
        return float(c.axis_info[0].unit_conversion_factor)
    except Exception:  # noqa: BLE001
        return 1.0


def scale_info(crs, bbox, frame_mm: float, *, units: str = "auto", rotation: float = 0.0, segments: int | None = None) -> dict:
    """Everything the layout needs to draw a true scale bar and a correct north arrow."""
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    m_per_unit = to_meters_factor(crs)
    width_m = (xmax - xmin) * m_per_unit
    pts = [(cx, cy), (xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)]
    fac = [local_factors(crs, x, y) for x, y in pts]
    # a bar is read along the frame's horizontal, so the parallel factor is the one that matters
    ks = [f["k"] for f in fac]
    kc = ks[0]
    variation = (max(ks) - min(ks)) / kc if kc else 0.0
    if fac[0]["geographic"]:                                   # degrees: ground m per degree of longitude at the center latitude
        width_m = (xmax - xmin) * 111_320.0 * math.cos(math.radians(cy))
        kc, variation = 1.0, (max(ks) - min(ks)) / max(min(ks), 1e-9)
    ground_m_per_mm = width_m / (frame_mm * kc) if frame_mm > 0 else 0.0     # true ground distance under 1 mm of paper at the center
    denom = ground_m_per_mm * 1000.0
    out: dict = {"scale_denominator": round(denom), "ratio": f"1:{round(denom, -int(max(0, math.floor(math.log10(max(denom, 1))) - 2))):,.0f}",
                 "variation": round(variation, 4), "center_factor": round(kc, 5), "frame_mm": frame_mm}
    # ---- scale bar
    unit = units
    if unit == "auto":
        unit, out["units_reason"], out["units_mixed"] = units_auto(crs, (xmin, ymin, xmax, ymax))
    out["units"] = unit
    sev = "ok"
    if denom >= HIDE_BELOW_SCALE:
        out["bar"] = None
        out["bar_hidden"] = f"1:{denom / 1e6:.0f}M is smaller than 1:50M; a scale bar would mislead"
    else:
        out["bar"] = bar_model(ground_m_per_mm, frame_mm, unit, segments)
        out["bar_hidden"] = None
    if variation > BLOCK_VARIATION:
        sev = "block"
    elif variation > WARN_VARIATION:
        sev = "warn"
    out["distortion"] = {"severity": sev,
                         "message": (f"scale varies by {variation:.0%} across the extent; the bar is true only near the center"
                                     if sev != "ok" else "scale is uniform to within 1%")}
    out["north"] = north_arrow(crs, cx, cy, bbox, rotation)
    return out


def units_auto(crs, bbox) -> tuple[str, str, list]:
    """units: auto (1.5.0, R3-UNITS): the distance units of the country under the centre (data/countries.yaml), with the reason, and the
    countries in the window whose units differ (MAP-LOC-002). Replaces the old US box, which gave Canada and Mexico miles (U2)."""
    import geo_locale as gl
    xmin, ymin, xmax, ymax = bbox
    try:
        xs = [(xmin + xmax) / 2, xmin, xmax, xmin, xmax, (xmin + xmax) / 2, (xmin + xmax) / 2, xmin, xmax]
        ys = [(ymin + ymax) / 2, ymin, ymin, ymax, ymax, ymin, ymax, (ymin + ymax) / 2, (ymin + ymax) / 2]
        lon, lat = _lonlat_of(crs, xs, ys)
    except Exception:  # noqa: BLE001
        return "km", "the centre could not be placed; kilometres", []
    iso = gl.country_at(float(lon[0]), float(lat[0]))
    row = gl.country(iso)
    unit = (row or {}).get("units", "km")
    others = []
    for lo, la in zip(lon[1:], lat[1:]):
        o = gl.country_at(float(lo), float(la))
        if o and o != iso and (gl.country(o) or {}).get("units", "km") != unit and o not in others:
            others.append(o)
    why = (f"the centre is in {row['name']} ({iso}), which uses {'miles' if unit == 'mi' else 'kilometres'}" if row
           else "the centre is not in a country (at sea); kilometres")
    return unit, why, others


def nice_length(max_ground: float) -> float:
    """The largest 1, 2, 2.5 or 5 x 10^n that does not exceed max_ground."""
    if max_ground <= 0:
        return 0.0
    e = math.floor(math.log10(max_ground))
    best = 0.0
    for p in (e - 1, e):
        for n in NICE:
            v = n * 10 ** p
            if v <= max_ground + 1e-12 and v > best:
                best = v
    return best


def bar_model(ground_m_per_mm: float, frame_mm: float, unit: str, segments: int | None = None, max_frac: float = 0.28) -> dict:
    per_unit = M_PER_MI if unit == "mi" else (1000.0 if unit == "km" else 1.0)
    max_ground = ground_m_per_mm * frame_mm * max_frac / per_unit
    total = nice_length(max_ground)
    if total <= 0:
        return {"total": 0, "length_mm": 0, "unit": unit, "segments": []}
    n = segments or _segments(total)
    step = total / n
    length_mm = total * per_unit / ground_m_per_mm
    decimals = 0 if float(step).is_integer() else (1 if round(step * 10) == step * 10 else 2)
    segs = [{"from": round(i * step, 6), "to": round((i + 1) * step, 6), "start_mm": round(length_mm * i / n, 4), "end_mm": round(length_mm * (i + 1) / n, 4)}
            for i in range(n)]
    ticks = [{"at_mm": round(length_mm * i / n, 4), "label": f"{i * step:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{i * step:.0f}"} for i in range(n + 1)]
    ticks[-1]["label"] = f"{total:g} {unit}"
    return {"total": total, "unit": unit, "length_mm": round(length_mm, 4), "segments": segs, "ticks": ticks, "decimals": decimals}


TICK_LABEL_GAP_MM = 1.0


def thin_ticks(ticks: list[dict], width_of, gap_mm: float = TICK_LABEL_GAP_MM) -> list[dict]:
    """The tick labels that can be drawn without touching (TODO from the sample-map showcase: 150, 200, 250 mi crowded at the right end). The
    first (0) and the last (the total with its unit, the widest) always stay; between them every label, else every second, third... is kept,
    and an interior label that would still touch the last one is dropped. `width_of(label)` is the label's width in mm (labels are centred)."""
    if len(ticks) <= 2:
        return list(ticks)

    def fits(sel):
        return all(a["at_mm"] + width_of(a["label"]) / 2 + gap_mm <= b["at_mm"] - width_of(b["label"]) / 2 for a, b in zip(sel, sel[1:]))
    n = len(ticks) - 1
    for step in range(1, n + 1):
        keep = [ticks[i] for i in range(0, n, step)]
        while len(keep) > 1 and not fits(keep[-1:] + [ticks[-1]]):
            keep.pop()                                     # an interior label too close to the end label goes
        sel = keep + [ticks[-1]]
        if fits(sel):
            return sel
    return [ticks[0], ticks[-1]]


def _segments(total: float) -> int:
    m = total / 10 ** math.floor(math.log10(total))             # mantissa 1, 2, 2.5 or 5
    return {1.0: 2, 2.0: 4, 2.5: 5, 5.0: 5}.get(round(m, 3), 4) if m != 1.0 else 2


def north_arrow(crs, cx, cy, bbox, rotation: float = 0.0) -> dict:
    """Rotation to draw (clockwise degrees; 0 = up) or a reason to hide it.

    True north differs from the grid's 'up' by the meridian convergence. It is not one direction on world-scale or polar maps,
    so there the arrow is hidden with a reason instead of pointing somewhere wrong.
    """
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    try:
        lon, lat = _lonlat_of(c, [bbox[0], bbox[2], (bbox[0] + bbox[2]) / 2], [bbox[1], bbox[3], (bbox[1] + bbox[3]) / 2])
        span = max(lon) - min(lon)
        polar = max(abs(v) for v in lat) > 80
    except Exception:  # noqa: BLE001
        span, polar = 0.0, False
    method = c.coordinate_operation.method_name.lower() if c.coordinate_operation else ""
    polar_crs = "polar" in c.name.lower() or "polar" in method
    polar_msg = {"show": False, "rotation": 0.0, "reason": "polar map: north points a different way across the frame"}
    if polar_crs:
        return polar_msg
    if span > 90:
        return {"show": False, "rotation": 0.0, "reason": "world-scale map: north is not one direction across the frame"}
    if polar:
        return polar_msg
    f = local_factors(c, cx, cy)
    conv = f["convergence"]
    # PROJ: meridian_convergence is the angle from grid north to true north; the arrow turns the other way to point at true north
    rot = (-conv + rotation) % 360
    if rot > 180:
        rot -= 360
    if abs(rot) < NORTH_UPRIGHT_TOLERANCE:
        rot = 0.0
    return {"show": True, "rotation": round(rot, 2), "reason": None, "convergence": round(conv, 3)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info")
    i.add_argument("--crs", required=True)
    i.add_argument("--bbox", nargs=4, type=float, required=True)
    i.add_argument("--frame-mm", type=float, required=True)
    i.add_argument("--units", default="auto", choices=["auto", "mi", "km", "m"])
    i.add_argument("--rotation", type=float, default=0.0)
    a = ap.parse_args(argv)
    print(json.dumps(scale_info(a.crs, a.bbox, a.frame_mm, units=a.units, rotation=a.rotation), indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
