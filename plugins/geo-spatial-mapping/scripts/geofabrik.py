#!/usr/bin/env python3
"""geofabrik: OpenStreetMap by tag from the day's regional extract (Geofabrik), for areas wider than the public Overpass API takes
(5.4.0, open data portals P4.4).

    geofabrik.py regions [TEXT]                               the extracts (554): id, name, ISO codes; TEXT narrows them
    geofabrik.py plan  --bbox=W,S,E,N [--region ID]            the extract(s) over the window, their size, what a read costs
    geofabrik.py fetch "highway=primary;highway=trunk" --bbox=W,S,E,N [--region ID] [--out F.gpkg] [--refresh]
    geofabrik.py read FILE.osm.pbf "building=*" --bbox=W,S,E,N --out F.gpkg     a .osm.pbf you already have

Geofabrik publishes OpenStreetMap as one file a region each day: 554 regions (continents, countries, US states, the provinces of the larger
countries). The region index (index-v1-nogeom.json) names each with its ISO codes and its .osm.pbf address. The region for a window is the
smallest whose country or US state meets it; the file is downloaded once, kept for 30 days, and read with GDAL's OSM driver: the features in
the box that carry the tags, as the same three layers (points, lines, polygons) an Overpass fetch writes.

A file above the cap is not downloaded (800 MB; the plan names its size and the smaller regions inside it). OpenStreetMap is ODbL: the credit
is "© OpenStreetMap contributors", and a database made from it carries the same licence. Requests go through SafeHTTP (public hosts, GET).
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
import osm  # noqa: E402

ENTRY = "ref.infrastructure.world.osm.geofabrik"
HOST = "download.geofabrik.de"
INDEX = f"https://{HOST}/index-v1-nogeom.json"
CAP_MB = 800
KEEP_DAYS = 30
MAX_REGIONS = 6
CREDIT = "© OpenStreetMap contributors (ODbL 1.0); extract by Geofabrik"
NOT_TAGS = ("osm_id", "osm_way_id", "z_order", "other_tags", "geometry")
HSTORE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')


class GeofabrikError(osm.OsmError):
    pass


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": 120, "max_bytes": 20_000_000, "retries": 1})


def cache_root() -> Path:
    return gc.outputs_dir() / "cache" / "geofabrik"


# ---------------------------------------------------------------------------------------------------------------- regions
def index(http=None, refresh: bool = False) -> list[dict]:
    """The regions as Geofabrik lists them: {id, name, parent, iso (codes), pbf}. Kept for a day."""
    f = cache_root() / "index.json"
    today = dt.date.today().isoformat()
    if f.is_file() and not refresh:
        try:
            kept = json.loads(f.read_text(encoding="utf-8"))
            if kept.get("date") == today or os.environ.get("GMAP_OFFLINE") == "1":
                return kept["regions"]
        except (ValueError, KeyError):
            pass
    if os.environ.get("GMAP_OFFLINE") == "1":
        raise GeofabrikError("offline (GMAP_OFFLINE=1): the Geofabrik region index is not kept and nothing is fetched", "MAP-OSM-001", gc.EXIT_REFUSED)
    try:
        doc = json.loads(_http(http).get(INDEX, expect="json").content)
    except Exception as ex:  # noqa: BLE001 - said as this source's
        raise GeofabrikError(f"download.geofabrik.de did not answer for its region index ({str(ex)[:160]}). It is not reachable from every network: "
                             "from a cloud workspace it is refused; from your own computer it answers") from None
    out = []
    for ft in doc.get("features") or []:
        p = ft.get("properties") or {}
        pbf = str((p.get("urls") or {}).get("pbf") or "")
        if not p.get("id") or not pbf.startswith(f"https://{HOST}/"):
            continue
        out.append({"id": str(p["id"]), "name": str(p.get("name") or p["id"]), "parent": p.get("parent"),
                    "iso": sorted({str(c).upper() for c in (p.get("iso3166-1:alpha2") or []) + (p.get("iso3166-2") or [])}), "pbf": pbf})
    if not out:
        raise GeofabrikError("the Geofabrik region index came back with no regions")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"date": today, "regions": out}), encoding="utf-8")
    return out


def find(text: str, regions: list[dict]) -> dict:
    """A region by its id (us/arizona), its name (Arizona) or an ISO code (US-AZ, KE)."""
    t = str(text or "").strip()
    low = t.lower()
    for test in (lambda r: r["id"].lower() == low, lambda r: r["name"].lower() == low, lambda r: r["iso"] == [t.upper()],
                 lambda r: r["id"].lower().endswith("/" + low)):
        hit = [r for r in regions if test(r)]
        if len(hit) == 1:
            return hit[0]
    near = [r["id"] for r in regions if low and (low in r["id"].lower() or low in r["name"].lower())][:8]
    raise GeofabrikError(f"'{t}' is not one Geofabrik region" + (f"; near it: {', '.join(near)}" if near else "; geofabrik.py regions lists them"),
                         "MAP-OSM-002", gc.EXIT_USAGE)


def children(region_id: str, regions: list[dict]) -> list[dict]:
    return sorted((r for r in regions if r.get("parent") == region_id), key=lambda r: r["id"])


def codes_over(bbox) -> list[str]:
    """ISO codes of what the window meets, from the bundled outlines: US-AZ for a US state, KE for a country."""
    import geopandas as gpd
    import shapely
    box = shapely.box(*osm.check_bbox(bbox))
    base = gc.plugin_root() / "assets" / "basemaps"
    out: list[str] = []
    try:
        st = gpd.read_parquet(base / "us_states_cb.parquet")[["STUSPS", "geometry"]]
        out += ["US-" + str(c) for c in st[st.geometry.intersects(box)]["STUSPS"]]
    except Exception:  # noqa: BLE001
        pass
    try:
        co = gpd.read_parquet(base / "ne_50m_countries.parquet")[["ISO_A2_EH", "geometry"]]
        for c in co[co.geometry.intersects(box)]["ISO_A2_EH"]:
            c = str(c).upper()
            if len(c) == 2 and c.isalpha() and not (c == "US" and out):
                out.append(c)
    except Exception:  # noqa: BLE001
        pass
    return out


def over(bbox, regions: list[dict]) -> list[dict]:
    """The smallest region carrying each code the window meets (a US state's own file; a country's; the group file when a country has none)."""
    got: dict = {}
    for code in codes_over(bbox):
        hit = [r for r in regions if code in r["iso"]]
        if not hit:
            continue
        r = min(hit, key=lambda r: (len(r["iso"]), len(r["id"])))
        got[r["id"]] = r
    return sorted(got.values(), key=lambda r: r["id"])


def _day(text) -> str | None:
    """'Tue, 24 Nov 2026 23:52:45 GMT' -> '2026-11-24'."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(text)).date().isoformat()
    except (TypeError, ValueError):
        return str(text)[:10] if text else None


