#!/usr/bin/env python3
"""isochrone: travel-time areas (isochrones) around sites, stations or venues: how far you get in 5, 10 or 15 minutes on foot, by bike, by car or by
transit (0.11.0).

    isochrone.py --in stations.gpkg --id stop_name --mode walk --minutes 5,10,15 --provider valhalla --out walk.gpkg
    isochrone.py --point=-122.3367,47.6116 --name Westlake --mode drive --minutes 10,20,30 --provider ors --credential cred:ors --out drive.gpkg
    isochrone.py --point=-122.3367,47.6116 --name Westlake --mode transit --gtfs st_rail_gtfs.zip --date 2026-11-10 --depart 08:00 \
                 --window 60 --minutes 15,30,45 --out transit.gpkg
    isochrone.py providers                                    what each provider does, its limits and terms
    (a western longitude starts with a minus sign, so write --point=LON,LAT with the equals sign)

Providers:
  valhalla  the FOSSGIS public Valhalla server on OpenStreetMap data (valhalla1.openstreetmap.de): walk, bike, drive; no key; a shared community
            server, so one request a second and at most 25 origins a run, kept by the plugin (MAP-ISO-001, 002); a default, so nothing to acknowledge. `--valhalla-url` points at the
            person's own public Valhalla instead (same API, no origin cap). Sent as GET (`/isochrone?json=...`) through SafeHTTP.
  ors       openrouteservice.org with the person's own key (`cred:ors`, registered with map-credentials: host api.openrouteservice.org, header
            Authorization): walk, bike, drive, up to 60 minutes; the free plan allows 20 requests a minute, so one request every 3.1 seconds. The
            isochrone endpoint takes POST only, so it is the second fixed address this plugin POSTs to (POST_ALLOWED); the body is the query.
  gtfs      transit, computed here from a GTFS feed (no service): walk from the origin to the stops it can reach, ride the timetable (a connection
            scan over the trips in service on the date), walk on from every stop reached in the time left. Walking is straight-line distance times a
            detour factor (1.3) at 4.5 km/h. One departure, or the median over a window of departures (`--window 60 --step 10`), since a single
            departure time depends on luck with the timetable (MAP-ISO-006 says which). The walking parts are an approximation; the routing services
            draw walk, bike and drive areas on the real street network.

Output: polygons in EPSG:4326, one per origin and band (or one per band with --dissolve: the area within N minutes of any origin), as rings that do
not overlap (0-5, 5-10, 10-15 min; --nested keeps each band whole), with origin, mode, minutes, band (a label such as "5-10 min"), band_index
(0 = nearest), provider and, for transit, date and departure. Beside it, `<out>.isochrone.json`: provider, endpoint, parameters, origins, the audit of
every request (URLs redacted, SHA-256 of each body), the attribution the map must carry and the method. A capture: a map build reads the file and never
calls a provider.

Nothing here fetches at map-build time; the person's key is read only for the request and never written (MAP-CRED-006).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import socket
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "mcp"))
import gmap_common as gc  # noqa: E402

WGS84 = "EPSG:4326"
MODES = ("walk", "bike", "drive", "transit")
PROVIDERS = ("valhalla", "ors", "gtfs")
VALHALLA_URL = "https://valhalla1.openstreetmap.de/isochrone"
VALHALLA_COSTING = {"walk": "pedestrian", "bike": "bicycle", "drive": "auto"}
VALHALLA_MAX_CONTOURS = 4
VALHALLA_MAX_MINUTES = 120
PUBLIC_VALHALLA_MAX_ORIGINS = 25
PUBLIC_VALHALLA_INTERVAL_S = 1.0
ORS_URL = "https://api.openrouteservice.org/v2/isochrones/{profile}"
ORS_HOST = "api.openrouteservice.org"
ORS_PROFILE = {"walk": "foot-walking", "bike": "cycling-regular", "drive": "driving-car"}
ORS_MAX_MINUTES = 60
ORS_MAX_RANGES = 10
ORS_INTERVAL_S = 3.1
ORS_MAX_ORIGINS = 200
POST_ALLOWED = tuple(ORS_URL.format(profile=p) for p in ORS_PROFILE.values())
LIMITS = {"timeout_s": 90, "max_bytes": 20_000_000}
OSM_ATTRIBUTION = "Routing data © OpenStreetMap contributors, ODbL"
ATTRIBUTION = {"valhalla": OSM_ATTRIBUTION + "; travel times by Valhalla (FOSSGIS public server)",
               "valhalla-own": OSM_ATTRIBUTION + "; travel times by Valhalla",
               "ors": OSM_ATTRIBUTION + "; travel times © openrouteservice by HeiGIT"}   # the wording the HeiGIT terms ask for (1.1.2)
VALHALLA_TERMS = ("FOSSGIS runs valhalla1.openstreetmap.de as a free community server for light use: identify the application, one request a second, "
                  "no bulk or production load, credit OpenStreetMap. For more origins use your own openrouteservice key or your own Valhalla server.")
WALK_MPS = 1.25          # 4.5 km/h
DETOUR = 1.3             # street distance / straight-line distance, a common urban average
TRANSFER_M = 250         # stops this close are a walking transfer (plus every pair transfers.txt names)
MAX_TRANSIT_MINUTES = 120


class IsoError(ValueError):
    """A usage error, safe to show."""


class IsoRefused(IsoError):
    def __init__(self, findings: list[dict]):
        super().__init__(findings[0]["message"] if findings else "refused")
        self.findings = findings


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def parse_minutes(text) -> list[int]:
    vals = sorted({int(round(float(x))) for x in (text.split(",") if isinstance(text, str) else text) if str(x).strip()})
    if not vals or vals[0] <= 0:
        raise IsoError("give the band limits in whole minutes, e.g. 5,10,15")
    return vals


def band_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi} min"


# ---------------------------------------------------------------------------------------------------------------- origins
def read_origins(path: str | Path | None = None, *, layer: str | None = None, id_field: str | None = None, points: list | None = None,
                 names: list | None = None) -> list[dict]:
    """[{id, name, lon, lat}] from a point file (geo_io; polygons and lines use a point on the surface) or from --point lon,lat values."""
    out = []
    if points:
        for i, p in enumerate(points):
            try:
                lon, lat = (float(x) for x in str(p).split(","))
            except ValueError:
                raise IsoError(f"--point takes lon,lat (decimal degrees), not '{p}'")
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise IsoError(f"--point {p}: longitude first, then latitude, in degrees")
            nm = names[i] if names and i < len(names) else f"origin {i + 1}"
            out.append({"id": nm, "name": nm, "lon": lon, "lat": lat})
    if path:
        import geo_io
        r = geo_io.read(path, layer=layer)
        g = r.gdf
        if g.crs is None:
            raise IsoError("the origin file has no coordinate system; say which (crs) before asking for travel times")
        g = g[~g.geometry.is_empty & g.geometry.notna()].to_crs(WGS84)
        if id_field and id_field not in g.columns:
            raise IsoError(f"'{id_field}' is not a column; columns: {', '.join(c for c in g.columns if c != g.geometry.name)}")
        for i, (idx, row) in enumerate(g.iterrows()):
            geom = row.geometry if row.geometry.geom_type == "Point" else row.geometry.representative_point()
            nm = str(row[id_field]) if id_field else str(idx)
            out.append({"id": nm, "name": nm, "lon": round(geom.x, 7), "lat": round(geom.y, 7)})
    if not out:
        raise IsoError("no origins: give --in FILE (points) or --point lon,lat")
    return out


# ---------------------------------------------------------------------------------------------------------------- the gate
def gate(provider: str, mode: str, minutes: list[int], origins: list[dict], *, terms_acknowledged: bool = False, own_server: bool = False,
         gtfs_path=None, credential: str | None = None) -> list[dict]:
    fs = []
    if provider not in PROVIDERS:
        fs.append(finding("MAP-ISO-004", "BLOCK", f"provider is one of {', '.join(PROVIDERS)}, not '{provider}'"))
        return fs
    if mode not in MODES:
        fs.append(finding("MAP-ISO-004", "BLOCK", f"mode is one of {', '.join(MODES)}, not '{mode}'"))
        return fs
    if (mode == "transit") != (provider == "gtfs"):
        fs.append(finding("MAP-ISO-004", "BLOCK", "transit travel times are computed from a GTFS feed (provider gtfs, --gtfs FEED); the routing services do walk, bike and drive"
                          if mode == "transit" else "provider gtfs computes transit only; use valhalla or ors for walk, bike and drive"))
    if provider == "valhalla":
        if minutes[-1] > VALHALLA_MAX_MINUTES:
            fs.append(finding("MAP-ISO-004", "BLOCK", f"Valhalla draws bands up to {VALHALLA_MAX_MINUTES} minutes; the largest asked is {minutes[-1]}"))
        if not own_server:
            # 0.23.0: the default router; the plugin keeps to its terms by itself (one request a second, the origin cap, the credit), so there
            # is nothing to acknowledge. terms_acknowledged is kept for older callers.
            fs.append(finding("MAP-ISO-001", "INFO", "the FOSSGIS Valhalla server is a free community server, and this run keeps to its terms: " + VALHALLA_TERMS))
            if len(origins) > PUBLIC_VALHALLA_MAX_ORIGINS:
                fs.append(finding("MAP-ISO-002", "BLOCK", f"{len(origins)} origins is more than the {PUBLIC_VALHALLA_MAX_ORIGINS} a run sends to the shared FOSSGIS server; "
                                  "use your own openrouteservice key (provider ors) or your own Valhalla server (--valhalla-url), or split the list", len(origins)))
    if provider == "ors":
        if minutes[-1] > ORS_MAX_MINUTES:
            fs.append(finding("MAP-ISO-004", "BLOCK", f"openrouteservice draws bands up to {ORS_MAX_MINUTES} minutes; the largest asked is {minutes[-1]}"))
        if len(minutes) > ORS_MAX_RANGES:
            fs.append(finding("MAP-ISO-004", "BLOCK", f"openrouteservice takes at most {ORS_MAX_RANGES} bands a request"))
        if len(origins) > ORS_MAX_ORIGINS:
            fs.append(finding("MAP-ISO-002", "BLOCK", f"{len(origins)} origins is more than {ORS_MAX_ORIGINS} a run (the free plan allows 500 requests a day)", len(origins)))
        if not credential:
            fs.append(finding("MAP-ISO-008", "BLOCK", "openrouteservice needs your own key: register it with map-credentials (add ors --host api.openrouteservice.org "
                              "--inject header:Authorization) and pass credential cred:ors. Never paste the key into the chat"))
    if provider == "gtfs":
        if not gtfs_path:
            fs.append(finding("MAP-ISO-004", "BLOCK", "transit travel times need the agency's GTFS feed (--gtfs FEED.zip)"))
        if minutes[-1] > MAX_TRANSIT_MINUTES:
            fs.append(finding("MAP-ISO-004", "BLOCK", f"transit bands go up to {MAX_TRANSIT_MINUTES} minutes; the largest asked is {minutes[-1]}"))
    return fs


# ---------------------------------------------------------------------------------------------------------------- the services
def user_agent() -> str:
    import ogc_http as oh
    return oh.default_user_agent().replace("read-only map data client", "read-only map data client; isochrones")


def make_http(**kw):
    import ogc_http as oh
    kw.setdefault("user_agent", user_agent())
    return oh.SafeHTTP(limits=LIMITS, **kw)


class Pacer:
    def __init__(self, interval: float, sleep=time.sleep, clock=time.monotonic):
        self.interval, self.sleep, self.clock, self.last = interval, sleep, clock, None

    def wait(self):
        if self.last is not None:
            gap = self.interval - (self.clock() - self.last)
            if gap > 0:
                self.sleep(gap)
        self.last = self.clock()


def _polygonal(geojson_geom):
    from shapely.geometry import shape
    g = shape(geojson_geom)
    if g.geom_type in ("LineString", "MultiLineString"):            # Valhalla returns lines when polygons=false; close them
        from shapely.geometry import MultiPolygon, Polygon
        rings = [g] if g.geom_type == "LineString" else list(g.geoms)
        polys = [Polygon(r.coords) for r in rings if len(r.coords) >= 4]
        g = MultiPolygon(polys) if len(polys) > 1 else (polys[0] if polys else None)
    if g is not None and not g.is_valid:
        g = g.buffer(0)
    return g


def valhalla_one(http, origin: dict, mode: str, minutes: list[int], url: str = VALHALLA_URL) -> dict:
    """{minutes: geometry} for one origin; contours are asked four at a time (the server's limit)."""
    import ogc_http as oh
    out = {}
    for i in range(0, len(minutes), VALHALLA_MAX_CONTOURS):
        chunk = minutes[i:i + VALHALLA_MAX_CONTOURS]
        q = {"locations": [{"lat": origin["lat"], "lon": origin["lon"]}], "costing": VALHALLA_COSTING[mode],
             "contours": [{"time": m} for m in chunk], "polygons": True, "denoise": 0.5, "generalize": 25}
        try:
            r = http.get(url, params={"json": json.dumps(q, separators=(",", ":"))}, expect="json")
        except oh.ServiceError as ex:
            msg = str(ex)
            if getattr(ex, "status", None) == 400:
                raise IsoError(f"Valhalla could not route from {origin['name']} ({origin['lon']}, {origin['lat']}): no road or path near it")
            raise IsoError(f"Valhalla answered with an error for {origin['name']}: {msg[:200]}")
        doc = json.loads(r.content)
        for f in doc.get("features", []):
            m = int(round(float((f.get("properties") or {}).get("contour", 0))))
            g = _polygonal(f.get("geometry")) if f.get("geometry") else None
            if g is not None and not g.is_empty:
                out[m] = g
    return out


def post_json(url: str, body: dict, cred, *, transport=None, resolver=socket.getaddrinfo, timeout_s: float = 90.0, max_bytes: int = 20_000_000,
              pin: bool = True, audit: list | None = None) -> bytes:
    """A JSON POST to openrouteservice's isochrone endpoints, the credential added at send time. 1.3.0 (R1-REQ): sent through SafeHTTP.request as a
    declared request of the built-in system `openrouteservice` (scripts/declared.py): public addresses only, the checked address connected to,
    no redirect followed, bytes and time capped, the key masked in anything the service echoes, an audit entry with the body's size and hash and
    a redacted copy of the JSON. Never retried, as before (the run paces the origins itself)."""
    import declared as dcl
    import ogc_http as oh
    if url not in POST_ALLOWED:
        raise oh.Refusal("isochrones POST to openrouteservice's isochrone endpoints only; every other request is a GET", "MAP-SVC-002")
    if cred is not None and cred.host != ORS_HOST:
        raise oh.Refusal(f"the credential is registered for {cred.host}, not {ORS_HOST}", "MAP-CRED-013")
    http = oh.SafeHTTP(transport=transport, resolver=resolver, limits={"timeout_s": timeout_s, "max_bytes": max_bytes, "retries": 0}, user_agent=user_agent(),
                       pin=pin, auth=cred)
    base = dcl.BUILTIN["openrouteservice"]
    system = dcl.System(base.name, base.host, [dcl.Declared(d.method, d.path, body=d.body, idempotent=False, changes=False, note=d.note) for d in base.requests],
                        kind=base.kind)
    sent = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    try:
        r = http.request("POST", url, system=system, json=body, headers={"Accept": "application/geo+json, application/json"})
    except oh.Refusal:
        raise
    except oh.ServiceError as ex:
        if audit is not None:
            audit += [dict(c, sent_bytes=sent) for c in http.audit.calls]
        st = ex.status
        if st in (401, 403) or ex.rule == "MAP-CRED-008":
            raise oh.ServiceError(f"openrouteservice refused the key ({st}); check it with map-credentials check ors", "MAP-SVC-018", auth=True, status=st) from None
        if st == 429:
            raise oh.ServiceError("openrouteservice says the plan's request limit is reached (429); wait, or ask for fewer origins", "MAP-SVC-013", status=st) from None
        if st in (301, 302, 303, 307, 308) or ex.rule == "MAP-CRED-005":
            raise oh.ServiceError("openrouteservice redirected the request, which is not followed", "MAP-SVC-020", status=st) from None
        if st is not None and st >= 400:
            msg = str(ex).split(": ", 1)[-1] if ": " in str(ex) else ""
            raise oh.ServiceError(f"openrouteservice answered {st}: {oh.untrusted(msg, 160)}", "MAP-SVC-013", status=st) from None
        if "did not answer" in str(ex):
            raise oh.ServiceError(f"openrouteservice did not answer within {timeout_s:.0f} seconds", "MAP-SVC-010") from None
        raise
    if audit is not None:
        audit += [dict(c, sent_bytes=sent) for c in http.audit.calls]
    return r.content


def ors_one(origin: dict, mode: str, minutes: list[int], cred, *, transport=None, resolver=socket.getaddrinfo, pin=True, audit=None) -> dict:
    import ogc_http as oh
    url = ORS_URL.format(profile=ORS_PROFILE[mode])
    body = {"locations": [[origin["lon"], origin["lat"]]], "range": [m * 60 for m in minutes], "range_type": "time", "smoothing": 25}
    try:
        raw = post_json(url, body, cred, transport=transport, resolver=resolver, pin=pin, audit=audit)
    except oh.ServiceError as ex:
        if getattr(ex, "status", None) == 404 or "2010" in str(ex) or "routable point" in str(ex).lower():
            raise IsoError(f"openrouteservice could not route from {origin['name']}: no road or path near it")
        raise
    doc = json.loads(raw)
    out = {}
    for f in doc.get("features", []):
        m = int(round(float((f.get("properties") or {}).get("value", 0)) / 60))
        g = _polygonal(f.get("geometry")) if f.get("geometry") else None
        if g is not None and not g.is_empty:
            out[m] = g
    return out


# ---------------------------------------------------------------------------------------------------------------- transit (GTFS)
def _secs(t: str) -> int | None:
    try:
        h, m, s = (int(x) for x in t.strip().split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def active_services(feed, date: dt.date) -> set:
    day = date.strftime("%Y%m%d")
    wd = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[date.weekday()]
    on = {r["service_id"] for r in feed.table("calendar.txt") if r.get("start_date", "") <= day <= r.get("end_date", "") and r.get(wd) == "1"}
    for r in feed.table("calendar_dates.txt"):
        if r.get("date") == day:
            (on.add if r.get("exception_type") == "1" else on.discard)(r.get("service_id"))
    return on


def _metric_points(lonlats: dict):
    """{id: (lon, lat)} -> ({id: (x, y)} in a local azimuthal equidistant projection, the transformer)."""
    from pyproj import Transformer
    lons = [p[0] for p in lonlats.values()]
    lats = [p[1] for p in lonlats.values()]
    lon0, lat0 = statistics.median(lons), statistics.median(lats)
    tf = Transformer.from_crs(WGS84, f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m", always_xy=True)
    xs, ys = tf.transform(lons, lats)
    return {k: (x, y) for k, x, y in zip(lonlats, xs, ys)}, tf


class Timetable:
    """The connections of one service day within a time window: (dep, arr, from_stop, to_stop, trip), sorted by departure."""

    def __init__(self, feed, date: dt.date, t_from: int, t_to: int):
        self.feed = feed
        services = active_services(feed, date)
        trips = {r["trip_id"]: r.get("route_id", "") for r in feed.rows("trips.txt", ("trip_id", "service_id", "route_id")) if r.get("service_id") in services}
        self.trips_in_service = len(trips)
        freq = defaultdict(list)
        for r in feed.rows("frequencies.txt", ("trip_id", "start_time", "end_time", "headway_secs")):
            a, b = _secs(r.get("start_time", "")), _secs(r.get("end_time", ""))
            if r.get("trip_id") in trips and a is not None and b is not None and str(r.get("headway_secs", "")).isdigit():
                freq[r["trip_id"]].append((a, b, int(r["headway_secs"])))
        seq = defaultdict(list)
        for r in feed.rows("stop_times.txt", ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence")):
            tid = r.get("trip_id")
            if tid not in trips:
                continue
            a, d = _secs(r.get("arrival_time", "")), _secs(r.get("departure_time", ""))
            if a is None and d is None:
                continue
            try:
                n = int(float(r.get("stop_sequence", "0")))
            except ValueError:
                continue
            seq[tid].append((n, a if a is not None else d, d if d is not None else a, r.get("stop_id", "")))
        conns = []
        for tid, s in seq.items():
            s.sort()
            runs = [(0, tid)]
            if tid in freq:                                    # frequency-based trip: one run per headway, the stop times as offsets
                base = s[0][2]
                runs = [(start - base + k * h, f"{tid}@{start + k * h}") for (start, end, h) in freq[tid] for k in range(max(0, (end - start) // h) + 1)]
            for off, rid in runs:
                for (_, _, dep, frm), (_, arr, _, to) in zip(s, s[1:]):
                    d0, a0 = dep + off, arr + off
                    if t_from <= d0 and a0 <= t_to and a0 >= d0:
                        conns.append((d0, a0, frm, to, rid))
        conns.sort()
        self.connections = conns
        self.stops_used = {c[2] for c in conns} | {c[3] for c in conns}


def footpaths(feed, stop_xy: dict) -> dict:
    """stop_id -> [(stop_id, seconds)]: stops within TRANSFER_M of each other, and every pair transfers.txt names (its min_transfer_time if given)."""
    from shapely import STRtree
    from shapely.geometry import Point
    ids = list(stop_xy)
    pts = [Point(*stop_xy[i]) for i in ids]
    tree = STRtree(pts)
    out = defaultdict(list)
    for i, p in enumerate(pts):
        for j in tree.query(p.buffer(TRANSFER_M)):
            if j != i:
                d = p.distance(pts[j])
                if d <= TRANSFER_M:
                    out[ids[i]].append((ids[int(j)], int(d * DETOUR / WALK_MPS)))
    for r in feed.table("transfers.txt"):
        a, b = r.get("from_stop_id"), r.get("to_stop_id")
        if a in stop_xy and b in stop_xy and a != b and r.get("transfer_type") != "3":
            t = int(r["min_transfer_time"]) if str(r.get("min_transfer_time", "")).isdigit() else int(math.dist(stop_xy[a], stop_xy[b]) * DETOUR / WALK_MPS)
            out[a].append((b, t))
    return out


def scan(tt: Timetable, access: dict, foot: dict, t0: int, budget: int) -> dict:
    """Connection scan: earliest arrival (seconds) at each stop, from {stop: arrival} reached on foot, within t0 + budget."""
    INF = float("inf")
    arr = defaultdict(lambda: INF)
    for s, a in access.items():
        arr[s] = min(arr[s], a)
        for f, w in foot.get(s, ()):
            arr[f] = min(arr[f], a + w)
    on_trip = set()
    limit = t0 + budget
    for dep, a, frm, to, trip in tt.connections:
        if dep < t0:
            continue
        if dep > limit:
            break
        if trip in on_trip or arr[frm] <= dep:
            on_trip.add(trip)
            if a < arr[to]:
                arr[to] = a
                for f, w in foot.get(to, ()):
                    if a + w < arr[f]:
                        arr[f] = a + w
    return {s: v for s, v in arr.items() if v <= limit}


def transit_one(feed, tt: Timetable, stop_xy: dict, foot: dict, tf, origin: dict, minutes: list[int], departures: list[int]) -> tuple[dict, dict]:
    """{minutes: geometry (WGS84)} and a summary, for one origin. With several departures, a stop's travel time is the median over them."""
    import numpy as np
    import shapely
    from shapely.geometry import Point
    from shapely.ops import unary_union
    budget = minutes[-1] * 60
    ox, oy = tf.transform(origin["lon"], origin["lat"])
    reach_m = budget * WALK_MPS / DETOUR
    access_d = {s: math.dist((ox, oy), xy) for s, xy in stop_xy.items()}
    access_d = {s: d for s, d in access_d.items() if d <= reach_m}
    per_stop = defaultdict(list)
    for t0 in departures:
        access = {s: t0 + int(d * DETOUR / WALK_MPS) for s, d in access_d.items()}
        for s, a in scan(tt, access, foot, t0, budget).items():
            per_stop[s].append(a - t0)
    n = len(departures)
    travel = {}
    for s, v in per_stop.items():                               # a stop reached on only some departures counts as unreached on the others
        v = sorted(v) + [float("inf")] * (n - len(v))
        med = statistics.median(v)
        if med != float("inf"):
            travel[s] = med
    inv = tf.transform
    out = {}
    for m in minutes:
        T = m * 60
        circles = [Point(ox, oy).buffer(T * WALK_MPS / DETOUR, 24)]
        for s, t in travel.items():
            left = T - t
            if left > 0:
                circles.append(Point(*stop_xy[s]).buffer(left * WALK_MPS / DETOUR, 16))
        out[m] = shapely.transform(unary_union(circles), lambda xy: np.column_stack(inv(xy[:, 0], xy[:, 1], direction="INVERSE")))
    rode = sum(1 for s, t in travel.items() if access_d.get(s, float("inf")) * DETOUR / WALK_MPS > t + 1)
    return out, {"stops_reached": len(travel), "stops_by_vehicle": rode}


def transit_setup(gtfs_path, date: dt.date | None, depart: str, window: int, step: int, minutes: list[int]):
    import gtfs
    feed = gtfs.open_feed(gtfs_path)
    fs = []
    if date is None:
        date = dt.date.today()
    start, end, _ = feed.validity()
    day = date.strftime("%Y%m%d")
    if (start and day < start) or (end and day > end):
        raise IsoRefused([finding("MAP-ISO-005", "BLOCK", f"{date.isoformat()} is outside the feed's service dates ({start} to {end}); pick a date inside them")])
    t0 = _secs(depart + (":00" if depart.count(":") == 1 else ""))
    if t0 is None:
        raise IsoError("--depart takes HH:MM (24-hour), e.g. 08:00")
    departures = list(range(t0, t0 + window * 60 + 1, max(step, 1) * 60)) if window > 0 else [t0]
    tt = Timetable(feed, date, t0, departures[-1] + minutes[-1] * 60)
    if not tt.connections:
        raise IsoRefused([finding("MAP-ISO-005", "BLOCK", f"no trips run on {date.isoformat()} ({date.strftime('%A')}) between {depart} and "
                                  f"{minutes[-1]} minutes later in this feed; pick a service day and time")])
    stops = feed.stops()
    lonlat = {s: (float(stops[s]["stop_lon"]), float(stops[s]["stop_lat"])) for s in tt.stops_used if s in stops and gtfs._coord_ok(stops[s])}
    stop_xy, tf = _metric_points(lonlat)
    foot = footpaths(feed, stop_xy)
    how = (f"median of {len(departures)} departures every {step} min from {depart} to {_hhmm(departures[-1])}" if len(departures) > 1 else f"one departure at {depart}")
    fs.append(finding("MAP-ISO-006", "INFO", f"transit travel times from the timetable on {date.isoformat()}, {how}; walking at {WALK_MPS * 3.6:.1f} km/h over straight-line "
                      f"distance x {DETOUR} (not the street network); transfers within {TRANSFER_M} m", {"date": date.isoformat(), "departures": [_hhmm(d) for d in departures]}))
    return feed, tt, stop_xy, foot, tf, departures, date, fs


def _hhmm(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


# ---------------------------------------------------------------------------------------------------------------- bands
def to_frame(results: list[tuple[dict, dict]], minutes: list[int], mode: str, provider: str, *, dissolve: bool = False, nested: bool = False, extra: dict | None = None):
    """[(origin, {minutes: geometry})] -> GeoDataFrame of bands (rings unless nested), one per origin and band, or per band when dissolved."""
    import geopandas as gpd
    from shapely.ops import unary_union
    groups = [({"id": "all origins", "name": f"{len(results)} origins"}, {m: unary_union([r[m] for _, r in results if m in r]) for m in minutes})] if dissolve else results
    rows, geoms = [], []
    for origin, bands in groups:
        prev = None
        lo = 0
        for i, m in enumerate(minutes):
            g = bands.get(m)
            if g is None or g.is_empty:
                lo = m
                continue
            whole = g if prev is None else unary_union([g, prev])          # a band always holds the smaller ones (providers can disagree at edges)
            shape = whole if (nested or prev is None) else whole.difference(prev)
            if not shape.is_empty:
                rows.append({"origin": origin["id"], "mode": mode, "minutes": m, "band": band_label(0 if nested else lo, m), "band_index": i, "provider": provider, **(extra or {})})
                geoms.append(shape.buffer(0) if not shape.is_valid else shape)
            prev, lo = whole, m
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84)


# ---------------------------------------------------------------------------------------------------------------- the run
def run(origins: list[dict], mode: str, minutes, provider: str = "valhalla", *, terms_acknowledged: bool = False, valhalla_url: str | None = None,
        credential: str | None = None, gtfs_path=None, date: dt.date | None = None, depart: str = "08:00", window: int = 0, step: int = 10,
        dissolve: bool = False, nested: bool = False, http=None, transport=None, resolver=socket.getaddrinfo, sleep=time.sleep, clock=time.monotonic,
        pin: bool = True, store=None):
    """-> (GeoDataFrame of bands, report dict). Raises IsoRefused on a BLOCK."""
    minutes = parse_minutes(minutes)
    own = bool(valhalla_url)
    fs = gate(provider, mode, minutes, origins, terms_acknowledged=terms_acknowledged, own_server=own, gtfs_path=gtfs_path, credential=credential)
    if any(f["severity"] == "BLOCK" for f in fs):
        raise IsoRefused([f for f in fs if f["severity"] == "BLOCK"])
    results, skipped, audit = [], [], []
    endpoint, attribution, extra, summary = None, None, {}, {}
    if provider == "valhalla":
        url = valhalla_url or VALHALLA_URL
        if own and not url.rstrip("/").endswith("/isochrone"):
            url = url.rstrip("/") + "/isochrone"
        h = http or make_http(transport=transport, resolver=resolver, sleep=sleep, clock=clock, pin=pin)
        pace = Pacer(0 if own else PUBLIC_VALHALLA_INTERVAL_S, sleep, clock)
        for o in origins:
            pace.wait()
            try:
                results.append((o, valhalla_one(h, o, mode, minutes, url)))
            except IsoError as ex:
                skipped.append({"origin": o["id"], "why": str(ex)})
        audit = list(h.audit.calls)
        endpoint, attribution = url, ATTRIBUTION["valhalla-own" if own else "valhalla"]
    elif provider == "ors":
        import credentials as cr
        try:
            cred = cr.resolve(credential, store=store, host=ORS_HOST)
        except cr.CredentialError as ex:
            raise IsoRefused(ex.findings if hasattr(ex, "findings") else [finding("MAP-ISO-008", "BLOCK", str(ex))])
        pace = Pacer(ORS_INTERVAL_S, sleep, clock)
        for o in origins:
            pace.wait()
            try:
                results.append((o, ors_one(o, mode, minutes, cred, transport=transport, resolver=resolver, pin=pin, audit=audit)))
            except IsoError as ex:
                skipped.append({"origin": o["id"], "why": str(ex)})
        endpoint, attribution = ORS_URL.format(profile=ORS_PROFILE[mode]), ATTRIBUTION["ors"]
        del cred
    else:
        feed, tt, stop_xy, foot, tf, departures, date, tfs = transit_setup(gtfs_path, date, depart, window, step, minutes)
        fs += tfs
        fi = (feed.table("feed_info.txt") or [{}])[0]
        for o in origins:
            bands, s = transit_one(feed, tt, stop_xy, foot, tf, o, minutes, departures)
            results.append((o, bands))
            summary[o["id"]] = s
            if s["stops_by_vehicle"] == 0:
                fs.append(finding("MAP-ISO-003", "WARN", f"from {o['name']} no stop is reached by vehicle within {minutes[-1]} min on {date.isoformat()} at {depart}; "
                                  "the area is walking only (no service nearby, or none at that time)", o["id"]))
        endpoint = str(Path(gtfs_path).name)
        attribution = f"Transit times from {fi.get('feed_publisher_name') or 'the agency'} GTFS" + (f" ({fi.get('feed_version')})" if fi.get("feed_version") else "")
        extra = {"date": date.isoformat(), "depart": depart}
    for s in skipped:
        fs.append(finding("MAP-ISO-007", "WARN", s["why"], s["origin"]))
    if not results:
        raise IsoRefused([finding("MAP-ISO-003", "BLOCK", "no origin gave a travel-time area: " + "; ".join(s["why"] for s in skipped)[:400])])
    gdf = to_frame(results, minutes, mode, provider if provider != "valhalla" or not own else "valhalla (own server)", dissolve=dissolve, nested=nested, extra=extra)
    missing = [(o["id"], m) for o, r in results for m in minutes if m not in r or r[m].is_empty]
    if missing:
        fs.append(finding("MAP-ISO-003", "WARN", f"{len(missing)} band(s) came back empty: " + ", ".join(f"{a} {m} min" for a, m in missing[:8]), missing))
    report = {"isochrone_version": 1, "provider": provider, "endpoint": endpoint, "mode": mode, "minutes": minutes, "dissolve": dissolve, "nested": nested,
              "origins": [{"id": o["id"], "lon": o["lon"], "lat": o["lat"]} for o in origins], "skipped": skipped, "features": len(gdf),
              "attribution": attribution, "terms": VALHALLA_TERMS if provider == "valhalla" and not own else None, "terms_acknowledged": bool(terms_acknowledged),
              "method": {"walk_kmh": WALK_MPS * 3.6, "detour": DETOUR, "transfer_m": TRANSFER_M} if provider == "gtfs" else {"network": "OpenStreetMap streets"},
              "transit": summary or None, **({"date": extra.get("date"), "depart": depart, "window_min": window, "step_min": step} if provider == "gtfs" else {}),
              "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "audit": audit, "findings": fs}
    report["provenance"] = layer_provenance(provider, own, mode, report["retrieved_at"][:10], extra)
    return gdf, report


MODE_WORDS = {"walk": "walk times", "pedestrian": "walk times", "bike": "bike times", "bicycle": "bike times", "drive": "drive times", "auto": "drive times",
              "transit": "transit times"}


def layer_provenance(provider: str, own: bool, mode: str, day: str, extra: dict | None = None) -> dict:
    """The `provenance:` block to copy into the map layer (0.19.0): the consolidated source line files the bands under the organization whose data
    they were made from (OpenStreetMap for Valhalla and openrouteservice), with the service in the parenthetical: "© OpenStreetMap contributors
    (walk times by Valhalla, FOSSGIS)". A GTFS isochrone is filed under the agency named in the feed."""
    what = MODE_WORDS.get(str(mode).lower(), "travel times")
    if provider == "gtfs":
        agency = (extra or {}).get("feed_publisher_name") or (extra or {}).get("agency_name") or "the agency"
        return {"source": f"Transit times from the {agency} GTFS feed", "publisher": {"name": agency, "product": "GTFS"}, "license": "see the feed",
                "retrieved_at": day}
    operator = {"valhalla": "Valhalla" if own else "Valhalla, FOSSGIS", "ors": "openrouteservice, HeiGIT"}.get(provider, provider)
    return {"source": f"{what} by {operator} on OpenStreetMap", "publisher": {"name": operator, "product": what}, "derived_from": "osm",
            "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors", "retrieved_at": day}


def write(gdf, report: dict, out: Path) -> tuple[Path, Path]:
    import geo_io
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    geo_io.write(gdf, out, layer="isochrones" if out.suffix.lower() == ".gpkg" else None)
    rec = out.with_name(out.stem + ".isochrone.json")
    rec.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out, rec


def providers_info() -> list[dict]:
    return [{"provider": "valhalla", "modes": ["walk", "bike", "drive"], "key": False, "max_minutes": VALHALLA_MAX_MINUTES,
             "limits": f"public FOSSGIS server: 1 request/s, {PUBLIC_VALHALLA_MAX_ORIGINS} origins a run, kept by the plugin; --valhalla-url for your own server",
             "terms": VALHALLA_TERMS, "attribution": ATTRIBUTION["valhalla"]},
            {"provider": "ors", "modes": ["walk", "bike", "drive"], "key": "cred:ors (header Authorization, host api.openrouteservice.org)",
             "max_minutes": ORS_MAX_MINUTES, "limits": "free plan: 20 requests/min, 500/day; up to 10 bands a request", "attribution": ATTRIBUTION["ors"]},
            {"provider": "gtfs", "modes": ["transit"], "key": False, "max_minutes": MAX_TRANSIT_MINUTES,
             "limits": "computed locally from the GTFS feed; walking is straight-line x 1.3 at 4.5 km/h", "attribution": "the agency's GTFS feed"}]


def main(argv=None, http=None, transport=None, resolver=socket.getaddrinfo, sleep=time.sleep) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "providers":
        print(json.dumps(providers_info(), indent=2))
        return gc.EXIT_OK
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--layer")
    ap.add_argument("--id", dest="id_field")
    ap.add_argument("--point", action="append")
    ap.add_argument("--name", action="append")
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--minutes", required=True)
    ap.add_argument("--provider", choices=PROVIDERS)
    ap.add_argument("--terms-acknowledged", action="store_true", help=argparse.SUPPRESS)    # older callers; nothing to acknowledge for the defaults
    ap.add_argument("--valhalla-url")
    ap.add_argument("--credential")
    ap.add_argument("--gtfs")
    ap.add_argument("--date")
    ap.add_argument("--depart", default="08:00")
    ap.add_argument("--window", type=int, default=0)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--dissolve", action="store_true")
    ap.add_argument("--nested", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    provider = a.provider or ("gtfs" if a.mode == "transit" else "valhalla")
    try:
        origins = read_origins(a.inp, layer=a.layer, id_field=a.id_field, points=a.point, names=a.name)
        date = dt.date.fromisoformat(a.date) if a.date else None
        gdf, rep = run(origins, a.mode, a.minutes, provider, terms_acknowledged=a.terms_acknowledged, valhalla_url=a.valhalla_url, credential=a.credential,
                       gtfs_path=a.gtfs, date=date, depart=a.depart, window=a.window, step=a.step, dissolve=a.dissolve, nested=a.nested, http=http,
                       transport=transport, resolver=resolver, sleep=sleep)
        out, rec = write(gdf, rep, Path(a.out))
        print(json.dumps({"out": str(out), "record": str(rec), "features": len(gdf), "attribution": rep["attribution"], "findings": rep["findings"]}, indent=2))
        return gc.EXIT_OK
    except IsoRefused as ex:
        print(json.dumps({"refused": ex.findings}, indent=2))
        return 4
    except (IsoError, ValueError) as ex:
        print(f"isochrone: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
