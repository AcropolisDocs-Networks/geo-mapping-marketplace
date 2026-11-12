"""Reference library lint, MAP-LIB-* (spec 12.10). Used at run time (structural subset) and at release (tools/ref_lint.py)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

import yaml

import library_gen as lg

SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}
_PRIVATE = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|localhost|[\w.-]+\.(?:internal|corp|lan|local))\b", re.I)
_SECRET = re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|bearer|authorization)\b\s*[:=]\s*(?!\{key\})\S+")     # {key}: a url_template placeholder (Stadia api_key={key})
_URL = re.compile(r"https?://[^\s\"')>]+", re.I)


def f(fid, sev, entry_id, msg, detail=None):
    return {"id": fid, "severity": sev, "entry": entry_id, "message": msg, "detail": detail}


def _d(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v)
        except ValueError:
            return None
    return None


def load_vocab(lib: Path) -> tuple[dict, dict]:
    tags = yaml.safe_load((lib / "tags.yaml").read_text())
    types = yaml.safe_load((lib / "types.yaml").read_text())
    return tags, types


def resolve_tag(tags: dict, facet: str, value: str) -> str | None:
    fac = tags["facets"].get(facet)
    if not fac:
        return None
    if value in fac["values"]:
        return value
    alias = (fac.get("aliases") or {}).get(str(value).lower())
    if alias:
        return alias
    for pat in fac.get("patterns", []):
        if re.match(pat, str(value)):
            return value
    return None


_ENTRY_CACHE: dict[str, tuple] = {}
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)     # libyaml when the wheel has it: the same safe subset, about ten times faster


def _load_entry(p: Path) -> dict:
    """One entry file, parsed once per process while its size and modification time stay the same (M15: a map build read the whole library
    about fourteen times, some 60 s of an atlas page's 70; an animation draws every page twice more). A copy is returned, so callers may mutate it."""
    import copy
    st = p.stat()
    sig = (st.st_mtime_ns, st.st_size)
    hit = _ENTRY_CACHE.get(str(p))
    if hit is None or hit[0] != sig:
        try:
            d = yaml.load(p.read_text(), Loader=_LOADER) or {}
        except yaml.YAMLError as e:
            d = {"_parse_error": str(e)}
        hit = (sig, d)
        _ENTRY_CACHE[str(p)] = hit
    return copy.deepcopy(hit[1])


def load_entries(entries_dir: Path, scope: str) -> list[dict]:
    out = []
    for p in sorted(entries_dir.rglob("*.yaml")) if entries_dir.is_dir() else []:
        d = _load_entry(p)
        d["_path"] = str(p)
        d["_scope"] = scope
        out.append(d)
    return out


CRED_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CRED_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,63}$")
CRED_FIELDS = {"name", "kind", "class", "class_allowed", "host", "inject", "reject", "probe", "signup_url", "free", "limits", "shape", "note", "lifetime_days"}
_KEY_IN_REQUEST = re.compile(r"(?i)[?&](key|api_key|apikey|token|access_token)=(?!\{key\}(?:&|$))|\$[A-Za-z_{]")


