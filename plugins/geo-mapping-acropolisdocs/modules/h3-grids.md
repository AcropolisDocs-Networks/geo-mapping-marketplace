---
name: h3-grids
description: >
  KNOWLEDGE MODULE - read by map-create and geo-analyze. Data that arrives on the H3 grid (FCC mobile hexagons, an h3 id column): reading it
  without building geometry, rolling it up with the share each group covers, the complete grid over a region, choosing the resolution from
  the page, drawing the cells and the empty grid, and writing the cells out.
---

# h3-grids

H3 is the grid the FCC publishes mobile coverage on, and the natural grid for drive tests and any comparison across maps: a cell is the same
cell on every map. `role: hexbin` puts points and polygons on H3; `source: {type: h3}` reads ids that are already there.

```yaml
region: {place: Idaho Panhandle}
layers:
- id: carriers
  source: {type: h3, path: "data/fcc-bdc/*.gpkg", id_field: h3_res9_id, where: "environmnt = 0"}   # or adapter: fcc-bdc-mobile
  h3: {resolution: auto, by: brandname, share_min: 0.5, agg: count_groups, complete: region, mode: centre}
  role: choropleth
  value_field: carriers
  classification: {method: discrete}
  empty: outline
```

- **Reading.** Only the id, group and value columns are read (GeoPackage through GDAL with `where` as SQL; Parquet and CSV through DuckDB
  when the db profile is installed). 1.25 million ids read in about 3 s, roll up in about 2 s. Ids may be hex strings or 64-bit integers.
- **Roll-up.** A cell at resolution r holds 7^(s - r) children of the source resolution s. A group (a carrier) counts in a cell when it
  covers at least `share_min` of the children (default 0.5, printed in the legend note). `agg`: count_groups, share (one `group`), any, all,
  count, sum, mean, max, min (`value_field`). Groups passing are listed in a `groups` field.
- **Complete grid.** `complete: region` makes every cell of the region: `centre` (the cell's centre inside, Day 20), `intersect`
  (touching), `share` (touching, with `area_share`). Cells with no data get 0 and draw as outlines only (`empty: outline|none|fill`).
- **Resolution.** `auto` picks the finest grid whose cells are at least 0.8 mm wide on the page, never finer than the data; under 1.2 mm
  MAP-HEX-002 names the next coarser resolution. Washington on a slide: resolution 7 (0.9 mm).
- **Drawing.** Edges are sized to the cells (at most a quarter point, at most 6 percent of a cell); the empty outlines are see-through by
  density so the relief reads through; one legend group with None first and the cell size in its note; the relief opacity rises to 0.6
  under a cells grid (`terrain.opacity: auto`).
- **Out.** `map_export.py --h3-cells map.yaml --layer carriers --out cells.parquet` (ids only, a few hundred KB) or `.gpkg` / `.fgb`.
- **Recipe.** `recipe.py make carrier-choice-h3 --region "<region>" --out map.yaml` writes the whole carrier-choice map; offer the share map
  of one carrier as the second panel, so a carrier at 49 percent of a cell is not read as absent.
- **FCC files.** On the person's computer the map server's `fcc_bdc_coverage(region=...)` lists every state's hexagon files with their
  size, then fetches them into `data/fcc-bdc/`; `map-run move-data` brings them to the cloud workspace in one step.
