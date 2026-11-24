#!/usr/bin/env python3
"""pdf_stack: the PDF libraries this plugin uses, loaded the way the acropolisdocs plugin loads them.

This file is self-contained. It does not import, call or depend on the acropolisdocs plugin; it carries the same components and the same
known workarounds so a fix found in one place can be applied to the other by name.

    weasyprint   renders the SVG sheet to a vector PDF (same renderer as acropolisdocs; >= 61).
    pypdf        reads a PDF back: page count, page size, fonts, images (same read-back library as acropolisdocs).
    pypdfium2    draws a PDF page to pixels for the PNG (same page rasterizer as acropolisdocs).
    pikepdf      WRITES PDF structure the others cannot: Info dictionary, XMP, page merge, GeoPDF. Kept for that only.

Workarounds carried over from acropolisdocs (weasyprint_import.py and its doctor):
  W1  Importing WeasyPrint loads Pango, GObject and HarfBuzz. A package that installed but whose native libraries are missing raises
      OSError at import, not ImportError. `weasyprint_status()` tells the two apart so the doctor can say which one to fix.
  W2  WeasyPrint's own fetcher reads any file: path and any http(s): URL a document names. A map is built from names and attributes nobody
      here wrote, so the module returned by `load_weasyprint()` is BOUNDED: HTML and CSS read data: URIs and files under this plugin only,
      and refuse the network. A caller that passes its own url_fetcher keeps it.
  W3  WeasyPrint 66 turned the fetcher function into a class (urls.URLFetcher); before that it was default_url_fetcher. Both are handled.
  W4  A page is rendered at its sheet size in mm with margin 0; see map_export.to_pdf_bytes.
  W5  (this plugin only, M16) WeasyPrint draws SVG paths in Python, about 40 microseconds a command; a 250,000-point map spent two minutes
      there. Straight-line paths in the plugin's own form are drawn by a fast path that makes the same PDF calls (see `_fast_paths`).
  W6  (this plugin only, 0.26.0) pydyf compresses every content stream at zlib level 9; on a 250,000-point map that is about 5 s for a
      file a few percent smaller than level 6 gives. A stream over ZLIB_FAST_BYTES (uncompressed) is compressed at level 6; smaller streams
      keep level 9, so every ordinary map's PDF is byte for byte what it was (see `_fast_zlib`, which checks that against pydyf first).

Not carried over: acropolisdocs also remembers the native-library lookups between processes (a start-up speed-up that needs its build
scratch store). This plugin imports WeasyPrint once per build, so the plain import is used.
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_BOUNDARY_FLAG = "_gm_render_boundary"


# ------------------------------------------------------------------------------------------------------------------ WeasyPrint
def refusal(url, roots) -> str | None:
    """None when a render may read `url`, else why not (W2)."""
    text = str(url or "")
    scheme = text.split(":", 1)[0].lower() if ":" in text else ""
    if scheme == "data":
        return None
    if scheme != "file":
        return f"{text[:120]} is not read: a render fetches nothing from the network"
    parts = urlsplit(text)
    if parts.netloc not in ("", "localhost"):
        return f"{text[:120]} is not read: a render reads no network share"
    p = os.path.realpath(url2pathname(unquote(parts.path)))
    for r in roots:
        if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
            return None
    return f"{p} is not read: it is outside this plugin"


def _fetcher(mod, roots):
    base = getattr(getattr(mod, "urls", None), "URLFetcher", None)
    if base is not None:                                 # W3: WeasyPrint 66+ is a class
        class _Bounded(base):
            def fetch(self, url, headers=None):
                why = refusal(url, roots)
                if why:
                    raise ValueError("render boundary: " + why)
                return super().fetch(url, headers)
        return _Bounded()
    default = mod.default_url_fetcher                    # WeasyPrint 61-65: a function

    def _fetch(url, *args, **kwargs):
        why = refusal(url, roots)
        if why:
            raise ValueError("render boundary: " + why)
        return default(url, *args, **kwargs)
    return _fetch


def _bound(mod):
    if getattr(mod, _BOUNDARY_FLAG, False):
        return mod
    if getattr(getattr(mod, "urls", None), "URLFetcher", None) is None and not callable(getattr(mod, "default_url_fetcher", None)):
        return mod
    roots = [os.path.realpath(PLUGIN_ROOT)]

    def _wrap(cls):
        class Bounded(cls):
            def __init__(self, *args, **kwargs):
                if kwargs.get("url_fetcher") is None:
                    kwargs["url_fetcher"] = _fetcher(mod, roots)
                super().__init__(*args, **kwargs)
        Bounded.__name__ = Bounded.__qualname__ = cls.__name__
        Bounded.__module__ = cls.__module__
        return Bounded

    mod.HTML = _wrap(mod.HTML)
    mod.CSS = _wrap(mod.CSS)
    setattr(mod, _BOUNDARY_FLAG, True)
    return mod


def load_weasyprint():
    """The `weasyprint` module, bounded (W2), with the fast path for straight-line SVG paths (W5). Raises what `import weasyprint` raises
    (ImportError, or OSError for W1)."""
    import weasyprint                                     # noqa: WPS433
    _fast_paths(weasyprint)
    _fast_zlib()
    return _bound(weasyprint)


# ------------------------------------------------------------------------------------------------------------------ W6: zlib level for big streams
ZLIB_FAST_BYTES = 4_000_000
ZLIB_FAST_LEVEL = 6


def _fast_zlib() -> bool:
    """Install W6 on pydyf.Stream.data when pydyf's own property gives the same bytes as this one for a small stream. Returns whether it is in."""
    try:
        import zlib
        import pydyf
    except ImportError:
        return False
    if getattr(pydyf.Stream, "_gmap_w6", False):
        return True
    stock = vars(pydyf.Stream).get("data")
    if not isinstance(stock, property):
        return False

    def data(self):
        stream = b"\n".join(pydyf._to_bytes(item) for item in self.stream)
        extra = pydyf.Dictionary(self.extra.copy())
        if self.compress:
            extra["Filter"] = "/FlateDecode"
            co = zlib.compressobj(level=9 if len(stream) <= ZLIB_FAST_BYTES else ZLIB_FAST_LEVEL)
            stream = co.compress(stream) + co.flush()
        extra["Length"] = len(stream)
        return b"\n".join((extra.data, b"stream", stream, b"endstream"))

    try:                                                  # the self-test: the same bytes as pydyf's own for a small stream, compressed and not
        for comp in (True, False):
            t = pydyf.Stream(compress=comp)
            t.move_to(0, 0)
            t.line_to(10, 10)
            t.stroke()
            if data(t) != stock.fget(t):
                return False
    except Exception:  # noqa: BLE001 - another pydyf: leave it as it is
        return False
    pydyf.Stream.data = property(data)
    pydyf.Stream._gmap_w6 = True
    return True


