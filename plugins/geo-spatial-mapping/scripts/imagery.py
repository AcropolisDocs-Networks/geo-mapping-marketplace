#!/usr/bin/env python3
"""imagery: satellite and aerial imagery for a map's window, from a STAC catalog, as one GeoTIFF with a record (5.3.0, open data portals P3).

    imagery.py plan  ENTRY --bounds W,S,E,N [--dates 2024-06-01/2024-09-30] [--cloud 20] [--product true-color] [--pick least-cloudy]
    imagery.py fetch ENTRY --bounds W,S,E,N ...            the same, then the read
    imagery.py series [ENTRY] (--spec map.yaml [--layer ID] [--template T] | --bounds W,S,E,N) (--years 1984-2025 --season 06-01/08-31
                      [--skip 2012] | --dates-file dates.yaml) [--jobs 3] [--plan-only] [--out series.json]     (5.9.0)

5.9.0: a series reads one picture per period, several at once, each filed in the cache as it comes, under the key the map layer with those
bounds and dates reads: `--spec` takes the window the MapSpec's build reads (planned, not fetched) and the layer's own settings, so the builds
of an atlas by time (and the frames of its animation, at --template) find every picture already read. A series cut off part way picks up
where it stopped. Scenes come in tiers: clean scenes in the dates; then a pass of the next path within `fill_days` (the entry's, Landsat 16)
for an edge the dates leave; then a degraded instrument (the entry's `degraded:` list: Landsat 7 after 31 May 2003), which only fills what is
left, with more of its passes read until its stripes are filled. `platforms` / `exclude_platforms` name the satellites; the plan lists the
candidates by satellite, path and row.

A library entry with a `delivery.stac` block is all a new imagery source needs:

    delivery:
      mode: fetch
      stac:
        routes:                                     tried in order; the first that answers with scenes is used
        - {name: earth-search, root: https://earth-search.aws.element84.com/v1, collection: sentinel-2-l2a,
           assets: {red: red, green: green, blue: blue, nir: nir, mask: scl}}
        - {name: planetary-computer, root: https://planetarycomputer.microsoft.com/api/stac/v1, collection: sentinel-2-l2a, sign: planetary-computer,
           assets: {red: B04, green: B03, blue: B02, nir: B08, mask: SCL}}
        cloud_property: eo:cloud_cover               absent for aerial photography
        reflectance: {scale: 0.0001, offset: -0.1}   when the catalog's assets do not state their own
        mask: {kind: classes, bad: [0, 1, 3, 8, 9, 10]}     or {kind: bits, bad: [0, 1, 3, 4]}
        stretch: [0.0, 0.3]                          reflectance drawn as black and as white in a colour picture
        bands: {image: [red, green, blue, nir]}      one asset that holds several bands (NAIP)
        max_scenes: 6
        max_px: 2400

What it does (decision D4): the scenes over the window between two dates, the least cloudy first; the window alone is read from each scene's
Cloud-Optimized GeoTIFFs by HTTP byte range through SafeHTTP (public hosts, GET, capped), straight onto one grid in the window's UTM zone, from
the overview that fits; then one picture: `least-cloudy` (the clearest scene, the next clearest filling what it leaves) or `median` (the middle
value of every clear look at each cell), as `true-color` (red, green, blue), `false-color` (near infrared, red, green), `ndvi`, or (5.10.0) the water indexes `ndwi`
(green and near infrared) and `mndwi` (green and short-wave infrared). No atmospheric
correction beyond the publisher's Level-2 product. Anything beyond these is analysis for a registered step.

A catalog whose files are in a requester-pays bucket is refused and the free route named (D5): no cloud keys are held. Planetary Computer's
addresses are signed with its keyless token (rule 8: a slow or failing host gets one retry). Before any read, `plan` says the scenes, the
bands and what the read may cost.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import raster_fetch as rf  # noqa: E402
import raster_io as rio  # noqa: E402

PRODUCTS = {"true-color": ("red", "green", "blue"), "false-color": ("nir", "red", "green"), "ndvi": ("nir", "red"),
            "ndwi": ("green", "nir"), "mndwi": ("green", "swir16")}
INDEX = {"ndvi": "NDVI", "ndwi": "NDWI", "mndwi": "MNDWI"}      # 5.10.0: (a - b) / (a + b) of the product's two bands; above 0 is water for the two water indexes
PICKS = ("least-cloudy", "median")
MAX_SCENES, MAX_PX, MAX_SPAN_DEG = 6, 2400, 6.0
SIGNERS = {"planetary-computer": "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"}
_TOKENS: dict = {}


class ImageryError(rio.RasterError):
    """A refusal or failure with a message safe to show."""

    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


def stac_block(entry) -> dict | None:
    if isinstance(entry, str):
        try:
            entry = rf.library_entry(entry)
        except rf.RasterFetchError:
            return None
    b = ((entry or {}).get("delivery") or {}).get("stac")
    return b if isinstance(b, dict) and b.get("routes") else None


def _http(http=None):
    return http or oh.SafeHTTP(limits={"timeout_s": 90, "max_bytes": rf.MAX_BYTES, "retries": 1})


def _get_json(http, url: str, params: dict | None = None, what: str = "the catalog") -> dict:
    last = None
    for _try in (1, 2):                                    # rule 8: one retry for a slow or failing host
        try:
            r = http.get(url, params, expect="any", max_bytes=8_000_000)
            if r.status == 200:
                return json.loads(r.content)
            last = f"HTTP {r.status}"
        except oh.ServiceError as ex:
            last = oh.untrusted(ex, 160)
        except ValueError:
            last = "an answer that is not JSON"
    raise ImageryError(f"{what} did not answer ({last})", gc.EXIT_SERVICE, "MAP-SVC-001")


def params_of(src: dict | None, block: dict) -> dict:
    """What a layer asks of the imagery (source: {type: ref, id, product, dates, cloud_max, pick, scenes}), with the entry's defaults."""
    s = dict(src or {})
    product = str(s.get("product") or s.get("variant") or block.get("default_product") or "true-color").lower().strip().replace("_", "-").replace(" ", "-").replace("colour", "color")
    if product not in PRODUCTS:
        raise ImageryError(f"'{product}' is not a picture this reader makes: {', '.join(PRODUCTS)}", gc.EXIT_USAGE)
    pick = str(s.get("pick") or "least-cloudy").lower()
    if pick not in PICKS:
        raise ImageryError(f"pick is {' or '.join(PICKS)}, not '{pick}'", gc.EXIT_USAGE)
    dates = str(s.get("dates") or s.get("datetime") or "").strip()
    if not dates:
        if block.get("cloud_property"):
            raise ImageryError("imagery needs its dates: source.dates as START/END (2024-06-01/2024-09-30), or one year (2024)", gc.EXIT_USAGE)
        dates = ""
    if re.fullmatch(r"\d{4}", dates):
        dates = f"{dates}-01-01/{dates}-12-31"
    if dates and not re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}", dates):
        raise ImageryError(f"source.dates is START/END as 2024-06-01/2024-09-30 (or one year), not '{dates}'", gc.EXIT_USAGE)
    cloud = s.get("cloud_max", block.get("cloud_max", 20))
    n = int(s.get("scenes") or (3 if pick == "median" else 0) or 0)
    st = s.get("stretch")
    if st is not None and not (isinstance(st, (list, tuple)) and len(st) == 2 and 0 <= float(st[0]) < float(st[1]) <= 2):
        raise ImageryError("source.stretch is two reflectances, drawn as black and as white: [0, 0.3] (the default), [0, 0.5] for desert or snow", gc.EXIT_USAGE)
    out = {"product": product, "pick": pick, "dates": dates, "cloud_max": float(cloud) if cloud is not None else None,
           "scenes": max(0, min(int(block.get("max_scenes") or MAX_SCENES), n)), "route": s.get("route"),
           "stretch": [float(st[0]), float(st[1])] if st is not None else None}
    # 5.9.0: which satellites, whether a degraded instrument is kept for last, and how far past the dates a gap may be filled
    out["platforms"] = _names(s.get("platforms"), "platforms")
    out["exclude_platforms"] = _names(s.get("exclude_platforms"), "exclude_platforms")
    both = set(out["platforms"]) & set(out["exclude_platforms"])
    if both:
        raise ImageryError(f"{', '.join(sorted(both))} is both asked for (platforms) and left out (exclude_platforms)", gc.EXIT_USAGE)
    ad = s.get("avoid_degraded", True)
    if not isinstance(ad, bool):
        raise ImageryError("source.avoid_degraded is true (the default: a degraded instrument only fills what clean scenes leave) or false", gc.EXIT_USAGE)
    out["avoid_degraded"] = ad
    fd = s.get("fill_days", block.get("fill_days", 0))
    try:
        fd = int(fd)
    except (TypeError, ValueError):
        raise ImageryError(f"source.fill_days is a number of days (0 to 32), not '{fd}'", gc.EXIT_USAGE) from None
    if not 0 <= fd <= 32:
        raise ImageryError("source.fill_days is 0 to 32: how many days before and after the dates a scene may come from to fill an edge the dates leave", gc.EXIT_USAGE)
    out["fill_days"] = fd if out["dates"] else 0
    return out


