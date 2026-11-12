#!/usr/bin/env python3
"""provenance-ledger: one record per layer, and everything printed from them (spec 6.2, 12.6).

    records(plan)            -> [record]   the provenance.json content (schema: schemas/provenance.schema.json)
    validate(plan, records)  -> findings   MAP-LIC-001 (source, date, license present), MAP-LIC-009 (no internal hosts), schema shape
    source_lines(plan)       -> [str]      the source note under the map (source, vintage; attribution; basemap credit)
    methods(plan, rendered)  -> dict       the structured "Data & Methods" content
    methods_svg(plan, m)     -> str        that content as a letter-size appendix page in the brand's tokens
    write_sidecar(path, ...)               provenance.json: records + methods + reproducibility block

Records are built from what the build actually did (the CRS read, the steps taken, the rows used), never from memory.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import svg_draw as sd  # noqa: E402
import textmeasure as tm  # noqa: E402

PT = 25.4 / 72
# Host names and addresses that must not reach a rendered output unless the user opts in (MAP-LIC-009).
_INTERNAL = re.compile(r"(?i)(?:https?://)?(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|localhost|127\.0\.0\.1|[\w-]+(?:\.[\w-]+)*\.(?:internal|corp|lan|local|intra))\b[^\s\"']*")
PAGE_MM = (215.9, 279.4)           # the appendix is always US Letter portrait


def _today() -> dt.date:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return dt.date.fromisoformat(fixed) if fixed else dt.date.today()


def withheld(text):
    """The text with internal host names replaced by the neutral phrase (MAP-LIC-009)."""
    if text is None:
        return None
    return _INTERNAL.sub("internal data service", str(text))


def has_internal(text) -> bool:
    return bool(text and _INTERNAL.search(str(text)))


# ------------------------------------------------------------------------------------------------------------- records
def _date_of(p: dict, entry: dict | None) -> str | None:
    for k in ("retrieved_at", "date", "vintage"):
        if p.get(k):
            return str(p[k])
    if entry:
        return str(entry.get("url_verified_on") or (entry.get("maintenance") or {}).get("reviewed_on") or "") or None
    return None


def record_for(l, plan) -> dict:
    p = l.provenance or {}
    e = l.entry
    src = (e or {}).get("source") or {}
    license_url = p.get("license_url")
    rec = {
        "layer_id": l.id,
        "source_name": withheld(p.get("source") or l.id),
        "source_url": withheld(p.get("url") or src.get("url")),
        "retrieved_at": _date_of(p, e) or "unknown",
        "license": p.get("license") or "unknown",
        "license_url": withheld(license_url),
        "attribution_text": p.get("attribution"),
        "crs_original": l.crs_original or "unknown",
        "crs_used": plan.crs,
        "transformations": list(l.transformations),
        "row_count": int(len(l.gdf)),
        "bbox": l.bbox,
        "sha256": l.src_sha256,
        "library_ref": p.get("library_ref"),
        "library_rev": p.get("library_rev"),
    }
    if rec["sha256"] is None:
        del rec["sha256"]
    if rec["bbox"] is None:
        del rec["bbox"]
    if l.confidentiality and l.confidentiality != "public":
        rec["confidentiality"] = l.confidentiality
    if p.get("ops"):                                         # one record per `ops:` step (M11): op, params, rows in and out, measurement CRS
        rec["ops"] = [dict(o) for o in p["ops"]]
    if p.get("aggregation"):
        rec["aggregation"] = dict(p["aggregation"])
    if p.get("raster"):                                      # a raster layer: file hash, band, window, resampling, colouring; a service picture's requests (M12)
        rec["raster"] = dict(p["raster"])
    if p.get("contours"):
        rec["contours"] = dict(p["contours"])
    for k in ("page_simplify", "thinning"):                  # M16: what the page did to a large layer (map_render), said in the record too
        if p.get(k):
            rec[k] = dict(p[k])
    engines = []                                             # M16: which engine ran each step that has a choice (duck_engine)
    jr = getattr(l, "join_report", None)
    if isinstance(jr, dict) and jr.get("engine"):
        engines.append({"step": "join", **jr["engine"]})
    for o in p.get("ops") or []:
        if isinstance(o, dict) and o.get("engine"):
            engines.append({"step": f"op {o.get('op')}", **o["engine"]})
    if isinstance(p.get("aggregation"), dict) and p["aggregation"].get("engine"):
        engines.append({"step": f"aggregation ({p['aggregation'].get('op')})", **p["aggregation"]["engine"]})
    if engines:
        rec["engine"] = engines
    if not getattr(l, "draw", True):
        rec["drawn"] = False
        rec["crs_used"] = l.crs_original or "unknown"      # a helper is never reprojected to the display; the op's record names the CRS it measured in
    return rec


def all_layers(plan) -> list:
    """The drawn layers, then the helpers (`draw: false`): a helper is a source of the map's numbers, so it has a record, a line in the source
    note and an entry on the methods page, in that order after the layers the reader sees."""
    return list(plan.layers) + list(getattr(plan, "helpers", None) or [])


def basemap_records(plan) -> list[dict]:
    out = []
    for b in plan.basemap:
        if b.get("record"):                                  # relief (M12): the record map_raster wrote, with terrain_fetch.public_record inside
            out.append(dict(b["record"]))
            continue
        e = b.get("entry")
        if not e:
            continue
        src = e.get("source") or {}
        im = b.get("image")
        if im:
            lic = e.get("license") or {}
            out.append({"layer_id": "basemap:tiles", "source_name": e.get("title") or "map tiles", "source_url": src.get("url"), "retrieved_at": str(im.get("retrieved_at")),
                        "license": lic.get("class"), "attribution_text": lic.get("attribution_text"), "crs_original": "EPSG:3857", "crs_used": plan.crs,
                        "transformations": [f"{im.get('tiles')} tiles at zoom {im.get('zoom')} joined into one picture ({im.get('fetched', 0)} fetched, {im.get('from_cache', 0)} from the cache; source: {im.get('source')})"]
                                       + ([f"used as a customization beyond the defaults: the person's one acknowledgment ({im.get('customization') or 'asked'}) that they keep it within the provider's terms"] if im.get("terms_acknowledged") else []),
                        "row_count": 0, "library_ref": e["id"], "library_rev": e.get("rev"), "sha256": im.get("sha256")})
            continue
        out.append({"layer_id": f"basemap:{b['kind']}", "source_name": f"{src.get('publisher', '')}, {src.get('product', e.get('title'))}".strip(", "),
                    "source_url": src.get("url"), "retrieved_at": str(e.get("url_verified_on") or (e.get("maintenance") or {}).get("reviewed_on")),
                    "license": (e.get("license") or {}).get("class"), "attribution_text": (e.get("license") or {}).get("attribution_text"),
                    "crs_original": _crs_label(b["gdf"].crs) if b.get("gdf") is not None and b["gdf"].crs is not None else "unknown", "crs_used": plan.crs,
                    "transformations": ["clipped to a window around the map and reprojected"], "row_count": int(len(b["gdf"])) if b.get("gdf") is not None else 0,
                    "library_ref": e["id"], "library_rev": e.get("rev")})
    return out


def _crs_label(crs) -> str:
    import map_plan as mp
    return mp.crs_label(crs) or "unknown"


def records(plan) -> list[dict]:
    return [record_for(l, plan) for l in all_layers(plan)] + basemap_records(plan)


# ------------------------------------------------------------------------------------------------------------- validation
def _f(fid, sev, msg, detail=None):
    return {"id": fid, "severity": sev, "message": msg, "detail": detail}


def validate(plan, recs: list[dict] | None = None) -> list[dict]:
    import jsonschema
    recs = recs if recs is not None else records(plan)
    schema = json.loads((gc.plugin_root() / "schemas" / "provenance.schema.json").read_text())
    out: list[dict] = []
    for r in recs:
        try:
            jsonschema.validate(r, schema)
        except jsonschema.ValidationError as ex:
            out.append(_f("MAP-LIC-001", "BLOCK", f"provenance for '{r.get('layer_id')}' is malformed: {ex.message}"))
    declared = {l.id: l for l in all_layers(plan)}
    for r in recs:
        l = declared.get(r["layer_id"])
        if l is None or l.role == "context":
            continue
        miss = []
        if not l.entry:
            p = l.provenance or {}
            if not p.get("source_declared"):
                miss.append("source")
            if not p.get("license"):
                miss.append("license")
            if r["retrieved_at"] == "unknown":
                miss.append("date (vintage, retrieved_at or date)")
        if miss:
            out.append(_f("MAP-LIC-001", "BLOCK", f"layer '{l.id}' has no {', '.join(miss)} in its provenance; "
                          "add provenance: {source, license, vintage} to the layer so the source note can state them", miss))
    for l in all_layers(plan):
        p = l.provenance or {}
        raw = [p.get("url"), p.get("source"), p.get("license_url"), (l.spec.get("source") or {}).get("url")]
        if any(has_internal(x) for x in raw):
            out.append(_f("MAP-LIC-009", "WARN", f"layer '{l.id}': an internal host name or address appears in its provenance; the output says \"internal data service\" instead",
                          [x for x in raw if has_internal(x)][:2]))
    return out


# ------------------------------------------------------------------------------------------------------------- source note
LONG_CREDIT = 300      # 0.24.0 (TODO M12): a credit longer than this (the AWS Terrain Tiles name every source, about 1,100 characters) is shortened


def short_credit(text: str) -> str:
    """A long relief credit as the map prints it: its first sentence and a pointer to the methods note, which prints it in full (the
    provenance record keeps the whole text). A short credit is returned as it is."""
    t = str(text or "").strip()
    if len(t) <= LONG_CREDIT:
        return t
    first = t.split(". ", 1)[0].rstrip(".")
    return f"{first} (its sources are credited in full in the methods note)"


def source_lines(plan) -> list[str]:
    seen, srcs, atts = set(), [], []
    ctx_atts = []
    for l in all_layers(plan):
        if l.role == "context":
            a = (l.provenance or {}).get("attribution")       # a context layer is not a source of the map, but its publisher is still credited
            if a and a.rstrip(".") not in ctx_atts:
                ctx_atts.append(a.rstrip("."))
            continue
        p = l.provenance
        s = withheld(p.get("source"))
        v = p.get("vintage")
        item = f"{s} ({v})" if v else str(s)
        if item and item not in seen:
            seen.add(item)
            srcs.append(item)
        a = p.get("attribution")
        if a and a not in atts:
            atts.append(a)
    base = []
    relief = []
    for b in plan.basemap:
        a = ((b["entry"] or {}).get("license") or {}).get("attribution_text")
        if b.get("kind") == "hillshade":                     # the elevation credit the DEM asks for, on its own line (M12)
            if a and a.rstrip(".") not in relief:
                relief.append(a.rstrip("."))
            elif not a:
                nm = (b.get("record") or {}).get("source_name")
                if nm and f"Relief: {nm}" not in relief:
                    relief.append(f"Relief: {nm}")
            continue
        if a:
            a = a.rstrip(".")
            if a.lower().startswith("source: "):
                a = a[8:]
            if a not in base:
                base.append(a)
    contours = sorted({float((l.provenance or {}).get("contours", {}).get("interval_m")) for l in all_layers(plan)
                       if l.role == "contour" and (l.provenance or {}).get("contours")})
    lines = []
    if srcs:
        lines.append("Source: " + "; ".join(srcs) + ".")
    own = [a for a in atts if a not in base and a.rstrip(".") not in relief]
    if own:
        lines.append(" ".join(own))
    if base:
        lines.append("Basemap: " + "; ".join(base) + ".")
    if relief or contours:
        relief = [short_credit(r) for r in relief]
        lines.append("; ".join(relief + [f"contours every {c:g} m" if relief else f"Contours every {c:g} m" for c in contours]) + ".")
    ctx_own = [a for a in ctx_atts if a not in base and a not in [x.rstrip(".") for x in atts]]
    if ctx_own:
        lines.append("Context: " + "; ".join(ctx_own) + ".")
    shown = " ".join(lines).lower()
    lines += [n for n in api_notice_lines(plan) if n.lower().rstrip(".") not in shown]
    lines += [n for n in as_of_notes(plan) if n.lower().rstrip(".") not in shown]
    return lines


def as_of_notes(plan) -> list[str]:
    """1.3.0 (R1-HIST): a layer filtered to a date (the as_of op) says so in the source note: "Boundaries as of 24 October 1861 (OpenHistoricalMap)"."""
    import hist_fetch as hf
    out = []
    for l in all_layers(plan):
        for o in (l.provenance or {}).get("ops") or []:
            if isinstance(o, dict) and o.get("op") == "as_of":
                d = (o.get("params") or {}).get("date")
                src = (l.provenance or {}).get("source") or "the source"
                src = "OpenHistoricalMap" if "openhistoricalmap" in str(src).lower().replace(" ", "") else src
                n = f"Boundaries as of {hf._long_date(str(d))} ({src})."
                if n not in out:
                    out.append(n)
    return out


def api_notice_lines(plan) -> list[str]:
    """1.3.0 (R1-TERMS): the notices an API's terms ask a deliverable to display, even for public-domain data: the Census Data API's "This
    product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau." From the layer's attribution or its library
    entry's licence (attribution_text, note). Printed in the source note, not in a web page's credits corner; the terms register checks them."""
    import terms_register as tr
    out: list[str] = []
    for l in all_layers(plan):
        lic = (l.entry or {}).get("license") or {}
        text = " ".join(str(x) for x in ((l.provenance or {}).get("attribution"), lic.get("attribution_text")) if x)
        for n in tr.api_notices(text):
            if n not in out:
                out.append(n)
    return out



NO_CREDIT_NEEDED = {"public-domain", "cc0"}


def required_credit_lines(plan) -> list[str]:
    """The credits a licence requires, as footer lines: kept when the person turns the source note off (MAP-LIC-010). A layer or basemap
    piece whose licence is public domain (or CC0) needs none; anything else that names an attribution keeps it: tile basemaps, a relief's
    DEM, a raster or service picture, a CC BY or ODbL data layer."""
    out: list[str] = []
    for l in all_layers(plan):
        p = l.provenance or {}
        lic = str(p.get("license") or "").strip().lower()
        a = p.get("attribution")
        if a and lic not in NO_CREDIT_NEEDED and a.rstrip(".") + "." not in out:
            out.append(a.rstrip(".") + ".")
    for b in plan.basemap:
        lic_d = (b.get("entry") or {}).get("license") or {}
        a = lic_d.get("attribution_text")
        if a and str(lic_d.get("class") or "").lower() not in NO_CREDIT_NEEDED and a.rstrip(".") + "." not in out:
            out.append(a.rstrip(".") + ".")
    return out

def attribution_block(plan) -> list[str]:
    """Every attribution the layers and the basemap require, once each, for the appendix and the web page footer."""
    out = []
    for l in all_layers(plan):
        a = (l.provenance or {}).get("attribution")
        if a and a not in out:
            out.append(a)
    for b in plan.basemap:
        a = ((b["entry"] or {}).get("license") or {}).get("attribution_text")
        if a and a not in out:
            out.append(a)
    return out


# ------------------------------------------------------------------------------------------------------------- methods
def spec_sha256(plan) -> str:
    return hashlib.sha256(Path(plan.spec_path).read_bytes()).hexdigest()


def methods(plan, rendered, recs: list[dict] | None = None) -> dict:
    recs = recs if recs is not None else records(plan)
    spec = plan.spec
    layers = []
    for l, r in zip(all_layers(plan), recs):
        item = {"layer": l.title or l.id, "id": l.id, "role": l.role, "source": r["source_name"], "date": r["retrieved_at"], "license": r["license"],
                "rows": r["row_count"], "url": r.get("source_url"), "library": f"{r['library_ref']} rev {r['library_rev']}" if r.get("library_ref") else None,
                "steps": r["transformations"], "confidentiality": r.get("confidentiality")}
        c = l.cls
        if c is not None and getattr(c, "k", 0):
            item["classification"] = {"method": c.method.replace("_", " "), "classes": c.k, "field": l.value_field, "palette": l.palette_kind}
            if c.kind == "numeric" and c.edges:
                item["classification"]["breaks"] = [round(float(x), 4) for x in c.edges]
        if l.legend_block and (l.spec.get("legend") or {}).get("title"):
            item["legend_title"] = l.spec["legend"]["title"]
        if r.get("ops"):                                     # the methods page names each op and the GeoPandas expression that does the same (M11)
            import geo_ops
            item["ops"] = [{"op": o.get("op"), "params": o.get("params"), "rows_in": o.get("rows_in"), "rows_out": o.get("rows_out"),
                            "measure_crs": o.get("measure_crs"), "equivalent": geo_ops.equivalent(str(o.get("op")), dict(o.get("params") or {}), o)["geopandas"]}
                           for o in r["ops"]]
        if r.get("drawn") is False:
            item["role"] = f"{l.role} (helper, not drawn)"
        layers.append(item)
    notes = [n for l in all_layers(plan) for n in l.notes]
    return {
        "title": rendered.title or spec["id"],
        "message": spec["message"],
        "layers": layers,
        "projection": {"name": plan.crs_info.get("name"), "code": plan.crs, "equal_area": bool(plan.crs_info.get("equal_area")), "why": plan.crs_info.get("rationale")},
        "notes": notes,
        "attribution": attribution_block(plan),
        "basemap": [r["source_name"] for r in recs if r["layer_id"].startswith("basemap:")]
                   + [f"Relief: {r['source_name']} ({'; '.join(r.get('transformations') or [])})" for r in recs if r["layer_id"].startswith("relief:")],
        "reproducibility": {"map_id": spec["id"], "mapspec_sha256": spec_sha256(plan), "built": _today().isoformat(),
                            "plugin": f"{plan.manifest['plugin_name']} {plan.manifest.get('plugin_version', '')}".strip(),
                            "library_version": _library_version()},
    }


def _library_version() -> str:
    p = gc.plugin_root() / "library" / "LIBRARY_VERSION"
    return p.read_text().strip() if p.is_file() else ""


def _para(out: list, x: float, y: float, text: str, w: float, size_pt: float, fill: str, weight: int = 400, gap: float = 0.0) -> float:
    for line in tm.wrap(text, w, size_pt, weight):
        out.append(sd.text(x, y + size_pt * PT * 0.85, line, size_pt, fill, weight=weight))
        y += tm.line_height_mm(size_pt)
    return y + gap


def methods_svg(plan, m: dict, assemble_root) -> str:
    """The appendix as one Letter page. `assemble_root(W, H, body, title, alt)` supplies the root, fonts and hidden metadata."""
    W, H = PAGE_MM
    t = plan.tokens
    mx, my = 18.0, 18.0
    cw = W - 2 * mx
    out = [sd.rect(0, 0, W, H, t["background"])]
    y = my
    y = _para(out, mx, y, "Data & Methods", cw, 20, t["label"], 700, 1.0)
    y = _para(out, mx, y, m["title"], cw, 11, t["label"], 600, 3.0)
    out.append(f'<line x1="{sd.num(mx)}" y1="{sd.num(y)}" x2="{sd.num(mx + cw)}" y2="{sd.num(y)}" stroke="{t["frame"]}" stroke-width="0.25"/>')
    y += 4.0

    def heading(s):
        nonlocal y
        y = _para(out, mx, y + 1.5, s, cw, 11, t["label"], 700, 1.0)

    def body(s, indent=0.0, size=9.0, weight=400, gap=1.2):
        nonlocal y
        y = _para(out, mx + indent, y, s, cw - indent, size, t["label"], weight, gap)

    heading("What this map shows")
    body(m["message"])
    heading("Data")
    for L in m["layers"]:
        body(f"{L['layer']}", weight=600, gap=0.2)
        parts = [f"Source: {L['source']}", f"date: {L['date']}", f"license: {L['license']}", f"{L['rows']:,} features"]
        if L.get("url"):
            parts.append(f"from {L['url']}")
        if L.get("library"):
            parts.append(f"library entry {L['library']}")
        body("; ".join(parts) + ".", indent=4.0, gap=0.4)
        if L.get("classification"):
            c = L["classification"]
            txt = f"Classified by {c['method']} into {c['classes']} classes" + (f" on {c['field']}" if c.get("field") else "") + "."
            if c.get("breaks"):
                txt += " Class edges: " + ", ".join(f"{b:g}" for b in c["breaks"]) + "."
            body(txt, indent=4.0, gap=0.4)
        for s in L["steps"]:
            body("- " + s, indent=4.0, gap=0.2)
        if L.get("confidentiality"):
            body(f"Handling: {L['confidentiality']}.", indent=4.0, gap=0.2)
        y += 1.2
    if m["basemap"]:
        body("Basemap: " + "; ".join(m["basemap"]) + ".")
    heading("Projection")
    p = m["projection"]
    why = p.get("why") or ""
    lead = "" if p["name"] and p["name"] in why else f"{p['name']} ({p['code']}). "
    body(lead + why + (" Equal-area, so areas compare fairly." if p["equal_area"] and "area" not in why.lower() else ""))
    if m["notes"]:
        heading("Notes on the data")
        for n in m["notes"]:
            body("- " + n, gap=0.3)
    if m["attribution"]:
        heading("Credits")
        for a in m["attribution"]:
            body(a, gap=0.3)
    heading("Reproducing this map")
    r = m["reproducibility"]
    body(f"Map {r['map_id']}, built {r['built']} with {r['plugin']}; reference library {r['library_version']}. MapSpec SHA-256 {r['mapspec_sha256'][:16]}…", size=8.0)
    if y > H - my:
        raise ValueError(f"the Data & Methods content is longer than one page ({y:.0f} mm of {H - my:.0f}); shorten the layer list or notes")
    return assemble_root(W, H, "".join(out), f"Data & Methods: {m['title']}", "Sources, processing steps and credits for the map.")


def write_sidecar(path: Path, plan, recs: list[dict], meth: dict | None) -> Path:
    doc = {"map_id": plan.spec["id"], "records": recs, "reproducibility": (meth or {}).get("reproducibility") or {
        "map_id": plan.spec["id"], "mapspec_sha256": spec_sha256(plan), "built": _today().isoformat(), "library_version": _library_version()}}
    if meth:
        doc["methods"] = {k: v for k, v in meth.items() if k != "reproducibility"}
    if getattr(plan, "style", None) is not None:                # 1.4.0 (R2-PACK): the pack, its version and pack.yaml hash
        doc["style"] = plan.style.record()
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Print the provenance records a MapSpec would produce (no rendering).")
    ap.add_argument("--spec", required=True)
    a = ap.parse_args(argv)
    import map_plan as mp
    try:
        plan = mp.prepare(a.spec)
    except mp.PlanError as ex:
        print(f"provenance: {ex}", file=sys.stderr)
        return ex.code
    recs = records(plan)
    print(json.dumps({"records": recs, "findings": validate(plan, recs)}, indent=2, default=str))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in validate(plan, recs)) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
