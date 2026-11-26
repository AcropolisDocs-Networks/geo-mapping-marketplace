#!/usr/bin/env python3
"""window_read: a library source kept as large GeoParquet files in a public bucket, read for the map's window only (5.4.0, open data portals
P4.1 and P4.3).

    window_read.py read ENTRY --bbox=W,S,E,N [--where "country = 'KE'"] [--out F.parquet] [--refresh]
    window_read.py plan ENTRY --bbox=W,S,E,N            the files and row groups the window touches, and the bytes they hold; nothing is read

A library entry says how in a `delivery.window` block, so a new source of this kind is data, never code:

    delivery:
      mode: fetch
      window:
        reader: overture                    Overture Maps: its pinned release, theme and type (overture_io reads it)
        theme: places
        type: place
        release: 2026-11-18.0
        cap_mb: 400                         the most the window may cost; over it the read stops and nothing is kept
        max_span_deg: 12                    a window wider than this is refused: the source is read for a region, not a continent

        reader: geoparquet                  any GeoParquet in a public bucket (AWS, Source Cooperative)
        list: https://data.source.coop/fused/?list-type=2&prefix=fsq-os-places/2025-02-06/places/      the bucket's own listing, or
        files: [https://host/a.parquet, ...]
        bbox: bbox                          the struct column with xmin, ymin, xmax, ymax whose statistics pick the row groups
        geometry: geometry                  the WKB column (or {lon: longitude, lat: latitude})
        columns: [name, country, ...]       what is read; a column not named is not fetched
        first_of: {category: fsq_category_labels}      a list column read as its first value, under a plain name

Every byte goes through SafeHTTP as an HTTPS range request (public addresses, capped, audited): no cloud keys, no DuckDB httpfs. Each file's
footer is read once and kept, with the box that holds its rows, so a later window opens only the files it needs. Only the row groups whose
box statistics meet the window are read, then only the rows whose box meets it. The result is kept beside the library cache with a record
(the files, the row groups, the bytes, the requests, the seconds), and a later map inside a kept window reads nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

DEFAULT_CAP_MB, MAX_SPAN_DEG = 300.0, 12.0
PRIVATE_COLUMNS = ("tel", "phone", "phones", "email", "emails", "socials", "facebook_id", "instagram", "twitter")     # never read, whatever an entry lists


class WindowError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


READERS = ("overture", "geoparquet", "tnm")


def block(entry) -> dict | None:
    """The entry's `delivery.window` block, or None."""
    w = ((entry or {}).get("delivery") or {}).get("window") if isinstance(entry, dict) else None
    return w if isinstance(w, dict) and w.get("reader") in READERS else None


def cache_root(entry_id: str) -> Path:
    return gc.outputs_dir() / "cache" / "window" / entry_id


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": 90, "max_bytes": 200_000_000, "retries": 1})


def _kept(root: Path, bbox, ident: str) -> tuple[Path, dict] | None:
    for rp in sorted(root.glob("*.json")):
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        w = rec.get("window_wgs84") or []
        f = rp.with_suffix(".parquet")
        if len(w) == 4 and f.is_file() and rec.get("ident") == ident and w[0] <= bbox[0] and w[1] <= bbox[1] and w[2] >= bbox[2] and w[3] >= bbox[3]:
            return f, rec
    return None


# ------------------------------------------------------------------------------------------------ any GeoParquet in a public bucket
def list_objects(list_url: str, http) -> list[dict]:
    """[{url, size}] of the .parquet objects an S3-style listing address names (ListObjectsV2, a read-only GET, followed while truncated)."""
    u = urllib.parse.urlsplit(list_url)
    if u.scheme != "https":
        raise WindowError("a listing address is https", gc.EXIT_USAGE)
    base = f"https://{u.netloc}{u.path}"
    q = dict(urllib.parse.parse_qsl(u.query))
    q.setdefault("list-type", "2")
    out, token = [], None
    for _page in range(50):
        params = dict(q)
        if token:
            params["continuation-token"] = token
        r = http.get(base, params, max_bytes=8_000_000)
        if r.status != 200:
            raise WindowError(f"the bucket's listing answered HTTP {r.status}", gc.EXIT_SERVICE)
        s = r.content.decode("utf-8", "replace")
        for blk in re.findall(r"<Contents>(.*?)</Contents>", s, re.S):
            k, z = re.search(r"<Key>([^<]*)</Key>", blk), re.search(r"<Size>(\d+)</Size>", blk)
            if k and z and k.group(1).endswith(".parquet") and "/_" not in "/" + k.group(1):
                out.append({"url": base.rstrip("/") + "/" + urllib.parse.quote(k.group(1)), "size": int(z.group(1))})
        m = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", s)
        if "<IsTruncated>true</IsTruncated>" in s and m:
            token = m.group(1)
            continue
        break
    return out


