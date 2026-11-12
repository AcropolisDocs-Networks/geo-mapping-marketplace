"""Reference lint, MAP-REF-* (spec 12.7d): checks a map's use of the reference library. Not the library's own lint (library_lint.py).

    lint_plan(plan, entries, tags, types, today)   001-004, 006-009 from the plan (layers, map type, audience)
    check_join(entry, values, key_values=None)     005, for a client table's key column against a library entry's join key

Rule summary (BLOCK unless noted):
  001 every library id the MapSpec names resolves        002 status is not blocked, retired or draft (deprecated: WARN, names its replacement)
  003 stale entry or vintage (WARN)                      004 the license class permits the stated audience and use
  005 join key present, same type and width             006 the map type fits the layers (needs a normalized rate, and so on)
  007 the task is in avoid_for (WARN)                    008 provenance records library_ref and library_rev
  009 a local entry that supersedes a shipped one is named (WARN)
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import library_lint as ll  # noqa: E402

AUDIENCES = ("any", "internal", "external", "regulatory")
DEFAULT_AUDIENCE = "external"      # the safe assumption when a MapSpec does not say who sees the map


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def map_type_entry(spec: dict, entries: list[dict]) -> tuple[dict | None, str | None]:
    """The map-type entry the MapSpec names (`choropleth` or `ref.map-type.choropleth`), or (None, name)."""
    name = spec.get("map_type")
    if not name:
        return None, None
    full = name if name.startswith("ref.") else f"ref.map-type.{name}"
    for e in entries:
        if e.get("id") == full:
            return e, name
    return None, name


def lint_plan(plan, entries: list[dict], tags: dict, types: dict, today: dt.date | None = None) -> list[dict]:
    import ref_select as rs
    today = today or dt.date.today()
    spec = plan.spec
    audience = spec.get("audience") or DEFAULT_AUDIENCE
    out: list[dict] = []
    ids = {e.get("id"): e for e in entries if e.get("_scope") == "shipped"}
    shipped_ids = set(ids)

    # map type
    mt, name = map_type_entry(spec, entries)
    if name and mt is None:
        out.append(finding("MAP-REF-001", "BLOCK", f"map_type '{name}' is not a map-type entry in the library; choose one with ref-library select", name))
    elif mt is not None:
        out += _status(mt, f"map type '{name}'")
        out += _stale(mt, today)
        roles = {l.role for l in plan.layers}
        uf = set((mt.get("tags") or {}).get("use_for") or [])
        if uf and not _role_fits(name, plan):
            out.append(finding("MAP-REF-006", "BLOCK", f"map_type '{name}' does not fit the layers ({', '.join(sorted(roles))}); {mt.get('title')} needs "
                               f"{(mt.get('requires') or {}).get('data_shape', 'different data')}", {"roles": sorted(roles)}))
        for term in rs._list(spec.get("task")):
            if term in ((mt.get("tags") or {}).get("avoid_for") or []):
                out.append(finding("MAP-REF-007", "WARN", f"map type '{name}' lists '{term}' in avoid_for"))

    # layers
    for l in plan.layers:
        e = l.entry
        if e is None:
            continue
        src = (l.spec.get("source") or {})
        label = f"layer '{l.id}' ({e['id']})"
        out += _status(e, label)
        out += _stale(e, today, label)
        reasons, _ = rs.gates(e, {"audience": audience, "use_for": [], "task": rs._list(spec.get("task"))}, today)
        lic = [r for r in reasons if r.startswith(("license_class", "entry audience", "entry confidentiality", "commercial", "redistribution"))]
        for r in lic:
            out.append(finding("MAP-REF-004", "BLOCK", f"{label}: {r} (audience of this map: {audience})", {"audience": audience, "entry": e["id"]}))
        avoid = (e.get("tags") or {}).get("avoid_for") or []
        for term in rs._list(spec.get("task")):
            if term in avoid:
                out.append(finding("MAP-REF-007", "WARN", f"{label}: the task '{term}' is in the entry's avoid_for"))
        p = l.provenance or {}
        if p.get("library_ref") != e["id"] or p.get("library_rev") != e.get("rev"):
            out.append(finding("MAP-REF-008", "BLOCK", f"{label}: provenance does not record library_ref and library_rev", {"have": {k: p.get(k) for k in ("library_ref", "library_rev")}}))
        if e.get("_scope") == "local" and e["id"] in shipped_ids:
            out.append(finding("MAP-REF-009", "WARN", f"{label}: a local entry supersedes the shipped entry of the same id; the QA report names it", e["id"]))
        sup = [s for s in rs._list(e.get("supersedes")) if s in shipped_ids]
        if e.get("_scope") == "local" and sup:
            out.append(finding("MAP-REF-009", "WARN", f"{label}: the local entry supersedes {', '.join(sup)}; the QA report names it", sup))
        if src.get("type") == "ref" and src.get("vintage") and (e.get("vintage") or {}).get("current") not in (None, str(src["vintage"])):
            out.append(finding("MAP-REF-003", "WARN", f"{label}: the MapSpec asks for vintage {src['vintage']} but the library holds {e['vintage']['current']}"))
    return out


def _role_fits(name: str, plan) -> bool:
    roles = {l.role for l in plan.layers}
    short = name.rsplit(".", 1)[-1]
    want = {"choropleth": {"choropleth"}, "proportional-symbol": {"proportional"}, "site-map": {"points", "lines", "polygons"},
            "locator-inset": None}.get(short, None)
    if want is None:
        return True
    return bool(roles & want)


def _status(e: dict, label: str) -> list[dict]:
    st = e.get("status")
    out = []
    if st in ("blocked", "retired", "draft"):
        why = (e.get("blocked") or {}).get("why") if st == "blocked" else None
        out.append(finding("MAP-REF-002", "BLOCK", f"{label} is {st}" + (f": {why}" if why else "; it cannot be used for a map"), e.get("id")))
    elif st == "deprecated":
        sup = e.get("superseded_by")
        out.append(finding("MAP-REF-002", "WARN", f"{label} is deprecated" + (f"; use {', '.join(ll._as_list(sup))}" if sup else ""), e.get("id")))
    return out


def _stale(e: dict, today: dt.date, label: str | None = None) -> list[dict]:
    out = []
    label = label or e.get("id")
    vb = ll._d((e.get("maintenance") or {}).get("verify_by"))
    if vb and today > vb:
        out.append(finding("MAP-REF-003", "WARN", f"{label}: review overdue since {vb}; confirm the source is current", e.get("id")))
    nxt = ll._d((e.get("vintage") or {}).get("next_expected"))
    if nxt and today >= nxt:
        out.append(finding("MAP-REF-003", "WARN", f"{label}: a newer vintage than {(e.get('vintage') or {}).get('current')} was expected by {nxt}", e.get("id")))
    return out


# ---------------------------------------------------------------------------------------------------------------- MAP-REF-005
def check_join(entry: dict, values, key_values=None, column: str = "key") -> list[dict]:
    """Does a client key column join to the entry's join key? `values` is an iterable of the client's raw cell values;
    `key_values` is the library side's key set when the payload is at hand."""
    j = entry.get("join") or {}
    out: list[dict] = []
    if not j:
        return [finding("MAP-REF-005", "BLOCK", f"{entry.get('id')} has no join key; it cannot be joined to a table")]
    want_w, want_t = j.get("width"), j.get("type", "string")
    vals = [v for v in values if v is not None and str(v).strip() != "" and str(v).lower() != "nan"]
    if not vals:
        return [finding("MAP-REF-005", "BLOCK", f"column '{column}' has no values to join")]
    raw = [str(v).strip() for v in vals]
    nonnum = [s for s in raw if not re.fullmatch(r"\d+(\.0+)?", s)]
    floaty = [s for s in raw if re.fullmatch(r"\d+\.0+", s)]
    if want_t == "string" and want_w:
        if floaty:
            out.append(finding("MAP-REF-005", "BLOCK", f"column '{column}' holds numbers with a decimal point ({floaty[0]}); the key {j['key']} is a string of {want_w} characters. "
                               "Read the column as text, never as a number", floaty[:3]))
        elif not nonnum:
            widths = sorted({len(s) for s in raw})
            if widths != [want_w]:
                short = [s for s in raw if len(s) < want_w]
                if short and not [s for s in raw if len(s) > want_w]:
                    out.append(finding("MAP-REF-005", "BLOCK", f"column '{column}' has values shorter than {want_w} characters ({short[0]}); leading zeros were probably lost. "
                                       f"Zero-pad to {want_w} (for example {short[0].zfill(want_w)}) and read the column as text", short[:3]))
                else:
                    out.append(finding("MAP-REF-005", "BLOCK", f"column '{column}' has values of width {widths}; the key {j['key']} is {want_w} characters", widths))
        else:
            bad = [s for s in raw if len(s) != want_w]
            if bad:
                out.append(finding("MAP-REF-005", "BLOCK", f"column '{column}' has values that are not {want_w} characters ({bad[0]}); the key {j['key']} is {want_w} characters", bad[:3]))
    pat = j.get("pattern")
    if pat and not [f for f in out if f["severity"] == "BLOCK"]:
        odd = [s for s in raw if not re.fullmatch(pat, s)]
        if odd:
            out.append(finding("MAP-REF-005", "BLOCK", f"values in '{column}' such as {odd[0]} do not look like {j['key']} ({j.get('note', '').split('.')[0] or 'a code'}); "
                               "this is probably a different kind of code, for example a postal abbreviation or a name instead of the numeric code", odd[:3]))
    if key_values is not None and not [f for f in out if f["severity"] == "BLOCK"]:
        keys = {str(k) for k in key_values}
        hit = sum(1 for s in raw if s in keys)
        rate = hit / len(raw)
        if rate < 0.5:
            out.append(finding("MAP-REF-005", "BLOCK", f"only {rate:.0%} of the values in '{column}' match {j['key']} in {entry.get('id')}; this is the wrong key or the wrong geography", {"matched": hit, "of": len(raw)}))
        elif rate < 0.98:
            miss = [s for s in raw if s not in keys][:3]
            out.append(finding("MAP-REF-005", "WARN", f"{len(raw) - hit} of {len(raw)} values in '{column}' do not match {j['key']} (for example {', '.join(miss)})", miss))
    return out
