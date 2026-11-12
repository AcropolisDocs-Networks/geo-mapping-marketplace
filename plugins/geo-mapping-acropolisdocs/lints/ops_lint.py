"""Geoprocessing lint, MAP-OPS-* (M11). Pure functions over numbers and names; no data is read, nothing is written.

    lint_ops_spec(ops)                  -> findings for a MapSpec `ops:` list before any data is read (008)
    lint_measure_crs(crs)               -> 001 when a measurement would be made in degrees (the caller raises)
    lint_buffer_distance(metres)        -> 002
    lint_join_emptied(n_in, n_out, op)  -> 003
    lint_value_is_rate(name, values)    -> 004
    lint_collections_reduced(n, op)     -> 005
    lint_grid_cells(n)                  -> 006
    lint_nearest_unbounded(rows, max_d) -> 007

001  BLOCK an op measured in a geographic CRS (never allowed; geo_ops raises MeasureCRSError)
002  WARN  a buffer wider than 500 km: one projection cannot hold a distance that large true everywhere
003  WARN  a spatial join or nearest op kept no row
004  INFO  population_covered or coverage_share on a value that reads like a rate (a percent, a density): apportioning a rate by area is wrong; use the count
005  INFO  an overlay or repair produced GeometryCollections, which were reduced to the layer's own geometry family
006  WARN  a grid or hex aggregation with more than 50,000 cells
007  WARN  nearest or a nearest join with no max_distance on more than 100,000 rows
008  BLOCK an `ops:` entry with an unknown op, or one that needs `layer:` without it (raster_zonal needs `raster:` and a known stat; a viewshed
           radius is at most 100 km) (M12 added raster_zonal, line_of_sight and viewshed)
009  WARN  coincident points in a Voronoi (raised by geo_ops)
"""
from __future__ import annotations

import re

BUFFER_WARN_M = 500_000.0
GRID_CELLS_WARN = 50_000
GRID_CELLS_MAX = 2_000_000
NEAREST_ROWS_WARN = 100_000
SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}

# the same vocabulary as classify.normalization_check, used the other way round: a field that reads like a rate must not be apportioned by area
RATE_HINT = re.compile(r"(per|pct|percent|ratio|rate|share|density|avg|average|mean|median|index|score|_pc$|per_)", re.I)
RAW_COUNT_HINT = re.compile(r"(^|[_\s])(count|cnt|total|num|number|n|pop|population|sites|towers|sum|households|hh|jobs|employees|housing|units)([_\s]|$)", re.I)

OP_NAMES = ("buffer", "centroid", "representative_point", "clip", "intersect", "intersection", "union", "difference", "symmetric_difference",
            "spatial_join", "nearest", "dissolve", "explode", "dedupe", "repair", "make_valid", "simplify", "antimeridian_split", "convex_hull",
            "concave_hull", "voronoi", "area", "length", "count_in", "sum_in", "population_covered", "coverage_share", "overlap", "grid", "hexbin",
            "raster_zonal", "line_of_sight", "viewshed", "geocode", "as_of")
ZONAL_STATS = ("sum", "mean", "min", "max", "count", "majority", "median", "std")
NEEDS_LAYER = ("clip", "intersect", "intersection", "union", "difference", "symmetric_difference", "spatial_join", "nearest", "count_in", "sum_in",
               "population_covered", "coverage_share")
NEEDS_VALUE = ("sum_in", "population_covered", "coverage_share")


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def lint_measure_crs(crs) -> list[dict]:
    """MAP-OPS-001. `crs` is anything pyproj can read."""
    from pyproj import CRS
    try:
        c = CRS.from_user_input(crs)
    except Exception as e:
        return [finding("MAP-OPS-001", "BLOCK", f"the measurement CRS '{crs}' cannot be read: {e}", str(crs))]
    if c.is_geographic:
        return [finding("MAP-OPS-001", "BLOCK", f"a measurement was about to be made in {c.to_string()}, a geographic CRS (degrees are not a distance); this is a plugin defect, not a data problem", c.to_string())]
    return []


def lint_buffer_distance(metres: float) -> list[dict]:
    if metres is not None and metres > BUFFER_WARN_M:
        return [finding("MAP-OPS-002", "WARN", f"a buffer of {metres / 1000:,.0f} km is wider than {BUFFER_WARN_M / 1000:,.0f} km; no single projection keeps a distance that large true everywhere, so the ring is approximate away from the data's centre", metres)]
    return []


def lint_join_emptied(n_in: int, n_out: int, op: str) -> list[dict]:
    if n_in > 0 and n_out == 0:
        return [finding("MAP-OPS-003", "WARN", f"{op} kept none of the {n_in} feature(s): nothing matched (check the predicate, the distance and that both layers lie in the same place)", {"rows_in": n_in})]
    return []


