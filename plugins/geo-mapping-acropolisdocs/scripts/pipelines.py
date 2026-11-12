#!/usr/bin/env python3
"""pipelines: the machinery behind `/map-run` (spec 7.2): plan a pipeline, show its cost, run it only on a matching confirmation, and write a manifest.

    pipelines.py list
    pipelines.py plan  PIPELINE [--spec F] [--out DIR] [--src DIR] [--to FMT] [--option key=value ...]
    pipelines.py run   PIPELINE --confirm TOKEN [same inputs as plan] [--param key=value ...]
    pipelines.py show  RUN_ID

A plan never changes anything. It reads the inputs, hashes them, estimates the cost (network requests, files, renders, minutes) and prints a manifest whose
identifier covers the pipeline, its version, the inputs (by content hash), the options and the edition. The same inputs give the same identifier. The
confirmation token is the first twelve characters of that identifier, so what the person agreed to is what runs: change an input and the token no longer fits.

A run writes `<working folder>/runs/<run id>/run.json` (stages, findings, outputs with checksums, a reproducibility digest) and, for pipelines that deliver
files, a `manifest.json` in the deliverable folder. Stages run in order; a blocking finding stops the run before anything is packaged.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

PIPELINE_VERSION = 1
HASH_BUDGET = 500 * 1024 * 1024
OPEN_LICENSES = {"public-domain", "open", "attribution", "cc0", "cc-by", "odbl", "odc-by"}
SHARE_ALIKE = {"share-alike", "odbl", "odbl-1.0", "cc-by-sa", "cc-by-sa-4.0", "cc-by-sa-3.0"}       # 0.15.0: MAP-LIC-012
SHARE_ALIKE_NAMES = {"odbl": ("Open Database License (ODbL) 1.0", "https://opendatacommons.org/licenses/odbl/1-0/"),
                     "odbl-1.0": ("Open Database License (ODbL) 1.0", "https://opendatacommons.org/licenses/odbl/1-0/"),
                     "cc-by-sa": ("Creative Commons Attribution-ShareAlike 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
                     "cc-by-sa-4.0": ("Creative Commons Attribution-ShareAlike 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
                     "cc-by-sa-3.0": ("Creative Commons Attribution-ShareAlike 3.0", "https://creativecommons.org/licenses/by-sa/3.0/")}



def _pack_fonts_beside(p) -> list | None:
    """1.4.0: the style pack a map file was built in (its provenance sidecar's `style`), as the font names its PDF may embed."""
    import json as _json
    import re as _re
    from pathlib import Path as _P
    stem = _re.sub(r"(_methods|_geo)?$", "", _P(p).stem)
    side = _P(p).with_name(stem + ".provenance.json")
    try:
        pk = ((_json.loads(side.read_text(encoding="utf-8")).get("style") or {}).get("pack")) if side.is_file() else None
        if not pk or pk == "default":
            return None
        import map_style
        return map_style.pdf_font_names(map_style.load(pk))
    except Exception:  # noqa: BLE001
        return None

def share_alike_terms(l, rec: dict) -> dict | None:
    """The licence a share-alike layer's data travels under, named well enough to write into a data package, or None when it cannot be named
    (a provenance that says only 'share-alike', with no library entry and no licence name or address)."""
    lic = str(rec.get("license") or "").strip().lower()
    ent = (l.entry or {}).get("license") or {}
    prov = getattr(l, "provenance", None) or (l.spec.get("provenance") or {})
    name, url = SHARE_ALIKE_NAMES.get(lic, (None, None))
    name = prov.get("license_name") or name or (ent.get("note") or "").split(":")[0].strip() or None
    url = prov.get("license_url") or rec.get("license_url") or url
    if not name or name.lower() == "share-alike":
        return None
    return {"layer": l.id, "title": l.title or l.id, "license": name, "url": url,
            "attribution": prov.get("attribution") or rec.get("attribution_text") or ent.get("attribution_text"), "note": ent.get("note")}


def write_share_alike(ddir: Path, terms: list[dict]) -> Path:
    """data/LICENSE-SHARE-ALIKE.txt: each share-alike layer, its licence and credit, and what the licence asks of whoever passes the data on."""
    lines = ["Share-alike data in this package", "", "These layers are under share-alike licences. Whoever shares this data, or a database made from it,",
             "must share it under the same licence and keep the credits below. A rendered map is not a derived database.", ""]
    for t in terms:
        lines += [f"Layer: {t['title']} ({t['layer']})", f"Licence: {t['license']}" + (f" <{t['url']}>" if t.get("url") else ""),
                  f"Credit: {t['attribution'] or 'see the provenance record'}"] + ([f"Note: {t['note']}"] if t.get("note") else []) + [""]
    p = Path(ddir) / "LICENSE-SHARE-ALIKE.txt"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p
DETERMINISTIC = {".svg", ".json", ".md", ".geojson", ".csv", ".yaml", ".txt"}
SEV = {"BLOCK": 0, "WARN": 1, "INFO": 2}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def blocked(fs) -> bool:
    return any(f["severity"] == "BLOCK" for f in fs)


def today() -> str:
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


def now() -> str:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return (fixed + "T00:00:00Z") if fixed else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------------------------- hashing
def file_format(f) -> str:
    """A built map file's format: geopdf and geotiff for the georeferenced copies (NAME_geo.pdf, NAME_geo.tif), else the suffix."""
    n = Path(f).name.lower()
    if n.endswith("_geo.pdf"):
        return "geopdf"
    if n.endswith("_geo.tif"):
        return "geotiff"
    return Path(f).suffix.lstrip(".").lower()


def file_role(f) -> str:
    """The package role of a built map file: `georeferenced` for a GeoPDF or GeoTIFF (0.26.0, TODO M8), else `map`."""
    return "georeferenced" if file_format(f) in ("geopdf", "geotiff") else "map"


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_path(p: Path, budget: list[int]) -> dict:
    """A content hash of a file, or of a folder (relative names, sizes, and content while the budget lasts)."""
    p = Path(p)
    if p.is_file():
        sz = p.stat().st_size
        if budget[0] >= sz:
            budget[0] -= sz
            return {"kind": "file", "size": sz, "sha256": sha_file(p)}
        return {"kind": "file", "size": sz, "sha256": None}
    if p.is_dir():
        h = hashlib.sha256()
        n = 0
        for q in sorted(x for x in p.rglob("*") if x.is_file() and not any(part.startswith(".") for part in x.relative_to(p).parts)):
            sz = q.stat().st_size
            h.update(str(q.relative_to(p)).replace("\\", "/").encode() + b"\0" + str(sz).encode() + b"\0")
            if budget[0] >= sz:
                budget[0] -= sz
                h.update(sha_file(q).encode())
            n += 1
            if n > 5000:
                break
        return {"kind": "folder", "files": n, "sha256": h.hexdigest()}
    return {"kind": "missing", "sha256": None}


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def edition_id() -> dict:
    try:
        m = gc.build_manifest()
        return {"plugin": m.get("plugin_name"), "version": m.get("version"), "digest": m.get("content_digest"), "library": m.get("library_version")}
    except (FileNotFoundError, ValueError):
        return {"plugin": None}


# ---------------------------------------------------------------------------------------------------------------- context
@dataclass
class Ctx:
    pipeline: str
    inputs: dict
    options: dict
    params: dict
    run_dir: Path
    outdir: Path | None
    http: object = None
    results: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    files: list = field(default_factory=list)          # {"path","role"}
    notes: list = field(default_factory=list)

    def add_file(self, path: Path, role: str) -> None:
        self.files.append({"path": str(path), "role": role})


def run_root() -> Path:
    return gc.workdir() / "runs"


# ---------------------------------------------------------------------------------------------------------------- cost
def cost_text(c: dict) -> str:
    bits = []
    if c.get("network_requests"):
        bits.append(f"about {c['network_requests']} request(s) to a public service")
    if c.get("features"):
        bits.append(f"up to {c['features']:,} features")
    if c.get("files_read"):
        bits.append(f"{c['files_read']} file(s) read ({c.get('bytes_in', 0) / 1e6:.1f} MB)")
    if c.get("renders"):
        bits.append(f"{c['renders']} map render(s)" + (f" ({c['frames']} of them animation frames)" if c.get("frames") else ""))
    if c.get("files_written"):
        bits.append(f"about {c['files_written']} file(s) written")
    bits += [str(x) for x in c.get("lines") or []]          # a pipeline's own cost items (webmap-build: page size, engine, basemap, network)
    if c.get("style_pack") and c["style_pack"] != "default":   # 1.4.0: the pack is fixed for the run; a run approved for one pack never runs another
        bits.append(f"style pack {c['style_pack']}")
    bits.append(f"roughly {max(1, round(c.get('est_seconds', 1) / 60))} minute(s)")
    return "; ".join(bits)


# ---------------------------------------------------------------------------------------------------------------- map-build
def _spec(path) -> dict:
    return gc.load_mapspec(Path(path))


def _fs_md(findings: list[dict]) -> str:
    c = {s: sum(1 for f in findings if f["severity"] == s) for s in ("BLOCK", "WARN", "INFO")}
    lines = ["# Lint report", "", f"{c['BLOCK']} BLOCK, {c['WARN']} WARN, {c['INFO']} INFO.", ""]
    if findings:
        lines += ["| Severity | Rule | Where | Message |", "|---|---|---|---|"]
        for f in sorted(findings, key=lambda f: (SEV.get(f["severity"], 3), f["id"], f.get("message", ""))):
            msg = str(f["message"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {f['severity']} | {f['id']} | {f.get('layer') or ''} | {msg} |")
    else:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def methods_md(meth: dict) -> str:
    L = [f"# Data and methods: {meth['title']}", "", f"**Message.** {meth['message']}", "", "## Data", "", "| Layer | Source | Date | License | Rows |", "|---|---|---|---|---|"]
    for l in meth["layers"]:
        L.append(f"| {l['layer']} | {l['source']} | {l['date']} | {l['license']} | {l['rows']} |")
    for l in meth["layers"]:
        if l.get("steps"):
            L += ["", f"### {l['layer']}", ""] + [f"- {s}" for s in l["steps"]]
        if l.get("classification"):
            c = l["classification"]
            L.append(f"- Classified by {c['method']} into {c['classes']} classes on '{c['field']}' ({c['palette']} palette)" + (f"; breaks {c['breaks']}" if c.get("breaks") else "") + ".")
    p = meth["projection"]
    L += ["", "## Projection", "", f"{p['name']} ({p['code']}){'; equal-area' if p.get('equal_area') else ''}. {p.get('why') or ''}".strip()]
    if meth.get("notes"):
        L += ["", "## Notes", ""] + [f"- {n}" for n in meth["notes"]]
    if meth.get("attribution"):
        L += ["", "## Attribution", ""] + [f"- {a}" for a in meth["attribution"]]
    r = meth["reproducibility"]
    L += ["", "## Reproducibility", "", f"- MapSpec SHA-256: {r['mapspec_sha256']}", f"- Built: {r['built']}", f"- Plugin: {r['plugin']}", f"- Library: {r['library_version']}"]
    return "\n".join(L) + "\n"


def _map_formats(formats) -> list:
    """The map formats of a `formats` list: the styled data formats (pmtiles, kmz, gpkg, qgz ...) go to the data package, not the build (M14)."""
    import data_outputs
    return [f for f in (formats or []) if str(f).lower() not in data_outputs.DATA_FORMATS]


def plan_map_build(inp: dict, opt: dict) -> dict:
    spec = _spec(inp["spec"])
    n_formats = len(_map_formats(opt.get("formats") or spec.get("export", {}).get("formats") or ["pdf", "png", "svg"]))
    layers = spec.get("layers") or []
    svc = [l for l in layers if (l.get("source") or {}).get("type") in ("service", "user-run")]
    import data_outputs
    n_styled = len(data_outputs.requested(opt)[0]) if opt.get("data_package") else 0
    return {"renders": 1, "files_written": n_formats + 5 + (len(layers) if opt.get("data_package") else 0) + n_styled, "network_requests": 0, "est_seconds": 20 + 10 * n_formats,
            "note": ("The build never fetches: " + f"{len(svc)} layer(s) are drawn from existing captures." if svc else "The build never fetches.")}


def safe_build(spec_path, out, formats, **kw):
    """bm.build that leaves the MapSpec as it found it (a build stamps the resolved brand into it). Returns (result, stamped bytes)."""
    import build_map as bm
    spec_path = Path(spec_path)
    original = spec_path.read_bytes()
    try:
        res = bm.build(str(spec_path), str(out), list(formats), **kw)
    finally:
        stamped = spec_path.read_bytes()
        if stamped != original:
            spec_path.write_bytes(original)
    return res, stamped


def stage_probe(c: Ctx) -> dict:
    tier = "T0"
    try:
        tier = json.loads((gc.workdir() / "env.json").read_text()).get("tier") or "T0"
    except (OSError, ValueError):
        pass
    c.results["tier"] = tier
    return {"ok": True, "summary": f"tier {tier}", "findings": []}


def stage_spec_lint(c: Ctx) -> dict:
    import mapspec_lint as ml
    fs = ml.lint_spec(_spec(c.inputs["spec"]))
    return {"ok": not blocked(fs), "summary": f"{len(fs)} finding(s)", "findings": fs}


def stage_build(c: Ctx) -> dict:
    import map_plan as mp
    formats = _map_formats(c.options.get("formats") or _spec(c.inputs["spec"]).get("export", {}).get("formats") or ["pdf", "png", "svg"])
    bdir = c.run_dir / "build"
    shutil.rmtree(bdir, ignore_errors=True)
    try:
        res, c.results["spec_stamped"] = safe_build(c.inputs["spec"], bdir, formats, brand=c.options.get("brand"), template=c.options.get("template"), strict=False, keep=True)
    except mp.PlanError as ex:
        return {"ok": False, "summary": str(ex).splitlines()[0][:200], "findings": [finding("MAP-RUN-003", "BLOCK", str(ex)[:600])]}
    c.results["build"] = res
    c.results["objects"] = res.pop("_objects", None)
    mt = res.get("map_text") or {}
    if mt:
        c.results["map_text"] = mt                       # map text §11.1: the run's chat summary lists these, never blocking
    note = f"; {mt['callout'][0][0].lower()}{mt['callout'][0][1:]}" if mt.get("callout") else ""
    return {"ok": not res["blocked"], "summary": f"{len(res['files'])} file(s), {len(res['findings'])} finding(s){note}", "findings": res["findings"]}


def stage_package(c: Ctx) -> dict:
    res = c.results["build"]
    mid = res["id"]
    dest = Path(c.outdir) / mid
    if dest.exists():
        if not c.options.get("overwrite"):
            return {"ok": False, "summary": "the deliverable folder exists", "findings": [finding("MAP-RUN-004", "BLOCK", f"{dest} already exists; choose another folder or set overwrite")]}
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in res["files"]:
        shutil.copy2(f, dest / Path(f).name)
        c.add_file(dest / Path(f).name, file_role(f))
    (dest / "mapspec.yaml").write_bytes(c.results.get("spec_stamped") or Path(c.inputs["spec"]).read_bytes())
    c.add_file(dest / "mapspec.yaml", "spec")
    shutil.copy2(res["provenance"], dest / "provenance.json")
    c.add_file(dest / "provenance.json", "provenance")
    for tf in (res.get("terms") or {}).get("files") or []:       # 1.3.0: the terms register (and a share-alike notice) beside the outputs
        shutil.copy2(tf, dest / Path(tf).name)
        c.add_file(dest / Path(tf).name, "terms")
    allf = list(c.results.get("pre_findings") or []) + list(c.results["all_findings"] if "all_findings" in c.results else res["findings"])
    (dest / "lint_report.md").write_text(_fs_md(allf))
    c.add_file(dest / "lint_report.md", "lint")
    obj = c.results.get("objects") or {}
    if obj.get("methods"):
        (dest / "methods.md").write_text(methods_md(obj["methods"]))
        c.add_file(dest / "methods.md", "methods")
    for extra in (c.results.get("gen") or {}).get("extras", []):
        shutil.copy2(extra, dest / Path(extra).name)
        c.add_file(dest / Path(extra).name, "summary")
    c.results["dest"] = str(dest)
    return {"ok": True, "summary": f"packaged in {dest.name}/", "findings": []}


def stage_data_package(c: Ctx) -> dict:
    import data_outputs
    styled, unknown = data_outputs.requested(c.options)
    if not c.options.get("data_package"):
        fs = [finding("MAP-RUN-003", "WARN", f"{', '.join(styled)} come with the data package; set data_package=true to write them")] if styled else []
        return {"ok": True, "summary": "not requested", "findings": fs, "skipped": True}
    if unknown:
        return {"ok": False, "summary": "bad data format", "findings": [finding("MAP-RUN-003", "BLOCK", f"{', '.join(unknown)}: not a data package format; "
                                                                                 f"the choices are {', '.join(data_outputs.DATA_FORMATS)}")]}
    import geo_io
    obj = c.results.get("objects") or {}
    plan = obj.get("plan")
    if plan is None:
        return {"ok": False, "summary": "no plan", "findings": [finding("MAP-RUN-003", "BLOCK", "the build objects are not available for the data package")]}
    recs = {r["layer_id"]: r for r in obj["records"]}
    fs, wrote, passed, rasters, sa_terms = [], 0, [], [], []
    ddir = Path(c.results["dest"]) / "data"
    ddir.mkdir(exist_ok=True)
    for l in plan.layers:
        if l.role == "context":              # the bundled context pack (M10) is reference data under the map, not the map's data
            continue
        shippable = l.kind == "raster" and l.role == "raster" and (l.raster or {}).get("src") != "service" and (l.raster or {}).get("image")
        contour = l.role == "contour" and getattr(l, "gdf", None) is not None and len(l.gdf)
        if (l.kind == "raster" or l.role == "contour") and not shippable and not contour:      # relief and a service picture are not data to hand on
            fs.append(finding("MAP-RUN-003", "INFO", f"layer '{l.id}' is a {l.role} drawn from a raster; the data package carries vector layers, "
                              "contour lines and raster windows (role raster) only", l.id))
            continue
        rec = recs.get(l.id, {})
        lic = str(rec.get("license") or "").strip().lower()
        conf = l.confidentiality
        ent_lic = ((l.entry or {}).get("license") or {})
        sa = lic in SHARE_ALIKE or ent_lic.get("class") == "share-alike"
        if (lic not in OPEN_LICENSES and not sa) or ent_lic.get("redistribute_ok") is False:
            if not c.options.get("allow_restricted_data"):
                fs.append(finding("MAP-LIC-003", "BLOCK", f"layer '{l.id}': its license ('{lic or 'none stated'}') does not allow the data to be redistributed in a package; set allow_restricted_data only if you hold the right to share it", l.id))
                continue
        if sa:
            terms = share_alike_terms(l, rec)
            if terms is None:
                fs.append(finding("MAP-LIC-012", "BLOCK", f"layer '{l.id}' is share-alike but its licence is not named, so the package cannot carry it: give "
                                  "provenance.license_name (ODbL 1.0, CC BY-SA 4.0) or license_url", l.id))
                continue
            sa_terms.append(terms)
            fs.append(finding("MAP-LIC-003", "WARN", f"layer '{l.id}' is share-alike ({terms['license']}): the data package must be shared under the same terms", l.id))
        if conf not in ("public", None, "") and not c.options.get("include_confidential"):
            fs.append(finding("MAP-LIC-007", "BLOCK", f"layer '{l.id}' is {conf}: its raw coordinates go into a data package only after explicit confirmation (include_confidential)", l.id))
            continue
        if shippable:                        # M16: the raster's window as the map read it, a GeoTIFF in the map's CRS (licence checked above)
            import data_outputs
            try:
                rw = data_outputs.raster_window(plan, l, ddir / f"{l.id}.tif")
            except Exception as ex:  # noqa: BLE001 - a window that cannot be read again is a finding, not a crash
                fs.append(finding("MAP-RUN-003", "WARN", f"layer '{l.id}': its raster window could not be written: {str(ex)[:160]}", l.id))
                continue
            c.add_file(ddir / f"{l.id}.tif", "data")
            rasters.append((l, rw))
            wrote += 1
            continue
        g = l.gdf.to_crs("EPSG:4326")
        if contour:                          # 0.24.0 (TODO M16): contour lines go as data, each with its elevation and whether it is an index line
            keep = [x for x in ("elevation", "index") if x in g.columns]
            g = g[keep + [g.geometry.name]]
        for fmt, ext in (("gpkg", "gpkg"), ("geojson", "geojson")):
            geo_io.write(g, ddir / f"{l.id}.{ext}", fmt, layer=l.id if fmt == "gpkg" else None, overwrite=True)
            c.add_file(ddir / f"{l.id}.{ext}", "data")
        wrote += 1
        if contour:
            fs.append(finding("MAP-RUN-003", "INFO", f"layer '{l.id}': {len(g)} contour line(s) written with their elevations; the styled outputs leave them out", l.id))
            continue
        passed.append((l, g))
    if not wrote and not any(f["severity"] == "BLOCK" for f in fs):
        fs.append(finding("MAP-RUN-003", "WARN", "no layer data was written"))
    written_ids = {l.id for l, _ in passed} | {l.id for l, _ in rasters}
    sa_written = [t for t in sa_terms if t["layer"] in written_ids]
    if sa_written:                                   # 0.15.0 MAP-LIC-012: the package carries the licence of its share-alike data, and says so
        lp = write_share_alike(ddir, sa_written)
        c.add_file(lp, "license")
        text = lp.read_text(encoding="utf-8") if lp.is_file() else ""
        missing = [t["layer"] for t in sa_written if f"({t['layer']})" not in text or t["license"] not in text]
        if missing:
            fs.append(finding("MAP-LIC-012", "BLOCK", f"the share-alike licence file does not name layer(s) {', '.join(missing)}", missing))
        else:
            fs.append(finding("MAP-LIC-012", "INFO", f"{lp.name} names the licence of {len(sa_written)} share-alike layer(s): {', '.join(t['layer'] for t in sa_written)}"))
    summary = f"{wrote} layer(s) written"
    if wrote:                                        # 1.3.0 (R1-TERMS): the register now says the folder carries these layers' data
        import terms_register as tr
        res = c.results.get("build") or {}
        stem = res.get("stem") or plan.spec["id"]
        rend = obj.get("rendered")
        tdoc, tfs, tfiles = tr.for_deliverable(Path(c.results["dest"]), obj["records"], deliverable="data package", stem=stem,
                                               outputs={"svg": getattr(rend, "svg", None), "data": sorted(p_.name for p_ in ddir.iterdir())},
                                               embedded=written_ids, notice=False, title=getattr(rend, "title", None))
        fs += [f for f in tfs if f["id"] != "MAP-LIC-016"]           # MAP-LIC-012 already speaks for the package's share-alike data
    # M14: the styled outputs, from the layers the gates above let through, and only when none was refused (a partial KMZ or project would mislead)
    if styled and (passed or rasters) and not blocked(fs):
        out = data_outputs.write_package(plan, passed, ddir, styled, options=c.options, rasters=rasters)
        for path, role in out["files"]:
            c.add_file(Path(path), role)
        fs += out["findings"]
        if out["notes"]:
            fs.append(finding("MAP-OUT-010", "INFO", "styled data outputs: " + "; ".join(out["notes"]), out["notes"]))
        summary += f"; styled: {', '.join(Path(p).name for p, _ in out['files'])}"
    elif styled:
        fs.append(finding("MAP-RUN-003", "INFO", f"the styled outputs ({', '.join(styled)}) were not written: a layer was refused above or none was written"))
    return {"ok": not blocked(fs), "summary": summary, "findings": fs}


# ---------------------------------------------------------------------------------------------------------------- map-qa
def plan_map_qa(inp: dict, opt: dict) -> dict:
    files = list(inp.get("files") or [])
    return {"renders": 1 if inp.get("spec") else 0, "files_read": len(files), "bytes_in": sum(Path(f).stat().st_size for f in files if Path(f).is_file()), "est_seconds": 30 + 5 * len(files),
            "network_requests": 0}


CHECKLIST = (
    ("overlaps and clipped text", ("MAP-CART-007", "MAP-CART-009", "MAP-A11Y-001")),
    ("legend readability", ("MAP-CART-002", "MAP-CART-004", "MAP-A11Y-002", "MAP-A11Y-003")),
    ("hierarchy: title and message", ("MAP-CART-013", "MAP-SPEC-002")),
    ("brand look", ("MAP-BRAND-001", "MAP-BRAND-002", "MAP-BRAND-003", "MAP-OUT-008")),
    ("extent framing", ("MAP-CART-009", "MAP-CART-012")),
)
FIX_FAMILIES = ("MAP-CART", "MAP-A11Y", "MAP-BRAND", "MAP-LIC", "MAP-OUT", "MAP-DATA", "MAP-SPEC", "MAP-CRS")


def stage_qa_spec(c: Ctx) -> dict:
    if not c.inputs.get("spec"):
        return {"ok": True, "summary": "no MapSpec given", "findings": [], "skipped": True}
    import map_plan as mp
    import mapspec_lint as ml
    fs = ml.lint_spec(_spec(c.inputs["spec"]))
    if blocked(fs):
        c.results["qa_findings"] = fs
        return {"ok": True, "summary": "the MapSpec has blocking problems; the build was not attempted", "findings": fs}
    bdir = c.run_dir / "qa-build"
    shutil.rmtree(bdir, ignore_errors=True)
    try:
        res, _ = safe_build(c.inputs["spec"], bdir, ["svg", "png"], strict=False, keep=True)
    except mp.PlanError as ex:
        f = [finding("MAP-RUN-003", "BLOCK", str(ex)[:600])]
        c.results["qa_findings"] = fs + f
        return {"ok": True, "summary": "the build failed", "findings": f}
    allf = fs + res["findings"]
    c.results["qa_findings"] = allf
    c.results["qa_build"] = res
    png = next((f for f in res["files"] if f.endswith(".png")), None)
    if png:
        dst = c.run_dir / "preview.png"
        shutil.copy2(png, dst)
        c.add_file(dst, "preview")
    return {"ok": True, "summary": f"{len(allf)} finding(s)", "findings": allf}


def stage_qa_files(c: Ctx) -> dict:
    files = [Path(f) for f in (c.inputs.get("files") or [])]
    if not files:
        return {"ok": True, "summary": "no files given", "findings": [], "skipped": True}
    import output_lint as ol
    fs = []
    for p in files:
        ext = p.suffix.lower()
        if not p.is_file():
            fs.append(finding("MAP-OUT-002", "BLOCK", f"{p.name} was not found"))
            continue
        if ext == ".svg":
            text = p.read_text(encoding="utf-8", errors="replace")
            fs += [dict(f, layer=p.name) for f in ol.check_svg(text, p.name) + ol.check_leaks(text, p.name)]
        elif ext == ".pdf":
            import pdf_stack
            rd = pdf_stack.read_pdf(p)
            wpt, hpt = pdf_stack.page_sizes_pt(rd)[0]
            size = (wpt * 25.4 / 72, hpt * 25.4 / 72)
            npages = len(rd.pages)
            fs += [dict(f, layer=p.name) for f in ol.check_pdf(str(p), size, fonts_allowed=_pack_fonts_beside(p))]
            if npages != 1:
                fs.append(finding("MAP-OUT-002", "WARN", f"{p.name} has {npages} pages; a map is one page", p.name))
        elif ext == ".png":
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
                dpi = (im.info.get("dpi") or (300, 300))[0] or 300
            fs += [dict(f, layer=p.name) for f in ol.check_png(str(p), (w / dpi * 25.4, h / dpi * 25.4), float(dpi))]
        else:
            fs.append(finding("MAP-OUT-002", "INFO", f"{p.name}: only svg, pdf and png are checked", p.name))
    c.results["qa_file_findings"] = fs
    return {"ok": True, "summary": f"{len(files)} file(s), {len(fs)} finding(s)", "findings": fs}


def stage_verdict(c: Ctx) -> dict:
    spec_f = c.results.get("qa_findings") or []
    file_f = c.results.get("qa_file_findings") or []
    allf = spec_f + file_f
    ids = {f["id"] for f in allf if f["severity"] in ("WARN", "BLOCK")}
    checklist = []
    for name, rules in CHECKLIST:
        hit = sorted(ids & set(rules))
        checklist.append({"item": name, "status": "fail" if hit else "pass", "evidence": hit})
    msg = str((_spec(c.inputs["spec"]).get("message") if c.inputs.get("spec") else "") or "")
    if c.inputs.get("spec"):
        ok = 3 <= len(msg.split()) <= 30
        checklist.append({"item": "one-sentence test (message is a single, short sentence)", "status": "pass" if ok else "fail", "evidence": [] if ok else ["MAP-SPEC-002"]})
    checklist.append({"item": "a stranger can state the message in five seconds", "status": "needs-eyes", "evidence": ["preview.png"] if c.inputs.get("spec") else []})
    if any(f["severity"] == "BLOCK" for f in allf):
        verdict = "BLOCK"
    elif any(f["severity"] == "WARN" and f["id"].startswith(FIX_FAMILIES) for f in allf) or any(x["status"] == "fail" for x in checklist):
        verdict = "FIX"
    else:
        verdict = "SHIP"
    why = {"BLOCK": [f for f in allf if f["severity"] == "BLOCK"], "FIX": [f for f in allf if f["severity"] == "WARN" and f["id"].startswith(FIX_FAMILIES)]}.get(verdict, [])
    report = {"verdict": verdict, "counts": {s: sum(1 for f in allf if f["severity"] == s) for s in ("BLOCK", "WARN", "INFO")}, "checklist": checklist,
              "reasons": [{"id": f["id"], "message": f["message"]} for f in why][:20], "visual_review": "pending: open preview.png and answer the needs-eyes items" if c.inputs.get("spec") else "not applicable (files only)"}
    c.results["verdict"] = report
    out = c.run_dir / "qa_report.json"
    out.write_text(json.dumps(report, indent=2))
    c.add_file(out, "qa")
    md = [f"# QA verdict: {verdict}", "", f"{report['counts']['BLOCK']} BLOCK, {report['counts']['WARN']} WARN, {report['counts']['INFO']} INFO.", "", "## Checklist", ""]
    md += [f"- [{'x' if x['status'] == 'pass' else ' '}] {x['item']} ({x['status']}{': ' + ', '.join(x['evidence']) if x['evidence'] else ''})" for x in checklist]
    if report["reasons"]:
        md += ["", "## Why", ""] + [f"- {r['id']}: {r['message']}" for r in report["reasons"]]
    md += ["", "## All findings", "", _fs_md(allf)]
    (c.run_dir / "qa_report.md").write_text("\n".join(md))
    c.add_file(c.run_dir / "qa_report.md", "qa")
    return {"ok": True, "summary": verdict, "findings": []}


# ---------------------------------------------------------------------------------------------------------------- format-batch
SKIP_DIRS = {"__MACOSX"}


def scan_geodata(src: Path, recursive: bool) -> list[Path]:
    import geo_io
    exts = {e for f in geo_io.FORMATS.values() for e in f.exts}
    it = src.rglob("*") if recursive else src.glob("*")
    out = []
    for p in sorted(it):
        if not p.is_file() or any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(src).parts):
            continue
        if p.suffix.lower() in exts:
            out.append(p)
    return out


def plan_format_batch(inp: dict, opt: dict) -> dict:
    src = Path(inp["src"])
    files = scan_geodata(src, bool(opt.get("recursive")))
    b = sum(f.stat().st_size for f in files)
    return {"files_read": len(files), "bytes_in": b, "files_written": len(files), "est_seconds": 5 + int(b / 15e6) + 2 * len(files), "network_requests": 0}


def stage_scan(c: Ctx) -> dict:
    src = Path(c.inputs["src"])
    files = scan_geodata(src, bool(c.options.get("recursive")))
    c.results["scan"] = files
    return {"ok": True, "summary": f"{len(files)} geodata file(s)", "findings": [] if files else [finding("MAP-RUN-003", "WARN", "no geodata files were found")]}


def stage_fb_plan(c: Ctx) -> dict:
    import geo_io
    src, dst = Path(c.inputs["src"]), Path(c.outdir)
    to = c.options.get("to", "gpkg")
    if to not in geo_io.FORMATS or not geo_io.FORMATS[to].write:
        return {"ok": False, "summary": "bad target", "findings": [finding("MAP-RUN-003", "BLOCK", f"'{to}' is not a format this plugin writes ({', '.join(k for k, f in geo_io.FORMATS.items() if f.write)})")]}
    ext = geo_io.FORMATS[to].exts[0]
    plan, seen, fs = [], {}, []
    for p in c.results["scan"]:
        try:
            layers = geo_io.list_layers(p)
        except Exception as ex:  # noqa: BLE001
            plan.append({"source": str(p), "skip": f"cannot be read: {str(ex)[:120]}"})
            continue
        rel = p.relative_to(src).with_suffix("")
        multi = len(layers) > 1
        for ly in (layers if multi else [None]):
            name = rel.as_posix().replace("/", "__") + (f"__{ly['name']}" if multi and ly else "")
            target = dst / f"{name}{ext}"
            if target in seen:
                fs.append(finding("MAP-RUN-004", "BLOCK", f"{p.name} and {Path(seen[target]).name} would both write {target.name}"))
            seen[target] = str(p)
            if target.exists() and not c.options.get("overwrite"):
                fs.append(finding("MAP-RUN-004", "BLOCK", f"{target.name} already exists; set overwrite or choose another folder"))
            plan.append({"source": str(p), "layer": ly["name"] if ly and multi else None, "target": str(target)})
    c.results["fb_plan"] = plan
    return {"ok": not blocked(fs), "summary": f"{sum(1 for x in plan if x.get('target'))} conversion(s) planned", "findings": fs}


def stage_fb_convert(c: Ctx) -> dict:
    import geo_convert
    import geo_io
    Path(c.outdir).mkdir(parents=True, exist_ok=True)
    rows, fs = [], []
    to = c.options.get("to", "gpkg")
    for item in c.results["fb_plan"]:
        if item.get("skip"):
            rows.append({"source": Path(item["source"]).name, "status": "skipped", "why": item["skip"]})
            fs.append(finding("MAP-RUN-003", "WARN", f"{Path(item['source']).name}: {item['skip']}"))
            continue
        try:
            r = geo_convert.convert([item["source"]], item["target"], fmt=to, layer=item.get("layer"), t_srs=c.options.get("crs") or None, overwrite=bool(c.options.get("overwrite")),
                                    crs_assume=c.options.get("crs_assume"))
        except (geo_io.GeoIOError, geo_convert.ConvertError) as ex:
            rows.append({"source": Path(item["source"]).name, "status": "failed", "why": str(ex)[:200]})
            fs.append(finding("MAP-RUN-003", "WARN", f"{Path(item['source']).name}: {str(ex)[:160]}"))
            continue
        for f in r["findings"]:
            if f["severity"] in ("BLOCK", "WARN"):
                fs.append(dict(f, layer=Path(item["source"]).name))
        rows.append({"source": Path(item["source"]).name, "layer": item.get("layer"), "target": Path(item["target"]).name, "features": r["features"], "status": "blocked" if r["blocked"] else "converted",
                     "ogr2ogr": r["ogr2ogr"]})
        if not r["blocked"]:
            for o in r["outputs"]:
                c.add_file(Path(o["path"]), "data")
    c.results["fb_rows"] = rows
    return {"ok": True, "summary": f"{sum(1 for r in rows if r['status'] == 'converted')} converted, {sum(1 for r in rows if r['status'] != 'converted')} not", "findings": fs}


def stage_fb_verify(c: Ctx) -> dict:
    import geo_io
    fs = []
    for row in c.results["fb_rows"]:
        if row["status"] != "converted":
            continue
        tgt = Path(c.outdir) / row["target"]
        try:
            back = geo_io.read(tgt)
        except geo_io.GeoIOError as ex:
            row["status"] = "failed-verify"
            fs.append(finding("MAP-OUT-005", "BLOCK", f"{row['target']} cannot be read back: {str(ex)[:160]}"))
            continue
        if len(back.gdf) != row["features"]:
            row["status"] = "failed-verify"
            fs.append(finding("MAP-OUT-005", "BLOCK", f"{row['target']}: {len(back.gdf)} features read back, {row['features']} written"))
        else:
            row["verified"] = True
    return {"ok": not blocked(fs), "summary": f"{sum(1 for r in c.results['fb_rows'] if r.get('verified'))} verified", "findings": fs}


def stage_fb_report(c: Ctx) -> dict:
    rows = c.results["fb_rows"]
    out = Path(c.outdir)
    (out / "format_batch_report.json").write_text(json.dumps({"to": c.options.get("to", "gpkg"), "files": rows}, indent=2))
    md = ["# Format batch report", "", f"Target format: {c.options.get('to', 'gpkg')}.", "", "| Source | Layer | Result | Features | Status |", "|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['source']} | {r.get('layer') or ''} | {r.get('target') or ''} | {r.get('features', '')} | {r['status']}{' (verified)' if r.get('verified') else ''} |")
    md += ["", "## Equivalent ogr2ogr commands", ""] + [f"- `{r['ogr2ogr']['command']}`" if isinstance(r.get("ogr2ogr"), dict) and r["ogr2ogr"].get("command") else f"- {r['source']}: {r.get('ogr2ogr')}" for r in rows if r.get("ogr2ogr")]
    (out / "format_batch_report.md").write_text("\n".join(md) + "\n")
    c.add_file(out / "format_batch_report.json", "report")
    c.add_file(out / "format_batch_report.md", "report")
    return {"ok": True, "summary": "report written", "findings": []}


# ---------------------------------------------------------------------------------------------------------------- data-refresh
def plan_data_refresh(inp: dict, opt: dict) -> dict:
    spec = _spec(inp["spec"])
    svc = [l for l in spec.get("layers", []) if (l.get("source") or {}).get("type") == "service"]
    reqs = 0
    base = Path(inp["spec"]).parent
    import map_plan as mp
    for l in svc:
        try:
            rec = json.loads(mp.find_capture(str(l["source"].get("capture")), base).read_text())
            reqs += 3 + len(rec.get("pages") or [1])
        except Exception:  # noqa: BLE001
            reqs += 4
    return {"network_requests": reqs, "renders": 1 if opt.get("rebuild", True) else 0, "files_written": 6, "est_seconds": 20 + 4 * reqs}


def stage_dr_read(c: Ctx) -> dict:
    import map_plan as mp
    spec_path = Path(c.inputs["spec"])
    spec = _spec(spec_path)
    rows, fs = [], []
    for l in spec.get("layers", []):
        s = l.get("source") or {}
        t = s.get("type")
        row = {"layer": l["id"], "type": t}
        if t == "service":
            try:
                cp = mp.find_capture(str(s.get("capture")), spec_path.parent)
                rec = json.loads(cp.read_text())
                row.update(capture=rec["id"], route=rec.get("route"), captured=str(rec.get("finished_at"))[:10], refreshable=rec.get("route") == "mcp")
            except mp.PlanError as ex:
                row.update(problem=str(ex)[:200])
                fs.append(finding("MAP-RUN-003", "WARN", f"layer '{l['id']}': {str(ex)[:160]}"))
        elif t == "user-run":
            route = None
            try:
                route = json.loads(mp.find_capture(str(s.get("capture")), spec_path.parent).read_text()).get("route")
            except Exception:  # noqa: BLE001
                pass
            if route == "device-run":
                row.update(refreshable=False, route=route, note="an approved run on your computer: plan the same run again with approved_run.py (a standing approval covers the same query shape for up to 90 days), finish it, then compare")
            else:
                row.update(refreshable=False, note="a capture from your own kit: run the kit again, ingest it, then compare")
        elif t == "file":
            p = (spec_path.parent / s["path"])
            row.update(sha256=sha_file(p) if p.is_file() else None, missing=not p.is_file())
            lib = _library_download_check(l, p, row.get("sha256"))          # 0.25.0 (TODO M5): a replaced download is seen
            if lib:
                row["library"] = lib
                if lib["same"] is False:
                    fs.append(finding("MAP-RUN-003", "WARN", f"layer '{l['id']}': the file is not the download the library recorded for {lib['entry']} "
                                      f"(rev {lib['rev']}): the publisher may have replaced it (check its vintage) or the file was edited", l["id"]))
        elif t == "ref":
            row.update(ref=s.get("id"))
        rows.append(row)
    c.results["dr_layers"] = rows
    return {"ok": True, "summary": f"{len(rows)} layer(s) read", "findings": fs}


def _library_download_check(layer: dict, path: Path, sha: str | None) -> dict | None:
    """A file layer whose provenance names a library entry (`library_ref`) that records its download's SHA-256 (`source.file_sha256`): whether the
    file is that download. Only an archive is compared (the entry's hash is of the publisher's zip, not of a file taken out of it)."""
    ref = (layer.get("provenance") or {}).get("library_ref")
    if not ref or not sha:
        return None
    try:
        import ref_library as rl
        e = next((x for x in rl.all_entries("all") if x.get("id") == ref), None)
    except Exception:  # noqa: BLE001
        return None
    want = ((e or {}).get("source") or {}).get("file_sha256")
    if not want:
        return None
    if path.suffix.lower() not in (".zip", ".gz", ".7z"):
        return {"entry": ref, "rev": e.get("rev"), "same": None, "note": "not compared: the library's hash is of the download archive, and this is a file taken out of it"}
    return {"entry": ref, "rev": e.get("rev"), "same": sha == want, "recorded": want[:12], "observed": sha[:12]}


def stage_dr_fetch(c: Ctx) -> dict:
    import service_refresh as srf
    reports, fs = [], []
    for row in c.results["dr_layers"]:
        if row["type"] != "service" or not row.get("refreshable"):
            continue
        try:
            http = c.http
            if http is None:
                import service_query as sq
                rec = json.loads(srf.locate(row["capture"]).read_text())
                http = sq.make_http(sq.need_mcp_profile(rec["profile"]))
            r = srf.refresh(http, row["capture"], apply=bool(c.params.get("apply")))
        except Exception as ex:  # noqa: BLE001
            row["refresh"] = {"ok": False, "problem": str(ex)[:200]}
            fs.append(finding("MAP-RUN-003", "WARN", f"layer '{row['layer']}': the refresh failed ({str(ex)[:140]})"))
            continue
        rep = r.get("report") or {}
        row["refresh"] = {"ok": r["ok"], "applied": bool(r.get("applied")), "identical": rep.get("identical"), "summary": rep.get("summary")}
        fs += [dict(f, layer=row["layer"]) for f in r["findings"] if f["severity"] in ("BLOCK", "WARN")]
        reports.append(row["layer"])
    c.results["dr_refreshed"] = reports
    return {"ok": True, "summary": f"{len(reports)} service layer(s) asked again", "findings": [f for f in fs if f["severity"] != "BLOCK"] + [dict(f, severity="WARN") for f in fs if f["severity"] == "BLOCK"]}


def stage_dr_rebuild(c: Ctx) -> dict:
    if not c.options.get("rebuild", True):
        return {"ok": True, "summary": "not requested", "findings": [], "skipped": True}
    changed = any((r.get("refresh") or {}).get("applied") for r in c.results["dr_layers"])
    if not changed and not c.options.get("force_rebuild"):
        c.results["dr_rebuilt"] = False
        return {"ok": True, "summary": "no applied change; the map was not rebuilt", "findings": [], "skipped": True}
    bdir = Path(c.outdir) if c.outdir else c.run_dir / "rebuild"
    res, _ = safe_build(c.inputs["spec"], bdir, c.options.get("formats") or ["pdf", "png", "svg"], strict=False)
    for f in res["files"]:
        c.add_file(Path(f), "map")
    c.add_file(Path(res["provenance"]), "provenance")
    c.results["dr_rebuilt"] = True
    c.results["dr_build"] = res
    return {"ok": not res["blocked"], "summary": f"{len(res['files'])} file(s) rebuilt", "findings": res["findings"]}


def stage_dr_summary(c: Ctx) -> dict:
    rows = c.results["dr_layers"]
    L = ["# Data refresh: change summary", ""]
    for r in rows:
        bit = f"- **{r['layer']}** ({r['type']}): "
        if r["type"] == "service":
            rf = r.get("refresh")
            bit += (f"{rf['summary']}" + (" (applied)" if rf.get("applied") else " (not applied)")) if rf and rf.get("ok") else (r.get("problem") or (rf or {}).get("problem") or "not refreshed")
        elif r["type"] == "user-run":
            bit += r["note"]
        elif r["type"] == "file":
            bit += "file missing" if r.get("missing") else f"file unchanged by this run (sha256 {r['sha256'][:12]})"
            lib = r.get("library")
            if lib:
                bit += ("; it is the library's recorded download of " if lib["same"] else "; it is NOT the library's recorded download of " if lib["same"] is False
                        else "; not compared with the library's download of ") + f"{lib['entry']} (rev {lib['rev']})"
        else:
            bit += f"library layer {r.get('ref')}"
        L.append(bit)
    L += ["", "Rebuilt." if c.results.get("dr_rebuilt") else "The map was not rebuilt."]
    p = c.run_dir / "change_summary.md"
    p.write_text("\n".join(L) + "\n")
    c.add_file(p, "report")
    return {"ok": True, "summary": "summary written", "findings": []}


# ---------------------------------------------------------------------------------------------------------------- service pipelines
def plan_harvest(inp: dict, opt: dict) -> dict:
    n = len(inp["layers"])
    return {"network_requests": 4 * n, "features": int(opt.get("max_total") or 500_000), "files_written": n * 3, "est_seconds": 30 * n}


def stage_harvest(c: Ctx) -> dict:
    import service_harvest as sh
    shared = {k: c.options.get(k) for k in ("protocol", "cql_filter", "bbox", "properties", "max_features", "page_size") if c.options.get(k) is not None}
    shared.setdefault("protocol", "wfs")
    http = c.http
    if http is None:
        import service_query as sq
        http = sq.make_http(sq.need_mcp_profile(c.inputs["profile"]))
    r = sh.harvest(http, c.inputs["profile"], list(c.inputs["layers"]), shared, confirm_total=c.params.get("confirm_total"), max_total=c.options.get("max_total"),
                   refresh_existing=bool(c.options.get("refresh_existing")), apply=bool(c.options.get("apply")))
    c.results["harvest"] = r
    if r.get("confirmed") is False:
        c.results["needs_size_confirmation"] = {"total": r.get("total"), "estimate": r.get("estimate")}
    if r.get("index"):
        c.add_file(Path(r["index"]), "index")
    return {"ok": r["ok"], "summary": ("estimate only; confirm the size to fetch" if r.get("confirmed") is False else f"{len(r.get('results') or [])} layer(s)"), "findings": r["findings"]}


def plan_refresh(inp: dict, opt: dict) -> dict:
    return {"network_requests": 6, "files_written": 3, "est_seconds": 40}


def stage_refresh(c: Ctx) -> dict:
    import service_refresh as srf
    http = c.http
    if http is None:
        import service_query as sq
        rec = json.loads(srf.locate(c.inputs["capture"]).read_text())
        http = sq.make_http(sq.need_mcp_profile(rec["profile"]))
    r = srf.refresh(http, c.inputs["capture"], apply=bool(c.params.get("apply")), key=c.options.get("key"))
    c.results["refresh"] = r
    return {"ok": r["ok"], "summary": (r.get("report") or {}).get("summary") or "refresh failed", "findings": r["findings"]}


# ---------------------------------------------------------------------------------------------------------------- 1.3.0: climate and broadband fetches
def plan_climate(inp: dict, opt: dict) -> dict:
    """The cost card of a CDS job, before anything is sent: the size estimated here (the constraints check, cost and licences come in the run)."""
    import yaml
    import cds_retrieve as cdr
    req = yaml.safe_load(Path(inp["request"]).read_text()) or {} if inp.get("request") and Path(inp["request"]).is_file() else {}
    area = [float(v) for v in str(opt["area"]).split(",")] if opt.get("area") else None
    try:
        ent = cdr.dataset_entry(inp["dataset"])
        est = cdr.estimate_bytes(cdr.inputs_of(req, area), float(((ent.get("delivery") or {}).get("cds") or {}).get("grid_deg") or 0.25))
    except cdr.CDSError as ex:
        return {"network_requests": 0, "files_written": 0, "est_seconds": 0, "lines": [str(ex)]}
    return {"network_requests": 8, "files_written": 3, "est_seconds": 60 + est / 2_000_000, "bytes": est,
            "lines": [f"one CDS job for {inp['dataset']}: about {est / 1024 ** 2:.0f} MB (2 GB cap)", "requests one at a time (ECMWF); the constraints are checked before submitting",
                      "uses your copernicus-cds token; the dataset's licence must already be accepted on its page"]}


def stage_climate(c: Ctx) -> dict:
    import yaml
    import cds_retrieve as cdr
    req = yaml.safe_load(Path(c.inputs["request"]).read_text()) or {}
    area = [float(v) for v in str(c.options["area"]).split(",")] if c.options.get("area") else None
    cl = None
    try:
        cl = cdr.Client(http=c.http)
        sub = cdr.submit(c.inputs["dataset"], req, area=area, queue=bool(c.options.get("queue")), client=cl)
        st = cdr.poll(sub["job"], client=cl, wait_s=float(c.options.get("wait_min") or 30) * 60)
        fs = list(sub.get("findings") or []) + list(st.get("findings") or [])
        if st.get("status") != "successful":
            return {"ok": True, "summary": f"job {sub['job']} is {st.get('status')} at CDS; resume later", "findings": fs}
        out = Path(c.options.get("out") or (Path(c.outdir or ".") / f"cds-{sub['job']}"))
        got = cdr.fetch(sub["job"], out, transform=c.options.get("transform"), client=cl)
        for f_ in got["files"]:
            c.add_file(Path(f_["tif"]), "raster")
        c.add_file(Path(got["record"]), "provenance")
        return {"ok": True, "summary": f"job {sub['job']}: {len(got['files'])} GeoTIFF(s)", "findings": fs}
    except cdr.CDSError as ex:
        return {"ok": False, "summary": str(ex)[:200], "findings": [finding(ex.rule, "BLOCK", str(ex))]}
    finally:
        if cl is not None:
            cl.close()


def plan_flythrough(inp: dict, opt: dict) -> dict:
    """1.6.0 (R4-FLY): the cost card of a flythrough: the frames, and the seconds a frame takes from a 10-frame warm-up of the draft."""
    import flythrough as fl
    why = fl.playwright_ok()
    q = str(opt.get("quality") or "draft")
    fps = fl.DRAFT["fps"] if q == "draft" else float(opt.get("fps") or 25)
    n = int(round(float(opt.get("duration") or 32) * fps))
    if why:
        return {"network_requests": 0, "files_written": 0, "est_seconds": 0, "lines": [why]}
    try:
        w = fl.run(inp["page"], Path(opt.get("out") or Path(inp["page"]).parent), mode=_fly_mode(opt), layer=opt.get("path"), quality="draft",
                   warmup_only=True, gl=str(opt.get("gl") or "auto"), duration=float(opt.get("duration") or 32))
        spf = float(w["sec_per_frame"] or 3.0) * (1.0 if q == "draft" else 8.0)
    except Exception as ex:  # noqa: BLE001 - the card still says what would run
        return {"network_requests": 0, "files_written": 0, "est_seconds": 0, "lines": [f"the warm-up did not run ({str(ex)[:120]})"]}
    est = n * spf
    lines = [f"{q} flythrough: {n} frames at {fl.DRAFT['size'][0] if q == 'draft' else opt.get('size') or '1280x720'}, about {spf:.1f} s a frame "
             f"({w.get('gl')}): about {est / 60:.0f} minutes"]
    if q == "draft":
        lines.append("the draft comes first (640 x 360 at 12.5 fps); the final render runs after you have looked at it and confirmed (confirm_final)")
    return {"network_requests": 0, "files_written": n + 2, "est_seconds": est, "lines": lines}


def _fly_mode(opt: dict) -> str:
    return "path" if opt.get("path") else ("chapters" if opt.get("chapters") else "orbit")


def stage_flythrough(c: Ctx) -> dict:
    import flythrough as fl
    q = str(c.options.get("quality") or "draft")
    if q == "final" and not c.params.get("confirm_final"):
        return {"ok": False, "summary": "the final render runs after the draft has been looked at: set confirm_final to run it",
                "findings": [finding("MAP-RUN-001", "BLOCK", "the final flythrough was not confirmed after the draft")]}
    out = Path(c.options.get("out") or c.outdir or Path(c.inputs["page"]).parent)
    r = fl.run(c.inputs["page"], out, mode=_fly_mode(c.options), layer=c.options.get("path"), duration=float(c.options.get("duration") or 32),
               fps=float(c.options.get("fps") or 25), size=str(c.options.get("size") or "1280x720"), marker=c.options.get("marker"),
               hud_on=str(c.options.get("hud") or "on") != "off", quality=q, resume=bool(c.options.get("resume", True)), gl=str(c.options.get("gl") or "auto"))
    for f_ in r.get("files") or []:
        c.add_file(Path(f_), "video")
    return {"ok": bool(r.get("ok")), "summary": f"{q} flythrough: {r.get('done', 0)} frames drawn, {r.get('skipped', 0)} kept, "
                                                 f"{r.get('sec_per_frame') or '-'} s a frame", "findings": r.get("findings") or []}


def plan_fcc(inp: dict, opt: dict) -> dict:
    return {"network_requests": 2, "files_written": 2, "est_seconds": 120, "lines": [f"FCC BDC file {inp['file_id']}: the run allows a download over 100 MB (to 190 MB)",
                                                                                     "uses your fcc-bdc key; 10 calls a minute"]}


def stage_fcc(c: Ctx) -> dict:
    import fcc_bdc as fb
    cl = None
    try:
        cl = fb.Client(http=c.http)
        got = fb.fetch_file(cl, c.inputs["file_id"], kind=c.options.get("kind") or "availability", fmt=c.options.get("format") or "csv", confirm_large=True)
        c.add_file(Path(got["path"]), "data")
        return {"ok": True, "summary": f"{got['bytes'] / 1024 ** 2:.0f} MB, {got.get('rows', '?')} rows", "findings": cl.findings}
    except fb.BDCError as ex:
        return {"ok": False, "summary": str(ex)[:200], "findings": [finding(ex.rule, "BLOCK", str(ex))]}
    finally:
        if cl is not None:
            cl.close()


# ---------------------------------------------------------------------------------------------------------------- telecom maps (M6)
def plan_telecom(inp: dict, opt: dict) -> dict:
    n_in = sum(1 for k in ("sites", "table", "tracker", "coordinates") if inp.get(k)) + (1 if opt.get("coverage") else 0)
    b = sum(Path(inp[k]).stat().st_size for k in ("sites", "table", "tracker", "coordinates") if inp.get(k) and Path(inp[k]).is_file())
    n_formats = len(opt.get("formats") or ["pdf", "png", "svg"])
    return {"files_read": n_in, "bytes_in": b, "renders": 1, "files_written": n_formats + 6, "network_requests": 0, "est_seconds": 30 + 10 * n_formats,
            "note": "Nothing is looked up or geocoded; positions and values come from your tables."}


def _gen_stage(kind: str):
    def stage(c: Ctx) -> dict:
        import table_join as tj
        import telecom_maps as tm
        gdir = c.run_dir / "spec"
        shutil.rmtree(gdir, ignore_errors=True)
        try:
            res = tm.PREPARE[kind](c.inputs, c.options, gdir)
        except (tm.PrepError, tj.JoinError, ValueError) as ex:
            f = finding(getattr(ex, "fid", "MAP-RUN-002"), "BLOCK", str(ex)[:600])
            c.results["pre_findings"] = [f]
            return {"ok": False, "summary": str(ex).splitlines()[0][:160], "findings": [f]}
        c.inputs["spec"] = res["spec"]
        c.results["gen"] = res
        c.results["pre_findings"] = list(res["findings"])
        c.results["gen_report"] = res["report"]
        rp = c.run_dir / "ingest_report.json"
        rp.write_text(json.dumps({"report": res["report"], "findings": res["findings"]}, indent=2, default=str))
        c.add_file(rp, "ingest")
        return {"ok": not blocked(res["findings"]), "summary": f"MapSpec written; {len(res['findings'])} finding(s)", "findings": res["findings"]}
    return stage


def _telecom_stages(kind: str, what: str):
    return [("probe", "Detect the tier", stage_probe), ("ingest", what, _gen_stage(kind)), ("spec-lint", "Check the MapSpec", stage_spec_lint),
            ("build", "Prepare, render and run every lint", stage_build), ("package", "Write the deliverable folder", stage_package),
            ("data-package", "Write layer data (only if requested and licensed)", stage_data_package)]


_TELECOM_OPTS = {"out": "folder (default ./maps)", "title": "text", "subtitle": "text", "message": "one sentence the map must make", "formats": "list (pdf,png,svg)", "audience": "external|internal",
                 "confidential": "bool (default true for client tables)", "privacy_m": "meters of coordinate reduction", "privacy_method": "precision|jitter", "data_package": "bool",
                 "include_confidential": "bool", "allow_restricted_data": "bool", "overwrite": "bool", "brand": "slug", "template": "name", "sheet": "workbook sheet",
                 "data_formats": "list (pmtiles,mbtiles,kmz,gpkg,qgz): styled data outputs, with data_package"}

# ---------------------------------------------------------------------------------------------------------------- network-analysis (M11 B)
NA_FILE_OPTIONS = ("links", "neighbors", "samples", "kpi", "population", "clip", "cells")


def _na_count(opt: dict) -> int:
    import telecom_maps as tm
    try:
        return len(tm.parse_analyses(opt.get("analysis")))
    except tm.PrepError:
        return 1


def plan_network(inp: dict, opt: dict) -> dict:
    n = _na_count(opt)
    files = [inp["sites"]] + [opt[k] for k in NA_FILE_OPTIONS if opt.get(k) and Path(str(opt[k])).is_file()]
    b = sum(Path(f).stat().st_size for f in files if Path(f).is_file())
    n_formats = len(opt.get("formats") or ["pdf", "png", "svg"])
    return {"files_read": len(files), "bytes_in": b, "renders": n, "files_written": n * (n_formats + 6) + 4, "network_requests": 0, "est_seconds": 20 + n * (15 + 10 * n_formats),
            "note": f"{n} analysis map(s). Geometry only: distances, cells, overlaps and bins are measured from the site positions; nothing models radio propagation, and nothing is geocoded."}


def stage_na_analyze(c: Ctx) -> dict:
    import table_join as tj
    import telecom_maps as tm
    gdir = c.run_dir / "analysis"
    shutil.rmtree(gdir, ignore_errors=True)
    try:
        res = tm.prepare_network(c.inputs, c.options, gdir)
    except (tm.PrepError, tj.JoinError, ValueError) as ex:
        f = finding(getattr(ex, "fid", "MAP-RUN-002"), "BLOCK", str(ex)[:600])
        c.results["pre_findings"] = [f]
        return {"ok": False, "summary": str(ex).splitlines()[0][:160], "findings": [f]}
    c.results["na"] = res
    c.results["pre_findings"] = list(res["findings"])
    c.results["gen_report"] = res["report"]
    rp = c.run_dir / "ingest_report.json"
    rp.write_text(json.dumps({"report": res["report"], "findings": res["findings"]}, indent=2, default=str))
    c.add_file(rp, "ingest")
    return {"ok": not blocked(res["findings"]), "summary": f"{len(res['specs'])} analysis MapSpec(s) written; {len(res['findings'])} finding(s)", "findings": res["findings"]}


def stage_na_spec_lint(c: Ctx) -> dict:
    import mapspec_lint as ml
    fs = []
    for a, sp in c.results["na"]["specs"].items():
        fs += [dict(f, layer=f"{a}:{f.get('layer') or ''}".rstrip(":")) for f in ml.lint_spec(_spec(sp))]
    return {"ok": not blocked(fs), "summary": f"{len(fs)} finding(s)", "findings": fs}


def stage_na_build(c: Ctx) -> dict:
    import map_plan as mp
    formats = c.options.get("formats") or ["pdf", "png", "svg"]
    builds, fs = {}, []
    for a, sp in c.results["na"]["specs"].items():
        bdir = c.run_dir / "build" / a
        shutil.rmtree(bdir, ignore_errors=True)
        try:
            res, stamped = safe_build(sp, bdir, formats, brand=c.options.get("brand"), template=c.options.get("template"), strict=False, keep=True)
        except mp.PlanError as ex:
            fs.append(finding("MAP-RUN-003", "BLOCK", f"{a}: {str(ex)[:600]}"))
            continue
        builds[a] = {"res": res, "objects": res.pop("_objects", None), "stamped": stamped}
        fs += [dict(f, layer=f"{a}:{f.get('layer') or ''}".rstrip(":")) for f in res["findings"]]
    c.results["na"]["builds"] = builds
    c.results["all_findings"] = list(fs)
    return {"ok": not blocked(fs) and bool(builds), "summary": f"{len(builds)} map(s) built, {len(fs)} finding(s)", "findings": fs}


def _na_analysis_md(res: dict, builds: dict) -> str:
    import telecom_maps as tm
    rep = res["report"]
    L = ["# Network analysis", "", f"{rep['sites']} sites, {rep['sectors']} sector rows from the site list" + (f", band {rep['band']}" if rep.get("band") else "") + f"; analyses: {', '.join(rep['analyses'])}.",
         "", "Every figure here is a measure of the site layout and the drawn footprints: straight-line distances, cells nearer one site than another, overlaps of drawn shapes, "
         "statistics of the samples that fell in a hexagon. Nothing models radio propagation, terrain or antenna patterns."]
    for a in rep["analyses"]:
        s = res["summary"].get(a) or {}
        L += ["", f"## {tm.NA_TITLES.get(a, a)}", ""]
        for n in res["notes"].get(a, []):
            L.append(f"- {n}")
        tbl = _na_table(a, s)
        if tbl:
            L += [""] + tbl
        b = builds.get(a)
        if b:
            L += ["", f"Map: `{a}/` ({', '.join(Path(f).name for f in b['res']['files'])})."]
    return "\n".join(L) + "\n"


def _f(x, d=2) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "" if x is None else str(x)
    if x != x:
        return "n/a"
    return f"{x:,.0f}" if abs(x) >= 100 or x.is_integer() else f"{x:,.{d}f}"


def _stat_rows(st: dict, label: str, unit: str = "") -> list[str]:
    if not st:
        return []
    return [f"| {label} | {_f(st.get('min'))}{unit} | {_f(st.get('p10'))}{unit} | {_f(st.get('median'))}{unit} | {_f(st.get('mean'))}{unit} | {_f(st.get('p90'))}{unit} | {_f(st.get('max'))}{unit} |"]


STAT_HEAD = ["| | min | p10 | median | mean | p90 | max |", "|---|---|---|---|---|---|---|"]


def _na_table(a: str, s: dict) -> list[str]:
    if a == "spacing":
        return STAT_HEAD + _stat_rows(s.get("nearest"), f"Nearest-site distance ({s.get('units', 'km')})") + _stat_rows(s.get("sites_per_km2"), "Sites per km² (hexagon)")
    if a == "best-server":
        return STAT_HEAD + _stat_rows(s.get("area_km2"), "Cell area (km²)")
    if a == "overlap":
        rows = ["| Group | Footprints | With an overlap | Area (km²) | Under 2 or more (km²) | Share under 2 or more |", "|---|---|---|---|---|---|"]
        for r in s.get("table", []):
            rows.append(f"| {r['group']} | {r['footprints']} | {r['with_overlap']} | {_f(r['area_km2'])} | {_f(r['area_2plus_km2'])} | {_f(100 * r['share_2plus'], 1)}% |")
        return rows
    if a == "backhaul":
        rows = STAT_HEAD + _stat_rows(s.get("length_km"), "Link length (km)")
        if s.get("hubs"):
            rows += ["", "| Site | Links |", "|---|---|"] + [f"| {h['site']} | {h['degree']} |" for h in s["hubs"]]
        if s.get("unmatched_ids"):
            rows += ["", "Ids not in the site list: " + ", ".join(s["unmatched_ids"][:30]) + ("..." if len(s["unmatched_ids"]) > 30 else "")]
        return rows
    if a == "neighbors":
        rows = STAT_HEAD + _stat_rows(s.get("length_km"), "Relation length (km)")
        if s.get("success_rate"):
            rows += _stat_rows({k: (v * 100 if isinstance(v, (int, float)) else v) for k, v in s["success_rate"].items()}, "Handover success", "%")
            rows += ["", f"{s.get('below_90', 0)} relation(s) below 90 percent; {s.get('intra_site', 0)} within one site (not drawn)."]
        if s.get("unmatched_cells"):
            rows += ["", "Cells with no site: " + ", ".join(s["unmatched_cells"][:30]) + ("..." if len(s["unmatched_cells"]) > 30 else "")]
        return rows
    if a == "drive-test":
        rows = STAT_HEAD + _stat_rows(s.get("count_stats"), "Samples per hexagon")
        if s.get("kpi_stats"):
            rows += _stat_rows(s["kpi_stats"], f"{s.get('kpi')} ({s.get('agg')}) per hexagon")
        rows += ["", f"{s.get('samples')} samples, resolution {s.get('resolution')}, {s.get('cells')} hexagons kept, {s.get('thin_cells')} with fewer than {s.get('min_count')} samples left out."]
        return rows
    if a == "sector-kpi":
        return STAT_HEAD + _stat_rows(s.get("stats"), f"{s.get('value')} per sector") + ["", f"{s.get('matched')} of {s.get('sectors')} sectors matched a KPI row; {s.get('unmatched_rows')} KPI row(s) matched no sector."]
    if a == "population":
        rows = []
        if s.get("covered_total") is not None:
            rows += [f"| {s.get('value')} | total in the zone layer | under the drawn footprints |", "|---|---|---|", f"| {s.get('vintage') or ''} | {_f(s.get('zone_total'))} | {_f(s.get('covered_total'))} |"]
        if s.get("within_radius"):
            rows += [""] + STAT_HEAD + _stat_rows(s["within_radius"], f"{s.get('value')} within {s.get('radius')} of a site")
        return rows
    return []


def stage_na_package(c: Ctx) -> dict:
    import geo_io
    res = c.results["na"]
    builds = res.get("builds") or {}
    rep = res["report"]
    base_id = rep.get("base_id") or "network-analysis"
    dest = Path(c.outdir) / base_id if c.outdir else c.run_dir / "deliverable"
    if dest.exists():
        if not c.options.get("overwrite"):
            return {"ok": False, "summary": "the deliverable folder exists", "findings": [finding("MAP-RUN-004", "BLOCK", f"{dest} already exists; choose another folder or set overwrite")]}
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    fs = []
    for a, b in builds.items():
        adir = dest / a
        adir.mkdir()
        r = b["res"]
        for f in r["files"]:
            shutil.copy2(f, adir / Path(f).name)
            c.add_file(adir / Path(f).name, "map")
        (adir / "mapspec.yaml").write_bytes(b["stamped"] or Path(res["specs"][a]).read_bytes())
        c.add_file(adir / "mapspec.yaml", "spec")
        shutil.copy2(r["provenance"], adir / "provenance.json")
        c.add_file(adir / "provenance.json", "provenance")
        obj = b.get("objects") or {}
        if obj.get("methods"):
            (adir / "methods.md").write_text(methods_md(obj["methods"]) + "\n## Analysis\n\n" + "\n".join(f"- {n}" for n in res["notes"].get(a, [])) + "\n")
            c.add_file(adir / "methods.md", "methods")
    tdir = dest / "tables"
    tdir.mkdir()
    for t in res["tables"]:
        shutil.copy2(t, tdir / Path(t).name)
        c.add_file(tdir / Path(t).name, "table")
    if c.options.get("data_package"):
        confidential = c.options.get("confidential") is not False
        lic = str(c.options.get("license") or ("client-proprietary" if confidential else "public-domain")).strip().lower()
        if lic not in OPEN_LICENSES and not c.options.get("allow_restricted_data"):
            fs.append(finding("MAP-LIC-003", "BLOCK", f"the analysis layers derive from the site list, whose license ('{lic}') does not allow the data to be redistributed in a package; set allow_restricted_data only if you hold the right to share it"))
        elif confidential and not c.options.get("include_confidential"):
            fs.append(finding("MAP-LIC-007", "BLOCK", "the analysis layers hold the true site positions (client-confidential): they go into a data package only after explicit confirmation (include_confidential)"))
        else:
            ddir = dest / "data"
            ddir.mkdir()
            for a, layers in res["layers"].items():
                for name, p in layers.items():
                    g = geo_io.read(p).gdf
                    for fmt, ext in (("gpkg", "gpkg"), ("geojson", "geojson")):
                        q = ddir / f"{a}_{name}.{ext}"
                        geo_io.write(g, q, fmt, layer=name if fmt == "gpkg" else None, overwrite=True)
                        c.add_file(q, "data")
    allf = list(c.results.get("pre_findings") or []) + list(c.results.get("all_findings") or []) + fs
    (dest / "lint_report.md").write_text(_fs_md(allf))
    c.add_file(dest / "lint_report.md", "lint")
    report = {"network_analysis_version": 1, "sites": rep["sites"], "sectors": rep["sectors"], "band": rep.get("band"), "analyses": rep["analyses"], "summary": rep["summary"],
              "notes": res["notes"], "maps": {a: [Path(f).name for f in b["res"]["files"]] for a, b in builds.items()}, "tables": [Path(t).name for t in res["tables"]],
              "findings": [{k: f.get(k) for k in ("id", "severity", "message", "analysis", "layer")} for f in allf], "caveat": "Geometric measures of the site layout and the drawn footprints; no propagation model."}
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str))
    c.add_file(dest / "report.json", "report")
    (dest / "analysis.md").write_text(_na_analysis_md(res, builds))
    c.add_file(dest / "analysis.md", "report")
    c.results["dest"] = str(dest)
    return {"ok": not blocked(fs), "summary": f"packaged in {dest.name}/ ({len(builds)} map(s))", "findings": fs}


