"""Map elements added in 0.15.0 (spec §10, Roadmap items of v0.14): callouts, the data table or chart insert, curved labels along lines,
leader lines for point labels that do not fit beside their symbol, and the "Data as of" rule for time-sensitive layers.

Everything here works in sheet millimetres on a fitted frame (layout.Frame) and returns SVG markup plus the rectangles it took, so the
renderer can keep later labels off them. Nothing here reads a file or the network.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path

import numpy as np

import svg_draw as sd
import textmeasure as tm
from label_engine import Rect

PT = 25.4 / 72.0
CALLOUT_MAX_W_MM = 42.0           # a callout's text wraps at this width
CALLOUT_MAX_LINES = 4
CALLOUT_FIGURE_PT = 5.0           # 1.13.0: a callout's `figure` is set this many points larger than its words, bold
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


STRONG_OVERLAP = 0.08      # 2.1.0: a callout box may cover at most this share of itself with the map's strongest data before another place is tried


def strong_data_mm(plan, frame):
    """2.1.0 (Day 19 issue 5): what the map's message rests on, in sheet mm: the top two classes of each classed polygon layer (the top one
    when it has three classes or fewer) and a layer's highlighted features. A callout keeps off them when it can, as labels keep off the
    symbols. -> a shapely geometry, or None."""
    import shapely
    parts = []
    for l in plan.layers:
        if getattr(l, "legend_only", False) or l.kind != "polygon" or not len(l.gdf):
            continue
        g = np.asarray(l.gdf.geometry.values, dtype=object)
        keep = None
        hl = getattr(l, "highlight", None)
        if isinstance(hl, np.ndarray) and len(hl) == len(g) and hl.any() and not hl.all():
            keep = np.asarray(hl, dtype=bool)
        elif l.cls is not None and len(getattr(l.cls, "assigned", [])) == len(g) and int(getattr(l.cls, "k", 0) or 0) >= 2:
            k = int(l.cls.k)
            a = np.asarray(l.cls.assigned)
            keep = a >= (k - 2 if k > 3 else k - 1)
        if keep is None or not keep.any():
            continue
        sub = g[keep]
        sub = sub[~shapely.is_missing(sub) & ~shapely.is_empty(sub)]
        if len(sub):
            parts.append(shapely.transform(sub, lambda c: np.column_stack(frame.to_mm(c[:, 0], c[:, 1]))))
    if not parts:
        return None
    try:
        u = shapely.union_all(shapely.make_valid(np.concatenate(parts)))
    except Exception:  # noqa: BLE001 - a geometry the union refuses: the callouts are placed by the overlays alone, as before
        return None
    return None if u.is_empty else u


def _covers_strong(r: Rect, strong) -> bool:
    if strong is None:
        return False
    import shapely
    b = shapely.box(r.x0, r.y0, r.x1, r.y1)
    return strong.intersects(b) and strong.intersection(b).area > STRONG_OVERLAP * b.area


def draw_callouts(plan, frame, cfg: dict, taken: list[Rect], fonts: dict, strong_data=None) -> tuple[str, list[Rect], list[dict], list[dict]]:
    """elements.callouts: {on, items: [{text, figure?, at | layer+where, direction?}]} (figure, 1.13.0: a key number set large and bold above
    the words). Each box is tried around its anchor (the direction asked for
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
        fig = str(item.get("figure") or "").strip()            # 1.13.0 (Day 12 issue 6): a key number set large above the words ("692 MW")
        fpt = pt + CALLOUT_FIGURE_PT
        tw = max([tm.width_mm(ln, pt, wt) for ln in lines] + ([tm.width_mm(fig, fpt, 700, tnum=True)] if fig else []))
        bw = tw + 2 * CALLOUT_PAD_MM
        bh = len(lines) * tm.line_height_mm(pt) + 2 * CALLOUT_PAD_MM + (tm.line_height_mm(fpt) if fig else 0.0)
        dirs = list(DIRECTIONS)
        want = DIR_NAMES.get(str(item.get("direction", "")).lower())
        if want:
            dirs.remove(want)
            dirs.insert(0, want)
        box = None
        # 2.1.0: first a place off the map's strongest data (its top classes, its highlight), then, if there is none, the old rule
        for avoid_strong in ((True, False) if strong_data is not None else (False,)):
            for dist in CALLOUT_RINGS_MM:
                for dx, dy in dirs:
                    cx, cy = ax + dx * dist, ay + dy * dist
                    r = Rect(cx - bw / 2 + dx * bw / 2, cy - bh / 2 + dy * bh / 2, cx + bw / 2 + dx * bw / 2, cy + bh / 2 + dy * bh / 2)
                    if _free(r, area, taken + rects) and not (avoid_strong and _covers_strong(r, strong_data)):
                        box = r
                        break
                if box:
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
        if fig:
            y = box.y0 + CALLOUT_PAD_MM + fpt * PT * 0.85
            out.append(sd.text(box.x0 + CALLOUT_PAD_MM, y, fig, fpt, t["label"], weight=700, num_cls=True, extra=' data-role="figure"'))
            y = box.y0 + CALLOUT_PAD_MM + tm.line_height_mm(fpt) + pt * PT * 0.85
        for ln in lines:
            out.append(sd.text(box.x0 + CALLOUT_PAD_MM, y, ln, pt, t["label"], weight=wt))
            y += tm.line_height_mm(pt)
        out.append("</g>")
        rects.append(box)
        rects.append(Rect(min(ax, ex) - 0.4, min(ay, ey) - 0.4, max(ax, ex) + 0.4, max(ay, ey) + 0.4))
        recs.append({"n": i + 1, "text": txt[:80], **({"figure": fig} if fig else {}), "anchor_mm": [round(ax, 2), round(ay, 2)], "box_mm": [round(box.x0, 2), round(box.y0, 2), round(box.x1, 2), round(box.y1, 2)],
                     "on_strong_data": bool(_covers_strong(box, strong_data))})
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
    stats["texts"] = sorted(seen)                         # 1.22.0: which names found room (a direct label that did not goes back to the key)
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


STACK_CLUSTER_MM = 10.0           # 5.6.0 (Q-06): labels left out within this distance of each other form one cluster
STACK_MIN = 2                     # ...and a cluster of at least this many is set as a column off it
STACK_OFFSETS_MM = (6.0, 10.0, 15.0, 22.0, 30.0)
STACK_ROW = 1.45                  # the column's line spacing, in label heights
MERGE_MM = 0.6                    # 5.6.0 (Q-06): two names whose points are closer than this on the sheet point at one dot
MERGE_MAX = 2                     # ...and at most this many names are joined (a crowd of dots is a cluster, set as a column)


def merge_same_dot(cands, merge_mm: float = MERGE_MM) -> tuple[list, list[str]]:
    """5.6.0 (Q-06): candidates whose points fall on one dot become one label, its names joined with a middle dot in priority order
    ("Provincetown · Cape Cod"). -> (candidates, the merged texts)."""
    out, merged = [], []
    for c in sorted(cands, key=lambda c: -c.priority):
        hit = next((o for o in out if o.kind == "point" and c.kind == "point" and math.hypot(o.x - c.x, o.y - c.y) < merge_mm
                    and len(o.text.split(" · ")) < MERGE_MAX), None)
        if hit is None:
            out.append(c)
        elif c.text not in hit.text.split(" · "):
            hit.text = f"{hit.text} · {c.text}"
            merged.append(hit.text)
    return out, merged


def _clusters(cands, reach: float) -> list[list]:
    """Single-linkage groups of point candidates on the sheet."""
    left = list(cands)
    groups = []
    while left:
        g = [left.pop(0)]
        grew = True
        while grew:
            grew = False
            for c in list(left):
                if any(math.hypot(c.x - o.x, c.y - o.y) <= reach for o in g):
                    g.append(c)
                    left.remove(c)
                    grew = True
        groups.append(g)
    return groups


def _cross(a, b) -> bool:
    """Whether two segments ((x0, y0), (x1, y1)) cross."""
    (p1, p2), (p3, p4) = a, b

    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(p1, p2, p3) * o(p1, p2, p4) < 0) and (o(p3, p4, p1) * o(p3, p4, p2) < 0)


