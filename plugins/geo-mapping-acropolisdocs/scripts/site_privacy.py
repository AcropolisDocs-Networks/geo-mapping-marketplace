#!/usr/bin/env python3
"""site_privacy: coordinate precision reduction and seeded jitter for client-confidential point layers (spec 14, MAP-LIC-007).

    site_privacy.py apply IN OUT --method precision|jitter --meters 500 [--seed N]

A layer in a MapSpec asks for it with `privacy: {method: precision|jitter, meters: 500, seed: 7}`. The map, the data package and the methods note then
carry the reduced positions only; the true positions are not drawn or packaged. The treatment is disclosed in the methods note (a transformation step)
and on the layer's notes.

precision  each point is moved to the centre of a square grid cell of `meters` on a side (several sites can land on one spot)
jitter     each point is moved in a random direction by up to `meters` (uniform over the disc); the same seed gives the same result

Both work in a local azimuthal equidistant projection centred on the layer, so a metre is a metre. Neither is anonymisation: a determined reader with other
data can narrow a position, and below 100 m the treatment is not worth calling protection (MAP-TEL-001).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

METHODS = ("precision", "jitter")
MIN_USEFUL_M = 100.0
MAX_M = 50_000.0


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


class PrivacyError(ValueError):
    pass


def default_seed(layer_id: str) -> int:
    return int(hashlib.sha256(layer_id.encode()).hexdigest()[:8], 16)


def _local(gdf):
    from pyproj import CRS, Transformer
    g = gdf.to_crs("EPSG:4326")
    lon0 = float(g.geometry.x.mean())
    lat0 = float(g.geometry.y.mean())
    aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs")
    fwd = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    inv = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True)
    return g, fwd, inv


def reduce_points(gdf, method: str, meters: float, seed: int):
    """Return a copy of a point GeoDataFrame with reduced positions (same CRS as given)."""
    import geopandas as gpd
    import shapely
    if method not in METHODS:
        raise PrivacyError(f"privacy method '{method}' is unknown; use precision or jitter")
    if not (0 < meters <= MAX_M):
        raise PrivacyError(f"privacy meters must be between 0 and {int(MAX_M)}; got {meters}")
    if len(gdf) == 0:
        return gdf.copy()
    if gdf.crs is None:
        raise PrivacyError("the layer has no coordinate system; privacy reduction needs to know the units")
    if not all(gt == "Point" for gt in gdf.geometry.geom_type):
        raise PrivacyError("privacy reduction works on point layers only")
    g, fwd, inv = _local(gdf)
    x, y = fwd.transform(g.geometry.x.to_numpy(), g.geometry.y.to_numpy())
    x, y = np.asarray(x), np.asarray(y)
    if method == "precision":
        nx = (np.floor(x / meters) + 0.5) * meters
        ny = (np.floor(y / meters) + 0.5) * meters
    else:
        rng = np.random.default_rng(seed)
        ang = rng.uniform(0, 2 * np.pi, len(x))
        rad = meters * np.sqrt(rng.uniform(0, 1, len(x)))
        nx, ny = x + rad * np.cos(ang), y + rad * np.sin(ang)
    lon, lat = inv.transform(nx, ny)
    out = gdf.copy()
    pts = gpd.GeoSeries(shapely.points(np.asarray(lon), np.asarray(lat)), index=gdf.index, crs="EPSG:4326").to_crs(gdf.crs)
    out.geometry = pts
    return out


def disclosure(method: str, meters: float, seed: int | None = None) -> str:
    m = f"{meters:g} m"
    if method == "precision":
        return f"coordinates were reduced in precision: each site is drawn at the centre of a {m} grid square, so its true position is not shown"
    return f"coordinates were displaced at random by up to {m} (seeded, repeatable): drawn positions are not the true positions"


COORD_NAMES = {"lat", "latitude", "lon", "long", "lng", "longitude", "x", "y", "xcoord", "ycoord", "x_coord", "y_coord", "easting", "northing", "point_x", "point_y", "lat_dd", "lon_dd"}


def coordinate_columns(gdf) -> list[str]:
    """Attribute columns that carry the true position: a coordinate-like name holding numbers, or any numeric column equal to the points' x or y (in WGS84 or the layer CRS)."""
    import pandas as pd
    if len(gdf) == 0:
        return []
    g84 = gdf.to_crs("EPSG:4326") if gdf.crs else gdf
    refs = [g84.geometry.x.to_numpy(), g84.geometry.y.to_numpy(), gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()]
    out = []
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        num = pd.to_numeric(gdf[c], errors="coerce")
        if num.notna().sum() == 0:
            continue
        named = str(c).strip().lower() in COORD_NAMES
        same = any(np.allclose(num.to_numpy(dtype="float64", na_value=np.nan), r, atol=1e-6, equal_nan=False) for r in refs if len(r) == len(num)) if num.notna().all() else False
        if named or same:
            out.append(c)
    return out


def apply(gdf, spec: dict | None, layer_id: str):
    """(gdf, steps, notes, findings, record) for a layer's `privacy:` block; no block returns the layer unchanged."""
    if not spec:
        return gdf, [], [], [], None
    method = str(spec.get("method") or "precision")
    try:
        meters = float(spec.get("meters"))
    except (TypeError, ValueError):
        raise PrivacyError(f"layer '{layer_id}': privacy needs meters, for example 500")
    seed = int(spec["seed"]) if spec.get("seed") is not None else default_seed(layer_id)
    fs = []
    if meters < MIN_USEFUL_M:
        fs.append(finding("MAP-TEL-001", "WARN", f"layer '{layer_id}': a {meters:g} m {method} is too small to protect a site; use at least {int(MIN_USEFUL_M)} m", meters))
    out = reduce_points(gdf, method, meters, seed)
    dropped = coordinate_columns(gdf)
    if dropped:
        out = out.drop(columns=dropped)                  # a lat or lon column would carry the true position past the reduced geometry
    text = disclosure(method, meters, seed)
    rec = {"method": method, "meters": meters, "seed": seed if method == "jitter" else None, "points": int(len(out))}
    notes = [text[0].upper() + text[1:] + "."]
    if dropped:
        fs.append(finding("MAP-TEL-004", "INFO", f"layer '{layer_id}': column(s) {', '.join(map(str, dropped))} held the true coordinates and were removed from the privacy-treated layer", dropped))
        rec["dropped_columns"] = [str(c) for c in dropped]
    return out, [text], notes, fs, rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["apply"])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--method", choices=METHODS, default="precision")
    ap.add_argument("--meters", type=float, required=True)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--layer")
    ap.add_argument("--x")
    ap.add_argument("--y")
    ap.add_argument("--crs-assume")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    import geo_io
    r = geo_io.read(a.src, layer=a.layer, lat_col=a.y, lon_col=a.x, crs_assume=a.crs_assume)
    try:
        out, steps, notes, fs, rec = apply(r.gdf, {"method": a.method, "meters": a.meters, "seed": a.seed}, Path(a.src).stem)
    except PrivacyError as ex:
        print(f"site_privacy: {ex}", file=sys.stderr)
        return 2
    geo_io.write(out, a.dst, overwrite=a.overwrite)
    print(json.dumps({"written": a.dst, "record": rec, "disclosure": steps, "findings": fs}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
