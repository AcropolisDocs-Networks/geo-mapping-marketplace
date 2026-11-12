#!/usr/bin/env python3
"""map-export: SVG master to PDF and PNG (spec 7.1 map-export, 12.7).

    map_export.py --svg MAP.svg --out DIR --formats pdf,png [--dpi 300]
    map_export.py --animate ATLAS_DIR --out DIR [--sizes square,slide] [--formats gif,mp4] [--hold 1.0] [--last-hold 3.0]

--animate (M15) takes the folder of an atlas made with `by: time` and writes an animated GIF and an MP4 of its periods at 1080 x 1080 and
1920 x 1080, each period redrawn at that size (animate.py). The MP4 needs ffmpeg (pip install imageio-ffmpeg); without it the GIF is still written.

PDF: WeasyPrint places the SVG on a page of exactly the template's size, with Inter embedded as a subset and the text kept as text,
then pikepdf writes the Info dictionary and XMP with the hidden accreditation. PNG: the PDF is rasterized by pypdfium2 (pdftoppm, then Chromium, when it is missing) at the
requested DPI, and the accreditation goes into PNG text chunks. Nothing visible is added. The PDF libraries load through pdf_stack.py.

Export options (M16, gap analysis P1-10), on the command line here and in build_map.py, or under `export:` in the MapSpec:

    --transparent [page|all]   PNG with no page fill: everything outside the map frame is see-through; the frame keeps its own background
                               (the sea, or the page colour) unless `all`, when only what is drawn on the map stays opaque.   export.transparent
    --bleed MM [--crop-marks]  print PDF: the page grows by MM on every side, the page colour runs into that bleed, TrimBox is the sheet and
                               BleedBox the sheet plus the bleed; crop marks (in the brand's text colour) sit outside the bleed.  export.bleed_mm, crop_marks
    --pdfa                     PDF/A-2b: WeasyPrint's PDF/A variant (sRGB output intent, fonts embedded), XMP with the title and the hidden
                               accreditation, the Info dictionary and the XMP kept in agreement.                               export.pdfa
    --inkscape                 the SVG with every gm-layer-* group and the furniture groups as Inkscape layers (inkscape:groupmode and
                               inkscape:label, the namespace declared); nothing else in the file changes.                     export.inkscape
    --outline-text             also <stem>_outlined_v<date>.svg: every text drawn as Inter glyph outlines (fontTools, kerned, tabular figures
                               where the map asks for them), each with its words as its accessible name and listed in the metadata.  export.outline_text
    --social                   also <stem>_social_v<date>.png: 2160 px wide and under 5 MB for X; full colour when it fits, else an adaptive
                               256-colour palette, else that at half the width (1.2.0).                                         export.social

Each is read back by output_lint (MAP-OUT-014 to MAP-OUT-018). A large PNG (over PNG_OPTIMIZE_MAX_PIXELS) is written at zlib level 6 instead
of Pillow's optimize pass, which took six times as long for files 2 to 6 percent smaller (measured on letter maps from 2 to 60 megapixels).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

MM_PER_IN = 25.4
PT_PER_MM = 72.0 / 25.4
MIN_DPI, MAX_DPI = 72, 1200
MAX_PIXELS = 160_000_000
PNG_OPTIMIZE_MAX_PIXELS = 12_000_000      # above this Pillow's optimize pass costs 2.5 to 13 s for 2 to 6 percent (M16 bench); zlib level 6 instead
MAX_BLEED_MM = 25.0
CROP_MARK_OFFSET_MM = 1.0                 # crop marks start this far outside the bleed edge, so a mark never prints into the bleed
CROP_MARK_LEN_MM = 5.0
CROP_MARK_W_MM = 0.1                      # 0.28 pt: the usual registration-mark weight
TRANSPARENT_MODES = ("page", "all")
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
FURNITURE = {"gm-legend": "Legend", "gm-scalebar": "Scale bar", "gm-north": "North arrow", "gm-inset": "Locator map", "gm-title": "Title",
             "gm-footer": "Footer: sources and notes", "gm-labels": "Labels", "gm-names": "Place names", "gm-contour-labels": "Contour labels",
             "gm-graticule": "Graticule", "gm-crop-marks": "Crop marks"}


class ExportError(Exception):
    pass


def options_from(export_spec: dict | None, overrides: dict | None = None) -> dict:
    """The export options in one form: {transparent: None|'page'|'all', bleed_mm: float, crop_marks: bool, pdfa: bool, inkscape: bool,
    outline_text: bool}, from the MapSpec's `export:` block with command-line `overrides` on top (a None override leaves the spec's value)."""
    e = dict(export_spec or {})
    for k, v in (overrides or {}).items():
        if v is not None:
            e[k] = v
    tr = e.get("transparent")
    tr = "page" if tr is True or tr == "outside" else (None if tr in (None, False, "none", "off") else str(tr))
    if tr is not None and tr not in TRANSPARENT_MODES:
        raise ExportError(f"transparent is page or all, not '{tr}'")
    bleed = float(e.get("bleed_mm") or 0.0)
    if not (0.0 <= bleed <= MAX_BLEED_MM):
        raise ExportError(f"a bleed of {bleed:g} mm is outside 0 to {MAX_BLEED_MM:g} mm (3 mm is usual)")
    marks = bool(e.get("crop_marks", False))
    return {"transparent": tr, "bleed_mm": bleed, "crop_marks": marks, "pdfa": bool(e.get("pdfa", False)),
            "inkscape": bool(e.get("inkscape", False)), "outline_text": bool(e.get("outline_text", False)), "social": bool(e.get("social", False))}


SOCIAL_WIDTH_PX = 2160              # 1.2.0 (Day 6 issue 7): the social PNG's width (X shows it sharp at 1080 on a 2x screen)
SOCIAL_MAX_BYTES = 5_000_000        # X's image limit


def social_png(pdf_bytes: bytes, svg: str, props: dict, title: str, alt: str, *, width_px: int = SOCIAL_WIDTH_PX,
               max_bytes: int = SOCIAL_MAX_BYTES) -> tuple[bytes, dict]:
    """A PNG for a social post: `width_px` wide, under `max_bytes`. Full colour when it fits; else an adaptive 256-colour palette (the map's
    data colours are few and survive it); else the palette at half the width. Returns (png, {width, height, colours, bytes, step})."""
    from PIL import Image
    w_mm, _h = svg_size_mm(svg)
    dpi = width_px / (w_mm / 25.4)
    full = stamp_png(rasterize(pdf_bytes, dpi, svg), dpi, props, title, alt)
    if len(full) <= max_bytes:
        im = Image.open(io.BytesIO(full))
        return full, {"width": im.size[0], "height": im.size[1], "colours": "full", "bytes": len(full), "step": "full colour", "dpi": round(dpi, 2)}
    last = None
    for scale in (1.0, 0.5):
        src = full if scale == 1.0 else stamp_png(rasterize(pdf_bytes, dpi * scale, svg), dpi * scale, props, title, alt)
        im = Image.open(io.BytesIO(src)).convert("RGB")
        q = im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        buf = io.BytesIO()
        q.save(buf, "PNG", optimize=True, dpi=(dpi * scale, dpi * scale))
        pal = stamp_png(buf.getvalue(), dpi * scale, props, title, alt)
        last = (pal, {"width": q.size[0], "height": q.size[1], "colours": 256, "bytes": len(pal), "dpi": round(dpi * scale, 2),
                      "step": "256-colour palette" + ("" if scale == 1.0 else " at half the width")})
        if len(pal) <= max_bytes:
            return last
    return last


# ---------------------------------------------------------------------------------------------------------------- SVG variants (M16)
def _drop_rect(svg: str, cls: str) -> str:
    return re.sub(r'<rect\b[^>]*\bclass="' + re.escape(cls) + r'"[^>]*/>', "", svg, count=1)


def transparent_svg(svg: str, mode: str = "page") -> str:
    """The sheet with no page fill (`page`), or also without the map frame's own background, the sea or page colour under the data (`all`)."""
    if mode not in TRANSPARENT_MODES:
        raise ExportError(f"transparent is page or all, not '{mode}'")
    out = _drop_rect(svg, "gm-page")
    if mode == "all":
        out = _drop_rect(out, "gm-frame-bg")
    return out


