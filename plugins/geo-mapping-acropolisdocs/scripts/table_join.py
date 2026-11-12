#!/usr/bin/env python3
"""table_join: attach a client table (CSV or XLSX) to a boundary layer by a key, and say exactly what matched (MAP-JOIN-*).

    table_join.py inspect TABLE [--sheet NAME]
    table_join.py join BOUNDARY TABLE --left GEOID --right fips --out joined.gpkg [--sheet NAME] [--columns a,b] [--pad 5]

A layer asks for it with `join: {table: data/markets.xlsx, sheet: Q3, left: GEOID, right: fips, columns: [subs, pop], pad: 5}`; the path is relative to the
MapSpec. Keys are compared as text: trimmed, an Excel "4013.0" read back as "4013", and zero-padded to the boundary's key width when the table dropped the leading
zeros (a county FIPS of 04013 stored as 4013). Each repair is reported (MAP-JOIN-005), never silent.

Which columns become numbers. Only the columns named in `numeric` (the planner passes the layer's value and denominator fields; the CLI takes --numeric)
are turned into numbers. Without that list, a column is turned into a number only when it is not code-like: its name is not an id, code, ZIP or FIPS, and no
value carries a leading zero (a "04013" or "00501" is a code, and a number would lose the zero and then the join on it). Everything else stays text.

Findings
  MAP-JOIN-001 BLOCK  the table or a key column is missing, or no row matched at all
  MAP-JOIN-002 BLOCK  the table has the same key on more than one row (the join would be ambiguous); say how to combine them first
  MAP-JOIN-003 WARN   table rows whose key matched no boundary (listed)
  MAP-JOIN-004 WARN   boundary features with no table row (drawn as No data)
  MAP-JOIN-005 INFO   a key repair was applied
  MAP-JOIN-006 WARN   a column from the table replaces a boundary column of the same name
  MAP-JOIN-007 WARN   the boundary repeats a key on several features (each gets the same row; a dissolve or a different key may be meant)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MAX_ROWS = 2_000_000
SHOW = 8


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


class JoinError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-JOIN-001"):
        super().__init__(message)
        self.fid = fid


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_lines(p: Path) -> int:
    """Newlines in a file, read in 1 MB blocks (a 2-million-row table takes well under a second)."""
    n = 0
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            n += block.count(b"\n")
    return n


def _first_line_delimiter(p: Path) -> str:
    """The delimiter pandas' python engine would use for sep=None: csv.Sniffer on the first non-empty line."""
    import csv
    with open(p, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if line.strip():
                return csv.Sniffer().sniff(line).delimiter
    raise JoinError(f"'{p.name}' is empty")


def read_table(path, sheet: str | None = None, engine: str | None = None, record: dict | None = None):
    """The table as text (every value a string, missing values NaN). A CSV above duck_engine.AUTO_ROWS['table'] rows (or any CSV with
    engine: duckdb) is read by DuckDB, as text, with pandas' words for a missing value; `record` receives which engine read it (M16)."""
    import pandas as pd
    p = Path(path)
    if not p.is_file():
        raise JoinError(f"the table '{p.name}' was not found")
    ext = p.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            df = pd.read_excel(p, sheet_name=sheet or 0, dtype=object, engine="openpyxl")
            if record is not None:
                record.update({"engine": "pandas", "why": "a workbook is read by openpyxl", "kind": "table"})
        elif ext in (".csv", ".txt", ".tsv"):
            sep = "\t" if ext == ".tsv" else None
            df = None
            import duck_engine as de
            try:
                dec = de.decide(engine, max(count_lines(p) - 1, 0), "table")
            except de.EngineError as ex:
                raise JoinError(str(ex)) from None
            if dec["engine"] == "duckdb":
                try:
                    df = de.read_csv_text(p, sep or _first_line_delimiter(p))
                except Exception as ex:  # noqa: BLE001 - anything DuckDB reads differently: pandas reads it as before
                    dec = {**dec, "engine": "pandas", "why": f"DuckDB could not read the table the way pandas does ({str(ex).splitlines()[0][:120]}); pandas read it"}
            if df is None:
                df = pd.read_csv(p, dtype=object, sep=sep, engine="python", encoding="utf-8-sig")
            if record is not None:
                record.update(dec)
        else:
            raise JoinError(f"'{p.name}': only .csv and .xlsx tables are read ({ext or 'no extension'} given)")
    except JoinError:
        raise
    except ValueError as ex:
        raise JoinError(f"'{p.name}': {str(ex)[:200]}")
    except Exception as ex:  # noqa: BLE001 - a corrupt workbook should be a finding, not a crash
        raise JoinError(f"'{p.name}' could not be read: {type(ex).__name__}: {str(ex)[:160]}")
    if len(df) > MAX_ROWS:
        raise JoinError(f"'{p.name}' has {len(df):,} rows; the limit is {MAX_ROWS:,}")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    return df


def sheets(path) -> list[str]:
    p = Path(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        return []
    import openpyxl
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


_FLOAT_INT = re.compile(r"^-?\d+\.0+$")


def norm_key(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return None
    if _FLOAT_INT.match(s):
        s = s.split(".")[0]
    return s


def keys_of(series) -> list:
    return [norm_key(v) for v in series]


def _to_number(s):
    import pandas as pd
    t = s.astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.replace("%", "", regex=False).str.strip()
    n = pd.to_numeric(t, errors="coerce")
    ok = s.notna() & (s.astype(str).str.strip() != "")
    return n if (n[ok].notna().mean() if ok.any() else 0) >= 0.9 else s


CODE_LIKE = re.compile(r"(^|[_\s])(id|fid|gid|objectid|geoid|fips|zip|zcta|zipcode|postal|code|key|cbsa|tract)(\d*)([_\s]|$)", re.I)
_LEADING_ZERO = re.compile(r"^0\d")


def is_code_like(name: str, values) -> bool:
    """A column that must stay text: named like an identifier or a code, or holding a value with a leading zero (a FIPS 04013, a ZIP 00501).
    Turning it into a number drops the zero, and the next join on it fails quietly."""
    if CODE_LIKE.search(str(name)):
        return True
    for v in values:
        if isinstance(v, str) and _LEADING_ZERO.match(v.strip()):
            return True
    return False


def numeric_columns(df, keep: list[str], declared: list[str] | None) -> list[str]:
    """The columns of `keep` to turn into numbers: the declared ones (value and denominator fields), else those that are not code-like."""
    if declared is not None:
        return [c for c in keep if c in declared]
    return [c for c in keep if not is_code_like(c, df[c].tolist())]


def check_boundary_duplicates(keys: list, left: str, show: int = 3) -> list[dict]:
    """MAP-JOIN-007: the boundary side repeats a key. Every feature with that key gets the same table row, which is right for a multi-part
    place stored as several features and wrong when the key is not the feature's own id (a state code on counties); the person decides."""
    seen, dup = {}, {}
    for k in keys:
        if k is None:
            continue
        if k in seen:
            dup[k] = dup.get(k, 1) + 1
        seen[k] = True
    if not dup:
        return []
    ex = sorted(dup)[:show]
    total = sum(dup.values())
    return [finding("MAP-JOIN-007", "WARN", f"the boundary repeats '{left}' on {total} features across {len(dup)} key(s) (for example {', '.join(ex)}); "
                    "each of them gets the same table row. If one feature per key is meant, dissolve the boundary on that key first", {"keys": len(dup), "features": total, "examples": ex})]


def join(gdf, table, left: str, right: str, *, columns: list[str] | None = None, pad: int | None = None, table_name: str = "table", numeric: list[str] | None = None):
    """(joined GeoDataFrame, steps, notes, findings, report). Raises JoinError for MAP-JOIN-001/002.
    `numeric` names the table columns to turn into numbers (the layer's value and denominator fields); None means the non-code-like ones."""
    import pandas as pd
    fs, steps, notes = [], [], []
    if left not in gdf.columns:
        raise JoinError(f"the boundary has no column '{left}'; columns are: {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
    if right not in table.columns:
        raise JoinError(f"{table_name} has no column '{right}'; columns are: {', '.join(table.columns)}")
    lk = keys_of(gdf[left])
    rk = keys_of(table[right])
    fs += check_boundary_duplicates(lk, left)
    repairs = []
    lens = {len(k) for k in lk if k}
    if pad is None and len(lens) == 1 and all(k is None or k.isdigit() for k in lk):
        width = next(iter(lens))
        short = [k for k in rk if k and k.isdigit() and len(k) < width]
        if short and all(k is None or k.isdigit() for k in rk):
            pad = width
    if pad:
        before = rk
        rk = [k.zfill(int(pad)) if (k and k.isdigit() and len(k) < int(pad)) else k for k in rk]
        n = sum(1 for a, b in zip(before, rk) if a != b)
        if n:
            repairs.append(f"{n} key(s) in {table_name} were zero-padded to {int(pad)} digits")
    raw_floaty = sum(1 for v in table[right] if isinstance(v, str) and _FLOAT_INT.match(v.strip())) + sum(1 for v in table[right] if isinstance(v, float) and v == int(v))
    if raw_floaty:
        repairs.append(f"{raw_floaty} key(s) written as decimals (like 4013.0) were read as whole numbers")
    for r in repairs:
        fs.append(finding("MAP-JOIN-005", "INFO", r))
    tbl = table.copy()
    tbl["__key"] = rk
    blank = int(tbl["__key"].isna().sum())
    if blank:
        fs.append(finding("MAP-JOIN-003", "WARN", f"{blank} row(s) of {table_name} have no key and were left out"))
        tbl = tbl[tbl["__key"].notna()]
    dup = tbl["__key"][tbl["__key"].duplicated(keep=False)]
    if len(dup):
        ex = sorted(set(dup))[:SHOW]
        raise JoinError(f"{table_name} repeats the key on {len(dup)} rows (for example {', '.join(ex)}); combine or filter them first, so each place has one row", "MAP-JOIN-002")
    keep = [c for c in (columns or [c for c in tbl.columns if c not in ("__key", right)]) if c != "__key"]
    missing = [c for c in keep if c not in tbl.columns]
    if missing:
        raise JoinError(f"{table_name} has no column(s) {', '.join(missing)}; columns are: {', '.join(c for c in table.columns)}")
    clash = [c for c in keep if c in gdf.columns and c != gdf.geometry.name]
    if clash:
        fs.append(finding("MAP-JOIN-006", "WARN", f"column(s) {', '.join(clash)} from {table_name} replace boundary column(s) of the same name", clash))
    sub = tbl[["__key"] + keep].copy()
    tried = numeric_columns(sub, keep, numeric)
    for c in tried:
        sub[c] = _to_number(sub[c])
    as_number = [c for c in tried if pd.api.types.is_numeric_dtype(sub[c])]       # a text column stays text even when it was tried
    kept_text = [c for c in keep if c not in tried]
    if kept_text and numeric is None:
        fs.append(finding("MAP-JOIN-005", "INFO", f"column(s) {', '.join(kept_text)} stay text (named like a code, or with leading zeros); name them in numeric: to map them as numbers", kept_text))
    out = gdf.copy()
    out["__key"] = lk
    out = out.drop(columns=[c for c in clash if c in out.columns])
    merged = out.join(sub.set_index("__key"), on="__key")
    key_matched = int(pd.Series(lk).isin(set(tbl["__key"])).sum())
    if key_matched == 0:
        ex_t = sorted({k for k in tbl["__key"]})[:3]
        ex_b = sorted({k for k in lk if k})[:3]
        raise JoinError(f"no row of {table_name} matched the boundary: its keys look like {', '.join(ex_t) or 'nothing'} and the boundary's like {', '.join(ex_b) or 'nothing'}; check '{left}' against '{right}'")
    bset = set(k for k in lk if k)
    tkeys = set(tbl["__key"])                    # built once (M16: it was rebuilt for every boundary feature, square in the table's size)
    unmatched_rows = sorted(tkeys - bset)
    no_row = [k for k in lk if k not in tkeys]
    if unmatched_rows:
        fs.append(finding("MAP-JOIN-003", "WARN", f"{len(unmatched_rows)} row(s) of {table_name} matched no boundary feature (for example {', '.join(unmatched_rows[:SHOW])}); they are not on the map", unmatched_rows[:50]))
    if no_row:
        fs.append(finding("MAP-JOIN-004", "WARN", f"{len(no_row)} of {len(lk)} boundary feature(s) have no row in {table_name}; they are drawn as No data", len(no_row)))
    merged = merged.drop(columns=["__key"])
    steps.append(f"joined {table_name} ({len(tbl)} rows) to the boundary on {left} = {right}: {key_matched} of {len(lk)} features matched" + (f" ({'; '.join(repairs)})" if repairs else ""))
    if no_row:
        notes.append(f"{len(no_row)} feature(s) have no value in {table_name} and are shown as No data.")
    report = {"table": table_name, "rows": int(len(tbl)), "features": int(len(lk)), "matched": key_matched, "unmatched_rows": len(unmatched_rows), "features_without_row": len(no_row),
              "columns": keep, "numeric": as_number, "repairs": repairs}
    return merged, steps, notes, fs, report


def _list(v) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [c.strip() for c in v.split(",") if c.strip()]
    return [str(c) for c in v]


def library_table(t: dict, layer_id: str) -> Path:
    """The cached table a library values entry (an ACS table, the FCC BDC summary) gives for {library, params, derive}: fetched by the entry's tool
    the first time (on this computer, with the person's key when the entry's route needs one), read from the cache after."""
    import ref_fetch as rf
    eid = t.get("library")
    if not eid:
        raise JoinError(f"layer '{layer_id}': join.table as a mapping needs `library: ENTRY_ID`")
    try:
        e = rf.find_entry(eid)
        params = dict(t.get("params") or {})
        if t.get("derive"):
            params["derive"] = dict(t["derive"])
        res = rf.fetch_by_tool(e, params)
    except rf.FetchError as ex:
        raise JoinError(f"layer '{layer_id}': {eid}: {ex}") from None
    return Path(res["path"])


def apply_spec(gdf, join_spec: dict, base: Path, layer_id: str, numeric: list[str] | None = None, engine: str | None = None):
    """Run a layer's `join:` block. Returns (gdf, steps, notes, findings, report, table_sha256); raises JoinError.
    `numeric` (or the block's own `numeric:` list) names the columns to read as numbers: the layer's value and denominator fields.
    `engine` (the layer's `engine:`, or the block's own) says what reads a CSV table (M16); the report's `engine` says what did."""
    if not join_spec or not join_spec.get("table"):
        raise JoinError(f"layer '{layer_id}': join needs table, left and right")
    for k in ("left", "right"):
        if not join_spec.get(k):
            raise JoinError(f"layer '{layer_id}': join needs '{k}'")
    if isinstance(join_spec["table"], dict):         # 1.3.0: {library: ID, params: {...}, derive: {...}}, fetched (or read from the cache) by the entry's tool
        p = library_table(join_spec["table"], layer_id)
    else:
        p = Path(join_spec["table"])
        p = p if p.is_absolute() else Path(base) / p
    eng: dict = {}
    table = read_table(p, join_spec.get("sheet"), engine=join_spec.get("engine") or engine, record=eng)
    cols = _list(join_spec.get("columns"))
    num = _list(join_spec.get("numeric"))
    if numeric:
        num = list(dict.fromkeys((num or []) + list(numeric)))
    g, steps, notes, fs, rep = join(gdf, table, str(join_spec["left"]), str(join_spec["right"]), columns=cols, pad=join_spec.get("pad"), table_name=p.name, numeric=num)
    if eng:
        rep["engine"] = eng
        if eng.get("engine") == "duckdb":
            steps.append(f"the table {p.name} ({eng.get('rows', 0):,} rows) was read by DuckDB, every column as text ({eng.get('why')})")
    return g, steps, notes, fs, rep, sha256_of(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("table")
    i.add_argument("--sheet")
    j = sub.add_parser("join")
    j.add_argument("boundary")
    j.add_argument("table")
    j.add_argument("--left", required=True)
    j.add_argument("--right", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--sheet")
    j.add_argument("--columns")
    j.add_argument("--pad", type=int)
    j.add_argument("--numeric", help="comma-separated columns to read as numbers (the value and denominator); others stay text when code-like")
    j.add_argument("--layer")
    j.add_argument("--overwrite", action="store_true")
    j.add_argument("--engine", choices=["auto", "pandas", "duckdb"], help="what reads a CSV table (default auto: DuckDB above 200,000 rows when installed)")
    a = ap.parse_args(argv)
    try:
        if a.op == "inspect":
            t = read_table(a.table, a.sheet)
            print(json.dumps({"sheets": sheets(a.table), "rows": len(t), "columns": list(t.columns), "head": t.head(5).astype(str).to_dict("records")}, indent=2))
            return 0
        import geo_io
        b = geo_io.read(a.boundary, layer=a.layer).gdf
        eng: dict = {}
        g, steps, notes, fs, rep = join(b, read_table(a.table, a.sheet, engine=a.engine, record=eng), a.left, a.right, columns=a.columns.split(",") if a.columns else None,
                                        pad=a.pad, table_name=Path(a.table).name, numeric=_list(a.numeric))
        if eng:
            rep["engine"] = eng
        geo_io.write(g, a.out, overwrite=a.overwrite)
        print(json.dumps({"written": a.out, "report": rep, "steps": steps, "findings": fs}, indent=2))
        return 0
    except JoinError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.fid, "BLOCK", str(ex))]}, indent=2))
        return 3


if __name__ == "__main__":
    sys.exit(main())
