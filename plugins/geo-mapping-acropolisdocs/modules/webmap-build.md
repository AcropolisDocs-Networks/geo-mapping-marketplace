---
name: webmap-build
description: >
  UTILITY MODULE - read by webmap-create and the map-run pipelines. Builds the single-file HTML page from a checked MapSpec: plans the map,
  chooses the engine, resolves the background through the tile registry, embeds data, tiles, fonts and the map engine, lints the result.
---

# webmap-build

Two ways in. A quick page from `webmap-create`:

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats html
```

or the pipeline, for a page that goes to someone (a deliverable folder with the MapSpec, provenance, lint report, methods and a page report),
started only through `map-run` after the person agrees to its cost:

```bash
python3 "$GMAP_ROOT/scripts/pipelines.py" plan webmap-build --spec map.yaml --out maps
python3 "$GMAP_ROOT/scripts/pipelines.py" run  webmap-build --spec map.yaml --out maps --confirm <token from the plan>
```

The plan's cost line says the number of features, the page size to expect, the engine and why, the basemap, and what the page calls when it
is opened ("none" unless the person chose an online basemap); a story adds a line for its chapters' print pictures. It reads the layer files'
feature counts and fetches nothing.

## Stages

| Stage | What it does |
|---|---|
| probe | Reads the tier from `env.json`. |
| spec-lint | The MapSpec against its schema and rules (MAP-SPEC-*). |
| build | Plans the map as the print build does, chooses the engine, writes the page and its static fallback picture. All MAP-WEB lints run on the page text. |
| web-lint | Reads the written page back from disk and runs the page-only checks again (hosts, CSP, embedded tiles, glyphs, size, attribution, names, time and compare), so what is delivered is what was checked. |
| package | `<map id>/`: the page, `mapspec.yaml`, `provenance.json`, `lint_report.md`, `methods.md`, `page-report.json` (engine and why, size, features, tiles, clusters, CSP), `manifest.json`. `run.json` is in the run folder. |

## What the build does

1. Plans the map exactly as the print build does (CRS, extent, classes, colours), with the scale bar off in the static fallback, because the page draws its own.
2. Resolves each requested basemap through `tile_registry.py`. Refusals are BLOCK findings; a missing key is a WARN and the bundled background is used.
3. Chooses the engine (`webmap.engine`, default `auto`): MapLibre when the page carries more than 20,000 features, a layer goes in as tiles, the
   basemap is vector tiles, the time slider or the swipe compare is on, the page is 3D or it tells a story; Leaflet otherwise. The page report
   and the notes say which and why. 3D on a page forced to Leaflet is a WARN (MAP-WEB-025) and the page is flat.
4. Writes the page: brand-token CSS, Inter embedded, the engine inlined with its licence, the data as GeoJSON with only the fields the page
   uses (coordinates to 5 decimals). On MapLibre: a layer over 20,000 features (or 3 MB of GeoJSON) goes in as a PMTiles archive (base64,
   read in the browser from memory), large point layers with their clusters made at build time, labels as symbol layers in Inter from the
   glyph files under `assets/glyphs/`. Then the controller, a content security policy, a host manifest, the static picture, a text summary
   and the hidden accreditation.
   A 3D page (M15) also carries: the terrain, made from the DEM the print relief uses (3DEP in the United States; Terrarium elsewhere,
   its credit printed) and written as Terrarium PNG tiles in one base64 block, served to MapLibre through the page's
   own `gmdem://` protocol; a hillshade from the same tiles in place of the relief picture; the extruded layers' heights (`_h`, `_b`) on their
   features; the mast columns and the links' segments as their own GeoJSON sources; each link's profile chart (`profile_chart`, web size) as
   inline SVG for its popup. A story page carries its chapters, the column of chapters, and a print picture per chapter rendered through the
   print path (the chapter's window, layers and period).
5. Runs `web_lint.py` (MAP-WEB-001 to 026) and the brand checks. `--strict` deletes the file when any BLOCK remains.

## Limits worth knowing

- **Page size.** MAP-WEB-020 warns above 16 MB (an artifact holds no more, and a phone is slow to open it) and blocks above 60 MB. 100,000
  sites with two short fields make a page of about 6 MB.
- **Leaflet** draws every feature: above 20,000 MAP-WEB-007 warns; `engine: auto` would use MapLibre instead.
- **Clusters** are MapLibre only. A tiled point layer's clusters are made at build time, one level per zoom; a GeoJSON one uses MapLibre's own.
  A layer with a time field is never clustered.
- **Search** covers a tiled layer only up to 50,000 features; above that it covers the other layers and the place names (the notes say so).
- **Zoom** on a tile provider stops at its last zoom plus two (enlarged tiles), at most 18. With the bundled background the page zooms four
  zooms past the one that fits the data, further on MapLibre when a dense point layer needs it to separate its points. Past zoom 8 its outlines
  fade but its land and water keep their colours (1.14.0: land no longer reads as water); when the print map drew detailed ground (TIGER,
  Overture, OSM), the page carries the same land and water, simplified to about a pixel at zoom 13 (at most 3 MB), and their credits.
- **Direction of travel** (`symbol.rotate_field`) is drawn on both engines as the print map's pointer (1.14.0); a point with no heading stays a dot.
- **Place names** a data label already says (the same name, or the name and a word such as terminal or station, within about 13 km) are left out, on paper too.
- **Image export** is MapLibre's (a PNG from the map canvas); the Leaflet page saves a picture through Print.
- **Terrain** is capped at 4 MB of tiles (`webmap.3d.max_dem_mb`, up to 12): the tiles go to the zoom whose pixels match the DEM's cells
  (a 10 m 3DEP DEM: 512-pixel tiles at zoom 13, about 8 m a pixel at mid latitudes) and lower until they fit; MapLibre enlarges the last ones past it. The
  INFO line of MAP-WEB-023 says which zoom and how many metres a pixel the page got; a capped page has a WARN. Heights on the page are
  relative to the window's lowest ground, and beyond the DEM the ground eases down to it.
- **3D on a phone or a slow computer.** A pitched view with terrain draws far more than a flat one. Software WebGL (headless browsers,
  some virtual desktops) takes seconds a frame; a phone's GPU does not. The 2D button turns terrain and extrusions off.
- **A story** renders one print picture per chapter: the build takes about ten seconds more per chapter, and the page about 0.5 to 2 MB
  more per chapter (an SVG, or a PNG of it when the SVG is over 1.5 MB).
- `python3 "$GMAP_ROOT/lints/web_lint.py"` is not a separate command: the lint runs inside the build and again in the pipeline's web-lint stage.