# ------------------------------------------------------------------------------------------------------------------ W5: straight-line paths
# WeasyPrint draws an SVG <path> in Python, one command at a time: normalising the text, a regular expression and a unit lookup per number,
# and the angle at every vertex for markers. On a map of 250,000 points or 100,000 polygons that is most of the PDF time (about 40 microseconds
# a command). This plugin writes its own paths in one narrow form: absolute or relative M, L, H, V and Z commands with plain decimal
# numbers, one space between x and y. For a path that matches that form exactly and has no markers, the fast path below parses the numbers
# with one regular expression and makes the same calls on the PDF stream, in the same order, with the same floats, as WeasyPrint's own
# `path()` (including the `0 0 m` it starts every path with). Anything else (arcs, curves, commas, exponents, implicit repeats, markers) goes
# to WeasyPrint unchanged. The fast path is installed only after a self-test compares its calls with WeasyPrint's on sample paths, so a
# WeasyPrint that changes how it draws paths simply keeps its own. GMAP_PDF_FASTPATH=0 turns it off.
_NUM = r"-?\d+(?:\.\d+)?"
_FAST_D = re.compile(rf"(?:[MLml]{_NUM} {_NUM}|[HVhv]{_NUM}|[Zz])*")
_FAST_CMD = re.compile(rf"([MLmlHVhvZz])(?:({_NUM})(?: ({_NUM}))?)?")
# The plugin's commonest paths (polygon rings, lines and round point symbols drawn as dots) use absolute M and L, `h0` and Z with numbers in
# canonical form: no trailing zeros, no leading zeros, at most six decimals. For those, the text WeasyPrint's PDF writer gives a number is the
# number's own text (pydyf writes a whole float as an integer and any other with six decimals, trailing zeros stripped), so the content stream
# is made by text substitution, without a float or a call per number. `M a bh0` is a dot: move to (a, b), line to (a + 0, b).
_CANON = r"(?:-?(?:0|[1-9]\d{0,6})\.\d{0,5}[1-9]|0|-?[1-9]\d{0,6})(?![.\d])"      # the whole number, or no match
_TEXT_D = re.compile(rf"(?:M{_CANON} {_CANON}(?:h0|(?:L{_CANON} {_CANON})+Z?))*")
_TEXT_SUBS = (
    (re.compile(rf"M({_CANON}) ({_CANON})h0"), "\n\\1 \\2 m\n\\1 \\2 l"),
    (re.compile(rf"M({_CANON}) ({_CANON})"), "\n\\1 \\2 m"),
    (re.compile(rf"L({_CANON}) ({_CANON})"), "\n\\1 \\2 l"),
)
_FAST_FLAG = "_gm_fast_paths"
_MARKER_ATTRS = ("marker", "marker-start", "marker-mid", "marker-end")