def stack_column(group, size_pt: float, area: Rect, taken: list[Rect], weight: int = 400):
    """The first column off the cluster that fits: right of it, then left, at STACK_OFFSETS_MM; its rows in the points' top-to-bottom order,
    and swapped until no two leaders cross. -> [(candidate, label rect, text x, baseline, anchor, leader end)] or None."""
    h = size_pt * PT
    row = h * STACK_ROW
    pts = sorted(group, key=lambda c: c.y)
    ws = {id(c): tm.width_mm(c.text, size_pt, weight) for c in pts}
    total = row * len(pts)
    cy = sum(c.y for c in pts) / len(pts)
    top = min(max(cy - total / 2, area.y0 + 0.5), area.y1 - total - 0.5)
    xmin, xmax = min(c.x - c.radius for c in pts), max(c.x + c.radius for c in pts)
    for off in STACK_OFFSETS_MM:
        for side in (1, -1):
            xc = xmax + off if side > 0 else xmin - off
            slots = [top + row * (k + 0.5) for k in range(len(pts))]
            order = list(range(len(pts)))
            for _ in range(len(pts) ** 2 + 1):                     # untangle: each swap of a crossing pair shortens the leaders
                segs = [((pts[i].x, pts[i].y), (xc, slots[order.index(i)])) for i in range(len(pts))]
                pair = next(((i, j) for i in range(len(pts)) for j in range(i + 1, len(pts)) if _cross(segs[i], segs[j])), None)
                if pair is None:
                    break
                a, b = order.index(pair[0]), order.index(pair[1])
                order[a], order[b] = order[b], order[a]
            rows, ok = [], True
            for k, i in enumerate(order):
                c = pts[i]
                w = ws[id(c)]
                y = slots[k]
                x0 = xc + 0.6 if side > 0 else xc - 0.6 - w
                r = Rect(x0 - 0.3, y - h * 0.55, x0 + w + 0.3, y + h * 0.45)
                if not _free(r, area, taken + [q[1] for q in rows], 0.3):
                    ok = False
                    break
                rows.append((c, r, x0, y + h * 0.35, "start", (xc, y)))
            if ok:
                return rows
    return None


