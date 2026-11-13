#!/usr/bin/env python3
"""crs-engine: detect, assign and validate coordinate reference systems, and choose a display projection (spec 7).

Library use:   import crs_engine as ce
               ce.describe(gdf); ce.coordinate_problems(gdf); ce.choose_display(bounds, purpose="thematic")
Command line:  crs_engine.py describe FILE
               crs_engine.py check FILE
               crs_engine.py choose FILE [--purpose thematic|reference|world] [--tiles]

Rules this module applies (the reasons are returned as text and recorded in the MapSpec, crs.rationale):
  * tile basemaps force Web Mercator (EPSG:3857), and only they do;
  * a world extent gets Equal Earth for area-based statistics and Robinson otherwise;
  * the contiguous US gets NAD83 / Conus Albers (EPSG:5070); Alaska gets EPSG:3338; Hawaii gets ESRI:102007;
    the whole US including Alaska and Hawaii gets the US National Atlas Equal Area (EPSG:9311; EPSG:2163 is its deprecated code
    and is still accepted as an input);
  * a local extent (under about 6 degrees wide) gets the UTM zone of its centre, in NAD83 where NAD83 is defined (the US, Canada,
    Puerto Rico and the US Virgin Islands) and in WGS 84 elsewhere (Mexico, Central America, the Caribbean);
  * a State Plane zone (choose_state_plane) is named as an alternative for a local US extent, never picked by itself;
  * any other regional extent gets a Lambert azimuthal equal-area centred on the extent (a proj string, no EPSG code);
  * an extent that crosses the antimeridian (a bbox with minx > maxx, or data whose parts straddle 180: spans_antimeridian) is centred
    on the antimeridian, never cut at 180 degrees.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

WEB_MERCATOR = "EPSG:3857"
EQUAL_AREA_METHODS = ("equal area", "equal earth", "albers", "mollweide", "sinusoidal", "eckert iv", "eckert vi")
LOCAL_SPAN_DEG = 6.0
STATE_SPAN_DEG = 16.0          # 1.13.0: a contiguous-US extent up to this wide and STATE_SPAN_H_DEG tall is state-sized: a centred Albers
STATE_SPAN_H_DEG = 12.0
POLAR_LAT = 80.0
NAD83_UTM_ZONES = range(1, 24)       # NAD83 UTM (EPSG:269zz) exists for zones 1 to 23 north


def _crs(x):
    from pyproj import CRS
    return x if isinstance(x, CRS) else CRS.from_user_input(x)


def describe(gdf_or_crs) -> dict:
    """Plain facts about a CRS: authority code, name, units, geographic or projected, datum, equal-area."""
    crs = gdf_or_crs.crs if hasattr(gdf_or_crs, "crs") else gdf_or_crs
    if crs is None:
        return {"defined": False}
    crs = _crs(crs)
    code = crs.to_authority()
    axis_units = sorted({a.unit_name for a in crs.axis_info})
    return {"defined": True, "authority": f"{code[0]}:{code[1]}" if code else None, "name": crs.name,
            "geographic": crs.is_geographic, "projected": crs.is_projected, "units": axis_units,
            "datum": crs.datum.name if crs.datum else None, "equal_area": is_equal_area(crs),
            "web_mercator": is_web_mercator(crs)}


def is_web_mercator(crs) -> bool:
    c = _crs(crs)
    return c.equals("EPSG:3857") or "pseudo-mercator" in c.name.lower() or "web mercator" in c.name.lower()


def is_equal_area(crs) -> bool:
    c = _crs(crs)
    if c.is_geographic:
        return False
    method = (c.coordinate_operation.method_name if c.coordinate_operation else "").lower()
    name = c.name.lower()
    return any(m in method or m in name for m in EQUAL_AREA_METHODS)


# --------------------------------------------------------------------------- coordinate sanity (MAP-DATA-004 / MAP-CRS)

def coordinate_problems(gdf) -> list[dict]:
    """Problems that the stated CRS does not explain: swapped lat/lon, out-of-range degrees, metres in a degree CRS.

    Each problem: {'code', 'severity', 'message'}. Codes: swapped, out_of_range, projected_in_degrees, degrees_in_projected.
    """
    out: list[dict] = []
    if gdf.empty:
        return out
    minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
    if any(math.isnan(v) for v in (minx, miny, maxx, maxy)):
        return out
    if gdf.crs is None:
        return out
    c = _crs(gdf.crs)
    if c.is_geographic:
        if abs(miny) > 90 or abs(maxy) > 90:
            if abs(minx) <= 90 and abs(maxx) <= 90 and (abs(miny) <= 180 and abs(maxy) <= 180):
                out.append({"code": "swapped", "severity": "BLOCK",
                            "message": f"latitude and longitude look swapped: y runs {miny:.4g} to {maxy:.4g} (latitude cannot pass 90) while x stays within 90"})
            elif max(abs(minx), abs(maxx), abs(miny), abs(maxy)) > 1000:
                out.append({"code": "projected_in_degrees", "severity": "BLOCK",
                            "message": f"values reach {max(abs(minx), abs(maxx), abs(miny), abs(maxy)):.6g}, which are projected units (metres or feet), but the CRS is {c.name} (degrees)"})
            else:
                out.append({"code": "out_of_range", "severity": "BLOCK",
                            "message": f"latitude runs {miny:.4g} to {maxy:.4g}; valid latitudes are -90 to 90"})
        elif abs(minx) > 180.0001 or abs(maxx) > 180.0001:
            if max(abs(minx), abs(maxx)) <= 360:
                out.append({"code": "out_of_range", "severity": "WARN",
                            "message": f"longitude runs {minx:.4g} to {maxx:.4g}; this looks like a 0 to 360 convention. Shift to -180 to 180"})
            else:
                out.append({"code": "projected_in_degrees", "severity": "BLOCK",
                            "message": f"longitude reaches {max(abs(minx), abs(maxx)):.6g}, which is a projected unit, but the CRS is {c.name} (degrees)"})
    elif c.is_projected:
        if max(abs(minx), abs(maxx), abs(miny), abs(maxy)) <= 360 and (maxx - minx) < 400:
            out.append({"code": "degrees_in_projected", "severity": "BLOCK",
                        "message": f"all values are within +-360, which are degrees, but the CRS is {c.name} (projected, {', '.join(sorted({a.unit_name for a in c.axis_info}))})"})
    return out


def guess_missing_crs(gdf) -> dict:
    """A missing CRS cannot be known; this proposes candidates from the values, stated as an assumption to confirm."""
    if gdf.empty or gdf.crs is not None:
        return {"candidates": [], "basis": "the data has a CRS" if gdf.crs is not None else "no data"}
    minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
    if -180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90:
        return {"candidates": ["EPSG:4326", "EPSG:4269"], "basis": "all values fit longitude/latitude; WGS84 and NAD83 differ by about a metre in North America"}
    if 100_000 <= minx and maxx <= 900_000 and 0 <= miny and maxy <= 10_000_000:
        return {"candidates": [], "basis": "values look like UTM metres, but the zone cannot be read from the values; ask for the CRS"}
    if abs(minx) < 20_100_000 and abs(maxx) < 20_100_000:
        return {"candidates": ["EPSG:3857"], "basis": "values fit Web Mercator metres; confirm before assuming"}
    return {"candidates": [], "basis": "the values fit no common CRS; ask for the CRS"}


# --------------------------------------------------------------------------- assign / reproject

def assign(gdf, crs, *, override: bool = False):
    """Set the CRS of data that has none. Replacing an existing CRS needs override=True (that is a claim, not a transform)."""
    if gdf.crs is not None and not override:
        raise ValueError(f"the data already has {gdf.crs.to_string()}; use reproject to change it, or override to relabel it")
    return gdf.set_crs(crs, allow_override=override)


def datum_note(src, dst) -> dict | None:
    """Describe the datum shift between two CRSs when they differ (MAP-CRS-004): the datums, the operation a reprojection will use
    (the best one whose grids are on this machine) and its accuracy, and the grid files a better operation would need."""
    import warnings
    from pyproj.transformer import TransformerGroup
    s, d = _crs(src), _crs(dst)
    sd = s.datum.name if s.datum else None
    dd = d.datum.name if d.datum else None
    if sd == dd or sd is None or dd is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                  # pyproj warns when the best operation's grid is missing; that is reported below
        grp = TransformerGroup(s, d, always_xy=True)
    best = grp.transformers[0] if grp.transformers else None
    acc = best.accuracy if best and best.accuracy not in (None, -1) else None
    better = []
    for op in grp.unavailable_operations:
        if acc is not None and op.accuracy not in (None, -1) and op.accuracy >= acc:
            continue
        for g in getattr(op, "grids", None) or []:
            if not g.available and g.short_name and g.short_name not in better:
                better.append(g.short_name)
        if better:
            break                                        # the first better operation is the one worth installing
    return {"from_datum": sd, "to_datum": dd,
            "operation": best.description if best else None,
            "accuracy_m": acc,
            "ballpark": best is None or "ballpark" in (best.description or "").lower(),
            "grids_missing": bool(grp.unavailable_operations),
            "better_grids": better}


DATUM_ACCURACY_WARN_M = 5.0          # NAD83 <-> WGS84 (1) is 4 m; anything coarser, or a ballpark offset, is worth a warning


def datum_accuracy_problem(src, dst) -> dict | None:
    """The datum note when the reprojection from src to dst is coarse: a ballpark offset (no real transform), an unknown accuracy,
    or an accuracy worse than DATUM_ACCURACY_WARN_M. None when the datums agree or the transform is good."""
    note = datum_note(src, dst)
    if note is None:
        return None
    if note["ballpark"] or note["accuracy_m"] is None or note["accuracy_m"] > DATUM_ACCURACY_WARN_M:
        return note
    return None


# --------------------------------------------------------------------------- area of use (MAP-CRS-006)

AOU_MARGIN_MIN_DEG = 1.0             # data commonly runs a little past a zone's edge
AOU_MARGIN_FRAC = 0.25
US_FOOT = 1200 / 3937                 # metres per US survey foot
INTL_FOOT = 0.3048


def _lonlat_box(c, minx, miny, maxx, maxy):
    """The lon/lat box of a projected bbox, by the CRS's own formulas (its geodetic CRS, so no datum shift enters)."""
    from pyproj import Transformer
    t = Transformer.from_crs(c, c.geodetic_crs, always_xy=True)
    xs = [minx, maxx, minx, maxx, (minx + maxx) / 2, minx, maxx, (minx + maxx) / 2, (minx + maxx) / 2]
    ys = [miny, miny, maxy, maxy, (miny + maxy) / 2, (miny + maxy) / 2, (miny + maxy) / 2, miny, maxy]
    lon, lat = t.transform(xs, ys)
    pts = [(a, b) for a, b in zip(lon, lat, strict=True) if math.isfinite(a) and math.isfinite(b)]
    if len(pts) < 2:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _inside(box, aou, mx, my) -> bool:
    w, s, e, n = aou
    return box[0] >= w - mx and box[2] <= e + mx and box[1] >= s - my and box[3] <= n + my


