---
name: map-create
description: >
  Make a branded print map (SVG, PDF, PNG) from a request or geodata: sites, choropleths, transit, terrain. Trigger on: make a map,
  map these sites, AcropolisDocs map, branded map.
---

# map-create

Turns a request into a finished map (SVG master, PDF, PNG). **Every map takes one road**, and each step prints the rules for the next, so
the rules arrive with the work: brief, sources, data, compose, draft, build, review.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/gmap.py" brief --region "Tempe, Arizona" --message "<one sentence: what the reader should take away>" \
        --intent site-map [--need "cell towers" ...] [--ask "theme=dark | <the person's own words>" ...] [--title "..."] [--computer linked|none]
python3 "$GMAP_ROOT/scripts/gmap.py" sources [--count]     # every library source for each need; then: gmap.py choose <need> <entry id> --why "<one phrase>"
python3 "$GMAP_ROOT/scripts/gmap.py" data plan             # how each source comes in; data add | file | derive | step record every layer
python3 "$GMAP_ROOT/scripts/gmap.py" compose               # writes map.yaml from the defaults, the intent, the brief, the asks and the data
python3 "$GMAP_ROOT/scripts/gmap.py" set <key>=<value> [--layer <id>] [--asked "<their words>"]     # a standing default moves only on the person's word
python3 "$GMAP_ROOT/scripts/gmap.py" draft                 # lint, two small pictures, the readings, and the cold-read request for a fresh agent
python3 "$GMAP_ROOT/scripts/gmap.py" build [--formats svg,pdf,png]
python3 "$GMAP_ROOT/scripts/gmap.py" review                # how the map was made, in numbers
```

`gmap.py intents` lists the kinds of map (site-map, choropleth, network, terrain, transit, coverage, comparison, story, explore, art);
`gmap.py rules <step>` prints a step's rules again; `gmap.py status` says where a job stands. Exit 0 done; 2 a request it cannot carry out as given;
3 a BLOCK finding; 4 stop and ask the person (a source needs their computer, a need has no source, a default would change without their word).

## How to walk it

- **Start with the brief, always.** One message, a full sentence; if the request has none, ask one short question ("What should the reader
  take from this map?") and nothing else. Write an `--ask` only for what the person said, in their words. `--intent art` is the one way to
  take the references away in one step.
- **Take sources from the sheet.** It lists every source the library holds for what the thing is, with licence, key and where it can be
  fetched from. When it says to compare, count first. When the best source needs the person's computer and none is linked, the job stops:
  ask them; never swap the source yourself. Answer each "also available here" line (add or decline).
- **Bring data through the door.** A library layer is fetched by the build or by the data tool the plan names. Shape data with an op
  (`data derive`); when no op does it, write the script and run it with `data step`, so its inputs, outputs and provenance are recorded.
  Never fetch with curl or draw with a script of your own. Run `geo-inspect` on a file the person brought.
- **Compose, then choose what is yours**: the map type from the data, the classes, each layer's palette colour, the labels, the padding.
  What the MapSpec does not say, the builder decides: do not write a position, a theme, a look or an on/off switch nobody asked for.
  A default changed with no ask is drawn at its default and said in one line.
- **Look before you say.** Open the draft and the mute picture and say what the map shows; act on a reading that contradicts the message
  (`message-check`). The cold read is made by a fresh agent that sees only the picture: never answer it yourself.
- **Fix, do not argue.** A BLOCK is a rule the map breaks: change the MapSpec and draft again. Overrides exist only for MAP-CART-001 and
  MAP-CART-010 and only with the person's own written reason.

A MapSpec the person already has builds directly; the build still counts its deviations from the defaults, and reports them without
putting them back. The whole result is always in `out/<stem>.result.json`.

```bash
export GMAP_BUILD_TOKEN=$(python3 "$GMAP_ROOT/scripts/env_probe.py" --begin-build --spec map.yaml --value)
python3 "$GMAP_ROOT/lints/mapspec_lint.py" map.yaml
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats svg,pdf,png --digest [--draft] [--strict]
```

**A fresh cloud workspace has no map packages**: the build's first line installs them. Elsewhere run `doctor.py --fix --quick` once, first;
never `pip install -r requirements-dev.txt`. On the person's own computer, ask before installing. In Cowork call `user_settings`
(`op=bring`, `work_folder=<project>`) once per folder first.

**Other doors.** A site list with azimuth and band columns whose reader should see sector footprints goes to
`/geo-spatial-mapping:map-run coverage-map`; a tracker with a status per location to `/geo-spatial-mapping:map-run program-map`; a client table to
roll up by admin area (state, county, province, district) to `/geo-spatial-mapping:map-run market-map`; one map per market to `map-atlas`;
a map to pan, zoom and click to `webmap-create`; a map told in steps is finished here, then `map-story`. Say which door you take in one phrase.

## Modules

These are this plugin's reference pages, not skills. A step's rules name the pages to read; read one with the line below. The Skill tool
cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read map-text        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `map-text`               | before writing the title, subtitle, tagline or a legend title |
| `message-check`          | **before looking at a draft**, and when a build lists Message check findings |
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
| `provenance-ledger`      | for the source line, the Data and Methods page, a provenance finding, or a file made from a library source (its credit and any logo its terms require) |
| `map-pdf-publisher`      | when the PDF or PNG step fails, or needs a size or DPI option |
| `mapspec-reference`      | **before writing or changing a MapSpec**: every field, the roles and symbols, and worked examples |
| `transit-maps`           | for a transit map from a GTFS feed |
| `map-ground-global`      | for a metro or city map that needs detailed land and water, and for any map outside the United States |
| `map-build-options`      | for a layer over 50,000 features, panels and the fit, a style pack, or a 3.7.0 line (stack, strip, Socrata, Spilhaus) |
| `map-env-probe`          | when `env_probe.py` stops the build (tier, packages, two editions installed) |
| `regions`                | **for every map**: the `region:` block |
| `h3-grids`               | for data on the H3 grid (FCC mobile hexagons) |

## Rules the builder enforces (so do not work around them)

- **Colors come from the brand tokens only**: palette roles (`brand_sequential`, `brand_diverging` with a `midpoint`, `brand_categorical`,
  `brand_neutral`) or token references such as `@brand.accent`, `@brand.categorical.0` to `.7`, `@brand.status.success|warning|danger`.
  Never a hex color or a color name. Page and text tokens (`@brand.label`, `.background`, `.land`, `.water`, `.frame`) are not for data.
- **Every layer needs provenance**: a source, a license and a date. A library layer supplies all three; the job records them for a file
  the person brought (ask them for the source and license; never invent them). MAP-LIC-001 blocks a map without it.
- **The library is checked against the audience**: a layer whose license does not allow the map's audience, or that is not `active`, is a BLOCK.
- **No private individuals' locations**, no API keys and no internal host names in outputs.
- **Inter, or the brand pack's own licensed typeface.** No visible house credit. Legend text never goes below 6 pt (print) or 10 pt (slides).

## Tell the person

Lead with the result: the PNG and one sentence on what the map shows. Then the deviations line (what was asked, what the intent set,
anything put back to its default). Then at most three findings that change what they would do. Then the map text and the message check,
every callout line as written; these never block. When the build's summary says relief was left out, say that line first. Change supplied
text only when the person says so. Offer one next step: `map-restyle` for a look change, `map-export` for another size. From the cloud
workspace hand back only the map files. Files are `<brand prefix><id>_<template>_v<YYYYMMDD>.<ext>`, with a `.provenance.json` beside them.