def stack_labels(plan, cands, frame, taken: list[Rect], size_pt: float, min_size: int = STACK_MIN, weight: int = 400) -> tuple[str, list[Rect], list]:
    """5.6.0 (Q-06): the labels a point layer could not place beside their symbols, where they crowd (Southampton, Dartmouth and Plymouth
    at ocean scale), set as a column off the cluster with a thin leader from each point to its name; leaders never cross. A cluster whose
    column does not fit anywhere is left to the single leaders. -> (svg, the rects taken, the candidates placed)."""
    t = plan.tokens
    out, rects, done = [], [], []
    for g in _clusters([c for c in cands if c.kind == "point"], STACK_CLUSTER_MM):
        if len(g) < min_size:
            continue
        rows = stack_column(g, size_pt, frame.rect, taken + rects, weight)
        if not rows:
            continue
        parts = []
        for c, r, x0, base, _a, (ex, ey) in rows:
            d = max(math.hypot(ex - c.x, ey - c.y), 1e-6)
            sx, sy = c.x + (ex - c.x) * c.radius / d, c.y + (ey - c.y) * c.radius / d
            parts.append(_leader(sx, sy, ex, ey, t["label"], t["label_halo"], 0.18)
                         + sd.text(x0, base, c.text, size_pt, t["label"], halo=t["label_halo"], halo_w=max(0.3, size_pt * PT * 0.22), weight=weight))
            rects.append(r)
            done.append(c)
        out.append('<g class="gm-callout-stack">' + "".join(parts) + "</g>")
    return "".join(out), rects, done


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


# ---------------------------------------------------------------------------------------------------------------- 1.19.0: figures
FIGURE_MARGIN_MM = 4.0
FIGURE_GAP_MM = 2.0
CORNERS = ("lower-left", "lower-right", "upper-left", "upper-right")


class FigureError(Exception):
    pass