def area_of_use_problem(gdf) -> dict | None:
    """MAP-CRS-006: a layer whose coordinates, read in the CRS it states, fall outside the area that CRS is defined for.

    That is the mark of a CRS label that is not the one the file was written in: the wrong UTM or State Plane zone, feet read as metres,
    or a CRS assumed for a file that stated none. Reprojection then moves the layer to the wrong place, and it does not line up with the others.
    Returns None when the layer sits within the area (with a margin), or the CRS has no bounded area of use."""
    if gdf is None or gdf.crs is None or gdf.empty:
        return None
    c = _crs(gdf.crs)
    aou = c.area_of_use
    if aou is None or not c.is_projected:
        return None
    w, s, e, n = aou.bounds
    if w > e or (e - w >= 359 and n - s >= 170):
        return None                                      # crosses the antimeridian (checked elsewhere) or covers the world
    minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        return None
    mx = max(AOU_MARGIN_MIN_DEG, AOU_MARGIN_FRAC * (e - w))
    my = max(AOU_MARGIN_MIN_DEG, AOU_MARGIN_FRAC * (n - s))
    box = _lonlat_box(c, minx, miny, maxx, maxy)
    if box is not None and _inside(box, (w, s, e, n), mx, my):
        return None
    out = {"crs": c.name, "area": aou.name, "area_bounds": [round(v, 2) for v in (w, s, e, n)],
           "layer_lonlat": [round(v, 2) for v in box] if box else None,
           "disjoint": box is None or box[2] < w - mx or box[0] > e + mx or box[3] < s - my or box[1] > n + my,
           "oversize": box is not None and ((box[2] - box[0]) > 2 * (e - w + 2 * mx) or (box[3] - box[1]) > 2 * (n - s + 2 * my)),
           "hint": None, "units_hint": False}
    unit = c.axis_info[0].unit_conversion_factor if c.axis_info else 1.0
    tries = []
    if abs(unit - 1.0) < 1e-9:
        tries = [(US_FOOT, "the numbers look like US survey feet, but this CRS is in metres; use the (ftUS) version of this CRS"),
                 (INTL_FOOT, "the numbers look like international feet, but this CRS is in metres; use the (ft) version of this CRS")]
    elif abs(unit - US_FOOT) < 1e-6 or abs(unit - INTL_FOOT) < 1e-6:
        tries = [(1 / unit, "the numbers look like metres, but this CRS is in feet; use the metre version of this CRS")]
    for f, why in tries:
        b2 = _lonlat_box(c, minx * f, miny * f, maxx * f, maxy * f)
        if b2 is not None and _inside(b2, (w, s, e, n), mx, my):
            out["hint"], out["units_hint"] = why, True
            break
    if out["hint"] is None:
        out["hint"] = ("the coordinates were probably written in another CRS: check the zone (UTM, State Plane), the units (feet or metres) "
                       "and the order of x and y, then set the layer's crs: to the one the file was made in")
    return out


