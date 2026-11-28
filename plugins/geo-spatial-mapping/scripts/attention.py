#!/usr/bin/env python3
"""attention: where a picture draws the eye, and how much of it a set of the map's own layers holds (5.6.0, Q-01).

A measurement, never a grade: the spectral-residual saliency (Hou and Zhang 2007) the draft readings report, and the share of it that falls on
the marks of the layers a map is about. The build reads that share to decide whether its subject needs a stronger boost; the evaluator
(map_evaluate) takes the same saliency and colour arithmetic from here, so the two measure alike. Nothing here reads a score, a rubric or a
record of an evaluation.
"""
from __future__ import annotations

import io

import numpy as np

FURNITURE = ("gm-title", "gm-legend", "gm-inset", "gm-logo", "gm-north", "gm-footer", "gm-scalebar", "gm-callout", "gm-data-table",
             "gm-provider-logo", "gm-chart", "gm-story")
CONTEXT = ("gm-base", "gm-names", "gm-graticule", "gm-focus-mask", "gm-focus-lift", "gm-relief")
DPI = 72.0                    # the renders the share is measured on (the rasterizer's floor)
INK_DE = 3.0                  # a pixel is the layer's mark where the layer alone differs from the bare sheet by more than this (CIE76)


def blur(arr, radius: float):
    from PIL import Image, ImageFilter
    a = arr.astype(np.float64)
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-12:
        return a
    im = Image.fromarray(np.uint8(255 * (a - lo) / (hi - lo)))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float64) / 255 * (hi - lo) + lo


def saliency(rgb):
    """Spectral-residual saliency on a 64-px-wide grey copy, blurred, scaled back to the picture, 0..1."""
    from PIL import Image
    g = (rgb @ np.array([0.299, 0.587, 0.114]))
    h, w = g.shape
    small_w = 64
    small_h = max(8, round(h * small_w / w))
    s = np.asarray(Image.fromarray(np.uint8(g * 255)).resize((small_w, small_h), Image.BILINEAR), dtype=np.float64) / 255
    F = np.fft.fft2(s)
    logamp = np.log(np.abs(F) + 1e-9)
    phase = np.angle(F)
    k = np.ones((3, 3)) / 9
    pad = np.pad(logamp, 1, mode="edge")
    avg = sum(pad[i:i + logamp.shape[0], j:j + logamp.shape[1]] * k[i, j] for i in range(3) for j in range(3))
    sal = np.abs(np.fft.ifft2(np.exp(logamp - avg + 1j * phase))) ** 2
    sal = blur(sal, 2.5)
    sal = (sal - sal.min()) / (sal.max() - sal.min() + 1e-12)
    return np.asarray(Image.fromarray(np.uint8(sal * 255)).resize((w, h), Image.BILINEAR), dtype=np.float64) / 255


def lab(rgb):
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def de(a, b):
    return np.sqrt(((a - b) ** 2).sum(axis=-1))


def _rgb(png: bytes):
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64) / 255.0


def _png(svg_text: str, dpi: float) -> bytes:
    import map_export as mx
    return mx.rasterize(mx.to_pdf_bytes(svg_text, "attention"), dpi, svg_text)


def only_layers(svg_text: str, keep: set[str]) -> str:
    """The SVG with only the `keep` data layers (by their gm-layer-<id> groups), and no furniture, context or text."""
    from lxml import etree

    import map_preview as pv
    root = etree.fromstring(svg_text.encode("utf-8"), pv._parser())
    ns = "{http://www.w3.org/2000/svg}"
    kill = list(root.iter(f"{ns}text"))
    for g in root.iter(f"{ns}g", f"{ns}image"):
        cls = (g.get("class") or "").split()
        gid = g.get("id") or ""
        if any(c.startswith(FURNITURE) for c in cls) or any(c.startswith(CONTEXT) for c in cls):
            kill.append(g)
        elif gid.startswith("gm-layer-") and g.get("data-layer") not in keep:
            kill.append(g)
    for el in kill:
        p = el.getparent()
        if p is not None:
            p.remove(el)
    return etree.tostring(root, encoding="unicode")


def _rgbs(svgs: list[str], dpi: float) -> list:
    """5.8.0: the pictures as 0..1 RGB arrays from one PDF with a page each: one WeasyPrint call for all of them (each call carries about a
    second of fixed cost, and the measure drew three a build, six when it boosted). Falls back to a PDF per picture without pypdfium2."""
    import re

    import map_export as mx
    import pdf_stack
    if not pdf_stack.pdfium_available():
        return [_rgb(_png(s, dpi)) for s in svgs]
    W, H = mx.svg_size_mm(svgs[0])
    faces = "\n".join(dict.fromkeys(re.findall(r"@font-face\s*\{.*?\}\s*", svgs[0], re.S)))
    pages = "".join(f'<div class="p">{s}</div>' for s in svgs)
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>attention</title><style>{faces}'
            f"@page{{size:{W}mm {H}mm;margin:0}}html,body{{margin:0;padding:0;font-family:Inter,sans-serif}}"
            f".p{{width:{W}mm;height:{H}mm;overflow:hidden;page-break-after:always;break-after:page}}.p:last-child{{page-break-after:auto;break-after:auto}}"
            f"svg{{display:block;width:{W}mm;height:{H}mm}}</style></head><body>{pages}</body></html>")
    imgs = pdf_stack.render_pages_rgb(pdf_stack.html_to_pdf(html), dpi)
    if len(imgs) != len(svgs):
        return [_rgb(_png(s, dpi)) for s in svgs]
    out = []
    for im in imgs:                                  # the sheet's nominal pixel size, as rasterize gives it (map_export.nominal_pixels)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out.append(_rgb(mx.nominal_pixels(buf.getvalue(), W, H, dpi)))
    return out


def layer_share(svg_text: str, ids, dpi: float = DPI) -> float | None:
    """The share of the whole picture's attention that falls on the marks of the layers `ids`. None when it cannot be measured here.
    5.8.0: the three pictures (the map, the subject alone, the bare sheet) are drawn in one pass (_rgbs)."""
    try:
        full, subj, bare = _rgbs([svg_text, only_layers(svg_text, set(ids)), only_layers(svg_text, set())], dpi)
        sal = saliency(full)
        a = lab(subj)
        b = lab(bare)
        if a.shape != b.shape or a.shape[:2] != sal.shape:
            return None
        mask = de(a, b) > INK_DE
        tot = float(sal.sum())
        return round(float(sal[mask].sum() / tot), 3) if tot > 0 and mask.any() else None
    except Exception:  # noqa: BLE001 - a measure that cannot run never stops a build
        return None
