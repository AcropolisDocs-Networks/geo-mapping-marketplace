"""Output lint, MAP-OUT-001/002/003/008 and the output half of MAP-LIC-005/009 (spec 12.6, 12.7).

Checks finished files, not plans. Everything here reads the files back from disk, so it tests what the user will receive.
M16 export fidelity adds MAP-OUT-014 (transparent PNG), 015 (PDF with bleed), 016 (PDF/A-2b), 017 (Inkscape SVG) and 018 (outlined-text SVG),
all through `check_export`; MAP-OUT-001 measures a PDF by its TrimBox.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

MM_PER_IN = 25.4
PDF_PAGE_TOLERANCE_MM = 0.8
PDF_WARN_BYTES = 25 * 1024 * 1024
PDF_IMAGE_WARN_PIXELS = 1_500_000
DPI_TOLERANCE = 0.5

_KEY_PATTERNS = [re.compile(p, re.I) for p in (
    r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{12,}",
    r"[?&](?:key|token|apikey|api_key|access_token)=[A-Za-z0-9_\-]{12,}",
    r"\bsk-[A-Za-z0-9]{20,}\b", r"\bAIza[0-9A-Za-z_\-]{30,}\b", r"\bghp_[A-Za-z0-9]{30,}\b", r"\bpk\.eyJ[A-Za-z0-9_\-\.]{20,}")]
# MAP-LIC-009: things that name a machine on a private network. Address literals are matched anywhere (an RFC 1918 or loopback IPv4,
# an IPv6 unique-local fc00::/7 or link-local fe80::/10 address); a private host name only where it stands as a host: after a URL
# scheme, after // or as host:port. A bare "gis.corp" in prose, a local file path (/home/x/, C:\Users) or a ".local" inside a word
# used to trip this rule; a local path says nothing about the network and the methods page names its own files.
_IPV4_PRIVATE = r"(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})"
# at least two colons and a last group of hex digits ("fd12::1", "fe80::a:b"): "fd12:" alone is five letters that a word or binary data can spell
_IPV6_PRIVATE = r"(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):(?:[0-9a-f]{0,4}:){1,6}[0-9a-f]{1,4}"
_HOST_PRIVATE = r"(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?:corp|internal|intranet|lan|local|home)|localhost)"
_INTERNAL = [re.compile(p, re.I) for p in (
    r"(?<![\d.])" + _IPV4_PRIVATE + r"(?![\d.])",
    r"(?<![0-9a-f:])" + _IPV6_PRIVATE + r"(?![0-9a-f:])",
    r"(?:https?|wss?|ftp|s?ftp)://" + _HOST_PRIVATE + r"(?![A-Za-z0-9-])",          # a URL host
    r"(?<![\w./-])//" + _HOST_PRIVATE + r"(?![A-Za-z0-9-])",                           # a scheme-less //host
    r"(?<![\w./-])" + _HOST_PRIVATE + r":\d{2,5}(?!\d)",                               # host:port
    r"\\\\[A-Za-z0-9][\w.-]*\\[\w$.-]+",                                                # a UNC share names a file server
)]


_DATA_URI = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{64,}")
# M16: path data is drawing commands and plain numbers, which no key, host name or address pattern can match: a map of 250,000 points carries
# 7 MB of it, and the address patterns spent six seconds walking its digits. A d attribute made only of command letters, digits, points,
# spaces and minus signs is left out of the scan, unless it writes numbers in the compact form ("1.5.3", two points in one run of digits),
# which could spell an address and so is still scanned.
_PATH_DATA = re.compile(r'\bd="([MLHVZmlhvzac0-9. -]*)"')
_COMPACT_NUMBERS = re.compile(r"\d*\.\d+\.")


def _SAFE_XML(huge: bool = False):
    """1.8.0: the parser every SVG read here uses: no entity expansion, no network, no DTD (the map's own output, and a file a person hands in)."""
    from lxml import etree
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=huge)


def _drop_path_data(text: str) -> str:
    return _PATH_DATA.sub(lambda m: m.group(0) if _COMPACT_NUMBERS.search(m.group(1)) else 'd=""', text)


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ------------------------------------------------------------------------------------------------------------------ leaks
def check_leaks(text: str, what: str) -> list[dict]:
    """MAP-LIC-005 (no API keys in outputs) and MAP-LIC-009 (no internal host names, paths or addresses). Embedded pictures (base64 data URIs)
    are left out of the scan: a long run of base64 can spell `AIza` and thirty letters by chance (M12: a raster layer is megabytes of it)."""
    out = []
    text = _drop_path_data(_DATA_URI.sub("data:,", text))
    for p in _KEY_PATTERNS:
        m = p.search(text)
        if m:
            out.append(finding("MAP-LIC-005", "BLOCK", f"{what}: something that looks like an API key or token is embedded", m.group(0)[:6] + "…"))
            break
    hits = []
    for p in _INTERNAL:
        hits += [m.group(0) for m in p.finditer(text)]
    hits = sorted(set(hits))
    if hits:
        out.append(finding("MAP-LIC-009", "BLOCK", f"{what}: internal host names, addresses or paths are present; the source note should read \"internal data service\"", hits[:5]))
    return out