# ---------------------------------------------------------------------------------------------------------------- map-atlas-run (M7)
def plan_atlas(inp: dict, opt: dict) -> dict:
    import atlas
    d = atlas.discover(inp["spec"], opt)
    n = len(d["values"])
    fm = len(set(["pdf"] + list(opt.get("formats") or [])))
    an = atlas.plan_extra(d)                  # by: time with animate: the frames are renders too (M15)
    per = f"{d['time']['word']} of {d['field']}" if d["options"].get("by") == "time" else f"{d['field']} value"
    tile_req, tile_note = 0, ""
    if d["tiles"] and d["options"].get("tiles"):              # 1.12.0: the tile composites are the one thing an atlas run fetches
        import basemap_render as br
        comps = n if (d["options"].get("by") != "time" and d["options"].get("mode") == "focus") else 1
        tile_req = min(br.HARD_CAP, comps * br.CAPS["T2"])
        tile_note = (f"; the tile basemap is kept: {comps} composite(s), at most {tile_req} tile requests in all (fewer from the cache), "
                     "each inside the provider's cap for one map")
    return {"renders": n + an.get("renders", 0), "files_written": n * (fm + 1) + 8 + an.get("files_written", 0), "network_requests": tile_req,
            "est_seconds": 20 + 9 * n + an.get("est_seconds", 0), "frames": an.get("frames", 0),
            "note": f"{n} maps, one per {per}" + (", with the same classes in every map" if d["shared"] else "") + (f"; {an['note']}" if an else "")
                    + tile_note + (". The build never fetches." if not tile_req else ". The page builds themselves fetch nothing.")}


