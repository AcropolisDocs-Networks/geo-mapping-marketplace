---
name: map-build
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Map build: from a MapSpec to a
  finished, linted package (PDF, PNG, SVG, the MapSpec, provenance, lint report, methods note, optional data package).
---

# map-build (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run map-build`. Never begin it because a map was mentioned; `map-create` is the conversational route.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-build --spec MAP.yaml --option out=./maps [--option formats=pdf,png,svg] [--option data_package=true]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  map-build --spec MAP.yaml --option out=./maps --confirm TOKEN
```

## Flow

1. **Plan.** Show the cost line the plan returns (renders, files, minutes). The build never touches the network; service layers are drawn from existing captures.
2. **Confirm.** Run only with the token from that plan. If the MapSpec or a layer file changed since, the token no longer fits: plan again.
3. **Stages.** probe the tier, lint the MapSpec, prepare and render with every lint, write the package, then the data package if asked.
4. **Report.** Give the folder, the lint counts (BLOCK/WARN/INFO) and anything the run flagged. A BLOCK stops the run before packaging; name the rule and the fix.
   List the run's `map_text.callout` lines as written (MAP-TEXT findings: the text found and one suggested fix, never blocking; the `map-text`
   module has the rules). Do not rewrite the MapSpec's title or subtitle unless the person asks.

## Package layout

`<out>/<map id>/` holds the map files, `mapspec.yaml`, `provenance.json`, `lint_report.md`, `methods.md`, `manifest.json` (every file with its SHA-256), and `data/` only when asked.

## Data package gates

Layer data is written only when `data_package=true`. A layer whose license does not allow redistribution stops it (MAP-LIC-003); a client-confidential layer goes in only after the person says so (`include_confidential=true`, MAP-LIC-007). Never set either on your own.

## Never

Run without a matching token; overwrite an existing package without `overwrite=true`; put raw coordinates of a confidential layer in a package unasked.