def reproject(gdf, dst):
    """Reproject, returning (gdf, datum_note_or_None)."""
    if gdf.crs is None:
        raise ValueError("the data has no CRS; assign one before reprojecting")
    note = datum_note(gdf.crs, dst)
    return gdf.to_crs(dst), note


# --------------------------------------------------------------------------- display projection

def _utm_for(lon: float, lat: float, north_america: bool) -> tuple[str, str]:
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    if north_america and lat >= 0 and zone in NAD83_UTM_ZONES and in_nad83_area(lon, lat):
        return f"EPSG:269{zone:02d}", f"NAD83 / UTM zone {zone}N"
    base = 32600 if lat >= 0 else 32700
    return f"EPSG:{base + zone}", f"WGS 84 / UTM zone {zone}{'N' if lat >= 0 else 'S'}"


# NAD83 (EPSG:6318) is defined for the United States (with Alaska and Hawaii), Canada, Puerto Rico and the US Virgin Islands. Its EPSG area
# of use is one box from 167.65 E round to 63.88 W and 14.92 to 74.71 N, which also covers Mexico, Central America and the whole Caribbean,
# where NAD83 is not the datum in use. These boxes are the parts of that area where NAD83 is the right choice; everything else gets WGS 84.
NAD83_BOXES = (
    (-141.1, 24.0, -52.5, 84.0),      # Canada and the contiguous US (lat 24 keeps the Florida Keys and leaves Cuba out)
    (-180.0, 51.0, -129.0, 72.0),     # Alaska west of 141 W
    (167.0, 50.0, 180.0, 72.0),       # the Aleutians past the antimeridian
    (-161.0, 18.5, -154.0, 22.5),     # Hawaii
    (-68.0, 17.5, -64.0, 18.6),       # Puerto Rico and the US Virgin Islands
    (-98.0, 24.0, -80.0, 32.0),       # the Gulf coast and Florida (south Texas to the Keys)
)


