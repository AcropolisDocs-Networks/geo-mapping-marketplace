#!/usr/bin/env python3
"""cds_retrieve: Copernicus Climate Data Store retrieval with the person's own token (1.3.0, R1-CDS; owner's decision D1).

    cds_retrieve.py plan    DATASET --request request.yaml [--area N,W,S,E]     check the request (constraints), estimate, cost, licences
    cds_retrieve.py submit  DATASET --request request.yaml [--queue]            send it (one job at a time), log it
    cds_retrieve.py status  JOB                                                 the job's status at CDS
    cds_retrieve.py resume  JOB [--wait-min 30]                                 poll until done, then fetch
    cds_retrieve.py fetch   JOB --out DIR [--transform kelvin_to_celsius]       results downloaded, read into GeoTIFFs
    cds_retrieve.py cancel  JOB --confirm                                       ask CDS to delete the job (changes the service; only when asked)

The flow (CDS retrieve API; the paths are the ones the CDS entry declares in delivery.api):

    valid values  POST /api/retrieve/v1/processes/{dataset}/constraints   {"inputs": {...}}
    cost          POST /api/retrieve/v1/processes/{dataset}/costing       {"inputs": {...}}
    submit        POST /api/retrieve/v1/processes/{dataset}/execution     {"inputs": {...}}   -> jobID
    status        GET  /api/retrieve/v1/jobs/{jobID}                      accepted, running, successful, failed, rejected
    results       GET  /api/retrieve/v1/jobs/{jobID}/results              -> asset href(s)
    licences      GET  /api/profiles/v1/account/licences                  the account's accepted licences
    needed        GET  /api/catalogue/v1/collections/{dataset}            the dataset's licences (links with rel "license": id, rev, title)

Rules. Only datasets with a library entry are accepted (MAP-CDS-001). The request is checked against `constraints` and an invalid one is never
submitted (MAP-CDS-002): ECMWF warns that "Repeated submission of invalid requests may result in your account being disabled". The size is
estimated here (variables x times x levels x grid cells) and capped at 2 GB (MAP-CDS-006); `costing` goes on the cost card. A dataset licence
the account has not accepted stops the run with the dataset page (MAP-CDS-003): accepting is never automated ("must be done manually from the
dataset page"). One job at a time ("Submit requests sequentially, rather than in parallel", ECMWF, 22 October 2026): a second submit while one is
pending is MAP-CDS-005 unless --queue, which waits. Every submission goes into .geo-mapping/cds/jobs.jsonl; an identical request that is pending,
or that succeeded in the last 30 days, is never sent again: its result is used. Status is polled every 5 s, rising 1.5 times to 120 s; after 30
minutes the run ends with MAP-CDS-004 (resume later). A failed or rejected job ends the run with CDS's message, quoted as untrusted text
(MAP-CDS-007). Results are fetched only from the entry's `download_hosts` (MAP-CDS-009), and the key is never sent there. Reading needs GDAL's
netCDF or GRIB driver (MAP-CDS-008). Cancelling (POST /jobs/delete) changes the service and is sent only when the person asks in that run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

ENTRY = "ref.public-service.world.climate.copernicus-cds"
BASE = "https://cds.climate.copernicus.eu"
CAP_BYTES = 2 * 1024 ** 3
POLL_FIRST_S, POLL_FACTOR, POLL_MAX_S = 5.0, 1.5, 120.0
WAIT_DEFAULT_S = 30 * 60
REUSE_DAYS = 30
TERMINAL = {"successful", "failed", "rejected", "dismissed", "deleted"}
CREDIT = "Generated using Copernicus Climate Change Service information"
CREDIT_MODIFIED = "Contains modified Copernicus Climate Change Service information"
DISCLAIMER = "Neither the European Commission nor ECMWF is responsible for any use that may be made of the Copernicus information or data it contains."


class CDSError(Exception):
    def __init__(self, message: str, rule: str, code: int = gc.EXIT_LINT):
        super().__init__(message)
        self.rule, self.code = rule, code


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def log_path() -> Path:
    p = gc.workdir() / "cds" / "jobs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> dt.datetime:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return dt.datetime.fromisoformat(fixed + "T12:00:00") if fixed else dt.datetime.now()


# ---------------------------------------------------------------------------------------------------------------- entries
def dataset_entry(dataset: str) -> dict:
    """The library entry that names the dataset (delivery.cds.dataset), or MAP-CDS-001."""
    import ref_library as rl
    for e in rl.all_entries("all"):
        if ((e.get("delivery") or {}).get("cds") or {}).get("dataset") == dataset and e.get("status") not in ("blocked", "retired"):
            return e
    raise CDSError(f"the CDS dataset '{dataset}' has no library entry; only datasets the library describes (licence, credit, limits) are retrieved", "MAP-CDS-001", gc.EXIT_USAGE)


class Client:
    def __init__(self, *, http=None, store=None, sleep=time.sleep, clock=time.monotonic):
        import declared as dcl
        import ogc_http as oh
        import ref_fetch as rf
        self.entry = rf.find_entry(ENTRY)
        self.system = dcl.from_entry(self.entry)
        if self.system is None:
            raise CDSError(f"{ENTRY} declares no requests (delivery.api)", "MAP-CDS-001", gc.EXIT_USAGE)
        self.http = http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": 50_000_000, "retries": 0}, sleep=sleep)
        self.store, self.sleep, self.clock = store, sleep, clock
        self._auth = None

    def auth(self):
        if self._auth is None:
            import ref_fetch as rf
            try:
                self._auth = rf.credential_for(self.entry, BASE + "/api/profiles/v1/account/", self.store)
            except rf.FetchError as ex:
                raise CDSError(f"no usable copernicus-cds token: {ex}. Register yours with map-credentials add copernicus-cds", "MAP-CRED-013", gc.EXIT_REFUSED) from None
        return self._auth

    def close(self):
        if self._auth is not None:
            self._auth.close()
            self._auth = None

    def call(self, method: str, path: str, body: dict | None = None, *, keyless: bool = False, confirm: bool = False):
        import ogc_http as oh
        try:
            r = self.http.request(method, BASE + path, system=self.system, json=body, expect="json", auth=None if keyless else self.auth(), confirm_changes=confirm)
        except oh.Refusal:
            raise
        except oh.ServiceError as ex:
            if ex.status == 403 and not ex.rejected and "licen" in (ex.body or "").lower():
                # 1.3.1: CDS answers 403 when the dataset's licence is not accepted; the key is fine (the entry counts only 401 as a wrong key)
                m = re.search(r"/processes/([^/]+)/", path)
                page = f"{BASE}/datasets/{m.group(1)}?tab=download" if m else f"{BASE}/datasets"
                raise CDSError(f"CDS refused the request because the account has not accepted the dataset's licence (HTTP 403: "
                               f"{oh.untrusted(ex.body, 200)!r}). Accept its Terms of use on the dataset page ({page}), then run again; your token is "
                               "fine and was not marked rejected", "MAP-CDS-003", gc.EXIT_REFUSED) from None
            raise CDSError(f"CDS answered {ex.status or ''}: {oh.untrusted(str(ex), 300)}", ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
        return json.loads(r.content or b"{}")


# ---------------------------------------------------------------------------------------------------------------- plan
def inputs_of(request: dict, area=None) -> dict:
    """The request's inputs, with `area` (N, W, S, E) from the map window when given, NetCDF unarchived by default."""
    inp = dict(request)
    if area is not None:
        n, w, s, e = (float(v) for v in area)
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 360):
            raise CDSError(f"area {area} is not N, W, S, E", "MAP-CDS-002", gc.EXIT_USAGE)
        inp["area"] = [n, w, s, e]
    inp.setdefault("data_format", "netcdf")
    inp.setdefault("download_format", "unarchived")
    return inp


