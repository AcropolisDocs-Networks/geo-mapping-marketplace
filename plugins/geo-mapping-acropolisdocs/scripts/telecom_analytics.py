#!/usr/bin/env python3
"""telecom_analytics: the RAN analytics pack (gap analysis P1-5; M11 part B). Eight analyses over a site list and the tables that go
with it, each built from geo_ops and hexbin (never reimplemented here) and each returning an AnalysisResult the `network-analysis`
pipeline turns into layer files, MapSpecs and a report.

    site_spacing(sites)                         nearest-site distance per site, sites per km2 by H3 hexagon, spacing statistics
    best_server(sites, clip=None)               geometric (Voronoi) best-server cells with their area
    footprint_overlap(footprints, by="band")    overlap count and area per footprint, within a band and across bands
    backhaul_topology(sites, links)             straight lines between linked sites, length, hub degree, unmatched ids
    neighbor_relations(sites, neighbors)        lines between the sites of neighbour-cell pairs with a handover success rate
    drive_test_bins(points, kpi, ...)           H3 bins of drive-test or crowd samples with a count and a KPI statistic
    sector_kpi(wedges, kpi_table, key, value)   a KPI per sector joined onto the wedge polygons
    population_in_footprints(footprints, zones, value, sites=None, radius=None)   households or population in each footprint, and within N km of each site

Every result is DISPLAY and MEASUREMENT of geometry. Nothing here models radio propagation: a nearest-site distance is a straight line,
a best-server cell is the area nearer one site than any other, an overlap is where two drawn footprints coincide, a drive-test bin is the
statistic of the samples that fell in it. Each note says so where a geometric proxy stands in for an RF quantity, and the maps the
pipeline builds say it again (MAP-TEL-020 applies to the wedges).

Inputs are GeoDataFrames in any CRS with a CRS stated; distances are measured in a projected CRS chosen by geo_ops (never degrees) and
area fields are geodesic. Site tables come from telecom_geo.ingest_sites; `site_points` collapses its sector rows to one point per site.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import geo_ops  # noqa: E402
import hexbin  # noqa: E402
import table_join as tj  # noqa: E402
import telecom_geo as tg  # noqa: E402

WGS84 = "EPSG:4326"
PROXY_NOTE = "This is a geometric measure of the site layout, not a propagation model: no terrain, antenna pattern, power or clutter is in it."
LINK_A = ("a_site", "site_a", "a", "from", "from_site", "a_end", "source", "source_site", "site1", "near_end", "hub")
LINK_B = ("b_site", "site_b", "b", "to", "to_site", "b_end", "target", "target_site", "site2", "far_end", "remote")
NBR_SRC = ("source_cell", "source", "src", "serving_cell", "serving", "cell", "cell_id", "from_cell", "src_cell", "pci_source")
NBR_TGT = ("target_cell", "target", "tgt", "neighbor_cell", "neighbour_cell", "neighbor", "neighbour", "to_cell", "tgt_cell", "pci_target")
HO_ATT = ("ho_attempts", "attempts", "ho_att", "handover_attempts", "attempt", "ho_total")
HO_SUC = ("ho_success", "success", "ho_succ", "handover_success", "successes", "ho_ok")
CELL_ID = ("cell_id", "cellid", "cell", "cell_name", "sector_id", "sector", "pci", "eci", "cgi", "nci")
VALUE_NAMES = ("households", "hh", "housing_units", "population", "pop", "total_pop", "persons", "people")
KPI_AGGS = ("mean", "median", "p10", "p90", "count", "min", "max", "sum")
OVERLAP_CLASSES = ("No overlap", "Overlaps 1 other", "Overlaps 2 or more")
DELTA_HINT = re.compile(r"(delta|change|diff|_vs_|improve|degrad|gain|loss|uplift|net)", re.I)


class AnalyticsError(ValueError):
    """A usage problem with a message safe to show; `fid` names the rule that refused."""

    def __init__(self, message: str, fid: str = "MAP-TEL-010"):
        super().__init__(message)
        self.fid = fid


@dataclass
class AnalysisResult:
    """gdf is the layer the map is drawn from; `layers` holds any further layers by name (always including the primary under `primary_name`)."""
    gdf: object
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    primary_name: str = "result"

    def as_tuple(self):
        """(gdf, notes, provenance), the shape geo_ops callers expect."""
        return self.gdf, self.notes, self.provenance


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _fmt(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not math.isfinite(x):
        return "n/a"
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:.2f}".rstrip("0").rstrip(".")


def _norm(c) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


def _pick(columns, names, wanted=None):
    """The column called `wanted` (any case), or the first of `names` present; None when nothing fits."""
    by = {_norm(c): c for c in columns}
    if wanted:
        if _norm(wanted) in by:
            return by[_norm(wanted)]
        raise AnalyticsError(f"there is no column '{wanted}' (columns: {', '.join(map(str, columns))})")
    for n in names:
        if n in by:
            return by[n]
    return None


def _clean_id(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return None
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def _require_points(gdf, what: str):
    if gdf is None or len(gdf) == 0:
        raise AnalyticsError(f"{what}: no features")
    if gdf.crs is None:
        raise AnalyticsError(f"{what}: the layer has no CRS; say which it uses first")
    kinds = {g.geom_type for g in gdf.geometry if g is not None and not g.is_empty}
    if kinds - {"Point"}:
        raise AnalyticsError(f"{what} must be points (got {', '.join(sorted(kinds))}); use site_points on a site table")


def _require_polygons(gdf, what: str):
    if gdf is None or len(gdf) == 0:
        raise AnalyticsError(f"{what}: no features")
    if gdf.crs is None:
        raise AnalyticsError(f"{what}: the layer has no CRS; say which it uses first")
    kinds = {g.geom_type.replace("Multi", "") for g in gdf.geometry if g is not None and not g.is_empty}
    if kinds - {"Polygon"}:
        raise AnalyticsError(f"{what} must be polygons (got {', '.join(sorted(kinds))})")


def _ctx(**layers) -> geo_ops.Ctx:
    return geo_ops.Ctx(layers=layers, layer_id="analysis")


def _stats(values) -> dict:
    import numpy as np
    x = np.asarray(values, dtype="float64")
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {"n": 0, "min": None, "p10": None, "median": None, "mean": None, "p90": None, "max": None}
    return {"n": int(x.size), "min": float(x.min()), "p10": float(np.percentile(x, 10)), "median": float(np.median(x)), "mean": float(x.mean()),
            "p90": float(np.percentile(x, 90)), "max": float(x.max())}


# --------------------------------------------------------------------------------------------------------------------- sites
def site_points(df, id_col: str = "site_id"):
    """One point per site from a site table (telecom_geo.ingest_sites output, or any frame with lon/lat/site_id), in EPSG:4326.

    Sector rows of one site collapse to the first row's position; `n_sectors` counts them and `bands` lists the bands seen. A table with
    no site id column gets one from the row number (S1, S2, ...). A GeoDataFrame of points is returned as it is, collapsed on the id."""
    import geopandas as gpd
    import pandas as pd
    import shapely
    if hasattr(df, "geometry") and getattr(df, "crs", None) is not None:
        g = df.to_crs(WGS84) if not df.crs.equals(WGS84) else df.copy()
        if id_col not in g.columns:
            g[id_col] = [f"S{i + 1}" for i in range(len(g))]
        g[id_col] = g[id_col].map(_clean_id)
        g = g[g[id_col].notna()]
        g = g[~g[id_col].duplicated(keep="first")].reset_index(drop=True)
        return gpd.GeoDataFrame(g, geometry=g.geometry.name, crs=WGS84)
    if "lon" not in df.columns or "lat" not in df.columns:
        raise AnalyticsError("a site table needs lon and lat columns (telecom_geo.ingest_sites gives them)")
    t = df.copy()
    if id_col not in t.columns:
        t[id_col] = [f"S{i + 1}" for i in range(len(t))]
    t[id_col] = t[id_col].map(_clean_id)
    t = t[t[id_col].notna() & t["lon"].notna() & t["lat"].notna()]
    grp = t.groupby(id_col, sort=False)
    first = grp.first()
    out = pd.DataFrame({id_col: first.index})
    out["lon"] = first["lon"].values
    out["lat"] = first["lat"].values
    out["n_sectors"] = grp.size().values
    for c in ("site_type", "tech"):
        if c in t.columns:
            out[c] = first[c].values
    if "band" in t.columns:
        out["bands"] = grp["band"].agg(lambda s: ", ".join(tg.canonical_order(s.dropna().astype(str), "band"))).values
    geoms = [shapely.Point(float(x), float(y)) for x, y in zip(out["lon"], out["lat"])]
    return gpd.GeoDataFrame(out.drop(columns=["lon", "lat"]), geometry=geoms, crs=WGS84)


def _site_lookup(sites, id_col: str = "site_id") -> dict:
    g = sites.to_crs(WGS84) if not sites.crs.equals(WGS84) else sites
    return {_clean_id(k): geom for k, geom in zip(g[id_col], g.geometry) if _clean_id(k) is not None and geom is not None and not geom.is_empty}


# --------------------------------------------------------------------------------------------------------------------- 1 spacing
def site_spacing(sites, resolution="auto", units: str = "km", id_col: str = "site_id") -> AnalysisResult:
    """Nearest-site distance per site (straight line) and sites per km2 by H3 hexagon. Primary: the sites with `nearest_site_id` and
    `nearest_<units>`; `layers["density"]`: the hexagons with `count` and `sites_per_km2`."""
    import numpy as np
    _require_points(sites, "site_spacing")
    if id_col not in sites.columns:
        raise AnalyticsError(f"site_spacing needs a '{id_col}' column (site_points gives one)")
    if len(sites) < 2:
        raise AnalyticsError("site_spacing needs at least two sites")
    units = (units or "km").lower()
    if units not in geo_ops.UNITS_M:
        raise AnalyticsError(f"unknown unit '{units}'")
    r = geo_ops.op_nearest(sites, {"op": "nearest", "layer": "sites", "id_field": id_col, "units": units, "exclusive": True}, _ctx(sites=sites))
    out = r.gdf.rename(columns={"nearest_id": "nearest_site_id", f"nearest_distance_{units}": f"nearest_{units}"})
    if "nearest_rank" in out.columns:
        out = out.drop(columns=["nearest_rank"])
    st = _stats(out[f"nearest_{units}"])
    basis = None
    if resolution in (None, "auto"):              # a cell whose edge is about the median spacing holds a few sites, so a density shows rather than one site per cell
        med_m = float(st["median"] or 1.0) * geo_ops.UNITS_M[units]
        resolution = min(10, max(4, hexbin.resolution_for_size(med_m)))
        basis = f"chosen so a cell's edge (about {hexbin.EDGE_KM[resolution]:g} km) is near the median inter-site distance"
    cells, hnotes, hprov = hexbin.aggregate(sites, resolution=resolution, agg="count")
    if basis:
        hprov["resolution_basis"] = basis
    area = geo_ops.op_area(cells, {"op": "area", "units": "km2", "field": "cell_km2"}, _ctx())
    cells = area.gdf
    cells["sites_per_km2"] = cells["count"] / cells["cell_km2"].where(cells["cell_km2"] > 0, np.nan)
    dens = _stats(cells["sites_per_km2"])
    notes = [
        f"`nearest_site_id` and `nearest_{units}`: for each of the {len(out)} sites, the nearest other site and the straight-line distance to it, measured in "
        f"{r.provenance['measure_crs']}. Median inter-site distance {_fmt(st['median'])} {units} (p10 {_fmt(st['p10'])}, p90 {_fmt(st['p90'])}). {PROXY_NOTE}",
        f"`sites_per_km2`: sites counted into H3 hexagons at resolution {hprov['resolution']} ({hprov['resolution_basis']}) and divided by each cell's geodesic area "
        f"({_fmt(cells['cell_km2'].median())} km2 a cell); the densest cell holds {_fmt(dens['max'])} sites per km2.",
    ] + r.notes
    summary = {"sites": int(len(out)), "units": units, "nearest": st, "hex_resolution": int(hprov["resolution"]), "hex_cells": int(len(cells)), "sites_per_km2": dens}
    prov = {"analysis": "site_spacing", "ops": [r.provenance, dict(hprov, op="hexbin"), area.provenance], "rows_in": int(len(sites)), "rows_out": int(len(out)), "summary": summary}
    return AnalysisResult(out, notes, list(r.findings), prov, {"sites": out, "density": cells}, summary, "sites")


# --------------------------------------------------------------------------------------------------------------------- 2 best server
def best_server(sites, clip=None, extend_km: float | None = None, id_col: str = "site_id") -> AnalysisResult:
    """Voronoi cells per site, cut to `clip` (a polygon layer) or to the sites' convex hull buffered by `extend_km` (default: the median
    nearest-site distance, at least 1 km). Primary: the cells with the site's attributes and `area_km2`."""
    _require_points(sites, "best_server")
    if id_col not in sites.columns:
        raise AnalyticsError(f"best_server needs a '{id_col}' column (site_points gives one)")
    notes, _findings, ops = [], [], []
    if clip is not None:
        _require_polygons(clip, "best_server clip")
        mask, how = clip, f"cut to the {len(clip)} polygon(s) given (a site outside the boundary keeps no cell, so ground nearer to it than to any site inside is left blank)"
    else:
        if extend_km is None:
            if len(sites) >= 2:
                near = geo_ops.op_nearest(sites, {"op": "nearest", "layer": "sites", "units": "km", "exclusive": True}, _ctx(sites=sites))
                med = _stats(near.gdf["nearest_distance_km"])["median"]
                extend_km = max(1.0, float(med or 1.0))
            else:
                extend_km = 1.0
        hull = geo_ops.op_convex_hull(sites, {"op": "convex_hull"}, _ctx())
        buf = geo_ops.op_buffer(hull.gdf, {"op": "buffer", "distance": extend_km, "units": "km"}, _ctx())
        ops += [hull.provenance, buf.provenance]
        mask, how = buf.gdf, f"cut to the sites' convex hull extended by {_fmt(extend_km)} km (no market boundary was given)"
    vor = geo_ops.op_voronoi(sites, {"op": "voronoi", "clip_layer": "boundary", "id_field": id_col}, _ctx(boundary=mask))
    cells = vor.gdf
    area = geo_ops.op_area(cells, {"op": "area", "units": "km2", "field": "area_km2"}, _ctx())
    cells = area.gdf
    st = _stats(cells["area_km2"])
    notes.append(f"Best-server cells by straight-line distance: each cell is the ground nearer its site than any other site, {how}. {len(sites)} site(s) gave {len(cells)} cell(s); "
                 f"median cell {_fmt(st['median'])} km2 (smallest {_fmt(st['min'])}, largest {_fmt(st['max'])}). Distance-based best server, no propagation model: a real best server depends on "
                 f"antenna direction, height, band and terrain, none of which is in this drawing.")
    notes += vor.notes
    summary = {"sites": int(len(sites)), "cells": int(len(cells)), "area_km2": st, "clip": "given polygon" if clip is not None else f"hull + {_fmt(extend_km)} km"}
    prov = {"analysis": "best_server", "ops": ops + [vor.provenance, area.provenance], "rows_in": int(len(sites)), "rows_out": int(len(cells)), "summary": summary}
    return AnalysisResult(cells, notes, list(vor.findings), prov, {"cells": cells, "sites": sites}, summary, "cells")


