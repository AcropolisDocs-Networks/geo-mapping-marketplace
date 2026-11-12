#!/usr/bin/env python3
"""terms-register: a record of the terms each source carries and how one deliverable meets them (1.3.0, R1-TERMS).

    terms_register.py --provenance MAP.provenance.json --svg MAP.svg [--html MAP.html] --out DIR      write the register for a built map

Every deliverable folder gets two files beside its outputs: `<stem>.TERMS.md` (for a person) and `<stem>.terms.json` (schemas/terms.schema.json).
They are built from the provenance records (one per data layer, helper, basemap piece, relief and tile provider), the library entries' `license`
and `attribution` blocks, and what the outputs actually show, read back with the same readers as the output lints. Not legal advice.

One row per source. Each row lists the source's obligations, each on its own with a status:

    attribution      the required text is shown in the output (SVG text, the web page's attribution, the video's credit overlay)
    api notice       the Census Bureau's "not endorsed or certified" notice (and any other API's notice), shown
    share-alike      ODbL and CC BY-SA: a static picture is a Produced Work (the credit is required); a web page or data export that carries the
                     features passes the database on, so the deliverable says so and carries the licence notice (MAP-LIC-016)
    non-commercial   never met by a brand deliverable (`ref_select.license_gate` already refuses library entries; a supplied file can still declare one)
    no redistribution  met when the data is only drawn; not met when the deliverable carries the data itself
    logo             the provider's logo beside its credit (1.1.3)
    terms accepted   "person to confirm" unless the plugin observed the acceptance (a CDS dataset licence on the account's list)

MAP-LIC-017 (BLOCK): a required attribution or API notice is not shown. MAP-LIC-016 (WARN): share-alike data is carried in a web page or data export.
A **Components** section lists the fonts the outputs embed and, on a web page, the vendored libraries (from vendor/*/VENDOR.json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

HEADER = ("A record of the terms the plugin read for each source and how this deliverable meets them. Not legal advice.")
NO_CREDIT_NEEDED = {"public-domain", "public domain", "cc0", "cc0-1.0"}
SHARE_ALIKE = {"share-alike", "odbl", "odbl-1.0", "cc-by-sa", "cc-by-sa-4.0", "cc-by-sa-3.0", "cc by-sa", "cc by-sa 4.0", "cc by-sa 3.0"}
_SA_RE = re.compile(r"(?i)\bodbl\b|share[- ]?alike|\bby-sa\b|\bcc[- ]by[- ]sa\b")
_NC_RE = re.compile(r"(?i)\bnon[- ]?commercial\b|\bby-nc\b|\bcc[- ]by[- ]nc\b|\bnc-sa\b")
_NOTICE_RE = re.compile(r"(?i)[^.]*\bnot endorsed or certified\b[^.]*\.")
STATUSES = ("met", "not met", "person to confirm")
USES = ("drawn", "embedded data", "derived", "tiles", "API")
DELIVERABLES = ("map", "web map", "atlas", "sheet", "export", "data package", "video")


def _today() -> str:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return fixed if fixed else dt.date.today().isoformat()


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def norm(s: str) -> str:
    """Text compared the way a reader sees it: entities decoded, case and spacing folded, a leading 'Source:' and the end stop dropped."""
    t = htmllib.unescape(str(s or ""))
    t = t.replace("\u00a0", " ").replace("’", "'").replace("“", '"').replace("”", '"').replace("(c)", "©").replace("(C)", "©")
    t = re.sub(r"\s+", " ", t).strip().lower()
    t = re.sub(r"^(sources?|basemap|relief|context)\s*:\s*", "", t)
    return t.rstrip(". ")


def api_notices(text) -> list[str]:
    """The notice sentences an API's terms ask the deliverable to display (the Census Data API's 'not endorsed or certified')."""
    return [m.group(0).strip() for m in _NOTICE_RE.finditer(str(text or ""))]


# ------------------------------------------------------------------------------------------------------------- what the outputs show
def svg_shown(svg: str) -> str:
    """The text a reader of the rendered SVG can see (the brand lint's reader: no metadata, title, desc, style or script)."""
    sys.path.insert(0, str(HERE.parent / "lints"))
    import brand_lint
    return norm(brand_lint.visible_text(svg))


def svg_logo_alts(svg: str) -> list[str]:
    return [htmllib.unescape(a) for a in re.findall(r'<svg[^>]*aria-label="([^"]*)"', svg)]


def html_shown(page: str) -> str:
    """A web page's credits are drawn by its script from the page's configuration (the attribution control, the source note): the whole page is
    read, its JSON strings decoded, so a credit in the configuration counts as shown."""
    t = page.replace("\\u00a9", "©").replace("\\u2019", "'").replace('\\"', '"').replace("\\/", "/")
    t = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), t)
    t = re.sub(r"<[^>]+>", " ", t)
    return norm(t)


