#!/usr/bin/env python3
"""geo-io: the single read/write layer for vector geodata (spec 7, 8).

Library use:   import geo_io; r = geo_io.read(path); geo_io.write(r.gdf, out)
Command line:  geo_io.py formats
               geo_io.py layers PATH
               geo_io.py command SRC DST [--t-srs EPSG:n] [--where SQL] [--select a,b] [--bbox x0 y0 x1 y1] [--simplify TOL]

What it does (and what the callers rely on):
  * one reader and one writer for GeoJSON, Shapefile (.shp or a .zip), KML, KMZ, GeoPackage, CSV (lat/lon or WKT),
    GPX, TopoJSON, FlatGeobuf and GeoParquet (spec 8);
  * a .zip is read by what it holds (a shapefile, several shapefiles, a GeoJSON, a GeoPackage, a CSV), not by its name; several
    shapefiles are listed as layers and `layer=` picks one;
  * a .json is sniffed: GeoJSON, TopoJSON (a "Topology") or Esri JSON (features with "attributes") are each read the right way;
  * CSV: the delimiter is sniffed (comma, semicolon, tab, pipe; .tsv and .txt are accepted), degree-minute-second coordinates are
    parsed with a note, and projected x/y or easting/northing columns are accepted when a CRS is given (--crs-assume or a .prj);
  * archives are extracted to a scratch folder with zip-slip, entry-count and size guards;
  * shapefile encodings follow the .cpg file; mojibake is reported, never silently kept;
  * Z and M values are dropped knowingly and the drop is reported (MAP-DATA-010);
  * shapefile field names are cut to 10 characters with a mapping table (MAP-DATA-011);
  * GeoJSON, KML, KMZ, GPX and TopoJSON are written in WGS84 (the formats require it);
  * a GTFS transit feed (a zip or a folder of GTFS tables) is read as layers routes, shapes, stations, stops and entrances (gtfs.py), each
    optionally narrowed to a mode (routes:light_rail); a saved GTFS-Realtime .pb file is read as vehicle points; neither is ever written;
  * PMTiles and MBTiles vector tile sets are written (tiles_out, through GDAL's drivers, in Web Mercator) but never read as data: a tile set
    is read only as a basemap (basemap-render), so they sit in TILE_FORMATS, outside FORMATS and the round-trip matrix;
  * every read and write returns notes; nothing is changed without a note;
  * the equivalent ogr2ogr command is produced for any conversion the user may want to rerun locally.

Nothing here writes outside the paths it is given, and it never fetches anything.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

WGS84 = "EPSG:4326"
GEOJSON_WARN_BYTES = 50 * 1024 * 1024          # spec 8: warn on large GeoJSON
ARCHIVE_MAX_ENTRIES = 2000
ARCHIVE_MAX_BYTES = 2 * 1024 ** 3              # also the shapefile component limit
ARCHIVE_MAX_RATIO = 200                        # uncompressed : compressed, a zip-bomb guard
SHP_FIELD_MAX = 10
FGB_INDEX_ABOVE = 10_000

LAT_NAMES = ("lat", "latitude", "y", "lat_dd", "point_y", "ycoord", "y_coord", "northing", "north", "lat_dms")
LON_NAMES = ("lon", "lng", "long", "longitude", "x", "lon_dd", "point_x", "xcoord", "x_coord", "easting", "east", "lon_dms")
PROJECTED_NAMES = ("x", "y", "xcoord", "ycoord", "x_coord", "y_coord", "point_x", "point_y", "easting", "northing", "east", "north")
CSV_SNIFF_BYTES = 8 * 1024
JSON_SNIFF_BYTES = 2 * 1024
GEOJSON_CHECK_BYTES = 64 * 1024
WKT_NAMES = ("wkt", "geometry", "geom", "the_geom", "shape")


class GeoIOError(Exception):
    """A refusal or a failure with a message that is safe to show the user."""


class AmbiguousColumns(GeoIOError):
    """CSV columns could not be chosen unambiguously (spec 8: confirmation when ambiguous)."""

    def __init__(self, message: str, candidates: dict):
        super().__init__(message)
        self.candidates = candidates


# --------------------------------------------------------------------------- format registry

@dataclass(frozen=True)
class Fmt:
    key: str
    label: str
    exts: tuple
    driver: str | None          # GDAL driver, None when handled by a library
    read: bool = True
    write: bool = True
    wgs84_only: bool = False
    note: str = ""


FORMATS: dict[str, Fmt] = {f.key: f for f in [
    Fmt("geojson", "GeoJSON (RFC 7946)", (".geojson", ".json"), "GeoJSON", wgs84_only=True,
        note="WGS84 on write; warns above 50 MB"),
    Fmt("shapefile", "Shapefile", (".shp", ".zip"), "ESRI Shapefile",
        note="field names cut to 10 characters on write, with a mapping table; a .zip is read by what it holds (shapefile, GeoJSON, GeoPackage or CSV)"),
    Fmt("kml", "KML", (".kml",), "KML", wgs84_only=True, note="styles are not carried; extended data becomes attributes"),
    Fmt("kmz", "KMZ", (".kmz",), "KML", wgs84_only=True, note="a zip holding doc.kml"),
    Fmt("gpkg", "GeoPackage", (".gpkg",), "GPKG", note="preferred interchange; multi-layer"),
    Fmt("csv", "CSV (lat/lon or WKT)", (".csv", ".tsv"), None,
        note="columns are auto-detected; ambiguity is reported; the delimiter is sniffed; DMS and projected x/y (with a CRS) are read"),
    Fmt("gpx", "GPX", (".gpx",), "GPX", wgs84_only=True, note="waypoints, routes and tracks"),
    Fmt("topojson", "TopoJSON", (".topojson",), "TopoJSON", wgs84_only=True,
        note="GDAL reads it but cannot write it; written with the topojson package"),
    Fmt("flatgeobuf", "FlatGeobuf", (".fgb",), "FlatGeobuf"),
    Fmt("geoparquet", "GeoParquet", (".parquet", ".geoparquet"), None,
        note="no GDAL Parquet driver in the pip wheel; geopandas and pyarrow are used"),
]}

# the write order used by the round-trip matrix
MATRIX = tuple(FORMATS)

# Vector tile sets (M14): written, never read as data. A tile set holds clipped, generalised copies of the features, so reading one back is
# not a conversion; basemap-render reads MBTiles and PMTiles as a basemap picture. They are outside FORMATS (and the round-trip matrix);
# write() and detect_write_format() take them, through tiles_out.
TILE_FORMATS: dict[str, Fmt] = {f.key: f for f in [
    Fmt("pmtiles", "PMTiles (vector tiles)", (".pmtiles",), "PMTiles", read=False,
        note="written through GDAL's PMTiles driver: Web Mercator, one source layer per layer, zooms from the data; read only as a basemap"),
    Fmt("mbtiles", "MBTiles (vector tiles)", (".mbtiles",), "MBTiles", read=False,
        note="written through GDAL's MBTiles driver (MVT tiles); read only as a basemap"),
]}
WRITE_FORMATS: dict[str, Fmt] = {**FORMATS, **TILE_FORMATS}

# Read-only formats (0.10.0): transit feeds are read as map layers by gtfs.py and never written. Outside FORMATS (and the round-trip matrix)
# like Esri JSON; a .zip or a folder is a GTFS feed when it holds stops.txt, routes.txt and trips.txt.
READ_ONLY_FORMATS: dict[str, Fmt] = {f.key: f for f in [
    Fmt("gtfs", "GTFS transit feed", (".zip",), None, write=False,
        note="a zip or folder of GTFS tables; layers routes, shapes, stations, stops, entrances, each optionally ':mode' (routes:light_rail)"),
    Fmt("gtfsrt", "GTFS-Realtime vehicle positions", (".pb",), None, write=False,
        note="a saved protobuf FeedMessage; vehicles as points (gtfs.py vehicles joins them to a static feed's routes)"),
]}


# extensions read only when a person names the file (never claimed by a folder scan or the formats table): a .txt is a README or notes
# far more often than a table, but a delimited .txt handed over as a site list is still read as one
EXPLICIT_EXTS = {".txt": "csv"}


def detect_format(path: str | Path) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    for f in FORMATS.values():
        if ext in f.exts:
            return f.key
    if ext in EXPLICIT_EXTS:
        return EXPLICIT_EXTS[ext]
    if ext == ".pb":
        return "gtfsrt"
    tile = next((f for f in TILE_FORMATS.values() if ext in f.exts), None)
    if tile is not None:
        raise GeoIOError(f"{p.name} is a {tile.label} tile set: this plugin writes tile sets but reads one only as a basemap (basemap-render); "
                         "convert the data the tiles were made from instead")
    raise GeoIOError(f"unsupported file type '{ext}'. Supported: " + ", ".join(sorted({e for f in FORMATS.values() for e in f.exts})))


def detect_write_format(path: str | Path) -> str:
    """The format a file is written as, from its extension: the read formats plus the tile sets (pmtiles, mbtiles)."""
    ext = Path(path).suffix.lower()
    for f in TILE_FORMATS.values():
        if ext in f.exts:
            return f.key
    return detect_format(path)


# --------------------------------------------------------------------------- results

@dataclass
class Note:
    id: str            # MAP-DATA-nnn / MAP-OUT-nnn the note belongs to, or "IO"
    sev: str           # INFO | WARN
    msg: str
    detail: object = None

    def as_dict(self) -> dict:
        d = {"id": self.id, "sev": self.sev, "msg": self.msg}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass
class ReadResult:
    gdf: object
    format: str
    path: Path
    layer: str | None = None
    layers: list = field(default_factory=list)
    encoding: str | None = None
    notes: list = field(default_factory=list)
    crs_assumed: bool = False
    columns: dict = field(default_factory=dict)       # CSV: which columns were used
    engine: dict | None = None                        # which engine ran `where` and why (M16: duck_engine.decide)


@dataclass
class WriteResult:
    path: Path
    format: str
    features: int
    crs: str | None
    field_map: dict = field(default_factory=dict)      # original -> written, only where different
    notes: list = field(default_factory=list)
    command: str = ""
    checks: list = field(default_factory=list)          # findings of the writer's own read-back (tile sets: MAP-OUT-009)


# --------------------------------------------------------------------------- archives

def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > ARCHIVE_MAX_ENTRIES:
        raise GeoIOError(f"archive has {len(infos)} entries; the limit is {ARCHIVE_MAX_ENTRIES}")
    total = sum(i.file_size for i in infos)
    packed = max(1, sum(i.compress_size for i in infos))
    if total > ARCHIVE_MAX_BYTES:
        raise GeoIOError(f"archive expands to {total / 1024 ** 2:.0f} MB; the limit is {ARCHIVE_MAX_BYTES // 1024 ** 2} MB")
    if total / packed > ARCHIVE_MAX_RATIO and total > 10 * 1024 * 1024:
        raise GeoIOError("archive compression ratio is implausible (possible zip bomb); refusing to extract")
    out = []
    for i in infos:
        n = i.filename.replace("\\", "/")
        if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/"):
            raise GeoIOError(f"archive entry '{i.filename}' escapes the extraction folder; refusing")
        if i.is_dir() or n.split("/")[-1].startswith("._") or "__MACOSX/" in n:
            continue
        out.append(i)
    return out


def extract_archive(path: Path, dest: Path) -> list[Path]:
    """Extract a zip (shapefile bundle or KMZ) into dest, flat by entry path, with guards."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise GeoIOError(f"{path.name} is not a valid zip archive") from e
    with zf:
        files = []
        for info in _safe_members(zf):
            target = dest / info.filename.replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files.append(target)
    return files


