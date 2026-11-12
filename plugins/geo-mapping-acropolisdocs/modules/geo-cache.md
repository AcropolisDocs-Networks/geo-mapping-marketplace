---
name: geo-cache
description: >
  UTILITY MODULE - read by terrain-analysis, reference-curate and map-doctor. One view of what the plugin keeps on disk between runs
  (library downloads, basemap tiles and composites, DEMs, service captures): size and age, hash checks, duplicates, and pruning that honours
  each tile provider's cache terms.
---

# geo-cache

The engine is `scripts/geo_cache.py`. Each tool keeps its downloads where it makes them; this reads them all as one content-addressed index
(every file by its SHA-256) and applies one set of keeping rules. It never fetches anything.

| Area | Folder | Kept for | Written by |
|---|---|---|---|
| library | `.geo-mapping/cache/library/` | 120 days | `ref_fetch` |
| tiles | `tiles/<provider>/<style>/z/x/y.png` | 30 days, or the provider's terms | `basemap_render` |
| basemaps | `basemaps/<map id>/` | 30 days | `basemap_render` |
| terrain | `terrain/` | 180 days | `terrain_fetch` |
| captures | `.geo-mapping/captures/` | never pruned unless named | service tools, geocode, PostGIS |

Provider terms come from the library's `basemap-provider` entries: `cache_ok: false` means its tiles are removed whatever their age (and
`basemap_render` keeps none); `min_cache_days` (OpenStreetMap: 7) means they are kept at least that long, so a re-render does not hit the
provider again.

```
python3 scripts/geo_cache.py status                         # every area: files, size, oldest, newest, past their time
python3 scripts/geo_cache.py prune --dry-run                # what would go; then without --dry-run
python3 scripts/geo_cache.py prune --area tiles --older-than 7
python3 scripts/geo_cache.py verify                         # the SHA-256 each record names, recomputed
python3 scripts/geo_cache.py duplicates                     # the same content kept twice
```

Captures are the evidence behind a map's provenance (the capture record names the file and its hash), so `prune` refuses them unless asked
with `--area captures --confirm`; say that to the person before they confirm, and only after the maps that use them are no longer needed. A
`verify` mismatch means a cached file changed after it was recorded: fetch it again rather than trust it.
