#!/usr/bin/env python3
"""flythrough: a video flown over a 3D web page (1.6.0, R4-FLY).

    map_export.py --flythrough PAGE.html --out DIR [--path LAYER | --chapters | --orbit] [--duration 32] [--fps 25]
                  [--size 1280x720 | 1920x1080 | 1080x1350] [--marker LAYER] [--hud on|off] [--quality draft|final] [--resume]

A flythrough always runs through map-run (the `flythrough` pipeline): its cost card shows the frame count and the estimate from a 10-frame
warm-up, the draft (640 x 360 at 12.5 fps, about 8 times faster) is offered first, and the final render runs after the person looks at the draft.

Camera (pure functions of the frame number, unit-tested):
  path      the line layer densified by distance; the camera behind the moving point on the path's smoothed tangent (a moving average over
            smooth_m, 400 m), at the page's pitch and a zoom that keeps the next lookahead_m in view; constant speed, or by a time_field
  chapters  eased flights between the story chapters' views, with a hold at each
  orbit     a bearing sweep around the centre
  slider    1.14.0 (Day 11 issue 14): a time-lapse of the page's time slider: the camera holds the page's own view and each period is shown for
            an equal share of the run (the last held a little longer), its label in the HUD; a 2D or 3D MapLibre page
  keyframes 1.16.0 (Day 15 issue 1): the timeline's camera keyframes (--timeline T.yaml, or --keyframes cam.json for keyframes alone): a free
            camera (eye lon/lat/height above the ground and the point it looks at, through MapLibre's calculateCameraOptionsFromTo) with its
            field of view, or the page's own center/zoom/pitch/bearing; pitch up to 115 degrees; the layers and extrusion opacity each keyframe
            sets (a see-through roof). The eye never goes under 6 m above the ground (issue 12).
1.16.0 for every mode: the layers are the page's data layers at the first frame, not the first story chapter's (issue 4); --chapters shows
each chapter's own layers. The HUD draws the brand logo bottom left above the credits (issue 13: the capture hides the page's controls and the
logo with them), the timeline's story line and counters, and --poster first|last opens the video on a held frame that dissolves into the
flight (issue 15: X shows the first frame as the thumbnail).
Capture: Playwright Chromium on the page in capture mode (?gm-capture=1: no controls, the drawing buffer kept, one device pixel per CSS pixel,
no fades). Per frame map.jumpTo(camera), wait for `idle`, screenshot. Software GL is Mesa llvmpipe (--use-angle=gl-egl), SwiftShader the
fallback; `gl: gpu` asks for the hardware GPU (a PC). The moving marker is a DOM element placed with map.project, never a GeoJSON update.
The HUD (title, clock, distance, the provider credits) is composited in Python with Pillow and Inter, never read from WebGL; a required credit
the compositor cannot place is MAP-OUT-020 (BLOCK). Frames are numbered PNGs with a manifest (a camera hash per frame); --resume keeps those
already done. Encoding: ffmpeg H.264 -preset veryfast, CRF 20, yuv420p, even dimensions, +faststart; a GIF too for loops of 10 s or less.
The hidden accreditation goes into the MP4 metadata, never into a frame.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import timeline as tlm  # noqa: E402

SIZES = {"1280x720": (1280, 720), "1920x1080": (1920, 1080), "1080x1350": (1080, 1350)}
DRAFT = {"size": (640, 360), "fps": 12.5}
R = 6378137.0
FOV = math.radians(36.8699)
GIF_MAX_S = 10.0
SLIDER_LAST_HOLD = 0.08          # a slider time-lapse holds its last period for this share of the frames
WARMUP = 10
# 1.6.2: "gpu" names the platform's own ANGLE back end. 1.6.0's flags (--enable-gpu-rasterization) left headless Chromium on SwiftShader on
# Windows (measured on a workstation: SwiftShader with them, the AMD Radeon through Direct3D 11 with --use-angle=d3d11). "auto": the GPU on Windows and
# macOS, Mesa llvmpipe on Linux (the cloud workspace has no GPU).
GPU_ARGS = {"win32": ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"],
            "darwin": ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"],
            "linux": ["--use-angle=gl", "--enable-gpu", "--ignore-gpu-blocklist"]}
GL_ARGS = {"llvmpipe": ["--use-angle=gl-egl", "--use-gl=angle", "--ignore-gpu-blocklist"],
           "swiftshader": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
           "gpu": GPU_ARGS.get(sys.platform, GPU_ARGS["linux"])}
SOFTWARE_GL = re.compile(r"swiftshader|llvmpipe|softpipe|basic render|microsoft basic|warp", re.I)
LOGO_JS = """async () => { const el = document.querySelector('svg.gm-brand-logo') || document.querySelector('svg.gm-logo'); if (!el) return null;
  const s = new XMLSerializer().serializeToString(el), img = new Image(); img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s);
  await img.decode(); const h = 176, w = Math.max(1, Math.round(h * (img.width || 1) / (img.height || 1)));
  const c = document.createElement('canvas'); c.width = w; c.height = h; c.getContext('2d').drawImage(img, 0, 0, w, h); return c.toDataURL('image/png'); }"""
RENDERER_JS = """() => { const g = document.createElement('canvas').getContext('webgl2') || document.createElement('canvas').getContext('webgl');
  if (!g) return null; const e = g.getExtension('WEBGL_debug_renderer_info');
  return String(e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER)); }"""


def gl_auto(gl: str) -> str:
    """The GL route to use: `auto` is the GPU on Windows and macOS, llvmpipe elsewhere."""
    return ("gpu" if sys.platform in ("win32", "darwin") else "llvmpipe") if gl == "auto" else gl


class FlyError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


# ------------------------------------------------------------------------------------------------------------------- geometry helpers
def _metres(a, b) -> float:
    """Ground distance between two lon/lat points (haversine)."""
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def _bearing(a, b) -> float:
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def densify(coords: list, step_m: float = 20.0) -> tuple[list, list]:
    """The line resampled every step_m metres: (points, cumulative distances in metres)."""
    pts, cum = [tuple(coords[0])], [0.0]
    for a, b in zip(coords, coords[1:]):
        d = _metres(a, b)
        n = max(1, int(math.ceil(d / step_m)))
        for k in range(1, n + 1):
            t = k / n
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            cum.append(cum[-1] + d / n)
    return pts, cum


def _at(pts: list, cum: list, s: float) -> tuple:
    """The point at distance s along the densified line."""
    import bisect
    s = max(0.0, min(cum[-1], s))
    i = max(1, bisect.bisect_left(cum, s))
    i = min(i, len(cum) - 1)
    d = cum[i] - cum[i - 1]
    t = 0.0 if d <= 0 else (s - cum[i - 1]) / d
    a, b = pts[i - 1], pts[i]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def zoom_for_span(span_m: float, lat: float, px: float) -> float:
    """The MapLibre zoom (512-pixel tiles) at which span_m metres fill px pixels at latitude lat."""
    return math.log2(2 * math.pi * R * math.cos(math.radians(lat)) * px / (512 * max(span_m, 1.0)))


def zoom_for_bbox(bbox, size) -> float:
    w, s, e, n = bbox
    lat = (s + n) / 2
    zx = zoom_for_span(max(1.0, _metres((w, lat), (e, lat))), lat, size[0] * 0.9)
    zy = zoom_for_span(max(1.0, _metres(((w + e) / 2, s), ((w + e) / 2, n))), lat, size[1] * 0.9)
    return min(zx, zy)


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _lerp_bearing(a: float, b: float, t: float) -> float:
    d = ((b - a + 540.0) % 360.0) - 180.0
    return (a + d * t) % 360.0


# ------------------------------------------------------------------------------------------------------------------- cameras
def rail(pts: list, cum: list, window_m: float) -> list:
    """1.6.2 -> 1.6.3: the camera's rail, the densified line averaged over window_m (a moving mean of the 20 m points, the ends held), so the
    camera rides the route's general direction and not each bend of the road; the marker stays on the line itself."""
    import numpy as np
    a = np.asarray(pts, dtype=float)
    step = (cum[-1] / max(1, len(cum) - 1)) or 20.0
    k = int(max(1, round(window_m / step / 2)))
    if len(a) < 3 or k < 1:
        return [tuple(p) for p in a]
    pad = np.vstack([np.repeat(a[:1], k, axis=0), a, np.repeat(a[-1:], k, axis=0)])
    c = np.cumsum(np.vstack([np.zeros((1, 2)), pad]), axis=0)
    m = (c[2 * k + 1:] - c[:-2 * k - 1]) / (2 * k + 1)
    return [tuple(p) for p in m]


