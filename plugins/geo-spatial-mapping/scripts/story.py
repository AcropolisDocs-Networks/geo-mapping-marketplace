#!/usr/bin/env python3
"""story: one story, many formats (2.0.0).

A map makes one claim. A story makes it in steps, and until 2.0.0 each step was written by hand once per format: the web page's chapters,
the video's captions and camera keyframes, the print title and the post. A `story:` block in the MapSpec states the claim and its beats
once; this module turns them into every format the plugin makes:

    story:
      claim: "Cheap hydro on the Mid-Columbia is why Washington's data centers sit in Quincy."     # default: the MapSpec's message
      beats:
        - {role: establish, title: "Power Rates Across the State", show: {view: fit}}              # no words needed: the whole picture
        - {role: evidence, say: "Five dams, the cheapest power in the state.", figure: "3.8 c/kWh",
           show: {highlight: {layer: rates, column: cents_kwh, max: 5}}}                           # the frame follows the highlight
        - {role: turn, say: "Grant PUD's queue is 692 MW; lines reach Quincy with 372.", figure: "692 MW",
           show: {highlight: {layer: hubs, column: name, in: [Quincy]}, layers: [rates, hubs, lines]}}
        - {role: so-what, say: "The next hub goes where the next line goes.", show: {focus: lines}}
      outputs: [web, video, sheet, slides, carousel]

A beat is mostly **a change in what stands out**: its `show` moves the frame (`view`, `focus`), the emphasis (`highlight`) and the layers.
Its line (`say`) is a caption of at most two lines and may be left out; `figure` is its key number. Beat lines may state findings; the
map's title stays subject-led (the map text rule).

    story.py plan  map.yaml                         the beats as resolved (views, highlights, seconds), what each output will build
    story.py build map.yaml --out DIR [--outputs web,sheet,carousel,slides,video]

    web        the page's story chapters (webmap.py reads the beats when the spec has no webmap.story of its own)
    video      <stem>_timeline.yaml for the flythrough: the camera from each beat's view, each caption held for its reading time,
               with <stem>.srt and <stem>.vtt; the video itself is flown by map-run's flythrough pipeline from the page
    sheet      a panel sheet (sheet.py): one numbered panel per beat, six to a sheet, captions under the panels
    carousel   one picture per beat (portrait-social, 1080 x 1350), numbered in the tagline
    slides     one slide per beat in a deck (pptx_adapter)

Every output is built by the same builders a single map uses, so each beat is linted, credited and recorded like any map. `story.json`
beside the outputs lists the claim, the beats and each beat's files, for whoever places them in an article.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

ROLES = ("establish", "evidence", "turn", "so-what", "close")
OUTPUTS = ("web", "video", "sheet", "slides", "carousel")
SAY_MAX = 84                 # two caption lines of 42 characters: what a video frame, a panel caption and a slide subtitle can all carry
TITLE_MAX = 48
BEATS_WARN = 7
BEATS_MAX = 12
READ_CPS = 15.0              # characters a second a caption is read at
HOLD_MIN_S = 2.5             # a beat with no words is still looked at this long
HOLD_PAD_S = 1.2
FLY_S = 2.2                  # the flight from one beat's view to the next
VIEW_PAD = 0.12              # the room left round a beat's features, as a share of their span
SHEET_PANELS = 6
VIEW_KEYS = ("bbox", "center", "zoom", "pitch", "bearing")
WEB_KEYS = ("cards", "navigation", "print_pictures", "print_dpi", "follow_time")


class StoryError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-STORY-020"):
        super().__init__(message)
        self.fid = fid


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- reading the block
def block(spec: dict) -> dict | None:
    s = (spec or {}).get("story")
    return s if isinstance(s, dict) else None


def slug(text: str, n: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:24].strip("-")
    return s or f"beat-{n}"


def beats(spec: dict) -> list[dict]:
    """The beats as written, filled in: n, id, role, title, say, figure, show {view, focus, layers, highlight, time}, hold."""
    out = []
    for i, b in enumerate((block(spec) or {}).get("beats") or []):
        b = b if isinstance(b, dict) else {}
        show = b.get("show") if isinstance(b.get("show"), dict) else {}
        say = str(b.get("say") or "").strip()
        title = str(b.get("title") or "").strip()
        out.append({"n": i + 1, "id": str(b.get("id") or slug(title or say, i + 1)), "role": str(b.get("role") or ("establish" if i == 0 else "evidence")),
                    "title": title, "say": say, "figure": str(b.get("figure")).strip() if b.get("figure") not in (None, "") else None,
                    "show": {"view": show.get("view", "fit"), "focus": show.get("focus"), "layers": show.get("layers"), "highlight": show.get("highlight"),
                             "time": show.get("time"), "marks": show.get("marks") if isinstance(show.get("marks"), list) else None},
                    "hold": float(b["hold"]) if b.get("hold") is not None else None})
    return out


def heading(beat: dict) -> str:
    """A beat's short heading: its title, else its line cut at a clause, else its role."""
    if beat["title"]:
        return beat["title"]
    if beat["say"]:
        head = re.split(r"(?<=[^0-9])[.;:](?:\s|$)", beat["say"], maxsplit=1)[0].strip()
        return head if len(head) <= TITLE_MAX else head[:TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"
    return {"establish": "The Whole Picture", "so-what": "What Follows", "close": "In Short"}.get(beat["role"], f"Beat {beat['n']}")


def hold_s(beat: dict) -> float:
    """How long a beat is held in a video: its own `hold`, else its caption's reading time (never under HOLD_MIN_S)."""
    if beat.get("hold") is not None:
        return max(0.5, float(beat["hold"]))
    chars = len(beat["say"]) + (len(beat["figure"]) if beat.get("figure") else 0)
    return round(max(HOLD_MIN_S, chars / READ_CPS + HOLD_PAD_S), 2)


def outputs(spec: dict) -> list[str]:
    o = (block(spec) or {}).get("outputs")
    o = [str(x) for x in o] if isinstance(o, list) and o else ["web", "sheet"]
    return [x for x in OUTPUTS if x in o]


# ---------------------------------------------------------------------------------------------------------------- checks on the block itself
def counters(spec: dict) -> list[dict]:
    """story.counters as written: numbers that run with the video, read from a layer at each beat (never typed)."""
    out = []
    for c in (block(spec) or {}).get("counters") or []:
        if isinstance(c, dict):
            out.append({"layer": str(c.get("layer")), "text": str(c.get("text") or "{v}"), "column": c.get("column"), "format": str(c.get("format") or ",.0f"),
                        "position": str(c.get("position") or "top-right")})
    return out


def lint(spec: dict) -> list[dict]:
    """MAP-STORY-020 to 023: the story block against its own rules, before any data is read."""
    st = block(spec)
    if st is None:
        return []
    fs: list[dict] = []
    raw = st.get("beats")
    if not isinstance(raw, list) or not raw:
        return [finding("MAP-STORY-020", "BLOCK", "story has no beats; give at least two (a first that shows the whole picture, then what stands out)")]
    if len(raw) > BEATS_MAX:
        fs.append(finding("MAP-STORY-020", "BLOCK", f"the story has {len(raw)} beats; a story holds at most {BEATS_MAX} (a longer one is two stories)", len(raw)))
    elif len(raw) > BEATS_WARN:
        fs.append(finding("MAP-STORY-022", "WARN", f"the story has {len(raw)} beats; past {BEATS_WARN} a reader loses the thread. Merge beats that show the same thing",
                          len(raw)))
    ids = {str(l.get("id")) for l in spec.get("layers") or [] if isinstance(l, dict)}
    bad_out = [x for x in (st.get("outputs") or []) if x not in OUTPUTS]
    if bad_out:
        fs.append(finding("MAP-STORY-020", "BLOCK", f"story.outputs names {', '.join(map(str, bad_out))}; the outputs are {', '.join(OUTPUTS)}", bad_out))
    for i, c in enumerate(st.get("counters") or [], 1):
        if not isinstance(c, dict) or str(c.get("layer")) not in ids:
            fs.append(finding("MAP-STORY-020", "BLOCK", f"story.counters {i} needs layer: one of {', '.join(sorted(ids))}", (c or {}).get("layer") if isinstance(c, dict) else c))
        elif "{v}" not in str(c.get("text") or "{v}"):
            fs.append(finding("MAP-STORY-020", "BLOCK", f"story.counters {i}: its text needs " + "{v} where the number goes ('{v} sites')", c.get("text")))
    bs = beats(spec)
    seen: set = set()
    for b, rb in zip(bs, raw, strict=True):
        where = f"beat {b['n']} ('{heading(b)}')"
        if not isinstance(rb, dict):
            fs.append(finding("MAP-STORY-020", "BLOCK", f"beat {b['n']} is not a mapping (role, say, figure, show)"))
            continue
        if b["id"] in seen:
            fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: the id '{b['id']}' is used by an earlier beat; give one of them an id of its own", b["id"]))
        seen.add(b["id"])
        if b["role"] not in ROLES:
            fs.append(finding("MAP-STORY-023", "INFO", f"{where}: role '{b['role']}' is not one of {', '.join(ROLES)}; it is kept as written", b["role"]))
        if len(b["say"]) > SAY_MAX:
            fs.append(finding("MAP-STORY-021", "WARN", f"{where}: its line is {len(b['say'])} characters; a caption carries {SAY_MAX} (two lines). Say one thing, "
                              "and let the highlight show the rest", {"chars": len(b["say"]), "suggest": f"{SAY_MAX} characters or fewer"}))
        sh = b["show"]
        for lid in (sh["layers"] or []) if isinstance(sh["layers"], list) else []:
            if str(lid) not in ids:
                fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.layers names '{lid}', which the map does not have (its layers are {', '.join(sorted(ids))})", lid))
        if sh["layers"] is not None and not isinstance(sh["layers"], list):
            fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.layers is a list of layer ids"))
        if sh["focus"] is not None and str(sh["focus"]) not in ids:
            fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.focus names '{sh['focus']}', which the map does not have", sh["focus"]))
        hl = sh["highlight"]
        if isinstance(hl, dict):
            if str(hl.get("layer")) not in ids:
                fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.highlight needs layer: one of {', '.join(sorted(ids))}", hl.get("layer")))
            elif not hl.get("auto") and not (hl.get("column") and any(hl.get(k) is not None for k in ("in", "min", "max", "top"))):
                fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.highlight needs column with in: [values], min:, max: or top:, or auto: true"))
        elif hl not in (None, "auto"):
            fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.highlight is auto, or {{layer, column, in | min | max | top}}"))
        v = sh["view"]
        if not (v in (None, "fit") or (isinstance(v, dict) and set(v) <= set(VIEW_KEYS))):
            fs.append(finding("MAP-STORY-020", "BLOCK", f"{where}: show.view is fit, or a mapping of {', '.join(VIEW_KEYS)}"))
    return fs


# ---------------------------------------------------------------------------------------------------------------- resolving against the data
def _wgs_box(plan, b) -> list[float]:
    from pyproj import Transformer
    tr = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    xs = [b[0], b[2], b[0], b[2], (b[0] + b[2]) / 2, (b[0] + b[2]) / 2, b[0], b[2]]
    ys = [b[1], b[1], b[3], b[3], b[1], b[3], (b[1] + b[3]) / 2, (b[1] + b[3]) / 2]
    lon, lat = tr.transform(xs, ys)
    return [round(min(lon), 6), round(min(lat), 6), round(max(lon), 6), round(max(lat), 6)]


def _padded(b, all_b, pad: float = VIEW_PAD) -> tuple:
    import map_plan as mp
    x0, y0, x1, y1 = b
    least = mp.SUBJECT_MIN_SHARE * max(all_b[2] - all_b[0], all_b[3] - all_b[1])
    if least > 0 and max(x1 - x0, y1 - y0) < least:
        cx, cy, h = (x0 + x1) / 2, (y0 + y1) / 2, least / 2
        x0, y0, x1, y1 = min(x0, cx - h), min(y0, cy - h), max(x1, cx + h), max(y1, cy + h)
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    return (x0 - px, y0 - py, x1 + px, y1 + py)


def resolve(spec: dict, plan) -> tuple[list[dict], list[dict]]:
    """The beats against the map's data. -> (beats with bbox (WGS 84), view, highlight (the block a layer takes), layers, seconds; findings).
    `plan` is the map's own plan (map_plan.prepare), read once for every beat."""
    import numpy as np
    import map_emphasis as me
    import map_plan as mp
    import story_lint
    fs: list[dict] = []
    data = story_lint.data_layers(plan)
    by_id = {l.id: l for l in plan.layers}
    all_b = mp._bounds_of(data) if data else tuple(plan.extent)
    out = []
    prev = None
    ctrs = counters(spec)
    for c in ctrs:
        if c["column"] is not None and str(c["column"]) not in by_id[c["layer"]].gdf.columns:
            raise StoryError(f"story.counters: layer '{c['layer']}' has no column '{c['column']}' to add up")
    last = [None] * len(ctrs)
    for b in beats(spec):
        where = f"beat {b['n']} ('{heading(b)}')"
        sh = b["show"]
        hl_layer, hl_block, mask = None, None, None
        hl = sh["highlight"]
        auto = hl == "auto" or (isinstance(hl, dict) and hl.get("auto"))
        if auto:
            cands = [by_id[str(hl["layer"])]] if isinstance(hl, dict) and str(hl.get("layer")) in by_id else data
            text = " ".join(x for x in (b["say"], b["title"]) if x) or (block(spec) or {}).get("claim") or spec.get("message")
            for l in cands:
                sg = me.suggest_highlight(text, l)
                if sg:
                    hl_layer, hl_block = l, dict(sg["highlight"])
                    break
            if hl_block is None:
                fs.append(finding("MAP-STORY-024", "WARN", f"{where}: highlight auto found nothing to single out: its line names no value, top N or end of a "
                                  "layer's values. The beat is drawn like the map", {"beat": b["n"], "suggest": "a line that names what stands out, or a highlight block"}))
        elif isinstance(hl, dict) and str(hl.get("layer")) in by_id:
            hl_layer = by_id[str(hl["layer"])]
            hl_block = {k: v for k, v in hl.items() if k not in ("layer", "auto")}
        if hl_block is not None:
            try:
                m, _what = mp.highlight_mask(hl_layer, hl_block)
                if not m.any():
                    fs.append(finding("MAP-STORY-024", "WARN", f"{where}: its highlight matches no feature of '{hl_layer.id}'; the beat is drawn like the map",
                                      {"beat": b["n"], "suggest": "values the layer holds"}))
                    hl_block = None
                else:
                    mask = np.asarray(m, dtype=bool)
            except mp.PlanError as ex:
                raise StoryError(f"{where}: {ex}") from None
        v = sh["view"]
        extra = {k: v[k] for k in ("pitch", "bearing") if isinstance(v, dict) and v.get(k) is not None}
        if isinstance(v, dict) and (v.get("bbox") or (v.get("center") and v.get("zoom") is not None)):
            import webmap_story as ws
            bbox = ws.view_bbox(v)
            if bbox is None:
                raise StoryError(f"{where}: show.view needs bbox: [w, s, e, n], or center: [lon, lat] with a zoom")
            view = {k: v[k] for k in VIEW_KEYS if v.get(k) is not None}
        else:
            if sh["focus"] is not None and str(sh["focus"]) in by_id:
                fl = by_id[str(sh["focus"])]
                g = fl.gdf if not (hl_layer is fl and mask is not None) else fl.gdf[mask]
                box = _padded(tuple(g.total_bounds), all_b)
            elif mask is not None:
                box = _padded(tuple(hl_layer.gdf[mask].total_bounds), all_b)
            else:
                box = _padded(tuple(all_b), all_b, pad=0.04)
            bbox = _wgs_box(plan, box)
            view = {"bbox": bbox, **extra}
        layers = [str(x) for x in sh["layers"]] if isinstance(sh["layers"], list) else None
        for i, c in enumerate(ctrs):                                  # what the beat shows of the counter's layer: its highlighted features, or all of them
            cl = by_id[c["layer"]]
            if layers is not None and cl.id not in layers and last[i] is not None:
                continue                                              # a beat that hides the layer keeps the number where it was
            g = cl.gdf[mask] if (hl_layer is cl and mask is not None) else cl.gdf
            v_ = float(np.nansum(np.asarray(g[str(c["column"])], dtype=float))) if c["column"] is not None else float(len(g))
            last[i] = round(v_, 6)
        r = {**b, "heading": heading(b), "bbox": [round(float(x), 6) for x in bbox], "view": view, "layers": layers,
             "highlight": ({"layer": hl_layer.id, **hl_block} if hl_block is not None else None),
             "highlighted": int(mask.sum()) if mask is not None else None, "seconds": hold_s(b), "counts": list(last)}
        sig = (tuple(round(x, 4) for x in r["bbox"]), json.dumps(r["highlight"], sort_keys=True, default=str), tuple(layers or ()), str(sh["time"]))
        if prev is not None and sig == prev:
            fs.append(finding("MAP-STORY-025", "WARN", f"{where} shows what the beat before it shows: the same frame, emphasis and layers. A beat is a change in "
                              "what stands out; give it a highlight, a focus or other layers, or fold its line into the beat before",
                              {"beat": b["n"], "suggest": "show: {highlight: ...} or show: {focus: ...}"}))
        prev = sig
        out.append(r)
    return out, fs


# ---------------------------------------------------------------------------------------------------------------- one beat as a map
def beat_edit(spec: dict, beat: dict, *, total: int, template: str | None = None, mode: str = "picture"):
    """An edit of the loaded MapSpec that makes it this beat's map (map_plan.prepare's `spec_edit`): the frame, the emphasis, the layers, and
    the beat's line where a single map carries one. The title stays the map's own (subject-led); the line is the subtitle.
    mode: picture (carousel, slide: the line as the subtitle, the figure as a callout, the count in the tagline) or panel (a sheet's panel:
    the sheet's caption carries the line)."""
    def edit(sp: dict) -> None:
        sp.pop("story", None)
        sp["id"] = f"{sp.get('id') or 'map'}-{beat['n']:02d}"
        sp["extent"] = {"mode": "bbox", "bbox": list(beat["bbox"]), "padding_pct": 2}
        hl = beat.get("highlight")
        for ly in sp.get("layers") or []:
            if not isinstance(ly, dict):
                continue
            if hl is not None:
                ly.pop("highlight", None)                 # the beat says what stands out; the map's own highlight would be a second voice
                if ly.get("id") == hl["layer"]:
                    ly["highlight"] = {k: v for k, v in hl.items() if k != "layer"}
            if beat.get("layers") is not None and ly.get("id") not in beat["layers"] and ly.get("role") != "context":
                ly["draw"] = False
        if beat["show"].get("time") is not None:
            for ly in sp.get("layers") or []:
                if isinstance(ly, dict) and isinstance(ly.get("time"), dict):
                    ly["time"] = {**ly["time"], "at": beat["show"]["time"]}
        if template:
            sp["template"] = template
        if hl is not None or beat.get("layers") is not None:
            sp["message"] = beat["say"] or beat["heading"] or sp.get("message")   # the beat's own point picks what is primary (map_emphasis)
        els = sp.setdefault("elements", {})
        if beat["show"].get("marks"):                     # the beat's own rings, arrows and brackets, in place of the map's
            els["marks"] = {"on": True, "items": copy.deepcopy(beat["show"]["marks"])}
        if mode == "picture":
            ti = dict(els.get("title") or {})
            ti["on"] = ti.get("on", True)
            fig = beat.get("figure")
            own_title = bool(beat["title"]) and beat["title"].rstrip(".") != beat["say"].rstrip(".")
            if beat["say"]:                               # the line is the subtitle; its number leads it unless a callout carries the number
                ti["subtitle"] = beat["say"] if not fig or own_title or fig in beat["say"] else f"{fig}: {beat['say']}"
            elif beat["title"]:
                ti["subtitle"] = beat["title"] if not fig else f"{fig}: {beat['title']}"
            base_tag = str(ti.get("tagline") or "").strip()
            # the count leads: a narrow sheet cuts a long tagline at its end, and "2 of 6" is what a carousel reader needs (Day 16 lost it)
            ti["tagline"] = f"{beat['n']} of {total} · {base_tag}" if base_tag else f"{beat['n']} of {total}"
            els["title"] = ti
            if fig and own_title and beat["say"]:          # a beat with a heading of its own: the number set large on the map, named by the heading
                cx, cy = (beat["bbox"][0] + beat["bbox"][2]) / 2, (beat["bbox"][1] + beat["bbox"][3]) / 2
                els["callouts"] = {"on": True, "items": [{"figure": fig, "text": beat["title"], "at": [round(cx, 6), round(cy, 6)]}]}
            else:
                els.pop("callouts", None)                 # each fact once: the subtitle already says it
        else:
            els.pop("callouts", None)
    return edit


# ---------------------------------------------------------------------------------------------------------------- web chapters
def chapters(resolved: list[dict]) -> list[dict]:
    """The beats as the page's story chapters (webmap.story.chapters): the heading, the line with its figure, the view, the layers, the
    period, and the beat's highlight for the page to draw."""
    out = []
    for r in resolved:
        text = r["say"] if not r.get("figure") else (f"{r['figure']}. {r['say']}" if r["say"] and r["figure"] not in r["say"] else (r["say"] or r["figure"]))
        ch = {"title": r["heading"], "text": text or "", "view": dict(r["view"])}
        if r.get("layers") is not None:
            ch["layers"] = list(r["layers"])
        if r["show"].get("time") is not None:
            ch["time"] = r["show"]["time"]
        if r.get("highlight"):
            ch["highlight"] = dict(r["highlight"])
        if r.get("figure"):
            ch["figure"] = r["figure"]
        if r["show"].get("marks"):                    # 2.3.0: the beat's rings, arrows and brackets on the page (and so in the video flown over it)
            ch["marks"] = [dict(m) for m in r["show"]["marks"] if isinstance(m, dict)]
        out.append(ch)
    return out


def web_story(spec: dict, plan) -> tuple[dict | None, list[dict]]:
    """webmap.story for a spec that has a `story:` block and no chapters of its own. -> ({chapters, ...}, findings)."""
    if block(spec) is None:
        return None, []
    fs = [f for f in lint(spec)]
    if any(f["severity"] == "BLOCK" for f in fs):
        return None, fs
    try:
        res, rfs = resolve(spec, plan)
    except StoryError as ex:
        return None, fs + [finding(ex.fid, "BLOCK", str(ex))]
    out = {"chapters": chapters(res)}
    web = (block(spec) or {}).get("web")
    if isinstance(web, dict):                             # the page's own story options (webmap.story's), for a page whose chapters are the beats
        out.update({k: web[k] for k in WEB_KEYS if k in web})
    claim = (block(spec) or {}).get("claim")
    if claim:
        out["claim"] = str(claim)
    return out, fs + rfs


# ---------------------------------------------------------------------------------------------------------------- the video timeline and captions
def timeline(resolved: list[dict], *, size=(1280, 720), fly_s: float = FLY_S, end_hold: float = 3.0, counters: list[dict] | None = None) -> dict:
    """The video's timeline (timeline.py) from the beats: the camera arrives at each beat's view, holds it for the caption's reading time,
    then flies to the next. Captions start as the camera arrives. No number here is typed by hand: Day 16's 14 keyframes and 11 caption
    times were."""
    import flythrough as fly
    t = 0.0
    keys, lines = [], []
    runs: list[list] = [[] for _ in (counters or [])]
    for i, r in enumerate(resolved):
        v = r["view"]
        if v.get("center") is not None and v.get("zoom") is not None:
            cam = {"center": [float(v["center"][0]), float(v["center"][1])], "zoom": float(v["zoom"])}
        else:
            b = r["bbox"]
            cam = {"center": [round((b[0] + b[2]) / 2, 6), round((b[1] + b[3]) / 2, 6)], "zoom": round(float(fly.zoom_for_bbox(b, size)), 2)}
        cam["pitch"] = float(v.get("pitch") or 0.0)
        cam["bearing"] = float(v.get("bearing") or 0.0)
        if i:
            t += fly_s
        k = {"t": round(t, 2), **cam, "chapter": i}
        if r.get("layers") is not None:
            k["layers"] = list(r["layers"])
        keys.append(k)
        hold = float(r["seconds"])
        if r["say"] or r.get("figure"):
            line = r["say"] if not r.get("figure") or (r["say"] and r["figure"] in r["say"]) else (f"{r['figure']}: {r['say']}" if r["say"] else r["figure"])
            lines.append({"start": round(t, 2), "end": round(t + hold, 2), "line": line})
        for j, run in enumerate(runs):                                # the number arrives with the camera, holds with the beat, and runs during the flight
            val = (r.get("counts") or [None] * len(runs))[j]
            if val is not None:
                run += [[round(t, 2), val], [round(t + hold, 2), val]]
        t += hold
        keys.append({"t": round(t, 2), **cam, "chapter": i, "ease": "hold", **({"layers": list(r["layers"])} if r.get("layers") is not None else {})})
    tl = {"duration": round(t, 2), "story": lines, "keyframes": keys, "end_hold": float(end_hold), "poster": "last", "logo": True}
    made = [{"text": c["text"], "format": c["format"], "position": c["position"], "keys": run} for c, run in zip(counters or [], runs, strict=True) if run]
    if made:
        tl["counters"] = made
    return tl


def _clock(s: float, sep: str) -> str:
    ms = int(round(max(s, 0.0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}{sep}{ms:03d}"


def _two_lines(text: str, width: int = 42) -> str:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "\n".join(lines[:2] if len(lines) <= 2 else [lines[0], " ".join(lines[1:])])


def captions(tl: dict, kind: str = "srt", *, lead_in: float = 0.0) -> str:
    """The timeline's story lines as a caption file: SubRip (.srt) or WebVTT (.vtt). `lead_in` is the seconds a poster frame adds before
    the first drawn frame (timeline.lead_in_s), so the captions stay on their pictures."""
    rows = [r for r in (tl.get("story") or []) if str(r.get("line") or "").strip()]
    out = ["WEBVTT", ""] if kind == "vtt" else []
    dur = tl.get("duration")
    for i, r in enumerate(rows):
        start = float(r["start"]) + lead_in
        end = r.get("end")
        if end is None or (isinstance(end, float) and math.isinf(end)):
            end = rows[i + 1]["start"] if i + 1 < len(rows) else (dur if dur else float(r["start"]) + HOLD_MIN_S)
        end = float(end) + lead_in
        sep = "." if kind == "vtt" else ","
        if kind != "vtt":
            out.append(str(i + 1))
        out += [f"{_clock(start, sep)} --> {_clock(end, sep)}", _two_lines(r["line"]), ""]
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------- building the outputs
def _prepare(spec_path: Path):
    import map_plan as mp
    spec = gc.load_mapspec(spec_path)
    if block(spec) is None:
        raise StoryError("the MapSpec has no story: block (claim, beats); the map-story skill writes one")
    fs = lint(spec)
    if any(f["severity"] == "BLOCK" for f in fs):
        return spec, None, [], fs
    plan = mp.prepare(str(spec_path), direct_labels=False)
    res, rfs = resolve(spec, plan)
    return spec, plan, res, fs + rfs


def plan_story(spec_path) -> dict:
    """What a story will build, before anything is built: the beats as resolved and the cost of each output."""
    spec_path = Path(spec_path)
    spec, _plan, res, fs = _prepare(spec_path)
    outs = outputs(spec)
    n = len(res)
    cost = {"web": {"builds": 1 + n, "what": f"one page with {n} chapters and a print picture of each"},
            "video": {"builds": 0, "what": "a timeline and caption files; the video is flown from the page by map-run's flythrough pipeline"},
            "sheet": {"builds": n, "what": f"{math.ceil(n / SHEET_PANELS) if n else 0} panel sheet(s) of up to {SHEET_PANELS} beats"},
            "carousel": {"builds": n, "what": f"{n} pictures, 1080 x 1350"},
            "slides": {"builds": n, "what": f"one deck of {n} slides"}}
    seconds = round(sum(r["seconds"] for r in res) + FLY_S * max(n - 1, 0), 1)
    return {"claim": (block(spec) or {}).get("claim") or spec.get("message"), "beats": [{k: r[k] for k in ("n", "id", "role", "heading", "say", "figure", "bbox", "highlight",
                                                                                                         "highlighted", "layers", "seconds")} for r in res],
            "outputs": outs, "cost": {k: cost[k] for k in outs}, "map_builds": sum(cost[k]["builds"] for k in outs), "video_seconds": seconds,
            "findings": fs, "blocked": any(f["severity"] == "BLOCK" for f in fs)}


def _sheet_template(spec: dict, n: int = 4) -> str:
    """The sheet a story's panels go on: the map's own landscape print sheet for up to four beats; five or six need the next size up, and a
    slide or a social sheet is too small for panels with captions (Day 16: six panels on a slide had no room for the key or the north arrow)."""
    t = str(spec.get("template") or "letter-landscape")
    if t in ("tabloid-landscape", "a3-landscape", "arch-d"):
        return t
    if t in ("letter-landscape", "a4-landscape"):
        return t if n <= 4 else {"letter-landscape": "tabloid-landscape", "a4-landscape": "a3-landscape"}[t]
    return "tabloid-landscape"


def sheet_specs(spec_path: Path, spec: dict, resolved: list[dict]) -> list[dict]:
    """The story as panel sheets (sheet.py): six beats to a sheet, each panel the map with the beat's frame, emphasis and layers, its
    caption the beat's number and line."""
    el_title = (spec.get("elements") or {}).get("title") or {}
    sheets = []
    groups = [resolved[i:i + SHEET_PANELS] for i in range(0, len(resolved), SHEET_PANELS)]
    for gi, grp in enumerate(groups):
        panels = []
        for r in grp:
            over: dict = {"extent": {"_replace": True, "mode": "bbox", "bbox": list(r["bbox"]), "padding_pct": 2}, "story": {"_replace": True, "_drop": True}}
            layers = []
            for ly in spec.get("layers") or []:
                if not isinstance(ly, dict):
                    continue
                o: dict = {"id": ly.get("id")}
                if r.get("highlight") is not None:
                    o["highlight"] = ({"_replace": True, **{k: v for k, v in r["highlight"].items() if k != "layer"}}
                                      if ly.get("id") == r["highlight"]["layer"] else {"_replace": True, "_drop": True})
                if r.get("layers") is not None and ly.get("id") not in r["layers"] and ly.get("role") != "context":
                    o["draw"] = False
                if len(o) > 1:
                    layers.append(o)
            if layers:
                over["layers"] = layers
            if r.get("highlight") is not None or r.get("layers") is not None:
                over["message"] = r["say"] or r["heading"]
            over["elements"] = {"callouts": {"_replace": True, "on": False}}
            if r["show"].get("marks"):                    # 2.0.1: the beat's own marks on its panel too, as on its carousel picture and its slide
                over["elements"]["marks"] = {"_replace": True, "on": True, "items": copy.deepcopy(r["show"]["marks"])}
            cap = f"{r['n']}  {r['say'] or r['heading']}"
            panels.append({"base": str(spec_path), "override": over, "caption": cap})
        n = len(grp)
        cols = n if n <= 3 else (2 if n == 4 else 3)
        sid = f"{spec.get('id') or 'map'}-story" + (f"-{gi + 1}" if len(groups) > 1 else "")
        claim = (block(spec) or {}).get("claim")
        sheets.append({"sheet_version": 1, "id": sid, "template": _sheet_template(spec, n), **({"brand": spec["brand"]} if spec.get("brand") else {}),
                       "title": {"text": str(el_title.get("text") or spec.get("message") or "Story"),
                                 **({"subtitle": str(claim)} if claim and len(str(claim)) <= 110 else ({"subtitle": el_title["subtitle"]} if el_title.get("subtitle") else {}))},
                       "arrangement": {"columns": cols, "rows": math.ceil(n / cols), "gap_mm": 5, "caption": "below"},
                       "shared": {"extent": "each", "scale": "each", "legend": "shared", "locator": "none", "north_arrow": "one", "scale_bar": "none", "sources": "one"},
                       "panels": panels})
    return sheets


def slides(spec_path: Path, spec: dict, resolved: list[dict], out_dir: Path, *, brand: str | None = None) -> tuple[str | None, list[dict]]:
    """One deck, one slide per beat (pptx_adapter: the map as a picture, the title, the beat's line and the legend as editable shapes).
    Each beat is written as a MapSpec of its own with full paths, and each slide is added to the deck the one before it made.
    -> (the deck, findings)."""
    import shutil
    import atlas
    import pptx_adapter as pa
    import yaml
    out_dir = Path(out_dir)
    work = out_dir / "beats"
    work.mkdir(parents=True, exist_ok=True)
    fs: list[dict] = []
    deck: Path | None = None
    for r in resolved:
        sp = copy.deepcopy(spec)
        atlas._abs_paths(sp, spec_path.parent)
        beat_edit(spec, r, total=len(resolved), template="16x9-slide")(sp)
        (sp.setdefault("elements", {})).pop("callouts", None)        # a slide's shapes are the map, its title, its line and its key
        bp = work / f"beat-{r['n']:02d}.yaml"
        bp.write_text(yaml.safe_dump(sp, sort_keys=False, allow_unicode=True), encoding="utf-8")
        nxt = out_dir / f".deck-{r['n']:02d}.pptx"
        try:
            res = pa.write_slide(str(bp), str(nxt), deck=str(deck) if deck else None, template="16x9-slide", brand=brand, overwrite=True)
        except pa.AdapterError as ex:
            fs.append(finding(getattr(ex, "fid", None) or "MAP-PPT-001", "BLOCK", f"beat {r['n']} ('{r['heading']}'): {ex}", {"beat": r["n"]}))
            break
        fs += [{**f, "beat": r["n"]} for f in res["findings"] if f["severity"] in ("BLOCK", "WARN")]
        if deck is not None:
            deck.unlink(missing_ok=True)
        deck = nxt
    if deck is None:
        return None, fs
    final = out_dir / f"{spec.get('id') or 'map'}_story.pptx"
    shutil.move(str(deck), str(final))
    return str(final), fs


def build(spec_path, out_dir, only: list[str] | None = None, *, brand: str | None = None, strict: bool = False) -> dict:
    """Build the story's outputs. -> {claim, beats, outputs: {name: {files, findings, ...}}, findings, blocked, story_json}."""
    import build_map
    import yaml
    spec_path = Path(spec_path).resolve()
    out = Path(out_dir)
    spec, _plan, res, fs = _prepare(spec_path)
    result: dict = {"claim": (block(spec) or {}).get("claim") or spec.get("message"), "beats": [], "outputs": {}, "findings": list(fs), "blocked": False}
    if any(f["severity"] == "BLOCK" for f in fs):
        result["blocked"] = True
        return result
    outs = [o for o in (only or outputs(spec)) if o in OUTPUTS]
    out.mkdir(parents=True, exist_ok=True)
    n = len(res)
    per_beat: dict[int, dict] = {r["n"]: {} for r in res}
    stem = str(spec.get("id") or "map")

    def note(name: str, files: list, findings: list, **more):
        result["outputs"][name] = {"files": [str(f) for f in files], "findings": findings, **more}
        result["findings"] += [f for f in findings if f not in result["findings"]]

    if "web" in outs:
        r = build_map.build(str(spec_path), str(out / "web"), ["html"], brand=brand, strict=strict)
        note("web", r["files"], r["findings"], chapters=((r.get("web") or {}).get("story") or {}).get("chapters"))
    if "video" in outs or "web" in outs:
        tl = timeline(res, counters=counters(spec))
        vdir = out / "video"
        vdir.mkdir(parents=True, exist_ok=True)
        tp = vdir / f"{stem}_timeline.yaml"
        tp.write_text("# written by story.py from the MapSpec's story: block; fly it with\n"
                      f"#   map_export.py --flythrough <the page>.html --keyframes {tp.name} --timeline {tp.name} --out <folder>\n"
                      + yaml.safe_dump(json.loads(json.dumps(tl)), sort_keys=False, allow_unicode=True), encoding="utf-8")   # a copy: no YAML anchors in a file a person reads
        import timeline as tlm
        lead = tlm.lead_in_s(tlm.load(tl), 25.0)
        files = [tp]
        for kind in ("srt", "vtt"):
            cp = vdir / f"{stem}.{kind}"
            cp.write_text(captions(tl, kind, lead_in=lead), encoding="utf-8")
            files.append(cp)
        if "video" in outs:
            note("video", files, [], seconds=tl["duration"], keyframes=len(tl["keyframes"]), captions=len(tl["story"]),
                 next="map-run flythrough: the page, with --keyframes and --timeline on the timeline file")
    if "carousel" in outs:
        files, cfs = [], []
        for r in res:
            br = build_map.build(str(spec_path), str(out / "carousel"), ["png"], brand=brand, template="portrait-social", strict=strict,
                                 spec_edit=beat_edit(spec, r, total=n, template="portrait-social"))
            png = [f for f in br["files"] if f.endswith(".png")]
            files += png
            per_beat[r["n"]]["carousel"] = png[0] if png else None
            cfs += [{**f, "beat": r["n"]} for f in br["findings"] if f["severity"] in ("BLOCK", "WARN")]
        note("carousel", files, cfs)
    if "sheet" in outs:
        files, sfs = [], []
        (out / "sheet").mkdir(parents=True, exist_ok=True)
        for sh in sheet_specs(spec_path, spec, res):
            sp = out / "sheet" / f"{sh['id']}.sheet.yaml"              # kept: the sheet can be rebuilt or edited by hand (its panels name the MapSpec by its full path)
            sp.write_text(yaml.safe_dump(sh, sort_keys=False, allow_unicode=True), encoding="utf-8")
            sr = build_map.build(str(sp), str(out / "sheet"), ["svg", "pdf", "png"], brand=brand, strict=strict)
            files += sr["files"]
            sfs += [f for f in sr["findings"] if f["severity"] in ("BLOCK", "WARN")]
        note("sheet", files, sfs)
    if "slides" in outs:
        deck, dfs = slides(spec_path, spec, res, out / "slides", brand=brand)
        for i, r in enumerate(res):
            per_beat[r["n"]]["slide"] = i + 1 if deck else None
        note("slides", [deck] if deck else [], dfs)
    for r in res:
        result["beats"].append({**{k: r[k] for k in ("n", "id", "role", "heading", "say", "figure", "bbox", "highlight", "highlighted", "seconds")}, "files": per_beat[r["n"]]})
    result["blocked"] = any(f["severity"] == "BLOCK" for f in result["findings"])
    sj = out / "story.json"
    sj.write_text(json.dumps({"story_version": 1, "map": spec.get("id"), "title": ((spec.get("elements") or {}).get("title") or {}).get("text"),
                              "claim": result["claim"], "beats": result["beats"],
                              "outputs": {k: v["files"] for k, v in result["outputs"].items()}}, indent=1, default=str) + "\n", encoding="utf-8")
    result["story_json"] = str(sj)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["plan", "build"])
    ap.add_argument("spec")
    ap.add_argument("--out")
    ap.add_argument("--outputs", help=f"a comma list of {', '.join(OUTPUTS)} (default: the story's own outputs)")
    ap.add_argument("--brand")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    import map_plan as mp
    try:
        if a.op == "plan":
            res = plan_story(a.spec)
        else:
            if not a.out:
                print("story: build needs --out DIR", file=sys.stderr)
                return gc.EXIT_USAGE
            only = [x.strip() for x in a.outputs.split(",") if x.strip()] if a.outputs else None
            bad = [x for x in (only or []) if x not in OUTPUTS]
            if bad:
                print(f"story: unknown output(s) {', '.join(bad)}; the outputs are {', '.join(OUTPUTS)}", file=sys.stderr)
                return gc.EXIT_USAGE
            res = build(a.spec, a.out, only, brand=a.brand, strict=a.strict)
    except StoryError as ex:
        print(f"story: {ex.fid} {ex}", file=sys.stderr)
        return gc.EXIT_LINT
    except mp.PlanError as ex:
        print(f"story: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(res, indent=2, default=str))
    return gc.EXIT_LINT if res.get("blocked") else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
