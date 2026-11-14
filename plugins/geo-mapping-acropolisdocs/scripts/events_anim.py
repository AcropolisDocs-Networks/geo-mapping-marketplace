#!/usr/bin/env python3
"""events_anim: an event-timeline video over a branded map (1.14.0, Day 13 issue 8).

    map_export.py --events MAP.yaml --layer QUAKES --out DIR [--time-field FIELD] [--highlight FIELD=VALUE | --highlight FIELD>=NUMBER]
                  [--size slide|square|portrait] [--duration 30] [--fps 25] [--intro 2] [--counter "M7+ near a cable: {hl} of {n}"]
                  [--quality draft|final]

The map is built once by the plugin at the video's size, with everything the MapSpec draws except the event layer, which stays in the legend
(legend.only): title, legend, credits, locator, logo as on any map. Then the events play over it in time order, at the time each happened
(the run is linear in time from the first event to the last, after `intro` seconds of the map alone):

  pop      each event appears with a short grow-and-settle, in its layer's colour, and stays
  ripple   an event that meets the highlight (a column's value, or a number at or above a threshold) appears in the brand's danger colour
           with two rings that spread and fade, and stays in that colour
  counter  a running count in the lower left, over a halo: "{n}" events so far, "{hl}" highlighted, "{date}" the time reached

The overlay is placed with the build report's `map_window` (the map's extent in its own CRS and the rectangle in millimetres it fills), the hook
any custom overlay can use (docs: export-animation-profile). Points are drawn at twice the size and reduced, so their edges are smooth. Frames are
numbered PNGs; the MP4 is H.264 through ffmpeg, as the other exports. The accreditation goes into the MP4 metadata, never into a frame.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

POP_S = 0.35                 # seconds an event takes to grow in
RIPPLE_S = 1.6               # seconds a highlighted event's rings take to spread and fade
RIPPLE_RINGS = 2
SS = 2                       # supersampling of the overlay
DRAFT = {"fps": 12.5, "scale": 0.5}


class EventsError(RuntimeError):
    def __init__(self, msg: str, code: int = gc.EXIT_USAGE):
        super().__init__(msg)
        self.code = code


def parse_highlight(text: str | None):
    """FIELD=VALUE (string match), FIELD>=NUMBER or FIELD<=NUMBER -> a test on a row, or None."""
    if not text:
        return None
    m = re.match(r"^\s*([^=<>]+?)\s*(>=|<=|=)\s*(.+?)\s*$", text)
    if not m:
        raise EventsError(f"--highlight is FIELD=VALUE, FIELD>=NUMBER or FIELD<=NUMBER, not '{text}'")
    col, op, val = m.group(1), m.group(2), m.group(3)
    if op == "=":
        return col, lambda v: str(v).strip().lower() == val.strip().lower()
    num = float(val)
    return col, (lambda v: _num(v) is not None and _num(v) >= num) if op == ">=" else (lambda v: _num(v) is not None and _num(v) <= num)


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def timeline(values) -> tuple[list[float], str]:
    """Each event's time as a number on one axis (seconds for dates, the value itself for numbers) and its kind ('date' or 'number')."""
    import pandas as pd
    nums = [_num(v) for v in values]
    if all(n is not None for n in nums) and not all(isinstance(v, str) for v in values):
        return [float(n) for n in nums], "number"
    ts = pd.to_datetime(pd.Series(list(values), dtype="object"), errors="coerce", utc=True, format="mixed")
    if ts.isna().all():
        raise EventsError("the time field holds neither dates nor numbers")
    return [float(t.timestamp()) if not pd.isna(t) else float("nan") for t in ts], "date"