def _figure_svg(path: Path, prefix: str) -> tuple[str, list[float]]:
    """The inner markup and viewBox of a figure's SVG file, its ids prefixed so two figures (or the map) never share one. A figure with script,
    foreign content or references outside itself is refused (the map's own SVG check would block it anyway)."""
    import map_export as mx
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^\s*<\?xml[^>]*>\s*", "", text)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    if re.search(r"<(?:script|foreignObject|iframe)\b", text, re.I):
        raise FigureError(f"{path.name} holds script or foreign content")
    if re.search(r"""(?:href\s*=\s*["'](?!#|data:)|url\(\s*["']?(?!#|data:))""", text, re.I):
        raise FigureError(f"{path.name} refers to files or addresses outside itself; embed pictures as data: URIs")
    m = re.search(r"<svg\b([^>]*)>(.*)</svg>\s*$", text, re.S)
    if not m:
        raise FigureError(f"{path.name} is not an SVG file")
    attrs, inner = m.group(1), m.group(2)
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', attrs)
    if vb:
        box = [float(v) for v in re.split(r"[\s,]+", vb.group(1).strip())[:4]]
    else:
        w = re.search(r'\bwidth\s*=\s*"([\d.]+)', attrs)
        h = re.search(r'\bheight\s*=\s*"([\d.]+)', attrs)
        if not (w and h):
            raise FigureError(f"{path.name} has no viewBox and no width and height")
        box = [0.0, 0.0, float(w.group(1)), float(h.group(1))]
    inner, _n = mx.split_paint_order(inner)                    # the PDF path ignores paint-order (Day 17 issue B9)
    ids = set(re.findall(r'\bid\s*=\s*"([^"]+)"', inner))
    for i in sorted(ids, key=len, reverse=True):
        inner = re.sub(r'(\bid\s*=\s*")' + re.escape(i) + '"', r"\g<1>" + prefix + i + '"', inner)
        inner = re.sub(r'(#)' + re.escape(i) + r'(?=["\')\s])', r"\g<1>" + prefix + i, inner)
    return inner, box


def place_figures(plan, map_rect: Rect, placed: dict, cfg: dict, base: Path) -> tuple[str, list[Rect], list[dict], list[str]]:
    """elements.figures (1.19.0, Day 17 issues B8 and B10): {on, items: [{svg, position | rect_mm, w_mm, title}]}. Each item is an SVG drawn
    into the map frame at its corner (lower-left by default: under the title, beside nothing) or at rect_mm ([x, y, w, h] from the frame's top
    left), at w_mm wide (its own aspect kept). A corner already holding an element (logo, scale bar, inset, legend) stacks the figure beside
    it, away from the edge. Labels keep clear of the figures, and the figure is drawn again on every build, so a section box or a chart made
    once stays with the map through every rebuild. Returns (svg, rects, findings, notes)."""
    items = cfg.get("items") or []
    out, rects, fs, notes = [], [], [], []
    taken = [v["rect"] for v in placed.values()]
    for k, it in enumerate(items):
        src = it.get("svg")
        if it.get("_svg") is not None:                   # 1.20.0: a figure the plugin drew itself (elements.section)
            inner, box, p = it["_svg"], list(it["_box"]), Path(str(it.get("title") or "figure"))
        elif not src:
            fs.append({"id": "MAP-SPEC-005", "severity": "WARN", "message": f"elements.figures item {k + 1} has no svg file; it was left out", "detail": None})
            continue
        else:
            p = Path(src)
            p = p if p.is_absolute() else base / p
            try:
                inner, box = _figure_svg(p, f"gmfig{k}-")
            except (OSError, FigureError) as ex:
                fs.append({"id": "MAP-SPEC-005", "severity": "WARN", "message": f"elements.figures item {k + 1}: {ex}; it was left out", "detail": str(src)})
                continue
        aspect = box[3] / box[2] if box[2] else 1.0
        if it.get("rect_mm"):
            # rect_mm is [x, y, w, h] from the map frame's top-left corner; 3.4.0 (Day 20 #24): `from: page` measures it from the page's
            x, y, w, h = (float(v) for v in it["rect_mm"])
            ox, oy = (0.0, 0.0) if str(it.get("from") or "frame") == "page" else (map_rect.x0, map_rect.y0)
            r = Rect(ox + x, oy + y, ox + x + w, oy + y + h)
        else:
            w = float(it.get("w_mm") or min(120.0, map_rect.w * 0.45))
            h = float(it.get("h_mm") or w * aspect)
            corner = str(it.get("position") or "lower-left")
            if corner not in CORNERS:
                corner = "lower-left"
            left, low = corner.endswith("left"), corner.startswith("lower")
            x0 = map_rect.x0 + FIGURE_MARGIN_MM if left else map_rect.x1 - FIGURE_MARGIN_MM - w
            y0 = map_rect.y1 - FIGURE_MARGIN_MM - h if low else map_rect.y0 + FIGURE_MARGIN_MM
            w0, h0 = w, h
            for scale in ((1.0, 0.85, 0.7, 0.6) if it.get("_svg") is not None else (1.0,)):
                # 5.5.0 (D26-20): a figure the plugin drew (a detail inset, a section) is made smaller before it is left out
                w, h = w0 * scale, h0 * scale
                x0 = map_rect.x0 + FIGURE_MARGIN_MM if left else map_rect.x1 - FIGURE_MARGIN_MM - w
                y0 = map_rect.y1 - FIGURE_MARGIN_MM - h if low else map_rect.y0 + FIGURE_MARGIN_MM
                r = Rect(x0, y0, x0 + w, y0 + h)
                for _try in range(6):                                 # step away from the edge past anything already in the corner
                    hit = [t_ for t_ in taken + rects if r.hits(t_)]
                    if not hit:
                        break
                    if low:
                        y0 = min(t_.y0 for t_ in hit) - FIGURE_GAP_MM - h
                    else:
                        y0 = max(t_.y1 for t_ in hit) + FIGURE_GAP_MM
                    r = Rect(x0, y0, x0 + w, y0 + h)
                if not (r.y0 < map_rect.y0 or r.y1 > map_rect.y1):
                    if scale < 1.0:
                        notes.append(f"figure '{it.get('title') or 'figure'}' set at {scale:.0%} of its size to fit the {corner} corner")
                    break
            if r.y0 < map_rect.y0 or r.y1 > map_rect.y1:
                fs.append({"id": "MAP-SPEC-005", "severity": "WARN", "message": f"elements.figures item {k + 1} does not fit in the {corner} corner beside the "
                           "elements there; give rect_mm or a smaller w_mm", "detail": str(src)})
                continue
        title = it.get("title") or p.stem
        out.append(f'<g class="gm-figure" data-figure="{sd.esc(str(title))}"><svg x="{sd.num(r.x0)}" y="{sd.num(r.y0)}" width="{sd.num(r.w)}" '
                   f'height="{sd.num(r.h)}" viewBox="{" ".join(sd.num(v) for v in box)}" preserveAspectRatio="xMidYMid meet" overflow="hidden">'
                   f"<title>{sd.esc(str(title))}</title>{inner}</svg></g>")
        rects.append(r)
        it["_placed"] = True
        notes.append(f"figure '{title}' at {r.x0:.1f}, {r.y0:.1f} mm, {r.w:.1f} x {r.h:.1f} mm")
    return "".join(out), rects, fs, notes


