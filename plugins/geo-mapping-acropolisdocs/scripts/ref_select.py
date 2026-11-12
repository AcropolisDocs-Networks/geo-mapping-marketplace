#!/usr/bin/env python3
"""ref-library select: deterministic, explainable choice of library entries (spec 9.3.5, Appendix F.4).

Input: the facets known from the request or MapSpec. Output: a ranked list with reasons, cautions, join keys, pairings,
exclusions (with the reason) and, when nothing fits, a plain "no reference entry fits" that hands off to geo-find-data.
Nothing here guesses: the same request and library always give the same answer, and a lookalike is never substituted.

Pipeline for each entry:
  1. relevance   type family, use_for (all requested), level (any requested), geography covered. Not relevant -> silent skip.
  2. hard gates  status, license vs audience/use, entry audience and confidentiality, tier, avoid_for. Any failure -> excluded,
                 with every reason listed.
  3. score       weights below; every point and penalty carries a reason string.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lints"))
import library_lint as ll  # noqa: E402

# Weights (documented in docs/milestones/M3/README.md; golden queries pin the resulting order).
W_USE_FOR = 5            # per requested use_for matched
W_LEVEL = 5
W_GEO = {"exact": 4, "region": 3, "country": 2, "continent": 1, "world": 0}
W_SCALE_OK, W_SCALE_MISS = 2, -2
W_DETAIL_OK, W_DETAIL_NEUTRAL, W_DETAIL_HEAVY, W_DETAIL_BLOCKY = 2, 1, -2, -3
W_QUALITY = {"authoritative": 2, "derived": 1, "community": 0, "commercial": 0}
W_BUNDLED = 2
W_STALE = -3
W_NEAR_MISS = -4
W_DEPRECATED = -4
W_PREFERRED_DETAIL = 2

# What a requested scale prefers.
PREFERRED_DETAIL = {"world": "generalized", "national": "generalized", "regional": "generalized", "state": "generalized",
                    "metro": "detailed", "local": "detailed", "site": "detailed"}
# An avoid_for term close to (but not equal to) the request: a near miss, penalized and cautioned, not excluded.
NEAR_MISS = {"site": ("site-precision", "engineering-grade", "parcel-level"), "local": ("site-precision", "parcel-level")}
DATA_FAMILIES = {"data"}
SCOPE_ORDER = {"local": 0, "shipped": 1}
AUDIENCE_OK = {"internal": {"any", "internal", "external", "regulatory"},
               "external": {"any", "external", "regulatory"},
               "regulatory": {"any", "regulatory"},
               "any": {"any", "internal", "external", "regulatory"}}


class SelectError(ValueError):
    pass


def _list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _today(today):
    return today or dt.date.today()


def regions(tags: dict) -> dict[str, set[str]]:
    """Region memberships (us/region/<name> -> state postal codes) and the parent of every geography value (tags.yaml, M13 hierarchy:
    world > north-america > us | ca | mx > us-xx | ca-xx). Kept in one dict so geo_cover takes a single argument: the `_parent` key
    maps a value to its parent."""
    vals = tags["facets"]["geography"]["values"]
    out = {k: set(v.get("members", [])) for k, v in vals.items() if k.startswith("us/region/")}
    out["_parent"] = {k: v.get("parent") for k, v in vals.items() if isinstance(v, dict) and v.get("parent")}
    out["_alias"] = dict(tags["facets"]["geography"].get("aliases") or {})       # 1.5.0: an ISO 3166-2 code's country (gb-eng -> gb -> uk)
    return out


SUBDIVISION = re.compile(r"^([a-z]{2})-[a-z0-9]{1,3}$")


def _ancestors(geo: str, parent: dict[str, str], alias: dict | None = None) -> list[str]:
    """The value's parents, nearest first: us-az -> [us, north-america, world]; us/region/west -> [us, north-america, world];
    1.5.0: an ISO 3166-2 code not listed by hand -> its country, then the country's M49 sub-region and region (ng-la -> [ng, sub-saharan-africa,
    africa, world])."""
    out, cur, seen = [], geo, set()
    while True:
        nxt = parent.get(cur)
        if nxt is None and cur.startswith("us/region/"):
            nxt = "us"
        if nxt is None and SUBDIVISION.match(cur):
            cc = SUBDIVISION.match(cur).group(1)
            nxt = cc if cc in parent else (alias or {}).get(cc)
        if not nxt or nxt in seen:
            break
        out.append(nxt)
        seen.add(nxt)
        cur = nxt
    return out


def _state_of(want: str) -> str | None:
    return want[3:].upper() if want.startswith("us-") and len(want) == 5 else None


def geo_cover(entry_geos: list[str], want: str, reg: dict) -> tuple[str, str] | None:
    """How the entry's geography covers the requested one: (kind, entry geography) or None.

    exact: the same value. region: a us/region entry whose members include the requested state. country: the country the requested
    state, province or region belongs to. continent: north-america or europe over a country or its subdivisions. world: anything. An entry
    narrower than the request (a state entry for a national request) does not cover it."""
    parent = reg.get("_parent", {})
    anc = _ancestors(want, parent, reg.get("_alias"))
    best = None
    rank = {"exact": 0, "region": 1, "country": 2, "continent": 3, "world": 4}
    state = _state_of(want)
    for g in entry_geos:
        kind = None
        if g == want:
            kind = "exact"
        elif g == "world":
            kind = "world"
        elif g.startswith("us/region/") and state and state in reg.get(g, set()):
            kind = "region"
        elif g in anc:
            kind = "continent" if parent.get(g) == "world" else "country"     # north-america, europe
        if kind and (best is None or rank[kind] < rank[best[0]]):
            best = (kind, g)
    return best


def normalise_request(req: dict, tags: dict) -> dict:
    """Resolve aliases and reject terms that are not in the vocabulary; returns a clean copy."""
    out = dict(req)
    for facet in ("geography", "use_for", "level", "scale", "detail", "audience", "confidentiality", "tier", "domain"):
        if req.get(facet) in (None, [], ""):
            out.pop(facet, None)
            continue
        vals = []
        for v in _list(req[facet]):
            r = ll.resolve_tag(tags, facet, str(v))
            if r is None:
                raise SelectError(f"'{v}' is not a {facet} term in the library vocabulary (tags.yaml)")
            vals.append(r)
        out[facet] = vals if facet in ("use_for", "level", "domain") else vals[0]
    if out.get("level"):                            # 1.5.0: admin1 is state, admin2 county, ... (tags.yaml level `equivalent`)
        lv = tags["facets"]["level"]["values"]
        more = list(out["level"])
        for v in out["level"]:
            more += list((lv.get(v) or {}).get("equivalent") or [])
            more += [k for k, d in lv.items() if v in ((d or {}).get("equivalent") or [])]
        out["level"] = list(dict.fromkeys(more))
    return out


def geography_of_window(bbox, tags: dict) -> tuple[str | None, str]:
    """1.5.0 (R3-LIB): the geography of a map's window when the request names none: the country under the centre, as a library term
    (GB is `uk`); None at sea. -> (term, why)."""
    import geo_locale as gl
    x0, y0, x1, y1 = (float(v) for v in bbox)
    iso = gl.country_at((x0 + x1) / 2, (y0 + y1) / 2)
    if not iso:
        return None, "the window's centre is not in a country"
    t = ll.resolve_tag(tags, "geography", iso.lower())
    return t, f"the country under the window's centre is {iso}"


def license_gate(e: dict, req: dict) -> tuple[list[str], list[str]]:
    """(reasons for exclusion, cautions) from the license class against the stated audience and use."""
    lic = e.get("license") or {}
    lc = lic.get("class")
    aud = req.get("audience", "any")
    reasons, cautions = [], []
    external = aud in ("external", "regulatory")
    if lc == "licensed":
        if external and not lic.get("external_ok"):
            reasons.append(f"license_class=licensed; audience={aud} not permitted")
        elif (e.get("delivery") or {}).get("mode") != "user-supplied":
            reasons.append("license_class=licensed but the entry is not user-supplied (licensed data cannot be bundled or fetched)")
        else:
            cautions.append("Licensed data: you must supply your own licensed copy; it is never bundled.")
    elif lc == "restricted":
        if external and not lic.get("external_ok"):
            reasons.append(f"license_class=restricted; audience={aud} not permitted")
    if req.get("commercial") and lic.get("commercial_ok") is False:
        reasons.append("commercial use is not permitted by the license")
    if req.get("redistribute") and lic.get("redistribute_ok") is False:
        reasons.append("redistribution is not permitted by the license")
    if lc == "share-alike" and (external or req.get("redistribute")):
        cautions.append("Share-alike: a derived map or dataset carries the same license (MAP-LIC-004).")
    if lc in ("attribution", "share-alike", "open") and lic.get("attribution_text"):
        cautions.append(f"Attribution required: {lic['attribution_text']}")
    return reasons, cautions


def gates(e: dict, req: dict, today: dt.date) -> tuple[list[str], list[str]]:
    reasons, cautions = [], []
    tg = e.get("tags") or {}
    st = e.get("status")
    if st == "blocked":
        reasons.append("status=blocked: " + str((e.get("blocked") or {}).get("why") or e.get("summary", "the plugin must refuse this source")))
    elif st == "draft":
        reasons.append("status=draft: not verified, so never chosen for a map (verify it, or find another source)")
    if st in ("blocked", "draft"):
        # The license and audience reasons are still worth giving for a blocked source.
        pass
    lr, lc = license_gate(e, req)
    reasons += lr
    cautions += lc
    aud = req.get("audience", "any")
    ent_aud = set(tg.get("audience") or ["any"])
    if not ent_aud & AUDIENCE_OK[aud]:
        reasons.append(f"entry audience={'/'.join(sorted(ent_aud))}; audience={aud} not permitted")
    conf = set(tg.get("confidentiality") or ["public"])
    if aud in ("external", "regulatory") and conf & {"internal", "client-confidential"} and "public" not in conf:
        reasons.append(f"entry confidentiality={'/'.join(sorted(conf))}; audience={aud} not permitted")
    tier = req.get("tier")
    if tier:
        tiers = set(tg.get("tier") or [])
        mode = (e.get("delivery") or {}).get("mode")
        if mode == "bundled":
            tiers |= {"T0", "T1", "T2"}
        if tier not in tiers:
            reasons.append(f"not reachable in tier {tier} (available in {', '.join(sorted(tiers)) or 'no tier'})")
    avoid = set(tg.get("avoid_for") or [])
    for want in _list(req.get("use_for")) + _list(req.get("task")):
        if want in avoid:
            reasons.append(f"avoid_for contains '{want}'")
    return reasons, cautions


def _scale_detail(e: dict, req: dict) -> tuple[int, list[dict], list[dict], list[str]]:
    pts, matched, pen, cautions = 0, [], [], []
    tg = e.get("tags") or {}
    scale = req.get("scale")
    if scale:
        if scale in (tg.get("scale") or []):
            pts += W_SCALE_OK
            matched.append({"facet": "scale", "points": W_SCALE_OK, "text": f"scale={scale}"})
        else:
            pts += W_SCALE_MISS
            pen.append({"facet": "scale", "points": W_SCALE_MISS, "text": f"scale={scale} is outside the entry's scales ({', '.join(tg.get('scale') or []) or 'none stated'})"})
    detail = _list(tg.get("detail"))
    if detail:
        want = req.get("detail") or (PREFERRED_DETAIL.get(scale) if scale else None)
        have = detail[0]
        if want:
            if have == want:
                pts += W_DETAIL_OK
                matched.append({"facet": "detail", "points": W_DETAIL_OK, "text": f"detail={have} fits scale={scale or 'requested'}"})
            elif have == "simplified":
                pts += W_DETAIL_NEUTRAL
                matched.append({"facet": "detail", "points": W_DETAIL_NEUTRAL, "text": "detail=simplified is acceptable"})
            elif have == "detailed":
                pts += W_DETAIL_HEAVY
                pen.append({"facet": "detail", "points": W_DETAIL_HEAVY, "text": f"detail=detailed is heavy for scale={scale or 'requested'}"})
            else:
                pts += W_DETAIL_BLOCKY
                pen.append({"facet": "detail", "points": W_DETAIL_BLOCKY, "text": f"detail=generalized looks blocky at scale={scale or 'requested'}"})
    return pts, matched, pen, cautions


def score_entry(e: dict, req: dict, cover: tuple[str, str] | None, today: dt.date) -> dict:
    tg = e.get("tags") or {}
    matched: list[dict] = []
    pen: list[dict] = []
    cautions: list[str] = []
    total = 0
    for u in _list(req.get("use_for")):
        total += W_USE_FOR
        matched.append({"facet": "use_for", "points": W_USE_FOR, "text": f"use_for={u}"})
    lv = [x for x in _list(req.get("level")) if x in (tg.get("level") or [])]
    if lv:
        total += W_LEVEL
        matched.append({"facet": "level", "points": W_LEVEL, "text": f"level={lv[0]}"})
    if cover:
        kind, g = cover
        w = W_GEO[kind]
        total += w
        want = req["geography"]
        txt = {"exact": f"geography={g}", "region": f"geography={g} (contains {want})", "country": f"geography={g} (covers {want})",
               "continent": f"geography={g} (covers {want})", "world": f"geography={g} (covers {want})"}[kind]
        matched.append({"facet": "geography", "points": w, "text": txt})
    p, m, pn, c = _scale_detail(e, req)
    total += p
    matched += m
    pen += pn
    cautions += c
    q = (tg.get("quality") or [None])[0]
    if q in W_QUALITY and W_QUALITY[q]:
        total += W_QUALITY[q]
        matched.append({"facet": "quality", "points": W_QUALITY[q], "text": f"quality={q}"})
    if (e.get("delivery") or {}).get("mode") == "bundled":
        total += W_BUNDLED
        matched.append({"facet": "delivery", "points": W_BUNDLED, "text": "bundled (T0)"})
    for term in NEAR_MISS.get(req.get("scale"), ()):
        if term in (tg.get("avoid_for") or []):
            total += W_NEAR_MISS
            pen.append({"facet": "avoid_for", "points": W_NEAR_MISS, "text": f"avoid_for={term} is close to a {req['scale']}-scale task"})
            cautions.append(f"This entry says to avoid it for {term}; check the map does not need that precision.")
            break
    if e.get("status") == "deprecated":
        total += W_DEPRECATED
        sup = e.get("superseded_by")
        pen.append({"facet": "status", "points": W_DEPRECATED, "text": "status=deprecated"})
        cautions.append("Deprecated" + (f"; use {', '.join(_list(sup))} instead." if sup else "."))
    m_ = e.get("maintenance") or {}
    vb = ll._d(m_.get("verify_by"))
    if vb and today > vb:
        total += W_STALE
        pen.append({"facet": "freshness", "points": W_STALE, "text": f"verify_by {vb} is past due"})
        cautions.append(f"Review is overdue (verify_by {vb}); confirm the source is still current before relying on it.")
    nxt = ll._d((e.get("vintage") or {}).get("next_expected"))
    if nxt and today >= nxt:
        cautions.append(f"A newer vintage than {(e.get('vintage') or {}).get('current')} was expected by {nxt}; check the publisher.")
    elif (e.get("vintage") or {}).get("current") and (e.get("vintage") or {}).get("cadence") not in (None, "static"):
        cautions.append(f"Vintage {(e['vintage'])['current']} ({e['vintage']['cadence']} release).")
    return {"score": total, "matched": matched, "penalties": pen, "cautions": cautions}


def join_note(e: dict, req: dict) -> dict | None:
    j = e.get("join")
    if not j:
        return None
    out = {"key": j.get("key"), "type": j.get("type"), "width": j.get("width"), "note": j.get("note")}
    ck = req.get("join_key")
    if ck:
        out["client_key"] = ck
        if j.get("type") == "string":
            width = f", width {j['width']}" if j.get("width") else ""
            pad = f"; zero-pad to {j['width']} characters" if j.get("width") else ""
            out["caution"] = f"Client key '{ck}' must be mapped to {j['key']} (string{width}){pad}; never cast to integer."
        else:
            out["caution"] = f"Client key '{ck}' joins on {j['key']} ({j.get('type')})."
    return out


def pairs_of(e: dict, by_id: dict) -> list[dict]:
    out = []
    for pid in _list(e.get("pairs_with")):
        p = by_id.get(pid)
        if not p:
            continue
        ptg = p.get("tags") or {}
        if "normalization" in (ptg.get("use_for") or []):
            role = "normalization denominator"
        else:
            role = " ".join(x for x in (", ".join(ptg.get("level") or []), p.get("type")) if x)
        out.append({"id": pid, "title": p.get("title"), "status": p.get("status"), "role": role})
    return out


def entry_geos(e: dict, tags: dict) -> list[str]:
    """The entry's geography tags in canonical spelling (aliases such as us/state/az resolve to us-az); unknown terms are kept as written."""
    out = []
    for g in _list((e.get("tags") or {}).get("geography")):
        r = ll.resolve_tag(tags, "geography", str(g))
        out.append(r if r is not None else str(g))
    return out


