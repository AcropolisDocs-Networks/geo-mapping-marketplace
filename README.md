# acropolisdocs-maps: maps for Claude

The Claude Code marketplace for **geo-mapping-acropolisdocs** 1.12.1: cartographic maps from a request or your own geodata (print maps as SVG, PDF and
PNG, interactive web maps, data conversion and analysis, public data services, travel-time areas and terrain).

## Install

```
/plugin marketplace add AcropolisDocs-Networks/geo-mapping-marketplace
/plugin install geo-mapping-acropolisdocs@acropolisdocs-maps
```

Python 3.11 or newer must be on the computer. The first start installs the plugin's Python packages by itself into a private environment
(a few minutes, once); restart the plugin when it says Done. On macOS and Linux run `sh mcp/use-posix.sh` once in the plugin's folder first.
Then ask "open the map guide".

The plugin ships in the AcropolisDocs brand; `/geo-mapping-acropolisdocs:brand new` makes your own from a name, a colour and a logo.

## Updates

`/plugin marketplace update acropolisdocs-maps`, then update the plugin in `/plugin`. Each release replaces `plugins/geo-mapping-acropolisdocs/` here.

Licence: Apache-2.0 (`plugins/geo-mapping-acropolisdocs/LICENSE` and `NOTICE`). Source, issues and security reports: https://acropolisdocs.com
