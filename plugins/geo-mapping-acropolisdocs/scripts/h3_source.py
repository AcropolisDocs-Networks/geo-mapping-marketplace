#!/usr/bin/env python3
"""h3_source: data that already arrives on the H3 grid, read, rolled up, counted and drawn without a hand-written script (2.6.0, spec
global-regions H-01 to H-07).

    source: {type: h3, path: data/*.gpkg, id_field: h3_res9_id, layer: <optional>, where: "environmnt = 0", group_field: brandname}
    h3: {resolution: auto | 0-15, by: brandname, share_min: 0.5, agg: count_groups, field: carriers, complete: region, mode: centre}

    read_ids(paths, id_field, ...)        -> DataFrame with `h3` (string ids) and the attribute columns asked for; no geometry is built
    to_parent_ids(ids, res)               -> parent ids, by bit arithmetic on the 64-bit index (a million ids in well under a second)
    rollup(df, res, by=, share_min=, agg=) -> DataFrame indexed by the target cell: the value, `groups` (the groups passing) and `children`
    complete_grid(geom, res, mode)        -> every cell of a region at a resolution (centre, intersect or share)
    cells_gdf(table, res)                 -> polygons in EPSG:4326, densified at coarse resolutions, split at the antimeridian

Rolling up. A target cell at resolution r holds 7^(s - r) cells of the source resolution s (pentagons hold fewer, which the note says).
For each target cell and each group (`by`, a carrier), the share is the group's distinct source cells over that number; a group counts in the
cell when its share is at least `share_min` (default 0.5). `agg`: count_groups (how many groups pass), share (one `group`'s share), any (1 when
any group passes), all (1 when every group named in the data passes), count (distinct source cells), sum, mean, max, min (of `value_field`).

Reading. GeoPackage and other GDAL files through pyogrio with read_geometry=False and only the id, group and value columns; `where` goes to
GDAL as SQL. Parquet and CSV through DuckDB when it is installed (the db profile), else pandas. Ids may be strings or 64-bit integers; both
become the string form. A progress line every 500,000 rows goes to stderr.
"""
from __future__ import annotations

import glob
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402
import hexbin  # noqa: E402

AGGS = ("count_groups", "share", "any", "all", "count", "sum", "mean", "max", "min")
MODES = ("centre", "intersect", "share")
SHARE_MIN = 0.5
PROGRESS_ROWS = 500_000
CELL_MIN_MM = 0.8          # resolution auto: the finest grid whose cells are at least this wide on the page
CELL_COMFORT_MM = 1.2      # under this, MAP-HEX-002 offers the next coarser resolution
RES_MASK = np.uint64(0xF) << np.uint64(52)


class H3SourceError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_USAGE, rule: str | None = None):
        super().__init__(message)
        self.code = code
        self.rule = rule


