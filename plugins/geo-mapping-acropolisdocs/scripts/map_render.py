#!/usr/bin/env python3
"""Render a prepared map (map_plan.Plan) to one SVG in sheet millimetres (spec 6.1, 7.3, 10, 11.7, 11.8).

The SVG is the master. PNG and PDF are made from it by map_export. Every color is a declared brand token, every piece of text is
Inter (embedded as @font-face), and the file carries the brand stamps and the hidden accreditation in <metadata> only.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import brandlib as bl  # noqa: E402
import classify as cf  # noqa: E402
import gmap_common as gc  # noqa: E402
import label_engine as le  # noqa: E402
import layout as lo  # noqa: E402
import legend as lg  # noqa: E402
import legend_draw as ld  # noqa: E402
import map_extras as mex  # noqa: E402
import geo_locale as gl  # noqa: E402
import map_plan as mp  # noqa: E402
import scalebar as sb  # noqa: E402
import svg_draw as sd  # noqa: E402
import textmeasure as tm  # noqa: E402
import text_lint  # noqa: E402
from label_engine import Rect  # noqa: E402

SIMPLIFY_MM = 0.04
MIN_FEATURE_MM = 0.05            # a polygon smaller than this on the sheet is a speck
PT = sd.PT
PROP_SIZE_STEP_MM = 0.05          # 0.24.0: a very large proportional layer is drawn one path per size step of this much
MIN_SYMBOL_MM = 0.8               # the smallest proportional symbol still has to be seen; above this size area stays proportional
OVERLAY_COVER_OK = 0.015          # overlays may sit over up to this share of the data cells; beyond it the map is reduced to make room (fit: shrink)
FIT_COVER_PCT = 5.0              # 1.3.0 (R1-FIT, owner's decision D2): fit auto and overlay let the elements together cover this much of the focus area
# M16 (scale). A layer with more features than LARGE_LAYER is drawn in the compact forms: round point symbols as dots (one path for the
# outlines, one for the fills, per class), and the cells of a choropleth, hexbin or grid, or opaque lines, as one path per class. Smaller
# layers are drawn exactly as before. Above PAGE_SIMPLIFY_ABOVE features, polygons and lines are simplified to one pixel of the output dpi.
LARGE_LAYER = 5_000
PAGE_SIMPLIFY_ABOVE = 20_000
CELL_OUTLINE_FULL_MM = 1.0          # 0.23.0: hexbin and grid cells at least this wide on the sheet keep their full outline...
CELL_OUTLINE_NONE_MM = 0.6          # ...and below this they are drawn without one
MERGE_ROLES = ("choropleth", "hexbin", "grid")       # cells that tile the map: features of one class never overlap, so one path draws the same


def output_dpi(plan) -> float:
    """The dpi the PNG is drawn at (the build's rule): export.dpi, else the template's default, else 300."""
    return float((plan.spec.get("export") or {}).get("dpi") or plan.tpl.get("raster_default_dpi") or 300)


def _page_simplify(plan, l, frame) -> dict | None:
    """The simplification a large polygon or line layer gets on the page (M16), or None below PAGE_SIMPLIFY_ABOVE features."""
    if l.kind not in ("polygon", "line") or len(l.gdf) <= PAGE_SIMPLIFY_ABOVE:
        return None
    dpi = output_dpi(plan)
    return {"tol_mm": 25.4 / dpi, "dpi": dpi, "coverage": l.kind == "polygon" and l.role in MERGE_ROLES}


def _record_page_simplify(l, page: dict, rep: dict) -> None:
    """Say what the page simplification did, in the report, the layer's methods steps and its provenance."""
    tol = max(page["tol_mm"], SIMPLIFY_MM)
    before, after = int(page.get("vertices_before", 0)), int(page.get("vertices_after", 0))
    how = ("as a coverage, so shared edges stay shared" if page.get("method") == "coverage"
           else "feature by feature (Douglas-Peucker" + (", each polygon keeping its topology)" if l.kind == "polygon" else ")"))
    rec = {"features": int(len(l.gdf)), "tolerance_mm": round(tol, 4), "dpi": page["dpi"], "method": page.get("method"), "vertices_before": int(before),
           "vertices_after": int(after)}
    rep.setdefault("page_simplify", {})[l.id] = rec
    if page.get("method") == "none":
        step = (f"simplified for the page: {len(l.gdf):,} features are more than {PAGE_SIMPLIFY_ABOVE:,}, but every vertex of their outlines stands well clear of "
                f"the {tol:.3f} mm tolerance (one pixel at {page['dpi']:g} dpi), so there was nothing to simplify and they were drawn as they are ({after:,} vertices)")
    else:
        step = (f"simplified for the page: {len(l.gdf):,} features are more than {PAGE_SIMPLIFY_ABOVE:,}, so their outlines were simplified to {tol:.3f} mm on the "
                f"sheet (one pixel at {page['dpi']:g} dpi) {how}: {before:,} vertices drawn as {after:,}")
    if step not in l.transformations:
        l.transformations.append(step)
        l.notes.append(step)
    if isinstance(l.provenance, dict):
        l.provenance["page_simplify"] = rec


def _thin_points(mx, my, vis, cls_of, d_mm: float):
    """`thin: true` (M16): of the visible points of one class that fall in the same cell of half a symbol width, the first is drawn. -> mask."""
    cell = max(d_mm / 2.0, 1e-6)
    idx = np.flatnonzero(vis)
    if not len(idx):
        return vis.copy()
    gx = np.floor(mx[idx] / cell).astype(np.int64)
    gy = np.floor(my[idx] / cell).astype(np.int64)
    key = np.stack([np.asarray(cls_of)[idx].astype(np.int64), gx, gy], axis=1)
    _, first = np.unique(key, axis=0, return_index=True)
    out = np.zeros_like(vis)
    out[idx[np.sort(first)]] = True
    return out


@dataclass
class Rendered:
    svg: str
    report: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    filename_stem: str = ""
    title: str = ""
    alt_text: str = ""


def pack_suffix(plan) -> str:
    """1.4.0: outputs of a pack other than `default` carry its id in the stem, so a new pack never overwrites the default build's files."""
    st = getattr(plan, "style", None)
    return f"_{st.id}" if st is not None and not st.is_default else ""


def _role(name: str):
    """1.4.0 (R2-PACK): measure and draw a function's text in a style pack's `name` face (textmeasure.role); a no-op under `default`."""
    def wrap(fn):
        import functools as _ft

        @_ft.wraps(fn)
        def inner(*a, **k):
            with tm.role(name):
                return fn(*a, **k)
        return inner
    return wrap


def today() -> dt.date:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return dt.date.fromisoformat(fixed) if fixed else dt.date.today()


def f(fid, sev, msg, detail=None, layer=None):
    d = {"id": fid, "severity": sev, "message": msg, "detail": detail}
    if layer:
        d["layer"] = layer
    return d


# ---------------------------------------------------------------------------------------------------------------- geometry
def to_sheet(gdf_or_geoms, frame: lo.Frame, polygon: bool, pad_frac: float = 0.02, page: dict | None = None):
    """Clip to the visible area (plus a little), simplify to the sheet resolution, and move into sheet millimetres.

    `page` (a large layer, M16): {"tol_mm": one output pixel, "coverage": bool} simplifies to that tolerance instead of SIMPLIFY_MM. Polygons
    that tile the map (choropleth, hexbin, grid cells) are simplified as a coverage, so a shared edge is simplified once and stays shared;
    other polygons and lines feature by feature (Douglas-Peucker; polygons keep their topology). The method used is written back into `page`."""
    import shapely
    geoms = np.asarray(gdf_or_geoms.geometry.values if hasattr(gdf_or_geoms, "geometry") else gdf_or_geoms, dtype=object)
    x0, y0, x1, y1 = frame.visible
    px, py = (x1 - x0) * pad_frac, (y1 - y0) * pad_frac
    clipped = shapely.clip_by_rect(geoms, x0 - px, y0 - py, x1 + px, y1 + py)
    tol = (max(page["tol_mm"], SIMPLIFY_MM) if page else SIMPLIFY_MM) / frame.scale
    keep = ~shapely.is_empty(clipped)
    simp = np.empty(len(clipped), dtype=object)
    simp[:] = None
    if keep.any():
        done = False
        if page and polygon and page.get("coverage") and hasattr(shapely, "coverage_simplify"):
            if _min_vertex_area(clipped[keep]) >= 4 * tol * tol:
                # no vertex makes a triangle with its neighbours anywhere near the tolerance's area, so the coverage simplifier (which
                # removes the smallest such triangles first, while they are under tolerance squared) would remove nothing; it costs 11 s
                # on 100,000 cells to find that out. The cells get the small per-feature simplification every layer gets.
                page["method"] = "none"
                tol = SIMPLIFY_MM / frame.scale
            else:
                try:
                    simp[keep] = shapely.coverage_simplify(clipped[keep], tol, simplify_boundary=True)
                    page["method"] = "coverage"
                    done = True
                except Exception:  # noqa: BLE001 - not a clean coverage (a collection, an overlap GEOS refuses): simplify feature by feature
                    done = False
        if not done:
            simp[keep] = shapely.simplify(clipped[keep], tol, preserve_topology=True) if polygon else shapely.simplify(clipped[keep], tol)
            if page and page.get("method") != "none":
                page["method"] = "feature"
        if page:
            page["vertices_before"] = int(shapely.get_num_coordinates(clipped[keep]).sum())
            page["vertices_after"] = int(shapely.get_num_coordinates(simp[keep]).sum())
    keep = keep & ~shapely.is_empty(np.where(keep, simp, shapely.Geometry("POINT EMPTY") if False else clipped))
    out = np.empty(len(simp), dtype=object)
    out[:] = None
    idx = np.where(keep)[0]
    if len(idx):
        out[idx] = shapely.transform(simp[idx], lambda c: np.column_stack(frame.to_mm(c[:, 0], c[:, 1])))
    return out, keep


def _min_vertex_area(geoms) -> float:
    """The smallest area of the triangle any polygon ring vertex makes with its two neighbours (the measure the coverage simplifier ranks
    vertices by), over all rings of `geoms`; infinity when there is no ring."""
    import shapely
    parts = shapely.get_parts(np.asarray(geoms, dtype=object))
    parts = parts[shapely.get_type_id(parts) == 3]
    if not len(parts):
        return float("inf")
    rings = shapely.get_rings(parts)
    xy, idx = shapely.get_coordinates(rings, return_index=True)
    if len(xy) < 4:
        return float("inf")
    counts = np.bincount(idx, minlength=len(rings))
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    ends = starts + counts                                   # exclusive; the last coordinate repeats the first
    ok = counts >= 4
    real = np.ones(len(xy), dtype=bool)
    real[(ends - 1)[ok]] = False                             # the closing repeat is not a vertex of its own
    real[np.repeat(~ok, counts)] = False
    prev = np.arange(len(xy)) - 1
    prev[starts[ok]] = (ends - 2)[ok]                        # the first vertex's neighbour before it is the ring's last real vertex
    nxt = np.minimum(np.arange(len(xy)) + 1, len(xy) - 1)
    i = np.flatnonzero(real)
    if not len(i):
        return float("inf")
    a, b, c = xy[prev[i]], xy[i], xy[nxt[i]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    return float(np.abs(cross).min() / 2.0)


def _polygons_overlap(geoms, l, rep) -> bool:
    """1.0.0 (TODO M16): whether any two features of a large `polygons` layer overlap (buffers and wedges do; parcels and footprints usually
    do not). Without overlaps the layer is drawn as one path per class like a choropleth (one path draws the same); with them, one per feature."""
    import shapely
    g = np.asarray(geoms)
    tree = shapely.STRtree(g)
    a, b = tree.query(g, predicate="overlaps")
    over = bool((a != b).any())
    rep.setdefault("notes", []).append(f"layer '{l.id}': {len(g):,} polygons " + ("overlap, so each is drawn as its own path" if over else
                                       "do not overlap, so each class is drawn as one path"))
    return over


NAME_LINE_VERTICES_MAX = 250_000   # 1.0.0: above this many line vertices names are not tested against the lines (the test would cost more than it gives)


def data_line_hit(plan, frame: lo.Frame):
    """A test `r -> bool`: does the label box r cross one of the map's own line layers (not the context pack, not contours)? None when there is
    no such line, or too many vertices to test. Place and state names use it so a town name does not sit on a fibre route or a transit line."""
    import shapely
    geoms = []
    n = 0
    for l in plan.layers:
        if l.kind != "line" or l.role in ("context", "contour") or getattr(l, "gdf", None) is None or not len(l.gdf):
            continue
        g = l.gdf.geometry.to_numpy()
        n += int(shapely.get_num_coordinates(g).sum())
        if n > NAME_LINE_VERTICES_MAX:
            return None
        geoms.extend(shapely.transform(g, lambda xy: np.column_stack(frame.to_mm(xy[:, 0], xy[:, 1]))))
    if not geoms:
        return None
    tree = shapely.STRtree(geoms)

    def hit(r) -> bool:
        return len(tree.query(shapely.box(r.x0, r.y0, r.x1, r.y1), predicate="intersects")) > 0
    return hit


def sheet_points(gdf, frame: lo.Frame):
    xy = np.column_stack([gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()])
    mx, my = frame.to_mm(xy[:, 0], xy[:, 1])
    vis = (mx >= frame.rect.x0) & (mx <= frame.rect.x1) & (my >= frame.rect.y0) & (my <= frame.rect.y1)
    return mx, my, vis


def explode_points(gdf):
    """MultiPoints become one row per point. Returns (gdf, src): `src[i]` is the row of the original frame that exploded row `i` came from, so
    anything assigned per original row (a class, a value) can be carried to every part instead of falling to no-data (P0-12)."""
    src = np.arange(len(gdf))
    if (gdf.geometry.geom_type == "MultiPoint").any():
        g = gdf.copy()
        g["_gm_src"] = src
        g = g.explode(index_parts=False)
        src = g["_gm_src"].to_numpy()
        gdf = g.drop(columns=["_gm_src"])
    return gdf, src


# ---------------------------------------------------------------------------------------------------------------- layers
def draw_layers(plan: mp.Plan, frame: lo.Frame, rep: dict) -> tuple[list[str], list[Rect]]:
    """Returns the SVG groups and the rectangles of point symbols (label obstacles)."""
    t = plan.tokens
    parts: list[str] = []
    obstacles: list[Rect] = []
    import map_focus
    fcfg = map_focus.config(plan.spec, getattr(plan, "look", "classic"))
    farea = map_focus.study_area(plan, fcfg)
    fsheet = map_focus.to_sheet_geom(farea, frame) if farea is not None else None
    plan.focus_sheet = fsheet                          # draw_names fades the names outside it
    focus_done = fsheet is None
    for l in mp.draw_order(plan.layers):
        if not focus_done and l.role not in ("context", "contour") and l.kind != "raster":
            # 0.18.0: the neighbours fade back (the frame with the study area cut out, in the page colour), then the study area's soft shadow
            parts.append(map_focus.mask_markup(fsheet, frame.rect, t["land"]))
            if fcfg["lift"]:
                import colorlib
                shadow = min((t["label"], t["background"]), key=colorlib.luminance)      # a shadow is dark on a dark page too
                parts.append(map_focus.lift_markup(fsheet, frame.rect, shadow))
            rep["focus"] = {"mask": True, "lift": bool(fcfg["lift"]), "area": fcfg["area"] or "auto"}
            focus_done = True
        if getattr(l, "highlight", None) is not None:
            parts.extend(highlight_groups(plan, l, frame, rep, obstacles))      # 0.22.0: the rest dimmed, then the highlighted features
        else:
            parts.append(layer_group(plan, l, frame, rep, obstacles))
    return parts, obstacles


def sketch_fill(plan, l, fill: str, g: list) -> str:
    """1.4.0 (R2-TREAT): under the sketch line character a data layer's fill becomes a jittered hatch in its own colour (its pattern defined
    once per colour, here); the legend's swatches take the same pattern. Anything else keeps the plain fill."""
    lc = (getattr(plan, "treatments", None) or {}).get("line_character") or {}
    if lc.get("style") != "sketch" or lc.get("hatch_fills", True) is False or l.role == "context" or not str(fill).startswith("#"):
        return fill
    import treatments as trm
    reg = plan.__dict__.setdefault("_hatch", {})
    pid = f"gm-sk-{str(fill).lstrip('#').upper()}"
    if fill not in reg:
        reg[fill] = pid
        g.append(f"<defs>{trm.hatch_pattern(pid, fill, trm._seed(plan.spec['id'], fill))}</defs>")
    return f"url(#{pid})"


def layer_group(plan: mp.Plan, l, frame: lo.Frame, rep: dict, obstacles: list) -> str:
    """One data or context layer as its SVG group (the body of draw_layers; 0.22.0 split out so a highlighted layer can draw twice)."""
    t = plan.tokens
    role_attr = ' data-role="context"' if l.role == "context" else (' data-role="contour"' if l.role == "contour" else "")
    gop = f' opacity="{sd.num(l.group_opacity)}"' if getattr(l, "group_opacity", 1.0) < 1 else ""     # 0.17.0: one see-through group
    g = [f'<g id="gm-layer-{sd.esc(l.id)}" data-layer="{sd.esc(l.id)}"{role_attr}{gop}>']
    if l.kind == "raster":
        im = (l.raster or {}).get("image")
        if im:
            g.append(image_markup(frame, im, f"gm-raster gm-raster-{sd.esc(l.id)}", data_layer=l.id))
        rep.setdefault("drawn", {})[l.id] = 1 if im else 0
    elif l.role == "contour":
        g.append(contour_markup(l, frame, rep))
    elif l.kind == "polygon":
        page = _page_simplify(plan, l, frame)
        geoms, keep = to_sheet(l.gdf, frame, True, page=page)
        # one large tiling layer (cells of a choropleth, hexbin or grid) draws each class as one path: the rings oriented so that the
        # non-zero rule keeps holes as holes and a repeated cell filled once (M16)
        merge = int(keep.sum()) > LARGE_LAYER and (l.role in MERGE_ROLES or (l.role == "polygons" and not _polygons_overlap(geoms[keep], l, rep)))
        orient = merge and hasattr(__import__("shapely"), "orient_polygons")
        if orient:
            import shapely
            idx = np.flatnonzero(keep)
            geoms[idx] = shapely.orient_polygons(geoms[idx])
        ds = sd.polygon_paths(geoms)
        if page:
            _record_page_simplify(l, page, rep)
        rule = "nonzero" if orient else "evenodd"
        n = 0

        def emit(sel):
            nonlocal n
            got = [ds[i] for i in sel.tolist() if ds[i]]
            n += len(got)
            if merge and got:
                g.append(f'<path d="{"".join(got)}"/>')
            else:
                g.extend(f'<path d="{d}"/>' for d in got)
        sw_mm = l.stroke_w_mm
        if l.role in ("hexbin", "grid") and keep.any():
            # 0.23.0 (TODO M16): cells under about 1 mm on the sheet get thinner outlines, and none under 0.6 mm, so 100,000 hexagons read
            # as a surface and not as a halftone of outlines
            import shapely
            cells = geoms[np.flatnonzero(keep)[:4000]]
            cw = float(np.sqrt(np.median(shapely.area(cells)))) if len(cells) else 1.0
            if cw < CELL_OUTLINE_FULL_MM:
                sw_mm = 0.0 if cw < CELL_OUTLINE_NONE_MM else l.stroke_w_mm * cw / CELL_OUTLINE_FULL_MM
                rep.setdefault("notes", []).append(f"layer '{l.id}': cells about {cw:.2f} mm across on the sheet, so their outlines are "
                                                   + ("left off" if sw_mm == 0 else f"thinned to {sw_mm:.2f} mm"))
        if l.cls is not None:
            assigned = np.asarray(l.cls.assigned)
            order = list(range(l.cls.k)) + [-1]
            for ci in order:
                sel = np.where(keep & (assigned == ci))[0]
                if not len(sel):
                    continue
                fill = t["no_data"] if ci == -1 else l.colors[ci]
                fo = l.fill_opacity * (l.class_opacity[ci] if getattr(l, "class_opacity", None) and ci >= 0 else 1.0)   # 0.20.0: value by alpha
                op = f' fill-opacity="{sd.num(fo)}"' if fo < 1 else ""
                edge = fill if getattr(l, "edge_in_fill", False) else l.stroke                   # 0.22.0: the edge in the class colour
                edge = "none" if sw_mm == 0 else edge
                g.append(f'<g fill="{sketch_fill(plan, l, fill, g)}"{op} stroke="{edge}" stroke-width="{sd.num(sw_mm)}" stroke-linejoin="round" fill-rule="{rule}">')
                emit(sel)
                g.append("</g>")
        else:
            op = f' fill-opacity="{sd.num(l.fill_opacity)}"' if l.fill_opacity < 1 and l.fill else ""
            if getattr(l, "edge_in_fill", False):             # 0.22.0: the edge in the fill colour, at full strength
                l.stroke = l.fill
            elif l.stroke_opacity < 1:                        # 1.2.0 (Day 8 issue 4): symbol.stroke_opacity is the edge's own strength
                op += f' stroke-opacity="{sd.num(l.stroke_opacity)}"'
            elif (l.fill_opacity < 1 and l.fill and str(l.stroke).upper() == str(l.fill).upper()
                  and not getattr(l, "stroke_named", False)):
                # an outline taken from the fill's own color is see-through too; an edge the spec names keeps its strength (1.2.0, Day 5 issue 2)
                op += f' stroke-opacity="{sd.num(l.fill_opacity)}"'
            dash = (l.symbol or {}).get("dash")
            if dash:                                          # 1.2.0 (Day 9 issue 9): dashed polygon edges
                op += f' stroke-dasharray="{sd.esc(mp.dash_array(dash))}"'
            g.append(f'<g fill="{sketch_fill(plan, l, l.fill, g) if l.fill else "none"}"{op} stroke="{l.stroke or "none"}" stroke-width="{sd.num(l.stroke_w_mm)}" stroke-linejoin="round" fill-rule="{rule}">')
            emit(np.where(keep)[0])
            g.append("</g>")
        rep.setdefault("drawn", {})[l.id] = n
        if merge:
            rep.setdefault("compact", {})[l.id] = "one path per class"
    elif l.kind == "line":
        page = _page_simplify(plan, l, frame)
        src_gdf = _parallel_lines(plan, l, frame, rep) if l.symbol.get("parallel") else l.gdf
        geoms, keep = to_sheet(src_gdf, frame, False, page=page)
        if page:
            _record_page_simplify(l, page, rep)
        dash = l.symbol.get("dash")
        da = f' stroke-dasharray="{sd.esc(dash)}"' if dash else ""
        if l.stroke_opacity < 1:
            da += f' stroke-opacity="{sd.num(l.stroke_opacity)}"'
        # opaque lines of one class draw the same as one path (a see-through line would darken where two cross, so it keeps its own)
        merge = int(keep.sum()) > LARGE_LAYER and l.stroke_opacity >= 1
        ds = sd.line_paths(geoms)
        n = 0
        cas = getattr(l, "casing", None)
        if cas:
            # 0.17.0: a band of the land colour under the lines, one path per class (merged: a single stroke, so crossings never darken it).
            # It starts with data-casing so the graduated-width read-back (MAP-CART-019) does not take its widths for class widths.
            cop = f' stroke-opacity="{sd.num(cas["opacity"])}"' if cas["opacity"] < 1 else ""
            classed = l.cls is not None and len(l.cls.assigned) == len(l.gdf)
            cw_ = l.symbol.get("_widths") or []
            groups_ = ([(ci, np.where(keep & (np.asarray(l.cls.assigned) == ci))[0]) for ci in list(range(l.cls.k)) + [-1]] if classed
                       else [(None, np.where(keep)[0])])
            for ci, sel in groups_:
                got = "".join(ds[i] for i in sel.tolist() if ds[i])
                if not got:
                    continue
                w_ = (cw_[max(ci, 0)] if (cw_ and ci is not None) else l.stroke_w_mm) + cas["extra_mm"]
                g.append(f'<g data-casing="1" fill="none" stroke="{cas["color"]}" stroke-width="{sd.num(w_)}" stroke-linecap="round" '
                         f'stroke-linejoin="round"{cop}><path d="{got}"/></g>')

        def emit_lines(sel):
            nonlocal n
            got = [ds[i] for i in sel.tolist() if ds[i]]
            n += len(got)
            if merge and got:
                g.append(f'<path d="{"".join(got)}"/>')
            else:
                g.extend(f'<path d="{d}"/>' for d in got)
        if l.cls is not None and len(l.cls.assigned) == len(l.gdf):      # a classified line layer is drawn class by class, in the class colors
            assigned = np.asarray(l.cls.assigned)
            for ci in list(range(l.cls.k)) + [-1]:
                sel = np.where(keep & (assigned == ci))[0]
                if not len(sel):
                    continue
                col = t["no_data"] if ci == -1 else l.colors[ci]
                cw = (l.symbol.get("_widths") or [])                         # graduated line widths, one per class (M15)
                w_ = cw[max(ci, 0)] if cw else l.stroke_w_mm
                cls_attr = f' data-gm-class-width="{sd.num(w_)}"' if cw else ""      # 0.24.0: what MAP-CART-019 reads back, whatever the group nesting
                cdash = l.symbol.get("_class_dash") or []                      # 1.4.0 (R2-TREAT category_patterns): one dash per class
                da_ = f' stroke-dasharray="{sd.esc(cdash[ci])}"' if (ci >= 0 and ci < len(cdash) and cdash[ci]) else da
                g.append(f'<g fill="none" stroke="{col}" stroke-width="{sd.num(w_)}" stroke-linecap="round" stroke-linejoin="round"{da_}{cls_attr}>')
                emit_lines(sel)
                g.append("</g>")
        else:
            g.append(f'<g fill="none" stroke="{l.stroke}" stroke-width="{sd.num(l.stroke_w_mm)}" stroke-linecap="round" stroke-linejoin="round"{da}>')
            emit_lines(np.where(keep)[0])
            g.append("</g>")
        import map_look
        if map_look.params(getattr(plan, "look", "classic"))["line_nodes"] and l.role not in ("context", "contour"):
            g.append(line_nodes_markup(l, geoms, keep, t, rep))       # 0.22.0 network look: a dot at each line end
        rep.setdefault("drawn", {})[l.id] = n
        if merge:
            rep.setdefault("compact", {})[l.id] = "one path per class"
    else:
        gdf, src = explode_points(l.gdf)
        mx, my, vis = sheet_points(gdf, frame)
        shape = l.symbol.get("shape", "circle")
        n = int(vis.sum())
        rep.setdefault("drawn", {})[l.id] = n
        if l.role == "proportional":
            vals = gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan)
            ok = vis & np.isfinite(vals) & (vals > 0)
            vmax = float(np.nanmax(vals[ok])) if ok.any() else 1.0
            dmax = float(l.symbol.get("max_size", 14)) * 0.3528
            size = np.zeros(len(vals))
            size[ok] = np.maximum(dmax * np.sqrt(vals[ok] / vmax), MIN_SYMBOL_MM)     # diameter ~ sqrt(value): AREA is proportional (MAP-CART-010)
            size = np.maximum(size, np.where(ok, 0.8, 0))
            l.symbol["_scale"] = {"max_value": vmax, "max_diameter_mm": dmax}
            l._drawn_sizes = size                                   # 1.2.0 (Day 4 issue 1): each label sits beside its own circle
            order = np.argsort(-size)
            g.append(f'<g fill="{l.fill}" fill-opacity="0.78" stroke="{l.stroke}" stroke-width="{sd.num(float(l.symbol.get("_stroke_mm", 0.15)))}">')
            if int(ok.sum()) > LARGE_LAYER:
                # 0.24.0 (TODO M16 scale): a very large proportional layer is drawn one path per size step (0.05 mm), largest first, instead of
                # one path a symbol, so 250,000 circles reach the PDF in a few hundred paths; each step's circles still sit under the smaller ones
                q = np.round(size / PROP_SIZE_STEP_MM) * PROP_SIZE_STEP_MM
                for qs in sorted({float(v) for v in q[ok]}, reverse=True):
                    sel = np.where(ok & (q == qs))[0]
                    g.append('<path d="' + "".join(sd.symbol_paths(shape, mx[sel], my[sel], qs)) + '"/>')
                obstacles.extend(Rect(mx[i] - size[i] / 2, my[i] - size[i] / 2, mx[i] + size[i] / 2, my[i] + size[i] / 2) for i in order[:LARGE_LAYER] if ok[i])
                rep.setdefault("notes", []).append(f"layer '{l.id}': {int(ok.sum()):,} symbols drawn in {len(set(q[ok].tolist()))} size steps of {PROP_SIZE_STEP_MM} mm")
            else:
                for i in order:
                    if ok[i]:
                        g.append(f'<path d="{sd.symbol_path(shape, mx[i], my[i], size[i])}"/>')
                        obstacles.append(Rect(mx[i] - size[i] / 2, my[i] - size[i] / 2, mx[i] + size[i] / 2, my[i] + size[i] / 2))
            g.append("</g>")
            n = int(ok.sum())
            rep["drawn"][l.id] = n
        elif str(shape).lower() == "none":
            rep.setdefault("notes", []).append(f"layer '{l.id}': symbol shape none, so only its labels are drawn")      # 1.2.0 label-only points
        else:
            d_mm = float(l.symbol["_size_mm"]) if l.symbol.get("_size_mm") else float(l.symbol.get("size", 3)) * 0.3528
            sw = float(l.symbol.get("_stroke_mm", 0.12))            # 0.17.0 auto: a wider light outline (the point's casing)
            # the classes were assigned per original row; a MultiPoint's parts all take their row's class
            assigned = (np.asarray(l.cls.assigned)[src] if len(l.cls.assigned) == len(l.gdf) else np.full(len(gdf), -1)) if l.cls is not None else None
            if l.spec.get("thin") is True:                       # asked for, never silent (M16): one point per class in each half-symbol cell
                before = n
                vis = _thin_points(mx, my, vis, assigned if assigned is not None else np.zeros(len(gdf), dtype=int), d_mm)
                n = int(vis.sum())
                rep["drawn"][l.id] = n
                rec = {"points": before, "drawn": n, "not_drawn": before - n, "cell_mm": round(d_mm / 2, 3)}
                rep.setdefault("thinned", {})[l.id] = rec
                step = (f"thinned on request (thin: true): {before - n:,} of {before:,} points were not drawn because they lie within half a symbol width "
                        f"({d_mm / 2:.2f} mm on the sheet) of another point of the same class drawn before them")
                if step not in l.transformations:
                    l.transformations.append(step)
                    l.notes.append(step)
                if isinstance(l.provenance, dict):
                    l.provenance["thinning"] = rec
            # a large layer draws round symbols as dots: the outline colour at the outer diameter, then the fill at the inner one, so each
            # symbol looks as before at two commands a point (M16); squares, triangles, diamonds and crosses keep their outlines
            dots = n > LARGE_LAYER
            stroked = l.stroke not in (None, "", "none")

            rot = _point_rotations(plan, l, gdf) if l.symbol.get("rotate_field") else None

            def symbols(col, shp, sel):
                if rot is not None:                      # direction of travel (0.11.0): a pointer where the heading is known, the shape where not
                    has = sel[np.isfinite(rot[sel])]
                    if len(has):
                        g.append(f'<path fill="{col}" stroke="{l.stroke}" stroke-width="{sd.num(sw)}" d="' + "".join(
                            sd.pointer_path(float(mx[i]), float(my[i]), d_mm, float(rot[i])) for i in has) + '"/>')
                    sel = sel[~np.isfinite(rot[sel])]
                    if not len(sel):
                        return
                if dots and shp == "circle":
                    path = sd.dot_path(mx[sel], my[sel])
                    if stroked:
                        g.append(f'<path fill="none" stroke="{l.stroke}" stroke-width="{sd.num(d_mm + sw)}" stroke-linecap="round" d="{path}"/>')
                    g.append(f'<path fill="none" stroke="{col}" stroke-width="{sd.num(max(d_mm - sw, d_mm / 2) if stroked else d_mm)}" stroke-linecap="round" d="{path}"/>')
                else:
                    g.append(f'<path fill="{col}" stroke="{l.stroke}" stroke-width="{sd.num(sw)}" d="' + "".join(sd.symbol_paths(shp, mx[sel], my[sel], d_mm)) + '"/>')
            if l.cls is not None:
                for ci in list(range(l.cls.k)) + [-1]:
                    sel = np.where(vis & (assigned == ci))[0]
                    if not len(sel):
                        continue
                    col = t["no_data"] if ci == -1 else l.colors[ci]
                    shp = shape if (ci == -1 or not l.class_shapes) else l.class_shapes[ci]
                    symbols(col, shp, sel)
            else:
                idx = np.where(vis)[0]
                if len(idx):
                    symbols(l.fill, shape, idx)
            if dots:
                rep.setdefault("compact", {})[l.id] = "round symbols as dots"
            if n <= 3000:
                for i in np.where(vis)[0]:
                    obstacles.append(Rect(mx[i] - d_mm / 2, my[i] - d_mm / 2, mx[i] + d_mm / 2, my[i] + d_mm / 2))
    g.append(ld.layer_patterns(l, frame, t, to_sheet))                     # hatch over patterned classes (M15); "" for most layers
    g.append("</g>")
    return "".join(g)


def highlight_groups(plan: mp.Plan, l, frame: lo.Frame, rep: dict, obstacles: list) -> list[str]:
    """A layer with `highlight:` (0.22.0): first the rest of it, its colours desaturated (map_look.dim) and the group at
    HIGHLIGHT_REST_OPACITY, then the highlighted features at full colour, symbols and lines HIGHLIGHT_SIZE larger (proportional symbols keep
    their sizes: the size is the value). Emphasis by contrast; one legend, the layer's own."""
    import copy
    import map_look
    m = np.asarray(l.highlight, dtype=bool)
    classed = l.cls is not None and len(l.cls.assigned) == len(l.gdf)

    def part(mask, rest: bool):
        c = copy.copy(l)
        c.gdf = l.gdf[mask]
        c.symbol = dict(l.symbol)
        if classed:
            c.cls = copy.copy(l.cls)
            c.cls.assigned = np.asarray(l.cls.assigned)[mask]
        if rest:
            c.id = f"{l.id}--rest"
            dim = lambda v: map_look.dim(v) if isinstance(v, str) and v.startswith("#") else v      # noqa: E731
            c.colors = [dim(v) for v in (l.colors or [])]
            c.fill, c.stroke = dim(l.fill), (dim(l.stroke) if l.kind == "line" else l.stroke)
            c.group_opacity = map_look.HIGHLIGHT_REST_OPACITY
            c.casing = None
        else:
            k = map_look.HIGHLIGHT_SIZE
            if l.kind == "point" and l.role != "proportional":
                c.symbol["_size_mm"] = (float(l.symbol["_size_mm"]) if l.symbol.get("_size_mm") else float(l.symbol.get("size", 3)) * 0.3528) * k
            elif l.kind == "line":
                c.stroke_w_mm = l.stroke_w_mm * k
                if l.symbol.get("_widths"):
                    c.symbol["_widths"] = [w * k for w in l.symbol["_widths"]]
        return c
    out = []
    if (~m).any():
        out.append(layer_group(plan, part(~m, True), frame, rep, obstacles))
    out.append(layer_group(plan, part(m, False), frame, rep, obstacles))
    drawn = rep.setdefault("drawn", {})
    drawn[l.id] = drawn.get(l.id, 0) + drawn.pop(f"{l.id}--rest", 0)
    rep.setdefault("highlight", {})[l.id] = {"highlighted": int(m.sum()), "rest": int((~m).sum())}
    return out


LINE_NODE_MAX = 3000              # a network look draws its end nodes up to this many; more would be a point layer of their own
LINE_NODE_MIN_MM = 1.2
LINE_NODE_MAX_MM = 3.2
LINE_NODE_SCALE = 1.6             # a node is this many times the width of its line, between the two sizes above


def line_nodes_markup(l, geoms, keep, t: dict, rep: dict) -> str:
    """The network look's nodes (0.22.0): a dot at each end of each line part, in the line's (class) colour with a casing-coloured edge. Where
    ends meet, one dot: the one of the widest line there. LINE_NODE_SCALE times the line's width, kept between LINE_NODE_MIN_MM and
    LINE_NODE_MAX_MM. Not drawn past LINE_NODE_MAX ends (noted in the report)."""
    import shapely
    classed = l.cls is not None and len(l.cls.assigned) == len(l.gdf)
    assigned = np.asarray(l.cls.assigned) if classed else None
    widths = l.symbol.get("_widths") or []
    edge = (getattr(l, "casing", None) or {}).get("color") or t["land"]
    at: dict = {}                                     # (x, y) rounded to 0.1 mm -> (width, colour) of the widest line ending there
    for i in np.where(keep)[0]:
        gm = geoms[i]
        if gm is None or gm.is_empty:
            continue
        ci = int(assigned[i]) if classed else None
        col = (t["no_data"] if ci == -1 else l.colors[ci]) if classed else l.stroke
        w = widths[max(ci, 0)] if (widths and ci is not None) else l.stroke_w_mm
        for pt in getattr(gm, "geoms", [gm]):
            c = shapely.get_coordinates(pt)
            if len(c) < 2:
                continue
            for x, y in (c[0], c[-1]):
                k = (round(float(x), 1), round(float(y), 1))
                if k not in at or w > at[k][0]:
                    at[k] = (w, col)
        if len(at) > LINE_NODE_MAX:
            rep.setdefault("line_nodes", {})[l.id] = f"not drawn: more than {LINE_NODE_MAX:,} line ends"
            return ""
    groups: dict = {}
    for (x, y), (w, col) in at.items():
        d = round(min(max(LINE_NODE_SCALE * w, LINE_NODE_MIN_MM), LINE_NODE_MAX_MM), 3)
        groups.setdefault((col, d), []).append((x, y))
    out = []
    for (col, d), pts in sorted(groups.items(), key=lambda kv: kv[0][1]):      # small dots first, the big ones over them
        xs, ys = zip(*sorted(pts))
        out.append(f'<path fill="{col}" stroke="{edge}" stroke-width="0.25" d="' + "".join(sd.symbol_paths("circle", xs, ys, d)) + '"/>')
    rep.setdefault("line_nodes", {})[l.id] = len(at)
    return f'<g data-nodes="1">{"".join(out)}</g>' if out else ""


def image_markup(frame: lo.Frame, im: dict, cls: str, data_layer: str | None = None) -> str:
    """An embedded picture (a raster layer, a relief, a service picture) placed by its box in display coordinates, the way the tile composite is:
    the file has no outside links. The class marks it as a declared raster for the output lint (MAP-OUT-002 INFO, not WARN)."""
    import base64
    x0, y0, x1, y1 = im["bbox"]
    mx0, my1 = frame.to_mm(x0, y0)
    mx1, my0 = frame.to_mm(x1, y1)
    data = base64.b64encode(im["png"]).decode("ascii")
    op = float(im.get("opacity", 1.0))
    opa = f' opacity="{op:g}"' if op < 1 else ""
    dl = f' data-layer="{sd.esc(data_layer)}"' if data_layer else ""
    return (f'<image class="{cls}" x="{float(mx0):.3f}" y="{float(my0):.3f}" width="{float(mx1 - mx0):.3f}" height="{float(my1 - my0):.3f}" '
            f'preserveAspectRatio="none"{opa}{dl} href="data:image/png;base64,{data}"/>')


def contour_markup(l, frame: lo.Frame, rep: dict) -> str:
    """Contours: thin neutral lines, index lines (every `index_every`-th level) heavier and darker. Their labels are placed after the names."""
    geoms, keep = to_sheet(l.gdf, frame, False)
    idx = l.gdf["index"].to_numpy(dtype=bool) if "index" in l.gdf.columns else np.zeros(len(l.gdf), bool)
    out = []
    n = 0
    op = f' stroke-opacity="{sd.num(l.stroke_opacity)}"' if l.stroke_opacity < 1 else ""
    for heavy, col, w in ((False, l.stroke, l.stroke_w_mm), (True, l.symbol.get("index_stroke") or l.stroke, float(l.symbol.get("index_w_mm") or l.stroke_w_mm * 2))):
        sel = np.where(keep & (idx == heavy))[0]
        if not len(sel):
            continue
        out.append(f'<g fill="none" stroke="{col}" stroke-width="{sd.num(w)}" stroke-linecap="round" stroke-linejoin="round"{op} data-contour="{"index" if heavy else "minor"}">')
        for i in sel:
            d = sd.line_path(geoms[i])
            if d:
                out.append(f'<path d="{d}"/>')
                n += 1
        out.append("</g>")
    rep.setdefault("drawn", {})[l.id] = n
    l.symbol["_sheet"] = (geoms, keep)
    return "".join(out)


CONTOUR_LABEL_MIN_LINE_MM = 25.0      # an index line shorter than this on the sheet is not labelled
CONTOUR_LABEL_STRAIGHT_MM = 0.5       # the line under a label may stray this far from the label's baseline
CONTOUR_LABEL_GAP_MM = 70.0           # a long line gets a label about every this many millimetres


def _rot_rect(cx: float, cy: float, w: float, h: float, ang: float) -> Rect:
    a = math.radians(ang)
    ca, sa = abs(math.cos(a)), abs(math.sin(a))
    hw, hh = (w * ca + h * sa) / 2, (w * sa + h * ca) / 2
    return Rect(cx - hw, cy - hh, cx + hw, cy + hh)


def draw_contour_labels(plan: mp.Plan, frame: lo.Frame, taken: list[Rect], fonts: dict, tpl: dict) -> tuple[str, dict]:
    """Elevation along the index contours: the text follows the line (rotated to its direction, kept upright) where a straight enough stretch has
    room, with a land-coloured halo that breaks the line under it. Labels are the lowest priority on the map: they go where nothing else is."""
    import shapely
    from shapely.ops import substring
    t = plan.tokens
    out: list[str] = []
    stats = {"lines": 0, "placed": 0}
    floor = lg.MIN_LABEL_PT["print" if tpl["kind"] == "print" else "slide"]
    size = max(floor, float(fonts["label"]) - 1.5)
    hmm = size * PT
    ground = t["land"] if plan.basemap else t["background"]
    rect = frame.rect
    taken = list(taken)
    for l in plan.layers:
        if l.role != "contour" or not l.symbol.get("labels", True) or "_sheet" not in l.symbol:
            continue
        geoms, keep = l.symbol["_sheet"]
        idx = l.gdf["index"].to_numpy(dtype=bool) if "index" in l.gdf.columns else np.zeros(len(l.gdf), bool)
        elev = l.gdf["elevation"].to_numpy(dtype=float)
        col = l.symbol.get("index_stroke") or l.stroke
        order = sorted(np.where(keep & idx)[0], key=lambda i: -(geoms[i].length if geoms[i] is not None else 0))
        for i in order:
            g = geoms[i]
            if g is None or g.is_empty:
                continue
            parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for ln in parts:
                L = ln.length
                if L < CONTOUR_LABEL_MIN_LINE_MM:
                    continue
                stats["lines"] += 1
                txt = f"{elev[i]:,.0f}"
                w = tm.width_mm(txt, size, 500, tnum=True) + 1.0
                want = max(1, int(L // CONTOUR_LABEL_GAP_MM))
                fracs = [(k + 0.5) / want for k in range(want)]
                tries = [0.0, 0.08, -0.08, 0.16, -0.16, 0.24, -0.24]
                for f0 in fracs:
                    for dfr in tries:
                        s = (f0 + dfr / want) * L
                        if s - w / 2 < 0 or s + w / 2 > L:
                            continue
                        p0, p1 = ln.interpolate(s - w / 2), ln.interpolate(s + w / 2)
                        mid = ln.interpolate(s)
                        seg = substring(ln, s - w / 2, s + w / 2)
                        chord = shapely.LineString([(p0.x, p0.y), (p1.x, p1.y)])
                        if seg.hausdorff_distance(chord) > CONTOUR_LABEL_STRAIGHT_MM:
                            continue
                        ang = math.degrees(math.atan2(p1.y - p0.y, p1.x - p0.x))
                        if ang > 90:
                            ang -= 180
                        elif ang < -90:
                            ang += 180
                        r = _rot_rect(mid.x, mid.y, w, hmm * 1.1, ang)
                        if r.x0 < rect.x0 + 1 or r.x1 > rect.x1 - 1 or r.y0 < rect.y0 + 1 or r.y1 > rect.y1 - 1:
                            continue
                        if any(r.hits(q) for q in taken):
                            continue
                        out.append(rotated_text(mid.x, mid.y, txt, size, col, ang, ground, max(0.3, hmm * 0.28)))
                        taken.append(r.grown(0.5))
                        stats["placed"] += 1
                        break
    return '<g class="gm-contour-labels">' + "".join(out) + "</g>", stats


def rotated_text(cx: float, cy: float, s: str, size_pt: float, fill: str, ang: float, halo: str, halo_w: float) -> str:
    """Text centred on (cx, cy) along a direction `ang` degrees (sheet y down), with a halo; the baseline sits a third of the cap height below the line."""
    size = size_pt * PT
    tr = f'transform="rotate({sd.num(ang)} {sd.num(cx)} {sd.num(cy)})"'
    attrs = f'class="gm-t gm-num" x="{sd.num(cx)}" y="{sd.num(cy + size * 0.35)}" font-size="{sd.num(size)}" font-weight="500" text-anchor="middle" {tr}'
    body = sd.esc(s)
    return (f'<text {attrs} fill="{halo}" stroke="{halo}" stroke-width="{sd.num(halo_w)}" stroke-linejoin="round">{body}</text>'
            f'<text {attrs} fill="{fill}" data-name="contour">{body}</text>')


def _ramp_color_at(stops: list, pos: float) -> str:
    """The ramp's own stop nearest a position (no colour is made up: the brand lint allows only declared and derived colours)."""
    i = int(round(pos * (len(stops) - 1)))
    return stops[max(0, min(len(stops) - 1, i))]


def draw_tiles(plan: mp.Plan, frame: lo.Frame, b: dict) -> str:
    """The prepared tile composite, placed by its Web Mercator box. It is embedded, so the file has no outside links."""
    import base64
    im = b["image"]
    x0, y0, x1, y1 = im["bbox_3857"]
    mx0, my1 = frame.to_mm(x0, y0)
    mx1, my0 = frame.to_mm(x1, y1)
    raw = Path(im["path"]).read_bytes()
    import map_look
    how = map_look.tile_treatment(plan.spec, getattr(plan, "look", "classic"))
    if how != "none":                                 # 0.20.0: the tiles step back behind the data, in the brand's ground (basemap.treatment)
        raw = map_look.treat_tiles(raw, plan.tokens["land"], how)
    data = base64.b64encode(raw).decode("ascii")
    op = (plan.spec.get("basemap") or {}).get("opacity")
    opa = f' opacity="{float(op):g}"' if op is not None and float(op) < 1 else ""
    return (f'<image class="gm-base gm-base-tiles" x="{float(mx0):.3f}" y="{float(my0):.3f}" width="{float(mx1 - mx0):.3f}" height="{float(my1 - my0):.3f}" '
            f'preserveAspectRatio="none"{opa} href="data:image/png;base64,{data}"/>')


def draw_basemap(plan: mp.Plan, frame: lo.Frame, ground_opacity: float = 1.0) -> str:
    t = plan.tokens
    out = []
    for b in plan.basemap:
        if b["kind"] == "tiles":
            out.append(draw_tiles(plan, frame, b))
            continue
        if b["kind"] == "hillshade":                      # relief: above the land, countries and states; under the lakes and every layer (M12)
            out.append(image_markup(frame, b["image"], "gm-base gm-relief", data_layer=b.get("layer_id")))
            continue
        if b["kind"] == "boundary":                       # 1.10.0: border lines of a vector-tile ground (states thinner than countries)
            geoms, keep = to_sheet(b["gdf"], frame, False)
            sw = 0.16 if b.get("class") == "country" else 0.12
            out.append(f'<g class="gm-base gm-base-boundary" fill="none" stroke="{t["frame"]}" stroke-width="{sw}" stroke-linejoin="round" stroke-linecap="round">')
            out += [f'<path d="{d}"/>' for d in (sd.line_path(geoms[i]) for i in np.where(keep)[0]) if d]
            out.append("</g>")
            continue
        poly = True
        geoms, keep = to_sheet(b["gdf"], frame, poly)
        if b["kind"] in ("lake", "ocean"):
            fill, stroke, sw = t["water"], t["water"], 0.1
        elif b["kind"] == "land":
            fill, stroke, sw = t["land"], "none", 0.0
        elif b["kind"] == "state":
            fill, stroke, sw = t["land"], t["frame"], 0.14
        else:
            fill, stroke, sw = t["land"], t["frame"], 0.16
        gop = f' fill-opacity="{sd.num(ground_opacity)}"' if ground_opacity < 1 and b["kind"] not in ("lake", "ocean") else ""   # 1.4.0: paper shows through
        out.append(f'<g class="gm-base gm-base-{b["kind"]}" fill="{fill}"{gop} stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round" fill-rule="evenodd">')
        for i in np.where(keep)[0]:
            d = sd.polygon_path(geoms[i])
            if d:
                out.append(f'<path d="{d}"/>')
        out.append("</g>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------- graticule
GRAT_STEPS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 30)


def graticule_label_point(kind: str, pts: list):
    """Where a graticule line's label goes among the points where it meets the frame: a meridian is labelled on the bottom edge (the largest
    y in sheet millimetres; the old rule took the smallest and put every longitude on the top edge), a parallel on the left edge (smallest x)."""
    return max(pts, key=lambda q: q.y) if kind == "lon" else min(pts, key=lambda q: q.x)


def graticule(plan: mp.Plan, frame: lo.Frame, spec_el: dict, fonts: dict, obstacles: list[Rect] | None = None) -> tuple[str, list[Rect]]:
    """The graticule lines and their edge labels; a label is not drawn where an overlay (the scale bar, the legend) already sits."""
    from pyproj import Transformer
    import shapely
    t = plan.tokens
    wgs = mp._wgs84_extent(plan)
    span = min(wgs[2] - wgs[0], (wgs[3] - wgs[1]) * 1.4)
    interval = spec_el.get("interval")
    if not interval or interval == "auto":
        want = span / 5.0
        interval = next((s for s in GRAT_STEPS if s >= want), GRAT_STEPS[-1])
    interval = float(interval)
    lon0, lon1 = np.floor(wgs[0] / interval) * interval, np.ceil(wgs[2] / interval) * interval
    lat0, lat1 = np.floor(wgs[1] / interval) * interval, np.ceil(wgs[3] / interval) * interval
    tr = Transformer.from_crs("EPSG:4326", plan.crs, always_xy=True)
    lines = []
    for lon in np.arange(lon0, lon1 + interval / 2, interval):
        lats = np.linspace(max(lat0 - interval, -89.9), min(lat1 + interval, 89.9), 60)
        lines.append(("lon", float(lon), tr.transform(np.full_like(lats, lon), lats)))
    for lat in np.arange(lat0, lat1 + interval / 2, interval):
        lons = np.linspace(lon0 - interval, lon1 + interval, 60)
        lines.append(("lat", float(lat), tr.transform(lons, np.full_like(lons, lat))))
    rect = frame.rect
    box = shapely.box(rect.x0, rect.y0, rect.x1, rect.y1)
    style = spec_el.get("style", "dashed")                     # 0.15.0: dashed (the default), solid or dotted
    dash = {"solid": "", "dotted": ' stroke-dasharray="0.15 0.6" stroke-linecap="round"'}.get(style, ' stroke-dasharray="0.6 0.9"')
    width = {"solid": 0.1, "dotted": 0.2}.get(style, 0.12)
    out = [f'<g class="gm-graticule" data-style="{sd.esc(style)}" fill="none" stroke="{t["frame"]}" stroke-width="{width}"{dash}>']
    labels = []
    pt = fonts["label"]
    for kind, val, (xs, ys) in lines:
        ok = np.isfinite(xs) & np.isfinite(ys)
        if ok.sum() < 2:
            continue
        mx, my = frame.to_mm(xs[ok], ys[ok])
        ln = shapely.LineString(np.column_stack([mx, my]))
        seg = ln.intersection(box)
        if seg.is_empty:
            continue
        d = sd.line_path(seg) if seg.geom_type in ("LineString", "MultiLineString") else ""
        if d:
            out.append(f'<path d="{d}"/>')
        edge = ln.intersection(box.boundary)
        pts = [edge] if edge.geom_type == "Point" else list(getattr(edge, "geoms", []))
        pts = [p for p in pts if p.geom_type == "Point"]
        if pts:
            p = graticule_label_point(kind, pts)
            hemi = ("E" if val >= 0 else "W") if kind == "lon" else ("N" if val >= 0 else "S")
            txt = f"{abs(val):g}°{hemi}"
            labels.append((kind, p.x, p.y, txt))
    out.append("</g>")
    if spec_el.get("labels", True) is False:                 # 0.15.0: the lines without their edge labels
        return "".join(out), []
    taken: list[Rect] = [o.grown(0.6) for o in (obstacles or [])]
    lab_rects: list[Rect] = []
    lab = []
    for kind, x, y, txt in labels:
        w = tm.width_mm(txt, pt, 400, tnum=True)
        h = pt * PT
        if kind == "lon":                      # on the bottom edge, label sits just inside
            if abs(y - rect.y1) > 1.0 and abs(y - rect.y0) > 1.0:
                continue
            at_bottom = abs(y - rect.y1) <= 1.0
            r = Rect(x - w / 2, (rect.y1 - h - 1.2) if at_bottom else rect.y0 + 1.0, x + w / 2, (rect.y1 - 1.0) if at_bottom else rect.y0 + h + 1.2)
            if r.x0 < rect.x0 + 1 or r.x1 > rect.x1 - 1:
                continue
            tx, ty, anchor = x, r.y1 - 0.4, "middle"
        else:
            if abs(x - rect.x0) > 1.0 and abs(x - rect.x1) > 1.0:
                continue
            at_left = abs(x - rect.x0) <= 1.0
            r = Rect((rect.x0 + 1.2) if at_left else rect.x1 - w - 1.2, y - h - 0.3, (rect.x0 + 1.2 + w) if at_left else rect.x1 - 1.2, y - 0.3 + 0.2)
            if r.y0 < rect.y0 + 1 or r.y1 > rect.y1 - 1:
                continue
            tx, ty, anchor = (r.x0, y - 0.6, "start") if at_left else (r.x1, y - 0.6, "end")
        if any(r.hits(q) for q in taken):
            continue
        taken.append(r)
        lab_rects.append(r)
        lab.append(sd.text(tx, ty, txt, pt, t["label"], anchor=anchor, num_cls=True, halo=t["label_halo"], halo_w=0.5))
    return "".join(out) + "".join(lab), lab_rects


# ---------------------------------------------------------------------------------------------------------------- elements
def draw_legend(blocks: list[dict], fit: dict, x: float, y: float, t: dict, header: str | None = None, frame: "bool | str" = True) -> str:
    """The legend. `header` is the legend's own title (elements.legend.title, 0.15.0); `frame: false` drops the panel behind it, so the
    legend sits on the map with its labels haloed by the panel colour's absence (the layout still keeps its room)."""
    size = fit["size"]
    lpt, tpt = fit["label_pt"], fit["label_pt"] + 1.0
    if isinstance(frame, str) and frame in ("ruled", "cartouche"):      # 1.4.0 (R2-ORN): elements.legend.frame
        import ornaments
        panel = ornaments.legend_frame(frame, x, y, size["width"], size["height"], t)
    else:
        panel = sd.rect(x, y, size["width"], size["height"], t["legend_bg"], t["legend_frame"], 0.2) if frame else ""
    out = [f'<g class="gm-legend" role="group" aria-label="Legend"{"" if frame else " data-frame=" + chr(34) + "none" + chr(34)}>', panel]
    cx = x + lg.PAD_MM
    cy0 = y + lg.PAD_MM
    if header:
        out.append(sd.text(cx, cy0 + tpt * PT * 0.85, header, tpt + 0.5, t["label"], weight=700))
        cy0 += tm.line_height_mm(tpt + 0.5) + 1.5
    for ci, idxs in enumerate(size["columns"]):
        cy = cy0
        for bi in idxs:
            b = blocks[bi]
            if b.get("title"):
                tl = lg.title_lines(b, lpt, tpt, bool(fit.get("wrap") or size.get("wrap")))      # 1.2.0: wrapped long titles
                for li, line in enumerate(tl):
                    out.append(sd.text(cx, cy + tpt * PT * 0.85 + li * tm.line_height_mm(tpt), line, tpt, t["label"], weight=600))
                cy += tm.line_height_mm(tpt) * len(tl) + 1.0
            if b["kind"] in ("ramp", "image"):                # a continuous bar, or the service's own legend picture (M12)
                cy = draw_legend_special(out, b, cx, cy, lpt, t)
                cy += lg.BLOCK_GAP_MM
                continue
            if b["kind"] == "bivariate" or (b["kind"] == "proportional" and b.get("nested")):     # M15 keys (legend_draw)
                cy = (ld.bivariate_key if b["kind"] == "bivariate" else ld.nested_key)(out, b, cx, cy, lpt, t)
                cy += lg.BLOCK_GAP_MM
                continue
            row_h = max(lg.SWATCH_H_MM, tm.line_height_mm(lpt))
            for it in b["items"]:
                ih = max(row_h, it.get("size_mm", 0) + 0.8)
                mid = cy + ih / 2
                icon_w = max(lg.SWATCH_W_MM, it.get("size_mm", 0) + 1.5)
                if b["kind"] in ("classes", "categories", "polygons"):
                    op = float(it.get("opacity", 1.0))
                    opa = f'fill-opacity="{sd.num(op)}"' if op < 1 else ""
                    sw = b.get("swatch")
                    if sw in ("hexagon", "square"):          # a hexbin or grid key is drawn in the cell's own shape (M11)
                        d = swatch_path(sw, cx + icon_w / 2, mid, lg.SWATCH_H_MM)
                        out.append(f'<path d="{d}" fill="{it["fill"]}" stroke="{it["stroke"]}" stroke-width="0.15" stroke-linejoin="round"{" " + opa if opa else ""}/>')
                    elif it.get("outline") or it.get("dash") or it.get("stroke_opacity") is not None:
                        # 1.2.0 (Day 9 issues 7, 9): an outline layer's swatch is its edge, as drawn: no land box, its width, dash and strength
                        sw_w = min(max(float(it.get("width_mm") or 0.3), 0.15), 0.8)
                        extra = (f' stroke-dasharray="{sd.esc(it["dash"])}"' if it.get("dash") else "") + \
                                (f' stroke-opacity="{sd.num(it["stroke_opacity"])}"' if it.get("stroke_opacity") is not None else "")
                        fill_ = it["fill"] if not it.get("outline") else "none"
                        if fill_ != "none" and op < 1:
                            out.append(sd.rect(cx, mid - lg.SWATCH_H_MM / 2, lg.SWATCH_W_MM, lg.SWATCH_H_MM, t["land"]))
                        x0, y0 = cx + sw_w / 2, mid - lg.SWATCH_H_MM / 2 + sw_w / 2
                        out.append(f'<rect x="{sd.num(x0)}" y="{sd.num(y0)}" width="{sd.num(lg.SWATCH_W_MM - sw_w)}" height="{sd.num(lg.SWATCH_H_MM - sw_w)}" '
                                   f'fill="{fill_}"{" " + opa if opa and fill_ != "none" else ""} stroke="{it["stroke"]}" stroke-width="{sd.num(sw_w)}"{extra}/>')
                    else:
                        if op < 1:                          # the swatch shows the fill as the map draws it: see-through, over the land color
                            out.append(sd.rect(cx, mid - lg.SWATCH_H_MM / 2, lg.SWATCH_W_MM, lg.SWATCH_H_MM, t["land"]))
                        out.append(sd.rect(cx, mid - lg.SWATCH_H_MM / 2, lg.SWATCH_W_MM, lg.SWATCH_H_MM, it["fill"], it["stroke"], 0.15, opa))
                        if it.get("pattern"):                       # the class's hatch over its colour (M15)
                            out.append(ld.swatch_pattern(it, cx, mid - lg.SWATCH_H_MM / 2, lg.SWATCH_W_MM, lg.SWATCH_H_MM, t, b["layer"]))
                elif b["kind"] == "swatches":                 # a MapServer legend: the server's own swatch picture, the label in Inter (M12)
                    s = float(it.get("size_mm", lg.SWATCH_IMG_MM))
                    op = float(it.get("opacity", 1.0))
                    opa = f' opacity="{op:g}"' if op < 1 else ""
                    out.append(f'<image class="gm-legend-img" x="{sd.num(cx + (icon_w - s) / 2)}" y="{sd.num(mid - s / 2)}" width="{sd.num(s)}" height="{sd.num(s)}"{opa} '
                               f'preserveAspectRatio="xMidYMid meet" href="{it["href"]}"/>')
                elif b["kind"] == "lines":
                    dash = f' stroke-dasharray="{sd.esc(it["dash"])}"' if it.get("dash") else ""
                    out.append(f'<path d="M{sd.num(cx)} {sd.num(mid)}h{sd.num(lg.SWATCH_W_MM)}" stroke="{it["stroke"]}" stroke-width="{sd.num(it["width_mm"])}" fill="none" stroke-linecap="round"{dash}/>')
                else:
                    out.append(f'<path d="{sd.symbol_path(it["shape"], cx + icon_w / 2, mid, it["size_mm"])}" fill="{it["fill"]}" stroke="{it["stroke"]}" stroke-width="0.12"/>')
                out.append(sd.text(cx + icon_w + 2.0, mid + lpt * PT * 0.35, it["label"], lpt, t["label"], num_cls=True))
                cy += ih + lg.ROW_GAP_MM
            if b.get("note"):                                # the block's note (a hexbin's resolution and edge length), in the room block_size reserves
                npt = lpt - 1
                out.append(sd.text(cx, cy + npt * PT * 0.85, b["note"], npt, t["label"], num_cls=True))
                cy += tm.line_height_mm(npt)
            cy += lg.gap_after(blocks, bi) - lg.ROW_GAP_MM
        cx += size["col_widths"][ci] + lg.COL_GAP_MM
        cx = cx
    out.append("</g>")
    return "".join(out)


def draw_legend_special(out: list, b: dict, cx: float, cy: float, lpt: float, t: dict) -> float:
    """A ramp block (one gradient through the brand ramp's tokens, ticks and labels under it) or an image block (the picture, scaled). -> the y below it."""
    if b["kind"] == "ramp":
        items = b["items"]
        first = tm.width_mm(items[0]["label"], lpt, 400, tnum=True) if items else 0.0
        x0 = cx + first / 2
        w, h = lg.RAMP_W_MM, lg.RAMP_H_MM
        gid = f"gm-ramp-{re.sub(r'[^A-Za-z0-9_-]', '-', str(b['layer']))}"
        stops = b["stops"]
        full_at = b.get("alpha_full_at")               # 0.20.0: value by alpha; the bar fades in the same way the surface does
        stop_list = [(i / (len(stops) - 1), c) for i, c in enumerate(stops)]
        if full_at:
            stop_list = sorted(stop_list + [(float(full_at), _ramp_color_at(stops, float(full_at)))])
        out.append(f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="0">'
                   + "".join(f'<stop offset="{sd.num(o)}" stop-color="{c}"' + (f' stop-opacity="{sd.num(min(1.0, o / float(full_at)))}"' if full_at and o < full_at else "")
                             + "/>" for o, c in stop_list) + "</linearGradient></defs>")
        op = float(b.get("opacity", 1.0))
        if op < 1 or full_at:                                       # the bar shows the colours as the map draws them: see-through, over the land
            out.append(sd.rect(x0, cy, w, h, t["land"]))
        opa = f'fill-opacity="{sd.num(op)}"' if op < 1 else ""
        out.append(sd.rect(x0, cy, w, h, f"url(#{gid})", t["legend_frame"], 0.15, opa))
        for it in items:
            x = x0 + w * float(it["pos"])
            out.append(f'<path d="M{sd.num(x)} {sd.num(cy + h)}v0.8" stroke="{t["label"]}" stroke-width="0.15" fill="none"/>')
            out.append(sd.text(x, cy + h + 0.8 + lpt * PT * 0.95, it["label"], lpt, t["label"], anchor="middle", num_cls=True))
        cy += h + 0.8 + tm.line_height_mm(lpt)
    else:
        for it in b["items"]:
            out.append(f'<image class="gm-legend-img" x="{sd.num(cx)}" y="{sd.num(cy)}" width="{sd.num(it["w_mm"])}" height="{sd.num(it["h_mm"])}" '
                       f'preserveAspectRatio="xMinYMin meet" href="{it["href"]}"/>')
            cy += float(it["h_mm"]) + lg.ROW_GAP_MM
    if b.get("note"):
        npt = lpt - 1
        out.append(sd.text(cx, cy + npt * PT * 0.85, b["note"], npt, t["label"], num_cls=True))
        cy += tm.line_height_mm(npt)
    return cy


def swatch_path(shape: str, cx: float, cy: float, h: float) -> str:
    """A legend key in a cell's shape, `h` mm tall and centred on (cx, cy): a flat-topped hexagon (as H3 draws them, near enough) or a square."""
    if shape == "square":
        s = h / 2
        return f"M{sd.num(cx - s)} {sd.num(cy - s)}h{sd.num(2 * s)}v{sd.num(2 * s)}h{sd.num(-2 * s)}Z"
    r = h / 2 / 0.8660254                        # circumradius for a flat-to-flat height of h
    pts = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in (0, 60, 120, 180, 240, 300)]
    return "M" + "L".join(f"{sd.num(x)} {sd.num(y)}" for x, y in pts) + "Z"


SCALE_BAR_H_MM = 1.5


def scale_bar_visible_h(pt: float, show_ratio: bool) -> float:
    """Height of what the scale bar draws: from the top of the bar to the baseline of its labels (or of the ratio line under them)."""
    return SCALE_BAR_H_MM + pt * PT * (2.75 if show_ratio else 1.25)


SCALE_STYLES = ("alternating", "line", "ticks", "ladder")      # 0.15.0: elements.scale_bar.style (1.4.0: ladder)


def draw_scale_bar(info: dict, x: float, y: float, t: dict, pt: float, show_ratio: bool, style: str = "alternating") -> tuple[str, float, float]:
    """The scale bar. `alternating` (the default): filled and open segments. `line`: one line with an end tick at each end. `ticks`: one line
    with a tick at every segment boundary. All three keep the same height and labels, so the layout does not change with the style."""
    bar = info["bar"]
    L = bar["length_mm"]
    bh = SCALE_BAR_H_MM
    end_label = bar["ticks"][-1]["label"]
    w = L + tm.width_mm(end_label, pt, 400, tnum=True) / 2 + 1.0
    h = bh + pt * PT * 1.6 + (pt * PT * 1.4 if show_ratio else 0) + 1.0
    out = [f'<g class="gm-scalebar" data-style="{sd.esc(style)}" role="img" aria-label="Scale bar">']
    if style in ("line", "ticks"):
        lw = 0.3
        out.append(f'<path d="M{sd.num(x)} {sd.num(y + bh)}h{sd.num(L)}" stroke="{t["label"]}" stroke-width="{lw}" fill="none" stroke-linecap="square"/>')
        stops = [0.0, L] if style == "line" else sorted({0.0, L, *[s["start_mm"] for s in bar["segments"]], *[s["end_mm"] for s in bar["segments"]]})
        for s_ in stops:
            tall = s_ in (0.0, L)
            out.append(f'<path d="M{sd.num(x + s_)} {sd.num(y + bh)}v{sd.num(-(bh if tall else bh * 0.6))}" stroke="{t["label"]}" stroke-width="{lw}" fill="none"/>')
    elif style == "ladder":                        # 1.4.0: two rows of segments, offset by one (the engraved double bar)
        for i, s in enumerate(bar["segments"]):
            for row in (0, 1):
                fill = t["label"] if (i + row) % 2 == 0 else t["background"]
                out.append(sd.rect(x + s["start_mm"], y + row * bh / 2, s["end_mm"] - s["start_mm"], bh / 2, fill, t["label"], 0.15))
    else:
        for i, s in enumerate(bar["segments"]):
            fill = t["label"] if i % 2 == 0 else t["background"]
            out.append(sd.rect(x + s["start_mm"], y, s["end_mm"] - s["start_mm"], bh, fill, t["label"], 0.18))
    for tk in sb.thin_ticks(bar["ticks"], lambda s_: tm.width_mm(s_, pt, 400, tnum=True)):     # labels that would touch are dropped (M15)
        anchor = "middle"
        out.append(sd.text(x + tk["at_mm"], y + bh + pt * PT * 1.25, tk["label"], pt, t["label"], anchor=anchor, num_cls=True,
                           halo=t["label_halo"], halo_w=0.45))
    if show_ratio:
        out.append(sd.text(x, y + bh + pt * PT * 2.75, info["ratio"], pt - 0.5, t["label"], num_cls=True, halo=t["label_halo"], halo_w=0.45))
    out.append("</g>")
    return "".join(out), w, h


def clear_space_intrusions(placed: dict, logo_box: dict) -> tuple[list[str], float]:
    """(the placed items that reach into the logo's clear space, the clear space in mm). The logo's own rect carries its pad round the mark."""
    lr, pad = placed["logo"]["rect"], logo_box["pad"]
    need = float(logo_box["clear"]) * float(logo_box["h"])
    zx0, zy0, zx1, zy1 = lr.x0 + pad - need, lr.y0 + pad - need, lr.x1 - pad + need, lr.y1 - pad + need
    inside = [k for k, v in placed.items() if k != "logo" and v["rect"].x0 < zx1 - 0.01 and zx0 + 0.01 < v["rect"].x1
              and v["rect"].y0 < zy1 - 0.01 and zy0 + 0.01 < v["rect"].y1]
    return inside, need


def logo_clear_space(plan) -> float | None:
    """0.15.0: the clear space round the logo as a share of its height, from the pack (map-tokens.yaml `logo.clear_space`, else brand.json
    `logo.clear_space`), or None when the brand declares none (then nothing is measured)."""
    for src in ((plan.pack.get("tokens") or {}).get("logo") or {}, (plan.pack.get("brand") or {}).get("logo") or {}):
        v = src.get("clear_space") if isinstance(src, dict) else None
        try:
            if v is not None and 0 < float(v) <= 2:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def logo_markup(plan: mp.Plan, ground_hex: str, x: float, y: float, h: float) -> tuple[str, float]:
    import re
    logo = plan.pack["brand"]["logo"]
    if ((plan.spec.get("elements") or {}).get("logo") or {}).get("variant") == "secondary" and isinstance(logo.get("secondary"), dict):
        logo = logo["secondary"]                                   # 0.15.0: the pack's secondary lockup (a wordmark), when it carries one
    r = ground_hex.lstrip("#")
    lum = (0.2126 * int(r[0:2], 16) + 0.7152 * int(r[2:4], 16) + 0.0722 * int(r[4:6], 16)) / 255
    key = "dark" if lum < 0.4 else "light"
    svg = logo.get(key) or logo.get("light") or logo.get("dark")
    if not svg:
        return "", 0.0
    m = re.search(r'viewBox="([\d.\-\s]+)"', svg)
    vb = [float(v) for v in m.group(1).split()] if m else [0, 0, 100, 100]
    w = h * vb[2] / vb[3]
    inner = re.sub(r"^<\?xml[^>]*>\s*", "", svg.strip())
    inner = re.sub(r'<svg\b([^>]*)>', lambda mm: f'<svg x="{sd.num(x)}" y="{sd.num(y)}" width="{sd.num(w)}" height="{sd.num(h)}" viewBox="{m.group(1) if m else "0 0 100 100"}" preserveAspectRatio="xMidYMid meet" class="gm-logo" role="img" aria-label="{sd.esc(plan.brand_name)} logo" xmlns="http://www.w3.org/2000/svg">', inner, count=1)
    return inner, w


def wrap_lines(text: str, w: float, pt: float, weight: int = 400, max_lines: int = 2) -> tuple[list[str], bool]:
    lines = tm.wrap(text, w, pt, weight)
    if len(lines) <= max_lines:
        return lines, False
    keep = lines[:max_lines]
    keep[-1] = tm.fit(keep[-1] + " " + " ".join(lines[max_lines:]), w, pt, weight)
    return keep, True


# ---------------------------------------------------------------------------------------------------------------- main
def _render_overlays(els, findings, fonts, map_rect, on, plan, rep, t, tpl):
    """render, stage 2 (1.9.0): the legend, the confidentiality banner, the locator and the sizes of every overlay, none of which depend on the frame. Split from render() unchanged."""
    # ---- legend (does not depend on the frame)
    blocks = [l.legend_block for l in plan.layers if l.legend_block]
    classified = {l.id: l.cls for l in plan.layers if l.cls is not None}
    for l in plan.layers:
        if l.role == "proportional" and l.legend_block is None and (l.spec.get("legend") or {}).get("include", True) is not False:
            vals = l.gdf[l.value_field].to_numpy(dtype="float64", na_value=np.nan)
            vals = vals[np.isfinite(vals) & (vals > 0)]
            if len(vals):
                dmax = float(l.symbol.get("max_size", 14)) * 0.3528
                vmax = float(vals.max())
                fmt = cf.number_format((l.spec.get("legend") or {}).get("format")).fmt
                l.legend_block = lg.proportional_block(l.spec | {"title": l.title}, t, list(vals), lambda v: max(MIN_SYMBOL_MM / 2, dmax / 2 * (v / vmax) ** 0.5), lambda v: fmt(v, 0 if float(v).is_integer() else 2), l.fill)
                blocks.append(l.legend_block)
    findings += lg.check_blocks(blocks, [l.spec | {"role": l.role} for l in plan.layers], classified)
    legend_on = on("legend")
    legend_fit = None
    if blocks and legend_on:
        lmax = tpl["legend_max"]
        cols = int((els.get("legend") or {}).get("columns", 1))
        legend_title = (els.get("legend") or {}).get("title") or None        # 0.15.0: a title for the legend as a whole
        legend_fit = lg.fit_legend(blocks, lmax["w"], min(lmax["h"], map_rect.h * 0.85), columns=cols, template_kind=tpl["kind"],
                                   header=legend_title, start_pt=float((tpl.get("fonts") or {}).get("legend") or 0) or None)
        if not legend_fit["fits"]:
            findings.append(f("MAP-SPEC-005", "BLOCK", legend_fit["notes"][0] + "; use a larger template or fewer layers in the legend"))
            # 1.2.0 (Day 4 issue 3): never silent: a build that goes on past the block says the legend was left off
            rep.setdefault("notes", []).append("legend NOT DRAWN: " + legend_fit["notes"][0] + " (MAP-SPEC-005)")
        for n in legend_fit["notes"]:
            rep.setdefault("notes", []).append(n)
    elif blocks and not legend_on:
        findings.append(f("MAP-CART-005", "BLOCK" if any(b["kind"] in ("classes", "categories") for b in blocks) else "INFO",
                          "the legend is off but the map has classed or categorical layers; turn it on, or confirm the labels carry the meaning"))

    # ---- confidentiality
    conf = [l.id for l in plan.layers if l.confidentiality == "client-confidential"]
    banner_on = on("confidentiality_banner", "auto")
    banner_on = bool(conf) if banner_on == "auto" else bool(banner_on)
    banner_h = 6.0 if banner_on else 0.0
    if conf and not banner_on:
        findings.append(f("MAP-LIC-007", "BLOCK", f"layer(s) {', '.join(conf)} are client-confidential but the confidentiality banner is off"))

    # ---- inset (independent of the main frame)
    inset_cfg = els.get("inset_map") or {}
    inset_on = inset_cfg.get("on", "auto")
    inset = None
    if inset_on is not False:
        inset = build_inset(plan, inset_cfg, tpl, force=(inset_on is True))

    # ---- overlay sizes that do not depend on the frame
    north_wanted = bool(on("north_arrow"))
    na_cfg0 = els.get("north_arrow") or {}
    na_size = max(9.0, min(map_rect.w, map_rect.h) * 0.075)
    if na_cfg0.get("size"):                                    # 0.15.0: the arrow's height in mm, kept between 6 mm and a fifth of the frame
        na_size = max(6.0, min(float(na_cfg0["size"]), min(map_rect.w, map_rect.h) * 0.2))
    na_style = na_cfg0.get("style", "needle") if na_cfg0.get("style", "needle") in sd.NORTH_STYLES else "needle"
    sb_style = (els.get("scale_bar") or {}).get("style", "alternating")
    sb_style = sb_style if sb_style in SCALE_STYLES else "alternating"
    sb_wanted = bool(on("scale_bar"))
    inmap = tpl.get("layout") == "in-map"                     # the default layout: title, logo and the other elements sit inside the map frame
    tbox = title_box(plan, fonts, map_rect, findings) if inmap else None
    logo_box = None
    logo_beside_scale = bool(inmap and sb_wanted and "position" not in (els.get("scale_bar") or {})
                             and (els.get("logo") or {}).get("position", "lower-left") == "lower-left")
    if inmap and (els.get("logo") or {}).get("on", True):
        lh = float(tpl.get("logo_height_mm", 10)) * LOGO_SCALE_IN_MAP
        if logo_beside_scale:                               # the logo is as tall as the scale bar beside it: top of the bar to the baseline of its labels
            lh = scale_bar_visible_h(float(fonts["scale"]), bool((els.get("scale_bar") or {}).get("ratio", False)))
        _svg, lw = logo_markup(plan, t["land"], 0, 0, lh)
        if _svg:
            cs = logo_clear_space(plan)               # 0.15.0: the brand's clear space round its logo, when the pack declares one
            logo_box = {"h": lh, "w": lw, "pad": max(LOGO_PAD_MM, cs * lh) if cs else LOGO_PAD_MM, "clear": cs}

    dt_cfg = dict(els.get("data_table") or {})             # 0.15.0: a small table or bar chart of the top features (a copy: the row colours go on it)
    dt_rows, dt_size = [], None
    if dt_cfg.get("on", False):
        dt_rows, dt_fs = mex.table_rows(plan, dt_cfg)
        findings += dt_fs
        if dt_rows:
            dt_size = mex.table_size(dt_rows, dt_cfg, float(fonts["label"]))
    lg_cfg = els.get("logo") or {}
    if lg_cfg.get("variant") == "secondary" and not isinstance(plan.pack["brand"]["logo"].get("secondary"), dict):
        findings.append(f("MAP-BRAND-014", "WARN", f"the {plan.brand} brand pack carries no secondary logo; the primary logo is used"))

    inset_try = {"corner": "lower-right", "drop": False}           # 0.24.0: the locator's corner, moved (or the locator dropped) if it would cover data
    return banner_h, banner_on, blocks, conf, dt_cfg, dt_rows, dt_size, findings, inmap, inset, inset_cfg, inset_on, inset_try, legend_fit, logo_beside_scale, logo_box, na_cfg0, na_size, na_style, north_wanted, sb_style, sb_wanted, tbox


def _render_fit(H, W, banner_h, data_geoms, data_tree, dt_cfg, dt_size, els, findings, fonts, frame, inmap, inset, inset_cfg, inset_on, inset_try, legend_fit, logo_beside_scale, logo_box, map_rect, na_size, north_wanted, pad_pct, plan, rep, sb_wanted, shapely, spec, t, tbox):
    """render, stage 3 (1.9.0): choose the frame and place the overlays: extent.fit auto (move the map first, shrink only past the cover limit), the slide of one element, the locator dropped where its corner holds data. Split from render() unchanged."""
    # 1.3.0 (R1-FIT): extent.fit auto (move the map first, shrink only past fit_cover_pct of the focus area), overlay (never shrink) or shrink (1.2.0)
    ext_cfg = spec.get("extent") or {}
    fit_mode = str(ext_cfg.get("fit") or ("shrink" if getattr(plan, "look", "classic") == "classic" else "auto"))
    if frame is not None:
        fit_mode = "fixed"                                     # a sheet's shared frame: no move, no shrink
    fit_limit = float(ext_cfg.get("fit_cover_pct", FIT_COVER_PCT)) / 100.0
    forced_pan = ext_cfg.get("fit_pan_mm")                     # an atlas or a sheet gives every page one shared move
    focus_name = (spec.get("focus") or {}).get("area") if isinstance(spec.get("focus"), dict) else None
    focus_geoms = None
    if fit_mode not in ("shrink", "fixed"):
        if focus_name:
            fl = [l for l in plan.layers if l.id == focus_name and l.kind == "polygon"]
            focus_geoms = np.asarray([g for l in fl for g in l.gdf.geometry.values if g is not None and not g.is_empty], dtype=object) if fl else None
        if focus_geoms is None or not len(focus_geoms):
            focus_name, focus_geoms = None, data_geoms
    focus_tree = (data_tree if focus_geoms is data_geoms else (shapely.STRtree(focus_geoms) if focus_geoms is not None and len(focus_geoms) else None))
    auto_locator = inset_on == "auto" and "position" not in inset_cfg
    fgrids: dict = {}

    def evaluate(reserve: tuple, pan: tuple = (0.0, 0.0)) -> dict:
        """Fit the data with `reserve` mm kept free (moved `pan` mm on the sheet), then place the overlays; report how much data they would cover."""
        base = frame if frame is not None else lo.Frame.fit(map_rect, plan.extent, pad_pct, (reserve[0], reserve[1] + banner_h, reserve[2], reserve[3]))
        fr = base.panned(*pan)
        oc = lo.occupancy(fr, data_geoms, tree=data_tree) if len(data_geoms) else None
        sb_cfg = els.get("scale_bar") or {}
        inf = sb.scale_info(plan.crs, fr.visible, map_rect.w, units=sb_cfg.get("units", "auto"),
                            rotation=float((els.get("north_arrow") or {}).get("rotation", 0)),
                            segments=int(sb_cfg["segments"]) if sb_cfg.get("segments") else None)
        its = []

        def pos(cfg: dict, default: str) -> tuple[str, bool]:
            """(corner, locked). Every element stays at its default corner, or the corner the spec names; only `position: auto`
            lets the layout move it to a corner with less data under it (0.2.1: defaults always apply, moving is asked for)."""
            p = (cfg or {}).get("position")
            if p == "auto":
                return default, False
            return (p or default), True
        if tbox:
            its.append({"name": "title", "w": tbox["w"], "h": tbox["h"], "prefer": (els.get("title") or {}).get("position", "upper-left"), "locked": True})
        if logo_box:
            its.append({"name": "logo", "w": logo_box["w"] + 2 * logo_box["pad"], "h": logo_box["h"] + 2 * logo_box["pad"],
                        "prefer": (els.get("logo") or {}).get("position", "lower-left" if inmap else "lower-right"), "locked": True})
        if legend_fit and legend_fit["fits"]:
            corner, locked = pos(els.get("legend"), "upper-right" if inmap else "lower-left")
            its.append({"name": "legend", "w": legend_fit["size"]["width"], "h": legend_fit["size"]["height"], "prefer": corner, "locked": locked})
        if inset and not inset_try.get("drop"):
            corner, locked = pos(inset_cfg, inset_try["corner"])
            its.append({"name": "inset", "w": inset["w"], "h": inset["h"], "prefer": corner, "locked": locked})
        if dt_size:
            corner, locked = pos(dt_cfg, "upper-left")
            its.append({"name": "data_table", "w": dt_size["w"], "h": dt_size["h"], "prefer": corner, "locked": locked})
        if north_wanted and inf["north"]["show"]:
            na_cfg = els.get("north_arrow") or {}
            corner, locked = pos(na_cfg, "upper-right")
            its.append({"name": "north", "w": na_size, "h": na_size + 1.0, "prefer": corner,
                        "locked": locked, **({"beside": "legend", "side": "left"} if inmap and "position" not in na_cfg else {})})
        sb_svg_, sw_, sh_ = "", 0.0, 0.0
        if sb_wanted and inf["bar"] is not None:
            sb_svg_, sw_, sh_ = draw_scale_bar(inf, 0, 0, t, fonts["scale"], bool((els.get("scale_bar") or {}).get("ratio", False)))
            if logo_box and logo_beside_scale:
                sh_ = logo_box["h"]                         # same height as the logo, so the two line up top and bottom
            corner, locked = pos(els.get("scale_bar"), "lower-left" if inmap else "lower-right")
            its.append({"name": "scale", "w": sw_, "h": sh_, "prefer": corner, "locked": locked,
                        **({"beside": "logo", "side": "right"} if logo_box and logo_beside_scale else {})})
        err = None
        try:
            pl = lo.place_overlays(fr, oc, its, banner_h)
        except lo.LayoutError as ex:
            pl, err = {}, str(ex)
        rects_ = [v["rect"] for v in pl.values()]
        if "scale" in pl and inf.get("bar"):
            # 0.26.0 (TODO M2): the bar's first label ("0") is centred on its left end, so half of it and its halo hang outside the bar's box
            lead = tm.width_mm(str(inf["bar"]["ticks"][0]["label"]), float(fonts["scale"]), 400, tnum=True) / 2 + 0.45
            r_ = pl["scale"]["rect"]
            rects_ = [lo.Rect(r_.x0 - lead, r_.y0, r_.x1, r_.y1) if k == "scale" else v["rect"] for k, v in pl.items()]
        metric = lo.covered_fraction(fr, oc, rects_)
        res = {"frame": fr, "occ": oc, "info": inf, "items": its, "placed": pl, "err": err, "cover": metric, "reserve": reserve, "pan": tuple(pan), "base": base}
        if fit_mode not in ("shrink", "fixed"):
            counted = [r for k, r in zip(pl.keys(), rects_) if not (k == "inset" and auto_locator)]
            res["counted"] = counted
            res["room"] = lo.pan_room(base, _bbox_mm(base), pad_pct)
            key = tuple(round(v, 4) for v in reserve)
            if key not in fgrids:
                fgrids[key] = lo.focus_grid(base, focus_geoms, res["room"], tree=focus_tree)
            res["fgrid"] = fgrids[key]
            res["fcover"] = lo.focus_cover(fgrids[key], counted, *pan)
        return res

    def _bbox_mm(fr) -> tuple:
        x0, y0, x1, y1 = (float(v) for v in plan.extent)
        mx, my = fr.to_mm([x0, x1], [y0, y1])
        return (float(min(mx)), float(min(my)), float(max(mx)), float(max(my)))

    def fit_search(reserve: tuple) -> dict:
        """R1-FIT steps 1 to 3 for one reserve: full placement; then the smallest move that brings the cover to the limit; then sliding the
        elements whose position is auto along their edge, within their corner half. A locked element never moves."""
        c0 = evaluate(reserve)
        if c0["err"] or not c0["items"]:
            return c0
        if forced_pan is not None:
            dx, dy = (float(v) for v in forced_pan)
            r = c0["room"]
            return evaluate(reserve, (min(max(dx, r[0]), r[1]), min(max(dy, r[2]), r[3])))
        if c0["fcover"] <= fit_limit:
            return c0
        po = lo.pan_offsets(c0["base"], _bbox_mm(c0["base"]), c0["counted"], c0["fgrid"], fit_limit, c0["room"])
        c1 = evaluate(reserve, (po["dx"], po["dy"])) if (po["dx"] or po["dy"]) else c0
        if c1["err"]:
            c1 = c0
        if c1["fcover"] > fit_limit:
            _slide(c1)
        c1["fcover_before"] = c0["fcover"]
        return c1

    def _slide(c: dict) -> None:
        """Step 3: an element whose position is auto slides along its frame edge, inside its own corner half, when that lowers the cover."""
        free_items = [it for it in c["items"] if not it.get("locked") and it["name"] in c["placed"] and not (it["name"] == "inset" and auto_locator)]
        for it in free_items:
            pl_ = c["placed"][it["name"]]
            r0 = pl_["rect"]
            corner = pl_["corner"]
            half_w = map_rect.w / 2
            others = [v["rect"] for k, v in c["placed"].items() if k != it["name"]]
            best_r, best_cv = r0, c["fcover"]
            for k in range(1, 9):
                d = (half_w - r0.w - lo.CORNER_INSET_MM) * k / 8
                if d <= 0:
                    break
                sx = d if corner.endswith("left") else -d
                r = lo.Rect(r0.x0 + sx, r0.y0, r0.x1 + sx, r0.y1)
                if not r.inside(map_rect) or any(r.grown(1.0).hits(o) for o in others):
                    continue
                counted = [r if x is r0 else x for x in c["counted"]]
                cv = lo.focus_cover(c["fgrid"], counted, *c["pan"])
                if cv < best_cv - 1e-9:
                    best_r, best_cv = r, cv
                if cv <= fit_limit:
                    break
            if best_r is not r0:
                pl_["rect"] = best_r
                c["counted"] = [best_r if x is r0 else x for x in c["counted"]]
                c["fcover"] = best_cv
                c.setdefault("slid", []).append(it["name"])
                c["cover"] = lo.covered_fraction(c["frame"], c["occ"], [v["rect"] for v in c["placed"].values()])
            if c["fcover"] <= fit_limit:
                break

    if fit_mode in ("shrink", "fixed"):
        best = evaluate((0.0, 0.0, 0.0, 0.0))
    else:
        best = fit_search((0.0, 0.0, 0.0, 0.0))
    full_scale = best["frame"].scale
    fit_before = best.get("fcover_before", best.get("fcover", 0.0))
    if fit_mode == "auto" and best["fcover"] > fit_limit and best["items"] and not best["err"] and forced_pan is None:
        # Step 4: shrink, only past the limit: the 1.2.0 strips, each with the move run again, so a small shrink with a move beats a larger one
        gap = lo.CORNER_INSET_MM * 2
        want = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
        for it in best["items"]:
            if it.get("beside"):
                continue
            v, h = it["prefer"].split("-")
            want[h] = max(want[h], it["w"] + gap)
            want["top" if v == "upper" else "bottom"] = max(want["top" if v == "upper" else "bottom"], it["h"] + gap)
        cands = [{"left": (size, 0, 0, 0), "right": (0, 0, size, 0), "top": (0, size, 0, 0), "bottom": (0, 0, 0, size)}[side] for side, size in want.items() if size > 0]
        if want["bottom"] and want["top"]:
            cands.append((0, want["top"], 0, want["bottom"]))
        tried = [best] + [fit_search(c) for c in cands]
        ok = [c for c in tried if c["fcover"] <= fit_limit and not c["err"] and c["frame"].scale >= 0.6 * full_scale]
        pick = max(ok, key=lambda c: c["frame"].scale) if ok else min((c for c in tried if not c["err"] and c["frame"].scale >= 0.6 * full_scale),
                                                                       key=lambda c: (c["fcover"], -c["frame"].scale), default=best)
        best = pick
    if fit_mode == "shrink" and best["cover"] > OVERLAY_COVER_OK and best["items"] and not best["err"]:
        # Overlays would sit on data (a map whose data fills the frame). Keep a strip free on the side that costs the least scale.
        gap = lo.CORNER_INSET_MM * 2
        want = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
        for it in best["items"]:
            if it.get("beside"):
                continue
            v, h = it["prefer"].split("-")
            if h == "left" or h == "right":
                want[h] = max(want[h], it["w"] + gap)
            want["top" if v == "upper" else "bottom"] = max(want["top" if v == "upper" else "bottom"], it["h"] + gap)
        cands = []
        for side, size in want.items():
            if size > 0:
                cands.append({"left": (size, 0, 0, 0), "right": (0, 0, size, 0), "top": (0, size, 0, 0), "bottom": (0, 0, 0, size)}[side])
        if want["bottom"] and want["top"]:
            cands.append((0, want["top"], 0, want["bottom"]))
        tried = [best] + [evaluate(c) for c in cands]
        ok = [c for c in tried if c["cover"] <= OVERLAY_COVER_OK and not c["err"] and c["frame"].scale >= 0.6 * best["frame"].scale]
        pick = max(ok, key=lambda c: c["frame"].scale) if ok else min((c for c in tried if not c["err"] and c["frame"].scale >= 0.6 * best["frame"].scale),
                                                                       key=lambda c: (c["cover"], -c["frame"].scale), default=best)
        if pick is not best:
            rep.setdefault("notes", []).append(
                f"the map was reduced to {pick['frame'].scale / best['frame'].scale:.0%} of full size to keep the legend and other elements off the data "
                f"(they would have covered {best['cover']:.0%} of it)")
        best = pick
    if (inset is not None and inset_on == "auto" and "position" not in inset_cfg and "inset" in best["placed"] and best["occ"] is not None
            and not best["err"] and lo.covered_fraction(best["frame"], best["occ"], [best["placed"]["inset"]["rect"]]) > 0):
        # 0.24.0 (sample-map showcase): an automatic locator never covers data. It moves to another free corner, or it is left off and the
        # notes say why (the 1.5 percent the other elements may cover does not apply to it: the locator is never needed more than the data)
        moved = None
        for corner in ("lower-left", "upper-left", "upper-right"):
            inset_try["corner"] = corner
            c = evaluate(best["reserve"], best.get("pan", (0.0, 0.0)))
            if not c["err"] and "inset" in c["placed"] and c["cover"] <= best["cover"] and \
                    lo.covered_fraction(c["frame"], c["occ"], [c["placed"]["inset"]["rect"]]) == 0:
                moved = c
                break
        if moved is not None:
            best = moved
            rep.setdefault("notes", []).append(f"locator: moved to the {inset_try['corner'].replace('-', ' ')} corner so it covers no data")
        else:
            inset_try.update(corner="lower-right", drop=True)
            best = evaluate(best["reserve"], best.get("pan", (0.0, 0.0)))
            inset = None
            rep.setdefault("notes", []).append("locator: left off, because in every corner it would have covered part of the data")
    frame, occ, info, _items, placed = best["frame"], best["occ"], best["info"], best["items"], best["placed"]
    if best["err"]:
        findings.append(f("MAP-SPEC-005", "BLOCK", best["err"]))
    if fit_mode not in ("shrink", "fixed") and best["items"] and not best["err"]:            # 1.3.0 (R1-FIT): what the fit did, in the report and the notes
        scale_pct = best["frame"].scale / full_scale * 100 if full_scale else 100.0
        pan_ = tuple(round(float(v), 1) + 0.0 for v in best.get("pan", (0.0, 0.0)))
        moved_ = list(best.get("slid") or [])
        rep["fit"] = {"mode": fit_mode, "cover_limit_pct": round(fit_limit * 100, 2), "focus_basis": "focus-layer" if focus_name else "data",
                      "pan_mm": list(pan_), "scale_pct": round(scale_pct, 1), "cover_before": round(float(fit_before or 0.0) * 100, 2),
                      "cover_after": round(float(best.get("fcover", 0.0)) * 100, 2), "moved": moved_,
                      "room_mm": [round(float(v), 2) for v in best.get("room", (0.0, 0.0, 0.0, 0.0))]}
        names = [k for k in placed if not (k == "inset" and auto_locator)]
        what = ", ".join(n.replace("_", " ").replace("scale", "scale bar").replace("north", "north arrow") for n in names)
        basis = "the study area" if focus_name else "the data"
        parts = []
        if pan_[0]:
            parts.append(f"{abs(pan_[0]):.0f} mm {'right' if pan_[0] > 0 else 'left'}")
        if pan_[1]:
            parts.append(f"{abs(pan_[1]):.0f} mm {'down' if pan_[1] > 0 else 'up'}")
        if parts or scale_pct < 99.95 or moved_:
            note = ("the map was moved " + " and ".join(parts) + "; " if parts else "") + \
                   (f"the {', '.join(moved_)} slid along the frame edge; " if moved_ else "") + \
                   f"the {what} now cover {best.get('fcover', 0.0) * 100:.1f}% of {basis} (limit {fit_limit * 100:g}%); scale kept at {scale_pct:.0f}%"
            rep.setdefault("notes", []).append(note)
            findings.append(f("MAP-SPEC-009", "INFO", note))
        if fit_mode == "overlay" and best.get("fcover", 0.0) > fit_limit:
            findings.append(f("MAP-SPEC-010", "WARN", f"extent.fit overlay: the map elements cover {best['fcover'] * 100:.1f}% of {basis}, over the "
                                                      f"{fit_limit * 100:g}% limit; the map is never shrunk in this mode (use fit: auto to allow it)"))
    rep["frame_scale_mm_per_unit"] = frame.scale
    rep["map_window"] = {"crs": plan.crs, "extent": [round(float(v), 6) for v in frame.visible], "rect_mm": [map_rect.x0, map_rect.y0, map_rect.x1, map_rect.y1], "page_mm": [W, H]}
    rep["frame"] = {"rect_mm": [round(frame.rect.x0, 3), round(frame.rect.y0, 3), round(frame.rect.x1, 3), round(frame.rect.y1, 3)],
                    "scale": frame.scale, "cx": frame.cx, "cy": frame.cy}          # 1.3.0: the frame itself, so a sheet can show its panels share it
    rep["overlay_cover"] = best["cover"]
    rep["data_cover"] = float(occ.mean()) if occ is not None else 0.0
    if len(data_geoms):
        bx = shapely.bounds(data_geoms[~shapely.is_missing(data_geoms) & ~shapely.is_empty(data_geoms)]).astype("float64")
        if len(bx):
            x0, y0, x1, y1 = bx[:, 0].min(), bx[:, 1].min(), bx[:, 2].max(), bx[:, 3].max()
            vx0, vy0, vx1, vy1 = frame.visible
            rep["extent_fill"] = float(min(1.0, ((x1 - x0) * frame.scale) * ((y1 - y0) * frame.scale) / (frame.inner.w * frame.inner.h)))
            rep["clipped_features"] = int(((bx[:, 0] < vx0) | (bx[:, 1] < vy0) | (bx[:, 2] > vx1) | (bx[:, 3] > vy1)).sum())
    return frame, info, inset, placed


def _render_body(H, W, banner_h, banner_on, els, findings, fonts, frame, map_rect, on, placed, plan, rep, t):
    """render, stage 5 (1.9.0): the map body: grounds, relief, context, the data layers in order, the frame. Split from render() unchanged."""
    # ---- body
    body: list[str] = []
    bg_fill = t["water"] if plan.basemap else t["background"]
    trt = getattr(plan, "treatments", None) or {}             # 1.4.0 (R2-TREAT)
    import treatments as trm
    body.append(sd.rect(0, 0, W, H, t["background"], extra='class="gm-page"'))          # the classes let an export drop the fills (M16: --transparent)
    paper_svg = trm.paper_markup(plan.style, trt["paper"], W, H) if "paper" in trt else ""
    if paper_svg:
        body.append(paper_svg)
        rep.setdefault("notes", []).append("paper: the pack's paper under the sheet; the ground and the land drawn see-through over it")
    i_map = len(body)
    body.append('<g id="gm-map" clip-path="url(#gm-clip)">')
    body.append(sd.rect(map_rect.x0, map_rect.y0, map_rect.w, map_rect.h, bg_fill, extra='class="gm-frame-bg"' + (' fill-opacity="0.55"' if paper_svg else "")))
    body.append(draw_basemap(plan, frame, ground_opacity=0.5 if paper_svg else 1.0))
    if "water_lining" in trt and plan.basemap:
        wl_svg, wl_fs, wl_notes = trm.water_lining(plan, frame, trt["water_lining"], t)
        body.append(wl_svg)
        findings += wl_fs
        rep.setdefault("notes", []).extend(wl_notes)
    if "grid" in trt:
        body.append(trm.grid_markup(map_rect, trt["grid"], t))
    obstacles: list[Rect] = []
    grat_taken: list[Rect] = []
    if on("graticule", False):
        gsvg, grat_taken = graticule(plan, frame, els.get("graticule") or {}, fonts, [v["rect"] for v in placed.values()])
        body.append(gsvg)
    layer_svgs, point_obstacles = draw_layers(plan, frame, rep)
    body += layer_svgs
    obstacles += point_obstacles + grat_taken
    for v in placed.values():
        obstacles.append(v["rect"])
    if banner_on:
        obstacles.append(Rect(map_rect.x0, map_rect.y0, map_rect.x1, map_rect.y0 + banner_h))
    if "hill_glyphs" in trt:                                  # 1.4.0 (R2-TREAT): after the data, before the labels (which avoid them)
        land_, _w = trm._land_and_water(plan, frame, False) if plan.basemap else (None, None)
        hg_svg, hg_rects, hg_fs, hg_notes = trm.hill_glyphs(plan, frame, trt["hill_glyphs"], t, obstacles, land_)
        body.append(hg_svg)
        obstacles += hg_rects
        findings += hg_fs
        rep.setdefault("notes", []).extend(hg_notes)
    co_cfg = els.get("callouts") or {}                      # 0.15.0: callouts take their room before the labels do
    co_svg = ""
    if co_cfg.get("on", bool(co_cfg.get("items"))) and co_cfg.get("items"):
        co_svg, co_rects, co_recs, co_fs = mex.draw_callouts(plan, frame, co_cfg, obstacles, fonts)
        obstacles += co_rects
        findings += co_fs
        rep["callouts"] = co_recs
    return body, co_svg, findings, i_map, obstacles, point_obstacles, trm, trt


def _render_labels(H, W, banner_h, banner_on, blocks, body, border, co_svg, dt_cfg, dt_rows, dt_size, els, findings, fonts, fr_style, frame, i_map, info, inset, inset_cfg, legend_fit, logo_beside_scale, logo_box, map_rect, na_cfg0, na_size, na_style, obstacles, on, ornaments, outer_rect, placed, plan, point_obstacles, rep, sb_style, spec, t, tbox, tpl, trm, trt):
    """render, stage 6 (1.9.0): labels (the data's own first, then place, water and admin names) and the overlays drawn at their placed rectangles. Split from render() unchanged."""
    # ---- labels: the data's own first, then the place, water and admin names in what room is left
    lab_svg, lab_stats, taken = draw_labels(plan, frame, obstacles, fonts, tpl)
    body.append(lab_svg)
    after_names: list[Rect] = []
    names_svg, names_stats = draw_names(plan, frame, taken, point_obstacles, fonts, tpl, out_taken=after_names)
    body.append(names_svg)
    lab_stats["names"] = names_stats
    if names_stats.get("script_fallback"):                    # 1.5.0 (R3-SCRIPT): names whose script has no font or shaper here
        miss = names_stats["script_fallback"]
        import script_text as st_
        keys = sorted({st_.key_for(x) or x.lower() for x in miss})
        findings.append(f("MAP-LOC-003", "WARN", f"place names in {', '.join(miss)} script{'s' if len(miss) > 1 else ''} have no font"
                          + ("" if st_.shaper() else " or shaper (uharfbuzz)") + " here, so those names are in English; run doctor --fonts "
                          + ",".join(keys) + " and build again", {"scripts": miss}))
    if any(l.role == "contour" for l in plan.layers):              # contour elevations last: they take the room nothing else wants (M12)
        c_svg, c_stats = draw_contour_labels(plan, frame, after_names or taken, fonts, tpl)
        body.append(c_svg)
        lab_stats["contours"] = c_stats
    rep["labels"] = lab_stats
    body.append("</g>")
    lc = trt.get("line_character") or {}
    if lc.get("style") == "sketch":                           # 1.4.0 (R2-TREAT): the drawn lines only; the data outputs are never jittered
        body[i_map:] = [trm.sketch_map("".join(body[i_map:]), lc, spec["id"])]
        rep.setdefault("notes", []).append("line character: sketch (seeded jitter on the drawn lines; the data files keep the true geometry)")
    body.append(sd.rect(map_rect.x0, map_rect.y0, map_rect.w, map_rect.h, "none", t["frame"], 0.35, 'class="gm-neatline"') if on("neatline") else "")
    if border:
        b_svg, b_notes = ornaments.frame_border(fr_style, outer_rect, map_rect, frame, plan.crs, t, max(4.5, float(fonts["label"]) - 1.5),
                                                (els.get("frame") or {}).get("bars"), sheet=(W, H))
        body.append(b_svg)
        rep.setdefault("notes", []).extend(b_notes)
    # overlays
    ov: list[str] = []
    if "title" in placed and tbox:
        r = placed["title"]["rect"]
        cells = None
        if tbox.get("box") == "title-block":
            cells = [("Drawing no.", spec["id"]), ("Date", today().isoformat()), ("Scale", info.get("ratio") or ""),
                     ("Revision", str((els.get("title") or {}).get("revision") or "A")), ("Drawn by", plan.brand_name), ("Sheet", plan.tpl_name)]
        ov.append(draw_title_box(tbox, r.x0, r.y0, t, cells))
    if "logo" in placed and logo_box:
        r = placed["logo"]["rect"]
        ov.append(logo_markup(plan, t["land"], r.x0 + logo_box["pad"], r.y0 + logo_box["pad"], logo_box["h"])[0])
    if "legend" in placed and legend_fit:
        r = placed["legend"]["rect"]
        lfr = (els.get("legend") or {}).get("frame", True)
        leg_svg = draw_legend(blocks, legend_fit, r.x0, r.y0, t, header=(els.get("legend") or {}).get("title") or None,
                              frame=lfr if lfr in ("ruled", "cartouche") else lfr is not False)
        for col, pid in (plan.__dict__.get("_hatch") or {}).items():       # 1.4.0: sketch hatches in the swatches too
            leg_svg = re.sub(rf'(<(?:rect|path)\b[^>]*?)fill="{re.escape(col)}"', rf'\1fill="url(#{pid})"', leg_svg, flags=re.I)
        ov.append(leg_svg)
    if "data_table" in placed and dt_size:
        r = placed["data_table"]["rect"]
        ov.append(mex.draw_table(dt_rows, dt_cfg, dt_size, r.x0, r.y0, t, float(fonts["label"])))
    if co_svg:
        ov.append(co_svg)
    if "inset" in placed and inset:
        r = placed["inset"]["rect"]
        ov.append(draw_inset(inset, r.x0, r.y0, t, frame.visible, float(fonts["label"]), highlight=inset_cfg.get("highlight") or {}))
    if "north" in placed:
        r = placed["north"]["rect"]
        if na_style == "compass-rose":                 # 1.4.0 (R2-ORN): 8 or 16 points, in the pack's ink
            import ornaments
            ov.append(ornaments.compass_rose(r.x0 + na_size / 2, r.y0 + (na_size + 1.0) / 2, na_size, info["north"]["rotation"], t.get("ornament") or t["label"],
                                             t["background"], fonts["label"] + 1, int(na_cfg0.get("points") or 8)))
        else:
            ov.append(sd.north_arrow(r.x0 + na_size / 2, r.y0 + (na_size + 1.0) / 2, na_size, info["north"]["rotation"], t["label"], t["background"], t["label"], fonts["label"] + 1,
                                     style=na_style))
    if "scale" in placed:
        r = placed["scale"]["rect"]
        svg_, _, _ = draw_scale_bar(info, r.x0, r.y0 + (0.09 if (logo_box and logo_beside_scale) else 0.5), t, fonts["scale"], bool((els.get("scale_bar") or {}).get("ratio", False)),
                                    style=sb_style)
        ov.append(svg_)
    if banner_on:
        txt = (els.get("confidentiality_banner") or {}).get("text") or "CLIENT CONFIDENTIAL"
        fill = t["status"]["danger"]
        tcol = cf.text_on(fill, t["label"], t["background"])
        ov.append(sd.rect(map_rect.x0, map_rect.y0, map_rect.w, banner_h, fill, "none"))
        ov.append(sd.text(map_rect.x0 + map_rect.w / 2, map_rect.y0 + banner_h / 2 + fonts["banner"] * PT * 0.35, txt, fonts["banner"], tcol, weight=700, anchor="middle"))
    body += ov
    return body,


def render(plan: mp.Plan, frame: "lo.Frame | None" = None, elements_off=()) -> Rendered:
    """The map as one SVG page. 1.3.0 (R1-PANEL): `frame` draws the map in a frame passed in (its rect is the template's map region) instead of
    fitting one, so the panels of a sheet share it exactly; `elements_off` lists the elements the sheet draws once for all of them (legend,
    north_arrow, scale_bar, inset_map, title, logo), which this page then leaves out."""
    if elements_off:
        import copy as _copy
        plan.spec = _copy.deepcopy(plan.spec)
        els_ = plan.spec.setdefault("elements", {})
        for name in elements_off:
            els_[name] = {**(els_.get(name) or {}), "on": False}
    spec, tpl, t = plan.spec, plan.tpl, plan.tokens
    plan.__dict__["_hatch"] = {}
    if getattr(plan, "style", None) is not None:          # 1.4.0: the plan's pack faces (another plan may have been prepared since)
        tm.use_type({r: f_ for r, f_ in plan.style.type.items() if not f_.get("brand")} or None)
    W, H = tpl["size_mm"]
    fonts = dict(tpl["fonts"])
    els = spec.get("elements") or {}
    findings: list[dict] = []
    rep: dict = {"template": plan.tpl_name, "brand": plan.brand, "crs": plan.crs}

    def on(name: str, default=True):
        v = (els.get(name) or {}).get("on", default)
        return v

    tiles_ = [b for b in plan.basemap if b.get("kind") == "tiles"]
    if tpl.get("footer_fit") and tpl.get("footer_fit_classic") is False and getattr(plan, "look", "classic") == "classic":
        tpl["footer_fit"] = False                 # 1.0.0: a template fitted since 1.0.0 keeps its fixed footer in the classic look
    if tpl.get("footer_fit") and not tiles_:    # M16: the footer takes what its notes need
        grown = lo.fit_footer(tpl, draw_footer(plan, tpl, fonts, {}, [], [], logo_in_map=tpl.get("layout") == "in-map")[1]["need_mm"])
        if grown:
            rep["footer_fit_mm"] = grown
    elif tpl.get("footer_fit") and getattr(plan, "look", "classic") != "classic":
        # 0.19.0: with a tile basemap too, when the prepared tiles still cover the taller frame (the room a shorter footer frees goes to the map)
        trial = dict(tpl)
        grown = lo.fit_footer(trial, draw_footer(plan, trial, fonts, {}, [], [], logo_in_map=tpl.get("layout") == "in-map")[1]["need_mm"])
        if grown:
            vis = lo.Frame.fit(lo.rect_of(trial["map"]), plan.extent, float((spec.get("extent") or {}).get("padding_pct", 5))).visible
            bb = (tiles_[0].get("image") or {}).get("bbox_3857")
            if bb and bb[0] <= vis[0] and bb[1] <= vis[1] and bb[2] >= vis[2] and bb[3] >= vis[3]:
                tpl.clear()
                tpl.update(trial)
                rep["footer_fit_mm"] = grown
    import ornaments
    fr_style = str((els.get("frame") or {}).get("style") or "single")       # 1.4.0 (R2-ORN): double or bar-border take a band from the map region
    border = ornaments.border_mm(fr_style) if frame is None else 0.0
    outer_rect = lo.rect_of(tpl["map"])
    if border:
        m_ = dict(tpl["map"])
        m_.update(x=m_["x"] + border, y=m_["y"] + border, w=m_["w"] - 2 * border, h=m_["h"] - 2 * border)
        tpl = {**tpl, "map": m_}
    map_rect = lo.rect_of(tpl["map"])
    pad_pct = float((spec.get("extent") or {}).get("padding_pct", 5))
    import shapely
    data_parts = [np.asarray(l.gdf.geometry.values, dtype=object) for l in plan.layers
                  if l.role not in ("context", "labels", "contour") and l.kind != "raster"]     # a raster fills the frame: overlays may sit on it (M12)
    data_geoms = np.concatenate(data_parts) if data_parts else np.array([], dtype=object)
    data_tree = shapely.STRtree(data_geoms) if len(data_geoms) else None          # built once for every layout the overlays try (M16)

    banner_h, banner_on, blocks, conf, dt_cfg, dt_rows, dt_size, findings, inmap, inset, inset_cfg, inset_on, inset_try, legend_fit, logo_beside_scale, logo_box, na_cfg0, na_size, na_style, north_wanted, sb_style, sb_wanted, tbox = _render_overlays(els, findings, fonts, map_rect, on, plan, rep, t, tpl)

    frame, info, inset, placed = _render_fit(H, W, banner_h, data_geoms, data_tree, dt_cfg, dt_size, els, findings, fonts, frame, inmap, inset, inset_cfg, inset_on, inset_try, legend_fit, logo_beside_scale, logo_box, map_rect, na_size, north_wanted, pad_pct, plan, rep, sb_wanted, shapely, spec, t, tbox)

    # ---- scale bar and north arrow findings (for the chosen frame)
    rep["scale"] = {k: info[k] for k in ("scale_denominator", "ratio", "variation", "units", "distortion", "bar_hidden")}
    try:                                                         # 1.5.0: MAP-LOC-001/002, the locale and units chosen and why
        findings += gl.locale_finding(plan, info)
        rep["locale"] = {"country": gl.plan_country(plan), "locale": gl.plan_locale(plan), "units": info.get("units"),
                         "units_reason": info.get("units_reason")}
    except Exception as e:  # noqa: BLE001 - the map does not depend on it
        findings.append(f("MAP-LOC-001", "INFO", f"the country under the centre could not be read ({type(e).__name__}); kilometres and en-GB dates"))
    rep["north"] = info["north"]
    if info["distortion"]["severity"] == "warn":
        findings.append(f("MAP-CRS-003", "WARN", info["distortion"]["message"]))
    elif info["distortion"]["severity"] == "block" and on("scale_bar"):
        findings.append(f("MAP-CRS-003", "BLOCK", info["distortion"]["message"] + "; turn the scale bar off or use another projection"))
    if north_wanted and not info["north"]["show"]:
        findings.append(f("MAP-CART-020", "INFO", "north arrow hidden: " + info["north"]["reason"]))
    if sb_wanted and info["bar"] is None:
        findings.append(f("MAP-CART-021", "INFO", "scale bar hidden: " + info["bar_hidden"]))
    if logo_box and logo_box.get("clear") and "logo" in placed:    # MAP-BRAND-015: nothing inside the logo's clear space
        inside, need = clear_space_intrusions(placed, logo_box)
        if inside:
            findings.append(f("MAP-BRAND-015", "WARN", f"the {', '.join(inside)} sit(s) inside the logo's clear space ({logo_box['clear']:g} of its height, {need:.1f} mm, "
                                                       f"from the {plan.brand} brand pack); move them or the logo"))
    rep["overlays"] = {k: {"corner": v["corner"], "moved_from": v["moved_from"], "reason": v["reason"], "cover": v["cover"],
                           "rect_mm": [round(v["rect"].x0, 2), round(v["rect"].y0, 2), round(v["rect"].x1, 2), round(v["rect"].y1, 2)]} for k, v in placed.items()}
    if inset and "inset" in placed:
        rep["inset"] = {"max_zoom_out": inset["max_zoom_out"], "zoom_out": inset["zoom_out"], "state": inset.get("state"), "crs": inset["crs"], "north": inset.get("north")}
    for k, v in placed.items():
        if v["moved_from"]:
            rep.setdefault("notes", []).append(f"{k} moved from {v['moved_from']} to {v['corner']}: {v['reason']}")

    body, co_svg, findings, i_map, obstacles, point_obstacles, trm, trt = _render_body(H, W, banner_h, banner_on, els, findings, fonts, frame, map_rect, on, placed, plan, rep, t)
    body, = _render_labels(H, W, banner_h, banner_on, blocks, body, border, co_svg, dt_cfg, dt_rows, dt_size, els, findings, fonts, fr_style, frame, i_map, info, inset, inset_cfg, legend_fit, logo_beside_scale, logo_box, map_rect, na_cfg0, na_size, na_style, obstacles, on, ornaments, outer_rect, placed, plan, point_obstacles, rep, sb_style, spec, t, tbox, tpl, trm, trt)

    # ---- title and footer
    if inmap:
        title_svg, title_text = "", (tbox["text"] if tbox else "")
    else:
        title_svg, title_text = draw_title(plan, tpl, fonts, findings)
    foot_svg, foot_info = draw_footer(plan, tpl, fonts, info, findings, conf, logo_in_map=inmap)
    body.append(title_svg)
    body.append(foot_svg)
    rep["footer"] = foot_info

    alt = alt_text(plan, title_text, blocks, info)
    svg = assemble(plan, W, H, "".join(body), frame, map_rect, title_text, alt, bool(plan.basemap))
    stem = f"{plan.pack['tokens'].get('output_prefix', '')}{spec['id']}_{plan.tpl_name}{pack_suffix(plan)}_v{today().strftime('%Y%m%d')}"
    if getattr(plan, "style", None) is not None:
        rep["style"] = plan.style.record()
    rep["alt_text"] = alt
    return Rendered(svg=svg, report=rep, findings=findings, filename_stem=stem, title=title_text, alt_text=alt)


# ---------------------------------------------------------------------------------------------------------------- labels
@_role("label")
def draw_labels(plan: mp.Plan, frame: lo.Frame, obstacles: list[Rect], fonts: dict, tpl: dict) -> tuple[str, dict]:
    import shapely
    t = plan.tokens
    out = []
    stats = {"layers": {}}
    pt = fonts["label"]
    floor = lg.MIN_LABEL_PT["print" if tpl["kind"] == "print" else "slide"]
    taken = list(obstacles)
    for lb in plan.spec.get("labels") or []:
        l = next((x for x in plan.layers if x.id == lb["layer"]), None)
        if l is None:
            continue
        field_ = lb.get("field")
        if field_ not in l.gdf.columns:
            raise mp.PlanError(f"labels: layer '{l.id}' has no field '{field_}'", gc.EXIT_USAGE)
        size = float(lb.get("size", pt))
        gdf = l.gdf
        cands = []

        def labelled(frame_, idx):
            """The rows of `idx` whose label value is not missing (None or NaN), and those values."""
            vals = frame_[field_].to_numpy(dtype=object)[idx]
            ok = np.fromiter((not (v is None or (isinstance(v, float) and v != v)) for v in vals), dtype=bool, count=len(vals))
            return idx[ok], vals[ok]

        def priorities(frame_, idx, default):
            pr = lb.get("priority_field")
            if not (pr and pr in frame_.columns) or not len(idx):
                return np.asarray(default, dtype="float64")
            pv = np.asarray(frame_[pr].to_numpy()[idx], dtype="float64")
            return np.where(np.isfinite(pv), pv, default)

        def top(prio):
            """The candidates place() would try, in its order: highest priority first, ties in row order, at most MAX_CANDIDATES (M16:
            chosen before any candidate object is made, so a layer of 250,000 points builds 2,000 of them)."""
            return np.argsort(-prio, kind="stable")[:le.MAX_CANDIDATES]
        if l.kind == "polygon":
            min_km2 = float(lb.get("min_zoom_area_km2", 0) or 0)
            areas = gdf.to_crs("EPSG:6933").area.to_numpy() / 1e6 if gdf.crs is not None else np.zeros(len(gdf))
            geoms, keep = to_sheet(gdf, frame, True)
            idx = np.where(keep)[0]
            idx = idx[areas[idx] >= min_km2]
            idx, vals = labelled(gdf, idx)
            prio = priorities(gdf, idx, areas[idx].astype("float64"))
            pick = top(prio)
            idx, vals, prio = idx[pick], vals[pick], prio[pick]
            pts = shapely.point_on_surface(geoms[idx]) if len(idx) else np.array([], dtype=object)
            px_, py_ = (shapely.get_x(pts), shapely.get_y(pts)) if len(idx) else (np.array([]), np.array([]))
            bnds = shapely.bounds(geoms[idx]) if len(idx) else np.zeros((0, 4))
            assigned = np.asarray(l.cls.assigned) if l.cls is not None else None
            for j, i in enumerate(idx.tolist()):
                fill = None
                if assigned is not None:
                    ci = int(assigned[i])
                    fill = t["no_data"] if ci < 0 else l.colors[ci]
                elif l.fill:
                    fill = l.fill
                cands.append(le.Candidate(str(vals[j]), "area", float(px_[j]), float(py_[j]), float(prio[j]), avail_w=float(bnds[j, 2] - bnds[j, 0]), fill=fill))
        elif l.kind == "point":
            gp, _src = explode_points(gdf)
            mx, my, vis = sheet_points(gp, frame)
            # 1.2.0 (Day 4 issues 1 and 19): the offset is the symbol as drawn: a proportional circle's own diameter, the look's count-based
            # size for plain points (it was the 3 pt default for both, so every label on a proportional layer met its own circle)
            if l.symbol.get("_size_mm") is not None and str(l.symbol.get("shape", "")).lower() != "none":
                d = float(l.symbol["_size_mm"]) / 2
            elif str(l.symbol.get("shape", "")).lower() == "none":
                d = 0.0
            else:
                d = float(l.symbol.get("size", 3)) * 0.3528 / 2
            sizes = getattr(l, "_drawn_sizes", None)
            sizes = sizes if (l.role == "proportional" and sizes is not None and len(sizes) == len(gp)) else None
            idx, vals = labelled(gp, np.where(vis)[0])
            prio = priorities(gp, idx, -idx.astype("float64"))
            pick = top(prio)
            idx, vals, prio = idx[pick], vals[pick], prio[pick]
            for j, i in enumerate(idx.tolist()):
                r_ = float(sizes[i]) / 2 if sizes is not None and sizes[i] > 0 else d
                cands.append(le.Candidate(str(vals[j]), "point", float(mx[i]), float(my[i]), float(prio[j]), radius=r_))
        else:                                              # 0.15.0: labels that follow their line
            if size < floor:
                stats["layers"][l.id] = {"placed": 0, "dropped_size": len(gdf)}
                continue
            geoms, _keep = to_sheet(gdf, frame, False)
            svg_l, rects_l, st_l = mex.line_label_markup(plan, l, lb, frame, taken, size, geoms)
            out.append(svg_l)
            taken += rects_l
            stats["layers"][l.id] = {"placed": st_l["placed"], "dropped_collision": st_l["collision"], "dropped_size": st_l["too_short"] + st_l["too_bent"],
                                     "dropped_outside": 0, "along_line": st_l}
            continue
        res = le.place(cands, lo.rect_of(tpl["map"]), taken, size, 400, floor)
        stats["layers"][l.id] = res.as_dict()
        if lb.get("leaders") and l.kind == "point" and res.dropped_collision:      # 0.15.0: a leader line for a label with no room beside its symbol
            got = [(p.text, p.rect) for p in res.placed]
            left = [c for c in cands if not any(tx == c.text and abs((r.x0 + r.x1) / 2 - c.x) < 30 and abs((r.y0 + r.y1) / 2 - c.y) < 15 for tx, r in got)]
            taken_now = taken + [p.rect.grown(le.PAD_MM) for p in res.placed]
            svg_ld, rects_ld, n_ld = mex.leader_labels(plan, left, frame, taken_now, size)
            out.append(svg_ld)
            taken += rects_ld
            stats["layers"][l.id]["with_leader"] = n_ld
            stats["layers"][l.id]["dropped_collision"] -= n_ld
        for p in res.placed:
            fg, halo = t["label"], t["label_halo"]
            if p.fill:                                    # text and halo swap when the fill is dark, so contrast holds on every class
                fg = cf.text_on(p.fill, t["label"], t["background"])
                halo = t["background"] if fg == t["label"] else t["label"]
                if getattr(plan, "look", "classic") != "classic":
                    halo = p.fill                         # 0.18.0: a halo in the fill's own colour breaks the lines under the text, no sticker
            out.append(sd.text(p.x, p.y, p.text, size, fg, anchor=p.anchor, halo=halo,
                               halo_w=max(0.3, size * PT * 0.22)))
            taken.append(p.rect.grown(le.PAD_MM))
    stats["total_candidates"] = sum(v["placed"] + v["dropped_collision"] + v["dropped_size"] + v["dropped_outside"] for v in stats["layers"].values())
    stats["placed"] = sum(v["placed"] for v in stats["layers"].values())
    return '<g class="gm-labels">' + "".join(out) + "</g>", stats, taken


# ---------------------------------------------------------------------------------------------------------------- names (toponymy, M10)
WATER_SLANT_DEG = 12.0                # water names lean like the tagline: Inter has no italic face, so an oblique stands in for it
ADMIN_MIN_FRAME_SHARE = 0.04          # a state or province is named only when at least this share of the frame is its land
ADMIN_WHOLE_FRAME_SHARE = 0.85        # ...and not when it is the whole map (the title already says which state it is)
POINT_CLEARANCE_MM = 1.5              # a place under a data point symbol (within this distance) is never named: the symbol is the point
CITY_DOT_MM = 0.6                     # the dot that marks a named place when no data symbol marks it
OUTSIDE_NAME_STEP_PT = 1.0          # 0.22.0: a place name outside the study area is this much smaller (visual styling spec 4.2)
TIER_SIZE = {"capital": 0.0, "city": 0.0, "town": -0.5, "village": -1.0}   # pt offsets from the template's label size
BIG_CITY_POP = 1_000_000
NAME_TIER_PRIORITY = {"capital": 3, "city": 2, "town": 1, "village": 0}
MAX_NAME_CANDIDATES = 1500


def water_label_color(t: dict) -> str:
    """The colour water names are set in: the brand's `water_label` token when a pack declares one; otherwise the declared colour nearest the
    water's hue that reads at 3:1 on the water (the sequential ramp, the accent, the first class colour, then the label colour). Every colour on
    a map must be a declared token (MAP-BRAND-001), so the colour is chosen among them rather than derived from the water."""
    import colorlib as cl
    if t.get("water_label"):
        return t["water_label"]
    water = t["water"]
    hw = cl.hls(water)[0]
    cands = list(t.get("sequential_ramp") or []) + [t["emphasis"]] + list((t.get("categorical") or [])[:1]) + [t["label"]]
    ok = [c for c in cands if cl.contrast(c, water) >= 3.0]
    if not ok:
        return t["label"]
    return min(ok, key=lambda c: min(abs(cl.hls(c)[0] - hw), 1.0 - abs(cl.hls(c)[0] - hw)))


def admin_label_color(t: dict) -> tuple[str, float]:
    """(colour, opacity) for admin names: a muted neutral that still reads at 3:1 on the land, else the label colour a little faded."""
    import colorlib as cl
    for c in reversed(list(t.get("neutral_ramp") or [])):
        if cl.contrast(c, t["land"]) >= 3.0 and cl.contrast(c, t["background"]) >= 3.0:
            return c, 1.0
    return t["label"], 0.75


def place_tier(adm0cap, featurecla, pop_max) -> str:
    """capital (a national capital), city (a million or more), town, or village (under 50,000)."""
    try:
        cap = int(adm0cap or 0) == 1 or str(featurecla or "").startswith("Admin-0 capital")
    except (TypeError, ValueError):
        cap = False
    pop = float(pop_max or 0) if pop_max == pop_max else 0.0
    if cap:
        return "capital"
    if pop >= BIG_CITY_POP:
        return "city"
    if pop < 50_000:
        return "village"
    return "town"


def caps(text: str) -> str:
    """Upper case for a capital's name; Greek capitals drop their accents (ΑΘΗΝΑ, not ΑΘΉΝΑ), as Greek typography sets them."""
    import unicodedata
    u = str(text).upper()
    if any("\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff" for ch in u):
        u = unicodedata.normalize("NFC", "".join(ch for ch in unicodedata.normalize("NFD", u) if ch not in "\u0301\u0300\u0342\u0313\u0314\u0345"))
    return u


def select_places(gdf, cfg: dict, tier: dict):
    """The places a map may name, from the gazetteer frame (any CRS): rows kept, each with its tier and priority. The scale decides the deepest
    scalerank (SCALE_TIERS); `names.places` narrows it to capitals or major cities; `names.min_pop` applies on the local tier; `names.exclude`
    removes names; `names.max` keeps the top N by tier and population."""
    import pandas as pd
    if cfg["places"] == "none" or not len(gdf):
        return gdf.iloc[0:0].copy()
    g = gdf.copy()
    rank = tier["places"]
    if rank is not None and "scalerank" in g.columns:
        g = g[g["scalerank"].astype(float) <= rank]
    else:
        g = g[pd.to_numeric(g["pop_max"], errors="coerce").fillna(0) >= cfg["min_pop"]]
    g = g.copy()
    g["_tier"] = [place_tier(a, f, p) for a, f, p in zip(g.get("adm0cap", [0] * len(g)), g.get("featurecla", [""] * len(g)), g["pop_max"])]
    if cfg["places"] == "capitals":
        g = g[g["_tier"] == "capital"]
    elif cfg["places"] == "major":
        g = g[g["_tier"].isin(["capital", "city"])]
    if cfg["exclude"]:
        g = g[[not mp.name_excluded(v, cfg["exclude"]) for v in g["name"]]]
    pop = pd.to_numeric(g["pop_max"], errors="coerce").fillna(0)
    g["_prio"] = [NAME_TIER_PRIORITY[t] * 1e9 + p for t, p in zip(g["_tier"], pop)]
    g = g.sort_values("_prio", ascending=False)
    if cfg["max"] is not None:
        g = g.head(int(cfg["max"]))
    return g


@_role("label")
def draw_names(plan: mp.Plan, frame: lo.Frame, taken: list[Rect], point_obstacles: list[Rect], fonts: dict, tpl: dict, out_taken: list | None = None) -> tuple[str, dict]:
    """Place, water and admin names from the bundled gazetteer, placed after the data labels (so they lose every collision with them).

    Capitals are bold small capitals with a ring, cities of a million or more are bold with a dot, towns regular, villages a size smaller; water
    names lean 12 degrees in the water label colour; state and province names are loose uppercase in a muted colour at the inside point of
    what the frame shows of them, only when that is at least 4 percent of the frame. A place that sits under a data point symbol is not named:
    the symbol is the point, and its own label (if the spec asks for one) already said what it is."""
    import shapely
    from shapely.geometry import box as sbox
    t = plan.tokens
    cfg = mp.names_config(plan.spec)
    stats: dict = {"on": cfg["on"]}
    import treatments as trm
    glyphs = "place_glyphs" in (getattr(plan, "treatments", None) or {})     # 1.4.0 (R2-TREAT): town glyphs in place of the dots
    if not cfg["on"]:
        if out_taken is not None:
            out_taken.extend(taken)
        return "", stats
    den = plan_scale_denominator(frame, plan)
    tier = mp.scale_tier(den)
    stats.update({"tier": tier["tier"], "denominator": round(den)})
    pt = float(fonts["label"])
    floor = lg.MIN_LABEL_PT["print" if tpl["kind"] == "print" else "slide"]
    on_line = data_line_hit(plan, frame)                    # 1.0.0 (TODO M10): place and state names keep off the map's own lines
    map_rect = lo.rect_of(tpl["map"])
    ground = t["land"] if plan.basemap else t["background"]
    window = mp.context_window(plan)
    out: list[str] = []
    taken = list(taken)
    import map_focus
    in_focus = map_focus.inside(getattr(plan, "focus_sheet", None))

    out_col = map_focus.outside_color(t)
    fill_at = map_focus.fill_lookup(plan, frame, to_sheet) if getattr(plan, "look", "classic") != "classic" else (lambda x, y: None)

    def paint(col: str, x: float, y: float) -> tuple[str, str]:
        """(text colour, halo colour) for a name at (x, y). On a class fill (0.18.0): the brand's dark or light label colour, whichever reads
        better on that fill, with a halo in the fill's own colour, so the halo still breaks the boundaries under the letters but no white
        sticker sits on a dark class. Elsewhere: the colour asked for (or the outside-the-study-area neutral) on the usual ground halo."""
        f_ = fill_at(x, y)
        if f_ is None:
            return fc(col, x, y), ground
        return cf.text_on(f_, t["label"], t["background"]), f_

    def fc(col: str, x: float, y: float) -> str:
        # 0.18.0: a name outside the study area steps back with the land it names: the brand's darkest neutral (still 3:1 on the land),
        # not a see-through group (WeasyPrint clips centred and end-anchored text inside a group with opacity)
        return col if in_focus(x, y) else out_col
    counts = {"places": 0, "water": 0, "admin": 0}

    def load(key):
        try:
            gdf, _e = mp.load_ref_layer(mp.NAMES_REFS[key])
        except mp.PlanError as ex:
            plan.notes.append(f"names: {key} not drawn ({ex})")
            return None
        return mp.window_reproject(gdf, window, plan.crs)

    # ---- places
    pstats = {"candidates": 0, "placed": 0, "dropped_collision": 0, "dropped_size": 0, "dropped_outside": 0, "under_symbol": 0, "tier1": 0, "tier1_dropped": 0}
    places = load("places") if cfg["places"] != "none" else None
    if places is not None and len(places):
        sel = select_places(places, cfg, tier)
        sel_names, sel_alt, sel_left = mp.choose_place_names(sel, cfg) if len(sel) else ([], [], set())
        if sel_left:
            stats["script_fallback"] = sorted(sel_left)
        mx, my, vis = sheet_points(sel, frame)
        groups: dict[str, list[le.Candidate]] = {k: [] for k in TIER_SIZE}
        symbols_index = le.RectIndex(point_obstacles)               # M16: each place is tested against the symbols near it, not all of them
        for i in np.where(vis)[0]:
            x, y = float(mx[i]), float(my[i])
            near = Rect(x - POINT_CLEARANCE_MM, y - POINT_CLEARANCE_MM, x + POINT_CLEARANCE_MM, y + POINT_CLEARANCE_MM)
            if symbols_index.hits(near):
                pstats["under_symbol"] += 1
                continue
            tr = str(sel["_tier"].iloc[i])
            text = str(sel_names[i])
            if tr == "capital":
                text = caps(text)
            groups[tr].append(le.Candidate(text, "point", x, y, float(sel["_prio"].iloc[i]), radius=(trm.GLYPH_MM.get(tr, 2.0) / 2 if glyphs else CITY_DOT_MM / 2 + (0.25 if tr == "capital" else 0.0)),
                                           weight=700 if tr in ("capital", "city") else 400, tracking=(pt * PT * 0.06 if tr == "capital" else 0.0),
                                           style={"tier": tr, "cx": x, "cy": y, "alt": sel_alt[i]}))
        for cands in groups.values():                           # every place's dot is kept clear of other names, named or not
            for c in cands:
                taken.append(Rect(c.x - c.radius, c.y - c.radius, c.x + c.radius, c.y + c.radius))
        passes = []
        for tr, cands in groups.items():
            if not cands:
                continue
            size = max(floor, pt + TIER_SIZE[tr])
            if len(cands) > MAX_NAME_CANDIDATES:
                cands = sorted(cands, key=lambda c: -c.priority)[:MAX_NAME_CANDIDATES]
            if getattr(plan, "focus_sheet", None) is not None:
                # 0.22.0 (spec 4.2): names outside the study area one size smaller, placed after the ones inside it, which keep their room
                ins = [c for c in cands if in_focus((c.style or {}).get("cx", c.x), (c.style or {}).get("cy", c.y))]
                outs = [c for c in cands if c not in ins]
                passes += [(tr, ins, size), (tr, outs, max(floor, size - OUTSIDE_NAME_STEP_PT))]
            else:
                passes.append((tr, cands, size))
        passes.sort(key=lambda ps_: ps_[2] < max(floor, pt + TIER_SIZE[ps_[0]]))       # every inside pass first, in tier order
        for tr, cands, size in passes:
            if not cands:
                continue
            res = le.place(cands, map_rect, taken, size, 400, floor, avoid=on_line)
            if res.dropped_lines:
                pstats.setdefault("dropped_for_lines", []).extend(res.dropped_lines)
            pstats["candidates"] += len(cands)
            for k in ("dropped_collision", "dropped_size", "dropped_outside"):
                pstats[k] += getattr(res, k)
            if tr in ("capital", "city"):
                pstats["tier1"] += len(cands)
                pstats["tier1_dropped"] += res.dropped_collision
            for p in res.placed:
                cx, cy = (p.style or {}).get("cx", p.x), (p.style or {}).get("cy", p.y)
                if glyphs:                                     # 1.4.0 (R2-TREAT): a town glyph in place of the dot
                    out.append(trm.place_glyph(tr, cx, cy, t.get("place_ink") or t["label"], t["background"]))
                elif tr == "capital":
                    out.append(f'<circle cx="{sd.num(cx)}" cy="{sd.num(cy)}" r="{sd.num(CITY_DOT_MM / 2 + 0.25)}" fill="{t["background"]}" stroke="{t["label"]}" stroke-width="0.18"/>')
                    out.append(f'<circle cx="{sd.num(cx)}" cy="{sd.num(cy)}" r="{sd.num(CITY_DOT_MM / 2 - 0.1)}" fill="{t["label"]}"/>')
                else:
                    out.append(f'<circle cx="{sd.num(cx)}" cy="{sd.num(cy)}" r="{sd.num(CITY_DOT_MM / 2)}" fill="{paint(t["label"], cx, cy)[0]}"/>')
                tc_, hc_ = paint(t["label"], p.x, p.y)
                out.append(sd.text(p.x, p.y, p.text, size, tc_, weight=p.weight or 400, anchor=p.anchor, halo=hc_, halo_w=max(0.3, size * PT * 0.2),
                                   tracking=(pt * PT * 0.06 if tr == "capital" else 0.0), extra=f' data-name="place" data-tier="{tr}"'))
                taken.append(p.rect.grown(le.PAD_MM))
                alt_ = (p.style or {}).get("alt")
                if alt_:                                       # 1.5.0: names.second_line, a smaller line under the name
                    s2 = max(floor, size - 1.5)
                    y2 = p.y + s2 * PT * 1.15
                    w2 = tm.width_mm(alt_, s2)
                    x2a = p.x - (w2 / 2 if p.anchor == "middle" else w2 if p.anchor == "end" else 0.0)
                    r2 = Rect(x2a, y2 - s2 * PT * 0.8, x2a + w2, y2 + s2 * PT * 0.2)
                    if not any(r2.hits(q) for q in taken):
                        out.append(sd.text(p.x, y2, alt_, s2, tc_, anchor=p.anchor, halo=hc_, halo_w=max(0.3, s2 * PT * 0.2),
                                           extra=' data-name="place-second" data-tier="second"'))
                        taken.append(r2.grown(le.PAD_MM))
                counts["places"] += 1
        pstats["placed"] = counts["places"]
        dl_ = pstats.get("dropped_for_lines") or []
        if dl_:                                           # 1.2.0 (Day 9 issue 6): say which names the map's own lines kept out, never silently
            shown = ", ".join(dl_[:6]) + (f" and {len(dl_) - 6} more" if len(dl_) > 6 else "")
            plan.notes.append(f"names: {len(dl_)} place name(s) left out because every free position crossed one of the map's own lines ({shown}); "
                              "draw that layer as outline polygons, or label the places with a labels: entry")
    stats["places"] = pstats

    # ---- water
    wstats = {"candidates": 0, "placed": 0, "dropped_collision": 0, "dropped_size": 0, "dropped_outside": 0}
    if cfg["water"]:
        wcol = water_label_color(t)
        wsize = max(floor, pt - 0.5)
        cands: list[le.Candidate] = []
        lakes = load("lakes")
        if lakes is not None and len(lakes):
            if tier["water"] is not None:
                lakes = lakes[lakes["scalerank"].astype(float) <= tier["water"]]
            lakes = lakes[lakes["name"].notna()]
            if len(lakes):
                geoms, keep = to_sheet(lakes, frame, True)
                for i in np.where(keep)[0]:
                    g = geoms[i]
                    if g is None or g.is_empty or g.area < 4.0:            # a lake smaller than 2 x 2 mm is a speck; its name would hide it
                        continue
                    p = shapely.point_on_surface(g)
                    minx, _, maxx, _ = g.bounds
                    cands.append(le.Candidate(str(lakes["name"].iloc[i]), "area", float(p.x), float(p.y), 1e6 - float(lakes["scalerank"].iloc[i]) * 1e4 + g.area,
                                              avail_w=maxx - minx, style={"tier": "water", "kind": "lake"}))
        rivers = load("rivers")
        if rivers is not None and len(rivers):
            if tier["water"] is not None:
                rivers = rivers[rivers["scalerank"].astype(float) <= tier["water"]]
            rivers = rivers[rivers["name"].notna() & (rivers["featurecla"].astype(str) != "Lake Centerline")]
            if len(rivers):
                geoms, keep = to_sheet(rivers, frame, False)
                for i in np.where(keep)[0]:
                    g = geoms[i]
                    if g is None or g.is_empty:
                        continue
                    ln = max(g.geoms, key=lambda q: q.length) if g.geom_type == "MultiLineString" else g
                    if ln.length < 18.0:                                   # a river shorter than its likely name on the sheet is left unnamed
                        continue
                    p = ln.interpolate(0.5, normalized=True)
                    cands.append(le.Candidate(str(rivers["name"].iloc[i]), "area", float(p.x), float(p.y), 5e5 - float(rivers["scalerank"].iloc[i]) * 1e4 + ln.length,
                                              avail_w=ln.length * 0.9, style={"tier": "water", "kind": "river"}))
        if cands:
            res = le.place(cands, map_rect, taken, wsize, 400, floor)
            wstats.update(res.as_dict() | {"candidates": len(cands)})
            for p in res.placed:
                tc_, hc_ = paint(wcol, p.x, p.y)
                out.append(sd.text(p.x, p.y, p.text, wsize, tc_, anchor=p.anchor, halo=hc_, halo_w=0.3, slant=WATER_SLANT_DEG,
                                   extra=' data-name="water" data-tier="water"'))
                taken.append(p.rect.grown(le.PAD_MM))
            counts["water"] = len(res.placed)
    stats["water"] = wstats

    # ---- admin (state and province names)
    astats = {"candidates": 0, "placed": 0, "dropped_collision": 0, "dropped_size": 0, "dropped_outside": 0}
    if cfg["admin"] and tier["tier"] != "world":
        wgs = mp._wgs84_extent(plan)
        in_us = -180 <= wgs[0] and wgs[2] <= -60 and 15 <= wgs[1] and wgs[3] <= 72
        adm = load("states" if in_us else "admin1")
        name_col = "NAME" if in_us else "name"
        if adm is not None and len(adm) and name_col in adm.columns:
            acol, aop = admin_label_color(t)
            asize = max(floor, pt)
            track = asize * PT * 0.16
            frame_box = sbox(frame.rect.x0, frame.rect.y0, frame.rect.x1, frame.rect.y1)
            farea = frame.rect.w * frame.rect.h
            geoms, keep = to_sheet(adm, frame, True)
            data_poly = data_polygons_on_sheet(plan, frame)
            cands = []
            for i in np.where(keep)[0]:
                g = geoms[i]
                if g is None or g.is_empty:
                    continue
                inside = g.intersection(frame_box)
                share = inside.area / farea if farea else 0.0
                if share < ADMIN_MIN_FRAME_SHARE or share > ADMIN_WHOLE_FRAME_SHARE:
                    continue
                if data_poly is not None:                      # the name goes on the state's land that the data does not fill, when there is enough of it
                    free = inside.difference(data_poly)
                    if not free.is_empty and free.area / farea >= ADMIN_MIN_FRAME_SHARE / 2:
                        inside = free
                p = shapely.point_on_surface(inside)
                minx, _, maxx, _ = inside.bounds
                cands.append(le.Candidate(str(adm[name_col].iloc[i]).upper(), "area", float(p.x), float(p.y), share, avail_w=maxx - minx,
                                          weight=500, tracking=track, style={"tier": "admin"}))
            if len(cands) < 2 and tier["tier"] in ("local", "metro"):
                cands = []                                    # a city map inside one state does not say which state it is in; the title and the locator do
            if cands:
                res = le.place(cands, map_rect, taken, asize, 500, floor, avoid=on_line)
                astats.update(res.as_dict() | {"candidates": len(cands)})
                for p in res.placed:
                    tc_, hc_ = paint(acol, p.x, p.y)
                    out.append(sd.text(p.x, p.y, p.text, asize, tc_, weight=500, anchor=p.anchor, halo=hc_, halo_w=0.35, tracking=track,
                                       extra=' data-name="admin" data-tier="admin"' + (f' fill-opacity="{sd.num(aop)}"' if aop < 1 else "")))
                    taken.append(p.rect.grown(le.PAD_MM))
                counts["admin"] = len(res.placed)
    stats["admin"] = astats
    stats["counts"] = counts
    plan.notes.append(f"{counts['places']} place names, {counts['water']} water names, {counts['admin']} admin names "
                      f"({tier['tier']} tier at 1:{den:,.0f})")
    if out_taken is not None:                     # what the names took, for the labels placed after them (contour elevations, M12)
        out_taken.extend(taken)
    return '<g class="gm-names">' + "".join(out) + "</g>", stats


def data_polygons_on_sheet(plan: mp.Plan, frame: lo.Frame):
    """The union of the data's filled polygons (choropleth, hexbin, grid and polygon layers) in sheet millimetres, or None; admin names stay off it."""
    import shapely
    parts = []
    for l in plan.layers:
        if l.kind != "polygon" or l.role not in (*mp.AREA_ROLES, "polygons") or not len(l.gdf):
            continue
        geoms, keep = to_sheet(l.gdf, frame, True)
        parts += [geoms[i] for i in np.where(keep)[0] if geoms[i] is not None]
    if not parts:
        return None
    if len(parts) > LARGE_LAYER:
        return footprint_grid(np.asarray(parts, dtype=object), frame)
    try:
        u = shapely.unary_union(parts)
        return u if not u.is_empty else None
    except Exception:  # noqa: BLE001 - an invalid union only costs the avoidance
        return None


FOOTPRINT_CELL_MM = 2.0


def footprint_grid(geoms, frame: lo.Frame):
    """Where many polygons lie on the sheet, as the union of the FOOTPRINT_CELL_MM cells any of them touches (M16). The exact union of
    100,000 cells takes longer than the rest of the map; the names that avoid the data need only to know where it is, to a couple of mm."""
    import shapely
    c = FOOTPRINT_CELL_MM
    r = frame.rect
    nx, ny = max(1, int(np.ceil(r.w / c))), max(1, int(np.ceil(r.h / c)))
    xs = r.x0 + np.arange(nx + 1) * c                  # one array of edges, so neighbouring cells share theirs exactly (a clean coverage)
    ys = r.y0 + np.arange(ny + 1) * c
    gx, gy = np.meshgrid(np.arange(nx), np.arange(ny))
    gx, gy = gx.ravel(), gy.ravel()
    cells = shapely.box(xs[gx], ys[gy], xs[gx + 1], ys[gy + 1])
    hit = np.unique(shapely.STRtree(cells).query(geoms, predicate="intersects")[1])
    if not len(hit):
        return None
    try:
        u = shapely.coverage_union_all(cells[hit]) if hasattr(shapely, "coverage_union_all") else shapely.union_all(cells[hit])
    except Exception:  # noqa: BLE001
        u = shapely.union_all(cells[hit])
    return u if not u.is_empty else None


def plan_scale_denominator(frame: lo.Frame, plan: mp.Plan) -> float:
    """The scale denominator of the frame as it will be drawn (the one the scale bar states)."""
    try:
        den = sb.scale_info(plan.crs, frame.visible, frame.rect.w).get("scale_denominator")
    except Exception:  # noqa: BLE001
        den = None
    return float(den) if den else mp.NE_ZOOM0_DENOMINATOR


# ---------------------------------------------------------------------------------------------------------------- inset
INSET_MAX_ZOOM_OUT = 5.0     # default: the locator shows at most 5 times the main map's extent (on its longer side)
INSET_BOX_SHARE_HIDE = 0.25  # auto: hide the locator when the main map would fill more than this share of its area


def _point_rotations(plan, l, gdf) -> np.ndarray:
    """Sheet angles (degrees clockwise from up) for `symbol.rotate_field`, a compass bearing (degrees clockwise from true north) per point, or NaN.
    The bearing is turned into the map's own grid by projecting a point 50 m ahead, so it is right on any projection (0.11.0)."""
    from pyproj import Transformer
    fld = l.symbol.get("rotate_field")
    out = np.full(len(gdf), np.nan)
    if fld not in gdf.columns or not len(gdf):
        return out
    b = pd_numeric(gdf[fld])
    ok = np.isfinite(b)
    if not ok.any():
        return out
    xs, ys = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
    to_ll = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True)
    to_map = Transformer.from_crs("EPSG:4326", plan.crs, always_xy=True)
    lon, lat = to_ll.transform(xs[ok], ys[ok])
    br = np.radians(b[ok])
    dlat = 50.0 * np.cos(br) / 111_320.0
    dlon = 50.0 * np.sin(br) / (111_320.0 * np.maximum(np.cos(np.radians(lat)), 1e-6))
    fx, fy = to_map.transform(np.asarray(lon) + dlon, np.asarray(lat) + dlat)
    out[ok] = np.degrees(np.arctan2(np.asarray(fx) - xs[ok], np.asarray(fy) - ys[ok]))      # map y is up, so this is clockwise from up on the sheet
    return out


def pd_numeric(series) -> np.ndarray:
    import pandas as pd
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)


