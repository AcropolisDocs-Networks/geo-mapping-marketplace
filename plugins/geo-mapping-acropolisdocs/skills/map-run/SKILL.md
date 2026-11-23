---
name: map-run
description: "Run a map pipeline after confirming its cost: map or web map build, QA, data refresh, format batch, service harvest or refresh, coverage, market, program, network analysis, atlas, library refresh"
---
Start the pipeline the person named. The pipelines are `map-build`, `webmap-build`, `map-qa`, `data-refresh`, `format-batch`, `service-harvest`, `service-refresh`, `coverage-map`, `market-map`, `program-map`, `network-analysis`, `map-atlas-run`, `reference-refresh`, `climate-fetch` (a Copernicus CDS job), `fcc-fetch` (an FCC BDC file over 100 MB), `flythrough` (a video flown over a 3D web page: a draft first, the final after you confirm), `map-evaluate` (grades finished maps from their pictures for improving the plugin, observe only: it never changes a map) and `move-data` (2.6.0: files fetched on the person's computer and staged here, unzipped into the work folder's data/ in one step with sizes and hashes recorded); with no name, list them and ask which.

3.7.0: every input a pipeline declares is reachable from the command line: `--page`, `--dataset`, `--request`, `--file-id`, or
`--input KEY=VALUE`. The confirm token covers the inputs and options only (a flythrough's estimate no longer changes it); when an estimate
moves by more than half, `run` says so and asks again with the same token. A flythrough takes `camera=street|neighbourhood|city` (900,
1,500 or 4,000 m kept in view) or `lookahead_m`, `smooth_m`, `rail_m`; its draft frames the same ground as the final; a resume after a
credit or title change composites the frames again without drawing them again; credits shorten to their organizations (then two lines)
before a draft would stop.

Each pipeline's guide is a module, not a skill: read it with the line below before planning (never with the Skill tool, which has no
pipeline skills to load).

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read <pipeline>       # the pipeline's guide: inputs, options, stages, rules
python3 "$GMAP_ROOT/scripts/pipelines.py" list                # the pipelines, when the person named none
```

1. Read that pipeline's module and `plan` it with the person's inputs, as the module shows. The plan reads and hashes the inputs and changes nothing.
2. Show the cost line and what will be written, and wait for a clear yes. The yes is the confirmation token from that plan; a token from an earlier plan does not carry over, because changing an input changes the token.
3. `run` with the token. Report the outcome from `run.json`: status, the folder, the BLOCK/WARN counts, and the reproducibility digest. For `map-qa`, open `preview.png` and `mute.png` (the sheet without its words, 1.22.0) and answer the needs-eyes items before giving the verdict. For `map-evaluate` the run stops at `awaiting-reads`: follow its module's steps 3 to 5 (fresh agents make the reads, then `record`). For `webmap-build`, say which engine the page uses and why (`page-report.json`), and its size. A run that stops says why in its exit code: 3 for a check that blocks, 4 for a refusal (a licence, a credential, or a customization the person has not acknowledged: name `/geo-mapping-acropolisdocs:map-services`).

Never run without the person's yes, never pass a token you made up, and never set `include_confidential`, `allow_restricted_data`, `overwrite` or `apply` unless the person asked for it.
