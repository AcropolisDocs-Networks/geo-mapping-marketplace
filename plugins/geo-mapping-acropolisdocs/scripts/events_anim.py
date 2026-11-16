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

1.16.0 (Day 14 issues 1, 6 and 7): `--timeline T.yaml` (timeline.py) adds the story line, counters (a year, a scale), the poster frame and the end
hold, and `features:` per-feature events on a polygon layer of the map (show, hide, fade, lift, drift, settle, recolor): that layer is keyed in
the legend only and drawn frame by frame from its state, each piece moved, turned and scaled about its own centre, a lifted piece over a soft
shadow. Its labels (`label_field`) are drawn upright, at one size and with a halo wherever the piece goes. Pieces that end the video on top of
each other are MAP-OUT-027 (WARN). `--layer` is optional: with only a timeline it is a story video over the map (`map_export.py --story`).

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
import timeline as tlm  # noqa: E402

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


def event_times(values) -> tuple[list[float], str]:
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


def _token(tokens: dict, ref):
    """A colour from a brand token path (status.danger, emphasis, categorical.3) or a hex string."""
    if ref is None:
        return None
    r = str(ref).lstrip("@").replace("brand.", "", 1)
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", r):
        return "#" + r.lstrip("#")
    cur = tokens
    for part in r.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise EventsError(f"colour '{ref}' is neither a hex colour nor a brand token")
    return cur


