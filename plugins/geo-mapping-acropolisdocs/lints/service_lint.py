#!/usr/bin/env python3
"""service_lint: MAP-SVC-* checks on service requests, profiles, captures and user-run manifests (spec 12.7b).

    service_lint.py capture CAPTURE.json [--audience external]
    service_lint.py url URL

`lint_capture` is the gate on a finished capture: complete record (008), bounded (004), complete paging (005), stable paging (006), coordinates where the
layer says (007), backoff (010), a license that is known (012), no secret anywhere in the record (022). A user-run capture adds 021, 024.

M13 (0.6.0) adds the one-shot address path and geocoding:
  MAP-SVC-033 BLOCK  a service address given as `url=` whose host the person has not confirmed in this session (`lint_oneshot`)
  MAP-SVC-034 BLOCK  a pasted address whose kind of service (ArcGIS REST, WFS, WMS, OGC API) cannot be told; save a profile instead
  MAP-SVC-035 BLOCK  geocoding privacy (spec 14): no `purpose` (sites | venues | businesses), or columns that look like private individuals (`lint_geocode_columns`)
  MAP-SVC-036 BLOCK  a geocoder whose terms were not acknowledged (Nominatim: one request a second, a named User-Agent, ODbL attribution) (`lint_geocode_terms`)
  MAP-SVC-037 WARN   a geocoding run that left rows unmatched, or whose match rate is below 80 percent (`lint_geocode_result`)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import redact  # noqa: E402

SECRET_PATTERNS = redact.SECRET_PATTERNS
RECORD_REQUIRED = ("capture_version", "id", "route", "profile", "protocol", "server", "request", "started_at", "finished_at", "hits_expected", "received", "truncated", "pages", "calls", "result", "license")
CALL_REQUIRED = ("url", "status", "bytes", "ms", "sha256")


def f(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def find_secrets(text: str) -> list[str]:
    return [label for pat, label in SECRET_PATTERNS if pat.search(text)]


def license_known(rec: dict) -> bool:
    lic = rec.get("license") or {}
    pd = (lic.get("profile_default") or "").lower()
    if pd and pd not in ("unknown", "per-service-abstract"):
        return True
    return any(lic.get(k) for k in ("abstract", "fees", "access_constraints", "copyright")) and pd != "unknown"


def lint_capture(rec: dict, profile: dict | None = None, audience: str | None = None) -> list[dict]:
    out: list[dict] = []
    missing = [k for k in RECORD_REQUIRED if k not in rec]
    if missing:
        out.append(f("MAP-SVC-008", "BLOCK", "the capture record is incomplete", missing))
    for i, c in enumerate(rec.get("calls") or []):
        m = [k for k in CALL_REQUIRED if k not in c]
        if m:
            out.append(f("MAP-SVC-008", "BLOCK", f"call {i + 1} in the capture record lacks {', '.join(m)}"))
            break
    if rec.get("route") == "mcp" and not rec.get("calls"):
        out.append(f("MAP-SVC-008", "BLOCK", "no network call is recorded for a capture made through the bundled server"))
    req = rec.get("request") or {}
    hits, got = rec.get("hits_expected"), rec.get("received")
    if hits is None and not req.get("max_features") and rec.get("route") not in ("user-run", "device-run"):
        out.append(f("MAP-SVC-004", "BLOCK", "the request is not bounded: the service gave no count and no max_features was set"))
    if rec.get("truncated") and rec.get("accepted_incomplete"):
        out.append(f("MAP-SVC-005", "WARN", f"the result is incomplete ({got} of {hits} features) and was accepted: {str(rec['accepted_incomplete'])[:160]}", {"received": got, "expected": hits}))
    elif rec.get("truncated"):
        out.append(f("MAP-SVC-005", "BLOCK", f"the result is incomplete: {got} of {hits} features were received", {"received": got, "expected": hits}))
    elif hits is not None and got is not None and got != hits and not (req.get("max_features") and got >= req["max_features"]):
        out.append(f("MAP-SVC-005", "BLOCK", f"{got} features were received and {hits} were expected", {"received": got, "expected": hits}))
    elif hits is None and rec.get("route") == "mcp":
        out.append(f("MAP-SVC-005", "WARN", "the service gave no count, so completeness is not proven; the result is limited to max_features"))
    if rec.get("server_limit"):
        out.append(f("MAP-SVC-005", "INFO", f"the server returns at most {rec['server_limit']} features per request; every page was fetched", rec["server_limit"]))
    if (rec.get("duplicates") or 0) > 0:
        out.append(f("MAP-SVC-006", "WARN", f"{rec['duplicates']} feature id(s) appeared on more than one page: paging order was not stable", rec["duplicates"]))
    if len(rec.get("pages") or []) > 1 and not req.get("sort_by") and rec.get("protocol") != "ogcapi":
        out.append(f("MAP-SVC-006", "WARN", "several pages were fetched without an ordering field"))
    cc = rec.get("crs_check") or {}
    if cc.get("ok") is False:
        out.append(f("MAP-SVC-007", "BLOCK", cc.get("reason") or "coordinates do not fall where the layer says", cc.get("bbox")))
    retried = [c for c in rec.get("calls") or [] if c.get("status") in (429, 500, 502, 503, 504)]
    if retried:
        out.append(f("MAP-SVC-010", "WARN", f"{len(retried)} request(s) were answered 429 or 5xx and retried with backoff"))
    if not license_known(rec):
        sev = "BLOCK" if (audience or "") in ("external", "regulatory", "any") else "WARN"
        out.append(f("MAP-SVC-012", sev, "the service declares no license, terms or copyright, so its data cannot be published externally until you record the terms" + ("" if sev == "BLOCK" else " (fine for internal use)")))
    bad = find_secrets(json.dumps(rec, default=str))
    if bad:
        out.append(f("MAP-SVC-022", "BLOCK", "the record contains " + ", ".join(bad) + "; secrets are never stored"))
    if rec.get("route") == "user-run":
        if rec.get("attested") != "user":
            out.append(f("MAP-SVC-024", "BLOCK", "user-run data must be marked attested: user"))
        if rec.get("verification") in (None, "none"):
            out.append(f("MAP-SVC-021", "WARN", "no manifest came with the data, so it is recorded as verification: none"))
    if rec.get("route") == "device-run":
        run = rec.get("run") or {}
        if rec.get("attested") != "plugin":
            out.append(f("MAP-SVC-028", "BLOCK", "device-run data must be marked attested: plugin (the plugin started the run on the person's computer)"))
        missing = [k for k in ("approval_id", "approved_at", "command_hash") if not run.get(k)]
        if missing:
            out.append(f("MAP-SVC-028", "BLOCK", "a device-run capture must record the approval it ran under; missing " + ", ".join(missing)))
        if rec.get("host") != "withheld":
            out.append(f("MAP-LIC-009", "BLOCK", "the host of a client network service is withheld from records; host must be 'withheld'"))
        if hits is None and rec.get("verification") != "manifest":
            out.append(f("MAP-SVC-021", "WARN", "no manifest checked this device-run result"))
        elif hits is None:
            out.append(f("MAP-SVC-005", "WARN", "the single request carried no count, so completeness is not proven (a kit run counts first and pages through everything)"))
    auth = rec.get("auth")
    named = isinstance(auth, dict) and set(auth) == {"credential", "kind", "host"} and all(isinstance(v, str) for v in auth.values())
    if auth not in (None, "none", "basic (credentials redacted)") and not named:
        out.append(f("MAP-SVC-009", "BLOCK", "the record's auth field may say only 'none', 'basic (credentials redacted)' or {credential, kind, host} for a registered credential; "
                                             "credentials are never recorded"))
    if named and rec.get("route") != "mcp":
        out.append(f("MAP-SVC-009", "BLOCK", "a registered credential is used by the MCP server on your computer (route mcp)"))
    if named and rec.get("host") not in (auth["host"], None, "withheld"):
        out.append(f("MAP-CRED-005", "BLOCK", "the record's host is not the credential's registered host"))
    if auth == "basic (credentials redacted)" and rec.get("route") != "device-run":
        out.append(f("MAP-SVC-009", "BLOCK", "authenticated data is captured only by an approved run on the person's computer (route device-run)"))
    if auth not in (None, "none") and rec.get("mode") == "live":
        out.append(f("MAP-SVC-017", "BLOCK", "an authenticated capture is capture-only; a live layer cannot carry credentials"))
    if rec.get("mode") == "live":
        out.append(f("MAP-SVC-014", "WARN", "a live layer is not reproducible: the page shows whatever the service answers at the time"))
    return out


def lint_plan(plan) -> list[dict]:
    """MAP-SVC findings for the service and user-run layers of a plan: the capture record is judged for the map's audience (default external)."""
    out: list[dict] = []
    audience = plan.spec.get("audience") or "external"
    for l in plan.layers:
        rec = getattr(l, "capture", None)
        if not rec:
            continue
        for x in lint_capture(rec, None, audience):
            if x["severity"] == "INFO":
                continue
            out.append({**x, "message": f"layer '{l.id}': {x['message']}", "layer": l.id})
        if rec.get("route") == "device-run" and l.confidentiality == "public" and rec.get("confidential"):
            out.append(f("MAP-SVC-024", "WARN", f"layer '{l.id}': the data came from a client network service and is recorded as confidential, but the layer is marked public; check you may publish it"))
        if rec.get("route") == "user-run" and l.confidentiality == "public" and rec.get("confidential"):
            out.append(f("MAP-SVC-024", "WARN", f"layer '{l.id}': the data was supplied by the user and recorded as confidential, but the layer is marked public; check you may publish it"))
    return out


