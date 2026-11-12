#!/usr/bin/env python3
"""ogc_http: the one safe way this plugin talks to a public map service (spec 8.2 guards, MAP-SVC-001/002/010/013/018/020).

Every request goes through `SafeHTTP`:
  * GET and HEAD, plus the requests a supported system declares (1.3.0, `request`, scripts/declared.py); a request that names a write operation
    (WFS Transaction, ArcGIS addFeatures and so on) is refused before it is sent, and so is any request no system declares (MAP-SVC-002).
  * The host must resolve only to public addresses, at every redirect hop (at most 3); the address that passed the check is the one connected to
    (the URL is rewritten to the address with Host and TLS server name kept), so a second DNS answer cannot change the target.
    Behind an HTTPS proxy (HTTPS_PROXY or https_proxy set, the host not in NO_PROXY) the request is not pinned: the proxy resolves the name and
    an allow-listing proxy resets a connection made to a bare address. The public-address check still runs on every request and every redirect
    hop; the audit record says `pinned: false, reason: proxy` (M12: one rule for every client, not a workaround per module).
  * No credentials in URLs; a 401 or 403 is reported as an authentication failure and never retried.
  * Bytes, time and retries are capped; 429 and 5xx back off and honor Retry-After; compressed bodies are counted after decoding.
  * The body is checked: an HTML page or text served with HTTP 200 where data was expected is an error; XML is parsed with entities and
    network access off, and a document type declaration with an internal subset is refused.
  * Every call is recorded in an audit list (URL with secrets redacted, status, bytes, time, SHA-256 of the body) for the capture record.
Text that comes back from a service (titles, abstracts, error messages) is data. `untrusted()` shortens and cleans it before it is shown.

A credential (0.6.0, credentials spec §6): `auth=` (on the client, or on one request) is a credential resolved by `scripts/credentials.py`. It is
added at send time only, to a request for its own registered host over https on the default port (anything else is refused, MAP-CRED-005); the
URL kept in the audit and in every message never carries it. A credentialed request never follows a redirect: a redirect to the provider's
declared reject page, or a declared reject status (401, 403), is a rejection (MAP-CRED-008, never retried; the credential is marked rejected),
any other redirect is refused. Every message raised while a credential is in use is masked with its exact value.

1.0.2: a status other than 401 and 403 (FIRMS answers 400 for a wrong key and for a wrong address alike) counts as a rejection only when the
entry declares `reject.body` and the answer's first 4 KB match it; otherwise it is an ordinary refusal (MAP-SVC-013) and the credential is left
as it was. A URL shown or recorded never carries the {key} slot either: `redact_url` writes it as ***.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "mcp"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ssrf  # noqa: E402

import httpx  # noqa: E402
from lxml import etree  # noqa: E402

MAX_REDIRECTS = 3
CEILING = {"timeout_s": 120.0, "max_bytes": 200_000_000, "retries": 3, "backoff_cap_s": 30.0}
DEFAULTS = {"timeout_s": 30.0, "max_bytes": 50_000_000, "retries": 2}
READ_REQUESTS = {"getcapabilities", "describefeaturetype", "getfeature", "getmap", "getfeatureinfo", "getlegendgraphic", "getstyles", "gettile", "describelayer", "getpropertyvalue", "liststoredqueries"}
WRITE_REQUESTS = {"transaction", "lockfeature", "getfeaturewithlock", "createstoredquery", "dropstoredquery"}
ARCGIS_WRITE = {"addfeatures", "updatefeatures", "deletefeatures", "applyedits", "append", "createservice", "deleteservice", "addattachment", "updateattachment", "deleteattachments", "uploads", "addtopipeline", "truncate", "calculate", "updatedefinition", "adddefinition", "deletefromdefinition", "refresh", "rebuildcache"}
SECRET_PARAM = re.compile(r"^(key|token|apikey|api_key|api-key|access_token|auth|authorization|password|passwd|pwd|secret|sig|signature|x-api-key|client_secret|sas|code)$", re.I)
PATH_SLOT_SHOWN = re.compile(r"(?i)\{key\}|%7Bkey%7D")       # 1.0.2: the path slot a credential fills at send time


class ServiceError(Exception):
    """A request failed. `rule` is the MAP-SVC rule it belongs to; `auth` marks a 401 or 403 (exit 5, never retried); `rejected` marks a
    registered credential the service refused (MAP-CRED-008)."""

    def __init__(self, message: str, rule: str | None = None, *, auth: bool = False, status: int | None = None, rejected: bool = False,
                 body: str | None = None):
        super().__init__(message)
        self.rule, self.auth, self.status, self.rejected = rule, auth, status, rejected
        self.body = body          # 1.3.1: the start of the answer (masked, untrusted), for a caller that reads why a request was refused


class Refusal(ServiceError):
    """The request was not sent because a rule forbids it."""


def redact_url(url: str) -> str:
    """The URL with secrets removed: user info dropped, secret-looking query values replaced by ***."""
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    if ":" in host:                                                    # an IPv6 address
        host = f"[{host}]"
    if u.port:
        host += f":{u.port}"
    pairs = [(k, "***" if SECRET_PARAM.match(k) else v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)]
    out = urllib.parse.urlunsplit((u.scheme, host, u.path, urllib.parse.urlencode(pairs, safe="*,:/"), ""))
    return PATH_SLOT_SHOWN.sub("***", out)                             # 1.0.2: the {key} slot of a path credential, raw or percent-encoded


def response_record(http, r) -> dict:
    """What a result says about the response it came from (1.0.2): the address as shown (secrets and the {key} slot as ***), its hash and size,
    and the credential that was attached, by name, when one was."""
    rec = {"url": redact_url(r.url), "sha256": r.sha256, "bytes": len(r.content)}
    cred = None
    with_auth = getattr(http, "_auth_for", None)
    if callable(with_auth):
        try:
            cred = with_auth(r.url, None)
        except Exception:  # noqa: BLE001 - a stand-in client in a test, or no credential
            cred = None
    if cred is not None:
        rec["credential"] = getattr(cred, "ref", None) or f"cred:{cred.name}"
    return rec


def untrusted(text, limit: int = 400) -> str:
    """Text from a service, shortened and stripped of control characters. It is data, never instructions (MAP-SVC-015)."""
    if text is None:
        return ""
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def check_read_only(url: str) -> None:
    """MAP-SVC-002: refuse any URL whose operation is not a read."""
    u = urllib.parse.urlsplit(url)
    q = {k.lower(): v for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)}
    req = (q.get("request") or "").strip().lower()
    if req in WRITE_REQUESTS or (req and req not in READ_REQUESTS and (q.get("service") or "").lower() in ("wfs", "wms", "wmts")):
        raise Refusal(f"'{untrusted(req, 40)}' is not a read operation; this plugin only reads", "MAP-SVC-002")
    segs = {s.lower() for s in u.path.split("/") if s}
    bad = segs & ARCGIS_WRITE
    if bad:
        raise Refusal(f"'{sorted(bad)[0]}' is an edit operation; this plugin only reads", "MAP-SVC-002")
    if "transaction" in segs:
        raise Refusal("a Transaction is not a read operation", "MAP-SVC-002")


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    content: bytes
    elapsed_ms: int
    sha256: str
    content_type: str

    @property
    def text(self) -> str:
        enc = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.content_type or "", re.I)
        if m:
            enc = m.group(1)
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


@dataclass
class Audit:
    calls: list = field(default_factory=list)

    def add(self, **kw) -> None:
        self.calls.append(kw)


def proxy_for(url: str, env=None) -> str | None:
    """The proxy the environment sends this URL through, or None: HTTPS_PROXY / https_proxy for https (HTTP_PROXY / http_proxy for http, ALL_PROXY
    for either), unless NO_PROXY / no_proxy names the host. `env` defaults to os.environ (tests pass a dict)."""
    e = os.environ if env is None else env
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "").lower()
    keys = ("HTTPS_PROXY", "https_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy") if scheme == "http" else ()
    val = next((e.get(k) for k in keys + ("ALL_PROXY", "all_proxy") if e.get(k)), None)
    if not val or not u.hostname:
        return None
    no = e.get("NO_PROXY") or e.get("no_proxy") or ""
    if no.strip() == "*":
        return None
    host = u.hostname.lower()
    for item in (x.strip().lower() for x in no.split(",")):
        if not item:
            continue
        item = item.lstrip("*").lstrip(".") if item.startswith(("*.", ".")) else item
        if host == item or host.endswith("." + item):
            return None
    try:                                                           # address ranges in NO_PROXY (10.0.0.0/8) only matter for a literal address
        import ipaddress
        ip = ipaddress.ip_address(host.strip("[]"))
        for item in (x.strip() for x in no.split(",")):
            try:
                if "/" in item and ip in ipaddress.ip_network(item, strict=False):
                    return None
            except ValueError:
                continue
    except ValueError:
        pass
    return val


REDIRECTS = (301, 302, 303, 307, 308)


def check_auth_target(url: str, auth) -> None:
    """MAP-CRED-005: a credential goes only to its registered host, over https, on the default port."""
    u = urllib.parse.urlsplit(url)
    if (u.scheme or "").lower() != "https":
        raise Refusal(f"the credential '{auth.name}' is sent only over https; this address is not https", "MAP-CRED-005")
    if (u.hostname or "").lower() != auth.host:
        raise Refusal(f"the credential '{auth.name}' is registered for {auth.host} and is never sent to another host ({untrusted(u.hostname, 80)})", "MAP-CRED-005")
    if u.port not in (None, 443):
        raise Refusal(f"the credential '{auth.name}' is sent only to the default https port of {auth.host}", "MAP-CRED-005")


class SafeHTTP:
    """`pin=True` (the default) connects to the checked address, except behind a proxy (proxy_for), where the proxy resolves the name; `pin=False`
    never pins. Either way the public-address check runs on every request and redirect hop. `env` replaces os.environ for the proxy rule (tests).
    `auth` is a resolved credential (scripts/credentials.py) added to every request for its own host and to no other."""

    def __init__(self, *, transport=None, resolver=socket.getaddrinfo, sleep=time.sleep, limits: dict | None = None, user_agent: str | None = None,
                 pin: bool = True, clock=time.monotonic, env=None, auth=None):
        lim = dict(DEFAULTS)
        lim.update({k: v for k, v in (limits or {}).items() if v is not None})
        self.limits = {"timeout_s": min(float(lim["timeout_s"]), CEILING["timeout_s"]), "max_bytes": min(int(lim["max_bytes"]), CEILING["max_bytes"]),
                       "retries": min(int(lim["retries"]), CEILING["retries"])}
        self._transport, self._resolver, self._sleep, self._pin, self._clock, self._env = transport, resolver, sleep, pin, clock, env
        self.user_agent = user_agent or default_user_agent()
        self.audit = Audit()
        self._last_pin: tuple[bool, str | None] = (bool(pin), None if pin else "off")
        self.auth = auth

    @property
    def auth_record(self) -> dict | None:
        """What a capture record says about the credential: its name, kind and host, never the value or the file."""
        return self.auth.record() if self.auth is not None else None

    def _auth_for(self, url: str, auth):
        if auth is not None:
            return auth
        if self.auth is not None and (urllib.parse.urlsplit(url).hostname or "").lower() == self.auth.host:
            return self.auth
        return None

    def pin_rule(self, url: str) -> tuple[bool, str | None]:
        """(pinned, reason): pinned to the checked address, or not and why ("proxy": the environment's proxy resolves the name; "off": pin=False)."""
        if not self._pin:
            return False, "off"
        if proxy_for(url, self._env):
            return False, "proxy"
        return True, None

    # ------------------------------------------------------------------------------------------------------------ one request
    def request(self, method: str, url: str, *, system=None, params: dict | None = None, json=None, data=None, files=None, content: bytes | None = None,
                expect: str = "any", headers: dict | None = None, max_bytes: int | None = None, auth=None, confirm_changes: bool = False) -> Response:
        """1.3.0 (R1-REQ): a request a supported system declares (scripts/declared.py): a library entry's `delivery.api`, a protocol's built-in read
        operations, or a built-in service. The method and path must match a declaration and the host must be the system's own, exactly; otherwise
        MAP-SVC-002, before anything is sent. Every guard of `get` holds (public addresses, the checked address connected to, capped bytes and
        time, a credential only to its host). New for these requests: a redirect is reported, not followed; a request that is not idempotent is
        never retried, an idempotent one once on 429 or 503 after Retry-After; one marked `changes: true` is sent only with `confirm_changes`
        (the person asked for it in this run); the system's `limits` (calls_per_minute, sequential) are shared across processes (ratelimit.py).
        The audit records the method, the size and SHA-256 of the body and, for JSON, a redacted copy. A GET with no system is `get`."""
        import declared as dcl
        m = method.upper()
        if system is None:
            if m in ("GET", "HEAD"):
                return self.get(url, params, expect=expect, headers=headers, method=m, max_bytes=max_bytes, auth=auth)
            path = urllib.parse.urlsplit(url).path or "/"
            raise Refusal(f"{m} {untrusted(path, 120)} is not a declared request: no supported system names it", "MAP-SVC-002")
        full = str(httpx.URL(url).copy_merge_params(params or {})) if params else url
        body, ctype, body_json = b"", None, None
        if json is not None:
            import json as _json
            body, ctype, body_json = _json.dumps(json, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), "application/json", json
        elif content is not None:
            body = bytes(content)
        elif data is not None or files is not None:
            req = httpx.Request(m, full, data=data, files=files)
            body, ctype = req.read(), req.headers.get("content-type")
        try:
            decl = dcl.match(system, m, full, body)
        except dcl.Undeclared as ex:
            raise Refusal(str(ex), "MAP-SVC-002")
        if m in ("GET", "HEAD"):
            check_read_only(full)
        if decl.changes and not confirm_changes:
            raise Refusal(f"{m} {urllib.parse.urlsplit(full).path} changes something on {system.name}; it is sent only when the person asks for it in this run", "MAP-SVC-002")
        cred = self._auth_for(full, auth)
        if cred is not None:
            check_auth_target(full, cred)
        hdrs = dict(headers or {})
        if ctype and not any(k.lower() == "content-type" for k in hdrs):
            hdrs["Content-Type"] = ctype
        extra = {"system": system.name}
        if m not in ("GET", "HEAD"):
            extra.update(body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
            if body_json is not None:
                red = repr(dcl.redact_json(body_json))
                extra["body_json"] = cred.mask(red) if cred is not None else red
        retry = (None if m in ("GET", "HEAD") else ((429, 503), 1) if decl.idempotent else ((), 0))
        import ratelimit
        try:
            with ratelimit.slot(system.host, system.limits, sleep=self._sleep) as waited:
                if waited:
                    extra["waited_s"] = round(waited, 2)
                return self._get(full, expect, hdrs, m, max_bytes, None, cred, payload=body, retry=retry, audit_extra=extra)
        except ServiceError as e:
            if cred is not None:
                e.args = (cred.mask(str(e)),)
            raise

    def get(self, url: str, params: dict | None = None, *, expect: str = "any", headers: dict | None = None, method: str = "GET", max_bytes: int | None = None,
            origin: str | None = None, auth=None) -> Response:
        if method not in ("GET", "HEAD"):
            raise Refusal(f"{method} {untrusted(urllib.parse.urlsplit(url).path or '/', 120)} is not a declared request: the plugin sends GET and HEAD, "
                          "and the other requests a supported system declares", "MAP-SVC-002")
        full = str(httpx.URL(url).copy_merge_params(params or {})) if params else url
        check_read_only(full)
        cred = self._auth_for(full, auth)
        if cred is not None:
            check_auth_target(full, cred)
        try:
            return self._get(full, expect, headers, method, max_bytes, origin, cred)
        except ServiceError as e:
            if cred is not None:
                e.args = (cred.mask(str(e)),)
            raise

    def _get(self, full: str, expect: str, headers: dict | None, method: str, max_bytes: int | None, origin: str | None, cred, *, payload: bytes | None = None,
             retry: tuple | None = None, audit_extra: dict | None = None) -> Response:
        """`retry` (1.3.0): None for a GET's usual rule (429 and 5xx, up to the client's retries); else (statuses, times) for a declared request."""
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*", "Accept-Encoding": "gzip"}
        hdrs.update(headers or {})
        if origin:
            hdrs["Origin"] = origin
        cap = min(int(max_bytes or self.limits["max_bytes"]), self.limits["max_bytes"])
        attempt = 0
        while True:
            t0 = self._clock()
            resp, final = self._follow(full, method, hdrs, cap, cred, body=payload)
            ms = int((self._clock() - t0) * 1000)
            status = resp["status"]
            body = resp["content"] if cred is None else cred.mask_bytes(resp["content"])   # a body that echoes the request never carries the key
            pinned, why = self._last_pin
            self.audit.add(url=redact_url(final), method=method, status=status, bytes=len(body), ms=ms, sha256=hashlib.sha256(body).hexdigest(), attempt=attempt + 1,
                           pinned=pinned, **({"reason": why} if why else {}), **({"credential": cred.name} if cred is not None else {}), **(audit_extra or {}))
            if method not in ("GET", "HEAD") and status in REDIRECTS and cred is None:
                raise ServiceError(f"the service answered {status} to a {method} with a redirect to {untrusted(redact_url(resp.get('location') or ''), 120)}; "
                                   "a redirect after a request that is not a GET is reported, not followed", "MAP-SVC-020", status=status)
            if cred is not None and status in REDIRECTS:
                nxt = resp["location"]
                if cred.is_reject_redirect(nxt):
                    cred.rejected(f"redirect to {urllib.parse.urlsplit(nxt).path}", url=redact_url(full))
                    raise ServiceError(f"the service rejected the credential '{cred.name}' (HTTP {status} to its reject page {untrusted(urllib.parse.urlsplit(nxt).path, 80)}); "
                                       f"the run stops here and is not retried. Get a new key and run map-credentials rotate {cred.name}", "MAP-CRED-008",
                                       auth=True, status=status, rejected=True)
                raise Refusal(f"the service answered {status} with a redirect to {untrusted(redact_url(nxt), 120)}; a request that carries the credential '{cred.name}' "
                              "never follows a redirect", "MAP-CRED-005")
            if cred is not None and status in (401, 403) and not cred.is_reject_status(status):
                # 1.3.1: the entry declares which codes reject its key (CDS: 401 only; it answers 403 for a dataset licence not yet accepted):
                # another 401 or 403 is a refusal of this request, and the credential is not marked rejected
                text = body[:1200].decode("utf-8", "replace")
                raise ServiceError(f"the service refused the request (HTTP {status}): {untrusted(text, 240)!r}. The credential '{cred.name}' was not marked "
                                   f"rejected: its entry does not count HTTP {status} as a wrong key", "MAP-SVC-013", auth=True, status=status, body=text)
            if cred is not None and cred.is_reject_status(status):
                body_rule = getattr(cred, "needs_body_match", None)
                if callable(body_rule) and body_rule(status) and not cred.body_rejects(resp["content"]):
                    # 1.0.2 (F1): this code also means a malformed request at this service, and the answer does not say the key is wrong
                    raise ServiceError(f"the service refused the request (HTTP {status}): {untrusted(body[:1200].decode('utf-8', 'replace'), 240)!r}. The credential "
                                       f"'{cred.name}' was not marked rejected: the answer does not say the key is wrong, so check the address, layer and box",
                                       "MAP-SVC-013", status=status)
                cred.rejected(f"HTTP {status}", url=redact_url(full))
                hint = cred.rejection_hint() if callable(getattr(cred, "rejection_hint", None)) else f"Get a new key and run map-credentials rotate {cred.name}"
                raise ServiceError(f"the service rejected the credential '{cred.name}' (HTTP {status}); the run stops here and is not retried. {hint}",
                                   "MAP-CRED-008", auth=True, status=status, rejected=True)
            if status in (401, 403):
                raise ServiceError(f"the service answered {status}: it needs authentication. Not retried; a service that needs a key is used through a "
                                   "registered credential (map-credentials)", "MAP-SVC-018", auth=True, status=status)
            retry_on, retry_n = (((429, 500, 502, 503, 504), self.limits["retries"]) if retry is None else retry)
            if status in (429, 500, 502, 503, 504):
                if status not in retry_on or attempt >= retry_n:
                    raise ServiceError(f"the service answered {status} after {attempt + 1} tr{'y' if attempt == 0 else 'ies'}"
                                       + ("" if retry is None or retry_n else f"; a {method} that is not idempotent is never retried"), "MAP-SVC-010", status=status)
                wait = backoff_seconds(resp["headers"].get("retry-after"), attempt)
                self._sleep(wait)
                attempt += 1
                continue
            if status >= 400:
                raise ServiceError(f"the service answered {status}: {untrusted(body[:300].decode('utf-8', 'replace'), 160)}", "MAP-SVC-013", status=status)
            r = Response(final, status, resp["headers"], body, ms, hashlib.sha256(body).hexdigest(), resp["headers"].get("content-type", ""))
            if method != "HEAD":
                validate_body(r, expect)
            return r

    def _follow(self, url: str, method: str, headers: dict, cap: int, cred=None, *, body: bytes | None = None):
        for hop in range(MAX_REDIRECTS + 1):
            try:
                addrs = ssrf.public_addresses(url, self._resolver)
            except ssrf.Refused as ex:
                raise Refusal(f"{ex}", "MAP-SVC-020")
            u = httpx.URL(url)
            req_url, req_headers, ext = url, dict(headers), {}
            if cred is not None:                                  # added at send time only; `url` (audited, shown) never carries it
                req_url, req_headers = cred.apply(req_url, req_headers)
            pinned, why = self.pin_rule(url)                      # behind a proxy the name goes to the proxy; the check above still ran
            self._last_pin = (pinned, why)
            if pinned:
                ip = addrs[0]
                host_for_url = f"[{ip}]" if ":" in ip else ip
                req_headers["Host"] = u.host + (f":{u.port}" if u.port else "")
                req_url = str(httpx.URL(req_url).copy_with(host=host_for_url))
                if u.scheme == "https":
                    ext["sni_hostname"] = u.host
            try:
                with httpx.Client(transport=self._transport, timeout=self.limits["timeout_s"], follow_redirects=False, verify=True) as c:
                    with c.stream(method, req_url, headers=req_headers, extensions=ext, **({"content": body} if body else {})) as r:
                        status, rh = r.status_code, {k.lower(): v for k, v in r.headers.items()}
                        buf = bytearray()
                        if method != "HEAD":
                            for chunk in r.iter_bytes():
                                buf += chunk
                                if len(buf) > cap:
                                    raise ServiceError(f"the response is larger than the {cap // 1_000_000} MB limit (counted after decompression)", "MAP-SVC-013")
            except httpx.TimeoutException:
                raise ServiceError(f"the service did not answer within {self.limits['timeout_s']:.0f} seconds", "MAP-SVC-010")
            except httpx.HTTPError as ex:
                raise ServiceError(f"the request failed: {type(ex).__name__}", "MAP-SVC-010")
            if status in REDIRECTS:
                loc = rh.get("location")
                if not loc:
                    raise ServiceError("a redirect had no destination", "MAP-SVC-020")
                nxt = urllib.parse.urljoin(url, loc)
                if cred is not None or method not in ("GET", "HEAD"):   # never followed with a credential or after a POST: the caller decides
                    return {"status": status, "headers": rh, "content": bytes(buf), "location": nxt}, url
                if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(nxt).scheme != "https":
                    raise Refusal("a redirect from https to http is refused", "MAP-SVC-020")
                check_read_only(nxt)
                url = nxt
                continue
            return {"status": status, "headers": rh, "content": bytes(buf)}, url
        raise Refusal(f"more than {MAX_REDIRECTS} redirects", "MAP-SVC-020")


# ---------------------------------------------------------------------------------------------------------------- helpers
def default_user_agent() -> str:
    try:
        import gmap_common as gc
        m = gc.build_manifest()
        return f"{m['plugin_name']}/{m['version']} (read-only map data client)"
    except Exception:  # noqa: BLE001
        return "geo-mapping/0 (read-only map data client)"


def backoff_seconds(retry_after: str | None, attempt: int) -> float:
    cap = CEILING["backoff_cap_s"]
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), cap))
        except ValueError:
            pass
    return min(0.5 * (2 ** attempt), cap)


