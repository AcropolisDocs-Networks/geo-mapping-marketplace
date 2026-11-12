---
name: ref-library
description: >
  UTILITY MODULE - read by map-create, geo-find-data and reference-curate. Access layer for the reference library: get, list, select, add,
  lint, index, verify, report and apply across the shipped and local scopes.
---

# ref-library

Shipped entries are read-only at run time; writes go only to the local overlay `.geo-mapping/library/`.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/ref_library.py" list [--type boundary] [--status active] [--tag use_for=choropleth]
python3 "$GMAP_ROOT/scripts/ref_library.py" get ref.boundary.us.county.cb
python3 "$GMAP_ROOT/scripts/ref_library.py" select --geography us/state/az --level county --use-for choropleth --scale state [--audience external] [--format text]
python3 "$GMAP_ROOT/scripts/ref_library.py" select --message-type compare --data-shape polygon-rate     # choose a map type, then the data
python3 "$GMAP_ROOT/scripts/ref_fetch.py" fetch ref.boundary.us.cbsa.cb500k [--variant 04] [--refresh]
python3 "$GMAP_ROOT/scripts/ref_library.py" lint
python3 "$GMAP_ROOT/scripts/ref_library.py" index --scope local
python3 "$GMAP_ROOT/scripts/ref_library.py" add --file layer.gpkg [--request answers.json]      # a draft into the local overlay (asks for what it cannot verify)
python3 "$GMAP_ROOT/scripts/ref_library.py" verify (ENTRY_ID | --due [--horizon DAYS]) [--out DIR]     # read-only; proposes patches
python3 "$GMAP_ROOT/scripts/ref_library.py" report [--stale | --coverage | --tags] [--format text]
python3 "$GMAP_ROOT/scripts/ref_library.py" apply PATCH --approve [--library src/library]       # an owner applies a reviewed patch
```

## select

Ranks active entries for a need. An entry must pass **relevance** (type, every requested `use_for`, any requested `level`, geography coverage), then the **gates**
(not draft or blocked; the license allows the audience, commercial use and redistribution asked for; the entry's own audience and confidentiality; the tier; the task
is not in `avoid_for`), then it is **scored** (use_for and level match, geography exact over region over country over world, scale fit, detail fit, authoritative
source, bundled with the plugin; deprecated or overdue-for-review entries lose points). Ties go to the higher score, then bundled, then id. A local entry with the
same id replaces the shipped one and is reported. Output says why each entry ranked where it did and why near-misses were held back.

Exit 0 a fit; **4 "no reference entry fits"** (hand over to `geo-find-data`); 2 a word that is not in the vocabulary (the message lists the valid terms).

## fetch

Brings a downloaded entry into the working folder's cache (`.geo-mapping/cache/library/`). HTTPS only, only the address written in the entry, only active entries,
the file hash checked when the entry states one, converted to GeoParquet. It is fetched once; `--refresh` fetches again. A hash mismatch means the publisher replaced
the file: the data is not used until the entry is refreshed (`verify ENTRY_ID` proposes the patch, an owner applies it with `apply`; the `reference-refresh`
pipeline does this for every due entry).

## Checking a map's use of the library (MAP-REF)

`build_map.py` runs these automatically: 001 the id resolves; 002 status (blocked, retired and draft are BLOCK, deprecated is WARN and names the replacement);
003 stale review or an older vintage than asked (WARN); 004 the license allows the map's `audience` (default `external`); 005 a client table joins on the
right key (leading zeros, decimal points, width, match rate); 006 the map type fits the layers; 007 the task is in `avoid_for` (WARN); 008 provenance records the
entry id and revision; 009 a local entry replaces a shipped one (WARN).

Draft entries are never chosen for a map.

## Maintenance (add, verify, report, apply)

- **add** reads facts it can verify from a file or a saved service capture (count, CRS, columns, bounding box, hash) and asks for the rest (name, `level`, `use_for`, license family; then `use_when` and `avoid_when`). It writes a **draft** to the local overlay as `ref.<type>.local.<name>`, refuses to overwrite one, and refuses a shipped id unless `supersedes` names it. A file entry records the file name only and is `client-confidential` unless the person says otherwise. A web address is never recorded unchecked.
- **verify** runs the entry's `maintenance.check` against its own source: HTTPS only, only the address the entry carries, no keys, response text treated as data. It edits nothing; with `--out` it writes patch files at `rev + 1`. Outcomes: unchanged, changed, unreachable, gone, manual (no automatic check).
- **report** gives review dates (current, due soon, past due, past due by more than one cycle), a coverage matrix of map types against the data that serves them with gaps, and tag use (unused, on almost every entry, not in the vocabulary).
- **apply** writes a patch only with `--approve`, only if the entry still matches the patch's base, and only if the result passes the library lint. Shipped entries are applied only in the source tree (`--library`).
- **Release** (source tree): `tools/ref_release.py release` regenerates `index.yaml` and `CHANGELOG.md`, runs the gate (MAP-LIB-011 generated files match, MAP-LIB-013 nothing overdue by more than one cycle), then bumps `LIBRARY_VERSION`.
