---
name: map-atlas-run
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Map atlas: one map per value of a
  field from one MapSpec, with shared class edges, an index page and one combined PDF.
---

# map-atlas-run (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run map-atlas-run`. Never begin it because a series of maps was mentioned; `map-atlas` is the conversational route.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-atlas-run --spec MAP.yaml --option layer=ID --option field=COL --option out=./atlas [--option mode=filter|focus] [--option values=a,b,c] [--option formats=pdf,png,svg]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  map-atlas-run --spec MAP.yaml --option layer=ID --option field=COL --option out=./atlas [...] --confirm TOKEN
```

## Flow

1. **Plan.** The plan reads the data, lists the values, and shows the cost: N renders and about N times the formats in files. A MapSpec with a join table or data file that changes later changes the token.
2. **Confirm.** Run only with the token from that plan.
3. **Stages.** probe, atlas-plan (values, shared class edges, one MapSpec per map in the run folder), build (every map built and linted; findings carry the value in brackets; MAP-CART-014 checks the classes match), assemble (index pages and the combined PDF with bookmarks), package.
4. **Report.** Give the folder, the page count, the BLOCK/WARN counts, and anything the index page says about comparability.

## Package layout

`<out>/<map id>-atlas/` holds `<map id>-atlas.pdf`, `index.csv` (page, value, title, features, folder), `atlas.json` (layer, field, mode, values, class edges), `mapspec.yaml` (the original), `lint_report.md`, `methods.md`, `maps/<value>/` (each map's files and `provenance.json`), and `manifest.json` (every file with its SHA-256).

## Never

Run without a matching token; change the person's MapSpec (the per-map copies live in the run folder); make more than `max_maps` maps without the person raising it; present maps with different classes as comparable.
