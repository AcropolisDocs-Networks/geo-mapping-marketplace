#!/usr/bin/env python3
"""interrupted: maps in a projection with cuts (Spilhaus, Goode homolosine; the polyhedral nets follow in 4.1.0) (3.7.0, spec C5; Day 22's ocean map).

A projection that cuts the world draws a line that crosses a cut as one long stroke across the sheet, and a land polygon cut in two as a
garbled shape. So, in such a projection:
  - every line layer is densified in longitude and latitude (DENSIFY_DEG) before it is projected, then split wherever one step jumps more
    than JUMP_SHARE of the map's width; the number of features split is reported;
  - the ground (land and water) is drawn as a picture made by inverse projection: each pixel of the map's window is taken back to longitude
    and latitude and coloured land or water from the bundled 1:10m ocean, so the coasts are exact and nothing is garbled;
  - a polygon data layer is drawn as it is, with a warning (MAP-CRS-012) that a feature crossing a cut may draw wrongly.

`crs: {display: spilhaus}` (or `goode`) names one; Spilhaus needs PROJ 9.6 or later (map-doctor checks it).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

NAMED = {"spilhaus": "+proj=spilhaus +datum=WGS84 +units=m +no_defs", "goode": "+proj=igh +datum=WGS84 +units=m +no_defs",
         "goode-ocean": "+proj=igh_o +lon_0=-160 +datum=WGS84 +units=m +no_defs"}
CUT_PROJ = ("spilhaus", "igh", "igh_o")
DENSIFY_DEG = 0.2
JUMP_SHARE = 0.1                 # a step longer than a tenth of the map's width crosses a cut
OCEAN_RES_DEG = 0.05
MAX_PX = 2400


class InterruptedError(Exception):
    pass


def named(display) -> str | None:
    s = str(display or "").strip().lower()
    return NAMED.get(s)


def proj_ok() -> str | None:
    """None when PROJ draws Spilhaus here, else why not (PROJ before 9.6)."""
    try:
        from pyproj import CRS
        CRS.from_user_input(NAMED["spilhaus"])
        return None
    except Exception as ex:  # noqa: BLE001
        import pyproj
        return f"PROJ {getattr(pyproj, 'proj_version_str', '?')} cannot draw Spilhaus (it needs PROJ 9.6 or later: pyproj 3.8): {str(ex)[:120]}"


def is_cut(crs) -> bool:
    try:
        from pyproj import CRS
        s = CRS.from_user_input(crs).to_proj4() or ""
    except Exception:  # noqa: BLE001
        s = str(crs or "")
    return any(f"+proj={p} " in s + " " for p in CUT_PROJ)


def split_lines(gdf, crs, width: float, *, densify_deg: float = DENSIFY_DEG, share: float = JUMP_SHARE):
    """(the layer in `crs`, its lines densified in lon/lat and cut where a step jumps more than share x width; the number of features cut)."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import LineString, MultiLineString
    g = gdf.to_crs(4326) if gdf.crs is not None and not gdf.crs.is_geographic else gdf
    g = g.copy()
    g["geometry"] = shapely.segmentize(np.asarray(g.geometry.values, dtype=object), densify_deg)
    p = g.to_crs(crs)
    lim = share * float(width)
    out, cut = [], 0
    for geom in p.geometry:
        if geom is None or geom.is_empty:
            out.append(geom)
            continue
        parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        pieces, did = [], False
        for ln in parts:
            xy = np.asarray(ln.coords)[:, :2]
            ok = np.all(np.isfinite(xy), axis=1)
            seg = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
            br = set(np.flatnonzero(seg > lim).tolist()) | set(np.flatnonzero(~(ok[:-1] & ok[1:])).tolist())
            start = 0
            for b in sorted(br) + [len(xy) - 1]:
                chunk = xy[start:b + 1]
                chunk = chunk[np.all(np.isfinite(chunk), axis=1)]
                if len(chunk) >= 2:
                    pieces.append(LineString(chunk))
                start = b + 1
            did = did or bool(br)
        cut += int(did)
        out.append(MultiLineString(pieces) if len(pieces) > 1 else (pieces[0] if pieces else None))
    return gpd.GeoDataFrame(p.drop(columns="geometry"), geometry=out, crs=crs), cut


_OCEAN: dict = {}


def ocean_mask(res_deg: float = OCEAN_RES_DEG):
    """A global boolean grid (rows from 90 N), True on water, from the bundled 1:10m ocean."""
    if res_deg in _OCEAN:
        return _OCEAN[res_deg]
    import map_plan as mp
    from rasterio import features
    from rasterio.transform import from_origin
    oc, _e = mp.load_ref_layer(mp.BASEMAP_REFS["ocean"])
    oc = oc.to_crs(4326) if oc.crs is not None and not oc.crs.is_geographic else oc
    w, h = int(round(360 / res_deg)), int(round(180 / res_deg))
    m = features.rasterize(((g, 1) for g in oc.geometry if g is not None and not g.is_empty), out_shape=(h, w),
                           transform=from_origin(-180, 90, res_deg, res_deg), fill=0, dtype="uint8").astype(bool)
    _OCEAN[res_deg] = m
    return m


