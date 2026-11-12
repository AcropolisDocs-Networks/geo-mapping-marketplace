#!/usr/bin/env python3
"""hexbin: H3 hexagonal aggregation (gap analysis P1-3; M11). Needs the `h3` package (4.x); without it every call raises
HexbinError naming `pip install h3`.

    aggregate(gdf_points, resolution='auto', value_field=None, agg='count', min_count=1) -> (cells, notes, provenance)
    from_polygons(gdf, resolution, value)                                                -> (cells, notes, provenance)
    to_parent(cells, resolution)                                                         -> cells at a coarser resolution
    auto_resolution(extent)  /  resolution_for_size(metres)

Cells are GeoDataFrames of H3 hexagons in EPSG:4326 with the fields `h3` (the cell id), `resolution`, `count` and, when a value
field is given, `<value>_<agg>`. H3 cells are defined on the sphere, so nothing here is measured in a projection: a cell is the
same cell on every map, which is what makes two hex maps comparable (an atlas, a before and after).

Automatic resolution from the data extent (the longer side, in km), aiming at a few hundred to a few thousand cells:

    extent (km)   resolution   average cell edge
    >= 5000            3            69 km
    2000 to 5000       4            26 km
     800 to 2000       5           9.9 km
     300 to  800       6           3.7 km
     100 to  300       7           1.4 km
      30 to  100       8           530 m
      10 to   30       9           200 m
       3 to   10      10            76 m
     under 3          11            29 m

Drive-test binning at resolution 8 (about 0.7 km2 per cell) is the usual choice for a market; 9 for a venue cluster.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_io  # noqa: E402

WGS84 = "EPSG:4326"
AGGS = ("count", "sum", "mean", "median", "max", "min")
AUTO_TABLE = ((5000, 3), (2000, 4), (800, 5), (300, 6), (100, 7), (30, 8), (10, 9), (3, 10), (0, 11))
# average edge length per resolution in km (H3 4.x, h3.average_hexagon_edge_length), kept here so a missing h3 still explains itself
EDGE_KM = (1281.256, 483.057, 182.513, 68.979, 26.072, 9.854, 3.725, 1.407, 0.531, 0.201, 0.076, 0.029, 0.011, 0.004, 0.0015, 0.0006)


class HexbinError(geo_io.GeoIOError):
    pass


def _h3():
    try:
        import h3
    except ImportError:
        raise HexbinError("hex aggregation needs the h3 package: pip install h3") from None
    if not hasattr(h3, "latlng_to_cell"):
        raise HexbinError("the installed h3 package is older than 4.0; run pip install --upgrade h3")
    return h3


def _extent_km(gdf) -> float:
    """The longer side of the data's WGS84 box, in km (a great-circle estimate; enough to pick a resolution)."""
    g = gdf if gdf.crs is None or gdf.crs.equals(WGS84) else gdf.to_crs(WGS84)
    minx, miny, maxx, maxy = [float(v) for v in g.total_bounds]
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        return 0.0
    lat = math.radians((miny + maxy) / 2)
    w = (maxx - minx) * 111.32 * math.cos(lat)
    h = (maxy - miny) * 110.57
    return max(w, h)


def auto_resolution(extent_km: float) -> int:
    for floor, res in AUTO_TABLE:
        if extent_km >= floor:
            return res
    return 11


def resolution_for_size(metres: float) -> int:
    """The resolution whose average edge is nearest the asked cell size (edge, not width)."""
    km = float(metres) / 1000.0
    return min(range(16), key=lambda r: abs(math.log(max(EDGE_KM[r], 1e-9)) - math.log(max(km, 1e-9))))


def _resolve(resolution, gdf) -> tuple[int, str]:
    if resolution in (None, "auto"):
        ext = _extent_km(gdf)
        r = auto_resolution(ext)
        return r, f"chosen from the data extent (about {ext:,.0f} km across)"
    try:
        r = int(resolution)
    except (TypeError, ValueError):
        raise HexbinError(f"resolution must be an integer 0 to 15 or 'auto', got {resolution!r}") from None
    if not 0 <= r <= 15:
        raise HexbinError("resolution must be between 0 and 15")
    return r, "as asked"