HTML_START = re.compile(rb"^\s*(<!doctype\s+html|<html|<head|<body)", re.I)


def sniff_html(body: bytes, ctype: str) -> bool:
    return "text/html" in (ctype or "").lower() or bool(HTML_START.match(body[:512].lstrip(b"\xef\xbb\xbf")))


def validate_body(r: Response, expect: str) -> None:
    """MAP-SVC-013: the body must be what was asked for. An HTML page served with HTTP 200 is an error page."""
    body, ct = r.content, (r.content_type or "").lower()
    if expect == "any":
        return
    if expect in ("xml", "json", "text") and sniff_html(body, ct):
        raise ServiceError(f"the service returned a web page instead of data (HTTP {r.status}): {untrusted(re.sub(rb'<[^>]+>', b' ', body[:2000]).decode('utf-8', 'replace'), 200)}", "MAP-SVC-013")
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if expect == "xml" and head != b"<":
        raise ServiceError(f"expected XML, got content type '{untrusted(ct, 60)}'", "MAP-SVC-013")
    if expect == "json" and head not in (b"{", b"["):
        raise ServiceError(f"expected JSON, got content type '{untrusted(ct, 60)}': {untrusted(body[:200].decode('utf-8', 'replace'), 120)}", "MAP-SVC-013")
    if expect == "image":
        magic = body[:8]
        if not (magic.startswith(b"\x89PNG") or magic[:3] == b"\xff\xd8\xff" or magic[:4] == b"GIF8" or magic[:4] == b"RIFF"):
            if head == b"<":
                raise ServiceError(f"the service returned a message instead of an image: {untrusted(body[:600].decode('utf-8', 'replace'), 200)}", "MAP-SVC-013")
            raise ServiceError(f"expected an image, got content type '{untrusted(ct, 60)}'", "MAP-SVC-013")
    if expect == "tile" and sniff_html(body, ct):
        raise ServiceError("the tile request returned a web page", "MAP-SVC-013")


