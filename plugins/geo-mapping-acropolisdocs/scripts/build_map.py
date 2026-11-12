#!/usr/bin/env python3
"""build_map: MapSpec in, finished map files out (spec 7.1 map-create, 12 run order).

    build_map.py --spec map.yaml --out DIR [--formats svg,pdf,png,html] [--brand SLUG] [--template NAME] [--strict]

Steps: spec lint -> load and data/CRS lint -> classify and color -> render the SVG master -> guard (palette, Inter, stamps) ->
export the other formats. A BLOCK finding stops the build (exit 3) and, with --strict, leaves no files behind. The brand
resolves by the rules of spec 11.4a and is written back into the MapSpec the first time, so a later default change never restyles it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import a11y_lint  # noqa: E402
import brand_lint  # noqa: E402
import cartographic_lint  # noqa: E402
import georef  # noqa: E402
import gmap_common as gc  # noqa: E402
import map_export as mx  # noqa: E402
import map_plan as mp  # noqa: E402
import map_render as mr  # noqa: E402
import output_lint  # noqa: E402
import provenance  # noqa: E402
import webmap  # noqa: E402
import ref_lint  # noqa: E402
import service_lint  # noqa: E402
import telecom_lint  # noqa: E402
import text_lint  # noqa: E402
import ref_library as rl  # noqa: E402
import library_lint as ll  # noqa: E402

SEV = {"BLOCK": 0, "WARN": 1, "INFO": 2}
STATIC_ONLY = {"MAP-CART-009", "MAP-CRS-003", "MAP-CART-020"}


def guard_findings(svg: str, plan: mp.Plan) -> list[dict]:
    st = getattr(plan, "style", None)                  # 1.4.0: the brand lints check against the resolved style pack
    return (brand_lint.check_palette(svg, plan.brand, st) + brand_lint.check_fonts(svg, st.families() if st is not None else None)
            + brand_lint.check_stamps(svg, plan.brand, plan.manifest)
            + brand_lint.check_hidden_accreditation(svg, plan.brand, plan.manifest))


def pdf_fonts(plan) -> list[str] | None:
    import map_style
    return map_style.pdf_font_names(getattr(plan, "style", None))


def last_build_pack(out_dir, plan: mp.Plan) -> str | None:
    """The style pack of the newest earlier build of this map in the output folder (its provenance sidecar), or None."""
    prefix = plan.pack["tokens"].get("output_prefix") or ""
    best = None
    for p in Path(out_dir).glob(f"{prefix}{plan.spec['id']}_*.provenance.json") if Path(out_dir).is_dir() else []:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("map_id") != plan.spec["id"]:
            continue
        if best is None or p.stat().st_mtime > best[0]:
            best = (p.stat().st_mtime, (doc.get("style") or {}).get("pack") or "default")
    return best[1] if best else None


def style_findings(plan: mp.Plan, out_dir) -> list[dict]:
    """MAP-STYLE-002 INFO: the pack changed since the last build of this map: the whole map was built again from its kept data (4.2)."""
    st = getattr(plan, "style", None)
    if st is None or out_dir is None:
        return []
    prev = last_build_pack(out_dir, plan)
    if prev is not None and prev != st.id:
        return [{"id": "MAP-STYLE-002", "severity": "INFO", "message": f"the pack changed from {prev} to {st.id}: the map is rebuilt from its data "
                 "(the data, analysis and notes are kept; the plan, colours, labels, legend, layout and every output are new, under a new file name)"}]
    return []


def reference_findings(plan: mp.Plan) -> list[dict]:
    import datetime as dt
    import os
    tags, types = ll.load_vocab(rl.shipped_lib())
    today = dt.date.fromisoformat(os.environ["GMAP_FIXED_DATE"]) if os.environ.get("GMAP_FIXED_DATE") else None
    return ref_lint.lint_plan(plan, rl.all_entries("all"), tags, types, today)


def voice_findings(plan: mp.Plan, rendered: mr.Rendered) -> list[dict]:
    text = " ".join([rendered.title, plan.spec["message"]] + [str(((plan.spec.get("elements") or {}).get("title") or {}).get("subtitle") or "")]
                    + [l.title for l in plan.layers])
    return brand_lint.check_voice(text, plan.brand)


def text_findings(plan: mp.Plan, rendered: mr.Rendered) -> list[dict]:
    """MAP-TEXT-001 to 012 (0.21.0, docs/specs/map-text-spec.md): the title and subtitle against the elements that own the other facts.
    WARN or INFO only, never BLOCK (D3)."""
    els = plan.spec.get("elements") or {}
    el = els.get("title") or {}
    if el.get("on") is False:
        return []
    legends = [str((l.legend_block or {}).get("title") or "") for l in plan.layers if l.legend_block]
    dt_cfg = els.get("data_table") or {}
    return text_lint.lint(text_lint.title_text(plan.spec), el.get("subtitle"), tagline=el.get("tagline"), legend_titles=[x for x in legends if x],
                          table_title=dt_cfg.get("title") if dt_cfg.get("on") else None, source=" ".join(mr.source_lines(plan)))


def map_text_summary(plan: mp.Plan, findings: list[dict]) -> dict:
    """The title and subtitle drawn, and the chat callout (§11.1): every MAP-TEXT WARN, and the INFO ones too on a client deliverable."""
    el = (plan.spec.get("elements") or {}).get("title") or {}
    client = plan.spec.get("audience") in ("external", "regulatory")
    return {"title": text_lint.title_text(plan.spec) if el.get("on") is not False else None, "subtitle": el.get("subtitle"),
            "supplied": bool(el.get("text")), "callout": text_lint.summary_lines(findings, client=client)}


def build(spec_path: str, out_dir: str, formats: list[str] | None = None, *, brand: str | None = None, template: str | None = None,
          strict: bool = False, mode: str | None = None, keep: bool = False, export_options: dict | None = None) -> dict:
    """`export_options` (M16): transparent, bleed_mm, crop_marks, pdfa, inkscape, outline_text over the MapSpec's `export:` block
    (map_export.options_from); each variant is read back (MAP-OUT-014 to 018). 1.3.0 (R1-PANEL): a file with a top-level `sheet_version` is a
    SheetSpec, built by sheet.build (two to six panels with one shared frame)."""
    import sheet as sheet_mod
    if sheet_mod.is_sheet(spec_path):
        try:
            sh_ = sheet_mod.load(spec_path)
        except sheet_mod.SheetError as ex:
            raise mp.PlanError(f"{ex.fid}: {ex}", gc.EXIT_LINT) from None
        fmts = formats if formats is not None else ((sh_.get("export") or {}).get("formats") or ["svg", "pdf"])
        return sheet_mod.build(spec_path, out_dir, fmts, brand=brand, template=template, strict=strict, export_options=export_options)
    formats = formats if formats is not None else (gc.load_mapspec(Path(spec_path)).get("export", {}).get("formats") or ["svg"])
    formats = ["html" if f == "webmap" else f for f in formats]
    web_fmts = [f for f in formats if f == "html"]
    static = [f for f in formats if f != "html"]
    unknown = [f for f in static if f not in ("svg", "pdf", "png") + georef.GEO_FORMATS]
    if unknown:
        raise mp.PlanError(f"formats {', '.join(unknown)} are not map outputs (data packages come with the map-build pipeline); this build writes svg, pdf, png, geopdf, geotiff and html", gc.EXIT_USAGE)
    web = webmap.build_page(spec_path, brand=brand, template=template) if web_fmts else None
    if static or web is None:
        plan = mp.prepare(spec_path, mode=mode, brand=brand, template=template)
        rendered = mr.render(plan)
    else:
        plan, rendered = web.plan, web.rendered            # the page's own plan: static scale bar off, bundled basemap
    # write the resolved brand back so a later default change never restyles this map
    if not plan.spec.get("brand"):
        import map_brand
        map_brand.write_spec_brand(Path(spec_path), plan.brand)
    recs = provenance.records(plan) + (web.records if web else [])
    findings = (plan.findings + rendered.findings + guard_findings(rendered.svg, plan) + voice_findings(plan, rendered)
                + provenance.validate(plan, recs) + reference_findings(plan) + service_lint.lint_plan(plan) + telecom_lint.lint_plan(plan) + cartographic_lint.lint_plan(plan, rendered.report, rendered.svg) + a11y_lint.lint(plan, rendered) + output_lint.check_svg(rendered.svg)
                + output_lint.check_layer_load(rendered.svg) + text_findings(plan, rendered))
    findings += style_findings(plan, out_dir)
    if web and not static:        # a page has its own scale bar and fills its own frame: the print-sheet checks for those do not apply
        findings = [f for f in findings if f["id"] not in STATIC_ONLY]
    if web:
        seen = {(f["id"], f["message"]) for f in findings}
        findings += [f for f in web.findings if (f["id"], f["message"]) not in seen]
    dpi = float(plan.spec.get("export", {}).get("dpi") or plan.tpl.get("raster_default_dpi") or 300)
    try:
        opts = mx.options_from(plan.spec.get("export"), export_options)
    except mx.ExportError as ex:
        raise mp.PlanError(str(ex), gc.EXIT_USAGE)
    formats = static + (["svg"] if (opts["inkscape"] or opts["outline_text"]) and "svg" not in static else [])
    result = {"id": plan.spec["id"], "brand": plan.brand, "template": plan.tpl_name, "crs": plan.crs, "findings": [],
              "blocked": False, "files": [], "report": rendered.report, "stem": rendered.filename_stem, "notes": plan.notes, "dpi": dpi}

    def finish():
        findings.sort(key=lambda f: (SEV.get(f["severity"], 3), f["id"]))
        result["findings"] = findings
        result["blocked"] = any(f["severity"] == "BLOCK" for f in findings)
        result["map_text"] = map_text_summary(plan, findings)
        return result

    if any(f["severity"] == "BLOCK" for f in findings) and strict:
        return finish()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / f"{rendered.filename_stem}.svg"
    svg_path.write_text(mx.inkscape_svg(rendered.svg) if opts["inkscape"] else rendered.svg, encoding="utf-8")
    written = [svg_path]
    meth = None
    if ((plan.spec.get("elements") or {}).get("methods_page") or {}).get("on"):
        meth = provenance.methods(plan, rendered, recs)
        try:
            msvg = provenance.methods_svg(plan, meth, lambda W, H, body, title, alt: mr.assemble_page(plan, W, H, body, title, alt))
        except ValueError as ex:
            raise mp.PlanError(str(ex), gc.EXIT_LINT)
        findings += guard_findings(msvg, plan) + output_lint.check_svg(msvg)
        mpath = out / f"{rendered.filename_stem}_methods.svg"
        mpath.write_text(msvg, encoding="utf-8")
        written.append(mpath)
        if "pdf" in formats:
            import map_brand
            props = map_brand.props_for(plan.brand, plan.manifest, "pdf")
            ppath = out / f"{rendered.filename_stem}_methods.pdf"
            ppath.write_bytes(mx.stamp_pdf(mx.to_pdf_bytes(msvg, meth["title"], pdfa=opts["pdfa"]), props, f"Data & Methods: {meth['title']}",
                                           plan.spec["message"], pdfa=opts["pdfa"]))
            written.append(ppath)
            findings += output_lint.check_pdf(str(ppath), provenance.PAGE_MM, fonts_allowed=pdf_fonts(plan))
    side = provenance.write_sidecar(out / f"{rendered.filename_stem}.provenance.json", plan, recs, meth)
    written.append(side)
    result["provenance"] = str(side)
    findings += output_lint.check_name(str(svg_path), plan.pack["tokens"].get("output_prefix") or None, plan.spec["id"], plan.tpl_name)
    if any(f in formats for f in ("pdf", "png")) or opts["outline_text"]:
        import map_brand
        props = {f: map_brand.props_for(plan.brand, plan.manifest, f) for f in ("pdf", "png")}
        ex = mx.export(rendered.svg, out, rendered.filename_stem, [f for f in formats if f in ("pdf", "png")], props=props,
                       title=rendered.title, alt=rendered.alt_text, dpi=dpi, subject=plan.spec["message"], options={**opts, "inkscape": False},
                       style=getattr(plan, "style", None))
        for fp in ex["files"]:
            written.append(Path(fp))
            if fp.endswith(".pdf"):
                findings += output_lint.check_pdf(fp, ex["size_mm"], fonts_allowed=pdf_fonts(plan))
            elif fp.endswith(".png"):
                findings += output_lint.check_png(fp, ex["size_mm"], dpi)
        if opts["inkscape"]:
            ex["variants"]["inkscape"] = {"file": str(svg_path)}
        findings += output_lint.check_export(ex, rendered.svg)
        result["export"] = {k: v for k, v in ex["variants"].items()}
    elif opts["inkscape"]:
        findings += output_lint.check_inkscape_svg(svg_path.read_text(encoding="utf-8"), rendered.svg)
    geo_fmts = [f for f in formats if f in georef.GEO_FORMATS]
    if geo_fmts:
        import map_brand
        gfind, refused = georef.guard(plan, rendered.report, geo_fmts, dpi)
        findings += gfind
        ok_fmts = [f for f in geo_fmts if f not in refused]
        if ok_fmts:
            win = rendered.report["map_window"]
            pprops = map_brand.props_for(plan.brand, plan.manifest, "pdf")
            pdf = mx.stamp_pdf(mx.to_pdf_bytes(rendered.svg, rendered.title), pprops, rendered.title, plan.spec["message"])
            if "geopdf" in ok_fmts:
                raw = georef.geopdf_bytes(pdf, win)
                gp = out / f"{rendered.filename_stem}_geo.pdf"
                gp.write_bytes(raw)
                written.append(gp)
                findings += georef.verify_pdf(raw, win) + output_lint.check_pdf(str(gp), mx.svg_size_mm(rendered.svg), fonts_allowed=pdf_fonts(plan))
            if "geotiff" in ok_fmts:
                nprops = map_brand.props_for(plan.brand, plan.manifest, "png")["tEXt"]
                raw, _info = georef.geotiff_bytes(mx.rasterize(pdf, dpi, rendered.svg), win, dpi,
                                                  {"title": rendered.title, "author": nprops["Author"], "software": nprops["Software"],
                                                   "items": {"accreditation": nprops["Accreditation"], "brand_pack": nprops["BrandPack"]}})
                tp = out / f"{rendered.filename_stem}_geo.tif"
                tp.write_bytes(raw)
                written.append(tp)
                findings += georef.verify_tiff(raw, win)
    if web:
        hpath = out / f"{rendered.filename_stem}.html"
        hpath.write_text(web.html, encoding="utf-8")
        written.append(hpath)
        result["web"] = {k: v for k, v in web.report.items() if k != "notes"}
    # 1.3.0 (R1-TERMS): the terms register beside the outputs, from the records and what the outputs show
    import terms_register as tr
    embedded = None
    if web:
        embedded = {l.id for l in provenance.all_layers(plan) if getattr(l, "kind", None) != "raster" and getattr(l, "draw", True)}
        embedded |= {r["layer_id"] for r in recs if str(r.get("layer_id", "")).startswith("basemap:") and r["layer_id"] != "basemap:tiles"}
    tdoc, tfs, tfiles = tr.for_deliverable(out, recs, deliverable="map" if static or not web else "web map", stem=rendered.filename_stem,
                                           outputs={"svg": rendered.svg if static or not web else None, "html": web.html if web else None},
                                           embedded=embedded, title=rendered.title)
    findings += tfs
    written += tfiles
    result["terms"] = {"files": [str(f) for f in tfiles], "summary": tdoc["summary"]}
    if "svg" not in formats:
        for w in [w for w in written if w.suffix == ".svg"]:
            w.unlink()
        written = [w for w in written if w.suffix != ".svg"]
    result["files"] = [str(w) for w in written if not w.name.endswith(".provenance.json") and w not in tfiles]
    result["svg_bytes"] = len(rendered.svg.encode("utf-8"))
    if keep:        # the objects a pipeline packages from (M5); never serialised
        if meth is None:
            try:
                meth = provenance.methods(plan, rendered, recs)
            except Exception:  # noqa: BLE001 - a methods note is a courtesy here, not a gate
                meth = None
        result["_objects"] = {"plan": plan, "rendered": rendered, "records": recs, "methods": meth}
    finish()
    if result["blocked"] and strict:
        for w in written:
            w.unlink(missing_ok=True)
        result["files"] = []
        result.pop("provenance", None)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--formats")
    ap.add_argument("--brand")
    ap.add_argument("--template")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--theme", choices=["light", "dark"], help="overrides the MapSpec's theme (default light); dark only when asked for")
    ap.add_argument("--mode", choices=["print", "screen"], help=argparse.SUPPRESS)      # old name: print = light, screen = dark
    mx.add_option_flags(ap)                                                               # M16: --transparent, --bleed, --crop-marks, --pdfa, --inkscape, --outline-text
    a = ap.parse_args(argv)
    try:
        res = build(a.spec, a.out, a.formats.split(",") if a.formats else None, brand=a.brand, template=a.template, strict=a.strict, mode=(mp.THEME_MODES[a.theme] if a.theme else a.mode),
                    export_options=mx.option_flags(a))
    except mp.PlanError as ex:
        print(f"build_map: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(res, indent=2, default=str))
    for ln in (res.get("map_text") or {}).get("callout") or []:     # map text §11.1: the findings the chat summary lists, never blocking
        print(ln, file=sys.stderr)
    return gc.EXIT_LINT if res["blocked"] else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