def check_constraints(inputs: dict, allowed: dict) -> list[str]:
    """The request's values the constraints answer does not allow (empty: valid)."""
    bad = []
    for k, v in inputs.items():
        if k in ("area", "data_format", "download_format", "grid"):
            continue
        ok = allowed.get(k)
        if ok is None:
            continue
        for x in (v if isinstance(v, list) else [v]):
            if str(x) not in {str(o) for o in ok}:
                bad.append(f"{k}={x}")
    return bad


def estimate_bytes(inputs: dict, grid_deg: float = 0.25) -> int:
    n = lambda k: len(inputs[k]) if isinstance(inputs.get(k), list) else (1 if inputs.get(k) else 1)   # noqa: E731
    times = n("year") * n("month") * max(1, n("day") if inputs.get("day") else 1) * max(1, n("time") if inputs.get("time") else 1)
    levels = n("pressure_level") if inputs.get("pressure_level") else 1
    a = inputs.get("area") or [90, -180, -90, 180]
    cells = max(1, math.ceil(abs(a[0] - a[2]) / grid_deg + 1)) * max(1, math.ceil(abs(a[3] - a[1]) / grid_deg + 1))
    return int(n("variable") * times * levels * cells * 4)


def licence_links(doc) -> list[dict]:
    """The licences a catalogue collection names: its links with rel "license" (1.3.1, read from the live catalogue on 2026-11-10: ERA5-Land
    monthly has {"rel": "license", "id": "cc-by", "rev": 1, "title": "CC-BY licence", "href": "https://spdx.org/licenses/CC-BY-4.0"})."""
    out = []
    for lk in (doc.get("links") if isinstance(doc, dict) else None) or []:
        if isinstance(lk, dict) and str(lk.get("rel", "")).lower() in ("license", "licence") and lk.get("id"):
            out.append({"id": str(lk["id"]), "rev": lk.get("rev"), "title": lk.get("title"), "href": lk.get("href")})
    return out


