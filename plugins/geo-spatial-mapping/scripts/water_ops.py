#!/usr/bin/env python3
"""water_ops: four ops that Day 26 (Water) had to write as registered steps (5.5.0, D26-13).

    depressions       {dem, min_depth_m: 0.05, min_area_m2: 25}      the closed hollows of a DEM over the layer's area (sink fill): polygons
                                                                       with max_depth_m, mean_depth_m, area_m2, volume_m3
    trace_downstream  {id_field, to_field: tohuc}                    each hydrologic unit followed downstream (WBD tohuc) to where its water
                                                                       ends: drains_to, terminal (ocean, closed basin, border, out of window), steps
    sample_along      {raster, spacing: 500 m}                       points along each line at a spacing, with dist_m and the raster's value
    sea_route         {order_field, cell_deg: auto, margin_deg: 3}    a route between the layer's points in order that stays at sea (a grid
                                                                       search on the bundled ocean), one line, its legs geodesic

Each is an op of geo_ops (OPS), so it runs from a MapSpec layer's `ops:`, from geo_analyze.py and from `gmap.py data derive`, with a record
like every other op. Nothing here fetches: a DEM or raster is a file the job already holds (the library's terrain fetch writes one).
"""
from __future__ import annotations

import heapq
import math

import numpy as np

WGS84 = "EPSG:4326"
TERMINALS = {"OCEAN": "ocean", "CLOSED BASIN": "closed basin", "CANADA": "border (Canada)", "MEXICO": "border (Mexico)"}
MAX_DEPRESSION_CELLS = 40_000_000
MAX_ROUTE_CELLS = 600_000


class WaterOpError(ValueError):
    pass


# --------------------------------------------------------------------------------------------------------------------------- depressions
def fill_sinks(z: np.ndarray, nodata_mask: np.ndarray | None = None) -> np.ndarray:
    """The DEM with every closed hollow filled to its spill height (priority flood from the edge; scikit-image's reconstruction when it is
    installed, which is the same surface faster)."""
    z = np.asarray(z, dtype="float64")
    bad = ~np.isfinite(z) if nodata_mask is None else (nodata_mask | ~np.isfinite(z))
    if (~bad).any():
        try:
            from skimage.morphology import reconstruction
            low = float(np.nanmin(z[~bad])) - 1.0
            zz = np.where(bad, low, z)
            seed = np.full_like(zz, float(zz.max()))
            seed[0, :], seed[-1, :], seed[:, 0], seed[:, -1] = zz[0, :], zz[-1, :], zz[:, 0], zz[:, -1]
            seed[bad] = low                                  # a cell with no elevation is an outlet: water leaves there
            out = reconstruction(seed, zz, method="erosion")
            out[bad] = np.nan
            return out
        except ImportError:
            pass
    h, w = z.shape
    filled = np.where(bad, np.nan, z).copy()
    done = bad.copy()
    pq: list = []
    for r in range(h):
        for c in (0, w - 1):
            if not done[r, c]:
                heapq.heappush(pq, (filled[r, c], r, c))
                done[r, c] = True
    for c in range(w):
        for r in (0, h - 1):
            if not done[r, c]:
                heapq.heappush(pq, (filled[r, c], r, c))
                done[r, c] = True
    if bad.any():                                            # beside a cell with no elevation is an edge too
        from scipy.ndimage import binary_dilation
        for r, c in zip(*np.nonzero(binary_dilation(bad, structure=np.ones((3, 3))) & ~done)):
            heapq.heappush(pq, (filled[r, c], r, c))
            done[r, c] = True
    while pq:
        v, r, c = heapq.heappop(pq)
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < h and 0 <= cc < w and not done[rr, cc]:
                done[rr, cc] = True
                if filled[rr, cc] < v:
                    filled[rr, cc] = v
                heapq.heappush(pq, (filled[rr, cc], rr, cc))
    return filled


