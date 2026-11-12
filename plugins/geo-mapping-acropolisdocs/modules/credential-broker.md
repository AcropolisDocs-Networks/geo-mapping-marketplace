---
name: credential-broker
description: >
  UTILITY MODULE - read by geo-service-query, isochrone-map and map-credentials. How a credential is named, checked and used without the
  plugin ever holding it: a registered key (cred:NAME) read by the plugin's MCP server on the person's computer, or an HTTP Basic secrets
  file checked inside the device shell for an approved run. Refuses any key or password typed into chat.
---

# credential-broker

Two kinds of credential, one rule: a credential is a **reference** to a file on the person's computer, never a value.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
# registered keys (0.6.0): API keys and tokens, one private file each, kept with /geo-mapping-acropolisdocs:map-credentials
python3 "$GMAP_ROOT/scripts/credentials.py" status
python3 "$GMAP_ROOT/scripts/credentials.py" check NAME
# HTTP Basic for an approved run on the person's computer (M9)
python3 "$GMAP_ROOT/scripts/credential_broker.py" parse --ref netrc:PATH
python3 "$GMAP_ROOT/scripts/credential_broker.py" check-command --ref REF --host HOST --out-dir D [--project-dir D]
python3 "$GMAP_ROOT/scripts/approved_run.py" cred-command --run RUN          # then run it with the device shell
python3 "$GMAP_ROOT/scripts/approved_run.py" cred-check   --run RUN --output check.txt
```

## Reference forms (MAP-CRED-001)

| Form | Meaning |
|---|---|
| `cred:NAME` (preferred) | A credential registered in the person's credential folder (`%USERPROFILE%\.geo-mapping\credentials\` or `~/.geo-mapping/credentials/`). The registry says the file, the one host it is sent to, how it is sent (`?key=`, a header, a bearer token, Basic, or a username and a key each in its own header), how the service says no, and its class |
| `apikey:PATH` | A file whose one line is the key; the host and how it is sent come from the library entry that needs it |
| `netrc:PATH` | A netrc file (`machine HOST login USER password PASS`) for an approved run; curl reads it with `--netrc-file` |
| `file:PATH` | A file with one line, `user:password`, for an approved run; curl gets it as a config on stdin (`-K -`) |

`env:` is not accepted (the plugin's processes do not see variables the person sets, H.3 D3). A key, user name, password, token, cookie or
Authorization header typed into the conversation is never used or stored (MAP-CRED-007): do not repeat it, say it is now in the conversation and
should be treated as exposed, and point to `/geo-mapping-acropolisdocs:map-credentials rotate` (`redact.scrub` removes anything that slipped through).

## Where a registered key is read (the reach decision)

- **On the person's computer, by the MCP server (primary).** The server resolves `cred:NAME`, checks the file, reads the key for one request, adds
  it at send time, masks it in everything it returns and drops it. The credential folder is never connected to a session. Service profiles that
  say `auth: {method: credential, credentials: cred:NAME}` and library entries with `requires_credential` are used this way (`ref_fetch` tool).
- **In-process with the folder connected (fallback).** Only when the MCP server is not running: the scripts read the key in-process, never on a
  command line, in an environment variable or a printed command. On a connected Windows folder privacy is not verifiable (MAP-CRED-012).

## Rules (BLOCK unless marked)

- **MAP-CRED-002.** The file and its folder are private: mode `0600`/`0700` on macOS and Linux; on Windows inheritance removed and entries only for
  the person, SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544), read from the access list by SID. `check` gives the exact fix.
- **MAP-CRED-003.** The file and folder are outside the plugin, project and output folders. **MAP-CRED-004 (WARN):** not in a cloud-synced folder.
- **MAP-CRED-005.** Sent only to the registered host, only over https on the default port, never across a redirect.
- **MAP-CRED-008.** A rejection (401, 403, or the provider's reject page: Census redirects to `invalid_key.html` or `missing_key.html`) ends the run
  with no retry and marks the credential `rejected`, with the request that was refused and its profile; runs that need it refuse to start until
  `check` passes. Another declared code (FIRMS's 400) is a rejection only when the answer carries the entry's `reject.body` text; otherwise it is
  MAP-SVC-013 and the credential is untouched (1.0.2). `check NAME --probe`, with consent, runs the probe even on an unchanged file and clears a
  rejection the test address does not repeat. MAP-SVC-018 stays for Basic.
- **MAP-CRED-011.** A `secret`-class key is never written into a web map or other shared output; only a `browser`-class tile key may be.
- **MAP-CRED-013.** A fetch that needs a credential stops when none is registered ("run /geo-mapping-acropolisdocs:map-credentials add NAME"), or when the registered host is
  not the request's host.
- **MAP-SVC-029** (Basic files). Only `netrc:` and `file:` references, as plain paths. The secrets file sits outside the project, output and plugin
  folders and outside cloud-synced folders; on the device shell it may sit in a connected folder only when that folder is the credential folder
  itself. The check prints yes/no answers (`exists`, `regular`, `private`, `shaped`, `synced`, `inside_project`, `mount_synthetic`), never the content.
- **MAP-SVC-016 / 017 / 009.** Basic credentials go only over https; an authenticated source is capture-only; a capture says only
  `auth: basic (credentials redacted)` or, for a registered key, `auth: {credential, kind, host}`.

## Flow for an approved run with Basic

1. Ask the person for the path of their secrets file (and for a dedicated read-only account if it is an admin account). Never ask for the value.
2. `plan-curl` / `plan-kit` with `--credentials REF` (or `--profile NAME`). The run card shows the reference and the host, never the secret.
3. Probe the host, then **check the secrets file**: `cred-command`, run it with the device shell, `cred-check`. Fix what it reports, check again.
4. Approve and run as in `service-request-kit`. A standing approval is tied to the one secrets file.
5. On success the capture is `device-run`, attested by the plugin, confidential, host withheld.

## Never

Ask for, accept, echo, store or log a secret; put a secret in a command's text or arguments or an environment variable; use `env:`; send a
credential over http or to another host; retry after a rejection; write a secret-class key into a page; use an admin account's credentials without
saying so.