def _parallel_lines(plan, l, frame, rep):
    """`symbol: {parallel: true}` on a categorical lines layer: lines that share a path drawn side by side, one line width apart (0.11.0,
    scripts/parallel_lines.py). Computed for this frame's scale; the layer's own geometry (web map, data outputs) is unchanged."""
    import parallel_lines as pl
    if l.cls is None or not l.value_field or l.value_field not in l.gdf.columns:
        return l.gdf                                              # the plan raised MAP-CART-023
    key = (round(frame.scale, 9),)
    cache = getattr(l, "_parallel_cache", None)
    if cache and cache[0] == key:
        return cache[1]
    assigned = np.asarray(l.cls.assigned)
    vals = l.gdf[l.value_field].tolist()
    order = []
    for ci in range(l.cls.k):                                    # the category order of the classes, which is the legend's order
        idx = np.where(assigned == ci)[0]
        if len(idx):
            order.append(vals[int(idx[0])])
    gap_mm = float(l.symbol.get("parallel_gap", 0.0)) * 0.3528
    tol = l.symbol.get("parallel_tolerance")
    out, info = pl.offset(l.gdf, l.value_field, order, 1.0 / frame.scale, float(l.stroke_w_mm), gap_mm, float(tol) if tol is not None else None)
    l._parallel_cache = (key, out)
    rep.setdefault("parallel_lines", {})[l.id] = info
    if info["max_side_by_side"] > 1:
        l.notes.append(f"lines sharing a path are drawn side by side ({info['max_side_by_side']} at most, {info['spacing_mm']:g} mm apart); the data keeps the true paths")
    return out