def _zip_write(dest: Path, files: list[Path], root: Path) -> None:
    """Deterministic zip: sorted entries, fixed timestamps."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
            zi = zipfile.ZipInfo(f.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, f.read_bytes())


# --------------------------------------------------------------------------- helpers

def _gpd():
    import geopandas as gpd
    return gpd


def _drop_zm(gdf, notes: list, where: str):
    import shapely
    if gdf.empty:
        return gdf
    has_z = bool(gdf.geometry.has_z.any())
    if has_z:
        gdf = gdf.copy()
        gdf["geometry"] = shapely.force_2d(gdf.geometry.values)
        notes.append(Note("MAP-DATA-010", "INFO", f"Z values were dropped {where}"))
    return gdf


MOJIBAKE = re.compile(r"(?:Ã[\x80-\xbf]|Â[\x80-\xbf]|â€[\x80-\xbf™œ]|�)")


def mojibake_columns(gdf) -> dict:
    """Columns whose strings look like text decoded with the wrong encoding."""
    found = {}
    for c in gdf.columns:
        import pandas as pd
        if c == gdf.geometry.name or not (gdf[c].dtype == object or pd.api.types.is_string_dtype(gdf[c])):
            continue
        hits = int(gdf[c].dropna().astype(str).str.contains(MOJIBAKE).sum())
        if hits:
            found[c] = hits
    return found


def _cpg_encoding(shp: Path) -> str | None:
    cpg = shp.with_suffix(".cpg")
    if cpg.is_file():
        raw = cpg.read_text(errors="ignore").strip().upper()
        if raw:
            return {"UTF8": "utf-8", "UTF-8": "utf-8", "65001": "utf-8", "1252": "cp1252", "ISO-8859-1": "latin-1",
                    "LATIN1": "latin-1", "ASCII": "ascii"}.get(raw, raw.lower())
    return None


ZIP_KINDS = (("shapefile", (".shp",)), ("gpkg", (".gpkg",)), ("geojson", (".geojson", ".json")), ("flatgeobuf", (".fgb",)),
             ("geoparquet", (".parquet", ".geoparquet")), ("kml", (".kml",)), ("gpx", (".gpx",)), ("csv", (".csv", ".tsv")))
# a .txt inside a zip is a README far more often than a table, so only .csv and .tsv members count; a .txt given by itself is read as a table


def zip_contents(path: str | Path) -> dict:
    """What a .zip holds, by its member names (no extraction): {'kind', 'members': {kind: [names]}, 'all': [names]}.

    A zip is not a shapefile by nature: publishers zip GeoJSON, GeoPackages and CSVs too, and a TIGER or Census download can hold several
    shapefiles. The kind is the first of shapefile, GeoPackage, GeoJSON, FlatGeobuf, GeoParquet, KML, GPX, CSV that is present, so a
    shapefile's own .dbf/.prj/.cpg companions and a README never decide."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = _safe_members(zf)
    except zipfile.BadZipFile as e:
        raise GeoIOError(f"{Path(path).name} is not a valid zip archive") from e
    names = [i.filename.replace("\\", "/") for i in infos]
    members = {}
    for kind, exts in ZIP_KINDS:
        hit = sorted(n for n in names if Path(n).suffix.lower() in exts)
        if hit:
            members[kind] = hit
    kind = next((k for k, _ in ZIP_KINDS if k in members), None)
    return {"kind": kind, "members": members, "all": names}


def sniff_json(path: str | Path) -> str:
    """geojson | topojson | esrijson, from the first 2 KB of a .json file. TopoJSON says "type": "Topology"; Esri JSON has features
    with "attributes" (not "properties") and usually a "spatialReference" or "geometryType" at the top."""
    head = Path(path).read_bytes()[:JSON_SNIFF_BYTES].decode("utf-8", errors="ignore")
    if re.search(r'"type"\s*:\s*"Topology"', head):
        return "topojson"
    if re.search(r'"(?:spatialReference|geometryType|displayFieldName|fieldAliases)"\s*:', head) or re.search(r'"attributes"\s*:\s*\{', head):
        if not re.search(r'"type"\s*:\s*"(?:Feature|FeatureCollection)"', head):
            return "esrijson"
    return "geojson"


def effective_format(path: str | Path) -> str:
    """The format a file is read as: the extension's format, except that a .zip is read by its members and a .json is sniffed."""
    p = Path(path)
    if p.is_dir():
        import gtfs
        if gtfs.is_gtfs(p):
            return "gtfs"
        raise GeoIOError(f"{p.name} is a folder; name a file in it (a folder is read only as a GTFS feed: stops.txt, routes.txt and trips.txt)")
    fmt = detect_format(p)
    if p.suffix.lower() == ".zip":
        zc = zip_contents(p)
        if zc["kind"] is None:
            import gtfs
            if gtfs.is_gtfs(p):
                return "gtfs"
            raise GeoIOError("the zip holds no geodata (no .shp, .gpkg, .geojson, .json, .fgb, .parquet, .kml, .gpx or .csv member, "
                             "and it is not a GTFS feed)")
        return zc["kind"]
    if fmt == "geojson" and p.suffix.lower() == ".json":
        return sniff_json(p)
    return fmt


