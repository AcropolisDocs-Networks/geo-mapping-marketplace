#!/usr/bin/env python3
"""vector_ground: a person's own vector tile file as the ground of a printed map (1.10.0).

    import vector_ground as vg
    info = vg.inspect("region.pmtiles")              # {format, schema, layers, minzoom, maxzoom, bounds, attribution}
    g = vg.read("region.pmtiles", window_lonlat, scale_denominator)
    g["land"], g["water"]                            # polygons, Web Mercator (EPSG:3857), each one merged shape
    g["rivers"], g["roads"], g["ferries"], g["urban"], g["boundaries"]
    g["zoom"], g["schema"], g["notes"]

A vector tile file (PMTiles v3 or MBTiles, tiles in Mapbox Vector Tile format) holds the same things the bundled ground does, at far finer
detail: land, water, roads, built-up areas, borders. The web page has drawn such a file since M14; this module reads one for a printed map,
so the same file is the ground on paper and on screen. The file is read through GDAL (pyogrio), for the map's window only, at the one zoom
that suits the map's scale. Nothing is fetched: the file is the person's own, on their disk.

Three tile schemas are known, told apart by their layer names:

    protomaps      earth, water, roads, boundaries, landuse / landcover        (Protomaps basemaps, schema v3 and v4)
    openmaptiles   water, waterway, transportation, boundary, landuse          (OpenMapTiles, MapTiler, Planetiler's default profile)
    shortbread     ocean, water_polygons, water_lines, streets, boundaries     (Shortbread: Geofabrik, VersaTiles)

OpenMapTiles and Shortbread carry no land layer (the land is whatever is not water), so their land is the window with the water cut out.
A file of another schema is refused with the layer names it holds; the bundled ground is drawn instead.

What is taken from the file, and what it becomes on the map (the colours are the brand's map roles, never the file's own style):

    land          the land fill
    water         sea, lakes and wide rivers, in the water colour
    rivers        river and canal centre lines (streams and ditches are left out)
    roads         two classes: 'major' (motorways and trunk roads) and 'secondary' (primary and secondary roads); with the file's
                  smaller roads as a third class, 'minor', on maps at 1:150,000 and larger
    road names    (1.12.0) the names of the major and secondary roads, set along them, on maps at 1:150,000 and larger
    places        (1.12.0) the file's cities, towns and villages as the map's place names (the bundled gazetteer names nothing then)
    buildings     (1.12.0) building footprints, on maps at 1:25,000 and larger
    ferries       ferry routes, dashed
    urban         built-up land (residential, commercial, industrial, retail)
    boundaries    country lines, and first-level lines (states, provinces) as a second class; maritime lines are left out

Tiles cut every feature at tile edges. Polygons are merged back into one shape per kind and lines into one geometry per class, so a
see-through fill or stroke does not darken where two tiles meet.
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WEB_MERCATOR = "EPSG:3857"
MERC_LAT = 85.0511287798
# The scale of a vector tile drawn at 512 screen pixels a tile (the size vector tiles are styled for), 0.28 mm a pixel, zoom 0, equator.
ZOOM0_DENOMINATOR = 279_541_132.0
MAX_TILES = 400                               # the window is read at a lower zoom when it would span more tiles than this
MINOR_ROADS_MAX_DENOMINATOR = 150_000
BUILDINGS_MAX_DENOMINATOR = 25_000            # 1.12.0: building footprints are drawn on maps at 1:25,000 and larger
ROAD_NAMES_MAX_DENOMINATOR = 150_000          # ...and road names on maps at 1:150,000 and larger
PLACE_POP = {"city": 200_000, "town": 20_000, "village": 2_000, "hamlet": 300, "suburb": 5_000, "neighbourhood": 1_000, "locality": 20_000,
             "capital": 1_000_000, "state_capital": 300_000}       # a population for a place the file gives none for, by its kind
# schema -> (layer, the attribute that says what kind of place it is, the kinds kept, name attributes in order, population, capital test)
PLACES = {
    "protomaps": ("places", ("kind_detail", "pmap:kind_detail", "kind"), ("city", "town", "village", "locality", "capital", "state_capital"), ("name",),
                  "population", lambda r: str(r.get("capital") or "") in ("yes", "2") or str(r.get("kind_detail") or "") == "capital"),
    "openmaptiles": ("place", ("class",), ("city", "town", "village"), ("name", "name:latin", "name_en"), None, lambda r: str(r.get("capital") or "") in ("2", "2.0")),
    "shortbread": ("place_labels", ("kind",), ("capital", "state_capital", "city", "town", "village"), ("name",), "population", lambda r: str(r.get("kind")) == "capital"),
}
BUILDINGS = {"protomaps": "buildings", "openmaptiles": "building", "shortbread": "buildings"}
ROAD_NAME_FIELDS = ("name", "ref")
URBAN_KINDS = ("residential", "commercial", "industrial", "retail", "urban_area", "urban", "suburb", "neighbourhood")
RIVER_KINDS = ("river", "canal")

# schema -> {what: (layer, attribute, {value: class}) or a list of them}. A class of None keeps every feature of the layer.
SCHEMAS: dict[str, dict] = {
    "protomaps": {
        "detect": ("earth", "water"),
        "land": [("earth", None, None)],
        "water": [("water", None, None)],                       # v4 keeps river lines in `water` too; v3 has them in physical_line
        "rivers": [("water", "kind", RIVER_KINDS), ("physical_line", "kind", RIVER_KINDS)],
        "roads": ("roads", ("kind", "pmap:kind"), {"highway": "major", "major_road": "secondary", "medium_road": "secondary", "minor_road": "minor",
                                                   "other": "minor", "ferry": "ferry"}),
        "urban": [("landuse", ("kind", "pmap:kind"), URBAN_KINDS), ("landcover", "kind", URBAN_KINDS)],
        "boundaries": ("boundaries", ("kind", "pmap:kind"), {"country": "country", "region": "region"}),
    },
    "openmaptiles": {
        "detect": ("water", "transportation"),
        "land": None,
        "water": [("water", None, None)],
        "rivers": [("waterway", "class", RIVER_KINDS)],
        "roads": ("transportation", ("class",), {"motorway": "major", "trunk": "major", "primary": "secondary", "secondary": "secondary",
                                                 "tertiary": "minor", "minor": "minor", "ferry": "ferry"}),
        "urban": [("landuse", "class", URBAN_KINDS)],
        "boundaries": ("boundary", ("admin_level",), {"2": "country", "3": "region", "4": "region"}),
    },
    "shortbread": {
        "detect": ("ocean", "streets"),
        "land": None,
        "water": [("ocean", None, None), ("water_polygons", None, None)],
        "rivers": [("water_lines", "kind", RIVER_KINDS)],
        "roads": ("streets", ("kind",), {"motorway": "major", "trunk": "major", "primary": "secondary", "secondary": "secondary",
                                         "tertiary": "minor", "unclassified": "minor", "residential": "minor"}),
        "ferries": ("ferries", None, None),
        "urban": [("land", "kind", URBAN_KINDS)],
        "boundaries": ("boundaries", ("admin_level",), {"2": "country", "4": "region"}),
    },
}


class VectorGroundError(Exception):
    pass


def _mbtiles_info(p: Path) -> dict:
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("select name, value from metadata").fetchall())
        finally:
            con.close()
    except sqlite3.Error as ex:
        raise VectorGroundError(f"{p.name} is not an MBTiles file ({str(ex)[:80]})") from None
    if str(meta.get("format") or "").lower() != "pbf":
        raise VectorGroundError(f"{p.name} holds picture tiles ({meta.get('format') or 'no format named'}), not vector tiles; "
                                "a picture tile set is a tile basemap (basemap-render)")
    try:
        bounds = [float(v) for v in str(meta.get("bounds") or "").split(",")]
    except ValueError:
        bounds = []
    return {"format": "mbtiles", "minzoom": int(meta.get("minzoom") or 0), "maxzoom": int(meta.get("maxzoom") or 14),
            "bounds": bounds if len(bounds) == 4 else [-180.0, -MERC_LAT, 180.0, MERC_LAT], "attribution": meta.get("attribution"), "name": meta.get("name")}


def _pmtiles_info(p: Path) -> dict:
    import tiles_out as to
    try:
        with open(p, "rb") as fh:
            h = to._pmtiles_header(fh.read(127))
        meta = to.pmtiles_metadata(p)
    except (OSError, to.TilesError, ValueError) as ex:
        raise VectorGroundError(f"{p.name} could not be read as a PMTiles file ({str(ex)[:80]})") from None
    if h.get("tile_type") != 1:
        raise VectorGroundError(f"{p.name} holds picture tiles, not vector tiles; a picture tile set is a tile basemap (basemap-render)")
    return {"format": "pmtiles", "minzoom": int(h["min_zoom"]), "maxzoom": int(h["max_zoom"]), "bounds": [float(v) for v in h["bounds"]],
            "attribution": meta.get("attribution"), "name": meta.get("name")}


def schema_of(layers) -> str | None:
    have = set(layers)
    for name, s in SCHEMAS.items():
        if all(x in have for x in s["detect"]):
            return name
    return None


def inspect(path) -> dict:
    """What the file is: its container, zoom range, bounds (lon/lat), layer names, the schema they make and the file's own credit."""
    import pyogrio
    p = Path(path)
    if not p.is_file():
        raise VectorGroundError(f"the basemap file {p.name} was not found")
    with open(p, "rb") as fh:
        magic = fh.read(16)
    info = _pmtiles_info(p) if magic[:7] == b"PMTiles" else _mbtiles_info(p) if magic[:15] == b"SQLite format 3" else None
    if info is None:
        raise VectorGroundError(f"{p.name} is neither a PMTiles nor an MBTiles file")
    try:
        info["layers"] = [str(x[0]) for x in pyogrio.list_layers(p)]
    except Exception as ex:  # noqa: BLE001 - GDAL's own message is the reason
        raise VectorGroundError(f"{p.name} could not be opened ({str(ex)[:120]})") from None
    info["schema"] = schema_of(info["layers"])
    info["path"] = str(p)
    return info