NEW_PARAMS = {"platforms": [], "exclude_platforms": [], "avoid_degraded": True}       # 5.9.0; fill_days is said per entry


def _names(v, what: str) -> list[str]:
    if v in (None, "", []):
        return []
    items = v.split(",") if isinstance(v, str) else v
    if not isinstance(items, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in items):
        raise ImageryError(f"source.{what} is a list of satellites as the catalog names them (landsat-5, landsat-8)", gc.EXIT_USAGE)
    return sorted({x.strip().lower().replace(" ", "-").replace("_", "-") for x in items})


def cache_params(p: dict, block: dict) -> dict:
    """What a cached picture is filed under: the parameters a 5.8.0 read was filed under, plus the 5.9.0 ones only where they differ from
    their defaults, so pictures already read keep their place (a kept picture that holds a degraded scene is read again: see fetch)."""
    k = {x: v for x, v in p.items() if x not in NEW_PARAMS and x != "fill_days"}
    k.update({x: p[x] for x, d in NEW_PARAMS.items() if x in p and p[x] != d})
    if p.get("fill_days") != int(block.get("fill_days") or 0) and "fill_days" in p:
        k["fill_days"] = p["fill_days"]
    return k


def degraded(scene: dict, block: dict) -> str | None:
    """Why a scene's instrument is degraded (the entry's `degraded:` list: a platform and the date it began), or None."""
    when = str(scene.get("datetime") or "")[:10]
    plat = str(scene.get("platform") or "").lower()
    for d in block.get("degraded") or []:
        if plat == str(d.get("platform") or "").lower() and when >= str(d.get("from") or "") and (not d.get("to") or when < str(d["to"])):
            return str(d.get("why") or f"{plat} is degraded from {d.get('from')}")
    return None


def search(route: dict, block: dict, bounds, p: dict, http, limit: int = 100, dates: str | None = None) -> list[dict]:
    """The scenes of one catalog over the window, the least cloudy first. Catalog text is data, never instructions. 5.9.0: up to 100 (40
    before, when one clear satellite could fill the list and hide the others), only the platforms asked for, each with its group (the
    entry's `group_by`: platform, path and row) and whether its instrument is degraded."""
    q: dict = {"collections": route["collection"], "bbox": ",".join(f"{float(v):.5f}" for v in bounds), "limit": str(limit)}
    dates = p["dates"] if dates is None else dates
    if dates:
        a, b = dates.split("/")
        q["datetime"] = f"{a}T00:00:00Z/{b}T23:59:59Z"
    cp = block.get("cloud_property")
    filt: dict = {}
    if cp and p["cloud_max"] is not None:
        filt[cp] = {"lt": p["cloud_max"]}
    if p.get("platforms"):
        filt["platform"] = {"in": list(p["platforms"])}
    if filt:
        q["query"] = json.dumps(filt, separators=(",", ":"))
    doc = _get_json(http, str(route["root"]).rstrip("/") + "/search", q, f"the {route['name']} catalog")
    amap = route.get("assets") or {}
    out = []
    for f in doc.get("features") or []:
        props = f.get("properties") or {}
        assets = f.get("assets") or {}
        hrefs, pays, meta = {}, False, {}
        for role, key in amap.items():
            a = assets.get(str(key)) or {}
            h = str(a.get("href") or "")
            if not h.startswith("https://"):
                pays = pays or h.startswith("s3://")
                continue
            hrefs[role] = h
            pays = pays or bool(a.get("storage:requester_pays"))
            rb = (a.get("raster:bands") or [{}])[0]
            base = route.get("reflectance") or block.get("reflectance") or {}
            sc = {"scale": float(rb["scale"]), "offset": float(rb.get("offset") or 0.0)} if rb.get("scale") is not None else (
                {"scale": float(base["scale"]), "offset": float(base.get("offset") or 0.0)} if base.get("scale") is not None else None)
            if sc and role != "mask":
                # a publisher that has already added its offset to the cells says so on the item; an offset that starts at a processing version
                # applies from that version on (Sentinel-2's from baseline 04.00)
                if base.get("offset_applied_property") and props.get(base["offset_applied_property"]) is True:
                    sc["offset"] = 0.0
                if base.get("offset_from") and str(props.get(base["offset_from"]["property"]) or "") < str(base["offset_from"]["value"]):
                    sc["offset"] = 0.0
                meta[role] = sc
        pays = pays or bool(props.get("storage:requester_pays"))
        when = str(props.get("datetime") or props.get("start_datetime") or "")[:19]
        plat = str(oh.untrusted(props.get("platform") or props.get("constellation"), 30) or "").lower()
        if (p.get("platforms") and plat not in p["platforms"]) or plat in (p.get("exclude_platforms") or []):
            continue
        sc_ = {"id": oh.untrusted(f.get("id"), 80), "datetime": when, "cloud": props.get(cp) if cp else None, "bbox": f.get("bbox"),
               "geometry": f.get("geometry"), "platform": plat or None, "gsd": props.get("gsd"), "assets": hrefs, "scale": meta, "requester_pays": pays,
               "group": " ".join(str(oh.untrusted(props.get(g) if g != "platform" else plat, 30) or "-") for g in (block.get("group_by") or ["platform"]))}
        sc_["degraded"] = degraded(sc_, block)
        out.append(sc_)
    key = (lambda s_: (float(s_["cloud"]) if s_["cloud"] is not None else 0.0, s_["datetime"])) if cp else (lambda s_: tuple(-ord(c) for c in s_["datetime"]))
    return sorted(out, key=key)


