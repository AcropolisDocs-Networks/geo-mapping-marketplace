#!/usr/bin/env python3
"""osm: OpenStreetMap features by tag (Overpass) and OpenStreetMap history (ohsome), as map layers (3.4.0; Day 21 issues 1, 2, 14).

    osm.py plan    --tags "amenity=drinking_water" --bbox=-112.14,33.405,-112.01,33.495
    osm.py fetch   --tags "amenity=drinking_water" --tags "highway=*,foot!=no" --bbox=... --out data/osm/phx.gpkg
    osm.py fetch   --query-file q.overpassql --out data/osm/q.gpkg
    osm.py history counts   --filter "building=* and geometry:polygon" --bbox=... --time 2023-02-01/2023-03-15/P1D --out counts.csv
    osm.py history born     --filter "building=* and geometry:polygon" --bbox=... --time 2023-02-06,2023-03-15 --out born.gpkg
    osm.py history snapshot --filter "highway=*" --bbox=... --time 2023-02-06 --out streets_2023-02-06.gpkg
    osm.py history users    --filter "building=*" --polygon frame.geojson --time 2023-02-06/2023-03-15/PT6H --out users.csv

Features (Overpass). A tag list (`k=v`, `k=*`, `k!=v`, `k~regex`; a comma joins filters that must all hold, each list item is one more
set) or a query of your own; `out geom`, so every way and relation arrives with its geometry. One query at a time, as the public instance
asks. A 429, 502, 503 or 504 ("at capacity", seen on most first tries on Day 21) is tried again after 2, 6 and 18 s, then the next mirror the
library entry declares, and the fetch stops after the declared list. The answer becomes one GeoPackage layer per geometry type (points, lines,
polygons) with the commonest tags as columns and every tag as JSON in `tags`. The OSM base time of the answer goes into the record beside the
file and into the layer's provenance. Mirrors seen on 2026-11-21: overpass-api.de answered from the person's computer; overpass.kumi.systems
and overpass.private.coffee were blocked by the account's network allowlist; maps.mail.ru answered 504; the cloud workspace reached none.

History (ohsome, HeiGIT). `counts` per period (elements/count), `born` (contributions/geometry: each feature's creation and deletion time and
its last shape), `snapshot` (the features as they stood at a time: elements/geometry, or, when ohsome refuses that with 403 as it did on
Day 21 from both machines, elementsFullHistory over a one-hour window, last state per feature, the reason recorded), `users`
(users/count over a polygon, cumulative from the first time). Boxes over 0.1 square degrees are split into tiles and joined. No key.

Both are ODbL: "© OpenStreetMap contributors" once on the map, and a derived database keeps the licence (MAP-LIC-004).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

ENTRY = "ref.public-service.world.osm.overpass"
HISTORY_ENTRY = "ref.public-service.world.osm.ohsome"
CREDIT = "© OpenStreetMap contributors"
LICENSE = "ODbL-1.0"
MIRRORS = ("overpass-api.de", "overpass.kumi.systems", "overpass.private.coffee", "maps.mail.ru")
MIRROR_PATH = {"maps.mail.ru": "/osm/tools/overpass/api/interpreter"}
BACKOFF_S = (2.0, 6.0, 18.0)
RETRY_STATUS = (429, 502, 503, 504)
MAX_BYTES = 512_000_000
OHSOME_HOST = "api.ohsome.org"
OHSOME_BASE = "/v1"
TILE_DEG2 = 0.1
TAG = re.compile(r"^\s*([A-Za-z0-9_:\-.]+)\s*(=|!=|~|!~)\s*(.*?)\s*$")


class OsmError(Exception):
    def __init__(self, message: str, rule: str = "MAP-OSM-001", code: int = gc.EXIT_SERVICE):
        super().__init__(message)
        self.rule, self.code = rule, code


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- queries
def check_bbox(bbox) -> tuple[float, float, float, float]:
    b = [float(v) for v in (bbox.split(",") if isinstance(bbox, str) else bbox)]
    if len(b) != 4 or not (-180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
        raise OsmError("bbox is W,S,E,N in degrees, west < east and south < north", "MAP-OSM-002", gc.EXIT_USAGE)
    return tuple(b)


def _filter(term: str) -> str:
    t = term.strip()
    if not t:
        return ""
    if "=" not in t and "~" not in t:
        return f'["{t}"]'
    m = TAG.match(t)
    if not m:
        raise OsmError(f"'{term}' is not a tag filter (k=v, k=*, k!=v, k~regex)", "MAP-OSM-002", gc.EXIT_USAGE)
    k, op, v = m.groups()
    if op == "=" and v in ("*", ""):
        return f'["{k}"]'
    if op == "!=" and v in ("*", ""):
        return f'[!"{k}"]'
    v = v.replace('"', '\\"')
    return f'["{k}"{op}"{v}"]'


def query(tags: list[str], bbox, *, timeout: int = 120, kinds: str = "nwr") -> str:
    """Overpass QL for every tag set (an item; a comma joins filters that must all hold) in the box, with `out geom`."""
    w, s, e, n = check_bbox(bbox)
    if not tags:
        raise OsmError("give at least one tag filter (amenity=drinking_water) or a query of your own", "MAP-OSM-002", gc.EXIT_USAGE)
    sets = []
    for item in tags:
        fl = "".join(_filter(x) for x in str(item).split(","))
        sets.append(f"  {kinds}{fl}({s:g},{w:g},{n:g},{e:g});")
    return f"[out:json][timeout:{int(timeout)}];\n(\n" + "\n".join(sets) + "\n);\nout geom;"


def _sc(name: str) -> str:
    """A Turbo shortcut as people write it, in double braces (built here so the source carries no template field of its own)."""
    return "{" * 2 + name + "}" * 2


def is_turbo(text: str | None) -> bool:
    """Whether a query is written for Overpass Turbo: its double-brace shortcuts, or a Turbo link."""
    t = str(text or "")
    return "{{" in t or bool(re.match(r"\s*https?://(www\.)?overpass-turbo\.eu/", t))


def from_turbo(text: str, bbox=None, today=None) -> tuple[str, list[str]]:
    """5.4.0 (open data portals P4.6): an Overpass Turbo query, or the query carried by a Turbo share link, as plain Overpass QL the API takes.
    -> (query, what was changed). Turbo's double-brace shortcuts are filled in here: bbox and center from the box given, date:1 year from
    today, name=value constants, geocodeArea:Name as the area of that name; a style block is dropped; the output statements
    are made `out geom`, which is what the layers are built from. A shortcut that needs Turbo's own geocoder or data is refused by name."""
    import datetime as _dt
    import urllib.parse as _up
    t, notes = str(text or "").strip(), []
    m = re.match(r"https?://(www\.)?overpass-turbo\.eu/(\S*)", t)
    if m:
        u = _up.urlsplit(t)
        q = (_up.parse_qs(u.query).get("Q") or _up.parse_qs(u.fragment).get("Q") or [None])[0]
        if not q:
            raise OsmError("this Overpass Turbo link keeps its query on overpass-turbo.eu (a short link): open it there, choose Export, then Query, and "
                           "give the query text", "MAP-OSM-002", gc.EXIT_USAGE)
        t = q.strip()
        notes.append("the query was read from the link's Q parameter")
    n_style = len(re.findall(r"\{\{\s*style\s*:", t))
    t = re.sub(r"\{\{\s*style\s*:.*?\}\}", "", t, flags=re.S)
    if n_style:
        notes.append(f"the {_sc('style')} block is Turbo's own drawing rule and was left out (the map styles the layer)")
    consts = dict(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^{}]*?)\s*\}\}", t))
    t = re.sub(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^{}]*?\s*\}\}\s*", "", t)
    for k, v in consts.items():
        t = re.sub(r"\{\{\s*" + re.escape(k) + r"\s*\}\}", lambda _m, v=v: v, t)
    if consts:
        notes.append(f"{len(consts)} constant(s) filled in ({', '.join(consts)})")
    if re.search(r"\{\{\s*(bbox|center)\s*\}\}", t):
        if bbox is None:
            raise OsmError(f"the query uses {_sc('bbox')} or {_sc('center')}: give the box (bbox W,S,E,N) it stands for", "MAP-OSM-002", gc.EXIT_USAGE)
        w, s_, e, n = check_bbox(bbox)
        t = re.sub(r"\{\{\s*bbox\s*\}\}", f"{s_:g},{w:g},{n:g},{e:g}", t)
        t = re.sub(r"\{\{\s*center\s*\}\}", f"{(s_ + n) / 2:g},{(w + e) / 2:g}", t)
        notes.append(f"{_sc('bbox')} filled in from the box given (south, west, north, east)")

    def _date(mm):
        num, unit = float(mm.group(1) or 0), (mm.group(2) or "day").lower().rstrip("s")
        days = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30.44, "year": 365.25}.get(unit)
        if days is None:
            raise OsmError(f"{_sc('date:' + mm.group(0))} is not a span this reads (days, weeks, months, years)", "MAP-OSM-002", gc.EXIT_USAGE)
        base = today or _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None)
        return (base - _dt.timedelta(days=num * days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.search(r"\{\{\s*date", t):
        t = re.sub(r"\{\{\s*date\s*:?\s*([0-9.]+)?\s*([A-Za-z]+)?\s*\}\}", _date, t)
        notes.append(f"{_sc('date')} filled in from today")
    areas = re.findall(r"\{\{\s*geocodeArea\s*:\s*([^{}]+?)\s*\}\}", t)
    for name in areas:
        first = name.split(",")[0].strip().replace('"', "")
        t = re.sub(r"\{\{\s*geocodeArea\s*:\s*" + re.escape(name) + r"\s*\}\}", f'area["name"="{first}"]', t)
    if areas:
        notes.append(f"{_sc('geocodeArea')} read as the area with that name (Turbo asks its geocoder for one area; a name several places share matches them "
                     f"all: {', '.join(a_.split(',')[0].strip() for a_ in areas)})")
    left = re.findall(r"\{\{\s*([A-Za-z]+)", t)
    if left:
        raise OsmError(f"the query uses Turbo's {_sc(left[0])} shortcut, which needs Turbo's own geocoder or data: write the box or the id into the query "
                       "in its place", "MAP-OSM-002", gc.EXIT_USAGE)
    if re.search(r"\[\s*out\s*:\s*(xml|csv|custom|popup)[^\]]*\]", t):
        t = re.sub(r"\[\s*out\s*:\s*(xml|csv|custom|popup)[^\]]*\]", "[out:json]", t)
        notes.append("the output format was made json")
    elif not re.search(r"\[\s*out\s*:\s*json\s*\]", t):
        t = "[out:json]" + ("" if t.lstrip().startswith("[") else ";\n") + t.lstrip()
    # the usual Turbo ending (out body; >; out skel qt;) sends nodes apart from their ways; the layers are built from `out geom`
    body = re.sub(r"\bout\s+(?:body|meta|tags|ids|skel|center|bb|qt|asc|\d+|\s)*;\s*>\s*;\s*out\s+skel(?:\s+qt)?\s*;", "out geom;", t)
    body = re.sub(r"\bout\s*(?:body|meta|skel|qt|asc|\s)*;", "out geom;", body)
    if body != t:
        notes.append("the output statements were made `out geom` (each feature with its own shape)")
        t = body
    if not re.search(r"\bout\b[^;]*;\s*$", t.strip()):
        t = t.rstrip() + "\nout geom;"
    return t.strip(), notes


def plan(tags: list[str] | None, bbox=None, own_query: str | None = None) -> dict:
    turbo_notes: list[str] = []
    if own_query and is_turbo(own_query):                 # 5.4.0: a query written for Overpass Turbo is read as plain Overpass QL
        own_query, turbo_notes = from_turbo(own_query, bbox)
    q = own_query or query(tags or [], bbox)
    area = None
    if bbox is not None:
        w, s, e, n = check_bbox(bbox)
        area = round((e - w) * (n - s), 4)
    fs = []
    if area and area > 1.0:
        fs.append(finding("MAP-OSM-003", "WARN", f"the box is {area:g} square degrees: the public Overpass instance is for small areas (a city); "
                          "a region is read from the day's Geofabrik extract (extract: auto on the layer or the tool; ref.infrastructure.world.osm.geofabrik)"))
    if turbo_notes:
        fs.append(finding("MAP-OSM-007", "INFO", "an Overpass Turbo query was read as Overpass QL: " + "; ".join(turbo_notes), turbo_notes))
    return {"query": q, "hosts": list(MIRRORS), "bbox_deg2": area, "credit": CREDIT, "license": LICENSE, "findings": fs,
            "note": "one POST at a time; 429/502/503/504 tried again after 2, 6 and 18 s, then the next declared mirror"}


def _system(host: str):
    import declared as dcl
    return dcl.protocol_system("overpass", f"https://{host}{MIRROR_PATH.get(host, '/api/interpreter')}", name=f"{ENTRY}:{host}",
                               limits={"sequential": True})


def _post_overpass(q: str, http, sleep, mirrors=MIRRORS) -> tuple[dict, str, list[dict]]:
    import ogc_http as oh
    tried: list[dict] = []
    for host in mirrors:
        url = f"https://{host}{MIRROR_PATH.get(host, '/api/interpreter')}"
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                r = http.request("POST", url, system=_system(host), data={"data": q}, expect="json", max_bytes=MAX_BYTES)
                return json.loads(r.content), host, tried
            except oh.ServiceError as ex:
                tried.append({"host": host, "status": ex.status, "error": str(ex)[:160]})
                if ex.status in RETRY_STATUS and attempt < len(BACKOFF_S):
                    sleep(BACKOFF_S[attempt])
                    continue
                break                                         # refused, unreachable or out of tries: the next mirror
            except oh.Refusal as ex:
                tried.append({"host": host, "status": None, "error": str(ex)[:160]})
                break
    raise OsmError("no Overpass instance answered: " + "; ".join(f"{t['host']} {t['status'] or ''} {t['error'][:60]}".strip() for t in tried[-6:])
                   + ". The public instance is often at capacity; try again in a few minutes, from the person's computer (the cloud workspace "
                   "reaches no Overpass instance)", "MAP-OSM-001")


# ---------------------------------------------------------------------------------------------------------------- Overpass JSON to layers
def elements_to_layers(doc: dict, top_tags: int = 24) -> dict:
    """Overpass `out geom` elements -> {"points", "lines", "polygons"} GeoDataFrames (EPSG:4326). A closed way is a polygon when it carries an
    area tag (building, landuse, amenity, leisure, natural other than coastline, area=yes) and not area=no; multipolygon relations are
    assembled from their member ways (outer and inner)."""
    import shapely
    from shapely.geometry import LineString, Point, Polygon
    rows = {"points": [], "lines": [], "polygons": []}
    for el in doc.get("elements") or []:
        t = el.get("type")
        tags = el.get("tags") or {}
        base = {"osm_type": t, "osm_id": int(el.get("id") or 0), "tags": tags}
        if t == "node" and "lat" in el:
            rows["points"].append({**base, "geometry": Point(el["lon"], el["lat"])})
        elif el.get("center") and not el.get("geometry") and not el.get("members"):
            # `out center`: a way or relation given as its centre point (a restaurant mapped as a building)
            rows["points"].append({**base, "geometry": Point(el["center"]["lon"], el["center"]["lat"])})
        elif t == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"] if p]
            if len(pts) < 2:
                continue
            closed = len(pts) >= 4 and pts[0] == pts[-1]
            if closed and _is_area(tags):
                rows["polygons"].append({**base, "geometry": shapely.make_valid(Polygon(pts))})
            else:
                rows["lines"].append({**base, "geometry": LineString(pts)})
        elif t == "relation" and el.get("members"):
            outer, inner = [], []
            for m in el["members"]:
                if m.get("type") == "way" and m.get("geometry"):
                    pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
                    if len(pts) >= 2:
                        (inner if m.get("role") == "inner" else outer).append(LineString(pts))
            if tags.get("type") in ("multipolygon", "boundary") and outer:
                polys = list(shapely.get_parts(shapely.polygonize(outer)))
                if polys:
                    g = shapely.union_all(polys)
                    holes = list(shapely.get_parts(shapely.polygonize(inner))) if inner else []
                    if holes:
                        g = g.difference(shapely.union_all(holes))
                    rows["polygons"].append({**base, "geometry": shapely.make_valid(g)})
            elif outer:
                rows["lines"].append({**base, "geometry": shapely.line_merge(shapely.union_all(outer))})
    return rows_to_layers(rows, top_tags)


def rows_to_layers(rows: dict, top_tags: int = 24) -> dict:
    """{"points" | "lines" | "polygons": [{osm_type, osm_id, tags, geometry}]} -> GeoDataFrames: the commonest tags as columns, every tag in `tags`
    (JSON). Shared by the Overpass answer and a Geofabrik extract (geofabrik.py), so a layer reads the same from either."""
    import geopandas as gpd
    out = {}
    for kind, rs in rows.items():
        if not rs:
            continue
        counts: dict[str, int] = {}
        for r in rs:
            for k in r["tags"]:
                counts[k] = counts.get(k, 0) + 1
        keep = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top_tags]]
        data = []
        for r in rs:
            row = {"osm_type": r["osm_type"], "osm_id": r["osm_id"], **{_col(k): r["tags"].get(k) for k in keep},
                   "tags": json.dumps(r["tags"], ensure_ascii=False, sort_keys=True), "geometry": r["geometry"]}
            data.append(row)
        out[kind] = gpd.GeoDataFrame(data, geometry="geometry", crs="EPSG:4326")
    return out


