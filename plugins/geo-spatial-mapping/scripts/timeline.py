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
  date: auto                         1.19.0: the date at the right end of the credits band (auto: today; off; or a text of your own)
  grow: {layer: bars, start: 3, end: 19}   1.19.0: an extruded layer built with extrude.moments rises through its moments (flythrough);
                                     1.20.0: its classes recolour as the values grow (recolor: false keeps the final colours)
  clock: [["2023-02-05", 0], ["2023-02-08", 6], ["2023-02-11", 22], ["2023-03-15", 40]]
                                     3.4.0 (Day 21 Antakya #7): real time to video time, piecewise linear (slow motion through a burst). Then
                                     a story line may say `at: 2023-02-08` instead of start, a counter's keys may be [real time, value], a
                                     feature event's t may be a real time, and a reveal's times are read through it
  counters: [{format: date, pattern: "%-d %B %Y", position: top-right}]
                                     3.4.0 (#6): a date counter shows the clock's real time at t (no keys needed)
  values: {layer: cells, columns: [u00, ..., u23], interpolate: cubic, cyclic: true, sparkline: {label: ...}}
                                     4.1.0 (Day 24): a polygon layer's colour through time, smoothly (motion.py)
  flow: {layer: canals, from: [lon, lat], gap_m: 120, arrive_s: 20, speed_kmh: 3.24}
                                     4.1.0 (Day 24): water, traffic or a signal moving along a line layer from its source (motion.py)
  top: auto | true | false | {layers: [sites]}
                                     4.1.0: the map's furniture drawn over the moving layers (auto: with values or flow); layers: data
                                     layers drawn over them too (masts over moving cells)
  overflow: {px: 30, bottom_px: 12}  4.1.0: moving layers may run past the map frame, fading out
  counters: [{format: clock | hm}]   4.1.0: a time of day ("4:15 am") or a duration ("23 h 30 min") from the counter's value in hours
  reveal: {layer: buildings, time_field: created, end_field: deleted, class_field: kind, flash_s: 0.6}
                                     3.4.0 (#3): a polygon or point layer appears feature by feature at its time (65,000 buildings): drawn
                                     from an id raster, class colours, each new feature flashing in the label colour, keyed "Just added"

The camera is never put under min_eye_m (6 m) above the ground: MapLibre clips the ground nearer than its near plane, so a 3 m street-level
eye showed the page background (Day 15 issue 12). A finding names each keyframe raised.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

FADE_S = 0.45
READ_S_PER_CHAR = 0.06       # 5.5.0 (D26-32): a story line with no end stays its reading time (60 ms a character, at least READ_MIN_S)
READ_MIN_S = 2.5
POSTERS = ("first", "last", "none", "preview")
MIN_EYE_M = 6.0
MAX_PITCH = 115.0
EASES = ("smooth", "linear", "in", "out", "hold")
ACTIONS = ("show", "hide", "fade", "lift", "drift", "settle", "recolor")
POSITIONS = ("top-left", "top-right", "bottom-right", "bottom-left")
LIFT_MM = 3.0


class TimelineError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------------------- loading
LEGEND_FULL_S = 6.0          # 5.6.0 (Q-07): the legend is read at full size this long, then steps back
LEGEND_REDUCE_MIN_S = 15.0   # ...in a video at least this long (a short one keeps it whole)
LEGEND_SCALE = 0.7           # ...to this share of its size, in its own corner
LEGEND_FADE_S = 0.6


def _legend(v, dur: float | None) -> dict:
    """5.6.0 (Q-07): the video's tiers are counter, chart, captions, then the legend: after its first seconds the key steps back to
    LEGEND_SCALE of its size in its own corner. `legend: {reduce_after_s: 8, scale: 0.6}`; `legend: {reduce: false}` keeps it whole."""
    if v is not None and not isinstance(v, dict):
        raise TimelineError("legend is a mapping: {reduce_after_s: SECONDS, scale: 0.5 to 1, reduce: false}")
    v = v or {}
    extra = sorted(set(v) - {"reduce_after_s", "scale", "reduce"})
    if extra:
        raise TimelineError(f"unknown legend key(s): {', '.join(extra)}; the keys are reduce, reduce_after_s, scale")
    sc = float(v.get("scale", LEGEND_SCALE))
    if not 0.4 <= sc <= 1.0:
        raise TimelineError(f"legend.scale is between 0.4 and 1, not {sc:g}")
    asked = v.get("reduce")
    at = float(v.get("reduce_after_s", LEGEND_FULL_S))
    on = asked is True or (asked is not False and ("reduce_after_s" in v or (dur or 0) >= LEGEND_REDUCE_MIN_S))
    return {"reduce": bool(on and sc < 1.0), "at": max(0.0, at), "scale": sc, "fade": LEGEND_FADE_S}


def legend_mix(tl: dict, t: float) -> float:
    """How far the legend has stepped back at time t: 0 (whole) to 1 (reduced)."""
    lg = tl.get("legend") or {}
    if not lg.get("reduce"):
        return 0.0
    return min(1.0, max(0.0, (t - lg["at"]) / max(lg["fade"], 1e-6)))


def load(src, *, duration: float | None = None) -> dict:
    """A timeline from a path, a YAML/JSON string or a dict, checked and filled in. -> {duration, story, counters, keyframes, features,
    end_hold, poster, poster_hold, poster_dissolve, logo, findings}. Raises TimelineError naming the first problem."""
    _base_dir = None                                  # 5.8.0: the folder a chart's `source:` table is read from (the timeline file's own)
    if src is None:
        d: dict = {}
    elif isinstance(src, dict):
        d = dict(src)
    else:
        p = Path(str(src))
        if len(str(src)) < 1024 and p.is_file():
            _base_dir = p.resolve().parent
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
             "min_eye_m", "date", "grow", "chart", "clock", "reveal", "values", "flow", "top", "overflow", "legend"}
    extra = sorted(set(d) - known)
    if extra:
        raise TimelineError(f"unknown timeline key(s): {', '.join(extra)}; the keys are {', '.join(sorted(known))}")
    dur = float(d.get("duration") or duration or 0) or None
    out = {"duration": dur, "fade_s": float(d.get("fade_s", FADE_S)), "end_hold": max(0.0, float(d.get("end_hold") or 0)),
           "poster": str(d.get("poster") or "none"), "poster_set": d.get("poster") is not None, "poster_hold": max(0.0, float(d.get("poster_hold", 1.2))),
           "poster_dissolve": max(0.0, float(d.get("poster_dissolve", 0.6))), "logo": d.get("logo", True) is not False,
           "min_eye_m": float(d.get("min_eye_m", MIN_EYE_M)), "findings": [],
           "date": d.get("date", "auto")}               # 1.19.0 (Day 16 issue 14): the date in the credits band: auto, off or a text
    if out["poster"] not in POSTERS and poster_seconds(out["poster"]) is None:
        # 5.5.0 (MEDIUM-3): preview (the route drawn faint with the title, no end state) or a second of the video, besides first, last, none
        raise TimelineError(f"poster is first, last, preview, none or a time in seconds, not '{out['poster']}'")
    out["legend"] = _legend(d.get("legend"), dur)
    out["clock"] = _clock(d.get("clock")) if d.get("clock") else None
    out["story"] = _story(d.get("story") or [], dur, out["clock"])
    out["counters"] = [_counter(c, i, out["clock"]) for i, c in enumerate(d.get("counters") or [])]
    out["reveal"] = _reveal(d["reveal"]) if d.get("reveal") else None
    out["keyframes"] = _keyframes(d.get("keyframes") or [], out)
    out["features"] = _features(d.get("features"), out["clock"]) if d.get("features") else None
    out["grow"] = _grow(d["grow"]) if d.get("grow") else None
    import motion                                     # 4.1.0 (Day 24): values through time, flow along lines, the furniture on top
    try:
        out["values"] = motion.load_values(d["values"], dur) if d.get("values") else None
        out["flow"] = motion.load_flow(d["flow"], dur) if d.get("flow") else None
        out["overflow"] = motion.load_overflow(d.get("overflow"))
    except motion.MotionError as ex:
        raise TimelineError(str(ex)) from None
    top = d.get("top", "auto")
    out["top_layers"] = [str(x) for x in (top.get("layers") or [])] if isinstance(top, dict) else []
    if isinstance(top, dict):                         # {layers: [sites]}: these data layers are drawn over the moving ones as well
        top = top.get("on", True)
    out["top"] = bool(out["values"] or out["flow"]) if top in ("auto", None) else bool(top)
    out["findings"] += caption_repeats(out)
    import heat_anim                                  # 2.1.0 (Day 19 video issue 15): bars and a line, the current row marked
    try:
        out["chart"] = heat_anim.load_chart(d.get("chart"), base=_base_dir)        # 5.8.0: a chart's source table is read beside the timeline
    except heat_anim.HeatError as ex:
        raise TimelineError(str(ex)) from None
    return out


def _grow(g) -> dict:
    """1.19.0 (Day 16 issue 16): `grow: {layer, start, end, ease, per_unit_m}`: an extruded layer built with extrude.moments rises through
    its moments (c0 .. cN) between start and end seconds, each block on its own values, blended between moments."""
    if not isinstance(g, dict) or not g.get("layer"):
        raise TimelineError("grow needs layer (an extruded layer built with extrude.moments), start and end (seconds)")
    a, b = float(g.get("start", 0.0)), float(g.get("end", 0.0))
    if b <= a:
        raise TimelineError(f"grow ends ({b:g} s) before it starts ({a:g} s)")
    return {"layer": str(g["layer"]), "start": a, "end": b, "ease": str(g.get("ease", "linear")),
            "per_unit_m": float(g["per_unit_m"]) if g.get("per_unit_m") is not None else None, "recolor": g.get("recolor", True) is not False}


def grow_at(tl: dict, t: float, moments: int) -> dict | None:
    """Where the grow block stands at t: {layer, i, j, f}, the moment before (i), after (j) and the blend between them (f), or None."""
    g = tl.get("grow")
    if not g or moments < 1:
        return None
    u = min(1.0, max(0.0, (t - g["start"]) / (g["end"] - g["start"])))
    u = ease(u, g["ease"]) if g["ease"] != "linear" else u
    x = u * (moments - 1)
    i = int(math.floor(x))
    j = min(moments - 1, i + 1)
    return {"layer": g["layer"], "i": i, "j": j, "f": round(x - i, 4), "k": g["per_unit_m"]}


def caption_repeats(tl: dict) -> list[dict]:
    """3.4.0 (eval INFO-3): MAP-STORY-026 (WARN) when a story line states the number or date a counter shows on screen at that moment:
    two text blocks then say one thing; the caption should say what the counter cannot (where, who, why)."""
    out = []
    for r in [q for q in tl.get("story") or [] if not q.get("pending")]:
        t = r["start"] + min(0.5, max(0.0, (r["end"] - r["start"]) / 2)) if math.isfinite(r["end"]) else r["start"] + 0.5
        for text, _pos in counters_at(tl, t):
            for tok in re.findall(r"\d[\d,.]*\d|\d{3,}", text) + ([text] if re.search(r"[A-Za-z]{3,}", text) and re.search(r"\d", text) else []):
                core = tok.strip()
                if len(re.sub(r"\D", "", core)) >= 3 and core in r["line"]:
                    out.append({"id": "MAP-STORY-026", "severity": "WARN", "message": f"the story line at {r['start']:g} s ('{r['line'][:60]}') repeats "
                                f"'{core}', which the counter shows at that moment; let the caption say what the counter cannot (where, who, why)",
                                "detail": {"line": r["line"], "counter": text}})
                    break
    return out


# ------------------------------------------------------------------------------------------------------------------- 3.4.0: the clock
def real_seconds(v) -> float:
    """A real time as seconds since 1970 (UTC): an ISO date or date-time, a datetime, or a number (already seconds, or a year)."""
    import datetime as dt
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)).timestamp()
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=dt.timezone.utc).timestamp()
    t = str(v).strip().replace("Z", "+00:00")
    try:
        x = dt.datetime.fromisoformat(t)
    except ValueError:
        raise TimelineError(f"'{v}' is not a date or time (2023-02-08, 2023-02-08T12:00Z)") from None
    return (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)).timestamp()


