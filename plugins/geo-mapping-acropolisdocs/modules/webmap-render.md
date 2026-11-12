---
name: webmap-render
description: >
  KNOWLEDGE MODULE - read by webmap-create. How the page draws data: layers from MapSpec roles, classes and colors, legend blocks, popups,
  labels, the bundled background, the static fallback picture and the hidden brand metadata.
---

# webmap-render

- **Layers** keep the MapSpec order and roles. Choropleth classes, colors and the no-data class come from the same plan as the print map, so the page and the PDF agree. Points, proportional symbols and lines use the roles' sizes in screen pixels.
- **The legend** (the panel's Legend tab) has one block per layer, with the same titles as the print legend and an eye toggle; a hidden layer's block stays, greyed and marked hidden. The source line, the per-layer sources, the data licences and the notes are in the About tab; the credits the terms require are in the map's corner (`webmap-controls`).
- **Popups** show the fields the spec lists (`popup: {title, fields}`), or the label field and the value field. With the panel open a click fills the Details tab instead. Text is escaped; a value never becomes markup.
- **The background** is always the bundled country, state and lake outlines. A tile layer, when allowed, is drawn above it, so a failed tile request leaves the outlines in place.
- **Two engines, one look.** Leaflet draws GeoJSON in the page; MapLibre draws the same layers from GeoJSON or, for a large layer, from a
  PMTiles archive embedded in the page, with the same tokens, legend and chrome. On MapLibre the place names and the data's labels are symbol
  layers set in Inter (glyph files generated from the plugin's own Inter), shown by the same scale tiers as the print map; the data's symbols
  are placed first, so the names avoid them. Contours: index lines heavier and darker on both, elevations along them from zoom 12 on MapLibre.
- **Data** is embedded as GeoJSON in WGS 84 with coordinates rounded to 5 decimals (about 1 m) and only the attributes the page uses. A layer the person marked not downloadable never offers its data.
- **The static picture** is the SVG of the same map, shown if the engine cannot start, and inside `noscript`. A banner says what happened.
- **Text summary.** A visually hidden summary names the map message, the layers and their counts, for assistive technology.
- **Metadata.** The page carries the hidden accreditation (`data-accreditation`, meta tags) and the brand and edition stamps. None of it is visible text.
- **Theme.** `light` uses the brand's print colors, `dark` its screen colors. Both stay inside the brand tokens; a stray hex color is a BLOCK.
- **Print.** The print style sheet hides the panel, the rail and the dock and keeps the title chip, the legend, the brand chip and the credits on the page.
- **Brand.** The brand chip (logo and name) is the brand's visible mark, from the active brand pack; the dark theme uses the brand's dark logo. The house credit ("Powered by ...") is visible only in the About tab of the house brand's own maps; every page carries it in its metadata.