def _say(msg: str) -> None:
    print(f"h3_source: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------------------------- ids
def ids_to_int(ids) -> np.ndarray:
    """Strings (hex) or integers to uint64."""
    a = np.asarray(ids)
    if a.dtype.kind in "iu":
        return a.astype(np.uint64)
    out = np.empty(len(a), dtype=np.uint64)
    for i, s in enumerate(a):
        out[i] = int(str(s).strip(), 16) if s is not None and str(s).strip() else 0
    return out


def ints_to_str(a: np.ndarray) -> list[str]:
    return [format(int(x), "x") for x in a]


def resolution_of(a: np.ndarray) -> np.ndarray:
    return ((a & RES_MASK) >> np.uint64(52)).astype(np.int64)


def to_parent_ids(a: np.ndarray, res: int) -> np.ndarray:
    """The parent at `res` of every id (uint64): the resolution field set to res and every finer digit set to 7 (unused), as H3 defines it."""
    r = np.uint64(int(res))
    low = np.uint64((1 << (3 * (15 - int(res)))) - 1)
    return (a & ~RES_MASK) | (r << np.uint64(52)) | low


# ---------------------------------------------------------------------------------------------------------------- reading
def expand(path, base: Path) -> list[Path]:
    pats = path if isinstance(path, (list, tuple)) else [path]
    out: list[Path] = []
    for p in pats:
        q = Path(str(p))
        q = q if q.is_absolute() else base / q
        hits = sorted(Path(x) for x in glob.glob(str(q))) if any(ch in str(q) for ch in "*?[") else ([q] if q.exists() else [])
        out += hits
    if not out:
        raise H3SourceError(f"source.path {path} matches no file", gc.EXIT_USAGE, "MAP-HEX-001")
    return out


def _read_one(p: Path, cols: list[str], layer: str | None, where: str | None):
    import pandas as pd
    suf = p.suffix.lower()
    if suf in (".parquet", ".geoparquet", ".csv", ".tsv"):
        try:
            import duckdb
            con = duckdb.connect()
            fn = "read_parquet" if suf in (".parquet", ".geoparquet") else "read_csv_auto"
            sql = f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} FROM {fn}(?)" + (f" WHERE {where}" if where else "")
            return con.execute(sql, [str(p)]).df()
        except ImportError:
            df = pd.read_parquet(p, columns=cols) if suf in (".parquet", ".geoparquet") else pd.read_csv(p, usecols=cols, dtype=str, sep="\t" if suf == ".tsv" else ",")
            if where:
                df = df.query(where.replace(" = ", " == ").replace("<>", "!="), engine="python")
            return df
    import pyogrio
    kw = {"columns": cols, "read_geometry": False}
    if layer:
        kw["layer"] = layer
    if where:
        kw["where"] = where
    df = pyogrio.read_dataframe(p, **kw)
    return pd.DataFrame(df)


def read_ids(paths: list[Path], id_field: str, *, group_field: str | None = None, value_field: str | None = None, layer: str | None = None,
             where: str | None = None, extra: dict | None = None) -> "object":
    """One table of ids (and the group and value columns) from every file, with a `source_file` column when there are several."""
    import pandas as pd
    cols = [c for c in dict.fromkeys([id_field, group_field, value_field]) if c]
    frames, rows = [], 0
    for p in paths:
        try:
            df = _read_one(p, cols, layer, where)
        except Exception as ex:  # noqa: BLE001
            raise H3SourceError(f"{p.name}: the H3 table could not be read ({str(ex).splitlines()[0][:200]}); check id_field "
                                f"'{id_field}'" + (f" and where ({where})" if where else ""), gc.EXIT_USAGE, "MAP-HEX-001") from None
        if id_field not in df.columns:
            raise H3SourceError(f"{p.name}: no column '{id_field}' (id_field)", gc.EXIT_USAGE, "MAP-HEX-001")
        if len(paths) > 1:
            df["source_file"] = p.name
        for k, v in (extra or {}).get(p.name, {}).items():
            df[k] = v
        rows += len(df)
        if rows // PROGRESS_ROWS != (rows - len(df)) // PROGRESS_ROWS:
            _say(f"{rows:,} rows read")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
    a = ids_to_int(df[id_field].to_numpy())
    ok = a != 0
    df = df.loc[ok].reset_index(drop=True)
    df["_h3"] = a[ok]
    return df


