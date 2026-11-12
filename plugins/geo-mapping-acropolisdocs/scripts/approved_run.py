#!/usr/bin/env python3
"""approved_run: run a read-only request on the person's computer after they approve it (spec 6.7, G.4; M4d, unauthenticated servers only).

The plugin never executes a pasted string and never holds a credential. This script is the policy and the record-keeping; the shell on the
person's computer is reached by the session's device tool, which runs the one command this script prints after approval.

    approved_run.py probe-command --host H [--port 443] [--tls yes|no]     the command that checks DNS, TCP and TLS from the device shell
    approved_run.py probe-check   --run ID --output FILE|-                 read what it printed; record the precondition (MAP-SVC-031)
    approved_run.py plan-curl     --curl-file F | --curl TEXT --device-dir D [--show-host]     a pasted curl, as text, rebuilt as an argument list
    approved_run.py plan-kit      --kit-dir LOCAL --device-kit-dir D --device-dir D [--show-host]   a kit built by request_kit.py
    approved_run.py approve       --run ID --hash H [--standing-days N]    record the person's approval, bound to the command's hash
    approved_run.py command       --run ID                                  the exact command to run on the device (refused without approval)
    approved_run.py finish        --run ID --stdout FILE --folder LOCAL --name N [--profile P]   scrub, check, verify and ingest what came back
    approved_run.py scrub         [--file F]                                remove credential patterns and control characters from text
    approved_run.py list | revoke --approval ID | show --run ID

Rules: only GET and HEAD; no redirect is followed and one that points off the approved host fails the run (MAP-SVC-032); loopback, link-local and
cloud-metadata addresses are always refused, by name, by number and by what the name resolves to; credentials of any kind are not run in this release
(MAP-SVC-029); shell syntax and file-reading options in a pasted command are refused (MAP-SVC-027); each command is bound to its hash and a change needs
a new approval; output is scrubbed before anyone reads it (MAP-SVC-030). The host is shown on the run card in the conversation and is withheld from
every file this script writes (MAP-LIC-009).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import credential_broker as cb  # noqa: E402
import redact  # noqa: E402
import request_kit as rk  # noqa: E402
import service_registry as sr  # noqa: E402

MAX_TIME_S = 120                    # one curl; the device command cap is about 150 s (H.3 D6)
CHUNK_S = 150
RETRY_MAX = 2
MAX_BYTES = 100 * 1024 * 1024      # a single transfer above this is unproven (H.3 D8)
PROBE_FRESH_S = 2 * 3600
PENDING_TTL_S = 24 * 3600
STANDING_MAX_DAYS = 90
UA = "geo-mapping-approved-run/1 (read-only)"
SAFE_HEADERS = {"accept", "content-type", "user-agent"}
METADATA_NAMES = {"metadata.google.internal", "metadata", "instance-data", "instance-data.ec2.internal", "metadata.tencentyun.com", "metadata.azure.com"}
METADATA_IPS = {"169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254"}
INTERNAL_SUFFIXES = (".corp", ".internal", ".intranet", ".lan", ".local", ".home", ".localdomain", ".private")
DEVICE_PATH = re.compile(r"^(?:\$HOME|~)?/[A-Za-z0-9._/@+=-]+$|^\$HOME/[A-Za-z0-9._/@+=-]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

# curl options and what happens to each in a pasted command
NOOP = {"-s", "--silent", "-S", "--show-error", "--compressed", "-g", "--globoff", "--http1.1", "--http2", "-4", "-6", "--fail", "-f", "--no-keepalive", "--no-progress-meter"}
GET_FLAGS = {"-G", "--get"}
TAKES_ARG_DROP = {"-o", "--output", "--output-dir", "-D", "--dump-header", "-w", "--write-out", "--trace", "--trace-ascii", "--stderr", "-c", "--cookie-jar", "--create-dirs",
                  "-x", "--proxy", "-e", "--referer", "-A", "--user-agent", "--connect-timeout", "--resolve", "--interface", "--dns-servers", "--limit-rate", "-m", "--max-time", "--retry",
                  "--retry-delay", "--retry-max-time", "--max-redirs", "--proto", "--tlsv1.2", "--tls-max"}
DROP_NOARG = {"-k", "--insecure", "-L", "--location", "-v", "--verbose", "-i", "--include", "-O", "--remote-name", "-J", "--remote-header-name", "-N", "--no-buffer", "--path-as-is", "-#"}
REFUSE = {"-K": "an include file (-K)", "--config": "an include file (--config)", "-T": "an upload (-T)", "--upload-file": "an upload (--upload-file)", "-F": "a form post (-F)", "--form": "a form post (--form)",
          "--form-string": "a form post", "-d": "a request body (-d)", "--data": "a request body (--data)", "--data-raw": "a request body", "--data-binary": "a request body (--data-binary)",
          "--json": "a request body (--json)", "--next": "several requests chained with --next", "-Z": "parallel requests (-Z)", "--parallel": "parallel requests (--parallel)",
          "--unix-socket": "a unix socket", "--abstract-unix-socket": "a unix socket", "--connect-to": "a connection redirected to another host (--connect-to)",
          "--proxy-header": "a proxy header", "--preproxy": "a proxy", "-:": "several requests chained with --next"}
CRED = {"-u": "--user", "--user": "--user", "--proxy-user": "--proxy-user", "-U": "--proxy-user", "--netrc": "--netrc", "-n": "--netrc", "--netrc-file": "--netrc-file", "--netrc-optional": "--netrc",
        "--cert": "--cert", "-E": "--cert", "--key": "--key", "--oauth2-bearer": "--oauth2-bearer", "-b": "--cookie", "--cookie": "--cookie", "--aws-sigv4": "--aws-sigv4", "--digest": "--digest",
        "--ntlm": "--ntlm", "--negotiate": "--negotiate", "--basic": "--basic", "--anyauth": "--anyauth", "--delegation": "--delegation", "--pass": "--pass", "--tlsuser": "--tlsuser", "--tlspassword": "--tlspassword"}
ARG_OPTS = set(TAKES_ARG_DROP) | {"-H", "--header", "--url", "--cacert", "--capath", "-X", "--request", "--data-urlencode", "-I"} | {k for k in REFUSE if k not in ("--next", "-Z", "--parallel", "-:")} \
    | {k for k in CRED if k in ("-u", "--user", "--proxy-user", "-U", "--netrc-file", "--cert", "-E", "--key", "--oauth2-bearer", "-b", "--cookie", "--aws-sigv4", "--pass", "--tlsuser", "--tlspassword", "--delegation")}
ARG_OPTS -= {"-I", "-:"}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def blocked(fs) -> bool:
    return any(f["severity"] == "BLOCK" for f in fs)


def now() -> dt.datetime:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return dt.datetime.fromisoformat(fixed + "T12:00:00+00:00") if fixed else dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================================================================ host guard
def _ip_of(host: str):
    """An address from a name that is one: dotted, decimal, hex, octal and IPv4-mapped forms are all read as the address they are."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)(\.(0x[0-9a-fA-F]+|\d+)){0,3}", h):
        try:
            return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(h)))
        except OSError:
            return None
    return None


