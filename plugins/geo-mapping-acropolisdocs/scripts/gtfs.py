#!/usr/bin/env python3
"""gtfs: read a GTFS transit feed (and a GTFS-Realtime vehicle-positions file) as map layers.

Library use:   import gtfs; gdf, notes = gtfs.read_layer("feed.zip", "routes:light_rail")
               gdf, notes = gtfs.read_vehicle_positions("vehicles.pb", feed="feed.zip")
Command line:  gtfs.py info FEED                         what the feed holds: agencies, routes by mode, stops, validity
               gtfs.py layers FEED                       the layers it can be read as
               gtfs.py export FEED OUT [--layers routes,stations] [--mode light_rail]   write layers to a GeoPackage (or one to GeoJSON)
               gtfs.py vehicles PB OUT [--feed FEED] [--mode light_rail]              GTFS-Realtime positions as points

A GTFS feed is a zip (or a folder) of CSV tables: agency, routes, trips, stops, stop_times, shapes, feed_info. It is read with the
standard library (csv and zipfile, streamed, never extracted), so it works wherever geo_io does. The layers:

  routes     one line feature per route: the route's shapes (shapes.txt) merged into one (Multi)LineString; a route whose trips have no
             shape is drawn through the stops of its longest trip in each direction (geometry_source says which). Carries the route's names,
             route_type and mode, agency, the feed's route_color and route_text_color as #RRGGBB, and how many trips and shapes it has.
  shapes     one line per shape_id with the route it serves (the raw shapes, before merging).
  stations   what a rider sees as a station: every location_type 1 station, plus every stop with no parent station. Each carries the routes
             that serve it (rolled up from its platforms, through stop_times) as `routes` ("1 Line; 2 Line"), `route_ids`, `modes` and
             `n_routes`.
  stops      every boarding point (location_type 0 or empty), with its parent station and the routes that serve it.
  entrances  location_type 2 entrances and exits.

A layer name may carry a mode after a colon: `routes:light_rail`, `stations:rail`, `stops:bus`. Modes follow route_type: light_rail (0, tram,
streetcar or light rail), subway (1), rail (2), bus (3), ferry (4), cable_tram (5), aerial_lift (6), funicular (7), trolleybus (11), monorail
(12); the extended types (100-1700) map onto these. A station is kept for a mode when a route of that mode stops there.

Everything read returns notes; feed validity (feed_info or calendar dates) is checked against today (MAP-GTFS-001). Nothing is fetched.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import struct
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

WGS84 = "EPSG:4326"
REQUIRED = ("stops.txt", "routes.txt", "trips.txt")
KINDS = ("routes", "shapes", "stations", "stops", "entrances")
DEFAULT_LAYER = "routes"

MODES = {0: "light_rail", 1: "subway", 2: "rail", 3: "bus", 4: "ferry", 5: "cable_tram", 6: "aerial_lift", 7: "funicular",
         11: "trolleybus", 12: "monorail"}
MODE_LABEL = {"light_rail": "Tram, streetcar or light rail", "subway": "Subway or metro", "rail": "Rail", "bus": "Bus", "ferry": "Ferry",
              "cable_tram": "Cable tram", "aerial_lift": "Aerial lift", "funicular": "Funicular", "trolleybus": "Trolleybus",
              "monorail": "Monorail", "other": "Other"}
# extended route types (the Google "Hierarchical Vehicle Type" list): hundreds band -> base mode
EXTENDED = {1: "rail", 2: "bus", 4: "subway", 7: "bus", 8: "trolleybus", 9: "light_rail", 10: "ferry", 11: "ferry", 12: "ferry",
            13: "aerial_lift", 14: "funicular", 15: "bus", 17: "other"}


class GTFSError(Exception):
    """A feed that cannot be read; carries a MAP-GTFS id."""

    def __init__(self, msg: str, fid: str = "MAP-GTFS-004"):
        super().__init__(f"{fid}: {msg}")
        self.fid = fid


def mode_of(route_type) -> str:
    try:
        t = int(str(route_type).strip())
    except ValueError:
        return "other"
    if t in MODES:
        return MODES[t]
    if t == 405:
        return "monorail"
    return EXTENDED.get(t // 100, "other") if t >= 100 else "other"


def _note(code, sev, msg, data=None):
    import geo_io
    return geo_io.Note(code, sev, msg, data)


# --------------------------------------------------------------------------------------------------------------- the feed
class Feed:
    """A GTFS feed in a zip or a folder. Tables are read on demand, streamed, and kept for the life of the object."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip = None
        self._prefix = ""
        self._tables: dict = {}
        if self.path.is_dir():
            names = {p.name for p in self.path.iterdir() if p.is_file()}
        elif self.path.is_file() and zipfile.is_zipfile(self.path):
            import geo_io
            self._zip = zipfile.ZipFile(self.path)
            infos = geo_io._safe_members(self._zip)              # the zip-slip, entry-count and size guards
            stops = sorted((i.filename for i in infos if Path(i.filename).name == "stops.txt"), key=lambda n: n.count("/"))
            self._prefix = stops[0][: -len("stops.txt")] if stops else ""
            names = {i.filename[len(self._prefix):] for i in infos if i.filename.startswith(self._prefix) and "/" not in i.filename[len(self._prefix):]}
        else:
            raise GTFSError(f"{self.path.name} is not a GTFS zip or folder")
        self.names = names
        missing = [n for n in REQUIRED if n not in names]
        if missing:
            raise GTFSError(f"{self.path.name} is not a complete GTFS feed: no {', '.join(missing)}")

    def has(self, name: str) -> bool:
        return name in self.names

    def _open(self, name: str):
        if self._zip is not None:
            return io.TextIOWrapper(self._zip.open(self._prefix + name), encoding="utf-8-sig", newline="")
        return open(self.path / name, encoding="utf-8-sig", newline="")

    def rows(self, name: str, columns: tuple | None = None):
        """Stream a table as dicts (only `columns`, stripped). An absent optional table yields nothing."""
        if name not in self.names:
            return
        with self._open(name) as fh:
            rd = csv.reader(fh)
            try:
                head = [h.strip() for h in next(rd)]
            except StopIteration:
                return
            idx = [(c, head.index(c)) for c in (columns or head) if c in head]
            for r in rd:
                if not r:
                    continue
                yield {c: (r[i].strip() if i < len(r) else "") for c, i in idx}

    def table(self, name: str) -> list[dict]:
        if name not in self._tables:
            self._tables[name] = list(self.rows(name))
        return self._tables[name]

    # ---- derived
    def agencies(self) -> dict:
        out = {r.get("agency_id", ""): r.get("agency_name", "") for r in self.table("agency.txt")}
        return out

    def routes(self) -> dict:
        ag = self.agencies()
        only = next(iter(ag.values())) if len(ag) == 1 else ""
        out = {}
        for r in self.table("routes.txt"):
            rid = r.get("route_id", "")
            r = dict(r)
            r["mode"] = mode_of(r.get("route_type", ""))
            r["agency_name"] = ag.get(r.get("agency_id", ""), only)
            out[rid] = r
        return out

    def trips(self) -> dict:
        """trip_id -> (route_id, shape_id, direction_id)."""
        if "_trips" not in self._tables:
            self._tables["_trips"] = {r["trip_id"]: (r.get("route_id", ""), r.get("shape_id", ""), r.get("direction_id", ""))
                                      for r in self.rows("trips.txt", ("trip_id", "route_id", "shape_id", "direction_id"))}
        return self._tables["_trips"]

    def stops(self) -> dict:
        if "_stops" not in self._tables:
            self._tables["_stops"] = {r["stop_id"]: r for r in self.table("stops.txt")}
        return self._tables["_stops"]

    def shapes(self) -> dict:
        """shape_id -> [(lon, lat), ...] in sequence order."""
        if "_shapes" not in self._tables:
            pts = defaultdict(list)
            for r in self.rows("shapes.txt", ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence")):
                try:
                    pts[r["shape_id"]].append((int(float(r["shape_pt_sequence"])), float(r["shape_pt_lon"]), float(r["shape_pt_lat"])))
                except (KeyError, ValueError):
                    continue
            self._tables["_shapes"] = {k: [(x, y) for _, x, y in sorted(v)] for k, v in pts.items()}
        return self._tables["_shapes"]

    def stop_routes(self) -> dict:
        """stop_id -> set(route_id) for every stop a trip calls at (stop_times streamed once, two columns)."""
        if "_stop_routes" not in self._tables:
            trips = self.trips()
            seen = defaultdict(set)
            for r in self.rows("stop_times.txt", ("trip_id", "stop_id")):
                t = trips.get(r.get("trip_id"))
                if t:
                    seen[r.get("stop_id", "")].add(t[0])
            self._tables["_stop_routes"] = seen
        return self._tables["_stop_routes"]

    def longest_trip_stops(self, route_ids: set) -> dict:
        """(route_id, direction_id) -> [stop_id, ...] of the trip with the most stops, for routes drawn without shapes."""
        trips = self.trips()
        want = {tid: t for tid, t in trips.items() if t[0] in route_ids}
        seqs = defaultdict(list)
        for r in self.rows("stop_times.txt", ("trip_id", "stop_id", "stop_sequence")):
            if r.get("trip_id") in want:
                try:
                    seqs[r["trip_id"]].append((int(float(r["stop_sequence"])), r["stop_id"]))
                except (KeyError, ValueError):
                    continue
        best: dict = {}
        for tid, s in seqs.items():
            key = (want[tid][0], want[tid][2])
            if key not in best or len(s) > len(best[key]):
                best[key] = s
        return {k: [sid for _, sid in sorted(v)] for k, v in best.items()}

    def validity(self) -> tuple[str | None, str | None, str]:
        """(start YYYYMMDD, end YYYYMMDD, where from): feed_info first, else the span of calendar and calendar_dates."""
        fi = self.table("feed_info.txt")
        if fi and (fi[0].get("feed_start_date") or fi[0].get("feed_end_date")):
            return fi[0].get("feed_start_date") or None, fi[0].get("feed_end_date") or None, "feed_info.txt"
        dates = [r.get("start_date") for r in self.table("calendar.txt")] + [r.get("end_date") for r in self.table("calendar.txt")]
        dates += [r.get("date") for r in self.table("calendar_dates.txt")]
        dates = sorted(d for d in dates if d and len(d) == 8 and d.isdigit())
        return (dates[0], dates[-1], "calendar") if dates else (None, None, "")

    def info(self) -> dict:
        fi = (self.table("feed_info.txt") or [{}])[0]
        start, end, src = self.validity()
        routes = self.routes()
        by_mode = defaultdict(list)
        for r in routes.values():
            by_mode[r["mode"]].append(r.get("route_short_name") or r.get("route_long_name") or r.get("route_id"))
        lt = defaultdict(int)
        for s in self.stops().values():
            lt[s.get("location_type") or "0"] += 1
        return {"path": self.path.name, "publisher": fi.get("feed_publisher_name") or ", ".join(sorted(set(self.agencies().values()))),
                "version": fi.get("feed_version") or None, "valid_from": start, "valid_to": end, "validity_source": src,
                "agencies": sorted(set(self.agencies().values())), "routes_by_mode": {k: sorted(v) for k, v in sorted(by_mode.items())},
                "stops": {"stops": lt["0"], "stations": lt["1"], "entrances": lt["2"], "other": sum(v for k, v in lt.items() if k not in "012")},
                "has_shapes": self.has("shapes.txt"), "tables": sorted(self.names)}

    def close(self):
        if self._zip is not None:
            self._zip.close()


def is_gtfs(path: str | Path) -> bool:
    """A zip or a folder holding stops.txt, routes.txt and trips.txt (at its top or one folder down)."""
    p = Path(path)
    try:
        if p.is_dir():
            return all((p / n).is_file() for n in REQUIRED)
        if p.is_file() and zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.count("/") <= 1]
            base = {Path(n).name for n in names}
            return all(n in base for n in REQUIRED)
    except (OSError, zipfile.BadZipFile):
        return False
    return False


