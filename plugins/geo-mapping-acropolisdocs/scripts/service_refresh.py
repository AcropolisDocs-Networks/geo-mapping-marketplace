#!/usr/bin/env python3
"""service_refresh: ask a service again for a capture you already hold, and say what changed (spec `service-refresh`, MAP-SVC-011).

    service_refresh.py list    [--older-than DAYS]
    service_refresh.py refresh CAPTURE [--apply] [--key FIELD] [--threshold PCT] [--accept-over-cap]
    service_refresh.py compare OLD NEW [--key FIELD] [--threshold PCT]
    service_refresh.py apply   CAPTURE

`refresh` repeats the request recorded in the capture (a public service the plugin reached itself), writes the new capture to a staging folder and a drift report
beside it, and changes nothing you use until you say so: `--apply`, or `apply` later, moves the old capture into `_history/` and puts the new one in its place, so
every map that names the capture id picks up the new data on its next build. A capture from your own request kit cannot be refreshed here (the plugin cannot reach
that service): run the kit again, ingest the folder under a new name, and `compare` the two.

The drift report covers the fields (added, removed, type changed), the feature count, the extent, and the rows: added, removed and changed when the layer has a stable
key, otherwise the shift of each numeric column. Schema and count drift are warnings (MAP-SVC-011), never silent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402

KEYS = ("id", "objectid", "fid", "gid", "gml_id", "ogc_fid", "ident", "globalid")
THRESHOLD_PCT = 10.0
EXTENT_PCT = 25.0


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def blocked(fs) -> bool:
    return any(f["severity"] == "BLOCK" for f in fs)


def root_of(captures_root: Path | None = None) -> Path:
    return Path(captures_root) if captures_root else gc.workdir() / "captures"


def locate(ref: str, captures_root: Path | None = None) -> Path:
    p = Path(ref)
    cands = [p, root_of(captures_root) / ref]
    for c in cands:
        c = c / "capture.json" if c.is_dir() else c
        if c.is_file():
            return c
    raise FileNotFoundError(f"capture '{ref}' was not found in {root_of(captures_root)}")


def load(ref: str, captures_root: Path | None = None):
    """(record, GeoDataFrame or None, record path)."""
    import geo_io
    cp = locate(ref, captures_root)
    rec = json.loads(cp.read_text())
    res = rec.get("result") or {}
    g = None
    if res.get("path"):
        rp = Path(res["path"])
        if not rp.is_file():
            rp = cp.parent / rp.name
        if rp.is_file():
            if res.get("sha256") and hashlib.sha256(rp.read_bytes()).hexdigest() != res["sha256"]:
                raise ValueError(f"the data file of capture {rec.get('id')} changed after the capture was recorded (checksum differs)")
            g = geo_io.read(rp).gdf
    return rec, g, cp


# ---------------------------------------------------------------------------------------------------------------- the comparison
def _row_hashes(g, cols):
    import shapely
    attrs = g[cols].astype(str).agg("\x1f".join, axis=1) if cols else None
    wkb = shapely.to_wkb(shapely.set_precision(g.geometry.values, 1e-7), output_dimension=2)
    out = []
    for i in range(len(g)):
        h = hashlib.sha1()
        if attrs is not None:
            h.update(attrs.iloc[i].encode())
        h.update(wkb[i] or b"")
        out.append(h.hexdigest())
    return out


def find_key(old, new, key: str | None):
    cands = [key] if key else [c for c in old.columns if c.lower() in KEYS]
    for c in cands:
        if c in old.columns and c in new.columns and old[c].notna().all() and new[c].notna().all() and old[c].is_unique and new[c].is_unique:
            return c
    return None


def drift(old_rec: dict, new_rec: dict, old, new, *, key: str | None = None, threshold_pct: float = THRESHOLD_PCT) -> dict:
    """What changed between two captures of the same layer. `old` and `new` are GeoDataFrames (or None for an empty capture)."""
    import numpy as np
    import pandas as pd
    fs: list[dict] = []
    oc = [c for c in (old.columns if old is not None else (old_rec.get("result") or {}).get("fields") or []) if c != "geometry"]
    nc = [c for c in (new.columns if new is not None else (new_rec.get("result") or {}).get("fields") or []) if c != "geometry"]
    added, removed = sorted(set(nc) - set(oc)), sorted(set(oc) - set(nc))
    retyped = {}
    if old is not None and new is not None:
        for c in sorted(set(oc) & set(nc)):
            a, b = str(old[c].dtype), str(new[c].dtype)
            if a != b and not ({a, b} <= {"int64", "float64", "Int64"}):
                retyped[c] = [a, b]
    schema = {"added": added, "removed": removed, "retyped": retyped, "changed": bool(added or removed or retyped)}
    if schema["changed"]:
        bits = ([f"new fields: {', '.join(added[:8])}"] if added else []) + ([f"fields gone: {', '.join(removed[:8])}"] if removed else []) + \
               ([f"type changed: {', '.join(f'{k} {v[0]}→{v[1]}' for k, v in list(retyped.items())[:5])}"] if retyped else [])
        fs.append(finding("MAP-SVC-011", "WARN", "the layer's fields changed since the earlier capture (" + "; ".join(bits) + "); check any map that uses them", schema))
    n0, n1 = (len(old) if old is not None else 0), (len(new) if new is not None else 0)
    pct = None if n0 == 0 else round((n1 - n0) / n0 * 100, 2)
    count = {"old": n0, "new": n1, "delta": n1 - n0, "pct": pct}
    if n0 != n1 and (pct is None or abs(pct) >= threshold_pct):
        fs.append(finding("MAP-SVC-011", "WARN", f"the feature count went from {n0:,} to {n1:,}" + (f" ({pct:+.1f}%)" if pct is not None else ""), count))
    elif n0 != n1:
        fs.append(finding("MAP-SVC-011", "INFO", f"the feature count went from {n0:,} to {n1:,} ({pct:+.1f}%)", count))
    ob, nb = (old_rec.get("result") or {}).get("bbox"), (new_rec.get("result") or {}).get("bbox")
    extent = {"old": ob, "new": nb, "shift_pct": None}
    if ob and nb:
        span = max(ob[2] - ob[0], ob[3] - ob[1], 1e-9)
        shift = max(abs(a - b) for a, b in zip(ob, nb, strict=True)) / span * 100
        extent["shift_pct"] = round(shift, 2)
        if shift >= EXTENT_PCT:
            fs.append(finding("MAP-SVC-011", "WARN", f"the extent of the data moved by {shift:.0f}% of its size; a map fitted to the layers will look different", extent))
    rows, numeric = None, []
    if old is not None and new is not None and n0 and n1:
        common = [c for c in oc if c in nc]
        k = find_key(old, new, key)
        if k:
            o, n = old.set_index(k, drop=False), new.set_index(k, drop=False)
            add_ids, rem_ids = sorted(set(n.index) - set(o.index), key=str), sorted(set(o.index) - set(n.index), key=str)
            both = [i for i in o.index if i in n.index]
            by_col: dict[str, int] = {}
            changed_ids = set()
            for c in [c for c in common if c != k]:
                a, b = o.loc[both, c].astype(str), n.loc[both, c].astype(str)
                diff = (a.values != b.values)
                if diff.any():
                    by_col[c] = int(diff.sum())
                    changed_ids |= {both[i] for i in np.where(diff)[0]}
            geom_changed = 0
            if both:
                ga, gb = o.loc[both].geometry.values, n.loc[both].geometry.values
                import shapely
                geom_changed = int((~shapely.equals_exact(ga, gb, 1e-7)).sum())
            rows = {"key": k, "added": len(add_ids), "removed": len(rem_ids), "attributes_changed": len(changed_ids), "geometry_changed": geom_changed, "by_column": dict(sorted(by_col.items(), key=lambda kv: -kv[1])[:10]),
                    "examples": {"added": [str(i) for i in add_ids[:5]], "removed": [str(i) for i in rem_ids[:5]]}}
            moved = len(add_ids) + len(rem_ids) + len(changed_ids | set()) + geom_changed
            share = moved / max(n0, 1) * 100
            if moved and share >= threshold_pct:
                fs.append(finding("MAP-SVC-011", "WARN", f"{len(add_ids):,} features added, {len(rem_ids):,} removed, {len(changed_ids):,} with changed attributes and {geom_changed:,} with changed shapes (by '{k}')", rows))
            elif moved:
                fs.append(finding("MAP-SVC-011", "INFO", f"{len(add_ids):,} added, {len(rem_ids):,} removed, {len(changed_ids):,} attribute changes, {geom_changed:,} shape changes (by '{k}')", rows))
        else:
            for c in common:
                if pd.api.types.is_numeric_dtype(old[c]) and pd.api.types.is_numeric_dtype(new[c]):
                    a, b = old[c].astype(float), new[c].astype(float)
                    ma, mb = float(a.mean()) if a.notna().any() else None, float(b.mean()) if b.notna().any() else None
                    sh = None if (ma in (None, 0) or mb is None) else round((mb - ma) / abs(ma) * 100, 2)
                    row = {"field": c, "mean_old": ma, "mean_new": mb, "shift_pct": sh, "nulls_old": int(a.isna().sum()), "nulls_new": int(b.isna().sum())}
                    numeric.append(row)
                    if sh is not None and abs(sh) >= threshold_pct:
                        fs.append(finding("MAP-SVC-011", "WARN", f"the average of '{c}' moved {sh:+.1f}% ({ma:.4g} to {mb:.4g}); the layer has no stable key, so rows cannot be matched", row))
    identical = False
    if old is not None and new is not None and not schema["changed"] and n0 == n1:
        cols = [c for c in oc if c in nc]
        identical = sorted(_row_hashes(old, cols)) == sorted(_row_hashes(new, cols))
    elif old is None and new is None:
        identical = True
    if identical:
        fs.append(finding("MAP-SVC-011", "INFO", "no change: the same fields, the same number of features and the same rows"))
    flags = []
    if new_rec.get("truncated") and not old_rec.get("truncated"):
        fs.append(finding("MAP-SVC-005", "WARN", "the new capture is incomplete (the service cut it short) and the earlier one was not", new_rec.get("server_limit")))
        flags.append("new capture truncated")
    summary = "no change" if identical else "; ".join(
        ([f"fields changed ({len(added)} new, {len(removed)} gone, {len(retyped)} retyped)"] if schema["changed"] else []) +
        ([f"count {n0:,} → {n1:,}" + (f" ({pct:+.1f}%)" if pct is not None else "")] if n0 != n1 else []) +
        ([f"rows: +{rows['added']} −{rows['removed']} ~{rows['attributes_changed']}"] if rows and (rows['added'] or rows['removed'] or rows['attributes_changed']) else []) +
        (["values shifted"] if any(f["severity"] == "WARN" and "average" in f["message"] for f in fs) else []) or ["small changes"])
    return {"identical": identical, "schema": schema, "count": count, "extent": extent, "rows": rows, "numeric": numeric, "flags": flags, "findings": fs, "summary": summary,
            "old": {"id": old_rec.get("id"), "finished_at": old_rec.get("finished_at")}, "new": {"id": new_rec.get("id"), "finished_at": new_rec.get("finished_at")}}


def compare(old_ref: str, new_ref: str, *, key: str | None = None, threshold_pct: float = THRESHOLD_PCT, captures_root: Path | None = None) -> dict:
    o, og, _ = load(old_ref, captures_root)
    n, ng, _ = load(new_ref, captures_root)
    if (o.get("request") or {}).get("layer") and (n.get("request") or {}).get("layer") and o["request"]["layer"] != n["request"]["layer"]:
        return {"ok": True, "report": {**drift(o, n, og, ng, key=key, threshold_pct=threshold_pct)},
                "findings": [finding("MAP-SVC-011", "WARN", "the two captures are of different layers; the comparison may not mean much")]}
    r = drift(o, n, og, ng, key=key, threshold_pct=threshold_pct)
    return {"ok": True, "report": r, "findings": r["findings"]}


# ---------------------------------------------------------------------------------------------------------------- list
def list_captures(*, older_than_days: int | None = None, captures_root: Path | None = None, today: dt.date | None = None) -> list[dict]:
    if today is None:
        fixed = __import__("os").environ.get("GMAP_FIXED_DATE")
        today = dt.date.fromisoformat(fixed) if fixed else dt.date.today()
    out = []
    root = root_of(captures_root)
    for d in sorted(root.glob("*/capture.json")) if root.is_dir() else []:
        try:
            r = json.loads(d.read_text())
        except (OSError, ValueError):
            continue
        fin = str(r.get("finished_at") or "")[:10]
        try:
            age = (today - dt.date.fromisoformat(fin)).days
        except ValueError:
            age = None
        row = {"id": r.get("id"), "route": r.get("route"), "profile": r.get("profile"), "layer": (r.get("request") or {}).get("layer"), "features": r.get("received"),
               "captured": fin, "age_days": age, "refreshable": r.get("route") == "mcp", "incomplete": bool(r.get("truncated"))}
        if older_than_days is None or (age is not None and age >= older_than_days):
            out.append(row)
    return out


# ---------------------------------------------------------------------------------------------------------------- refresh
def refresh(http, ref: str, *, apply: bool = False, key: str | None = None, threshold_pct: float = THRESHOLD_PCT, accept_over_cap: bool = False,
            captures_root: Path | None = None, profile: dict | None = None) -> dict:
    import service_query as sq
    root = root_of(captures_root)
    try:
        old, og, cp = load(ref, captures_root)
    except (FileNotFoundError, ValueError) as ex:
        return {"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", str(ex))]}
    if old.get("route") == "device-run":
        return {"ok": False, "findings": [finding("MAP-SVC-028", "BLOCK", "this capture came from an approved run on your computer, which the bundled server cannot reach. Plan the same run again "
                                                  "(approved_run.py plan-curl or plan-kit; a standing approval covers the same query shape), run it, finish it under a new name, then compare: service_refresh.py compare OLD NEW")]}
    if old.get("route") != "mcp":
        return {"ok": False, "findings": [finding("MAP-SVC-002", "BLOCK", "this capture came from your own request kit, which this plugin cannot reach. Run the same kit again, ingest the new folder under a new name "
                                                  "(request_kit.py ingest FOLDER --name NAME), then compare: service_refresh.py compare OLD NEW")]}
    asked = dict(old.get("asked") or {k: v for k, v in (old.get("request") or {}).items() if k in ("layer", "version", "cql_filter", "bbox", "srs", "properties", "sort_by", "page_size", "max_features")})
    asked.update(profile=old["profile"], protocol=old["protocol"], accept_over_cap=accept_over_cap)
    req = sq.request_from(asked)
    try:
        prof = profile or sq.need_mcp_profile(old["profile"])
    except (FileNotFoundError, oh.ServiceError) as ex:
        return {"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", f"the service profile '{old['profile']}' cannot be used: {oh.untrusted(ex, 200)}")]}
    stage_root = root / "_refresh"
    shutil.rmtree(stage_root / old["id"], ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    res = sq.capture(http, prof, req, outroot=stage_root, resume=False)
    if not res.get("record"):
        return {"ok": False, "findings": res["findings"]}
    new = res["record"]
    staged = Path(res["record_path"]).parent
    if staged.name != old["id"]:
        shutil.rmtree(stage_root / old["id"], ignore_errors=True)
        staged.rename(stage_root / old["id"])
        staged = stage_root / old["id"]
        new["id"] = old["id"]
        res["record_path"] = str(staged / "capture.json")
    new_g = None
    if (new.get("result") or {}).get("path"):
        import geo_io
        new_g = geo_io.read(staged / Path(new["result"]["path"]).name).gdf
    report = drift(old, new, og, new_g, key=key, threshold_pct=threshold_pct)
    report["applied"] = False
    fs = list(res["findings"]) + [f for f in report["findings"]]
    (staged / "drift.json").write_text(json.dumps(report, indent=2, default=str))
    out = {"ok": res["ok"], "report": report, "staged": str(staged), "findings": fs, "new_capture": new["id"]}
    if res["ok"] and apply:
        p = promote(old["id"], captures_root=captures_root, drift_report=report)
        out["findings"] += p["findings"]
        out["applied"] = p["ok"]
        report["applied"] = p["ok"]
        out["history"] = p.get("history")
    elif res["ok"]:
        out["findings"].append(finding("MAP-SVC-011", "INFO", f"not applied: your maps still use the earlier capture. To switch, run: service_refresh.py apply {old['id']}"))
    else:
        out["findings"].append(finding("MAP-SVC-011", "INFO", "the new capture has blocking problems and cannot be applied"))
    return out


def promote(cid: str, *, captures_root: Path | None = None, drift_report: dict | None = None) -> dict:
    """Move the earlier capture to _history/<id>/<date> and put the staged one in its place."""
    root = root_of(captures_root)
    staged, live = root / "_refresh" / cid, root / cid
    if not (staged / "capture.json").is_file():
        return {"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", f"there is no staged refresh for '{cid}'; run refresh first")]}
    new = json.loads((staged / "capture.json").read_text())
    if any(f["severity"] == "BLOCK" for f in new.get("findings") or []):
        return {"ok": False, "findings": [finding("MAP-SVC-011", "BLOCK", "the staged capture has blocking findings and is not applied")]}
    old = json.loads((live / "capture.json").read_text()) if (live / "capture.json").is_file() else {}
    stamp = str(old.get("finished_at") or "earlier")[:19].replace(":", "").replace("-", "")
    hist = root / "_history" / cid / stamp
    n = 1
    while hist.exists():
        n += 1
        hist = root / "_history" / cid / f"{stamp}-{n}"
    if live.exists():
        hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(live), str(hist))
    shutil.move(str(staged), str(live))
    res = new.get("result") or {}
    if res.get("path"):
        res["path"] = str(live / Path(res["path"]).name)
    rpt = drift_report
    if rpt is None and (live / "drift.json").is_file():
        rpt = json.loads((live / "drift.json").read_text())
    new["refreshed"] = {"from": {"finished_at": old.get("finished_at"), "received": old.get("received"), "history": str(hist) if old else None},
                        "summary": (rpt or {}).get("summary"), "identical": (rpt or {}).get("identical")}
    (live / "capture.json").write_text(json.dumps(new, indent=2, default=str))
    return {"ok": True, "history": str(hist) if old else None, "findings": [finding("MAP-SVC-011", "INFO", f"applied: '{cid}' now holds the new capture; the earlier one is kept in _history")]}


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None, http=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("list")
    p.add_argument("--older-than", type=int)
    for name in ("refresh", "apply"):
        p = sub.add_parser(name)
        p.add_argument("capture")
    p = sub.add_parser("compare")
    p.add_argument("old")
    p.add_argument("new")
    for name in ("refresh", "compare"):
        q = sub.choices[name]
        q.add_argument("--key")
        q.add_argument("--threshold", type=float, default=THRESHOLD_PCT)
    sub.choices["refresh"].add_argument("--apply", action="store_true")
    sub.choices["refresh"].add_argument("--accept-over-cap", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.op == "list":
            print(json.dumps(list_captures(older_than_days=a.older_than), indent=2))
            return 0
        if a.op == "compare":
            r = compare(a.old, a.new, key=a.key, threshold_pct=a.threshold)
            print(json.dumps(r, indent=2, default=str))
            return 0
        if a.op == "apply":
            r = promote(a.capture)
            print(json.dumps(r, indent=2, default=str))
            return 0 if r["ok"] else 3
        import service_query as sq
        if http is None:
            try:
                old = json.loads(locate(a.capture).read_text())
                http = sq.make_http(sq.need_mcp_profile(old["profile"]))
            except (FileNotFoundError, oh.ServiceError) as ex:
                print(json.dumps({"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", oh.untrusted(ex, 300))]}))
                return 3
        r = refresh(http, a.capture, apply=a.apply, key=a.key, threshold_pct=a.threshold, accept_over_cap=a.accept_over_cap)
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ok"] else 3
    except oh.ServiceError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule or "MAP-SVC-018", "BLOCK", oh.untrusted(ex, 300))]}))
        return 5 if ex.auth else 3


if __name__ == "__main__":
    sys.exit(main())
