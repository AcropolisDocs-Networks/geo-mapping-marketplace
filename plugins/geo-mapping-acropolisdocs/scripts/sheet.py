#!/usr/bin/env python3
"""sheet: two to six maps on one sheet with one shared frame (1.3.0, R1-PANEL; a panel or diptych sheet).

    build_map.py --spec sheet.yaml --out DIR          a file with a top-level `sheet_version` is a SheetSpec (schemas/sheetspec.schema.json)

    sheet_version: 1
    id: phoenix-heat-diptych
    template: tabloid-landscape
    title: {text: "Phoenix: Shade and Heat", subtitle: "Tree canopy and land surface temperature, July 2026"}
    arrangement: {columns: 2, rows: 1, gap_mm: 6, caption: below}     # at most 6 panels
    shared:
      extent: union        # union | first | each
      scale: same          # same | each (each only with extent: each; MAP-PNL-004)
      legend: each         # shared | each | none
      locator: one         # one | each | none
      north_arrow: one     # one | each | none
      scale_bar: one       # one | each | none
      sources: one         # one consolidated Sources line for the sheet
    panels:
      - spec: canopy.yaml
        caption: "Shade: tree canopy share"
      - base: canopy.yaml                    # or a second spec
        override: {layers: [{id: lst, source: {path: data/lst_jul2026.tif}}]}
        caption: "Heat: land surface temperature"

How the panels share one frame. The sheet's title band, footer and source line come from the template; the map area is cut into equal panel
rects with a caption band each. What is shared (`one`, `shared`) is drawn once, in a band on the side or bottom with the most room, never inside
a panel. With `legend: each` every panel keeps the same legend room, the largest panel legend's. The frame is computed once (the union of the
panels' extents, or the first's) and handed to every panel's render (`map_render.render(plan, frame=...)`), so the frames are identical by
construction. Panels that classify the same field of the same layer share their class edges (the atlas rule); every panel takes the first
panel's display CRS. The atlas's contact sheet is drawn on this path too (`contact`), up to 24 tiles with one shared legend.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import geo_locale as gl  # noqa: E402
import layout as lo  # noqa: E402

PANEL_MAX = 6
CONTACT_MAX = 24
CAPTION_MM = 6.0
BAND_GAP_MM = 4.0
LEGEND_MAX_SHARE = 0.3             # a panel's own legend (legend: each) is at most this share of the panel's width; its title wraps
PANEL_MIN_MM = 40.0                 # MAP-PNL-006: below this a panel cannot hold its labels at the template's minimum type size
SHARED_DEFAULTS = {"extent": "union", "scale": "same", "legend": "each", "locator": "one", "north_arrow": "one", "scale_bar": "one", "sources": "one"}
ELEMENT_OF = {"legend": "legend", "locator": "inset_map", "north_arrow": "north_arrow", "scale_bar": "scale_bar"}
GROUP_OF = {"legend": "gm-legend", "locator": "gm-inset", "north_arrow": "gm-north", "scale_bar": "gm-scalebar"}


class SheetError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-PNL-001"):
        super().__init__(message)
        self.fid = fid


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def is_sheet(path) -> bool:
    try:
        d = gc.load_yaml(Path(path))
    except Exception:  # noqa: BLE001 - not YAML: not a sheet (the MapSpec reader says what is wrong)
        return False
    return isinstance(d, dict) and "sheet_version" in d


def load(path) -> dict:
    import jsonschema
    d = gc.load_yaml(Path(path))
    schema = json.loads((gc.plugin_root() / "schemas" / "sheetspec.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(d, schema)
    except jsonschema.ValidationError as ex:
        raise SheetError(f"the SheetSpec is malformed at {'/'.join(str(p) for p in ex.absolute_path) or 'top'}: {ex.message}", "MAP-PNL-001") from None
    d["shared"] = {**SHARED_DEFAULTS, **(d.get("shared") or {})}
    d["arrangement"] = {"gap_mm": 6, "caption": "below", **(d.get("arrangement") or {})}
    return d


# ---------------------------------------------------------------------------------------------------------------- panel specs
def merge(base, over):
    """`override` onto a base MapSpec, as atlas pages are made: dicts merge key by key, `layers` merge by id (a new id is appended), anything
    else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            if k == "layers" and isinstance(v, list) and isinstance(base.get("layers"), list):
                layers = [copy.deepcopy(x) for x in base["layers"]]
                for ly in v:
                    i = next((n for n, x in enumerate(layers) if x.get("id") == ly.get("id")), None)
                    if i is None:
                        layers.append(copy.deepcopy(ly))
                    else:
                        layers[i] = merge(layers[i], ly)
                out[k] = layers
            else:
                out[k] = merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def panel_specs(sheet: dict, sheet_path: Path, work: Path) -> list[dict]:
    """Each panel's MapSpec written into `work` with absolute paths: [{spec (path), caption, index, data (dict)}]. MAP-PNL-001 when one is missing."""
    import atlas
    import yaml
    out = []
    work.mkdir(parents=True, exist_ok=True)
    for k, p in enumerate(sheet["panels"]):
        ref = p.get("spec") or p.get("base")
        src = (sheet_path.parent / ref) if not Path(ref).is_absolute() else Path(ref)
        if not src.is_file():
            raise SheetError(f"panel {k + 1}: the MapSpec {ref} does not exist", "MAP-PNL-001")
        try:
            spec = gc.load_mapspec(src)
        except Exception as ex:  # noqa: BLE001
            raise SheetError(f"panel {k + 1}: {ref} cannot be read: {str(ex).splitlines()[0][:200]}", "MAP-PNL-001") from None
        atlas._abs_paths(spec, src.parent)
        if p.get("override"):
            over = copy.deepcopy(p["override"])
            atlas._abs_paths(over, sheet_path.parent)
            spec = merge(spec, over)
        spec["id"] = f"{sheet['id']}-p{k + 1}"
        if sheet.get("template"):
            spec["template"] = sheet["template"]
        dest = work / f"panel-{k + 1}.yaml"
        dest.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
        out.append({"spec": str(dest), "caption": p.get("caption"), "index": k, "data": spec})
    return out