def list_layers(path: str | Path) -> list[dict]:
    """Layers in a file: name, geometry type, feature count. A CSV or Parquet file has one; a zip of several shapefiles lists each."""
    import pyogrio
    p = Path(path)
    if not p.is_file() and not p.is_dir():
        raise GeoIOError(f"file not found: {p.name}")
    fmt = effective_format(p)
    if fmt == "gtfs":
        import gtfs
        try:
            return gtfs.list_layers(p)
        except gtfs.GTFSError as e:
            raise GeoIOError(str(e)) from e
    if fmt == "gtfsrt":
        return [{"name": "vehicles", "geometry_type": "Point", "features": None}]
    if fmt in ("csv", "geoparquet") and p.suffix.lower() != ".zip":
        return [{"name": p.stem, "geometry_type": None, "features": None}]
    tmp = None
    try:
        (target, tmp), _, extra = _resolve_container(p, fmt, None)
        if fmt in ("csv", "geoparquet"):
            return [{"name": Path(target).stem, "geometry_type": None, "features": None}]
        out = []
        for src in ([target] + [Path(x) for x in extra.get("other_shapefiles", [])]):
            for name, gtype in pyogrio.list_layers(src):
                n = pyogrio.read_info(src, layer=name).get("features")
                out.append({"name": name, "geometry_type": gtype, "features": int(n) if n is not None and n >= 0 else None})
        return out
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _esri_to_geojson_file(src: Path, dest: Path) -> tuple[str | None, int]:
    """Esri JSON (a FeatureServer query saved to disk) to a GeoJSON file. arcgis_client's converter is reused when it imports (it handles
    rings, holes and paths the Esri way); a small point/path/ring converter stands in otherwise. Returns (crs or None, features)."""
    doc = json.loads(src.read_text(encoding="utf-8-sig"))
    try:
        import arcgis_client as ac
        fc = ac.esri_to_geojson(doc)
    except ImportError:
        fc = _esri_to_geojson_basic(doc)
    fc.pop("exceededTransferLimit", None)
    sr = doc.get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    crs = None
    if wkid:
        wkid = int(wkid)
        crs = f"EPSG:{3857 if wkid == 102100 else wkid}"
    dest.write_text(json.dumps(fc), encoding="utf-8")
    return crs, len(fc.get("features", []))


def _esri_to_geojson_basic(doc: dict) -> dict:
    from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, Polygon, mapping
    out = []
    for f in doc.get("features", []):
        g, a = f.get("geometry"), f.get("attributes") or {}
        geom = None
        if g:
            if "x" in g and "y" in g:
                geom = Point(g["x"], g["y"])
            elif "points" in g:
                geom = MultiPoint([tuple(q[:2]) for q in g["points"]])
            elif "paths" in g:
                geom = MultiLineString([[tuple(q[:2]) for q in path] for path in g["paths"]]) if len(g["paths"]) != 1 else LineString([tuple(q[:2]) for q in g["paths"][0]])
            elif "rings" in g:
                rings = [[tuple(q[:2]) for q in r] for r in g["rings"] if len(r) >= 4]
                geom = Polygon(rings[0], rings[1:]) if rings else None
        out.append({"type": "Feature", "properties": a, "geometry": mapping(geom) if geom is not None else None})
    return {"type": "FeatureCollection", "features": out}


def _resolve_container(p: Path, fmt: str, layer: str | None = None):
    """Return ((readable_path, scratch_dir_or_None), encoding, extra). Extracts zip and KMZ containers and rewrites Esri JSON.

    extra: {'notes': [Note], 'other_shapefiles': [paths], 'esri_crs': str|None, 'esri_features': int|None}. A zip of several
    shapefiles reads the one `layer` names (by stem), else the first, and lists the others."""
    extra: dict = {"notes": [], "other_shapefiles": []}
    if fmt == "kmz" or p.suffix.lower() == ".zip":
        scratch = Path(tempfile.mkdtemp(prefix="gmap_io_"))
        files = extract_archive(p, scratch)
        if fmt == "kmz":
            kml = sorted([f for f in files if f.suffix.lower() == ".kml"], key=lambda f: (f.name != "doc.kml", f.as_posix()))
            if not kml:
                shutil.rmtree(scratch, ignore_errors=True)
                raise GeoIOError("the KMZ holds no .kml file")
            return (kml[0], scratch), None, extra
        exts = dict(ZIP_KINDS).get(fmt, ())
        cands = sorted(f for f in files if f.suffix.lower() in exts)
        if not cands:
            shutil.rmtree(scratch, ignore_errors=True)
            raise GeoIOError(f"the zip holds no {fmt} file")
        if fmt == "shapefile" and len(cands) > 1:
            if layer:
                pick = next((f for f in cands if f.stem == layer or f.name == layer), None)
                if pick is None:
                    shutil.rmtree(scratch, ignore_errors=True)
                    raise GeoIOError(f"the zip holds no shapefile '{layer}'. Shapefiles: " + ", ".join(f.stem for f in cands))
            else:
                pick = cands[0]
                extra["notes"].append(Note("IO", "INFO", f"the zip holds {len(cands)} shapefiles; read '{pick.stem}'. Others: "
                                           + ", ".join(f.stem for f in cands if f is not pick) + " (pass layer= to pick one)", [f.stem for f in cands]))
            extra["other_shapefiles"] = [f for f in cands if f is not pick]
            return (pick, scratch), _cpg_encoding(pick), extra
        if len(cands) > 1:
            extra["notes"].append(Note("IO", "INFO", f"the zip holds {len(cands)} {fmt} files; read '{cands[0].name}'. Others: " + ", ".join(f.name for f in cands[1:])))
        if fmt == "shapefile":
            return (cands[0], scratch), _cpg_encoding(cands[0]), extra
        if fmt == "geojson" and cands[0].suffix.lower() == ".json" and sniff_json(cands[0]) == "esrijson":
            target = scratch / "esri_as_geojson.geojson"
            extra["esri_crs"], extra["esri_features"] = _esri_to_geojson_file(cands[0], target)
            return (target, scratch), None, extra
        return (cands[0], scratch), None, extra
    if fmt == "shapefile":
        return (p, None), _cpg_encoding(p), extra
    if fmt == "esrijson":
        scratch = Path(tempfile.mkdtemp(prefix="gmap_io_"))
        target = scratch / "esri_as_geojson.geojson"
        try:
            extra["esri_crs"], extra["esri_features"] = _esri_to_geojson_file(p, target)
        except (ValueError, KeyError, TypeError) as e:
            shutil.rmtree(scratch, ignore_errors=True)
            raise GeoIOError(f"{p.name} looks like Esri JSON but could not be converted: {str(e)[:120]}") from e
        return (target, scratch), None, extra
    return (p, None), None, extra


# --------------------------------------------------------------------------- CSV

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", s.strip().lower().replace(" ", "_"))


def detect_csv_columns(columns: list[str]) -> dict:
    """Pick lat/lon or WKT columns. Returns {'mode': 'latlon'|'wkt', ...} or raises AmbiguousColumns."""
    norm = {c: _norm(c) for c in columns}
    lats = [c for c, n in norm.items() if n in LAT_NAMES]
    lons = [c for c, n in norm.items() if n in LON_NAMES]
    wkts = [c for c, n in norm.items() if n in WKT_NAMES]

    def pick(names, cands):
        for want in names:                      # earlier names are stronger evidence
            hit = [c for c in cands if norm[c] == want]
            if hit:
                return hit
        return []

    lat_best, lon_best = pick(LAT_NAMES, lats)[:1], pick(LON_NAMES, lons)[:1]
    if len(lats) > 1 or len(lons) > 1:
        # several candidates: accept only when the strongest names pair up unambiguously
        strong_lat = [c for c in lats if norm[c] in ("lat", "latitude")]
        strong_lon = [c for c in lons if norm[c] in ("lon", "lng", "long", "longitude")]
        if len(strong_lat) == 1 and len(strong_lon) == 1:
            return {"mode": "latlon", "lat": strong_lat[0], "lon": strong_lon[0]}
        raise AmbiguousColumns("more than one latitude/longitude column; name the pair to use",
                               {"lat": lats, "lon": lons})
    if lat_best and lon_best:
        return {"mode": "latlon", "lat": lat_best[0], "lon": lon_best[0]}
    if len(wkts) == 1:
        return {"mode": "wkt", "wkt": wkts[0]}
    if len(wkts) > 1:
        raise AmbiguousColumns("more than one WKT geometry column; name the one to use", {"wkt": wkts})
    raise AmbiguousColumns("no latitude/longitude or WKT columns found; name them", {"columns": list(columns)})


