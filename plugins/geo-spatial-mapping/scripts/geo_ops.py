#!/usr/bin/env python3
"""geo-ops: geoprocessing over GeoDataFrames (gap analysis P1-1, P1-2, P1-5; M11).

Pure functions. Each op takes (gdf, params, ctx) and returns an OpResult: the new frame, notes (plain sentences for the
methods page), findings (MAP-OPS-* and MAP-DATA-* dicts) and a provenance record (op, parameters, rows in and out, the CRS
the measurement was made in). The driver runs a list of ops in order:

    import geo_ops
    ctx = geo_ops.Ctx(layers={"tracts": tracts_gdf}, layer_id="footprint")
    gdf, notes, findings = geo_ops.apply_ops(gdf, [{"op": "buffer", "distance": 2, "units": "km"},
                                                   {"op": "population_covered", "layer": "tracts", "value": "households"}], ctx)
    ctx.provenance        # one record per op, in order

Rules every op keeps:
  * nothing is measured in degrees. Distances, areas and grids are computed in a projected CRS chosen for the data
    (the layer's own metric CRS when it has one; otherwise crs_engine.choose_display: UTM for a local extent, Conus Albers
    for the lower 48, an equal-area LAEA elsewhere). A geographic measurement CRS is MAP-OPS-001 and raises.
  * area and length fields are geodesic (pyproj Geod on the WGS84 ellipsoid), so they do not depend on that choice.
  * distances are given with units (m, km, mi, ft); areas in m2, km2, mi2, acres or ha. The default is metres.
  * results come back in the layer's own CRS, with its attributes kept; another layer's attributes get a prefix.
  * an op that needs another layer names it with `layer:`; the ctx hands it over by id.

Registry: OPS maps the op name to its function. equivalent(op, params, prov) gives the GeoPandas and PostGIS expression.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import crs_engine as ce  # noqa: E402
import geo_io  # noqa: E402
import ops_lint  # noqa: E402

UNITS_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048}
AREA_UNITS_M2 = {"m2": 1.0, "km2": 1_000_000.0, "mi2": 2_589_988.110336, "acres": 4_046.8564224, "ha": 10_000.0}
PREDICATES = ("intersects", "within", "contains", "nearest", "overlaps", "touches", "crosses", "covers", "covered_by")
BUFFER_SEGMENTS = 16                   # quarter-circle segments: a 64-gon keeps a circle's area within 0.2 percent
NEAREST_BRUTE_FORCE_MAX = 200_000_000  # left x right pairs for k > 1
WGS84 = "EPSG:4326"
RING_FIELD = "ring"


class OpsError(geo_io.GeoIOError):
    """A refusal or a usage error with a message that is safe to show."""


class MeasureCRSError(OpsError):
    """MAP-OPS-001: a measurement was about to be made in degrees."""

    def __init__(self, message: str, finding: dict):
        super().__init__(message)
        self.finding = finding


@dataclass
class Ctx:
    """What an op may reach beyond its own layer: the other layers by id, and the running provenance."""
    layers: dict = field(default_factory=dict)
    layer_id: str = "layer"
    measure_crs: str | None = None          # force one measurement CRS (tests, or a MapSpec `crs.measure`)
    provenance: list = field(default_factory=list)
    base: object = None                     # the folder a relative raster or DEM path is read from (the MapSpec's folder) (M12)
    dem: object = None                      # callable(bounds_wgs84, res_m) -> (path, record): the DEM an op uses when it names none (M12)
    raster_fetch: object = None             # callable(entry_id, bounds_wgs84) -> raster_fetch.raster_for result; tests replace it (M12)
    engine: str | None = None               # auto | pandas | duckdb for the aggregate ops (the layer's `engine:`, an op's own wins) (M16)

    def other(self, name, op: str):
        if not name:
            raise OpsError(f"op '{op}' needs `layer:` naming another layer" + (f" (known: {', '.join(sorted(self.layers))})" if self.layers else ""))
        if name not in self.layers:
            raise OpsError(f"op '{op}': layer '{name}' is not available" + (f"; known layers: {', '.join(sorted(self.layers))}" if self.layers else " (give it with --other)"))
        g = self.layers[name]
        if g is None or g.crs is None:
            raise OpsError(f"op '{op}': layer '{name}' has no CRS; say which it uses first")
        return g


@dataclass
class OpResult:
    gdf: object
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- units and helpers

def parse_distance(value, units: str | None = None, what: str = "distance") -> tuple[float, str]:
    """2, '2 km', '1.5mi' -> (2.0, 'km'). A bare number takes `units` (default m)."""
    if isinstance(value, str):
        m = re.fullmatch(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*", value)
        if not m:
            raise OpsError(f"{what} '{value}' is not a number with optional units (m, km, mi, ft)")
        num, u = float(m.group(1)), (m.group(2) or units or "m").lower()
    else:
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise OpsError(f"{what} must be a number, got {value!r}") from None
        u = (units or "m").lower()
    if u not in UNITS_M:
        raise OpsError(f"unknown {what} unit '{u}'. Use one of: {', '.join(UNITS_M)}")
    return num, u


def to_metres(value, units: str | None = None, what: str = "distance") -> tuple[float, float, str]:
    """-> (metres, value in its units, unit name)."""
    v, u = parse_distance(value, units, what)
    return v * UNITS_M[u], v, u


def _area_unit(units) -> str:
    u = (units or "km2").lower().replace("²", "2").replace("sq_km", "km2").replace("sqkm", "km2").replace("sq_mi", "mi2").replace("sqmi", "mi2").replace("acre", "acres").replace("acress", "acres")
    if u not in AREA_UNITS_M2:
        raise OpsError(f"unknown area unit '{units}'. Use one of: {', '.join(AREA_UNITS_M2)}")
    return u


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:g}"


def _geom_col(gdf) -> str:
    return gdf.geometry.name


def _family(g) -> str:
    return g.geom_type.replace("Multi", "") if g is not None else ""


def _families(gdf) -> set:
    return {_family(g) for g in gdf.geometry if g is not None and not g.is_empty}


def _require_crs(gdf, op: str):
    if gdf.crs is None:
        raise OpsError(f"op '{op}': the layer has no CRS; say which it uses first (geo-inspect, or --crs-assume)")


def _bool(v, default=False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [x.strip() for x in str(v).split(",") if x.strip()]


# --------------------------------------------------------------------------- measurement CRS

def _metric_projected(crs) -> bool:
    if crs is None or crs.is_geographic or ce.is_web_mercator(crs):
        return False
    try:
        ax = crs.axis_info
        return bool(ax) and all(abs(float(a.unit_conversion_factor) - 1.0) < 1e-9 for a in ax[:2])
    except Exception:
        return False


def measure_crs(gdf, kind: str = "distance", others: list | None = None, ctx: Ctx | None = None) -> tuple[str, str]:
    """The projected CRS a measurement is made in, and why. Never geographic (MAP-OPS-001 raises)."""
    if ctx is not None and ctx.measure_crs:
        chosen, why = ctx.measure_crs, "the measurement CRS set for this run"
    elif _metric_projected(gdf.crs):
        chosen, why = gdf.crs.to_string(), "the layer's own projected CRS (metres)"
    else:
        frames = [gdf] + [o for o in (others or []) if o is not None and len(o)]
        boxes = []
        for f in frames:
            if len(f) == 0 or f.geometry.isna().all():
                continue
            boxes.append(ce._wgs84_bounds(f))
        if not boxes:
            chosen, why = "EPSG:6933", "an equal-area fallback for an empty layer"
        else:
            bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            choice = ce.choose_display(bbox, purpose="thematic" if kind == "area" else "reference")
            chosen = choice["crs"]
            if chosen == "ESRI:54030":        # Robinson is neither equal-area nor conformal; a world distance takes Equal Earth
                chosen = "EPSG:8857"
            why = choice["name"] + " chosen from the data extent"
    bad = ops_lint.lint_measure_crs(chosen)
    if bad:
        raise MeasureCRSError(bad[0]["message"], bad[0])
    return chosen, why


def _in_measure(gdf, crs: str):
    return gdf if gdf.crs is not None and gdf.crs.equals(crs) else gdf.to_crs(crs)


def _back(gdf, src_crs):
    return gdf if gdf.crs is not None and gdf.crs.equals(src_crs) else gdf.to_crs(src_crs)


def _prov(op: str, params: dict, n_in: int, n_out: int, crs: str | None, **extra) -> dict:
    p = {k: v for k, v in params.items() if k != "op"}
    d = {"op": op, "params": p, "rows_in": int(n_in), "rows_out": int(n_out), "measure_crs": crs}
    d.update(extra)
    return d


def _reduce_collections(gdf, family: str | None):
    """GeometryCollections and parts of another family are reduced to `family` (polygons, lines or points). -> (gdf, n_reduced)."""
    import shapely
    if family is None or len(gdf) == 0:
        return gdf, 0
    geoms = gdf.geometry.values
    types = shapely.get_type_id(geoms)
    # 7 = GeometryCollection
    coll = (types == 7)
    want = {"Polygon": ("Polygon", "MultiPolygon"), "LineString": ("LineString", "MultiLineString"), "Point": ("Point", "MultiPoint")}[family]
    mixed = ~coll & ~gdf.geometry.isna().values & ~shapely.is_empty(geoms) & ~gdf.geometry.geom_type.isin(want).values
    mask = coll | mixed
    n = int(mask.sum())
    if not n:
        return gdf, 0
    out = gdf.copy()
    new = out.geometry.values.copy()
    for i in [int(x) for x in mask.nonzero()[0]]:
        parts = _parts_of_family(geoms[i], family)
        new[i] = shapely.union_all(parts) if parts else shapely.from_wkt(f"{family.upper()} EMPTY")
    out[_geom_col(out)] = new
    out = out[~out.geometry.is_empty].reset_index(drop=True)
    return out, n


def _parts_of_family(g, family: str) -> list:
    """Every part of `g` (collections flattened, however deep) that belongs to `family`."""
    import shapely
    stack, parts = [g], []
    while stack:
        x = stack.pop()
        if x is None or x.is_empty:
            continue
        if x.geom_type == "GeometryCollection":
            stack += list(shapely.get_parts(x))
        elif _family(x) == family:
            parts += list(shapely.get_parts(x))
    return parts


def _other_prefixed(other, prefix: str, fields: list | None, geom_name: str):
    """The other layer's attributes with a prefix, limited to `fields` when given."""
    cols = [c for c in other.columns if c != other.geometry.name]
    if fields:
        miss = [f for f in fields if f not in cols]
        if miss:
            raise OpsError(f"field(s) not in the other layer: {', '.join(miss)}. It has: {', '.join(cols)}")
        cols = [c for c in cols if c in fields]
    o = other[cols + [other.geometry.name]].rename(columns={c: f"{prefix}{c}" for c in cols})
    if o.geometry.name != geom_name:
        o = o.rename_geometry(geom_name)
    return o


# --------------------------------------------------------------------------- the ops

def op_buffer(gdf, p: dict, ctx: Ctx) -> OpResult:
    """buffer {distance, units, dissolve, cap, join, rings: [d1, d2, ...], segments}"""
    import geopandas as gpd
    import shapely
    _require_crs(gdf, "buffer")
    units = p.get("units")
    rings = _list(p.get("rings"))
    if rings:
        dists = [to_metres(r, units, "ring distance") for r in rings]
        if any(d[0] <= 0 for d in dists) or any(dists[i][0] >= dists[i + 1][0] for i in range(len(dists) - 1)):
            raise OpsError("ring distances must be positive and increasing")
    else:
        if p.get("distance") is None:
            raise OpsError("buffer needs `distance` (a number with units, e.g. 2 km)")
        dists = [to_metres(p["distance"], units, "buffer distance")]
    unit = dists[0][2]
    cap = {"round": "round", "flat": "flat", "square": "square"}.get(str(p.get("cap", "round")).lower())
    join = {"round": "round", "mitre": "mitre", "miter": "mitre", "bevel": "bevel"}.get(str(p.get("join", "round")).lower())
    if cap is None or join is None:
        raise OpsError("buffer cap must be round, flat or square; join must be round, mitre or bevel")
    segs = int(p.get("segments", BUFFER_SEGMENTS))
    dissolve = _bool(p.get("dissolve"))
    findings = ops_lint.lint_buffer_distance(dists[-1][0])
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    src = gdf.crs
    if rings:
        rows = []
        prev = None
        for i, (dm, dv, du) in enumerate(dists, start=1):
            b = m.geometry.buffer(dm, quad_segs=segs, cap_style=cap, join_style=join)
            if dissolve:
                b = gpd.GeoSeries([shapely.union_all(b.values)], crs=crs)
            ring = b if prev is None else b.difference(prev)
            prev = b
            if dissolve:
                part = gpd.GeoDataFrame({RING_FIELD: [i], "ring_from": [0.0 if i == 1 else dists[i - 2][1]], "ring_to": [dv]}, geometry=ring.values, crs=crs)
            else:
                part = m.copy()
                part[_geom_col(part)] = ring.values
                part[RING_FIELD] = i
                part["ring_from"] = 0.0 if i == 1 else dists[i - 2][1]
                part["ring_to"] = dv
            rows.append(part)
        import pandas as pd
        out = gpd.GeoDataFrame(pd.concat(rows, ignore_index=True), geometry=_geom_col(m), crs=crs)
        note = (f"Concentric rings at {', '.join(_fmt(d[1]) for d in dists)} {unit} around each feature"
                + (", merged where they overlap" if dissolve else "") + f"; the `{RING_FIELD}` field numbers them from the inside. ")
    else:
        dm, dv, du = dists[0]
        b = m.geometry.buffer(dm, quad_segs=segs, cap_style=cap, join_style=join)
        if dissolve:
            out = gpd.GeoDataFrame({"features": [len(m)]}, geometry=[shapely.union_all(b.values)], crs=crs)
        else:
            out = m.copy()
            out[_geom_col(out)] = b.values
        note = f"Each feature buffered by {_fmt(dv)} {du}" + (", merged into one shape" if dissolve else "") + ". "
    out = _back(out, src)
    note += f"The buffer was computed in {crs} ({why}), not in degrees."
    return OpResult(out, [note], findings, _prov("buffer", p, len(gdf), len(out), crs, distance_m=dists[-1][0], units=unit))


def op_centroid(gdf, p: dict, ctx: Ctx) -> OpResult:
    """centroid {inside: bool}; representative_point is centroid with inside: true"""
    _require_crs(gdf, "centroid")
    inside = _bool(p.get("inside")) or p.get("op") == "representative_point"
    crs, why = measure_crs(gdf, "area", ctx=ctx)
    m = _in_measure(gdf, crs)
    out = m.copy()
    out[_geom_col(out)] = (m.geometry.representative_point() if inside else m.geometry.centroid).values
    out = _back(out, gdf.crs)
    note = ("A point inside each feature (a representative point, which stays inside concave shapes)" if inside else "The centroid of each feature") + f", computed in {crs}."
    return OpResult(out, [note], [], _prov("representative_point" if inside else "centroid", p, len(gdf), len(out), crs))


def op_clip(gdf, p: dict, ctx: Ctx) -> OpResult:
    """clip {layer}: cut the layer to the polygons of another layer"""
    import geopandas as gpd
    import shapely
    _require_crs(gdf, "clip")
    other = ctx.other(p.get("layer"), "clip")
    fam = _families(other)
    if fam - {"Polygon"}:
        raise OpsError(f"clip needs a polygon layer; '{p.get('layer')}' holds {', '.join(sorted(fam))}")
    o = other.to_crs(gdf.crs)
    mask = shapely.union_all(o.geometry.values)
    n = len(gdf)
    out = gpd.clip(gdf, mask, keep_geom_type=True)
    out = out[~out.geometry.is_empty & ~out.geometry.isna()].reset_index(drop=True)
    note = f"Clipped to the {len(other)} polygon(s) of '{p.get('layer')}'; {n} feature(s) became {len(out)}, cut at the boundary."
    return OpResult(out, [note], [], _prov("clip", p, n, len(out), None, other_layer=p.get("layer"), other_rows=len(other)))


def _overlay(how: str):
    def fn(gdf, p: dict, ctx: Ctx) -> OpResult:
        import geopandas as gpd
        _require_crs(gdf, how)
        name = p.get("layer")
        other = ctx.other(name, how)
        prefix = p.get("prefix", f"{name}_")
        o = _other_prefixed(other.to_crs(gdf.crs), prefix, _list(p.get("fields")) or None, _geom_col(gdf))
        fam_l, fam_r = _families(gdf), _families(o)
        if len(fam_l) != 1 or len(fam_r) != 1:
            raise OpsError(f"{how} needs one geometry family per layer (got {', '.join(sorted(fam_l)) or 'none'} and {', '.join(sorted(fam_r)) or 'none'}); explode or split by type first")
        family = fam_l.pop()
        n = len(gdf)
        res = gpd.overlay(gdf, o, how=how, keep_geom_type=False)
        res, reduced = _reduce_collections(res, family if how != "intersection" else min((fam_r.pop(), family), key=("Point", "LineString", "Polygon").index))
        findings = ops_lint.lint_collections_reduced(reduced, how)
        word = {"intersection": "the overlap of", "union": "the union of", "difference": "what is left of", "symmetric_difference": "the parts not shared by"}[how]
        note = (f"Overlay ({how}): {word} this layer and '{name}'; {n} and {len(other)} feature(s) became {len(res)}. "
                + (f"The other layer's attributes carry the prefix '{prefix}'. " if how != "difference" else "")
                + (f"{reduced} mixed result(s) were reduced to their {family.lower()} parts. " if reduced else "")).strip()
        return OpResult(res, [note], findings, _prov(how, p, n, len(res), None, other_layer=name, other_rows=len(other), reduced=reduced))
    fn.__doc__ = f"{how} {{layer, prefix, fields}}: gpd.overlay how='{how}'"
    return fn