# ------------------------------------------------------------------------------------------------------------------ SVG
def check_svg(text: str, filename: str | None = None) -> list[dict]:
    """MAP-OUT-003: valid XML, no external references, Inter embedded, text kept as text."""
    out = []
    try:
        from lxml import etree
        root = etree.fromstring(text.encode("utf-8"), _SAFE_XML())
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-003", "BLOCK", f"the SVG is not valid XML: {str(e)[:120]}", None)]
    ext = []
    for el in root.iter():
        for k, v in el.attrib.items():
            if k.endswith("href") and v and not v.startswith("#") and not v.startswith("data:"):
                ext.append(v)
    ext += re.findall(r"url\(\s*[\"']?(?!#|data:)([^)\"']+)", text)
    ext += re.findall(r"@import\s+[\"']?([^\"';)\s]+)", text)
    if ext:
        out.append(finding("MAP-OUT-003", "BLOCK", "the SVG refers to external files or addresses; it must be self-contained", sorted(set(ext))[:5]))
    if re.search(r"<(?:script|foreignObject|iframe)\b", text, re.I):
        out.append(finding("MAP-OUT-003", "BLOCK", "the SVG contains script or foreign content", None))
    if re.search(r"<text\b", text) and not re.search(r"@font-face", text, re.S):      # 1.4.0: Inter, or a style pack's faces (MAP-BRAND-003 checks which)
        out.append(finding("MAP-OUT-003", "BLOCK", "the SVG has live text but Inter is not embedded", None))
    if re.search(r"<filter\b", text, re.I):                    # 1.4.0 (R2-PACK): Chromium rasterizes filters; Day 6's PDF reached 26 MB
        out.append(finding("MAP-OUT-019", "BLOCK", "the SVG carries an SVG <filter>; every treatment is vector or one raster image, never a filter", None))
    out += check_leaks(text, "SVG")
    if filename:
        out += check_name(filename)
    return out


# ------------------------------------------------------------------------------------------------------------------ large layers (M16)
LAYER_ELEMENTS_WARN = 50_000          # separate shapes in one layer: SVG viewers and the PDF writer slow down past this
SVG_PATH_DATA_WARN = 40 * 1024 * 1024 # path data in one file: a browser takes seconds to open more
OVERPLOT_WARN = 1.0                   # point symbols whose areas add up to more than the map frame hide each other
_DOT_D = re.compile(r"^(?:M-?[\d.]+ -?[\d.]+h0)+$")
_ARC = re.compile(r"a(-?[\d.]+) -?[\d.]+ 0 1 0 ")


def check_layer_load(text: str) -> list[dict]:
    """Read the written SVG back, layer by layer (the `gm-layer-<id>` groups):
    MAP-OUT-011 WARN  a layer drawn as more than LAYER_ELEMENTS_WARN separate shapes, or a file with more than SVG_PATH_DATA_WARN of path
                      data: too large for SVG and PDF viewers, which open it slowly or not at all;
    MAP-OUT-012 WARN  round point symbols whose areas add up to more than the map frame (OVERPLOT_WARN times over): they cover each other,
                      and the class drawn last hides the rest, so the layer reads as a blob."""
    import math
    try:
        from lxml import etree
        root = etree.fromstring(text.encode("utf-8"), _SAFE_XML(huge=True))
    except Exception:  # noqa: BLE001 - check_svg reports an unreadable SVG
        return []
    ns = "{http://www.w3.org/2000/svg}"
    frame = 0.0
    for clip in root.iter(f"{ns}clipPath", "clipPath"):
        if clip.get("id") == "gm-clip":
            r = next(iter(clip), None)
            if r is not None:
                frame = float(r.get("width") or 0) * float(r.get("height") or 0)
    out, total = [], 0
    for g in root.iter(f"{ns}g", "g"):
        gid = g.get("id") or ""
        if not gid.startswith("gm-layer-"):
            continue
        lid = g.get("data-layer") or gid[len("gm-layer-"):]
        paths = [p for p in g.iter(f"{ns}path", "path")]
        size = sum(len(p.get("d") or "") for p in paths)
        total += size
        if len(paths) > LAYER_ELEMENTS_WARN:
            out.append(finding("MAP-OUT-011", "WARN", f"layer '{lid}' is drawn as {len(paths):,} separate shapes; SVG viewers and PDF writers slow down past "
                               f"{LAYER_ELEMENTS_WARN:,}. Aggregate it (role hexbin or grid, or a dissolve), filter it, or simplify it", {"layer": lid, "elements": len(paths)}))
        area, seen = 0.0, set()
        for p in paths:
            d = p.get("d") or ""
            if d in seen:
                continue                                     # the fill pass of a dot layer repeats its outline pass
            if p.get("stroke-linecap") == "round" and _DOT_D.match(d):
                seen.add(d)
                w = max((float(q.get("stroke-width") or 0) for q in paths if q.get("d") == d), default=0.0)
                area += d.count("h0") * math.pi * (w / 2) ** 2
            else:
                m = _ARC.search(d)
                if m:                                        # a circle drawn as two arcs, its outline half outside it
                    r = float(m.group(1)) + float(p.get("stroke-width") or 0) / 2
                    area += (d.count("a") / 2) * math.pi * r ** 2
        if frame and area / frame > OVERPLOT_WARN:
            k = area / frame
            out.append(finding("MAP-OUT-012", "WARN", f"layer '{lid}': its point symbols add up to {k:.1f} times the area of the map frame, so they cover one another and "
                               "the class drawn last hides the rest. Show the density instead (role: hexbin) or draw smaller symbols (symbol.size); thin: true makes "
                               "the file smaller but leaves the picture as it is", {"layer": lid, "coverage": round(k, 2)}))
    if total > SVG_PATH_DATA_WARN:
        out.append(finding("MAP-OUT-011", "WARN", f"the SVG holds {total / 1024 ** 2:.0f} MB of path data; browsers take a long time to open it. Aggregate or simplify "
                           "the largest layer, or hand over the PDF or PNG", {"path_data_bytes": total}))
    return out


# ------------------------------------------------------------------------------------------------------------------ names
def check_name(path: str, prefix: str | None = None, map_id: str | None = None, template: str | None = None) -> list[dict]:
    """MAP-OUT-008: brand prefix + id + template + version date + extension."""
    n = Path(path).name
    if not re.fullmatch(r"[A-Za-z0-9_\-]+_v\d{8}\.(?:svg|pdf|png|html|gpkg|geojson|kml|kmz|zip)", n):
        return [finding("MAP-OUT-008", "INFO", f"'{n}' does not follow the naming convention (prefix, id, template, vYYYYMMDD, extension)", n)]
    if prefix and not n.startswith(prefix):
        return [finding("MAP-OUT-008", "INFO", f"'{n}' does not start with the brand prefix '{prefix}'", n)]
    if map_id and template and not re.search(re.escape(map_id) + "_" + re.escape(template) + r"_(?:[a-z][a-z0-9-]*_)?v\d{8}", n):     # 1.4.0: a pack id may follow
        return [finding("MAP-OUT-008", "INFO", f"'{n}' does not carry the map id and template", n)]
    return []


