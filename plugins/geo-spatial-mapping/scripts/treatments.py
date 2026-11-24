"""treatments: the treatments of a style pack (1.4.0, R2-TREAT). Vector and deterministic (seeded by the map id and the feature), no SVG filters.

    paper           one raster image under the map: the pack's seeded-noise paper (tools/make_paper.py, CC0), repeated as a 512 px tile in an
                    SVG <pattern> (spike S1, 1.4.0: WeasyPrint draws patterns with images; a tabloid PDF grows by about 16 KB). The ground
                    and the land are drawn see-through over it, so the texture shows on the map.
    water_lining    rings in the water parallel to the coast: ring k is the boundary of land.buffer(d_k) clipped to the water; widths and
                    opacity taper. Rings x coast vertices over 2 million are simplified first (MAP-STYLE-006).
    hill_glyphs     pictorial hills placed from an elevation model on a grid (spacing_mm) at local maxima of relative relief, three sizes by
                    prominence; after the data, before the labels, which treat them as obstacles. No elevation model: MAP-STYLE-007.
    place_glyphs    a tower cluster for capitals and building clusters in three tiers in place of the dots of named places.
    line_character  sketch: perpendicular noise of about 0.3 mm at a wavelength of about 4 mm on every line and outline, seeded per path,
                    with an optional second stroke over open lines; fills become jittered hatches. The data outputs are never jittered.
    grid            a light measuring grid over the map at spacing_mm (blueprint).
"""
from __future__ import annotations

import base64
import hashlib
import math
import re

import numpy as np

import svg_draw as sd

PAPER_DPI = 150
COAST_CAP = 2_000_000
GLYPH_MM = {"capital": 3.2, "city": 2.6, "town": 2.0, "village": 1.6}
DEFAULTS = {
    "water_lining": {"rings": 5, "first_mm": 0.6, "step_mm": 0.7, "width_mm": [0.25, 0.10], "lakes": True},
    "hill_glyphs": {"spacing_mm": 6.0},
    "line_character": {"style": "sketch", "amplitude_mm": 0.3, "wavelength_mm": 4.0, "overdraw": True, "hatch_fills": True},
    "grid": {"spacing_mm": 10.0, "opacity": 0.35},
}


def merged(style, spec: dict) -> dict:
    """The treatments of a build: the pack's, then the MapSpec's style.treatments key by key (false turns one off; true takes the defaults)."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in ((style.treatments if style is not None else {}) or {}).items()}
    own = ((spec.get("style") or {}).get("treatments") or {}) if isinstance(spec.get("style"), dict) else {}
    for k, v in own.items():
        if v is False:
            out.pop(k, None)
        elif v is True:
            out.setdefault(k, {})
        elif isinstance(v, dict):
            out[k] = {**(out.get(k) or {}), **v}
    for k in list(out):
        if out[k] is True:
            out[k] = {}
        out[k] = {**DEFAULTS.get(k, {}), **(out[k] or {})}
    return out


def _seed(*parts) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


# ---------------------------------------------------------------------------------------------------------------- paper
def paper_markup(style, cfg: dict, W: float, H: float) -> str:
    """The pack's paper over the whole sheet as one <pattern> of its tile (a raster image, never a filter)."""
    p = style.path(cfg["image"]) if style is not None else None
    if p is None or not p.is_file():
        return ""
    raw = p.read_bytes()
    try:
        from PIL import Image
        import io
        px = Image.open(io.BytesIO(raw)).size[0]
    except Exception:  # noqa: BLE001
        px = int(cfg.get("tile_px") or 512)
    tile = px / float(cfg.get("dpi") or PAPER_DPI) * 25.4
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return (f'<g class="gm-paper"><defs><pattern id="gm-paper" patternUnits="userSpaceOnUse" width="{sd.num(tile)}" height="{sd.num(tile)}">'
            f'<image width="{sd.num(tile)}" height="{sd.num(tile)}" preserveAspectRatio="none" href="data:{mime};base64,{b64}"/></pattern></defs>'
            f'<rect x="0" y="0" width="{sd.num(W)}" height="{sd.num(H)}" fill="url(#gm-paper)"/></g>')