def path_camera(i: int, n: int, line: list, *, pitch: float, size=(1280, 720), smooth_m: float = 400.0, lookahead_m: float = 1500.0,
                times: list | None = None, rail_m: float | None = None, _cache: dict | None = None) -> dict:
    """The camera of frame i of n following a line (lon/lat): the marker at a constant speed (or by `times`, one value per vertex), the
    camera centred lookahead/3 ahead of it on the rail (the line averaged over rail_m, by default the larger of smooth_m and lookahead_m),
    the bearing the rail's direction over the lookahead, the zoom that keeps lookahead_m in view. cameras() then steadies the sequence."""
    rail_m = float(rail_m if rail_m is not None else max(smooth_m, lookahead_m))
    key = (id(line), rail_m)
    if _cache is not None and key in _cache:
        pts, cum, tcum, rl = _cache[key]
    else:
        pts, cum = densify(line)
        rl = rail(pts, cum, rail_m)
        tcum = None
        if times:
            vc, acc = [0.0], 0.0                      # vertex distances, to map each vertex's time to a distance
            for a, b in zip(line, line[1:]):
                acc += _metres(a, b)
                vc.append(acc)
            tcum = (list(map(float, times)), vc)
        if _cache is not None:
            _cache[key] = (pts, cum, tcum, rl)
    f = 0.0 if n <= 1 else i / (n - 1)
    if tcum:
        ts, vc = tcum
        t = ts[0] + (ts[-1] - ts[0]) * f
        import bisect
        j = min(max(1, bisect.bisect_left(ts, t)), len(ts) - 1)
        u = 0.0 if ts[j] == ts[j - 1] else (t - ts[j - 1]) / (ts[j] - ts[j - 1])
        s = vc[j - 1] + (vc[j] - vc[j - 1]) * u
    else:
        s = cum[-1] * f
    marker = _at(pts, cum, s)
    half = max(smooth_m, lookahead_m) / 2
    behind, ahead = _at(rl, cum, s - half), _at(rl, cum, s + half)
    bearing = _bearing(behind, ahead) if behind != ahead else 0.0
    centre = _at(rl, cum, s + lookahead_m / 3)
    zoom = zoom_for_span(lookahead_m, centre[1], size[1] * 0.75)
    return {"center": [round(centre[0], 7), round(centre[1], 7)], "zoom": round(zoom, 4), "bearing": round(bearing, 3), "pitch": float(pitch),
            "marker": [round(marker[0], 7), round(marker[1], 7)], "distance_m": round(s, 1), "fraction": round(f, 6)}


