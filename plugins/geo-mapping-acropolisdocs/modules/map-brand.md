---
name: map-brand
description: >
  UTILITY MODULE - read by map-create, map-restyle and geo-set-brand; never used for documents, decks, charts or any non-map content.
  Resolves the map's brand pack (the shipped brand, or a pack the person added) into map tokens.
---

# map-brand

The only brand surface in geo-mapping-acropolisdocs, and map-scoped by construction. The brand is resolved, never asked:
named by the user, then the person's stored default, then the plugin's default `acropolisdocs`. The plugin ships that one brand; any
other is a brand pack the person added with `/geo-mapping-acropolisdocs:brand` (kept outside the plugin, in `.geo-mapping/brands/`), resolved the same way.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
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
