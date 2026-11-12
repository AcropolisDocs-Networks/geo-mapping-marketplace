#!/usr/bin/env python3
"""census_query: American Community Survey tables beyond population, fetched by variable (1.3.0, R1-ACS).

    census_query.py variables --dataset acs/acs5 --year 2024 --group B28002
    census_query.py plan|fetch --dataset acs/acs5 --year 2024 (--group B28002 | --variables B28002_001E,B28002_004E) \\
                    --level tract --in state:04 [--moe] [--route api|summary-file|auto] [--derive 'no_bb_pct=(B28002_001E - B28002_004E) / B28002_001E * 100']

Variables. GET /data/{year}/{dataset}/groups/{group}.json (labels and concepts), cached per year. An unknown variable is MAP-ACS-001 (BLOCK),
with the nearest names.

Query building. `get=` lists of at most 50 variables a call (the Census API's documented limit, read 2026-11-08); longer lists are split,
merged on the geography columns and reported (MAP-ACS-002 INFO). `for=`/`in=` come from the level (block groups need in=county:* and
in=tract:*). GEOIDs are built per level with their leading zeros; estimates are numbers.

Jam values. The API's special negative codes become missing values, counted per code with its meaning (MAP-ACS-003 INFO).

Derived measures. `derive: {name: "(A - B) / A * 100"}` in a small grammar (variable names, numbers, + - * / and brackets), nothing else
evaluated. Margins of error are carried with the ACS handbook's formulas: a sum or difference takes the square root of the sum of squared MOEs; a
quotient whose values are all proportions (0 to 1) takes the proportion formula, falling back to the ratio formula where the term under the
root is negative (counted); a product of two estimates the product formula; a constant scales the MOE. The coefficient of variation
(MOE / 1.645 / estimate) is written beside each measure; values with CV over 30 percent are counted (MAP-ACS-004 WARN with the share).

Keyless route. With no `census` credential, or --route summary-file, the table comes from the ACS table-based Summary File for that year and
table (acsdt5y{year}-{table}.dat, pipe-delimited, GEO_ID first), filtered by the GEO_ID prefix of the level and state. `auto` uses the API when
a key is registered and the Summary File otherwise (MAP-ACS-005 INFO says which). Every request goes through SafeHTTP (GET, public addresses,
capped, audited); the key is added at send time only and masked in every record.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

API = "https://api.census.gov/data"
SF_URL = "https://www2.census.gov/programs-surveys/acs/summary_file/{year}/table-based-SF/data/5YRData/acsdt5y{year}-{table}.dat"
MAX_VARS = 50
VAR = re.compile(r"^[A-Z]\d{5}[A-Z]?_\d{3}[EM]$")
GROUP = re.compile(r"^[A-Z]\d{5}[A-Z]?$")
DATASET = re.compile(r"^acs/acs[15](?:/subject|/profile)?$")
LEVELS = {"state": "0400000US", "county": "0500000US", "tract": "1400000US", "block group": "1500000US", "place": "1600000US",
          "cbsa": "310M700US", "zcta": "860Z200US"}
GEO_COLS = {"state": ["state"], "county": ["state", "county"], "tract": ["state", "county", "tract"],
            "block group": ["state", "county", "tract", "block group"], "place": ["state", "place"]}
# The API's notes on its special values (read from the Census Data API's ACS notes; kept here as the plugin's table of their meanings)
JAM = {-666666666: "the estimate could not be computed: too few sample observations",
       -999999999: "the estimate or its margin cannot be displayed: too few sample observations",
       -888888888: "not applicable: the margin of error is not appropriate for this estimate",
       -555555555: "the estimate is controlled: no sampling error (margin shown as missing)",
       -333333333: "the median falls in the lowest or highest interval of an open-ended distribution",
       -222222222: "the margin of error could not be computed: too few sample observations"}
CV_LIMIT = 30.0
Z90 = 1.645


class QueryError(Exception):
    def __init__(self, message: str, rule: str = "MAP-ACS-001", code: int = gc.EXIT_USAGE):
        super().__init__(message)
        self.rule, self.code = rule, code


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def cache_dir() -> Path:
    d = gc.outputs_dir() / "cache" / "census"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http(http=None):
    import ogc_http as oh
    return http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": oh.CEILING["max_bytes"], "retries": 1})


def entry() -> dict:
    import ref_fetch as rf
    try:
        return rf.find_entry("ref.statistic.us.acs5.table")
    except rf.FetchError:
        return {"id": "ref.statistic.us.acs5.table", "delivery": {"requires_credential": {"name": "census", "host": "api.census.gov", "inject": {"query": "key"}}}}


def _credential(url: str, store=None, need: bool = True):
    """The registered `census` credential for an API request, or None (auto falls back to the Summary File)."""
    import ref_fetch as rf
    try:
        return rf.credential_for(entry(), url, store)
    except rf.FetchError as ex:
        if need:
            raise QueryError(str(ex), ex.rule or "MAP-CRED-013", ex.code) from None
        return None


# ---------------------------------------------------------------------------------------------------------------- variables
def check_args(dataset: str, year: int) -> None:
    if not DATASET.match(str(dataset)):
        raise QueryError(f"dataset '{dataset}' is not an ACS dataset (acs/acs5, acs/acs1, with /subject or /profile)", "MAP-ACS-001")
    if not (2009 <= int(year) <= 2100):
        raise QueryError(f"year {year} is not an ACS year", "MAP-ACS-001")


def variables(dataset: str, year: int, group: str, *, http=None, store=None, refresh: bool = False) -> dict:
    """{name: {label, concept}} of one group, from /groups/{group}.json, cached per year and dataset."""
    check_args(dataset, year)
    if not GROUP.match(str(group)):
        raise QueryError(f"group '{group}' is not an ACS table id such as B28002", "MAP-ACS-001")
    cp = cache_dir() / f"{year}-{dataset.replace('/', '_')}-{group}.json"
    if cp.is_file() and not refresh:
        return json.loads(cp.read_text(encoding="utf-8"))
    url = f"{API}/{int(year)}/{dataset}/groups/{group}.json"
    import ogc_http as oh
    h = _http(http)
    try:
        r = h.get(url, expect="json")
    except oh.ServiceError as ex:
        raise QueryError(f"the Census API did not list group {group} for {year}: {ex}", ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
    doc = json.loads(r.content)
    out = {k: {"label": oh.untrusted(v.get("label"), 300), "concept": oh.untrusted(v.get("concept"), 200)} for k, v in (doc.get("variables") or {}).items() if VAR.match(k)}
    cp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def resolve_variables(dataset, year, group=None, names=None, moe=False, *, http=None, store=None) -> tuple[list[str], dict]:
    """The variable list asked for, checked against the groups listing (MAP-ACS-001 with the nearest names for one that does not exist)."""
    groups = {group} if group else {n.split("_")[0] for n in names or []}
    known: dict = {}
    for g in sorted(groups):
        known.update(variables(dataset, year, g, http=http, store=store))
    if group and not names:
        names = sorted(k for k in known if k.endswith("E"))
    names = [n.strip() for n in names or [] if n.strip()]
    for n in names:
        if n not in known:
            near = difflib.get_close_matches(n, list(known), n=3)
            raise QueryError(f"variable {n} is not in {', '.join(sorted(groups))} for {year}" + (f"; the nearest are {', '.join(near)}" if near else ""), "MAP-ACS-001")
    if moe:
        names = names + [n[:-1] + "M" for n in names if n.endswith("E") and n[:-1] + "M" not in names]
    return names, known


def geography(level: str, within: str | None) -> tuple[str, list[str]]:
    """(for=, [in=...]) for a level: block groups need in=county:* and in=tract:* (the B01003 entry's pattern)."""
    level = level.replace("_", " ").replace("block-group", "block group")
    if level not in GEO_COLS:
        raise QueryError(f"level '{level}' is not one of {', '.join(GEO_COLS)}", "MAP-ACS-001")
    st = None
    if within:
        m = re.fullmatch(r"state:(\d{2})", str(within).strip())
        if not m:
            raise QueryError(f"--in '{within}' must be state:NN (a state FIPS code)", "MAP-ACS-001")
        st = m.group(1)
    if level == "state":
        return f"state:{st or '*'}", []
    if not st:
        raise QueryError(f"a {level} query needs --in state:NN", "MAP-ACS-001")
    if level == "block group":
        return "block group:*", [f"state:{st}", "county:*", "tract:*"]
    if level == "tract":
        return "tract:*", [f"state:{st}", "county:*"]
    return f"{level}:*", [f"state:{st}"]


def requests_for(dataset, year, names, level, within) -> list[str]:
    """The API addresses, at most MAX_VARS variables each (NAME counts as one in the first)."""
    fr, ins = geography(level, within)
    chunks, first = [], True
    rest = list(names)
    while rest:
        room = MAX_VARS - (1 if first else 0)
        part, rest = rest[:room], rest[room:]
        get = (["NAME"] if first else []) + part
        q = [("get", ",".join(get)), ("for", fr)] + [("in", x) for x in ins]
        chunks.append(f"{API}/{int(year)}/{dataset}?" + urllib.parse.urlencode(q, safe=",:*"))
        first = False
    return chunks


# ---------------------------------------------------------------------------------------------------------------- derived measures and MOE
_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Name, ast.Constant, ast.Load)