def choose(scenes: list[dict], bounds, need: tuple, p: dict, block: dict) -> list[dict]:
    """The scenes to read: the clearest whose footprints together cover the window (least-cloudy), or the N clearest (median)."""
    from shapely.geometry import box, shape
    from shapely.ops import unary_union
    win = box(*bounds)
    cap = int(block.get("max_scenes") or MAX_SCENES)
    ok = [s for s in scenes if all(r in s["assets"] or _in_multi(block, r, s) for r in need)]
    # 5.9.0: the scenes in tiers, each in the catalog's order (the least cloudy first): clean scenes in the dates, clean scenes from the days
    # around them that fill a gap (another path's pass), then a degraded instrument (Landsat 7 after May 2003), which only fills what is left
    deg = p.get("avoid_degraded", True)
    ok = sorted(ok, key=lambda s_: (bool(deg and s_.get("degraded")), bool(s_.get("fill"))))       # sorted() keeps the order inside a tier
    if p["pick"] == "median":
        return ok[: max(2, p["scenes"] or 3)][:cap]
    feet = []
    for s in ok:
        try:
            g = shape(s["geometry"]) if s.get("geometry") else box(*s["bbox"])
        except Exception:  # noqa: BLE001 - a footprint that cannot be read counts as its box
            g = box(*s["bbox"])
        feet.append(g.intersection(win))
    # the clearest first: each scene that shows a part of the window no clearer scene shows (at least a fiftieth of it)
    keep, cover = [], None
    for i, f in enumerate(feet):
        if f.is_empty or (cover is not None and f.difference(cover).area < 0.02 * win.area):
            continue
        keep.append(i)
        cover = f if cover is None else unary_union([cover, f])
        if cover.area >= 0.995 * win.area:
            break
    if len(keep) > cap:
        # more scenes than one read may take: keep the ones that together show the most of the window
        got, cover, left = [], None, list(keep)
        while left and len(got) < cap:
            gain = {i: (feet[i].area if cover is None else feet[i].difference(cover).area) for i in left}
            best = max(left, key=lambda i: (round(gain[i] / win.area * 50), -i))
            got.append(best)
            cover = feet[best] if cover is None else unary_union([cover, feet[best]])
            left.remove(best)
        keep = sorted(got)
    # 5.9.0: a degraded scene leaves stripes; the next degraded scenes of the same group (other dates, the stripes elsewhere) fill them
    for i in [i for i in keep if deg and ok[i].get("degraded")]:
        more = [j for j, s_ in enumerate(ok) if j not in keep and s_.get("degraded") and s_.get("group") == ok[i].get("group")][:2]
        keep += more[: max(0, cap - len(keep))]
    keep = sorted(set(keep))
    return [ok[i] for i in keep]                               # painted clearest first: a clearer scene is never hidden under a cloudier one


def _in_multi(block: dict, role: str, scene: dict) -> bool:
    return any(role in bands and asset in scene["assets"] for asset, bands in (block.get("bands") or {}).items())


def sign(route: dict, href: str, http) -> str:
    """The address with the catalog's keyless read token added (Planetary Computer). The token is public and short-lived; it is kept out of records."""
    kind = route.get("sign")
    if not kind:
        return href
    tpl = SIGNERS.get(str(kind))
    if not tpl:
        raise ImageryError(f"this build does not know how '{kind}' signs its addresses", gc.EXIT_USAGE)
    key = (kind, route["collection"])
    tok = _TOKENS.get(key)
    now = dt.datetime.now(dt.timezone.utc)
    if not tok or tok[1] <= now + dt.timedelta(minutes=3):
        d = _get_json(http, tpl.format(collection=route["collection"]), None, "the catalog's token service")
        try:
            exp = dt.datetime.fromisoformat(str(d.get("msft:expiry")).replace("Z", "+00:00"))
        except ValueError:
            exp = now + dt.timedelta(minutes=30)
        tok = (str(d.get("token") or ""), exp)
        if not tok[0]:
            raise ImageryError("the catalog's token service sent no token", gc.EXIT_SERVICE)
        _TOKENS[key] = tok
    return href + ("&" if "?" in href else "?") + tok[0]


