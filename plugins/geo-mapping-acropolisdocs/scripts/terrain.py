#!/usr/bin/env python3
"""terrain: elevation profiles, line of sight, viewsheds, hillshade and contours from a DEM (gap analysis P1-4; M12).

    terrain.py fetch     --bounds lon0,lat0,lon1,lat1 --res 30 [--source auto|3dep|terrarium] --out dem.tif [--cache DIR]
    terrain.py profile   --dem dem.tif --a lon,lat --b lon,lat [--samples 200] [--out profile.json] [--csv profile.csv]
    terrain.py los       --dem dem.tif --a lon,lat --b lon,lat --ha 30 --hb 30 [--k 1.333] [--freq 11] [--out los.json]
    terrain.py viewshed  --dem dem.tif --point lon,lat --h 30 --radius 10000 [--step 1] [--target-h 1.5] --out visible.geojson
    terrain.py hillshade --dem dem.tif --out hill.tif|hill.png [--azimuth 315] [--altitude 45] [--z 1] [--multi]
    terrain.py contours  --dem dem.tif --interval 50 --out contours.gpkg [--index 5] [--bounds lon0,lat0,lon1,lat1]
    terrain.py profile-chart --dem dem.tif --a lon,lat --b lon,lat --ha 30 --hb 30 [--freq 11] --out DIR   (a brand chart; profile_chart.py, M15)

Every command prints a JSON report; `los` prints the verdict first ("CLEAR: ..." or "OBSTRUCTED: ..."). Exit 0 done; 1 a runtime
failure; 2 a usage error; 4 a refusal (3dep asked for outside the US); 5 a service failure.

What the numbers are, and are not. A DEM is bare earth: 3DEP is 10 m (1/3 arc-second) over most of the lower 48 and 1 m where lidar
exists, the Terrain Tiles 30 m (SRTM) or coarser outside the US. Buildings, trees and clutter are not in it, so a clear line of sight
here is optimistic: it says the ground does not block the path, not that nothing does. Earth curvature is corrected with an effective
radius of k x 6,371 km (k = 4/3, the standard-atmosphere refraction; k = 1 is the geometric earth). The Fresnel test is the first zone
at the worst point: a link is "Fresnel ok" when at least 60 percent of F1 is clear everywhere, F1 = 17.32 x sqrt(d1 x d2 / (f x D)) metres
with d1, d2 and D in km and f in GHz. The viewshed is ray marching at one-degree steps on the DEM's own cells: approximate, and it
says so in its record.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import raster_io as rio  # noqa: E402

R_EARTH_M = 6_371_000.0
K_DEFAULT = 4.0 / 3.0
FRESNEL_CLEARANCE = 0.6
MAX_ANTENNA_M = 1000.0
WGS84 = "EPSG:4326"


class TerrainError(rio.RasterError):
    pass


def _geod():
    from pyproj import Geod
    return Geod(ellps="WGS84")


def _lonlat(v, what: str = "point") -> tuple[float, float]:
    try:
        if isinstance(v, str):
            lon, lat = (float(x) for x in v.replace(";", ",").split(","))
        else:
            lon, lat = float(v[0]), float(v[1])
    except (TypeError, ValueError):
        raise TerrainError(f"{what} must be lon,lat") from None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise TerrainError(f"{what} {lon},{lat} is not a lon/lat pair (longitude first)")
    return lon, lat


def dem_resolution_m(dem_path) -> float:
    info = rio.open_info(dem_path)
    from rasterio.transform import Affine
    t = Affine(*info["transform"])
    lat = rio.centre_lat(t, (info["size"][1], info["size"][0]), info["crs"])
    dx, dy = rio.cell_size_m(t, info["crs"], lat)
    return float(min(dx, dy))


# --------------------------------------------------------------------------------------------------------------- profile

def geodesic_points(a, b, samples: int):
    """`samples` points from a to b along the geodesic (both ends included) and the distances along it, in metres."""
    g = _geod()
    lon0, lat0 = a
    lon1, lat1 = b
    _, _, D = g.inv(lon0, lat0, lon1, lat1)
    n = max(2, int(samples))
    mids = g.npts(lon0, lat0, lon1, lat1, n - 2) if n > 2 else []
    pts = [(lon0, lat0)] + [(p[0], p[1]) for p in mids] + [(lon1, lat1)]
    d = np.linspace(0.0, D, n)
    return pts, d, float(D)


def profile(dem_path, a_lonlat, b_lonlat, samples: int = 200) -> dict:
    """Elevation along the geodesic from A to B. -> {points: [(distance_m, elevation_m)], length_m, min_m, max_m, gain_m, loss_m, ...}.
    Samples are bilinear from the DEM's own grid; a sample over nodata is nan and counted."""
    a, b = _lonlat(a_lonlat, "a"), _lonlat(b_lonlat, "b")
    pts, d, D = geodesic_points(a, b, samples)
    z = rio.sample(dem_path, pts, WGS84)
    ok = np.isfinite(z)
    zz = z[ok]
    diffs = np.diff(zz) if zz.size > 1 else np.array([])
    return {"a": list(a), "b": list(b), "length_m": D, "samples": int(len(pts)), "nodata_samples": int((~ok).sum()), "spacing_m": float(D / (len(pts) - 1)),
            "points": [(float(x), (float(y) if np.isfinite(y) else None)) for x, y in zip(d, z, strict=True)],
            "min_m": float(zz.min()) if zz.size else None, "max_m": float(zz.max()) if zz.size else None,
            "start_m": float(z[0]) if np.isfinite(z[0]) else None, "end_m": float(z[-1]) if np.isfinite(z[-1]) else None,
            "gain_m": float(diffs[diffs > 0].sum()) if diffs.size else 0.0, "loss_m": float(-diffs[diffs < 0].sum()) if diffs.size else 0.0,
            "dem": str(dem_path), "dem_resolution_m": dem_resolution_m(dem_path), "method": "bilinear samples along the WGS84 geodesic"}


