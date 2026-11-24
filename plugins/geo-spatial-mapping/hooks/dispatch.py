#!/usr/bin/env python3
"""dispatch: one process for the two hooks that run before a tool call (1.22.0).

Until 1.21.2 `PreToolUse` carried two entries, the timing hook and the pre-export check, so every shell call in every session started two
Python processes before it ran (and a third after it), in sessions that never made a map as well. This script is the single `PreToolUse`
entry: it reads the event once, lets the timing hook record the start of one of this plugin's own calls, then asks the pre-export check for
its verdict. Both scope themselves as before (hooks/README.md), so any other call returns at once and unrecorded.

The exit code is the pre-export check's alone: 2 with its findings on stderr when a MapSpec has a blocking finding, otherwise 0. The timer
can never change it: it runs first, inside its own guard. Reaches no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        import map_timing
        map_timing.handle(event)
    except Exception:                                    # noqa: BLE001 - the timer never affects the session it observes
        pass
    if event.get("hook_event_name") not in (None, "", "PreToolUse"):
        return 0                                         # the verdict belongs to the call that is about to run, never to one that has
    try:
        import pre_export_lint
    except Exception:                                    # noqa: BLE001 - a hook never stops work it cannot judge
        return 0
    return pre_export_lint.handle(event)


if __name__ == "__main__":
    sys.exit(main())
