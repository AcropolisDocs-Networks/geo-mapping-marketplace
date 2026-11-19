#!/usr/bin/env python3
"""story_lint: does the display carry the map's message by itself? (1.22.0)

A map should tell its story through what stands out, where the frame sits and what is labelled, before a reader has decoded the legend or
read a callout. The builder has known what a map is about since 1.13.0 (the `message` picks the primary layers). These checks measure
whether the drawing agrees with that, on every build, from the plan and the layout report (nothing is rendered again):

    MAP-STORY-001  the subject spans less than a quarter of the frame
    MAP-STORY-002  the subject carries less of the data's visual weight than a layer that only supports it
    MAP-STORY-003  the legend takes more of the map than a key should (width or area)
    MAP-STORY-004  two keys of one categorical legend look alike as drawn
    MAP-STORY-005  the map is explained in words (callouts) more than it is shown
    MAP-STORY-006  the message singles out part of a layer that the drawing does not single out (with the highlight: line that would)
    MAP-STORY-007  a layer was named on the map in place of its key (legend.direct) and a name found no room

None blocks, and none moves a map element: each says what to change in the MapSpec. They are called out in chat beside the MAP-TEXT
findings (`summary_lines`). The measures themselves go into the build result (`story.measures`) with the mute preview, a second picture
with the title, the legend's words and the callouts taken off, for the one test no script can make: what does the map say with no words?
"""
from __future__ import annotations

import math
import re

import colorlib as cl

SUBJECT_SPAN_MIN = 0.25          # 001: the subject's longer span as a share of the frame
POINT_SPAN = 0.02                # 001: under this the subject is a point on the sheet, not an area that could fill it
SUBJECT_WEIGHT_MIN = 0.40        # 002: the subject's share of the data's ink, when a supporting layer outweighs it
LEGEND_WIDTH_MAX = 0.22          # 003: the legend's width as a share of the map's
LEGEND_AREA_MAX = 0.12           # 003: its area as a share of the map's
KEY_DELTA_E = 10.0               # 004: two categorical keys closer than this, as drawn over the land, read as one
CALLOUTS_MAX = 2                 # 005
CALLOUT_WORDS_MAX = 30           # 005
SKIP_ROLES = ("context", "contour", "hillshade", "raster")


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def data_layers(plan) -> list:
    return [l for l in plan.layers if l.role not in SKIP_ROLES and l.kind != "raster" and getattr(l, "context_kind", None) is None
            and (l.spec or {}).get("draw", True) is not False and len(l.gdf)]


def subject(plan) -> tuple[list, bool]:
    """([(layer, mask or None)], singled_out): what the map is about. The layers the message names or that are marked primary, each cut to
    its highlighted features; with nothing singled out, every data layer (and singled_out False)."""
    import map_emphasis
    import numpy as np
    layers = data_layers(plan)
    picked = map_emphasis.subject_layers(plan.spec, layers)
    out = []
    for l in (picked or layers):
        m = getattr(l, "highlight", None)
        m = np.asarray(m, dtype=bool) if m is not None and len(m) == len(l.gdf) else None
        out.append((l, m if m is not None and m.any() and not m.all() else None))
    singled = bool(picked) or any(m is not None for _, m in out)
    if not picked and singled:                             # no layer named, but a highlight says which features: those are the subject
        out = [(l, m) for l, m in out if m is not None]
    return out, singled


def frame_share(plan, report: dict, subj: list) -> dict | None:
    """How much of the frame the subject spans: {w, h, span, area} as shares of the visible window."""
    import numpy as np
    win = (report.get("map_window") or {}).get("extent")
    if not win or not subj:
        return None
    x0, y0, x1, y1 = (float(v) for v in win)
    bs = []
    for l, m in subj:
        g = l.gdf if m is None else l.gdf[m]
        if len(g):
            b = g.total_bounds
            if all(math.isfinite(float(v)) for v in b):
                bs.append(b)
    if not bs or x1 <= x0 or y1 <= y0:
        return None
    a = np.array(bs)
    bx0, by0, bx1, by1 = max(float(a[:, 0].min()), x0), max(float(a[:, 1].min()), y0), min(float(a[:, 2].max()), x1), min(float(a[:, 3].max()), y1)
    w, h = max(bx1 - bx0, 0.0) / (x1 - x0), max(by1 - by0, 0.0) / (y1 - y0)
    return {"w": round(w, 3), "h": round(h, 3), "span": round(max(w, h), 3), "area": round(w * h, 3)}