_FEEDS: dict = {}


def open_feed(path: str | Path) -> Feed:
    """One Feed per file and modification time, so a map reading routes and stations from one zip parses it once."""
    p = Path(path).resolve()
    key = (str(p), p.stat().st_mtime_ns if p.exists() else 0)
    f = _FEEDS.get(key)
    if f is None:
        if len(_FEEDS) > 4:
            for old in list(_FEEDS.values()):
                old.close()
            _FEEDS.clear()
        f = _FEEDS[key] = Feed(p)
    return f


def parse_layer(name: str | None) -> tuple[str, str | None]:
    if not name:
        return DEFAULT_LAYER, None
    kind, _, mode = str(name).partition(":")
    kind = kind.strip().lower()
    mode = mode.strip().lower() or None
    if kind not in KINDS:
        raise GTFSError(f"'{name}' is not a GTFS layer. Layers: {', '.join(KINDS)} (add :mode, e.g. routes:light_rail)", "MAP-GTFS-006")
    if mode and mode not in MODE_LABEL:
        raise GTFSError(f"'{mode}' is not a GTFS mode. Modes: {', '.join(m for m in MODE_LABEL)}", "MAP-GTFS-006")
    return kind, mode


def list_layers(path: str | Path) -> list[dict]:
    f = open_feed(path)
    info = f.info()
    modes = list(info["routes_by_mode"])
    gtype = {"routes": "MultiLineString", "shapes": "LineString", "stations": "Point", "stops": "Point", "entrances": "Point"}
    counts = {"routes": sum(len(v) for v in info["routes_by_mode"].values()), "shapes": len(f.shapes()) if f.has("shapes.txt") else 0,
              "stations": None, "stops": info["stops"]["stops"], "entrances": info["stops"]["entrances"]}
    out = [{"name": k, "geometry_type": gtype[k], "features": counts[k]} for k in KINDS if k != "entrances" or counts["entrances"]]
    for m in modes:
        out.append({"name": f"routes:{m}", "geometry_type": "MultiLineString", "features": len(info["routes_by_mode"][m])})
        out.append({"name": f"stations:{m}", "geometry_type": "Point", "features": None})
    return out


