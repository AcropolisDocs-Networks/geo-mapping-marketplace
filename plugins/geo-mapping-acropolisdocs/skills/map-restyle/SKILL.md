---
name: map-restyle
description: >
  Change how an existing map looks (legend, palette, classes, size, basemap, relief, labels, brand), then re-check and rebuild. Trigger on:
  change the colors, remove the legend, make it A4.
---

# map-restyle

Edits the map's MapSpec in small checked steps, shows what changed, and rebuilds. It never edits an SVG by hand and never picks colors.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/map_restyle.py" --spec map.yaml --off legend --position north_arrow:upper-left
python3 "$GMAP_ROOT/scripts/map_restyle.py" --spec map.yaml --palette counties:brand_sequential:reverse --classify counties:quantile:5
python3 "$GMAP_ROOT/scripts/map_restyle.py" --spec map.yaml --template tabloid-landscape --build out --formats svg,pdf,png
python3 "$GMAP_ROOT/scripts/map_restyle.py" --spec map.yaml --undo
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
python3 "$GMAP_ROOT/scripts/modules.py" read map-text        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `map-text`               | when the title, subtitle or a legend title changes |
| `cartography-principles` | when a change alters the map type or its classes |
| `classify-and-color`     | for a new classification method, class count or rounding |
| `color-accessibility`    | for a palette change, bivariate keys and hatch patterns |
| `basemap-render`         | for streets or terrain behind the map: the tiles are prepared once, then the rebuild reads them |
| `tile-licensing`         | before naming any tile provider |
| `raster-data`            | for relief or a raster layer |
| `map-layout`             | when an element moves or the template changes |
| `legend-builder`         | when the legend changes |
| `label-engine`           | when place names or labels change |
| `map-brand`              | when the brand changes |
| `sld-style-import`       | to take a GeoServer SLD's styling (by attribute, widths, sizes, labels) onto a layer |

## Phrase to option