def weights(plan, subj: list) -> dict | None:
    """Each data layer's ink in the frame (look_lint.weight: area on the sheet times contrast with the land times opacity), the subject's
    share of it, and the heaviest layer that is not the subject. A highlighted layer counts its highlighted part at full strength and the
    rest at the strength it is drawn. None when a layer cannot be weighed, or a data raster fills the frame."""
    import look_lint
    import numpy as np
    if any(l.kind == "raster" and l.role == "raster" for l in plan.layers):
        return None
    k = look_lint._mm_per_unit(plan)
    layers = data_layers(plan)
    if k <= 0 or not layers:
        return None
    subj_ids = {l.id: m for l, m in subj}
    per, fig = {}, 0.0
    try:
        for l in layers:
            w = float(look_lint.weight(l, plan, k))
            m = getattr(l, "highlight", None)
            if m is not None and len(m) == len(l.gdf) and np.asarray(m, dtype=bool).any():
                mm = np.asarray(m, dtype=bool)
                size = l.gdf.geometry.area.to_numpy() if l.kind == "polygon" else (l.gdf.geometry.length.to_numpy() if l.kind == "line" else np.ones(len(l.gdf)))
                tot = float(np.nansum(size)) or 1.0
                frac = float(np.nansum(size[mm])) / tot
                rest = getattr(l, "highlight_rest", None) or {"dim": True, "opacity": 0.3}
                rest_k = float(rest.get("opacity", 0.3)) * (0.6 if rest.get("dim") else 1.0)
                hl_w, rest_w = w * frac, w * (1 - frac) * rest_k
                per[l.id] = hl_w + rest_w
                if l.id in subj_ids:
                    fig += hl_w if subj_ids[l.id] is not None else hl_w + rest_w
            else:
                per[l.id] = w
                if l.id in subj_ids:
                    fig += w
    except Exception:  # noqa: BLE001 - a layer the weighing cannot read: no measure rather than a wrong one
        return None
    total = sum(per.values())
    if total <= 0:
        return None
    others = {i: w for i, w in per.items() if i not in subj_ids}
    top = max(others, key=others.get) if others else None
    return {"subject_share": round(fig / total, 3), "layers": {i: round(w / total, 3) for i, w in per.items()},
            "heaviest_other": top, "heaviest_other_share": round(others[top] / total, 3) if top else None}


def legend_share(report: dict) -> dict | None:
    lg = ((report.get("overlays") or {}).get("legend") or {}).get("rect_mm")
    mr = (report.get("map_window") or {}).get("rect_mm")
    if not lg or not mr:
        return None
    lw, lh = float(lg[2]) - float(lg[0]), float(lg[3]) - float(lg[1])
    mw, mh = float(mr[2]) - float(mr[0]), float(mr[3]) - float(mr[1])
    if min(lw, lh, mw, mh) <= 0:
        return None
    return {"width": round(lw / mw, 3), "height": round(lh / mh, 3), "area": round(lw * lh / (mw * mh), 3)}


def lookalike_keys(plan) -> list[dict]:
    """Pairs of categories in one layer's key that the reader cannot tell apart as drawn: the class colour mixed with the land at the
    strength the layer is drawn (a supporting layer at 55 percent loses most of the distance between two light colours)."""
    out = []
    land = plan.tokens.get("land")
    for l in data_layers(plan):
        cls = l.cls
        if cls is None or getattr(cls, "kind", None) != "categorical" or not l.legend_block or len(l.colors or []) < 2:
            continue
        op = float(getattr(l, "group_opacity", 1.0) or 1.0) * (float(l.fill_opacity or 1.0) if l.kind == "polygon" else 1.0)
        try:
            drawn = [cl.mix(land, c, min(max(op, 0.0), 1.0)) if op < 1 else c for c in l.colors]
            pairs = []
            for i in range(len(drawn)):
                for j in range(i + 1, len(drawn)):
                    d = cl.delta_e(drawn[i], drawn[j])
                    if d < KEY_DELTA_E:
                        pairs.append((cls.labels[i], cls.labels[j], round(d, 1)))
        except Exception:  # noqa: BLE001 - a colour that is not a hex value (a pattern, a token left unresolved): not judged
            continue
        if pairs:
            out.append({"layer": l.id, "pairs": pairs, "opacity": round(op, 2)})
    return out


