"""Map elements added in 0.15.0 (spec §10, Roadmap items of v0.14): callouts, the data table or chart insert, curved labels along lines,
leader lines for point labels that do not fit beside their symbol, and the "Data as of" rule for time-sensitive layers.

Everything here works in sheet millimetres on a fitted frame (layout.Frame) and returns SVG markup plus the rectangles it took, so the
renderer can keep later labels off them. Nothing here reads a file or the network.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

import svg_draw as sd
import textmeasure as tm
from label_engine import Rect

PT = 25.4 / 72.0
CALLOUT_MAX_W_MM = 42.0           # a callout's text wraps at this width
CALLOUT_MAX_LINES = 4
CALLOUT_PAD_MM = 1.2
CALLOUT_RINGS_MM = (7.0, 11.0, 16.0, 22.0)   # distances from the anchor the box is tried at
DIRECTIONS = ((1, -1), (-1, -1), (1, 1), (-1, 1), (1, 0), (-1, 0), (0, -1), (0, 1))   # ne, nw, se, sw, e, w, n, s (sheet y down)
DIR_NAMES = {"ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1), "e": (1, 0), "w": (-1, 0), "n": (0, -1), "s": (0, 1)}
TABLE_MAX_ROWS = 12
BAR_W_MM = 22.0
LEADER_RINGS_MM = (5.0, 8.0, 12.0)
LINE_LABEL_MAX_TURN_DEG = 28.0    # a label along a line is dropped where consecutive letters would turn more than this
LINE_LABEL_SPACING = 1.25         # a line must be this many times the label's length to carry it
TIME_SENSITIVE = ("live", "monthly", "quarterly")


def f(fid, sev, msg, detail=None):
    return {"id": fid, "severity": sev, "message": msg, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- shared
def _free(r: Rect, area: Rect, taken: list[Rect], pad: float = 0.6) -> bool:
    return r.inside(area) and not any(r.grown(pad).hits(q) for q in taken)


def _edge_point(r: Rect, x: float, y: float) -> tuple[float, float]:
    """Where a leader from (x, y) meets the rectangle's edge (the nearest point of the box)."""
    return min(max(x, r.x0), r.x1), min(max(y, r.y0), r.y1)


def _leader(x0: float, y0: float, x1: float, y1: float, color: str, halo: str, w: float = 0.25) -> str:
    d = f"M{sd.num(x0)} {sd.num(y0)}L{sd.num(x1)} {sd.num(y1)}"
    return (f'<path d="{d}" stroke="{halo}" stroke-width="{sd.num(w + 0.5)}" stroke-linecap="round" fill="none"/>'
            f'<path d="{d}" stroke="{color}" stroke-width="{sd.num(w)}" stroke-linecap="round" fill="none"/>')


# ---------------------------------------------------------------------------------------------------------------- callouts
def callout_anchor(plan, item: dict, frame) -> tuple[float, float] | None:
    """The anchor of a callout in sheet mm: `at: [lon, lat]` (WGS 84), or `layer` with `where: {field: value}` (the feature's point on surface).
    None when it cannot be found; the caller reports it."""
    import shapely
    if item.get("at") is not None:
        lon, lat = (float(v) for v in item["at"])
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", plan.crs, always_xy=True).transform(lon, lat)
    elif item.get("layer"):
        l = next((x for x in list(plan.layers) + list(getattr(plan, "helpers", None) or []) if x.id == item["layer"]), None)
        if l is None:
            return None
        g = l.gdf
        if g.crs is not None and plan.crs is not None and not g.crs.equals(plan.crs):      # a helper is not reprojected with the drawn layers
            g = g.to_crs(plan.crs)
        for k, v in (item.get("where") or {}).items():
            if k not in g.columns:
                return None
            g = g[g[k].astype(str) == str(v)]
        if not len(g):
            return None
        p = shapely.point_on_surface(g.geometry.values[0])
        x, y = p.x, p.y
    else:
        return None
    mx, my = frame.to_mm(np.array([x]), np.array([y]))
    return float(mx[0]), float(my[0])


