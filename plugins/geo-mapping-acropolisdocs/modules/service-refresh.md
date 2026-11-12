---
name: service-refresh
description: >
  PIPELINE - started only through /geo-mapping-acropolisdocs:map-run, which reads this guide before planning. Service refresh: ask a service again
  for a capture and report what changed; apply only on request.
---

# service-refresh (pipeline)

Started by `/geo-mapping-acropolisdocs:map-run service-refresh`, which shows the cost and asks for the confirmation token first. The commands below are what the pipeline runs; use them directly only inside a run.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/service_refresh.py" list [--older-than DAYS]
python3 "$GMAP_ROOT/scripts/service_refresh.py" refresh CAPTURE_ID [--key FIELD] [--threshold PCT] [--accept-over-cap]
python3 "$GMAP_ROOT/scripts/service_refresh.py" apply CAPTURE_ID
python3 "$GMAP_ROOT/scripts/service_refresh.py" compare OLD NEW [--key FIELD]
```

Where the shell cannot reach the service, the plugin's server's `service_refresh` tool does steps 2 to 4: `service_refresh(capture=ID)` stages the new capture and returns the drift report; `apply=true` only after the person says so.

## Flow

1. **List.** `list` shows each capture's age, route, size and whether it can be refreshed. A capture made through a request kit cannot (the plugin cannot reach that service).
2. **Refresh.** `refresh` repeats the recorded request, writes the new capture to a staging folder and a drift report next to it. **Nothing a map uses has changed yet.**
3. **Tell the person what changed**, in words, from the report's `summary`: new or lost fields (MAP-SVC-011 warns), the count (warns at 10% by default), the extent, and, when the layer has a stable key, rows added, removed and changed. Without a key, the shift of each numeric average. Say "no change" when the report says so.
4. **Apply only when they say so.** `apply CAPTURE_ID` (or `refresh --apply`) keeps the earlier capture in `_history/` and puts the new one in its place; every map that names the capture id uses the new data on its next build. Remind them to look at maps that used a field that was removed or retyped.
5. **An approved run (device-run capture).** Plan the same run again with `approved_run.py` (see `service-request-kit`). If the person gave a standing approval and the query has the same host, layer and filter shape, `command` issues it without a new question and says it is running under that approval; otherwise show the run card and ask. Probe first, run, `finish` under a new name, then `compare OLD NEW`.
6. **Your own kit.** For a user-run capture: have them run the same kit again, ingest the new folder under a new name (`service-request-kit`), then `compare OLD NEW`.

## Never

Apply without the person's go-ahead when the report has warnings; delete the earlier capture; retry after a 401 or 403; call a capture refreshed when the new one is marked incomplete without saying so.
