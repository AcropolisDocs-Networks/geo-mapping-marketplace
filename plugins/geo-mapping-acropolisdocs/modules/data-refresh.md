---
name: data-refresh
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Data refresh: ask the services behind
  a map again, report what changed, rebuild the map when a change is applied.
---

# data-refresh (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run data-refresh`. For one capture without a map, the `service-refresh` pipeline.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/pipelines.py" plan data-refresh --spec MAP.yaml [--option out=./maps]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  data-refresh --spec MAP.yaml --confirm TOKEN [--param apply=true]
```

## Flow

1. **Plan.** The cost line counts requests to the public services behind the map. Show it.
2. **Read** each layer: service layers are refreshable, your own kit captures are compared (run the kit again, ingest, then compare), files are fingerprinted, library layers are noted.
3. **Fetch and compare.** Each service layer is asked again into a staging folder with a drift report. Nothing the map uses changes unless `apply=true`.
4. **Rebuild** only when a change was applied (or `force_rebuild=true`).
5. **Summary.** Say what changed per layer, in words, from `change_summary.md`.

## Never

Apply on the person's behalf when the report warns; retry after a 401 or 403.
