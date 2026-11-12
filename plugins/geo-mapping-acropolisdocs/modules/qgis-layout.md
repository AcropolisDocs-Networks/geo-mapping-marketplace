---
name: qgis-layout
description: >
  UTILITY MODULE - read by map-export. Experimental optional path: lays a finished branded MapSpec out in QGIS (PyQGIS) and exports
  PDF, PNG or SVG from there, leaving an editable .qgz. Only when QGIS is installed and the person asked for it.
---

# qgis-layout (experimental)

For teams that finish maps in QGIS. The plugin writes a kit of its own decisions (classes, colors, extent, title, sources, banner) and a fixed
PyQGIS script lays them out, so the QGIS map matches the plugin's SVG map. The supported renderer is still `build_map.py`; use this path only when asked.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/qgis_layout.py" probe                                            # is PyQGIS usable, and which Python has it
python3 "$GMAP_ROOT/scripts/qgis_layout.py" run --spec map.yaml --out out --formats pdf,png   # kit, QGIS build, checks, hidden accreditation
python3 "$GMAP_ROOT/scripts/qgis_layout.py" kit --spec map.yaml --out kit                     # the kit only, for opening or scripting yourself
```

## Rules

- Run `probe` first. Without PyQGIS (QGIS 3.28 or later) say so, and offer the normal build; never try to install QGIS unasked. When QGIS has its own
  Python, set `GEO_MAPPING_QGIS_PYTHON` to it.
- The path needs no network and no credentials. The ground the plugin draws is in the layout (1.11.0): land, water and border lines, the relief, a
  raster layer's window, and a tile basemap when its tiles were prepared beforehand (`basemap-render`); nothing is fetched here, so a tile basemap
  with no prepared tiles is left out and the notes say so.
- Read `*_qgis_report.json`: every check must be ok (page size, Inter embedded, PNG size). Report the notes, in particular a legend or scale bar sitting over data.
- The legend, scale bar and north arrow are QGIS items, each in the corner with least data under it. The locator is the plugin's own, placed as a
  picture (1.11.0). Offer the `.qgz` for fine adjustment.
- Labels and place names are placed by the plugin's own label engine and carried as a `Labels` layer, each pinned at its point with its size, colours
  and side (1.11.0); QGIS does not move them. Labels the SVG path draws as outlines (non-Latin scripts) or along a curve are not carried; the notes
  count them. Class breaks, colors, shapes, the title and the source lines are the plugin's.
- The confidentiality banner stays when a layer is client-confidential. Never remove it to make room.
- Say "experimental" in the reply; the report says so too.