def draw_callouts(plan, frame, cfg: dict, taken: list[Rect], fonts: dict) -> tuple[str, list[Rect], list[dict], list[dict]]:
    """elements.callouts: {on, items: [{text, at | layer+where, direction?}]}. Each box is tried around its anchor (the direction asked for
    first, then the eight directions at growing distances) and joined to it by a leader line; boxes avoid the overlays, the other callouts and
    each other. Returns (svg, rects taken, records, findings)."""
    t = plan.tokens
    strong = cfg.get("weight") == "strong"                # 1.4.0 (editorial pack): callouts carry more weight
    pt = float(fonts["label"]) + (1.0 if strong else 0.0)
    wt = 600 if strong else 400
    area = frame.rect
    out, rects, recs, fs = [], [], [], []
    for i, item in enumerate(cfg.get("items") or []):
        txt = str(item.get("text") or "").strip()
        if not txt:
            fs.append(f("MAP-CART-026", "WARN", f"callout {i + 1} has no text; it is not drawn"))
            continue
        anc = callout_anchor(plan, item, frame)
        if anc is None:
            fs.append(f("MAP-CART-026", "WARN", f"callout {i + 1} ('{txt[:40]}'): its place was not found (at: [lon, lat], or layer with where: {{field: value}})"))
            continue
        ax, ay = anc
        if not (area.x0 <= ax <= area.x1 and area.y0 <= ay <= area.y1):
            fs.append(f("MAP-CART-026", "WARN", f"callout {i + 1} ('{txt[:40]}') points outside the map frame; it is not drawn"))
            continue
        lines = tm.wrap(txt, CALLOUT_MAX_W_MM, pt, wt)[:CALLOUT_MAX_LINES]
        tw = max(tm.width_mm(ln, pt, wt) for ln in lines)
        bw = tw + 2 * CALLOUT_PAD_MM
        bh = len(lines) * tm.line_height_mm(pt) + 2 * CALLOUT_PAD_MM
        dirs = list(DIRECTIONS)
        want = DIR_NAMES.get(str(item.get("direction", "")).lower())
        if want:
            dirs.remove(want)
            dirs.insert(0, want)
        box = None
        for dist in CALLOUT_RINGS_MM:
            for dx, dy in dirs:
                cx, cy = ax + dx * dist, ay + dy * dist
                r = Rect(cx - bw / 2 + dx * bw / 2, cy - bh / 2 + dy * bh / 2, cx + bw / 2 + dx * bw / 2, cy + bh / 2 + dy * bh / 2)
                if _free(r, area, taken + rects):
                    box = r
                    break
            if box:
                break
        if box is None:
            fs.append(f("MAP-CART-026", "WARN", f"callout {i + 1} ('{txt[:40]}'): no free room near its place; shorten the text or move other elements"))
            continue
        ex, ey = _edge_point(box, ax, ay)
        out.append(f'<g class="gm-callout" data-callout="{i + 1}">')
        out.append(_leader(ax, ay, ex, ey, t["label"], t["label_halo"]))
        out.append(f'<circle cx="{sd.num(ax)}" cy="{sd.num(ay)}" r="0.7" fill="{t["label"]}" stroke="{t["label_halo"]}" stroke-width="0.3"/>')
        out.append(sd.rect(box.x0, box.y0, box.w, box.h, t["legend_bg"], t["label"] if strong else t["legend_frame"], 0.45 if strong else 0.2))
        y = box.y0 + CALLOUT_PAD_MM + pt * PT * 0.85
        for ln in lines:
            out.append(sd.text(box.x0 + CALLOUT_PAD_MM, y, ln, pt, t["label"], weight=wt))
            y += tm.line_height_mm(pt)
        out.append("</g>")
        rects.append(box)
        rects.append(Rect(min(ax, ex) - 0.4, min(ay, ey) - 0.4, max(ax, ex) + 0.4, max(ay, ey) + 0.4))
        recs.append({"n": i + 1, "text": txt[:80], "anchor_mm": [round(ax, 2), round(ay, 2)], "box_mm": [round(box.x0, 2), round(box.y0, 2), round(box.x1, 2), round(box.y1, 2)]})
    return "".join(out), rects, recs, fs


