#!/usr/bin/env python3
"""Reference library maintenance (spec 9.3.7): staleness and coverage reports, read-only verification, proposed patches,
scaffolding of local draft entries, and the owner-approved application of a patch.

Functions here are used by `ref_library.py` (add, verify, report, apply), by the `reference-refresh` pipeline and by the
source-tree tools (tools/ref_verify.py, ref_report.py, ref_release.py).

Rules that hold everywhere in this file:
  * Verification reads only the addresses written in the entry itself, over HTTPS, with no keys, and treats every response as data.
  * A verification never edits an entry. It returns a proposal (a patch with the whole new entry) and the owner applies it.
  * Nothing here geocodes, uploads or writes outside the library folders it is given.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lints"))
import gmap_common as gc  # noqa: E402
import library_lint as ll  # noqa: E402

UA = "geo-mapping-plugin/0.1 (reference-library verify)"
MAX_BYTES = 400 * 1024 * 1024
ACTIVE = ("active", "deprecated")
FAMILY_DIR = {"data": "data", "style": "style", "method": "method"}


class MaintError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME, fid: str = "MAP-LIB-000"):
        super().__init__(message)
        self.code = code
        self.fid = fid


def clean(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def canon_sha(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------- staleness and report
def staleness(e: dict, types: dict, today: dt.date, horizon_days: int = 30) -> dict | None:
    """None for entries that are not reviewed on a cycle (draft, blocked, retired). Else the state of the review date."""
    if e.get("status") not in ACTIVE:
        return None
    m = e.get("maintenance") or {}
    vb = ll._d(m.get("verify_by"))
    spec = types["types"].get(e.get("type")) or {}
    cadence = spec.get("cadence_months", 12)
    if not vb:
        return {"id": e.get("id"), "type": e.get("type"), "state": "no-date", "verify_by": None, "days": None, "cadence_months": cadence}
    days = (vb - today).days
    months_over = (today.year - vb.year) * 12 + (today.month - vb.month)
    if today <= vb:
        state = "ok" if days > horizon_days else "due-soon"
    else:
        state = "overdue-cycle" if months_over > cadence else "due"
    return {"id": e.get("id"), "type": e.get("type"), "state": state, "verify_by": vb.isoformat(), "days": days, "cadence_months": cadence,
            "reviewed_on": str(m.get("reviewed_on")), "scope": e.get("_scope")}


def due_entries(entries: list[dict], types: dict, today: dt.date, horizon_days: int = 0, ids: list[str] | None = None) -> list[dict]:
    """Entries to verify: past due, or due within the horizon. `ids` selects entries whatever their date."""
    out = []
    for e in entries:
        if ids is not None:
            if e.get("id") in ids:
                out.append(e)
            continue
        s = staleness(e, types, today, horizon_days)
        if s and (s["state"] in ("due", "overdue-cycle", "no-date") or (horizon_days and s["state"] == "due-soon")):
            out.append(e)
    return out


def _tag_values(e: dict, facet: str) -> list[str]:
    v = (e.get("tags") or {}).get(facet) or []
    return list(v) if isinstance(v, list) else [v]


def report(entries: list[dict], tags: dict, types: dict, today: dt.date, horizon_days: int = 30) -> dict:
    live = [e for e in entries if e.get("status") != "retired"]
    stale = [s for s in (staleness(e, types, today, horizon_days) for e in live) if s]
    order = {"overdue-cycle": 0, "due": 1, "no-date": 2, "due-soon": 3, "ok": 4}
    stale.sort(key=lambda s: (order[s["state"]], s["verify_by"] or "", s["id"]))
    counts = {k: sum(1 for s in stale if s["state"] == k) for k in order}

    # coverage: each map type against the data types that serve the same use_for, by geography
    maps = [e for e in live if e.get("type") == "map-type" and e.get("status") in ACTIVE]
    data = [e for e in live if types["types"].get(e.get("type"), {}).get("family") == "data" and e.get("status") in ACTIVE and e.get("type") != "basemap-provider"]
    data_types = sorted({e["type"] for e in data})
    matrix, gaps = {}, []
    for mt in sorted(maps, key=lambda x: x["id"]):
        uses = set(_tag_values(mt, "use_for"))
        paired = set(mt.get("pairs_with") or [])
        hit = [d for d in data if (set(_tag_values(d, "use_for")) & uses) or d["id"] in paired]
        row = {t: sum(1 for d in hit if d["type"] == t) for t in data_types}
        geos = sorted({g for d in hit for g in _tag_values(d, "geography")})
        matrix[mt["id"]] = {"by_type": row, "geography": geos, "entries": len(hit)}
        if not hit:
            gaps.append({"kind": "map-type-without-data", "id": mt["id"], "message": f"{mt['id']} has no active data entry serving {', '.join(sorted(uses)) or 'it'}"})
        elif "world" not in geos and not any(g.startswith("us") for g in geos):
            gaps.append({"kind": "geography", "id": mt["id"], "message": f"{mt['id']}: no data entry for any recorded geography"})
    for fac in ("level",):
        have = {v for d in data for v in _tag_values(d, fac)}
        for v in tags["facets"][fac]["values"]:
            if v not in have:
                gaps.append({"kind": "level-unserved", "id": v, "message": f"no active data entry at level '{v}'"})

    # tag use: unused values, values on almost every entry, tags not in the vocabulary
    use: dict[tuple, int] = {}
    for e in live:
        for fac, vals in (e.get("tags") or {}).items():
            for v in (vals if isinstance(vals, list) else [vals]):
                use[(fac, str(v))] = use.get((fac, str(v)), 0) + 1
    unused, overloaded, unknown = [], [], []
    n_live = max(1, len(live))
    for fac, spec in tags["facets"].items():
        for v in spec["values"]:
            if not use.get((fac, v)):
                unused.append(f"{fac}={v}")
            elif fac in ("domain", "use_for", "scale", "detail", "geography", "level") and n_live >= 10 and use[(fac, v)] / n_live > 0.8:
                overloaded.append({"tag": f"{fac}={v}", "entries": use[(fac, v)], "of": n_live})
    for (fac, v), n in sorted(use.items()):
        if fac not in tags["facets"] or ll.resolve_tag(tags, fac, v) is None:
            unknown.append(f"{fac}={v}")
    return {"today": today.isoformat(), "entries": len(live), "staleness": stale, "staleness_counts": counts,
            "coverage": {"data_types": data_types, "matrix": matrix, "gaps": gaps},
            "tags": {"unused": sorted(unused), "overloaded": overloaded, "unknown": unknown}}


def report_text(r: dict, part: str = "all") -> str:
    L = [f"Library report, {r['today']}: {r['entries']} entries", ""]
    if part in ("all", "stale"):
        c = r["staleness_counts"]
        L.append(f"Review dates: {c['ok']} current, {c['due-soon']} due soon, {c['due']} past due, {c['overdue-cycle']} past due by more than one cycle, {c['no-date']} without a date.")
        for s in r["staleness"]:
            if s["state"] != "ok":
                L.append(f"  {s['state']:<14} {s['id']}  verify_by {s['verify_by']}")
        L.append("")
    if part in ("all", "coverage"):
        L.append("Coverage (map type, data entries that serve it):")
        for k, v in r["coverage"]["matrix"].items():
            L.append(f"  {k}: {v['entries']} entries; geography {', '.join(v['geography']) or 'none'}")
        for g in r["coverage"]["gaps"]:
            L.append(f"  gap: {g['message']}")
        L.append("")
    if part in ("all", "tags"):
        t = r["tags"]
        L.append(f"Tags: {len(t['unused'])} unused, {len(t['overloaded'])} on almost every entry, {len(t['unknown'])} not in the vocabulary.")
        for x in t["overloaded"]:
            L.append(f"  overloaded: {x['tag']} ({x['entries']} of {x['of']})")
        for x in t["unknown"]:
            L.append(f"  unknown: {x}")
    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------------------------------------------------------- verification
class Net:
    """A small read-only network helper. `opener` replaces urllib.request.urlopen (tests, device-bridge routes)."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.calls: list[dict] = []

    def _check(self, url: str):
        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or not u.netloc:
            raise MaintError("only HTTPS addresses are checked", gc.EXIT_REFUSED, "MAP-LIB-100")
        if re.search(r"[\s<>\"']", url):
            raise MaintError("the address contains characters that are not allowed", gc.EXIT_REFUSED, "MAP-LIB-100")

    def status(self, url: str, method: str = "HEAD") -> int:
        self._check(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
        self.calls.append({"method": method, "host": urllib.parse.urlparse(url).netloc})
        try:
            with self.opener(req, timeout=60) as r:
                code = getattr(r, "status", None) or r.getcode()
                if method == "GET":
                    r.read(1024)
                return int(code)
        except urllib.error.HTTPError as ex:
            return int(ex.code)
        except Exception as ex:   # noqa: BLE001
            raise MaintError(f"{urllib.parse.urlparse(url).netloc} could not be reached: {type(ex).__name__}", gc.EXIT_SERVICE, "MAP-LIB-101")

    def download(self, url: str, dest: Path) -> str:
        import ref_fetch as rf
        self.calls.append({"method": "GET", "host": urllib.parse.urlparse(url).netloc})
        try:
            return rf.download(url, dest, self.opener)
        except rf.FetchError as ex:
            raise MaintError(str(ex), ex.code, "MAP-LIB-101")


def entry_url(e: dict) -> str | None:
    chk = (e.get("maintenance") or {}).get("check") or {}
    d = e.get("delivery") or {}
    u = chk.get("url") or (e.get("source") or {}).get("url") or d.get("url_template")
    if u and "{" in u:
        u = u.replace("{z}", "0").replace("{x}", "0").replace("{y}", "0").replace("{statefp}", "04")
        if "{" in u:
            return None
    return u


def _same_list(a, b) -> bool:
    return list(a or []) == list(b or [])


def verify_entry(e: dict, today: dt.date, types: dict, net: Net | None = None, plugin_root: Path | None = None) -> dict:
    """Run the entry's own check. Returns {id, rev, kind, outcome, checks, observed, proposal}. Never edits the entry.

    outcome: unchanged | changed | unreachable | gone | refused | manual | local-only
    """
    net = net or Net()
    eid, kind = e.get("id"), ((e.get("maintenance") or {}).get("check") or {}).get("kind")
    res = {"id": eid, "rev": e.get("rev"), "scope": e.get("_scope"), "kind": kind or "none", "outcome": "manual", "checks": [], "observed": {}, "notes": [], "proposal": None}
    if e.get("status") in ("retired", "blocked"):
        res["outcome"], res["notes"] = "manual", [f"{e.get('status')}: not verified"]
        return res
    # a bundled payload is checked against its own file first; this needs no network
    d = e.get("delivery") or {}
    if d.get("mode") == "bundled" and plugin_root is not None:
        p = plugin_root / d.get("path", "")
        ok = p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == d.get("sha256")
        res["checks"].append({"name": "bundled payload hash", "ok": bool(ok), "expected": (d.get("sha256") or "")[:12], "observed": "match" if ok else "differs or missing"})
        if not ok:
            res["outcome"] = "changed"
            res["notes"].append("the bundled file does not match its entry; rebuild it from the source and re-run the golden-image tests")
    try:
        if kind in ("http_head", "http_get"):
            url = entry_url(e)
            if not url:
                res["notes"].append("no address to check")
                return res
            code = net.status(url, "HEAD" if kind == "http_head" else "GET")
            want = int((e["maintenance"]["check"]).get("expect", 200))
            res["checks"].append({"name": "status", "ok": code == want, "expected": want, "observed": code})
            res["observed"]["status"] = code
            if code in (404, 410):
                res["outcome"] = "gone"
            elif code != want:
                res["outcome"] = "changed" if res["outcome"] != "changed" else "changed"
                res["notes"].append(f"the address answered {code}; {want} was expected")
            elif res["outcome"] != "changed":
                res["outcome"] = "unchanged"
        elif kind in ("feature_count_and_hash", "row_count_and_hash"):
            url = (e.get("source") or {}).get("url")
            if not url:
                res["notes"].append("the entry records no download address")
                return res
            tmp = Path(tempfile.mkdtemp(prefix="gmap-verify-"))
            try:
                z = tmp / ("download" + (Path(urllib.parse.urlparse(url).path).suffix.lower() or ".zip"))
                sha = net.download(url, z)
                want = (e.get("source") or {}).get("file_sha256")
                res["observed"]["sha256"] = sha
                res["checks"].append({"name": "file hash", "ok": (sha == want) if want else None, "expected": (want or "not recorded")[:12], "observed": sha[:12]})
                facts = read_facts(z, kind)
                res["observed"].update(facts)
                chk = e["maintenance"]["check"]
                n_key = "features" if kind == "feature_count_and_hash" else "rows"
                if facts.get(n_key) is not None and chk.get(n_key) is not None:
                    res["checks"].append({"name": n_key, "ok": facts[n_key] == chk[n_key], "expected": chk[n_key], "observed": facts[n_key]})
                if facts.get("columns") and d.get("columns"):
                    res["checks"].append({"name": "columns", "ok": _same_list(facts["columns"], d["columns"]), "expected": len(d["columns"]), "observed": len(facts["columns"])})
                if facts.get("bbox") and d.get("bbox"):
                    close = all(abs(a - b) < 1e-3 for a, b in zip(facts["bbox"], d["bbox"]))
                    res["checks"].append({"name": "bbox", "ok": close, "expected": d["bbox"], "observed": facts["bbox"]})
                bad = [c for c in res["checks"] if c["ok"] is False]
                if res["outcome"] != "changed":
                    res["outcome"] = "changed" if bad else "unchanged"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            res["notes"].append("no automatic check for this entry; a person reviews it")
            return res
    except MaintError as ex:
        if res["outcome"] != "changed":                      # a bundled payload that differs from its entry is a fact whatever the network says
            res["outcome"] = "refused" if ex.code == gc.EXIT_REFUSED else "unreachable"
        res["notes"].append(str(ex))
        res["fid"] = ex.fid
        return res
    res["proposal"] = propose(e, res, today, types)
    return res


def read_facts(path: Path, kind: str) -> dict:
    """Facts read from a downloaded file: feature or row count, column names, bbox in WGS84. Failure to read is a fact, not a crash."""
    try:
        if kind == "feature_count_and_hash":
            import geo_io
            g = geo_io.read(path).gdf
            f = {"features": int(len(g)), "columns": [c for c in g.columns if c != "geometry"]}
            if len(g) and g.crs is not None:
                f["bbox"] = [round(float(v), 6) for v in g.to_crs("EPSG:4326").total_bounds]
            return f
        import pandas as pd
        head = path.read_bytes()[:4]
        if head[:2] == b"PK":
            import zipfile
            try:
                names = zipfile.ZipFile(path).namelist()
            except zipfile.BadZipFile:
                names = []
            if any(n.startswith("xl/") for n in names):         # 0.24.0 (TODO M3b): a workbook's first sheet is read, rows and columns compared
                df = pd.read_excel(path, sheet_name=0, dtype=str)
                return {"rows": int(len(df)), "columns": [str(c) for c in df.columns], "note": "the first sheet of the workbook"}
            return {"rows": None, "columns": None, "note": "an archive: only the hash is compared"}
        df = pd.read_csv(path, dtype=str)
        return {"rows": int(len(df)), "columns": list(df.columns)}
    except Exception as ex:   # noqa: BLE001
        return {"read_error": f"{type(ex).__name__}: {str(ex)[:120]}"}


def propose(e: dict, v: dict, today: dt.date, types: dict) -> dict:
    """A patch: the whole entry at rev+1, the field-by-field changes, and the changelog line. Applied only by an owner."""
    new = copy.deepcopy(clean(e))
    spec = types["types"].get(e.get("type")) or {}
    m = new.setdefault("maintenance", {})
    changes, review = [], []

    def setv(path: list, val):
        cur = new
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        old = cur.get(path[-1])
        if old != val:
            cur[path[-1]] = val
            changes.append({"path": ".".join(path), "from": old, "to": val})

    obs = v.get("observed") or {}
    kind = "renew"
    if v["outcome"] == "changed":
        kind = "update"
        if obs.get("sha256"):
            setv(["source", "file_sha256"], obs["sha256"])
        if obs.get("features") is not None:
            setv(["maintenance", "check", "features"], obs["features"])
            if (new.get("delivery") or {}).get("features") is not None:
                setv(["delivery", "features"], obs["features"])
        if obs.get("rows") is not None:
            setv(["maintenance", "check", "rows"], obs["rows"])
        if obs.get("columns") and (new.get("delivery") or {}).get("columns") is not None:
            setv(["delivery", "columns"], obs["columns"])
        if obs.get("bbox") and (new.get("delivery") or {}).get("bbox") is not None:
            setv(["delivery", "bbox"], obs["bbox"])
        review += ["The source file changed. Decide whether it is a new vintage: set vintage.current, and the title and product name if the year is part of them.",
                   "Check the join key and column list against the new file before accepting."]
        if (e.get("delivery") or {}).get("mode") == "bundled":
            review.append("A bundled payload is affected: rebuild it from the source, update delivery.sha256, and run the golden-image tests before applying.")
        note = "Re-verified against the source: " + "; ".join(f"{c['name']} {c['expected']} -> {c['observed']}" for c in v["checks"] if c["ok"] is False) + ". Vintage not changed here; set at review."
    else:
        note = "Re-verified against the source; no change found."
    new["rev"] = int(e.get("rev", 1)) + 1
    if new.get("url_verified_on") or (e.get("source") or {}).get("url"):
        new["url_verified_on"] = today.isoformat()
        changes.append({"path": "url_verified_on", "from": e.get("url_verified_on"), "to": today.isoformat()})
    m["reviewed_on"] = today.isoformat()
    m["verify_by"] = add_months(today, spec.get("cadence_months", 12)).isoformat()
    changes += [{"path": "maintenance.reviewed_on", "from": (e.get("maintenance") or {}).get("reviewed_on"), "to": m["reviewed_on"]},
                {"path": "maintenance.verify_by", "from": (e.get("maintenance") or {}).get("verify_by"), "to": m["verify_by"]}, {"path": "rev", "from": e.get("rev"), "to": new["rev"]}]
    line = {"rev": new["rev"], "date": today.isoformat(), "note": note}
    m["changelog"] = list(m.get("changelog") or []) + [line]
    return {"patch_version": 1, "entry": e.get("id"), "scope": e.get("_scope"), "kind": kind, "base_rev": e.get("rev"), "base_sha256": canon_sha(clean(e)), "created": today.isoformat(),
            "changes": changes, "changelog": line, "review": review + ["Read the changes above, then apply with ref_library.py apply --approve."], "entry_after": new}


# ---------------------------------------------------------------------------------------------------------------- apply
def apply_patch(patch: dict, entries_by_id: dict[str, dict], lib: Path, today: dt.date, approve: bool, plugin_root: Path | None = None) -> dict:
    """Write an owner-approved patch. Refuses without approval, on a changed base, or if the result would not pass the library lint."""
    if not approve:
        raise MaintError("a patch is applied only with --approve, after the owner has read it", gc.EXIT_REFUSED, "MAP-LIB-102")
    if patch.get("patch_version") != 1 or not isinstance(patch.get("entry_after"), dict):
        raise MaintError("this is not a library patch", gc.EXIT_USAGE, "MAP-LIB-102")
    eid = patch["entry"]
    cur = entries_by_id.get(eid)
    if not cur:
        raise MaintError(f"no entry '{eid}' in this library", gc.EXIT_USAGE, "MAP-LIB-102")
    if cur.get("rev") != patch.get("base_rev") or canon_sha(clean(cur)) != patch.get("base_sha256"):
        raise MaintError(f"{eid} changed since the patch was made (now rev {cur.get('rev')}); verify again", gc.EXIT_REFUSED, "MAP-LIB-102")
    path = Path(cur["_path"])
    before = path.read_text()
    after = patch["entry_after"]
    path.write_text(yaml.safe_dump(after, sort_keys=False, allow_unicode=True, width=160))
    scope = cur.get("_scope", "shipped")
    es = ll.load_entries(lib / "entries", scope)
    fs = [x for x in ll.lint_entries(es, lib if scope == "shipped" else _shipped_lib(), plugin_root=plugin_root, today=today) if x["severity"] == "BLOCK"]
    if fs:
        path.write_text(before)
        raise MaintError("the patched entry fails the library lint, so nothing was changed: " + "; ".join(f"{x['id']} {x['entry']}: {x['message']}" for x in fs[:4]), gc.EXIT_LINT, "MAP-LIB-102")
    return {"applied": eid, "rev": after.get("rev"), "path": str(path), "next": "regenerate index.yaml and CHANGELOG.md (tools/ref_release.py regen) before a release" if scope == "shipped" else "run ref_library.py index --scope local"}


def _shipped_lib() -> Path:
    return gc.plugin_root() / "library"


# ---------------------------------------------------------------------------------------------------------------- add (scaffold a local draft)
INFER_TYPE = {"Polygon": "boundary", "MultiPolygon": "boundary", "Point": "infrastructure", "MultiPoint": "infrastructure", "LineString": "infrastructure", "MultiLineString": "infrastructure"}
LICENSE_FLAGS = {"public-domain": (True, True, True), "open": (True, True, True), "attribution": (True, True, True), "share-alike": (True, True, True),
                 "restricted": (False, False, False), "licensed": (False, False, False)}


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:48] or "layer"