def in_nad83_area(lon: float, lat: float) -> bool:
    """Whether NAD83 is the datum to map a point in (see NAD83_BOXES). Mexico (lat < 32 west of 98 W), Central America and the
    Caribbean islands other than Puerto Rico and the US Virgin Islands are outside it, so their local extents get WGS 84 UTM."""
    for w, s, e, n in NAD83_BOXES:
        if w <= lon <= e and s <= lat <= n:
            if (w, s, e, n) == NAD83_BOXES[0] and lat < 32 and lon < -98.0:
                continue                          # north-west Mexico lies inside the big box's south-west corner
            return True
    return False


def spans_antimeridian(gdf) -> bool:
    """Whether a layer's geometry crosses 180 degrees of longitude: a Pacific extent (the Aleutians, Fiji, Kiribati) whose bbox does not
    say so because minx < maxx. True when any single part is wider than 180 degrees (drawn the long way round), or when parts lie on
    both sides of the antimeridian within a few degrees of it while the layer as a whole spans more than 180 degrees."""
    import shapely
    if gdf is None or gdf.empty or gdf.crs is None:
        return False
    g = gdf if _crs(gdf.crs).is_geographic else gdf.to_crs("EPSG:4326")
    ok = ~g.geometry.isna().values & ~g.geometry.is_empty.values
    if not ok.any():
        return False
    parts = shapely.get_parts(g.geometry.values[ok])
    pb = shapely.bounds(parts)
    if ((pb[:, 2] - pb[:, 0]) > 180).any():
        return True
    minx, maxx = float(pb[:, 0].min()), float(pb[:, 2].max())
    if maxx - minx <= 180:
        return False
    near_east = bool((pb[:, 2] > 170).any())
    near_west = bool((pb[:, 0] < -170).any())
    return near_east and near_west


def _wgs84_bounds(gdf_or_bounds, crs=None) -> tuple[float, float, float, float]:
    if hasattr(gdf_or_bounds, "total_bounds"):
        g = gdf_or_bounds
        if g.crs is not None and not _crs(g.crs).equals("EPSG:4326"):
            g = g.to_crs("EPSG:4326")
        return tuple(float(v) for v in g.total_bounds)
    return tuple(float(v) for v in gdf_or_bounds)