# --------------------------------------------------------------------------------------------------------------------- 3 overlap
def _multi_share(polys, crs: str) -> tuple[float, float]:
    """(area covered by at least one polygon, area covered by two or more), in km2, measured in `crs`."""
    import shapely
    g = polys.to_crs(crs).geometry.values
    g = g[[x is not None and not x.is_empty for x in g]]
    if len(g) == 0:
        return 0.0, 0.0
    total = shapely.area(shapely.union_all(g)) / 1e6
    tree = shapely.STRtree(g)
    li, ri = tree.query(g, predicate="intersects")
    keep = li < ri
    li, ri = li[keep], ri[keep]
    if len(li) == 0:
        return float(total), 0.0
    inter = shapely.intersection(g[li], g[ri])
    inter = inter[[x is not None and not x.is_empty and shapely.area(x) > 0 for x in inter]]
    multi = shapely.area(shapely.union_all(list(inter))) / 1e6 if len(inter) else 0.0
    return float(total), float(multi)


def footprint_overlap(footprints, by: str | None = "band", units: str = "km2") -> AnalysisResult:
    """`overlap_count` and `overlap_area_<units>` across all footprints and, with `by`, `overlap_count_same` and `overlap_area_same_<units>` within
    the same value of `by` (n41 against n41). The summary gives, for all footprints and for each `by` value, the share of the footprint area
    under two or more footprints."""
    _require_polygons(footprints, "footprint_overlap")
    units = geo_ops._area_unit(units)
    if by and by not in footprints.columns:
        by = None
    r = geo_ops.op_overlap(footprints, {"op": "overlap", "layer": "self", "units": units}, _ctx())
    out = r.gdf
    out["overlap_class"] = out["overlap_count"].map(lambda n: OVERLAP_CLASSES[0] if n <= 0 else (OVERLAP_CLASSES[1] if n == 1 else OVERLAP_CLASSES[2]))
    ops = [r.provenance]
    crs = r.provenance["measure_crs"]
    total, multi = _multi_share(footprints, crs)
    rows = [{"group": "all footprints", "footprints": int(len(footprints)), "area_km2": total, "area_2plus_km2": multi, "share_2plus": (multi / total) if total > 0 else 0.0,
             "with_overlap": int((out["overlap_count"] > 0).sum())}]
    if by:
        out["overlap_count_same"] = 0
        out[f"overlap_area_same_{units}"] = 0.0
        for val, grp in footprints.groupby(by, dropna=False, sort=True):
            if len(grp) < 1:
                continue
            rs = geo_ops.op_overlap(grp, {"op": "overlap", "layer": "self", "units": units}, _ctx())
            out.loc[rs.gdf.index, "overlap_count_same"] = rs.gdf["overlap_count"].values
            out.loc[rs.gdf.index, f"overlap_area_same_{units}"] = rs.gdf[f"overlap_area_{units}"].values
            t, m = _multi_share(grp, crs)
            rows.append({"group": f"{by} = {val}", "footprints": int(len(grp)), "area_km2": t, "area_2plus_km2": m, "share_2plus": (m / t) if t > 0 else 0.0,
                         "with_overlap": int((rs.gdf["overlap_count"] > 0).sum())})
        out["overlap_count_same"] = out["overlap_count_same"].astype(int)
    notes = [f"Overlap of the drawn footprints, measured in {crs}: {rows[0]['with_overlap']} of {len(footprints)} footprints overlap another; "
             f"{_fmt(100 * rows[0]['share_2plus'])} percent of the footprint area lies under two or more footprints."
             + (f" Within one {by} the shares are: " + "; ".join(f"{x['group'].split('= ', 1)[-1]} {_fmt(100 * x['share_2plus'])} percent" for x in rows[1:]) + "." if by else "")
             + " The footprints are display geometry (a drawing radius and beamwidth), so an overlap here is where two drawn sectors coincide, not where two signals can be received. " + PROXY_NOTE] + r.notes
    summary = {"footprints": int(len(footprints)), "by": by, "units": units, "table": rows}
    prov = {"analysis": "footprint_overlap", "ops": ops, "rows_in": int(len(footprints)), "rows_out": int(len(out)), "summary": summary, "measure_crs": crs}
    return AnalysisResult(out, notes, list(r.findings), prov, {"footprints": out}, summary, "footprints")


