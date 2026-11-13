---
name: map-text
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle, webmap-create and map-atlas. The words on a map: what the title, subtitle, tagline,
  legend title, data table, source line and footer each say, the title and subtitle rules, patterns to start from, how to write the text
  when the person gives none, and the MAP-TEXT lints with the chat callout they need.
---

# map-text

Read this before writing a map's `elements.title` (text, subtitle, tagline) or a legend title, and when a MAP-TEXT finding needs explaining.
Placement, sizes and fonts are not text: the template sets them. The source of these rules is the plugin's map text spec (0.21.0).

## The rule in one line

**The title names the subject. The subtitle adds the one thing a reader needs next.** Everything else goes to the element that owns it.

## Who says what

Each fact appears in exactly one element. If two say the same thing, it stays in the one higher in this table.

| Element | Owns | Never carries |
|---|---|---|
| Title | The subject, or the single headline finding | Dates, sources, methods, layer lists, series names, units |
| Subtitle | One of: the key number, the scope (where/when), or the measure | A second finding, layer narration, sources, series names |
| Tagline | The series or program identifier only | Anything about this map's content |
| Legend title and labels | Units and class meanings | Restating the title |
| Data table | The supporting numbers | Prose |
| Source line | Data sources and the method in a phrase | Findings |
| Date stamp, footer | Production date, CRS note, disclaimer | Anything else |

## Title

- 3 to 7 words; 45 characters or fewer is the target, 60 the limit.
- One idea: no dash, colon, parentheses or "and" joining a second idea.
- **Subject-led by default**: a noun phrase naming what and where (*Link Light Rail Walksheds, Seattle*). A finding-led claim
  (*Most of Seattle Is Beyond a 10-Min Walk of Link*) only when the person asks for one or writes it.
- No filler: never "Map of", "A map showing", "Overview of", "Analysis of", "Sample", "Draft".
- No dates, quarters, sources, method words (buffer, isochrone, kernel), hashtags, file or version names.
- Title Case, AP style: short articles, conjunctions and prepositions lower case unless first or last. No ALL CAPS, no trailing period.
- With a subject-led title the headline number goes in the subtitle. Numerals, not words (*16%*).
- The name a reader knows (*Link*, *BART*, *Caltrain*); abbreviations the audience uses (n41, 5G SA, DAS) are fine.

## Subtitle

- One line; 60 characters or fewer is the target, 80 the limit.
- Exactly one job: **the key number**, **the scope** (place and/or period) or **the measure** (what the colour or size encodes).
- Shares no content word with the title; never narrates the drawing ("over", "shown", "colored by", "by census tract", "every building").
- Sentence case; a phrase, no trailing period.
- At most one separator, the middle dot with spaces (` · `), joining scope pieces: *King County · Q3 2026*. No pipes, slashes or colons.
- If nothing passes without repeating the title or the legend, leave the subtitle off. An empty subtitle beats a filler one.

## Tagline

`<Series> · <Item> · <Theme>`, the same on every map of the series, 50 characters or fewer: `#30DayMapChallenge · Day 07 · 10 Minute Map`,
`Venue Readiness · Report 3`. Off unless the map belongs to a series or program.

## Patterns to start from

| Map type | Title | Subtitle |
|---|---|---|
| Subject-led (default) | Link Light Rail Walksheds, Seattle | 16% of residents live within a 10-minute walk |
| Finding-led (on request) | 16% of Seattle Lives a 10-Min Walk from Link | Residents near a station, 2020 |
| Coverage | n41 Coverage, Phoenix Metro | Outdoor RSRP better than -105 dBm · Q3 2026 |
| Site density | Macro Site Density by County | Arizona · Q3 2026 |
| Program status | Venue Connectivity Status | 31 of 46 venues complete · November 2026 |
| Clustering | NYC Public Wi-Fi Clusters | LinkNYC kiosks and public hotspots |
| Gap (on request) | Bronx and Queens Have the Fewest Hotspots | Public Wi-Fi points per 10,000 residents |

## Every other line

