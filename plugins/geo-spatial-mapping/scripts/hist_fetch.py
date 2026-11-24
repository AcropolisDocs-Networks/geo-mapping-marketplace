#!/usr/bin/env python3
"""hist_fetch: historical boundaries as of a date, from OpenHistoricalMap (1.3.0, R1-HIST).

    hist_fetch.py plan  --as-of 1861-10-24 --bbox=-125,30,-90,50 --admin-level 4 [--tag boundary=administrative]
    hist_fetch.py fetch --as-of 1861-10-24 --bbox=... --admin-level 4 --out data/territories_1861.gpkg

OpenHistoricalMap data is dedicated to the public domain (CC0) "except where otherwise noted": a feature may carry its own licence in a
`license=*` tag (CC BY, CC BY-SA), and share-alike then applies "when you reuse a significant part of the database covered by them". The
licence and source tags are kept as columns; features under CC BY or CC BY-SA add their credit to the Sources line and a share-alike line to the
terms register. The credit OHM asks for: "Map data courtesy of the OpenHistoricalMap project, in the public domain unless otherwise noted."

`plan` shows the Overpass QL, the host and a rough size. `fetch` sends one GET to the OHM Overpass instance
(https://overpass-api.openhistoricalmap.org/api/interpreter, read from the OpenHistoricalMap/Overpass wiki page on 2026-11-10), declared in the
library entry `ref.boundary.world.historical.ohm` (delivery.api). A 429 or 504 is tried once more after Retry-After (or 60 s); nothing else is
retried. The answer is capped at 200 MB. No key is ever sent.

Relations are assembled into polygons from their member ways by role (outer and inner rings polygonized; unclosed rings counted). The date
filter runs here, not in Overpass: a feature is kept when start <= as-of < end. A missing end means it still exists; a missing start excludes it
(counted, MAP-HIST-002 INFO). Partial dates are read as the first day of their period for a start (1861 -> 1861-01-01) and the first day after it
for an end (1861 -> 1862-01-01); OHM compares its dates as ISO 8601 strings, which agrees with that rule for a start. A window with no feature for
the date is MAP-HIST-001 (WARN): modern boundaries with a note on the map are the fallback.

The MapSpec op `{op: as_of, date: 1861-10-24}` (or the short form `{as_of: 1861-10-24}`) applies the same filter to any layer with
start_date / end_date columns.
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

ENTRY = "ref.boundary.world.historical.ohm"
INTERPRETER = "https://overpass-api.openhistoricalmap.org/api/interpreter"
CREDIT = "Map data courtesy of the OpenHistoricalMap project, in the public domain unless otherwise noted."
MAX_BYTES = 200_000_000
RETRY_S = 60.0
_DATE = re.compile(r"^(-?\d{1,4})(?:-(\d{2}))?(?:-(\d{2}))?$")


class HistError(Exception):
    def __init__(self, message: str, rule: str = "MAP-SVC-013", code: int = gc.EXIT_SERVICE):
        super().__init__(message)
        self.rule, self.code = rule, code


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- dates
def _parts(s) -> tuple | None:
    m = _DATE.match(str(s or "").strip())
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None), (int(m.group(3)) if m.group(3) else None)


def start_of(s) -> str | None:
    """A start date as a full ISO day: 1861 -> 1861-01-01, 1861-10 -> 1861-10-01."""
    p = _parts(s)
    if p is None:
        return None
    y, m, d = p
    return f"{y:04d}-{m or 1:02d}-{d or 1:02d}"


def end_of(s) -> str | None:
    """An end date as the first day after its period: 1861 -> 1862-01-01, 1861-10 -> 1861-11-01, 1861-10-24 -> 1861-10-24 (an end day is exclusive)."""
    p = _parts(s)
    if p is None:
        return None
    y, m, d = p
    if m is None:
        return f"{y + 1:04d}-01-01"
    if d is None:
        return f"{y + (m == 12):04d}-{(m % 12) + 1:02d}-01"
    return f"{y:04d}-{m:02d}-{d:02d}"


def as_of_mask(starts, ends, as_of: str) -> tuple[list[bool], int]:
    """(kept, excluded for a missing start): start <= as_of < end, a missing end is open."""
    day = start_of(as_of)
    if day is None:
        raise HistError(f"as-of '{as_of}' is not a date (YYYY, YYYY-MM or YYYY-MM-DD)", "MAP-HIST-003", gc.EXIT_USAGE)
    keep, no_start = [], 0
    for s, e in zip(starts, ends, strict=True):
        s0, e0 = start_of(s), end_of(e)
        if s0 is None:
            keep.append(False)
            no_start += 1
            continue
        keep.append(s0 <= day and (e0 is None or day < e0))
    return keep, no_start


def as_of_filter(gdf, as_of: str):
    """A layer's features as of a date, from its start_date / end_date columns. -> (gdf, notes, findings)."""
    if "start_date" not in gdf.columns:
        raise HistError("the layer has no start_date column, so it cannot be filtered by date", "MAP-HIST-003", gc.EXIT_USAGE)
    ends = gdf["end_date"] if "end_date" in gdf.columns else [None] * len(gdf)
    keep, no_start = as_of_mask(gdf["start_date"].tolist(), list(ends), as_of)
    out = gdf[keep].copy()
    fs = []
    if no_start:
        fs.append(finding("MAP-HIST-002", "INFO", f"{no_start} feature(s) have no start_date and are left out of the as-of {as_of} view"))
    if not len(out):
        fs.append(finding("MAP-HIST-001", "WARN", f"no feature exists on {as_of} in this window: OpenHistoricalMap's coverage is uneven; use modern "
                          "boundaries with a note on the map"))
    return out, [f"as of {as_of}: {len(out)} of {len(gdf)} feature(s) (start <= date < end)"], fs