def op_spatial_join(gdf, p: dict, ctx: Ctx) -> OpResult:
    """spatial_join {layer, predicate, how, max_distance, units, fields, prefix}"""
    import geopandas as gpd
    _require_crs(gdf, "spatial_join")
    name = p.get("layer")
    other = ctx.other(name, "spatial_join")
    pred = str(p.get("predicate", "intersects")).lower()
    how = str(p.get("how", "left")).lower()
    if pred not in PREDICATES:
        raise OpsError(f"predicate must be one of: {', '.join(PREDICATES)}")
    if how not in ("left", "inner"):
        raise OpsError("how must be left or inner")
    prefix = p.get("prefix", f"{name}_")
    o = _other_prefixed(other, prefix, _list(p.get("fields")) or None, _geom_col(gdf))
    n = len(gdf)
    crs = None
    if pred == "nearest":
        crs, why = measure_crs(gdf, "distance", [other], ctx=ctx)
        units = (p.get("units") or "m").lower()
        if units not in UNITS_M:
            raise OpsError(f"unknown unit '{units}'")
        maxd = to_metres(p["max_distance"], units, "max_distance")[0] if p.get("max_distance") is not None else None
        findings = ops_lint.lint_nearest_unbounded(n, maxd)
        res = gpd.sjoin_nearest(_in_measure(gdf, crs), _in_measure(o, crs), how=how, max_distance=maxd, distance_col="__d")
        res[f"distance_{units}"] = res["__d"] / UNITS_M[units]
        res = res.drop(columns=["__d"])
        res = _back(res, gdf.crs)
        note = (f"Each feature joined to the nearest feature of '{name}'" + (f" within {_fmt(maxd / UNITS_M[units])} {units}" if maxd else "")
                + f"; the distance is in `distance_{units}`, measured in {crs} ({why}).")
    else:
        findings = []
        o2 = o.to_crs(gdf.crs)
        dec = None
        clash = (set(gdf.columns) - {_geom_col(gdf)}) & (set(o2.columns) - {_geom_col(o2)})
        if pred in ("intersects", "within", "contains") and _point_polygon(gdf, o2) and not clash:
            # a point-in-polygon join (M16): the pairs from the STRtree or DuckDB, the exact predicate checked by shapely, the rows made
            # by one assembly for both engines, matches of one feature in the other layer's order
            dec = _engine(p, ctx, len(gdf) + len(o2), "pairs", needs_spatial=True)
            res, dec = _pip_join(gdf, o2, pred, how, dec, p)
        else:
            res = gpd.sjoin(gdf, o2, how=how, predicate=pred)
        note = f"Spatial join ({how}): each feature joined to the feature(s) of '{name}' it {pred}; a feature with several matches appears once per match."
    res = res.drop(columns=[c for c in ("index_right", "index_left") if c in res.columns]).reset_index(drop=True)
    findings += ops_lint.lint_join_emptied(n, len(res), "spatial_join")
    note += f" {n} feature(s) became {len(res)}."
    extra = {"engine": dec} if pred != "nearest" and dec else {}
    return OpResult(res, [note], findings, _prov("spatial_join", p, n, len(res), crs, other_layer=name, other_rows=len(other), **extra))


def _pip_join(gdf, o, pred: str, how: str, dec: dict, p: dict):
    """gpd.sjoin's rows for a point-in-polygon join, from sorted pairs: the left rows in order (with no match, once, for how=left), the
    other layer's attributes beside them. -> (frame, engine record)."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    l, r, dec = _pip_pairs(gdf, o, dec, p)
    if pred != "intersects" and len(l):
        test = shapely.within if pred == "within" else shapely.contains
        ok = test(np.asarray(gdf.geometry.values, dtype=object)[l], np.asarray(o.geometry.values, dtype=object)[r])
        l, r = l[ok], r[ok]
    n = len(gdf)
    if how == "left":
        missing = np.setdiff1d(np.arange(n), l)
        at = np.searchsorted(l, missing)
        l, r = np.insert(l, at, missing), np.insert(r, at, -1)
    left = gdf.iloc[l].reset_index(drop=True)
    attrs = pd.DataFrame(o.drop(columns=o.geometry.name)).reset_index(drop=True)
    right = attrs.reindex(r)
    right.index = pd.RangeIndex(len(r))
    out = pd.concat([left, right], axis=1)
    return gpd.GeoDataFrame(out, geometry=_geom_col(gdf), crs=gdf.crs), dec


def op_nearest(gdf, p: dict, ctx: Ctx) -> OpResult:
    """nearest {layer, k, max_distance, units, id_field}: nearest_id and nearest_distance_<units> per feature"""
    import geopandas as gpd
    import numpy as np
    import shapely
    _require_crs(gdf, "nearest")
    name = p.get("layer")
    other = ctx.other(name, "nearest")
    k = int(p.get("k", 1))
    units = (p.get("units") or "m").lower()
    if units not in UNITS_M:
        raise OpsError(f"unknown unit '{units}'")
    maxd = to_metres(p["max_distance"], units, "max_distance")[0] if p.get("max_distance") is not None else None
    idf = p.get("id_field")
    if idf and idf not in other.columns:
        raise OpsError(f"id_field '{idf}' is not in '{name}'")
    n = len(gdf)
    crs, why = measure_crs(gdf, "distance", [other], ctx=ctx)
    findings = ops_lint.lint_nearest_unbounded(n, maxd)
    left = _in_measure(gdf, crs)
    right = _in_measure(other, crs)
    ids = right[idf].values if idf else right.index.values
    self_join = other is gdf or _bool(p.get("exclusive"))
    dcol = f"nearest_distance_{units}"
    if k == 1:
        r = gpd.GeoDataFrame({"__id": ids}, geometry=right.geometry.values, crs=crs)
        res = gpd.sjoin_nearest(left, r, how="left", max_distance=maxd, distance_col="__d", exclusive=self_join)
        res = res[~res.index.duplicated(keep="first")]          # ties: keep the first
        res = res.rename(columns={"__id": "nearest_id"})
        res[dcol] = res["__d"] / UNITS_M[units]
        res = res.drop(columns=[c for c in ("__d", "index_right") if c in res.columns])
    else:
        if n * len(right) > NEAREST_BRUTE_FORCE_MAX:
            raise OpsError(f"k > 1 is computed on all pairs; {n} x {len(right)} is above the limit. Use k: 1, or filter first")
        rg = np.asarray(right.geometry.values, dtype=object)
        lg_all = np.asarray(left.geometry.values, dtype=object)
        rows = []
        for start in range(0, n, 500):
            lg = lg_all[start:start + 500]
            d = shapely.distance(lg[:, None], rg[None, :])
            if self_join:
                for i in range(len(lg)):
                    d[i, start + i] = np.inf
            kk = min(k, d.shape[1])
            idx = np.argpartition(d, kk - 1, axis=1)[:, :kk]
            for i in range(len(lg)):
                order = idx[i][np.argsort(d[i, idx[i]])]
                for rank, j in enumerate(order, start=1):
                    if maxd is not None and d[i, j] > maxd:
                        continue
                    if not np.isfinite(d[i, j]):
                        continue
                    rows.append((start + i, rank, ids[j], d[i, j] / UNITS_M[units]))
        import pandas as pd
        t = pd.DataFrame(rows, columns=["__i", "nearest_rank", "nearest_id", dcol]).set_index("__i")
        res = left.join(t, how="left")
        res.index.name = None
    res = _back(res, gdf.crs).reset_index(drop=True)
    findings += ops_lint.lint_join_emptied(n, int(res["nearest_id"].notna().sum()), "nearest")
    note = (f"For each feature, the nearest {'feature' if k == 1 else f'{k} features'} of '{name}'" + (" (itself excluded)" if self_join else "")
            + (f" within {_fmt(maxd / UNITS_M[units])} {units}" if maxd else "") + f": `nearest_id` and `{dcol}`, straight-line distance measured in {crs} ({why}).")
    return OpResult(res, [note], findings, _prov("nearest", p, n, len(res), crs, other_layer=name, other_rows=len(other)))


def op_dissolve(gdf, p: dict, ctx: Ctx) -> OpResult:
    """dissolve {by, agg: {field: sum|mean|min|max|first|count|median}}; no `by` merges everything into one feature"""
    import geo_convert as gcv
    import geopandas as gpd
    import shapely
    by = p.get("by")
    agg = p.get("agg") or {}
    if isinstance(agg, str):
        agg = gcv.parse_pairs(agg)
    n = len(gdf)
    notes = []
    if by:
        io_notes: list = []
        try:
            out = gcv.dissolve(gdf, by, agg, io_notes)
        except gcv.ConvertError as e:
            raise OpsError(str(e)) from None
        notes += [x.msg[0].upper() + x.msg[1:] + "." for x in io_notes]
    else:
        for col, fn in agg.items():
            if col not in gdf.columns:
                raise OpsError(f"aggregate field '{col}' is not in the data")
            if fn not in gcv.AGG:
                raise OpsError(f"unknown aggregate '{fn}'. Use one of: {', '.join(sorted(gcv.AGG))}")
        data = {c: [getattr(gdf[c], fn)()] for c, fn in agg.items()}
        data["features"] = [n]
        out = gpd.GeoDataFrame(data, geometry=[shapely.union_all(gdf.geometry.values)], crs=gdf.crs)
        notes.append(f"All {n} feature(s) merged into one shape" + (f"; {', '.join(f'{c} ({fn})' for c, fn in agg.items())} aggregated" if agg else "") + ".")
    return OpResult(out, notes, [], _prov("dissolve", p, n, len(out), None))


def op_explode(gdf, p: dict, ctx: Ctx) -> OpResult:
    """explode: one feature per part of a multi-part geometry; `part_index` numbers the parts"""
    n = len(gdf)
    out = gdf.explode(index_parts=True)
    out["part_index"] = [int(i[1]) for i in out.index]
    out = out.reset_index(drop=True)
    return OpResult(out, [f"Multi-part features split into their parts: {n} feature(s) became {len(out)}; `part_index` numbers the parts."], [],
                    _prov("explode", p, n, len(out), None))


def op_dedupe(gdf, p: dict, ctx: Ctx) -> OpResult:
    """dedupe {fields: [...]} or {geometry: true, tolerance, units}: drop exact duplicates"""
    import shapely
    n = len(gdf)
    fields = _list(p.get("fields"))
    crs = None
    if fields:
        miss = [f for f in fields if f not in gdf.columns]
        if miss:
            raise OpsError(f"dedupe field(s) not in the data: {', '.join(miss)}")
        out = gdf.drop_duplicates(subset=fields, keep="first").reset_index(drop=True)
        note = f"Duplicates on {', '.join(fields)} dropped: {n} feature(s) became {len(out)} (the first of each kept)."
    elif _bool(p.get("geometry"), True):
        _require_crs(gdf, "dedupe")
        tol = p.get("tolerance")
        g = gdf
        if tol is not None:
            tm = to_metres(tol, p.get("units"), "tolerance")[0]
            crs, _ = measure_crs(gdf, "distance", ctx=ctx)
            g = _in_measure(gdf, crs)
            geoms = shapely.set_precision(g.geometry.values, tm)
        else:
            geoms = g.geometry.values
        import pandas as pd
        key = shapely.to_wkb(shapely.normalize(geoms), hex=True)
        keep = ~pd.Series(key).duplicated(keep="first").values
        out = gdf[keep].reset_index(drop=True)
        note = ("Features with the same geometry" + (f" (within {_fmt(tm)} m, measured in {crs})" if tol is not None else "")
                + f" dropped: {n} feature(s) became {len(out)}.")
    else:
        raise OpsError("dedupe needs `fields` or `geometry: true`")
    return OpResult(out, [note], [], _prov("dedupe", p, n, len(out), crs))


def op_repair(gdf, p: dict, ctx: Ctx) -> OpResult:
    """repair (alias make_valid): shapely make_valid through data_lint.repair_geometry; collections reduced to the layer's family"""
    import data_lint as dl
    n = len(gdf)
    fams = _families(gdf)
    out, findings = dl.repair_geometry(gdf)
    family = fams.pop() if len(fams) == 1 else None
    out, reduced = _reduce_collections(out, family)
    findings += ops_lint.lint_collections_reduced(reduced, "repair")
    fixed = next((f["detail"]["repaired"] for f in findings if f["id"] == "MAP-DATA-002" and isinstance(f.get("detail"), dict)), 0)
    note = (f"{fixed} invalid geometr{'y was' if fixed == 1 else 'ies were'} repaired with make_valid" if fixed else "Every geometry was already valid; nothing was changed") + \
           (f"; {reduced} result(s) reduced to their {family.lower()} parts" if reduced else "") + "."
    return OpResult(out, [note], findings, _prov("repair", p, n, len(out), None, repaired=int(fixed), reduced=reduced))


def op_simplify(gdf, p: dict, ctx: Ctx) -> OpResult:
    """simplify {tolerance, units}: Douglas-Peucker per feature, in metres"""
    import data_lint as dl
    _require_crs(gdf, "simplify")
    if p.get("tolerance") is None:
        raise OpsError("simplify needs `tolerance` (a distance, e.g. 50 m)")
    tm, tv, tu = to_metres(p["tolerance"], p.get("units"), "tolerance")
    if tm <= 0:
        raise OpsError("the simplify tolerance must be greater than zero")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    v0 = dl.vertex_count(gdf)
    out = m.copy()
    out[_geom_col(out)] = m.geometry.simplify(tm, preserve_topology=True).values
    out = _back(out, gdf.crs)
    v1 = dl.vertex_count(out)
    note = f"Simplified with a tolerance of {_fmt(tv)} {tu} in {crs}: {v0:,} to {v1:,} vertices (per feature; shared borders between neighbours may open slightly). Disclose this on the map."
    return OpResult(out, [note], [], _prov("simplify", p, len(gdf), len(out), crs, tolerance_m=tm))


def _split_at_180(geom):
    """Split one geometry that crosses the antimeridian (lon/lat) into parts on each side. Returns (geometry, n_parts)."""
    import numpy as np
    import shapely
    from shapely.ops import split
    minx, _, maxx, _ = geom.bounds
    if maxx - minx > 180 and -180 <= minx and maxx <= 180:
        # drawn the long way round: move the western lobe east of 180
        g = shapely.transform(geom, lambda c: np.column_stack([np.where(c[:, 0] < 0, c[:, 0] + 360, c[:, 0]), c[:, 1]]))
    else:
        g = geom
    minx, _, maxx, _ = g.bounds
    if not (minx < 180 < maxx):
        return geom, 1
    line = shapely.LineString([(180, -90), (180, 90)])
    parts = []
    for part in shapely.get_parts(g):
        pieces = split(part, line).geoms if part.intersects(line) else [part]
        for piece in pieces:
            if piece.is_empty:
                continue
            if piece.bounds[0] >= 180 - 1e-9:
                piece = shapely.transform(piece, lambda c: np.column_stack([c[:, 0] - 360, c[:, 1]]))
            parts.append(piece)
    fam = _family(geom)
    parts = [x for x in parts if _family(x) == fam]
    if len(parts) <= 1:
        return (parts[0] if parts else geom), 1
    return shapely.union_all(parts) if fam != "Point" else shapely.MultiPoint(parts), len(parts)


def op_antimeridian_split(gdf, p: dict, ctx: Ctx) -> OpResult:
    """antimeridian_split: any geometry crossing +-180 becomes a multi-part geometry with one part per side"""
    _require_crs(gdf, "antimeridian_split")
    g = gdf.to_crs(WGS84) if not gdf.crs.equals(WGS84) else gdf.copy()
    n = len(gdf)
    geoms = list(g.geometry.values)
    changed = 0
    for i, geom in enumerate(geoms):
        if geom is None or geom.is_empty:
            continue
        minx, _, maxx, _ = geom.bounds
        if maxx - minx > 180 or minx < -180 or maxx > 180:
            new, parts = _split_at_180(geom)
            if parts > 1:
                geoms[i] = new
                changed += 1
    out = g.copy()
    out[_geom_col(out)] = geoms
    out = _back(out, gdf.crs)
    note = (f"{changed} feature(s) crossing the antimeridian were split at 180 degrees into one part per side (the fix for MAP-DATA-009)." if changed
            else "No feature crosses the antimeridian; nothing was changed.")
    return OpResult(out, [note], [], _prov("antimeridian_split", p, n, len(out), WGS84, split=changed))


def op_convex_hull(gdf, p: dict, ctx: Ctx) -> OpResult:
    """convex_hull {by}: one hull over all features, or one per value of `by`"""
    import geopandas as gpd
    import shapely
    _require_crs(gdf, "convex_hull")
    by = p.get("by")
    n = len(gdf)
    if by:
        if by not in gdf.columns:
            raise OpsError(f"field '{by}' is not in the data")
        groups = gdf.groupby(by, dropna=False, sort=True)
        rows = [{by: k, "n_features": len(grp), "geometry": shapely.convex_hull(shapely.union_all(grp.geometry.values))} for k, grp in groups]
        out = gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs)
        note = f"One convex hull per value of '{by}': {n} feature(s) became {len(out)} hull(s)."
    else:
        out = gpd.GeoDataFrame({"n_features": [n]}, geometry=[shapely.convex_hull(shapely.union_all(gdf.geometry.values))], crs=gdf.crs)
        note = f"The convex hull of all {n} feature(s)."
    return OpResult(out, [note], [], _prov("convex_hull", p, n, len(out), None))


def op_concave_hull(gdf, p: dict, ctx: Ctx) -> OpResult:
    """concave_hull {ratio: 0..1, by, allow_holes}: shapely.concave_hull, computed in a projected CRS"""
    import geopandas as gpd
    import shapely
    _require_crs(gdf, "concave_hull")
    ratio = float(p.get("ratio", 0.3))
    if not 0 <= ratio <= 1:
        raise OpsError("ratio must be between 0 (tight) and 1 (the convex hull)")
    holes = _bool(p.get("allow_holes"))
    by = p.get("by")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    n = len(gdf)
    if by:
        if by not in gdf.columns:
            raise OpsError(f"field '{by}' is not in the data")
        rows = [{by: k, "n_features": len(grp), "geometry": shapely.concave_hull(shapely.union_all(grp.geometry.values), ratio=ratio, allow_holes=holes)}
                for k, grp in m.groupby(by, dropna=False, sort=True)]
        out = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    else:
        out = gpd.GeoDataFrame({"n_features": [n]}, geometry=[shapely.concave_hull(shapely.union_all(m.geometry.values), ratio=ratio, allow_holes=holes)], crs=crs)
    out = _back(out, gdf.crs)
    note = f"Concave hull (ratio {ratio:g}; 1 is the convex hull)" + (f" per value of '{by}'" if by else "") + f" of {n} feature(s), computed in {crs}."
    return OpResult(out, [note], [], _prov("concave_hull", p, n, len(out), crs))


