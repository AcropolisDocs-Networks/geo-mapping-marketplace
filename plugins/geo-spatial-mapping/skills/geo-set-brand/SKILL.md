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
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/set_brand.py" list                      # the brands here, the default, where added packs are looked for
python3 "$GMAP_ROOT/scripts/set_brand.py" set <slug>                # your default brand for new maps
python3 "$GMAP_ROOT/scripts/set_brand.py" new --name "Acme Fiber" --color "<hex>" [--color "<hex>" ...] --logo logo.svg [--logo-dark logo-dark.svg] \
        [--trademark "Acme is a trademark of Acme Fiber, Inc."] [--default]
python3 "$GMAP_ROOT/scripts/set_brand.py" add <pack folder or brand.json> [--default] [--replace]
python3 "$GMAP_ROOT/scripts/set_brand.py" export <slug> --out <folder>   # copy an added pack, to share it or keep it with a project
python3 "$GMAP_ROOT/scripts/set_brand.py" template --out <folder>        # a starter brand.json to edit by hand, then `add`
python3 "$GMAP_ROOT/scripts/set_brand.py" remove <slug>              # an added pack only; never the shipped brand
python3 "$GMAP_ROOT/scripts/set_brand.py" set <slug> --here         # a default for this project only (the default is your own folder)
python3 "$GMAP_ROOT/scripts/set_brand.py" adopt --slug <slug> [--default]   # keep an edit made to the shipped brand as your own pack
python3 "$GMAP_ROOT/scripts/set_brand.py" font <slug> --family "<name>" --regular FILE [--bold FILE] --licence-ok "<how the licence is held>"
```

**Class colours (4.0.0).** A new pack keeps a categorical map in the brand's own family: its accents, then their tints and shades and the
brand's greys, and other hues only when the classes outnumber what a colour-blind reader can tell apart. `--categorical hues` keeps the way
before 4.0.0 (other hues straight after the accents). A pack added before 4.0.0, and the shipped brand, keep their colours.

**The brand's own typeface (4.0.0).** `font` puts a font the person has (.woff2, .woff, .ttf or .otf; a bold file for titles is optional)
in a pack they added. A font is licensed work: ask the person whether its licence lets them embed it in maps (PDF, SVG, web pages) and pass
their answer as `--licence-ok`; never assume it. Maps in that brand then measure and embed it, with Inter for any glyph it lacks.
`font <slug> --remove` goes back to Inter. The shipped brand keeps Inter.

## Making a brand with the person

1. Ask for the brand's **name**, its **main colour** (and up to four more, in order of importance) and its **logo** (an SVG file is best; a PNG or JPEG works). A second file
   for dark backgrounds is optional. Ask whether the brand requires a trademark line on its maps. Do not guess colours from a logo you cannot read.
2. Run `new`. It prints the colours it had to move and why (contrast against the map's grounds, colours too alike for colour-blind readers, a
   dark background lifted off black). Tell the person those lines; they are the whole difference from what they gave.
3. Offer one sample map in the new brand (a small `map-create` build with `brand: <slug>`), and `set <slug>` if they want it as the default.

Without a logo the pack gets a plain placeholder mark; say so, and run `new` again with `--logo FILE.svg --replace` when they have one.
A full brand file in the AcropolisDocs brand registry format (`schemas/brand.schema.json`) is added with `add`; `template` writes a starter.

## Where packs are kept (3.7.0)

`new`, `add` and `set` write to **your own folder**: packs to `~/.geo-mapping/brands/` (or `brands_dir:` in `~/.geo-mapping/config.yaml`), the
default to `~/.geo-mapping/user-defaults.yaml`. Every project reads it and a plugin update never touches it. A project's own
`.geo-mapping/` is read first: `--here` keeps a pack or a default for this project only (a client project with its own brand). `--to DIR`
keeps a pack in a folder of your choice; name that folder in `brands_dir:` so maps find it. `list` says where the default came from.

**Never edit the shipped brand** inside the installed plugin: an update restores it and the edit is lost. `/geo-spatial-mapping:map-doctor` and the map plan warn
when the shipped brand was edited (MAP-BRAND-018); `adopt --slug NAME [--default]` keeps the edit as the person's own pack.

In a **Cowork session** the session's home does not last, so the folder that lasts is the project, and the plugin's data server keeps your
own folder on your computer: after making a brand, `brand_copy direction=to_library` saves the pack there and `brand_default slug=<slug>`
makes it the default for every project. At the start of map work in a folder, `user_settings work_folder=<folder> op=bring` brings your
default, its pack, your acknowledgment, licence answers and your own sources into the project (once per folder; again to refresh).
`brand_library` lists your packs; look there before asking for a brand again. A map whose default cannot be found here says so
(MAP-BRAND-017) instead of quietly using the shipped brand.

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
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
