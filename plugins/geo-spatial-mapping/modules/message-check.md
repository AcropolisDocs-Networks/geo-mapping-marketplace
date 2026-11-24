---
name: message-check
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle and webmap-create. The display tells the story: the message check (MAP-STORY findings
  and measures), the mute picture and its one question, the draft build, and the MapSpec lines that make the drawing carry the point
  (extent fit_message, highlight auto, legend direct and counts).
---

# message-check

Read this when a build lists "Message check" findings, before you look at a draft, and whenever a map needs a callout to make its point.
The rule (1.22.0): **show it before you say it.** A reader should get the point from what stands out, where the frame sits and what is
labelled, before decoding the legend or reading a callout. Words are for what the drawing cannot say.

## The loop

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --draft      # two small pictures, seconds: <stem>_draft.png and <stem>_mute.png
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats svg,pdf,png     # the finished set, once the draft is right
```

1. Build with `--draft`. Nothing else is written, so a finished set is never half replaced.
2. **Open the mute picture first** (`story.mute_preview`): the sheet with its title, legend words, callouts, table and footer taken off.
   Say in one sentence what it shows. If that is not the MapSpec's `message`, the drawing is not carrying it yet.
3. Fix the drawing, not the words: the table below. Then the draft again.
4. When the mute picture says the message, build the finished set. Every full build writes the mute picture too (`--no-mute` leaves it out)
   and never lists it as a deliverable.

## What the build measures, and the line that fixes it

`story.measures` in the build result holds the numbers; `story.callout` the findings to pass on. None blocks and none moves an element.

| Finding | What it saw | Change in the MapSpec |
|---|---|---|
| MAP-STORY-001 | What the map is about spans under a quarter of the frame | `extent: {mode: fit_message}`: the frame fits the layers the message names (their `highlight:` features when they have one); the rest are surroundings. `padding_pct` sets the room round it (default 14) When the frame has to stay wide (a stadium in a district that must show its stations), keep it and add `elements.detail: {on: true, subject: true}`: the detail inset draws the subject closer and this finding is not raised (2.0.0) |
| MAP-STORY-002 | A supporting layer carries more ink than the subject | `emphasis: background` on that layer, an outline (`symbol: {fill: none}`), a thinner line, or a stronger subject |
| MAP-STORY-003 | The legend takes over 22% of the map's width or 12% of its area | `elements.legend: {direct: auto}`, `legend: {single: true}` on a classed layer, `legend: {include: false}` on a layer that needs no key |
| MAP-STORY-004 | Two categories of one key look alike as drawn | Fewer categories, `emphasis: primary` on the layer (a layer at 55% loses the difference), or names on the map; or one entry for the whole layer (`legend: {single: true}`) when the categories do not matter to the message |
| MAP-STORY-005 | More than two callouts, or over 30 words in them | One callout with the key `figure`; `highlight:` for the rest |
| MAP-STORY-006 | The message singles out part of a layer; the drawing does not | The `highlight:` block the finding gives, or `highlight: auto` |
| MAP-STORY-007 | A layer named on the map found no room for a name | Nothing: the build already drew it again with the layer back in the legend. `legend: {direct: false}` settles it |

## The lines

```yaml
message: Cheap hydro on the Mid-Columbia is why Washington's data centers sit in Quincy.   # names what stands out: layers, values, an end
extent: {mode: fit_message}                # frame what the message names; fit_layers (default) frames every data layer
elements:
  legend: {on: true, direct: auto, counts: bars}   # direct: lines and categories named on the map leave the key; counts: features per class beside each class
layers:
  - id: rates
    role: choropleth
    value_field: cents_kwh
    highlight: auto                        # the message says "cheapest": the lowest class at full strength, the rest faded; names ("Quincy", "n41") and "top 5" work the same way
    legend: {title: "Business rate (c/kWh)", counts: bars}     # or numbers; a layer's own setting wins over elements.legend.counts
  - id: lines
    role: lines
    legend: {label: "345 kV and above", direct: true}          # named along its longest line with room; false keeps it in the key
```

- **`highlight: auto`** reads the message against the layer: values it names in a text column (whole words; three characters, or two with
  a digit), "top N" / "largest five", or one end of a classed number (lowest, fewest, cheapest, smallest, weakest, slowest; highest,
  greatest, largest, biggest, busiest, strongest, fastest, densest, peak). "Most" and "least" alone single nothing out. With nothing found
  the layer is drawn at one strength and a note says so.
- **The key agrees with the map.** A class or category with no highlighted feature is keyed at the strength the rest is drawn.
- **`legend.direct`** qualifies a line layer with one symbol or up to 8 categories, an area layer with one symbol or categories (named in
  its largest area), and a point layer only when each name belongs to one point. Numeric classes always keep their key. Web pages and
  slides keep every key: their legends are their own.
- **Labels follow the message.** With a `labels:` entry on a layer, a feature the message names is labelled first and bold, a highlighted
  one next and semibold. A map whose message names nothing is labelled as before.
- **Element corners do not move.** The title, legend, north arrow, logo, scale bar and locator keep their default places; these lines
  change what is in the frame and in the key, never where an element sits.

## Tell the person

After the result and the map text: "Message check: N findings (not blocking)" and each line of `story.callout` as written, then what you
changed because of the mute picture, in one sentence. Do not add a callout to fix a message-check finding.