def required_licences(c: Client, dataset: str, entry: dict) -> list[dict]:
    """The dataset's licences as [{id, rev, title, href}]: the catalogue collection (GET /api/catalogue/v1/collections/{dataset}, public), else any
    `licences` list in the process description, else the entry's delivery.cds.licences (ids). 1.3.0 read only the process description, which
    names none, so ERA5-Land listed no licence."""
    try:
        found = licence_links(c.call("GET", f"/api/catalogue/v1/collections/{dataset}", keyless=True))
    except CDSError:
        found = []
    if not found:
        try:
            doc = c.call("GET", f"/api/retrieve/v1/processes/{dataset}", keyless=True)
        except CDSError:
            doc = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("licences", "licenses") and isinstance(v, list):
                        found.extend({"id": str(x.get("id")), "rev": x.get("revision", x.get("rev"))} if isinstance(x, dict) else {"id": str(x), "rev": None}
                                     for x in v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(doc)
    if not found:
        found = [{"id": str(x), "rev": None} for x in (((entry.get("delivery") or {}).get("cds") or {}).get("licences") or [])]
    seen, out = set(), []
    for x in found:
        if x["id"] not in seen:
            seen.add(x["id"])
            out.append(x)
    return out


def missing_licences(need: list[dict], accepted) -> list[str]:
    """The needed licences the account has not accepted, or has accepted at an older revision than the one the dataset names."""
    have = {}
    for x in (accepted.get("licences") if isinstance(accepted, dict) else accepted) or []:
        if isinstance(x, dict) and x.get("id") is not None:
            have[str(x["id"])] = x.get("revision", x.get("rev"))
    out = []
    for n in need:
        if n["id"] not in have:
            out.append(n["id"])
        elif n.get("rev") is not None and have[n["id"]] is not None and int(have[n["id"]]) < int(n["rev"]):
            out.append(f"{n['id']} (revision {n['rev']}; accepted {have[n['id']]})")
    return out


def plan(dataset: str, request: dict, *, area=None, client: Client | None = None) -> dict:
    entry = dataset_entry(dataset)
    c = client or Client()
    inputs = inputs_of(request, area)
    allowed = c.call("POST", f"/api/retrieve/v1/processes/{dataset}/constraints", {"inputs": {k: v for k, v in inputs.items() if k != "area"}}, keyless=True)
    bad = check_constraints(inputs, allowed if isinstance(allowed, dict) else {})
    if bad:
        raise CDSError(f"the request asks for values CDS does not offer for {dataset}: {', '.join(bad[:8])}. It was not submitted (repeated invalid "
                       "requests can disable an account)", "MAP-CDS-002")
    grid = float(((entry.get("delivery") or {}).get("cds") or {}).get("grid_deg") or 0.25)
    est = estimate_bytes(inputs, grid)
    if est > CAP_BYTES:
        raise CDSError(f"the request is about {est / 1024 ** 3:.1f} GB, over the 2 GB cap: ask for fewer variables, months or a smaller area", "MAP-CDS-006")
    cost = c.call("POST", f"/api/retrieve/v1/processes/{dataset}/costing", {"inputs": inputs})
    need = required_licences(c, dataset, entry)
    missing = missing_licences(need, c.call("GET", "/api/profiles/v1/account/licences"))
    if missing:
        raise CDSError(f"the account has not accepted the licence(s) {', '.join(missing)} that {dataset} needs: accept the dataset's Terms of use on its page "
                       f"({BASE}/datasets/{dataset}?tab=download), then run again", "MAP-CDS-003")
    return {"dataset": dataset, "entry": entry["id"], "inputs": inputs, "estimate_bytes": est, "cost": cost, "licences": [x["id"] for x in need], "licence_details": need,
            "sha256": request_sha(dataset, inputs), "card": f"CDS job: {dataset}, about {est / 1024 ** 2:.0f} MB; cost {json.dumps(cost)[:120]}; licence "
            + (", ".join(f"{x.get('title') or x['id']} ({x['id']}" + (f", revision {x['rev']}" if x.get("rev") is not None else "") + ", accepted)" for x in need)
               or "none named by CDS")}


def request_sha(dataset: str, inputs: dict) -> str:
    return hashlib.sha256(json.dumps([dataset, inputs], sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------- the job log
def jobs() -> list[dict]:
    p = log_path()
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    latest: dict = {}
    for j in out:                                       # the last line of a job wins (submitted, then its status updates)
        latest[j["job"]] = {**latest.get(j["job"], {}), **j}
    return list(latest.values())


def log(entry: dict) -> None:
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def reusable(sha: str) -> dict | None:
    cutoff = (_now() - dt.timedelta(days=REUSE_DAYS)).isoformat()
    for j in jobs():
        if j.get("sha256") != sha:
            continue
        if j.get("status") not in TERMINAL or (j.get("status") == "successful" and str(j.get("time", "")) >= cutoff):
            return j
    return None


def pending() -> list[dict]:
    return [j for j in jobs() if j.get("status") not in TERMINAL]


def submit(dataset: str, request: dict, *, area=None, queue: bool = False, client: Client | None = None, wait_s: float = WAIT_DEFAULT_S) -> dict:
    c = client or Client()
    p = plan(dataset, request, area=area, client=c)
    again = reusable(p["sha256"])
    if again:
        return {"job": again["job"], "reused": True, "status": again.get("status"), "plan": p,
                "findings": [finding("MAP-CDS-004", "INFO", f"an identical request is already job {again['job']} ({again.get('status')}); its result is used, nothing was sent")]}
    if pending():
        if not queue:
            pj = pending()[0]
            raise CDSError(f"job {pj['job']} is still pending at CDS: requests go one at a time (ECMWF); wait, resume it, or submit with --queue", "MAP-CDS-005")
        for pj in pending():
            poll(pj["job"], client=c, wait_s=wait_s)
    doc = c.call("POST", f"/api/retrieve/v1/processes/{dataset}/execution", {"inputs": p["inputs"]})
    job = str(doc.get("jobID") or doc.get("jobId") or doc.get("id") or "")
    if not re.fullmatch(r"[0-9A-Za-z-]{6,80}", job):
        raise CDSError("CDS did not return a job id", "MAP-CDS-007", gc.EXIT_SERVICE)
    log({"job": job, "dataset": dataset, "sha256": p["sha256"], "time": _now().isoformat(), "status": str(doc.get("status") or "accepted"), "inputs": p["inputs"]})
    return {"job": job, "reused": False, "status": doc.get("status") or "accepted", "plan": p, "findings": []}


def status(job: str, *, client: Client | None = None) -> dict:
    c = client or Client()
    doc = c.call("GET", f"/api/retrieve/v1/jobs/{job}")
    st = str(doc.get("status") or "").lower()
    log({"job": job, "status": st, "checked": _now().isoformat()})
    return {"job": job, "status": st, "message": _message(doc)}


def _message(doc: dict) -> str | None:
    import ogc_http as oh
    for k in ("message", "detail", "title"):
        if doc.get(k):
            return oh.untrusted(doc[k], 400)
    return None


def poll(job: str, *, client: Client | None = None, wait_s: float = WAIT_DEFAULT_S) -> dict:
    """Status every 5 s, rising 1.5 times to 120 s, for at most wait_s. -> the last status; MAP-CDS-004 (still running) or MAP-CDS-007."""
    c = client or Client()
    t0 = c.clock()
    delay = POLL_FIRST_S
    while True:
        st = status(job, client=c)
        if st["status"] == "successful":
            return st
        if st["status"] in ("failed", "rejected", "dismissed"):
            raise CDSError(f"CDS {st['status']} job {job}: \"{st.get('message') or 'no message'}\" (CDS's words, quoted as data)", "MAP-CDS-007", gc.EXIT_SERVICE)
        if c.clock() - t0 + delay > wait_s:
            return {**st, "findings": [finding("MAP-CDS-004", "INFO", f"the job is still {st['status']} at CDS; cds_retrieve.py resume {job} picks it up")]}
        c.sleep(delay)
        delay = min(POLL_MAX_S, delay * POLL_FACTOR)


def results(job: str, *, client: Client | None = None) -> list[str]:
    c = client or Client()
    doc = c.call("GET", f"/api/retrieve/v1/jobs/{job}/results")
    hrefs = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "href" and isinstance(v, str):
                    hrefs.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(doc)
    return hrefs


def fetch(job: str, out_dir, *, transform: str | None = None, client: Client | None = None) -> dict:
    """The job's files from the hosts the entry lists (MAP-CDS-009), never with the key; read into GeoTIFFs; a record with the credit."""
    import ogc_http as oh
    import climate_read as cr
    c = client or Client()
    hosts = [h.lower() for h in (c.system.download_hosts or [])]
    j = next((x for x in jobs() if x["job"] == job), {})
    out_dir = Path(out_dir)
    cache = gc.outputs_dir() / "cache" / "cds" / job
    cache.mkdir(parents=True, exist_ok=True)
    files, recs = [], []
    for href in results(job, client=c):
        host = (urllib.parse.urlsplit(href).hostname or "").lower()
        if host not in hosts:
            raise CDSError(f"the result is on {host}, which {ENTRY} does not list in delivery.api.download_hosts; record the host after checking it (the "
                           "live check), then fetch again", "MAP-CDS-009", gc.EXIT_REFUSED)
        dl = oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": min(CAP_BYTES, oh.CEILING["max_bytes"]), "retries": 1}, sleep=c.sleep,
                         transport=getattr(c.http, "_transport", None), resolver=getattr(c.http, "_resolver", None) or __import__("socket").getaddrinfo)
        try:
            r = dl.get(href, expect="any")                 # a plain GET: no credential is attached to another host
        except oh.ServiceError as ex:
            raise CDSError(f"the result could not be downloaded: {ex}", ex.rule or "MAP-SVC-013", gc.EXIT_SERVICE) from None
        name = Path(urllib.parse.urlsplit(href).path).name or "result.nc"
        p = cache / name
        p.write_bytes(r.content)
        try:
            rd = cr.read(p, out_dir, transform=transform)
        except cr.ReadError as ex:
            raise CDSError(str(ex), ex.rule) from None
        files += rd["files"]
        recs.append({"href_host": host, "file": name, "sha256": hashlib.sha256(r.content).hexdigest(), "bytes": len(r.content), "calls": dl.audit.calls})
    year = str(_now().year)
    credit = f"{CREDIT_MODIFIED if transform else CREDIT} [{year}]. {DISCLAIMER}"
    rec = {"job": job, "dataset": j.get("dataset"), "request": j.get("inputs"), "retrieved_at": _now().date().isoformat(), "downloads": recs,
           "transform": transform, "provenance": {"source": f"Copernicus Climate Data Store, {j.get('dataset')}", "license": "attribution",
                                                   "attribution": credit, "library_ref": ENTRY, "job": job}}
    (out_dir / f"cds-{job}.json").write_text(json.dumps(rec, indent=2, default=str))
    log({"job": job, "fetched": _now().isoformat(), "files": [f["tif"] for f in files]})
    return {"job": job, "files": files, "record": str(out_dir / f"cds-{job}.json"), "credit": credit}


def cancel(job: str, *, confirm: bool, client: Client | None = None) -> dict:
    c = client or Client()
    c.call("POST", "/api/retrieve/v1/jobs/delete", {"job_ids": [job]}, confirm=confirm)
    log({"job": job, "status": "deleted", "time": _now().isoformat()})
    return {"job": job, "status": "deleted"}


def source_path(job: str, variable: str, time_: str | None = None) -> tuple[str, int | None]:
    """A MapSpec `source: {cds: {job, variable, time}}`: the fetched GeoTIFF and the band of that time (None: all bands)."""
    j = next((x for x in jobs() if x["job"] == job), None)
    if not j or not j.get("files"):
        raise CDSError(f"job {job} has not been fetched here; run cds_retrieve.py fetch {job}", "MAP-CDS-002", gc.EXIT_USAGE)
    tif = next((f for f in j["files"] if f.endswith(f"_{variable}.tif")), None)
    if not tif:
        raise CDSError(f"job {job} holds no variable {variable}", "MAP-CDS-002", gc.EXIT_USAGE)
    if not time_:
        return tif, None
    side = json.loads(Path(tif).with_suffix(".json").read_text())
    for k, d in enumerate(side.get("dates") or []):
        if d and str(d).startswith(str(time_)):
            return tif, k + 1
    raise CDSError(f"time {time_} is not among the job's dates ({', '.join(str(d) for d in (side.get('dates') or [])[:6])})", "MAP-CDS-002", gc.EXIT_USAGE)


def kit_script(dataset: str, request: dict) -> str:
    """The request kit's script on ecmwf-datastores-client: it reads the key from the person's own .ecmwfdatastoresrc or ECMWF_DATASTORES_KEY; the
    plugin never sees it."""
    body = json.dumps(request, indent=4, sort_keys=True)
    return ("# CDS request kit (geo-mapping plugin, 1.3.0). Holds no key: ecmwf-datastores-client reads yours from ~/.ecmwfdatastoresrc or\n"
            "# ECMWF_DATASTORES_KEY. Accept the dataset's licence on its page first. Run it, then: request_kit.py ingest THE_FILE\n"
            "from ecmwf.datastores import Client\n\n"
            f"DATASET = {dataset!r}\nREQUEST = {body}\n\n"
            "client = Client()\nclient.retrieve(DATASET, REQUEST, target='cds-result.nc')\n")


def main(argv=None) -> int:
    import yaml
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["plan", "submit", "status", "resume", "fetch", "cancel", "kit"])
    ap.add_argument("target", help="DATASET (plan, submit, kit) or JOB")
    ap.add_argument("--request")
    ap.add_argument("--area")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--wait-min", type=float, default=30)
    ap.add_argument("--out")
    ap.add_argument("--transform")
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    req = yaml.safe_load(Path(a.request).read_text()) if a.request else {}
    area = [float(v) for v in a.area.split(",")] if a.area else None
    c = None
    try:
        if a.op == "kit":
            print(kit_script(a.target, req))
            return 0
        c = Client()
        if a.op == "plan":
            res = plan(a.target, req, area=area, client=c)
        elif a.op == "submit":
            res = submit(a.target, req, area=area, queue=a.queue, client=c)
        elif a.op == "status":
            res = status(a.target, client=c)
        elif a.op == "resume":
            res = poll(a.target, client=c, wait_s=a.wait_min * 60)
            if res.get("status") == "successful" and a.out:
                res = {**res, **fetch(a.target, a.out, transform=a.transform, client=c)}
        elif a.op == "fetch":
            res = fetch(a.target, a.out or ".", transform=a.transform, client=c)
        else:
            res = cancel(a.target, confirm=a.confirm, client=c)
    except CDSError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return ex.code
    finally:
        if c is not None:
            c.close()
    print(json.dumps({"ok": True, **res}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
