---
name: map-atlas
description: >
  Make a series of branded maps from one design, one per market, state, vendor or month, with shared classes. Trigger on: one map
  per market, map series, small multiples.
---

# map-atlas

Turns one finished MapSpec into many maps that differ only in what they show.


**A contact sheet** (`--option contact_sheet=true` on the atlas pipeline): every map of the series side by side on one sheet, each cropped to its
data and captioned with its value, with one shared legend, the title and the source lines, as a PDF and a PNG beside the atlas. It needs shared
classes and at most 24 maps; say so when the run skipped it (MAP-ATL-010).

## Flow

1. Find or make the MapSpec first (`map-create`). An atlas repeats a design that already works.
2. Ask only what the series needs: which layer, which column makes the pages (`field`), and whether each page should **filter** to the value (one vendor's sites) or **focus** on it (all markets drawn, the extent fitted to one). Default to filter for points and sites, focus for areas.
3. Preview the count and the values: `python3 "$GMAP_ROOT/scripts/atlas.py" plan MAP.yaml --layer ID --field COL [--mode focus]` (no files are written). Show the person how many maps there will be.
4. Up to about 10 maps, build here with `atlas.py build`. For a longer series, start the `map-atlas-run` pipeline through `/geo-mapping-acropolisdocs:map-run map-atlas-run` so its cost is confirmed first.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/atlas.py" plan  MAP.yaml --layer market --field NAME
python3 "$GMAP_ROOT/scripts/atlas.py" build MAP.yaml --layer market --field NAME --out ./atlas
```

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
python3 "$GMAP_ROOT/scripts/modules.py" read map-atlas-run        # any module in the table
```

| Module               | Read it |
|----------------------|---|
| `map-atlas-run`      | for a long series, started through `/geo-mapping-acropolisdocs:map-run map-atlas-run` |
| `classify-and-color` | for classes shared by every page |
| `map-text`           | for the series title and each page's subtitle |

## One map per period: `by: time`

When the person asks for the map "month by month", "per quarter", "how the rollout grew" or "an animation", the pages are periods, not values.
The layer needs a `time_field` (dates, years, `2026-03` months, `2026-Q1` quarters, or named periods such as "Wave 2"); `field=` names it if the
spec does not. Ask one thing: should each page show **only that period** (`cumulative: false`, the default: this month's outages) or **everything
up to it** (`cumulative: true`: a rollout, where the period's own sites are drawn larger in the second palette colour, earlier ones in the
layer's colour and the sites still planned as small grey dots). `unit=quarter` (or `year`, `month`, `day`) groups dates. Every page keeps the
whole data's frame, the classes come from the whole range, and the period is the large label under the title.

```bash
python3 "$GMAP_ROOT/scripts/atlas.py" plan MAP.yaml --layer sites --by time --cumulative
```

For an animation, start the pipeline with `animate=gif,mp4` (and `animate_sizes=square,slide`); the plan counts the frames (one render per period
and size) before anything runs. `map-export` explains the GIF and MP4.

## Rules

- **Same classes everywhere.** The class edges come from all the data, so a color means the same thing on every page. Turn it off (`--no-shared-classes`) only if the person wants each map to stand alone, and then say the index page will tell readers the colors are not comparable.
- A class with nothing in it on one page stays in the legend on purpose.
- More than 60 maps is refused (MAP-ATL-002); ask which values the person wants, or raise `max_maps` with their yes.
- Every map is linted on its own. One that cannot be built stops the series (MAP-ATL-005); name the value.
- The result is one PDF (index page, then every map, with bookmarks), `index.csv`, and a folder per map.