def ip_class(ip) -> str:
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if str(ip) in METADATA_IPS:
        return "metadata"
    if ip.is_loopback or ip.is_unspecified:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast or ip.is_reserved:
        return "reserved"
    if ip.is_private or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")):
        return "private"
    return "public"


def host_class(host: str) -> str:
    """public | private | internal | loopback | link-local | metadata | reserved | invalid"""
    h = (host or "").strip().lower().rstrip(".")
    if not h or not HOST_RE.match(h.strip("[]")) and ":" not in h:
        return "invalid"
    ip = _ip_of(h)
    if ip is not None:
        return ip_class(ip)
    if h in METADATA_NAMES or h == "localhost" or h.endswith(".localhost"):
        return "metadata" if h in METADATA_NAMES else "loopback"
    if "." not in h or h.endswith(INTERNAL_SUFFIXES):
        return "internal"
    return "public"


def refuse_host(host: str) -> str | None:
    c = host_class(host)
    if c in ("loopback", "link-local", "metadata", "reserved", "invalid"):
        return {"loopback": "a loopback address (the device itself)", "link-local": "a link-local address", "metadata": "a cloud metadata address",
                "reserved": "a reserved address", "invalid": "not a valid host name"}[c]
    return None


def host_id(host: str) -> str:
    return hashlib.sha256(("geo-mapping-host:" + host.lower()).encode()).hexdigest()[:12]


# ============================================================================================================================ pasted text
def shell_syntax(text: str) -> list[str]:
    """What a real shell would do with this text beyond running curl: command substitution, chaining, pipes, redirection, background. Quote-aware."""
    t = rk._clean_continuations(text.strip())
    found: list[str] = []
    q = None
    i = 0
    while i < len(t):
        c = t[i]
        nxt = t[i + 1] if i + 1 < len(t) else ""
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
            elif c == "`":
                found.append("command substitution (`)")
            elif c == "$" and nxt in "({":
                found.append("command or variable substitution ($" + nxt + ")")
        else:
            if c == "\\":
                i += 1
            elif c in "'\"":
                q = c
            elif c == "`":
                found.append("command substitution (`)")
            elif c == "$" and nxt in "({":
                found.append("command or variable substitution ($" + nxt + ")")
            elif c == ";":
                found.append("a second command (;)")
            elif c == "|":
                found.append("a pipe (|)")
            elif c == "&":
                found.append("chaining or background (&): quote the address")
            elif c in "<>":
                found.append("redirection (" + c + ")")
            elif c == "\n":
                found.append("a second line")
            elif c == "$" and (nxt.isalpha() or nxt == "_"):
                found.append("a variable ($NAME)")
        i += 1
    return sorted(set(found))


# ============================================================================================================================ plans
def store() -> Path:
    d = gc.workdir(True) / "approved_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
        raise ValueError("not a run id")
    return store() / run_id


def canonical_hash(argv: list[str], out_dir: str, extra: dict | None = None) -> str:
    return hashlib.sha256(json.dumps({"v": 1, "argv": argv, "out": out_dir, "extra": extra or {}}, sort_keys=True).encode()).hexdigest()


def _device_path(p: str, what: str) -> str:
    p = p.rstrip("/")
    if not DEVICE_PATH.match(p) or ".." in p.split("/"):
        raise ValueError(f"{what} '{p}' is not a plain path on the device (letters, digits and . _ - / only; start with $HOME/ or /)")
    return p