# ---------------------------------------------------------------------------------------------------------------- rolling up
def rollup(df, res: int, *, by: str | None = None, share_min: float = SHARE_MIN, agg: str = "count_groups", value_field: str | None = None,
           group: str | None = None, field: str = "value"):
    """-> (table indexed by the target cell's string id, notes). Columns: `field` (the value), `children` (distinct source cells present),
    `groups` (the groups passing, comma-separated) when `by` is given, and `share` for agg share."""
    import pandas as pd
    if agg not in AGGS:
        raise H3SourceError(f"h3.agg must be one of {', '.join(AGGS)}", gc.EXIT_USAGE, "MAP-HEX-001")
    if not 0 < float(share_min) <= 1:
        raise H3SourceError("h3.share_min must be above 0 and at most 1", gc.EXIT_USAGE, "MAP-HEX-001")
    if agg in ("sum", "mean", "max", "min") and not value_field:
        raise H3SourceError(f"h3.agg {agg} needs value_field", gc.EXIT_USAGE, "MAP-HEX-001")
    if agg in ("count_groups", "share", "all") and not by:
        raise H3SourceError(f"h3.agg {agg} needs h3.by (the column whose groups are counted, such as brandname)", gc.EXIT_USAGE, "MAP-HEX-001")
    if agg == "share" and group is None:
        raise H3SourceError("h3.agg share needs h3.group: the one group whose share is drawn", gc.EXIT_USAGE, "MAP-HEX-001")
    notes = []
    srcres = np.unique(resolution_of(df["_h3"].to_numpy())) if len(df) else np.array([res])
    if len(srcres) > 1:
        raise H3SourceError(f"the H3 ids are of more than one resolution ({', '.join(map(str, srcres))}); give one table per resolution", gc.EXIT_USAGE, "MAP-HEX-001")
    s = int(srcres[0])
    if res > s:
        notes.append(f"h3: the data is at resolution {s}; resolution {res} is finer than the data, so {s} is drawn")
        res = s
    kids = 7 ** (s - res)
    d = pd.DataFrame({"child": df["_h3"].to_numpy(), "cell": to_parent_ids(df["_h3"].to_numpy(), res)})
    if by:
        d["g"] = df[by].astype(str).to_numpy()
    if value_field:
        d["v"] = pd.to_numeric(df[value_field], errors="coerce").to_numpy()
    out = pd.DataFrame(index=pd.Index(np.unique(d["cell"].to_numpy()), name="cell"))
    out["children"] = d.groupby("cell")["child"].nunique()
    if by:
        gs = d.drop_duplicates(["cell", "g", "child"]).groupby(["cell", "g"]).size() / float(kids)
        passing = gs[gs >= float(share_min) - 1e-12]
        names = sorted(set(d["g"]))
        if agg == "count_groups":
            out[field] = passing.groupby(level=0).size().reindex(out.index, fill_value=0).astype(int)
        elif agg == "share":
            one = gs.xs(str(group), level=1) if str(group) in set(gs.index.get_level_values(1)) else pd.Series(dtype=float)
            out[field] = one.reindex(out.index, fill_value=0.0).round(4)
        elif agg == "any":
            out[field] = (passing.groupby(level=0).size().reindex(out.index, fill_value=0) > 0).astype(int)
        elif agg == "all":
            out[field] = (passing.groupby(level=0).size().reindex(out.index, fill_value=0) == len(names)).astype(int)
        lists = passing.reset_index().groupby("cell")["g"].apply(lambda x: ", ".join(sorted(x)))
        out["groups"] = lists.reindex(out.index).fillna("")
    if agg == "count":
        out[field] = out["children"].astype(int)
    elif agg in ("sum", "mean", "max", "min"):
        out[field] = getattr(d.groupby("cell")["v"], agg)().reindex(out.index)
    elif agg == "any" and not by:
        out[field] = (out["children"] / float(kids) >= float(share_min)).astype(int)
    out.index = ints_to_str(out.index.to_numpy(dtype=np.uint64))
    out.index.name = "h3"
    notes.append(f"h3: {len(d):,} cells at resolution {s} rolled up to {len(out):,} at resolution {res} ({kids:,} children a cell)"
                 + (f"; a {by} counts in a cell when it covers at least {float(share_min):.0%} of its children" if by else ""))
    return out, notes, res, s


# ---------------------------------------------------------------------------------------------------------------- the complete grid
def complete_grid(geom, res: int, mode: str = "centre") -> tuple[list[str], dict]:
    """Every cell of `geom` (lon/lat) at `res`: centre (the cell's centre inside), intersect (touching it) or share (touching, with the
    share of each cell inside, returned as {id: share})."""
    h3 = hexbin._h3()
    import shapely
    from shapely.geometry import mapping
    if mode not in MODES:
        raise H3SourceError(f"h3.mode must be one of {', '.join(MODES)}", gc.EXIT_USAGE, "MAP-HEX-001")
    parts = list(getattr(geom, "geoms", [geom]))
    cells: set = set()
    for g in parts:
        if g.is_empty:
            continue
        gg = g if mode == "centre" else g.buffer(hexbin.EDGE_KM[res] / 111.32 * 2.0)
        try:
            cells |= set(h3.geo_to_cells(mapping(gg), res))
        except Exception:  # noqa: BLE001 - a part h3 cannot read: its polyfill from the exterior only
            cells |= set(h3.geo_to_cells(mapping(shapely.Polygon(gg.exterior)), res))
    shares: dict = {}
    if mode != "centre":
        keep = set()
        for c in cells:
            poly = shapely.Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)])
            inter = poly.intersection(geom).area
            if inter > 0:
                keep.add(c)
                shares[c] = round(inter / poly.area, 4) if poly.area else 0.0
        cells = keep
    return sorted(cells), shares


