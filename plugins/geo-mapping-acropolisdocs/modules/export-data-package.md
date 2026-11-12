---
name: export-data-package
description: >
  KNOWLEDGE MODULE - read by map-export. The map's data exported styled as on the map: KMZ, GeoPackage with styles, QGIS project, PMTiles and MBTiles, with what each format keeps and loses.
---

# export-data-package

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
