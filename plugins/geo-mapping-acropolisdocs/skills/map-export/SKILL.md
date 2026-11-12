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
| `export-print-sheets` | for a new size, or a file for a print shop, a slide or a designer |
| `export-animation-profile` | for an animated rollout or a link profile chart |
| `export-data-package` | for the map's data as KMZ, GeoPackage, QGIS, PMTiles or MBTiles |

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
