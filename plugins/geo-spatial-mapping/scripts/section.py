#!/usr/bin/env python3
"""section: a terrain cross-section drawn into a print map (1.20.0, #30DayMapChallenge Day 17 issue 8).

    elements:
      section:
        on: true
        line: [[-112.25, 36.12], [-112.10, 36.10]]     # A and A' in lon/lat, or {layer: cut} (the first line of a line layer)
        dem: dem.tif                                    # optional: else the DEM of the map's hillshade
        sun: auto                                       # auto: the hillshade's one sun; {azimuth, altitude}; off
        exaggeration: auto                              # auto: true scale when the box is tall enough, else the least of 2, 3, 5, 10
        labels: [{text: Isis Temple, at: [-112.17, 36.11]}]   # names set at their place along the cut (within 1 km of it)
        low_point: true                                 # the lowest elevation on the cut, named in metres
        ends: [A, "A′"]
        title: "Section A–A′ along the sun's path"
        position: lower-left                            # as elements.figures; w_mm (default 120)

The cut is sampled along the WGS84 geodesic at the DEM's own spacing (bilinear). With a sun, the shadow line is the highest sun ray reaching each
point, worked in the plane of the cut from a run-in past its sunward end (so ridges beyond A still throw their shadow in): exact when the cut
runs along the sun's bearing, and drawn only when the cut is within 45 degrees of it (the notes say how far off it is). Every colour is a brand
token; the box is a figure (map_extras.place_figures), so labels keep clear of it and every rebuild draws it again. The cut itself is drawn
on the map as a dashed line with its end letters.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import svg_draw as sd  # noqa: E402
import textmeasure as tm  # noqa: E402

WGS84 = "EPSG:4326"
RUN_IN_MAX_M = 20000.0
MAX_SUN_OFF_DEG = 45.0
MIN_PLOT_H_MM = 14.0
MAX_PLOT_H_MM = 70.0
NICE_VE = (2, 3, 5, 10, 20)
PT = 0.3528


class SectionError(Exception):
    pass


def bearing(a, b) -> float:
    from pyproj import Geod
    fwd, _back, _d = Geod(ellps="WGS84").inv(a[0], a[1], b[0], b[1])
    return fwd % 360.0


def cut_points(a, b, spacing_m: float, before_m: float = 0.0, after_m: float = 0.0):
    """Points every `spacing_m` along the geodesic from `before_m` before A to `after_m` past B, and their distance from A (negative before A)."""
    from pyproj import Geod
    g = Geod(ellps="WGS84")
    az, back, D = g.inv(a[0], a[1], b[0], b[1])
    start = g.fwd(a[0], a[1], back, before_m)[:2] if before_m > 0 else a
    total = before_m + D + after_m
    n = max(2, int(math.ceil(total / max(spacing_m, 1.0))) + 1)
    d = np.linspace(-before_m, D + after_m, n)
    lon, lat, _ = g.fwd(np.full(n, start[0]), np.full(n, start[1]), np.full(n, az), d + before_m)
    return np.column_stack([lon, lat]), d, float(D)


def shadow_line(d: np.ndarray, z: np.ndarray, tan_alt: float, sun_ahead: bool) -> np.ndarray:
    """The height of the highest sun ray over each sample: walking from the sunward end, H = max(z, H_prev - step x tan(altitude))."""
    zz = np.where(np.isfinite(z), z, -1e9)
    order = range(len(d) - 1, -1, -1) if sun_ahead else range(len(d))
    h = np.full(len(d), -1e9)
    prev, prev_d = -1e9, None
    for i in order:
        cur = zz[i] if prev_d is None else max(zz[i], prev - abs(d[i] - prev_d) * tan_alt)
        h[i], prev, prev_d = cur, cur, d[i]
    return h


def compute(dem_path, a, b, sun: dict | None) -> dict:
    """The cut's profile and, with a sun, its shadow line. -> {d, z, shadow (or None), length_m, sun_off_deg, notes}."""
    import raster_io as rio
    import terrain as tr
    spacing = max(5.0, tr.dem_resolution_m(dem_path))
    notes: list[str] = []
    brg = bearing(a, b)
    run_in, sun_ahead, off, tan_eff = 0.0, True, None, None
    if sun:
        off = ((sun["azimuth"] - brg + 180.0) % 360.0) - 180.0       # the sun's bearing from the cut's direction A to B
        sun_ahead = abs(off) < 90.0
        along = abs(off) if sun_ahead else 180.0 - abs(off)
        if along > MAX_SUN_OFF_DEG:
            notes.append(f"section: the cut runs {along:.0f} degrees off the sun's bearing, so no shadow line is drawn (it is exact along the sun's path)")
            sun = None
        else:
            tan_eff = math.tan(math.radians(max(0.5, sun["altitude"]))) / max(math.cos(math.radians(along)), 1e-6)
            if along > 5:
                notes.append(f"section: the cut runs {along:.0f} degrees off the sun's bearing; the shadow line is worked in the plane of the cut")
    pts0, d0, D = cut_points(a, b, spacing)
    z0 = rio.sample(dem_path, pts0, WGS84)
    if not np.isfinite(z0).any():
        raise SectionError("the DEM holds no elevation along the cut")
    shadow = None
    if sun:
        relief = float(np.nanmax(z0) - np.nanmin(z0))
        run_in = min(RUN_IN_MAX_M, relief / tan_eff * 1.5 + spacing) if tan_eff else 0.0
        pts, d, _D = cut_points(a, b, spacing, before_m=0.0 if sun_ahead else run_in, after_m=run_in if sun_ahead else 0.0)
        z = rio.sample(dem_path, pts, WGS84)
        h = shadow_line(d, z, tan_eff, sun_ahead)
        inside = (d >= -1e-6) & (d <= D + 1e-6)
        shadow = np.interp(d0, d[inside], h[inside])
    return {"d": d0, "z": z0, "shadow": shadow, "length_m": D, "spacing_m": spacing, "bearing": brg, "sun_off_deg": off, "sun": sun,
            "run_in_m": run_in, "notes": notes}


