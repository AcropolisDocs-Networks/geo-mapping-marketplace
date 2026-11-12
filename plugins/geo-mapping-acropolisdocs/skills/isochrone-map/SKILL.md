---
name: isochrone-map
description: >
  Make travel-time (isochrone) areas and maps on foot, by bike, by car or by transit from sites, stations or venues. Trigger on: isochrone,
  walkshed, drive time.
---

# isochrone-map

Travel-time areas around one or more origins, as non-overlapping bands (0-5, 5-10, 10-15 min) in a GeoPackage, then a map. A straight-line ring
(`geo-analyze` buffer) answers "within 800 m"; this answers "within a 10-minute walk", which follows the streets, the water and the bridges.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/isochrone.py" providers                                      # modes, limits and terms of each provider
python3 "$GMAP_ROOT/scripts/isochrone.py" --in stations.gpkg --id stop_name --mode walk --minutes 5,10,15 --out walk.gpkg
python3 "$GMAP_ROOT/scripts/isochrone.py" --point=-122.3367,47.6116 --name Westlake --mode drive --minutes 10,20,30 --provider ors --credential cred:ors --out drive.gpkg
python3 "$GMAP_ROOT/scripts/isochrone.py" --point=-122.3367,47.6116 --name Westlake --mode transit --gtfs feed.zip --date 2026-11-10 --depart 08:00 --window 60 --minutes 15,30,45 --out transit.gpkg
```

The same anywhere: walksheds round Lisbon's Baixa-Chiado station, the map in kilometres, an A4 sheet and the Portuguese national grid when built
with `template: auto` (1.5.0):

```bash
python3 "$GMAP_ROOT/scripts/isochrone.py" --point=-9.1405,38.7107 --name Baixa-Chiado --mode walk --minutes 5,10,15 --out lisbon_walk.gpkg
```

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/modules.py" read credential-broker        # any module in the table
```

| Module              | Read it |
|---------------------|---|
| `credential-broker` | when the person's openrouteservice key is used |

A western longitude starts with a minus sign: write `--point=LON,LAT` with the equals sign. The bundled server offers the same as the `isochrone`
tool (`mode`, `minutes`, `provider`, `points="lon,lat;lon,lat"`, `names="A;B"` or `file=` with `id_column=`, `credential`,
`gtfs_feed`, `date`, `depart`, `window`, `step`, `dissolve`, `nested`, `work_folder`); it is the route when the person's openrouteservice key is on
their computer, since the key is read there and never comes into the chat.

## Choosing the provider

| Mode | Provider | Ask the person | Limits |
|---|---|---|---|
| walk, bike, drive | `valhalla` (default) | Nothing: the default. A free community server for light use; the plugin keeps to its terms (one request a second, the origin cap, the OpenStreetMap credit) | one request a second, at most 25 origins a run, bands up to 120 min |
| walk, bike, drive | `valhalla` + `--valhalla-url` | Their own Valhalla server's public address | none from the plugin |
| walk, bike, drive | `ors` | Their own openrouteservice key, registered once: `credentials_add name=ors host=api.openrouteservice.org inject=header:Authorization`, then they paste the key into the file it names. Never ask for the key in chat | free plan: 20 a minute, 500 a day; bands up to 60 min; up to 200 origins a run |
| transit | `gtfs` | The agency's GTFS feed (see `map-create`, transit maps), a service date inside the feed's dates, a departure time | computed here; bands up to 120 min |

More than 25 origins on the public server is refused (MAP-ISO-002): offer `ors` with their key, their own Valhalla, or a shorter list.

## Transit, and what to say about it

Transit areas are computed from the timetable: walk to the stops within reach, ride the trips in service that day (connections, transfers within
250 m and those in transfers.txt), walk on from each stop reached in the time left. Walking there is straight-line distance times 1.3 at 4.5 km/h,
not the street network, so say "about". One departure depends on luck with the timetable; for a fair picture use `--window 60 --step 10` (the
median over departures every 10 minutes for an hour) and say which on the map (the MAP-ISO-006 note has the words). A feed with only rail shows
rail reach; for buses too, use the agency's bus feed (some regions publish one combined feed).

## Mapping the bands

```yaml
layers:
- id: walk
  source: {type: file, path: walk.gpkg, layer: isochrones}
  role: polygons
  value_field: band
  classification: {method: categorical, order: [0-5 min, 5-10 min, 10-15 min],
                   colors: ['@brand.sequential.5', '@brand.sequential.3', '@brand.sequential.1']}
  symbol: {opacity: 0.55, stroke: '@brand.background', stroke_width: 0.3}
  legend: {title: Walking time, swatch: rect}
  provenance: {source: "walk times by Valhalla, FOSSGIS on OpenStreetMap", publisher: {name: "Valhalla, FOSSGIS", product: walk times},
               derived_from: osm, license: ODbL-1.0, attribution: "© OpenStreetMap contributors", retrieved_at: '2026-11-06'}
```

- Darker is nearer: put the nearest band on the darkest ramp step. The bands do not overlap, so they can be opaque; keep some transparency when
  streets or names must show through.
- Credit: copy the record's `provenance` block into the layer as it is (0.19.0). `derived_from: osm` files the bands under OpenStreetMap on the
  consolidated source line, the service in the parentheses: "© OpenStreetMap contributors (walk times by Valhalla, FOSSGIS)"; a transit
  isochrone is credited to the agency named in the feed.
- Many origins: `--dissolve` draws one set of bands, "within N minutes of any station"; without it each origin has its own bands (good for one to
  five origins, crowded beyond).
- A metro or city map: run `ground_detail.py prepare --spec` first, so the water the bands stop at is drawn in detail (see `map-create`).

Tell the person: the provider and its terms, how many origins were skipped (no road near them, MAP-ISO-007) and, for transit, the date, the
departures and that walking is approximate.