def _stat_cols(md, bcol: str) -> dict | None:
    names = [md.schema.column(i).path for i in range(md.num_columns)]
    want = {k: f"{bcol}.{k}" for k in ("xmin", "xmax", "ymin", "ymax")}
    return {k: names.index(v) for k, v in want.items()} if all(v in names for v in want.values()) else None


def _groups(md, bbox, cols) -> tuple[list[int], list | None]:
    """(row groups whose box statistics meet the window, the box that holds the whole file or None when a group has no statistics)."""
    W, S, E, N = bbox
    if cols is None:
        return list(range(md.num_row_groups)), None
    out, box, whole = [], [float("inf"), float("inf"), float("-inf"), float("-inf")], True
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        st = {k: rg.column(c).statistics for k, c in cols.items()}
        if any(s is None or not s.has_min_max for s in st.values()):
            out.append(i)
            whole = False
            continue
        box = [min(box[0], st["xmin"].min), min(box[1], st["ymin"].min), max(box[2], st["xmax"].max), max(box[3], st["ymax"].max)]
        if st["xmin"].min <= E and st["xmax"].max >= W and st["ymin"].min <= N and st["ymax"].max >= S:
            out.append(i)
    return out, ([float(v) for v in box] if whole and md.num_row_groups else None)


def read_geoparquet(w: dict, bbox, *, http=None, cap_bytes: int, footer_dir: Path | None = None, plan_only: bool = False):
    """The rows of a remote GeoParquet whose box meets the window. -> (GeoDataFrame EPSG:4326 or None for a plan, cost)."""
    import geopandas as gpd
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    import shapely
    import overture_io as ov
    h = _http(http)
    files = [{"url": str(u), "size": None} for u in (w.get("files") or [])] or list_objects(str(w["list"]), h)
    if not files:
        raise WindowError("the source lists no GeoParquet file", gc.EXIT_SERVICE)
    host = urllib.parse.urlsplit(files[0]["url"]).hostname or ""
    if any((urllib.parse.urlsplit(f["url"]).hostname or "") != host for f in files):
        raise WindowError("the source's files are on more than one host; an entry reads one bucket", gc.EXIT_USAGE)
    bcol = str(w.get("bbox") or "bbox")
    geom = w.get("geometry") or "geometry"
    first_of = {str(k): str(v) for k, v in (w.get("first_of") or {}).items()}
    want = [c for c in (w.get("columns") or []) if c not in PRIVATE_COLUMNS]
    budget = ov.Budget(cap_bytes)
    W, S, E, N = (float(v) for v in bbox)
    index_p = (footer_dir / "files.bbox.json") if footer_dir else None
    try:
        index = json.loads(index_p.read_text(encoding="utf-8")) if index_p and index_p.is_file() else {}
    except ValueError:
        index = {}
    known = len(index)
    tables, n_files, n_rg, n_rows_in, opened, est = [], 0, 0, 0, 0, 0

    def name_of(f) -> str:
        return Path(urllib.parse.urlsplit(f["url"]).path).name

    def prefix_of(f) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(f["url"]).path.lstrip("/")).rsplit("/", 1)[0]

    def kept_of(f):
        return (footer_dir / f"{name_of(f)}.meta") if footer_dir else None

    def misses(f) -> bool:
        b = index.get(name_of(f))
        return bool(b) and not (b[0] <= E and b[2] >= W and b[1] <= N and b[3] >= S)

    def footer(f, hh):
        """A file's footer: the kept copy, or read once by range request and kept."""
        kept = kept_of(f)
        if kept is not None and kept.is_file():
            try:
                return pq.read_metadata(kept), False
            except Exception:  # noqa: BLE001 - a damaged footer copy is read again
                pass
        if f["size"] is None:
            import raster_fetch as rf
            f["size"] = rf.remote_size(f["url"], hh)
            if f["size"] is None:
                raise WindowError(f"{name_of(f)} did not answer a byte-range request", gc.EXIT_SERVICE)
        md_ = pq.ParquetFile(pa.PythonFile(ov.RangeFile(hh, f["url"], f["size"], budget, prefix_of(f), host=host), mode="r")).metadata
        if kept is not None:
            kept.parent.mkdir(parents=True, exist_ok=True)
            md_.write_metadata_file(str(kept))
        return md_, True

    todo = [f for f in files if not misses(f)]                     # a file whose rows lie elsewhere is not opened
    need = [f for f in todo if not (kept_of(f) is not None and kept_of(f).is_file())]
    metas: dict = {}
    try:
        if http is None and len(need) > 2:                         # the plugin's own connections, one a thread: the footers are read side by side, once
            import concurrent.futures as cf
            import threading
            local = threading.local()

            def one(f):
                if not hasattr(local, "h"):
                    local.h = _http(None)
                return f["url"], footer(f, local.h)
            with cf.ThreadPoolExecutor(max_workers=ov.FOOTER_THREADS) as ex:
                for url, got in ex.map(one, need):
                    metas[url] = got
    except ov.CapExceeded as ex:
        raise WindowError(str(ex), gc.EXIT_REFUSED, "MAP-GND-003") from None
    for f in todo:
        name = name_of(f)
        try:
            md, fresh = metas.get(f["url"]) or footer(f, h)
        except ov.CapExceeded as ex:
            raise WindowError(str(ex), gc.EXIT_REFUSED, "MAP-GND-003") from None
        opened += int(fresh)
        src = ov.RangeFile(h, f["url"], f["size"] if f["size"] is not None else 0, budget, prefix_of(f), host=host) if not plan_only else None
        rgs, fbox = _groups(md, (W, S, E, N), _stat_cols(md, bcol))
        if fbox and name not in index:
            index[name] = fbox
        if not rgs:
            continue
        n_files += 1
        n_rg += len(rgs)
        est += sum(md.row_group(i).total_byte_size for i in rgs)
        if plan_only:
            continue
        if f["size"] is None:
            import raster_fetch as rf
            f["size"] = rf.remote_size(f["url"], h) or 0
            src = ov.RangeFile(h, f["url"], f["size"], budget, prefix_of(f), host=host)
        pf = pq.ParquetFile(pa.PythonFile(src, mode="r"), metadata=md)
        names = pf.schema_arrow.names
        gcols = [geom] if isinstance(geom, str) else [geom["lon"], geom["lat"]]
        cols = [c for c in dict.fromkeys((want or [n for n in names if n not in PRIVATE_COLUMNS]) + list(first_of.values()) + gcols + [bcol]) if c in names]
        try:
            t = pf.read_row_groups(rgs, columns=cols)
        except ov.CapExceeded as ex:
            raise WindowError(str(ex) + "; draw a smaller window, or raise the entry's cap for this read", gc.EXIT_REFUSED, "MAP-GND-003") from None
        n_rows_in += t.num_rows
        if bcol in t.column_names:
            bb = t.column(bcol)
            t = t.filter(pc.and_(pc.and_(pc.less_equal(pc.struct_field(bb, "xmin"), E), pc.greater_equal(pc.struct_field(bb, "xmax"), W)),
                                 pc.and_(pc.less_equal(pc.struct_field(bb, "ymin"), N), pc.greater_equal(pc.struct_field(bb, "ymax"), S))))
        elif not isinstance(geom, str):
            lo, la = t.column(geom["lon"]), t.column(geom["lat"])
            t = t.filter(pc.and_(pc.and_(pc.greater_equal(lo, W), pc.less_equal(lo, E)), pc.and_(pc.greater_equal(la, S), pc.less_equal(la, N))))
        if t.num_rows:
            tables.append(t)
    if index_p is not None and len(index) != known:
        index_p.parent.mkdir(parents=True, exist_ok=True)
        index_p.write_text(json.dumps(index), encoding="utf-8")
    cost = {"files": n_files, "row_groups": n_rg, "bytes": budget.used, "requests": budget.requests, "files_of_source": len(files), "footers_read": opened,
            "rows_read": n_rows_in, "row_group_bytes": int(est)}
    if plan_only:
        return None, cost
    if not tables:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326"), cost
    t = pa.concat_tables(tables, promote_options="default")
    if isinstance(geom, str):
        g = list(shapely.from_wkb(t.column(geom).to_numpy(zero_copy_only=False)))
    else:
        g = list(shapely.points(t.column(geom["lon"]).to_numpy(zero_copy_only=False), t.column(geom["lat"]).to_numpy(zero_copy_only=False)))
    data: dict = {}
    for c in t.column_names:
        if c == bcol or c == geom or c in first_of.values():
            continue
        if pa.types.is_nested(t.schema.field(c).type):
            continue
        data[c] = t.column(c).to_pylist()
    for out_name, col in first_of.items():
        if col in t.column_names:
            data[out_name] = [(x[0] if x else None) for x in t.column(col).to_pylist()]
    return gpd.GeoDataFrame(data, geometry=g, crs="EPSG:4326"), cost


