---
name: map-doctor
disable-model-invocation: true
description: >
  Check whether this computer can make maps: Python packages, GDAL drivers, capability tier, brand packs and the reference library, with an
  optional fix.
---

# map-doctor

Reports what geo-mapping-acropolisdocs can do here. HARD dependencies block maps; SOFT ones give a quietly worse result.
It also reports the single-edition check: only one `geo-mapping-*` plugin may be installed.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/doctor.py"                 # human report
python3 "$GMAP_ROOT/scripts/doctor.py" --json          # machine report
python3 "$GMAP_ROOT/scripts/doctor.py" --fix --all     # install what is missing (ask first)
python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick   # a fresh cloud workspace: what a map needs, one pip step, 10 minutes at most
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
python3 "$GMAP_ROOT/scripts/modules.py" read map-env-probe        # any module in the table
```

| Module          | Read it |
|-----------------|---|
| `map-env-probe` | for the capability tiers and the two-edition guard |
| `geo-cache`     | for what the plugin keeps on disk, and pruning |

Add `--device-bridge yes` when a call to the person's linked computer has already succeeded in this session, and `--mcp yes`
when a call to the plugin's MCP server has; the script cannot see the model's tool list, so it only knows what it is told.

The report ends with a **credential summary** (0.6.0): how many keys are registered, and any that are rejected, past their review
date, missing, readable by other accounts, not verifiable from here, a browser key without its domain restriction, or an old key file
still kept after a rotation. Names only, never a value. When the credential folder is not on this machine, the MCP server's
`credentials_status` tool gives the same summary from the person's computer. Point to `/geo-mapping-acropolisdocs:map-credentials` for any fix.

An **Engines and routes** section (0.24.0) says whether DuckDB has its spatial extension (large tables and GeoParquet), whether PROJ has
its datum grids (without them a NAD27 or older NAD83 layer is shifted by a coarser method, and the build's MAP-CRS-004 finding
names the grid that would fix it) and whether the plugin's
own data-service server can start here. `--fix --all` installs DuckDB's spatial extension; it does not download grids.

Read the report to the person in plain words. State the tier in one line, name anything HARD that is missing,
and offer `--fix --all`. Do not install anything unasked. Exit codes: 0 ready, 1 a HARD item is missing,
2 two editions are installed.
