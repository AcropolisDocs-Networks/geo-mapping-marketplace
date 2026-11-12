---
name: webmap-create
description: >
  Make an interactive branded web map as one self-contained HTML page: layers, popups, time slider, swipe, 3D, story maps. Trigger
  on: interactive map, web map, AcropolisDocs map, branded map.
---

# webmap-create

Turns a map request into one HTML file the person opens in a browser, emails, or hosts. It is the same MapSpec as `map-create`, plus an optional
`webmap` block. The page carries its data, its fonts and its map engine inside it; a page on the bundled background makes no network request at all.
For a page that goes to a client or a team, run the `webmap-build` pipeline through `map-run` instead: it plans with a cost line (features,
page size, engine, basemap, network) and delivers a folder with the page, the MapSpec, provenance, the lint report and a page report.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/lints/mapspec_lint.py" map.yaml
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats html [--strict]     # add svg,pdf,png for the print set as well
```

## Modules

These are this plugin's reference pages, not skills: read one with the line below when its row applies. The Skill tool cannot load them.

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/modules.py" read map-text        # any module in the table
```

| Module                   | Read it |
|--------------------------|---|
| `map-text`               | before writing the title, subtitle or About text |
| `webmap-build`           | for the engine limits and what the build embeds |
| `webmap-controls`        | when choosing the page's controls |
| `webmap-render`          | for how layers, legends, popups and labels are drawn |
| `tile-provider-registry` | when picking the background |
| `tile-licensing`         | before naming any tile provider |
| `classify-and-color`     | for classification |
| `color-accessibility`    | when choosing colours |
| `telecom-geo`            | for sites, sectors, masts and link layers |
| `geo-service-explore`    | to save the host-confirmed public profile a WMS or WMTS layer needs |
| `webmap-block-reference` | **before writing or changing the `webmap:` block**: every option, with examples |
| `webmap-3d-story` | for a 3D page or a story map |

Exit codes are the same as `map-create`: 0 built, 3 a BLOCK finding, 2 a request this release cannot do.

## Order of work

**A fresh cloud workspace has no map packages** (`python3 -c "import geopandas"` fails): run `python3 "$GMAP_ROOT/scripts/doctor.py" --fix --quick`
once, before anything else. It installs what a map needs in one pip step, wheels first, within 10 minutes (1.2.0); never `pip install -r
requirements-dev.txt`, which brings the test stack. On the person's own computer, ask before installing.

1. **Same first steps as `map-create`**: find the message, inspect uploaded files, use the library first, pick the map type from the data, write the MapSpec.
2. **Decide the page's audience.** `audience:` says who opens the page (`internal`, `external`, `regulatory`, `any`). An unstated audience is treated as public.
   The audience decides which tile providers may be used and whether a confidential layer may go on the page at all.
3. **Pick the background** (read `tile-provider-registry`). The default is `basemap: {provider: bundled}`: country, state and lake outlines that ship with the plugin.
   A tile provider (`carto-positron`, `maptiler`) is used only with the person's own **browser-class** key, restricted to their domain and registered with
   `/geo-mapping-acropolisdocs:map-credentials add <provider> --class browser --confirm-restricted`; otherwise the page falls back to the bundled background
   (MAP-WEB-015 says how to add one). A `secret`-class key is never written into a page (MAP-CRED-011). Never ask for a key in the chat and never write one into the spec.
4. **Leave the frame at its defaults** (read `webmap-controls`, the `webmap.frame` reference). The page has one panel (title, then the Story,
   Legend, Layers, Details and About tabs; a tab with nothing to show is left out), one control rail on the map, one playback dock when there is
   time, the brand's small logo in the map's bottom-left corner (as on the print map; `brand: {show: chip}` adds the name on a pill) and the required credits in its bottom-right corner. Change it only when the person asks:
   a preset (`minimal` for a single-message map, `presentation`, `embed` inside an article), the panel's side or start state, the playback label
   (`frame.playback.label: "km {end}"`), or a co-brand. Give layers a `name:` when two would share one in the Layers tab (MAP-WEB-037).
   The page carries the same place names and context as a print map: the `names:` block (city, water-body and state names from the bundled Natural
   Earth 10m data, tiered by zoom; `names: {on: false}` turns them off) and the `context:` block (roads, urban areas, lakes and rivers from the same
   data; presets `auto`, `none`, `roads`, `urban`, `water`, `full`). On the bundled background `auto` draws the context; over a tile basemap it draws
   none, because the tiles already carry roads and names.
   **Rasters and relief go on the page as pictures.** A `role: raster` layer (a GeoTIFF, a library raster, or a WMS or MapServer picture) and the relief
   of `terrain:` or a `role: hillshade` layer are the same pictures the print map draws, warped to Web Mercator and carried inside the page as Leaflet
   image overlays (data URIs: the page still calls no host for them). Relief sits under every layer; a raster sits over the background and under the
   vector data, with an opacity slider beside its toggle in the layer list, and its legend is the same continuous ramp or classes as on paper. Each
   picture is at most 2,048 px a side on the page; a larger print picture is carried smaller and the notes say so. Contours are line layers.