def size_of(url: str, http=None) -> tuple[int | None, str | None]:
    """(bytes, last-modified) of the file behind the address, from a HEAD (the -latest address hands off to the dated file)."""
    try:
        r = _http(http).get(url, method="HEAD")
        h = {str(k).lower(): v for k, v in dict(getattr(r, "headers", {}) or {}).items()}
        n = h.get("content-length")
        return (int(n) if n and str(n).isdigit() else None), _day(h.get("last-modified"))
    except Exception:  # noqa: BLE001 - the size is then learned at the download, under the cap
        return None, None


def plan(bbox=None, region: str | None = None, tags: list[str] | None = None, http=None, cap_mb: float | None = None) -> dict:
    regions = index(http)
    cap = float(cap_mb or CAP_MB)
    if region and str(region).lower() != "auto":
        take = [find(region, regions)]
    else:
        if bbox is None:
            raise GeofabrikError("name the region (us/arizona, kenya) or give the box it is picked from", "MAP-OSM-002", gc.EXIT_USAGE)
        take = over(bbox, regions)
        if not take:
            raise GeofabrikError("no Geofabrik region was found for this window (open sea, or a place the bundled outlines do not name): name the "
                                 "region yourself (geofabrik.py regions TEXT)", "MAP-OSM-002", gc.EXIT_USAGE)
    files, total, refusal = [], 0, None
    for r in take:
        kept = kept_file(r)
        n, when = (kept[1].get("bytes"), kept[1].get("last_modified")) if kept else size_of(r["pbf"], http)
        files.append({"region": r["id"], "name": r["name"], "url": r["pbf"], "bytes": n, "last_modified": when, "kept": bool(kept)})
        total += 0 if kept else int(n or 0)
    if len(take) > MAX_REGIONS:
        refusal = (f"the window meets {len(take)} regions ({', '.join(r['id'] for r in take)}); at most {MAX_REGIONS} are read for one map. Name the one "
                   "region the map is about (extract: <id>; the rest of the window stays empty), or draw a smaller window")
    elif total / 1e6 > cap:
        big = max((f for f in files if not f["kept"]), key=lambda f: f["bytes"] or 0)
        kids = [c["id"] for c in children(big["region"], regions)]
        refusal = (f"{big['name']} is {(big['bytes'] or 0) / 1e6:,.0f} MB; the cap is {cap:g} MB a read. " +
                   (f"It is published in parts: {', '.join(kids[:16])}{' ...' if len(kids) > 16 else ''}; name the one the window is in (extract: <id>). " if kids else "") +
                   "Or say a higher cap (cap_mb)")
    cost = (f"{sum(1 for f in files if not f['kept'])} file(s) to download ({total / 1e6:,.0f} MB), kept {KEEP_DAYS} days; "
            f"{sum(1 for f in files if f['kept'])} already kept. The tags are then read from the file for the box")
    return {"regions": files, "cost": cost, "cap_mb": cap, "refusal": refusal, "hosts": [HOST], "credit": CREDIT, "license": osm.LICENSE,
            "tags": tags or [], "bbox": list(osm.check_bbox(bbox)) if bbox is not None else None,
            "note": "one extract a region a day; a kept file is used again for 30 days (refresh takes today's)"}