def appear_frames(times: list[float], n_frames: int, intro_frames: int) -> list[int]:
    """The frame each event appears at: linear in time from the first event (after the intro) to the last (a few frames before the end)."""
    ok = [t for t in times if t == t]
    if not ok:
        return [-1] * len(times)
    t0, t1 = min(ok), max(ok)
    span = max(t1 - t0, 1e-9)
    last = max(intro_frames, n_frames - max(2, n_frames // 12))
    return [-1 if t != t else intro_frames + round((t - t0) / span * (last - intro_frames)) for t in times]


def to_px(x, y, win: dict, px_per_mm: float):
    """A point in the map's CRS to page pixels, through the build report's map_window (extent in the CRS, the rect it fills in millimetres)."""
    ex0, ey0, ex1, ey1 = win["extent"]
    rx0, ry0, rx1, ry1 = win["rect_mm"]
    mx = rx0 + (x - ex0) / (ex1 - ex0) * (rx1 - rx0)
    my = ry0 + (ey1 - y) / (ey1 - ey0) * (ry1 - ry0)
    return mx * px_per_mm, my * px_per_mm


def _ease_pop(f: float) -> float:
    """0 to 1 with a small overshoot (back-out), so an event lands rather than fades in."""
    f = min(max(f, 0.0), 1.0)
    s = 1.70158
    f -= 1.0
    return f * f * ((s + 1) * f + s) + 1.0


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def counter_xy(report: dict, ppm: float, size, font_px: int) -> tuple[int, int]:
    """Where the counter's text starts: just above the logo (lower left, the default corner), in the map; else 3 percent in from the lower left."""
    ov = (report or {}).get("overlays") or {}
    logo = (ov.get("logo") or {}).get("rect_mm")
    if logo:
        return round(logo[0] * ppm), max(0, round(logo[1] * ppm - font_px * 1.6))
    w, h = size
    return round(w * 0.03), round(h * 0.86)


def draw_frame(base, events: list[dict], k: int, fps: float, *, r_px: float, colours: dict, counter: str | None, font, date_text,
               at: tuple[int, int] | None = None) -> "object":
    """One frame: the base map, every event that has appeared by frame k (popping, rippling or settled), and the counter."""
    from PIL import Image, ImageDraw
    w, h = base.size
    over = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    pop_n, rip_n = max(1, round(POP_S * fps)), max(1, round(RIPPLE_S * fps))
    n = hl = 0
    reached = None
    for e in events:
        a = e["frame"]
        if a < 0 or a > k:
            continue
        n += 1
        hl += 1 if e["hl"] else 0
        reached = e["t"] if reached is None or e["t"] > reached else reached
        x, y = e["px"][0] * SS, e["px"][1] * SS
        age = k - a
        col = colours["hl"] if e["hl"] else colours["event"]
        if e["hl"] and age < rip_n:
            for j in range(RIPPLE_RINGS):
                f = age / rip_n - j * 0.25
                if 0 <= f <= 1:
                    rr = r_px * SS * (1.5 + 5.0 * f)
                    alpha = int(220 * (1 - f))
                    d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=(*col, alpha), width=max(1, round(SS * 1.6)))
        scale = _ease_pop(age / pop_n) if age < pop_n else 1.0
        rr = r_px * SS * (1.4 if e["hl"] else 1.0) * scale
        if rr > 0:
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(*col, 235), outline=(*colours["edge"], 255), width=max(1, round(SS * 0.6)))
    over = over.resize((w, h), Image.LANCZOS)
    im = base.copy().convert("RGBA")
    im.alpha_composite(over)
    if counter:
        text = counter.format(n=f"{n:,}", hl=f"{hl:,}", date=date_text(reached) if reached is not None else "")
        dd = ImageDraw.Draw(im)
        tx, ty = at or (round(w * 0.03), round(h * 0.86))       # 1.14.0: above the logo, never over it
        dd.text((tx, ty), text, font=font, fill=(*colours["ink"], 255), stroke_width=max(2, round(font.size * 0.14)), stroke_fill=(*colours["halo"], 255))
    return im.convert("RGB")