def sniff_delimiter(p: Path, encoding: str = "utf-8-sig") -> str:
    """The delimiter of a text table, from its first 8 KB: comma, semicolon, tab or pipe. csv.Sniffer decides; when it cannot, the most
    frequent of the four on the header line wins, and a comma is the last resort (so a one-column file still reads)."""
    import csv
    raw = p.read_bytes()[:CSV_SNIFF_BYTES]
    try:
        head = raw.decode(encoding)
    except UnicodeDecodeError:
        head = raw.decode("cp1252", errors="ignore")
    try:
        return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        first = head.splitlines()[0] if head.splitlines() else ""
        counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


DMS = re.compile(r"""^\s*([+-])?\s*(\d{1,3}(?:\.\d+)?)\s*(?:°|º|deg|d|:)?\s*
                     (?:(\d{1,2}(?:\.\d+)?)\s*(?:'|′|min|m|:)?\s*)?
                     (?:(\d{1,2}(?:\.\d+)?)\s*(?:"|″|''|sec|s)?\s*)?
                     ([NSEWnsew])?\s*$""", re.X)
DMS_LEAD = re.compile(r"""^\s*([NSEWnsew])\s*(\d.*)$""")


def parse_dms(text) -> float | None:
    """33°25'12"N, 112 04 20 W, 33d25m12sN, -112:04:20 or N33 25.2 as decimal degrees; None when the text is not a coordinate.
    A plain number is not DMS (the numeric path reads it), so a bare "33.5" gives None here on purpose."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    hemi = None
    m = DMS_LEAD.match(s)
    if m:
        hemi, s = m.group(1).upper(), m.group(2)
    m = DMS.match(s)
    if not m:
        return None
    sign, deg, mins, secs, h = m.groups()
    h = (h or hemi or "").upper()
    if mins is None and secs is None and not h and not re.search(r"[°ºd:]", s):
        return None
    v = float(deg) + (float(mins) if mins else 0.0) / 60.0 + (float(secs) if secs else 0.0) / 3600.0
    if (mins and float(mins) >= 60) or (secs and float(secs) >= 60):
        return None
    if sign == "-" or h in ("S", "W"):
        v = -v
    return v


def _coord_series(series, notes: list, what: str):
    """Numbers from a coordinate column: plain numbers as they are, DMS strings parsed (noted), anything else NaN."""
    import pandas as pd
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return series                    # already numbers: nothing to parse (M16: the text test below cost a second per 250,000 rows)
    num = pd.to_numeric(series, errors="coerce")
    missing = num.isna() & series.notna() & (series.astype(str).str.strip() != "")
    if missing.any():
        parsed = series[missing].map(parse_dms)
        got = parsed.notna()
        if got.any():
            num = num.astype("float64")
            num.loc[parsed.index[got]] = parsed[got].astype(float)
            notes.append(Note("IO", "INFO", f"{int(got.sum())} {what} value(s) were written as degrees, minutes and seconds and were converted to decimal degrees", int(got.sum())))
    return num


def _read_csv(p: Path, layer, crs_assume, lat_col, lon_col, wkt_col, encoding, max_features: int | None = None):
    import pandas as pd
    import shapely
    notes: list[Note] = []
    enc = encoding or "utf-8-sig"
    sep = sniff_delimiter(p, enc)
    kw = {"dtype": None, "low_memory": False, "sep": sep}
    if max_features:
        kw["nrows"] = int(max_features)
    try:
        df = pd.read_csv(p, encoding=enc, **kw)
    except UnicodeDecodeError:
        df = pd.read_csv(p, encoding="cp1252", **kw)
        enc = "cp1252"
        notes.append(Note("MAP-DATA-007", "WARN", "the file is not UTF-8; it was read as Windows-1252"))
    if sep != ",":
        notes.append(Note("IO", "INFO", f"the table is delimited by {'tab' if sep == chr(9) else repr(sep)}, not a comma"))
    df.columns = [str(c).strip() for c in df.columns]
    if lat_col or lon_col:
        if not (lat_col and lon_col):
            raise GeoIOError("give both the latitude and the longitude column")
        cols = {"mode": "latlon", "lat": lat_col, "lon": lon_col}
    elif wkt_col:
        cols = {"mode": "wkt", "wkt": wkt_col}
    else:
        cols = detect_csv_columns(list(df.columns))
    for c in cols.values():
        if c not in ("latlon", "wkt") and c not in df.columns:
            raise GeoIOError(f"column '{c}' is not in the file")
    gpd = _gpd()
    prj = p.with_suffix(".prj")
    projected = False
    if cols["mode"] == "latlon":
        lat = _coord_series(df[cols["lat"]], notes, "latitude")
        lon = _coord_series(df[cols["lon"]], notes, "longitude")
        bad = int((lat.isna() | lon.isna()).sum())
        ok = ~(lat.isna() | lon.isna())
        # x/y or easting/northing columns whose values cannot be degrees hold projected coordinates: they need a CRS, never a guess
        if ok.any() and (lon[ok].abs().max() > 180.0001 or lat[ok].abs().max() > 90.0001):
            projected = True
            crs_text = crs_assume or (prj.read_text(errors="ignore").strip() if prj.is_file() else None)
            if not crs_text:
                raise GeoIOError(f"columns '{cols['lon']}' and '{cols['lat']}' hold projected coordinates (values beyond 180/90 degrees), and the file states no CRS. "
                                 f"Say which CRS they are in with --crs-assume (or put a .prj file beside {p.name})")
            from pyproj import CRS
            if CRS.from_user_input(crs_text).is_geographic:
                raise GeoIOError(f"columns '{cols['lon']}' and '{cols['lat']}' hold projected coordinates (values beyond 180/90 degrees), but {crs_text} is a degree CRS; name the projected CRS they were written in")
        geom = shapely.points(lon.to_numpy(dtype="float64"), lat.to_numpy(dtype="float64"))
        geom = [None if (a != a or b != b) else g for g, a, b in zip(geom, lat, lon, strict=True)]
        if bad:
            notes.append(Note("MAP-DATA-003", "WARN", f"{bad} row(s) have no usable coordinates; their geometry is empty"))
        gdf = gpd.GeoDataFrame(df, geometry=gpd.array.from_shapely(geom), crs=None)
    else:
        raw = df[cols["wkt"]]
        try:
            geom = shapely.from_wkt(raw.where(raw.notna(), None).to_numpy(dtype=object), on_invalid="warn")
        except Exception as e:
            raise GeoIOError(f"WKT column '{cols['wkt']}' could not be parsed: {e}") from e
        gdf = gpd.GeoDataFrame(df.drop(columns=[cols["wkt"]]), geometry=gpd.array.from_shapely(list(geom)), crs=None)
    assumed = False
    if crs_assume:
        gdf = gdf.set_crs(crs_assume)
        assumed = True
        notes.append(Note("MAP-DATA-001", "INFO", f"a CSV has no CRS; {crs_assume} was assumed as you instructed" + (f" for the projected columns '{cols['lon']}' and '{cols['lat']}'" if projected else "")))
    elif (cols["mode"] == "wkt" or projected) and prj.is_file():
        gdf = gdf.set_crs(prj.read_text(errors="ignore").strip())
        assumed = False
        notes.append(Note("MAP-DATA-001", "INFO", f"CRS taken from {prj.name}" + (f" for the projected columns '{cols['lon']}' and '{cols['lat']}'" if projected else "")))
    elif cols["mode"] == "latlon":
        gdf = gdf.set_crs(WGS84)
        assumed = True
        notes.append(Note("MAP-DATA-001", "INFO", "a CSV has no CRS; latitude/longitude columns are taken to be WGS84 (EPSG:4326)"))
    else:
        notes.append(Note("MAP-DATA-001", "WARN", "a WKT CSV has no CRS; pass the CRS it uses"))
    return gdf, enc, cols, assumed, notes


KML_BOOKKEEPING = ("id", "timestamp", "begin", "end", "altitudeMode", "tessellate", "extrude", "visibility", "drawOrder", "icon")
GPX_BOOKKEEPING = ("ele", "time", "magvar", "geoidheight", "cmt", "src", "link1_href", "link1_text", "link1_type", "link2_href",
                    "link2_text", "link2_type", "sym", "type", "fix", "sat", "hdop", "vdop", "pdop", "ageofdgpsdata", "dgpsid",
                    "track_fid", "track_seg_id", "track_seg_point_id", "route_fid", "route_point_id", "number", "name", "desc")


def _tidy_columns(gdf, fmt: str, notes: list):
    """Drop the empty bookkeeping columns some drivers add, and normalize KML's 'Name' to 'name'."""
    if fmt in ("kml", "kmz"):
        empty = [c for c in KML_BOOKKEEPING if c in gdf.columns and gdf[c].nunique(dropna=False) <= 1]
        gdf = gdf.drop(columns=empty)
        if "id" in gdf.columns and gdf["id"].astype(str).str.fullmatch(r"[^.]+\.\d+").all():
            gdf = gdf.drop(columns=["id"])          # LIBKML's generated placemark ids
        if "Name" in gdf.columns and "name" not in gdf.columns:
            gdf = gdf.rename(columns={"Name": "name"})
        if "description" in gdf.columns and gdf["description"].isna().all():
            gdf = gdf.drop(columns=["description"])
        if "name" in gdf.columns and gdf["name"].isna().all():
            gdf = gdf.drop(columns=["name"])
    elif fmt == "gpx":
        empty = [c for c in GPX_BOOKKEEPING if c in gdf.columns and gdf[c].isna().all()]
        gdf = gdf.drop(columns=empty)
        gdf = gdf.rename(columns={c: c[4:] for c in gdf.columns if c.startswith("ogr_") and c[4:] not in gdf.columns})
    elif fmt == "topojson":
        if "id" in gdf.columns and list(gdf["id"].astype(str)) == [str(i) for i in range(len(gdf))]:
            gdf = gdf.drop(columns=["id"])
    return gdf

