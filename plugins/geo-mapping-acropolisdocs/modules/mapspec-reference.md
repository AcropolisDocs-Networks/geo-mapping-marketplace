---
name: mapspec-reference
description: >
  KNOWLEDGE MODULE - read by map-create. The MapSpec, field by field: layers, roles, symbols,
  classification, elements, names, context, basemap, extent, theme and the worked examples, as the builder reads them.
---

# mapspec-reference

## A MapSpec

```yaml
mapspec_version: 1
id: az-water-share                 # file names start from this
template: letter-landscape         # letter-landscape | tabloid-landscape | arch-d | 16x9-slide | square-social | portrait-social
                                   # portrait-social (4:5, 1080 x 1350 px): a social post of a tall subject (a city, Arizona, a corridor)
theme: light                       # light (default) | dark; dark only when the person asks for it, never because the map is for a screen or social post
style: {look: auto}                # auto (default) picks focus (areas), clean (points), network (lines), terrain (relief) or surface (raster) from the data;
                                   # bold (a quarter larger, for slides and social) and dark (a dark ground) only when asked; classic: the 0.16 look
# a layer's highlight: {column: band, in: [n41]} draws those features full colour and a fifth larger, the rest of the layer greyed at 30%;
#   1.13.0: {column: MW, min: 1000} | {max: 5} | {top: 8}, rest: fade (keep the colours), rest_opacity: 0.4
# a layer's emphasis: primary | supporting (55%) | background (30%) | auto (default: the layers the message names are primary, the rest supporting)
# style.hierarchy: off draws every layer at full strength; a proportional layer's symbol.scale_with: <layer id> shares that layer's size scale
# a library service layer's filter goes in its source: {type: ref, id: ..., where: "VOLT_CLASS IN ('345','500')"}; a file layer uses filter: {column, in}
# elements.callouts: {on: true, items: [{text: "Large-load queue", figure: "692 MW", at: [lon, lat] | layer: id, where: {field: value}}]}: the figure set large
# elements.title.halo: glow (default: a soft cloud of the page colour under the title block) | crisp | thin | none
# change a MapSpec with map_restyle.py --set (or gmap_common.dump_mapspec): plain PyYAML writes on: back as true: (read as on again since 1.13.0)
focus: {mask: auto, lift: true}    # auto: a choropleth or grid fades the land around it and sits on a soft shadow; area: <layer id> picks the outline
# elements.source_note.style: consolidated (one Sources: line, each organization once; default on slide and social) or detailed (print default)
message: "Water makes up a small share of Arizona county area, highest along the Colorado River."
audience: external                 # any | internal | external | regulatory; who sees the map (default external, the safe assumption)
task: [market-rollup]              # optional: what the map is for; the library warns when a layer says it is a poor fit
extent: {mode: fit_layers, padding_pct: 6}
basemap: {provider: bundled}       # or none; streets or terrain: {provider: carto-positron, style: light_all} after the `basemap-render` module's steps have prepared the tiles;
                                   # your own vector tile file as the ground, nothing to prepare: {provider: pmtiles, source: pmtiles:FILE}
layers:
  - id: counties
    source: {type: ref, id: ref.boundary.us.county.cb}       # or {type: file, path: data.gpkg, layer: counties}
    role: choropleth
    value_field: water_share_pct
    classification: {method: jenks, classes: 5, rounding: nice}   # quantile (equal_count) | equal_interval | pretty (rounded) | geometric | jenks | std_dev | log | head_tail | manual | categorical; rounding: nice (default) | none | a number
    legend: {title: "Water share of area (%)"}
    # a library layer needs no provenance block: the entry supplies source, license and vintage
labels:
  - {layer: counties, field: NAME}
names: {on: true}                  # place names from the bundled Natural Earth 10m data: cities, water bodies and state names, tiered by scale; on by default, `on: false` turns them off
context: {preset: auto}            # roads, urban areas, lakes and rivers from the bundled Natural Earth 10m data; presets auto | none | roads | urban | water | full | terrain
elements:
  title: {on: true, text: "Water Share of County Area, Arizona", subtitle: "2025 boundaries"}   # tagline: "#Series · Item · Theme" adds a slanted line
  legend: {on: true}               # no position: the default corner applies
```

