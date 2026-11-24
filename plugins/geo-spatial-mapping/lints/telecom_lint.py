"""Telecom lint, MAP-TEL-* (M6). Reads the finished plan; no network, no writes.

    lint_plan(plan) -> [{"id", "severity", "message", "detail"}]

MAP-TEL-001 (privacy distance too small) is raised where the reduction is applied (site_privacy), and MAP-TEL-010..018 where a site list is read (telecom_geo).

002  WARN  a client-confidential point or wedge layer on an external map is drawn at full precision (add `privacy:`)
003  WARN  data from the FCC National Broadband Map is called "coverage"; it is provider-reported availability
020  WARN  sector wedges are called coverage; they are display geometry
021  INFO  a wedge layer's radius is a drawing choice (always reported so the legend note and methods say so)
"""
from __future__ import annotations

import re

COVER = re.compile(r"\bcover(?:age|s|ed|ing)?\b", re.I)


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _texts(plan) -> list[tuple[str, str]]:
    spec = plan.spec
    el = spec.get("elements") or {}
    out = [("message", str(spec.get("message") or ""))]
    t = el.get("title") or {}
    out += [("title", str(t.get("text") or "")), ("subtitle", str(t.get("subtitle") or ""))]
    for l in plan.layers:
        out.append((f"legend of '{l.id}'", str(((l.spec.get("legend") or {}).get("title")) or "")))
    return [(k, v) for k, v in out if v]


def lint_plan(plan) -> list[dict]:
    out = []
    audience = plan.spec.get("audience") or "external"
    texts = _texts(plan)
    for l in plan.layers:
        pts = l.kind == "point" or l.wedge_info
        if l.confidentiality == "client-confidential" and pts and not l.privacy and audience == "external":
            out.append(finding("MAP-TEL-002", "WARN",
                               f"layer '{l.id}': client-confidential site positions are drawn at full precision on an external map; add `privacy: {{method: precision, meters: 500}}` or confirm they may be shown", l.id))
        ref = " ".join(str(x) for x in [(l.provenance or {}).get("library_ref"), (l.entry or {}).get("id"), (l.provenance or {}).get("source")] if x)
        if re.search(r"fcc-nbm|national broadband map", ref, re.I):
            hit = [k for k, v in texts if COVER.search(v) and "availab" not in v.lower()]
            if hit:
                out.append(finding("MAP-TEL-003", "WARN",
                                   f"layer '{l.id}' is FCC National Broadband Map data, which is provider-reported availability, not measured coverage; say 'availability' in the {', '.join(hit)}", hit))
        if l.wedge_info:
            hit = [k for k, v in texts if COVER.search(v)]
            if hit:
                out.append(finding("MAP-TEL-020", "WARN",
                                   f"layer '{l.id}' draws sector wedges, which are display geometry; the {', '.join(hit)} call(s) them coverage. Say 'sectors' or 'antenna direction'", hit))
            out.append(finding("MAP-TEL-021", "INFO", f"layer '{l.id}': wedge radius {l.wedge_info['radius_m']:g} m is a drawing choice, stated in the methods note", l.wedge_info))
    return out
