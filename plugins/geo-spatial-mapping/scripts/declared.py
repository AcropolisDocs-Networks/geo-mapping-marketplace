#!/usr/bin/env python3
"""declared: the requests this plugin may send beyond GET and HEAD, system by system (1.3.0, R1-REQ; owner's decision of 2026-11-10).

The plugin sends the requests a **supported system's** API needs. A system is supported when one of these declares the request:

1. A library entry's `delivery.api` block: every request the entry allows, by method and path pattern.

       delivery:
         api:
           host: cds.climate.copernicus.eu
           requests:
             - {method: GET,  path: "/api/retrieve/v1/jobs/{id}"}
             - {method: POST, path: "/api/retrieve/v1/processes/{dataset}/execution", body: json, changes: false}
             - {method: POST, path: "/api/retrieve/v1/jobs/delete", body: json, changes: true}      # sent only when the person asks, that run
           limits: {sequential: true, calls_per_minute: 30}
           download_hosts: [object-store.example.org]                                            # where results may be fetched (GET only)

2. A protocol's built-in read operations, for a registered service profile (geo-service-query). Fixed here and read-only:
   WFS 2.0 GetFeature by POST, OGC API Features and STAC `/search` by POST, ArcGIS REST `query` by POST, Overpass `interpreter` by POST.
   Transactions (WFS-T, ArcGIS applyEdits and the other edit operations) are never sent.

3. The built-in services that POST: openrouteservice isochrones and the Census batch geocoder.

Patterns are anchored; `{name}` matches one path segment and `**` any number of them. The host is the system's own, exactly: no wildcard
hosts. A request that matches no declaration is refused before it is sent: MAP-SVC-002 "POST /x is not a declared request for SYSTEM".
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
METHODS = ("GET", "HEAD") + WRITE_METHODS
BODIES = ("json", "form", "multipart", "xml", "text", "none")


class Undeclared(Exception):
    """Raised by `match`; ogc_http turns it into a Refusal with rule MAP-SVC-002."""


@dataclass
class Declared:
    method: str
    path: str
    body: str = "none"
    idempotent: bool = False
    changes: bool = False
    note: str | None = None
    guard: object = None                     # a function(body bytes) -> None that raises Undeclared (the protocol read operations)

    def __post_init__(self):
        self.method = self.method.upper()
        self.regex = compile_path(self.path)
        if self.method in ("GET", "HEAD"):
            self.idempotent = True


@dataclass
class System:
    name: str
    host: str
    requests: list = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    download_hosts: list = field(default_factory=list)
    kind: str = "entry"                      # entry | protocol | builtin

    def declared(self, method: str, path: str) -> Declared | None:
        m = method.upper()
        for d in self.requests:
            if d.method == m and d.regex.fullmatch(path):
                return d
        return None


def compile_path(pattern: str) -> re.Pattern:
    """An anchored path pattern: `{name}` one segment, `**` one or more segments, everything else literal."""
    p = str(pattern)
    if not p.startswith("/"):
        raise ValueError(f"a declared path starts with '/': {pattern!r}")
    out, i = [], 0
    while i < len(p):
        if p.startswith("**", i):
            out.append(r"[^/]+(?:/[^/]+)*")
            i += 2
        elif p[i] == "{":
            j = p.index("}", i)
            out.append(r"[^/]+")
            i = j + 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out))


def _problem(rule: str, severity: str, message: str) -> tuple[str, str]:
    return rule, message


def lint_api(api: dict, entry_host: str | None = None) -> list[tuple[str, str]]:
    """(rule, message) problems of one `delivery.api` block: MAP-LIB-115 (host, anchoring, wildcards), MAP-LIB-116 (`changes:` unset)."""
    out = []
    host = str((api or {}).get("host") or "")
    if not host or "*" in host or "/" in host or ":" in host:
        out.append(_problem("MAP-LIB-115", "BLOCK", f"delivery.api.host must be one exact host name, not {host!r}"))
    if entry_host and host and host.lower() != str(entry_host).lower():
        out.append(_problem("MAP-LIB-115", "BLOCK", f"delivery.api.host {host} is not the entry's host {entry_host}"))
    for dh in (api or {}).get("download_hosts") or []:
        if "*" in str(dh) or "/" in str(dh):
            out.append(_problem("MAP-LIB-115", "BLOCK", f"a download host must be an exact host name, not {dh!r}"))
    for r in (api or {}).get("requests") or []:
        m, path = str(r.get("method") or "").upper(), str(r.get("path") or "")
        if m not in METHODS:
            out.append(_problem("MAP-LIB-115", "BLOCK", f"{m or '(no method)'} {path}: the method is not one of {', '.join(METHODS)}"))
        if r.get("host") and str(r["host"]).lower() != host.lower():
            out.append(_problem("MAP-LIB-115", "BLOCK", f"{m} {path}: a request names host {r['host']}, not the entry's {host}"))
        if not path.startswith("/") or "://" in path or path.startswith("//"):
            out.append(_problem("MAP-LIB-115", "BLOCK", f"{m} {path}: the pattern must be an anchored path starting with '/'"))
        if r.get("body") and r["body"] not in BODIES:
            out.append(_problem("MAP-LIB-115", "BLOCK", f"{m} {path}: body must be one of {', '.join(BODIES)}"))
        if m in WRITE_METHODS and not isinstance(r.get("changes"), bool):
            out.append(_problem("MAP-LIB-116", "BLOCK", f"{m} {path}: say whether it changes something on the service (changes: true or false)"))
    return out


def from_entry(entry: dict) -> System | None:
    api = ((entry or {}).get("delivery") or {}).get("api")
    if not isinstance(api, dict) or not api.get("host"):
        return None
    reqs = [Declared(method=r["method"], path=r["path"], body=r.get("body", "none"), idempotent=bool(r.get("idempotent")), changes=bool(r.get("changes")),
                     note=r.get("note")) for r in api.get("requests") or []]
    return System(name=entry["id"], host=str(api["host"]).lower(), requests=reqs, limits=dict(api.get("limits") or {}),
                  download_hosts=[str(h).lower() for h in api.get("download_hosts") or []], kind="entry")


# ---------------------------------------------------------------------------------------------------------------- protocol read operations
def _wfs_guard(body: bytes) -> None:
    b = body or b""
    if not re.search(rb"<(?:\w+:)?GetFeature\b", b) or re.search(rb"<(?:\w+:)?(?:Transaction|LockFeature|GetFeatureWithLock|CreateStoredQuery|DropStoredQuery)\b", b):
        raise Undeclared("a WFS POST must be a GetFeature request; Transactions and locks are never sent")


def _overpass_guard(body: bytes) -> None:
    # Overpass is read-only by design; the guard only keeps the body a query (form field data=...)
    if not re.match(rb"^\s*(?:data=|\[out:|\()", body or b""):
        raise Undeclared("an Overpass POST carries one query in the 'data' field")


PROTOCOLS = ("wfs", "ogcapi", "stac", "arcgis", "overpass")


def protocol_system(protocol: str, base_url: str, name: str | None = None, limits: dict | None = None) -> System:
    """The fixed, read-only POST operations of a registered service profile's protocol, at that profile's host and path."""
    proto = str(protocol).lower().replace("ogcapi-features", "ogcapi").replace("arcgis-rest", "arcgis").replace("esri", "arcgis")
    u = urllib.parse.urlsplit(base_url)
    base = (u.path or "/").rstrip("/")
    if proto == "wfs":
        reqs = [Declared("POST", base or "/", body="xml", idempotent=True, changes=False, guard=_wfs_guard, note="WFS 2.0 GetFeature by POST")]
    elif proto in ("ogcapi", "stac"):
        reqs = [Declared("POST", (base or "") + "/search", body="json", idempotent=True, changes=False, note="search by POST"),
                Declared("POST", (base or "") + "/collections/{c}/items", body="json", idempotent=True, changes=False, note="items with a filter by POST")] \
            if proto == "stac" else [Declared("POST", (base or "") + "/search", body="json", idempotent=True, changes=False, note="search by POST")]
    elif proto == "arcgis":
        root = base.split("/rest/services")[0] + "/rest/services" if "/rest/services" in base else base
        reqs = [Declared("POST", root + "/**/MapServer/{layer}/query", body="form", idempotent=True, changes=False, note="ArcGIS REST query by POST"),
                Declared("POST", root + "/**/FeatureServer/{layer}/query", body="form", idempotent=True, changes=False, note="ArcGIS REST query by POST")]
    elif proto == "overpass":
        root = base[: -len("/interpreter")] if base.endswith("/interpreter") else base
        reqs = [Declared("POST", root + "/interpreter", body="form", idempotent=True, changes=False, guard=_overpass_guard, note="Overpass interpreter by POST")]
    else:
        raise ValueError(f"no built-in POST read operations for protocol {protocol!r}; the choices are {', '.join(PROTOCOLS)}")
    return System(name=name or f"{proto}:{u.hostname}", host=(u.hostname or "").lower(), requests=reqs, limits=dict(limits or {}), kind="protocol")


# ---------------------------------------------------------------------------------------------------------------- built-in services
BUILTIN = {
    "openrouteservice": System("openrouteservice", "api.openrouteservice.org",
                               [Declared("POST", "/v2/isochrones/{profile}", body="json", idempotent=True, changes=False, note="isochrones"),
                                Declared("POST", "/v2/isochrones/{profile}/geojson", body="json", idempotent=True, changes=False, note="isochrones")],
                               kind="builtin"),
    "census-geocoder": System("census-geocoder", "geocoding.geo.census.gov",
                              [Declared("POST", "/geocoder/locations/addressbatch", body="multipart", idempotent=True, changes=False, note="batch geocoder")],
                              kind="builtin"),
}


def match(system: System, method: str, url: str, body: bytes | None = None) -> Declared:
    """The declaration a request matches, or Undeclared. Checks the method and path, the exact host, and a protocol's body guard."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    path = u.path or "/"
    m = method.upper()
    if (u.scheme or "").lower() != "https" and system.kind != "protocol":
        raise Undeclared(f"{m} {path}: {system.name} is reached over https only")
    if host != system.host:
        raise Undeclared(f"{m} {path} is not a declared request for {system.name}: the host {host} is not {system.host}")
    d = system.declared(m, path)
    if d is None:
        raise Undeclared(f"{m} {path} is not a declared request for {system.name}")
    if d.guard is not None:
        d.guard(body or b"")
    return d


def redact_json(obj, secret_key=re.compile(r"(?i)^(key|token|apikey|api_key|access_token|password|secret|private[-_]?token|authorization|hash_value)$")):
    """A copy of a JSON body for the audit: values under secret-looking keys replaced by ***."""
    if isinstance(obj, dict):
        return {k: ("***" if secret_key.match(str(k)) else redact_json(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(x) for x in obj]
    return obj