def _short_shown(text: str, shown: str) -> bool:
    """A credit longer than provenance.LONG_CREDIT is printed as its first sentence and a pointer to the methods note, which prints it whole."""
    import provenance
    s = provenance.short_credit(text)
    return s != str(text or "").strip() and shown_in(s, shown)


def shown_in(text: str, shown: str) -> bool:
    n = norm(text)
    return bool(n) and n in shown


# ------------------------------------------------------------------------------------------------------------- entries
_ENTRIES: dict | None = None


def entry_index() -> dict:
    global _ENTRIES
    if _ENTRIES is None:
        try:
            import ref_library as rl
            _ENTRIES = {e["id"]: e for e in rl.all_entries("all")}
        except Exception:  # noqa: BLE001 - a register is still written from the records alone
            _ENTRIES = {}
    return _ENTRIES


def _lic_of(rec: dict, entry: dict | None) -> dict:
    lic = dict((entry or {}).get("license") or {})
    cls = str(lic.get("class") or rec.get("license") or "unknown")
    return {"name": rec.get("license") if rec.get("license") not in (None, "", cls) else (lic.get("name") or cls), "class": cls,
            "url": rec.get("license_url") or lic.get("url"), "redistribute_ok": lic.get("redistribute_ok"), "commercial_ok": lic.get("commercial_ok"),
            "attribution": rec.get("attribution_text") or lic.get("attribution_text"), "note": lic.get("note"),
            "terms_accepted": lic.get("terms_accepted"), "per_feature": rec.get("per_feature_licences") or []}


def _is_sa(lic: dict) -> bool:
    c = str(lic["class"]).lower()
    return c in SHARE_ALIKE or bool(_SA_RE.search(f"{lic['name']} {lic['class']}"))


def _is_nc(lic: dict) -> bool:
    return lic.get("commercial_ok") is False or bool(_NC_RE.search(f"{lic['name']} {lic['class']}"))


def _kind_of(rec: dict) -> str:
    lid = str(rec.get("layer_id") or "")
    if lid.startswith("basemap:tiles") or lid.startswith("tiles:"):
        return "tiles"
    if lid.startswith("basemap:"):
        return "ground"
    if lid.startswith("relief:"):
        return "terrain"
    if lid.startswith("logo:"):
        return "logo"
    return "helper" if rec.get("drawn") is False else "data"