# ---------------------------------------------------------------------------------------------------------------- geometry
@dataclass
class Grid:
    panels: list                     # [Rect] the panels' map rects on the sheet
    captions: list                   # [(x, y)] caption baselines
    band: object = None              # Rect of the shared band, or None
    band_side: str | None = None     # right | bottom
    pw: float = 0.0
    ph: float = 0.0
    notes: list = field(default_factory=list)


def panel_grid(area, cols: int, rows: int, n: int, *, gap: float, caption: bool, band_w: float = 0.0, band_h: float = 0.0) -> Grid:
    """layout.panel_grid (R1-PANEL): the map area cut into cols x rows equal panels (each with its caption band), with the shared band on the right
    or at the bottom, whichever leaves the larger panels. band_w / band_h: the band's width on the right or height at the bottom (0: no band)."""
    cap = CAPTION_MM if caption else 0.0

    def cut(a, side):
        if side == "right":
            inner = lo.Rect(a.x0, a.y0, a.x1 - band_w - BAND_GAP_MM, a.y1)
            band = lo.Rect(inner.x1 + BAND_GAP_MM, a.y0, a.x1, a.y1)
        elif side == "bottom":
            inner = lo.Rect(a.x0, a.y0, a.x1, a.y1 - band_h - BAND_GAP_MM)
            band = lo.Rect(a.x0, inner.y1 + BAND_GAP_MM, a.x1, a.y1)
        else:
            inner, band = a, None
        pw = (inner.w - (cols - 1) * gap) / cols
        ph = (inner.h - (rows - 1) * gap) / rows - cap
        return inner, band, pw, ph

    sides = ([None] if not (band_w or band_h) else [s for s, v in (("right", band_w), ("bottom", band_h)) if v])
    best = None
    for side in sides:
        inner, band, pw, ph = cut(area, side)
        if best is None or pw * ph > best[3] * best[4]:
            best = (side, inner, band, pw, ph)
    side, inner, band, pw, ph = best
    rects, caps = [], []
    for k in range(n):
        c, r = k % cols, k // cols
        x0 = inner.x0 + c * (pw + gap)
        y0 = inner.y0 + r * (ph + cap + gap)
        rects.append(lo.Rect(x0, y0, x0 + pw, y0 + ph))
        caps.append((x0, y0 + ph + cap * 0.72))
    return Grid(panels=rects, captions=caps, band=band, band_side=side, pw=pw, ph=ph)