def shape_of(filter_text: str | None) -> str:
    """The filter with every literal replaced, so a standing approval covers the same query with other values but not another kind of query."""
    s = re.sub(r"'[^']*'", "'?'", filter_text or "")
    s = re.sub(r"\b\d+(\.\d+)?\b", "?", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _hashed_extra(priv: dict) -> dict | None:
    """The extra part of the hash with the command text taken from what will actually be issued, so a changed command fails the check."""
    ex = dict(priv.get("extra") or {})
    if "cmd" in ex:
        ex["cmd"] = priv.get("command")
    return ex or None


def _save(run_id: str, plan: dict, private: dict) -> Path:
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    pp = d / "command.private.json"
    pp.write_text(json.dumps(private, indent=2))
    try:
        os.chmod(pp, 0o600)
    except OSError:
        pass
    return d


def load(run_id: str) -> tuple[dict, dict]:
    d = run_dir(run_id)
    if not (d / "plan.json").is_file():
        raise ValueError(f"no run {run_id}; plan one first")
    return json.loads((d / "plan.json").read_text()), json.loads((d / "command.private.json").read_text())


def parse_options(text: str) -> tuple[dict, list[dict]]:
    """Walk a pasted curl as tokens. Returns {url, headers, cacert, dropped, kept} and findings; nothing is run."""
    fs: list[dict] = []
    t = rk._clean_continuations(text.strip())
    try:
        toks = shlex.split(t, posix=True)
    except ValueError:
        return {}, [finding("MAP-SVC-027", "BLOCK", "the quotes in the pasted command do not match, so it cannot be read safely; fix the quoting")]
    if not toks or toks[0].lower().rsplit("/", 1)[-1] not in ("curl", "curl.exe"):
        if len(toks) == 1 and re.match(r"^https?://", toks[0], re.I):
            toks = ["curl", toks[0]]
        else:
            return {}, [finding("MAP-SVC-027", "BLOCK", "only a curl command or an address can be approved; this is neither")]
    out = {"urls": [], "headers": [], "cacert": None, "dropped": [], "kept": [], "get": False, "method": None, "data_urlencode": [], "max_time": None, "retry": 0}
    i = 1
    cred: list[str] = []
    refused: list[str] = []
    while i < len(toks):
        tk = toks[i]
        i += 1
        if not tk.startswith("-") or tk == "-":
            if re.match(r"^https?://", tk, re.I):
                out["urls"].append(tk)
            else:
                out["dropped"].append("stray argument")
            continue
        name, eq, val = tk.partition("=") if tk.startswith("--") else (tk, "", "")
        if not tk.startswith("--") and len(tk) > 2 and tk[:2] in ARG_OPTS | {"-X", "-H", "-d", "-u", "-o", "-w", "-m", "-A", "-e", "-x", "-b", "-c", "-D", "-T", "-F", "-K", "-E", "-U"}:
            name, val, eq = tk[:2], tk[2:], "="                      # -XGET, -H'..' written together
        if not tk.startswith("--") and len(tk) > 2 and not eq:       # combined letters such as -sSkL
            for ch in tk[1:]:
                f = "-" + ch
                if f in CRED:
                    cred.append(CRED[f])
                elif f in REFUSE:
                    refused.append(REFUSE[f])
                elif f in GET_FLAGS:
                    out["get"] = True
                elif f in DROP_NOARG:
                    out["dropped"].append(f)
                elif f not in NOOP and f not in ("-I",):
                    out["dropped"].append(f)
                if f in ARG_OPTS:
                    break
            continue
        flag = name
        arg = val if eq else None
        if flag in ARG_OPTS and arg is None:
            arg = toks[i] if i < len(toks) else ""
            i += 1
        if flag in CRED:
            cred.append(CRED[flag])
        elif flag in REFUSE:
            refused.append(REFUSE[flag])
        elif flag in ("-X", "--request"):
            out["method"] = (arg or "").upper()
        elif flag in ("-I", "--head"):
            out["method"] = out["method"] or "HEAD"
        elif flag in GET_FLAGS:
            out["get"] = True
        elif flag == "--url":
            out["urls"].append(arg or "")
        elif flag in ("-H", "--header"):
            out["headers"].append(arg or "")
        elif flag == "--cacert":
            out["cacert"] = arg
        elif flag == "--data-urlencode":
            if (arg or "").startswith("@") or re.match(r"^[^=@]*@", arg or ""):
                refused.append("a file read into the request (--data-urlencode @file)")
            else:
                out["data_urlencode"].append(arg or "")
        elif flag in ("-m", "--max-time"):
            try:
                out["max_time"] = float(arg)
            except (TypeError, ValueError):
                pass
        elif flag == "--retry":
            try:
                out["retry"] = int(arg)
            except (TypeError, ValueError):
                pass
        elif flag in NOOP:
            out["kept"].append(flag)
        elif flag in TAKES_ARG_DROP or flag in DROP_NOARG:
            out["dropped"].append(flag)                                  # the name only: an argument may be an address or a secret
        elif flag in ("--capath",):
            out["dropped"].append(flag)
        else:
            out["dropped"].append(flag)
    for what in sorted(set(refused)):
        fs.append(finding("MAP-SVC-027", "BLOCK", f"the command carries {what}, which is never run: it can read files, send data or run other requests. Rewrite it as one GET address"))
    for what in sorted(set(cred)):
        fs.append(finding("MAP-SVC-029", "BLOCK", f"the command uses {what}, which is a login typed into the command. It is never run or kept. Give a reference to a secrets file instead (--credentials netrc:PATH or file:PATH), or use a user-run kit"))
    return out, fs


def cred_id(cred: dict | None) -> str | None:
    """A hash of the reference, so a standing approval is tied to one secrets file without the path being kept in the approval."""
    return hashlib.sha256(("geo-mapping-cred:" + cred["kind"] + ":" + cred["path"]).encode()).hexdigest()[:12] if cred else None


def resolve_credentials(credentials: str | None, profile_name: str | None, host: str, url: str, out_dir: str) -> tuple[dict | None, list[dict]]:
    """A reference from --credentials or from a saved profile. A profile's credentials go only to that profile's own host (MAP-SVC-029); https only (MAP-SVC-016)."""
    if not credentials and not profile_name:
        return None, []
    ref = credentials
    if profile_name:
        try:
            prof = sr.load(profile_name)
        except FileNotFoundError as ex:
            return None, [finding("MAP-SVC-001", "BLOCK", str(ex))]
        if (prof.get("auth") or {}).get("method") != "basic":
            return None, [finding("MAP-SVC-019", "BLOCK", f"profile '{profile_name}' does not use credentials (auth: {(prof.get('auth') or {}).get('method')})")]
        if (sr.host_of(prof) or "").lower() != host.lower():
            return None, [finding("MAP-SVC-029", "BLOCK", f"profile '{profile_name}' holds credentials for another host; they are never sent to a different one")]
        refs = (prof["auth"].get("credentials") or {})
        ref = credentials or refs.get("netrc") or refs.get("file")
    cred, fs = cb.validate_ref(ref or "", out_dir=out_dir, endpoint=url)
    return cred, fs


def plan_curl(text: str, device_dir: str, *, show_host: bool = False, run_id: str | None = None, credentials: str | None = None, profile_name: str | None = None) -> dict:
    """A pasted curl, read as text and rebuilt as an argument list from an allowlist. Returns {ok, run_id, command_hash, card, findings}."""
    fs: list[dict] = []
    if len(text) > 20_000:
        return {"ok": False, "findings": [finding("MAP-SVC-023", "BLOCK", "that is too long to be one request")]}
    _, removed = redact.scrub_command(text)
    if removed:
        fs.append(finding("MAP-SVC-029", "BLOCK", "the pasted command carries " + ", ".join(removed) + "; it is a login typed into the command, so it is not run and nothing of it is kept. Rewrite it with a reference to a secrets file (--credentials netrc:PATH or file:PATH), or use a user-run kit"))
    syn = shell_syntax(text)
    if syn:
        fs.append(finding("MAP-SVC-027", "BLOCK", "the pasted text contains shell syntax: " + ", ".join(syn) + ". It is never executed as typed; paste one curl command with the address in quotes"))
    if blocked(fs):
        return {"ok": False, "findings": fs}
    opts, ofs = parse_options(text)
    fs += ofs
    if blocked(fs):
        return {"ok": False, "findings": fs}
    urls = [u for u in opts["urls"] if re.match(r"^https?://", u, re.I)]
    if not urls:
        return {"ok": False, "findings": fs + [finding("MAP-SVC-003", "BLOCK", "no http or https address found in that command")]}
    if len(urls) > 1:
        fs.append(finding("MAP-SVC-026", "WARN", f"{len(urls)} addresses were found; only the first is run"))
    url = urls[0]
    try:
        sp = urllib.parse.urlsplit(url)
        sp.port                                                          # noqa: B018  (raises on a bad port)
    except ValueError:
        return {"ok": False, "findings": fs + [finding("MAP-SVC-032", "BLOCK", "that address cannot be read as a host and port (an IPv6 address needs brackets); approved runs never go to a host they cannot name")]}
    if sp.username or sp.password or "@" in (sp.netloc or ""):
        return {"ok": False, "findings": fs + [finding("MAP-SVC-029", "BLOCK", "the address carries a user name or password, which is a login; it is not run and not kept. Use a reference to a secrets file (--credentials netrc:PATH or file:PATH)")]}
    host = sp.hostname or ""
    why = refuse_host(host)
    if why:
        return {"ok": False, "findings": fs + [finding("MAP-SVC-032", "BLOCK", f"the host is {why}; approved runs never go there")]}
    method = (opts["method"] or "GET").upper()
    if method not in ("GET", "HEAD"):
        return {"ok": False, "findings": fs + [finding("MAP-SVC-023", "BLOCK", f"a {method} request is not a read; only GET and HEAD are run")]}
    try:
        oh.check_read_only(url)
    except oh.Refusal as ex:
        return {"ok": False, "findings": fs + [finding(ex.rule or "MAP-SVC-002", "BLOCK", str(ex))]}
    if redact.find(url):
        return {"ok": False, "findings": fs + [finding("MAP-SVC-029", "BLOCK", "the address carries " + ", ".join(redact.find(url)) + "; it is a credential, so it is not run and not kept")]}
    if sp.scheme.lower() == "http":
        fs.append(finding("MAP-SVC-031", "WARN", "the address is plain http: the request and the answer cross the network unencrypted"))
    try:
        out_dir = _device_path(device_dir, "the device folder")
    except ValueError as ex:
        return {"ok": False, "findings": fs + [finding("MAP-SVC-027", "BLOCK", str(ex))]}
    cred, cfs = resolve_credentials(credentials, profile_name, host, url, out_dir)
    fs += cfs
    if (credentials or profile_name) and cred is None:
        return {"ok": False, "findings": fs}
    headers = []
    for h in opts["headers"]:
        nm, _, val = h.partition(":")
        if nm.strip().lower() in SAFE_HEADERS and re.fullmatch(r"[\x20-\x7e]{1,200}", val.strip()) and nm.strip().lower() != "user-agent":
            headers.append(f"{nm.strip()}: {val.strip()}")
        else:
            opts["dropped"].append("header " + re.sub(r"[^A-Za-z0-9-]", "", nm.strip())[:30])
    cacert = None
    if opts["cacert"]:
        try:
            cacert = _device_path(opts["cacert"], "the CA file")
        except ValueError as ex:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-027", "BLOCK", str(ex))]}
    for d in opts["dropped"]:
        if d.split()[0] in ("-k", "--insecure"):
            fs.append(finding("MAP-SVC-026", "WARN", "dropped -k: certificate checking stays on. For a private certificate authority give --cacert with a file on the device"))
        elif d.split()[0] in ("-L", "--location"):
            fs.append(finding("MAP-SVC-032", "INFO", "dropped -L: redirects are not followed; a redirect fails the run"))
    other = sorted({d.split()[0] for d in opts["dropped"] if d.split()[0] not in ("-k", "--insecure", "-L", "--location")})
    if other:
        fs.append(finding("MAP-SVC-026", "INFO", "not carried over: " + ", ".join(other[:8])))
    rid = run_id or "run-" + hashlib.sha256((url + iso(now()) + os.urandom(4).hex()).encode()).hexdigest()[:8]
    max_time = int(min(max(opts["max_time"] or 60, 5), MAX_TIME_S))
    retry = int(min(max(opts["retry"], 0), RETRY_MAX))
    out_file = f"{out_dir}/{rid}-out/response.bin"
    argv = ["curl", "--silent", "--show-error", "--max-time", str(max_time), "--max-redirs", "0", "--proto", "=http,https", "--user-agent", UA]
    if retry:
        argv += ["--retry", str(retry)]
    prefix = ""
    if cred:
        extra_args, prefix = cb.curl_pieces(cred)
        argv += extra_args
    if cacert:
        argv += ["--cacert", cacert]
    for h in headers:
        argv += ["--header", h]
    if method == "HEAD":
        argv.append("--head")
    if opts["data_urlencode"]:
        argv.append("--get")
        for d in opts["data_urlencode"]:
            argv += ["--data-urlencode", d]
    argv += ["--output", out_file, "--write-out", '\n{"http_code":%{http_code},"size_download":%{size_download},"redirect_url":"%{redirect_url}","content_type":"%{content_type}","time_total":%{time_total}}\n', "--url", url]
    read = rk.understand(url)
    cmd = f"mkdir -p {shlex.quote(os.path.dirname(out_file))} && {prefix}{shlex.join(argv)}"
    extra = {"cmd": cmd, **({"cred": cred} if cred else {})}                  # the hash covers the exact text that will be issued
    chash = canonical_hash(argv, os.path.dirname(out_file), extra)
    plan = {"run_id": rid, "kind": "curl", "status": "planned", "created_at": iso(now()), "command_hash": chash, "host_id": host_id(host), "host_class": host_class(host), "tls": sp.scheme.lower() == "https",
            "port": sp.port or (443 if sp.scheme.lower() == "https" else 80), "operation": f"{(read['protocol'] or 'service').upper()} {read['operation'] or 'read'}", "layer": read["layer"], "protocol": read["protocol"],
            "filter_shape": shape_of(read.get("cql_filter")), "method": method, "out_dir": os.path.dirname(out_file), "max_time_s": max_time, "dropped": opts["dropped"][:20], "probe": None, "approval": None,
            "expected": None, "profile": profile_name, "auth": "basic" if cred else "none", "credentials": {"kind": cred["kind"], "checked": None} if cred else None, "cred_id": cred_id(cred)}
    _save(rid, plan, {"argv": argv, "command": cmd, "host": host, "out_file": out_file, "extra": extra, "credential": cred})
    fs.append(finding("MAP-SVC-031", "INFO", "probe the host from the device shell first (probe-command), then ask the person to approve the card"))
    return {"ok": True, "run_id": rid, "command_hash": chash, "card": run_card(plan, argv, host if (show_host or cred) else None, opts["dropped"], read, cred=cred), "findings": fs,
            "probe_command": probe_command(host, plan["port"], plan["tls"], cacert)["command"]}


def plan_kit(kit_dir, device_kit_dir: str, device_dir: str, *, show_host: bool = False, run_id: str | None = None, credentials: str | None = None, profile_name: str | None = None, cacert: str | None = None) -> dict:
    """A kit built by request_kit.py: runs `python3 kit.py OUT` on the device, with the kit file's hash checked there first."""
    fs: list[dict] = []
    kd = Path(kit_dir)
    kp, rp = kd / "kit.py", kd / "kit-request.json"
    if not kp.is_file() or not rp.is_file():
        return {"ok": False, "findings": [finding("MAP-SVC-031", "BLOCK", "the folder holds no kit.py and kit-request.json; build a Python kit with request_kit.py build --format py")]}
    req = json.loads(rp.read_text())
    ep = req.get("endpoint") or ""
    try:
        sp = urllib.parse.urlsplit(ep)
        sp.port                                                          # noqa: B018
        host = sp.hostname or ""
    except ValueError:
        return {"ok": False, "findings": [finding("MAP-SVC-032", "BLOCK", "the kit's address cannot be read as a host and port")]}
    why = refuse_host(host)
    if why:
        return {"ok": False, "findings": [finding("MAP-SVC-032", "BLOCK", f"the host is {why}; approved runs never go there")]}
    if sp.username or sp.password:
        return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "the kit's address carries a login; it is not run. Use a reference to a secrets file (--credentials netrc:PATH or file:PATH)")]}
    try:
        kdir = _device_path(device_kit_dir, "the kit folder on the device")
        ddir = _device_path(device_dir, "the device folder")
    except ValueError as ex:
        return {"ok": False, "findings": [finding("MAP-SVC-027", "BLOCK", str(ex))]}
    kit_cacert = None
    if cacert:
        try:
            kit_cacert = _device_path(cacert, "the CA file")
        except ValueError as ex:
            return {"ok": False, "findings": [finding("MAP-SVC-027", "BLOCK", str(ex))]}
    cred, cfs = resolve_credentials(credentials, profile_name, host, ep, ddir)
    fs += cfs
    if (credentials or profile_name) and cred is None:
        return {"ok": False, "findings": fs}
    rid = run_id or "run-" + hashlib.sha256((req.get("kit_id", "") + iso(now()) + os.urandom(4).hex()).encode()).hexdigest()[:8]
    out_dir = f"{ddir}/{rid}-out"
    ksha = hashlib.sha256(kp.read_bytes()).hexdigest()
    q_out = shlex.quote(out_dir)
    script = (f"cd {shlex.quote(kdir)} && echo '{ksha}  kit.py' | sha256sum -c --quiet - && {cb.kit_env_prefix(cred) if cred else ''}{'export GEO_CACERT=' + shlex.quote(kit_cacert) + ' && ' if kit_cacert else ''}GEO_MAX_SECONDS={CHUNK_S - 20} timeout {CHUNK_S} python3 kit.py {q_out}; rc=$?; "
              f"if [ $rc -eq 0 ] || [ $rc -eq 3 ]; then tar -czf {q_out}.tgz -C {q_out} . ; fi; exit $rc")
    argv = ["bash", "-c", script]
    cmd = f"mkdir -p {shlex.quote(out_dir)} && {shlex.join(argv)}"
    extra = {"cmd": cmd, "kit_sha256": ksha, **({"cred": cred} if cred else {})}
    chash = canonical_hash(argv, out_dir, extra)
    if sp.scheme.lower() == "http":
        fs.append(finding("MAP-SVC-031", "WARN", "the service address is plain http: the request and the answer cross the network unencrypted"))
    plan = {"run_id": rid, "kind": "kit", "status": "planned", "created_at": iso(now()), "command_hash": chash, "host_id": host_id(host), "host_class": host_class(host), "tls": sp.scheme.lower() == "https",
            "port": sp.port or (443 if sp.scheme.lower() == "https" else 80), "operation": f"{(req.get('protocol') or 'service').upper()} GetFeature (paged, read-only)", "layer": req.get("layer"),
            "protocol": req.get("protocol"), "filter_shape": shape_of(req.get("cql_filter")), "method": "GET", "out_dir": out_dir, "max_time_s": CHUNK_S, "dropped": [], "probe": None, "approval": None,
            "expected": None, "profile": profile_name or req.get("profile"), "kit_id": req.get("kit_id"), "kit_sha256": ksha, "page_size": req.get("page_size"),
            "auth": "basic" if cred else "none", "credentials": {"kind": cred["kind"], "checked": None} if cred else None, "cred_id": cred_id(cred)}
    _save(rid, plan, {"argv": argv, "command": cmd, "host": host, "extra": extra, "credential": cred, "kit_request": {k: v for k, v in req.items() if k != "endpoint"}})
    read = {"operation": "GetFeature", "layer": req.get("layer"), "cql_filter": req.get("cql_filter")}
    return {"ok": True, "run_id": rid, "command_hash": chash, "card": run_card(plan, argv, host if (show_host or cred) else None, [], read, kit=True, cred=cred), "findings": fs,
            "probe_command": probe_command(host, plan["port"], plan["tls"], kit_cacert)["command"]}


