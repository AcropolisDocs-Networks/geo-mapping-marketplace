#!/usr/bin/env python3
"""thematic: the geometry behind three map types the renderer draws with its existing roles (1.17.0).

  dot density       polygons with a count -> dots placed at random inside each polygon, one dot per `per` units (stochastic rounding,
                    seeded, so the same data gives the same dots). Drawn as a points layer of small dots. The dots are not locations.
  density surface   many points (optionally weighted) -> a kernel density surface on a grid (a Gaussian kernel of `bandwidth`), cut into
                    `classes` bands from a floor share of the peak to the peak. Drawn as a choropleth of bands (lower to higher).
  flow lines        origin -> destination pairs with a volume -> curved lines (a quadratic arc that bends to the right of travel, so the
                    two directions between a pair separate). Drawn as a lines layer, its width graduated by the volume.

Everything here works in a projected CRS in metres (the op chooses it, as every op does) and returns plain shapely geometry.
"""
from __future__ import annotations

import math

import numpy as np

MAX_DOTS = 200_000
MAX_CELLS = 4_000_000


class ThematicError(ValueError):
    pass


# --------------------------------------------------------------------------------------------------------------------------- dot density
def dot_counts(values, per: float, seed: int = 1) -> np.ndarray:
    """Dots per polygon: value / per, the fraction kept with that probability (stochastic rounding: the expected total is exact)."""
    if not per or per <= 0:
        raise ThematicError("dot_density needs per: the amount one dot stands for, greater than 0")
    v = np.nan_to_num(np.asarray(values, dtype=float), nan=0.0).clip(min=0.0) / float(per)
    whole = np.floor(v)
    rng = np.random.default_rng(seed)
    return (whole + (rng.random(len(v)) < (v - whole))).astype(int)


def dots_in(poly, n: int, rng) -> np.ndarray:
    """n points uniformly inside a polygon (rejection sampling in its bounds, in batches)."""
    import shapely
    if n <= 0 or poly is None or poly.is_empty:
        return np.empty((0, 2))
    minx, miny, maxx, maxy = poly.bounds
    shapely.prepare(poly)
    out = []
    got, tries = 0, 0
    share = max(poly.area / max((maxx - minx) * (maxy - miny), 1e-12), 1e-4)
    while got < n and tries < 60:
        k = int(min(2_000_000, max(64, (n - got) / share * 1.3)))
        x = rng.uniform(minx, maxx, k)
        y = rng.uniform(miny, maxy, k)
        keep = shapely.contains_xy(poly, x, y)
        pts = np.column_stack([x[keep], y[keep]])[: n - got]
        out.append(pts)
        got += len(pts)
        tries += 1
    return np.vstack(out) if out else np.empty((0, 2))


# ----------------------------------------------------------------------------------------------------------------------- density surface
def silverman_m(x: np.ndarray, y: np.ndarray) -> float:
    """A bandwidth from the points' spread (Silverman's rule on each axis, averaged), in the coordinates' units."""
    def one(a):
        sd = float(np.std(a))
        iqr = float(np.subtract(*np.percentile(a, [75, 25]))) / 1.34
        s = min(sd, iqr) if iqr > 0 else sd
        return 0.9 * s * len(a) ** -0.2
    return max(1.0, (one(x) + one(y)) / 2)


def density_grid(x, y, w, bandwidth: float, cell: float, pad: float):
    """(xs, ys, z): a kernel density surface of the points on a grid of `cell`, z in weight per km2."""
    x0, x1 = float(np.min(x)) - pad, float(np.max(x)) + pad
    y0, y1 = float(np.min(y)) - pad, float(np.max(y)) + pad
    nx, ny = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    if nx * ny > MAX_CELLS:
        raise ThematicError(f"a {nx} x {ny} grid is too fine for the area; give a larger cell (now {cell:.0f} m)")
    h, xe, ye = np.histogram2d(x, y, bins=[nx, ny], range=[[x0, x0 + nx * cell], [y0, y0 + ny * cell]], weights=w)
    sigma = bandwidth / cell
    try:
        from scipy.ndimage import gaussian_filter
        z = gaussian_filter(h, sigma=sigma, mode="constant")
    except ImportError:                                            # numpy only: a separable kernel by FFT along each axis
        r = int(math.ceil(4 * sigma))
        k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
        k /= k.sum()
        z = np.apply_along_axis(lambda a: np.convolve(a, k, mode="same"), 0, h)
        z = np.apply_along_axis(lambda a: np.convolve(a, k, mode="same"), 1, z)
    z = z / ((cell / 1000.0) ** 2)                                  # weight per km2
    xs = (xe[:-1] + xe[1:]) / 2
    ys = (ye[:-1] + ye[1:]) / 2
    return xs, ys, z.T                                              # z[row=y, col=x]


def bands(xs, ys, z, levels) -> list:
    """[(lo, hi, MultiPolygon)] for each band between consecutive levels (matplotlib's filled contours, rings resolved even-odd)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shapely
    from shapely.geometry import Polygon
    fig, ax = plt.subplots()
    try:
        cs = ax.contourf(xs, ys, z, levels=levels)
        out = []
        for i, path in enumerate(cs.get_paths()):
            geom = None
            for ring in path.to_polygons(closed_only=True):
                if len(ring) < 4:
                    continue
                p = Polygon(ring)
                if not p.is_valid:
                    p = shapely.make_valid(p)
                geom = p if geom is None else geom.symmetric_difference(p)
            if geom is not None and not geom.is_empty:
                out.append((float(levels[i]), float(levels[i + 1]), geom))
        return out
    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------------------------------------------------------- flow lines
def arc(x0, y0, x1, y1, bend: float = 0.2, n: int = 32):
    """A quadratic arc from (x0, y0) to (x1, y1) whose middle stands `bend` x the chord to the right of travel (a straight line at 0)."""
    from shapely.geometry import LineString
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L == 0:
        return None
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    cx, cy = mx + bend * L * (dy / L), my - bend * L * (dx / L)     # the right-hand normal of the travel direction
    t = np.linspace(0.0, 1.0, n)
    px = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
    py = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
    return LineString(np.column_stack([px, py]))
