---
name: format-batch
description: >
  PIPELINE - started only through /geo-spatial-mapping:map-run, which reads this guide before planning. Format batch: convert a folder of
  geodata to one format, verify every result, and write a report with the ogr2ogr equivalents.
---

# format-batch (pipeline)

Started by `/geo-spatial-mapping:map-run format-batch`. One file: `geo-convert`.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/pipelines.py" plan format-batch --src ./in --option out=./out --option to=gpkg [--option recursive=true]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  format-batch --src ./in --option out=./out --option to=gpkg --confirm TOKEN
```

## Flow

1. **Scan** the folder for geodata files; the plan counts files and bytes.
2. **Plan one output per file or layer.** Two inputs that would write the same name, or an existing output without `overwrite=true`, stop the run (MAP-RUN-004).
3. **Convert** through `geo-convert`, then **verify**: each result is read back and its feature count compared (MAP-OUT-005).
4. **Report.** `format_batch_report.md` lists each file, result and the equivalent ogr2ogr command.

## Never

Overwrite or delete the inputs; report a file converted before it has been read back.