def frame_rect_mm(svg: str) -> tuple[float, float, float, float] | None:
    """(x, y, w, h) of the map frame, from its clip path."""
    m = re.search(r'<clipPath id="gm-clip"><rect x="([-0-9.]+)" y="([-0-9.]+)" width="([0-9.]+)" height="([0-9.]+)"', svg)
    return tuple(float(v) for v in m.groups()) if m else None


def brand_ink(svg: str) -> str:
    """The brand's text colour in the light (print) theme, for crop marks on the paper outside the bleed."""
    m = re.search(r'data-map-brand="([^"]+)"', svg)
    try:
        import brandlib as bl
        return str(bl.load_pack(m.group(1) if m else "acropolisdocs")["tokens"]["modes"]["print"]["label"])
    except Exception:  # noqa: BLE001 - a sheet without a brand stamp falls back to the house brand's ink
        import brandlib as bl
        return str(bl.load_pack("acropolisdocs")["tokens"]["modes"]["print"]["label"])


def bleed_svg(svg: str, bleed_mm: float, crop_marks: bool = False) -> tuple[str, dict]:
    """The sheet on a larger page: `bleed_mm` on every side, filled with the page colour, and, with crop marks, a further slug for the marks.
    Returns (svg, boxes) with boxes in mm from the page's lower-left corner: media, bleed and trim, as (x0, y0, x1, y1)."""
    W, H = svg_size_mm(svg)
    b = float(bleed_mm)
    slug = (CROP_MARK_OFFSET_MM + CROP_MARK_LEN_MM + 1.0) if crop_marks else 0.0
    pad = b + slug
    PW, PH = W + 2 * pad, H + 2 * pad
    m = re.search(r"<svg\b[^>]*>", svg)
    root = m.group(0)
    root = re.sub(r'viewBox="[^"]*"', f'viewBox="{-pad:g} {-pad:g} {PW:g} {PH:g}"', root, count=1)
    root = re.sub(r'\bwidth="[^"]*"', f'width="{PW:g}mm"', root, count=1)
    root = re.sub(r'\bheight="[^"]*"', f'height="{PH:g}mm"', root, count=1)
    body = svg[m.end():]

    def grow(mm):
        tag = mm.group(0)
        tag = re.sub(r'\bx="[^"]*"', f'x="{-b:g}"', tag, count=1)
        tag = re.sub(r'\by="[^"]*"', f'y="{-b:g}"', tag, count=1)
        tag = re.sub(r'\bwidth="[^"]*"', f'width="{W + 2 * b:g}"', tag, count=1)
        return re.sub(r'\bheight="[^"]*"', f'height="{H + 2 * b:g}"', tag, count=1)
    body = re.sub(r'<rect\b[^>]*\bclass="gm-page"[^>]*/>', grow, body, count=1)
    if crop_marks:
        o, L = b + CROP_MARK_OFFSET_MM, CROP_MARK_LEN_MM
        segs = []
        for x in (0.0, W):
            segs += [f"M{x:g} {-o:g}V{-o - L:g}", f"M{x:g} {H + o:g}V{H + o + L:g}"]
        for y in (0.0, H):
            segs += [f"M{-o:g} {y:g}H{-o - L:g}", f"M{W + o:g} {y:g}H{W + o + L:g}"]
        marks = (f'<g class="gm-crop-marks" fill="none" stroke="{brand_ink(svg)}" stroke-width="{CROP_MARK_W_MM:g}">'
                 f'<path d="{"".join(segs)}"/></g>')
        body = body[:body.rindex("</svg>")] + marks + "</svg>"
    boxes = {"media": (0.0, 0.0, PW, PH), "bleed": (slug, slug, PW - slug, PH - slug), "trim": (pad, pad, pad + W, pad + H),
             "bleed_mm": b, "crop_marks": bool(crop_marks), "sheet_mm": (W, H)}
    return svg[:m.start()] + root + body, boxes