def words_on_map(plan) -> dict:
    els = plan.spec.get("elements") or {}
    co = els.get("callouts") or {}
    items = [i for i in (co.get("items") or []) if isinstance(i, dict)] if co.get("on") else []
    words = sum(len(re.findall(r"\w+", f"{i.get('figure') or ''} {i.get('text') or ''}")) for i in items)
    notes = sum(len(re.findall(r"\w+", str((l.legend_block or {}).get("note") or ""))) for l in plan.layers if l.legend_block)
    keys = sum(len((l.legend_block or {}).get("items") or []) for l in plan.layers if l.legend_block)
    return {"callouts": len(items), "callout_words": words, "legend_note_words": notes, "legend_keys": keys}


def suggestions(plan, subj: list) -> list[dict]:
    """The highlight: blocks the message asks for and the spec does not have (map_emphasis.suggest_highlight), one per layer."""
    import map_emphasis
    out = []
    msg = plan.spec.get("message")
    for l, _m in subj:
        if (l.spec or {}).get("highlight") is not None:
            continue
        try:
            s = map_emphasis.suggest_highlight(msg, l)
        except Exception:  # noqa: BLE001 - a column the reader cannot search: no suggestion
            s = None
        if s:
            out.append({"layer": l.id, **s})
    return out


def _yaml(d: dict) -> str:
    def v(x):
        if isinstance(x, list):
            return "[" + ", ".join(v(i) for i in x) + "]"
        if isinstance(x, float):
            return f"{x:g}"
        return str(x)
    return "{" + ", ".join(f"{k}: {v(x)}" for k, x in d.items()) + "}"