def _antimeridian_bounds(gdf) -> tuple[float, float, float, float]:
    """The bbox of a layer that crosses 180, written as minx > maxx (the eastern edge first) so choose_display takes the antimeridian branch:
    the western-most part east of 180 starts it, the eastern-most part west of 180 ends it."""
    import shapely
    g = gdf if _crs(gdf.crs).is_geographic else gdf.to_crs("EPSG:4326")
    ok = ~g.geometry.isna().values & ~g.geometry.is_empty.values
    pb = shapely.bounds(shapely.get_parts(g.geometry.values[ok]))
    east = pb[pb[:, 0] >= 0]
    west = pb[pb[:, 2] < 0]
    minx = float(east[:, 0].min()) if len(east) else float(pb[:, 0].min())
    maxx = float(west[:, 2].max()) if len(west) else float(pb[:, 2].max())
    return minx, float(pb[:, 1].min()), maxx, float(pb[:, 3].max())


STATE_PLANE_NAME = re.compile(r"^NAD83 / (?!BLM)(.+) \((?:ftUS|ft)\)$")


def choose_state_plane(lon: float, lat: float) -> dict | None:
    """The NAD83 State Plane zone in feet (US survey feet, or international feet where the state legislated them, as Arizona did) that
    contains a point: {'crs', 'epsg', 'name'}, or None outside the US. The zones come from pyproj's EPSG database, so the list cannot
    drift from PROJ; when a point lies in two zones' areas of use (they overlap along their shared edge), the zone with the smaller area is
    taken, then the one whose area is centred nearest the point.

    Named in notes as an alternative for engineering-grade work inside one state; never the default pick (UTM keeps one rule everywhere)."""
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_crs_info
    if not in_nad83_area(lon, lat):
        return None
    try:
        infos = query_crs_info(auth_name="EPSG", pj_types="PROJECTED_CRS", area_of_interest=AreaOfInterest(lon, lat, lon, lat), contains=True)
    except Exception:  # noqa: BLE001 - an old PROJ without the query falls back to no alternative
        return None
    hits = [i for i in infos if not i.deprecated and STATE_PLANE_NAME.match(i.name) and i.area_of_use is not None]

    def key(i):
        w, s, e, n = i.area_of_use.bounds
        return ((e - w) * (n - s), abs((w + e) / 2 - lon) + abs((s + n) / 2 - lat))

    if not hits:
        return None
    best = sorted(hits, key=key)[0]
    return {"crs": f"EPSG:{best.code}", "epsg": int(best.code), "name": best.name}


def state_plane_for_extent(minx: float, miny: float, maxx: float, maxy: float) -> dict | None:
    """0.15.0: the one NAD83 State Plane zone whose area of use holds the whole extent (all four corners and the centre), or None. The zones
    come from PROJ's EPSG database (NGS's SPCS83 definitions), as in choose_state_plane."""
    corners = [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy), ((minx + maxx) / 2, (miny + maxy) / 2)]
    picks = [choose_state_plane(x, y) for x, y in corners]
    if not picks or any(p is None for p in picks):
        return None
    codes = {p["epsg"] for p in picks}
    if len(codes) == 1:
        return picks[0]
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_crs_info
    try:                                             # corners on a shared edge can name either zone: take a zone that contains the whole box
        infos = query_crs_info(auth_name="EPSG", pj_types="PROJECTED_CRS", area_of_interest=AreaOfInterest(minx, miny, maxx, maxy), contains=True)
    except Exception:  # noqa: BLE001
        return None
    hits = sorted((i for i in infos if not i.deprecated and STATE_PLANE_NAME.match(i.name) and i.area_of_use is not None and int(i.code) in codes),
                  key=lambda i: (i.area_of_use.bounds[2] - i.area_of_use.bounds[0]) * (i.area_of_use.bounds[3] - i.area_of_use.bounds[1]))
    return {"crs": f"EPSG:{hits[0].code}", "epsg": int(hits[0].code), "name": hits[0].name} if hits else None