# --------------------------------------------------------------------------------------------------------------- line of sight

def fresnel_radius_m(d1_m: float, d2_m: float, freq_ghz: float) -> float:
    """First Fresnel zone radius at a point d1 from one end and d2 from the other: 17.32 * sqrt(d1 * d2 / (f * D)) with km and GHz."""
    D = d1_m + d2_m
    if D <= 0 or freq_ghz <= 0:
        return 0.0
    return 17.32 * math.sqrt((d1_m / 1000.0) * (d2_m / 1000.0) / (freq_ghz * (D / 1000.0)))


def curvature_drop_m(d_m, D_m: float, k_factor: float = K_DEFAULT):
    """The earth bulge at distance d along a path of length D: how far the effective earth's surface rises above the straight chord between the
    path's two ends (d1 x d2 / (2 k R)). The name is kept from 0.5.0; it is a rise of the ground relative to the ray, not a drop."""
    return d_m * (D_m - d_m) / (2.0 * k_factor * R_EARTH_M)


def _los_eval(d, z, D, h_a, h_b, k_factor, freq_ghz):
    """Clearance of the ray over the curvature-corrected terrain at every sample. -> dict of arrays and the worst point.
    The terrain is raised by the earth bulge (the chord between two points on a sphere passes under its surface), so curvature always
    takes clearance away. 0.5.0 subtracted the bulge and so added clearance (fixed in 0.5.1: a 50 km sea path with 30 m masts at
    k = 4/3 is beyond the radio horizon, about 45 km, and is obstructed)."""
    z_eff = z + curvature_drop_m(d, D, k_factor)              # terrain raised by the bulge; the ray stays a straight chord
    za, zb = z[0] + h_a, z[-1] + h_b
    ray = za + (zb - za) * (d / D if D > 0 else 0.0)
    clear = ray - z_eff
    inner = slice(1, -1) if len(d) > 2 else slice(0, len(d))
    f1 = np.array([fresnel_radius_m(x, D - x, freq_ghz) for x in d]) if freq_ghz else None
    if f1 is not None:
        ratio = np.where(f1 > 0, clear / np.where(f1 > 0, f1, 1.0), np.inf)
        worst_i = int(np.argmin(ratio[inner])) + (1 if len(d) > 2 else 0)
    else:
        worst_i = int(np.argmin(clear[inner])) + (1 if len(d) > 2 else 0)
    obstructed = bool(np.any(clear[inner] < 0))
    return {"z_eff": z_eff, "ray": ray, "clear": clear, "f1": f1, "worst_i": worst_i, "obstructed": obstructed,
            "min_clear": float(clear[inner].min()), "min_ratio": float((clear[inner] / f1[inner]).min()) if f1 is not None else None}