def _slug(region_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", region_id.lower()).strip("_")


def kept_file(region: dict, max_age_days: int = KEEP_DAYS) -> tuple[Path, dict] | None:
    f = cache_root() / f"{_slug(region['id'])}.osm.pbf"
    side = f.with_name(f.name + ".json")
    if not (f.is_file() and side.is_file()):
        return None
    try:
        rec = json.loads(side.read_text(encoding="utf-8"))
        age = (dt.date.today() - dt.date.fromisoformat(rec["retrieved_at"])).days
    except (ValueError, KeyError):
        return None
    if rec.get("bytes") != f.stat().st_size or (age > max_age_days and os.environ.get("GMAP_OFFLINE") != "1"):
        return None
    return f, rec


def download(region: dict, http=None, refresh: bool = False, cap_mb: float = CAP_MB) -> tuple[Path, dict]:
    """The region's .osm.pbf in the cache, with a record of where it came from. Kept; used again for 30 days."""
    if not refresh:
        hit = kept_file(region)
        if hit:
            return hit[0], {**hit[1], "kept": True}
    if os.environ.get("GMAP_OFFLINE") == "1":
        raise GeofabrikError(f"offline (GMAP_OFFLINE=1): the extract of {region['name']} is not kept and nothing is fetched", "MAP-OSM-001", gc.EXIT_REFUSED)
    f = cache_root() / f"{_slug(region['id'])}.osm.pbf"
    t0 = time.time()
    try:
        r = _http(http).download(region["pbf"], f, max_mb=int(cap_mb) + 1)
    except Exception as ex:  # noqa: BLE001
        raise GeofabrikError(f"the extract of {region['name']} did not download ({str(ex)[:200]})") from None
    h = {str(k).lower(): v for k, v in dict(getattr(r, "headers", {}) or {}).items()}
    rec = {"region": region["id"], "name": region["name"], "url": region["pbf"], "from": str(getattr(r, "url", "") or region["pbf"]),
           "bytes": f.stat().st_size, "last_modified": _day(h.get("last-modified")), "sha256": getattr(r, "sha256", None),
           "retrieved_at": dt.date.today().isoformat(), "seconds": round(time.time() - t0, 1)}
    f.with_name(f.name + ".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return f, {**rec, "kept": False}


# ---------------------------------------------------------------------------------------------------------------- reading a .osm.pbf
def parse_sets(tags: list[str]) -> list[list[tuple[str, str, str]]]:
    """The tag grammar of osm.py (k=v, k=*, k!=v, k~regex, k!~regex, k; a comma joins filters that must all hold) as [(key, op, value)] a set."""
    sets = []
    for item in tags or []:
        one = []
        for term in str(item).split(","):
            t = term.strip()
            if not t:
                continue
            if "=" not in t and "~" not in t:
                one.append((t, "has", ""))
                continue
            m = osm.TAG.match(t)
            if not m:
                raise GeofabrikError(f"'{term}' is not a tag filter (k=v, k=*, k!=v, k~regex)", "MAP-OSM-002", gc.EXIT_USAGE)
            k, op, v = m.groups()
            if op == "=" and v in ("*", ""):
                one.append((k, "has", ""))
            elif op == "!=" and v in ("*", ""):
                one.append((k, "hasnot", ""))
            else:
                one.append((k, op, v))
        if one:
            sets.append(one)
    if not sets:
        raise GeofabrikError("give at least one tag filter (highway=primary; building=*)", "MAP-OSM-002", gc.EXIT_USAGE)
    return sets


def _holds(tags: dict, one: list[tuple[str, str, str]]) -> bool:
    for k, op, v in one:
        have = tags.get(k)
        if op == "has":
            ok = have is not None
        elif op == "hasnot":
            ok = have is None
        elif op == "=":
            ok = have == v
        elif op == "!=":
            ok = have != v
        elif op == "~":
            ok = have is not None and re.search(v, have) is not None
        else:
            ok = have is None or re.search(v, have) is None
        if not ok:
            return False
    return True


def _sql(sets, columns: list[str]) -> str | None:
    """A first cut GDAL can apply while it reads: a row must pass the first positive filter of some set (the key present, or the key with
    its value). None when a set has no positive filter. The exact test is made on the rows that come back."""
    def lit(v: str) -> str:
        return v.replace("'", "''")

    ors = []
    for one in sets:
        pos = next(((k, op, v) for k, op, v in one if op in ("has", "=", "~")), None)
        if pos is None:
            return None
        k, op, v = pos
        if k in columns:
            ors.append(f'"{k}" = \'{lit(v)}\'' if op == "=" else f'"{k}" IS NOT NULL')
        else:
            safe = lit(k).replace("%", "").replace("_", "\\_")
            val = (lit(v).replace("%", "").replace("_", "\\_") + '"') if op == "=" and '"' not in v and "\\" not in v else ""
            ors.append(f"other_tags LIKE '%\"{safe}\"=>\"{val}%' ESCAPE '\\'")
    return " OR ".join(sorted(set(ors)))


def _tags_of(row: dict, columns: list[str]) -> dict:
    t = {}
    for c in columns:
        v = row.get(c)
        if v is not None and v == v and str(v) != "":
            t[c] = str(v)
    ot = row.get("other_tags")
    if isinstance(ot, str) and ot:
        for k, v in HSTORE.findall(ot):
            t[k.replace('\\"', '"')] = v.replace('\\"', '"').replace("\\\\", "\\")
    return t


KINDS = ("points", "lines", "polygons")


def read(pbf, tags: list[str], bbox=None, kinds=None) -> tuple[dict, dict]:
    """({"points", "lines", "polygons"} GeoDataFrames as an Overpass fetch gives them, counts read) from a .osm.pbf, for the tags and the box.
    `kinds` names the geometries wanted (a map layer draws one): each is one pass over the file, about a minute for 100 MB of extract."""
    want = [k for k in KINDS if not kinds or k in kinds]
    import warnings

    import pyogrio
    if "OSM" not in pyogrio.list_drivers():
        raise GeofabrikError("this computer's GDAL was built without the OSM driver, so a .osm.pbf cannot be read here; convert it once with ogr2ogr "
                             "(ogr2ogr -f GPKG out.gpkg in.osm.pbf) and give the GeoPackage as a file", "MAP-OSM-001", gc.EXIT_REFUSED)
    sets = parse_sets(tags)
    box = tuple(osm.check_bbox(bbox)) if bbox is not None else None
    rows = {"points": [], "lines": [], "polygons": []}
    seen = {}
    for layer, kind in (("points", "points"), ("lines", "lines"), ("multipolygons", "polygons")):
        if kind not in want:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                cols = [str(c) for c in pyogrio.read_info(str(pbf), layer=layer)["fields"]]
                tagcols = [c for c in cols if c not in NOT_TAGS]
                g = pyogrio.read_dataframe(str(pbf), layer=layer, bbox=box, where=_sql(sets, tagcols))
            except Exception as ex:  # noqa: BLE001
                raise GeofabrikError(f"{Path(str(pbf)).name} could not be read as OpenStreetMap PBF ({str(ex)[:200]})") from None
        seen[kind] = int(len(g))
        if not len(g):
            continue
        recs = g.drop(columns="geometry").to_dict("records")
        for rec, geom in zip(recs, g.geometry.values):
            if geom is None or geom.is_empty:
                continue
            t = _tags_of(rec, tagcols)
            if not any(_holds(t, one) for one in sets):
                continue
            if layer == "points":
                typ, oid = "node", rec.get("osm_id")
            elif layer == "lines":
                typ, oid = "way", rec.get("osm_id")
            else:
                rel = rec.get("osm_id")
                typ, oid = ("relation", rel) if rel is not None and rel == rel and str(rel) != "" else ("way", rec.get("osm_way_id"))
            try:
                oid = int(oid)
            except (TypeError, ValueError):
                oid = 0
            rows[kind].append({"osm_type": typ, "osm_id": oid, "tags": t, "geometry": geom})
    return osm.rows_to_layers(rows), seen


def fetch(tags: list[str], bbox, out, *, region: str | None = None, http=None, refresh: bool = False, cap_mb: float | None = None, kinds=None) -> dict:
    """The tags in the box from the day's extract(s), written as osm.fetch writes: one GeoPackage layer a geometry and a record beside it."""
    import geopandas as gpd
    import pandas as pd
    parse_sets(tags)
    p = plan(bbox, region, tags, http, cap_mb)
    if p["refusal"]:
        raise GeofabrikError(p["refusal"], "MAP-OSM-003", gc.EXIT_REFUSED)
    regions = index(http)
    t0 = time.time()
    parts, files, seen_all = {"points": [], "lines": [], "polygons": []}, [], {}
    for f in p["regions"]:
        path, rec = download(find(f["region"], regions), http, refresh, p["cap_mb"])
        layers, seen = read(path, tags, bbox, kinds)
        for k, g in layers.items():
            parts[k].append(g)
        for k, n in seen.items():
            seen_all[k] = seen_all.get(k, 0) + n
        files.append({k: rec.get(k) for k in ("region", "name", "from", "bytes", "last_modified", "retrieved_at", "kept")})
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    counts = {}
    for kind, gs in parts.items():
        if not gs:
            continue
        g = gs[0] if len(gs) == 1 else gpd.GeoDataFrame(pd.concat(gs, ignore_index=True), crs="EPSG:4326").drop_duplicates(subset=["osm_type", "osm_id"])
        g.to_file(out, layer=kind, driver="GPKG")
        counts[kind] = int(len(g))
    fs = []
    if not counts:
        fs.append(osm.finding("MAP-OSM-004", "WARN", "nothing in the box carries these tags in the extract: check the tags and the box (OpenStreetMap "
                              "carries only what contributors have mapped)"))
    dates = sorted({str(f.get("last_modified") or f.get("retrieved_at")) for f in files})
    rec = {"fetch_version": 1, "entry": ENTRY, "host": HOST, "via": "geofabrik", "regions": files, "tags": list(tags),
           "bbox": list(osm.check_bbox(bbox)) if bbox is not None else None, "read": seen_all, "kinds": [k for k in KINDS if not kinds or k in kinds], "counts": counts, "seconds": round(time.time() - t0, 1),
           "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat(), "findings": fs, "file": str(out) if counts else None,
           "provenance": {"source": f"OpenStreetMap, Geofabrik extract of {', '.join(f['name'] for f in files)}", "license": osm.LICENSE, "attribution": CREDIT,
                          "library_ref": ENTRY, "vintage": f"extract of {'; '.join(dates)}",
                          "note": "counts are what OpenStreetMap contributors have mapped; route relations (multilinestrings) are not read"}}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    gc.ledger_note(outputs=[str(out)] if counts else None)
    return {"ok": True, "file": rec["file"], "record": str(out.with_suffix(".json")), "layers": counts, "regions": files, "seconds": rec["seconds"],
            "findings": fs, "credit": CREDIT}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("regions", "plan", "fetch", "read"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--bbox", help="W,S,E,N (write --bbox=-75.8,38.4,-75.0,39.9)")
    ap.add_argument("--region", help="a region id, name or ISO code; left out, it is picked from the box")
    ap.add_argument("--out")
    ap.add_argument("--cap-mb", type=float)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "regions":
            rs = index(refresh=a.refresh)
            t = " ".join(a.args).lower()
            hit = [r for r in rs if not t or t in r["id"].lower() or t in r["name"].lower() or t.upper() in r["iso"]]
            print(json.dumps({"regions": [{k: r[k] for k in ("id", "name", "iso", "parent")} for r in hit], "count": len(hit), "of": len(rs)}, indent=2))
            return gc.EXIT_OK
        if a.cmd == "plan":
            print(json.dumps(plan(a.bbox, a.region, cap_mb=a.cap_mb), indent=2))
            return gc.EXIT_OK
        if a.cmd == "read":
            if len(a.args) < 2 or not a.out:
                raise GeofabrikError("read takes FILE.osm.pbf, the tags and --out", "MAP-OSM-002", gc.EXIT_USAGE)
            layers, seen = read(a.args[0], [t.strip() for t in a.args[1].split(";") if t.strip()], a.bbox)
            out = Path(a.out)
            out.unlink(missing_ok=True)
            for k, g in layers.items():
                g.to_file(out, layer=k, driver="GPKG")
            print(json.dumps({"file": str(out) if layers else None, "layers": {k: len(g) for k, g in layers.items()}, "read": seen, "credit": CREDIT}, indent=2))
            return gc.EXIT_OK
        if not a.args or not a.bbox:
            raise GeofabrikError("fetch takes the tags and --bbox=W,S,E,N", "MAP-OSM-002", gc.EXIT_USAGE)
        tl = [t.strip() for t in a.args[0].split(";") if t.strip()]
        out = a.out or str(gc.data_dir("osm") / ("osm_" + (re.sub(r"[^a-z0-9]+", "_", tl[0].lower()).strip("_")[:40] or "tags") + "_extract.gpkg"))
        print(json.dumps(fetch(tl, a.bbox, out, region=a.region, refresh=a.refresh, cap_mb=a.cap_mb), indent=2, default=str))
        return gc.EXIT_OK
    except osm.OsmError as ex:
        print(f"geofabrik: {ex.rule}: {ex}", file=sys.stderr)
        return ex.code


if __name__ == "__main__":
    sys.exit(main())