# ---------------------------------------------------------------------------------------------------------------- water-lining
def _land_and_water(plan, frame, lakes: bool):
    """The map's own ground in display units, cut to a box a little wider than the frame: (land, water)."""
    import shapely
    from shapely.geometry import box
    vx0, vy0, vx1, vy1 = frame.visible
    pad = max(vx1 - vx0, vy1 - vy0) * 0.05
    win = box(vx0 - pad, vy0 - pad, vx1 + pad, vy1 + pad)
    land_parts, lake_parts = [], []
    for b in plan.basemap:
        if b.get("kind") in ("land", "country", "state") and b.get("gdf") is not None and len(b["gdf"]):
            land_parts.append(shapely.union_all(shapely.intersection(b["gdf"].geometry.values, win)))
        elif b.get("kind") == "lake" and lakes and b.get("gdf") is not None and len(b["gdf"]):
            lake_parts.append(shapely.union_all(shapely.intersection(b["gdf"].geometry.values, win)))
    if not land_parts:
        return None, None
    land = shapely.make_valid(shapely.union_all(land_parts))
    if lake_parts:
        land = shapely.make_valid(land.difference(shapely.union_all(lake_parts)))
    water = shapely.make_valid(win.difference(land))
    return land, water


def water_lining(plan, frame, cfg: dict, t: dict) -> tuple[str, list[dict], list[str]]:
    """Rings in the water parallel to the coast, thinner and fainter outwards. -> (svg, findings, notes)."""
    import shapely
    from shapely import affinity
    fs, notes = [], []
    land, water = _land_and_water(plan, frame, bool(cfg.get("lakes", True)))
    if land is None or water is None or water.is_empty:
        return "", fs, ["water-lining: no coast in the window"]
    rings = int(cfg.get("rings", 5))
    first, step = float(cfg.get("first_mm", 0.6)), float(cfg.get("step_mm", 0.7))
    w0, w1 = (list(cfg.get("width_mm") or [0.25, 0.10]) + [0.1, 0.1])[:2]
    s = frame.scale
    verts = int(shapely.get_num_coordinates(land))
    tol = min(w0, w1) / 2 / s                          # simplified at half the line width
    if rings * verts > COAST_CAP:
        tol = max(tol, 0.25 / s)
        land = land.simplify(tol, preserve_topology=True)
        fs.append({"id": "MAP-STYLE-006", "severity": "INFO", "message": f"water-lining: the coast has {verts:,} vertices, so it was simplified to "
                   f"{tol * s:.2f} mm before the {rings} rings were drawn (the cap is {COAST_CAP:,} ring vertices)"})
    ink = t.get(cfg.get("token") or "water_line") or t["frame"]
    out = ['<g class="gm-water-lining" fill="none" stroke-linejoin="round" stroke-linecap="round">']
    for k in range(rings):
        d_mm = first + k * step
        ring = land.buffer(d_mm / s, quad_segs=4).boundary.intersection(water)
        if ring.is_empty:
            continue
        ring = ring.simplify(tol, preserve_topology=False)
        sheet = affinity.affine_transform(ring, [s, 0, 0, -s, frame._ox - frame.cx * s, frame._oy + frame.cy * s])
        f = k / max(1, rings - 1)
        w = w0 + (w1 - w0) * f
        op = 0.85 - 0.5 * f
        d = sd.line_path(sheet)
        if d:
            out.append(f'<path d="{d}" stroke="{ink}" stroke-width="{sd.num(w)}" stroke-opacity="{sd.num(op)}" data-ring="{k + 1}"/>')
    out.append("</g>")
    notes.append(f"water-lining: {rings} rings from {first:g} mm, {step:g} mm apart")
    return "".join(out), fs, notes


# ---------------------------------------------------------------------------------------------------------------- hill glyphs
def hill_path(x: float, y: float, w: float) -> tuple[str, str]:
    """A pictorial hill of width w on the baseline (x, y): a rounded outline, and hachures down its south-east side (light from the north-west)."""
    h = w * 0.5
    outline = f"M{sd.num(x - w / 2)} {sd.num(y)}C{sd.num(x - w * 0.32)} {sd.num(y - h * 0.35)} {sd.num(x - w * 0.22)} {sd.num(y - h)} {sd.num(x)} {sd.num(y - h)}" \
              f"C{sd.num(x + w * 0.22)} {sd.num(y - h)} {sd.num(x + w * 0.32)} {sd.num(y - h * 0.35)} {sd.num(x + w / 2)} {sd.num(y)}"
    hach = []
    n = 5
    for i in range(n):
        f = (i + 1) / (n + 1)                          # along the right flank, from the top down
        x0 = x + w * 0.5 * f * 0.95
        y0 = y - h * (1.0 - f ** 1.6) * 0.95
        ln = h * (0.25 + 0.5 * (1 - f))
        hach.append(f"M{sd.num(x0)} {sd.num(y0)}l{sd.num(-ln * 0.18)} {sd.num(ln)}")
    return outline, "".join(hach)


