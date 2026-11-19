---
name: story-beats
description: >
  KNOWLEDGE MODULE - read by map-story. The story: block of a MapSpec: the claim, the beats and each field of a beat, how a beat's frame
  and emphasis are resolved against the data, what each output makes of a beat, the timing of the video, and the MAP-STORY-020 to 025 findings.
---

# story-beats

Read this before writing or changing a `story:` block. The builder is `scripts/story.py`; the web page reads the same block through it.

## The block

```yaml
message: Cheap hydro on the Mid-Columbia is why Washington's data centers sit in Quincy.
story:
  claim: "Cheap hydro on the Mid-Columbia is why Washington's data centers sit in Quincy."   # default: the message
  outputs: [web, sheet, carousel, slides, video]        # default: web and sheet
  beats:
    - role: establish
      title: "Power Rates Across the State"             # a short heading; default: the line cut at its first clause
      show: {view: fit}                                 # the whole of the data; the default view
    - role: evidence
      say: "Five dams, the cheapest power in the state."            # the caption: 84 characters or fewer; may be left out
      figure: "3.8 c/kWh"                               # the key number
      show:
        highlight: {layer: rates, column: cents_kwh, max: 5}        # the frame follows the highlighted features
    - role: turn
      say: "Grant PUD's queue is 692 MW; lines reach Quincy with 372."
      figure: "692 MW"
      show:
        highlight: {layer: hubs, column: name, in: [Quincy]}
        layers: [rates, hubs, lines]                    # the data layers this beat shows (context layers always stay)
        view: {pitch: 45, bearing: -20}                 # a tilt for the page and the video, on the frame the highlight gives
    - role: so-what
      say: "The next hub goes where the next line goes."
      show: {focus: lines}                              # frame a layer without changing the emphasis
      hold: 6                                           # seconds in the video (default: the line's reading time)
```

| Field | What it is |
|---|---|
| `claim` | The one thing the story shows. The page's Story tab and the sheet's subtitle lead with it |
| `role` | establish, evidence, turn, so-what or close. A guide to the order, not a rule (another word is MAP-STORY-023, INFO) |
| `title` | The beat's heading: a chapter's title, a callout's label. Keep it under 48 characters |
| `say` | The beat's line. One fact, 84 characters or fewer (MAP-STORY-021). It may state a finding; the map's title stays subject-led |
| `figure` | The key number. It leads the line where one sentence is shown; with a `title` of its own it is set large on the picture |
| `show.view` | `fit` (default), or `{bbox}` / `{center, zoom}` with optional `pitch` and `bearing`; `pitch` and `bearing` alone tilt the resolved frame |
| `show.focus` | A layer id: the frame fits that layer (its highlighted features when the beat highlights it) |
| `show.highlight` | `{layer, column, in | min | max | top}` as on a layer, or `auto` / `{layer, auto: true}`: read from the beat's line ("the n41 sites", "top 5", "the cheapest") |
| `show.layers` | The data layers shown. Context, relief and rasters stay |
| `show.time` | A period of the layers' time field (a value or a label of the page's slider) |
| `show.marks` | This beat's rings, arrows and brackets (`elements.marks` items, see `mapspec-reference`), in place of the map's. Drawn on the sheet, the carousel and the slides; the page and the video show the highlight only |
| `hold` | Seconds the beat is held in a video |
| `web` (on `story`) | The page's own story options when its chapters are the beats: `{cards: compact | full, navigation: buttons | scroll | both, print_pictures: on | off | first, print_dpi, follow_time}`. `print_pictures: off` saves one map build a beat on a heavy map |
| `counters` (on `story`) | Up to three numbers that run with the video, read from the data at each beat and never typed: `{layer, text: "{v} sites", column, format, position}`. The value is the count of the layer's features the beat highlights (all of them when it highlights another layer or nothing), or the sum of `column`. A beat that hides the layer keeps the number where it was |

## How a beat is resolved