def _required_height(d, z, D, h_b, k_factor, freq_ghz, want_ratio: float, h_min: float) -> float | None:
    """Smallest antenna height at A (binary search, 0.1 m) that gives every sample at least `want_ratio` of F1 clearance (or > 0 clearance)."""
    def ok(h):
        e = _los_eval(d, z, D, h, h_b, k_factor, freq_ghz)
        return (e["min_ratio"] >= want_ratio) if (freq_ghz and want_ratio > 0) else (e["min_clear"] >= 0)
    if ok(h_min):
        return float(h_min)
    lo, hi = h_min, MAX_ANTENNA_M
    if not ok(hi):
        return None
    while hi - lo > 0.1:
        mid = (lo + hi) / 2.0
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return float(math.ceil(hi * 10) / 10)


def line_of_sight(dem_path, a_lonlat, b_lonlat, h_a_m: float = 0.0, h_b_m: float = 0.0, k_factor: float = K_DEFAULT, freq_ghz: float | None = None,
                  samples: int | None = None) -> dict:
    """Is the path from A (antenna h_a above ground) to B (h_b) clear of the terrain? -> verdict with the worst point, the required height at A,
    and the Fresnel figures when a frequency is given. Sampling is one point per DEM cell along the path (at least 50, at most 5,000)."""
    a, b = _lonlat(a_lonlat, "a"), _lonlat(b_lonlat, "b")
    res = dem_resolution_m(dem_path)
    g = _geod()
    _, _, D = g.inv(a[0], a[1], b[0], b[1])
    if D <= 0:
        raise TerrainError("A and B are the same point")
    n = int(samples) if samples else int(min(5000, max(50, math.ceil(D / max(res, 1.0)) + 1)))
    pts, d, D = geodesic_points(a, b, n)
    z = rio.sample(dem_path, pts, WGS84)
    if not np.isfinite(z[0]) or not np.isfinite(z[-1]):
        raise TerrainError("the DEM has no elevation at A or B (outside its extent, or nodata)")
    nd = int((~np.isfinite(z)).sum())
    if nd:
        z = np.interp(d, d[np.isfinite(z)], z[np.isfinite(z)])        # gaps are bridged, and said
    e = _los_eval(d, z, D, h_a_m, h_b_m, k_factor, freq_ghz)
    i = e["worst_i"]
    out = {"a": list(a), "b": list(b), "length_m": float(D), "h_a_m": float(h_a_m), "h_b_m": float(h_b_m), "k_factor": float(k_factor), "freq_ghz": freq_ghz,
           "clear": not e["obstructed"], "verdict": "obstructed" if e["obstructed"] else "clear",
           "worst": {"distance_m": float(d[i]), "terrain_m": float(z[i]), "terrain_effective_m": float(e["z_eff"][i]), "ray_m": float(e["ray"][i]),
                     "clearance_m": float(e["clear"][i]), "lon": pts[i][0], "lat": pts[i][1]},
           "obstruction_distance_m": float(d[i]) if e["obstructed"] else None, "obstruction_height_m": float(-e["min_clear"]) if e["obstructed"] else 0.0,
           "min_clearance_m": e["min_clear"], "curvature_bulge_mid_m": float(curvature_drop_m(D / 2.0, D, k_factor)),
           "samples": n, "spacing_m": float(D / (n - 1)), "nodata_samples_bridged": nd, "dem": str(dem_path), "dem_resolution_m": res,
           "elevation_a_m": float(z[0]), "elevation_b_m": float(z[-1]),
           "required_h_a_m": _required_height(d, z, D, h_b_m, k_factor, None, 0.0, h_a_m)}
    if freq_ghz:
        f1w = float(e["f1"][i])
        out.update({"fresnel_f1_worst_m": f1w, "fresnel_clearance_ratio": e["min_ratio"], "fresnel_ok": bool(e["min_ratio"] >= FRESNEL_CLEARANCE),
                    "fresnel_rule": f"at least {FRESNEL_CLEARANCE:.0%} of the first Fresnel zone clear at every point",
                    "fresnel_f1_mid_m": fresnel_radius_m(D / 2.0, D / 2.0, freq_ghz),
                    "required_h_a_fresnel_m": _required_height(d, z, D, h_b_m, k_factor, freq_ghz, FRESNEL_CLEARANCE, h_a_m)})
    out["profile"] = [(float(x), float(y), float(c)) for x, y, c in zip(d, z, e["clear"], strict=True)]
    out["caveat"] = ("bare-earth DEM: buildings, trees and other clutter are not in it, so a clear verdict is optimistic; "
                     f"the DEM is about {res:.0f} m per cell, so a ridge narrower than that can be missed")
    return out