def op_voronoi(gdf, p: dict, ctx: Ctx) -> OpResult:
    """voronoi {clip_layer | bbox, id_field, extend_km, merge_m, max_km}: best-server cells by straight-line distance (geometry only, no
    propagation model). 4.1.0 (Day 24, D24-26): merge_m first merges sites closer than that into one (masts of one site), and max_km cuts each
    cell to that distance round its site (a lone site no longer owns the desert)."""
    import geopandas as gpd
    import shapely
    _require_crs(gdf, "voronoi")
    if _families(gdf) - {"Point"}:
        raise OpsError("voronoi needs a point layer (use centroid first for areas)")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    merged_note = None
    if p.get("merge_m"):
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
        from scipy.spatial import cKDTree
        mm = to_metres(p["merge_m"], "m", "merge_m")[0]
        xy = shapely.get_coordinates(m.geometry.values)
        pr = cKDTree(xy).query_pairs(mm, output_type="ndarray")
        k, lab = connected_components(coo_matrix((np.ones(len(pr)), (pr[:, 0], pr[:, 1])), shape=(len(m), len(m))), directed=False)
        if k < len(m):
            keep = m.reset_index(drop=True).copy()
            keep["_site"] = lab
            keep["merged_n"] = keep.groupby("_site")["_site"].transform("size")
            cx = keep.groupby("_site").geometry.apply(lambda g: shapely.get_coordinates(g.values).mean(axis=0))
            keep = keep.drop_duplicates("_site").set_index("_site")
            keep = gpd.GeoDataFrame(keep.drop(columns=[keep.geometry.name]), geometry=gpd.points_from_xy([v[0] for v in cx.loc[keep.index]],
                                    [v[1] for v in cx.loc[keep.index]]), crs=crs).reset_index(drop=True)
            merged_note = f"{len(m) - len(keep)} site(s) within {_fmt(mm)} m of another were merged ({len(m)} points, {len(keep)} sites; `merged_n` counts them)"
            m = keep
    n = len(m)
    pts = m.geometry.values
    uniq = len(set(shapely.to_wkb(pts)))
    findings = []
    if uniq < n:
        findings.append(ops_lint.finding("MAP-OPS-009", "WARN", f"{n - uniq} point(s) share a position with another; coincident sites get one cell between them", n - uniq))
    clip_name = p.get("clip_layer")
    bbox = p.get("bbox")
    extend_m = float(p.get("extend_km", 0)) * 1000.0
    if clip_name:
        other = ctx.other(clip_name, "voronoi")
        mask = shapely.union_all(other.to_crs(crs).geometry.values)
    elif bbox:
        b = [float(x) for x in _list(bbox)]
        if len(b) != 4:
            raise OpsError("bbox needs four numbers: minx miny maxx maxy, in the layer's CRS")
        mask = gpd.GeoSeries([shapely.box(*b)], crs=gdf.crs).to_crs(crs).values[0]
    else:
        minx, miny, maxx, maxy = shapely.total_bounds(pts)
        pad = extend_m or max(maxx - minx, maxy - miny) * 0.1 or 1000.0
        mask = shapely.box(minx - pad, miny - pad, maxx + pad, maxy + pad)
    cells = shapely.get_parts(shapely.voronoi_polygons(shapely.MultiPoint(list(pts)), extend_to=mask))
    cells = gpd.GeoDataFrame(geometry=list(cells), crs=crs)
    cells[_geom_col(cells)] = cells.geometry.intersection(mask)
    cells = cells[~cells.geometry.is_empty]
    # attach the generating point's attributes: the cell that contains each point (a point on a shared edge takes the first cell)
    joined = gpd.sjoin(cells, m, how="inner", predicate="intersects")
    joined = joined[~joined.index.duplicated(keep="first")]
    if p.get("max_km"):
        r_m = float(p["max_km"]) * 1000.0
        sites = m.geometry
        gcol = _geom_col(cells)
        joined[gcol] = [c.intersection(sites.loc[i].buffer(r_m, quad_segs=16)) for c, i in zip(joined.geometry.values, joined["index_right"])]
        joined = joined[~joined.geometry.is_empty]
    joined = joined.drop(columns=["index_right"])
    out = _back(gpd.GeoDataFrame(joined, geometry=_geom_col(cells), crs=crs), gdf.crs).reset_index(drop=True)
    idf = p.get("id_field")
    if idf and idf not in out.columns:
        raise OpsError(f"id_field '{idf}' is not in the data")
    note = (f"Best-server (Voronoi) cells: each cell is the area closer to its site than to any other, by straight-line distance in {crs}; "
            f"geometry only, with no propagation model, terrain or antenna pattern. {n} site(s) gave {len(out)} cell(s)"
            + (f", cut to '{clip_name}'" if clip_name else (", cut to the given box" if bbox else ", cut to a box around the sites"))
            + (f", each cut to {float(p['max_km']):g} km round its site" if p.get("max_km") else "") + "."
            + (f" {merged_note}." if merged_note else ""))
    return OpResult(out, [note], findings, _prov("voronoi", p, n, len(out), crs, clip_layer=clip_name))


def _geod():
    from pyproj import Geod
    return Geod(ellps="WGS84")


def op_as_of(gdf, p: dict, ctx: Ctx) -> OpResult:
    """as_of {date}: the features that exist on the date, by their start_date and end_date (start <= date < end; a missing end is open, a missing
    start leaves the feature out); partial dates by OpenHistoricalMap's convention (1.3.0, R1-HIST)"""
    import hist_fetch as hf
    date = p.get("date") or p.get("as_of")
    if not date:
        raise OpsError("as_of needs `date` (YYYY, YYYY-MM or YYYY-MM-DD)")
    try:
        g, notes, fs = hf.as_of_filter(gdf, str(date))
    except hf.HistError as ex:
        raise OpsError(str(ex)) from None
    return OpResult(g, notes, fs, {"op": "as_of", "params": {"date": str(date)}, "rows_in": int(len(gdf)), "rows_out": int(len(g))})


def op_area(gdf, p: dict, ctx: Ctx) -> OpResult:
    """area {field, units: km2|mi2|m2|acres|ha}: geodesic area on the WGS84 ellipsoid"""
    _require_crs(gdf, "area")
    units = _area_unit(p.get("units"))
    fld = p.get("field") or f"area_{units}"
    g = gdf.to_crs(WGS84) if not gdf.crs.equals(WGS84) else gdf
    geod = _geod()
    vals = []
    for geom in g.geometry.values:
        if geom is None or geom.is_empty or _family(geom) != "Polygon":
            vals.append(float("nan"))
            continue
        a, _ = geod.geometry_area_perimeter(geom)
        vals.append(abs(a) / AREA_UNITS_M2[units])
    out = gdf.copy()
    out[fld] = vals
    note = f"`{fld}`: geodesic area in {units} on the WGS84 ellipsoid (pyproj Geod), independent of any map projection."
    return OpResult(out, [note], [], _prov("area", p, len(gdf), len(out), "geodesic (WGS84 ellipsoid)", field=fld, units=units))


def op_length(gdf, p: dict, ctx: Ctx) -> OpResult:
    """length {field, units: m|km|mi|ft}: geodesic length (perimeter for polygons) on the WGS84 ellipsoid"""
    _require_crs(gdf, "length")
    units = (p.get("units") or "km").lower()
    if units not in UNITS_M:
        raise OpsError(f"unknown unit '{units}'. Use one of: {', '.join(UNITS_M)}")
    fld = p.get("field") or f"length_{units}"
    g = gdf.to_crs(WGS84) if not gdf.crs.equals(WGS84) else gdf
    geod = _geod()
    vals = []
    for geom in g.geometry.values:
        if geom is None or geom.is_empty or _family(geom) == "Point":
            vals.append(float("nan"))
            continue
        if _family(geom) == "Polygon":
            _, per = geod.geometry_area_perimeter(geom)
            vals.append(abs(per) / UNITS_M[units])
        else:
            vals.append(geod.geometry_length(geom) / UNITS_M[units])
    out = gdf.copy()
    out[fld] = vals
    note = f"`{fld}`: geodesic length in {units} on the WGS84 ellipsoid (pyproj Geod); for polygons it is the perimeter."
    return OpResult(out, [note], [], _prov("length", p, len(gdf), len(out), "geodesic (WGS84 ellipsoid)", field=fld, units=units))


def _engine(p: dict, ctx: Ctx, rows: int, kind: str, *, needs_spatial: bool = False) -> dict:
    """Which engine runs this op (duck_engine.decide with the op's own `engine:` or the layer's)."""
    import duck_engine as de
    try:
        return de.decide(p.get("engine") or ctx.engine, rows, kind, needs_spatial=needs_spatial)
    except de.EngineError as ex:
        raise OpsError(str(ex)) from None


def _pip_pairs(left, right, dec: dict, p: dict):
    """(left positions, right positions, engine record) of the intersecting pairs, sorted by left then right. DuckDB's spatial join when the
    engine record says duckdb (falling back to the tree when the extension refuses), else shapely's STRtree."""
    import numpy as np
    import shapely
    if dec["engine"] == "duckdb":
        import duck_engine as de
        try:
            l, r = de.pip_pairs(left.geometry.values, right.geometry.values)
            return l, r, dec
        except Exception as ex:  # noqa: BLE001
            dec = {**dec, "engine": "pandas", "why": f"DuckDB could not run the spatial join ({str(ex).splitlines()[0][:120]}); the STRtree ran it"}
    tree = shapely.STRtree(np.asarray(right.geometry.values, dtype=object))
    l, r = tree.query(np.asarray(left.geometry.values, dtype=object), predicate="intersects")
    order = np.lexsort((r, l))
    return l[order], r[order], dec


def _point_polygon(a, b) -> bool:
    """One side points, the other polygons: the shape of join DuckDB's spatial extension runs here."""
    fa, fb = _families(a), _families(b)
    return (fa == {"Point"} and fb == {"Polygon"}) or (fa == {"Polygon"} and fb == {"Point"})


def _points_of(other, name: str, crs):
    """The other layer as points in `crs` (areas and lines become representative points, said in the note)."""
    o = other.to_crs(crs)
    fam = _families(o)
    if fam - {"Point"}:
        o = o.copy()
        o[_geom_col(o)] = o.geometry.representative_point().values
        return o, f"the {', '.join(sorted(fam)).lower()} features of '{name}' were counted by a point inside each"
    return o, None


def op_count_in(gdf, p: dict, ctx: Ctx) -> OpResult:
    """count_in {layer, field, engine}: how many features of another layer fall inside each feature"""
    import numpy as np
    _require_crs(gdf, "count_in")
    name = p.get("layer")
    other = ctx.other(name, "count_in")
    fld = p.get("field") or "count"
    o, extra = _points_of(other, name, gdf.crs)
    # the intersecting pairs (shapely's STRtree, or DuckDB's spatial join for polygons against points, M16), counted per feature
    dec = (_engine(p, ctx, len(gdf) + len(o), "pairs", needs_spatial=True) if _point_polygon(gdf, o)
           else {"engine": "pandas", "why": "not a polygon layer against points", "kind": "pairs", "rows": len(gdf) + len(o)})
    l, _r, dec = _pip_pairs(gdf, o, dec, p)
    out = gdf.copy()
    out[fld] = np.bincount(l, minlength=len(gdf)).astype(int)[:len(gdf)]
    note = f"`{fld}`: the number of features of '{name}' inside each feature (a point on a shared boundary counts for both)" + (f"; {extra}" if extra else "") + "."
    return OpResult(out, [note], [], _prov("count_in", p, len(gdf), len(out), None, other_layer=name, other_rows=len(other), field=fld, engine=dec))


def op_sum_in(gdf, p: dict, ctx: Ctx) -> OpResult:
    """sum_in {layer, value, field, engine}: the sum of another layer's value field over the features inside each feature"""
    import pandas as pd
    _require_crs(gdf, "sum_in")
    name = p.get("layer")
    other = ctx.other(name, "sum_in")
    val = p.get("value")
    if not val or val not in other.columns:
        raise OpsError(f"sum_in needs `value`, a numeric field of '{name}'" + (f" (it has: {', '.join(c for c in other.columns if c != other.geometry.name)})" if other is not None else ""))
    fld = p.get("field") or f"{val}_sum"
    o, extra = _points_of(other, name, gdf.crs)
    o = o[[val, _geom_col(o)]].copy()
    o[val] = pd.to_numeric(o[val], errors="coerce")
    dec = (_engine(p, ctx, len(gdf) + len(o), "pairs", needs_spatial=True) if _point_polygon(gdf, o)
           else {"engine": "pandas", "why": "not a polygon layer against points", "kind": "pairs", "rows": len(gdf) + len(o)})
    l, r, dec = _pip_pairs(gdf, o, dec, p)
    # the values are added feature by feature in the other layer's order, whichever engine found the pairs, so the sums agree to the last digit
    vals = o[val].to_numpy(dtype="float64", na_value=float("nan"))
    sums = pd.Series(vals[r], index=l).groupby(level=0).sum(min_count=1)
    out = gdf.copy()
    out[fld] = sums.reindex(range(len(gdf))).fillna(0.0).values
    note = f"`{fld}`: the sum of `{val}` over the features of '{name}' inside each feature" + (f"; {extra}" if extra else "") + "."
    return OpResult(out, [note], [], _prov("sum_in", p, len(gdf), len(out), None, other_layer=name, other_rows=len(other), field=fld, value=val, engine=dec))


def _areal_interpolate(gdf, other, val: str, mode: str, crs: str):
    """Area-weighted (or centroid) transfer of `val` from the other layer's zones into each feature of gdf. -> Series aligned to gdf.index."""
    import geopandas as gpd
    import pandas as pd
    tgt = _in_measure(gdf, crs)
    src = _in_measure(other, crs)[[val, _geom_col(other)]].copy()
    src[val] = pd.to_numeric(src[val], errors="coerce").fillna(0.0)
    src = src.rename_geometry(_geom_col(tgt)) if src.geometry.name != _geom_col(tgt) else src
    src["__src"] = range(len(src))
    if mode == "centroid":
        pts = src.copy()
        pts[_geom_col(pts)] = pts.geometry.representative_point().values
        j = gpd.sjoin(tgt[[_geom_col(tgt)]], pts, how="left", predicate="intersects")
        j = j[~j.duplicated(subset=["__src"], keep="first") | j["__src"].isna()]      # a zone's value goes to one feature
        return j.groupby(level=0)[val].sum(min_count=1).reindex(tgt.index).fillna(0.0)
    src["__area"] = src.geometry.area
    t = tgt[[_geom_col(tgt)]].copy()
    t["__tgt"] = tgt.index
    inter = gpd.overlay(t, src, how="intersection", keep_geom_type=False)
    if len(inter) == 0:
        return pd.Series(0.0, index=tgt.index)
    inter, _ = _reduce_collections(inter, "Polygon")
    piece = inter.geometry.area
    share = (piece / inter["__area"].where(inter["__area"] > 0, float("nan"))).fillna(0.0)
    inter["__v"] = share * inter[val]
    return inter.groupby("__tgt")["__v"].sum().reindex(tgt.index).fillna(0.0)


def op_population_covered(gdf, p: dict, ctx: Ctx) -> OpResult:
    """population_covered {layer, value, mode: area_weighted|centroid, field}: areal interpolation of a zone layer's value into each feature"""
    _require_crs(gdf, "population_covered")
    name = p.get("layer")
    other = ctx.other(name, "population_covered")
    val = p.get("value")
    if not val or val not in other.columns:
        raise OpsError(f"population_covered needs `value`, a field of '{name}' holding a count (population, households)")
    if _families(other) - {"Polygon"} or _families(gdf) - {"Polygon"}:
        raise OpsError("population_covered needs polygons on both sides (buffer the sites first for a footprint)")
    mode = str(p.get("mode", "area_weighted")).lower()
    if mode not in ("area_weighted", "centroid"):
        raise OpsError("mode must be area_weighted or centroid")
    fld = p.get("field") or f"{val}_covered"
    findings = ops_lint.lint_value_is_rate(val, other[val].values)
    crs, why = measure_crs(gdf, "area", [other], ctx=ctx)
    series = _areal_interpolate(gdf, other, val, mode, crs)
    out = gdf.copy()
    out[fld] = series.values
    import pandas as pd
    total = float(pd.to_numeric(other[val], errors="coerce").fillna(0).sum())
    covered = float(series.sum())
    if mode == "area_weighted":
        note = (f"`{fld}`: `{val}` of '{name}' apportioned to each feature by the share of each zone's area that falls inside it (area-weighted interpolation, "
                f"computed in {crs}, {why}). This assumes {val} is spread evenly within each zone; where zones are much larger than the features the figure is an estimate.")
    else:
        note = f"`{fld}`: `{val}` of each zone of '{name}' counted wholly in the feature that holds the zone's centre (centroid method, computed in {crs})."
    note += f" The features together hold {_fmt(covered)} of the {_fmt(total)} in the zone layer."
    return OpResult(out, [note], findings, _prov("population_covered", p, len(gdf), len(out), crs, other_layer=name, other_rows=len(other), field=fld, value=val,
                                                 mode=mode, source_total=total, covered_total=covered))


def op_coverage_share(gdf, p: dict, ctx: Ctx) -> OpResult:
    """coverage_share {layer, value, field, mode}: the share (0..1) of the other layer's total value inside each feature"""
    import pandas as pd
    q = dict(p)
    q["field"] = "__covered"
    r = op_population_covered(gdf, q, ctx)
    val = p["value"]
    fld = p.get("field") or f"{val}_share"
    total = float(pd.to_numeric(ctx.other(p.get("layer"), "coverage_share")[val], errors="coerce").fillna(0).sum())
    out = r.gdf
    out[fld] = (out["__covered"] / total) if total > 0 else float("nan")
    out = out.drop(columns=["__covered"])
    note = f"`{fld}`: the share of all `{val}` in '{p.get('layer')}' ({_fmt(total)}) that lies inside each feature, from the same apportionment: " + r.notes[0]
    prov = dict(r.provenance, op="coverage_share", params={k: v for k, v in p.items() if k != "op"}, field=fld)
    return OpResult(out, [note], r.findings, prov)


def op_overlap(gdf, p: dict, ctx: Ctx) -> OpResult:
    """overlap {layer: self|<id>, units}: overlap_count and overlap_area_<units> per feature (sector wedges, footprints)"""
    import numpy as np
    import shapely
    _require_crs(gdf, "overlap")
    name = p.get("layer", "self")
    other = gdf if name in (None, "self") else ctx.other(name, "overlap")
    units = _area_unit(p.get("units"))
    if _families(gdf) - {"Polygon"} or _families(other) - {"Polygon"}:
        raise OpsError("overlap needs polygons (wedges or footprints) on both sides")
    crs, why = measure_crs(gdf, "area", [other] if other is not gdf else None, ctx=ctx)
    a = _in_measure(gdf, crs)
    b = _in_measure(other, crs)
    ag, bg = a.geometry.values, b.geometry.values
    tree = shapely.STRtree(bg)
    li, ri = tree.query(ag, predicate="intersects")
    if other is gdf:
        keep = li != ri
        li, ri = li[keep], ri[keep]
    inter = shapely.intersection(ag[li], bg[ri]) if len(li) else np.array([], dtype=object)
    areas = shapely.area(inter) if len(li) else np.array([])
    real = areas > 0
    li, areas = li[real], areas[real]
    count = np.bincount(li, minlength=len(a))
    total = np.bincount(li, weights=areas, minlength=len(a)) / AREA_UNITS_M2[units]
    out = gdf.copy()
    out["overlap_count"] = count.astype(int)
    out[f"overlap_area_{units}"] = total
    note = (f"`overlap_count` and `overlap_area_{units}`: for each feature, how many {'other features of this layer' if other is gdf else f'features of {name!r}'} it overlaps "
            f"and the overlapping area (summed over the pairs, so a spot under three features counts twice for each), measured in {crs}.")
    return OpResult(out, [note], [], _prov("overlap", p, len(gdf), len(out), crs, other_layer=name, units=units))