def validity_notes(f: Feed, today: dt.date | None = None) -> list:
    today = today or dt.date.today()
    start, end, src = f.validity()
    out = []

    def d(s):
        try:
            return dt.datetime.strptime(s, "%Y%m%d").date()
        except (TypeError, ValueError):
            return None
    s0, e0 = d(start), d(end)
    fi = (f.table("feed_info.txt") or [{}])[0]
    ver = fi.get("feed_version")
    span = f"{s0.isoformat() if s0 else '?'} to {e0.isoformat() if e0 else '?'}"
    if e0 and e0 < today:
        out.append(_note("MAP-GTFS-001", "WARN", f"the feed's service ended on {e0.isoformat()} ({src}); the map shows a past network. "
                         "Get the current feed from the agency", {"valid_from": start, "valid_to": end}))
    elif s0 and s0 > today:
        out.append(_note("MAP-GTFS-001", "WARN", f"the feed's service starts on {s0.isoformat()} ({src}); the map shows a future network",
                         {"valid_from": start, "valid_to": end}))
    else:
        out.append(_note("IO", "INFO", f"GTFS feed {fi.get('feed_publisher_name') or f.path.name}" + (f" version {ver}" if ver else "")
                         + f", service {span}", {"valid_from": start, "valid_to": end, "version": ver}))
    return out