def grid_for(bounds, native_m: float, max_px: int) -> tuple:
    """(crs, transform, width, height, cell_m): one grid in the window's UTM zone, at the imagery's own grain or coarser to hold max_px."""
    from pyproj import Transformer
    from rasterio.transform import from_origin
    x0, y0, x1, y1 = (float(v) for v in bounds)
    lon, lat = (x0 + x1) / 2, (y0 + y1) / 2
    zone = int((lon + 180) // 6) % 60 + 1
    crs = f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = zip(*[t.transform(x, y) for x in (x0, lon, x1) for y in (y0, lat, y1)])
    w_m, h_m = max(xs) - min(xs), max(ys) - min(ys)
    cell = max(float(native_m), max(w_m, h_m) / float(max_px))
    width, height = max(1, math.ceil(w_m / cell)), max(1, math.ceil(h_m / cell))
    return crs, from_origin(min(xs), max(ys), cell, cell), width, height, cell


def read_onto(url: str, label: str, grid: tuple, http, bands=(1,), resampling: str = "bilinear") -> tuple:
    """Bands of a remote Cloud-Optimized GeoTIFF warped onto the grid, read by byte range. -> (array bands x rows x cols, bytes, requests)."""
    size = rf.remote_size(url, http)
    if size is None:
        raise ImageryError(f"{label} did not answer a byte-range request", gc.EXIT_SERVICE)
    last = None
    for _try in (1, 2):                                      # rule 8: one retry for a read that fails part way
        f = rf.RangedFile(url, size, http)
        try:
            return _read_once(f, label, grid, bands, resampling)
        except rf.RasterFetchError as ex:
            last = ImageryError(str(ex).split("?")[0], gc.EXIT_SERVICE)
        except Exception as ex:  # noqa: BLE001 - GDAL's message through the opener; the address is left out of it
            last = ImageryError(f"{label} could not be read by byte range ({type(ex).__name__})", gc.EXIT_SERVICE)
    raise last


def _read_once(f, label: str, grid: tuple, bands, resampling: str) -> tuple:
    import numpy as np
    rasterio = rio._rasterio()
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    crs, transform, width, height, _cell = grid
    with rasterio.open(label, opener=f.opener(label)) as src:
        nd = src.nodata
        unit = 111_320.0 if src.crs is not None and src.crs.is_geographic else 1.0
        ratio = _cell / max(abs(src.res[0]) * unit, 1e-9)            # how many of the file's cells one grid cell spans
        ovs = [o for o in src.overviews(1) if o <= ratio * 1.05]
        level = (len(ovs) - 1) if ovs else None                        # the coarsest overview still as fine as the grid: the warp reads it, not the full file
    kw = {} if level is None else {"overview_level": level}
    with rasterio.open(label, opener=f.opener(label), **kw) as src:
        with WarpedVRT(src, crs=crs, transform=transform, width=width, height=height, resampling=getattr(Resampling, resampling)) as vrt:
            a = vrt.read(list(bands))
    return np.asarray(a), nd, f.fetched, f.requests


def plan(entry_id: str, bounds, src: dict | None = None, http=None) -> dict:
    """What a read would take, before any pixel is read: the route, the scenes with their dates and cloud cover, the bands and a cost line."""
    e = rf.library_entry(entry_id)
    block = stac_block(e)
    if block is None:
        raise ImageryError(f"'{entry_id}' is not an imagery entry (it has no delivery.stac block)", gc.EXIT_USAGE)
    bounds = tuple(float(v) for v in bounds)
    if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) > float(block.get("max_span_deg") or MAX_SPAN_DEG):
        raise ImageryError(f"the window is wider than {block.get('max_span_deg') or MAX_SPAN_DEG:g} degrees; imagery is read for a city or a region, not a continent", gc.EXIT_REFUSED)
    p = params_of(src, block)
    h = _http(http)
    need = PRODUCTS[p["product"]]
    tried, refused = [], []
    for route in block["routes"]:
        if p.get("route") and p["route"] != route["name"]:
            continue
        try:
            scenes = search(route, block, bounds, p, h)
        except ImageryError as ex:
            tried.append(f"{route['name']}: {ex}")
            continue
        if scenes and all(s["requester_pays"] for s in scenes):
            refused.append(route["name"])                      # D5: no cloud keys; the next route is the free one
            continue
        free = [s for s in scenes if not s["requester_pays"]]
        chosen = choose(free, bounds, need, p, block)
        filled = []
        if chosen and p.get("fill_days") and p["pick"] != "median" and (_cover(chosen, bounds, clean=p.get("avoid_degraded", True)) < 0.995):
            wide = _widen(p["dates"], p["fill_days"])
            try:
                extra = search(route, block, bounds, p, h, dates=wide)
            except ImageryError:
                extra = []
            seen_ids = {s["id"] for s in scenes}
            extra = [{**s, "fill": True} for s in extra if s["id"] not in seen_ids and not s["requester_pays"]]
            if extra:
                chosen = choose(free + extra, bounds, need, p, block)
                filled = [s["id"] for s in chosen if s.get("fill")]
                scenes = scenes + extra
        if not chosen:
            tried.append(f"{route['name']}: {len(scenes)} scene(s), none with the bands for {p['product']} over the window")
            continue
        native = float(block.get("native_m") or chosen[0].get("gsd") or 10.0)
        grid = grid_for(bounds, native, int(block.get("max_px") or MAX_PX))
        multi = block.get("bands") or {}
        files = sorted({a for a, bs in multi.items() if any(r in bs for r in need)}) or list(need)
        if p["pick"] == "median" and (block.get("mask") or {}).get("asset_role", "mask") in (route.get("assets") or {}):
            files = files + ["mask"]
        est = len(chosen) * len(files) * grid[2] * grid[3] * 2 * 1.4 / 1e6
        return {"entry": entry_id, "route": route["name"], "collection": route["collection"], "product": p["product"], "pick": p["pick"], "dates": p["dates"] or None,
                "cloud_max": p["cloud_max"], "scenes": [_brief(s) for s in chosen], "found": len(scenes),
                "candidates": candidates(scenes, block), "filled_from_around": filled,
                "degraded_used": [s["id"] for s in chosen if s.get("degraded")],
                "degraded_left_out": sum(1 for s in scenes if s.get("degraded")) - sum(1 for s in chosen if s.get("degraded")),
                "platforms": p.get("platforms") or None, "exclude_platforms": p.get("exclude_platforms") or None,
                "files_per_scene": files, "grid": {"crs": grid[0], "pixels": [grid[2], grid[3]], "cell_m": round(grid[4], 2), "native_m": native},
                "estimate_mb": round(est, 1), "refused_routes": refused, "tried": tried, "_scenes": chosen, "_route": route, "_grid": grid, "_params": p,
                "_reserve": [s for s in sorted(free + [x for x in scenes if x.get("fill") and not x["requester_pays"]], key=lambda x: x.get("cloud") or 0)
                             if s.get("degraded") and s["id"] not in {c["id"] for c in chosen}
                             and s.get("group") in {c.get("group") for c in chosen if c.get("degraded")}],
                "cost": f"{len(chosen)} scene(s) x {len(files)} file(s), {grid[2]} x {grid[3]} cells of {grid[4]:.0f} m: about {est:,.0f} MB to read"}
    if refused and not tried:
        raise ImageryError(f"the {', '.join(refused)} catalog keeps these files in a requester-pays bucket, which is not read (no cloud keys are held); "
                           "no free route is listed for this entry", gc.EXIT_REFUSED, "MAP-SVC-046")
    raise ImageryError("no scene was found for this window and these dates" + (f" (cloud cover under {p['cloud_max']:g} percent)" if p["cloud_max"] is not None else "")
                       + ": " + "; ".join(tried + [f"{r}: requester-pays, not read" for r in refused]) + ". Widen the dates or raise source.cloud_max", gc.EXIT_REFUSED)


