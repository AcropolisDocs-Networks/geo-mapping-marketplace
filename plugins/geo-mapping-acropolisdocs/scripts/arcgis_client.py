#!/usr/bin/env python3
"""arcgis_client: Esri REST FeatureServer and MapServer, read only (spec 7.3 `arcgis-rest-client`).

Layer metadata, feature counts, paged `query` with a stable `orderByFields`, `exceededTransferLimit` and `maxRecordCount` honored (a short page is
never taken for the end), `f=geojson` with a fallback to Esri JSON on servers that lack it, `export` for a preview image, and the layer legend.
Shares `FetchPlan`, `FetchResult` and the safety layer with `ogc_client`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ogc_http as oh  # noqa: E402
import ogc_client as oc  # noqa: E402
from ogc_http import ServiceError, untrusted  # noqa: E402
from ogc_client import FetchPlan, FetchResult  # noqa: E402

ESRI_TYPES = {"esriFieldTypeString": "string", "esriFieldTypeInteger": "integer", "esriFieldTypeSmallInteger": "integer", "esriFieldTypeOID": "integer", "esriFieldTypeDouble": "number",
              "esriFieldTypeSingle": "number", "esriFieldTypeDate": "datetime", "esriFieldTypeGlobalID": "string", "esriFieldTypeGUID": "string"}
GEOM_TYPES = {"esriGeometryPoint": "Point", "esriGeometryMultipoint": "MultiPoint", "esriGeometryPolyline": "MultiLineString", "esriGeometryPolygon": "MultiPolygon", "esriGeometryEnvelope": "Polygon"}
MAX_RECORD_CEILING = 20_000


def _json(r) -> dict:
    try:
        d = json.loads(r.content)
    except ValueError:
        raise ServiceError("the response is not valid JSON", "MAP-SVC-013")
    if isinstance(d, dict) and d.get("error"):
        e = d["error"]
        code = e.get("code") if isinstance(e, dict) else None
        if code in (498, 499, 403, 401):
            raise ServiceError("the service needs a token; this plugin does not use authentication (not retried)", "MAP-SVC-018", auth=True, status=int(code))
        raise ServiceError("the service returned an error: " + untrusted(json.dumps(e), 240), "MAP-SVC-013")
    return d


def url_of(profile: dict, path: str) -> str:
    base = oc.endpoint(profile, "arcgis").rstrip("/")
    return f"{base}/{path.strip('/')}" if path else base


def service_info(http, profile: dict, path: str = "") -> dict:
    """Folders, services or layers at an address under the REST root."""
    r = http.get(url_of(profile, path), {"f": "json"}, expect="json")
    d = _json(r)
    return {"service": "ArcGIS REST", "version": d.get("currentVersion"), "title": untrusted(d.get("mapName") or d.get("serviceDescription") or path or "services", 160),
            "abstract": untrusted(d.get("description") or d.get("serviceDescription"), 500), "keywords": [], "fees": None, "access_constraints": untrusted(d.get("accessInformation"), 300) or None,
            "copyright": untrusted(d.get("copyrightText"), 300) or None, "max_record_count": d.get("maxRecordCount"),
            "folders": [untrusted(f, 80) for f in d.get("folders", [])][:100], "services": [{"name": untrusted(s.get("name"), 100), "type": s.get("type")} for s in d.get("services", [])][:200],
            "layers": [{"name": str(l.get("id")), "title": untrusted(l.get("name"), 120), "abstract": "", "crs": [], "bbox": None} for l in d.get("layers", [])][:300],
            "_response": oh.response_record(http, r)}


def layer_info(http, profile: dict, layer_path: str) -> dict:
    r = http.get(url_of(profile, layer_path), {"f": "json"}, expect="json")
    d = _json(r)
    if "fields" not in d:
        raise ServiceError("this address is not a layer with fields (give the path to a layer, such as Folder/Service/MapServer/0)", "MAP-SVC-003")
    fields = [{"name": f.get("name"), "type": ESRI_TYPES.get(f.get("type"), "string"), "nillable": bool(f.get("nullable", True)), "alias": untrusted(f.get("alias"), 80)} for f in d["fields"]]
    oid = next((f["name"] for f in d["fields"] if f.get("type") == "esriFieldTypeOID"), None)
    ext = d.get("extent") or {}
    sr = (ext.get("spatialReference") or {}).get("latestWkid") or (ext.get("spatialReference") or {}).get("wkid")
    mrc = int(d.get("maxRecordCount") or 1000)
    return {"typename": layer_path, "name": untrusted(d.get("name"), 120), "fields": fields, "oid_field": oid, "geometry": {"name": "shape", "type": GEOM_TYPES.get(d.get("geometryType"), d.get("geometryType"))},
            "max_record_count": mrc, "extent": [ext.get(k) for k in ("xmin", "ymin", "xmax", "ymax")] if ext else None, "extent_wkid": sr,
            "supports_geojson": "geojson" in (d.get("supportedQueryFormats") or "").lower(), "supports_pagination": bool((d.get("advancedQueryCapabilities") or {}).get("supportsPagination", True)),
            "copyright": untrusted(d.get("copyrightText"), 300) or None, "description": untrusted(d.get("description"), 500) or None, "capabilities": untrusted(d.get("capabilities"), 80),
            "_response": oh.response_record(http, r)}


def _wkid(srs: str | None) -> str:
    """The numeric id ArcGIS takes for outSR: 'EPSG:3857' -> '3857'; anything that is not an EPSG code falls back to 4326."""
    m = re.match(r"^\s*(?:EPSG|ESRI)\s*:\s*(\d+)\s*$", str(srs or ""), re.I)
    return m.group(1) if m else "4326"


def query_params(plan: FetchPlan, *, count_only: bool = False, offset: int = 0, size: int | None = None, fmt: str = "geojson", oid_field: str | None = None) -> dict:
    p = {"where": plan.cql_filter or "1=1", "outFields": ",".join(plan.properties) if plan.properties else "*", "returnGeometry": "true", "outSR": _wkid(plan.srs), "f": fmt}
    if plan.bbox:
        p.update({"geometry": ",".join(str(v) for v in plan.bbox), "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"})
    if count_only:
        return {k: v for k, v in p.items() if k not in ("outFields", "outSR")} | {"returnCountOnly": "true", "f": "json"}
    order = plan.sort_by or oid_field
    if order:
        p["orderByFields"] = order
    p["resultOffset"] = int(offset)
    p["resultRecordCount"] = int(size or plan.page_size)
    return p


def hits(http, profile: dict, plan: FetchPlan) -> int | None:
    r = oh.read_query(http, url_of(profile, plan.layer) + "/query", query_params(plan, count_only=True), protocol="arcgis", expect="json")
    n = _json(r).get("count")
    return int(n) if isinstance(n, int) else None


def date_fields(doc_or_layer: dict) -> list[str]:
    """The names of the esriFieldTypeDate fields in an Esri JSON response's `fields` or a layer description."""
    return [f.get("name") for f in (doc_or_layer or {}).get("fields", []) if isinstance(f, dict) and f.get("type") == "esriFieldTypeDate" and f.get("name")]