def verdict_line(r: dict) -> str:
    km = r["length_m"] / 1000.0
    if r["clear"]:
        s = f"CLEAR: {km:.2f} km, minimum clearance {r['min_clearance_m']:.1f} m above the terrain at {r['worst']['distance_m'] / 1000:.2f} km (k = {r['k_factor']:.3g})"
    else:
        s = (f"OBSTRUCTED: terrain at {r['worst']['distance_m'] / 1000:.2f} km of {km:.2f} km rises {r['obstruction_height_m']:.1f} m above the ray; "
             f"{'an antenna of ' + format(r['required_h_a_m'], '.1f') + ' m at A clears it' if r['required_h_a_m'] is not None else 'no antenna height up to 1,000 m at A clears it'}"
             f" (k = {r['k_factor']:.3g})")
    if r.get("freq_ghz"):
        if r["clear"]:
            s += (f"; Fresnel {'ok' if r['fresnel_ok'] else 'NOT ok'} at {r['freq_ghz']:g} GHz: {r['fresnel_clearance_ratio']:.0%} of F1 "
                  f"({r['fresnel_f1_worst_m']:.1f} m at the worst point) clear, 60% needed")
        else:
            s += f"; Fresnel NOT ok at {r['freq_ghz']:g} GHz (the path itself is obstructed; F1 is {r['fresnel_f1_worst_m']:.1f} m at the worst point)"
        if r.get("required_h_a_fresnel_m") is not None and not r["fresnel_ok"]:
            s += f"; {r['required_h_a_fresnel_m']:.1f} m at A gives 60%"
    return s + ". Bare earth only: clutter is not modelled."


def los_matrix(dem_path, sites, links, heights=None, *, id_field: str | None = None, k_factor: float = K_DEFAULT, freq_ghz: float | None = None,
               default_height_m: float = 30.0):
    """Line of sight for every (a_id, b_id) link between sites. `sites` is a GeoDataFrame of points (any CRS); `heights` a number for every site or
    {id: metres}. -> GeoDataFrame of lines (EPSG:4326) with a, b, length_m, clear, obstruction_m, obstruction_distance_m, fresnel_ok,
    required_h_a_m."""
    import geopandas as gpd
    from shapely.geometry import LineString
    s = sites.to_crs(WGS84) if sites.crs and str(sites.crs) != WGS84 else sites
    if s.crs is None:
        raise TerrainError("the sites layer has no CRS")
    idf = id_field or next((c for c in s.columns if c.lower() in ("site_id", "id", "site", "name")), None)
    if not idf or idf not in s.columns:
        raise TerrainError("say which field holds the site id (id_field)")
    pos = {str(r[idf]): (geom.x, geom.y) for r, geom in zip(s.to_dict("records"), s.geometry) if geom is not None and not geom.is_empty}
    rows, missing = [], []
    for a_id, b_id in links:
        a_id, b_id = str(a_id), str(b_id)
        if a_id not in pos or b_id not in pos:
            missing.append((a_id, b_id))
            continue
        ha = float(heights.get(a_id, default_height_m)) if isinstance(heights, dict) else float(heights if heights is not None else default_height_m)
        hb = float(heights.get(b_id, default_height_m)) if isinstance(heights, dict) else float(heights if heights is not None else default_height_m)
        r = line_of_sight(dem_path, pos[a_id], pos[b_id], ha, hb, k_factor, freq_ghz)
        rows.append({"a": a_id, "b": b_id, "length_m": r["length_m"], "h_a_m": ha, "h_b_m": hb, "clear": r["clear"], "obstruction_m": r["obstruction_height_m"],
                     "obstruction_distance_m": r["obstruction_distance_m"], "min_clearance_m": r["min_clearance_m"], "fresnel_ok": r.get("fresnel_ok"),
                     "fresnel_ratio": r.get("fresnel_clearance_ratio"), "required_h_a_m": r["required_h_a_m"], "geometry": LineString([pos[a_id], pos[b_id]])})
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84) if rows else gpd.GeoDataFrame({"a": [], "b": []}, geometry=[], crs=WGS84)
    out.attrs["missing_links"] = missing
    out.attrs["k_factor"], out.attrs["freq_ghz"] = k_factor, freq_ghz
    return out