def _clock(items) -> list[tuple[float, float]]:
    if not isinstance(items, list) or len(items) < 2:
        raise TimelineError("clock is a list of at least two [real time, video seconds] pairs")
    pts = []
    for i, p in enumerate(items):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise TimelineError(f"clock entry {i + 1} is [real time, video seconds]")
        pts.append((real_seconds(p[0]), float(p[1])))
    pts.sort()
    for (r0, v0), (r1, v1) in zip(pts, pts[1:]):
        if r1 <= r0 or v1 < v0:
            raise TimelineError("the clock's real times and video seconds both rise (a clock never runs backwards)")
    return pts


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    if x <= xs[0]:
        k = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0.0
        return ys[0] + (x - xs[0]) * k
    for a, b, c, d in zip(xs, xs[1:], ys, ys[1:]):
        if a <= x <= b:
            return c + (d - c) * ((x - a) / (b - a) if b > a else 0.0)
    k = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if xs[-1] != xs[-2] else 0.0
    return ys[-1] + (x - xs[-1]) * k


def to_video(clock, real) -> float:
    """Video seconds for a real time, through the clock (straight on past its ends)."""
    if not clock:
        raise TimelineError("a real time needs a clock: [[real time, video seconds], ...] in the timeline")
    return _interp(real_seconds(real), [p[0] for p in clock], [p[1] for p in clock])


