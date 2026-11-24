#!/usr/bin/env python3
"""massing: the 3D context of a building from its footprint (1.16.0, Day 15 issues 8 and 13).

Open data often has a building's outline and nothing about its shape above the ground: Overture and OpenStreetMap hold Lumen Field's outline
with its bowl as a hole, the roof canopies and a scoreboard, and no stands. The `massing` op turns a footprint into parts with a base and a
height that the 3D page extrudes (`extrude: {height_field: height_m, base_field: base_m}`), in one of four forms:

  solid      the footprint with its holes filled, one block (a tower, a warehouse, a hall)
  courtyard  the footprint as it is, holes kept open (a perimeter block, a cloister)
  stepped    tiers set back from the edge, each standing on the one below ({tiers: [{inset_m, height_m}, ...]}: a podium and a tower)
  raked      rows that rise from an inner edge to the outer one (a stadium, an arena, an amphitheatre, a racecourse stand): the opening is the
             footprint's largest hole, or the polygons of another layer (`opening: LAYER`); rows of row_m rise by rake metres a metre, from
             start_m, up to top_m (or a top for each side by bearing from the opening's centre, `sides: [{from_deg, to_deg, top_m}]`); past the
             top row the level stays flat (a concourse) out to an outer wall wall_m thick that stands wall_above_m over it. The rows are cut into
             slices of slice_deg so the heights can follow the sides.

Every part carries `part` (block, tier, row, concourse, wall), `base_m`, `height_m` and the source feature's columns. Heights a person did not
give are modelled, not surveyed: the op says so (MAP-OPS-010, INFO) and the map's note should too.
"""
from __future__ import annotations

import math

FORMS = ("solid", "courtyard", "stepped", "raked")
DEFAULTS = {"row_m": 3.0, "rake": 0.55, "start_m": 2.0, "top_m": 30.0, "slice_deg": 7.5, "wall_m": 3.0, "wall_above_m": 4.0, "height_m": 12.0}


class MassingError(ValueError):
    pass


def fill_holes(geom, max_area_m2: float | None = None, min_area_m2: float | None = None):
    """The polygon with its holes filled (only those between min_area and max_area, in the geometry's own units squared)."""
    from shapely.geometry import MultiPolygon, Polygon
    if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        return geom, 0
    n = 0
    out = []
    for p in getattr(geom, "geoms", [geom]):
        keep = []
        for r in p.interiors:
            a = Polygon(r).area
            if (max_area_m2 is None or a <= max_area_m2) and (min_area_m2 is None or a >= min_area_m2):
                n += 1
            else:
                keep.append(r)
        out.append(Polygon(p.exterior, keep))
    return (out[0] if len(out) == 1 else MultiPolygon(out)), n


def largest_hole(geom):
    from shapely.geometry import Polygon
    best = None
    for p in getattr(geom, "geoms", [geom]):
        for r in p.interiors:
            h = Polygon(r)
            if best is None or h.area > best.area:
                best = h
    return best


def _top_for(bearing: float, p: dict) -> float:
    for s in p.get("sides") or []:
        a, b = float(s["from_deg"]) % 360, float(s["to_deg"]) % 360
        inside = a <= bearing < b if a <= b else (bearing >= a or bearing < b)
        if inside:
            return float(s["top_m"])
    return float(p["top_m"])


def _wedge(cx, cy, a0, a1, r):
    """A pie slice from bearing a0 to a1 (degrees clockwise from north) of radius r around (cx, cy)."""
    from shapely.geometry import Polygon
    pts = [(cx, cy)]
    k = max(2, int(math.ceil((a1 - a0) / 2.0)))
    for i in range(k + 1):
        a = math.radians(a0 + (a1 - a0) * i / k)
        pts.append((cx + r * math.sin(a), cy + r * math.cos(a)))
    return Polygon(pts)


