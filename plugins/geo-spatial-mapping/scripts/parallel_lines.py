#!/usr/bin/env python3
"""parallel_lines: lines that share a path drawn side by side instead of on top of each other (0.11.0).

Transit lines share track: Seattle's 1 and 2 Lines from Int'l District to Lynnwood, New York's 4, 5 and 6 on Lexington Avenue, up to nine
services on one trunk. Drawn as they are, the last one hides the others. A lines layer classed by category (`value_field` with a categorical
classification) and `symbol: {parallel: true}` is drawn instead with each line moved sideways by its place among the lines running beside it at
that point, so a trunk of k lines becomes k strands, one line width apart (`parallel_gap` adds a gap, in points).

How: each line is sampled every half millimetre of the page. At each sample the lines within `parallel_tolerance` (page millimetres; default
0.75 of the line width plus 0.4 mm) that also run the same way (within 35 degrees: a line that crosses is not sharing) are its companions. The
strands are ordered by the layer's category order, the same order on every trunk, so a line keeps its side as companions join and leave; the
side is taken against a canonical direction (pointing east, or north when the trunk runs exactly north-south), so two lines drawn in opposite
directions still agree which side is which. The sideways shift is smoothed over a few samples, so a junction is a short jog, not a step.

The shift is computed for the page (print, PNG, the slide): a web map keeps the true geometry, since one gap in metres cannot be one line width
at every zoom. The legend, the data outputs and the provenance are unchanged; the methods note says the lines were drawn side by side.
"""
from __future__ import annotations

import math

import numpy as np

SAMPLE_MM = 0.5
PARALLEL_DEG = 35.0
SMOOTH = 5


def _parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms if g.geom_type == "LineString"]
    return []


def _canonical(tx: np.ndarray, ty: np.ndarray):
    """Unit tangents flipped to point east (or north when exactly north-south): the same for two lines drawn in opposite directions."""
    flip = (tx < -1e-12) | ((np.abs(tx) <= 1e-12) & (ty < 0))
    s = np.where(flip, -1.0, 1.0)
    return tx * s, ty * s


def _tangents(xy: np.ndarray):
    n = len(xy)
    nxt = np.vstack([xy[1:], xy[-1:]])
    prv = np.vstack([xy[:1], xy[:-1]])
    d = nxt - prv
    if n >= 2:
        d[0] = xy[1] - xy[0]
        d[-1] = xy[-1] - xy[-2]
    L = np.hypot(d[:, 0], d[:, 1])
    L[L == 0] = 1.0
    return d[:, 0] / L, d[:, 1] / L


def _local_dir(line, pts):
    """The unit direction of `line` near each of `pts` (from two points either side of the nearest point on it)."""
    out = []
    for p in pts:
        s = line.project(p)
        a = line.interpolate(max(0.0, s - 1e-3 * max(line.length, 1.0)))
        b = line.interpolate(min(line.length, s + 1e-3 * max(line.length, 1.0)))
        dx, dy = b.x - a.x, b.y - a.y
        L = math.hypot(dx, dy) or 1.0
        out.append((dx / L, dy / L))
    return np.array(out) if out else np.zeros((0, 2))


def offset(gdf, field: str, order: list, unit_per_mm: float, width_mm: float, gap_mm: float = 0.0, tol_mm: float | None = None):
    """-> (GeoDataFrame with each feature's lines moved sideways, report dict). `gdf` is in the map's projected CRS; `unit_per_mm` is ground units per
    page millimetre; `order` is the category order (strands are stacked in it)."""
    import shapely
    from shapely.geometry import LineString, MultiLineString, Point
    spacing = (width_mm + gap_mm) * unit_per_mm
    tol = (tol_mm if tol_mm is not None else 0.75 * width_mm + 0.4) * unit_per_mm
    step = SAMPLE_MM * unit_per_mm
    rank = {v: i for i, v in enumerate(order)}
    whole = {}
    for v, g in gdf.groupby(field, sort=False).geometry:
        whole[v] = shapely.line_merge(shapely.union_all(list(g.values))) if len(g) else None
    cos_par = math.cos(math.radians(PARALLEL_DEG))
    shared_mm = 0.0
    max_k = 1
    new = []
    for _, row in gdf.iterrows():
        me = row[field]
        others = [v for v in whole if v != me and whole[v] is not None and not whole[v].is_empty]
        parts_out = []
        for part in _parts(row.geometry):
            if part.length <= 0:
                continue
            dense = shapely.segmentize(part, step)
            xy = np.asarray(dense.coords)[:, :2]
            tx, ty = _canonical(*_tangents(xy))
            pts = [Point(x, y) for x, y in xy]
            members = [[me] for _ in pts]
            for v in others:
                ov = whole[v]
                d = shapely.distance(ov, shapely.points(xy))
                near = np.where(d <= tol)[0]
                if not len(near):
                    continue
                od = _local_dir(ov, [pts[i] for i in near])
                for j, i in enumerate(near):
                    if abs(od[j, 0] * tx[i] + od[j, 1] * ty[i]) >= cos_par:
                        members[i].append(v)
            shift = np.zeros(len(xy))
            for i, m in enumerate(members):
                k = len(m)
                if k > 1:
                    m_sorted = sorted(m, key=lambda x: (rank.get(x, 10 ** 6), str(x)))
                    shift[i] = (m_sorted.index(me) - (k - 1) / 2.0) * spacing
                    max_k = max(max_k, k)
            if len(shift) >= SMOOTH:
                ker = np.ones(SMOOTH) / SMOOTH
                padded = np.concatenate([np.full(SMOOTH // 2, shift[0]), shift, np.full(SMOOTH // 2, shift[-1])])
                shift = np.convolve(padded, ker, mode="valid")[:len(xy)]
            shared_mm += float(np.count_nonzero(np.abs(shift) > 1e-9)) * SAMPLE_MM
            nx, ny = -ty, tx                                  # the left of the canonical direction
            moved = np.column_stack([xy[:, 0] + nx * shift, xy[:, 1] + ny * shift])
            parts_out.append(LineString(moved))
        new.append(MultiLineString(parts_out) if len(parts_out) > 1 else (parts_out[0] if parts_out else row.geometry))
    out = gdf.copy()
    out["geometry"] = new
    out = out.set_geometry("geometry", crs=gdf.crs)
    rep = {"spacing_mm": round(width_mm + gap_mm, 3), "tolerance_mm": round(tol / unit_per_mm, 3), "max_side_by_side": max_k,
           "shared_page_mm": round(shared_mm / 2, 1)}
    return out, rep
