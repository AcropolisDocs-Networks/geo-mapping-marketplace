---
name: export-print-sheets
description: >
  KNOWLEDGE MODULE - read by map-export. Sheet sizes and custom sizes, and the files a print shop, a slide or a designer needs: bleed, crop marks, PDF/X notes, transparent PNG, outlined text.
---

# export-print-sheets

## Sheet sizes, and files for a print shop, a slide or a designer

Any of these goes on the same `build_map.py` run (or `map_export.py --svg` for an SVG already built), or under `export:` in the MapSpec.

| The person says | Do |
|---|---|
| "portrait", "make it tall", "A4", "A3", "for a European printer" | `--template letter-portrait`, `tabloid-portrait`, `arch-d-portrait`, `a4-portrait`, `a4-landscape`, `a3-portrait`, `a3-landscape` |
| "an Instagram or LinkedIn story", "9:16", "vertical video size" | `--template story` (1080 x 1920 px at its default 144 dpi) |
| "exactly 500 by 700 mm", "fit our display board" | `--template custom:500x700` (millimetres; 100 to 1,500 a side, at most 3:1), scaled from the nearest print sheet |
| "transparent background", "to drop on a slide", "no white box around it" | `--transparent` (the page outside the map frame is see-through, the frame keeps its background); `--transparent all` drops the frame's background too. PNG only |
| "with bleed", "the printer wants 3 mm bleed", "crop marks" | `--bleed 3 --crop-marks`: the PDF page grows by the bleed, the page colour runs into it, TrimBox and BleedBox are set, marks sit outside the bleed |
| "PDF/A", "for the archive", "for the records system" | `--pdfa`: PDF/A-2b, fonts embedded, the title and the hidden accreditation in the XMP |
| "for Inkscape", "so I can edit the layers", "for our designer" | `--inkscape`: each map layer and each piece of furniture (title, legend, locator, scale bar, footer) is an Inkscape layer |
| "outline the text", "the designer does not have Inter", "convert text to paths" | `--outline-text`: also `<name>_outlined_v<date>.svg`, every text drawn as Inter outlines; the words stay as each piece's name for search and screen readers |

```bash
python3 "$GMAP_ROOT/scripts/build_map.py" --spec map.yaml --out out --formats svg,pdf,png --template a4-portrait --bleed 3 --crop-marks --pdfa
python3 "$GMAP_ROOT/scripts/map_export.py" --svg out/<stem>.svg --out out --formats png --transparent --inkscape --outline-text
```

- Each variant is read back after it is written: the transparent PNG's corners and frame (MAP-OUT-014), the bleed PDF's boxes and painted bleed
  (MAP-OUT-015), the PDF/A metadata, output intent and fonts (MAP-OUT-016), the Inkscape layers (MAP-OUT-017), the outlined text (MAP-OUT-018).
  Report what the INFO says. For PDF/A say plainly that the file was checked here for its metadata, output intent and fonts, not with veraPDF.
- A transparent PNG keeps the theme's text colour for the title and footer: light theme for a light slide, `--theme dark` for a dark one.
- The PNG is always the sheet's own size; the bleed and crop marks are in the PDF only.
- A portrait, A-series or custom sheet fits its footer to its notes and gives the map the room that frees; the four landscape print sheets
  and the 16:9 slide keep fixed footers.
