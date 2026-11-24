---
name: ogc-client
description: >
  UTILITY MODULE - read by geo-service-query. The protocol layer for OGC services: WFS, OGC API Features, WMS, WMTS, with the safe HTTP
  client under it. The bundled server and the scripts use the same code.
---

# ogc-client

Python modules in `scripts/`: `ogc_http.py` (the safe client), `ogc_client.py` (protocols), `service_tools.py` (one function per operation), `service_query.py` (the command line). Skills do not open sockets themselves.

## What the safe client guarantees

- **Read-only, and declared (1.3.0).** GET and HEAD, plus the requests a supported system declares: a WFS GetFeature or ArcGIS `query` too long for a GET address goes as the protocol's POST read operation (`ogc_http.read_query`). A Transaction, LockFeature or any edit operation is refused before a request is made, and so is any request no system declares (MAP-SVC-002).
- **Public hosts only.** The host name is resolved, every address must be public, and the connection is pinned to the address that was checked. This is repeated for every redirect, at most three (SSRF guard, MAP-SVC-020).
- **No credentials in a URL**, and none held. A 401 or 403 is reported as an authentication problem and never retried (MAP-SVC-018).
- **Bounded.** Time, bytes after decoding, retries (429 and 5xx with Retry-After and backoff) and pages are all capped.
- **Honest about what came back.** An HTML error page with status 200 is an error. XML with an entity or internal subset is refused; nothing on the network is ever loaded for it.
- **Audit.** Each call is recorded with a redacted address, status, size, time and a checksum, and the list goes into the capture record.

## Protocol details that bite

- WFS 1.1 and 2.0: `typeName` vs `typeNames`, `maxFeatures` vs `count`. GeoJSON output is requested; a server with only GML is routed to the kit and `ogr2ogr` (MAP-SVC-013).
- WMS 1.3.0 swaps axis order for geographic CRSs; previews use `CRS:84` to avoid it. 1.1.1 uses `SRS`.
- Servers may return fewer features than asked without saying so; the fetch compares each page with the count and pages on.
- OGC API `next` links are followed only on the same host. The kit never follows them.

## Offline use

The parsers read saved capabilities and feature files too, which is how a private server's output is understood without contacting it.