# --------------------------------------------------------------------------------------------------------------------- 4 backhaul
def _lines_between(sites, pairs, a_col: str, b_col: str, id_col: str, what: str, fid: str):
    """LineStrings between the sites named in `pairs[a_col]` and `pairs[b_col]`. -> (lines gdf, unmatched ids, dropped rows, findings)."""
    import geopandas as gpd
    import shapely
    lut = _site_lookup(sites, id_col)
    a = pairs[a_col].map(_clean_id)
    b = pairs[b_col].map(_clean_id)
    ok_a, ok_b = a.map(lambda v: v in lut), b.map(lambda v: v in lut)
    missing = sorted({v for v, ok in zip(a, ok_a) if not ok and v is not None} | {v for v, ok in zip(b, ok_b) if not ok and v is not None})
    blank = int((a.isna() | b.isna()).sum())
    keep = ok_a & ok_b
    fs = []
    if missing:
        fs.append(finding(fid, "WARN", f"{len(missing)} {what} id(s) are not in the site list and their {int((~keep).sum() - blank)} row(s) are not drawn: "
                          + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""), missing[:200]))
    if blank:
        fs.append(finding(fid, "WARN", f"{blank} row(s) have a blank id and are not drawn"))
    rows = pairs[keep].copy()
    rows[a_col], rows[b_col] = a[keep].values, b[keep].values
    geoms = [shapely.LineString([lut[x], lut[y]]) for x, y in zip(rows[a_col], rows[b_col])]
    g = gpd.GeoDataFrame(rows.reset_index(drop=True), geometry=geoms, crs=WGS84)
    return g, missing, int((~keep).sum()), fs


