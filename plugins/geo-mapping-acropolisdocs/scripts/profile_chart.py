#!/usr/bin/env python3
"""profile_chart: a terrain profile chart for one link, in the brand (TODO from M12; M15).

    profile_chart.py --dem dem.tif --a lon,lat --b lon,lat --ha 30 --hb 30 [--freq 11] [--k 1.333] [--name-a A] [--name-b B]
                     [--title TEXT] [--dem-credit TEXT] --out DIR [--formats svg,pdf,png] [--size letter|slide] [--brand SLUG]
    profile_chart.py --spec MAP.yaml --layer LINKS --link A-B --out DIR [...]      a link a line_of_sight op drew (its DEM, heights, k, frequency)
    terrain.py profile-chart ...                                                   the same, from the terrain command

What it draws, distance from A on x and height above sea level on y:
  * the ground: bare-earth elevation sampled along the WGS84 geodesic (terrain.line_of_sight's samples, one per DEM cell)
  * the line-of-sight ray from A's antenna to B's, with earth curvature applied: drawn over the true ground, the straight ray sags by the
    earth bulge d1 x d2 / (2 k R) (k = 4/3 by default), which is the same clearance terrain.line_of_sight computes
  * the first Fresnel zone around the ray when a frequency is given (F1 = 17.32 x sqrt(d1 d2 / (f D)), 60 percent must be clear)
  * the obstruction (a cross and its height above the ray) or, on a clear path, the point of least clearance (a diamond), never colour alone
  * the masts at A and B with their antenna heights
  * the source line: the DEM's credit and cell size, the bare-earth caveat, the k factor; the brand's logo and the date stamp

Every colour is a brand token, every word Inter; the hidden accreditation is in the metadata only. The chart is checked like a map (palette,
fonts, stamps) and MAP-TER-006 reads the SVG back for the caveat and the DEM credit.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import geo_locale as gl  # noqa: E402
import svg_draw as sd  # noqa: E402
import textmeasure as tm  # noqa: E402

PT = sd.PT
SIZES = {"letter": (279.4, 215.9, {"title": 20, "sub": 10.5, "label": 7.5, "axis": 8, "note": 6.5}),
         "slide": (338.67, 190.5, {"title": 26, "sub": 13, "label": 10, "axis": 10.5, "note": 9}),
         # a web page's popup (M15 web): 160 x 110 mm is about 600 px wide on screen, so its type is set larger for its size
         "web": (160.0, 110.0, {"title": 12, "sub": 8, "label": 7, "axis": 7.5, "note": 5.5, "m": 6.0, "foot": 19.0, "foot_y": 11.0, "logo": 6.0})}
CAVEAT = ("Bare earth: buildings, trees and other clutter are not in the DEM, so a clear path is optimistic; a ridge narrower than a cell can be missed.")


class ChartError(ValueError):
    pass


def _nice_step(span: float, target: int = 6) -> float:
    raw = span / max(target, 1)
    e = math.floor(math.log10(raw)) if raw > 0 else 0
    for m in (1, 2, 2.5, 5, 10):
        if m * 10 ** e >= raw:
            return m * 10 ** e
    return 10 ** (e + 1)


def dem_credit(rec: dict | None, dem_path) -> str:
    import terrain_fetch as tf
    src = (rec or {}).get("source")
    if src == "3dep":
        return tf.THREEDEP_ATTRIBUTION
    if src == "terrarium":
        return tf.TERRARIUM_ATTRIBUTION
    return f"Elevation: a DEM supplied for this chart ({Path(str(dem_path)).name})."


def series(r: dict) -> dict:
    """The arrays the chart draws, from a terrain.line_of_sight result: distance (km), ground, the ray over the true ground with the earth bulge
    applied, and the Fresnel radius."""
    import terrain as te
    d = np.array([p[0] for p in r["profile"]], dtype=float)
    z = np.array([p[1] for p in r["profile"]], dtype=float)
    D = float(r["length_m"])
    za, zb = r["elevation_a_m"] + r["h_a_m"], r["elevation_b_m"] + r["h_b_m"]
    bulge = te.curvature_drop_m(d, D, r["k_factor"])
    ray = za + (zb - za) * d / D - bulge
    f1 = np.array([te.fresnel_radius_m(x, D - x, r["freq_ghz"]) for x in d]) if r.get("freq_ghz") else None
    return {"d_km": d / 1000.0, "z": z, "ray": ray, "f1": f1, "bulge_mid": float(te.curvature_drop_m(D / 2, D, r["k_factor"]))}


def subtitle(r: dict, names=("A", "B")) -> str:
    km = r["length_m"] / 1000
    w = r["worst"]
    na = names[0]
    if r["clear"]:
        s = f"Clear over {km:.2f} km: the ray passes {r['min_clearance_m']:.1f} m above the ground at its closest, {w['distance_m'] / 1000:.2f} km from {na}"
    else:
        s = f"Obstructed: the ground rises {r['obstruction_height_m']:.1f} m above the ray {w['distance_m'] / 1000:.2f} km from {na} ({km:.2f} km path)"
        s += (f"; an antenna of {r['required_h_a_m']:.1f} m at {na} would clear it" if r.get("required_h_a_m") is not None
              else f"; no antenna up to 1,000 m at {na} clears it")
    if r.get("freq_ghz"):
        if not r["clear"]:
            s += f". The first Fresnel zone at {r['freq_ghz']:g} GHz is blocked with it"
        elif (r.get("fresnel_clearance_ratio") or 0) >= 1.0:
            s += f". The first Fresnel zone at {r['freq_ghz']:g} GHz is fully clear of the ground"
        else:
            s += (f". First Fresnel zone at {r['freq_ghz']:g} GHz: {'clear enough' if r.get('fresnel_ok') else 'not clear enough'}, "
                  f"{max(r.get('fresnel_clearance_ratio') or 0, 0):.0%} of its radius clear where it is tightest (60% needed)")
    return s + "."


def draw(r: dict, *, brand: str | None = None, title: str | None = None, names=("A", "B"), credit: str | None = None, size: str = "letter",
         chart_id: str = "profile") -> dict:
    """The chart as an SVG master. -> {svg, title, alt, brand, size_mm, findings}."""
    import brandlib as bl
    import map_render as mr
    if size not in SIZES:
        raise ChartError(f"size is letter or slide, not {size}")
    W, H, fs = SIZES[size]
    manifest = gc.build_manifest()
    slug = bl.resolve(manifest, None, brand)["brand"]
    pack = bl.load_pack(slug)
    t = pack["tokens"]["modes"]["print"]
    s = series(r)
    na, nb = names
    title = title or f"Path profile: {na} to {nb}"
    sub = subtitle(r, names)
    m = fs.get("m", 12.0)
    out = [sd.rect(0, 0, W, H, t["background"])]
    # ---- title block
    y = m + fs["title"] * PT * 0.9
    out.append(sd.text(m, y, title, fs["title"], t["label"], weight=700, extra=' id="gm-title-text"'))
    sub_lines = tm.wrap(sub, W - 2 * m, fs["sub"], 400)[:2]
    for ln in sub_lines:
        y += fs["sub"] * PT * 1.35
        out.append(sd.text(m, y, ln, fs["sub"], t["label"], extra=' data-role="verdict"'))
    # ---- plot box
    ylab_w = tm.width_mm("0,000", fs["label"], 400, tnum=True) + 2.0
    px0, px1 = m + fs["axis"] * PT * 1.6 + ylab_w, W - m
    py0, py1 = y + 8.0, H - m - fs.get("foot", 26.0)
    d, z, ray = s["d_km"], s["z"], s["ray"]
    tops = [ray.max(), z.max()] + ([float((ray + s["f1"]).max())] if s["f1"] is not None else [])
    bots = [z.min()] + ([float((ray - s["f1"]).min())] if s["f1"] is not None else [])
    ylo, yhi = min(bots), max(tops)
    pad = max((yhi - ylo) * 0.12, 5.0)
    ystep = _nice_step(yhi - ylo + 2 * pad, 6)
    ylo, yhi = math.floor((ylo - pad) / ystep) * ystep, math.ceil((yhi + 2.2 * pad) / ystep) * ystep      # room above the ray for the labels
    xmax = float(d[-1])
    xstep = _nice_step(xmax, 8)

    def X(v):
        return px0 + (np.asarray(v, dtype=float) / xmax) * (px1 - px0)

    def Y(v):
        return py1 - (np.asarray(v, dtype=float) - ylo) / (yhi - ylo) * (py1 - py0)

    def poly(xs, ys):
        return "M" + "L".join(f"{sd.num(a)} {sd.num(b)}" for a, b in zip(xs, ys, strict=True))
    out.append('<g class="gm-chart-grid">')
    v = ylo
    while v <= yhi + 1e-9:
        out.append(f'<path d="M{sd.num(px0)} {sd.num(Y(v))}H{sd.num(px1)}" stroke="{t["legend_frame"]}" stroke-width="0.15" fill="none"/>')
        out.append(sd.text(px0 - 1.5, float(Y(v)) + fs["label"] * PT * 0.35, f"{v:,.0f}", fs["label"], t["label"], anchor="end", num_cls=True))
        v += ystep
    v = 0.0
    while v <= xmax + 1e-9:
        out.append(f'<path d="M{sd.num(X(v))} {sd.num(py0)}V{sd.num(py1)}" stroke="{t["legend_frame"]}" stroke-width="0.15" fill="none"/>')
        lab = f"{v:g}" if xstep >= 1 else f"{v:.1f}"
        out.append(sd.text(float(X(v)), py1 + 1.5 + fs["label"] * PT * 0.9, lab, fs["label"], t["label"], anchor="middle", num_cls=True))
        v += xstep
    out.append("</g>")
    out.append(sd.text(px0 + (px1 - px0) / 2, py1 + 3.5 + fs["label"] * PT * 0.9 + fs["axis"] * PT * 1.2, f"Distance from {na} (km)", fs["axis"], t["label"],
                       weight=600, anchor="middle"))
    ycx, ycy = m + fs["axis"] * PT * 0.8, py0 + (py1 - py0) / 2
    out.append(sd.text(ycx, ycy, "Height above sea level (m)", fs["axis"], t["label"], weight=600, anchor="middle", rotate=-90))
    out.append(f'<defs><clipPath id="gm-plot"><rect x="{sd.num(px0)}" y="{sd.num(py0)}" width="{sd.num(px1 - px0)}" height="{sd.num(py1 - py0)}"/></clipPath></defs>')
    out.append('<g clip-path="url(#gm-plot)">')
    seq = t["sequential_ramp"]
    ground = poly(X(d), Y(z)) + f"L{sd.num(px1)} {sd.num(py1)}L{sd.num(px0)} {sd.num(py1)}Z"
    out.append(f'<path d="{ground}" fill="{t["neutral_ramp"][1]}" stroke="{t["label"]}" stroke-width="0.35" stroke-linejoin="round" data-role="ground"/>')
    if s["f1"] is not None:                                   # the first Fresnel zone around the ray, over the ground so its intrusion shows
        up, lo = ray + s["f1"], ray - s["f1"]
        dd = poly(X(d), Y(up)) + "L" + "L".join(f"{sd.num(a)} {sd.num(b)}" for a, b in zip(X(d[::-1]), Y(lo[::-1]), strict=True)) + "Z"
        out.append(f'<path d="{dd}" fill="{seq[0]}" fill-opacity="0.22" stroke="{seq[2]}" stroke-width="0.25" stroke-dasharray="1.2 0.8" data-role="fresnel"/>')
    out.append(f'<path d="{poly(X(d), Y(ray))}" fill="none" stroke="{t["emphasis"]}" stroke-width="0.6" data-role="ray"/>')
    out.append("</g>")
    out.append(sd.rect(px0, py0, px1 - px0, py1 - py0, "none", t["frame"], 0.3))
    # ---- key, upper right inside the plot (placed first: the labels below keep off it)
    items = [("ground", "Ground (bare earth)"), ("ray", f"Line of sight, earth curvature applied (k = {r['k_factor']:.3g})")]
    if s["f1"] is not None:
        items.append(("fresnel", f"First Fresnel zone, {r['freq_ghz']:g} GHz"))
    kw = max(tm.width_mm(lab, fs["label"], 400) for _, lab in items) + 12.0
    kh = len(items) * (fs["label"] * PT * 1.6) + 3.0
    kx, ky = px1 - kw - 2.0, py0 + 2.0
    key_box = (kx - 1.0, ky - 1.0, kx + kw + 1.0, ky + kh + 1.0)
    taken = [key_box]                                       # the key and every label placed so far
    gx, gy, ry_ = np.asarray(X(d), dtype=float), np.asarray(Y(z), dtype=float), np.asarray(Y(ray), dtype=float)
    fz = (np.asarray(Y(ray + s["f1"]), dtype=float), np.asarray(Y(ray - s["f1"]), dtype=float)) if s["f1"] is not None else None   # (top, bottom) in mm
    cap = fs["label"] * PT * 0.75

    def free(x0, x1, base) -> bool:
        """A label from x0 to x1 with its baseline at `base` sits in the sky: inside the plot, off the key, above the ground and off the ray."""
        top, bot = base - cap - 0.4, base + 0.6
        if x0 < px0 + 0.5 or x1 > px1 - 0.5 or top < py0 + 0.5 or bot > py1 - 0.5:
            return False
        if any(not (x1 < b[0] or x0 > b[2] or bot < b[1] or top > b[3]) for b in taken):
            return False
        sel = (gx >= x0 - 0.5) & (gx <= x1 + 0.5)
        if sel.any() and fz is not None and ((fz[1][sel] >= top - 0.4) & (fz[0][sel] <= bot + 0.4)).any():
            return False                                     # off the Fresnel zone as well
        if sel.any() and (bot >= gy[sel].min() - 0.4 or ((ry_[sel] >= top - 0.4) & (ry_[sel] <= bot + 0.4)).any()):
            return False
        return True
    # ---- masts and antenna heights; each label above its mast, as low as it can go without touching the ray, the ground or the key
    for k, (nm, h, z0) in enumerate(((na, r["h_a_m"], z[0]), (nb, r["h_b_m"], z[-1]))):
        xx = float(X(d[0] if k == 0 else d[-1]))
        y0, y1 = float(Y(z0)), float(Y(z0 + h))
        out.append(f'<path d="M{sd.num(xx)} {sd.num(y0)}V{sd.num(y1)}" stroke="{t["label"]}" stroke-width="0.7" data-role="mast"/>')
        out.append(f'<circle cx="{sd.num(xx)}" cy="{sd.num(y1)}" r="0.9" fill="{t["emphasis"]}" stroke="{t["background"]}" stroke-width="0.2"/>')
        txt = f"{nm}: antenna {h:g} m above ground"
        tw = tm.width_mm(txt, fs["label"], 600, tnum=True)
        lx0 = xx + 1.6 if k == 0 else xx - 1.6 - tw
        base = next((y1 - off for off in np.arange(2.0, 40.0, 1.0) if free(lx0, lx0 + tw, y1 - off)), y1 - 2.0)
        if base < y1 - 2.5:                                  # lifted clear of the ray: a short leader back to the antenna
            out.append(f'<path d="M{sd.num(xx)} {sd.num(y1 - 1.0)}V{sd.num(base + 0.8)}" stroke="{t["label"]}" stroke-width="0.15" stroke-dasharray="0.5 0.35"/>')
        taken.append((lx0 - 0.8, base - cap - 0.8, lx0 + tw + 0.8, base + 0.8))
        out.append(sd.text(lx0, base, txt, fs["label"], t["label"], weight=600, halo=t["background"], halo_w=0.5, num_cls=True, extra=' data-role="antenna"'))
    # ---- the obstruction (or the least clearance): the marker on the ground, a dashed line from the ray to it, the label in the sky beside it
    w = r["worst"]
    wx, wz = float(X(w["distance_m"] / 1000)), float(Y(w["terrain_m"]))
    wr = float(np.interp(w["distance_m"] / 1000, d, ray))
    out.append(f'<path d="M{sd.num(wx)} {sd.num(float(Y(wr)))}V{sd.num(wz)}" stroke="{t["status"]["danger"] if not r["clear"] else t["label"]}" '
               f'stroke-width="0.25" stroke-dasharray="0.8 0.5" data-role="gap"/>')
    if not r["clear"]:
        c, s_ = t["status"]["danger"], 1.4
        out.append(f'<path d="M{sd.num(wx - s_)} {sd.num(wz - s_)}l{sd.num(2 * s_)} {sd.num(2 * s_)}M{sd.num(wx + s_)} {sd.num(wz - s_)}l{sd.num(-2 * s_)} {sd.num(2 * s_)}" '
                   f'stroke="{c}" stroke-width="0.7" data-role="obstruction"/>')
        msg = f"Obstruction: the ground is {r['obstruction_height_m']:.1f} m above the ray, {w['distance_m'] / 1000:.2f} km from {na}"
    else:
        c = t["label"]
        out.append(f'<path d="{sd.symbol_path("diamond", wx, wz, 2.4)}" fill="{t["background"]}" stroke="{c}" stroke-width="0.4" data-role="least-clearance"/>')
        msg = f"Least clearance: {r['min_clearance_m']:.1f} m, {w['distance_m'] / 1000:.2f} km from {na}"
    lw = tm.width_mm(msg, fs["label"], 600, tnum=True)
    top_y = min(wz, float(Y(wr)))
    cands = []
    for off in np.arange(3.0, 60.0, 1.5):                   # above the marker: left, right or centred, as low as is free
        for x0 in (wx - 2.0 - lw, wx + 2.0, wx - lw / 2):
            cands.append((x0, top_y - off))
    lx, ly = next(((x0, b) for x0, b in cands if free(x0, x0 + lw, b)), (min(max(wx - lw / 2, px0 + 2), px1 - 2 - lw), top_y - 3.0))
    ax = min(max(wx, lx), lx + lw)
    out.append(f'<path d="M{sd.num(wx)} {sd.num(top_y - 1.6)}L{sd.num(ax)} {sd.num(ly + 0.8)}" stroke="{c}" stroke-width="0.2"/>')
    out.append(sd.text(lx, ly, msg, fs["label"], c, weight=600, halo=t["background"], halo_w=0.6, num_cls=True, extra=' data-role="obstruction-label"'))
    out.append(sd.rect(kx, ky, kw, kh, t["legend_bg"], t["legend_frame"], 0.2))
    for i, (kind, lab) in enumerate(items):
        yy = ky + 1.5 + (i + 0.5) * fs["label"] * PT * 1.6
        if kind == "ground":
            out.append(sd.rect(kx + 2, yy - 1.4, 6, 2.8, t["neutral_ramp"][1], t["label"], 0.3))
        elif kind == "ray":
            out.append(f'<path d="M{sd.num(kx + 2)} {sd.num(yy)}h6" stroke="{t["emphasis"]}" stroke-width="0.6"/>')
        else:
            out.append(sd.rect(kx + 2, yy - 1.4, 6, 2.8, seq[0], seq[2], 0.25, 'fill-opacity="0.22" stroke-dasharray="1.2 0.8"'))
        out.append(sd.text(kx + 10, yy + fs["label"] * PT * 0.35, lab, fs["label"], t["label"]))
    # ---- source line, logo, date
    credit = credit or dem_credit(None, r.get("dem", ""))
    notes = [f"{credit} About {r['dem_resolution_m']:.0f} m cells, sampled along the WGS84 geodesic every {r['spacing_m']:.0f} m.",
             CAVEAT + f" Earth curvature with k = {r['k_factor']:.3g}: the ray sags {s['bulge_mid']:.1f} m at mid-path against the ground."]
    tm_line = pack["pack"].get("trademark_line")
    if tm_line:
        notes.append(tm_line)
    logo_h = fs.get("logo", 9.0)
    plan_like = SimpleNamespace(manifest=manifest, brand=slug, brand_name=bl.brand_name(slug), pack=pack, crs="EPSG:4326", tpl_name=f"profile-{size}",
                                spec={"id": chart_id, "message": sub})
    logo, lw_ = mr.logo_markup(plan_like, t["background"], 0, 0, logo_h)
    fy = H - m - fs.get("foot_y", 14.0)
    nw = W - 2 * m - lw_ - 8
    yy = fy + fs["note"] * PT
    roles = ["credit", "caveat", "trademark"]
    for k, ln in enumerate(notes):
        for wl in tm.wrap(ln, nw, fs["note"], 400):
            out.append(sd.text(m, yy, wl, fs["note"], t["label"], extra=f' data-role="{roles[min(k, 2)]}"'))
            yy += tm.line_height_mm(fs["note"])
    if logo:
        out.append(mr.logo_markup(plan_like, t["background"], W - m - lw_, fy - 1.0, logo_h)[0])
    stamp = f"Chart produced {gl.plan_date_text(plan_like, mr.today())}"
    out.append(sd.text(W - m, H - m + 1.0, stamp, fs["note"], t["label"], anchor="end", num_cls=True))
    alt = (f"Terrain profile from {na} to {nb}, {r['length_m'] / 1000:.2f} km: {sub} The ground runs from {z.min():,.0f} to {z.max():,.0f} m above sea level; "
           f"antennas {r['h_a_m']:g} m at {na} and {r['h_b_m']:g} m at {nb}.")
    svg = mr.assemble(plan_like, W, H, "".join(out), None, None, title, alt, False)
    return {"svg": svg, "title": title, "alt": alt, "brand": slug, "size_mm": (W, H), "findings": lint_chart(svg, slug, manifest)}


def lint_chart(svg: str, slug: str, manifest: dict) -> list[dict]:
    """The map guards on the chart (palette, Inter, stamps, hidden accreditation, SVG hygiene) and MAP-TER-006: read back, the chart states the
    bare-earth caveat and the DEM credit."""
    import re
    import brand_lint
    import output_lint
    fs = (brand_lint.check_palette(svg, slug) + brand_lint.check_fonts(svg) + brand_lint.check_stamps(svg, slug, manifest)
          + brand_lint.check_hidden_accreditation(svg, slug, manifest) + output_lint.check_svg(svg))
    text = " ".join(re.findall(r'data-role="(?:caveat|credit)"[^>]*>([^<]*)<', svg))
    if "Bare earth" not in text:
        fs.append({"id": "MAP-TER-006", "severity": "BLOCK", "message": "the profile chart does not state that the DEM is bare earth (clutter is not modelled)", "detail": None})
    if "Elevation" not in text:
        fs.append({"id": "MAP-TER-006", "severity": "BLOCK", "message": "the profile chart does not credit its DEM in the source line", "detail": None})
    return fs


def from_spec(spec_path, layer_id: str, link: str):
    """A link a line_of_sight op drew in a MapSpec: its ends, heights, k, frequency, DEM and the DEM's record. -> (lon/lat a, b, kwargs, names, rec)."""
    import map_plan as mp
    spec_path = Path(spec_path)
    plan = mp.prepare(spec_path)
    lay = next((l for l in plan.layers + list(plan.helpers or []) if l.id == layer_id), None)
    if lay is None:
        raise ChartError(f"the MapSpec has no layer '{layer_id}'")
    ops = [o for o in (lay.spec.get("ops") or []) if isinstance(o, dict) and str(o.get("op", "")).lower() == "line_of_sight"]
    if not ops or "a" not in lay.gdf.columns:
        raise ChartError(f"layer '{layer_id}' is not drawn by a line_of_sight op")
    op = ops[-1]
    a_id, _, b_id = str(link).partition("-")
    g = lay.gdf.to_crs("EPSG:4326")
    hit = g[(g["a"].astype(str) == a_id) & (g["b"].astype(str) == b_id)]
    if not len(hit):
        hit = g[(g["a"].astype(str) == b_id) & (g["b"].astype(str) == a_id)]
    if not len(hit):
        links = ", ".join(f"{x}-{y}" for x, y in zip(g["a"], g["b"]))
        raise ChartError(f"no link '{link}' in layer '{layer_id}' (links: {links[:300]})")
    row = hit.iloc[0]
    cs = list(row.geometry.coords)
    pa, pb = cs[0], cs[-1]
    base = spec_path.parent
    prov = next((p for p in (lay.ops_provenance or []) if p.get("op") == "line_of_sight"), {}) or {}
    if op.get("dem") and str(op["dem"]) != "auto":
        dem = Path(op["dem"]) if Path(op["dem"]).is_absolute() else base / op["dem"]
        rec = {"source": "user"}
    else:
        import terrain as te
        lon = [pa[0], pb[0]]
        lat = [pa[1], pb[1]]
        dem, rec = mp.ops_dem_resolver(plan.spec, base)((min(lon) - 0.02, min(lat) - 0.02, max(lon) + 0.02, max(lat) + 0.02), float(op.get("dem_res_m", 30)))
        _ = te
    kw = {"h_a_m": float(row["h_a_m"]), "h_b_m": float(row["h_b_m"]), "k_factor": float(op.get("k", prov.get("k_factor") or 4 / 3)),
          "freq_ghz": float(op["freq_ghz"]) if op.get("freq_ghz") is not None else None}
    return pa, pb, str(dem), kw, (str(row["a"]), str(row["b"])), rec


