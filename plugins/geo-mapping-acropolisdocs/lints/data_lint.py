"""Data, CRS and round-trip lints: MAP-DATA-*, MAP-CRS-* and MAP-OUT-005/006/007 (spec 12.2, 12.3, 12.7).

Each check returns a list of findings: {'id', 'severity', 'message', 'detail'}. Severity is BLOCK, WARN or INFO.
`worst()` gives the process exit code (3 when any BLOCK). The checks read data; they change nothing except `repair_geometry`,
which returns a new frame and the area change it caused.

    python3 data_lint.py FILE [--layer L] [--crs EPSG:n] [--threshold-null 0.05]      # MAP-DATA on one file
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import crs_engine as ce  # noqa: E402
import geo_io  # noqa: E402
import gmap_common as gc  # noqa: E402

SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}
NULL_RATIO_WARN = 0.05
REPAIR_AREA_BLOCK = 0.01          # a repair that changes area by more than 1 percent blocks (MAP-DATA-002)
JOIN_MATCH_MIN = 0.95
SIZE_FEATURES_WARN = 100_000
SIZE_VERTICES_WARN = 2_000_000
AREA_BASED_TYPES = {"choropleth", "density", "dasymetric", "hexbin-density", "cartogram"}
REGIONAL_SPAN_DEG = 6.0


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def worst(findings: list[dict]) -> int:
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in findings) else gc.EXIT_OK


def _geom_col(gdf):
    return gdf.geometry.name


# --------------------------------------------------------------------------- MAP-DATA

def check_crs_defined(gdf, assumed: bool = False, assumption: str | None = None) -> list[dict]:
    if gdf.crs is not None:
        if assumed:
            return [finding("MAP-DATA-001", "INFO", f"the CRS ({gdf.crs.to_string()}) was assumed, not read from the data" + (f": {assumption}" if assumption else ""))]
        return []
    guess = ce.guess_missing_crs(gdf)
    return [finding("MAP-DATA-001", "BLOCK", "the data has no CRS, and it was not stated. " + guess["basis"], guess["candidates"])]


def check_validity(gdf) -> list[dict]:
    import shapely
    if gdf.empty:
        return []
    bad = ~shapely.is_valid(gdf.geometry.values) & ~gdf.geometry.isna().values
    n = int(bad.sum())
    if not n:
        return []
    return [finding("MAP-DATA-002", "WARN", f"{n} feature(s) have invalid geometry (self-intersections or unclosed rings); "
                    "repair them with geo-convert --repair (make_valid, logged with the area change), or set repair: true or ops: [{op: repair}] on the layer",
                    [int(i) for i in gdf.index[bad][:20]])]


def _area(geoms, crs) -> float:
    """Total area of geometries, in an equal-area CRS when the data is in degrees."""
    import geopandas as gpd
    s = gpd.GeoSeries(geoms, crs=crs)
    if crs is not None and crs.is_geographic:
        s = s.to_crs("EPSG:6933")
    return float(s.area.sum())


def repair_geometry(gdf):
    """Repair invalid geometries with make_valid. Returns (gdf, findings). A repair that changes area by > 1 percent blocks."""
    import shapely
    out = gdf.copy()
    mask = ~shapely.is_valid(out.geometry.values) & ~out.geometry.isna().values
    n = int(mask.sum())
    if not n:
        return out, []
    # an invalid polygon's own area is unreliable (a bowtie's lobes cancel), so the change is measured against the repaired area
    fixed = shapely.make_valid(out.geometry.values[mask])
    geoms = out.geometry.values.copy()
    geoms[mask] = fixed
    out[_geom_col(out)] = geoms
    before = _area(gdf.geometry.values[mask], gdf.crs)
    after = _area(fixed, gdf.crs)
    change = 0.0 if before == after else (1.0 if before == 0 else abs(after - before) / before)
    sev = "BLOCK" if change > REPAIR_AREA_BLOCK else "INFO"
    return out, [finding("MAP-DATA-002", sev, f"{n} invalid geometr{'y was' if n == 1 else 'ies were'} repaired; their total area changed by {change * 100:.3f} percent"
                         + (" (over the 1 percent limit; check the data)" if sev == "BLOCK" else ""), {"repaired": n, "area_change": change})]


def reduce_collections(gdf):
    """A GeometryCollection row (what make_valid often returns for a bowtie: polygons plus stray lines or points) becomes its polygons,
    else its lines, else its points, so a repaired layer is not read as empty or mixed. Returns (gdf, rows changed).

    The same rule as map_plan.unpack_collections, kept here so the data side (geo-convert --repair) does not import the planner."""
    import shapely
    from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon
    if gdf.empty:
        return gdf, 0
    gc_rows = (gdf.geometry.geom_type == "GeometryCollection") & ~gdf.geometry.isna()
    if not gc_rows.any():
        return gdf, 0

    def best(g):
        parts = list(shapely.get_parts(shapely.get_parts(g)))
        flat = [q for p in parts for q in (getattr(p, "geoms", None) or [p])]
        for types, cls in ((("Polygon",), MultiPolygon), (("LineString", "LinearRing"), MultiLineString), (("Point",), MultiPoint)):
            got = [p for p in flat if p.geom_type in types and not p.is_empty]
            if got:
                return cls(got) if cls is not MultiLineString else MultiLineString([list(p.coords) for p in got])
        return g

    out = gdf.copy()
    out.loc[gc_rows, _geom_col(out)] = [best(g) for g in out.geometry[gc_rows]]
    return out, int(gc_rows.sum())


def check_null_geometry(gdf, threshold: float = NULL_RATIO_WARN) -> list[dict]:
    if gdf.empty:
        return [finding("MAP-DATA-003", "WARN", "the layer has no features")]
    empty = int((gdf.geometry.isna() | gdf.geometry.is_empty).sum())
    ratio = empty / len(gdf)
    if ratio > threshold:
        return [finding("MAP-DATA-003", "WARN", f"{empty} of {len(gdf)} features ({ratio * 100:.1f} percent) have no geometry; the limit is {threshold * 100:.0f} percent", empty)]
    return []


def check_coordinates(gdf) -> list[dict]:
    return [finding("MAP-DATA-004", p["severity"], p["message"], p["code"]) for p in ce.coordinate_problems(gdf)]


ID_LIKE = re.compile(r"(^|_)(id|fid|gid|objectid|geoid|fips|code|key)$", re.I)


def check_duplicates(gdf, id_field: str | None = None) -> list[dict]:
    import shapely
    out = []
    if gdf.empty:
        return out
    cols = [id_field] if id_field else [c for c in gdf.columns if c != _geom_col(gdf) and ID_LIKE.search(str(c))]
    for c in cols:
        if c in gdf.columns:
            vals = gdf[c].dropna()
            d = int(vals.duplicated().sum())
            if d:
                out.append(finding("MAP-DATA-005", "WARN", f"'{c}' has {d} duplicated value(s) across {len(gdf)} features", c))
    import pandas as pd
    wkb = shapely.to_wkb(gdf.geometry.values)
    # the key is the geometry's WKB and the attributes joined with "|" as text, built column by column (a row-wise join took 12 s on 25,000 rows, M16);
    # only rows whose geometry occurs more than once can repeat a whole feature, so the attribute text is built for those rows alone
    rep = pd.Series(wkb).duplicated(keep=False).to_numpy()
    keys = pd.DataFrame({"__wkb": wkb[rep]})
    attrs = gdf.drop(columns=_geom_col(gdf))[rep]
    if len(attrs.columns):
        text = attrs.astype(object).fillna("").astype(str)
        joined = text.iloc[:, 0].to_numpy(dtype=object)
        for c in range(1, len(text.columns)):
            joined = joined + "|" + text.iloc[:, c].to_numpy(dtype=object)
        keys["__row"] = joined
    dup = int(keys.duplicated().sum())
    if dup:
        out.append(finding("MAP-DATA-005", "WARN", f"{dup} feature(s) repeat another feature's geometry and attributes exactly", dup))
    return out


def check_join(left, right, key_left: str, key_right: str | None = None, threshold: float = JOIN_MATCH_MIN, override: bool = False) -> tuple[list[dict], dict]:
    """Join integrity (MAP-DATA-006). Keys are compared as stripped strings so '04013' and 4013 are not silently merged."""
    key_right = key_right or key_left
    for df, k, nm in ((left, key_left, "left"), (right, key_right, "right")):
        if k not in df.columns:
            return [finding("MAP-DATA-006", "BLOCK", f"the {nm} table has no column '{k}'")], {}
    lk = left[key_left].astype(str).str.strip()
    rk = set(right[key_right].astype(str).str.strip())
    matched = lk.isin(rk)
    rate = float(matched.mean()) if len(lk) else 1.0
    unmatched = lk[~matched].unique().tolist()
    stats = {"left_rows": len(left), "matched": int(matched.sum()), "unmatched": int((~matched).sum()), "match_rate": rate,
             "unmatched_keys_sample": unmatched[:20],
             "right_only": len(rk - set(lk))}
    if rate < threshold:
        sev = "WARN" if override else "BLOCK"
        return [finding("MAP-DATA-006", sev, f"only {rate * 100:.1f} percent of rows matched on '{key_left}' ({stats['unmatched']} unmatched; first: {unmatched[:5]}); the threshold is {threshold * 100:.0f} percent"
                        + (" (overridden)" if override else ""), stats)], stats
    if stats["unmatched"]:
        return [finding("MAP-DATA-006", "INFO", f"{stats['unmatched']} row(s) did not match on '{key_left}' (match rate {rate * 100:.1f} percent)", stats)], stats
    return [], stats


def check_encoding(gdf, cpg_found: bool | None = None) -> list[dict]:
    moji = geo_io.mojibake_columns(gdf)
    if not moji:
        return []
    return [finding("MAP-DATA-007", "WARN", "text that looks mis-decoded in: " + ", ".join(f"{c} ({n} values)" for c, n in moji.items())
                    + ". Re-read with the right encoding (a shapefile's .cpg names it)", moji)]


def vertex_count(gdf) -> int:
    import shapely
    return int(shapely.get_num_coordinates(gdf.geometry.values).sum()) if len(gdf) else 0


def check_size(gdf, max_features: int = SIZE_FEATURES_WARN, max_vertices: int = SIZE_VERTICES_WARN) -> list[dict]:
    out = []
    v = vertex_count(gdf)
    if len(gdf) > max_features:
        out.append(finding("MAP-DATA-008", "WARN", f"{len(gdf)} features exceed the renderer guard of {max_features}; aggregate with role: hexbin (or ops grid/hexbin), cluster or filter", len(gdf)))
    if v > max_vertices:
        out.append(finding("MAP-DATA-008", "WARN", f"{v} vertices exceed the renderer guard of {max_vertices}; simplify (the tolerance is disclosed in the map notes)", v))
    return out


def check_antimeridian_polar(gdf, display_crs=None) -> list[dict]:
    """MAP-DATA-009. A geometry part spanning more than 180 degrees of longitude was probably drawn the long way round."""
    import shapely
    out = []
    if gdf.empty or gdf.crs is None:
        return out
    g = gdf if gdf.crs.is_geographic else gdf.to_crs("EPSG:4326")
    ok = ~g.geometry.isna().values & ~g.geometry.is_empty.values
    # Judge each part of a multi-part feature: Alaska has parts on both sides of 180 and is fine; one polygon that spans
    # more than 180 degrees was drawn the long way round.
    parts = shapely.get_parts(g.geometry.values[ok])
    pb = shapely.bounds(parts)
    wide = int(((pb[:, 2] - pb[:, 0]) > 180).sum())
    if wide:
        out.append(finding("MAP-DATA-009", "BLOCK", f"{wide} geometry part(s) span more than 180 degrees of longitude; they cross the antimeridian and would draw across the whole map. Split them at 180 (ops: [{{op: antimeridian_split}}], or geo-convert --antimeridian-split) or centre the projection on the antimeridian", wide))
    b = shapely.bounds(g.geometry.values)
    if display_crs is not None and ce.is_web_mercator(display_crs):
        polar = int((np_abs(b[:, 3][ok]) > 85.06).sum() + (np_abs(b[:, 1][ok]) > 85.06).sum())
        if polar:
            out.append(finding("MAP-DATA-009", "BLOCK", f"{polar} feature edge(s) lie beyond 85.06 degrees latitude, which Web Mercator cannot show", polar))
    return out


def np_abs(a):
    import numpy as np
    return np.abs(a)


def check_zm(gdf) -> list[dict]:
    if not gdf.empty and bool(gdf.geometry.has_z.any()):
        return [finding("MAP-DATA-010", "INFO", "the data carries Z values; they are dropped for mapping")]
    return []


def check_field_mapping(field_map: dict | None) -> list[dict]:
    if field_map:
        return [finding("MAP-DATA-011", "INFO", f"{len(field_map)} field name(s) were shortened for the shapefile (10 characters)", field_map)]
    return []


TERRITORY_OVERLAP_SHARE = 0.01      # two territories overlapping more than this share of the smaller one
TERRITORY_GAP_KM2 = 2.0             # a hole inside the territories' union larger than this is a gap
TERRITORY_DENSITY_FACTOR = 20.0     # a territory this many times sparser (per km2 of its density field) than the median is an outlier


def check_territories(gdf, name_field: str | None = None, density_field: str | None = None) -> list[dict]:
    """1.14.0 (Day 12 issue 14): MAP-DATA-012 (WARN), service territories that do not tile: territories that overlap (each pair over 1 percent
    of the smaller), holes inside the territories' outer edge that no territory covers (over 2 km2: Everett outside Snohomish PUD), and with a
    density field (customers, people), a territory far sparser than the others (a city utility drawn over 6,000 km2 of its county). Measured
    on the ellipsoid's equal-area projection of the layer's own centre. Turned on by the layer's `qa: territories`."""
    import numpy as np
    import shapely
    g = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if len(g) < 2:
        return []
    c = g.to_crs(4326).geometry.union_all().centroid if hasattr(g.geometry, "union_all") else g.to_crs(4326).unary_union.centroid
    laea = f"+proj=laea +lat_0={c.y:.4f} +lon_0={c.x:.4f} +datum=WGS84 +units=m"
    gm = g.to_crs(laea)
    geoms = shapely.make_valid(np.asarray(gm.geometry.values, dtype=object))
    names = [str(v) for v in (g[name_field].tolist() if name_field and name_field in g.columns else [f"row {i}" for i in g.index])]
    area = shapely.area(geoms)
    out = []
    tree = shapely.STRtree(geoms)
    pairs = []
    for i, j in zip(*tree.query(geoms, predicate="intersects"), strict=True):
        if i >= j:
            continue
        ov = shapely.area(shapely.intersection(geoms[i], geoms[j]))
        small = min(area[i], area[j])
        if small > 0 and ov / small > TERRITORY_OVERLAP_SHARE:
            pairs.append((names[i], names[j], ov / 1e6, ov / small))
    if pairs:
        pairs.sort(key=lambda p: -p[2])
        top = "; ".join(f"{a} and {b} ({km:,.0f} km2, {sh:.0%} of the smaller)" for a, b, km, sh in pairs[:4])
        out.append(finding("MAP-DATA-012", "WARN", f"{len(pairs)} pair(s) of territories overlap: {top}; decide which one serves the overlap "
                           "(the smaller wins, say) before mapping a value per territory", [p[:2] for p in pairs[:20]]))
    union = shapely.union_all(geoms)
    holes = []
    for part in getattr(union, "geoms", [union]):
        for ring in getattr(part, "interiors", []):
            h = shapely.Polygon(ring)
            if h.area / 1e6 >= TERRITORY_GAP_KM2:
                holes.append(h)
    if holes:
        from pyproj import Transformer
        back = Transformer.from_crs(laea, "EPSG:4326", always_xy=True)
        where = [back.transform(h.representative_point().x, h.representative_point().y) for h in sorted(holes, key=lambda h: -h.area)[:5]]
        out.append(finding("MAP-DATA-012", "WARN", f"{len(holes)} gap(s) inside the territories that no territory covers (largest "
                           f"{max(h.area for h in holes) / 1e6:,.0f} km2, near {where[0][1]:.3f}, {where[0][0]:.3f}); a territory may be missing a city",
                           [[round(y, 4), round(x, 4)] for x, y in where]))
    if density_field and density_field in g.columns:
        import pandas as pd
        v = pd.to_numeric(g[density_field], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
        dens = v / np.maximum(area / 1e6, 1e-9)
        ok = np.isfinite(dens) & (dens > 0)
        if ok.sum() >= 3:
            med = float(np.median(dens[ok]))
            sparse = [(names[i], dens[i], area[i] / 1e6) for i in np.where(ok)[0] if dens[i] * TERRITORY_DENSITY_FACTOR < med]
            if sparse:
                sparse.sort(key=lambda s: s[1])
                top = "; ".join(f"{n} ({d:,.1f} per km2 over {a:,.0f} km2)" for n, d, a in sparse[:4])
                out.append(finding("MAP-DATA-012", "WARN", f"{len(sparse)} territor{'y is' if len(sparse) == 1 else 'ies are'} far sparser in {density_field} "
                                   f"than the median ({med:,.1f} per km2): {top}; the polygon may cover more than the utility serves", [s[0] for s in sparse[:20]]))
    return out


def lint_data(gdf, *, assumed: bool = False, display_crs=None, null_threshold: float = NULL_RATIO_WARN, id_field: str | None = None) -> list[dict]:
    """The MAP-DATA checks that need only the data. id_field (or the reader's gdf.attrs['id_field'], which gtfs.py sets) names the one key
    column the duplicate check reads; without it every id-like column is checked (a zone or agency id is shared by design, so name the key)."""
    out = []
    out += check_crs_defined(gdf, assumed)
    out += check_validity(gdf)
    out += check_null_geometry(gdf, null_threshold)
    out += check_coordinates(gdf)
    out += check_duplicates(gdf, id_field or (gdf.attrs.get("id_field") if gdf.attrs.get("id_field") in gdf.columns else None))
    out += check_encoding(gdf)
    out += check_size(gdf)
    out += check_antimeridian_polar(gdf, display_crs)
    out += check_zm(gdf)
    return sorted(out, key=lambda f: (SEV_ORDER[f["severity"]], f["id"]))


# --------------------------------------------------------------------------- MAP-CRS

def _span_deg(extent) -> float:
    b = ce._wgs84_bounds(extent)
    return abs(b[2] - b[0])


def lint_crs(display_crs, *, map_type: str | None = None, extent=None, has_tiles: bool = False, user_chosen: bool = False,
             layer_crs: list | None = None, scale_bar: bool = False, rationale: str | None = None,
             datum_transforms_declared: bool = False) -> list[dict]:
    """MAP-CRS-001 to 005 for the display CRS of a map."""
    out = []
    if display_crs is None:
        return [finding("MAP-CRS-005", "WARN", "no display CRS is set")]
    c = ce._crs(display_crs)
    span = _span_deg(extent) if extent is not None else None
    regional_or_larger = span is None or span > REGIONAL_SPAN_DEG
    area_based = (map_type or "") in AREA_BASED_TYPES
    if area_based and not ce.is_equal_area(c):
        if ce.is_web_mercator(c) and regional_or_larger:
            out.append(finding("MAP-CRS-001", "BLOCK", f"a {map_type} map in Web Mercator misstates areas at this extent ({span:.0f} degrees wide); use an equal-area CRS such as EPSG:5070 or Equal Earth" if span else
                               f"a {map_type} map in Web Mercator misstates areas; use an equal-area CRS such as EPSG:5070 or Equal Earth"))
        elif regional_or_larger:
            out.append(finding("MAP-CRS-001", "WARN", f"a {map_type} map uses {c.name}, which is not equal-area; areas are not comparable across the map"))
    if ce.is_web_mercator(c) and not has_tiles and not user_chosen:
        out.append(finding("MAP-CRS-002", "WARN", "Web Mercator is set without a tile basemap and without a user choice; it distorts areas and distances"))
    if scale_bar:
        if c.is_geographic:
            out.append(finding("MAP-CRS-003", "BLOCK", f"a scale bar cannot be valid in {c.name}: its units are degrees, whose length changes with latitude. Use a projected CRS"))
        elif ce.is_web_mercator(c):
            out.append(finding("MAP-CRS-003", "WARN", "in Web Mercator, scale varies with latitude by a factor of 1/cos(latitude); the scale bar must be computed at the map's centre latitude and say so"))
    datums = {ce._crs(x).datum.name for x in (layer_crs or []) if x is not None and ce._crs(x).datum} | ({c.datum.name} if c.datum else set())
    if len(datums) > 1 and not datum_transforms_declared:
        out.append(finding("MAP-CRS-004", "WARN", "layers use different datums (" + "; ".join(sorted(datums)) + ") and no transform is declared; positions can shift by a metre (NAD83/WGS84) or by tens of metres (NAD27)", sorted(datums)))
    if not rationale:
        out.append(finding("MAP-CRS-005", "WARN", "crs.rationale is empty; the choice of projection is not recorded in the MapSpec"))
    return sorted(out, key=lambda f: (SEV_ORDER[f["severity"]], f["id"]))


# --------------------------------------------------------------------------- MAP-CRS-004 / 006 per layer, MAP-CRS-007 across layers

BALLPARK_ERROR_M = 100.0          # a ballpark datum offset leaves the whole shift in place: NAD27 to NAD83 is 10 to 100 m in the US, more in Alaska
VISIBLE_SHIFT_MM = 0.25           # a shift smaller than this on the page cannot be seen
MAP_WIDTH_MM = 250.0              # letter landscape; the planner passes the template's own width


def _span_m(extent) -> float | None:
    if extent is None:
        return None
    b = ce._wgs84_bounds(extent)
    lat = math.radians(max(-89.0, min(89.0, (b[1] + b[3]) / 2)))
    return max(abs(b[2] - b[0]) * 111_320 * math.cos(lat), abs(b[3] - b[1]) * 110_574, 1.0)


def lint_layer_crs(layers, display_crs, *, extent=None, map_width_mm: float = MAP_WIDTH_MM) -> list[dict]:
    """Per layer, before reprojection. `layers` is a list of (layer_id, gdf, assumed).
    MAP-CRS-006: the layer's coordinates fall outside the area its CRS is defined for (a wrong zone, feet as metres, a wrong assumption).
    MAP-CRS-004: the datum transform to the display CRS is a ballpark offset or coarser than 5 m; the severity follows what the shift
    would measure on the page (WARN when it can be seen, INFO when it cannot)."""
    out = []
    span = _span_m(extent)
    for lid, gdf, assumed in layers:
        if gdf is None or gdf.crs is None or gdf.empty:
            continue
        p = ce.area_of_use_problem(gdf)
        if p:
            where = f"lon {p['layer_lonlat'][0]} to {p['layer_lonlat'][2]}, lat {p['layer_lonlat'][1]} to {p['layer_lonlat'][3]}" if p["layer_lonlat"] else "nowhere on Earth"
            area = p["area_bounds"]
            if p["layer_lonlat"] is None:
                centre_out = True
            else:
                cx, cy = (p["layer_lonlat"][0] + p["layer_lonlat"][2]) / 2, (p["layer_lonlat"][1] + p["layer_lonlat"][3]) / 2
                centre_out = not ce._inside((cx, cy, cx, cy), area, ce.AOU_MARGIN_MIN_DEG, ce.AOU_MARGIN_MIN_DEG)
            sev = "WARN" if p["disjoint"] or centre_out or p["oversize"] or p["units_hint"] or assumed else "INFO"
            lead = (f"layer '{lid}' is labelled {p['crs']}, which is defined for {p['area']} (lon {area[0]} to {area[2]}, lat {area[1]} to {area[3]}), "
                    f"but its coordinates read in that CRS land at {where}")
            if sev == "WARN":
                msg = lead + ". It will not line up with the other layers. " + p["hint"][0].upper() + p["hint"][1:] + ("; the CRS was assumed, not read from the file" if assumed else "")
            else:
                msg = lead + ": part of the layer runs past the edge of the CRS's area. Positions are right if the label is right, but distortion grows outside the area"
            out.append({**finding("MAP-CRS-006", sev, msg, p), "layer": lid})
        q = ce.datum_accuracy_problem(gdf.crs, display_crs)
        if q:
            err = BALLPARK_ERROR_M if q["ballpark"] or q["accuracy_m"] is None else float(q["accuracy_m"])
            page_mm = err * map_width_mm / span if span else None
            sev = "WARN" if page_mm is None or page_mm >= VISIBLE_SHIFT_MM else "INFO"
            acc_text = "unknown accuracy" if q["accuracy_m"] is None else "about {:g} m accuracy".format(q["accuracy_m"])
            how = "a ballpark offset (no real datum transform)" if q["ballpark"] else "a transform with " + acc_text
            msg = (f"layer '{lid}' goes from {q['from_datum']} to {q['to_datum']} by {how}, so its positions can be off by up to about {err:g} m"
                   + (f", about {page_mm:.2g} mm on this map" if page_mm is not None else ""))
            if q["better_grids"]:
                msg += (f". A better transform needs the grid file {q['better_grids'][0]}, which is not on this machine: install it with "
                        f"`projsync --file {q['better_grids'][0]}`, or let PROJ fetch grids by setting PROJ_NETWORK=ON")
            if sev == "INFO":
                msg += "; at this scale the shift cannot be seen"
            out.append({**finding("MAP-CRS-004", sev, msg, q), "layer": lid})
    return sorted(out, key=lambda f: (SEV_ORDER[f["severity"]], f["id"]))


ALIGN_TOL_FRAC = 0.02             # bbox tolerance: 2 percent of the diagonal of all layers together
CONTAIN_TOL_FRAC = 0.01           # outline tolerance: 1 percent of the outline's own diagonal
CONTAIN_MIN_AREA_RATIO = 0.5      # an outline is used only when its box is at least half the size of the layer's box
CONTAIN_OUTSIDE_SHIFTED = 0.25    # outside share that warns when the layer's box is displaced from the outline's (one side past it, the other short)
CONTAIN_OUTSIDE_ANY = 0.75        # outside share that warns whatever the boxes look like
SHIFT_MIN_FRAC = 0.10            # a displacement counts when both edges move by more than a tenth of the outline's size
OUTLINE_FILL_MIN = 0.35           # a polygon layer is an outline only when it fills this share of its own box
SAMPLE_MAX = 400


def _bbox(gdf):
    b = [float(v) for v in gdf.total_bounds]
    return b if all(math.isfinite(v) for v in b) else None


def _grow(b, d):
    return [b[0] - d, b[1] - d, b[2] + d, b[3] + d]


def _meets(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _fill_share(gdf, b) -> float:
    """How much of its own box a polygon layer fills. A state or a set of counties fills a good part of it and can serve as an outline;
    forest patches or flood zones leave most of it empty and cannot (features fall between their pieces by nature)."""
    import warnings
    a = (b[2] - b[0]) * (b[3] - b[1])
    if a <= 0:
        return 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                  # area in a geographic CRS: only the ratio is used
        return float(gdf.geometry.area.sum()) / a


def _shift_of(b, o) -> str | None:
    """The direction a layer's box is displaced from an outline's box: past the outline on one side and short of it on the other,
    on the same axis, by more than a tenth of the outline's size (a lon/lat box and a curved outline differ by a few percent in any
    projection). Data clipped to a bigger box encloses the outline and data in one part of it sits inside: neither is a shift."""
    dirs = []
    for lo, hi, neg, pos, size in ((b[0] - o[0], b[2] - o[2], "west", "east", o[2] - o[0]), (b[1] - o[1], b[3] - o[3], "south", "north", o[3] - o[1])):
        tol = SHIFT_MIN_FRAC * size
        if lo > tol and hi > tol:
            dirs.append(pos)
        elif lo < -tol and hi < -tol:
            dirs.append(neg)
    if not dirs:
        return None
    ns = [d for d in dirs if d in ("north", "south")]
    ew = [d for d in dirs if d in ("east", "west")]
    return (ns[0] if ns else "") + (ew[0] if ew else "")


def _samples(gdf, kind: str):
    import numpy as np
    g = gdf.geometry[~gdf.geometry.isna() & ~gdf.geometry.is_empty]
    if kind != "point" or g.geom_type.str.startswith("Multi").any():
        g = g.explode(index_parts=False)                 # one sample per part: a forest held as two multipolygons has thousands of patches
    if len(g) > SAMPLE_MAX:
        g = g.iloc[np.linspace(0, len(g) - 1, SAMPLE_MAX).astype(int)]
    if kind == "point":
        return g.representative_point()
    if kind == "line":
        return g.interpolate(0.5, normalized=True)
    return g.representative_point()


def _units(crs) -> str:
    try:
        return "km" if ce._crs(crs).is_projected else "degrees"
    except Exception:                                     # noqa: BLE001
        return "map units"


WATERY = ("sea", "ocean", "water", "lake", "coast", "shore", "bay", "sound", "river")


def not_an_outline(layer_id: str, role: str | None = None, title: str | None = None) -> bool:
    """1.2.0 (Day 10 issue 7): a context polygon, or one that is water (sea, ocean, lakes), is not an outline other layers should sit inside."""
    if role == "context":
        return True
    import re as _re
    words = set(_re.split(r"[^a-z]+", f"{layer_id} {title or ''}".lower()))
    return any(w in words or (w + "s") in words for w in WATERY)


def lint_alignment(layers, crs, *, skip_outlines=(), bbox_extent: bool = False) -> list[dict]:
    """MAP-CRS-007, after every layer is in the display CRS. `layers` is a list of (layer_id, gdf, kind, assumed) where kind is point, line or polygon.

    Two signs that layers are not registered (one of them carries a CRS label that is not the CRS its numbers were written in):
      * a layer that touches no other layer at all, when the map has two or more;
      * a layer that sits inside an outline layer's box (a state, a county, a market) but whose features mostly fall outside the outline.
    Both are warnings: a layer can lie outside the others on purpose (data clipped to a box, an offshore site). The message says what to check."""
    out = []
    items = [(lid, g, k, a, _bbox(g)) for lid, g, k, a in layers if g is not None and not g.empty]
    items = [it for it in items if it[4] is not None]
    if len(items) < 2:
        return out
    allb = [min(i[4][0] for i in items), min(i[4][1] for i in items), max(i[4][2] for i in items), max(i[4][3] for i in items)]
    tol = ALIGN_TOL_FRAC * math.hypot(allb[2] - allb[0], allb[3] - allb[1])
    unit = _units(crs)
    scale = 1000.0 if unit == "km" else 1.0
    flagged = set()
    for lid, g, k, a, b in items:
        others = [o for o in items if o[0] != lid]
        if any(_meets(_grow(b, tol), o[4]) for o in others):
            continue
        near = min(others, key=lambda o: _gap(b, o[4]))
        d = _gap(b, near[4]) / scale
        flagged.add(lid)
        out.append({**finding("MAP-CRS-007", "WARN",
                              f"layer '{lid}' touches no other layer: it lies about {d:,.0f} {unit} from '{near[0]}', the nearest. "
                              f"If the layers belong together, one of them carries the wrong CRS (zone, units, x/y order"
                              + (", or the CRS that was assumed for it" if a or near[3] else "") + "); check the crs: of each against its source",
                              {"nearest": near[0], "gap": round(d, 1), "unit": unit}), "layer": lid})
    skip = set(skip_outlines or ())
    outlines = [o for o in items if o[2] == "polygon" and o[0] not in skip and _fill_share(o[1], o[4]) >= OUTLINE_FILL_MIN]
    for lid, g, k, a, b in items:
        if lid in flagged:
            continue
        area_b = max((b[2] - b[0]) * (b[3] - b[1]), 0.0)
        cands = [o for o in outlines if o[0] != lid and o[0] not in flagged and _meets(_grow(b, tol), o[4])
                 and (o[4][2] - o[4][0]) * (o[4][3] - o[4][1]) >= CONTAIN_MIN_AREA_RATIO * area_b]
        if not cands:
            continue
        o = min(cands, key=lambda c: (c[4][2] - c[4][0]) * (c[4][3] - c[4][1]))
        otol = CONTAIN_TOL_FRAC * math.hypot(o[4][2] - o[4][0], o[4][3] - o[4][1])
        shift = _shift_of(b, o[4])
        pts = _samples(g, k)
        if not len(pts):
            continue
        og = o[1][~o[1].geometry.isna() & ~o[1].geometry.is_empty]
        try:
            hit = og.sindex.query(pts.values, predicate="dwithin", distance=otol)
        except Exception:                                 # noqa: BLE001  (an older spatial index without dwithin)
            hit = og.sindex.query(pts.buffer(otol).values, predicate="intersects")
        share_out = 1 - len(set(hit[0].tolist())) / len(pts)
        if share_out > CONTAIN_OUTSIDE_ANY or (shift and share_out > CONTAIN_OUTSIDE_SHIFTED):
            moved = f" Its features sit {shift} of where '{o[0]}' is." if shift else ""
            # 1.2.0 (Day 4 issue 15): with an explicit bbox extent, data clipped to the box is expected to cross an outline: INFO
            out.append({**finding("MAP-CRS-007", "INFO" if bbox_extent and not shift else "WARN",
                                  f"{share_out:.0%} of layer '{lid}' falls outside '{o[0]}', the outline it overlaps.{moved} "
                                  f"If '{lid}' should lie inside '{o[0]}', the two are not registered: one carries the wrong CRS (zone, units, x/y order) or was shifted"
                                  + (" (a CRS was assumed for one of them)" if a or o[3] else "")
                                  + ". If it extends past the outline on purpose, this is expected; crs: {alignment_check: false} turns the check off",
                                  {"outline": o[0], "outside_share": round(share_out, 3), "sampled": int(len(pts)), "shift": shift}), "layer": lid})
    return out


# --------------------------------------------------------------------------- MAP-OUT-005 / 006 / 007

# formats whose attribute types are not kept (everything comes back as text or a loose type)
LOOSE_TYPES = {"csv", "gpx", "kml", "kmz"}
# formats that move coordinates (quantization)
QUANTIZED = {"topojson"}
GEOM_ALIASES = {"Polygon": "Polygon", "MultiPolygon": "Polygon", "LineString": "LineString", "MultiLineString": "LineString",
                "Point": "Point", "MultiPoint": "Point", "MultiLineStringZ": "LineString"}


def _family(g) -> str:
    return GEOM_ALIASES.get(g.geom_type, g.geom_type)


def _dtype_kind(s) -> str:
    k = s.dtype.kind
    return {"i": "int", "u": "int", "f": "float", "b": "bool", "O": "text", "U": "text", "M": "datetime"}.get(k, k)


def round_trip(before, after, *, fmt: str, field_map: dict | None = None, geom_tolerance: float | None = None,
               dropped_fields: list | None = None, crs_forced_wgs84: bool = False) -> list[dict]:
    """MAP-OUT-005: the data read back from a written file matches what was written.

    Compares feature count, geometry family, CRS, attribute names (through the shapefile mapping), attribute types
    (loosely for formats that do not keep them), values of numeric fields, and geometry within a tolerance.
    """
    import numpy as np
    import shapely
    out = []
    field_map = field_map or {}
    if len(before) != len(after):
        out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: {len(before)} features were written but {len(after)} were read back"))
        return out
    # CRS
    exp_crs = ce._crs("EPSG:4326") if crs_forced_wgs84 and before.crs is not None else before.crs
    if exp_crs is not None and after.crs is None:
        out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: the CRS ({exp_crs.to_string()}) was lost"))
    elif exp_crs is not None and after.crs is not None and not exp_crs.equals(after.crs):
        out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: the CRS changed from {exp_crs.to_string()} to {after.crs.to_string()}"))
    # attributes
    gb, ga = _geom_col(before), _geom_col(after)
    names_b = [c for c in before.columns if c != gb and c not in (dropped_fields or [])]
    expected = [field_map.get(c, c) for c in names_b]
    names_a = [c for c in after.columns if c != ga]
    missing = [c for c in expected if c not in names_a]
    extra = [c for c in names_a if c not in expected]
    if missing:
        out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: attribute(s) lost: {', '.join(missing)}", missing))
    if extra:
        out.append(finding("MAP-OUT-005", "WARN", f"{fmt}: attribute(s) added by the format: {', '.join(extra)}", extra))
    for c in names_b:
        w = field_map.get(c, c)
        if w not in names_a:
            continue
        kb, ka = _dtype_kind(before[c]), _dtype_kind(after[w])
        if kb != ka and fmt not in LOOSE_TYPES and not (kb == "int" and ka == "float" and fmt == "geojson"):
            out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: type of '{c}' changed from {kb} to {ka}"))
        elif kb in ("int", "float") and ka in ("int", "float", "text"):
            try:
                x = before[c].astype(float).to_numpy()
                y = after[w].astype(float).to_numpy()
                if not np.allclose(np.nan_to_num(x, nan=-9e99), np.nan_to_num(y, nan=-9e99), rtol=1e-9, atol=1e-9):
                    out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: values of '{c}' changed"))
            except (ValueError, TypeError):
                out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: values of '{c}' could not be read back as numbers"))
        elif kb == "text" and ka == "text":
            xb = before[c].fillna("").astype(str).tolist()
            xa = after[w].fillna("").astype(str).tolist()
            if xb != xa:
                n = sum(1 for p, q in zip(xb, xa) if p != q)
                out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: {n} text value(s) of '{c}' changed (encoding?)"))
    # geometry
    fb = [_family(g) for g in before.geometry if g is not None]
    fa = [_family(g) for g in after.geometry if g is not None]
    if sorted(set(fb)) != sorted(set(fa)):
        out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: geometry type changed from {sorted(set(fb))} to {sorted(set(fa))}"))
    else:
        tb = before.to_crs(after.crs) if (before.crs is not None and after.crs is not None and not before.crs.equals(after.crs)) else before
        tol = geom_tolerance
        if tol is None:
            minx, miny, maxx, maxy = (float(v) for v in tb.total_bounds) if len(tb) else (0, 0, 1, 1)
            diag = math.hypot(maxx - minx, maxy - miny) or 1.0
            tol = diag / 1e4 if fmt in QUANTIZED else (1e-7 if (after.crs is not None and after.crs.is_geographic) else 1e-3)
        worst_d = 0.0
        for g1, g2 in zip(tb.geometry, after.geometry, strict=True):
            if g1 is None or g2 is None:
                continue
            worst_d = max(worst_d, float(shapely.hausdorff_distance(g1, g2)))
        if worst_d > tol:
            out.append(finding("MAP-OUT-005", "BLOCK", f"{fmt}: geometry moved by up to {worst_d:.3g} (tolerance {tol:.3g})"))
    return out


def check_geojson_rfc7946(path) -> list[dict]:
    """MAP-OUT-007: RFC 7946 conformance of a GeoJSON file: WGS84, winding order, no NaN, no crs member."""
    out = []
    p = Path(path)

    def refuse(tok):
        raise ValueError(tok)

    try:
        doc = json.loads(p.read_text(encoding="utf-8"), parse_constant=refuse)
    except ValueError as e:
        return [finding("MAP-OUT-007", "BLOCK", f"not valid JSON or contains NaN/Infinity ({e})")]
    if not isinstance(doc, dict) or "type" not in doc:
        return [finding("MAP-OUT-007", "BLOCK", "not a GeoJSON object")]
    crs = doc.get("crs")
    if crs:
        name = (crs.get("properties") or {}).get("name", "") if isinstance(crs, dict) else ""
        if not re.search(r"(CRS84|EPSG::?4326)$", str(name)):
            out.append(finding("MAP-OUT-007", "BLOCK", f"a crs member names {name or 'something'}; RFC 7946 allows only WGS84"))
        else:
            out.append(finding("MAP-OUT-007", "WARN", "a crs member is present; RFC 7946 removed it (WGS84 is implied)"))

    def ring_area(r):
        return sum((r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]) for i in range(len(r) - 1)) / 2

    bad_pos = bad_wind = jumps = 0

    def pos_ok(c):
        return isinstance(c, list) and len(c) >= 2 and -180 <= c[0] <= 180 and -90 <= c[1] <= 90

    def walk_geom(g):
        nonlocal bad_pos, bad_wind, jumps
        if not isinstance(g, dict):
            return
        t, co = g.get("type"), g.get("coordinates")
        if t == "GeometryCollection":
            for x in g.get("geometries", []):
                walk_geom(x)
            return

        def lines(c, depth):
            nonlocal bad_pos
            if depth == 0:
                if not pos_ok(c):
                    bad_pos += 1
                return
            for x in c:
                lines(x, depth - 1)

        depth = {"Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2, "Polygon": 2, "MultiPolygon": 3}.get(t)
        if depth is None or co is None:
            return
        lines(co, depth)
        polys = [co] if t == "Polygon" else co if t == "MultiPolygon" else []
        for poly in polys:
            for i, ring in enumerate(poly):
                if len(ring) >= 4 and all(pos_ok(c) for c in ring):
                    a = ring_area(ring)
                    if (i == 0 and a < 0) or (i > 0 and a > 0):
                        bad_wind += 1
        segs = [co] if t == "LineString" else co if t == "MultiLineString" else [r for poly in polys for r in poly]
        for s in segs:
            for i in range(len(s) - 1):
                if pos_ok(s[i]) and pos_ok(s[i + 1]) and abs(s[i][0] - s[i + 1][0]) > 180:
                    jumps += 1

    feats = doc.get("features", []) if doc["type"] == "FeatureCollection" else [doc] if doc["type"] == "Feature" else [{"geometry": doc}]
    for f in feats:
        walk_geom(f.get("geometry"))
    if bad_pos:
        out.append(finding("MAP-OUT-007", "BLOCK", f"{bad_pos} position(s) are outside WGS84 longitude/latitude range or malformed", bad_pos))
    if bad_wind:
        out.append(finding("MAP-OUT-007", "BLOCK", f"{bad_wind} polygon ring(s) break the right-hand rule (exterior counter-clockwise, holes clockwise)", bad_wind))
    if jumps:
        out.append(finding("MAP-OUT-007", "WARN", f"{jumps} segment(s) jump more than 180 degrees of longitude; cut them at the antimeridian (RFC 7946 section 3.1.9)", jumps))
    return out


def check_kml(path) -> list[dict]:
    """MAP-OUT-006: a KML (or the doc.kml of a KMZ) is well-formed, in the KML 2.2 namespace, with lon,lat coordinates in range."""
    p = Path(path)
    out = []
    try:
        from lxml import etree
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        if p.suffix.lower() == ".kmz":
            import zipfile
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not names:
                    return [finding("MAP-OUT-006", "WARN", "the KMZ holds no .kml file")]
                data = zf.read("doc.kml" if "doc.kml" in names else names[0])
            root = etree.fromstring(data, parser)
        else:
            root = etree.fromstring(p.read_bytes(), parser)
    except Exception as e:
        return [finding("MAP-OUT-006", "WARN", f"not well-formed KML: {str(e)[:120]}")]
    ns = root.nsmap.get(None, "")
    if not root.tag.endswith("}kml") and root.tag != "kml":
        out.append(finding("MAP-OUT-006", "WARN", "the root element is not <kml>"))
    if ns and "opengis.net/kml/2" not in ns:
        out.append(finding("MAP-OUT-006", "WARN", f"unexpected KML namespace {ns}"))
    bad = 0
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.endswith("}coordinates"):
            for tup in (el.text or "").split():
                v = tup.split(",")
                try:
                    lon, lat = float(v[0]), float(v[1])
                    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                        bad += 1
                except (ValueError, IndexError):
                    bad += 1
    if bad:
        out.append(finding("MAP-OUT-006", "WARN", f"{bad} coordinate tuple(s) are malformed or out of range (KML order is longitude,latitude)", bad))
    return out


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MAP-DATA lint for one geodata file")
    ap.add_argument("path")
    ap.add_argument("--layer")
    ap.add_argument("--crs", help="CRS to assume if the file has none")
    ap.add_argument("--threshold-null", type=float, default=NULL_RATIO_WARN)
    a = ap.parse_args(argv)
    try:
        r = geo_io.read(a.path, layer=a.layer, crs_assume=a.crs)
    except geo_io.GeoIOError as e:
        print(f"data-lint: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    findings = lint_data(r.gdf, assumed=r.crs_assumed, null_threshold=a.threshold_null)
    print(json.dumps({"file": Path(a.path).name, "layer": r.layer, "features": len(r.gdf), "findings": findings,
                      "io_notes": [n.as_dict() for n in r.notes]}, indent=2, default=str))
    return worst(findings)


if __name__ == "__main__":
    sys.exit(main())
