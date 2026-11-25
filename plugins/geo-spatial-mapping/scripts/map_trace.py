#!/usr/bin/env python3
"""map_trace: what this session did with the map plugin, where it failed, and where it repeated itself (0.2.1).

    map_trace.py [--transcript FILE | --session ID] [--since 2h] [--until ISO] [--out-dir DIR] [--format text|json] [--no-files] [--strict]
    map_trace.py find                                   list the transcripts this report can read

Reads the session transcript (and any `.precompact-N.jsonl` copy beside it) plus this plugin's pipeline run records, and reports only the
map plugin's part of the session: its skills, its scripts and lints, and its OGC server's tools. Everything else (another plugin, direct
work) is counted and never read. It is the map plugin's own trace: it never reads another plugin's trace files, never writes where another
plugin writes, and prints its own markers (MAP_TRACE_*), so it cannot be mistaken for, or collide with, the acropolisdocs run-trace.

Attribution, per call:
  declared  the Skill tool loading one of this plugin's skills (a load opens a window that later calls fall into);
  path      a command running one of this plugin's scripts or lints with Python (never a `skills/<slug>/scripts/` path, which is another
            plugin's layout; reading a file with grep, sed or cat is not using the plugin);
  import    an inline Python program that puts a scripts/ folder on its path and imports this plugin's modules (a weaker claim);
  server    a tool of this plugin's OGC server (a tool name carrying the plugin's name).

Status: ok, failed, blocked (the harness or a permission refused the call, or a headless session's call needed approval; excluded from the error rate), orphan (no result: the run was
interrupted) or self (this report's own call, which cannot see its own result).

Rework: a retry is the same command again with nothing written between; an iteration loop is the same script on the same target again after
a write, where the earlier run failed; a re-run after edit is the same shape where it did not fail (normal for visual checks). Writes made
through the shell are inferred from the command text and marked so.

Printed: skill, script and tool names, option names (never their values), call numbers, timings, exit codes, MAP-* finding ids, and for each
failure one line of its output, redacted (hosts, keys, tokens) with every path cut to its last part and capped at 160 characters. Never
message text, command text or a full path. Durations come from the plugin's own timing hook when it logged the session (1.17.2:
hooks/map_timing.py, .geo-mapping/traces/hooks/<session>.ndjson, joined by tool_use_id); otherwise from transcript timestamps, as bounds.
The same log marks a build the pre-export hook stopped (a lint-block, never a crash) and counts the credentials reminders.

Files: <out-dir>/map-trace_<session8>_<UTCstamp>.md (report and fix spec) and .json (the events), in .geo-mapping/traces/ by default;
GEO_MAPPING_TRACE_DIR overrides. Exit 0; 2 nothing to trace; 3 with --strict when a map call failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import redact  # noqa: E402
import shell_calls  # noqa: E402
from entry_skills import ENTRY_SKILLS  # noqa: E402

PLUGIN_ROOT = HERE.parent
EVIDENCE_CAP = 160
IDLE_SECONDS = 120
HOTSPOT_LOOPS = 3
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FINDING = re.compile(r"\b(MAP-[A-Z0-9]+-[0-9A-Z]+)\b")
FINDING_JSON = re.compile(r'"id":\s*"(MAP-[A-Z0-9]+-[0-9A-Z]+)",\s*"severity":\s*"(BLOCK|WARN|INFO)"')
FINDING_NEAR = re.compile(r"\b(BLOCK|WARN|INFO)\b[^\n]{0,40}?\b(MAP-[A-Z0-9]+-[0-9A-Z]+)\b|\b(MAP-[A-Z0-9]+-[0-9A-Z]+)\b[^\n]{0,12}?\b(BLOCK|WARN|INFO)\b")
EXIT = re.compile(r"^\s*(?:Error:\s*)?Exit code (\d+)", re.M)
FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>.]+)')
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Refusal|Interrupt))(?::\s*(.*))?$")
OPTION = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
SHELL_WRITE = re.compile(r"(?:^|[;&|\n]\s*)(?:cat|tee|printf|echo)\b[^\n|;]*>{1,2}\s*[^&\s]|>{1,2}\s*[\"']?[\w./~$-]+\.(?:ya?ml|json|geojson|py|md|txt|csv|gpkg|svg|html)\b|\bsed\s+-i\b|\b(?:cp|mv|tee)\b\s")
EXIT_CLASS = {gc.EXIT_USAGE: "usage", gc.EXIT_LINT: "lint-block", gc.EXIT_REFUSED: "refused", gc.EXIT_SERVICE: "service", gc.EXIT_BRAND_OUTSIDE_BUILD: "brand"}
DISPOSITION = {"plugin-moved": "host", "shell-tail": "shell", "inline-script": "operator", "session-script": "operator", "crash": "product", "dependency": "dependency", "lint-block": "input", "usage": "input", "refused": "input", "brand": "input",
               "service": "environment", "network": "environment", "environment": "environment", "timeout": "environment", "failed": "unclassified"}


SESSION_SIDE = ("shell-tail", "inline-script", "session-script")     # 1.21.0: classes whose failure the session's own command set; 4.1.0: + its scripts

READING = {"input": "a gate or the input stopped the run (the spec, the data or the arguments); fix the input, not the plugin.",
           "dependency": "a package or native library is missing; run the map doctor (`doctor.py --fix --all`).",
           "environment": "the machine, network or a permission stopped the call; not a plugin defect.",
           "operator": "a traceback with no frame in this plugin: an inline script of the session's own failed.",
           "shell": "a command after the plugin's script on the same line (grep or findstr with no match, a test) set the exit code; "
                    "the script printed no error. Not a plugin defect.",
           "unclassified": "the output does not say why; reproduce before calling it a defect.",
           "host": "the plugin's folder moved during the session (an update put a new copy beside the old one, ~g2): a command or script that "
                   "named the old folder found nothing. Find the folder in each command (the skills' root finder takes the newest copy); "
                   "a script of the session's own reads it from ~/.geo-mapping/plugin-root.txt (3.4.0), never a pasted path. Not a plugin defect."}
# 2.1.0 (Day 19 trace): reading the plugin's own instructions is not raising a finding; a call of these names no finding
READING_SCRIPTS = {"modules", "map_guide"}
# 2.1.0: programs a shell command runs beside the plugin's script; its time is then the whole command's, not the script's
OTHER_RUN = re.compile(r"""(?:^|[\s;&|(])(?:[^\s"';&|()]*python3?(?:\.\d+)?(?:\.exe)?|py)(?:\s+-[A-Za-z]+)*\s+["']?([^\s"';&|]+\.py)\b|(?:^|[\s;&|(])(ffmpeg|node|npx|Rscript|ogr2ogr|gdalwarp)\b""")
# 1.18.0: Claude Code reports a hook's refusal as "PreToolUse:<Tool> hook error: [<the hook's command>]: <what the hook printed>". On Windows the
# bracket holds the whole command (1.17.3's is long), which filled the evidence line; the hook's own words follow it.
HOOK_ERROR = re.compile(r"^\w+:[\w-]+ hook error: \[.*?\]:\s*")


# 1.18.1: the plugin's own session reports (map-trace, map-token-use). Their calls are counted and timed like any map call, but running one
# again is not a retry or a loop, and the MAP-* ids they print are an earlier report's, not findings of this session's work.
REPORT_SCRIPTS = {"map_trace", "session_report", "map_evaluate"}
# 2.2.0: the map evaluator's own calls (map_evaluate.py, and the map-evaluate pipeline through pipelines.py) are not the session's map work:
# they are counted on their own line (spec I-map-trace 4, as 1.18.1 keeps map-trace from counting itself)
EVALUATION_CALL = re.compile(r"map_evaluate\.py|pipelines\.py[\"']?\s+(?:plan|run|show)\s+map-evaluate\b")
# 2.2.0: the scripts that write finished maps; their results name the files a session made (map-evaluate is offered on them)
OUTPUT_SCRIPTS = {"build_map", "map_export", "webmap", "pipelines", "animate", "events_anim", "heat_anim", "flythrough", "story", "sheet", "external_qa",
                  "atlas", "timeline", "map_restyle", "telecom_maps", "webmap_story", "massing", "section", "profile_chart"}
OUTPUT_PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|/)[^"'\n\r*?<>|]*?\.(?:png|svg|pdf|html|mp4|gif|webm|jpe?g)(?=["'\s,\]}]|$)""", re.I)
SPEC_ARG = re.compile(r"""--spec[=\s]+["']?([^"'\s]+\.ya?ml)""")
# 1.19.0 (map-trace of Day 17): a build is usually one shell line, `env_probe --begin-build && mapspec_lint && build_map`, and 1.18 named the
# call after its first script, so env_probe.py and mapspec_lint.py showed 30 s to 80 s calls that were the build's. The call is named after
# its last script that is not one of these set-up steps; the others still appear in its scripts.
PREAMBLE_SCRIPTS = ("gmap_common", "redact", "env_probe", "mapspec_lint", "modules", "map_guide")
# 1.19.0: a command after the plugin's last script on the same line (`modules.py read x > m.md; wc -l m.md; grep -c y m.md`) sets the call's
# exit code; grep and findstr return 1 for "no match". Such a failure with no error from the plugin is the shell line's, not the script's.
TAIL_CMD = re.compile(r"""(?:\|\||&&|[;|\n])\s*(?:grep|egrep|fgrep|rg|findstr|select-string|sls|wc|head|tail|test|\[|diff|cmp|find|ls|dir|where|which|type|cat|sed|awk|sort|uniq)\b""", re.I)


def is_report(e: dict) -> bool:
    return bool(REPORT_SCRIPTS & set(e.get("scripts") or []))


class TraceError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- the plugin's own names
def plugin_name() -> str:
    try:
        return gc.build_manifest().get("plugin_name") or "geo-spatial-mapping"
    except Exception:  # noqa: BLE001
        return "geo-spatial-mapping"


def _category(desc: str) -> str:
    d = (desc or "").strip()
    for prefix, cat in (("UTILITY SKILL", "utility"), ("UTILITY MODULE", "utility"), ("KNOWLEDGE MODULE", "knowledge"), ("PIPELINE", "pipeline")):
        if d.startswith(prefix):
            return cat
    return "user"


def plugin_inventory(root: Path = PLUGIN_ROOT) -> dict:
    """This plugin's skills and modules, scripts and the page that owns each script (the first user skill whose SKILL.md runs it, else any
    skill or module). 1.0.1: modules/ holds the helpers, knowledge pages and pipeline guides; `modules.py` itself is owned by no page, so a
    module read falls in the window of the skill that asked for it."""
    scripts = {p.stem for d in ("scripts", "lints") for p in (root / d).glob("*.py")}
    skills, owners = {}, {}
    pages = [(sk.parent.name, sk) for sk in sorted((root / "skills").glob("*/SKILL.md")) if sk.parent.name not in ENTRY_SKILLS] + \
            [(md.stem, md) for md in sorted((root / "modules").glob("*.md"))]
    for name, sk in pages:
        text = sk.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^description:\s*(>\s*\n((?:\s+.*\n)+)|\"?(.*)\"?\s*$)", text, re.M)
        desc = " ".join((m.group(2) or m.group(3) or "").split()) if m else ""
        cat = _category(desc)
        skills[name] = cat
        for s in set(re.findall(r"(?:scripts|lints)/([a-z0-9_]+)\.py", text)):
            if s == "modules":
                continue
            if s in scripts and (s not in owners or (cat == "user" and skills.get(owners[s]) != "user")):
                owners[s] = name
    for name in sorted(ENTRY_SKILLS):                 # 1.14.1: the typed entries are skills/<name>/SKILL.md (commands/ was the legacy layout)
        cmd = root / "skills" / name / "SKILL.md"
        if not cmd.is_file():
            continue
        for s in set(re.findall(r"(?:scripts|lints)/([a-z0-9_]+)\.py", cmd.read_text(encoding="utf-8", errors="replace"))):
            if s != "modules":
                owners.setdefault(s, f"/{name}")
    return {"scripts": scripts, "skills": skills, "owners": owners}


# ---------------------------------------------------------------------------------------------------------------- locating the session
def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def is_transcript(p: Path) -> bool:
    return p.suffix == ".jsonl" and ".precompact" not in p.name and not p.name.endswith(".hooks.ndjson")


def transcript_roots() -> list[Path]:
    """Where transcripts are looked for: CLAUDE_CONFIG_DIR or ~/.claude/projects, and 4.0.0 (GSC T-1) the Cowork VM's layout,
    $HOME/mnt/.claude/projects (the session's own folder mounted beside the person's)."""
    roots = [projects_dir()]
    try:
        home = Path.home()
        roots += [home / "mnt" / ".claude" / "projects"]
        roots += sorted(p / ".claude" / "projects" for p in (home / "mnt").iterdir() if p.is_dir() and (p / ".claude" / "projects").is_dir())
    except (OSError, RuntimeError):
        pass
    return list(dict.fromkeys(roots))


def candidates() -> list[Path]:
    found = []
    for root in transcript_roots():
        if root.is_dir():
            found += [p for p in root.glob("*/*.jsonl") if is_transcript(p)]
    return sorted(dict.fromkeys(found), key=lambda p: p.stat().st_mtime, reverse=True)


def locate(transcript: str | None = None, session: str | None = None) -> tuple[Path, str]:
    """(transcript, how it was found). Order: --transcript, CLAUDE_TRANSCRIPT, --session, CLAUDE_CODE_SESSION_ID, a session id in a
    scratchpad path, the only transcript there is, else the newest one (a guess, reported as NEWEST-FALLBACK)."""
    if transcript:
        p = Path(transcript)
        if not p.is_file():
            raise TraceError(f"no transcript at {p.name}")
        return p, "given"
    env = os.environ.get("CLAUDE_TRANSCRIPT")
    if env and Path(env).is_file():
        return Path(env), "CLAUDE_TRANSCRIPT"
    cands = candidates()
    ids = [(session, "session-id:given")] if session else []
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        ids.append((os.environ["CLAUDE_CODE_SESSION_ID"], "session-id:env"))
    for v in (os.environ.get("TMPDIR", ""), os.getcwd(), *[x for k, x in os.environ.items() if "SCRATCH" in k.upper()]):
        if "scratchpad" in v:
            ids += [(u, "session-id:scratchpad") for u in UUID.findall(v)]
    for sid, how in ids:
        hits = [p for p in cands if p.stem == sid]
        if hits:
            return hits[0], how
    if session:
        raise TraceError(f"no transcript for session {session}")
    if not cands:
        raise TraceError("No session transcript was found on this machine (searched: " + "; ".join(str(r) for r in transcript_roots())
                         + "); pass --transcript FILE")
    return cands[0], ("only-candidate" if len(cands) == 1 else "NEWEST-FALLBACK")


# ---------------------------------------------------------------------------------------------------------------- reading
def parse_ts(s) -> dt.datetime | None:
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) and "text" in c else c.get("content") if isinstance(c, dict) else c) for c in content)
    return ""


