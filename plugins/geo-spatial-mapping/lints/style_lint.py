"""style-lint (1.4.0, R2-PACK): a style pack checked before it is used. MAP-STYLE-001 (schema and file hashes) and 004 (font licences) are
raised by map_style.load; this module adds what needs the pack's resolved colours:

  MAP-STYLE-005 BLOCK  the palette fails the contrast budget: the emphasis and categorical colours 3:1 or more against the land, the labels
                       4.5:1 against their halo; or the categorical colours are not distinct under colour-vision simulation (delta E 10, the
                       0.17.0 rule for categories) - unless the pack tells categories apart by pattern as well (category_patterns)

Every mode the pack draws is checked (print and screen), against the brand a build uses (a brand-derived palette changes with the brand).
"""
from __future__ import annotations

import colorlib as cl

DATA_ON_GROUND_MIN = 3.0
LABEL_MIN = 4.5
CAT_CVD_MIN = 10.0
CATS_CHECKED = 4              # the first categories a map usually shows; later ones are told apart by order and label as well


def finding(fid: str, sev: str, msg: str, detail=None) -> dict:
    d = {"id": fid, "severity": sev, "message": msg}
    if detail is not None:
        d["detail"] = detail
    return d


def lint_palette(style, brand_modes: dict) -> list[dict]:
    import map_style
    if style is None or style.palette == "brand":
        return []                                       # the brand's own palette was checked when the brand was imported
    out = []
    patterns = bool((style.treatments or {}).get("category_patterns"))
    for mode, t in map_style.palette_modes(style, brand_modes).items():
        low = []
        for name, c in [("emphasis", t["emphasis"])] + [(f"categorical {i + 1}", c) for i, c in enumerate(t["categorical"][:CATS_CHECKED])]:
            r = cl.contrast(c, t["land"])
            if r < DATA_ON_GROUND_MIN:
                low.append(f"{name} {c} {r:.2f}:1")
        if low:
            out.append(finding("MAP-STYLE-005", "BLOCK", f"style pack '{style.id}' ({mode}): data colours under {DATA_ON_GROUND_MIN:g}:1 against the land "
                               f"{t['land']}: " + "; ".join(low)))
        r = cl.contrast(t["label"], t["label_halo"])
        if r < LABEL_MIN:
            out.append(finding("MAP-STYLE-005", "BLOCK", f"style pack '{style.id}' ({mode}): labels {t['label']} on their halo {t['label_halo']} are {r:.2f}:1 "
                               f"(at least {LABEL_MIN:g}:1)"))
        if not patterns:
            cats = t["categorical"][:CATS_CHECKED]
            weak = [(a, b, round(cl.cvd_min_delta_e(a, b), 1)) for i, a in enumerate(cats) for b in cats[i + 1:] if cl.cvd_min_delta_e(a, b) < CAT_CVD_MIN]
            if weak:
                out.append(finding("MAP-STYLE-005", "BLOCK", f"style pack '{style.id}' ({mode}): categorical colours too close under colour-vision simulation "
                                   f"(delta E under {CAT_CVD_MIN:g})", weak[:4]))
    return out
