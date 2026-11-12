#!/usr/bin/env python3
"""Session reports (spec 7.5): where a session's tokens went, and where its time went.

    session_report.py tokens [--transcript FILE] [--format text|json]
    session_report.py trace  [--transcript FILE] [--format text|json] [--idle-seconds 120]
    session_report.py find                                            list transcripts this report can read

Two sources, both local and read-only:
  * the session transcript (a JSONL file Claude writes: per-reply token counts, tool calls with their start and finish times),
  * this plugin's run records in the working folder (`.geo-mapping/runs/*/run.json`: pipeline, status, seconds per stage).

Only counts, tool names, script names and timings are read out. No message text, command text or file path from the transcript is printed.
Without a transcript the trace still works from the run records; the token report needs the transcript and says so.
Token figures for a tool's output are an estimate (characters / 4); the per-reply counts come from the transcript as written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

CHARS_PER_TOKEN = 4
SCRIPT = re.compile(r"scripts/([a-z0-9_]+)\.py")
TOOLS_OF_INTEREST = 10


class ReportError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- reading
def candidates() -> list[Path]:
    """Transcript files this machine holds, newest first. CLAUDE_TRANSCRIPT overrides; otherwise ~/.claude/projects/*/*.jsonl."""
    out: list[Path] = []
    env = os.environ.get("CLAUDE_TRANSCRIPT")
    if env and Path(env).is_file():
        out.append(Path(env))
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    if root.is_dir():
        out += [p for p in root.glob("*/*.jsonl") if ".precompact" not in p.name and not p.name.endswith(".hooks.ndjson")]
    seen, res = set(), []
    for p in sorted(set(out), key=lambda p: p.stat().st_mtime, reverse=True):
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def parse_ts(s) -> dt.datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _size(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_size(c.get("text") if isinstance(c, dict) and "text" in c else c.get("content") if isinstance(c, dict) else c) for c in content)
    return 0


def scrubbed_record(line: str) -> dict | None:
    """One transcript line as a record, with every secret pattern scrubbed before anything reads it (0.6.0, credentials spec §7.2): a key pasted
    into the session never reaches a report. The line is scrubbed as text; when that breaks its JSON, it is parsed and every string scrubbed."""
    import redact
    try:
        d = json.loads(redact.scrub(line)[0])
    except json.JSONDecodeError:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        d = redact.scrub_obj(d)[0]
    return d if isinstance(d, dict) else None