# ------------------------------------------------------------------------------------------------------------- the register
def build(records: list[dict], *, deliverable: str, stem: str, outputs: dict, embedded: set | None = None, components: list | None = None,
          observed: dict | None = None, title: str | None = None) -> tuple[dict, list[dict]]:
    """(the register, findings).

    outputs     {"svg": text, "html": text, "video": credit text, "data": [file names]}: what was written; each present kind is checked
    embedded    layer ids whose features the deliverable carries (a web page's vector layers, a data export's layers); None = by the outputs
    observed    {entry_id: "accepted on DATE (how)"}: acceptances the plugin saw, so the row is met instead of 'person to confirm'
    """
    observed = observed or {}
    shown = {}
    if outputs.get("svg"):
        shown["map"] = svg_shown(outputs["svg"])
    if outputs.get("html"):
        shown["web page"] = html_shown(outputs["html"])
    if outputs.get("video"):
        shown["video credits"] = norm(outputs["video"])
    logos = svg_logo_alts(outputs.get("svg") or "") + re.findall(r'alt="([^"]*)"', outputs.get("html") or "")
    carries_data = bool(outputs.get("html") or outputs.get("data"))
    rows, findings, seen = [], [], set()
    idx = entry_index()
    for rec in records:
        ent = idx.get(rec.get("library_ref")) if rec.get("library_ref") else None
        kind = _kind_of(rec)
        lic = _lic_of(rec, ent)
        key = (rec.get("source_name"), rec.get("library_ref"), lic["class"], lic["attribution"])
        if key in seen:                                          # one row per source (panels, atlas pages, a page's two tile layers)
            for r in rows:
                if r["_key"] == key and rec.get("layer_id") not in r["layers"]:
                    r["layers"].append(rec.get("layer_id"))
            continue
        seen.add(key)
        uses = []
        if kind == "tiles":
            uses.append("tiles")
        elif kind == "helper":
            uses.append("derived")
        else:
            if outputs.get("svg") or outputs.get("video"):
                uses.append("drawn")
            lid = rec.get("layer_id")
            emb = (lid in embedded) if embedded is not None else (carries_data and kind in ("data", "ground"))
            if emb:
                uses.append("embedded data")
        if rec.get("ops") or rec.get("aggregation") or rec.get("contours"):
            if "derived" not in uses:
                uses.append("derived")
        dl = (ent or {}).get("delivery") or {}
        if dl.get("mode") == "service" or dl.get("requires_credential") or dl.get("api") or dl.get("tool"):
            if kind != "tiles":
                uses.append("API")
        uses = uses or ["drawn"]
        obl = []

        def ob(kind_, text, status, where=None, note=None):
            o = {"kind": kind_, "text": text, "status": status}
            if where:
                o["where"] = where
            if note:
                o["note"] = note
            obl.append(o)

        needs_credit = str(lic["class"]).lower() not in NO_CREDIT_NEEDED and uses != ["derived"]
        att = lic["attribution"]
        if needs_credit and att:
            missing = [w for w, s in shown.items() if not shown_in(att, s)]
            short = [w for w in missing if _short_shown(att, shown[w])]
            missing = [w for w in missing if w not in short]
            if missing:
                ob("attribution", att, "not met", note="not shown on: " + ", ".join(missing))
                findings.append(finding("MAP-LIC-017", "BLOCK", f"the credit {rec.get('source_name')!s} requires is not shown on the {', '.join(missing)}: \"{att[:140]}\"",
                                        {"layer": rec.get("layer_id"), "missing": missing}))
            elif short:                                         # a long relief credit printed short, in full in the methods note (0.24.0)
                ob("attribution", att, "met", where=", ".join(shown) or None, note="shortened on the " + ", ".join(short) + "; the full text is in this register, the provenance record and the methods page")
            else:
                ob("attribution", att, "met", where=", ".join(shown) or None)
        elif needs_credit:
            ob("attribution", None, "person to confirm", note="the licence asks for a credit and no wording was recorded; check the licence and give provenance.attribution")
        else:
            ob("attribution", None, "met", note="not required (public domain)" if uses != ["derived"] else "not required (a helper; its data is not shown)")
        for n in api_notices(f"{att or ''} {lic.get('note') or ''}"):
            if uses == ["derived"] or n in [o["text"] for o in obl if o["kind"] == "api notice"]:
                continue
            missing = [w for w, s in shown.items() if not shown_in(n, s)]
            if missing:
                ob("api notice", n, "not met", note="not shown on: " + ", ".join(missing))
                findings.append(finding("MAP-LIC-017", "BLOCK", f"the notice the {rec.get('source_name')} API's terms ask for is not shown on the {', '.join(missing)}: \"{n[:140]}\"",
                                        {"layer": rec.get("layer_id"), "missing": missing}))
            else:
                ob("api notice", n, "met", where=", ".join(shown) or None)
        if _is_sa(lic):
            if "embedded data" in uses:
                ob("share-alike", f"{lic['name']}: the deliverable carries the data, so it is offered under the same licence", "met",
                   where="the notice file LICENSE-SHARE-ALIKE.txt")
                findings.append(finding("MAP-LIC-016", "WARN", f"{rec.get('source_name')} is share-alike ({lic['name']}) and this {deliverable} carries its features: the data "
                                        "in it is offered under that licence, and the notice file LICENSE-SHARE-ALIKE.txt goes with it", {"layer": rec.get("layer_id")}))
            else:
                ob("share-alike", "a rendered picture is a Produced Work: the credit is required, the picture is not under the database licence", "met")
        for pf in lic["per_feature"]:                        # R1-HIST: features carrying their own licence tag
            if _SA_RE.search(str(pf.get("license"))):
                ob("share-alike", f"{pf.get('count', 'some')} feature(s) under {pf.get('license')}: share-alike applies when a significant part of them is reused",
                   "met" if "embedded data" not in uses else "person to confirm")
        if _is_nc(lic):
            ob("non-commercial", "the licence allows non-commercial use only; a brand deliverable is commercial", "not met")
        if lic.get("redistribute_ok") is False:
            ob("no redistribution", "the data may not be passed on", "not met" if "embedded data" in uses else "met",
               note="drawn only, never carried as data" if "embedded data" not in uses else "this deliverable carries the data")
        lg = ((ent or {}).get("attribution") or {}).get("logo")
        if lg:
            alt = str(lg.get("alt") or "") if isinstance(lg, dict) else ""
            ok = bool(alt) and any(norm(alt) == norm(a) for a in logos)
            ob("logo", alt or "the provider's logo", "met" if ok else "not met", note=None if ok else "the logo is not beside the credit (MAP-LIC-015)")
        ta = lic.get("terms_accepted")
        if ta:
            seen_ok = observed.get(rec.get("library_ref"))
            ob("terms accepted", str(ta), "met" if seen_ok else "person to confirm", note=seen_ok or None)
        status = "not met" if any(o["status"] == "not met" for o in obl) else ("person to confirm" if any(o["status"] == "person to confirm" for o in obl) else "met")
        rows.append({"_key": key, "source": rec.get("source_name"), "publisher": ((ent or {}).get("source") or {}).get("publisher"), "entry": rec.get("library_ref"),
                     "file": None if rec.get("library_ref") else (rec.get("source_url") or None), "kind": kind, "layers": [rec.get("layer_id")],
                     "licence": {k: lic[k] for k in ("name", "class", "url") if lic.get(k)}, "how_used": uses, "obligations": obl, "status": status})
    for r in rows:
        r.pop("_key", None)
    doc = {"register_version": 1, "header": HEADER, "deliverable": deliverable, "stem": stem, "title": title, "built": _today(),
           "outputs": sorted(k for k, v in outputs.items() if v), "rows": rows, "components": components or [],
           "summary": {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}}
    return doc, findings


