#!/usr/bin/env python3
"""ogc_client: protocol layer for OGC services (spec 7.3 `ogc-client`). WFS 1.1/2.0, OGC API Features, WMS 1.1.1/1.3.0, WMTS, tiles.

Pure functions over `ogc_http.SafeHTTP`; the MCP server and the command-line scripts call the same code, so they give the same result for the same
request. Parsers also work offline on saved responses (`parse_capabilities`, `parse_describe_feature_type`), for user-run captures.
Text that comes from a service is data: titles and abstracts pass through `ogc_http.untrusted` before they are shown.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ogc_http as oh  # noqa: E402
from ogc_http import ServiceError, Refusal, untrusted  # noqa: E402

JSON_FORMATS = ("application/json", "application/geo+json", "application/vnd.geo+json", "geojson", "json")
SERVER_MAX_CEILING = 100_000
PAGE_CEILING = 100_000
MAX_PAGES = 200


# ---------------------------------------------------------------------------------------------------------------- xml helpers
def ln(e) -> str:
    t = e.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def kids(e, name: str):
    return [c for c in e if ln(c) == name]


def first(e, *path):
    cur = e
    for p in path:
        if cur is None:
            return None
        nxt = kids(cur, p)
        cur = nxt[0] if nxt else None
    return cur


def txt(e, *path) -> str | None:
    n = first(e, *path) if path else e
    return (n.text or "").strip() if n is not None and n.text else None


def desc(e, name: str):
    return [c for c in e.iter() if ln(c) == name]


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------- endpoints
def keeps_slash(profile: dict) -> bool:
    """1.0.2 (F4): a profile whose address carries the {key} slot (a key in the path) or says keep_trailing_slash: true is used exactly as
    saved. FIRMS documents /{MAP_KEY}/?SERVICE=...; when the key is the last segment, .../KEY?... and .../KEY/?... can route differently."""
    u = str(profile.get("base_url") or "")
    return bool(profile.get("keep_trailing_slash")) or bool(re.search(r"(?i)\{key\}|%7Bkey%7D", u))


def sub_url(base: str, *parts: str) -> str:
    """A sub-path under a service address: one slash between, whatever the address ends with."""
    return "/".join([base.rstrip("/")] + [p.strip("/") for p in parts if p])


def endpoint(profile: dict, protocol: str) -> str:
    """The URL for a protocol on a profile: an explicit `endpoints` entry, else the GeoServer pattern, else the base address (1.0.2: as saved
    when the profile keeps its trailing slash)."""
    eps = profile.get("endpoints") or {}
    if eps.get(protocol):
        return eps[protocol]
    raw = profile["base_url"]
    base = raw.rstrip("/")
    ptype = profile.get("type", "ogc-generic")
    if ptype == "geoserver" and protocol in ("wfs", "wms", "wmts", "wcs"):
        ws = profile.get("workspace")
        return f"{base}/{ws}/{protocol}" if ws else f"{base}/{protocol}"
    if protocol == "wmts" and ptype == "geoserver":
        return f"{base}/gwc/service/wmts"
    return raw if keeps_slash(profile) else base


# ---------------------------------------------------------------------------------------------------------------- capabilities
def parse_capabilities(content: bytes) -> dict:
    root = oh.safe_xml(content)
    name = ln(root)
    if name == "WFS_Capabilities":
        return _wfs_caps(root)
    if name in ("WMS_Capabilities", "WMT_MS_Capabilities"):
        return _wms_caps(root)
    if name == "Capabilities":
        ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        return _wcs_caps(root) if "/wcs" in ns.lower() else _wmts_caps(root)
    if name in ("ExceptionReport", "ServiceExceptionReport"):
        raise ServiceError("the service returned an error: " + untrusted(" ".join(t.strip() for t in root.itertext() if t.strip()), 240), "MAP-SVC-013")
    raise ServiceError(f"not a capabilities document (root element '{untrusted(name, 40)}')", "MAP-SVC-013")


# ---------------------------------------------------------------------------------------------------------------- 0.15.0: WCS 2.0
WCS_MAX_SIDE = 4096          # GetCoverage asks for at most this many cells a side (SCALESIZE), whatever the box


def wcs_describe(http, profile: dict, coverage: str) -> dict:
    """DescribeCoverage: the coverage's native CRS, its axis labels (the names GetCoverage subsets by), its box and its grid size."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", coverage or ""):
        raise Refusal(f"'{untrusted(coverage, 40)}' is not a coverage id", "MAP-SVC-003")
    r = http.get(endpoint(profile, "wcs"), {"service": "WCS", "version": "2.0.1", "request": "DescribeCoverage", "coverageId": coverage}, expect="xml")
    root = oh.safe_xml(r.content)
    if ln(root) in ("ExceptionReport", "ServiceExceptionReport"):
        raise ServiceError("the service returned an error: " + untrusted(" ".join(x.strip() for x in root.itertext() if x.strip()), 240), "MAP-SVC-013")
    env = next(iter(desc(root, "Envelope")), None)
    if env is None:
        raise ServiceError("the coverage description has no envelope", "MAP-SVC-013")
    labels = (env.get("axisLabels") or "").split()
    srs = env.get("srsName") or ""
    lo_, hi_ = [float(v) for v in (txt(env, "lowerCorner") or "").split()], [float(v) for v in (txt(env, "upperCorner") or "").split()]
    hl = next(iter(desc(root, "high")), None)
    grid = [int(v) + 1 for v in (hl.text or "").split()] if hl is not None and hl.text else None
    m = re.search(r"EPSG(?:/0/|::|:)(\d+)", srs)
    return {"coverage": coverage, "crs": f"EPSG:{m.group(1)}" if m else srs, "axis_labels": labels, "lower": lo_, "upper": hi_, "grid": grid}