# --------------------------------------------------------------------------------------------------------------- the layers
def _hex(c: str) -> str | None:
    c = (c or "").strip().lstrip("#")
    return f"#{c.upper()}" if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c) else None


def _route_name(r: dict) -> str:
    return r.get("route_short_name") or r.get("route_long_name") or r.get("route_id", "")


def _routes_layer(f: Feed, mode: str | None, notes: list):
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge, unary_union
    routes = f.routes()
    sel = {rid: r for rid, r in routes.items() if mode is None or r["mode"] == mode}
    trips = f.trips()
    shape_ids, ntrips = defaultdict(set), defaultdict(int)
    for rid, sid, _ in trips.values():
        if rid in sel:
            ntrips[rid] += 1
            if sid:
                shape_ids[rid].add(sid)
    shapes = f.shapes() if f.has("shapes.txt") else {}
    no_shape = {rid for rid in sel if not any(s in shapes for s in shape_ids.get(rid, ()))}
    fallback = f.longest_trip_stops(no_shape) if no_shape else {}
    stops = f.stops()
    rows, geoms, unmapped = [], [], []
    for rid, r in sel.items():
        lines, src = [], "shapes"
        if rid in no_shape:
            src = "stop_sequence"
            for (frid, _), seq in fallback.items():
                if frid != rid:
                    continue
                pts = [(float(stops[s]["stop_lon"]), float(stops[s]["stop_lat"])) for s in seq if s in stops and _coord_ok(stops[s])]
                if len(pts) >= 2:
                    lines.append(LineString(pts))
        else:
            lines = [LineString(shapes[s]) for s in sorted(shape_ids[rid]) if s in shapes and len(shapes[s]) >= 2]
        if not lines:
            unmapped.append(_route_name(r))
            continue
        u = unary_union(lines)
        merged = linemerge(u) if u.geom_type == "MultiLineString" else u
        if merged.geom_type == "LineString":
            merged = MultiLineString([merged])
        rows.append({"route_id": rid, "route_name": _route_name(r), "route_short_name": r.get("route_short_name", ""),
                     "route_long_name": r.get("route_long_name", ""), "route_type": int(r["route_type"]) if r.get("route_type", "").isdigit() else None,
                     "mode": r["mode"], "agency_id": r.get("agency_id", ""), "agency_name": r.get("agency_name", ""),
                     "route_color": _hex(r.get("route_color")), "route_text_color": _hex(r.get("route_text_color")),
                     "route_url": r.get("route_url", ""), "route_sort_order": r.get("route_sort_order", ""),
                     "trips": ntrips.get(rid, 0), "shapes": len(shape_ids.get(rid, ())), "geometry_source": src})
        geoms.append(merged)
    _terminal_pairs(f, rows, notes)
    if no_shape - {r["route_id"] for r in rows if r["geometry_source"] == "shapes"}:
        drawn = [routes[x] for x in no_shape if x in sel and _route_name(routes[x]) not in unmapped]
        if drawn:
            notes.append(_note("MAP-GTFS-002", "WARN", f"{len(drawn)} route(s) have no shapes and are drawn stop to stop (straight lines between stops): "
                               + ", ".join(sorted(_route_name(r) for r in drawn)[:12]), [r["route_id"] for r in drawn]))
    if unmapped:
        notes.append(_note("MAP-GTFS-002", "WARN", f"{len(unmapped)} route(s) have no shape and no trips with located stops; left off: "
                           + ", ".join(sorted(unmapped)[:12]), unmapped))
    return rows, geoms


TERMINAL_PAIR_MAX_ROUTES = 150      # terminal pairs are worked out for a routes layer of up to this many routes (stop_times is read once)


def _place(stops: dict, sid: str) -> str:
    """A stop's name for a terminal: its station's (parent_station) name when it has one, so the bays of one terminal are one place."""
    st = stops.get(sid) or {}
    par = stops.get(st.get("parent_station") or "")
    return str((par or st).get("stop_name") or sid).strip()