def along_cut(a, b, lonlat) -> tuple[float, float]:
    """(distance along the cut from A, distance off it), in metres, of a point (a flat-earth projection at the cut's latitude)."""
    lat0 = math.radians((a[1] + b[1]) / 2)
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    ax, ay = 0.0, 0.0
    bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    px, py = (lonlat[0] - a[0]) * kx, (lonlat[1] - a[1]) * ky
    L = math.hypot(bx, by) or 1.0
    ux, uy = bx / L, by / L
    return (px - ax) * ux + (py - ay) * uy, abs(-(px - ax) * uy + (py - ay) * ux)


def _nice_step(span: float, target: int = 5) -> float:
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def compass(az: float) -> str:
    names = ["northern", "north-eastern", "eastern", "south-eastern", "southern", "south-western", "western", "north-western"]
    return names[int(((az % 360) + 22.5) // 45) % 8]


def svg(prof: dict, cfg: dict, t: dict, w_mm: float = 120.0, label_pt: float = 7.0) -> tuple[str, list[float], list[str]]:
    """The section box as SVG markup in millimetres (its own viewBox): title, subtitle, ground, sunlit and shaded edges, the air in shadow,
    the sun and its grazing rays, names, the low point, the end letters, distance and height ticks. -> (inner markup, viewBox, notes)."""
    d, z, sh = np.asarray(prof["d"], float), np.asarray(prof["z"], float), prof.get("shadow")
    ok = np.isfinite(z)
    D = prof["length_m"]
    notes: list[str] = []
    pad, title_h, sub_h, axis_h = 3.0, label_pt * 1.25 * PT + 1.6, (label_pt - 1) * 1.25 * PT + 1.2, 6.0
    plot_w = w_mm - 2 * pad - 9.0                      # 9 mm at the left for the height ticks
    k = plot_w / D                                     # mm per metre, both ways at true scale
    zmin, zmax = float(np.nanmin(z)), float(np.nanmax(z))
    if sh is not None:
        zmax = max(zmax, float(np.nanmax(np.where(ok, np.minimum(sh, zmax + (zmax - zmin)), np.nan))))
    step_z = _nice_step(max(zmax - zmin, 1.0), 3)
    base = math.floor(zmin / step_z) * step_z
    top = math.ceil(zmax / step_z) * step_z
    ve = cfg.get("exaggeration", "auto")
    true_h = (top - base) * k
    if ve in (None, "auto"):
        ve = 1
        if true_h < MIN_PLOT_H_MM:
            ve = next((v for v in NICE_VE if true_h * v >= MIN_PLOT_H_MM), NICE_VE[-1])
    ve = float(ve)
    plot_h = min(MAX_PLOT_H_MM, true_h * ve)
    if true_h * ve > MAX_PLOT_H_MM:
        notes.append(f"section: the box is held to {MAX_PLOT_H_MM:g} mm high; a shorter cut or a wider box (w_mm) shows it at true scale")
    kz = plot_h / (top - base)
    x0, y_top = pad + 9.0, pad + title_h + sub_h + 2.0
    y_base = y_top + plot_h
    H = y_base + axis_h + pad

    def X(dd):
        return x0 + dd * k

    def Y(zz):
        return y_base - (zz - base) * kz

    neutral = list(t.get("neutral_ramp") or [t["land"]])
    seq = list(t.get("sequential_ramp") or [t["emphasis"]])
    cat = list(t.get("categorical") or [t["emphasis"]])
    ray_c = cat[4] if len(cat) > 4 else t["emphasis"]
    ink, bg = t["label"], t.get("legend_bg") or t["background"]
    out = [sd.rect(0, 0, w_mm, H, bg, t.get("legend_frame") or t["frame"], 0.2)]
    title = str(cfg.get("title") or "Section " + "–".join(cfg.get("ends") or ["A", "A′"]))
    out.append(sd.text(pad, pad + label_pt * PT, title, label_pt, ink, weight=600))
    sun = prof.get("sun")
    scale_txt = "True scale" if ve == 1 and plot_h >= true_h - 1e-6 else f"Vertical × {ve:g}"
    sub = cfg.get("subtitle") or (scale_txt + (f" · sun {sun['altitude']:g}° above the {compass(sun['azimuth'])} horizon" if sun else ""))
    out.append(sd.text(pad, pad + title_h + (label_pt - 1) * PT, str(sub), label_pt - 1, ink))
    xs, ys = X(d), Y(np.where(ok, z, base))
    ground = "M" + " L".join(f"{sd.num(a_)} {sd.num(b_)}" for a_, b_ in zip(xs, ys)) + f" L{sd.num(xs[-1])} {sd.num(y_base)} L{sd.num(xs[0])} {sd.num(y_base)} Z"
    if sh is not None:
        shd = np.asarray(sh, float)
        in_sh = ok & (shd > z + 0.5)
        # the air in shadow: between the ground and the highest ray, where the ray is above the ground
        segs, i = [], 0
        while i < len(d):
            if in_sh[i]:
                j = i
                while j + 1 < len(d) and in_sh[j + 1]:
                    j += 1
                segs.append((i, j))
                i = j + 1
            else:
                i += 1
        for i, j in segs:
            lo, hi = max(0, i - 1), min(len(d) - 1, j + 1)
            top_line = " L".join(f"{sd.num(X(d[q]))} {sd.num(Y(min(shd[q], top)))}" for q in range(lo, hi + 1))
            bot_line = " L".join(f"{sd.num(X(d[q]))} {sd.num(Y(z[q]))}" for q in range(hi, lo - 1, -1))
            out.append(f'<path d="M{top_line} L{bot_line} Z" fill="{seq[0]}" fill-opacity="0.6" stroke="none"/>')
        out.append(f'<path d="{ground}" fill="{neutral[min(2, len(neutral) - 1)]}" stroke="none"/>')
        # the surface: sunlit edge in the page colour, shaded edge in the ramp's darkest step
        for lit, col, wid in ((True, t["background"], 0.5), (False, seq[-1], 0.5)):
            run = []
            for q in range(len(d)):
                if ok[q] and (not in_sh[q]) == lit:
                    run.append(f"{sd.num(xs[q])} {sd.num(ys[q])}")
                elif run:
                    if len(run) > 1:
                        out.append(f'<path d="M{" L".join(run)}" fill="none" stroke="{col}" stroke-width="{wid}" stroke-linejoin="round"/>')
                    run = []
            if len(run) > 1:
                out.append(f'<path d="M{" L".join(run)}" fill="none" stroke="{col}" stroke-width="{wid}" stroke-linejoin="round"/>')
        # the two longest shadows' grazing rays, from above the ridge that throws them, and the sun
        lens = sorted(segs, key=lambda s: -(d[s[1]] - d[s[0]]))[:2]
        sun_ahead = abs(((sun["azimuth"] - prof["bearing"] + 180) % 360) - 180) < 90
        for i, j in lens:
            far, near = (j, i) if sun_ahead else (i, j)
            src = min(len(d) - 1, far + 1) if sun_ahead else max(0, far - 1)
            out.append(f'<path d="M{sd.num(X(d[src]))} {sd.num(Y(min(shd[src], top)))} L{sd.num(X(d[near]))} {sd.num(Y(z[near]))}" '
                       f'stroke="{ray_c}" stroke-width="0.3" fill="none"/>')
        sx = X(D) - 9.0 if sun_ahead else X(0) + 9.0             # beside the end letter, on the sun's side
        sy = y_top + 2.0
        out.append(f'<circle cx="{sd.num(sx)}" cy="{sd.num(sy)}" r="1.4" fill="{ray_c}" stroke="none"/>')
        for a_ in range(0, 360, 45):
            r_ = math.radians(a_)
            out.append(f'<path d="M{sd.num(sx + 2.0 * math.cos(r_))} {sd.num(sy + 2.0 * math.sin(r_))} L{sd.num(sx + 2.9 * math.cos(r_))} '
                       f'{sd.num(sy + 2.9 * math.sin(r_))}" stroke="{ray_c}" stroke-width="0.25"/>')
        share = float(in_sh[ok].mean()) if ok.any() else 0.0
        notes.append(f"section: {share:.0%} of the cut lies in cast shadow at this sun")
        prof["shadow_share"] = share
    else:
        out.append(f'<path d="{ground}" fill="{neutral[min(2, len(neutral) - 1)]}" stroke="{seq[-1]}" stroke-width="0.4" stroke-linejoin="round"/>')
    # height ticks at the left, distance ticks under the ground
    zt = base
    while zt <= top + 1e-6:
        y = Y(zt)
        out.append(f'<path d="M{sd.num(x0 - 1.0)} {sd.num(y)}h1" stroke="{ink}" stroke-width="0.15"/>')
        out.append(sd.text(x0 - 1.6, y + (label_pt - 2) * PT * 0.35, f"{zt:,.0f}", label_pt - 2, ink, anchor="end", num_cls=True))
        zt += step_z
    out.append(sd.text(x0 - 1.6, y_top - 1.0, "m", label_pt - 2, ink, anchor="end"))
    step_d = _nice_step(D / 1000.0, 4) * 1000.0
    dd = 0.0
    while dd <= D + 1e-6:
        x = X(dd)
        out.append(f'<path d="M{sd.num(x)} {sd.num(y_base)}v1" stroke="{ink}" stroke-width="0.15"/>')
        out.append(sd.text(x, y_base + 1.2 + (label_pt - 2) * PT, f"{dd / 1000:g}" + (" km" if dd + step_d > D else ""), label_pt - 2, ink, anchor="middle", num_cls=True))
        dd += step_d
    ends = list(cfg.get("ends") or ["A", "A′"])
    out.append(sd.text(X(0), y_top - 1.0, str(ends[0]), label_pt, ink, weight=700, anchor="start"))
    out.append(sd.text(X(D), y_top - 1.0, str(ends[-1]), label_pt, ink, weight=700, anchor="end"))
    # names along the cut, and the low point
    a, b = prof["a"], prof["b"]
    for lab in cfg.get("labels") or []:
        try:
            at, off = along_cut(a, b, lab["at"])
        except (KeyError, TypeError, IndexError):
            continue
        if off > 1000.0 or not (0 <= at <= D):
            notes.append(f"section: '{lab.get('text')}' is {off / 1000:.1f} km off the cut or past its ends; not named")
            continue
        zz = float(np.interp(at, d[ok], z[ok]))
        x, y = X(at), Y(zz)
        out.append(f'<path d="M{sd.num(x)} {sd.num(y - 0.6)}v-3" stroke="{ink}" stroke-width="0.15"/>')
        half = tm.width_mm(str(lab.get("text", "")), label_pt - 1, 400) / 2 + 0.5
        out.append(sd.text(min(max(x, x0 + half), X(D) - half), y - 4.2, str(lab.get("text", "")), label_pt - 1, ink, anchor="middle", halo=bg, halo_w=0.6))
    if cfg.get("low_point", True) and ok.any():
        q = int(np.nanargmin(np.where(ok, z, np.inf)))
        name = cfg.get("low_name")
        txt = (f"{name} " if name else "") + f"{z[q]:,.0f} m"
        half = tm.width_mm(txt, label_pt - 1, 400) / 2 + 0.5
        lx = min(max(X(d[q]), x0 + half), X(D) - half)          # kept inside the plot, clear of the height ticks
        out.append(sd.text(lx, Y(z[q]) - 1.4, txt, label_pt - 1, ink, anchor="middle", halo=bg, halo_w=0.6))
    notes.append(f"section: {D / 1000:.1f} km from {ends[0]} to {ends[-1]}, {zmin:,.0f} to {float(np.nanmax(z)):,.0f} m, "
                 + ("true scale" if ve == 1 else f"vertical × {ve:g}") + f", {k * 1000:.2f} mm per km")
    return "".join(out), [0.0, 0.0, round(w_mm, 3), round(H, 3)], notes


def endpoints(cfg: dict, plan) -> tuple[tuple, tuple]:
    line = cfg.get("line")
    if isinstance(line, dict) and line.get("layer"):
        lyr = next((l for l in plan.layers if l.id == line["layer"]), None)
        if lyr is None or lyr.kind != "line" or not len(lyr.gdf):
            raise SectionError(f"elements.section.line.layer '{line['layer']}' is not a line layer of this map")
        g = lyr.gdf.to_crs(WGS84).geometry.iloc[0]
        g = max(g.geoms, key=lambda q: q.length) if g.geom_type == "MultiLineString" else g
        c = list(g.coords)
        return (c[0][0], c[0][1]), (c[-1][0], c[-1][1])
    try:
        (a, b) = line
        return (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    except (TypeError, ValueError):
        raise SectionError("elements.section.line is two lon/lat points [[lon, lat], [lon, lat]] or {layer: <a line layer>}") from None


def for_map(plan, cfg: dict) -> dict:
    """Everything the render needs from elements.section: {figure: a place_figures item, line: (a, b), ends, notes}. The DEM is the section's
    own (cfg.dem, beside the MapSpec) or the hillshade's; the sun the hillshade's one sun (sun: auto), the one given, or none."""
    a, b = endpoints(cfg, plan)
    dem = cfg.get("dem")
    relief = next((x for x in (plan.basemap or []) if x.get("kind") == "hillshade"), None)
    if dem:
        p = Path(dem)
        dem = str(p if p.is_absolute() else plan.spec_path.parent / p)
    elif relief is not None:
        dem = relief.get("dem_path")
    if not dem or not Path(dem).is_file():
        raise SectionError("elements.section needs a DEM: give elements.section.dem, or draw a role: hillshade layer")
    s = cfg.get("sun", "auto")
    sun = None
    if isinstance(s, dict):
        sun = {"azimuth": float(s.get("azimuth", 315)), "altitude": float(s.get("altitude", 45))}
    elif s == "auto" and relief is not None and relief.get("sun") and not relief["sun"].get("multidirectional"):
        sun = {"azimuth": relief["sun"]["azimuth"], "altitude": relief["sun"]["altitude"]}
    prof = compute(dem, a, b, sun)
    prof["a"], prof["b"] = a, b
    inner, box, notes = svg(prof, cfg, plan.tokens, float(cfg.get("w_mm", 120.0)))
    ends = list(cfg.get("ends") or ["A", "A′"])
    item = {"_svg": inner, "_box": box, "title": str(cfg.get("title") or "Section"), "position": cfg.get("position", "lower-left"),
            "w_mm": box[2], "h_mm": box[3]}
    if cfg.get("rect_mm"):
        item["rect_mm"] = cfg["rect_mm"]
    return {"figure": item, "line": (a, b), "ends": ends, "notes": prof["notes"] + notes, "profile": prof}


def map_line(plan, frame, a, b, ends, t: dict, label_pt: float, to_sheet) -> str:
    """The cut on the map: a dashed line in the section's ray colour and its end letters, haloed like the map's labels."""
    import geopandas as gpd
    import shapely
    cat = list(t.get("categorical") or [t["emphasis"]])
    col = cat[4] if len(cat) > 4 else t["emphasis"]
    g = gpd.GeoDataFrame(geometry=[shapely.LineString([a, b])], crs=WGS84).to_crs(plan.crs)
    geoms, keep = to_sheet(g, frame, False)
    if not len(geoms) or not keep[0] or geoms[0] is None or geoms[0].is_empty:
        return ""
    ln = geoms[0]
    ln = max(ln.geoms, key=lambda q: q.length) if ln.geom_type == "MultiLineString" else ln
    c = list(ln.coords)
    out = [f'<g class="gm-section-line"><path d="M{" L".join(f"{sd.num(x)} {sd.num(y)}" for x, y in c)}" fill="none" stroke="{col}" '
           f'stroke-width="0.4" stroke-dasharray="1.6 0.9" stroke-linecap="round"/>']
    (x0_, y0_), (x1_, y1_) = c[0], c[-1]
    dx, dy = x1_ - x0_, y1_ - y0_
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    for (x, y), lab, s in (((x0_, y0_), ends[0], -1), ((x1_, y1_), ends[-1], 1)):
        out.append(sd.text(x + s * ux * 2.2, y + s * uy * 2.2 + label_pt * PT * 0.35, str(lab), label_pt, t["label"], weight=700, anchor="middle",
                           halo=t["background"], halo_w=0.5))
    out.append("</g>")
    return "".join(out)