AREA_KEYS = ("building", "landuse", "amenity", "leisure", "natural", "shop", "tourism", "man_made", "place", "boundary", "water", "aeroway", "military", "office", "parking")


def _is_area(tags: dict) -> bool:
    if tags.get("area") == "no":
        return False
    if tags.get("area") == "yes":
        return True
    if tags.get("natural") == "coastline" or tags.get("barrier") or (tags.get("highway") and tags.get("area") != "yes"):
        return False
    return any(k in tags for k in AREA_KEYS)


def _col(k: str) -> str:
    c = re.sub(r"[^A-Za-z0-9_]", "_", k)
    return c if c not in ("osm_type", "osm_id", "tags", "geometry") else f"tag_{c}"


def osm_base(doc: dict) -> str | None:
    return ((doc.get("osm3s") or {}).get("timestamp_osm_base")) or None


# ---------------------------------------------------------------------------------------------------------------- fetch
def fetch(tags: list[str] | None, bbox, out, *, own_query: str | None = None, http=None, sleep=time.sleep, mirrors=MIRRORS) -> dict:
    """Run the query, write one GeoPackage layer per geometry type and a record beside it; -> the file, counts, OSM base time, findings."""
    import ogc_http as oh
    p = plan(tags, bbox, own_query)
    h = http or oh.SafeHTTP(limits={"timeout_s": 200, "max_bytes": MAX_BYTES, "retries": 0}, sleep=sleep)
    doc, host, tried = _post_overpass(p["query"], h, sleep, mirrors)
    layers = elements_to_layers(doc)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    counts = {}
    for kind, gdf in layers.items():
        gdf.to_file(out, layer=kind, driver="GPKG")
        counts[kind] = int(len(gdf))
    fs = list(p["findings"])
    if not counts:
        fs.append(finding("MAP-OSM-004", "WARN", "the query matched nothing in the box: check the tags and the box (OpenStreetMap carries only what "
                          "contributors have mapped)"))
    if tried:
        fs.append(finding("MAP-OSM-005", "INFO", f"{len(tried)} attempt(s) before {host} answered (" +
                          ", ".join(f"{t['host']} {t['status'] or 'refused'}" for t in tried[:6]) + ")"))
    base = osm_base(doc)
    rec = {"fetch_version": 1, "entry": ENTRY, "host": host, "query": p["query"], "bbox": list(check_bbox(bbox)) if bbox is not None else None,
           "osm_base": base, "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat(), "counts": counts,
           "attempts": tried, "findings": fs, "file": str(out) if counts else None,
           "provenance": {"source": f"OpenStreetMap via the Overpass API ({host})", "license": LICENSE, "attribution": CREDIT,
                          "library_ref": ENTRY, "vintage": f"OSM base {base}" if base else "live", "note": "counts are what OpenStreetMap contributors have mapped"}}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    gc.ledger_note(outputs=[str(out)] if counts else None)
    return {"ok": True, "file": rec["file"], "record": str(out.with_suffix(".json")), "layers": counts, "osm_base": base, "host": host,
            "findings": fs, "credit": CREDIT}