def to_real(clock, t: float) -> float:
    """Real time (seconds since 1970, UTC) at video second t."""
    vs = [p[1] for p in clock]
    if len(set(vs)) < len(vs):                       # a pause in video time: the later real time of a held moment
        pass
    return _interp(float(t), vs, [p[0] for p in clock])


def _vt(v, clock) -> float:
    """A time in a timeline: video seconds as given, or a real time read through the clock."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return to_video(clock, v)


def strftime_portable(ts: float, pattern: str) -> str:
    """strftime with %-d / %-m / %-H (no leading zero) on every platform (Windows has no %-d)."""
    import datetime as dt
    x = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    for code, val in (("%-d", str(x.day)), ("%-m", str(x.month)), ("%-H", str(x.hour)), ("%-I", str((x.hour % 12) or 12))):
        pattern = pattern.replace(code, val)
    return x.strftime(pattern)


def _reveal(r) -> dict:
    if not isinstance(r, dict) or not r.get("layer") or not r.get("time_field"):
        raise TimelineError("reveal needs layer (a polygon or point layer of the map) and time_field (when each feature appears)")
    return {"layer": str(r["layer"]), "time_field": str(r["time_field"]), "end_field": r.get("end_field"), "class_field": r.get("class_field"),
            "colours": dict(r.get("colours") or {}), "flash_s": max(0.0, float(r.get("flash_s", 0.6))), "flash_colour": r.get("flash_colour"),
            "flash_label": str(r.get("flash_label") or "Just added"), "glow": r.get("glow", True) is not False,
            "supersample": int(min(4, max(1, int(r.get("supersample", 3))))), "opacity": float(r.get("opacity", 1.0)),
            "before": r.get("before")}


def poster_seconds(p) -> float | None:
    """A poster given as a time ('12', '12.5', '12s'), or None."""
    try:
        return max(0.0, float(str(p).strip().rstrip("s")))
    except (TypeError, ValueError):
        return None


def reading_s(line: str) -> float:
    return max(READ_MIN_S, READ_S_PER_CHAR * len(str(line or "")))


def _story(items, dur, clock=None) -> list[dict]:
    rows = []
    for i, s in enumerate(items):
        if isinstance(s, dict) and "start" not in s and "at" not in s and (s.get("at_km") is not None or s.get("at_fraction") is not None):
            # 5.5.0 (D26-32): a beat tied to a distance along the flow; its time is set once the flow is measured (resolve_beats)
            rows.append({"start": 0.0, "end": None if s.get("end") is None else float(s["end"]), "line": str(s.get("line") or ""),
                         "at_km": None if s.get("at_km") is None else float(s["at_km"]),
                         "at_fraction": None if s.get("at_fraction") is None else float(s["at_fraction"]),
                         "for_s": None if s.get("for_s") is None else float(s["for_s"]), "pending": True, "end_given": s.get("end") is not None})
            if not rows[-1]["line"]:
                raise TimelineError(f"story line {i + 1} needs line (text)")
            continue
        if isinstance(s, dict) and "at" in s and "start" not in s:
            s = {**s, "start": _vt(s["at"], clock)}           # 3.4.0: a line placed at a real time, by the clock
            if s.get("until") is not None:
                s["end"] = _vt(s["until"], clock)
        if not isinstance(s, dict) or "line" not in s or "start" not in s:
            raise TimelineError(f"story line {i + 1} needs start (seconds) or at (a real time, with a clock) and line (text)")
        rows.append({"start": _vt(s["start"], clock), "end": None if s.get("end") is None else _vt(s["end"], clock), "line": str(s["line"]),
                     "for_s": None if s.get("for_s") is None else float(s["for_s"]), "end_given": s.get("end") is not None or s.get("until") is not None})
    return _story_ends(rows, dur)


def _story_ends(rows: list[dict], dur) -> list[dict]:
    """Ends filled in: a line with no end stays its reading time (or for_s), never past the next line's start nor the video's end
    (5.5.0, D26-32: a line used to stay until the next one, over pictures that no longer showed what it said)."""
    rows.sort(key=lambda r: r["start"])
    for i, r in enumerate(rows):
        if r.get("pending"):
            continue
        if not r.get("end_given"):
            nxt = next((q["start"] for q in rows[i + 1:] if not q.get("pending")), None)
            cap = min(x for x in (nxt, dur if dur else None, math.inf) if x is not None)
            r["end"] = min(cap, r["start"] + (r.get("for_s") or reading_s(r["line"])))
        if r["end"] <= r["start"]:
            raise TimelineError(f"story line '{r['line'][:40]}' ends ({r['end']:g} s) before it starts ({r['start']:g} s)")
    return rows


def resolve_beats(tl: dict, flow) -> list[str]:
    """5.5.0 (D26-32): story lines given at_km or at_fraction start when the flow's front reaches that distance; their ends are filled in
    again. `flow` has time_at_m(m) and max_m. -> notes."""
    rows = tl.get("story") or []
    pend = [r for r in rows if r.get("pending")]
    if not pend:
        return []
    if flow is None:
        raise TimelineError("a story line at_km or at_fraction needs a flow: in the timeline (the distance is along it)")
    for r in pend:
        m = r["at_km"] * 1000.0 if r.get("at_km") is not None else float(r["at_fraction"]) * float(flow.max_m)
        r["start"] = round(float(flow.time_at_m(m)), 3)
        r.pop("pending", None)
    _story_ends(rows, tl.get("duration"))
    return [f"{len(pend)} story line(s) placed by distance along the flow"]


def _counter(c, i, clock=None) -> dict:
    if isinstance(c, dict) and str(c.get("format") or "") == "date":
        # 3.4.0 (Antakya #6): the clock's real time as a date, one counter in place of two step counters
        if not clock:
            raise TimelineError(f"counter {i + 1}: format: date shows the clock's time; add clock: [[real time, video seconds], ...]")
        pos = str(c.get("position") or "top-right")
        if pos not in POSITIONS:
            raise TimelineError(f"counter {i + 1}: position is one of {', '.join(POSITIONS)}")
        return {"text": str(c.get("text") or "{v}"), "keys": [(clock[0][1], 0.0), (clock[-1][1], 0.0)], "format": "date",
                "pattern": str(c.get("pattern") or "%-d %B %Y"), "step": False, "position": pos, "clock": clock,
                "start": _vt(c.get("start", clock[0][1]), clock), "end": _vt(c["end"], clock) if c.get("end") is not None else math.inf}
    if isinstance(c, dict) and c.get("source") in ("reveal", "flow") and not c.get("keys"):
        # 3.4.0: a counter of the reveal's features on the map at each moment (filled in by the video once the layer is read)
        c = {**c, "keys": [[0.0, 0.0], [1.0, 0.0]]}
    if not isinstance(c, dict) or not c.get("keys"):
        raise TimelineError(f"counter {i + 1} needs keys: [[t, value], ...]")
    keys = sorted((_vt(k[0], clock), float(k[1])) for k in c["keys"])
    pos = str(c.get("position") or "top-right")
    if pos not in POSITIONS:
        raise TimelineError(f"counter {i + 1}: position is one of {', '.join(POSITIONS)}")
    text = str(c.get("text") or "{v}")
    if "{v}" not in text:
        raise TimelineError("counter " + str(i + 1) + ": its text needs {v} where the value goes")
    return {"text": text, "keys": keys, "format": str(c.get("format") or ",.0f"), "step": bool(c.get("step")), "position": pos,
            "start": _vt(c.get("start", keys[0][0]), clock), "end": _vt(c["end"], clock) if c.get("end") is not None else math.inf,
            "source": c.get("source"), "class": c.get("class"),
            # 5.5.0 (D26-33): a flow counter with a total runs from `from` to `total` with the flow's front (format: day shows whole days)
            "total": None if c.get("total") is None else float(c["total"]), "from": float(c.get("from", 0.0))}


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
        row["chapter"] = None if k.get("chapter") is None else int(k["chapter"])     # 2.0.0: the story chapter whose highlight the page draws from here on
        row["opacity"] = {str(a): float(b) for a, b in (k.get("opacity") or {}).items()}
        rows.append(row)
    rows.sort(key=lambda r: r["t"])
    if rows and len({r["free"] for r in rows}) > 1:
        raise TimelineError("keyframes are all free cameras (camera, target) or all page views (center, zoom), not a mix")
    return rows


def _features(f, clock=None) -> dict:
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
        evs.append({"t": _vt(e["t"], clock), "action": a, "dur": max(0.0, float(e.get("dur", 0.0 if a in ("show", "hide", "recolor") else 1.0))),
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
        if r.get("pending"):
            continue
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


def clock_text(hours: float, fmt: str) -> str:
    """4.1.0 (Day 24): format: clock shows hours of the day ("4:15 am", to the quarter hour); format: hm a duration ("23 h 30 min", to ten
    minutes)."""
    if fmt == "clock":
        h = float(hours) % 24
        hr, mn = int(h), int(round((h - int(h)) * 60 / 15) * 15)
        if mn == 60:
            hr, mn = (hr + 1) % 24, 0
        return f"{hr % 12 or 12}:{mn:02d} {'am' if hr < 12 else 'pm'}"
    h = max(0.0, float(hours))
    hr, mn = int(h), int((h - int(h)) * 60) // 10 * 10
    return f"{hr} h {mn:02d} min"


def counters_at(tl: dict, t: float) -> list[tuple[str, str]]:
    """[(text, position)] of the counters showing at t."""
    out = []
    for c in tl.get("counters") or []:
        if c["start"] <= t < c["end"]:
            if c["format"] == "date":
                v = strftime_portable(to_real(c["clock"], t), c["pattern"])
            elif c["format"] in ("clock", "hm"):
                v = clock_text(counter_value(c, t), c["format"])
            elif c["format"] in ("day", "int"):                       # 5.5.0 (D26-33): a whole count (the day reached), never rounded up early
                v = f"{int(math.floor(counter_value(c, t) + 1e-9)):,}"
            else:
                v = format(counter_value(c, t), c["format"])
            out.append((c["text"].replace("{v}", v), c["position"]))
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
    cam["chapter"] = next((k["chapter"] for k in reversed(reached) if k.get("chapter") is not None), None)
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
        # 5.5.0: preview is a picture of its own (frame -1: frame_poster.png); a time is that moment's frame
        sec = poster_seconds(p) if p not in POSTERS else None
        src = n - 1 if p == "last" else (-1 if p == "preview" else (min(n - 1, round(sec * fps)) if sec is not None else 0))
        seq += [(src, None, 0.0)] * max(1, round(tl.get("poster_hold", 1.2) * fps))
        if src != 0:
            k = max(0, round(tl.get("poster_dissolve", 0.6) * fps))
            seq += [(src, 0, (j + 1) / (k + 1)) for j in range(k)]
    seq += [(i, None, 0.0) for i in range(n)]
    seq += [(n - 1, None, 0.0)] * round((tl.get("end_hold") or 0.0) * fps)
    return seq


def lead_in_s(tl: dict, fps: float) -> float:
    """2.0.0: the seconds a poster frame adds before the first drawn frame, so a caption file's times stay on their pictures."""
    p = tl.get("poster") or "none"
    if p == "none":
        return 0.0
    k = max(1, round(tl.get("poster_hold", 1.2) * fps)) + (max(0, round(tl.get("poster_dissolve", 0.6) * fps)) if p != "first" else 0)
    return k / fps


