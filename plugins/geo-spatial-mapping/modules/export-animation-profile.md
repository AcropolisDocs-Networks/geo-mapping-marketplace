---
name: export-animation-profile
description: >
  KNOWLEDGE MODULE - read by map-export. An animation of a rollout over time (GIF, MP4) and a link's elevation profile chart: options, sizes and what each needs installed.
---

# export-animation-profile

## An animation of a rollout, and a link's profile chart

- **"Animate the rollout", "a GIF for LinkedIn", "a video of the build-out"**: the maps must first be an atlas made by time (`map-atlas`,
  `by: time`, usually `cumulative: true`). Then `map_export.py --animate <atlas folder> --out out` writes an animated GIF and an MP4 at
  1080 x 1080 (square, for social posts) and 1920 x 1080 (slides), each period redrawn at that size; `--sizes square` or `slide` picks one,
  `--hold 1.0` and `--last-hold 3.0` set the seconds per period and on the last one. A thin bar along the bottom shows how far through the
  run each frame is. The MP4 needs ffmpeg (`pip install imageio-ffmpeg`); without it the GIF is still written and the report says so. Each file
  is read back (MAP-OUT-013: frames, size, timing, H.264 in yuv420p). Say how many frames before starting: one render per period per size.
  5.9.0: every frame is kept in `OUT/.frames/<size>/` under a key of what it is drawn from (its MapSpec, the size, the brand, the files it
  names, the plugin), so a second run redraws only the periods that changed (a new chart, a new hold, a fixed callout cost nothing) and a
  run cut off part way picks up where it stopped; `--fresh` draws every frame again. `--jobs N` (default `auto`: one per core but one, at
  most four) draws the frames after the first in worker processes, which take the first frame's decisions, so the frames match one run's.
  The report's `frames` says per size how many were drawn and how many reused.
  5.10.0: `--fade 0.2` dissolves each period into the next (at most four fifths of the hold; the run keeps its length), in the MP4 with
  ffmpeg's xfade and in the GIF as blended frames a tenth of a second each. The timeline's `story:` lines and `counters:` are drawn on an
  atlas animation too, written by period: `{at: "2002", until: "2015", line: "..."}` (a line with no `until` stays its reading time), and
  a counter's keys `[["1984", 564], ...]` or read from a table: `{text: "{v} km² of water", source: areas.csv, label: year, value:
  area_km2, format: ",.0f"}`. Both stay inside the map frame and clear of the chart and the map's elements.
- **"A chart beside the time-lapse", "change since the first year", "bars up and down from 1984"** (5.8.0): `--timeline T.yaml` on
  `--animate` draws the timeline's `chart:` on every period's frame, inside the map frame and clear of the legend, locator, title and logo
  (the panel the events video uses). Its rows follow the periods (by label when `atlas.json` names `periods`, else in order). Read the rows
  from the data, never type them: `chart: {source: areas.csv, label: year, bar: area_km2}` (the table beside the timeline file).
  `baseline: first` (or `last`, or a number) draws each bar as its difference from that value: a gain rises from the zero line in the
  emphasis colour, a loss hangs below it in the danger colour, the current period at full strength and the line named after the baseline's
  row; the current difference is printed top right with a true minus (`bar_format: "{:+,.0f} km²"`). `reveal: true` adds the bars period
  by period on the whole run's scale; `labels: ends` keeps the first, last and current labels on a long run of years. Only the chart is drawn
  on an atlas animation: a timeline's story lines and counters are said to be left out (they stay with `--story` and `--events`).
- **"Fly through it", "a video of the 3D page", "follow the route"** (1.6.0): always through map-run's `flythrough` pipeline, which shows the
  frame count and the seconds a frame takes from a 10-frame warm-up before anything runs. `--path LAYER` follows a line layer (the camera
  behind a moving marker on the smoothed route, constant speed or a `time_field`), `--chapters` flies the story's chapters, `--orbit` circles
  the centre. A draft (640 x 360 at 12.5 fps) comes first; the final (`--size 1280x720 | 1920x1080 | 1080x1350`, 25 fps) runs only after the
  person has looked at the draft and confirmed. The title, the distance and the provider credits are drawn on every frame in Inter (MAP-OUT-020
  blocks a frame that cannot carry the credits); frames are kept, so `--resume` continues a run; H.264 MP4, and a GIF for 10 s or less. It
  needs Playwright's Chromium (`pip install playwright`, then `python -m playwright install chromium`). `--gl auto` (the default) uses the
  GPU on Windows and macOS and software GL (2 to 6 s a frame) on Linux; the report names the renderer Chromium got, and MAP-OUT-021 warns
  when a GPU run fell back to software. On a time page the slider follows the marker, so a timed route draws in as it is flown.
  The path camera rides the route averaged over its lookahead and turns at most 30 degrees a second (`--steady 2`, `--max-turn 30`), so a
  winding road does not shake the view.
- **"A video of the time slider", "time-lapse of the page"** (1.14.0): `map_export.py --timelapse PAGE.html --out DIR` (the flythrough capture
  with `--slider`): the camera holds the page's own view and each period of the slider is shown for an equal share of the run, the last a little
  longer, its label in the HUD. A MapLibre page (`webmap.engine: maplibre`); the same draft-then-final route through map-run.
- **"Animate the events", "quakes popping in", "a running counter"** (1.14.0): `map_export.py --events map.yaml --layer quakes --time-field time
  --highlight "mag>=7" --counter "{hl} of {n} by {date}" --size slide --out DIR [--quality final]`: the map is built once at the size (the event
  layer stays in the legend only), then each event pops in at its time, a highlighted one ripples in the brand's danger colour and stays, and
  the counter runs in the lower left. A custom overlay of your own lines up with the map through the build report's `map_window`: `extent` (the
  map's window in its own CRS) fills `rect_mm` (millimetres on the page); a point's page position is rect_x0 + (x - x0) / (x1 - x0) x width,
  and rect_y0 + (y1 - y) / (y1 - y0) x height, times dpi / 25.4 for pixels.
- **"Heat that builds up", "the city warms as the reports come in", "month by month"** (2.1.0): on the events video, `--trail settle` flashes
  each event and fades it into a heat surface that grows on one scale (the end's peak, never re-scaled per frame); `--periods month` shows one
  surface per value of a column, all on the busiest period's scale, cross-faded (`--fade 0.8` s), the counter reading "{period}: {n} events".
  The heat is kept on the map's land, coloured from the sequential ramp, clear at the low end; `--bandwidth` sets its kernel in metres (default
  a fiftieth of the map's width). A timeline's `chart: {title, bar_label, line_label, rows: [{label, bar, line, start}]}` adds a panel of bars
  and a line on its own axis, the current row marked and a Fewer-More key for the heat; with `--periods` the rows follow the periods in order.
  It keeps clear of the legend, inset, title and logo (`position: auto`, or a corner). Use this, not a drawing script of your own.
- **"A narrated video", "captions along the bottom", "a year counter", "fly the camera exactly here"** (1.16.0): a timeline file
  (`--timeline T.yaml`, scripts/timeline.py) works with the flythrough and with the map videos alike. `story:` lines (start, end, text) run
  along the bottom, faded over 0.45 s; `counters:` run a number through key times ("AD {v}", "{v} fans"); `end_hold:` holds the last frame;
  `poster: last` opens the video on its final frame held `poster_hold` seconds and dissolving into the flight (X shows the first frame as the
  thumbnail and does not autoplay). Every flythrough frame carries the brand logo bottom left, above the credits.
- **"The city appears building by building", "slow motion through the burst", "a date counter"** (3.4.0): `clock: [[real, video_s], ...]`
  maps real time to video time (story lines may say `at: 2023-02-08`, counters' keys and feature events take real times); a counter
  `{format: date, pattern: "%-d %B %Y"}` shows the clock's date, `{source: reveal}` the features on the map; `reveal: {layer, time_field,
  end_field, class_field, colours, flash_s, before}` brings a polygon or point layer in feature by feature from an id raster (65,000
  buildings), each new one flashing and keyed "Just added". Counters of one corner stack as a column; a long story line wraps on its row.
- **"Add my voice-over", "captions for the player"** (2.0.0): `--audio FILE` (mp3, m4a, aac, wav, ogg or flac: the person's own
  recording; the plugin makes no voice) is laid under a flythrough and cut at the video's last frame. A timeline with `story:` lines also
  writes `<video>.srt` and `<video>.vtt` beside the MP4 (two lines of at most 42 characters, shifted by the poster's lead-in), for players,
  search and translation. A keyframe's `chapter: N` asks the page for that story chapter's highlight in the frames.
- **"The video of the story"** (2.0.0): do not write its timeline. `story.py build map.yaml --outputs video` writes
  `video/<id>_timeline.yaml` from the story's beats (camera at each beat's view, held for the line's reading time, counters from the data);
  pass it as both `--keyframes` and `--timeline`. The `story-beats` module has the timing.
  Camera keyframes (`keyframes:`, or `--keyframes cam.yaml` alone) are either a free camera, `{t, camera: [lon, lat, height_m above the
  ground], target: [lon, lat, height_m], fov}` (pitch up to 115 degrees, looking up at a roof; the eye never goes under 6 m: MapLibre clips
  nearer ground, MAP-OUT-026), or the page's own `{t, center, zoom, pitch, bearing}`; each segment eases (`ease: smooth | linear | in | out |
  hold`). A keyframe may set `layers: [...]` and `opacity: {roof: 0.25}` (a factor of the layer's own, eased, a see-through roof).
- **"The bars grow year by year", "date in the corner", "small enough to post"** (1.19.0): build the page with `extrude: {per_unit_m: 25,
  value_field: n, moments: [c2008, ..., c2026]}` and give the timeline `grow: {layer: bars, start: 3, end: 19}`: each block rises through its
  own values, blended between moments, and a classed layer takes each block's class at that moment (1.20.0; `recolor: false` keeps the
  final colours; the moments must count what value_field counts). `date: auto` (default; or `off`, or your own text) prints the date at the right of the credits band, as
  the print footer does. A story line keeps clear of a bottom-right counter. The draft is drawn at the final's field of view. A final MP4 over
  15 MB also gets `<stem>_web.mp4` under it (`--web-mb`). `--on-ready READY.js` (once the page has settled) and `--on-frame FRAME.js` (before
  each frame, with `map` and `cam`) run your own MapLibre code; a page error (a bad layer or filter) is reported as MAP-OUT-013, not lost.
- **"Tear the map", "pieces drift away", "the empire shrinks"** (1.16.0): `map_export.py --story map.yaml --timeline T.yaml --out DIR`, the
  map video with no point layer: `features: {layer: pieces, id_field, label_field, events: [{t, ids | where, action: show | hide | fade | lift
  | drift | settle | recolor, dur, dx_mm, dy_mm, rotate, scale, colour}]}` moves a polygon layer's pieces over the map (keyed in the legend
  only); labels stay upright and one size; pieces that end on top of each other are MAP-OUT-027. With `--events --layer quakes` the same
  timeline adds its story line and counters to the event video. Cut a map into pieces with `ops: [{op: voronoi}]` or `split_by`.
- **"The load moves through the day", "the cells change colour hour by hour"** (4.1.0): `values: {layer: cells, column_prefix: u (or
  columns: [u00, ..., u23]), start, end, interpolate: cubic | linear | step, cyclic: true, sparkline: {label}}` recolours a classed polygon
  layer frame by frame through its own classes (cubic is monotone, cyclic wraps the last step to the first); the sparkline draws the mean
  of every column with the current moment marked, clear of the story line. A counter `{format: clock}` shows the hour of the day ("4:15 am").
- **"Water fills the canals", "the signal runs down the fibre"** (4.1.0): `flow: {layer: canals, from: [lon, lat] | a point layer,
  gap_m, snap_m, start, arrive_s, speed_kmh, dash, front, dry}` fills a line layer from its source along the network (network_distance's
  measure): the channel is pale until the water reaches it, dashes move downstream and each head glows. A counter `{source: flow, format:
  hm}` shows the real hours at `speed_kmh` ("+23 h 30 min"). `top: {layers: [sites]}` draws the furniture, labels and the named layers over
  the moving ones; `overflow: {px: 30}` lets them fade past the map frame (0: cut at it). `also:` takes polygon, line and point layers.
- **"A ship's crossing", "the depth under the route", "day by day at sea"** (5.5.0): a flow along one line (a route) carries a solid marker
  at its front, the strongest mark on the frame (`front: {marker: point | none | auto, size_mm: 3, colour: "@brand.status.danger", halo: true}`;
  auto: a marker on a single line, the glow on a network). `chart: {kind: profile, along: flow, raster: gebco.tif | column: depth_m, invert: true,
  y_label: "Depth (m)", x_label: "Day", x_map: {from: 0, to: 64}}` draws the value along the flow with its marker at the front on every frame (no
  row times); a counter `{source: flow, total: 64, format: day, text: "Day {v} at sea"}` runs with the same front, and the chart's x reads the same
  mapping. Both chart kinds take `w`, `h` (shares of the frame, or pixels on a 1080 frame), `value_label`, `y_axis` (two or three ticks; on for a
  profile), `invert` (bars hang from the top), `min_text_px` (20 on 1080p by default); tick labels never overprint the current one. The panel
  is kept inside the map frame, clear of the credits band, legend, locator, logo and the counters' corners, whatever `position` says.
  A story line with no `end` stays its reading time (60 ms a character, at least 2.5 s; `for_s` sets it), never until the next line; a line
  may start `at_km: 1200` or `at_fraction: 0.4` along the flow instead of at a second; MAP-STORY-028 says when a line states a measure the chart
  no longer shows. Counters sit on a panel in the page colour, so a base-map name under them leaves no ghost glyphs. `poster: preview` (the
  default for a flow video) opens on the whole route drawn faint with the title, not on the end; `poster: 12` opens on that second.
  A `values:` layer outlines only the features that have a value at that moment (`outline: false` for none); a legend-only layer never
  becomes the base map's study area, so the other layers keep their strength, and a region given as a bare `bbox` is a frame, not a study area.
- **"Keep it all on the map"** (5.7.0): a video's map is everything within its frame. The title, counters, chart, story line, sparkline,
  legend and an atlas animation's progress bar sit inside the map frame (the bar along the inside of its bottom edge); only the credits band
  and the date are below it. Anything that leaves the frame is MAP-CART-046 in the video's findings.
- **"Make the counter the first thing read"** (5.6.0): a video's tiers are counter, chart, captions, then the key: in a video of 15 s or
  more the legend is read whole for its first 6 s, then steps back to 70% of its size in its own corner (`legend: {reduce_after_s: 8, scale:
  0.6}` in the timeline; `legend: {reduce: false}` keeps it whole; the map is drawn a second time without its key for this). Every story line
  sits in one slot, the row the highest-placed line needs, so captions do not jump between heights from beat to beat. On a slide (and the
  1920 x 1080 video drawn on one) body labels are 12 pt or more under every look but classic.
- **"Check my own render", "QA this video I made"** (1.16.0): `map_export.py --external FILE.mp4 --spec map.yaml [--credits-shown "TEXT"]`
  gives a still or a video made outside the pipeline the build's paper trail: read back (MAP-OUT-013), every credit its sources need found in
  the credits it shows (MAP-OUT-028), colours outside the brand palette (MAP-OUT-029), a provenance sidecar and the hidden accreditation in
  the file's metadata, and a short list to confirm by eye (logo, title, the illustrative note).
- **"A profile of the link", "path profile", "show the Fresnel zone"**: `python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --dem dem.tif
  --a lon,lat --b lon,lat --ha 30 --hb 30 --freq 11 --out out` (or `--spec map.yaml --layer links --link SITE1-SITE2` for a link a
  `line_of_sight` op drew). A chart in the brand: the ground, the line of sight with earth curvature, the first Fresnel zone, the obstruction or
  the least clearance, both antenna heights; the source line credits the DEM and says it is bare earth (MAP-TER-006). SVG, PDF and PNG.

```bash
python3 "$GMAP_ROOT/scripts/map_export.py" --animate out/rollout-atlas --out out/animation [--sizes square,slide] [--formats gif,mp4] [--timeline chart.yaml] [--jobs auto] [--fresh] [--fade 0.2]
python3 "$GMAP_ROOT/scripts/map_export.py" --timelapse out/ferries.html --out out/video --size 1080x1350 [--quality final]
python3 "$GMAP_ROOT/scripts/map_export.py" --events map.yaml --layer quakes --time-field time --highlight "mag>=7" --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --flythrough out/venue.html --timeline flight.yaml --poster last --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --story map.yaml --timeline tear.yaml --size slide --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --external out/custom.mp4 --spec map.yaml --credits-shown "Data: AWMC (ODbL)"
python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --spec map.yaml --layer links --link PHX01-PHX04 --out out --formats svg,pdf,png
```
