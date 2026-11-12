#!/usr/bin/env python3
"""basemap_render: a static raster basemap for a print map, from tiles (spec `basemap-render`, T1/T2).

    basemap_render.py plan   --spec map.yaml [--provider carto-positron] [--tier T1|T2] [--max-tiles N] [--zoom Z] [--detail legible|sharp]
    basemap_render.py render --spec map.yaml [--provider P] [--source mbtiles:FILE | dir:FOLDER] [--scheme xyz|tms] [--tier T1|T2] [--max-tiles N] [--zoom Z]
    basemap_render.py show   --spec map.yaml

`render` writes `<working folder>/basemaps/<map id>/composite.png` and `composite.json`; the map build only reads that pair, so a build stays offline and
repeatable. The composite is in Web Mercator, and a map that uses it is drawn in Web Mercator (MAP-CRS-002 allows that when tiles are present).

The rules, all enforced here: only providers the library marks active and print_ok; a tile count cap (25 on T1, 100 on T2, a quarter of that when the
provider's terms do not allow bulk use); keys only from the person's registered credential cred:<provider> (0.6.0: added to each tile request at send
time, never in the tile address; GEO_MAPPING_KEY_<PROVIDER> is read as a command-line fallback); a cache on disk that is read before the network; a polite pause between
requests; one request at a time; the attribution recorded with the composite. A user's own MBTiles file or a folder of tiles they staged needs no provider
terms, only the attribution they give. Google and Bing are refused with the reason.

Zoom (0.2.2). Tiles are drawn for a screen: a label is about 11 tile pixels high, which reads at 96 pixels per inch. On paper the same tile prints at
the map's width, so the zoom decides both how sharp the background is and how large its labels print. `basemap.detail` picks the rule:
  legible  (the default for a style with labels)  the highest zoom whose labels still print at 6 pt or more; the background is softer than the data
  sharp    (the default for a style without labels) the lowest zoom whose pixels reach the export dpi (capped at 200); labels print at about 4 pt
`basemap.zoom` (or --zoom) sets the zoom outright. Every plan reports the label size and the dpi the tiles reach on paper (MAP-TILE-001). The source's
own zoom range is honoured: the provider's or the MBTiles file's min and max zoom, and the zoom folders present in a tile folder (MAP-TILE-002).
A tile folder is read as XYZ (row 0 at the top, what most tools write) or TMS (row 0 at the bottom, gdal2tiles' default before 3.1); the scheme is
found by trying both on the tiles the map needs, or set with `basemap.scheme`, and recorded (MAP-TILE-003).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import tile_registry as tr  # noqa: E402

R = 6378137.0
ORIGIN = math.pi * R
CAPS = {"T1": 25, "T2": 100}
BULK_FACTOR = 4                 # a provider that does not allow bulk use gets a quarter of the cap
HARD_CAP = 400
MAX_MISSING = 0.1
MIN_PAUSE_S = 0.5
TILE_LABEL_PX = 11.0            # the height tile styles letter most labels at, in tile pixels (CARTO, OpenStreetMap: 10 to 12 px)
LEGIBLE_LABEL_PT = 6.0          # the smallest label that reads on paper
NO_LABEL_STYLE = re.compile(r"no_?labels?|nolabel|_base$|terrain_background|hillshade|imagery|satellite", re.I)


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def blocked(fs) -> bool:
    return any(f["severity"] == "BLOCK" for f in fs)


# ---------------------------------------------------------------------------------------------------------------- Web Mercator tiles
def lonlat_to_merc(lon: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    return lon * ORIGIN / 180.0, math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * R


def merc_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return x * 180.0 / ORIGIN, math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)


def resolution(z: int, tile_size: int = 256) -> float:
    return 2 * ORIGIN / (tile_size * (1 << z))


def tile_of(x: float, y: float, z: int) -> tuple[int, int]:
    n = 1 << z
    tx = int(math.floor((x + ORIGIN) / (2 * ORIGIN) * n))
    ty = int(math.floor((ORIGIN - y) / (2 * ORIGIN) * n))
    return min(max(tx, 0), n - 1), min(max(ty, 0), n - 1)


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    size = 2 * ORIGIN / n
    return (-ORIGIN + x * size, ORIGIN - (y + 1) * size, -ORIGIN + (x + 1) * size, ORIGIN - y * size)


def grid(merc_bbox, z: int) -> dict:
    """The tiles of zoom z that cover the box. Rows run from the top (north) down."""
    c0, r0 = tile_of(merc_bbox[0], merc_bbox[3], z)       # top-left
    c1, r1 = tile_of(merc_bbox[2], merc_bbox[1], z)       # bottom-right
    cols, rows = list(range(c0, c1 + 1)), list(range(r0, r1 + 1))
    tl, br = tile_bbox(z, cols[0], rows[0]), tile_bbox(z, cols[-1], rows[-1])
    return {"z": z, "cols": cols, "rows": rows, "tiles": [(z, c, r) for r in rows for c in cols], "bbox": (tl[0], br[1], br[2], tl[3])}


def pad_bbox(b, pct: float):
    w, h = b[2] - b[0], b[3] - b[1]
    p = max(w, h) * pct / 100.0
    return (max(b[0] - p, -ORIGIN), max(b[1] - p, -ORIGIN), min(b[2] + p, ORIGIN), min(b[3] + p, ORIGIN))


def paper_dpi(merc_w: float, z: int, map_w_mm: float, tile_size: int = 256) -> float:
    """Tile pixels per inch when the box is printed `map_w_mm` wide."""
    return (merc_w / resolution(z, tile_size)) / (map_w_mm / 25.4)


def label_pt(merc_w: float, z: int, map_w_mm: float, tile_size: int = 256, label_px: float = TILE_LABEL_PX) -> float:
    """How large a tile's labels print, in points, at zoom z."""
    return label_px * 72.0 / paper_dpi(merc_w, z, map_w_mm, tile_size)