def build_base(spec_path: Path, layer: str, size: str, work: Path, brand: str | None, scale: float = 1.0):
    """The map without the event layer (keyed in the legend only), at the size's template and dpi. -> (RGB image, report, plan, events frame)."""
    import animate
    import build_map as bm
    import map_plan as mp
    import yaml
    if size not in animate.SIZES:
        raise EventsError(f"size {size} is not one of {', '.join(animate.SIZES)}")
    cfg = animate.SIZES[size]
    spec = gc.load_mapspec(spec_path)
    lay = next((ly for ly in spec.get("layers") or [] if ly.get("id") == layer), None)
    if lay is None:
        raise EventsError(f"the MapSpec has no layer '{layer}'; its layers: {', '.join(ly.get('id', '?') for ly in spec.get('layers') or [])}")
    plan = mp.prepare(str(spec_path))
    lp = next((l for l in plan.layers if l.id == layer), None)
    if lp is None or lp.kind != "point":
        raise EventsError(f"layer '{layer}' must be a point layer that the map draws")
    events_gdf, layer_colour = lp.gdf.copy(), lp.fill
    lay.setdefault("legend", {})["only"] = True
    spec["template"] = cfg["template"]
    spec["export"] = {**(spec.get("export") or {}), "dpi": cfg["dpi"] * scale}
    work.mkdir(parents=True, exist_ok=True)
    tmp = spec_path.parent / f".{spec_path.stem}.events-base.yaml"          # beside the MapSpec, so its relative data paths still resolve
    tmp.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    try:
        res = bm.build(str(tmp), str(work / "base"), ["png"], brand=brand or spec.get("brand"), keep=True)
    except mp.PlanError as ex:
        raise EventsError(f"the map cannot be built: {str(ex).splitlines()[0]}") from None
    finally:
        tmp.unlink(missing_ok=True)
    obj = res.pop("_objects")
    blocks = [f for f in res["findings"] if f["severity"] == "BLOCK"]
    if blocks:
        raise EventsError("the map has blocking findings: " + "; ".join(f["message"] for f in blocks[:3]), gc.EXIT_LINT)
    png = Path(next(f for f in res["files"] if f.endswith(".png"))).read_bytes()
    px = (round(cfg["px"][0] * scale) // 2 * 2, round(cfg["px"][1] * scale) // 2 * 2)
    base = animate.fit_frame(png, *px, _rgb(obj["plan"].tokens["background"]))
    return base, obj["rendered"].report, obj["plan"], events_gdf, layer_colour, cfg["dpi"] * scale


def run(spec_path, out_dir, *, layer: str, time_field: str | None = None, highlight: str | None = None, size: str = "slide", duration: float = 30.0,
        fps: float = 25.0, intro: float = 2.0, counter: str | None = "{n} events", quality: str = "final", brand: str | None = None,
        frames: int | None = None) -> dict:
    """Build the base map, place the events, draw the frames, encode the MP4. `frames` overrides duration x fps (tests)."""
    import animate
    import flythrough as fl
    import map_brand
    from PIL import ImageFont
    spec_path, out = Path(spec_path), Path(out_dir)
    if quality == "draft":
        fps = DRAFT["fps"]
    scale = DRAFT["scale"] if quality == "draft" else 1.0
    work = Path(tempfile.mkdtemp(prefix="gm-events-"))
    base, report, plan, gdf, layer_colour, dpi = build_base(spec_path, layer, size, work, brand, scale)
    tf = time_field or (plan.spec and next((ly.get("time_field") for ly in plan.spec.get("layers") or [] if ly.get("id") == layer), None))
    if not tf or tf not in gdf.columns:
        raise EventsError(f"give --time-field: one of {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
    hl = parse_highlight(highlight)
    if hl and hl[0] not in gdf.columns:
        raise EventsError(f"--highlight names '{hl[0]}', which layer '{layer}' does not have")
    times, kind = timeline(gdf[tf].tolist())
    n = int(frames or max(2, round(duration * fps)))
    intro_f = min(n - 2, round(intro * fps))
    app = appear_frames(times, n, intro_f)
    win = report["map_window"]
    ppm = dpi / 25.4
    xs, ys = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
    events = []
    for i in range(len(gdf)):
        if not (math.isfinite(xs[i]) and math.isfinite(ys[i])):
            continue
        events.append({"px": to_px(xs[i], ys[i], win, ppm), "frame": app[i], "t": times[i], "hl": bool(hl and hl[1](gdf[hl[0]].iloc[i]))})
    events.sort(key=lambda e: (e["frame"], e["hl"]))                    # highlighted events are drawn over the others of their frame
    t = plan.tokens
    colours = {"event": _rgb(layer_colour), "hl": _rgb((t.get("status") or {}).get("danger") or t["emphasis"]), "edge": _rgb(t["background"]),
               "ink": _rgb(t["label"]), "halo": _rgb(t.get("label_halo") or t["background"])}
    import datetime as dt
    import geo_locale as gl

    def date_text(v):
        if kind == "number":
            return f"{v:,.6g}"
        return gl.plan_date_text(plan, dt.datetime.fromtimestamp(v, dt.timezone.utc).date())
    font = ImageFont.truetype(str(fl._inter_ttf(600)), max(12, round(base.size[1] * 0.032)))
    r_px = max(2.0, base.size[1] * 0.0042)
    at = counter_xy(report, ppm, base.size, font.size)
    stem = f"{spec_path.stem}_events{'_draft' if quality == 'draft' else ''}"
    fdir = out / f"{stem}_frames"
    fdir.mkdir(parents=True, exist_ok=True)
    for k in range(n):
        draw_frame(base, events, k, fps, r_px=r_px, colours=colours, counter=counter, font=font, date_text=date_text,
                   at=at).save(fdir / f"frame_{k:05d}.png")
    manifest = gc.build_manifest()
    props = map_brand.props_for(plan.brand, manifest, "png")["tEXt"]
    comment = "; ".join(f"{k_}: {v}" for k_, v in props.items() if k_ in ("Author", "Accreditation", "BrandPack", "Software"))
    rep = {"ok": True, "frames": n, "fps": fps, "size": list(base.size), "events": len(events), "highlighted": sum(e["hl"] for e in events),
           "intro_s": intro_f / fps, "time_field": tf, "files": [], "findings": [], "frames_dir": str(fdir),
           "notes": [f"{len(events):,} events over {n / fps:.1f} s; the map built once at {size} ({base.size[0]} x {base.size[1]} px)"]}
    mp4 = out / f"{stem}.mp4"
    try:
        rep["files"].append(fl.encode(fdir, mp4, fps, {"title": plan.spec.get("id") or stem, "comment": comment,
                                                        "copyright": props.get("Author", ""), "artist": props.get("Author", "")})["path"])
        import output_lint as ol
        rep["findings"] += ol.check_mp4(str(mp4), tuple(v - v % 2 for v in base.size), n / fps)
    except (fl.FlyError, animate.AnimateError) as ex:
        rep["findings"].append({"id": "MAP-OUT-013", "severity": "WARN", "message": str(ex)})
    rep["ok"] = not any(f["severity"] == "BLOCK" for f in rep["findings"])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--layer", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--time-field")
    ap.add_argument("--highlight")
    ap.add_argument("--size", default="slide")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--intro", type=float, default=2.0)
    ap.add_argument("--counter", default="{n} events")
    ap.add_argument("--quality", choices=("draft", "final"), default="draft")
    ap.add_argument("--brand")
    a = ap.parse_args(argv)
    try:
        rep = run(a.spec, a.out, layer=a.layer, time_field=a.time_field, highlight=a.highlight, size=a.size, duration=a.duration, fps=a.fps,
                  intro=a.intro, counter=a.counter or None, quality=a.quality, brand=a.brand)
    except EventsError as ex:
        print(f"events: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_OK if rep.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