def run_card(plan: dict, argv: list[str], host: str | None, dropped: list[str], read: dict, kit: bool = False, cred: dict | None = None) -> str:
    shown = " ".join(shlex.quote(a) for a in argv)
    if cred and cred["kind"] == "file" and not kit:
        shown = "(user = <read from the secrets file>) | " + shown
    if host:
        shown = shown
    else:
        shown = re.sub(r"https?://[^\s'\"]+", "<service address withheld>", shown)
    short = shown if len(shown) < 700 else shown[:700] + " ..."
    L = [f"Run on your computer?                                    approval id: {plan['command_hash'][:12]}   run: {plan['run_id']}",
         f"  host:        {plan['host_class']} service ({host if host else 'host withheld in outputs'}){'' if plan['tls'] else '  PLAIN HTTP'}",
         f"  operation:   {plan['operation']}, {plan['method']}, read-only" + (", paged" if kit else ""),
         f"  layer:       {plan.get('layer') or '(not a layer request)'}" + (f"      filter shape: {plan['filter_shape']}" if plan.get("filter_shape") else ""),
         f"  expected:    {'a count check, then every page (kit)' if kit else 'one response; no count check (a kit adds one)'}   caps: {plan['max_time_s']} s per command, {MAX_BYTES // (1024 * 1024)} MB",
         (f"  credentials: HTTP Basic by reference {cred['kind']}:{cred['path']}  (read inside the shell; never shown, never stored in a record; sent only to the host above)" if cred
          else "  credentials: none"),
         f"  output:      {plan['out_dir']}/",
         f"  command:     {short}"]
    if kit:
        L.append(f"  kit:         {plan.get('kit_id')} (sha256 {str(plan.get('kit_sha256'))[:12]}..., checked on the device before it runs)")
    if dropped:
        L.append("  changed from your pasted curl:")
        L += [f"    dropped  {d}" for d in dropped[:10]]
    L += ["  redirects are not followed; the run fails if the server tries to send it elsewhere",
          "  [ Approve this run ]   [ Approve and allow refreshes for 30 days ]   [ Cancel ]"]
    return "\n".join(L)