def wcs_get_coverage(http, profile: dict, coverage: str, bbox: list, out: Path, *, fmt: str = "image/tiff", max_side: int = WCS_MAX_SIDE) -> dict:
    """GetCoverage (WCS 2.0.1) for a lon/lat box: the box moved into the coverage's own CRS and sent as one SUBSET per axis label, the size held
    to `max_side` cells a side with SCALESIZE, the answer written as a GeoTIFF and read back to check it is one. Returns the file record."""
    from pyproj import Transformer
    d = wcs_describe(http, profile, coverage)
    if len(d["axis_labels"]) != 2:
        raise ServiceError(f"coverage '{untrusted(coverage, 60)}' has {len(d['axis_labels'])} axes; this plugin reads 2-D coverages", "MAP-SVC-013")
    x0, y0, x1, y1 = (float(v) for v in bbox)
    crs = d["crs"] if d["crs"].startswith("EPSG:") else "EPSG:4326"
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = tr.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bx = [min(xs), min(ys), max(xs), max(ys)]
    lab = d["axis_labels"]
    lat_first = lab[0].lower() in ("lat", "latitude", "y", "n", "northing") or (crs == "EPSG:4326" and lab[0].lower() not in ("long", "lon", "x", "e"))
    ax_x, ax_y = (lab[1], lab[0]) if lat_first else (lab[0], lab[1])
    params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"), ("coverageId", coverage), ("format", fmt),
              ("subset", f"{ax_x}({bx[0]:.10g},{bx[2]:.10g})"), ("subset", f"{ax_y}({bx[1]:.10g},{bx[3]:.10g})"),
              ("scalesize", f"{ax_x}({int(max_side)}),{ax_y}({int(max_side)})")]
    url = endpoint(profile, "wcs") + "?" + urllib.parse.urlencode(params)
    r = http.get(url, None, expect="any")
    if not r.content.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        raise ServiceError("the service did not return a GeoTIFF: " + untrusted(r.content[:200].decode("utf-8", "replace"), 160), "MAP-SVC-013")
    Path(out).write_bytes(r.content)
    import rasterio
    with rasterio.open(out) as ds:
        info = {"width": ds.width, "height": ds.height, "bands": ds.count, "crs": ds.crs.to_string() if ds.crs else None, "dtype": ds.dtypes[0],
                "bounds": [round(v, 6) for v in ds.bounds], "nodata": ds.nodata}
    return {"path": str(out), "bytes": len(r.content), "sha256": r.sha256, "url": oh.redact_url(r.url), "coverage": coverage, "native_crs": crs,
            "axis_labels": lab, "raster": info}


def _keywords(e) -> list[str]:
    return [untrusted(k.text, 60) for k in desc(e, "Keyword") if k.text][:20]


def _bbox_lonlat(e) -> list[float] | None:
    b = first(e, "WGS84BoundingBox")
    if b is not None:
        lo, hi = (txt(b, "LowerCorner") or "").split(), (txt(b, "UpperCorner") or "").split()
        v = [to_float(x) for x in lo + hi]
        if len(v) == 4 and None not in v:
            return v
    g = first(e, "EX_GeographicBoundingBox")
    if g is not None:
        v = [to_float(txt(g, k)) for k in ("westBoundLongitude", "southBoundLatitude", "eastBoundLongitude", "northBoundLatitude")]
        if None not in v:
            return v
    ll = first(e, "LatLonBoundingBox")
    if ll is not None:
        v = [to_float(ll.get(k)) for k in ("minx", "miny", "maxx", "maxy")]
        if None not in v:
            return v
    return None


def _wfs_caps(root) -> dict:
    si = first(root, "ServiceIdentification") if first(root, "ServiceIdentification") is not None else first(root, "Service")
    out = {"service": "WFS", "version": root.get("version"), "title": untrusted(txt(si, "Title") if si is not None else None, 160),
           "abstract": untrusted(txt(si, "Abstract") if si is not None else None, 500), "keywords": _keywords(si) if si is not None else [],
           "fees": untrusted(txt(si, "Fees") if si is not None else None, 200), "access_constraints": untrusted(txt(si, "AccessConstraints") if si is not None else None, 300),
           "layers": [], "operations": [], "formats": [], "count_default": None, "result_paging": None}
    for ft in desc(root, "FeatureType"):
        nm = txt(ft, "Name")
        if not nm:
            continue
        crs = [txt(ft, k) for k in ("DefaultCRS", "DefaultSRS", "SRS") if txt(ft, k)]
        crs += [c.text.strip() for c in kids(ft, "OtherCRS") + kids(ft, "OtherSRS") if c.text]
        out["layers"].append({"name": nm, "title": untrusted(txt(ft, "Title"), 160), "abstract": untrusted(txt(ft, "Abstract"), 400), "crs": crs[:12], "bbox": _bbox_lonlat(ft), "keywords": _keywords(ft)})
    om = first(root, "OperationsMetadata")
    if om is not None:
        for op in kids(om, "Operation"):
            out["operations"].append(op.get("name"))
            if op.get("name") == "GetFeature":
                for p in kids(op, "Parameter"):
                    if (p.get("name") or "").lower() == "outputformat":
                        out["formats"] += [v.text.strip() for v in desc(p, "Value") if v.text]
        for c in desc(om, "Constraint"):
            n = (c.get("name") or "")
            dv = txt(c, "DefaultValue")
            if n == "CountDefault" and dv:
                out["count_default"] = int(dv) if dv.isdigit() else None
            if n == "ImplementsResultPaging" and dv:
                out["result_paging"] = dv.lower() == "true"
    return out


def _wms_caps(root) -> dict:
    sv = first(root, "Service")
    out = {"service": "WMS", "version": root.get("version"), "title": untrusted(txt(sv, "Title") if sv is not None else None, 160),
           "abstract": untrusted(txt(sv, "Abstract") if sv is not None else None, 500), "keywords": _keywords(sv) if sv is not None else [],
           "fees": untrusted(txt(sv, "Fees") if sv is not None else None, 200), "access_constraints": untrusted(txt(sv, "AccessConstraints") if sv is not None else None, 300),
           "layers": [], "operations": [], "formats": []}
    cap = first(root, "Capability")
    if cap is None:
        return out
    req = first(cap, "Request")
    if req is not None:
        out["operations"] = [ln(c) for c in req]
        out["formats"] = [f.text.strip() for f in desc(first(req, "GetMap"), "Format") if f.text] if first(req, "GetMap") is not None else []
    top = first(cap, "Layer")

    def walk(layer, inherited_crs, inherited_time):
        crs = list(inherited_crs) + [c.text.strip() for c in kids(layer, "CRS") + kids(layer, "SRS") if c.text]
        tdim = _time_dimension(layer) or inherited_time
        nm = txt(layer, "Name")
        if nm:
            out["layers"].append({"name": nm, "title": untrusted(txt(layer, "Title"), 160), "abstract": untrusted(txt(layer, "Abstract"), 400), "crs": sorted(set(crs))[:30],
                                  "bbox": _bbox_lonlat(layer), "queryable": layer.get("queryable") in ("1", "true"), "styles": [txt(s, "Name") for s in kids(layer, "Style")][:10],
                                  **({"time": tdim} if tdim else {})})
        for c in kids(layer, "Layer"):
            walk(c, crs, tdim)
    if top is not None:
        walk(top, [], None)
    return out