def lint_url(url: str) -> list[dict]:
    import ogc_http as oh
    out = []
    try:
        oh.check_read_only(url)
    except oh.Refusal as e:
        out.append(f("MAP-SVC-002", "BLOCK", str(e)))
    for label in find_secrets(url):
        out.append(f("MAP-SVC-022", "BLOCK", f"the address contains {label}"))
    return out


# ---------------------------------------------------------------------------------------------------------------- M13: one-shot addresses
def lint_oneshot(host: str, confirmed: bool) -> list[dict]:
    """MAP-SVC-033: a host reached from a pasted address is used only after the person confirmed it, once per session."""
    if confirmed:
        return []
    return [f("MAP-SVC-033", "BLOCK", f"the host {host} has not been confirmed in this session: ask the person \"query {host}?\" and call again with confirm_host=\"{host}\"", host)]


def unknown_kind(url_shown: str) -> list[dict]:
    return [f("MAP-SVC-034", "BLOCK", "the address does not say what kind of service it is (an ArcGIS REST /rest/services path, a WFS or WMS endpoint, or an OGC API /collections path); "
                                    "save a profile with the type and base address instead", url_shown)]


# ---------------------------------------------------------------------------------------------------------------- M13: geocoding privacy and terms
PURPOSES = ("sites", "venues", "businesses")
PERSON_COLUMN = re.compile(r"^(first|last|given|family|sur|middle|maiden)[ _.-]?name$|^(fname|lname|firstname|lastname|surname|forename|givenname|familyname)$"
                           r"|^(resident|tenant|occupant|homeowner|patient|household)([ _.-]?name)?$|^(dob|date[ _.-]?of[ _.-]?birth|birth[ _.-]?date|ssn|social[ _.-]?security)$", re.I)
