---
name: map-export
description: >
  Export an existing map as PDF, PNG or SVG at a new size or DPI or for a slide, or its data as KMZ, PMTiles, GeoPackage or QGIS; a rollout
  GIF or MP4. Trigger on: export as PDF, 300 dpi.
---

# map-export

Writes the formats the person asks for and checks every file after it is written (page size, fonts embedded, dimensions, DPI).

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats pdf,png [--template 16x9-slide] [--strict]
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats geopdf,geotiff                        # placed on the ground: _geo.pdf, _geo.tif
python3 "$GMAP_ROOT/scripts/map_export.py" --svg out/<stem>.svg --out out --formats pdf,png --dpi 300     # from an SVG already built
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
python3 "$GMAP_ROOT/scripts/modules.py" read map-pdf-publisher        # any module in the table
```

| Module              | Read it |
|---------------------|---|
| `map-pdf-publisher` | for sizes, DPI, PDF/A, bleed, crop marks and outlined text |
| `map-pptx-adapter`  | for slide pieces or an editable one-slide .pptx |
| `qgis-layout`       | only when the person asks for the experimental QGIS layout |
| `provenance-ledger` | when exported data needs its attribution and licence notes |
| `tile-licensing`    | when the map carries a tile basemap: whether the provider allows print and export |

## Choosing

- **PDF** for print and for sending: one page of the template's exact size, vector, Inter embedded, text selectable.
- **PNG** for slides, documents and email: the template's size at its default DPI (300 for print sheets, 200 for slides); ask for `--dpi 150` when the
  file must be small. Between 72 and 1200 dpi.
- **GeoPDF** and **GeoTIFF** when the person wants the map placed in a GIS or a field viewer. Only the map window is georeferenced (in the PDF the title, legend and other overlays are drawn on top of that window, so features under them are hidden in the viewer too; the footer stays on the sheet; the GeoTIFF is the window alone). State the accuracy the report gives (MAP-GEO-003) and never call it survey grade. A map with client-confidential layers is refused (MAP-GEO-001): offer the plain PDF or PNG, and only set `export.georeference.allow_confidential` with a reason when the owner says so. GeoTIFF needs an EPSG code; the GeoPDF takes any coordinate system.
- **SVG** for editing or for the web: self-contained, no external references.
- "For a slide" means the `16x9-slide` template, not a resized print map: rebuild with `--template 16x9-slide` so the type sizes (10 pt minimum) are right. When the person wants to edit the title or legend on the slide, or add the map to a deck, use `map-pptx-adapter` (a picture plus text boxes and legend shapes, or a slide added to a copy of their deck).
- Teams that finish in QGIS can ask for the experimental QGIS layout (`qgis-layout`: same classes, colors and text, an editable `.qgz`). It needs QGIS installed; the normal export stays the supported path.
- **HTML** (an interactive page) is `--formats html` on the same build, or the `webmap-create` skill; add it to the print set with `--formats svg,pdf,png,html`.
- A **data package** (the layer files beside the map, each license checked) comes from the `map-build` pipeline through `/geo-mapping-acropolisdocs:map-run` with
  `data_package=true`, never from this skill.

## Sheet sizes, and files for a print shop, a slide or a designer

Any of these goes on the same `build_map.py` run (or `map_export.py --svg` for an SVG already built), or under `export:` in the MapSpec.

| The person says | Do |
|---|---|
| "portrait", "make it tall", "A4", "A3", "for a European printer" | `--template letter-portrait`, `tabloid-portrait`, `arch-d-portrait`, `a4-portrait`, `a4-landscape`, `a3-portrait`, `a3-landscape` |
| "an Instagram or LinkedIn story", "9:16", "vertical video size" | `--template story` (1080 x 1920 px at its default 144 dpi) |
| "exactly 500 by 700 mm", "fit our display board" | `--template custom:500x700` (millimetres; 100 to 1,500 a side, at most 3:1), scaled from the nearest print sheet |
| "transparent background", "to drop on a slide", "no white box around it" | `--transparent` (the page outside the map frame is see-through, the frame keeps its background); `--transparent all` drops the frame's background too. PNG only |
| "with bleed", "the printer wants 3 mm bleed", "crop marks" | `--bleed 3 --crop-marks`: the PDF page grows by the bleed, the page colour runs into it, TrimBox and BleedBox are set, marks sit outside the bleed |
| "PDF/A", "for the archive", "for the records system" | `--pdfa`: PDF/A-2b, fonts embedded, the title and the hidden accreditation in the XMP |
| "for Inkscape", "so I can edit the layers", "for our designer" | `--inkscape`: each map layer and each piece of furniture (title, legend, locator, scale bar, footer) is an Inkscape layer |
| "outline the text", "the designer does not have Inter", "convert text to paths" | `--outline-text`: also `<name>_outlined_v<date>.svg`, every text drawn as Inter outlines; the words stay as each piece's name for search and screen readers |

```bash
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats svg,pdf,png --template a4-portrait --bleed 3 --crop-marks --pdfa
python3 "$GMAP_ROOT/scripts/map_export.py" --svg out/<stem>.svg --out out --formats png --transparent --inkscape --outline-text
```

- Each variant is read back after it is written: the transparent PNG's corners and frame (MAP-OUT-014), the bleed PDF's boxes and painted bleed
  (MAP-OUT-015), the PDF/A metadata, output intent and fonts (MAP-OUT-016), the Inkscape layers (MAP-OUT-017), the outlined text (MAP-OUT-018).
  Report what the INFO says. For PDF/A say plainly that the file was checked here for its metadata, output intent and fonts, not with veraPDF.
- A transparent PNG keeps the theme's text colour for the title and footer: light theme for a light slide, `--theme dark` for a dark one.
- The PNG is always the sheet's own size; the bleed and crop marks are in the PDF only.
- A portrait, A-series or custom sheet fits its footer to its notes and gives the map the room that frees; the four landscape print sheets
  and the 16:9 slide keep fixed footers.

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
- **"A profile of the link", "path profile", "show the Fresnel zone"**: `python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --dem dem.tif
  --a lon,lat --b lon,lat --ha 30 --hb 30 --freq 11 --out out` (or `--spec map.yaml --layer links --link SITE1-SITE2` for a link a
  `line_of_sight` op drew). A chart in the brand: the ground, the line of sight with earth curvature, the first Fresnel zone, the obstruction or
  the least clearance, both antenna heights; the source line credits the DEM and says it is bare earth (MAP-TER-006). SVG, PDF and PNG.

```bash
python3 "$GMAP_ROOT/scripts/map_export.py" --animate out/rollout-atlas --out out/animation [--sizes square,slide] [--formats gif,mp4]
python3 "$GMAP_ROOT/scripts/terrain.py" profile-chart --spec map.yaml --layer links --link PHX01-PHX04 --out out --formats svg,pdf,png
```

## The map's data, styled as on the map

The same `map-build` run (through `/geo-mapping-acropolisdocs:map-run`, which shows the cost and waits for the yes) writes these into the package's `data/`
folder when asked with `data_package=true` and `data_formats=kmz,gpkg,qgz,pmtiles` (or the same names in `formats`), next to the per-layer GeoPackage
and GeoJSON files. Every layer goes through the same licence and confidentiality gates (MAP-LIC-003, MAP-LIC-007); when a layer is refused, none of
these is written.

| Ask | `data_formats` | What the person gets |
|---|---|---|
| "open it in Google Earth", "send me a KMZ" | `kmz` | `<map id>.kmz`: one folder per layer, the map's class colours, widths and point symbols (icons inside the KMZ), a balloon of the fields, the legend in the corner |
| "PMTiles for the tile server", "vector tiles", "MBTiles" | `pmtiles` or `mbtiles` | `<map id>.pmtiles`: every layer as a source layer of its own, in Web Mercator, top zoom chosen from the data (`maxzoom=N` and `minzoom=N` set them); attribution and the plugin version in the metadata |
| "a GeoPackage that opens styled in QGIS" | `gpkg` | `<map id>.gpkg`: every layer as a table, its style (QML and SLD) in `layer_styles`, so QGIS draws it as the map does on open |
| "open it in QGIS", "a QGIS project" | `qgz` | `<map id>.qgz` beside that GeoPackage: the layers in the map's draw order, styled, in the map's CRS and extent; the project title is the map's |

- Each file is read back after it is written (MAP-OUT-009 for the tile set: every layer present, every feature at its top zoom; MAP-OUT-010 for the KMZ,
  the GeoPackage styles and the project). Report the files and those findings, not just that they were written.
- These are the map's data, not the map: the title, legend layout, scale bar and footer stay in the PDF, PNG and SVG. The KMZ's legend is a picture;
  QGIS builds its own legend from the styles.
- A raster layer (`role: raster`) ships its window as the map read it, `data/<layer>.tif` in the map's CRS, when its licence allows (MAP-LIC-003);
  the QGIS project draws it with the map's ramp or classes. A bivariate layer opens in QGIS with nine rules in the map's nine colours, and graduated
  line widths as a graduated renderer on width. Relief, contours and a service picture are not data and stay out (MAP-RUN-003 says so).
- A large tile set is refused with what to change (raise the minimum zoom, lower the maximum zoom, clip the data). GDAL writes the tiles; tippecanoe,
  if the person has it, is the faster path for millions of features and can be run by hand on the same GeoPackage.
- One layer rather than a map: `geo-convert` writes a styled KMZ (`--kml-style`) or a tile set (`-o out.pmtiles`).
- The experimental QGIS **layout** (`qgis-layout`, the whole sheet laid out in QGIS) is a different thing from this project, which holds the layers only.

## Rules

- Never claim a file was written without the build result listing it. Exit 3 means a check blocked: report which one, fix the cause, rebuild.
- With `--strict` a blocked build leaves no files; without it the files stay and the BLOCK is reported next to them.
- The person gets the files by `SendUserFile`; name them in plain words (the PDF, the PNG), not by path.
- If `pdftoppm` is missing the PNG falls back to headless Chromium; if neither exists, `/geo-mapping-acropolisdocs:map-doctor` names the fix.

## The terms register (1.3.0)

Every deliverable folder gets `<stem>.TERMS.md` and `<stem>.terms.json`: one row per source with its licence, how it is used (drawn,
embedded data, derived, tiles, API) and each obligation's status (met, not met, person to confirm). A required credit or API notice the output
does not show is MAP-LIC-017 (BLOCK); a web page or data export that carries ODbL or CC BY-SA features gets `LICENSE-SHARE-ALIKE.txt` and
MAP-LIC-016 (WARN). Mention the register when a row is "not met" or "person to confirm". It is a record, not legal advice.
