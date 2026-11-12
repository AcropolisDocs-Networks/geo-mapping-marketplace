---
name: webmap-create
description: >
  Make an interactive branded web map as one self-contained HTML page: layers, popups, time slider, swipe, 3D, story maps. Trigger
  on: interactive map, web map, AcropolisDocs map, branded map.
---

# webmap-create

Turns a map request into one HTML file the person opens in a browser, emails, or hosts. It is the same MapSpec as `map-create`, plus an optional
`webmap` block. The page carries its data, its fonts and its map engine inside it; a page on the bundled background makes no network request at all.
For a page that goes to a client or a team, run the `webmap-build` pipeline through `map-run` instead: it plans with a cost line (features,
page size, engine, basemap, network) and delivers a folder with the page, the MapSpec, provenance, the lint report and a page report.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/lints/mapspec_lint.py" map.yaml
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats html [--strict]     # add svg,pdf,png for the print set as well
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
| `map-text`               | before writing the title, subtitle or About text |
| `webmap-build`           | for the engine limits and what the build embeds |
| `webmap-controls`        | when choosing the page's controls |
| `webmap-render`          | for how layers, legends, popups and labels are drawn |
| `tile-provider-registry` | when picking the background |
| `tile-licensing`         | before naming any tile provider |
| `classify-and-color`     | for classification |
| `color-accessibility`    | when choosing colours |
| `telecom-geo`            | for sites, sectors, masts and link layers |
| `geo-service-explore`    | to save the host-confirmed public profile a WMS or WMTS layer needs |

Exit codes are the same as `map-create`: 0 built, 3 a BLOCK finding, 2 a request this release cannot do.

## Order of work

**A fresh cloud workspace has no map packages** (`python3 -c "import geopandas"` fails): run `python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick`
once, before anything else. It installs what a map needs in one pip step, wheels first, within 10 minutes (1.2.0); never `pip install -r
requirements-dev.txt`, which brings the test stack. On the person's own computer, ask before installing.

1. **Same first steps as `map-create`**: find the message, inspect uploaded files, use the library first, pick the map type from the data, write the MapSpec.
2. **Decide the page's audience.** `audience:` says who opens the page (`internal`, `external`, `regulatory`, `any`). An unstated audience is treated as public.
   The audience decides which tile providers may be used and whether a confidential layer may go on the page at all.
3. **Pick the background** (read `tile-provider-registry`). The default is `basemap: {provider: bundled}`: country, state and lake outlines that ship with the plugin.
   A tile provider (`carto-positron`, `maptiler`) is used only with the person's own **browser-class** key, restricted to their domain and registered with
   `/geo-mapping-acropolisdocs:map-credentials add <provider> --class browser --confirm-restricted`; otherwise the page falls back to the bundled background
   (MAP-WEB-015 says how to add one). A `secret`-class key is never written into a page (MAP-CRED-011). Never ask for a key in the chat and never write one into the spec.
4. **Leave the frame at its defaults** (read `webmap-controls`, the `webmap.frame` reference). The page has one panel (title, then the Story,
   Legend, Layers, Details and About tabs; a tab with nothing to show is left out), one control rail on the map, one playback dock when there is
   time, the brand's small logo in the map's bottom-left corner (as on the print map; `brand: {show: chip}` adds the name on a pill) and the required credits in its bottom-right corner. Change it only when the person asks:
   a preset (`minimal` for a single-message map, `presentation`, `embed` inside an article), the panel's side or start state, the playback label
   (`frame.playback.label: "km {end}"`), or a co-brand. Give layers a `name:` when two would share one in the Layers tab (MAP-WEB-037).
   The page carries the same place names and context as a print map: the `names:` block (city, water-body and state names from the bundled Natural
   Earth 10m data, tiered by zoom; `names: {on: false}` turns them off) and the `context:` block (roads, urban areas, lakes and rivers from the same
   data; presets `auto`, `none`, `roads`, `urban`, `water`, `full`). On the bundled background `auto` draws the context; over a tile basemap it draws
   none, because the tiles already carry roads and names.
   **Rasters and relief go on the page as pictures.** A `role: raster` layer (a GeoTIFF, a library raster, or a WMS or MapServer picture) and the relief
   of `terrain:` or a `role: hillshade` layer are the same pictures the print map draws, warped to Web Mercator and carried inside the page as Leaflet
   image overlays (data URIs: the page still calls no host for them). Relief sits under every layer; a raster sits over the background and under the
   vector data, with an opacity slider beside its toggle in the layer list, and its legend is the same continuous ramp or classes as on paper. Each
   picture is at most 2,048 px a side on the page; a larger print picture is carried smaller and the notes say so. Contours are line layers.
