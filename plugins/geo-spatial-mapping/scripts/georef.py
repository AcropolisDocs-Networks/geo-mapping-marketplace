#!/usr/bin/env python3
"""georef: GeoPDF and GeoTIFF for the map window (spec 7.2, I.17).

    python3 georef.py pdf  --pdf map.pdf --window WINDOW.json --out map_geo.pdf
    python3 georef.py tiff --png map.png --window WINDOW.json --out map_geo.tif [--dpi 300]

build_map.py writes these itself with `--formats geopdf,geotiff` (files `<stem>_geo.pdf`, `<stem>_geo.tif`). WINDOW.json is a build result
(its report.map_window) or a QGIS kit layout.json. The window is the part of the sheet that shows the map and the ground coordinates of its edges.

GeoPDF: an ISO 32000-1 viewport (/VP with /Measure /GEO: four corners in latitude and longitude, the same four in the viewport's 0..1 box, the
coordinate system as WKT). Title, legend and footer stay outside the viewport. GeoTIFF: the map window cropped from the PNG, with
ModelPixelScale, ModelTiepoint and an EPSG GeoKey directory (a CRS without an EPSG code is refused).

Positions are exact at the window's edges and as accurate as one map pixel inside it; the drawing is simplified to sheet resolution and symbols are
larger than the features they mark. Client-confidential layers are refused (MAP-GEO-001): a georeferenced picture hands any GIS the positions. No network.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MM_PER_IN = 25.4
GEO_FORMATS = ("geopdf", "geotiff")


class GeorefError(Exception):
    pass


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def load_window(path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    for src in (d.get("report") or {}, d):
        if "map_window" in src:
            w = src["map_window"]
            return {"crs": w["crs"], "extent": w["extent"], "rect_mm": w["rect_mm"], "page_mm": w["page_mm"]}
    if "crs_wkt" in d and "map" in d:                                   # a QGIS kit
        m = d["map"]
        return {"crs": d["crs_wkt"], "extent": d["extent"], "rect_mm": [m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"]], "page_mm": d["page_mm"]}
    raise GeorefError("the file holds no map window (expected report.map_window or a QGIS kit layout.json)")


def _crs(w: dict):
    from pyproj import CRS
    crs = CRS.from_user_input(w["crs"])
    x0, y0, x1, y1 = w["extent"]
    if not (x1 > x0 and y1 > y0):
        raise GeorefError("the window extent is empty")
    return crs


def pixel_size_m(w: dict, dpi: float) -> float:
    """Ground metres under one pixel of the map window at this DPI (the accuracy statement)."""
    from pyproj import CRS
    crs = CRS.from_user_input(w["crs"])
    x0, y0, x1, y1 = w["extent"]
    rx0, _, rx1, _ = w["rect_mm"]
    px = (rx1 - rx0) * dpi / MM_PER_IN
    if crs.is_geographic:
        import math
        cy = (y0 + y1) / 2
        return (x1 - x0) * 111_320.0 * math.cos(math.radians(cy)) / px
    fac = crs.axis_info[0].unit_conversion_factor if crs.axis_info else 1.0
    return (x1 - x0) * fac / px


def pdf_geo_points(w: dict) -> dict:
    """Corner points of the map window: PDF user-space box, GPTS (lat, lon) and LPTS (0..1 in the box), TL, BL, BR, TR."""
    from pyproj import Transformer
    crs = _crs(w)
    page_w, page_h = w["page_mm"]
    rx0, ry0, rx1, ry1 = w["rect_mm"]
    k = 72.0 / MM_PER_IN
    bbox = [rx0 * k, (page_h - ry1) * k, rx1 * k, (page_h - ry0) * k]
    ex0, ey0, ex1, ey1 = w["extent"]
    tr = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)
    gpts: list[float] = []
    for x, y in ((ex0, ey1), (ex0, ey0), (ex1, ey0), (ex1, ey1)):
        lon, lat = tr.transform(x, y)
        gpts += [lat, lon]
    return {"bbox": bbox, "gpts": gpts, "lpts": [0, 1, 0, 0, 1, 0, 1, 1], "crs": crs}


def geopdf_bytes(pdf: bytes, w: dict) -> bytes:
    import pikepdf
    from pyproj.enums import WktVersion
    g = pdf_geo_points(w)
    crs = g["crs"]
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        page = doc.pages[0]
        gcs = pikepdf.Dictionary(Type=pikepdf.Name.PROJCS if crs.is_projected else pikepdf.Name.GEOGCS, WKT=pikepdf.String(crs.to_wkt(WktVersion.WKT1_GDAL)))
        measure = pikepdf.Dictionary(Type=pikepdf.Name.Measure, Subtype=pikepdf.Name.GEO, GPTS=pikepdf.Array([float(v) for v in g["gpts"]]),
                                     LPTS=pikepdf.Array([float(v) for v in g["lpts"]]), GCS=gcs)
        vp = doc.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Viewport, BBox=pikepdf.Array([float(v) for v in g["bbox"]]),
                                                  Name=pikepdf.String("Map"), Measure=doc.make_indirect(measure)))
        page["/VP"] = pikepdf.Array([vp])
        out = io.BytesIO()
        doc.save(out, deterministic_id=True)
    return out.getvalue()


def geotiff_bytes(png: bytes, w: dict, dpi: float, meta: dict | None = None) -> tuple[bytes, dict]:
    """The map window cropped from the sheet PNG, georeferenced. `meta` (title, author, software, items) goes into hidden tags."""
    from PIL import Image, TiffImagePlugin
    crs = _crs(w)
    epsg = crs.to_epsg()
    if epsg is None:
        raise GeorefError("GeoTIFF needs a coordinate system with an EPSG code; this one has none (use the GeoPDF, or reproject first)")
    rx0, ry0, rx1, ry1 = w["rect_mm"]
    s = dpi / MM_PER_IN
    im = Image.open(io.BytesIO(png)).convert("RGB").crop((round(rx0 * s), round(ry0 * s), round(rx1 * s), round(ry1 * s)))
    ex0, ey0, ex1, ey1 = w["extent"]
    sx, sy = (ex1 - ex0) / im.width, (ey1 - ey0) / im.height
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[33550] = (sx, sy, 0.0)
    ifd.tagtype[33550] = 12
    ifd[33922] = (0.0, 0.0, 0.0, ex0, ey1, 0.0)
    ifd.tagtype[33922] = 12
    keys = [(1024, 0, 1, 1 if crs.is_projected else 2), (1025, 0, 1, 1), ((3072 if crs.is_projected else 2048), 0, 1, int(epsg))]
    ifd[34735] = tuple([1, 1, 0, len(keys)] + [v for k in keys for v in k])
    ifd.tagtype[34735] = 3
    meta = meta or {}
    if meta.get("title"):
        ifd[270] = str(meta["title"])
    if meta.get("author"):
        ifd[315] = str(meta["author"])
    if meta.get("software"):
        ifd[305] = str(meta["software"])
    if meta.get("items"):
        from xml.sax.saxutils import quoteattr, escape
        ifd[42112] = "<GDALMetadata>" + "".join(f"<Item name={quoteattr(k.upper())}>{escape(str(v))}</Item>" for k, v in meta["items"].items()) + "</GDALMetadata>"
    out = io.BytesIO()
    im.save(out, "TIFF", tiffinfo=ifd, dpi=(dpi, dpi), compression="tiff_lzw")
    return out.getvalue(), {"size_px": [im.width, im.height], "pixel_size": [sx, sy], "epsg": int(epsg)}


# ---------------------------------------------------------------------------------------------------------------- guards and checks
def guard(plan, report: dict, formats: list[str], dpi: float) -> tuple[list[dict], set[str]]:
    """MAP-GEO-001..005 for a plan. Returns (findings, formats refused)."""
    wanted = [f for f in formats if f in GEO_FORMATS]
    if not wanted:
        return [], set()
    out: list[dict] = []
    refused: set[str] = set()
    conf = [l.id for l in plan.layers if l.confidentiality == "client-confidential"]
    opt = ((plan.spec.get("export") or {}).get("georeference") or {})
    if conf:
        if opt.get("allow_confidential") is True and str(opt.get("reason") or "").strip():
            out.append(finding("MAP-GEO-001", "WARN", f"georeferenced output of client-confidential layer(s) {', '.join(conf)} was allowed by the MapSpec: {str(opt['reason']).strip()[:120]}"))
        else:
            out.append(finding("MAP-GEO-001", "BLOCK", f"layer(s) {', '.join(conf)} are client-confidential, so a georeferenced file is refused: it gives any GIS the positions. "
                                                      "Use the plain PDF or PNG, or set export.georeference.allow_confidential: true with a reason"))
            refused |= set(wanted)
    if any(b.get("kind") == "tiles" for b in plan.basemap):
        out.append(finding("MAP-GEO-002", "BLOCK", "the map draws a tile basemap, and a georeferenced file would let the provider's imagery travel as data; use the bundled basemap or the plain PDF or PNG"))
        refused |= set(wanted)
    w = (report or {}).get("map_window")
    if not w:
        out.append(finding("MAP-GEO-005", "BLOCK", "the render report has no map window, so the file cannot be georeferenced"))
        refused |= set(wanted)
        return out, refused
    if "geotiff" in wanted:
        try:
            from pyproj import CRS
            if CRS.from_user_input(w["crs"]).to_epsg() is None:
                out.append(finding("MAP-GEO-004", "BLOCK", "GeoTIFF needs a coordinate system with an EPSG code; this map's has none. The GeoPDF carries any coordinate system"))
                refused.add("geotiff")
        except Exception as ex:  # noqa: BLE001
            out.append(finding("MAP-GEO-004", "BLOCK", f"the coordinate system could not be read ({type(ex).__name__})"))
            refused |= set(wanted)
    if set(wanted) - refused:
        px = pixel_size_m(w, dpi)
        out.append(finding("MAP-GEO-003", "INFO", f"positions are exact at the window's edges and about {px:,.0f} m per pixel inside it at {dpi:g} dpi; symbols are larger than the features they mark. Not a survey product"))
    return out, refused


def verify_pdf(raw: bytes, w: dict) -> list[dict]:
    import pikepdf
    out: list[dict] = []
    try:
        with pikepdf.open(io.BytesIO(raw)) as doc:
            vp = doc.pages[0].get("/VP")
            m = vp[0].get("/Measure") if vp else None
            ok = bool(m) and str(m.get("/Subtype")) == "/GEO" and len(m.get("/GPTS", [])) == 8 and len(m.get("/LPTS", [])) == 8 and "/WKT" in m.get("/GCS", {})
    except Exception as ex:  # noqa: BLE001
        return [finding("MAP-GEO-010", "BLOCK", f"the GeoPDF could not be read back ({type(ex).__name__})")]
    if not ok:
        out.append(finding("MAP-GEO-010", "BLOCK", "the GeoPDF has no complete georeferenced viewport"))
    return out


def verify_tiff(raw: bytes, w: dict) -> list[dict]:
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        tags = im.tag_v2
        scale, tie = tags.get(33550), tags.get(33922)
        ok = bool(scale) and bool(tie) and 34735 in tags and abs(tie[3] - w["extent"][0]) < 0.01 and abs(tie[4] - w["extent"][3]) < 0.01
    except Exception as ex:  # noqa: BLE001
        return [finding("MAP-GEO-011", "BLOCK", f"the GeoTIFF could not be read back ({type(ex).__name__})")]
    return [] if ok else [finding("MAP-GEO-011", "BLOCK", "the GeoTIFF's tiepoint or tags do not match the map window")]


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="georef", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("pdf")
    p.add_argument("--pdf", required=True)
    p.add_argument("--window", required=True)
    p.add_argument("--out", required=True)
    t = sub.add_parser("tiff")
    t.add_argument("--png", required=True)
    t.add_argument("--window", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--dpi", type=float, default=300.0)
    a = ap.parse_args(argv)
    try:
        w = load_window(a.window)
        if a.op == "pdf":
            raw = geopdf_bytes(Path(a.pdf).read_bytes(), w)
            info = {"bbox_pt": pdf_geo_points(w)["bbox"]}
        else:
            raw, info = geotiff_bytes(Path(a.png).read_bytes(), w, a.dpi)
        Path(a.out).write_bytes(raw)
    except GeorefError as ex:
        print(f"georef: {ex}", file=sys.stderr)
        return 3
    print(json.dumps({"out": a.out, **info}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