# ---------------------------------------------------------------------------------------------------------------- drawing
def _boundary(h3, c: str, res: int) -> list:
    pts = [(lng, lat) for lat, lng in h3.cell_to_boundary(c)]
    if res <= 5:                                   # H-05: densified, so a projected cell keeps its curved edges
        dense = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            if abs(x1 - x0) > 180:
                x1 = x1 + (360 if x0 > 0 else -360)
            for k in range(5):
                dense.append((x0 + (x1 - x0) * k / 5, y0 + (y1 - y0) * k / 5))
        pts = dense
    return pts


def _poly(h3, c: str, res: int):
    import shapely
    from shapely.geometry import box
    pts = _boundary(h3, c, res)
    xs = [p[0] for p in pts]
    if max(xs) - min(xs) > 180:                    # H-05: a cell across the antimeridian is split, never a band across the map
        sh = [(x + 360 if x < 0 else x, y) for x, y in pts]
        p = shapely.Polygon(sh)
        east = p.intersection(box(0, -90, 180, 90))
        west = shapely.affinity.translate(p.intersection(box(180, -90, 360, 90)), xoff=-360)
        return shapely.MultiPolygon([g for g in (east, west) if not g.is_empty and g.geom_type == "Polygon"]) if not east.is_empty or not west.is_empty else p
    return shapely.Polygon(pts)


def cells_gdf(table, res: int):
    import geopandas as gpd
    import shapely.affinity  # noqa: F401
    h3 = hexbin._h3()
    ids = list(table.index)
    polys = [_poly(h3, c, res) for c in ids]
    data = {"h3": ids, "resolution": res}
    for c in table.columns:
        data[c] = table[c].values
    return gpd.GeoDataFrame(data, geometry=polys, crs=hexbin.WGS84)


def cell_width_km(res: int) -> float:
    """Flat-to-flat width of an average cell (sqrt 3 times the edge)."""
    return math.sqrt(3) * hexbin.EDGE_KM[res]


def resolution_for_page(mm_per_km: float, data_res: int | None = None) -> tuple[int, float, str]:
    """H-07: the finest resolution whose cell is at least CELL_MIN_MM wide on the page (never finer than the data). -> (res, mm, note)."""
    for r in range(15, -1, -1):
        mm = cell_width_km(r) * mm_per_km
        if mm >= CELL_MIN_MM:
            if data_res is not None and r > data_res:
                return data_res, cell_width_km(data_res) * mm_per_km, f"the data's own resolution ({data_res}) is coarser than the page allows"
            return r, mm, f"cells {mm:.2f} mm wide on the page"
    return 0, cell_width_km(0) * mm_per_km, "the coarsest resolution"


def cell_area_km2(res: int) -> float:
    return 3 * math.sqrt(3) / 2 * hexbin.EDGE_KM[res] ** 2


