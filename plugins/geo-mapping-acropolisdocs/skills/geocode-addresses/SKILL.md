---
name: geocode-addresses
description: >
  Turn a table of site, venue or store addresses into map points (US Census or Nominatim) with match quality; never private individuals'
  addresses. Trigger on: geocode these addresses.
---

# geocode-addresses

**The privacy rule comes first (spec 14).** This plugin geocodes the addresses of sites, venues and businesses: cell sites, venues, stores,
offices, schools, hospitals, depots. It never geocodes where a private person lives, works or travels. Every run declares a `purpose`
(`sites`, `venues` or `businesses`); a table whose columns name a person (first and last name, resident, date of birth) is refused whatever
the purpose, and a name column beside a street column is refused until the purpose is declared (MAP-SVC-035). When a list is people's home
addresses, say so and stop: there is no override.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/geocode.py" --in venues.csv --address street,city,state,zip --provider census --purpose venues --out venues.gpkg
python3 "$GMAP_ROOT/scripts/geocode.py" --in stores.csv --address address --provider nominatim --purpose businesses --country ca --out stores.gpkg
python3 "$GMAP_ROOT/scripts/geocode.py" reverse --lon -112.2646 --lat 33.5291                  # Census: place, county, state at a point
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
python3 "$GMAP_ROOT/scripts/modules.py" read provenance-ledger        # any module in the table
```

| Module                 | Read it |
|------------------------|---|
| `provenance-ledger`    | for the provenance record and the attribution the map carries |
| `site-confidentiality` | when the points are client sites going to an external audience |

The bundled server offers the same as the `geocode` tool (`file`, `address`, `purpose`, `provider`, `country`); it writes the
points under `.geo-mapping/captures/geocode/` and returns counts and paths, never the rows. In a MapSpec or `geo-analyze`, the same step is the
op `{op: geocode, address: [street, city, state, zip], provider: census, purpose: venues}` on a table layer.

## Providers and their terms

| Provider | Coverage | Terms | How it runs |
|---|---|---|---|
| `census` (default) | United States only; street addresses, not place names | No key, no stated rate limit, public domain. Benchmark `Public_AR_Current` | One batch upload of up to 10,000 rows per request (the one POST the plugin sends; same host check, caps and audit as every GET). A whole table of a few thousand rows takes a minute or two |
| `nominatim` | World; addresses and place names (OpenStreetMap) | Usage policy: one request a second, a User-Agent naming the application, no bulk use, attribution **Data © OpenStreetMap contributors, ODbL 1.0** on every map. A customization beyond the default: it needs the person's one acknowledgment (`/geo-mapping-acropolisdocs:map-services`, made once) (MAP-SVC-036); at most 1,000 rows a run | One request per row, paced a second apart, with backoff on 429 |

US tables go to the Census geocoder. Outside the US, or for venue names rather than street addresses, use Nominatim (once the person has made the acknowledgment; `/geo-mapping-acropolisdocs:map-services` asks it) with
`--country` set; tell the person the ODbL attribution goes on the map. A larger non-US table is a job for the person's own geocoder (their own
Nominatim, or a licensed provider with their key), not for this plugin.

## The address columns

`--address` is one column holding the whole address, or the street, city, state and ZIP columns in that order (names such as `Street Address`,
`City`, `ST`, `ZIP` are read by name; anything else by position). A one-line address goes to the Census geocoder in its street field, which parses it.
Check the match rate before using the result: a rate under 80 percent nearly always means the columns were given in the wrong order or the city
and state are missing (MAP-SVC-037).

## Match quality

Each row gets `lon`, `lat`, `match_quality`, `matched_address` and `geocoder`; an unmatched row keeps its attributes and has no geometry.

| `match_quality` | Census | Nominatim |
|---|---|---|
| `exact` | the address range matched exactly | a building, amenity, shop or venue (place rank 30) |
| `non_exact` | matched after normalisation (a misspelt suffix, a missing direction); check a sample | — |
| `approximate` | — | a street or block, not the building |
| `area` | — | a town, district or region: the address was too thin |
| `tie` | several places matched equally; not placed | — |
| `no_match` | nothing matched | nothing matched |

Census coordinates are interpolated along the street segment (TIGER/Line), so a point sits on the street near the address, not on the rooftop:
good for a market or county map, not for a parcel-level map. Say so when the map is zoomed in.

## The record

`<out>.geocode.json` beside the points: provider, endpoint, purpose, rows, matched, match rate, the ids of unmatched rows, quality counts, the
attribution, the terms, every call (URL, status, bytes, SHA-256) and the findings. The map's methods page takes the provider, the count and the
attribution from it; the unmatched ids go in the reply so the person can fix those rows.

## Tell the person

How many rows geocoded of how many, with which provider; the match-quality counts; the unmatched rows by id; the attribution line the map must
carry; and the next step (`map-create` with the GeoPackage, or `/geo-mapping-acropolisdocs:map-run program-map` for a tracker). When the gate refused the table, say which column
set it saw and what to change: declare the purpose, or drop the person-name columns.
