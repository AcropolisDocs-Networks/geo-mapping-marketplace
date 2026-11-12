---
name: geo-find-data
description: >
  Find geodata for a map: the reference library first, then data.gov, ArcGIS Hub, Socrata, STAC and the web, with license and download
  route. Trigger on: find map data, where do I get boundaries.
---

# geo-find-data

Answers "what data should this map use, and where does it come from?" The library comes first because its entries have verified
sources, licenses and join keys. The open-data catalogs come second (`geo-catalog-search`: data.gov, ArcGIS Online and Hub, Socrata, STAC,
in one call, each result with its license and route). The web is the last fallback, and what it finds is reported as a **find record**,
never used silently.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/ref_library.py" select --geography us-az --level county --use-for choropleth --scale state [--audience external]
python3 "$GMAP_ROOT/scripts/ref_library.py" select --bbox=3.1,6.3,3.7,6.7 --level admin2 --use-for choropleth      # Lagos: the country under the window
python3 "$GMAP_ROOT/scripts/ref_library.py" get ref.boundary.us.county.cb500k
python3 "$GMAP_ROOT/scripts/catalog_search.py" "cell towers Arizona" --sources library,datagov,hub,socrata --limit 5     # the catalogs, library first
python3 "$GMAP_ROOT/scripts/catalog_search.py" "cell towers Arizona" --sources hub --find-record 1 > find.json           # result 1 as a draft record
python3 "$GMAP_ROOT/scripts/find_record.py" template > find.json     # or fill one in by hand, then:
python3 "$GMAP_ROOT/scripts/find_record.py" check find.json
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
python3 "$GMAP_ROOT/scripts/modules.py" read geo-catalog-search        # any module in the table
```

| Module                    | Read it |
|---------------------------|---|
| `geo-catalog-search`      | when the library has nothing that fits: data.gov, ArcGIS Online and Hub, Socrata, STAC |
| `geodata-sources`         | for the gotchas of the common public sources |
| `reference-library-guide` | for licence classes and how to read an entry's use_when and avoid_when |
| `ref-library`             | for the `ref_library.py select` and `get` options |
| `geo-service-explore`     | when a find is a live service: what it offers before anything is pulled |

## Order of work

1. **Say what is needed.** Geography, level (state, county, tract, site and so on), what the map does with it (`use_for`), the scale,
   who sees the map (`--audience internal|external|regulatory`). If one of these is missing and the answer would change, ask one question.
   Geography words (1.5.0): `world`; the UN M49 regions and sub-regions (`africa`, `sub-saharan-africa`, `southern-europe`, `eastern-asia`, ...);
   every ISO 3166-1 country by its two letters or its name (`ng` or `nigeria`, `jp`, `gr`; the United Kingdom is `uk`, with `gb` an alias);
   any ISO 3166-2 subdivision (`ng-la`, `gr-i`, `ca-on`, `us-az`), whose parent is its country; `north-america`; a US Census region
   `us/region/west`. A subdivision request also matches entries for its country, its region and `world`. With no geography, `--bbox` of the
   map's window takes the country under its centre (a Lagos window chooses world entries and Nigerian ones, never a US-only entry).
   Levels: `admin0` to `admin4` are the international names and match the older ones (`admin1` = `state`, `admin2` = `county`,
   `admin3` = `county-subdivision`); the country's own word for each is in `data/countries.yaml` (region, prefecture, province, ...).
2. **Ask the library.** `select` returns the best entry, the runners-up and, for each, why it ranked where it did. Exit 4 means
   "no reference entry fits"; exit 2 means a word is not in the vocabulary (the message lists the valid terms).
   Tell the person which entry fits and how it arrives (bundled, downloaded on first use, a service, or a file they supply).
   A `service` entry names a shipped profile (`delivery.profile`, such as `hifld-geoplatform`, `fema-nfhl`, `tigerweb`) and a layer:
   pull it with `geo-service-query` and that profile, no saving needed. A `user-supplied` entry's `delivery.note` says where to download it.
3. **Check the gates.** An entry is never offered if it is draft or blocked, if its license does not allow the audience, or if the task
   is in its `avoid_for`. Say so when a near-miss was held back, and name the reason in one phrase.
4. **No fit: ask the catalogs.** `catalog_search.py "<words>" --bbox=...` (or the `catalog_search` tool) searches data.gov, ArcGIS Online and Hub,
   Socrata and STAC at once. Read the top results with their license as the catalog states it and their route (a download, or an ArcGIS
   FeatureServer or WFS address). `--find-record N` drafts the record for one result (`confidence: medium`, a caveat that the publisher's page has not
   been read); open that page to fill in the resolution and the data's own vintage before presenting it. A service address in a result is never
   queried from here: `geo-service-explore` with `url=` does that after the person confirms the host.
5. **Still no fit: search the web.** See the recipe below. Only the publisher's own pages count as a source.
6. **Report.** A fit: the entry id, vintage, license and a ready-to-paste `source: {type: ref, id: ...}` layer. No fit: the find record.

## Web search recipe (only when the library and the catalogs have no fit)

- Search for the dataset name plus the publisher, then open the publisher's page. A mirror, a forum post or a blog is a lead, not a source.
- Read the page for: the exact product name, the vintage, the file format, the resolution or scale, the license or terms of use, and the download or API address.
- If the page cannot be read (blocked, login wall), say so and give the address for the person to open. Never fill in a license from memory.
- If the search found a **different** product than the one asked for (a lookalike), say so. Put what was asked for in `instead_of` and write a caveat.
- Never put a key, token or user name in a record. If the source needs a key, the route is `api` and the caveat says the person must supply one.
- Record `license_class` using the library words: public-domain, open, attribution, share-alike, restricted, licensed. When the terms are unclear, use `restricted` and say why.

## The find record

Fields: `dataset`, `publisher`, `vintage`, `license_class`, `format`, `resolution`, `route` (download | api | service | request | purchase),
`url` (https), `confidence` (high | medium | low), plus `caveat`, `instead_of`, `mirror_of`, `alt_urls` when they apply. Run `find_record.py check` before
presenting it; fix every BLOCK. A WARN (a host that does not look like the publisher, a low-confidence record without a caveat) is told to the person.

## Tell the person

Lead with the answer: the entry to use, or the dataset found. Then the license in plain words (can it go on a client slide? can it be
shared?), the vintage, and how to get it. One next step: build the map with it, or, for a web find, bring the file in as a user layer
(`geo-inspect` then `map-create`). A find worth keeping becomes a local draft library entry once its file is on disk: `/geo-mapping-acropolisdocs:reference-curate`
(`ref_library.py add --file FILE`) reads what it can verify, asks for the rest, and writes the draft into the local overlay. Offer that as the
second step, not the first; a draft is never chosen for a map until the person activates it.

## Data-source tools (1.3.0)

- **Historical boundaries:** `ref.boundary.world.historical.ohm` (OpenHistoricalMap, public domain unless a feature says otherwise):
  `hist_fetch` with a date, a box and an admin level; or the op `{as_of: 1861-10-24}` on a layer with start_date / end_date.
- **Census tables by variable:** `ref.statistic.us.acs5.table`, `.broadband` (B28002), `.computers` (B28001), `.median-income` (B19013):
  `census_query` (API with the person's census key, else the keyless Summary File), with MOEs and derived measures; or a MapSpec
  `join: {table: {library: ID, params: {level: tract, state: AZ}, derive: {...}}, left: GEOID, right: GEOID}`.
- **FCC broadband availability:** `ref.statistic.us.broadband.fcc-bdc-summary` with the person's fcc-bdc key: `fcc_bdc_list`, then
  `fcc_bdc_fetch summary=true` for one filing, geography, state, technology and speed tier.
- **Climate (Copernicus CDS):** the ERA5 entries with the person's copernicus-cds token: `cds_plan` (show the card), `cds_submit`,
  `cds_status`, `cds_fetch`; or the `climate-fetch` pipeline in map-run. The person accepts each dataset's licence on its page first.

## Boundaries and ground outside the United States

- **Admin-1 and admin-2 of any country**: `ref.boundary.world.country.geoboundaries`, fetched by country and level:
  `ref_fetch.py fetch ref.boundary.world.country.geoboundaries --variant nga --level adm2` (the ISO alpha-3 code). Only the gbOpen release, and
  only a country whose licence (read from each API record) is CC BY 4.0 or more open; the record keeps the source, licence and year for the credit.
- **Detailed land and water** for a city map anywhere: `ref.context.world.ground.overture` (read per window by `ground_detail.py prepare`), and the
  OSM simplified land for regional maps (`ref.context.world.land.osm-simplified`). Both ODbL.
- **Elevation** anywhere: `ref.terrain.world.dem.copernicus-glo30` (keyless, the default relief outside the US).
- **Population denominators** for a rate "per 1,000 people" outside the US: GHSL or WorldPop (`ref.statistic.world.population.ghsl`,
  `.worldpop`); the ACS is US-only.
