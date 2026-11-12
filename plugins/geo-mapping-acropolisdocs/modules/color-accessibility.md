---
name: color-accessibility
description: >
  KNOWLEDGE MODULE - read by map-create, map-restyle and webmap-create. Colour-vision-safe palettes, contrast for labels over fills, meaning
  carried by shape and text as well as colour, grayscale printing, and the lints that check each.
---

# color-accessibility

Read this when choosing or changing a palette, when an MAP-A11Y finding needs explaining, or when a person asks whether a map works for
colour-blind readers or in black and white. About 1 in 12 men and 1 in 200 women have a colour-vision deficiency, most often red-green
(deuteranomaly, protanomaly). The colours always come from the brand pack's map tokens (`map-brand`); this module says how to use them.

## Rules and the lint behind each

| Rule | Check | Lint |
|---|---|---|
| Adjacent classes stay apart under deuteranopia, protanopia and tritanopia simulation | minimum ΔE between neighbours after simulation | MAP-A11Y-001 |
| Labels read on what they sit on: 4.5:1, or a halo in the ground colour | WCAG contrast of label colour against fill and halo | MAP-A11Y-002 |
| Status (red, amber, green; on air, planned, blocked) is never colour alone | a shape, a pattern or a label per status | MAP-A11Y-003 (BLOCK on status maps) |
| The map survives grayscale printing | lightness steps between classes | MAP-A11Y-004 |
| Every picture has alt text; every page a text summary | alt text in PNG and HTML, `#gm-summary` | MAP-A11Y-005 |
| Single-symbol lines and points stand out from the land (3:1) | contrast against the land token | MAP-A11Y-007 |
| Two single-symbol data layers are told apart under simulation | ΔE after simulation | MAP-CART-016 |
| Bivariate keys: distinct cells, low-low apart from No data | ΔE and contrast read back from the SVG | MAP-A11Y-008 |

## Choosing a ramp

| Data | Ramp | Notes |
|---|---|---|
| Ordered, one direction (rate, density) | Sequential: light to dark in one hue family | Lightness carries the order, so it survives grayscale and CVD |
| Ordered around a meaningful middle (change, difference from target) | Diverging: two hues through a light neutral | Only with a real midpoint (MAP-CART-003); the middle class is the neutral |
| Categories | Categorical: at most 8 hues of similar lightness (MAP-CART-004) | Brand greys are demoted to Other and No data; a ninth category is grouped |
| Status | Brand status tokens plus a shape per status | Never red against green alone |

Keep the light end visibly different from the land and from the No data grey; keep the dark end from swallowing labels (use halos).

## Redundant encoding

Pair colour with something that is not colour: shape for points (`symbol.shape` by category: circle, square, triangle, diamond), dash for
lines, hatch patterns for areas (`map-restyle` "hatch patterns"), direct labels instead of a legend lookup, and the value in popups.

## Grayscale and print

Sequential ramps pass if their lightness steps are even; categorical palettes usually fail (similar lightness by design), so a print in
black and white needs shapes or patterns. Check with `map-qa` (MAP-A11Y-004) before sending a map to a printer that may be monochrome.

## Dark theme

On the dark theme the grounds swap: land darker than the page frame, labels light with dark halos. MAP-A11Y-006 checks the grounds still read
(the locator box at 3:1, land against page 1.2:1, water apart from land and page). Do not reuse a light-theme ramp's darkest class as a fill on
dark ground: it disappears. The pack's dark-mode tokens are already adjusted.

## Saying it to the person

Name the check, not the theory: "The five classes stay distinct for red-green colour blindness, and the status colours also have shapes, so
the map reads in black and white." When a brand colour fails, `map-brand` substitutes the nearest passing tone and says so (never silently).