def op_grid(gdf, p: dict, ctx: Ctx) -> OpResult:
    """grid {size, units, shape: square|hex, agg: count|sum|mean|median|max, value, extent: layer|bbox, min_count}"""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    _require_crs(gdf, "grid")
    shape = str(p.get("shape", "square")).lower()
    agg = str(p.get("agg", "count")).lower()
    val = p.get("value")
    if agg != "count" and (not val or val not in gdf.columns):
        raise OpsError(f"grid agg '{agg}' needs `value`, a numeric field of the layer")
    if agg not in ("count", "sum", "mean", "median", "max", "min"):
        raise OpsError("agg must be count, sum, mean, median, max or min")
    min_count = int(p.get("min_count", 1))
    n = len(gdf)
    if shape == "hex":
        import hexbin
        res = p.get("resolution", "auto")
        sm = to_metres(p["size"], p.get("units"), "size")[0] if p.get("size") is not None and res == "auto" else None
        cells, hnotes, hprov = hexbin.aggregate(gdf, resolution=hexbin.resolution_for_size(sm) if sm else res, value_field=val, agg=agg, min_count=min_count)
        findings = ops_lint.lint_grid_cells(len(cells))
        out = cells.to_crs(gdf.crs) if gdf.crs is not None and not cells.crs.equals(gdf.crs) else cells
        prov = _prov("grid", p, n, len(out), None, shape="hex", **hprov)
        return OpResult(out, hnotes, findings, prov)
    if shape != "square":
        raise OpsError("shape must be square or hex")
    if p.get("size") is None:
        raise OpsError("grid needs `size` (a distance, e.g. 1 km)")
    sm, sv, su = to_metres(p["size"], p.get("units"), "size")
    if sm <= 0:
        raise OpsError("size must be greater than zero")
    crs, why = measure_crs(gdf, "area", ctx=ctx)
    m = _in_measure(gdf, crs)
    pts = m.copy()
    if _families(m) - {"Point"}:
        pts[_geom_col(pts)] = m.geometry.representative_point().values
    extent = p.get("extent", "layer")
    if isinstance(extent, (list, tuple)) or (isinstance(extent, str) and extent not in ("layer",)):
        b = [float(x) for x in _list(extent)]
        if len(b) != 4:
            raise OpsError("extent must be `layer` or a bbox minx miny maxx maxy in the layer's CRS")
        minx, miny, maxx, maxy = gpd.GeoSeries([shapely.box(*b)], crs=gdf.crs).to_crs(crs).total_bounds
    else:
        minx, miny, maxx, maxy = pts.total_bounds
    nx = max(1, int(math.ceil((maxx - minx) / sm))) if np.isfinite(maxx - minx) else 0
    ny = max(1, int(math.ceil((maxy - miny) / sm))) if np.isfinite(maxy - miny) else 0
    findings = ops_lint.lint_grid_cells(nx * ny)
    if nx * ny > ops_lint.GRID_CELLS_MAX:
        raise OpsError(f"the grid would have {nx * ny:,} cells (above {ops_lint.GRID_CELLS_MAX:,}); use a larger size")
    xs = pts.geometry.x.values
    ys = pts.geometry.y.values
    ix = np.clip(np.floor((xs - minx) / sm).astype(int), 0, nx - 1) if nx else np.array([], dtype=int)
    iy = np.clip(np.floor((ys - miny) / sm).astype(int), 0, ny - 1) if ny else np.array([], dtype=int)
    inside = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
    key = pd.Series(np.where(inside, iy * nx + ix, -1), index=pts.index)
    if agg == "median" and str(p.get("engine") or ctx.engine or "auto").lower() == "auto":
        dec = {"engine": "pandas", "why": "medians per cell are faster in pandas (measured)", "kind": "grid", "rows": int(n)}
    else:
        dec = _engine(p, ctx, n, "grid")
    table = None
    if dec["engine"] == "duckdb":
        import duck_engine as de
        try:              # the GROUP BY in DuckDB (M16): counts, Kahan sums and means, medians, minima and maxima, the same numbers as pandas
            table = de.grid_table(key.to_numpy(), pd.to_numeric(pts[val], errors="coerce").to_numpy(dtype="float64", na_value=np.nan) if val else None, agg, val)
        except Exception as ex:  # noqa: BLE001
            dec = {**dec, "engine": "pandas", "why": f"DuckDB could not run the aggregation ({str(ex).splitlines()[0][:120]}); pandas ran it"}
    if table is None:
        df = pd.DataFrame({"cell": key})
        if val:
            df[val] = pd.to_numeric(pts[val], errors="coerce")
        df = df[df["cell"] >= 0]
        grp = df.groupby("cell")
        table = grp.size().rename("count").to_frame()
        if agg != "count":
            table[f"{val}_{agg}"] = getattr(grp[val], agg)()
    table = table[table["count"] >= min_count]
    cells_ix = table.index.values
    cx, cy = cells_ix % nx, cells_ix // nx
    geoms = [shapely.box(minx + i * sm, miny + j * sm, minx + (i + 1) * sm, miny + (j + 1) * sm) for i, j in zip(cx, cy, strict=True)]
    out = gpd.GeoDataFrame({"cell": cells_ix.astype(int), "col": cx.astype(int), "row": cy.astype(int), **{c: table[c].values for c in table.columns}}, geometry=geoms, crs=crs)
    out = _back(out, gdf.crs).reset_index(drop=True)
    note = (f"Square grid of {_fmt(sv)} {su} cells laid in {crs} ({why}) over {'the layer' if extent == 'layer' else 'the given box'}: "
            f"{n} feature(s) counted into {len(out)} occupied cell(s)" + (f" (cells with fewer than {min_count} are left out)" if min_count > 1 else "")
            + (f"; `{val}_{agg}` is the {agg} of `{val}` per cell" if agg != "count" else "") + ".")
    return OpResult(out, [note], findings, _prov("grid", p, n, len(out), crs, shape="square", size_m=sm, cells=len(out), engine=dec))


def op_hexbin(gdf, p: dict, ctx: Ctx) -> OpResult:
    """hexbin {resolution: int|auto, value, agg, min_count}: H3 cells with count and an aggregate"""
    import hexbin
    _require_crs(gdf, "hexbin")
    n = len(gdf)
    cells, notes, hprov = hexbin.aggregate(gdf, resolution=p.get("resolution", "auto"), value_field=p.get("value"), agg=str(p.get("agg", "count")), min_count=int(p.get("min_count", 1)))
    findings = ops_lint.lint_grid_cells(len(cells))
    out = cells.to_crs(gdf.crs) if not cells.crs.equals(gdf.crs) else cells
    return OpResult(out, notes, findings, _prov("hexbin", p, n, len(out), None, **hprov))


# --------------------------------------------------------------------------- raster and terrain (M12)

def _local_path(value, ctx: Ctx) -> Path:
    p = Path(str(value))
    if not p.is_absolute() and ctx.base is not None:
        p = Path(ctx.base) / p
    return p


def _wgs84_bounds(gdf, pad_frac: float = 0.0, pad_m: float = 0.0):
    g = gdf.to_crs(WGS84) if gdf.crs is not None and not gdf.crs.equals(WGS84) else gdf
    x0, y0, x1, y1 = (float(v) for v in g.total_bounds)
    dx, dy = (x1 - x0) * pad_frac, (y1 - y0) * pad_frac
    lat = (y0 + y1) / 2
    dlat = pad_m / 110_540.0
    dlon = pad_m / max(111_320.0 * math.cos(math.radians(lat)), 1.0)
    return (max(-180.0, x0 - dx - dlon), max(-89.9, y0 - dy - dlat), min(180.0, x1 + dx + dlon), min(89.9, y1 + dy + dlat))


def op_raster_zonal(gdf, p: dict, ctx: Ctx) -> OpResult:
    """raster_zonal {raster: path | library id, stat: sum|mean|min|max|count|majority|median|std, band, field, all_touched}: a raster statistic per polygon"""
    import raster_io as rio
    _require_crs(gdf, "raster_zonal")
    src = p.get("raster")
    if not src:
        raise OpsError("raster_zonal needs `raster:` (a GeoTIFF path, or a library id such as ref.statistic.world.population.ghsl)")
    if _families(gdf) - {"Polygon"}:
        raise OpsError("raster_zonal reads a raster under polygons; buffer points or lines first")
    stat = str(p.get("stat", "sum")).lower()
    fetch_rec, entry, findings = None, None, []
    if str(src).startswith("ref."):
        import raster_fetch as rf
        fetch = ctx.raster_fetch or (lambda eid, b: rf.raster_for(eid, b))
        try:
            r = fetch(str(src), _wgs84_bounds(gdf, 0.02))
        except rio.RasterError as ex:
            raise OpsError(f"raster_zonal: {ex}") from None
        path, fetch_rec, entry = Path(r["path"]), r.get("record"), r.get("entry")
        findings += list(r.get("findings") or [])
    else:
        path = _local_path(src, ctx)
        if not path.is_file():
            raise OpsError(f"raster_zonal: raster not found: {src}")
    try:
        out, prov = rio.zonal(path, gdf, stat=stat, band=int(p.get("band", 1)), all_touched=_bool(p.get("all_touched")), field=p.get("field"),
                              nodata=p.get("nodata"))
    except rio.RasterError as ex:
        raise OpsError(f"raster_zonal: {ex}") from None
    note = prov["note"]
    if entry:
        att = (entry.get("license") or {}).get("attribution_text")
        prov["library_ref"], prov["attribution"] = entry.get("id"), att
        note += f" Source: {(entry.get('source') or {}).get('publisher', entry.get('title'))}, {entry.get('title')}" + (f" ({att})" if att else "") + "."
    if fetch_rec:
        prov["fetch"] = {k: v for k, v in fetch_rec.items() if k != "files"}
    prov["raster"] = str(src)
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f_:
        for chunk in iter(lambda: f_.read(1 << 20), b""):
            h.update(chunk)
    prov["raster_sha256"] = h.hexdigest()
    pv = _prov("raster_zonal", p, len(gdf), len(out), None, **{k: v for k, v in prov.items() if k not in ("op", "measure_crs")})
    return OpResult(out, [note], findings, pv)              # counted on the raster's own grid (raster_crs); nothing is measured in a display CRS


def _dem_for_op(gdf, p: dict, ctx: Ctx, pad_m: float, what: str):
    """The DEM a terrain op reads: `dem:` (a path), else ctx.dem for the layer's window (a MapSpec's terrain source, or a fetch)."""
    if p.get("dem") and str(p["dem"]) not in ("auto",):
        path = _local_path(p["dem"], ctx)
        if not path.is_file():
            raise OpsError(f"{what}: DEM not found: {p['dem']}")
        return str(path), {"source": "user", "path": path.name}
    if ctx.dem is None:
        raise OpsError(f"{what} needs a DEM: give `dem: <file.tif>` (terrain.py fetch writes one)")
    path, rec = ctx.dem(_wgs84_bounds(gdf, 0.05, pad_m), float(p.get("dem_res_m", 30)))
    return str(path), rec or {}


def _dem_name(rec: dict, path: str) -> str:
    return {"3dep": "USGS 3DEP", "terrarium": "the AWS Terrain Tiles (Terrarium)"}.get((rec or {}).get("source"), Path(path).name)


def _height(row: dict, p: dict, end: str, default: float) -> float:
    h = p.get("heights") or {}
    fld = h.get(f"{end}_field")
    if fld:
        v = row.get(fld)
        try:
            return float(v)
        except (TypeError, ValueError):
            raise OpsError(f"line_of_sight: heights.{end}_field '{fld}' is not a number for every site (got {v!r})") from None
    if h.get(f"{end}_m") is not None:
        return float(h[f"{end}_m"])
    return default


def op_line_of_sight(gdf, p: dict, ctx: Ctx) -> OpResult:
    """line_of_sight {to: layer (optional; else every pair of this layer), heights: {a_field|a_m, b_field|b_m}, freq_ghz, k, dem, match: {a_field, b_field},
    max_links}: one line per link with clear or obstructed against a bare-earth DEM"""
    import geopandas as gpd
    import terrain as te
    from shapely.geometry import LineString
    _require_crs(gdf, "line_of_sight")
    if _families(gdf) - {"Point"}:
        raise OpsError("line_of_sight runs between point sites; this layer holds " + ", ".join(sorted(_families(gdf))).lower())
    a = gdf.to_crs(WGS84) if not gdf.crs.equals(WGS84) else gdf
    to = p.get("to")
    b = ctx.other(to, "line_of_sight").to_crs(WGS84) if to else a
    if to and _families(b) - {"Point"}:
        raise OpsError(f"line_of_sight: layer '{to}' must hold points")
    k = float(p.get("k", te.K_DEFAULT))
    f = p.get("freq_ghz")
    f = float(f) if f is not None else None
    cap = int(p.get("max_links", 500))
    m = p.get("match") or {}
    ar, br = a.reset_index(drop=True), b.reset_index(drop=True)
    pairs = []
    if m.get("a_field"):
        af, bf = m["a_field"], m.get("b_field")
        if af not in ar.columns or (bf and bf not in br.columns):
            raise OpsError(f"line_of_sight: match fields {af!r} / {bf!r} are not fields of the layers")
        key = {str(v): j for j, v in enumerate(br[bf] if bf else br.index)}
        for i, v in enumerate(ar[af]):
            if str(v) in key:
                pairs.append((i, key[str(v)]))
    elif to:
        pairs = [(i, j) for i in range(len(ar)) for j in range(len(br))]
    else:
        pairs = [(i, j) for i in range(len(ar)) for j in range(i + 1, len(ar))]
    if not pairs:
        raise OpsError("line_of_sight: no link to test (check `to`, `match` and that the layers hold sites)")
    if len(pairs) > cap:
        raise OpsError(f"line_of_sight: {len(pairs):,} links is more than max_links ({cap}); name the links with match: {{a_field, b_field}} or raise max_links")
    dem, rec = _dem_for_op(_sites(ar, br), p, ctx, 2000.0, "line_of_sight")
    rows = []
    arec, brec = ar.drop(columns=ar.geometry.name).to_dict("records"), br.drop(columns=br.geometry.name).to_dict("records")
    for i, j in pairs:
        pa, pb = ar.geometry.iloc[i], br.geometry.iloc[j]
        ha, hb = _height(arec[i], p, "a", 30.0), _height(brec[j], p, "b", 30.0)
        try:
            r = te.line_of_sight(dem, (pa.x, pa.y), (pb.x, pb.y), ha, hb, k, f)
        except te.TerrainError as ex:
            raise OpsError(f"line_of_sight: link {i}-{j}: {ex}") from None
        ida = _site_id(arec[i], p.get("id_field"), None, i)
        idb = _site_id(brec[j], p.get("id_field"), m.get("b_field"), j)
        rows.append({"a": str(ida), "b": str(idb), "status": "clear" if r["clear"] else "obstructed", "clear": bool(r["clear"]), "length_m": round(r["length_m"], 1),
                     "h_a_m": ha, "h_b_m": hb, "min_clearance_m": round(r["min_clearance_m"], 1), "obstruction_m": round(r["obstruction_height_m"], 1),
                     "obstruction_at_m": None if r["obstruction_distance_m"] is None else round(r["obstruction_distance_m"], 0),
                     "required_h_a_m": r["required_h_a_m"], "fresnel_ok": r.get("fresnel_ok"),
                     "fresnel_ratio": None if r.get("fresnel_clearance_ratio") is None else round(r["fresnel_clearance_ratio"], 3),
                     "verdict": te.verdict_line(r), "geometry": LineString([(pa.x, pa.y), (pb.x, pb.y)])})
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84).to_crs(gdf.crs)
    res = te.dem_resolution_m(dem)
    n_clear = int(sum(1 for r in rows if r["clear"]))
    hdesc = ", ".join(f"{k_}: {v}" for k_, v in (p.get("heights") or {}).items()) or "30 m at both ends"
    note = (f"Line of sight for {len(rows)} link(s): {n_clear} clear, {len(rows) - n_clear} obstructed, on {_dem_name(rec, dem)} (about {res:.0f} m cells), "
            f"antenna heights {hdesc} above the ground, earth curvature with k = {k:.3g}" + (f", first Fresnel zone at {f:g} GHz (60 percent clear needed)" if f else "")
            + ". Bare earth: buildings, trees and other clutter are not in the DEM, so a clear link is optimistic; a ridge narrower than a cell can be missed.")
    return OpResult(out, [note], [], _prov("line_of_sight", p, len(gdf), len(out), None, to=to, links=len(rows), clear=n_clear, k_factor=k, freq_ghz=f,
                                           dem=Path(dem).name, dem_resolution_m=res, dem_source=(rec or {}).get("source"), caveat="bare earth"))


def _site_id(rec: dict, id_field, prefer, i):
    """A site's name for a link: the op's id_field, the matched field, a usual id column (site_id, id, site, name, anything ending _id), else its row."""
    for c in (id_field, prefer, "site_id", "id", "site", "name") + tuple(k for k in rec if str(k).lower().endswith("_id")):
        if c and c in rec and rec[c] is not None and str(rec[c]) != "":
            return rec[c]
    return i


def _sites(a, b):
    """Both ends' points in one frame (the window a DEM is fetched for)."""
    import geopandas as gpd
    return a if b is a else gpd.GeoDataFrame(geometry=list(a.geometry) + list(b.geometry), crs=a.crs)