def depressions(dem_path: str, area_wgs84, min_depth_m: float = 0.05, min_area_m2: float = 25.0):
    """-> (GeoDataFrame of hollows in the DEM's CRS, cell area m2, notes)."""
    import geopandas as gpd
    import rasterio
    from rasterio.features import geometry_mask, shapes
    from rasterio.windows import from_bounds
    from scipy import ndimage
    from shapely.geometry import shape
    with rasterio.open(dem_path) as src:
        area = gpd.GeoSeries([area_wgs84], crs=WGS84).to_crs(src.crs).iloc[0]
        win = from_bounds(*area.bounds, transform=src.transform).round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        if win.width * win.height > MAX_DEPRESSION_CELLS:
            raise WaterOpError(f"depressions: the area holds {int(win.width * win.height):,} cells of the DEM; at most {MAX_DEPRESSION_CELLS:,} (a smaller area or a coarser DEM)")
        z = src.read(1, window=win, masked=True).astype("float64")
        tr = src.window_transform(win)
        crs = src.crs
    arr = np.ma.filled(z, np.nan)
    inside = geometry_mask([area], out_shape=arr.shape, transform=tr, invert=True)
    filled = fill_sinks(arr)
    depth = np.where(np.isfinite(arr) & inside, filled - arr, 0.0)
    wet = depth >= float(min_depth_m)
    lab, n = ndimage.label(wet, structure=np.ones((3, 3)))
    cell_m2 = abs(tr.a * tr.e)
    if crs is not None and crs.is_geographic:
        lat = math.radians((area.bounds[1] + area.bounds[3]) / 2)
        cell_m2 = abs(tr.a) * 111_320 * math.cos(lat) * abs(tr.e) * 110_574
    if n == 0:
        return gpd.GeoDataFrame({"max_depth_m": [], "mean_depth_m": [], "area_m2": [], "volume_m3": []}, geometry=[], crs=crs), cell_m2, ["no hollow as deep as asked"]
    idx = np.arange(1, n + 1)
    cnt = ndimage.sum(np.ones_like(depth), lab, idx)
    mx = ndimage.maximum(depth, lab, idx)
    sm = ndimage.sum(depth, lab, idx)
    keep = {int(i) for i, c in zip(idx, cnt) if c * cell_m2 >= float(min_area_m2)}
    stats = {int(i): (float(m), float(s / c), float(c * cell_m2), float(s * cell_m2)) for i, c, m, s in zip(idx, cnt, mx, sm)}
    geoms, rows = [], []
    for g, v in shapes(lab.astype("int32"), mask=lab > 0, transform=tr):
        v = int(v)
        if v not in keep:
            continue
        m, mean, a, vol = stats[v]
        geoms.append(shape(g))
        rows.append({"hollow": v, "max_depth_m": round(m, 3), "mean_depth_m": round(mean, 3), "area_m2": round(a, 1), "volume_m3": round(vol, 1)})
    out = gpd.GeoDataFrame(rows, geometry=geoms, crs=crs).dissolve(by="hollow", aggfunc="first").reset_index()
    return out, cell_m2, [f"{len(out):,} hollow(s) at least {min_depth_m:g} m deep and {min_area_m2:g} m² across, from a {math.sqrt(cell_m2):.1f} m grid"]


# --------------------------------------------------------------------------------------------------------------------------- downstream
def _id_field(gdf, asked: str | None) -> str:
    if asked:
        if asked not in gdf.columns:
            raise WaterOpError(f"trace_downstream: the layer has no field '{asked}'")
        return asked
    for c in ("huc12", "huc10", "huc8", "huc6", "huc4", "huc2", "HUC12", "HUC10", "HUC8", "HUC6", "HUC4", "HUC2", "huc", "HUC"):
        if c in gdf.columns:
            return c
    raise WaterOpError("trace_downstream: give id_field (the unit's code, e.g. huc12)")