# --------------------------------------------------------------------------------------------------------------- viewshed

def viewshed(dem_path, point_lonlat, h_m: float = 10.0, radius_m: float = 5000.0, step_deg: float = 1.0, *, target_h_m: float = 0.0, k_factor: float = K_DEFAULT,
             max_px: int = 1024) -> dict:
    """The ground visible from a point `h_m` above the terrain within `radius_m`, by ray marching every `step_deg` degrees on an azimuthal
    equidistant grid centred on the point (one sample per DEM cell, or coarser so the grid stays within max_px a side). -> {geometry (EPSG:4326
    MultiPolygon), visible_share, ...}. Approximate: the rays are `step_deg` apart, so at the edge of the radius a strip of
    radius x step_deg x pi / 180 metres lies between them and is filled in."""
    import geopandas as gpd
    from shapely.geometry import Polygon, Point
    from shapely.ops import unary_union
    from pyproj import Transformer
    lon, lat = _lonlat(point_lonlat, "point")
    if radius_m <= 0:
        raise TerrainError("radius must be positive")
    aeqd = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    res = dem_resolution_m(dem_path)
    side = int(min(max_px, max(32, math.ceil(2 * radius_m / max(res, 1.0)))))
    g = rio.read_window(dem_path, (-radius_m, -radius_m, radius_m, radius_m), aeqd, out_shape=(side, side), resampling="bilinear")
    z = np.ma.filled(np.ma.asarray(g.array, dtype=float), np.nan)
    cell = 2 * radius_m / side
    inv = ~g.transform
    cx, cy = inv @ (0.0, 0.0)
    z0 = rio.bilinear(z, np.array([cx - 0.5]), np.array([cy - 0.5]))[0]
    if not np.isfinite(z0):
        raise TerrainError("the DEM has no elevation at the point")
    eye = z0 + h_m
    nstep = max(2, int(math.ceil(radius_m / cell)))
    dist = np.linspace(cell, radius_m, nstep)
    drop = dist * dist / (2.0 * k_factor * R_EARTH_M)          # how far the effective earth falls below the eye's horizontal at each distance
    sectors = []
    half = math.radians(step_deg) / 2.0
    n_rays = int(round(360.0 / step_deg))
    vis_len = 0.0
    for i in range(n_rays):
        az = math.radians(i * step_deg)
        xs, ys = dist * math.sin(az), dist * math.cos(az)
        cols, rows = inv @ (xs, ys)
        zs = rio.bilinear(z, np.asarray(cols) - 0.5, np.asarray(rows) - 0.5)
        zs = np.where(np.isfinite(zs), zs, -1e9)
        ang = (zs + target_h_m - drop - eye) / dist                 # tangent of the elevation angle to each sample
        hmax = np.maximum.accumulate(np.concatenate([[-np.inf], ang[:-1]]))
        visible = ang >= hmax
        visible &= zs > -1e8
        # runs of visible samples become annular sectors
        j = 0
        while j < nstep:
            if visible[j]:
                k = j
                while k + 1 < nstep and visible[k + 1]:
                    k += 1
                r0, r1 = max(0.0, dist[j] - cell / 2.0), dist[k] + cell / 2.0
                vis_len += r1 - r0
                a0, a1 = az - half, az + half
                ring = [(r0 * math.sin(a0), r0 * math.cos(a0)), (r1 * math.sin(a0), r1 * math.cos(a0)), (r1 * math.sin(az), r1 * math.cos(az)),
                        (r1 * math.sin(a1), r1 * math.cos(a1)), (r0 * math.sin(a1), r0 * math.cos(a1))]
                if r0 < 1e-9:
                    ring = [(0.0, 0.0)] + ring[1:4]
                sectors.append(Polygon(ring))
                j = k + 1
            else:
                j += 1
    geom = unary_union(sectors).buffer(cell * 0.5).buffer(-cell * 0.5) if sectors else Polygon()
    geom = geom.intersection(Point(0, 0).buffer(radius_m, 64))
    tr = Transformer.from_crs(aeqd, WGS84, always_xy=True)
    import shapely
    geom_ll = shapely.transform(geom, lambda c: np.column_stack(tr.transform(c[:, 0], c[:, 1])))
    share = float(vis_len / (n_rays * radius_m)) if n_rays else 0.0
    return {"point": [lon, lat], "h_m": float(h_m), "target_h_m": float(target_h_m), "radius_m": float(radius_m), "step_deg": float(step_deg), "k_factor": float(k_factor),
            "elevation_m": float(z0), "eye_m": float(eye), "rays": n_rays, "samples_per_ray": nstep, "cell_m": float(cell), "dem": str(dem_path), "dem_resolution_m": res,
            "visible_share_of_rays": share, "visible_area_km2": float(geom.area / 1e6), "geometry": geom_ll, "crs": WGS84,
            "method": f"ray marching every {step_deg:g} degrees on an azimuthal equidistant grid of {cell:.0f} m cells; approximate, bare earth, k = {k_factor:.3g}",
            "gdf": gpd.GeoDataFrame({"h_m": [h_m], "radius_m": [radius_m]}, geometry=[geom_ll], crs=WGS84)}