def epoch_ms_to_iso(v):
    """An Esri date (milliseconds since 1970-01-01 UTC) as an ISO-8601 string; None, strings and NaN pass through unchanged."""
    if v is None or isinstance(v, str) or isinstance(v, bool):
        return v
    try:
        ms = float(v)
    except (TypeError, ValueError):
        return v
    if ms != ms:
        return None
    try:
        t = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return v
    return t.isoformat().replace("+00:00", "Z") if ms % 1000 else t.strftime("%Y-%m-%dT%H:%M:%SZ")


def convert_dates(features: list, fields: list[str]) -> int:
    """Rewrite the epoch-millisecond values of `fields` in place (GeoJSON `properties` or Esri `attributes`) as ISO-8601. Returns the
    number of values changed. ArcGIS sends dates as integers in every format it offers, so a 1.7e12 would be classified as a number and
    the legend would show class breaks in milliseconds."""
    n = 0
    for f in features:
        props = f.get("properties") if "properties" in f else f.get("attributes")
        if not isinstance(props, dict):
            continue
        for k in fields:
            if k in props and props[k] is not None and not isinstance(props[k], str):
                props[k] = epoch_ms_to_iso(props[k])
                n += 1
    return n


def esri_to_geojson(doc: dict, geometry_type: str | None = None) -> dict:
    """Esri JSON features to a GeoJSON FeatureCollection (points, multipoints, polylines, polygons with holes). Date fields (the doc's
    own `fields` say which) become ISO-8601 strings."""
    from shapely.geometry import Polygon, MultiPolygon, mapping, Point, MultiPoint, LineString, MultiLineString
    out = []
    dates = date_fields(doc)
    for f in doc.get("features", []):
        g, a = f.get("geometry"), dict(f.get("attributes") or {})
        for k in dates:
            if k in a and a[k] is not None and not isinstance(a[k], str):
                a[k] = epoch_ms_to_iso(a[k])
        geom = None
        if g:
            if "x" in g and "y" in g:
                geom = Point(g["x"], g["y"])
            elif "points" in g:
                geom = MultiPoint([tuple(p[:2]) for p in g["points"]])
            elif "paths" in g:
                geom = MultiLineString([[tuple(p[:2]) for p in path] for path in g["paths"]]) if len(g["paths"]) != 1 else LineString([tuple(p[:2]) for p in g["paths"][0]])
            elif "rings" in g:
                rings = [[tuple(p[:2]) for p in r] for r in g["rings"] if len(r) >= 4]
                ext = [r for r in rings if _signed_area(r) < 0]            # Esri exterior rings run clockwise
                holes = [r for r in rings if _signed_area(r) >= 0]
                polys = []
                for e in ext:
                    ep = Polygon(e)
                    mine = [h for h in holes if ep.contains(Polygon(h).representative_point())]
                    polys.append(Polygon(e, mine))
                geom = polys[0] if len(polys) == 1 else MultiPolygon(polys) if polys else None
        out.append({"type": "Feature", "id": a.get("OBJECTID") or a.get("FID") or a.get("objectid"), "properties": a, "geometry": mapping(geom) if geom is not None else None})
    return {"type": "FeatureCollection", "features": out, "exceededTransferLimit": bool(doc.get("exceededTransferLimit"))}