def chapter_camera(i: int, n: int, views: list[dict], *, hold_share: float = 0.35) -> dict:
    """Eased flights between chapter views ({center, zoom, pitch, bearing}); each chapter holds for hold_share of its segment."""
    if len(views) == 1 or n <= 1:
        v = views[0]
        return {k: v[k] for k in ("center", "zoom", "pitch", "bearing")} | {"chapter": 0}
    seg = (n - 1) / (len(views) - 1)
    k = min(len(views) - 2, int(i // seg))
    t = (i - k * seg) / seg
    t = 0.0 if t < hold_share else _ease(min(1.0, (t - hold_share) / (1 - hold_share)))
    a, b = views[k], views[k + 1]
    cen = [a["center"][0] + (b["center"][0] - a["center"][0]) * t, a["center"][1] + (b["center"][1] - a["center"][1]) * t]
    return {"center": [round(cen[0], 7), round(cen[1], 7)], "zoom": round(a["zoom"] + (b["zoom"] - a["zoom"]) * t, 4),
            "pitch": round(a["pitch"] + (b["pitch"] - a["pitch"]) * t, 3), "bearing": round(_lerp_bearing(a["bearing"], b["bearing"], t), 3),
            "chapter": k + (1 if t >= 1.0 else 0)}


def orbit_camera(i: int, n: int, centre, zoom: float, pitch: float, *, start: float = 0.0, sweep: float = 360.0) -> dict:
    f = 0.0 if n <= 1 else i / n                      # a closed loop: the last frame is one step short of the first
    return {"center": [round(centre[0], 7), round(centre[1], 7)], "zoom": round(zoom, 4), "pitch": float(pitch),
            "bearing": round((start + sweep * f) % 360.0, 3)}


def camera_hash(cam: dict) -> str:
    return hashlib.sha256(json.dumps({k: cam[k] for k in sorted(cam)}, sort_keys=True).encode()).hexdigest()[:16]


def views_of(cfg: dict, size) -> list[dict]:
    """The story chapters' views as cameras (a bbox view becomes the centre and zoom that fit it)."""
    out = []
    p0 = float(((cfg.get("ml") or {}).get("view3d") or {}).get("pitch") or 0.0)
    for ch in ((cfg.get("story") or {}).get("chapters") or []):
        v = ch.get("view") or {}
        if v.get("center") is not None and v.get("zoom") is not None:
            c, z = v["center"], float(v["zoom"]) - 1                  # the page's zooms are one higher than MapLibre's
        else:
            bb = v.get("bbox") or cfg["bounds"]
            c, z = [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2], zoom_for_bbox(bb, size)
        out.append({"center": list(c), "zoom": z, "pitch": float(v.get("pitch", p0)), "bearing": float(v.get("bearing", 0.0)),
                    "layers": ch.get("layers")})
    return out


def line_of(data: dict, layer: str) -> tuple[list, list | None]:
    """The coordinates of a page layer's lines joined in order (a MultiLineString's parts end to end), and its time_field values if any."""
    feats = (data.get(layer) or {}).get("features") or []
    if not feats:
        raise FlyError(f"layer '{layer}' has no features on the page (a tiled layer cannot be flown; give a line layer the page carries)", gc.EXIT_USAGE)
    coords = []
    for f in feats:
        g = f.get("geometry") or {}
        parts = [g["coordinates"]] if g.get("type") == "LineString" else (g.get("coordinates") or []) if g.get("type") == "MultiLineString" else []
        for p in parts:
            if coords and tuple(coords[-1]) == tuple(p[0]):
                p = p[1:]
            coords += [tuple(c[:2]) for c in p]
    if len(coords) < 2:
        raise FlyError(f"layer '{layer}' is not a line layer", gc.EXIT_USAGE)
    return coords, None


STEADY_S = 2.0          # 1.6.3: the camera's bearing, centre and zoom are averaged over this many seconds (a Gaussian, sigma a third of it)
MAX_TURN = 30.0         # 1.6.3: and never turn faster than this many degrees a second


def steady(cams: list[dict], fps: float, *, steady_s: float = STEADY_S, max_turn: float = MAX_TURN) -> list[dict]:
    """1.6.3: a path camera that does not shake. Each frame's bearing (unwrapped), centre and zoom are averaged over steady_s seconds of the
    flight, then the bearing is held to max_turn degrees a second (a forward and a backward pass, so the limit is symmetric). The marker is
    left where it is. Still a pure function of the frame numbers: the same flight gives the same frames, so --resume keeps them."""
    import numpy as np
    n = len(cams)
    if n < 3 or steady_s <= 0:
        return cams
    sig = max(0.5, steady_s * fps / 3.0)
    r = int(math.ceil(3 * sig))
    w = np.exp(-0.5 * (np.arange(-r, r + 1) / sig) ** 2)
    w /= w.sum()

    def smooth(v):
        v = np.asarray(v, dtype=float)
        pad = np.concatenate([np.repeat(v[:1], r), v, np.repeat(v[-1:], r)])
        return np.convolve(pad, w, mode="valid")

    br = np.degrees(np.unwrap(np.radians([c["bearing"] for c in cams])))
    br = smooth(br)
    lim = max_turn / fps if max_turn and max_turn > 0 else None
    if lim:
        for _ in range(2):
            for i in range(1, n):
                br[i] = br[i - 1] + max(-lim, min(lim, br[i] - br[i - 1]))
            for i in range(n - 2, -1, -1):
                br[i] = br[i + 1] + max(-lim, min(lim, br[i] - br[i + 1]))
    cx = smooth([c["center"][0] for c in cams])
    cy = smooth([c["center"][1] for c in cams])
    zz = smooth([c["zoom"] for c in cams])
    out = []
    for i, c in enumerate(cams):
        out.append(dict(c, bearing=round(float(br[i]) % 360.0, 3), center=[round(float(cx[i]), 7), round(float(cy[i]), 7)], zoom=round(float(zz[i]), 4)))
    return out


def line_periods(data: dict, layer: str) -> list | None:
    """The time period (`_t`) of each vertex of line_of(data, layer), the period of the feature whose segment ends there; None when the
    layer's features carry no period (1.6.2: the page's time slider follows the marker)."""
    feats = (data.get(layer) or {}).get("features") or []
    if not feats or not any("_t" in (f.get("properties") or {}) for f in feats):
        return None
    out, last = [], None
    for f in feats:
        g = f.get("geometry") or {}
        tv = (f.get("properties") or {}).get("_t")
        parts = [g["coordinates"]] if g.get("type") == "LineString" else (g.get("coordinates") or []) if g.get("type") == "MultiLineString" else []
        for p in parts:
            if out and last is not None and tuple(last) == tuple(p[0]):
                p = p[1:]
            out += [tv] * len(p)
            if p:
                last = p[-1]
    return out


def with_periods(cams: list[dict], mode: str, ctx: dict, layer: str | None) -> list[dict]:
    """1.6.2: each camera's page time period. Following a timed line, the period of the stretch under the marker (so a course draws in as
    the marker runs along it, and the posts appear as it reaches them); otherwise, on a cumulative time page, the last period (everything
    drawn). A page without a time slider is left as it is."""
    T = (ctx.get("cfg") or {}).get("time")
    if not T or not T.get("labels") or mode == "slider":          # a slider time-lapse sets its own period per frame
        return cams
    last = len(T["labels"]) - 1
    per = line_periods(ctx["data"], layer) if (mode == "path" and layer) else None
    if per:
        line, _ = line_of(ctx["data"], layer)
        vc, acc = [0.0], 0.0
        for a, b in zip(line, line[1:]):
            acc += _metres(a, b)
            vc.append(acc)
        import bisect
        for c in cams:
            j = min(max(1, bisect.bisect_left(vc, c["distance_m"])), len(vc) - 1)
            v = per[j]
            c["time"] = int(v) if isinstance(v, (int, float)) else last
        return cams
    if T.get("mode") == "cumulative":
        for c in cams:
            c["time"] = last
    return cams


def data_layers(ctx: dict) -> list[str]:
    """The page's data layers (not the context or relief layers): what every mode but --chapters shows from the first frame (1.16.0)."""
    return [d["id"] for d in (ctx["cfg"].get("layers") or []) if not d.get("context") and d.get("role") != "hillshade"]


def keyframe_cameras(n: int, fps: float, tl: dict) -> list[dict]:
    """1.16.0: frame i's camera from the timeline's keyframes at t = i / fps."""
    out = []
    for i in range(n):
        c = tlm.camera_at(tl, i / fps)
        if c["free"]:
            out.append({"free": True, "camera": c["camera"], "target": c["target"], "fov": c["fov"], "layers": c["layers"], "opacity": c["opacity"]})
        else:
            out.append({"center": c["center"], "zoom": round(c["zoom"] - 1, 4), "pitch": c["pitch"], "bearing": c["bearing"], "fov": c["fov"],
                        "layers": c["layers"], "opacity": c["opacity"]})
    return out


def with_layers(cams: list[dict], mode: str, ctx: dict) -> list[dict]:
    """1.16.0 (Day 15 issue 4): the layer set each frame shows. The capture used to start on the first story chapter's set whatever the mode;
    now a chapter flight shows each chapter's layers, a keyframe its own (or the last one set), and the rest the page's data layers."""
    every = data_layers(ctx)
    if mode == "chapters":
        views = views_of(ctx["cfg"], (1280, 720))
        for c in cams:
            lay = views[min(len(views) - 1, int(c.get("chapter", 0)))].get("layers") if views else None
            c["layers"] = lay if lay is not None else every
        return cams
    for c in cams:
        if c.get("layers") is None:
            c["layers"] = every
    return cams


def cameras(mode: str, n: int, ctx: dict, *, size, layer: str | None = None, pitch: float | None = None, smooth_m: float = 400.0,
            lookahead_m: float = 1500.0, fps: float = 25.0, steady_s: float = STEADY_S, max_turn: float = MAX_TURN, tl: dict | None = None) -> list[dict]:
    cfg = ctx["cfg"]
    if mode == "keyframes":
        if not tl or not tl.get("keyframes"):
            raise FlyError("--keyframes needs a timeline with keyframes (keyframes: [{t, camera, target} or {t, center, zoom}])", gc.EXIT_USAGE)
        return keyframe_cameras(n, fps, tl)
    v3 = (cfg.get("ml") or {}).get("view3d") or {}
    p = float(pitch if pitch is not None else v3.get("pitch") or 60.0)
    if mode == "path":
        line, times = line_of(ctx["data"], layer)
        cache: dict = {}
        cams = [path_camera(i, n, line, pitch=p, size=size, smooth_m=smooth_m, lookahead_m=lookahead_m, times=times, _cache=cache) for i in range(n)]
        return steady(cams, fps, steady_s=steady_s, max_turn=max_turn)
    if mode == "slider":
        T = cfg.get("time") or {}
        P = len(T.get("labels") or [])
        if P < 2:
            raise FlyError("the page has no time slider with two or more periods to play (webmap.time)", gc.EXIT_USAGE)
        b = cfg["bounds"]
        c = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
        z = zoom_for_bbox(b, size)
        hold_last = max(1, round(n * SLIDER_LAST_HOLD))                   # the last period stays on a little longer
        per = max(1, n - hold_last)
        return [{"center": [round(c[0], 7), round(c[1], 7)], "zoom": round(z, 4), "pitch": float(v3.get("pitch") or 0.0) if pitch is None else float(pitch),
                 "bearing": float(v3.get("bearing") or 0.0), "time": min(P - 1, int(i * P / per)) if i < per else P - 1} for i in range(n)]
    if mode == "chapters":
        views = views_of(cfg, size)
        if not views:
            raise FlyError("the page has no story chapters to fly between", gc.EXIT_USAGE)
        return [chapter_camera(i, n, views) for i in range(n)]
    b = cfg["bounds"]
    c = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
    return [orbit_camera(i, n, c, zoom_for_bbox(b, size), p, start=float(v3.get("bearing") or 0.0)) for i in range(n)]


# ------------------------------------------------------------------------------------------------------------------- HUD
def _inter_ttf(weight: int) -> Path:
    """Inter at a weight as a TTF Pillow can read (from the bundled variable WOFF2), kept in the work folder."""
    out = gc.workdir(create=True) / "fonts" / f"inter-{weight}.ttf"
    if out.is_file():
        return out
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    f = TTFont(str(gc.plugin_root() / "assets" / "fonts" / "inter" / "inter-latin-wght-normal.woff2"))
    f = instancer.instantiateVariableFont(f, {"wght": weight})
    f.flavor = None
    out.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(out))
    return out


def hud(img, *, title: str | None, lines: list[str], credits: str, tokens: dict, info: dict | None = None, date_text: str | None = None):
    """The overlay drawn on a frame: the title top left, the clock and distance lines under it, the credits along the bottom on a band, and
    (1.19.0, Day 16 issue 14) the date at the band's right end in the credits' face, as the print map's footer carries it.
    -> (image, placed: {title, lines, credits, date: bool})."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = img.size
    k = H / 720.0
    big = ImageFont.truetype(str(_inter_ttf(700)), max(12, int(30 * k)))
    small = ImageFont.truetype(str(_inter_ttf(500)), max(10, int(18 * k)))
    cred_size = max(9, int(13 * k))
    im = img.convert("RGBA")
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)

    def rgb(hx, a=255):
        hx = hx.lstrip("#")
        return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4)) + (a,)
    ink, bg = rgb(tokens.get("label") or tokens.get("emphasis")), rgb(tokens.get("background") or tokens.get("legend_bg"), 215)
    x, y = int(24 * k), int(20 * k)
    placed = {"title": False, "lines": False, "credits": False}
    info = info if info is not None else {}
    info.update(band_px=0, top_px=0)
    blocks = ([(title, big)] if title else []) + [(ln, small) for ln in lines if ln]
    if blocks:
        wmax = max(d.textlength(t, font=f) for t, f in blocks)
        hsum = sum(f.size * 1.3 for _t, f in blocks)
        d.rounded_rectangle([x - 12 * k, y - 8 * k, x + wmax + 12 * k, y + hsum + 6 * k], radius=int(6 * k), fill=bg)
        for t, f in blocks:
            d.text((x, y), t, font=f, fill=ink)
            y += int(f.size * 1.3)
        placed["title"] = bool(title)
        placed["lines"] = any(lines)
        info["top_px"] = int(y + 6 * k)
    if date_text:
        placed["date"] = False
    if credits or date_text:
        size = cred_size
        df = ImageFont.truetype(str(_inter_ttf(400)), size)
        dw = (d.textlength(date_text, font=df) + 24 * k) if date_text else 0.0
        cf = df
        while credits and size >= max(7, int(8 * k)):
            cf = ImageFont.truetype(str(_inter_ttf(400)), size)
            if d.textlength(credits, font=cf) <= W - 32 * k - dw:
                break
            size -= 1
        fits = (not credits) or d.textlength(credits, font=cf) <= W - 32 * k - dw
        if not fits and date_text:                   # the credits come first: the date steps aside when both do not fit
            date_text, dw = None, 0.0
            cf = df
            fits = d.textlength(credits, font=cf) <= W - 32 * k
        if fits:
            band = int(max(size, cred_size if not credits else size) * 1.9)
            d.rectangle([0, H - band, W, H], fill=bg)
            if credits:
                d.text((int(16 * k), H - band + int(size * 0.45)), credits, font=cf, fill=ink)
                placed["credits"] = True
            if date_text:
                df = ImageFont.truetype(str(_inter_ttf(400)), size)
                d.text((W - int(16 * k) - d.textlength(date_text, font=df), H - band + int(size * 0.45)), date_text, font=df, fill=ink)
                placed["date"] = True
            info["band_px"] = band
    return Image.alpha_composite(im, over).convert("RGB"), placed


def date_line(tl: dict) -> str | None:
    """The timeline's date: auto (today, or GMAP_FIXED_DATE, written "16 November 2026"), a text of its own, or off."""
    import datetime as _dt
    import os
    v = tl.get("date", "auto")
    if v in (None, False, "off", "none", ""):
        return None
    if v != "auto":
        return str(v)
    fixed = os.environ.get("GMAP_FIXED_DATE")
    day = _dt.date.fromisoformat(fixed) if fixed else _dt.date.today()
    return f"{day.day} {day.strftime('%B')} {day.year}"


# ------------------------------------------------------------------------------------------------------------------- capture
def playwright_ok() -> str | None:
    try:
        import playwright.sync_api  # noqa: F401
        return None
    except Exception:  # noqa: BLE001
        return "the flythrough needs Playwright and its Chromium: pip install playwright, then python -m playwright install chromium"


class Capture:
    """One Chromium page in capture mode; frame(cam) jumps, waits for idle, places the marker and returns the PNG bytes."""

    def __init__(self, page_html: Path, size, gl: str = "auto", timeout_s: float = 60.0, hooks: dict | None = None):
        from playwright.sync_api import sync_playwright
        self.hooks = hooks or {}
        self._pw = sync_playwright().start()
        gl = gl_auto(gl)
        args = GL_ARGS.get(gl, GL_ARGS["llvmpipe"])
        try:
            self.browser = self._pw.chromium.launch(args=args)
        except Exception:  # noqa: BLE001 - SwiftShader when the EGL route is not there
            self.browser = self._pw.chromium.launch(args=GL_ARGS["swiftshader"])
            gl = "swiftshader"
        self.gl = gl
        self.renderer = None
        # the page's Content-Security-Policy allows no eval; the capture's own waits are evaluated by Playwright, so CSP is bypassed for it
        self.page = self.browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1, bypass_csp=True)
        self.page.goto(Path(page_html).resolve().as_uri() + "?gm-capture=1", wait_until="load")
        self.page.wait_for_function("window.__gmMap && window.__gmMap.loaded()", timeout=timeout_s * 1000)
        self.timeout_s = timeout_s
        # 1.19.0 (Day 16 issue 17): MapLibre reports a bad layer or filter as an error event, not an exception; the capture collects them
        self.page.evaluate("() => { window.__gmErrors = []; window.__gmMap.on('error', (e) => window.__gmErrors.push(String((e && e.error && e.error.message) || e))); }")
        if self.hooks.get("ready"):              # 1.19.0: the capture's own on_ready, run once the page has settled (its restyle is done)
            self.page.evaluate("() => new Promise((r) => { const m = window.__gmMap; if (m.isStyleLoaded() && !m.isMoving()) r(); else m.once('idle', () => r()); })")
            self.page.evaluate(f"(map) => {{ {self.hooks['ready']} }}", None)
        try:                                     # 1.6.2: which renderer WebGL got, so a GPU run that fell back to software says so
            self.renderer = self.page.evaluate(RENDERER_JS)
        except Exception:  # noqa: BLE001
            self.renderer = None

    def context(self) -> dict:
        ctx = self.page.evaluate("() => ({cfg: window.__gmCtx.cfg, data: window.__gmCtx.data, credits: window.__gmCtx.credits(), title: document.title, "
                                 "can_time: typeof window.__gmCtx.setTime === 'function', can_layers: typeof window.__gmCtx.setLayers === 'function'})")
        try:                                     # 1.16.0: the brand logo the page shows, drawn by the browser into a PNG for the HUD
            ctx["logo_png"] = self.page.evaluate(LOGO_JS)
        except Exception:  # noqa: BLE001
            ctx["logo_png"] = None
        return ctx

    def errors(self) -> list[str]:
        try:
            return list(self.page.evaluate("() => (window.__gmErrors || []).splice(0)") or [])
        except Exception:  # noqa: BLE001
            return []

    def frame(self, cam: dict, marker: bool) -> bytes:
        if self.hooks.get("frame"):              # 1.19.0: on_frame(map, cam) before the camera moves (a layer set per frame)
            self.page.evaluate(f"(cam) => {{ const map = window.__gmMap; {self.hooks['frame']} }}", cam)
        self.page.evaluate("""(c) => new Promise((res) => {
            const m = window.__gmMap;
            if (c.time != null && window.__gmCtx.setTime) window.__gmCtx.setTime(c.time);
            if (c.layers && window.__gmCtx.setLayers) {                                  // 1.16.0: the frame's layer set, set when it changes
              const key = c.layers.join('|'); if (window.__gmFlyLayers !== key) { window.__gmCtx.setLayers(c.layers); window.__gmFlyLayers = key; } }
            if (c.chapter != null && window.__gmCtx.storyHighlight && window.__gmFlyChapter !== c.chapter) {     // 2.0.0: the beat's highlight
              window.__gmCtx.storyHighlight(c.chapter); window.__gmFlyChapter = c.chapter; }
            if (c.opacity) Object.keys(c.opacity).forEach((id) => {                     // a factor of the layer's own opacity, as its slider
              if (window.__gmCtx.setOpacity) { window.__gmCtx.setOpacity(id, c.opacity[id]); return; }
              (m.getStyle().layers || []).forEach((l) => { if (l.type === 'fill-extrusion' && l.metadata && l.metadata['gm:layer'] === id)
                m.setPaintProperty(l.id, 'fill-extrusion-opacity', c.opacity[id]); }); });
            if (c.grow) (m.getStyle().layers || []).forEach((l) => {                       // 1.19.0: each block blended between two moments
              if (l.type !== 'fill-extrusion' || !l.metadata || l.metadata['gm:layer'] !== c.grow.layer) return;
              const g = c.grow, at = (n) => ['to-number', ['get', '_m' + n], 0];
              const v = ['+', ['*', 1 - g.f, at(g.i)], ['*', g.f, at(g.j)]];
              m.setPaintProperty(l.id, 'fill-extrusion-height', ['*', g.k, v]);
              if (g.classes) {                                                             // 1.20.0: each block in its class at that moment
                const st = ['step', v, g.classes.colors[0]];
                g.classes.edges.forEach((e, n) => { st.push(e, g.classes.colors[n + 1]); });
                m.setPaintProperty(l.id, 'fill-extrusion-color', st); } });
            if (c.fov && m.setVerticalFieldOfView) m.setVerticalFieldOfView(c.fov);
            if (c.free) {                                                                  // 1.16.0: a free camera, heights above the ground
              if (m.getMaxPitch() < 115) m.setMaxPitch(115);
              const ground = (ll) => { try { return m.queryTerrainElevation(ll) || 0; } catch (e) { return 0; } };
              const eye = [c.camera[0], c.camera[1]], at = [c.target[0], c.target[1]];
              const o = m.calculateCameraOptionsFromTo(eye, ground(eye) + Math.max(6, c.camera[2]), at, ground(at) + (c.target[2] || 0));
              if (o.zoom > m.getMaxZoom()) m.setMaxZoom(Math.min(24, o.zoom + 0.5));          // the camera is not held back by the page's zoom cap
              m.jumpTo(o);
            } else {
              if (c.pitch > m.getMaxPitch()) m.setMaxPitch(Math.min(115, c.pitch));
              m.jumpTo({center: c.center, zoom: c.zoom, bearing: c.bearing, pitch: c.pitch});
            }
            let mk = document.getElementById('gm-fly-marker');
            if (c.marker) {
              if (!mk) { mk = document.createElement('div'); mk.id = 'gm-fly-marker';
                mk.setAttribute('style', 'position:fixed;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;border:3px solid #fff;' +
                  'background:' + (window.__gmCtx.cfg.tokens.emphasis || 'red') + ';z-index:2147483646;pointer-events:none'); document.body.appendChild(mk); }
              const p = m.project(c.marker); mk.style.left = p.x + 'px'; mk.style.top = p.y + 'px';
            }
            const done = () => requestAnimationFrame(() => res(true));
            if (m.loaded() && !m.isMoving() && m.areTilesLoaded()) { m.once('idle', done); m.triggerRepaint(); } else { m.once('idle', done); }
        })""", {**cam, "marker": cam.get("marker") if marker else None})
        return self.page.screenshot(type="png")

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