def _brief(s: dict) -> dict:
    b = {k: s.get(k) for k in ("id", "datetime", "cloud", "platform", "group")}
    if s.get("degraded"):
        b["degraded"] = s["degraded"]
    if s.get("fill"):
        b["fill"] = True
    return b


def candidates(scenes: list[dict], block: dict) -> list[dict]:
    """5.9.0: what the catalog holds over the window, by group (platform, path and row for Landsat): how many scenes, the clearest and
    whether the group is degraded; so a plan shows the clean satellite a cloudless degraded one would otherwise hide."""
    by: dict = {}
    for s in scenes:
        by.setdefault(s.get("group") or "-", []).append(s)
    out = []
    for g, ss in by.items():
        best = ss[0]                                   # the catalog's order: the least cloudy first
        out.append({"group": g, "scenes": len(ss), "clearest": {k: best.get(k) for k in ("id", "datetime", "cloud")},
                    "dates": [min(x["datetime"][:10] for x in ss), max(x["datetime"][:10] for x in ss)],
                    **({"degraded": best["degraded"]} if best.get("degraded") else {}),
                    **({"from_around": True} if all(x.get("fill") for x in ss) else {})})
    return sorted(out, key=lambda c: (bool(c.get("degraded")), c["group"]))


def _cover(scenes: list[dict], bounds, clean: bool = True) -> float:
    """The share of the window the scenes' footprints cover (with `clean`, the scenes of a degraded instrument count for nothing)."""
    from shapely.geometry import box, shape
    from shapely.ops import unary_union
    win = box(*bounds)
    feet = []
    for s in scenes:
        if clean and s.get("degraded"):
            continue
        try:
            g = shape(s["geometry"]) if s.get("geometry") else box(*s["bbox"])
        except Exception:  # noqa: BLE001
            g = box(*s["bbox"])
        feet.append(g.intersection(win))
    return float(unary_union(feet).area / win.area) if feet and win.area else 0.0


def _widen(dates: str, days: int) -> str:
    a, b = (dt.date.fromisoformat(x) for x in dates.split("/"))
    return f"{(a - dt.timedelta(days=days)).isoformat()}/{(b + dt.timedelta(days=days)).isoformat()}"


def _bad(mask_arr, spec: dict):
    import numpy as np
    m = np.asarray(mask_arr)
    if (spec or {}).get("kind") == "bits":
        bad = np.zeros(m.shape, bool)
        for b in spec.get("bad") or []:
            bad |= (m.astype("uint32") >> int(b)) & 1 == 1
        return bad
    return np.isin(m, list((spec or {}).get("bad") or []))