def _relevant(e: dict, req: dict, families: set, reg: dict, types: dict, tags: dict | None = None) -> tuple[bool, tuple | None]:
    spec = types["types"].get(e.get("type"))
    if not spec:
        return False, None
    if req.get("type"):
        if e.get("type") != req["type"]:
            return False, None
    elif spec["family"] not in families:
        return False, None
    if e.get("status") == "retired":
        return False, None
    tg = e.get("tags") or {}
    for u in _list(req.get("use_for")):
        if u not in (tg.get("use_for") or []):
            return False, None
    if req.get("level") and not set(_list(req["level"])) & set(tg.get("level") or []):
        return False, None
    cover = None
    if req.get("geography"):
        cover = geo_cover(entry_geos(e, tags) if tags else (tg.get("geography") or []), req["geography"], reg)
        if not cover:
            return False, None
    return True, cover


def select(entries: list[dict], tags: dict, types: dict, req: dict, today: dt.date | None = None, limit: int = 5,
           families: set | None = None) -> dict:
    """Rank data (or other) entries for a request. `entries` may mix scopes; a local entry supersedes a shipped one of the same id."""
    today = _today(today)
    req = dict(req)
    bbox = req.pop("bbox", None)
    geo_why = None
    if bbox and not req.get("geography"):
        g, geo_why = geography_of_window(bbox, tags)
        if g:
            req["geography"] = g
    req = normalise_request(req, tags)
    families = families or ({"data", "style", "method"} if req.get("type") else DATA_FAMILIES)
    reg = regions(tags)
    # local overlay wins over shipped for the same id; the replacement is reported
    by_id: dict[str, dict] = {}
    overridden: list[dict] = []
    for e in sorted(entries, key=lambda x: SCOPE_ORDER.get(x.get("_scope", "shipped"), 1)):
        if "id" not in e:
            continue
        if e["id"] in by_id:
            overridden.append({"id": e["id"], "used": by_id[e["id"]].get("_scope"), "ignored": e.get("_scope")})
            continue
        by_id[e["id"]] = e
    results, excluded = [], []
    for e in by_id.values():
        ok, cover = _relevant(e, req, families, reg, types, tags)
        if not ok:
            continue
        reasons, cautions = gates(e, req, today)
        if reasons:
            excluded.append({"id": e["id"], "rev": e.get("rev"), "status": e.get("status"), "title": e.get("title"), "reasons": reasons})
            continue
        sc = score_entry(e, req, cover, today)
        d = e.get("delivery") or {}
        results.append({
            "id": e["id"], "rev": e.get("rev"), "type": e.get("type"), "title": e.get("title"), "status": e.get("status"), "scope": e.get("_scope", "shipped"),
            "score": sc["score"], "matched": sc["matched"], "penalties": sc["penalties"],
            "cautions": lc_unique(license_gate(e, req)[1] + sc["cautions"]),
            "join": join_note(e, req), "pairs": pairs_of(e, by_id),
            "delivery": {"mode": d.get("mode"), "path": d.get("path"), "tiers": (e.get("tags") or {}).get("tier")},
            "license": {"class": (e.get("license") or {}).get("class"), "attribution_text": (e.get("license") or {}).get("attribution_text")},
            "use_when": e.get("use_when"), "avoid_when": e.get("avoid_when"),
            "_bundled": d.get("mode") == "bundled",
        })
    results.sort(key=lambda r: (-r["score"], not r["_bundled"], r["id"]))
    for i, r in enumerate(results, 1):
        r["rank"] = i
        del r["_bundled"]
    excluded.sort(key=lambda x: x["id"])
    out = {"request": {k: v for k, v in req.items()}, "fit": bool(results), "results": results[:limit], "alternates": max(0, len(results) - limit),
           "excluded": excluded, "overridden": overridden}
    if geo_why:
        out["geography_from_window"] = geo_why
    if not results:
        out["message"] = "no reference entry fits"
        out["handoff"] = "geo-find-data"
        out["detail"] = ("No active library entry matches all of: " + ", ".join(f"{k}={v}" for k, v in out["request"].items() if k in ("geography", "use_for", "level"))
                         + (f". {len(excluded)} matching entr{'y was' if len(excluded) == 1 else 'ies were'} excluded (see 'excluded')." if excluded else "."))
    return out


