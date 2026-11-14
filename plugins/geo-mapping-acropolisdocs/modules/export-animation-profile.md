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
- **"A profile of the link", "path profile", "show the Fresnel zone"**: `python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --dem dem.tif
  --a lon,lat --b lon,lat --ha 30 --hb 30 --freq 11 --out out` (or `--spec map.yaml --layer links --link SITE1-SITE2` for a link a
  `line_of_sight` op drew). A chart in the brand: the ground, the line of sight with earth curvature, the first Fresnel zone, the obstruction or
  the least clearance, both antenna heights; the source line credits the DEM and says it is bare earth (MAP-TER-006). SVG, PDF and PNG.

```bash
python3 "$GMAP_ROOT/scripts/map_export.py" --animate out/rollout-atlas --out out/animation [--sizes square,slide] [--formats gif,mp4]
python3 "$GMAP_ROOT/scripts/map_export.py" --timelapse out/ferries.html --out out/video --size 1080x1350 [--quality final]
python3 "$GMAP_ROOT/scripts/map_export.py" --events map.yaml --layer quakes --time-field time --highlight "mag>=7" --out out/video
python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --spec map.yaml --layer links --link PHX01-PHX04 --out out --formats svg,pdf,png
```