def parse(expr: str):
    """The expression as an AST of names, numbers, + - * / and brackets only (QueryError otherwise)."""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as ex:
        raise QueryError(f"derive: '{expr}' is not an expression of variables, numbers, + - * / and brackets ({ex.msg})", "MAP-ACS-001") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED) or (isinstance(node, ast.Constant) and not isinstance(node.value, (int, float))):
            raise QueryError(f"derive: '{expr}' may hold only variables, numbers, + - * / and brackets", "MAP-ACS-001")
    return tree.body


def names_in(expr: str) -> list[str]:
    return sorted({n.id for n in ast.walk(parse(expr)) if isinstance(n, ast.Name)})


def evaluate(expr: str, est: dict, moe: dict) -> tuple:
    """(estimate, margin of error, notes) as numpy arrays for one derived measure (ACS handbook, chapter 8 formulas)."""
    import numpy as np
    notes = {"ratio_fallback": 0}

    def ev(node):
        if isinstance(node, ast.Constant):
            return float(node.value), 0.0, True
        if isinstance(node, ast.Name):
            if node.id not in est:
                raise QueryError(f"derive: '{node.id}' is not a fetched variable", "MAP-ACS-001")
            return np.asarray(est[node.id], dtype="float64"), np.asarray(moe.get(node.id, np.zeros(len(est[node.id]))), dtype="float64"), False
        if isinstance(node, ast.UnaryOp):
            e, m, c = ev(node.operand)
            return (-e if isinstance(node.op, ast.USub) else e), m, c
        a, ma, ca = ev(node.left)
        b, mb, cb = ev(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return (a + b if isinstance(node.op, ast.Add) else a - b), np.sqrt(np.square(ma) + np.square(mb)), ca and cb
        if isinstance(node.op, ast.Mult):
            if ca or cb:
                return a * b, (np.abs(b) * ma if cb else np.abs(a) * mb), ca and cb
            return a * b, np.sqrt(np.square(a) * np.square(mb) + np.square(b) * np.square(ma)), False
        if cb:                                           # division by a constant
            return a / b, ma / abs(b), ca
        with np.errstate(divide="ignore", invalid="ignore"):
            p = a / b
            fin = np.isfinite(p)
            if fin.any() and np.all((p[fin] >= 0) & (p[fin] <= 1)):     # a proportion: the numerator is part of the denominator
                under = np.square(ma) - np.square(p) * np.square(mb)
                neg = under < 0
                notes["ratio_fallback"] += int(np.sum(neg & fin))
                m = np.where(neg, np.sqrt(np.square(ma) + np.square(p) * np.square(mb)), np.sqrt(np.clip(under, 0, None))) / np.abs(b)
            else:                                        # a ratio
                m = np.sqrt(np.square(ma) + np.square(p) * np.square(mb)) / np.abs(b)
        return p, m, False

    e, m, _ = ev(parse(expr))
    n = len(next(iter(est.values()))) if est else 1
    e = np.broadcast_to(np.asarray(e, dtype="float64"), (n,)).copy()
    m = np.broadcast_to(np.asarray(m, dtype="float64"), (n,)).copy()
    return e, m, notes


def cv(est, moe):
    import numpy as np
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.asarray(est) != 0, np.asarray(moe) / Z90 / np.abs(np.asarray(est)) * 100.0, np.nan)