def scrubbed_record(line: str) -> dict | None:
    """One transcript line as a record, every secret pattern scrubbed first (credentials spec §7.2); see session_report.scrubbed_record."""
    try:
        d = json.loads(redact.scrub(line)[0])
    except json.JSONDecodeError:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        d = redact.scrub_obj(d)[0]
    return d if isinstance(d, dict) else None


def read_records(path: Path) -> tuple[list[dict], list[str]]:
    """Records of the transcript and of any pre-compaction copy beside it, de-duplicated (a compacted transcript keeps less than it ran)."""
    files = sorted(path.parent.glob(f"{path.stem}.precompact-*.jsonl")) + [path]
    seen, out = set(), []
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                d = scrubbed_record(line)                  # 0.6.0: secrets scrubbed before anything reads the line
                if d is None:
                    continue
                key = d.get("uuid") or hashlib.sha1(line.encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
    out.sort(key=lambda d: d.get("timestamp") or "")
    return out, [f.name for f in files]


def run_shape(records: list[dict], since: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """1.18.0: the assistant turns by model and by effort level, each with its tool calls, and how many turns reasoned. One turn is one
    assistant message (its blocks are written as several records). Read from the transcript's own fields; nothing is estimated."""
    turns: dict[str, dict] = {}
    for d in records:
        if d.get("type") != "assistant" or not isinstance(d.get("message"), dict):
            continue
        ts = parse_ts(d.get("timestamp"))
        if (since and ts and ts < since) or (until and ts and ts > until):
            continue
        m = d["message"]
        key = str(m.get("id") or d.get("requestId") or d.get("uuid"))
        t = turns.setdefault(key, {"model": None, "effort": None, "calls": 0, "reasoned": False})
        t["model"] = t["model"] or m.get("model") or d.get("requestedModel")
        eff = d.get("effort") if isinstance(d.get("effort"), str) else None
        t["effort"] = t["effort"] or eff
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                t["calls"] += 1
            elif isinstance(c, dict) and c.get("type") in ("thinking", "redacted_thinking"):
                t["reasoned"] = True
        if d.get("thinkingDurationMs"):
            t["reasoned"] = True
    models: dict[str, dict] = {}
    efforts: dict[str, dict] = {}
    for t in turns.values():
        if t["model"] in (None, "<synthetic>"):
            continue
        for bucket, k in ((models, t["model"]), (efforts, t["effort"])):
            if k:
                b = bucket.setdefault(k, {"turns": 0, "calls": 0})
                b["turns"] += 1
                b["calls"] += t["calls"]
    counted = [t for t in turns.values() if t["model"] not in (None, "<synthetic>")]
    return {"turns": len(counted), "reasoning_turns": sum(1 for t in counted if t["reasoned"]), "models": models, "efforts": efforts}


def calls_of(records: list[dict]) -> tuple[list[dict], list[dt.datetime], int]:
    """Tool calls in order with their results, every record time, and the number of user turns."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    times: list[dt.datetime] = []
    turns = 0
    for d in records:
        ts = parse_ts(d.get("timestamp"))
        if ts:
            times.append(ts)
        msg = d.get("message") if isinstance(d.get("message"), dict) else None
        if not msg:
            continue
        if d.get("type") == "assistant":
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id") not in calls:
                    calls[c["id"]] = {"id": c["id"], "tool": c.get("name") or "?", "input": c.get("input") if isinstance(c.get("input"), dict) else {},
                                      "start": ts, "end": None, "result": None, "is_error": False, "cwd": d.get("cwd")}
                    order.append(c["id"])
        elif d.get("type") == "user":
            content = msg.get("content")
            got = False
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                        k = calls[c["tool_use_id"]]
                        k.update(end=ts, result=_text(c.get("content"))[:40000], is_error=bool(c.get("is_error")))
                        got = True
            if not got and (isinstance(content, str) or isinstance(content, list)) and not d.get("isMeta"):
                turns += 1
    return [calls[i] for i in order], times, turns


# ---------------------------------------------------------------------------------------------------------------- classifying
def _command(call: dict) -> str:
    """The command line of a Bash, PowerShell (1.17.3: Claude Code's primary shell on Windows) or device-shell call, with Windows path
    separators read as forward slashes; '' for any other tool."""
    return shell_calls.command_of(call["tool"], call["input"])


IMPORT = re.compile(r"^\s*(?:import|from)\s+([a-z0-9_]+)", re.M)


def map_scripts(cmd: str, inv: dict) -> tuple[list[str], str | None]:
    """This plugin's scripts or lints the command RUNS (a Python interpreter on the file), or imports from an inline Python program that
    puts a scripts/ or lints/ folder on its path. Reading a file (grep, sed, cat) is not using the plugin. Returns (names, how)."""
    out = shell_calls.runs(cmd, inv["scripts"])
    if out:
        return out, "path"
    if shell_calls.INLINE_PY.search(cmd) and re.search(r"""(?:^|[/\s"'])(?:scripts|lints)["'/]""", cmd) and not re.search(r"skills/[^/\s\"']+/scripts", cmd):
        mods = [m for m in IMPORT.findall(cmd) if m in inv["scripts"] and m not in ("gmap_common",)]
        if mods:
            return list(dict.fromkeys(mods)), "import"
    return [], None


def _op_after(cmd: str, script: str) -> str | None:
    m = re.search(re.escape(script) + r"\.py[\"']?\s+([a-z][a-z0-9_-]{1,24})\b(?!\s*=)", cmd)
    return m.group(1) if m else None


def _target(cmd: str) -> str:
    files = re.findall(r"[\w./-]+\.(?:ya?ml|json|geojson|gpkg|svg)\b", cmd)
    return hashlib.sha1(" ".join(sorted(set(Path(f).name for f in files))).encode()).hexdigest()[:10] if files else "-"


# 3.4.0 (F-3): a Python file the session wrote that imports the plugin's modules is map work (the Antakya render.py: events_anim, timeline,
# flythrough, map_brand, output_lint). Read at trace time from the disk, or from the Write that made it when the file is gone.
PY_FILE_RUN = re.compile(r"""(?:^|[\s;&|(])(?:&\s*)?(?:"[^"]*python[^"]*"|[^\s"';&|()]*python3?(?:\.\d+)?(?:\.exe)?|py(?:\.exe)?)(?:\s+-[A-Za-z]+)*\s+["']?([^\s"';&|]+\.py)\b""", re.I)
HEREDOC_PY = re.compile(r"""\bcat\s+>\s*["']?([^\s"'<>]+\.py)["']?\s*<<-?\s*["']?(\w+)["']?\n(.*?)\n\2\b""", re.S)
PLUGIN_PATH_HINT = re.compile(r"""scripts|GMAP_ROOT|geo-mapping|geo-spatial-mapping|lints""")


def _note_written(call: dict, written: dict[str, str]) -> None:
    """Python files the session wrote (Write, Edit, a heredoc), by absolute path and by name -> their text as written ('' for an Edit)."""
    tool, inp = call["tool"], call["input"] or {}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if fp.endswith(".py"):
            written[fp.replace("\\", "/")] = str(inp.get("content") or written.get(fp.replace("\\", "/"), ""))
            written.setdefault("name:" + Path(fp.replace("\\", "/")).name, fp.replace("\\", "/"))
        return
    cmd = _command(call)
    for m in HEREDOC_PY.finditer(cmd):
        base = shell_calls.effective_dir(cmd, call.get("cwd"), m.start())
        fp = m.group(1) if m.group(1).startswith("/") or not base else f"{base}/{m.group(1)}"
        written[fp] = m.group(3)
        written.setdefault("name:" + Path(fp).name, fp)


def _file_imports(cmd: str, call: dict, written: dict[str, str], inv: dict) -> list[str]:
    mods: list[str] = []
    for m in PY_FILE_RUN.finditer(cmd):
        raw = m.group(1)
        if Path(raw).name in {f"{x}.py" for x in inv["scripts"]}:
            continue
        base = shell_calls.effective_dir(cmd, call.get("cwd"), m.start())
        fp = raw if raw.startswith("/") or re.match(r"[A-Za-z]:/", raw) or not base else f"{base}/{raw}"
        key = fp if fp in written else written.get("name:" + Path(raw).name)
        if not key:
            continue
        text = ""
        try:
            text = Path(key).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = written.get(key) or ""
        if not PLUGIN_PATH_HINT.search(text):
            continue
        mods += [x for x in IMPORT.findall(text) if x in inv["scripts"] and x not in ("gmap_common",)]
    return list(dict.fromkeys(mods))


def call_folders(calls: list[dict]) -> list[str]:
    """3.4.0 (F-2): every folder a shell call changed to, and the parent of every --out folder it named: where a script's own ledger is."""
    out: list[str] = []
    for c in calls:
        cmd = _command(c)
        if not cmd:
            continue
        fs = shell_calls.folders_named(cmd, c.get("cwd"))
        out += fs
        om = OUT_ARG.search(cmd)
        if om:
            base = shell_calls.effective_dir(cmd, c.get("cwd"), om.start()) or c.get("cwd")
            if base:
                o = _resolve(next(g for g in om.groups() if g), Path(base))
                out += [str(o.parent), str(o)]
    return list(dict.fromkeys(out))


def classify(call: dict, inv: dict, pname: str, window: str | None, written: dict | None = None) -> dict:
    """Map lane, basis, scripts, op, options and the keys rework detection uses, or lane None for a call that is not the map plugin's."""
    tool, inp = call["tool"], call["input"]
    ev = {"lane": None, "basis": None, "scripts": [], "op": None, "options": [], "mcp_tool": None, "write": None, "key": None, "target": None}
    if tool == "Skill":
        sk = str(inp.get("skill") or "")
        base, _, short = sk.rpartition(":")
        if (base.startswith("geo-spatial-mapping") or base == pname) or (not base and short in inv["skills"]):
            ev.update(lane=short or sk, basis="declared", opens=short or sk)
        else:
            ev["closes"] = True
        return ev
    if "geo-spatial-mapping" in tool and "__" in tool:
        ev.update(lane="ogc-server", basis="server", mcp_tool=tool.rsplit("__", 1)[-1])
        ev["key"] = hashlib.sha1((tool + json.dumps(inp, sort_keys=True)).encode()).hexdigest()[:12]
        ev["target"] = (ev["mcp_tool"], "-")
        return ev
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        ev["write"] = "observed"
        return ev
    cmd = _command(call)
    if not cmd:
        return ev
    if SHELL_WRITE.search(cmd):
        ev["write"] = "inferred"
    found, how = map_scripts(cmd, inv)
    if not found and written:
        found = _file_imports(cmd, call, written, inv)
        how = "import-file" if found else None
    if not found:
        return ev
    main = next((s for s in reversed(found) if s not in PREAMBLE_SCRIPTS), None) or next((s for s in found if s not in ("gmap_common", "redact")), found[0])
    found = [main] + [s for s in found if s != main]
    last_run = max((m.end() for m in shell_calls.RUN.finditer(cmd)), default=0) if how == "path" else 0
    ev["tail"] = bool(how == "path" and TAIL_CMD.search(cmd[last_run:]))
    plugin_py = {f"{s}.py" for s in inv["scripts"]}
    others = [m.group(1) or m.group(2) for m in OTHER_RUN.finditer(shell_calls.normalized(cmd))]
    others = [Path(o).name for o in others if o and Path(o).name not in plugin_py]
    if others:
        ev["shared"] = list(dict.fromkeys(others))[:4]       # 2.1.0 (Day 19 trace): the call's time is the whole command's
    owner = inv["owners"].get(main)
    lane = window if (window and window in inv["skills"] and owner in (None, window)) else (owner or f"({main})")
    if how in ("import", "import-file"):
        # 4.1.0 (Day 24, T-01): the session's own script that imports the plugin's modules (render_cells.py importing events_anim) is the
        # session's work: its failures and its time are its own, not a crash or a slow call of the module it imports
        ev["session_script"] = True
    ev.update(lane=lane, basis=how if lane != window else "window", scripts=found, op=_op_after(cmd, main) if how == "path" else None,
              options=sorted(set(OPTION.findall(cmd)))[:12], key=hashlib.sha1(cmd.encode()).hexdigest()[:12], target=(main, ev.get("op") or _op_after(cmd, main) or "-", _target(cmd)))
    return ev


def json_result(t: str | None) -> dict | None:
    """2.4.0 (X-01): an MCP tool's result is a JSON object; {"ok": false, ...} is a failed call even though the tool call itself returned."""
    s = (t or "").strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except ValueError:
        m = re.match(r'\{\s*"ok"\s*:\s*(true|false)', s)
        return {"ok": m.group(1) == "true", "_partial": True} if m else None
    return d if isinstance(d, dict) else None


JSON_KIND = {"lint": "lint-block", "usage": "usage", "input": "usage", "refused": "refused", "service": "service", "network": "network",
             "timeout": "timeout", "credential": "service", "environment": "environment", "brand": "brand",
             "runtime": "crash"}          # 4.1.0 (Day 24, T-02): MAP-SVC-013 is an exception inside the data server (KeyError: 'mcc'): the plugin's


def status_of(call: dict, is_last_map_trace: bool) -> str:
    if call["result"] is None:
        return "self" if is_last_map_trace else "orphan"
    t = call["result"]
    jr = json_result(t)
    if jr is not None and jr.get("ok") is False:
        return "failed"
    if (t.lstrip().startswith("<tool_use_error>") or "Permission for this action was denied" in t or "permission rule" in t.lower() and "denied" in t.lower()
            or t.strip().startswith("This command requires approval")):              # 1.17.2: a headless session's unapproved call
        return "blocked"
    if call["is_error"] or EXIT.search(t):
        code = EXIT.search(t)
        if code and int(code.group(1)) == 0:
            return "ok"
        return "failed"
    return "ok"


def _short_paths(s: str) -> str:
    def cut(m):
        p = m.group(0)
        if "/scripts/" in p or "/lints/" in p:
            return "<plugin>/" + "/".join(p.split("/")[-2:])
        return ".../" + p.rstrip("/").split("/")[-1]
    return re.sub(r"(?:[A-Za-z]:)?(?:/|\\\\)[^\s\"':,()\]]+(?:/|\\\\)[^\s\"':,()\]]+", cut, s)


def evidence(text: str) -> str:
    """One line of a failed result: the exception line, else the first line naming an error or a finding, else the first line. Redacted,
    every path cut to its last part, capped."""
    lines = [HOOK_ERROR.sub("", ln.strip()) for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln and not EXIT.match(ln)]
    stop = next((ln for ln in lines if HOOK_STOP in ln), None)
    if stop:                                                     # the pre-export check's own line, from where it starts
        lines = [stop[stop.index(HOOK_STOP):]]
    pick = next((ln for ln in reversed(lines) if EXC_LINE.match(ln)), None) \
        or next((ln for ln in lines if re.search(r"error|refused|BLOCK|not found|denied|failed|cannot|unable", ln, re.I)), None) \
        or (lines[0] if lines else "")
    pick, _ = redact.scrub(pick)
    pick = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", "<url>", pick, flags=re.I)        # no host name or query ever prints
    pick = _short_paths(pick)
    return pick if len(pick) <= EVIDENCE_CAP else pick[:EVIDENCE_CAP - 1] + "…"


def plugin_moved(text: str) -> bool:
    """A path in `text` inside a folder named for this plugin (or a ~N copy of it) that is not there now, while this plugin runs from another
    folder: the plugin moved under the session (2.1.0, Day 19 trace)."""
    name = plugin_name()
    for m in re.finditer(r"""['"]?((?:[A-Za-z]:)?[/\\][^\s'"]*?""" + re.escape(name) + r"""[^/\\\s'"]*[/\\][^\s'":]+)""", text or ""):
        p = Path(m.group(1).rstrip(".,;)"))
        try:
            if not p.exists() and PLUGIN_ROOT.resolve() not in p.resolve().parents:
                return True
        except OSError:
            continue
    return False


def error_class(text: str) -> tuple[str, str | None, str | None]:
    """(class, crash site inside this plugin or None, exception name or None)."""
    t = text or ""
    code = EXIT.search(t)
    frames = FRAME.findall(t)
    site = None
    for f, line, fn in reversed(frames):
        g = f.replace("\\", "/")
        if re.search(r"/(scripts|lints|mcp)/[a-z0-9_]+\.py$", g) and Path(g).stem in plugin_inventory_cache()["scripts"] | {"ogc_server", "ssrf"}:
            site = f"{g.split('/')[-2]}/{g.split('/')[-1]}:{line} ({fn})"
            break
    exc = None
    for ln in reversed([x.strip() for x in t.splitlines() if x.strip()]):
        m = EXC_LINE.match(ln)
        if m:
            exc = m.group(1)
            break
    low = t.lower()
    jr = json_result(t)
    if jr is not None and jr.get("ok") is False:          # 2.4.0 (X-01): the class comes from the result's own error_kind
        k = str(jr.get("error_kind") or "lint")
        if k == "runtime":                                  # 4.1.0 (T-02): the exception's name from the message ("KeyError: 'mcc'")
            msg = jr.get("message") or next((f.get("message") for f in (jr.get("findings") or []) if isinstance(f, dict)), "") or ""
            m = re.match(r"\s*([A-Za-z_]\w*(?:Error|Exception))\b", str(msg))
            return "crash", site or "mcp/ogc_server.py (data server)", exc or (m.group(1) if m else None)
        return JSON_KIND.get(k, "lint-block"), site, exc
    if HOOK_STOP in t:                                     # 1.17.2: the plugin's own pre-export hook stopped the build before it ran
        return "lint-block", None, None
    if ("no such file or directory" in low or "can't open file" in low) and plugin_moved(t):
        return "plugin-moved", None, exc                   # 2.1.0 (Day 19 trace #117): not a missing package
    if exc in ("ModuleNotFoundError", "ImportError") or "cannot load library" in low or "no module named" in low:
        return "dependency", site, exc
    if "connecterror" in low or "connection refused" in low or "name or service not known" in low or "map-svc-010" in low:
        return "network", site, exc
    if "timed out" in low or "did not complete within" in low or "timeoutexpired" in low:
        return "timeout", site, exc
    if "permission denied" in low or "operation not permitted" in low or "read-only file system" in low:
        return "environment", site, exc
    if frames and exc and exc not in ("SystemExit",):
        if exc in ("PlanError",) and "blocking problems" in t:
            return "lint-block", site, exc
        return "crash", site, exc
    if code and int(code.group(1)) in EXIT_CLASS:
        return EXIT_CLASS[int(code.group(1))], site, exc
    if "blocking problems" in t or '"blocked": true' in t:
        return "lint-block", site, exc
    return "failed", site, exc


_INV = None


def plugin_inventory_cache() -> dict:
    global _INV
    if _INV is None:
        _INV = plugin_inventory()
    return _INV


_SEV = None
SEV_IN_CODE = re.compile(r"""\b(?:finding|f|_f|fnd|add|mk)\(\s*["'](MAP-[A-Z0-9]+-[0-9A-Z]+)["']\s*,\s*["'](BLOCK|WARN|INFO)["']""")


def severities_in_code(root: Path = PLUGIN_ROOT) -> dict[str, str]:
    """1.19.0 (map-trace of Day 17): the severity each MAP-* id is raised with in this plugin's own code, the highest when an id is raised at
    more than one. A result that names an id without its severity (a skill's summary line, a PlanError) then shows it, not "?"."""
    global _SEV
    if _SEV is not None:
        return _SEV
    rank = {"INFO": 0, "WARN": 1, "BLOCK": 2}
    out: dict[str, str] = {}
    for d in ("scripts", "lints", "mcp"):
        for p in (root / d).glob("*.py"):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            for fid, sev in SEV_IN_CODE.findall(text):
                if rank[sev] > rank.get(out.get(fid, ""), -1):
                    out[fid] = sev
    _SEV = out
    return out


def findings_in(text: str) -> dict[str, str]:
    """MAP-* ids a result names, with the severity printed beside them when there is one."""
    out: dict[str, str] = {}
    rank = {"INFO": 0, "WARN": 1, "BLOCK": 2, "?": -1}
    for fid, sev in FINDING_JSON.findall(text or ""):
        out[fid] = sev                 # 2.4.0 (X-05): the finding object the build printed last wins (after its own downgrades), not the highest
    from_json = set(out)
    for a, b, c, d in FINDING_NEAR.findall(text or ""):
        fid, sev = (b, a) if b else (c, d)
        if fid not in from_json and rank[sev] > rank.get(out.get(fid, "?"), -1):
            out[fid] = sev
    for fid in FINDING.findall(text or ""):
        # 1.17.2: the pre-export check lists blocking findings only; 1.19.0: else the severity the plugin's code raises the id with
        out.setdefault(fid, "BLOCK" if HOOK_STOP in (text or "") else (severities_in_code().get(fid) or "?"))
    return out


# ---------------------------------------------------------------------------------------------------------------- the plugin's hook log
HOOK_STOP = "geo-mapping pre-export check"


def hook_log_dirs(records: list[dict]) -> list[Path]:
    """Where the plugin's hooks may have logged this session (hooks/hook_log.py): GEO_MAPPING_HOME, this process's working folder, the
    .geo-mapping/ of every folder the session ran in, and the home folder's."""
    dirs: list[Path] = []
    if os.environ.get("GEO_MAPPING_HOME"):
        dirs.append(Path(os.environ["GEO_MAPPING_HOME"]))
    dirs.append(gc.workdir())
    for cwd in dict.fromkeys(str(d.get("cwd")) for d in records if d.get("cwd")):
        dirs.append(Path(cwd) / gc.WORKDIR_NAME)
    dirs.append(Path.home() / gc.WORKDIR_NAME)
    out = []
    for d in dirs:
        p = d / "traces" / "hooks"
        if p not in out:
            out.append(p)
    return out


def read_hook_log(session: str, records: list[dict]) -> tuple[list[dict], list[str]]:
    """The hook log lines for one session, from every place it may be, without duplicates. (lines, files read)."""
    lines, seen, files = [], set(), []
    for d in hook_log_dirs(records):
        f = d / f"{session}.ndjson"
        try:
            if not f.is_file():
                continue
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if raw in seen:
                    continue
                seen.add(raw)
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("t"), (int, float)):
                    lines.append(rec)
            files.append(str(f))
        except OSError:
            continue
    return sorted(lines, key=lambda r: r["t"]), files


def _ms_time(ms) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def apply_hook_log(events: list[dict], log: list[dict]) -> dict:
    """Measured times, failures and stops from the hook log, onto the map events (joined by the call's tool_use_id)."""
    by_id: dict = {}
    for r in log:
        if r.get("id"):
            by_id.setdefault(str(r["id"]), {})[r.get("e")] = r
    measured = 0
    for e in events:
        if not e["lane"]:
            continue
        got = by_id.get(e.get("_id") or "")
        if not got:
            continue
        start, end = got.get("start"), got.get("end") or got.get("fail")
        if start and end:
            ms = end.get("ms") if isinstance(end.get("ms"), (int, float)) else end["t"] - start["t"]
            e["_start"] = _ms_time(start["t"])
            e["_end"] = _ms_time(start["t"] + ms)
            e["seconds"] = round(ms / 1000, 1)
            e["measured"] = True
            measured += 1
        if "fail" in got and e["status"] == "orphan":
            e["status"] = "failed"
            e.setdefault("error_class", "failed")
            e.setdefault("exit_code", got["fail"].get("exit"))
        elif "end" in got and e["status"] == "orphan":
            e["status"] = "ok"
        if "stop" in got:
            e["status"] = "failed"
            e["stopped_by"] = "pre-export hook"
            e["error_class"] = "lint-block"
            for fid in got["stop"].get("findings") or []:
                e.setdefault("findings", {})[fid] = "BLOCK"
    return {"measured": measured}


# ---------------------------------------------------------------------------------------------------------------- the trace
# ---------------------------------------------------------------------------------------------------------------- 2.4.0: the run ledger
LEDGER_SLACK_S = 5.0
DEGRADED_TEXT = re.compile(r"\brelief missing\b|\"degraded\":\s*\[\s*\"", re.I)


def ledger_dirs(records: list[dict], extra: list[str] | None = None) -> list[Path]:
    """Where the plugin's run ledger (gmap_common.ledger_note, X-02) may be: this process's own, the person's one session ledger (3.4.0,
    F-2), and .geo-mapping/traces/runs under every folder the session's calls ran from, every folder a command changed to with `cd` and every
    `--out` folder's parent (`extra`)."""
    out: list[Path] = []
    for f in (gc.ledger_dir, gc.session_ledger_dir):
        try:
            out.append(f())
        except Exception:  # noqa: BLE001
            pass
    for c in dict.fromkeys([str(d.get("cwd")) for d in records if d.get("cwd")] + list(extra or [])):
        out.append(Path(c) / gc.WORKDIR_NAME / "traces" / "runs")
    seen, keep = set(), []
    for d in out:
        try:
            k = str(d.resolve())
        except OSError:
            continue
        if k not in seen and d.is_dir():
            seen.add(k)
            keep.append(d)
    return keep


def read_ledger(records: list[dict], lo: dt.datetime | None = None, hi: dt.datetime | None = None, extra: list[str] | None = None) -> tuple[list[dict], list[str]]:
    rows, files, seen = [], [], set()
    for d in ledger_dirs(records, extra):
        for f in sorted(d.glob("*.ndjson")):
            files.append(str(f))
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                st = parse_ts(r.get("start"))
                if not st or (lo and st < lo - dt.timedelta(minutes=1)) or (hi and st > hi + dt.timedelta(minutes=1)):
                    continue
                key = (r.get("script"), r.get("start"), r.get("pid"))      # 3.4.0: one line is in the script's ledger and the session's
                if key in seen:
                    continue
                seen.add(key)
                r["_start"], r["_end"] = st, parse_ts(r.get("end")) or st
                rows.append(r)
    return rows, files


def adopt_ledger_rows(events: list[dict], rows: list[dict], commands: dict) -> int:
    """4.0.0 (GSC T-2): a ledger line no map call took (the call's command named the script by a path the trace did not read as the plugin's,
    such as /sessions/<id>/mnt/... in a Cowork VM, or through a wrapper) goes to the shell call nearest in time whose command names that
    script's file; that call becomes map work. -> how many calls were adopted this way."""
    taken = {id(r) for e in events for r in (e.get("_ledger_rows") or [])}
    slack = dt.timedelta(seconds=LEDGER_SLACK_S * 4)
    n = 0
    for r in rows:
        if id(r) in taken or not r.get("script") or not r.get("_start"):
            continue
        fname = f"{r['script']}.py"
        near = [e for e in events if not e.get("lane") and e.get("_start") and fname in (commands.get(e.get("_id")) or "")
                and e["_start"] - slack <= r["_start"] <= (e.get("_end") or e["_start"]) + slack]
        if not near:
            continue
        e = min(near, key=lambda x: abs((x["_start"] - r["_start"]).total_seconds()))
        e.update(lane=f"({r['script']})", basis="ledger", scripts=[r["script"]])
        e.setdefault("_ledger_rows", []).append(r)
        e["ledger"] = (e.get("ledger") or []) + [{k: r.get(k) for k in ("script", "exit", "seconds", "degraded") if r.get(k) is not None}]
        e["_ledger_outputs"] = (e.get("_ledger_outputs") or []) + list(r.get("outputs") or [])
        e["_ledger_specs"] = (e.get("_ledger_specs") or []) + ([r["spec"]] if r.get("spec") else [])
        e["_ledger_outs"] = (e.get("_ledger_outs") or []) + ([r["out"]] if r.get("out") else [])
        taken.add(id(r))
        n += 1
    return n


def apply_ledger(events: list[dict], rows: list[dict]) -> int:
    """Join each map call to the ledger lines its scripts wrote (same script, started inside the call's window). A line with a non-zero exit
    marks the call failed whatever the shell printed (`| tail`, `> file`, `2>&1`); a line with dropped items marks it degraded (X-04).
    -> how many calls the ledger matched."""
    used: set = set()
    n = 0
    slack = dt.timedelta(seconds=LEDGER_SLACK_S)
    for e in events:
        if not e.get("lane") or not e.get("scripts") or not e.get("_start"):
            continue
        hi = (e.get("_end") or e["_start"]) + slack
        mine = [i for i, r in enumerate(rows) if i not in used and r.get("script") in e["scripts"] and e["_start"] - slack <= r["_start"] <= hi]
        if not mine:
            continue
        n += 1
        used |= set(mine)
        mrows = [rows[i] for i in mine]
        e["_ledger_rows"] = mrows
        e["ledger"] = [{k: r.get(k) for k in ("script", "exit", "seconds", "degraded") if r.get(k) is not None} for r in mrows]
        e["_ledger_outputs"] = [o for r in mrows for o in (r.get("outputs") or [])]
        e["_ledger_specs"] = [r.get("spec") for r in mrows if r.get("spec")]
        e["_ledger_outs"] = [r.get("out") for r in mrows if r.get("out")]
        bad = [r for r in mrows if r.get("exit") not in (0, None)]
        if bad and e["status"] in ("ok", "degraded"):
            r = bad[-1]
            code = int(r["exit"]) if isinstance(r["exit"], int) else 1
            cls = "crash" if r.get("exception") else EXIT_CLASS.get(code, "failed")
            e.update(status="failed", error_class=cls, exit_code=code, exception=r.get("exception"), crash_site=None, via="ledger",
                     evidence=f"{r['script']}.py exited {code}" + (f" ({r['exception']})" if r.get("exception") else "")
                     + "; the shell line hid it, the plugin's run ledger recorded it")
            for fid, sev in (r.get("findings") or {}).items():
                e.setdefault("findings", {})[fid] = sev or "?"
        drop = [d for r in mrows for d in (r.get("degraded") or [])]
        if drop and e["status"] == "ok":
            e.update(status="degraded", degraded=list(dict.fromkeys(drop)))
    return n


# 2.4.0 (X-06): hand-written glue between plugin calls is a signal (spec principle 6): a feature the plugin lacked
GLUE_LIBS = ("geopandas", "h3", "shapely", "rasterio", "pyogrio", "fiona", "duckdb", "pyproj", "osgeo")
HAND_FETCH = re.compile(r"(?:curl|wget|Invoke-WebRequest|requests\.get|httpx\.get|urlopen)[^\n]{0,200}?(ImageServer|exportImage|/WCS|GetCoverage|\.tif\b|/downloadFile/)", re.I)


# 4.1.0 (Day 24, T-03): what the session's own code did, so each line of "work done outside the plugin" names a candidate feature
OPS_SEEN = ((r"voronoi_diagram|voronoi_polygons|Voronoi\(", "voronoi"), (r"\.overlay\(|gpd\.overlay", "overlay"),
            (r"dijkstra|shortest_path|networkx", "network distance"), (r"\+proj=|to_crs\(", "reprojection"),
            (r"segmentize|interpolate\(", "densify"), (r"cKDTree|query_pairs|query_ball", "nearest-neighbour search"),
            (r"sjoin|spatial join", "spatial join"), (r"ImageDraw|alpha_composite|ffmpeg", "frame drawing"),
            (r"CubicSpline|PchipInterpolator|np\.interp", "interpolation through time"), (r"buffer\(", "buffer"),
            (r"union_all|unary_union|linemerge|line_merge", "merge"), (r"\.clip\(|clip_by_rect|intersection\(", "clip"),
            (r"tracert|traceroute|ripe\.net|atlas\.ripe", "traceroute"), (r"read_parquet|read_file", "data read"))


def outside_work(calls: list[dict], events: list[dict], inv: dict) -> list[dict]:
    """The session's own Python that used geodata libraries or the plugin's modules, and hand fetches of data the plugin fetches, between
    plugin calls: prep.py, an inline heredoc, a curl to an ImageServer. -> [{n, what, libraries}] (names only, never the code)."""
    written: dict[str, set] = {}
    written_text: dict[str, list] = {}
    plugin_mods = set(inv.get("scripts") or [])
    out = []
    for c, e in zip(calls, events, strict=False):
        inp = c.get("input") or {}
        if c["tool"] in ("Write", "Edit") and str(inp.get("file_path", "")).endswith(".py"):
            body = str(inp.get("content") or inp.get("new_string") or "")
            libs = {m for m in IMPORT.findall(body) if m in GLUE_LIBS or m in plugin_mods}
            name = Path(str(inp["file_path"])).name
            written.setdefault(name, set()).update(libs)
            written_text.setdefault(name, []).append(body[:200_000])
            continue
        cmd = _command(c)
        if not cmd or (e.get("lane") and e.get("scripts") and not e.get("session_script")):
            continue
        libs: set = set()
        what = None
        if e.get("session_script"):                       # 4.1.0 (T-01): named by its own file, with the plugin modules it imports
            libs |= {s_ for s_ in (e.get("scripts") or []) if s_ in plugin_mods}
        for m in OTHER_RUN.finditer(shell_calls.normalized(cmd)):
            f = m.group(1)
            if f and Path(f).name in written and Path(f).name not in {f"{s}.py" for s in plugin_mods}:
                libs |= written[Path(f).name]
                what = Path(f).name
        if re.search(r"python3?[^\n]*(?:\s-\s*<<|\s-c\s)", cmd):
            inl = {m for m in IMPORT.findall(cmd.replace(";", "\n")) if m in GLUE_LIBS or m in plugin_mods}
            if inl:
                libs |= inl
                what = what or "inline Python"
        hm = HAND_FETCH.search(cmd)
        if hm:
            out.append({"n": e["n"], "what": f"hand fetch ({hm.group(1)})", "libraries": []})
        if e.get("session_script") and not what:
            fm = re.search(r"(?:cat\s*>\s*|python3?\s+)([\w./-]+\.py)", cmd)
            what = Path(fm.group(1)).name if fm else "session script"
        if what and libs:
            body = cmd + " " + " ".join(written_text.get(what, []))
            ops = sorted({lab for rx, lab in OPS_SEEN if re.search(rx, body)})
            out.append({"n": e["n"], "what": what, "libraries": sorted(libs), **({"ops": ops} if ops else {})})
    return out


def window_filter(calls: list[dict], times: list[dt.datetime], since: str | None, until: str | None):
    if not times:
        return calls, times, None
    last = max(times)
    lo = hi = None
    if since:
        m = re.fullmatch(r"(\d+)\s*([smhd])", since.strip())
        if m:
            lo = last - dt.timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
        else:
            lo = parse_ts(since)
        if lo is None:
            raise TraceError(f"--since '{since}' is not 90s, 30m, 2h, 1d or an ISO time")
    if until:
        hi = parse_ts(until)
        if hi is None:
            raise TraceError(f"--until '{until}' is not an ISO time")
    keep = [c for c in calls if c["start"] and (lo is None or c["start"] >= lo) and (hi is None or c["start"] <= hi)]
    t2 = [t for t in times if (lo is None or t >= lo) and (hi is None or t <= hi)]
    return keep, t2, (since or "") + (f" until {until}" if until else "")


def _union(intervals: list[tuple[dt.datetime, dt.datetime]]) -> float:
    tot, cur = 0.0, None
    for a, b in sorted(i for i in intervals if i[0] and i[1] and i[1] >= i[0]):
        if cur is None or a > cur[1]:
            if cur:
                tot += (cur[1] - cur[0]).total_seconds()
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        tot += (cur[1] - cur[0]).total_seconds()
    return tot


def build(path: Path, how: str, *, since: str | None = None, until: str | None = None, idle_seconds: int = IDLE_SECONDS) -> dict:
    inv = plugin_inventory_cache()
    pname = plugin_name()
    records, files = read_records(path)
    calls, times, turns = calls_of(records)
    calls, times, win = window_filter(calls, times, since, until)
    run = run_shape(records, *((min(times), max(times)) if win and times else (None, None)))
    events, window = [], None
    last_trace = max((i for i, c in enumerate(calls) if "map_trace.py" in _command(c)), default=-1)
    written: dict[str, str] = {}
    for n, c in enumerate(calls, 1):
        _note_written(c, written)
        ev = classify(c, inv, pname, window, written)
        if ev.get("opens"):
            window = ev["opens"]
        elif ev.get("closes"):
            window = None
        st = status_of(c, n - 1 == last_trace)
        dur = (c["end"] - c["start"]).total_seconds() if c["start"] and c["end"] else None
        e = {"n": n, "tool": c["tool"] if not c["tool"].startswith("mcp__") or ev["mcp_tool"] else "mcp", "lane": ev["lane"], "basis": ev["basis"],
             "scripts": ev["scripts"], "op": ev["op"], "options": ev["options"], "mcp_tool": ev["mcp_tool"], "write": ev["write"],
             "status": st, "seconds": round(dur, 1) if dur is not None else None, "start": c["start"].isoformat() if c["start"] else None,
             "_key": ev["key"], "_target": ev["target"], "_start": c["start"], "_end": c["end"], "_id": c["id"]}
        if ev.get("shared"):
            e["shared_with"] = ev["shared"]
        if ev.get("session_script"):
            e["session_script"] = True
        if ev["lane"] and EVALUATION_CALL.search(_command(c)):
            e.update(lane=None, evaluation=True)                          # 2.2.0: counted on its own line, never as map work
        if "installing them now" in (c["result"] or "") and "env_probe" in (ev["scripts"] or []):
            e["note"] = "includes the first-run package install"        # 2.1.0 (Day 19 trace): not env_probe's own time
        if ev["lane"]:
            reading = c["tool"] == "Skill" or bool(ev["scripts"] and ev["scripts"][0] in READING_SCRIPTS)
            if reading:
                e["reading"] = True          # 4.2.0 (Day 25 trace): the plugin's own instructions, read; never a build's result
            e["findings"] = {} if is_report(e) or reading else findings_in(c["result"] or "")
            if st == "failed":
                cls, site, exc = error_class(c["result"] or "")
                code = EXIT.search(c["result"] or "")
                if cls == "failed" and ev.get("tail") and code and int(code.group(1)) == 1 and not FINDING.search(c["result"] or ""):
                    cls = "shell-tail"
                if e.get("session_script") and cls in ("crash", "failed", "usage"):
                    cls = "session-script"                                   # 4.1.0 (T-01)
                elif cls == "crash" and not site:
                    # 1.21.0 (Day 18 trace F-1): a traceback with no frame in this plugin is the session's own script (python -c reading the
                    # plugin's output, a JSONDecodeError on an empty line): not a crash of the script the call is named after
                    cls = "inline-script"
                e.update(error_class=cls, crash_site=site, exception=exc, exit_code=int(code.group(1)) if code else None, evidence=evidence(c["result"] or ""))
                if HOOK_STOP in (c["result"] or ""):
                    e.update(stopped_by="pre-export hook", exit_code=2)
            elif st == "blocked":
                e["evidence"] = evidence(c["result"] or "")
        events.append(e)
    # 1.17.2: the plugin's own hook log (hooks/map_timing.py and friends): measured times, stops and reminders
    log, log_files = read_hook_log(path.stem, records)
    hk = apply_hook_log(events, log)
    for e, c in zip(events, calls, strict=False):           # 2.4.0 (X-04): a build that exited 0 but dropped what the spec asked for
        if e["status"] == "ok" and e["lane"] and not is_report(e) and not e.get("reading") and DEGRADED_TEXT.search(c["result"] or ""):
            e.update(status="degraded", degraded=degraded_items(c["result"] or ""))
    led_rows, led_files = read_ledger(records, *((min(times), max(times)) if times else (None, None)), extra=call_folders(calls))
    led_matched = apply_ledger(events, led_rows)
    led_matched += adopt_ledger_rows(events, led_rows, {c["id"]: _command(c) for c in calls})
    mapev = [e for e in events if e["lane"]]
    lo_t, hi_t = (min(times), max(times)) if times else (None, None)
    reminders = sum(1 for r in log if r.get("e") == "reminder" and (not win or lo_t is None or lo_t <= _ms_time(r["t"]) <= hi_t + dt.timedelta(minutes=5)))
    timed = [e for e in mapev if e["status"] not in ("self", "blocked") and not e.get("stopped_by")]     # calls that ran: a stopped one has no time
    untimed: dict = {}                                   # 4.1.0 (Day 24, T-04): which calls the timing hook did not measure, by tool
    for e in timed:
        if not e.get("measured"):
            k_ = "data server" if e.get("lane") == "ogc-server" else (e.get("tool") or "?")
            untimed[k_] = untimed.get(k_, 0) + 1
    hooks = {"untimed": untimed, "log": bool(log_files), "files": [Path(f).name for f in log_files], "measured": hk["measured"], "map_calls": len(timed),
             "stopped": sum(1 for e in mapev if e.get("stopped_by")), "reminders": reminders}
    # --- rework
    retries, loops, reruns = [], [], []
    last_key, wrote_since = None, False
    last_by_target: dict = {}
    for e in events:
        if e["write"]:
            wrote_since = True
            for k in last_by_target:
                last_by_target[k]["wrote"] = True
            continue
        if not e["lane"] or not e["_key"] or is_report(e):
            continue
        if e["_key"] == last_key and not wrote_since:
            retries.append(e["n"])
        last_key, wrote_since = e["_key"], False
        prev = last_by_target.get(e["_target"])
        if prev and prev["wrote"]:
            row = {"from": prev["n"], "to": e["n"], "lane": e["lane"], "script": e["scripts"][0] if e["scripts"] else e["mcp_tool"],
                   "write": prev["write_kind"], "matched": "target"}
            (loops if prev["status"] == "failed" else reruns).append(row)
        last_by_target[e["_target"]] = {"n": e["n"], "status": e["status"], "wrote": False, "write_kind": None}
    # mark the kind of write between (observed beats inferred)
    for row in loops + reruns:
        kinds = {x["write"] for x in events if row["from"] < x["n"] < row["to"] and x["write"]}
        row["write"] = "observed" if "observed" in kinds else "inferred"
    hot: dict = {}
    for r in loops:
        hot[(r["lane"], r["script"])] = hot.get((r["lane"], r["script"]), 0) + 1
    hotspots = [{"lane": k[0], "script": k[1], "loops": v} for k, v in sorted(hot.items(), key=lambda kv: -kv[1]) if v >= HOTSPOT_LOOPS]
    # --- time
    first, last = (min(times), max(times)) if times else (None, None)
    wall = (last - first).total_seconds() if first else 0.0
    tool_s = _union([(e["_start"], e["_end"]) for e in events])
    map_s = _union([(e["_start"], e["_end"]) for e in mapev])
    gaps = []
    stamps = sorted(set([t for t in times]))
    busy = sorted((e["_start"], e["_end"]) for e in events if e["_start"] and e["_end"])
    idle = 0.0
    for a, b in zip(stamps, stamps[1:]):
        g = (b - a).total_seconds()
        if g >= idle_seconds and not any(s <= a and en >= b for s, en in busy):
            idle += g
            gaps.append(g)
    rtt = []
    for a, b in zip(events, events[1:]):
        if a["_end"] and b["_start"] and b["_start"] >= a["_end"]:
            g = (b["_start"] - a["_end"]).total_seconds()
            if g < idle_seconds:
                rtt.append(g)
    model = max(0.0, wall - tool_s - idle)
    # --- tallies
    lanes: dict = {}
    for e in mapev:
        L = lanes.setdefault(e["lane"], {"calls": 0, "failed": 0, "blocked": 0, "orphan": 0, "seconds": 0.0, "basis": set()})
        L["calls"] += 1
        L["basis"].add(e["basis"])
        L["seconds"] += e["seconds"] or 0.0
        if e["status"] in ("failed", "blocked", "orphan"):
            L[e["status"]] += 1
    for L in lanes.values():
        L["basis"] = sorted(L["basis"])
        L["seconds"] = round(L["seconds"], 1)
        L["low_confidence"] = L["basis"] == ["window"]
    ledger: dict = {}
    for e in mapev:
        for fid, sev in (e.get("findings") or {}).items():
            row = ledger.setdefault(fid, {"severity": sev, "calls": 0, "in_failed": 0})
            row["calls"] += 1
            row["in_failed"] += e["status"] == "failed"
            if {"BLOCK": 2, "WARN": 1, "INFO": 0}.get(sev, -1) > {"BLOCK": 2, "WARN": 1, "INFO": 0}.get(row["severity"], -1):
                row["severity"] = sev
    counted = [e for e in mapev if e["status"] in ("ok", "failed", "degraded")]
    failed = [e for e in mapev if e["status"] == "failed"]
    # 1.21.0 (Day 18 trace): the error rate is the plugin's: a failure set by the session's own command (shell-tail, inline-script) is listed
    # with its evidence but not counted against the plugin
    session_side = [e for e in failed if e.get("error_class") in SESSION_SIDE]
    try:
        import session_report as sr
        runs = sr.load_runs()
        rework_runs = sr.rework(runs)
    except Exception:  # noqa: BLE001
        runs, rework_runs = [], {}
    fix = fix_spec(failed, events) + degraded_spec(mapev, start=len(groups_of(failed)) + 1)
    evals = [e for e in events if e.get("evaluation")]
    outputs, miss = session_outputs(calls, events, records, why=True)
    try:                                                 # 4.2.0: which outputs were handed to the person; the others are tries that were set aside
        sent = {FORMAT_EXT.sub("", q.name) for q, _n in delivered_files(calls, events)}
        for x in outputs:
            x["delivered"] = FORMAT_EXT.sub("", Path(x["path"]).name) in sent
    except Exception:  # noqa: BLE001
        pass
    recorded = recorded_quality(outputs, calls)
    glue = outside_work(calls, events, inv)
    return {**({"map_quality": recorded} if recorded else {}), "outside_plugin": glue, "outputs_missed": miss, "run_ledger": {"files": [Path(f).name for f in led_files], "rows": len(led_rows), "calls_matched": led_matched},
            "evaluation_calls": {"calls": len(evals), "seconds": round(sum(e["seconds"] or 0.0 for e in evals), 1)}, "outputs": outputs,"plugin": pname, "version": _version(), "release": _release_status(), "transcript": path.stem, "found_by": how, "files_read": files, "window": win,
            "first": first.isoformat() if first else None, "last": last.isoformat() if last else None,
            "calls": {"total": len(events), "map": len(mapev), "other": len(events) - len(mapev)},
            "status": {s: sum(1 for e in mapev if e["status"] == s) for s in ("ok", "failed", "degraded", "blocked", "orphan", "self")},
            "error_rate": round((len(failed) - len(session_side)) / len(counted), 3) if counted else 0.0, "session_side_failures": len(session_side),
            "user_turns": turns, "run": run,
            "time": {"wall": round(wall), "tools": round(tool_s), "map_tools": round(map_s), "idle": round(idle), "idle_gaps": len(gaps),
                     "model_overhead_residual": round(model), "round_trip": {"count": len(rtt), "median": round(statistics.median(rtt), 1) if rtt else None,
                                                                          "worst": round(max(rtt), 1) if rtt else None, "total": round(sum(rtt))},
                     "durations": durations_note(hooks)},
            "hooks": hooks,
            "lanes": dict(sorted(lanes.items(), key=lambda kv: -kv[1]["calls"])), "failures": [_public(e) for e in failed],
            "blocked": [_public(e) for e in mapev if e["status"] == "blocked"], "orphans": [e["n"] for e in mapev if e["status"] == "orphan"],
            "retries": retries, "loops": loops, "reruns_after_edit": reruns, "hotspots": hotspots,
            "ledger": dict(sorted(ledger.items(), key=lambda kv: (-{"BLOCK": 2, "WARN": 1, "INFO": 0}.get(kv[1]["severity"], -1), -kv[1]["calls"]))),
            "slowest": [_public(e) for e in sorted((e for e in mapev if e["seconds"] and not e.get("session_script")), key=lambda e: -e["seconds"])[:5]],
            "runs": [{"pipeline": r.get("pipeline"), "status": r.get("status")} for r in runs][-10:], "run_rework": rework_runs,
            "fix_spec": fix, "events": [_public(e) for e in events]}


# 2.4.0 (MT-01): a relative path ending in an output extension, as build_map prints them under a relative --out ("out/<stem>.png")
OUTPUT_REL = re.compile(r"""(?<![\w./\\:-])(?:\.{1,2}/)?[\w.-]+(?:[\\/][\w .-]+)*\.(?:png|svg|pdf|html|mp4|gif|webm|jpe?g)(?=["'\s,\]}]|$)""", re.I)
OUT_ARG = re.compile(r"""--out[=\s]+(?:"([^"]+)"|'([^']+)'|([^\s;&|>]+))""")
SIDECAR = ".provenance.json"


def _resolve(raw: str, base: Path) -> Path:
    p = Path(raw.replace("\\\\", "\\"))
    return p if p.is_absolute() else base / p


def _in_window(f: Path, lo: dt.datetime | None, hi: dt.datetime | None, slack_s: float = 5.0) -> bool:
    try:
        m = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return False
    if lo is None:
        return True
    return lo - dt.timedelta(seconds=slack_s) <= m <= (hi or lo) + dt.timedelta(seconds=slack_s)


def _from_out_folder(folder: Path, lo, hi, spec_id: str | None) -> list[Path]:
    """MT-02: the maps in a build's --out folder whose provenance sidecar was written during the call (or whose stem carries the spec's id)."""
    found = []
    try:
        side = sorted(folder.glob("*" + SIDECAR))
    except OSError:
        return []
    for s in side:
        stem = s.name[: -len(SIDECAR)]
        if not (_in_window(s, lo, hi) or (spec_id and spec_id in stem)):
            continue
        for ext in ("png", "svg", "pdf", "html", "mp4", "gif", "webm", "jpg", "jpeg"):
            f = folder / f"{stem}.{ext}"
            if f.is_file():
                found.append(f)
    return found


def _spec_id(spec: str | None) -> str | None:
    if not spec:
        return None
    try:
        return str((gc.load_mapspec(Path(spec)) or {}).get("id") or "") or None
    except Exception:  # noqa: BLE001
        return None


def session_outputs(calls: list[dict], events: list[dict], records: list[dict], why: bool = False):
    """The finished maps this session wrote through the plugin (2.2.0, widened in 2.4.0). Routes, in order, for every map-writing call that ran
    ok: (1) the outputs the plugin's run ledger recorded for it (X-02, X-03); (2) the paths its printed result names, absolute or relative to
    the call's working folder (MT-01); (3) the files in its `--out` folder with a provenance sidecar written during the call, or carrying the
    spec's id, so redirection no longer matters (MT-02). Previews, mute and draft pictures, sidecars and working folders are left out by
    map_evaluate.discover. With why=True -> (outputs, miss) where miss explains an empty list after finished builds (MT-03)."""
    cwds = [str(d.get("cwd")) for d in records if d.get("cwd")]
    default_base = Path(cwds[-1]) if cwds else Path.cwd()
    found: dict[str, dict] = {}
    builds, reasons, folders = 0, [], []
    for c, e in zip(calls, events, strict=False):
        if e.get("status") not in ("ok", "degraded") or e.get("evaluation") or not (set(e.get("scripts") or []) & OUTPUT_SCRIPTS):
            continue
        builds += 1
        cmd = _command(c)
        run_at = next((m.start() for m in shell_calls.RUN.finditer(cmd)), None) if cmd else None
        eff = shell_calls.effective_dir(cmd, c.get("cwd") or str(default_base), run_at) if cmd else None   # 3.4.0 (F-2): `cd day21 && ...`
        base = Path(eff) if eff else (Path(c.get("cwd")) if c.get("cwd") else default_base)
        sm = SPEC_ARG.search(cmd)
        spec = None
        specs = list(e.get("_ledger_specs") or [])
        if sm:
            sp = _resolve(sm.group(1), base)
            specs.insert(0, str(sp))
        spec = next((s for s in specs if Path(s).is_file()), None)
        before = len(found)

        def add(p: Path, how: str):
            if p.is_file() and str(p) not in found:
                found[str(p)] = {"path": str(p), "spec": spec, "call": e["n"], "how": how}
        for o in e.get("_ledger_outputs") or []:
            add(Path(o), "ledger")
        text = c.get("result") or ""
        for m in OUTPUT_PATH.finditer(text):
            add(Path(m.group(0).replace("\\\\", "\\")), "result")
        for m in OUTPUT_REL.finditer(text):
            add(_resolve(m.group(0), base), "result")
        om = OUT_ARG.search(cmd)
        outs = list(e.get("_ledger_outs") or [])
        if om:
            outs.insert(0, str(_resolve(next(g for g in om.groups() if g), base)))
        for od in dict.fromkeys(outs):
            folder = Path(od)
            folders.append({"out": str(folder), "spec": spec, "call": e["n"]})
            if folder.is_dir():
                for f in _from_out_folder(folder, c.get("start"), c.get("end"), _spec_id(spec)):
                    add(f, "out-folder")
        if len(found) == before:
            if not outs:
                reasons.append("the command names no output folder")
            elif not any(Path(o).is_dir() for o in outs):
                reasons.append("the output folder is gone")
            else:
                reasons.append("no map with a provenance sidecar was written in the call's window")
    stems = {Path(k).stem for k in found}                     # 3.4.0 (F-4): what the person was sent, when the working copy is not found
    for f, n in delivered_files(calls, events):
        if Path(f).stem not in stems and str(f) not in found:
            found[str(f)] = {"path": str(f), "spec": None, "call": n, "how": "delivered"}
    try:
        import map_evaluate as me
        outs_, _fs = me.discover(list(found), {k: v["spec"] for k, v in found.items() if v["spec"]})
        res = [{"path": f, "spec": o.get("spec"), "kind": o["kind"], "id": o["id"]} for o in outs_ for f in o["files"]]
    except Exception:  # noqa: BLE001 - the trace stands without the outputs list
        res = []
    if not why:
        return res
    miss = None
    if builds and not res:
        top = max(set(reasons), key=reasons.count) if reasons else "the files are not where the builds wrote them"
        live = [f for f in folders if Path(f["out"]).is_dir()][:12]
        files = []
        for f in live:
            d = Path(f["out"])
            files += [str(p) for p in sorted(d.glob("*.png")) + sorted(d.glob("*.svg")) + sorted(d.glob("*.pdf"))]
        try:                                                # MT-03: the offer is still made on those builds' --out folders
            import map_evaluate as me
            outs_, _fs = me.discover(files, {}) if files else ([], [])
            fo = [{"path": f, "spec": o.get("spec"), "kind": o["kind"], "id": o["id"]} for o in outs_ for f in o["files"]]
        except Exception:  # noqa: BLE001
            fo = []
        miss = {"builds": builds, "reason": top, "folders": live, "folder_outputs": fo}
    return res, miss


COPY_STEM = re.compile(r"_(?:poster|qa|web)$", re.I)
FORMAT_EXT = re.compile(r"\.(?:png|svg|pdf|mp4|gif|webm|html|jpe?g)$", re.I)
DELIVERED_EXT = (".png", ".svg", ".pdf", ".mp4", ".gif", ".webm", ".html", ".jpg", ".jpeg")
OUTPUTS_FOLDER = Path("/mnt/user-data/outputs")


def plugin_mark(p: Path) -> bool:
    """3.4.0 (F-4): the file carries this plugin's mark: a provenance sidecar beside it, the PNG's Software/BrandPack text, the MP4 comment
    the encoder writes, the page's generator meta or the SVG's data-map-edition."""
    for side in (p.with_name(p.name + SIDECAR), p.with_name(p.stem + SIDECAR)):
        if side.is_file():
            return True
    try:
        head = p.read_bytes()[:400_000] if p.stat().st_size < 400_000_000 else b""
    except OSError:
        return False
    suf = p.suffix.lower()
    if suf == ".png":
        return b"BrandPack" in head or (b"Software" in head and b"geo-mapping" in head)
    if suf in (".mp4", ".webm", ".gif"):
        return b"geo-mapping" in head or b"BrandPack" in head or b"brand_pack" in head
    if suf in (".html", ".svg"):
        return b"data-map-edition" in head or (b'name="generator"' in head and b"geo-mapping" in head)
    if suf == ".pdf":
        return b"geo-mapping" in head
    return False


def delivered_files(calls: list[dict], events: list[dict]) -> list[tuple[Path, int]]:
    """Files the session handed to the person: SendUserFile's files, device_commit_files' staged paths, Artifact asset uploads and publishes,
    and maps under /mnt/user-data/outputs written in the session's window; map extensions carrying the plugin's mark only. -> [(path, call n)]"""
    out: list[tuple[Path, int]] = []
    lo = min((c["start"] for c in calls if c.get("start")), default=None)
    hi = max((c["end"] or c["start"] for c in calls if c.get("start")), default=None)
    for c, e in zip(calls, events, strict=False):
        tool, inp = c["tool"], c["input"] or {}
        raw: list[str] = []
        if tool == "SendUserFile":
            raw = [str(x) for x in inp.get("files") or []]
        elif tool.endswith("device_commit_files"):
            raw = [str(f.get("stagedPath")) for f in inp.get("files") or [] if isinstance(f, dict) and f.get("stagedPath")]
        elif tool == "Artifact":
            raw = [str(x) for x in ([inp.get("file_path")] + list(inp.get("file_paths") or [])) if x]
        base = Path(c.get("cwd")) if c.get("cwd") else Path.cwd()
        for r in raw:
            q = _resolve(r, base)
            if q.suffix.lower() in DELIVERED_EXT and q.is_file() and plugin_mark(q):
                out.append((q, e["n"]))
    if OUTPUTS_FOLDER.is_dir():
        for q in sorted(OUTPUTS_FOLDER.rglob("*")):
            if q.is_file() and q.suffix.lower() in DELIVERED_EXT and (lo is None or _in_window(q, lo, hi, 600)) and plugin_mark(q):
                out.append((q, 0))
    seen, keep = set(), []
    for q, n in out:
        if COPY_STEM.search(q.stem):                         # the video's poster still, its _qa and _web copies: the same map
            continue
        if str(q) not in seen:
            seen.add(str(q))
            keep.append((q, n))
    return keep


def recorded_quality(outputs: list[dict], calls: list[dict]) -> dict | None:
    """3.4.0 (F-5): the Map quality section from scorecards already recorded for these outputs (same file and hash), every output listed:
    scored ones with Effectiveness, Craft and works, the rest "not scored" with the reason. None when no output has a scorecard."""
    if not outputs:
        return None
    try:
        import map_evaluate as me
        by_id: dict[str, dict] = {}
        for x in outputs:
            by_id.setdefault(x["id"], {"id": x["id"], "files": []})["files"].append(x["path"])
        cards = me.recorded_cards(list(by_id.values()), [str(c.get("cwd")) for c in calls if c.get("cwd")][:1])
    except Exception:  # noqa: BLE001 - the trace stands without the scores
        return None
    if not cards:
        return None
    rows = []
    for oid in by_id:
        c = cards.get(oid)
        if c:
            h = c.get("headline") or {}
            rows.append({"output": oid, "effectiveness": h.get("effectiveness"), "craft": h.get("craft"), "works": h.get("works"),
                         "flags": h.get("flags") or [], "run": c.get("run"), "recorded": True})
        else:
            rows.append({"output": oid, "effectiveness": None, "craft": None, "works": "not scored", "flags": ["no scorecard for this file yet"],
                         "recorded": False})
    any_card = next(iter(cards.values()))
    return {"run": ", ".join(sorted({str(c.get("run")) for c in cards.values()})), "rubric_version": any_card.get("rubric_version"),
            "report": str(Path(any_card["_file"]).parent / "report.html"), "outputs": rows, "top_components": [], "from": "recorded"}


def offer_evaluation(o: dict, stem: Path, everything: bool = False) -> list[str]:
    """2.2.0 (map evaluator O2): the outputs list beside the report and a map-evaluate plan for it; the lines to print. Nothing runs here:
    the person sees the cost and says yes first. 2.4.0 (MT-03): finished builds with no output found are never a bare 0: the line says how many
    and why, and the offer is made on those builds' --out folders and specs when they still exist."""
    if not o.get("outputs"):
        miss = o.get("outputs_missed")
        if not miss:      # 4.0.0 (GSC T-2): never a bare 0
            return ["MAP_TRACE_OUTPUTS: 0 (no finished map build was found among this session's calls; a map built by a script of your own is "
                    "found when that script ran the plugin's build_map.py)"]
        lines = [f"MAP_TRACE_OUTPUTS: 0 ({miss['builds']} finished build(s), no output found: {miss['reason']})"]
        outs = miss.get("folder_outputs") or []
        if not outs:
            return lines
        lst = stem.with_suffix(".outputs.json")
        lst.write_text(json.dumps({"session": o["transcript"], "outputs": outs, "from_out_folders": True}, indent=1), encoding="utf-8")
        return lines + _plan_lines({**o, "outputs": outs}, stem, lst)
    mq = o.get("map_quality") or {}
    done = {x["output"] for x in mq.get("outputs") or [] if x.get("recorded")}
    todo = [x for x in o["outputs"] if x["id"] not in done]
    # 4.2.0 (decision D12): the maps that were handed over are graded; a try that was built and set aside is graded only when asked
    # (--evaluate-all). Day 25: six outputs were offered (12 reads); two were delivered.
    handed = {x["id"] for x in o["outputs"] if x.get("delivered")}
    aside = []
    if handed and not everything:
        aside = sorted({x["id"] for x in todo if x["id"] not in handed})
        todo = [x for x in todo if x["id"] in handed]
    lst = stem.with_suffix(".outputs.json")
    lst.write_text(json.dumps({"session": o["transcript"], "outputs": todo or o["outputs"]}, indent=1), encoding="utf-8")
    n = len({x["id"] for x in o["outputs"]})
    head = f"MAP_TRACE_OUTPUTS: {n} {lst}" + (f" ({len(done)} already scored, read back)" if done else "")
    note = ([f"MAP_TRACE_EVALUATE_SET_ASIDE: {len(aside)} output(s) were built and not handed over ({', '.join(aside[:4])}); they are graded only when asked "
             "(map_trace.py --evaluate-only --evaluate-all)"] if aside else [])
    if not todo:
        return [head, "MAP_TRACE_EVALUATE: skipped - every output handed over already has a scorecard (the Map quality section shows them)"] + note
    mode = "auto"
    try:
        import map_evaluate as me
        mode = me.evaluate_mode()
    except Exception:  # noqa: BLE001
        pass
    if mode == "off":
        return [head, "MAP_TRACE_EVALUATE: off (evaluate: off in the person's config); the outputs list is kept"] + note
    auto = (["MAP_TRACE_EVALUATE_AUTO: yes (graded without asking since 4.2.0: one cold read and one graded read for each map handed over, by fresh "
             "agents, observe only; evaluate: ask in the config asks first)"] if mode == "auto" else [])
    return [head] + _plan_lines({**o, "outputs": todo}, stem, lst) + auto + note


def _plan_lines(o: dict, stem: Path, lst: Path) -> list[str]:
    try:
        import pipelines
        p = pipelines.plan("map-evaluate", {"outputs": str(lst)}, {"trace": str(stem.with_suffix(".json"))})
    except Exception as ex:  # noqa: BLE001
        return [f"MAP_TRACE_EVALUATE: skipped - the evaluation could not be planned ({type(ex).__name__})"]
    if not p.get("ok"):
        return ["MAP_TRACE_EVALUATE: skipped - " + "; ".join(f["message"] for f in p.get("findings") or [])[:200]]
    return [f"MAP_TRACE_EVALUATE: {p['cost_text']}",
            f'MAP_TRACE_EVALUATE_RUN: run map-evaluate --outputs "{lst}" --option trace="{stem.with_suffix(".json")}" --confirm {p["confirm_token"]}']


def durations_note(h: dict) -> str:
    if not h["log"] or not h["measured"]:
        return "bounded from transcript timestamps (no timing log for this session)"
    if h["measured"] >= h["map_calls"]:
        return "measured by the plugin's timing hook"
    return f"measured by the plugin's timing hook for {h['measured']} of {h['map_calls']} map calls; the rest bounded from transcript timestamps"


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _version() -> str | None:
    try:
        return gc.build_manifest().get("version")
    except Exception:  # noqa: BLE001
        return None


def groups_of(failed: list[dict]) -> set:
    return {(e["error_class"], e["scripts"][0] if e["scripts"] else (e["mcp_tool"] or e["lane"]), e.get("crash_site") or e.get("evidence", "")[:60]) for e in failed}


def degraded_items(text: str) -> list[str]:
    """What a build dropped, from its result: the `degraded` list of its JSON, or its 'relief missing: <reason>' summary."""
    out = []
    m = re.search(r'"degraded":\s*(\[[^\]]*\])', text or "")
    if m:
        try:
            out += [str(x) for x in json.loads(m.group(1))]
        except ValueError:
            pass
    out += [f"relief missing: {r.strip()}" for r in re.findall(r"relief missing:\s*([^\n\"]{0,120})", text or "")]
    return list(dict.fromkeys(out))[:6] or ["something the spec asked for"]


def degraded_spec(mapev: list[dict], start: int = 1) -> list[dict]:
    """2.4.0 (X-04): a call that exited 0 but dropped something the spec asked for is a finding of its own: disposition product when the
    plugin could have drawn it (relief from another source, a layer it could not read), input when the spec named something absent."""
    out = []
    for i, e in enumerate((e for e in mapev if e["status"] == "degraded"), start):
        items = e.get("degraded") or []
        disp = "input" if any("not found" in x or "no such" in x for x in items) else "product"
        script = e["scripts"][0] if e["scripts"] else (e["mcp_tool"] or e["lane"])
        out.append({"id": f"F-{i}", "class": "degraded", "disposition": disp, "owner": f"scripts/{script}.py" if script else None,
                    "crash_site": None, "exception": None, "exit_code": 0, "calls": [e["n"]], "lane": e["lane"],
                    "evidence": "; ".join(items)[:240], "recovered": False, "severity": "medium",
                    "diagnosis": "DIAGNOSIS PENDING (F-%d)" % i if disp == "product" else None,
                    "reading": None if disp == "product" else READING.get(disp, "")})
    return out


def fix_spec(failed: list[dict], events: list[dict]) -> list[dict]:
    """One finding per (class, script, crash site or evidence): every failure gets one disposition; product findings need a diagnosis."""
    groups: dict = {}
    for e in failed:
        script = e["scripts"][0] if e["scripts"] else (e["mcp_tool"] or e["lane"])
        key = (e["error_class"], script, e.get("crash_site") or e.get("evidence", "")[:60])
        groups.setdefault(key, []).append(e)
    out = []
    for i, ((cls, script, _), es) in enumerate(sorted(groups.items(), key=lambda kv: kv[1][0]["n"]), 1):
        first = es[0]
        target = None
        for ev in events:
            if ev["n"] == first["n"]:
                target = ev["_target"]
        later_ok = any(ev["n"] > first["n"] and ev["_target"] == target and ev["status"] == "ok" for ev in events) if target else False
        disp = DISPOSITION.get(cls, "unclassified")
        if cls == "crash" and not first.get("crash_site"):
            disp = "operator"                                  # a traceback with no frame in this plugin: an inline script of the session's own
        if disp in ("operator", "shell") and script:
            owner = f"the session's own command after {script}.py" if script in plugin_inventory_cache()["scripts"] else f"the session's own command ({script})"
        else:
            owner = None
        out.append({"id": f"F-{i}", "class": cls, "disposition": disp, "owner": owner or (f"scripts/{script}.py" if script and script in plugin_inventory_cache()["scripts"] else script),
                    "crash_site": first.get("crash_site"), "exception": first.get("exception"), "exit_code": first.get("exit_code"),
                    "calls": [x["n"] for x in es], "lane": first["lane"], "evidence": first.get("evidence"),
                    "recovered": later_ok, "severity": "low" if later_ok else ("high" if disp == "product" else "medium"),
                    "diagnosis": "DIAGNOSIS PENDING (F-%d)" % i if disp == "product" else None,
                    "reading": None if disp == "product" else READING.get(disp, "")})
    return out


# ---------------------------------------------------------------------------------------------------------------- output
def fmt_d(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _release_status() -> dict:
    """4.1.0 (Day 24, D24-05): the session ran 4.0.1 while 4.0.3 was out. The trace says so, from the doctor's once-a-day release check."""
    try:
        import release_check as rc
        r = rc.check()
        return {"latest": r.get("latest"), "newer": bool(r.get("newer")), "note": r.get("note")}
    except Exception:  # noqa: BLE001 - never stops a trace
        return {"latest": None, "newer": False, "note": None}


def bar(n: int, top: int, width: int = 18) -> str:
    return "█" * max(1, round(width * n / top)) if top and n else ""


def text_report(o: dict) -> str:
    L = []
    L.append(f"MAP TRACE  {o['plugin']} {o['version'] or ''}".rstrip())
    rel = o.get("release") or {}
    if rel.get("newer"):
        L.append(f"Release   this session ran {o['version']}; {rel['latest']} is out. Findings below may already be fixed: update first")
    warn = " (a guess: the newest transcript; pass --session <id>)" if o["found_by"] == "NEWEST-FALLBACK" else ""
    L.append(f"Transcript: {o['transcript'][:8]}… found by {o['found_by']}{warn}" + (f" · window {o['window']}" if o["window"] else ""))
    if len(o["files_read"]) > 1:
        L.append(f"Read with {len(o['files_read']) - 1} pre-compaction cop{'y' if len(o['files_read']) == 2 else 'ies'}")
    if o["first"]:
        L.append(f"Span: {o['first'][:16].replace('T', ' ')} → {o['last'][:16].replace('T', ' ')} UTC · durations {o['time']['durations']}")
    c, s = o["calls"], o["status"]
    L.append("")
    L.append(f"Calls     {c['total']} in the session · {c['map']} map plugin ({100 * c['map'] // max(1, c['total'])}%) · {c['other']} other (not read)")
    if not c["map"]:
        L.append("No call in this window used the map plugin.")
        return "\n".join(L)
    L.append(f"Status    {s['ok']} ok · {s['failed']} failed" + (f" · {s['degraded']} degraded" if s.get("degraded") else "") + f" · {s['blocked']} blocked · {s['orphan']} unfinished" + (" · 1 self" if s["self"] else "")
             + f"   error rate {100 * o['error_rate']:.1f}% (blocked" + (f" and {o['session_side_failures']} session-side" if o.get("session_side_failures") else "")
             + " excluded)")
    t = o["time"]
    L.append(f"Time      wall {fmt_d(t['wall'])} · tools {fmt_d(t['tools'])} (map {fmt_d(t['map_tools'])}) · idle {fmt_d(t['idle'])} in {t['idle_gaps']} gap(s)"
             f" · not running a tool {fmt_d(t['model_overhead_residual'])} (residual)")
    h = o["hooks"]
    L.append(f"Hooks     timing {h['measured']} of {h['map_calls']} map call(s) measured" + ("" if h["log"] else " (no timing log)")
             + f" · pre-export check stopped {h['stopped']} build(s) · credentials reminder {h['reminders']} time(s)")
    if h.get("untimed"):
        L.append("          not measured: " + ", ".join(f"{k} {v}" for k, v in sorted(h["untimed"].items(), key=lambda kv: -kv[1]))
                 + " (their time is bounded from the transcript)")
    L.append("")
    L.append("Map lanes (calls)")
    top = max(v["calls"] for v in o["lanes"].values())
    for name, v in list(o["lanes"].items())[:12]:
        flag = "  LOW CONFIDENCE" if v["low_confidence"] else ""
        bad = f"  {v['failed']} failed" if v["failed"] else ""
        L.append(f"  {name[:24]:<24} {v['calls']:>4} {bar(v['calls'], top):<18} {fmt_d(v['seconds']):>7}{bad}{flag}")
    if o["failures"]:
        L.append("")
        L.append("Failures (map)")
        for e in o["failures"][:15]:
            what = (e["scripts"][0] + ".py" + (f" {e['op']}" if e["op"] else "")) if e["scripts"] else (e["mcp_tool"] or e["lane"])
            site = f"  @ {e['crash_site']}" if e.get("crash_site") else ""
            why = "stopped before it ran by the pre-export check" if e.get("stopped_by") else e.get("evidence", "")[:70]
            L.append(f"  #{e['n']:<5} {what[:26]:<26} {e['error_class']:<11} {why}{site}")
        if len(o["failures"]) > 15:
            L.append(f"  … {len(o['failures']) - 15} more in the report file")
    if o["blocked"]:
        L.append(f"Blocked   {len(o['blocked'])} call(s) refused by the harness or a permission (calls " + ", ".join(f"#{e['n']}" for e in o["blocked"][:8]) + ")")
    if o["orphans"]:
        L.append(f"Unfinished {len(o['orphans'])} call(s) with no result (calls " + ", ".join(f"#{n}" for n in o["orphans"][:8]) + ")")
    L.append("")
    L.append(f"Rework    {len(o['loops'])} iteration loop(s) (fail → edit → re-run) · {len(o['reruns_after_edit'])} re-run(s) after an edit · {len(o['retries'])} retry(ies)")
    for h in o["hotspots"]:
        L.append(f"  HOTSPOT {h['lane']} / {h['script']}: {h['loops']} loops")
    for r in o["loops"][:6]:
        L.append(f"  loop #{r['from']} → #{r['to']}  {r['script']}  (write {r['write']}, matched on {r['matched']})")
    if o["ledger"]:
        L.append("")
        L.append("Findings named (MAP-*)")
        for fid, r in list(o["ledger"].items())[:10]:
            L.append(f"  {fid:<14} {r['severity']:<5} in {r['calls']} call(s)" + (f", {r['in_failed']} failed" if r["in_failed"] else ""))
    if o["slowest"]:
        L.append("")
        L.append("Slowest map calls")
        for e in o["slowest"]:
            what = (e["scripts"][0] + ".py" + (f" {e['op']}" if e["op"] else "")) if e["scripts"] else (e["mcp_tool"] or e["lane"])
            why = (f"  whole command, also ran {', '.join(e['shared_with'])}" if e.get("shared_with") else "") + (f"  ({e['note']})" if e.get("note") else "")
            L.append(f"  #{e['n']:<5} {what[:30]:<30} {fmt_d(e['seconds']):>7}  {e['status']}{why}")
    if o["runs"]:
        rr = o["run_rework"] or {}
        L.append("")
        rep = [f"{g['pipeline']} x{g['runs']}" + ("" if g["ended_ok"] else " (never ended ok)") for g in rr.get("groups", [])]
        L.append(f"Pipeline runs (working folder): {len(o['runs'])} recorded" + (f" · repeated or blocked: {', '.join(rep[:5])}" if rep else ""))
    ec = o.get("evaluation_calls") or {}
    if ec.get("calls"):
        L.append(f"Evaluation {ec['calls']} call(s) of the map evaluator, {fmt_d(ec['seconds'])}, not counted as map work")
    if o.get("outputs"):
        L.append(f"Outputs   {len({x['id'] for x in o['outputs']})} finished map output(s) written this session (the map evaluator can grade them)")
    elif o.get("outputs_missed"):                       # 2.4.0 (MT-03): never a silent 0 after finished builds
        m = o["outputs_missed"]
        L.append(f"Outputs   0 ({m['builds']} finished build(s), no output found: {m['reason']})")
    if o.get("outside_plugin"):                        # 2.4.0 (X-06): each one is a feature the plugin lacked, or a recipe for the library
        L.append("")
        L.append("Work done outside the plugin (each is a candidate plugin feature)")
        for g in o["outside_plugin"][:10]:
            L.append(f"  #{g['n']:<5} {g['what'][:40]:<40} {', '.join(g['libraries'])}" + (f"  ({', '.join(g['ops'][:6])})" if g.get("ops") else ""))
    if o.get("road"):                                  # 4.2.0: the road rubric, how the maps were made (road.yaml)
        import road_rubric
        L += road_rubric.lines(o["road"])
        L.append("")
    if (o.get("run_ledger") or {}).get("rows"):
        rl = o["run_ledger"]
        L.append(f"Ledger    {rl['rows']} plugin run(s) recorded, {rl['calls_matched']} call(s) matched (exit codes and outputs from the plugin itself)")
    mq = o.get("map_quality")
    if mq:
        L.append("")
        L.append(f"Map quality (observed, rubric {mq.get('rubric_version')})")
        for x in mq.get("outputs") or []:
            L.append(f"  {x['output'][:34]:<34} effectiveness {x['effectiveness'] if x['effectiveness'] is not None else '-':<4} craft {x['craft'] if x['craft'] is not None else '-':<4} works {x['works']}")
    pend = sum(1 for f in o["fix_spec"] if f["diagnosis"])
    L.append("")
    L.append(f"Fix spec  {len(o['fix_spec'])} finding(s): " + ", ".join(f"{d} {sum(1 for f in o['fix_spec'] if f['disposition'] == d)}"
                                                                for d in sorted({f['disposition'] for f in o['fix_spec']})) if o["fix_spec"] else "Fix spec  no map failures")
    if pend:
        L.append(f"          {pend} product finding(s) need a diagnosis (see the report file)")
    return "\n".join(L)


def md_report(o: dict, text: str) -> str:
    out = [f"# Map trace: {o['plugin']} {o['version'] or ''}".rstrip(), "",
           f"Transcript `{o['transcript'][:8]}…` (found by {o['found_by']}). Durations are {o['time']['durations']}. "
           "Only this plugin's calls are read; other calls are counted.", "", "```", text, "```", "", "## Fix spec", ""]
    if not o["fix_spec"]:
        out.append("No map plugin call failed in this window.")
    for f in o["fix_spec"]:
        out += [f"### {f['id']}: {f['class']} in {f['owner']} ({f['disposition']}, severity {f['severity']})", "",
                "- **Calls:** " + ", ".join(f"#{n}" for n in f["calls"]) + (" (recovered: a later run of the same target succeeded)" if f["recovered"] else " (not recovered)"),
                f"- **Lane:** {f['lane']}",
                f"- **Crash site:** `{f['crash_site']}`" if f["crash_site"] else "- **Crash site:** none inside this plugin",
                f"- **Exit code:** {f['exit_code']}" if f["exit_code"] is not None else "- **Exit code:** not recorded",
                f"- **Evidence:** `{f['evidence']}`" if f.get("evidence") else "- **Evidence:** none recorded"]
        if f["diagnosis"]:
            out += [f"- **Diagnosis:** {f['diagnosis']}",
                    "- **Acceptance:** the call shape above runs without this error, and a test in `tests/` reproduces the failure before the fix and passes after it."]
        else:
            out.append(f"- **Reading:** {f['reading']}" if f.get("reading") else "")
        out.append("")
    return "\n".join(out)


def write_page(o: dict, stem: Path, pdf: bool = True) -> tuple[str, str]:
    """1.18.0: the report as a branded page (trace_page), HTML and PDF, beside the Markdown. Returns the two lines to print: a path, or
    `skipped - <reason>`. A page that cannot be made never fails the trace: the text report and the Markdown already stand."""
    try:
        import trace_page
        page = trace_page.html(o)
        stem.with_suffix(".html").write_text(page, encoding="utf-8")
        html_line = str(stem.with_suffix(".html"))
    except Exception as ex:                                      # noqa: BLE001
        return f"skipped - the page could not be made ({type(ex).__name__}: {str(ex)[:120]})", "skipped - no HTML page"
    if not pdf:
        return html_line, "skipped - --no-pdf"
    try:
        stem.with_suffix(".pdf").write_bytes(trace_page.pdf(page))
        return html_line, str(stem.with_suffix(".pdf"))
    except Exception as ex:                                      # noqa: BLE001 - WeasyPrint or its native libraries missing: map-doctor says which
        return html_line, f"skipped - the PDF renderer is not available ({type(ex).__name__}); run the map doctor"


def out_dir(arg: str | None) -> Path:
    d = Path(arg or os.environ.get("GEO_MAPPING_TRACE_DIR") or gc.workdir() / "traces")
    d.mkdir(parents=True, exist_ok=True)
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", nargs="?", default="trace", choices=["trace", "find"])
    ap.add_argument("--transcript")
    ap.add_argument("--session")
    ap.add_argument("--since", help="only the last 30m / 2h / 1d, or from an ISO time")
    ap.add_argument("--until")
    ap.add_argument("--out-dir")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--no-files", action="store_true")
    ap.add_argument("--no-html", action="store_true", help="no HTML or PDF page (1.18.0)")
    ap.add_argument("--no-pdf", action="store_true", help="the HTML page without its PDF (1.18.0)")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--idle-seconds", type=int, default=IDLE_SECONDS)
    ap.add_argument("--no-evaluate", action="store_true", help="2.2.0: do not list the session's outputs or offer the map evaluator")
    ap.add_argument("--copy-to", metavar="DIR", help="4.1.0 (Day 24, T-04): also copy the report files into DIR (the map's work folder's "
                    ".geo-mapping/traces), so a later session can read them")
    ap.add_argument("--evaluate-all", action="store_true", help="4.2.0: grade the tries that were built and set aside too, not only the maps handed over")
    ap.add_argument("--evaluate-only", action="store_true", help="2.2.0: offer the map evaluator on the outputs of the newest trace of this session, without tracing again")
    a = ap.parse_args(argv)
    if a.op == "find":
        for p in candidates()[:20]:
            print(f"{p.stem}  {dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='minutes')}  {p.stat().st_size // 1024} KB")
        return gc.EXIT_OK
    try:
        path, how = locate(a.transcript, a.session)
        o = build(path, how, since=a.since, until=a.until, idle_seconds=a.idle_seconds)
        if (o.get("calls") or {}).get("map"):            # 4.2.0: the road rubric, from this summary and the finished maps' own records
            try:
                import road_rubric
                o["road"] = road_rubric.measure(o)
            except Exception:  # noqa: BLE001 - a report line, never a reason to lose the trace
                pass
    except TraceError as ex:
        print(f"map_trace: {ex}. Nothing to trace.", file=sys.stderr)
        return gc.EXIT_USAGE
    if a.evaluate_only:
        d = out_dir(a.out_dir)
        prev = sorted(d.glob(f"map-trace_{o['transcript'][:8]}_*.json"), key=lambda q: q.stat().st_mtime)
        prev = [q for q in prev if not q.name.endswith(".outputs.json")]
        if not prev:
            print("map_trace: no earlier trace of this session to evaluate; run map-trace first.", file=sys.stderr)
            return gc.EXIT_USAGE
        for ln in offer_evaluation(o, prev[-1].with_suffix(""), a.evaluate_all):
            print(ln)
        return gc.EXIT_OK
    text = text_report(o)
    if a.format == "json":
        print(json.dumps({k: v for k, v in o.items() if k != "events"}, indent=1, default=str))
    else:
        print(text)
    if not a.no_files:
        d = out_dir(a.out_dir)
        stem = f"map-trace_{o['transcript'][:8]}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        (d / f"{stem}.md").write_text(md_report(o, text), encoding="utf-8")
        (d / f"{stem}.json").write_text(json.dumps(o, indent=1, default=str), encoding="utf-8")
        print(f"\nMAP_TRACE_REPORT: {d / (stem + '.md')}")
        print(f"MAP_TRACE_EVENTS: {d / (stem + '.json')}")
        if not a.no_html:
            page_line, pdf_line = write_page(o, d / stem, pdf=not a.no_pdf)
            print(f"MAP_TRACE_HTML: {page_line}")
            print(f"MAP_TRACE_PDF: {pdf_line}")
        print(f"MAP_TRACE_FIXSPEC_PENDING: {sum(1 for f in o['fix_spec'] if f['diagnosis'])}")
        files = [d / f"{stem}{x}" for x in (".md", ".json", ".html", ".pdf") if (d / f"{stem}{x}").is_file()]
        if a.copy_to:
            import shutil
            dst = Path(a.copy_to)
            dst.mkdir(parents=True, exist_ok=True)
            for f_ in files:
                shutil.copy2(f_, dst / f_.name)
            print(f"MAP_TRACE_COPIED: {len(files)} file(s) to {dst}")
        else:
            # 4.1.0 (T-04): a cloud workspace is gone when the session ends; the files are kept where the map is
            print(f"MAP_TRACE_KEEP: {len(files)} file(s) live only in {d}; copy them into the work folder's .geo-mapping/traces on the person's "
                  "computer (or pass --copy-to), and offer, never assume, a copy in the Project")
        if not a.no_evaluate:
            for ln in offer_evaluation(o, d / stem, a.evaluate_all):
                print(ln)
    if a.strict and o["status"]["failed"]:
        return gc.EXIT_LINT
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