# --------------------------------------------------------------------------------------------------------------- CLI

def _parse_bounds(s):
    try:
        return tuple(float(v) for v in s.replace(";", ",").split(","))
    except (AttributeError, ValueError):
        raise TerrainError("bounds must be lon0,lat0,lon1,lat1") from None


def _dem_window(dem, bounds):
    info = rio.open_info(dem)
    if bounds:
        from pyproj import Transformer
        tr = Transformer.from_crs(WGS84, info["crs"], always_xy=True)
        xs, ys = tr.transform([bounds[0], bounds[2], bounds[0], bounds[2]], [bounds[1], bounds[1], bounds[3], bounds[3]])
        b = (min(xs), min(ys), max(xs), max(ys))
    else:
        b = tuple(info["bounds"])
    return rio.read_window(dem, b, info["crs"]), info


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["profile-chart"]:                   # the brand profile chart for one link (profile_chart.py, M15)
        import profile_chart
        return profile_chart.main(args[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["fetch", "profile", "los", "viewshed", "hillshade", "contours"])
    ap.add_argument("--dem")
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--point")
    ap.add_argument("--ha", type=float, default=0.0)
    ap.add_argument("--hb", type=float, default=0.0)
    ap.add_argument("--h", type=float, default=10.0)
    ap.add_argument("--target-h", type=float, default=0.0)
    ap.add_argument("--k", type=float, default=K_DEFAULT)
    ap.add_argument("--freq", type=float)
    ap.add_argument("--samples", type=int)
    ap.add_argument("--radius", type=float, default=5000.0)
    ap.add_argument("--step", type=float, default=1.0)
    ap.add_argument("--bounds")
    ap.add_argument("--res", type=float, default=30.0)
    ap.add_argument("--source", default="auto", choices=["auto", "3dep", "terrarium", "copernicus"])
    ap.add_argument("--terms-acknowledged", action="store_true", help=argparse.SUPPRESS)    # older callers; the Terrarium credit is printed for you
    ap.add_argument("--cache")
    ap.add_argument("--azimuth", type=float, default=315.0)
    ap.add_argument("--altitude", type=float, default=45.0)
    ap.add_argument("--z", type=float, default=1.0)
    ap.add_argument("--multi", action="store_true")
    ap.add_argument("--interval", type=float)
    ap.add_argument("--index", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--csv")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):                      # --a -112.07,33.41: a western longitude looks like an option to argparse; join it
        if argv[i] in ("--a", "--b", "--point", "--bounds") and argv[i + 1].startswith("-"):
            argv[i], argv[i + 1] = argv[i] + "=" + argv[i + 1], "--=joined"
    argv = [t for t in argv if t != "--=joined"]
    a = ap.parse_args(argv)
    try:
        if a.op == "fetch":
            import terrain_fetch as tf
            if not a.bounds or not a.out:
                raise TerrainError("fetch needs --bounds lon0,lat0,lon1,lat1 and --out dem.tif")
            r = tf.dem_for(_parse_bounds(a.bounds), a.res, a.source, a.cache, terms_acknowledged=a.terms_acknowledged)
            if not r["ok"]:
                print(json.dumps({"ok": False, "findings": r["findings"], "attribution": r.get("attribution")}, indent=2))
                return gc.EXIT_REFUSED if any(f["severity"] == "BLOCK" for f in r["findings"]) else gc.EXIT_RUNTIME
            import shutil
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(r["path"], a.out)
            Path(a.out).with_suffix(".json").write_text(json.dumps(tf.public_record(r["record"]), indent=2, default=str))
            print(json.dumps({"ok": True, "findings": r["findings"], "out": a.out, "record": tf.public_record(r["record"])}, indent=2, default=str))
            return gc.EXIT_OK
        if not a.dem:
            raise TerrainError(f"{a.op} needs --dem dem.tif (terrain.py fetch gets one)")
        if a.op == "profile":
            if not (a.a and a.b):
                raise TerrainError("profile needs --a lon,lat and --b lon,lat")
            r = profile(a.dem, a.a, a.b, a.samples or 200)
            if a.csv:
                Path(a.csv).write_text("distance_m,elevation_m\n" + "".join(f"{d:.1f},{'' if z is None else format(z, '.2f')}\n" for d, z in r["points"]))
            if a.out:
                Path(a.out).write_text(json.dumps(r, indent=2))
            print(json.dumps({k: v for k, v in r.items() if k != "points"} | {"points": len(r["points"])}, indent=2))
            return gc.EXIT_OK
        if a.op == "los":
            if not (a.a and a.b):
                raise TerrainError("los needs --a lon,lat and --b lon,lat")
            r = line_of_sight(a.dem, a.a, a.b, a.ha, a.hb, a.k, a.freq, a.samples)
            print(verdict_line(r))
            if a.out:
                Path(a.out).write_text(json.dumps(r, indent=2))
            print(json.dumps({k: v for k, v in r.items() if k != "profile"}, indent=2))
            return gc.EXIT_OK
        if a.op == "viewshed":
            if not (a.point and a.out):
                raise TerrainError("viewshed needs --point lon,lat and --out file.geojson")
            r = viewshed(a.dem, a.point, a.h, a.radius, a.step, target_h_m=a.target_h, k_factor=a.k)
            import geo_io
            geo_io.write(r.pop("gdf"), a.out)
            r.pop("geometry")
            r["out"] = a.out
            print(json.dumps(r, indent=2, default=str))
            return gc.EXIT_OK
        bounds = _parse_bounds(a.bounds) if a.bounds else None
        g, info = _dem_window(a.dem, bounds)
        if a.op == "hillshade":
            if not a.out:
                raise TerrainError("hillshade needs --out hill.tif or hill.png")
            hs = rio.hillshade(g.array, g.transform, a.azimuth, a.altitude, a.z, a.multi, crs=g.crs)
            rep = {"op": "hillshade", "dem": a.dem, "azimuth": a.azimuth, "altitude": a.altitude, "z_factor": a.z, "multidirectional": a.multi, "crs": g.crs, "shape": list(hs.shape),
                   "mean": float(np.nanmean(hs)), "out": a.out}
            if a.out.lower().endswith(".png"):
                png, _ = rio.to_png(hs, lambda t: (t, t, t), vmin=0.0, vmax=1.0)      # a grey ramp; a map draws it through the brand
                Path(a.out).write_bytes(png)
            else:
                rep["written"] = rio.write_geotiff(a.out, np.ma.masked_invalid(hs), g.transform, g.crs)
            print(json.dumps(rep, indent=2, default=str))
            return gc.EXIT_OK
        if a.op == "contours":
            if not a.out or not a.interval:
                raise TerrainError("contours needs --interval metres and --out contours.gpkg")
            import geo_io
            c = rio.contours(g.array, g.transform, g.crs, interval=a.interval, index_every=a.index)
            w = geo_io.write(c, a.out) if len(c) else None
            print(json.dumps({"op": "contours", "dem": a.dem, "interval_m": a.interval, "index_every": a.index, "lines": int(len(c)), "levels": sorted(set(float(v) for v in c["elevation"])) if len(c) else [],
                              "crs": g.crs, "out": a.out if w else None}, indent=2, default=str))
            return gc.EXIT_OK
    except TerrainError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_USAGE
    except rio.RasterError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_RUNTIME
    except Exception as e:  # noqa: BLE001
        import ogc_http as oh
        if isinstance(e, oh.ServiceError):
            print(json.dumps({"ok": False, "findings": [{"id": getattr(e, "rule", None) or "MAP-SVC-010", "severity": "BLOCK", "message": str(e)}]}))
            return gc.EXIT_SERVICE
        raise
    return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
