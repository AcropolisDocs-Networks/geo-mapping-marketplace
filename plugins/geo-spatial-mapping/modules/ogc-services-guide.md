---
name: ogc-services-guide
description: >
  KNOWLEDGE MODULE - read by geo-service-query. GeoServer and OGC specifics: capabilities, WFS versions and axis order, page limits and
  silent truncation, filters, stable paging, tile endpoints, CORS, ArcGIS differences and common failures.
---

# ogc-services-guide

## Reading capabilities

`GetCapabilities` lists the operations, the output formats for `GetFeature`, the feature types (name, title, default CRS, extent) and constraints such as `CountDefault` (the page limit when no count is given). Names carry a workspace prefix (`roads:highways`); the title is for people.

## WFS

- **1.1.0** uses `typeName` and `maxFeatures`; **2.0.0** uses `typeNames` and `count`. 2.0 pages with `startIndex`.
- **Axis order.** `urn:ogc:def:crs:EPSG::4326` means latitude then longitude; `EPSG:4326` in a GeoServer query string usually means longitude then latitude. If points land in the wrong hemisphere, the order is wrong. Ask for `srsName=EPSG:4326` and check the result's bounds (MAP-SVC-007).
- **Silent truncation.** A server may cap pages (`CountDefault`, `maxFeatures` in the service settings) and return fewer features without an error. Compare each page with the count from `resultType=hits`.
- **Stable paging** needs `sortBy` on a unique field; without it pages can repeat or skip rows.
- **Filters.** CQL (`CQL_FILTER`) is GeoServer's; OGC Filter XML is the standard and works in a POST; the kit and plugin use GET with CQL. `bbox` and `CQL_FILTER` together are refused by GeoServer; fold the box into `BBOX(geom, x0,y0,x1,y1, 'EPSG:4326')`.
- **Output.** `application/json` is GeoJSON on GeoServer. GML-only servers need `ogr2ogr`.

## Maps and tiles

WMS 1.3.0 swaps axes for geographic CRSs (use `CRS:84`). GeoWebCache serves WMTS at `/gwc/service/wmts`, and TMS or XYZ tiles at `/gwc/service/tms/1.0.0` and similar. Vector tiles (MVT) are a live-layer matter and are not in this release.

## ArcGIS REST

A layer is `.../MapServer/<id>` or `.../FeatureServer/<id>`. `maxRecordCount` is the page limit. `f=geojson` where supported, otherwise `f=json`. `where=1=1` selects everything. Tokens mean a private service.

## OGC API Features

`/collections`, `/collections/<id>/items?limit=&offset=&bbox=`. Use offset paging; follow `next` only on the same host.

## CORS and mixed content

A web page can load a live layer only if the service answers with `Access-Control-Allow-Origin` and the page and service are both https. Enabling CORS is a server administrator's task (GeoServer: the Jetty or Tomcat CORS filter in `web.xml`). Capturing the data avoids it.

## Common failures

| Symptom | Likely cause |
|---|---|
| `OperationNotSupported` or `InvalidParameterValue` | wrong version parameter name, a format the server lacks, an unknown layer prefix |
| An HTML page with status 200 | a login redirect, a gateway error page, a maintenance page |
| Fewer features than the count | a service page limit; page on in its steps |
| 401 or 403 | the service needs a login: use the request kit; do not retry |
| Points in the ocean | swapped axes, or coordinates in a projected CRS read as degrees |
| A timeout on a large layer | ask for fewer fields, a box, or a defined sample |
