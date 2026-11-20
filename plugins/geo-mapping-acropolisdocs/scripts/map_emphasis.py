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

import numpy as np

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


def subject_layers(spec: dict, layers) -> list:
    """1.22.0: the layers a map is about, known before anything is styled: the ones marked `emphasis: primary`, and the ones the `message`
    names (the same word test as `assign`); when neither picks one, the layers that carry a `highlight:`. An empty list means the message
    singles nothing out, and the whole of the data is the subject. `extent: {mode: fit_message}` frames these (map_plan), and the message
    check measures them (lints/story_lint.py)."""
    cands = [l for l in layers if l.role not in SKIP_ROLES and l.kind != "raster" and getattr(l, "context_kind", None) is None]
    msg = words((spec or {}).get("message"))
    asked = {l.id: str((l.spec or {}).get("emphasis") or "auto") for l in cands}
    hit = [l for l in cands if asked[l.id] == "primary" or (asked[l.id] == "auto" and layer_words(l) & msg)]
    if hit and len(hit) < len(cands):
        return hit
    marked = [l for l in cands if (l.spec or {}).get("highlight") is not None]
    return marked


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


# ---------------------------------------------------------------------------------------------------------------- emphasis inside a layer (1.22.0)
# The message often singles out part of a layer, not a layer: one end of the values ("the cheapest power"), a few of them ("the top 5 dams")
# or named ones ("the n41 sites", "Quincy"). 1.13.0 could draw that (`highlight:`), but only when the spec spelled it out. `suggest_highlight`
# reads the message against the layer's own values and classes and returns the `highlight:` block that says the same thing; the message check
# offers it (MAP-STORY-006) and `highlight: auto` on the layer applies it.
LOW_WORDS = {"lowest", "fewest", "cheapest", "smallest", "weakest", "slowest", "shortest", "poorest", "thinnest", "sparsest", "minimum"}
HIGH_WORDS = {"highest", "greatest", "largest", "biggest", "busiest", "strongest", "fastest", "tallest", "richest", "densest", "priciest",
              "costliest", "maximum", "peak"}
TOP_N = re.compile(r"\b(?:top|largest|biggest|busiest|highest)\s+(\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|twelve|twenty)\b", re.I)
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "twenty": 20}
NAMED_MAX_UNIQUE = 5000          # a text column with more distinct values than this is not searched for names
NAMED_MAX_COLUMNS = 16


# 2.4.0 (V-03, Day 20 issue 9): class values that are everyday words ("almost none" is not a name). They match the message only when it quotes
# them or capitalises them mid-sentence ("the 'None' class", "where None is drawn").
VALUE_STOP = {"none", "other", "others", "unknown", "yes", "no", "all", "n/a", "na", "null", "nil", "zero", "some", "many", "few", "high", "low",
              "medium", "mid", "partial", "full", "mixed", "missing", "not", "nothing", "everything", "any"}
QUOTES = "\"'\u2018\u2019\u201c\u201d"


def _quoted_or_capitalised(v: str, msg: str) -> bool:
    w = re.escape(v)
    if re.search("[" + QUOTES + "]" + w + "[" + QUOTES + "]", msg, re.I):
        return True
    for m in re.finditer(r"(?<!\w)" + w + r"(?!\w)", msg):
        word = m.group(0)
        before = msg[:m.start()].rstrip()
        if word[:1].isupper() and before and before[-1] not in ".!?:":     # capitalised, and not because it starts a sentence
            return True
    return False



def named_in(message, texts) -> set[str]:
    """The texts the message names: whole words, case ignored, three characters or more (two when one is a digit: n41, I5)."""
    msg = str(message or "")
    out: set[str] = set()
    if not msg.strip():
        return out
    for v in {str(t) for t in texts}:
        v_ = v.strip()
        if len(v_) < 3 and not (len(v_) == 2 and any(ch.isdigit() for ch in v_)):
            continue
        if v_.lower() in STOP:
            continue
        if v_.lower() in VALUE_STOP:
            if _quoted_or_capitalised(v_, msg):
                out.add(v)
            continue
        if re.search(r"(?<!\w)" + re.escape(v_) + r"(?!\w)", msg, re.I):
            out.add(v)
    return out


