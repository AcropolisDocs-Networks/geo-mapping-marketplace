---
name: raster-data
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle, geo-inspect and terrain-analysis. What rasters this plugin reads (GeoTIFF, COG, a WMS
  or ArcGIS picture), how nodata, overviews and resampling are handled, how a raster is coloured and zoned, and the terrain and population
  sources with their terms.
---

# raster-data

Read this before drawing a raster, zoning one, or fetching terrain. The engine is `scripts/raster_io.py` (reading, colouring, statistics,
zonal, hillshade, contours, service pictures), `scripts/terrain_fetch.py` (a DEM for a window) and `scripts/terrain.py` (profile, line of
sight, viewshed, the CLI). All three need `rasterio` (SOFT: `/geo-mapping-acropolisdocs:map-doctor` reports it; `pip install rasterio`); contours need `contourpy`,
which comes with matplotlib. Without rasterio every call fails with one message naming the package, and the rest of the plugin is unaffected.

## What is read

| Input | How |
|---|---|
| **GeoTIFF** (a DEM, a propagation grid, an RSRP surface, a population grid) | `raster_io.open_info(path)`: driver, CRS, bounds, cell size, bands and their types, nodata, overviews, whether it is a COG and why not. Any CRS; a file with none is refused with the sentence that says how to set it. |
| **Cloud Optimized GeoTIFF** | The same reader; tiled with overviews, so a window of a 10 GB file reads only the tiles and the overview level it needs. `is_cog` and `cog_reason` say whether a file is one ("not tiled", "no overviews"). |
| **A WMS picture** | `raster_io.read_wms_image(url, layers, bounds, crs, (w, h))`: one GetMap through the plugin's guarded client (public hosts, GET, caps, the plugin's User-Agent; axis order handled for WMS 1.3.0), capped at 1024 px a side, returned as RGBA bands on the requested box. The day-02 case: the forest layer drawn as a picture under the data. |
| **An ArcGIS MapServer export** | `raster_io.read_arcgis_export(url, bounds, sr, cell=30)`: the picture is placed on the extent the server says it drew, never the one asked for (ArcGIS widens a box to the image's shape). |
| **Terrarium tiles, 3DEP exportImage** | Through `terrain_fetch.dem_for`, below; both end as a local float32 GeoTIFF in metres with nodata -9999. |

Not read: NetCDF, HDF, JPEG2000 and MrSID (GDAL may open them; nothing here is tested on them), and any raster without a CRS.

## Windows, overviews, resampling

- `read_window(path, bounds, crs, out_shape=None, max_px=4096, band=1, resampling="bilinear")` returns a `Grid`: a masked array, its transform and CRS. The window is warped to the CRS asked for (a WarpedVRT), so a DEM in degrees reads straight into the map's UTM.
- The pixel count is the source's own at the window unless `out_shape` says otherwise; the longer side is capped at `max_px`, and then GDAL reads from the overview nearest that scale. A 10 m DEM under a state map is never read in full.
- Resampling: `bilinear` for continuous fields (elevation, signal, population density), `nearest` for classes and codes (land cover, a coverage category), `average` when shrinking a continuous field for statistics, `mode` when shrinking classes. `cubic` for a smooth-looking hillshade input. Say which was used when a figure comes from a resampled window (the `Grid.source` record carries it).
- Nodata is masked everywhere: the file's nodata value, nan in a float raster, and the area outside the file's extent (an alpha band when the file declares no nodata). A statistic never includes it; a PNG makes it transparent; a zonal count reports it.

## Colouring

`to_png(array, ramp, vmin, vmax, nodata_alpha=0, classes=None)` gives PNG bytes and the legend entries. **The ramp is passed in**: the
brand's sequential ramp for a magnitude (elevation tint, population density), the diverging ramp for a difference around a midpoint
(a delta grid), two neutrals for a hillshade. Nothing in the engine knows a colour. Continuous: vmin to vmax (the 2nd and 98th percentile
when not given, so one outlier cannot flatten the picture) with five legend stops. Classed: ascending break values, one legend row per class;
the classify-and-color rules apply (round breaks, at most seven classes, the legend states the unit).

## Statistics and zonal

- `stats(path, bounds, crs)` samples at most a million pixels (from overviews when the raster is larger) and says `sampled: true` when it did; min, max, mean, std, the percentiles, the valid and nodata counts.
- `zonal(path, polygons, stat)` writes one field per polygon: `sum`, `mean`, `min`, `max`, `count`, `majority`, `median`, `std`, with `<field>_cells` and `<field>_nodata` beside it. The polygons are projected to the raster's CRS and rasterized onto its own grid: **a cell counts in full when its centre is inside the polygon**; `all_touched=True` counts every cell the polygon touches. There is no area weighting, so a polygon smaller than a cell can count zero cells or one whole cell. The provenance record says the pixel area (in m2, converted for degree and Web Mercator grids), the nodata handling and this policy, and the note for the methods page repeats it.
- A population raster summed under a footprint is "people in the cells whose centres fall inside"; for a footprint a few cells across say that the figure is coarse. The grid's year and model (GHSL 2020, WorldPop 2020) is part of the figure's name.

## Terrain derivatives

- `hillshade(dem, transform, azimuth=315, altitude=45, z_factor=1, multidirectional=False, crs=...)`: Horn's method, 0 to 1. Pass `crs` so a DEM in degrees or Web Mercator has its cells converted to metres (otherwise a degree grid shades like a cliff). `multidirectional` blends four lights (the GDAL weights) for a softer background.
- `slope` (degrees or percent) and `aspect` (degrees clockwise from north, nan where flat).
- `contours(dem, transform, crs, interval=50)`: contourpy lines with `elevation`, `index` (every fifth level from zero, drawn heavier) and `length`; they stop at nodata. Over 2,000 levels is refused: pick a larger interval.

## The terrain and population sources

| Entry | What | Terms | Cap |
|---|---|---|---|
| `ref.terrain.us.dem.3dep` | USGS 3DEP ImageServer, bare earth, 10 m (1 m with lidar), the US only | Public domain; credit "3DEP data courtesy of the U.S. Geological Survey" | one exportImage up to 4096 x 4096 px; a wider window comes back coarser (MAP-TER-003) |
| `ref.terrain.world.dem.terrarium` | AWS Terrain Tiles, Terrarium PNG, world, about 30 m | **Attribution required** to every source in the tilezen attribution document; the block is in the record and printed with the map, so there is nothing to acknowledge (0.23.0) | 64 tiles per fetch, 4 at a time, zoom lowered to fit; raw tiles cached |
| `ref.statistic.world.population.ghsl` | GHS-POP R2023A, 2020, 100 m, Mollweide, 10-degree tiles from the JRC | CC BY 4.0 (JRC reuse notice); cite the DOI | a tile is 17 MB zipped; fetched per window, never bundled |
| `ref.statistic.world.population.worldpop` | WorldPop constrained 2020, 100 m, one GeoTIFF per country in degrees | CC BY 4.0; cite the country dataset | fetched per country, never bundled; a file over 200 MB (the US one is 494 MB) is not fetched whole: the map's window is read from it by byte range (1.11.0) |

`terrain_fetch.dem_for(bounds_wgs84, target_m_per_px, source="auto", cache_dir, dem_path=None)` picks 3DEP inside
the US and Terrarium elsewhere (MAP-TER-001 says which and why), caches by source, window and resolution (MAP-TER-002 on a hit), and
returns `{ok, findings, path, record}`. The record is provenance: source, entry, CRS, resolution reached, tiles or pixels, bytes, sha256,
attribution text, `retrieved_at`, the User-Agent, and every request (redacted URL, status, bytes, time, sha256).
`public_record(rec)` is the same with the request list reduced to counts, for a map's provenance file. A `dem_path` the person supplies is
recorded as `source: user` and nothing is fetched.

## Rules

1. Rasters are never bundled with the plugin; the library entries say where to fetch and the record says what was fetched.
2. A raster's credit line goes in the source note like any layer's; the Terrarium block is printed whole.
3. A map draws a raster through the brand (a ramp or two neutrals); the engine returns numbers and a PNG coloured with what it was given.
4. Nothing is measured in degrees: a zonal pixel area on a degree grid is converted at the raster's centre latitude and the record says so.
5. A DEM is bare earth and a cell is an average; every line-of-sight or viewshed statement carries that sentence (terrain-analysis, Caveats).

## On the map (scripts/map_raster.py)

The MapSpec draws all of this without a script: `role: raster` (a file, a library raster read for the window, or a service picture with
`source: {type: service, mode: image}`), `role: hillshade` and `role: contour` (a DEM file or a 3DEP or Terrarium ref), the `terrain:` block
(relief, and contours, as context), and the `raster_zonal`, `line_of_sight` and `viewshed` ops. Every window is read in the plan step for the
map's own frame at the export's pixel size (at most 4,096 px), so the build is offline after the first fetch; `GMAP_OFFLINE=1` fetches nothing.
The population rasters come through `scripts/raster_fetch.py` (GHSL tiles, WorldPop country files; cached, at most 200 MB a fetch: a larger country file gives its window by byte range).
