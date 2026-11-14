---
name: service
description: Get features from a data service (WFS, OGC API, ArcGIS) with geo-mapping-acropolisdocs
disable-model-invocation: true
---
Use the `geo-service-query` skill for this request; when the person has not named a layer it explores the service first. Decide the route from the service: public goes through the bundled server, private or login-protected goes through a request kit the person runs. Never ask for a password, key or token; if one appears, stop and point to the kit route. Show the planned query in words before a large fetch, report counts and the capture id rather than rows, and say plainly if the license is unknown. For several layers of one public service use `/geo-mapping-acropolisdocs:map-run service-harvest` (estimate first, confirm the size); to update a capture and see what changed use `/geo-mapping-acropolisdocs:map-run service-refresh`; to take over a GeoServer style, geo-service-query's SLD route.
