---
name: site-confidentiality
description: >
  UTILITY MODULE - read by map-create, geo-analyze and geocode-addresses. Reduce coordinate precision or jitter client-confidential site
  points with a seed, and join a client table to boundaries with a reported match.
---

# site-confidentiality

Wraps `site_privacy.py` and `table_join.py`. Used by coverage-map, market-map and program-map; call it directly only to prepare a layer by hand.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/site_privacy.py" apply IN.gpkg OUT.gpkg --method precision --meters 500
python3 "$GMAP_ROOT/scripts/site_privacy.py" apply IN.csv OUT.gpkg --method jitter --meters 500 --seed 7 --x lon --y lat
python3 "$GMAP_ROOT/scripts/table_join.py" inspect TABLE.xlsx [--sheet NAME]
python3 "$GMAP_ROOT/scripts/table_join.py" join BOUNDARY.gpkg TABLE.xlsx --left GEOID --right fips --out joined.gpkg
```

## In a MapSpec

```yaml
layers:
  - id: sites
    confidentiality: client-confidential
    privacy: {method: precision, meters: 500}      # or jitter, with a seed
  - id: market
    join: {table: market.csv, left: GEOID, right: key, columns: [rate], pad: 5}
```

## Rules

- **precision** moves each point to the center of a square grid cell; **jitter** moves it by a random distance up to the given meters, repeatable from the seed. Neither is anonymization. Under 100 m is not protection (MAP-TEL-001).
- The disclosure goes into the methods note. Do not hide that positions were reduced.
- An external map with a confidential point or wedge layer and no privacy treatment is a WARN (MAP-TEL-002).
- Joins compare keys as text and report: unmatched rows (MAP-JOIN-003), features with no row (MAP-JOIN-004), repairs (MAP-JOIN-005). Duplicate keys and zero matches stop the build (MAP-JOIN-002, MAP-JOIN-001).
- Never ask for, store or print credentials to reach client data; the person supplies the file.