| The person says | Option |
|---|---|
| remove / drop / hide the legend, north arrow, scale bar, inset, logo | `--off legend` and so on |
| show / add the north arrow or graticule | `--on north_arrow` |
| move the legend to the top left | `--position legend:upper-left` |
| put it back where it was / the usual place | `--position legend:default` |
| let the layout find room for the inset | `--position inset_map:auto` |
| dark map, dark mode / back to light | `--theme dark` / `--theme light` |
| different colors, reverse the colors | `--palette LAYER:brand_sequential[:reverse]` |
| the ramp in another brand color (green, the second brand color) | `--set layers.LAYER.palette.anchor=@brand.categorical.1` |
| the old look, exactly as it was before 0.17 / back to the new look | `--set style.look=classic` / `--set style.look=auto` |
| make it bolder for a slide or social post / a dark map / a named look | `--set style.look=bold` / `--set style.look=dark` / `--set style.look=network` (focus, clean, network, terrain, surface) |
| make some features stand out (the rest greyed) / no highlight | `--set "layers.L.highlight={column: band, in: [n41]}"` / `--set layers.L.highlight=null` |
| bigger or smaller points than the automatic size | `--set layers.LAYER.symbol.size=8` (points; the automatic size goes by how many there are) |
| no light band under the lines | `--set layers.LAYER.symbol.casing=false` |
| don't fade the land around the study area / no shadow under it | `--set focus.mask=false` / `--set focus.lift=false` |
| fade around a different area (one layer's outline) | `--set focus.area=LAYER` |
| one short line of sources / the full source lines | `--set elements.source_note.style=consolidated` / `--set elements.source_note.style=detailed` |
| a wider title box, so a long title stays on one line | `--set elements.title.max_width_pct=70` |
| the tile basemap as fetched / quieter still behind the data | `--set basemap.treatment=none` / `--set basemap.treatment=knockout` |
| fewer / more classes, use Jenks or quantiles | `--classify LAYER:jenks:5` |
| log scale classes, the values span orders of magnitude / a few huge values dominate | `--classes-method LAYER:log:5` / `--classes-method LAYER:head_tail` |
| show two measures at once, compare X with Y on one map | `--bivariate LAYER:XFIELD:YFIELD` (3 x 3; `:2` for 2 x 2; `LAYER:off` to go back, then `--set layers.LAYER.value_field=FIELD`) |
| hatch the classes, it must work in black and white / for colour-blind readers | `--patterns LAYER:on` (`auto`: only when colours alone fail the check) |
| thicker lines for bigger values | `--set layers.LAYER.symbol.graduated=true` (a classed lines layer) |
| nested circles in the size key | `--set layers.LAYER.legend.nested=true` |
| larger sheet, slide size, square for social | `--template tabloid-landscape` / `16x9-slide` / `square-social` |
| portrait, A4, A3, a story for social | `--template letter-portrait` (also `tabloid-portrait`, `arch-d-portrait`) / `a4-portrait`, `a4-landscape`, `a3-portrait`, `a3-landscape` / `story` |
| an exact sheet size, 500 by 700 mm | `--template custom:500x700` (mm; 100 to 1,500 a side, at most 3:1) |
| transparent background, bleed, PDF/A, Inkscape layers, outlined text | not a restyle: export options on the build (`map-export`: `--transparent`, `--bleed 3 --crop-marks`, `--pdfa`, `--inkscape`, `--outline-text`), or `--set export.pdfa=true` to keep it in the spec |
| plain background, no basemap / back to the bundled outlines | `--basemap none` / `--basemap bundled` |
| streets or terrain behind the map | not a restyle option: `basemap-render` prepares the tiles once (`plan`, then `render`) and the next build reads them |
| add roads, towns and water / take the roads off / only water | `--context full` / `--context none` / `--context water` (presets: auto, none, roads, urban, water, full, terrain) |
| put hillshade / relief / terrain under this map, add contours, take the relief off | `--terrain on` / `--terrain contours` / `--terrain off` (3DEP in the US, fetched once and cached; a DEM file: `--set terrain.source=dem.tif`) |
| make the raster see-through, lighter colours on the raster | `--set layers.LAYER.raster.opacity=0.7` / `--set layers.LAYER.raster.vmax=2000` |
| turn off the place names / put the city names back | `--names off` / `--names on` |
| change the title | `--title "..."`, `--subtitle "..."` (written by the `map-text` module: subject-led Title Case title, a subtitle with one job; list the build's `map_text.callout` lines) |
| use another brand (the shipped one, or a pack you added with the brand command) | `--brand <slug>` |
| a compass rose / plain arrow / bigger north arrow | `--set elements.north_arrow.style=compass` (`needle`, `arrow`, `triangle`, `compass`) / `--set elements.north_arrow.size=14` (mm) |
| a simple line scale bar, ticks on the scale bar | `--set elements.scale_bar.style=line` / `ticks` (`alternating` is the default) |
| a title on the legend, no box behind the legend | `--set "elements.legend.title=Map key"` / `--set elements.legend.frame=false` |
| graticule without labels, solid or dotted graticule | `--set elements.graticule.labels=false` / `--set elements.graticule.style=dotted` |
| shade the locator box, mark it in red | `--set elements.inset_map.highlight.style=fill` (`box`, `fill`, `corners`) / `--set elements.inset_map.highlight.color=danger` |
| the wordmark logo instead of the mark | `--set elements.logo.variant=secondary` (when the brand pack has one) |
| point out a site, add a note with an arrow | edit `elements.callouts.items` in the spec: `{text, at: [lon, lat]}` or `{text, layer, where: {field: value}}`, optional `direction: ne` |
| a top-5 table or bar chart on the map | `--set elements.data_table.on=true --set elements.data_table.layer=L --set elements.data_table.value_field=F --set elements.data_table.label_field=NAME` (`kind=bar`, `top=5`, `title=...`) |
| label the roads / the routes along the line | a `labels` entry for the lines layer (`{layer, field}`): line labels follow the line |
| labels that do not fit: use leader lines | `--set labels.N.leaders=true` on the point layer's labels entry |
| say how current the data is | `--set elements.date_stamp.data_as_of=2026-11-07` (or `true` to take it from the captures) |
| anything else in the spec | `--set elements.legend.columns=2` |

## Rules

- A request for an exact color is answered with the nearest palette role or token; custom colors are not allowed. Data layers take palette colors
  (`@brand.categorical.N`, `@brand.status.*`, `@brand.accent`), never `@brand.label` or another page or text color (MAP-CART-015).
- Move an element only when the person asks; never move one on your own to clear data (the builder keeps the corner clear itself).
- Turning off the legend on a map with classed layers is refused by the lint (MAP-CART-005); say why, and offer direct labels instead.
- Turning off the source note needs a stated reason (`--set "elements.source_note.reason=..."`, MAP-LIC-011); the credits a licence requires stay in the footer anyway (MAP-LIC-010). Ask the person for the reason; never invent one.
- `--brand` takes a brand that is here (`set_brand.py list`); an unknown one is refused with the list. Adding a brand is the brand command's job, not this skill's.
- `--terrain on` outside the United States uses the AWS Terrain Tiles, a default: their credit is printed for you, nothing to acknowledge.
  Relief that cannot be fetched never stops the build.
- Each change keeps the previous spec as `<spec>.prev`; `--undo` swaps them. Comments in the YAML file are not kept.
- After a restyle with `--build`, report only what changed in the picture and any new findings.

## Another style pack (1.4.0)

"Use the antique style" is `--pack antique` (or `default`, `blueprint`, `editorial`, `monochrome`, `sketch`, or one of the person's own).
That is not a small change: the whole map is built again from its kept data (nothing is fetched again), with a new plan, colours, labels,
legend and layout, under new file names; the build says so (MAP-STYLE-002) and the earlier files stay. A restyle that keeps the pack stays a
restyle. `style_pack.py list` names the packs, `show ID` describes one.
