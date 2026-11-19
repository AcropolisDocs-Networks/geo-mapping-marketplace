#!/usr/bin/env python3
"""duck_engine: DuckDB as an optional engine for large tables (M16, gap analysis P2-8).

    duck_engine.py status          what is installed, whether the spatial extension loads, and the thresholds `auto` uses

One switch, `engine: auto | pandas | duckdb` (a layer key in the MapSpec, a parameter of geo_io.read, table_join.join and the geo_ops
aggregate ops). `pandas` is the path the plugin always had. `duckdb` asks for DuckDB and says why when it cannot be used. `auto` (the default)
picks DuckDB when it is installed and the job is above the threshold in AUTO_ROWS for its kind. Every result says which engine ran (the
`engine` record in provenance: {engine, why, rows}).

What DuckDB does here, and only this:
  where    an attribute filter on a CSV or GeoParquet read, instead of the round trip through a scratch GeoPackage. The filter text is the
           OGR SQL / SQLite text ogr2ogr takes; DuckDB runs it only when it is in the subset both read the same way (columns compared with
           values of their own type, AND, OR, NOT, IN, IS NULL, BETWEEN, LIKE with a plain-ASCII pattern, which SQLite reads without regard
           to case and DuckDB is told to as well). Anything else (a function, arithmetic, a column compared with a column, a date) goes
           through the GeoPackage as before, so a filter never changes meaning with the engine.
  tables   a large CSV table for a join is read by DuckDB as text (every column VARCHAR, so a FIPS 04013 keeps its zero), with pandas'
           missing-value words; the MAP-JOIN checks then run unchanged on what was read.
  pairs    the point-in-polygon pairs of count_in, sum_in and spatial_join (a point layer against a polygon layer), through the spatial
           extension's join; the counts, sums and joined rows are then made by the same code as the pandas path, so the results match
           to the last digit.
  grid     the square-grid aggregation's GROUP BY (counts, sums, means, medians, minima and maxima per cell).

DuckDB is a SOFT dependency (pip install duckdb). Its `spatial` extension is not part of the wheel: DuckDB downloads it on `INSTALL spatial`,
which this module never runs. It is loaded only from where it already is on this machine: DuckDB's own extension folder (after the person ran
`INSTALL spatial` once, outside any map build), the file named by GMAP_DUCKDB_SPATIAL, or the `duckdb-extension-spatial` pip
package. Without it the attribute work (where, tables, grid) still runs in DuckDB and the point-in-polygon pairs stay on the pandas path, with
that said in the record. Automatic install and automatic loading of extensions are switched off on every connection, so nothing here reaches
the network.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ENGINES = ("auto", "pandas", "duckdb")
# Rows above which `auto` picks DuckDB, by kind of work (measured on the M16 bench machine; see docs/milestones/M16/README-scale.md).
AUTO_ROWS = {"where": 20_000, "table": 200_000, "pairs": None, "grid": 1_000_000}
# None: auto never picks DuckDB for that kind. Point-in-polygon pairs: shapely's STRtree was twice as fast as DuckDB's spatial join at 250,000
# and at 1,000,000 points on the two-core bench machine, so DuckDB runs them only when `engine: duckdb` asks.
# 1.15.0, measured again (DuckDB 2.0, shapely 2.2, 1,000,000 points in the 3,108 counties of the lower 48): STRtree 0.70 s on one core;
# DuckDB 2.09 s with one thread and with two, of which loading the geometries (WKB in, serial) is 0.42 to 0.54 s and the join 1.19 s with one
# thread, 0.68 s with two. Even a join that kept that speed-up on every added core would leave DuckDB at about 0.74 s on four cores and
# 0.57 s on eight: no faster than the tree below a million points, under a fifth faster above. So `pairs` stays None by default, and a
# machine decides for itself: `duck_engine.py bench-pairs` measures both on the machine it runs on and, where DuckDB wins, records the
# smallest size it won at in the person's own .geo-mapping/engine.json, which `auto` then uses on that machine (same core count) only.
MACHINE_FILE = "engine.json"
BENCH_SIZES = (250_000, 1_000_000, 4_000_000)
INSTALL_HINT = "pip install duckdb"
SPATIAL_HINT = ("the DuckDB spatial extension is not on this machine; DuckDB downloads it only when told to (`INSTALL spatial`, once, by you), "
                "or `pip install duckdb-extension-spatial`; without it point-in-polygon work stays on the pandas path")
_SPATIAL_STATE: dict = {}


class EngineError(Exception):
    """`engine: duckdb` was asked for and cannot be used; the message says why and what to install."""


def version() -> str | None:
    try:
        import duckdb
        return str(duckdb.__version__)
    except Exception:  # noqa: BLE001 - not installed, or a broken install: either way not usable
        return None


def connect():
    """A private in-memory DuckDB connection that never installs or auto-loads an extension (so it never fetches one)."""
    import duckdb
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    return con


def _spatial_candidates() -> list[tuple[str, str]]:
    """(how, LOAD argument) in the order tried."""
    out = [("DuckDB's extension folder", "spatial")]
    env = os.environ.get("GMAP_DUCKDB_SPATIAL")
    if env:
        out.append(("GMAP_DUCKDB_SPATIAL", env))
    try:
        import importlib.util
        spec = importlib.util.find_spec("duckdb_extension_spatial")
        if spec and spec.submodule_search_locations:
            ver = version() or ""
            for root in spec.submodule_search_locations:
                for p in sorted(Path(root).glob(f"extensions/v{ver}/spatial.duckdb_extension")) + sorted(Path(root).glob("extensions/*/spatial.duckdb_extension")):
                    out.append(("the duckdb-extension-spatial package", str(p)))
    except Exception:  # noqa: BLE001
        pass
    return out


def load_spatial(con) -> tuple[bool, str]:
    """Load the spatial extension into `con` from a local file only. -> (loaded, where from or why not). DuckDB checks the extension's
    signature as it loads it, whichever place it comes from."""
    whys = []
    for how, arg in _spatial_candidates():
        try:
            if arg == "spatial":
                con.execute("LOAD spatial")
            else:
                con.execute("LOAD '" + arg.replace("'", "''") + "'")
            _SPATIAL_STATE.update(ok=True, source=how)
            return True, how
        except Exception as ex:  # noqa: BLE001 - not there, wrong version, unsigned: try the next place
            whys.append(f"{how}: {str(ex).splitlines()[0][:120]}")
    _SPATIAL_STATE.update(ok=False, source=None, why="; ".join(whys))
    return False, SPATIAL_HINT


def spatial_available() -> bool:
    if "ok" not in _SPATIAL_STATE:
        if version() is None:
            return False
        con = connect()
        try:
            load_spatial(con)
        finally:
            con.close()
    return bool(_SPATIAL_STATE.get("ok"))


def decide(engine: str | None, rows: int, kind: str, *, needs_spatial: bool = False, refuse: str | None = None) -> dict:
    """Which engine runs a job of `rows` rows of `kind` (where, table, pairs, grid). -> {engine, why, rows, kind}.
    `refuse` is a reason the job cannot run in DuckDB at all (a filter outside the shared subset); it sends `auto` to pandas and makes
    `duckdb` fall back to pandas with that reason. `engine: duckdb` without DuckDB raises EngineError."""
    eng = (engine or "auto").strip().lower()
    if eng not in ENGINES:
        raise EngineError(f"engine must be one of {', '.join(ENGINES)} (got '{engine}')")
    rec = {"engine": "pandas", "rows": int(rows), "kind": kind}
    if eng == "pandas":
        return rec | {"why": "engine: pandas was asked for"}
    limit = auto_rows(kind)
    if eng == "auto" and limit is None:
        return rec | {"why": f"auto keeps {kind} on the pandas path, which was faster on every size measured; engine: duckdb runs it in DuckDB"}
    have = version()
    if have is None:
        if eng == "duckdb":
            raise EngineError(f"engine: duckdb was asked for but DuckDB is not installed; {INSTALL_HINT}, or use engine: pandas")
        return rec | {"why": f"DuckDB is not installed ({INSTALL_HINT} to use it above {limit:,} rows)"}
    if refuse:
        return rec | {"why": refuse}
    if needs_spatial and not spatial_available():
        return rec | {"why": SPATIAL_HINT}
    if eng == "auto" and rows <= limit:
        return rec | {"why": f"{rows:,} rows, not more than the {limit:,} at which auto uses DuckDB for {kind}"}
    why = "engine: duckdb was asked for" if eng == "duckdb" else f"{rows:,} rows, more than the {limit:,} at which auto uses DuckDB for {kind}"
    return {"engine": "duckdb", "rows": int(rows), "kind": kind, "why": why, "duckdb": have}


# ---------------------------------------------------------------------------------------------------------------- where
_TOKEN = re.compile(r"""\s*(?:
    (?P<str>'(?:[^']|'')*')
  | (?P<num>[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<qid>"(?:[^"]|"")+")
  | (?P<op><>|!=|<=|>=|==|=|<|>)
  | (?P<punct>[(),])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
)""", re.X)
_KEYWORDS = {"AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN"}


def _family(series) -> str | None:
    """num, text, or None for a column the two engines may read differently (booleans, dates, an object column that is not all text)."""
    import pandas as pd
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return None
    if pd.api.types.is_integer_dtype(dtype) or pd.api.types.is_float_dtype(dtype):
        return "num"
    if pd.api.types.is_string_dtype(dtype) and not pd.api.types.is_object_dtype(dtype):
        return "text"
    if pd.api.types.is_object_dtype(dtype) and pd.api.types.infer_dtype(series, skipna=True) in ("string", "empty"):
        return "text"
    return None


def where_sql(where: str, frame) -> tuple[str | None, str | None]:
    """The DuckDB text of an ogr2ogr/SQLite `where`, or (None, why not) when it is outside the subset both read the same way.

    The subset: comparisons of a column with a literal of its own kind (text with 'quoted', numbers with numbers), IN lists, IS [NOT] NULL,
    BETWEEN, LIKE with an ASCII pattern on a text column, AND / OR / NOT and parentheses. A text column holding anything but text (mixed values)
    is outside it too."""
    geom = getattr(frame, "_geometry_column_name", None)
    cols = {str(c).lower(): c for c in frame.columns if c != geom}
    fams: dict = {}
    toks, pos = [], 0
    text = where.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            return None, f"the filter has text DuckDB is not given ({text[pos:pos + 20]!r})"
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "word" and val.upper() not in _KEYWORDS:
            kind = "id"
        if kind == "qid":
            kind, val = "id", val[1:-1].replace('""', '"')
        toks.append((kind, val))
    out = []
    i = 0
    n = len(toks)

    def lit_family(tok):
        return {"str": "text", "num": "num"}.get(tok[0])

    while i < n:
        kind, val = toks[i]
        if kind == "word":
            out.append(val.upper())
            i += 1
            continue
        if kind == "punct":
            out.append(val)
            i += 1
            continue
        if kind != "id":
            return None, "the filter compares a value with a value, or puts a value where a column belongs"
        col = cols.get(val.lower())
        if col is None:
            return None, f"'{val}' is not a column of the layer"
        if col not in fams:
            fams[col] = _family(frame[col])
        fam = fams[col]
        if fam is None:
            return None, f"column '{col}' is a {frame[col].dtype} column, which DuckDB and SQLite compare differently"
        ident = '"' + str(col).replace('"', '""') + '"'
        nxt = toks[i + 1] if i + 1 < n else ("end", "")
        if nxt[0] == "op":
            lit = toks[i + 2] if i + 2 < n else ("end", "")
            if lit_family(lit) != fam:
                return None, f"column '{col}' is compared with something that is not a {'number' if fam == 'num' else 'quoted text'}"
            out += [ident, "=" if nxt[1] == "==" else nxt[1], lit[1]]
            i += 3
        elif nxt[0] == "word" and nxt[1].upper() == "IS":
            j = i + 2
            neg = j < n and toks[j][0] == "word" and toks[j][1].upper() == "NOT"
            j += 1 if neg else 0
            if not (j < n and toks[j][0] == "word" and toks[j][1].upper() == "NULL"):
                return None, "IS is used other than as IS NULL or IS NOT NULL"
            out += [ident, "IS NOT NULL" if neg else "IS NULL"]
            i = j + 1
        elif nxt[0] == "word" and nxt[1].upper() in ("IN", "NOT", "LIKE", "BETWEEN"):
            j = i + 1
            neg = toks[j][1].upper() == "NOT"
            j += 1 if neg else 0
            if j >= n or toks[j][0] != "word":
                return None, "NOT after a column must come before IN, LIKE or BETWEEN"
            word = toks[j][1].upper()
            if word == "IN":
                if j + 1 >= n or toks[j + 1] != ("punct", "("):
                    return None, "IN needs a list in parentheses"
                k, items = j + 2, []
                while k < n and toks[k] != ("punct", ")"):
                    if toks[k] == ("punct", ","):
                        k += 1
                        continue
                    if lit_family(toks[k]) != fam:
                        return None, f"the IN list for '{col}' holds something that is not a {'number' if fam == 'num' else 'quoted text'}"
                    items.append(toks[k][1])
                    k += 1
                if k >= n or not items:
                    return None, "an IN list is empty or not closed"
                out += [ident, "NOT IN" if neg else "IN", "(" + ", ".join(items) + ")"]
                i = k + 1
            elif word == "LIKE":
                lit = toks[j + 1] if j + 1 < n else ("end", "")
                if fam != "text" or lit[0] != "str" or not lit[1].isascii():
                    return None, "LIKE is used on a column that is not text, or with a pattern that is not plain ASCII (SQLite ignores case only for ASCII)"
                out += [ident, "NOT ILIKE" if neg else "ILIKE", lit[1]]
                i = j + 2
            else:                                               # BETWEEN a AND b
                if j + 3 >= n or lit_family(toks[j + 1]) != fam or toks[j + 2][1].upper() != "AND" or lit_family(toks[j + 3]) != fam:
                    return None, f"BETWEEN on '{col}' needs two values of its own kind"
                out += [ident, "NOT BETWEEN" if neg else "BETWEEN", toks[j + 1][1], "AND", toks[j + 3][1]]
                i = j + 4
        else:
            return None, f"column '{col}' is used other than in a comparison with a value"
    sql = " ".join(out)
    # the result must still be a well-formed condition; DuckDB's parser is the judge, at run time
    return sql, None


def _arrow_result(res):
    """The result of an executed query as an Arrow table (DuckDB 2 names it to_arrow_table, DuckDB 1 fetch_arrow_table)."""
    for name in ("to_arrow_table", "fetch_arrow_table"):
        fn = getattr(res, name, None)
        if callable(fn):
            return fn()
    return res.arrow()


def _arrow_attrs(frame):
    """The attribute columns as an Arrow table, NaN read as NULL (the GeoPackage stores NaN as NULL), plus the row position."""
    import numpy as np
    import pyarrow as pa
    import pandas as pd
    attrs = pd.DataFrame({c: frame[c] for c in frame.columns if c != getattr(frame, "_geometry_column_name", None)
                          and not str(frame[c].dtype) == "geometry"})
    attrs.columns = [str(c) for c in attrs.columns]
    table = pa.Table.from_pandas(attrs, preserve_index=False)
    return table.append_column("__gm_pos", pa.array(np.arange(len(frame), dtype="int64")))


def filter_positions(frame, where: str):
    """Row positions of `frame` that the filter keeps, in row order (DuckDB). Raises when `where` is outside the shared subset."""
    import numpy as np
    sql, why = where_sql(where, frame)
    if sql is None:
        raise EngineError(why)
    con = connect()
    try:
        tab = _arrow_attrs(frame)
        con.register("gm_t", tab)
        got = con.execute(f"SELECT __gm_pos FROM gm_t WHERE {sql} ORDER BY __gm_pos").fetchnumpy()["__gm_pos"]
        return np.asarray(got, dtype="int64")
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------------------------- tables
PANDAS_NA = ["", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan", "1.#IND", "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None",
             "n/a", "nan", "null"]


def read_csv_text(path, sep: str):
    """A CSV table read as text the way table_join.read_table reads it with pandas (dtype=object, pandas' missing-value words, a BOM
    dropped), through DuckDB. Raises on anything DuckDB reads differently (duplicate header names, ragged rows)."""
    import numpy as np
    import pandas as pd
    con = connect()
    try:
        nulls = "[" + ", ".join("'" + v.replace("'", "''") + "'" for v in PANDAS_NA) + "]"
        q = (f"SELECT * FROM read_csv(?, all_varchar = true, header = true, delim = ?, quote = '\"', escape = '\"', nullstr = {nulls}, "
             f"strict_mode = true, auto_detect = true, sample_size = -1, normalize_names = false, encoding = 'utf-8')")
        rel = con.sql(q, params=[str(path), sep])
        names = list(rel.columns)
        tab = _arrow_result(rel)
    finally:
        con.close()
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        header = f.readline()
    import csv
    raw = next(csv.reader([header], delimiter=sep))
    if len(raw) != len(names) or len(set(raw)) != len(raw):
        raise EngineError("the header has repeated or unread names")
    data = {}
    for raw_name, col in zip(raw, tab.columns, strict=True):
        vals = np.array(col.to_pylist(), dtype=object)
        vals[np.equal(vals, None)] = np.nan
        data[raw_name] = vals
    return pd.DataFrame(data, dtype=object)


# ---------------------------------------------------------------------------------------------------------------- point-in-polygon pairs
def pip_pairs(left_geoms, right_geoms) -> tuple:
    """(left position, right position) of every pair whose geometries intersect, one side points and the other polygons, sorted by left
    then right, through the spatial extension's join. Raises EngineError when the extension does not load."""
    import numpy as np
    import pyarrow as pa
    import shapely
    con = connect()
    try:
        ok, why = load_spatial(con)
        if not ok:
            raise EngineError(why)
        for name, geoms in (("gm_l", left_geoms), ("gm_r", right_geoms)):
            g = np.asarray(geoms, dtype=object)
            good = ~shapely.is_missing(g) & ~shapely.is_empty(g)
            pos = np.flatnonzero(good)
            wkb = shapely.to_wkb(g[good], output_dimension=2)
            con.register(name + "_a", pa.table({"pos": pa.array(pos, type=pa.int64()), "wkb": pa.array(list(wkb), type=pa.binary())}))
            con.execute(f"CREATE TEMP TABLE {name} AS SELECT pos, ST_GeomFromWKB(wkb) AS g FROM {name}_a")
        got = con.execute("SELECT gm_l.pos AS l, gm_r.pos AS r FROM gm_l JOIN gm_r ON ST_Intersects(gm_l.g, gm_r.g) ORDER BY l, r").fetchnumpy()
        return np.asarray(got["l"], dtype=np.intp), np.asarray(got["r"], dtype=np.intp)
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------------------------- grid
def grid_table(cell, values, agg: str, val: str | None):
    """GROUP BY cell: `count` and, unless agg is count, `<val>_<agg>` over the non-missing values (pandas' rules: the median of an even
    number of values is the mean of the middle two). -> a pandas DataFrame indexed by cell, cells ascending."""
    import numpy as np
    import pyarrow as pa
    con = connect()
    try:
        # one thread and Kahan sums (fsum), value by value in row order: pandas adds a group's values the same way, so sums and means
        # agree to the last digit; a parallel sum would add the same values in another order
        con.execute("SET threads = 1")
        cols = {"cell": pa.array(np.asarray(cell, dtype="int64"))}
        if values is not None:
            cols["v"] = pa.array(np.asarray(values, dtype="float64"), from_pandas=True)
        con.register("gm_g", pa.table(cols))
        sel = "cell, count(*) AS count"
        if agg != "count":
            expr = {"sum": "coalesce(fsum(v), 0.0)", "mean": "fsum(v) / count(v)", "max": "max(v)", "min": "min(v)",
                    "median": "CASE WHEN count(v) = 0 THEN NULL ELSE (list_sort(list(v) FILTER (WHERE v IS NOT NULL))[(count(v) + 1) // 2] "
                              "+ list_sort(list(v) FILTER (WHERE v IS NOT NULL))[count(v) // 2 + 1]) / 2 END"}[agg]
            sel += f", {expr} AS \"{val}_{agg}\""
        df = con.execute(f"SELECT {sel} FROM gm_g WHERE cell >= 0 GROUP BY cell ORDER BY cell").fetchdf()
    finally:
        con.close()
    return df.set_index("cell")


