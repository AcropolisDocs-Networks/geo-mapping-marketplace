#!/usr/bin/env python3
"""map_focus: the study area stands out (0.18.0; stage 2 of the visual styling spec).

    focus:
      mask: auto          # auto (the default under style.look auto): on when a choropleth or grid covers a region; true; false
      area: counties      # the layer whose outline is the study area (default: the union of the choropleth and grid layers)
                          # 2.3.0: or a point or line layer: its features (the highlighted ones, when it has a highlight) buffered on the
                          # sheet by reach_mm, so a route, a corridor or a set of sites is the study area
      reach_mm: 6         # 2.3.0: how far around a point or line subject the study area reaches, in sheet millimetres
      lift: true          # a soft shadow under the study area (default: on for a region, off for a point or line subject)

With `mask: true` and no choropleth, grid or named area, the subject is the map's primary point or line layers (2.3.0).

What it draws, all safe for the PDF route (no SVG filter, mask or blend mode; WeasyPrint ignores or drops those):

  mask    one even-odd path, the map frame with the study area cut out of it, filled with the page colour at MASK_OPACITY above the basemap
          and the context and below the data: the neighbours, their roads and their land fade back, the study area keeps full strength.
  lift    a soft shadow of the study area, offset down and to the right, in the brand's label colour at LIFT_OPACITY: LIFT_STEPS grown
          copies of the outline stacked so the edge fades over LIFT_REACH_MM (vector, so a PDF stays vector), above the mask, under the data.
  names   place, water and admin names outside the study area are drawn in the brand's darkest neutral that still reads at 3:1 on the land.

Pure geometry; nothing reads the network.
"""
from __future__ import annotations

import numpy as np

FOCUS_ROLES = ("choropleth", "grid")           # a classed fill that covers a region (a hexbin covers where the samples are, not a region)
MASK_OPACITY = 0.6
LIFT_SIGMA_MM = 1.2
LIFT_OFFSET_MM = (0.6, 0.8)
LIFT_OPACITY = 0.18
LIFT_REACH_MM = 2 * LIFT_SIGMA_MM            # how far the shadow fades out past the outline (about two sigma of the blur it stands for)
LIFT_STEPS = 8
SIMPLIFY_MM = 0.15
REACH_MM = 6.0                                # 2.3.0: the default reach round a point or line subject


def config(spec: dict, look: str) -> dict:
    f = dict((spec or {}).get("focus") or {})
    mask = f.get("mask", "auto")
    if look == "classic" and mask == "auto":
        mask = False
    return {"mask": mask, "area": f.get("area"), "lift": f.get("lift"), "reach_mm": float(f.get("reach_mm") or REACH_MM)}


def lift_on(cfg: dict, area) -> bool:
    """The shadow: as asked, else on under a region and off round a point or line subject (a shadow under a corridor reads as a smudge)."""
    if cfg.get("lift") is not None:
        return cfg["lift"] is not False
    return area is not None and area.geom_type in ("Polygon", "MultiPolygon")


def _subject_layers(plan, cfg: dict) -> list:
    if cfg["area"]:
        return [l for l in plan.layers if l.id == cfg["area"] and l.kind in ("polygon", "point", "line")]
    if cfg["mask"] not in ("auto", True):
        return []
    ls = [l for l in plan.layers if l.role in FOCUS_ROLES and l.kind == "polygon"]
    if ls or cfg["mask"] is not True:
        return ls
    # 2.3.0: asked for (mask: true) on a map without a region: the primary point and line layers are the subject
    data = [l for l in plan.layers if l.kind in ("point", "line") and l.role not in ("context", "contour") and not getattr(l, "legend_only", False)]
    prim = [l for l in data if getattr(l, "emphasis", "primary") == "primary"]
    return prim or data


def study_area(plan, cfg: dict):
    """The study area in display coordinates, or None: the named layer, else the union of the choropleth and grid layers (auto), else (2.3.0,
    mask: true) the primary point and line layers. A point or line subject is returned as its geometry; to_sheet_geom gives it its reach."""
    import shapely
    if cfg["mask"] is False:
        return None
    ls = _subject_layers(plan, cfg)
    geoms = []
    for l in ls:
        vals = l.gdf.geometry.values
        hl = getattr(l, "highlight", None)
        if hl is not None and len(hl) == len(vals) and np.asarray(hl, dtype=bool).any():
            vals = vals[np.asarray(hl, dtype=bool)]           # the highlighted features are what the map is about
        geoms += [g for g in vals if g is not None and not g.is_empty]
    if not geoms:
        return None
    try:
        arr = np.asarray(geoms, dtype=object)
        if all(g.geom_type in ("Polygon", "MultiPolygon") for g in geoms):
            arr = shapely.make_valid(arr)
        u = shapely.union_all(arr)
    except Exception:  # noqa: BLE001 - a union GEOS refuses: no focus rather than a wrong one
        return None
    return None if u.is_empty else u