def _scale_from_bbox(b: list[float]) -> str:
    span = max(b[2] - b[0], b[3] - b[1])
    return "national" if span > 30 else "regional" if span > 10 else "state" if span > 3 else "metro" if span > 0.5 else "local"


GEO_SHARE_UNIT = 0.80        # a box is "in" a state or province when at least this share of its land lies there (a state's own box pokes into its neighbours)
GEO_SHARE_COUNTRY = 0.75     # likewise for the United States, Canada or Mexico (a CONUS box is 80 percent US, a Canada box 76 percent Canada)
GEO_SHARE_CONTINENT = 0.90   # and for North America, the three together (a Mexico box reaches into Central America)
_GEO_UNITS: list | None = None


def _geo_units() -> list:
    """(vocabulary value, country, geometry in EPSG:6933) for every unit the box is measured against: the bundled US states (us-xx), the Canadian
    provinces of the bundled Natural Earth 50m admin-1 (ca-xx), and the bundled Natural Earth 110m countries for Mexico and the rest of the world."""
    global _GEO_UNITS
    if _GEO_UNITS is not None:
        return _GEO_UNITS
    import map_plan as mp
    out = []
    st, _ = mp.load_ref_layer("ref.boundary.us.state.cb20m")
    for code, g in zip(st["STUSPS"].astype(str), st.to_crs("EPSG:6933").geometry):
        out.append((f"us-{code.lower()}", "us", g))
    a1, _ = mp.load_ref_layer("ref.boundary.world.state.ne50")
    ca = a1[a1["admin"] == "Canada"]
    for code, g in zip(ca["iso_3166_2"].astype(str), ca.to_crs("EPSG:6933").geometry):
        out.append((code.lower(), "ca", g))
    co, _ = mp.load_ref_layer("ref.boundary.world.country.ne110")
    co = co.to_crs("EPSG:6933")
    for iso, g in zip(co["ISO_A2_EH"].astype(str), co.geometry):
        if iso in ("US", "CA", "PR"):
            continue                                       # measured by their states and provinces (Puerto Rico is us-pr among the states)
        out.append(("mx" if iso == "MX" else None, "mx" if iso == "MX" else "other", g))
    _GEO_UNITS = out
    return out


