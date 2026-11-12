#!/usr/bin/env python3
"""fcc_bdc: the FCC Broadband Data Collection public data API with the person's own `fcc-bdc` key (1.3.0, R1-FCC).

    fcc_bdc.py dates   [--type availability|challenge]
    fcc_bdc.py list    --as-of 2026-06-30 [--category Summary|State|Provider] [--subcategory TEXT] [--technology "Fixed Broadband"] [--state AZ]
    fcc_bdc.py fetch   FILE_ID [--type availability] [--format csv|shapefile|gpkg] [--confirm-large]
    fcc_bdc.py summary --as-of 2026-06-30 --geography state|county|tract|place|cd|tribal --state AZ --technology wired-lfw --speed 100/20 --biz-res R

Host bdc.fcc.gov, headers `username` and `hash_value` (the credential's user and key, added at send time), 10 calls a minute on each API. Every call
is a GET that the library entry `ref.public-service.us.broadband.fcc-bdc-api` declares (delivery.api), through SafeHTTP.request: public
addresses, capped, audited, the key only to bdc.fcc.gov. The rate limit is the shared bucket of ratelimit.py, so parallel processes share it; a
call waits for a slot rather than failing (MAP-FCC-004 INFO when it waited); a 429 waits 60 s and is tried once more.

The list returns file ids, names, categories, technologies, states and sizes, never rows. A download over 100 MB needs a confirmed run (map-run
shows the cost card; MAP-FCC-005). A downloaded zip's CSV is checked against the layout the summary entry declares (MAP-FCC-006: a changed FCC
layout is never read wrongly). `summary` is the Day 9 table in one command: the "Fixed Broadband Summary by Geography Type" rows for one geography,
state, technology, speed tier and residential or business units, as a Parquet table with a geography id ready to join to the library's tracts and
counties.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

API_ENTRY = "ref.public-service.us.broadband.fcc-bdc-api"
SUMMARY_ENTRY = "ref.statistic.us.broadband.fcc-bdc-summary"
BASE = "https://bdc.fcc.gov/api/public/map"
CAP_BYTES = 100 * 1024 * 1024
HARD_CAP = 190 * 1024 * 1024
RETRY_429_S = 60.0
TECH = {"wired-lfw": "All Wired and Licensed Fixed Wireless", "wired": "All Wired", "lfw": "Licensed Fixed Wireless", "fiber": "Fiber",
        "cable": "Cable", "copper": "Copper", "all": "All Technologies"}
GEOGRAPHY = {"state": "state", "county": "county", "tract": "census tract", "place": "place", "cd": "congressional district", "tribal": "tribal",
             "cbsa": "cbsa", "block group": "block group"}


class BDCError(Exception):
    def __init__(self, message: str, rule: str = "MAP-FCC-003", code: int = gc.EXIT_LINT):
        super().__init__(message)
        self.rule, self.code = rule, code


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _entry(eid: str) -> dict:
    import ref_fetch as rf
    return rf.find_entry(eid)


class Client:
    """The BDC API: the entry's declared GETs, the person's key at send time, the shared rate limit, and what each call waited."""

    def __init__(self, *, http=None, store=None, sleep=time.sleep):
        import declared as dcl
        import ogc_http as oh
        self.entry = _entry(API_ENTRY)
        self.system = dcl.from_entry(self.entry)
        if self.system is None:
            raise BDCError(f"{API_ENTRY} declares no requests (delivery.api)", "MAP-FCC-001", gc.EXIT_USAGE)
        self.sleep = sleep
        self.http = http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": HARD_CAP, "retries": 0}, sleep=sleep)
        self.store = store
        self.findings: list = []
        self._auth = None

    def auth(self):
        if self._auth is None:
            import ref_fetch as rf
            try:
                self._auth = rf.credential_for(self.entry, BASE + "/listAsOfDates", self.store)
            except rf.FetchError as ex:
                raise BDCError(f"no usable fcc-bdc credential: {ex}. Register yours with map-credentials add fcc-bdc (your FCC username and API token)",
                               "MAP-FCC-001", gc.EXIT_REFUSED) from None
        return self._auth

    def close(self):
        if self._auth is not None:
            self._auth.close()
            self._auth = None

    def get(self, path: str, params: dict | None = None, *, expect: str = "json", max_bytes: int | None = None):
        import ogc_http as oh
        url = BASE + path
        for attempt in (0, 1):
            try:
                n = len(self.http.audit.calls)
                r = self.http.request("GET", url, system=self.system, params={k: v for k, v in (params or {}).items() if v not in (None, "")}, expect=expect,
                                      auth=self.auth(), max_bytes=max_bytes)
                waited = sum(float(c.get("waited_s") or 0) for c in self.http.audit.calls[n:])
                if waited:
                    self.findings.append(finding("MAP-FCC-004", "INFO", f"a call waited {waited:.0f} s for the FCC's limit of 10 calls a minute"))
                return r
            except oh.ServiceError as ex:
                if ex.status == 429 and attempt == 0:
                    self.sleep(RETRY_429_S)
                    continue
                raise BDCError(str(ex), ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
        raise BDCError("the FCC API answered 429 twice", "MAP-SVC-010", gc.EXIT_SERVICE)


def _rows(doc) -> list[dict]:
    if isinstance(doc, dict):
        for k in ("data", "results", "items"):
            if isinstance(doc.get(k), list):
                return [x for x in doc[k] if isinstance(x, dict)]
    return [x for x in doc if isinstance(x, dict)] if isinstance(doc, list) else []


def dates(client: Client, kind: str | None = None) -> list[dict]:
    r = client.get("/listAsOfDates")
    out = []
    for row in _rows(json.loads(r.content)):
        d = str(row.get("as_of_date") or row.get("asOfDate") or "")[:10]
        t = str(row.get("data_type") or row.get("dataType") or "").lower()
        if d and (not kind or t == kind):
            out.append({"as_of_date": d, "data_type": t})
    return out


def list_files(client: Client, as_of: str, *, category=None, subcategory=None, technology=None, state=None, kind: str = "availability") -> list[dict]:
    """The files of one as-of date (MAP-FCC-002 when the date is not listed). Each: file_id, name, category, subcategory, technology, state, size."""
    import ogc_http as oh
    known = {d["as_of_date"] for d in dates(client, kind)}
    if as_of not in known:
        raise BDCError(f"as-of date {as_of} is not one the FCC lists for {kind} data ({', '.join(sorted(known)[-4:]) or 'none listed'})", "MAP-FCC-002")
    path = f"/downloads/list{'Availability' if kind == 'availability' else 'Challenge'}Data/{as_of}"
    r = client.get(path, {"category": category, "subcategory": subcategory, "technology_type": technology})
    out = []
    st = str(state or "").strip().upper()
    for row in _rows(json.loads(r.content)):
        f = {"file_id": str(row.get("file_id") or row.get("fileId") or ""), "name": oh.untrusted(row.get("file_name") or row.get("fileName"), 200),
             "category": row.get("category"), "subcategory": oh.untrusted(row.get("subcategory"), 120), "technology": row.get("technology_type") or row.get("technology_code_desc"),
             "state": row.get("state_name") or row.get("state_fips"), "state_fips": row.get("state_fips"), "provider": row.get("provider_name"),
             "records": row.get("record_count"), "bytes": row.get("file_size") or row.get("size")}
        if st and f["state"] is not None:
            import census_query as cq
            want = {st, state_name(st).upper()} | ({cq.fips(st)} if st in cq.STATES or re.fullmatch(r"\d{2}", st) else set())
            if not ({str(f["state"]).upper(), str(f.get("state_fips") or "")} & want):
                continue
        if f["file_id"]:
            out.append(f)
    return out


def state_name(code: str) -> str:
    import census_query as cq
    return cq.STATE_NAMES.get(code.upper(), code)


def declared_columns() -> list[str]:
    e = _entry(SUMMARY_ENTRY)
    return [c for c in ((e.get("delivery") or {}).get("columns") or []) if c and not c.endswith("_as_published")]


def fetch_file(client: Client, file_id: str, *, kind: str = "availability", fmt: str = "csv", confirm_large: bool = False, check_layout: bool = False) -> dict:
    """One file into the library cache (zip, capped: 100 MB without a confirmed run, MAP-FCC-005) and its CSV read back; -> {path, csv, rows, columns}."""
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", str(file_id)):
        raise BDCError(f"file id '{file_id}' is not one the list returned", "MAP-FCC-003", gc.EXIT_USAGE)
    suffix = {"csv": "", "shapefile": "/1", "gpkg": "/2"}[fmt]
    cap = HARD_CAP if confirm_large else CAP_BYTES
    try:
        r = client.get(f"/downloads/downloadFile/{kind}/{file_id}{suffix}", expect="any", max_bytes=cap)
    except BDCError as ex:
        if "larger than" in str(ex):
            raise BDCError(f"file {file_id} is larger than {cap // 1_048_576} MB: it is fetched only in a confirmed run (map-run shows the cost first)", "MAP-FCC-005") from None
        raise
    cdir = gc.outputs_dir() / "cache" / "library" / "fcc-bdc"
    cdir.mkdir(parents=True, exist_ok=True)
    zp = cdir / f"{kind}-{file_id}.zip"
    zp.write_bytes(r.content)
    out = {"path": str(zp), "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()}
    if fmt == "csv":
        df = read_csv_zip(r.content)
        if check_layout:
            check_columns(list(df.columns))
        pq = zp.with_suffix(".parquet")
        df.to_parquet(pq, index=False)
        out.update(csv=str(pq), rows=int(len(df)), columns=list(df.columns))
    return out


def read_csv_zip(raw: bytes):
    import pandas as pd
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise BDCError("the download holds no CSV", "MAP-FCC-006")
            with z.open(names[0]) as fh:
                return pd.read_csv(fh, dtype=str)
    return pd.read_csv(io.BytesIO(raw), dtype=str)


def check_columns(cols: list[str]) -> None:
    want = declared_columns()
    missing = [c for c in want if c not in cols]
    if missing:
        raise BDCError(f"the CSV's layout is not the one {SUMMARY_ENTRY} declares: {', '.join(missing)} missing (the FCC may have changed it); "
                       "nothing was read", "MAP-FCC-006")


def summarize(df, *, geography: str, state: str | None, technology: str, speed: str, biz_res: str = "R"):
    """The summary rows for one geography, state, technology, speed tier and unit type: [geography_id, geography_desc, area_data_type, units,
    share] with share in percent."""
    import pandas as pd
    import census_query as cq
    check_columns(list(df.columns))
    col = "speed_" + "_".join(p.strip().zfill(2) if len(p.strip()) == 1 else p.strip() for p in str(speed).split("/"))
    if col not in df.columns:
        raise BDCError(f"speed {speed} has no column {col}; the tiers are {', '.join(c for c in df.columns if c.startswith('speed_'))}", "MAP-FCC-003")
    gtype = GEOGRAPHY.get(geography, geography).lower()
    tech = TECH.get(technology, technology).lower()
    d = df[(df["geography_type"].str.lower().str.contains(gtype, regex=False)) & (df["technology"].str.lower() == tech) & (df["biz_res"].str.upper() == biz_res.upper())]
    if state:
        fp = cq.fips(state)
        d = d[d["geography_id"].astype(str).str.startswith(fp) | (d["geography_type"].str.lower() == "tribal")]
    if not len(d):
        raise BDCError(f"no summary rows for {geography}, {state or 'all states'}, {TECH.get(technology, technology)}, {biz_res}", "MAP-FCC-003")
    share = pd.to_numeric(d[col], errors="coerce")
    if share.max() <= 1.0001:
        share = share * 100.0
    units = pd.to_numeric(d.get("total_units", pd.Series(index=d.index, dtype=float)), errors="coerce")
    return pd.DataFrame({"geography_id": d["geography_id"].astype(str).values, "geography_desc": d["geography_desc"].values,
                         "area_data_type": d["area_data_type"].values, "units": units.values, f"share_{col[6:]}": share.round(1).values})


def summary(client: Client, *, as_of: str, geography: str, state: str | None, technology: str = "wired-lfw", speed: str = "100/20", biz_res: str = "R",
            confirm_large: bool = False) -> dict:
    files = [f for f in list_files(client, as_of, category="Summary", technology="Fixed Broadband")
             if "summary by geography" in str(f["name"] or f["subcategory"] or "").lower() or "geography" in str(f["subcategory"] or "").lower()]
    if not files:
        raise BDCError(f"no 'Summary by Geography' file is listed for {as_of}", "MAP-FCC-003")
    got = fetch_file(client, files[0]["file_id"], confirm_large=confirm_large)
    import pandas as pd
    df = pd.read_parquet(got["csv"])
    out = summarize(df, geography=geography, state=state, technology=technology, speed=speed, biz_res=biz_res)
    pq = Path(got["path"]).with_name(f"summary-{as_of}-{geography}-{state or 'us'}-{technology}-{speed.replace('/', '_')}-{biz_res}.parquet")
    out.to_parquet(pq, index=False)
    rec = {"as_of": as_of, "file": files[0], "geography": geography, "state": state, "technology": TECH.get(technology, technology), "speed": speed,
           "biz_res": biz_res, "rows": int(len(out)), "sha256": got["sha256"], "calls": client.http.audit.calls,
           "provenance": {"library_ref": SUMMARY_ENTRY, "license": "public-domain", "attribution_text": "Source: FCC National Broadband Map (Broadband Data Collection)."}}
    import redact
    rec, _ = redact.scrub_obj(rec)
    pq.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str))
    return {"ok": True, "path": str(pq), "record": str(pq.with_suffix(".json")), "rows": int(len(out)), "columns": list(out.columns),
            "join": "join on geography_id to the library's Census boundaries (GEOID)", "findings": client.findings}


def from_library(entry_: dict, params: dict, *, store=None, http=None, refresh: bool = False) -> dict:
    """ref_fetch's hand-off for the summary entry: params {as_of (default: the latest availability date), geography (level), state (variant),
    technology, speed, biz_res}."""
    c = Client(http=http, store=store)
    try:
        as_of = params.get("as_of") or max(d["as_of_date"] for d in dates(c, "availability"))
        return summary(c, as_of=as_of, geography=params.get("geography") or params.get("level") or "county", state=params.get("state"),
                       technology=params.get("technology") or "wired-lfw", speed=params.get("speed") or "100/20", biz_res=params.get("biz_res") or "R")
    finally:
        c.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["dates", "list", "fetch", "summary"])
    ap.add_argument("file_id", nargs="?")
    ap.add_argument("--type", default="availability", choices=["availability", "challenge"])
    ap.add_argument("--as-of")
    ap.add_argument("--category")
    ap.add_argument("--subcategory")
    ap.add_argument("--technology")
    ap.add_argument("--state")
    ap.add_argument("--format", default="csv", choices=["csv", "shapefile", "gpkg"])
    ap.add_argument("--geography", default="county")
    ap.add_argument("--speed", default="100/20")
    ap.add_argument("--biz-res", default="R")
    ap.add_argument("--confirm-large", action="store_true")
    a = ap.parse_args(argv)
    c = None
    try:
        c = Client()
        if a.op == "dates":
            res = {"ok": True, "dates": dates(c, a.type)}
        elif a.op == "list":
            fl = list_files(c, a.as_of, category=a.category, subcategory=a.subcategory, technology=a.technology, state=a.state, kind=a.type)
            res = {"ok": True, "files": fl, "total_bytes": sum(int(f["bytes"] or 0) for f in fl), "findings": c.findings}
        elif a.op == "fetch":
            res = {"ok": True, **fetch_file(c, a.file_id, kind=a.type, fmt=a.format, confirm_large=a.confirm_large), "findings": c.findings}
        else:
            res = summary(c, as_of=a.as_of, geography=a.geography, state=a.state, technology=a.technology or "wired-lfw", speed=a.speed, biz_res=a.biz_res,
                          confirm_large=a.confirm_large)
    except BDCError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return ex.code
    finally:
        if c is not None:
            c.close()
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