def inkscape_svg(svg: str) -> str:
    """Every `gm-layer-*` group, the map group and the furniture groups as Inkscape layers: inkscape:groupmode="layer" and an inkscape:label,
    with the namespace declared on the root. Only those attributes are added; nothing else in the file changes."""
    if "xmlns:inkscape=" not in svg[:2000]:
        svg = re.sub(r"<svg\b", f'<svg xmlns:inkscape="{INKSCAPE_NS}"', svg, count=1)

    def label_for(attrs: str) -> str | None:
        gid = re.search(r'\bid="([^"]*)"', attrs)
        gid = gid.group(1) if gid else ""
        if gid == "gm-map":
            return "Map"
        if gid.startswith("gm-layer-"):
            lid = re.search(r'\bdata-layer="([^"]*)"', attrs)
            role = re.search(r'\bdata-role="([^"]*)"', attrs)
            name = lid.group(1) if lid else gid[len("gm-layer-"):]
            return f"Layer: {name}" + (f" ({role.group(1)})" if role else "")
        cls = re.search(r'\bclass="([^"]*)"', attrs)
        classes = cls.group(1).split() if cls else []
        if "gm-base" in classes:
            kind = next((c[len("gm-base-"):] for c in classes if c.startswith("gm-base-")), "")
            return f"Basemap: {kind.replace('-', ' ')}" if kind else "Basemap"
        for c in classes:
            if c in FURNITURE:
                return FURNITURE[c]
        return None

    def tag(mm):
        attrs = mm.group(1)
        if "inkscape:groupmode" in attrs:
            return mm.group(0)
        lab = label_for(attrs)
        if lab is None:
            return mm.group(0)
        safe = lab.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        return f'<g{attrs} inkscape:groupmode="layer" inkscape:label="{safe}">'
    head, sep, rest = svg.partition("<defs>")
    if not sep:
        return re.sub(r"<g\b([^>]*)>", tag, svg)
    defs, sep2, body = rest.partition("</defs>")
    return head + sep + defs + sep2 + re.sub(r"<g\b([^>]*)>", tag, body)


def strip_inkscape(svg: str) -> str:
    """inkscape_svg undone: what MAP-OUT-017 compares with the master SVG."""
    svg = svg.replace(f' xmlns:inkscape="{INKSCAPE_NS}"', "", 1)
    return re.sub(r' inkscape:groupmode="layer" inkscape:label="[^"]*"', "", svg)


