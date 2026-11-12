---
name: file-format-notes
description: >
  KNOWLEDGE MODULE - read by geo-inspect and geo-convert. Geodata format quirks: shapefile limits, KML and KMZ, GeoJSON RFC 7946,
  GeoPackage, TopoJSON, GeoParquet, FlatGeobuf, GPX and CSV.
---

# file-format-notes

Read this when a conversion loses something, a file will not open elsewhere, or a user asks which format to use.

| Format | What to know |
|---|---|
| **Shapefile** | A set of files (.shp .shx .dbf .prj and .cpg), delivered as one .zip. Field names are cut to 10 characters. One geometry type per file. Each component is limited to 2 GB. Text encoding comes from the .cpg file; without it, accents may be wrong. No true null for numbers or dates. |
| **GeoJSON** | RFC 7946: WGS84 only (no `crs` member), exterior rings counter-clockwise and holes clockwise, no NaN. Shapes that cross the antimeridian must be cut at 180. Coordinates are text, so large files get slow; over 50 MB prefer GeoPackage or FlatGeobuf. |
| **KML** | Google Earth's format: WGS84, longitude then latitude. Styles are not carried through conversion; extended data becomes attributes. Network links are never followed here. Altitude values are dropped for mapping. |
| **KMZ** | A zip holding `doc.kml` and any icons. The same rules as KML. |
| **GeoPackage** | The preferred interchange: one SQLite file, many layers, any CRS, real types and nulls. Use it when a choice is open. |
| **CSV** | No CRS and no geometry type. Latitude and longitude columns are taken as WGS84; a WKT column needs its CRS stated (a `.prj` beside the file records it). Leading zeros (FIPS codes) survive only when columns are read as text. |
| **GPX** | Waypoints (points), routes and tracks (lines). WGS84. A fixed attribute set; other attributes travel as `ogr:` extensions that other GPS software may ignore. No polygons. |
| **TopoJSON** | Shared borders stored once; coordinates are quantized (here to 100,000 steps), so positions move by a tiny fraction of the extent. GDAL reads it but cannot write it; this plugin writes it with the `topojson` package. No CRS recorded: WGS84 is assumed. |
| **FlatGeobuf** | Fast, streamable, typed. Large files carry a spatial index that reorders features along a Hilbert curve. |
| **GeoParquet** | Columnar and compact; keeps the CRS in its metadata. The GDAL wheel has no Parquet driver, so geopandas and pyarrow do the work. |
| **GTFS** | A transit schedule: a zip of CSV tables (agency, routes, trips, stops, stop_times, shapes, feed_info). Read only. Routes become lines from `shapes.txt` (stop to stop when a route has none, with a warning); stations roll up their platforms and the routes calling there (from `stop_times`). Coordinates are WGS84. A feed has service dates: one that has ended shows a past network. Route colours are the agency's; maps use the brand palette unless asked. |
| **GTFS-Realtime** | A protobuf snapshot (`.pb`) of vehicle positions, trip updates or alerts; vehicle positions are read as points. It holds route and trip ids only, so join it to the static feed for names. Positions are seconds old when fetched and only a snapshot once saved. Many agencies need an API key for it. |
| **PMTiles, MBTiles** | Tile containers for basemaps, not interchange formats. |
| **GeoTIFF, GeoPDF** | Written from a map: `build_map.py --formats geopdf,geotiff` (files `_geo.pdf`, `_geo.tif`). Only the map window is georeferenced, exact at its edges and about one map pixel inside it. Refused for client-confidential layers (MAP-GEO-001) and over tile basemaps (MAP-GEO-002); GeoTIFF needs an EPSG-coded coordinate system. Not a survey product. |

## Choosing a format for a user

- To share data with another GIS user: GeoPackage.
- For a web map: GeoJSON if small, FlatGeobuf or PMTiles if large.
- For Google Earth: KML or KMZ.
- For a legacy system that asks for shapefiles: a zipped shapefile, with the field-name mapping table beside it.
- For a spreadsheet: CSV, with the CRS stated in the message.
