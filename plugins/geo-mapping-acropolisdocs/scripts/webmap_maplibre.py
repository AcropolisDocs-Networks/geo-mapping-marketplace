"""webmap_maplibre: the MapLibre half of the web page (M14, gap analysis P1-9): the MapLibre style, large layers as embedded PMTiles with their
clusters baked in, the Inter glyphs, the Protomaps vector basemap layers and the page's Content-Security-Policy. `webmap.build_page` calls it
when the page's engine is MapLibre; nothing here touches the network.

Large layers. A layer the page carries with more than TILE_LAYER_FEATURES features, or more than TILE_LAYER_BYTES of GeoJSON, goes into the
page as a PMTiles archive (tiles_out.write_pmtiles, GDAL's own writer), base64 in a script element, and is read in the browser through a
PMTiles source over that in-memory buffer (`maplibregl.addProtocol('pmtiles', ...)`). Smaller layers stay GeoJSON sources.

Clusters. A point layer with more than CLUSTER_MIN_POINTS points is clustered while the control `cluster` is on. A GeoJSON layer uses
MapLibre's own clustering. A tiled layer cannot be clustered in the browser, so its clusters are made here the way MapLibre's clustering
(supercluster) makes them: from the zoom below the points down, every item not yet taken gathers the items within 50 pixels, the cluster sits
at their weighted mean, and each zoom is built from the one above, so clusters nest. One source layer per zoom carries them, with
`point_count`; the points themselves start at the zoom where most of them stand apart (`points_zoom`). A layer with a time field is never
clustered: the time slider filters its points.

Zoom numbers. The page's own zoom numbers (the configuration, the shared address, the names' `min_zoom`) are those of 256-pixel tiles, as
Leaflet counts. MapLibre counts 512-pixel tiles, so its zoom is the page's minus one; the style below is written in MapLibre's numbers.
"""
from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path

import gmap_common as gc

MAPLIBRE_VERSION = "5.24.0"
PMTILES_VERSION = "4.5.0"
TILE_LAYER_FEATURES = 20_000            # a layer above this many features goes into the page as PMTiles
TILE_LAYER_BYTES = 3 * 1024 * 1024      # ...or above this much GeoJSON
CLUSTER_MIN_POINTS = 300                # a point layer with more points than this is clustered (control `cluster`)
CLUSTER_RADIUS_PX = 50                  # the cluster radius in screen pixels
CLUSTER_SINGLE_SHARE = 0.6              # the points show on their own from the first zoom at which this share of them stands alone
POINTS_EXTRA_ZOOMS = 2                  # a tiled point layer is cut at its first zoom and up to this many more (MapLibre enlarges past that)
MERC_HALF = 20037508.342789244
GLYPH_STACKS = {"Inter Regular": "inter-regular", "Inter SemiBold": "inter-semibold"}
BASEMAP_GLYPH_RANGES = ("0-255", "256-511", "512-767", "8192-8447")     # a vector basemap's names are unknown when the page is built
PMTILES_EMBED_MAX = 40 * 1024 * 1024    # a basemap file larger than this is not embedded: it is served beside the page
NAME_TIERS = ("village", "town", "water", "city", "capital", "admin")   # drawn bottom to top; the top one is placed first


class StyleError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- glyphs
def glyph_dir() -> Path:
    return gc.plugin_root() / "assets" / "glyphs"


def glyph_manifest() -> dict:
    return json.loads((glyph_dir() / "MANIFEST.json").read_text())