def raked(footprint, opening, p: dict) -> list[dict]:
    """The rows, concourse and wall of a raked stand between `opening` and the footprint's outer edge (metric CRS)."""
    from shapely.geometry import Polygon
    p = {**DEFAULTS, **{k: v for k, v in p.items() if v is not None}}
    row, rake, start = float(p["row_m"]), float(p["rake"]), float(p["start_m"])
    if row <= 0 or rake < 0:
        raise MassingError("raked needs row_m > 0 and rake >= 0")
    outer = Polygon(footprint.exterior) if footprint.geom_type == "Polygon" else footprint.convex_hull
    stand = outer.difference(opening)
    if stand.is_empty:
        raise MassingError("the opening covers the whole footprint; nothing is left for the stands")
    wall_w = float(p["wall_m"])
    inner_of_wall = outer.buffer(-wall_w) if wall_w > 0 else outer
    wall = outer.difference(inner_of_wall) if wall_w > 0 else None
    c = opening.centroid
    minx, miny, maxx, maxy = outer.bounds
    radius = 2 * math.hypot(maxx - minx, maxy - miny) + 10
    reach = max(0.0, max(math.hypot(x - c.x, y - c.y) for x, y in outer.exterior.coords))
    n_rows = int(math.ceil(reach / row)) + 1
    step = float(p["slice_deg"])
    if not (1.0 <= step <= 90.0):
        raise MassingError("slice_deg is between 1 and 90 degrees")
    rings = [opening.buffer(row * k, join_style="mitre", mitre_limit=3.0) for k in range(n_rows + 1)]
    parts = []
    a = 0.0
    while a < 360.0 - 1e-9:
        b = min(360.0, a + step)
        wedge = _wedge(c.x, c.y, a, b, radius)
        top = _top_for((a + b) / 2, p)
        sl = stand.intersection(wedge).intersection(inner_of_wall)
        if not sl.is_empty:
            for k in range(n_rows):
                band = sl.intersection(rings[k + 1].difference(rings[k]))
                if band.is_empty or band.area < 1e-6:
                    continue
                h = start + (k + 1) * row * rake
                flat = h >= top
                parts.append({"geometry": band, "part": "concourse" if flat and (start + k * row * rake) >= top else "row", "base_m": 0.0,
                              "height_m": round(min(h, top), 2), "row": k + 1, "bearing": round((a + b) / 2, 2)})
            rest = sl.difference(rings[n_rows])
            if not rest.is_empty and rest.area > 1e-6:
                parts.append({"geometry": rest, "part": "concourse", "base_m": 0.0, "height_m": round(top, 2), "row": n_rows + 1, "bearing": round((a + b) / 2, 2)})
        if wall is not None:
            w = wall.intersection(wedge)
            if not w.is_empty and w.area > 1e-6:
                parts.append({"geometry": w, "part": "wall", "base_m": 0.0, "height_m": round(top + float(p["wall_above_m"]), 2), "row": None,
                              "bearing": round((a + b) / 2, 2)})
        a = b
    return parts


def stepped(footprint, tiers: list[dict], base_m: float = 0.0) -> list[dict]:
    from shapely.geometry import Polygon
    if not tiers:
        raise MassingError("stepped needs tiers: [{inset_m, height_m}, ...] (the first tier at inset 0 is the podium)")
    shape = Polygon(footprint.exterior) if footprint.geom_type == "Polygon" else footprint
    parts, z = [], float(base_m)
    for i, t in enumerate(tiers):
        g = shape.buffer(-float(t.get("inset_m", 0.0)), join_style="mitre") if float(t.get("inset_m", 0.0)) > 0 else shape
        if g.is_empty:
            raise MassingError(f"tier {i + 1}: an inset of {t.get('inset_m')} m leaves nothing of the footprint")
        h = float(t["height_m"])
        parts.append({"geometry": g, "part": "tier", "base_m": round(z, 2), "height_m": round(z + h, 2), "row": i + 1, "bearing": None})
        z += h
    return parts


def mass(footprint, p: dict, *, opening=None, height: float | None = None, base: float = 0.0) -> list[dict]:
    """The parts of one footprint (in a metric CRS) for form p['form']."""
    form = str(p.get("form") or "solid")
    if form not in FORMS:
        raise MassingError(f"form is one of {', '.join(FORMS)}, not '{form}'")
    h = float(height if height is not None else p.get("height_m") or DEFAULTS["height_m"])
    if form == "solid":
        g, _n = fill_holes(footprint)
        return [{"geometry": g, "part": "block", "base_m": round(base, 2), "height_m": round(base + h, 2), "row": None, "bearing": None}]
    if form == "courtyard":
        return [{"geometry": footprint, "part": "block", "base_m": round(base, 2), "height_m": round(base + h, 2), "row": None, "bearing": None}]
    if form == "stepped":
        return stepped(footprint, p.get("tiers") or [], base)
    op = opening if opening is not None else largest_hole(footprint)
    if op is None or op.is_empty:
        raise MassingError("raked needs an opening: the footprint has no hole, so give opening: LAYER (the pitch, the arena floor)")
    return raked(footprint, op, p)
