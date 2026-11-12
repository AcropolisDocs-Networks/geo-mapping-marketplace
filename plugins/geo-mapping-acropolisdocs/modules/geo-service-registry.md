---
name: geo-service-registry
description: >
  UTILITY MODULE - read by geo-service-query. Stores, lists and checks service profiles: address, protocols, limits, license and how the
  service is reached. A profile never holds a secret; the host must be confirmed by the person.
---

# geo-service-registry

A profile is a small YAML file in the working folder (`services/<name>.yaml`). It records where a service is and how it may be used; nothing else.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/service_registry.py" list
python3 "$GMAP_ROOT/scripts/service_registry.py" get NAME
python3 "$GMAP_ROOT/scripts/service_registry.py" validate FILE
python3 "$GMAP_ROOT/scripts/service_registry.py" save FILE --confirm-host gis.example.org
```

## The fields that matter

```yaml
profile: county-gis
type: geoserver                 # geoserver | arcgis-rest | ogc-api-features | wmts | ogc-generic
base_url: https://gis.example.org/geoserver
workspace: roads
auth: {method: none}            # none | user-managed   (anything else is refused in this release: MAP-SVC-019)
network: {route: mcp, public: true}        # mcp: the bundled server fetches. user-run: the person runs a kit.
license: {default: public-domain}
limits: {max_features_per_request: 1000}
host_visibility: withheld       # optional: outputs say "internal data service"
```

## Rules

- **The host is confirmed by the person.** `save` refuses unless the host name is typed back as `--confirm-host` (MAP-SVC-001). Never fill it in yourself.
- **Three kinds of profile.** Authenticated (`auth basic`, `route device-run`, `public false`, `credentials: {netrc: "netrc:PATH"}` or `{file: "file:PATH"}`): references only, https only, capture-only, run on the person's computer (`credential-broker`). Public: `auth none`, `route mcp`, `public true`; the bundled server may fetch from it. Private: `auth user-managed`, `route user-run`, `public false`; it is capture-only and the bundled server never contacts it (MAP-SVC-025).
- **No secret, ever.** A user name or password in the address, or a key in its query, is refused (MAP-SVC-022).
- **http is a warning, a private or local address is a block** for the bundled server (MAP-SVC-020).
- **Record the terms.** If the license is missing the profile is valid but captures say `unknown` and cannot go into an external map (MAP-SVC-012).
- **Show profiles with the host withheld when the profile says so.** Use the `public_view`, not the file.
