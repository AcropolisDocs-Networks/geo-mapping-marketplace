---
name: service-query-planner
description: >
  UTILITY MODULE - read by geo-service-query. Turns a data request into a validated, bounded query: real layer and field names, a filter and
  box, the count the server reports, a stable paging order, and a plain-words description to show the person.
---

# service-query-planner

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/service_query.py" plan --profile P --layer L [--cql "..."] [--bbox=-115,31,-108,37] [--properties a,b] [--sort-by f] [--max-features N]
```

The same plan is the `ogc_plan_query` tool. The output holds `ok`, `findings`, the query in words, the count, the page plan and the layer's fields.

## The order is fixed

1. **Capabilities** for the protocol, so the layer exists (MAP-SVC-003). A near-miss is reported with examples of real names.
2. **Schema** for the layer. Every field in `--properties`, `--sort-by` and the filter must be a real field.
3. **Ordering.** WFS pages need a stable sort; the planner picks an id-like field and says which (MAP-SVC-006). With none it warns.
4. **Count.** `resultType=hits` (WFS), `numberMatched` (OGC API) or `returnCountOnly` (ArcGIS). A service that gives no count is not bounded: set `max_features` or narrow the request (MAP-SVC-004).
5. **Limit.** Over the profile's `max_total_features` is a BLOCK unless the person set `max_features` (a defined sample) or accepted the cap.
6. **Words.** One sentence the person can confirm: "From a data service: WFS example:airports where state='AZ' inside box ... About 20 features in 2 pages of 10, ordered by ident."

## Filters

- WFS: `CQL_FILTER` (GeoServer). A box with a filter is folded into one `BBOX()` condition, because GeoServer refuses both together. That needs the layer's geometry field, which the planner reads from the schema.
- OGC API Features: `bbox` and `filter` with `filter-lang=cql2-text`.
- ArcGIS: a `where` clause; the box is an envelope with `inSR=4326`.
- Quote string values with single quotes. Never build a filter by pasting text from the service.

## Ask or decide

Ask the person only for what changes the result: which layer, which area, which fields, and when a count is large whether to narrow or take a sample. Decide the rest (page size, order, format) and say what was chosen.