def fetch(entry_id: str, bounds, src: dict | None = None, cache_dir=None, http=None) -> dict:
    """The picture for the window as a GeoTIFF: three bands (uint8, with a mask) for a colour picture, one float band for NDVI. -> {path, record, findings, entry}."""
    import numpy as np
    rasterio = rio._rasterio()
    e = rf.library_entry(entry_id)
    if e.get("status") in ("blocked", "retired", "draft"):
        raise ImageryError(f"library entry '{entry_id}' is {e.get('status')}", gc.EXIT_REFUSED)
    block = stac_block(e) or {}
    bounds = tuple(float(v) for v in bounds)
    root = rf.cache_root(cache_dir) / entry_id
    p0 = params_of(src, block) if block else {}
    key = hashlib.sha256(json.dumps([[round(v, 4) for v in bounds], cache_params(p0, block) if block else p0], sort_keys=True).encode()).hexdigest()[:12]
    out = root / f"{p0.get('product', 'x')}_{key}.tif"
    rec_p = out.with_suffix(".json")
    lic = e.get("license") or {}
    if out.is_file() and rec_p.is_file():
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        # 5.9.0: a picture kept from before the scene tiers that holds a degraded scene is read again (a clean scene may now cover it)
        stale = "tiers" not in rec and p0.get("avoid_degraded", True) and any(degraded(sc, block) for sc in rec.get("scenes") or [])
        if rec.get("sha256") == rf._sha(out) and not stale:
            return {"path": str(out), "record": {**rec, "cached": True, "request_count": 0, "request_bytes": 0,
                                                 "files": [{**f, "cached": True} for f in rec.get("files", [])]}, "findings": [], "entry": e}
    if rf.offline():
        raise ImageryError("offline (GMAP_OFFLINE=1): this picture is not in the cache and nothing is fetched")
    h = _http(http)
    pl = plan(entry_id, bounds, src, h)
    route, grid, p, scenes = pl["_route"], pl["_grid"], pl["_params"], pl["_scenes"]
    crs, transform, width, height, cell = grid
    need = PRODUCTS[p["product"]]
    multi = block.get("bands") or {}
    mspec = block.get("mask") or {}
    stacks, valid, used = [], [], []
    tally = {"b": 0, "r": 0}

    def read_scene(s):
        data = {}
        for asset, names in multi.items():                     # one file that holds several bands (aerial photography)
            want = [r for r in need if r in names]
            if want and asset in s["assets"]:
                a, nd, b, r = read_onto(sign(route, s["assets"][asset], h), f"{s['id']}_{asset}.tif", grid, h, bands=[names.index(x) + 1 for x in want])
                tally["b"], tally["r"] = tally["b"] + b, tally["r"] + r
                for i, x in enumerate(want):
                    data[x] = (a[i].astype("float32"), nd, None)
        for role in need:
            if role in data:
                continue
            a, nd, b, r = read_onto(sign(route, s["assets"][role], h), f"{s['id']}_{role}.tif", grid, h)
            tally["b"], tally["r"] = tally["b"] + b, tally["r"] + r
            data[role] = (a[0].astype("float32"), nd, s["scale"].get(role))
        ok = np.ones((height, width), bool)
        for role in need:
            a, nd, sc = data[role]
            ok &= (a != (nd if nd is not None else 0)) if (nd is not None or sc) else np.ones_like(ok)
        ok &= np.any(np.stack([data[r][0] for r in need]) != 0, axis=0)
        if p["pick"] == "median" and "mask" in s["assets"] and mspec:
            m, _nd, b, r = read_onto(sign(route, s["assets"]["mask"], h), f"{s['id']}_mask.tif", grid, h, resampling="nearest")
            tally["b"], tally["r"] = tally["b"] + b, tally["r"] + r
            ok &= ~_bad(m[0], mspec)
        vals = []
        for role in need:
            a, _nd, sc = data[role]
            vals.append(a * float(sc["scale"]) + float(sc.get("offset") or 0.0) if sc else a / 255.0)
        stacks.append(np.stack(vals))
        valid.append(ok)
        used.append(s)

    for s in scenes:
        try:
            read_scene(s)
        except ImageryError as ex:
            if len(scenes) > 1:
                continue                                        # one scene that cannot be read is left out; the record names those used
            raise ex
        if tally["b"] > 3 * rf.MAX_BYTES:
            break
    # 5.9.0: a degraded scene's stripes left empty: the next passes of a degraded group (other dates) are read until the window is full
    cap = int(block.get("max_scenes") or MAX_SCENES)
    if stacks and p["pick"] != "median" and any(s_.get("degraded") for s_ in used):
        for s in pl.get("_reserve") or []:
            if len(used) >= cap or tally["b"] > 3 * rf.MAX_BYTES or float(np.logical_or.reduce(valid).mean()) >= 0.98:
                break
            try:
                read_scene(s)
            except ImageryError:
                continue
    total_b, total_r = tally["b"], tally["r"]
    if not stacks:
        raise ImageryError("no scene could be read for this window", gc.EXIT_SERVICE)
    cube = np.stack(stacks)                                      # scenes x bands x rows x cols, in reflectance (0 to 1)
    vmask = np.stack(valid)
    if p["pick"] == "median" and len(stacks) > 1:
        c = np.where(vmask[:, None, :, :], cube, np.nan)
        with np.errstate(all="ignore"):
            comp = np.nanmedian(c, axis=0)
        okc = vmask.any(axis=0)
        comp = np.where(okc[None], comp, 0.0)
    else:
        comp = np.zeros(cube.shape[1:], "float32")
        okc = np.zeros((height, width), bool)
        for i in range(len(stacks)):                             # the clearest scene first; each later one fills only what is still empty
            take = vmask[i] & ~okc
            comp[:, take] = cube[i][:, take]
            okc |= take
    out.parent.mkdir(parents=True, exist_ok=True)
    prof = {"driver": "GTiff", "height": height, "width": width, "crs": crs, "transform": transform, "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256}
    stats: dict = {}
    if p["product"] in INDEX:
        a_, b_ = comp[0], comp[1]
        with np.errstate(all="ignore"):
            nd = (a_ - b_) / (a_ + b_)
        nd = np.where(okc & np.isfinite(nd), np.clip(nd, -1, 1), -9999.0).astype("float32")
        with rasterio.open(out, "w", count=1, dtype="float32", nodata=-9999.0, **prof) as dst:
            dst.write(nd, 1)
            dst.set_band_description(1, INDEX[p["product"]])
        good = nd[nd > -9000]
        stats = {f"{p['product']}_median": round(float(np.median(good)), 3) if good.size else None}
        if p["product"] in ("ndwi", "mndwi") and good.size:
            cell_km2 = (cell / 1000.0) ** 2
            stats["water_km2"] = round(float((good > 0).sum()) * cell_km2, 2)      # cells above 0, in the grid's UTM metres
            stats["water_note"] = f"{INDEX[p['product']]} above 0 counted at {cell:.0f} m cells; the water op draws and measures it per area"
    else:
        lo, hi = (0.0, 1.0) if multi else [float(v) for v in (p.get("stretch") or block.get("stretch") or [0.0, 0.3])]
        g = float(block.get("gamma") or (1.0 if multi else 1.6))
        x = np.clip((comp - lo) / max(hi - lo, 1e-6), 0, 1) ** (1.0 / g)
        rgb = np.where(okc[None], np.clip(np.round(x * 254) + 1, 1, 255), 0).astype("uint8")       # 0 is kept for "no picture here"
        with rasterio.open(out, "w", count=3, dtype="uint8", nodata=0, photometric="RGB", **prof) as dst:
            dst.write(rgb)
            for i, name in enumerate(need, 1):
                dst.set_band_description(i, name)
        stats = {"stretch": [lo, hi], "gamma": g}
    covered = float(okc.mean())
    dates = sorted(s["datetime"][:10] for s in used)
    rec = {"entry": entry_id, "route": route["name"], "collection": route["collection"], "product": p["product"], "pick": p["pick"], "dates_asked": p["dates"] or None,
           "cloud_max": p["cloud_max"], "scenes": [_brief(s) for s in used],
           "scene_dates": [dates[0], dates[-1]] if dates else None, "bands": list(need), "grid": {"crs": crs, "pixels": [width, height], "cell_m": round(cell, 2)},
           "covered": round(covered, 4), "bounds_wgs84": list(bounds), "request_count": total_r, "request_bytes": total_b, "cached": False,
           "retrieved_at": rf.today(), "sha256": rf._sha(out), "path": str(out), "attribution": lic.get("attribution_text"), "license": lic.get("class"),
           "refused_routes": pl.get("refused_routes") or [], "files": [{"scene": s["id"], "cached": False} for s in used], **stats,
           "tiers": True, "degraded_used": [s["id"] for s in used if s.get("degraded")], "filled_from_around": [s["id"] for s in used if s.get("fill")]}
    rec_p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    findings = []
    if pl.get("refused_routes"):
        findings.append({"id": "MAP-SVC-046", "severity": "INFO", "detail": pl["refused_routes"],
                         "message": f"the {', '.join(pl['refused_routes'])} catalog keeps these files in a requester-pays bucket, which is not read; "
                                    f"the picture came from {route['name']}, which is free to read"})
    deg_used = [s for s in used if s.get("degraded")]
    if deg_used:
        clean_n = len(used) - len(deg_used)
        findings.append({"id": "MAP-TER-005", "severity": "WARN" if not clean_n else "INFO", "detail": [s["id"] for s in deg_used],
                         "message": f"{len(deg_used)} of the {len(used)} scenes are from a degraded instrument ({deg_used[0]['degraded']}); "
                                    + ("they fill only what the clean scenes leave" if clean_n else
                                       "no clean scene covers these dates, so the picture may show its stripes; widen source.dates or take pick: median")})
    if covered < 0.98:
        findings.append({"id": "MAP-TER-005", "severity": "WARN", "detail": {"covered": round(covered, 3)},
                         "message": f"the picture covers {covered:.0%} of the window: the scenes chosen ({len(used)}) leave the rest empty; widen source.dates or raise source.cloud_max"})
    if cell > 1.5 * float(pl["grid"]["native_m"]):
        findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": {"cell_m": round(cell, 1)},
                         "message": f"the window is wide for this imagery, so it was read at {cell:.0f} m a cell (its own grain is {pl['grid']['native_m']:g} m)"})
    return {"path": str(out), "record": rec, "findings": findings, "entry": e}


