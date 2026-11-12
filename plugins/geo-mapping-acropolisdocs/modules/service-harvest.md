---
name: service-harvest
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Service harvest: capture several
  layers of one public data service after a size estimate the person confirms.
---

# service-harvest (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run service-harvest`, which shows the cost and asks for the confirmation token first. The commands below are what the pipeline runs; use them directly only inside a run.

For a public service the plugin can reach itself. A service behind a login goes through `service-request-kit` instead, one layer at a time.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/service_harvest.py" --profile P --layers a,b,c [--cql ...] [--bbox=...] [--properties ...] [--max-features N]
python3 "$GMAP_ROOT/scripts/service_harvest.py" --profile P --layers a,b,c --confirm-total N      # after the person has seen the size
python3 "$GMAP_ROOT/scripts/service_harvest.py" --profile P --layers a,b,c --yes --refresh-existing [--apply]
```

Where the shell cannot reach the service (the desktop app), the plugin's server does the same as the `service_harvest` tool: the first call (no `confirm`) returns the estimate; after the person agrees, call it again with `confirm=true` and `confirm_total=` the total they agreed to. Pass `work_folder=` so the captures land in the person's project folder.

## Flow

1. **Estimate first.** The first run fetches no features. It asks the service how many each layer would bring and reports each layer, the pages, the total, and which layers already have a capture.
2. **Say the size.** "Three layers, about 48,200 features in 25 pages." Name any layer that cannot be requested (not in the service, field missing) and any whose count the service does not report: those need `--max-features`.
3. **Confirm.** Only after the person agrees, run again with `--confirm-total N` (the estimate or more) or `--yes`. A confirmed number below the estimate does not go ahead.
4. **Limits.** A harvest takes at most 40 layers and 500,000 features in all (the profile or `--max-total` can lower that). Over the limit it stops before fetching: narrow the request or split it.
5. **Result.** One capture per layer (`ok`, `kept`, `failed`), the number received against the number expected, a note for any capture the service cut short, and an index file for the run. Report capture ids and counts, not rows.
6. **Repeat runs.** A layer already captured with the same request is kept as it is. `--refresh-existing` asks again through `service-refresh` and reports what changed; `--apply` switches to the new capture.

## Never

Retry after a 401 or 403 (the whole harvest stops); ask for a credential; hide a layer that failed or a capture that is incomplete; fetch before the person has agreed to the size.
