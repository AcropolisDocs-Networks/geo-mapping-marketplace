#!/usr/bin/env python3
"""qgis-layout: an optional path that lays the same map out in QGIS and exports it from there (spec I.16). EXPERIMENTAL.

    qgis_layout.py probe                                      is PyQGIS usable here, and which Python has it
    qgis_layout.py kit  --spec map.yaml --out DIR             write the kit only (layout.json, layers.gpkg, the build script)
    qgis_layout.py run  --spec map.yaml --out DIR [--formats pdf,png,svg] [--dpi 300]

Why: some teams finish maps in QGIS. The kit is the plugin's decisions written down - classes, colors, extent, text, sizes,
sources, the confidentiality banner - and a fixed PyQGIS script lays them out, so the QGIS map and the plugin's own SVG map match.
`run` also leaves layout.qgz, a project with the layout in it, that opens in QGIS for further editing.

What it does not do: it never draws web basemap tiles (no network), never reads credentials, and never changes the MapSpec.
The legend, scale bar and north arrow are QGIS items placed in the three least-covered corners; check for overlap. After every
run the files are verified (page size, Inter embedded, dimensions) and the hidden accreditation is stamped. PNG and SVG outputs
from this path are labeled experimental in qgis_report.json, and the plugin's own renderer remains the supported path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import geo_locale as gl  # noqa: E402

PT = 25.4 / 72
BUILD_SCRIPT = HERE / "qgis_build_layout.py"
FORBIDDEN_IN_BUILD = ("socket", "urllib", "requests", "http.client", "subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__", "ftplib", "smtplib")
PY_CANDIDATES = ("python3.12", "python3.11", "python3.13", "python3.10", "python3", "python")


class QgisError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------------------------------------------- probe
def _try_python(py: str) -> dict | None:
    code = ("import os;os.environ.setdefault('QT_QPA_PLATFORM','offscreen');"
            "from qgis.core import Qgis;print(Qgis.version())")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=90, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    ver = (r.stdout.strip().splitlines() or [""])[-1]
    return {"python": py, "version": ver}


def probe() -> dict:
    """Find a Python that can import PyQGIS. GEO_MAPPING_QGIS_PYTHON wins; then this interpreter; then the usual system names."""
    tried: list[str] = []
    cands: list[str] = []
    env = os.environ.get("GEO_MAPPING_QGIS_PYTHON")
    if env:
        cands.append(env)
    cands.append(sys.executable)
    for n in PY_CANDIDATES:
        for base in (shutil.which(n), f"/usr/bin/{n}"):
            if base and base not in cands and Path(base).exists():
                cands.append(base)
    for py in cands:
        tried.append(py)
        found = _try_python(py)
        if found:
            ver = found["version"]
            major_minor = tuple(int(x) for x in re.findall(r"\d+", ver)[:2]) if re.findall(r"\d+", ver) else (0, 0)
            ok = major_minor >= (3, 28)
            return {"available": ok, "python": py, "qgis": ver, "supported": ok,
                    "note": "" if ok else f"QGIS {ver} is older than 3.28, the oldest this path is written for",
                    "tried": tried}
    return {"available": False, "python": None, "qgis": None, "supported": False, "tried": tried,
            "note": "PyQGIS was not found. Install QGIS 3.28 or later (Debian and Ubuntu: python3-qgis) and, if its Python is not the default, "
                    "set GEO_MAPPING_QGIS_PYTHON to that interpreter. The plugin's own renderer still produces the map."}


def scan_build_script(path: Path | None = None) -> list[str]:
    """Static check that the fixed PyQGIS script has no network, shell or dynamic-code calls. Empty means clean."""
    text = (path or BUILD_SCRIPT).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    return [tok for tok in FORBIDDEN_IN_BUILD if tok in code]


# ---------------------------------------------------------------------------------------------------------------- kit
def _safe(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")[:40] or "layer"


def _wrap_fit(lines: list[str], width: float, pt: float, floor: float, height: float):
    import textmeasure as tm
    while True:
        n = 0
        for ln in lines:
            n += len(tm.wrap(ln, width, pt, 400))
        need = n * tm.line_height_mm(pt)
        if need <= height + 0.2 or pt <= floor:
            return pt, need
        pt = round(pt - 0.5, 2)


def build_kit(spec_path, out_dir, *, brand: str | None = None, template: str | None = None) -> dict:
    """Write the kit for a MapSpec. Returns {'dir', 'layout', 'layers', 'notes'}; nothing is rendered."""
    import geopandas as gpd
    import numpy as np
    from pyproj import CRS
    import layout as lo
    import map_plan as mp
    import map_render as mr
    import provenance
    import scalebar as sbm
    from label_engine import Rect

    plan = mp.prepare(spec_path, mode="print", brand=brand, template=template, use_tiles=False, classic_layout=True)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec, tpl, t = plan.spec, plan.tpl, plan.tokens
    W, H = tpl["size_mm"]
    fonts = dict(tpl["fonts"])
    els = spec.get("elements") or {}
    notes: list[str] = ["web basemap tiles are not drawn in the QGIS path"]

    def on(name, default=True):
        return (els.get(name) or {}).get("on", default)

    map_rect = lo.rect_of(tpl["map"])
    pad_pct = float((spec.get("extent") or {}).get("padding_pct", 5))
    conf = [l.id for l in plan.layers if l.confidentiality == "client-confidential"]
    banner_on = on("confidentiality_banner", "auto")
    banner_on = bool(conf) if banner_on == "auto" else bool(banner_on)
    banner_h = 6.0 if banner_on else 0.0

    # ---- layers: geometry, the value field and the class index only
    crs_obj = CRS.from_user_input(plan.crs)
    gp = out / "layers.gpkg"
    if gp.exists():
        gp.unlink()
    layers_json: list[dict] = []
    data_geoms: list = []
    label_cfg = {lb["layer"]: lb for lb in (plan.spec.get("labels") or [])}
    for i, l in enumerate(plan.layers):
        if l.kind == "raster":               # a raster picture is not a vector table; the QGIS project does not carry it yet (M12, TODO)
            continue
        gdf = l.gdf
        if gdf.crs is not None and CRS.from_user_input(gdf.crs) != crs_obj:
            gdf = gdf.to_crs(crs_obj)
        n = len(gdf)
        cols = {}
        if l.cls is not None and len(np.asarray(l.cls.assigned)) == n:
            cols["_cls"] = np.asarray(l.cls.assigned, dtype="int64")
        vf = l.value_field if (l.value_field and l.value_field in gdf.columns) else None
        if vf:
            cols[vf] = gdf[vf].to_numpy()
        lb = label_cfg.get(l.id)
        if lb and lb.get("field") in gdf.columns:
            cols[lb["field"]] = gdf[lb["field"]].astype(str).to_numpy()
        slim = gpd.GeoDataFrame(cols, geometry=gdf.geometry.values, crs=gdf.crs if gdf.crs is not None else crs_obj)
        keep = (~slim.geometry.is_empty) & slim.geometry.notna()
        slim = slim[keep.to_numpy()]
        table = f"L{i}_{_safe(l.id)}"
        slim.to_file(gp, layer=table, driver="GPKG")
        if l.role not in ("context", "labels"):
            data_geoms.extend(list(slim.geometry.values))
        L: dict = {"id": l.id, "table": table, "title": l.title, "kind": l.kind, "role": l.role, "rows": int(len(slim)),
                   "fill": l.fill, "stroke": l.stroke, "stroke_w_mm": round(float(l.stroke_w_mm), 4), "opacity": float(l.fill_opacity),
                   "shape": str(l.symbol.get("shape") or "circle"), "size_mm": round(float(l.symbol.get("size", 3)) * 0.3528, 4)}
        if l.cls is not None and "_cls" in cols:
            shapes = l.class_shapes or [L["shape"]] * len(l.colors)
            L["classes"] = [{"label": lab, "color": col, "shape": shp} for lab, col, shp in zip(l.cls.labels, l.colors, shapes)]
            if int((np.asarray(l.cls.assigned) < 0).sum()):
                L["nodata"] = {"label": "No data", "color": t["no_data"], "shape": L["shape"]}
            L["fill"] = None
        if l.role == "proportional" and vf:
            vals = gdf[vf].to_numpy(dtype="float64", na_value=np.nan)
            vals = vals[np.isfinite(vals) & (vals > 0)]
            if len(vals):
                dmax = float(l.symbol.get("max_size", 14)) * 0.3528
                L["size_expr"] = f'max({mr.MIN_SYMBOL_MM}, {dmax:.4f} * sqrt("{vf}" / {float(vals.max()):.6g}))'
        if lb and lb.get("field") in cols:
            L["label"] = {"field": lb["field"], "size_pt": float(lb.get("size", fonts["label"]))}
        layers_json.append(L)

    # ---- overlays go to the least-covered corners (the same occupancy test the SVG path uses); when the legend would cover data, the data
    #      is fitted with a strip kept free beside it, as the SVG path does
    legend_ids = [l.id for l in plan.layers if l.role not in ("context", "labels", "contour") and l.kind != "raster" and (l.spec.get("legend") or {}).get("include", True) is not False
                  and (l.cls is not None or l.role == "proportional")]
    # 1.0.0 (TODO M15, M16): a bivariate layer's key is its n x n grid (QGIS shapes and labels), not QGIS's list of nine rules
    biv = next((l for l in plan.layers if l.id in legend_ids and (l.legend_block or {}).get("kind") == "bivariate" and (l.legend_block or {}).get("cells")), None)
    if biv is not None:
        legend_ids = [i for i in legend_ids if i != biv.id]
    BV_CELL = 6.0
    bv_wh = ((int(biv.legend_block["n"]) * BV_CELL + 24.0), (int(biv.legend_block["n"]) * BV_CELL + 16.0)) if biv is not None else (0.0, 0.0)
    lmax = tpl["legend_max"]
    na_size = max(9.0, min(map_rect.w, map_rect.h) * 0.075)
    leg_wh = (min(lmax["w"], map_rect.w * 0.3), min(lmax["h"], map_rect.h * 0.85))
    corners = ["lower-left", "lower-right", "upper-left", "upper-right"]
    inset = lo.CORNER_INSET_MM

    def attempt(reserve: tuple) -> dict:
        fr_ = lo.Frame.fit(map_rect, plan.extent, pad_pct, (reserve[0], reserve[1] + banner_h, reserve[2], reserve[3]))
        vis_ = fr_.visible
        occ_ = lo.occupancy(fr_, data_geoms) if data_geoms else None
        info_ = sbm.scale_info(plan.crs, vis_, map_rect.w, units=(els.get("scale_bar") or {}).get("units", "auto"))
        bar_ = info_.get("bar") if on("scale_bar") else None
        if bar_ and not bar_.get("segments"):
            bar_ = None
        sizes_ = {"legend": leg_wh, "scale": (((bar_["length_mm"] + 14.0) if bar_ else 0.0), 9.0), "north": (na_size, na_size + 1.0), "bivariate_key": bv_wh}
        north_ = bool(on("north_arrow")) and bool((info_.get("north") or {}).get("show", True))
        want_ = [k for k, v in (("legend", bool(legend_ids) and on("legend")), ("bivariate_key", biv is not None and on("legend")), ("scale", bool(bar_)),
                                ("north", north_)) if v]
        taken_: set[str] = set()
        placed_: dict = {}
        fr_rect = Rect(map_rect.x0, map_rect.y0 + banner_h, map_rect.x1, map_rect.y1)
        for key in want_:
            w, h = sizes_[key]
            best = None
            for c in corners:
                if c in taken_:
                    continue
                r = lo.corner_rect(fr_rect, c, w, h)
                cov = lo.cover(fr_, occ_, r) if occ_ is not None else 0.0
                if best is None or cov < best[0]:
                    best = (cov, c, r)
            taken_.add(best[1])
            placed_[key] = (best[1], best[2], best[0])
        return {"frame": fr_, "vis": fr_.visible, "info": info_, "bar": bar_, "north": north_, "placed": placed_, "reserve": reserve,
                "score": sum(v[2] for v in placed_.values())}

    best_try = attempt((0.0, 0.0, 0.0, 0.0))
    if legend_ids and on("legend") and best_try["score"] > 0.03:
        side = leg_wh[0] + 2 * inset
        for rs in ((side, 0.0, 0.0, 0.0), (0.0, 0.0, side, 0.0)):
            cand = attempt(rs)
            if cand["score"] + 0.01 < best_try["score"]:
                best_try = cand
    frame, vis, info, bar, north_show, placed = (best_try[k] for k in ("frame", "vis", "info", "bar", "north", "placed"))
    notes += [f"extent fitted with {best_try['reserve'][0] or best_try['reserve'][2]:.0f} mm kept free beside the legend"] if any(best_try["reserve"]) else []
    overlays: dict = {}
    if "legend" in placed:
        c, r, cov = placed["legend"]
        x = r.x0 if c.endswith("left") else r.x1
        y = r.y0 if c.startswith("upper") else r.y1
        overlays["legend"] = {"layers": legend_ids, "corner": c, "x": round(x, 3), "y": round(y, 3), "title_pt": float(fonts["legend_title"]),
                              "label_pt": float(fonts["legend"]), "covered": round(float(cov), 4)}
    if "bivariate_key" in placed:
        c, r, cov = placed["bivariate_key"]
        b = biv.legend_block
        n = int(b["n"])
        xt, yt = list((b.get("x") or {}).get("ticks") or []), list((b.get("y") or {}).get("ticks") or [])
        overlays["bivariate_key"] = {"layer": biv.id, "n": n, "cell": BV_CELL, "x": round(r.x0, 3), "y": round(r.y0, 3), "w": round(r.w, 3), "h": round(r.h, 3),
                                     "cells": [[c_["fill"] for c_ in row] for row in b["cells"]], "x_title": (b.get("x") or {}).get("title") or "",
                                     "y_title": (b.get("y") or {}).get("title") or "", "x_ticks": [xt[0], xt[-1]] if xt else ["", ""],
                                     "y_ticks": [yt[0], yt[-1]] if yt else ["", ""], "title": b.get("title") or "", "label_pt": float(fonts["legend"]),
                                     "covered": round(float(cov), 4)}
    if "scale" in placed:
        c, r, cov = placed["scale"]
        per_seg = bar["segments"][0]["to"] - bar["segments"][0]["from"]
        overlays["scale_bar"] = {"unit": bar["unit"], "segments": len(bar["segments"]), "per_segment": round(per_seg, 6), "total": bar["total"],
                                 "pt": float(fonts["scale"]), "x": round(r.x0, 3), "y": round(r.y0, 3), "covered": round(float(cov), 4)}
    if "north" in placed:
        c, r, cov = placed["north"]
        overlays["north"] = {"x": round(r.x0, 3), "y": round(r.y0, 3), "size": round(na_size, 3), "rotation_hint": info["north"].get("rotation", 0.0),
                             "covered": round(float(cov), 4)}
    for k, (c, r, cov) in placed.items():
        if cov > 0.12:
            notes.append(f"the {k} sits over {cov:.0%} of the data in the corner with least cover; move it in QGIS")
    if bar is None and on("scale_bar"):
        notes.append(info.get("bar_hidden") or "the scale bar is hidden at this scale")
    if on("north_arrow") and not north_show:
        notes.append((info.get("north") or {}).get("reason") or "the north arrow is hidden")
    if banner_on:
        fill = t["status"]["danger"]
        import classify as cf
        overlays["banner"] = {"text": (els.get("confidentiality_banner") or {}).get("text") or "CLIENT CONFIDENTIAL", "h": banner_h, "pt": float(fonts["banner"]),
                              "color": fill, "text_color": cf.text_on(fill, t["label"], t["background"])}

    # ---- title and footer text (fitted with the same measuring the SVG path uses)
    title_el = els.get("title") or {}
    ttl: dict = {"text": "", "rect": {"x": 0, "y": 0, "w": 0, "h": 0}}
    if title_el.get("on") is not False:
        import text_lint
        text = text_lint.title_text(spec)
        tr = lo.rect_of(tpl["title"])
        size = float(fonts["title"])
        lines, cut = mr.wrap_lines(text, tr.w, size, 700, 2)
        while cut and size > 12.0:
            size -= 1.0
            lines, cut = mr.wrap_lines(text, tr.w, size, 700, 2)
        sub = title_el.get("subtitle")
        title_h = len(lines) * size * PT * 1.15 + 1.0
        ttl = {"text": text, "rect": {"x": tr.x0, "y": tr.y0, "w": tr.w, "h": tr.h}, "size_pt": size, "title_h": round(title_h, 3),
               "subtitle": sub or "", "subtitle_pt": float(fonts["subtitle"])}
    fr = lo.rect_of(tpl["footer"])
    logo_h = float(tpl.get("logo_height_mm", 10))
    logo = None
    logo_w = 0.0
    if on("logo"):
        try:
            svg_, logo_w = mr.logo_markup(plan, t["background"], 0, 0, logo_h)
            if svg_:
                (out / "logo.svg").write_text(svg_, encoding="utf-8")
                logo = {"file": "logo.svg", "w": round(logo_w, 3), "h": logo_h}
        except Exception as ex:  # noqa: BLE001
            notes.append(f"the brand logo could not be prepared ({type(ex).__name__}); it is not in this layout")
    left_w = fr.w - max(logo_w, 0) - 2.0 - 6.0
    lines: list[str] = []
    if (els.get("source_note") or {}).get("on", True):
        lines += provenance.source_lines(plan)
    if (els.get("crs_note") or {}).get("on", False):
        lines.append(f"Projection: {plan.crs_info['name']}.")
    disc = els.get("disclaimer") or {}
    if disc.get("on") and disc.get("text"):
        lines.append(str(disc["text"]))
    tm_line = plan.pack["pack"].get("trademark_line")
    if tm_line and (els.get("trademark_line") or {}).get("on", "auto") in ("auto", True):
        lines.append(tm_line)
    npt, need = _wrap_fit(lines, left_w, float(fonts["note"]), 6.0, fr.h)
    ds = els.get("date_stamp") or {}
    date_text = ""
    if ds.get("on", True):
        td = mr.today()
        date_text = ds.get("text") if ds.get("text") not in (None, "auto") else f"Map produced {gl.plan_date_text(plan, td)}"
    footer = {"rect": {"x": fr.x0, "y": fr.y0, "w": fr.w, "h": fr.h}, "lines": lines, "left_w": round(left_w, 3), "note_pt": npt,
              "date_text": date_text, "date_y": round(logo_h + 1.5 if logo else 0.0, 3), "logo": logo, "need_mm": round(need, 2)}
    if need > fr.h + 0.2:
        notes.append(f"the footer notes need {need:.0f} mm but the footer band is {fr.h:.0f} mm; shorten them in QGIS")

    layout = {
        "version": 1, "name": f"{spec['id']}_{plan.tpl_name}", "page_mm": [W, H], "template": plan.tpl_name, "brand": plan.brand,
        "crs": plan.crs, "crs_wkt": crs_obj.to_wkt(), "extent": [round(float(v), 6) for v in vis],
        "map": {"x": map_rect.x0, "y": map_rect.y0, "w": map_rect.w, "h": map_rect.h},
        "font": {"family": "Inter"},
        "colors": {"label": t["label"], "background": t["background"], "frame": t["frame"]},
        "neatline": bool(on("neatline")), "draw_order": [L["id"] for L in layers_json], "layers": layers_json,
        "title": ttl, "footer": footer, "overlays": overlays, "notes": notes,
    }
    (out / "layout.json").write_text(json.dumps(layout, indent=1), encoding="utf-8")
    shutil.copyfile(BUILD_SCRIPT, out / "qgis_build_layout.py")
    return {"dir": str(out), "layout": layout, "plan": plan, "notes": notes, "title": ttl.get("text") or spec["message"][:80],
            "stem": f"{plan.pack['tokens'].get('output_prefix', '')}{spec['id']}_{plan.tpl_name}{mr.pack_suffix(plan)}_qgis_v{mr.today().strftime('%Y%m%d')}"}


# ---------------------------------------------------------------------------------------------------------------- run + verify
def _verify(out_dir: Path, files: list[str], layout: dict, dpi: float) -> list[dict]:
    checks: list[dict] = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    W, H = layout["page_mm"]
    for fn in files:
        p = out_dir / fn
        if fn.endswith(".pdf"):
            from pypdf import PdfReader
            rd = PdfReader(str(p))
            box = rd.pages[0].mediabox
            w, h = float(box.width) * 25.4 / 72, float(box.height) * 25.4 / 72
            add("pdf-page-size", abs(w - W) <= 0.6 and abs(h - H) <= 0.6, f"{w:.1f} x {h:.1f} mm; template {W} x {H} mm")
            names: set[str] = set()
            for pg in rd.pages:
                res = pg.get("/Resources") or {}
                fonts_ = (res.get("/Font") or {}) if hasattr(res, "get") else {}
                for k in fonts_:
                    try:
                        names.add(str(fonts_[k].get_object().get("/BaseFont", "")))
                    except Exception:  # noqa: BLE001
                        pass
            fam = layout["font"]["family"]
            inter = sorted(n for n in names if fam.lower() in n.lower())
            other = sorted(n for n in names if fam.lower() not in n.lower())
            add("pdf-font", bool(inter), f"{fam} used: {', '.join(inter) or 'none'}" + (f"; other faces: {', '.join(other)}" if other else ""))
            txt = (rd.pages[0].extract_text() or "")
            title = (layout["title"].get("text") or "")[:20]
            add("pdf-text-kept", (not title) or title.split(" ")[0] in txt, "title text is extractable" if txt else "no extractable text")
        elif fn.endswith(".png"):
            from PIL import Image
            with Image.open(p) as im:
                ew, eh = round(W * dpi / 25.4), round(H * dpi / 25.4)
                add("png-size", abs(im.width - ew) <= 3 and abs(im.height - eh) <= 3, f"{im.width} x {im.height} px; expected about {ew} x {eh} at {dpi:g} dpi")
        elif fn.endswith(".svg"):
            s = p.read_text(encoding="utf-8", errors="replace")
            add("svg-no-external", not re.search(r'(?:href|src)="https?://', s), "no external references")
    return checks


def run(spec_path, out_dir, *, formats=("pdf", "png"), dpi: float | None = None, brand: str | None = None, template: str | None = None,
        timeout: int = 300) -> dict:
    import map_brand
    import map_export as mx
    pr = probe()
    if not pr["available"]:
        raise QgisError(pr["note"], gc.EXIT_REFUSED)
    bad = scan_build_script()
    if bad:
        raise QgisError(f"the QGIS build script contains calls this path forbids: {', '.join(bad)}", gc.EXIT_REFUSED)
    out = Path(out_dir)
    stem0 = None
    kit_tmp = out / "_qgis_kit_work"
    if kit_tmp.exists():
        shutil.rmtree(kit_tmp)
    kit = build_kit(spec_path, kit_tmp, brand=brand, template=template)
    layout, plan = kit["layout"], kit["plan"]
    stem0 = kit["stem"]
    dpi_ = float(dpi or plan.tpl.get("raster_default_dpi", 300))
    fmts = [f for f in formats if f in ("pdf", "png", "svg")]
    work = out / "_qgis_work"
    if work.exists():
        shutil.rmtree(work)
    t0 = time.time()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    r = subprocess.run([pr["python"], str(kit_tmp / "qgis_build_layout.py"), str(kit_tmp), str(work), ",".join(fmts), str(dpi_)],
                       capture_output=True, text=True, timeout=timeout, env=env)
    res_path = work / "build_result.json"
    res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.is_file() else {"files": [], "warnings": []}
    if r.returncode != 0 and not res.get("files"):
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-6:])
        raise QgisError(f"QGIS could not build the layout:\n{tail}", gc.EXIT_RUNTIME)
    # ---- the final names, the hidden accreditation, then checks on what was written
    out.mkdir(parents=True, exist_ok=True)
    final_kit = out / f"{stem0}_kit"
    if final_kit.exists():
        shutil.rmtree(final_kit)
    shutil.move(str(kit_tmp), str(final_kit))
    written: list[str] = []
    title = kit["title"]
    for fn in res["files"]:
        src = work / fn
        ext = src.suffix.lstrip(".")
        dst = out / f"{stem0}.{ext}"
        raw = src.read_bytes()
        props = map_brand.props_for(plan.brand, plan.manifest, ext if ext in ("pdf", "svg") else "png")
        if ext == "pdf":
            raw = mx.stamp_pdf(raw, props, title)
        elif ext == "png":
            raw = mx.stamp_png(raw, dpi_, props, title, "")
        elif ext == "svg":
            s = raw.decode("utf-8")
            attrs = " ".join(f'{k}="{v}"' for k, v in (props.get("root_attributes") or {}).items())
            if attrs:
                s = re.sub(r"<svg\b", f"<svg {attrs}", s, count=1)
            raw = s.encode("utf-8")
        dst.write_bytes(raw)
        written.append(dst.name)
    shutil.rmtree(work, ignore_errors=True)
    checks = _verify(out, written, layout, dpi_)
    ok = all(c["ok"] for c in checks) and bool(written)
    report = {"experimental": True, "qgis": pr["qgis"], "python": Path(pr["python"]).name, "seconds": round(time.time() - t0, 1), "files": written,
              "kit": final_kit.name, "project": f"{final_kit.name}/layout.qgz", "dpi": dpi_, "checks": checks, "ok": ok,
              "notes": layout["notes"] + res.get("warnings", []), "items": res.get("items", [])}
    (out / f"{stem0}_qgis_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qgis_layout", description="Optional QGIS print-layout path (experimental).")
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("probe")
    for name in ("kit", "run"):
        p = sub.add_parser(name)
        p.add_argument("--spec", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--brand")
        p.add_argument("--template")
        if name == "run":
            p.add_argument("--formats", default="pdf,png")
            p.add_argument("--dpi", type=float)
    a = ap.parse_args(argv)
    try:
        if a.op == "probe":
            r = probe()
            print(json.dumps(r, indent=1))
            return gc.EXIT_OK if r["available"] else gc.EXIT_REFUSED
        if a.op == "kit":
            k = build_kit(a.spec, a.out, brand=a.brand, template=a.template)
            print(json.dumps({"dir": k["dir"], "layers": [L["id"] for L in k["layout"]["layers"]], "notes": k["notes"]}, indent=1))
            return gc.EXIT_OK
        rep = run(a.spec, a.out, formats=[f.strip() for f in a.formats.split(",") if f.strip()], dpi=a.dpi, brand=a.brand, template=a.template)
        print(json.dumps(rep, indent=1))
        return gc.EXIT_OK if rep["ok"] else gc.EXIT_LINT
    except QgisError as ex:
        print(f"qgis_layout: {ex}", file=sys.stderr)
        return ex.code
    except Exception as ex:  # noqa: BLE001
        mod = type(ex).__module__
        if mod.startswith("map_plan") or type(ex).__name__ == "PlanError":
            print(f"qgis_layout: {ex}", file=sys.stderr)
            return getattr(ex, "code", gc.EXIT_USAGE)
        raise


if __name__ == "__main__":
    sys.exit(main())
