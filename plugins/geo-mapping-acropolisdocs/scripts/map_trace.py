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

Status: ok, failed, blocked (the harness or a permission refused the call; excluded from the error rate), orphan (no result: the run was
interrupted) or self (this report's own call, which cannot see its own result).

Rework: a retry is the same command again with nothing written between; an iteration loop is the same script on the same target again after
a write, where the earlier run failed; a re-run after edit is the same shape where it did not fail (normal for visual checks). Writes made
through the shell are inferred from the command text and marked so.

Printed: skill, script and tool names, option names (never their values), call numbers, timings, exit codes, MAP-* finding ids, and for each
failure one line of its output, redacted (hosts, keys, tokens) with every path cut to its last part and capped at 160 characters. Never
message text, command text or a full path. Durations come from transcript timestamps: they are bounds, not measurements.

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
DISPOSITION = {"crash": "product", "dependency": "dependency", "lint-block": "input", "usage": "input", "refused": "input", "brand": "input",
               "service": "environment", "network": "environment", "environment": "environment", "timeout": "environment", "failed": "unclassified"}


class TraceError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- the plugin's own names
def plugin_name() -> str:
    try:
        return gc.build_manifest().get("plugin_name") or "geo-mapping"
    except Exception:  # noqa: BLE001
        return "geo-mapping"


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


def candidates() -> list[Path]:
    root = projects_dir()
    found = [p for p in root.glob("*/*.jsonl") if is_transcript(p)] if root.is_dir() else []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


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
        raise TraceError("No session transcript was found on this machine")
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
                                      "start": ts, "end": None, "result": None, "is_error": False}
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
    inp = call["input"]
    return str(inp.get("command") or "") if call["tool"] in ("Bash",) or call["tool"].endswith("device_bash") else ""


RUN_SCRIPT = re.compile(r"""(?:^|[\s;&|(])(?:python3?(?:\.\d+)?|py(?:\s+-3)?)(?:\s+-[A-Za-z]+)*\s+["']?([^\s"';&|]*?)(?:^|/)(scripts|lints)/([a-z0-9_]+)\.py""")
INLINE_PY = re.compile(r"python3?(?:\.\d+)?(?:\s+-[A-Za-z]+)*\s+(?:-\s*<<|-c\b)")
IMPORT = re.compile(r"^\s*(?:import|from)\s+([a-z0-9_]+)", re.M)


def map_scripts(cmd: str, inv: dict) -> tuple[list[str], str | None]:
    """This plugin's scripts or lints the command RUNS (a Python interpreter on the file), or imports from an inline Python program that
    puts a scripts/ or lints/ folder on its path. Reading a file (grep, sed, cat) is not using the plugin. Returns (names, how)."""
    out = []
    for m in RUN_SCRIPT.finditer(cmd):
        name, before = m.group(3), (m.group(1) or "") + "/" + m.group(2)
        if name in inv["scripts"] and not re.search(r"skills/[^/\s\"']+/" + re.escape(m.group(2)), before) and name not in out:
            out.append(name)
    if out:
        return out, "path"
    if INLINE_PY.search(cmd) and re.search(r"""(?:^|[/\s"'])(?:scripts|lints)["'/]""", cmd) and not re.search(r"skills/[^/\s\"']+/scripts", cmd):
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


def classify(call: dict, inv: dict, pname: str, window: str | None) -> dict:
    """Map lane, basis, scripts, op, options and the keys rework detection uses, or lane None for a call that is not the map plugin's."""
    tool, inp = call["tool"], call["input"]
    ev = {"lane": None, "basis": None, "scripts": [], "op": None, "options": [], "mcp_tool": None, "write": None, "key": None, "target": None}
    if tool == "Skill":
        sk = str(inp.get("skill") or "")
        base, _, short = sk.rpartition(":")
        if (base.startswith("geo-mapping-") or base == pname) or (not base and short in inv["skills"]):
            ev.update(lane=short or sk, basis="declared", opens=short or sk)
        else:
            ev["closes"] = True
        return ev
    if "geo-mapping" in tool and "__" in tool:
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
    if not found:
        return ev
    main = next((s for s in found if s not in ("gmap_common", "redact")), found[0])
    owner = inv["owners"].get(main)
    lane = window if (window and window in inv["skills"] and owner in (None, window)) else (owner or f"({main})")
    ev.update(lane=lane, basis=how if lane != window else "window", scripts=found, op=_op_after(cmd, main) if how == "path" else None,
              options=sorted(set(OPTION.findall(cmd)))[:12], key=hashlib.sha1(cmd.encode()).hexdigest()[:12], target=(main, ev.get("op") or _op_after(cmd, main) or "-", _target(cmd)))
    return ev