def _terminal_pairs(f: "Feed", rows: list, notes: list) -> None:
    """1.14.0 (Day 11 issue 16): each route's terminals (`terminal_a`, `terminal_b`: the first and last stop of its longest trip, by station)
    and `route_pair`, the two in name order ("Bainbridge Island – Seattle"), which is the same for the two directions of a directional
    terminal-pair route. Dissolve by route_pair (ops: [{op: dissolve, by: route_pair}]) to draw each pair once; join a two-column table of
    route_pair and the agency's own route name, and dissolve by that, for an agency's operating routes (Washington State Ferries: 38 GTFS
    routes, 8 operating routes). Worked out for up to TERMINAL_PAIR_MAX_ROUTES routes."""
    if not rows:
        return
    if len(rows) > TERMINAL_PAIR_MAX_ROUTES:
        notes.append(_note("MAP-GTFS-010", "INFO", f"terminal pairs are not worked out for {len(rows)} routes (at most {TERMINAL_PAIR_MAX_ROUTES}); "
                           "choose a mode (routes:ferry) to have them"))
        return
    stops = f.stops()
    longest = f.longest_trip_stops({r["route_id"] for r in rows})
    by_route: dict = {}
    for (rid, _dir), seq in longest.items():
        if seq and (rid not in by_route or len(seq) > len(by_route[rid])):
            by_route[rid] = seq
    pairs = set()
    for r in rows:
        seq = by_route.get(r["route_id"])
        if not seq:
            r["terminal_a"] = r["terminal_b"] = r["route_pair"] = None
            continue
        a, b = _place(stops, seq[0]), _place(stops, seq[-1])
        r["terminal_a"], r["terminal_b"] = a, b
        r["route_pair"] = " – ".join(sorted({a, b}, key=str.casefold)) if a != b else a
        pairs.add(r["route_pair"])
    if pairs and len(pairs) < len(rows):
        notes.append(_note("MAP-GTFS-010", "INFO", f"{len(rows)} routes run between {len(pairs)} terminal pairs (route_pair); dissolve by route_pair "
                           "to draw each pair once", sorted(pairs)[:20]))


def _coord_ok(s: dict) -> bool:
    try:
        lat, lon = float(s.get("stop_lat", "")), float(s.get("stop_lon", ""))
    except ValueError:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _shapes_layer(f: Feed, mode: str | None, notes: list):
    from shapely.geometry import LineString
    routes = f.routes()
    by_shape = defaultdict(set)
    for rid, sid, _ in f.trips().values():
        if sid and rid in routes and (mode is None or routes[rid]["mode"] == mode):
            by_shape[sid].add(rid)
    rows, geoms = [], []
    for sid, pts in sorted(f.shapes().items()):
        if sid not in by_shape or len(pts) < 2:
            continue
        rids = sorted(by_shape[sid])
        r = routes[rids[0]]
        rows.append({"shape_id": sid, "route_id": rids[0], "route_name": _route_name(r), "mode": r["mode"],
                     "route_color": _hex(r.get("route_color")), "routes": "; ".join(_route_name(routes[x]) for x in rids)})
        geoms.append(LineString(pts))
    if not f.has("shapes.txt"):
        notes.append(_note("MAP-GTFS-002", "WARN", "the feed has no shapes.txt; the shapes layer is empty (the routes layer draws stop to stop)"))
    return rows, geoms


def _stop_layer(f: Feed, kind: str, mode: str | None, notes: list):
    from shapely.geometry import Point
    routes = f.routes()
    stops = f.stops()
    served = f.stop_routes() if kind in ("stations", "stops") else {}
    # roll platforms up to their station
    station_routes = defaultdict(set)
    for sid, rids in served.items():
        s = stops.get(sid)
        if s is None:
            continue
        station_routes[sid] |= rids
        if s.get("parent_station"):
            station_routes[s["parent_station"]] |= rids
    rows, geoms, bad = [], [], 0
    for sid, s in stops.items():
        lt = s.get("location_type") or "0"
        if kind == "stations" and not (lt == "1" or (lt == "0" and not s.get("parent_station"))):
            continue
        if kind == "stops" and lt != "0":
            continue
        if kind == "entrances" and lt != "2":
            continue
        rids = sorted(station_routes.get(sid, set()) if kind == "stations" else served.get(sid, set()), key=lambda x: _sort_key(routes.get(x, {})))
        rmodes = sorted({routes[x]["mode"] for x in rids if x in routes})
        if mode and kind != "entrances" and mode not in rmodes:
            continue
        if mode and kind == "entrances":
            par = s.get("parent_station")
            pm = {routes[x]["mode"] for x in station_routes.get(par, set()) if x in routes} if par else set()
            if not served:
                served = f.stop_routes()
                for xs, xr in served.items():
                    p2 = stops.get(xs, {}).get("parent_station")
                    if p2:
                        station_routes[p2] |= xr
                pm = {routes[x]["mode"] for x in station_routes.get(par, set()) if x in routes} if par else set()
            if mode not in pm:
                continue
        if not _coord_ok(s):
            bad += 1
            continue
        names = [_route_name(routes[x]) for x in rids if x in routes]
        if mode:                                       # list only the routes of the mode asked for (the bus bridge is not a Link line)
            names = [_route_name(routes[x]) for x in rids if x in routes and routes[x]["mode"] == mode]
            rids = [x for x in rids if x in routes and routes[x]["mode"] == mode]
        rows.append({"stop_id": sid, "stop_name": s.get("stop_name", ""), "stop_code": s.get("stop_code", ""),
                     "location_type": int(lt) if lt.isdigit() else 0, "parent_station": s.get("parent_station", ""),
                     "routes": "; ".join(dict.fromkeys(names)), "route_ids": "; ".join(rids), "modes": "; ".join(rmodes),
                     "n_routes": len(set(names)), "wheelchair_boarding": s.get("wheelchair_boarding", ""), "zone_id": s.get("zone_id", ""),
                     "platform_code": s.get("platform_code", ""), "stop_url": s.get("stop_url", "")})
        geoms.append(Point(float(s["stop_lon"]), float(s["stop_lat"])))
    if bad:
        notes.append(_note("MAP-GTFS-003", "WARN", f"{bad} {kind} without usable coordinates (missing, out of range or 0,0) were left off"))
    return rows, geoms