# ---------------------------------------------------------------------------------------------------------------- the query
def check_bbox(bbox) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(v) for v in (bbox.split(",") if isinstance(bbox, str) else bbox))
    except (TypeError, ValueError):
        raise HistError("bbox is W,S,E,N in degrees", "MAP-HIST-003", gc.EXIT_USAGE) from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HistError(f"bbox {bbox} is not W,S,E,N with W < E and S < N", "MAP-HIST-003", gc.EXIT_USAGE)
    return w, s, e, n


def query(bbox, admin_level: int | str, tags: list[str] | None = None) -> str:
    w, s, e, n = check_bbox(bbox)
    flt = ""
    for t in (tags or ["boundary=administrative"]):
        k, _, v = str(t).partition("=")
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,40}", k) or (v and not re.fullmatch(r"[A-Za-z0-9_ :.-]{1,60}", v)):
            raise HistError(f"tag '{t}' is not key=value of plain words", "MAP-HIST-003", gc.EXIT_USAGE)
        flt += f'["{k}"="{v}"]' if v else f'["{k}"]'
    lvl = str(admin_level)
    if not re.fullmatch(r"\d{1,2}", lvl):
        raise HistError(f"admin level '{admin_level}' is not a number", "MAP-HIST-003", gc.EXIT_USAGE)
    return f'[out:json][timeout:120];rel{flt}["admin_level"="{lvl}"]({s:g},{w:g},{n:g},{e:g});out geom;'


def plan(as_of: str, bbox, admin_level, tags=None) -> dict:
    w, s, e, n = check_bbox(bbox)
    if start_of(as_of) is None:
        raise HistError(f"as-of '{as_of}' is not a date (YYYY, YYYY-MM or YYYY-MM-DD)", "MAP-HIST-003", gc.EXIT_USAGE)
    q = query(bbox, admin_level, tags)
    area = (e - w) * (n - s)
    est = int(min(MAX_BYTES, max(200_000, area * 120_000 * (1 if int(admin_level) <= 4 else 4))))
    return {"host": "overpass-api.openhistoricalmap.org", "method": "GET, once (a 429 or 504 once more after 60 s)", "query": q,
            "as_of": as_of, "estimate_bytes": est, "estimate_note": "a rough guess from the box's area and the level; Overpass reports no size first",
            "key": None, "credit": CREDIT}


def _system():
    import declared as dcl
    import ref_fetch as rf
    try:
        e = rf.find_entry(ENTRY)
        s = dcl.from_entry(e)
        if s is not None:
            return s
    except rf.FetchError:
        pass
    return dcl.System(ENTRY, "overpass-api.openhistoricalmap.org", [dcl.Declared("GET", "/api/interpreter")], kind="entry")


