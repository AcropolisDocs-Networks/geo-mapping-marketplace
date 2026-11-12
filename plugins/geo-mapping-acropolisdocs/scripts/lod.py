#!/usr/bin/env python3
"""lod: a level-of-detail pyramid for a large polygon layer on a web page (1.6.0, R4-BLD).

At the top zoom a page carries every footprint. Below it, each zoom gets its own source layer `<id>_lod<z>`:
  - footprints closer than the merge gap (merge_gap_m, or half a pixel of that zoom when that is wider) whose heights fall in the same band
    (height_band_m) and that share a class colour are dissolved into blocks;
  - each block is simplified at half a pixel of that zoom, and a block smaller than one square pixel is dropped at that zoom only;
  - a block's height is the band's highest footprint (`lod_height: max`) or the area-weighted mean (`mean`); its base is the lowest base.
Each zoom reports the share of the footprint area its blocks keep (MAP-WEB-018 extended: under 98 percent is a WARN).

Zooms are MapLibre zooms (512-pixel tiles); the page numbers them one higher.
"""
from __future__ import annotations

import math

R = 6378137.0
KEEP_AREA_MIN = 0.98
DEFAULTS = {"lod": "auto", "merge_gap_m": 1.0, "height_band_m": 3.0, "lod_height": "max", "min_zoom": "auto"}
TOP_ZOOM_MAX = 15                  # MapLibre zoom 15 is page zoom 16: the top zoom never goes past it


def config(ex: dict | None) -> dict:
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (ex or {}).items() if k in DEFAULTS})
    if str(c["lod"]).lower() in ("off", "false", "none"):
        c["lod"] = "off"
    return c


def px_3857(z: int) -> float:
    """The size of one pixel at MapLibre zoom z, in Web Mercator units."""
    return 2 * math.pi * R / (512 * 2 ** z)