def _time_dimension(layer) -> dict | None:
    """WMS-T (0.15.0): a layer's time dimension, from <Dimension name="time"> (1.3.0, values inline) or <Extent name="time"> (1.1.1), with its
    default and its values as the server lists them (an ISO 8601 list, or start/end/period), shortened to keep the answer small."""
    for tag in ("Dimension", "Extent"):
        for d in kids(layer, tag):
            if (d.get("name") or "").lower() == "time":
                vals = (d.text or "").strip()
                ext = next((e for e in kids(layer, "Extent") if (e.get("name") or "").lower() == "time"), None)
                if not vals and ext is not None:
                    vals = (ext.text or "").strip()
                    d = ext if d.get("default") is None else d
                if vals or d.get("default"):
                    return {"default": untrusted(d.get("default"), 40), "values": untrusted(vals, 400), "units": d.get("units") or "ISO8601",
                            "current": d.get("current") in ("1", "true")}
    return None


_TIME_RE = re.compile(r"^(current|\d{4}(-\d{2}(-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?)"
                      r"(/(\d{4}(-\d{2}(-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?|current)(/P[0-9YMDTHS]+)?)?$")


def check_time(value: str | None) -> str | None:
    """A WMS TIME value this plugin sends: an ISO 8601 date or date-time, a start/end range (with an optional period), or `current`; anything
    else is refused before a request is made (MAP-SVC-039)."""
    if value in (None, ""):
        return None
    v = str(value).strip()
    if len(v) > 80 or not _TIME_RE.match(v):
        raise Refusal(f"'{untrusted(v, 40)}' is not a WMS time this plugin sends: an ISO 8601 date or time (2026-11-07, 2026-11-07T15:00Z), a range "
                      "start/end, or current", "MAP-SVC-039")
    return v


def _wcs_caps(root) -> dict:
    """WCS 2.0 (0.15.0): the coverages a server offers (CoverageId, subtype, the WGS 84 box) and the formats it can return."""
    si = first(root, "ServiceIdentification")
    out = {"service": "WCS", "version": root.get("version"), "title": untrusted(txt(si, "Title") if si is not None else None, 160),
           "abstract": untrusted(txt(si, "Abstract") if si is not None else None, 500), "keywords": _keywords(si) if si is not None else [],
           "fees": untrusted(txt(si, "Fees") if si is not None else None, 200), "access_constraints": untrusted(txt(si, "AccessConstraints") if si is not None else None, 300),
           "layers": [], "operations": [], "formats": []}
    om = first(root, "OperationsMetadata")
    if om is not None:
        out["operations"] = [op.get("name") for op in kids(om, "Operation")]
    sm = first(root, "ServiceMetadata")
    if sm is not None:
        out["formats"] = [f.text.strip() for f in kids(sm, "formatSupported") if f.text][:20]
    for cs in desc(root, "CoverageSummary"):
        cid = txt(cs, "CoverageId")
        if cid:
            out["layers"].append({"name": untrusted(cid, 120), "title": untrusted(txt(cs, "Title"), 160), "subtype": txt(cs, "CoverageSubtype"), "bbox": _bbox_lonlat(cs)})
    return out


def _wmts_caps(root) -> dict:
    si = first(root, "ServiceIdentification")
    out = {"service": "WMTS", "version": root.get("version"), "title": untrusted(txt(si, "Title") if si is not None else None, 160),
           "abstract": untrusted(txt(si, "Abstract") if si is not None else None, 500), "keywords": [], "fees": untrusted(txt(si, "Fees") if si is not None else None, 200),
           "access_constraints": untrusted(txt(si, "AccessConstraints") if si is not None else None, 300), "layers": [], "tile_matrix_sets": [], "operations": [], "formats": []}
    contents = first(root, "Contents")
    if contents is None:
        return out
    for lyr in kids(contents, "Layer"):
        out["layers"].append({"name": txt(lyr, "Identifier"), "title": untrusted(txt(lyr, "Title"), 160), "abstract": untrusted(txt(lyr, "Abstract"), 300), "bbox": _bbox_lonlat(lyr),
                              "formats": [f.text for f in kids(lyr, "Format") if f.text], "tile_matrix_sets": [txt(l, "TileMatrixSet") for l in kids(lyr, "TileMatrixSetLink")],
                              "resource_urls": [{"format": r.get("format"), "template": r.get("template")} for r in kids(lyr, "ResourceURL")][:4]})
    for tms in kids(contents, "TileMatrixSet"):
        out["tile_matrix_sets"].append({"id": txt(tms, "Identifier"), "crs": txt(tms, "SupportedCRS"), "levels": len(kids(tms, "TileMatrix"))})
    return out


def get_capabilities(http, profile: dict, protocol: str = "wfs", version: str | None = None) -> dict:
    ep = endpoint(profile, protocol)
    ver = version or {"wfs": (profile.get("protocols") or {}).get("wfs") or "2.0.0", "wms": (profile.get("protocols") or {}).get("wms") or "1.3.0", "wmts": "1.0.0", "wcs": "2.0.1"}[protocol]
    r = http.get(ep, {"service": protocol.upper(), "version": ver, "request": "GetCapabilities"}, expect="xml")
    caps = parse_capabilities(r.content)
    caps["_response"] = oh.response_record(http, r)
    return caps


# ---------------------------------------------------------------------------------------------------------------- OGC API
def ogcapi_collections(http, profile: dict) -> dict:
    base = endpoint(profile, "ogcapi")
    land = json.loads(http.get(base, {"f": "json"}, expect="json").content)
    r = http.get(base.rstrip("/") + "/collections", {"f": "json"}, expect="json")
    cols = json.loads(r.content).get("collections", [])
    out = {"service": "OGC API", "version": None, "title": untrusted(land.get("title"), 160), "abstract": untrusted(land.get("description"), 500), "keywords": [], "layers": [], "operations": ["items"], "formats": ["application/geo+json"],
           "fees": None, "access_constraints": None}
    for c in cols:
        ext = ((c.get("extent") or {}).get("spatial") or {}).get("bbox") or []
        out["layers"].append({"name": c.get("id"), "title": untrusted(c.get("title"), 160), "abstract": untrusted(c.get("description"), 400), "crs": (c.get("crs") or [])[:8],
                              "bbox": ext[0][:4] if ext and len(ext[0]) >= 4 else None, "keywords": []})
    out["_response"] = oh.response_record(http, r)
    return out