def _cells_frame(h3, table, res: int):
    import geopandas as gpd
    import shapely
    ids = list(table.index)
    polys = [shapely.Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]) for c in ids]
    data = {"h3": ids, "resolution": res}
    for c in table.columns:
        data[c] = table[c].values
    return gpd.GeoDataFrame(data, geometry=polys, crs=WGS84)


def aggregate(gdf, resolution="auto", value_field: str | None = None, agg: str = "count", min_count: int = 1):
    """Points (or the representative point of other geometry) into H3 cells. -> (cells, notes, provenance)."""
    import pandas as pd
    h3 = _h3()
    agg = (agg or "count").lower()
    if agg not in AGGS:
        raise HexbinError(f"agg must be one of: {', '.join(AGGS)}")
    if agg != "count" and (not value_field or value_field not in gdf.columns):
        raise HexbinError(f"agg '{agg}' needs a value field of the layer" + (f"; it has: {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}" if value_field else ""))
    if gdf.crs is None:
        raise HexbinError("the layer has no CRS; say which it uses first")
    res, why = _resolve(resolution, gdf)
    g = gdf if gdf.crs.equals(WGS84) else gdf.to_crs(WGS84)
    kinds = {x.geom_type.replace("Multi", "") for x in g.geometry if x is not None and not x.is_empty}
    note_pts = ""
    pts = g.geometry
    if kinds - {"Point"}:
        pts = g.geometry.representative_point()
        note_pts = f" The {', '.join(sorted(kinds)).lower()} features were binned by a point inside each."
    ok = pts.notna() & ~pts.is_empty
    cells = [h3.latlng_to_cell(float(y), float(x), res) for x, y in zip(pts[ok].x, pts[ok].y, strict=True)]
    df = pd.DataFrame({"h3": cells}, index=g.index[ok])
    if value_field:
        df[value_field] = pd.to_numeric(g.loc[ok, value_field], errors="coerce")
    grp = df.groupby("h3")
    table = grp.size().rename("count").to_frame()
    vfield = None
    if agg != "count":
        vfield = f"{value_field}_{agg}"
        table[vfield] = getattr(grp[value_field], agg)()
    table = table[table["count"] >= int(min_count)]
    out = _cells_frame(h3, table, res)
    edge = EDGE_KM[res]
    note = (f"H3 hexagons at resolution {res} ({why}; average edge about {edge:g} km, area about {2.598 * edge * edge:.3g} km2): "
            f"{int(ok.sum())} feature(s) binned into {len(out)} cell(s)" + (f" with at least {min_count} each" if int(min_count) > 1 else "")
            + (f"; `{vfield}` is the {agg} of `{value_field}` per cell" if vfield else "") + "." + note_pts
            + " Cells are fixed on the sphere, so the same cell ids compare across maps.")
    prov = {"method": "h3", "resolution": res, "resolution_basis": why, "agg": agg, "value": value_field, "cells": len(out), "rows_in": int(len(gdf)),
            "measure_crs": "H3 cells on the sphere (EPSG:4326 boundaries)"}
    return out, [note], prov