def load(path: Path) -> dict:
    """Read a transcript into: replies (de-duplicated by message id), calls (tool use with start, end, size), user turns."""
    replies: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    turns = 0
    first = last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = scrubbed_record(line)
            if d is None:
                continue
            ts = parse_ts(d.get("timestamp"))
            if ts:
                first, last = first or ts, ts
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if d.get("type") == "assistant" and msg:
                mid = msg.get("id") or d.get("uuid")
                u = msg.get("usage") or {}
                if mid not in replies:
                    replies[mid] = {"ts": ts, "model": msg.get("model"), "input": u.get("input_tokens", 0) or 0, "output": u.get("output_tokens", 0) or 0,
                                    "cache_read": u.get("cache_read_input_tokens", 0) or 0, "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                                    "thinking": ((u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0)}
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        cmd = (c.get("input") or {}).get("command") if isinstance(c.get("input"), dict) else None
                        scripts = SCRIPT.findall(cmd) if isinstance(cmd, str) else []
                        calls[c["id"]] = {"name": c.get("name"), "start": ts, "end": None, "result_chars": 0, "scripts": scripts, "error": False,
                                          "input_chars": len(json.dumps(c.get("input") or {}))}
            elif d.get("type") == "user" and msg:
                content = msg.get("content")
                if isinstance(content, list):
                    got_result = False
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                            k = calls[c["tool_use_id"]]
                            k["end"], k["result_chars"], k["error"] = ts, _size(c.get("content")), bool(c.get("is_error"))
                            got_result = True
                    if not got_result:
                        turns += 1
                elif isinstance(content, str):
                    turns += 1
    return {"path": str(path), "replies": list(replies.values()), "calls": list(calls.values()), "user_turns": turns, "first": first, "last": last}


def load_runs() -> list[dict]:
    root = gc.workdir() / "runs"
    out = []
    for p in sorted(root.glob("*/run.json")) if root.is_dir() else []:
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        plan = {}
        pp = p.parent / "plan.json"
        if pp.is_file():
            try:
                plan = json.loads(pp.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        inp = plan.get("inputs") or {}
        key = inp.get("spec") or inp.get("sites") or inp.get("table") or inp.get("tracker") or inp.get("src") or inp.get("capture") or ""
        out.append({"run_id": r.get("run_id"), "pipeline": r.get("pipeline"), "status": r.get("status"), "started": parse_ts(r.get("started_at")), "finished": parse_ts(r.get("finished_at")),
                    "seconds": r.get("seconds") or 0, "stages": r.get("stages") or [], "target": Path(key).name if key else "", "manifest_id": r.get("manifest_id"),
                    "blocking": sorted({f["id"] for f in r.get("findings") or [] if f.get("severity") == "BLOCK"})})
    out.sort(key=lambda r: r["started"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return out


# ---------------------------------------------------------------------------------------------------------------- tokens
def fmt_n(n: float) -> str:
    return f"{int(n):,}"


def tokens(t: dict) -> dict:
    rs = t["replies"]
    tot = {k: sum(r[k] for r in rs) for k in ("input", "output", "cache_read", "cache_write", "thinking")}
    fresh_in = tot["input"] + tot["cache_write"]
    reads = tot["cache_read"]
    ctx = [r["input"] + r["cache_read"] + r["cache_write"] for r in rs]
    by_tool: dict[str, dict] = {}
    for c in t["calls"]:
        d = by_tool.setdefault(c["name"] or "?", {"calls": 0, "result_tokens": 0, "input_tokens": 0, "errors": 0})
        d["calls"] += 1
        d["result_tokens"] += c["result_chars"] // CHARS_PER_TOKEN
        d["input_tokens"] += c["input_chars"] // CHARS_PER_TOKEN
        d["errors"] += 1 if c["error"] else 0
    by_script: dict[str, dict] = {}
    for c in t["calls"]:
        for s in dict.fromkeys(c["scripts"]):
            d = by_script.setdefault(s, {"calls": 0, "result_tokens": 0})
            d["calls"] += 1
            d["result_tokens"] += c["result_chars"] // CHARS_PER_TOKEN
    big = sorted(({"tool": c["name"], "tokens": c["result_chars"] // CHARS_PER_TOKEN, "scripts": c["scripts"][:2]} for c in t["calls"]), key=lambda x: -x["tokens"])[:5]
    growth = None
    if len(ctx) >= 4:
        q = max(1, len(ctx) // 4)
        growth = {"start": int(sum(ctx[:q]) / q), "end": int(sum(ctx[-q:]) / q)}
    out = {"transcript": Path(t["path"]).name, "replies": len(rs), "user_turns": t["user_turns"], "tool_calls": len(t["calls"]), "models": sorted({r["model"] for r in rs if r["model"]}),
           "totals": tot, "cache_hit_share": round(reads / (reads + fresh_in), 3) if reads + fresh_in else None, "largest_context": max(ctx) if ctx else 0, "context_growth": growth,
           "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1]["result_tokens"])[:TOOLS_OF_INTEREST]),
           "by_plugin_script": dict(sorted(by_script.items(), key=lambda kv: -kv[1]["result_tokens"])[:TOOLS_OF_INTEREST]), "largest_results": big, "note": "Tool-output tokens are estimated at characters / 4."}
    out["advice"] = advise_tokens(out)
    return out


def advise_tokens(o: dict) -> list[str]:
    a = []
    tot_res = sum(v["result_tokens"] for v in o["by_tool"].values()) or 1
    top = next(iter(o["by_tool"].items()), None)
    if top and top[1]["result_tokens"] / tot_res > 0.5 and top[1]["result_tokens"] > 20_000:
        a.append(f"{top[0]} output is {round(100 * top[1]['result_tokens'] / tot_res)}% of what tools returned; ask for counts, schema and paths instead of whole data, and write bulk results to files.")
    if o["largest_results"] and o["largest_results"][0]["tokens"] > 15_000:
        a.append(f"One tool result was about {fmt_n(o['largest_results'][0]['tokens'])} tokens; page or filter large reads.")
    g = o.get("context_growth")
    if g and g["end"] > 3 * max(1, g["start"]):
        a.append(f"The conversation grew from about {fmt_n(g['start'])} to {fmt_n(g['end'])} tokens per reply; start a fresh session for a new job and keep decisions in files.")
    if o["cache_hit_share"] is not None and o["cache_hit_share"] < 0.5 and o["replies"] > 10:
        a.append("Less than half of the input was served from cache; long pauses or changing the early part of a conversation cost more.")
    if o["totals"]["thinking"] and o["totals"]["output"] and o["totals"]["thinking"] / o["totals"]["output"] > 0.7:
        a.append("Most output was reasoning, not text; a more exact request reduces it.")
    return a or ["Nothing stands out."]


def tokens_text(o: dict) -> str:
    t = o["totals"]
    L = [f"Token use, {o['transcript']}", "", f"{o['replies']} replies, {o['user_turns']} messages from you, {o['tool_calls']} tool calls. Models: {', '.join(o['models']) or 'unknown'}.", "",
         f"New input {fmt_n(t['input'] + t['cache_write'])} (of which written to cache {fmt_n(t['cache_write'])}); read from cache {fmt_n(t['cache_read'])}; output {fmt_n(t['output'])} (reasoning {fmt_n(t['thinking'])}).",
         f"Cache hit share of input: {round(100 * o['cache_hit_share'])}%." if o["cache_hit_share"] is not None else "", f"Largest context in one reply: {fmt_n(o['largest_context'])} tokens.", ""]
    L.append("Where tool output went (estimated):")
    for k, v in o["by_tool"].items():
        L.append(f"  {k:<18} {v['calls']:>4} calls  {fmt_n(v['result_tokens']):>10} tokens back" + (f"  {v['errors']} errors" if v["errors"] else ""))
    if o["by_plugin_script"]:
        L += ["", "By plugin script:"] + [f"  {k:<22} {v['calls']:>4} calls  {fmt_n(v['result_tokens']):>10} tokens back" for k, v in o["by_plugin_script"].items()]
    L += ["", "What to change:"] + [f"  - {x}" for x in o["advice"]] + ["", o["note"]]
    return "\n".join(x for x in L if x is not None).replace("\n\n\n", "\n\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------- trace
def secs(a, b) -> float:
    return max(0.0, (b - a).total_seconds()) if a and b else 0.0


def fmt_d(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def rework(runs: list[dict]) -> dict:
    """Runs of the same pipeline on the same target. The last good run is the result; earlier ones and blocked ones are rework."""
    groups: dict[tuple, list[dict]] = {}
    for r in runs:
        groups.setdefault((r["pipeline"], r["target"]), []).append(r)
    rows, wasted, total = [], 0.0, sum(r["seconds"] for r in runs)
    for (pipe, target), rs in groups.items():
        if len(rs) < 2 and rs[0]["status"] == "ok":
            continue
        final = next((r for r in reversed(rs) if r["status"] == "ok"), None)
        lost = sum(r["seconds"] for r in rs if r is not final)
        wasted += lost
        rows.append({"pipeline": pipe, "target": target or "-", "runs": len(rs), "statuses": [r["status"] for r in rs], "blocking": sorted({b for r in rs for b in r["blocking"]}),
                     "seconds_before_final": round(lost, 1), "ended_ok": final is not None})
    rows.sort(key=lambda r: -r["seconds_before_final"])
    return {"groups": rows, "seconds_lost": round(wasted, 1), "share_of_run_time": round(wasted / total, 3) if total else 0.0}


def trace(t: dict | None, runs: list[dict], idle_seconds: int = 120) -> dict:
    out: dict = {"transcript": Path(t["path"]).name if t else None, "runs": len(runs)}
    if t and t["first"] and t["last"]:
        wall = secs(t["first"], t["last"])
        calls = [c for c in t["calls"] if c["start"] and c["end"]]
        tool_time = sum(secs(c["start"], c["end"]) for c in calls)
        evs = sorted([c["start"] for c in t["calls"] if c["start"]] + [c["end"] for c in t["calls"] if c["end"]] + [r["ts"] for r in t["replies"] if r["ts"]])
        idle = sum(g for g in (secs(a, b) for a, b in zip(evs, evs[1:])) if g > idle_seconds)
        by_tool: dict[str, dict] = {}
        for c in calls:
            d = by_tool.setdefault(c["name"] or "?", {"calls": 0, "seconds": 0.0, "slowest": 0.0})
            s = secs(c["start"], c["end"])
            d["calls"] += 1
            d["seconds"] += s
            d["slowest"] = max(d["slowest"], s)
        slow = sorted(({"tool": c["name"], "seconds": round(secs(c["start"], c["end"]), 1), "scripts": c["scripts"][:2], "error": c["error"]} for c in calls), key=lambda x: -x["seconds"])[:5]
        out["session"] = {"started": t["first"].isoformat(), "wall_seconds": round(wall), "tools_seconds": round(tool_time), "waiting_seconds": round(idle), "model_seconds": round(max(0.0, wall - tool_time - idle)),
                          "by_tool": {k: {**v, "seconds": round(v["seconds"], 1), "slowest": round(v["slowest"], 1)} for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["seconds"])[:TOOLS_OF_INTEREST]},
                          "slowest_calls": slow, "failed_calls": sum(1 for c in t["calls"] if c["error"]), "idle_threshold": idle_seconds}
    if runs:
        stage_t: dict[str, float] = {}
        for r in runs:
            for s in r["stages"]:
                stage_t[f"{r['pipeline']}/{s['id']}"] = stage_t.get(f"{r['pipeline']}/{s['id']}", 0.0) + (s.get("seconds") or 0)
        out["runs_detail"] = [{"run": r["run_id"], "pipeline": r["pipeline"], "target": r["target"] or "-", "status": r["status"], "seconds": round(r["seconds"], 1), "blocking": r["blocking"]} for r in runs]
        out["slowest_stages"] = [{"stage": k, "seconds": round(v, 1)} for k, v in sorted(stage_t.items(), key=lambda kv: -kv[1])[:5]]
        out["rework"] = rework(runs)
    out["advice"] = advise_trace(out)
    return out


def advise_trace(o: dict) -> list[str]:
    a = []
    s = o.get("session")
    if s and s["wall_seconds"]:
        if s["waiting_seconds"] / s["wall_seconds"] > 0.5:
            a.append("Most of the elapsed time was waiting between steps, not work; the trace shows the work itself.")
        if s["tools_seconds"] / s["wall_seconds"] > 0.5:
            top = next(iter(s["by_tool"].items()), None)
            if top:
                a.append(f"Tools took {round(100 * s['tools_seconds'] / s['wall_seconds'])}% of the time, mostly {top[0]}; run long steps once, with the right options, instead of retrying.")
        if s["failed_calls"] >= 5:
            a.append(f"{s['failed_calls']} tool calls failed; look at the first failure of each kind before retrying.")
    rw = o.get("rework")
    if rw and rw["groups"]:
        worst = rw["groups"][0]
        a.append(f"{len(rw['groups'])} job(s) were run more than once or ended blocked; the most rework was {worst['pipeline']} on {worst['target']} ({worst['runs']} runs"
                 + (f", blocked by {', '.join(worst['blocking'][:3])}" if worst["blocking"] else "") + "). Check a MapSpec with map-qa or the spec lint before a build, and read the plan before confirming.")
    return a or ["Nothing stands out."]


def trace_text(o: dict) -> str:
    L = ["Run trace" + (f", {o['transcript']}" if o.get("transcript") else " (from this plugin's run records; no transcript was found)"), ""]
    s = o.get("session")
    if s:
        L += [f"Elapsed {fmt_d(s['wall_seconds'])}: tools {fmt_d(s['tools_seconds'])}, waiting {fmt_d(s['waiting_seconds'])} (gaps over {s['idle_threshold']} s), the rest model time {fmt_d(s['model_seconds'])}.", "", "By tool:"]
        L += [f"  {k:<18} {v['calls']:>4} calls  {fmt_d(v['seconds']):>9}  slowest {fmt_d(v['slowest'])}" for k, v in s["by_tool"].items()]
        L += ["", "Slowest calls:"] + [f"  {fmt_d(c['seconds']):>8}  {c['tool']}" + (f" ({', '.join(c['scripts'])})" if c["scripts"] else "") + ("  FAILED" if c["error"] else "") for c in s["slowest_calls"]]
        L.append("")
    if o.get("runs_detail"):
        L.append(f"{o['runs']} pipeline run(s):")
        L += [f"  {r['run']:<34} {r['status']:<8} {fmt_d(r['seconds']):>8}  {r['target']}" + (f"  blocked by {', '.join(r['blocking'][:3])}" if r["blocking"] else "") for r in o["runs_detail"]]
        L += ["", "Slowest stages: " + ", ".join(f"{x['stage']} {fmt_d(x['seconds'])}" for x in o["slowest_stages"])]
        rw = o["rework"]
        L += ["", f"Rework: {len(rw['groups'])} job(s) repeated or blocked; {fmt_d(rw['seconds_lost'])} of run time ({round(100 * rw['share_of_run_time'])}%) went on runs that were not the final good one."]
        L += [f"  {g['pipeline']} on {g['target']}: {g['runs']} runs ({', '.join(g['statuses'])})" for g in rw["groups"][:5]]
        L.append("")
    L += ["What to change:"] + [f"  - {x}" for x in o["advice"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["tokens", "trace", "find"])
    ap.add_argument("--transcript")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--idle-seconds", type=int, default=120)
    a = ap.parse_args(argv)
    if a.op == "find":
        c = candidates()
        print(json.dumps([{"file": p.name, "bytes": p.stat().st_size, "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"), "path": str(p)} for p in c[:10]], indent=2))
        return gc.EXIT_OK if c else gc.EXIT_USAGE
    path = Path(a.transcript) if a.transcript else (candidates() or [None])[0]
    t = None
    if path is not None:
        if not path.is_file():
            print(f"{path} is not a file", file=sys.stderr)
            return gc.EXIT_USAGE
        t = load(path)
    if a.op == "tokens":
        if t is None:
            print("No session transcript was found on this machine, and the token report needs one. Pass --transcript FILE (a Claude .jsonl transcript), or run this where the session was recorded.", file=sys.stderr)
            return gc.EXIT_USAGE
        o = tokens(t)
        print(json.dumps(o, indent=2, default=str) if a.format == "json" else tokens_text(o))
        return gc.EXIT_OK
    runs = load_runs()
    if t is None and not runs:
        print("Nothing to trace: no session transcript and no pipeline runs in this working folder.", file=sys.stderr)
        return gc.EXIT_USAGE
    o = trace(t, runs, a.idle_seconds)
    print(json.dumps(o, indent=2, default=str) if a.format == "json" else trace_text(o))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