def write_captions(tl: dict, out_dir: Path, stem: str, fps: float) -> list[str]:
    """2.0.0: <stem>.srt and <stem>.vtt beside a video whose timeline has a story line: the captions as text a player can show, a reader
    can search and a platform can translate. Nothing is written for a timeline with no lines."""
    import gmap_common as gc
    gc.ledger_start_lazy("timeline.write_captions")    # 3.4.0 (F-3)
    if not [r for r in (tl.get("story") or []) if str(r.get("line") or "").strip()]:
        return []
    import story
    files = []
    for kind in ("srt", "vtt"):
        p = Path(out_dir) / f"{stem}.{kind}"
        p.write_text(story.captions(tl, kind, lead_in=lead_in_s(tl, fps)), encoding="utf-8")
        files.append(str(p))
    return files


def assemble(frames_dir: Path, n: int, fps: float, tl: dict) -> tuple[Path, int]:
    """The frames in video order (sequence()) in frames_dir/seq, blended where the poster dissolves. -> (folder, frame count). With no
    poster and no end hold the drawn frames are the video and the folder is frames_dir itself."""
    import gmap_common as gc
    gc.ledger_start_lazy("timeline.assemble")
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
        src = frames_dir / ("frame_poster.png" if i < 0 else f"frame_{i:05d}.png")
        if i < 0 and not src.is_file():
            src = frames_dir / "frame_00000.png"           # no preview picture was made: the first frame stands in
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


