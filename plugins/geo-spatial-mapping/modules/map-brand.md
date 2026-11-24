---
name: map-brand
description: >
  UTILITY MODULE - read by map-create, map-restyle and geo-set-brand; never used for documents, decks, charts or any non-map content.
  Resolves the map's brand pack (the shipped brand, or a pack the person added) into map tokens.
---

# map-brand

The only brand surface in geo-spatial-mapping, and map-scoped by construction. The brand is resolved, never asked:
named by the user, then the person's stored default, then the plugin's default `acropolisdocs`. The plugin ships that one brand; any
other is a brand pack the person added with `/geo-spatial-mapping:brand` (kept outside the plugin, in `.geo-mapping/brands/`), resolved the same way.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
export GMAP_BUILD_TOKEN=$(python3 "$GMAP_ROOT/scripts/env_probe.py" --begin-build --spec map.yaml --value)   # or --token
python3 "$GMAP_ROOT/scripts/map_brand.py" resolve --spec map.yaml [--request-brand SLUG]
python3 "$GMAP_ROOT/scripts/map_brand.py" tokens  --spec map.yaml --mode print|screen      # print = theme light (default), screen = theme dark
python3 "$GMAP_ROOT/scripts/map_brand.py" logo    --spec map.yaml --ground light|dark|#RRGGBB [--variant secondary]
python3 "$GMAP_ROOT/scripts/map_brand.py" fonts   --spec map.yaml [--files]
python3 "$GMAP_ROOT/scripts/map_brand.py" guard   --spec map.yaml --file out.svg
python3 "$GMAP_ROOT/scripts/map_brand.py" props   --spec map.yaml --format pdf|svg|html|png|gpkg|geojson|kml
python3 "$GMAP_ROOT/scripts/map_brand.py" voice-check --spec map.yaml --text "..."
```

**Hard rules**
1. Needs a MapSpec and a build token. Without both it exits 6 ("brand request outside a map build") and prints nothing.
2. Returns nothing for documents, decks, charts, wiki pages or other plugins' output.
3. Typeface is Inter for every brand; a brand file's typography is ignored (a style pack may bring its own faces).
4. The house accreditation is carried only in hidden metadata (`props`). It is never drawn, spoken or put in alt text.
5. The brand's own trademark line (`trademark_line` from `props`) is visible when the pack defines one.
6. An unknown brand stops with one line naming the brands that are here; it is never substituted silently.
