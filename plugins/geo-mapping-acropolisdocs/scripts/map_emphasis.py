"""Visual hierarchy between a map's data layers (1.13.0).

Every data layer used to be drawn at full strength, so a supporting layer (transmission lines under a rate map, say) competed with the
layers that carry the map's point. Each data layer now has an emphasis:

    primary      full strength: the layers the map is about
    supporting   drawn as one see-through group at SUPPORTING_OPACITY: there for context, readable, quieter
    background   at BACKGROUND_OPACITY: barely there, for orientation only

A layer sets it with `emphasis:` (primary, supporting, background, or auto, the default). Under auto the map's `message` decides: a layer
whose id, legend title or legend label shares a word with the message is primary, and the other auto layers are supporting. Nothing changes
when only one data layer is drawn, when no layer matches the message and none is marked primary, or when `style.hierarchy: off` (and under
the classic look, which keeps the 0.16 drawing). The legend's keys are drawn at the same strength as the layer, so key and map agree. The No
data class of a classed area layer is drawn half see-through (NODATA_OPACITY) for the same reason: it is not what the map is about.

Opacity only: the colours stay the brand's own (MAP-BRAND-001), they just let the ground through.
"""
from __future__ import annotations

import re

EMPHASIS = ("auto", "primary", "supporting", "background")
SUPPORTING_OPACITY = 0.55
BACKGROUND_OPACITY = 0.3
OPACITY = {"primary": 1.0, "supporting": SUPPORTING_OPACITY, "background": BACKGROUND_OPACITY}
NODATA_OPACITY = 0.5           # the No data class of a classed area layer: drawn half see-through, so the grey does not read as a class of its own
FULL_STRENGTH_WARN = 3          # MAP-CART-038 when this many data layers or more are all drawn at full strength
SKIP_ROLES = ("context", "contour", "hillshade", "raster")

STOP = {"the", "and", "for", "with", "from", "that", "this", "which", "while", "where", "what", "when", "into", "onto", "over", "under", "than",
        "then", "their", "there", "these", "those", "them", "they", "its", "it's", "are", "was", "were", "been", "being", "has", "have", "had",
        "not", "but", "all", "any", "each", "per", "own", "out", "off", "one", "two", "three", "more", "most", "less", "least", "much", "many",
        "why", "how", "who", "whom", "our", "your", "his", "her", "about", "around", "near", "across", "between", "within", "without", "also",
        "only", "very", "some", "such", "same", "other", "both", "even", "just", "like", "comes", "come", "make", "makes", "made", "show", "shows",
        "map", "maps", "layer", "layers", "data", "number", "numbers", "count", "counts", "share", "total", "times", "time", "year", "years",
        "above", "below", "and/or", "via", "every", "where's", "it", "is", "of", "in", "on", "to", "a", "an", "by", "or", "as", "at", "be"}


def stem(w: str) -> str:
    w = w.lower().strip("'’")
    if w.endswith("'s") or w.endswith("’s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w.endswith("us"):
        return w[:-1]
    return w


def words(text) -> set[str]:
    """The content words of a text, stemmed (plural s and ies): 'Hydro dams, 100 MW' -> {hydro, dam}. Numbers and short words are left out."""
    out = set()
    for w in re.split(r"[^\w’']+", str(text or "")):
        if len(w) < 3 or w.isdigit() or any(c.isdigit() for c in w):
            continue
        s = stem(w)
        if s not in STOP and w.lower() not in STOP and len(s) >= 3:
            out.add(s)
    return out


def layer_words(l) -> set[str]:
    ly = l.spec or {}
    lg = ly.get("legend") if isinstance(ly.get("legend"), dict) else {}
    return words(" ".join(str(v) for v in (l.id.replace("_", " ").replace("-", " "), l.title, lg.get("title"), lg.get("label"),
                                             ly.get("name")) if v))


def config(spec: dict, look: str) -> str:
    st = spec.get("style") if isinstance(spec.get("style"), dict) else {}
    h = st.get("hierarchy")
    if h in (False, "off"):
        return "off"
    if h is None and look == "classic":
        return "off"
    return "auto"


def candidates(plan) -> list:
    return [l for l in plan.layers if l.role not in SKIP_ROLES and l.kind != "raster" and getattr(l, "context_kind", None) is None]


def assign(plan) -> dict:
    """Give each data layer its emphasis and draw strength. -> {layer id: emphasis}; also plan.emphasis, notes and MAP-CART-038."""
    mode = config(plan.spec, getattr(plan, "look", "classic"))
    layers = candidates(plan)
    asked = {l.id: str((l.spec or {}).get("emphasis") or "auto") for l in layers}
    tiers: dict[str, str] = {lid: v for lid, v in asked.items() if v in OPACITY}
    autos = [l for l in layers if asked[l.id] == "auto"]
    why = ""
    if autos:
        if mode == "off" or len(layers) < 2:
            tiers.update({l.id: "primary" for l in autos})
            why = "style.hierarchy off" if mode == "off" else "one data layer"
        else:
            msg = words(plan.spec.get("message"))
            hit = {l.id for l in autos if layer_words(l) & msg}
            any_primary = bool(hit) or any(v == "primary" for v in tiers.values())
            if not any_primary:
                tiers.update({l.id: "primary" for l in autos})
                why = "no layer is named in the message"
            else:
                for l in autos:
                    tiers[l.id] = "primary" if l.id in hit else "supporting"
                why = "the layers the message names are primary"
    for l in layers:
        tier = tiers.get(l.id, "primary")
        l.emphasis = tier
        k = OPACITY[tier]
        if k < 1:
            l.group_opacity = float(getattr(l, "group_opacity", 1.0) or 1.0) * k
            if isinstance(l.legend_block, dict):
                l.legend_block = {**l.legend_block, "key_opacity": k}
        if mode != "off" and l.cls is not None and getattr(l.cls, "nodata", 0) and l.kind == "polygon":
            l.nodata_opacity = NODATA_OPACITY
            if isinstance(l.legend_block, dict):
                l.legend_block = {**l.legend_block, "items": [({**it, "opacity": NODATA_OPACITY * float(it.get("opacity", 1.0))} if it.get("nodata") else it)
                                                              for it in l.legend_block.get("items") or []]}
    plan.emphasis = {l.id: l.emphasis for l in layers}
    quiet = [l for l in layers if l.emphasis != "primary"]
    if quiet:
        plan.notes.append("emphasis: " + "; ".join(f"{l.id} {l.emphasis} ({OPACITY[l.emphasis]:.0%})" for l in quiet)
                          + f"; primary at full strength: {', '.join(l.id for l in layers if l.emphasis == 'primary') or 'none'}"
                          + (f" ({why})" if why else "") + ". A layer's `emphasis:` sets it by hand.")
    elif len(layers) >= FULL_STRENGTH_WARN and mode != "off":
        plan.findings.append({"id": "MAP-CART-038", "severity": "WARN", "detail": {"layers": [l.id for l in layers]},
                              "message": f"all {len(layers)} data layers are drawn at full strength, so nothing stands out "
                                         f"({why or 'every layer is marked primary'}); give the layers that only support the point "
                                         "emphasis: supporting (drawn at 55 percent) or background (30 percent), or name the main layers in the message"})
    return plan.emphasis
