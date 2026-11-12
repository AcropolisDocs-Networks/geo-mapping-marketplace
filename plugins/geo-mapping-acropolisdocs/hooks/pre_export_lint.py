#!/usr/bin/env python3
"""Pre-export lint hook (spec 5 and 12, gap analysis P1-13). OFF by default: see hooks/README.md.

A Claude Code `PreToolUse` hook on the Bash tool. It reads the hook's JSON from stdin and acts only when the command runs this plugin's
own `build_map.py`, `webmap.py` or `map_restyle.py --build` with a `--spec` (through `$GMAP_ROOT`, the skills' resolver, or this plugin's
root path). For any other command it exits 0 at once and prints nothing, so it scopes itself by the command line, since a hook matcher
selects tools, not files. When it acts, it runs the spec lint (MAP-SPEC-*, and the `ops:` checks inside it) on that MapSpec; a BLOCK
stops the command with exit 2 and the findings on stderr, which Claude Code shows to the model. Anything it cannot read (no spec, a
parse error, a missing module, a schema check that could not run because jsonschema is too old) lets the command run: the build lints the
spec again and is the gate that counts. A `cd DIR &&` before the exporter is followed, so a relative --spec is found where the command runs.

Writes nothing and reaches no network.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTERS = ("build_map.py", "webmap.py", "map_restyle.py")
UNJUDGED = "could not be checked against its schema"      # MAP-SPEC-001 when the schema check itself failed (no or old jsonschema): not a verdict
CD = re.compile(r"(?:^|[;&|(]\s*)cd\s+(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)")


def ours(command: str) -> str | None:
    """The exporter this command runs, when it is this plugin's own; None otherwise."""
    m = _match(command)
    return m.group(1) if m else None


def run_dir(command: str, cwd: Path) -> Path | None:
    """The folder the exporter runs in: the session's cwd, moved by each `cd DIR` before the exporter. None when a cd cannot be followed."""
    m = _match(command)
    here = cwd
    for arg in CD.findall(command[: m.start()] if m else ""):
        arg = arg.strip("\"'")
        if not arg or arg == "-" or "$" in arg or "`" in arg:
            return None
        here = here / Path(arg).expanduser()
    return here


def _match(command: str):
    roots = [re.escape(str(ROOT)), r"\$GMAP_ROOT", r"\$\{GMAP_ROOT\}"]
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        roots.append(re.escape(env_root.rstrip("/")))
    rx = re.compile(r"(?:%s)/scripts/(%s)" % ("|".join(roots), "|".join(re.escape(e) for e in EXPORTERS)))
    return rx.search(command)


def spec_arg(command: str, script: str) -> str | None:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    if script == "map_restyle.py" and "--build" not in words:
        return None                                       # a restyle without --build writes only the MapSpec
    for i, w in enumerate(words):
        if w == "--spec" and i + 1 < len(words):
            return words[i + 1]
        if w.startswith("--spec="):
            return w.split("=", 1)[1]
    return None


def decide(event: dict) -> tuple[int, str]:
    """(exit code, message for stderr): 0 and '' to let the command run, 2 and the findings to stop it."""
    if event.get("tool_name") != "Bash":
        return 0, ""
    command = str((event.get("tool_input") or {}).get("command") or "")
    script = ours(command)
    if not script:
        return 0, ""
    spec = spec_arg(command, script)
    if not spec or "$" in spec:
        return 0, ""
    path = Path(spec).expanduser()
    if not path.is_absolute():
        base = run_dir(command, Path(event.get("cwd") or os.getcwd()))
        if base is None:
            return 0, ""
        path = base / path
    if not path.is_file():
        return 0, ""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "lints")]
    try:
        import gmap_common as gc
        import mapspec_lint
        findings = mapspec_lint.lint_spec(gc.load_mapspec(path))
    except Exception:                                    # noqa: BLE001 - a hook never stops work it cannot judge
        return 0, ""
    if any(UNJUDGED in str(f.get("message")) for f in findings):
        return 0, ""                                     # the schema check did not run here; the build reports it with the fix
    block = [f for f in findings if f.get("severity") == "BLOCK"]
    if not block:
        return 0, ""
    lines = [f"geo-mapping pre-export check: {path.name} has {len(block)} blocking finding(s); fix the MapSpec, then run the command again."]
    lines += [f"  {f['id']}: {str(f['message'])[:220]}" for f in block[:8]]
    return 2, "\n".join(lines)


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    code, msg = decide(event if isinstance(event, dict) else {})
    if msg:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