# ------------------------------------------------------------------------------------------------------------- components
def font_components(*texts: str) -> list[dict]:
    fams = []
    for t in texts:
        for f in re.findall(r"@font-face\s*\{[^}]*?font-family\s*:\s*[\"']?([^;\"'}]+)", t or "", re.I):
            f = f.strip()
            if f and f not in fams:
                fams.append(f)
    out = []
    for f in fams:
        if f.lower() == "inter":
            out.append({"component": "Inter (font, embedded as a subset)", "licence": "SIL Open Font License 1.1", "file": "assets/fonts/inter/LICENSE.txt",
                        "obligations": "the licence travels with the font; embedding in a document is allowed", "status": "met"})
        else:
            known = _pack_font(f)                         # 1.4.0: a bundled style pack's font: its licence is in the pack (LICENSES/)
            if known:
                out.append({"component": f"{f} (font, embedded as a subset; style pack {known['pack']})", "licence": known["licence"],
                            "file": known["file"], "obligations": "the licence travels with the font; embedding in a document is allowed", "status": "met"})
            else:
                out.append({"component": f"{f} (font, embedded)", "licence": "person to confirm", "obligations": "the font's own licence", "status": "person to confirm"})
    seen = set()
    for t in texts:                                        # 1.5.0 (R3-SCRIPT): labels outlined in a Noto face (no font file travels with the map)
        for f in re.findall(r'data-script-font="([^"]+)"', t or ""):
            if f in seen or f == "Inter":                 # Inter outlines are Inter's own files, listed above
                continue
            seen.add(f)
            out.append({"component": f"{f} (font, labels drawn as outlines)", "licence": "SIL Open Font License 1.1",
                        "file": ".geo-mapping/fonts/noto/ (OFL text beside the font, fetched by doctor --fonts)",
                        "obligations": "outlined glyphs in a document are allowed by the OFL; the font itself is not redistributed", "status": "met"})
    return out


