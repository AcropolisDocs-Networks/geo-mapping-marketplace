#!/usr/bin/env python3
"""pin_python: point this plugin's hook commands at the Python running this script (1.17.0, spike P7).

The shipped hooks.json starts each hook with `python3`. On Windows that name is usually the Microsoft Store stub, which prints a message and
exits 9009, so every Bash call would show a hook error. The server launcher (mcp/launch.cmd) runs this script with the plugin's own Python
on each start; it rewrites `python3 "` at the head of each hook command to that interpreter's full path. It runs again after an update
replaces the plugin folder, changes nothing when the commands already name an interpreter, never prints to stdout (the MCP protocol owns
it) and never fails the launch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent / "hooks.json"


def pinned(command: str, exe: str) -> str:
    if command.startswith("python3 "):
        return f'"{exe}" ' + command[len("python3 "):]
    return command


def main() -> int:
    try:
        data = json.loads(HOOKS.read_text(encoding="utf-8"))
        exe = sys.executable
        changed = False
        for groups in (data.get("hooks") or {}).values():
            for g in groups:
                for h in g.get("hooks") or []:
                    new = pinned(str(h.get("command") or ""), exe)
                    if new != h.get("command"):
                        h["command"] = new
                        changed = True
        if changed:
            HOOKS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception as ex:  # noqa: BLE001 - a hook left on python3 is a notice, never a failed server start
        print(f"geo-mapping: the hook commands were not pointed at the plugin's Python ({type(ex).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
