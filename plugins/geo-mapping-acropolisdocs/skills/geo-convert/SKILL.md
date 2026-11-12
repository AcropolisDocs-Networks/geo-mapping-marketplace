---
name: geo-convert
description: >
  Convert, reproject, clip, filter, dissolve, merge or split geodata between formats (Shapefile, GeoJSON, KML, GeoPackage, CSV, GeoParquet,
  PMTiles). Trigger on: convert to geojson, reproject.
---

# geo-convert

The `ogr2ogr` workhorse. It converts, then reads the result back and checks it matches (round trip). A check that blocks
removes the output and says why.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/geo_convert.py" SRC [SRC ...] -o OUT [options]
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
python3 "$GMAP_ROOT/scripts/modules.py" read file-format-notes        # any module in the table
```

| Module              | Read it |
|---------------------|---|
| `file-format-notes` | before writing a format with limits: shapefile, KML, GeoJSON, CSV |
| `geo-io`            | for driver options and the ogr2ogr equivalent |
| `crs-engine`        | when reprojecting or assigning a CRS |
| `projections-guide` | to pick a target projection |
| `basemap-render`    | when a tile set is given as input: tiles are read only as a print basemap |

| Task | Options |
|---|---|
| Convert | `-o out.gpkg` (the format comes from the extension; `--format` overrides) |
| Reproject | `--t-srs EPSG:5070`; `--crs-assume EPSG:4326` for a source with no CRS |
| Filter | `--where "pop > 1000"` (OGR SQL); `--bbox x0 y0 x1 y1` (source CRS), with `--clip` to cut geometry |
| Fields | `--select a,b`; `--rename old:new` |
| Dissolve | `--dissolve FIELD --agg pop:sum,area:mean` |
| Simplify | `--simplify TOL` (units of the output CRS; the tolerance is disclosed) |
| Merge | several sources: reprojected to the first source's CRS, attributes unioned |
| Split | `--split-by FIELD` (layers in a GeoPackage, files for other formats) |
| Repair | `--repair` makes invalid polygons valid (`make_valid`) and reports the area that changed; the fix for MAP-DATA-002 |
| CSV columns | `--lat-col`, `--lon-col` or `--wkt-col`; the delimiter (comma, semicolon, tab, pipe) is sniffed, and coordinates written as degrees-minutes-seconds (`33°25'12"N`) are read |
| Archives | a `.zip` is opened and read by what it holds (a shapefile, GeoJSON, GeoPackage or CSV inside), and a file is told by its content, not only its extension (JSON that is GeoJSON or TopoJSON) |
| Styled KML/KMZ | `-o out.kmz --kml-style style.json [--kml-legend]`: class colours, widths, point icons (inside the KMZ), balloons; see below |
| Vector tiles | `-o out.pmtiles` or `-o out.mbtiles` (or `--format pmtiles`), `--minzoom N` (default 0), `--maxzoom N` or `auto` (default: from the data) |

## Styled KMZ and tile sets

- `--kml-style` takes a style file (JSON or YAML). Keys: `mode` (`single`, `categorical` or `classed`), `field`, `colors` (one per class), `categories` or
  `edges` (left out: read from the data; `method` and `classes` for computed edges), `labels`, `color` (single), `stroke`, `width` and `size` in
  millimetres, `shape` or `shapes` (circle, square, triangle, diamond, cross), `opacity`, `label: {field}`, `balloon_fields`. Colours are brand tokens
  (`@brand.accent`, `@brand.categorical.2`, `@brand.sequential.4`); a style with no colours takes the brand's categorical or sequential palette. A
  minimal file: `{"mode": "categorical", "field": "band"}`.
- The styles are read back from the file and compared with what was written (MAP-OUT-010), as well as the usual data round trip (MAP-OUT-005). A
  `.kml` cannot hold the point icons: they go in a `<name>_files/` folder beside it, so offer the KMZ when the person will send the file on. A label
  field becomes the placemark name, and a reader gives that back as a `name` field (a WARN, not a fault).
- A tile set is written by GDAL in Web Mercator. It cannot be read back as data, so instead of the round trip it is opened again and every feature is
  counted at the top zoom (MAP-OUT-009). The report gives the zooms chosen and why, and the exact `ogr2ogr` command under `ogr2ogr.tiles`; tippecanoe,
  if the person has it, is the faster path for millions of features. A tile set over the size cap (250 MB) or over about two million tiles is refused
  with what to change. Several layers in one tile set, or the whole map styled for QGIS or Google Earth, come from `map-export` (the map's data package).
- A `.pmtiles` or `.mbtiles` file given as an input is refused: tile sets are read only as a basemap (`basemap-render`).

## Limits for large files

- `--where` on a CSV or GeoParquet source runs in DuckDB when the table is large (more than 20,000 rows) and DuckDB is installed
  (`pip install duckdb`), instead of a round trip through a scratch GeoPackage; `--engine pandas|duckdb` overrides. DuckDB runs only filters that
  it and ogr2ogr read the same way (columns compared with values of their own kind, AND, OR, NOT, IN, IS NULL, BETWEEN, LIKE with a plain ASCII
  pattern); anything else (a function, arithmetic, a date) goes through the GeoPackage as before. The report's `engine` says which ran and why.
- Other formats are filtered by GDAL while they are read, whatever the engine.
- A GeoJSON over 50 MB is slow to read and write (MAP-DATA-008): offer GeoPackage, FlatGeobuf or GeoParquet for large layers.

## Steps

1. If the input is unfamiliar, run `geo-inspect` first and fix what it blocks.
2. A CSV whose columns are ambiguous exits 2 with candidates: ask the user, then rerun with the chosen columns.
3. A missing CRS is a question. Never pass `--crs-assume` without the user's answer (lat/lon CSV columns are the one noted exception).
4. Existing outputs are never replaced without `--overwrite`.
5. Report: the output file(s), feature count, every note (CRS assumed, Z dropped, fields shortened with the mapping table,
   datum shift, simplify tolerance) and any findings. Then show the `ogr2ogr` command so the user can rerun it locally,
   with its caveats (rename, dissolve and clip need extra SQL or options).
6. Exit 3 means the result was blocked and removed. Say what blocked it; offer the fix, not a workaround that skips the check.

Shapefile output also needs the field-name mapping table in the reply. A format limit (GPX polygons, mixed geometry in a
shapefile) is explained with the alternative; the data is never silently reduced.