NAME_LIKE = re.compile(r"(^|[ _.-])(name|contact|person|customer|subscriber|employee|owner|applicant)($|[ _.-])", re.I)
BUSINESS_LIKE = re.compile(r"(site|venue|store|business|company|corp|org|organi[sz]ation|facility|location|building|branch|office|shop|school|hospital|station|tower|cell|node|"
                           r"property|premise|account|market|vendor|supplier|partner|dealer|retail|outlet|hotel|stadium|arena|center|centre|campus|plant|warehouse|depot|agency|department)", re.I)
STREET_LIKE = re.compile(r"(street|address|addr\b|addr[ _.-]?|\bst\b|road|line[ _.-]?1|address[ _.-]?1|house[ _.-]?(no|num|number)|full[ _.-]?address)", re.I)
NOMINATIM_TERMS = ("Nominatim usage policy (nominatim.org): at most one request a second, a User-Agent that names the application, no bulk use (this plugin stops at 1,000 rows a run; "
                   "use the Census geocoder for larger US tables or your own Nominatim), and the attribution 'Data © OpenStreetMap contributors, ODbL 1.0' on every map made from the result.")
NOMINATIM_MAX_ROWS = 1000
MATCH_RATE_WARN = 0.8


def lint_geocode_columns(columns, address_cols, purpose) -> list[dict]:
    """MAP-SVC-035: the privacy gate (spec 14). A table is geocoded only for sites, venues or businesses, never for private individuals.

    Blocks when no purpose is declared, when a column names a private person (first/last name, resident, date of birth and the like) whatever the
    purpose, and, without a purpose, when a name-like column sits beside a street column (a residential-looking list)."""
    out: list[dict] = []
    cols = [str(c) for c in (columns or [])]
    addr = [str(c) for c in (address_cols or [])]
    purpose_ok = str(purpose or "").strip().lower() in PURPOSES
    persons = [c for c in cols if PERSON_COLUMN.search(c.strip())]
    if persons:
        out.append(f("MAP-SVC-035", "BLOCK", f"column(s) {', '.join(persons)} name a private person; this plugin never geocodes individuals' addresses. "
                                           "Drop those columns (or, for a business contact, rename them) and declare the purpose", persons))
    names = [c for c in cols if NAME_LIKE.search(c) and not BUSINESS_LIKE.search(c)]
    streets = [c for c in addr + cols if STREET_LIKE.search(c)]
    if not purpose_ok:
        if names and streets:
            out.append(f("MAP-SVC-035", "BLOCK", f"the columns look like people's addresses ({', '.join(names[:3])} with {', '.join(dict.fromkeys(streets[:3]))}); geocoding needs "
                                               f"purpose: {' | '.join(PURPOSES)}, which says these are sites, venues or businesses and not private individuals", {"names": names, "streets": streets}))
        else:
            out.append(f("MAP-SVC-035", "BLOCK", f"declare purpose: {' | '.join(PURPOSES)} before geocoding; this plugin geocodes business, venue and site addresses only, never private individuals'",
                         purpose))
    elif names:
        out.append(f("MAP-SVC-035", "INFO", f"column(s) {', '.join(names[:3])} are taken as {purpose} names under purpose: {purpose}", names))
    return out


