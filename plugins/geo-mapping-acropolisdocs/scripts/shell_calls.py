#!/usr/bin/env python3
"""shell_calls: read a shell tool call the way the map plugin's hooks and map-trace need to (1.17.3).

One place that answers "does this Bash or PowerShell command run one of this plugin's scripts?" for the timing hook, the pre-export check
and map-trace. Standard library only, no side effects, so a hook can import it cheaply.

Why 1.17.3: on Windows with Git Bash installed, Claude Code's PowerShell tool is on by default for claude.ai accounts and is the primary
shell, so a map build there arrives as a `PowerShell` call (`& "C:\\...\\python.exe" "C:\\...\\scripts\\build_map.py" --spec ...`), not as
`python3 "$GMAP_ROOT/scripts/build_map.py"` in Bash. The 1.17.2 hooks and map-trace read Bash only and saw none of those builds.
"""
from __future__ import annotations

import re

# The tools whose input is a shell command line: the shell tools, and the device shell on a linked computer.
SHELL_TOOLS = ("Bash", "PowerShell")

# A backslash between two path characters is a Windows path separator (PowerShell, or a Windows path quoted in Git Bash); bash escapes such
# as \$ or \" never sit between two of them. Only the copy the readers parse is changed, never the command.
_WINSEP = re.compile(r"(?<=[\w.:~)-])\\(?=[\w.~$-])")

# The interpreter in front of a script: a quoted path naming python, python / python3 / python3.12 (with .exe), the py launcher, or a
# variable holding the interpreter ($py, ${PY}, $GMAP_PY), after PowerShell's call operator & when there is one.
INTERP = r"""(?:&\s*)?(?:"[^"]*python[^"]*"|'[^']*python[^']*'|[^\s"';&|()]*python3?(?:\.\d+)?(?:\.exe)?|py(?:\.exe)?(?:\s+-3(?:\.\d+)?)?|\$\{?[A-Za-z_][\w:]*\}?)"""
RUN = re.compile(r"""(?:^|[\s;&|(])""" + INTERP + r"""(?:\s+-[A-Za-z]+)*\s+["']?([^\s"';&|]*?)(?:^|/)(scripts|lints)/([a-z0-9_]+)\.py""", re.I)
INLINE_PY = re.compile(r"""(?:python3?(?:\.\d+)?(?:\.exe)?["']?|py(?:\.exe)?(?:\s+-3(?:\.\d+)?)?)(?:\s+-[A-Za-z]+)*\s+(?:-\s*<<|-c\b)""", re.I)


def is_shell_tool(tool: str) -> bool:
    return tool in SHELL_TOOLS or tool.endswith("device_bash")


def normalized(command: str) -> str:
    """The command with Windows path separators turned into forward slashes, for matching only."""
    return _WINSEP.sub("/", command)


def command_of(tool: str, tool_input) -> str:
    """The normalized command line of a shell tool call; '' for any other tool."""
    if not is_shell_tool(str(tool or "")) or not isinstance(tool_input, dict):
        return ""
    return normalized(str(tool_input.get("command") or ""))


def runs(command: str, names: set[str] | frozenset[str] | dict) -> list[str]:
    """This plugin's scripts or lints (by name, in order) that the command RUNS with a Python interpreter. A `skills/<slug>/scripts/` path
    is another plugin's and never counts. `command` should already be normalized()."""
    out: list[str] = []
    for m in RUN.finditer(command):
        name, before = m.group(3), (m.group(1) or "") + "/" + m.group(2)
        if name in names and not re.search(r"skills/[^/\s\"']+/" + re.escape(m.group(2)), before, re.I) and name not in out:
            out.append(name)
    return out
