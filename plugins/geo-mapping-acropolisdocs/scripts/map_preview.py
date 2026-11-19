#!/usr/bin/env python3
"""map_preview: the two pictures a map is looked at with while it is being made (1.22.0).

    draft   <stem>_draft.png   the whole sheet at DRAFT_DPI, written by `build_map.py --draft` in place of the SVG, PDF and full-size PNG, so
                               the look-and-fix loop costs one small picture instead of the full set (a finished map took 8 to 16 builds)
    mute    <stem>_mute.png    the same sheet with the words taken off: no title, subtitle or tagline, no callouts, no data table, no footer,
                               and the legend's swatches without their labels. Place names and data labels stay: they are part of the map.

The mute picture is for one question, asked of whoever (or whichever model) is making the map: with no words, what does this map say? If the
answer is not the MapSpec's `message`, the drawing is not carrying it yet, and the fix is in the frame, the emphasis or the labels, not in
another callout. Both pictures are working files: they carry no file properties, are not listed in the provenance or the terms register, and
are never a deliverable.
"""
from __future__ import annotations

from pathlib import Path

DRAFT_DPI = 96.0
MUTE_CLASSES = ("gm-title", "gm-callout", "gm-data-table", "gm-footer")     # whole groups that are words
SVG_NS = "{http://www.w3.org/2000/svg}"


def _parser():
    from lxml import etree
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)


def mute_svg(svg: str) -> tuple[str, dict]:
    """The sheet without its words. -> (svg, {removed: {class: n}, legend_texts: n})."""
    from lxml import etree
    root = etree.fromstring(svg.encode("utf-8"), _parser())
    removed: dict = {}
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        cls = (el.get("class") or "").split()
        hit = next((c for c in MUTE_CLASSES if c in cls), None)
        if hit and el.getparent() is not None:
            el.getparent().remove(el)
            removed[hit] = removed.get(hit, 0) + 1
    n = 0
    for mk in [e for e in root.iter() if isinstance(e.tag, str) and "gm-marks" in (e.get("class") or "").split()]:
        for tx in [e for e in mk.iter() if isinstance(e.tag, str) and e.tag in (SVG_NS + "text", "text")]:      # a mark's drawing stays; its words go
            if tx.getparent() is not None:
                tx.getparent().remove(tx)
                removed["gm-mark-label"] = removed.get("gm-mark-label", 0) + 1
    for lg in [e for e in root.iter() if isinstance(e.tag, str) and "gm-legend" in (e.get("class") or "").split()]:
        for tx in [e for e in lg.iter() if isinstance(e.tag, str) and e.tag in (SVG_NS + "text", "text")]:
            if tx.getparent() is not None:
                tx.getparent().remove(tx)
                n += 1
    return etree.tostring(root, encoding="unicode"), {"removed": removed, "legend_texts": n}


def png_bytes(svg: str, dpi: float = DRAFT_DPI) -> bytes:
    import map_export as mx
    return mx.rasterize(mx.to_pdf_bytes(svg, "preview"), dpi, svg)


def write_draft(svg: str, out_dir, stem: str, dpi: float = DRAFT_DPI) -> Path:
    p = Path(out_dir) / f"{stem}_draft.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(png_bytes(svg, dpi))
    return p


def write_mute(svg: str, out_dir, stem: str, dpi: float = DRAFT_DPI) -> tuple[Path, dict]:
    msvg, info = mute_svg(svg)
    p = Path(out_dir) / f"{stem}_mute.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(png_bytes(msvg, dpi))
    return p, info


MUTE_QUESTION = ("Look at the mute picture before you answer: with no title, legend text or callout, what does this map say? "
                 "If that is not the message, change the frame (extent: {mode: fit_message}), the emphasis (emphasis:, highlight:) or the "
                 "labels, not the words.")
