---
name: market-map
description: >
  PIPELINE - started only through /geo-spatial-mapping:map-run, which reads this guide before planning. Market map: an admin-1 or admin-2 rollup (state or county, province or district)
  from a client table joined to Census boundaries, as a rate choropleth or proportional symbols.
---

# market-map (pipeline)

Started by `/geo-spatial-mapping:map-run market-map`. Never begin it because a map was mentioned; `map-create` is the conversational route.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/pipelines.py" plan market-map --table MARKETS.xlsx --option out=./maps [--option value=subscribers] [--option denominator=population] [--option per=1000] [--option states=AZ,NM] [--option style=choropleth|proportional] [--option sheet=Q3]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  market-map --table MARKETS.xlsx --option out=./maps [--option value=subscribers] [--option denominator=population] [--option per=1000] [--option states=AZ,NM] [--option style=choropleth|proportional] [--option sheet=Q3] --confirm TOKEN
```

## Flow

1. **Plan.** Show the cost line. Nothing is looked up or geocoded; positions and values come from the person's file.
2. **Confirm.** Run only with the token from that plan. A changed input changes the token: plan again.
3. **Stages.** probe the tier, read the table, repair keys, check normalization and write the MapSpec, lint the MapSpec, render with every lint, package, and write layer data only if asked and allowed.
4. **Report.** Give the folder, the BLOCK/WARN/INFO counts and the ingest findings in plain words. A BLOCK stops the run; name the rule and the fix.

The package also holds `mapspec.yaml` (the generated MapSpec, editable and re-buildable with `map-build`), `ingest_report.json`, the cleaned data the map was drawn from, `methods.md` and `manifest.json`.

## Rules

- A raw count as a choropleth stops the build (MAP-CART-001): big places look high because they are big. Give `denominator`, choose `style=proportional`, or have the person state in `rate_justification` why the value is already a rate. Do not write the justification for them.
- **Areas outside the US** (3.1.0): `--option by=admin1` joins ISO 3166-2 codes (DE-BY, FR-IDF) to the worldwide admin-1 table; `by=admin2 --option region=...` the admin-2 areas of a region (geoBoundaries, or US counties); `by=regions` the named regions; `by=h3` H3 cell ids. Without `by` the keys decide.
- **Areas** (1.0.0): 2-digit keys are states, 5-digit counties, 11-digit census tracts (the national tract file is fetched once into the library cache). Other areas come from a file: `--option boundary=FILE --option geography=cma` (the FCC CMA layer captured with `service_query.py`, key `CMA`), or any polygons with `boundary_key=COLUMN`.
- The legend and title name the denominator in words: a column called `pop`, `population` or `pop_2020_est` reads "per 1,000 people", `hh` reads "households", `housing_units` "housing units". Any other column name is shown with its underscores as spaces.
- Keys: a county table needs 5-digit FIPS (GEOID); a state table takes FIPS or postal codes. Dropped leading zeros are repaired and reported (MAP-JOIN-005). Duplicate keys stop the build (MAP-JOIN-002); counties missing from the table are drawn as No data (MAP-JOIN-004).
- The client table is credited in the source note ("Data: client table ...") and is client-confidential unless `confidential=false`.