def default_detail(style: str | None, labels: bool | None = None) -> str:
    """legible for a style that letters places and roads, sharp for one that does not."""
    if labels is not None:
        return "legible" if labels else "sharp"
    return "sharp" if style and NO_LABEL_STYLE.search(style) else "legible"


def choose_zoom(merc_bbox, need_px: float, cap: int, max_zoom: int, tile_size: int = 256, force: int | None = None, *, min_zoom: int = 0,
                detail: str = "sharp", map_w_mm: float | None = None, label_px: float = TILE_LABEL_PX) -> dict:
    """The zoom for a print basemap, within [min_zoom, max_zoom], then lowered until the tile count fits `cap`.
    sharp: the smallest zoom whose pixels reach `need_px` across the box. legible: the highest zoom whose labels print at LEGIBLE_LABEL_PT or more
    (never above the sharp zoom: more tiles would add nothing). `force` sets the zoom outright (clamped to the source's range)."""
    w = merc_bbox[2] - merc_bbox[0]
    min_zoom = max(0, min(int(min_zoom), int(max_zoom)))
    sharp = next((z for z in range(min_zoom, max_zoom + 1) if w / resolution(z, tile_size) >= need_px), max_zoom)
    ideal = sharp
    if detail == "legible" and map_w_mm:
        ok = [z for z in range(min_zoom, max_zoom + 1) if label_pt(w, z, map_w_mm, tile_size, label_px) >= LEGIBLE_LABEL_PT]
        ideal = min(max(ok), sharp) if ok else min_zoom
    clamped = None
    if force is not None:
        z = min(max(int(force), min_zoom), max_zoom)
        clamped = z if z != int(force) else None
    else:
        z = ideal
    g = grid(merc_bbox, z)
    limited = False
    while len(g["tiles"]) > cap and z > min_zoom and force is None:
        z -= 1
        g = grid(merc_bbox, z)
        limited = True
    g["ideal_zoom"], g["sharp_zoom"], g["limited_by_cap"], g["clamped_from"] = ideal, sharp, limited, (int(force) if clamped is not None else None)
    g["effective_px"] = round(w / resolution(z, tile_size))
    g["detail"] = "set" if force is not None else detail
    g["min_zoom"], g["max_zoom"] = min_zoom, max_zoom
    if map_w_mm:
        g["paper_dpi"] = round(paper_dpi(w, z, map_w_mm, tile_size))
        g["label_pt"] = round(label_pt(w, z, map_w_mm, tile_size, label_px), 1)
    return g


# ---------------------------------------------------------------------------------------------------------------- plan
def spec_extent(spec_path):
    """The map window in Web Mercator (the box the page shows, with the build's own padding), the map width in mm, the dpi, the map id, the audience, the spec.
    The map is planned the way the build will plan it when tiles exist: Web Mercator, bundled basemap."""
    import map_plan as mp
    import layout as lo

    spec0 = gc.load_mapspec(Path(spec_path))
    named = str((spec0.get("crs") or {}).get("display") or "auto")
    own = named not in ("auto", "EPSG:3857", "epsg:3857")          # 1.11.0: a map in a projection of its own; the composite is warped to it

    def edit(s):
        s["basemap"] = {"provider": "bundled"}
        if not own:
            s.setdefault("crs", {})["display"] = "EPSG:3857"
    plan = mp.prepare(str(spec_path), spec_edit=edit)
    win = mp.plan_visible_pad(plan)
    if own and plan.crs != "EPSG:3857":
        win = mp.box_to_crs(win, plan.crs, "EPSG:3857")
        if win is None:
            raise ValueError(f"the map window in {plan.crs} has no Web Mercator image (it reaches a pole or the far side of the projection)")
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lo_, la_ = t.transform([win[0], win[2]], [win[1], win[3]])
    rect = lo.rect_of(plan.tpl["map"])
    dpi = float(spec0.get("export", {}).get("dpi") or plan.tpl.get("raster_default_dpi") or 150)
    return {"merc": tuple(win), "wgs84": (min(lo_), min(la_), max(lo_), max(la_)), "width_mm": float(rect.w), "dpi": min(dpi, 200.0), "map_id": plan.spec["id"],
            "audience": plan.spec.get("audience") or "external", "spec": spec0, "display_crs": plan.crs if own else "EPSG:3857"}


