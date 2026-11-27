"""motion: values that change over time, water that flows, and the map's furniture drawn over both (4.1.0, Day 24, D24-19 to D24-22).

Day 24 made three videos, each with a renderer of its own over events_anim.build_base: the Phoenix cells breathing through a weekday, the
Salt River Project canals filling from Granite Reef Dam, and a packet's light budget across the Atlantic. This file is what they needed,
read by events_anim (`map_export.py --story MAP.yaml --timeline T.yaml`):

  values:                           a polygon layer's colour through time, smoothly
    layer: cells
    columns: [u00, u01, ..., u23]   one value per step (or column_prefix: u, which takes every u.. column in order)
    start: 1.5                      video seconds where the first column is shown (default 0)
    end: 18.3                       where the last one is (cyclic: where the first comes round again); default the run's end
    interpolate: cubic              linear | cubic (smooth); cubic never overshoots past the class range
    cyclic: true                    the last step joins the first (a day, a year)
    sparkline: {position: bottom-right, label: "Whole-metro traffic", series: [..]}   a small chart with a moving marker (series:
                                    default the mean of every column)

  flow:                             water, traffic or a signal moving along a line layer
    layer: canals
    from: [-111.69113, 33.5158]     where it starts (or a point layer's id); the distance along the lines is network_measure's
    snap_m: 400, gap_m: 120         how far a line may start from the source, and the gaps between lines it may cross
    start: 1, arrive_s: 20          it leaves at start and reaches the farthest point arrive_s later
    width_mm: 1.2                   the water line's width on the sheet (default: the layer's own)
    dry: true                       the lines still to come drawn in the brand's neutral colour first
    dash: {length_mm: 3, gap_mm: 7, speed_mm_s: 9}   light dashes moving downstream on the water
    front: {glow: true}             a soft glow at each head of water
    speed_kmh: 3.24                 the real speed, so a counter can show the real time passed ({v} hours, format: hm)

  top: true | {layers: [sites]}     the map's furniture (title, legend, locator, scale, logo, credits, labels and place names) is drawn over
                                    the moving layers, from the map's own drawing on a transparent sheet (auto: on with values or flow)
  overflow: {px: 30, bottom_px: 12} the moving layers may run past the map frame, fading out over px (bottom_px at the bottom, above the
                                    credits); 0 (the default) keeps them inside the frame

Colours come from the layer as the map draws it: its classes (values) or its line colour (flow), never a hex in the timeline.
"""
from __future__ import annotations

import numpy as np

SS = 2                       # drawn at twice the size and reduced, so edges are smooth


class MotionError(ValueError):
    pass


def _rgb(h: str) -> tuple[int, int, int]:
    h = str(h).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# --------------------------------------------------------------------------------------------------------------------------- timeline keys
def load_values(v, duration: float | None) -> dict:
    if not isinstance(v, dict) or not v.get("layer"):
        raise MotionError("values needs layer (a polygon layer of the map) and columns (one value per step)")
    cols = v.get("columns")
    if not cols and not v.get("column_prefix"):
        raise MotionError("values needs columns: [c0, c1, ...] or column_prefix")
    mode = str(v.get("interpolate", "cubic"))
    if mode not in ("linear", "cubic", "step"):
        raise MotionError(f"values.interpolate is linear, cubic or step, not '{mode}'")
    start = float(v.get("start", 0.0))
    end = float(v["end"]) if v.get("end") is not None else (float(duration) if duration else None)
    if end is not None and end <= start:
        raise MotionError(f"values ends ({end:g} s) before it starts ({start:g} s)")
    sp = v.get("sparkline")
    if sp is not None and not isinstance(sp, dict):
        sp = {} if sp is True else None
    return {"layer": str(v["layer"]), "columns": [str(c) for c in cols] if cols else None, "column_prefix": v.get("column_prefix"),
            "interpolate": mode, "cyclic": bool(v.get("cyclic")), "start": start, "end": end, "sparkline": sp,
            "opacity": float(v.get("opacity", 0.0)) or None, "outline": v.get("outline", True) is not False}