def dem_for(plan, cfg: dict):
    """An elevation model for the window: the treatment's own `dem:` file, else the one the map's terrain fetched (the relief record)."""
    from pathlib import Path
    if cfg.get("dem"):
        p = Path(cfg["dem"])
        if not p.is_absolute():
            p = Path(plan.spec_path).parent / p
        return p if p.is_file() else None
    for b in plan.basemap:
        rec = (b.get("record") or {}) if b.get("kind") == "hillshade" else {}
        for k in ("dem_path", "path"):
            if rec.get(k) and Path(rec[k]).is_file():
                return Path(rec[k])
    for l in plan.layers:
        r = getattr(l, "raster", None) or {}
        for k in ("dem_path", "path"):
            v = r.get(k) if isinstance(r, dict) else None
            if v and str(v).lower().endswith((".tif", ".tiff")) and Path(v).is_file() and l.role in ("hillshade", "contour"):
                return Path(v)
    return None


def hill_glyphs(plan, frame, cfg: dict, t: dict, avoid: list, land=None) -> tuple[str, list, list[dict], list[str]]:
    """Hills on the local maxima of relative relief, sampled on a grid every spacing_mm; three sizes by prominence; never on data or other
    glyphs. -> (svg, rects the labels avoid, findings, notes)."""
    from layout import Rect
    fs, notes = [], []
    dem = dem_for(plan, cfg)
    if dem is None:
        fs.append({"id": "MAP-STYLE-007", "severity": "INFO", "message": "hill glyphs: the map has no elevation model (terrain off and no `dem:` given), so none are drawn"})
        return "", [], fs, notes
    import rasterio
    from pyproj import Transformer
    sp = float(cfg.get("spacing_mm", 6.0))
    r = frame.rect
    xs = np.arange(r.x0 + sp / 2, r.x1, sp)
    ys = np.arange(r.y0 + sp / 2, r.y1, sp)
    X, Y = np.meshgrid(xs, ys)
    dx, dy = frame.to_display(X.ravel(), Y.ravel())
    with rasterio.open(dem) as ds:
        tr = Transformer.from_crs(plan.crs, ds.crs, always_xy=True)
        ex, ey = tr.transform(dx, dy)
        vals = np.array([np.nan if np.ma.is_masked(v[0]) else float(v[0]) for v in ds.sample(zip(ex, ey, strict=True), masked=True)], dtype="float64")
        nod = ds.nodata
    if nod is not None:
        vals[vals == nod] = np.nan
    Z = vals.reshape(X.shape)
    if not np.isfinite(Z).any():
        fs.append({"id": "MAP-STYLE-007", "severity": "INFO", "message": "hill glyphs: the elevation model has no values in this window"})
        return "", [], fs, notes
    from scipy.ndimage import maximum_filter, minimum_filter
    Zf = np.where(np.isfinite(Z), Z, np.nanmin(Z))
    local_max = (Zf == maximum_filter(Zf, size=3)) & np.isfinite(Z)
    relief = Zf - minimum_filter(Zf, size=5)
    pos = relief[local_max & (relief > 0)]
    if not len(pos):
        return "", [], fs, ["hill glyphs: no relief in this window"]
    cut = max(np.percentile(pos, 35), 40.0)
    t1, t2 = np.percentile(pos, 66), np.percentile(pos, 90)
    ink = t.get(cfg.get("token") or "hill_ink") or t["frame"]
    ground = t["land"]
    placed, rects = [], []
    order = np.argsort(-relief.ravel())
    import shapely
    for idx in order:
        iy, ix = divmod(int(idx), X.shape[1])
        if not local_max[iy, ix] or relief[iy, ix] < cut:
            continue
        w = 6.0 if relief[iy, ix] >= t2 else 4.6 if relief[iy, ix] >= t1 else 3.4
        x, y = float(X[iy, ix]), float(Y[iy, ix]) + w * 0.25
        rr = Rect(x - w / 2, y - w * 0.6, x + w / 2, y)
        if rr.x0 < r.x0 or rr.x1 > r.x1 or rr.y0 < r.y0 or rr.y1 > r.y1:
            continue
        if any(rr.hits(o) for o in avoid) or any(rr.hits(o) for o in rects):
            continue
        if land is not None:
            dpx, dpy = frame.to_display(np.array([x]), np.array([y - w * 0.3]))
            if not land.contains(shapely.Point(float(dpx[0]), float(dpy[0]))):
                continue
        o, hch = hill_path(x, y, w)
        placed.append(f'<path d="{o}" fill="{ground}" fill-opacity="0.7" stroke="{ink}" stroke-width="0.22"/><path d="{hch}" stroke="{ink}" stroke-width="0.15"/>')
        rects.append(rr)
    notes.append(f"hill glyphs: {len(placed)} placed from {dem.name}, sampled every {sp:g} mm")
    return '<g class="gm-hill-glyphs" fill="none" stroke-linecap="round">' + "".join(placed) + "</g>", rects, fs, notes