def story_room(d, text, font_path, size, min_size, y, avoid, pad, W, H, lo, hi, k, centre=None):
    """Where the story line goes (2.1.0): on its row between `lo` and `hi` less every overlay rectangle of `avoid` the row meets, the widest
    such gap, the type made smaller (to `min_size`) to fit it; when no gap holds it, the row moves up past the overlays. -> (x, y, size, font, w)."""
    from PIL import ImageFont

    def gaps(y0, y1):
        cuts = sorted((a[0], a[2]) for a in avoid or [] if a[1] < y1 and a[3] > y0 and a[2] > lo and a[0] < hi)
        out, cur = [], lo
        for a0, a1 in cuts:
            if a0 - pad > cur:
                out.append((cur, a0 - pad))
            cur = max(cur, a1 + pad)
        if hi > cur:
            out.append((cur, hi))
        return out

    yy = y
    first = True
    for _ in range(8):
        y0, y1 = yy - 8 * k, yy + size * 1.3 + 6 * k
        room = max(gaps(y0, y1), key=lambda g: g[1] - g[0], default=(lo, lo))
        if first and avoid:
            # 3.4.0 (Antakya #5): a line too long for the room on its row wraps to two lines there before it moves (lines jumped between two
            # heights as they changed); the two lines grow upward from the row, over the same gap
            first = False
            f1 = ImageFont.truetype(font_path, max(min_size, size))
            fits_one = d.textlength(text, font=ImageFont.truetype(font_path, min_size)) + 28 * k <= room[1] - room[0]
            two = None if fits_one or d.textlength(text, font=f1) + 28 * k <= room[1] - room[0] else \
                wrap_two(d, text, font_path, size, min_size, room[1] - room[0] - 28 * k)
            if two:
                s2, f2, lines, tw2 = two
                y2 = yy - s2 * 1.3                           # the second line sits on the row; the first above it
                room2 = max(gaps(y2 - 8 * k, yy + s2 * 1.3 + 6 * k), key=lambda g: g[1] - g[0], default=(lo, lo))
                if room2[1] - room2[0] >= tw2 + 28 * k:
                    mid = (room2[0] + room2[1]) / 2
                    return max(room2[0] + 14 * k, mid - tw2 / 2), y2, s2, f2, tw2, lines
        s = size
        f = ImageFont.truetype(font_path, s)
        while d.textlength(text, font=f) + 28 * k > room[1] - room[0] and s > min_size:
            s -= 1
            f = ImageFont.truetype(font_path, s)
        tw = d.textlength(text, font=f)
        if tw + 28 * k <= room[1] - room[0] or not avoid:
            mid = (room[0] + room[1]) / 2
            if room == (lo, hi):
                mid = W / 2 if centre is None else centre     # nothing in the way: centred on the frame, as before (5.7.0: the map frame's)
            return max(room[0] + 14 * k, mid - tw / 2), yy, s, f, tw, [text]
        above = [a[1] for a in avoid if a[1] < y1 and a[3] > y0]
        yy = min(above) - 6 * k - size * 1.3 - 8 * k if above else yy
        if yy < pad:
            break
    f = ImageFont.truetype(font_path, size)
    tw = d.textlength(text, font=f)
    if tw + 28 * k > W - 2 * pad:                      # 3.4.0: a line wider than the frame wraps rather than running off its edges
        two = wrap_two(d, text, font_path, size, min_size, W - 2 * pad - 28 * k)
        if two:
            s2, f2, lines, tw2 = two
            return (W - tw2) / 2, y - s2 * 1.3, s2, f2, tw2, lines
    return (W - tw) / 2, y, size, f, tw, [text]