def backhaul_topology(sites, links, id_col: str = "site_id") -> AnalysisResult:
    """Straight lines between the two sites of each link row (a_site, b_site; any other columns such as type and capacity carried), with
    `length_km`; `layers["sites"]` carries `degree` (links per site). Ids not in the site list are a MAP-TEL-060 finding and are not drawn."""
    import pandas as pd
    _require_points(sites, "backhaul_topology")
    if id_col not in sites.columns:
        raise AnalyticsError(f"backhaul_topology needs a '{id_col}' column on the sites")
    links = pd.DataFrame(links)
    a_col, b_col = _pick(links.columns, LINK_A), _pick(links.columns, LINK_B)
    if not a_col or not b_col:
        raise AnalyticsError(f"the links table needs two site id columns (a_site, b_site; from, to); found: {', '.join(map(str, links.columns))}")
    g, missing, dropped, fs = _lines_between(sites, links, a_col, b_col, id_col, "link", "MAP-TEL-060")
    g = g.rename(columns={a_col: "a_site", b_col: "b_site"})
    self_loop = g["a_site"] == g["b_site"]
    if self_loop.any():
        fs.append(finding("MAP-TEL-060", "INFO", f"{int(self_loop.sum())} link(s) join a site to itself and are not drawn"))
        g = g[~self_loop].reset_index(drop=True)
    if len(g) == 0:
        raise AnalyticsError("no link joins two sites of the site list", "MAP-TEL-060")
    ln = geo_ops.op_length(g, {"op": "length", "units": "km", "field": "length_km"}, _ctx())
    g = ln.gdf
    deg = pd.concat([g["a_site"], g["b_site"]]).value_counts()
    sites_out = sites.copy()
    sites_out["degree"] = sites_out[id_col].map(_clean_id).map(deg).fillna(0).astype(int)
    hubs = deg.head(5)
    st = _stats(g["length_km"])
    type_col = _pick(g.columns, ("type", "link_type", "medium", "technology"))
    by_type = g.groupby(type_col).size().to_dict() if type_col else {}
    notes = [f"Backhaul topology: {len(g)} link(s) drawn as straight lines between site positions, {_fmt(g['length_km'].sum())} km in all (median {_fmt(st['median'])} km, "
             f"longest {_fmt(st['max'])} km, geodesic). `degree` on the sites counts the links at each; the busiest {'is' if len(hubs) == 1 else 'are'} "
             + ", ".join(f"{k} ({v})" for k, v in hubs.items()) + ". A straight line is the shortest possible route, not the fibre or microwave path as built."
             + (f" Link types: {', '.join(f'{k} {v}' for k, v in by_type.items())}." if by_type else "")
             + (f" {len(missing)} id(s) in the links table were not in the site list." if missing else "")]
    summary = {"links": int(len(g)), "dropped_rows": dropped, "unmatched_ids": missing, "length_km": st, "total_km": float(g["length_km"].sum()),
               "hubs": [{"site": k, "degree": int(v)} for k, v in hubs.items()], "by_type": {str(k): int(v) for k, v in by_type.items()}}
    prov = {"analysis": "backhaul_topology", "ops": [ln.provenance], "rows_in": int(len(links)), "rows_out": int(len(g)), "summary": summary}
    return AnalysisResult(g, notes, fs, prov, {"links": g, "sites": sites_out}, summary, "links")


