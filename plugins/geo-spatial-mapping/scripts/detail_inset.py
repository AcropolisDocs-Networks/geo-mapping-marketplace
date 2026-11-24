#!/usr/bin/env python3
"""detail_inset: a closer look at one part of the map, in a corner of the same map (2.0.0).

    elements:
      detail: {on: true, layer: sites, where: {site: S07}}          # the feature(s) to look at closer
      detail: {on: true, bbox: [-122.34, 47.59, -122.32, 47.60]}    # or a window in longitude and latitude
      detail: {on: true, subject: true}                             # or what the map is about (the message's layers, their highlight)
              position: lower-left    w_mm: 70    title: "Lumen Field"    pad: 0.25
      detail: {on: true, bbox: [...], layers: [cells, {id: streets, source: ..., role: lines}], opacity: 0.7,
               basemap: {provider: carto-positron}, width: legend}          # 3.4.0 (Day 20 #23): its own layers, strength, ground and width

The locator in the lower right says where the map is; a detail inset says what is at one spot on it, at a scale where it can be read.
Day 15's stadium was a twentieth of a frame that also had to show three stations: with a detail inset the frame can keep the stations and
the stadium is still seen. It is the map's own layers, drawn again by the same renderer in a smaller frame (so its type and symbols are the
map's, not a shrunken copy), placed as a figure: labels keep clear of it, and every rebuild draws it again. Its window is outlined on the
map. It carries its own scale bar; the title, legend, north arrow, logo and locator are the map's alone.
"""
from __future__ import annotations

import copy

import layout as lo

WIDTH_MM = 70.0
ASPECT = 0.75                      # height to width
PAD = 0.25                         # the room round the features, as a share of their span
MIN_SHARE = 0.03                   # a window narrower than this share of the map's is widened to it (one point has no span)
OFF = ("title", "legend", "inset_map", "north_arrow", "logo")
DROP_KEYS = ("detail", "figures", "section", "callouts", "marks", "data_table", "methods_page")
TILE_DROP = ("gm-title", "gm-legend", "gm-inset", "gm-logo", "gm-north", "gm-footer", "gm-page")


class DetailError(ValueError):
    pass


def window(plan, cfg: dict) -> tuple:
    """The detail's window in the map's display coordinates. Raises DetailError when the place cannot be found."""
    import numpy as np
    from pyproj import Transformer
    full = tuple(float(v) for v in plan.extent)
    if cfg.get("bbox"):
        w, s, e, n = (float(v) for v in cfg["bbox"])
        tr = Transformer.from_crs("EPSG:4326", plan.crs, always_xy=True)
        xs, ys = tr.transform([w, e, w, e], [s, s, n, n])
        box = (min(xs), min(ys), max(xs), max(ys))
        pad = 0.0
    else:
        pad = float(cfg.get("pad", PAD))
        if cfg.get("layer"):
            l = next((x for x in plan.layers if x.id == str(cfg["layer"])), None)
            if l is None:
                raise DetailError(f"elements.detail names layer '{cfg['layer']}', which the map does not have")
            g = l.gdf
            for k, v in (cfg.get("where") or {}).items():
                if k not in g.columns:
                    raise DetailError(f"elements.detail: layer '{l.id}' has no field '{k}'")
                g = g[g[k].astype(str) == str(v)]
            if not len(g):
                raise DetailError(f"elements.detail: nothing in layer '{l.id}' matches {cfg.get('where')}")
            box = tuple(float(v) for v in g.total_bounds)
        elif cfg.get("subject"):
            import story_lint
            subj, singled = story_lint.subject(plan)
            if not singled:
                raise DetailError("elements.detail: subject: true needs a map whose message singles something out (a layer it names, or a highlight)")
            bs = np.array([(l.gdf if m is None else l.gdf[m]).total_bounds for l, m in subj])
            box = (float(bs[:, 0].min()), float(bs[:, 1].min()), float(bs[:, 2].max()), float(bs[:, 3].max()))
        else:
            raise DetailError("elements.detail needs layer (with where), bbox: [w, s, e, n] or subject: true")
    x0, y0, x1, y1 = box
    least = MIN_SHARE * max(full[2] - full[0], full[3] - full[1])
    if max(x1 - x0, y1 - y0) < least:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, y0, x1, y1 = cx - least / 2, cy - least / 2, cx + least / 2, cy + least / 2
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    return (x0 - px, y0 - py, x1 + px, y1 + py)