def trace_downstream(gdf, id_field: str | None = None, to_field: str = "tohuc", max_steps: int = 5000):
    """Each unit followed along to_field until a terminal code (OCEAN, CLOSED BASIN, a border) or a code not in the layer. -> gdf with
    drains_to (the terminal code, or the last unit in the layer), terminal (its kind) and steps."""
    idf = _id_field(gdf, id_field)
    tf = to_field if to_field in gdf.columns else next((c for c in gdf.columns if c.lower() == str(to_field).lower()), None)
    if tf is None:
        raise WaterOpError(f"trace_downstream: the layer has no field '{to_field}' (the WBD's downstream unit)")
    ids = gdf[idf].astype(str).str.strip().tolist()
    nxt = dict(zip(ids, gdf[tf].astype(str).str.strip()))
    memo: dict = {}

    def follow(u):
        path = []
        cur = u
        for _ in range(max_steps):
            if cur in memo:
                end, kind, k = memo[cur]
                res = (end, kind, k + len(path))
                break
            path.append(cur)
            to = nxt.get(cur, "")
            up = to.upper()
            if up in TERMINALS or up.startswith("CLOSED"):
                res = (up, TERMINALS.get(up, "closed basin"), len(path))
                break
            if not to or up in ("NONE", "NAN") or to not in nxt:
                res = (to or cur, "out of window" if to and to not in nxt else "unknown", len(path))
                break
            if to in path:
                res = (cur, "loop", len(path))
                break
            cur = to
        else:
            res = (cur, "too long", len(path))
        for j, p in enumerate(path):
            memo[p] = (res[0], res[1], res[2] - j)
        return res
    out = gdf.copy()
    r = [follow(u) for u in ids]
    out["drains_to"] = [x[0] for x in r]
    out["terminal"] = [x[1] for x in r]
    out["steps"] = [x[2] for x in r]
    return out


# --------------------------------------------------------------------------------------------------------------------------- sample along
def sample_along(gdf, raster: str, spacing_m: float, measure_crs: str):
    """Points every spacing_m along each line (in the measure CRS), with line (row), dist_m and value (the raster's band 1)."""
    import geopandas as gpd
    import rasterio
    from pyproj import Transformer
    g = gdf.to_crs(measure_crs)
    pts, rows = [], []
    for i, geom in enumerate(g.geometry):
        if geom is None or geom.is_empty:
            continue
        parts = list(getattr(geom, "geoms", [geom]))
        base = 0.0
        for part in parts:
            L = part.length
            n = max(2, int(math.floor(L / max(spacing_m, 1e-6))) + 1)
            for d in np.linspace(0.0, L, n):
                pts.append(part.interpolate(d))
                rows.append({"line": i, "dist_m": round(base + float(d), 2)})
            base += L
    if not pts:
        raise WaterOpError("sample_along: the layer has no line to sample")
    out = gpd.GeoDataFrame(rows, geometry=pts, crs=measure_crs)
    with rasterio.open(raster) as src:
        xs, ys = np.array([p.x for p in pts]), np.array([p.y for p in pts])
        if src.crs is not None and not _same_crs(src.crs, measure_crs):
            xs, ys = Transformer.from_crs(measure_crs, src.crs, always_xy=True).transform(xs, ys)
        vals = np.array([v[0] for v in src.sample(zip(xs, ys))], dtype="float64")
        if src.nodata is not None:
            vals[vals == src.nodata] = np.nan
    out["value"] = vals
    return out


def _same_crs(a, b) -> bool:
    from pyproj import CRS
    try:
        return CRS.from_user_input(a.to_wkt() if hasattr(a, "to_wkt") else a).equals(CRS.from_user_input(b.to_wkt() if hasattr(b, "to_wkt") else b))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------------------------------------- sea route
def _ocean_union(box):
    import geopandas as gpd
    import gmap_common as gc
    import shapely
    p = gc.plugin_root() / "assets" / "basemaps" / "ne_10m_ocean.parquet"
    oc = gpd.read_parquet(p)
    oc = oc.to_crs(WGS84) if oc.crs is not None and not oc.crs.equals(WGS84) else oc
    return shapely.union_all(shapely.clip_by_rect(oc.geometry.values, *box))


