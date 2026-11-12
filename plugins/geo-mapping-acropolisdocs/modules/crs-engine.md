---
name: crs-engine
description: >
  UTILITY MODULE - read by map-create, geo-inspect and geo-convert. Detects, validates and assigns coordinate reference systems, spots
  swapped latitude/longitude, and chooses a display projection by purpose and extent with a recorded rationale.
---

# crs-engine

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/crs_engine.py" describe FILE
python3 "$GMAP_ROOT/scripts/crs_engine.py" check FILE            # exit 3 when the coordinates cannot be right
python3 "$GMAP_ROOT/scripts/crs_engine.py" choose FILE --purpose thematic [--tiles]
```

## How a display projection is chosen

| Situation | CRS | Why |
|---|---|---|
| A tile basemap is shown | EPSG:3857 | tiles exist only in Web Mercator; unsuitable for area statistics at regional scale or larger |
| World extent, values per area | EPSG:8857 Equal Earth | equal-area |
| World extent, locations only | ESRI:54030 Robinson | balanced distortion |
| Contiguous US, regional or national | EPSG:5070 NAD83 Conus Albers | equal-area, federal practice |
| US including Alaska and Hawaii | EPSG:2163 | one equal-area CRS for all |
| Alaska / Hawaii alone | EPSG:3338 / ESRI:102007 | the standard Albers for each |
| Local (under about 6 degrees wide) | UTM zone of the centre; NAD83 (EPSG:269zz) across North America | conformal, area error negligible at that size |
| Other regional extent | Lambert azimuthal equal-area centred on the extent | equal-area, low distortion near the centre |
| Polar | EPSG:3413 / EPSG:3031 | avoids the cylindrical cut |

The choice and its reason go into the MapSpec as `crs.display` and `crs.rationale` (`mapspec_crs`). A CRS the user asks for is
kept; if it is not equal-area, the rationale says so.

## Rules

- Never relabel data that already has a CRS to "fix" it. Reprojecting transforms; relabelling (`assign` with override) is a
  claim about the data and needs the user's say-so.
- `coordinate_problems` blocks on swapped latitude/longitude, degrees in a projected CRS, and metres in a degree CRS. Offer the
  likely cause; do not repair silently.
- A datum shift (NAD27, NAD83, WGS84) is reported with its operation and accuracy. When the grid-based transform is not
  installed, the stated accuracy is the fallback and the result can be off by more than that. Say so.

## Making layers line up (0.2.2)

Every layer is reprojected to the one display CRS before anything is drawn, so layers align when each one's CRS label is the CRS its
numbers were written in. The map build checks that, per layer and across layers, and says what to check when it is not:

| Check | When it fires | Severity |
|---|---|---|
| MAP-CRS-006 area of use | the layer's coordinates, read in the CRS it states, land outside the area that CRS is defined for: feet read as metres (the message says which), a State Plane or zone label from somewhere else, a wrong assumption for a file with no CRS | WARN when the layer lands away from the area or the units explain it; INFO when part of a correct layer runs past the edge |
| MAP-CRS-004 per layer | the datum transform to the display CRS is a ballpark offset (no grid on this machine) or coarser than 5 m | WARN when the possible shift measures 0.25 mm or more on the page, INFO when it cannot be seen; names the grid file and `projsync --file NAME` |
| MAP-CRS-007 registration | after reprojection, a layer touches no other layer, or most of a layer falls outside an outline layer (a state, counties, a market) that it overlaps while its box is displaced from the outline's | WARN; `crs: {alignment_check: false}` turns it off for layers that lie apart on purpose |

A label one UTM zone off lands inside the wrong zone, so MAP-CRS-006 cannot see it; MAP-CRS-007 does, by comparing the layers. Never
"fix" a finding by relabelling: ask where the file came from, then set the layer's `crs:` to the CRS it was made in.