By default the title sits inside the map at the upper left with no panel behind it (the logo has none either), the legend is at the upper right with the north arrow just left of it, the logo (as tall as the scale bar) is at the lower left with the scale bar to its right, and the locator inset is at the lower right, zoomed out at most 5x from the map (`inset_map: {max_zoom_out: N}` changes it; the box on the locator is the main map's whole frame, so a wide map gives a wide box); the source lines and date sit under the map. `title: {placement: outside}` restores the title band above the map.

**Always use the default positions.** Do not write a `position` unless the person asks for an element somewhere else, and do not move an element
because it sits on data: the builder keeps every element in its corner and shrinks the map a little to keep that corner clear. When the person asks for
a different arrangement, write the corner they named; `position: auto` lets the builder move that one element to a freer corner.

**Light is the default theme.** Use `theme: dark` only when the person asks for a dark map. Dark draws on a very dark grey (never black) with land,
water, the locator and the frame kept apart by the contrast rules in MAP-A11Y-006.

**Every map carries place names and context unless told otherwise.** The `names:` block draws city, water-body and state names from the bundled
Natural Earth 10m data, tiered by scale (a state map names the big cities, a metro map the towns), placed by the label engine so they never collide
with the data labels; `names: {on: false}` turns them off, and `names: {exclude: ["Fort *", Gila Bend]}` leaves out a name or a pattern. The `context:` block draws roads, urban areas, lakes and rivers from the same bundled data
under the data layers: `auto` (the default) is `full` on a site map, water only under a choropleth, and nothing over a tile basemap; `none`, `roads`,
`urban`, `water` and `full` set it outright. Both come from the plugin's own files, so a map stays offline. On a US map, `context: {roads: tiger}` (or `tiger-prisec`) draws the Census TIGER roads instead, once the file is in the library cache (`ref_fetch.py fetch ref.infrastructure.us.road.tiger-primary`); until then the Natural Earth roads stand in. Say in one phrase when names were thinned
or context was left off, and why.

**Four layer keys do data work the person would otherwise do by hand.** `repair: true` on a layer makes invalid geometry valid before anything
else looks at it (the same repair as `geo-convert --repair`, logged with the area change; a repair that moves more than 1 percent of the area
blocks with MAP-DATA-002, because that is a data problem to show the person, not to fix quietly). A `join:` reads the layer's `value_field` and
the classification's `denominator` as numbers whatever their names look like, and keeps every other code-like column (FIPS, ZIP, ids) as text with
its leading zeros; `join: {numeric: [col, ...]}` names any further column to read as a number. `classification: {normalize: area_km2}` (or
`area_mi2`, `area_m2`, `area_ha`, `area_acre`) divides the value by each feature's geodesic area, so a count becomes a density without a
denominator column; the legend title gains "per km²" and the methods page records the division. `classification: {midpoint: auto}` picks the
diverging palette and centres it: on 0 when the values run below and above zero (a change, a net add), else on the median; a number is used as
given. Use one of `normalize` and `denominator`, and never a `midpoint` with a categorical method (MAP-SPEC-006).

**A layer can be computed, not just drawn: `ops:`.** A list of geoprocessing steps runs on the layer after its join and repair, in order
(the `geo-analyze` ops: `buffer`, `clip`, `intersect`, `union`, `difference`, `spatial_join`, `nearest`, `dissolve`, `explode`, `dedupe`, `repair`,
`simplify`, `antimeridian_split`, `convex_hull`, `concave_hull`, `voronoi`, `area`, `length`, `count_in`, `sum_in`, `population_covered`,
`coverage_share`, `overlap`, `grid`, `hexbin`). An op that works against another layer names it with `layer:` (`clip_layer:` for a Voronoi);
that layer is loaded first whatever the order in the spec, and a layer that is only there for the ops gets `draw: false` (loaded, joined, never
drawn, never in the legend, the extent, the web page or the data package; its source still goes in the source note). Nothing is measured in
degrees: a buffer or a grid is computed in a projected CRS chosen for the data, or in `crs: {measure: EPSG:26912}` when the person wants one
pinned; areas and lengths are geodesic. Each op writes one sentence to the methods page with the GeoPandas expression that does the same, and a
record to the provenance. An op that leaves a drawn layer empty blocks (MAP-OPS-003); an op naming a layer the spec does not have is MAP-SPEC-007;
a missing parameter is MAP-OPS-008, found before any data is read. "How many households are inside 2 km of these sites" is two ops and a helper:

```yaml
layers:
  - id: tracts
    source: {type: ref, id: ref.boundary.us.tract.cb500k}
    join: {table: acs.csv, left: GEOID, right: geoid, columns: [households]}
    draw: false                                  # a helper: available to the ops below by its id, never drawn
  - id: footprint
    source: {path: sites.csv, x: lon, y: lat, crs: "EPSG:4326"}
    role: polygons
    ops:
      - {op: buffer, distance: 2, units: km, dissolve: true}
      - {op: population_covered, layer: tracts, value: households, field: hh_covered}
    legend: {title: "2 km around the sites"}
```

**Dense points are counted into cells: `role: hexbin`, `heat` or `grid`.** The layer's points (or a point inside each other feature) are
counted into H3 hexagons (`hexbin: {resolution: 0-15 | auto, value, agg: count|sum|mean|median|max|min, min_count}`) or square cells
(`grid: {size: 1, units: km, value, agg, min_count}`) and the cells are drawn like a choropleth of `count` (or `<value>_<agg>`) on the sequential
ramp in round-number classes, with a hexagon (or square) legend key, the cell size in the legend title ("Sites per hex (about 7.5 km across)",
"Sites per 1 km cell") and the H3 resolution and edge length in a note. `auto` picks the resolution at which cells average about four features
over the extent. `role: heat` is `hexbin` at automatic resolution with a count: the plugin draws density as hex cells a reader can count, never
as a smoothed kernel surface, and the methods page says so. A cell count is not a raw count over unequal units (cells are equal), so MAP-CART-001
does not apply; MAP-DATA-008 and MAP-CART-011 (too many points to draw) point here.

```yaml
layers:
  - id: drive_test
    source: {path: drive_test.csv, x: lon, y: lat, crs: "EPSG:4326"}
    role: hexbin
    hexbin: {resolution: auto, value: rsrp, agg: mean, min_count: 3}
    legend: {title: "Mean RSRP per hex (dBm)"}
```

**Two rates at once: a bivariate choropleth.** When the person asks how two measures line up across areas ("site density against income",
"coverage gap against population"), give the choropleth a `bivariate:` block instead of a `value_field`. Each field is cut into 3 classes
(quantile unless the axis names another `method`; `classes: 2` makes a 2 x 2 grid) and every area takes one of nine colours computed from the
brand's diverging ramp: one hue to the right, the other up, darkest where both are high. The key is the grid with an arrow along each axis, the
field's title beside it and the class edges at the ticks. Both fields must be rates, not counts (MAP-CART-001 checks each); fewer than about nine
areas per cell is a WARN (MAP-CART-018); when the dark theme's grid is too close under colour-vision simulation the rows get hatch patterns
(`patterns: auto`, the default).

```yaml
  - id: tracts
    source: {path: tracts.gpkg}
    role: choropleth
    bivariate:
      x: {field: sites_per_10k, title: "Sites per 10,000 people"}
      y: {field: income_k, title: "Median household income ($k)", method: quantile}
```

**Other classed looks.** `classification: {method: log}` gives classes of equal ratio (values above zero only) and `head_tail` follows a heavy
tail (a few very large values: the class count comes from the data, `classes` is the most). A classed lines layer is keyed with line samples; add
`symbol: {graduated: true}` to draw its classes as wider lines in one colour (`widths_mm: [...]` sets them; MAP-CART-019 checks they can be told
apart). `classification: {patterns: auto}` hatches the classes of an area layer when colour alone fails the colour-vision check (`true` always).
A proportional layer with `legend: {nested: true}` keys its sizes as nested circles with leader lines. A layer with `time_field: on_air` (dates,
years, months, quarters or named periods) can be made into one map per period with `map-atlas` (`by: time`); a single map of one period takes `time: {at: 2026-06}` on the layer (`cumulative: true` for everything up to it).

**Rasters, relief and service pictures go on the map as layers too.** All four are read for the map's own window once its extent is known (the
frame plus a margin, in the map's projection, at the export's pixel size, at most 4,096 px a side) and drawn as embedded pictures placed by their
box; the build reads the network only for a DEM, a service picture or a population raster, caches each, and a second build is offline.