# --------------------------------------------------------------------------- read

def _gpkg_where(gdf, where):
    """The reference meaning of `where`: the frame written to a scratch GeoPackage and read back through GDAL with the filter, as ogr2ogr
    would read it."""
    import pyogrio
    scratch = Path(tempfile.mkdtemp(prefix="gmap_io_"))
    try:
        pyogrio.write_dataframe(gdf, scratch / "t.gpkg", driver="GPKG", layer="t")
        return pyogrio.read_dataframe(scratch / "t.gpkg", layer="t", where=where)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _duck_where(gdf, where):
    """The same rows through DuckDB (M16), in the shape the GeoPackage gives back: a fresh index and the geometry last, named `geometry`.
    An empty result takes its column types from the GeoPackage read of the empty frame, as the round trip would."""
    import duck_engine as de
    pos = de.filter_positions(gdf, where)
    if not len(pos):
        return _gpkg_where(gdf.iloc[:0], None)
    out = gdf.iloc[pos].reset_index(drop=True)
    geom = out.geometry.name
    if geom != "geometry":
        out = out.rename_geometry("geometry")
    cols = [c for c in out.columns if c != "geometry"] + ["geometry"]
    return out[cols] if list(out.columns) != cols else out


def _post_filter(gdf, where, bbox, columns, notes: list, engine: str | None = None, record: dict | None = None):
    """Formats read by pandas get the same filters as GDAL-read formats. `where` means exactly what it means to ogr2ogr: the frame goes
    through a scratch GeoPackage, or (M16) through DuckDB when `engine` picks it and the filter is in the subset both read the same way
    (duck_engine.where_sql). `record` receives the engine record."""
    if bbox:
        minx, miny, maxx, maxy = bbox
        gdf = gdf.cx[minx:maxx, miny:maxy]
    if where:
        import duck_engine as de
        sql, why = (de.where_sql(where, gdf) if de.version() else (None, None))
        try:
            dec = de.decide(engine, len(gdf), "where", refuse=(f"the filter is read by the GeoPackage path: {why}" if sql is None and why else None))
        except de.EngineError as ex:
            raise GeoIOError(str(ex)) from None
        if dec["engine"] == "duckdb":
            try:
                gdf = _duck_where(gdf, where)
            except Exception as ex:  # noqa: BLE001 - DuckDB refused the text or the columns: the reference path gives the answer
                dec = {**dec, "engine": "pandas", "why": f"DuckDB could not run the filter ({str(ex).splitlines()[0][:120]}); the GeoPackage path ran it"}
                gdf = _gpkg_where(gdf, where)
        else:
            gdf = _gpkg_where(gdf, where)
        if record is not None:
            record.update(dec)
        notes.append(Note("IO", "INFO", f"the where filter ran in {'DuckDB' if dec['engine'] == 'duckdb' else 'a scratch GeoPackage'} ({dec['why']})", dec))
    if columns:
        keep = [c for c in columns if c in gdf.columns]
        gdf = gdf[keep + [gdf.geometry.name]]
    return gdf