class Pieces:
    """1.16.0: a polygon layer drawn frame by frame from the timeline's feature events (timeline.feature_state): each piece moved (mm), turned
    and scaled about its centre, faded, recoloured, a lifted piece over a soft shadow; its label upright, one size, haloed, on top."""

    def __init__(self, lp, feats: dict, win: dict, ppm: float, size, tokens: dict, *, font_path: str):
        from PIL import ImageFont
        g = lp.gdf.reset_index(drop=True)
        idf = feats.get("id_field")
        if idf and idf not in g.columns:
            raise EventsError(f"features.id_field '{idf}' is not a column of '{feats['layer']}'")
        self.ids = [str(v) for v in (g[idf] if idf else range(len(g)))]
        rows = [{k: v for k, v in r.items() if k != g.geometry.name} for r in g.to_dict("records")]
        self.targets = tlm.resolve_targets(feats, rows, self.ids)
        for e, tg in zip(feats["events"], self.targets):
            if not tg:
                raise EventsError(f"the feature event at {e['t']:g} s ({e['action']}) matches no feature of '{feats['layer']}'")
        self.feats, self.ppm, self.size, self.tokens = feats, ppm, size, tokens
        self.fill = lp.fill or tokens.get("emphasis")
        self.edge = (lp.stroke if lp.stroke and lp.stroke != "none" else tokens.get("background"))
        self.edge_w = max(1.0, (lp.stroke_w_mm or 0.15) * ppm)
        self.opacity = float(getattr(lp, "fill_opacity", 1.0) or 1.0)
        lf = feats.get("label_field")
        self.labels = [str(r.get(lf) or "") for r in rows] if lf else [""] * len(rows)
        self.rings, self.centres = [], []
        for geom in g.geometry:
            polys = list(getattr(geom, "geoms", [geom]))
            rs = [[[to_px(x, y, win, ppm) for x, y in p.exterior.coords]] + [[to_px(x, y, win, ppm) for x, y in r.coords] for r in p.interiors] for p in polys]
            self.rings.append(rs)
            c = geom.representative_point()
            self.centres.append(to_px(c.x, c.y, win, ppm))
        self.font = ImageFont.truetype(font_path, max(11, round(size[1] * 0.02)))
        self._static = {}

    def _xf(self, pts, c, s):
        ang = math.radians(s["rot"])
        ca, sa = math.cos(ang), math.sin(ang)
        dx, dy = s["dx"] * self.ppm, (s["dy"] - s["lift"] * tlm.LIFT_MM) * self.ppm
        out = []
        for x, y in pts:
            u, v = (x - c[0]) * s["scale"], (y - c[1]) * s["scale"]
            out.append((c[0] + u * ca - v * sa + dx, c[1] + u * sa + v * ca + dy))
        return out

    def _colour(self, s):
        col = _rgb(_token(self.tokens, s["colour"]) if s.get("colour") else self.fill)
        if s.get("_mix"):
            to, u = s["_mix"]
            c2 = _rgb(_token(self.tokens, to))
            col = tuple(round(a + (b - a) * u) for a, b in zip(col, c2))
        return col

    def _paint(self, d, mask_d, i, s, shadow: bool):
        from PIL import ImageDraw  # noqa: F401
        c = self.centres[i]
        for poly in self.rings[i]:
            ext = self._xf(poly[0], c, s)
            if shadow:
                off = s["lift"] * tlm.LIFT_MM * 0.6 * self.ppm
                d.polygon([(x + off, y + off * 1.4) for x, y in ext], fill=(0, 0, 0, round(70 * s["lift"] * s["opacity"])))
                continue
            a = round(255 * self.opacity * s["opacity"])
            d.polygon(ext, fill=(*self._colour(s), a), outline=(*_rgb(self.edge), round(255 * s["opacity"])), width=round(self.edge_w))
            for hole in poly[1:]:
                d.polygon(self._xf(hole, c, s), fill=(0, 0, 0, 0))

    def states(self, t: float) -> dict:
        return tlm.feature_state(self.feats, self.targets, self.ids, t)

    def draw(self, base, t: float):
        from PIL import Image, ImageDraw
        st = self.states(t)
        still = tuple(i for i, fid in enumerate(self.ids) if st[fid] == tlm.BASE)
        im = base.copy().convert("RGBA")
        if still not in self._static:                       # the pieces at rest, drawn once for each set of them
            self._static.clear()
            lay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            for i in still:
                self._paint(d, None, i, tlm.BASE, False)
            self._static[still] = lay
        im.alpha_composite(self._static[still])
        moving = [i for i, fid in enumerate(self.ids) if i not in set(still) and st[self.ids[i]]["opacity"] > 0.005]
        if moving:
            lay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            for i in sorted(moving, key=lambda j: st[self.ids[j]]["lift"]):
                if st[self.ids[i]]["lift"] > 0.01:
                    self._paint(d, None, i, st[self.ids[i]], True)
                self._paint(d, None, i, st[self.ids[i]], False)
            im.alpha_composite(lay)
        if any(self.labels):
            d = ImageDraw.Draw(im)
            ink, halo = _rgb(self.tokens["label"]), _rgb(self.tokens.get("label_halo") or self.tokens["background"])
            for i, fid in enumerate(self.ids):
                s = st[fid]
                if not self.labels[i] or s["opacity"] < 0.05:
                    continue
                x, y = self._xf([self.centres[i]], self.centres[i], s)[0]
                a = round(255 * s["opacity"])
                d.text((x, y), self.labels[i], font=self.font, anchor="mm", fill=(*ink, a), stroke_width=max(2, round(self.font.size * 0.14)),
                       stroke_fill=(*halo, a))
        return im.convert("RGB")

    def end_findings(self, t: float) -> list[dict]:
        st = self.states(t)
        boxes = {}
        for i, fid in enumerate(self.ids):
            s = st[fid]
            if s == tlm.BASE or s["opacity"] < 0.05 or (abs(s["dx"]) + abs(s["dy"]) < 0.01 and abs(s["rot"]) < 0.01):
                continue
            pts = [p for poly in self.rings[i] for p in self._xf(poly[0], self.centres[i], s)]
            xs, ys = [p[0] / self.ppm for p in pts], [p[1] / self.ppm for p in pts]
            boxes[fid] = (min(xs), min(ys), max(xs), max(ys))
        return tlm.end_collisions(boxes)