def op_viewshed(gdf, p: dict, ctx: Ctx) -> OpResult:
    """viewshed {h_m, radius_m, step_deg, target_h_m, k, dem}: the ground visible from each point, as polygons"""
    import geopandas as gpd
    import terrain as te
    _require_crs(gdf, "viewshed")
    if _families(gdf) - {"Point"}:
        raise OpsError("viewshed is seen from points (sites); this layer holds " + ", ".join(sorted(_families(gdf))).lower())
    if len(gdf) > 50:
        raise OpsError(f"viewshed from {len(gdf)} points is too many for one map (at most 50); filter the sites first")
    h = float(p.get("h_m", 30.0))
    rad = float(p.get("radius_m", 5000.0))
    step = float(p.get("step_deg", 1.0))
    th = float(p.get("target_h_m", 0.0))
    k = float(p.get("k", te.K_DEFAULT))
    a = gdf.to_crs(WGS84) if not gdf.crs.equals(WGS84) else gdf
    dem, rec = _dem_for_op(a, p, ctx, rad * 1.1, "viewshed")
    rows, geoms, method = [], [], None
    attrs = a.drop(columns=a.geometry.name).to_dict("records")
    for i, pt in enumerate(a.geometry):
        try:
            v = te.viewshed(dem, (pt.x, pt.y), h, rad, step, target_h_m=th, k_factor=k)
        except te.TerrainError as ex:
            raise OpsError(f"viewshed: point {i}: {ex}") from None
        rows.append({**attrs[i], "h_m": h, "radius_m": rad, "visible_km2": round(v["visible_area_km2"], 3), "visible_share": round(v["visible_share_of_rays"], 3)})
        geoms.append(v["geometry"])
        method = v["method"]
    out = gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84).to_crs(gdf.crs)
    res = te.dem_resolution_m(dem)
    note = (f"Visible ground from {len(rows)} point(s) at {h:g} m above the ground within {rad / 1000:g} km, on {_dem_name(rec, dem)} (about {res:.0f} m cells): {method}."
            " Bare earth: buildings and trees are not in the DEM, so more ground shows as visible than is.")
    return OpResult(out, [note], [], _prov("viewshed", p, len(gdf), len(out), None, h_m=h, radius_m=rad, step_deg=step, k_factor=k, dem=Path(dem).name,
                                           dem_resolution_m=res, dem_source=(rec or {}).get("source"), caveat="bare earth"))


def op_geocode(gdf, p: dict, ctx: Ctx) -> OpResult:
    """geocode {address: [cols] | col, provider: census|nominatim, purpose: sites|venues|businesses, country, terms_acknowledged}: a table's addresses into points (M13)"""
    import geocode
    n = len(gdf)
    try:
        out, rep = geocode.geocode_table(gdf, p.get("address"), str(p.get("provider") or "census"), p.get("purpose"), str(p.get("country") or "us"), _bool(p.get("terms_acknowledged")))
    except geocode.GeocodeRefused as e:
        raise OpsError("op 'geocode' refused: " + "; ".join(f"{f['id']} {f['message']}" for f in e.findings)) from None
    except geocode.GeocodeError as e:
        raise OpsError(f"op 'geocode': {e}") from None
    kept = out[out.geometry.notna()].copy()
    notes = [f"Geocoded {rep['matched']} of {rep['rows']} addresses with the {rep['provider']} geocoder (purpose: {rep['purpose']}); {rep['unmatched_total']} unmatched row(s) left out. {rep['attribution']}"]
    findings = list(rep["findings"])
    prov = _prov("geocode", {k: v for k, v in p.items() if k != "op"}, n, len(kept), None, provider=rep["provider"], endpoint=rep["endpoint"], match_rate=rep["match_rate"], unmatched=rep["unmatched"][:50],
                 attribution=rep["attribution"], terms=rep["terms"], quality_counts=rep["quality_counts"])
    return OpResult(kept, notes, findings, prov)


# --------------------------------------------------------------------------- 1.16.0: building context, cutting, thinning, reconciling

def op_fill_holes(gdf, p: dict, ctx: Ctx) -> OpResult:
    """fill_holes {max_area, min_area, units}: fill polygons' holes (all of them, or those in an area range): a stadium bowl, a courtyard"""
    import massing as ms
    _require_crs(gdf, "fill_holes")
    if _families(gdf) - {"Polygon"}:
        raise OpsError("fill_holes needs a polygon layer")
    u = _area_unit(p.get("units") or "m2") if p.get("units") else "m2"
    k = AREA_UNITS_M2[u] if u in AREA_UNITS_M2 else 1.0
    crs, why = measure_crs(gdf, "area", ctx=ctx)
    m = _in_measure(gdf, crs)
    mx = None if p.get("max_area") is None else float(p["max_area"]) * k
    mn = None if p.get("min_area") is None else float(p["min_area"]) * k
    geoms, n = [], 0
    for g in m.geometry.values:
        g2, c = ms.fill_holes(g, mx, mn)
        geoms.append(g2)
        n += c
    out = m.copy()
    out[_geom_col(out)] = geoms
    out = _back(out, gdf.crs)
    rng = "" if mx is None and mn is None else f" (holes of {_fmt(mn or 0)} to {_fmt(mx) if mx is not None else 'any'} m2)"
    return OpResult(out, [f"Filled {n} hole(s){rng} in {len(gdf)} polygon(s), measured in {crs}."], [], _prov("fill_holes", p, len(gdf), len(out), crs, filled=n))


def op_massing(gdf, p: dict, ctx: Ctx) -> OpResult:
    """massing {form: solid|courtyard|stepped|raked, height_m|height_field, base_m|base_field, tiers, opening, row_m, rake, start_m, top_m,
    sides, slice_deg, wall_m, wall_above_m}: the 3D context of a building from its footprint, as parts with base_m and height_m (massing.py)"""
    import geopandas as gpd
    import massing as ms
    _require_crs(gdf, "massing")
    if _families(gdf) - {"Polygon"}:
        raise OpsError("massing needs a polygon layer of footprints")
    form = str(p.get("form") or "solid")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    opening = None
    if p.get("opening"):
        import shapely
        opening = shapely.union_all(_in_measure(ctx.other(p["opening"], "massing"), crs).geometry.values)
    hf, bf = p.get("height_field"), p.get("base_field")
    for f in (hf, bf):
        if f and f not in gdf.columns:
            raise OpsError(f"massing: the field '{f}' is not a column of the layer")
    rows = []
    attrs = [c for c in m.columns if c != _geom_col(m)]
    for i, (g, rec) in enumerate(zip(m.geometry.values, m[attrs].to_dict("records"))):
        h = _num_or_none(rec.get(hf)) if hf else None
        b = _num_or_none(rec.get(bf)) if bf else None
        try:
            parts = ms.mass(g, p, opening=opening.intersection(g.buffer(1.0)) if opening is not None else None, height=h,
                            base=b if b is not None else float(p.get("base_m") or 0.0))
        except ms.MassingError as ex:
            raise OpsError(f"massing (feature {i}): {ex}") from None
        for part in parts:
            rows.append({**rec, **part})
    if not rows:
        raise OpsError("massing made no parts")
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    out = _back(out, gdf.crs)
    given = (hf or p.get("height_m") is not None) and form in ("solid", "courtyard")
    findings = [] if given else [{"id": "MAP-OPS-010", "severity": "INFO",
                                  "message": f"massing ({form}): the heights are modelled from the parameters, not surveyed; say so in the map's note"}]
    note = (f"Massing ({form}) in {crs}: {len(gdf)} footprint(s) became {len(out)} part(s) with base_m and height_m "
            f"({', '.join(f'{k} {v}' for k, v in out['part'].value_counts().items())}); extrude them with height_field: height_m, base_field: base_m.")
    return OpResult(out, [note], findings, _prov("massing", p, len(gdf), len(out), crs, form=form))