def load_flow(f, duration: float | None) -> dict:
    if not isinstance(f, dict) or not f.get("layer"):
        raise MotionError("flow needs layer (a line layer of the map) and from ([lon, lat] or a point layer's id)")
    if f.get("from") is None and not f.get("measure_field"):
        raise MotionError("flow needs from: [lon, lat] or a point layer's id (where it starts), or measure_field (a distance per line)")
    start = float(f.get("start", 0.0))
    arrive = float(f["arrive_s"]) if f.get("arrive_s") is not None else ((float(duration) - start) if duration else None)
    if arrive is not None and arrive <= 0:
        raise MotionError("flow.arrive_s must be above 0")
    dash = f.get("dash", True)
    dash = {"length_mm": 3.0, "gap_mm": 7.0, "speed_mm_s": 9.0, **(dash if isinstance(dash, dict) else {})} if dash is not False else None
    front = f.get("front", True)
    front = {"glow": True, **(front if isinstance(front, dict) else {})} if front is not False else None
    if front is not None and front.get("marker") not in (None, "auto", "point", "none", False):
        raise MotionError(f"flow.front.marker is point, none or auto, not '{front.get('marker')}'")
    return {"layer": str(f["layer"]), "from": f.get("from"), "measure_field": f.get("measure_field"), "snap_m": float(f.get("snap_m", 400)),
            "gap_m": float(f.get("gap_m", 0)), "densify_m": float(f.get("densify_m", 25)), "start": start, "arrive_s": arrive,
            "width_mm": float(f["width_mm"]) if f.get("width_mm") else None, "dry": f.get("dry", True) is not False, "dash": dash,
            "front": front, "speed_kmh": float(f["speed_kmh"]) if f.get("speed_kmh") else None}


def load_overflow(o) -> dict:
    if o in (None, False, 0):
        return {"px": 0, "bottom_px": 0}
    if o is True:
        o = {}
    if isinstance(o, (int, float)):
        o = {"px": o}
    px = int(o.get("px", 30))
    return {"px": max(0, px), "bottom_px": max(0, int(o.get("bottom_px", min(px, 12))))}


# --------------------------------------------------------------------------------------------------------------------------- top layer
FURNITURE = ("gm-title", "gm-logo", "gm-legend", "gm-inset", "gm-scalebar", "gm-footer", "gm-north", "gm-callouts", "gm-figures", "gm-neatline")
MAP_KEEP = ("gm-labels", "gm-names")


def top_svg(svg: str, layers=()) -> str:
    """The map's SVG with only what goes over moving layers: the furniture, the frame line, data labels and place names (no page, no ground,
    no data layers). -> SVG text."""
    from lxml import etree
    root = etree.fromstring(svg.encode("utf-8"))
    for el in list(root):
        tag = etree.QName(el).localname
        cls = set((el.get("class") or "").split())
        if tag == "rect" and "gm-page" in cls:
            root.remove(el)
        elif el.get("id") == "gm-map":
            for c in list(el):
                ccls = set((c.get("class") or "").split())
                if not (ccls & set(MAP_KEEP)) and c.get("data-layer") not in set(layers or ()):
                    el.remove(c)
    return etree.tostring(root, encoding="unicode")


def top_layer(svg: str, dpi: float, size: tuple[int, int], layers=()):
    """The furniture on a transparent sheet at the base's pixels (RGBA), or None when it cannot be drawn here."""
    import io

    import map_export as mx
    from PIL import Image
    try:
        t = top_svg(svg, layers)
        png = mx.rasterize(mx.to_pdf_bytes(t), float(dpi), t, alpha=True)
    except Exception:  # noqa: BLE001 - the video then draws as before, the moving layers over the furniture
        return None
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    if im.size != tuple(size):
        if abs(im.width - size[0]) > 2:
            im = im.resize((size[0], round(im.height * size[0] / im.width)), Image.LANCZOS)
        out = Image.new("RGBA", tuple(size), (0, 0, 0, 0))
        out.paste(im.crop((0, 0, min(size[0], im.width), min(size[1], im.height))), (0, 0))
        im = out
    return im


def overflow_mask(size, rect_px, ov: dict):
    """An L mask: 255 inside the map frame, fading to 0 over ov['px'] outside it (ov['bottom_px'] below it)."""
    from PIL import Image
    W, H = size
    x0, y0, x1, y1 = rect_px
    yy, xx = np.mgrid[0:H, 0:W]
    dx = np.maximum(np.maximum(x0 - xx, xx - x1), 0).astype(float)
    dt = np.maximum(y0 - yy, 0).astype(float)
    db = np.maximum(yy - y1, 0).astype(float)
    m, mb = max(ov.get("px", 0), 1e-6), max(ov.get("bottom_px", 0), 1e-6)
    f = 1 - np.maximum(np.maximum(dx / m, dt / m), db / mb)
    if ov.get("px", 0) <= 0:
        f = np.where((dx > 0) | (dt > 0), 0.0, f)
    if ov.get("bottom_px", 0) <= 0:
        f = np.where(db > 0, 0.0, f)
    return Image.fromarray((np.clip(f, 0, 1) * 255).astype(np.uint8), "L")


