---
name: webmap-3d-story
description: >
  KNOWLEDGE MODULE - read by webmap-create. 3D pages (terrain, extruded buildings, sky, link profiles) and story maps (chapters, camera, print pictures): the options and their limits.
---

# webmap-3d-story

## 3D and story maps (M15)

```yaml
terrain: {on: true}                    # the same DEM as the print relief: 3DEP in the US, terrarium elsewhere (credit printed)
layers:
  - {id: buildings, role: buildings, source: {type: file, path: buildings.parquet, bbox: [-118.37, 33.93, -118.25, 34.06]}}
     # the person's own Overture or Microsoft file. role: buildings is polygons extruded from height, then num_floors x 3.5 m, then 9 m,
     # with the legend title "Buildings"; an extrude: block of its own overrides that
  - {id: masts, role: points, mast: {height_field: antenna_m}}                                    # a column to the antenna height
  - {id: sectors, role: polygons, wedges: {azimuth: azimuth, radius_m: 150}, extrude: {at_field: antenna_m, slab_m: 3}}
  - {id: links, role: lines, ops: [{op: line_of_sight, to: masts, ...}]}                          # drawn between antenna tops in 3D
webmap:
  view: {pitch: 60, bearing: -25}      # any pitch or bearing makes the page 3D (MapLibre)
  3d: {terrain: true, exaggeration: auto, max_dem_mb: 4}     # auto: 1 on local maps, 1.5 metro, 2 coarser
  panel: {title: Site figures, fields: [peak_users, {field: throughput_gbps, label: Throughput, unit: Gbps}]}
  story:
    chapters:
      - {title: SoFi Stadium, text: "...", view: {center: [-118.339, 33.953], zoom: 15.5, pitch: 60, bearing: 30},
         layers: [buildings, sectors, masts], time: "Jun 2028", select: {layer: masts, field: venue, in: [SoFi Stadium]}}
```

What to ask for, in the person's words:

- "Show it in 3D", "tilt the map", "a 3D venue page": `webmap.view: {pitch: 60}`. With a DEM on the map (`terrain: {on: true}` or a
  hillshade layer) the ground is 3D too; `webmap.3d: {terrain: true}` turns the terrain on for a page that has none. Outside the United
  States the DEM is the Copernicus DEM GLO-30 (1.5.0, keyless): its notice is printed for you.
- "Buildings in 3D": the person's own footprints (Overture or Microsoft, GeoParquet or GeoJSON; the library entries say how to get them for
  a box). Read only the window with `source.bbox` (lon/lat for those files). Heights come from `height_field`, then floors times
  `floor_m`, then `default_m`; the notes count each. They are drawn in one brand neutral unless the layer is classified.
  Over 20,000 footprints (1.6.0): every footprint is on the page from page zoom 16, and below it a level-of-detail pyramid of blocks
  (footprints within `extrude.merge_gap_m` and one `height_band_m` merged, simplified, tiny blocks dropped at that zoom only), so the
  buildings stay on wider views; each zoom keeps 98 percent of the area or MAP-WEB-018 says so. `extrude: {lod: off}` keeps the 1.5.0 tiling;
  `lod:` with the same keys does it for any large polygon layer.
- "A sky", "haze on the horizon": a page tilted over 30 degrees gets a sky and haze in the page's colours (`webmap.3d.sky: on | off`). The
  terrain reaches the ground the camera can see at the page's highest tilt (a coarser DEM beyond the window, fetched once); offline or with a DEM
  of your own the highest tilt comes down to what is covered (MAP-WEB-044).
- "Masts with their heights": `mast: {height_field}` on the sites layer. "Sectors at antenna height": `wedges` plus `extrude: {at_field, slab_m}`.
- "Line of sight in 3D": a `line_of_sight` layer is drawn between antenna tops at the ray's clearance over the ground shown, clear and
  obstructed in the print colours; a click on a link opens its profile chart.
- "A story map", "walk the client through it": `webmap.story.chapters`, each with a view, the layers it shows, a period (with the time
  slider) and a selection for the KPI panel. Check that each chapter's view shows its layers (MAP-WEB-026 warns when it lands on an empty
  view). A pitched view shows more ground at the top than a flat one; give a centre and a zoom rather than a box for a close view.
- "A panel with the numbers": `webmap.panel.fields` from the layer or its joined table (`join`); clicking a feature fills it, a chapter's
  `select` shows totals (sum, or `agg: mean | min | max`).

Tell the person: the page opens in 3D with a 2D button under the zoom buttons; drag with the right mouse button (or two fingers) to tilt and
turn; the compass turns it back to north; the scale bar shows in 2D only; the printed page and the static picture are flat. The terrain's
detail is in the notes (the zoom and metres a pixel the page carries). A story page prints its chapters, each with its own map.
