---
name: map-layout
description: >
  UTILITY MODULE - read by map-create and map-restyle. Places the map frame, title, legend, scale bar, north arrow, inset and footer on a
  sheet template, keeps overlays off the data and off each other, and reports why anything moved.
---

# map-layout

Library modules used by `build_map.py`; no command line.

- `layout.load_template(name)`: reads `templates/<name>.yaml` (sheet size in mm, regions, type sizes, legend room) and refuses a template whose regions
  leave the sheet (MAP-SPEC-005).
- `layout.Frame.fit(rect, extent, padding_pct)`: fits the data extent into the map region, centered, with y flipped to sheet coordinates.
- `layout.occupancy(frame, geoms)` and `layout.place_overlays(frame, occupancy, items, banner_h)`: each overlay prefers a corner, moves to another when
  the corner is taken or would cover more than 30 percent of the data, and returns `moved_from` and a plain-language `reason`. Overlays never overlap one
  another; if there is no room the layout raises instead of overlapping.
- Default layout: title upper-left (no panel; thin halo), legend upper-right with the north arrow just left of it (`beside`), logo lower-left, as tall as the scale bar (top and bottom aligned) with the scale bar to its right (`beside`), the locator inset lower-right, zoomed out at most 5x from the main map (`inset_map.max_zoom_out`); an automatic locator keeps that corner (1.14.0) while it covers no more of the data than the elements may (`extent.fit_cover_pct`), and past that is left off with a note, never moved (give `elements.inset_map.position` a corner, or `auto` to let it move to a free corner and never cover data); source lines and the date stay in the footer under the map. `layout.apply_layout(template, spec, classic)` picks it: templates carry `map_full`, and `elements.title.placement: outside` (or the .pptx and QGIS paths) keeps the title band above the map. An element whose corner is taken stacks next to what is there before another corner is tried.
- Templates: letter-landscape, tabloid-landscape, arch-d, 16x9-slide, square-social; their portrait variants letter-portrait, tabloid-portrait,
  arch-d-portrait, story (9:16, the slide turned) and portrait-social (1.2.0: 4:5, 1080 x 1350 px, for a tall subject such as a city, Arizona or a
  corridor, which fills a third to a half of the square); ISO a4-landscape, a4-portrait, a3-landscape, a3-portrait (M16). A new
  template is a new YAML file; the build checks it.
- `layout.load_template({"custom": {"w_mm": 500, "h_mm": 700}})` (or the string `custom:500x700`): a custom sheet, 100 to 1,500 mm a side and at most
  3:1, scaled from the nearest print template (`nearest_template`: closest in shape, then in size). Margins, bands, legend and inset room, logo and
  type scale by the square root of the area ratio (type never under 6 pt); the map takes the rest of the height. Files are named `custom-<w>x<h>mm`.
- `footer_fit: true` (square-social, the portrait, A-series and custom sheets): `layout.fit_footer(template, need_mm)` shrinks the footer to what its
  notes need and grows the map region (and `map_full`) into the room, by at most 18 percent of the map's height so the plan's raster and context
  windows still cover the frame. The renderer calls it before the frame is fitted; a map with a tile basemap keeps the fixed footer.

Everything is measured in millimetres on the sheet, so the SVG viewBox equals the paper size.