5. **Let the engine follow the page** (read `webmap-build` for the limits). Leave `webmap.engine` at `auto`: small pages are drawn with
   Leaflet; MapLibre takes over when the page carries more than 20,000 features, a layer is large enough to go in as tiles, the basemap is
   vector tiles, or the time slider or the swipe compare is on. Name an engine only when the person asks for one. MapLibre draws large layers
   from tiles embedded in the page, groups dense points into numbered clusters until they stand apart, sets every label in Inter, and saves
   the view as a PNG; it needs a browser with WebGL (the static picture shows otherwise).
6. **Build, then look.** Build the page and report the findings, with every line of `map_text.callout` (the MAP-TEXT findings, never blocking).
   The `webmap-build` run opens the page in a headless browser at 1440x900, 1024x768 and 390x844 (the layout check) and keeps a screenshot of
   each size and state in the deliverable's `layout/` folder: look at them. The panel's header carries the same title and subtitle as the
   printed map, written by the `map-text` module (subject-led, Title Case; the About text may run to 3 sentences).
7. **Fix, do not argue.** A BLOCK is a rule the page breaks (an undeclared network host, a refused provider, a confidential layer headed for a public artifact). Change the spec and rebuild.

## The webmap block (all optional)

```yaml
audience: internal
basemap: {provider: osm}               # bundled (default) | none | carto-positron | osm (a customization: the one acknowledgment, /geo-mapping-acropolisdocs:map-services)
                                       # | protomaps (vector tiles, your browser key; free tier non-commercial) | pmtiles + source: pmtiles:FILE
                                       # | nasa-gibs (imagery, no key; time: YYYY-MM-DD) | esri, mapbox (the person's own browser key)
layers:
  - {id: sites, role: points, time_field: in_service_year, ...}       # a time field puts the layer on the time slider
webmap:
  engine: auto                         # auto (default) | leaflet | maplibre
  theme: light                         # light uses the print colors, dark the screen colors
  basemaps: [carto-positron, bundled]  # offered in the basemap switcher; first is the default
  frame:                               # 1.1.0: the frame (webmap-controls); every key optional, auto shows an element only when it has content
    preset: auto                       # auto (story when there is a story, else standard) | minimal | presentation | embed
    panel: {side: left, start: open}   # the panel pushes the map on a desktop, is a drawer on a tablet and a bottom sheet on a phone
    playback: {label: "km {end}"}      # the dock's label: {start} {end} {value}; never a raw "6 to 6.2"
    brand: {co_brand: {name: Partner Co, url: "https://example.org"}}   # the logo comes from the brand pack; a co-brand's logo sits after a divider
    tools: [measure, save_image, print, copy_link, download]   # the ⋯ menu; each still gated by its own rule and by the delivery
  search_field: name                   # which attribute search looks in (place names are searched too)
  time: {step: 1, play: true}          # numbers step by `step`; dates by day, week, month, quarter or year; mode: period | cumulative
  compare: {left: [sites_2020], right: [sites_2026], left_label: "2020", right_label: "Now"}   # a draggable divider; other layers on both sides
  basemap_file: embed                  # a PMTiles basemap inside the page (default; over 40 MB it goes beside the page) | beside
  initial_view: {mode: fit_layers, max_zoom: 12}
  delivery: [file]                     # add artifact only for a page that is safe to publish
  confirm_publish: false               # set true only after the person agreed to publish a confidential layer
```

What to ask for, in the person's words:

- "Show it by year" or "animate the rollout": a `time_field` on the layer and `webmap.time`. The legend keeps the classes of the whole layer,
  so colours mean the same in every period. Features with no time value are hidden while the slider is on (the notes count them).
- "Before and after", "compare 2020 with now": `webmap.compare` with the layers for each side.
- "100,000 sites" or "every tower in the state": nothing to set. The page goes to MapLibre, the sites go in as tiles with clusters, and the
  page stays under 16 MB for that many points with a few short fields. Keep only the fields the popup needs.
- "A street background without a tile account": the person's own PMTiles basemap (`basemap: {provider: pmtiles, source: pmtiles:FILE}`),
  drawn in the brand's colours. The Protomaps API (`provider: protomaps`, a browser-class key registered with `/geo-mapping-acropolisdocs:map-credentials add protomaps --class browser`) is free for non-commercial
  pages only; a client deliverable needs the provider's paid plan. The key is the person's own acceptance of the terms: say it once, do not ask again.
- "Measure distances": on by default, in the units of the map's scale bar (miles in the United States).
- "A picture for a slide": `Save image` (the ⋯ menu) on a MapLibre page gives a PNG of the view with the title, the legend, the brand logo and
  the credits in full; on Leaflet, Print. A page published as an artifact cannot start a download, so it leaves Save image, Print and Download
  data out (MAP-WEB-041) and its About tab says the file version has them.