def to_sheet_geom(area, frame, reach_mm: float = REACH_MM):
    """The study area in sheet millimetres, clipped to the frame and simplified to SIMPLIFY_MM. A point or line subject (2.3.0) is buffered
    by reach_mm on the sheet, its corners rounded, so the area is the same size on the page at any scale."""
    import shapely
    from shapely import affinity
    region = area.geom_type in ("Polygon", "MultiPolygon")
    s = frame.scale
    if region:
        g = shapely.clip_by_rect(area, *frame.visible)
    else:
        pad = reach_mm / s if s else 0.0
        x0, y0, x1, y1 = frame.visible
        g = shapely.clip_by_rect(area, x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    if g.is_empty:
        return None
    # display -> sheet: x' = ox + (x - cx) * s ; y' = oy - (y - cy) * s
    ox, oy = (frame.inner.x0 + frame.inner.x1) / 2, (frame.inner.y0 + frame.inner.y1) / 2
    g = affinity.affine_transform(g, [s, 0, 0, -s, ox - frame.cx * s, oy + frame.cy * s])
    if not region:
        r = max(0.5, float(reach_mm))
        g = g.buffer(r * 1.5, quad_segs=8).buffer(-r * 0.5, quad_segs=8)          # rounded, with the gaps between close features filled
        rect = frame.rect
        g = shapely.clip_by_rect(g, rect.x0, rect.y0, rect.x1, rect.y1)
    g = g.simplify(SIMPLIFY_MM, preserve_topology=True)
    return None if g.is_empty else g


def _rings(g):
    polys = [g] if g.geom_type == "Polygon" else [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon"]
    for p in polys:
        yield p.exterior.coords
        for r in p.interiors:
            yield r.coords


def _d(coords) -> str:
    pts = list(coords)
    return "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in pts) + "Z"


def mask_markup(sheet_area, rect, color: str, opacity: float = MASK_OPACITY) -> str:
    """The frame with the study area cut out (even-odd), in the page colour."""
    frame_d = f"M{rect.x0:.2f} {rect.y0:.2f}H{rect.x1:.2f}V{rect.y1:.2f}H{rect.x0:.2f}Z"
    holes = "".join(_d(r) for r in _rings(sheet_area))
    return (f'<path class="gm-focus-mask" d="{frame_d}{holes}" fill="{color}" fill-opacity="{opacity:g}" fill-rule="evenodd" '
            f'stroke="none" data-focus="mask"/>')


def lift_markup(sheet_area, rect, color: str, opacity: float = LIFT_OPACITY) -> str:
    """A soft shadow of the study area, in vector: LIFT_STEPS copies of the outline, offset down and right and grown by up to LIFT_REACH_MM
    with round corners, each at the opacity that stacks to `opacity` where they all overlap. The edge fades over the reach the way a blur
    would, and a PDF stays all vector (no picture, no filter)."""
    import shapely
    from shapely import affinity
    dx, dy = LIFT_OFFSET_MM
    base = affinity.translate(sheet_area, dx, dy).simplify(SIMPLIFY_MM)
    if base.is_empty:
        return ""
    clip = shapely.box(rect.x0, rect.y0, rect.x1, rect.y1)
    a = 1.0 - (1.0 - opacity) ** (1.0 / LIFT_STEPS)
    paths = []
    for i in range(LIFT_STEPS):
        g = base.buffer(LIFT_REACH_MM * (i + 0.5) / LIFT_STEPS, quad_segs=4).intersection(clip)
        d = "".join(_d(r) for r in _rings(g))
        if d:
            paths.append(f'<path d="{d}" fill="{color}" fill-opacity="{a:.4f}" fill-rule="evenodd" stroke="none"/>')
    return f'<g class="gm-focus-lift" data-focus="lift">{"".join(paths)}</g>' if paths else ""


def fill_lookup(plan, frame, to_sheet):
    """A function (x, y) -> the class fill colour under that sheet point (choropleth, grid and hexbin cells), or None."""
    import shapely
    t = plan.tokens
    polys, cols = [], []
    for l in plan.layers:
        if l.kind != "polygon" or l.cls is None or not l.colors or l.role not in ("choropleth", "grid", "hexbin") or not len(l.gdf):
            continue
        geoms, keep = to_sheet(l.gdf, frame, True)
        assigned = np.asarray(l.cls.assigned)
        if len(assigned) != len(l.gdf):
            continue
        for i in np.where(keep)[0]:
            g = geoms[i]
            if g is None or g.is_empty:
                continue
            ci = int(assigned[i])
            polys.append(g)
            cols.append(t["no_data"] if ci == -1 else l.colors[ci])
    if not polys:
        return lambda x, y: None
    tree = shapely.STRtree(polys)

    def at(x: float, y: float):
        hit = tree.query(shapely.Point(float(x), float(y)), predicate="intersects")
        return cols[int(hit[-1])] if len(hit) else None
    return at


def outside_color(tokens: dict) -> str:
    """The colour of a name outside the study area: the darkest step of the brand's neutral ramp that still reads at 3:1 on the land."""
    import colorlib as cl
    ground = tokens.get("land") or tokens["background"]
    for c in reversed(list(tokens.get("neutral_ramp") or [])):
        if 3.0 <= cl.contrast(c, ground) < cl.contrast(tokens["label"], ground):
            return c
    return tokens["label"]


def inside(sheet_area):
    """A function (x, y) -> bool: is this sheet point inside the study area (True everywhere when there is none)."""
    if sheet_area is None:
        return lambda x, y: True
    from shapely import Point
    from shapely.prepared import prep
    pa = prep(sheet_area.buffer(0.5))
    return lambda x, y: bool(pa.contains(Point(float(x), float(y))))