# --------------------------------------------------------------------------------------------------------------------- 5 neighbours
def _cell_to_site(sites, cells=None, id_col: str = "site_id") -> dict:
    """cell id -> site id, from a cells table (cell_id, site_id), from the sites' own cell or sector column, and from the site ids themselves."""
    import pandas as pd
    lut = {}
    if cells is not None:
        c = pd.DataFrame(cells)
        cc, sc = _pick(c.columns, CELL_ID), _pick(c.columns, tg.ALIASES["site_id"])
        if not cc or not sc:
            raise AnalyticsError(f"the cells table needs a cell id and a site id column; found: {', '.join(map(str, c.columns))}")
        for k, v in zip(c[cc].map(_clean_id), c[sc].map(_clean_id)):
            if k is not None and v is not None:
                lut.setdefault(k, v)
    for col in ("sector", "cell_id", "cell", "sector_id"):
        if col in sites.columns:
            for k, v in zip(sites[col].map(_clean_id), sites[id_col].map(_clean_id)):
                if k is not None and v is not None:
                    lut.setdefault(k, v)
    for v in sites[id_col].map(_clean_id):
        if v is not None:
            lut.setdefault(v, v)
    return lut


def _resolve_cell(cell, lut: dict, site_ids: set):
    """The site of a cell id: the lookup first, then the convention `<site id>-<n>` (or `_<n>`) that coverage-map's sector ids follow."""
    if cell is None:
        return None
    if cell in lut:
        return lut[cell]
    m = re.match(r"^(.*?)[-_](\d{1,3})$", cell)
    if m and m.group(1) in site_ids:
        return m.group(1)
    return None


