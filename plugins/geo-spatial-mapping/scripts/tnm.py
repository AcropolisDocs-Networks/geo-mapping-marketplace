#!/usr/bin/env python3
"""tnm: The National Map's staged products (USGS), found for a window through the TNM Access products API and read for that window
(5.4.0, open data portals P4.5).

    tnm.py datasets                                         the product families this reads, and how
    tnm.py products nhd --bbox=-111.9,33.2,-111.6,33.42      what USGS has staged over the window (title, size, date, extent)
    tnm.py plan structures --bbox=...                       the product(s) a read would take, and what it costs
    tnm.py read structures --bbox=... [--layer NAME] [--out FILE]

USGS stages its national datasets as files: hydrography by hydrologic unit and state, structures and transportation by state, elevation as
tiles. The products API (tnmaccess.nationalmap.gov, no key) answers which files lie over a window. A vector product is a zipped GeoPackage:
the smallest one whose box holds the window is downloaded once, kept, and each layer is read for the window by the file's own index. A raster
product (the 1 m DEM) is a Cloud-Optimized GeoTIFF: only the window is read from it, by byte range (raster_fetch).

Nothing is downloaded above the cap (the entry's, or --cap-mb); the plan names the size first. Every request goes through SafeHTTP (public
hosts, GET only). USGS data is in the public domain; the credit names the product and its publication date.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

API = "https://tnmaccess.nationalmap.gov/api/v1/products"
FILE_HOSTS = ("prd-tnm.s3.amazonaws.com",)
TIMEOUT_S = 90                                    # the API answers in a second or in a minute, by the hour
PAGE = 100
DEFAULT_CAP_MB = 700

# key: the dataset name the API takes (its sbDatasetTag), the format asked for, the kind of read, the layer read when none is named
DATASETS = {
    "nhd": {"dataset": "National Hydrography Dataset (NHD) Best Resolution", "format": "GeoPackage", "kind": "vector", "layer": "NHDFlowline",
            "what": "streams, canals and water bodies, by hydrologic unit"},
    "nhdplus-hr": {"dataset": "National Hydrography Dataset Plus High Resolution (NHDPlus HR)", "format": "FileGDB", "kind": "list",
                   "what": "the network with its value-added attributes (file geodatabase: listed, not read)"},
    "wbd": {"dataset": "National Watershed Boundary Dataset (WBD)", "format": "GeoPackage", "kind": "vector", "layer": "WBDHU12",
            "what": "watershed boundaries, HUC2 to HUC12"},
    "3dhp": {"dataset": "3D Hydrography Program (3DHP)", "format": "GeoPackage", "kind": "list",
             "what": "the lidar-derived hydrography that replaces NHD (staged as one 23 GB file for the contiguous US: listed, not read)"},
    "structures": {"dataset": "National Structures Dataset (NSD)", "format": "GeoPackage", "kind": "vector", "layer": "Struct_Point",
                   "what": "schools, hospitals, fire and police stations, by state"},
    "transportation": {"dataset": "National Transportation Dataset (NTD)", "format": "GeoPackage", "kind": "vector", "layer": None,
                       "what": "roads, railways, trails and airports, by state"},
    "boundaries": {"dataset": "National Boundary Dataset (NBD)", "format": "GeoPackage", "kind": "vector", "layer": None,
                   "what": "government unit boundaries, by state"},
    "dem-1m": {"dataset": "Digital Elevation Model (DEM) 1 meter", "format": "GeoTIFF", "kind": "raster",
               "what": "the 1 m bare-earth elevation model from lidar, in 10 km tiles by project"},
    "lidar": {"dataset": "Lidar Point Cloud (LPC)", "format": "LAS,LAZ", "kind": "list",
              "what": "lidar project tiles (point clouds: listed with their sizes, not read)"},
}


class TNMError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_SERVICE, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": TIMEOUT_S, "max_bytes": 50_000_000, "retries": 1})


def spec_of(name_or_block) -> dict:
    """A dataset key ('nhd'), or a block {dataset, format, kind, layer} an entry carries -> the full block."""
    if isinstance(name_or_block, dict):
        b = dict(name_or_block)
        base = DATASETS.get(str(b.get("key") or "")) or {}
        b = {**base, **{k: v for k, v in b.items() if v is not None}}
        if not b.get("dataset"):
            raise TNMError("a TNM source names its dataset (delivery.window.dataset, as the products API lists it)", gc.EXIT_USAGE)
        b.setdefault("format", "GeoPackage")
        b.setdefault("kind", "raster" if "tif" in str(b["format"]).lower() else "vector")
        return b
    k = str(name_or_block or "").strip().lower()
    if k not in DATASETS:
        raise TNMError(f"'{name_or_block}' is not a TNM dataset this reads; it reads: {', '.join(DATASETS)}", gc.EXIT_USAGE)
    return {"key": k, **DATASETS[k]}


def _box(bbox) -> tuple[float, float, float, float]:
    b = tuple(float(v) for v in bbox)
    if len(b) != 4 or not (b[0] < b[2] and b[1] < b[3]):
        raise TNMError("the window is W,S,E,N with W < E and S < N", gc.EXIT_USAGE)
    return b


def _item(i: dict) -> dict | None:
    bb = i.get("boundingBox") or {}
    url = str(i.get("downloadURL") or "")
    try:
        box = (float(bb["minX"]), float(bb["minY"]), float(bb["maxX"]), float(bb["maxY"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not url.startswith("https://"):
        return None
    return {"title": str(i.get("title") or "")[:200], "url": url, "bytes": int(i.get("sizeInBytes") or 0), "format": i.get("format"),
            "published": str(i.get("publicationDate") or "")[:10], "updated": str(i.get("lastUpdated") or "")[:10], "extent": i.get("extent"), "box": box}


def products(spec, bbox, http=None, limit: int = 400) -> list[dict]:
    """Every staged product of the dataset whose box touches the window, as the API lists them (paged)."""
    import urllib.parse as up
    s, b, h = spec_of(spec), _box(bbox), _http(http)
    out, offset, total = [], 0, None
    while total is None or (offset < total and len(out) < limit):
        q = {"datasets": s["dataset"], "bbox": ",".join(f"{v:g}" for v in b), "prodFormats": s["format"], "max": str(PAGE), "offset": str(offset),
             "outputFormat": "JSON"}
        try:
            r = h.get(API + "?" + up.urlencode(q), expect="json")
            doc = json.loads(r.content)
        except ValueError:
            raise TNMError("The National Map's products API answered with something that is not JSON (it does this when busy): try again in a minute") from None
        except Exception as ex:  # noqa: BLE001 - SafeHTTP's refusals and timeouts, said as this source's
            raise TNMError(f"The National Map's products API did not answer ({str(ex)[:160]}); it can take a minute: try again") from None
        if doc.get("errors"):
            raise TNMError(f"The National Map's products API refused the request: {str(doc['errors'])[:200]}")
        total = int(doc.get("total") or 0)
        items = doc.get("items") or []
        out += [x for x in (_item(i) for i in items) if x]
        if not items:
            break
        offset += len(items)
    return out


def _holds(box, b) -> bool:
    return box[0] <= b[0] and box[1] <= b[1] and box[2] >= b[2] and box[3] >= b[3]


def _touches(box, b) -> bool:
    return box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1]


_STATES = None
_HU2 = None


def hu2_over(bbox) -> list[str] | None:
    """5.5.0 (D26-01): the two-digit codes of the hydrologic regions (HU-2) whose outline (USGS WBD, bundled, generalized) meets the window;
    None when the outline is not at hand. A region's box reaches far past its shape: Arizona's window took the Missouri region (554 MB)."""
    global _HU2
    try:
        if _HU2 is None:
            import geopandas as gpd
            _HU2 = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "us_wbd_hu2.parquet")[["huc2", "geometry"]]
        import shapely
        hit = _HU2[_HU2.geometry.intersects(shapely.box(*_box(bbox)))]
        return [str(c).zfill(2) for c in hit["huc2"]]
    except Exception:  # noqa: BLE001 - without the outlines every region whose box touches is taken
        return None




def _hu_code(item: dict) -> str | None:
    """The HU-2 region a hydrologic-unit product is for, from its file name (WBD_15_HU2_GPKG.zip, NHD_H_1506_HU4_GPKG.zip): its first two digits."""
    f = Path(item["url"]).name
    m = re.search(r"_(\d{2,8})_HU\d", f, re.I) or re.search(r"_(\d{2,8})_", f)
    return m.group(1)[:2] if m else None


def states_over(bbox) -> list[str] | None:
    """The names of the US states whose shape (the bundled Census outline) meets the window; None when the outline is not at hand."""
    global _STATES
    try:
        if _STATES is None:
            import geopandas as gpd
            _STATES = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "us_states_cb.parquet")[["NAME", "geometry"]]
        import shapely
        hit = _STATES[_STATES.geometry.intersects(shapely.box(*_box(bbox)))]
        return [str(n) for n in hit["NAME"]]
    except Exception:  # noqa: BLE001 - without the outline every state whose box touches is taken
        return None


def _state_of(item: dict, names: list[str]) -> str | None:
    """The state a by-state product is for: the longest state name its file name carries (West_Virginia, not Virginia)."""
    f = Path(item["url"]).name.lower()
    hit = [n for n in names if n.lower().replace(" ", "_") in f]
    return max(hit, key=len) if hit else None


def pick_vector(items: list[dict], bbox, cap_mb: float | None = None, max_files: int = 6) -> list[dict]:
    """The products that together hold the window, from the smallest kind of unit that does it in a few files. A product's box is not its
    coverage (a hydrologic unit is a watershed, a state has a shape), so every unit of one kind whose box touches the window is taken: a
    window across two watersheds reads both. Kinds are tried from the smallest unit up (HU-8, HU-4, state, national) until one fits in
    `max_files` files under the cap; when none fits, the smallest kind is returned and the plan refuses it by size."""
    b = _box(bbox)
    kinds: dict = {}
    for i in items:
        if i["bytes"] > 0 and _touches(i["box"], b):
            kinds.setdefault(str(i.get("extent") or ""), []).append(i)
    if not kinds:
        return []
    hu = hu2_over(b)
    if hu is not None:                                       # 5.5.0 (D26-01): a hydrologic unit is kept only where its region's outline meets the window
        for k in [k for k in kinds if re.match(r"HU-?\d", k, re.I)]:
            kept = [i for i in kinds[k] if _hu_code(i) is None or _hu_code(i) in hu]
            if kept:
                kinds[k] = kept
    over = states_over(b)
    if over is not None and "State" in kinds:               # a state's box reaches over its neighbours; its shape does not
        all_names = [str(n) for n in _STATES["NAME"]]
        kept = [i for i in kinds["State"] if _state_of(i, all_names) in over or _state_of(i, all_names) is None]
        if kept:
            kinds["State"] = kept
    area = lambda i: (i["box"][2] - i["box"][0]) * (i["box"][3] - i["box"][1])  # noqa: E731
    order = sorted(kinds.values(), key=lambda g: sorted(area(i) for i in g)[len(g) // 2])
    newest = []
    for g in order:                                     # one file a unit: the latest publication when a unit is listed twice
        seen: dict = {}
        for i in g:
            k = tuple(round(v, 3) for v in i["box"])
            if k not in seen or i["published"] > seen[k]["published"]:
                seen[k] = i
        newest.append(sorted(seen.values(), key=lambda i: i["url"]))
    for g in newest:
        if len(g) <= max_files and (cap_mb is None or sum(i["bytes"] for i in g) / 1e6 <= cap_mb):
            return g
    return min(newest, key=lambda g: sum(i["bytes"] for i in g))


def pick_raster(items: list[dict], bbox) -> list[dict]:
    """The tiles over the window, one a footprint: where two lidar projects cover the same tile, the later publication."""
    b = _box(bbox)
    best: dict = {}
    for i in items:
        if not _touches(i["box"], b):
            continue
        k = tuple(round(v, 2) for v in i["box"])
        if k not in best or i["published"] > best[k]["published"]:
            best[k] = i
    return sorted(best.values(), key=lambda i: i["url"])


def plan(spec, bbox, http=None, cap_mb: float | None = None) -> dict:
    s, b = spec_of(spec), _box(bbox)
    items = products(s, b, http)
    cap = float(cap_mb or s.get("cap_mb") or DEFAULT_CAP_MB)
    if s["kind"] == "raster":
        take = pick_raster(items, b)
        cost = f"{len(take)} tile(s) read by window (the files hold {sum(i['bytes'] for i in take) / 1e6:,.0f} MB; only the window's cells are fetched)"
        refusal = None
    elif s["kind"] == "vector":
        take = pick_vector(items, b, cap)
        mb = sum(i["bytes"] for i in take) / 1e6
        cost = f"{len(take)} file(s), {mb:,.0f} MB downloaded once and kept; each layer is then read for the window from the kept file"
        refusal = (f"the product(s) over this window hold {mb:,.0f} MB; the cap is {cap:g} MB. Say a higher cap for this read (source.cap_mb, or "
                   "--cap-mb), or draw a window inside one hydrologic unit or state") if mb > cap else None
    else:
        take, refusal = [], None
        cost = f"{len(items)} product(s) listed ({sum(i['bytes'] for i in items) / 1e9:,.1f} GB); this format is listed, not read"
    if s["kind"] != "list" and not take:
        refusal = f"USGS has staged no {s['dataset']} product ({s['format']}) over this window"
    return {"dataset": s["dataset"], "format": s["format"], "kind": s["kind"], "window_wgs84": list(b), "listed": len(items), "take": take, "cost": cost,
            "cap_mb": cap, "refusal": refusal, "hosts": ["tnmaccess.nationalmap.gov", *FILE_HOSTS]}


def _file_ok(url: str) -> None:
    import urllib.parse as up
    u = up.urlsplit(url)
    if u.scheme != "https" or u.hostname not in FILE_HOSTS:
        raise TNMError(f"the products API named a file on {u.hostname}, which is not The National Map's public bucket; it is not fetched", gc.EXIT_REFUSED)


def cache_root() -> Path:
    return gc.outputs_dir() / "cache" / "tnm"


def tiles_for(block: dict, bbox, http=None) -> list[tuple[str, str]]:
    """For raster_fetch.cog_tiles: [(tile name, url)] of the GeoTIFF tiles over the window."""
    p = plan(block, bbox, http)
    if p["refusal"]:
        raise TNMError(p["refusal"], gc.EXIT_REFUSED)
    for i in p["take"]:
        _file_ok(i["url"])
    return [(Path(i["url"]).stem, i["url"]) for i in p["take"]]


def layers_of(gpkg: Path) -> list[tuple[str, str | None]]:
    import pyogrio
    return [(str(n), (str(t) if t else None)) for n, t in pyogrio.list_layers(gpkg)]


def read_vector(spec, bbox, *, layer: str | None = None, http=None, cap_mb: float | None = None, refresh: bool = False):
    """(GeoDataFrame, record): the layer of the product(s) over the window, cut to the window. The zip is downloaded once and kept."""
    import geopandas as gpd
    import pandas as pd

    import ref_fetch as rf
    import warnings
    warnings.filterwarnings("ignore", message="Measured")
    s, b, h = spec_of(spec), _box(bbox), _http(http)
    if s["kind"] != "vector":
        raise TNMError(f"{s['dataset']} is not read as features ({s['kind']})", gc.EXIT_USAGE)
    t0 = time.time()
    p = plan(s, b, h, cap_mb)
    if p["refusal"]:
        raise TNMError(p["refusal"], gc.EXIT_REFUSED)
    want = layer or s.get("layer")
    parts, files, notes = [], [], []
    for i in p["take"]:
        _file_ok(i["url"])
        z = cache_root() / Path(i["url"]).name
        side = z.with_name(z.name + ".json")                 # what was downloaded: the catalog's size is not always the file's
        try:
            was = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
        except ValueError:
            was = {}
        got = (not refresh and z.is_file() and was.get("url") == i["url"] and was.get("bytes") == z.stat().st_size
               and was.get("updated") == i["updated"])
        if not got:
            if os.environ.get("GMAP_OFFLINE") == "1":
                raise TNMError(f"offline (GMAP_OFFLINE=1): {z.name} is not kept and nothing is fetched", gc.EXIT_REFUSED)
            try:
                h.download(i["url"], z, max_mb=int(max(p["cap_mb"], i["bytes"] / 1e6) + 1))
            except Exception as ex:  # noqa: BLE001
                raise TNMError(f"{z.name} did not download ({str(ex)[:160]})") from None
            side.write_text(json.dumps({"url": i["url"], "bytes": z.stat().st_size, "updated": i["updated"], "published": i["published"],
                                        "title": i["title"], "retrieved_at": dt.date.today().isoformat()}, indent=2), encoding="utf-8")
        g, more = rf.read_user_file({"id": "tnm", "delivery": {}}, z, bbox=b, layer=want)
        notes += more
        if g is not None and len(g):
            parts.append(g if g.crs is None or g.crs.equals("EPSG:4326") else g.to_crs("EPSG:4326"))
        files.append({"name": z.name, "title": i["title"], "bytes": z.stat().st_size, "published": i["published"], "extent": i.get("extent"), "kept": bool(got)})
    if parts:
        g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
        idc = next((c for c in g.columns if c.lower() in ("permanent_identifier", "permanentidentifier", "tnmid")), None)
        if idc and len(parts) > 1:
            g = g.drop_duplicates(subset=[idc]).reset_index(drop=True)
    else:
        g = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    rec = {"source": "USGS The National Map, staged products", "dataset": s["dataset"], "format": s["format"], "layer": want, "window_wgs84": list(b),
           "features": int(len(g)), "files": files, "bytes": sum(f["bytes"] for f in files if not f["kept"]), "requests": 1 + sum(1 for f in files if not f["kept"]),
           "seconds": round(time.time() - t0, 1), "published": max((f["published"] for f in files), default=None), "notes": notes,
           "retrieved_at": dt.date.today().isoformat()}
    return g, rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("datasets", "products", "plan", "read", "layers"))
    ap.add_argument("dataset", nargs="?")
    ap.add_argument("--bbox", help="W,S,E,N (write --bbox=-111.9,33.2,-111.6,33.42)")
    ap.add_argument("--layer")
    ap.add_argument("--out")
    ap.add_argument("--cap-mb", type=float)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "datasets":
            print(json.dumps({k: {"dataset": v["dataset"], "format": v["format"], "read": v["kind"], "what": v["what"]} for k, v in DATASETS.items()}, indent=2))
            return gc.EXIT_OK
        if not a.dataset or not a.bbox:
            raise TNMError(f"{a.cmd} takes a dataset and --bbox=W,S,E,N", gc.EXIT_USAGE)
        b = [float(v) for v in a.bbox.split(",")]
        if a.cmd == "products":
            items = products(a.dataset, b)
            print(json.dumps({"dataset": spec_of(a.dataset)["dataset"], "products": items, "total_bytes": sum(i["bytes"] for i in items)}, indent=2))
        elif a.cmd == "plan":
            print(json.dumps(plan(a.dataset, b, cap_mb=a.cap_mb), indent=2))
        elif a.cmd == "layers":
            p = plan(a.dataset, b, cap_mb=a.cap_mb)
            out = {}
            for i in p["take"]:
                z = cache_root() / Path(i["url"]).name
                gp = next(iter(z.parent.glob(z.stem + "__*.gpkg")), None)
                out[z.name] = layers_of(gp) if gp else "not kept yet: a read downloads it"
            print(json.dumps(out, indent=2))
        else:
            g, rec = read_vector(a.dataset, b, layer=a.layer, cap_mb=a.cap_mb, refresh=a.refresh)
            if a.out:
                import geo_io
                geo_io.write(g, Path(a.out), None)
                rec["out"] = a.out
                rec["sha256"] = hashlib.sha256(Path(a.out).read_bytes()).hexdigest()
            print(json.dumps(rec, indent=2, default=str))
        return gc.EXIT_OK
    except TNMError as ex:
        print(f"tnm: {ex}", file=sys.stderr)
        return ex.code


if __name__ == "__main__":
    sys.exit(main())
