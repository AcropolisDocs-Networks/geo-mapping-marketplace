#!/usr/bin/env python3
"""Map rules digest (4.2.0): the standing rules, put in front of the session when a map is asked for, once a session.

Called by the `UserPromptSubmit` hook (hooks/credentials_reminder.py, the one entry for that event). The rules a map must keep were
written in the map-create skill and its modules, and a session read them in pieces: Day 25's first map was drawn dark with no ask, though
"dark only when the person asks" stood in the reference and in the schema. When the person's message asks for a map, this adds the ten
lines of data/policy.yaml's `digest` to the model's context, so the rules arrive with the request and not after a search for them.

It acts once a session (the plugin's own hook log remembers it: a line `{"e": "digest"}`, never the message), prints nothing for any other
message, never blocks and reaches no network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A request for a map, not the word map in passing ("map these columns", "a map function"): a making verb near the noun, a map of or for
# something, a map kind by name, the challenge names, or one of this plugin's own commands.
MAP_REQUEST = re.compile(
    r"(?:\b(?:make|build|create|draw|design|render|generate|produce|update|redo|rebuild|restyle|export|plot)\b[^.?!\n]{0,60}\bmaps?\b)"
    r"|(?:\bmaps?\s+(?:of|for|showing|that\s+shows?)\b)"
    r"|(?:\b(?:choropleth|isochrone|walkshed|viewshed|hexbin|heat\s?map|web\s?map|story\s?map|locator\s+map|site\s+map|coverage\s+map|transit\s+map)\b)"
    r"|(?:\bmap\s?challenge\b|#30daymapchallenge|\b100[- ]item\s+map\b)"
    r"|(?:/geo-spatial-mapping:(?:make|web|map-create|webmap-create|map-story|map-atlas|map-restyle|isochrone-map|terrain-analysis))",
    re.I)


def wanted(text: str) -> bool:
    return bool(text) and bool(MAP_REQUEST.search(text))


def digest_text() -> str | None:
    try:
        import yaml
        doc = yaml.safe_load((ROOT / "data" / "policy.yaml").read_text(encoding="utf-8")) or {}
    except Exception:                                    # noqa: BLE001 - no PyYAML or no file: say nothing
        return None
    lines = [str(x) for x in (doc.get("digest") or [])][:12]
    return "\n".join(lines) if lines else None


def already(event: dict) -> bool:
    """True when this session's hook log already holds a digest line."""
    try:
        import hook_log
        p = hook_log.log_path(str(event.get("session_id") or ""), event.get("cwd"))
        if p is None or not p.is_file():
            return False
        with open(p, encoding="utf-8") as fh:
            return any('"e":"digest"' in ln for ln in fh)
    except Exception:                                    # noqa: BLE001
        return False


def decide(event: dict) -> str | None:
    """The digest to add for this message, or None. Records that it was given."""
    if not wanted(str(event.get("prompt") or "")) or already(event):
        return None
    text = digest_text()
    if not text:
        return None
    try:
        import hook_log
        hook_log.append(event, {"e": "digest"})
    except Exception:                                    # noqa: BLE001
        pass
    return text


if __name__ == "__main__":                               # a check by hand: echo '{"prompt": "make a map of Tempe"}' | python3 map_rules.py
    import sys
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        ev = {}
    print(decide(ev if isinstance(ev, dict) else {}) or "")