`story.py plan` prints each beat as the outputs will draw it. For each beat, in this order: the highlight (written, or found by `auto`), the
frame (a written view; else the `focus` layer; else the highlighted features; else all the data), padded by 12 percent and never narrower
than 12 percent of the data's span, then the layers. A frame is kept in WGS 84, so every output draws the same window.

The beat's own line picks what is primary in it: with a highlight or a layer list, the beat's map takes the line as its `message`, so the
layers the line names stand at full strength and the others recede, as on any map.

A number in a line must be one the data gives. `plan` prints each beat's `highlighted` count, and a counter shows what the layer holds: when
a line says "a third" and the counter says 23 percent, the line is about other data than the map draws (Day 16: the claim counted every
square, the layer left out squares with fewer than three editors). Change the line or the layer, not the counter.

## The outputs

- **web**: the beats are the page's chapters when the MapSpec has no `webmap.story` of its own. A chapter's highlight is drawn by the page
  (MapLibre: the picked features at full strength, the rest of that layer faded; a Leaflet page moves the map and switches layers only). The
  chapters' print pictures show the same highlight. A chapter written by hand can carry `highlight: {layer, column, ...}` too.
- **video**: `video/<id>_timeline.yaml` holds the camera keyframes (arrive at each beat's view, hold for the line's reading time: its
  characters at 15 a second plus 1.2 s, never under 2.5 s, 2.2 s of flight between beats) and the captions. Fly it with
  `map_export.py --flythrough PAGE.html --keyframes T.yaml --timeline T.yaml` through map-run's flythrough pipeline; each keyframe names its
  chapter, so the page draws that beat's highlight in the frames. `story.counters` become the timeline's counters: each arrives with the
  camera, holds with the beat and runs during the flight to the next. `<id>.srt` and `<id>.vtt` carry the captions for players, search
  and translation; `--audio FILE` lays the person's own recording under the video (the plugin makes no voice).
- **sheet**: a panel sheet, one numbered panel per beat with the line as its caption, one shared legend and sources line, six beats to a
  sheet (a longer story makes more sheets). The sheet's `.sheet.yaml` is kept beside it and can be rebuilt with `build_map.py`.
- **carousel**: one `portrait-social` picture per beat. The title is the map's; the subtitle is the beat's line; the tagline counts
  ("2 of 4", before the map's own tagline when it has one).
- **slides**: one deck, one `16x9-slide` per beat, through the slide adapter (the map as a picture; title, line and key as shapes).

`story.json` is the hand-off: the claim, the map's title, and for each beat its heading, line, figure, frame, highlight, the number of
features it highlights and the files made for it (`carousel` picture, `slide` number). An article pipeline places the pictures and
captions from it without reading the MapSpec.

## Pointing without explaining

Two elements of any map, often used by a beat (both are in `mapspec-reference`):

- `elements.marks`: a **ring** round a feature, an **arrow** to one, a **bracket** along a stretch, each with at most a few words. A mark
  points; the reader still sees for themselves. Three marks on one picture is the most that reads.
- `elements.detail`: a **detail inset**, the same layers drawn closer in a free corner, with the window outlined on the map. Use it when
  the subject is small in a frame that has to stay wide (a stadium in a district, one site in a market).

## Findings

| Finding | Severity | Meaning and fix |
|---|---|---|
| MAP-STORY-020 | BLOCK | The block cannot be built: no beats, over 12, a repeated id, an unknown layer or output, a highlight without a column and a test |
| MAP-STORY-021 | WARN | A line over 84 characters: say one thing, and let the highlight show the rest |
| MAP-STORY-022 | WARN | More than 7 beats: merge the ones that show the same thing |
| MAP-STORY-023 | INFO | A role outside the five; kept as written |
| MAP-STORY-024 | WARN | A highlight that picks nothing: `auto` found no value, top N or end in the line, or the written test matches no feature |
| MAP-STORY-025 | WARN | A beat that shows what the beat before it shows: give it a highlight, a focus or other layers, or fold its line into the one before |
