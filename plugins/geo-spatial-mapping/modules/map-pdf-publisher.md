---
name: map-pdf-publisher
description: >
  UTILITY MODULE - read by map-create and map-export. Turns a finished map SVG into a vector PDF of exact sheet size with Inter embedded,
  and into a PNG at a stated DPI, writing the hidden file properties; then checks the files it wrote.
---

# map-pdf-publisher

Library module `scripts/map_export.py` (also a command line used by `map-export`).

- `to_pdf_bytes(svg, title)`: WeasyPrint places the SVG on a page of exactly its viewBox size; Inter comes from the SVG's own embedded font face and is subset
  into the PDF; text stays text.
- `stamp_pdf(raw, props, title, subject)`: pikepdf writes Title, Author, Creator, Producer, dates and the hidden accreditation (Info and XMP). Dates are fixed
  when `GMAP_FIXED_DATE` is set so tests are repeatable. Properties come from `map_brand.props_for(slug, manifest, "pdf")`; this module never chooses a brand.
- `rasterize(pdf_bytes, dpi, svg)`: `pdftoppm`, or headless Chromium when poppler is missing; 72 to 1200 dpi.
- `stamp_png(png, dpi, props, title, alt)`: DPI, alt text and the hidden accreditation as PNG text chunks. Pixels are untouched.

The output lint (`output_lint.check_pdf`, `check_png`, `check_svg`) reads the written files back: one page of the template's size, every font embedded and
Inter only, no large raster images in a PDF, PNG pixel size equal to the template at the DPI, no external references, no keys or internal names.
