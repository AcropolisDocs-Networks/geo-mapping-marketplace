#!/usr/bin/env python3
"""Credentials reminder hook (spec 5 and 14, gap analysis P1-13). OFF by default: see hooks/README.md.

A Claude Code `UserPromptSubmit` hook. It reads the person's message from the hook's JSON on stdin and looks for a key, token or password
with the plugin's own secret patterns (`scripts/redact.py`, the same ones that scrub every record and pasted command, MAP-SVC-022) and a
few bare key shapes that have no `key=` in front of them. When one is there it adds a reminder to the model's context: do not repeat,
store or use it, and point the person to their own credential folder (map-credentials). It never prints the secret, never blocks the
message and never writes a file. With nothing found it prints nothing.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Bare key shapes: a value pasted alone, with no `key=` or header name the redact rules would see. (pattern, what it looks like)
BARE = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "a Google API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "a Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "a secret API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bGEO_MAPPING_KEY_[A-Z0-9_]+\s*[=:]\s*['\"]?[^\s'\"]{6,}"), "a tile provider key with its value"),
]

REMINDER = (
    "geo-mapping credentials reminder (MAP-CRED-007): the person's last message appears to contain {kinds}. Do not repeat it, quote it, put "
    "it in a command, a file, a map, a web page, a log or a saved note, and do not use it. In one or two plain sentences: say that keys never "
    "go in the chat; that their own keys live in their own credential folder on their computer, added with map-credentials add NAME and "
    "opened in their own editor; and, since this one has now been typed into the conversation, that they should revoke it at the service and "
    "rotate it (map-credentials rotate NAME). Then carry on with the request without the secret."
)


def _norm(kind: str) -> str:
    """'a Google API key' and 'Google API key' are the same kind."""
    k = kind.strip().lower()
    for art in ("a ", "an "):
        if k.startswith(art):
            k = k[len(art):]
    return k


def _seen(what: str, found: list[str]) -> bool:
    w = _norm(what)
    return any(w == _norm(f) or w in _norm(f) or _norm(f) in w for f in found)


def kinds(text: str) -> list[str]:
    found: list[str] = []
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import credentials                               # the credentials scanner (MAP-CRED-007): redact patterns, key shapes, a Census key in context
        f = credentials.pasted_finding(text)
        found += list((f or {}).get("detail") or [])
    except Exception:                                    # noqa: BLE001 - fall back to the redact patterns, then the bare shapes below
        try:
            import redact
            found += redact.find(text)
        except Exception:                                # noqa: BLE001
            pass
    for rx, what in BARE:
        if rx.search(text) and not _seen(what, found):
            found.append(what)
    return found


def decide(event: dict) -> dict | None:
    text = str(event.get("prompt") or "")
    if not text:
        return None
    ks = kinds(text)
    if not ks:
        return None
    k = ks[0] if len(ks) == 1 else ", ".join(ks[:-1]) + " and " + ks[-1]
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": REMINDER.format(kinds=k)}}


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    out = decide(event if isinstance(event, dict) else {})
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
