---
name: network-analysis
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Network analysis: RAN analytics from
  a site list, one map and one table per analysis: site spacing and density, best-server cells by distance, footprint overlap, backhaul
  topology, neighbour relations, drive-test bins, a KPI per sector, population in footprints. Geometry only, never a propagation model.
---

# network-analysis (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run network-analysis`. Never begin it because sites or coverage were mentioned; `geo-analyze` answers a
one-off spatial question, `coverage-map` draws the footprints, and this pipeline measures the layout and draws what it measured.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pipelines.py" plan network-analysis --sites SITES.csv --option analysis=spacing,best-server --option out=./maps [--option band=n41] [--option clip=market.gpkg] [--option formats=pdf,png,svg]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  network-analysis --sites SITES.csv --option analysis=spacing,best-server --option out=./maps [same options] --confirm TOKEN
```

## What each analysis gives

| `analysis=` | Needs | Layer fields | Map |
|---|---|---|---|
| `spacing` | the site list | sites: `nearest_site`, `nearest_km`; density: H3 `count`, `cell_km2`, `sites_per_km2` | hexagons shaded by sites per hexagon, sites on top |
| `best-server` | the site list; `clip=FILE` (a polygon the cells are cut to) or `extend_km=` | cells: the site's attributes, `area_km2` | one cell per site, shaded by area |
| `overlap` | the site list (azimuth, beamwidth); `radius_m=` for the drawing radius | footprints: `overlap_count`, `overlap_area_km2`, `overlap_count_same`, `overlap_area_same_km2`, `overlap_class` | wedges by No overlap / Overlaps 1 other / 2 or more |
| `backhaul` | `links=FILE` with `a_site`, `b_site` (or from, to) and any of type, capacity | links: `length_km` and the table's columns; sites: `degree` | lines by type, sites sized by the links they carry |
| `neighbors` | `neighbors=FILE` with `source_cell`, `target_cell`, optional `ho_attempts`, `ho_success`; `cells=FILE` (cell_id, site_id) when cell ids are not `<site>-<n>` | relations: `source_site`, `target_site`, `success_rate`, `success_pct`, `length_km` | lines on a diverging ramp centred on the median success rate |
| `drive-test` | `samples=FILE` (csv with lat/lon, or geodata) and `kpi_field=` (rsrp, sinr, throughput); `agg=mean|median|p10|p90|count|min|max`, `min_count=`, `resolution=` | bins: H3 `count`, `<kpi>_<agg>` | hexagons shaded by the statistic |
| `sector-kpi` | `kpi=FILE` with a key column and the KPI; `key=sector` (default), `cell_id`, or `site_id+azimuth`; `value=` | sectors: the wedges with the KPI | wedges shaded by the KPI; a delta (around zero, or named change/delta) gets a diverging ramp centred on zero |
| `population` | `population=FILE` (polygons with a count) or a library entry id; `population_value=` (default households or population); `radius=1 mi` or `radius_km=` for circles around each site | footprints: `<value>_covered`; rings: `<value>_within_<r>` | circles (when a radius is given) or wedges shaded by the count under them |

Several analyses run in one go: `analysis=spacing,best-server,overlap`. `band=n41` keeps one band of the site list for every analysis.
The deliverable folder holds one subfolder per analysis (map files, `mapspec.yaml`, `provenance.json`, `methods.md` with the analysis
notes), `tables/` (the per-feature results as CSV, with no coordinates), `report.json` (every summary table), `analysis.md` (the
same for people), `lint_report.md` and `manifest.json`. The layer files with geometry go into `data/` only with `data_package=true`,
and a confidential site list needs `include_confidential=true` as well.

## Flow

1. **Plan.** Show the cost line: one render per analysis, nothing fetched, nothing geocoded.
2. **Confirm.** Run only with the token from that plan; a changed input or option changes the token.
3. **Stages.** probe; read and check the site list and run each analysis (layers, tables, one MapSpec each); lint each MapSpec; build each
   map with every lint; package.
4. **Report.** Give the folder, the headline number of each analysis from `report.json` (median spacing, the share of area under two or more
   footprints, the busiest hub, the relations below 90 percent, the households within the radius), the BLOCK/WARN counts, and say in one
   sentence that the figures are geometric.

## The geometric-proxy caveat (say it every time)

The plugin displays and measures geometry. Nearest-site distance is a straight line; a best-server cell is the ground nearer one site than
any other (a Voronoi cell, not a prediction); an overlap is where two drawn wedges coincide; a drive-test bin is the statistic of the
samples that happened to fall in it, and an empty hexagon was not measured, not uncovered; a population figure is what lies under a drawn
shape. No terrain, antenna pattern, power, clutter or propagation model is involved, and the notes and `analysis.md` say so. Never call
any of these outputs coverage (MAP-TEL-020 applies to the wedges). Read `telecom-geo` for the thresholds people expect next to a
drive-test map and for what a site list holds.

## Rules

- The pipeline defaults to `audience=internal`: cells, hexagons, lines and footprints are measured and drawn from the true site positions,
  so a coordinate reduction (`privacy_m`) moves the site symbols only (MAP-TEL-063 says so). For an outside reader, set `audience=external`
  with `privacy_m` and expect MAP-TEL-002 on the derived layers, or keep the map internal.
- Ids that match nothing are listed, never guessed: a link whose site is not in the list (MAP-TEL-060), a neighbour cell with no site
  (MAP-TEL-061); fix the table or give `cells=`.
- Bins with fewer than `min_count` samples are left out and counted (MAP-TEL-062); say how many.
- Population figures carry the vintage of their layer: a library entry's `vintage` (ACS 2020-2024) or `population_vintage=` for a file.
- A KPI joined per sector keeps table_join's checks: dropped leading zeros and decimal keys are repaired and reported (MAP-JOIN-005); a
  table that repeats a key blocks (MAP-JOIN-002).

## Examples

- *"Spacing of our n41 sites in Phoenix."* `--sites phoenix_sites.csv --option analysis=spacing --option band=n41`
- *"Best-server cells for these sites clipped to the market."* `--option analysis=best-server --option clip=phoenix_market.gpkg`
- *"Households within 1 mile of each venue."* `--sites venue_list.csv --option analysis=population --option population=blockgroups_hh.gpkg --option radius="1 mi"`
- *"Where do our n41 footprints overlap?"* `--option analysis=overlap --option band=n41 --option radius_m=1200`
- *"Draw the backhaul and show the hubs."* `--option analysis=backhaul --option links=backhaul.csv`
- *"Which neighbour relations hand over badly?"* `--option analysis=neighbors --option neighbors=ncr.csv`
- *"Bin the drive test by RSRP."* `--option analysis=drive-test --option samples=drive.csv --option kpi_field=rsrp --option agg=p10 --option min_count=5`
- *"Colour the sectors by PRB utilisation change."* `--option analysis=sector-kpi --option kpi=prb.csv --option key=cell_id --option value=prb_delta`
