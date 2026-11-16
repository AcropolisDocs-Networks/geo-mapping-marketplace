#!/usr/bin/env python3
"""timeline: one video timeline for the flythrough and the map videos (1.16.0, Days 13 to 15 of the #30DayMapChallenge).

    map_export.py --flythrough PAGE.html --timeline T.yaml ...     camera keyframes, story line, counters, poster frame, end hold, logo
    map_export.py --events MAP.yaml --timeline T.yaml ...          the same over a print map, plus per-feature events on a polygon layer
    map_export.py --story MAP.yaml --timeline T.yaml ...           a print-map story video with no point events (events_anim, no --layer)

Days 13, 14 and 15 each built their video outside the plugin (a pycairo overlay, an HTML render(t) captured in Playwright, a free-camera
driver). This file is the timeline they needed, read by flythrough.py and events_anim.py alike. Every function here is a pure function of the
time t in seconds, so a frame can be redrawn alone (--resume) and the tests need no browser.

A timeline (YAML or JSON; every key optional):

  duration: 36                       the run in seconds (the CLI's --duration when absent)
  story:                             a running line along the bottom, faded in and out over fade_s (0.45 s)
    - {start: 0,   line: "It starts in the seats."}
    - {start: 3.2, end: 6.4, line: "67,000 fans in one bowl."}         end: the next line's start, else the end of the run
  counters:                          numbers that run with the video ("{v}" the value, format a Python format spec)
    - {text: "AD {v}", keys: [[0, 117], [26, 1453]], format: ".0f", step: false, position: top-right}
  keyframes:                         the camera (flythrough): a free camera or the page's own view terms, eased segment by segment
    - {t: 0,  camera: [-122.3316, 47.5952, 8], target: [-122.3317, 47.5960, 30], fov: 50}      lon, lat, height above the ground (m)
    - {t: 6,  center: [-122.33, 47.59], zoom: 16.5, pitch: 70, bearing: 20, ease: linear}       or the page's terms
    - {t: 10, ..., layers: [stadium, bowl], opacity: {roof: 0.25}}       the layers shown; opacity a factor of the layer's own (its slider)
  features:                          per-feature state on a polygon layer of the map (events_anim)
    layer: pieces
    id_field: piece                  the column that names each feature (default: the row number)
    label_field: name                labels drawn upright, at one size, with a halo, wherever the feature moves
    events:
      - {t: 4.0, ids: [12, 13], action: lift, dur: 0.8}                 lift: up 3 mm with a shadow
      - {t: 5.0, where: "lost<=395", action: drift, dur: 3, dx_mm: 40, dy_mm: -10, rotate: 25}
      - {t: 9.0, ids: [12], action: settle, dur: 1.2}                   back where it started
      - {t: 2.0, ids: [7], action: recolor, colour: status.danger}     a brand token path or a hex colour
      - {t: 3.0, ids: [8], action: fade, dur: 1}  |  show  |  hide
  end_hold: 5                        the last frame held this long
  poster: last                       first | last | none: what the first frame shows (X has no autoplay: its thumbnail is the first frame)
  poster_hold: 1.2                   seconds the poster frame is held before the video
  poster_dissolve: 0.6               seconds it dissolves into the first frame
  logo: true                         the brand logo bottom left, above the credits (the print map's corner)

The camera is never put under min_eye_m (6 m) above the ground: MapLibre clips the ground nearer than its near plane, so a 3 m street-level
eye showed the page background (Day 15 issue 12). A finding names each keyframe raised.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

FADE_S = 0.45
MIN_EYE_M = 6.0
MAX_PITCH = 115.0
EASES = ("smooth", "linear", "in", "out", "hold")
ACTIONS = ("show", "hide", "fade", "lift", "drift", "settle", "recolor")
POSITIONS = ("top-left", "top-right", "bottom-right", "bottom-left")
LIFT_MM = 3.0


class TimelineError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------------------- loading
def load(src, *, duration: float | None = None) -> dict:
    """A timeline from a path, a YAML/JSON string or a dict, checked and filled in. -> {duration, story, counters, keyframes, features,
    end_hold, poster, poster_hold, poster_dissolve, logo, findings}. Raises TimelineError naming the first problem."""
    if src is None:
        d: dict = {}
    elif isinstance(src, dict):
        d = dict(src)
    else:
        p = Path(str(src))
        text = p.read_text(encoding="utf-8") if (len(str(src)) < 1024 and p.is_file()) else str(src)
        try:
            import yaml
            d = yaml.safe_load(text) or {}
        except Exception as ex:  # noqa: BLE001
            raise TimelineError(f"the timeline is not YAML or JSON: {str(ex).splitlines()[0]}") from None
    if "timeline" in d and isinstance(d["timeline"], dict):
        d = d["timeline"]
    if not isinstance(d, dict):
        raise TimelineError("a timeline is a mapping (story, counters, keyframes, features, end_hold, poster)")
    known = {"duration", "story", "counters", "keyframes", "features", "end_hold", "poster", "poster_hold", "poster_dissolve", "logo", "fade_s",
             "min_eye_m"}
    extra = sorted(set(d) - known)
    if extra:
        raise TimelineError(f"unknown timeline key(s): {', '.join(extra)}; the keys are {', '.join(sorted(known))}")
    dur = float(d.get("duration") or duration or 0) or None
    out = {"duration": dur, "fade_s": float(d.get("fade_s", FADE_S)), "end_hold": max(0.0, float(d.get("end_hold") or 0)),
           "poster": str(d.get("poster") or "none"), "poster_hold": max(0.0, float(d.get("poster_hold", 1.2))),
           "poster_dissolve": max(0.0, float(d.get("poster_dissolve", 0.6))), "logo": d.get("logo", True) is not False,
           "min_eye_m": float(d.get("min_eye_m", MIN_EYE_M)), "findings": []}
    if out["poster"] not in ("first", "last", "none"):
        raise TimelineError(f"poster is first, last or none, not '{out['poster']}'")
    out["story"] = _story(d.get("story") or [], dur)
    out["counters"] = [_counter(c, i) for i, c in enumerate(d.get("counters") or [])]
    out["keyframes"] = _keyframes(d.get("keyframes") or [], out)
    out["features"] = _features(d.get("features")) if d.get("features") else None
    return out


def _story(items, dur) -> list[dict]:
    rows = []
    for i, s in enumerate(items):
        if not isinstance(s, dict) or "line" not in s or "start" not in s:
            raise TimelineError(f"story line {i + 1} needs start (seconds) and line (text)")
        rows.append({"start": float(s["start"]), "end": None if s.get("end") is None else float(s["end"]), "line": str(s["line"])})
    rows.sort(key=lambda r: r["start"])
    for i, r in enumerate(rows):
        if r["end"] is None:
            r["end"] = rows[i + 1]["start"] if i + 1 < len(rows) else (dur if dur else math.inf)
        if r["end"] <= r["start"]:
            raise TimelineError(f"story line '{r['line'][:40]}' ends ({r['end']:g} s) before it starts ({r['start']:g} s)")
    return rows


def _counter(c, i) -> dict:
    if not isinstance(c, dict) or not c.get("keys"):
        raise TimelineError(f"counter {i + 1} needs keys: [[t, value], ...]")
    keys = sorted((float(k[0]), float(k[1])) for k in c["keys"])
    pos = str(c.get("position") or "top-right")
    if pos not in POSITIONS:
        raise TimelineError(f"counter {i + 1}: position is one of {', '.join(POSITIONS)}")
    text = str(c.get("text") or "{v}")
    if "{v}" not in text:
        raise TimelineError("counter " + str(i + 1) + ": its text needs {v} where the value goes")
    return {"text": text, "keys": keys, "format": str(c.get("format") or ",.0f"), "step": bool(c.get("step")), "position": pos,
            "start": float(c.get("start", keys[0][0])), "end": float(c["end"]) if c.get("end") is not None else math.inf}


def _keyframes(items, out) -> list[dict]:
    rows = []
    for i, k in enumerate(items):
        if not isinstance(k, dict) or "t" not in k:
            raise TimelineError(f"keyframe {i + 1} needs t (seconds)")
        free = "camera" in k
        if free:
            cam = [float(v) for v in k["camera"]]
            tgt = [float(v) for v in (k.get("target") or [])]
            if len(cam) != 3 or len(tgt) not in (2, 3):
                raise TimelineError(f"keyframe {i + 1}: camera is [lon, lat, height_m] and target [lon, lat] or [lon, lat, height_m]")
            if len(tgt) == 2:
                tgt.append(0.0)
            if cam[2] < out["min_eye_m"]:
                out["findings"].append({"id": "MAP-OUT-026", "severity": "INFO",
                                        "message": f"keyframe {i + 1} (t {float(k['t']):g} s): the eye at {cam[2]:g} m was raised to {out['min_eye_m']:g} m above "
                                                   "the ground; nearer than that MapLibre clips the ground and the page background shows"})
                cam[2] = out["min_eye_m"]
            row = {"t": float(k["t"]), "free": True, "camera": cam, "target": tgt}
        else:
            miss = [x for x in ("center", "zoom") if x not in k]
            if miss:
                raise TimelineError(f"keyframe {i + 1} needs camera and target, or center and zoom (missing {', '.join(miss)})")
            row = {"t": float(k["t"]), "free": False, "center": [float(v) for v in k["center"]], "zoom": float(k["zoom"]),
                   "pitch": float(k.get("pitch", 0.0)), "bearing": float(k.get("bearing", 0.0))}
        fov = k.get("fov")
        if fov is not None and not (5.0 <= float(fov) <= 120.0):
            raise TimelineError(f"keyframe {i + 1}: fov is between 5 and 120 degrees")
        row["fov"] = None if fov is None else float(fov)
        if row.get("pitch") is not None and not row["free"] and not (0.0 <= row["pitch"] <= MAX_PITCH):
            raise TimelineError(f"keyframe {i + 1}: pitch is between 0 and {MAX_PITCH:g}")
        ease = str(k.get("ease") or "smooth")
        if ease not in EASES:
            raise TimelineError(f"keyframe {i + 1}: ease is one of {', '.join(EASES)}")
        row["ease"] = ease
        row["layers"] = None if k.get("layers") is None else [str(x) for x in k["layers"]]
        row["opacity"] = {str(a): float(b) for a, b in (k.get("opacity") or {}).items()}
        rows.append(row)
    rows.sort(key=lambda r: r["t"])
    if rows and len({r["free"] for r in rows}) > 1:
        raise TimelineError("keyframes are all free cameras (camera, target) or all page views (center, zoom), not a mix")
    return rows


def _features(f) -> dict:
    if not isinstance(f, dict) or not f.get("layer"):
        raise TimelineError("features needs layer (a polygon layer of the map) and events")
    evs = []
    for i, e in enumerate(f.get("events") or []):
        if not isinstance(e, dict) or "t" not in e or "action" not in e:
            raise TimelineError(f"feature event {i + 1} needs t and action")
        a = str(e["action"])
        if a not in ACTIONS:
            raise TimelineError(f"feature event {i + 1}: action is one of {', '.join(ACTIONS)}")
        if e.get("ids") is None and e.get("where") is None:
            raise TimelineError(f"feature event {i + 1} needs ids or where")
        evs.append({"t": float(e["t"]), "action": a, "dur": max(0.0, float(e.get("dur", 0.0 if a in ("show", "hide", "recolor") else 1.0))),
                    "ids": None if e.get("ids") is None else [str(x) for x in e["ids"]], "where": e.get("where"),
                    "dx_mm": float(e.get("dx_mm", 0.0)), "dy_mm": float(e.get("dy_mm", 0.0)), "rotate": float(e.get("rotate", 0.0)),
                    "scale": float(e.get("scale", 1.0)), "colour": e.get("colour") or e.get("color"),
                    "opacity": None if e.get("opacity") is None else float(e["opacity"])})
    evs.sort(key=lambda e: e["t"])
    return {"layer": str(f["layer"]), "id_field": f.get("id_field"), "label_field": f.get("label_field"), "events": evs}


# ------------------------------------------------------------------------------------------------------------------- easing
def ease(u: float, kind: str = "smooth") -> float:
    u = min(1.0, max(0.0, u))
    if kind == "linear":
        return u
    if kind == "in":
        return u * u
    if kind == "out":
        return 1 - (1 - u) * (1 - u)
    if kind == "hold":
        return 0.0 if u < 1.0 else 1.0
    return u * u * (3 - 2 * u)


# ------------------------------------------------------------------------------------------------------------------- story and counters
def story_at(tl: dict, t: float) -> tuple[str, float] | None:
    """The story line on screen at t and its opacity (faded in and out over fade_s), or None."""
    f = max(1e-6, tl.get("fade_s", FADE_S))
    for r in tl.get("story") or []:
        if r["start"] <= t < r["end"]:
            a = min(1.0, (t - r["start"]) / f, (r["end"] - t) / f if math.isfinite(r["end"]) else 1.0)
            return r["line"], max(0.0, a)
    return None


def counter_value(c: dict, t: float) -> float:
    keys = c["keys"]
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t0 <= t < t1:
            return v0 if c["step"] else v0 + (v1 - v0) * ((t - t0) / (t1 - t0) if t1 > t0 else 1.0)
    return keys[-1][1]


def counters_at(tl: dict, t: float) -> list[tuple[str, str]]:
    """[(text, position)] of the counters showing at t."""
    out = []
    for c in tl.get("counters") or []:
        if c["start"] <= t < c["end"]:
            out.append((c["text"].replace("{v}", format(counter_value(c, t), c["format"])), c["position"]))
    return out


# ------------------------------------------------------------------------------------------------------------------- camera
def _lerp(a, b, u):
    return a + (b - a) * u


def _lerp_bearing(a: float, b: float, u: float) -> float:
    d = ((b - a + 540.0) % 360.0) - 180.0
    return (a + d * u) % 360.0


def camera_at(tl: dict, t: float) -> dict | None:
    """The camera at t from the keyframes, eased segment by segment (the ease of the keyframe a segment starts at); before the first and after
    the last keyframe it holds. A free camera is {free, camera: [lon, lat, h], target: [lon, lat, h], fov}; a page view {center, zoom, pitch,
    bearing, fov}. Layers and opacity come from the last keyframe reached; opacity is eased between keyframes that both set it."""
    ks = tl.get("keyframes") or []
    if not ks:
        return None
    if t <= ks[0]["t"]:
        a, b, u = ks[0], ks[0], 0.0
    elif t >= ks[-1]["t"]:
        a, b, u = ks[-1], ks[-1], 0.0
    else:
        j = next(i for i in range(1, len(ks)) if ks[i]["t"] > t)
        a, b = ks[j - 1], ks[j]
        u = ease((t - a["t"]) / max(1e-9, b["t"] - a["t"]), a["ease"])
    if a["free"]:
        cam = {"free": True, "camera": [round(_lerp(x, y, u), 8) for x, y in zip(a["camera"], b["camera"])],
               "target": [round(_lerp(x, y, u), 8) for x, y in zip(a["target"], b["target"])]}
    else:
        cam = {"free": False, "center": [round(_lerp(a["center"][0], b["center"][0], u), 8), round(_lerp(a["center"][1], b["center"][1], u), 8)],
               "zoom": round(_lerp(a["zoom"], b["zoom"], u), 4), "pitch": round(_lerp(a["pitch"], b["pitch"], u), 3),
               "bearing": round(_lerp_bearing(a["bearing"], b["bearing"], u), 3)}
    fa, fb = a.get("fov"), b.get("fov")
    cam["fov"] = None if fa is None and fb is None else round(_lerp(fa if fa is not None else fb, fb if fb is not None else fa, u), 3)
    reached = [k for k in ks if k["t"] <= t] or [ks[0]]
    lay = next((k["layers"] for k in reversed(reached) if k["layers"] is not None), None)
    cam["layers"] = lay
    op: dict = {}
    for k in reached:
        op.update(k["opacity"])
    for lid, vb in b["opacity"].items():
        if lid in a["opacity"] and a is not b:
            op[lid] = round(_lerp(a["opacity"][lid], vb, u), 4)
    cam["opacity"] = op
    return cam


# ------------------------------------------------------------------------------------------------------------------- feature events
def _matches(where: str, row: dict) -> bool:
    m = re.match(r"^\s*([^=<>!]+?)\s*(>=|<=|!=|=|<|>)\s*(.+?)\s*$", str(where))
    if not m:
        raise TimelineError(f"where is FIELD=VALUE, FIELD!=VALUE or FIELD>=, <=, <, > NUMBER, not '{where}'")
    col, op, val = m.group(1), m.group(2), m.group(3)
    v = row.get(col)
    if op in ("=", "!="):
        eq = str(v).strip().lower() == val.strip().lower()
        return eq if op == "=" else not eq
    try:
        x, y = float(v), float(val)
    except (TypeError, ValueError):
        return False
    return {">=": x >= y, "<=": x <= y, "<": x < y, ">": x > y}[op]


def resolve_targets(feats: dict, rows: list[dict], ids: list[str]) -> list[list[str]]:
    """The feature ids each event applies to (ids as given, or the rows matching `where`)."""
    out = []
    for e in feats["events"]:
        if e["ids"] is not None:
            out.append([x for x in e["ids"] if x in set(ids)])
        else:
            out.append([fid for fid, r in zip(ids, rows) if _matches(e["where"], r)])
    return out


BASE = {"opacity": 1.0, "dx": 0.0, "dy": 0.0, "rot": 0.0, "scale": 1.0, "lift": 0.0, "colour": None}


def feature_state(feats: dict, targets: list[list[str]], ids: list[str], t: float) -> dict:
    """Each feature's state at t: {opacity, dx, dy (mm, y down), rot (degrees), scale, lift (0 to 1), colour}. Events apply in time order;
    an event eases from the state it found to its target over dur."""
    st = {fid: dict(BASE) for fid in ids}
    for e, tg in zip(feats["events"], targets):
        if t < e["t"]:
            break
        u = 1.0 if e["dur"] <= 0 else ease((t - e["t"]) / e["dur"])
        for fid in tg:
            s = st[fid]
            a = e["action"]
            goal = dict(s)
            if a == "show":
                goal["opacity"] = 1.0 if e["opacity"] is None else e["opacity"]
            elif a in ("hide", "fade"):
                goal["opacity"] = 0.0 if e["opacity"] is None else e["opacity"]
            elif a == "lift":
                goal["lift"] = 1.0
            elif a == "drift":
                goal.update(dx=s["dx"] + e["dx_mm"], dy=s["dy"] + e["dy_mm"], rot=s["rot"] + e["rotate"], scale=s["scale"] * e["scale"], lift=1.0)
                if e["opacity"] is not None:
                    goal["opacity"] = e["opacity"]
            elif a == "settle":
                goal.update(dx=0.0, dy=0.0, rot=0.0, scale=1.0, lift=0.0)
                if e["opacity"] is not None:
                    goal["opacity"] = e["opacity"]
            elif a == "recolor":
                goal["colour"] = e["colour"]
            for k in ("opacity", "dx", "dy", "rot", "scale", "lift"):
                s[k] = s[k] + (goal[k] - s[k]) * u
            if a == "recolor" and u >= 1.0:
                s["colour"] = goal["colour"]
            elif a == "recolor":
                s["_mix"] = (goal["colour"], u)
    return st


def end_collisions(boxes: dict, *, min_overlap_mm2: float = 1.0) -> list[dict]:
    """Moved features that end on top of each other (Day 14 issue 6: two scraps at Timgad and Dougga overlapped). `boxes` is
    {id: (x0, y0, x1, y1)} in mm of the features that moved and stay visible. -> WARN findings, one per pair."""
    out = []
    items = sorted(boxes.items())
    for i, (a, ba) in enumerate(items):
        for b, bb in items[i + 1:]:
            w = min(ba[2], bb[2]) - max(ba[0], bb[0])
            h = min(ba[3], bb[3]) - max(ba[1], bb[1])
            if w > 0 and h > 0 and w * h >= min_overlap_mm2:
                out.append({"id": "MAP-OUT-027", "severity": "WARN",
                            "message": f"features '{a}' and '{b}' end the video overlapping by {w * h:.0f} mm2; move one (dx_mm, dy_mm) or settle it", "object": [a, b]})
    return out


# ------------------------------------------------------------------------------------------------------------------- frame order
def sequence(n: int, fps: float, tl: dict) -> list[tuple[int, int | None, float]]:
    """The video's frames from the n drawn ones: [(frame, blend_frame, blend_alpha)]. A poster (first or last) is held poster_hold seconds
    and dissolves into frame 0 over poster_dissolve; the last frame is held end_hold seconds."""
    seq: list[tuple[int, int | None, float]] = []
    p = tl.get("poster") or "none"
    if p != "none" and n > 1:
        src = n - 1 if p == "last" else 0
        seq += [(src, None, 0.0)] * max(1, round(tl.get("poster_hold", 1.2) * fps))
        if p == "last":
            k = max(0, round(tl.get("poster_dissolve", 0.6) * fps))
            seq += [(src, 0, (j + 1) / (k + 1)) for j in range(k)]
    seq += [(i, None, 0.0) for i in range(n)]
    seq += [(n - 1, None, 0.0)] * round((tl.get("end_hold") or 0.0) * fps)
    return seq


def assemble(frames_dir: Path, n: int, fps: float, tl: dict) -> tuple[Path, int]:
    """The frames in video order (sequence()) in frames_dir/seq, blended where the poster dissolves. -> (folder, frame count). With no
    poster and no end hold the drawn frames are the video and the folder is frames_dir itself."""
    seq = sequence(n, fps, tl)
    if len(seq) == n and all(s == (i, None, 0.0) for i, s in enumerate(seq)):
        return frames_dir, n
    import shutil
    from PIL import Image
    out = frames_dir / "seq"
    out.mkdir(exist_ok=True)
    for old in out.glob("frame_*.png"):
        old.unlink()
    for j, (i, b, a) in enumerate(seq):
        dst = out / f"frame_{j:05d}.png"
        src = frames_dir / f"frame_{i:05d}.png"
        if b is None:
            shutil.copyfile(src, dst)
        else:
            Image.blend(Image.open(src).convert("RGB"), Image.open(frames_dir / f"frame_{b:05d}.png").convert("RGB"), a).save(dst)
    return out, len(seq)


# ------------------------------------------------------------------------------------------------------------------- painting
def _rgb(hx, a: int = 255) -> tuple:
    if not hx:
        return (255, 255, 255, a)
    hx = str(hx).lstrip("#")
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4)) + (a,)


def _clear(box, avoid, down: bool):
    """Move box (x0, y0, x1, y1) down (or up) past every rectangle of `avoid` it meets."""
    x0, y0, x1, y1 = box
    moved = True
    while moved:
        moved = False
        for a in avoid or []:
            if x0 < a[2] and x1 > a[0] and y0 < a[3] and y1 > a[1]:
                h = y1 - y0
                y0 = a[3] + 4 if down else a[1] - 4 - h
                y1 = y0 + h
                moved = True
    return x0, y0, x1, y1


def paint(img, t: float, tl: dict, tokens: dict, *, font_path: str, logo=None, bottom_px: int = 0, top_px: int = 0, avoid=None):
    """Draw the timeline's screen elements on a frame at t: the story line (centred, above the credits band of bottom_px), the counters in
    their corners, and the brand logo bottom left above the band. `avoid`: rectangles in pixels (the map's legend, title, inset, logo) a
    counter steps clear of. -> (image, placed: {story, counters, logo})."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = img.size
    k = H / 720.0
    im = img.convert("RGBA")
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    ink = _rgb(tokens.get("label") or tokens.get("emphasis")) if (tokens.get("label") or tokens.get("emphasis")) else (0, 0, 0, 255)
    bgc = tokens.get("background") or tokens.get("legend_bg")
    placed = {"story": False, "counters": 0, "logo": False}
    pad = int(16 * k)
    logo_w = 0
    if logo is not None and tl.get("logo", True):
        lh = max(16, int(44 * k))
        lg = logo.convert("RGBA")
        lg = lg.resize((max(1, round(lg.width * lh / lg.height)), lh), Image.LANCZOS)
        over.alpha_composite(lg, (pad, H - bottom_px - pad - lh))
        logo_w = lg.width + pad
        placed["logo"] = True
    s = story_at(tl, t)
    if s:
        text, a = s
        size = max(11, int(22 * k))
        f = ImageFont.truetype(font_path, size)
        maxw = W - 2 * (pad + logo_w)
        while d.textlength(text, font=f) > maxw and size > max(9, int(14 * k)):
            size -= 1
            f = ImageFont.truetype(font_path, size)
        tw = d.textlength(text, font=f)
        x, y = (W - tw) / 2, H - bottom_px - pad - size * 1.6
        bx = [x - 14 * k, y - 8 * k, x + tw + 14 * k, y + size * 1.3 + 6 * k]
        d.rounded_rectangle(bx, radius=int(6 * k), fill=_rgb(bgc, int(215 * a)))
        d.text((x, y), text, font=f, fill=ink[:3] + (int(255 * a),))
        placed["story"] = True
    cf = ImageFont.truetype(font_path, max(12, int(26 * k)))
    slots = {"top-left": 0, "top-right": 0, "bottom-right": 0, "bottom-left": 0}
    for text, pos in counters_at(tl, t):
        tw = d.textlength(text, font=cf)
        row = slots[pos]
        slots[pos] += 1
        x = pad if pos.endswith("left") else W - pad - tw
        y = (top_px + pad + row * cf.size * 1.5) if pos.startswith("top") else (H - bottom_px - pad - (row + 1) * cf.size * 1.5
                                                                             - (int(44 * k) + pad if pos == "bottom-left" and placed["logo"] else 0))
        x, y, _x1, _y1 = _clear((x, y, x + tw, y + cf.size * 1.25), avoid, pos.startswith("top"))
        d.text((x, y), text, font=cf, fill=ink, stroke_width=max(2, int(cf.size * 0.12)), stroke_fill=_rgb(bgc))
        placed["counters"] += 1
    return Image.alpha_composite(im, over).convert("RGB"), placed


def dumps(tl: dict) -> str:
    """The checked timeline as JSON (for the report and the manifest's hash)."""
    return json.dumps({k: v for k, v in tl.items() if k != "findings"}, sort_keys=True, default=str)
