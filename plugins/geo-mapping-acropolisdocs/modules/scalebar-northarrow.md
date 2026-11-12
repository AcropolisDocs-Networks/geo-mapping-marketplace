---
name: scalebar-northarrow
description: >
  UTILITY MODULE - read by map-create. Computes the true map scale, a scale bar with round intervals, and a north arrow rotation for the
  display projection, and says when either should be hidden or cannot be trusted.
---

# scalebar-northarrow

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/scalebar.py" info --crs EPSG:5070 --bbox XMIN YMIN XMAX YMAX --frame-mm 250 [--units auto|mi|km|m] [--rotation 0]
```

The result holds the scale ratio, the bar (total, segments, tick positions in millimetres), how much the scale varies across the
frame, and the north arrow (rotation or the reason it is hidden).

## Rules

- **A bar is true only where the local scale factor is 1.** The module measures the factor with PROJ at the center and corners.
  Variation over 1 percent is a WARN (MAP-CRS-003); over 25 percent is a BLOCK unless the bar is turned off. Web Mercator at
  high latitude is the usual cause: use an equal-area or conformal CRS instead.
- Bar lengths are 1, 2, 2.5 or 5 times a power of ten, at most 28 percent of the frame width. Units: miles inside the US,
  kilometres elsewhere, unless the spec says otherwise.
- **No bar below 1:50,000,000** (world maps): it would mislead (spec section 10).
- **The north arrow follows true north.** It is rotated by the grid convergence at the center of the map, plus any frame
  rotation. It is hidden on world-scale and polar maps, where north is not one direction, and the reason is reported.