# ============================================================================================================================ probe
PROBE_CODE = (
    "import socket,ssl,sys,json,ipaddress\n"
    "h,p,t=sys.argv[1],int(sys.argv[2]),sys.argv[3]=='yes'\n"
    "ca=sys.argv[4] if len(sys.argv)>4 else None\n"
    "r={'dns':False,'classes':[],'tcp':False,'tls':None,'error':None}\n"
    "def cl(a):\n"
    " i=ipaddress.ip_address(a.split('%')[0])\n"
    " i=getattr(i,'ipv4_mapped',None) or i\n"
    " if str(i) in ('169.254.169.254','169.254.170.2','100.100.100.200','fd00:ec2::254'): return 'metadata'\n"
    " if i.is_loopback or i.is_unspecified: return 'loopback'\n"
    " if i.is_link_local: return 'link-local'\n"
    " if i.is_multicast or i.is_reserved: return 'reserved'\n"
    " return 'private' if i.is_private else 'public'\n"
    "try:\n"
    " ai=socket.getaddrinfo(h,p,type=socket.SOCK_STREAM)\n"
    " r['dns']=True\n"
    " r['classes']=sorted({cl(a[4][0]) for a in ai})\n"
    " s=socket.create_connection(ai[0][4][:2],timeout=8)\n"
    " r['tcp']=True\n"
    " if t:\n"
    "  try:\n"
    "   c=ssl.create_default_context(cafile=ca).wrap_socket(s,server_hostname=h)\n"
    "   r['tls']='ok'\n"
    "   c.close()\n"
    "  except ssl.SSLCertVerificationError:\n"
    "   r['tls']='untrusted-certificate'\n"
    "  except Exception as e:\n"
    "   r['tls']='failed:'+type(e).__name__\n"
    " else:\n"
    "  s.close()\n"
    "except Exception as e:\n"
    " r['error']=type(e).__name__\n"
    "print(json.dumps(r))\n")


def probe_command(host: str, port: int = 443, tls: bool = True, cacert: str | None = None) -> dict:
    """The command that checks reach from the device shell: DNS, TCP and the TLS handshake. It sends no request and no data."""
    if not HOST_RE.match(host.strip("[]")) and ":" not in host:
        raise ValueError("not a valid host name")
    why = refuse_host(host)
    if why:
        raise ValueError(f"the host is {why}; approved runs never go there")
    ca = f" {shlex.quote(_device_path(cacert, 'the CA file'))}" if cacert else ""
    return {"command": f"python3 -c {shlex.quote(PROBE_CODE)} {shlex.quote(host)} {int(port)} {'yes' if tls else 'no'}{ca}", "timeout_ms": 30000, "sends": "nothing: DNS, TCP and the TLS handshake only"}


