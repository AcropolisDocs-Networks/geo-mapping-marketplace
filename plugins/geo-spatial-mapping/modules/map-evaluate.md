---
name: map-evaluate
description: >
  PIPELINE - started only through /geo-spatial-mapping:map-run, or offered by /geo-spatial-mapping:map-trace. Grade finished maps from their
  pictures against the evaluation rubric, by fresh agents, observe only: for improving the plugin later, never for changing a map.
---

# map-evaluate (pipeline, 2.2.0)

Grades finished outputs (a still, an SVG or PDF sheet, a web map's default view, a video's frames, a story's panels) from the pictures
alone, against the rubric in `evaluation/rubric.yaml`. It is **observe only**: its grades never change the map in hand, never feed a
build, and no skill that makes a map reads them. The record is for finding which parts of the plugin to improve.

## Run it

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-evaluate --files a.png,b.svg,page.html,clip.mp4       # or a folder
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-evaluate --outputs <outputs list>                 # what the map trace offers
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-evaluate --option anchors=<anchors.yaml>             # calibration on the anchor set
python3 "$GMAP_ROOT/scripts/pipelines.py" run  map-evaluate <same inputs> --confirm TOKEN [--param model=<model id>]
```

Options: `passes=N` graded reads per output (1 by default; 3 with `mode=calibration` or `anchors=`); `outcome=` and `secondary=` when
the person says what the map is for (explain, compare, locate, inventory, monitor, explore, sequence, evoke); `trace=` the map trace's
JSON whose page gets the Map quality section (the map trace sets it).

1. **Plan and confirm.** Show the cost line (outputs, frames, and how many image reads fresh agents will make) and wait for a clear yes.
2. **Run.** It makes one packet per output in `.geo-mapping/evaluations/<run>/`: the frames, the measurements, the intent card, which
   criteria apply, and the cold-read prompt. Status `awaiting-reads`; `awaiting_reads.evaluation_run` is the run folder.
3. **The reads, by fresh agents only.** Run `python3 "$GMAP_ROOT/scripts/map_evaluate.py" status --run <run folder>`. Each
   `MAP_EVAL_PENDING:` line names a read, its prompt file and its answer file. For each one, start a **new agent** with the agent tool
   (general purpose) and this instruction, and nothing else: *"Read the file <prompt> and follow its instructions exactly. Do nothing
   else."* Never answer a read yourself, never add context, and never pass on what you know about the map: you may have made it, and a
   grader that saw the making grades its own work. All cold reads first (they can run in parallel); then run `status` again, which writes
   the graded prompts (a `save` of a cold read writes its output's graded prompts at once and prints their `MAP_EVAL_PENDING:` lines),
   and start one new agent per graded read. If an agent could not write its file and replied with the JSON, store it
   unchanged with `map_evaluate.py save --run <run folder> --read <id> <file holding the reply>`. With no agent tool in this session, stop
   and say so: the packets are kept and the evaluation can be finished in a session that has one.
4. **Record.** When `MAP_EVAL_READY: yes`, run `python3 "$GMAP_ROOT/scripts/map_evaluate.py" record --run <run folder>` (add
   `--trace <trace JSON>` when the map trace offered it). It writes a scorecard per output (`<output>.eval.json`), one history line per output,
   the improvement backlog and `report.html`, and prints `MAP_EVAL_REPORT:`.
5. **Report.** Send `report.html` (and the map trace's page again when it was updated). Say, per output, Effectiveness, Craft and the works
   verdict, then the two or three components the backlog names most. Keep the two numbers apart; never blend them into one.

## What the grades are and are not

- **Effectiveness** (story, information added, hierarchy, readability, honesty, orientation) and **Craft** (layout, style, medium fit)
  are separate headline numbers, with a **works** verdict (yes, partly, no) beside them: a plain map that does its job is never hidden
  behind a low blended score.
- Which criteria count, and how much, follows the map's outcome and family: an inventory map is not graded on story, a world map not on
  its north arrow. The code settles this before any agent sees the picture.
- Every score has evidence (a box on the frame and what is seen there) or it is void. Improvement candidates are about the plugin's
  parts (`label_placer`, `legend_builder`, `extent_fitting` ...), never instructions for the map in hand.
- The rubric is version 0.x and meant to change. Scores compare only within one rubric major version.

## Other commands

```bash
python3 "$GMAP_ROOT/scripts/map_evaluate.py" trend                                   # Effectiveness and Craft over time (trend.html)
python3 "$GMAP_ROOT/scripts/map_evaluate.py" anchors --set anchors.yaml --owner owner-scores.yaml   # agreement with the owner's scores
python3 "$GMAP_ROOT/scripts/map_evaluate.py" review --days 10                         # the 10-day rubric review
```

## Never

Use a grade to change, rebuild or re-style the map being graded; mention scores in a build summary; let the agent that built a map grade
it; copy scorecards or reports beside a deliverable or into a shared place unless the person asks (they can hold client maps).