def _num_or_none(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def op_split_by(gdf, p: dict, ctx: Ctx) -> OpResult:
    """split_by {layer, id_field, field}: cut every feature by the polygons of another layer (map pieces, districts), each part tagged with
    its piece; an STRtree finds the pairs, so thousands of lines into dozens of pieces take seconds, not minutes"""
    import numpy as np
    import shapely
    _require_crs(gdf, "split_by")
    other = ctx.other(p.get("layer"), "split_by")
    if _families(other) - {"Polygon"}:
        raise OpsError("split_by needs a polygon layer to cut by")
    idf = p.get("id_field")
    if idf and idf not in other.columns:
        raise OpsError(f"split_by: id_field '{idf}' is not a column of '{p.get('layer')}'")
    fld = p.get("field") or "piece"
    o = other.to_crs(gdf.crs)
    pieces = np.asarray(o.geometry.values, dtype=object)
    tree = shapely.STRtree(pieces)
    gi, pi = tree.query(np.asarray(gdf.geometry.values, dtype=object), predicate="intersects")
    parts = shapely.intersection(np.asarray(gdf.geometry.values, dtype=object)[gi], pieces[pi])
    fam = _families(gdf)
    keep = ~shapely.is_empty(parts)
    out = gdf.iloc[gi[keep]].copy()
    out[_geom_col(out)] = parts[keep]
    out[fld] = (o[idf].to_numpy()[pi[keep]] if idf else pi[keep])
    out = out.reset_index(drop=True)
    if len(fam) == 1:
        out, _red = _reduce_collections(out, fam.pop())
    note = f"Split {len(gdf):,} feature(s) by the {len(o)} polygon(s) of '{p.get('layer')}': {len(out):,} part(s), each with `{fld}`."
    return OpResult(out, [note], [], _prov("split_by", p, len(gdf), len(out), None, other_layer=p.get("layer"), other_rows=len(o)))


def op_thin_lines(gdf, p: dict, ctx: Ctx) -> OpResult:
    """thin_lines {cell, max_per_cell, rank_field, keep_share}: a dense line network thinned for its scale (Day 14 issue 5: Roman roads round
    Rome drew as a blob). Lines go in rank order (rank_field, highest first, else longest); a line is kept while at least keep_share of the
    grid cells it passes through still hold fewer than max_per_cell kept lines"""
    import numpy as np
    _require_crs(gdf, "thin_lines")
    if _families(gdf) - {"LineString"}:
        raise OpsError("thin_lines needs a line layer")
    if p.get("cell") is None:
        raise OpsError("thin_lines needs `cell` (a distance, e.g. 2 km: about a millimetre or two on the sheet at the map's scale)")
    cm, cv, cu = to_metres(p["cell"], p.get("units"), "cell")
    cap = int(p.get("max_per_cell") or 2)
    share = float(p.get("keep_share") if p.get("keep_share") is not None else 0.5)
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    lengths = m.geometry.length.to_numpy()
    rf = p.get("rank_field")
    if rf and rf not in gdf.columns:
        raise OpsError(f"thin_lines: rank_field '{rf}' is not a column of the layer")
    import pandas as pd
    rank = pd.to_numeric(gdf[rf], errors="coerce").fillna(-np.inf).to_numpy() if rf else lengths
    order = np.lexsort((-lengths, -rank))
    counts: dict = {}
    keep = np.zeros(len(m), dtype=bool)
    for i in order:
        g = m.geometry.values[i]
        if g is None or g.is_empty:
            continue
        n = max(2, int(math.ceil(lengths[i] / (cm / 2))) + 1)
        pts = [g.interpolate(t, normalized=True) for t in np.linspace(0, 1, n)]
        cells = {(int(math.floor(q.x / cm)), int(math.floor(q.y / cm))) for q in pts}
        free = sum(1 for c in cells if counts.get(c, 0) < cap)
        if free >= share * len(cells):
            keep[i] = True
            for c in cells:
                counts[c] = counts.get(c, 0) + 1
    out = gdf[keep].reset_index(drop=True)
    note = (f"Thinned the lines on a {_fmt(cv)} {cu} grid in {crs}, at most {cap} a cell, by {rf or 'length'}: {len(gdf):,} to {len(out):,} "
            f"({len(gdf) - len(out):,} left out). Say so in the map's note.")
    return OpResult(out, [note], [], _prov("thin_lines", p, len(gdf), len(out), crs, cell_m=cm))


def op_reconcile(gdf, p: dict, ctx: Ctx) -> OpResult:
    """reconcile {layer, tolerance, threshold, field, overrides}: the share of each feature inside another layer whose edges disagree with
    it (two sources of one boundary, Day 14 issue 3), the other layer widened by `tolerance` first; `<field>` the share and `<field>_in` the
    share at or over threshold; overrides {id: true|false} set a feature by hand, and are listed in provenance"""
    import shapely
    _require_crs(gdf, "reconcile")
    other = ctx.other(p.get("layer"), "reconcile")
    if _families(gdf) - {"Polygon"} or _families(other) - {"Polygon"}:
        raise OpsError("reconcile needs polygons on both sides")
    tm, tv, tu = to_metres(p.get("tolerance", 0), p.get("units"), "tolerance")
    thr = float(p.get("threshold", 0.5))
    fld = p.get("field") or f"{p.get('layer')}_share"
    crs, why = measure_crs(gdf, "area", [other], ctx=ctx)
    a = _in_measure(gdf, crs)
    ref = shapely.union_all(shapely.make_valid(_in_measure(other, crs).geometry.values))
    if tm > 0:
        ref = ref.buffer(tm)
    shapely.prepare(ref)
    geoms = a.geometry.values
    area = shapely.area(geoms)
    inter = shapely.area(shapely.intersection(geoms, ref))
    sh = [float(i / x) if x > 0 else 0.0 for i, x in zip(inter, area)]
    out = gdf.copy()
    out[fld] = [round(v, 4) for v in sh]
    out[f"{fld}_in"] = [v >= thr for v in sh]
    over = p.get("overrides") or {}
    idf = p.get("id_field")
    done = []
    if over:
        keys = out[idf].astype(str).tolist() if idf else [str(i) for i in range(len(out))]
        for k, v in over.items():
            if str(k) in keys:
                j = keys.index(str(k))
                out.loc[out.index[j], f"{fld}_in"] = bool(v)
                done.append(str(k))
        missing = [str(k) for k in over if str(k) not in done]
        if missing:
            raise OpsError(f"reconcile: overrides name feature(s) not in the layer: {', '.join(missing)}")
    note = (f"`{fld}`: the share of each feature inside '{p.get('layer')}' widened by {_fmt(tv)} {tu} (in {crs}); `{fld}_in` at {thr:g} or more: "
            f"{int(out[f'{fld}_in'].sum())} of {len(out)}" + (f"; set by hand: {', '.join(done)}" if done else "") + ".")
    return OpResult(out, [note], [], _prov("reconcile", p, len(gdf), len(out), crs, tolerance_m=tm, overrides=done))


def op_dot_density(gdf, p: dict, ctx: Ctx) -> OpResult:
    """dot_density {value, per, seed, max_dots, keep}: a polygon layer with a count becomes dots placed at random inside each polygon, one dot
    per `per` units (1.17.0, map type dot density). Each dot has `dot_value` and the polygon columns named in `keep` (none by default: a dot
    repeating its area's id reads as a duplicate); the dots are not locations (MAP-OPS-011)."""
    import geopandas as gpd
    import thematic as tm
    _require_crs(gdf, "dot_density")
    if _families(gdf) - {"Polygon"}:
        raise OpsError("dot_density needs a polygon layer (areas with a count)")
    val = p.get("value")
    if not val or val not in gdf.columns:
        raise OpsError("dot_density needs value: the column holding each area's count" + (f" ('{val}' is not a column)" if val else ""))
    try:
        per = float(p.get("per"))
    except (TypeError, ValueError):
        raise OpsError("dot_density needs per: the amount one dot stands for (for example per: 1000)") from None
    seed, cap = int(p.get("seed", 1)), int(p.get("max_dots", tm.MAX_DOTS))
    try:
        counts = tm.dot_counts(pd_numeric(gdf[val]), per, seed)
    except tm.ThematicError as ex:
        raise OpsError(str(ex)) from None
    total = int(counts.sum())
    if total > cap:
        need = float(pd_numeric(gdf[val]).clip(lower=0).sum()) / cap
        raise OpsError(f"dot_density would draw {total:,} dots (at most {cap:,}); give a larger per, about {_nice(need)} or more")
    crs, why = measure_crs(gdf, "area", ctx=ctx)
    m = _in_measure(gdf, crs)
    rng = np.random.default_rng(seed + 7919)
    keep = [p["keep"]] if isinstance(p.get("keep"), str) else list(p.get("keep") or [])
    miss = [c for c in keep if c not in m.columns]
    if miss:
        raise OpsError(f"dot_density keep: {', '.join(miss)} is not a column of the layer")
    attrs = keep
    recs, xs, ys = [], [], []
    records = m[attrs].to_dict("records") if attrs else [{} for _ in range(len(m))]      # no columns kept: one empty record a polygon
    for g, n, rec in zip(m.geometry.values, counts, records):
        pts = tm.dots_in(g, int(n), rng)
        for x, y in pts:
            xs.append(x)
            ys.append(y)
            recs.append(rec)
    out = gpd.GeoDataFrame(recs, geometry=gpd.points_from_xy(xs, ys), crs=crs) if recs else gpd.GeoDataFrame(columns=attrs + ["geometry"], geometry="geometry", crs=crs)
    out["dot_value"] = per
    out = _back(out, gdf.crs)
    find = [{"id": "MAP-OPS-011", "severity": "INFO", "message": f"dot_density: one dot = {_fmt(per)} of {val}; the {len(out):,} dots are placed at random inside "
             "each area, not at real locations; say so on the map (the legend's dot line, or the note)"}]
    return OpResult(out, [f"{len(out):,} dots, one per {_fmt(per)} of {val}, inside {len(gdf)} area(s) (seed {seed}), measured in {crs}."], find,
                    _prov("dot_density", p, len(gdf), len(out), crs, dots=len(out), per=per))


DENSITY_SCALES = ("linear", "sqrt", "log", "quantile")
SMOOTH_STEPS_PER_CLASS = 6     # 2.3.0: `smooth: true` cuts this many fine bands per class...
SMOOTH_STEPS_MAX = 48          # ...up to this many
HOT_SPOT_SHARE = 0.02          # 2.1.0: one location holding more than this share of the points is said (MAP-OPS-014): it can make a bullseye


def density_levels(z, peak: float, floor: float, k: int, scale: str):
    """The k+1 band edges of a density surface (2.1.0, Day 19 issue 4): from floor x peak to the peak, spaced linearly (1.17.0), on a square
    root or a log scale (a few dense spots no longer push everything else into the lowest band), or at quantiles of the drawn cells."""
    lo = floor * peak
    if scale == "sqrt":
        lv = np.linspace(np.sqrt(lo), np.sqrt(peak), k + 1) ** 2
    elif scale == "log":
        lv = np.geomspace(max(lo, peak * 1e-6), peak, k + 1)
    elif scale == "quantile":
        drawn = np.asarray(z)[np.asarray(z) >= lo]
        lv = np.quantile(drawn, np.linspace(0, 1, k + 1)) if drawn.size > k else np.linspace(lo, peak, k + 1)
        lv[0] = lo
        lv = np.maximum.accumulate(lv)
        for i in range(1, len(lv)):                     # quantiles of a flat surface can repeat: keep the edges strictly rising
            if lv[i] <= lv[i - 1]:
                lv[i] = lv[i - 1] * (1 + 1e-9) + 1e-12
    else:
        lv = np.linspace(lo, peak, k + 1)
    lv = np.asarray(lv, dtype=float)
    lv[-1] = max(lv[-1], peak) * 1.000001
    return lv


def _location_weights(x, y, cap: int) -> tuple[np.ndarray, dict]:
    """Each location (the same point to 1 m) counted at most `cap` times: weight min(1, cap / n) on its n points (2.1.0, Day 19: two addresses
    with 615 and 577 reports made bullseyes). -> (weights, {kept share, locations capped})."""
    key = np.round(x).astype(np.int64) * 1_000_003 + np.round(y).astype(np.int64)
    _u, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    n = cnt[inv].astype(float)
    w = np.minimum(1.0, cap / n)
    return w, {"kept_share": round(float(w.sum() / len(w)), 4), "locations_capped": int((cnt > cap).sum())}


def op_density_surface(gdf, p: dict, ctx: Ctx) -> OpResult:
    """density_surface {bandwidth, cell, classes, weight, floor, cap, scale, peak, within, outside, by}: many points become a kernel density
    surface cut into `classes` bands (1.17.0, map type density surface): polygons with `band` (1 lowest), `density_min` and `density_max`
    (per km2) and `band_label`. 2.1.0 (Day 19): `cap` counts each location at most N times; `scale` spaces the bands (linear, sqrt, log,
    quantile); `peak` fixes the top of the scale (per km2) so separate maps share it; `within` keeps the bands inside another layer (the land)
    and `outside` cuts another layer out (the water); `by` makes one surface per value of a column (a month, a year) on one shared set of
    band edges, each row carrying that value. 2.3.0: `smooth: true` draws it as a smooth heat map: classes x SMOOTH_STEPS_PER_CLASS fine bands
    (`band_fine`, `tone` 0..1), each the whole area at or above its level so the bands stack with no seam, every row keeping its coarse
    `band` and `band_label`; the map draws them in a continuous ramp with a ramp legend (map_plan)."""
    import geopandas as gpd
    import shapely

    import thematic as tm
    _require_crs(gdf, "density_surface")
    if _families(gdf) - {"Point"}:
        raise OpsError("density_surface needs a point layer")
    if len(gdf) < 3:
        raise OpsError("density_surface needs at least 3 points; draw a few points as symbols instead")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    m = _in_measure(gdf, crs)
    x, y = m.geometry.x.to_numpy(), m.geometry.y.to_numpy()
    w = None
    if p.get("weight"):
        if p["weight"] not in gdf.columns:
            raise OpsError(f"density_surface: the weight column '{p['weight']}' is not in the layer")
        w = pd_numeric(m[p["weight"]]).fillna(0).clip(lower=0).to_numpy()
    if p.get("bandwidth") is not None:
        num, u = parse_distance(p["bandwidth"], p.get("units"), "bandwidth")
        bw = num * UNITS_M[u]
        auto = False
    else:
        bw, auto = tm.silverman_m(x, y), True
    if p.get("cell"):
        cnum, cu = parse_distance(p["cell"], p.get("units"), "cell")
        cell = cnum * UNITS_M[cu]
    else:
        cell = bw / 4
    k = int(p.get("classes", 5))
    if not 2 <= k <= 9:
        raise OpsError("density_surface classes is between 2 and 9")
    floor = float(p.get("floor", 0.05))
    scale = str(p.get("scale", "linear")).lower()
    if scale not in DENSITY_SCALES:
        raise OpsError(f"density_surface scale is one of {', '.join(DENSITY_SCALES)}, not '{scale}'")
    find, steps, extra = [], [], {}
    # a location (one address) holding many of the points: said, and capped when asked
    key = np.round(x).astype(np.int64) * 1_000_003 + np.round(y).astype(np.int64)
    _u, cnt = np.unique(key, return_counts=True)
    if cnt.max() > max(10, HOT_SPOT_SHARE * len(x)) and not p.get("cap"):
        find.append({"id": "MAP-OPS-014", "severity": "WARN", "message": f"density_surface: one location holds {int(cnt.max()):,} of the {len(x):,} points "
                     f"({cnt.max() / len(x):.0%}); it can make a bullseye that pushes the rest into the lowest band. cap: 10 counts each location at "
                     "most 10 times; scale: sqrt or log spaces the bands for a few dense spots"})
    if p.get("cap"):
        cap = int(p["cap"])
        if cap < 1:
            raise OpsError("density_surface cap is a whole number of 1 or more (how many times one location counts at most)")
        cw, info = _location_weights(x, y, cap)
        w = cw if w is None else w * cw
        extra.update(cap=cap, **info)
        steps.append(f"each location counted at most {cap} times ({info['locations_capped']:,} location(s) capped; {info['kept_share']:.0%} of the points' weight kept)")
    # the periods: one surface each, on one grid and one set of band edges
    by = p.get("by")
    if by is not None and by not in gdf.columns:
        raise OpsError(f"density_surface: the by column '{by}' is not in the layer")
    groups = [(None, np.ones(len(x), dtype=bool))]
    if by is not None:
        vals = gdf[by].to_numpy()
        keys = [v for v in dict.fromkeys(vals.tolist()) if v is not None and not (isinstance(v, float) and np.isnan(v))]
        try:
            keys = sorted(keys)
        except TypeError:
            pass
        if not 1 <= len(keys) <= 120:
            raise OpsError(f"density_surface by '{by}': {len(keys)} values; one surface per value takes 1 to 120 of them")
        groups = [(v, vals == v) for v in keys]
    pad = 3 * bw
    try:
        whole = tm.density_grid(x, y, w, bw, cell, pad)          # the shared grid: every period's surface on the same cells
    except tm.ThematicError as ex:
        raise OpsError(f"density_surface: {ex}") from None
    xs, ys, zall = whole
    surfaces = []
    for v, m in groups:
        if v is None:
            z = zall
        else:
            if m.sum() < 1:
                continue
            gx = np.concatenate([x[m], [x.min(), x.max()]])        # two weightless points at the whole layer's corners: the same grid
            gy = np.concatenate([y[m], [y.min(), y.max()]])
            gw = np.concatenate([(w[m] if w is not None else np.ones(int(m.sum()))), [0.0, 0.0]])
            _xs, _ys, z = tm.density_grid(gx, gy, gw, bw, cell, pad)
            z = _fit_grid(z, zall.shape)
        surfaces.append((v, z))
    peak = float(p["peak"]) if p.get("peak") is not None else max(float(z.max()) for _v, z in surfaces)
    if peak <= 0:
        raise OpsError("density_surface: the points carry no weight")
    levels = density_levels(np.concatenate([z.ravel() for _v, z in surfaces]), peak, floor, k, scale)
    names = _band_names(k)
    rows, geoms = [], []
    smooth = bool(p.get("smooth"))
    if smooth:
        n = min(SMOOTH_STEPS_MAX, k * SMOOTH_STEPS_PER_CLASS)
        fine = density_levels(np.concatenate([z.ravel() for _v, z in surfaces]), peak, floor, n, scale)
        for v, z in surfaces:
            parts = list(tm.bands(xs, ys, np.minimum(z, fine[-1] * 0.9999999), fine))
            cum, stack = None, []
            for lo, hi, g in reversed(parts):                 # the area at or above each level: each band covers the ones above it
                cum = _clean_area(g if cum is None else shapely.union(cum, _clean_area(g, cell)), cell)
                stack.append((lo, cum))
            for j, (lo, g) in enumerate(reversed(stack)):
                ci = min(k - 1, int(np.searchsorted(levels, lo, side="right") - 1)) if lo >= levels[0] else 0
                rows.append({**({by: v} if by is not None else {}), "band": max(0, ci) + 1, "band_fine": j + 1, "tone": round((j + 0.5) / len(stack), 4),
                             "density_min": round(lo, 4), "density_max": round(float(levels[-1]), 4), "band_label": names[max(0, ci)]})
                geoms.append(g)
        steps.append(f"drawn smooth: {n} fine bands, each the area at or above its level, so they stack with no seam")
        extra.update(smooth_steps=n)
    for v, z in ([] if smooth else surfaces):
        for i, (lo, hi, g) in enumerate(tm.bands(xs, ys, np.minimum(z, levels[-1] * 0.9999999), levels)):
            rows.append({**({by: v} if by is not None else {}), "band": i + 1, "density_min": round(lo, 4), "density_max": round(hi, 4), "band_label": names[i]})
            geoms.append(g)
    out = gpd.GeoDataFrame(rows, geometry=geoms, crs=crs)
    # the land: kept inside one layer, another cut out (2.1.0, Day 19 issue 3: two helper layers and two more ops did this by hand)
    for key_, how in (("within", "inside"), ("outside", "outside")):
        if p.get(key_):
            other = ctx.other(p[key_], "density_surface")
            if _families(other) - {"Polygon"}:
                raise OpsError(f"density_surface {key_}: layer '{p[key_]}' must be polygons (a land, a county, a water layer)")
            mask = shapely.union_all(shapely.make_valid(np.asarray(_in_measure(other, crs).geometry.values, dtype=object)))
            gs = np.asarray(out.geometry.values, dtype=object)
            gs = shapely.intersection(gs, mask) if key_ == "within" else shapely.difference(gs, mask)
            out = out.set_geometry(gpd.GeoSeries(gs, crs=crs))
            out = out[~out.geometry.is_empty].reset_index(drop=True)
            steps.append(f"kept {how} layer '{p[key_]}'")
    out = _back(out, gdf.crs)
    unit = f"{p['weight']} per km2" if p.get("weight") else "points per km2"
    find.insert(0, {"id": "MAP-OPS-012", "severity": "INFO", "message": f"density_surface: smoothed with a {_fmt(bw)} m kernel" + (" (from the points' spread)" if auto else "")
                    + f"; the bands show where {len(gdf):,} points concentrate, not values at a place; below {floor:.0%} of the peak nothing is drawn"
                    + (f"; bands on a {scale} scale" if scale != "linear" else "")
                    + (f"; {len(surfaces)} surfaces (one per {by}) share one set of band edges" if by is not None else "")})
    return OpResult(out, [f"Density of {len(gdf):,} points ({unit}), a {_fmt(bw)} m kernel on a {_fmt(cell)} m grid, {k} band(s) from {_fmt(levels[0])} "
                          f"to {_fmt(peak)}{f' ({scale} scale)' if scale != 'linear' else ''}" + (f", one surface per {by} ({len(surfaces)})" if by is not None else "")
                          + f", measured in {crs}." + ("".join(f" {s[0].upper() + s[1:]}." for s in steps))], find,
                    _prov("density_surface", p, len(gdf), len(out), crs, bandwidth_m=round(bw, 1), cell_m=round(cell, 1), peak=round(peak, 4),
                          levels=[round(float(v), 6) for v in levels], scale=scale, **({"periods": [v.item() if hasattr(v, "item") else v for v, _z in surfaces]} if by is not None else {}), **extra))


def _clean_area(g, cell: float):
    """2.3.0: a band's area made valid and free of slivers: polygons and holes smaller than half a grid cell are dropped (the unions of the
    smooth surface leave rings of a few points that a reprojection makes invalid)."""
    import shapely
    from shapely.geometry import MultiPolygon, Polygon
    g = shapely.make_valid(g)
    polys = [x for x in getattr(g, "geoms", [g]) if x.geom_type in ("Polygon", "MultiPolygon")]
    parts = [q for x in polys for q in (x.geoms if x.geom_type == "MultiPolygon" else [x])]
    keep = []
    for q in parts:
        if q.area < 0.5 * cell * cell:
            continue
        holes = [r for r in q.interiors if Polygon(r).area >= 0.5 * cell * cell]
        keep.append(Polygon(q.exterior, holes))
    if not keep:
        return Polygon()
    out = keep[0] if len(keep) == 1 else MultiPolygon(keep)
    return out if out.is_valid else shapely.make_valid(out)


def _fit_grid(z, shape):
    """A period's grid on the shared grid's shape: the same origin and cell, so at most a row or column more or less; cut or padded with 0."""
    out = np.zeros(shape, dtype=float)
    r, c = min(shape[0], z.shape[0]), min(shape[1], z.shape[1])
    out[:r, :c] = z[:r, :c]
    return out


def op_flow_lines(gdf, p: dict, ctx: Ctx) -> OpResult:
    """flow_lines {to_x, to_y | to_id + layer + id_field, value, bend, top, min_value}: each row's point is the origin; a curved line to
    its destination (1.17.0, map type flow lines), bending to the right of travel. Columns kept, plus `flow` and `length_km`."""
    import geopandas as gpd
    import thematic as tm
    _require_crs(gdf, "flow_lines")
    if _families(gdf) - {"Point"}:
        raise OpsError("flow_lines needs a point layer whose points are the origins (a CSV read with x: and y: on the origin columns)")
    g4 = gdf.to_crs("EPSG:4326")
    if p.get("to_x") or p.get("to_y"):
        cols = [p.get("to_x"), p.get("to_y")]
        if any(not c or c not in gdf.columns for c in cols):
            raise OpsError("flow_lines needs to_x and to_y: the destination's longitude and latitude columns, both columns of the layer")
        dx, dy = pd_numeric(gdf[cols[0]]).to_numpy(), pd_numeric(gdf[cols[1]]).to_numpy()
    elif p.get("to_id"):
        if p["to_id"] not in gdf.columns:
            raise OpsError(f"flow_lines to_id: '{p['to_id']}' is not a column of the layer")
        places = ctx.other(p.get("layer"), "flow_lines").to_crs("EPSG:4326")
        key = p.get("id_field")
        if not key or key not in places.columns:
            raise OpsError("flow_lines with to_id needs id_field: the column of the places layer that the ids match")
        look = {str(k): (pt.x, pt.y) for k, pt in zip(places[key], places.geometry.representative_point())}
        miss = sorted({str(v) for v in gdf[p["to_id"]] if str(v) not in look})
        if miss:
            raise OpsError(f"flow_lines: {len(miss)} destination id(s) are not in the places layer: {', '.join(miss[:5])}")
        dx = np.array([look[str(v)][0] for v in gdf[p["to_id"]]])
        dy = np.array([look[str(v)][1] for v in gdf[p["to_id"]]])
    else:
        raise OpsError("flow_lines needs the destination: to_x and to_y (its longitude and latitude columns), or to_id with layer and id_field")
    val = p.get("value")
    if val and val not in gdf.columns:
        raise OpsError(f"flow_lines: the value column '{val}' is not in the layer")
    vals = pd_numeric(gdf[val]).fillna(0).to_numpy() if val else np.ones(len(gdf))
    keep = np.isfinite(dx) & np.isfinite(dy)
    if p.get("min_value") is not None:
        keep &= vals >= float(p["min_value"])
    order = np.argsort(-vals)
    if p.get("top"):
        top = set(order[: int(p["top"])].tolist())
        keep &= np.array([i in top for i in range(len(vals))])
    dest = gpd.GeoSeries(gpd.points_from_xy(dx, dy), crs="EPSG:4326")
    crs, why = measure_crs(gdf, "distance", others=[gpd.GeoDataFrame(geometry=dest[np.isfinite(dx)])], ctx=ctx)
    o = g4.to_crs(crs).geometry
    d = dest.to_crs(crs)
    bend = float(p.get("bend", 0.2))
    if not 0 <= bend <= 0.6:
        raise OpsError("flow_lines bend is between 0 (straight) and 0.6")
    rows, geoms = [], []
    attrs = [c for c in gdf.columns if c != _geom_col(gdf)]
    for i in np.where(keep)[0]:
        ln = tm.arc(o.iloc[i].x, o.iloc[i].y, d.iloc[i].x, d.iloc[i].y, bend)
        if ln is None:
            continue
        rows.append({**{a: gdf.iloc[i][a] for a in attrs}, "flow": float(vals[i]), "length_km": round(ln.length / 1000, 2)})
        geoms.append(ln)
    out = gpd.GeoDataFrame(rows, geometry=geoms, crs=crs).sort_values("flow").reset_index(drop=True) if rows else gpd.GeoDataFrame(columns=attrs + ["flow", "length_km", "geometry"], geometry="geometry", crs=crs)
    out = _back(out, gdf.crs)
    dropped = int(len(gdf) - len(out))
    find = [{"id": "MAP-OPS-013", "severity": "INFO", "message": f"flow_lines: {len(out)} curved line(s) bending to the right of travel; draw them with "
             "symbol: {graduated: true} on value_field: flow so the width shows the volume" + (f"; {dropped} row(s) left out (no destination, under min_value "
             "or past top)" if dropped else "")}]
    return OpResult(out, [f"{len(out)} flow line(s) from {len(gdf)} row(s), measured in {crs} (bend {bend:g} of the chord)."], find,
                    _prov("flow_lines", p, len(gdf), len(out), crs, dropped=dropped))


# ---------------------------------------------------------------------------------------------------------------- 3.4.0: walk along a network
def _graph(lines, snap_tol: float):
    """Nodes (vertices rounded to snap_tol so touching ends join) and edges (consecutive vertices) of a line layer in a metric CRS."""
    import numpy as np
    import shapely
    q = max(float(snap_tol), 1e-6)
    key: dict = {}
    xs, ys, ea, eb, ew, eseg = [], [], [], [], [], []

    def node(x, y):
        k = (round(x / q), round(y / q))
        i = key.get(k)
        if i is None:
            i = key[k] = len(xs)
            xs.append(x)
            ys.append(y)
        return i
    for si, g in enumerate(lines):
        for part in shapely.get_parts(g):
            c = shapely.get_coordinates(part)
            if len(c) < 2:
                continue
            ids = [node(x, y) for x, y in c]
            d = np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1]))
            for a, b, w in zip(ids[:-1], ids[1:], d, strict=True):
                if a != b:
                    ea.append(a)
                    eb.append(b)
                    ew.append(float(w))
                    eseg.append(si)
    return np.array(xs), np.array(ys), np.array(ea, dtype=int), np.array(eb, dtype=int), np.array(ew), np.array(eseg, dtype=int)


def _multi_source(n: int, ea, eb, ew, sources, offsets=None):
    """Shortest distance from the nearest source to every node (a virtual node joined to each source by its snap distance, then one Dijkstra;
    scipy when present, else a heap)."""
    import numpy as np
    srcs = [int(x) for x in sources]
    if not srcs:
        return np.full(n, np.inf)
    off = [float(o) for o in (offsets if offsets is not None else [0.0] * len(srcs))]
    v = n                                                  # the virtual source
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import dijkstra
        rows = np.r_[ea, eb, np.full(len(srcs), v)]
        cols = np.r_[eb, ea, np.array(srcs)]
        w = np.r_[ew, ew, np.maximum(np.array(off), 1e-9)]
        m = coo_matrix((w, (rows, cols)), shape=(n + 1, n + 1)).tocsr()
        return dijkstra(m, directed=True, indices=v)[:n]
    except ImportError:
        import heapq
        adj: list[list] = [[] for _ in range(n)]
        for a, b, w in zip(ea, eb, ew, strict=True):
            adj[a].append((b, w))
            adj[b].append((a, w))
        dist = np.full(n, np.inf)
        h = []
        for s_, o in zip(srcs, off, strict=True):
            if o < dist[s_]:
                dist[s_] = o
                h.append((o, s_))
        heapq.heapify(h)
        while h:
            d, u = heapq.heappop(h)
            if d > dist[u]:
                continue
            for v2, w in adj[u]:
                nd = d + w
                if nd < dist[v2]:
                    dist[v2] = nd
                    heapq.heappush(h, (nd, v2))
        return dist