# ---------------------------------------------------------------------------------------------------------------- a series (5.9.0)
def years_of(spec: str, skip=()) -> list[int]:
    """'1984-2025', '1984,1990,2000' or '1984-1990,2000' -> the years in order, less `skip`."""
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", part)
        if m:
            a, b = int(m[1]), int(m[2])
            if b < a or b - a > 200:
                raise ImageryError(f"years '{part}' run backwards or over two centuries", gc.EXIT_USAGE)
            out.update(range(a, b + 1))
        elif re.fullmatch(r"\d{4}", part):
            out.add(int(part))
        elif part:
            raise ImageryError(f"years are 1984-2025 or 1984,1990,2000, not '{part}'", gc.EXIT_USAGE)
    skip_ = {int(x) for x in (skip.split(",") if isinstance(skip, str) else skip or []) if str(x).strip()}
    ys = sorted(out - skip_)
    if not ys:
        raise ImageryError("no years are left to read", gc.EXIT_USAGE)
    return ys


def season_dates(year: int, season: str) -> str:
    """The dates of one year's season: '06-01/08-31' -> '1990-06-01/1990-08-31'; a season that crosses the new year ends in the next."""
    m = re.fullmatch(r"(\d{2})-(\d{2})/(\d{2})-(\d{2})", str(season).strip())
    if not m:
        raise ImageryError(f"a season is MM-DD/MM-DD (06-01/08-31), not '{season}'", gc.EXIT_USAGE)
    a = dt.date(year, int(m[1]), int(m[2]))
    b = dt.date(year, int(m[3]), int(m[4]))
    if b < a:
        b = dt.date(year + 1, b.month, b.day)
    return f"{a.isoformat()}/{b.isoformat()}"


def series_dates(years=None, season: str | None = None, dates: dict | None = None, skip=()) -> dict:
    """{period label: START/END}: one year's season for each year, or the dates given per period (a file of label: dates)."""
    if dates:
        out = {}
        for k, v in dates.items():
            if str(k) in {str(x) for x in (skip.split(",") if isinstance(skip, str) else skip or [])}:
                continue
            d = str(v).strip()
            if re.fullmatch(r"\d{4}", d):
                d = f"{d}-01-01/{d}-12-31"
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}", d):
                raise ImageryError(f"the dates of '{k}' are START/END (1990-06-01/1990-08-31), not '{v}'", gc.EXIT_USAGE)
            out[str(k)] = d
        if not out:
            raise ImageryError("no periods are left to read", gc.EXIT_USAGE)
        return dict(sorted(out.items()))
    if not years or not season:
        raise ImageryError("a series is --years with --season (1984-2025 and 06-01/08-31), or --dates-file with each period's dates", gc.EXIT_USAGE)
    return {str(y): season_dates(y, season) for y in years_of(years, skip)}