def fetch(as_of: str, bbox, admin_level, out: str | Path, tags=None, *, http=None, sleep=time.sleep) -> dict:
    import ogc_http as oh
    p = plan(as_of, bbox, admin_level, tags)
    h = http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": MAX_BYTES, "retries": 0}, sleep=sleep)
    system = _system()
    r = None
    for attempt in (0, 1):
        try:
            r = h.request("GET", INTERPRETER, system=system, params={"data": p["query"]}, expect="json", max_bytes=MAX_BYTES)
            break
        except oh.ServiceError as ex:
            if ex.status in (429, 504) and attempt == 0:
                sleep(RETRY_S)
                continue
            raise HistError(f"OpenHistoricalMap's Overpass did not answer: {ex}", ex.rule or "MAP-SVC-013") from None
    doc = json.loads(r.content)
    gdf, built = relations_to_gdf(doc)
    fs = []
    if built["unclosed"]:
        fs.append(finding("MAP-HIST-002", "INFO", f"{built['unclosed']} ring(s) did not close and were left out (relations {', '.join(map(str, built['unclosed_ids'][:5]))})"))
    total = len(gdf)
    gdf, notes, ffs = as_of_filter(gdf, as_of) if total else (gdf, [], [finding("MAP-HIST-001", "WARN", f"no boundary at admin level {admin_level} in this window: "
                                                                                              "use modern boundaries with a note on the map")])
    fs += ffs
    lic = per_feature_licences(gdf)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(gdf):
        gdf.to_file(out, layer=out.stem, driver="GPKG")
    rec = {"fetch_version": 1, "entry": ENTRY, "as_of": as_of, "bbox": list(check_bbox(bbox)), "admin_level": str(admin_level), "query": p["query"],
           "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat(), "relations": total, "kept": int(len(gdf)),
           "calls": h.audit.calls, "per_feature_licences": lic, "findings": fs, "file": str(out) if len(gdf) else None,
           "provenance": {"source": "OpenHistoricalMap", "license": "public-domain", "license_url": "https://openhistoricalmap.org/copyright",
                          "attribution": CREDIT + "".join(f" {x['credit']}" for x in lic if x.get("credit")), "library_ref": ENTRY,
                          "vintage": f"boundaries as of {as_of}", "per_feature_licences": lic}}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "file": rec["file"], "record": str(out.with_suffix(".json")), "relations": total, "kept": int(len(gdf)),
            "per_feature_licences": lic, "findings": fs, "map_note": f"Boundaries as of {_long_date(as_of)} (OpenHistoricalMap)"}


def _long_date(s: str) -> str:
    y, m, d = _parts(s) or (None, None, None)
    if y is None:
        return str(s)
    if m is None:
        return f"{y}"
    import calendar
    return f"{d} {calendar.month_name[m]} {y}" if d else f"{calendar.month_name[m]} {y}"


def per_feature_licences(gdf) -> list[dict]:
    """The licences single features carry (license=* tags) with their counts, and the credit each asks the Sources line for."""
    if "license" not in getattr(gdf, "columns", []):
        return []
    out = []
    for lic, n in gdf["license"].dropna().astype(str).value_counts().items():
        if not lic.strip() or lic.strip().upper() in ("CC0", "CC0-1.0", "PUBLIC DOMAIN", "PD"):
            continue
        srcs = sorted({str(x) for x in gdf.loc[gdf["license"] == lic, "source"].dropna()})[:3] if "source" in gdf.columns else []
        out.append({"license": lic, "count": int(n), "sources": srcs,
                    "credit": f"Some boundaries {lic}" + (f" ({'; '.join(srcs)})" if srcs else "") + "."})
    return out


def relations_to_gdf(doc: dict):
    """Overpass `out geom` relations as polygons: outer and inner member ways polygonized, the inners cut out; unclosed rings counted."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import LineString
    rows, unclosed, ids = [], 0, []
    for el in doc.get("elements") or []:
        if el.get("type") != "relation":
            continue
        tags = el.get("tags") or {}
        outer, inner = [], []
        for m in el.get("members") or []:
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p and "lon" in p]
            if len(pts) < 2:
                continue
            (inner if m.get("role") == "inner" else outer).append(LineString(pts))
        if not outer:
            continue
        polys, cuts, dangles, invalid = shapely.polygonize_full(outer)
        ext = [g for g in shapely.get_parts(polys)]
        bad = len(shapely.get_parts(dangles)) + len(shapely.get_parts(cuts))
        if bad:
            unclosed += bad
            ids.append(el.get("id"))
        if not ext:
            continue
        geom = shapely.union_all(ext)
        if inner:
            holes = [g for g in shapely.get_parts(shapely.polygonize(inner))]
            if holes:
                geom = geom.difference(shapely.union_all(holes))
        rows.append({"ohm_id": int(el.get("id") or 0), "name": tags.get("name"), "admin_level": tags.get("admin_level"),
                     "start_date": tags.get("start_date"), "end_date": tags.get("end_date"), "license": tags.get("license"), "source": tags.get("source"),
                     "geometry": shapely.make_valid(geom)})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326") if rows else gpd.GeoDataFrame(
        {"ohm_id": [], "name": [], "admin_level": [], "start_date": [], "end_date": [], "license": [], "source": []}, geometry=[], crs="EPSG:4326")
    return gdf, {"unclosed": unclosed, "unclosed_ids": ids}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["plan", "fetch"])
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--admin-level", default="4")
    ap.add_argument("--tag", action="append")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        if a.op == "plan":
            res = plan(a.as_of, a.bbox, a.admin_level, a.tag)
        else:
            if not a.out:
                print("hist_fetch: fetch needs --out FILE.gpkg", file=sys.stderr)
                return gc.EXIT_USAGE
            res = fetch(a.as_of, a.bbox, a.admin_level, a.out, a.tag)
    except HistError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return ex.code
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