def build_base(spec_path: Path, layer: str | None, size: str, work: Path, brand: str | None, scale: float = 1.0, also: tuple = ()):
    """The map without the event layer (keyed in the legend only), at the size's template and dpi. `also`: more layers keyed in the legend
    only (1.16.0: a timeline's feature layer, drawn by the overlay). -> (RGB image, report, plan, events frame or None, colour, dpi, {id: plan})."""
    import animate
    import build_map as bm
    import map_plan as mp
    import yaml
    if size not in animate.SIZES:
        raise EventsError(f"size {size} is not one of {', '.join(animate.SIZES)}")
    cfg = animate.SIZES[size]
    spec = gc.load_mapspec(spec_path)
    names = ', '.join(ly.get('id', '?') for ly in spec.get('layers') or [])
    plan = mp.prepare(str(spec_path))
    events_gdf, layer_colour, extra = None, None, {}
    for lid in ([layer] if layer else []) + list(also):
        lay = next((ly for ly in spec.get("layers") or [] if ly.get("id") == lid), None)
        if lay is None:
            raise EventsError(f"the MapSpec has no layer '{lid}'; its layers: {names}")
        lp = next((l for l in plan.layers if l.id == lid), None)
        if lid == layer:
            if lp is None or lp.kind != "point":
                raise EventsError(f"layer '{layer}' must be a point layer that the map draws")
            events_gdf, layer_colour = lp.gdf.copy(), lp.fill
        else:
            if lp is None or lp.kind != "polygon":
                raise EventsError(f"the timeline's feature layer '{lid}' must be a polygon layer that the map draws")
            extra[lid] = lp
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
    return base, obj["rendered"].report, obj["plan"], events_gdf, layer_colour, cfg["dpi"] * scale, extra