def series(entry_id: str, bounds, periods: dict, src: dict | None = None, *, jobs: int = 3, plan_only: bool = False, cache_dir=None, http=None,
           out=None, progress=None) -> dict:
    """5.9.0: one picture per period (each period's own dates), read with `jobs` reads at once. Each period is filed in the cache as soon as
    it is read, under the key a map layer with these bounds and dates reads, so a series cut off part way picks up where it stopped and the
    builds that follow read every picture from the cache. `plan_only` asks the catalog only. `out` (a path) is written after every period.
    -> {entry, bounds, periods: [{period, dates, path, covered, scenes, cached, error}], summary}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    bounds = tuple(float(v) for v in bounds)
    src = {k: v for k, v in (src or {}).items() if k not in ("type", "id", "dates")}
    say = progress or (lambda _m: None)
    rows: dict[str, dict] = {}
    doc = {"entry": entry_id, "bounds": list(bounds), "params": src, "plan_only": plan_only, "periods": []}

    def one(label: str, dates: str) -> dict:
        s_ = {**src, "dates": dates}
        if plan_only:
            pl = plan(entry_id, bounds, s_, http)
            return {"period": label, "dates": dates, "scenes": pl["scenes"], "cost": pl["cost"], "estimate_mb": pl["estimate_mb"],
                    "candidates": len(pl["candidates"]), "degraded_used": pl["degraded_used"], "filled_from_around": pl["filled_from_around"]}
        r = fetch(entry_id, bounds, s_, cache_dir=cache_dir, http=http)
        rec = r["record"]
        return {"period": label, "dates": dates, "path": r["path"], "covered": rec.get("covered"), "cached": bool(rec.get("cached")),
                "scenes": rec.get("scenes"), "request_bytes": rec.get("request_bytes"), "degraded_used": rec.get("degraded_used") or [],
                "findings": [f["message"] for f in r["findings"] if f["severity"] in ("WARN", "BLOCK")]}

    def write():
        if out:
            doc["periods"] = [rows[k] for k in periods if k in rows]
            o = Path(out)
            o.parent.mkdir(parents=True, exist_ok=True)
            o.with_suffix(".part").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
            o.with_suffix(".part").replace(o)

    n = max(1, min(int(jobs or 1), 8, len(periods)))
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(one, k, d): k for k, d in periods.items()}
        for fu in as_completed(futs):
            k = futs[fu]
            try:
                rows[k] = fu.result()
            except rio.RasterError as e:
                rows[k] = {"period": k, "dates": periods[k], "error": str(e)}
            r_ = rows[k]
            say(f"{k}: " + (f"error: {r_['error']}" if r_.get("error") else (r_.get("cost") if plan_only else
                f"{'kept' if r_.get('cached') else 'read'}, {len(r_.get('scenes') or [])} scene(s), {float(r_.get('covered') or 0):.0%} covered")))
            write()
    doc["periods"] = [rows[k] for k in periods]
    bad = [r_["period"] for r_ in doc["periods"] if r_.get("error")]
    if plan_only:
        mb = sum(float(r_.get("estimate_mb") or 0) for r_ in doc["periods"])
        doc["summary"] = f"{len(periods)} period(s) planned, about {mb:,.0f} MB to read" + (f"; {len(bad)} with no scene: {', '.join(bad)}" if bad else "")
    else:
        kept = sum(1 for r_ in doc["periods"] if r_.get("cached"))
        low = [r_["period"] for r_ in doc["periods"] if not r_.get("error") and float(r_.get("covered") or 0) < 0.98]
        mb = sum(int(r_.get("request_bytes") or 0) for r_ in doc["periods"]) / 1e6
        doc["summary"] = (f"{len(periods) - len(bad)} of {len(periods)} period(s) ready ({kept} kept from before, {mb:,.0f} MB read)"
                          + (f"; under 98% covered: {', '.join(low)}" if low else "") + (f"; failed: {', '.join(bad)} (run again to retry them)" if bad else ""))
    doc["failed"] = bad
    write()
    return doc


def layer_of_spec(spec_path, layer: str | None = None, template: str | None = None, values: dict | None = None) -> dict:
    """5.9.0: the library imagery layer of a MapSpec and the window its build reads (map_raster.probe_window: planned, nothing fetched).
    `values` fills the placeholders of a MapSpec written for an atlas by time ({period}) for the plan."""
    import map_raster as mr
    try:
        got = mr.probe_window(spec_path, layer, template=template, values=values)
    except mr.RasterLayerError as ex:
        raise ImageryError(str(ex), gc.EXIT_USAGE) from None
    if stac_block(got["entry"]) is None:
        raise ImageryError(f"layer '{got['layer']}' reads {got['entry']}, which is not an imagery entry; name the imagery layer with --layer", gc.EXIT_USAGE)
    return got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Satellite or aerial imagery for a window, from a library entry's STAC catalog")
    ap.add_argument("cmd", choices=("plan", "fetch", "series"))
    ap.add_argument("entry", nargs="?", help="the library entry (series --spec: taken from the layer)")
    ap.add_argument("--bounds", help="W,S,E,N (write --bounds=-112,33,-111,34)")
    ap.add_argument("--spec", help="5.9.0, series: a MapSpec; the window its build reads, and its imagery layer's own source settings")
    ap.add_argument("--layer", help="series --spec: the imagery layer (default the first library raster)")
    ap.add_argument("--template", help="series --spec: the template(s) the builds are drawn at, comma separated (the pages' and the animation's: "
                    "16x9-slide,square-social); each has its own window")
    ap.add_argument("--years", help="series: 1984-2025 or 1984,1990,2000")
    ap.add_argument("--season", help="series: each year's dates as MM-DD/MM-DD (06-01/08-31)")
    ap.add_argument("--skip", default="", help="series: periods left out (2012)")
    ap.add_argument("--dates-file", help="series: a JSON or YAML file of period: START/END, for dates that differ by year")
    ap.add_argument("--jobs", type=int, default=3, help="series: reads at once (default 3, at most 8)")
    ap.add_argument("--plan-only", action="store_true", help="series: ask the catalog only; nothing is read")
    ap.add_argument("--out", help="series: the series file, written after every period (default stdout only)")
    ap.add_argument("--dates", help="START/END (2024-06-01/2024-09-30) or one year")
    ap.add_argument("--cloud", type=float, help="the most cloud cover a scene may have, in percent (default 20)")
    ap.add_argument("--product", choices=list(PRODUCTS))
    ap.add_argument("--pick", choices=list(PICKS))
    ap.add_argument("--scenes", type=int, help="median: how many scenes (default 3)")
    ap.add_argument("--route", help="one of the entry's catalogs by name")
    ap.add_argument("--platforms", help="5.9.0: only these satellites, as the catalog names them (landsat-5,landsat-8)")
    ap.add_argument("--exclude-platforms", help="5.9.0: never these satellites (landsat-7)")
    ap.add_argument("--keep-degraded", action="store_true", help="5.9.0: rank a degraded instrument's scenes with the clean ones (default: last)")
    ap.add_argument("--fill-days", type=int, help="5.9.0: days before and after the dates a scene may come from to fill an edge (Landsat: 16)")
    a = ap.parse_args(argv)
    src = {k: v for k, v in {"dates": a.dates, "cloud_max": a.cloud, "product": a.product, "pick": a.pick, "scenes": a.scenes, "route": a.route,
                              "platforms": a.platforms, "exclude_platforms": a.exclude_platforms, "fill_days": a.fill_days,
                              "avoid_degraded": False if a.keep_degraded else None}.items() if v is not None}
    try:
        if a.cmd == "series":
            entry, b, base = a.entry, None, {}
            temps = [t.strip() for t in (a.template or "").split(",") if t.strip()] or [None]
            dfile = None
            if a.dates_file:
                import yaml
                dfile = yaml.safe_load(Path(a.dates_file).read_text(encoding="utf-8"))
                if not isinstance(dfile, dict):
                    raise ImageryError("--dates-file holds period: START/END lines", gc.EXIT_USAGE)
            per = series_dates(a.years, a.season, dfile, a.skip)
            first = {"period": next(iter(per)), "dates": next(iter(per.values()))}
            if a.spec:
                lay = layer_of_spec(a.spec, a.layer, temps[0], first)
                entry, b, base = lay["entry"], lay["bounds"], lay["source"]
                if a.entry and a.entry != entry:
                    raise ImageryError(f"the MapSpec's layer reads {entry}, not {a.entry}", gc.EXIT_USAGE)
            if not entry:
                raise ImageryError("series needs the library entry, or --spec", gc.EXIT_USAGE)
            if a.bounds:
                b = tuple(float(v) for v in a.bounds.split(","))
            if b is None:
                raise ImageryError("series needs --bounds or --spec", gc.EXIT_USAGE)
            r = series(entry, b, per, {**base, **src}, jobs=a.jobs, plan_only=a.plan_only, out=a.out,
                       progress=lambda m: print(m, file=sys.stderr, flush=True))
            if a.spec:
                r["spec"], r["layer"], r["template"] = str(a.spec), lay["layer"], temps[0]
                r["also"] = []
                for t in temps[1:]:                         # the same periods for another size's window (an animation's frames)
                    l2 = layer_of_spec(a.spec, a.layer, t, first)
                    r2 = series(entry, l2["bounds"], per, {**l2["source"], **src}, jobs=a.jobs, plan_only=a.plan_only,
                                progress=lambda m, t=t: print(f"[{t}] {m}", file=sys.stderr, flush=True))
                    r["also"].append({"template": t, "bounds": r2["bounds"], "summary": r2["summary"], "failed": r2["failed"]})
                    r["failed"] = r["failed"] + [f"{t}:{x}" for x in r2["failed"]]
                if a.out:
                    Path(a.out).write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
            print(json.dumps({k: v for k, v in r.items() if k != "periods"} | {"periods": len(r["periods"])}, indent=2, default=str))
            return gc.EXIT_OK if not r["failed"] else gc.EXIT_RUNTIME
        if not a.entry or not a.bounds:
            raise ImageryError("plan and fetch need the entry and --bounds", gc.EXIT_USAGE)
        b = tuple(float(v) for v in a.bounds.split(","))
        if a.cmd == "plan":
            r = {k: v for k, v in plan(a.entry, b, src).items() if not k.startswith("_")}
        else:
            r = fetch(a.entry, b, src)
            r = {"path": r["path"], "record": r["record"], "findings": r["findings"]}
    except rio.RasterError as ex:
        print(f"imagery: {ex}", file=sys.stderr)
        return getattr(ex, "code", gc.EXIT_RUNTIME)
    print(json.dumps(r, indent=2, default=str))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
