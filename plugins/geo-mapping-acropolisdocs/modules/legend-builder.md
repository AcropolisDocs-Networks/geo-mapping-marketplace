---
name: legend-builder
description: >
  UTILITY MODULE - read by map-create and map-restyle. Builds the legend model for a map from its layer symbology, measures it in Inter,
  fits it into the room the template allows, and reports missing or inconsistent legend entries.
---

# legend-builder

The module `scripts/legend.py` is a library used by the layout step; it has no command line of its own.

```python
import legend
block = legend.class_block(layer, classification, colors, tokens)      # classed bins, with No data when values are null
block = legend.points_block(layer, tokens)                              # symbol shapes
block = legend.proportional_block(layer, tokens, values, radius_for, fmt, color)
fit   = legend.fit_legend(blocks, max_w_mm, max_h_mm, columns=1, template_kind="print")
finds = legend.check_blocks(blocks, layers, classified)                 # MAP-CART-005 / 006
```

## Rules

- Every symbolized layer is represented. Basemap and context layers never get an entry (MAP-CART-005).
- Each classed block has a title that names the measure and its unit, and a "No data" swatch when any value is null.
- Labels are formatted by `classify-and-color`: one number of decimals, thousands separators, ranges that do not overlap
  (MAP-CART-006).
- Status maps (RAG) carry a **shape and a color** for every state; `status_block` refuses a state without a shape
  (MAP-A11Y-003).
- Proportional symbols are scaled by **area**, not radius (MAP-CART-010). The size key shows the smallest, a middle and the
  largest value.
- Sizing uses the bundled Inter's real advance widths (`textmeasure.py`). The legend tries more columns, then a smaller font,
  but never below 6 pt on print or 10 pt on slides. If it still does not fit it says so and the layout moves the legend or
  reports an overflow (MAP-SPEC-005); text is not silently shrunk.


## Proportional-symbol keys

The size key shows round values by default: the largest round number (1, 2, 2.5 or 5 x 10^n) at or below the data's maximum, then round values near half and a tenth of it
that sit inside the data's range, for example 10 / 50 / 100 for data from 5 to 126. Per layer, `legend: {values: nice | exact | [10, 50, 100], count: 3}`: `exact` restores the
data's smallest, middle and largest value; a list is used as given. Symbol sizes are always drawn from the same area scale as the map, so any value you list is sized correctly.