def template_for_aspect(aspect: float) -> str:
    """A custom template whose map region (the in-map layout's map_full) has `aspect` (w / h), for a panel's plan: the plan-time windows (basemap,
    rasters, names) are computed for that shape, so they cover the panel."""
    w = 400.0
    lo_h, hi_h = lo.CUSTOM_MIN_MM if hasattr(lo, "CUSTOM_MIN_MM") else 100.0, 1200.0
    lo_h = max(lo_h, w / lo.CUSTOM_MAX_ASPECT + 1)
    hi_h = min(hi_h, w * lo.CUSTOM_MAX_ASPECT - 1, getattr(lo, "CUSTOM_MAX_MM", 2000.0))

    def asp(h):
        t = lo.custom_template(w, h)
        m = t.get("map_full") or t["map"]
        return m["w"] / m["h"]
    for _ in range(40):
        mid = (lo_h + hi_h) / 2
        if asp(mid) > aspect:
            lo_h = mid
        else:
            hi_h = mid
    return f"custom:{w:g}x{round((lo_h + hi_h) / 2, 1):g}"


def bare_template(base: dict, pw: float, ph: float, legend_w: float | None = None) -> dict:
    """The page a panel is drawn on: its map region is the whole panel (0, 0, pw, ph); the footer below it is never shown (the tile is the map)."""
    foot = 30.0
    lm = base.get("legend_max") or {"w": 80, "h": 120}
    return {**copy.deepcopy(base), "size_mm": [pw, ph + foot], "title": {"x": 0, "y": 0, "w": pw, "h": 0}, "map": {"x": 0, "y": 0, "w": pw, "h": ph},
            "map_full": {"x": 0, "y": 0, "w": pw, "h": ph}, "footer": {"x": 0, "y": ph, "w": pw, "h": foot}, "footer_fit": False, "layout": "in-map",
            "legend_max": {"w": round(min(float(lm["w"]), legend_w if legend_w else pw * LEGEND_MAX_SHARE, pw - 2 * lo.CORNER_INSET_MM), 1),
                           "h": round(min(float(lm["h"]), ph * 0.85), 1)}}


# ---------------------------------------------------------------------------------------------------------------- shared classes
def shared_classes(plans: list) -> dict:
    """{(layer id, field): {edges, decimals, integer_data, method}} for numeric classified layers that two or more panels have: the edges of the
    panels' values together, with the first panel's method and class count (MAP-CART-014, the atlas rule)."""
    import numpy as np

    import classify as cf
    seen: dict = {}
    for plan in plans:
        for l in plan.layers:
            c = l.cls
            if c is None or getattr(c, "kind", "") != "numeric" or not l.value_field or l.value_field not in l.gdf.columns:
                continue
            seen.setdefault((l.id, l.value_field), []).append(l)
    out = {}
    for key, ls in seen.items():
        if len(ls) < 2:
            continue
        vals = np.concatenate([ly.gdf[key[1]].to_numpy(dtype="float64", na_value=np.nan) for ly in ls])
        vals = vals[np.isfinite(vals)]
        if not len(vals):
            continue
        c0 = ls[0].cls
        cspec = dict(ls[0].spec.get("classification") or {})
        try:
            res = cf.classify(vals.tolist(), cspec.get("method", c0.method), int(cspec.get("classes", c0.k)))
        except Exception:  # noqa: BLE001 - classes that cannot be shared stay each panel's own (MAP-PNL-005 if one legend is asked for)
            continue
        out[key] = {"edges": [float(e) for e in res.edges], "decimals": int(res.decimals), "integer_data": bool(res.integer_data), "method": res.method}
    return out


# ---------------------------------------------------------------------------------------------------------------- the sheet
def _sources_line(plans: list) -> str:
    import credits
    parts: list[str] = []
    for plan in plans:
        line, _ = credits.consolidated(plan, "")
        for p in [x.strip() for x in re.split(r";\s+|(?<=\.)\s+(?=This product)", line.rstrip(".")) if x.strip()]:
            if p not in parts:
                parts.append(p)
    return ("Sources: " + "; ".join(parts) + ".") if parts else ""