# ---------------------------------------------------------------------------------------------------------------- DescribeFeatureType
XSD_TYPES = {"string": "string", "int": "integer", "integer": "integer", "long": "integer", "short": "integer", "byte": "integer", "nonnegativeinteger": "integer", "positiveinteger": "integer",
             "double": "number", "float": "number", "decimal": "number", "boolean": "boolean", "date": "date", "datetime": "datetime", "time": "time"}
GML_KIND = {"point": "Point", "multipoint": "MultiPoint", "linestring": "LineString", "multilinestring": "MultiLineString", "curve": "LineString", "multicurve": "MultiLineString",
            "surface": "Polygon", "multisurface": "MultiPolygon", "polygon": "Polygon", "multipolygon": "MultiPolygon", "geometry": "Geometry", "multigeometry": "GeometryCollection"}


def parse_describe_feature_type(content: bytes) -> dict:
    root = oh.safe_xml(content)
    if ln(root) in ("ExceptionReport", "ServiceExceptionReport"):
        raise ServiceError("the service returned an error: " + untrusted(" ".join(t.strip() for t in root.itertext() if t.strip()), 240), "MAP-SVC-013")
    tops = kids(root, "element")
    typename = tops[0].get("name") if tops else None
    fields, geom = [], None
    for el in desc(root, "element"):
        if el.getparent() is not None and ln(el.getparent()) == "schema":
            continue
        t = (el.get("type") or "")
        nm = el.get("name")
        if not nm:
            continue
        local = t.split(":")[-1]
        gm = re.match(r"(?i)(.+)PropertyType$", local) if t.lower().startswith("gml:") else None
        if gm:
            geom = {"name": nm, "type": GML_KIND.get(gm.group(1).lower(), gm.group(1))}
            continue
        fields.append({"name": nm, "type": XSD_TYPES.get(local.lower(), untrusted(local, 30) or "string"), "nillable": el.get("nillable") == "true"})
    if not fields and geom is None:
        raise ServiceError("the schema lists no fields", "MAP-SVC-003")
    return {"typename": typename, "fields": fields, "geometry": geom}


def describe_feature_type(http, profile: dict, typename: str, version: str | None = None) -> dict:
    ver = version or (profile.get("protocols") or {}).get("wfs") or "2.0.0"
    p = {"service": "WFS", "version": ver, "request": "DescribeFeatureType", "typeNames" if ver.startswith("2") else "typeName": typename}
    r = http.get(endpoint(profile, "wfs"), p, expect="xml")
    d = parse_describe_feature_type(r.content)
    d["_response"] = oh.response_record(http, r)
    return d


def infer_fields(features: list[dict]) -> dict:
    """Fields and geometry type from a few GeoJSON features (OGC API collections and ArcGIS geojson)."""
    types: dict[str, str] = {}
    geom = None
    for f in features:
        g = (f.get("geometry") or {}).get("type")
        geom = geom or g
        for k, v in (f.get("properties") or {}).items():
            t = "integer" if isinstance(v, int) and not isinstance(v, bool) else "number" if isinstance(v, float) else "boolean" if isinstance(v, bool) else "string" if v is not None else None
            if t and (k not in types or (types[k] == "integer" and t == "number")):
                types[k] = t
            types.setdefault(k, "string")
    return {"fields": [{"name": k, "type": t, "nillable": True} for k, t in types.items()], "geometry": {"name": "geometry", "type": geom} if geom else None}


# ---------------------------------------------------------------------------------------------------------------- WFS and OGC API features
@dataclass
class FetchPlan:
    protocol: str                      # wfs | ogcapi | arcgis
    layer: str                         # typename, collection id, or ArcGIS layer URL path
    version: str = "2.0.0"
    cql_filter: str | None = None
    bbox: list | None = None           # lon/lat minx, miny, maxx, maxy
    srs: str = "EPSG:4326"
    properties: list | None = None
    page_size: int = 1000
    sort_by: str | None = None
    max_features: int | None = None    # a cap the person set; also the cap when the server will not say how many there are
    output_format: str | None = None
    extra: dict = field(default_factory=dict)
    conditions: list | None = None     # 0.15.0: [{property, op, value}], sent as OGC Filter XML (WFS) instead of CQL


@dataclass
class FetchResult:
    pages: list = field(default_factory=list)       # {n, path, count, sha256, url}
    hits: int | None = None
    received: int = 0
    truncated: bool = False
    server_limit: int | None = None
    duplicates: int = 0
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    resumed_pages: int = 0


def pick_json_format(formats: list[str]) -> str | None:
    low = [(f, f.lower()) for f in formats]
    for want in JSON_FORMATS:
        for f, lf in low:
            if lf == want:
                return f
    for f, lf in low:
        if "json" in lf and "jsonp" not in lf:
            return f
    return None


# ---------------------------------------------------------------------------------------------------------------- 0.15.0: non-JSON WFS output
FORMAT_KINDS = (("json", JSON_FORMATS), ("gml", ("application/gml+xml; version=3.2", "gml32", "gml3", "text/xml; subtype=gml/3.1.1", "gml2", "text/xml")),
                ("csv", ("csv", "text/csv")), ("shapezip", ("shape-zip", "application/zip", "shapezip")))


def pick_format(formats: list[str]) -> tuple[str, str]:
    """(format to ask for, its kind: json | gml | csv | shapezip). GeoJSON when the server offers it; otherwise GML, then CSV with WKT, then a
    zipped shapefile, each read back into GeoJSON page by page. A server that lists no formats is asked for application/json."""
    j = pick_json_format(formats)
    if j or not formats:
        return (j or "application/json"), "json"
    low = [(f, f.lower()) for f in formats]
    for kind, names in FORMAT_KINDS[1:]:
        for want in names:
            for f, lf in low:
                if lf == want or (kind == "gml" and "gml" in lf):
                    return f, kind
    return formats[0], "gml"