def _sort_key(r: dict):
    o = r.get("route_sort_order", "")
    return (int(o) if str(o).isdigit() else 10 ** 9, _route_name(r)) if r else (10 ** 9, "")


def read_layer(path: str | Path, layer: str | None = None, *, max_features: int | None = None, today: dt.date | None = None):
    """One GTFS layer as a GeoDataFrame in WGS84, with notes. `layer` is routes, shapes, stations, stops or entrances, optionally ':mode'."""
    import geopandas as gpd
    kind, mode = parse_layer(layer)
    f = open_feed(path)
    notes = validity_notes(f, today)
    if layer is None:
        notes.append(_note("IO", "INFO", f"a GTFS feed is read as layers ({', '.join(KINDS)}); read '{DEFAULT_LAYER}'. Pass layer=, e.g. "
                           "stations or routes:light_rail", list(KINDS)))
    fn = {"routes": _routes_layer, "shapes": _shapes_layer}.get(kind)
    rows, geoms = fn(f, mode, notes) if fn else _stop_layer(f, kind, mode, notes)
    if mode and not rows:
        have = sorted(f.info()["routes_by_mode"])
        notes.append(_note("MAP-GTFS-005", "WARN", f"the feed has no {kind} for mode {mode}; its modes are: {', '.join(have) or 'none'}", have))
    if max_features:
        rows, geoms = rows[:max_features], geoms[:max_features]
    key = {"routes": "route_id", "shapes": "shape_id"}.get(kind, "stop_id")
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84) if rows else gpd.GeoDataFrame({key: []}, geometry=[], crs=WGS84)
    return _tidy(gdf, key), notes


def _tidy(gdf, key: str):
    """Empty GTFS fields become nulls (an empty stop_code is no code, not a code shared by fifty stops), and the key column is named for the
    data lint's duplicate check (gdf.attrs['id_field'])."""
    import pandas as pd
    for c in gdf.columns:
        if c != gdf.geometry.name and (pd.api.types.is_object_dtype(gdf[c]) or pd.api.types.is_string_dtype(gdf[c])):
            gdf[c] = gdf[c].astype(object).where(gdf[c].astype(object) != "", None)
    gdf.attrs["id_field"] = key
    return gdf


# --------------------------------------------------------------------------------------------------------------- GTFS-Realtime
def _varint(b: bytes, i: int):
    shift = out = 0
    while True:
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if not c & 0x80:
            return out, i
        shift += 7
        if shift > 63:
            raise GTFSError("bad varint in the GTFS-Realtime message", "MAP-GTFS-007")