def pyramid(gdf, zooms, *, merge_gap_m: float = 1.0, height_band_m: float = 3.0, lod_height: str = "max") -> tuple[dict, dict]:
    """Blocks for each zoom. `gdf`: the layer in lon/lat with `_h` (height), optionally `_b` (base) and `_c` (class colour).
    -> ({z: GeoDataFrame in lon/lat with _h, _b, _c, _n}, {z: {"blocks", "footprints", "area_kept"}})."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    g = gdf.to_crs("EPSG:3857").reset_index(drop=True)
    g = g[g.geometry.notna() & ~g.geometry.is_empty & g.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].reset_index(drop=True)
    h = pd.to_numeric(g.get("_h", pd.Series(np.zeros(len(g)))), errors="coerce").fillna(0.0).to_numpy()
    b = pd.to_numeric(g.get("_b", pd.Series(np.zeros(len(g)))), errors="coerce").fillna(0.0).to_numpy()
    c = g["_c"].astype(str).to_numpy() if "_c" in g.columns else np.array([""] * len(g))
    lat = float(np.degrees(2 * np.arctan(np.exp(float(np.mean(g.total_bounds[[1, 3]])) / R)) - np.pi / 2)) if len(g) else 0.0
    m_to_3857 = 1.0 / max(0.05, math.cos(math.radians(lat)))          # one ground metre in Web Mercator units here
    band = np.floor(h / max(0.1, float(height_band_m))).astype(int)
    geoms = shapely.make_valid(g.geometry.values)
    area = shapely.area(geoms)
    total = float(area.sum()) or 1.0
    rp = shapely.point_on_surface(geoms)
    out, report = {}, {}
    for z in zooms:
        px = px_3857(z)
        gap = max(float(merge_gap_m) * m_to_3857, px / 2)
        pieces, _owners = [], []                                   # each block before the size rule, and the footprints it holds
        for key in sorted(set(zip(band.tolist(), c.tolist()))):
            sel = np.where((band == key[0]) & (c == key[1]))[0]
            merged = shapely.union_all(shapely.buffer(geoms[sel], gap / 2, quad_segs=2))
            merged = shapely.buffer(merged, -gap / 2, quad_segs=2)
            parts = shapely.get_parts(merged)
            parts = parts[~shapely.is_empty(parts)]
            if not len(parts):
                continue
            tree = shapely.STRtree(parts)
            pi, fi = tree.query(rp[sel], predicate="intersects")
            first = {}
            for a_, b_ in zip(pi.tolist(), fi.tolist()):
                first.setdefault(a_, b_)                          # a footprint belongs to one block
            groups: dict = {}
            for a_, b_ in first.items():
                groups.setdefault(b_, []).append(int(sel[a_]))
            for b_, fps in groups.items():
                pieces.append([parts[b_], key[1], fps])
        if not pieces:
            out[z] = gpd.GeoDataFrame({"_h": [], "_b": [], "_n": []}, geometry=[], crs="EPSG:4326")
            report[z] = {"blocks": 0, "footprints": int(len(g)), "area_kept": 0.0}
            continue
        pa = np.array([shapely.area(p_[0]) for p_ in pieces])
        small = pa < px * px
        # a piece under one square pixel follows the nearest piece beside it (within the merge gap, the same class colour), which may itself
        # follow another: a row of buildings of mixed heights becomes one block at a coarse zoom and is not lost. A block keeps its own
        # height (the root piece's); pieces that follow it add their area. What is still under one square pixel is dropped at this zoom only.
        geo_arr = np.array([p_[0] for p_ in pieces], dtype=object)
        tree = shapely.STRtree(geo_arr)
        parent = list(range(len(pieces)))
        has_big = [not sm for sm in small.tolist()]

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        si = np.where(small)[0]
        if len(si):
            qa, qb = tree.query(geo_arr[si], predicate="dwithin", distance=gap)
            edges = sorted({(int(si[a_]), int(b_)) for a_, b_ in zip(qa.tolist(), qb.tolist()) if int(si[a_]) != int(b_)
                            and pieces[int(si[a_])][1] == pieces[int(b_)][1]},
                           key=lambda e: shapely.distance(geo_arr[e[0]], geo_arr[e[1]]))
            for a_, b_ in edges:
                ra, rb = root(a_), root(b_)
                if ra == rb or (has_big[ra] and has_big[rb]):
                    continue                                       # two blocks of their own never merge through a small piece
                if has_big[rb] or (not has_big[ra] and pa[rb] > pa[ra]):
                    ra, rb = rb, ra
                parent[rb] = ra
                has_big[ra] = has_big[ra] or has_big[rb]
        comps: dict = {}
        for i in range(len(pieces)):
            comps.setdefault(root(i), []).append(i)
        rows = []
        kept_area = 0.0
        for r_, members in comps.items():
            geo = shapely.union_all([geo_arr[i] for i in members]) if len(members) > 1 else geo_arr[r_]
            if len(members) > 1:
                geo = shapely.buffer(shapely.buffer(geo, gap / 2, quad_segs=2), -gap / 2, quad_segs=2)
            if shapely.area(geo) < px * px:
                continue
            geo = shapely.simplify(geo, px / 2, preserve_topology=True)
            if shapely.area(geo) < px * px:
                continue
            own = pieces[r_][2] if not small[r_] else [f for i in members for f in pieces[i][2]]
            allf = [f for i in members for f in pieces[i][2]]
            hh = float(h[own].max()) if lod_height != "mean" else float((h[own] * area[own]).sum() / max(area[own].sum(), 1e-9))
            rows.append((geo, round(hh, 1), round(float(b[allf].min()), 1), pieces[r_][1], len(allf)))
            kept_area += float(area[allf].sum())
        gz = gpd.GeoDataFrame({"_h": [r[1] for r in rows], "_b": [r[2] for r in rows], "_c": [r[3] or None for r in rows], "_n": [r[4] for r in rows]},
                              geometry=[r[0] for r in rows], crs="EPSG:3857").to_crs("EPSG:4326")
        if "_c" not in gdf.columns:
            gz = gz.drop(columns=["_c"])
        out[z] = gz
        report[z] = {"blocks": len(gz), "footprints": int(len(g)), "area_kept": round(kept_area / total, 4)}
    return out, report


def zooms_for(top: int, page_min: int, asked_min) -> list[int]:
    lo = page_min if asked_min in (None, "auto") else max(0, int(asked_min) - 1)          # min_zoom is in page numbering
    return list(range(max(0, lo), top))