def _machine_path():
    import gmap_common as gc
    from pathlib import Path
    home = os.environ.get("GEO_MAPPING_HOME")
    return (Path(home) if home else Path.home() / gc.WORKDIR_NAME) / MACHINE_FILE


def machine_thresholds() -> dict:
    """What `bench-pairs` measured on this machine ({kind: rows or None}), when it was measured with the cores the machine has now."""
    p = _machine_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no record, or an unreadable one: the shipped thresholds
        return {}
    if not isinstance(d, dict) or int(d.get("cores") or 0) != (os.cpu_count() or 0):
        return {}
    return {k: v for k, v in (d.get("auto_rows") or {}).items() if k in AUTO_ROWS and (v is None or isinstance(v, int))}


def auto_rows(kind: str):
    m = machine_thresholds()
    return m[kind] if kind in m else AUTO_ROWS.get(kind)


def bench_pairs(sizes=BENCH_SIZES, *, polygons=None, seed: int = 1, write: bool = True, runs: int = 2) -> dict:
    """Point-in-polygon pairs on this machine: shapely's STRtree against DuckDB's spatial join, `runs` times each (the best kept), at each
    size. The polygons are the bundled US counties unless given. -> {cores, sizes: {n: {strtree_s, duckdb_s}}, pairs: threshold or None}."""
    import time as _t
    import numpy as np
    import shapely
    if version() is None:
        raise EngineError(f"DuckDB is not installed; {INSTALL_HINT}")
    if not spatial_available():
        raise EngineError(SPATIAL_HINT)
    if polygons is None:
        import geopandas as gpd
        import gmap_common as gc
        g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "us_counties_cb.parquet").to_crs("EPSG:4326")
        g = g[~g["STATEFP"].isin(["02", "15", "60", "66", "69", "72", "78"])]
        polygons = np.asarray(g.geometry.values, dtype=object)
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = shapely.total_bounds(polygons)
    out: dict = {}
    win = None
    for n in sizes:
        pts = shapely.points(rng.uniform(x0, x1, int(n)), rng.uniform(y0, y1, int(n)))
        best_t = best_d = float("inf")
        for _ in range(max(1, runs)):
            t = _t.perf_counter()
            tree = shapely.STRtree(polygons)
            a, b = tree.query(pts, predicate="intersects")
            np.lexsort((b, a))
            best_t = min(best_t, _t.perf_counter() - t)
            t = _t.perf_counter()
            pip_pairs(pts, polygons)
            best_d = min(best_d, _t.perf_counter() - t)
        out[int(n)] = {"strtree_s": round(best_t, 3), "duckdb_s": round(best_d, 3)}
        if win is None and best_d < best_t * 0.9:          # a tenth faster at least, or the switch is not worth it
            win = int(n)
    rec = {"cores": os.cpu_count(), "duckdb": version(), "measured_on": __import__("datetime").date.today().isoformat(), "sizes": out,
           "auto_rows": {"pairs": win}}
    if write:
        p = _machine_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        p.write_text(json.dumps({**(old if isinstance(old, dict) else {}), **rec}, indent=2), encoding="utf-8")
        rec["stored_in"] = str(p)
    rec["pairs"] = win
    return rec


def status() -> dict:
    v = version()
    out = {"duckdb": v, "install": None if v else INSTALL_HINT, "auto_rows": {k: auto_rows(k) for k in AUTO_ROWS},
           "measured_here": machine_thresholds() or None}
    if v:
        out["spatial"] = spatial_available()
        out["spatial_source"] = _SPATIAL_STATE.get("source")
        if not out["spatial"]:
            out["spatial_why"] = SPATIAL_HINT
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what is installed and the thresholds auto uses")
    b = sub.add_parser("bench-pairs", help="measure point-in-polygon pairs here (STRtree against DuckDB) and record where DuckDB wins")
    b.add_argument("--sizes", default=",".join(str(n) for n in BENCH_SIZES))
    b.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "bench-pairs":
        try:
            r = bench_pairs(tuple(int(x) for x in a.sizes.split(",") if x.strip()), write=not a.no_write)
        except EngineError as ex:
            print(str(ex), file=sys.stderr)
            return 1
        print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
