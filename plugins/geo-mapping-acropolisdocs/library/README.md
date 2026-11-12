# Reference library

Descriptions and recipes for the data, styles and map types this plugin can use, with a controlled vocabulary (`tags.yaml`) and entry types (`types.yaml`). It holds no client data, credentials or internal hostnames (MAP-LIB-009).

- **Read:** `scripts/ref_library.py select` ranks entries for a need and says why; `get` and `list` show entries.
- **Extend (anyone):** `ref_library.py add` writes a draft into the local overlay (`.geo-mapping/library/`). It asks for what it cannot verify.
- **Maintain (source tree):** `tools/ref_verify.py` and `ref_report.py` check and report; the `reference-refresh` pipeline proposes patches; `ref_library.py apply --approve --library src/library` applies a reviewed one; `tools/ref_release.py release` regenerates `index.yaml` and `CHANGELOG.md`, runs the release gate and bumps `LIBRARY_VERSION`.
- **Generated, do not edit:** `index.yaml`, `CHANGELOG.md`.
- **Services:** an entry with `delivery.mode: service` names a shipped, read-only service profile (`delivery.profile`, a file in `profiles/` beside `library/`) and a layer path under it; `service_query.py fetch --profile P --layer L` pulls it. A working-folder profile with the same name overrides the shipped one.

## Geography vocabulary (tags.yaml version 2, M13)

The `geography` facet is a hierarchy, and every value names its `parent`:

| Level | Values | Aliases accepted |
|---|---|---|
| World | `world` | |
| Continent | `north-america` (members `us`, `ca`, `mx`) | `na`, `north america` |
| Country | `us`, `ca`, `mx` | `usa`, `united states`, `canada`, `mexico`, `méxico` |
| US state | `us-<postal>` for the 50 states, DC and Puerto Rico (`us-az`, `us-ca`, `us-tx` ...) | `us/state/<postal>` (the spelling before M13), the state's name in lower case (`arizona`) |
| Canadian province or territory | `ca-<postal>` for all 13 (`ca-on`, `ca-qc`, `ca-bc`, `ca-ab` ...) | `ca/province/<postal>`, the name in lower case (`ontario`, `québec`) |
| US Census region | `us/region/northeast`, `midwest`, `south`, `west`, and the informal `southwest`, each with a `members` list of postal codes | |

How selection reads it (`scripts/ref_select.py geo_cover`): an entry covers a request when its geography is the same value (exact), a region whose
members include the requested state (region), the requested value's country (country), `north-america` above it (continent) or `world`. Those
rank in that order. An entry narrower than the request never covers it: a `us-az` entry does not answer a `us` request, and a `us` entry does not
answer `ca-on`. Entries and requests may use any alias; both are resolved to the canonical value before matching. Mexican states have no value
yet: a Mexican request uses `mx`.

## What an entry needs

An entry needs: tags from the vocabulary, a `use_when` and an `avoid_when` that are concrete, a license class consistent with its flags, the join key and CRS, a URL only with `url_verified_on`, an owner, `reviewed_on` and `verify_by` within the type's cadence, and a changelog line for its current `rev`.

## Promoting a local entry: a worked example (0.25.0)

A person's draft becomes a shipped entry only in the source tree, by the maintainer. Say a project described its market areas file:

1. **The person's draft.** In their project, `ref_library.py add --file markets.gpkg --request answers.json` reads the file (geometry, CRS,
   columns, extent, feature count, hash), takes the answers it cannot read (title, use_when, avoid_when, licence class) from the request, runs
   the library lint and writes `.geo-mapping/library/entries/data/<id>.yaml` as a `draft` in the local scope. `ref_library.py get <id> --scope local`
   shows it; it is used by that person only.
2. **Copy it into the source tree** as `src/library/entries/data/<id>.yaml` (`style/` or `method/` for those families). Remove the
   `_path` and `_scope` keys if the export carried them.
3. **Read what the draft could not.** The publisher's licence and terms page (record what was read and when in the changelog line), the
   download address with `url_verified_on`, `source.file_sha256` of the publisher's archive, the vintage and cadence. Fill
   `maintenance.owner`, `reviewed_on`, `verify_by` (within the type's cadence) and a `maintenance.check` (`http_head`, `http_get` or
   `feature_count_and_hash` with the expected `features`), so a refresh can verify it without a person.
4. **Decide the delivery.** `bundled` when the plugin ships the payload (the file under `src/assets/`, its `path` and `sha256`; only when the
   licence allows redistribution); `fetch` when each person downloads it once into their cache; `user-supplied` for licensed data.
5. **Status and revision.** `status: active` only when steps 3 and 4 are done; otherwise it stays `draft` and says what was not read.
   `rev: 1` with a changelog line for a new id; a new revision of an existing shipped id instead when the draft improves one.
6. **Check and release the library.** `python tools/ref_release.py check --today YYYY-MM-DD` (MAP-LIB-011 and 013 block), then
   `python tools/ref_release.py release --today YYYY-MM-DD`, which regenerates `index.yaml` and `CHANGELOG.md` and bumps `LIBRARY_VERSION`.
7. **Build and gate.** Build the editions, run the library tests and `tools/release_gate.py --steps library,editions,...`, then release the
   plugin with a new version.
8. **Tell the person to delete their local draft** once they have the new release: a local entry with the same id is read before the
   shipped one, so their old draft would keep standing in for it.