- `role: raster` with `source: {type: file, path: grid.tif}` (or `raster: {path: ...}`, or a library raster such as
  `{type: ref, id: ref.statistic.world.population.ghsl}`, fetched for the window): coloured on the brand's ramp, never a hex. The `raster:` block:
  `band`, `ramp: sequential | diverging | categorical`, `vmin`, `vmax` (default: the 2nd and 98th percentile, rounded), `midpoint` (diverging),
  `classes: [breaks]` (a classed raster with a box legend; without it the legend is one continuous bar), `labels`, `opacity`, `resampling`, `nodata`
  (a value drawn transparent, 0 for a population grid), `unit` (added to the legend title). A sequential ramp starts at the land colour so low values
  fade into the ground. The provenance records the file's SHA-256, the band, the window and the resampling.
- `role: hillshade` with `dem: dem.tif` (nothing is fetched) or `source: {type: ref, id: ref.terrain.us.dem.3dep}` (fetched for the window):
  multidirectional relief in two brand neutrals at 35 percent, above the land and under every layer, never in the legend or the extent; its credit
  goes in the source note.
- `role: contour` with a DEM the same way and `contours: {interval: 50 | auto, index_every: 5}`: thin neutral lines, every fifth one heavier and
  labelled with its elevation along the line; the interval goes in the source note.