DOCTYPE = re.compile(rb"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.I | re.S)


def safe_xml(content: bytes):
    """Parse XML with entities and network off. A DOCTYPE with an internal subset, or any ENTITY declaration, is refused;
    a plain DOCTYPE that only names a DTD (WMS 1.1.1 capabilities) is removed before parsing."""
    if re.search(rb"<!ENTITY", content, re.I):
        raise ServiceError("the XML declares entities, which are not accepted", "MAP-SVC-013")
    m = DOCTYPE.search(content[:4096])
    if m:
        if m.group(1):
            raise ServiceError("the XML has an internal document type subset, which is not accepted", "MAP-SVC-013")
        content = content[: m.start()] + content[m.end():]
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False, remove_comments=True, remove_pis=True)
    try:
        return etree.fromstring(content, parser)
    except etree.XMLSyntaxError as ex:
        raise ServiceError(f"the XML could not be read: {untrusted(str(ex), 120)}", "MAP-SVC-013")


# ---------------------------------------------------------------------------------------------------------------- 1.3.0: long read queries by POST
MAX_GET_URL = 2000          # longer GET addresses are refused by some servers and proxies; a read query that long goes by POST (R1-REQ)


def read_query(http, url: str, params: dict | None, *, protocol: str, expect: str = "any", force_post: bool = False) -> Response:
    """A read query (WFS GetFeature, ArcGIS `query`, Overpass `interpreter`) as a GET, or, when its address would be longer than MAX_GET_URL
    (a long filter or `where` clause) or `force_post`, as the protocol's built-in POST read operation (scripts/declared.py). The POST carries the
    same query: WFS as a GetFeature XML document, ArcGIS and Overpass as form fields. Transactions are never sent either way."""
    import declared as dcl
    full = str(httpx.URL(url).copy_merge_params(params or {})) if params else url
    if len(full) <= MAX_GET_URL and not force_post:
        return http.get(url, params, expect=expect)
    if not hasattr(http, "request"):                      # a stand-in client in a test
        return http.get(url, params, expect=expect)
    system = dcl.protocol_system(protocol, url)
    if protocol == "wfs":
        body, rest = wfs_post_body(params or {})
        return http.request("POST", url, system=system, params=rest or None, content=body.encode("utf-8"),
                            headers={"Content-Type": "application/xml; charset=utf-8"}, expect=expect)
    return http.request("POST", url, system=system, data={k: str(v) for k, v in (params or {}).items()}, expect=expect)