# ---------------------------------------------------------------------------------------------------------------- data table / chart insert
def table_rows(plan, cfg: dict) -> tuple[list[tuple[str, float]], list[dict]]:
    """The rows of elements.data_table: the `top` features of `layer` by `value_field` (descending unless order: asc), labelled by
    `label_field`. Rows with a missing value are skipped."""
    fs = []
    # 1.2.0 (Day 9 issue 1): a draw: false helper can feed the table (a measure the map does not draw as its own layer)
    l = next((x for x in list(plan.layers) + list(getattr(plan, "helpers", None) or []) if x.id == cfg.get("layer")), None)
    if l is None:
        return [], [f("MAP-CART-027", "BLOCK", f"data_table: no layer '{cfg.get('layer')}' in the map")]
    vf, lf = cfg.get("value_field"), cfg.get("label_field")
    miss = [k for k in (vf, lf) if not k or k not in l.gdf.columns]
    if miss:
        return [], [f("MAP-CART-027", "BLOCK", f"data_table: layer '{l.id}' has no field {', '.join(repr(m) for m in miss)} (value_field and label_field are both needed)")]
    import pandas as pd
    vals = pd.to_numeric(l.gdf[vf], errors="coerce")
    df = pd.DataFrame({"label": l.gdf[lf].astype(str).to_numpy(), "value": vals.to_numpy(), "pos": range(len(l.gdf))}).dropna(subset=["value"])
    if not len(df):
        return [], [f("MAP-CART-027", "BLOCK", f"data_table: '{vf}' on layer '{l.id}' has no numeric values")]
    df = df.sort_values("value", ascending=str(cfg.get("order", "desc")).lower() == "asc", kind="stable")
    stats = cfg.get("stats") or []
    stats = [stats] if isinstance(stats, str) else list(stats)
    bad = [x for x in stats if x not in STATS]
    if bad:
        return [], [f("MAP-CART-027", "BLOCK", f"data_table.stats: {', '.join(map(str, bad))} not one of {', '.join(STATS)}")]
    top = 0 if cfg.get("kind") == "summary" else int(cfg.get("top", 5))
    if cfg.get("kind") == "summary" and not stats:
        stats = ["count", "sum", "mean", "median", "min", "max"]
    n = max(0, min(top, TABLE_MAX_ROWS))
    if top > TABLE_MAX_ROWS:
        fs.append(f("MAP-CART-027", "WARN", f"data_table: at most {TABLE_MAX_ROWS} rows are drawn on a map"))
    rows = [(r.label, float(r.value)) for r in df.head(n).itertuples()]
    # 0.19.0 (Day 7 issue 23): on a classified layer each bar takes its feature's class colour, so the chart reads with the map's legend
    if l.cls is not None and l.colors and len(getattr(l.cls, "assigned", [])) == len(l.gdf):
        import numpy as np
        asg = np.asarray(l.cls.assigned)
        cfg["_row_colors"] = [l.colors[int(asg[int(p)])] if int(asg[int(p)]) >= 0 else plan.tokens["no_data"] for p in df.head(n)["pos"]]
    v = df["value"]                                    # 0.16.0: summary statistics over every feature with a value, not only the rows shown
    calc = {"count": float(len(v)), "sum": float(v.sum()), "mean": float(v.mean()), "median": float(v.median()), "min": float(v.min()), "max": float(v.max())}
    rows += [(STATS[k], calc[k], "stat") for k in stats]
    skipped = int(pd.to_numeric(l.gdf[vf], errors="coerce").isna().sum())
    if stats and skipped:
        fs.append(f("MAP-CART-027", "INFO", f"data_table: the statistics leave out {skipped} feature(s) with no value in '{vf}'"))
    return rows, fs


STATS = {"count": "Count", "sum": "Total", "mean": "Mean", "median": "Median", "min": "Minimum", "max": "Maximum"}


def _fmt(v: float, cfg: dict) -> str:
    import classify as cf
    nf = cf.number_format(cfg.get("format"))
    # 1.2.0 (Day 9 issue 11): format.decimals is honoured (96.1 shows as 96 with decimals 0); without it whole numbers show none and others one
    return nf.fmt(v, None if nf.decimals is not None else (0 if float(v).is_integer() else 1))