# ------------------------------------------------------------------------------------------------------------------- encoding
WEB_MB = 15.0                     # 1.19.0 (Day 16 issue 12): an artifact or a post takes a file up to about this size
WEB_CRFS = (26, 28, 30, 32)


AUDIO_TYPES = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")


def encode(frames_dir: Path, out: Path, fps: float, meta: dict, crf: int = 20, audio=None, seconds: float | None = None) -> dict:
    """2.0.0: `audio` is the person's own narration or sound file, laid under the frames as AAC from the first frame; the video keeps its own
    length (`seconds`): a longer recording is cut at the last frame, a shorter one ends in silence."""
    import animate
    exe = animate.ffmpeg_exe()
    if not exe:
        raise FlyError(animate.FFMPEG_MISSING.replace("; the GIF was written without it", ""), gc.EXIT_RUNTIME)
    cmd = [exe, "-y", "-loglevel", "error", "-framerate", f"{fps:g}", "-i", str(frames_dir / "frame_%05d.png")]
    if audio:
        cmd += ["-i", str(audio)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(int(crf)), "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-r", f"{fps:g}", "-movflags", "+faststart"]
    if audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "160k"] + (["-t", f"{seconds:.3f}"] if seconds else [])
    for k, v in meta.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(out))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FlyError(f"ffmpeg failed: {r.stderr.strip()[:300]}")
    return {"path": str(out), "bytes": out.stat().st_size}


