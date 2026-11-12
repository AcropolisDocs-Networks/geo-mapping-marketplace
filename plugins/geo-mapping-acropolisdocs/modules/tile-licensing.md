---
name: tile-licensing
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle, map-export, webmap-create and map-services. What each basemap tile provider's terms
  allow (generated from the library): keys, live versus bulk use, attribution, print, commercial use, and which providers are refused.
---

# tile-licensing

Read this before choosing a basemap, or when someone asks "can I use Google or Bing maps?". The table is generated from the
`basemap-provider` entries, so it matches the library. Terms change; the last column says whether the terms were read at build or must be verified again.

| Provider | Status | Key needed | Live use | Bulk or offline | Redistribute | Commercial | Print | Attribution to show | Verify at build |
|---|---|---|---|---|---|---|---|---|---|
| CARTO basemaps (Positron, Voyager, Dark Matter) (`ref.basemap-provider.carto-positron`) | active | yes | yes | no | no | yes | yes | © OpenStreetMap contributors, © CARTO | terms read 2026-11-08 |
| Esri ArcGIS basemaps (`ref.basemap-provider.esri-arcgis`) | active | yes | yes | no | no | yes | yes | Powered by Esri. Sources: Esri, TomTom, Garmin, FAO, NOAA, USGS, © OpenStreetMap contributors, and the GIS User Community | terms read 2026-11-07 |
| Mapbox basemaps (`ref.basemap-provider.mapbox`) | active | yes | yes | no | no | yes | no | © Mapbox © OpenStreetMap | terms read 2026-11-07 |
| MapTiler basemaps (`ref.basemap-provider.maptiler`) | active | yes | yes | no | no | yes | not stated | © MapTiler © OpenStreetMap contributors | terms read 2026-11-08 |
| NASA GIBS satellite imagery (`ref.basemap-provider.nasa-gibs`) | active | no | yes | no | yes | yes | yes | Imagery: NASA Global Imagery Browse Services (GIBS), part of NASA's Earth Science Data and Information System (ESDIS) | terms read 2026-11-07 |
| OpenStreetMap standard tiles (`ref.basemap-provider.osm-standard`) | active | no | yes | no | no | yes | yes | © OpenStreetMap contributors | terms read 2026-11-02 |
| PMTiles basemap (your own file) (`ref.basemap-provider.pmtiles-user`) | active | no | yes | yes | yes | yes | yes | © OpenStreetMap contributors | terms read 2026-11-09 |
| Protomaps basemap API (vector tiles) (`ref.basemap-provider.protomaps-api`) | active | yes | yes | no | no | no | no | © OpenStreetMap | terms read 2026-11-08 |
| Stadia Maps basemaps (Alidade Smooth, Stamen Toner and Terrain, Outdoors) (`ref.basemap-provider.stadia`) | active | yes | yes | no | no | no | no | © Stadia Maps © OpenMapTiles © OpenStreetMap contributors | terms read 2026-11-07 |
| Thunderforest basemaps (Transport, Outdoors, Cycle) (`ref.basemap-provider.thunderforest`) | active | yes | yes | no | no | yes | yes | Maps © Thunderforest, Data © OpenStreetMap contributors | terms read 2026-11-10 |
| USGS National Map topographic tiles (`ref.basemap-provider.usgs-topo`) | active | no | yes | no | yes | yes | yes | Map services and data available from U.S. Geological Survey, National Geospatial Program. | terms read 2026-11-04 |
| Ordnance Survey Maps API (OS Data Hub, Great Britain) (`ref.basemap-provider.os-maps`) | draft | not verified | not verified | not verified | not verified | not verified | not verified | not verified | yes |
| Bing Maps tiles (`ref.basemap-provider.bing-maps`) | blocked | no | blocked | no | no | no | not stated |  | yes |
| Google Maps tiles (`ref.basemap-provider.google-maps`) | blocked | no | blocked | no | no | no | not stated |  | yes |

Terms as recorded in the library:

- **CARTO basemaps (Positron, Voyager, Dark Matter)**: The publisher's page lists a free allowance (5 million requests a month non-commercial, 1 million commercial) and asks for the attribution; read the current terms before heavy use.
- **Esri ArcGIS basemaps**: Master Agreement E204CW read 2026-11-07 (arcgis.com/home/termsofuse.html): 3.2(a) data only with the products Esri provided it for; 3.2(b) hard-copy or static electronic representations (PDF, GIF, JPEG, HTML) allowed, including in reports and presentations for third parties, with an attribution statement naming Esri or its licensors; 3.2(c) offline only through Esri Content Packages, otherwise no scraping, downloading or storing; 3.4 licensors' extra attribution at esri.com/legal/third-party-data; B.1(g) attribution may not be removed. Static maps attribution guide read the same day: "Powered by Esri" (at least "Esri") and the data source names, unobstructed. The source list differs by style and changes: check it with the style's attribution endpoint at each review. Earlier: The Master Agreement page lists PDFs only. Product-Specific Terms of Use E300 (read 2026-11-07): footnote 10 forbids programmatic use of session tokens "(e.g., exporting volumes of basemap tiles)" on Location Platform and developer plans; footnote 89 requires authentication for a revenue-generating application; E300 says nothing on caching, print or attribution, which sit in the Master Agreement E204 (PDF, not read). The attribution service (static.arcgis.com/attribution/World_Street_Map) answered 200 on 2026-11-04.
- **Mapbox basemaps**: Read 2026-11-07: Terms of Service (licence to use the Services in Licensed Applications; proprietary notices may not be removed; the Product Terms govern use and are a PDF not read); attribution guide ("© Mapbox", "© OpenStreetMap" and "Improve this map" as links, the wordmark always shown; static and print maps carry the logo and "© Mapbox, © OpenStreetMap"); static maps help page (printing Studio exports needs Mapbox sales; print maps need Mapbox attribution); pricing page (free monthly allowances).
- **MapTiler basemaps**: MapTiler Terms of Service, section 'Map Data Attribution' (read 2026-11-04): '© MapTiler' (with the MapTiler logo on a free account) and '© OpenStreetMap' linked to openstreetmap.org/copyright on every map; commercial use per plan; no bulk extraction.
- **NASA GIBS satellite imagery**: GIBS API documentation (read 2026-11-07): NASA "promotes full and open sharing of data" with no restrictions listed; it asks clients and presentations to acknowledge "the use of imagery provided by services from NASA's Global Imagery Browse Services (GIBS), part of NASA's Earth Science Data and Information System (ESDIS)". No rate limit or caching rule is stated; the tiles are sent with no-store, so none is kept.
- **OpenStreetMap standard tiles**: Governed by the OSMF Tile Usage Policy; the data itself is under the ODbL.
- **PMTiles basemap (your own file)**: OpenStreetMap-derived tiles carry the ODbL: attribute, and share a derived database under the same license. Check the license of the specific file you supply.
- **Protomaps basemap API (vector tiles)**: Protomaps API page (read 2026-11-05): "The Protomaps Tile API is free for non-commercial use"; commercial use needs a GitHub Sponsorship; a key is required and its allowed sites are set on the account; requests must carry an Origin header unless the key's CORS setting is *; zoom up to 15; the attribution asked for is "© OpenStreetMap" linked to openstreetmap.org/copyright. The site terms (protomaps.com/terms) forbid scraping and harvesting. The data is OpenStreetMap's, under the ODbL: OSMF attribution guidelines (read 2026-11-05) ask for "OpenStreetMap" (or "© OpenStreetMap contributors") in a corner of the map, visible without interaction, linked to openstreetmap.org/copyright, and that URL printed in full on a printed or exported picture.
- **Stadia Maps basemaps (Alidade Smooth, Stamen Toner and Terrain, Outdoors)**: Free plan non-commercial (pricing page, read 2026-11-07); caching static maps and printing (up to 5,000 copies) are tied to a paid subscription. Stamen styles also credit Stamen Design. The data is OpenStreetMap's, under the ODbL.
- **Thunderforest basemaps (Transport, Outdoors, Cycle)**: Terms of service read 2026-11-10 (thunderforest.com/terms) - commercial use is permitted and encouraged; each map image is CC-BY-SA 4.0, the map data ODbL; credit Thunderforest and OpenStreetMap contributors with working links, or on printed media 'Maps © www.thunderforest.com, Data © www.osm.org/copyright'. The free plan is offered at Thunderforest's discretion.
- **USGS National Map topographic tiles**: USGS FAQ 'What are the terms of use/licensing of map services and data from The National Map?' (read 2026-11-04): public domain, no restrictions; the acknowledgement line above is requested.
- **Ordnance Survey Maps API (OS Data Hub, Great Britain)**: Attribution wording and Premium terms were not read on 2026-11-07; confirm before the entry leaves draft.
- **Bing Maps tiles**: Bing Maps terms allow Bing map content only through Microsoft's own APIs and controls; extracting tiles for static maps or exports is not permitted. Use OpenStreetMap-derived or bundled basemaps instead.
- **Google Maps tiles**: Google Maps Platform terms allow Google map content only through Google's own APIs with Google's display requirements; extracting tiles for static maps or exports is not permitted. Use OpenStreetMap-derived or bundled basemaps instead.

## Rules this plugin follows

- **Google Maps and Bing Maps tiles are refused.** Their terms do not allow extracting tiles for static maps or exports. Offer the bundled basemap, an OpenStreetMap-derived basemap or the person's own PMTiles file.
- **The bundled basemap is the default.** It needs no key, no network and no attribution beyond its own credit line.
- **OpenStreetMap's own tile servers are for light, live use.** Bulk downloads and offline exports are not allowed by the OSMF Tile Usage Policy. They are a customization beyond the defaults: the plugin uses them once the person has made the person's one acknowledgment (`/geo-mapping-acropolisdocs:map-services`, made once), which says they keep each service they choose within its terms. The plugin keeps the tile cap at a quarter and prints the credit either way; nothing is set in the MapSpec.
- **A provider that needs a key is used only with the person's own registered key** (`/geo-mapping-acropolisdocs:map-credentials add <provider>`). The plugin never asks for one in chat and
  never writes one into a file. A `secret` key (the default) is used only for print tiles fetched on the person's computer; only a `browser` key, restricted to the
  person's domain, may go into a web page.
- **Attribution is part of the map.** The source note prints the provider's attribution text. It is not removed to save space. On a web page
  the provider's `attribution:` block (short corner form, links, full text, when it may collapse, any logo the terms require, the export
  wording) puts the credit in the map's bottom-right corner, never covered (`webmap-controls`, MAP-WEB-030 to -034).
- **A draft provider is not used.** Its terms have not been read yet.
- **Bulk or offline use** (exporting tiles, a data package) is allowed only where the table says "yes" for bulk. When in doubt, use the person's own PMTiles file.

Tile basemaps are used only on web map pages (`webmap-create`); print maps keep the bundled basemap. A page's tile provider is checked by `tile-provider-registry`, and its provenance record notes that the reader's browser fetches the tiles live.