# ------------------------------------------------------------------------------------------------ the one call
def read(entry: dict, bbox, src: dict | None = None, *, http=None, refresh: bool = False, plan_only: bool = False) -> tuple:
    """(GeoDataFrame, record) of the entry's features in the lon/lat window. Kept; a later window inside a kept one reads nothing."""
    import geopandas as gpd
    w = block(entry)
    if w is None:
        raise WindowError(f"'{(entry or {}).get('id')}' is not read by window (it has no delivery.window block)", gc.EXIT_USAGE)
    if entry.get("status") != "active":
        raise WindowError(f"library entry '{entry['id']}' is {entry.get('status')}", gc.EXIT_REFUSED, "MAP-REF-002")
    bbox = tuple(float(v) for v in bbox)
    if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise WindowError("the window is W,S,E,N with W < E and S < N", gc.EXIT_USAGE)
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    lim = float((src or {}).get("max_span_deg") or w.get("max_span_deg") or MAX_SPAN_DEG)
    if span > lim:
        raise WindowError(f"the window is {span:.1f} degrees wide; {entry['id']} is read for a region of at most {lim:g} degrees. Draw a smaller window "
                          "(for a country of that size, read it in parts and join them with a registered step)", gc.EXIT_REFUSED)
    cap_mb = float((src or {}).get("cap_mb") or w.get("cap_mb") or DEFAULT_CAP_MB)
    layer = (src or {}).get("layer") or w.get("layer")
    if w["reader"] == "tnm" and w.get("layers") and layer not in w["layers"]:
        raise WindowError(f"{entry['id']} has no layer '{layer}'; it has: {', '.join(w['layers'])} (say source.layer)", gc.EXIT_USAGE)
    ident = hashlib.sha256(json.dumps({**{k: w.get(k) for k in ("reader", "theme", "type", "release", "list", "files", "columns", "dataset", "format")},
                                       "layer": layer if w["reader"] == "tnm" else None}, sort_keys=True, default=str).encode()).hexdigest()[:10]
    root = cache_root(entry["id"])
    if not refresh and not plan_only:
        hit = _kept(root, bbox, ident)
        if hit is not None:
            g = gpd.read_parquet(hit[0])
            if tuple(hit[1]["window_wgs84"]) != bbox and len(g):
                g = g[g.geometry.intersects(__import__("shapely").box(*bbox))].copy()
            return g, {**hit[1], "cached": True, "path": str(hit[0])}
    import overture_io as ov
    t0 = time.time()
    try:
        if w["reader"] == "overture":
            rel, theme, typ = str(w.get("release") or ""), str(w.get("theme") or ""), str(w.get("type") or "")
            foot = ov.ground_dir(rel) / "_footers" / theme
            if plan_only:
                raise WindowError("a plan is given for a geoparquet source; an Overture read says its cost when it is made", gc.EXIT_USAGE)
            g, cost = ov.read_window(rel, typ, bbox, theme=theme, http=http, cap_bytes=int(cap_mb * 1e6), footer_dir=foot)
            what = f"Overture {theme}/{typ}, release {rel}"
        elif w["reader"] == "tnm":                          # USGS staged products: the file(s) over the window, kept, each layer read by box
            import tnm
            try:
                if plan_only:
                    pl = tnm.plan(w, bbox, http, cap_mb)
                    if pl["refusal"]:
                        raise WindowError(pl["refusal"], gc.EXIT_REFUSED)
                    return None, {"entry": entry["id"], "what": pl["dataset"], "window_wgs84": list(bbox), "cost": pl["cost"],
                                  "files": [{"title": i["title"], "bytes": i["bytes"], "published": i["published"]} for i in pl["take"]]}
                g, trec = tnm.read_vector(w, bbox, layer=layer, http=http, cap_mb=cap_mb, refresh=refresh)
            except tnm.TNMError as ex:
                raise WindowError(str(ex), ex.code) from None
            cost = {"files": len(trec["files"]), "row_groups": 0, "bytes": trec["bytes"], "requests": trec["requests"], "products": trec["files"],
                    "layer": trec["layer"], "published": trec["published"]}
            what = f"{trec['dataset']} ({trec['layer']}), published {trec['published']}"
        else:
            g, cost = read_geoparquet(w, bbox, http=http, cap_bytes=int(cap_mb * 1e6), footer_dir=root / "_footers", plan_only=plan_only)
            what = str(w.get("title") or "GeoParquet in a public bucket")
    except ov.CapExceeded as ex:
        raise WindowError(f"MAP-GND-003: {ex}; draw a smaller window, or say source.cap_mb for this read (the entry allows {cap_mb:g} MB)", gc.EXIT_REFUSED, "MAP-GND-003") from None
    except ov.Refused as ex:
        raise WindowError(str(ex), gc.EXIT_REFUSED) from None
    except OSError as ex:
        raise WindowError(f"the source did not answer: {str(ex)[:200]}", gc.EXIT_SERVICE) from None
    secs = round(time.time() - t0, 1)
    if plan_only:
        return None, {"entry": entry["id"], "what": what, "window_wgs84": list(bbox), **cost, "seconds": secs,
                      "cost": f"{cost['files']} file(s), {cost['row_groups']} row group(s) holding about {cost['row_group_bytes'] / 1e6:,.0f} MB uncompressed"}
    key = hashlib.sha256(json.dumps([[round(v, 4) for v in bbox], ident]).encode()).hexdigest()[:12]
    out = root / f"{key}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    g.to_parquet(out)
    lic = entry.get("license") or {}
    rec = {"entry": entry["id"], "what": what, "ident": ident, "window_wgs84": list(bbox), "features": int(len(g)), **cost, "seconds": secs, "cached": False,
           "retrieved_at": dt.date.today().isoformat(), "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "attribution": lic.get("attribution_text"),
           "license": lic.get("class"), "release": w.get("release")}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return g, {**rec, "path": str(out)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("read", "plan"))
    ap.add_argument("entry")
    ap.add_argument("--bbox", required=True, help="W,S,E,N (write --bbox=-112.4,33.2,-111.6,33.8)")
    ap.add_argument("--where", help="read: keep the rows this condition holds for, after the window (country = 'KE')")
    ap.add_argument("--out", help="read: also write the layer here (GeoParquet, GeoPackage or GeoJSON by extension)")
    ap.add_argument("--cap-mb", type=float)
    ap.add_argument("--layer", help="a source with several layers (USGS staged products): the one to read")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    try:
        import ref_library as rl
        e = next((x for x in rl.all_entries("all") if x.get("id") == a.entry), None)
        if e is None:
            raise WindowError(f"the reference library has no entry '{a.entry}'", gc.EXIT_USAGE)
        g, rec = read(e, [float(v) for v in a.bbox.split(",")], {k: v for k, v in (("cap_mb", a.cap_mb), ("layer", a.layer)) if v} or None, refresh=a.refresh, plan_only=a.cmd == "plan")
        if g is not None and a.where:
            import geo_io
            notes: list = []
            g = geo_io._post_filter(g, a.where, None, None, notes)
            rec["kept_where"] = int(len(g))
        if g is not None and a.out:
            import geo_io
            geo_io.write(g, Path(a.out), None)
            rec["out"] = a.out
        print(json.dumps(rec, indent=2, default=str))
        return gc.EXIT_OK
    except WindowError as ex:
        print(f"window_read: {ex}", file=sys.stderr)
        return ex.code


if __name__ == "__main__":
    sys.exit(main())