def provider_rules(entry: dict, tier: str, source_kind: str) -> tuple[int, list[dict]]:
    fs: list[dict] = []
    pr = entry.get("provider") or {}
    lic = entry.get("license") or {}
    cap = CAPS.get(tier, CAPS["T1"])
    if source_kind == "http":
        if pr.get("print_ok") is False:
            fs.append(finding("MAP-WEB-011", "BLOCK", f"{entry.get('title')}: its terms do not allow printed or static maps"))
        if pr.get("cache_ok") is False:
            fs.append(finding("MAP-WEB-011", "WARN", f"{entry.get('title')}: its terms do not allow caching, so tiles are fetched each time and no cache is kept"))
        if not pr.get("bulk_ok", lic.get("bulk_ok", False)):
            cap = max(1, cap // BULK_FACTOR)
            fs.append(finding("MAP-WEB-011", "INFO", f"{entry.get('title')} does not allow bulk use, so the tile cap is {cap} (a few tiles for one map, not a download of an area)"))
        fs.append(finding("MAP-WEB-011", "WARN", f"check the current terms of {entry.get('title')} before this map is published; a static image of their tiles is a use their usage policy may limit", entry.get("id")))
    return min(cap, HARD_CAP), fs


def plan_render(spec_path, provider: str | None = None, *, tier: str = "T1", max_tiles: int | None = None, zoom: int | None = None, source: str | None = None,
                env=None, entries=None, style: str | None = None, terms_acknowledged: bool | None = None, detail: str | None = None,
                scheme: str | None = None) -> dict:
    """Everything decided before a single tile is read: provider gates, extent, zoom, tile list. Returns {ok, findings, plan}."""
    import ref_library as rl
    spec_path = Path(spec_path)
    try:
        ex = spec_extent(spec_path)
    except ValueError as exc:
        return {"ok": False, "findings": [finding("MAP-CRS-002", "BLOCK", f"{exc}; a tile basemap cannot be drawn under this map")]}
    wgs, w_mm, dpi, mid, audience, spec = ex["wgs84"], ex["width_mm"], ex["dpi"], ex["map_id"], ex["audience"], ex["spec"]
    warp_note = []
    if ex["display_crs"] != "EPSG:3857":
        # 1.11.0: tiles are published in Web Mercator; for a map in another projection the window's tiles are fetched as usual and the
        # composite is warped to the map's CRS by the build (needs rasterio). Words in the tiles are warped with it.
        try:
            import rasterio  # noqa: F401
        except ImportError:
            return {"ok": False, "findings": [finding("MAP-CRS-002", "BLOCK", f"the MapSpec sets crs.display to {ex['display_crs']}; warping the tiles to it needs the rasterio "
                                                      "package (python3 -m pip install rasterio). Without it, remove crs.display to draw the map in Web Mercator")]}
        warp_note = [finding("MAP-CRS-002", "INFO", f"tiles are published in Web Mercator; the composite is warped to {ex['display_crs']}, the map's own projection. "
                             "Place names inside the tiles turn and stretch with it: a style without labels reads best", {"display_crs": ex["display_crs"]})]
    bm = spec.get("basemap") or {}
    provider = provider or bm.get("provider")
    style = style or bm.get("style")
    source = source or bm.get("source")
    zoom = zoom if zoom is not None else bm.get("zoom")
    detail = detail or bm.get("detail")
    scheme = scheme or bm.get("scheme")
    if detail not in (None, "legible", "sharp"):
        return {"ok": False, "findings": [finding("MAP-TILE-001", "BLOCK", f"basemap.detail is legible or sharp, not {detail}")]}
    if scheme not in (None, "xyz", "tms"):
        return {"ok": False, "findings": [finding("MAP-TILE-003", "BLOCK", f"basemap.scheme is xyz or tms, not {scheme}")]}
    fs: list[dict] = list(warp_note)
    if not provider or provider in ("bundled", "none"):
        return {"ok": False, "findings": [finding("MAP-WEB-015", "BLOCK", "the MapSpec asks for no tile basemap; set basemap: {provider: carto-positron, mode: static} or name a provider")]}
    entries = entries if entries is not None else rl.all_entries("all")
    kind, src_info = "http", {}
    entry = None
    if source:
        sk, _, sv = source.partition(":")
        if sk not in ("mbtiles", "dir") or not sv:
            return {"ok": False, "findings": [finding("MAP-WEB-011", "BLOCK", "source is mbtiles:FILE or dir:FOLDER (tiles you supply)")]}
        p = Path(sv)
        if not p.exists():
            return {"ok": False, "findings": [finding("MAP-WEB-011", "BLOCK", f"{sk} source not found: {sv}")]}
        kind, src_info = sk, {"path": str(p)}
        entry = tr.entry_for(provider, entries) or tr.entry_for("pmtiles", entries)
        if provider in ("google-maps", "bing-maps"):
            return {"ok": False, "findings": [finding("MAP-WEB-011", "BLOCK", "Google Maps and Bing Maps tiles cannot be used, not even from a file")]}
    else:
        ack = bool(bm.get("terms_acknowledged")) if terms_acknowledged is None else bool(terms_acknowledged)
        r = tr.resolve(provider, entries, style=style, audience=audience, env=env, terms_acknowledged=ack, purpose="print", time=bm.get("time"))
        fs += r["findings"]
        if not r["ok"] or r["mode"] != "tiles":
            return {"ok": False, "findings": fs or [finding("MAP-WEB-015", "BLOCK", f"{provider} gives no tiles here (a key is missing or the provider needs your own file); the bundled basemap is used")]}
        entry = tr.entry_for(provider, entries)
        src_info = {"url": r["url"], "hosts": r["hosts"], "style": r.get("style"), "attribution": r.get("attribution_text") or r.get("attribution"), "max_zoom": r["max_zoom"], "tile_size": r["tile_size"],
                    "auth": r.get("auth")}                # 0.6.0: a registered key is added at send time; the address carries none
    pr = (entry or {}).get("provider") or {}
    tile_size = int(src_info.get("tile_size") or pr.get("tile_size") or 256)
    max_zoom = int(src_info.get("max_zoom") or pr.get("max_zoom") or 18)
    min_zoom = int(src_info.get("min_zoom") or pr.get("min_zoom") or 0)
    label_px = float(pr.get("label_px") or TILE_LABEL_PX)
    labels = pr.get("labels") if isinstance(pr.get("labels"), bool) else None
    cap, rf = provider_rules(entry or {}, tier, kind)
    fs += rf
    if blocked(fs):
        return {"ok": False, "findings": fs}
    if max_tiles:
        cap = min(cap, int(max_tiles), HARD_CAP)
    zooms_present = None
    name_for_style = style
    if kind == "mbtiles":
        mt = mbtiles_meta(Path(src_info["path"]))
        max_zoom = min(max_zoom, int(mt.get("maxzoom", mt.get("max_present", max_zoom))))
        min_zoom = int(mt.get("minzoom", mt.get("min_present", 0)))
        src_info["attribution"] = mt.get("attribution") or None
        src_info["meta"] = mt
        tile_size = int(mt.get("tile_size", tile_size))
        name_for_style = mt.get("name")
    elif kind == "dir":
        zooms_present = dir_zooms(Path(src_info["path"])) or None
        if zooms_present is None:
            fs.append(finding("MAP-TILE-002", "INFO", "the tile folder holds no zoom folders (0/, 1/, ... with x/y files) yet: this plan lists the tiles to put in it, "
                              "in XYZ order unless basemap.scheme says tms. If the tiles are already there, point dir: at the folder that holds the zoom folders"))
        else:
            min_zoom, max_zoom = min(zooms_present), min(max_zoom, max(zooms_present))
        name_for_style = Path(src_info["path"]).name
    if min_zoom > max_zoom:
        return {"ok": False, "findings": fs + [finding("MAP-TILE-002", "BLOCK", f"the tile source's zoom range is empty (min {min_zoom}, max {max_zoom})")]}
    detail_used = detail or default_detail(name_for_style, labels)
    mb = pad_bbox(ex["merc"], 2.0)
    need_px = w_mm / 25.4 * dpi
    g = choose_zoom(mb, need_px, cap, max_zoom, tile_size, zoom, min_zoom=min_zoom, detail=detail_used, map_w_mm=w_mm, label_px=label_px)
    if zooms_present is not None and g["z"] not in zooms_present:
        below = [z for z in zooms_present if z <= g["z"]]
        z2 = max(below) if below else min(zooms_present)
        fs.append(finding("MAP-TILE-002", "INFO", f"the tile folder has no zoom {g['z']}; zoom {z2} is used (present: {', '.join(map(str, zooms_present))})"))
        keep = {k: g[k] for k in ("ideal_zoom", "sharp_zoom", "limited_by_cap", "clamped_from", "detail", "min_zoom", "max_zoom")}
        g = {**grid(mb, z2), **keep}
        g["effective_px"] = round((mb[2] - mb[0]) / resolution(z2, tile_size))
        g["paper_dpi"] = round(paper_dpi(mb[2] - mb[0], z2, w_mm, tile_size))
        g["label_pt"] = round(label_pt(mb[2] - mb[0], z2, w_mm, tile_size, label_px), 1)
    if g["limited_by_cap"]:
        fs.append(finding("MAP-WEB-011", "WARN", f"the tile cap ({cap}) limits the basemap to zoom {g['z']}; zoom {g['ideal_zoom']} would suit this map ({g['detail']}). The background will look softer than the data; "
                          "raise the cap only if the provider's terms allow it, or use your own tile file"))
    if g.get("clamped_from") is not None:
        fs.append(finding("MAP-TILE-002", "WARN", f"zoom {g['clamped_from']} is outside the source's range ({min_zoom} to {max_zoom}); zoom {g['z']} is used. "
                          "Past its maximum a source has no tiles (blank squares); below its minimum it has none either"))
    elif detail_used == "sharp" and g["z"] == max_zoom and g["effective_px"] < need_px:
        fs.append(finding("MAP-TILE-002", "INFO", f"the source stops at zoom {max_zoom}; the background reaches about {g.get('paper_dpi')} dpi on paper, softer than the {dpi:.0f} dpi export"))
    if g.get("label_pt") is not None:
        why = {"legible": "the highest zoom whose labels print at 6 pt or more", "sharp": f"the lowest zoom that reaches {dpi:.0f} dpi", "set": "the zoom you set"}[g["detail"]]
        if g["limited_by_cap"]:
            why = f"held down by the tile cap; {g['detail']} would use zoom {g['ideal_zoom']}"
        fs.append(finding("MAP-TILE-001", "INFO", f"basemap zoom {g['z']} ({why}): its labels print at about {g['label_pt']:g} pt and the background at about {g['paper_dpi']} dpi on paper",
                          {"zoom": g["z"], "detail": g["detail"], "label_pt": g["label_pt"], "paper_dpi": g["paper_dpi"], "min_zoom": min_zoom, "max_zoom": max_zoom}))
        labelled = default_detail(name_for_style, labels) == "legible"
        if labelled and g["label_pt"] < LEGIBLE_LABEL_PT:
            fs.append(finding("MAP-TILE-001", "WARN", f"the basemap's labels print at about {g['label_pt']:g} pt, too small to read; set basemap: {{detail: legible}} "
                              "for readable place names, or use a style without labels when the map's own labels carry the names"))
        if g["paper_dpi"] < 60:
            fs.append(finding("MAP-TILE-001", "WARN", f"the basemap prints at about {g['paper_dpi']} dpi, which shows as blocky pixels; "
                              + ("the tile cap holds the zoom down" if g["limited_by_cap"] else "set a higher basemap.zoom, or basemap: {detail: sharp}")))
    if len(g["tiles"]) > cap:
        return {"ok": False, "findings": fs + [finding("MAP-WEB-011", "BLOCK", f"{len(g['tiles'])} tiles are needed at zoom {g['z']} and the cap is {cap}")]}
    if not src_info.get("attribution") and kind != "http":
        fs.append(finding("MAP-WEB-004", "WARN", "this tile source gives no attribution; add the credit its license asks for as the map's source note"))
    if kind == "dir":
        det = detect_scheme(Path(src_info["path"]), g["tiles"])
        if scheme:
            other = "tms" if scheme == "xyz" else "xyz"
            if det[f"{other}_hits"] > det[f"{scheme}_hits"]:
                fs.append(finding("MAP-TILE-003", "WARN", f"basemap.scheme is {scheme}, but the folder's files match {other} row order ({det[other + '_hits']} of {det['sampled']} tiles "
                                  f"found that way, {det[scheme + '_hits']} as {scheme}); north and south will be swapped tile by tile. Check the scheme", det))
            src_info["scheme"], src_info["scheme_by"] = scheme, "set"
        elif det["scheme"] is None:
            src_info["scheme"], src_info["scheme_by"] = "xyz", "default"
            if zooms_present:
                fs.append(finding("MAP-TILE-003", "WARN", f"no tile in the folder covers this map at zoom {g['z']}, read either as XYZ or as TMS; "
                                  "check that the folder holds this area at this zoom", det))
        else:
            src_info["scheme"], src_info["scheme_by"] = det["scheme"], "found"
            fs.append(finding("MAP-TILE-003", "INFO", f"the tile folder is read as {det['scheme'].upper()} ({'rows from the top' if det['scheme'] == 'xyz' else 'rows from the bottom'}): "
                              f"{det[det['scheme'] + '_hits']} of {det['sampled']} sampled tiles found that way, {det[('tms' if det['scheme'] == 'xyz' else 'xyz') + '_hits']} the other way. "
                              "Set basemap.scheme to override", det))
    return {"ok": True, "findings": fs, "plan": {"map_id": mid, "provider": provider, "kind": kind, "source": src_info, "entry": entry, "tier": tier, "cap": cap, "zoom": g["z"], "ideal_zoom": g["ideal_zoom"],
                                                  "tile_size": tile_size, "tiles": g["tiles"], "cols": len(g["cols"]), "rows": len(g["rows"]), "bbox_3857": list(g["bbox"]),
                                                  "bbox_wgs84": list(wgs), "dpi": dpi, "effective_px": g["effective_px"], "audience": audience, "display_crs": ex["display_crs"],
                                                  "detail": g["detail"], "label_pt": g.get("label_pt"), "paper_dpi": g.get("paper_dpi"), "min_zoom": min_zoom, "max_zoom": max_zoom,
                                                  "terms_acknowledged": bool(kind == "http" and r.get("terms_acknowledged")),
                                                  "customization": (r.get("customization") or "") if kind == "http" else ""}}


# ---------------------------------------------------------------------------------------------------------------- tile sources
def mbtiles_meta(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = dict(con.execute("select name, value from metadata").fetchall())
    except sqlite3.Error:
        rows = {}
    finally:
        con.close()
    out = {"attribution": rows.get("attribution"), "name": rows.get("name"), "format": rows.get("format", "png")}
    for k in ("minzoom", "maxzoom"):
        if str(rows.get(k, "")).isdigit():
            out[k] = int(rows[k])
    if "minzoom" not in out or "maxzoom" not in out:              # metadata is optional; the tiles table says what is there
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            lo_, hi_ = con.execute("select min(zoom_level), max(zoom_level) from tiles").fetchone()
            if lo_ is not None:
                out["min_present"], out["max_present"] = int(lo_), int(hi_)
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if out["format"] not in ("png", "jpg", "jpeg", "webp"):
        out["unsupported"] = out["format"]                 # pbf (vector) tiles cannot be drawn here
    return out


class MBTiles:
    def __init__(self, path: Path):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def get(self, z: int, x: int, y: int) -> bytes | None:
        row = self.con.execute("select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?", (z, x, (1 << z) - 1 - y)).fetchone()
        return bytes(row[0]) if row else None

    def close(self):
        self.con.close()


TILE_EXTS = ("png", "jpg", "jpeg", "webp")


def dir_zooms(root: Path) -> list[int]:
    """The zoom folders (0/, 1/, ...) of a tile folder that hold at least one x folder."""
    out = []
    for d in Path(root).iterdir() if Path(root).is_dir() else []:
        if d.is_dir() and d.name.isdigit() and any(c.is_dir() and c.name.isdigit() for c in d.iterdir()):
            out.append(int(d.name))
    return sorted(out)


def _tile_file(root: Path, z: int, x: int, y: int) -> Path | None:
    for ext in TILE_EXTS:
        p = root / str(z) / str(x) / f"{y}.{ext}"
        if p.is_file():
            return p
    return None


def detect_scheme(root: Path, tiles: list[tuple[int, int, int]], sample: int = 40) -> dict:
    """Which row order a tile folder uses, by counting the files each order finds among the tiles the map needs.
    XYZ counts rows from the top (most tools); TMS from the bottom (gdal2tiles before 3.1, and any folder with a tilemapresource.xml)."""
    root = Path(root)
    step = max(1, len(tiles) // sample)
    pick = tiles[::step][:sample]
    xyz = sum(1 for z, x, y in pick if _tile_file(root, z, x, y))
    tms = sum(1 for z, x, y in pick if _tile_file(root, z, x, (1 << z) - 1 - y))
    hint = (root / "tilemapresource.xml").is_file()
    if xyz == tms:
        found = "tms" if hint and tms else ("xyz" if xyz else None)
    else:
        found = "xyz" if xyz > tms else "tms"
    return {"scheme": found, "xyz_hits": xyz, "tms_hits": tms, "sampled": len(pick), "tilemapresource": hint}


class DirTiles:
    def __init__(self, root: Path, scheme: str = "xyz"):
        self.root = Path(root)
        self.scheme = scheme

    def get(self, z: int, x: int, y: int) -> bytes | None:
        p = _tile_file(self.root, z, x, (1 << z) - 1 - y if self.scheme == "tms" else y)
        return p.read_bytes() if p else None

    def close(self):
        pass


class HttpTiles:
    def __init__(self, url_template: str, http, cache_dir: Path | None, pause_s: float, sleep=time.sleep, auth=None):
        self.tpl, self.http, self.cache, self.pause, self.sleep, self.auth = url_template, http, cache_dir, pause_s, sleep, auth
        self.fetched = self.cached = 0
        self.last = 0.0
        self.errors: list[str] = []

    def path(self, z, x, y):
        return (self.cache / str(z) / str(x) / f"{y}.png") if self.cache else None

    def get(self, z: int, x: int, y: int) -> bytes | None:
        p = self.path(z, x, y)
        if p is not None and p.is_file():
            self.cached += 1
            return p.read_bytes()
        url = self.tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{-y}", str((1 << z) - 1 - y))
        wait = self.pause - (time.monotonic() - self.last)
        if self.last and wait > 0:
            self.sleep(wait)
        self.last = time.monotonic()
        try:
            r = self.http.get(url, None, expect="tile", **({"auth": self.auth} if self.auth is not None else {}))
        except oh.ServiceError as e:
            if e.auth:
                raise
            self.errors.append(f"{z}/{x}/{y}: {oh.untrusted(e, 100)}")
            return None
        if r.status != 200:
            self.errors.append(f"{z}/{x}/{y}: HTTP {r.status}")
            return None
        self.fetched += 1
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
        return r.content

    def close(self):
        pass


# ---------------------------------------------------------------------------------------------------------------- render
def out_dir(map_id: str) -> Path:
    return gc.workdir() / "basemaps" / re.sub(r"[^A-Za-z0-9_.-]+", "-", map_id)


def render(spec_path, provider: str | None = None, *, tier: str = "T1", max_tiles: int | None = None, zoom: int | None = None, source: str | None = None,
           http=None, env=None, entries=None, style: str | None = None, sleep=time.sleep, outroot: Path | None = None, pause_s: float | None = None,
           terms_acknowledged: bool | None = None, detail: str | None = None, scheme: str | None = None) -> dict:
    from PIL import Image
    p = plan_render(spec_path, provider, tier=tier, max_tiles=max_tiles, zoom=zoom, source=source, env=env, entries=entries, style=style, terms_acknowledged=terms_acknowledged,
                    detail=detail, scheme=scheme)
    if not p["ok"]:
        return {"ok": False, "findings": p["findings"]}
    pl, fs = p["plan"], list(p["findings"])
    if pl["kind"] == "mbtiles":
        if pl["source"]["meta"].get("unsupported"):
            return {"ok": False, "findings": fs + [finding("MAP-WEB-015", "BLOCK", f"this MBTiles file holds {pl['source']['meta']['unsupported']} (vector) tiles, which a static raster basemap cannot draw in this release")]}
        src = MBTiles(Path(pl["source"]["path"]))
    elif pl["kind"] == "dir":
        src = DirTiles(Path(pl["source"]["path"]), pl["source"].get("scheme") or "xyz")
    else:
        entry = pl["entry"] or {}
        pr = entry.get("provider") or {}
        cache = None if pr.get("cache_ok") is False else gc.workdir() / "tiles" / re.sub(r"[^a-z0-9]+", "-", pl["provider"]) / re.sub(r"[^a-z0-9_]+", "-", str(pl["source"].get("style") or "default"))
        http = http or oh.SafeHTTP(limits={"timeout_s": 20, "max_bytes": 5_000_000})
        pause = pause_s if pause_s is not None else max(MIN_PAUSE_S, 1.0 / float(pr.get("rate_limit_per_s") or 2))
        src = HttpTiles(pl["source"]["url"], http, cache, pause, sleep, auth=pl["source"].get("auth"))
    size = pl["tile_size"]
    img = Image.new("RGB", (size * pl["cols"], size * pl["rows"]), (255, 255, 255))
    x0 = min(t[1] for t in pl["tiles"])
    y0 = min(t[2] for t in pl["tiles"])
    missing = []
    try:
        for z, x, y in pl["tiles"]:
            data = src.get(z, x, y)
            if data is None:
                missing.append(f"{z}/{x}/{y}")
                continue
            try:
                t = Image.open(io.BytesIO(data)).convert("RGBA")
                if t.size != (size, size):
                    t = t.resize((size, size))
                bg = Image.new("RGBA", t.size, (255, 255, 255, 255))
                img.paste(Image.alpha_composite(bg, t).convert("RGB"), ((x - x0) * size, (y - y0) * size))
            except Exception:  # noqa: BLE001
                missing.append(f"{z}/{x}/{y}")
    finally:
        src.close()
    errs = getattr(src, "errors", [])
    if len(missing) > MAX_MISSING * len(pl["tiles"]):
        how = f"; the folder was read as {pl['source'].get('scheme', 'xyz').upper()} row order" if pl["kind"] == "dir" else ""
        return {"ok": False, "findings": fs + [finding("MAP-WEB-011", "BLOCK", f"{len(missing)} of {len(pl['tiles'])} tiles could not be read ({'; '.join(errs[:3]) or 'missing or unreadable'}{how}); no composite was written")]}
    if missing:
        fs.append(finding("MAP-WEB-011", "WARN", f"{len(missing)} tile(s) are blank in the basemap: {', '.join(missing[:5])}"))
    d = (Path(outroot) if outroot else out_dir(pl["map_id"]))
    d.mkdir(parents=True, exist_ok=True)
    png = d / "composite.png"
    img.save(png, optimize=True)
    entry = pl["entry"] or {}
    attribution = pl["source"].get("attribution") or (entry.get("license") or {}).get("attribution_text")
    rec = {"composite_version": 1, "map_id": pl["map_id"], "provider": pl["provider"], "entry": entry.get("id"), "entry_rev": entry.get("rev"), "source": pl["kind"], "style": pl["source"].get("style"),
           "zoom": pl["zoom"], "detail": pl.get("detail"), "label_pt": pl.get("label_pt"), "paper_dpi": pl.get("paper_dpi"), "zoom_range": [pl.get("min_zoom"), pl.get("max_zoom")],
           "scheme": pl["source"].get("scheme") if pl["kind"] == "dir" else ("tms" if pl["kind"] == "mbtiles" else "xyz"), "tile_size": size, "tiles": len(pl["tiles"]), "fetched": getattr(src, "fetched", 0), "from_cache": getattr(src, "cached", 0), "missing": missing,
           "bbox_3857": pl["bbox_3857"], "bbox_wgs84": pl["bbox_wgs84"], "display_crs": pl.get("display_crs") or "EPSG:3857", "pixels": [img.width, img.height], "attribution": attribution, "license": (entry.get("license") or {}).get("class"),
           "retrieved_at": dt.date.today().isoformat() if not __import__("os").environ.get("GMAP_FIXED_DATE") else __import__("os").environ["GMAP_FIXED_DATE"],
           "sha256": hashlib.sha256(png.read_bytes()).hexdigest(), "tier": tier, "terms_acknowledged": bool(pl.get("terms_acknowledged")), "customization": pl.get("customization") or "", "title": entry.get("title") or (pl["source"].get("meta") or {}).get("name")}
    (d / "composite.json").write_text(json.dumps(rec, indent=2))
    return {"ok": True, "findings": fs, "composite": str(png), "record": rec, "plan": {k: v for k, v in pl.items() if k not in ("entry", "source", "tiles")}}


def load_prepared(map_id: str, root: Path | None = None) -> dict | None:
    d = (Path(root) if root else out_dir(map_id))
    j, p = d / "composite.json", d / "composite.png"
    if not (j.is_file() and p.is_file()):
        return None
    rec = json.loads(j.read_text())
    if hashlib.sha256(p.read_bytes()).hexdigest() != rec.get("sha256"):
        return {"error": "the composite image changed after it was written; render it again"}
    rec["path"] = str(p)
    return rec


def public_plan(p: dict) -> dict:
    """What may be printed: no key, no address."""
    pl = dict(p.get("plan") or {})
    pl.pop("entry", None)
    pl.pop("source", None)
    pl["tile_count"] = len(pl.pop("tiles", []) or [])
    return {"ok": p["ok"], "findings": p["findings"], "plan": pl or None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["plan", "render", "show"])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--provider")
    ap.add_argument("--tier", choices=["T1", "T2"])
    ap.add_argument("--max-tiles", type=int)
    ap.add_argument("--zoom", type=int)
    ap.add_argument("--source")
    ap.add_argument("--style")
    ap.add_argument("--detail", choices=["legible", "sharp"], help="legible: labels print at 6 pt or more; sharp: pixels reach the export dpi (same as basemap.detail)")
    ap.add_argument("--scheme", choices=["xyz", "tms"], help="row order of a tile folder (same as basemap.scheme); found by trying both when not set")
    ap.add_argument("--terms-acknowledged", action="store_true", default=None, help="an older MapSpec's basemap.terms_acknowledged; the person's one acknowledgment (map-services) is read by itself")
    a = ap.parse_args(argv)
    tier = a.tier
    if tier is None:
        try:
            tier = json.loads((gc.workdir() / "env.json").read_text()).get("tier") or "T1"
        except (OSError, ValueError):
            tier = "T1"
        tier = tier if tier in CAPS else "T1"
    try:
        if a.op == "show":
            mid = gc.load_mapspec(Path(a.spec))["id"]
            r = load_prepared(mid)
            print(json.dumps(r, indent=2) if r else json.dumps({"prepared": False}))
            return 0 if r and "error" not in r else 3
        if a.op == "plan":
            p = plan_render(a.spec, a.provider, tier=tier, max_tiles=a.max_tiles, zoom=a.zoom, source=a.source, style=a.style, terms_acknowledged=a.terms_acknowledged,
                            detail=a.detail, scheme=a.scheme)
            print(json.dumps(public_plan(p), indent=2, default=str))
            return 0 if p["ok"] else 3
        r = render(a.spec, a.provider, tier=tier, max_tiles=a.max_tiles, zoom=a.zoom, source=a.source, style=a.style, terms_acknowledged=a.terms_acknowledged,
               detail=a.detail, scheme=a.scheme)
        if r.get("record"):
            r["record"].pop("path", None)
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ok"] else 3
    except oh.ServiceError as e:
        print(json.dumps({"ok": False, "findings": [finding(getattr(e, "rule", None) or "MAP-SVC-018", "BLOCK", str(e))]}))
        return 3
    except oh.Refusal as e:
        print(json.dumps({"ok": False, "findings": [finding("MAP-SVC-002", "BLOCK", str(e))]}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