def lint_geocode_terms(provider: str, terms_acknowledged: bool, rows: int | None = None) -> list[dict]:
    """MAP-SVC-036: Nominatim is a customization beyond the default (the Census geocoder): it needs the person's one acknowledgment (map-services,
    0.23.0; an older caller's terms_acknowledged still counts) and stays within the row cap. The Census geocoder needs nothing."""
    out: list[dict] = []
    if (provider or "").lower() == "nominatim":
        import customizations as cz
        if not cz.acknowledged(terms_acknowledged):
            out.append(f("MAP-SVC-036", "BLOCK", cz.needed("Nominatim geocoding", "use the Census geocoder for a US table") + ". " + NOMINATIM_TERMS, provider))
        if rows is not None and rows > NOMINATIM_MAX_ROWS:
            out.append(f("MAP-SVC-036", "BLOCK", f"{rows} rows is more than Nominatim's fair use allows here (at most {NOMINATIM_MAX_ROWS} a run); use the Census geocoder for a US table or your own Nominatim", rows))
    return out


def lint_geocode_result(report: dict) -> list[dict]:
    """MAP-SVC-037: unmatched rows are said out loud; a match rate under 80 percent means the addresses need work before the map does."""
    out: list[dict] = []
    n, m = int(report.get("rows") or 0), int(report.get("matched") or 0)
    if n and m < n:
        rate = m / n
        sev = "WARN"
        msg = f"{n - m} of {n} rows did not geocode and are left out of the points layer (their ids are in the provenance record)"
        if rate < MATCH_RATE_WARN:
            msg += f"; the match rate is {rate:.0%}, so check the address columns (street, city, state, ZIP in that order) before using the map"
        out.append(f("MAP-SVC-037", sev, msg, {"rows": n, "matched": m, "match_rate": round(rate, 3)}))
    ties = int(report.get("ties") or 0)
    if ties:
        out.append(f("MAP-SVC-037", "WARN", f"{ties} row(s) matched more than one place equally (Tie) and were not placed", ties))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    c = sub.add_parser("capture"); c.add_argument("file"); c.add_argument("--audience")
    u = sub.add_parser("url"); u.add_argument("url")
    a = ap.parse_args(argv)
    fs = lint_capture(json.loads(Path(a.file).read_text()), audience=a.audience) if a.op == "capture" else lint_url(a.url)
    print(json.dumps(fs, indent=2))
    return 3 if any(x["severity"] == "BLOCK" for x in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