def _text_line_path(stream, d: str) -> bool:
    """The canonical form as one block of PDF operators appended to the stream: the bytes WeasyPrint writes for the same path (its own
    `0 0 m`, then one operator a line). Returns False (and appends nothing) when the stream is not a plain pydyf operator list."""
    ops = getattr(stream, "stream", None)
    if not isinstance(ops, list):
        return False
    s = d
    for rx, rep in _TEXT_SUBS:
        s = rx.sub(rep, s)
    ops.append(("0 0 m" + s.replace("Z", "\nh")).encode("ascii"))
    return True


def _draw_line_path(stream, d: str) -> None:
    """WeasyPrint's path() for the straight-line form, as stream calls (see W5). `d` has been checked against _FAST_D."""
    move_to, line_to, close = stream.move_to, stream.line_to, stream.close
    cx, cy = 0, 0
    move_to(cx, cy)
    last = None
    first = None
    for letter, a, b in _FAST_CMD.findall(d):
        if last in (None, "m", "M", "z", "Z"):
            first = None
        if letter not in "mMzZ" and first is None:
            first = (cx, cy)
        if letter == "M":
            cx, cy = float(a), float(b)
            move_to(cx, cy)
        elif letter == "L":
            cx, cy = float(a), float(b)
            line_to(cx, cy)
        elif letter == "H":
            cx = float(a)
            line_to(cx, cy)
        elif letter == "V":
            cy = float(a)
            line_to(cx, cy)
        elif letter == "m":
            cx, cy = float(a) + cx, float(b) + cy
            move_to(cx, cy)
        elif letter == "l":
            cx, cy = float(a) + cx, float(b) + cy
            line_to(cx, cy)
        elif letter == "h":
            cx = float(a) + cx
            line_to(cx, cy)
        elif letter == "v":
            cy = float(a) + cy
            line_to(cx, cy)
        elif first:                                       # Z or z: WeasyPrint closes only a subpath that drew something
            close()
            cx, cy = first
        last = letter


def _fast_paths(mod) -> bool:
    """Install W5 on `weasyprint.svg` when the self-test passes. Returns whether it is installed."""
    if os.environ.get("GMAP_PDF_FASTPATH") == "0":
        return False
    try:
        svgmod = sys.modules.get("weasyprint.svg") or __import__("weasyprint.svg", fromlist=["TAGS"])
        tags = getattr(svgmod, "TAGS", None)
        stock = tags.get("path") if isinstance(tags, dict) else None
        if stock is None:
            return False
        if getattr(stock, _FAST_FLAG, False):
            return True
        if not _fast_path_selftest(stock):
            return False

        def path(svg, node, font_size):
            d = node.get("d", "") or ""
            if not any(k in node.attrib for k in _MARKER_ATTRS):
                if _TEXT_D.fullmatch(d) and _text_line_path(svg.stream, d):
                    return None
                if _FAST_D.fullmatch(d):
                    _draw_line_path(svg.stream, d)
                    return None
            return stock(svg, node, font_size)
        setattr(path, _FAST_FLAG, True)
        path.stock = stock
        tags["path"] = path
        return True
    except Exception:  # noqa: BLE001 - the fast path is an optimisation; WeasyPrint's own drawing is always correct
        return False


