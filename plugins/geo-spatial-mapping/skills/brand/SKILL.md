---
name: brand
description: List, add, make or set the brand for maps made with geo-spatial-mapping
argument-hint: "[list | set SLUG | new | add PATH | export SLUG | remove SLUG]"
disable-model-invocation: true
---
Your map brands (the same job as `/geo-spatial-mapping:geo-set-brand`; follow that skill's steps). With no argument, list the brands and the
default. With a brand's short name, make it the default. `new` makes a brand from a name, one to five colours and an SVG logo; `add` takes a
brand pack folder or a brand `.json` file.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/set_brand.py" list
python3 "$GMAP_ROOT/scripts/set_brand.py" set <slug>
python3 "$GMAP_ROOT/scripts/set_brand.py" new --name "<name>" --color "<hex>" --logo <logo.svg> [--default]
python3 "$GMAP_ROOT/scripts/set_brand.py" add <pack folder or brand.json> [--default]
```

The plugin ships one brand; every other brand is a pack kept outside the plugin (`.geo-mapping/brands/`), so nothing is rebuilt and an update
never removes it. Maps already made keep the brand in their MapSpec, an unknown brand is refused with the list of brands that are here, and
documents, decks and charts are never touched.