# ---------------------------------------------------------------------------------------------------------------- the routes
def _jam(df, fs: list) -> None:
    import pandas as pd
    counts: dict = {}
    for c in [c for c in df.columns if VAR.match(str(c))]:
        v = pd.to_numeric(df[c], errors="coerce")
        for code in JAM:
            k = int((v == code).sum())
            if k:
                counts[code] = counts.get(code, 0) + k
        df[c] = v.mask(v.isin(list(JAM)))
    if counts:
        fs.append(finding("MAP-ACS-003", "INFO", "Census special values left empty: " + "; ".join(f"{n} x {code} ({JAM[code]})" for code, n in sorted(counts.items())),
                          {str(k): v for k, v in counts.items()}))


def _geoid(df, level: str) -> None:
    cols = [c for c in GEO_COLS.get(level, []) if c in df.columns]
    if cols:
        df["GEOID"] = df[cols].astype(str).agg("".join, axis=1)


def via_api(dataset, year, names, level, within, *, http=None, store=None, fs: list) -> tuple:
    import ogc_http as oh
    import pandas as pd
    urls = requests_for(dataset, year, names, level, within)
    if len(urls) > 1:
        fs.append(finding("MAP-ACS-002", "INFO", f"{len(names)} variables are more than {MAX_VARS} a call: split into {len(urls)} calls and merged on the geography columns"))
    auth = _credential(urls[0], store)
    h = _http(http)
    frames = []
    try:
        for u in urls:
            try:
                r = h.get(u, expect="json", auth=auth)
            except oh.ServiceError as ex:
                raise QueryError(str(ex), ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
            rows = json.loads(r.content)
            if not (isinstance(rows, list) and rows and all(isinstance(x, list) for x in rows)):
                raise QueryError("the Census API did not answer with a table", "MAP-SVC-013", gc.EXIT_SERVICE)
            frames.append(pd.DataFrame(rows[1:], columns=[str(c) for c in rows[0]]))
    finally:
        if auth is not None:
            auth.close()
    geo = [c for c in GEO_COLS[level.replace("_", " ")] if c in frames[0].columns]
    df = frames[0]
    for f_ in frames[1:]:
        df = df.merge(f_[[c for c in f_.columns if c not in df.columns or c in geo]], on=geo, how="outer")
    calls = [dict(c) for c in h.audit.calls]
    return df, calls, (auth.record() if auth is not None else None)


def via_summary_file(dataset, year, names, level, within, *, http=None, fs: list) -> tuple:
    """The table-based Summary File: one GET of the table's national .dat file (capped and audited), read line by line and kept only for the
    rows whose GEO_ID starts with the level's and state's prefix, so the national table is never held as a frame."""
    import io

    import ogc_http as oh
    import pandas as pd
    if dataset != "acs/acs5":
        raise QueryError("the keyless Summary File route serves acs/acs5 only; register a census key for other datasets", "MAP-ACS-001")
    tables = sorted({n.split("_")[0] for n in names})
    level = level.replace("_", " ")
    st = re.fullmatch(r"state:(\d{2})", str(within or "")).group(1) if within else None
    prefix = LEVELS[level] + (st or "")
    h = _http(http)
    frames = []
    for t in tables:
        url = SF_URL.format(year=int(year), table=t.lower())
        try:
            r = h.get(url, expect="text")
        except oh.ServiceError as ex:
            raise QueryError(f"the Summary File for {t} {year} could not be read: {ex}", ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
        text = io.StringIO(r.content.decode("utf-8", "replace"))
        header = text.readline().rstrip("\r\n").split("|")
        if "GEO_ID" not in header:
            raise QueryError(f"the Summary File for {t} has no GEO_ID column (its layout changed?)", "MAP-SVC-013", gc.EXIT_SERVICE)
        gi = header.index("GEO_ID")
        keep = [ln.rstrip("\r\n").split("|") for ln in text if ln.split("|", gi + 1)[gi].startswith(prefix)]
        df = pd.DataFrame(keep, columns=header)
        ren = {}
        for c in df.columns:                              # B28002_E001 -> B28002_001E, B28002_M001 -> B28002_001M
            m = re.fullmatch(r"([A-Z]\d{5}[A-Z]?)_([EM])(\d{3})", c)
            if m:
                ren[c] = f"{m.group(1)}_{m.group(3)}{m.group(2)}"
        df = df.rename(columns=ren)
        frames.append(df[["GEO_ID"] + [n for n in names if n in df.columns and n.startswith(t)]])
    df = frames[0]
    for f_ in frames[1:]:
        df = df.merge(f_, on="GEO_ID", how="outer")
    missing = [n for n in names if n not in df.columns]
    if missing:
        raise QueryError(f"the Summary File has no column for {', '.join(missing[:5])}", "MAP-ACS-001")
    df["GEOID"] = df["GEO_ID"].str.split("US").str[-1]
    calls = [dict(c) for c in h.audit.calls]
    return df, calls, None


def plan(dataset: str, year: int, *, group=None, variables_=None, level="tract", within=None, moe=False, route="auto", derive=None, store=None, http=None) -> dict:
    """What a fetch will send: the requests (key=*** on the API route), the route and why."""
    check_args(dataset, year)
    names, _ = resolve_variables(dataset, year, group, variables_, moe, http=http, store=store)
    for expr in (derive or {}).values():
        missing = [n for n in names_in(expr) if n not in names]
        if missing:
            raise QueryError(f"derive needs {', '.join(missing)}, which is not asked for", "MAP-ACS-001")
    use, why = choose_route(route, store)
    if use == "api":
        reqs = [u + "&key=***" for u in requests_for(dataset, year, names, level, within)]
    else:
        reqs = [SF_URL.format(year=int(year), table=t.lower()) for t in sorted({n.split("_")[0] for n in names})]
    return {"route": use, "why": why, "variables": names, "requests": reqs, "level": level, "in": within, "calls": len(reqs)}


def choose_route(route: str, store=None) -> tuple[str, str]:
    if route in ("api", "summary-file"):
        return route, f"--route {route}"
    import credentials as cr
    try:
        r = cr.resolve("cred:census", store=store, host="api.census.gov")
        r.close()
        ok = True
    except Exception:  # noqa: BLE001 - no key registered (or it cannot be read): the keyless route
        ok = False
    return ("api", "a census key is registered") if ok else ("summary-file", "no census key is registered, so the keyless Summary File")


def fetch(dataset: str, year: int, *, group=None, variables_=None, level="tract", within=None, moe=False, route="auto", derive=None,
          refresh=False, store=None, http=None) -> dict:
    """The table cached as Parquet with a GEOID column (and derived measures with their MOE and CV), a record beside it. -> summary, never rows."""
    import numpy as np
    import pandas as pd
    fs: list = []
    p = plan(dataset, year, group=group, variables_=variables_, level=level, within=within, moe=moe or bool(derive), route=route, derive=derive, store=store, http=http)
    names = p["variables"]
    fs.append(finding("MAP-ACS-005", "INFO", f"route: {p['route']} ({p['why']})"))
    key = hashlib.sha256(json.dumps([dataset, year, names, level, within, p["route"], derive], sort_keys=True).encode()).hexdigest()[:16]
    out = cache_dir() / f"acs-{year}-{(group or names[0].split('_')[0])}-{level.replace(' ', '-')}-{(within or 'us').replace(':', '')}-{key}.parquet"
    if out.is_file() and not refresh:
        rec = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        return {"ok": True, "path": str(out), "record": str(out.with_suffix(".json")), "cached": True, "rows": rec["rows"], "columns": rec["columns"],
                "route": p["route"], "findings": rec.get("findings", fs)}
    if p["route"] == "api":
        df, calls, auth_rec = via_api(dataset, year, names, level, within, http=http, store=store, fs=fs)
    else:
        df, calls, auth_rec = via_summary_file(dataset, year, names, level, within, http=http, fs=fs)
    _jam(df, fs)
    if "GEOID" not in df.columns:
        _geoid(df, level.replace("_", " "))
    for n in names:
        df[n] = pd.to_numeric(df[n], errors="coerce")
    for name, expr in (derive or {}).items():
        need = names_in(expr)
        est = {n: df[n].to_numpy(dtype="float64") for n in need}
        moes = {n: df[n[:-1] + "M"].to_numpy(dtype="float64") if n[:-1] + "M" in df.columns else np.zeros(len(df)) for n in need}
        e, m, notes = evaluate(expr, est, moes)
        df[name] = e
        df[f"{name}_moe"] = m
        df[f"{name}_cv"] = cv(e, m)
        if notes["ratio_fallback"]:
            fs.append(finding("MAP-ACS-004", "INFO", f"{name}: the proportion formula's root was negative for {notes['ratio_fallback']} feature(s); the ratio formula was used for them (ACS handbook)"))
        c = df[f"{name}_cv"].to_numpy()
        ok = np.isfinite(c)
        if ok.any():
            share = float((c[ok] > CV_LIMIT).mean())
            if share > 0:
                fs.append(finding("MAP-ACS-004", "WARN", f"{name}: {share:.0%} of the features have a coefficient of variation over {CV_LIMIT:g}%; classes built on "
                                  "them are not reliable (show the margins, or use a larger geography)", {"share": round(share, 4)}))
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    import redact
    rec = {"fetch_version": 1, "dataset": dataset, "year": int(year), "group": group, "variables": names, "level": level, "in": within, "route": p["route"],
           "requests": p["requests"], "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or __import__("datetime").date.today().isoformat(),
           "auth": auth_rec, "calls": calls, "derive": derive or {}, "rows": int(len(df)), "columns": list(df.columns),
           "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "findings": fs,
           "provenance": {"library_ref": "ref.statistic.us.acs5.table", "license": "public-domain",
                          "attribution_text": "Source: U.S. Census Bureau, American Community Survey 5-Year Estimates. This product uses the Census Bureau Data API but is "
                                              "not endorsed or certified by the Census Bureau." if p["route"] == "api" else
                                              "Source: U.S. Census Bureau, American Community Survey 5-Year Estimates (table-based Summary File)."}}
    rec, _ = redact.scrub_obj(rec)
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "path": str(out), "record": str(out.with_suffix(".json")), "cached": False, "rows": int(len(df)), "columns": list(df.columns),
            "route": p["route"], "findings": fs, "join": "a table without geometry: join it to a boundary layer on GEOID"}


def from_library(entry_: dict, params: dict, *, refresh=False, store=None, http=None) -> dict:
    """A curated entry (ref.statistic.us.acs5.broadband and the rest) fetched with its own group and the caller's params {level, state | in, year,
    route, moe, derive}: the route `ref_fetch` and a MapSpec join use."""
    d = entry_.get("delivery") or {}
    q = dict(d.get("query") or {})
    q.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    st = q.get("state")
    within = q.get("in") or (f"state:{fips(st)}" if st else None)
    return fetch(q.get("dataset", "acs/acs5"), int(q.get("year", 2024)), group=q.get("group"), variables_=q.get("variables"), level=q.get("level", "tract"),
                 within=within, moe=bool(q.get("moe", True)), route=q.get("route", "auto"), derive=q.get("derive"), refresh=refresh, store=store, http=http)


STATES = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
          "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
          "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
          "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
          "WV": "54", "WI": "55", "WY": "56", "PR": "72"}


