---
name: geo-io
description: >
  UTILITY MODULE - read by map-create, geo-inspect and geo-convert. Single read/write layer over GDAL/pyogrio for vector geodata: ten
  formats, archive and size guards, encoding and CRS notes, and the equivalent ogr2ogr command.
---

# geo-io

One reader and one writer for every vector format the plugin supports. Other skills call it; they never open a
geodata file any other way.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/geo_io.py" formats
python3 "$GMAP_ROOT/scripts/geo_io.py" layers FILE
python3 "$GMAP_ROOT/scripts/geo_io.py" command SRC DST --t-srs EPSG:5070 --where "pop > 1000"
```

In Python (inside this plugin's scripts): `import geo_io; r = geo_io.read(path, layer=None, crs_assume=None)` returns a
GeoDataFrame in `r.gdf` and a list of notes in `r.notes`. `geo_io.write(gdf, path)` returns the notes and the field map.

## Rules every caller follows

- **Report the notes.** Each note says something was decided for the user: a CRS assumed, Z dropped, a field name
  shortened, a layer chosen, a network link not followed. None is silent.
- **A CSV that cannot be resolved is a question, not a guess.** `AmbiguousColumns` carries the candidate columns; ask the
  user which pair to use, then pass `lat_col` and `lon_col` (or `wkt_col`).
- **A missing CRS is a question too.** Pass `crs_assume` only with the user's answer. Lat/lon columns are the one case
  taken as WGS84, and that is noted.
- **Formats that need WGS84** (GeoJSON, KML, KMZ, GPX, TopoJSON) are reprojected on write, with a note. Data without a CRS
  is refused for them.
- **One geometry type** per shapefile; GPX holds points and lines only. Split the data first or choose another format.
- **Archives** (zipped shapefile, KMZ) are unpacked in a scratch folder with limits on entries, size and compression
  ratio. A KML network link is never followed; data is never fetched.
- **GTFS** (a zip or folder holding stops.txt, routes.txt and trips.txt) is read, never written, by `gtfs.py`: pass `layer=` as
  `routes`, `shapes`, `stations`, `stops` or `entrances`, optionally `:mode` (`routes:light_rail`); with no layer, routes are read
  with a note. A `.pb` file is GTFS-Realtime vehicle positions (layer `vehicles`). Feed validity is a note (MAP-GTFS-001).
- Large GeoJSON (over 50 MB) is warned about; prefer GeoPackage or FlatGeobuf.

Format quirks are in `file-format-notes`.