def plain_credit(text: str | None) -> str | None:
    """The file's attribution as plain text (tile sets carry it as HTML links)."""
    if not text:
        return None
    import html
    import re
    s = html.unescape(re.sub(r"<[^>]+>", "", str(text)))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def zoom_for(denominator: float, lat: float, minzoom: int, maxzoom: int) -> int:
    """The zoom whose tiles were made for this print scale at this latitude, within the file's range. Three quarters of the way to the next
    zoom is rounded up: paper shows more detail than a screen does."""
    den = max(1.0, float(denominator))
    z = math.log2(ZOOM0_DENOMINATOR * max(0.05, math.cos(math.radians(lat))) / den)
    return max(int(minzoom), min(int(maxzoom), int(math.floor(z + 0.75))))


def _merc_window(window) -> tuple:
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", WEB_MERCATOR, always_xy=True)
    x0, y0 = t.transform(max(-180.0, window[0]), max(-MERC_LAT, window[1]))
    x1, y1 = t.transform(min(180.0, window[2]), min(MERC_LAT, window[3]))
    return (x0, y0, x1, y1)


def tiles_in(bbox3857, z: int) -> int:
    side = 2 * 20037508.342789244 / 2 ** z
    return (int((bbox3857[2] - bbox3857[0]) // side) + 1) * (int((bbox3857[3] - bbox3857[1]) // side) + 1)


def _read(path, layer: str, z: int, bbox, have) -> object | None:
    if layer not in have:
        return None
    import pyogrio
    try:
        df = pyogrio.read_dataframe(path, layer=layer, ZOOM_LEVEL=str(z), bbox=bbox)
    except Exception:  # noqa: BLE001 - a layer with no tile at this zoom reads as nothing
        return None
    if df is None or not len(df):
        return None
    df = df[~df.geometry.isna() & ~df.geometry.is_empty]
    return df if len(df) else None


def _col(df, names):
    for n in ((names,) if isinstance(names, str) else names or ()):
        if n in df.columns:
            return df[n].astype(str).str.replace(r"\.0$", "", regex=True)
    return None


def _parts(df, dims: tuple):
    """The geometries of a layer that are of the wanted dimension (1 lines, 2 polygons); a layer can hold both (Protomaps `water`)."""
    import shapely
    g = df.geometry.values
    return g[[shapely.get_dimensions(x) in dims for x in g]]


def _merge(geoms, box_, lines: bool):
    import shapely
    if geoms is None or not len(geoms):
        return None
    g = shapely.intersection(shapely.make_valid(geoms), box_)
    u = shapely.union_all(g)
    if lines:
        u = shapely.line_merge(u) if u.geom_type == "MultiLineString" else u
    else:
        u = shapely.make_valid(u)
        if u.geom_type == "GeometryCollection":
            u = shapely.union_all([x for x in u.geoms if x.geom_type in ("Polygon", "MultiPolygon")])
    return None if u.is_empty else u


def read(path, window, denominator: float, *, zoom: int | None = None) -> dict:
    """The ground in the file for a lon/lat window (x0, y0, x1, y1) at a print scale. Returns Web Mercator geometry: one merged shape per
    kind, and {class: geometry} for roads and boundaries. Raises VectorGroundError when the file is not a vector tile set of a known schema
    or does not reach the window."""
    import shapely
    from shapely.geometry import box
    info = inspect(path)
    if info["schema"] is None:
        raise VectorGroundError(f"{Path(path).name} is not in a tile schema this plugin reads for print (Protomaps, OpenMapTiles, Shortbread); "
                                f"its layers are {', '.join(info['layers'][:12]) or 'none'}")
    s = SCHEMAS[info["schema"]]
    b = info["bounds"]
    if not (b[0] <= window[2] and b[2] >= window[0] and b[1] <= window[3] and b[3] >= window[1]):
        raise VectorGroundError(f"{Path(path).name} covers {b[0]:.2f},{b[1]:.2f} to {b[2]:.2f},{b[3]:.2f}; the map is somewhere else")
    bbox = _merc_window(window)
    lat = (max(-MERC_LAT, window[1]) + min(MERC_LAT, window[3])) / 2
    notes: list[str] = []
    z = zoom_for(denominator, lat, info["minzoom"], info["maxzoom"]) if zoom is None else max(info["minzoom"], min(info["maxzoom"], int(zoom)))
    want = z
    while z > info["minzoom"] and tiles_in(bbox, z) > MAX_TILES:
        z -= 1
    if z < want:
        notes.append(f"read at zoom {z}, not {want}: the window spans more than {MAX_TILES} tiles at zoom {want}")
    have = set(info["layers"])
    win = box(*bbox)
    out: dict = {"schema": info["schema"], "zoom": z, "format": info["format"], "bounds": b, "attribution": plain_credit(info.get("attribution")),
                 "notes": notes, "crs": WEB_MERCATOR, "window_3857": bbox, "file": Path(path).name}

    def polys(defs):
        acc = []
        for layer, attr, values in defs or []:
            df = _read(path, layer, z, bbox, have)
            if df is None:
                continue
            if values is not None:
                c = _col(df, attr)
                if c is None:
                    continue
                df = df[c.isin(values).values]
            acc += list(_parts(df, (2,)))
        return _merge(shapely.from_wkb(shapely.to_wkb(acc)) if acc else None, win, False)

    def lines(defs):
        acc = []
        for layer, attr, values in defs or []:
            df = _read(path, layer, z, bbox, have)
            if df is None:
                continue
            c = _col(df, attr) if values is not None else None
            if values is not None and c is None:
                continue
            if c is not None:
                df = df[c.isin(values).values]
            acc += list(_parts(df, (1,)))
        return _merge(shapely.from_wkb(shapely.to_wkb(acc)) if acc else None, win, True)

    def classed(d):
        if not d:
            return {}
        layer, attr, table = d
        df = _read(path, layer, z, bbox, have)
        if df is None:
            return {}
        if table is None:
            m = _merge(_parts(df, (1,)), win, True)
            return {"ferry": m} if m is not None else {}
        c = _col(df, attr)
        if c is None:
            return {}
        if "maritime" in df.columns:                                 # a border drawn across the sea is not a line on the ground
            sea = df["maritime"].astype(str).isin(("1", "True", "true")).values
            df, c = df[~sea], c[~sea]
        res = {}
        for cls in dict.fromkeys(table.values()):
            keys = [k for k, v in table.items() if v == cls]
            m = _merge(_parts(df[c.isin(keys).values], (1,)), win, True)
            if m is not None:
                res[cls] = m
        return res

    water = polys(s["water"])
    out["water"] = water
    if s["land"]:
        out["land"] = polys(s["land"])
    else:
        out["land"] = win if water is None else shapely.make_valid(win.difference(water))
    if out["land"] is None or out["land"].is_empty:
        out["land"] = None
    out["rivers"] = lines(s["rivers"])
    roads = classed(s["roads"])
    ferries = classed(s.get("ferries"))
    if "ferry" in roads or ferries:
        out["ferries"] = roads.pop("ferry", None) or ferries.get("ferry")
    else:
        out["ferries"] = None
    if denominator > MINOR_ROADS_MAX_DENOMINATOR:
        roads.pop("minor", None)
    out["roads"] = roads
    out["urban"] = polys(s["urban"])
    out["boundaries"] = classed(s["boundaries"])
    # 1.12.0: the file's own place names, its building footprints on large-scale maps, and the names of its larger roads
    out["places"] = _places(path, info["schema"], z, bbox, have)
    out["buildings"] = polys([(BUILDINGS[info["schema"]], None, None)]) if denominator <= BUILDINGS_MAX_DENOMINATOR else None
    out["road_names"] = _road_names(path, s["roads"], z, bbox, have, win) if denominator <= ROAD_NAMES_MAX_DENOMINATOR else None
    return out


def _places(path, schema: str, z: int, bbox, have):
    """The file's places as a gazetteer table (lon/lat points): name, pop_max, scalerank (from the population), adm0cap. One row per place:
    a place near a tile edge is in two tiles."""
    import geopandas as gpd
    layer, kind_attrs, kinds, name_attrs, pop_attr, is_cap = PLACES[schema]
    df = _read(path, layer, z, bbox, have)
    if df is None:
        return None
    kc = _col(df, kind_attrs)
    if kc is None:
        return None
    df = df[kc.isin(kinds).values].copy()
    kc = kc[kc.isin(kinds).values]
    if not len(df):
        return None
    rows = []
    seen = set()
    geoms = df.geometry.values
    for i in range(len(df)):
        r = df.iloc[i]
        name = next((str(r[a]) for a in name_attrs if a in df.columns and isinstance(r[a], str) and r[a].strip()), None)
        g = geoms[i]
        pt = g.geoms[0] if hasattr(g, "geoms") and len(g.geoms) else g
        if not name or pt.is_empty or pt.geom_type != "Point":
            continue
        key = (name, round(pt.x / 200.0), round(pt.y / 200.0))
        if key in seen:
            continue
        seen.add(key)
        kind = str(kc.iloc[i])
        try:
            pop = float(r[pop_attr]) if pop_attr and pop_attr in df.columns and r[pop_attr] == r[pop_attr] and r[pop_attr] is not None else 0.0
        except (TypeError, ValueError):
            pop = 0.0
        pop = pop if pop > 0 else float(PLACE_POP.get(kind, 2_000))
        cap = bool(is_cap(r))
        rank = next((k for k, lim in enumerate((5e6, 2e6, 1e6, 5e5, 2e5, 1e5, 5e4, 2e4, 1e4, 5e3)) if pop >= lim), 10)
        rows.append({"name": name, "pop_max": pop, "scalerank": 0 if cap else rank, "adm0cap": 1 if cap else 0, "featurecla": kind, "geometry": pt})
    if not rows:
        return None
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs=WEB_MERCATOR).to_crs("EPSG:4326")
    return g.sort_values("pop_max", ascending=False, kind="stable").reset_index(drop=True)


def _road_names(path, roads_def, z: int, bbox, have, win):
    """The named major and secondary roads of the file: one line geometry per name (its tile pieces merged), with the name and the class."""
    import geopandas as gpd
    import shapely
    layer, attr, table = roads_def
    df = _read(path, layer, z, bbox, have)
    if df is None or table is None:
        return None
    c = _col(df, attr)
    field = next((f for f in ROAD_NAME_FIELDS if f in df.columns), None)
    if c is None or field is None:
        return None
    keep = c.map(table).isin(("major", "secondary")).values
    df, c = df[keep], c[keep]
    names = df[field]
    ok = [isinstance(v, str) and bool(v.strip()) for v in names]
    df, c = df[ok], c[ok]
    if not len(df):
        return None
    rows = []
    for name, part in df.groupby(df[field].astype(str).str.strip()):
        m = _merge(_parts(part, (1,)), win, True)
        if m is not None and m.length > 0:
            cls = "major" if (c.loc[part.index].map(table) == "major").any() else "secondary"
            rows.append({"name": name, "class": cls, "length": float(shapely.length(m)), "geometry": m})
    if not rows:
        return None
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs=WEB_MERCATOR)
    return g.sort_values("length", ascending=False, kind="stable").head(80).reset_index(drop=True)


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Say what a vector tile file holds and whether it can be the ground of a printed map.")
    ap.add_argument("file")
    a = ap.parse_args(argv)
    try:
        info = inspect(a.file)
    except VectorGroundError as ex:
        print(str(ex), file=sys.stderr)
        return 2
    info["print_ground"] = info["schema"] is not None
    info["attribution"] = plain_credit(info.get("attribution"))
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