5. **Let the engine follow the page** (read `webmap-build` for the limits). Leave `webmap.engine` at `auto`: small pages are drawn with
   Leaflet; MapLibre takes over when the page carries more than 20,000 features, a layer is large enough to go in as tiles, the basemap is
   vector tiles, or the time slider or the swipe compare is on. Name an engine only when the person asks for one. MapLibre draws large layers
   from tiles embedded in the page, groups dense points into numbered clusters until they stand apart, sets every label in Inter, and saves
   the view as a PNG; it needs a browser with WebGL (the static picture shows otherwise).
6. **Build, then look.** Build the page and report the findings, with every line of `map_text.callout` (the MAP-TEXT findings, never blocking).
   The `webmap-build` run opens the page in a headless browser at 1440x900, 1024x768 and 390x844 (the layout check) and keeps a screenshot of
   each size and state in the deliverable's `layout/` folder: look at them. The panel's header carries the same title and subtitle as the
   printed map, written by the `map-text` module (subject-led, Title Case; the About text may run to 3 sentences).
7. **Fix, do not argue.** A BLOCK is a rule the page breaks (an undeclared network host, a refused provider, a confidential layer headed for a public artifact). Change the spec and rebuild.

## Rules the builder enforces

- **No network host that is not declared.** The only hosts a page may call are tile hosts from a registry entry, listed in a host manifest and in the page's content security policy. Analytics, web fonts and CDN scripts are refused (MAP-WEB-001, -002).
- **Keys are public once they are in a page.** Only a browser-class key goes in, with MAP-WEB-003 reminding the person it is visible; without the recorded
  domain restriction the page also warns MAP-CRED-009. A secret-class key in a page is refused (MAP-CRED-011).
- **Attribution cannot be hidden** (MAP-WEB-004, -030, -031). The credits a provider's terms or a dataset's licence require sit in the map's
  bottom-right corner, built from the library's `attribution:` blocks (one holder once: one OpenStreetMap credit serves the tiles and every
  OpenStreetMap-derived layer), linked, and never covered (MAP-WEB-034); a provider logo the terms require (Mapbox, MapTiler on a free account)
  keeps its corner. Method notes and full provenance are in the About tab. An exported picture carries the credits in full, with the
  OpenStreetMap address when its data is on it. For the person's own file give `provenance.credit: {corner: ..., links: [...]}`. A file made from a library source names it with `provenance.library_entry`, and the corner then carries that source's credit and any logo its terms require (OpenWeather's, 1.1.3).
- **Only what the engine needs.** The page's content security policy allows nothing more (MAP-WEB-017): a MapLibre page adds `worker-src blob:`
  and `child-src blob:`, and network access only to the chosen basemap's host.
- **Confidential layers.** A client-confidential layer puts a banner and a `noindex` tag on the page. It never goes into an artifact unless the person confirmed (`confirm_publish`) (MAP-WEB-013). Say this before publishing, in one sentence.
- **Colors from the brand tokens, Inter only, accreditation in metadata only**, as for every map. The brand's visible mark is its small logo in the
  bottom-left corner, as on the print map (from the active brand pack; no name, no pill behind it); switching it off needs a reason (MAP-WEB-036).
- **A page that cannot start shows its picture and a message**, not a blank screen.

## Artifacts

A page published as an Artifact runs under the artifact host's content security policy, which may block tile requests. The bundled background always works.
Treat a tile basemap in an artifact as "may not load" until it has been seen loading; the fallback message tells the reader what happened. Never publish a page that holds
a confidential layer unless the person asked for exactly that.

## Tell the person

Lead with the file and one sentence on what it shows and how to use it (pan, zoom, click a feature, toggle layers, Print). Then at most three findings that change what they
would do: the background used and why, a key reminder, a confidentiality note. Offer one next step: a print set (`--formats svg,pdf,png,html`) or a different background.
