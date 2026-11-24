# geo-spatial-mapping 4.0.6

Cartographic maps from a request or your own geodata: print maps (SVG, PDF, PNG), interactive web maps (one self-contained HTML page), data
conversion and analysis, public data services, travel-time areas and terrain, each checked before it is handed over and each with its sources.

Open the guide first: ask "open the map guide", or open `docs/geo-spatial-mapping.html` in a browser. It has the tasks, recipes, every skill, the
formats, the API-key setup and the **Customization** tab (your own brand, style packs, defaults).

## Install

Install it in Claude Cowork: **Customize → Plugins → Add → Add marketplace**, enter `AcropolisDocs-Networks/geo-mapping-marketplace` and
install the plugin from **Discover** (updates: **Check for updates** on the marketplace), or upload the `.plugin` file there. It is saved to
your Claude account, so chat and Claude Code have it too. Install one copy only: if a second copy of
`geo-spatial-mapping` is installed beside this one, every map skill stops and names both.

Nothing has to be installed by hand. The first time the plugin starts, its data server builds a private Python environment and installs the
map packages, the optional extras and Chromium (for web-map checks and flythroughs) by itself; restart the plugin when it says Done. Python
3.11 or newer must be on the computer. `/geo-spatial-mapping:map-doctor` reports what is present and what each missing piece costs you.

| Computer | How the data server starts | Where its private environment lives |
|---|---|---|
| Windows | `mcp\launch.cmd` (the default in `.mcp.json`) | `%USERPROFILE%\.geo-mapping\runtime\venv` |
| macOS | `mcp/launch.sh`, after the one step below | `~/Library/Application Support/AcropolisDocs/geo-mapping/venv` |
| Linux | `mcp/launch.sh`, after the one step below | `~/.local/share/acropolisdocs/geo-mapping/venv` |

**macOS and Linux, one step.** A plugin's server entry cannot yet name a different launcher per operating system, and the default is the
Windows one. From the plugin's folder run `sh mcp/use-posix.sh` once (again after a plugin update), then restart the plugin. It rewrites
`.mcp.json` to start `mcp/launch.sh`; `sh mcp/use-posix.sh --windows` puts the Windows entry back. In Claude Code (through the account sync) on macOS or Linux the map
skills themselves work without the server (they run the same scripts in your shell); the server adds the typed service tools and the
credential folder.

The map skills run in the session's shell. In the desktop app that is a Linux sandbox, where `scripts/doctor.py --fix --quick` installs the
map packages in about a minute; only the data server runs on your own computer.

## What installs the first time the plugin starts

Nothing is installed into your system Python and no administrator rights are needed. Everything below goes into one private folder (the table
above) that is outside the plugin, so it survives plugin updates. To remove it, close Claude and delete that folder.

1. **A Python is chosen.** The plugin needs a whole Python 3.11 or newer; 3.13 is the one it is tested on (python.org, or `winget install
   Python.Python.3.13` on Windows). It tests every Python it finds (the version, its own standard library in place, the `venv` module) and skips
   one that fails, such as a folder holding only `python.exe` and its DLLs. The Microsoft Store `python` stubs on Windows are skipped on purpose.
2. **The private environment is built** from that Python.
3. **The data server's packages go in first** (`mcp/requirements.txt`, exact versions, about a hundred megabytes). The server starts as soon as
   they are in.
4. **The map-building set follows** (`requirements.txt`: geopandas, shapely, matplotlib, Pillow, pypdf and the rest). It is installed at the tested
   versions in `constraints.txt`: each is the newest release the test suite ran on, and a release is refused when a pin is behind the newest
   release or has a known vulnerability. If one tested version cannot be installed on your computer, the newest that fit are installed instead and
   the window says so. A package already newer than its pin would be moved back to the tested one, which is why every release checks that the pins
   are the newest releases.
5. **The optional extras go in one at a time** (`requirements-optional.txt`: rasterio, duckdb, h3, playwright, keyring and others), so one with no
   build for your computer is skipped and named without stopping the rest. A map that needs a skipped extra says so.
6. **Chromium for flythroughs and web-map checks** (about 150 MB, once).

