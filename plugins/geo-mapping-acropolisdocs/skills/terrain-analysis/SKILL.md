---
name: terrain-analysis
description: >
  Answer terrain questions from an elevation model: profiles, line of sight with Fresnel clearance, viewsheds, hillshade or contours.
  Trigger on: elevation profile, line of sight, viewshed.
---

# terrain-analysis

A DEM is a grid of ground heights. This skill fetches one for the window (or reads the one the person has), then answers the question
with `scripts/terrain.py`, and gives the file a map needs (a hillshade GeoTIFF or PNG, contour lines, a viewshed polygon, a profile table).
Every answer states what a DEM can and cannot say; read **Caveats** before replying.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/terrain.py" fetch     --bounds -112.2,33.3,-111.9,33.6 --res 30 --out dem.tif                 # 3DEP inside the US
python3 "$GMAP_ROOT/scripts/terrain.py" fetch     --bounds -100.4,25.6,-100.2,25.75 --res 30 --out dem.tif                        # Terrarium elsewhere
python3 "$GMAP_ROOT/scripts/terrain.py" profile   --dem dem.tif --a -112.07,33.45 --b -111.95,33.52 --csv profile.csv
python3 "$GMAP_ROOT/scripts/terrain.py" los       --dem dem.tif --a -112.07,33.45 --b -111.95,33.52 --ha 30 --hb 30 --freq 11
python3 "$GMAP_ROOT/scripts/terrain.py" viewshed  --dem dem.tif --point -112.07,33.45 --h 30 --radius 10000 --out visible.geojson
python3 "$GMAP_ROOT/scripts/terrain.py" hillshade --dem dem.tif --out hillshade.tif        # or .png; --multi for the softer four-light blend
python3 "$GMAP_ROOT/scripts/terrain.py" contours  --dem dem.tif --interval 50 --out contours.gpkg
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
python3 "$GMAP_ROOT/scripts/modules.py" read raster-data        # any module in the table
```

| Module        | Read it |
|---------------|---|
| `raster-data` | for DEM sources, nodata, resampling and their terms |
| `telecom-geo` | for microwave links, antenna heights and line-of-sight wording |
| `geo-cache`   | when DEMs pile up on disk |
| `terrain-dem-and-maps` | when no DEM file is given, or the result goes on a map |

Exit 0 done; 2 a usage error; 4 a refusal (3DEP asked for outside the US); 5 the elevation service failed.
Coordinates are longitude then latitude. Every command prints a JSON report; `los` prints its verdict first.

## The requests and what to run

**A fresh cloud workspace has no map packages** (`python3 -c "import geopandas"` fails): run `python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick`
once, before anything else. It installs what a map needs in one pip step, wheels first, within 10 minutes (1.2.0); never `pip install -r
requirements-dev.txt`, which brings the test stack. On the person's own computer, ask before installing.

| The person says | Do |
|---|---|
| "profile from site A to site B", "what does the terrain look like between these two" | `fetch` a DEM for a box around both sites (pad 10 percent), then `profile`. Reply with the length, start and end heights, the high point and where it is, and the gain; give the CSV. A chart of distance against height is the natural picture (dataviz rules, the brand's line colour). |
| "is there line of sight between these two sites at 30 m", "will an 11 GHz link clear", "microwave path" | `los` with `--ha`/`--hb` (metres above ground; 30 m when not said, and say so) and `--freq` in GHz when the person names a band. Reply with the verdict line first, then the worst point (distance and how far the ground intrudes), the antenna height at A that clears, and the Fresnel figure. Always add the bare-earth caveat. |
| "which of these links are clear" (a site list and link pairs) | `terrain.los_matrix(dem, sites_gdf, links, heights)` in Python, or `los` per link; the result is a line layer with `clear`, `obstruction_m`, `fresnel_ok` that map-create draws (clear and obstructed as two classes). |
| "what can this site see", "coverage of this hilltop", "viewshed" | `viewshed` with the antenna height and a radius (10 km when not said). The polygon goes under the sites on a map as a filled layer; say it is approximate ground visibility, not RF coverage. |
| "hillshade under this market map", "shaded relief", "terrain background" | Not a separate file: put `terrain: {on: true}` in the map's MapSpec (or `map-restyle --terrain on`). The build fetches the DEM for the map's window at the paper's resolution (3DEP in the US, cached), shades it and draws it in two brand neutrals at 35 percent above the land and under every layer, with the credit in the source note. A DEM the person has: `terrain: {source: dem.tif}` or a `role: hillshade` layer with `dem: dem.tif`; nothing is fetched. `terrain.py hillshade` is for a GeoTIFF or PNG outside a map. |
| "contours every 50 m", "elevation lines" | On a map: `terrain: {on: true, contours: {on: true, interval: 50}}`, or a `role: contour` layer with `dem:` and `contours: {interval: 50}`; every fifth line is heavier and labelled along the line. As a file: `contours --interval 50`. Pick the interval from the relief: 10 m for a flat metro, 50 m for hills, 100 m or more for mountains (`interval: auto` aims at about 12 levels); more than about 40 lines across a sheet is noise. |
| "line of sight between these sites" on a map | The `line_of_sight` op on the sites layer, drawn with `role: lines`: `ops: [{op: line_of_sight, heights: {a_m: 30, b_m: 30}, freq_ghz: 11}]` (every pair; `to: <layer>` and `match: {a_field, b_field}` for named links). The links are drawn clear or obstructed in two brand colours with a legend, and the methods page carries k, the heights, the DEM and the bare-earth sentence. `viewshed {h_m, radius_m}` on a point layer drawn with `role: polygons` is the visible ground. |
| "elevation of this site", "how high is the tower base" | `profile` with A and B a few metres apart, or `raster_io.sample(dem, [(lon, lat)])`; give the figure in metres and feet and the DEM's resolution. |
| "population under this footprint from a raster", "people within 2 km using GHSL" | The `raster_zonal` op on the footprint layer: `{op: raster_zonal, raster: ref.statistic.world.population.ghsl, stat: sum, field: people}` (the GHSL tile, or the WorldPop country file with `.worldpop`, is fetched for the window, cached under the working folder and credited, CC BY 4.0), or `raster: pop.tif` for the person's own file. The provenance carries the pixel area, the nodata count and the partial-pixel rule. For the US with census geography at hand, geo-analyze `population_covered` on ACS is the better figure. |

## Caveats, said every time they matter

1. **Bare earth.** 3DEP and the Terrain Tiles are ground height. Buildings, trees, towers and other clutter are not in them, so a clear line of sight is optimistic: it says the ground does not block the path, not that nothing does. Say so in every LOS and viewshed reply. A 1 m lidar DEM is still bare earth.
2. **Resolution.** A 10 m or 30 m cell averages the ground across it; a ridge narrower than a cell, a cut or an embankment can be missed. The report states `dem_resolution_m`; repeat it.
3. **Earth curvature and k.** The path is corrected for the bulge of an effective earth of radius k x 6,371 km. k = 4/3 (the default) is the standard atmosphere's refraction, the usual microwave planning value; k = 1 is the geometric earth; k = 2/3 is a pessimistic sub-refractive case. A 30 km path bulges 13 m at k = 4/3. Say which k was used.
4. **Fresnel.** With a frequency, the first Fresnel zone radius at the worst point is given (17.32 x sqrt(d1 x d2 / (f x D)) metres, distances in km, f in GHz, largest at mid-path) and the link is "Fresnel ok" when at least 60 percent of F1 is clear everywhere. Without a frequency only the geometric line is judged. Neither is a link budget: no rain, no multipath, no antenna pattern.
5. **Viewshed is approximate.** Rays every degree on the DEM's own cells, ground-to-ground; between rays at the edge of a 10 km radius lies a 175 m strip that is filled in. It is visibility, not coverage.
6. **Heights are above ground, in metres.** A tower height from a site list is usually feet; convert and say so. Heights above sea level come out of the report as `elevation_a_m` plus the antenna.
7. **Not engineering-grade.** A planning answer for a map or a conversation; the propagation tool with its own terrain and clutter model signs off a design. Put that sentence in the methods note of any map that carries an LOS result.
