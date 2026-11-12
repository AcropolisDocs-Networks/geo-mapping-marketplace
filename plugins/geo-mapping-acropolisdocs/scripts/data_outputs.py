#!/usr/bin/env python3
"""data_outputs: the styled data outputs of a map's data package (M14): a vector tile set (PMTiles or MBTiles), a styled KMZ for Google
Earth, a GeoPackage that opens styled in QGIS (layer_styles) and a QGIS project (.qgz), built from the map's own layers and styling.

The map-build pipeline (and the telecom map pipelines that share its data-package stage) calls `write_package` after its licence and
confidentiality gates (MAP-LIC-003, MAP-LIC-007) have passed each layer, with only the layers that passed; it never writes a layer the gates
refused, and it writes nothing when any layer was refused. Ask for them with `data_package=true` and `data_formats=pmtiles,kmz,gpkg,qgz` (or
the same names in `formats`).

    data/<map id>.pmtiles   (or .mbtiles)  every layer as a source layer of its own, zooms from the data (tiles_out)
    data/<map id>.kmz       one folder per layer, class styles, point icons inside, a legend overlay (kml_style)
    data/<map id>.gpkg      every layer as a table, its default QGIS style in layer_styles (gpkg_style)
    data/<map id>.qgz       the layers in draw order from that GeoPackage, styled, in the map's CRS and extent (qgis_project); a raster
                            layer's window (data/<layer id>.tif, written by the pipeline when its licence allows, M16) under them, coloured as on the map
A map whose id is also one of its layer ids gets `<map id>-map.*`, so the per-layer files beside them are never overwritten.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import style_dict as sd  # noqa: E402

DATA_FORMATS = ("pmtiles", "mbtiles", "kmz", "gpkg", "qgz")


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def requested(options: dict) -> tuple[list[str], list[str]]:
    """(the styled data formats asked for, unknown names in data_formats). `formats` may name them too, beside the map formats."""
    asked = [str(f).strip().lower() for f in (options.get("data_formats") or []) if str(f).strip()]
    unknown = [f for f in asked if f not in DATA_FORMATS]
    asked = [f for f in asked if f in DATA_FORMATS] + [str(f).lower() for f in (options.get("formats") or []) if str(f).lower() in DATA_FORMATS]
    return list(dict.fromkeys(asked)), unknown


def map_title(plan) -> str:
    import text_lint                                          # map_plan put the lints on the path
    return text_lint.title_text(plan.spec)


def _attribution(l) -> str:
    import provenance
    p = l.provenance or {}
    a = p.get("attribution") or provenance.withheld(p.get("source"))
    return str(a or "").strip().rstrip(".")


def raster_window(plan, l, path) -> dict:
    """M16: a `role: raster` layer's window as the map read it (the same file, band, resampling, window and pixel size, in the map's CRS),
    written as a GeoTIFF at `path` with its nodata kept; and the QGIS colouring that matches the map (qgis_project.raster_style).
    Returns {'path', 'style', 'shape', 'crs'}. A service picture is not data and is refused (ValueError)."""
    import numpy as np
    import map_raster
    import qgis_project as qp
    import raster_io as rio
    info = l.raster or {}
    im = info.get("image") or {}
    if info.get("src") == "service" or not info.get("path") or not im:
        raise ValueError(f"layer '{l.id}' is a picture drawn by a service, not values that can be handed on")
    cfg = map_raster.raster_cfg(l.spec)
    g = rio.read_window(info["path"], tuple(im["bbox"]), plan.crs, out_shape=tuple(im["shape"]), band=cfg["band"], resampling=cfg["resampling"],
                        nodata=cfg["nodata"])
    a = np.ma.masked_invalid(np.ma.asarray(g.array, dtype="float32"))
    if cfg["nodata"] is not None:
        a = np.ma.masked_where(np.ma.filled(a, np.nan) == float(cfg["nodata"]), a)
    nod = float(cfg["nodata"]) if cfg["nodata"] is not None else -9999.0
    rio.write_geotiff(path, np.ma.filled(a, nod).astype("float32"), g.transform, plan.crs, nodata=nod)
    lb = l.legend_block or {}
    colors = list(l.colors or [])
    if lb.get("kind") == "ramp":
        vals = [float(it["value"]) for it in lb["items"] if it.get("value") is not None]
        st = qp.raster_style("ramp", lb.get("stops") or colors, [vals[0], vals[-1]], opacity=float(lb.get("opacity", 1.0)))
        st["labels"] = [map_raster._fmt(v) for v in st["values"]]
        st["labels"][0], st["labels"][-1] = str(lb["items"][0]["label"]), str(lb["items"][-1]["label"])     # "or more" as on the map's legend
    elif lb.get("kind") == "classes" and cfg["classes"]:
        breaks = sorted(float(x) for x in cfg["classes"])
        st = qp.raster_style("classes", colors, breaks + [float("inf")], [it["label"] for it in lb["items"]], float(cfg["opacity"]))
    else:
        vals = [float(v) for v in np.unique(a.compressed())]
        st = qp.raster_style("categories", colors[:len(vals)], vals, [it["label"] for it in (lb.get("items") or [])][:len(vals)] or None, float(cfg["opacity"]))
    return {"path": str(path), "style": st, "shape": [int(a.shape[0]), int(a.shape[1])], "crs": plan.crs}


def write_package(plan, layers: list[tuple], ddir, formats: list[str], *, options: dict | None = None, rasters: list | None = None) -> dict:
    """Write the styled outputs for `layers` ([(LayerPlan, GeoDataFrame in EPSG:4326)], draw order) into `ddir`. Returns {'files': [(path,
    role)], 'findings', 'notes'}. A writer that refuses gives a BLOCK finding (MAP-OUT-009 for tiles, MAP-OUT-010 otherwise), not an exception.
    `rasters` ([(LayerPlan, raster_window result)], M16) go into the QGIS project as raster layers under the vector layers, as on the map."""
    import gpkg_style as gs
    import kml_style as ks
    import qgis_project as qp
    import tiles_out as to
    options = options or {}
    ddir = Path(ddir)
    files: list[tuple[str, str]] = []
    fs: list[dict] = []
    notes: list[str] = []
    ids = {l.id for l, _ in layers}
    mid = str(plan.spec["id"])
    stem = mid if mid not in ids else f"{mid}-map"
    title = map_title(plan)
    message = str(plan.spec.get("message") or "")
    labels = {lb.get("layer"): lb for lb in (plan.spec.get("labels") or []) if isinstance(lb, dict)}
    items = []
    for l, g in layers:
        lb = labels.get(l.id)
        st = sd.style_from_layerplan(l, plan.tokens, label=lb)
        if st.get("label"):
            st["label"].update(color=plan.tokens.get("label"), halo=plan.tokens.get("label_halo") or plan.tokens.get("background"))
        try:
            st = sd.resolve(st, g, plan.tokens)
        except sd.StyleError as ex:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"layer '{l.id}': its style could not be carried into the data outputs: {ex}"))
            return {"files": files, "findings": fs, "notes": notes}
        items.append((l, g, st))
    attribution = "; ".join(dict.fromkeys(a for a in (_attribution(l) for l, _, _ in items) if a))

    gpkg_path = ddir / f"{stem}.gpkg"
    if items and ("gpkg" in formats or "qgz" in formats):
        try:
            rep = gs.write_styled([(l.id, g, st) for l, g, st in items], gpkg_path, tokens=plan.tokens)
            files.append((str(gpkg_path), "data"))
            fs += gs.check(gpkg_path, {name: v["renderer"] for name, v in rep["layers"].items()})
            if "gpkg" not in formats:
                notes.append(f"{gpkg_path.name} was written for the QGIS project, which reads its layers from it")
        except (gs.GpkgStyleError, sd.StyleError, OSError) as ex:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"the styled GeoPackage could not be written: {ex}"))
    if "qgz" in formats and (gpkg_path.is_file() or (not items and rasters)):
        q = ddir / f"{stem}.qgz"
        try:
            rl = [{"raster": rw["path"], "name": l.title, "style": rw["style"]} for l, rw in (rasters or [])]
            rep = qp.write_qgz(q, rl + [(l.id, gpkg_path, st, l.title) for l, _, st in items], crs=plan.crs, extent=plan.extent, title=title,
                               abstract=message, keywords=[plan.brand_name], tokens=plan.tokens)
            files.append((str(q), "data"))
            info = qp.check_qgz(q, expect_layers=len(items) + len(rl), expect_crs=rep["crs"])
            fs += info["findings"]
        except (qp.QgisProjectError, sd.StyleError, OSError) as ex:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"the QGIS project could not be written: {ex}"))
    if "kmz" in formats and items:
        k = ddir / f"{stem}.kmz"
        try:
            rep = ks.write_layers([(l.id, g, st) for l, g, st in items], k, title=title, tokens=plan.tokens, legend=True)
            files.append((str(k), "data"))
            notes += rep["notes"]
            fs += ks.check(k, {name: v["features"] for name, v in rep["layers"].items()}, legend=True)
        except (ks.KmlStyleError, sd.StyleError, OSError) as ex:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"the styled KMZ could not be written: {ex}"))
    for fmt in ("pmtiles", "mbtiles"):
        if fmt not in formats or not items:
            continue
        t = ddir / f"{stem}.{fmt}"
        try:
            rep = to.write_tiles(fmt, {l.id: g for l, g, _ in items}, t, minzoom=options.get("minzoom", 0), maxzoom=options.get("maxzoom", "auto"),
                                 attribution=attribution or None, name=title, description=message or title,
                                 layer_descriptions={l.id: l.title for l, _, _ in items})
            files += [(p, "data") for p in rep["paths"]]
            fs += rep["findings"]
            notes += rep["notes"]
        except to.TilesError as ex:
            fs.append(finding("MAP-OUT-009", "BLOCK", f"the {fmt} tile set could not be written: {ex}"))
    return {"files": files, "findings": fs, "notes": notes}
