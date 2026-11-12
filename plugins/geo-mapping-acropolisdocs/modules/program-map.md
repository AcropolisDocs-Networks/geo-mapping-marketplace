---
name: program-map
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Program map: status of locations from
  a tracker export, five statuses each with its own shape as well as color, positions from the tracker or a venue coordinates table.
---

# program-map (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run program-map`. Never begin it because a map was mentioned; `map-create` is the conversational route.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/pipelines.py" plan program-map --tracker TRACKER.csv [--coordinates VENUES.csv] --option out=./maps [--option name=Venue] [--option status=Status] [--option labels=true] [--option title="..."]
python3 "$GMAP_ROOT/scripts/pipelines.py" run  program-map --tracker TRACKER.csv [--coordinates VENUES.csv] --option out=./maps [--option name=Venue] [--option status=Status] [--option labels=true] [--option title="..."] --confirm TOKEN
```

## Flow

1. **Plan.** Show the cost line. Nothing is looked up or geocoded; positions and values come from the person's file.
2. **Confirm.** Run only with the token from that plan. A changed input changes the token: plan again.
3. **Stages.** probe the tier, read the tracker, map status words and place each row, then write the MapSpec, lint the MapSpec, render with every lint, package, and write layer data only if asked and allowed.
4. **Report.** Give the folder, the BLOCK/WARN/INFO counts and the ingest findings in plain words. A BLOCK stops the run; name the rule and the fix.

The package also holds `mapspec.yaml` (the generated MapSpec, editable and re-buildable with `map-build`), `ingest_report.json`, the cleaned data the map was drawn from, `methods.md` and `manifest.json`.

## Rules

- Statuses: Complete (circle), On track (square), At risk (triangle), Delayed or blocked (diamond), Not started (cross). An unrecognized word is drawn as No data and flagged (MAP-TEL-041); show the person the words and let them decide, do not guess.
- Positions come from the tracker's lat and lon columns or the coordinates table, matched by normalized name. Rows with no position are listed, not geocoded (MAP-TEL-040).
- Program maps are public unless `confidential=true`. Ask before treating a client tracker as public.