def inset_window(box: tuple, bounds: tuple, aspect: float, factor: float) -> tuple:
    """The window (xmin, ymin, xmax, ymax) the locator shows, in projected units.

    Centred on the main-map box `box`, with the locator's aspect, zoomed out `factor` times on the box's more constrained side (so the window is at most `factor` times the main map's extent on its longer side;
    a very wide or tall map is only ever given the margin it needs to fit the locator's shape). When it runs off the context `bounds` it slides back inside; a window larger than the context shows the context.
    """
    bx0, by0, bx1, by1 = box
    mw, mh = max(bx1 - bx0, 1e-9), max(by1 - by0, 1e-9)
    r = mh * aspect / mw
    k = max(1.25, factor / max(r, 1.0 / r))
    ww = k * max(mw, mh * aspect)
    wh = ww / aspect
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    x0, y0, x1, y1 = bounds
    if ww >= x1 - x0 or wh >= y1 - y0:
        ww = min(ww, (y1 - y0) * aspect)
        ww = min(ww, x1 - x0)
        wh = ww / aspect
    cx = min(max(cx, x0 + ww / 2), x1 - ww / 2)
    cy = min(max(cy, y0 + wh / 2), y1 - wh / 2)
    return (cx - ww / 2, cy - wh / 2, cx + ww / 2, cy + wh / 2)


