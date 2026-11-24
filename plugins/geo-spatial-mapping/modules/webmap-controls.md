---
name: webmap-controls
description: >
  KNOWLEDGE MODULE - read by webmap-create. The web map page's frame (webmap.frame, 1.1.0): the panel, the control rail, the playback dock,
  the brand chip and the credits corner, the presets, every key and its default, the 1.0.x webmap.controls keys and where they went, and how
  the page behaves on a phone, with the keyboard, in print, as an artifact and when its address is shared.
---

# webmap-controls

The frame has one design on both engines and both themes. **Map first, panel second, overlays last.**

| Part | Where | What it holds |
|---|---|---|
| Panel | left (360 px, pushes the map) on a desktop; a 320 px drawer on a tablet; a bottom sheet (peek, half, full) on a phone | Header: logo, title, subtitle, tagline, search, copy link, the ⋯ tools menu, collapse «. Tabs: Story · Legend · Layers · Details · About (a tab with nothing to show is left out). Footer: Previous, the chapter dots, Next. |
| Title chip | top left, when the panel is collapsed or off | », the logo and the title; on a story page the caption card under it (chapter, its text clamped to 3 lines, ‹ ›) |
| Control rail | top right | zoom in and out · compass (shown when the view is rotated or tilted) · 2D/3D · reset view (always) · full screen |
| Playback dock | bottom centre of the visible map | Play (the one filled button), step back and forward, the slider with chapter markers, the period label, speed, loop; one row |
| Brand chip | bottom left (in the sheet's header on a phone) | the brand's logo and name from the active brand pack, linked to its homepage; a co-brand after a divider |
| Credits corner | bottom right, never covered | the required credits of the background and the shown layers, each holder once, linked; ⓘ opens the full credits and the sources |

## webmap.frame (every key optional)

| Key | Default | Use |
|---|---|---|
| `preset` | `auto` | `auto` is `story` when `webmap.story` is set, else `standard`. `minimal`: no panel, a floating legend. `presentation`: panel collapsed, the rail fades after 3 s idle, autoplay. `embed`: panel collapsed, no copy link (pages inside articles). A key set here wins over the preset. |
| `panel.show` | `auto` (on) | `off` draws no panel: the title chip, the rail, the dock, the brand and the credits remain |
| `panel.side` | `left` | `right` for a page that reads better with the map on the left |
| `panel.width` | 360 | 320 to 440 px on a desktop |
| `panel.start` | `open` | `collapsed` gives the map the whole window at first; the viewer's last choice is remembered (browser storage) and the shared link carries `p=0` or `p=1` |
| `panel.tabs` | all five | order and choice; empty tabs are left out; at most five |
| `panel.default_tab` | `auto` | Story when there is a story, else Legend |
| `title_chip` | `auto` | the title chip when the panel is collapsed or off |
| `controls.zoom` | `auto` | on; hidden on touch screens (pinch) |
| `controls.compass` | `auto` | shown when the view is rotated or tilted (3D pages); a click returns to north and flat |
| `controls.dimension` | `auto` | the 2D/3D button on a 3D page |
| `controls.fullscreen` | `on` | |
| `playback.show` | `auto` | the dock when `webmap.time` is set; `off` fixes the time at its first period |
| `playback.mode` | `full` | `slider` leaves out Play, step, speed and loop; `hidden` draws no dock |
| `playback.position` | `map` | `panel` puts the same row in the panel's footer (an empty map on a story page) |
| `playback.label` | the slider's own | `"km {end}"`: tokens `{start}`, `{end}`, `{value}`; numbers keep `webmap.time.decimals`, dates use `webmap.time.format` (strftime). Never a raw "6 to 6.2". |
| `playback.speeds`, `loop`, `autoplay` | `[0.5, 1, 2, 4]`, off, off | speed is a multiple of `webmap.time.interval_ms` |
| `legend.float` | `auto` | a floating legend (top right, beside the rail) when the panel is collapsed or off; on for `minimal` only |
| `legend.toggles` | `true` | an eye on each legend block, in step with the Layers tab; a hidden layer's block stays, greyed, marked hidden |
| `brand.show` | `chip` | `mark` (logo only, for embeds under 480 px); `off` needs `reason:` (MAP-WEB-036, the chip is drawn otherwise) |
| `brand.co_brand` | none | `{name, url, logo_svg}`: drawn after a divider inside the same chip |
| `attribution.collapse` | `auto` | each credit's own rule, on a phone after the first interaction; `never`. The corner cannot be switched off (MAP-WEB-031). |
| `scale_bar` | `auto` | right of the brand chip, in 2D only |
| `locator` | `off` | reserved: the web page draws no locator inset yet |
| `search` | `auto` | on above about 20 named features; the header's search button |
| `popups` | `auto` | with the panel open a click fills the Details tab; collapsed or off, a popup. `popup`, `panel`, `off` force one way. |
| `tools` | `[measure, save_image, print, copy_link, download]` | the ⋯ menu. Each keeps its own rule: download only for layers whose licence allows it; on Leaflet Save image is Print. A page delivered only as an artifact leaves out Save image, Print and Download (MAP-WEB-041). |
| `shortcuts` | `on` | the keys below and their help (`?`) |

`webmap.story` adds `cards: compact | full` (compact: the active chapter expanded, the others one line), `navigation: buttons | scroll | both`
(scroll: a chapter becomes active when its card crosses 35% of the panel), `follow_time` (playing the slider activates the chapter whose period
it reaches) and `print_pictures: on | off | first` (each chapter's picture in the print view is about 1.3 MB on a 3D page). A chapter's `time`
is a value or a label and the value wins (`time: 6` is the period holding 6); `time_index:` names a period by its index. A chapter may name the
relief by its spec id.

`layers[].name` is the layer's name in the Layers tab. Without it: the legend label (a one-item legend block), the legend title when no other
layer shares it, else the id in words. Two layers with one name: MAP-WEB-037.

## The 1.0.x webmap.controls keys

They still work; each raises MAP-SPEC-008 (INFO, "moved to webmap.frame").

| 1.0.x | 1.1.0 |
|---|---|
| `title_panel: false` | `frame.panel.show: off`, `title_chip: off` |
| `legend: false` | the Legend tab is left out |
| `layer_switcher`, `basemap_switcher` | the Layers tab (a background list when `webmap.basemaps` names more than one) |
| `opacity` | in each layer's ⋯ menu; pictures (raster, relief) and buildings keep the slider in the row |
| `popups`, `scale`, `search`, `fullscreen`, `compass` | `frame.popups`, `frame.scale_bar`, `frame.search`, `frame.controls.*` |
| `data_download`, `print_view`, `export_image`, `measure`, `share_state_in_url` | `frame.tools` |
| `time_slider` | `frame.playback.show` (`time.play: false` is `mode: slider`) |
| `logo: false` | `frame.brand.show: off` with that reason |
| `error_banners`, `cluster`, `compare` | unchanged |

## Credits on the map

The corner shows the required credit of the background in use and of each shown layer whose licence requires one, in draw order, joined by
" · ", each holder once (one "© OpenStreetMap contributors" serves the OpenStreetMap tiles, Overture and any other OpenStreetMap-derived layer),
linked as the provider's terms ask. The wording comes from the library entry's `attribution:` block (`corner`, `links`, `full`,
`required_on_map`, `collapse`, `logo`, `export`); an entry without one gives its `license.attribution_text`. A public-domain credit (Natural
Earth, the bundled background) is a courtesy: it is in About and the ⓘ popover, not the corner. Switching the background or hiding a layer
changes the corner at once. `collapse: never` (Esri, provider logos) stays in full; `after_interaction` (OpenStreetMap) may shrink to its short
form on a phone after the first pan or zoom, after 5 s; the ⓘ always leads back to the full text and the licences. A provider whose terms require its logo (OpenWeather, 1.1.3) has it drawn, linked, before its credit, from `attribution.logo`, and in Save image. For the person's own file,
`layers[].provenance.credit: {corner: "© Example Agency", links: [{text: ..., href: https://...}], collapse: after_interaction}`.

## Behaviour to expect

- **Phone (under 640 px).** The panel is a bottom sheet: at its peek the brand, the title and the story navigation or the play row; the map is
  at least 60% of the screen. Drag or tap the handle for half and full. Touch targets are 44 px; zoom buttons are hidden (pinch).
- **Tablet (640 to 1023 px).** The panel is a 320 px drawer over the map; it starts open on story pages, collapsed otherwise.
- **Keyboard.** `[` panel · `1`–`5` tabs · ← → chapters (a step of time when the dock has focus) · Space play · `+` `-` zoom · `0` reset · `f`
  full screen · `?` help · Esc closes menus. Every control is a button or input with a name and a 2 px focus ring.
- **Shared link.** `#v=<zoom>/<lat>/<lon>&l=<layers>&b=<background>`, plus `n=0`, `t=<period>`, `c=<divider %>`, `d=2`, `ch=<chapter>`,
  `p=0|1` (panel) and `tab=<tab>`.
- **Print.** Page 1 is the view with the title chip, the legend of the visible layers, the brand chip and the credits in full; no tools. A story
  page prints its chapters.
- **Save image** (MapLibre) burns in the title, the legend (top right), the brand chip (bottom left) and the credits in full; the hidden
  accreditation is in its metadata.
- **Reduced motion.** No animation.
- **The layout check.** `webmap-build` opens the page at 1440x900, 1024x768 and 390x844 in a headless browser: a frame element over the credits
  or a provider logo is a BLOCK (MAP-WEB-034); overlaps, a map under 75% of the desktop window (60% of a phone at the peek), small touch targets
  and a story card mostly empty are warnings (035, 039, 040, 038). Its report and a screenshot per size and state are in the deliverable's
  `layout/` folder.
- **Engines.** The frame is one controller (`assets/web/frame.js`) over `page.js`; each engine adapter adds only zoom, bearing and pitch,
  resize and padding. Clustering, 3D and the PNG export are MapLibre's; everything else works on both.