def read(path: str | Path, layer: str | None = None, *, crs_assume: str | None = None,
         lat_col: str | None = None, lon_col: str | None = None, wkt_col: str | None = None,
         encoding: str | None = None, drop_z: bool = True, max_features: int | None = None,
         where: str | None = None, bbox: tuple | list | None = None, columns: list | None = None, engine: str | None = None) -> ReadResult:
    """Read one layer of a vector file into a GeoDataFrame, with notes about everything that was decided.

    where   an OGR SQL attribute filter, the same text ogr2ogr -where takes (applied at read time, so it is cheap on big files)
    engine  auto | pandas | duckdb: what runs `where` on a CSV or GeoParquet read (duck_engine; GDAL formats filter while reading);
            the choice and why is in the result's `engine`
    bbox    (minx, miny, maxx, maxy) in the file's own CRS: keep features that intersect it (ogr2ogr -spat)
    columns attribute columns to keep (ogr2ogr -select)
    layer   a layer name; for a zip of several shapefiles, the stem of the shapefile to read
    max_features  stop after this many features, whatever the format (GDAL formats stop reading; CSV reads that many rows;
            GeoParquet reads the file's first row groups until it has them)

    A .zip is read by what it holds and a .json by what it says (see effective_format); the result's `format` is the format that was
    read, so a zipped GeoPackage reports gpkg, not shapefile.
    """
    import pyogrio
    _gpd()
    p = Path(path)
    if not p.is_file() and not p.is_dir():
        raise GeoIOError(f"file not found: {p.name}")
    fmt = effective_format(p)
    if fmt in ("gtfs", "gtfsrt"):
        return _read_gtfs(p, fmt, layer, max_features=max_features, where=where, bbox=bbox, columns=columns, engine=engine)
    nominal = detect_format(p)
    notes: list[Note] = []
    size = p.stat().st_size
    if fmt == "geojson" and size > GEOJSON_WARN_BYTES:
        notes.append(Note("MAP-DATA-008", "WARN", f"GeoJSON is {size / 1024 ** 2:.0f} MB; prefer GeoPackage or FlatGeobuf for large data"))
    if p.suffix.lower() == ".zip" and fmt != "shapefile":
        notes.append(Note("IO", "INFO", f"the zip holds {FORMATS[fmt].label if fmt in FORMATS else fmt} data, not a shapefile; it was read as such"))
    if fmt == "esrijson":
        notes.append(Note("IO", "INFO", f"{p.name} is Esri JSON (features with attributes); it was converted to GeoJSON to read"))
    elif fmt == "topojson" and nominal == "geojson":
        notes.append(Note("IO", "INFO", f"{p.name} is TopoJSON (a Topology), not GeoJSON; it was read as TopoJSON"))

    layers: list = []
    enc = encoding
    assumed = False
    csv_cols: dict = {}
    scratch = None
    pushed_rec: dict = {}
    try:
        if fmt in ("csv", "geoparquet"):
            target = p
            if p.suffix.lower() == ".zip":
                (target, scratch), _, extra = _resolve_container(p, fmt, layer)
                notes += extra["notes"]
            if fmt == "csv":
                gdf, enc, csv_cols, assumed, n2 = _read_csv(target, layer, crs_assume, lat_col, lon_col, wkt_col, encoding, max_features)
                notes += n2
            else:
                pushed = _parquet_pushdown(target, where, columns, engine) if where and not max_features else None
                if pushed is not None:                       # 1.0.0 (TODO M16): the filter and the columns read inside the Parquet scan
                    gdf, pushed_rec = pushed
                    notes.append(Note("IO", "INFO", f"the where filter ran inside DuckDB's Parquet scan ({pushed_rec['why']})", pushed_rec))
                else:
                    gdf = _read_parquet(target, max_features)
                if gdf.crs is None:
                    notes.append(Note("MAP-DATA-001", "WARN", "the GeoParquet file carries no CRS metadata"))
            layer_name = target.stem
        else:
            (target, scratch), cpg, extra = _resolve_container(p, fmt, layer)
            notes += extra["notes"]
            enc = encoding or cpg
            layers = [x[0] for x in pyogrio.list_layers(target)]
            if not layers:
                raise GeoIOError("the file holds no layers")
            others = extra.get("other_shapefiles") or []
            if others:
                layers = layers + [f.stem for f in others]
            layer_name = layers[0] if (layer is None or others) else layer
            if layer is None and fmt == "gpx":
                # a GPX file always has five layers and reports no feature counts; take the first that holds features
                for cand in ("waypoints", "tracks", "routes"):
                    if cand in layers:
                        probe = pyogrio.read_dataframe(target, layer=cand, max_features=1, read_geometry=False)
                        if len(probe):
                            layer_name = cand
                            break
            if layer is None and len(layers) > 1 and not others:
                notes.append(Note("IO", "INFO", f"the file has {len(layers)} layers; read '{layer_name}'. Layers: " + ", ".join(layers), layers))
            if layer is not None and layer not in layers:
                raise GeoIOError(f"layer '{layer}' is not in the file. Layers: " + ", ".join(layers))
            kw = {"layer": layer_name}
            if enc:
                kw["encoding"] = enc
            if max_features:
                kw["max_features"] = max_features
            if where:
                kw["where"] = where
            if bbox:
                kw["bbox"] = tuple(bbox)
            if columns:
                kw["columns"] = list(columns)
            try:
                gdf = pyogrio.read_dataframe(target, **kw)
            except Exception as e:
                raise GeoIOError(f"could not read {p.name}: {e}") from e
            if fmt in ("kml", "kmz"):
                head = target.read_bytes()[:2_000_000].lower()
                if b"<networklink" in head:
                    notes.append(Note("IO", "WARN", "the KML has network links; they are not followed (no data is fetched)"))
            gdf = _tidy_columns(gdf, fmt, notes)
            if fmt == "topojson" and gdf.crs is None:
                gdf = gdf.set_crs(crs_assume or WGS84)
                assumed = True
                notes.append(Note("MAP-DATA-001", "INFO", "TopoJSON carries no CRS; " + (crs_assume or WGS84) + " was assumed"))
            if fmt == "esrijson":
                ecrs = extra.get("esri_crs")
                if ecrs and gdf.crs is None:
                    gdf = gdf.set_crs(ecrs)
                    notes.append(Note("MAP-DATA-001", "INFO", f"CRS {ecrs} taken from the Esri JSON spatialReference"))
                elif ecrs and not gdf.crs.equals(ecrs):
                    gdf = gdf.set_crs(ecrs, allow_override=True)
                    notes.append(Note("MAP-DATA-001", "INFO", f"CRS {ecrs} taken from the Esri JSON spatialReference"))
                elif not ecrs and gdf.crs is not None and not crs_assume:
                    gdf = gdf.set_crs(None, allow_override=True)
                    notes.append(Note("MAP-DATA-001", "WARN", "the Esri JSON states no spatialReference; pass the CRS it uses"))
            if fmt == "shapefile" and enc is None:
                notes.append(Note("MAP-DATA-007", "INFO", "no .cpg file; the attribute encoding is the GDAL default (UTF-8 with a Latin-1 fallback)"))
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    if gdf.crs is None and crs_assume and not assumed:
        gdf = gdf.set_crs(crs_assume)
        assumed = True
        notes.append(Note("MAP-DATA-001", "INFO", f"the file has no CRS; {crs_assume} was assumed as you instructed"))
    engine_rec: dict = {}
    if fmt == "geoparquet" and pushed_rec:
        engine_rec = dict(pushed_rec)
        gdf = _post_filter(gdf, None, bbox, columns, notes)
    elif fmt in ("csv", "geoparquet"):
        gdf = _post_filter(gdf, where, bbox, columns, notes, engine=engine, record=engine_rec)
    elif where:
        engine_rec = {"engine": "gdal", "why": f"GDAL filters {FORMATS[fmt].label if fmt in FORMATS else fmt} while reading", "kind": "where"}
    if drop_z:
        gdf = _drop_zm(gdf, notes, "on reading")
    moji = mojibake_columns(gdf)
    if moji:
        notes.append(Note("MAP-DATA-007", "WARN", "text that looks mis-decoded in: " + ", ".join(f"{c} ({n})" for c, n in moji.items()), moji))
    if max_features and len(gdf) >= max_features:
        notes.append(Note("MAP-DATA-008", "WARN", f"reading stopped at {max_features} features"))
    return ReadResult(gdf=gdf, format=fmt, path=p, layer=layer_name, layers=layers, encoding=enc,
                      notes=notes, crs_assumed=assumed, columns=csv_cols, engine=engine_rec or None)


def _read_gtfs(p: Path, fmt: str, layer, *, max_features=None, where=None, bbox=None, columns=None, engine=None) -> ReadResult:
    """A GTFS feed (layer routes, stations, ... optionally ':mode') or a GTFS-Realtime file, through gtfs.py; where/bbox/columns as for CSV."""
    import gtfs
    try:
        if fmt == "gtfs":
            gdf, notes = gtfs.read_layer(p, layer, max_features=max_features)
            kind, mode = gtfs.parse_layer(layer)
            name = f"{kind}:{mode}" if mode else kind
            layers = [x["name"] for x in gtfs.list_layers(p)]
        else:
            if layer not in (None, "vehicles"):
                raise GeoIOError(f"a GTFS-Realtime file has one layer, 'vehicles' (not '{layer}')")
            gdf, notes = gtfs.read_vehicle_positions(p)
            name, layers = "vehicles", ["vehicles"]
    except gtfs.GTFSError as e:
        raise GeoIOError(str(e)) from e
    rec: dict = {}
    gdf = _post_filter(gdf, where, bbox, columns, notes, engine=engine, record=rec)
    return ReadResult(gdf=gdf, format=fmt, path=p, layer=name, layers=layers, encoding="utf-8", notes=notes, crs_assumed=False,
                      columns={}, engine=rec or None)


PARQUET_PUSHDOWN_BYTES = 256 * 1024 * 1024     # 1.0.0: `auto` reads a GeoParquet file at least this big through DuckDB when it has a where


def _parquet_pushdown(p: Path, where: str, columns, engine: str | None):
    """1.0.0 (TODO M16): a GeoParquet `where` (and column list) run inside DuckDB's Parquet scan, so a file larger than memory is never read
    whole. Only when DuckDB is installed, `engine` is duckdb (or auto and the file is PARQUET_PUSHDOWN_BYTES or more) and the filter is in the
    subset both engines read the same way (duck_engine.where_sql, checked against the file's schema). -> (gdf, engine record) or None."""
    if engine == "pandas":
        return None
    import duck_engine as de
    if not de.version():
        return None
    size = p.stat().st_size
    if engine in (None, "auto") and size < PARQUET_PUSHDOWN_BYTES:
        return None
    try:
        import io
        import json as _json
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(p)
        meta = pf.schema_arrow.metadata or {}
        geo = _json.loads((meta.get(b"geo") or b"{}").decode("utf-8"))
        gcol = geo.get("primary_column") or "geometry"
        probe = pf.schema_arrow.empty_table().to_pandas()
        probe = probe.drop(columns=[gcol], errors="ignore")
        sql, why = de.where_sql(where, probe)
        if sql is None:
            return None
        names = [f.name for f in pf.schema_arrow]
        keep = [c for c in (columns or names) if c in names and c != gcol] + [gcol]
        sel = ", ".join('"' + c.replace('"', '""') + '"' for c in keep)
        con = de.connect()
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")              # DuckDB 2 notes the extra copy a DB-API result makes; the rows are the filtered ones only
                tab = de._arrow_result(con.execute(f"SELECT {sel} FROM read_parquet(?, file_row_number = true) WHERE {sql} ORDER BY file_row_number",
                                                   [str(p)]))
        finally:
            con.close()
        if hasattr(tab, "read_all"):
            tab = tab.read_all()
        if "file_row_number" in tab.column_names:
            tab = tab.drop(["file_row_number"])
        import pyarrow as pa
        tab = tab.select(keep).cast(pa.schema([pf.schema_arrow.field(c) for c in keep]))     # the file's own types (DuckDB hands back string views)
        tab = tab.replace_schema_metadata(meta)
        buf = io.BytesIO()
        pq.write_table(tab, buf)
        buf.seek(0)
        gdf = _gpd().read_parquet(buf)
        if not len(gdf):                                   # an empty result has the GeoPackage path's columns and types, as _duck_where gives
            gdf = _gpkg_where(gdf, None)
    except Exception:  # noqa: BLE001 - DuckDB could not read it here: the plain read and the GeoPackage filter give the answer
        return None
    return gdf, {"engine": "duckdb", "kind": "where", "rows": int(len(gdf)), "pushdown": True,
                 "why": f"{'asked for' if engine == 'duckdb' else f'the file is {size / 1048576:,.0f} MB'}; {len(keep) - 1} column(s) read"}