# ------------------------------------------------------------------------------------------------------------------ PDF
def _font_report(pdf) -> list[dict]:
    fonts = {}
    for page in pdf.pages:
        res = page.get("/Resources")
        stack = [res] if res is not None else []
        seen = set()
        while stack:
            r = stack.pop()
            try:
                f = r.get("/Font")
                for k in (f.keys() if f is not None else []):
                    fo = f[k]
                    fd = fo.get("/FontDescriptor")
                    name = str(fo.get("/BaseFont", "")).lstrip("/")
                    if fo.get("/Subtype") == "/Type0":
                        d = fo["/DescendantFonts"][0]
                        fd = d.get("/FontDescriptor")
                    embedded = bool(fd is not None and any(x in fd for x in ("/FontFile", "/FontFile2", "/FontFile3")))
                    fonts[name] = embedded
                xo = r.get("/XObject")
                for k in (xo.keys() if xo is not None else []):
                    o = xo[k]
                    if o.get("/Subtype") == "/Form" and o.objgen not in seen:
                        seen.add(o.objgen)
                        if o.get("/Resources") is not None:
                            stack.append(o["/Resources"])
            except Exception:  # noqa: BLE001
                continue
    return [{"name": n, "embedded": e} for n, e in sorted(fonts.items())]


def _images(pdf) -> list[tuple[int, int]]:
    out = []
    for page in pdf.pages:
        try:
            xo = page["/Resources"].get("/XObject")
            for k in (xo.keys() if xo is not None else []):
                o = xo[k]
                if o.get("/Subtype") == "/Image":
                    out.append((int(o["/Width"]), int(o["/Height"])))
        except Exception:  # noqa: BLE001
            continue
    return out


def _read_back(p: Path):
    """(page count, [(w_pt, h_pt)], fonts, images) from the file: pypdf, else pikepdf. The size is the TrimBox's: the finished sheet, which
    is the MediaBox on a page without bleed (M16)."""
    try:
        import pdf_stack
        r = pdf_stack.read_pdf(p)
        return len(r.pages), pdf_stack.page_sizes_pt(r, "trimbox"), pdf_stack.font_report(r), pdf_stack.image_sizes(r)
    except ImportError:
        import pikepdf
        with pikepdf.open(str(p)) as pdf:
            sizes = []
            for pg in pdf.pages[:1]:
                mb = [float(x) for x in (pg.obj.get("/TrimBox") or pg.MediaBox)]
                sizes.append((mb[2] - mb[0], mb[3] - mb[1]))
            return len(pdf.pages), sizes, _font_report(pdf), _images(pdf)


_DECLARED = r"gm-base-tiles|gm-raster|gm-relief"        # a tile composite (0.3), a raster layer and a relief (M12): rasters by design, not by accident
_BASE_IMG = re.compile(r'<image\b[^>]*class="([^"]*\b(?:' + _DECLARED + r')\b[^"]*)"[^>]*href="data:image/png;base64,([A-Za-z0-9+/=]{40})', re.S)
_BASE_IMG_ALT = re.compile(r'<image\b[^>]*href="data:image/png;base64,([A-Za-z0-9+/=]{40})"[^>]*class="([^"]*\b(?:' + _DECLARED + r')\b[^"]*)"', re.S)
DECLARED_KINDS = {"gm-base-tiles": "tile basemap composite", "gm-raster": "raster layer", "gm-relief": "relief shading"}


def declared_images(svg_text: str) -> list[tuple[int, int, str]]:
    """(width, height, kind) of every declared raster an SVG embeds: the tile composite (`gm-base-tiles`), a raster layer (`gm-raster`) and a relief
    (`gm-relief`), read from the PNG header inside the data URI without decoding the picture (the first 24 bytes of a PNG hold its IHDR size)."""
    import base64
    out = []
    hits = [(m.group(1), m.group(2)) for m in _BASE_IMG.finditer(svg_text)] + [(m.group(2), m.group(1)) for m in _BASE_IMG_ALT.finditer(svg_text)]
    for cls, b64 in hits:
        try:
            head = base64.b64decode(b64[:32])
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                kind = next((v for k, v in DECLARED_KINDS.items() if re.search(r"\b" + k + r"\b", cls)), "raster")
                out.append((int(w), int(h), kind))
        except Exception:  # noqa: BLE001 - a malformed data URI is MAP-OUT-003's business
            continue
    return out


def basemap_images(svg_text: str) -> list[tuple[int, int]]:
    """The pixel sizes of the declared rasters an SVG embeds (tile composites, raster layers, reliefs); see declared_images."""
    return [(w, h) for w, h, _k in declared_images(svg_text)]


def _sibling_svg(p: Path) -> Path | None:
    """The SVG a PDF was made from: the same stem beside it, or the stem without the _geo / _methods suffix (build_map writes them so)."""
    for stem in (p.stem, re.sub(r"_(?:geo|methods)$", "", p.stem)):
        q = p.with_name(stem + ".svg")
        if q.is_file():
            return q
    return None