def for_map(plan, cfg: dict, legend_w: float | None = None) -> dict:
    """-> {figure (an elements.figures item the plugin drew), window (display coordinates, as drawn), notes}. 3.4.0 (Day 20 #23): `layers`
    (the ids of the map's layers to draw in it, and layers of its own written inline), `opacity` (the strength of its data layers),
    `basemap` (its own ground, a tile provider for a street inset) and `width: legend` (as wide as the legend, so the two line up)."""
    import atlas
    import map_plan as mp
    import map_render as mr
    import sheet
    from pyproj import Transformer
    box = window(plan, cfg)
    if str(cfg.get("width") or "") == "legend" and legend_w:
        w_mm = min(max(float(legend_w), 30.0), 140.0)
    else:
        w_mm = min(max(float(cfg.get("w_mm") or WIDTH_MM), 40.0), 140.0)
    h_mm = round(w_mm * float(cfg.get("aspect") or ASPECT), 2)
    tr = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform([box[0], box[2], box[0], box[2]], [box[1], box[1], box[3], box[3]])
    wgs = [min(lon), min(lat), max(lon), max(lat)]
    base = copy.deepcopy(plan.spec)

    def edit(sp: dict) -> None:
        sp.clear()
        sp.update(copy.deepcopy(base))                      # the map as it is being drawn (a story beat's edits included), not the file's
        sp["id"] = f"{base.get('id') or 'map'}-detail"
        sp["extent"] = {"mode": "bbox", "bbox": wgs, "padding_pct": 0}
        sp["crs"] = {**(sp.get("crs") or {}), "display": plan.crs}
        els = sp.setdefault("elements", {})
        for k in DROP_KEYS:
            els.pop(k, None)
        els["scale_bar"] = {**(els.get("scale_bar") or {}), "on": True}
        sp.pop("story", None)
        if cfg.get("layers"):
            ids = [x for x in cfg["layers"] if isinstance(x, str)]
            own = [copy.deepcopy(x) for x in cfg["layers"] if isinstance(x, dict) and x.get("id")]
            known = {ly.get("id") for ly in sp.get("layers") or []}
            missing = [i for i in ids if i not in known]
            if missing:
                raise DetailError(f"elements.detail.layers names {', '.join(missing)}, which the map does not have")
            sp["layers"] = [ly for ly in sp.get("layers") or [] if ly.get("id") in ids] + own
        if cfg.get("opacity") is not None:
            for ly in sp.get("layers") or []:
                if ly.get("role") not in ("context", "labels"):
                    ly["symbol"] = {**(ly.get("symbol") or {}), "opacity": float(cfg["opacity"])}
        if isinstance(cfg.get("basemap"), dict):
            sp["basemap"] = copy.deepcopy(cfg["basemap"])
    sub = mp.prepare(str(plan.spec_path), mode=plan.mode, brand=plan.brand, template=plan.tpl_name, spec_edit=edit, direct_labels=False)
    sub.tpl = sheet.bare_template(plan.tpl, w_mm, h_mm)
    local = lo.Rect(0.0, 0.0, w_mm, h_mm)
    frame = lo.Frame.fit(local, sub.extent, 0.0)
    r = mr.render(sub, frame=frame, elements_off=OFF)
    saved = atlas.SHEET_DROP
    try:
        atlas.SHEET_DROP = TILE_DROP
        inner = atlas._tile_svg(r.svg, [0.0, 0.0, w_mm, h_mm], "gmdet-")
    finally:
        atlas.SHEET_DROP = saved
    t = plan.tokens
    inner += (f'<rect x="0.15" y="0.15" width="{w_mm - 0.3:.2f}" height="{h_mm - 0.3:.2f}" fill="none" stroke="{t["label"]}" stroke-width="0.3"/>')
    title = str(cfg.get("title") or "Detail")
    if cfg.get("title"):                                    # its name, in the upper left of the inset, as the map's title is in the map's
        import svg_draw as sd
        lpt = float((plan.tpl.get("fonts") or {}).get("label") or 7.0)
        inner += sd.text(2.0, 1.6 + lpt * 0.3528, title, lpt, t["label"], weight=600, halo=t["label_halo"], halo_w=max(0.3, lpt * 0.3528 * 0.22))
    fig = {"_svg": inner, "_box": [0.0, 0.0, w_mm, h_mm], "title": title, "position": str(cfg.get("position") or "lower-left"), "w_mm": w_mm, "h_mm": h_mm}
    vis = frame.visible
    den = (r.report.get("scale") or {}).get("ratio")
    return {"figure": fig, "window": tuple(float(v) for v in vis), "findings": [],          # the same layers are judged on the map itself; the detail has no legend, title or sources of its own to check
            "notes": [f"detail inset '{title}': {w_mm:g} x {h_mm:g} mm" + (f" at {den}" if den else "") + ", its window outlined on the map"]}


def window_outline(frame, win: tuple, t: dict) -> str:
    """The detail's window on the map: a thin frame in the label ink over a halo of the ground."""
    import numpy as np
    xs, ys = frame.to_mm(np.array([win[0], win[2]]), np.array([win[3], win[1]]))
    x0, x1 = sorted((float(xs[0]), float(xs[1])))
    y0, y1 = sorted((float(ys[0]), float(ys[1])))
    r = frame.rect
    x0, x1, y0, y1 = max(x0, r.x0), min(x1, r.x1), max(y0, r.y0), min(y1, r.y1)
    if x1 - x0 < 0.6 or y1 - y0 < 0.6:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, x1, y0, y1 = cx - 0.6, cx + 0.6, cy - 0.6, cy + 0.6
    box = f'x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}"'
    return (f'<g class="gm-detail-window"><rect {box} fill="none" stroke="{t["label_halo"]}" stroke-width="1.0" stroke-opacity="0.85"/>'
            f'<rect {box} fill="none" stroke="{t["label"]}" stroke-width="0.35"/></g>')
