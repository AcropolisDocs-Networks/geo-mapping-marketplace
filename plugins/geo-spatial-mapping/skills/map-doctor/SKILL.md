---
name: map-doctor
disable-model-invocation: true
description: >
  Check whether this computer can make maps: Python packages, GDAL drivers, capability tier, brand packs and the reference library, with an
  optional fix.
---

# map-doctor

Reports what geo-spatial-mapping can do here. HARD dependencies block maps; SOFT ones give a quietly worse result.
It also reports the single-edition check: only one copy of this plugin may be installed.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/doctor.py"                 # human report
python3 "$GMAP_ROOT/scripts/doctor.py" --json          # machine report
python3 "$GMAP_ROOT/scripts/doctor.py" --fix --all     # install what is missing (ask first)
python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick   # a fresh cloud workspace: what a map needs, one pip step, 10 minutes at most
python3 "$GMAP_ROOT/scripts/doctor.py" --settings     # 3.7.0: your own folder, your sources, your brand and default, an edited shipped brand
python3 "$GMAP_ROOT/scripts/doctor.py" --move-settings --yes   # copy the settings found in this project into your own folder (ask first)
python3 "$GMAP_ROOT/scripts/doctor.py" --setup        # 4.0.0: the data server's setup on THIS computer: state, cause code, fix, log lines
```

**The data server's setup (4.0.0).** When the plugin's data tools are missing or failing in a Cowork session, call the data server's
`setup_status` tool: it runs on the person's computer and gives the state (ready, installing, failed, needs a restart), the cause code
(NO_PYTHON, NO_WRITE, PIP_CERT, PIP_TLS, NO_NETWORK, VENV_LOCKED, IMPORT, PIP) and its one-line fix, and the last lines of setup.log. Tell
the person the fix in plain words; the repair command it names reinstalls from nothing. After setup says Done, Claude must be restarted.

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read map-env-probe        # any module in the table
```

| Module          | Read it |
|-----------------|---|
| `map-env-probe` | for the capability tiers and the two-edition guard |
| `geo-cache`     | for what the plugin keeps on disk, and pruning |
| `regions`       | when `--grids` names a region |

Add `--device-bridge yes` when a call to the person's linked computer has already succeeded in this session, and `--mcp yes`
when a call to the plugin's MCP server has; the script cannot see the model's tool list, so it only knows what it is told.

The report ends with a **credential summary** (0.6.0): how many keys are registered, and any that are rejected, past their review
date, missing, readable by other accounts, not verifiable from here, a browser key without its domain restriction, or an old key file
still kept after a rotation. Names only, never a value. When the credential folder is not on this machine, the MCP server's
`credentials_status` tool gives the same summary from the person's computer. Point to `/geo-spatial-mapping:map-credentials` for any fix.

An **Engines and routes** section (0.24.0) says whether DuckDB has its spatial extension (large tables and GeoParquet), whether PROJ has
its datum grids (without them a NAD27 or older NAD83 layer is shifted by a coarser method, and the build's MAP-CRS-004 finding
names the grid that would fix it) and whether the plugin's
own data-service server can start here. `--fix --all` installs DuckDB's spatial extension (the matching wheel first, else DuckDB's own download) and checks it again in the same run.

**Places and profiles (2.4.0, 3.0.0).** The first line names where the doctor runs: your computer, the Cowork VM or the cloud workspace,
and whether a linked computer is reachable for data tools (tier T2 only on your own computer). Packages come in profiles, one pip call
each: `--fix --profile map|raster|h3|web|video|db|all` (`--fix --all` is `all`, and installs DuckDB's spatial wheel); each SOFT row says
its profile, and the **Fixed** section lists what was installed. `--grids <region>` lists the PROJ datum grids the region needs with their
size, `--fix --grids <region>` downloads them; `--fix --place cowork-vm` makes a Linux venv inside the work folder so the Cowork VM can
build; `--wheelhouse DIR` keeps the wheels for the next session (off unless asked). Where each step runs is in `data/execution-routes.yaml`.

Read the report to the person in plain words. State the place and the tier in one line, name anything HARD that is missing,
and offer `--fix --all`. Do not install anything unasked. Exit codes: 0 ready, 1 a HARD item is missing,
2 two editions are installed.
