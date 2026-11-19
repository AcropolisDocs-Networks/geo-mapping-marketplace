---
name: map-qa
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Map QA: a SHIP, FIX or BLOCK verdict
  on a MapSpec and/or finished map files, with the visual checklist and a preview image.
---

# map-qa (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run map-qa`.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/pipelines.py" plan map-qa [--spec MAP.yaml] [--files a.svg,b.pdf,c.png]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  map-qa [--spec ...] [--files ...] --confirm TOKEN
```

## Flow

1. **Plan and confirm** as for every pipeline.
2. **Checks.** The MapSpec is linted and built to a preview; finished SVG, PDF and PNG files get the output checks (size, one page, fonts, metadata, leaks).
3. **Verdict.** BLOCK when any check blocks; FIX when a cartographic, accessibility, brand, license, output, data, spec or CRS warning is open or a checklist item fails; otherwise SHIP.
4. **Look at the picture.** `preview.png` is in the run folder. The items marked `needs-eyes` (can a stranger state the message in five seconds, overlaps, legend) are the person's call, or yours with the preview in view: open it and answer them before calling a map shipped.

## Never

Call a map SHIP while a `needs-eyes` item is unanswered without saying so; edit the map during QA.