def _geo_from_bbox(b: list[float]) -> str:
    """The narrowest geography vocabulary value that holds a lon/lat box: us-xx or ca-xx when most of its land lies in one state or province,
    else us, ca or mx, else north-america when the three hold nearly all of it, else world. Land is measured in an equal-area CRS (EPSG:6933)
    against the bundled boundaries; a box with no land (open water, a degenerate box off the coast) is world."""
    import numpy as np
    import shapely
    from pyproj import Transformer
    x0, y0, x1, y1 = (float(v) for v in b)
    x0, x1 = max(min(x0, x1), -180.0), min(max(x0, x1), 180.0)
    y0, y1 = max(min(y0, y1), -85.0), min(max(y0, y1), 85.0)
    if x1 - x0 < 1e-3:
        x0, x1 = x0 - 5e-4, x1 + 5e-4
    if y1 - y0 < 1e-3:
        y0, y1 = y0 - 5e-4, y1 + 5e-4
    t = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    box = shapely.transform(shapely.segmentize(shapely.box(x0, y0, x1, y1), 0.25), lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))
    unit: dict[str, float] = {}
    country: dict[str, float] = {}
    for value, ctry, g in _geo_units():
        if not g.intersects(box):
            continue
        a = float(g.intersection(box).area)
        if a <= 0:
            continue
        country[ctry] = country.get(ctry, 0.0) + a
        if value and value not in ("mx",):
            unit[value] = unit.get(value, 0.0) + a
    land = sum(country.values())
    if land <= 0:
        return "world"
    if unit:
        top, a = max(unit.items(), key=lambda kv: kv[1])
        if a / land >= GEO_SHARE_UNIT:
            return top
    ctop, a = max(country.items(), key=lambda kv: kv[1])
    if ctop in ("us", "ca", "mx") and a / land >= GEO_SHARE_COUNTRY:
        return ctop
    if sum(country.get(c, 0.0) for c in ("us", "ca", "mx")) / land >= GEO_SHARE_CONTINENT:
        return "north-america"
    return "world"


