---
name: map-pptx-adapter
description: >
  UTILITY MODULE - read by map-export. Turns a map into slide pieces (map-only picture, title, legend, source lines, message) or a one-slide
  .pptx with editable text, and can add that slide to a copy of a deck. Brands the map only; never restyles the deck.
---

# map-pptx-adapter

Called by `map-export` when someone wants a map for a slide, and by `map-atlas-run` callers who want slides.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pptx_adapter.py" parts MAP.yaml --out slide-parts
python3 "$GMAP_ROOT/scripts/pptx_adapter.py" slide MAP.yaml --out MAP.pptx
python3 "$GMAP_ROOT/scripts/pptx_adapter.py" slide MAP.yaml --out MAP_in_deck.pptx --deck DECK.pptx [--layout "Blank"]
```

## What you get

- **parts**: `map.png` and `map.svg` (the map only, cropped to its frame, no title or legend inside), `text.json` (title, subtitle, message, each legend entry with its color and shape, source and trademark lines), `notes.md`.
- **slide**: one 16:9 slide. The picture is the map; the title and subtitle are text boxes; the legend is native shapes with text boxes beside the map; the source lines are a text box; the message is in the speaker notes and the picture's alt text.
- The legend matches the map's (M16): a continuous raster ramp is a native gradient, a service's legend picture is a picture, a bivariate key is the 3 x 3 grid of boxes with both arrowed axes and their labels, graduated line widths are lines of those widths, nested size circles are native ellipses with leaders, and hatched classes are PowerPoint pattern fills. The result's `legend_fallbacks` says what is approximate: PowerPoint's patterns are fixed presets, so a hatch keeps its direction but not the map's spacing; say so when the slide has hatches.
- **with `--deck`**: the slide is added to a **copy** of the deck, on one of its own layouts (a blank one unless `--layout` names another). The deck's theme, fonts and other slides are untouched, and a deck of another shape gets the map fitted inside the slide (MAP-PPT-003).

## Rules

- The map is built with the `16x9-slide` template, so type sizes are right for a slide. The full map is linted like any other; a blocking finding stops the adapter (MAP-PPT-001).
- Text is set in Inter by name. A .pptx cannot embed fonts, so tell the person Inter must be installed where the deck is shown (MAP-PPT-002).
- The adapter never changes a deck it is given and never writes over it (MAP-PPT-004). Give the output a new name.
- A confidential map keeps its confidentiality banner in the picture, and the source and trademark lines stay on the slide.
- If a deck-building skill from another plugin is installed and the person is building a deck with it, offer to hand it `slide-parts/` and say what is in it. Do not call that skill without the person's yes, and do not apply this plugin's brand to the deck.
