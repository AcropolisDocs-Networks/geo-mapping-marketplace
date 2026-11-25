#!/usr/bin/env python3
"""build_map: MapSpec in, finished map files out (spec 7.1 map-create, 12 run order).

    build_map.py --spec map.yaml --out DIR [--formats svg,pdf,png,html] [--brand SLUG] [--template NAME] [--strict] [--draft] [--no-mute]

1.22.0: `--draft` writes one small picture (<stem>_draft.png, 96 dpi) and the mute picture in place of the full set, for the look-and-fix
loop; every build that makes a picture also writes <stem>_mute.png (the sheet without its words; `--no-mute` leaves it out) and carries the
message check's measures and findings in `story` (lints/story_lint.py, never blocking).

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
import story_lint  # noqa: E402
import telecom_lint  # noqa: E402
import text_lint  # noqa: E402
import ref_library as rl  # noqa: E402
import library_lint as ll  # noqa: E402

SEV = {"BLOCK": 0, "WARN": 1, "INFO": 2}
STATIC_ONLY = {"MAP-CART-009", "MAP-CRS-003", "MAP-CART-020", "MAP-STORY-001", "MAP-STORY-003"}     # 1.22.0: a page has no fixed frame and its legend is a tab


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
    """MAP-TEXT-001 to 012 (0.21.0, the map-text module): the title and subtitle against the elements that own the other facts.
    WARN or INFO only, never BLOCK (D3)."""
    els = plan.spec.get("elements") or {}
    el = els.get("title") or {}
    if el.get("on") is False:
        return []
    legends = [str((l.legend_block or {}).get("title") or "") for l in plan.layers if l.legend_block]
    dt_cfg = els.get("data_table") or {}
    fs = text_lint.lint(text_lint.title_text(plan.spec), el.get("subtitle"), tagline=el.get("tagline"), legend_titles=[x for x in legends if x],
                        table_title=dt_cfg.get("title") if dt_cfg.get("on") else None, source=" ".join(mr.source_lines(plan)))
    # 4.2.0 (Day 25 #6): text the person supplied is drawn as written and not marked. elements.title.supplied: true (the title and the
    # subtitle) or a list of the supplied ones ([subtitle]) says which; a brief's asks set it (5.0.0).
    sup = el.get("supplied")
    supplied = {"title", "subtitle"} if sup is True else {str(x) for x in sup} if isinstance(sup, (list, tuple)) else set()
    return [f for f in fs if (f.get("detail") or {}).get("element") not in supplied]


def map_text_summary(plan: mp.Plan, findings: list[dict]) -> dict:
    """The title and subtitle drawn, and the chat callout (§11.1): every MAP-TEXT WARN, and the INFO ones too on a client deliverable."""
    el = (plan.spec.get("elements") or {}).get("title") or {}
    client = plan.spec.get("audience") in ("external", "regulatory")
    return {"title": text_lint.title_text(plan.spec) if el.get("on") is not False else None, "subtitle": el.get("subtitle"),
            "supplied": bool(el.get("text")), "callout": text_lint.summary_lines(findings, client=client)}


def outside_raster_window(report: dict, tol: float = 0.002) -> tuple | None:
    """1.21.0 (Day 18 issue 5): the window to read again (the drawn frame plus a tenth all round) when the frame the fit chose reaches past the
    window the rasters were read for by more than `tol` of its size; None when it stays inside, or the map has no raster."""
    rw = report.get("raster_window")
    vis = (report.get("map_window") or {}).get("extent")
    if not rw or not vis:
        return None
    x0, y0, x1, y1 = (float(v) for v in vis)
    m = tol * max(x1 - x0, y1 - y0)
    if x0 >= rw[0] - m and y0 >= rw[1] - m and x1 <= rw[2] + m and y1 <= rw[3] + m:
        return None
    pad = 0.1 * max(x1 - x0, y1 - y0)
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def summary_line(res: dict) -> str:
    """2.4.0 (T-06): one line that says what was built and, loudly, what the spec asked for that is not on the map."""
    n = len(res.get("files") or [])
    head = f"build_map: {res.get('id')}: {'blocked' if res.get('blocked') else ('draft' if res.get('draft') else f'{n} file(s)')}"
    return head + "".join(f"; {d}" for d in res.get("degraded") or [])


def build(spec_path: str, out_dir: str, formats: list[str] | None = None, *, brand: str | None = None, template: str | None = None,
          strict: bool = False, mode: str | None = None, keep: bool = False, export_options: dict | None = None, draft: bool = False,
          mute: bool = False, spec_edit=None) -> dict:
    """`export_options` (M16): transparent, bleed_mm, crop_marks, pdfa, inkscape, outline_text over the MapSpec's `export:` block
    (map_export.options_from); each variant is read back (MAP-OUT-014 to 018). 1.3.0 (R1-PANEL): a file with a top-level `sheet_version` is a
    SheetSpec, built by sheet.build (two to six panels with one shared frame)."""
    gc.ledger_start_lazy("build_map.build")
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
        # 2.0.0: `spec_edit(spec)` adjusts the loaded MapSpec in memory before it is planned (a story beat: its frame, emphasis and line);
        # the file is never changed, and its folder still resolves the data paths
        plan = mp.prepare(spec_path, mode=mode, brand=brand, template=template, spec_edit=spec_edit)
        rendered = mr.render(plan)
        wider = outside_raster_window(rendered.report)
        if wider is not None:
            # 1.21.0 (Day 18 issue 5): the fit moved or shrank the frame past the window the rasters, the relief and the ground were read
            # for (a third of the map had no relief). Read them again for the frame the fit chose, and draw once more.
            plan = mp.prepare(spec_path, mode=mode, brand=brand, template=template, window=wider, spec_edit=spec_edit)
            rendered = mr.render(plan)
            plan.notes.append("rasters, relief and ground read a second time: the fit moved the frame past the window they were first read for")
        # 1.22.0: a layer named on the map in place of its key (legend.direct) whose name found no room goes back into the legend, and the
        # map is drawn once more: a map never shows a layer it neither names nor keys.
        crowded = sorted(k for k, v in ((rendered.report.get("labels") or {}).get("direct") or {}).items() if v.get("missing"))
        if crowded:
            def keyed(spec_):
                if spec_edit:
                    spec_edit(spec_)
                for ly in spec_.get("layers") or []:
                    if ly.get("id") in crowded:
                        ly["legend"] = {**(ly.get("legend") if isinstance(ly.get("legend"), dict) else {}), "direct": False}
            plan = mp.prepare(spec_path, mode=mode, brand=brand, template=template, window=wider, spec_edit=keyed)
            rendered = mr.render(plan)
            plan.notes.append("legend.direct: " + ", ".join(f"'{c}'" for c in crowded) + " found no room for a name on the map and stayed in the legend")
    else:
        plan, rendered = web.plan, web.rendered            # the page's own plan: static scale bar off, bundled basemap
    # write the resolved brand back so a later default change never restyles this map
    if not plan.spec.get("brand") and spec_edit is None:
        import map_brand
        map_brand.write_spec_brand(Path(spec_path), plan.brand)
    recs = provenance.records(plan) + (web.records if web else [])
    findings = (plan.findings + rendered.findings + guard_findings(rendered.svg, plan) + voice_findings(plan, rendered)
                + provenance.validate(plan, recs) + ([] if draft else reference_findings(plan)) + service_lint.lint_plan(plan) + telecom_lint.lint_plan(plan) + cartographic_lint.lint_plan(plan, rendered.report, rendered.svg) + a11y_lint.lint(plan, rendered) + output_lint.check_svg(rendered.svg)
                + output_lint.check_layer_load(rendered.svg) + text_findings(plan, rendered))
    findings += style_findings(plan, out_dir)
    # 1.22.0: the message check, on every build: does the drawing carry the message by itself? Measures and findings, never a BLOCK.
    try:
        story_fs, story_measures = story_lint.lint(plan, rendered.report)
    except Exception as ex:  # noqa: BLE001 - a check that cannot run never stops a build
        story_fs, story_measures = [], {"error": f"{type(ex).__name__}: {str(ex)[:160]}"}
    findings += story_fs
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
              "blocked": False, "files": [], "report": rendered.report, "stem": rendered.filename_stem, "notes": plan.notes, "dpi": dpi,
              "map_window_lonlat": (rendered.report.get("map_window") or {}).get("lonlat"),        # 3.4.0: the window drawn, W, S, E, N
              "map_window_area_km2": (rendered.report.get("map_window") or {}).get("area_km2")}
    try:                                              # 1.16.0 (Day 14 issue 12): say when a newer plugin release is out (today's answer only)
        import release_check as rc
        rn = rc.check(cache_only=True)["note"]
        if rn and rn not in result["notes"]:
            result["notes"] = list(result["notes"]) + [rn]
    except Exception:  # noqa: BLE001 - never stops a build
        pass

    story = {"message": plan.spec.get("message"), "measures": story_measures}
    result["story"] = story

    def finish():
        findings.sort(key=lambda f: (SEV.get(f["severity"], 3), not f.get("degraded"), f["id"]))   # 2.4.0 (T-06): a missing relief leads the warnings
        result["findings"] = findings
        result["degraded"] = list(getattr(plan, "degraded", []) or [])
        # 2.5.0 (I-22): a fetched DEM is never part of the hand-back; its request is, so the person's computer fetches it again if it needs it
        result["refetch"] = [{"layer": b.get("layer_id"), **((b.get("record") or {}).get("terrain") or {}).get("request", {})}
                             for b in getattr(plan, "basemap", []) or [] if b.get("kind") == "hillshade"
                             and ((b.get("record") or {}).get("terrain") or {}).get("request")]
        result["summary"] = summary_line(result)
        try:                                                 # 4.2.0: the MapSpec against the standing defaults (data/policy.yaml); a report, never a gate
            import policy as _policy
            result["deviations"] = _policy.deviations(plan.spec)
        except Exception as ex:  # noqa: BLE001 - the table is a report; it never stops a build
            result["deviations"] = {"line": f"deviations from the defaults: not checked ({type(ex).__name__})", "items": [], "counts": {}}
        gc.ledger_note(outputs=[f for f in result.get("files") or [] if Path(f).is_file()], findings=findings, degraded=result["degraded"])
        result["blocked"] = any(f["severity"] == "BLOCK" for f in findings)
        result["map_text"] = map_text_summary(plan, findings)
        story["callout"] = story_lint.summary_lines(findings)
        return result

    if any(f["severity"] == "BLOCK" for f in findings) and strict:
        return finish()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def mute_picture():
        """The sheet without its words, beside the map (1.22.0). A working file: never in the provenance, the terms register or a package."""
        import map_preview as pv
        try:
            p, info = pv.write_mute(rendered.svg, out, rendered.filename_stem)
            story["mute_preview"] = str(p)
            story["mute_removed"] = info
            story["ask"] = pv.MUTE_QUESTION
        except Exception as ex:  # noqa: BLE001 - a preview that cannot be drawn never stops a build
            story["mute_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"

    if draft:
        # 1.22.0: the look-and-fix loop. One small picture of the whole sheet and the mute picture; the SVG, the PDF, the full-size PNG,
        # the provenance sidecar and the terms register are written by the build without --draft, so a finished set is never half replaced.
        import map_preview as pv
        if not static and web is not None:
            raise mp.PlanError("--draft draws the print sheet; a page (--formats html) has its own layout check", gc.EXIT_USAGE)
        try:                                              # 2.3.0: both pictures from one PDF of two pages (one WeasyPrint run, not two)
            dp, mp_, info = pv.write_draft_and_mute(rendered.svg, out, rendered.filename_stem)
            story.update(mute_preview=str(mp_), mute_removed=info, ask=pv.MUTE_QUESTION)
        except Exception:  # noqa: BLE001 - the two-page route failed: the two pictures one at a time, as before
            dp = pv.write_draft(rendered.svg, out, rendered.filename_stem)
            mute_picture()
        result["draft"] = True
        result["dpi"] = pv.DRAFT_DPI
        result["files"] = [str(dp)] + ([story["mute_preview"]] if story.get("mute_preview") else [])
        result["notes"] = list(result["notes"]) + ["draft: one picture at 96 dpi; run again without --draft for the SVG, PDF and full-size PNG "
                                                   "(and the reference library checks, which a draft leaves to the full build)"]
        return finish()
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
    if mute and static and any(x in formats for x in ("png", "pdf")):
        mute_picture()
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


DIGEST_FINDINGS = 12          # 4.2.0: the digest lists this many findings (every BLOCK and WARN first); the result file holds them all
_SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}


def write_result(res: dict, out_dir: str) -> Path | None:
    """4.2.0 (trace T1): the whole build result as a file beside the map, `<stem>.result.json`, so nothing has to read it off the console
    (three traces show a session's own parse of the printed result failing). -> the file, or None when it could not be written."""
    try:
        p = Path(out_dir) / f"{res.get('stem') or res.get('id') or 'map'}.result.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        return p
    except OSError:
        return None


def digest_lines(res: dict, result_file: Path | None = None) -> list[str]:
    """The build in at most 25 lines, for a person and for the session that ran it: what was built, the deviations from the defaults, the
    findings that matter, the map text and message callouts, and where the full result is."""
    fs = sorted(res.get("findings") or [], key=lambda f: _SEV_ORDER.get(f.get("severity"), 3))
    n = {s: sum(1 for f in fs if f.get("severity") == s) for s in ("BLOCK", "WARN", "INFO")}
    L = [f"{'BLOCKED' if res.get('blocked') else 'built'}: {res.get('id')} on {res.get('template')} ({res.get('brand')}), {res.get('crs')}"]
    L += [f"file: {f}" for f in (res.get("files") or [])[:6]]
    if res.get("summary"):
        L.append(str(res["summary"]))
    dev = res.get("deviations") or {}
    if dev.get("line"):
        L.append(dev["line"])
        L += [f"  {x}" for x in (dev.get("unexplained_lines") or [])[:4]]
    L.append(f"findings: {n['BLOCK']} BLOCK, {n['WARN']} WARN, {n['INFO']} INFO")
    shown = [f for f in fs if f.get("severity") in ("BLOCK", "WARN")][:DIGEST_FINDINGS]
    L += [f"  {f.get('id')} {f.get('severity')}: {str(f.get('message') or '')[:200]}" for f in shown]
    L += [str(x) for x in ((res.get("map_text") or {}).get("callout") or [])[:3]]
    L += [str(x) for x in ((res.get("story") or {}).get("callout") or [])[:3]]
    L = L[:24]
    if result_file is not None:
        L.append(f"result file: {result_file}")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--digest", action="store_true", help="4.2.0: print the build in at most 25 lines in place of the whole result (which is always written to <stem>.result.json)")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--formats")
    ap.add_argument("--brand")
    ap.add_argument("--template")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--theme", choices=["light", "dark"], help="overrides the MapSpec's theme (default light); dark only when asked for")
    ap.add_argument("--mode", choices=["print", "screen"], help=argparse.SUPPRESS)      # old name: print = light, screen = dark
    ap.add_argument("--draft", action="store_true", help="1.22.0: one small picture (<stem>_draft.png, 96 dpi) and the mute picture, in place of the full set: for looking and fixing")
    ap.add_argument("--no-mute", action="store_true", help="1.22.0: leave out <stem>_mute.png (the sheet without its words)")
    mx.add_option_flags(ap)                                                               # M16: --transparent, --bleed, --crop-marks, --pdfa, --inkscape, --outline-text
    a = ap.parse_args(argv)
    try:
        res = build(a.spec, a.out, a.formats.split(",") if a.formats else None, brand=a.brand, template=a.template, strict=a.strict, mode=(mp.THEME_MODES[a.theme] if a.theme else a.mode),
                    export_options=mx.option_flags(a), draft=a.draft, mute=not a.no_mute)
    except mp.PlanError as ex:
        print(f"build_map: {ex}", file=sys.stderr)
        return ex.code
    rfile = write_result(res, a.out)
    if a.digest:
        print("\n".join(digest_lines(res, rfile)))
        return gc.EXIT_LINT if res["blocked"] else gc.EXIT_OK
    print(json.dumps(res, indent=2, default=str))
    if rfile is not None:
        print(f"result file: {rfile}", file=sys.stderr)
    if res.get("summary"):
        print(res["summary"], file=sys.stderr)                       # 2.4.0 (T-06): "relief missing: <reason>" is never only a finding in a list
    for ln in ((res.get("map_text") or {}).get("callout") or []) + ((res.get("story") or {}).get("callout") or []):
        print(ln, file=sys.stderr)                                  # map text §11.1 and the message check (1.22.0): listed in chat, never blocking
    return gc.EXIT_LINT if res["blocked"] else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