def _fields(b: bytes):
    """Yield (field number, wire type, value) of one protobuf message; nested messages come back as bytes."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise GTFSError(f"unsupported protobuf wire type {wt} in the GTFS-Realtime message", "MAP-GTFS-007")
        if i > n:
            raise GTFSError("the GTFS-Realtime message is truncated", "MAP-GTFS-007")
        yield fno, wt, v


def _f32(v: bytes) -> float:
    return struct.unpack("<f", v)[0]


STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def decode_vehicle_positions(data: bytes) -> tuple[dict, list[dict]]:
    """A GTFS-Realtime FeedMessage (protobuf) -> (header, [vehicle dict]). Pure Python: only the VehiclePosition fields a map uses."""
    header, out = {}, []
    for fno, wt, v in _fields(data):
        if fno == 1 and wt == 2:
            for hf, hw, hv in _fields(v):
                if hf == 1 and hw == 2:
                    header["gtfs_realtime_version"] = hv.decode("utf-8", "replace")
                elif hf == 3 and hw == 0:
                    header["timestamp"] = hv
        elif fno == 2 and wt == 2:
            ent = {"entity_id": ""}
            vp = None
            for ef, ew, ev in _fields(v):
                if ef == 1 and ew == 2:
                    ent["entity_id"] = ev.decode("utf-8", "replace")
                elif ef == 4 and ew == 2:
                    vp = ev
            if vp is None:
                continue
            rec = {**ent, "trip_id": "", "route_id": "", "direction_id": None, "vehicle_id": "", "vehicle_label": "", "lat": None, "lon": None,
                   "bearing": None, "speed_mps": None, "current_status": "", "stop_id": "", "timestamp": None}
            for pf, pw, pv in _fields(vp):
                if pf == 1 and pw == 2:                       # TripDescriptor
                    for tf, tw, tv in _fields(pv):
                        if tf == 1 and tw == 2:
                            rec["trip_id"] = tv.decode("utf-8", "replace")
                        elif tf == 5 and tw == 2:
                            rec["route_id"] = tv.decode("utf-8", "replace")
                        elif tf == 6 and tw == 0:
                            rec["direction_id"] = tv
                elif pf == 2 and pw == 2:                     # Position
                    for qf, qw, qv in _fields(pv):
                        if qw == 5 and qf in (1, 2, 3, 5):
                            rec[{1: "lat", 2: "lon", 3: "bearing", 5: "speed_mps"}[qf]] = round(_f32(qv), 6)
                elif pf == 8 and pw == 2:                     # VehicleDescriptor
                    for vf, vw, vv in _fields(pv):
                        if vf == 1 and vw == 2:
                            rec["vehicle_id"] = vv.decode("utf-8", "replace")
                        elif vf == 2 and vw == 2:
                            rec["vehicle_label"] = vv.decode("utf-8", "replace")
                elif pf == 4 and pw == 0:
                    rec["current_status"] = STATUS.get(pv, str(pv))
                elif pf == 5 and pw == 0:
                    rec["timestamp"] = pv
                elif pf == 7 and pw == 2:
                    rec["stop_id"] = pv.decode("utf-8", "replace")
            out.append(rec)
    return header, out


SHAPE_HEADING_MAX_M = 300          # a vehicle further than this from its trip's shape gets no inferred heading


def shape_heading(pts: list, lon: float, lat: float) -> float | None:
    """The compass bearing (degrees clockwise from north) of a shape (its points in travel order) at the point nearest lon, lat, or None when the
    point is more than SHAPE_HEADING_MAX_M from it. Local flat metres around the point: exact enough for a direction over a few hundred metres."""
    import numpy as np
    if len(pts) < 2:
        return None
    a = np.asarray(pts, dtype="float64")
    kx = 111_320.0 * math.cos(math.radians(lat))
    x, y = (a[:, 0] - lon) * kx, (a[:, 1] - lat) * 110_540.0
    x0, y0, x1, y1 = x[:-1], y[:-1], x[1:], y[1:]
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    t = np.clip(np.where(L2 > 0, -(x0 * dx + y0 * dy) / np.where(L2 > 0, L2, 1), 0), 0, 1)
    px, py = x0 + t * dx, y0 + t * dy
    d = np.hypot(px, py)
    i = int(np.argmin(np.where(L2 > 0, d, np.inf)))
    if not np.isfinite(d[i]) or d[i] > SHAPE_HEADING_MAX_M:
        return None
    return round((math.degrees(math.atan2(dx[i], dy[i])) + 360.0) % 360.0, 1)


def read_vehicle_positions(path: str | Path, feed: str | Path | None = None, mode: str | None = None, now: dt.datetime | None = None):
    """Vehicle positions as points (WGS84), joined to a static feed's routes when one is given (route name, mode, colour)."""
    import geopandas as gpd
    from shapely.geometry import Point
    data = Path(path).read_bytes()
    if data[:1] in (b"{", b"["):
        raise GTFSError("the file is JSON; GTFS-Realtime is read as protobuf (.pb). Save the feed's binary response", "MAP-GTFS-007")
    header, recs = decode_vehicle_positions(data)
    notes = []
    routes = open_feed(feed).routes() if feed else {}
    trips = open_feed(feed).trips() if feed else {}
    shapes = open_feed(feed).shapes() if feed and open_feed(feed).has("shapes.txt") else {}
    from_shape = 0
    rows, geoms, unlocated, unmatched = [], [], 0, 0
    for r in recs:
        if r["lat"] is None or r["lon"] is None or not (-90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180) or (r["lat"] == 0 and r["lon"] == 0):
            unlocated += 1
            continue
        rid = r["route_id"] or (trips.get(r["trip_id"], ("",))[0] if trips else "")
        rr = routes.get(rid)
        if feed and rr is None:
            unmatched += 1
        m = rr["mode"] if rr else None
        if mode and m != mode:
            continue
        ts = r["timestamp"] or header.get("timestamp")
        heading, hsrc = (r["bearing"] % 360.0, "bearing") if r["bearing"] is not None else (None, None)
        if heading is None and r["trip_id"] in trips and trips[r["trip_id"]][1] in shapes:     # the trip's own shape, in travel order (0.11.0)
            heading = shape_heading(shapes[trips[r["trip_id"]][1]], r["lon"], r["lat"])
            hsrc = "trip shape" if heading is not None else None
            from_shape += heading is not None
        rows.append({**{k: v for k, v in r.items() if k not in ("lat", "lon")}, "route_id": rid, "heading": heading, "heading_source": hsrc,
                     "route_name": _route_name(rr) if rr else rid, "mode": m or "", "route_color": _hex(rr.get("route_color")) if rr else None,
                     "observed_at": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z") if ts else ""})
        geoms.append(Point(r["lon"], r["lat"]))
    hts = header.get("timestamp")
    when = dt.datetime.fromtimestamp(hts, dt.timezone.utc) if hts else None
    notes.append(_note("IO", "INFO", f"GTFS-Realtime vehicle positions: {len(recs)} vehicles" + (f" at {when.isoformat().replace('+00:00', 'Z')}" if when else "")
                       + (f"; {len(rows)} kept for mode {mode}" if mode else ""), {"feed_timestamp": hts, "vehicles": len(recs)}))
    if when:
        age = ((now or dt.datetime.now(dt.timezone.utc)) - when).total_seconds()
        if age > 600:
            notes.append(_note("MAP-GTFS-008", "INFO", f"the positions are a snapshot {age / 60:.0f} minutes old; the map shows where trains were "
                               f"at {when.isoformat().replace('+00:00', 'Z')}, not now"))
    if unlocated:
        notes.append(_note("MAP-GTFS-003", "WARN", f"{unlocated} vehicles had no usable position and were left off"))
    if rows:
        nb = sum(1 for x in rows if x["heading_source"] == "bearing")
        ns = sum(1 for x in rows if x["heading_source"] == "trip shape")
        none = len(rows) - nb - ns
        notes.append(_note("MAP-GTFS-009", "INFO" if none == 0 else "WARN", f"direction of travel: {nb} from the feed's bearing, {ns} from the trip's shape, {none} unknown "
                           "(drawn without a pointer)" + ("; the feed sends no bearing, so headings follow each trip's path" if nb == 0 and ns else ""),
                           {"bearing": nb, "trip_shape": ns, "unknown": none}))
    if unmatched:
        sev = "INFO" if mode else "WARN"
        notes.append(_note("MAP-GTFS-005", sev, f"{unmatched} vehicles name a route or trip the static feed does not have (another of the agency's "
                           "feeds, such as its buses, or an older feed)" + (f"; they were left off, since the {mode} filter needs the route" if mode else "")))
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84) if rows else gpd.GeoDataFrame({"vehicle_id": []}, geometry=[], crs=WGS84)
    return _tidy(gdf, "vehicle_id"), notes


# --------------------------------------------------------------------------------------------------------------- command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("info", help="what the feed holds")
    pi.add_argument("feed")
    pl = sub.add_parser("layers", help="the layers the feed can be read as")
    pl.add_argument("feed")
    pe = sub.add_parser("export", help="write layers to a GeoPackage, or one layer to any geo_io format")
    pe.add_argument("feed")
    pe.add_argument("out")
    pe.add_argument("--layers", default="routes,stations")
    pe.add_argument("--mode")
    pv = sub.add_parser("vehicles", help="GTFS-Realtime vehicle positions as points")
    pv.add_argument("pb")
    pv.add_argument("out")
    pv.add_argument("--feed")
    pv.add_argument("--mode")
    a = ap.parse_args(argv)
    import geo_io
    try:
        if a.cmd == "info":
            print(json.dumps(open_feed(a.feed).info(), indent=2))
        elif a.cmd == "layers":
            print(json.dumps(list_layers(a.feed), indent=2))
        elif a.cmd == "export":
            kinds = [k.strip() for k in a.layers.split(",") if k.strip()]
            out = Path(a.out)
            if len(kinds) > 1 and out.suffix.lower() != ".gpkg":
                print("gtfs: several layers go to a GeoPackage (.gpkg); name one layer for another format", file=sys.stderr)
                return gc.EXIT_USAGE
            report = []
            for k in kinds:
                name = f"{k}:{a.mode}" if a.mode else k
                gdf, notes = read_layer(a.feed, name)
                wr = geo_io.write(gdf, out, layer=k.replace(":", "_") if out.suffix.lower() == ".gpkg" else None)
                report.append({"layer": name, "features": len(gdf), "notes": [n.as_dict() for n in notes + list(wr.notes)]})
            print(json.dumps({"out": str(out), "layers": report}, indent=2))
        elif a.cmd == "vehicles":
            gdf, notes = read_vehicle_positions(a.pb, a.feed, a.mode)
            wr = geo_io.write(gdf, a.out)
            print(json.dumps({"out": a.out, "features": len(gdf), "notes": [n.as_dict() for n in notes + list(wr.notes)]}, indent=2))
        return gc.EXIT_OK
    except GTFSError as e:
        print(f"gtfs: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