def _national_choice(minx, miny, maxx, maxy, cx, purpose, equal_wanted, width):
    """(crs, name, why, equal_area, epsg, alternatives) for the country's preset or Europe's, or None (1.5.0, R3-CRS)."""
    try:
        import geo_locale as gl
        isos = gl.countries_in((minx, miny, maxx, maxy))
    except Exception:  # noqa: BLE001 - without the bundled table the generic rules apply
        return None
    if not isos:
        return None
    if len(isos) == 1:
        iso = isos[0]
        pre = gl.national_crs(iso, "thematic" if equal_wanted else "reference", cx)
        if not pre:
            return None
        if equal_wanted and not pre["equal_area"] and width > 2 * LOCAL_SPAN_DEG:
            return None                       # a conformal national grid over a whole large country distorts areas: the centred LAEA stays
        nm = (gl.country(iso) or {}).get("name", iso)
        why = (f"The window lies inside {nm}, which has a national preset: {pre['name']} ({pre['crs']}), the grid its mapping agency publishes in."
               + (" It is equal-area." if pre["equal_area"] else " It is conformal; over one country the area error stays well under one percent."))
        return (pre["crs"], pre["name"], why, bool(pre["equal_area"]), int(pre["crs"].split(":")[1]), ["crs.prefer: auto for the generic rule (UTM or a centred LAEA)"])
    eu = (gl.crs_table().get("regions") or {}).get("europe") or {}
    rows = [gl.country(i) or {} for i in isos]
    bb = eu.get("bbox") or [-180, -90, 180, 90]
    n_eu = sum(1 for r in rows if r.get("region") == "Europe")
    if (eu and width > LOCAL_SPAN_DEG and rows[0].get("region") == "Europe" and n_eu * 2 > len(rows)
            and bb[0] <= minx and maxx <= bb[2] and bb[1] <= miny and maxy <= bb[3]):
        return (eu["crs"], "ETRS89-extended / LAEA Europe",
                f"The window crosses {len(isos)} countries, {n_eu} of them European; ETRS89-LAEA (EPSG:3035) is the equal-area grid of the EU's statistical mapping (INSPIRE, Eurostat).",
                True, int(eu["crs"].split(":")[1]), ["a centred LAEA with crs.prefer: auto"])
    return None


