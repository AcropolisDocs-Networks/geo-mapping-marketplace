---
name: map-trace
disable-model-invocation: true
description: >
  Troubleshoot this session's use of the map plugin: which skills, scripts and lints ran, what failed and why, and where the time went,
  as an inline report and a branded HTML and PDF page.
---

# map-trace

What this session did with the map plugin, where it failed, and where it went round in circles. One script: an inline report, a
branded HTML and PDF page (1.18.0, laid out like the acropolisdocs run-trace page), and a Markdown report with a fix spec. It is the map plugin's own trace: it reads only this plugin's calls (its skills, its scripts and lints, its OGC
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
   GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
   python3 "$GMAP_ROOT/scripts/map_trace.py"
   ```

2. **Print the inline report verbatim** inside a fenced code block. It is column-aligned; reflowing it breaks the bars.

3. **Complete the fix spec.** When the run prints `MAP_TRACE_FIXSPEC_PENDING: N` with N above 0, open the `MAP_TRACE_REPORT:` file. For each
   finding marked `DIAGNOSIS PENDING (F-n)`, read its evidence, then the plugin file and line its **Crash site** names (`scripts/<name>.py:<line>`
   under `$GMAP_ROOT`), and replace that line with a verdict (`defect`, `not a defect`, or `not diagnosed - reproduce first`) and one to three
   sentences of cause citing the `file:line` you read. Never guess past the evidence. Change nothing else in the report and no plugin source:
   the spec is the deliverable, a fix is a separate change. Confirm no `DIAGNOSIS PENDING` remains.

4. **Deliver.** Send the `MAP_TRACE_HTML:` page, the `MAP_TRACE_PDF:` file and the `MAP_TRACE_REPORT:` file (the Markdown report and fix
   spec) with `SendUserFile`; the `MAP_TRACE_EVENTS:` JSON only if asked. `MAP_TRACE_PDF:` is always printed: a path, or `skipped - <reason>`;
   when it says skipped, say the PDF was not made and why (the map doctor fixes a missing PDF renderer). Without `SendUserFile`, give the paths. Then say, in plain words: how many of the session's calls were the map plugin's, how many failed and their classes, any hotspot or
   iteration loop, and the one or two changes that would save the most time.

5. **Offer the map evaluation (2.2.0).** When the run prints `MAP_TRACE_OUTPUTS:` with a number above 0, the session wrote finished maps,
   and `MAP_TRACE_EVALUATE:` is the cost of grading them from their pictures (outputs, frames, and how many image reads fresh agents will
   make). Show that cost line and ask whether to run it; it is observe only and never changes a map. On a clear yes, read the pipeline's
   module and run the `MAP_TRACE_EVALUATE_RUN:` arguments through `pipelines.py`, then follow the module's steps 3 to 5, recording with
   `--trace` so this report's page gains its Map quality section; send the updated page and the evaluation report. On no, or with
   `MAP_TRACE_EVALUATE: skipped`, say nothing more about it. `--no-evaluate` leaves the offer out; `--evaluate-only` offers it again on
   the newest trace of this session without tracing again (2.4.0: it finds the outputs from the transcript again, so an older page with an
   empty outputs list can still gain its Map quality section).
   When the run prints a reason line, `MAP_TRACE_OUTPUTS: 0 (N finished build(s), no output found: <reason>)` (2.4.0), say that N finished
   builds were not matched to outputs, give the reason, and offer the evaluation it lists (`MAP_TRACE_EVALUATE:` and
   `MAP_TRACE_EVALUATE_RUN:` follow it when those builds' `--out` folders still hold maps). Never report a session that built maps as having
   no outputs without saying why.
   3.4.0: outputs that already have a scorecard are read back into the Map quality section with no new reads, and only the others are
   offered. When the run prints `MAP_TRACE_EVALUATE_AUTO: yes`, the owner set `evaluate: auto` (`~/.geo-mapping/config.yaml` or
   `GMAP_EVALUATE=auto`): run the evaluation without asking, still by fresh agents and still observe only, and say it was run.

   ```bash
   GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
   python3 "$GMAP_ROOT/scripts/modules.py" read map-evaluate
   python3 "$GMAP_ROOT/scripts/pipelines.py" <the MAP_TRACE_EVALUATE_RUN: arguments>
   ```

6. **Say what is measured and what is inferred.** The `Hooks` line says how many map calls the plugin's timing hook measured (1.17.2); those
   durations are measurements, the rest are bounds from transcript timestamps, and with no timing log for the session (`no timing log`)
   all of them are. A build the pre-export hook stopped is a `lint-block` with disposition `input`: the gate worked, so fix the MapSpec, not
   the plugin. "Not running a tool" is a residual (wall
   minus tool time minus idle gaps) that holds model time, runtime overhead and short waits; never call it model generation. A lane marked
   `LOW CONFIDENCE` was built only from calls after a skill load, not from the calls naming the plugin's own scripts. If the transcript line
   says `NEWEST-FALLBACK`, say the session was guessed before quoting a number, and offer `--session <id>`.

## What it reads and what it prints

| Signal | How |
|---|---|
| Map call | The Skill tool loading one of this plugin's skills (`declared`), Python running one of its scripts or lints (`path`), in the Bash or the PowerShell tool (1.17.3), an inline Python program importing them from a `scripts/` folder (`import`, weaker), or a tool of its OGC server (`server`). A `modules.py read` call (1.0.1) is a script call like any other and falls in the window of the skill that asked for the module. Reading a file with `grep`, `sed` or `cat` is not using the plugin. A `skills/<slug>/scripts/` path is another plugin's and is never counted |
| Lane | The skill that runs the script (from this plugin's own skill files), or the skill a load opened; `(name)` for a script no skill runs directly |
| Status | `ok`, `failed` (2.4.0: also an MCP result with `"ok": false`, and a script whose run ledger line has a non-zero exit even when `\| tail` or `> file` hid it), `degraded` (2.4.0: exited 0 but dropped something the spec asked for, such as relief; counted beside failed and listed in the fix spec), `blocked` (the harness or a permission refused it; excluded from the error rate), `unfinished` (no result: interrupted), `self` (this report's own call) |
| Error class | `crash` (a traceback; the crash site is the deepest frame inside this plugin), `lint-block` (exit 3 or blocking MAP-* findings), `usage` (2), `refused` (4), `service` (5), `dependency` (a package or native library missing), `network`, `timeout`, `environment` (permissions, read-only folders), `failed` (the output does not say) |
| Findings named | MAP-* ids in the map calls' output, with the severity printed beside them, ranked BLOCK first |
| Rework | Retry: the same command again with nothing written between. Iteration loop: the same script on the same target again after a write, where the earlier run failed. Re-run after edit: the same shape after a success (normal for visual checks). Hotspot: 3 or more loops on one script. Writes made through the shell are inferred from the command and marked |
| Time | Wall clock, tool time (map tool time on its own), idle gaps of 2 minutes or more, the residual, and the slowest map calls; a map call's time is measured when the timing hook logged it, else bounded |
| Hooks | From the plugin's own hook log (`.geo-mapping/traces/hooks/<session>.ndjson`, joined by the call's id): how many map calls were measured, how many builds the pre-export check stopped (also read from the transcript when there is no log), how many times the credentials reminder fired. Never a command, an argument or a message |
| Pipeline runs | This plugin's run records in the working folder: repeated or blocked pipeline jobs |
| Run ledger (2.4.0) | `.geo-mapping/traces/runs/*.ndjson`: one line per plugin script run (script, option names, exit code, duration, outputs, finding ids), joined to the calls by time and script |
| Outputs (2.2.0, 2.4.0) | From the run ledger first; then paths in the results of the plugin's map-writing scripts, absolute or relative to the call's folder; then the files in the build's `--out` folder whose provenance sidecar was written during the call. Finished maps that ran ok and still exist (previews, mute and draft pictures and sidecars left out); their paths go only into the outputs list beside the report. The map evaluator's own calls are counted on their own line, never as map work |
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
- `--no-pdf`: the HTML page without its PDF. `--no-html`: no page at all (the Markdown and the JSON only).
- `--strict`: exit 3 when a map call failed (for checks in a pipeline).
- `--no-evaluate`: no outputs list and no evaluation offer. `--evaluate-only`: the offer alone, on the newest trace of this session.

## What it does not do

- No token or cost figures (`/geo-mapping-acropolisdocs:map-token-use`), and no analysis of another plugin's calls (the AcropolisDocs documents
  plugin has its own run trace for those).
- No fixes. The fix spec is a register of what the transcript shows; a fix is a separate change, after reproducing it.
- No hooks of its own. It reads the log the plugin's hooks write (1.17.2): the timing hook (`hooks/map_timing.py`) records only this
  plugin's calls, in this plugin's working folder, so it cannot collide with another plugin's hooks or logs (the acropolisdocs run-trace
  keeps its own). Turn the timing hook off in `hooks/hooks.json` (hooks/README.md) and durations go back to bounds.