def _signed_area(r) -> float:
    return sum((x1 * y2 - x2 * y1) for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1])) / 2.0


def fetch_pages(http, profile: dict, plan: FetchPlan, outdir: Path, *, hits: int | None, layer: dict | None = None, resume: bool = True, page_fn=None) -> FetchResult:
    res = FetchResult(hits=hits)
    pdir = Path(outdir) / "pages"
    pdir.mkdir(parents=True, exist_ok=True)
    state_p = Path(outdir) / "state.json"
    state = json.loads(state_p.read_text()) if resume and state_p.is_file() else {}
    mrc = min((layer or {}).get("max_record_count") or 1000, MAX_RECORD_CEILING)
    page = max(1, min(plan.page_size, mrc))
    if plan.page_size > mrc:
        res.notes.append(f"the layer allows {mrc} records per request; the page size was lowered from {plan.page_size}")
        res.server_limit = mrc
    fmt = "geojson" if (layer or {}).get("supports_geojson", True) else "json"
    oid = (layer or {}).get("oid_field")
    if not (plan.sort_by or oid):
        res.findings.append({"id": "MAP-SVC-006", "severity": "WARN", "message": "no ordering field is known for this layer, so paging may repeat or miss records"})
    cap = plan.max_features if plan.max_features else hits
    url = url_of(profile, plan.layer) + "/query"
    # the layer description names its date fields (esriFieldTypeDate, mapped to "datetime" by layer_info); f=geojson still sends them as
    # epoch milliseconds, so they are rewritten as ISO-8601 on every page and the note says which fields changed
    dates = [f["name"] for f in (layer or {}).get("fields", []) if f.get("type") == "datetime" and f.get("name")]
    converted: set = set()
    ids: set = set()
    start, n = 0, 0
    while True:
        n += 1
        if n > oc.MAX_PAGES:
            res.truncated = True
            res.notes.append(f"stopped after {oc.MAX_PAGES} pages")
            break
        want = page if cap is None else max(0, min(page, cap - res.received))
        if want == 0:
            break
        key = f"page-{n:04d}.geojson"
        ppath = pdir / key
        rec = (state.get("pages") or {}).get(key)
        if rec and ppath.is_file() and hashlib.sha256(ppath.read_bytes()).hexdigest() == rec["sha256"] and rec.get("start") == start and rec.get("asked") == want:
            content, purl = ppath.read_bytes(), rec["url"]
            res.resumed_pages += 1
        else:
            r = oh.read_query(http, url, query_params(plan, offset=start, size=want, fmt=fmt, oid_field=oid), protocol="arcgis", expect="json")
            doc = _json(r)
            if fmt == "json":
                page_dates = list(dict.fromkeys(dates + date_fields(doc)))
                doc = esri_to_geojson(doc, (layer or {}).get("geometry", {}).get("type"))      # converts the dates the page itself declares
                convert_dates(doc.get("features", []), page_dates)                                # and the ones only the layer declares
                first = (doc.get("features") or [{}])[0].get("properties") or {}
                converted.update(k for k in page_dates if k in first)
            elif doc.get("type") != "FeatureCollection":
                raise ServiceError("the service did not return GeoJSON for f=geojson", "MAP-SVC-013")
            elif dates and convert_dates(doc.get("features", []), dates):
                converted.update(dates)
            exceeded = bool(doc.get("exceededTransferLimit") or (doc.get("properties") or {}).get("exceededTransferLimit"))
            doc["exceededTransferLimit"] = exceeded
            content = json.dumps(doc, separators=(",", ":")).encode()
            purl = oh.redact_url(r.url)
            ppath.write_bytes(content)
        doc = json.loads(content)
        feats = doc.get("features", [])
        exceeded = bool(doc.get("exceededTransferLimit"))
        for f in feats:
            fid = f.get("id")
            if fid is not None:
                if fid in ids:
                    res.duplicates += 1
                ids.add(fid)
        res.pages.append({"n": n, "path": str(ppath), "count": len(feats), "sha256": hashlib.sha256(content).hexdigest(), "url": purl, "start": start, "asked": want, "exceeded": exceeded})
        state.setdefault("pages", {})[key] = res.pages[-1]
        state_p.write_text(json.dumps(state))
        res.received += len(feats)
        if page_fn:
            page_fn(res)
        if not feats:
            break
        if exceeded and len(feats) < want:
            res.server_limit = len(feats)
            page = len(feats)
            res.notes.append(f"the server returned {len(feats)} records and said more remain; paging continues")
        start += len(feats)
        if cap is not None and res.received >= cap:
            break
        if not exceeded and len(feats) < want and hits is None:
            break
        if not exceeded and len(feats) < want and hits is not None and res.received >= hits:
            break
    res.truncated = bool(hits is not None and res.received < hits and not (plan.max_features and res.received >= plan.max_features)) or res.truncated
    if converted:
        res.notes.append("date field(s) " + ", ".join(sorted(converted)) + " were sent as milliseconds since 1970 and are written as ISO-8601 dates (UTC)")
    return res


