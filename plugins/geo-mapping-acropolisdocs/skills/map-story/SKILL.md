---
name: map-story
description: >
  Tell a story with a map in a few beats, written once: web story page, video timeline and captions, panel sheet, carousel, slides.
  Trigger on: tell the story of, story map, walk me through, carousel.
---

# map-story

One story, many formats (2.0.0). A map makes one claim; a story makes it in steps. The claim and its beats are written once, in a `story:`
block of the MapSpec, and every format is made from them. A single map does not need this skill: use `map-create`.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found; nothing was run" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/lints/mapspec_lint.py" map.yaml
python3 "$GMAP_ROOT/scripts/story.py" plan map.yaml                                  # the beats as resolved, and what each output builds
python3 "$GMAP_ROOT/scripts/story.py" build map.yaml --out out [--outputs web,sheet,carousel,slides,video]
```

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found; nothing was run" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/modules.py" read story-beats        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `story-beats`            | **before writing the `story:` block**: every field, how a beat's frame and emphasis are worked out, each output, the findings |
| `message-check`          | before the story: the map itself must make its point with no words (the mute picture) |
| `map-text`               | for the map's title (subject-led, the same on every beat) |
| `mapspec-reference`      | for the MapSpec the story sits in |
| `webmap-3d-story`        | for a 3D page, a pitch or bearing per beat, or chapters written by hand |
| `export-animation-profile` | for the video: the flythrough's draft and final, sizes, narration and captions |

## Order of work

1. **The map first.** A story is built on a finished map. If there is none, make it with `map-create` (and `webmap-create` for a page) and
   pass its message check: a beat cannot rescue a map that needs explaining.
2. **The claim.** One sentence the whole story shows. If the request does not give it, ask once: "What should someone believe after the
   last beat?" It is `story.claim`; the map's `message` is used when you leave it out.
3. **The beats: three to five**, each **a change in what stands out**, not a paragraph. Propose them to the person in one short list before
   building, in this shape:
   - **establish**: the whole picture (`show: {view: fit}`), often with no line at all;
   - **evidence**: what the claim rests on (`show: {highlight: ...}`: the frame follows the highlighted features);
   - **turn**: the exception, the cost or the surprise (another highlight, or other layers);
   - **so-what**: what follows (`show: {focus: <layer>}` or the whole picture again).
   A beat's line (`say`) is a caption: 84 characters or fewer, one fact, and it may state a finding. Its key number goes in `figure`. The
   map's title does not change from beat to beat and stays subject-led. A beat can point (`show.marks`: a ring, an arrow, a bracket), and
   a number that runs through the video is a `story.counters` entry read from the data, never typed.
4. **Write the block** (read `story-beats` first), lint the MapSpec, then `story.py plan`. Read back each beat's `highlighted` count and
   `bbox`: a beat that highlights nothing (MAP-STORY-024) or repeats the beat before it (MAP-STORY-025) is fixed here, before anything is built.
5. **Say the cost, then build.** The plan's `map_builds` is the number of maps the outputs draw (a page counts its chapters' print
   pictures). Say it in one line ("4 beats, 3 outputs, 13 map builds"); over 20, ask before building. Then `story.py build`.
6. **Look at what was made**: open the sheet's PNG (the whole story on one page) and two of the carousel pictures. Each beat should read
   without its caption; if it does not, change its `show`, not its words.
7. **The video** is a second step, through `/geo-mapping-acropolisdocs:map-run flythrough` on the page the story built, with `--keyframes` and
   `--timeline` on `video/<id>_timeline.yaml`: a draft first, the final after the person has looked. The camera and the caption times in
   that file come from the beats; do not type them. `--audio FILE` lays the person's own narration under it; `<id>.srt` and `<id>.vtt`
   are written beside it.

## What the build makes

| Output | Files | From each beat |
|---|---|---|
| `web` | `web/<name>.html` | a chapter: its heading, its line, its view, its layers, and its highlight drawn by the page |
| `video` | `video/<id>_timeline.yaml`, `.srt`, `.vtt` | a camera keyframe at its view, held for its line's reading time |
| `sheet` | `sheet/<name>.svg`, `.pdf`, `.png` | a numbered panel with its caption; six to a sheet |
| `carousel` | `carousel/<name>-NN_portrait-social.png` | one picture, 1080 x 1350: the line as the subtitle, "N of M" in the tagline |
| `slides` | `slides/<id>_story.pptx` | one slide: the map as a picture, the title, the line and the key as editable shapes |

`story.json` lists the claim, the beats and each beat's files, for whoever places them in an article.

## Rules

- **Every beat is a map like any other**: linted, credited and recorded by the same builders. A BLOCK on one beat is fixed in the MapSpec.
- **Element corners do not move** between beats; the frame, the emphasis and the layers do.
- **No number typed twice.** A figure in a beat's line comes from the data you read for the map; say where in the reply. Check it against
  the plan's `highlighted` counts: a line and a layer that count different things is the commonest fault in a story.
- **Confidential layers** follow the map's own rules on every output; a page with one is never published unless the person asked.

## Tell the person

Lead with the sheet's PNG and the claim in one sentence. Then the files by output, one line each. Then at most three findings that change
what they would do, and the message check and map text lines as `map-create` gives them. Offer one next step: the video draft, or a beat
to change.
