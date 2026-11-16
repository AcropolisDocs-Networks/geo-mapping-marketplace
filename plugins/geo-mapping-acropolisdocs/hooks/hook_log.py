#!/usr/bin/env python3
"""hook_log: the map plugin's own hook log (1.17.2). Shared by the timing hook, the pre-export check and the credentials reminder.

One file per session: <working folder>/traces/hooks/<session id>.ndjson, where the working folder is the plugin's `.geo-mapping/` for the
folder the session runs in (GEO_MAPPING_HOME overrides; a system or read-only folder falls back to the home folder, as everywhere in the
plugin). One JSON object per line, written with a single append. Only event names, ids, times, exit codes and the plugin's own script,
skill and tool names are recorded: never a command, an argument, a result, an error text or a message.

It belongs to this plugin alone: it is not beside the transcript and never touches another plugin's log, so it cannot collide with the
acropolisdocs run-trace. Nothing here prints to stdout or raises: a hook that cannot log lets the work go on untouched.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def workdir_for(cwd: str | None) -> Path:
    """The plugin's .geo-mapping/ for a session folder, by the rules of gmap_common.default_workdir (imported lazily: only map calls pay)."""
    base = os.environ.get("GEO_MAPPING_HOME")
    if base:
        return Path(base)
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import gmap_common as gc
    folder = Path(cwd) if cwd else Path.cwd()
    try:
        if not folder.is_dir() or gc.system_folder(folder) or not os.access(folder, os.W_OK):
            return Path.home() / gc.WORKDIR_NAME
    except Exception:                                   # noqa: BLE001
        return Path.home() / gc.WORKDIR_NAME
    return folder / gc.WORKDIR_NAME


def log_path(session_id: str, cwd: str | None) -> Path | None:
    if not session_id or not SAFE_ID.match(session_id):
        return None
    return workdir_for(cwd) / "traces" / "hooks" / f"{session_id}.ndjson"


def append(event: dict, record: dict) -> None:
    """Add one record to the session's log. Silent on every failure."""
    try:
        p = log_path(str(event.get("session_id") or ""), event.get("cwd"))
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": now_ms(), **record}, separators=(",", ":"), sort_keys=True) + "\n"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:                                   # noqa: BLE001 - logging never affects the session
        pass