def stage_atlas_plan(c: Ctx) -> dict:
    import atlas
    try:
        d = atlas.discover(c.inputs["spec"], c.options)
        maps = atlas.make_specs(c.inputs["spec"], d, c.run_dir / "specs")
    except (atlas.AtlasError, ValueError) as ex:
        return {"ok": False, "summary": str(ex).splitlines()[0][:160], "findings": [finding(getattr(ex, "fid", "MAP-RUN-003"), "BLOCK", str(ex)[:600])]}
    tiles_note = ""
    if d["tiles"] and d["options"].get("tiles"):              # 1.12.0: the pages keep the tile basemap; its composites are made here, once
        tier = str(c.results.get("tier") or "T1")
        tp = atlas.prepare_tiles(maps, tier=tier if tier in ("T1", "T2") else "T1", http=c.http)
        d["findings"] += tp["findings"]
        tiles_note = f", {tp['composites']} tile composite(s)" if tp["composites"] else ", bundled basemap"
    c.results["atlas"] = {"disc": d, "maps": maps}
    c.results["pre_findings"] = list(d["findings"])
    return {"ok": True, "summary": f"{len(maps)} map(s) planned" + (", classes shared" if d["shared"] else "") + tiles_note, "findings": d["findings"]}


def stage_atlas_build(c: Ctx) -> dict:
    import atlas
    a = c.results["atlas"]
    formats = c.options.get("formats") or ["pdf"]
    results = atlas.build_all(c.inputs["spec"], a["disc"], a["maps"], c.run_dir / "atlas-build", formats, brand=c.options.get("brand"), template=c.options.get("template"))
    a["results"] = results
    fs = [f for r in results for f in r["findings"]]
    if a["disc"]["shared"]:
        fs += atlas.compare_classes(results)
    c.results["all_findings"] = list(fs)
    nfiles = sum(len(r["files"]) for r in results)
    return {"ok": not blocked(fs), "summary": f"{len(results)} map(s), {nfiles} file(s), {len(fs)} finding(s)", "findings": fs}