# ---------------------------------------------------------------------------------------------------------------- outlined text (M16)
class _Face:
    """Inter at one weight, for drawing glyph outlines: the glyph set, advances, the `kern` pairs of GPOS and the `tnum` digits of GSUB."""

    def __init__(self, weight: int):
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        import textmeasure as tm
        f = TTFont(str(tm.font_file()))
        if "fvar" in f:
            f = instancer.instantiateVariableFont(f, {"wght": weight}, inplace=False)
        self.font = f
        self.upm = f["head"].unitsPerEm
        self.cmap = f.getBestCmap()
        self.glyphs = f.getGlyphSet()
        self.hmtx = f["hmtx"]
        self.pairs, self.classes = self._kerning(f)
        self.tnum = self._single_subst(f, "tnum")

    @staticmethod
    def _lookups(table, feature: str) -> list:
        if table is None or not getattr(table.table, "FeatureList", None):
            return []
        idx = set()
        for fr in table.table.FeatureList.FeatureRecord:
            if fr.FeatureTag == feature:
                idx.update(fr.Feature.LookupListIndex)
        out = []
        for i in sorted(idx):
            lk = table.table.LookupList.Lookup[i]
            for st in lk.SubTable:
                if lk.LookupType in (7, 9) and hasattr(st, "ExtSubTable"):          # GSUB 7 and GPOS 9 are extensions
                    st = st.ExtSubTable
                out.append(st)
        return out

    def _kerning(self, f):
        pairs: dict = {}
        classes: list = []
        for st in self._lookups(f.get("GPOS") if "GPOS" in f else None, "kern"):
            if getattr(st, "LookupType", 2) != 2:
                continue
            cov = st.Coverage.glyphs
            if st.Format == 1:
                for g1, ps in zip(cov, st.PairSet):
                    for rec in ps.PairValueRecord:
                        v = getattr(rec.Value1, "XAdvance", 0) if rec.Value1 else 0
                        if v:
                            pairs.setdefault((g1, rec.SecondGlyph), v)
            elif st.Format == 2:
                classes.append((set(cov), st.ClassDef1.classDefs, st.ClassDef2.classDefs, st.Class1Record))
        return pairs, classes

    def kern(self, a: str, b: str) -> int:
        v = self.pairs.get((a, b))
        if v is not None:
            return v
        for cov, cd1, cd2, recs in self.classes:
            if a in cov:
                r = recs[cd1.get(a, 0)].Class2Record[cd2.get(b, 0)]
                return int(getattr(r.Value1, "XAdvance", 0) or 0) if r.Value1 else 0
        return 0

    def _single_subst(self, f, feature: str) -> dict:
        out: dict = {}
        for st in self._lookups(f.get("GSUB") if "GSUB" in f else None, feature):
            if hasattr(st, "mapping"):
                out.update(st.mapping)
        return out

    def glyph(self, ch: str, tnum: bool) -> str:
        g = self.cmap.get(ord(ch)) or self.cmap.get(0xFFFD) or ".notdef"
        return self.tnum.get(g, g) if tnum else g


_FACES: dict = {}


def _face(weight: int) -> _Face:
    w = min((400, 500, 600, 700), key=lambda v: abs(v - int(weight)))
    if w not in _FACES:
        _FACES[w] = _Face(w)
    return _FACES[w]


_HB: dict = {}


def _hb_font(fc: "_Face", weight: int):
    """1.0.0 (TODO M16): a HarfBuzz font for the instanced face, when uharfbuzz is installed (optional); None otherwise."""
    if weight in _HB:
        return _HB[weight]
    try:
        import io
        import uharfbuzz as hb
        buf = io.BytesIO()
        flavor = fc.font.flavor
        fc.font.flavor = None                         # HarfBuzz reads plain sfnt, not the WOFF2 the plugin ships
        try:
            fc.font.save(buf)
        finally:
            fc.font.flavor = flavor
        _HB[weight] = hb.Font(hb.Face(hb.Blob(buf.getvalue())))
    except Exception:  # noqa: BLE001 - not installed, or this build cannot load the font: the fontTools path below
        _HB[weight] = None
    return _HB[weight]


def _shape(fc: "_Face", weight: int, s: str, tnum: bool):
    """[(glyph name, x advance, x offset, y offset)] in font units, shaped by HarfBuzz with kern, liga, calt (and tnum when asked): the
    ligatures and contextual forms (Inter's arrows from "->", for one) come out as the live text draws them. None without uharfbuzz."""
    f = _hb_font(fc, weight)
    if f is None:
        return None
    import uharfbuzz as hb
    b = hb.Buffer()
    b.add_str(s)
    b.guess_segment_properties()
    hb.shape(f, b, {"kern": True, "liga": True, "calt": True, "tnum": bool(tnum)})
    order = fc.font.getGlyphOrder()
    return [(order[i.codepoint], p.x_advance, p.x_offset, p.y_offset) for i, p in zip(b.glyph_infos, b.glyph_positions)]


