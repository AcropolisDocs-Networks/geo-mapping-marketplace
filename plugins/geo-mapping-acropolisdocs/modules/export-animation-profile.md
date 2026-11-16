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
- **"A narrated video", "captions along the bottom", "a year counter", "fly the camera exactly here"** (1.16.0): a timeline file
  (`--timeline T.yaml`, scripts/timeline.py) works with the flythrough and with the map videos alike. `story:` lines (start, end, text) run
  along the bottom, faded over 0.45 s; `counters:` run a number through key times ("AD {v}", "{v} fans"); `end_hold:` holds the last frame;
  `poster: last` opens the video on its final frame held `poster_hold` seconds and dissolving into the flight (X shows the first frame as the
  thumbnail and does not autoplay). Every flythrough frame carries the brand logo bottom left, above the credits.
  Camera keyframes (`keyframes:`, or `--keyframes cam.yaml` alone) are either a free camera, `{t, camera: [lon, lat, height_m above the
  ground], target: [lon, lat, height_m], fov}` (pitch up to 115 degrees, looking up at a roof; the eye never goes under 6 m: MapLibre clips
  nearer ground, MAP-OUT-026), or the page's own `{t, center, zoom, pitch, bearing}`; each segment eases (`ease: smooth | linear | in | out |
  hold`). A keyframe may set `layers: [...]` and `opacity: {roof: 0.25}` (a factor of the layer's own, eased, a see-through roof).
- **"Tear the map", "pieces drift away", "the empire shrinks"** (1.16.0): `map_export.py --story map.yaml --timeline T.yaml --out DIR`, the
  map video with no point layer: `features: {layer: pieces, id_field, label_field, events: [{t, ids | where, action: show | hide | fade | lift
  | drift | settle | recolor, dur, dx_mm, dy_mm, rotate, scale, colour}]}` moves a polygon layer's pieces over the map (keyed in the legend
  only); labels stay upright and one size; pieces that end on top of each other are MAP-OUT-027. With `--events --layer quakes` the same
  timeline adds its story line and counters to the event video. Cut a map into pieces with `ops: [{op: voronoi}]` or `split_by`.
- **"Check my own render", "QA this video I made"** (1.16.0): `map_export.py --external FILE.mp4 --spec map.yaml [--credits-shown "TEXT"]`
  gives a still or a video made outside the pipeline the build's paper trail: read back (MAP-OUT-013), every credit its sources need found in
  the credits it shows (MAP-OUT-028), colours outside the brand palette (MAP-OUT-029), a provenance sidecar and the hidden accreditation in
  the file's metadata, and a short list to confirm by eye (logo, title, the illustrative note).
- **"A profile of the link", "path profile", "show the Fresnel zone"**: `python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --dem dem.tif
  --a lon,lat --b lon,lat --ha 30 --hb 30 --freq 11 --out out` (or `--spec map.yaml --layer links --link SITE1-SITE2` for a link a
  `line_of_sight` op drew). A chart in the brand: the ground, the line of sight with earth curvature, the first Fresnel zone, the obstruction or
  the least clearance, both antenna heights; the source line credits the DEM and says it is bare earth (MAP-TER-006). SVG, PDF and PNG.

```bash
python3 "$GMAP_ROOT/scripts/map_export.py" --animate out/rollout-atlas --out out/animation [--sizes square,slide] [--formats gif,mp4]
python3 "$GMAP_ROOT/scripts/map_export.py" --timelapse out/ferries.html --out out/video --size 1080x1350 [--quality final]
python3 "$GMAP_ROOT/scripts/map_export.py" --events map.yaml --layer quakes --time-field time --highlight "mag>=7" --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --flythrough out/venue.html --timeline flight.yaml --poster last --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --story map.yaml --timeline tear.yaml --size slide --out out/video
python3 "$GMAP_ROOT/scripts/map_export.py" --external out/custom.mp4 --spec map.yaml --credits-shown "Data: AWMC (ODbL)"
python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --spec map.yaml --layer links --link PHX01-PHX04 --out out --formats svg,pdf,png
```
