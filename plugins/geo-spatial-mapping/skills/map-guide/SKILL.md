---
name: map-guide
description: >
  Open the map plugin's guide and pick a task; names the setup commands to type (map-credentials for API keys, map-services, map-doctor,
  map-trace, brand). Trigger on: open the map guide, map plugin guide.
---

# map-guide

The guide is the overview page this edition carries, `docs/geo-spatial-mapping.html`: a task launcher (Main), what the plugin does, how to use
it, recipes to copy, every skill, the formats and sheets, a glossary and the changelog, all for this edition and in its brand. This skill
opens it and then helps the person pick one task in two or three short turns. It does not make the map: the skill it names does that.
Run as `/geo-spatial-mapping:map-guide`, or when the person asks how to use the map plugin. It never builds a map or starts a pipeline itself.

## Setup commands

These skills are typed by the person, never loaded with the Skill tool (which refuses them). When someone asks for one of these jobs in
words, give them the line to type:

| They want | They type |
|---|---|
| to add, check, rotate or remove an API key | `/geo-spatial-mapping:map-credentials` (for example `add census`) |
| to see the default services, or use OpenStreetMap tiles or Nominatim | `/geo-spatial-mapping:map-services` |
| to know whether this computer can make maps, or why a build cannot start | `/geo-spatial-mapping:map-doctor` |
| to know why a map failed in this session | `/geo-spatial-mapping:map-trace` |
| to add a dataset to the library or check entries for staleness | `/geo-spatial-mapping:reference-curate` |
| to add a data source of their own (an API, a file, tiles) with their own key, for every project | `/geo-spatial-mapping:map-sources` |
| where this session's tokens went | `/geo-spatial-mapping:map-token-use` |

`/geo-spatial-mapping:brand` (or `/geo-spatial-mapping:geo-set-brand`) lists your brands, adds a brand pack, makes one from a name, a colour and a logo, and sets the default map brand.


```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/map_guide.py" where                   # the page's path; exit 1 if this edition has none
python3 "$GMAP_ROOT/scripts/map_guide.py" copy --to "<folder>"   # a copy where the person can open it
python3 "$GMAP_ROOT/scripts/map_guide.py" tasks [--goal ID]      # goals and tasks: what you get, the skill, the line to type
python3 "$GMAP_ROOT/scripts/map_guide.py" asks                   # the everyday asks, each with the skill that picks it up
```

## Steps

1. **Open the page.** Run `where`, then `copy --to` the folder the session shows the person (the output folder, or a connected folder on
   their computer), and give them the copied file as a link or file card. If the host can show an HTML file inside the session, open it
   there. In a terminal session, give the path and say to open it in a browser. One sentence: "This is the plugin's guide: the Main tab
   lists the tasks; pick one and it opens here with the prompt typed."
2. **Ask what they want to make.** Run `tasks` and offer the goals as a short list (at most five, in the person's words where you can).
   One question only. If they already said what they want, skip the question.
3. **Name the task.** For the goal they pick, give at most three tasks from `tasks --goal ID`: what they get, the skill or pipeline that
   makes it, and the line to type (the task's `type`, with its blanks in brackets). For a quick start, add one or two everyday asks from
   `asks` that fit.
4. **Hand over.** Say the first line to type, or offer to start it now with their file or place filled in. When they say yes, the named
   skill takes over (`map-create`, `webmap-create`, `geo-convert`, `geo-find-data`, `map-restyle`, `map-export` and the rest). A task whose
   skill is a pipeline starts only through `/geo-spatial-mapping:map-run <pipeline>`, which shows its cost first; say so and stop there.

## Rules

- Plain and short: no rule ids, no file paths beyond the page link, no list longer than five lines.
- Only what this edition ships: the page lists it, so read the page, never memory.
- Never run `build_map.py`, `pipelines.py` or any map script from this skill; never fetch anything.
- If the page is missing (exit 1), say the edition was built without its guide, and list the map skills by name instead.
