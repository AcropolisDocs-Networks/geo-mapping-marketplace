#!/usr/bin/env python3
"""Pre-export lint hook (spec 5 and 12, gap analysis P1-13). On since 1.17.0 (spike P7): see hooks/README.md.

A `PreToolUse` hook (Claude Cowork and Claude Code) on the Bash, PowerShell and device shell tools (PowerShell since 1.17.3: Claude Code's primary shell on Windows; the device shell of a Cowork session linked to a computer since 1.20.1). It reads the hook's JSON from stdin and acts only when the command runs this plugin's
own `build_map.py`, `webmap.py` or `map_restyle.py --build` with a `--spec` (through `$GMAP_ROOT`, the skills' resolver, or this plugin's
root path). For any other command it exits 0 at once and prints nothing, so it scopes itself by the command line, since a hook matcher
selects tools, not files. When it acts, it runs the spec lint (MAP-SPEC-*, and the `ops:` checks inside it) on that MapSpec; a BLOCK
stops the command with exit 2 and the findings on stderr, which Claude Code shows to the model. Anything it cannot read (no spec, a
parse error, a missing module, a schema check that could not run because jsonschema is too old) lets the command run: the build lints the
spec again and is the gate that counts. A `cd DIR &&` before the exporter is followed, so a relative --spec is found where the command runs.

Reaches no network. 1.17.2: a stop also adds one line to the plugin's own hook log (hooks/hook_log.py: the call's id, the script and the
finding ids, never the spec's text), so map-trace can tell a call this hook stopped from one that failed.
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
# 1.17.3: PowerShell's Set-Location and Push-Location (and their aliases) as well as cd, with ; or a new line between commands.
CD = re.compile(r"""(?:^|[;&|(\n]\s*)(?:cd|chdir|pushd|sl|Set-Location|Push-Location)\s+(?:-(?:Literal)?Path\s+)?("[^"]*"|'[^']*'|[^\s;&|)]+)""",
                re.I)
EXPORTER_RX = "|".join(re.escape(e) for e in EXPORTERS)
# A literal folder in front of scripts/<exporter>: the PowerShell tool spells the plugin's folder out, and a plugin installed from a
# marketplace added by local path runs in place, from the folder it was added from. It counts only when that folder is a copy of this
# plugin (it holds scripts/gmap_common.py), so another plugin's build script is never linted.
LITERAL = re.compile(r"""((?:[A-Za-z]:)?/[^\s"';&|]*?|~[^\s"';&|]*?)/scripts/(%s)""" % EXPORTER_RX)


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def ours(command: str) -> str | None:
    """The exporter this command runs, when it is this plugin's own; None otherwise."""
    m = _match(command)
    return m[1] if m else None


def run_dir(command: str, cwd: Path) -> Path | None:
    """The folder the exporter runs in: the session's cwd, moved by each `cd DIR` before the exporter. None when a cd cannot be followed."""
    command = _cmd(command)
    m = _match(command)
    here = cwd
    for arg in CD.findall(command[: m[0]] if m else ""):
        arg = arg.strip("\"'")
        if not arg or arg == "-" or "$" in arg or "`" in arg:
            return None
        here = here / Path(arg).expanduser()
    return here


def _cmd(command: str) -> str:
    """The command as the readers see it: Windows path separators as forward slashes (shell_calls.normalized)."""
    try:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import shell_calls
        return shell_calls.normalized(command)
    except Exception:                                    # noqa: BLE001 - without the reader the Bash forms still match
        return command


def _match(command: str) -> tuple[int, str] | None:
    """(where the exporter's path starts, the exporter) when the command runs this plugin's own exporter; None otherwise."""
    command = _cmd(command)
    roots = [re.escape(_norm(str(ROOT))), r"\$GMAP_ROOT", r"\$\{GMAP_ROOT\}", r"\$env:CLAUDE_PLUGIN_ROOT", r"\$\{env:CLAUDE_PLUGIN_ROOT\}"]
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        roots.append(re.escape(_norm(env_root)))
    m = re.search(r"(?i:%s)/scripts/(%s)" % ("|".join(roots), EXPORTER_RX), command)
    if m:
        return m.start(), m.group(1)
    for m in LITERAL.finditer(command):
        if (Path(m.group(1)).expanduser() / "scripts" / "gmap_common.py").is_file():
            return m.start(), m.group(2)
    return None


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
    tool = str(event.get("tool_name") or "")
    # Bash: the shell of a Cowork session's workspace (and of Claude Code); 1.17.3: PowerShell, Claude Code's primary shell on Windows;
    # 1.20.1: the device shell of a Cowork session linked to the person's computer (mcp__...__device_bash), where builds run on their files
    if tool not in ("Bash", "PowerShell") and not tool.endswith("device_bash"):
        return 0, ""
    command = _cmd(str((event.get("tool_input") or {}).get("command") or ""))
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
    return handle(event if isinstance(event, dict) else {})


def handle(event: dict) -> int:
    """The verdict on one event: prints the findings and logs a stop (1.22.0: hooks/dispatch.py calls this after the timer)."""
    code, msg = decide(event)
    if msg:
        print(msg, file=sys.stderr)
    if code == 2 and event.get("tool_use_id"):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import hook_log
            ids = sorted(set(re.findall(r"\bMAP-[A-Z0-9]+-[0-9A-Z]+\b", msg)))
            hook_log.append(event, {"e": "stop", "id": str(event["tool_use_id"])[:64], "script": (ours(str((event.get("tool_input") or {}).get("command") or "")) or "")[:-3],
                                    "findings": ids})
        except Exception:                                # noqa: BLE001 - the log never changes the verdict
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