All together it is several hundred megabytes and a few minutes, and it needs the internet that one time. On **Windows** a window titled
"geo-spatial-mapping setup" opens by itself: leave it open until it says Done, press a key, then restart Claude. On **macOS and Linux** it runs
in the background. Meanwhile (4.0.0) a small stand-in answers for the data server with one tool, `setup_status`, so Claude can say what is
happening and what to do; the map tools appear after the restart. The full map-building set (steps 4 to 6) is installed only while the data
server is not running from the environment, so an install never changes packages under a running server.

It runs again only when a plugin update changes a package list, or when the environment was built on a Python that is not whole (it is rebuilt on
a good one, and says so).

**Checks before installing (4.0.0).** Setup first checks that its folder can be written, which Python it uses (and why any other was skipped),
that no certificate setting points to a missing file, and that `pypi.org` and `files.pythonhosted.org` answer. Each prints PASS or FAIL with
the fix. Everything printed is also in `setup.log`.

**If something goes wrong,** setup ends with a cause code, a one-line fix, the log's path and its last lines:

| Code | What it means | Fix |
|---|---|---|
| `NO_PYTHON` | no whole Python 3.11 or newer | `winget install Python.Python.3.13` (Windows); python.org or your package manager |
| `NO_WRITE` | the environment's folder cannot be written | free disk space, or ask IT to allow the folder |
| `PIP_CERT` | a certificate setting (`PIP_CERT`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, or pip's `cert`) names a missing file | `pip config unset global.cert` (or the key named), or point it at an existing PEM file. That run goes on without the setting |
| `PIP_TLS` | PyPI answers but its certificate is not trusted (a proxy that inspects TLS) | pip 24.2 or later reads the Windows certificate store; or ask IT for the root certificate as a PEM and `pip config set global.cert <file>` |
| `NO_NETWORK` | PyPI cannot be reached (name lookup, proxy, time-out, each said apart) | see below |
| `VENV_LOCKED` | the environment is in use ("Access is denied", "being used by another process", or the data server runs from it) | close Claude, then `setup.cmd /repair` |
| `IMPORT` | packages installed but do not import | `setup.cmd /repair` |
| `PIP` | another pip failure | the last pip line is shown; the whole message is in `setup.log` |
| `PIN_FALLBACK` | (a warning) the tested versions do not all install on this Python | the newest that fit were installed; Python 3.13 gets the tested set |

**Behind a corporate proxy.** Set `HTTPS_PROXY` (or `pip config set global.proxy http://host:port`) for a proxy. For a proxy that inspects TLS,
pip 24.2 and later trust what the operating system trusts, so nothing more is usually needed; otherwise ask IT for the root certificate as a
PEM file and `pip config set global.cert <file>`. Setup never adds a trusted host or turns certificate checks off: that is a decision for your
IT policy, not for an installer.

**Which Python.** 3.13 is the one setup recommends and the one tested; 3.12 and 3.14 get the same tested versions; 3.11 gets its own (numpy
2.4.6, pyproj 3.7.2), without Spilhaus maps, which need PROJ 9.6. To pick one yourself, add a line `python: C:\path\to\python.exe` to
`%USERPROFILE%\.geo-mapping\config.yaml` (an environment variable would not reach the Claude app's processes).

| File | What it tells you |
|---|---|
| `setup.log` | everything the latest setup printed, with `pip freeze` at the end |
| `setup.failed` | the cause code and its fix; the setup is not retried by itself. Fix it, then run the repair |
| `launch.log` | what the latest start decided and why |
| `server.log` | the data server's starts, stops, each tool's name and time, and any crash (never arguments or results) |
| `python-check.log` | each Python that was found and not used, with the reason |
| `setup.running` | a setup is in progress, or was interrupted; one more than a day old is treated as dead and ignored |

Run it by hand: **Windows** `mcp\setup.cmd` in the plugin's folder (PowerShell: `& "<plugin folder>\mcp\setup.cmd"`), `setup.cmd /repair` to start
again from nothing, `setup.cmd all` (with Claude closed) for the full map-building set; **macOS and Linux** `sh "<plugin folder>/mcp/setup.sh"`
with the same words. `map-doctor --setup` reads the logs on the computer it runs on; in Cowork ask the data server's `setup_status` tool.

**PDF.** The data server never makes PDFs. A map and its PDF are made where the map is built: in Cowork that is the session's Linux shell,
where PDF output works. On Windows, building maps in Windows' own Python needs WeasyPrint's GTK runtime for PDF, which pip cannot install;
SVG, PNG and web maps do not need it.

## Your brand

The plugin ships in the AcropolisDocs brand. To use your own, add a **brand pack**: nothing is rebuilt and the plugin is not changed.

```
/geo-spatial-mapping:brand new      a brand from a name, one to five colours and a logo (SVG is best; PNG or JPEG works)
/geo-spatial-mapping:brand add      a pack folder or a brand .json you were given
/geo-spatial-mapping:brand set      your default brand for new maps
/geo-spatial-mapping:brand          the brands here and where packs are kept
```

A pack changes the map's colours (derived once into map roles, with contrast and colour-vision checks), the logo, the names in file
properties, the output file prefix and an optional trademark line. It lives outside the plugin (`.geo-mapping/brands/<slug>/` in your project,
or a folder named in `GEO_MAPPING_BRANDS`), so a plugin update never removes it. Brands bundled: acropolisdocs.

## What is in the package

| Piece | Where |
|---|---|
| Skills and commands | `skills/` (each command is a skill of its own; the guide lists them) |
| Reference pages the skills read | `modules/` |
| The map engine, checks and pipelines | `scripts/`, `lints/` |
| Data server (OGC, ArcGIS, Census, geocoding, credentials) | `.mcp.json`, `mcp/` |
| Reference library of data sources, with terms | `library/` |
| Bundled world and US background data (Natural Earth, Census) | `assets/basemaps/` |
| Style packs (antique, blueprint, classical, editorial, monochrome, sketch) | `styles/` |
| The shipped brand pack | `brands/` |
| Your brands: list, add, make, set the default | `/geo-spatial-mapping:brand`, skill `geo-set-brand` |
## Python packages

`requirements.txt` lists what the maps need (a floor only where an older release misbehaves); it includes `mcp/requirements.txt`, the data
server's own exact pins. `requirements-optional.txt` holds the extras (rasterio, duckdb, h3, playwright and others); a map that needs one says
so. PDF output needs WeasyPrint's native libraries (Pango), which pip cannot install: the doctor's PDF line says when they are missing, and
SVG and PNG do not need them.

## License and NOTICE

Apache License 2.0 (`LICENSE`), copyright 2026 AcropolisDocs, LLC. You may use, change and redistribute the plugin, including commercially. Keep the
`NOTICE` file with every copy and every modified version, and credit AcropolisDocs, LLC (License section 4(d)).

- **Not covered by the licence:** the AcropolisDocs name and logo, and the shipped brand pack in `brands/`. A brand pack you add stays on your computer
  and is used on your own authority; its marks belong to their owners (`NOTICE`, "Trademarks and brand packs").
- **Bundled parts keep their own licences:** the Inter typeface and the style-pack typefaces (SIL Open Font License 1.1), Leaflet (BSD-2), MapLibre GL JS and
  pmtiles (BSD-3), Natural Earth and the U.S. Census boundary files (public domain). Library entries describe third-party datasets; each stays under its
  publisher's terms, and the credits those terms require are on your maps.
- **The Python packages installed on first start** (see above) are fetched from the Python Package Index onto your computer. They are not in the plugin
  file and not redistributed by it; each has its own licence, which `pip show NAME` names inside the private environment.

## Rules this plugin keeps

- Inter is the typeface. A brand changes colour, logo and voice, never type; a style pack may bring its own faces.
- The AcropolisDocs credit travels only as hidden tags and document properties. It is never drawn on a map.
- The brand tools answer only inside a map build and never style documents, decks or charts.
- Read-only toward services. Your keys stay in your own credential folder on your computer; they never enter the chat, a map, a log or the package.
- A logo in a brand pack must be a plain SVG drawing; one that carries scripts, links or embedded pages is refused.

Working files live in `.geo-mapping/` (defaults, added brand packs, library overlay, run records). Licence: Apache-2.0, see `LICENSE` and `NOTICE`.
