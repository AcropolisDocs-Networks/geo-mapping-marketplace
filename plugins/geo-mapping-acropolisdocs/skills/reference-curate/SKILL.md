---
name: reference-curate
disable-model-invocation: true
description: >
  Curate the map reference library: what it holds and when each entry is best, add a dataset or saved service layer as a local draft, check
  entries for staleness and gaps.
---

# reference-curate

The conversational front of the reference library. It reads the shipped library, reads and writes only the **local overlay** (`.geo-mapping/library/`), and never edits a shipped entry: a good local entry is exported and promoted by the maintainer in the source tree.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/ref_library.py" select --geography us-az --level county --use-for choropleth --scale state --format text   # what exists, ranked, with reasons
python3 "$GMAP_ROOT/scripts/ref_library.py" report --stale --format text        # past-due entries
python3 "$GMAP_ROOT/scripts/ref_library.py" report --coverage --format text     # map types against the data that serves them; gaps
python3 "$GMAP_ROOT/scripts/ref_library.py" add --file layer.gpkg [--request answers.json]         # a local draft from a file
python3 "$GMAP_ROOT/scripts/ref_library.py" add --capture CAPTURE_ID [--request answers.json]      # a local draft from a saved service layer
python3 "$GMAP_ROOT/scripts/ref_library.py" verify ENTRY_ID                      # one read-only check; shows the proposed patch, changes nothing
python3 "$GMAP_ROOT/scripts/ref_library.py" lint --scope local
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
python3 "$GMAP_ROOT/scripts/modules.py" read reference-library-guide        # any module in the table
```

| Module                    | Read it |
|---------------------------|---|
| `reference-library-guide` | before adding or reviewing an entry |
| `ref-library`             | for every `ref_library.py` option |
| `geodata-sources`         | for the gotchas of the public sources |
| `cartography-principles`  | for which map type each entry serves |
| `geo-cache`               | when library downloads need pruning |
| `reference-refresh`       | for every due entry at once, through `/geo-mapping-acropolisdocs:map-run reference-refresh` |

## Flow

1. **A question about what exists** ("do we have a boundary for ...", "what do we have for ...", "is it in the library"): run `select` with the facets you can read from the request and answer from its reasons. If it says nothing fits, say so and hand over to `geo-find-data`, which searches the web ("find ...", "where do I get ..." start there directly). Do not name a lookalike.
2. **Add an entry.** Run `add`. It reads what it can verify (feature count, CRS, columns, bounding box, file hash, or a capture's record) and **asks** for what only the person knows: a short name, what one feature is (`level`), the map types it serves (`use_for`), the license family, and then one concrete sentence each for when to use it and when not to. Put the answers in a small JSON file and run `add` again. The result is a **draft**: it is never chosen for a map until its status is `active`.
3. **A client's own data stays confidential by default.** A file entry is `client-confidential` and `internal`, records the file name (never its folder), and says so in the answer. Say that plainly; the person can state otherwise.
4. **Is it still current?** `report --stale` lists review dates. `verify ENTRY_ID` runs the entry's own check against its source (HTTPS, only the address in the entry, no keys) and shows what changed. For every due entry at once, offer the `reference-refresh` pipeline through `/geo-mapping-acropolisdocs:map-run`.
5. **Retire or replace.** Tell the person how: set `superseded_by` (old maps keep working with a warning) or `status: retired` (the entry stays so old maps can be explained). A shipped entry changes only in the source tree; a local one is a YAML file they can edit, followed by `lint --scope local`.

## Geography and shipped services

Geography terms are `world`, `north-america`, `us`, `ca`, `mx`, `us-<state>` (us-az, us-ca, us-tx ...), `ca-<province>` (ca-on, ca-qc, ca-bc, ca-ab ...) and the Census regions `us/region/<name>`; full names (`arizona`, `ontario`) and the old `us/state/az` spelling are aliases. A draft entry for a local dataset uses the narrowest term that covers it.
Entries delivered as `service` point at a shipped, read-only service profile (TIGERweb, FCC, HIFLD on GeoPlatform, USGS, NWS, FEMA NFHL, NTIA, BLM, MRLC and five state portals); `service_profile_list` shows them with `shipped: true`, and a profile of the same name saved in the working folder overrides one.

## Never

Edit a shipped entry; write a URL into an entry that has not been checked (an entry with an address needs `url_verified_on`); activate a draft whose `use_when` and `avoid_when` the person has not read; send a local entry or client table anywhere; guess a license.