- **Terrain as context**: `terrain: {on: true}` draws relief from the authoritative DEM for the place: USGS 3DEP inside the United States (public domain),
  the Copernicus DEM GLO-30 everywhere else (keyless, from the AWS Open Data bucket; at most 16 one-degree tiles, GLO-90 where a 30 m tile is not
  public, its one-sentence notice printed for you), or `terrain: {source: dem.tif}`; `contours: {on: true}` adds contour lines. Terrain is off by default; `context: {preset: full}` on a
  regional, metro or local sheet also brings it when the DEM can be had, and `context: {preset: terrain}` brings relief alone. A fetch that fails never
  stops the map: it is built without relief, with a note (MAP-TER-004).
- **A WMS or ArcGIS picture as a print layer**: `role: raster` with `source: {type: service, mode: image, protocol: wms | arcgis, url, layers}`.
  The picture is requested in Web Mercator in tiles (1,024 px for WMS, 2,048 px for a MapServer), each placed on the box the server drew, warped into
  the map's projection, and cached; the service's own legend (GetLegendGraphic, or the MapServer's swatches with Inter labels) goes in the map legend.
  State the service's licence in `provenance`. A service picture has no extent of its own: another layer, or `extent:`, sets the window.

Relief under a site map, from the DEM the person sent:

```yaml
basemap: {provider: bundled}
terrain: {on: true, source: dem.tif, contours: {on: true, interval: 50}}    # or source: auto (3DEP in the US, Copernicus GLO-30 elsewhere; cached)
layers:
  - id: sites
    source: {type: file, path: sites.csv, x: lon, y: lat, crs: "EPSG:4326"}
    role: points
    legend: {title: "Candidate sites"}
    provenance: {source: "Client site list", license: "client-confidential", retrieved_at: "2026-11-05"}
labels: [{layer: sites, field: site_id}]
```

The vegetation picture under the power lines (the Day 2 map, one spec away):

```yaml
layers:
  - id: vegetation
    role: raster
    source: {type: service, mode: image, protocol: arcgis, url: "https://egis.fire.ca.gov/arcgis/rest/services/FRAP/fveg_WHR13/MapServer", layers: [0]}
    raster: {opacity: 0.85}
    legend: {title: "Vegetation (CAL FIRE FRAP, WHR13)"}
    provenance: {source: "CAL FIRE FRAP California Vegetation (fveg22_1)", license: "attribution", attribution: "Vegetation: CAL FIRE FRAP."}
  - id: hv
    source: {type: file, path: ca_hv.geojson}
    role: lines
    filter: {column: VOLT_CLASS, in: ["220-287", "345", "500"]}
    legend: {title: "Power lines, 220 kV and above"}
    provenance: {source: "HIFLD electric power transmission lines", license: "public-domain", retrieved_at: "2026-11-03"}
```

Three ops read rasters and terrain: `raster_zonal {raster: pop.tif | ref.statistic.world.population.ghsl, stat: sum}` (people under each polygon;
GHSL tiles and WorldPop country files are fetched for the window and credited, CC BY 4.0), `line_of_sight {to: <layer> or every pair, heights:
{a_field | a_m, b_field | b_m}, freq_ghz, k}` on a point layer drawn with `role: lines` (clear and obstructed in two brand colours, with a methods
sentence naming k, the heights, the DEM's cell size and the bare-earth caveat), and `viewshed {h_m, radius_m}` on a point layer drawn with
`role: polygons` (the visible ground). Both take `dem: dem.tif`, or use `terrain.source`, or fetch a DEM for the sites' window.

Elements (each `{on: true|false, position: upper-left|upper-right|lower-left|lower-right|auto}`): title, legend, north_arrow, scale_bar, graticule,
inset_map, logo, source_note, date_stamp, confidentiality_banner, methods_page (off by default), terrain (the same switch as `terrain.on`). The brand is written into the spec the first time a map is built, so a later
default change never restyles it.
