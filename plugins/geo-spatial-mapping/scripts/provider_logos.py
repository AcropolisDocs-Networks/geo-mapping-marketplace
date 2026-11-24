#!/usr/bin/env python3
"""provider_logos (1.1.3): the logo a data or tile provider's terms require beside its credit, on the printed map and on the web page.

A library entry asks for one in its attribution block:

    attribution:
      corner: Weather data provided by OpenWeather
      logo: {file: openweathermap.svg, height_px: 10, link: https://openweathermap.org/, alt: OpenWeather, required: true}

The file is one of the plugin's own, in assets/logos/ (a bare file name; nothing outside that folder is read). It is a plain SVG: no script, no
event handlers, no links, no embedded pictures or foreign content, and it carries `currentColor`, so it is drawn in the colour of the text
beside it on either theme. `height_px` is the logo's height on screen (at 96 px to the inch on paper: 10 px is 2.65 mm); the width follows
the drawing's own proportions.

    import provider_logos as pl
    lg = pl.load(entry["attribution"]["logo"])    # {"svg_inner", "viewbox", "aspect", "height_px", "link", "alt", "file"} or raises LogoError
    pl.inline_svg(lg, "gm-cr-logo")               # the web corner's markup
    pl.print_svg(lg, x_mm, y_mm, h_mm, colour)    # a nested <svg> for the print footer
    pl.for_plan(plan)                             # [(entry id, logo)] every logo the map's layers and background require, each once
"""
from __future__ import annotations

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGO_DIR = HERE.parent / "assets" / "logos"
PX_TO_MM = 25.4 / 96
FORBIDDEN = re.compile(r"<\s*(script|foreignObject|image|iframe|object|embed|use|style)\b|\son\w+\s*=|(?:xlink:)?href\s*=|javascript:", re.I)
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}\.svg$")


class LogoError(ValueError):
    pass


def spec_of(entry: dict | None) -> dict | None:
    """The entry's logo request, when it names a plugin logo file (a basemap provider's own hosted logo, Mapbox or MapTiler, is not one)."""
    a = (entry or {}).get("attribution")
    lg = a.get("logo") if isinstance(a, dict) else None
    return lg if isinstance(lg, dict) and lg.get("file") else None


def load(spec: dict) -> dict:
    name = str(spec.get("file") or "")
    if not NAME.match(name):
        raise LogoError(f"logo file '{name}' is not a plain file name in assets/logos")
    p = LOGO_DIR / name
    if not p.is_file():
        raise LogoError(f"logo file '{name}' is not in the plugin (assets/logos)")
    text = p.read_text(encoding="utf-8")
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if FORBIDDEN.search(re.sub(r'xmlns(:\w+)?="[^"]*"', "", body)):
        raise LogoError(f"logo file '{name}' holds a script, a link, a picture or a style; a logo is plain shapes")
    m = re.search(r"<svg\b([^>]*)>(.*)</svg>", body, re.S)
    if not m:
        raise LogoError(f"logo file '{name}' is not an SVG")
    vb = re.search(r'viewBox="([^"]+)"', m.group(1))
    if not vb:
        raise LogoError(f"logo file '{name}' has no viewBox")
    v = [float(x) for x in vb.group(1).replace(",", " ").split()]
    if len(v) != 4 or v[2] <= 0 or v[3] <= 0:
        raise LogoError(f"logo file '{name}' has a broken viewBox")
    h = float(spec.get("height_px") or 10)
    if not 4 <= h <= 64:
        raise LogoError(f"logo '{name}': height_px is between 4 and 64")
    link = str(spec.get("link") or "")
    if link and not link.startswith("https://"):
        raise LogoError(f"logo '{name}': its link is an https address")
    return {"file": name, "svg_inner": m.group(2).strip(), "viewbox": v, "aspect": v[2] / v[3], "height_px": h, "link": link,
            "alt": str(spec.get("alt") or name.rsplit(".", 1)[0]), "required": spec.get("required", True) is not False}


def width_px(lg: dict) -> float:
    return lg["height_px"] * lg["aspect"]


def inline_svg(lg: dict, cls: str) -> str:
    """The logo as inline SVG at its height, in the text colour, linked when the entry gives a link."""
    h = lg["height_px"]
    w = round(width_px(lg), 2)
    vb = " ".join(f"{x:g}" for x in lg["viewbox"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" width="{w:g}" height="{h:g}" viewBox="{vb}" role="img" '
           f'aria-label="{html.escape(lg["alt"])}" focusable="false" style="color:currentColor">{lg["svg_inner"]}</svg>')
    return svg


def print_svg(lg: dict, x: float, y: float, h: float, colour: str) -> str:
    """A nested <svg> at (x, y), h high (the print map's own units, mm), its shapes in `colour`. Unlinked: a print map refers to nothing outside it."""
    w = h * lg["aspect"]
    vb = " ".join(f"{v:g}" for v in lg["viewbox"])
    inner = lg["svg_inner"].replace("currentColor", colour)
    out = (f'<svg x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" viewBox="{vb}" role="img" aria-label="{html.escape(lg["alt"])}">'
           f"<title>{html.escape(lg['alt'])}</title>{inner}</svg>")
    return out                                    # no link: a printed map is self-contained (MAP-OUT-003); the credit text names the address


def for_entries(entries) -> tuple[list[tuple[str, dict]], list[str]]:
    """[(entry id, logo)] for the entries that ask for a logo, each file once, in order; and the problems (a logo that cannot be loaded)."""
    out, seen, probs = [], set(), []
    for e in entries:
        sp = spec_of(e)
        if not sp or sp.get("file") in seen:
            continue
        seen.add(sp.get("file"))
        try:
            out.append(((e or {}).get("id") or "", load(sp)))
        except LogoError as ex:
            probs.append(f"{(e or {}).get('id') or '?'}: {ex}")
    return out, probs


def for_plan(plan) -> tuple[list[tuple[str, dict]], list[str]]:
    """Every logo a print map's drawn layers and its background require."""
    import provenance as pv
    ents = [l.entry for l in pv.all_layers(plan) if getattr(l, "draw", True) is not False and l.entry]
    ents += [b.get("entry") for b in plan.basemap if b.get("entry")]
    return for_entries(ents)
