"""network_measure: distance along a line network from one or more sources, vertex by vertex (4.1.0, Day 24, D24-25).

The Day 24 canal map measured how far water travels from Granite Reef Dam through 288 km of Salt River Project canals. OpenStreetMap
draws a canal as many ways that touch, nearly touch (a branch stopping a few metres short of the canal it leaves) or cross, so the
network is built the way that map needed it:

  densify    every line is cut into steps of at most `densify_m` metres, so a distance is known all along it, not only at its ends
  join       points of two lines closer than `join_m` (default 3 m) are one place
  gaps       a line end with no other line within `join_m` is tied to the nearest point of another line within `gap_m` (default 0: off)
  sources    each line within `snap_m` of a source starts there (at its nearest point); the distance is the shortest path from any of them
             (multi-source Dijkstra, scipy)

Everything is measured in the layer's own CRS units, which must be metres (geo_ops hands it a metric CRS). Pure functions of their
input; no file or network is touched.
"""
from __future__ import annotations

import numpy as np


class NetworkError(ValueError):
    pass


def _parts(geoms):
    """[(feature index, coords n x 2)] of every LineString part."""
    out = []
    for i, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        if g.geom_type == "LineString":
            ps = [g]
        elif g.geom_type == "MultiLineString":
            ps = list(g.geoms)
        else:
            raise NetworkError(f"a network is made of lines; feature {i} is a {g.geom_type}")
        for p in ps:
            c = np.asarray(p.coords, dtype=float)[:, :2]
            if len(c) >= 2:
                out.append((i, c))
    return out


def _densify(c: np.ndarray, step: float) -> np.ndarray:
    seg = np.hypot(*np.diff(c, axis=0).T)
    pts = [c[:1]]
    for k, L in enumerate(seg):
        n = max(1, int(np.ceil(L / step)))
        u = np.linspace(0, 1, n + 1)[1:, None]
        pts.append(c[k] + (c[k + 1] - c[k]) * u)
    return np.vstack(pts)


def measure(geoms, sources, *, snap_m: float = 400.0, densify_m: float = 25.0, join_m: float = 3.0, gap_m: float = 0.0) -> dict:
    """Distance along the network from the nearest source, at every densified vertex.

    `geoms`: line geometries in a metric CRS; `sources`: [(x, y)] in the same CRS. Returns {parts: [(feature, coords, dist)], snapped,
    sources, reached_m (total length reached), total_m, max_m, gaps (links added across gaps)}. Unreached vertices have dist inf."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    from scipy.spatial import cKDTree
    if densify_m <= 0:
        raise NetworkError("densify_m must be above 0")
    parts = [(i, _densify(c, densify_m)) for i, c in _parts(geoms)]
    if not parts:
        raise NetworkError("the layer has no lines")
    offs = np.cumsum([0] + [len(c) for _, c in parts])
    pts = np.vstack([c for _, c in parts])
    owner = np.concatenate([np.full(len(c), k) for k, (_, c) in enumerate(parts)])
    rows, cols, w = [], [], []
    for k, (_, c) in enumerate(parts):
        a = np.arange(offs[k], offs[k + 1] - 1)
        rows.append(a)
        cols.append(a + 1)
        w.append(np.hypot(*np.diff(c, axis=0).T))
    tree = cKDTree(pts)
    pairs = tree.query_pairs(max(join_m, 1e-6), output_type="ndarray")
    if len(pairs):
        pairs = pairs[owner[pairs[:, 0]] != owner[pairs[:, 1]]]
        rows.append(pairs[:, 0])
        cols.append(pairs[:, 1])
        w.append(np.hypot(*(pts[pairs[:, 0]] - pts[pairs[:, 1]]).T))
    n_gap = 0
    if gap_m > join_m:
        ends = np.concatenate([[offs[k], offs[k + 1] - 1] for k in range(len(parts))])
        for e in ends:
            d, j = tree.query(pts[e], k=min(64, len(pts)), distance_upper_bound=gap_m)
            d, j = np.atleast_1d(d), np.atleast_1d(j)
            for dd, jj in zip(d, j):
                if np.isfinite(dd) and jj < len(pts) and owner[jj] != owner[e]:
                    rows.append(np.array([e]))
                    cols.append(np.array([jj]))
                    w.append(np.array([max(dd, 1e-6)]))
                    n_gap += 1
                    break
    r, c_, ww = np.concatenate(rows), np.concatenate(cols), np.concatenate(w)
    ww = np.maximum(ww, 1e-9)
    g = coo_matrix((ww, (r, c_)), shape=(len(pts), len(pts))).tocsr()
    src = []
    hit = 0
    for x, y in sources:
        # every line within snap_m of a source starts at it (a dam feeds the canals on both banks): its nearest point is a source
        near = tree.query_ball_point((float(x), float(y)), snap_m)
        best: dict = {}
        for j in near:
            dd = float(np.hypot(pts[j, 0] - float(x), pts[j, 1] - float(y)))
            if owner[j] not in best or dd < best[owner[j]][0]:
                best[owner[j]] = (dd, j)
        src += [j for _, j in best.values()]
        hit += bool(best)
    if not src:
        raise NetworkError(f"no source lies within {snap_m:g} m of the network (snap_m sets the reach)")
    dist = dijkstra(g, directed=False, indices=sorted(set(src)), min_only=True)
    out_parts = [(i, c, dist[offs[k]:offs[k + 1]]) for k, (i, c) in enumerate(parts)]
    seglen = np.concatenate(w[:len(parts)]) if parts else np.array([])
    reached = 0.0
    for _, c, d in out_parts:
        L = np.hypot(*np.diff(c, axis=0).T)
        ok = np.isfinite(d[:-1]) & np.isfinite(d[1:])
        reached += float(L[ok].sum())
    fin = dist[np.isfinite(dist)]
    return {"parts": out_parts, "snapped": hit, "starts": len(set(src)), "sources": len(list(sources)), "reached_m": reached,
            "total_m": float(seglen.sum()), "max_m": float(fin.max()) if len(fin) else 0.0, "gaps": n_gap}


def feature_ranges(res: dict, n_features: int) -> tuple[np.ndarray, np.ndarray]:
    """(start, end) distance of each feature: the nearest and farthest reached vertex (nan where none is reached)."""
    lo = np.full(n_features, np.inf)
    hi = np.full(n_features, -np.inf)
    for i, _c, d in res["parts"]:
        f = d[np.isfinite(d)]
        if len(f):
            lo[i] = min(lo[i], float(f.min()))
            hi[i] = max(hi[i], float(f.max()))
    lo[~np.isfinite(lo)] = np.nan
    hi[~np.isfinite(hi)] = np.nan
    return lo, hi