def _named_values(message: str, l) -> tuple[str, list] | None:
    """(column, values) when the message names some, but not all, of the distinct values of one of the layer's text columns: whole words,
    case ignored, three characters or more (two when one is a digit: n41, I5). The column with the most named values wins."""
    import pandas as pd
    msg = str(message or "")
    if not msg.strip():
        return None
    best = None
    gdf = l.gdf
    cols = [c for c in gdf.columns if c != gdf.geometry.name and not str(c).startswith("_")][:NAMED_MAX_COLUMNS]
    for c in cols:
        s = gdf[c]
        if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
            continue
        uniq = [str(v) for v in pd.unique(s.dropna())]
        if not (1 < len(uniq) <= NAMED_MAX_UNIQUE):
            continue
        named = named_in(msg, uniq)
        hit = [v for v in uniq if v in named]
        if hit and len(hit) < len(uniq) and (best is None or len(hit) > len(best[1])):
            best = (str(c), hit)
    return best


GRADIENT_R2 = 0.6               # 2.3.0: a classed layer whose classes follow a straight trend across the frame this well is a gradient
GRADIENT_MIN_FEATURES = 8


def gradient(l) -> dict | None:
    """2.3.0 (the grid example, 2.2.1): does a classed numeric layer already run from one side of the frame to the other? A plane fitted to
    each feature's class against its centre; R squared at or above GRADIENT_R2 is a gradient, and the ramp itself carries a message about
    the highest or lowest values. -> {"r2", "toward"} (the compass direction the values rise toward) or None."""
    cls = getattr(l, "cls", None)
    if cls is None or getattr(cls, "kind", None) != "numeric" or l.gdf is None or len(l.gdf) < GRADIENT_MIN_FEATURES:
        return None
    asg = np.asarray(cls.assigned, dtype="float64")
    if len(asg) != len(l.gdf):
        return None
    ok = asg >= 0
    if ok.sum() < GRADIENT_MIN_FEATURES or len(set(asg[ok].tolist())) < 3:
        return None
    try:
        c = l.gdf.geometry[ok].representative_point()
        x, y = c.x.to_numpy(dtype="float64"), c.y.to_numpy(dtype="float64")
    except Exception:  # noqa: BLE001 - geometry without centres: not a gradient
        return None
    v = asg[ok]
    A = np.column_stack([np.ones_like(x), x - x.mean(), y - y.mean()])
    coef, *_ = np.linalg.lstsq(A, v, rcond=None)
    ss_res = float(((v - A @ coef) ** 2).sum())
    ss_tot = float(((v - v.mean()) ** 2).sum())
    if ss_tot <= 0:
        return None
    r2 = 1 - ss_res / ss_tot
    if r2 < GRADIENT_R2:
        return None
    ang = (np.degrees(np.arctan2(coef[1], coef[2])) + 360) % 360          # bearing of steepest rise (x east, y north)
    toward = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][int(((ang + 22.5) % 360) // 45)]
    return {"r2": round(r2, 2), "toward": toward}


def suggest_highlight(message: str, l) -> dict | None:
    """The `highlight:` block the message asks of this layer, with the reason in words: {"highlight": {...}, "why": "..."}; None when the
    message singles nothing out in it. Order: named values, then "top N", then one end of a classed number."""
    if l.kind not in ("polygon", "line", "point") or l.role in SKIP_ROLES:
        return None
    msg = str(message or "")
    named = _named_values(msg, l)
    if named:
        col, vals = named
        return {"highlight": {"column": col, "in": vals}, "why": f"the message names {', '.join(vals[:4])}{' and more' if len(vals) > 4 else ''}"}
    vf = l.value_field
    cls = l.cls
    if not vf or vf not in l.gdf.columns or cls is None or getattr(cls, "kind", None) != "numeric":
        return None
    m = TOP_N.search(msg)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1).lower()]
        if 1 <= n < len(l.gdf):
            return {"highlight": {"column": vf, "top": n}, "why": f"the message speaks of the top {n}"}
    raw = {w.lower().strip("'’.,;:") for w in re.split(r"\s+", msg)}
    edges = [float(e) for e in (cls.edges or [])]
    if len(edges) < 3:
        return None
    if gradient(l):                     # 2.3.0: the ramp already says where the highest (or lowest) values are; a highlight would hide it
        return None
    low, high = raw & LOW_WORDS, raw & HIGH_WORDS
    if low and not high:
        return {"highlight": {"column": vf, "max": edges[1], "rest": "fade"}, "why": f"the message speaks of the {sorted(low)[0]} values"}
    if high and not low:
        return {"highlight": {"column": vf, "min": edges[-2], "rest": "fade"}, "why": f"the message speaks of the {sorted(high)[0]} values"}
    return None