def status_of(call: dict, is_last_map_trace: bool) -> str:
    if call["result"] is None:
        return "self" if is_last_map_trace else "orphan"
    t = call["result"]
    if t.lstrip().startswith("<tool_use_error>") or "Permission for this action was denied" in t or "permission rule" in t.lower() and "denied" in t.lower():
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
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not EXIT.match(ln)]
    pick = next((ln for ln in reversed(lines) if EXC_LINE.match(ln)), None) \
        or next((ln for ln in lines if re.search(r"error|refused|BLOCK|not found|denied|failed|cannot|unable", ln, re.I)), None) \
        or (lines[0] if lines else "")
    pick, _ = redact.scrub(pick)
    pick = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", "<url>", pick, flags=re.I)        # no host name or query ever prints
    pick = _short_paths(pick)
    return pick if len(pick) <= EVIDENCE_CAP else pick[:EVIDENCE_CAP - 1] + "…"


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


def findings_in(text: str) -> dict[str, str]:
    """MAP-* ids a result names, with the severity printed beside them when there is one."""
    out: dict[str, str] = {}
    rank = {"INFO": 0, "WARN": 1, "BLOCK": 2, "?": -1}
    for fid, sev in FINDING_JSON.findall(text or ""):
        if rank[sev] > rank.get(out.get(fid, "?"), -1):
            out[fid] = sev
    for a, b, c, d in FINDING_NEAR.findall(text or ""):
        fid, sev = (b, a) if b else (c, d)
        if rank[sev] > rank.get(out.get(fid, "?"), -1):
            out[fid] = sev
    for fid in FINDING.findall(text or ""):
        out.setdefault(fid, "?")
    return out