def _read_parquet(p: Path, max_features: int | None = None):
    """GeoParquet, the whole file or its first row groups until max_features rows are in hand (pyarrow reads by row group, so a cap never
    pulls the whole file through memory)."""
    gpd = _gpd()
    try:
        if not max_features:
            return gpd.read_parquet(p)
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(p)
        batches, n = [], 0
        for i in range(pf.num_row_groups):
            t = pf.read_row_group(i)
            batches.append(t)
            n += t.num_rows
            if n >= max_features:
                break
        table = pa.concat_tables(batches) if batches else pf.read()
        table = table.slice(0, int(max_features)).replace_schema_metadata(pf.schema_arrow.metadata or {})
        buf = io.BytesIO()
        pq.write_table(table, buf)                   # the geo metadata travels with the schema, so geopandas reads the CRS back
        buf.seek(0)
        return gpd.read_parquet(buf)
    except Exception as e:
        raise GeoIOError(f"could not read GeoParquet: {e}") from e


# --------------------------------------------------------------------------- write

def shapefile_field_map(columns: list[str]) -> dict:
    """original -> written (10 characters, unique, case-insensitive) for the names that change."""
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for c in columns:
        base = re.sub(r"[^A-Za-z0-9_]", "_", c)[:SHP_FIELD_MAX] or "field"
        name, i = base, 0
        while name.lower() in used:
            i += 1
            suffix = f"_{i}"
            name = base[: SHP_FIELD_MAX - len(suffix)] + suffix
        used.add(name.lower())
        if name != c:
            mapping[c] = name
    return mapping


def _to_wgs84(gdf, notes: list, why: str):
    if gdf.crs is None:
        raise GeoIOError(f"the data has no CRS, and {why} needs WGS84. Say which CRS it uses first")
    if not gdf.crs.equals(WGS84):
        notes.append(Note("IO", "INFO", f"reprojected from {gdf.crs.to_string()} to {WGS84} ({why})"))
        gdf = gdf.to_crs(WGS84)
    return gdf


def _single_geometry_family(gdf) -> str | None:
    kinds = {g.geom_type.replace("Multi", "") for g in gdf.geometry if g is not None and not g.is_empty}
    return kinds.pop() if len(kinds) == 1 else None


def _kml_driver() -> str:
    import pyogrio
    drivers = pyogrio.list_drivers()
    return "LIBKML" if "LIBKML" in drivers else "KML"


def write(gdf, path: str | Path, fmt: str | None = None, layer: str | None = None, *,
          overwrite: bool = True, append: bool = False, topo_quantization: int = 1e5, tiles: dict | None = None) -> WriteResult:
    """Write a GeoDataFrame in the given format (default: from the file extension). A tile set (pmtiles, mbtiles) takes `tiles`:
    {minzoom, maxzoom, simplify, attribution, name, description} (tiles_out.write_tiles)."""
    import pyogrio
    p = Path(path)
    fmt = fmt or detect_write_format(p)
    if fmt not in WRITE_FORMATS or not WRITE_FORMATS[fmt].write:
        raise GeoIOError(f"cannot write '{fmt}'")
    if p.exists() and not overwrite and not append:
        raise GeoIOError(f"{p.name} exists; choose another name or allow overwrite")
    if fmt in TILE_FORMATS:
        return _write_tiles(gdf, p, fmt, layer, tiles or {})
    p.parent.mkdir(parents=True, exist_ok=True)
    notes: list[Note] = []
    field_map: dict = {}
    layer = layer or p.stem

    gdf = _drop_zm(gdf, notes, "on writing")
    if FORMATS[fmt].wgs84_only:
        gdf = _to_wgs84(gdf, notes, f"{FORMATS[fmt].label} requires WGS84")
    if gdf.crs is None and fmt not in ("csv",):
        notes.append(Note("MAP-DATA-001", "WARN", "the data has no CRS; the output has none either"))

    if fmt == "geojson":
        opts = {"RFC7946": "YES"}
        # a GeoJSON file is one layer; GDAL names it from the file
        pyogrio.write_dataframe(gdf, p, driver="GeoJSON", layer=layer, **opts)
        _check_geojson(p, notes)
    elif fmt == "shapefile":
        field_map = shapefile_field_map([c for c in gdf.columns if c != gdf.geometry.name])
        out = gdf.rename(columns=field_map) if field_map else gdf
        if field_map:
            notes.append(Note("MAP-DATA-011", "INFO", f"{len(field_map)} field name(s) changed for the shapefile", field_map))
        if _single_geometry_family(out) is None and len(out):
            raise GeoIOError("a shapefile holds one geometry type; split the data by geometry type first")
        if p.suffix.lower() == ".zip":
            scratch = Path(tempfile.mkdtemp(prefix="gmap_io_"))
            try:
                pyogrio.write_dataframe(out, scratch / f"{layer}.shp", driver="ESRI Shapefile", encoding="UTF-8")
                _zip_write(p, [f for f in scratch.iterdir() if f.is_file()], scratch)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        else:
            pyogrio.write_dataframe(out, p, driver="ESRI Shapefile", encoding="UTF-8")
    elif fmt in ("kml", "kmz"):
        drv = _kml_driver()
        if fmt == "kml":
            pyogrio.write_dataframe(gdf, p, driver=drv, layer=layer)
        else:
            scratch = Path(tempfile.mkdtemp(prefix="gmap_io_"))
            try:
                pyogrio.write_dataframe(gdf, scratch / "doc.kml", driver=drv, layer=layer)
                _zip_write(p, [scratch / "doc.kml"], scratch)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
    elif fmt == "gpkg":
        pyogrio.write_dataframe(gdf, p, driver="GPKG", layer=layer, append=append)
    elif fmt == "flatgeobuf":
        # FlatGeobuf sorts features along a Hilbert curve when it builds its spatial index. Small files keep the input
        # order; large ones are indexed for fast bounding-box reads and are therefore reordered.
        indexed = len(gdf) > FGB_INDEX_ABOVE
        pyogrio.write_dataframe(gdf, p, driver="FlatGeobuf", layer=layer, SPATIAL_INDEX="YES" if indexed else "NO")
        if indexed:
            notes.append(Note("IO", "INFO", "FlatGeobuf wrote a spatial index, which reorders features along a Hilbert curve"))
    elif fmt == "gpx":
        _write_gpx(gdf, p, notes)
    elif fmt == "topojson":
        import topojson as tp
        topo = tp.Topology(gdf, topology=True, prequantize=int(topo_quantization), object_name=layer)
        p.write_text(topo.to_json(), encoding="utf-8")
        notes.append(Note("IO", "INFO", f"TopoJSON written with the topojson package, quantized to {int(topo_quantization)} steps"))
    elif fmt == "geoparquet":
        gdf.to_parquet(p, index=False)
    elif fmt == "csv":
        _write_csv(gdf, p, notes)
    return WriteResult(path=p, format=fmt, features=len(gdf), crs=gdf.crs.to_string() if gdf.crs else None,
                       field_map=field_map, notes=notes)


def _write_tiles(gdf, p: Path, fmt: str, layer: str | None, opts: dict) -> WriteResult:
    """One layer as a vector tile set (tiles_out); the read-back findings come back in `checks`, the decisions in `notes`."""
    import tiles_out
    name = layer or p.stem
    known = {"minzoom", "maxzoom", "simplify", "attribution", "name", "description", "max_bytes"}
    try:
        rep = tiles_out.write_tiles(fmt, {name: gdf}, p, **{k: v for k, v in opts.items() if k in known and v is not None})
    except tiles_out.TilesError as e:
        raise GeoIOError(str(e)) from e
    notes = [Note("IO", "INFO", n) for n in rep["notes"]]
    notes.append(Note("IO", "INFO", f"{TILE_FORMATS[fmt].label}: zoom {rep['minzoom']} to {rep['maxzoom']}, {rep['bytes'] / 1024:.0f} kB, in Web Mercator (EPSG:3857)"))
    return WriteResult(path=p, format=fmt, features=sum(v["features"] for v in rep["layers"].values()), crs="EPSG:3857", notes=notes,
                       command=rep["command"], checks=rep["findings"])