def wrap_two(d, text, font_path, size, min_size, width):
    """The line split at the space nearest its middle, at the largest size (down to min_size) where both halves fit `width`, or None."""
    from PIL import ImageFont
    words = text.split()
    if len(words) < 3 or width <= 0:
        return None
    cuts = sorted(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    s = size
    while s >= min_size:
        f = ImageFont.truetype(font_path, s)
        for i in cuts[:3]:
            a, b = " ".join(words[:i]), " ".join(words[i:])
            tw = max(d.textlength(a, font=f), d.textlength(b, font=f))
            if tw <= width:
                return s, f, [a, b], tw
        s -= 1
    return None


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


COUNTER_FOLLOW = 0.7          # 3.4.0: the second and later counters of a corner, as a share of the first's size
COUNTER_MAX_SHARE = 0.04      # 3.4.0 (eval HIER-1): a counter's type at most this share of the frame height


def paint(img, t: float, tl: dict, tokens: dict, *, font_path: str, logo=None, bottom_px: int = 0, top_px: int = 0, avoid=None, frame_px=None):
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
        over.alpha_composite(lg, ((round(frame_px[0]) if frame_px else 0) + pad, H - bottom_px - pad - lh))     # 5.7.0: inside the map frame
        logo_w = lg.width + pad
        placed["logo"] = True
    s = story_at(tl, t)
    cf = ImageFont.truetype(font_path, max(12, min(int(26 * k), int(H * COUNTER_MAX_SHARE))))
    if s:
        text, a = s
        size = max(11, int(22 * k))
        f = ImageFont.truetype(font_path, size)
        # 1.19.0 (Day 16 issue 15): a centred story line keeps clear of the logo on the left and of a bottom-right counter on the right
        right = max((d.textlength(ct, font=cf) + pad for ct, pos in counters_at(tl, t) if pos == "bottom-right"), default=0.0)
        # 5.7.0 (a map is everything inside its border): the story line sits inside the map frame, centred on it, never over the margins
        fx0, fx1 = (float(frame_px[0]), float(frame_px[2])) if frame_px else (0.0, float(W))
        maxw = (fx1 - fx0) - 2 * (pad + max(logo_w, right))
        while d.textlength(text, font=f) > maxw and size > max(9, int(14 * k)):
            size -= 1
            f = ImageFont.truetype(font_path, size)
        tw = d.textlength(text, font=f)
        x, y = (fx0 + fx1 - tw) / 2, H - bottom_px - pad - size * 1.6
        # 5.6.0 (Q-10): every line of the story in one slot: the row the highest-placed line needs, worked out once over all the lines, so a
        # caption does not jump between heights from beat to beat
        key_ = (W, H, bottom_px, logo_w, round(fx0), round(fx1), tuple(tuple(round(v) for v in a) for a in (avoid or [])))
        if tl.get("_slot_key") != key_:
            ys_ = []
            for r_ in tl.get("story") or []:
                tx_ = str(r_.get("line") or "")
                if tx_:
                    ys_.append(story_room(d, tx_, font_path, max(11, int(22 * k)), max(9, int(14 * k)), y, avoid, pad, W, H, fx0 + pad + logo_w,
                                          fx1 - pad - right, k, centre=(fx0 + fx1) / 2)[1])
            tl["_slot_key"], tl["_slot_y"] = key_, (min(ys_) if ys_ else None)
        if tl.get("_slot_y") is not None:
            y = min(y, tl["_slot_y"])
        # 2.1.0 (Day 19 video issue 16): the line keeps clear of the map's overlays (the locator inset bottom right, the legend) as the
        # counters do: it is centred in the room left between them on its row, made smaller to fit, or moved up past them
        x, y, size, f, tw, lines = story_room(d, text, font_path, size, max(9, int(14 * k)), y, avoid, pad, W, H, fx0 + pad + logo_w, fx1 - pad - right, k,
                                              centre=(fx0 + fx1) / 2)
        lh = size * 1.3
        bx = [x - 14 * k, y - 8 * k, x + tw + 14 * k, y + lh * len(lines) + 6 * k]
        placed["story_box"] = [round(v, 1) for v in bx]
        placed["story_lines"] = len(lines)
        d.rounded_rectangle(bx, radius=int(6 * k), fill=_rgb(bgc, int(215 * a)))
        for li, ln in enumerate(lines):
            lx = x + (tw - d.textlength(ln, font=f)) / 2 if len(lines) > 1 else x
            d.text((lx, y + li * lh), ln, font=f, fill=ink[:3] + (int(255 * a),))
        placed["story"] = True
    # 3.4.0 (Antakya #4): the counters of one corner are one column, cleared past the overlays as one block and then stacked, so three
    # top-right counters no longer land on one line under the legend; the first leads, the rest are set at COUNTER_FOLLOW of its size
    # (eval STYLE-4), and the column's edge is the map frame's when it is given (eval LAYOUT-4)
    sub = ImageFont.truetype(font_path, max(10, int(cf.size * COUNTER_FOLLOW)))
    cols: dict[str, list[str]] = {}
    for text, pos in counters_at(tl, t):
        cols.setdefault(pos, []).append(text)
    right_edge = (frame_px[2] if frame_px else W) - pad
    left_edge = (frame_px[0] if frame_px else 0) + pad
    for pos, texts in cols.items():
        fonts_ = [cf] + [sub] * (len(texts) - 1)
        widths = [d.textlength(tx, font=fo) for tx, fo in zip(texts, fonts_)]
        heights = [fo.size * 1.45 for fo in fonts_]
        bw, bh = max(widths), sum(heights)
        x0 = left_edge if pos.endswith("left") else right_edge - bw
        if pos.startswith("top"):
            y0 = top_px + pad
        else:
            y0 = H - bottom_px - pad - bh - (int(44 * k) + pad if pos == "bottom-left" and placed["logo"] else 0)
        x0, y0, _x1, _y1 = _clear((x0, y0, x0 + bw, y0 + bh), avoid, pos.startswith("top"))
        if tl.get("counter_panel", True) is not False:
            # 5.5.0 (D26-30): a panel in the page colour under the counter column, so a place name of the base map under it leaves no
            # ghost glyphs between the words ("Day 7 'at sea")
            d.rounded_rectangle([x0 - 8 * k, y0 - 4 * k, x0 + bw + 8 * k, y0 + bh + 2 * k], radius=int(5 * k), fill=_rgb(bgc, 228))
        y = y0
        for tx, fo, w_, h_ in zip(texts, fonts_, widths, heights):
            x = x0 if pos.endswith("left") else x0 + bw - w_
            d.text((x, y), tx, font=fo, fill=ink, stroke_width=max(2, int(fo.size * 0.12)), stroke_fill=_rgb(bgc))
            y += h_
            placed["counters"] += 1
        placed.setdefault("counter_boxes", {})[pos] = [round(x0, 1), round(y0, 1), round(x0 + bw, 1), round(y0 + bh, 1)]
    return Image.alpha_composite(im, over).convert("RGB"), placed


def dumps(tl: dict) -> str:
    """The checked timeline as JSON (for the report and the manifest's hash)."""
    return json.dumps({k: v for k, v in tl.items() if k != "findings"}, sort_keys=True, default=str)