def _window_quad(visible: tuple, src_crs: str, dst_crs: str, n: int = 24):
    """The main map's visible rectangle (in its own projection) as a densified outline in the locator's projection.

    The rectangle is generally not a rectangle in another projection, so it is sampled along its four edges and drawn as the outline it really is."""
    from pyproj import Transformer
    x0, y0, x1, y1 = (float(v) for v in visible)
    t = np.linspace(0.0, 1.0, n, endpoint=False)
    ex = np.concatenate([x0 + (x1 - x0) * t, np.full(n, x1), x1 - (x1 - x0) * t, np.full(n, x0)])
    ey = np.concatenate([np.full(n, y0), y0 + (y1 - y0) * t, np.full(n, y1), y1 - (y1 - y0) * t])
    px, py = Transformer.from_crs(src_crs, dst_crs, always_xy=True).transform(ex, ey)
    px, py = np.asarray(px, dtype="float64"), np.asarray(py, dtype="float64")
    ok = np.isfinite(px) & np.isfinite(py)
    return px[ok], py[ok]


def inset_box_path(inset: dict, visible: tuple) -> str:
    """Path data (sheet mm, inside the locator) for the outline of the main map's window `visible`.

    `visible` is the final frame's window, so the outline has the main map's real shape (a wide map gives a wide box) and is cut at the
    locator's edge, so it can never leave it."""
    from shapely.geometry import Polygon, box as sbox
    px, py = _window_quad(visible, inset["main_crs"], inset["crs"])
    if len(px) < 3:
        return ""
    mx, my = inset["frame"].to_mm(px, py)
    poly = Polygon(np.column_stack([mx, my]))
    if not poly.is_valid:
        poly = poly.buffer(0)
    w, h = inset["w"], inset["h"]
    x0, y0, x1, y1 = poly.bounds
    if (x1 - x0) < 1.2 or (y1 - y0) < 1.2:                 # a tiny map still gets a box that can be seen
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = max((x1 - x0) / 2, 0.6), max((y1 - y0) / 2, 0.6)
        poly = sbox(cx - hw, cy - hh, cx + hw, cy + hh)
    poly = poly.intersection(sbox(0, 0, w, h))
    return sd.polygon_path(poly) if not poly.is_empty else ""


