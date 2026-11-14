---
name: brand
description: List, add, make or set the brand for maps made with geo-mapping-acropolisdocs
argument-hint: "[list | set SLUG | new | add PATH | export SLUG | remove SLUG]"
disable-model-invocation: true
---
Your map brands (the same job as `/geo-mapping-acropolisdocs:geo-set-brand`; follow that skill's steps). With no argument, list the brands and the
default. With a brand's short name, make it the default. `new` makes a brand from a name, one to five colours and an SVG logo; `add` takes a
brand pack folder or a brand `.json` file.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/set_brand.py" list
python3 "$GMAP_ROOT/scripts/set_brand.py" set <slug>
python3 "$GMAP_ROOT/scripts/set_brand.py" new --name "<name>" --color "<hex>" --logo <logo.svg> [--default]
python3 "$GMAP_ROOT/scripts/set_brand.py" add <pack folder or brand.json> [--default]
```

The plugin ships one brand; every other brand is a pack kept outside the plugin (`.geo-mapping/brands/`), so nothing is rebuilt and an update
never removes it. Maps already made keep the brand in their MapSpec, an unknown brand is refused with the list of brands that are here, and
documents, decks and charts are never touched.
