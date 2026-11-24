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


# ------------------------------------------------------------------------------------------------------------- 3.4.0 (F-2): the folder in effect
# `cd day21 && python3 build_map.py --out out` runs in day21, but the transcript records the session's starting folder as the call's cwd, so
# `--out out` resolved against it pointed at a folder that never existed. These read the leading folder changes of a command line.
_SEP = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")
_CD = re.compile(r"""^\s*(?:cd|chdir|pushd|Set-Location|Push-Location|sl)(?:\s+/d)?(?:\s+-(?:Literal)?Path)?\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))\s*$""", re.I)
_GIT_C = re.compile(r"""\bgit\s+-C\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))""")


def _expand(raw: str, base: str | None) -> str:
    import os
    import posixpath
    s = raw.replace("\\", "/")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    s = re.sub(r"^(?:~|\$HOME|\$\{HOME\}|\$env:USERPROFILE)(?=/|$)", home.replace("\\", "/"), s)
    if re.match(r"^(?:/|[A-Za-z]:/)", s) or not base:
        return posixpath.normpath(s) if s.startswith("/") else s
    return posixpath.normpath(posixpath.join(str(base).replace("\\", "/"), s))


def _segments(command: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for m in _SEP.finditer(command):
        out.append((pos, command[pos:m.start()]))
        pos = m.end()
    out.append((pos, command[pos:]))
    return out


def effective_dir(command: str, cwd: str | None, upto: int | None = None) -> str | None:
    """The folder a command line is in at character `upto` (default: its end), after its leading `cd DIR &&`, `cd DIR;`, `pushd DIR`,
    `Set-Location DIR` or `cd /d DIR` changes, relative ones taken from `cwd`. `command` should already be normalized()."""
    d = cwd
    for pos, seg in _segments(command):
        if upto is not None and pos > upto:
            break
        m = _CD.match(seg)
        if m:
            d = _expand(next(g for g in m.groups() if g), d)
    return d


def folders_named(command: str, cwd: str | None) -> list[str]:
    """Every folder a command changes to or names with `git -C`: where a script's own ledger may be."""
    out, d = [], cwd
    for _pos, seg in _segments(command):
        m = _CD.match(seg)
        if m:
            d = _expand(next(g for g in m.groups() if g), d)
            out.append(d)
        for g in _GIT_C.finditer(seg):
            out.append(_expand(next(x for x in g.groups() if x), d))
    return list(dict.fromkeys(x for x in out if x))