def sea_route(points_lonlat: list[tuple[float, float]], cell_deg: float | None = None, margin_deg: float = 3.0):
    """A route through the points in order that stays on the bundled ocean (8-neighbour A* on a lon/lat grid, the cost the great-circle
    distance), each leg smoothed and drawn as geodesic segments. -> (list of lon/lat vertices, notes)."""
    import shapely
    from pyproj import Geod
    if len(points_lonlat) < 2:
        raise WaterOpError("sea_route needs at least two points")
    xs = [p[0] for p in points_lonlat]
    ys = [p[1] for p in points_lonlat]
    box = (max(-180.0, min(xs) - margin_deg), max(-85.0, min(ys) - margin_deg), min(180.0, max(xs) + margin_deg), min(85.0, max(ys) + margin_deg))
    span = max(box[2] - box[0], box[3] - box[1])
    cell = float(cell_deg) if cell_deg else max(0.02, span / 360.0)
    nx, ny = int(math.ceil((box[2] - box[0]) / cell)) + 1, int(math.ceil((box[3] - box[1]) / cell)) + 1
    if nx * ny > MAX_ROUTE_CELLS:
        cell = math.sqrt((box[2] - box[0]) * (box[3] - box[1]) / MAX_ROUTE_CELLS)
        nx, ny = int(math.ceil((box[2] - box[0]) / cell)) + 1, int(math.ceil((box[3] - box[1]) / cell)) + 1
    sea = _ocean_union(box)
    gx = box[0] + np.arange(nx) * cell
    gy = box[1] + np.arange(ny) * cell
    X, Y = np.meshgrid(gx, gy)
    water = shapely.contains_xy(sea, X.ravel(), Y.ravel()).reshape(ny, nx)
    geod = Geod(ellps="WGS84")

    def snap(lon, lat):
        c, r = int(round((lon - box[0]) / cell)), int(round((lat - box[1]) / cell))
        best, bd = None, 1e18
        for rad in range(0, 60):
            for rr in range(max(0, r - rad), min(ny, r + rad + 1)):
                for cc in range(max(0, c - rad), min(nx, c + rad + 1)):
                    if water[rr, cc]:
                        d = (rr - r) ** 2 + (cc - c) ** 2
                        if d < bd:
                            best, bd = (rr, cc), d
            if best is not None:
                return best
        raise WaterOpError(f"sea_route: no sea near {lon:.2f}, {lat:.2f}")

    def astar(a, b):
        def hcost(r, c):
            return geod.inv(gx[c], gy[r], gx[b[1]], gy[b[0]])[2]
        openq = [(hcost(*a), 0.0, a)]
        came, gs = {a: None}, {a: 0.0}
        while openq:
            _f, g0, cur = heapq.heappop(openq)
            if cur == b:
                break
            if g0 > gs.get(cur, 1e30):
                continue
            r, c = cur
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < ny and 0 <= cc < nx) or not water[rr, cc]:
                    continue
                step = geod.inv(gx[c], gy[r], gx[cc], gy[rr])[2]
                ng = g0 + step
                if ng < gs.get((rr, cc), 1e30):
                    gs[(rr, cc)] = ng
                    came[(rr, cc)] = cur
                    heapq.heappush(openq, (ng + hcost(rr, cc), ng, (rr, cc)))
        if b not in came:
            raise WaterOpError("sea_route: no way by sea between two of the points inside the search box (a larger margin_deg)")
        path, cur = [], b
        while cur is not None:
            path.append(cur)
            cur = came[cur]
        return path[::-1]

    verts = [tuple(points_lonlat[0])]
    total = 0.0
    for p0, p1 in zip(points_lonlat[:-1], points_lonlat[1:]):
        cells = astar(snap(*p0), snap(*p1))
        line = shapely.LineString([(gx[c], gy[r]) for r, c in cells]) if len(cells) > 1 else None
        if line is not None:
            # the grid's stair steps taken out, but never across land: simplified only as far as the line stays at sea
            for tol in (cell * 3, cell * 1.5, cell * 0.75, 0.0):
                s = line.simplify(tol) if tol else line
                if tol == 0 or sea.buffer(cell * 0.5).contains(s):
                    line = s
                    break
            verts += [tuple(v) for v in list(line.coords)[1:-1]]
        verts.append(tuple(p1))
    for a, b in zip(verts[:-1], verts[1:]):
        total += geod.inv(a[0], a[1], b[0], b[1])[2]
    return verts, [f"a route at sea through {len(points_lonlat)} point(s), {total / 1000:,.0f} km, searched on a {cell:.2f}° grid of the bundled ocean"
                   " (Natural Earth 1:10m); the true track is not known, this is the shortest way at sea between the points"]


def densify_geodesic(verts, step_km: float = 50.0):
    """Every leg as points along its geodesic, so the line is drawn as the shortest path on the ground in any projection."""
    from pyproj import Geod
    g = Geod(ellps="WGS84")
    out = [tuple(verts[0])]
    for a, b in zip(verts[:-1], verts[1:]):
        d = g.inv(a[0], a[1], b[0], b[1])[2]
        n = int(d / (step_km * 1000))
        if n > 0:
            out += [tuple(p) for p in g.npts(a[0], a[1], b[0], b[1], n)]
        out.append(tuple(b))
    return out