def text_path(s: str, x: float, y: float, size_mm: float, weight: int = 400, anchor: str = "start", tnum: bool = False,
              tracking_mm: float = 0.0) -> tuple[str, float]:
    """(path data, advance in mm) for `s` set in Inter at `size_mm`, its baseline starting at (x, y) as SVG's text-anchor places it, kerned;
    shaped by HarfBuzz (ligatures and contextual forms too) when uharfbuzz is installed."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    fc = _face(weight)
    k = size_mm / fc.upm
    shaped = _shape(fc, min((400, 500, 600, 700), key=lambda v: abs(v - int(weight))), s, tnum)
    if shaped is not None:
        names = [g for g, _a, _dx, _dy in shaped]
        adv = [a * k + tracking_mm for _g, a, _dx, _dy in shaped]
        offs = [(dx * k, dy * k) for _g, _a, dx, dy in shaped]
    else:
        names = [fc.glyph(ch, tnum) for ch in s]
        offs = [(0.0, 0.0)] * len(names)
        adv = []
        for i, g in enumerate(names):
            a = fc.hmtx[g][0] * k if g in fc.hmtx.metrics else 0.0
            if i + 1 < len(names):
                a += fc.kern(g, names[i + 1]) * k
            adv.append(a + tracking_mm)
    total = sum(adv) - (tracking_mm if names else 0.0)
    x0 = x - (total / 2 if anchor == "middle" else total if anchor == "end" else 0.0)
    pen = SVGPathPen(fc.glyphs, ntos=lambda v: (f"{v:.3f}".rstrip("0").rstrip(".") or "0"))
    cx = x0
    for g, a, (dx, dy) in zip(names, adv, offs):
        if g in fc.glyphs:
            fc.glyphs[g].draw(TransformPen(pen, (k, 0, 0, -k, cx + dx, y - dy)))
        cx += a
    return pen.getCommands(), total


def outline_text_svg(svg: str) -> tuple[str, dict]:
    """Every <text> drawn as Inter glyph outlines: a <path> with the text's own paint, transform, id and data attributes. A text's fill pass
    becomes a group whose accessible name and <title> are the words; its halo pass (a stroked copy under it) is a path hidden from screen
    readers. The words are also listed in the root <metadata> (geo:outlined_text), and the @font-face is dropped: nothing needs Inter.
    Returns (svg, {texts, halos, chars})."""
    from lxml import etree
    SVG = "{http://www.w3.org/2000/svg}"
    root = etree.fromstring(svg.encode("utf-8"), etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True))
    stats = {"texts": 0, "halos": 0, "chars": 0}
    words: list[str] = []
    keep = ("fill", "fill-opacity", "stroke", "stroke-width", "stroke-linejoin", "stroke-opacity", "opacity", "transform", "id", "visibility")
    for el in list(root.iter(SVG + "text")):
        s = "".join(el.itertext())
        a = el.attrib
        if not s.strip():
            el.getparent().remove(el)
            continue
        size = float(a.get("font-size") or 3.0)
        d, _w = text_path(s, float(a.get("x") or 0), float(a.get("y") or 0), size, int(float(a.get("font-weight") or 400)),
                          a.get("text-anchor") or "start", "gm-num" in (a.get("class") or ""), float(a.get("letter-spacing") or 0))
        path = etree.Element(SVG + "path")
        path.set("d", d or "M0 0")
        for k_ in keep:
            if a.get(k_) is not None and k_ != "id":
                path.set(k_, a.get(k_))
        halo = a.get("stroke") not in (None, "none")
        if halo:
            path.set("class", "gm-outlined-halo")
            path.set("aria-hidden", "true")
            for k_, v in a.items():
                if k_.startswith("data-"):
                    path.set(k_, v)
            stats["halos"] += 1
            new = path
        else:
            new = etree.Element(SVG + "g")
            new.set("class", "gm-outlined")
            new.set("role", "img")
            new.set("aria-label", s)
            for k_, v in a.items():
                if k_ == "id" or k_.startswith("data-"):
                    new.set(k_, v)
            t_ = etree.SubElement(new, SVG + "title")
            t_.text = s
            new.append(path)
            stats["texts"] += 1
            stats["chars"] += len(s)
            words.append(s)
        new.tail = el.tail
        el.getparent().replace(el, new)
    for st in root.iter(SVG + "style"):
        if st.text and "@font-face" in st.text:
            st.text = re.sub(r"@font-face\s*\{.*?\}\s*", "", st.text, flags=re.S)
    GEO = "{https://acropolisdocs.com/ns/geo-mapping#}"
    RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
    desc = root.find(".//" + RDF + "Description")
    if desc is not None:
        ot = etree.SubElement(desc, GEO + "outlined_text")
        ot.text = "\n".join(words)
    root.set("data-text-outlined", "true")
    return etree.tostring(root, encoding="unicode"), stats


def _fixed_or_now() -> dt.datetime:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    if fixed:
        return dt.datetime.fromisoformat(fixed).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def svg_size_mm(svg: str) -> tuple[float, float]:
    """The sheet size from the root element's viewBox (its origin may be negative: a page with bleed, M16)."""
    root = re.search(r"<svg\b[^>]*>", svg)
    m = re.search(r'viewBox="(-?[0-9.]+) (-?[0-9.]+) ([0-9.]+) ([0-9.]+)"', root.group(0)) if root else None
    if not m:
        raise ExportError("the SVG has no viewBox, so its sheet size is unknown")
    return float(m.group(3)), float(m.group(4))


def to_pdf_bytes(svg: str, title: str = "", *, pdfa: bool = False) -> bytes:
    """Vector PDF of exactly the sheet size. Fonts come from the @font-face inside the SVG, copied to the page. `pdfa` asks WeasyPrint for its
    PDF/A-2b variant (PDF 1.7, an sRGB output intent, the pdfaid XMP), which stamp_pdf then completes."""
    import pdf_stack
    W, H = svg_size_mm(svg)
    faces = "\n".join(re.findall(r"@font-face\s*\{.*?\}\s*", svg, re.S))
    safe = re.sub(r"[<>&]", "", title or "Map")
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{safe}</title><style>{faces}'
            f"@page{{size:{W}mm {H}mm;margin:0}}html,body{{margin:0;padding:0;font-family:Inter,sans-serif}}"
            f"svg{{display:block;width:{W}mm;height:{H}mm}}</style></head><body>{svg}</body></html>")
    return pdf_stack.html_to_pdf(html, pdf_variant="pdf/a-2b" if pdfa else None)