# ---------------------------------------------------------------------------------------------------------------- marks (2.0.0)
MARK_TYPES = ("ring", "arrow", "bracket")
MARK_INK_MM = 0.45
MARK_HALO_MM = 1.1
RING_MIN_MM = 3.0
ARROW_OFFSET_MM = (-16.0, -11.0)
ARROW_HEAD_MM = 2.2
BRACKET_DEPTH_MM = 2.4


def _mark_point(plan, where, frame) -> tuple[float, float] | None:
    """A mark's place in sheet mm: [lon, lat], or {layer, where: {field: value}} (callout_anchor's two forms)."""
    if isinstance(where, (list, tuple)) and len(where) == 2:
        return callout_anchor(plan, {"at": where}, frame)
    if isinstance(where, dict):
        return callout_anchor(plan, where, frame)
    return None


def _stroke(d: str, color: str, halo: str, *, w: float = MARK_INK_MM, fill: str = "none") -> str:
    return (f'<path d="{d}" fill="none" stroke="{halo}" stroke-width="{sd.num(w + MARK_HALO_MM - MARK_INK_MM)}" stroke-linecap="round" stroke-linejoin="round" stroke-opacity="0.85"/>'
            f'<path d="{d}" fill="{fill}" stroke="{color}" stroke-width="{sd.num(w)}" stroke-linecap="round" stroke-linejoin="round"/>')