def table_size(rows: list[tuple[str, float]], cfg: dict, pt: float) -> dict:
    title = cfg.get("title") or ""
    lw = max((tm.width_mm(r[0], pt, 600 if len(r) > 2 else 400) for r in rows), default=0.0)
    vw = max((tm.width_mm(_fmt(r[1], cfg), pt, 400, tnum=True) for r in rows), default=0.0)
    kind = cfg.get("kind", "table")
    body_w = lw + 3.0 + (BAR_W_MM + 2.0 if kind == "bar" else 0.0) + vw
    tw = tm.width_mm(title, pt + 0.5, 600) if title else 0.0
    rh = tm.line_height_mm(pt) + 0.6
    w = max(body_w, tw) + 2 * CALLOUT_PAD_MM
    rule = 1.2 if any(len(r) > 2 for r in rows) and any(len(r) == 2 for r in rows) else 0.0
    h = 2 * CALLOUT_PAD_MM + (tm.line_height_mm(pt + 0.5) + 0.8 if title else 0.0) + rh * len(rows) + rule
    return {"w": w, "h": h, "label_w": lw, "value_w": vw, "row_h": rh, "kind": kind, "title": title}


def draw_table(rows: list[tuple[str, float]], cfg: dict, size: dict, x: float, y: float, t: dict, pt: float) -> str:
    """A small table (label, value) or a horizontal bar chart (label, bar, value) in a panel like the legend's."""
    out = [f'<g class="gm-data-table" data-kind="{sd.esc(size["kind"])}">', sd.rect(x, y, size["w"], size["h"], t["legend_bg"], t["legend_frame"], 0.2)]
    cx, cy = x + CALLOUT_PAD_MM, y + CALLOUT_PAD_MM
    if size["title"]:
        out.append(sd.text(cx, cy + (pt + 0.5) * PT * 0.85, size["title"], pt + 0.5, t["label"], weight=600))
        cy += tm.line_height_mm(pt + 0.5) + 0.8
    vmax = max((abs(r[1]) for r in rows if len(r) == 2), default=1.0) or 1.0
    bar_col = t.get("emphasis") or t["label"]
    seen_row = False
    row_cols = list(cfg.get("_row_colors") or [])
    for row in rows:
        lab, v = row[0], row[1]
        stat = len(row) > 2
        if stat and seen_row:                          # a thin rule between the top rows and the statistics
            out.append(f'<path class="gm-stats-rule" d="M{cx:.2f} {cy + 0.4:.2f}H{cx + size["w"] - 2 * CALLOUT_PAD_MM:.2f}" stroke="{t["legend_frame"]}" '
                       f'stroke-width="0.2" fill="none"/>')
            cy += 1.2
            seen_row = False
        seen_row = seen_row or not stat
        base = cy + pt * PT * 0.85
        out.append(sd.text(cx, base, lab, pt, t["label"], weight=600 if stat else 400))
        vx = cx + size["label_w"] + 3.0
        if size["kind"] == "bar" and not stat:
            bw = max(0.4, BAR_W_MM * abs(v) / vmax)
            out.append(sd.rect(vx, cy + 0.5, bw, size["row_h"] - 1.6, row_cols.pop(0) if row_cols else bar_col, "none"))
            vx += BAR_W_MM + 2.0
        if size["kind"] == "bar" and stat:
            vx += BAR_W_MM + 2.0
        out.append(sd.text(vx + size["value_w"], base, _fmt(v, cfg), pt, t["label"], anchor="end", num_cls=True))
        cy += size["row_h"]
    out.append("</g>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------- labels along lines
def _glyphs_along(line, text: str, size_pt: float, start: float) -> list[tuple[str, float, float, float, float]] | None:
    """(char, x, y, angle_deg, width) for each letter of `text` set along `line` from distance `start`; None where the line turns too
    sharply between two letters."""
    out = []
    s = start
    prev = None
    for ch in text:
        w = tm.width_mm(ch, size_pt, 500) if ch != " " else size_pt * PT * 0.28
        mid = s + w / 2
        p0, p1 = line.interpolate(max(mid - 0.3, 0)), line.interpolate(min(mid + 0.3, line.length))
        ang = math.degrees(math.atan2(p1.y - p0.y, p1.x - p0.x))
        if prev is not None:
            turn = abs((ang - prev + 180) % 360 - 180)
            if turn > LINE_LABEL_MAX_TURN_DEG:
                return None
        p = line.interpolate(mid)
        out.append((ch, p.x, p.y, ang, w))
        prev = ang
        s += w
    return out


def line_label_markup(plan, l, lb: dict, frame, taken: list[Rect], size_pt: float, sheet_geoms) -> tuple[str, list[Rect], dict]:
    """Labels that follow their line (a road, a river, a fibre route): each letter set along the line, upright, at the line's middle (then
    at a third and two thirds), one label per feature, highest priority first. A label is skipped where the line is shorter than the text
    or bends too sharply, or where it would touch another label or an overlay. Returns (svg, glyph rects, stats)."""
    import shapely
    t = plan.tokens
    field_ = lb.get("field")
    vals = l.gdf[field_].to_numpy(dtype=object)
    pr = lb.get("priority_field")
    prio = np.asarray(l.gdf[pr], dtype="float64") if pr and pr in l.gdf.columns else None
    order = list(range(len(vals)))
    if prio is not None:
        order.sort(key=lambda i: -(prio[i] if np.isfinite(prio[i]) else -1e18))
    else:
        lens = shapely.length(sheet_geoms)
        order.sort(key=lambda i: -(lens[i] if lens[i] == lens[i] else 0))
    out, rects = [], []
    stats = {"placed": 0, "too_short": 0, "too_bent": 0, "collision": 0}
    seen: set = set()
    halo_w = max(0.3, size_pt * PT * 0.22)
    for i in order[:400]:
        v = vals[i]
        if v is None or (isinstance(v, float) and v != v):
            continue
        g = sheet_geoms[i]
        if g is None or g.is_empty:
            continue
        txt = str(v)
        if txt in seen and lb.get("once_per_name", True):
            continue
        parts = list(getattr(g, "geoms", [g]))
        line = max(parts, key=lambda p: p.length)
        tw = tm.width_mm(txt, size_pt, 500)
        if line.length < tw * LINE_LABEL_SPACING:
            stats["too_short"] += 1
            continue
        a, b = line.coords[0], line.coords[-1]
        if b[0] < a[0]:                                   # read left to right
            line = shapely.LineString(list(line.coords)[::-1])
        placed = None
        bent = False
        for frac in (0.5, 0.33, 0.67):
            start = line.length * frac - tw / 2
            if start < 0 or start + tw > line.length:
                continue
            gl = _glyphs_along(line, txt, size_pt, start)
            if gl is None:
                bent = True
                continue
            h = size_pt * PT
            grs = [Rect(x - max(w, h) / 2, y - h * 0.6, x + max(w, h) / 2, y + h * 0.6) for _, x, y, _, w in gl]
            if all(_free(r, frame.rect, taken + rects, 0.3) for r in grs):
                placed = (gl, grs)
                break
        if placed is None:
            stats["too_bent" if bent else "collision"] += 1
            continue
        gl, grs = placed
        out.append(f'<g class="gm-line-label" data-layer="{sd.esc(l.id)}">')
        for ch, x, y, ang, w in gl:
            if ch.strip():
                out.append(_glyph(x, y, ch, size_pt, t["label"], ang, t["label_halo"], halo_w))
        out.append("</g>")
        rects += grs
        seen.add(txt)
        stats["placed"] += 1
    return "".join(out), rects, stats


def _glyph(cx, cy, ch, size_pt, fill, ang, halo, halo_w) -> str:
    size = size_pt * PT
    tr = f'transform="rotate({sd.num(ang)} {sd.num(cx)} {sd.num(cy)})"'
    attrs = f'class="gm-t" x="{sd.num(cx)}" y="{sd.num(cy + size * 0.35)}" font-size="{sd.num(size)}" font-weight="500" text-anchor="middle" {tr}'
    body = sd.esc(ch)
    return (f'<text {attrs} fill="{halo}" stroke="{halo}" stroke-width="{sd.num(halo_w)}" stroke-linejoin="round">{body}</text>'
            f'<text {attrs} fill="{fill}">{body}</text>')


# ---------------------------------------------------------------------------------------------------------------- leader lines
def leader_labels(plan, cands, frame, taken: list[Rect], size_pt: float, weight: int = 400) -> tuple[str, list[Rect], int]:
    """Point labels that did not fit beside their symbol, placed a little farther out with a thin leader line back to the symbol (labels
    with leaders: true). Tried at 5, 8 and 12 mm in eight directions; a label that still does not fit stays dropped."""
    t = plan.tokens
    out, rects = [], []
    n = 0
    h = size_pt * PT
    for c in cands:
        w = tm.width_mm(c.text, size_pt, weight)
        done = False
        for dist in LEADER_RINGS_MM:
            for dx, dy in DIRECTIONS:
                cx, cy = c.x + dx * (dist + c.radius), c.y + dy * (dist + c.radius)
                x0 = cx if dx > 0 else cx - w if dx < 0 else cx - w / 2
                r = Rect(x0 - 0.3, cy - h * 0.55, x0 + w + 0.3, cy + h * 0.45)
                if not _free(r, frame.rect, taken + rects, 0.4):
                    continue
                ex, ey = _edge_point(r, c.x, c.y)
                lr = Rect(min(c.x, ex), min(c.y, ey), max(c.x, ex), max(c.y, ey))
                if any(lr.grown(0.2).hits(q) for q in rects):
                    continue
                out.append('<g class="gm-leader-label">' + _leader(c.x + (ex - c.x) * (c.radius / max(dist, 1e-6)), c.y + (ey - c.y) * (c.radius / max(dist, 1e-6)),
                                                                   ex, ey, t["label"], t["label_halo"], 0.18)
                           + sd.text(x0, cy + h * 0.35, c.text, size_pt, t["label"], halo=t["label_halo"], halo_w=max(0.3, h * 0.22)) + "</g>")
                rects.append(r)
                n += 1
                done = True
                break
            if done:
                break
    return "".join(out), rects, n


# ---------------------------------------------------------------------------------------------------------------- data as of
def time_sensitive_layers(plan) -> list[tuple[str, str | None]]:
    """(layer id, the date its data is as of) for every layer whose data changes over time: a service capture, or a library entry whose
    freshness is live, monthly or quarterly. The date is the capture's date, or the entry's vintage, when known."""
    out = []
    for l in plan.layers:
        p = l.provenance or {}
        cap = p.get("captured_at") or p.get("capture_time") or (p.get("capture") or {}).get("captured_at")
        fresh = str(p.get("freshness") or (p.get("library") or {}).get("freshness") or "").lower()
        if cap or fresh in TIME_SENSITIVE:
            out.append((l.id, str(cap)[:10] if cap else (str(p.get("vintage")) if p.get("vintage") else None)))
    return out


def data_as_of_text(plan, ds: dict) -> tuple[str | None, list[dict]]:
    """The 'Data as of' line of the date stamp. `data_as_of: "2026-11-07"` states it; `data_as_of: true` (or a time-sensitive layer with
    nothing said) takes the latest date the time-sensitive layers carry. A time-sensitive layer with no date known and no data_as_of is
    MAP-CART-028 (WARN): the reader cannot tell how current the map is."""
    fs = []
    v = ds.get("data_as_of")
    ts = time_sensitive_layers(plan)
    if isinstance(v, str) and v.strip():
        return f"Data as of {_nice(v.strip(), plan)}", fs
    dates = sorted(d for _, d in ts if d)
    if v is True or ts:
        if dates:
            return f"Data as of {_nice(dates[-1], plan)}", fs
        if ts:
            fs.append(f("MAP-CART-028", "WARN", f"layer(s) {', '.join(i for i, _ in ts)} change over time but carry no date; set elements.date_stamp.data_as_of "
                                                "to the date the data was taken"))
    return None, fs


def _nice(s: str, plan=None) -> str:
    try:
        d = dt.date.fromisoformat(s[:10])
        import geo_locale as gl
        return gl.plan_date_text(plan, d) if plan is not None else gl.date_text(d, "en-US")
    except ValueError:
        return s
