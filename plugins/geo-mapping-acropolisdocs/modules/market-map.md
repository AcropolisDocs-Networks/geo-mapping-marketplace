---
name: market-map
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Market map: a state or county rollup
  from a client table joined to Census boundaries, as a rate choropleth or proportional symbols.
---

# market-map (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run market-map`. Never begin it because a map was mentioned; `map-create` is the conversational route.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
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
- **Areas** (1.0.0): 2-digit keys are states, 5-digit counties, 11-digit census tracts (the national tract file is fetched once into the library cache). Other areas come from a file: `--option boundary=FILE --option geography=cma` (the FCC CMA layer captured with `service_query.py`, key `CMA`), or any polygons with `boundary_key=COLUMN`.
- The legend and title name the denominator in words: a column called `pop`, `population` or `pop_2020_est` reads "per 1,000 people", `hh` reads "households", `housing_units` "housing units". Any other column name is shown with its underscores as spaces.
- Keys: a county table needs 5-digit FIPS (GEOID); a state table takes FIPS or postal codes. Dropped leading zeros are repaired and reported (MAP-JOIN-005). Duplicate keys stop the build (MAP-JOIN-002); counties missing from the table are drawn as No data (MAP-JOIN-004).
- The client table is credited in the source note ("Data: client table ...") and is client-confidential unless `confidential=false`.