def probe_check(run_id: str, output: str) -> dict:
    """MAP-SVC-031: record what the probe found. Reachable means DNS resolved, TCP opened and, for https, the handshake verified."""
    plan, priv = load(run_id)
    fs: list[dict] = []
    try:
        line = [l for l in output.strip().splitlines() if l.strip().startswith("{")][-1]
        r = json.loads(line)
    except (IndexError, ValueError):
        fs.append(finding("MAP-SVC-031", "BLOCK", "the probe printed nothing readable; the device shell may not have python3. Use a user-run kit"))
        return {"ok": False, "findings": fs}
    bad = [c for c in r.get("classes") or [] if c in ("loopback", "link-local", "metadata", "reserved")]
    if bad:
        fs.append(finding("MAP-SVC-032", "BLOCK", "the name resolves to " + ", ".join(sorted(set(bad))) + " addresses; approved runs never go there"))
    elif not r.get("dns"):
        fs.append(finding("MAP-SVC-031", "BLOCK", "the host name does not resolve from the device shell. If it needs the VPN, connect and probe again; otherwise use a user-run kit"))
    elif not r.get("tcp"):
        fs.append(finding("MAP-SVC-031", "BLOCK", f"the device shell could not open a connection ({r.get('error') or 'refused or timed out'}). If it needs the VPN, connect and probe again; otherwise use a user-run kit"))
    elif plan["tls"] and r.get("tls") == "untrusted-certificate":
        fs.append(finding("MAP-SVC-031", "BLOCK", "the server's certificate is not trusted by the device. Give the CA file (--cacert, a path on the device); certificate checking is never turned off"))
    elif plan["tls"] and r.get("tls") != "ok":
        fs.append(finding("MAP-SVC-031", "BLOCK", f"the TLS handshake failed ({r.get('tls')})"))
    ok = not blocked(fs)
    plan["probe"] = {"ok": ok, "at": iso(now()), "dns": bool(r.get("dns")), "tcp": bool(r.get("tcp")), "tls": r.get("tls"), "classes": r.get("classes")}
    d = run_dir(run_id)
    (d / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    return {"ok": ok, "findings": fs, "probe": plan["probe"]}


# ============================================================================================================================ approval
def _approvals_path() -> Path:
    return store() / "approvals.json"


def _approvals() -> list[dict]:
    p = _approvals_path()
    return json.loads(p.read_text()) if p.is_file() else []


def _save_approvals(rows: list[dict]) -> None:
    p = _approvals_path()
    p.write_text(json.dumps(rows, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def approve(run_id: str, confirm_hash: str, *, standing_days: int | None = None) -> dict:
    """Record the person's approval. `confirm_hash` is the id the run card showed; a card that has changed since has another hash and is refused."""
    plan, priv = load(run_id)
    fs: list[dict] = []
    if not confirm_hash or not plan["command_hash"].startswith(confirm_hash) or len(confirm_hash) < 8:
        return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "the approval does not match this run's command; show the run card again and approve that one")]}
    if canonical_hash(priv["argv"], plan["out_dir"], _hashed_extra(priv)) != plan["command_hash"]:
        return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "the stored command no longer matches its hash; plan the run again")]}
    t = now()
    rec = {"approval_id": "ap-" + hashlib.sha256((plan["command_hash"] + iso(t)).encode()).hexdigest()[:10], "run_id": run_id, "command_hash": plan["command_hash"], "approved_at": iso(t),
           "approved_by": "user", "standing": None, "revoked": False}
    if standing_days:
        n = int(min(max(standing_days, 1), STANDING_MAX_DAYS))
        rec["standing"] = {"expires_at": iso(t + dt.timedelta(days=n)), "days": n, "scope": {"profile": plan.get("profile"), "operation": plan["operation"], "layer": plan.get("layer"),
                                                                                              "filter_shape": plan.get("filter_shape"), "host_id": plan["host_id"], "kind": plan["kind"], "auth": plan.get("auth", "none"),
                                                                                              "cred_id": plan.get("cred_id")}}
        fs.append(finding("MAP-SVC-028", "INFO", f"standing approval for {n} day(s): the same query shape on the same host and layer may be re-run by a refresh; list or revoke it with approved_run.py list / revoke"))
    rows = _approvals()
    rows.append(rec)
    _save_approvals(rows)
    plan["approval"] = rec["approval_id"]
    plan["status"] = "approved"
    (run_dir(run_id) / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    return {"ok": True, "approval": rec, "findings": fs}


def revoke(approval_id: str) -> dict:
    rows = _approvals()
    hit = [r for r in rows if r["approval_id"] == approval_id]
    for r in hit:
        r["revoked"] = True
        r["revoked_at"] = iso(now())
    _save_approvals(rows)
    return {"ok": bool(hit), "revoked": approval_id if hit else None}


def listing() -> list[dict]:
    t = now()
    out = []
    for r in _approvals():
        st = r.get("standing")
        live = not r["revoked"] and (not st or dt.datetime.fromisoformat(st["expires_at"].replace("Z", "+00:00")) > t)
        out.append({"approval_id": r["approval_id"], "run_id": r["run_id"], "approved_at": r["approved_at"], "standing": bool(st), "expires_at": (st or {}).get("expires_at"), "revoked": r["revoked"],
                    "live": live, "scope": (st or {}).get("scope")})
    return out


def _find_approval(plan: dict) -> tuple[dict | None, bool]:
    """(approval, standing). An exact approval of this command, or a live standing approval whose scope this run matches."""
    t = now()
    for r in _approvals():
        if r["revoked"]:
            continue
        st = r.get("standing")
        if r["command_hash"] == plan["command_hash"]:
            if st and dt.datetime.fromisoformat(st["expires_at"].replace("Z", "+00:00")) <= t:
                continue
            if not st and (t - dt.datetime.fromisoformat(r["approved_at"].replace("Z", "+00:00"))).total_seconds() > PENDING_TTL_S:
                continue
            return r, False
    for r in _approvals():
        st = r.get("standing")
        if r["revoked"] or not st or dt.datetime.fromisoformat(st["expires_at"].replace("Z", "+00:00")) <= t:
            continue
        sc = st["scope"]
        if all(sc.get(k) == v for k, v in (("operation", plan["operation"]), ("layer", plan.get("layer")), ("filter_shape", plan.get("filter_shape")), ("host_id", plan["host_id"]), ("kind", plan["kind"]), ("profile", plan.get("profile")), ("auth", plan.get("auth", "none")), ("cred_id", plan.get("cred_id")))):
            return r, True
    return None, False


def command(run_id: str) -> dict:
    """The command to run on the device. Refused unless the approval exists, the command is unchanged, the host passed the probe and the host is allowed."""
    plan, priv = load(run_id)
    fs: list[dict] = []
    if canonical_hash(priv["argv"], plan["out_dir"], _hashed_extra(priv)) != plan["command_hash"]:
        return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "the stored command no longer matches its hash; plan the run again")]}
    why = refuse_host(priv["host"])
    if why:
        return {"ok": False, "findings": [finding("MAP-SVC-032", "BLOCK", f"the host is {why}; approved runs never go there")]}
    ap, standing = _find_approval(plan)
    if ap is None:
        return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "this command has no approval (or it was revoked or has expired); show the run card and ask the person to approve it")]}
    pr = plan.get("probe") or {}
    if not pr.get("ok"):
        return {"ok": False, "findings": [finding("MAP-SVC-031", "BLOCK", "the device shell has not shown that it can reach the host; probe it first (probe-command, then probe-check)")]}
    if (now() - dt.datetime.fromisoformat(pr["at"].replace("Z", "+00:00"))).total_seconds() > PROBE_FRESH_S:
        return {"ok": False, "findings": [finding("MAP-SVC-031", "BLOCK", "the reach check is more than two hours old; probe the host again")]}
    cr = plan.get("credentials")
    if cr:
        ck = cr.get("checked") or {}
        if not ck.get("ok"):
            return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "the secrets file has not been checked from the device shell; run cred-command, then cred-check")]}
        if (now() - dt.datetime.fromisoformat(ck["at"].replace("Z", "+00:00"))).total_seconds() > PROBE_FRESH_S:
            return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "the secrets file check is more than two hours old; check it again")]}
        if priv.get("credential") and priv["credential"].get("kind") != cr.get("kind"):
            return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "the stored credential reference no longer matches the plan; plan the run again")]}
    if standing:
        fs.append(finding("MAP-SVC-028", "INFO", f"running under standing approval {ap['approval_id']} (expires {ap['standing']['expires_at'][:10]}), not a new approval of this run"))
    plan["status"] = "issued"
    plan["approval"] = ap["approval_id"]
    plan["issued_at"] = iso(now())
    (run_dir(run_id) / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    return {"ok": True, "command": priv["command"], "timeout_ms": (CHUNK_S + 20) * 1000, "approval_id": ap["approval_id"], "findings": fs,
            "note": "run exactly this with the device shell; it reads once and writes only into its own output folder"}


def cred_command(run_id: str) -> dict:
    """The command that checks the secrets file from the device shell. It prints yes/no answers about the file and never its content."""
    plan, priv = load(run_id)
    if not priv.get("credential"):
        return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "this run uses no credentials")]}
    return {"ok": True, **cb.check_command(priv["credential"], priv["host"])}


