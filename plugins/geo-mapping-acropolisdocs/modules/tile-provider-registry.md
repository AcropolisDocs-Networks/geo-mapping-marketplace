---
name: tile-provider-registry
description: >
  KNOWLEDGE MODULE - read by webmap-create and map-services. Which basemap tile providers a web map page may use, the registered credential
  that holds each key and its class, and what happens when a provider is refused, missing a key, or not available in this release.
---

# tile-provider-registry

The registry is the library's `basemap-provider` entries read by `tile_registry.py`. A page asks for a provider by name and gets a URL template, a host, an attribution and a zoom limit, or a reason it cannot.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/tile_registry.py" list [--audience external]
python3 "$GMAP_ROOT/scripts/tile_registry.py" resolve --provider carto-positron [--style light_all] [--audience external]
```

`list` shows every provider, whether it is usable now, whether it needs a key, the credential name (`cred:<provider>`) and the classes it may have. `resolve` prints the decision with the key masked. Exit 4 means refused.

## Decisions the registry makes

| Case | Result |
|---|---|
| Google Maps, Bing Maps | Refused (MAP-WEB-011). The entry says why: their terms do not allow what a static or exported map does. Offer the bundled background. |
| A provider whose entry is draft | Refused: its terms have not been read. |
| OpenStreetMap's own tile server | A customization: used once the person has made the person's one acknowledgment (`/geo-mapping-acropolisdocs:map-services`, made once); refused without it, the refusal naming `/geo-mapping-acropolisdocs:map-services`. A public page gets a warning that the policy (light use, credit shown, cached tiles, no bulk) still applies; an internal page a note. A page opened from a file sends no Referer, so the server may still refuse it: open it from a web server. |
| A style the provider does not list | Refused; the message lists the valid styles. |
| A provider that needs a key and none is registered | The bundled background is used (MAP-WEB-015). The message says how to add one: `/geo-mapping-acropolisdocs:map-credentials add <provider>`. |
| A `secret`-class key is registered (the default) | A web page is refused (MAP-CRED-011) with the alternatives; a print basemap uses it on the person's computer: from a cloud or Cowork session, through the data server's `basemap_tiles` tool (1.12.0), which fetches the tiles there and writes the composite into the project folder. |
| A `browser`-class key is registered | Tiles are used; MAP-WEB-003 reminds the person the key is visible in the page; MAP-CRED-009 warns when the domain restriction was never confirmed. |
| PMTiles (the person's own file) | `basemap: {provider: pmtiles, source: pmtiles:FILE}`: a vector PMTiles file (Protomaps, OpenMapTiles or Shortbread layer names; 1.10.0) drawn by the MapLibre page in the brand's colours, embedded in the page (over 40 MB, or with `webmap.basemap_file: beside`, served beside it from a web server). Without `source` the bundled background is used (MAP-WEB-015); a missing file or one that is not vector PMTiles is refused. **On a printed map (1.10.0)** the same file (or a vector `mbtiles:FILE`) is read as the ground itself, with no preparation step and in the map's own projection: land, water and border lines, and its roads, rivers and built-up land as the context, all in the brand's map colours, at the zoom that suits the print scale; the file's own credit goes in the source note. A file of another schema, or one that does not cover the whole map window, is left out with the reason (MAP-CART-037) and the bundled ground is drawn. `python scripts/vector_ground.py FILE` says what a file holds. |
| Protomaps API (`protomaps`) | Vector tiles for the MapLibre page; a browser-class key registered with `/geo-mapping-acropolisdocs:map-credentials add protomaps --class browser`. Its free tier is for non-commercial use only: the key is the person's own acceptance of the terms, so it is used with a note that their plan decides whether a client page may use it; the credit is OpenStreetMap's, linked to its copyright page. A Leaflet page cannot draw vector tiles: the bundled background is used (MAP-WEB-015). |
| NASA GIBS (`nasa-gibs`, 0.16.0) | Satellite imagery, no key: `style` is MODIS_Terra_CorrectedReflectance_TrueColor (default), VIIRS_SNPP_CorrectedReflectance_TrueColor, BlueMarble_ShadedRelief_Bathymetry, VIIRS_CityLights_2012 or VIIRS_Black_Marble; `basemap.time: YYYY-MM-DD` names the day of a daily layer (two days ago when not named; a future day is refused). Zoom 9 (daily) or 8 at most: world to state maps, not streets. Prints and pages. |
| Esri (`esri`, 0.16.0) | ArcGIS styles `arcgis/streets` (default), `arcgis/navigation`, `arcgis/light-gray`, `arcgis/dark-gray`, `arcgis/outdoor`, `arcgis/community` as 512-pixel tiles, with the person's own Location Platform key (`/geo-mapping-acropolisdocs:map-credentials add esri`, `--class browser` for a page, restricted to its referrers). Prints allowed with "Powered by Esri" and the sources; tiles are never kept. |
| Mapbox (`mapbox`, 0.16.0) | Web pages only, with a browser-class public token restricted to the page's URLs. The page shows the Mapbox logo Mapbox requires, loaded from Mapbox's own stylesheet while the basemap is shown. A printed map is refused: printing needs Mapbox's permission. |

## Keys

Keys are the person's own registered credentials, `cred:<provider>` (`cred:maptiler`, `cred:carto-positron`), kept with `/geo-mapping-acropolisdocs:map-credentials`.
Two classes: `secret` (the default) never leaves the request made on the person's computer; `browser` is a key the provider designs to be public and the
person has restricted to their domain, and is the only kind that may go into a page. `GEO_MAPPING_KEY_<PROVIDER>` (for example `GEO_MAPPING_KEY_CARTO_POSITRON`)
in the person's environment is still read as a fallback for the command line only; the desktop app's processes do not see it. The plugin never asks for a
key in the chat, never writes one into a spec or a log, and never prints one. A key in a page is readable by anyone who has the page.

## Tell the person

Say which background was used and why in one phrase. If a fallback happened, say how to register a key (`/geo-mapping-acropolisdocs:map-credentials add <provider>`, with `--class browser` for a page). Do not describe a refused provider's terms from memory; use the entry's own words (`ref_library.py get ref.basemap-provider.<name>`). See `tile-licensing` for what each provider's terms allow.