# ---------------------------------------------------------------------------------------------------------------- the layer
def load_layer(layer: dict, base: Path, *, region_geom=None, mm_per_km=None, reader=None) -> tuple:
    """A MapSpec layer with `source: {type: h3}` -> (cells GeoDataFrame, notes, provenance steps, findings)."""
    src = dict(layer.get("source") or {})
    cfg = dict(layer.get("h3") or {})
    if src.get("adapter"):
        import adapters
        try:
            got = adapters.get(src["adapter"]).fetch_h3(src, base, region_geom)
        except adapters.AdapterError as ex:
            raise H3SourceError(str(ex), ex.code, ex.rule) from None
        df, src_steps = got["table"], got["steps"]
        id_field = "_h3"
        for k, v in (got.get("defaults") or {}).items():          # 3.1.0: each adapter says how its table rolls up (operators per cell, a speed, a group count)
            if k not in cfg:
                cfg[k] = v
        if not cfg.get("by") and "provider" in df.columns and cfg.get("agg") in (None, "count_groups", "share", "any", "all"):
            cfg["by"] = "provider"
    else:
        if not src.get("id_field"):
            raise H3SourceError(f"layer '{layer.get('id')}': an h3 source needs id_field (the column holding the cell ids)", gc.EXIT_USAGE, "MAP-HEX-001")
        paths = expand(src.get("path"), base)
        df = (reader or read_ids)(paths, src["id_field"], group_field=cfg.get("by") or src.get("group_field"),
                                  value_field=layer.get("h3_value_field") or cfg.get("value_field"), layer=src.get("layer"), where=src.get("where"))
        src_steps = [f"read {len(df):,} H3 ids from {len(paths)} file(s) ({', '.join(p.name for p in paths[:4])}{', ...' if len(paths) > 4 else ''})"
                     + (f" where {src['where']}" if src.get("where") else "") + "; no geometry was built"]
        id_field = src["id_field"]
    findings: list = []
    data_res = int(resolution_of(df["_h3"].to_numpy()[:1])[0]) if len(df) else None
    want = cfg.get("resolution", "auto")
    if want in (None, "auto"):
        if mm_per_km:
            res, mm, why = resolution_for_page(mm_per_km, data_res)
            if mm < CELL_COMFORT_MM and res > 0:
                findings.append({"id": "MAP-HEX-002", "severity": "WARN", "layer": layer.get("id"),
                                 "message": f"H3 resolution {res} cells are {mm:.2f} mm wide on this page (under {CELL_COMFORT_MM} mm); "
                                            f"h3.resolution: {res - 1} draws cells {cell_width_km(res - 1) * mm_per_km:.2f} mm wide", "detail": None})
        else:
            res, why = (data_res if data_res is not None else 7), "the data's own resolution (no page scale known)"
    else:
        res, why = int(want), "as asked"
    field = cfg.get("field") or layer.get("value_field") or "value"
    table, notes, res, srcres = rollup(df, res, by=cfg.get("by"), share_min=float(cfg.get("share_min", SHARE_MIN)), agg=cfg.get("agg", "count_groups" if cfg.get("by") else "count"),
                                       value_field=cfg.get("value_field"), group=cfg.get("group"), field=field)
    notes.insert(0, f"h3: resolution {res} ({why})")
    comp = cfg.get("complete")
    if comp:
        if region_geom is None:
            raise H3SourceError(f"layer '{layer.get('id')}': h3.complete: region needs a region: block (or region_geom)", gc.EXIT_USAGE, "MAP-HEX-001")
        mode = cfg.get("mode", "centre")
        cells, shares = complete_grid(region_geom, res, mode)
        keep = set(cells)
        empty_val = 0 if table[field].dtype.kind in "iub" else 0.0
        import pandas as pd
        full = pd.DataFrame(index=pd.Index(cells, name="h3"))
        full = full.join(table, how="left")
        full[field] = full[field].fillna(empty_val)
        full["children"] = full["children"].fillna(0).astype(int)
        if "groups" in full.columns:
            full["groups"] = full["groups"].fillna("")
        if mode == "share":
            full["area_share"] = [shares.get(c, 0.0) for c in full.index]
        dropped = int((~table.index.isin(list(keep))).sum())
        notes.append(f"h3: the complete grid of the region at resolution {res} ({mode} mode): {len(full):,} cells, "
                     f"{int((full['children'] == 0).sum()):,} with no data drawn as {empty_val}"
                     + (f"; {dropped:,} cell(s) with data outside the region left out" if dropped else ""))
        table = full
    if field in table.columns and table[field].dtype.kind in "f" and (table[field] == table[field].round()).all() and cfg.get("agg", "count_groups") in ("count_groups", "count", "any", "all"):
        table[field] = table[field].astype(int)
    gdf = cells_gdf(table, res)
    steps = src_steps + [n for n in notes] + [f"H3 resolution {res}: about {cell_area_km2(res):.2f} km² a cell"]
    return gdf, notes, steps, findings, {"resolution": res, "source_resolution": srcres, "id_field": id_field, "field": field,
                                         "cell_km2": round(cell_area_km2(res), 3), "cells": int(len(gdf))}


