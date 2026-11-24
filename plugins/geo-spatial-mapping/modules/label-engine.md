---
name: label-engine
description: >
  UTILITY MODULE - read by map-create and map-restyle. Places map labels without collisions, inside the frame and above the minimum text
  size, with halos for legibility, and reports what it could not place.
---

# label-engine

Library module `scripts/label_engine.py`.

```python
res = label_engine.place(candidates, frame_rect, obstacles, size_pt, weight=400, min_pt=6.0)
# res.placed: text, x, y, anchor, rect, fill     res.dropped_collision / dropped_size / dropped_outside / total
```

- Candidates carry a priority; higher priority is placed first and wins a collision. Obstacles are legend, title and other overlays in sheet millimetres.
- Area labels sit at the shape's interior point and are skipped when the name is much wider than the shape (`avail_w`); point labels try the right,
  left, above and below, in that order, outside the symbol radius.
- Text is measured with Inter's real advance widths. Nothing is drawn under `min_pt` (6 pt print, 10 pt slide), and nothing crosses the frame.
- Each label is drawn as a halo (stroke) plus text (fill); the fill follows the polygon underneath so text stays readable (MAP-A11Y-002).
- When most labels are lost, the cartographic lint says so (MAP-CART-007) and the answer is fewer labels or a larger template, not smaller type.