def page_to_geojson(content: bytes, kind: str) -> bytes:
    """A WFS page in GML, CSV (geometry as WKT) or a zipped shapefile, as GeoJSON bytes (EPSG:4326 when the source says its CRS)."""
    import io
    import geopandas as gpd
    if kind == "json":
        return content
    try:
        if kind == "csv":
            import pandas as pd
            import shapely
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
            gcol = next((c for c in df.columns if c.lower() in ("the_geom", "geom", "geometry", "wkt", "shape")), None)
            if gcol is None:
                raise ServiceError("the CSV page carries no geometry column (the_geom, geom, geometry or wkt)", "MAP-SVC-013")
            geoms = shapely.from_wkt(df[gcol].replace("", None).to_numpy(), on_invalid="ignore")
            g = gpd.GeoDataFrame(df.drop(columns=[gcol]), geometry=geoms, crs="EPSG:4326")
        else:
            g = gpd.read_file(io.BytesIO(content))
    except ServiceError:
        raise
    except Exception as ex:                      # noqa: BLE001 - GDAL raises many types; the page is reported, not the trace
        raise ServiceError(f"the {kind.upper()} page could not be read ({type(ex).__name__})", "MAP-SVC-013")
    if g.crs is not None and g.crs.to_epsg() != 4326:
        g = g.to_crs(4326)
    return g.to_json(drop_id=False).encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------- 0.15.0: OGC Filter XML
FILTER_OPS = {"=": "PropertyIsEqualTo", "!=": "PropertyIsNotEqualTo", "<": "PropertyIsLessThan", "<=": "PropertyIsLessThanOrEqualTo",
              ">": "PropertyIsGreaterThan", ">=": "PropertyIsGreaterThanOrEqualTo", "~": "PropertyIsLike"}
_COND = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.:-]{0,63})\s*(>=|<=|!=|=|<|>|~)\s*(.*?)\s*$")