def stamp_pdf(raw: bytes, props: dict, title: str, subject: str = "", *, pdfa: bool = False, boxes: dict | None = None) -> bytes:
    """Info dictionary plus XMP carrying the hidden accreditation. Dates are fixed when GMAP_FIXED_DATE is set.

    `boxes` (from bleed_svg) sets the page's BleedBox and TrimBox. With `pdfa` the Info entries that have an XMP twin (Title, Author, Subject,
    Creator, Producer, the two dates) are written so the two agree, as PDF/A requires: the subject is dc:description, the creator tool is the
    Info Creator, and the accreditation's address and the brand pack go into dc:relation instead of dc:description. (Without `pdfa` the
    mirroring pikepdf does leaves /Subject as the accreditation line, as every PDF before M16 had it.)"""
    import pikepdf
    now = _fixed_or_now()
    stamp = now.strftime("D:%Y%m%d%H%M%S+00'00'")
    iso = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    pdf = pikepdf.open(io.BytesIO(raw))
    info = props["Info"]
    x = props["XMP"]
    subj = subject or info.get("Subject", "")
    with pdf:
        di = pdf.docinfo
        di[pikepdf.Name.Title] = title
        di[pikepdf.Name.Author] = info["Author"]
        di[pikepdf.Name.Creator] = x["creator_tool"] if pdfa else info["Creator"]
        di[pikepdf.Name.Producer] = info["Producer"]
        di[pikepdf.Name.Subject] = subj
        di[pikepdf.Name.CreationDate] = stamp
        di[pikepdf.Name.ModDate] = stamp
        di[pikepdf.Name("/Accreditation")] = x["accreditation"]
        di[pikepdf.Name("/AccreditationURL")] = x["accreditation_url"]
        di[pikepdf.Name("/BrandPack")] = x["brand_pack"]
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:          # pikepdf mirrors each XMP twin into the Info dictionary
            meta["dc:title"] = title
            meta["dc:creator"] = [info["Author"]]
            meta["dc:publisher"] = [x["accreditation"]]
            meta["xmp:CreatorTool"] = x["creator_tool"]
            meta["xmp:CreateDate"] = iso
            if pdfa:
                meta["dc:description"] = subj
                meta["dc:relation"] = [x["accreditation_url"], f"brand pack {x['brand_pack']}"]
                meta["xmp:ModifyDate"] = iso
                meta["xmp:MetadataDate"] = iso
                meta["pdf:Producer"] = info["Producer"]
            else:
                meta["dc:description"] = f"{x['accreditation_url']} | {x['brand_pack']}"
        if boxes:
            pg = pdf.pages[0]
            pg.obj[pikepdf.Name.BleedBox] = pikepdf.Array([round(v * PT_PER_MM, 4) for v in boxes["bleed"]])
            pg.obj[pikepdf.Name.TrimBox] = pikepdf.Array([round(v * PT_PER_MM, 4) for v in boxes["trim"]])
        out = io.BytesIO()
        pdf.save(out, deterministic_id=True)
    return out.getvalue()