# a bare NaN or Infinity in a value position (after : , or [ and before , ] or }); the same words inside a quoted string are text
NAN_TOKEN = re.compile(rb"[:,\[]\s*(?:NaN|-?Infinity)\s*(?=[,\]}])")


def _check_geojson(p: Path, notes: list) -> None:
    """RFC 7946 spot checks on the bytes just written: no NaN/Infinity tokens, not above the size warning.

    The token scan streams the file in 1 MB blocks (with a 16-byte overlap so a token split across blocks is still seen) instead of
    reading it all into one string: a 500 MB GeoJSON was read twice over for one regex. The full RFC 7946 check (data_lint) parses it."""
    size = p.stat().st_size
    if size > GEOJSON_WARN_BYTES:
        notes.append(Note("MAP-DATA-008", "WARN", f"the GeoJSON is {size / 1024 ** 2:.0f} MB; prefer GeoPackage or FlatGeobuf"))
    found = False
    with open(p, "rb") as f:
        tail = b""
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            if NAN_TOKEN.search(tail + block):
                found = True
                break
            tail = block[-16:]
    if found:
        notes.append(Note("MAP-OUT-007", "WARN", "the GeoJSON contains NaN or Infinity, which RFC 7946 does not allow"))


def _write_gpx(gdf, p: Path, notes: list) -> None:
    import pyogrio
    fam = _single_geometry_family(gdf)
    if fam is None and len(gdf):
        raise GeoIOError("GPX holds points (waypoints) or lines (tracks); split mixed data first")
    if fam not in (None, "Point", "LineString"):
        raise GeoIOError("GPX cannot hold polygons; write the boundary as lines or use another format")
    layer = "waypoints" if fam == "Point" else "tracks"
    out = gdf.copy()
    notes.append(Note("IO", "INFO", "attributes beyond name, desc, ele and time are written as ogr: extensions; other GPX software may ignore them"))
    if layer == "tracks":
        import shapely
        out["geometry"] = [shapely.MultiLineString([g]) if g is not None and g.geom_type == "LineString" else g for g in out.geometry]
    pyogrio.write_dataframe(out, p, driver="GPX", layer=layer, dataset_options={"GPX_USE_EXTENSIONS": "YES"})


def _write_csv(gdf, p: Path, notes: list) -> None:
    import pandas as pd
    out = pd.DataFrame(gdf.drop(columns=gdf.geometry.name))
    fam = _single_geometry_family(gdf)
    if gdf.crs is not None and not gdf.crs.equals(WGS84) and fam == "Point":
        gdf = gdf.to_crs(WGS84)
        notes.append(Note("IO", "INFO", f"reprojected to {WGS84} for the latitude/longitude columns"))
    if fam == "Point" and all(g.geom_type == "Point" for g in gdf.geometry if g is not None):
        out["longitude"] = [None if g is None or g.is_empty else g.x for g in gdf.geometry]
        out["latitude"] = [None if g is None or g.is_empty else g.y for g in gdf.geometry]
        notes.append(Note("IO", "INFO", "points written as longitude and latitude columns (WGS84)"))
    else:
        out["wkt"] = [None if g is None else g.wkt for g in gdf.geometry]
        crs = gdf.crs.to_string() if gdf.crs else "unknown"
        if gdf.crs is not None:
            p.with_suffix(".prj").write_text(gdf.crs.to_wkt(), encoding="utf-8")
            notes.append(Note("IO", "INFO", f"geometry written as WKT in {crs}; the CRS is recorded in {p.with_suffix('.prj').name}"))
        else:
            notes.append(Note("IO", "WARN", "geometry written as WKT; the data had no CRS, so none is recorded"))
    out.to_csv(p, index=False, encoding="utf-8")


# --------------------------------------------------------------------------- ogr2ogr equivalent

OGR_DRIVER = {"geojson": "GeoJSON", "shapefile": "ESRI Shapefile", "kml": "LIBKML", "kmz": "LIBKML", "gpkg": "GPKG",
              "csv": "CSV", "gpx": "GPX", "flatgeobuf": "FlatGeobuf", "geoparquet": "Parquet", "pmtiles": "PMTiles", "mbtiles": "MBTiles"}


def ogr2ogr_command(src: str | Path, dst: str | Path, *, t_srs: str | None = None, s_srs: str | None = None,
                    where: str | None = None, select: list[str] | None = None, bbox: list[float] | None = None,
                    simplify: float | None = None, layer: str | None = None, dst_format: str | None = None) -> tuple[str, list[str]]:
    """The ogr2ogr command that does the same conversion. Returns (command, caveats)."""
    caveats: list[str] = []
    dfmt = dst_format or detect_write_format(dst)
    sfmt = detect_format(src)
    if dfmt == "topojson":
        return "", ["ogr2ogr cannot write TopoJSON; use the topojson Python package or mapshaper (`mapshaper in.geojson -o out.topojson format=topojson`)"]
    args = ["ogr2ogr", "-f", OGR_DRIVER[dfmt]]
    if dfmt == "geojson":
        args += ["-lco", "RFC7946=YES"]
    if dfmt == "shapefile":
        args += ["-lco", "ENCODING=UTF-8"]
        caveats.append("shapefile field names longer than 10 characters are truncated by GDAL; check the names")
    if dfmt == "csv":
        args += ["-lco", "GEOMETRY=AS_WKT"]
        caveats.append("add -lco GEOMETRY=AS_XY for point data if you want longitude and latitude columns")
    if dfmt == "geoparquet":
        caveats.append("needs a GDAL build that has the Parquet driver (the pip wheels do not)")
    if sfmt == "csv":
        caveats.append("add -oo X_POSSIBLE_NAMES=lon -oo Y_POSSIBLE_NAMES=lat (or GEOM_POSSIBLE_NAMES=wkt) so GDAL finds the geometry")
    if dfmt in TILE_FORMATS:
        args += ["-preserve_fid"]
        caveats.append("add -dsco MINZOOM=n -dsco MAXZOOM=n (the zooms this tool chose are in its report) and -dsco SIMPLIFICATION=1 -dsco SIMPLIFICATION_MAX_ZOOM=0; "
                       "tippecanoe is a faster alternative for millions of features")
    wgs = WRITE_FORMATS[dfmt].wgs84_only
    if dfmt in TILE_FORMATS:
        args += ["-t_srs", "EPSG:3857"]
    elif t_srs or wgs:
        args += ["-t_srs", t_srs or WGS84]
    if s_srs:
        args += ["-s_srs", s_srs]
    if bbox:
        args += ["-spat"] + [str(v) for v in bbox]
    if where:
        args += ["-where", where]
    if select:
        args += ["-select", ",".join(select)]
    if simplify:
        args += ["-simplify", str(simplify)]
    if layer:
        args += ["-nln", layer]
    args += [str(dst), str(src)]
    return " ".join(shlex.quote(a) for a in args), caveats


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("formats", help="list supported formats")
    pl = sub.add_parser("layers", help="list the layers in a file")
    pl.add_argument("path")
    pc = sub.add_parser("command", help="print the equivalent ogr2ogr command")
    pc.add_argument("src")
    pc.add_argument("dst")
    pc.add_argument("--t-srs")
    pc.add_argument("--s-srs")
    pc.add_argument("--where")
    pc.add_argument("--select")
    pc.add_argument("--bbox", nargs=4, type=float)
    pc.add_argument("--simplify", type=float)
    pc.add_argument("--layer")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "formats":
            print(json.dumps([{"key": f.key, "label": f.label, "extensions": list(f.exts), "read": f.read, "write": f.write, "note": f.note}
                              for f in (*WRITE_FORMATS.values(), *READ_ONLY_FORMATS.values())], indent=2))
        elif a.cmd == "layers":
            print(json.dumps(list_layers(a.path), indent=2))
        elif a.cmd == "command":
            cmd, caveats = ogr2ogr_command(a.src, a.dst, t_srs=a.t_srs, s_srs=a.s_srs, where=a.where,
                                           select=a.select.split(",") if a.select else None, bbox=a.bbox,
                                           simplify=a.simplify, layer=a.layer)
            print(json.dumps({"command": cmd, "caveats": caveats}, indent=2))
        return gc.EXIT_OK
    except GeoIOError as e:
        print(f"geo-io: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
