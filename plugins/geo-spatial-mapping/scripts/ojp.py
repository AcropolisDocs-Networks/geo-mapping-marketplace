#!/usr/bin/env python3
"""ojp: Open Journey Planner 2.0 (opentransportdata.swiss) stops and trips for a map layer (3.3.0).

OJP is XML sent by POST to https://api.opentransportdata.swiss/ojp20 with the person's bearer token (`cred:opentransportdata`). The library entry
`ref.public-service.ch.transit.opentransportdata` declares that POST; `library_request` builds the body here and reads the answer here:

  stops   OJPLocationInformationRequest: stops by name and/or inside a box (bbox), up to `limit`
          -> one point per stop: stop_ref, name, modes, probability
  trips   OJPTripRequest: from a stop (from_ref: a DiDok number such as 8507000) or a point (from_lon, from_lat) to another, departing at
          `time` (ISO, default now), up to `results` trips, with the legs' track (IncludeLegProjection)
          -> one line per leg: trip, leg, kind (timed, transfer, continuous), mode, line, from, to, departure, arrival; geometry from the
             leg's track, else a straight line between its ends when they have positions

Values reach the XML only as text nodes (lxml builds the tree; nothing is pasted into markup). The answer is parsed with entities and the network
off (ogc_http.safe_xml) and read by local names, so the namespace prefixes a server chooses do not matter. An answer's ErrorCondition is
reported, never treated as data.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

OJP_NS = "http://www.vdv.de/ojp"
SIRI_NS = "http://www.siri.org.uk/siri"
REQUESTOR = "acropolisdocs-geo-mapping"
REF_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,60}$")
KINDS = ("stops", "trips")


class OjpError(Exception):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _el(parent, tag: str, text=None, ns: str = OJP_NS):
    from lxml import etree
    e = etree.SubElement(parent, "{" + ns + "}" + tag)
    if text is not None:
        e.text = str(text)
    return e


def _envelope(request_tag: str, message_id: str):
    from lxml import etree
    root = etree.Element("{" + OJP_NS + "}OJP", nsmap={None: OJP_NS, "siri": SIRI_NS}, version="2.0")
    sr = _el(_el(root, "OJPRequest"), "ServiceRequest", ns=SIRI_NS)
    now = _now()
    _el(sr, "RequestTimestamp", now, SIRI_NS)
    _el(sr, "RequestorRef", REQUESTOR, SIRI_NS)
    req = _el(sr, request_tag)
    _el(req, "RequestTimestamp", now, SIRI_NS)
    _el(req, "MessageIdentifier", message_id, SIRI_NS)
    return root, req


def _float(v, name: str, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise OjpError(f"{name} is a number") from None
    if not lo <= x <= hi:
        raise OjpError(f"{name} is out of range")
    return x


def stops_body(values: dict) -> bytes:
    """The LocationInformationRequest for stops by name and/or box."""
    from lxml import etree
    name = (values.get("name") or "").strip()
    if len(name) > 100:
        raise OjpError("name is at most 100 characters")
    has_box = all(k in values for k in ("west", "south", "east", "north"))
    if not name and not has_box:
        raise OjpError("give name=... or bbox=W,S,E,N")
    limit = int(_float(values.get("limit") or 50, "limit", 1, 500))
    root, req = _envelope("OJPLocationInformationRequest", "LIR-1")
    ii = _el(req, "InitialInput")
    if name:
        _el(ii, "Name", name)
    if has_box:
        w, s = _float(values["west"], "west", -180, 180), _float(values["south"], "south", -90, 90)
        e, n = _float(values["east"], "east", -180, 180), _float(values["north"], "north", -90, 90)
        rect = _el(_el(ii, "GeoRestriction"), "Rectangle")
        ul, lr = _el(rect, "UpperLeft"), _el(rect, "LowerRight")
        _el(ul, "Longitude", f"{w:.6f}", SIRI_NS)
        _el(ul, "Latitude", f"{n:.6f}", SIRI_NS)
        _el(lr, "Longitude", f"{e:.6f}", SIRI_NS)
        _el(lr, "Latitude", f"{s:.6f}", SIRI_NS)
    rs = _el(req, "Restrictions")
    _el(rs, "Type", "stop")
    _el(rs, "NumberOfResults", limit)
    _el(rs, "IncludePtModes", "true")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _place(parent, values: dict, side: str):
    pr = _el(parent, "PlaceRef")
    ref = (values.get(f"{side}_ref") or "").strip()
    if ref:
        if not REF_RE.match(ref):
            raise OjpError(f"{side}_ref is a stop reference such as 8507000")
        _el(pr, "StopPlaceRef", ref)
    elif f"{side}_lon" in values and f"{side}_lat" in values:
        gp = _el(pr, "GeoPosition")
        _el(gp, "Longitude", f"{_float(values[f'{side}_lon'], f'{side}_lon', -180, 180):.6f}", SIRI_NS)
        _el(gp, "Latitude", f"{_float(values[f'{side}_lat'], f'{side}_lat', -90, 90):.6f}", SIRI_NS)
    else:
        raise OjpError(f"give {side}_ref=STOP or {side}_lon and {side}_lat")
    _el(_el(pr, "Name"), "Text", (values.get(f"{side}_name") or side)[:100])


def trips_body(values: dict) -> bytes:
    from lxml import etree
    t = (values.get("time") or "").strip() or _now()
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?$", t):
        raise OjpError("time is ISO, such as 2026-11-23T08:00:00Z")
    results = int(_float(values.get("results") or 3, "results", 1, 10))
    root, req = _envelope("OJPTripRequest", "TR-1")
    o = _el(req, "Origin")
    _place(o, values, "from")
    _el(o, "DepArrTime", t)
    _place(_el(req, "Destination"), values, "to")
    p = _el(req, "Params")
    _el(p, "NumberOfResults", results)
    _el(p, "IncludeLegProjection", "true")
    _el(p, "IncludeIntermediateStops", "false")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def body(kind: str, values: dict) -> bytes:
    if kind == "stops":
        return stops_body(values)
    if kind == "trips":
        return trips_body(values)
    raise OjpError(f"OJP requests here are {', '.join(KINDS)}")


# ---------------------------------------------------------------------------------------------------------------- answers
def _ln(e) -> str:
    return e.tag.split("}", 1)[-1] if isinstance(e.tag, str) else ""


def _find(e, *path):
    cur = [e]
    for name in path:
        cur = [c for x in cur for c in x if _ln(c) == name]
        if not cur:
            return None
    return cur[0]


def _all(e, name):
    return [x for x in e.iter() if _ln(x) == name]


def _text(e, *path) -> str | None:
    x = _find(e, *path) if path else e
    if x is None:
        return None
    t = (x.text or "").strip()
    if not t:
        inner = _find(x, "Text")
        t = (inner.text or "").strip() if inner is not None else ""
    return t or None


def _pos(e) -> tuple[float, float] | None:
    if e is None:
        return None
    lon, lat = _find(e, "Longitude"), _find(e, "Latitude")
    try:
        return float(lon.text), float(lat.text)
    except (AttributeError, TypeError, ValueError):
        return None


def check_errors(root) -> None:
    errs = [x for x in _all(root, "ErrorCondition")]
    if errs:
        desc = " ".join(filter(None, (_text(x, "Description") or (" ".join(t.strip() for t in x.itertext() if t.strip())) for x in errs)))[:300]
        raise OjpError(f"OJP answered an error: {desc}")


def parse(kind: str, raw: bytes):
    """The answer as a GeoDataFrame (points for stops, lines for trip legs)."""
    import geopandas as gpd
    import ogc_http as oh
    from shapely.geometry import LineString, Point
    root = oh.safe_xml(raw)
    check_errors(root)
    rows, geoms = [], []
    if kind == "stops":
        for pr in _all(root, "PlaceResult"):
            place = _find(pr, "Place")
            if place is None:
                continue
            sp = _find(place, "StopPlace")
            ref = _text(sp, "StopPlaceRef") if sp is not None else None
            name = (_text(sp, "StopPlaceName") if sp is not None else None) or _text(place, "Name")
            modes = sorted({(m.text or "").strip() for m in _all(place, "PtMode") if (m.text or "").strip()})
            p = _pos(_find(place, "GeoPosition"))
            rows.append({"stop_ref": ref, "name": name, "modes": ",".join(modes) or None, "probability": _text(pr, "Probability")})
            geoms.append(Point(p) if p else None)
    else:
        for ti, tr in enumerate(_all(root, "TripResult")):
            trip = _find(tr, "Trip")
            if trip is None:
                continue
            for li, leg in enumerate([x for x in trip if _ln(x) == "Leg"]):
                inner = next((x for x in leg if _ln(x) in ("TimedLeg", "TransferLeg", "ContinuousLeg")), None)
                if inner is None:
                    continue
                kind_ = _ln(inner)[:-3].lower()
                if kind_ == "timed":
                    board, alight, svc = _find(inner, "LegBoard"), _find(inner, "LegAlight"), _find(inner, "Service")
                    row = {"from": _text(board, "StopPointName"), "to": _text(alight, "StopPointName"),
                           "depart": _text(board, "ServiceDeparture", "TimetabledTime"), "arrive": _text(alight, "ServiceArrival", "TimetabledTime"),
                           "mode": _text(svc, "Mode", "PtMode") if svc is not None else None,
                           "line": (_text(svc, "PublishedServiceName") or _text(svc, "PublishedLineName")) if svc is not None else None,
                           "toward": _text(svc, "DestinationText") if svc is not None else None}
                    ends = [_pos(_find(board, "GeoPosition")) if board is not None else None, _pos(_find(alight, "GeoPosition")) if alight is not None else None]
                else:
                    st, en = _find(inner, "LegStart"), _find(inner, "LegEnd")
                    row = {"from": _text(st, "Name") if st is not None else None, "to": _text(en, "Name") if en is not None else None,
                           "depart": None, "arrive": None, "mode": _text(inner, "TransferType") or _text(inner, "Service", "PersonalMode") or "walk",
                           "line": None, "toward": None}
                    ends = [_pos(_find(st, "GeoPosition")) if st is not None else None, _pos(_find(en, "GeoPosition")) if en is not None else None]
                track = [p for p in (_pos(x) for x in _all(inner, "Position")) if p]
                geom = LineString(track) if len(track) >= 2 else (LineString(ends) if all(ends) else None)
                rows.append({"trip": ti + 1, "leg": li + 1, "kind": kind_, **row, "trip_duration": _text(trip, "Duration"),
                             "transfers": _text(trip, "Transfers")})
                geoms.append(geom)
    g = gpd.GeoDataFrame(rows, geometry=gpd.GeoSeries(geoms, crs="EPSG:4326"), crs="EPSG:4326")
    return g