def lc_unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def choose_map_type(entries: list[dict], tags: dict, types: dict, req: dict, today: dt.date | None = None) -> dict | None:
    """Pick a map-type entry from the message type and data shape (spec 9.3.5 step 5). Entries state `chooses_when`."""
    today = _today(today)
    want_mt, want_ds, want_use = req.get("message_type"), req.get("data_shape"), req.get("map_type")
    cands = []
    for e in sorted(entries, key=lambda x: x.get("id", "")):
        if e.get("type") != "map-type" or e.get("status") not in ("active", "deprecated"):
            continue
        short = e["id"].rsplit(".", 1)[-1]
        if want_use and want_use not in (short, e["id"]):
            continue
        cw = e.get("chooses_when") or {}
        if want_mt and want_mt not in _list(cw.get("message_type")):
            continue
        if want_ds and want_ds not in _list(cw.get("data_shape")):
            continue
        cands.append(e)
    if not cands:
        return None
    # more specific `chooses_when` wins, then id
    cands.sort(key=lambda e: (len((e.get("chooses_when") or {}).get("message_type", [])) + len((e.get("chooses_when") or {}).get("data_shape", [])), e["id"]))
    e = cands[0]
    return {"id": e["id"], "rev": e.get("rev"), "title": e.get("title"), "use_for": (e.get("tags") or {}).get("use_for"),
            "requires": e.get("requires"), "recommended": e.get("recommended"), "use_when": e.get("use_when"), "avoid_when": e.get("avoid_when"),
            "guarded_by": e.get("guarded_by"), "alternatives": [c["id"] for c in cands[1:]],
            "chosen_for": {k: v for k, v in (("message_type", want_mt), ("data_shape", want_ds), ("map_type", want_use)) if v}}