def wfs_post_body(params: dict) -> tuple[str, dict]:
    """(a GetFeature XML document, the vendor parameters kept on the address): the KVP of `ogc_client.wfs_params` as WFS POST XML. CQL_FILTER is
    GeoServer's own parameter and has no XML form, so it stays on the address."""
    from xml.sax.saxutils import escape, quoteattr
    p = {str(k): v for k, v in params.items()}
    low = {k.lower(): k for k in p}
    g = lambda k: p.get(low.get(k.lower(), k))
    ver = str(g("version") or "2.0.0")
    v2 = ver.startswith("2")
    wns = "http://www.opengis.net/wfs/2.0" if v2 else "http://www.opengis.net/wfs"
    attrs = {"service": "WFS", "version": ver}
    if g("outputFormat"):
        attrs["outputFormat"] = str(g("outputFormat"))
    if g("resultType"):
        attrs["resultType"] = str(g("resultType"))
    if g("count") is not None:
        attrs["count"] = str(g("count"))
    if g("maxFeatures") is not None:
        attrs["maxFeatures"] = str(g("maxFeatures"))
    if g("startIndex") is not None:
        attrs["startIndex"] = str(g("startIndex"))
    qattrs = {("typeNames" if v2 else "typeName"): str(g("typeNames") or g("typeName") or "")}
    if g("srsName"):
        qattrs["srsName"] = str(g("srsName"))
    inner = []
    for prop in [x for x in str(g("propertyName") or "").split(",") if x]:
        inner.append(f"<wfs:PropertyName>{escape(prop)}</wfs:PropertyName>")
    flt = g("FILTER")
    if not flt and g("bbox"):
        import ogc_client as oc
        parts = str(g("bbox")).split(",")
        flt = oc.filter_xml([], ver, [float(x) for x in parts[:4]], parts[4] if len(parts) > 4 else "EPSG:4326")
    if flt:
        inner.append(str(flt))
    if g("sortBy"):
        fes = "fes" if v2 else "ogc"
        fns = "http://www.opengis.net/fes/2.0" if v2 else "http://www.opengis.net/ogc"
        ref = "ValueReference" if v2 else "PropertyName"
        items = "".join(f"<{fes}:SortProperty><{fes}:{ref}>{escape(s.split()[0])}</{fes}:{ref}>"
                        + (f"<{fes}:SortOrder>{'DESC' if s.upper().endswith(('D', 'DESC')) and ' ' in s else 'ASC'}</{fes}:SortOrder>")
                        + f"</{fes}:SortProperty>" for s in str(g("sortBy")).split(",") if s.strip())
        inner.append(f'<{fes}:SortBy xmlns:{fes}="{fns}">{items}</{fes}:SortBy>')
    a = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    q = " ".join(f"{k}={quoteattr(v)}" for k, v in qattrs.items())
    doc = f'<?xml version="1.0" encoding="UTF-8"?><wfs:GetFeature xmlns:wfs="{wns}" {a}><wfs:Query {q}>{"".join(inner)}</wfs:Query></wfs:GetFeature>'
    known = {"service", "version", "request", "outputformat", "resulttype", "count", "maxfeatures", "startindex", "typenames", "typename", "srsname",
             "propertyname", "filter", "bbox", "sortby"}
    rest = {k: v for k, v in p.items() if k.lower() not in known}
    return doc, rest