def web_copy(frames_dir: Path, mp4: Path, fps: float, meta: dict, cap_mb: float = WEB_MB) -> dict | None:
    """A `<stem>_web.mp4` beside an MP4 over `cap_mb`: the same frames at CRF 26, then 28, 30 and 32, until one is under the cap. None when
    the MP4 is already under it (or the cap is 0)."""
    if not cap_mb or mp4.stat().st_size <= cap_mb * 1e6:
        return None
    out = mp4.with_name(mp4.stem + "_web.mp4")
    r = None
    for crf in WEB_CRFS:
        r = encode(frames_dir, out, fps, meta, crf=crf)
        r["crf"] = crf
        if r["bytes"] <= cap_mb * 1e6:
            break
    return r


def write_gif(frames_dir: Path, out: Path, fps: float, n: int, comment: str = "") -> dict:
    from PIL import Image
    ims = [Image.open(frames_dir / f"frame_{i:05d}.png").convert("RGB") for i in range(n)]
    pal = [im.quantize(256) for im in ims]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(round(1000 / fps)), loop=0, comment=comment.encode("utf-8"))
    return {"path": str(out), "bytes": out.stat().st_size}


# ------------------------------------------------------------------------------------------------------------------- run
def plan(n_frames: int, sec_per_frame: float | None) -> dict:
    est = None if sec_per_frame is None else n_frames * sec_per_frame
    return {"frames": n_frames, "sec_per_frame": sec_per_frame, "est_seconds": est,
            "line": f"{n_frames} frames" + (f", about {sec_per_frame:.1f} s a frame: {est / 60:.0f} minutes" if est else "")}