# ---------------------------------------------------------------------------------------------------------------- place glyphs
def place_glyph(tier: str, cx: float, cy: float, ink: str, ground: str) -> str:
    """A town glyph centred on the place: a tower cluster (capital) or one, two or three gabled houses (city, town, village)."""
    s = GLYPH_MM.get(tier, 2.0)
    if tier == "capital":
        parts = []
        for dx, hh in ((-0.32, 0.55), (0.0, 0.9), (0.32, 0.55)):
            x0 = cx + dx * s - s * 0.13
            parts.append(f"M{sd.num(x0)} {sd.num(cy + s * 0.35)}v{sd.num(-hh * s)}h{sd.num(s * 0.08)}v{sd.num(s * 0.08)}h{sd.num(s * 0.1)}v{sd.num(-s * 0.08)}"
                         f"h{sd.num(s * 0.08)}v{sd.num(hh * s)}Z")
        return f'<path d="{"".join(parts)}" fill="{ink}" stroke="{ground}" stroke-width="0.12" class="gm-place-glyph" data-tier="capital"/>'
    n = {"city": 3, "town": 2}.get(tier, 1)
    w = s / max(n, 1.6)
    parts = []
    for i in range(n):
        x0 = cx - n * w / 2 + i * w
        base = cy + s * 0.3
        parts.append(f"M{sd.num(x0)} {sd.num(base)}v{sd.num(-s * 0.35)}l{sd.num(w / 2)} {sd.num(-s * 0.28)}l{sd.num(w / 2)} {sd.num(s * 0.28)}v{sd.num(s * 0.35)}Z")
    return f'<path d="{"".join(parts)}" fill="{ink}" stroke="{ground}" stroke-width="0.12" class="gm-place-glyph" data-tier="{tier}"/>'


# ---------------------------------------------------------------------------------------------------------------- grid
def grid_markup(rect, cfg: dict, t: dict) -> str:
    sp = float(cfg.get("spacing_mm", 10.0))
    ink = t.get(cfg.get("token") or "grid") or t["frame"]
    d = []
    x = rect.x0 + sp
    while x < rect.x1 - 0.01:
        d.append(f"M{sd.num(x)} {sd.num(rect.y0)}V{sd.num(rect.y1)}")
        x += sp
    y = rect.y0 + sp
    while y < rect.y1 - 0.01:
        d.append(f"M{sd.num(rect.x0)} {sd.num(y)}H{sd.num(rect.x1)}")
        y += sp
    return (f'<path class="gm-grid" d="{"".join(d)}" fill="none" stroke="{ink}" stroke-width="0.12" stroke-opacity="{sd.num(float(cfg.get("opacity", 0.35)))}"/>'
            if d else "")


# ---------------------------------------------------------------------------------------------------------------- sketch
_PATH = re.compile(r'(<path\b[^>]*?\sd=")([^"]*)(")')
_ML = re.compile(r"^(?:[MLZ]\s*-?[\d.]+\s+-?[\d.]+\s*|Z\s*)+$")
_TOK = re.compile(r"([MLZ])\s*(?:(-?[\d.]+)\s+(-?[\d.]+))?")