INSET_STATE_SHARE = 0.5      # a main map inside one US state that fills less than this share of it is located on that state's outline
INSET_STATE_PAD = 0.08       # the state outline gets this margin (share of its longer side) inside the locator
INSET_STATE_IN = 0.95        # the main window counts as inside a state when this share of its area lies in that state


def inset_projection(ctx: str) -> tuple[str, str]:
    """(fallback CRS, BASEMAP_REFS key) for a locator context: EPSG:9311 (US National Atlas Equal Area, the current code for what EPSG:2163 (deprecated) described)
    for the US, Equal Earth for the world. Since 0.11.0 the locator is drawn in matched_north_crs, centred on the map; these are used only when the
    main map's north cannot be measured (a geographic main map, or PROJ cannot give the convergence)."""
    if ctx == "us_states":
        return "EPSG:9311", "states5m"
    if ctx == "admin1":
        return "EPSG:8857", "admin1_loc"              # 1.5.0: the admin-1 locator anywhere (Natural Earth 10m, simplified)             # 0.11.0: the 1:5,000,000 states, so sounds, bays and islands read at inset size
    return "EPSG:8857", "countries"


def _convergence(crs, lon: float, lat: float) -> float | None:
    """PROJ's meridian convergence (degrees) of `crs` at lon, lat, or None when it cannot be had (a geographic CRS has none: north is up)."""
    from pyproj import CRS, Proj
    c = CRS.from_user_input(crs)
    if c.is_geographic:
        return 0.0
    try:
        v = float(Proj(c).get_factors(lon, lat).meridian_convergence)
    except Exception:  # noqa: BLE001 - a projection PROJ cannot give factors for: fall back to the fixed locator projection
        return None
    return v if math.isfinite(v) else None