# ---------------------------------------------------------------------------------------------------------------- history (ohsome)
def _ohsome_system():
    import declared as dcl
    ops = ["elements/count", "elements/geometry", "elements/centroid", "elementsFullHistory/geometry", "contributions/geometry",
           "contributions/count", "users/count"]
    reqs = [dcl.Declared("POST", f"{OHSOME_BASE}/{o}", body="form", idempotent=True, changes=False, note=f"ohsome {o}") for o in ops]
    return dcl.System(HISTORY_ENTRY, OHSOME_HOST, reqs, limits={"sequential": True}, kind="entry")


def tiles(bbox, max_deg2: float = TILE_DEG2) -> list[tuple[float, float, float, float]]:
    w, s, e, n = check_bbox(bbox)
    import math
    k = max(1, math.ceil(math.sqrt(((e - w) * (n - s)) / max_deg2)))
    dx, dy = (e - w) / k, (n - s) / k
    return [(w + i * dx, s + j * dy, w + (i + 1) * dx, s + (j + 1) * dy) for i in range(k) for j in range(k)]


def _ohsome(h, op: str, form: dict, sleep) -> dict:
    import ogc_http as oh
    url = f"https://{OHSOME_HOST}{OHSOME_BASE}/{op}"
    last = None
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            r = h.request("POST", url, system=_ohsome_system(), data=form, expect="json", max_bytes=MAX_BYTES)
            return json.loads(r.content)
        except oh.ServiceError as ex:
            last = ex
            if ex.status in RETRY_STATUS and attempt < len(BACKOFF_S):
                sleep(BACKOFF_S[attempt])
                continue
            raise
    raise last  # pragma: no cover


