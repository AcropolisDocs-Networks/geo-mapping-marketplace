---
name: terrain-dem-and-maps
description: >
  KNOWLEDGE MODULE - read by terrain-analysis. Fetching an elevation model (3DEP in the US, Copernicus GLO-30 and the terrain tiles elsewhere; sizes, caching, credit) and putting terrain on a map: hillshade, contours and the profile inset.
---

# terrain-dem-and-maps

## Fetching the DEM

- **Inside the United States** (the lower 48, Alaska, Hawaii, Puerto Rico): USGS 3DEP (`ref.terrain.us.dem.3dep`). Public domain; the credit line "3DEP data courtesy of the U.S. Geological Survey" goes in the source note. One request, at most 4096 pixels a side: a window wider than that comes back coarser and the record says so (MAP-TER-003). 10 m over most of the lower 48, 1 m where lidar exists.
- **Elsewhere**: the AWS Terrain Tiles in Terrarium encoding (`ref.terrain.world.dem.terrarium`), about 30 m. **Attribution is required** and it is long (every source the tiles were built from). It is the default outside the US, so there is nothing to acknowledge: the fetch record carries the text and the map's source note prints it in full.
- `--source auto` (the default) picks by the window; `3dep` outside the US blocks with a message. At most 64 tiles per fetch; the zoom is lowered to fit and the record says the resolution it reached.
- **Relief, hill glyphs and 3D pages outside the US** (1.5.0): the Copernicus DEM GLO-30 (`ref.terrain.world.dem.copernicus-glo30`, keyless, 1-degree
  tiles from the AWS Open Data bucket, at most 16 a map, GLO-90 where a 30 m tile is not public). It is a **surface model**: buildings and trees are in
  it, which suits shading but not a bare-earth profile, so line of sight, profiles and viewsheds keep the Terrain Tiles above unless you name
  `--source copernicus` (and then say the ground includes clutter). Its notice ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and ©
  Airbus Defence and Space GmbH 2014-2018 ...") is printed for you.

Outside the US, a profile from a rooftop in Valparaíso to a hill site:

```bash
python3 "$GMAP_ROOT/scripts/terrain.py" fetch --bounds -71.70,-33.10,-71.55,-33.00 --res 30 --out dem.tif      # Terrarium, about 30 m
python3 "$GMAP_ROOT/scripts/terrain.py" los --dem dem.tif --a=-71.627,-33.045 --b=-71.590,-33.030 --ha 25 --hb 30 --freq 18
```
- The DEM is cached under the working folder by source, window and resolution; a second question on the same window reads nothing from the network, and the record says `cached: true`.
- **The person's own DEM** (a GeoTIFF from a propagation tool, a lidar tile): pass it as `--dem` and skip `fetch`. Any CRS works; `raster_io.open_info` says what it is. Nothing is fetched and the record says so.
- Every request is logged in the record (URL, status, bytes, time, sha256), and the record goes into the map's provenance.

## Putting terrain on a map

The map roles do all of this (map-create): `terrain: {on: true, source: auto | 3dep | terrarium | dem.tif, opacity, contours:
{on, interval}}` for relief as context, `role: hillshade` and `role: contour` layers for a DEM named in the spec, `role: raster` for an elevation
tint, and the `line_of_sight`, `viewshed` and `raster_zonal` ops. The DEM is fetched in the plan step (the only network step of a build) and cached;
a failed fetch leaves the map without relief, with a note (MAP-TER-004), and Terrarium without its terms falls back to 3DEP inside the US.

- A hillshade goes **under** everything: the ground, then the hillshade at 30 to 40 percent opacity in the brand's neutrals (never a colour ramp for relief), then context, then the data. It is a context layer: never in the legend, never in the extent, named in the source note with the DEM's credit line.
- Contours are a context line layer in a muted neutral, index lines heavier and labelled with their height; they do not fight the data's own lines (drop them on a dense route map).
- An elevation tint (`role: raster` with the brand's sequential ramp) is for a map **about** elevation; under a site or market map use the hillshade alone.
- A viewshed or an LOS line layer is data: it goes in the legend ("visible ground", "clear" and "obstructed") and the methods page carries the k-factor, the heights, the DEM and its resolution, and the bare-earth sentence.
- The source note prints the DEM's attribution text from the fetch record in full; for Terrarium that is the whole block.