def neighbor_relations(sites, neighbors, cells=None, id_col: str = "site_id") -> AnalysisResult:
    """Lines between the sites of each neighbour-cell pair (source_cell, target_cell; optional ho_attempts, ho_success -> `success_rate`).
    A cell maps to its site through `cells` (cell_id, site_id), the sites' own sector or cell column, the site id itself, or the convention
    `<site id>-<n>` that coverage-map's sector ids follow. Cells that map to nothing are a MAP-TEL-061 finding; pairs within one site are counted, not drawn."""
    import numpy as np
    import pandas as pd
    _require_points(sites, "neighbor_relations")
    if id_col not in sites.columns:
        raise AnalyticsError(f"neighbor_relations needs a '{id_col}' column on the sites")
    nb = pd.DataFrame(neighbors)
    s_col, t_col = _pick(nb.columns, NBR_SRC), _pick(nb.columns, NBR_TGT)
    if not s_col or not t_col:
        raise AnalyticsError(f"the neighbours table needs a source cell and a target cell column; found: {', '.join(map(str, nb.columns))}")
    lut = _cell_to_site(sites, cells, id_col)
    t = pd.DataFrame({"source_cell": nb[s_col].map(_clean_id), "target_cell": nb[t_col].map(_clean_id)})
    site_ids = {v for v in sites[id_col].map(_clean_id) if v is not None}
    t["source_site"] = t["source_cell"].map(lambda c: _resolve_cell(c, lut, site_ids))
    t["target_site"] = t["target_cell"].map(lambda c: _resolve_cell(c, lut, site_ids))
    att_col, suc_col = _pick(nb.columns, HO_ATT), _pick(nb.columns, HO_SUC)
    if att_col:
        t["ho_attempts"] = pd.to_numeric(nb[att_col], errors="coerce")
    if suc_col:
        t["ho_success"] = pd.to_numeric(nb[suc_col], errors="coerce")
    for c in nb.columns:
        if c not in (s_col, t_col, att_col, suc_col) and c not in t.columns:
            t[c] = nb[c].values
    missing = sorted({v for v, s in zip(t["source_cell"], t["source_site"]) if v is not None and pd.isna(s)} | {v for v, s in zip(t["target_cell"], t["target_site"]) if v is not None and pd.isna(s)})
    fs = []
    if missing:
        n_rows = int((t["source_site"].isna() | t["target_site"].isna()).sum())
        fs.append(finding("MAP-TEL-061", "WARN", f"{len(missing)} neighbour cell id(s) belong to no site of the site list (give a cells table with cell_id and site_id); {n_rows} relation(s) are not drawn: "
                          + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""), missing[:200]))
    t = t[t["source_site"].notna() & t["target_site"].notna()].copy()
    intra = t["source_site"] == t["target_site"]
    n_intra = int(intra.sum())
    t = t[~intra].copy()
    if len(t) == 0:
        raise AnalyticsError("no neighbour relation joins two different sites of the site list", "MAP-TEL-061")
    g, _m, _d, fs2 = _lines_between(sites, t, "source_site", "target_site", id_col, "neighbour", "MAP-TEL-061")
    fs += fs2
    if "ho_attempts" in g.columns and "ho_success" in g.columns:
        g["success_rate"] = (g["ho_success"] / g["ho_attempts"].where(g["ho_attempts"] > 0, np.nan))
        g["success_pct"] = g["success_rate"] * 100.0
        rate_note = (f" `success_rate` is handover successes over attempts per relation; median {_fmt(100 * float(np.nanmedian(g['success_rate'])))} percent, "
                     f"{int((g['success_rate'] < 0.9).sum())} relation(s) below 90 percent" + (f", {int(g['success_rate'].isna().sum())} with no attempts" if g["success_rate"].isna().any() else "") + ".")
    else:
        rate_note = " No handover counts were given, so the lines carry the relation only."
    ln = geo_ops.op_length(g, {"op": "length", "units": "km", "field": "length_km"}, _ctx())
    g = ln.gdf
    st = _stats(g["length_km"])
    notes = [f"Neighbour relations: {len(g)} relation(s) between {g[['source_site', 'target_site']].stack().nunique()} sites, drawn as straight lines from the serving cell's site to the "
             f"neighbour's site (median {_fmt(st['median'])} km, longest {_fmt(st['max'])} km)." + rate_note
             + (f" {n_intra} relation(s) between cells of the same site are counted but not drawn." if n_intra else "")
             + " The line joins site positions; it says nothing about where the handover happens or about signal levels. " + PROXY_NOTE]
    summary = {"relations": int(len(g)), "intra_site": n_intra, "unmatched_cells": missing, "length_km": st,
               "success_rate": _stats(g["success_rate"]) if "success_rate" in g.columns else None,
               "below_90": int((g["success_rate"] < 0.9).sum()) if "success_rate" in g.columns else None}
    prov = {"analysis": "neighbor_relations", "ops": [ln.provenance], "rows_in": int(len(nb)), "rows_out": int(len(g)), "summary": summary}
    return AnalysisResult(g, notes, fs, prov, {"relations": g, "sites": sites}, summary, "relations")


# --------------------------------------------------------------------------------------------------------------------- 6 drive test
def drive_test_bins(points, kpi_field: str | None = None, resolution="auto", agg: str = "mean", min_count: int = 1) -> AnalysisResult:
    """H3 bins of drive-test or crowd samples: `count` per cell and `<kpi>_<agg>` (mean, median, p10, p90, min, max, sum; count alone needs
    no KPI). Cells with fewer than `min_count` samples are left out and counted in the note."""
    import pandas as pd
    _require_points(points, "drive_test_bins")
    agg = (agg or "mean").lower()
    if agg not in KPI_AGGS:
        raise AnalyticsError(f"agg must be one of: {', '.join(KPI_AGGS)}")
    if agg != "count":
        if not kpi_field:
            raise AnalyticsError("drive_test_bins needs the KPI field (rsrp, sinr, throughput) unless agg is count")
        kpi_field = _pick(points.columns, (), kpi_field)
    min_count = max(1, int(min_count or 1))
    base_agg = agg if agg in hexbin.AGGS else "count"
    cells, hnotes, hprov = hexbin.aggregate(points, resolution=resolution, value_field=kpi_field if base_agg != "count" else None, agg=base_agg, min_count=1)
    vfield = f"{kpi_field}_{agg}" if agg != "count" else None
    if agg in ("p10", "p90"):
        h3 = hexbin._h3()
        g = points.to_crs(WGS84) if not points.crs.equals(WGS84) else points
        ok = g.geometry.notna() & ~g.geometry.is_empty
        df = pd.DataFrame({"h3": [h3.latlng_to_cell(float(p.y), float(p.x), int(hprov["resolution"])) for p in g.geometry[ok]],
                           "v": pd.to_numeric(g.loc[ok, kpi_field], errors="coerce").values})
        q = df.groupby("h3")["v"].quantile(0.10 if agg == "p10" else 0.90)
        cells[vfield] = cells["h3"].map(q).values
    thin = cells["count"] < min_count
    n_thin = int(thin.sum())
    out = cells[~thin].reset_index(drop=True)
    fs = []
    if n_thin:
        fs.append(finding("MAP-TEL-062", "INFO", f"{n_thin} bin(s) with fewer than {min_count} samples were left out of the map; their statistic would rest on too few samples", n_thin))
    st = _stats(out[vfield]) if vfield else None
    cs = _stats(out["count"])
    notes = [f"Drive-test bins: {int(hprov['rows_in'])} sample(s) binned into H3 hexagons at resolution {hprov['resolution']} ({hprov['resolution_basis']}; about "
             f"{2.598 * hexbin.EDGE_KM[int(hprov['resolution'])] ** 2:.3g} km2 a cell), {len(out)} cell(s) with at least {min_count} sample(s) kept"
             + (f" ({n_thin} thinner cell(s) left out)" if n_thin else "") + f"; median {_fmt(cs['median'])} samples a cell."
             + (f" `{vfield}` is the {agg} of `{kpi_field}` over the samples in each cell: from {_fmt(st['min'])} to {_fmt(st['max'])}, median {_fmt(st['median'])}." if vfield else "")
             + " A cell's value is the statistic of the samples that fell in it (where the vehicle or the handsets went), not a prediction for the whole cell; empty cells were not measured, not uncovered."]
    summary = {"samples": int(hprov["rows_in"]), "resolution": int(hprov["resolution"]), "cells": int(len(out)), "thin_cells": n_thin, "min_count": min_count, "kpi": kpi_field, "agg": agg,
               "value_field": vfield, "kpi_stats": st, "count_stats": cs}
    prov = {"analysis": "drive_test_bins", "ops": [dict(hprov, op="hexbin", agg=agg)], "rows_in": int(len(points)), "rows_out": int(len(out)), "summary": summary}
    return AnalysisResult(out, notes, fs, prov, {"bins": out}, summary, "bins")