def lint(plan, report: dict) -> tuple[list[dict], dict]:
    """(findings, measures) for one built map."""
    fs: list[dict] = []
    subj, singled = subject(plan)
    names = ", ".join(f"'{l.id}'" for l, _ in subj[:4])
    measures: dict = {"message": plan.spec.get("message"), "subject": [l.id for l, _ in subj], "singled_out": singled}
    mode = (plan.spec.get("extent") or {}).get("mode", "fit_layers")
    share = frame_share(plan, report, subj)
    measures["frame"] = share
    # a subject with no size of its own (one site) cannot span a frame: once the frame is fitted to it (fit_message gives it a window with
    # its surroundings), the framing has done what it can
    framed_point = mode == "fit_message" and share is not None and share["span"] < POINT_SPAN
    if share and singled and share["span"] < SUBJECT_SPAN_MIN and not framed_point:
        fix = ("extent: {mode: fit_message} frames what the message names and keeps the rest as surroundings" if mode != "fit_message"
               else "give the extent a smaller padding_pct, or a bbox round the subject")
        fs.append(finding("MAP-STORY-001", "WARN", f"what the map is about ({names}) spans {share['span']:.0%} of the frame, so the reader sees the "
                          f"surroundings first; {fix}", {**share, "suggest": "extent: {mode: fit_message}" if mode != "fit_message" else "a smaller extent.padding_pct"}))
    w = weights(plan, subj)
    measures["weight"] = w
    if w and singled and w["heaviest_other"] and w["subject_share"] < SUBJECT_WEIGHT_MIN and w["heaviest_other_share"] > w["subject_share"]:
        o = w["heaviest_other"]
        ol = next((l for l in plan.layers if l.id == o), None)
        how = ("draw it as an outline (symbol: {fill: none})" if ol is not None and ol.kind == "polygon" else
               "draw it thinner (symbol: {width: ...})" if ol is not None and ol.kind == "line" else "draw it smaller (symbol: {size: ...})")
        fs.append(finding("MAP-STORY-002", "WARN", f"what the map is about ({names}) carries {w['subject_share']:.0%} of the data's visual weight; layer "
                          f"'{o}' carries {w['heaviest_other_share']:.0%} and draws the eye first. Give '{o}' emphasis: background (30 percent), {how}, "
                          "or strengthen the subject",
                          {**w, "suggest": f"emphasis: background on layer '{o}'"}))
    ls = legend_share(report)
    measures["legend"] = ls
    if ls and (ls["width"] > LEGEND_WIDTH_MAX or ls["area"] > LEGEND_AREA_MAX):
        fs.append(finding("MAP-STORY-003", "WARN", f"the legend takes {ls['width']:.0%} of the map's width and {ls['area']:.0%} of its area: the reader "
                          "decodes before seeing. Name lines and categories on the map (elements.legend: {direct: auto}), key a layer as one entry "
                          "(legend: {single: true}), or drop keys the map does not need (legend: {include: false})",
                          {**ls, "suggest": "elements.legend: {direct: auto}"}))
    alike = lookalike_keys(plan)
    measures["lookalike_keys"] = alike
    for a in alike:
        p = a["pairs"][0]
        more = f" and {len(a['pairs']) - 1} more pair(s)" if len(a["pairs"]) > 1 else ""
        why = f" (the layer is drawn at {a['opacity']:.0%}, which washes its colours out)" if a["opacity"] < 0.99 else ""
        fs.append(finding("MAP-STORY-004", "WARN", f"layer '{a['layer']}': the keys '{p[0]}' and '{p[1]}'{more} look alike as drawn{why}; a reader cannot "
                          "match them to the map. Group categories, give the layer emphasis: primary, or name the categories on the map",
                          {**a, "suggest": "fewer categories, or emphasis: primary"}))
    wm = words_on_map(plan)
    measures["words"] = wm
    if wm["callouts"] > CALLOUTS_MAX or wm["callout_words"] > CALLOUT_WORDS_MAX:
        fs.append(finding("MAP-STORY-005", "INFO", f"{wm['callouts']} callout(s) carry {wm['callout_words']} words: the map is being explained more than "
                          "shown. Keep the one callout the message needs and let highlight:, emphasis: and the frame say the rest",
                          {**wm, "suggest": "one callout; highlight: for the rest"}))
    sg = suggestions(plan, subj)
    measures["suggested_highlights"] = sg
    for s in sg:
        fs.append(finding("MAP-STORY-006", "INFO", f"layer '{s['layer']}': {s['why']}, but the layer is drawn at one strength throughout; "
                          f"highlight: {_yaml(s['highlight'])} holds the rest back (or highlight: auto)",
                          {"layer": s["layer"], "highlight": s["highlight"], "suggest": f"highlight: {_yaml(s['highlight'])}"}))
    direct = ((report.get("labels") or {}).get("direct") or {})
    measures["direct_labels"] = {k: {"named": len(v.get("asked") or []) - len(v.get("missing") or []), "missing": v.get("missing") or []} for k, v in direct.items()}
    for lid, v in direct.items():
        if v.get("missing"):
            fs.append(finding("MAP-STORY-007", "WARN", f"layer '{lid}' was named on the map in place of its key, but {', '.join(v['missing'][:4])} found no "
                              f"room, so the map shows it without saying what it is; legend: {{direct: false}} on the layer puts its key back",
                              {"layer": lid, "missing": v["missing"], "suggest": "legend: {direct: false}"}))
    return fs, measures


def summary_lines(findings: list[dict]) -> list[str]:
    """The chat callout: every MAP-STORY finding, one line each. Never blocking."""
    picked = [f for f in findings if f["id"].startswith("MAP-STORY")]
    if not picked:
        return []
    out = [f"Message check: {len(picked)} finding{'s' if len(picked) != 1 else ''} (not blocking)"]
    for f in picked:
        out.append(f"- {f['id']} {f['message']}")
    return out
