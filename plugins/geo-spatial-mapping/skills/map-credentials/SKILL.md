---
name: map-credentials
disable-model-invocation: true
description: >
  Keep your own API keys for map data services in a private folder on your computer: services, status, add, check, rotate, remove, audit.
  Never asks for a key in chat.
---

# map-credentials

Each person uses their own keys. A key lives in one private file in the person's credential folder on their own computer
(`%USERPROFILE%\.geo-mapping\credentials\` on Windows, `~/.geo-mapping/credentials/` elsewhere), with a registry `credentials.yaml` that holds
names and metadata only. Maps and profiles name a key as `cred:NAME` (for example `cred:census`), never by value. Full rules: the
`credential-broker` module.

## The conversation rules (MAP-CRED-007)

- **Never ask for a key, token or password.** The person pastes the key into the file in their own editor (Notepad on Windows).
- **If a person pastes a key into the chat:** do not use it, do not repeat it, do not put it in a file. Say, in one or two sentences, that the key
  is now in this conversation and should be treated as exposed: get a new key from the service and run `rotate`. Then carry on with `add` or `rotate`.
- Show names, hosts, dates, yes/no answers and file paths. Never the content of a credential file.

## Which services the plugin takes a key for (3.5.0)

`credentials_services` (CLI `services`) lists every service in the plugin's library that takes the person's own key: the name to give `add`,
the other names `add` also accepts (`meteofrance` for `meteo-france`, `ofcom` for `ofcom-mobile`, `earthdata-lpdaac` for `earthdata`), host,
how the key is sent, sign-up address and free limits. `include_keyless=true` adds the active entries that need no key.

- **Never tell the person a service is not in the plugin without this list.** When `add NAME` answers MAP-CRED-013 "not a service the library
  knows", read its "did you mean" names and the list, and run `add` again with the library's name. Ask for a host and how the key is sent only
  for a service that is truly not listed.
- An alias is registered under the library's own name (MAP-CRED-015): say which name it was registered as, because maps use `cred:NAME`.
- A key marked `optional` (TfL) is used when registered; the service also answers without it at a lower limit.
- A key list kept elsewhere (a tracker page) is kept in step from this list, not from release notes.
- 3.7.0: keys for the person's **own** data sources (`/geo-spatial-mapping:map-sources`) are listed apart, under `your_sources`: the plugin does not support or vouch
  for those services. `add NAME` finds a name from the person's own sources as well as the library's.

## Which route

1. **Host-side (preferred).** The plugin's MCP server runs on the person's computer and keeps the folder there. Use its tools:
   `credentials_status`, `credentials_add`, `credentials_check`, `credentials_rotate`, `credentials_remove`, `credentials_audit`, and `ref_fetch`
   for a library fetch that needs the key. The folder is never connected to the session, so neither the chat nor the shell can read the key.
   When the map is built in a connected folder (this session's shell or a cloud session), pass `ref_fetch` that folder's path on the person's
   computer as `work_folder=` (for example `C:\temp\my-project`): the table goes to its `.geo-mapping/cache/library/`, where the map build
   finds it without the key.
2. **Fallback, only when the MCP server does not answer.** The person may connect the credential folder to the session; then run the script with
   `--dir` pointing at the connected folder. Say once, in one sentence: "a connected folder can be read by this session; the plugin's MCP server
   on your computer avoids that." On a connected Windows folder privacy cannot be verified from the shell (MAP-CRED-012): pass on the PowerShell
   command the check prints so the person can look at the access list themselves.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/credentials.py" services [--keyless]      # no credential folder needed: the library's list
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" status
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" add census
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" check census [--probe --confirm-probe api.census.gov]
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" rotate census [--finish]
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" remove census [--delete-files]
python3 "$GMAP_ROOT/scripts/credentials.py" --dir "$HOME/mnt/<credential folder>" audit [FOLDER]
```

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read credential-broker        # any module in the table
```

| Module              | Read it |
|---------------------|---|
| `credential-broker` | for how a registered key is named, checked and used |

## Subcommands

| Subcommand | MCP tool | What to tell the person |
|---|---|---|
| `services [--keyless]` | `credentials_services` | 3.5.0: every service the plugin takes a key for (name, other names, host, how the key is sent, sign-up, free limits, probe yes/no, optional); with `--keyless` (`include_keyless=true`) the entries that need no key too |
| `status` | `credentials_status` | Each credential's name, host, class, status (unchecked, ready, rejected, expired, missing), last check, review date, whether the file is private, and an old key file still waiting to be deleted after a rotation |
| `add NAME [--class browser]` | `credentials_add` | The file path to open, the sign-up address (Census: https://api.census.gov/data/key_signup.html), then "run check". For a name the library does not know (check `services` and the "did you mean" names first), ask for the host and how the key is sent (`query:NAME`, `header:NAME`, `header:NAME:PREFIX` when the service wants a word before the key such as `header:Authorization:OAuth`, `bearer`, `basic`, `path` when the key sits in the address path at `{key}`; for a service that wants a username and a key, `kind=userkey` and `user=header:NAME,key=header:NAME`, and the file holds `username:token` on one line; for the person's own PostGIS database, `inject=postgres:database=NAME,user=NAME` with optional `port`, `sslmode` and `schema`, the host as their computer or network names it, and the file holds the password of a read-only role). For a token that ends pass `expires=YYYY-MM-DD`; never ask for the key. **NASA Earthdata (3.2.0): one token for every archive.** `add earthdata` registers ONE bearer token that goes to CMR and every DAAC (LP DAAC, NSIDC, LAADS, GES DISC, PO.DAAC, ORNL, ASF), never one per host; when `earthdata-lpdaac`, `earthdata-nsidc`, `earthdata-laads` or the like are registered it takes their token over (the same token, or the one that ends last) with its end date, and says they can be removed. Then `check earthdata --probe` with `confirm_probe=cmr.earthdata.nasa.gov`. `also_hosts=` adds exact hosts to any credential |
| `check NAME [--probe]` | `credentials_check` | The yes/no answers, or the exact fix (a chmod line, or an icacls line by SID on Windows; `fix=true` applies it). 3.6.0: every keyed service has a test, so after `add` always offer `check NAME --probe`; the test is the entry's probe address, or its own sample request (a geocode, a box of images, a GraphQL stop search) when it names none, and a file-only check is never reported as the key working. A probe sends one small request to the registered test address: ask "send one test request to api.census.gov with your key?" and pass the host back as `confirm_probe` only on a yes |
| `rotate NAME` then `rotate NAME --finish` | `credentials_rotate` | Revoke the old key at the service first if it may have leaked; paste the new key into `NAME.next.key`; finish checks it and switches; the old file is kept for the person to delete (status reminds after 7 days) |
| `remove NAME` | `credentials_remove` | The registry entry and profile references are gone; the files are listed for the person to delete (deleted only when they ask) |
| `keychain NAME` (back: `keychain NAME --to-file`) | `credentials_keychain` | 1.12.0, only when the person asks: the key moves from its file into the computer's own keychain (Windows Credential Manager, macOS Keychain); the file is checked, the value stored and read back, then the file is overwritten and deleted. Needs the optional `keyring` package; when no keychain is available it says so and nothing changes. `rotate` is done as a file: move it back first |
| `audit [FOLDER]` | `credentials_audit` | Files and lines where a key pattern, or the exact value of a registered key, was found; never the value. For each hit: remove it, and if a real key was written anywhere, revoke and rotate it |

## Key classes (tile providers)

- `secret` (the default for every key): never written into any output. A print basemap uses it on the person's computer; a web page that would
  need it is refused (MAP-CRED-011), with the alternatives: the bundled basemap, a PMTiles file, or a browser key.
- `browser`: a tile key the provider designs to be public (MapTiler, CARTO), restricted by the person to their web site's domain in the
  provider's dashboard. Only then `add NAME --class browser --confirm-restricted`. It may go into a page (MAP-WEB-003 reminds them it is visible).

## After a rejection (MAP-CRED-008)

The run stopped on the first rejection and was not retried; the credential is marked `rejected`, and runs that need it refuse to start until
`check` passes. Census says no with a redirect to its `invalid_key.html` or `missing_key.html` page, not with 401.

A rejection is not always the key (1.0.2). Some services answer the same code for a wrong key and for a wrong address: FIRMS answers 400 to both.
The library says which text means the key (`reject.body`, such as "Invalid MAP_KEY"); a 400 without it is reported as the service's refusal of the
request (MAP-SVC-013) and the key is left alone. When a credential is marked `rejected`, `check` says which request was refused and through which
profile. Then:

- **The key may be fine** (the refused request went to an address other than the test address, or the person just made the key): offer
  `check NAME --probe`. With their consent (the host typed back), the probe runs even though the file is unchanged; if the test address
  accepts the key, the rejection is cleared with an INFO line saying it was not the key. Fix the address that was refused.
- **The probe is refused too**, or there is no test address: the key is wrong or revoked. Tell the person to get a new key and run `rotate`.

## Never

Ask for, accept, echo, store or log a key; put a key in a command line, an environment variable, a MapSpec, a profile, a project or output folder,
or a cloud-synced folder; share one person's key with another; delete a file the person did not ask to delete.