def stage_atlas_assemble(c: Ctx) -> dict:
    import atlas
    import map_brand
    import map_export as mx
    a = c.results["atlas"]
    results = a["results"]
    first = next((r for r in results if r.get("objects")), None)
    if first is None:
        return {"ok": False, "summary": "no map was built", "findings": [finding("MAP-ATL-005", "BLOCK", "no map of the atlas could be built")]}
    plan = first["objects"]["plan"]
    d = a["disc"]
    n_idx = max(1, -(-len(results) // atlas.ROWS_PER_PAGE)) if c.options.get("index_page") is not False else 0
    rows = [{"title": r["title"], "features": r["features"], "page": n_idx + i + 1} for i, r in enumerate(results)]
    adir = c.run_dir / "assembled"
    shutil.rmtree(adir, ignore_errors=True)
    adir.mkdir(parents=True)
    props = map_brand.props_for(plan.brand, plan.manifest, "pdf")
    title = f"{d['base_title']} (atlas of {len(results)} maps)"
    idx_pdfs = []
    if n_idx:
        for svg in atlas.index_pages(plan, rows, d, 1, atlas.shared_note(d), "Atlas index"):
            idx_pdfs.append(mx.to_pdf_bytes(svg, "Atlas index"))
    map_pdfs = []
    for r in results:
        f = next((x for x in r["files"] if x.endswith(".pdf")), None)
        map_pdfs.append((r["title"], Path(f).read_bytes()))
    data = atlas.combine_pdf(idx_pdfs, map_pdfs, props, title, plan.spec["message"])
    out = adir / f"{d['base_id']}-atlas.pdf"
    out.write_bytes(data)
    want = sum(atlas.page_count(b) for b in idx_pdfs) + sum(atlas.page_count(b) for _, b in map_pdfs)
    got = atlas.page_count(data)
    fs = []
    if got != want or any(atlas.page_count(b) != 1 for _, b in map_pdfs):
        fs.append(finding("MAP-ATL-007", "BLOCK", f"the combined PDF has {got} page(s) but {want} were expected (each map must be one page)"))
    c.results["atlas"]["combined"] = str(out)
    c.results["atlas"]["rows"] = rows
    if d["options"].get("contact_sheet") or c.options.get("contact_sheet"):        # 1.0.0: the maps side by side on one sheet
        svg, cfs = atlas.contact_sheet(results, d)
        fs += cfs
        if svg:
            sp = adir / f"{d['base_id']}-contact-sheet.pdf"
            pdf_ = mx.stamp_pdf(mx.to_pdf_bytes(svg, f"{d['base_title']}: contact sheet"), props, f"{d['base_title']}: contact sheet", plan.spec["message"])
            sp.write_bytes(pdf_)
            pp = adir / f"{d['base_id']}-contact-sheet.png"
            pp.write_bytes(mx.rasterize(pdf_, 150, svg))
            c.results["atlas"]["contact_sheet"] = [str(sp), str(pp)]
    return {"ok": not blocked(fs), "summary": f"{got} page(s): {len(idx_pdfs)} index + {len(map_pdfs)} map(s)", "findings": fs}


def stage_atlas_package(c: Ctx) -> dict:
    import csv
    import atlas
    a = c.results["atlas"]
    d = a["disc"]
    dest = Path(c.outdir) / f"{d['base_id']}-atlas"
    if dest.exists():
        if not c.options.get("overwrite"):
            return {"ok": False, "summary": "the deliverable folder exists", "findings": [finding("MAP-RUN-004", "BLOCK", f"{dest} already exists; choose another folder or set overwrite")]}
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    pdf = Path(a["combined"])
    shutil.copy2(pdf, dest / pdf.name)
    c.add_file(dest / pdf.name, "atlas")
    for f_ in a.get("contact_sheet") or []:          # 1.0.0
        shutil.copy2(f_, dest / Path(f_).name)
        c.add_file(dest / Path(f_).name, "contact-sheet")
    keep = set(c.options.get("formats") or ["pdf"])
    for r in a["results"]:
        md = dest / "maps" / r["slug"]
        md.mkdir(parents=True)
        for f in r["files"]:
            if file_format(f) in keep:
                shutil.copy2(f, md / Path(f).name)
                c.add_file(md / Path(f).name, file_role(f))
        prov = next(Path(r["files"][0]).parent.glob("*.provenance.json"), None) if r["files"] else None
        if prov:
            shutil.copy2(prov, md / "provenance.json")
            c.add_file(md / "provenance.json", "provenance")
        for tf in (sorted(Path(r["files"][0]).parent.glob("*.TERMS.md")) + sorted(Path(r["files"][0]).parent.glob("*.terms.json"))) if r["files"] else []:
            shutil.copy2(tf, md / tf.name)                  # 1.3.0: each page's terms register
            c.add_file(md / tf.name, "terms")
    # 1.3.0 (R1-TERMS): the atlas's own register, every page's sources once each, checked against the pages that show them
    import terms_register as tr
    recs = [x for r in a["results"] if r.get("objects") for x in (r["objects"].get("records") or [])]
    svgs = "".join(r["objects"]["rendered"].svg for r in a["results"] if r.get("objects"))
    tdoc, tfs, tfiles = tr.for_deliverable(dest, recs, deliverable="atlas", stem=f"{d['base_id']}-atlas", outputs={"svg": svgs}, title=d.get("base_title"))
    for tf in tfiles:
        c.add_file(tf, "terms")
    c.results.setdefault("all_findings", []).extend(tfs)
    with open(dest / "index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["page", "value", "title", "features", "folder"])
        for r, row in zip(a["results"], a["rows"], strict=True):
            w.writerow([row["page"], r["value"], r["title"], r["features"], f"maps/{r['slug']}"])
    c.add_file(dest / "index.csv", "index")
    sh = d["shared"]
    meta = {"atlas_version": 1, "layer": d["layer"], "field": d["field"], "mode": d["options"]["mode"], "values": d["values"], "shared_classes": bool(sh),
            "class_edges": sh["edges"] if sh else None, "maps": len(a["results"]), "note": atlas.shared_note(d)}
    atlas.package_time(d, a["results"], dest, meta, c.add_file)          # by: time keeps the period specs and the periods (M15)
    (dest / "atlas.json").write_text(json.dumps(meta, indent=2))
    c.add_file(dest / "atlas.json", "atlas")
    (dest / "mapspec.yaml").write_bytes(Path(c.inputs["spec"]).read_bytes())
    c.add_file(dest / "mapspec.yaml", "spec")
    allf = list(c.results.get("pre_findings") or []) + list(c.results.get("all_findings") or [])
    (dest / "lint_report.md").write_text(_fs_md(allf))
    c.add_file(dest / "lint_report.md", "lint")
    meth = next((r["objects"]["methods"] for r in a["results"] if r.get("objects") and r["objects"].get("methods")), None)
    if meth:
        (dest / "methods.md").write_text(methods_md(meth) + "\n## Atlas\n\n" + atlas.shared_note(d) + "\n")
        c.add_file(dest / "methods.md", "methods")
    c.results["dest"] = str(dest)
    return {"ok": True, "summary": f"packaged in {dest.name}/", "findings": []}


# ---------------------------------------------------------------------------------------------------------------- reference-refresh (M3b)
def _rr_lib(opt: dict):
    import ref_library as rl
    lib = Path(opt["library"]) if opt.get("library") else gc.plugin_root() / "library"
    return rl, lib


def _rr_today(opt: dict):
    return dt.date.fromisoformat(opt["today"]) if opt.get("today") else dt.date.today()


def _rr_entries(opt: dict):
    """(entries, tags, types, lib, root): the scope's entries, already loaded; the shipped scope may be a source-tree library."""
    import library_lint as ll
    rl, lib = _rr_lib(opt)
    scope = opt.get("scope") or "shipped"
    es = []
    if scope in ("shipped", "all"):
        es += ll.load_entries(lib / "entries", "shipped")
    if scope in ("local", "all"):
        es += ll.load_entries(rl.local_lib() / "entries", "local")
    tags, types = ll.load_vocab(lib)
    root = lib.parent if opt.get("library") else gc.plugin_root()
    return es, tags, types, lib, root


def plan_reference_refresh(inp: dict, opt: dict) -> dict:
    import ref_maint as rm
    es, tags, types, lib, root = _rr_entries(opt)
    due = rm.due_entries(es, types, _rr_today(opt), int(opt.get("horizon") or 0), opt.get("ids"))
    net = [e for e in due if ((e.get("maintenance") or {}).get("check") or {}).get("kind") in ("http_head", "http_get", "feature_count_and_hash", "row_count_and_hash")]
    dl = [e for e in net if e["maintenance"]["check"]["kind"].endswith("_and_hash")]
    mb = sum(float((e.get("delivery") or {}).get("size_mb") or 0) for e in dl)
    return {"network_requests": len(net), "files_written": 3 + len(net), "est_seconds": 10 + 6 * len(net) + int(mb / 5), "entries_due": len(due), "downloads": len(dl),
            "note": f"{len(due)} entr(ies) due, {len(net)} with an automatic check ({len(dl)} downloads, about {mb:.0f} MB). Nothing in the library is changed; patches are proposed for an owner to review."}


def stage_rr_due(c: Ctx) -> dict:
    import ref_maint as rm
    es, tags, types, lib, root = _rr_entries(c.options)
    today_ = _rr_today(c.options)
    due = rm.due_entries(es, types, today_, int(c.options.get("horizon") or 0), c.options.get("ids"))
    rep = rm.report(es, tags, types, today_)
    c.results["rr"] = {"entries": es, "due": due, "tags": tags, "types": types, "lib": lib, "root": root, "today": today_, "report": rep}
    fs = []
    unknown = [i for i in (c.options.get("ids") or []) if i not in {e.get("id") for e in es}]
    if unknown:
        fs.append(finding("MAP-LIB-113", "BLOCK", "no such entry: " + ", ".join(unknown)))
        return {"ok": False, "summary": "unknown entry id", "findings": fs}
    if not due:
        fs.append(finding("MAP-LIB-113", "INFO", "no entry is due; nothing to verify"))
    return {"ok": True, "summary": f"{len(due)} of {len(es)} entries due", "findings": fs}


def stage_rr_verify(c: Ctx) -> dict:
    import ref_maint as rm
    r = c.results["rr"]
    net = rm.Net(c.http)
    results = [rm.verify_entry(e, r["today"], r["types"], net, r["root"]) for e in r["due"]]
    r["results"], r["net_calls"] = results, net.calls
    fs = []
    for v in results:
        o = v["outcome"]
        if o == "unreachable":
            fs.append(finding("MAP-LIB-101", "WARN", f"{v['id']}: {v['notes'][0] if v['notes'] else 'could not be reached'}"))
        elif o == "gone":
            fs.append(finding("MAP-LIB-109", "WARN", f"{v['id']}: the address answered 404/410; consider deprecating the entry (a decision for the owner)"))
        elif o == "refused":
            fs.append(finding("MAP-LIB-100", "WARN", f"{v['id']}: {v['notes'][0] if v['notes'] else 'refused'}"))
        elif o == "changed":
            fs.append(finding("MAP-LIB-110", "INFO", f"{v['id']}: changed at the source ({'; '.join(x['name'] for x in v['checks'] if x['ok'] is False) or 'see report'})"))
        elif o == "manual":
            fs.append(finding("MAP-LIB-114", "INFO", f"{v['id']}: no automatic check; a person reviews it"))
    checked = [v for v in results if v["outcome"] != "manual"]
    if checked and all(v["outcome"] in ("unreachable", "refused") for v in checked):
        fs.append(finding("MAP-LIB-101", "BLOCK", "none of the sources could be reached; run this from a route that reaches them (a connected machine or the device shell)"))
        return {"ok": False, "summary": "no source reachable", "findings": fs}
    n = {k: sum(1 for v in results if v["outcome"] == k) for k in ("unchanged", "changed", "unreachable", "gone", "manual", "refused")}
    return {"ok": True, "summary": ", ".join(f"{v} {k}" for k, v in n.items() if v), "findings": fs}


def stage_rr_propose(c: Ctx) -> dict:
    """Write one patch per proposal after checking that the patched entry passes the library lint. The library itself is not touched."""
    import library_lint as ll
    import yaml
    r = c.results["rr"]
    dest = (Path(c.outdir) if c.outdir else Path.cwd() / "library-refresh") / f"reference-refresh-{r['today'].isoformat()}"
    if dest.exists() and any(dest.iterdir()) and not c.options.get("overwrite"):
        return {"ok": False, "summary": "output folder exists", "findings": [finding("MAP-RUN-004", "BLOCK", f"{dest.name} already exists; pass overwrite to replace it")]}
    if dest.exists() and c.options.get("overwrite"):
        shutil.rmtree(dest)
    (dest / "patches").mkdir(parents=True)
    fs, written, lines = [], [], []
    by_id = {e.get("id"): e for e in r["entries"]}
    for v in r["results"]:
        pr = v.get("proposal")
        if not pr:
            continue
        after = dict(pr["entry_after"])
        after["_path"], after["_scope"] = by_id[v["id"]]["_path"], by_id[v["id"]]["_scope"]
        trial = [after if e.get("id") == v["id"] else e for e in r["entries"]]
        bad = [x for x in ll.lint_entries(trial, r["lib"], plugin_root=r["root"], today=r["today"]) if x["severity"] == "BLOCK" and x["entry"] == v["id"]
               and x["id"] not in ("MAP-LIB-011",)]
        if bad:
            fs.append(finding("MAP-LIB-111", "WARN", f"{v['id']}: the proposed patch does not pass the library lint ({bad[0]['id']}: {bad[0]['message']}); not written, review the entry by hand"))
            v["proposal_rejected"] = [x["message"] for x in bad]
            continue
        pp = dest / "patches" / f"{v['id']}.rev{after['rev']}.patch.yaml"
        pp.write_text(yaml.safe_dump(pr, sort_keys=False, allow_unicode=True, width=160))
        written.append(pp)
        c.add_file(pp, "patch")
        lines.append(f"- `{v['id']}` rev {after['rev']} ({pr['kind']}): {pr['changelog']['note']}")
    cl = dest / "CHANGELOG.proposed.md"
    cl.write_text("# Proposed library changelog lines\n\nThese become part of the generated CHANGELOG.md when the patches are applied and the library is regenerated.\n\n" + ("\n".join(lines) or "(none)") + "\n")
    c.add_file(cl, "changelog")
    c.results["dest"] = str(dest)
    c.results["rr"]["patches"] = [str(p) for p in written]
    return {"ok": True, "summary": f"{len(written)} patch(es) proposed", "findings": fs}


def stage_rr_golden(c: Ctx) -> dict:
    r = c.results["rr"]
    bundled = [v["id"] for v in r["results"] if v["outcome"] == "changed" and ((next((e for e in r["entries"] if e["id"] == v["id"]), {}).get("delivery") or {}).get("mode") == "bundled")]
    r["golden"] = {"needed": bundled, "ran": False}
    if not bundled:
        return {"ok": True, "summary": "no bundled payload changed", "findings": [], "skipped": True}
    tests = Path(r["root"]).parent / "tests" / "test_golden.py"
    if c.options.get("run_golden") and tests.is_file():
        import subprocess
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-q", "-p", "no:cacheprovider", str(tests)], capture_output=True, text=True, timeout=1800, cwd=str(tests.parent.parent))
        r["golden"].update({"ran": True, "passed": p.returncode == 0, "tail": p.stdout[-600:]})
        if p.returncode != 0:
            return {"ok": True, "summary": "golden-image tests FAILED", "findings": [finding("MAP-LIB-112", "WARN", "golden-image tests failed with the current payloads; rebuild the payload before applying: " + ", ".join(bundled))]}
        return {"ok": True, "summary": "golden-image tests passed", "findings": []}
    return {"ok": True, "summary": "golden-image tests are due", "findings": [finding("MAP-LIB-112", "INFO", "a bundled payload is affected (" + ", ".join(bundled) + "); rebuild it and run the golden-image tests before applying (pass run_golden in the source tree to run them here)")]}


def stage_rr_package(c: Ctx) -> dict:
    r = c.results["rr"]
    dest = Path(c.results["dest"])
    L = [f"# Reference library refresh, {r['today'].isoformat()}", "", "Nothing in the library was changed. Each proposal is a patch file for an owner to read and apply.", ""]
    n = {}
    for v in r["results"]:
        n[v["outcome"]] = n.get(v["outcome"], 0) + 1
    L.append("Outcome: " + ", ".join(f"{k} {n[k]}" for k in sorted(n)) + f". {len(r['results'])} of {len(r['entries'])} entries were due.")
    L += ["", "| Entry | Outcome | Checks | Note |", "|---|---|---|---|"]
    for v in sorted(r["results"], key=lambda x: (x["outcome"] != "changed", x["id"])):
        ch = "; ".join(f"{x['name']} {'ok' if x['ok'] else 'differs' if x['ok'] is False else 'recorded'}" for x in v["checks"]) or "-"
        L.append(f"| `{v['id']}` | {v['outcome']} | {ch} | {(v['notes'] or [''])[0].replace('|', '/')[:120]} |")
    if r["golden"]["needed"]:
        L += ["", "Golden-image tests: " + ("ran and " + ("passed" if r["golden"].get("passed") else "FAILED") if r["golden"]["ran"] else "needed before applying (not run here)") + f" for {', '.join(r['golden']['needed'])}."]
    L += ["", "## Review checklist", "", "- Tags come from the vocabulary; `use_when` and `avoid_when` are still true.", "- A changed source: set `vintage.current` and the title if the year is in it; check the join key and columns.",
          "- The license has not changed (read the publisher's terms, not the patch).", "- A gone address: decide whether to deprecate, supersede or retire the entry.",
          "", "## Apply", "", "```", "python tools/ref_release.py check          # before", "python src/scripts/ref_library.py apply patches/<file> --approve --library src/library", "python tools/ref_release.py release        # regenerate, gate, bump", "```", ""]
    (dest / "refresh_report.md").write_text("\n".join(L))
    c.add_file(dest / "refresh_report.md", "report")
    (dest / "refresh.json").write_text(json.dumps({"date": r["today"].isoformat(), "results": [{k: v[k] for k in ("id", "rev", "kind", "outcome", "checks", "notes")} for v in r["results"]],
                                                   "patches": [Path(p).name for p in r["patches"]], "requests": len(r["net_calls"]), "golden": r["golden"]}, indent=2, default=str))
    c.add_file(dest / "refresh.json", "record")
    return {"ok": True, "summary": "report written; waiting for owner review", "findings": []}


# ---------------------------------------------------------------------------------------------------------------- registry
# ---------------------------------------------------------------------------------------------------------------- webmap-build (M14)
def plan_webmap_build(inp: dict, opt: dict) -> dict:
    """The web page's cost line: features, the page size to expect, the engine and why, the basemap, and what the page calls at view time."""
    import webmap as wm
    pv = wm.preview(inp["spec"])
    lines = [f"a page of about {pv['page_mb']:.1f} MB", f"engine {pv['engine']} ({pv['engine_why']})", f"basemap: {pv['basemap']}", f"network at view time: {pv['network']}"]
    if pv.get("story_chapters"):                     # M15: each chapter's print picture is one more render
        lines.append(f"a story of {pv['story_chapters']} chapter(s), each with a print picture")
    return {"renders": 1 + int(pv.get("story_chapters") or 0), "features": pv["features"], "files_written": 6, "network_requests": 0, "est_seconds": 30 + pv["features"] / 800, "lines": lines,
            "engine": pv["engine"], "page_mb": pv["page_mb"], "basemap": pv["basemap"], "network": pv["network"], "note": "The build never fetches."}


def stage_webmap_build(c: Ctx) -> dict:
    import map_plan as mp
    bdir = c.run_dir / "build"
    shutil.rmtree(bdir, ignore_errors=True)
    try:
        res, c.results["spec_stamped"] = safe_build(c.inputs["spec"], bdir, ["html"], brand=c.options.get("brand"), template=c.options.get("template"), strict=False, keep=True)
    except mp.PlanError as ex:
        return {"ok": False, "summary": str(ex).splitlines()[0][:200], "findings": [finding("MAP-RUN-003", "BLOCK", str(ex)[:600])]}
    c.results["build"] = res
    c.results["objects"] = res.pop("_objects", None)
    rep = res.get("web") or {}
    c.results["page_report"] = {k: rep.get(k) for k in ("engine", "engine_why", "features", "page_bytes", "data_bytes", "hosts", "basemaps", "tiled", "clusters", "time", "compare",
                                                       "csp", "min_zoom", "max_zoom", "glyph_ranges", "glyph_missing", "three_d", "story", "panel")}
    return {"ok": not res["blocked"], "summary": f"{rep.get('engine', '?')} page, {len(res['files'])} file(s), {len(res['findings'])} finding(s)", "findings": res["findings"]}


def stage_web_lint(c: Ctx) -> dict:
    """The web lint again, on the page read back from disk (what will be delivered, not what was assembled in memory)."""
    import web_lint
    page = next((f for f in c.results["build"]["files"] if f.endswith(".html")), None)
    if not page:
        return {"ok": False, "summary": "no page", "findings": [finding("MAP-RUN-003", "BLOCK", "the build wrote no page")]}
    fs = web_lint.lint_file(page)
    have = {(f["id"], f["message"]) for f in c.results.get("all_findings") or []}
    new = [f for f in fs if (f["id"], f["message"]) not in have]
    c.results.setdefault("pre_findings", []).extend(new)
    return {"ok": not blocked(fs), "summary": f"read back: {len(fs)} finding(s), {sum(1 for f in fs if f['severity'] == 'BLOCK')} BLOCK", "findings": new}


def stage_web_layout(c: Ctx) -> dict:
    """1.1.0: the page's frame in a headless browser at 1440x900, 1024x768 and 390x844 (web_layout_check): nothing covers the credits corner
    (BLOCK), no overlaps, room for the map, touch targets. Without Chromium it is skipped with a note."""
    import web_layout_check as wlc
    page = next((f for f in c.results["build"]["files"] if f.endswith(".html")), None)
    if not page:
        return {"ok": False, "summary": "no page", "findings": [finding("MAP-RUN-003", "BLOCK", "the build wrote no page")]}
    if c.options.get("layout_check") is False:
        return {"ok": True, "summary": "layout check off (layout_check: false)", "findings": []}
    out = c.run_dir / "build" / "layout"
    r = wlc.check(Path(page), out)
    c.results["layout_dir"] = str(out) if not r["skipped"] else None
    c.results.setdefault("pre_findings", []).extend(r["findings"])
    n_b = sum(1 for f in r["findings"] if f["severity"] == "BLOCK")
    return {"ok": n_b == 0, "summary": (f"skipped: {r['skipped']}" if r["skipped"] else f"{len(r['report'].get('sizes', []))} sizes, {len(r['findings'])} finding(s), {n_b} BLOCK"),
            "findings": r["findings"]}


def stage_webmap_package(c: Ctx) -> dict:
    if c.outdir is None:
        c.outdir = Path.cwd() / "maps"
    r = stage_package(c)
    if r["ok"]:
        dest = Path(c.results["dest"])
        (dest / "page-report.json").write_text(json.dumps(c.results.get("page_report") or {}, indent=2, default=str))
        c.add_file(dest / "page-report.json", "report")
        ld = Path(c.results["layout_dir"]) if c.results.get("layout_dir") else None
        if ld and ld.is_dir():                       # the layout check's report and screenshots, for a person to review (1.1.0)
            (dest / "layout").mkdir(exist_ok=True)
            for f in sorted(ld.iterdir()):
                shutil.copy2(f, dest / "layout" / f.name)
                c.add_file(dest / "layout" / f.name, "report")
        r["summary"] += f"; {(c.results.get('page_report') or {}).get('engine')} page"
    return r


WEBMAP_STAGES = [("probe", "Detect the tier", stage_probe), ("spec-lint", "Check the MapSpec", stage_spec_lint),
                 ("build", "Plan, choose the engine, write the page and its fallback picture", stage_webmap_build),
                 ("web-lint", "Read the page back and run the web lint", stage_web_lint),
                 ("layout", "Open the page in a headless browser at three sizes and check its frame", stage_web_layout),
                 ("package", "Write the deliverable folder", stage_webmap_package)]


REGISTRY = {
    "webmap-build": {"title": "Web map build", "purpose": "One self-contained interactive web page from a MapSpec, linted, with its MapSpec, provenance, lint report, methods and page report.",
                     "inputs": {"spec": "path"}, "options": {"out": "folder (default ./maps)", "overwrite": "bool", "brand": "slug", "template": "name",
                                                             "layout_check": "bool (default on; needs Chromium)"},
                     "plan": plan_webmap_build, "stages": WEBMAP_STAGES, "delivers": True},
    "map-build": {
        "title": "Map build", "purpose": "A finished, linted map package: map files, MapSpec, provenance, lint report, methods, optional data package.",
        "inputs": {"spec": "path"}, "options": {"out": "folder (default ./maps)", "formats": "list (pdf,png,svg)", "data_package": "bool", "include_confidential": "bool",
                                                "allow_restricted_data": "bool", "overwrite": "bool", "brand": "slug", "template": "name",
                                                "data_formats": "list (pmtiles,mbtiles,kmz,gpkg,qgz): styled data outputs in data/, with data_package (also accepted in formats)",
                                                "minzoom": "tile zoom to start at (pmtiles, mbtiles; default 0)", "maxzoom": "tile zoom to stop at (default auto, from the data)"},
        "plan": plan_map_build,
        "stages": [("probe", "Detect the tier", stage_probe), ("spec-lint", "Check the MapSpec", stage_spec_lint), ("build", "Prepare, render and run every lint", stage_build),
                   ("package", "Write the deliverable folder", stage_package), ("data-package", "Write layer data (only if requested and licensed)", stage_data_package)],
        "delivers": True,
    },
    "map-qa": {
        "title": "Map QA", "purpose": "A SHIP / FIX / BLOCK verdict on a MapSpec and/or finished map files, with the visual checklist.",
        "inputs": {"spec": "path (optional)", "files": "list of svg/pdf/png (optional)"}, "options": {}, "plan": plan_map_qa,
        "stages": [("spec-and-build", "Lint the MapSpec and build a preview", stage_qa_spec), ("files", "Check the finished files", stage_qa_files), ("verdict", "Checklist and verdict", stage_verdict)],
        "delivers": False,
    },
    "format-batch": {
        "title": "Format batch", "purpose": "Convert a folder of geodata to one format, verify each result, and write a report with the ogr2ogr equivalents.",
        "inputs": {"src": "folder"}, "options": {"out": "folder", "to": "gpkg|geojson|shapefile|...", "recursive": "bool", "crs": "target CRS", "crs_assume": "CRS for files that state none", "overwrite": "bool"},
        "plan": plan_format_batch,
        "stages": [("scan", "Find geodata files", stage_scan), ("plan", "Plan one output per file or layer", stage_fb_plan), ("convert", "Convert", stage_fb_convert),
                   ("verify", "Read each result back and compare", stage_fb_verify), ("report", "Write the report", stage_fb_report)],
        "delivers": True,
    },
    "data-refresh": {
        "title": "Data refresh", "purpose": "Ask the services behind a map again, report what changed, and rebuild the map when a change is applied.",
        "inputs": {"spec": "path"}, "options": {"out": "folder", "rebuild": "bool (default true)", "force_rebuild": "bool", "formats": "list"}, "params": {"apply": "bool: switch maps to the new captures"},
        "plan": plan_data_refresh,
        "stages": [("read", "Read the layers and their captures", stage_dr_read), ("fetch", "Ask each service again and compare", stage_dr_fetch), ("rebuild", "Rebuild if a change was applied", stage_dr_rebuild),
                   ("summary", "Write the change summary", stage_dr_summary)],
        "delivers": False,
    },
    "map-atlas-run": {
        "title": "Map atlas", "purpose": "One map per value of a field from one MapSpec, with the same classes in every map, an index page and one combined PDF.",
        "inputs": {"spec": "path"}, "options": {"layer": "layer id to iterate", "field": "column whose values make the maps", "values": "list (default: every value)", "mode": "filter|focus",
                                                "max_maps": "int (default 60)", "shared_classes": "bool (default true)", "index_page": "bool (default true)", "title": "template with {title} and {value}",
                                                "out": "folder", "formats": "list (pdf always; png, svg optional)", "overwrite": "bool", "brand": "slug", "template": "name",
                                                "by": "field (default) or time: one page per period of the layer's time_field (M15)", "unit": "auto|year|quarter|month|day",
                                                "cumulative": "bool: each period draws everything up to it, its own features emphasised", "planned": "bool (default: cumulative)",
                                                "tiles": "bool: keep the MapSpec's tile basemap (one composite per page extent, inside the tile caps; the one thing this run fetches)",
                                                "animate": "gif,mp4 (by: time)", "animate_sizes": "square,slide", "hold": "seconds per frame (1.0)", "last_hold": "seconds (3.0)"},
        "plan": plan_atlas, "stages": [("probe", "Detect the tier", stage_probe), ("atlas-plan", "Choose the values and the shared classes, write one MapSpec per map", stage_atlas_plan),
                                      ("build", "Build and lint every map; check the classes match", stage_atlas_build), ("assemble", "Index pages and the combined PDF", stage_atlas_assemble),
                                      ("package", "Write the deliverable folder", stage_atlas_package),
                                      ("animate", "Animated GIF and MP4 from the pages (by: time, animate=)", lambda c: __import__("atlas").stage_animate(c))],
        "delivers": True,
    },
    "coverage-map": {
        "title": "Coverage map", "purpose": "Sector or site map from a site list: wedges drawn from azimuth and beamwidth (display geometry), colored by band, technology or site type, with privacy options for confidential sites.",
        "inputs": {"sites": "site list (csv/xlsx/gpkg)"}, "options": {**_TELECOM_OPTS, "color_by": "band|tech|site_type", "radius_m": "wedge drawing radius", "beamwidth": "default degrees", "wedges": "bool",
                                                                          "clip_bbox": "list minx,miny,maxx,maxy", "coverage": "polygon file (user or FCC availability)", "coverage_label": "text", "show_sites": "bool"},
        "plan": plan_telecom, "stages": _telecom_stages("coverage", "Read and check the site list, write the MapSpec"), "delivers": True,
    },
    "market-map": {
        "title": "Market map", "purpose": "State or county rollup from a client table joined to Census boundaries: a choropleth of a rate, or proportional symbols for counts.",
        "inputs": {"table": "market table (csv/xlsx) with a state or county code"}, "options": {**_TELECOM_OPTS, "key": "code column", "value": "column to map", "denominator": "column for a rate", "per": "rate base (default 1000)",
                                                                                               "geography": "state|county", "style": "choropleth|proportional", "states": "list of state codes", "method": "quantile|equal_interval|jenks",
                                                                                               "classes": "int", "rate_justification": "why the value is already a rate"},
        "plan": plan_telecom, "stages": _telecom_stages("market", "Read the table, check normalization, write the MapSpec"), "delivers": True,
    },
    "program-map": {
        "title": "Program map", "purpose": "Status map of locations from a tracker: five statuses, each with its own shape as well as color; positions from the tracker or a venue coordinates table.",
        "inputs": {"tracker": "tracker export (csv/xlsx)", "coordinates": "venue coordinates table (optional)"},
        "options": {**_TELECOM_OPTS, "name": "name column", "status": "status column", "lat": "latitude column", "lon": "longitude column", "labels": "bool", "size": "symbol size pt"},
        "plan": plan_telecom, "stages": _telecom_stages("program", "Read the tracker, map statuses, write the MapSpec"), "delivers": True,
    },
    "network-analysis": {
        "title": "Network analysis", "purpose": "RAN analytics from a site list, one map and one table per analysis: site spacing and density, best-server cells by distance, footprint overlap, backhaul topology, neighbour relations, drive-test bins, a KPI per sector, population in footprints. Geometry only, no propagation model.",
        "inputs": {"sites": "site list (csv/xlsx/gpkg)"},
        "options": {**_TELECOM_OPTS, "analysis": "list: spacing, best-server, overlap, backhaul, neighbors, drive-test, sector-kpi, population", "band": "keep one band of the site list",
                    "links": "links table (a_site, b_site, type, capacity) for backhaul", "neighbors": "neighbour pairs (source_cell, target_cell, ho_attempts, ho_success)", "cells": "cell_id to site_id table (neighbors)",
                    "samples": "drive-test or crowd samples (csv/gpkg) with a KPI column", "kpi_field": "the KPI column of the samples", "agg": "mean|median|p10|p90|count|min|max", "min_count": "samples a bin needs",
                    "kpi": "KPI table per sector (sector-kpi)", "key": "join key: sector, cell_id, or site_id+azimuth", "value": "the KPI column (sector-kpi)",
                    "population": "a polygon file with a count field, or a library entry id", "population_value": "its count field (default: households or population)",
                    "radius": "distance with units around each site (population)", "radius_km": "the same in km",
                    "clip": "polygon file the best-server cells are cut to", "extend_km": "hull extension when no clip is given", "resolution": "H3 resolution of the drive-test bins", "density_resolution": "H3 resolution of the site-density hexagons",
                    "radius_m": "wedge drawing radius", "beamwidth": "default degrees", "audience": "internal (default) or external"},
        "plan": plan_network,
        "stages": [("probe", "Detect the tier", stage_probe), ("analyze", "Read the site list, run each analysis, write its layers, tables and MapSpec", stage_na_analyze),
                   ("spec-lint", "Check each MapSpec", stage_na_spec_lint), ("build", "Prepare, render and run every lint on each map", stage_na_build),
                   ("package", "Write the deliverable folder with report.json and analysis.md", stage_na_package)],
        "delivers": True,
    },
    "reference-refresh": {
        "title": "Reference library refresh", "purpose": "Check the library entries that are due against their sources, and propose reviewed patches. Nothing is changed or merged.",
        "inputs": {}, "options": {"scope": "shipped|local|all (default shipped)", "ids": "list of entry ids (default: every entry that is due)", "horizon": "also entries due within this many days",
                                  "library": "a source-tree library folder (maintainers)", "out": "folder (default ./library-refresh)", "run_golden": "bool: run the golden-image tests when a bundled payload changed",
                                  "overwrite": "bool", "today": "ISO date (tests)"},
        "plan": plan_reference_refresh,
        "stages": [("due", "Find the entries that are due", stage_rr_due), ("verify", "Run each entry's check against its source", stage_rr_verify),
                   ("propose", "Write patches and changelog lines; lint each patched entry", stage_rr_propose), ("golden", "Golden-image tests where a bundled payload changed", stage_rr_golden),
                   ("package", "Write the review report", stage_rr_package)],
        "delivers": True,
    },
    "service-harvest": {
        "title": "Service harvest", "purpose": "Capture several layers of a public service after a size estimate you confirm.",
        "inputs": {"profile": "service profile name", "layers": "list of layer names"}, "options": {"protocol": "wfs|ogcapi|arcgis", "cql_filter": "filter", "bbox": "list", "properties": "list",
                                                                                                    "max_features": "int", "page_size": "int", "max_total": "int", "refresh_existing": "bool", "apply": "bool"},
        "params": {"confirm_total": "int: the size you accept (from the estimate)"},
        "plan": plan_harvest, "stages": [("harvest", "Estimate, then capture after the size is confirmed", stage_harvest)], "delivers": False,
    },
    "climate-fetch": {
        "title": "Climate fetch (Copernicus CDS)", "purpose": "One CDS job: checked, submitted one at a time, polled, downloaded and read into GeoTIFFs with the credit.",
        "inputs": {"dataset": "CDS dataset id (with a library entry)", "request": "path to the request YAML"},
        "options": {"area": "N,W,S,E (the map window)", "transform": "kelvin_to_celsius|m_to_mm|per_second_to_per_day", "queue": "bool", "wait_min": "minutes (30)", "out": "folder"},
        "plan": plan_climate, "stages": [("cds", "Check, submit, poll and fetch the CDS job", stage_climate)], "delivers": False,
    },
    "flythrough": {
        "title": "Flythrough video", "purpose": "A video flown over a 3D web page (a path, the story's chapters or an orbit): a draft first, the final after you confirm.",
        "inputs": {"page": "path to the 3D web page (.html)"},
        "options": {"path": "line layer to follow", "chapters": "bool", "orbit": "bool", "duration": "seconds (32)", "fps": "25", "size": "1280x720|1920x1080|1080x1350",
                    "marker": "layer", "hud": "on|off", "quality": "draft|final", "resume": "bool (true)", "gl": "auto|gpu|llvmpipe|swiftshader", "out": "folder"},
        "params": {"confirm_final": "bool: the draft was looked at; render the final"},
        "plan": plan_flythrough, "stages": [("flythrough", "Capture, composite the HUD and credits, encode", stage_flythrough)], "delivers": False,
    },
    "fcc-fetch": {
        "title": "FCC BDC download", "purpose": "One Broadband Data Collection file over 100 MB, after the size is shown.",
        "inputs": {"file_id": "the file id fcc_bdc_list returned"}, "options": {"kind": "availability|challenge", "format": "csv|shapefile|gpkg"},
        "plan": plan_fcc, "stages": [("fcc", "Download and read the file", stage_fcc)], "delivers": False,
    },
    "service-refresh": {
        "title": "Service refresh", "purpose": "Ask a service again for one capture and report what changed; apply on request.",
        "inputs": {"capture": "capture id"}, "options": {"key": "field"}, "params": {"apply": "bool"}, "plan": plan_refresh, "stages": [("refresh", "Re-query and compare", stage_refresh)], "delivers": False,
    },
}


# ---------------------------------------------------------------------------------------------------------------- plan and run
PATH_INPUTS = {"spec", "src", "files", "sites", "table", "tracker", "coordinates", "request", "page"}


def normalize(pipeline: str, inputs: dict, options: dict) -> tuple[dict, dict]:
    inp = dict(inputs)
    for k in PATH_INPUTS & set(inp):
        if k == "files":
            inp[k] = [str(Path(f).resolve()) for f in inp[k]]
        elif inp[k]:
            inp[k] = str(Path(inp[k]).resolve())
    opt = {k: v for k, v in options.items() if v is not None}
    for k in ("formats", "properties", "bbox", "clip_bbox", "states", "extra_columns", "values", "ids", "data_formats"):
        if isinstance(opt.get(k), str):
            opt[k] = [x.strip() for x in opt[k].split(",") if x.strip()]
    if isinstance(inp.get("layers"), str):
        inp["layers"] = [x.strip() for x in inp["layers"].split(",") if x.strip()]
    for k in ("out", "coverage", "library"):
        if k in opt:
            opt[k] = str(Path(opt[k]).resolve())
    for k in NA_FILE_OPTIONS:                       # a file path is resolved; a library entry id (population=ref.*) is left as it is
        if isinstance(opt.get(k), str) and not opt[k].startswith("ref.") and Path(opt[k]).exists():
            opt[k] = str(Path(opt[k]).resolve())
    return inp, opt


def plan(pipeline: str, inputs: dict, options: dict | None = None) -> dict:
    if pipeline not in REGISTRY:
        return {"ok": False, "findings": [finding("MAP-RUN-002", "BLOCK", f"'{pipeline}' is not a pipeline; the choices are {', '.join(REGISTRY)}")]}
    reg = REGISTRY[pipeline]
    inp, opt = normalize(pipeline, inputs, options or {})
    fs = []
    req = [k for k, v in reg["inputs"].items() if "optional" not in v]
    for k in req:
        if not inp.get(k):
            fs.append(finding("MAP-RUN-002", "BLOCK", f"{pipeline} needs '{k}' ({reg['inputs'][k]})"))
    if pipeline == "map-qa" and not (inp.get("spec") or inp.get("files")):
        fs.append(finding("MAP-RUN-002", "BLOCK", "map-qa needs a MapSpec, finished files, or both"))
    hashes, budget = {}, [HASH_BUDGET]
    for k in ("spec", "src", "files", "capture", "sites", "table", "tracker", "coordinates", "coverage") + NA_FILE_OPTIONS:
        v = inp.get(k) if k not in ("coverage",) + NA_FILE_OPTIONS else opt.get(k)
        if not v:
            continue
        if k in NA_FILE_OPTIONS and isinstance(v, str) and v.startswith("ref."):
            hashes[k] = {"ref": v}
            continue
        if k == "files":
            hashes[k] = {f: hash_path(Path(f), budget) for f in v}
        elif k == "capture":
            hashes[k] = {"id": v}
        else:
            h = hash_path(Path(v), budget)
            hashes[k] = h
            if h["kind"] == "missing":
                fs.append(finding("MAP-RUN-002", "BLOCK", f"{k} '{v}' was not found"))
    if pipeline in ("map-build", "map-qa", "data-refresh", "map-atlas-run", "webmap-build") and inp.get("spec") and Path(inp["spec"]).is_file():
        # layer files named by the MapSpec are inputs too, so a changed data file changes the plan
        try:
            spec = _spec(inp["spec"])
            for l in spec.get("layers", []):
                s = l.get("source") or {}
                if isinstance((l.get("join") or {}).get("table"), str):     # a joined client table is an input too (a library table is in the cache)
                    hashes.setdefault("layer_tables", {})[l["id"]] = hash_path(Path(inp["spec"]).parent / l["join"]["table"], budget)
                if s.get("type") == "file" and s.get("path"):
                    hashes.setdefault("layer_files", {})[l["id"]] = hash_path(Path(inp["spec"]).parent / s["path"], budget)
                elif s.get("type") in ("service", "user-run") and s.get("capture"):
                    import map_plan as mp
                    try:
                        cp = mp.find_capture(str(s["capture"]), Path(inp["spec"]).parent)
                        hashes.setdefault("captures", {})[l["id"]] = (json.loads(cp.read_text()).get("result") or {}).get("sha256")
                    except mp.PlanError:
                        pass
        except Exception as ex:  # noqa: BLE001
            fs.append(finding("MAP-RUN-002", "BLOCK", f"the MapSpec cannot be read: {str(ex)[:200]}"))
    if pipeline == "reference-refresh":
        try:
            import ref_library as rl
            import ref_maint as rm
            es, _t, types, lib, _r = _rr_entries(opt)
            hashes["library"] = {"entries": rm.canon_sha([rm.clean(e) for e in es]), "files": rl.library_digest(lib)}
        except Exception as ex:  # noqa: BLE001
            fs.append(finding("MAP-RUN-002", "BLOCK", f"the library cannot be read: {str(ex)[:200]}"))
    if blocked(fs):
        return {"ok": False, "findings": fs}
    try:
        cost = reg["plan"](inp, opt)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "findings": fs + [finding(getattr(ex, "fid", "MAP-RUN-002"), "BLOCK", f"the plan could not be made: {str(ex)[:300]}")]}
    if inp.get("spec") and Path(inp["spec"]).is_file() and pipeline in ("map-build", "webmap-build", "map-atlas-run", "map-qa"):
        try:
            import map_style
            cost = {**cost, "style_pack": map_style.pack_of(_spec(inp["spec"]))}
        except Exception:  # noqa: BLE001
            pass
    manifest = {"manifest_version": 1, "pipeline": pipeline, "pipeline_version": PIPELINE_VERSION, "inputs": inp, "input_hashes": hashes, "options": opt,
                "stages": [{"id": s[0], "what": s[1]} for s in reg["stages"]], "edition": edition_id(), "cost": cost}
    mid = hashlib.sha256(canon(manifest).encode()).hexdigest()
    return {"ok": True, "pipeline": pipeline, "manifest": manifest, "manifest_id": mid, "confirm_token": mid[:12], "cost": cost, "cost_text": cost_text(cost),
            "findings": fs, "message": f"{reg['title']}: {reg['purpose']} Cost: {cost_text(cost)}. To go ahead, run again with confirm token {mid[:12]}."}


def run(pipeline: str, inputs: dict, options: dict | None = None, *, confirm: str | None = None, params: dict | None = None, http=None) -> dict:
    p = plan(pipeline, inputs, options)
    if not p["ok"]:
        return p
    if not confirm:
        return {"ok": False, "findings": [finding("MAP-RUN-001", "BLOCK", "this pipeline runs only after its cost is confirmed; " + p["message"])], "plan": {k: p[k] for k in ("manifest_id", "confirm_token", "cost_text")}}
    if confirm != p["confirm_token"]:
        return {"ok": False, "findings": [finding("MAP-RUN-001", "BLOCK", "the confirmation does not match this plan (an input or option changed since the plan was shown); plan again")],
                "plan": {k: p[k] for k in ("manifest_id", "confirm_token", "cost_text")}}
    reg = REGISTRY[pipeline]
    mid = p["manifest_id"]
    base = run_root() / f"{pipeline}-{mid[:8]}"
    run_dir, n = base, 1
    while run_dir.exists():
        n += 1
        run_dir = Path(f"{base}-{n}")
    run_dir.mkdir(parents=True)
    inp, opt = p["manifest"]["inputs"], p["manifest"]["options"]
    outdir = Path(opt["out"]) if opt.get("out") else (Path.cwd() / "maps" if pipeline == "map-build" else None)
    c = Ctx(pipeline=pipeline, inputs=dict(inp), options=opt, params=params or {}, run_dir=run_dir, outdir=outdir, http=http)
    started = now()
    stages, status = [], "ok"
    t_all = time.monotonic()
    import contextlib
    import map_plan as _mp
    # 1.11.0: an atlas's pages and an animation's frames share one ground and one read of each library layer (map_plan.frame_session)
    session = _mp.frame_session() if pipeline == "map-atlas-run" else contextlib.nullcontext()
    session.__enter__()
    for sid, what, fn in reg["stages"]:
        t0 = time.monotonic()
        try:
            r = fn(c)
        except Exception as ex:  # noqa: BLE001
            r = {"ok": False, "summary": f"{type(ex).__name__}: {str(ex)[:200]}", "findings": [finding("MAP-RUN-003", "BLOCK", f"stage '{sid}' failed: {type(ex).__name__}: {str(ex)[:300]}")]}
        c.findings += r["findings"]
        if sid == "build":
            c.results["all_findings"] = list(r["findings"])
        stages.append({"id": sid, "what": what, "ok": r["ok"], "skipped": bool(r.get("skipped")), "summary": r["summary"], "findings": len(r["findings"]), "seconds": round(time.monotonic() - t0, 2)})
        if not r["ok"]:
            status = "blocked" if blocked(r["findings"]) else "failed"
            break
    session.__exit__(None, None, None)
    outputs = []
    seen = set()
    for f in c.files:
        fp = Path(f["path"])
        if fp.is_file() and str(fp) not in seen:
            seen.add(str(fp))
            outputs.append({"path": str(fp), "role": f["role"], "bytes": fp.stat().st_size, "sha256": sha_file(fp), "deterministic": fp.suffix.lower() in DETERMINISTIC})
    base_dir = None
    if reg["delivers"] and status == "ok":
        base_dir = Path(c.results["dest"]) if c.results.get("dest") else (Path(c.outdir) if c.outdir else None)
    det = sorted((str(Path(o["path"]).name), o["sha256"]) for o in outputs if o["deterministic"])
    digest = hashlib.sha256(canon([mid, det]).encode()).hexdigest()
    extra = {}
    if c.results.get("verdict"):
        extra["verdict"] = c.results["verdict"]
    if c.results.get("map_text"):
        extra["map_text"] = c.results["map_text"]
    if c.results.get("needs_size_confirmation"):
        status = "needs-size-confirmation" if status == "ok" else status
        extra["needs_size_confirmation"] = c.results["needs_size_confirmation"]
    rec = {"run_version": 1, "run_id": run_dir.name, "pipeline": pipeline, "manifest_id": mid, "status": status, "started_at": started, "finished_at": now(), "seconds": round(time.monotonic() - t_all, 2),
           "stages": stages, "outputs": outputs, "reproducibility": {"digest": digest, "deterministic_files": len(det), "note": "same inputs and edition give the same manifest id; files marked deterministic repeat byte for byte"},
           "findings": sorted(c.findings, key=lambda f: (SEV.get(f["severity"], 3), f["id"])), **extra}
    (run_dir / "run.json").write_text(json.dumps(rec, indent=2, default=str))
    (run_dir / "plan.json").write_text(json.dumps(p["manifest"], indent=2, default=str))
    if base_dir is not None:
        deliver = {"deliverable_manifest_version": 1, "pipeline": pipeline, "manifest_id": mid, "files": [{"path": str(Path(o["path"]).relative_to(base_dir)) if str(o["path"]).startswith(str(base_dir)) else o["path"], "role": o["role"],
                                                                                                     "sha256": o["sha256"]} for o in outputs if str(o["path"]).startswith(str(base_dir))]}
        deliver["files"].sort(key=lambda x: x["path"])
        (base_dir / "manifest.json").write_text(json.dumps(deliver, indent=2))
        rec["deliverable"] = str(base_dir)
        (run_dir / "run.json").write_text(json.dumps(rec, indent=2, default=str))
    return {"ok": status in ("ok", "needs-size-confirmation"), "run": rec, "run_dir": str(run_dir), "findings": rec["findings"]}


def show(run_id: str) -> dict:
    p = run_root() / run_id / "run.json"
    if not p.is_file():
        return {"ok": False, "findings": [finding("MAP-RUN-002", "BLOCK", f"run '{run_id}' was not found")]}
    return {"ok": True, "run": json.loads(p.read_text())}


def listing() -> list[dict]:
    return [{"id": k, "title": v["title"], "purpose": v["purpose"], "inputs": v["inputs"], "options": v["options"], "params": v.get("params", {}), "stages": [s[1] for s in v["stages"]]} for k, v in REGISTRY.items()]


def kv(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if v.lower() in ("true", "false"):
            val = v.lower() == "true"
        elif v.isdigit():
            val = int(v)
        elif k in ("formats", "layers", "properties", "bbox", "clip_bbox", "states", "extra_columns", "values", "ids", "data_formats"):
            val = [float(x) if k in ("bbox", "clip_bbox") else x for x in v.split(",") if x]
        else:
            val = v
        out[k] = val
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["list", "plan", "run", "show"])
    ap.add_argument("pipeline", nargs="?")
    ap.add_argument("--spec")
    ap.add_argument("--src")
    ap.add_argument("--files", help="comma-separated finished map files (map-qa)")
    ap.add_argument("--profile")
    ap.add_argument("--layers")
    ap.add_argument("--capture")
    ap.add_argument("--sites", help="site list (coverage-map)")
    ap.add_argument("--table", help="client table (market-map)")
    ap.add_argument("--tracker", help="program tracker (program-map)")
    ap.add_argument("--coordinates", help="venue coordinates table (program-map)")
    ap.add_argument("--option", action="append", default=[], help="key=value (out, to, formats, data_package ...)")
    ap.add_argument("--param", action="append", default=[], help="key=value, not part of the plan (apply, confirm_total)")
    ap.add_argument("--confirm")
    a = ap.parse_args(argv)
    if a.op == "list":
        print(json.dumps(listing(), indent=2))
        return 0
    if a.op == "show":
        r = show(a.pipeline or "")
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ok"] else 3
    inputs = {k: v for k, v in {"spec": a.spec, "src": a.src, "profile": a.profile, "capture": a.capture,
                                "sites": a.sites, "table": a.table, "tracker": a.tracker, "coordinates": a.coordinates,
                                "files": a.files.split(",") if a.files else None, "layers": a.layers.split(",") if a.layers else None}.items() if v}
    opts = kv(a.option)
    if a.op == "plan":
        r = plan(a.pipeline, inputs, opts)
    else:
        r = run(a.pipeline, inputs, opts, confirm=a.confirm, params=kv(a.param))
    print(json.dumps(r, indent=2, default=str))
    return exit_code(r)


REFUSAL_PREFIXES = ("MAP-LIC-", "MAP-CRED-", "MAP-SVC-035", "MAP-SVC-036")    # a licence, a key or a refusal on purpose, not a broken map


def exit_code(r: dict) -> int:
    """0.24.0 (TODO M5): the exit code says why a run stopped. 0 done; 4 refused for a licence, a key, the privacy gate or the customization
    acknowledgment (nothing is wrong with the map; something must be decided first); 3 any other BLOCK finding; 1 a runtime failure."""
    if r.get("ok"):
        return gc.EXIT_OK
    blocks = [f for f in r.get("findings") or [] if f.get("severity") == "BLOCK"]
    if blocks and all(str(f.get("id", "")).startswith(REFUSAL_PREFIXES) or "map-services" in str(f.get("message", "")) for f in blocks):
        return gc.EXIT_REFUSED
    return gc.EXIT_LINT if blocks else gc.EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