STATE_NAMES = dict(zip(STATES, ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "District of Columbia",
                                 "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
                                 "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
                                 "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
                                 "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
                                 "Washington", "West Virginia", "Wisconsin", "Wyoming", "Puerto Rico"]))


def fips(state) -> str:
    s = str(state).strip().upper()
    if re.fullmatch(r"\d{2}", s):
        return s
    if s in STATES:
        return STATES[s]
    raise QueryError(f"state '{state}' is not a two-letter code or a FIPS code", "MAP-ACS-001")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["variables", "plan", "fetch"])
    ap.add_argument("--dataset", default="acs/acs5")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--group")
    ap.add_argument("--variables")
    ap.add_argument("--level", default="tract")
    ap.add_argument("--in", dest="within")
    ap.add_argument("--moe", action="store_true")
    ap.add_argument("--route", default="auto", choices=["auto", "api", "summary-file"])
    ap.add_argument("--derive", action="append", default=[], help="NAME=EXPRESSION")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    derive = dict(x.split("=", 1) for x in a.derive) if a.derive else None
    try:
        if a.op == "variables":
            res = variables(a.dataset, a.year, a.group)
        elif a.op == "plan":
            res = plan(a.dataset, a.year, group=a.group, variables_=(a.variables or "").split(",") if a.variables else None, level=a.level, within=a.within,
                       moe=a.moe, route=a.route, derive=derive)
        else:
            res = fetch(a.dataset, a.year, group=a.group, variables_=(a.variables or "").split(",") if a.variables else None, level=a.level, within=a.within,
                        moe=a.moe, route=a.route, derive=derive, refresh=a.refresh)
    except QueryError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return ex.code if isinstance(ex.code, int) else 3
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