def _bbox_form(b) -> str:
    return ",".join(f"{v:.6f}" for v in b)


def history(op: str, osm_filter: str, *, bbox=None, polygon=None, time_: str, out, http=None, sleep=time.sleep) -> dict:
    """ohsome history as a table or layer. op counts | born | snapshot | users."""
    import ogc_http as oh
    import pandas as pd
    h = http or oh.SafeHTTP(limits={"timeout_s": 300, "max_bytes": MAX_BYTES, "retries": 0}, sleep=sleep)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fs: list[dict] = []
    area = [_bbox_form(t) for t in tiles(bbox)] if bbox is not None else []
    if op == "users":
        if polygon is None and bbox is None:
            raise OsmError("users needs --polygon (a GeoJSON file) or --bbox", "MAP-OSM-002", gc.EXIT_USAGE)
        form = {"filter": osm_filter, "time": time_}
        if polygon is not None:
            form["bpolys"] = Path(polygon).read_text(encoding="utf-8") if Path(str(polygon)).is_file() else str(polygon)
        else:
            form["bboxes"] = _bbox_form(check_bbox(bbox))
        doc = _ohsome(h, "users/count", form, sleep)
        df = pd.DataFrame([{"from": r.get("fromTimestamp"), "to": r.get("toTimestamp"), "users": r.get("value")} for r in doc.get("result") or []])
        df.to_csv(out, index=False)
        res = {"rows": int(len(df))}
    elif op == "counts":
        rows = []
        for b in area:
            doc = _ohsome(h, "elements/count", {"bboxes": b, "filter": osm_filter, "time": time_}, sleep)
            rows += [{"time": r.get("timestamp"), "value": r.get("value")} for r in doc.get("result") or []]
        df = pd.DataFrame(rows).groupby("time", as_index=False)["value"].sum() if rows else pd.DataFrame(columns=["time", "value"])
        df.to_csv(out, index=False)
        res = {"rows": int(len(df))}
    elif op in ("born", "snapshot"):
        feats, why = [], None
        for b in area:
            if op == "born":
                doc = _ohsome(h, "contributions/geometry", {"bboxes": b, "filter": osm_filter, "time": time_,
                                                             "properties": "metadata,tags", "clipGeometry": "false"}, sleep)
                feats += doc.get("features") or []
            else:
                try:
                    doc = _ohsome(h, "elements/geometry", {"bboxes": b, "filter": osm_filter, "time": time_, "properties": "tags"}, sleep)
                    feats += doc.get("features") or []
                except oh.ServiceError as ex:
                    if ex.status != 403:
                        raise
                    why = f"ohsome refused elements/geometry ({ex.status}); read elementsFullHistory over the hour before {time_} instead"
                    t1 = dt.datetime.fromisoformat(str(time_).replace("Z", "+00:00")[:19])
                    t0 = t1 - dt.timedelta(hours=1)
                    doc = _ohsome(h, "elementsFullHistory/geometry", {"bboxes": b, "filter": osm_filter, "properties": "tags",
                                                                     "time": f"{t0.isoformat()},{t1.isoformat()}"}, sleep)
                    feats += [f for f in doc.get("features") or [] if (f.get("properties") or {}).get("@validTo", "") >= t1.isoformat()[:19]
                              or not (f.get("properties") or {}).get("@validTo")]
        if why:
            fs.append(finding("MAP-OSM-006", "INFO", why))
        gdf = born_table(feats) if op == "born" else _features(feats)
        if out.exists():
            out.unlink()
        if len(gdf):
            gdf.to_file(out, driver="GPKG")
        res = {"features": int(len(gdf))}
        if op == "born":
            res.update(created=int(gdf["created"].notna().sum()) if len(gdf) else 0, deleted=int(gdf["deleted"].notna().sum()) if len(gdf) else 0)
    else:
        raise OsmError(f"history op {op!r} is one of counts, born, snapshot, users", "MAP-OSM-002", gc.EXIT_USAGE)
    rec = {"fetch_version": 1, "entry": HISTORY_ENTRY, "op": op, "filter": osm_filter, "time": time_, "bbox": list(check_bbox(bbox)) if bbox is not None else None,
           "tiles": len(area), "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat(), "findings": fs, **res,
           "provenance": {"source": "OpenStreetMap full history via the ohsome API (HeiGIT)", "license": LICENSE, "attribution": CREDIT,
                          "library_ref": HISTORY_ENTRY}}
    out.with_suffix(out.suffix + ".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    gc.ledger_note(outputs=[str(out)])
    return {"ok": True, "file": str(out), **res, "findings": fs, "credit": CREDIT}


def _features(feats: list[dict]):
    import geopandas as gpd
    from shapely.geometry import shape
    rows = []
    for f in feats:
        pr = f.get("properties") or {}
        try:
            g = shape(f.get("geometry"))
        except Exception:  # noqa: BLE001
            continue
        rows.append({"osm_id": pr.get("@osmId"), **{_col(k): v for k, v in pr.items() if not k.startswith("@")}, "geometry": g})
    if not rows:
        return gpd.GeoDataFrame({"osm_id": []}, geometry=[], crs="EPSG:4326")
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return g.drop_duplicates("osm_id", keep="last")


def born_table(feats: list[dict]):
    """One row per feature: created (first creation), deleted (a deletion after it, if any), the last shape and its tags. A building deleted
    and created again keeps its first creation and loses the deletion."""
    import geopandas as gpd
    from shapely.geometry import shape
    by: dict = {}
    for f in feats:
        pr = f.get("properties") or {}
        oid = pr.get("@osmId")
        if not oid:
            continue
        t = pr.get("@timestamp") or pr.get("@validFrom")
        r = by.setdefault(oid, {"osm_id": oid, "created": None, "deleted": None, "geometry": None, "tags": {}})
        if pr.get("@creation") or "CREATION" in str(pr.get("@contributionTypes") or ""):
            if t and (r["created"] is None or t < r["created"]):
                r["created"] = t
            if r["deleted"] and t and t > r["deleted"]:
                r["deleted"] = None                           # created again after a deletion
        if pr.get("@deletion") or "DELETION" in str(pr.get("@contributionTypes") or ""):
            r["deleted"] = t
            continue
        if f.get("geometry"):
            try:
                r["geometry"] = shape(f["geometry"])
            except Exception:  # noqa: BLE001
                pass
        r["tags"].update({k: v for k, v in pr.items() if not k.startswith("@")})
    rows = [{"osm_id": r["osm_id"], "created": r["created"], "deleted": r["deleted"], "source": r["tags"].get("source"),
             "tags": json.dumps(r["tags"], ensure_ascii=False, sort_keys=True), "geometry": r["geometry"]} for r in by.values() if r["geometry"] is not None]
    if not rows:
        return gpd.GeoDataFrame({"osm_id": [], "created": [], "deleted": []}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


# ---------------------------------------------------------------------------------------------------------------- MapSpec source
def source_layer(src: dict, base: Path, bbox_wgs=None):
    """`source: {type: osm, tags: [...], bbox | region, geometry: points|lines|polygons}`: the cached GeoPackage under data/osm/ (named by a hash
    of the query), fetched once through Overpass when it is not there. -> (GeoDataFrame, provenance record)."""
    import hashlib

    import geopandas as gpd
    tags = [str(t) for t in (src.get("tags") or [])]
    b = src.get("bbox") or bbox_wgs
    if b is None:
        raise OsmError("an osm source needs tags and a bbox (or a region: block on the map)", "MAP-OSM-002", gc.EXIT_USAGE)
    ext = src.get("extract")
    if ext and src.get("query"):
        raise OsmError("an extract is read by tags; a query of your own goes to Overpass (leave extract out)", "MAP-OSM-002", gc.EXIT_USAGE)
    q = src.get("query") or query(tags, b)
    want = src.get("geometry")
    folder = Path(src.get("cache") or "data/osm")
    folder = folder if folder.is_absolute() else base / folder
    one = None
    if ext:                                             # 5.4.0 (P4.4): the day's Geofabrik extract in place of Overpass, for a region
        q = f"geofabrik:{str(ext).lower()}:{q}"
        if want in ("points", "lines", "polygons"):     # a layer draws one geometry: one pass over the extract, kept under its own name,
            one = folder / f"osm_{hashlib.sha256((q + ':' + want).encode()).hexdigest()[:12]}.gpkg"   # unless the whole answer is already kept
    key = hashlib.sha256(q.encode()).hexdigest()[:12]
    f = folder / f"osm_{key}.gpkg"
    if one is not None and not f.is_file():
        f = one
    if not f.is_file():
        if os.environ.get("GMAP_OFFLINE") == "1":
            raise OsmError(f"the OSM query is not cached ({f.name}) and GMAP_OFFLINE=1 forbids fetching it", "MAP-OSM-001", gc.EXIT_REFUSED)
        if ext:
            import geofabrik
            geofabrik.fetch(tags, b, f, region=None if str(ext).lower() in ("auto", "true") else str(ext), cap_mb=src.get("cap_mb"),
                            kinds=[want] if f == one else None)
        else:
            fetch(tags, b, f, own_query=src.get("query"))
    rec = json.loads(f.with_suffix(".json").read_text(encoding="utf-8")) if f.with_suffix(".json").is_file() else {}
    import pyogrio
    layers = [x[0] for x in pyogrio.list_layers(f)] if f.is_file() else []
    if not layers:
        raise OsmError("the OSM query matched nothing", "MAP-OSM-004", gc.EXIT_LINT)
    lay = want if want in layers else (want or layers[0])
    if lay not in layers:
        raise OsmError(f"the OSM answer has no {lay} (it has {', '.join(layers)})", "MAP-OSM-004", gc.EXIT_LINT)
    gdf = gpd.read_file(f, layer=lay)
    return gdf, rec.get("provenance") or {"source": "OpenStreetMap via the Overpass API", "license": LICENSE, "attribution": CREDIT}


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["plan", "fetch", "history"])
    ap.add_argument("hop", nargs="?", choices=["counts", "born", "snapshot", "users"])
    ap.add_argument("--tags", action="append", default=[])
    ap.add_argument("--query-file")
    ap.add_argument("--bbox")
    ap.add_argument("--polygon")
    ap.add_argument("--filter")
    ap.add_argument("--time")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        q = Path(a.query_file).read_text(encoding="utf-8") if a.query_file else None
        if a.op == "plan":
            print(json.dumps(plan(a.tags, a.bbox, q), indent=2))
            return gc.EXIT_OK
        if not a.out:
            raise OsmError("--out names the file to write", "MAP-OSM-002", gc.EXIT_USAGE)
        if a.op == "fetch":
            r = fetch(a.tags, a.bbox, a.out, own_query=q)
        else:
            if not (a.hop and a.filter and a.time):
                raise OsmError("history needs counts|born|snapshot|users, --filter and --time", "MAP-OSM-002", gc.EXIT_USAGE)
            r = history(a.hop, a.filter, bbox=a.bbox, polygon=a.polygon, time_=a.time, out=a.out)
        print(json.dumps(r, indent=2, default=str))
        return gc.EXIT_OK
    except OsmError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return ex.code


if __name__ == "__main__":
    sys.exit(main())
