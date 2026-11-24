---
name: regions
description: >
  KNOWLEDGE MODULE - read by map-create, geo-find-data and map-atlas. The `region:` block (3.0.0): the place a map is about, resolved once from
  ISO admin codes worldwide, named regions, admin-2 areas, a file, a box or H3 cells; cross-border regions; adding your own named regions;
  what a region decides (extent, clip, projection, names, units, the H3 complete grid, the adapters' partitions).
---

# regions

A map names its place once and everything follows from it: the frame, the projection, the admin names, the scale-bar units, the H3 grid
and the partitions every data adapter fetches by. No step asks for a state code; a US state is one admin code among all the others.

```yaml
region: {admin: US-WA}                                  # ISO 3166-2 (US-WA, DE-BY, CA-BC, AT-8) or ISO 3166-1 (KE, GB)
region: {admin: [US-WA, US-OR, US-ID, CA-BC]}           # several, across a border: one area
region: {place: "Idaho Panhandle"}                      # a named region (data/regions.yaml, or your own)
region: {admin2: {in: US-ID, names: [Bonner, Boundary]}}  # admin-2 areas inside one admin-1 area
region: {file: area.gpkg}  |  {bbox: [W, S, E, N]}  |  {h3: [872830828ffffff]}
region: {place: Cascadia, buffer_km: 10, name: "Cascadia"}
```

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/region.py" --place "Idaho Panhandle"     # what it resolves to: definition, countries, admin-1 areas, partitions
python3 "$GMAP_ROOT/scripts/region.py" --admin US-WA,CA-BC
python3 "$GMAP_ROOT/scripts/region.py" --spec map.yaml
```

Say which definition was used, in one phrase ("the ten northern counties"), whenever the region is a named one.

## What the region decides

| | |
|---|---|
| Frame | `extent: {mode: region}` is the default with a region: the frame fits the region's outline without its remote specks (3.1.0: Rockall off Scotland is drawn but not framed), and the legend and locator stay off it (2 percent at most for an area-filling map) |
| Clip | `ops: [{op: clip, layer: region}]` clips any layer to it; `region` is a layer name for ops |
| Projection | an equal-area projection centred on it for a thematic map: Albers between 30 and 65 degrees for a subject about as wide as tall or wider, Lambert azimuthal otherwise; the datum by the region (NAD83, ETRS89, GDA2020, SIRGAS 2000, WGS 84). National presets and `crs.prefer` still win |
| Names | admin-1 names from one worldwide table (the US states are its US rows); places the message names are labelled at any scale |
| Units | the scale bar in the region's own units (miles for a US or UK region, km elsewhere); a region across both (Cascadia) gets a two-unit bar, km over miles (3.1.0) |
| H3 | `h3: {complete: region}` makes every cell of the region (see `h3-grids`) |
| Adapters | each adapter fetches by the region's partitions: `us_states` (FCC files per state), `countries`, `admin1` (ISO 3166-2). 3.1.0: the regulators' coverage outside the US (bnetza-mobile-coverage, arcep-mon-reseau-mobile, accc-mobile-coverage, canada-wireless-coverage, ofcom-connected-nations), OpenCelliD, Ookla and kontur-population, each fetched for the region with a cost card first (the map server's `coverage_fetch`, or `adapters_cli.py fetch NAME --region ...`) |
| Tools | the map server's `ref_fetch`, `census_query` and `fcc_bdc_list` take `region=` (a place or admin codes) instead of a state: one call per part, the parts outside the source listed (3.1.0) |
| Series | `map-atlas` and `market-map` divide a region `by: admin1`, `admin2`, `regions` or `h3` (3.1.0) |
| Names | towns the message names come from GeoNames (cities1000, 172,000 places) when Natural Earth has none, at any scale: Sandpoint, Moscow (Idaho) (3.1.0) |

## Where the shapes come from

Countries and admin-1 areas worldwide from Natural Earth 1:10m (bundled, offline). US states and counties from the Census cartographic
boundaries: the bundled ones, or the 1:500,000 counties when `ref.boundary.us.county.cb500k` is cached (the Day 20 counts need it).
Admin-2 outside the United States comes from geoBoundaries ADM2 (CC BY 4.0), fetched once with the person's yes (MAP-REG-002 says so).
France's 13 regions answer to their ISO codes (FR-ARA, FR-BRE, FR-IDF ...) as named regions of departments (3.1.0).

## Named regions

`data/regions.yaml` ships about 45 (Cascadia, Pacific Northwest, Idaho Panhandle and its five-county variant, New England, Scotland
(`admin: GB-SCT`), Benelux, DACH, Lake Constance, Nordic countries, Baltic states, Maghreb, East African Community, Kanto, Kansai, Northeast
India, Argentine Patagonia ...). Add your own in `<work folder>/.geo-mapping/regions.yaml`, same shape; yours win on a name clash:

```yaml
- name: Our Northwest Market
  aliases: [NW market]
  members: [US-WA, US-OR, {in: US-ID, names: [Kootenai, Bonner]}]
  note: the market as sales defines it
```