- "Make it clean", "just the map": `frame: {panel: {start: collapsed}}` (a title chip and the full window) or `preset: minimal`.
- "Show the server's live layer", "the flood zones as they are today" (0.15.0): a live layer the viewer's browser draws from the service,
  web pages only. It needs a saved, host-confirmed public profile without a login (`geo-service-explore` saves one):
  `{id: flood, role: raster, source: {type: service, mode: live, profile: P, protocol: wms, layers: [ns:flood], time: "2026-11-01"}}`;
  `protocol: wmts` or `xyz` with `url_template`; `mvt` with `url_template`, `source_layer`, `geometry` (MapLibre only); `wfs` with `layer`,
  `geometry`, optional `conditions` and `max_features` (read once for the page's area). Run `ogc_cors_check` first and record the day as
  `cors_checked` for a WFS, an MVT, a WMS on MapLibre or WMS popups. Fetch the WMS legend with `ogc_get_legend` so it is embedded. Tell the
  person a live layer is not reproducible (MAP-SVC-014), the static picture leaves it out, and a printed map needs a capture instead.

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

## Rules the builder enforces

- **No network host that is not declared.** The only hosts a page may call are tile hosts from a registry entry, listed in a host manifest and in the page's content security policy. Analytics, web fonts and CDN scripts are refused (MAP-WEB-001, -002).
- **Keys are public once they are in a page.** Only a browser-class key goes in, with MAP-WEB-003 reminding the person it is visible; without the recorded
  domain restriction the page also warns MAP-CRED-009. A secret-class key in a page is refused (MAP-CRED-011).
- **Attribution cannot be hidden** (MAP-WEB-004, -030, -031). The credits a provider's terms or a dataset's licence require sit in the map's
  bottom-right corner, built from the library's `attribution:` blocks (one holder once: one OpenStreetMap credit serves the tiles and every
  OpenStreetMap-derived layer), linked, and never covered (MAP-WEB-034); a provider logo the terms require (Mapbox, MapTiler on a free account)
  keeps its corner. Method notes and full provenance are in the About tab. An exported picture carries the credits in full, with the
  OpenStreetMap address when its data is on it. For the person's own file give `provenance.credit: {corner: ..., links: [...]}`. A file made from a library source names it with `provenance.library_entry`, and the corner then carries that source's credit and any logo its terms require (OpenWeather's, 1.1.3).
- **Only what the engine needs.** The page's content security policy allows nothing more (MAP-WEB-017): a MapLibre page adds `worker-src blob:`
  and `child-src blob:`, and network access only to the chosen basemap's host.
- **Confidential layers.** A client-confidential layer puts a banner and a `noindex` tag on the page. It never goes into an artifact unless the person confirmed (`confirm_publish`) (MAP-WEB-013). Say this before publishing, in one sentence.
- **Colors from the brand tokens, Inter only, accreditation in metadata only**, as for every map. The brand's visible mark is its small logo in the
  bottom-left corner, as on the print map (from the active brand pack; no name, no pill behind it); switching it off needs a reason (MAP-WEB-036).
- **A page that cannot start shows its picture and a message**, not a blank screen.

## Artifacts

A page published as an Artifact runs under the artifact host's content security policy, which may block tile requests. The bundled background always works.
Treat a tile basemap in an artifact as "may not load" until it has been seen loading; the fallback message tells the reader what happened. Never publish a page that holds
a confidential layer unless the person asked for exactly that.

## Tell the person

Lead with the file and one sentence on what it shows and how to use it (pan, zoom, click a feature, toggle layers, Print). Then at most three findings that change what they
would do: the background used and why, a key reminder, a confidentiality note. Offer one next step: a print set (`--formats svg,pdf,png,html`) or a different background.

## Named periods on the time slider (1.3.0)

A `time_field` of names ("Phase 1", "Wave 2") works on the slider: the periods and their order come from the reader the print atlas uses
(natural order, or `webmap.time.order`, or the layer's `time_order`; values the order leaves out are hidden and counted, MAP-WEB-043).
`webmap.time.labels: {value: label}` renames periods for display; `webmap.time.label_format: "km {value:.1f}"` formats a numeric period.

## Style packs on the web (1.4.0)

A web page built from a MapSpec with `style.pack` takes the pack's palette, and its fonts where the pack's web level allows (antique and
sketch: palette and fonts). Ornaments and treatments (frames, cartouches, paper, water-lining, glyphs, sketch lines) are print only, and
labels drawn by the vector map engine stay in Inter; the build lists what was left out (MAP-STYLE-008).