def compose(sheet: dict, panels: list[dict], tpl: dict, *, contact: bool = False) -> dict:
    """The sheet from prepared panel plans ([{plan, caption}]): -> {svg, report, findings, rendered: [Rendered], frames}. `tpl` is the sheet's
    template (classic layout: the title band above the map area)."""
    import atlas
    import map_render as mr
    import svg_draw as sd
    import textmeasure as tm
    sh = sheet["shared"]
    fs: list[dict] = []
    plans = [p["plan"] for p in panels]
    p0 = plans[0]
    t = p0.tokens
    W, H = (float(v) for v in tpl["size_mm"])
    fonts = dict(tpl["fonts"])
    area = lo.rect_of(tpl["map"])
    arr = sheet["arrangement"]
    n = len(panels)
    cols = int(arr.get("columns") or 0)
    rows = int(arr.get("rows") or 0)
    if not cols or not rows:
        cols = int(arr.get("columns") or max(1, round((n * area.w / max(area.h, 1)) ** 0.5)))
        rows = -(-n // cols)
    if cols * rows < n:
        raise SheetError(f"the arrangement {cols} x {rows} has room for {cols * rows} panels, not {n}", "MAP-PNL-002")
    band_items = [k for k in ("legend", "locator", "north_arrow", "scale_bar") if sh.get(k) in ("one", "shared")]
    band_w = min(float(tpl["legend_max"]["w"]), W * 0.24) if band_items else 0.0
    band_h = min(float(tpl["legend_max"]["h"]) * 0.45, H * 0.22) if band_items else 0.0
    grid = panel_grid(area, cols, rows, n, gap=float(arr.get("gap_mm", 6)), caption=arr.get("caption", "below") != "none" or contact,
                      band_w=band_w, band_h=band_h)
    if min(grid.pw, grid.ph) < PANEL_MIN_MM and not contact:
        fs.append(finding("MAP-PNL-006", "BLOCK", f"each panel would be {grid.pw:.0f} x {grid.ph:.0f} mm, too small for the template's minimum type sizes "
                          f"(at least {PANEL_MIN_MM:g} mm a side); use a larger template or fewer panels"))
        return {"svg": None, "report": {}, "findings": fs, "rendered": [], "frames": []}
    # ---- one frame (or one per panel with extent: each)
    pad = float((p0.spec.get("extent") or {}).get("padding_pct", 5))
    local = lo.Rect(0.0, 0.0, grid.pw, grid.ph)
    reserve = (0.0, 0.0, 0.0, 0.0)
    if sh.get("legend") == "each" and not contact:
        widths = []
        for plan in plans:
            import legend as lg
            blocks = [l.legend_block for l in plan.layers if l.legend_block]
            if blocks:
                fit = lg.fit_legend(blocks, min(float(tpl["legend_max"]["w"]), grid.pw * LEGEND_MAX_SHARE), grid.ph * 0.85, template_kind=tpl.get("kind", "print"),
                                    start_pt=float(fonts.get("legend") or 0) or None)
                if fit.get("fits"):
                    widths.append(float(fit["size"]["width"]))
        if widths:
            reserve = (0.0, 0.0, max(widths) + 2 * lo.CORNER_INSET_MM, 0.0)      # the largest panel legend's room, the same in every panel
    if sh.get("scale_bar") == "each" or contact:          # a scale bar on every panel: its strip kept free of data, the same in every panel
        reserve = (reserve[0], reserve[1], reserve[2], reserve[3] + 2.6 * float(fonts.get("scale", 8)) * 0.3528 + 2 * lo.CORNER_INSET_MM)
    exts = [[float(v) for v in plan.extent] for plan in plans]
    union = [min(e[0] for e in exts), min(e[1] for e in exts), max(e[2] for e in exts), max(e[3] for e in exts)]
    frames = []
    mode = sh.get("extent", "union")
    if mode == "each":
        frames = [lo.Frame.fit(local, e, pad, reserve) for e in exts]
        if sh.get("scale", "same") == "same":
            s = min(f_.scale for f_ in frames)
            frames = [lo.Frame(f_.rect, s, f_.cx, f_.cy, f_.inner) for f_ in frames]
        elif not contact:
            fs.append(finding("MAP-PNL-004", "WARN", "the panels are at different scales (shared.scale: each): a reader cannot compare their sizes; the scale bars are drawn on each panel"))
    else:
        one = lo.Frame.fit(local, exts[0] if mode == "first" else union, pad, reserve)
        frames = [one] * n
    # ---- each panel drawn in its frame; the shared elements left out of the tiles
    off = [ELEMENT_OF[k] for k in ELEMENT_OF if sh.get(k) in ("one", "shared", "none")] + ["title", "logo"]
    if contact:
        off = ["legend", "inset_map", "north_arrow", "title", "logo"]
    if mode == "each" and sh.get("scale") == "each" and "scale_bar" in off:
        off.remove("scale_bar")
    rend = []
    for k, (plan, fr) in enumerate(zip(plans, frames, strict=True)):
        shared_legend = k == 0 and sh.get("legend") in ("one", "shared") and grid.band is not None
        plan.tpl = bare_template(tpl, grid.pw, grid.ph, legend_w=(grid.band.w - 2 if grid.band_side == "right" else grid.band.w * 0.5) if shared_legend else None)
        plan.tpl_name = tpl.get("name", plan.tpl_name)
        keep_on = [x for x in off if not (k == 0 and x in ("legend", "inset_map", "north_arrow", "scale_bar")
                                           and sh.get({v: kk for kk, v in ELEMENT_OF.items()}[x]) in ("one", "shared"))]
        r = mr.render(plan, frame=lo.Frame(local, fr.scale, fr.cx, fr.cy, fr.inner), elements_off=keep_on)
        rend.append(r)
        for f_ in r.findings:
            if f_["id"] in ("MAP-CART-005", "MAP-LIC-011", "MAP-CART-020", "MAP-CART-021"):
                continue                     # the legend, scale bar and north arrow are the sheet's; the sources are the sheet's line
            fs.append({**f_, "message": f"[panel {k + 1}] {f_['message']}"})
    # ---- the sheet
    out = [sd.rect(0, 0, W, H, t["background"], extra='class="gm-page"')]
    ti = tpl["title"]
    st = sheet.get("title") or {}
    if st.get("text"):
        out.append(f'<g class="gm-title">{sd.text(ti["x"], ti["y"] + fonts["title"] * 0.3528 * 0.95, st["text"], fonts["title"], t["label"], weight=700)}'
                   + (sd.text(ti["x"], ti["y"] + fonts["title"] * 0.3528 * 0.95 + fonts["subtitle"] * 0.3528 * 1.5, st["subtitle"], fonts["subtitle"], t["label"])
                      if st.get("subtitle") else "") + "</g>")
    drop = tuple(g for g in atlas.SHEET_DROP if not (g == "gm-legend" and sh.get("legend") == "each" and not contact)
                 and not (g == "gm-scalebar" and "scale_bar" not in off))
    rects_rep = []
    for k, (r, rr, cap) in enumerate(zip(rend, grid.panels, grid.captions, strict=True)):
        inner = _tile(r.svg, (0.0, 0.0, grid.pw, grid.ph), f"p{k + 1}-", drop)
        out.append(f'<svg x="{sd.num(rr.x0)}" y="{sd.num(rr.y0)}" width="{sd.num(rr.w)}" height="{sd.num(rr.h)}" viewBox="0 0 {sd.num(grid.pw)} {sd.num(grid.ph)}" '
                   f'class="gm-panel" data-panel="{k + 1}">{inner}</svg>')
        out.append(sd.rect(rr.x0, rr.y0, rr.w, rr.h, "none", t["frame"], 0.25))
        if panels[k].get("caption"):
            out.append(sd.text(cap[0], cap[1], str(panels[k]["caption"]), float(fonts.get("legend_title", 9)), t["label"], weight=600))
        fr = r.report.get("frame") or {}
        rects_rep.append({"panel": k + 1, "rect_mm": [round(rr.x0 + v, 3) if i % 2 == 0 else round(rr.y0 + v, 3) for i, v in enumerate(fr.get("rect_mm", [0, 0, grid.pw, grid.ph]))],
                          "scale": fr.get("scale"), "cx": fr.get("cx"), "cy": fr.get("cy")})
    # ---- the shared band: legend, locator, north arrow and scale bar from the first panel, once
    if grid.band is not None:
        ov = rend[0].report.get("overlays") or {}
        bx, by = grid.band.x0, grid.band.y0
        placed_band = []
        for k in band_items:
            name = {"legend": "legend", "locator": "inset", "north_arrow": "north", "scale_bar": "scale"}[k]
            if name not in ov:
                continue
            R = ov[name]["rect_mm"]
            w, h = R[2] - R[0], R[3] - R[1]
            if grid.band_side == "right" and by + h > grid.band.y1 + 0.5 or grid.band_side == "bottom" and bx + w > grid.band.x1 + 0.5:
                fs.append(finding("MAP-PNL-006", "WARN", f"the shared {k.replace('_', ' ')} does not fit in the sheet's band and is left out"))
                continue
            vb = f"{sd.num(R[0] - 1)} {sd.num(R[1] - 1)} {sd.num(w + 2)} {sd.num(h + 2)}"
            out.append(f'<svg x="{sd.num(bx)}" y="{sd.num(by)}" width="{sd.num(w + 2)}" height="{sd.num(h + 2)}" viewBox="{vb}" class="gm-sheet-{k.replace("_", "-")}">'
                       f'{_tile(rend[0].svg, R, f"b{k}-", keep_only=GROUP_OF[k])}</svg>')
            placed_band.append(k)
            if grid.band_side == "right":
                by += h + 2 + BAND_GAP_MM
            else:
                bx += w + 2 + BAND_GAP_MM
    # ---- footer: one Sources line for the sheet, the date stamp and the logo
    ft = tpl["footer"]
    npt = float(fonts["note"])
    logo_svg, logo_w = mr.logo_markup(p0, t["background"], 0, 0, float(tpl.get("logo_height_mm", 10)))
    lines = []
    if sh.get("sources", "one") == "one":
        src = _sources_line(plans)
        lines += tm.wrap(src, ft["w"] - logo_w - 8.0, npt, 400) if src else []
    els0 = p0.spec.get("elements") or {}
    tml = p0.pack["pack"].get("trademark_line")
    if tml and (els0.get("trademark_line") or {}).get("on", "auto") in ("auto", True):
        lines += tm.wrap(tml, ft["w"] - logo_w - 8.0, npt, 400)
    d = mr.today()
    lines.append(f"Map produced {gl.plan_date_text(p0, d)}")
    fo = ['<g class="gm-footer">']
    y = ft["y"] + npt * 0.3528 * 1.2
    for ln in lines:
        fo.append(sd.text(ft["x"], y, ln, npt, t["label"]))
        y += tm.line_height_mm(npt)
    if logo_svg:
        fo.append(mr.logo_markup(p0, t["background"], ft["x"] + ft["w"] - logo_w, ft["y"] + 1.0, float(tpl.get("logo_height_mm", 10)))[0])
    fo.append("</g>")
    out += fo
    title = st.get("text") or sheet["id"]
    alt = f"A sheet of {n} maps: " + "; ".join(str(p.get("caption") or f"panel {k + 1}") for k, p in enumerate(panels)) + "."
    svg = mr.assemble_page(p0, W, H, "".join(out), title, alt)
    rep = {"template": tpl.get("name"), "panels": rects_rep, "grid": {"columns": cols, "rows": rows, "panel_mm": [round(grid.pw, 2), round(grid.ph, 2)],
                                                                      "band": grid.band_side, "band_items": band_items},
           "shared": dict(sh), "frame_shared": len({(round(x["scale"] or 0, 12), round(x["cx"] or 0, 6), round(x["cy"] or 0, 6)) for x in rects_rep}) == 1}
    return {"svg": svg, "report": rep, "findings": fs, "rendered": rend, "frames": frames}


def _tile(svg: str, rect, prefix: str, drop: tuple = (), keep_only: str | None = None) -> str:
    import atlas
    if keep_only:
        return atlas._tile_svg(svg, list(rect), prefix, keep_only=keep_only)
    saved = atlas.SHEET_DROP
    try:
        atlas.SHEET_DROP = drop
        return atlas._tile_svg(svg, list(rect), prefix)
    finally:
        atlas.SHEET_DROP = saved


# ---------------------------------------------------------------------------------------------------------------- build
def build(sheet_path: str, out_dir: str, formats: list[str] | None = None, *, brand: str | None = None, template: str | None = None,
          strict: bool = False, export_options: dict | None = None) -> dict:
    """A sheet from its SheetSpec: plan every panel (two passes: the first finds the shared CRS, extent and classes, the second plans with them),
    compose, lint, export (SVG, PDF, PNG), provenance and the terms register."""
    import build_map as bm
    import map_export as mx
    import map_plan as mp
    import provenance
    import terms_register as tr
    import yaml
    sheet_path = Path(sheet_path)
    sheet = load(sheet_path)
    fs: list[dict] = []
    result = {"id": sheet["id"], "kind": "sheet", "findings": fs, "blocked": False, "files": []}

    def done():
        fs.sort(key=lambda x: (bm.SEV.get(x["severity"], 3), x["id"]))
        result["blocked"] = any(x["severity"] == "BLOCK" for x in fs)
        return result
    if len(sheet["panels"]) > PANEL_MAX:
        fs.append(finding("MAP-PNL-002", "BLOCK", f"{len(sheet['panels'])} panels: a sheet holds at most {PANEL_MAX}; for more, make an atlas with a contact sheet"))
        return done()
    work = Path(out_dir) / ".sheet-work"
    try:
        pspecs = panel_specs(sheet, sheet_path, work)
    except SheetError as ex:
        fs.append(finding(ex.fid, "BLOCK", str(ex)))
        return done()
    pack = ((sheet.get("style") or {}).get("pack")) or "default"
    for ps in pspecs:                                   # one pack per build (4.2): a panel that names another is refused
        pp = ((ps["data"].get("style") or {}).get("pack"))
        if pp and pp != pack:
            fs.append(finding("MAP-PNL-007", "BLOCK", f"panel {ps['index'] + 1} names the style pack '{pp}', but the sheet's is '{pack}': one pack per build"))
        elif pack != "default" and not pp:                # 1.4.0: the sheet's pack is every panel's
            st_ = ps["data"].get("style") if isinstance(ps["data"].get("style"), dict) else {}
            ps["data"]["style"] = {**st_, "pack": pack}
            Path(ps["spec"]).write_text(__import__("yaml").safe_dump(ps["data"], sort_keys=False, allow_unicode=True))
    if any(x["severity"] == "BLOCK" for x in fs):
        return done()
    tname = template or sheet.get("template") or "tabloid-landscape"
    try:
        tpl = lo.apply_layout(lo.load_template(tname), {"elements": {"title": {"placement": "outside"}}}, classic=True)
    except lo.LayoutError as ex:
        fs.append(finding("MAP-PNL-001", "BLOCK", str(ex)))
        return done()
    tpl["name"] = lo.template_label(tname)
    # pass 1: each panel as written
    first = []
    for ps in pspecs:
        try:
            first.append(mp.prepare(ps["spec"], brand=brand, template=tname))
        except mp.PlanError as ex:
            fs.append(finding("MAP-PNL-001", "BLOCK", f"panel {ps['index'] + 1} does not plan: {str(ex).splitlines()[0][:300]}"))
    if len(first) != len(pspecs):
        return done()
    crs0 = first[0].crs
    for ps, plan in zip(pspecs[1:], first[1:], strict=True):
        named = ((ps["data"].get("crs") or {}).get("display"))
        if named and plan.crs != crs0:
            fs.append(finding("MAP-PNL-003", "BLOCK", f"panel {ps['index'] + 1} names the display CRS {named}, but the sheet's panels take the first panel's, {crs0}"))
    if any(x["severity"] == "BLOCK" for x in fs):
        return done()
    classes = shared_classes(first)
    sh = sheet["shared"]
    if sh.get("legend") == "shared":
        for plan in first[1:]:
            for l in plan.layers:
                a = next((x for x in first[0].layers if x.id == l.id), None)
                if l.cls is not None and (a is None or a.cls is None or (getattr(l.cls, "kind", "") == "numeric" and (l.id, l.value_field) not in classes
                                                                     and list(getattr(l.cls, "edges", []) or []) != list(getattr(a.cls, "edges", []) or []))):
                    fs.append(finding("MAP-PNL-005", "BLOCK", f"shared.legend is shared, but layer '{l.id}' is classed differently in another panel; use legend: each"))
                    break
    if any(x["severity"] == "BLOCK" for x in fs):
        return done()
    # pass 2: the first panel's CRS, the shared classes and, for union or first, an extent every panel's plan covers
    from pyproj import Transformer
    tx = Transformer.from_crs(crs0, "EPSG:4326", always_xy=True)
    exts = [[float(v) for v in p.extent] for p in first]
    ext = exts[0] if sh.get("extent") == "first" else [min(e[0] for e in exts), min(e[1] for e in exts), max(e[2] for e in exts), max(e[3] for e in exts)]
    import numpy as np
    X, Y = np.meshgrid(np.linspace(ext[0], ext[2], 9), np.linspace(ext[1], ext[3], 9))
    lon, lat = tx.transform(X.ravel(), Y.ravel())
    bbox = [float(np.nanmin(lon)), float(np.nanmin(lat)), float(np.nanmax(lon)), float(np.nanmax(lat))]
    arr = sheet["arrangement"]
    n = len(pspecs)
    cols, rows = int(arr.get("columns") or 0), int(arr.get("rows") or 0)
    area = lo.rect_of(tpl["map"])
    if not cols or not rows:
        cols = cols or max(1, round((n * area.w / max(area.h, 1)) ** 0.5))
        rows = -(-n // cols)
    aspect = ((area.w * 0.8 - (cols - 1) * 6) / cols) / max(1.0, (area.h - (rows - 1) * 6) / rows - CAPTION_MM)
    ptpl = template_for_aspect(max(0.3, min(3.0, aspect)))
    plans = []
    for ps in pspecs:
        d = copy.deepcopy(ps["data"])
        d.setdefault("crs", {})["display"] = crs0
        if sh.get("extent") in ("union", "first"):
            d["extent"] = {**(d.get("extent") or {}), "mode": "bbox", "bbox": bbox}
            d["extent"].pop("place", None), d["extent"].pop("preset", None)
        for ly in d.get("layers") or []:
            for (lid, fld), e in classes.items():
                if ly.get("id") == lid and ly.get("value_field") == fld:
                    c = dict(ly.get("classification") or {})
                    c.update({"edges": e["edges"], "decimals": e["decimals"], "integer_data": e["integer_data"]})
                    c.pop("breaks", None)
                    ly["classification"] = c
        Path(ps["spec"]).write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
        try:
            plans.append(mp.prepare(ps["spec"], brand=brand, template=ptpl))
        except mp.PlanError as ex:
            fs.append(finding("MAP-PNL-001", "BLOCK", f"panel {ps['index'] + 1} does not plan with the sheet's CRS and extent: {str(ex).splitlines()[0][:300]}"))
            return done()
    if sh.get("extent") in ("union", "first"):                 # the frame is the panels' data, not the padded box the plans were given
        for plan in plans:
            plan.extent = ext
    try:
        comp = compose(sheet, [{"plan": p, "caption": ps["caption"]} for p, ps in zip(plans, pspecs, strict=True)], tpl)
    except SheetError as ex:
        fs.append(finding(ex.fid, "BLOCK", str(ex)))
        return done()
    fs += comp["findings"]
    if comp["svg"] is None:
        return done()
    svg = comp["svg"]
    p0 = plans[0]
    fs += bm.guard_findings(svg, p0)
    import output_lint
    fs += output_lint.check_svg(svg)
    if strict and any(x["severity"] == "BLOCK" for x in fs):
        return done()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{p0.pack['tokens'].get('output_prefix', '')}{sheet['id']}_{tpl['name']}{__import__('map_render').pack_suffix(p0)}_v{__import__('map_render').today().strftime('%Y%m%d')}"
    formats = formats or ["svg", "pdf"]
    written = []
    sp = out / f"{stem}.svg"
    sp.write_text(svg, encoding="utf-8")
    written.append(sp)
    title = (sheet.get("title") or {}).get("text") or sheet["id"]
    if any(f_ in formats for f_ in ("pdf", "png")):
        import map_brand
        props = {f_: map_brand.props_for(p0.brand, p0.manifest, f_) for f_ in ("pdf", "png")}
        dpi = float(p0.tpl.get("raster_default_dpi") or 300)
        ex = mx.export(svg, out, stem, [f_ for f_ in formats if f_ in ("pdf", "png")], props=props, title=title, alt="", dpi=min(dpi, 200),
                       subject=f"A sheet of {len(plans)} maps", options=mx.options_from(None, export_options))
        for fp in ex["files"]:
            written.append(Path(fp))
            if fp.endswith(".pdf"):
                fs.extend(output_lint.check_pdf(fp, ex["size_mm"], fonts_allowed=bm.pdf_fonts(p0)))
    # provenance: every panel's records, deduplicated, and a panels block
    recs, seen = [], set()
    for k, plan in enumerate(plans):
        for r in provenance.records(plan):
            key = (r.get("source_name"), r.get("library_ref"), r.get("sha256"), r.get("layer_id") if not str(r.get("layer_id", "")).startswith("basemap:") else "")
            r = {**r, "layer_id": f"p{k + 1}:{r['layer_id']}"}
            if key in seen:
                continue
            seen.add(key)
            recs.append(r)
    doc = {"map_id": sheet["id"], "kind": "sheet", "records": recs,
           "panels": [{"panel": k + 1, "spec": Path(ps["spec"]).name, "caption": ps["caption"], "frame": comp["report"]["panels"][k]} for k, ps in enumerate(pspecs)],
           "reproducibility": {"map_id": sheet["id"], "built": __import__("map_render").today().isoformat(), "library_version": provenance._library_version()}}
    prov = out / f"{stem}.provenance.json"
    prov.write_text(json.dumps(doc, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    tdoc, tfs, tfiles = tr.for_deliverable(out, recs, deliverable="sheet", stem=stem, outputs={"svg": svg}, title=title)
    fs += tfs
    if "svg" not in formats:
        sp.unlink()
        written.remove(sp)
    result.update({"files": [str(w) for w in written], "provenance": str(prov), "stem": stem, "report": comp["report"], "template": tpl["name"],
                   "terms": {"files": [str(x) for x in tfiles], "summary": tdoc["summary"]}, "crs": crs0})
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return done()
