#!/usr/bin/env python3
"""map_focus: the study area stands out (0.18.0; stage 2 of the visual styling spec).

    focus:
      mask: auto          # auto (the default under style.look auto): on when a choropleth or grid covers a region; true; false
      area: counties      # the layer whose outline is the study area (default: the union of the choropleth and grid layers)
      lift: true          # a soft shadow under the study area (default true with the mask)

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


def config(spec: dict, look: str) -> dict:
    f = dict((spec or {}).get("focus") or {})
    mask = f.get("mask", "auto")
    if look == "classic" and mask == "auto":
        mask = False
    return {"mask": mask, "area": f.get("area"), "lift": f.get("lift", True) is not False}


def study_area(plan, cfg: dict):
    """The study area in display coordinates, or None: the named layer's outline, else the union of the choropleth and grid layers (auto)."""
    import shapely
    if cfg["mask"] is False:
        return None
    if cfg["area"]:
        ls = [l for l in plan.layers if l.id == cfg["area"] and l.kind == "polygon"]
    else:
        ls = [l for l in plan.layers if l.role in FOCUS_ROLES and l.kind == "polygon"] if cfg["mask"] in ("auto", True) else []
    geoms = [g for l in ls for g in l.gdf.geometry.values if g is not None and not g.is_empty]
    if not geoms:
        return None
    try:
        u = shapely.union_all(shapely.make_valid(np.asarray(geoms, dtype=object)))
    except Exception:  # noqa: BLE001 - a union GEOS refuses: no focus rather than a wrong one
        return None
    return None if u.is_empty else u


def to_sheet_geom(area, frame):
    """The study area in sheet millimetres, clipped to the frame and simplified to SIMPLIFY_MM."""
    import shapely
    from shapely import affinity
    g = shapely.clip_by_rect(area, *frame.visible)
    if g.is_empty:
        return None
    # display -> sheet: x' = ox + (x - cx) * s ; y' = oy - (y - cy) * s
    s = frame.scale
    ox, oy = (frame.inner.x0 + frame.inner.x1) / 2, (frame.inner.y0 + frame.inner.y1) / 2
    g = affinity.affine_transform(g, [s, 0, 0, -s, ox - frame.cx * s, oy + frame.cy * s])
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
