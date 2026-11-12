---
name: coverage-map
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Coverage map: sector wedges and sites
  from a site list, colored by band, technology or site type, with coordinate privacy for confidential sites.
---

# coverage-map (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run coverage-map`. Never begin it because a map was mentioned; `map-create` is the conversational route.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pipelines.py" plan coverage-map --sites SITES.csv --option out=./maps [--option color_by=band|tech|site_type] [--option privacy_m=500] [--option privacy_method=precision|jitter] [--option radius_m=1500] [--option title="..."] [--option formats=pdf,png,svg]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  coverage-map --sites SITES.csv --option out=./maps [--option color_by=band|tech|site_type] [--option privacy_m=500] [--option privacy_method=precision|jitter] [--option radius_m=1500] [--option title="..."] [--option formats=pdf,png,svg] --confirm TOKEN
```

## Flow

1. **Plan.** Show the cost line. Nothing is looked up or geocoded; positions and values come from the person's file.
2. **Confirm.** Run only with the token from that plan. A changed input changes the token: plan again.
3. **Stages.** probe the tier, read and check the site list (columns, ranges, duplicates, hemisphere) and write the MapSpec, lint the MapSpec, render with every lint, package, and write layer data only if asked and allowed.
4. **Report.** Give the folder, the BLOCK/WARN/INFO counts and the ingest findings in plain words. A BLOCK stops the run; name the rule and the fix.

The package also holds `mapspec.yaml` (the generated MapSpec, editable and re-buildable with `map-build`), `ingest_report.json`, the cleaned data the map was drawn from, `methods.md` and `manifest.json`.

## Rules

- Read `telecom-geo` first. Wedges show direction, not coverage; never use the word coverage for them (MAP-TEL-020).
- A client site list is confidential by default. For an external audience give `privacy_m` (500 or more); without it the build warns (MAP-TEL-002). Ask the person which they want; never choose a smaller number to make the map look precise.
- `coverage=FILE` adds a polygon layer of availability. FCC National Broadband Map data is "reported availability" (MAP-TEL-003).
- True coordinates are not in the map, the methods note or the package. `data_package=true` with a confidential layer needs `include_confidential=true`, which only the person sets.
- This pipeline draws footprints and stops there. Anything measured from them, the site spacing and density, best-server cells, where the
  footprints overlap, backhaul or neighbour lines, drive-test bins, a KPI per sector, households under a footprint, is the `network-analysis`
  pipeline (`/geo-mapping-acropolisdocs:map-run network-analysis`), which takes the same site list and the same `radius_m` and `band` options.

**Sectors.** A site list with an `azimuth` column is drawn as wedges: `beamwidth` per row (or `--option beamwidth=65`), and a `radius_m` column
gives each sector its own drawing radius (50 m to 50 km; `--option radius_m` where it is empty, `--option radius_field=COL` for another column).
A wedge is display geometry, not coverage: antenna height, downtilt and power are not modelled. For a real footprint pass the propagation tool's
output polygons as `--option coverage=FILE` (drawn as the availability layer, credited as supplied); for heights on a web page, a mast layer's
`mast: {height_field}` raises each site in 3D (`webmap-create`).

