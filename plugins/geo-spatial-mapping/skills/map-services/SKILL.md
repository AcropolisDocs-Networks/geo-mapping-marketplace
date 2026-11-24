---
name: map-services
disable-model-invocation: true
description: >
  Show the map plugin's default services and your customizations, and make or withdraw the one acknowledgment customizations need.
---

# map-services

The plugin's defaults need nothing from the person. Anything beyond them is a customization, and the terms of a customization are the
person's: they accepted them when they signed up or got their key. The plugin does not ask them to accept those terms again and never sets a
terms switch in a MapSpec on their behalf. It asks **once**, in plain words, that they keep each customization within the terms they accepted,
and records the yes in `.geo-mapping/customizations.yaml`.

| Defaults (nothing to acknowledge) | Customizations (the one acknowledgment) |
|---|---|
| The bundled background and reference layers | OpenStreetMap's own tile servers (light use), on any map |
| The US Census geocoder | Nominatim geocoding (world) |
| USGS 3DEP elevation in the US; the AWS terrain tiles elsewhere, with their credit printed | A free tier for non-commercial use without a key |
| The FOSSGIS Valhalla router, within the limits the plugin keeps | A data service you save as a profile (said, never refused) |
| Transit times from your own GTFS feed | A key you register: the key itself is your acceptance of that service's terms, so it needs nothing more |
| Public services the reference library lists as active | |
| | A data source of your own (`/geo-spatial-mapping:map-sources`): keyless ones need the acknowledgment; a keyed one is covered by its key |

Nothing here lifts a refusal that is not about terms: Google Maps and Bing Maps stay refused, entries whose terms are not read yet stay
unavailable, a third party's licence is asked about in its own pop-up (below), and the privacy gate, the caps and the request limits stay on.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/customizations.py" status            # defaults, the acknowledgment (when, how), the registered keys
python3 "$GMAP_ROOT/scripts/customizations.py" acknowledge --yes  # only after the person said yes to the statement in this conversation
python3 "$GMAP_ROOT/scripts/customizations.py" withdraw
```

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read tile-licensing        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `tile-licensing`         | for what each tile provider's terms allow |
| `tile-provider-registry` | for which provider a web page may use and the key each needs |

The plugin's server on the person's computer offers the same as `services_status` and `services_acknowledge` (`confirm=true`), with
`work_folder=` the project folder on their computer so a map built there finds it.

## The conversation

1. Run `status` and tell the person, in two or three lines, what is a default and what they have customized (their registered keys by name).
2. If a customization is asked for and there is no acknowledgment, show the statement exactly as `status` prints it and ask one question
   (AskUserQuestion where the session has it): **Acknowledge** or **Keep the defaults**. Never make the choice for them, never record it
   without their yes in this conversation, and never write `terms_acknowledged` into a MapSpec instead.
3. On a yes, run `acknowledge --yes` (or `services_acknowledge confirm=true`), then build again. On a no, use the default the refusal named.
4. The acknowledgment is asked once. Do not ask again when it is on file; `withdraw` takes it back, and a registered key still counts for its
   own service.

## A third party's licence (1.15.0)

Some data is only usable under a licence the person holds with its publisher (TeleGeography's submarine cables). That is not covered by the
one acknowledgment above, and it is never hidden or switched on quietly: the first map that uses such an entry is refused with MAP-LIC-018 (or
a selection names `licence_acknowledge`). Call the server's `licence_acknowledge` tool with the entry id and the work folder: it shows the
person a pop-up asking whether they hold that provider's licence and which uses it covers (internal, publication, commercial,
redistribution). Where the pop-up cannot be shown the tool returns `ask_user_question`: ask exactly that with AskUserQuestion and call again
with `answered_uses=` their picks. Never pick for them; no picks means no licence and nothing is recorded. `licence_status` lists what is
on file; `licence_ack.py withdraw ENTRY` takes one back.

```bash
python3 "$GMAP_ROOT/scripts/licence_ack.py" status
python3 "$GMAP_ROOT/scripts/licence_ack.py" ask ENTRY_ID
python3 "$GMAP_ROOT/scripts/licence_ack.py" acknowledge ENTRY_ID --uses internal,publication --yes   # only with the person's answer
```

A refusal for want of the acknowledgment names this command. An older MapSpec that says `terms_acknowledged: true` still builds: the line
counts as the acknowledgment for that one map. Do not add it to new MapSpecs.