def _jitter_sub(pts: np.ndarray, closed: bool, amp: float, wl: float, seed: int) -> np.ndarray:
    """Densify a polyline at about wl/4 and push each vertex sideways by smooth noise (two sines with seeded phases) of about `amp`."""
    if len(pts) < 2:
        return pts
    if closed:
        pts = np.vstack([pts, pts[:1]])
    seg = np.diff(pts, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    out = [pts[:1]]
    step = wl / 4.0
    for i in range(len(seg)):
        k = max(1, int(math.ceil(L[i] / step)))
        f = (np.arange(1, k + 1) / k)[:, None]
        out.append(pts[i] + seg[i] * f)
    P = np.vstack(out)
    d = np.diff(P, axis=0, prepend=P[:1])
    s = np.cumsum(np.hypot(d[:, 0], d[:, 1]))
    rng = np.random.default_rng(seed)
    ph1, ph2 = rng.uniform(0, 2 * math.pi, 2)
    n = amp * (0.65 * np.sin(2 * math.pi * s / wl + ph1) + 0.35 * np.sin(2 * math.pi * s / (wl * 0.43) + ph2))
    tx = np.gradient(P[:, 0])
    ty = np.gradient(P[:, 1])
    norm = np.hypot(tx, ty)
    norm[norm == 0] = 1.0
    nx, ny = -ty / norm, tx / norm
    Q = P + np.column_stack([nx * n, ny * n])
    if closed:
        Q = Q[:-1]
    return Q


def jitter_d(d: str, amp: float, wl: float, seed: int) -> str:
    """Path data of M, L and Z commands (absolute, as svg_draw writes it) with the sketch jitter; anything else is returned unchanged."""
    if not _ML.match(d.strip()):
        return d
    out, cur, k = [], [], 0

    def flush(closed: bool):
        nonlocal k
        if len(cur) >= 2:
            q = _jitter_sub(np.asarray(cur, dtype="float64"), closed, amp, wl, seed + k)
            out.append("M" + "L".join(f"{sd.num(x)} {sd.num(y)}" for x, y in q) + ("Z" if closed else ""))
        elif cur:
            out.append(f"M{sd.num(cur[0][0])} {sd.num(cur[0][1])}")
        k += 1
    for m in _TOK.finditer(d):
        c = m.group(1)
        if c == "M":
            if cur:
                flush(False)
            cur = [(float(m.group(2)), float(m.group(3)))]
        elif c == "L":
            cur.append((float(m.group(2)), float(m.group(3))))
        elif c == "Z":
            flush(True)
            cur = []
    if cur:
        flush(False)
    return "".join(out)


def sketch_map(svg_map: str, cfg: dict, map_id: str) -> str:
    """The sketch line character over the map group: every M/L path jittered (seeded by the map id and the path's order), and every open
    line drawn a second time with its own seed when overdraw is on. Point symbols (arcs) and text are left alone."""
    amp, wl = float(cfg.get("amplitude_mm", 0.3)), float(cfg.get("wavelength_mm", 4.0))
    over = bool(cfg.get("overdraw", True))
    count = [0]

    def rep(m):
        count[0] += 1
        d = m.group(2)
        new = jitter_d(d, amp, wl, _seed(map_id, count[0]))
        tag = m.group(1) + new + m.group(3)
        return tag

    parts = re.split(r"(<defs>.*?</defs>)", svg_map, flags=re.S)       # pattern and clip definitions are left as drawn
    out = "".join(p if p.startswith("<defs>") else _PATH.sub(rep, p) for p in parts)
    if over:
        def dup(m):
            whole = m.group(0)
            d = re.search(r'\sd="([^"]*)"', whole).group(1)
            if "Z" in d or not _ML.match(d.strip()) or "fill=" in whole and 'fill="none"' not in whole:
                return whole
            d2 = jitter_d(d, amp * 0.8, wl * 1.1, _seed(map_id, "over", d[:24]))
            return whole + re.sub(r'\sd="[^"]*"', f' d="{d2}" stroke-opacity="0.55"', whole, count=1)
        parts = re.split(r"(<defs>.*?</defs>)", out, flags=re.S)
        out = "".join(p if p.startswith("<defs>") else re.sub(r"<path\b[^>]*/>", dup, p) for p in parts)
    return out


def hatch_pattern(pid: str, color: str, seed: int, spacing: float = 1.7) -> str:
    """A jittered hatch for a sketch fill: a light tint of the colour, and hatch lines in it."""
    rng = np.random.default_rng(seed)
    sp = spacing
    w = sp * 6
    lines = []
    for i in range(7):
        y0 = i * sp
        pts = [(x, y0 + rng.uniform(-0.12, 0.12)) for x in np.linspace(-sp, w + sp, 6)]
        lines.append("M" + "L".join(f"{sd.num(x)} {sd.num(y)}" for x, y in pts))
    return (f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{sd.num(w)}" height="{sd.num(w)}" patternTransform="rotate(45)">'
            f'<rect width="{sd.num(w)}" height="{sd.num(w)}" fill="{color}" fill-opacity="0.45"/>'
            f'<path d="{"".join(lines)}" stroke="{color}" stroke-width="0.24" fill="none" stroke-linecap="round"/></pattern>')