def cred_check(run_id: str, output: str) -> dict:
    """MAP-SVC-009 and 029: record what the check found, without any content of the file."""
    plan, priv = load(run_id)
    if not priv.get("credential"):
        return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "this run uses no credentials")]}
    sc = scrub_output(output, 2000)
    res = cb.check_result(priv["credential"], sc["text"])
    plan["credentials"]["checked"] = {"ok": res["ok"], "at": iso(now()), "answers": res["answers"]}
    (run_dir(run_id) / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    return res


# ============================================================================================================================ output
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-9;?]*[A-Za-z]")
_INSTRUCTION = re.compile(r"(?i)\b(ignore (?:all |any |the )?(?:previous|prior|above) (?:instructions|messages)|you are now|system prompt|disregard (?:the )?(?:above|previous)|run the following command)\b")


def scrub_output(text: str, limit: int = 20_000) -> dict:
    """MAP-SVC-030: credential patterns removed, control characters stripped, length capped; instruction-like text is flagged, never obeyed."""
    fs: list[dict] = []
    t = _CTRL.sub("", text or "")
    t, found = redact.scrub(t)
    if found:
        fs.append(finding("MAP-SVC-030", "WARN", "removed from the output before anyone read it: " + ", ".join(found)))
    if _INSTRUCTION.search(t):
        fs.append(finding("MAP-SVC-030", "WARN", "the output contains text that reads like an instruction; it is data from the server and is not acted on"))
    if len(t) > limit:
        t = t[:limit] + f"\n... [{len(text) - limit} more characters not shown]"
    return {"text": t, "findings": fs, "removed": found}


