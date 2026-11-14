---
name: transit-maps
description: >
  KNOWLEDGE MODULE - read by map-create. Transit maps from a GTFS feed: lines by route, stops, frequency, parallel
  lines drawn side by side, and what the feed must hold.
---

# transit-maps

## Transit maps from a GTFS feed

A GTFS zip (or folder) is a file source like any other; `layer:` picks what to draw (`scripts/gtfs.py`, read through geo-io):

```yaml
layers:
- id: lines
  source: {type: file, path: feed.zip, layer: "routes:light_rail"}   # one line per route, its shapes merged
  role: lines
  value_field: route_name
  classification: {method: categorical}
  symbol: {width: 2.2, parallel: true}                               # lines sharing track drawn side by side, not on top of each other
  legend: {title: Line, swatch: line}
- id: stations
  source: {type: file, path: feed.zip, layer: "stations:light_rail"} # stations, with the lines that stop there in `routes`
  role: points
  symbol: {shape: circle, size: 2.2}                              # no color: the next free palette color
  legend: {title: Station, label: Station}
- id: trains
  source: {type: file, path: trains.geojson}                         # gtfs.py vehicles PB trains.geojson --feed feed.zip --mode light_rail
  id_field: vehicle_id
  role: points
  symbol: {size: 3.4, color: '@brand.status.danger', rotate_field: heading}   # a pointer showing the direction of travel
  legend: {title: Trains, label: 'Train at 11:56 PT, pointing the way it runs'}
labels: [{layer: stations, field: stop_name}]
```

- Layers: `routes`, `shapes`, `stations`, `stops`, `entrances`, each optionally `:mode` (`light_rail`, `subway`, `rail`, `bus`, `ferry`,
  ...). `stations` holds `routes`, `route_ids`, `modes`, `n_routes`; `routes` holds `route_name`, `route_long_name`, `mode`,
  `route_color` and `trips`. `gtfs.py info FEED` says what the feed holds before the MapSpec is written.
- **Shared track:** `symbol: {parallel: true}` on the categorical lines layer sets lines that share a path side by side, one line width apart,
  stacked in the category order on every trunk (Seattle's 1 and 2 Lines; nine services on a New York trunk). `parallel_gap` (points) adds a
  gap; `parallel_tolerance` (page mm) says how close counts as sharing. Print, PNG and slides only: the web map keeps the true paths.
- **Direction of travel:** `symbol: {rotate_field: heading}` draws each point as a pointer turned to its compass bearing (degrees from true
  north, turned into the map's own grid); a point with no heading keeps the plain shape. `gtfs.py vehicles` writes `heading` from the feed's
  bearing, or, when the feed sends none (Sound Transit does not), from the trip's own shape at the vehicle's position (`heading_source`;
  MAP-GTFS-009 counts each). Web pages draw the same pointer (1.14.0), turned with the map; the web legend keys it as a pointer.
- **Directional route pairs:** a routes layer carries `terminal_a`, `terminal_b` and `route_pair` (the two terminals in name order, the same for both
  directions; up to 150 routes, so name a mode for a large feed). `ops: [{op: dissolve, by: route_pair}]` draws each pair once; join a two-column
  table of route_pair and the agency's own route name, and dissolve by that, for an agency's operating routes (MAP-GTFS-010 says how many pairs).
- Line colours come from the brand's categorical palette by `route_name`, like any category. The agency's own `route_color` is in the
  data (and in the web map's popups); use it only when the person asks for the agency's colours.
- Say the feed's publisher, version and service dates (the read note) in the source line, and pass on MAP-GTFS-001 when the feed has
  ended or not started. A feed is downloaded from the agency's open-data page (`geo-find-data`); the plugin never fetches it at build time.
- Live positions: a saved GTFS-Realtime `.pb` is a points source too (`layer: vehicles`), or `gtfs.py vehicles PB OUT.geojson --feed FEED
  --mode light_rail` joins each train to its route name and heading first. Say it is a snapshot and when (the note gives the time).
- Travel-time areas around stations (a 10-minute walk, transit reach): `isochrone-map`.