def _norm_font(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def check_pdf(path: str, size_mm: tuple[float, float], *, basemap_px: list | None = None, fonts_allowed: list | None = None) -> list[dict]:
    """MAP-OUT-001/002: one page of the template's size, Inter embedded and nothing else, vector preserved, file size sane.
    Read back with pypdf (the acropolisdocs read-back library, via pdf_stack); pikepdf reads it only when pypdf is not installed.

    `basemap_px` lists the pixel sizes of declared tile-basemap composites. A map with a tile basemap is a vector map over one raster by
    design, so that raster does not make it "not fully vector" (MAP-OUT-002 fired on every tile-basemap PDF and taught people to ignore
    it). When the caller passes nothing, the composites are read from the SVG beside the PDF; a raster larger than any declared composite,
    or any large raster when no basemap was declared, still warns."""
    out = []
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return [finding("MAP-OUT-002", "BLOCK", f"{p.name} was not written", None)]
    kinds = {"tile basemap composite"}
    if basemap_px is None:
        svg = _sibling_svg(p)
        dec = declared_images(svg.read_text(encoding="utf-8", errors="replace")) if svg else []
        basemap_px = [(w, h) for w, h, _k in dec]
        kinds = {k for _w, _h, k in dec} or kinds
    try:
        pages, sizes_pt, fonts, images = _read_back(p)
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-002", "BLOCK", f"{p.name} cannot be opened: {str(e)[:100]}", None)]
    if pages != 1:
        out.append(finding("MAP-OUT-002", "BLOCK", f"the PDF has {pages} pages; a map is one page", pages))
    if sizes_pt:
        w, h = sizes_pt[0][0] * MM_PER_IN / 72, sizes_pt[0][1] * MM_PER_IN / 72
        if abs(w - size_mm[0]) > PDF_PAGE_TOLERANCE_MM or abs(h - size_mm[1]) > PDF_PAGE_TOLERANCE_MM:
            out.append(finding("MAP-OUT-001", "BLOCK", f"the PDF page is {w:.1f} x {h:.1f} mm but the template is {size_mm[0]:.1f} x {size_mm[1]:.1f} mm", [w, h]))
    loose = [f["name"] for f in fonts if not f["embedded"]]
    allowed = [_norm_font(a) for a in (fonts_allowed or ["Inter"])]          # 1.4.0: a style pack's faces (their family and PostScript names)
    foreign = [f["name"] for f in fonts if not any(a and a in _norm_font(f["name"]) for a in allowed)]
    if loose:
        out.append(finding("MAP-OUT-002", "BLOCK", "fonts are not embedded in the PDF", loose))
    if foreign:
        out.append(finding("MAP-OUT-002", "BLOCK", "the PDF uses a font other than Inter" if not fonts_allowed else
                           "the PDF uses a font other than the style pack's", foreign))
    if not fonts:
        out.append(finding("MAP-OUT-002", "WARN", "the PDF contains no text; labels may have been drawn as images or outlines", None))
    big = [wh for wh in images if wh[0] * wh[1] > PDF_IMAGE_WARN_PIXELS]
    declared = [tuple(int(v) for v in wh) for wh in (basemap_px or [])]
    cap = max((w * h for w, h in declared), default=0)
    composite = [wh for wh in big if declared and wh[0] * wh[1] <= cap * 1.05]
    other = [wh for wh in big if wh not in composite]
    if other:
        what = ("larger than the declared tile basemap composite" if kinds == {"tile basemap composite"} else "larger than the declared raster images") if declared else "and no tile basemap is declared"
        out.append(finding("MAP-OUT-002", "WARN", f"the PDF holds large raster images {what}, so the map is not fully vector", other[:3]))
    elif composite and kinds == {"tile basemap composite"}:
        out.append(finding("MAP-OUT-002", "INFO", f"the PDF's one raster is the declared tile basemap composite ({composite[0][0]} x {composite[0][1]} px); everything drawn over it is vector", composite[:3]))
    elif composite:
        out.append(finding("MAP-OUT-002", "INFO", f"the PDF's large rasters are declared images ({', '.join(sorted(kinds))}: {', '.join(f'{w} x {h} px' for w, h in composite[:3])}); "
                           "everything else is vector", composite[:3]))
    if p.stat().st_size > PDF_WARN_BYTES:
        out.append(finding("MAP-OUT-002", "WARN", f"the PDF is {p.stat().st_size / 1e6:.1f} MB; simplify the geometry or drop detail", p.stat().st_size))
    try:
        out += check_leaks(pdf_readable_text(p), "PDF")
    except Exception:  # noqa: BLE001
        pass
    return out


_XMP = re.compile(rb"<x:xmpmeta.*?</x:xmpmeta>", re.S)
_PDF_STRINGS = re.compile(rb"/(?:URI|Title|Author|Subject|Keywords|Creator|Producer)\s*\(((?:\\.|[^\\)])*)\)")


def pdf_readable_text(p) -> str:
    """What a reader of the PDF can see or look up: the text of every page, the document properties, the XMP metadata and the link
    addresses. The compressed content, font and image streams are left out: raw bytes spelled an IPv6 address or a key shape by chance,
    which made MAP-LIC-009 fail now and then on an atlas (several PDFs a run)."""
    raw = Path(p).read_bytes()
    parts: list[str] = []
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
        try:
            parts += [str(v) for v in (doc.get_metadata_dict() or {}).values() if v]
            for i in range(len(doc)):
                page = doc[i]
                tp = page.get_textpage()
                parts.append(tp.get_text_range())
                tp.close()
                page.close()
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - without pypdfium2 the strings below still cover the properties and links
        pass
    parts += [m.group(0).decode("utf-8", "replace") for m in _XMP.finditer(raw)]
    parts += [m.group(1).decode("latin-1") for m in _PDF_STRINGS.finditer(raw)]
    return "\n".join(parts)


# ------------------------------------------------------------------------------------------------------------------ PNG
def png_info(path: str) -> dict:
    """Pixel size, DPI (pHYs) and text chunks, read straight from the file."""
    b = Path(path).read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    i, info, text = 8, {"text": {}}, {}
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]
        kind = b[i + 4:i + 8]
        data = b[i + 8:i + 8 + n]
        if kind == b"IHDR":
            info["width"], info["height"] = struct.unpack(">II", data[:8])
        elif kind == b"pHYs":
            x, y, unit = struct.unpack(">IIB", data)
            if unit == 1:
                info["dpi"] = (x * 0.0254, y * 0.0254)
        elif kind == b"tEXt":
            k, _, v = data.partition(b"\x00")
            text[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"iTXt":
            k, _, rest = data.partition(b"\x00")
            text[k.decode("latin-1")] = rest[4:].split(b"\x00", 2)[-1].decode("utf-8", "replace") if rest[:1] in (b"\x00", b"\x01") else ""
        elif kind == b"zTXt":
            k, _, rest = data.partition(b"\x00")
            try:
                text[k.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
            except Exception:  # noqa: BLE001
                pass
        i += 12 + n
    info["text"] = text
    return info


def check_png(path: str, size_mm: tuple[float, float], dpi: float) -> list[dict]:
    """MAP-OUT-001: pixel size matches the template at the requested DPI, and the file says so."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return [finding("MAP-OUT-001", "BLOCK", f"{p.name} was not written", None)]
    try:
        info = png_info(path)
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-001", "BLOCK", f"{p.name} is not a readable PNG: {str(e)[:80]}", None)]
    out = []
    ew, eh = size_mm[0] / MM_PER_IN * dpi, size_mm[1] / MM_PER_IN * dpi
    if abs(info["width"] - ew) > 1.5 or abs(info["height"] - eh) > 1.5:
        out.append(finding("MAP-OUT-001", "BLOCK", f"the PNG is {info['width']} x {info['height']} px but the template at {dpi:g} dpi needs about {ew:.0f} x {eh:.0f} px", [info["width"], info["height"]]))
    got = info.get("dpi")
    if not got or got[0] < dpi - DPI_TOLERANCE:
        out.append(finding("MAP-OUT-001", "BLOCK", f"the PNG does not declare {dpi:g} dpi (found {'none' if not got else f'{got[0]:.0f}'})", got))
    return out


# ---------------------------------------------------------------------------------------------------------------- animations (M15)
ANIM_TIME_TOLERANCE_S = 0.25


def check_gif(path: str, size_px: tuple[int, int], frames: int, hold_s: float, last_hold_s: float) -> list[dict]:
    """MAP-OUT-013 (GIF): read back, the file has the frames, the size and the timing asked for (every frame `hold_s`, the last longer), loops,
    and carries the hidden accreditation in its comment rather than in a frame."""
    from PIL import Image
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return [finding("MAP-OUT-013", "BLOCK", f"{p.name} was not written", None)]
    out = []
    try:
        im = Image.open(p)
        n = getattr(im, "n_frames", 1)
        size = im.size
        durs = []
        for k in range(n):
            im.seek(k)
            durs.append(int(im.info.get("duration", 0)))
        im.seek(0)
        loop = im.info.get("loop")
        comment = im.info.get("comment", b"")
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-013", "BLOCK", f"{p.name} is not a readable GIF: {str(e)[:80]}", None)]
    if n != frames:
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} has {n} frame(s); {frames} were drawn", n))
    if tuple(size) != tuple(size_px):
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} is {size[0]} x {size[1]} px, not {size_px[0]} x {size_px[1]}", list(size)))
    if durs and (abs(durs[-1] / 1000 - last_hold_s) > ANIM_TIME_TOLERANCE_S or any(abs(d / 1000 - hold_s) > ANIM_TIME_TOLERANCE_S for d in durs[:-1])):
        out.append(finding("MAP-OUT-013", "WARN", f"{p.name}: frame times {sorted(set(durs))} ms differ from the hold asked for ({hold_s:g} s, last {last_hold_s:g} s)", durs))
    if loop != 0:
        out.append(finding("MAP-OUT-013", "WARN", f"{p.name} does not loop", loop))
    if b"Accreditation" not in (comment or b""):
        out.append(finding("MAP-OUT-013", "WARN", f"{p.name} carries no hidden accreditation in its comment", None))
    return out


def mp4_info(path: str) -> dict:
    """Codec, pixel format, size, frame rate and duration of a video, read back through ffmpeg (imageio-ffmpeg's reader)."""
    import imageio_ffmpeg
    gen = imageio_ffmpeg.read_frames(path)
    meta = next(gen)
    gen.close()
    nframes, secs = imageio_ffmpeg.count_frames_and_secs(path)
    return {"size": tuple(meta.get("size") or (0, 0)), "fps": float(meta.get("fps") or 0), "codec": str(meta.get("codec") or ""),
            "pix_fmt": str(meta.get("pix_fmt") or ""), "frames": int(nframes), "seconds": float(secs)}


def check_mp4(path: str, size_px: tuple[int, int], seconds: float) -> list[dict]:
    """MAP-OUT-013 (MP4): read back, H.264 in yuv420p (what every phone and slide tool plays), even dimensions of the size asked for, and about
    the length asked for (the frames' holds added up)."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return [finding("MAP-OUT-013", "BLOCK", f"{p.name} was not written", None)]
    try:
        info = mp4_info(path)
    except ImportError:
        return [finding("MAP-OUT-013", "INFO", f"{p.name} was not read back: pip install imageio-ffmpeg to check it", None)]
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-013", "BLOCK", f"{p.name} is not a readable video: {str(e)[:120]}", None)]
    out = []
    w, h = info["size"]
    if (w, h) != tuple(size_px):
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} is {w} x {h} px, not {size_px[0]} x {size_px[1]}", [w, h]))
    if w % 2 or h % 2:
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} has odd dimensions ({w} x {h}); H.264 in yuv420p needs even ones", [w, h]))
    if "h264" not in info["codec"].lower():
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} is {info['codec'] or 'an unknown codec'}, not H.264", info["codec"]))
    if not info["pix_fmt"].startswith("yuv420p"):
        out.append(finding("MAP-OUT-013", "BLOCK", f"{p.name} is in {info['pix_fmt'] or 'an unknown pixel format'}, not yuv420p; many players will not open it", info["pix_fmt"]))
    if abs(info["seconds"] - seconds) > max(ANIM_TIME_TOLERANCE_S, 2.0 / max(info["fps"], 1)):
        out.append(finding("MAP-OUT-013", "WARN", f"{p.name} runs {info['seconds']:.2f} s; the holds add up to {seconds:.2f} s", info["seconds"]))
    return out


# ---------------------------------------------------------------------------------------------------------------- export fidelity (M16)
TRANSPARENT_OUTSIDE_MIN = 0.5      # at least this share of the sheet outside the map frame must be see-through (the rest is footer text, logo)
FRAME_OPAQUE_MIN = 0.99            # `page` mode: this share of the frame's pixels keeps its background
BLEED_TOLERANCE_MM = 0.2
PAPER_DIFF = 12                    # a bleed strip within this many levels of the page colour just inside the trim is painted


def check_transparent_png(path: str, size_mm: tuple[float, float], frame_mm, mode: str) -> list[dict]:
    """MAP-OUT-014: a transparent PNG read back. It has an alpha channel; the sheet's corners and at least half of what lies outside the map
    frame are see-through (BLOCK); in `page` mode the frame keeps its background, so its pixels stay opaque (BLOCK); in `all` mode the
    share of the frame left see-through is reported (INFO)."""
    import numpy as np
    from PIL import Image
    p = Path(path)
    try:
        im = Image.open(p)
        im.load()
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-014", "BLOCK", f"{p.name} is not a readable PNG: {str(e)[:80]}", None)]
    if im.mode not in ("RGBA", "LA") and "transparency" not in im.info:
        return [finding("MAP-OUT-014", "BLOCK", f"{p.name} was asked for with a transparent background but has no alpha channel ({im.mode})", im.mode)]
    a = np.asarray(im.convert("RGBA"))[:, :, 3]
    h, w = a.shape
    kx, ky = w / float(size_mm[0]), h / float(size_mm[1])
    out = []
    c = max(1, int(round(2.0 * kx)))
    corners = [a[:c, :c], a[:c, -c:], a[-c:, :c], a[-c:, -c:]]
    if any(int(q.max()) > 0 for q in corners):
        out.append(finding("MAP-OUT-014", "BLOCK", f"{p.name}: the corners of the sheet are painted; the page fill was not left out", None))
    inside = np.zeros_like(a, dtype=bool)
    if frame_mm:
        x, y, fw, fh = frame_mm
        i0, i1 = int(np.floor(x * kx)) - 1, int(np.ceil((x + fw) * kx)) + 1
        j0, j1 = int(np.floor(y * ky)) - 1, int(np.ceil((y + fh) * ky)) + 1
        inside[max(0, j0):max(0, j1), max(0, i0):max(0, i1)] = True
        core = a[int(np.ceil(y * ky)) + 1:int(np.floor((y + fh) * ky)) - 1, int(np.ceil(x * kx)) + 1:int(np.floor((x + fw) * kx)) - 1]
    else:
        core = a[:0, :0]
    outside = a[~inside]
    clear = float((outside == 0).mean()) if outside.size else 1.0
    if clear < TRANSPARENT_OUTSIDE_MIN:
        out.append(finding("MAP-OUT-014", "BLOCK", f"{p.name}: only {clear:.0%} of the sheet outside the map frame is see-through", round(clear, 3)))
    opaque = float((core == 255).mean()) if core.size else 1.0
    if mode == "page" and opaque < FRAME_OPAQUE_MIN:
        out.append(finding("MAP-OUT-014", "BLOCK", f"{p.name}: {1 - opaque:.0%} of the map frame is see-through; with transparent page the frame keeps its background", round(opaque, 3)))
    if not out:
        msg = (f"{p.name}: {clear:.0%} of the sheet outside the map frame is see-through" +
               (", the frame keeps its background" if mode == "page" else f", {1 - opaque:.0%} of the frame is see-through where nothing is drawn") +
               "; the footer and title stay in the theme's text colour, so put a light theme on a light background and a dark theme on a dark one")
        out.append(finding("MAP-OUT-014", "INFO", msg, {"outside_clear": round(clear, 3), "frame_opaque": round(opaque, 3)}))
    return out


def _boxes_mm(page) -> dict:
    k = MM_PER_IN / 72.0
    out = {}
    for name in ("/MediaBox", "/BleedBox", "/TrimBox"):
        v = page.obj.get(name)
        if v is not None:
            out[name.strip("/")] = tuple(float(x) * k for x in v)
    return out


def check_bleed_pdf(path: str, size_mm: tuple[float, float], bleed_mm: float, crop_marks: bool = False) -> list[dict]:
    """MAP-OUT-015: a print PDF with bleed read back. The TrimBox is the sheet, the BleedBox the sheet plus the bleed on every side, the
    MediaBox holds the BleedBox (BLOCK); drawn at 100 dpi, the page colour reaches the bleed edge on all four sides (BLOCK), and asked-for crop
    marks are on the paper outside the bleed at the trim lines (WARN)."""
    import numpy as np
    import pikepdf
    p = Path(path)
    out = []
    try:
        with pikepdf.open(str(p)) as pdf:
            bx = _boxes_mm(pdf.pages[0])
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-015", "BLOCK", f"{p.name} cannot be opened: {str(e)[:100]}", None)]
    trim, bleed, media = bx.get("TrimBox"), bx.get("BleedBox"), bx.get("MediaBox")
    if not trim or not bleed:
        return [finding("MAP-OUT-015", "BLOCK", f"{p.name} was asked for with a {bleed_mm:g} mm bleed but has no {'TrimBox' if not trim else 'BleedBox'}", bx)]
    tw, th = trim[2] - trim[0], trim[3] - trim[1]
    if abs(tw - size_mm[0]) > PDF_PAGE_TOLERANCE_MM or abs(th - size_mm[1]) > PDF_PAGE_TOLERANCE_MM:
        out.append(finding("MAP-OUT-015", "BLOCK", f"{p.name}: the TrimBox is {tw:.1f} x {th:.1f} mm, not the {size_mm[0]:.1f} x {size_mm[1]:.1f} mm sheet", [tw, th]))
    want = (trim[0] - bleed_mm, trim[1] - bleed_mm, trim[2] + bleed_mm, trim[3] + bleed_mm)
    if any(abs(a_ - b_) > BLEED_TOLERANCE_MM for a_, b_ in zip(bleed, want, strict=True)):
        out.append(finding("MAP-OUT-015", "BLOCK", f"{p.name}: the BleedBox is not the sheet plus {bleed_mm:g} mm on every side", [round(v, 2) for v in bleed]))
    if media[0] > bleed[0] + 1e-3 or media[1] > bleed[1] + 1e-3 or media[2] < bleed[2] - 1e-3 or media[3] < bleed[3] - 1e-3:
        out.append(finding("MAP-OUT-015", "BLOCK", f"{p.name}: the BleedBox lies outside the page (MediaBox)", None))
    try:
        import io
        import pdf_stack
        from PIL import Image
        dpi = 100.0
        im = np.asarray(Image.open(io.BytesIO(pdf_stack.render_png(p.read_bytes(), dpi))).convert("L")).astype("int16")
    except Exception as e:  # noqa: BLE001 - no rasterizer: the boxes were still checked
        out.append(finding("MAP-OUT-015", "INFO", f"{p.name}: the bleed was not drawn to check its colour ({str(e)[:60]})", None))
        return out
    H_, k = im.shape[0], dpi / MM_PER_IN

    def px(xmm, ymm_from_bottom):
        return im[min(H_ - 1, max(0, int((media[3] - ymm_from_bottom) * k))), min(im.shape[1] - 1, max(0, int((xmm - media[0]) * k)))]
    cx, cy, b2 = (trim[0] + trim[2]) / 2, (trim[1] + trim[3]) / 2, bleed_mm / 2
    probes = {"top": ((cx, trim[3] + b2), (cx, trim[3] - 1.0)), "bottom": ((cx, trim[1] - b2), (cx, trim[1] + 1.0)),
              "left": ((trim[0] - b2, cy), (trim[0] + 1.0, cy)), "right": ((trim[2] + b2, cy), (trim[2] - 1.0, cy))}
    bare = [side for side, (outer, inner) in probes.items() if abs(int(px(*outer)) - int(px(*inner))) > PAPER_DIFF]
    if bleed_mm > 0 and bare:
        out.append(finding("MAP-OUT-015", "BLOCK", f"{p.name}: the page colour does not run into the bleed on the {', '.join(bare)} side(s)", bare))
    if crop_marks:
        o = bleed_mm + 1.0
        seen = 0
        for x in (trim[0], trim[2]):
            col = int(round((x - media[0]) * k))
            r0, r1 = int((media[3] - (trim[3] + o + 5.0)) * k), int((media[3] - (trim[3] + o)) * k)
            if 0 <= col < im.shape[1] and r1 > r0 >= 0 and int(im[r0:r1, max(0, col - 1):col + 2].min()) < 200:
                seen += 1
        if seen < 2:
            out.append(finding("MAP-OUT-015", "WARN", f"{p.name}: crop marks were asked for but {2 - seen} of the top marks are not on the page", seen))
    if not out:
        out.append(finding("MAP-OUT-015", "INFO", f"{p.name}: trim {tw:.1f} x {th:.1f} mm, {bleed_mm:g} mm bleed painted on all four sides"
                           + (", crop marks outside the bleed" if crop_marks else ""), {k_: [round(v, 2) for v in b_] for k_, b_ in bx.items()}))
    return out


PDFA_NOT_CHECKED = ("the full ISO 19005-2 rule set (veraPDF is not available here): glyph widths against the embedded font programs, the ICC "
                    "profile's own contents, the colour spaces of transparency groups and every content-stream operator")


def check_pdfa(path: str, title: str | None = None, accreditation: str | None = None) -> list[dict]:
    """MAP-OUT-016: a PDF/A-2b file read back with pikepdf. Checked: the XMP claims part 2 conformance B; an output intent GTS_PDFA1 with an
    RGB ICC profile; every font embedded; not encrypted, no JavaScript, no embedded files; the Info entries agree with their XMP twins (Title,
    Author, Subject, Creator, Producer, the dates); the XMP holds the title and the hidden accreditation (BLOCK each). What is not checked is
    said in an INFO (PDFA_NOT_CHECKED)."""
    import pikepdf
    p = Path(path)
    out = []
    try:
        pdf = pikepdf.open(str(p))
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-016", "BLOCK", f"{p.name} cannot be opened: {str(e)[:100]}", None)]
    with pdf:
        meta = pdf.open_metadata()
        part, conf = str(meta.get("pdfaid:part", "")), str(meta.get("pdfaid:conformance", ""))
        if part != "2" or conf.upper() != "B":
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name}: the XMP claims PDF/A part '{part}' conformance '{conf}', not 2B", [part, conf]))
        intents = pdf.Root.get("/OutputIntents")
        ok_intent = False
        for oi in (intents or []):
            prof = oi.get("/DestOutputProfile")
            if str(oi.get("/S")) == "/GTS_PDFA1" and prof is not None and int(prof.get("/N", 0)) == 3:
                ok_intent = True
        if not ok_intent:
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name} has no PDF/A output intent with an RGB ICC profile", None))
        if pdf.is_encrypted:
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name} is encrypted", None))
        names = pdf.Root.get("/Names")
        if names is not None and ("/JavaScript" in names or "/EmbeddedFiles" in names):
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name} carries JavaScript or embedded files", None))
        loose = [f["name"] for f in _font_report(pdf) if not f["embedded"]]
        if loose:
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name}: fonts are not embedded", loose))
        di = pdf.docinfo
        twins = {"/Title": str(meta.get("dc:title", "")), "/Author": (list(meta.get("dc:creator") or [""]) or [""])[0],
                 "/Subject": str(meta.get("dc:description", "")), "/Creator": str(meta.get("xmp:CreatorTool", "")), "/Producer": str(meta.get("pdf:Producer", ""))}
        differ = [k for k, v in twins.items() if k in di and str(di[k]) != v]
        for k, xk in (("/CreationDate", "xmp:CreateDate"), ("/ModDate", "xmp:ModifyDate")):
            if k in di:
                d = re.sub(r"\D", "", str(di[k]))[:14]
                x = re.sub(r"\D", "", str(meta.get(xk, "")))[:14]
                if d != x:
                    differ.append(k)
        if differ:
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name}: the Info dictionary and the XMP disagree on {', '.join(differ)}", differ))
        if title is not None and str(meta.get("dc:title", "")) != title:
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name}: the XMP title is not the map's title", str(meta.get("dc:title", ""))[:80]))
        pubs = [str(v) for v in (meta.get("dc:publisher") or [])]
        if not pubs or (accreditation and accreditation not in pubs):
            out.append(finding("MAP-OUT-016", "BLOCK", f"{p.name}: the XMP does not carry the hidden accreditation (dc:publisher)", pubs))
        version = str(pdf.pdf_version)
    if not out:
        out.append(finding("MAP-OUT-016", "INFO", f"{p.name}: PDF/A-2b as far as read back here (PDF {version}, output intent, fonts embedded, Info and XMP "
                           f"agree, title and accreditation in the XMP). Not checked: {PDFA_NOT_CHECKED}", {"pdf_version": version}))
    return out


def check_inkscape_svg(text: str, master: str) -> list[dict]:
    """MAP-OUT-017: the Inkscape SVG read back. The inkscape namespace is declared on the root; every `gm-layer-*` group, the map group and every
    furniture group (legend, title, footer, scale bar, north arrow, locator, labels, names, basemap) is a layer with a label (BLOCK); with the
    layer attributes taken out the file is the master SVG, byte for byte (BLOCK)."""
    import map_export as mx
    out = []
    head = text[:text.find(">", text.find("<svg")) + 1] if "<svg" in text else ""
    if f'xmlns:inkscape="{mx.INKSCAPE_NS}"' not in head:
        out.append(finding("MAP-OUT-017", "BLOCK", "the Inkscape SVG does not declare the inkscape namespace on its root", None))
    missing, layers = [], 0
    body = text.split("</defs>", 1)[-1]
    for m in re.finditer(r"<g\b([^>]*)>", body):
        attrs = m.group(1)
        gid = (re.search(r'\bid="([^"]*)"', attrs) or [None, ""])[1]
        cls = ((re.search(r'\bclass="([^"]*)"', attrs) or [None, ""])[1]).split()
        wanted = gid == "gm-map" or gid.startswith("gm-layer-") or "gm-base" in cls or any(c in mx.FURNITURE for c in cls)
        if not wanted:
            continue
        lab = re.search(r'inkscape:label="([^"]+)"', attrs)
        if 'inkscape:groupmode="layer"' in attrs and lab:
            layers += 1
        else:
            missing.append(gid or " ".join(cls))
    if missing:
        out.append(finding("MAP-OUT-017", "BLOCK", f"{len(missing)} group(s) are not Inkscape layers: {', '.join(missing[:5])}", missing[:10]))
    if mx.strip_inkscape(text) != master:
        out.append(finding("MAP-OUT-017", "BLOCK", "the Inkscape SVG differs from the map's SVG in more than its layer attributes", None))
    if not out:
        out.append(finding("MAP-OUT-017", "INFO", f"Inkscape SVG: {layers} layers (each map layer and each piece of furniture); nothing else changed", layers))
    return out


def check_outlined_svg(text: str, master: str) -> list[dict]:
    """MAP-OUT-018: the outlined-text SVG read back. No <text> is left and no font is needed (BLOCK); every text of the master is there as an
    outlined group, each with its words as its accessible name and title (BLOCK); the words are listed in the metadata (WARN); and the file
    passes MAP-OUT-003's checks (self-contained, no script, no keys)."""
    out = []
    try:
        from lxml import etree
        root = etree.fromstring(text.encode("utf-8"), _SAFE_XML(huge=True))
        mroot = etree.fromstring(master.encode("utf-8"), _SAFE_XML(huge=True))
    except Exception as e:  # noqa: BLE001
        return [finding("MAP-OUT-018", "BLOCK", f"the outlined SVG is not valid XML: {str(e)[:100]}", None)]
    S = "{http://www.w3.org/2000/svg}"
    left = list(root.iter(f"{S}text"))
    if left:
        out.append(finding("MAP-OUT-018", "BLOCK", f"{len(left)} text element(s) were not outlined", len(left)))
    want = [("".join(t.itertext())) for t in mroot.iter(f"{S}text") if t.get("stroke") in (None, "none") and "".join(t.itertext()).strip()]
    groups = [g for g in root.iter(f"{S}g") if g.get("class") == "gm-outlined"]
    named = [g.get("aria-label") for g in groups if g.get("aria-label") and g.find(f"{S}title") is not None and g.find(f"{S}path") is not None]
    if len(named) != len(want) or sorted(named) != sorted(want):
        out.append(finding("MAP-OUT-018", "BLOCK", f"{len(named)} outlined text(s) carry their words; the map has {len(want)} text(s)", [len(named), len(want)]))
    md = root.find(".//{https://acropolisdocs.com/ns/geo-mapping#}outlined_text")
    listed = (md.text or "").split("\n") if md is not None else []
    if sorted(listed) != sorted(want):
        out.append(finding("MAP-OUT-018", "WARN", "the metadata does not list every outlined text", None))
    if "@font-face" in text:
        out.append(finding("MAP-OUT-018", "BLOCK", "the outlined SVG still embeds a font; nothing in it should need one", None))
    out += [dict(f, id="MAP-OUT-018") if f["id"] == "MAP-OUT-003" else f for f in check_svg(text)]
    if not out:
        out.append(finding("MAP-OUT-018", "INFO", f"outlined SVG: {len(named)} texts drawn as Inter outlines, each with its words as its name; no font needed", len(named)))
    return out


def check_export(res: dict, svg: str) -> list[dict]:
    """The read-back findings for the export variants map_export.export wrote (`res['variants']`): MAP-OUT-014 to MAP-OUT-018."""
    v = res.get("variants") or {}
    size = tuple(res.get("size_mm") or (0, 0))
    out: list[dict] = []
    if "transparent" in v:
        out += check_transparent_png(v["transparent"]["file"], size, v["transparent"].get("frame_mm") or None, v["transparent"]["mode"])
    if "bleed" in v:
        out += check_bleed_pdf(v["bleed"]["file"], size, float(v["bleed"]["bleed_mm"]), bool(v["bleed"].get("crop_marks")))
    if "pdfa" in v:
        import html as _h
        acc = re.search(r"<geo:accreditation>(.*?)</geo:accreditation>", svg, re.S)
        out += check_pdfa(v["pdfa"]["file"], res.get("title"), _h.unescape(acc.group(1)) if acc else None)
    if "inkscape" in v:
        out += check_inkscape_svg(Path(v["inkscape"]["file"]).read_text(encoding="utf-8"), svg)
    if "outlined" in v:
        out += check_outlined_svg(Path(v["outlined"]["file"]).read_text(encoding="utf-8"), svg)
    return out