def from_polygons(gdf, resolution="auto", value: str | None = None):
    """Polygons to H3 cells with an area share: each cell gets the share of each polygon it covers (and `value` apportioned by it).

    Cells that cover any part of a polygon are included (the polyfill plus one ring), so the shares of one polygon sum to 1 and
    an apportioned value is conserved. -> (cells, notes, provenance).
    """
    import geopandas as gpd
    import pandas as pd
    import shapely
    h3 = _h3()
    if gdf.crs is None:
        raise HexbinError("the layer has no CRS; say which it uses first")
    if value and value not in gdf.columns:
        raise HexbinError(f"value field '{value}' is not in the data")
    res, why = _resolve(resolution, gdf)
    g = gdf if gdf.crs.equals(WGS84) else gdf.to_crs(WGS84)
    kinds = {x.geom_type.replace("Multi", "") for x in g.geometry if x is not None and not x.is_empty}
    if kinds - {"Polygon"}:
        raise HexbinError("from_polygons needs a polygon layer")
    # an equal-area CRS for the shares; the cells themselves stay H3
    import crs_engine as ce
    ea = ce.choose_display(g, purpose="thematic")["crs"]
    if ea == "ESRI:54030":
        ea = "EPSG:8857"
    rows = []
    for idx, geom in zip(g.index, g.geometry.values):
        if geom is None or geom.is_empty:
            continue
        ids = set()
        for part in shapely.get_parts(geom):
            try:
                ids |= set(h3.geo_to_cells(part, res))
            except Exception:
                continue
        if not ids:
            rp = geom.representative_point()
            ids = {h3.latlng_to_cell(rp.y, rp.x, res)}
        ring = set()
        for c in ids:
            ring |= set(h3.grid_disk(c, 1))
        ids = sorted(ids | ring)
        cell_polys = gpd.GeoSeries([shapely.Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]) for c in ids], crs=WGS84).to_crs(ea)
        poly = gpd.GeoSeries([geom], crs=WGS84).to_crs(ea).values[0]
        parea = poly.area
        inter = cell_polys.intersection(poly).area.values
        for c, a in zip(ids, inter, strict=True):
            if a > 0 and parea > 0:
                rows.append((c, idx, a / parea))
    df = pd.DataFrame(rows, columns=["h3", "src", "share"])
    if value:
        vals = pd.to_numeric(g[value], errors="coerce").fillna(0.0)
        df[value] = df["share"].values * vals.reindex(df["src"]).values
    grp = df.groupby("h3")
    table = grp["src"].nunique().rename("polygon_count").to_frame()
    table["share_sum"] = grp["share"].sum()
    if value:
        table[value] = grp[value].sum()
    out = _cells_frame(h3, table, res)
    note = (f"H3 hexagons at resolution {res} ({why}) over {len(g)} polygon(s): each cell carries the share of each polygon's area it covers (shares computed in {ea})"
            + (f", and `{value}` apportioned by that share, which assumes {value} is spread evenly within each polygon" if value else "") + f"; {len(out)} cell(s).")
    prov = {"method": "h3-polyfill-area-share", "resolution": res, "resolution_basis": why, "value": value, "cells": len(out), "rows_in": int(len(gdf)), "measure_crs": ea}
    return out, [note], prov


def to_parent(cells, resolution: int):
    """Cells at a coarser resolution: counts and sums add up, means and maxima are recomputed from what is there."""
    import pandas as pd
    h3 = _h3()
    res = int(resolution)
    if "h3" not in cells.columns:
        raise HexbinError("to_parent needs a cells frame with an `h3` field")
    cur = int(cells["resolution"].iloc[0]) if "resolution" in cells.columns and len(cells) else h3.get_resolution(cells["h3"].iloc[0])
    if res >= cur:
        raise HexbinError(f"the parent resolution must be coarser than {cur}")
    df = pd.DataFrame(cells.drop(columns=[cells.geometry.name, "resolution"], errors="ignore"))
    df["h3"] = [h3.cell_to_parent(c, res) for c in df["h3"]]
    how = {}
    for c in df.columns:
        if c == "h3" or not pd.api.types.is_numeric_dtype(df[c]):
            continue
        how[c] = "mean" if c.endswith("_mean") or c.endswith("_median") else ("max" if c.endswith("_max") else ("min" if c.endswith("_min") else "sum"))
    table = df.groupby("h3").agg(how) if how else df.groupby("h3").size().rename("count").to_frame()
    return _cells_frame(h3, table, res)