def lint_value_is_rate(name: str, values) -> list[dict]:
    """MAP-OPS-004: classify's raw-count heuristic in reverse. A rate apportioned by area is a wrong number."""
    import numpy as np
    x = np.asarray(values, dtype="float64") if values is not None else np.array([])
    x = x[np.isfinite(x)]
    named_rate = bool(RATE_HINT.search(name or ""))
    named_count = bool(RAW_COUNT_HINT.search(name or ""))
    integer = x.size > 0 and bool(np.all(x == np.round(x))) and bool(np.all(x >= 0))
    reasons = []
    if named_rate:                      # as in classify: a rate-like name wins over a count-like one (pop_density is a rate)
        reasons.append(f"'{name}' reads like a rate")
    if x.size > 5 and not integer and float(x.max()) <= 100 and float(x.min()) >= 0 and not named_count:
        reasons.append(f"'{name}' holds fractions between 0 and {x.max():g}, which looks like a percent or share")
    if not reasons:
        return []
    return [finding("MAP-OPS-004", "INFO", f"population_covered apportions a count by area; {'; '.join(reasons)}. A rate cannot be summed across zones: use the count it was made from (population, households) and divide afterwards", {"field": name, "reasons": reasons})]


def lint_collections_reduced(n: int, op: str) -> list[dict]:
    if n:
        return [finding("MAP-OPS-005", "INFO", f"{op} produced {n} GeometryCollection or mixed-type result(s) (slivers, stray lines or points along shared edges); they were reduced to the layer's own geometry type", n)]
    return []


def lint_grid_cells(n: int) -> list[dict]:
    if n > GRID_CELLS_WARN:
        return [finding("MAP-OPS-006", "WARN", f"{n:,} cells is more than {GRID_CELLS_WARN:,}; a map cannot show them and the file is large. Use a larger cell size or a coarser resolution", n)]
    return []


def lint_nearest_unbounded(rows: int, max_distance_m) -> list[dict]:
    if max_distance_m is None and rows > NEAREST_ROWS_WARN:
        return [finding("MAP-OPS-007", "WARN", f"nearest on {rows:,} rows with no max_distance searches without a bound; set max_distance to keep it quick and to leave far-off matches out", rows)]
    return []


def lint_ops_spec(ops) -> list[dict]:
    """MAP-OPS-008: the `ops:` list of a layer, checked before any data is read."""
    out = []
    if ops is None:
        return out
    if not isinstance(ops, list):
        return [finding("MAP-OPS-008", "BLOCK", "`ops` must be a list of mappings, each with an `op` name", type(ops).__name__)]
    for i, o in enumerate(ops):
        if isinstance(o, dict) and not o.get("op") and o.get("as_of"):          # 1.3.0: the short form {as_of: DATE}
            o = {"op": "as_of", "date": o["as_of"]}
        if not isinstance(o, dict) or not o.get("op"):
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] must be a mapping with an `op` name", o))
            continue
        name = str(o["op"]).lower()
        if name not in OP_NAMES:
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}]: unknown op '{name}'. Ops: {', '.join(OP_NAMES)}", name))
            continue
        if name in NEEDS_LAYER and not o.get("layer"):
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] ({name}) needs `layer:` naming another layer of the spec", name))
        if name in NEEDS_VALUE and not o.get("value"):
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] ({name}) needs `value:` naming the field to apportion or sum", name))
        if name == "buffer" and o.get("distance") is None and not o.get("rings"):
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (buffer) needs `distance` (or `rings`) with units", name))
        if name == "grid" and o.get("size") is None and str(o.get("shape", "square")) != "hex":
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (grid) needs `size` with units", name))
        if name == "simplify" and o.get("tolerance") is None:
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (simplify) needs `tolerance` with units", name))
        if name == "raster_zonal":
            if not o.get("raster"):
                out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (raster_zonal) needs `raster:` (a GeoTIFF path or a library id)", name))
            if str(o.get("stat", "sum")).lower() not in ZONAL_STATS:
                out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (raster_zonal): stat is one of {', '.join(ZONAL_STATS)}", name))
        if name == "viewshed" and o.get("radius_m") is not None and float(o["radius_m"]) > 100_000:
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (viewshed): a radius of {float(o['radius_m']) / 1000:g} km is beyond what a one-degree ray march can show; at most 100 km", name))
        if name == "geocode" and (not o.get("address") or str(o.get("purpose") or "") not in ("sites", "venues", "businesses")):
            out.append(finding("MAP-OPS-008", "BLOCK", f"ops[{i}] (geocode) needs `address:` (the column, or street, city, state, zip) and `purpose: sites | venues | businesses` "
                                                   "(private individuals' addresses are never geocoded, MAP-SVC-035)", name))
    return sorted(out, key=lambda f: (SEV_ORDER[f["severity"]], f["id"]))
