---
description: "Run a map pipeline after confirming its cost: map or web map build, QA, data refresh, format batch, service harvest or refresh, coverage, market, program, network analysis, atlas, library refresh"
---
Start the pipeline the person named. The pipelines are `map-build`, `webmap-build`, `map-qa`, `data-refresh`, `format-batch`, `service-harvest`, `service-refresh`, `coverage-map`, `market-map`, `program-map`, `network-analysis`, `map-atlas-run`, `reference-refresh`, `climate-fetch` (a Copernicus CDS job), `fcc-fetch` (an FCC BDC file over 100 MB) and `flythrough` (a video flown over a 3D web page: a draft first, the final after you confirm); with no name, list them and ask which.

Each pipeline's guide is a module, not a skill: read it with the line below before planning (never with the Skill tool, which has no
pipeline skills to load).

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/modules.py" read <pipeline>       # the pipeline's guide: inputs, options, stages, rules
python3 "$GMAP_ROOT/scripts/pipelines.py" list                # the pipelines, when the person named none
```

1. Read that pipeline's module and `plan` it with the person's inputs, as the module shows. The plan reads and hashes the inputs and changes nothing.
2. Show the cost line and what will be written, and wait for a clear yes. The yes is the confirmation token from that plan; a token from an earlier plan does not carry over, because changing an input changes the token.
3. `run` with the token. Report the outcome from `run.json`: status, the folder, the BLOCK/WARN counts, and the reproducibility digest. For `map-qa`, open `preview.png` and answer the needs-eyes items before giving the verdict. For `webmap-build`, say which engine the page uses and why (`page-report.json`), and its size. A run that stops says why in its exit code: 3 for a check that blocks, 4 for a refusal (a licence, a credential, or a customization the person has not acknowledged: name `/geo-mapping-acropolisdocs:map-services`).

Never run without the person's yes, never pass a token you made up, and never set `include_confidential`, `allow_restricted_data`, `overwrite` or `apply` unless the person asked for it.