class _Recorder:
    def __init__(self):
        self.calls = []

    def move_to(self, x, y):
        self.calls.append(("m", repr(x), repr(y)))

    def line_to(self, x, y):
        self.calls.append(("l", repr(x), repr(y)))

    def close(self):
        self.calls.append(("h",))

    def __getattr__(self, name):                          # any other drawing call means the forms differ: record it so the test fails
        return lambda *a, **k: self.calls.append((name,) + tuple(map(repr, a)))


class _Node(dict):
    def __init__(self, d):
        super().__init__(d=d)
        self.attrib = self
        from collections import deque
        self.vertices = deque()


SELFTEST_PATHS = ("M10 20L30.5 -4.25L0 0Z", "M1 1h0M2.5 3h0M-4 -0.5h0", "M0 0h5v5h-5Z", "M3 4l1 1l-2 0zM5 5H7V9H5Z", "M1 1Z", "m2 3l1 1Zl4 4",
                  "M12.34 56.78L12.34 56.78L90 10.01Z", "M0.1 0.2l0.1 0.2l0.1 0.2h-0.3v-0.6z", "M5.5 0.05L-0.5 -12.25L1234567.123456 7ZM0 0L1 0",
                  "M-3 4h0M0 -0.01h0", "M1.5 2L3 4M5 6L7 8L9 10Z", "")
SELFTEST_NOT_TEXT = ("M1 5.50L2 3", "M01 2L3 4", "M1 2L3 4.0", "M-0 1h0", "M1 2h1", "M1 2L3 4zM5 6L7 8", "M1e3 2L3 4", "M1 2 3 4")


def _fast_path_selftest(stock) -> bool:
    """True when both fast forms give exactly what WeasyPrint gives on the sample paths: the same stream calls with the same floats for the
    line form, and the same content-stream bytes for the canonical form; and when the canonical form refuses what it must refuse."""
    from types import SimpleNamespace
    import pydyf

    def svg_for(stream):
        return SimpleNamespace(stream=stream, point=lambda x, y, fs: (float(x), float(y)))

    for d in SELFTEST_PATHS:
        if not _FAST_D.fullmatch(d):
            return False
        want, got = _Recorder(), _Recorder()
        stock(svg_for(want), _Node(d), 12.0)
        _draw_line_path(got, d)
        if want.calls != got.calls:
            return False
        if _TEXT_D.fullmatch(d):
            a, b = pydyf.Stream(compress=False), pydyf.Stream(compress=False)
            stock(svg_for(a), _Node(d), 12.0)
            if not _text_line_path(b, d) or a.data != b.data:
                return False
    return not any(_TEXT_D.fullmatch(d) for d in SELFTEST_NOT_TEXT)


def weasyprint_status() -> dict:
    """{ok, installed, error, fix}: tells a missing package from missing native libraries (W1)."""
    try:
        import weasyprint                                 # noqa: F401, WPS433
        return {"ok": True, "installed": True, "error": None, "fix": None}
    except ImportError as ex:
        return {"ok": False, "installed": False, "error": str(ex)[:120], "fix": "pip install weasyprint"}
    except OSError as ex:
        first = (str(ex).splitlines() or [""])[0][:90]
        if sys.platform == "win32":
            fix = "install the GTK runtime for Windows (it provides Pango, GObject and HarfBuzz), then run the doctor again"
        elif sys.platform == "darwin":
            fix = "brew install pango, then run the doctor again"
        else:
            fix = "install the native libraries with the system package manager (Debian and Ubuntu: libpango-1.0-0 libpangoft2-1.0-0), then run the doctor again"
        return {"ok": False, "installed": True, "error": first, "fix": f"WeasyPrint is installed but its native libraries (Pango, GObject, HarfBuzz) cannot be loaded ({first}); {fix}"}


def html_to_pdf(html: str, pdf_variant: str | None = None) -> bytes:
    """`pdf_variant` is WeasyPrint's (M16 uses 'pdf/a-2b'); the W5 fast path draws the same content stream in every variant."""
    doc = load_weasyprint().HTML(string=html)
    return doc.write_pdf(pdf_variant=pdf_variant) if pdf_variant else doc.write_pdf()


# ------------------------------------------------------------------------------------------------------------------ pypdf (read-back)
def read_pdf(source):
    """A pypdf.PdfReader over a path or bytes."""
    from pypdf import PdfReader
    return PdfReader(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else str(source))


