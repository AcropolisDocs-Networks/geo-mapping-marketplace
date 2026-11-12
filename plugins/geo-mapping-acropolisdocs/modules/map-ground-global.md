---
name: map-ground-global
description: >
  KNOWLEDGE MODULE - read by map-create. Detailed land and water on metro and city maps anywhere (the ground
  providers and their preparation), and maps outside the United States: units, dates, national projections, locators and local names.
---

# map-ground-global

## Detailed land and water on metro and city maps, anywhere

The ground comes from the finest provider whose coverage holds the **whole** window at the map's scale (`data/ground-providers.yaml`), and the
build says which and why (MAP-GND-001):

| Provider | Where | Scales |
|---|---|---|
| `us-tiger` | a window wholly inside the United States | finer than 1:1,500,000 (TIGER/Line land and water, public domain) |
| `global-overture` | anywhere | finer than 1:1,500,000 (the Overture base theme's land and water, OSM-derived, ODbL) |
| `global-osm-simplified` | anywhere | 1:10,000,000 to 1:1,500,000 (the OSM simplified land polygons with the Natural Earth lakes, ODbL) |
| `bundled-ne` | anywhere | always there (Natural Earth) |

**Rule: land and water always come from one source at one resolution** (MAP-CART-024 blocks a map that mixes them). So a Seattle and Vancouver
window, which is not wholly inside the US, takes Overture for all its ground, never TIGER on one side and OSM on the other; the context pack's
Natural Earth lakes and rivers stay off detailed ground.

```bash
python3 "$GMAP_ROOT/scripts/ground_detail.py" status  --spec map.yaml      # which provider the map takes, and what is not cached yet
python3 "$GMAP_ROOT/scripts/ground_detail.py" prepare --spec map.yaml      # once per area: TIGER files in the US, Overture z8 tiles elsewhere
```

Run `prepare` before the first build of a metro or city map (it says when the map is too small-scale to need it). The build never goes online:
until the files are cached it draws the next provider and says so (MAP-GND-002). Overture is read by HTTPS range requests for the window's
row groups only, capped at 300 MB a window (MAP-GND-003 when over), from a pinned release (MAP-GND-004 when the release is past its review
date). The ODbL credit goes in the source note; a web page or data export that carries the ground gets the share-alike notice.
`basemap: {ground_detail: false}` keeps the bundled ground; `basemap: {ground: global-overture}` names a provider.

**The locator** (`elements.inset_map.extent: auto`, the default) fits the place: a map inside one US state gets the state locator; a city or
regional map elsewhere gets its **admin-1 locator** (the region, province or prefecture with its neighbours, named in small capitals, the word
from `data/countries.yaml`: "inside Attiki (a region of Greece)"); a map larger than one unit gets its country's locator; larger still, the world.
**Rule: the locator inset's north is the main map's north** (MAP-CART-025 blocks a mismatch): the inset is drawn in the main map's own
projection, so a meridian leans the same way on both; a world-scale or polar map, which has no single north, is exempt.

## Maps outside the United States

Nothing assumes the United States; the country under the map's centre decides (`data/countries.yaml`, MAP-LOC-001 says what was chosen):

- **Units**: the scale bar is in miles in the US, Liberia and Myanmar and in kilometres everywhere else (Canada and Mexico included). A window
  across countries with different units keeps the centre's and says so (MAP-LOC-002). `elements.scale_bar.units` overrides.
- **Dates**: `locale: auto` gives "10 November 2026" outside the US, Liberia and Myanmar; `en-US`, `en-GB` or `iso` (2026-11-10) as asked.
- **Paper**: `template: auto` takes Letter in the Letter countries (US, Canada, Mexico, the Philippines, ...) and A4 elsewhere.
- **Projection**: a map inside one country with a national grid takes it (`data/national-crs.yaml`: British National Grid, Lambert-93, the Greek
  Grid, JGD2011 UTM in Japan, ...); a map across Europe takes ETRS89-LAEA (EPSG:3035). `crs: {prefer: auto}` keeps the generic rule, `prefer:
  EPSG:2100` names one.
- **Names**: `names: {language: auto}` gives local names in Latin, Greek and Cyrillic scripts (Αθήνα, München) and English names otherwise;
  `local`, `en` or a language code (`ja`, `ar`) as asked, and `second_line: local | en` adds the other form under each name. Names in Arabic,
  Hebrew, Devanagari, Chinese, Japanese, Korean and other scripts Inter lacks are drawn in their Noto face, shaped and outlined, once
  `doctor.py --fonts arab,hebr,deva,jpan` has fetched the pinned fonts; until then they are in English, with MAP-LOC-003 naming the command.

A sites map in Athens:

```yaml
template: auto                           # A4 landscape in Greece
locale: auto                             # 10 November 2026
names: {language: auto, second_line: en} # Αθήνα with Athens under it
terrain: {on: true}                      # Copernicus GLO-30
elements: {inset_map: {on: true}}        # the Attiki locator
layers:
  - id: sites
    source: {type: file, path: athens_sites.csv, x: lon, y: lat, crs: "EPSG:4326"}
    role: points
    legend: {title: "Candidate sites"}
    provenance: {source: "Client site list", license: "client-confidential", retrieved_at: "2026-11-10"}
```

Then `ground_detail.py prepare --spec map.yaml` once (the Overture coast of the Saronic Gulf), and build: the Greek Grid (EPSG:2100), kilometres.