def masked(img, mask):
    """img (RGBA) with its alpha multiplied by mask."""
    from PIL import Image
    a = np.asarray(img.getchannel("A")).astype(np.uint16) * np.asarray(mask).astype(np.uint16) // 255
    img.putalpha(Image.fromarray(a.astype(np.uint8), "L"))
    return img


# --------------------------------------------------------------------------------------------------------------------------- values
class Values:
    """A polygon layer recoloured frame by frame from its values through time, through the layer's own classes, blended between them."""

    def __init__(self, layer, cfg: dict, to_px, size, tokens: dict, duration: float):
        gdf = layer.gdf
        cols = cfg["columns"] or sorted([c for c in gdf.columns if str(c).startswith(str(cfg["column_prefix"]))])
        miss = [c for c in cols if c not in gdf.columns]
        if miss or len(cols) < 2:
            raise MotionError(f"values: layer '{cfg['layer']}' needs at least two value columns; it lacks {', '.join(miss) or 'them'}")
        cls = getattr(layer, "cls", None)
        if cls is None or not getattr(cls, "edges", None) or not getattr(layer, "colors", None):
            raise MotionError(f"values: layer '{cfg['layer']}' needs a classification (value_field and classification), whose colours the video uses")
        self.cfg, self.cols, self.size = cfg, cols, size
        self.V = np.stack([np.asarray(gdf[c], dtype=float) for c in cols], 1)
        self.n = self.V.shape[1]
        self.start = cfg["start"]
        self.end = cfg["end"] if cfg["end"] is not None else duration
        edges = [float(e) for e in cls.edges]
        mids = np.array([(a + b) / 2 for a, b in zip(edges[:-1], edges[1:])])
        self.mids, self.lo, self.hi = mids, edges[0], edges[-1]
        self.C = np.array([_rgb(c) for c in layer.colors[:len(mids)]], dtype=float)
        self.alpha = int(255 * float(cfg.get("opacity") or getattr(layer, "fill_opacity", 0.85) or 0.85))
        self.edge = _rgb(tokens.get("background", "ffffff"))
        self.polys = []
        for g in gdf.geometry:
            ps = list(g.geoms) if g.geom_type == "MultiPolygon" else [g] if g.geom_type == "Polygon" else []
            self.polys.append([[tuple(v * SS for v in to_px(x, y)) for x, y in p.exterior.coords] for p in ps])
        self.spline = None
        if cfg["interpolate"] == "cubic":
            from scipy.interpolate import CubicSpline, PchipInterpolator
            if cfg["cyclic"]:
                self.spline = CubicSpline(np.arange(self.n + 1), np.c_[self.V, self.V[:, :1]], axis=1, bc_type="periodic")
            else:
                self.spline = PchipInterpolator(np.arange(self.n), self.V, axis=1)      # monotone: no overshoot between steps

    def position(self, t: float) -> float:
        """The step position (0 .. n-1, or 0 .. n cyclic) at video time t."""
        span = self.n if self.cfg["cyclic"] else self.n - 1
        u = (t - self.start) / max(self.end - self.start, 1e-9)
        return float(np.clip(u, 0, 1) * span)

    def values_at(self, t: float) -> np.ndarray:
        x = self.position(t)
        if self.cfg["interpolate"] == "step":
            return self.V[:, min(int(x), self.n - 1) % self.n]
        if self.spline is not None:
            return np.clip(self.spline(x), self.lo, self.hi)
        i0 = int(np.floor(x)) % self.n
        i1 = (i0 + 1) % self.n if self.cfg["cyclic"] else min(i0 + 1, self.n - 1)
        f = x - np.floor(x)
        return self.V[:, i0] * (1 - f) + self.V[:, i1] * f

    def colours(self, vals: np.ndarray) -> np.ndarray:
        v = np.clip(vals, self.mids[0], self.mids[-1])
        return np.stack([np.interp(v, self.mids, self.C[:, k]) for k in range(3)], 1)

    def overlay(self, t: float):
        from PIL import Image, ImageDraw
        W, H = self.size
        ov = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        vals = self.values_at(t)
        rgb = self.colours(vals)
        for parts, c, v in zip(self.polys, rgb, vals):
            if not np.isfinite(v):
                continue
            for p in parts:
                d.polygon(p, fill=tuple(int(x) for x in c) + (self.alpha,))
        if self.cfg.get("outline", True):
            # 5.5.0 (D26-24): only the features that have a value at t are outlined; a track cut into 160 cells read as a white ladder
            for parts, v in zip(self.polys, vals):
                if not np.isfinite(v):
                    continue
                for p in parts:
                    d.line(p + [p[0]], fill=self.edge + (150,), width=SS)
        return ov.resize((W, H), Image.LANCZOS)

    def series(self) -> np.ndarray:
        """The mean of every column: the default sparkline."""
        return np.nanmean(self.V, axis=0)


