---
name: geo-inspect
description: >
  Say what is in a geodata file (GeoJSON, Shapefile, KML, GeoPackage, CSV, GPX, GeoParquet, GTFS) and flag problems before mapping. Trigger
  on: what's in this file, what projection is this.
---

# geo-inspect

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/geo_inspect.py" FILE --text
python3 "$GMAP_ROOT/scripts/geo_inspect.py" FILE --layer NAME --thumbnail preview.png
python3 "$GMAP_ROOT/scripts/geo_inspect.py" FILE --crs EPSG:4326        # only with the user's answer
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
| `file-format-notes` | when a format quirk explains a finding |
| `geo-io`            | for layers, encodings, archives and size limits |
| `crs-engine`        | when the CRS is missing or the coordinates look swapped |
| `projections-guide` | to explain the suggested display projection |
| `raster-data`       | for a GeoTIFF or other raster |

## What to tell the user

Lead with the verdict (`usable`, `usable, with warnings`, or `do not map as is` and why). Then, briefly: feature count,
geometry types, CRS (say if it was assumed), extent, the fields worth knowing, and the display projection suggested with its
reason. Name any layer that was not read when the file has several.

## Rules

- Exit 3 means a BLOCK finding (no CRS, swapped latitude and longitude, features that cross the antimeridian). Explain the
  likely cause and the fix. Do not map the data until it is fixed or the user decides.
- Exit 2 means a CSV with ambiguous columns: list the candidates and ask.
- The thumbnail is neutral grey with no text. It is a check that the shapes look right, not a map. Maps get their style from
  the map skills.
- Attribute samples are the user's own values; show a few to help them recognise the fields, never more.
- Nothing in the file is fetched or followed: KML network links and URLs in attributes stay as text.