def facts_from_file(path: Path) -> dict:
    import geo_inspect
    rep = geo_inspect.inspect(path)
    blocks = [f for f in rep["findings"] if f["severity"] == "BLOCK"]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"origin": "file", "name": path.name, "bytes": path.stat().st_size, "sha256": h.hexdigest(), "format": rep["file"]["format"], "features": rep["features"],
            "geometry": next(iter(rep["geometry_types"]), None), "crs": rep["crs"].get("epsg") or rep["crs"].get("name") or rep["crs"].get("code"), "columns": [a["name"] for a in rep["attributes"]],
            "bbox": rep.get("bbox_wgs84"), "blocking": [f["message"] for f in blocks]}


def facts_from_capture(ref: str) -> dict:
    import map_plan as mp
    try:
        rec = json.loads(mp.find_capture(ref, Path.cwd()).read_text())
    except mp.PlanError as ex:
        raise MaintError(str(ex), gc.EXIT_USAGE, "MAP-LIB-103")
    res = rec.get("result") or {}
    return {"origin": "capture", "name": rec.get("id"), "profile": rec.get("profile"), "layer": (rec.get("request") or {}).get("layer"), "features": res.get("features"), "crs": res.get("crs"),
            "columns": res.get("fields") or [], "bbox": res.get("bbox"), "sha256": res.get("sha256"), "format": res.get("format"), "confidential": bool(rec.get("confidential")),
            "license": rec.get("license") or {}, "geometry": None, "blocking": []}