def export_cells(spec_path, layer_id: str, out) -> dict:
    """H-06: a MapSpec's H3 cells layer, rolled up as the map draws it, written as a table of ids (.csv, .parquet: no geometry, a few MB at
    most) or as polygons (.gpkg, .fgb, .geojson), with its h3 and resolution fields."""
    import map_plan as mp
    import region as region_mod
    sp = Path(spec_path)
    spec = gc.load_mapspec(sp)
    ly = next((x for x in spec.get("layers") or [] if x.get("id") == layer_id), None)
    if ly is None or (ly.get("source") or {}).get("type") != "h3":
        raise H3SourceError(f"layer '{layer_id}' is not an h3 layer of {sp.name}", gc.EXIT_USAGE, "MAP-HEX-001")
    reg = region_mod.region_of(spec, sp.parent)
    mp._BUILD.clear()
    if reg is not None:
        mp._BUILD["region"] = reg
    mmk = None
    try:
        import layout as lo
        fr = lo.load_template(str(spec.get("template") or "letter-landscape").replace("auto-social", "16x9-slide").replace("auto-slide", "16x9-slide")).get("map_full") or {}
        if reg is not None and fr.get("w"):
            mmk = mp.mm_per_km(reg.bounds, (fr["w"], fr["h"]))
    except Exception:  # noqa: BLE001
        pass
    gdf, notes, _steps, _fs, info = load_layer(ly, sp.parent, region_geom=reg.geometry if reg is not None else None, mm_per_km=mmk)
    o = Path(out)
    o.parent.mkdir(parents=True, exist_ok=True)
    suf = o.suffix.lower()
    if suf in (".csv", ".parquet"):
        tab = gdf.drop(columns=gdf.geometry.name)
        (tab.to_parquet(o, index=False) if suf == ".parquet" else tab.to_csv(o, index=False))
    elif suf in (".gpkg", ".fgb", ".geojson"):
        gdf.to_file(o, driver={".gpkg": "GPKG", ".fgb": "FlatGeobuf", ".geojson": "GeoJSON"}[suf])
    else:
        raise H3SourceError("the cells go to .csv or .parquet (ids only) or .gpkg, .fgb, .geojson (polygons)", gc.EXIT_USAGE, "MAP-HEX-001")
    gc.ledger_note(outputs=[str(o)])
    return {"ok": True, "out": str(o), "cells": int(len(gdf)), "bytes": o.stat().st_size, "resolution": info["resolution"], "notes": notes}


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Roll an H3 table up to a coarser resolution (no map): counts and the groups that pass")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--id-field", required=True)
    ap.add_argument("--resolution", type=int, required=True)
    ap.add_argument("--by")
    ap.add_argument("--share-min", type=float, default=SHARE_MIN)
    ap.add_argument("--agg", default=None, choices=AGGS)
    ap.add_argument("--where")
    ap.add_argument("--layer")
    ap.add_argument("--out", help="write the table (ids and values) as .csv or .parquet")
    a = ap.parse_args(argv)
    try:
        df = read_ids([Path(p) for p in a.paths], a.id_field, group_field=a.by, layer=a.layer, where=a.where)
        t, notes, res, s = rollup(df, a.resolution, by=a.by, share_min=a.share_min, agg=a.agg or ("count_groups" if a.by else "count"))
    except H3SourceError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule or "MAP-HEX-001", "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return ex.code
    if a.out:
        (t.to_parquet if a.out.endswith(".parquet") else t.to_csv)(a.out)
    vc = t["value"].value_counts().sort_index().to_dict() if "value" in t.columns else {}
    print(json.dumps({"ok": True, "cells": int(len(t)), "resolution": res, "source_resolution": s, "value_counts": {str(k): int(v) for k, v in vc.items()},
                      "notes": notes, "out": a.out}, indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