def select_all(entries: list[dict], tags: dict, types: dict, req: dict, today: dt.date | None = None, limit: int = 5) -> dict:
    """Map type first (when the request names a message or data shape), then data entries that serve that map type."""
    req = dict(req)
    mt = None
    if any(req.get(k) for k in ("message_type", "data_shape", "map_type")):
        mt = choose_map_type(entries, tags, types, req, today)
        if mt is None:
            return {"request": req, "fit": False, "results": [], "excluded": [], "map_type": None, "message": "no reference entry fits",
                    "handoff": "geo-find-data", "detail": "No active map-type entry matches that message type and data shape."}
        if not req.get("use_for") and mt.get("use_for"):
            req["use_for"] = mt["use_for"]
    for k in ("message_type", "data_shape", "map_type"):
        req.pop(k, None)
    out = select(entries, tags, types, req, today, limit)
    out["map_type"] = mt
    return out


def render_text(res: dict) -> str:
    lines = []
    r = res["request"]
    lines.append("request: " + "  ".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in r.items()))
    for x in res["results"]:
        lines.append(f"{x['rank']}. {x['id']}  rev {x['rev']}  score {x['score']}")
        lines.append("   matched: " + "; ".join(m["text"] for m in x["matched"]))
        if x["penalties"]:
            lines.append("   penalty: " + "; ".join(p["text"] for p in x["penalties"]))
        for c in x["cautions"]:
            lines.append("   caution: " + c)
        if x.get("join"):
            j = x["join"]
            lines.append(f"   join: {j['key']} ({j['type']}{', width ' + str(j['width']) if j.get('width') else ''})" + (f". {j['caution']}" if j.get("caution") else ""))
        for p in x["pairs"]:
            lines.append(f"   pairs: {p['id']} ({p['role']})")
    if res.get("alternates"):
        lines.append(f"({res['alternates']} more alternate(s) not shown)")
    for x in res["excluded"]:
        lines.append(f"excluded: {x['id']} ({'; '.join(x['reasons'])})")
    mt = res.get("map_type")
    if mt:
        lines.append(f"map type: {mt['id']} ({', '.join(f'{k}={v}' for k, v in mt['chosen_for'].items())})")
    if not res["fit"]:
        lines.append(f"{res['message']}: {res.get('detail', '')} Hand off to {res['handoff']}.")
    return "\n".join(lines)
