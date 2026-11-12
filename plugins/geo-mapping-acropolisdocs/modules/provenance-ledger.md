---
name: provenance-ledger
description: >
  UTILITY MODULE - read by map-create, map-export and geocode-addresses. Records where each layer of a map came from (source, license,
  vintage, hash, transformations), checks it, prints the source note and writes the sidecar and the Data and Methods page.
---

# provenance-ledger

`build_map.py` calls this for every map; run it directly to see what a map spec will record, or to check records without building.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/provenance.py" --spec map.yaml          # records, findings and the source note, no map
```

## What is recorded, per layer

Source (publisher and product), license class, vintage or retrieval date, the library entry id and revision when one was used,
file name and SHA-256 of the input, rows loaded, the original CRS and every transformation (reprojection, clipping, joins, classification).
Basemaps are recorded too, with the provider and its attribution. Context layers are exempt from the source and license checks.

## Findings

| Id | Severity | Meaning |
|---|---|---|
| MAP-LIC-001 | BLOCK | A layer has no declared source, no license, or no date (retrieved_at, date or vintage). |
| MAP-LIC-009 | WARN | A source address is internal. The host is replaced by "internal data service" in everything the reader sees. |
| MAP-LIC-015 | BLOCK | A provider's terms require its logo beside its credit and the logo cannot be drawn (1.1.3). |

Fix a MAP-LIC-001 by writing `provenance: {source: ..., license: ..., retrieved_at: YYYY-MM-DD}` on the layer, or by using a library entry, which supplies all three.

## A file made from a library source (1.1.3)

When the person's file was made from a source the library knows (weather fetched from OpenWeather and joined to their sites, say), name the
entry: `provenance: {source: ..., license: ..., retrieved_at: ..., library_entry: ref.public-service.world.weather.openweathermap}`. The layer
then carries that entry's credit, licence gates and, when its terms require one, the provider's logo. A blocked entry refuses the layer.

**Provider logos.** An entry whose terms require a logo beside the credit says so in `attribution.logo` (`file`, `height_px`, `link`, `alt`): a
plain SVG the plugin ships in `assets/logos/`. The print footer draws it at its height (10 px is 2.65 mm) at the start of the source note, in
the text colour and unlinked (the credit text names the address); a web page draws it, linked, before the credit in the credits corner, and
Save image burns it in. OpenWeather's free plan needs its logo, the text "Weather data provided by OpenWeather" and the link: all three come
from the entry. A logo that cannot be drawn blocks the map (MAP-LIC-015). Never draw a provider's logo by hand or from the web.

## Outputs

- **Source note**: one line on the map. It shows the vintage, never the retrieval date.
- **Sidecar** `<name>.provenance.json`: always written next to the map. It stays with the files the build made but is not one of the deliverables sent to the person unless asked for.
- **Data and Methods page**: off by default. Turn it on with `elements: {methods_page: {on: true}}` for a one-page `<name>_methods.svg` (and `.pdf` when PDF is built) listing each layer's source, license, vintage, hash and steps. It is a separate file so the map PDF stays one page.
