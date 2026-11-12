---
name: arcgis-rest-client
description: >
  UTILITY MODULE - read by geo-service-query. Esri REST FeatureServer and MapServer: service and layer information, counts, paged queries,
  GeoJSON conversion, image export and legends. Read-only, through the safe client.
---

# arcgis-rest-client

`scripts/arcgis_client.py`. Same safe client as `ogc-client`; the bundled server uses it for `protocol=arcgis`.

- **Address.** `<REST root>/<Folder>/<Service>/<MapServer|FeatureServer>/<layer id>`. The layer path is the `--layer` value. The profile's base is the REST root.
- **Count first.** `query?returnCountOnly=true`.
- **Pages.** `resultOffset` and `resultRecordCount`, ordered by the object id field. The layer's `maxRecordCount` is the real page limit; `exceededTransferLimit` says more remains. A page smaller than asked is not the end while the count says otherwise.
- **Format.** `f=geojson` when the layer supports it; otherwise `f=json` is converted (points, lines, polygons with holes). Coordinates are requested in 4326.
- **Edit verbs are refused.** `applyEdits`, `addFeatures`, `updateFeatures`, `deleteFeatures` and the rest, and REST configuration changes (MAP-SVC-002).
- **Tokens.** A service that needs a token is a private service: use the request kit. The plugin never holds or asks for a token.
- **Other tools.** `export_preview` makes a preview image (not georeferenced), `legend` reads the legend; both are data from the service.
- **A picture you will place on the map: `export_image`, never a hand-built `export` call.** ArcGIS widens the box to the image's shape when the two
  differ, so a square image of a tall box covers a wider area than asked. Placing it on the requested box squeezes it toward the middle (the
  2026-11 Day 2 forest layer was 9% narrow, 24 km at a tile edge, and stopped short of the coast). `export_image` sizes the image to the box
  (square pixels), reads the extent the server drew from `f=json`, and returns it as `extent`; place the picture on that, with `cell_size`. For
  a large area, tile the box and export each tile the same way; never stitch tiles by their requested boxes.