def run(page_html, out_dir, *, mode: str = "orbit", layer: str | None = None, duration: float = 32.0, fps: float = 25.0, size: str = "1280x720",
        marker: str | None = None, hud_on: bool = True, quality: str = "final", resume: bool = False, gl: str = "auto", frames: int | None = None,
        title: str | None = None, warmup_only: bool = False, capture_factory=None, steady_s: float = STEADY_S, max_turn: float = MAX_TURN,
        timeline=None, poster: str | None = None, web_mb: float = WEB_MB, hooks: dict | None = None, audio=None) -> dict:
    """Capture, composite and encode. `frames` overrides duration x fps (tests). Returns the report: files, frames done and skipped, the seconds
    a frame took, the findings (MAP-OUT-013 from the read-back, MAP-OUT-020 when a credit could not be placed)."""
    import map_brand
    page_html = Path(page_html)
    out = Path(out_dir)
    why = playwright_ok() if capture_factory is None else None
    if why:
        return {"ok": False, "findings": [{"id": "MAP-OUT-013", "severity": "WARN", "message": why}], "files": [], "skipped_reason": why}
    if mode == "path" and not layer:
        raise FlyError("--path needs the line layer to follow", gc.EXIT_USAGE)
    try:                                               # 1.16.0: the timeline (story line, counters, keyframes, poster, end hold)
        tl = tlm.load(timeline, duration=duration)
    except (tlm.TimelineError, OSError) as ex:
        raise FlyError(f"timeline: {ex}", gc.EXIT_USAGE) from None
    if poster:
        tl["poster"] = poster
    if mode != "keyframes" and tl["keyframes"]:
        mode = "keyframes" if mode == "orbit" else mode
    if tl["duration"] and timeline is not None:
        duration = tl["duration"]
    if mode == "slider" and '"engine": "leaflet"' in page_html.read_text(encoding="utf-8", errors="replace")[:400000].replace('"engine":"leaflet"', '"engine": "leaflet"'):
        raise FlyError("a time-lapse of the slider is captured from a MapLibre page: build it with webmap.engine: maplibre", gc.EXIT_USAGE)
    if quality == "draft":
        wh, fps = DRAFT["size"], DRAFT["fps"]
    else:
        if size not in SIZES:
            raise FlyError(f"size {size} is not one of {', '.join(SIZES)}", gc.EXIT_USAGE)
        wh = SIZES[size]
    n = int(frames or max(2, round(duration * fps)))
    stem = f"{page_html.stem}_{'timelapse' if mode == 'slider' else 'flythrough'}{'_draft' if quality == 'draft' else ''}"
    fdir = out / f"{stem}_frames"
    fdir.mkdir(parents=True, exist_ok=True)
    man_p = fdir / "manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8")) if (resume and man_p.is_file()) else {}
    cap = (capture_factory or Capture)(page_html, wh, gl=gl, **({"hooks": hooks} if hooks else {}))
    findings: list[dict] = []
    try:
        ctx = cap.context()
        cams = with_periods(cameras(mode, n, ctx, size=wh, layer=layer, fps=fps, steady_s=steady_s, max_turn=max_turn, tl=tl), mode, ctx, layer)
        cams = with_layers(cams, mode, ctx)
        notes = []
        if quality == "draft" and size in SIZES and SIZES[size][0] != wh[0]:
            # 1.19.0 (Day 16 issue 11): page zooms are in pixels, so the 640 px draft framed twice the area of the 1280 px final and looked
            # right where the final was too wide. The draft is drawn at the final's field of view, scaled down.
            dz = math.log2(wh[0] / float(SIZES[size][0]))
            cams = [({**c, "zoom": float(c["zoom"]) + dz} if isinstance(c.get("zoom"), (int, float)) else c) for c in cams]
            notes.append(f"draft: zooms {dz:+.0f} so the {wh[0]} px draft shows the {SIZES[size][0]} px final's field of view")
        date_text = date_line(tl)
        if hooks and hooks.get("frame"):
            cams = [{**c, "t": round(i / fps, 4)} for i, c in enumerate(cams)]
        if tl.get("grow"):                               # 1.19.0 (Day 16 issue 16): the layer's blocks rise through their moments
            gd = next((d for d in (ctx["cfg"].get("layers") or []) if d.get("id") == tl["grow"]["layer"]), None)
            if not gd or not gd.get("moments"):
                raise FlyError(f"timeline grow: layer '{tl['grow']['layer']}' has no moments on this page; build it with extrude.moments", gc.EXIT_USAGE)
            k = tl["grow"]["per_unit_m"] or gd.get("per_unit_m") or 1.0
            mc = gd.get("moment_classes") if tl["grow"].get("recolor", True) else None
            cams = [{**c, "grow": {**tlm.grow_at(tl, i / fps, int(gd["moments"])), "k": float(k), **({"classes": mc} if mc else {})}}
                    for i, c in enumerate(cams)]
            notes.append(f"grow: '{gd['id']}' rises through {gd['moments']} moments from {tl['grow']['start']:g} to {tl['grow']['end']:g} s at {k:g} m per unit")
        findings += tl["findings"]
        if ctx.get("can_layers") is False:
            notes.append("the page was built before 1.16.0, so the capture cannot set its layers (it starts on the first story chapter's): rebuild it")
        logo = None
        if ctx.get("logo_png"):
            import base64
            import io as _io
            from PIL import Image as _Image
            logo = _Image.open(_io.BytesIO(base64.b64decode(ctx["logo_png"].split(",", 1)[1])))
        elif tl.get("logo", True):
            notes.append("the page shows no brand logo (svg.gm-brand-logo), so the video carries none")
        font_tl = str(_inter_ttf(600))
        if any("time" in c for c in cams) and ctx.get("can_time") is False:
            notes.append("the page was built before 1.6.2, so its time slider stays where it starts: rebuild the page (webmap.py --spec) for the "
                         "course to draw in as it is flown")
        renderer = getattr(cap, "renderer", None)
        if getattr(cap, "gl", gl) == "gpu" and renderer and SOFTWARE_GL.search(renderer):
            findings.append({"id": "MAP-OUT-021", "severity": "WARN",
                             "message": f"the GPU was asked for, but Chromium drew with {renderer}: the frames are right, only slower; "
                                        "check the graphics driver, or use --gl swiftshader to say so"})
        tokens = ctx["cfg"].get("tokens") or {}
        credits = str(ctx.get("credits") or "").strip()
        title = title if title is not None else str(ctx.get("title") or ctx["cfg"].get("title") or "") or None
        done, skipped, times = 0, 0, []
        frames_rec = dict(man.get("frames") or {}) if man.get("size") == list(wh) else {}
        upto = min(n, WARMUP) if warmup_only else n
        tl_key = tlm.dumps(tl)[:4000] if (tl["story"] or tl["counters"]) else ""
        for i in range(upto):
            h = camera_hash({**cams[i], "_tl": tl_key, "_logo": bool(logo)})
            fp = fdir / f"frame_{i:05d}.png"
            if resume and frames_rec.get(str(i)) == h and fp.is_file():
                skipped += 1
                continue
            t0 = time.monotonic()
            png = cap.frame(cams[i], bool(marker) or mode == "path")
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(png)).convert("RGB")
            if im.size != tuple(wh):
                im = im.resize(wh)
            lines = []
            if mode == "path":
                lines.append(f"{cams[i]['distance_m'] / 1000:.1f} km")
            if mode == "slider":
                labs = ((ctx["cfg"].get("time") or {}).get("labels") or [])
                if labs:
                    lines.append(str(labs[cams[i]["time"]]))
            info = {"band_px": 0, "top_px": 0}
            if hud_on or credits:
                im, placed = hud(im, title=title if hud_on else None, lines=lines if hud_on else [], credits=credits, tokens=tokens, info=info,
                                 date_text=date_text if hud_on else None)
                if credits and not placed["credits"] and not any(f["id"] == "MAP-OUT-020" for f in findings):
                    findings.append({"id": "MAP-OUT-020", "severity": "BLOCK",
                                     "message": f"the provider credits ({credits[:80]}...) could not be placed on the frame at {wh[0]} x {wh[1]}; a larger size carries them"})
            if hud_on:                                  # 1.16.0: the logo, the story line and the counters
                im, _pl = tlm.paint(im, i / fps, tl, tokens, font_path=font_tl, logo=logo, bottom_px=info["band_px"], top_px=info["top_px"])
            errs = cap.errors() if hasattr(cap, "errors") else []
            if errs and not any(f.get("detail") == "maplibre" for f in findings):
                findings.append({"id": "MAP-OUT-013", "severity": "WARN", "detail": "maplibre",
                                 "message": f"the page reported an error while frame {i} was drawn (MapLibre raises a bad layer or filter as an event, so it "
                                            f"would pass silently): {errs[0][:200]}"})
            im.save(fp)
            frames_rec[str(i)] = h
            done += 1
            times.append(time.monotonic() - t0)
            if done % 10 == 0:
                man_p.write_text(json.dumps({"size": list(wh), "fps": fps, "mode": mode, "frames": frames_rec}, indent=1), encoding="utf-8")
        man_p.write_text(json.dumps({"size": list(wh), "fps": fps, "mode": mode, "frames": frames_rec}, indent=1), encoding="utf-8")
        spf = round(sum(times) / len(times), 2) if times else None
        rep = {"ok": True, "frames": n, "done": done, "skipped": skipped, "sec_per_frame": spf, "gl": getattr(cap, "gl", gl), "renderer": renderer, "notes": notes, "size": list(wh), "fps": fps,
               "frames_dir": str(fdir), "files": [], "findings": findings, "credits": credits, "estimate": plan(n, spf)}
        if warmup_only:
            return rep
    finally:
        cap.close()
    if any(f["severity"] == "BLOCK" for f in findings):
        rep["ok"] = False
        return rep
    manifest = gc.build_manifest()
    import re
    m_ = re.search(r'data-map-brand="([a-z0-9]+)"', page_html.read_text(encoding="utf-8", errors="replace")[:20000])
    props = map_brand.props_for(m_.group(1), manifest, "png")["tEXt"] if m_ else {}
    comment = "; ".join(f"{k}: {v}" for k, v in props.items() if k in ("Author", "Accreditation", "BrandPack", "Software"))
    meta = {"title": title or stem, "comment": comment, "copyright": props.get("Author", ""), "artist": props.get("Author", "")}
    rep["accreditation"] = bool(comment)
    mp4 = out / f"{stem}.mp4"
    seq_dir, n_out = tlm.assemble(fdir, n, fps, tl)        # 1.16.0: the poster frame and the end hold
    rep["video_frames"] = n_out
    rep["poster"] = tl["poster"]
    if audio is not None:                                  # 2.0.0: the person's own recording under the video
        ap_ = Path(str(audio))
        if not ap_.is_file() or ap_.suffix.lower() not in AUDIO_TYPES:
            raise FlyError(f"--audio needs a sound file of your own ({', '.join(AUDIO_TYPES)}); {ap_.name} is not one", gc.EXIT_USAGE)
        rep["audio"] = ap_.name
        rep["notes"].append(f"sound: {ap_.name} laid under the video from its first frame (cut at the last frame, {n_out / fps:.1f} s); the caption "
                            "files carry the same words for a player without sound")
    try:
        rep["files"].append(encode(seq_dir, mp4, fps, meta, audio=audio, seconds=n_out / fps)["path"])
        rep["files"] += tlm.write_captions(tl, out, stem, fps)            # 2.0.0: <stem>.srt and <stem>.vtt when the timeline has a story line
        import output_lint as ol
        rep["findings"] += ol.check_mp4(str(mp4), tuple(w - w % 2 for w in wh), n_out / fps)
        if quality == "final":
            wc = web_copy(seq_dir, mp4, fps, meta, web_mb)
            if wc:
                rep["files"].append(wc["path"])
                rep["notes"].append(f"the MP4 is {mp4.stat().st_size / 1e6:.1f} MB, over {web_mb:g} MB: a web copy at CRF {wc['crf']} is "
                                    f"{wc['bytes'] / 1e6:.1f} MB ({Path(wc['path']).name}) for an artifact or a post")
    except FlyError as ex:
        rep["findings"].append({"id": "MAP-OUT-013", "severity": "WARN", "message": str(ex)})
    if n_out / fps <= GIF_MAX_S:
        rep["files"].append(write_gif(seq_dir, out / f"{stem}.gif", fps, n_out, comment)["path"])
    rep["ok"] = not any(f["severity"] == "BLOCK" for f in rep["findings"])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("page")
    ap.add_argument("--out", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--path")
    g.add_argument("--chapters", action="store_true")
    g.add_argument("--orbit", action="store_true")
    g.add_argument("--slider", action="store_true", help="1.14.0: a time-lapse of the page's time slider from its own view")
    g.add_argument("--keyframes", metavar="CAM.yaml", help="1.16.0: fly the camera keyframes of a timeline file (free camera, fov, pitch to 115)")
    ap.add_argument("--duration", type=float, default=32.0)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--marker")
    ap.add_argument("--hud", choices=("on", "off"), default="on")
    ap.add_argument("--quality", choices=("draft", "final"), default="draft")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--gl", choices=("auto", *GL_ARGS), default="auto", help="auto: the GPU on Windows and macOS, Mesa llvmpipe on Linux")
    ap.add_argument("--steady", type=float, default=STEADY_S, help="seconds the path camera is averaged over (0: follow every bend)")
    ap.add_argument("--max-turn", type=float, default=MAX_TURN, help="the path camera turns at most this many degrees a second")
    ap.add_argument("--warmup", action="store_true", help="capture the first 10 frames and report the seconds a frame takes")
    ap.add_argument("--timeline", help="1.16.0: a timeline (YAML or JSON): keyframes, story line, counters, poster, end hold (timeline.py)")
    ap.add_argument("--poster", choices=("first", "last", "none"), help="1.16.0: open the video on a held first or last frame")
    ap.add_argument("--on-ready", metavar="READY.js", help="1.19.0: JavaScript run once the page has settled (map is the MapLibre map): add a layer that stays")
    ap.add_argument("--on-frame", metavar="FRAME.js", help="1.19.0: JavaScript run before each frame (map, cam: the frame's camera, its t in cam.t)")
    ap.add_argument("--web-mb", type=float, default=WEB_MB, help="1.19.0: a final MP4 over this many MB also gets a _web.mp4 copy under it (0: none)")
    ap.add_argument("--audio", metavar="FILE", help="2.0.0: your own narration or sound file (mp3, m4a, wav ...), laid under the video from its first frame")
    a = ap.parse_args(argv)
    mode = "path" if a.path else "chapters" if a.chapters else "slider" if a.slider else "keyframes" if a.keyframes else "orbit"
    try:
        rep = run(a.page, a.out, mode=mode, layer=a.path, duration=a.duration, fps=a.fps, size=a.size, marker=a.marker, hud_on=a.hud == "on",
                  quality=a.quality, resume=a.resume, gl=a.gl, warmup_only=a.warmup, steady_s=a.steady, max_turn=a.max_turn,
                  timeline=a.timeline or a.keyframes, poster=a.poster, web_mb=a.web_mb, audio=a.audio,
                  hooks={k: Path(v).read_text(encoding="utf-8") for k, v in (("ready", a.on_ready), ("frame", a.on_frame)) if v} or None)
    except FlyError as ex:
        print(f"flythrough: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_OK if rep.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