def write(chart: dict, out_dir, stem: str, formats: list[str], dpi: float = 300) -> list[str]:
    import map_brand
    import map_export as mx
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    if "svg" in formats:
        p = out_dir / f"{stem}.svg"
        p.write_text(chart["svg"], encoding="utf-8")
        files.append(str(p))
    rest = [f for f in formats if f in ("pdf", "png")]
    if rest:
        props = {f: map_brand.props_for(chart["brand"], gc.build_manifest(), f) for f in ("pdf", "png")}
        ex = mx.export(chart["svg"], out_dir, stem, rest, props=props, title=chart["title"], alt=chart["alt"], dpi=dpi, subject=chart["alt"][:200])
        files += ex["files"]
    return files


def main(argv=None) -> int:
    import terrain as te
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dem")
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--ha", type=float, default=30.0)
    ap.add_argument("--hb", type=float, default=30.0)
    ap.add_argument("--k", type=float, default=te.K_DEFAULT)
    ap.add_argument("--freq", type=float)
    ap.add_argument("--name-a", default="A")
    ap.add_argument("--name-b", default="B")
    ap.add_argument("--spec")
    ap.add_argument("--layer")
    ap.add_argument("--link", help="A-B: the a and b site ids of a link a line_of_sight op drew")
    ap.add_argument("--title")
    ap.add_argument("--dem-credit")
    ap.add_argument("--out", required=True)
    ap.add_argument("--formats", default="svg,pdf,png")
    ap.add_argument("--size", default="letter", choices=list(SIZES))
    ap.add_argument("--dpi", type=float, default=300)
    ap.add_argument("--brand")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):                      # --a -112.07,33.41: a western longitude looks like an option to argparse; join it
        if argv[i] in ("--a", "--b") and argv[i + 1].startswith("-"):
            argv[i], argv[i + 1] = argv[i] + "=" + argv[i + 1], "--=joined"
    argv = [x for x in argv if x != "--=joined"]
    a = ap.parse_args(argv)
    try:
        rec = None
        if a.spec:
            if not (a.layer and a.link):
                raise ChartError("--spec needs --layer (the line_of_sight layer) and --link A-B")
            pa, pb, dem, kw, names, rec = from_spec(a.spec, a.layer, a.link)
        else:
            if not (a.dem and a.a and a.b):
                raise ChartError("give --dem dem.tif --a lon,lat --b lon,lat (or --spec MAP.yaml --layer LINKS --link A-B)")
            pa, pb, dem = a.a, a.b, a.dem
            kw = {"h_a_m": a.ha, "h_b_m": a.hb, "k_factor": a.k, "freq_ghz": a.freq}
            names = (a.name_a, a.name_b)
        r = te.line_of_sight(dem, pa, pb, **kw)
        chart = draw(r, brand=a.brand, title=a.title, names=names, credit=a.dem_credit or dem_credit(rec, dem), size=a.size,
                     chart_id=f"profile-{names[0]}-{names[1]}")
        stem = f"profile_{names[0]}-{names[1]}_{a.size}".replace(" ", "_")
        files = write(chart, a.out, stem, [f.strip() for f in a.formats.split(",") if f.strip()], a.dpi)
    except (ChartError, te.TerrainError) as ex:
        print(f"profile_chart: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    except Exception as ex:  # noqa: BLE001
        import map_plan as mp
        if isinstance(ex, mp.PlanError):
            print(f"profile_chart: {ex}", file=sys.stderr)
            return ex.code
        raise
    rep = {"verdict": te.verdict_line(r), "files": files, "findings": chart["findings"]}
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in chart["findings"]) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
