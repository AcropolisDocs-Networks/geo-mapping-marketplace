---
name: geo-analyze
description: >
  Answer a spatial question in one geoprocessing step: buffers, overlays, joins, nearest, best-server Voronoi, hex grids, population
  covered. Trigger on: buffer these sites, hexbin.
---

# geo-analyze

One question, one op, one new file. The op runs on a layer (and, where it needs one, a second layer), writes the result in the
format the output extension names, reads it back to check it, and prints the sentences the methods page will carry, the findings,
a provenance record and the GeoPandas and PostGIS expression that does the same thing.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/geo_analyze.py" ops                                     # the ops and their parameters
python3 "$GMAP_ROOT/scripts/geo_analyze.py" buffer --in sites.csv --distance 2 --units km --dissolve --out footprint.gpkg --report footprint.json
python3 "$GMAP_ROOT/scripts/geo_analyze.py" population_covered --in footprint.gpkg --other tracts.gpkg --value households --out covered.gpkg
python3 "$GMAP_ROOT/scripts/geo_analyze.py" nearest --in venues.gpkg --other sites.gpkg --units mi --id-field site_id --out venues_nearest.gpkg
python3 "$GMAP_ROOT/scripts/geo_analyze.py" hexbin --in drive_test.csv --resolution 8 --value rsrp --agg mean --out bins.gpkg
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
python3 "$GMAP_ROOT/scripts/modules.py" read projections-guide        # any module in the table
```

| Module                 | Read it |
|------------------------|---|
| `projections-guide`    | for the measuring CRS: geodesic against projected distances and areas |
| `telecom-geo`          | for sites, sectors and what the layout analytics measure |
| `site-confidentiality` | before a buffer or Voronoi around confidential sites |

Exit 0 done; 2 a usage error (an op parameter, an ambiguous CSV); 3 a check blocked and the output was removed (`--no-strict` keeps it).
The same ops run inside `geo-convert` (`--repair`, `--buffer`, `--clip-layer`, `--area`, `--op JSON`) and as an `ops:` list on a
MapSpec layer, so a map can carry its own analysis.

## The ops

| Question | Op and arguments |
|---|---|
| Area around sites, or distance bands | `buffer --distance 2 --units km [--dissolve]`; rings with `--rings 1,2,5 --units mi` (field `ring`, 1 is innermost) |
| A point for each area | `centroid`, or `centroid --inside` for a point that stays inside the shape |
| Cut a layer to an area | `clip --other area.gpkg` (by the polygons, not their box) |
| Where two layers overlap, or what is left | `intersect`, `union`, `difference`, `symmetric_difference` with `--other`; the other layer's fields get a prefix |
| Which features touch, fall inside or are nearest | `spatial_join --other B --predicate intersects|within|contains|nearest [--how inner] [--max-distance 5 --units mi]` |
| How far is the nearest site from each feature | `nearest --other sites.gpkg [--k 3] [--max-distance 10 --units km] [--id-field site_id]` → `nearest_id`, `nearest_distance_<units>` |
| Merge areas | `dissolve --by market --agg pop:sum,sites:count`; no `--by` merges everything |
| One feature per part; drop repeats; fix geometry | `explode`; `dedupe --fields a,b` or `dedupe --geometry`; `repair` (the fix for MAP-DATA-002) |
| Fewer vertices | `simplify --tolerance 50 --units m` (disclosed on the map) |
| Shapes drawn across 180 degrees | `antimeridian_split` (the fix for MAP-DATA-009) |
| An outline around points | `convex_hull [--by market]`; `concave_hull --ratio 0.3` for a tighter one |
| Best-server cells | `voronoi [--other market.gpkg]` (cut to the other layer) or `--bbox x0 y0 x1 y1`; one cell per site |
| Size of each polygon, length of each line | `area --field area_km2 --units km2|mi2|m2|acres|ha`; `length --field len_mi --units mi` |
| How many points in each polygon; their total | `count_in --other sites.gpkg [--field sites]`; `sum_in --other sites.gpkg --value tenants` |
| Population or households inside each footprint | `population_covered --other tracts.gpkg --value households [--mode centroid] [--field hh]` |
| Share of a market's population in each footprint | `coverage_share --other tracts.gpkg --value population` (0 to 1) |
| Where footprints or sectors pile up | `overlap` (within the layer) or `overlap --other other_footprints.gpkg` → `overlap_count`, `overlap_area_km2` |
| Density on a grid | `grid --size 500 --units m [--agg mean --value rsrp]`; hexagons with `grid --shape hex --size 500 --units m` |
| Density in H3 hexagons | `hexbin [--resolution 8 or auto] [--value rsrp --agg mean|sum|median|max] [--min-count 3]` |

## Rules

1. **Units are always stated.** Distances take m, km, mi or ft; areas km2, mi2, m2, acres or ha. A number with no unit is metres.
   Repeat the unit in the reply, and name the field the result went into.
2. **Nothing is measured in degrees.** Every distance, buffer, grid and overlap is computed in a projected CRS picked for the data
   (UTM for a local extent, Conus Albers for the lower 48, an equal-area projection elsewhere); the result comes back in the layer's own CRS.
   Area and length fields are geodesic on the WGS84 ellipsoid, so they do not depend on that pick. The provenance names the CRS used; say it
   in one phrase when the person asks how a number was made.
3. **The result is a layer file plus a report.** Hand the output to `map-create` (`source: {path: ...}`) and copy the printed notes into the
   layer's `provenance.notes`, or put the same op in the MapSpec as `ops:` so the map carries it. The report JSON holds notes, findings, the
   provenance record and the equivalent expressions.
4. **Population figures carry their vintage.** `population_covered` and `coverage_share` apportion a count by area (uniform density inside
   each zone; say so). When the zone layer came from the library (ACS 5-year population, households), read the entry's `vintage` and state it
   with the number ("about 42,000 households, ACS 2020-2024"). A rate (a percent, a density) cannot be apportioned; MAP-OPS-004 says so; use the
   count it was made from.
5. **Privacy.** No op geocodes anyone and none is run on a list of individuals. Site lists marked client-confidential keep their treatment
   (`site-confidentiality`); a buffer or Voronoi around a confidential site reveals its position, so apply the coordinate reduction first or keep
   the map internal.
6. **A Voronoi cell is geometry, not coverage.** Say "best-server by distance" and never "coverage"; no propagation, terrain or antenna pattern is
   in it (MAP-TEL-020 applies to the map).
7. **Findings.** MAP-OPS-002: a buffer wider than 500 km is approximate; MAP-OPS-003: a join matched nothing; MAP-OPS-005: slivers were reduced;
   MAP-OPS-006: more than 50,000 cells; MAP-OPS-007: a nearest search with no bound on a large layer. Report each in one line with the fix.

## Telecom examples

- *Households in a sector footprint.* the coverage-map pipeline (`/geo-mapping-acropolisdocs:map-run coverage-map`) writes the wedges; then `population_covered --in wedges.gpkg --other blockgroups.gpkg --value households`.
  Overlapping wedges each count the households under them; `dissolve` the wedges first for the site total.
- *Sites within 1 mile of each venue.* `spatial_join --in venues.gpkg --other sites.gpkg --predicate nearest --max-distance 1 --units mi --how inner`,
  or `buffer --in venues.gpkg --distance 1 --units mi` then `count_in --other sites.gpkg`.
- *Overlap of n41 footprints.* `overlap --in n41_footprints.gpkg --units km2`; map `overlap_count` as a choropleth of the footprints.
- *Drive-test bins.* `hexbin --in drive_test.csv --resolution 8 --value rsrp --agg mean --min-count 5`, then `map-create` with the cells as a choropleth
  of `rsrp_mean`. Resolution 8 is about 0.7 km2 per cell; 9 for a venue cluster.
- *Best-server cells for a market.* `voronoi --in sites.gpkg --other market.gpkg`, labelled "best server by distance".
