#!/usr/bin/env python3
"""geo-inspect: say what is in a geodata file and flag problems before anyone builds on it (spec 7).

    geo_inspect.py FILE [--layer L] [--crs EPSG:n] [--thumbnail OUT.png] [--text]

Reports file and layer facts, CRS, geometry types, feature count, bounding box (native and WGS84), the attribute schema with null
and distinct counts, invalid and empty geometry counts, encoding, size, and the findings of the data and CRS lints. It also
proposes a display projection with the reason. The optional thumbnail is neutral grey and carries no text, so it never needs
a brand or a font; the styled map is made by the map skills.

Exit code 3 when any finding is a BLOCK (the data should not be mapped as it is).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import crs_engine as ce  # noqa: E402
import data_lint as dl  # noqa: E402
import geo_io  # noqa: E402
import gmap_common as gc  # noqa: E402

THUMB_MAX_FEATURES = 20_000
SAMPLE_VALUES = 3


def _schema(gdf) -> list[dict]:
    out = []
    geom = gdf.geometry.name
    for c in gdf.columns:
        if c == geom:
            continue
        s = gdf[c]
        nn = int(s.notna().sum())
        item = {"name": c, "type": str(s.dtype), "non_null": nn, "null": int(len(s) - nn)}
        try:
            item["distinct"] = int(s.nunique(dropna=True))
        except TypeError:
            item["distinct"] = None
        vals = s.dropna().astype(str).unique()[:SAMPLE_VALUES]
        item["sample"] = [v[:40] for v in vals]
        if s.dtype.kind in "iuf" and nn:
            item["min"], item["max"] = float(s.min()), float(s.max())
        out.append(item)
    return out


def inspect(path, layer=None, crs_assume=None, thumbnail=None, **read_kw) -> dict:
    p = Path(path)
    r = geo_io.read(p, layer=layer, crs_assume=crs_assume, **read_kw)
    g = r.gdf
    import shapely

    geoms = g.geometry
    types = geoms[~geoms.isna()].geom_type.value_counts().to_dict()
    rep: dict = {
        "file": {"name": p.name, "bytes": p.stat().st_size, "format": r.format, "layers": r.layers or [r.layer], "layer_read": r.layer,
                 "encoding": r.encoding},
        "features": int(len(g)),
        "geometry_types": {k: int(v) for k, v in types.items()},
        "vertices": dl.vertex_count(g),
        "empty_or_null_geometry": int((geoms.isna() | geoms.is_empty).sum()),
        "invalid_geometry": int((~shapely.is_valid(geoms.values) & ~geoms.isna().values).sum()),
        "crs": ce.describe(g) | {"assumed": r.crs_assumed},
        "attributes": _schema(g),
    }
    if len(g) and not any(map(lambda v: v != v, g.total_bounds)):
        rep["bbox_native"] = [float(v) for v in g.total_bounds]
        if g.crs is not None and not g.crs.equals("EPSG:4326"):
            rep["bbox_wgs84"] = [float(v) for v in g.to_crs("EPSG:4326").total_bounds]
        elif g.crs is not None:
            rep["bbox_wgs84"] = rep["bbox_native"]
    findings = dl.lint_data(g, assumed=r.crs_assumed)
    rep["findings"] = findings
    rep["io_notes"] = [n.as_dict() for n in r.notes]
    if g.crs is not None and len(g):
        try:
            choice = ce.choose_display(g, "thematic")
            rep["suggested_display_crs"] = {k: choice[k] for k in ("crs", "name", "rationale", "equal_area")}
        except Exception as e:                       # a suggestion must never stop a report
            rep["suggested_display_crs"] = {"error": str(e)[:100]}
    rep["verdict"] = ("do not map as is: " + "; ".join(f["message"] for f in findings if f["severity"] == "BLOCK")[:300]) \
        if any(f["severity"] == "BLOCK" for f in findings) else \
        ("usable, with warnings" if any(f["severity"] == "WARN" for f in findings) else "usable")
    if thumbnail:
        rep["thumbnail"] = make_thumbnail(g, thumbnail)
    return rep


def make_thumbnail(gdf, out) -> str | None:
    """A small neutral-grey preview, no text. Returns the path, or None when there is nothing to draw."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    g = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)]
    if g.empty:
        return None
    if len(g) > THUMB_MAX_FEATURES:
        g = g.sample(THUMB_MAX_FEATURES, random_state=0)
    if g.crs is not None and not g.crs.is_projected:
        try:
            g = g.to_crs(ce.choose_display(g, "reference")["crs"])
        except Exception:
            pass
    fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=100)
    kinds = set(g.geom_type)
    if kinds & {"Polygon", "MultiPolygon"}:
        g.plot(ax=ax, color="0.82", edgecolor="0.35", linewidth=0.4)
    elif kinds & {"LineString", "MultiLineString"}:
        g.plot(ax=ax, color="0.3", linewidth=0.6)
    else:
        g.plot(ax=ax, color="0.25", markersize=6)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(0.01, 0.01, 0.99, 0.99)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return str(out)


def as_text(rep: dict) -> str:
    f = rep["file"]
    lines = [f"{f['name']}  {rep['features']} features  {f['format']}  layer '{f['layer_read']}'"
             + (f"  ({len(f['layers'])} layers)" if len(f["layers"]) > 1 else "")]
    c = rep["crs"]
    lines.append(f"CRS: {c.get('authority') or c.get('name')} {'(assumed) ' if c.get('assumed') else ''}" if c.get("defined") else "CRS: none")
    lines.append("Geometry: " + ", ".join(f"{k} {v}" for k, v in rep["geometry_types"].items()) + f"; {rep['vertices']} vertices")
    if "bbox_wgs84" in rep:
        lines.append("Extent (WGS84): " + ", ".join(f"{v:.4f}" for v in rep["bbox_wgs84"]))
    lines.append("Attributes: " + ", ".join(f"{a['name']} ({a['type']}, {a['null']} null)" for a in rep["attributes"][:12])
                 + (" ..." if len(rep["attributes"]) > 12 else ""))
    for x in rep["findings"]:
        lines.append(f"{x['severity']:5} {x['id']}  {x['message']}")
    if "suggested_display_crs" in rep and "crs" in rep["suggested_display_crs"]:
        s = rep["suggested_display_crs"]
        lines.append(f"Suggested display CRS: {s['name']} ({s['crs'][:40]})")
    lines.append("Verdict: " + rep["verdict"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--layer")
    ap.add_argument("--crs", help="CRS to assume when the file has none")
    ap.add_argument("--thumbnail", help="write a neutral PNG preview here")
    ap.add_argument("--lat-col")
    ap.add_argument("--lon-col")
    ap.add_argument("--wkt-col")
    ap.add_argument("--text", action="store_true", help="plain-text summary instead of JSON")
    a = ap.parse_args(argv)
    try:
        rep = inspect(a.path, a.layer, a.crs, a.thumbnail, lat_col=a.lat_col, lon_col=a.lon_col, wkt_col=a.wkt_col)
    except geo_io.AmbiguousColumns as e:
        print(json.dumps({"error": str(e), "candidates": e.candidates}, indent=2))
        return gc.EXIT_USAGE
    except geo_io.GeoIOError as e:
        print(f"geo-inspect: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    print(as_text(rep) if a.text else json.dumps(rep, indent=2, default=str))
    return dl.worst(rep["findings"])


if __name__ == "__main__":
    sys.exit(main())