def matched_north_crs(main_crs, lon: float, lat: float) -> tuple[str, float, float] | None:
    """A Lambert azimuthal equal-area projection centred on the map's latitude whose meridian convergence at lon, lat equals the main map's, so north
    on the locator points the same way as north on the main map (0.11.0). Found by bisection on the central meridian. -> (proj string, main
    convergence, locator convergence) or None."""
    target = _convergence(main_crs, lon, lat)
    if target is None:
        return None

    def crs_for(lon0: float) -> str:
        return f"+proj=laea +lat_0={lat:.6f} +lon_0={lon0:.6f} +datum=WGS84 +units=m +no_defs"

    def f(lon0: float) -> float:
        return (_convergence(crs_for(lon0), lon, lat) or 0.0) - target
    if abs(target) < 1e-6:
        return crs_for(lon), target, 0.0
    lo_, hi_ = lon - 89.0, lon + 89.0
    flo, fhi = f(lo_), f(hi_)
    if flo * fhi > 0:
        return None
    for _ in range(60):
        mid = (lo_ + hi_) / 2
        fm = f(mid)
        if abs(fm) < 1e-7:
            break
        if flo * fm <= 0:
            hi_, fhi = mid, fm
        else:
            lo_, flo = mid, fm
    best = (lo_ + hi_) / 2
    return crs_for(best), target, (_convergence(crs_for(best), lon, lat) or 0.0)


