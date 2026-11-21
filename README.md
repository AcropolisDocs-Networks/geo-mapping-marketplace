# acropolisdocs-maps: maps for Claude

The marketplace for **geo-mapping-acropolisdocs** 3.2.2, built for Claude Cowork: cartographic maps from a request or your own geodata (print maps as SVG,
PDF and PNG, interactive web maps, data conversion and analysis, public data services, travel-time areas and terrain).

## Install (Claude Cowork)

1. Open **Customize → Plugins**.
2. Choose **Add**, then **Add marketplace**, and enter `AcropolisDocs-Networks/geo-mapping-marketplace`.
3. Find **geo-mapping-acropolisdocs** under **Discover** and choose **Install**.

Python 3.11 or newer must be on the computer. The first start installs the plugin's Python packages by itself into a private environment
(a few minutes, once); restart the plugin when it says Done. On macOS and Linux run `sh mcp/use-posix.sh` once in the plugin's folder first.
Then ask "open the map guide".

A plugin installed in Cowork is saved to your Claude account, so it is in chat and in Claude Code as well (Claude Code picks it up when it
starts). In Claude Code alone: `/plugin marketplace add AcropolisDocs-Networks/geo-mapping-marketplace`, then `/plugin install geo-mapping-acropolisdocs@acropolisdocs-maps`.

The plugin ships in the AcropolisDocs brand; `/geo-mapping-acropolisdocs:brand new` makes your own from a name, a colour and a logo.

## Updates

In **Customize → Plugins**, choose **Check for updates** on this marketplace, or turn on **Sync automatically** once. Each release replaces
`plugins/geo-mapping-acropolisdocs/` here. A Team or Enterprise organization that syncs this repository from its own settings needs its own private copy, and
updates there sync when a pull request with the version bump is merged (a direct push does not trigger it) or with **Re-sync**.

Licence: Apache-2.0 (`plugins/geo-mapping-acropolisdocs/LICENSE` and `NOTICE`). Source, issues and security reports: https://acropolisdocs.com
