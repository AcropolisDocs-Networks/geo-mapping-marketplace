---
name: map-build-options
description: >
  KNOWLEDGE MODULE - read by map-create. Limits for large layers, panels and how the map is fitted around its
  elements, style packs: what each pack is and how one is chosen at the start of a build, and the 3.7.0 MapSpec lines that replace hand work.
---

# map-build-options

## Limits for large layers

- A map of 250,000 points or 100,000 polygons builds to SVG and PDF in under a minute on a two-core machine (`tools/scale_bench.py` measures it).
  Above 5,000 features a layer is drawn in a compact form that looks the same: round point symbols as dots, and the cells of a choropleth, hexbin or
  grid, or opaque lines, as one path per class. Above 20,000 features polygons and lines are simplified to one pixel of the output dpi (cells that
  tile the map keep their shared edges); the methods note says by how much.
- Many points still hide each other. When the symbols add up to more than the map's area (MAP-OUT-012), the metro core reads as one colour, the
  class drawn last. Offer `role: hexbin` (density per hexagon) or a smaller `symbol.size`. `thin: true` on a point layer drops points that sit within
  half a symbol of another of the same class; it makes the file smaller and leaves the picture as it is, and it is never applied unless asked.
- A layer drawn as more than 50,000 separate shapes (MAP-OUT-011) opens slowly in PDF and SVG viewers: aggregate, filter or dissolve it.
- `engine: auto | pandas | duckdb` on a layer chooses what runs its large-table work (the join table read, `count_in`, `sum_in`, `spatial_join`,
  `grid`). `auto` uses DuckDB above the thresholds in `duck_engine.AUTO_ROWS` when it is installed (`pip install duckdb`); the results are the same
  either way and provenance says which ran. DuckDB's spatial extension is loaded only from this machine, never fetched while a map builds.

## Panels and the fit (1.3.0)

Two to six maps on one sheet with one shared frame (a diptych, before and after, shade and heat) are a SheetSpec, built by the same command:
a file with `sheet_version: 1`, the sheet's `template`, `title`, `arrangement` (columns, rows, gap, captions) and `shared` (extent union, first or
each; legend shared, each or none; one locator, north arrow, scale bar and Sources line), and `panels` (`spec:` a MapSpec, or `base:` with
`override:`). Every panel takes the first panel's CRS and the same frame; same-field layers share their classes. Findings MAP-PNL-001 to 007.
The overlay fit (`extent.fit`, default `auto`): the map is moved first, an element whose position is `auto` slides along its edge, and the
map shrinks only when the elements together still cover more than `extent.fit_cover_pct` (default 5) of the focus area (`focus.area`, else
the data). `overlay` never shrinks; `shrink` and `style.look: classic` keep the 1.2.0 rule. Report the fit note (MAP-SPEC-009) when there is one.

## Style packs (1.4.0)

Every map has a style pack: palette, type, ornaments and treatments. Without one it is `default`, the map exactly as before (the brand's
palette and Inter). The bundled packs: `antique` (paper and ink, IM Fell and EB Garamond, bar border, cartouche, compass rose, water-lining,
hill and town glyphs), `blueprint` (white line work on Prussian blue, IBM Plex, title block, grid), `editorial` (a Source Serif headline,
quiet context), `monochrome` (greys, categories by pattern) and `sketch` (hand-drawn lines, hatched fills, Caveat and Patrick Hand). Set it in
the MapSpec as `style: {pack: antique}`. Use a pack only when the person names one; you may suggest one ("an antique pack would suit this").
The brand's logo, stamps, hidden accreditation and footer credit stay under every pack. The pack is fixed when the build starts; its output
names carry its id. Ornaments and treatments also work on a default map: `elements.frame.style: double | bar-border`,
`elements.title.box: cartouche | title-block`, `elements.north_arrow.style: compass-rose`, `elements.scale_bar.style: ladder`,
`elements.legend.frame: ruled | cartouche`, `style.treatments: {water_lining: true, place_glyphs: true, hill_glyphs: {dem: FILE}}`.

A helper script of your own (a box drawn into the map, a wrapper around a plugin script) resolves the plugin's folder on every run, the way
the skills' first line does, and never keeps a path from an earlier call: a plugin update moves the synced folder (`...~g2`) mid-session.
A figure that belongs on the map goes in `elements.figures` (1.19.0), so every rebuild draws it again instead of losing it; a terrain
cross-section is `elements.section` (1.20.0), drawn from the map's own DEM and sun.

## MapSpec lines that replace hand work (3.7.0)

- **Draw order of categorical points:** the class with most points is drawn first and the smallest on top (`symbol.stack: auto`, the
  default), so a minority is never buried under the majority. `stack: [label, ...]` bottom to top, `stack: shuffle` (seeded), `stack: key`
  (the old order); a class in `classification.background: [label]` is always drawn first. The legend keeps the key's order.
- **Labels below the sheet's minimum** (10 pt on a slide, 6 pt in print) are drawn at the minimum with a MAP-CART-007 warning, not dropped.
- **A route strip:** `elements.strip: {on: true, layer: STOPS, label_field, line: LINE | order_field, stack: {layer: POINTS, join | max_m},
  callout_field, marks: [{between: [A, B], text}], title, position | rect_mm}` draws the line straight with one stacked bar per stop in the
  points layer's own colours, and carries that layer's key, so `legend: {on: false}` is fine. A hand-made figure can say so with
  `elements.figures.items[].carries_key: [layer]`.
- **The locator** of a map under 60 km across shows the metro area around it, not the whole state (`state_outline: true` asks for the state).
- **Foreign keys** (a stop_id on restaurants) are not reported as duplicates when another layer has that column as its `id_field`, or a join
  or the strip names it; `foreign_keys: [col]` on a layer says so directly.
- **Socrata open data** (city portals): a library entry or your own source with `protocol: socrata`; `library_request` takes `select`,
  `where` (field op value; ...), `group`, `within_box`/bbox, `within_polygon=FILE`, `order`, `limit` and writes the SoQL itself.
- **An OpenStreetMap basemap file** for any window, no tile cap: `basemap_extract` (plan first: it states the size), then
  `basemap: {provider: pmtiles, source: pmtiles:FILE}`. A capped provider's plan offers it (MAP-TILE-005).

- **Ocean and cable maps:** `crs: {display: spilhaus}` (PROJ 9.6 or later; `/geo-mapping-acropolisdocs:map-doctor` checks it) or `goode`. The ground is drawn by
  inverse projection, line layers are split where they cross a cut (the count is reported), a polygon layer gets MAP-CRS-012.
- **Small countries at world scale:** the 1:110m countries layer gains the 65 small states and territories from the
  bundled 1:50m layer (`fill_small: false` leaves them out); `source.detail: 50m | 10m` reads the finer outlines (10m is fetched once).
  A country too small to see is drawn as a dot in its class colour and listed (MAP-CART-042).
- **Venues by name:** `geocode_venues` (stadium, arena, station, airport, landmark) from Wikidata; an ambiguous name returns candidates.
