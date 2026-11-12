---
name: map-create
description: >
  Make a branded print map (SVG, PDF, PNG) from a request or geodata: sites, choropleths, transit, terrain. Trigger on: make a map,
  map these sites, AcropolisDocs map, branded map.
---

# map-create

Turns a request into a finished map. One message in, three files out (SVG master, PDF, PNG), with the findings that matter.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/env_probe.py" --begin-build --spec map.yaml     # prints the build token; export GMAP_BUILD_TOKEN
python3 "$GMAP_ROOT/lints/mapspec_lint.py" map.yaml                       # spec check, before any data is read
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats svg,pdf,png [--strict]
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
python3 "$GMAP_ROOT/scripts/modules.py" read map-text        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `map-text`               | before writing the title, subtitle, tagline or a legend title |
| `cartography-principles` | when choosing the map type, the classes or what to leave out |
| `classify-and-color`     | for classification methods, class labels, and a count that needs dividing first |
| `color-accessibility`    | when choosing colours, or a colour or contrast finding needs a fix |
| `ref-library`            | for the `ref_library.py select` facets and what a library entry carries |
| `geodata-sources`        | for what the library holds and the gotchas of Census, TIGER, Natural Earth, FCC, HIFLD and OSM data |
| `geo-io`                 | when a file will not read, or needs a layer, encoding or size option |
| `crs-engine`             | when the data has no CRS or swapped coordinates, or the display projection is chosen by hand |
| `projections-guide`      | for which projection suits the extent and the job, and datum caveats |
| `telecom-geo`            | for a site list: columns, sectors, bands, status words, confidential sites |
| `site-confidentiality`   | before client site points go on a map for an external audience |
| `basemap-render`         | for a street or terrain tile background on a print map (prepared once; the build then reads it) |
| `tile-licensing`         | before naming any tile provider |
| `raster-data`            | for a GeoTIFF, hillshade or contour layer |
| `map-layout`             | when a layout finding moves or drops an element |
| `legend-builder`         | when a legend finding needs a fix |
| `label-engine`           | when labels collide, drop out or fall below the minimum size |
| `scalebar-northarrow`    | when the scale bar or north arrow is hidden or flagged |
| `map-brand`              | when the person names a brand, or a brand finding appears |
| `provenance-ledger`      | for the source line, the Data and Methods page, a provenance finding, or a file made from a library source (`provenance.library_entry`: its credit and any logo its terms require) |
| `map-pdf-publisher`      | when the PDF or PNG step fails, or needs a size or DPI option |
| `mapspec-reference`      | **before writing or changing a MapSpec**: every field, the roles and symbols, and worked examples |
| `transit-maps`           | for a transit map from a GTFS feed |
| `map-ground-global`      | for a metro or city map that needs detailed land and water, and for any map outside the United States |
| `map-build-options`      | for a layer over 50,000 features, side panels and the fit, or a style pack (antique, blueprint, editorial, monochrome, sketch) |
| `map-env-probe`          | when `env_probe.py` stops the build (tier, packages, two editions installed) |

Exit 0 built; 3 a BLOCK finding (nothing is withheld unless `--strict`, which deletes the files); 2 a request this release cannot do.

## Order of work

**A fresh cloud workspace has no map packages** (`python3 -c "import geopandas"` fails): run `python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick`
once, before anything else. It installs what a map needs in one pip step, wheels first, within 10 minutes (1.2.0); never `pip install -r
requirements-dev.txt`, which brings the test stack. On the person's own computer, ask before installing.

1. **Find the message.** A map says one thing. If the request does not, ask one short question ("What should the reader take from this map?") and
   nothing else. Write it as the MapSpec `message`: a full sentence, not "Map".
2. **Inspect before mapping.** Run `geo-inspect` on any uploaded file (CRS, geometry, fields). Do not map data that reports "do not map as is".
3. **Use the library first.** Ask `ref_library.py select --geography ... --level ... --use-for ... --scale ... --audience ...` for the layer that fits
   (the `ref-library` module has every option). A layer is used as `source: {type: ref, id: <entry id>}`. Say which entry was chosen and why in one phrase. Entries that are not
   `active` are never used. Exit 4 ("no reference entry fits") hands over to `geo-find-data`; ask for a file if the web has nothing usable. A table the person
   supplies is joined on the entry's join key: read the key column as text, keep leading zeros. A service entry (FEMA flood zones, HIFLD cellular towers,
   FCC CMAs, transmission lines) is drawn the same way: the build fetches it once for the map window through its shipped profile and keeps the capture, so a
   rebuild sends nothing; `where: "SFHA_TF = 'T'"` filters at the service. Venue, site or business addresses become points with
   `source: {type: geocode, table: venues.csv, address: [street, city, state, zip], purpose: venues}` (the `geocode-addresses` rules apply).