def page_sizes_pt(reader, box: str = "mediabox") -> list[tuple[float, float]]:
    """Each page's size in points: the MediaBox, or `box='trimbox'` for the finished sheet of a page with bleed (pypdf falls back to the
    CropBox and then the MediaBox when a page has no TrimBox, so a page without bleed gives the same answer either way)."""
    out = []
    for pg in reader.pages:
        b = getattr(pg, box)
        out.append((float(b.right) - float(b.left), float(b.top) - float(b.bottom)))
    return out


def font_report(reader) -> list[dict]:
    """[{name, embedded}] for every font on every page, form XObjects included."""
    fonts: dict[str, bool] = {}

    def walk(res, seen):
        if res is None:
            return
        res = res.get_object()
        fd_all = res.get("/Font")
        for k in (fd_all.get_object().keys() if fd_all is not None else []):
            fo = fd_all.get_object()[k].get_object()
            name = str(fo.get("/BaseFont", "")).lstrip("/")
            fdesc = fo.get("/FontDescriptor")
            if fo.get("/Subtype") == "/Type0":
                d = fo["/DescendantFonts"].get_object()[0].get_object()
                fdesc = d.get("/FontDescriptor")
            fdesc = fdesc.get_object() if fdesc is not None else None
            fonts[name] = bool(fdesc is not None and any(x in fdesc for x in ("/FontFile", "/FontFile2", "/FontFile3")))
        xo = res.get("/XObject")
        for k in (xo.get_object().keys() if xo is not None else []):
            o = xo.get_object()[k].get_object()
            if o.get("/Subtype") == "/Form" and id(o) not in seen:
                seen.add(id(o))
                walk(o.get("/Resources"), seen)

    for page in reader.pages:
        try:
            walk(page.get("/Resources"), set())
        except Exception:                                  # noqa: BLE001
            continue
    return [{"name": n, "embedded": e} for n, e in sorted(fonts.items())]


def image_sizes(reader) -> list[tuple[int, int]]:
    out = []
    for page in reader.pages:
        try:
            xo = page["/Resources"].get("/XObject")
            for k in (xo.get_object().keys() if xo is not None else []):
                o = xo.get_object()[k].get_object()
                if o.get("/Subtype") == "/Image":
                    out.append((int(o["/Width"]), int(o["/Height"])))
        except Exception:                                  # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------------------------------------------------------------ pypdfium2 (pixels)
def pdfium_available() -> bool:
    try:
        import pypdfium2                                   # noqa: F401
        return True
    except Exception:                                      # noqa: BLE001
        return False


def render_png(pdf_bytes: bytes, dpi: float, alpha: bool = False) -> bytes:
    """Page 1 of a PDF as PNG bytes at `dpi` (pypdfium2; its scale is relative to 72 points per inch).
    The pixel size is ceil(MediaBox points x dpi / 72), exactly what pdftoppm writes (measured for letter, tabloid, arch D, 16:9 slide and
    square sheets at 72, 150, 200 and 300 dpi), so the picture is the same size whichever rasterizer made it. pdfium measures the page in
    32-bit floats and would give 960 where pdftoppm gives 961 for a page 960.000094 points wide, so the size comes from the file's own MediaBox.
    `alpha` (M16, a transparent PNG) draws on a see-through bitmap and keeps the alpha channel: RGBA instead of RGB."""
    import math
    import pypdfium2 as pdfium
    w_pt, h_pt = page_sizes_pt(read_pdf(pdf_bytes))[0]
    tw, th = max(1, math.ceil(w_pt * dpi / 72.0)), max(1, math.ceil(h_pt * dpi / 72.0))
    doc = pdfium.PdfDocument(pdf_bytes)
    mode = "RGBA" if alpha else "RGB"
    try:
        page = doc[0]
        if alpha:
            bitmap = page.render(scale=max(tw / w_pt, th / h_pt), fill_color=(255, 255, 255, 0))
        else:
            bitmap = page.render(scale=max(tw / w_pt, th / h_pt))
        img = bitmap.to_pil().convert(mode)
        if img.size != (tw, th):
            from PIL import Image
            canvas = Image.new(mode, (tw, th), (255, 255, 255, 0) if alpha else (255, 255, 255))
            canvas.paste(img.crop((0, 0, min(tw, img.width), min(th, img.height))), (0, 0))
            img = canvas
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    finally:
        doc.close()