def aspect_box(bounds: tuple, aspect: float, pad: float) -> tuple:
    """`bounds` grown by `pad` (share of the longer side) and widened or heightened to `aspect` (w / h) about its centre."""
    x0, y0, x1, y1 = (float(v) for v in bounds)
    p = max(x1 - x0, y1 - y0) * pad
    x0, y0, x1, y1 = x0 - p, y0 - p, x1 + p, y1 + p
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if w / max(h, 1e-9) < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _state_around(states, window_poly, name_col: str = "NAME"):
    """The one state that holds the main map's window (INSET_STATE_IN of the window's land, so a coastal map with sea in its frame still
    counts as inside its state), or None."""
    if window_poly.is_empty or window_poly.area <= 0:
        return None
    cand = states[states.geometry.intersects(window_poly)]
    if not len(cand):
        return None
    parts = [g.intersection(window_poly).area for g in cand.geometry]
    land = sum(parts)
    if land <= 0:
        return None
    for i, a in enumerate(parts):
        if a / land >= INSET_STATE_IN:
            return cand.iloc[i]
    return None


def build_inset(plan: mp.Plan, cfg: dict, tpl: dict, force: bool = False):
    """The locator: the context layer around the main map, zoomed out at most `max_zoom_out` (default 5) times from it.

    The box it draws is the main map's whole visible window (the frame rectangle after fitting), not the extent of the data: a wide or
    tall main map therefore gets a wide or tall box, in the same shape as the map frame.

    A city or regional map that lies inside one US state (and fills less than half of it) is located on that state instead: the locator shows
    the whole state outline with its neighbours, the state is outlined more strongly and named in small capitals when there is room, and the
    main map's box sits inside it. That reads as 'this part of Arizona', where the zoomed-out rectangle read as a clipped blank."""
    from shapely.geometry import Polygon
    wgs = mp._wgs84_extent(plan)
    cx, cy = (wgs[0] + wgs[2]) / 2, (wgs[1] + wgs[3]) / 2
    asked = cfg.get("extent", "auto")
    ctx = locator_context(plan, asked, wgs)            # 1.5.0 (R3-LOC): auto, the default, chooses the locator for anywhere
    crs, ref_key = inset_projection(ctx)
    cb = mp.PRESET_BBOX["us" if ctx == "us_states" else "world"]
    refs = [mp.BASEMAP_REFS[ref_key]]
    if not (cb[0] <= cx <= cb[2] and cb[1] <= cy <= cb[3]):
        return None
    unit_like = ctx in ("us_states", "admin1")         # a locator that outlines the one unit holding the map
    name_col = "NAME" if ctx == "us_states" else "name"
    # Rule (0.12.0): the locator's north is the main map's north. It is drawn in the main map's own projection, so north is the same grid
    # direction on both everywhere, not only at the centre; a projection that cannot draw the locator's area (non-finite coordinates) falls back
    # to an azimuthal projection whose convergence at the map's centre is matched to the main map's (0.11.0). MAP-CART-025 checks the result.
    north = None
    if cfg.get("match_north", True) is not False:
        mc = _convergence(plan.crs, cx, cy)
        if mc is not None and _locator_ok(plan.crs, cx, cy):
            crs, north = plan.crs, (plan.crs, mc, mc)
        else:
            north = matched_north_crs(plan.crs, cx, cy)
            if north is not None:
                crs = north[0]
    factor = float(cfg.get("max_zoom_out", INSET_MAX_ZOOM_OUT))
    if factor < 1.5:
        factor = 1.5
    w, h = tpl["inset_size"]["w"], tpl["inset_size"]["h"]
    pad_pct = float((plan.spec.get("extent") or {}).get("padding_pct", 5))
    main_vis = lo.Frame.fit(lo.rect_of(tpl["map"]), plan.extent, pad_pct).visible     # the window the main map frame will show
    srcs = [mp.load_ref_layer(r)[0] for r in refs]                     # 0.23.0: kept, so a regional locator can be cut wider
    gdfs = [_locator_layer(g_, crs, cx, cy) for g_ in srcs]
    water_src = None
    water_gdf = None
    if ctx == "admin1":
        # 1.5.0: neighbours outside the bundled admin-1 are land (the 1:110m countries); the 1:10m ocean is the water, the ground provider at
        # the locator's scale
        try:
            c0 = mp.load_ref_layer(mp.BASEMAP_REFS["countries"])[0]
            srcs.append(c0)
            gdfs.append(_locator_layer(c0, crs, cx, cy))
            water_src = mp.load_ref_layer(mp.BASEMAP_REFS["ocean"])[0]
            water_gdf = _locator_layer(water_src, crs, cx, cy)
        except Exception:  # noqa: BLE001
            water_gdf = None
    if ctx == "us_states" and getattr(plan, "look", "classic") != "classic":
        # 0.19.0 (Day 7 issue 26): Canada and Mexico are land (the 1:110m countries and the 1:50m admin-1 outside the US), and the 1:10m ocean, the main map's own
        # ground at that scale, is laid over the states so sounds and straits (Puget Sound, the Salish Sea) read as water
        try:
            c0 = mp.load_ref_layer(mp.BASEMAP_REFS["countries"])[0]             # every neighbour is land (the 1:50m admin-1 has no Mexico)
            srcs.append(c0[c0["ADM0_A3"] != "USA"])
            gdfs.append(_locator_layer(srcs[-1], crs, cx, cy))
            a1 = mp.load_ref_layer(mp.BASEMAP_REFS["admin1"])[0]
            srcs.append(a1[a1["adm0_a3"] != "USA"])
            gdfs.append(_locator_layer(srcs[-1], crs, cx, cy))
            water_src = mp.load_ref_layer(mp.BASEMAP_REFS["ocean"])[0]
            water_gdf = _locator_layer(water_src, crs, cx, cy)
            # the main map's own detailed water (TIGER, cached by ground_detail prepare for a city or metro map) is the finer cut round the box
            tw = [b["gdf"] for b in plan.basemap if b.get("kind") == "lake" and b.get("gdf") is not None and len(b["gdf"])
                  and str((b.get("entry") or {}).get("id") or "").startswith("ref.context.us.water-area")]
            if tw:
                import pandas as pd
                water_gdf = gpd_concat([water_gdf, tw[0].to_crs(crs)])
        except Exception:  # noqa: BLE001 - the locator keeps its 0.18 land when a bundled layer cannot be read
            water_gdf = None
    px, py = _window_quad(main_vis, plan.crs, crs)
    if len(px) < 3:
        return None
    mbox = (float(px.min()), float(py.min()), float(px.max()), float(py.max()))
    state = None
    if unit_like and cfg.get("state_outline", True) is not False:
        wpoly = Polygon(np.column_stack([px, py]))
        wpoly = wpoly if wpoly.is_valid else wpoly.buffer(0)
        st = _state_around(gdfs[0], wpoly, name_col)
        if st is not None:
            sb_ = st.geometry.bounds
            if wpoly.area / max(st.geometry.area, 1e-9) < INSET_STATE_SHARE:
                state = {"name": str(st.get(name_col, "")), "geometry": st.geometry, "bounds": tuple(float(v) for v in sb_),
                         "iso": str(st.get("iso_a2", "US") if ctx == "admin1" else "US"), "col": name_col, "value": str(st.get(name_col, ""))}
        if state is None and ctx == "admin1":
            # 1.5.0: larger than one unit but inside one country: the country locator (the country with its neighbours, the country outlined)
            import geo_locale as gl
            iso = gl.plan_country(plan)
            a1 = gdfs[0]
            mine = a1[a1["iso_a2"] == iso] if iso and "iso_a2" in a1.columns else a1.iloc[0:0]
            if len(mine):
                from pyproj import CRS as _C
                bf = 0.008 if _C.from_user_input(crs).is_geographic else 800.0
                cg = mine.geometry.buffer(bf).union_all().buffer(-bf)     # units simplified apart leave slivers between them: closed first
                import shapely as _shp                  # 1.10.0: unit by unit (a reprojected unit can be invalid, and merging the whole table then fails)
                land = float(_shp.area(_shp.intersection(_shp.make_valid(np.asarray(a1.geometry.values, dtype=object)), wpoly)).sum())
                if land > 0 and wpoly.intersection(cg).area / land >= INSET_STATE_IN and wpoly.area / max(cg.area, 1e-9) < INSET_STATE_SHARE:
                    nm = (gl.country(iso) or {}).get("name", iso)
                    state = {"name": nm, "geometry": cg, "bounds": tuple(float(v) for v in cg.bounds), "iso": iso, "col": "iso_a2", "value": iso,
                             "country": True}
    win = None
    if state:
        win = aspect_box(state["bounds"], w / h, INSET_STATE_PAD)
        cbnd = tuple(float(v) for v in gdfs[0].total_bounds)
        win = inset_window(win, cbnd, w / h, 1.0) if (win[2] - win[0] > cbnd[2] - cbnd[0]) else win
        zo = max((win[2] - win[0]) / max(mbox[2] - mbox[0], 1e-9), (win[3] - win[1]) / max(mbox[3] - mbox[1], 1e-9))
        if "max_zoom_out" in cfg and zo > factor + 1e-6:       # a limit the spec sets wins over the state outline
            state, win = None, None
    if win is None:
        win = inset_window(mbox, tuple(float(v) for v in gdfs[0].total_bounds), w / h, factor)
    share = ((mbox[2] - mbox[0]) * (mbox[3] - mbox[1])) / max((win[2] - win[0]) * (win[3] - win[1]), 1e-9)
    zoomed_out = None
    if (not force and share > INSET_BOX_SHARE_HIDE and ctx == "us_states" and "max_zoom_out" not in cfg and len(gdfs) > 1
            and cfg.get("zoom_out_regional", True) is not False):
        # 0.23.0 (owner's decision, 2026-11-08): a multi-state regional map keeps its locator. Zoomed out further, onto North America (the
        # neighbours are land when the look is not classic), until the box is small enough to read; without the neighbours it is hidden as before.
        import pandas as pd
        for span in (None, LOCATOR_SPAN_REGIONAL):
            if span is not None and not _locator_ok(crs, cx, cy, span):
                break
            cand = gdfs if span is None else [_locator_layer(g_, crs, cx, cy, span=span) for g_ in srcs]
            wide = tuple(float(v) for v in pd.concat([g.geometry for g in cand if len(g)]).total_bounds)
            for f in (factor * 1.5, factor * 2.0, factor * 3.0, factor * 4.0):
                w2 = inset_window(mbox, wide, w / h, f)
                s2 = ((mbox[2] - mbox[0]) * (mbox[3] - mbox[1])) / max((w2[2] - w2[0]) * (w2[3] - w2[1]), 1e-9)
                if s2 <= INSET_BOX_SHARE_HIDE:
                    win, share, zoomed_out = w2, s2, round(f, 1)
                    break
            if zoomed_out:
                if span is not None:
                    gdfs = cand
                    if water_src is not None:
                        water_gdf = _locator_layer(water_src, crs, cx, cy, span=span)
                break
    if not force and share > INSET_BOX_SHARE_HIDE:
        return None
    fr = lo.Frame.fit(Rect(0, 0, w, h), win, 0.0)
    paths, focus, label = [], [], None
    for g in gdfs:
        near = g.cx[win[0]:win[2], win[1]:win[3]]
        geoms, keep = to_sheet(near, fr, True)
        for i in np.where(keep)[0]:
            d = sd.polygon_path(geoms[i])
            if not d:
                continue
            if (state and not state.get("country") and state.get("col", name_col) in near.columns
                    and str(near[state.get("col", name_col)].iloc[i]) == state.get("value", state["name"])):
                focus.append(d)
            else:
                paths.append(d)
    if state and state.get("country"):                 # the country as one outline over its units
        cgeom, ck = to_sheet([state["geometry"]], fr, True)
        if ck[0] and cgeom[0] is not None:
            d = sd.polygon_path(cgeom[0])
            if d:
                focus.append(d)
    water = []
    if water_gdf is not None and len(water_gdf):
        near = water_gdf.cx[win[0]:win[2], win[1]:win[3]]
        geoms, keep = to_sheet(near, fr, True)
        water = [d for d in (sd.polygon_path(geoms[i]) for i in np.where(keep)[0]) if d]
    if state:
        import shapely
        from shapely.geometry import box as sbox
        sgeom, _ = to_sheet([state["geometry"]], fr, True)
        if sgeom[0] is not None:
            p = shapely.point_on_surface(sgeom[0].intersection(sbox(0, 0, w, h)))
            label = {"text": state["name"].upper(), "x": float(p.x), "y": float(p.y)}
    out = {"w": w, "h": h, "paths": paths, "focus": focus, "water": water, "label": label, "state": state["name"] if state else None,
           "context": ctx, "asked": asked,
           "frame": fr, "crs": crs, "main_crs": plan.crs, "main_visible": main_vis,
           "north": {"matched": north is not None, "same_projection": bool(north is not None and crs == plan.crs),
                     "main_convergence": round(north[1], 4) if north else None,
                     "inset_convergence": round(_convergence(crs, cx, cy), 4) if north else None},
           "window": [round(v, 1) for v in win], "max_zoom_out": round(float(zoomed_out or factor), 2), "regional_zoom_out": zoomed_out,
           "zoom_out": round(((win[2] - win[0]) / max(mbox[2] - mbox[0], 1e-9)) if (mbox[2] - mbox[0]) >= (mbox[3] - mbox[1])
                             else ((win[3] - win[1]) / max(mbox[3] - mbox[1], 1e-9)), 2)}
    if state and ctx == "us_states":
        plan.notes.append(f"locator: the map lies inside {state['name']}, so the inset shows the whole state with its neighbours")
    elif state and state.get("country"):
        plan.notes.append(f"locator: the map lies inside {state['name']}, so the inset shows the whole country with its neighbours")
    elif state:
        import geo_locale as gl
        word = gl.admin_word(state["iso"]) or "region"
        cn = (gl.country(state["iso"]) or {}).get("name", state["iso"])
        plan.notes.append(f"locator: the map lies inside {state['name']} (a {word} of {cn}), so the inset shows the whole {word} with its neighbours")
    if zoomed_out:
        plan.notes.append(f"locator: a regional map, so the inset is zoomed out {zoomed_out:g} times onto North America to keep the box small enough to read")
    return out


def locator_context(plan, asked: str, wgs) -> str:
    """The locator's context (1.5.0, R3-LOC). us_states and world are kept as asked; auto (the default) is: the US state locator for a map whose
    centre is in the United States (unchanged); the admin-1 locator for a map whose centre is in another country (it falls back to that country's
    neighbourhood when the map is larger than one unit); the world locator for a map larger than a country or at sea."""
    if asked in ("us_states", "world", "admin1"):
        return asked
    try:
        import geo_locale as gl
        iso = gl.plan_country(plan)
    except Exception:  # noqa: BLE001 - without the bundled table, the 1.4 default
        return "us_states"
    if iso == "US":
        return "us_states"
    if iso is None or (wgs[2] - wgs[0]) > 40 or (wgs[3] - wgs[1]) > 30:
        return "world"
    return "admin1"


LOCATOR_SPAN = (30.0, 25.0)            # degrees of longitude and latitude either side of the map's centre a locator layer is cut to (main CRS)
LOCATOR_SPAN_REGIONAL = (50.0, 32.0)   # 0.23.0: wider, for a regional map's North America locator


def _locator_ok(crs, cx: float, cy: float, span: tuple | None = None) -> bool:
    """Whether the main map's projection draws the locator's neighbourhood (30 x 25 degrees around the centre, or `span`) with finite coordinates."""
    from pyproj import Transformer
    sx, sy = span or LOCATOR_SPAN
    xs = np.linspace(cx - sx, cx + sx, 13)
    ys = np.linspace(max(-89.0, cy - sy), min(89.0, cy + sy), 11)
    X, Y = np.meshgrid(xs, ys)
    try:
        px, py = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(X.ravel(), Y.ravel())
    except Exception:  # noqa: BLE001
        return False
    return bool(np.isfinite(px).all() and np.isfinite(py).all())


def gpd_concat(frames):
    import geopandas as gpd
    import pandas as pd
    fr = [f for f in frames if f is not None and len(f)]
    return gpd.GeoDataFrame(pd.concat([f[["geometry"]] for f in fr], ignore_index=True), crs=fr[0].crs) if fr else None


def _locator_layer(gdf, crs: str, cx: float, cy: float, span: tuple | None = None):
    """A locator layer in `crs`. An azimuthal projection centred on the map cannot show the far side of the earth, and a local projection (the main
    map's own) is wild far from its centre, so a world layer is cut to the area around the map first (the locator shows at most a few times the
    map's extent anyway)."""
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    if not str(crs).startswith("+proj=laea") and not c.is_geographic:
        from shapely.geometry import box
        g = gdf.to_crs("EPSG:4326")
        sx, sy = span or LOCATOR_SPAN
        win = box(max(-180.0, cx - sx), max(-89.0, cy - sy), min(180.0, cx + sx), min(89.0, cy + sy))
        g = g[g.geometry.intersects(win)].copy()
        g["geometry"] = g.geometry.intersection(win)
        return g[~g.geometry.is_empty].to_crs(crs)
    if not str(crs).startswith("+proj=laea"):
        return gdf.to_crs(crs)
    from shapely.geometry import box
    g = gdf.to_crs("EPSG:4326")
    win = box(max(-180.0, cx - 80.0), max(-89.0, cy - 60.0), min(180.0, cx + 80.0), min(89.0, cy + 60.0))
    g = g[g.geometry.intersects(win)].copy()
    g["geometry"] = g.geometry.intersection(win)
    g = g[~g.geometry.is_empty]
    return g.to_crs(crs)


def _inside_outline(paths, rects) -> list[bool]:
    """For each rect, whether it lies inside the outline drawn by `paths` (absolute M/L/Z path data), 0.3 mm in from its edge (M16)."""
    if not paths or not rects:
        return [False] * len(rects)
    import shapely
    rings = []
    for d in paths:
        for part in re.split(r"(?=M)", d):
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", part)]
            if len(nums) >= 6:
                rings.append(shapely.Polygon(list(zip(nums[0::2], nums[1::2]))))
    if not rings:
        return [False] * len(rects)
    rings = [r if r.is_valid else shapely.make_valid(r) for r in rings]      # 1.5.0: a simplified admin-1 outline can self-touch on the sheet
    area = shapely.make_valid(shapely.union_all(rings)).buffer(-0.3)
    return [bool(area.contains(shapely.box(r.x0, r.y0, r.x1, r.y1))) for r in rects]


INSET_HIGHLIGHTS = ("box", "fill", "corners")          # 0.15.0: elements.inset_map.highlight.style
INSET_COLORS = ("emphasis", "danger", "warning", "label")