# --------------------------------------------------------------------------------------------------------------------- 7 sector KPI
def sector_kpi(wedges, kpi, key="cell_id", value: str | None = None, diverging: bool | None = None) -> AnalysisResult:
    """A KPI per sector joined onto the wedge polygons (table_join, so dropped zeros and decimal keys are repaired and reported). `key` is one
    column present on both sides (cell_id, sector_id) or two columns (site_id + sector, site_id + azimuth) joined as a composite key. The
    result's `kpi_diverging` says whether the value reads as a change (a delta around zero) and so wants a diverging ramp."""
    import numpy as np
    import pandas as pd
    _require_polygons(wedges, "sector_kpi")
    t = pd.DataFrame(kpi)
    keys = [key] if isinstance(key, str) else list(key)
    if not keys or len(keys) > 2:
        raise AnalyticsError("key is one column, or two columns joined (site_id + sector)")
    nums = [c for c in t.columns if c not in keys and tj._to_number(t[c]).dtype.kind in "if"]
    if value:
        value = _pick(t.columns, (), value)
    elif len(nums) == 1:
        value = nums[0]
    else:
        raise AnalyticsError(f"say which KPI column to map with value= (numeric columns: {', '.join(nums) or 'none'})")
    g = wedges.copy()
    if len(keys) == 1:
        k = keys[0]
        right = {_norm(c): c for c in t.columns}.get(_norm(k)) or _pick(t.columns, CELL_ID)
        if right is None:
            raise AnalyticsError(f"the KPI table has no '{k}' column to join on (columns: {', '.join(map(str, t.columns))})")
        left = {_norm(c): c for c in g.columns}.get(_norm(k)) or _pick(g.columns, ("sector", "sector_id", "cell_id", "cell"))
        if left is None:
            raise AnalyticsError(f"the wedges have no '{k}' column to join on (columns: {', '.join(c for c in g.columns if c != g.geometry.name)})")
    else:
        lk = [_pick(g.columns, (), k) for k in keys]
        rk = [_pick(t.columns, (), k) for k in keys]
        def comp(frame, cols):
            parts = []
            for c in cols:
                s = frame[c]
                if pd.api.types.is_numeric_dtype(s):
                    s = s.map(lambda v: _clean_id(v) if v == v else None).map(lambda v: str(int(float(v))) if v is not None and re.fullmatch(r"-?\d+(\.0+)?", v) else v)
                else:
                    s = s.map(_clean_id)
                parts.append(s.astype(object))
            return parts[0].astype(str) + "|" + parts[1].astype(str)
        g["__sector_key"] = comp(g, lk)
        t["__sector_key"] = comp(t, rk)
        left = right = "__sector_key"
    joined, steps, jnotes, fs, rep = tj.join(g, t, left, right, columns=[value], table_name="the KPI table", numeric=[value])
    if "__sector_key" in joined.columns:
        joined = joined.drop(columns=["__sector_key"])
    vals = joined[value].to_numpy(dtype="float64", na_value=np.nan)
    finite = vals[np.isfinite(vals)]
    if finite.size == 0:
        raise AnalyticsError(f"'{value}' holds no numbers after the join", "MAP-JOIN-001")
    if diverging is None:
        diverging = bool(DELTA_HINT.search(value)) or bool(finite.size and finite.min() < 0 < finite.max())
    st = _stats(vals)
    notes = [f"`{value}` per sector: {rep['matched']} of {rep['features']} sector wedge(s) matched a KPI row" + (f" ({'; '.join(rep['repairs'])})" if rep["repairs"] else "")
             + f"; values run from {_fmt(st['min'])} to {_fmt(st['max'])}, median {_fmt(st['median'])}."
             + (" The value reads as a change, so the map centres a diverging ramp on zero." if diverging else "")
             + " The wedge is the sector's drawing (azimuth, beamwidth, a fixed radius); the colour is the KPI for that sector, not a value for every point inside the wedge."]
    summary = {"value": value, "key": keys, "matched": int(rep["matched"]), "sectors": int(rep["features"]), "unmatched_rows": int(rep["unmatched_rows"]), "features_without_row": int(rep["features_without_row"]),
               "diverging": bool(diverging), "stats": st}
    prov = {"analysis": "sector_kpi", "ops": [{"op": "table_join", "left": left, "right": right, "value": value, "report": {k: v for k, v in rep.items() if k != "table"}}], "rows_in": int(len(wedges)), "rows_out": int(len(joined)), "summary": summary,
            "kpi_diverging": bool(diverging)}
    return AnalysisResult(joined, notes + jnotes, list(fs), prov, {"sectors": joined}, summary, "sectors")


