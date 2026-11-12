---
name: map-trace
disable-model-invocation: true
description: >
  Troubleshoot this session's use of the map plugin: which skills, scripts and lints ran, what failed and why, and where the time went.
---

# map-trace

What this session did with the map plugin, where it failed, and where it went round in circles. One script, one inline report, one report
file with a fix spec. It is the map plugin's own trace: it reads only this plugin's calls (its skills, its scripts and lints, its OGC
server), never another plugin's trace files, and writes only `map-trace_*` files in the map working folder, so it runs beside the
acropolisdocs `run-trace` without touching it. Use that one for acropolisdocs work; use this one for maps.

## When to run

When the person asks to trace or troubleshoot the map plugin's work in this session ("why did the map build keep failing", "map trace",
"which map step failed"), or runs `/geo-mapping-acropolisdocs:map-trace`. Not for token use: that is `/geo-mapping-acropolisdocs:map-token-use`. Not for whether
the machine can make maps at all: that is `/geo-mapping-acropolisdocs:map-doctor`.

## Steps

1. **Run the script.** It finds the current session's transcript by itself (the session id the runtime sets, else a session id in the
   scratchpad path, else the only transcript; a newest-file guess is reported as `NEWEST-FALLBACK`).

   ```bash
   # Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
   python3 "$GMAP_ROOT/scripts/map_trace.py"
   ```

2. **Print the inline report verbatim** inside a fenced code block. It is column-aligned; reflowing it breaks the bars.

3. **Complete the fix spec.** When the run prints `MAP_TRACE_FIXSPEC_PENDING: N` with N above 0, open the `MAP_TRACE_REPORT:` file. For each
   finding marked `DIAGNOSIS PENDING (F-n)`, read its evidence, then the plugin file and line its **Crash site** names (`scripts/<name>.py:<line>`
   under `$GMAP_ROOT`), and replace that line with a verdict (`defect`, `not a defect`, or `not diagnosed - reproduce first`) and one to three
   sentences of cause citing the `file:line` you read. Never guess past the evidence. Change nothing else in the report and no plugin source:
   the spec is the deliverable, a fix is a separate change. Confirm no `DIAGNOSIS PENDING` remains.

4. **Deliver.** Send the `MAP_TRACE_REPORT:` file (the Markdown report and fix spec) with `SendUserFile`; the `MAP_TRACE_EVENTS:` JSON only if
   asked. Then say, in plain words: how many of the session's calls were the map plugin's, how many failed and their classes, any hotspot or
   iteration loop, and the one or two changes that would save the most time.

5. **Say what is inferred.** Durations are bounds from transcript timestamps, not measurements. "Not running a tool" is a residual (wall
   minus tool time minus idle gaps) that holds model time, runtime overhead and short waits; never call it model generation. A lane marked
   `LOW CONFIDENCE` was built only from calls after a skill load, not from the calls naming the plugin's own scripts. If the transcript line
   says `NEWEST-FALLBACK`, say the session was guessed before quoting a number, and offer `--session <id>`.

## What it reads and what it prints

| Signal | How |
|---|---|
| Map call | The Skill tool loading one of this plugin's skills (`declared`), Python running one of its scripts or lints (`path`), an inline Python program importing them from a `scripts/` folder (`import`, weaker), or a tool of its OGC server (`server`). A `modules.py read` call (1.0.1) is a script call like any other and falls in the window of the skill that asked for the module. Reading a file with `grep`, `sed` or `cat` is not using the plugin. A `skills/<slug>/scripts/` path is another plugin's and is never counted |
| Lane | The skill that runs the script (from this plugin's own skill files), or the skill a load opened; `(name)` for a script no skill runs directly |
| Status | `ok`, `failed`, `blocked` (the harness or a permission refused it; excluded from the error rate), `unfinished` (no result: interrupted), `self` (this report's own call) |
| Error class | `crash` (a traceback; the crash site is the deepest frame inside this plugin), `lint-block` (exit 3 or blocking MAP-* findings), `usage` (2), `refused` (4), `service` (5), `dependency` (a package or native library missing), `network`, `timeout`, `environment` (permissions, read-only folders), `failed` (the output does not say) |
| Findings named | MAP-* ids in the map calls' output, with the severity printed beside them, ranked BLOCK first |
| Rework | Retry: the same command again with nothing written between. Iteration loop: the same script on the same target again after a write, where the earlier run failed. Re-run after edit: the same shape after a success (normal for visual checks). Hotspot: 3 or more loops on one script. Writes made through the shell are inferred from the command and marked |
| Time | Wall clock, tool time (map tool time on its own), idle gaps of 2 minutes or more, the residual, and the slowest map calls |
| Pipeline runs | This plugin's run records in the working folder: repeated or blocked pipeline jobs |
| Fix spec | One finding per class, script and crash site, each with one disposition: `product` (a crash inside the plugin: needs a diagnosis), `input` (a gate, the spec, the data or the arguments stopped it), `dependency`, `environment`, `operator` (a traceback with no frame in the plugin: the session's own inline script), `unclassified`; severity by whether a later run of the same target recovered |

Printed: skill, script and tool names, option names (never their values), call numbers, timings, exit codes, MAP-* ids, and for each
failure one line of its output, with keys and tokens masked, every URL replaced by `<url>`, every path cut to its last part, at most 160
characters. Never message text, command text or a full path. Every transcript line is scrubbed of key, token and password patterns before
anything reads it (0.6.0), so a key someone pasted into the session never reaches the report.

## Options (only if the person asks)

- `--since 30m|2h|1d|ISO`, `--until ISO`: only that window of the session.
- `--session <id>` or `--transcript <file>`: another session; `map_trace.py find` lists the transcripts on this machine.
- `--out-dir <dir>`: where the report files go (default `.geo-mapping/traces/`; `GEO_MAPPING_TRACE_DIR` overrides). `--no-files`: inline only.
- `--format json`: the summary as JSON instead of the inline report.
- `--strict`: exit 3 when a map call failed (for checks in a pipeline).

## What it does not do

- No token or cost figures (`/geo-mapping-acropolisdocs:map-token-use`), and no analysis of another plugin's calls (the AcropolisDocs documents
  plugin has its own run trace for those).
- No fixes. The fix spec is a register of what the transcript shows; a fix is a separate change, after reproducing it.
- No hooks. It installs nothing that runs on every tool call, so it adds nothing to a session and cannot collide with another plugin's
  hooks; without a hook log, durations stay bounds.
