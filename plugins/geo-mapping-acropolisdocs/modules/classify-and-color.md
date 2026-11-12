---
name: classify-and-color
description: >
  UTILITY MODULE - read by map-create, map-restyle, webmap-create and map-atlas. Classifies a numeric or categorical field into 3 to 7
  classes, formats class labels, checks whether a field is a raw count that needs normalizing, and picks class colors from the active
  brand's declared ramps.
---

# classify-and-color

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/classify.py" breaks FILE --field F --method quantile --classes 5 [--rounding nice|none|25] [--manual 10,50] [--prefix '$']
python3 "$GMAP_ROOT/scripts/classify.py" check  FILE --field F         # raw count or a rate?
python3 "$GMAP_ROOT/scripts/classify.py" palette --spec map.yaml --kind sequential --classes 5 [--mode print|screen] [--reverse]
```

## Choosing a method

| Data | Method | Why |
|---|---|---|
| Skewed rates, most maps | `quantile` | every class has features; reads well, but can split similar values (a quantile map is an equal-count map) |
| A distribution with clear clusters | `jenks` | puts breaks in the gaps; deterministic (a capped, weighted sample on very large fields) |
| Ranges a reader can remember (0 to 25, 25 to 50 ...) | `pretty` (aliases `rounded`, `nice`) | equal-width classes on a 1, 2, 2.5 or 5 x 10^n step; the first and last edge are round numbers too; 3 to 7 classes, nearest your `classes` |
| Every class holds about the same number of features | `quantile` (alias `equal_count`) | reads well, but can split similar values |
| Strongly skewed counts or sizes (a few huge, most small) | `geometric` | each class a constant multiple wider than the last |
| A familiar range such as 0 to 100 percent | `equal_interval` | easy to read; empty classes are possible |
| Values around a meaningful mean | `std_dev` | diverging; use a diverging palette |
| A policy threshold or a number the audience knows | `manual` | breaks you can defend |
| Kinds, not amounts | `categorical` | the 7 most frequent get colors; the rest become Other |

## Rules

- **Never shade raw counts over units of unequal size.** Run `check`. If it says a count is suspected, divide by population,
  households or area and say so in the legend title. Only the user may override, with a reason (MAP-CART-001). In a MapSpec,
  `classification: {normalize: area_km2}` (or `area_mi2`, `area_m2`, `area_ha`, `area_acre`) does the division by geodesic area for you
  and adds "per km²" to the legend title; a `denominator: column` divides by a column instead (one or the other, MAP-SPEC-006).
- **Hex and grid cells are classed like a choropleth, and their key says what a cell is.** A `role: hexbin`, `heat` or `grid` layer
  classes `count` (or `<value>_<agg>`) on the sequential ramp with `pretty` round-number classes unless the spec says otherwise; a cell count is
  not a raw count over unequal units (every cell is the same size), so MAP-CART-001 does not apply and no normalization is needed. The legend
  key is drawn in the cell's own shape (a hexagon for H3 cells, a square for a grid), the title names the measure and the cell size ("Sites
  per hex (about 7.5 km across)", "Sites per 1 km cell"), and a hexbin block carries a note with the H3 resolution and edge length; a mean or
  median per cell says its unit in the title ("Mean RSRP per hex (dBm)"). Never draw one hex density as a diverging ramp without a midpoint.
- **3 to 7 classes** (MAP-CART-002). Out-of-range requests are clamped with a note; ties that collapse classes are reported.
- **Breaks are rounded by default.** For `quantile`, `equal_interval`, `jenks` and `geometric` each interior break is moved to a round number (1, 2, 2.5 or 5 x 10^n)
  when that shifts it by no more than a sixth of the class beside it, so a legend reads "25 – 49", not "23 – 47". Set `classification: {rounding: none}` for the exact
  breaks, or `rounding: 25` to snap to multiples of 25. `pretty` is always round; `manual` is never touched. A note on the map says when breaks were rounded and that
  class sizes are then close to, not exactly, the method's.
- **Breaks are snapped** to the decimals the labels show, so a label states exactly which values the class holds. Whole-number
  data labels read "25 – 49"; continuous data reads "Under 12.5", "12.5 to 20.0" (1.2.0; each class includes its lower value and stops below the next class's, which the methods note says once; the classic look keeps "12.5 to under 20.0").
- **Class colors are generated from a brand color** (0.17.0, `style.look: auto`, the default). A sequential ramp is the full range of
  tints and shades of the brand's emphasis color (or `palette: {anchor: "@brand.categorical.1"}` on the layer), made for exactly the number
  of classes: about 70 L* from the lightest class to the darkest, hue kept, chroma highest in the middle. A diverging ramp is two such arms
  meeting at the brand's light neutral. Categorical colors keep the brand's order, but a color too close to one already used (colour-vision
  safe) waits its turn. The brand lint recomputes the same colors from the tokens, so only these and the declared tokens pass (MAP-BRAND-001).
  `style.look: classic` picks from the brand's declared ramps at even steps, as before 0.17.
- Sequential data takes a sequential ramp. A diverging ramp needs a stated midpoint (MAP-CART-003): `classification: {midpoint: auto}` picks
  the diverging ramp and centres it on 0 when the values run below and above zero, else on the median, and moves the nearest class break onto it;
  a number is used as given. A midpoint has no meaning for a categorical method (MAP-SPEC-006).
- Categorical maps show at most 8 colors (MAP-CART-004). The `palette_report` gives the adjacent-class distance under normal
  vision and the three color-vision deficiencies (MAP-A11Y-001) and each color's contrast with the background.
- Pick label text over a fill with `text_on(fill, dark, light)`, using the brand's `label` and `background` tokens.
- Null values are "No data", drawn in the brand's `no_data` color, never as the lowest class.
- **Single-symbol layers use the whole palette.** Each line, point or polygon layer without classes takes its own palette color
  (`categorical.0`-`.7`, `accent`, `status.*`); a line or point layer that names no color gets the next one no other layer shows. Page and text tokens
  (`label`, `background`, `land`, `water`, `frame`, `legend_bg`, `legend_frame`, `no_data`) are never data colors (MAP-CART-015). Highlight a
  subset (lines inside an area, sites at risk) with a distinct palette color, not white or black.
- Light (`print` tokens) is the default theme; dark (`screen` tokens) only when asked. The dark tokens keep land, water and frames apart on a
  very dark grey page (MAP-A11Y-006).