def draw_inset(inset: dict, x: float, y: float, t: dict, visible: tuple | None = None, label_pt: float = 7.0, highlight: dict | None = None) -> str:
    """The locator. Everything is cut at the locator's own rectangle with a real clipPath (a CSS `clip-path: inset(0)` on a group clips to the
    group's own bounding box, which is no clip at all, and let the context layer spill out of the frame)."""
    w, h = inset["w"], inset["h"]
    cid = "gm-inset-clip"
    out = [f'<g class="gm-inset" role="img" aria-label="Locator map" transform="translate({sd.num(x)} {sd.num(y)})">',
           f'<defs><clipPath id="{cid}"><rect x="0" y="0" width="{sd.num(w)}" height="{sd.num(h)}"/></clipPath></defs>',
           sd.rect(0, 0, w, h, t["water"], "none", 0),
           f'<g clip-path="url(#{cid})">',
           f'<g fill="{t["land"]}" stroke="{t["frame"]}" stroke-width="0.1" fill-rule="evenodd">']
    out += [f'<path d="{d}"/>' for d in inset["paths"]]
    out.append("</g>")
    water = inset.get("water") or []
    if inset.get("focus"):                                              # the state the map lies in: same land, a stronger outline
        out.append(f'<g fill="{t["land"]}" stroke="{"none" if water else t["label"]}" stroke-width="0.28" stroke-linejoin="round" fill-rule="evenodd">')
        out += [f'<path d="{d}"/>' for d in inset["focus"]]
        out.append("</g>")
    if water:                                                           # 0.19.0: sounds and straits over the land, then the state's outline on top
        out.append(f'<g fill="{t["water"]}" stroke="none" fill-rule="evenodd" data-locator="water">')
        out += [f'<path d="{d}"/>' for d in water]
        out.append("</g>")
        if inset.get("focus"):
            out.append(f'<g fill="none" stroke="{t["label"]}" stroke-width="0.28" stroke-linejoin="round">')
            out += [f'<path d="{d}"/>' for d in inset["focus"]]
            out.append("</g>")
    d = inset_box_path(inset, visible or inset["main_visible"])
    box_rect = None
    if d:
        hl = highlight or {}
        style = hl.get("style", "box") if hl.get("style", "box") in INSET_HIGHLIGHTS else "box"
        cname = hl.get("color", "emphasis") if hl.get("color", "emphasis") in INSET_COLORS else "emphasis"
        col = t["status"][cname] if cname in ("danger", "warning") else t[cname]
        if style == "fill":                                         # the window shaded, with a thin outline
            out.append(f'<path d="{d}" fill="{col}" fill-opacity="0.35" stroke="{col}" stroke-width="0.25" stroke-linejoin="miter" data-highlight="fill"/>')
        elif style == "corners":                                    # only the corners of the window, for a map whose window would hide the state
            nums_ = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]
            bx0, by0, bx1, by1 = min(nums_[0::2]), min(nums_[1::2]), max(nums_[0::2]), max(nums_[1::2])
            k = max(0.8, min(bx1 - bx0, by1 - by0) * 0.3)
            cp = (f"M{sd.num(bx0)} {sd.num(by0 + k)}V{sd.num(by0)}H{sd.num(bx0 + k)}M{sd.num(bx1 - k)} {sd.num(by0)}H{sd.num(bx1)}V{sd.num(by0 + k)}"
                  f"M{sd.num(bx1)} {sd.num(by1 - k)}V{sd.num(by1)}H{sd.num(bx1 - k)}M{sd.num(bx0 + k)} {sd.num(by1)}H{sd.num(bx0)}V{sd.num(by1 - k)}")
            out.append(f'<path d="{cp}" fill="none" stroke="{col}" stroke-width="0.6" stroke-linecap="square" data-highlight="corners"/>')
        else:
            out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="0.5" stroke-linejoin="miter"/>')
        nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]
        if len(nums) >= 4:
            box_rect = Rect(min(nums[0::2]), min(nums[1::2]), max(nums[0::2]), max(nums[1::2]))
    lab = inset.get("label")
    if lab:                                                             # the state name in small capitals, when it fits and misses the box
        pt = max(5.5, label_pt - 1.0)
        track = pt * PT * 0.12
        tw = tm.width_mm(lab["text"], pt, 500) + track * max(len(lab["text"]) - 1, 0)
        th = pt * PT
        ok = []
        for ly in [lab["y"]] + [lab["y"] + s_ * th * k for k in (1.6, 3.2, 4.8, 6.4) for s_ in (-1, 1)]:
            r = Rect(lab["x"] - tw / 2, ly - th * 0.8, lab["x"] + tw / 2, ly + th * 0.3)
            if r.x0 < 1.0 or r.x1 > w - 1.0 or r.y0 < 1.0 or r.y1 > h - 1.0:
                continue
            if box_rect and r.hits(box_rect.grown(0.4)):
                continue
            ok.append((ly, r))
        inside = _inside_outline(inset.get("focus"), [r for _, r in ok])      # M16: on a tall frame the box covers the middle of the state;
        pick = next((c for c, i in zip(ok, inside) if i), ok[0] if ok else None)    # a place inside the state beats one on its border
        if pick:
            out.append(sd.text(lab["x"], pick[0], lab["text"], pt, t["label"], weight=500, anchor="middle", halo=t["land"], halo_w=0.45, tracking=track,
                               extra=' data-role="inset-state"'))
    out.append("</g>")
    out.append(sd.rect(0, 0, w, h, "none", t["legend_frame"], 0.25))      # the frame is drawn last, over any edge of the context layer
    out.append("</g>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------- title, footer
TITLE_MAX_FRAC = 0.5          # the in-map title box is at most this share of the map's width (elements.title.max_width_pct sets it, 0.19.0)
TITLE_PAD_MM = 0.5
TITLE_SCALE_IN_MAP = 0.75     # the in-map title is set at this share of the template's title size (it sits on the map, not above it)
TITLE_FLOOR_PT = 11.0
TAGLINE_SLANT_DEG = 12.0      # elements.title.tagline is set as a slanted (oblique) line: Inter has no italic face
LOGO_SCALE_IN_MAP = 0.5       # the in-map logo is this share of the template's logo height
FRAME_LABEL_SCALE = 1.6       # an atlas-by-time page's period (elements.title.frame_label) is this many times the subtitle size (M15)
LOGO_PAD_MM = 0.0


@_role("title")
def title_box(plan: mp.Plan, fonts: dict, map_rect: Rect, findings: list) -> dict | None:
    """The title (and subtitle) as a box for the map's upper-left corner: wrapped lines, their size and the box's width and height."""
    el = (plan.spec.get("elements") or {}).get("title") or {}
    if el.get("on") is False:
        return None
    text = text_lint.title_text(plan.spec)                # 0.21.0: supplied text as written, else the message's first sentence tidied (map text §10)
    sub = el.get("subtitle")
    frac = TITLE_MAX_FRAC
    if el.get("max_width_pct") is not None:              # 0.19.0 (Day 7 issue 27): a wider box keeps a long title on one line
        frac = max(0.2, min(0.9, float(el["max_width_pct"]) / 100.0))
    maxw = map_rect.w * frac - 2 * TITLE_PAD_MM
    size = round(float(fonts["title"]) * TITLE_SCALE_IN_MAP * float(el.get("scale") or 1.0), 1)      # 1.4.0: elements.title.scale
    lines, cut = wrap_lines(text, maxw, size, 700, 3)
    while cut and size > TITLE_FLOOR_PT:
        size -= 1.0
        lines, cut = wrap_lines(text, maxw, size, 700, 3)
    if cut:
        findings.append(f("MAP-SPEC-005", "WARN", "the title is too long for the template and was shortened"))
    sp = float(fonts["subtitle"])
    sl = wrap_lines(sub, maxw, sp, 400, 2)[0] if sub else []
    tag = el.get("tagline")
    tl = wrap_lines(str(tag), maxw, sp, 400, 1)[0] if tag else []
    lean = sp * PT * 0.75 * math.tan(math.radians(TAGLINE_SLANT_DEG))      # the slant pushes the top of the letters right
    fl = str(el["frame_label"]) if el.get("frame_label") else None          # an atlas-by-time page's period, large (M15)
    fpt = round(sp * FRAME_LABEL_SCALE, 1)
    fl_w = max(tm.width_mm(fl, fpt, 700, tnum=True), tm.width_mm(str(el.get("frame_label_reserve") or ""), fpt, 700, tnum=True)) if fl else 0.0
    w = max([tm.width_mm(ln, size, 700) for ln in lines] + [tm.width_mm(ln, sp, 400) for ln in sl] + [tm.width_mm(ln, sp, 400) + lean for ln in tl] + [fl_w]) + 2 * TITLE_PAD_MM
    h = len(lines) * size * PT * 1.15 + (len(sl) + len(tl)) * sp * PT * 1.3 + 2 * TITLE_PAD_MM + (size * PT * 0.1) + (fpt * PT * 1.35 if fl else 0.0)
    box = str(el.get("box") or "plain")                  # 1.4.0 (R2-ORN): elements.title.box
    pad = 0.0
    if box == "cartouche":
        import ornaments
        pad = ornaments.CARTOUCHE_PAD_MM
        w, h = w + 2 * pad, h + 2 * pad
    elif box == "title-block":
        import ornaments
        w, h = ornaments.title_block_size(text, min(size, 13.0))
        size = min(size, 13.0)
    return {"text": text, "lines": lines, "size": size, "sub": sl, "tag": tl, "sub_pt": sp, "w": w, "h": h, "frame": fl, "frame_pt": fpt, "box": box, "pad": pad}


@_role("title")
def draw_title_box(b: dict, x: float, y: float, t: dict, cells: list | None = None) -> str:
    if b.get("box") == "title-block":                    # 1.4.0 (R2-ORN): an engineering drawing block
        import ornaments
        return ornaments.title_block(x, y, b["w"], b["h"], b["text"], b["size"], cells or [], t)
    pre = ""
    if b.get("box") == "cartouche":                      # 1.4.0 (R2-ORN): an ornamental frame round the title (corners keep their size)
        import ornaments
        pre = ornaments.cartouche(x, y, b["w"], b["h"], t)
        x, y = x + b["pad"], y + b["pad"]
    out = ['<g class="gm-title">' + pre]                  # no panel behind it: the text carries a thin halo so it reads over the map
    ty = y + TITLE_PAD_MM + b["size"] * PT * 0.85
    for i, ln in enumerate(b["lines"]):
        out.append(sd.text(x + TITLE_PAD_MM, ty, ln, b["size"], t["label"], weight=700, halo=t["label_halo"], halo_w=max(0.5, b["size"] * PT * 0.12),
                           extra=' id="gm-title-text"' if i == 0 else ""))
        ty += b["size"] * PT * 1.15
    sy = ty - b["size"] * PT * 1.15 + b["sub_pt"] * PT * 1.3
    for ln in b["sub"]:
        out.append(sd.text(x + TITLE_PAD_MM, sy, ln, b["sub_pt"], t["label"], weight=400, halo=t["label_halo"], halo_w=0.5))
        sy += b["sub_pt"] * PT * 1.3
    for ln in b.get("tag") or []:
        out.append(sd.text(x + TITLE_PAD_MM, sy, ln, b["sub_pt"], t["label"], weight=400, halo=t["label_halo"], halo_w=0.5, slant=TAGLINE_SLANT_DEG,
                           extra=' data-role="tagline"'))
        sy += b["sub_pt"] * PT * 1.3
    if b.get("frame"):                                     # the period of an atlas-by-time page (M15)
        fy = sy - b["sub_pt"] * PT * 1.3 + b["frame_pt"] * PT * 1.25
        out.append(sd.text(x + TITLE_PAD_MM, fy, b["frame"], b["frame_pt"], t["label"], weight=700, halo=t["label_halo"], halo_w=0.6, num_cls=True,
                           extra=' data-role="frame-label"'))
    out.append("</g>")
    return "".join(out)


@_role("title")
def draw_title(plan: mp.Plan, tpl: dict, fonts: dict, findings: list) -> tuple[str, str]:
    t = plan.tokens
    spec = plan.spec
    el = (spec.get("elements") or {}).get("title") or {}
    if el.get("on") is False:
        return "", ""
    text = text_lint.title_text(spec)
    sub = el.get("subtitle")
    r = lo.rect_of(tpl["title"])
    size = float(fonts["title"])
    floor = 12.0
    lines, cut = wrap_lines(text, r.w, size, 700, 2)
    while cut and size > floor:
        size -= 1.0
        lines, cut = wrap_lines(text, r.w, size, 700, 2)
    if cut:
        findings.append(f("MAP-SPEC-005", "WARN", "the title is too long for the template and was shortened"))
    out = ['<g class="gm-title">']
    y = r.y0 + size * PT * 0.95
    bottom = y
    for ln in lines:
        out.append(sd.text(r.x0, y, ln, size, t["label"], weight=700, extra=' id="gm-title-text"' if ln == lines[0] else ""))
        bottom = y + size * PT * 0.25
        y += size * PT * 1.15
    if sub:
        sp = float(fonts["subtitle"])
        sl, _ = wrap_lines(sub, r.w, sp, 400, 1)
        sy = y - size * PT * 1.15 + size * PT * 0.25 + sp * PT * 1.15
        out.append(sd.text(r.x0, sy, sl[0], sp, t["label"], weight=400))
        bottom = sy + sp * PT * 0.25
        y = sy + size * PT * 1.15 - size * PT * 0.25 - sp * PT * 1.15 + sp * PT * 1.3
    if el.get("tagline"):
        sp = float(fonts["subtitle"])
        tl, _ = wrap_lines(str(el["tagline"]), r.w, sp, 400, 1)
        ty = (bottom if sub else y - size * PT * 1.15 + size * PT * 0.25) + sp * PT * 1.15
        out.append(sd.text(r.x0, ty, tl[0], sp, t["label"], weight=400, slant=TAGLINE_SLANT_DEG, extra=' data-role="tagline"'))
        bottom = ty + sp * PT * 0.25
    if el.get("frame_label"):                             # an atlas-by-time page's period, at the right of the title band (M15)
        fpt = float(fonts["subtitle"]) * FRAME_LABEL_SCALE
        out.append(sd.text(r.x1, r.y0 + fpt * PT * 0.95, str(el["frame_label"]), fpt, t["label"], weight=700, anchor="end", num_cls=True,
                           extra=' data-role="frame-label"'))
    if bottom > r.y1 + 0.5:
        findings.append(f("MAP-SPEC-005", "WARN", "title and subtitle are taller than the template's title band"))
    out.append("</g>")
    return "".join(out), text


def source_lines(plan: mp.Plan) -> list[str]:
    import provenance
    return provenance.source_lines(plan)


def draw_footer(plan: mp.Plan, tpl: dict, fonts: dict, info: dict, findings: list, conf: list, logo_in_map: bool = False) -> tuple[str, dict]:
    t = plan.tokens
    spec = plan.spec
    els = spec.get("elements") or {}
    r = lo.rect_of(tpl["footer"])
    npt = float(fonts["note"])
    min_pt = 6.0 if tpl["kind"] == "print" else 10.0
    logo_h = float(tpl.get("logo_height_mm", 10))
    logo_on = (els.get("logo") or {}).get("on", True)
    logo_svg, logo_w = ("", 0.0)
    if logo_on and not logo_in_map:
        logo_svg, logo_w = logo_markup(plan, t["background"], 0, 0, logo_h)
    ds0 = els.get("date_stamp") or {}
    date_txt = ds0.get("text") if ds0.get("text") not in (None, "auto") else f"Map produced {gl.plan_date_text(plan, today())}"
    classic = getattr(plan, "look", "classic") == "classic"
    # 0.19.0: the date stamp's width is kept free in every case (0.18 kept it only with the logo in the map, so with the logo in the footer a long
    # source note ran under the date); classic keeps the 0.16 arithmetic
    date_w = tm.width_mm(date_txt, npt, 400, tnum=True) if ds0.get("on", True) and (logo_in_map or not classic) else 0.0
    right_w = max(logo_w, date_w, 0) + 2.0
    left_w = r.w - right_w - 6.0
    # 1.1.3: the logos a provider's terms require beside its credit (OpenWeather's free plan), at their height, before the source note
    import provider_logos as plg
    plogos, pprobs = plg.for_plan(plan)
    for pr_ in pprobs:
        findings.append(f("MAP-LIC-015", "BLOCK", f"a provider's terms require its logo beside its credit and it cannot be drawn: {pr_}"))
    plogo_h = {fn: lg["height_px"] * plg.PX_TO_MM for fn, lg in ((lg["file"], lg) for _, lg in plogos)}
    plogo_w = sum(plogo_h[lg["file"]] * lg["aspect"] for _, lg in plogos) + 1.2 * len(plogos)
    if plogos:
        left_w -= plogo_w
    lines: list[str] = []
    sn = els.get("source_note") or {}
    style = str(sn.get("style") or ("detailed" if classic or tpl.get("kind") == "print" else "consolidated"))
    lines_before = None
    if sn.get("on", True) and style == "consolidated":
        import credits
        line, crep = credits.consolidated(plan, str(sn.get("label", "Sources")))     # each organization once (0.19.0)
        lines.append(line)
        lines_before = sum(len(tm.wrap(ln, left_w, npt, 400)) for ln in source_lines(plan))
        for req in crep["appended_required"]:
            findings.append(f("MAP-LIC-014", "WARN", f"a credit the data's or basemap's terms require was not covered by the consolidated source line and "
                                                     f"was added as written: \"{req[:120]}\"; give the publisher in library/publishers.yaml or the layer's "
                                                     "provenance.publisher so it is grouped"))
    elif sn.get("on", True):
        lines += source_lines(plan)
    else:                                         # the credits a licence requires stay when the source note is turned off (MAP-LIC-010)
        if not str(sn.get("reason") or "").strip():               # 0.15.0: turning the source note off needs a stated reason
            findings.append(f("MAP-LIC-011", "BLOCK", "the source note is how a reader checks the map's data; it is turned off with no reason. "
                                                      "Turn it back on, or give elements.source_note.reason (recorded in the report)"))
        import provenance
        kept = provenance.required_credit_lines(plan) + provenance.api_notice_lines(plan)
        if kept:
            lines += kept
            findings.append(f("MAP-LIC-010", "INFO", f"the source note is off; {len(kept)} credit line(s) the data's or basemap's terms require are still printed"))
    if (els.get("crs_note") or {}).get("on", False):
        lines.append(f"Projection: {plan.crs_info['name']}.")
    disc = els.get("disclaimer") or {}
    if disc.get("on") and disc.get("text"):
        lines.append(str(disc["text"]))
    tm_line = plan.pack["pack"].get("trademark_line")
    tm_on = (els.get("trademark_line") or {}).get("on", "auto")
    if tm_line and tm_on in ("auto", True):
        lines.append(tm_line)
    # fit the notes: wrap, shrink to the floor, then report
    while True:
        wrapped = []
        for ln in lines:
            wrapped += tm.wrap(ln, left_w, npt, 400)
        need = len(wrapped) * tm.line_height_mm(npt)
        if need <= r.h + 0.2 or npt <= min_pt:
            break
        npt = round(npt - 0.5, 2)
    if need > r.h + 0.2:
        findings.append(f("MAP-SPEC-005", "BLOCK", f"the footer notes need {need:.0f} mm but the template allows {r.h:.0f} mm at the minimum text size; shorten the notes or use a larger template"))
    out = ['<g class="gm-footer">']
    y = r.y0 + npt * PT * 0.95
    lx_ = r.x0
    for _, plogo in plogos:                          # each logo at its height, its foot on the first line's baseline
        h_ = plogo_h[plogo["file"]]
        out.append(f'<g class="gm-provider-logo">{plg.print_svg(plogo, lx_, y + npt * PT * 0.2 - h_, h_, t["label"])}</g>')
        lx_ += h_ * plogo["aspect"] + 1.2
    tx_ = r.x0 + (plogo_w if plogos else 0.0)
    for ln in wrapped:
        out.append(sd.text(tx_, y, ln, npt, t["label"]))
        y += tm.line_height_mm(npt)
    # right block: logo then date stamp
    ry = r.y0
    if logo_svg:
        lx = r.x1 - logo_w
        logo_svg2, _ = logo_markup(plan, t["background"], lx, ry, logo_h)
        out.append(logo_svg2)
        ry += logo_h + 1.5
    ds = els.get("date_stamp") or {}
    aso = None
    if ds.get("on", True):
        txt = ds.get("text") if ds.get("text") not in (None, "auto") else f"Map produced {gl.plan_date_text(plan, today())}"
        out.append(sd.text(r.x1, ry + npt * PT * 0.95, txt, npt, t["label"], anchor="end", num_cls=True))
        aso, aso_fs = mex.data_as_of_text(plan, ds)             # 0.15.0: "Data as of" under the date when the data changes over time
        findings += aso_fs
        if aso:
            out.append(sd.text(r.x1, ry + npt * PT * 0.95 + tm.line_height_mm(npt), aso, npt, t["label"], anchor="end", num_cls=True))
    elif mex.time_sensitive_layers(plan):
        findings.append(f("MAP-CART-028", "WARN", "the date stamp is off but the map has layers that change over time; turn it on so the map says how current it is"))
    out.append("</g>")
    right_h = (logo_h + 1.5 if logo_svg else 0.0) + (npt * PT * 1.25 if ds.get("on", True) else 0.0) + (tm.line_height_mm(npt) if aso else 0.0)
    return "".join(out), {"note_pt": npt, "lines": len(wrapped), "logo": bool(logo_svg) or (logo_on and logo_in_map),
                          **({"provider_logos": [lg["file"] for _, lg in plogos]} if plogos else {}),
                          **({"source_note_style": style, "source_lines_before": lines_before,
                              "source_lines_after": sum(len(tm.wrap(ln, left_w, npt, 400)) for ln in lines[:1])} if lines_before is not None else {}),
                          "need_mm": round(max(need, right_h), 2), **({"source_note_off": str(sn.get("reason"))[:200]} if not sn.get("on", True) else {}),
                          **({"data_as_of": aso} if aso else {})}


def alt_text(plan: mp.Plan, title: str, blocks: list[dict], info: dict) -> str:
    bits = [f"Map: {title or plan.spec['message']}."]
    bits.append(plan.spec["message"].strip())
    for l in plan.layers:
        if l.role == "context":
            continue
        lb = l.legend_block or {}
        if l.kind == "raster" and lb.get("kind") == "ramp" and lb.get("items"):
            bits.append(f"{l.title}: a continuous shading from {lb['items'][0]['label']} to {lb['items'][-1]['label']}.")
            continue
        if l.kind == "raster" and lb.get("kind") in ("classes", "categories") and lb.get("items"):
            bits.append(f"{l.title}: {len(lb['items'])} {'classes' if lb['kind'] == 'classes' else 'categories'}, {', '.join(i['label'] for i in lb['items'][:6])}.")
            continue
        if l.role == "contour":
            bits.append(f"Contour lines every {l.symbol.get('interval', 0):g} m.")
            continue
        if l.cls is not None and l.cls.kind == "bivariate":            # M15
            import bivariate
            bits.append(bivariate.summary(l))
        elif l.cls is not None and l.cls.kind == "numeric":
            bits.append(f"{l.title}: {l.cls.k} classes from {l.cls.labels[0].lower()} to {l.cls.labels[-1].lower()}.")
        elif l.cls is not None:
            bits.append(f"{l.title}: categories {', '.join(l.cls.labels[:6])}.")
        else:
            bits.append(f"{l.title} ({l.role}).")
    if any(b.get("kind") == "hillshade" for b in plan.basemap):
        bits.append("Shaded relief under the map.")
    bits.append(f"Scale about {info['ratio']}." if info.get("scale_denominator") else "")
    return " ".join(b for b in bits if b)


def assemble(plan: mp.Plan, W: float, H: float, body: str, frame: lo.Frame | None, map_rect: Rect | None, title: str, alt: str, has_basemap: bool) -> str:
    manifest = plan.manifest
    acc = bl.accreditation_props(manifest, plan.brand)
    house = manifest["house_brand"]
    created = today().isoformat()
    meta = (f'<metadata><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:geo="https://acropolisdocs.com/ns/geo-mapping#">'
            f'<rdf:Description rdf:about="">'
            f'<dc:title>{sd.esc(title or plan.spec["id"])}</dc:title><dc:creator>{sd.esc(plan.brand_name)}</dc:creator><dc:publisher>{sd.esc(plan.brand_name)}</dc:publisher>'
            f'<dc:date>{created}</dc:date><dc:description>{sd.esc(plan.spec["message"])}</dc:description><dc:format>image/svg+xml</dc:format>'
            f'<geo:accreditation>{sd.esc(acc["house"])}</geo:accreditation><geo:accreditation_url>{sd.esc(acc["house_url"])}</geo:accreditation_url>'
            f'<geo:generator>{sd.esc(acc["plugin"])} {sd.esc(acc["plugin_version"])}</geo:generator><geo:brand_pack>{sd.esc(acc["brand_pack"])}</geo:brand_pack>'
            f'<geo:crs>{sd.esc(plan.crs)}</geo:crs><geo:template>{sd.esc(plan.tpl_name)}</geo:template>'
            f'</rdf:Description></rdf:RDF></metadata>')
    st = getattr(plan, "style", None)
    if st is not None and st.fonts != "brand":       # 1.4.0 (R2-PACK): the pack's faces, subset to the glyphs this sheet draws
        import map_style
        style = map_style.font_css(st, map_style.text_chars(body)) + ".gm-num{font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1;}"
    else:
        style = (bl.inter_font_face_css() +
                 ".gm-t{font-family:Inter,sans-serif;font-kerning:normal;}.gm-num{font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1;}")
    root = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {sd.num(W)} {sd.num(H)}" width="{sd.num(W)}mm" height="{sd.num(H)}mm" '
            f'lang="en" role="img" aria-labelledby="gm-title gm-desc" data-accreditation="{sd.esc(house)}" '
            f'data-map-brand="{sd.esc(plan.brand)}" data-map-edition="{sd.esc(manifest["plugin_name"])}" data-map-id="{sd.esc(plan.spec["id"])}"'
            f' data-map-style-pack="{sd.esc(st.id if st is not None else "default")}">')
    return (root + f'<title id="gm-title">{sd.esc(title or plan.spec["id"])}</title><desc id="gm-desc">{sd.esc(alt)}</desc>' + meta +
            f'<defs><style>{style}</style>'
            + (f'<clipPath id="gm-clip"><rect x="{sd.num(map_rect.x0)}" y="{sd.num(map_rect.y0)}" width="{sd.num(map_rect.w)}" height="{sd.num(map_rect.h)}"/></clipPath>' if map_rect is not None else "")
            + '</defs>' + body + "</svg>")


def assemble_page(plan: mp.Plan, W: float, H: float, body: str, title: str, alt: str) -> str:
    """A page that is not the map (the Data & Methods appendix): same root, fonts, stamps and hidden metadata."""
    return assemble(plan, W, H, body, None, None, title, alt, False)