def _writeout(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and "http_code" in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def unpack(archive, dest) -> tuple[Path | None, list[dict]]:
    """Extract a returned .tgz into a new folder: regular files only, no path that leaves the folder, a size cap. Anything else ends the unpacking."""
    import tarfile
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                nm = m.name[2:] if m.name.startswith("./") else m.name
                if m.isdir() or nm in ("", "."):
                    continue
                if not m.isreg() or not rk._safe_member(nm):
                    return None, [finding("MAP-SVC-021", "BLOCK", f"the archive holds '{oh.untrusted(m.name, 60)}', which is not a plain file inside the folder; nothing was unpacked")]
                total += m.size
                if total > 4 * MAX_BYTES:
                    return None, [finding("MAP-SVC-005", "BLOCK", "the archive is larger than the plugin will unpack")]
            for m in tf.getmembers():
                nm = m.name[2:] if m.name.startswith("./") else m.name
                if m.isreg():
                    t = dest / nm
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_bytes(tf.extractfile(m).read())
    except (tarfile.TarError, OSError, EOFError) as ex:
        return None, [finding("MAP-SVC-021", "BLOCK", f"the archive cannot be read: {oh.untrusted(ex, 100)}")]
    return dest, []


def kit_progress(stdout: str) -> dict:
    """Did the kit finish, or stop at its time budget? The stop message is the kit's own; the exit code is the device shell's."""
    t = stdout or ""
    return {"more_to_do": "run the same command again to carry on" in t, "done": bool(re.search(r"^done: \d+ features", t, re.M))}


def finish(run_id: str, stdout: str, folder, *, name: str, profile: dict | None = None, accept_incomplete: str | None = None, outroot: Path | None = None) -> dict:
    """Judge what came back: scrub the shell's output, check the status and redirect, then verify the folder against its manifest and ingest it as a device-run capture."""
    plan, priv = load(run_id)
    fs: list[dict] = []
    sc = scrub_output(stdout)
    fs += sc["findings"]
    fol = Path(folder)
    if plan["kind"] == "kit":
        pr = kit_progress(stdout)
        if re.search(r"authentication failed", stdout or "") and not pr["done"]:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-018", "BLOCK", "the server did not accept the credentials; not retried, so the account is not locked. Fix the secrets file, then plan the run again")], "output": sc["text"]}
        if pr["more_to_do"] and not pr["done"]:
            return {"ok": False, "more_to_do": True, "findings": fs + [finding("MAP-SVC-031", "INFO", "the kit stopped at its time budget and kept its place; run the same command again, then finish")], "output": sc["text"]}
        if fol.is_file():
            fol, bad = unpack(fol, fol.parent / (fol.name.split(".")[0] + "-unpacked"))
            if fol is None:
                return {"ok": False, "findings": fs + bad, "output": sc["text"]}
    if plan["kind"] == "curl":
        w = _writeout(stdout)
        if not w:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-021", "BLOCK", "the run printed no status line; nothing was fetched")], "output": sc["text"]}
        if w.get("redirect_url"):
            return {"ok": False, "findings": fs + [finding("MAP-SVC-032", "BLOCK", "the server answered with a redirect; redirects are not followed, so nothing was accepted. Check the address")], "output": sc["text"]}
        code = int(w.get("http_code") or 0)
        if code in (401, 403):
            who = ("the credentials were not accepted (a wrong or expired entry in the secrets file, or an account without access); not retried, so the account is not locked. Fix the secrets file, then plan the run again"
                   if plan.get("credentials") else "it needs a login. Not retried. Plan the run again with --credentials netrc:PATH or file:PATH, or use a user-run kit")
            return {"ok": False, "findings": fs + [finding("MAP-SVC-018", "BLOCK", f"the server answered {code}: authentication failed; {who}")], "output": sc["text"]}
        if code != 200:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-021", "BLOCK", f"the server answered {code}")], "output": sc["text"]}
        if int(w.get("size_download") or 0) > MAX_BYTES:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-005", "BLOCK", f"the response is larger than {MAX_BYTES // (1024 * 1024)} MB, the most one transfer is trusted to carry; narrow the query")], "output": sc["text"]}
        resp = next((p for p in (fol / "response.bin", fol / "response.json") if p.is_file()), None)
        if resp is None:
            return {"ok": False, "findings": fs + [finding("MAP-SVC-021", "BLOCK", "the folder holds no response.bin")], "output": sc["text"]}
        body = resp.read_bytes()
        if re.match(rb"\s*(<!doctype html|<html)", body[:200], re.I):
            return {"ok": False, "findings": fs + [finding("MAP-SVC-021", "BLOCK", "the server returned a web page (an error or login page), not data")], "output": sc["text"]}
        try:
            doc = json.loads(body)
            feats = doc["features"]
            assert isinstance(feats, list)
        except Exception:  # noqa: BLE001
            return {"ok": True, "kind": "document", "findings": fs + [finding("MAP-SVC-021", "INFO", "the response is not GeoJSON features, so it is kept as a document and not ingested as a layer")], "output": sc["text"],
                    "response": str(resp), "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        (fol / "pages").mkdir(exist_ok=True)
        (fol / "pages" / "page_0001.json").write_bytes(body)
        started = plan.get("issued_at") or plan["created_at"]
        man = {"manifest_version": 1, "kit_id": f"curl-{run_id[4:]}-{plan['command_hash'][:6]}", "created_by": "approved-curl", "service": {"type": plan.get("protocol") if plan.get("protocol") in ("wfs", "ogcapi", "arcgis") else "wfs",
               "version": None, "host_class": "internal" if plan["host_class"] in ("private", "internal") else "public"},
               "request": {"operation": plan["operation"], "layer": plan.get("layer") or "response", "filter": None}, "run": {"started_at": started, "finished_at": iso(now()), "tool": "curl (rebuilt argument list)"},
               "result": {"hits_expected": None, "total_features": len(feats), "pages": 1, "truncated": False, "shortfall_note": "one request; the plugin did not check the count (a kit does)",
                          "files": [{"name": "pages/page_0001.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "feature_count": len(feats), "crs": "EPSG:4326"}]},
               "attestation": {"by": "plugin", "note": f"fetched by the plugin on the person's computer under approval {plan.get('approval')}"}}
        import yaml
        (fol / "manifest.yaml").write_text(yaml.safe_dump(man, sort_keys=False))
    ing = rk.ingest(fol, name=name, profile=profile, accept_incomplete=accept_incomplete, outroot=outroot, route="device-run",
                    run_info={"approval_id": plan.get("approval"), "approved_at": _approved_at(plan.get("approval")), "command_hash": plan["command_hash"], "host_id": plan["host_id"], "run_id": run_id, "kind": plan["kind"], "auth": plan.get("auth", "none")})
    fs += ing.get("findings", [])
    plan["status"] = "finished" if ing.get("ok") else "failed"
    plan["finished_at"] = iso(now())
    (run_dir(run_id) / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
    return {"ok": bool(ing.get("ok")), "findings": fs, "capture": rk.summary(ing) if ing.get("record") else None, "output": sc["text"], "record_path": ing.get("record_path")}


def _approved_at(approval_id: str | None) -> str | None:
    for r in _approvals():
        if r["approval_id"] == approval_id:
            return r["approved_at"]
    return None


# ============================================================================================================================ CLI
def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("probe-command")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--tls", default="yes", choices=["yes", "no"])
    p = sub.add_parser("probe-check")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("plan-curl")
    p.add_argument("--credentials", help="netrc:PATH or file:PATH, a secrets file on your computer (never a user name or password)")
    p.add_argument("--profile", help="a saved profile with auth basic; its credentials go only to its own host")
    p.add_argument("--curl-file")
    p.add_argument("--curl")
    p.add_argument("--device-dir", required=True)
    p.add_argument("--show-host", action="store_true")
    p = sub.add_parser("plan-kit")
    p.add_argument("--cacert", help="a CA file on your computer, for a private certificate authority")
    p.add_argument("--credentials")
    p.add_argument("--profile")
    p.add_argument("--kit-dir", required=True)
    p.add_argument("--device-kit-dir", required=True)
    p.add_argument("--device-dir", required=True)
    p.add_argument("--show-host", action="store_true")
    p = sub.add_parser("cred-command")
    p.add_argument("--run", required=True)
    p = sub.add_parser("cred-check")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--run", required=True)
    p.add_argument("--hash", required=True)
    p.add_argument("--standing-days", type=int)
    p = sub.add_parser("command")
    p.add_argument("--run", required=True)
    p = sub.add_parser("finish")
    p.add_argument("--run", required=True)
    p.add_argument("--stdout", required=True)
    p.add_argument("--folder", required=True, help="the returned folder, or the .tgz a kit run packs")
    p.add_argument("--name", required=True)
    p.add_argument("--accept-incomplete")
    p = sub.add_parser("scrub")
    p.add_argument("--file")
    sub.add_parser("list")
    p = sub.add_parser("revoke")
    p.add_argument("--approval", required=True)
    p = sub.add_parser("show")
    p.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    try:
        if a.op == "probe-command":
            _emit(probe_command(a.host, a.port, a.tls == "yes"))
        elif a.op == "probe-check":
            out = sys.stdin.read() if a.output == "-" else Path(a.output).read_text()
            r = probe_check(a.run, out)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "plan-curl":
            text = Path(a.curl_file).read_text() if a.curl_file else (a.curl or "")
            r = plan_curl(text, a.device_dir, show_host=a.show_host, credentials=a.credentials, profile_name=a.profile)
            if r.get("card"):
                print(r["card"] + "\n")
            _emit({k: v for k, v in r.items() if k != "card"})
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "plan-kit":
            r = plan_kit(a.kit_dir, a.device_kit_dir, a.device_dir, show_host=a.show_host, credentials=a.credentials, profile_name=a.profile, cacert=a.cacert)
            if r.get("card"):
                print(r["card"] + "\n")
            _emit({k: v for k, v in r.items() if k != "card"})
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "cred-command":
            r = cred_command(a.run)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "cred-check":
            r = cred_check(a.run, sys.stdin.read() if a.output == "-" else Path(a.output).read_text())
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "approve":
            r = approve(a.run, a.hash, standing_days=a.standing_days)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_REFUSED
        elif a.op == "command":
            r = command(a.run)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_REFUSED
        elif a.op == "finish":
            r = finish(a.run, Path(a.stdout).read_text() if a.stdout != "-" else sys.stdin.read(), a.folder, name=a.name, accept_incomplete=a.accept_incomplete)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_LINT
        elif a.op == "scrub":
            r = scrub_output(Path(a.file).read_text() if a.file else sys.stdin.read())
            print(r["text"])
            for f in r["findings"]:
                print(f"[{f['id']}] {f['message']}", file=sys.stderr)
        elif a.op == "list":
            _emit(listing())
        elif a.op == "revoke":
            r = revoke(a.approval)
            _emit(r)
            return gc.EXIT_OK if r["ok"] else gc.EXIT_USAGE
        elif a.op == "show":
            plan, _ = load(a.run)
            _emit(plan)
    except ValueError as ex:
        print(f"approved_run: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
