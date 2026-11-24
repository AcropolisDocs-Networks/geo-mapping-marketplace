#!/usr/bin/env python3
"""map_timing: measured start, end and failure of the map plugin's own calls, for map-trace (1.17.2).

A hook (Claude Cowork and Claude Code) on PreToolUse, PostToolUse and PostToolUseFailure, with the matcher `Bash|PowerShell|mcp__.*device_bash|mcp__.*geo-spatial-mapping.*`
(the shells, the device shell on a linked computer, and this plugin's map server). 1.17.3: PowerShell too, the primary shell of Claude Code
on Windows; both shells' command lines are read by scripts/shell_calls.py, which map-trace shares. A matcher selects tools, not files, so the hook scopes
itself: it records a call only when the command runs one of this plugin's scripts or lints with Python, or the tool is this plugin's map
server. Every other call returns at once, unrecorded.

Recorded, per call, in the plugin's own hook log (hooks/hook_log.py): the event (start, end, fail), the call's tool_use_id (which joins
the line back to the transcript), the time, the runtime's own duration_ms, the exit code at the head of a failure and the interrupt flag,
and the script or map-server tool name. Never the command, an argument, a result or an error text. The transcript can only bound a
call's duration from message times; this measures it.

Prints nothing, writes nothing else, reaches no network and always exits 0: it can never stop, change or slow a call beyond its own start.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVENTS = {"PreToolUse": "start", "PostToolUse": "end", "PostToolUseFailure": "fail"}
EXIT = re.compile(r"^\s*(?:Error:\s*)?Exit code (\d+)", re.M)


def own_scripts() -> set[str]:
    out: set[str] = set()
    for d in ("scripts", "lints"):
        try:
            out |= {n[:-3] for n in os.listdir(ROOT / d) if n.endswith(".py")}
        except OSError:
            pass
    return out


def subject(event: dict) -> tuple[str, str] | None:
    """('script', name) or ('server', tool) when the call is this plugin's own; None otherwise."""
    tool = str(event.get("tool_name") or "")
    if tool.startswith("mcp__") and ("geo-mapping" in tool or "geo-spatial-mapping" in tool):
        return "server", tool.rsplit("__", 1)[-1]
    if tool not in ("Bash", "PowerShell") and not tool.endswith("device_bash"):
        return None
    raw = str((event.get("tool_input") or {}).get("command") or "")
    if not any(k in raw for k in ("scripts", "lints", "Scripts")):
        return None                                     # the common case returns before anything is imported
    sys.path.insert(0, str(ROOT / "scripts"))
    import shell_calls
    found = shell_calls.runs(shell_calls.command_of(tool, event.get("tool_input")), own_scripts())
    return ("script", found[0]) if found else None


def record(event: dict) -> dict | None:
    kind = EVENTS.get(str(event.get("hook_event_name") or ""))
    if not kind or not event.get("tool_use_id"):
        return None
    sub = subject(event)
    if sub is None:
        return None
    rec = {"e": kind, "id": str(event["tool_use_id"])[:64], sub[0]: sub[1]}
    if kind != "start" and isinstance(event.get("duration_ms"), (int, float)):
        rec["ms"] = int(event["duration_ms"])
    if kind == "fail":
        m = EXIT.search(str(event.get("error") or "")[:200])
        rec["exit"] = int(m.group(1)) if m else None
        rec["interrupt"] = bool(event.get("is_interrupt"))
    return rec


def handle(event: dict) -> None:
    """Record one event (1.22.0: hooks/dispatch.py calls this for PreToolUse, so the start costs no process of its own)."""
    rec = record(event)
    if rec:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import hook_log
        hook_log.append(event, rec)


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if isinstance(event, dict):
            handle(event)
    except Exception:                                   # noqa: BLE001 - the timer never affects the session it observes
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