# ---------------------------------------------------------------------------------------------------------------- the trace
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
    events, window = [], None
    last_trace = max((i for i, c in enumerate(calls) if "map_trace.py" in _command(c)), default=-1)
    for n, c in enumerate(calls, 1):
        ev = classify(c, inv, pname, window)
        if ev.get("opens"):
            window = ev["opens"]
        elif ev.get("closes"):
            window = None
        st = status_of(c, n - 1 == last_trace)
        dur = (c["end"] - c["start"]).total_seconds() if c["start"] and c["end"] else None
        e = {"n": n, "tool": c["tool"] if not c["tool"].startswith("mcp__") or ev["mcp_tool"] else "mcp", "lane": ev["lane"], "basis": ev["basis"],
             "scripts": ev["scripts"], "op": ev["op"], "options": ev["options"], "mcp_tool": ev["mcp_tool"], "write": ev["write"],
             "status": st, "seconds": round(dur, 1) if dur is not None else None, "start": c["start"].isoformat() if c["start"] else None,
             "_key": ev["key"], "_target": ev["target"], "_start": c["start"], "_end": c["end"]}
        if ev["lane"]:
            e["findings"] = findings_in(c["result"] or "")
            if st == "failed":
                cls, site, exc = error_class(c["result"] or "")
                code = EXIT.search(c["result"] or "")
                e.update(error_class=cls, crash_site=site, exception=exc, exit_code=int(code.group(1)) if code else None, evidence=evidence(c["result"] or ""))
            elif st == "blocked":
                e["evidence"] = evidence(c["result"] or "")
        events.append(e)
    mapev = [e for e in events if e["lane"]]
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
        if not e["lane"] or not e["_key"]:
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
    counted = [e for e in mapev if e["status"] in ("ok", "failed")]
    failed = [e for e in mapev if e["status"] == "failed"]
    try:
        import session_report as sr
        runs = sr.load_runs()
        rework_runs = sr.rework(runs)
    except Exception:  # noqa: BLE001
        runs, rework_runs = [], {}
    fix = fix_spec(failed, events)
    return {"plugin": pname, "version": _version(), "transcript": path.stem, "found_by": how, "files_read": files, "window": win,
            "first": first.isoformat() if first else None, "last": last.isoformat() if last else None,
            "calls": {"total": len(events), "map": len(mapev), "other": len(events) - len(mapev)},
            "status": {s: sum(1 for e in mapev if e["status"] == s) for s in ("ok", "failed", "blocked", "orphan", "self")},
            "error_rate": round(len(failed) / len(counted), 3) if counted else 0.0, "user_turns": turns,
            "time": {"wall": round(wall), "tools": round(tool_s), "map_tools": round(map_s), "idle": round(idle), "idle_gaps": len(gaps),
                     "model_overhead_residual": round(model), "round_trip": {"count": len(rtt), "median": round(statistics.median(rtt), 1) if rtt else None,
                                                                          "worst": round(max(rtt), 1) if rtt else None, "total": round(sum(rtt))},
                     "durations": "bounded from transcript timestamps"},
            "lanes": dict(sorted(lanes.items(), key=lambda kv: -kv[1]["calls"])), "failures": [_public(e) for e in failed],
            "blocked": [_public(e) for e in mapev if e["status"] == "blocked"], "orphans": [e["n"] for e in mapev if e["status"] == "orphan"],
            "retries": retries, "loops": loops, "reruns_after_edit": reruns, "hotspots": hotspots,
            "ledger": dict(sorted(ledger.items(), key=lambda kv: (-{"BLOCK": 2, "WARN": 1, "INFO": 0}.get(kv[1]["severity"], -1), -kv[1]["calls"]))),
            "slowest": [_public(e) for e in sorted((e for e in mapev if e["seconds"]), key=lambda e: -e["seconds"])[:5]],
            "runs": [{"pipeline": r.get("pipeline"), "status": r.get("status")} for r in runs][-10:], "run_rework": rework_runs,
            "fix_spec": fix, "events": [_public(e) for e in events]}


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _version() -> str | None:
    try:
        return gc.build_manifest().get("version")
    except Exception:  # noqa: BLE001
        return None


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
        out.append({"id": f"F-{i}", "class": cls, "disposition": disp, "owner": f"scripts/{script}.py" if script and script in plugin_inventory_cache()["scripts"] else script,
                    "crash_site": first.get("crash_site"), "exception": first.get("exception"), "exit_code": first.get("exit_code"),
                    "calls": [x["n"] for x in es], "lane": first["lane"], "evidence": first.get("evidence"),
                    "recovered": later_ok, "severity": "low" if later_ok else ("high" if disp == "product" else "medium"),
                    "diagnosis": "DIAGNOSIS PENDING (F-%d)" % i if disp == "product" else None})
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


def bar(n: int, top: int, width: int = 18) -> str:
    return "█" * max(1, round(width * n / top)) if top and n else ""