def _walk_edges(left, sites, speed_ms: float, snap_m: float, tol: float):
    """Every edge of the line layer (consecutive vertices) with its walk in minutes (the mean of its two ends' distance to the nearest site
    along the network, each site entering at its snap distance) -> (minutes per edge, edge feature index, edge geometries, snapped, sites)."""
    import numpy as np
    import shapely
    xs, ys, ea, eb, ew, eseg = _graph(left.geometry.values, tol)
    nodes = shapely.points(np.c_[xs, ys])
    tree = shapely.STRtree(nodes)
    pts = shapely.get_coordinates(sites.geometry.centroid.values)
    src, off = [], []
    for x, y in pts:
        j, d = tree.query_nearest(shapely.Point(x, y), return_distance=True)
        if len(j) and d[0] <= snap_m:
            src.append(int(j[0]))
            off.append(float(d[0]))
    dist = _multi_source(len(xs), ea, eb, ew, src, off)
    mins = (dist[ea] + dist[eb]) / 2.0 / speed_ms / 60.0
    geoms = shapely.linestrings(np.stack([np.c_[xs[ea], ys[ea]], np.c_[xs[eb], ys[eb]]], axis=1)) if len(ea) else np.array([], dtype=object)
    return mins, eseg, geoms, ew, len(src), len(pts)


def _walk_minutes(left, sites, crs, speed_ms: float, snap_m: float, tol: float):
    """Per line feature: the length-weighted walk in minutes over its edges; and how many sites snapped."""
    import numpy as np
    mins, eseg, _g, ew, snapped, total = _walk_edges(left, sites, speed_ms, snap_m, tol)
    out = np.full(len(left), np.inf)
    ok = np.isfinite(mins)
    if ok.any():
        num = np.bincount(eseg[ok], weights=mins[ok] * ew[ok], minlength=len(left))
        den = np.bincount(eseg[ok], weights=ew[ok], minlength=len(left))
        has = den > 0
        out[has] = num[has] / den[has]
    return out, snapped, total


def op_network_nearest(gdf, p: dict, ctx: Ctx) -> OpResult:
    """network_nearest {sites, speed_kmh: 4.5, snap_m: 25, bands: [5, 10, 15], field: walk_min, compare}: the walk along this line layer to the
    nearest of `sites` (multi-source Dijkstra on the lines' shared vertices, offline, any number of sites; Day 21's walk to 26 fountains),
    in minutes per line, with a band label; `compare` (a second sites layer) adds the minutes with both and `changed` where the band moves."""
    import numpy as np
    _require_crs(gdf, "network_nearest")
    sname = p.get("sites") or p.get("layer")
    sites = ctx.other(sname, "network_nearest")
    speed = float(p.get("speed_kmh", 4.5))
    if speed <= 0:
        raise OpsError("network_nearest: speed_kmh must be above 0")
    snap_m = to_metres(p.get("snap_m", 25), "m", "snap_m")[0]
    tol = float(p.get("join_m", 0.05))
    bands = [float(b) for b in (p.get("bands") or [5, 10, 15])]
    field = str(p.get("field") or "walk_min")
    if set(_families(gdf)) - {"LineString", "LinearRing"}:
        raise OpsError("network_nearest runs on a line layer (streets, footways)")
    crs, why = measure_crs(gdf, "distance", [sites], ctx=ctx)
    left = _in_measure(gdf, crs).reset_index(drop=True)
    sp = speed / 3.6
    split = str(p.get("split") or "").lower() in ("segments", "true", "yes", "1")
    if split:
        # 3.4.0: one row per segment between two vertices (Day 21's 121,786 pieces), so a long street changes colour where its walk does
        import geopandas as gpd
        em, eseg, eg, _ew, snapped, total = _walk_edges(left, _in_measure(sites, crs), sp, snap_m, tol)
        attrs = left.drop(columns=[left.geometry.name]).iloc[eseg].reset_index(drop=True)
        left = gpd.GeoDataFrame(attrs, geometry=list(eg), crs=crs)
        mins = em
        out = _back(left, gdf.crs).reset_index(drop=True)
    else:
        mins, snapped, total = _walk_minutes(left, _in_measure(sites, crs), crs, sp, snap_m, tol)
        out = gdf.reset_index(drop=True).copy()

    def band(m):
        if not np.isfinite(m):
            return None
        lo = 0.0
        for b in bands:
            if m < b:
                return f"{_fmt(lo)}-{_fmt(b)} min"
            lo = b
        return f"{_fmt(bands[-1])}+ min"
    out[field] = np.where(np.isfinite(mins), np.round(mins, 2), np.nan)
    out[f"{field}_band"] = [band(m) for m in mins]
    lengths = left.geometry.length.to_numpy()
    total_len = float(lengths[np.isfinite(mins)].sum()) or 1.0
    share = {}
    for lab in [band(b - 1e-9) for b in bands] + [band(bands[-1] + 1)]:
        share[lab] = round(float(lengths[np.array([band(m) == lab for m in mins])].sum()) / total_len, 4)
    notes = [f"Walk along '{ctx.layer_id or 'this layer'}' to the nearest of {snapped} of {total} '{sname}' site(s) (snapped within "
             f"{_fmt(snap_m)} m) at {speed:g} km/h, measured in {crs} ({why}): `{field}` and `{field}_band`; share of length by band: "
             + ", ".join(f"{k} {v:.1%}" for k, v in share.items())]
    findings = []
    if snapped < total:
        findings.append({"id": "MAP-OPS-016", "severity": "WARN", "message": f"network_nearest: {total - snapped} of {total} site(s) are farther than "
                         f"{_fmt(snap_m)} m from the network and were left out (snap_m sets the reach)", "detail": {"left_out": total - snapped}})
    unreached = int((~np.isfinite(mins)).sum())
    if unreached:
        notes.append(f"{unreached:,} line(s) are on parts of the network no site reaches; they have no {field}")
    extra = {"share_by_band": share, "speed_kmh": speed, "sites": snapped}
    if p.get("compare"):
        cname = p["compare"]
        both = _in_measure(ctx.other(cname, "network_nearest"), crs)
        import pandas as pd
        allsites = pd.concat([_in_measure(sites, crs)[[sites.geometry.name]].rename_geometry("geometry") if sites.geometry.name != "geometry" else _in_measure(sites, crs)[["geometry"]],
                              both[[both.geometry.name]].rename_geometry("geometry") if both.geometry.name != "geometry" else both[["geometry"]]], ignore_index=True)
        import geopandas as gpd
        allsites = gpd.GeoDataFrame(allsites, geometry="geometry", crs=crs)
        if split:
            m2, _es, _eg, _w, sn2, t2 = _walk_edges(left, allsites, sp, snap_m, tol)
        else:
            m2, sn2, t2 = _walk_minutes(left, allsites, crs, sp, snap_m, tol)
        out[f"{field}_before"] = out[field]
        out[field] = np.where(np.isfinite(m2), np.round(m2, 2), np.nan)
        out[f"{field}_band_before"] = out[f"{field}_band"]
        out[f"{field}_band"] = [band(m) for m in m2]
        out["changed"] = out[f"{field}_band"] != out[f"{field}_band_before"]
        share2 = {lab: round(float(lengths[np.array([band(m) == lab for m in m2])].sum()) / (float(lengths[np.isfinite(m2)].sum()) or 1.0), 4) for lab in share}
        extra.update(share_by_band_before=share, share_by_band=share2, compare=cname)
        notes.append(f"What-if with '{cname}' as well ({sn2} of {t2} sites): `{field}` is the walk with both, `{field}_before` without, "
                     f"`changed` marks {int(out['changed'].sum()):,} line(s) whose band moves; share of length by band: "
                     + ", ".join(f"{k} {v:.1%}" for k, v in share2.items()))
    method = (f"Walk times along the mapped network at {speed:g} km/h to the nearest site (snapped within {_fmt(snap_m)} m); modelled, not timed"
              + ("; a hypothetical with the second set of sites" if p.get("compare") else ""))
    return OpResult(out, notes, findings, _prov("network_nearest", p, len(gdf), len(out), crs, other_layer=sname, other_rows=len(sites),
                                                method_note=method, **extra))


def op_network_distance(gdf, p: dict, ctx: Ctx) -> OpResult:
    """network_distance {from: layer | [lon, lat], snap_m: 400, densify_m: 25, join_m: 3, gap_m: 0, speed_kmh, field: dist}: how far along this
    line layer each line is from the nearest source (4.1.0, Day 24 D24-25: water from Granite Reef Dam through the canals). Each line gets
    `{field}_start_m` and `{field}_end_m` (its nearest and farthest point along the network) and, with speed_kmh, `arrive_h` (hours to its
    start). Lines no source reaches have none. gap_m ties a line end to a line it stops short of (a branch drawn a few metres off)."""
    import geopandas as gpd
    import network_measure as nm
    _require_crs(gdf, "network_distance")
    if set(_families(gdf)) - {"LineString", "LinearRing"}:
        raise OpsError("network_distance runs on a line layer (canals, pipes, rivers, cable routes, streets)")
    src = p.get("from") or p.get("sources") or p.get("layer")
    crs, why = measure_crs(gdf, "distance", ctx=ctx)
    lines = _in_measure(gdf, crs).reset_index(drop=True)
    if isinstance(src, (list, tuple)) and len(src) == 2 and all(isinstance(v, (int, float)) for v in src):
        pts = gpd.GeoSeries.from_xy([float(src[0])], [float(src[1])], crs="EPSG:4326").to_crs(crs)
        sname = f"{float(src[0]):.5f}, {float(src[1]):.5f}"
    elif isinstance(src, str):
        other = ctx.other(src, "network_distance")
        pts = _in_measure(other, crs).geometry.representative_point()
        sname = f"'{src}'"
    else:
        raise OpsError("network_distance needs from: a point layer's id or [lon, lat]")
    f = lambda k, d: to_metres(p.get(k, d), "m", k)[0]          # noqa: E731
    try:
        res = nm.measure(lines.geometry.values, [(g.x, g.y) for g in pts], snap_m=f("snap_m", 400), densify_m=f("densify_m", 25),
                         join_m=f("join_m", 3), gap_m=f("gap_m", 0))
    except nm.NetworkError as ex:
        raise OpsError(f"network_distance: {ex}") from None
    lo, hi = nm.feature_ranges(res, len(lines))
    field = str(p.get("field") or "dist")
    out = gdf.reset_index(drop=True).copy()
    out[f"{field}_start_m"] = np.round(lo, 1)
    out[f"{field}_end_m"] = np.round(hi, 1)
    notes = [f"Distance along '{ctx.layer_id or 'this layer'}' from {sname} ({res['snapped']} of {res['sources']} source(s) on the network, {res['starts']} line(s) starting there), "
             f"measured in {crs} ({why}): {res['reached_m'] / 1000:,.1f} of {res['total_m'] / 1000:,.1f} km reached, the farthest point "
             f"{res['max_m'] / 1000:,.1f} km away" + (f"; {res['gaps']} gap(s) of up to {f('gap_m', 0):g} m bridged" if res["gaps"] else "")]
    extra = {"reached_km": round(res["reached_m"] / 1000, 2), "max_km": round(res["max_m"] / 1000, 2), "gaps": res["gaps"]}
    if p.get("speed_kmh"):
        sp = float(p["speed_kmh"])
        if sp <= 0:
            raise OpsError("network_distance: speed_kmh must be above 0")
        out["arrive_h"] = np.round(lo / 1000.0 / sp, 3)
        notes.append(f"`arrive_h`: hours to reach each line at {sp:g} km/h (an assumed speed); the farthest point at {res['max_m'] / 1000 / sp:,.1f} h")
        extra["speed_kmh"] = sp
    unreached = int(np.isnan(lo).sum())
    findings = []
    if unreached:
        findings.append({"id": "MAP-OPS-016", "severity": "INFO", "message": f"network_distance: {unreached} of {len(lines)} line(s) are on parts no source "
                         "reaches (gap_m joins lines that stop short of each other)", "detail": {"unreached": unreached}})
    return OpResult(out, notes, findings, _prov("network_distance", p, len(gdf), len(out), crs, method_note=(
        "Distance along the mapped lines from the source; modelled, not measured" + (f", at an assumed {p['speed_kmh']} km/h" if p.get("speed_kmh") else "")), **extra))


FIBRE_INDEX = 1.468          # light in single-mode fibre travels at c / 1.468: about 204 km per millisecond