def ground_image(crs, bbox, px_w: int, land: str, water: str, *, res_deg: float = OCEAN_RES_DEG) -> dict:
    """{png, bbox}: the window drawn by inverse projection, land and water from the 1:10m ocean, outside the projection transparent."""
    from PIL import Image
    from pyproj import Transformer
    x0, y0, x1, y1 = (float(v) for v in bbox)
    W = int(max(200, min(MAX_PX, px_w)))
    H = int(max(100, round(W * (y1 - y0) / max(x1 - x0, 1e-9))))
    xs = x0 + (np.arange(W) + 0.5) * (x1 - x0) / W
    ys = y1 - (np.arange(H) + 0.5) * (y1 - y0) / H
    X, Y = np.meshgrid(xs, ys)
    tr = Transformer.from_crs(crs, 4326, always_xy=True)
    lon, lat = tr.transform(X.ravel(), Y.ravel())
    lon, lat = np.asarray(lon), np.asarray(lat)
    ok = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) <= 180) & (np.abs(lat) <= 90)
    # a point outside the projection comes back as another place: a forward round trip within a pixel keeps only real ones
    fw = Transformer.from_crs(4326, crs, always_xy=True)
    bx, by = fw.transform(np.where(ok, lon, 0.0), np.where(ok, lat, 0.0))
    px = (x1 - x0) / W
    ok &= np.hypot(np.asarray(bx) - X.ravel(), np.asarray(by) - Y.ravel()) < 2.0 * px
    m = ocean_mask(res_deg)
    lat0, lon0 = np.where(ok, lat, 0.0), np.where(ok, lon, 0.0)
    r = np.clip(((90.0 - lat0) / res_deg).astype(int), 0, m.shape[0] - 1)
    c = np.clip(((lon0 + 180.0) / res_deg).astype(int), 0, m.shape[1] - 1)
    wet = np.where(ok, m[r, c], False)

    def rgb(hx):
        hx = str(hx).lstrip("#")
        return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))
    img = np.zeros((H * W, 4), dtype=np.uint8)
    img[ok & ~wet, :3], img[ok & wet, :3] = rgb(land), rgb(water)
    img[ok, 3] = 255
    im = Image.fromarray(img.reshape(H, W, 4), "RGBA")
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return {"png": b.getvalue(), "bbox": (x0, y0, x1, y1), "px": [W, H], "water_share": round(float(wet.sum()) / max(int(ok.sum()), 1), 3)}


def world_width(crs) -> float:
    """The width of the whole world in this projection (from a 2 degree grid), to measure a jump against before the map's extent is known."""
    from pyproj import Transformer
    lon, lat = np.meshgrid(np.arange(-180, 180.01, 2.0), np.arange(-88, 88.01, 2.0))
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(lon.ravel(), lat.ravel())
    x, y = np.asarray(x), np.asarray(y)
    ok = np.isfinite(x) & np.isfinite(y)
    return float(max(np.ptp(x[ok]), np.ptp(y[ok]))) if ok.any() else 4.0e7

# ---------------------------------------------------------------------------------------------------------------- 4.1.0: projections with a far side
FAR_SIDE_PROJ = ("tpeqd", "ortho", "nsper", "aeqd", "gnom", "tpers")


def has_far_side(crs) -> bool:
    """4.1.0 (Day 24, D24-08): a projection whose far side folds back across the map (two-point equidistant, orthographic, azimuthal)."""
    try:
        from pyproj import CRS
        s = CRS.from_user_input(crs).to_proj4() or ""
    except Exception:  # noqa: BLE001
        s = str(crs or "")
    return any(f"+proj={p} " in s + " " for p in FAR_SIDE_PROJ)


def clip_far_side(gdf_wgs, crs, box, *, mode: str = "frame", share: float = 0.5, margin: float = 0.25):
    """Lines in lon/lat -> the layer in `crs`, densified at DENSIFY_DEG, split where a step jumps more than share x the frame's width
    (the far side folding back), and clipped to the frame grown by `margin` on each side. hemisphere also drops what lies past the horizon
    (non-finite points, which split_lines already drops). -> (GeoDataFrame, features changed)."""
    import shapely
    W, S, E, N = (float(v) for v in box)
    w, h = E - W, N - S
    g, cut = split_lines(gdf_wgs, crs, w, share=share)
    clip = (W - margin * w, S - margin * h, E + margin * w, N + margin * h)
    geoms = np.asarray(g.geometry.values, dtype=object)
    ok = np.array([x is not None and not x.is_empty for x in geoms])
    out = geoms.copy()
    if ok.any():
        out[ok] = shapely.clip_by_rect(geoms[ok], *clip)
    changed = int(sum(1 for a, b in zip(geoms, out) if a is not None and b is not None and not a.equals(b))) + cut
    g = g.copy()
    g["geometry"] = out
    keep = np.array([x is not None and not x.is_empty for x in out])
    return g[keep], changed