# --------------------------------------------------------------------------------------------------------------------------- flow
def _token_hex(tokens: dict, ref) -> str:
    """'@brand.status.danger' or '#rrggbb' -> '#rrggbb' (5.5.0: a flow marker's colour)."""
    s = str(ref or "")
    if not s.startswith("@"):
        return s
    cur = tokens
    for k in s.lstrip("@").split(".")[1:]:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, (list, tuple)) and k.isdigit() and int(k) < len(cur):
            cur = cur[int(k)]
        else:
            cur = None
    return str(cur) if isinstance(cur, str) else str(tokens.get("emphasis"))


def _rgb_page(flow) -> tuple:
    return getattr(flow, "page", (255, 255, 255))


class Flow:
    """A line layer filled from its source over time: the water to its head, dashes moving downstream, a glow at each head."""

    def __init__(self, layer, cfg: dict, plan, to_px, size, tokens: dict, ppm: float, other_layers: dict | None = None):
        import geopandas as gpd
        import network_measure as nm
        from pyproj import Transformer
        gdf = layer.gdf
        self.cfg, self.size, self.ppm = cfg, size, ppm
        crs_disp = gdf.crs
        if cfg["measure_field"]:
            if cfg["measure_field"] not in gdf.columns:
                raise MotionError(f"flow: layer '{cfg['layer']}' has no column '{cfg['measure_field']}'")
            parts = []
            m0 = np.asarray(gdf[cfg["measure_field"]], dtype=float)
            g_m = gdf.to_crs(gdf.estimate_utm_crs())
            for i, (g, gm) in enumerate(zip(gdf.geometry, g_m.geometry)):
                for p, pm in zip(getattr(g, "geoms", [g]), getattr(gm, "geoms", [gm])):
                    c, cm = np.asarray(p.coords)[:, :2], np.asarray(pm.coords)[:, :2]
                    along = np.r_[0, np.cumsum(np.hypot(*np.diff(cm, axis=0).T))]
                    parts.append((i, c, m0[i] + along))
            self.max_m = float(np.nanmax([np.nanmax(d) for _, _, d in parts]))
            self.note = f"flow along '{cfg['layer']}' from its {cfg['measure_field']} column"
        else:
            src = cfg["from"]
            metric = gdf.estimate_utm_crs() if (gdf.crs is None or gdf.crs.is_geographic) else gdf.crs
            lines = gdf.to_crs(metric)
            if isinstance(src, str):
                other = (other_layers or {}).get(src)
                if other is None:
                    raise MotionError(f"flow.from names '{src}', which is not a layer of the map")
                pts = [(g.x, g.y) for g in other.gdf.to_crs(metric).geometry.representative_point()]
            else:
                p = gpd.GeoSeries.from_xy([float(src[0])], [float(src[1])], crs="EPSG:4326").to_crs(metric)
                pts = [(p.iloc[0].x, p.iloc[0].y)]
            try:
                res = nm.measure(lines.geometry.values, pts, snap_m=cfg["snap_m"], densify_m=cfg["densify_m"], gap_m=cfg["gap_m"])
            except nm.NetworkError as ex:
                raise MotionError(f"flow: {ex}") from None
            back = Transformer.from_crs(metric, crs_disp, always_xy=True)
            parts = []
            for i, c, d in res["parts"]:
                x, y = back.transform(c[:, 0], c[:, 1])
                parts.append((i, np.c_[x, y], d))
            self.max_m = res["max_m"]
            self.note = (f"flow along '{cfg['layer']}': {res['reached_m'] / 1000:,.1f} of {res['total_m'] / 1000:,.1f} km reached from the source, "
                         f"the farthest {res['max_m'] / 1000:,.1f} km" + (f"; {res['gaps']} gap(s) bridged" if res["gaps"] else ""))
        self.parts_xy = [(i, np.asarray(c, dtype=float), np.asarray(d, dtype=float)) for i, c, d in parts]     # 5.5.0: display coordinates (a profile reads them)
        self.parts = [(i, np.array([[v * SS for v in to_px(x, y)] for x, y in c]), d) for i, c, d in parts]
        fr = cfg.get("front") or {}
        mk = fr.get("marker", "auto")
        # 5.5.0 (D26-33, Q-01): a single route (a ship, a vehicle) carries a solid marker at its front, the strongest mark on the frame; a
        # network keeps the soft glow at each head
        self.marker = cfg.get("front") is not None and ((mk == "point") or (mk in ("auto", None) and len({i for i, _, _ in parts}) == 1))
        self.marker_r = max(2, int(round(float(fr.get("size_mm") or 3.0) / 2 * ppm * SS)))
        mc = fr.get("colour") or fr.get("color")
        self.marker_col = _rgb(_token_hex(tokens, mc) if mc else ((tokens.get("status") or {}).get("danger") or tokens["emphasis"]))
        self.marker_halo = fr.get("halo", True) is not False
        w_mm = cfg["width_mm"] or max(0.6, float(getattr(layer, "stroke_w_mm", 0.6) or 0.6) * 1.6)
        self.w = max(1, int(round(w_mm * ppm * SS)))
        self.water = _rgb(getattr(layer, "stroke", None) or tokens["emphasis"])
        # the channel still to fill: the water's own colour mixed far toward the page, so it reads as the same canal, empty
        bg = _rgb(tokens.get("background", "ffffff"))
        self.dry = tuple(int(w * 0.32 + b * 0.68) for w, b in zip(self.water, bg))
        self.page = bg
        self.crs_display = crs_disp
        self.foam = tuple(int(255 - (255 - c) * 0.15) for c in self.water)
        self.glow = tuple(min(255, int(c * 0.5 + 255 * 0.5)) for c in self.water)
        self.arrive = cfg["arrive_s"] or 20.0
        self.speed = self.max_m / max(self.arrive, 1e-6)          # metres of network per video second
        self.dry_img = self._dry() if cfg["dry"] else None

    def _dry(self):
        from PIL import Image, ImageDraw
        W, H = self.size
        im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for _, P, dist in self.parts:
            if np.isfinite(dist).any():
                d.line([tuple(p) for p in P], fill=self.dry + (255,), width=max(1, int(self.w * 0.8)), joint="curve")
        return im.resize((W, H), Image.LANCZOS)

    def front_m(self, t: float) -> float:
        return max(0.0, (t - self.cfg["start"])) * self.speed

    def progress(self, t: float) -> float:
        """5.5.0 (D26-23): how far along the flow is at t, 0 to 1 (a chart's marker and a day counter read it, so they cannot drift)."""
        return float(min(1.0, self.front_m(t) / max(self.max_m, 1e-9)))

    def time_at_m(self, m: float) -> float:
        """The video second the front reaches m metres along (a story beat tied to a distance)."""
        return float(self.cfg["start"] + max(0.0, m) / max(self.speed, 1e-9))

    def head_px(self, t: float) -> list:
        """The front's position(s) in frame pixels at t (one per part still filling; the end when it has arrived)."""
        fm = min(self.front_m(t), self.max_m)
        out = []
        for _, P, r in self.parts:
            ok = np.isfinite(r)
            if not ok.any():
                continue
            rr = np.where(ok, r, np.nan)
            if fm >= np.nanmax(rr):
                if fm >= self.max_m and np.nanmax(rr) >= self.max_m - 1e-6:
                    k = int(np.nanargmax(rr))
                    out.append(tuple(P[k] / SS))
                continue
            below = np.flatnonzero(ok & (r <= fm))
            if not len(below):
                continue
            k = below.max()
            if k + 1 < len(P) and ok[k + 1] and r[k + 1] > r[k]:
                f = (fm - r[k]) / (r[k + 1] - r[k])
                out.append(tuple((P[k] + (P[k + 1] - P[k]) * f) / SS))
        return out

    def profile(self, raster: str | None = None, column: str | None = None, layer=None, samples: int = 240) -> tuple:
        """5.5.0 (D26-23): a value along the flow, by distance: from a raster sampled at `samples` points, or a column of the flow layer.
        -> (distance m, value) arrays, in order of distance."""
        ds, vs = [], []
        if column is not None:
            if layer is None or column not in layer.gdf.columns:
                raise MotionError(f"chart.column: the flow layer has no column '{column}'")
            col = np.asarray(layer.gdf[column], dtype=float)
            for i, _c, d in self.parts_xy:
                ok = np.isfinite(d)
                ds += list(d[ok])
                vs += [col[i]] * int(ok.sum())
        elif raster:
            import rasterio
            from pyproj import Transformer
            allp = np.concatenate([np.c_[c[np.isfinite(d)], d[np.isfinite(d)]] for _i, c, d in self.parts_xy]) if self.parts_xy else np.zeros((0, 3))
            if not len(allp):
                raise MotionError("chart: the flow has no measured line to sample")
            allp = allp[np.argsort(allp[:, 2])]
            want = np.linspace(allp[0, 2], allp[-1, 2], max(8, int(samples)))
            xs, ys = np.interp(want, allp[:, 2], allp[:, 0]), np.interp(want, allp[:, 2], allp[:, 1])
            with rasterio.open(raster) as src:
                crs_from = getattr(self, "crs_display", None)
                import water_ops as _wo
                if crs_from is not None and src.crs is not None and not _wo._same_crs(src.crs, crs_from):
                    xs, ys = Transformer.from_crs(crs_from, src.crs, always_xy=True).transform(xs, ys)
                vals = np.array([v[0] for v in src.sample(zip(xs, ys))], dtype=float)
                if src.nodata is not None:
                    vals[vals == src.nodata] = np.nan
            ds, vs = list(want), list(vals)
        else:
            raise MotionError("chart kind profile needs raster: FILE or column: NAME (a value along the flow)")
        o = np.argsort(ds)
        return np.asarray(ds, dtype=float)[o], np.asarray(vs, dtype=float)[o]

    def real_hours(self, t: float) -> float:
        """Real hours since the start at speed_kmh (for a counter)."""
        sp = self.cfg.get("speed_kmh")
        return (min(self.front_m(t), self.max_m) / 1000.0 / sp) if sp else 0.0

    def overlay(self, t: float):
        from PIL import Image, ImageDraw, ImageFilter
        W, H = self.size
        ov = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        gl = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d, g = ImageDraw.Draw(ov), ImageDraw.Draw(gl)
        fm = self.front_m(t)
        done = fm >= self.max_m
        dash = self.cfg["dash"]
        per = (dash["length_mm"] + dash["gap_mm"]) * self.ppm * SS if dash else 0
        for _, P, r in self.parts:
            ok = np.isfinite(r)
            m = ok & (r <= fm)
            if m.sum() < 1:
                continue
            idx = np.flatnonzero(m)
            k = idx.max()
            pts = [tuple(p) for p in (P[:k + 1] if idx.min() == 0 else P[idx])]
            if k + 1 < len(P) and ok[k + 1] and r[k + 1] > fm and r[k + 1] > r[k]:
                f = (fm - r[k]) / (r[k + 1] - r[k])
                head = tuple(P[k] + (P[k + 1] - P[k]) * f)
                pts.append(head)
                if self.cfg["front"] and self.cfg["front"].get("glow") and not done:
                    R = self.w * 2.5
                    g.ellipse([head[0] - R, head[1] - R, head[0] + R, head[1] + R], fill=self.glow + (190,))
            if len(pts) > 1:
                d.line(pts, fill=self.water + (255,), width=self.w, joint="curve")
            if dash and per > 0:
                # dashes by the screen length along the line, moving downstream (where the distance grows)
                L = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
                phase = ((L - dash["speed_mm_s"] * self.ppm * SS * t) % per) < dash["length_mm"] * self.ppm * SS
                on = phase & m
                i = 0
                while i < len(on):
                    if on[i]:
                        j = i
                        while j + 1 < len(on) and on[j + 1]:
                            j += 1
                        if j > i:
                            d.line([tuple(p) for p in P[i:j + 1]], fill=self.foam + (235,), width=max(1, int(self.w * 0.4)))
                        i = j + 1
                    else:
                        i += 1
        if self.marker:
            R = self.marker_r
            for hx, hy in self.head_px(t):
                X, Y = hx * SS, hy * SS
                if self.marker_halo:
                    h_ = R + max(SS, int(R * 0.45))
                    d.ellipse([X - h_, Y - h_, X + h_, Y + h_], fill=_rgb_page(self) + (255,))
                d.ellipse([X - R, Y - R, X + R, Y + R], fill=self.marker_col + (255,))
        ov = ov.resize((W, H), Image.LANCZOS)
        gl = gl.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(max(2, self.w // 2)))
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if self.dry_img is not None:
            out.alpha_composite(self.dry_img)
        out.alpha_composite(gl)
        out.alpha_composite(ov)
        return out


# --------------------------------------------------------------------------------------------------------------------------- sparkline
def sparkline_rect(size, rect_px, cfg: dict, avoid=None) -> list[int]:
    """Where the sparkline panel goes, in pixels: a corner of the map frame (position, default bottom-right), stepped clear of the legend,
    locator and the like. The story line keeps clear of it (events_anim passes it to timeline.paint)."""
    W, H = size
    k = H / 720.0
    PW, PH = int(260 * k), int(96 * k)
    x0, y0, x1, y1 = rect_px
    pos = str(cfg.get("position") or "bottom-right")
    pad = int(12 * k)
    PX = int(x1 - PW - pad) if pos.endswith("right") else int(x0 + pad)
    PY = int(y1 - PH - pad) if pos.startswith("bottom") else int(y0 + pad)
    for a in avoid or []:                                   # step clear of the legend, locator and the like, upward
        if not (PX + PW < a[0] or PX > a[2] or PY + PH < a[1] or PY > a[3]):
            PY = int(a[1] - PH - pad) if pos.startswith("bottom") else int(a[3] + pad)
    return [PX, PY, PX + PW, PY + PH]


def paint_sparkline(img, t: float, values: Values, cfg: dict, tokens: dict, font_path: str, rect_px, avoid=None):
    """A small chart of a series over the run, its area filled, a marker at time t, in a panel in a free corner of the map frame."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = img.size
    k = H / 720.0
    series = np.asarray(cfg.get("series") or values.series(), dtype=float)
    if len(series) < 2:
        return img
    PX, PY, PX2, PY2 = sparkline_rect(img.size, rect_px, cfg, avoid)
    PW, PH = PX2 - PX, PY2 - PY
    pad = int(12 * k)
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    bg, fr = _rgb(tokens.get("legend_bg", "ffffff")), _rgb(tokens.get("legend_frame", "cccccc"))
    ink, em = _rgb(tokens.get("label", "222222")), _rgb(tokens.get("emphasis", "444444"))
    d.rounded_rectangle([PX, PY, PX + PW, PY + PH], radius=int(5 * k), fill=bg + (238,), outline=fr, width=1)
    f = ImageFont.truetype(font_path, max(10, int(12 * k)))
    if cfg.get("label"):
        d.text((PX + pad, PY + int(6 * k)), str(cfg["label"]), font=f, fill=ink + (210,))
    cx0, cy0, cx1, cy1 = PX + pad, PY + int(26 * k), PX + PW - pad, PY + PH - int(10 * k)
    n = len(series) + (1 if values.cfg["cyclic"] else 0)
    ys = np.r_[series, series[:1]] if values.cfg["cyclic"] else series
    lo, hi = float(np.nanmin(ys)), float(np.nanmax(ys))
    sx = lambda i: cx0 + i / max(n - 1, 1) * (cx1 - cx0)                 # noqa: E731
    sy = lambda v: cy1 - (v - lo) / max(hi - lo, 1e-9) * (cy1 - cy0)     # noqa: E731
    pts = [(sx(i), sy(v)) for i, v in enumerate(ys)]
    d.polygon(pts + [(cx1, cy1), (cx0, cy1)], fill=em + (60,))
    d.line(pts, fill=em + (255,), width=max(1, int(2 * k)))
    x = values.position(t)
    mx = sx(x)
    my = sy(float(np.interp(x, np.arange(len(ys)), ys)))
    d.line([(mx, cy0 - 2), (mx, cy1)], fill=ink + (150,), width=1)
    r = max(3, int(5 * k))
    d.ellipse([mx - r, my - r, mx + r, my + r], fill=em + (255,), outline=bg, width=max(1, int(2 * k)))
    im = img.convert("RGBA")
    im.alpha_composite(over)
    return im