def parse_conditions(text: str | list | None) -> list[dict]:
    """`STATE=AZ; POP>=1000; NAME~Phoe*` (or a list of {property, op, value}) as conditions joined by AND. `~` is a like-match with `*` as the
    wildcard. A property is a plain name; a value is taken as text (the server compares it by the property's type)."""
    if not text:
        return []
    items = text if isinstance(text, list) else [s for s in str(text).split(";") if s.strip()]
    out = []
    for it in items:
        if isinstance(it, dict):
            prop, op, val = it.get("property"), it.get("op", "="), it.get("value")
        else:
            m = _COND.match(it)
            if not m:
                raise Refusal(f"'{untrusted(it, 60)}' is not a condition such as STATE=AZ, POP>=1000 or NAME~Phoe*", "MAP-SVC-038")
            prop, op, val = m.groups()
        if isinstance(val, str) and val[:1] in "<>=!~":            # STATE<>AZ would otherwise read as STATE < ">AZ"
            raise Refusal(f"'{untrusted(str(it), 60)}': use != for 'not equal', and one operator per condition", "MAP-SVC-038")
        if not (isinstance(prop, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", prop)) or op not in FILTER_OPS:
            raise Refusal(f"condition on '{untrusted(prop, 40)}' with '{op}' is not one this plugin sends", "MAP-SVC-038")
        out.append({"property": prop, "op": op, "value": "" if val is None else str(val)[:200]})
    return out


def conditions_text(conds: list[dict], dialect: str = "cql2") -> str:
    """The same conditions as text for a service that takes a text filter: CQL2 text (OGC API Features) or an SQL where clause (ArcGIS). A
    value is quoted (with quotes doubled) unless it is a plain number; `~` becomes LIKE with % for *."""
    out = []
    for c in conds:
        v = c["value"]
        num = bool(re.fullmatch(r"-?\d+(\.\d+)?", v))
        if c["op"] == "~":
            lit = "'" + v.replace("'", "''").replace("*", "%") + "'"
            out.append(f"{c['property']} LIKE {lit}")
            continue
        lit = v if num else "'" + v.replace("'", "''") + "'"
        op = "<>" if c["op"] == "!=" else c["op"]
        out.append(f"{c['property']} {op} {lit}")
    return " AND ".join(out)


def filter_xml(conds: list[dict], version: str = "2.0.0", bbox: list | None = None, srs: str = "EPSG:4326") -> str:
    """OGC Filter XML for WFS GetFeature: FES 2.0 for WFS 2.0, Filter Encoding 1.1 for WFS 1.x; the conditions joined by And, and the box
    as a BBOX when there is one. Every name is checked and every value escaped; nothing from the request is passed through as markup."""
    from xml.sax.saxutils import escape
    v2 = version.startswith("2")
    ns, pfx, ref = (("http://www.opengis.net/fes/2.0", "fes", "ValueReference") if v2 else ("http://www.opengis.net/ogc", "ogc", "PropertyName"))
    parts = []
    for c in conds:
        tag = FILTER_OPS[c["op"]]
        attrs = ' wildCard="*" singleChar="." escapeChar="!"' if c["op"] == "~" else (' matchCase="true"' if c["op"] in ("=", "!=") else "")
        parts.append(f"<{pfx}:{tag}{attrs}><{pfx}:{ref}>{escape(c['property'])}</{pfx}:{ref}><{pfx}:Literal>{escape(c['value'])}</{pfx}:Literal></{pfx}:{tag}>")
    if bbox:
        x0, y0, x1, y1 = (float(v) for v in bbox)
        if v2 and srs.upper() in ("EPSG:4326", "URN:OGC:DEF:CRS:EPSG::4326"):
            x0, y0, x1, y1 = y0, x0, y1, x1                    # EPSG:4326 is latitude first in WFS 2.0
        parts.append(f'<{pfx}:BBOX><gml:Envelope xmlns:gml="http://www.opengis.net/{"gml/3.2" if v2 else "gml"}" srsName="{escape(srs)}">'
                     f"<gml:lowerCorner>{x0:g} {y0:g}</gml:lowerCorner><gml:upperCorner>{x1:g} {y1:g}</gml:upperCorner></gml:Envelope></{pfx}:BBOX>")
    body = parts[0] if len(parts) == 1 else f"<{pfx}:And>{''.join(parts)}</{pfx}:And>"
    return f'<{pfx}:Filter xmlns:{pfx}="{ns}">{body}</{pfx}:Filter>'


def wfs_params(plan: FetchPlan, *, hits_only: bool = False, start: int = 0, count: int | None = None, fmt: str | None = None) -> dict:
    v2 = plan.version.startswith("2")
    p = {"service": "WFS", "version": plan.version, "request": "GetFeature", "typeNames" if v2 else "typeName": plan.layer, "srsName": plan.srs}
    if plan.conditions and plan.cql_filter:
        raise Refusal("give either a CQL filter or conditions (sent as OGC Filter XML), not both", "MAP-SVC-038")
    flt = {"FILTER": filter_xml(plan.conditions, plan.version, plan.bbox, plan.srs)} if plan.conditions else None
    if hits_only:
        p["resultType"] = "hits"
        return p | (flt or (({"CQL_FILTER": plan.cql_filter} if plan.cql_filter else {}) | _wfs_bbox(plan)))
    p["outputFormat"] = fmt or plan.output_format or "application/json"
    p["count" if v2 else "maxFeatures"] = int(count if count is not None else plan.page_size)
    p["startIndex"] = int(start)
    if plan.sort_by:
        p["sortBy"] = plan.sort_by
    if plan.properties:
        p["propertyName"] = ",".join(plan.properties)
    if flt:
        return p | flt
    if plan.cql_filter:
        p["CQL_FILTER"] = plan.cql_filter
    return p | _wfs_bbox(plan)


def _wfs_bbox(plan: FetchPlan) -> dict:
    """A box, as the `bbox` parameter. GeoServer refuses `bbox` together with `CQL_FILTER`, so with both the box goes inside the filter,
    which needs the layer's geometry field (from DescribeFeatureType, in plan.extra['geometry_field'])."""
    if not plan.bbox:
        return {}
    x0, y0, x1, y1 = plan.bbox
    if not plan.cql_filter:
        return {"bbox": f"{x0},{y0},{x1},{y1},{plan.srs}"}
    g = (plan.extra or {}).get("geometry_field")
    if not g:
        raise ServiceError("a filter and a bounding box together need the layer's geometry field name; run DescribeFeatureType first", "MAP-SVC-003")
    return {"CQL_FILTER": f"({plan.cql_filter}) AND BBOX({g},{x0},{y0},{x1},{y1},'{plan.srs}')"}


def count_from_xml(content: bytes) -> int | None:
    root = oh.safe_xml(content)
    if ln(root) in ("ExceptionReport", "ServiceExceptionReport"):
        raise ServiceError("the service returned an error: " + untrusted(" ".join(t.strip() for t in root.itertext() if t.strip()), 240), "MAP-SVC-013")
    for k in ("numberMatched", "numberOfFeatures"):
        v = root.get(k)
        if v is not None:
            return int(v) if v.isdigit() else None
    return None


def wfs_hits(http, profile: dict, plan: FetchPlan) -> int | None:
    r = oh.read_query(http, endpoint(profile, "wfs"), wfs_params(plan, hits_only=True), protocol="wfs", expect="xml")
    return count_from_xml(r.content)


def ogcapi_params(plan: FetchPlan, *, limit: int, offset: int = 0) -> dict:
    p = {"f": "json", "limit": int(limit)}
    if offset:
        p["offset"] = int(offset)
    if plan.bbox:
        p["bbox"] = ",".join(str(v) for v in plan.bbox)
    if plan.cql_filter:
        p["filter"] = plan.cql_filter
        p["filter-lang"] = "cql2-text"
    if plan.properties:
        p["properties"] = ",".join(plan.properties)
    return p


def ogcapi_hits(http, profile: dict, plan: FetchPlan) -> int | None:
    base = endpoint(profile, "ogcapi").rstrip("/")
    r = http.get(f"{base}/collections/{urllib.parse.quote(plan.layer, safe='')}/items", ogcapi_params(plan, limit=1), expect="json")
    d = json.loads(r.content)
    n = d.get("numberMatched")
    return int(n) if isinstance(n, int) else None


def _features_of(content: bytes) -> tuple[list, dict]:
    try:
        d = json.loads(content)
    except ValueError:
        raise ServiceError("the response is not valid JSON", "MAP-SVC-013")
    if not isinstance(d, dict) or d.get("type") != "FeatureCollection" or not isinstance(d.get("features"), list):
        if isinstance(d, dict) and (d.get("exceptions") or d.get("error")):
            raise ServiceError("the service returned an error: " + untrusted(json.dumps(d.get("exceptions") or d.get("error")), 240), "MAP-SVC-013")
        raise ServiceError("the response is not a GeoJSON FeatureCollection", "MAP-SVC-013")
    return d["features"], d


def fetch_pages(http, profile: dict, plan: FetchPlan, outdir: Path, *, hits: int | None, caps: dict | None = None, resume: bool = True, page_fn=None) -> FetchResult:
    """Download every page for a WFS or OGC API plan into outdir/pages/, with stable ordering, resumable, truncation detected and reported.

    `hits` is the count from `wfs_hits` or `ogcapi_hits`. Pages already on disk (with a matching record) are reused when resume is true.
    """
    res = FetchResult(hits=hits)
    pdir = Path(outdir) / "pages"
    pdir.mkdir(parents=True, exist_ok=True)
    state_p = Path(outdir) / "state.json"
    state = json.loads(state_p.read_text()) if resume and state_p.is_file() else {}
    cap = plan.max_features if plan.max_features else (hits if hits is not None else None)
    page = max(1, min(int(plan.page_size), PAGE_CEILING))
    fmt, kind = plan.output_format, "json"
    if plan.protocol == "wfs" and not fmt:
        fmt, kind = pick_format((caps or {}).get("formats") or [])
        if kind != "json":
            res.notes.append(f"the server offers no GeoJSON; pages are asked for as {fmt} and read back into GeoJSON")
    ids: set = set()
    start, n = 0, 0
    ep = endpoint(profile, plan.protocol)                  # 1.0.2 (F4): a WFS address is asked as saved; sub-paths are joined to it below
    base = ep.rstrip("/")
    next_url = None
    while True:
        n += 1
        if n > MAX_PAGES:
            res.truncated = True
            res.notes.append(f"stopped after {MAX_PAGES} pages")
            break
        want = page if cap is None else max(0, min(page, cap - res.received))
        if want == 0:
            break
        key = f"page-{n:04d}.geojson"
        ppath = pdir / key
        rec = (state.get("pages") or {}).get(key)
        if rec and ppath.is_file() and hashlib.sha256(ppath.read_bytes()).hexdigest() == rec["sha256"] and rec.get("start") == start and rec.get("asked") == want:
            content, url = ppath.read_bytes(), rec["url"]
            res.resumed_pages += 1
        else:
            if plan.protocol == "wfs":
                r = oh.read_query(http, ep, wfs_params(plan, start=start, count=want, fmt=fmt), protocol="wfs", expect="json" if kind == "json" else "any")
                if kind != "json":
                    r = type(r)(r.url, r.status, r.headers, page_to_geojson(r.content, kind), r.elapsed_ms, r.sha256, "application/geo+json")
            else:
                items = next_url or f"{base}/collections/{urllib.parse.quote(plan.layer, safe='')}/items"
                r = http.get(items, None if next_url else ogcapi_params(plan, limit=want, offset=start), expect="json")
            content, url = r.content, oh.redact_url(r.url)
            ppath.write_bytes(content)
        feats, doc = _features_of(content)
        if plan.protocol == "ogcapi":
            nxt = next((l.get("href") for l in doc.get("links", []) if l.get("rel") == "next"), None)
            if nxt:
                nxt_abs = urllib.parse.urljoin(base + "/", nxt)
                if urllib.parse.urlsplit(nxt_abs).hostname != urllib.parse.urlsplit(base).hostname:
                    raise Refusal("the service pointed to a different host for the next page; that address is not followed", "MAP-SVC-001")
                next_url = nxt_abs
            else:
                next_url = None
            if res.hits is None and isinstance(doc.get("numberMatched"), int):
                res.hits = doc["numberMatched"]
                cap = plan.max_features if plan.max_features else res.hits
        for f in feats:
            fid = f.get("id")
            if fid is not None:
                if fid in ids:
                    res.duplicates += 1
                ids.add(fid)
        res.pages.append({"n": n, "path": str(ppath), "count": len(feats), "sha256": hashlib.sha256(content).hexdigest(), "url": url, "start": start, "asked": want})
        state.setdefault("pages", {})[key] = res.pages[-1]
        state_p.write_text(json.dumps(state))
        res.received += len(feats)
        if page_fn:
            page_fn(res)
        if not feats:
            break
        if len(feats) < want and (res.hits is None or res.received < res.hits) and (cap is None or res.received < cap):
            if plan.protocol == "ogcapi" and next_url:
                pass
            elif res.hits is not None and res.received < res.hits:
                res.server_limit = len(feats)                      # the server gave fewer than asked and there is more: its own page limit
                page = len(feats)
                res.notes.append(f"the server returns at most {len(feats)} features per request; paging continues in steps of {len(feats)}")
            else:
                break
        start += len(feats)
        if cap is not None and res.received >= cap:
            break
        if plan.protocol == "ogcapi" and not next_url and len(feats) < want:
            break
    res.truncated = bool(res.hits is not None and res.received < res.hits and not (plan.max_features and res.received >= plan.max_features)) or res.truncated
    return res


# ---------------------------------------------------------------------------------------------------------------- WMS and tiles
def _swap_for_130(version: str, crs: str, bbox):
    if version.startswith("1.3") and crs.upper() == "EPSG:4326":
        x0, y0, x1, y1 = bbox
        return [y0, x0, y1, x1]
    return bbox


def wms_getmap_params(profile: dict, layers: list[str], bbox: list, *, width: int = 600, height: int = 400, fmt: str = "image/png", crs: str = "CRS:84", version: str | None = None, styles=None, transparent: bool = True,
                      time: str | None = None) -> dict:
    ver = version or (profile.get("protocols") or {}).get("wms") or "1.3.0"
    width, height = max(16, min(int(width), 1024)), max(16, min(int(height), 1024))
    if ver.startswith("1.3"):
        crs_name, srs_key = crs, "CRS"
    else:
        crs_name, srs_key = ("EPSG:4326" if crs.upper() == "CRS:84" else crs), "SRS"
    bb = _swap_for_130(ver, crs_name, bbox)
    tm_ = check_time(time)
    return {"service": "WMS", "version": ver, "request": "GetMap", "layers": ",".join(layers), "styles": ",".join(styles or [""] * len(layers)), srs_key: crs_name,
            "bbox": ",".join(str(v) for v in bb), "width": width, "height": height, "format": fmt, "transparent": "true" if transparent else "false",
            **({"time": tm_} if tm_ else {})}


def wms_preview(http, profile: dict, layers: list[str], bbox: list, out: Path, **kw) -> dict:
    r = http.get(endpoint(profile, "wms"), wms_getmap_params(profile, layers, bbox, **kw), expect="image")
    Path(out).write_bytes(r.content)
    return {"path": str(out), "bytes": len(r.content), "content_type": r.content_type, "sha256": r.sha256, "url": oh.redact_url(r.url)}


def wms_legend(http, profile: dict, layer: str, out: Path, *, fmt: str = "image/png", version: str | None = None) -> dict:
    ver = version or (profile.get("protocols") or {}).get("wms") or "1.3.0"
    r = http.get(endpoint(profile, "wms"), {"service": "WMS", "version": ver, "request": "GetLegendGraphic", "layer": layer, "format": fmt}, expect="image")
    Path(out).write_bytes(r.content)
    entries: list = []
    try:
        j = http.get(endpoint(profile, "wms"), {"service": "WMS", "version": ver, "request": "GetLegendGraphic", "layer": layer, "format": "application/json"}, expect="json")
        for lg in json.loads(j.content).get("Legend", []):
            for rule in lg.get("rules", []):
                entries.append({"label": untrusted(rule.get("title") or rule.get("name"), 80)})
    except (ServiceError, ValueError):
        pass
    return {"path": str(out), "bytes": len(r.content), "sha256": r.sha256, "entries": entries[:50]}


def wms_feature_info(http, profile: dict, layers: list[str], lon: float, lat: float, *, version: str | None = None, info_format: str = "application/json") -> dict:
    d = 0.01
    bbox = [lon - d, lat - d, lon + d, lat + d]
    p = wms_getmap_params(profile, layers, bbox, width=101, height=101)
    ver = p["version"]
    for k in ("request", "transparent"):
        p.pop(k, None)
    p.update({"request": "GetFeatureInfo", "query_layers": ",".join(layers), "info_format": info_format, "feature_count": 5})
    p.update({"i": 50, "j": 50} if ver.startswith("1.3") else {"x": 50, "y": 50})
    r = http.get(endpoint(profile, "wms"), p, expect="any")
    props: list = []
    try:
        for f in json.loads(r.content).get("features", [])[:5]:
            props.append({k: (untrusted(v, 120) if isinstance(v, str) else v) for k, v in list((f.get("properties") or {}).items())[:40]})
    except ValueError:
        return {"features": [], "raw_text": untrusted(r.text, 400), "url": oh.redact_url(r.url)}
    return {"features": props, "url": oh.redact_url(r.url)}


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v, s = 0, 0
    while True:
        if i >= len(b) or s > 63:
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if not c & 0x80:
            return v, i
        s += 7


def mvt_layer_names(content: bytes, limit: int = 64) -> list[str]:
    """The source-layer names inside one Mapbox Vector Tile (0.15.0): field 3 of the tile is a layer, field 1 of a layer its name. Gzip is
    undone first. A malformed tile gives the names read so far; nothing else of the tile is decoded."""
    import gzip
    b = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
    names, i = [], 0
    try:
        while i < len(b) and len(names) < limit:
            key, i = _varint(b, i)
            field_, wt = key >> 3, key & 7
            if wt == 2:
                n, i = _varint(b, i)
                if i + n > len(b):                                   # a length past the end: the tile is cut here
                    break
                chunk, i = b[i:i + n], i + n
                if field_ == 3:
                    j = 0
                    while j < len(chunk):
                        k2, j = _varint(chunk, j)
                        if k2 & 7 == 2:
                            m, j = _varint(chunk, j)
                            if k2 >> 3 == 1:
                                names.append(untrusted(chunk[j:j + m].decode("utf-8", "replace"), 80))
                                break
                            j += m
                        elif k2 & 7 == 0:
                            _, j = _varint(chunk, j)
                        elif k2 & 7 == 5:
                            j += 4
                        elif k2 & 7 == 1:
                            j += 8
                        else:
                            break
            elif wt == 0:
                _, i = _varint(b, i)
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
    except (ValueError, IndexError):
        pass
    return names


def tilejson_layers(http, url: str) -> dict:
    """MVT source-layer names and their fields from a TileJSON document (vector_layers), with the tile addresses, zoom range and attribution
    (0.15.0). Text from the document is data, shortened."""
    r = http.get(url, None, expect="json")
    try:
        d = json.loads(r.content)
    except ValueError:
        raise ServiceError("the TileJSON document is not valid JSON", "MAP-SVC-013")
    if not isinstance(d, dict):
        raise ServiceError("the TileJSON document is not an object", "MAP-SVC-013")
    vl = []
    for v in (d.get("vector_layers") or [])[:64]:
        if isinstance(v, dict) and v.get("id"):
            fields = v.get("fields") or {}
            vl.append({"id": untrusted(str(v["id"]), 80), "fields": [untrusted(str(k), 60) for k in list(fields)[:40]] if isinstance(fields, dict) else [],
                       "minzoom": v.get("minzoom"), "maxzoom": v.get("maxzoom"), "description": untrusted(v.get("description"), 160) or None})
    return {"tilejson": untrusted(d.get("tilejson"), 10), "tiles": [oh.redact_url(str(x)) for x in (d.get("tiles") or [])[:4]], "minzoom": d.get("minzoom"),
            "maxzoom": d.get("maxzoom"), "attribution": untrusted(d.get("attribution"), 200) or None, "vector_layers": vl, "url": oh.redact_url(r.url)}


# ---------------------------------------------------------------------------------------------------------------- 0.15.0: GeoServer REST, read-only
REST_WHAT = ("workspaces", "layers", "datastores", "featuretypes", "styles", "layergroups")
_REST_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")


def geoserver_rest(http, profile: dict, what: str, workspace: str | None = None, *, auth=None) -> dict:
    """GeoServer's REST configuration API, read-only (GET of a listing, never a change): workspaces, layers, datastores, feature types, styles
    or layer groups, for the whole server or one workspace. The names come back as data; descriptions are not followed. A server that keeps
    REST behind a login answers 401 or 403, reported as needing a registered credential."""
    if (profile.get("type") or "") != "geoserver":
        raise Refusal("REST discovery is for a GeoServer profile (type: geoserver)", "MAP-SVC-040")
    if what not in REST_WHAT:
        raise Refusal(f"'{untrusted(what, 30)}' is not a listing this plugin reads; one of {', '.join(REST_WHAT)}", "MAP-SVC-040")
    if workspace is not None and not _REST_NAME.match(workspace):
        raise Refusal(f"'{untrusted(workspace, 40)}' is not a workspace name", "MAP-SVC-040")
    if what in ("datastores", "featuretypes") and not workspace:
        raise Refusal(f"{what} are listed per workspace; give workspace=", "MAP-SVC-040")
    base = profile["base_url"].rstrip("/") + "/rest"
    path = {"workspaces": "/workspaces.json", "layers": "/layers.json", "styles": "/styles.json", "layergroups": "/layergroups.json",
            "datastores": "/workspaces/{w}/datastores.json", "featuretypes": "/workspaces/{w}/featuretypes.json"}[what]
    if workspace and what in ("layers", "styles", "layergroups"):
        path = "/workspaces/{w}" + path
    url = base + path.replace("{w}", urllib.parse.quote(workspace or "", safe=""))
    r = http.get(url, None, expect="json", **({"auth": auth} if auth is not None else {}))
    try:
        d = json.loads(r.content)
    except ValueError:
        raise ServiceError("the REST answer is not valid JSON", "MAP-SVC-013")
    names = []

    def collect(o):
        if isinstance(o, dict):
            if "name" in o and isinstance(o["name"], str) and len(o) <= 3:
                names.append(untrusted(o["name"], 120))
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for v in o:
                collect(v)
    collect(d)
    return {"what": what, "workspace": workspace, "names": names[:2000], "count": len(names), "url": oh.redact_url(r.url)}


def tile_probe(http, url_template: str, *, z: int = 4, x: int = 3, y: int = 6) -> dict:
    url = url_template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{-y}", str((1 << z) - 1 - y))
    r = http.get(url, None, expect="tile")
    h = r.headers
    out = {"url": oh.redact_url(r.url), "status": r.status, "bytes": len(r.content), "content_type": r.content_type, "sha256": r.sha256,
           "cache": {k: h.get(k) for k in ("cache-control", "expires", "etag", "age", "last-modified") if h.get(k)}, "server": untrusted(h.get("server"), 60) or None,
           "attribution_hint": untrusted(h.get("x-attribution") or h.get("attribution"), 120) or None}
    if "protobuf" in (r.content_type or "") or "mapbox-vector-tile" in (r.content_type or "") or url.split("?")[0].endswith((".pbf", ".mvt")):
        out["mvt_layers"] = mvt_layer_names(r.content)              # 0.15.0: the source-layer names a MapLibre layer must name
    return out


def cors_check(http, url: str, origin: str = "https://example.org") -> dict:
    r = http.get(url, None, expect="any", origin=origin)
    h = r.headers
    allow = h.get("access-control-allow-origin")
    return {"url": oh.redact_url(r.url), "origin": origin, "status": r.status, "allow_origin": untrusted(allow, 80) or None, "allowed": allow in ("*", origin),
            "allow_methods": untrusted(h.get("access-control-allow-methods"), 80) or None, "vary_origin": "origin" in (h.get("vary") or "").lower(),
            "mixed_content": url.startswith("http://")}