def run(spec_path, out_dir, *, layer: str | None = None, time_field: str | None = None, highlight: str | None = None, size: str = "slide",
        duration: float = 30.0, fps: float = 25.0, intro: float = 2.0, counter: str | None = "{n} events", quality: str = "final",
        brand: str | None = None, frames: int | None = None, timeline=None, poster: str | None = None) -> dict:
    """Build the base map, place the events, draw the frames, encode the MP4. `frames` overrides duration x fps (tests). 1.16.0: a timeline
    (story line, counters, feature events, poster, end hold); with a timeline the point event layer is optional."""
    import animate
    import flythrough as fl
    import map_brand
    from PIL import ImageFont
    spec_path, out = Path(spec_path), Path(out_dir)
    if quality == "draft":
        fps = DRAFT["fps"]
    scale = DRAFT["scale"] if quality == "draft" else 1.0
    try:
        tl = tlm.load(timeline, duration=duration)
    except (tlm.TimelineError, OSError) as ex:
        raise EventsError(f"timeline: {ex}") from None
    if poster:
        tl["poster"] = poster
    if timeline is not None and tl["duration"]:
        duration = tl["duration"]
    feats = tl["features"]
    if not layer and not (feats or tl["story"] or tl["counters"]):
        raise EventsError("give --layer (a point layer of events), or a --timeline with story, counters or features")
    work = Path(tempfile.mkdtemp(prefix="gm-events-"))
    base, report, plan, gdf, layer_colour, dpi, extra = build_base(spec_path, layer, size, work, brand, scale,
                                                                   also=(feats["layer"],) if feats else ())
    n = int(frames or max(2, round(duration * fps)))
    intro_f = min(n - 2, round(intro * fps))
    win = report["map_window"]
    ppm = dpi / 25.4
    events, kind = [], "number"
    if layer:
        tf = time_field or (plan.spec and next((ly.get("time_field") for ly in plan.spec.get("layers") or [] if ly.get("id") == layer), None))
        if not tf or tf not in gdf.columns:
            raise EventsError(f"give --time-field: one of {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
        hl = parse_highlight(highlight)
        if hl and hl[0] not in gdf.columns:
            raise EventsError(f"--highlight names '{hl[0]}', which layer '{layer}' does not have")
        times, kind = event_times(gdf[tf].tolist())
        app = appear_frames(times, n, intro_f)
        xs, ys = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
        for i in range(len(gdf)):
            if not (math.isfinite(xs[i]) and math.isfinite(ys[i])):
                continue
            events.append({"px": to_px(xs[i], ys[i], win, ppm), "frame": app[i], "t": times[i], "hl": bool(hl and hl[1](gdf[hl[0]].iloc[i]))})
    else:
        tf, counter = None, None
    events.sort(key=lambda e: (e["frame"], e["hl"]))                    # highlighted events are drawn over the others of their frame
    t = plan.tokens
    colours = {"event": _rgb(layer_colour or t["emphasis"]), "hl": _rgb((t.get("status") or {}).get("danger") or t["emphasis"]), "edge": _rgb(t["background"]),
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
    stem = f"{spec_path.stem}_{'events' if layer else 'story'}{'_draft' if quality == 'draft' else ''}"
    fdir = out / f"{stem}_frames"
    fdir.mkdir(parents=True, exist_ok=True)
    pieces = Pieces(extra[feats["layer"]], feats, win, ppm, base.size, plan.tokens, font_path=str(fl._inter_ttf(600))) if feats else None
    findings = list(tl["findings"])
    bottom_px = max(0, base.size[1] - round(win["rect_mm"][3] * ppm)) + round(base.size[1] * 0.02)
    font_tl = str(fl._inter_ttf(600))
    avoid = [[v * ppm for v in (o or {}).get("rect_mm")] for o in ((report or {}).get("overlays") or {}).values()
             if isinstance(o, dict) and (o or {}).get("rect_mm")]
    for k in range(n):
        tk = k / fps
        b = pieces.draw(base, tk) if pieces else base
        im = draw_frame(b, events, k, fps, r_px=r_px, colours=colours, counter=counter, font=font, date_text=date_text, at=at)
        if tl["story"] or tl["counters"]:
            im, _pl = tlm.paint(im, tk, tl, t, font_path=font_tl, logo=None, bottom_px=bottom_px, top_px=round(win["rect_mm"][1] * ppm), avoid=avoid)
        im.save(fdir / f"frame_{k:05d}.png")
    if pieces:
        findings += pieces.end_findings((n - 1) / fps)
    manifest = gc.build_manifest()
    props = map_brand.props_for(plan.brand, manifest, "png")["tEXt"]
    comment = "; ".join(f"{k_}: {v}" for k_, v in props.items() if k_ in ("Author", "Accreditation", "BrandPack", "Software"))
    rep = {"ok": True, "frames": n, "fps": fps, "size": list(base.size), "events": len(events), "highlighted": sum(e["hl"] for e in events),
           "intro_s": intro_f / fps, "time_field": tf, "files": [], "findings": findings, "frames_dir": str(fdir),
           "pieces": len(pieces.ids) if pieces else 0,
           "notes": [f"{len(events):,} events over {n / fps:.1f} s; the map built once at {size} ({base.size[0]} x {base.size[1]} px)"]}
    mp4 = out / f"{stem}.mp4"
    seq_dir, n_out = tlm.assemble(fdir, n, fps, tl)
    rep["video_frames"], rep["poster"] = n_out, tl["poster"]
    try:
        rep["files"].append(fl.encode(seq_dir, mp4, fps, {"title": plan.spec.get("id") or stem, "comment": comment,
                                                           "copyright": props.get("Author", ""), "artist": props.get("Author", "")})["path"])
        import output_lint as ol
        rep["findings"] += ol.check_mp4(str(mp4), tuple(v - v % 2 for v in base.size), n_out / fps)
    except (fl.FlyError, animate.AnimateError) as ex:
        rep["findings"].append({"id": "MAP-OUT-013", "severity": "WARN", "message": str(ex)})
    rep["ok"] = not any(f["severity"] == "BLOCK" for f in rep["findings"])
    return rep


# 1.14.0's name for event_times, kept for scripts that import it (inside run() the name is the --timeline argument).
timeline = event_times


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--layer", help="the point layer of events (optional with a --timeline)")
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
    ap.add_argument("--timeline", help="1.16.0: story line, counters, feature events, poster and end hold (timeline.py)")
    ap.add_argument("--poster", choices=("first", "last", "none"), help="1.16.0: open the video on a held first or last frame")
    a = ap.parse_args(argv)
    try:
        rep = run(a.spec, a.out, layer=a.layer, time_field=a.time_field, highlight=a.highlight, size=a.size, duration=a.duration, fps=a.fps,
                  intro=a.intro, counter=a.counter or None, quality=a.quality, brand=a.brand, timeline=a.timeline, poster=a.poster)
    except EventsError as ex:
        print(f"events: {ex}", file=sys.stderr)
        return ex.code
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_OK if rep.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