def draw_marks(plan, frame, cfg: dict, taken: list[Rect], fonts: dict, under: dict | None = None) -> tuple[str, list[Rect], list[dict]]:
    """elements.marks (2.0.0): {on, items: [...]}: the few drawn marks that point without explaining. Drawn over the data in the label ink
    with a halo of the ground, before the labels (which keep clear of them).

      {type: ring, at: [lon, lat] | layer: id, where: {field: value}, radius_mm: 5, label: "Quincy"}      a circle round a place
      {type: arrow, to: [lon, lat] | {layer, where}, from: [lon, lat] | offset_mm: [dx, dy], label: "692 MW queued"}   a curved arrow; its label at the tail
      {type: bracket, from: [lon, lat], to: [lon, lat], label: "42 km with no site", side: left | right}  a bracket along a stretch

    A mark whose place is not found, is outside the frame, or lies under a map element (`under`: {element name: rect}, the legend, the
    locator, the title) is left out with a MAP-CART-026 WARN: a ring round a place the legend covers points at nothing. -> (svg, rects, findings)."""
    t = plan.tokens

    def covered(*pts) -> str | None:
        for name, r in (under or {}).items():
            if any(q is not None and r.x0 <= q[0] <= r.x1 and r.y0 <= q[1] <= r.y1 for q in pts):
                return str(name).replace("_", " ")
        return None
    ink, halo = t["label"], t["label_halo"]
    pt = float(fonts["label"])
    area = frame.rect
    out, rects, fs = [], [], []
    for i, it in enumerate(cfg.get("items") or []):
        if not isinstance(it, dict) or it.get("type") not in MARK_TYPES:
            fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1}: type is one of {', '.join(MARK_TYPES)}; it is not drawn"))
            continue
        kind = it["type"]
        label = str(it.get("label") or "").strip()
        g = [f'<g class="gm-mark" data-type="{kind}" data-mark="{i + 1}">']
        if kind == "ring":
            p = _mark_point(plan, it.get("at") if it.get("at") is not None else {k: it[k] for k in ("layer", "where") if k in it}, frame)
            if p is None or not (area.x0 <= p[0] <= area.x1 and area.y0 <= p[1] <= area.y1):
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (ring): its place was not found inside the map frame; it is not drawn"))
                continue
            if covered(p):
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (ring): its place lies under the map's {covered(p)}; it is not drawn. Mark a place the "
                            "map shows, or give the map more room on that side (extent.padding_pct)"))
                continue
            r = max(RING_MIN_MM, float(it.get("radius_mm") or 4.5))
            g.append(f'<circle cx="{sd.num(p[0])}" cy="{sd.num(p[1])}" r="{sd.num(r)}" fill="none" stroke="{halo}" stroke-width="{sd.num(MARK_HALO_MM)}" stroke-opacity="0.85"/>'
                     f'<circle cx="{sd.num(p[0])}" cy="{sd.num(p[1])}" r="{sd.num(r)}" fill="none" stroke="{ink}" stroke-width="{sd.num(MARK_INK_MM)}"/>')
            rects.append(Rect(p[0] - r, p[1] - r, p[0] + r, p[1] + r))
            if label:
                tw = tm.width_mm(label, pt, 600)
                lx, ly = p[0], p[1] - r - 1.2
                if ly - pt * PT < area.y0:
                    ly = p[1] + r + pt * PT + 0.8
                g.append(sd.text(lx, ly, label, pt, ink, weight=600, anchor="middle", halo=halo, halo_w=max(0.3, pt * PT * 0.22)))
                rects.append(Rect(lx - tw / 2, ly - pt * PT, lx + tw / 2, ly + 0.6))
        elif kind == "arrow":
            to = _mark_point(plan, it.get("to"), frame)
            if to is None or not (area.x0 <= to[0] <= area.x1 and area.y0 <= to[1] <= area.y1):
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (arrow): the place it points to was not found inside the map frame; it is not drawn"))
                continue
            if covered(to):
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (arrow): the place it points to lies under the map's {covered(to)}; it is not drawn. "
                            "Point at a place the map shows, or give the map more room on that side (extent.padding_pct)"))
                continue
            a = _mark_point(plan, it.get("from"), frame) if it.get("from") is not None else None
            if a is None:
                dx, dy = (float(v) for v in (it.get("offset_mm") or ARROW_OFFSET_MM))
                a = (min(max(to[0] + dx, area.x0 + 3), area.x1 - 3), min(max(to[1] + dy, area.y0 + 3), area.y1 - 3))
            vx, vy = to[0] - a[0], to[1] - a[1]
            ln = math.hypot(vx, vy)
            if ln < 3.0:
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (arrow): its start and end are under 3 mm apart; it is not drawn"))
                continue
            ux, uy = vx / ln, vy / ln
            gap = float(it.get("gap_mm", 1.8))                 # the head stops short of the place, so it points at it and does not cover it
            tip = (to[0] - ux * gap, to[1] - uy * gap)
            bend = min(ln * 0.18, 6.0)
            cx, cy = (a[0] + tip[0]) / 2 - uy * bend, (a[1] + tip[1]) / 2 + ux * bend
            hx, hy = tip[0] - cx, tip[1] - cy
            hl = math.hypot(hx, hy) or 1.0
            hx, hy = hx / hl, hy / hl
            h = ARROW_HEAD_MM
            left = (tip[0] - hx * h - hy * h * 0.45, tip[1] - hy * h + hx * h * 0.45)
            right = (tip[0] - hx * h + hy * h * 0.45, tip[1] - hy * h - hx * h * 0.45)
            g.append(_stroke(f"M{sd.num(a[0])} {sd.num(a[1])}Q{sd.num(cx)} {sd.num(cy)} {sd.num(tip[0])} {sd.num(tip[1])}", ink, halo))
            g.append(_stroke(f"M{sd.num(left[0])} {sd.num(left[1])}L{sd.num(tip[0])} {sd.num(tip[1])}L{sd.num(right[0])} {sd.num(right[1])}Z", ink, halo, fill=ink))
            rects.append(Rect(min(a[0], tip[0], cx) - 0.5, min(a[1], tip[1], cy) - 0.5, max(a[0], tip[0], cx) + 0.5, max(a[1], tip[1], cy) + 0.5))
            if label:
                tw = tm.width_mm(label, pt, 600)
                anchor = "end" if ux > 0 else "start"
                lx = a[0] - 1.0 if ux > 0 else a[0] + 1.0
                ly = a[1] + pt * PT * 0.35
                g.append(sd.text(lx, ly, label, pt, ink, weight=600, anchor=anchor, halo=halo, halo_w=max(0.3, pt * PT * 0.22)))
                rects.append(Rect(lx - tw if anchor == "end" else lx, ly - pt * PT, lx if anchor == "end" else lx + tw, ly + 0.6))
        else:                                                  # bracket
            a, b = _mark_point(plan, it.get("from"), frame), _mark_point(plan, it.get("to"), frame)
            if a is None or b is None:
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (bracket): it needs from: [lon, lat] and to: [lon, lat]; it is not drawn"))
                continue
            if covered(a, b):
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (bracket): an end lies under the map's {covered(a, b)}; it is not drawn"))
                continue
            vx, vy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(vx, vy)
            if ln < 4.0:
                fs.append(f("MAP-CART-026", "WARN", f"mark {i + 1} (bracket): its ends are under 4 mm apart on the sheet; it is not drawn"))
                continue
            nx, ny = -vy / ln, vx / ln
            if str(it.get("side") or "left") == "right":
                nx, ny = -nx, -ny
            off, d = float(it.get("offset_mm", 2.0)), float(it.get("depth_mm", BRACKET_DEPTH_MM))
            p0 = (a[0] + nx * off, a[1] + ny * off)
            p1 = (a[0] + nx * (off + d), a[1] + ny * (off + d))
            p2 = (b[0] + nx * (off + d), b[1] + ny * (off + d))
            p3 = (b[0] + nx * off, b[1] + ny * off)
            g.append(_stroke("M" + "L".join(f"{sd.num(x)} {sd.num(y)}" for x, y in (p0, p1, p2, p3)), ink, halo))
            xs, ys = [p[0] for p in (p0, p1, p2, p3)], [p[1] for p in (p0, p1, p2, p3)]
            rects.append(Rect(min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5))
            if label:
                tw = tm.width_mm(label, pt, 600)
                mx_, my_ = (p1[0] + p2[0]) / 2 + nx * (pt * PT * 0.9), (p1[1] + p2[1]) / 2 + ny * (pt * PT * 0.9)
                anchor = "middle" if abs(nx) < 0.5 else ("start" if nx > 0 else "end")
                ly = my_ + pt * PT * 0.35
                g.append(sd.text(mx_, ly, label, pt, ink, weight=600, anchor=anchor, halo=halo, halo_w=max(0.3, pt * PT * 0.22)))
                x0 = mx_ - tw / 2 if anchor == "middle" else (mx_ if anchor == "start" else mx_ - tw)
                rects.append(Rect(x0, ly - pt * PT, x0 + tw, ly + 0.6))
        g.append("</g>")
        out.append("".join(g))
    return ('<g class="gm-marks">' + "".join(out) + "</g>") if out else "", rects, fs