def range_of(cp: int) -> str:
    lo = (cp // 256) * 256
    return f"{lo}-{lo + 255}"


def covered(manifest: dict, stack: str) -> set[int]:
    out: set[int] = set()
    for r in (manifest["stacks"].get(stack) or {}).get("ranges", {}).values():
        for a, b in r["codepoints"]:
            out.update(range(a, b + 1))
    return out


_COVER: dict = {}


def glyphs_cover(text: str) -> bool:
    """1.9.0: every character of the text, and of its upper-case form, is in both label stacks' glyph files (Latin, Greek, Cyrillic)."""
    if "all" not in _COVER:
        man = glyph_manifest()
        sets = [covered(man, st) for st in man["stacks"]]
        _COVER["all"] = set.intersection(*sets) if sets else set()
    return all(ord(ch) in _COVER["all"] or ch.isspace() for ch in str(text) + str(text).upper())


def glyph_ranges_for(texts: dict[str, set[str]]) -> dict[str, set[str]]:
    """{stack: {range}} the label texts need. `texts` is {stack: {text}}; upper-case forms are included (labels drawn upper case)."""
    out: dict[str, set[str]] = {}
    for stack, ts in texts.items():
        rs = out.setdefault(stack, {"0-255"})
        for t in ts:
            for ch in str(t) + str(t).upper():
                rs.add(range_of(ord(ch)))
    return out


def pack_stack(style) -> dict | None:
    """1.10.0: the glyph files of a style pack's own label face (the pack's glyphs/ folder, written by tools/sdf_glyphs.py --packs):
    {stack, dir, ranges} or None when the pack labels in the brand's Inter or brings no glyph files. A file that does not match the
    manifest is not used."""
    if style is None or getattr(style, "fonts", "brand") == "brand" or not (getattr(style, "type", None) or {}).get("label"):
        return None
    if (style.type["label"] or {}).get("brand"):
        return None
    d = Path(style.dir) / "glyphs"
    try:
        man = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
        ranges = {r: v for r, v in (man.get("ranges") or {}).items() if (d / f"{r}.pbf").is_file() and (d / f"{r}.pbf").stat().st_size == v.get("bytes")}
    except (OSError, ValueError):
        return None
    if not man.get("stack") or "0-255" not in ranges:
        return None
    return {"stack": str(man["stack"]), "dir": d, "ranges": ranges}


def use_pack_stack(st: dict, stack: str) -> int:
    """Every label the engine draws takes the pack's label face. Returns the number of layers changed."""
    n = 0
    for ly in st["layers"]:
        if ly.get("type") == "symbol" and (ly.get("layout") or {}).get("text-field"):
            ly["layout"]["text-font"] = [stack]
            n += 1
    return n


def glyph_payload(ranges: dict[str, set[str]], pack: dict | None = None) -> tuple[dict, list[str]]:
    """({'<stack>/<range>': base64}, [ranges asked for that the plugin has no file for]). A pack's stack is served from the pack's own
    files; a range the pack's face has no file for (Greek, Cyrillic) is served from Inter Regular under the pack's name, so those names
    are still drawn."""
    man = glyph_manifest()
    out, missing = {}, []
    for stack, rs in ranges.items():
        own = pack["ranges"] if pack and stack == pack["stack"] else {}
        base = "Inter Regular" if own or stack not in GLYPH_STACKS else stack
        folder = GLYPH_STACKS[base]
        have = (man["stacks"].get(base) or {}).get("ranges", {})
        for r in sorted(rs, key=lambda s: int(s.split("-")[0])):
            if r in own:
                out[f"{stack}/{r}"] = base64.b64encode((pack["dir"] / f"{r}.pbf").read_bytes()).decode("ascii")
            elif r in have:
                out[f"{stack}/{r}"] = base64.b64encode((glyph_dir() / folder / f"{r}.pbf").read_bytes()).decode("ascii")
            else:
                missing.append(f"{stack} {r}")
    return out, missing


# ---------------------------------------------------------------------------------------------------------------- zooms
def ml_zoom(page_zoom: float) -> float:
    return max(0.0, float(page_zoom) - 1.0)


def _merc(lon, lat):
    import numpy as np
    lon = np.asarray(lon, dtype="float64")
    lat = np.clip(np.asarray(lat, dtype="float64"), -85.0511, 85.0511)
    x = np.radians(lon) * 6378137.0
    y = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * 6378137.0
    return x, y


def _grid(x, y, z: int, radius_px: int = CLUSTER_RADIUS_PX):
    """Cell keys of a radius_px grid at MapLibre zoom z (512-pixel tiles)."""
    import numpy as np
    cell = 2 * MERC_HALF / (512 * 2 ** z) * radius_px
    gx = np.floor((x + MERC_HALF) / cell).astype("int64")
    gy = np.floor((MERC_HALF - y) / cell).astype("int64")
    return gx * 4_294_967_296 + gy


def points_zoom(lon, lat, zmin: int, zmax: int) -> int:
    """The first MapLibre zoom from zmin at which CLUSTER_SINGLE_SHARE of the points stand alone in a CLUSTER_RADIUS_PX grid (never past zmax)."""
    import numpy as np
    x, y = _merc(lon, lat)
    n = len(x)
    for z in range(zmin, zmax + 1):
        _, inv, cnt = np.unique(_grid(x, y, z), return_inverse=True, return_counts=True)
        if n and (cnt[inv] == 1).sum() / n >= CLUSTER_SINGLE_SHARE:
            return z
    return zmax


def bake_clusters(lon, lat, zooms) -> dict:
    """{z: GeoDataFrame of clusters at MapLibre zoom z}, made as supercluster (MapLibre's clustering) makes them: from the highest zoom down,
    each item not yet taken (the biggest first) gathers the untaken items within CLUSTER_RADIUS_PX, the cluster sits at their weighted mean,
    and the next zoom down starts from these clusters. Each carries point_count, point_count_abbreviated and cluster = 1 (MapLibre's own
    names, so one style draws both kinds)."""
    import geopandas as gpd
    import numpy as np
    import shapely
    try:                                          # scipy is SOFT: its k-d tree is faster; shapely's STRtree (always there) finds the same neighbours
        from scipy.spatial import cKDTree
    except ImportError:
        cKDTree = None
    cx, cy = _merc(lon, lat)
    w = np.ones(len(cx), dtype="int64")
    out = {}
    for z in sorted(zooms, reverse=True):
        r = 2 * MERC_HALF / (512 * 2 ** z) * CLUSTER_RADIUS_PX
        if cKDTree is not None:
            kd = cKDTree(np.c_[cx, cy])
            near = lambda i, kd=kd, r=r: np.asarray(kd.query_ball_point((cx[i], cy[i]), r), dtype="int64")
        else:
            pts = shapely.points(cx, cy)
            st = shapely.STRtree(pts)
            near = lambda i, st=st, pts=pts, r=r: np.asarray(st.query(pts[i], predicate="dwithin", distance=r), dtype="int64")
        taken = np.zeros(len(cx), dtype=bool)
        nx, ny, nw = [], [], []
        for i in np.argsort(-w, kind="stable"):
            if taken[i]:
                continue
            nb = np.sort(near(i))
            nb = nb[~taken[nb]]
            taken[nb] = True
            ww = w[nb]
            s = ww.sum()
            nx.append(float((cx[nb] * ww).sum() / s))
            ny.append(float((cy[nb] * ww).sum() / s))
            nw.append(int(s))
        cx, cy, w = np.asarray(nx), np.asarray(ny), np.asarray(nw, dtype="int64")
        lo = np.degrees(cx / 6378137.0)
        la = np.degrees(2 * np.arctan(np.exp(cy / 6378137.0)) - np.pi / 2)
        out[z] = gpd.GeoDataFrame({"point_count": w, "point_count_abbreviated": [abbreviate(int(c)) for c in w],
                                   "cluster": np.ones(len(w), dtype="int64")}, geometry=shapely.points(lo, la), crs="EPSG:4326")
    return out


def abbreviate(n: int) -> str:
    """As MapLibre's clustering abbreviates a count: 1234 -> 1.2k, 12345 -> 12k."""
    if n >= 10000:
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{round(n / 100) / 10:g}k"
    return str(n)


# ---------------------------------------------------------------------------------------------------------------- tiled layers
def needs_tiles(col: dict) -> bool:
    n = len(col["features"])
    if n > TILE_LAYER_FEATURES:
        return True
    return n > 2000 and len(json.dumps(col, separators=(",", ":"))) > TILE_LAYER_BYTES


def collection_gdf(col: dict):
    """The page's GeoJSON collection of a layer as a GeoDataFrame in lon/lat (the properties the page uses, nothing else)."""
    import geopandas as gpd
    from shapely.geometry import shape
    feats = col["features"]
    keys: list[str] = []
    for f in feats:
        for k in f["properties"]:
            if k not in keys:
                keys.append(k)
    data = {k: [f["properties"].get(k) for f in feats] for k in keys}
    return gpd.GeoDataFrame(data, geometry=[shape(f["geometry"]) for f in feats], crs="EPSG:4326")


LOD_MVT_LIMITS = {"MAX_SIZE": 2_000_000, "MAX_FEATURES": 500_000}     # 1.6.0: per tile, for a layer with a level-of-detail pyramid
LOD_BUDGET_BYTES = 8 * 1024 * 1024                                       # the archive of such a layer; over it the pyramid starts a zoom higher


def lod_tile_layer(lid: str, col: dict, *, zmin: int, zmax: int, scratch: Path, attribution: str | None, cfg: dict,
                   budget: int = LOD_BUDGET_BYTES) -> dict:
    """1.6.0 (R4-BLD): a large polygon layer as every footprint from the top zoom (at most MapLibre 15, page 16) with GDAL's per-tile limits
    raised, and a level-of-detail pyramid below it (`<id>_lod<z>`, one zoom each). Returns tile_layer's record plus `lod`: {zooms, report}."""
    import lod
    import tiles_out as to
    gdf = collection_gdf(col)
    name = to.safe_layer_name(lid)
    top = min(lod.TOP_ZOOM_MAX, zmax)
    zs = lod.zooms_for(top, zmin, cfg.get("min_zoom"))
    blocks, report = lod.pyramid(gdf, zs, merge_gap_m=float(cfg["merge_gap_m"]), height_band_m=float(cfg["height_band_m"]),
                                 lod_height=str(cfg["lod_height"]))
    notes = []
    while True:
        layers = {name: gdf}
        zooms = {name: (top, max(top, zmax))}
        for z in zs:
            if len(blocks[z]):
                layers[f"{name}_lod{z}"] = blocks[z]
                zooms[f"{name}_lod{z}"] = (z, z)
        path = scratch / f"{name}.pmtiles"
        rep = to.write_pmtiles(layers, path, minzoom=min(v[0] for v in zooms.values()), maxzoom=max(v[1] for v in zooms.values()), simplify=True,
                               attribution=attribution, name=lid, description=f"layer {lid} of a web map page", layer_zooms=zooms, check=False,
                               mvt_limits=LOD_MVT_LIMITS, fixed_zooms=True)
        if rep["bytes"] <= budget or not zs:
            break
        notes.append(f"layer '{lid}': the level of detail starts at zoom {zs[0] + 2} (page numbering), not {zs[0] + 1}, to keep the tiles under "
                     f"{budget / 1048576:.0f} MB")
        zs = zs[1:]
    if len(rep["paths"]) != 1:
        raise StyleError(f"layer '{lid}': GDAL's tiling call is not reachable here, so the layer cannot go into one PMTiles archive")
    info = {k: {"features": int(v["features"]), "minzoom": int(v["minzoom"]), "maxzoom": int(v["maxzoom"]), "kind": v["kind"], "cluster": False,
                "points": int(v["features"]), "lod": k != name} for k, v in rep["layers"].items()}
    if zs:
        notes.append(f"layer '{lid}': every footprint from zoom {top + 1}; below it, blocks at zooms {zs[0] + 1} to {top} (page numbering), "
                     + ", ".join(f"{report[z]['area_kept']:.1%} of the area at {z + 1}" for z in zs))
    return {"key": f"gm-{name}", "path": str(path), "bytes": int(rep["bytes"]), "layers": info, "points_zoom": None, "source_layer": name,
            "zmin": min(i["minzoom"] for i in info.values()), "zmax": max(i["maxzoom"] for i in info.values()), "notes": notes,
            "lod": {"top": top, "zooms": zs, "report": {z: report[z] for z in zs}}}


def lod_style(layers: list[dict], source_layer: str, lod_info: dict) -> list[dict]:
    """The page's style for a layer with a pyramid: every style layer that reads `source_layer` starts at the top zoom, and a copy of it reads
    each `<source_layer>_lod<z>` at that zoom only (fills, outlines and extrusions alike)."""
    import copy
    out = []
    for L in layers:
        if L.get("source-layer") != source_layer:
            out.append(L)
            continue
        for z in lod_info["zooms"]:
            c = copy.deepcopy(L)
            c["id"] = f"{L['id']}--lod{z}"
            c["source-layer"] = f"{source_layer}_lod{z}"
            c["minzoom"], c["maxzoom"] = z, z + 1
            c.setdefault("metadata", {})["gm:lod"] = z
            out.append(c)
        top = copy.deepcopy(L)
        if lod_info["zooms"]:
            top["minzoom"] = max(top.get("minzoom", 0), lod_info["top"])
        out.append(top)
    return out


def tile_layer(lid: str, col: dict, kind: str, *, zmin: int, zmax: int, cluster: bool, scratch: Path, attribution: str | None,
               lod_cfg: dict | None = None) -> dict:
    """Write one large layer as an embedded PMTiles archive. Returns {key, path, bytes, layers: {source layer: {features, minzoom, maxzoom,
    kind, cluster}}, points_zoom, notes}. Zooms are MapLibre zooms (512-pixel tiles). 1.6.0: a polygon layer with `lod_cfg` (its extrude block
    or its `lod` block, lod not off) gets a level-of-detail pyramid (lod_tile_layer)."""
    import numpy as np
    import tiles_out as to
    if kind == "polygon" and lod_cfg and lod_cfg.get("lod") != "off":
        return lod_tile_layer(lid, col, zmin=zmin, zmax=zmax, scratch=scratch, attribution=attribution, cfg=lod_cfg)
    gdf = collection_gdf(col)
    notes = []
    name = to.safe_layer_name(lid)
    layers = {name: gdf}
    zooms = {name: (zmin, zmax)}
    pz = None
    if kind == "point":
        lon = np.asarray(gdf.geometry.x)
        lat = np.asarray(gdf.geometry.y)
        g3 = gdf.to_crs("EPSG:3857")
        pz = points_zoom(lon, lat, zmin, zmax) if cluster else zmin
        dz, _worst = to.dense_minzoom(g3, "point", pz, zmax)
        if dz > pz:
            notes.append(f"layer '{lid}': the points start at zoom {dz + 1} (page numbering), not {pz + 1}: below it one tile would hold over "
                         f"{to.TILE_FEATURES_POINTS:,} of them" + ("" if cluster else "; turn clustering on to show them as counts below that zoom"))
            pz = dz
        zooms[name] = (pz, min(zmax, pz + POINTS_EXTRA_ZOOMS))
        if cluster and pz > zmin:
            for z, cg in bake_clusters(lon, lat, range(zmin, pz)).items():
                cn = f"{name}__c{z}"
                layers[cn] = cg
                zooms[cn] = (z, z)
    path = scratch / f"{name}.pmtiles"
    rep = to.write_pmtiles(layers, path, minzoom=min(z[0] for z in zooms.values()), maxzoom=max(z[1] for z in zooms.values()),
                           simplify=kind != "point", attribution=attribution, name=lid, description=f"layer {lid} of a web map page",
                           layer_zooms=zooms, check=False)
    if len(rep["paths"]) != 1:
        raise StyleError(f"layer '{lid}': GDAL's tiling call is not reachable here, so the layer cannot go into one PMTiles archive")
    info = {}
    for k, v in rep["layers"].items():
        info[k] = {"features": int(v["features"]), "minzoom": int(v["minzoom"]), "maxzoom": int(v["maxzoom"]), "kind": v["kind"],
                   "cluster": k != name, "points": int(layers[k]["point_count"].sum()) if k != name else int(v["features"])}
    notes += [n for n in rep["notes"] if "top zoom" not in n]
    return {"key": f"gm-{name}", "path": str(path), "bytes": int(rep["bytes"]), "layers": info, "points_zoom": pz, "source_layer": name,
            "zmin": min(i["minzoom"] for i in info.values()), "zmax": max(i["maxzoom"] for i in info.values()), "notes": notes}


def b64_file(path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------------------------------------------- style
def _num(v, nd=3):
    return round(float(v), nd)


def _meta(layer: str, role: str, **kw) -> dict:
    return {"gm:layer": layer, "gm:role": role, **kw}



def _scaled(v, k: float):
    return v * k if isinstance(v, (int, float)) else ["*", v, k]


def highlight_passes(layers: list[dict], hl: dict) -> list[dict]:
    """Each drawn layer twice: the rest (`_hl` 0) faded to hl.rest of its opacity, then the highlighted features (`_hl` 1) with widths and radii
    hl.size larger. Both keep the layer's metadata, so the opacity slider still moves both; a numeric opacity stays numeric for it."""
    import copy
    rest, top = [], []
    for L in layers:
        r, t = copy.deepcopy(L), copy.deepcopy(L)
        r["id"] = L["id"] + "--rest"
        for lay, cond in ((r, ["==", ["get", "_hl"], 0]), (t, ["!=", ["get", "_hl"], 0])):
            lay["filter"] = ["all", lay["filter"], cond] if lay.get("filter") else cond
        for prop, v in list(r.get("paint", {}).items()):
            if prop.endswith("-opacity"):
                r["paint"][prop] = round(float(v) * hl["rest"], 3) if isinstance(v, (int, float)) else ["*", v, hl["rest"]]
        if r.get("paint") and not any(p.endswith("-opacity") for p in r["paint"]):
            key = {"line": "line-opacity", "fill": "fill-opacity", "circle": "circle-opacity"}.get(r.get("type"))
            if key:
                r["paint"][key] = hl["rest"]
        if hl.get("size", 1) != 1:
            for prop in ("line-width", "circle-radius"):
                if prop in t.get("paint", {}):
                    t["paint"][prop] = _scaled(t["paint"][prop], hl["size"])
        rest.append(r)
        top.append(t)
    return rest + top

def data_layers(d: dict, src: str, source_layer: str | None, t: dict, *, visible: bool, cluster: dict | None, label_field: str | None, label_zoom: float = 0,
                tiled: dict | None) -> tuple[list[dict], list[dict]]:
    """(drawn layers, symbol layers) for one page layer. Symbol layers (point collision boxes, feature labels, cluster counts) go above the
    place names so the names are placed after them and avoid them."""
    s = d["style"]
    vis = {"visibility": "visible" if visible else "none"}
    sl = {"source-layer": source_layer} if source_layer else {}
    lid = d["id"]
    base, syms = [], []
    op = _num(s.get("opacity", 1) if s.get("opacity") is not None else 1)
    if d["kind"] == "polygon":
        base.append({"id": f"{lid}--fill", "type": "fill", "source": src, **sl, "layout": dict(vis), "metadata": _meta(lid, "data"),
                     "paint": {"fill-color": ["coalesce", ["get", "_c"], s["fill"]], "fill-opacity": _num(s.get("fillOpacity", 1))}})
        if s.get("weight", 0) > 0:
            base.append({"id": f"{lid}--line", "type": "line", "source": src, **sl, "layout": {**vis, "line-join": "round"}, "metadata": _meta(lid, "data"),
                         "paint": {"line-color": s["stroke"], "line-width": _num(s["weight"]), "line-opacity": op}})
    elif d["kind"] == "line":
        paint = {"line-color": ["coalesce", ["get", "_c"], s["stroke"]], "line-width": _num(s["weight"]), "line-opacity": op}
        if s.get("dash"):
            paint["line-dasharray"] = [3, 2]
        if d.get("contour"):                         # index contours heavier and darker, as on paper (TODO M12)
            c = d["contour"]
            paint["line-color"] = ["case", ["==", ["get", "_i"], 1], c["index_stroke"], s["stroke"]]
            paint["line-width"] = ["case", ["==", ["get", "_i"], 1], _num(c["index_weight"]), _num(s["weight"])]
        if s.get("casing"):                           # 0.22.0: the print's casing, a wider land-coloured line under the data line
            c_ = s["casing"]
            base.append({"id": f"{lid}--casing", "type": "line", "source": src, **sl, "layout": {**vis, "line-join": "round", "line-cap": "round"},
                         "metadata": _meta(lid, "data"), "paint": {"line-color": c_["color"], "line-width": _num(c_["weight"]), "line-opacity": _num(c_["opacity"])}})
        base.append({"id": f"{lid}--line", "type": "line", "source": src, **sl, "layout": {**vis, "line-join": "round", "line-cap": "round"},
                     "metadata": _meta(lid, "data"), "paint": paint})
        if d.get("contour") and d["contour"].get("labels"):
            syms.append({"id": f"{lid}--elev", "type": "symbol", "source": src, **sl, "minzoom": ml_zoom(d["contour"]["label_zoom"]),
                         "filter": ["==", ["get", "_i"], 1], "metadata": _meta(lid, "label"),
                         "layout": {**vis, "symbol-placement": "line", "text-field": ["to-string", ["get", "_e"]], "text-font": ["Inter Regular"],
                                    "text-size": 10, "symbol-spacing": 320, "text-max-angle": 30, "text-keep-upright": True},
                         "paint": {"text-color": d["contour"]["index_stroke"], "text-halo-color": t["land"], "text-halo-width": 1.5}})
    elif d["kind"] == "point":
        unclustered = ["!", ["has", "point_count"]]
        radius = ["coalesce", ["get", "_r"], _num(s["radius"])]
        pt_filter = unclustered if cluster else None
        circ = {"id": f"{lid}--pt", "type": "circle", "source": src, **sl, "layout": dict(vis), "metadata": _meta(lid, "data"),
                "paint": {"circle-radius": radius, "circle-color": ["coalesce", ["get", "_c"], s["fill"]], "circle-opacity": _num(s.get("fillOpacity", 0.85)),
                          "circle-stroke-color": s["stroke"], "circle-stroke-width": _num(s.get("pointWeight", 1)), "circle-stroke-opacity": 1}}
        if pt_filter:
            circ["filter"] = pt_filter
        if tiled:
            circ["minzoom"] = tiled["points_zoom"]
        base.append(circ)
        # the collision layer: an invisible box per point (so the place names avoid the data's symbols) and the feature label beside it
        lay = {**vis, "icon-image": "gm-box", "icon-size": ["/", ["*", 2, radius], 8], "icon-allow-overlap": True, "icon-ignore-placement": False,
               "icon-padding": 0}
        paint = {"icon-opacity": 0}
        if label_field:
            lay.update({"text-field": ["step", ["zoom"], "", ml_zoom(label_zoom), ["to-string", ["get", label_field]]], "text-font": ["Inter Regular"], "text-size": 11, "text-anchor": "left",
                        "text-offset": [0.7, 0], "text-optional": True, "text-padding": 2})
            paint.update({"text-color": t["label"], "text-halo-color": t["land"], "text-halo-width": 1.5})
        sym = {"id": f"{lid}--sym", "type": "symbol", "source": src, **sl, "layout": lay, "paint": paint, "metadata": _meta(lid, "label")}
        if pt_filter:
            sym["filter"] = pt_filter
        if tiled:
            sym["minzoom"] = tiled["points_zoom"]
        syms.append(sym)
        if cluster:
            ccol = cluster["fill"]
            steps = ["step", ["get", "point_count"], 12, 100, 16, 1000, 21, 10000, 27]
            cdefs = []
            if tiled:                                # one source layer per zoom, each shown at its own zoom only
                for z in range(tiled["zmin"], tiled["points_zoom"]):
                    cdefs.append((f"{source_layer}__c{z}", z, z + 1, f"{lid}--cl{z}"))
            else:
                cdefs.append((None, None, None, f"{lid}--cl"))
            for csl, zlo, zhi, cid in cdefs:
                extra = {"source-layer": csl} if csl else {"filter": ["has", "point_count"]}
                zr = {} if zlo is None else {"minzoom": zlo, "maxzoom": zhi}
                base.append({"id": cid, "type": "circle", "source": src, **extra, **zr, "layout": dict(vis), "metadata": _meta(lid, "cluster"),
                             "paint": {"circle-color": ccol, "circle-radius": steps, "circle-opacity": 0.85, "circle-stroke-color": cluster["stroke"],
                                       "circle-stroke-width": 1.5, "circle-stroke-opacity": 1}})
                syms.append({"id": cid + "-n", "type": "symbol", "source": src, **extra, **zr, "metadata": _meta(lid, "cluster-count"),
                             "layout": {**vis, "text-field": ["get", "point_count_abbreviated"], "text-font": ["Inter SemiBold"], "text-size": 11,
                                        "text-allow-overlap": True, "text-ignore-placement": False, "icon-image": "gm-box",
                                        "icon-size": ["/", ["*", 2, steps], 8], "icon-allow-overlap": True, "icon-ignore-placement": False},
                             "paint": {"text-color": cluster["text"], "icon-opacity": 0}})
    if s.get("highlight"):                    # 0.23.0: the print's highlight, as two passes over the same features (filters on _hl)
        base = highlight_passes(base, s["highlight"])
    return base, syms


def names_layers(t: dict, admin_color: str, water_color: str, visible: bool) -> list[dict]:
    """Place, water and admin names as symbol layers, one per tier, shown by Natural Earth's own zoom (the page's numbers, so MapLibre's
    zoom plus one), thinned by MapLibre's own collision pass, the higher tiers placed first."""
    vis = "visible" if visible else "none"
    zf = ["all", [">=", ["+", ["zoom"], 1], ["get", "min_zoom"]], ["<=", ["+", ["zoom"], 1], ["coalesce", ["get", "max_zoom"], 99]]]
    halo = {"text-halo-color": t["land"], "text-halo-width": 1.6, "text-halo-blur": 0.5}
    tiers = {
        "capital": {"font": "Inter SemiBold", "size": 12, "upper": True, "spacing": 0.06, "icon": "gm-dot-capital", "color": t["label"]},
        "city": {"font": "Inter SemiBold", "size": 12, "icon": "gm-dot", "color": t["label"]},
        "town": {"font": "Inter Regular", "size": 11, "icon": "gm-dot", "color": t["label"]},
        "village": {"font": "Inter Regular", "size": 10, "icon": "gm-dot", "color": t["label"]},
        "water": {"font": "Inter Regular", "size": 11, "color": water_color},
        "admin": {"font": "Inter Regular", "size": 11, "upper": True, "spacing": 0.16, "color": admin_color},
    }
    out = []
    for tier in NAME_TIERS:
        c = tiers[tier]
        lay = {"visibility": vis, "text-field": ["get", "name"], "text-font": [c["font"]], "text-size": c["size"], "symbol-sort-key": ["-", 0, ["get", "prio"]],
               "text-padding": 2, "text-max-width": 8}
        if c.get("upper"):
            lay["text-transform"] = "uppercase"
        if c.get("spacing"):
            lay["text-letter-spacing"] = c["spacing"]
        if c.get("icon"):
            lay.update({"icon-image": c["icon"], "icon-size": 1, "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 0.45,
                        "text-justify": "auto"})
        out.append({"id": f"gm-names-{tier}", "type": "symbol", "source": "gm-names", "filter": ["all", ["==", ["get", "tier"], tier], zf],
                    "layout": lay, "paint": {"text-color": c["color"], **halo}, "metadata": _meta("gm-names", "names", tier=tier)})
    return out


def protomaps_layers(t: dict, src: str, visible: bool, group: str) -> list[dict]:
    """A quiet basemap from Protomaps vector tiles (schema v4: earth, landuse, water, roads, boundaries, places), in the brand's map colours."""
    vis = "visible" if visible else "none"
    m = lambda role: _meta(group, role)                          # noqa: E731
    roads_w = ["interpolate", ["linear"], ["zoom"], 5, 0.3, 10, 0.8, 14, 2.4]
    return [
        {"id": f"{group}--earth", "type": "fill", "source": src, "source-layer": "earth", "layout": {"visibility": vis}, "paint": {"fill-color": t["land"]}, "metadata": m("basemap")},
        {"id": f"{group}--water", "type": "fill", "source": src, "source-layer": "water", "layout": {"visibility": vis}, "paint": {"fill-color": t["water"]}, "metadata": m("basemap")},
        {"id": f"{group}--minor", "type": "line", "source": src, "source-layer": "roads", "minzoom": 11,
         "filter": ["in", ["get", "kind"], ["literal", ["minor_road", "other"]]], "layout": {"visibility": vis, "line-cap": "round", "line-join": "round"},
         "paint": {"line-color": t["frame"], "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.3, 15, 1.5], "line-opacity": 0.5}, "metadata": m("basemap")},
        {"id": f"{group}--major", "type": "line", "source": src, "source-layer": "roads",
         "filter": ["in", ["get", "kind"], ["literal", ["highway", "major_road"]]], "layout": {"visibility": vis, "line-cap": "round", "line-join": "round"},
         "paint": {"line-color": t["frame"], "line-width": roads_w, "line-opacity": 0.8}, "metadata": m("basemap")},
        {"id": f"{group}--bound", "type": "line", "source": src, "source-layer": "boundaries", "layout": {"visibility": vis},
         "paint": {"line-color": t["frame"], "line-width": 0.8, "line-dasharray": [3, 2]}, "metadata": m("basemap")},
        {"id": f"{group}--places", "type": "symbol", "source": src, "source-layer": "places", "filter": ["in", ["get", "kind"], ["literal", ["locality", "region"]]],
         "layout": {"visibility": vis, "text-field": ["get", "name"], "text-font": ["Inter Regular"], "text-size": 11, "symbol-sort-key": ["coalesce", ["get", "min_zoom"], 10],
                    "text-padding": 3},
         "paint": {"text-color": t["label"], "text-halo-color": t["land"], "text-halo-width": 1.5}, "metadata": m("basemap-label")},
    ]


# 1.10.0: the same quiet basemap from the two other common tile schemas. role -> (source layer, filter or None); a role a schema has no
# layer for is left out (OpenMapTiles and Shortbread carry no land layer: the page's water background is covered by a land-colour layer
# under the water, drawn as the map background of that basemap).
def _in(attr: str, values: list) -> list:
    return ["in", ["to-string", ["get", attr]], ["literal", values]]


VECTOR_SCHEMAS = {
    "openmaptiles": {"water": ("water", None), "minor": ("transportation", _in("class", ["tertiary", "minor"])),
                     "major": ("transportation", _in("class", ["motorway", "trunk", "primary", "secondary"])),
                     "bound": ("boundary", ["all", _in("admin_level", ["2", "3", "4"]), ["!=", ["to-string", ["get", "maritime"]], "1"]]),
                     "places": ("place", _in("class", ["city", "town", "state"])), "sort": "rank"},
    "shortbread": {"water": ("ocean", None), "water2": ("water_polygons", None), "minor": ("streets", _in("kind", ["tertiary", "unclassified", "residential"])),
                   "major": ("streets", _in("kind", ["motorway", "trunk", "primary", "secondary"])),
                   "bound": ("boundaries", ["all", _in("admin_level", ["2", "4"]), ["!=", ["to-string", ["get", "maritime"]], "true"]]),
                   "places": ("place_labels", _in("kind", ["capital", "state_capital", "city", "town"])), "sort": "population"},
}


def vector_base_layers(t: dict, src: str, visible: bool, group: str, schema: str | None) -> list[dict]:
    """The basemap layers for a vector tile source in its own schema (Protomaps when the schema is not named)."""
    sc = VECTOR_SCHEMAS.get(schema or "")
    if sc is None:
        return protomaps_layers(t, src, visible, group)
    vis = "visible" if visible else "none"
    m = lambda role: _meta(group, role)                          # noqa: E731
    out = [{"id": f"{group}--earth", "type": "background", "layout": {"visibility": vis}, "paint": {"background-color": t["land"]}, "metadata": m("basemap")}]

    def add(role, kind, paint, layout=None, minzoom=None, key=None):
        if (key or role) not in sc:
            return
        layer, flt = sc[key or role]
        d = {"id": f"{group}--{key or role}", "type": kind, "source": src, "source-layer": layer, "layout": {"visibility": vis, **(layout or {})}, "paint": paint,
             "metadata": m("basemap-label" if kind == "symbol" else "basemap")}
        if flt:
            d["filter"] = flt
        if minzoom is not None:
            d["minzoom"] = minzoom
        out.append(d)

    round_ = {"line-cap": "round", "line-join": "round"}
    add("water", "fill", {"fill-color": t["water"]})
    add("water", "fill", {"fill-color": t["water"]}, key="water2")
    add("minor", "line", {"line-color": t["frame"], "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.3, 15, 1.5], "line-opacity": 0.5}, round_, 11)
    add("major", "line", {"line-color": t["frame"], "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 10, 0.8, 14, 2.4], "line-opacity": 0.8}, round_)
    add("bound", "line", {"line-color": t["frame"], "line-width": 0.8, "line-dasharray": [3, 2]})
    add("places", "symbol", {"text-color": t["label"], "text-halo-color": t["land"], "text-halo-width": 1.5},
        {"text-field": ["get", "name"], "text-font": ["Inter Regular"], "text-size": 11, "text-padding": 3,
         "symbol-sort-key": (["coalesce", ["get", "rank"], 10] if sc["sort"] == "rank" else ["-", 0, ["coalesce", ["to-number", ["get", "population"]], 0]])})
    return out


def bundled_layers(bdefs: list[dict], detail_zoom: int) -> list[dict]:
    """The bundled background (Natural Earth countries, US states, lakes: the same data the print map draws) as fill and line layers, faded
    past the zoom where its generalized outlines stop being a position reference."""
    out = []
    fade = ["step", ["zoom"], 1, ml_zoom(detail_zoom), 0.45]
    for b in bdefs:
        st = b["style"]
        out.append({"id": f"{b['id']}--fill", "type": "fill", "source": b["id"], "paint": {"fill-color": st["fillColor"], "fill-opacity": fade},
                    "metadata": _meta("bundled", "bundled")})
        out.append({"id": f"{b['id']}--line", "type": "line", "source": b["id"], "paint": {"line-color": st["color"], "line-width": st["weight"], "line-opacity": fade},
                    "metadata": _meta("bundled", "bundled")})
    return out


def style(*, t: dict, bdefs: list[dict], bundled_attr: str | None, base_defs: list[dict], defs: list[dict], tiles: dict, label_fields: dict,
          clusters: dict, names_on: bool, label_zoom: float = 0, admin_color: str, water_color: str, detail_zoom: int) -> dict:
    """The MapLibre style of the page. GeoJSON sources carry `{"gm": id}` in place of their data; the page fills them from #gmap-data. The
    bundled background's credit is the attribution control's own text (customAttribution, as on the Leaflet page), so its sources carry none;
    a basemap source carries its provider's credit, which MapLibre shows while the basemap is on."""
    sources: dict = {}
    layers: list[dict] = [{"id": "gm-water", "type": "background", "paint": {"background-color": t["water"]}, "metadata": _meta("bundled", "bundled")}]
    for b in bdefs:
        sources[b["id"]] = {"type": "geojson", "data": {"gm": b["id"]}, "attribution": "", "maxzoom": 12, "tolerance": 0.6}
    layers += bundled_layers(bdefs, detail_zoom)
    first = base_defs[0]["id"] if base_defs else "bundled"
    for b in base_defs:
        on = b["id"] == first
        if b["mode"] == "tiles":
            sources[f"base-{b['id']}"] = {"type": "raster", "tiles": [b["url"]], "tileSize": int(b.get("tileSize") or 256), "maxzoom": int(b["nativeMaxZoom"]) + (1 if int(b.get("tileSize") or 256) == 512 else 0),
                                          "minzoom": int(b.get("nativeMinZoom") or 0), "attribution": b.get("attribution") or ""}
            layers.append({"id": f"base-{b['id']}", "type": "raster", "source": f"base-{b['id']}", "layout": {"visibility": "visible" if on else "none"},
                           "metadata": _meta(f"base:{b['id']}", "basemap")})
            if b.get("treatment"):                         # 0.22.0: greyed, and see-through over the land colour, as on paper
                layers[-1]["paint"] = {"raster-saturation": -_num(b["treatment"]["grayscale"]), "raster-opacity": _num(b["treatment"]["opacity"])}
        elif b["mode"] in ("vector", "vector-file"):
            sources[f"base-{b['id']}"] = ({"type": "vector", "tiles": [b["url"]], "maxzoom": int(b["nativeMaxZoom"]), "attribution": b.get("attribution") or ""}
                                          if b["mode"] == "vector" else {"type": "vector", "url": b["url"], "attribution": b.get("attribution") or ""})
            layers += vector_base_layers(t, f"base-{b['id']}", on, f"base:{b['id']}", b.get("schema"))
    syms: list[dict] = []
    for d in defs:
        if d["kind"] == "live":                            # 0.15.0: a live service layer, fetched by the viewer's browser from its one host
            sid = f"live-{d['id']}"
            on = "visible" if d["visible"] else "none"
            if d["raster"]:
                sources[sid] = {"type": "raster", "tiles": [d["url"]], "tileSize": 256, "minzoom": int(d.get("minzoom") or 0), "maxzoom": int(d.get("maxzoom") or 19),
                                "attribution": d.get("credit") or ""}
                layers.append({"id": f"{d['id']}--img", "type": "raster", "source": sid, "layout": {"visibility": on},
                               "paint": {"raster-opacity": _num(d.get("opacity", 1)), "raster-fade-duration": 0}, "metadata": _meta(d["id"], "image")})
                continue
            if d["live"] == "mvt":
                sources[sid] = {"type": "vector", "tiles": [d["url"]], "minzoom": int(d.get("minzoom") or 0), "maxzoom": int(d.get("maxzoom") or 14),
                                "attribution": d.get("credit") or ""}
                b, s = data_layers({**d, "kind": d["geometry"]}, sid, d["source_layer"], t, visible=d["visible"], cluster=None, label_field=None, tiled=None)
            else:
                sources[sid] = {"type": "geojson", "data": d["url"], "attribution": d.get("credit") or ""}
                b, s = data_layers({**d, "kind": d["geometry"]}, sid, None, t, visible=d["visible"], cluster=None, label_field=None, tiled=None)
            layers += b
            syms += s
            continue
        if d["kind"] == "image":
            sid = f"img-{d['id']}"
            b = d["bounds"]                                    # [[south, west], [north, east]]
            s, w, n, e = b[0][0], b[0][1], b[1][0], b[1][1]
            sources[sid] = {"type": "image", "url": f"gmimg://{d['id']}", "coordinates": [[w, n], [e, n], [e, s], [w, s]]}
            layers.append({"id": f"{d['id']}--img", "type": "raster", "source": sid, "layout": {"visibility": "visible" if d["visible"] else "none"},
                           "paint": {"raster-opacity": _num(d.get("opacity", 1)), "raster-fade-duration": 0}, "metadata": _meta(d["id"], "image")})
            continue
        tl = tiles.get(d["id"])
        if tl:
            sid = f"tiles-{d['id']}"
            sources[sid] = {"type": "vector", "url": f"pmtiles://{tl['key']}", "attribution": ""}
            sl = tl["source_layer"]
        else:
            sid = f"data-{d['id']}"
            src = {"type": "geojson", "data": {"gm": d["id"]}, "attribution": "", "tolerance": 0.3}
            if clusters.get(d["id"]):
                c = clusters[d["id"]]
                src.update({"cluster": True, "clusterRadius": CLUSTER_RADIUS_PX, "clusterMaxZoom": max(0, int(c["points_zoom"]) - 1)})
            sources[sid] = src
            sl = None
        b, s = data_layers(d, sid, sl, t, visible=d["visible"], cluster=clusters.get(d["id"]), label_field=label_fields.get(d["id"]), label_zoom=label_zoom, tiled=tl)
        layers += b
        syms += s
    if names_on:
        sources["gm-names"] = {"type": "geojson", "data": {"gm": "gm-names"}, "attribution": ""}
        layers += names_layers(t, admin_color, water_color, True)
    layers += syms
    return {"version": 8, "name": "gmap", "glyphs": "gmglyph://{fontstack}/{range}.pbf", "sources": sources, "layers": layers,
            "metadata": {"gm:engine": "maplibre", "gm:zoom": "MapLibre zoom = page zoom - 1"}}


def style_texts(st: dict, data: dict, tiles_text: dict[str, set[str]]) -> dict[str, set[str]]:
    """{stack: {text}} every symbol layer of the style can draw, read from the style and the page's data (the glyph lint uses the same)."""
    out: dict[str, set[str]] = {}
    for ly in st["layers"]:
        if ly.get("type") != "symbol":
            continue
        lay = ly.get("layout") or {}
        tf = lay.get("text-field")
        if not tf:
            continue
        stack = (lay.get("text-font") or ["Inter Regular"])[0]
        bucket = out.setdefault(stack, set())
        field = _text_field(tf)
        src = (st["sources"].get(ly.get("source")) or {})
        if field == "point_count_abbreviated":
            bucket.update({"0123456789.k"})
            continue
        if src.get("type") == "geojson" and isinstance(src.get("data"), dict) and src["data"].get("gm"):
            col = data.get(src["data"]["gm"]) or {}
            for f in col.get("features") or []:
                v = (f.get("properties") or {}).get(field)
                if v is not None:
                    bucket.add(str(v))
        elif ly.get("source", "").startswith("tiles-"):
            bucket.update(tiles_text.get(ly["source"][len("tiles-"):], set()))
    return out


def _text_field(tf) -> str | None:
    s = json.dumps(tf)
    m = re.search(r'\["get", "([^"]+)"\]', s)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------------------------------------------- CSP
def csp(engine: str, img_hosts: list[str], fetch_hosts: list[str], self_fetch: bool = False) -> str:
    """The page's Content-Security-Policy: nothing but what the engine needs. Leaflet loads tile pictures as images (img-src). MapLibre also
    starts its worker from a blob: URL (worker-src, child-src) and fetches tiles with fetch() (connect-src); a basemap file served beside
    the page is fetched from the page's own origin ('self')."""
    img = "data: blob:" + "".join(" https://" + h for h in img_hosts)
    conn = " ".join((["'self'"] if self_fetch else []) + ["https://" + h for h in fetch_hosts]) or "'none'"
    parts = ["default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'", "font-src data:", f"img-src {img}"]
    if engine == "maplibre":
        parts += ["worker-src blob:", "child-src blob:"]
    parts += [f"connect-src {conn}", "base-uri 'none'", "form-action 'none'"]
    return "; ".join(parts)


def vendor_text(name: str) -> str:
    return (gc.plugin_root() / "vendor" / "maplibre" / name).read_text(encoding="utf-8")


def available() -> str | None:
    """None when the MapLibre engine can be built; otherwise why not."""
    root = gc.plugin_root()
    for rel in ("vendor/maplibre/maplibre-gl.js", "vendor/maplibre/maplibre-gl.css", "vendor/maplibre/pmtiles.js", "assets/glyphs/MANIFEST.json",
                "assets/web/engine-maplibre.js"):
        if not (root / rel).is_file():
            return f"{rel} is missing from this edition"
    return None


def zoom_note(z: float) -> int:
    """A MapLibre zoom in the page's numbers (for messages)."""
    return int(math.floor(z)) + 1