def light_budget(a, b, rtt_a_ms: float, rtt_b_ms: float, n: float = FIBRE_INDEX, overhead_ms: float = 0.0, segments: int = 720) -> dict:
    """The places a signal could have passed between two traceroute hops: every point whose path a -> point -> b fits in the one-way time
    (rtt_b - rtt_a) / 2 at the speed of light in fibre. In the two-point equidistant projection centred on a and b (distances to both are true)
    that set is an exact ellipse with a and b as foci. -> {crs, polygon (in crs), budget_km, gc_km, ratio, km_per_ms}."""
    from pyproj import Geod, Transformer
    from shapely.geometry import Polygon
    a, b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    one_way = (float(rtt_b_ms) - float(rtt_a_ms)) / 2.0 - float(overhead_ms)
    if one_way <= 0:
        raise OpsError("light_budget: the far hop's round trip must be longer than the near hop's")
    kmms = 299792.458 / float(n) / 1000.0
    budget_m = one_way * kmms * 1000.0
    crs = (f"+proj=tpeqd +lat_1={a[1]:.6f} +lon_1={a[0]:.6f} +lat_2={b[1]:.6f} +lon_2={b[0]:.6f} +R=6371000 +units=m +no_defs")
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    pa, pb = t.transform(*a), t.transform(*b)
    _, _, gc = Geod(a=6371000, b=6371000).inv(a[0], a[1], b[0], b[1])
    if budget_m <= gc:
        raise OpsError(f"light_budget: {one_way:.1f} ms one way covers {budget_m / 1000:,.0f} km, less than the {gc / 1000:,.0f} km between the hops; "
                       "the round trips or the places are wrong")
    A = budget_m / 2.0
    c = abs(pb[0] - pa[0]) / 2.0
    B = math.sqrt(max(A * A - c * c, 0.0))
    cx, cy = (pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0
    th = np.linspace(0, 2 * np.pi, int(segments) + 1)
    poly = Polygon(zip(cx + A * np.cos(th), cy + B * np.sin(th)))
    return {"crs": crs, "polygon": poly, "budget_km": budget_m / 1000.0, "gc_km": gc / 1000.0, "ratio": budget_m / gc, "km_per_ms": kmms,
            "one_way_ms": one_way}


def op_light_budget(gdf, p: dict, ctx: Ctx) -> OpResult:
    """light_budget {a: [lon, lat], b: [lon, lat], rtt_a_ms, rtt_b_ms, n: 1.468, overhead_ms: 0}: the ellipse of every place a packet could have
    passed between two hops of a traceroute in the time between them (4.1.0, Day 24 D24-27). One polygon in the two-point equidistant CRS
    centred on the two hops; draw the map in that CRS (crs.display) and the ellipse, even round a pole, is drawn whole."""
    import geopandas as gpd
    for k in ("a", "b", "rtt_a_ms", "rtt_b_ms"):
        if p.get(k) is None:
            raise OpsError("light_budget needs a and b ([lon, lat] of the two hops) and rtt_a_ms and rtt_b_ms (their round trips)")
    r = light_budget(p["a"], p["b"], p["rtt_a_ms"], p["rtt_b_ms"], float(p.get("n", FIBRE_INDEX)), float(p.get("overhead_ms", 0)))
    out = gpd.GeoDataFrame({"budget_km": [round(r["budget_km"], 1)], "gc_km": [round(r["gc_km"], 1)], "ratio": [round(r["ratio"], 3)],
                            "one_way_ms": [round(r["one_way_ms"], 2)]}, geometry=[r["polygon"]], crs=r["crs"])
    note = (f"Light budget: {r['one_way_ms']:.1f} ms one way at {r['km_per_ms']:.1f} km per ms (fibre, n = {float(p.get('n', FIBRE_INDEX)):g}) is a path of up "
            f"to {r['budget_km']:,.0f} km against {r['gc_km']:,.0f} km straight ({r['ratio']:.2f}x); an outer bound, since router and queueing "
            f"delay only shrink it. Drawn in {r['crs']} (set crs.display to it)")
    return OpResult(out, [note], [], _prov("light_budget", p, len(gdf), 1, r["crs"], method_note="Reach of light in fibre between two traceroute hops; an outer bound",
                                           display_crs=r["crs"], budget_km=round(r["budget_km"], 1), gc_km=round(r["gc_km"], 1)))


def pd_numeric(s):
    import pandas as pd
    return pd.to_numeric(s, errors="coerce")


def _nice(v: float) -> str:
    if v <= 0:
        return "1"
    e = 10 ** math.floor(math.log10(v))
    for k in (1, 2, 2.5, 5, 10):
        if k * e >= v:
            return _fmt(k * e)
    return _fmt(10 * e)


def _band_names(k: int) -> list[str]:
    named = {2: ["Lower", "Higher"], 3: ["Lower", "Middle", "Higher"], 4: ["Lowest", "Lower", "Higher", "Highest"],
             5: ["Lowest", "Low", "Middle", "High", "Highest"]}
    return named.get(k) or (["Lowest"] + [f"Band {i}" for i in range(2, k)] + ["Highest"])



OPS = {
    "geocode": op_geocode,
    "dot_density": op_dot_density,
    "density_surface": op_density_surface,
    "flow_lines": op_flow_lines,
    "buffer": op_buffer,
    "centroid": op_centroid,
    "representative_point": op_centroid,
    "clip": op_clip,
    "intersect": _overlay("intersection"),
    "intersection": _overlay("intersection"),
    "union": _overlay("union"),
    "difference": _overlay("difference"),
    "symmetric_difference": _overlay("symmetric_difference"),
    "spatial_join": op_spatial_join,
    "nearest": op_nearest,
    "dissolve": op_dissolve,
    "explode": op_explode,
    "dedupe": op_dedupe,
    "repair": op_repair,
    "make_valid": op_repair,
    "simplify": op_simplify,
    "antimeridian_split": op_antimeridian_split,
    "convex_hull": op_convex_hull,
    "concave_hull": op_concave_hull,
    "voronoi": op_voronoi,
    "area": op_area,
    "as_of": op_as_of,
    "length": op_length,
    "count_in": op_count_in,
    "sum_in": op_sum_in,
    "population_covered": op_population_covered,
    "coverage_share": op_coverage_share,
    "overlap": op_overlap,
    "grid": op_grid,
    "hexbin": op_hexbin,
    "raster_zonal": op_raster_zonal,
    "line_of_sight": op_line_of_sight,
    "viewshed": op_viewshed,
    "fill_holes": op_fill_holes,
    "massing": op_massing,
    "split_by": op_split_by,
    "thin_lines": op_thin_lines,
    "reconcile": op_reconcile,
    "network_nearest": op_network_nearest,
    "network_distance": op_network_distance,
    "light_budget": op_light_budget,
}

NEEDS_LAYER = {"clip", "intersect", "intersection", "union", "difference", "symmetric_difference", "spatial_join", "nearest",
               "count_in", "sum_in", "population_covered", "coverage_share", "split_by", "reconcile"}
# 1.16.0 (Day 14 issue 9): the ops that union or overlay geometry; their input is repaired first when it is invalid, so a GEOS
# TopologyException (Natural Earth's 50 m land raised one) is never what a person sees
TOPO_OPS = {"clip", "intersect", "intersection", "union", "difference", "symmetric_difference", "dissolve", "split_by", "reconcile", "massing",
            "fill_holes", "count_in", "sum_in", "population_covered", "coverage_share", "overlap"}
# the input findings an op is the fix for: a caller drops them from the input lint, since the op's own finding replaces them
FIXES = {"repair": "MAP-DATA-002", "make_valid": "MAP-DATA-002", "antimeridian_split": "MAP-DATA-009"}


def _valid_first(gdf, op: str, what: str):
    """The layer with invalid geometry repaired before a topology op, and the finding that says so (data_lint.repair_geometry: INFO, or
    BLOCK when the area moves more than 1 percent). -> (gdf, findings)."""
    import shapely
    if gdf is None or len(gdf) == 0:
        return gdf, []
    bad = ~shapely.is_valid(gdf.geometry.values) & ~gdf.geometry.isna().values
    if not bad.any():
        return gdf, []
    import data_lint as dl
    out, fs = dl.repair_geometry(gdf)
    return out, [{**f, "message": f"{op}: {what}: {f['message']} first"} for f in fs]


def fixed_ids(ops: list) -> set:
    return {FIXES[str(o.get("op")).lower()] for o in (ops or []) if isinstance(o, dict) and str(o.get("op")).lower() in FIXES}


def run_op(gdf, spec: dict, ctx: Ctx) -> OpResult:
    """One op from its dict ({"op": name, ...params}). 1.3.0: `{as_of: DATE}` is the short form of `{op: as_of, date: DATE}`."""
    if isinstance(spec, dict) and not spec.get("op") and spec.get("as_of"):
        spec = {"op": "as_of", "date": spec["as_of"]}
    if not isinstance(spec, dict) or not spec.get("op"):
        raise OpsError("each op is a mapping with an `op` name, e.g. {op: buffer, distance: 2 km}")
    name = str(spec["op"]).lower()
    fn = OPS.get(name)
    if fn is None:
        raise OpsError(f"unknown op '{name}'. Ops: {', '.join(sorted(OPS))}")
    p = dict(spec)
    p["op"] = name
    pre: list = []
    if name in TOPO_OPS:
        gdf, pre = _valid_first(gdf, name, "the layer")
        oname = p.get("layer") or p.get("opening")
        if oname and oname in ctx.layers and ctx.layers[oname] is not None:
            ctx.layers[oname], more = _valid_first(ctx.layers[oname], name, f"layer '{oname}'")
            pre += more
    try:
        r = fn(gdf, p, ctx)
    except Exception as ex:  # noqa: BLE001 - a GEOS error the check above did not foresee: one more try on repaired input
        if type(ex).__name__ not in ("GEOSException", "TopologicalError", "TopologyException"):
            raise
        import shapely
        g2 = gdf.copy()
        g2[_geom_col(g2)] = shapely.make_valid(gdf.geometry.values)
        r = fn(g2, p, ctx)
        pre.append({"id": "MAP-DATA-002", "severity": "INFO", "message": f"{name}: GEOS refused the geometry ({str(ex)[:80]}); the op ran again on repaired geometry"})
    if pre:
        r.findings = pre + list(r.findings)
    r.provenance.setdefault("op", name)
    r.provenance["layer"] = ctx.layer_id
    return r


def apply_ops(gdf, ops: list, ctx: Ctx | None = None):
    """Run ops in order. -> (gdf, notes, findings); ctx.provenance gains one record per op."""
    ctx = ctx or Ctx()
    notes: list[str] = []
    findings: list[dict] = []
    for spec in ops or []:
        r = run_op(gdf, spec, ctx)
        gdf = r.gdf
        notes += r.notes
        findings += r.findings
        ctx.provenance.append(r.provenance)
    return gdf, notes, findings


# --------------------------------------------------------------------------- equivalents for the methods page

def equivalent(op: str, p: dict, prov: dict | None = None) -> dict:
    """The GeoPandas and PostGIS expression that does what the op did (for the methods page; not run)."""
    prov = prov or {}
    crs = prov.get("measure_crs") or "<projected CRS>"
    other = p.get("layer") or "other"
    val = p.get("value") or "value"
    fld = p.get("field")
    d = prov.get("distance_m")
    dist = f"{d:g}" if isinstance(d, (int, float)) else str(p.get("distance", "D"))
    table = {
        "buffer": (f"gdf.to_crs('{crs}').buffer({dist}).to_crs(gdf.crs)" + (".unary_union" if _bool(p.get("dissolve")) else ""),
                   f"ST_Buffer(geom::geography, {dist})" + (" grouped with ST_Union" if _bool(p.get("dissolve")) else "")),
        "centroid": (f"gdf.to_crs('{crs}').centroid.to_crs(gdf.crs)", "ST_Centroid(geom)"),
        "representative_point": (f"gdf.to_crs('{crs}').representative_point().to_crs(gdf.crs)", "ST_PointOnSurface(geom)"),
        "clip": (f"gpd.clip(gdf, {other}.unary_union)", f"ST_Intersection(a.geom, b.geom) FROM a, {other} b WHERE ST_Intersects(a.geom, b.geom)"),
        "intersect": (f"gpd.overlay(gdf, {other}, how='intersection')", "ST_Intersection(a.geom, b.geom) ... WHERE ST_Intersects(a.geom, b.geom)"),
        "intersection": (f"gpd.overlay(gdf, {other}, how='intersection')", "ST_Intersection(a.geom, b.geom) ... WHERE ST_Intersects(a.geom, b.geom)"),
        "union": (f"gpd.overlay(gdf, {other}, how='union')", "ST_Union / ST_Difference pieces (no single PostGIS overlay)"),
        "difference": (f"gpd.overlay(gdf, {other}, how='difference')", f"ST_Difference(a.geom, (SELECT ST_Union(geom) FROM {other}))"),
        "symmetric_difference": (f"gpd.overlay(gdf, {other}, how='symmetric_difference')", "ST_SymDifference(a.geom, b.geom)"),
        "spatial_join": (f"gpd.sjoin(gdf, {other}, how='{p.get('how', 'left')}', predicate='{p.get('predicate', 'intersects')}')" if p.get("predicate", "intersects") != "nearest"
                         else f"gpd.sjoin_nearest(gdf.to_crs('{crs}'), {other}.to_crs('{crs}'), max_distance={p.get('max_distance')})",
                         f"a {('LEFT ' if p.get('how', 'left') == 'left' else '')}JOIN {other} b ON ST_{str(p.get('predicate', 'intersects')).capitalize()}(a.geom, b.geom)"
                         if p.get("predicate", "intersects") != "nearest" else f"CROSS JOIN LATERAL (SELECT * FROM {other} b ORDER BY a.geom <-> b.geom LIMIT 1)"),
        "nearest": (f"gpd.sjoin_nearest(gdf.to_crs('{crs}'), {other}.to_crs('{crs}'), distance_col='nearest_distance')",
                    f"CROSS JOIN LATERAL (SELECT b.id, ST_Distance(a.geom::geography, b.geom::geography) FROM {other} b ORDER BY a.geom <-> b.geom LIMIT {p.get('k', 1)})"),
        "dissolve": (f"gdf.dissolve(by={p.get('by')!r}, aggfunc={p.get('agg') or 'first'!r})", f"SELECT {p.get('by') or '1'}, ST_Union(geom) ... GROUP BY {p.get('by') or '1'}"),
        "explode": ("gdf.explode(index_parts=False)", "ST_Dump(geom)"),
        "dedupe": (f"gdf.drop_duplicates(subset={_list(p.get('fields')) or 'geometry'})", "SELECT DISTINCT ON (...)"),
        "repair": ("gdf.geometry.make_valid()", "ST_MakeValid(geom)"),
        "make_valid": ("gdf.geometry.make_valid()", "ST_MakeValid(geom)"),
        "simplify": (f"gdf.to_crs('{crs}').simplify({prov.get('tolerance_m', p.get('tolerance'))}, preserve_topology=True)", f"ST_SimplifyPreserveTopology(geom, {prov.get('tolerance_m', p.get('tolerance'))})"),
        "antimeridian_split": ("shapely.ops.split(geom, LineString([(180,-90),(180,90)])) after shifting west of 180", "ST_Split(ST_ShiftLongitude(geom), ST_MakeLine(ST_Point(180,-90), ST_Point(180,90)))"),
        "convex_hull": (f"gdf{'.dissolve(by=' + repr(p.get('by')) + ')' if p.get('by') else ''}.convex_hull", f"ST_ConvexHull(ST_Collect(geom)){' GROUP BY ' + str(p.get('by')) if p.get('by') else ''}"),
        "concave_hull": (f"shapely.concave_hull(gdf.to_crs('{crs}').unary_union, ratio={p.get('ratio', 0.3)})", f"ST_ConcaveHull(ST_Collect(geom), {p.get('ratio', 0.3)})"),
        "voronoi": (f"shapely.voronoi_polygons(MultiPoint(gdf.to_crs('{crs}').geometry), extend_to=mask)", "ST_VoronoiPolygons(ST_Collect(geom))"),
        "network_distance": (f"scipy.sparse.csgraph.dijkstra(graph of gdf.to_crs('{crs}') densified every {p.get('densify_m', 25)} m, indices=sources, min_only=True)",
                             "pgr_drivingDistance(edges, source, distance) (pgRouting)"),
        "light_budget": ("Polygon(ellipse with foci a and b and major axis (rtt_b - rtt_a) / 2 x c / n) in +proj=tpeqd of a and b",
                         "ST_Transform(ellipse built in the two-point equidistant CRS, 4326)"),
        "as_of": (f"gdf[(gdf.start_date <= '{p.get('date')}') & ((gdf.end_date.isna()) | ('{p.get('date')}' < gdf.end_date))]  # partial dates widened first",
                  f"WHERE start_date <= '{p.get('date')}' AND (end_date IS NULL OR '{p.get('date')}' < end_date)"),
        "area": (f"gdf['{fld or 'area'}'] = gdf.to_crs('EPSG:4326').geometry.apply(lambda g: abs(Geod(ellps='WGS84').geometry_area_perimeter(g)[0]))", "ST_Area(geom::geography)"),
        "length": (f"gdf['{fld or 'length'}'] = gdf.to_crs('EPSG:4326').geometry.apply(Geod(ellps='WGS84').geometry_length)", "ST_Length(geom::geography)"),
        "count_in": (f"gpd.sjoin(gdf, {other}, predicate='intersects').groupby(level=0).size()", f"SELECT a.id, count(b.*) FROM a LEFT JOIN {other} b ON ST_Intersects(a.geom, b.geom) GROUP BY a.id"),
        "sum_in": (f"gpd.sjoin(gdf, {other}, predicate='intersects').groupby(level=0)['{val}'].sum()", f"SELECT a.id, sum(b.{val}) FROM a LEFT JOIN {other} b ON ST_Intersects(a.geom, b.geom) GROUP BY a.id"),
        "population_covered": (f"pieces = gpd.overlay(gdf.to_crs('{crs}'), {other}.to_crs('{crs}')); pieces['{val}'] * pieces.area / zone_area, summed per feature",
                               f"SELECT a.id, sum(b.{val} * ST_Area(ST_Intersection(a.geom, b.geom)) / ST_Area(b.geom)) FROM a JOIN {other} b ON ST_Intersects(a.geom, b.geom) GROUP BY a.id"),
        "coverage_share": (f"population_covered / {other}['{val}'].sum()", f"... / (SELECT sum({val}) FROM {other})"),
        "overlap": (f"pairs = gpd.sjoin(gdf, {other if other != 'other' else 'gdf'}, predicate='intersects'); intersection areas summed per feature",
                    f"SELECT a.id, count(*), sum(ST_Area(ST_Intersection(a.geom, b.geom))) FROM a JOIN {other if other != 'other' else 'a'} b ON a.id <> b.id AND ST_Intersects(a.geom, b.geom) GROUP BY a.id"),
        "grid": (f"cells = gpd.GeoDataFrame(geometry=[box(...) for each {p.get('size')} cell]); gpd.sjoin(cells, points).groupby(level=0).size()"
                 if str(p.get("shape", "square")) != "hex" else "hexbin.aggregate(gdf, resolution=..., value_field=..., agg=...)",
                 f"ST_SquareGrid({prov.get('size_m', p.get('size'))}, ST_Extent(geom)) joined with ST_Intersects" if str(p.get("shape", "square")) != "hex" else "h3_lat_lng_to_cell(geom, res) GROUP BY cell (h3-pg)"),
        "hexbin": (f"hexbin.aggregate(gdf, resolution={p.get('resolution', 'auto')!r}, value_field={p.get('value')!r}, agg='{p.get('agg', 'count')}')",
                   f"SELECT h3_lat_lng_to_cell(geom, {prov.get('resolution', p.get('resolution', 'res'))}) AS h3, count(*) FROM points GROUP BY 1 (h3-pg)"),
        "raster_zonal": (f"rasterstats.zonal_stats(gdf.to_crs(raster.crs), '{Path(str(p.get('raster', 'raster.tif'))).name}', stats=['{p.get('stat', 'sum')}'], "
                         f"all_touched={_bool(p.get('all_touched'))})",
                         f"SELECT a.id, (ST_SummaryStatsAgg(ST_Clip(r.rast, a.geom), 1, true)).{p.get('stat', 'sum')} FROM a JOIN raster r ON ST_Intersects(r.rast, a.geom) GROUP BY a.id"),
        "line_of_sight": ("terrain.line_of_sight(dem, a_lonlat, b_lonlat, h_a, h_b, k_factor, freq_ghz) for each link (bilinear DEM samples on the WGS84 geodesic; "
                          "curvature d1 d2 / (2 k R))", "(no PostGIS equivalent: sample ST_Value along ST_Segmentize(ST_MakeLine(a, b)) and compare with the chord)"),
        "viewshed": (f"gdal_viewshed -oz {p.get('h_m', 30)} -md {p.get('radius_m', 5000)} -cc {1 - 1 / float(p.get('k', 4 / 3)):.4f} dem.tif out.tif, then polygonize",
                     "(no PostGIS equivalent)"),
        "geocode": (f"geocode.geocode_table(df, address={_list(p.get('address')) or ['address']!r}, provider='{p.get('provider', 'census')}', purpose='{p.get('purpose', 'sites')}')"
                    " then gpd.points_from_xy(df.lon, df.lat, crs='EPSG:4326')",
                    f"a geocoder call per row ({prov.get('provider', p.get('provider', 'census'))}); PostGIS has none built in (TIGER geocoder extension: geocode(address))"),
        "fill_holes": ("gdf.geometry.apply(lambda g: Polygon(g.exterior) if g.geom_type == 'Polygon' else MultiPolygon([Polygon(p.exterior) for p in g.geoms]))",
                       "ST_MakePolygon(ST_ExteriorRing(geom))  # per part after ST_Dump; ST_Collect to rebuild"),
        "massing": (f"massing.mass(footprint.to_crs('{crs}'), dict(form='{p.get('form', 'solid')}')) per feature: parts with base_m and height_m, then to_crs(gdf.crs)",
                    "(no one-step PostGIS equivalent: ST_Buffer(hole, -k * row_m) rings intersected with ST_Intersection wedges, heights by row)"),
        "split_by": (f"gpd.overlay(gdf, {other}[['geometry']], how='intersection', keep_geom_type=True)  # with an STRtree query first",
                     f"SELECT a.*, ST_Intersection(a.geom, b.geom) FROM a JOIN {other} b ON ST_Intersects(a.geom, b.geom)"),
        "thin_lines": (f"gdf.assign(cell=grid_cell(gdf.to_crs('{crs}').centroid, {p.get('cell', 'cell')})).sort_values(rank_field, ascending=False).groupby('cell').head({p.get('max_per_cell', 'N')})",
                       f"SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY ST_SnapToGrid(ST_Centroid(geom), {p.get('cell', 'cell')}) ORDER BY rank DESC) AS n FROM a) t "
                       f"WHERE n <= {p.get('max_per_cell', 'N')}"),
        "dot_density": (f"per polygon: n = stochastic_round({val} / {p.get('per', 'per')}); n uniform points inside it (rejection sampling in its bounds), seeded",
                        "ST_GeneratePoints(geom, round(value / per)::int, seed) per polygon"),
        "density_surface": ("np.histogram2d(x, y, weights) then scipy.ndimage.gaussian_filter(sigma = bandwidth / cell); matplotlib contourf bands to polygons",
                            "(no one-step PostGIS equivalent: ST_SnapToGrid counts, then a kernel in SQL or raster ST_MapAlgebra, then ST_DumpAsPolygons)"),
        "flow_lines": ("for each row: a quadratic arc from the origin point to the destination, its control point bend x chord to the right of travel",
                       "ST_LineInterpolatePoints over ST_MakeLine(o, ST_Translate(midpoint, normal * bend * chord), d), or ST_CurveToLine of a CircularString"),
        "network_nearest": ("graph of the lines' shared vertices (scipy.sparse.csgraph): a virtual source joined to every site's snapped vertex, one "
                            "dijkstra; minutes = metres / (speed_kmh / 0.06), banded",
                            "pgr_dijkstraCost(edges_sql, ARRAY[site vertex ids], ARRAY[every vertex id]) with min() per end vertex (pgRouting), "
                            "then ST_LineLocatePoint per line"),
        "reconcile": (f"gpd.sjoin(gdf, {other}.to_crs(gdf.crs).buffer(tolerance), predicate='intersects') then share of length within tolerance >= threshold",
                      f"SELECT a.id, ST_Length(ST_Intersection(a.geom, ST_Buffer(b.geom, tol))) / ST_Length(a.geom) FROM a JOIN {other} b ON ST_DWithin(a.geom, b.geom, tol)"),
    }
    g, s = table.get(op, ("(no one-line equivalent)", "(no one-line equivalent)"))
    return {"geopandas": g, "postgis": s}