def text_report(o: dict) -> str:
    L = []
    L.append(f"MAP TRACE  {o['plugin']} {o['version'] or ''}".rstrip())
    warn = " (a guess: the newest transcript; pass --session <id>)" if o["found_by"] == "NEWEST-FALLBACK" else ""
    L.append(f"Transcript: {o['transcript'][:8]}… found by {o['found_by']}{warn}" + (f" · window {o['window']}" if o["window"] else ""))
    if len(o["files_read"]) > 1:
        L.append(f"Read with {len(o['files_read']) - 1} pre-compaction cop{'y' if len(o['files_read']) == 2 else 'ies'}")
    if o["first"]:
        L.append(f"Span: {o['first'][:16].replace('T', ' ')} → {o['last'][:16].replace('T', ' ')} UTC · durations are bounds from transcript timestamps, not measured")
    c, s = o["calls"], o["status"]
    L.append("")
    L.append(f"Calls     {c['total']} in the session · {c['map']} map plugin ({100 * c['map'] // max(1, c['total'])}%) · {c['other']} other (not read)")
    if not c["map"]:
        L.append("No call in this window used the map plugin.")
        return "\n".join(L)
    L.append(f"Status    {s['ok']} ok · {s['failed']} failed · {s['blocked']} blocked · {s['orphan']} unfinished" + (" · 1 self" if s["self"] else "")
             + f"   error rate {100 * o['error_rate']:.1f}% (blocked excluded)")
    t = o["time"]
    L.append(f"Time      wall {fmt_d(t['wall'])} · tools {fmt_d(t['tools'])} (map {fmt_d(t['map_tools'])}) · idle {fmt_d(t['idle'])} in {t['idle_gaps']} gap(s)"
             f" · not running a tool {fmt_d(t['model_overhead_residual'])} (residual)")
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
            L.append(f"  #{e['n']:<5} {what[:26]:<26} {e['error_class']:<11} {e.get('evidence', '')[:70]}{site}")
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
            L.append(f"  #{e['n']:<5} {what[:30]:<30} {fmt_d(e['seconds']):>7}  {e['status']}")
    if o["runs"]:
        rr = o["run_rework"] or {}
        L.append("")
        rep = [f"{g['pipeline']} x{g['runs']}" + ("" if g["ended_ok"] else " (never ended ok)") for g in rr.get("groups", [])]
        L.append(f"Pipeline runs (working folder): {len(o['runs'])} recorded" + (f" · repeated or blocked: {', '.join(rep[:5])}" if rep else ""))
    pend = sum(1 for f in o["fix_spec"] if f["diagnosis"])
    L.append("")
    L.append(f"Fix spec  {len(o['fix_spec'])} finding(s): " + ", ".join(f"{d} {sum(1 for f in o['fix_spec'] if f['disposition'] == d)}"
                                                                for d in sorted({f['disposition'] for f in o['fix_spec']})) if o["fix_spec"] else "Fix spec  no map failures")
    if pend:
        L.append(f"          {pend} product finding(s) need a diagnosis (see the report file)")
    return "\n".join(L)


def md_report(o: dict, text: str) -> str:
    out = [f"# Map trace: {o['plugin']} {o['version'] or ''}".rstrip(), "",
           f"Transcript `{o['transcript'][:8]}…` (found by {o['found_by']}). Durations are bounds from transcript timestamps, not measurements. "
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
            out.append({"input": "- **Reading:** a gate or the input stopped the run (the spec, the data or the arguments); fix the input, not the plugin.",
                        "dependency": "- **Reading:** a package or native library is missing; run the map doctor (`doctor.py --fix --all`).",
                        "environment": "- **Reading:** the machine, network or a permission stopped the call; not a plugin defect.",
                        "operator": "- **Reading:** a traceback with no frame in this plugin: an inline script of the session's own failed.",
                        "unclassified": "- **Reading:** the output does not say why; reproduce before calling it a defect."}.get(f["disposition"], ""))
        out.append("")
    return "\n".join(out)


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
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--idle-seconds", type=int, default=IDLE_SECONDS)
    a = ap.parse_args(argv)
    if a.op == "find":
        for p in candidates()[:20]:
            print(f"{p.stem}  {dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='minutes')}  {p.stat().st_size // 1024} KB")
        return gc.EXIT_OK
    try:
        path, how = locate(a.transcript, a.session)
        o = build(path, how, since=a.since, until=a.until, idle_seconds=a.idle_seconds)
    except TraceError as ex:
        print(f"map_trace: {ex}. Nothing to trace.", file=sys.stderr)
        return gc.EXIT_USAGE
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
        print(f"MAP_TRACE_FIXSPEC_PENDING: {sum(1 for f in o['fix_spec'] if f['diagnosis'])}")
    if a.strict and o["status"]["failed"]:
        return gc.EXIT_LINT
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