def credential_findings(e: dict, eid: str) -> list[dict]:
    """0.6.0 (credentials spec §10): `requires_key` is replaced by a structured `requires_credential` block under delivery (or provider). The block
    names the credential, its kind and class, the one host it is sent to, how it is sent and how the service says no; the request text carries no
    key and no variable (MAP-CRED-001), and its host is the block's host (MAP-CRED-013)."""
    out: list[dict] = []
    d, pr = e.get("delivery") or {}, e.get("provider") or {}
    for part, blob in (("delivery", d), ("provider", pr)):
        if "requires_key" in blob:
            out.append(f("MAP-LIB-001", "BLOCK", eid, f"{part}.requires_key is replaced by {part}.requires_credential (name, kind, host, inject); see the credentials spec §10"))
    blocks = [(p, b.get("requires_credential")) for p, b in (("delivery", d), ("provider", pr)) if "requires_credential" in b]
    if len(blocks) > 1:
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential is given twice; keep it under delivery"))
    requests = [d.get("request")] + list((d.get("requests") or {}).values())
    for rq in [r for r in requests if r]:
        if _KEY_IN_REQUEST.search(str(rq)):
            out.append(f("MAP-CRED-001", "BLOCK", eid, "the request text carries a key or a variable; name the credential in requires_credential instead (the key is added at send time)"))
    if not blocks:
        return out
    part, b = blocks[0]
    if not isinstance(b, dict):
        return out + [f("MAP-LIB-001", "BLOCK", eid, f"{part}.requires_credential must be a block (name, kind, host, inject)")]
    extra = sorted(set(b) - CRED_FIELDS)
    if extra:
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential has fields this release does not read", extra))
    if not CRED_NAME.match(str(b.get("name") or "")):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.name is lower-case letters, digits, - and _ (the cred:NAME the person registers)"))
    if b.get("kind") not in ("basic", "apikey", "bearer", "userkey"):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.kind is basic, apikey, bearer or userkey"))
    host = str(b.get("host") or "").lower()
    if not CRED_HOST.match(host):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.host is the one host name the credential is sent to"))
    inj = b.get("inject")
    def _place(t):
        if isinstance(t, dict) and set(t) == {"header", "prefix"}:          # 0.14.0: a scheme word before the key (Stadia-Auth, OAuth)
            return (isinstance(t["prefix"], str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,31}", t["prefix"])) and t["prefix"].lower() not in ("bearer", "basic")
                    and _place({"header": t["header"]}))
        return isinstance(t, dict) and len(t) == 1 and (
            (isinstance(t.get("query"), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", t["query"])) or
            (isinstance(t.get("header"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", t["header"]) and t["header"].lower() not in ("host", "cookie", "content-length", "transfer-encoding", "connection")))
    two = isinstance(inj, dict) and set(inj) == {"user", "key"}
    ok_inj = two and _place(inj["user"]) and _place(inj["key"]) and inj["user"] != inj["key"] or (
        isinstance(inj, dict) and (_place(inj) or (len(inj) == 1 and (inj.get("bearer") is True or inj.get("basic") is True or inj.get("path") is True))))
    if not ok_inj:
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.inject is {query: NAME}, {header: NAME}, {header: NAME, prefix: WORD}, {bearer: true}, {basic: true}, "
                                                   "{path: true} or, for kind userkey, {user: {header: NAME}, key: {header: NAME}}"))
    elif isinstance(inj, dict) and inj.get("path"):
        # 0.14.0: the key sits in the address path at {key}; every request and the probe carry that slot once, in the path, and the class is secret
        if b.get("kind") != "apikey":
            out.append(f("MAP-LIB-001", "BLOCK", eid, "a key in the address path ({path: true}) is kind apikey"))
        if "browser" in (b.get("class_allowed") or [b.get("class", "secret")]):
            out.append(f("MAP-CRED-011", "BLOCK", eid, "a key in the address path is always secret class"))
        for u in [str(r) for r in requests if r] + ([str(b["probe"])] if b.get("probe") else []):
            path, _, query = u.partition("?")
            if "{key}" in query:
                out.append(f("MAP-LIB-001", "BLOCK", eid, "the {key} slot belongs in the address path, not the query", u[:120]))
            elif path.count("{key}") != 1:
                out.append(f("MAP-LIB-001", "BLOCK", eid, "with {path: true} every request and the probe carry one {key} slot in the address path", u[:120]))
    elif two != (b.get("kind") == "userkey"):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "a username + key inject goes with kind userkey, and kind userkey needs {user: ..., key: ...}"))
    cls = b.get("class", "secret")
    allowed = b.get("class_allowed") or [cls]
    if cls not in ("secret", "browser") or not set(allowed) <= {"secret", "browser"} or cls not in allowed:
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.class is secret or browser, and class_allowed lists it"))
    if "browser" in allowed and e.get("type") != "basemap-provider":
        out.append(f("MAP-CRED-011", "BLOCK", eid, "only a tile provider's key may be a browser-class key; every other credential is secret"))
    if "lifetime_days" in b and not (isinstance(b["lifetime_days"], int) and 1 <= b["lifetime_days"] <= 3650):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.lifetime_days is the token's lifetime in whole days (1 to 3650), as the service states it"))
    rj = b.get("reject") or {}
    if rj and (not all(isinstance(s, int) and 300 <= s < 600 for s in rj.get("status") or []) or not all(isinstance(p, str) and p.startswith("/") for p in rj.get("redirect_path") or [])
               or set(rj) - {"status", "redirect_path", "body"}):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.reject is {status: [HTTP codes], redirect_path: [/path, ...], body: [text, ...]}"))
    body = rj.get("body")
    if body is not None and (not isinstance(body, list) or not body or not all(isinstance(x, str) and x.strip() for x in body)):
        out.append(f("MAP-LIB-001", "BLOCK", eid, "requires_credential.reject.body is a list of texts the service's answer carries when the key is wrong"))
    for x in body if isinstance(body, list) else []:
        if isinstance(x, str) and len(x) > 2 and x.startswith("/") and x.endswith("/"):
            try:
                re.compile(x[1:-1])
            except re.error as ex:
                out.append(f("MAP-LIB-001", "BLOCK", eid, f"requires_credential.reject.body: /{x[1:-1]}/ is not a regular expression ({ex})"))
    # 1.0.2 (F1): 401 and 403 always mean the key; any other code (FIRMS answers 400 for a wrong key and for a wrong address alike) is counted
    # as a rejection only with a body text that tells the two apart, or one typo in a profile locks a working key
    loose = sorted(s for s in rj.get("status") or [] if isinstance(s, int) and s not in (401, 403))
    if loose and not body:
        out.append(f("MAP-LIB-001", "BLOCK", eid, f"requires_credential.reject.status lists {', '.join(map(str, loose))}, which the service can also answer for a malformed "
                     "request; add reject.body, the text its answer carries when the key is wrong", loose))
    hosts = set()
    for u in [r for r in requests if r] + [b.get("probe"), e.get("url_template")]:
        if u:
            m = re.match(r"https?://([^/:?#]+)", str(u).replace("{", "").replace("}", ""))
            if m:
                hosts.add(m.group(1).lower())
    if b.get("probe") and not str(b["probe"]).startswith("https://"):
        out.append(f("MAP-CRED-005", "BLOCK", eid, "requires_credential.probe must be an https address"))
    if not b.get("signup_url") or (b.get("free") and not str(b.get("limits") or "").strip()):
        # reference library additions spec (2026-11-07) R2 and test A6: say where the key is issued and, for a free tier, its limit in the
        # provider's words with the date it was read. WARN, so entries written before the field existed still pass the gate.
        out.append(f("MAP-LIB-016", "WARN", eid, "a key-gated entry names its signup_url and, when free, the free-tier limits as the provider states them"))
    bad = sorted(h for h in hosts if h != host)
    if host and bad:
        out.append(f("MAP-CRED-013", "BLOCK", eid, f"the request or probe goes to {', '.join(bad)}, but the credential is registered for {host}"))
    return out


def lint_entries(entries: list[dict], lib: Path, plugin_root: Path | None = None, today: dt.date | None = None,
                 release: bool = False, check_index: bool = False) -> list[dict]:
    today = today or dt.date.today()
    tags, types = load_vocab(lib)
    findings: list[dict] = []
    by_id: dict[str, dict] = {}
    for e in entries:
        eid = e.get("id", f"<{Path(e['_path']).name}>")
        if "_parse_error" in e:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, "entry is not valid YAML", e["_parse_error"]))
            continue
        if eid in by_id:
            findings.append(f("MAP-LIB-008", "BLOCK", eid, "duplicate id", [by_id[eid]["_scope"], e["_scope"]]))
        by_id[eid] = e

        # 001 schema
        missing = [k for k in types["core_fields"] if k not in e]
        etype = e.get("type")
        spec = types["types"].get(etype)
        if not spec:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, f"unknown type '{etype}'"))
            continue
        if missing:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, "missing core fields", missing))
        if not re.fullmatch(r"ref\." + re.escape(etype) + r"(\.[a-z0-9-]+)+", str(eid)):
            findings.append(f("MAP-LIB-001", "BLOCK", eid, f"id must look like ref.{etype}.<geo>.<name>"))
        if e.get("status") not in types["statuses"]:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, f"status must be one of {types['statuses']}"))
        if not isinstance(e.get("rev"), int) or e["rev"] < 1:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, "rev must be a positive integer"))
        if spec["family"] == "data" and etype not in ("basemap-provider",):
            miss = [k for k in spec.get("extra_fields", []) if k not in e and k in ("join", "crs", "delivery")]
            if miss:
                findings.append(f("MAP-LIB-001", "BLOCK", eid, f"type {etype} also needs", miss))
        if "delivery" in e and e["delivery"].get("mode") not in types["delivery_modes"]:
            findings.append(f("MAP-LIB-001", "BLOCK", eid, f"delivery.mode must be one of {types['delivery_modes']}"))
        findings += credential_findings(e, eid)

        # 002 tags controlled; 003 required facets
        etags = e.get("tags") or {}
        for facet, vals in etags.items():
            if facet not in tags["facets"]:
                findings.append(f("MAP-LIB-002", "BLOCK", eid, f"unknown facet '{facet}'"))
                continue
            for v in vals if isinstance(vals, list) else [vals]:
                if resolve_tag(tags, facet, str(v)) is None:
                    findings.append(f("MAP-LIB-002", "BLOCK", eid, f"tag '{v}' is not in tags.yaml for facet '{facet}'"))
        for facet in spec["required_facets"]:
            if not etags.get(facet):
                findings.append(f("MAP-LIB-003", "BLOCK", eid, f"required facet '{facet}' missing for type {etype}"))

        # 004 active entries answer both questions with concrete sentences
        if e.get("status") == "active":
            for key in ("use_when", "avoid_when"):
                vals = e.get(key) or []
                if not vals or any(len(str(v).split()) < 4 for v in vals):
                    findings.append(f("MAP-LIB-004", "BLOCK", eid, f"{key} needs at least one concrete sentence"))

        # 005 maintenance block; cadence
        m = e.get("maintenance") or {}
        for k in ("owner", "reviewed_on", "verify_by"):
            if not m.get(k):
                findings.append(f("MAP-LIB-005", "BLOCK", eid, f"maintenance.{k} missing"))
        rv, vb = _d(m.get("reviewed_on")), _d(m.get("verify_by"))
        if rv and vb:
            months = (vb.year - rv.year) * 12 + (vb.month - rv.month)
            if months > spec["cadence_months"]:
                findings.append(f("MAP-LIB-005", "BLOCK", eid, f"verify_by is {months} months after review; {etype} cadence is {spec['cadence_months']}"))
            overdue = (today.year - vb.year) * 12 + (today.month - vb.month)
            if e.get("status") == "active" and today > vb:
                sev = "BLOCK" if (release and overdue > spec["cadence_months"]) else "WARN"
                findings.append(f("MAP-LIB-013", sev, eid, f"review overdue since {vb}"))

        # 006 URLs
        blob = yaml.safe_dump({k: v for k, v in e.items() if not k.startswith("_")})
        urls = _URL.findall(blob)
        if urls and not e.get("url_verified_on"):
            findings.append(f("MAP-LIB-006", "BLOCK", eid, "URL present without url_verified_on", urls[:3]))

        # 007 license consistency
        lic = e.get("license") or {}
        lc = lic.get("class")
        if lc and etags.get("license_class") and lc not in [resolve_tag(tags, "license_class", str(x)) for x in etags["license_class"]]:
            findings.append(f("MAP-LIB-007", "BLOCK", eid, "license.class disagrees with the license_class tag"))
        if lc == "public-domain" and lic.get("redistribute_ok") is False:
            findings.append(f("MAP-LIB-007", "BLOCK", eid, "public-domain entries cannot forbid redistribution"))
        if lc in ("restricted", "licensed") and lic.get("redistribute_ok") is True and e.get("delivery", {}).get("mode") == "bundled":
            findings.append(f("MAP-LIB-007", "BLOCK", eid, "a restricted or licensed entry cannot be bundled"))

        # 009 shipped entries: no brand assets, secrets, internal hostnames
        if e["_scope"] == "shipped":
            if "<svg" in blob.lower():
                findings.append(f("MAP-LIB-009", "BLOCK", eid, "inline SVG (a possible brand asset) in a shipped entry"))
            if _SECRET.search(blob):
                findings.append(f("MAP-LIB-009", "BLOCK", eid, "possible secret in a shipped entry"))
            if _PRIVATE.search(blob):
                findings.append(f("MAP-LIB-009", "BLOCK", eid, "internal hostname or private address in a shipped entry", _PRIVATE.search(blob).group(0)))

        # 115, 116 (1.3.0, R1-REQ): the requests an entry declares beyond GET
        api = (e.get("delivery") or {}).get("api")
        if isinstance(api, dict):                    # a string `api:` (an address, as geoBoundaries has) is a note, not a declaration
            import sys
            _scripts = str(Path(__file__).resolve().parent.parent / "scripts")
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            import declared as dcl
            rc = (e.get("delivery") or {}).get("requires_credential") or {}
            for rule, msg in dcl.lint_api(api, rc.get("host")):
                findings.append(f(rule, "BLOCK", eid, msg))

        # 010 bundled payload hash
        d = e.get("delivery") or {}
        if d.get("mode") == "bundled" and plugin_root is not None:
            p = plugin_root / d.get("path", "")
            if not p.is_file():
                findings.append(f("MAP-LIB-010", "BLOCK", eid, "bundled payload missing", d.get("path")))
            elif hashlib.sha256(p.read_bytes()).hexdigest() != d.get("sha256"):
                findings.append(f("MAP-LIB-010", "BLOCK", eid, "bundled payload hash does not match", d.get("path")))
            elif p.stat().st_size > 25 * 1024 * 1024:
                findings.append(f("MAP-LIB-010", "BLOCK", eid, "bundled payload exceeds the 25 MB budget"))

        # 012 current rev has a changelog line
        cl = m.get("changelog") or []
        if isinstance(e.get("rev"), int) and not any(c.get("rev") == e["rev"] for c in cl if isinstance(c, dict)):
            findings.append(f("MAP-LIB-012", "BLOCK", eid, f"no changelog line for rev {e['rev']}"))

    # 008 link integrity and cycles
    ids = set(by_id)
    for eid, e in by_id.items():
        for key in ("pairs_with", "supersedes", "superseded_by"):
            for t in _as_list(e.get(key)):
                if t not in ids:
                    findings.append(f("MAP-LIB-008", "BLOCK", eid, f"{key} -> '{t}' does not resolve"))
        for t in _as_list(e.get("distinct_from")):
            t = t.get("id") if isinstance(t, dict) else t
            if t not in ids:
                findings.append(f("MAP-LIB-008", "BLOCK", eid, f"distinct_from -> '{t}' does not resolve"))
        req = e.get("requires")
        if e.get("type") == "map-type" and isinstance(req, dict):
            for t in _as_list(req.get("entries")):
                if t not in ids:
                    findings.append(f("MAP-LIB-008", "BLOCK", eid, f"requires -> '{t}' does not resolve"))
    for eid in by_id:
        seen, cur = set(), eid
        while cur in by_id and by_id[cur].get("supersedes"):
            nxt = _as_list(by_id[cur]["supersedes"])[0]
            if nxt in seen or nxt == eid:
                findings.append(f("MAP-LIB-008", "BLOCK", eid, "supersede cycle"))
                break
            seen.add(nxt)
            cur = nxt

    # 014 near-duplicates; 015 coverage
    groups: dict[tuple, list[str]] = {}
    for eid, e in by_id.items():
        t = e.get("tags") or {}
        key = (e.get("type"), tuple(sorted(t.get("level", []))), tuple(sorted(t.get("geography", []))), tuple(sorted(t.get("detail", []))))
        if e.get("type") in ("boundary", "boundary-derived") and key[1]:
            groups.setdefault(key, []).append(eid)
    for key, members in groups.items():
        if len(members) > 1:
            for eid in members:
                if not by_id[eid].get("distinct_from"):
                    findings.append(f("MAP-LIB-014", "WARN", eid, "near-duplicate of " + ", ".join(m for m in members if m != eid) + " without distinct_from"))
    used = {(fa, v) for e in by_id.values() for fa, vs in (e.get("tags") or {}).items() for v in (vs if isinstance(vs, list) else [vs])}
    for eid, e in by_id.items():
        if e.get("type") == "map-type" and e.get("status") != "retired" and not any(p in ids and by_id[p].get("type") != "map-type" for p in _as_list(e.get("pairs_with"))):
            findings.append(f("MAP-LIB-015", "INFO", eid, "map-type has no pairing data entry"))
    findings.append(f("MAP-LIB-015", "INFO", "-", f"{sum(len(x['values']) for x in tags['facets'].values()) - len(used)} tag value(s) are used by no entry (coverage, not an error)"))
    # 011 generated files match the entries
    if check_index:
        ship = [e for e in entries if e.get("_scope") == "shipped" and "_parse_error" not in e]
        for name, render in (("index.yaml", lambda: lg.render_index(ship, lib)), ("CHANGELOG.md", lambda: lg.render_changelog(ship))):
            gp = lib / name
            if not gp.is_file():
                findings.append(f("MAP-LIB-011", "BLOCK", "-", f"{name} is missing; run tools/ref_release.py regen"))
            elif gp.read_text() != render():
                findings.append(f("MAP-LIB-011", "BLOCK", "-", f"{name} does not match the entries; run tools/ref_release.py regen"))
    findings.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["id"], x["entry"]))
    return findings


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]
