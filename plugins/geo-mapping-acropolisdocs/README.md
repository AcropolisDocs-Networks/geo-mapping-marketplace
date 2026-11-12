# geo-mapping-acropolisdocs 1.9.0

Cartographic maps from a request or your own geodata: print maps (SVG, PDF, PNG), interactive web maps (one self-contained HTML page), data
conversion and analysis, public data services, travel-time areas and terrain, each checked before it is handed over and each with its sources.

Open the guide first: ask "open the map guide", or open `docs/geo-mapping-acropolisdocs.html` in a browser. It has the tasks, recipes, every skill, the
formats, the API-key setup and the **Customization** tab (your own brand, style packs, defaults).

## Install

Install the `.plugin` file in Claude (desktop app or Claude Code). Install one `geo-mapping-*` plugin only: if an older edition is still
installed beside this one, every map skill stops and names both, so remove the old one.

Nothing has to be installed by hand. The first time the plugin starts, its data server builds a private Python environment and installs the
map packages, the optional extras and Chromium (for web-map checks and flythroughs) by itself; restart the plugin when it says Done. Python
3.11 or newer must be on the computer. `/geo-mapping-acropolisdocs:map-doctor` reports what is present and what each missing piece costs you.

| Computer | How the data server starts | Where its private environment lives |
|---|---|---|
| Windows | `mcp\launch.cmd` (the default in `.mcp.json`) | `%LOCALAPPDATA%\AcropolisDocs\geo-mapping\venv` |
| macOS | `mcp/launch.sh`, after the one step below | `~/Library/Application Support/AcropolisDocs/geo-mapping/venv` |
| Linux | `mcp/launch.sh`, after the one step below | `~/.local/share/acropolisdocs/geo-mapping/venv` |

**macOS and Linux, one step.** A plugin's server entry cannot yet name a different launcher per operating system, and the default is the
Windows one. From the plugin's folder run `sh mcp/use-posix.sh` once (again after a plugin update), then restart the plugin. It rewrites
`.mcp.json` to start `mcp/launch.sh`; `sh mcp/use-posix.sh --windows` puts the Windows entry back. In Claude Code on macOS or Linux the map
skills themselves work without the server (they run the same scripts in your shell); the server adds the typed service tools and the
credential folder.

The map skills run in the session's shell. In the desktop app that is a Linux sandbox, where `scripts/doctor.py --fix --quick` installs the
map packages in about a minute; only the data server runs on your own computer.

## Your brand

The plugin ships in the AcropolisDocs brand. To use your own, add a **brand pack**: nothing is rebuilt and the plugin is not changed.

```
/geo-mapping-acropolisdocs:brand new      a brand from a name, one to five colours and a logo (SVG is best; PNG or JPEG works)
/geo-mapping-acropolisdocs:brand add      a pack folder or a brand .json you were given
/geo-mapping-acropolisdocs:brand set      your default brand for new maps
/geo-mapping-acropolisdocs:brand          the brands here and where packs are kept
```

A pack changes the map's colours (derived once into map roles, with contrast and colour-vision checks), the logo, the names in file
properties, the output file prefix and an optional trademark line. It lives outside the plugin (`.geo-mapping/brands/<slug>/` in your project,
or a folder named in `GEO_MAPPING_BRANDS`), so a plugin update never removes it. Brands bundled: acropolisdocs.

## What is in the package

| Piece | Where |
|---|---|
| Skills and commands | `skills/`, `commands/` (the guide lists them) |
| Reference pages the skills read | `modules/` |
| The map engine, checks and pipelines | `scripts/`, `lints/` |
| Data server (OGC, ArcGIS, Census, geocoding, credentials) | `.mcp.json`, `mcp/` |
| Reference library of data sources, with terms | `library/` |
| Bundled world and US background data (Natural Earth, Census) | `assets/basemaps/` |
| Style packs (antique, blueprint, editorial, monochrome, sketch) | `styles/` |
| The shipped brand pack | `brands/` |
| Your brands: list, add, make, set the default | `/geo-mapping-acropolisdocs:brand`, skill `geo-set-brand` |

## Python packages

`requirements.txt` lists what the maps need (a floor only where an older release misbehaves); it includes `mcp/requirements.txt`, the data
server's own exact pins. `requirements-optional.txt` holds the extras (rasterio, duckdb, h3, playwright and others); a map that needs one says
so. PDF output needs WeasyPrint's native libraries (Pango), which pip cannot install: the doctor's PDF line says when they are missing, and
SVG and PNG do not need them.

## Rules this plugin keeps

- Inter is the typeface. A brand changes colour, logo and voice, never type; a style pack may bring its own faces.
- The AcropolisDocs credit travels only as hidden tags and document properties. It is never drawn on a map.
- The brand tools answer only inside a map build and never style documents, decks or charts.
- Read-only toward services. Your keys stay in your own credential folder on your computer; they never enter the chat, a map, a log or the package.
- A logo in a brand pack must be a plain SVG drawing; one that carries scripts, links or embedded pages is refused.

Working files live in `.geo-mapping/` (defaults, added brand packs, library overlay, run records). Licence: Apache-2.0, see `LICENSE` and `NOTICE`.