- Legend title: the measure and its unit, sentence case, no period: *Walk time (min)*, *Sites per 100 sq mi*.
- Legend classes: ranges with an en dash and no repeated unit (*0–5*, *5–10*); the unit lives in the legend title.
- Callouts and labels: 1 to 5 words, a place name or a number with its unit. A callout's key number goes in its `figure` (*692 MW*), set large
  above the words (1.13.0).
- The title block sits on the map with a soft glow of the page colour under its letters (`elements.title.halo: glow`, 1.13.0), so the title,
  subtitle and tagline read where they cross lines and class edges; it is no reason to move the title.
- Data table headers: 1 to 3 words, unit in brackets: *Residents (k)*.
- Source line: one method phrase at most (*walk times by Valhalla*). A licence's required credit is never shortened.
- Web title panel: the same title and subtitle as the static map; the About text may run to 3 sentences.
- House style: numerals; `%` attached; `min`, `mi`, `km`, `sq mi`; `2020`, `Q3 2026`, `November 2026` (never `11/26`); no emoji, no
  exclamation marks; one name for one thing across title, legend and source.

## When the person gives no title

1. Read the message. The title is subject-led unless the person asked for a finding-led one.
2. Write the title by the rules above, from the subject-led pattern; a headline number becomes the first subtitle candidate.
3. Subtitle candidates: the key number, the scope, the measure. Drop any that repeats the title, a legend title or the data table.
4. Keep the best one left (number, then scope, then measure); none left, no subtitle.
5. Move what was removed to its owner: sources to the source line, series to the tagline, dates to the scope or the footer.
6. Revise until no MAP-TEXT WARN fires, then write `elements.title.text` and `subtitle` into the MapSpec.

When the MapSpec has no `elements.title.text` at all, the build draws the message's first sentence tidied (filler, dates and hashtags
stripped, cut at a dash, colon or parenthesis, Title Case, 60 characters at most). Write the title yourself rather than rely on that.

**Text the person supplies is drawn as written.** The lints still run and are reported; never rewrite supplied text unless asked.

## The lints

None of them blocks a build or a deliverable, MAP-TEXT-009 included.

| Code | Severity | Fires when |
|---|---|---|
| MAP-TEXT-001 | WARN (INFO over 45) | Title over 60 characters |
| MAP-TEXT-002 | WARN | Title joins two ideas with a dash, colon, parenthesis or "and" between clauses |
| MAP-TEXT-003 | WARN | Title starts with filler |
| MAP-TEXT-004 | WARN | Title holds a date, quarter, source name or method word |
| MAP-TEXT-005 | WARN (INFO over 60) | Subtitle over 80 characters |
| MAP-TEXT-006 | WARN | Subtitle repeats a content word of the title (a word both use only as a modifier is not counted) |
| MAP-TEXT-007 | WARN | Subtitle narrates the layers |
| MAP-TEXT-008 | INFO | Subtitle stacks separators: more than one ` · `, a pipe, slash list, semicolon or colon |
| MAP-TEXT-009 | WARN | The same fact in the title or subtitle and a legend title, the data table, the tagline or the source line |
| MAP-TEXT-010 | INFO | Title not Title Case, subtitle not sentence case, ALL CAPS, a trailing period |
| MAP-TEXT-011 | WARN | A series name, hashtag or production date in the title or subtitle |
| MAP-TEXT-012 | INFO | A unit or date written two ways across the map's text (*min* and *minute*) |

MAP-CART-013 (the title states nothing of the message) still runs; MAP-TEXT-001 replaces MAP-CART-022.

## The callout in chat

The build result carries `map_text`: the title and subtitle drawn and `callout`, the lines to put in the chat summary. List every line,
never only in a log or report file (the INFO findings join the WARNs when the audience is external or regulatory):

```
Map text: 2 findings (not blocking)
- MAP-TEXT-006 the subtitle repeats 'walk' from the title -> suggest: drop 'walk' from the subtitle, or say the next thing instead
- MAP-TEXT-009 'q3 2026' appears in the subtitle and the legend title -> suggest: say it once: drop it from the legend title
```

The person decides. Offer the suggested text; change supplied text only when they say so.
