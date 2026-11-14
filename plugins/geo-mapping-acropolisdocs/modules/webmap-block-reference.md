---
name: webmap-block-reference
description: >
  KNOWLEDGE MODULE - read by webmap-create. The `webmap:` block of a MapSpec, option by option: basemaps, layers and popups, the frame and its panels, search, the time slider and its named periods, swipe, and style packs on a web page.
---

# webmap-block-reference

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
  time: {step: 1, play: true}          # numbers step by `step`; dates by auto (default), instant, second, minute, hour, day, week, month, quarter or year; times of day (08:15:30) work; mode: period | cumulative
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

## Named periods on the time slider (1.3.0)

A `time_field` of names ("Phase 1", "Wave 2") works on the slider: the periods and their order come from the reader the print atlas uses
(natural order, or `webmap.time.order`, or the layer's `time_order`; values the order leaves out are hidden and counted, MAP-WEB-043).
`webmap.time.labels: {value: label}` renames periods for display; `webmap.time.label_format: "km {value:.1f}"` formats a numeric period.

## Style packs on the web (1.4.0)

A web page built from a MapSpec with `style.pack` takes the pack's palette, and its fonts where the pack's web level allows (antique and
sketch: palette and fonts). Ornaments and treatments (frames, cartouches, paper, water-lining, glyphs, sketch lines) are print only; the
build lists what was left out (MAP-STYLE-008). Labels drawn by the vector map engine are in the pack's label face too (1.10.0: antique,
blueprint and sketch ship glyph files for it; a pack of your own without a `glyphs/` folder keeps those labels in Inter).
