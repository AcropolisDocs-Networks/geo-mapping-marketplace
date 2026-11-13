---
name: cartography-principles
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle and reference-curate. Visual hierarchy, figure and ground, generalization, labels,
  which map type answers which question, normalization, classification pitfalls, MAUP, the ecological fallacy and the lie factor, each tied
  to the lint that enforces it.
---

# cartography-principles

Read this when choosing a map type, a classification or a symbol, when a lint finding needs explaining to the person, or when reviewing a
`map-type` library entry (its cadence is reviewed against this module). The rules here are the reasons behind the MAP-CART and MAP-A11Y lints;
the lints are what the build enforces.

## The map makes one claim

Every MapSpec carries a `message`: the one sentence the map must show. The title states it, the data layer carries it, everything else
supports it. MAP-CART-013 compares the title with the message. A map with two messages is two maps (`map-atlas` makes the series).

## Visual hierarchy, figure and ground

| Level | What sits there | How it is held there |
|---|---|---|
| Figure | The data layer the message is about | The brand's data colours, full saturation, drawn last among data |
| Supporting data | Second layers, labels of the figure | Drawn see-through: `emphasis: supporting` (55 percent) or `background` (30); labels in the label colour with a halo |
| Ground | Land, water, context roads and places, relief | Brand land and water tokens, low contrast; context drawn under data |
| Furniture | Title, legend, scale bar, north arrow, source note | Out of the map's way; the layout engine places them where data is sparse |

1.13.0: the builder holds these levels by itself. The data layers the `message` names are the figure, at full strength; the other data layers
are supporting (55 percent, their legend keys too), and the No data class fades to half. Write the message so it names what the map is about,
or set `emphasis:` per layer; `highlight:` does the same inside one layer (`in`, `min`, `max`, `top`). MAP-CART-038 warns when three or more
layers all stand at full strength.

MAP-CART-008 checks the basemap stays subordinate; MAP-CART-015 refuses a data layer drawn in a page or text colour. A tile basemap under a
choropleth competes with it: prefer the bundled background or `provider: none`.

## Which map answers which question

| Question | Map type (library `ref.map-type.*`) | Not this, because |
|---|---|---|
| How much per area, compared across areas | Choropleth of a **rate** (per person, per km², percent) | Raw counts: big areas look important (MAP-CART-001) |
| How many, where | Proportional symbols, **area**-scaled (MAP-CART-010) | Radius scaling exaggerates large values about fourfold at twice the value |
| Where things are, many of them | Points with clustering, a hexbin or a density surface (MAP-CART-011) | 10,000 overlapping points hide density |
| Which category, where | Categorical colours, at most 8 (MAP-CART-004), shape as well when status matters (MAP-A11Y-003) | A ninth hue is not told apart; group the rest as Other |
| Two variables together | Bivariate choropleth, 2x2 or 3x3 (MAP-CART-018) | More cells than features to fill them |
| Movement between places | Flow lines, graduated widths that can be told apart (MAP-CART-019) | Colour alone for magnitude |
| Change over time | An atlas by period with shared classes (MAP-CART-014), or the page's time slider | Re-classing each period: colours stop meaning the same thing |
| Coverage or service area | Footprint polygons with a coverage disclaimer (`telecom-geo`) | Implying availability where only coverage is modelled |

## Normalization

Divide counts by the denominator the question needs: people for incidence, households for take-up, area for density, a total for a share.
`classification.normalize` (a field) or `denominator`; never both (MAP-SPEC-006). Very small denominators make wild rates: a county of 80
people with 2 events tops the map. Say so in a note, or use a minimum population, or map the count with proportional symbols instead.

## Classification

| Method | Use when | Watch for |
|---|---|---|
| `quantile` | Ranking matters; values fairly even | Equal counts can split near-identical values; gaps hidden |
| `equal_interval` | The value range itself is meaningful (percent, temperature) | Skewed data puts most areas in one class |
| `jenks` (natural breaks) | Clustered data; one map, not a series | Breaks change with every dataset: never compare two Jenks maps |
| `head_tail` | Heavy-tailed data (city sizes, traffic) | Few classes in the tail |
| `std_dev` | Distance from the mean is the message | Needs a diverging ramp with the mean as midpoint (MAP-CART-003) |
| `log` | Values over several orders of magnitude | Zero and negatives cannot be shown; say so |
| `manual` | Thresholds that mean something (targets, regulatory limits) | Write the reason in the legend note |

Three to seven classes (MAP-CART-002). Breaks unique and ordered; legend numbers formatted with consistent decimals and no overlapping ranges
(MAP-CART-006); a No data class whenever there are nulls (MAP-CART-005).

## Pitfalls to name to the person

- **MAUP (modifiable areal unit problem).** The pattern depends on the units: counties, tracts and hexagons of the same data show different
  maps. Pick the unit for the question, and say which unit the map uses.
- **Ecological fallacy.** A rate for an area says nothing about any person in it. "Tract with high take-up" is not "people here take it up".
- **Lie factor.** The size of the effect shown divided by the size of the effect in the data; above 1 exaggerates. Radius-scaled symbols,
  truncated legends and a sequential ramp on diverging data all raise it.
- **Big empty areas.** Large sparsely populated units dominate a choropleth's ink. A cartogram, dot density or hexbin can show people, not land.
- **False precision.** Legend values to more decimals than the data supports; boundaries drawn at 1:500,000 shown at 1:24,000 (MAP-CART-012).

## Generalization

Match detail to scale: boundaries from the library at the level whose `scale` facet fits the output (500k cartographic files for a state map,
TIGER full detail for a city), simplified for the page (`webmap` does it per zoom). Land and water from one source at one resolution so the
coasts agree (MAP-CART-024).

## Labels

Place names under data labels; data labels only where they help the message. Minimum sizes: 6 pt print, 10 pt slide (MAP-CART-007). Halos in
the ground colour rather than boxes. Lines are labelled along the line, upright (map_extras), points to the upper right first, areas inside
the area or with a leader line when it is too small. Never let a label hide the figure.
