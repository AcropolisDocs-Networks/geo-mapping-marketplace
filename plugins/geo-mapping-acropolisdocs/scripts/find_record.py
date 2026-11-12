#!/usr/bin/env python3
"""find_record: validate a "find record", the structured answer of a data search that found nothing in the library (spec 9.3.7).

    find_record.py check FILE.json|FILE.yaml     validate; prints findings; exit 0 clean, 3 BLOCK findings, 2 usage
    find_record.py template                      print an empty record to fill in

A find record names ONE dataset the search actually found: what it is, who publishes it, which vintage, under which license
class, in which format, at what resolution or scale, and where to get it. Rules (all BLOCK unless noted):

  FIND-001 required field missing or empty               FIND-002 a URL is not https, or has no host
  FIND-003 license class is not in the controlled vocabulary (tags.yaml)
  FIND-004 confidence is not high | medium | low         FIND-005 vintage is not a year, a year-month or an ISO date
  FIND-006 the record names a lookalike: the title says one product, the publisher or URL host belongs to another (WARN)
  FIND-007 the URL carries a key, token, password or user name   FIND-008 confidence is low but no caveat is written (WARN)

A record never substitutes a lookalike: if the search found a different product, the record says so under `instead_of`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

REQUIRED = ("dataset", "publisher", "vintage", "license_class", "format", "resolution", "route", "url", "confidence")
CONFIDENCE = ("high", "medium", "low")
ROUTES = ("download", "api", "service", "request", "purchase")
SECRET_QUERY = re.compile(r"(^|_|-)(key|token|apikey|api_key|secret|password|passwd|pwd|auth|sig|signature|access_token)$", re.I)
VINTAGE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$|^\d{4}Q[1-4]$|^\d{4}-\d{4}$")


def template() -> dict:
    return {"dataset": "", "publisher": "", "vintage": "", "license_class": "", "format": "", "resolution": "", "route": "download",
            "url": "", "confidence": "medium", "caveat": "", "instead_of": ""}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def license_classes() -> list[str]:
    try:
        import ref_library as rl
        import library_lint as ll
        tags, _ = ll.load_vocab(rl.shipped_lib())
        return list(tags["facets"]["license_class"]["values"].keys())
    except Exception:  # noqa: BLE001
        return ["public-domain", "open", "attribution", "share-alike", "restricted", "licensed"]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def check(rec: dict, classes: list[str] | None = None) -> list[dict]:
    classes = classes or license_classes()
    out: list[dict] = []
    if not isinstance(rec, dict):
        return [finding("FIND-001", "BLOCK", "a find record is a mapping of fields")]
    for k in REQUIRED:
        v = rec.get(k)
        if v is None or str(v).strip() == "":
            out.append(finding("FIND-001", "BLOCK", f"'{k}' is missing; write what the search found or leave the record out", k))
    if rec.get("route") and rec["route"] not in ROUTES:
        out.append(finding("FIND-001", "BLOCK", f"route '{rec['route']}' is not one of {', '.join(ROUTES)}", rec["route"]))

    urls = [("url", rec.get("url"))] + [(f"alt_urls[{i}]", u) for i, u in enumerate(rec.get("alt_urls") or [])]
    for name, u in urls:
        if not u:
            continue
        p = urllib.parse.urlparse(str(u))
        if p.scheme != "https" or not p.netloc:
            out.append(finding("FIND-002", "BLOCK", f"{name} must be a full https address ({u})", u))
            continue
        if p.username or p.password:
            out.append(finding("FIND-007", "BLOCK", f"{name} carries a user name or password; remove it", p.netloc))
        for qk, _ in urllib.parse.parse_qsl(p.query):
            if SECRET_QUERY.search(qk):
                out.append(finding("FIND-007", "BLOCK", f"{name} carries a '{qk}' parameter; records never hold keys or tokens", qk))

    lc = rec.get("license_class")
    if lc and lc not in classes:
        out.append(finding("FIND-003", "BLOCK", f"license_class '{lc}' is not one of {', '.join(classes)}; if the terms are unclear, write 'restricted' and say why in the caveat", lc))

    if rec.get("confidence") and rec["confidence"] not in CONFIDENCE:
        out.append(finding("FIND-004", "BLOCK", f"confidence '{rec['confidence']}' must be high, medium or low", rec["confidence"]))
    elif rec.get("confidence") == "low" and not str(rec.get("caveat") or "").strip():
        out.append(finding("FIND-008", "WARN", "confidence is low; say in the caveat what is uncertain"))

    if rec.get("vintage") and not VINTAGE.match(str(rec["vintage"]).strip()):
        out.append(finding("FIND-005", "BLOCK", f"vintage '{rec['vintage']}' must be a year (2025), a year and month (2025-06), a date, a quarter (2025Q3) or a range (2019-2023)", rec["vintage"]))

    # lookalike: the page that was found must be the publisher's own, or the record must say who hosts it
    host = urllib.parse.urlparse(str(rec.get("url") or "")).netloc.lower()
    pub = _words(rec.get("publisher", ""))
    if host and pub and not rec.get("mirror_of") and not (pub & _words(host.replace(".", " "))) and not host.endswith((".gov", ".edu", ".int")):
        out.append(finding("FIND-006", "WARN", f"the address is on {host}, which does not look like the publisher '{rec.get('publisher')}'; if it is a mirror, "
                           "say so in `mirror_of`, otherwise find the publisher's own page", host))
    if rec.get("instead_of") and not str(rec.get("caveat") or "").strip():
        out.append(finding("FIND-006", "WARN", "the record names a different product than the one asked for (instead_of) but writes no caveat saying how they differ"))
    return out


def load(path: Path) -> dict:
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["check", "template"])
    ap.add_argument("file", nargs="?")
    a = ap.parse_args(argv)
    if a.op == "template":
        print(json.dumps(template(), indent=2))
        return gc.EXIT_OK
    if not a.file:
        print("find_record: check needs a file", file=sys.stderr)
        return gc.EXIT_USAGE
    try:
        rec = load(Path(a.file))
    except Exception as ex:  # noqa: BLE001
        print(f"find_record: cannot read {a.file}: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    fs = check(rec)
    for f in fs:
        print(f"{f['severity']:5} {f['id']} {f['message']}")
    if not fs:
        print("find record is complete")
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in fs) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
