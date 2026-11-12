---
name: map-env-probe
description: >
  UTILITY MODULE - read by map-create and map-doctor. Detects the capability tier, packages, GDAL drivers and shell egress, guards against
  two geo-mapping editions, and starts a map build.
---

# map-env-probe

Runs first in every map pipeline. Writes `env.json` to the working directory and prints a one-line status.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/env_probe.py" --device-bridge <yes|no|unknown> --web-tools <yes|no|unknown>
python3 "$GMAP_ROOT/scripts/env_probe.py" --begin-build --spec map.yaml    # prints GMAP_BUILD_TOKEN=...
```

- **Single-edition guard.** The script lists sibling plugin directory NAMES that match `geo-mapping-*`. If another
  edition is present it exits 2 with one line naming both; every map skill then stops.
- **Device bridge and web tools** are facts about the model's tool list. Pass `yes` only after a call has succeeded.
- **Build token.** `map-brand` answers only with a MapSpec and a current token; `--begin-build` creates it.