def choose_display(extent, purpose: str = "thematic", *, tiles: bool = False, area_stat: bool | None = None, prefer: str | None = None) -> dict:
    """Pick a display CRS for an extent (a GeoDataFrame or a WGS84 bbox minx, miny, maxx, maxy).

    purpose: 'thematic' (values per area; equal-area), 'reference' (locations; conformal is fine), 'world', 'engineering' (0.15.0: a local US
    map in the State Plane zone that holds it). prefer='state_plane' (MapSpec crs.prefer) asks for the same on any purpose.
    Returns {'crs', 'epsg', 'name', 'rationale', 'equal_area', 'antimeridian', 'alternatives'}.
    """
    if hasattr(extent, "total_bounds") and spans_antimeridian(extent):
        minx, miny, maxx, maxy = _antimeridian_bounds(extent)        # written as minx > maxx so the branch below takes it
    else:
        minx, miny, maxx, maxy = _wgs84_bounds(extent)
    equal_wanted = (purpose == "thematic") if area_stat is None else area_stat
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    width, height = maxx - minx, maxy - miny
    alts: list[str] = []
    anti = False
    # an extent given as minx > maxx (a bbox, or a layer whose parts straddle 180: spans_antimeridian) crosses the antimeridian
    if minx > maxx:
        anti = True
        width = (maxx + 360) - minx
        cx = ((minx + maxx + 360) / 2 + 180) % 360 - 180

    def out(crs, name, why, ea, epsg=None, extra_alts=None):
        return {"crs": crs, "epsg": epsg, "name": name, "rationale": why, "equal_area": ea,
                "antimeridian": anti, "alternatives": extra_alts if extra_alts is not None else alts}

    if tiles:
        return out(WEB_MERCATOR, "WGS 84 / Pseudo-Mercator", "Web Mercator is used because a tile basemap is shown; tiles are only published in it. "
                   "Areas are distorted away from the equator, so it is unsuitable for area-based statistics at regional scale or larger.",
                   False, 3857, ["EPSG:5070 without tiles for US thematic maps"])
    if purpose == "world" or width > 180 or height > 100:
        if equal_wanted:
            return out("EPSG:8857", "WGS 84 / Equal Earth Greenwich", "The extent is global and the statistic depends on area, so an equal-area projection is used: Equal Earth.",
                       True, 8857, ["ESRI:54030 Robinson (compromise, not equal-area)"])
        return out("ESRI:54030", "World_Robinson", "The extent is global and locations, not areas, are shown; Robinson balances distortion for a world view.",
                   False, None, ["EPSG:8857 Equal Earth if the map ever shows values per area"])
    # 1.5.0 (R3-CRS): an EPSG code asked for in crs.prefer is used as asked. 1.13.0 (Day 12 issue 2): before the US families, so a state map
    # that names its own CRS gets it (1.12 checked it after them, and a Washington map took Conus Albers whatever it asked for)
    pref = (prefer or "").strip()
    if re.match(r"^(EPSG|ESRI):[0-9]+$", pref):
        c = _crs(pref)
        return out(pref, c.name, f"{c.name} was asked for (crs.prefer)." + ("" if is_equal_area(c) else " It is not equal-area; values per area are not comparable across the map."),
                   is_equal_area(c), int(pref.split(":")[1]) if pref.startswith("EPSG") else None)
    # United States families. An extent that crosses the antimeridian can only be Alaska's (the Aleutians) among these.
    in_conus = not anti and -125.5 <= minx and maxx <= -66 and 24 <= miny and maxy <= 49.6
    in_alaska = (165 <= minx if anti else -180 <= minx) and maxx <= -129 and 51 <= miny and maxy <= 72
    in_hawaii = not anti and -161 <= minx and maxx <= -154 and 18.5 <= miny and maxy <= 22.5
    # "spans the US" means it reaches both Alaska or Hawaii longitudes and the lower 48: a Mexican or Caribbean box inside the same
    # window is not a US map and falls through to the local and regional rules
    spans_us = not anti and -180 <= minx <= -129 and -100 <= maxx <= -64 and 17 <= miny and maxy <= 72 and not (in_conus or in_alaska or in_hawaii)
    if in_conus and width > LOCAL_SPAN_DEG and width <= STATE_SPAN_DEG and height <= STATE_SPAN_H_DEG:
        # 1.13.0 (Day 12 issue 2): a state-sized US map gets an Albers equal-area centred on it, so the state stands upright (Conus Albers,
        # centred at 96 W, turned Washington about 10 degrees). State Plane when asked for and one zone holds it.
        if pref.replace("-", "_") == "state_plane" or purpose == "engineering":
            whole = state_plane_for_extent(minx, miny, maxx, maxy)
            if whole:
                return out(whole["crs"], whole["name"], f"State Plane was asked for (crs.prefer or an engineering map): {whole['name']} holds the whole extent.",
                           False, whole["epsg"], ["an Albers equal-area centred on the extent"])
        lat1, lat2 = round(miny + height / 6, 2), round(maxy - height / 6, 2)
        lon0, lat0 = round(cx, 2), round(cy, 2)
        proj = f"+proj=aea +lat_0={lat0} +lon_0={lon0} +lat_1={lat1} +lat_2={lat2} +x_0=0 +y_0=0 +datum=NAD83 +units=m +no_defs"
        return out(proj, "Albers Equal Area (NAD83, centred on the extent)",
                   f"The data lies in the contiguous United States at the size of a state ({width:.1f} by {height:.1f} degrees). An Albers equal-area "
                   f"projection centred on it (central meridian {lon0}, standard parallels {lat1} and {lat2}) keeps areas true and the extent upright; "
                   "Conus Albers would turn it by the distance from 96 W.",
                   True, None, ["EPSG:5070 Conus Albers (the national projection)", "the State Plane zone, for engineering-grade work"])
    if in_conus and width > LOCAL_SPAN_DEG:
        return out("EPSG:5070", "NAD83 / Conus Albers", "The data lies in the contiguous United States at a regional or national scale. Conus Albers is equal-area and conforms to US federal mapping practice.",
                   True, 5070, ["EPSG:9311 for a national map that also shows Alaska and Hawaii", "UTM or state plane for one state"])
    if in_alaska and width > LOCAL_SPAN_DEG:
        return out("EPSG:3338", "NAD83 / Alaska Albers", "The data lies in Alaska; Alaska Albers is the standard equal-area projection there.", True, 3338)
    if in_hawaii and width > 1.5:
        return out("ESRI:102007", "Hawaii Albers Equal Area Conic", "The data lies in Hawaii; Hawaii Albers is the standard equal-area projection there.", True, None)
    if spans_us:
        return out("EPSG:9311", "NAD27 / US National Atlas Equal Area", "The data spans the United States including Alaska and/or Hawaii; the US National Atlas Equal Area keeps areas true across all of them "
                   "(EPSG:9311, the current code for the projection once listed as EPSG:2163).",
                   True, 9311, ["EPSG:5070 if only the lower 48 are shown"])
    pref = (prefer or "").strip()
    # 1.5.0: national presets (data/national-crs.yaml) when the window is inside one country that has one; ETRS89-LAEA across Europe
    if not anti and pref.replace("-", "_") in ("", "national") and abs(cy) <= POLAR_LAT:
        nat = _national_choice(minx, miny, maxx, maxy, cx, purpose, equal_wanted, width)
        if nat:
            return out(*nat)
    # local extent
    if width <= LOCAL_SPAN_DEG and abs(cy) <= POLAR_LAT:
        north_america = -170 <= cx <= -50 and 7 <= cy <= 84
        code, name = _utm_for(cx, cy, north_america)
        zones_spanned = len({int((x + 180) // 6) for x in (minx, maxx)})
        why = (f"The extent is about {width:.1f} degrees wide, local enough for one UTM zone: {name}, chosen from the centre of the data. "
               "UTM is conformal, so shapes and distances stay good; area error stays under about 0.1 percent inside a zone.")
        if zones_spanned > 1:
            why += " The extent touches two zones; the centre's zone is used and edge distortion rises slightly."
        if anti:
            why += " The extent crosses the antimeridian; check that the data is not cut at 180 degrees."
        if equal_wanted:
            why += " For area-based statistics at this size the area error is negligible."
        want_sp = (prefer or "").replace("-", "_") == "state_plane" or purpose == "engineering"
        if want_sp and north_america and not anti:
            whole = state_plane_for_extent(minx, miny, maxx, maxy)
            if whole:
                return out(whole["crs"], whole["name"], f"The map is local to the United States and asks for State Plane (crs.prefer or an engineering map): {whole['name']} "
                           "holds the whole extent. It is the survey grid of the state and its counties, conformal, with scale error under 1 in 10,000 inside the zone.",
                           False, whole["epsg"], [f"{code} ({name}), the UTM zone, for one rule everywhere"])
            why += " State Plane was asked for, but the extent leaves one zone (or lies outside NAD83 State Plane coverage), so the UTM zone is used."
        sp = choose_state_plane(cx, cy) if north_america else None
        local_alts = [f"{sp['crs']} ({sp['name']}) for engineering-grade work inside one US state" if sp else "state plane for engineering-grade work inside one US state"]
        if not in_nad83_area(cx, cy) and north_america:
            why += " NAD83 is not the datum in use here, so the WGS 84 zone is taken."
        return out(code, name, why, False, int(code.split(":")[1]), local_alts)
    # polar
    if abs(cy) > POLAR_LAT or maxy > 87 or miny < -87:
        pole = "north" if cy > 0 else "south"
        epsg = "EPSG:3413" if pole == "north" else "EPSG:3031"
        nm = "WGS 84 / NSIDC Sea Ice Polar Stereographic " + pole.capitalize()
        return out(epsg, nm, f"The extent is polar ({pole}); a polar stereographic projection avoids the distortion and the antimeridian cut of a cylindrical one.", False, int(epsg.split(":")[1]))
    # regional elsewhere: Lambert azimuthal equal-area centred on the extent
    lon0 = round(cx, 1)
    lat0 = round(cy, 1)
    proj = f"+proj=laea +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    why = (f"The extent is regional ({width:.0f} by {height:.0f} degrees), outside the United States presets. "
           f"A Lambert azimuthal equal-area centred on the extent ({lat0}, {lon0}) keeps areas true and distortion low near the centre.")
    if anti:
        why += " The extent crosses the antimeridian; centring on it avoids cutting the data at 180 degrees."
    return out(proj, "Lambert Azimuthal Equal Area (centred on the extent)", why, True, None,
               ["a national CRS for one country (for example a local Lambert Conformal Conic)"])


# --------------------------------------------------------------------------- MapSpec fragment

def mapspec_crs(choice: dict, requested: str | None = None) -> dict:
    """The `crs:` block for a MapSpec. A user-requested CRS is kept; the rationale says so."""
    if requested and requested != "auto":
        c = _crs(requested)
        return {"display": requested, "rationale": f"{c.name} was requested by the user." + (
            "" if is_equal_area(c) else " It is not equal-area; area-based statistics are not comparable across the map.")}
    return {"display": choice["crs"], "rationale": choice["rationale"]}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("describe", "check", "choose"):
        p = sub.add_parser(name)
        p.add_argument("path")
        p.add_argument("--layer")
        p.add_argument("--crs", help="CRS to assume when the file has none")
        if name == "choose":
            p.add_argument("--purpose", default="thematic", choices=["thematic", "reference", "world"])
            p.add_argument("--tiles", action="store_true")
    a = ap.parse_args(argv)
    import geo_io
    try:
        r = geo_io.read(a.path, layer=a.layer, crs_assume=a.crs)
    except geo_io.GeoIOError as e:
        print(f"crs-engine: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    g = r.gdf
    if a.cmd == "describe":
        print(json.dumps(describe(g) | {"assumed": r.crs_assumed}, indent=2))
    elif a.cmd == "check":
        res = {"crs": describe(g), "problems": coordinate_problems(g), "if_missing": guess_missing_crs(g)}
        print(json.dumps(res, indent=2))
        return gc.EXIT_LINT if any(p["severity"] == "BLOCK" for p in res["problems"]) else gc.EXIT_OK
    else:
        if g.crs is None:
            print("crs-engine: the data has no CRS; pass --crs", file=sys.stderr)
            return gc.EXIT_USAGE
        print(json.dumps(choose_display(g, a.purpose, tiles=a.tiles), indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
