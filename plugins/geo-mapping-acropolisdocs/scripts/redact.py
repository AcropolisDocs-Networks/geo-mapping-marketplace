#!/usr/bin/env python3
"""redact: one secret scrubber for everything the plugin writes or shows (MAP-SVC-022).

`find(text)` names the kinds of secret in a text; `scrub(text)` replaces each secret with `***` and tells which kinds it removed. A text that has been
scrubbed finds nothing, so the same patterns serve the lint (no secret in a record) and the pasted-command reader (adopt).
"""
from __future__ import annotations

import re
import sys

_SCHEME = r"(?:basic|bearer|digest|token|negotiate|apikey|ntlm)"
# (pattern, label, replacement). Group 1 is the part that stays; the secret is what follows it.
RULES = [
    (re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?(?:" + _SCHEME + r"\s+)?)(?!\*\*\*)(?!" + _SCHEME + r"\s+\*\*\*)[^\s'\"]{4,}"), "an Authorization header", r"\1***"),
    (re.compile(r"(?i)(\bproxy-authorization[\"']?\s*[:=]\s*[\"']?(?:" + _SCHEME + r"\s+)?)(?!\*\*\*)(?!" + _SCHEME + r"\s+\*\*\*)[^\s'\"]{4,}"), "a proxy Authorization header", r"\1***"),
    (re.compile(r"(?i)(\bbearer\s+)(?!\*\*\*)[a-z0-9._~+/=-]{8,}"), "a bearer token", r"\1***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "a signed token", "***"),
    (re.compile(r"(://[^/\s:@\"']+:)(?!\*\*\*@)([^/\s@\"']+)(@)"), "a user name and password in an address", r"\1***\3"),
    (re.compile(r"(://)(?!\*\*\*@)([^/\s:@\"']{6,})(@)"), "a credential in an address", r"\1***\3"),
    (re.compile(r"(?i)(\b(?:key|token|apikey|api_key|api-key|access_token|auth|authkey|password|passwd|pwd|secret|client_secret|sig|signature|sas)=)(?!\*\*\*)[^&\s\"']+"), "a key, token or password", r"\1***"),
    (re.compile(r"(?i)([\"']?\b(?:x-api-key|x-auth-token|x-access-token|x-api-token|api-key|apikey|hash_value|x-amz-security-token|ocp-apim-subscription-key)[\"']?\s*[:=]\s*[\"']?)(?!\*\*\*)[^\s'\"]{3,}"), "an API key header", r"\1***"),
    (re.compile(r"(?i)(\bcookie\s*:\s*)(?!\*\*\*)(?!\s*\*\*\*)[^\n'\"]{4,}"), "a cookie", r"\1***"),
    (re.compile(r"(?i)((?:^|\s)(?:-u|--user|--proxy-user)(?:\s+|=))(?![+%$-])(?!\*\*\*)('[^'\s:]+:[^']*'|\"[^\"\s:]+:[^\"]*\"|[^\s:'\"+%$-][^\s:]*:\S*)"), "a user and password on a command line", r"\1***"),
    (re.compile(r"(?i)((?:^|\s)(?:-b|--cookie)(?:\s+|=))(?!\*\*\*)('[^']*'|\"[^\"]*\"|\S+)"), "a cookie on a command line", r"\1***"),
]
SECRET_PATTERNS = [(r[0], r[1]) for r in RULES]       # (pattern, label): the form service_lint always used


# a command line a person pasted: any -u argument is a login, even a bare user name (nothing legitimate in a pasted curl needs it)
COMMAND_RULES = [(re.compile(r"(?i)((?:^|\s)(?:-u|--user|--proxy-user|--oauth2-bearer)(?:\s+|=))(?!\*\*\*)('[^']*'|\"[^\"]*\"|\S+)"), "a login on a command line", r"\1***")]


def scrub_command(text: str) -> tuple[str, list[str]]:
    """scrub() plus the stricter command-line rule; for a pasted curl or wget line that is never run."""
    text, found = scrub(text)
    for pat, label, repl in COMMAND_RULES:
        new, n = pat.subn(repl, text)
        if n and new != text:
            text = new
            if label not in found:
                found.append(label)
    return text, found


def find(text: str) -> list[str]:
    """The kinds of secret in a text, each named once."""
    out: list[str] = []
    for pat, label, _ in RULES:
        if pat.search(text) and label not in out:
            out.append(label)
    return out


def scrub(text: str) -> tuple[str, list[str]]:
    """The text with every secret replaced by ***, and the kinds that were removed."""
    found: list[str] = []
    for pat, label, repl in RULES:
        new, n = pat.subn(repl, text)
        if n and new != text:
            text = new
            if label not in found:
                found.append(label)
    return text, found


def scrub_obj(obj):
    """Scrub every string in a nested structure (dict keys are kept). Returns (clean, kinds)."""
    kinds: list[str] = []

    def walk(o):
        if isinstance(o, str):
            s, f = scrub(o)
            kinds.extend(k for k in f if k not in kinds)
            return s
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o
    return walk(obj), kinds


# ---------------------------------------------------------------------------------------------------------------- exact-value masking (0.6.0)
MIN_MASK_LEN = 4          # shorter values would mask ordinary words; a real key is far longer


def _forms(value: str) -> list[str]:
    """A value and the forms it takes in an address or a header: URL-encoded (both ways) and, for user:password, Base64."""
    import base64
    import urllib.parse
    out = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value), urllib.parse.quote(value)}
    if ":" in value:
        out.add(base64.b64encode(value.encode("utf-8")).decode("ascii"))
    return sorted((v for v in out if len(v) >= MIN_MASK_LEN), key=len, reverse=True)


PREFIX_LEN = 12          # a value cut short (a message capped at N characters) still starts with its first 12 characters


def mask(text, values) -> str:
    """The text with every exact value (and its encoded forms) replaced by ***. Used by the process that read a credential, on every string it
    returns or writes (credentials spec R7); `scrub` is the pattern pass that runs everywhere else. A long value cut short by a length cap is
    caught by its first characters, so a truncated message never carries part of a key."""
    if not isinstance(text, str) or not values:
        return text
    import re
    for v in values:
        if not isinstance(v, str) or len(v) < MIN_MASK_LEN:
            continue
        for form in _forms(v):
            if form in text:
                text = text.replace(form, "***")
            if len(form) >= 2 * PREFIX_LEN and form[:PREFIX_LEN] in text:
                text = re.sub(re.escape(form[:PREFIX_LEN]) + r"[A-Za-z0-9._~+/=%-]*", "***", text)
    return text


def mask_bytes(data: bytes, values) -> bytes:
    """mask() for a response body: every exact value (and its encoded forms) replaced by ***."""
    if not data or not values:
        return data
    for v in values:
        if isinstance(v, str) and len(v) >= MIN_MASK_LEN:
            for form in _forms(v):
                b = form.encode("utf-8")
                if b in data:
                    data = data.replace(b, b"***")
    return data


def mask_obj(obj, values):
    """mask() over every string in a nested structure (dict keys too, since a key can be any text)."""
    if not values:
        return obj
    if isinstance(obj, str):
        return mask(obj, values)
    if isinstance(obj, dict):
        return {mask(k, values) if isinstance(k, str) else k: mask_obj(v, values) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v, values) for v in obj]
    return obj


def main(argv=None) -> int:
    text = sys.stdin.read()
    clean, kinds = scrub(text)
    sys.stdout.write(clean)
    if kinds:
        sys.stderr.write("removed: " + ", ".join(kinds) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