# --------------------------------------------------------------------------------------------------------------------- 8 population
def _value_col(zones, value):
    if value:
        return _pick(zones.columns, (), value)
    v = _pick(zones.columns, VALUE_NAMES)
    if not v:
        raise AnalyticsError(f"say which field of the population layer to apportion with value= (households, population); columns: {', '.join(c for c in zones.columns if c != zones.geometry.name)}")
    return v


def population_in_footprints(footprints, zones, value: str | None = None, sites=None, radius=None, radius_units: str = "km", zone_provenance: dict | None = None,
                             mode: str = "area_weighted") -> AnalysisResult:
    """`<value>_covered` per footprint by areal interpolation (geo_ops population_covered), and with `sites` and `radius` a second layer
    `layers["rings"]`: each site's circle of `radius` with `<value>_within_<radius>`. `zone_provenance` (a library entry's provenance: vintage,
    source) is written into the note so a population figure carries its ACS vintage."""
    import numpy as np
    _require_polygons(zones, "population zones")
    val = _value_col(zones, value)
    vintage = (zone_provenance or {}).get("vintage") or ((zone_provenance or {}).get("entry") or {}).get("vintage", {}).get("current")
    source = (zone_provenance or {}).get("source")
    tag = f" ({source}, {vintage})" if source and vintage else (f" ({vintage})" if vintage else (f" ({source})" if source else ""))
    layers, ops, notes, fs = {}, [], [], []
    fld = f"{val}_covered"
    out = None
    if footprints is not None and len(footprints):
        _require_polygons(footprints, "footprints")
        r = geo_ops.op_population_covered(footprints, {"op": "population_covered", "layer": "zones", "value": val, "mode": mode, "field": fld}, _ctx(zones=zones))
        out = r.gdf
        ops.append(r.provenance)
        fs += r.findings
        tot = float(r.provenance["source_total"])
        cov = float(r.provenance["covered_total"])
        notes.append(f"`{fld}`: {val}{tag} apportioned into each of the {len(out)} footprint(s) by the share of each zone's area inside it; the footprints together hold "
                     f"{_fmt(cov)} of the {_fmt(tot)} in the zone layer ({_fmt(100 * cov / tot) if tot else 'n/a'} percent). Overlapping footprints each count what lies under them, "
                     "so the per-footprint figures add to more than the ground covered. The footprints are display geometry, so this is the population under a drawn shape, not a served population.")
        notes += r.notes
        layers["footprints"] = out
    if sites is not None and radius is not None:
        _require_points(sites, "sites")
        rm, rv, ru = geo_ops.to_metres(radius, radius_units, "radius")
        b = geo_ops.op_buffer(sites, {"op": "buffer", "distance": rv, "units": ru}, _ctx())
        rfld = f"{val}_within_{f'{rv:g}'.replace('.', '_')}{ru}"
        rr = geo_ops.op_population_covered(b.gdf, {"op": "population_covered", "layer": "zones", "value": val, "mode": mode, "field": rfld}, _ctx(zones=zones))
        rings = rr.gdf
        rings["radius_" + ru] = rv
        ops += [b.provenance, rr.provenance]
        fs += rr.findings
        st = _stats(rings[rfld])
        notes.append(f"`{rfld}`: {val}{tag} within {_fmt(rv)} {ru} of each of the {len(rings)} site(s) (a circle around the site position, apportioned the same way): "
                     f"median {_fmt(st['median'])}, from {_fmt(st['min'])} to {_fmt(st['max'])}. A circle of {_fmt(rv)} {ru} is a distance band, not a service area.")
        layers["rings"] = rings
        layers["sites"] = sites
        if out is None:
            out = rings
    if out is None:
        raise AnalyticsError("population_in_footprints needs footprints, or sites with a radius")
    summary = {"value": val, "vintage": vintage, "source": source, "zones": int(len(zones)), "zone_total": float(np.nansum(np.asarray(zones[val], dtype="float64"))),
               "footprints": int(len(footprints)) if footprints is not None else 0,
               "covered_total": float(ops[0]["covered_total"]) if footprints is not None and len(footprints) else None,
               "radius": (f"{_fmt(rv)} {ru}" if sites is not None and radius is not None else None),
               "within_radius": _stats(layers["rings"][rfld]) if "rings" in layers else None}
    prov = {"analysis": "population_in_footprints", "ops": ops, "rows_in": int(len(zones)), "rows_out": int(len(out)), "summary": summary, "zone_provenance": zone_provenance}
    primary = "footprints" if "footprints" in layers else "rings"
    return AnalysisResult(out, notes, fs, prov, layers, summary, primary)


ANALYSES = {
    "spacing": site_spacing, "best-server": best_server, "overlap": footprint_overlap, "backhaul": backhaul_topology,
    "neighbors": neighbor_relations, "drive-test": drive_test_bins, "sector-kpi": sector_kpi, "population": population_in_footprints,
}
