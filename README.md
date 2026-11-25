# geo-spatial-mapping: maps for Claude

The marketplace for **geo-spatial-mapping** 4.1.0, built for Claude Cowork: cartographic maps from a request or your own geodata (print maps as SVG,
PDF and PNG, interactive web maps, data conversion and analysis, public data services, travel-time areas and terrain).

## Install (Claude Cowork)

1. Open **Customize → Plugins**.
2. Choose **Add**, then **Add marketplace**, and enter `AcropolisDocs-Networks/geo-mapping-marketplace`.
3. Find **geo-spatial-mapping** under **Discover** and choose **Install**.

Or from the file: download `geo-spatial-mapping_v4.1.0.plugin` from this repository's [latest release](https://github.com/AcropolisDocs-Networks/geo-mapping-marketplace/releases/latest),
then in **Customize → Plugins** choose the upload option and select it.

Python 3.11 or newer must be on the computer. The first start installs the plugin's Python packages by itself into a private environment
(a few minutes, once); restart the plugin when it says Done. On macOS and Linux run `sh mcp/use-posix.sh` once in the plugin's folder first.
Then ask "open the map guide".

A plugin installed in Cowork is saved to your Claude account, so it is in chat and in Claude Code as well (Claude Code picks it up when it
starts). In Claude Code alone: `/plugin marketplace add AcropolisDocs-Networks/geo-mapping-marketplace`, then `/plugin install geo-spatial-mapping@geo-spatial-mapping`.

The plugin ships in the AcropolisDocs brand; `/geo-spatial-mapping:brand new` makes your own from a name, a colour and a logo.

## Updates

In **Customize → Plugins**, choose **Check for updates** on this marketplace, or turn on **Sync automatically** once. Each release replaces
`plugins/geo-spatial-mapping/` here. A Team or Enterprise organization that syncs this repository from its own settings needs its own private copy, and
updates there sync when a pull request with the version bump is merged (a direct push does not trigger it) or with **Re-sync**.

Licence: Apache-2.0 (`plugins/geo-spatial-mapping/LICENSE` and `NOTICE`). Source, issues and security reports: https://acropolisdocs.com