def rasterize(pdf_bytes: bytes, dpi: float, svg: str | None = None, *, alpha: bool = False) -> bytes:
    """PDF to PNG bytes at `dpi`. pypdfium2 first (the acropolisdocs page rasterizer); then pdftoppm; headless Chromium when neither is there.
    `alpha` keeps what the page leaves unpainted see-through (RGBA); pdftoppm cannot, so a transparent PNG goes to Chromium without pdfium."""
    if not (MIN_DPI <= dpi <= MAX_DPI):
        raise ExportError(f"dpi {dpi:g} is outside {MIN_DPI}-{MAX_DPI}")
    W, H = svg_size_mm(svg) if svg else (None, None)
    if W and (W / MM_PER_IN * dpi) * (H / MM_PER_IN * dpi) > MAX_PIXELS:
        raise ExportError("the requested size is too large to rasterize")
    import pdf_stack
    if pdf_stack.pdfium_available():
        try:
            return pdf_stack.render_png(pdf_bytes, dpi, alpha=alpha)
        except Exception:  # noqa: BLE001
            pass                                           # fall through to pdftoppm
    if alpha:
        if svg is not None:
            return _chromium_png(svg, dpi, alpha=True)
        raise ExportError("a transparent PNG needs pypdfium2 or Playwright Chromium; pdftoppm draws on white")
    exe = shutil.which("pdftoppm")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "m.pdf"
        src.write_bytes(pdf_bytes)
        if exe:
            r = subprocess.run([exe, "-r", f"{dpi:g}", "-png", "-singlefile", "-aa", "yes", "-aaVector", "yes", str(src), str(Path(td) / "m")],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not (Path(td) / "m.png").is_file():
                raise ExportError(f"pdftoppm failed: {r.stderr.strip()[:200]}")
            return (Path(td) / "m.png").read_bytes()
    if svg is not None:
        return _chromium_png(svg, dpi)
    raise ExportError("no rasterizer found: install pypdfium2, poppler (pdftoppm) or Playwright Chromium; run the doctor to see what is missing")


def _chromium_png(svg: str, dpi: float, alpha: bool = False) -> bytes:
    from playwright.sync_api import sync_playwright
    W, H = svg_size_mm(svg)
    scale = dpi / 96.0
    wpx, hpx = W / MM_PER_IN * 96, H / MM_PER_IN * 96
    if wpx * hpx * scale * scale > MAX_PIXELS:
        raise ExportError("the requested size is too large for the fallback rasterizer")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": round(wpx), "height": round(hpx)}, device_scale_factor=scale)
        pg.set_content(f'<html><body style="margin:0{";background:transparent" if alpha else ""}">{svg}</body></html>')
        pg.wait_for_timeout(200)
        data = pg.screenshot(type="png", clip={"x": 0, "y": 0, "width": wpx, "height": hpx}, omit_background=alpha)
        b.close()
    return data


def stamp_png(png: bytes, dpi: float, props: dict, title: str, alt: str) -> bytes:
    """DPI plus text chunks (accreditation hidden, alt text for screen readers). Pixels are untouched (an alpha channel is kept).
    Up to PNG_OPTIMIZE_MAX_PIXELS Pillow's optimize pass; above it zlib level 6 (see the module docstring for the measurements)."""
    from PIL import Image, PngImagePlugin
    im = Image.open(io.BytesIO(png))
    info = PngImagePlugin.PngInfo()
    for k, v in props["tEXt"].items():
        info.add_text(k, str(v))
    info.add_text("Title", title)
    if alt:
        info.add_text("Description", alt)
    info.add_text("Creation Time", _fixed_or_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    out = io.BytesIO()
    if im.size[0] * im.size[1] > PNG_OPTIMIZE_MAX_PIXELS:
        im.save(out, "PNG", pnginfo=info, dpi=(dpi, dpi), compress_level=6)
    else:
        im.save(out, "PNG", pnginfo=info, dpi=(dpi, dpi), optimize=True)
    return out.getvalue()


def _variant_stem(stem: str, tag: str) -> str:
    """`<prefix><id>_<template>_<tag>_v<date>`: a variant keeps the naming convention, with its tag before the version date."""
    return re.sub(r"(_v\d{8})$", rf"_{tag}\1", stem) if re.search(r"_v\d{8}$", stem) else f"{stem}_{tag}"


def export(svg: str, out_dir: Path, stem: str, formats: list[str], *, props: dict, title: str, alt: str, dpi: float, subject: str = "",
           options: dict | None = None) -> dict:
    """Write the requested formats next to each other. Returns {files: [...], dpi, size_mm, options, variants}.

    `options` (options_from): a transparent PNG, a PDF with bleed (and crop marks), PDF/A-2b, the SVG as Inkscape layers and an outlined-text SVG.
    The PNG is always the sheet's own size (never the bleed); `svg` in `formats` writes <stem>.svg here (build_map writes its own)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    opt = options_from(None, options or {})              # idempotent: options already in this form pass through unchanged
    res = {"files": [], "dpi": dpi, "size_mm": svg_size_mm(svg), "options": opt, "variants": {}, "title": title}
    plain = None

    def plain_pdf():
        nonlocal plain
        if plain is None:
            plain = stamp_pdf(to_pdf_bytes(svg, title, pdfa=opt["pdfa"]), props["pdf"], title, subject, pdfa=opt["pdfa"])
        return plain
    if "pdf" in formats:
        p = out_dir / f"{stem}.pdf"
        if opt["bleed_mm"] or opt["crop_marks"]:
            bsvg, boxes = bleed_svg(svg, opt["bleed_mm"], opt["crop_marks"])
            p.write_bytes(stamp_pdf(to_pdf_bytes(bsvg, title, pdfa=opt["pdfa"]), props["pdf"], title, subject, pdfa=opt["pdfa"], boxes=boxes))
            res["variants"]["bleed"] = {"file": str(p), **{k: list(v) if isinstance(v, tuple) else v for k, v in boxes.items()}}
        else:
            p.write_bytes(plain_pdf())
        if opt["pdfa"]:
            res["variants"]["pdfa"] = {"file": str(p), "part": 2, "conformance": "B"}
        res["files"].append(str(p))
    if "png" in formats:
        p = out_dir / f"{stem}.png"
        if opt["transparent"]:
            tsvg = transparent_svg(svg, opt["transparent"])
            tpdf = stamp_pdf(to_pdf_bytes(tsvg, title), props["pdf"], title, subject)
            p.write_bytes(stamp_png(rasterize(tpdf, dpi, tsvg, alpha=True), dpi, props["png"], title, alt))
            res["variants"]["transparent"] = {"file": str(p), "mode": opt["transparent"], "frame_mm": list(frame_rect_mm(svg) or ())}
        else:
            p.write_bytes(stamp_png(rasterize(plain_pdf(), dpi, svg), dpi, props["png"], title, alt))
        res["files"].append(str(p))
    if opt.get("social"):
        p = out_dir / f"{_variant_stem(stem, 'social')}.png"
        data, info = social_png(plain_pdf(), svg, props["png"], title, alt)
        p.write_bytes(data)
        res["variants"]["social"] = {"file": str(p), **info}
        res["files"].append(str(p))
    if "svg" in formats or opt["inkscape"]:
        p = out_dir / f"{stem}.svg"
        p.write_text(inkscape_svg(svg) if opt["inkscape"] else svg, encoding="utf-8")
        if opt["inkscape"]:
            res["variants"]["inkscape"] = {"file": str(p)}
        if str(p) not in res["files"]:
            res["files"].append(str(p))
    if opt["outline_text"]:
        osvg, stats = outline_text_svg(svg)
        p = out_dir / f"{_variant_stem(stem, 'outlined')}.svg"
        p.write_text(osvg, encoding="utf-8")
        res["variants"]["outlined"] = {"file": str(p), **stats}
        res["files"].append(str(p))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--svg")
    ap.add_argument("--animate", metavar="ATLAS_DIR", help="an atlas-by-time folder: write an animated GIF and an MP4 of its periods (animate.py)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--formats", help="pdf,png for --svg (the default); gif,mp4 for --animate (the default)")
    ap.add_argument("--dpi", type=float, default=300)
    ap.add_argument("--sizes", default="square,slide", help="--animate: square (1080 x 1080) and/or slide (1920 x 1080)")
    ap.add_argument("--hold", type=float, default=1.0, help="--animate: seconds each period stays on screen")
    ap.add_argument("--last-hold", type=float, default=3.0, help="--animate: seconds the last period stays")
    ap.add_argument("--brand")
    ap.add_argument("--flythrough", metavar="PAGE.html", help="1.6.0: a video flown over a 3D web page (flythrough.py); run it through map-run")
    add_option_flags(ap)
    if argv is None:
        argv = sys.argv[1:]
    if "--flythrough" in argv:                 # 1.6.0 (R4-FLY): its own options (--path, --chapters, --orbit, --size, --quality, --resume, ...)
        import flythrough as fl
        i = argv.index("--flythrough")
        return fl.main([argv[i + 1]] + argv[:i] + argv[i + 2:])
    a = ap.parse_args(argv)
    if a.animate:                              # M15: animated export from an atlas-by-time run
        import animate as an
        try:
            r = an.animate(a.animate, a.out, sizes=[s.strip() for s in a.sizes.split(",") if s.strip()],
                           formats=[s.strip() for s in (a.formats or "gif,mp4").split(",") if s.strip()], hold=a.hold, last_hold=a.last_hold, brand=a.brand)
        except an.AnimateError as e:
            print(f"map_export: {e}", file=sys.stderr)
            return gc.EXIT_USAGE
        print(json.dumps(r, indent=2, default=str))
        return gc.EXIT_LINT if r["blocked"] else gc.EXIT_OK
    if not a.svg:
        print("map_export: give --svg MAP.svg (or --animate ATLAS_DIR)", file=sys.stderr)
        return gc.EXIT_USAGE
    a.formats = a.formats or "pdf,png"
    import map_brand
    svg = Path(a.svg).read_text(encoding="utf-8")
    m = re.search(r'data-map-brand="([^"]+)"', svg)
    slug = a.brand or (m.group(1) if m else None)
    if not slug:
        print("map_export: the SVG carries no brand stamp; pass --brand", file=sys.stderr)
        return gc.EXIT_USAGE
    manifest = gc.build_manifest()
    props = {f: map_brand.props_for(slug, manifest, f) for f in ("pdf", "png")}
    t = re.search(r"<title[^>]*>(.*?)</title>", svg, re.S)
    d = re.search(r"<desc[^>]*>(.*?)</desc>", svg, re.S)
    try:
        opts = options_from(None, option_flags(a))
        fmts = [f.strip() for f in a.formats.split(",") if f.strip()]
        out = Path(a.out)
        if "svg" in fmts and out.resolve() == Path(a.svg).resolve().parent and not opts["inkscape"]:
            fmts.remove("svg")                                  # the SVG asked for is the one given
        res = export(svg, out, Path(a.svg).stem, fmts, props=props, title=t.group(1) if t else "Map", alt=d.group(1) if d else "", dpi=a.dpi,
                     options=opts)
    except ExportError as e:
        print(f"map_export: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    sys.path.insert(0, str(HERE.parent / "lints"))
    import output_lint
    res["findings"] = output_lint.check_export(res, svg)
    prov = Path(a.svg).with_name(Path(a.svg).stem + ".provenance.json")
    if prov.is_file():                          # 1.3.0 (R1-TERMS): the export's own terms register, from the map's provenance
        import terms_register as tr
        try:
            recs = json.loads(prov.read_text(encoding="utf-8")).get("records") or []
        except ValueError:
            recs = []
        tdoc, tfs, tfiles = tr.for_deliverable(out, recs, deliverable="export", stem=Path(a.svg).stem, outputs={"svg": svg},
                                               title=t.group(1) if t else None)
        res["findings"] += tfs
        res["terms"] = {"files": [str(f) for f in tfiles], "summary": tdoc["summary"]}
    print(json.dumps(res, indent=2, default=str))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in res["findings"]) else gc.EXIT_OK


def add_option_flags(ap) -> None:
    """The M16 export options as command-line flags (also on build_map.py)."""
    ap.add_argument("--transparent", nargs="?", const="page", choices=list(TRANSPARENT_MODES),
                    help="PNG with no page fill outside the map frame; 'all' also drops the frame's own background")
    ap.add_argument("--bleed", type=float, metavar="MM", help="print PDF with this bleed on every side (TrimBox and BleedBox set)")
    ap.add_argument("--crop-marks", action="store_true", default=None, help="crop marks outside the bleed (with --bleed)")
    ap.add_argument("--pdfa", action="store_true", default=None, help="PDF/A-2b")
    ap.add_argument("--inkscape", action="store_true", default=None, help="the SVG with its layer groups as Inkscape layers")
    ap.add_argument("--outline-text", action="store_true", default=None, help="also an SVG with every text drawn as Inter outlines")
    ap.add_argument("--social", action="store_true", default=None, help="also <stem>_social.png: 2160 px wide, under 5 MB (1.2.0)")


def option_flags(a) -> dict:
    return {"transparent": getattr(a, "transparent", None), "bleed_mm": getattr(a, "bleed", None), "crop_marks": getattr(a, "crop_marks", None),
            "pdfa": getattr(a, "pdfa", None), "inkscape": getattr(a, "inkscape", None), "outline_text": getattr(a, "outline_text", None),
            "social": getattr(a, "social", None)}


if __name__ == "__main__":
    sys.exit(main())