def _pack_font(family: str) -> dict | None:
    try:
        import map_style
        for p in map_style.list_packs():
            st = map_style.load(p["id"])
            for face in st.type.values():
                if face.get("family", "").lower() == family.lower() and not face.get("brand"):
                    return {"pack": st.id, "licence": map_style.FONT_LICENCES.get(face.get("licence_id"), face.get("licence_id")),
                            "file": f"styles/{st.id}/{face['licence']}"}
    except Exception:  # noqa: BLE001
        return None
    return None


def vendored_components(page: str) -> list[dict]:
    """The libraries a web page inlines, with their licences (vendor/*/VENDOR.json)."""
    out = []
    vdir = gc.plugin_root() / "vendor"
    for vj in sorted(vdir.glob("*/VENDOR.json")):
        try:
            v = json.loads(vj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for lib in v.get("libraries") or [v]:
            nm = str(lib.get("name") or vj.parent.name)
            marker = {"maplibre-gl": "maplibregl", "pmtiles": "pmtiles", "leaflet": "L.map"}.get(nm, nm)
            if marker and marker in (page or ""):
                out.append({"component": f"{nm} {lib.get('version', '')} (inlined script)".replace("  ", " "), "licence": lib.get("license"),
                            "file": f"vendor/{vj.parent.name}/{lib.get('license_file') or 'LICENSE'}",
                            "obligations": "the licence text is kept in the page with the library", "status": "met" if lib.get("license") else "person to confirm"})
    return out


# ------------------------------------------------------------------------------------------------------------- files
def markdown(doc: dict) -> str:
    L = [f"# Terms register: {doc.get('title') or doc['stem']}", "", doc["header"], "",
         f"Deliverable: {doc['deliverable']} ({', '.join(doc['outputs']) or 'no outputs'}), built {doc['built']}. "
         f"Sources: {len(doc['rows'])}; met {doc['summary']['met']}, not met {doc['summary']['not met']}, person to confirm {doc['summary']['person to confirm']}.", ""]
    L += ["| Source | Licence | How used | Obligations | Status |", "|---|---|---|---|---|"]
    esc = lambda s: str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")
    for r in doc["rows"]:
        src = esc(r["source"]) + (f" (`{r['entry']}`)" if r.get("entry") else (f" ({esc(r['file'])})" if r.get("file") else ""))
        lic = esc(r["licence"].get("name") or r["licence"].get("class")) + (f" ({esc(r['licence']['class'])})" if r["licence"].get("name") not in (None, r["licence"].get("class")) else "")
        obls = "; ".join(f"{o['kind']}: {o['status']}" + (f" ({esc(o.get('note'))})" if o.get("note") else "") for o in r["obligations"])
        L.append(f"| {src} | {lic} | {', '.join(r['how_used'])} | {obls} | **{r['status']}** |")
    texts = [(r["source"], o) for r in doc["rows"] for o in r["obligations"] if o.get("text")]
    if texts:
        L += ["", "## Required wording", ""]
        for s, o in texts:
            L.append(f"- {esc(s)}, {o['kind']}: \"{esc(o['text'])}\"" + (f" (shown on the {o['where']})" if o.get("where") else ""))
    if doc["components"]:
        L += ["", "## Components", "", "| Component | Licence | Obligations | Status |", "|---|---|---|---|"]
        for c in doc["components"]:
            L.append(f"| {esc(c['component'])} | {esc(c.get('licence'))} | {esc(c.get('obligations'))} | {c.get('status')} |")
    return "\n".join(L) + "\n"


def write(out_dir, doc: dict) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    j = out / f"{doc['stem']}.terms.json"
    j.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    m = out / f"{doc['stem']}.TERMS.md"
    m.write_text(markdown(doc), encoding="utf-8")
    return [m, j]


def validate(doc: dict) -> list[dict]:
    import jsonschema
    schema = json.loads((gc.plugin_root() / "schemas" / "terms.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(doc, schema)
    except jsonschema.ValidationError as ex:
        return [finding("MAP-LIC-017", "BLOCK", f"the terms register is malformed: {ex.message}")]
    return []


def share_alike_notice(out_dir, doc: dict) -> Path | None:
    """LICENSE-SHARE-ALIKE.txt beside a deliverable that carries share-alike data (the data package's file, 0.15.0, written the same way)."""
    terms = []
    for r in doc["rows"]:
        if "embedded data" in r["how_used"] and any(o["kind"] == "share-alike" and "carries the data" in str(o.get("text")) for o in r["obligations"]):
            att = next((o["text"] for o in r["obligations"] if o["kind"] == "attribution" and o.get("text")), None)
            terms.append({"layer": ", ".join(str(x) for x in r["layers"]), "title": r["source"], "license": r["licence"].get("name") or r["licence"].get("class"),
                          "url": r["licence"].get("url"), "attribution": att, "note": None})
    if not terms:
        return None
    import pipelines as pl
    return pl.write_share_alike(Path(out_dir), terms)


def for_deliverable(out_dir, records: list[dict], *, deliverable: str, stem: str, outputs: dict, embedded: set | None = None,
                    observed: dict | None = None, title: str | None = None, notice: bool = True) -> tuple[dict, list[dict], list[Path]]:
    """Build, check and write the register (and the share-alike notice when needed): (doc, findings, files written)."""
    comps = font_components(outputs.get("svg") or "", outputs.get("html") or "") + (vendored_components(outputs["html"]) if outputs.get("html") else [])
    doc, fs = build(records, deliverable=deliverable, stem=stem, outputs=outputs, embedded=embedded, components=comps, observed=observed, title=title)
    fs += validate(doc)
    files = write(out_dir, doc)
    sa = share_alike_notice(out_dir, doc) if notice else None
    if sa:
        files.append(sa)
    return doc, fs, files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--provenance", required=True)
    ap.add_argument("--svg")
    ap.add_argument("--html")
    ap.add_argument("--out", required=True)
    ap.add_argument("--deliverable", default="map", choices=DELIVERABLES)
    a = ap.parse_args(argv)
    prov = json.loads(Path(a.provenance).read_text(encoding="utf-8"))
    outputs = {"svg": Path(a.svg).read_text(encoding="utf-8") if a.svg else None, "html": Path(a.html).read_text(encoding="utf-8") if a.html else None}
    stem = Path(a.provenance).name.replace(".provenance.json", "")
    doc, fs, files = for_deliverable(a.out, prov.get("records") or [], deliverable=a.deliverable, stem=stem, outputs=outputs)
    print(json.dumps({"files": [str(f) for f in files], "summary": doc["summary"], "findings": fs}, indent=2))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in fs) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
