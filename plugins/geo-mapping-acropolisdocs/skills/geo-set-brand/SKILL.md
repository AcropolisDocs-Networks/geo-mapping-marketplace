---
name: geo-set-brand
disable-model-invocation: true
description: >
  List your map brands, add a brand pack, make a brand from a name, a colour and a logo, or set the default map brand. Maps only.
---

# geo-set-brand

The plugin ships one brand, its default. Any other brand is a **brand pack** you add: nothing is rebuilt and the plugin itself is not changed.
A pack is a small folder (`brand.json`, `map-tokens.yaml`, `PACK.json`) kept outside the plugin, so a plugin update never removes it.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/set_brand.py" list                      # the brands here, the default, where added packs are looked for
python3 "$GMAP_ROOT/scripts/set_brand.py" set <slug>                # your default brand for new maps
python3 "$GMAP_ROOT/scripts/set_brand.py" new --name "Acme Fiber" --color "<hex>" [--color "<hex>" ...] --logo logo.svg [--logo-dark logo-dark.svg] \
        [--trademark "Acme is a trademark of Acme Fiber, Inc."] [--default]
python3 "$GMAP_ROOT/scripts/set_brand.py" add <pack folder or brand.json> [--default] [--replace]
python3 "$GMAP_ROOT/scripts/set_brand.py" export <slug> --out <folder>   # copy an added pack, to share it or keep it with a project
python3 "$GMAP_ROOT/scripts/set_brand.py" template --out <folder>        # a starter brand.json to edit by hand, then `add`
python3 "$GMAP_ROOT/scripts/set_brand.py" remove <slug>              # an added pack only; never the shipped brand
```

## Making a brand with the person

1. Ask for the brand's **name**, its **main colour** (and up to four more, in order of importance) and its **logo** (an SVG file is best; a PNG or JPEG works). A second file
   for dark backgrounds is optional. Ask whether the brand requires a trademark line on its maps. Do not guess colours from a logo you cannot read.
2. Run `new`. It prints the colours it had to move and why (contrast against the map's grounds, colours too alike for colour-blind readers, a
   dark background lifted off black). Tell the person those lines; they are the whole difference from what they gave.
3. Offer one sample map in the new brand (a small `map-create` build with `brand: <slug>`), and `set <slug>` if they want it as the default.

Without a logo the pack gets a plain placeholder mark; say so, and run `new` again with `--logo FILE.svg --replace` when they have one.
A full brand file in the AcropolisDocs brand registry format (`schemas/brand.schema.json`) is added with `add`; `template` writes a starter.

## Where packs are kept

`new` and `add` write to this project's `.geo-mapping/brands/<slug>/`, which every map built in this folder finds. `--home` writes to your own
`~/.geo-mapping/brands/` instead (found from every project on a computer where the map scripts run), and `--to DIR` to a folder of your choice:
name that folder in the `GEO_MAPPING_BRANDS` environment variable to keep one pack library for all projects. In a cloud or desktop session
the working folder is the one that lasts. So that a brand follows the person anyway, the plugin's data server keeps a library on their own
computer: its `brand_copy` tool with `direction=to_library` saves a project's pack there, `brand_library` lists it, and `brand_copy` (to_project)
brings a pack into a new project. Offer `to_library` once after making a brand, and look in the library before asking for a brand again.

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/modules.py" read map-brand        # any module in the table
```

| Module      | Read it |
|-------------|---|
| `map-brand` | for what a brand pack sets on a map |

- Maps already made keep the brand written in their MapSpec; changing the default restyles nothing.
- A brand changes colours, logo, the names in file properties, the file prefix and an optional trademark line. The typeface stays Inter (a
  style pack may bring its own faces), and the layout defaults and every check stay as they are.
- A logo must be a plain SVG drawing. One that carries a script, an event handler, a link or an embedded page is refused; ask for a plain export
  (outlined text, no links). A PNG or JPEG logo is taken too (up to 180 KB; it is drawn at its own pixels, so an SVG is better).
- The shipped brand's name cannot be taken by an added pack, and the shipped brand cannot be removed.
- An unknown brand is refused with a one-line list of the brands that are here; it is never substituted silently.
- Use a brand only where the person has the right to: a client's marks belong to the client. The plugin does not check this.
- Never write to user memory, preferences, settings or CLAUDE.md.