def export_preview(http, profile: dict, service_path: str, bbox: list, out: Path, *, size: int = 600) -> dict:
    size = max(16, min(int(size), 1024))
    p = {"bbox": ",".join(str(v) for v in bbox), "bboxSR": "4326", "imageSR": "3857", "size": f"{size},{size}", "format": "png32", "transparent": "true", "f": "image"}
    r = http.get(url_of(profile, service_path) + "/export", p, expect="image")
    Path(out).write_bytes(r.content)
    return {"path": str(out), "bytes": len(r.content), "sha256": r.sha256, "url": oh.redact_url(r.url)}


MAX_EXPORT_PX = 4096        # the usual ArcGIS Server maximum image size per side


def export_image(http, profile: dict, service_path: str, bbox: list, sr: int, out: Path, *, cell: float | None = None, max_px: int = MAX_EXPORT_PX,
                 layers: str | None = None, fmt: str = "png", transparent: bool = False, dpi: int = 96, size: tuple[int, int] | None = None) -> dict:
    """A georeferenced picture from a MapServer `export`: the image is sized to the box's own shape (square pixels) and the extent recorded is the
    one the server says it drew, read from `f=json`, never the one asked for.

    ArcGIS widens the box to the image's shape when the two differ. A square image of a tall box comes back covering a wider area, so placing it
    on the requested box squeezes everything toward the middle: on the 2026-11 Day 2 map that was 9% sideways, about 24 km at a tile edge, and the
    forest stopped short of the coast. `adjusted` says the server changed the box; `extent` is always the right one to place the picture on.
    `size` overrides the computed size (a fixed-size request); the returned extent still says where the picture is."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        raise ServiceError("the export box is empty or inverted", "MAP-SVC-003")
    if size:
        W, H = int(size[0]), int(size[1])
    elif cell:
        W, H = max(1, round(w / cell)), max(1, round(h / cell))
    else:
        k = max_px / max(w, h)
        W, H = max(1, round(w * k)), max(1, round(h * k))
    if max(W, H) > max_px:
        raise ServiceError(f"a {W} x {H} image is larger than the {max_px}-pixel limit; split the box into tiles or use a larger cell", "MAP-SVC-003")
    p = {"bbox": ",".join(repr(v) for v in (x0, y0, x1, y1)), "bboxSR": str(sr), "imageSR": str(sr), "size": f"{W},{H}", "format": fmt,
         "transparent": "true" if transparent else "false", "dpi": str(dpi), "f": "json"}
    if layers:
        p["layers"] = layers
    url = url_of(profile, service_path) + "/export"
    meta = _json(http.get(url, p, expect="json"))
    e = meta.get("extent") or {}
    if not all(k in e for k in ("xmin", "ymin", "xmax", "ymax")):
        raise ServiceError("the server did not say which extent it drew, so the picture cannot be placed", "MAP-SVC-003")
    got = [float(e["xmin"]), float(e["ymin"]), float(e["xmax"]), float(e["ymax"])]
    Wr, Hr = int(meta.get("width") or W), int(meta.get("height") or H)
    r = http.get(url, p | {"f": "image"}, expect="image")
    Path(out).write_bytes(r.content)
    cw, ch = (got[2] - got[0]) / Wr, (got[3] - got[1]) / Hr
    adjusted = any(abs(a - b) > 0.5 * min(cw, ch) for a, b in zip(got, (x0, y0, x1, y1)))
    return {"path": str(out), "bbox_requested": [x0, y0, x1, y1], "extent": got, "sr": int(sr), "width": Wr, "height": Hr, "cell_size": [cw, ch],
            "adjusted": adjusted, "bytes": len(r.content), "sha256": r.sha256, "url": oh.redact_url(r.url)}


def legend(http, profile: dict, service_path: str, out: Path | None = None) -> dict:
    r = http.get(url_of(profile, service_path) + "/legend", {"f": "json"}, expect="json")
    d = _json(r)
    entries = []
    for lay in d.get("layers", []):
        for lg in lay.get("legend", []):
            entries.append({"layer": untrusted(lay.get("layerName"), 80), "label": untrusted(lg.get("label"), 80)})
    if out:
        Path(out).write_text(json.dumps(entries))
    return {"entries": entries[:100], "url": oh.redact_url(r.url)}