4. **Pick the map type from the data**, never from habit:
   - a rate or share per area → `role: choropleth` with `value_field` (normalized: per capita, per household, per sq mi, a percent);
   - a count per area → divide first, or show proportional symbols;
   - sites or events → `role: points`; a measure at each site → `role: proportional`; roads or paths → `role: lines`;
   - a grid of values (a GeoTIFF: signal strength, population per cell, a propagation model) → `role: raster`; terrain → `role: hillshade` or `contour`; building footprints (raised on a 3D web page) → `role: buildings`.
   **Routing note.** A plain site map (where the sites are, what kind each is) is built here. A site list with azimuth and band columns whose
   reader should see sector footprints goes to the coverage-map pipeline (`/geo-mapping-acropolisdocs:map-run coverage-map`): it draws the wedges, applies
   coordinate privacy and words them as sectors, not coverage. A tracker with a status per location goes to `/geo-mapping-acropolisdocs:map-run program-map`; a client table to roll
   up by state or county goes to `/geo-mapping-acropolisdocs:map-run market-map`; one map per market goes to `map-atlas`. Say which door you are taking in one phrase.
5. **Write the MapSpec** (read the `mapspec-reference` module first: it has every field and worked examples), lint it, then build. Show the PNG with a one-line caption, then the findings. Write the title and subtitle
   yourself by the `map-text` module (read it first): a subject-led title in Title Case (*Link Light Rail Walksheds, Seattle*), a subtitle that
   adds one thing (the key number, the scope or the measure); a finding-led title only when the person asks. Text the person supplies is drawn
   as written.
6. **Fix, do not argue.** A BLOCK finding is a rule the map breaks. Change the spec and rebuild. Overrides exist only for MAP-CART-001 and
   MAP-CART-010 and only with the person's own written reason (`override: {MAP-CART-001: "reason"}` on the layer).

## Rules the builder enforces (so do not work around them)

- **Colors come from the brand tokens only.** Never write a hex color or a color name. Use palette roles (`brand_sequential`, `brand_diverging`
  with a `midpoint`, `brand_categorical`, `brand_neutral`) or token references such as `@brand.accent`.
- **Data uses the whole brand palette, never the page or text colors.** Give each data layer its own palette color: `@brand.accent`,
  `@brand.categorical.0` to `.7`, or `@brand.status.success|warning|danger` for a status meaning. Or leave the color out: each line or point
  layer that names none gets the next palette color no other layer shows (named colors, class colors and the proportional accent are skipped),
  so no two layers share one. A `role: polygons` layer with no fill (or `symbol: {fill: none}`) is an outline: no fill, its edge in the next
  palette color at 0.4 mm, so relief and context show through (1.2.0). Color it with `symbol: {fill: "@brand.categorical.2", opacity: 0.4}` (inside `symbol`,
  not at the layer's top level); `symbol.stroke` names the edge (drawn at full strength even in the fill's color), `symbol.stroke_opacity` its strength,
  `symbol.dash: dashed | dotted | [1.2, 0.8]` dashes it (a tribal or planning boundary), and the legend swatch shows the same. A point layer with
  `symbol: {shape: none}` draws only its labels (names at chosen spots; no legend entry). `@brand.label`, `.background`, `.land`, `.water`, `.frame`,
  `.legend_bg`, `.legend_frame` and `.no_data` are for the page and text, not data (MAP-CART-015); in the dark theme `label` is white. Two layers
  whose colors cannot be told apart get MAP-CART-016, and a line or point layer weaker than 3:1 on the land gets MAP-A11Y-007: pick another palette color.
- **Inter is the only font.** Text is live and embedded.
- **No visible house credit.** The accreditation lives in file metadata only. A brand pack that requires a trademark line has it shown in the footer.
- **Every layer needs provenance**: a source, a license and a date (`retrieved_at`, `date` or `vintage`). A library layer supplies all three; a file the person brought needs
  `provenance: {source: ..., license: ..., retrieved_at: YYYY-MM-DD}`, and MAP-LIC-001 blocks the map without it. Ask the person for the source and license; never invent them.
  Client-confidential layers need the confidentiality banner on.
- **The library is checked against the audience.** A layer whose license does not allow the map's audience, or that is draft, blocked or retired, is a BLOCK (MAP-REF-002, -004).
- **No private individuals' locations**, no API keys and no internal host names in outputs.
- Legend text never goes below 6 pt (print) or 10 pt (slides); the builder says when a template is too small instead of shrinking it.

## Tell the person

Lead with the result: the PNG and one sentence on what the map shows. Then at most three findings that change what they would do (BLOCKs fixed, WARNs that
remain, a note when the legend or scale bar moved or was hidden and why). Then the map text: the title and subtitle drawn (`map_text` in the build
result) and every line of `map_text.callout`, as written ("Map text: N findings (not blocking)" and one line per finding with its suggested fix);
these never count against the three and never block. Change supplied text only when the person says so. Offer one next step: `map-restyle` for a look change, `map-export` for another size.
Files are named `<brand prefix><id>_<template>_v<YYYYMMDD>.<ext>`. A provenance sidecar (`.provenance.json`) is written beside the map; send it only if asked.
When the person wants a map they can pan, zoom and click, use `webmap-create` (one HTML file) instead of, or beside, these three files. When the person wants to show how the map was made, turn on `methods_page` and send the one-page Data and Methods file with the map.