NEEDS = {"title": "a short name", "level": "what one feature is (county, site, sector, ...)", "use_for": "the map types it serves", "license_class": "the license family",
         "use_when": "one concrete sentence on when to pick it", "avoid_when": "one concrete sentence on when not to"}


def scaffold(facts: dict, answers: dict, tags: dict, types: dict, shipped: list[dict], today: dt.date, local_entries: list[dict]) -> dict:
    """Build a draft entry from verified facts and the person's answers. Returns {entry, questions, findings}; no entry while a required answer is missing."""
    a = dict(answers or {})
    etype = a.get("type") or INFER_TYPE.get(facts.get("geometry") or "", "context")
    guessed = not a.get("type")
    if etype not in types["types"] or types["types"][etype]["family"] != "data":
        raise MaintError(f"type '{etype}' is not a data type; the choices are {', '.join(t for t, s in types['types'].items() if s['family'] == 'data')}", gc.EXIT_USAGE, "MAP-LIB-103")
    q = {k: v for k, v in NEEDS.items() if k in ("title", "level", "use_for", "license_class") and not a.get(k)}
    if q:
        return {"entry": None, "questions": q, "type_guess": etype if guessed else None, "facts": {k: facts.get(k) for k in ("name", "features", "geometry", "crs", "bbox", "columns")}, "findings": []}
    spec = types["types"][etype]
    title = a["title"]
    eid = a.get("id") or f"ref.{etype}.local.{slug(title)}"
    bbox = facts.get("bbox")
    conf = a.get("confidentiality") or ("client-confidential" if (facts.get("confidential") or facts.get("origin") == "file") else "public")
    audience = a.get("audience") or ("any" if conf == "public" else "internal")
    lic_class = a["license_class"]
    red, com, bulk = LICENSE_FLAGS.get(lic_class, (False, False, False))
    delivery = {"mode": "user-supplied" if facts["origin"] == "file" else "service"}
    if facts["origin"] == "file":
        delivery.update({"file_name": facts["name"], "format": facts.get("format"), "size_mb": round(facts["bytes"] / (1024 * 1024), 2), "sha256": facts["sha256"]})
    else:
        delivery.update({"profile": facts.get("profile"), "layer": facts.get("layer"), "capture": facts.get("name")})
    delivery.update({k: facts[k] for k in ("features", "columns") if facts.get(k)})
    if bbox:
        delivery["bbox"] = [round(float(v), 6) for v in bbox]
    entry = {
        "id": eid, "type": etype, "rev": 1, "status": "draft", "title": title, "summary": a.get("summary") or f"{title}. Added to the local library from {facts['origin']} {facts.get('name')}.",
        "tags": {"geography": _list(a.get("geography") or (_geo_from_bbox(bbox) if bbox else "world")), "level": _list(a["level"]), "domain": _list(a.get("domain") or "general"),
                 "use_for": _list(a["use_for"]), "avoid_for": _list(a.get("avoid_for") or ["engineering-grade"]), "scale": _list(a.get("scale") or (_scale_from_bbox(bbox) if bbox else "regional")),
                 "detail": _list(a.get("detail") or "simplified"), "audience": _list(audience), "confidentiality": _list(conf), "freshness": _list(a.get("freshness") or "irregular"),
                 "tier": _list(a.get("tier") or "T1"), "license_class": _list(lic_class), "quality": _list(a.get("quality") or "community")},
        "use_when": _list(a.get("use_when") or []), "avoid_when": _list(a.get("avoid_when") or []),
        "delivery": delivery, "crs": facts.get("crs") or "unknown",
        "source": {"publisher": a.get("publisher") or "not stated", "product": a.get("product") or facts.get("name")},
        "vintage": {"current": str(a.get("vintage") or "not stated"), "cadence": a.get("freshness") or "irregular"},
        "license": {"class": lic_class, "redistribute_ok": red, "commercial_ok": com, "bulk_ok": bulk, "note": a.get("license_note") or "Stated by the person who added this entry; not verified."},
        "maintenance": {"owner": a.get("owner") or "local", "reviewed_on": today.isoformat(), "verify_by": add_months(today, spec["cadence_months"]).isoformat(),
                        "changelog": [{"rev": 1, "date": today.isoformat(), "note": f"Scaffolded as a draft from {facts['origin']} {facts.get('name')}: {facts.get('features', '?')} features, CRS {facts.get('crs') or 'unknown'}."}]},
    }
    if "join" in spec.get("extra_fields", []) or a.get("join_key"):
        entry["join"] = {"key": a.get("join_key") or "not stated", "type": a.get("join_type") or "string", "note": a.get("join_note") or "Not verified: check the key and its width before joining."}
    if a.get("supersedes"):
        entry["supersedes"] = _list(a["supersedes"])
    if a.get("pairs_with"):
        entry["pairs_with"] = _list(a["pairs_with"])
    findings = []
    if guessed:
        findings.append({"id": "MAP-LIB-104", "severity": "INFO", "message": f"type '{etype}' was guessed from the geometry; confirm it"})
    if conf != "public":
        findings.append({"id": "MAP-LIB-105", "severity": "INFO", "message": f"confidentiality is '{conf}': maps built on this entry carry the confidentiality banner and are not shared"})
    if facts.get("blocking"):
        findings.append({"id": "MAP-LIB-106", "severity": "WARN", "message": "the data has problems that block mapping: " + "; ".join(facts["blocking"])[:300]})
    ids = {x.get("id") for x in shipped}
    if eid in ids and not a.get("supersedes"):
        raise MaintError(f"'{eid}' is already a shipped entry; a local entry may replace it only by naming it in supersedes", gc.EXIT_REFUSED, "MAP-LIB-107")
    if any(x.get("id") == eid for x in local_entries) and not a.get("replace"):
        raise MaintError(f"'{eid}' is already in the local library; pass replace to overwrite it", gc.EXIT_REFUSED, "MAP-LIB-107")
    missing = [k for k in ("use_when", "avoid_when") if not entry[k]]
    return {"entry": entry, "questions": {k: NEEDS[k] for k in missing}, "findings": findings, "missing_to_activate": missing, "family": spec["family"]}


def _list(v):
    return list(v) if isinstance(v, (list, tuple)) else [v]


def write_local(entry: dict, family: str, lib: Path, shipped_lib: Path, shipped: list[dict], today: dt.date) -> dict:
    """Write the draft after the library lint accepts it (the link and tag checks need the shipped entries)."""
    e = copy.deepcopy(entry)
    d = lib / "entries" / FAMILY_DIR.get(family, "data")
    p = d / f"{e['id']}.yaml"
    e["_path"], e["_scope"] = str(p), "local"
    fs = [x for x in ll.lint_entries(shipped + [e], shipped_lib, plugin_root=None, today=today) if x["severity"] == "BLOCK" and x["entry"] == e["id"]]
    if fs:
        raise MaintError("the draft fails the library lint: " + "; ".join(f"{x['id']}: {x['message']}" for x in fs[:4]), gc.EXIT_LINT, "MAP-LIB-108")
    d.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(clean(e), sort_keys=False, allow_unicode=True, width=160))
    return {"path": str(p), "id": e["id"]}
