#!/usr/bin/env python3
"""service_harvest: capture several layers of one public service in a single, confirmed run (spec `service-harvest`).

    service_harvest.py --profile P --layers a,b,c [--cql ...] [--bbox=...] [--max-features N] [--page-size N]
                       [--yes | --confirm-total N] [--max-total N] [--refresh-existing [--apply]] [--accept-over-cap]

Without `--yes` (or `--confirm-total` with a number at least as big as the estimate) nothing is fetched: the run reports how many features each layer would bring and the
total. A layer already captured with the same request is left alone unless `--refresh-existing` is given, in which case it goes through service_refresh and a drift report.
An authentication failure stops the whole run; any other failure is recorded for that layer and the run goes on. The index of the run is written to
`<working folder>/captures/_harvests/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import ogc_http as oh  # noqa: E402

MAX_LAYERS = 40
DEFAULT_TOTAL = 500_000


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def estimate(http, profile: dict, layers: list[str], shared: dict, *, accept_over_cap: bool = False, captures_root: Path | None = None) -> dict:
    """Ask the service how big each layer's request is. Fetches no features."""
    import service_query as sq
    import service_refresh as srf
    root = srf.root_of(captures_root)
    rows, fs = [], []
    for name in layers:
        req = sq.request_from({**shared, "profile": profile["profile"], "layer": name, "accept_over_cap": accept_over_cap})
        try:
            mp = sq.make_plan(http, profile, req)
        except oh.ServiceError as ex:
            if ex.auth:
                raise
            rows.append({"layer": name, "ok": False, "problem": oh.untrusted(ex, 200)})
            continue
        if not mp["ok"]:
            rows.append({"layer": name, "ok": False, "problem": "; ".join(f["message"] for f in mp["findings"] if f["severity"] == "BLOCK")[:300], "findings": mp["findings"]})
            continue
        plan = mp["plan"]
        cid = sq.capture_id(profile, req, plan.layer)
        existing = (root / cid / "capture.json").is_file()
        rows.append({"layer": plan.layer, "ok": True, "hits": mp["hits"], "max_features": plan.max_features, "pages": (-(-mp["hits"] // plan.page_size) if mp["hits"] is not None else None),
                     "capture_id": cid, "exists": existing, "request": req, "findings": mp["findings"]})
    return {"rows": rows, "findings": fs}


def total_of(rows: list[dict]) -> int:
    t = 0
    for r in rows:
        if r.get("ok"):
            n = r["hits"] if r["hits"] is not None else (r.get("max_features") or 0)
            t += min(n, r["max_features"]) if r.get("max_features") else n
    return t


def harvest(http, profile_name: str, layers: list[str], shared: dict | None = None, *, confirm: bool = False, confirm_total: int | None = None, max_total: int | None = None,
            refresh_existing: bool = False, apply: bool = False, accept_over_cap: bool = False, captures_root: Path | None = None, profile: dict | None = None) -> dict:
    import service_query as sq
    import service_refresh as srf
    shared = dict(shared or {})
    layers = [l for l in dict.fromkeys(x.strip() for x in layers) if l]
    if not layers:
        return {"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", "name at least one layer")]}
    if len(layers) > MAX_LAYERS:
        return {"ok": False, "findings": [finding("MAP-SVC-004", "BLOCK", f"{len(layers)} layers is more than one harvest takes ({MAX_LAYERS}); split it")]}
    try:
        prof = profile or sq.need_mcp_profile(profile_name)
    except (FileNotFoundError, oh.ServiceError) as ex:
        return {"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", f"the service profile '{profile_name}' cannot be used: {oh.untrusted(ex, 200)}")]}
    est = estimate(http, prof, layers, shared, accept_over_cap=accept_over_cap, captures_root=captures_root)
    rows = est["rows"]
    total = total_of(rows)
    limit = int(max_total or (prof.get("limits") or {}).get("max_harvest_features") or DEFAULT_TOTAL)
    fs = list(est["findings"])
    public_rows = [{k: v for k, v in r.items() if k not in ("request", "findings")} for r in rows]
    bad = [r for r in rows if not r.get("ok")]
    unknown = [r for r in rows if r.get("ok") and r["hits"] is None and not r.get("max_features")]
    if unknown:
        fs.append(finding("MAP-SVC-004", "BLOCK", f"the service does not say how many features {', '.join(r['layer'] for r in unknown)} holds; set max_features to bound the request"))
    if total > limit:
        fs.append(finding("MAP-SVC-004", "BLOCK", f"{total:,} features in all is more than this harvest may take ({limit:,}); narrow the request, take fewer layers, or raise --max-total knowingly"))
    if bad:
        fs.append(finding("MAP-SVC-003", "WARN", f"{len(bad)} layer(s) cannot be requested and will be skipped: " + "; ".join(f"{r['layer']}: {r['problem']}" for r in bad[:5])))
    ok_rows = [r for r in rows if r.get("ok")]
    if not ok_rows:
        return {"ok": False, "estimate": public_rows, "total": total, "findings": fs + [finding("MAP-SVC-003", "BLOCK", "no layer can be requested")]}
    if any(f["severity"] == "BLOCK" for f in fs):
        return {"ok": False, "estimate": public_rows, "total": total, "findings": fs}
    confirmed = bool(confirm) or (confirm_total is not None and confirm_total >= total)
    if confirm_total is not None and confirm_total < total:
        fs.append(finding("MAP-SVC-004", "WARN", f"--confirm-total {confirm_total:,} is below the estimate ({total:,}); nothing was fetched"))
    if not confirmed:
        fs.append(finding("MAP-SVC-004", "INFO", f"nothing was fetched. {total:,} features in {len(ok_rows)} layer(s) would be requested; run again with --yes (or --confirm-total {total}) to go ahead"))
        return {"ok": True, "confirmed": False, "estimate": public_rows, "total": total, "findings": fs}
    results = []
    stopped = None
    for r in ok_rows:
        item = {"layer": r["layer"], "capture_id": r["capture_id"]}
        if r["exists"] and not refresh_existing:
            item.update(status="kept", note="already captured with the same request; add --refresh-existing to ask again and see what changed")
            results.append(item)
            continue
        try:
            if r["exists"]:
                rr = srf.refresh(http, r["capture_id"], apply=apply, accept_over_cap=accept_over_cap, captures_root=captures_root, profile=prof)
                rep = rr.get("report") or {}
                item.update(status=("refreshed" if rr.get("applied") else "staged") if rr["ok"] else "failed", drift=rep.get("summary"), identical=rep.get("identical"),
                            problems=[f["message"] for f in rr["findings"] if f["severity"] == "BLOCK"][:3])
            else:
                res = sq.capture(http, prof, r["request"], outroot=captures_root, resume=False)
                rec = res.get("record")
                item.update(status="captured" if res["ok"] else "failed", features=(rec or {}).get("received"), expected=(rec or {}).get("hits_expected"), truncated=(rec or {}).get("truncated"),
                            problems=[f["message"] for f in res["findings"] if f["severity"] == "BLOCK"][:3])
        except oh.ServiceError as ex:
            if ex.auth:
                item.update(status="stopped", problems=[oh.untrusted(ex, 200)])
                results.append(item)
                stopped = f"the service asked for authentication at '{r['layer']}'; the harvest was stopped (not retried)"
                break
            item.update(status="failed", problems=[oh.untrusted(ex, 200)])
        results.append(item)
    if stopped:
        fs.append(finding("MAP-SVC-018", "BLOCK", stopped))
    done = [x for x in results if x["status"] in ("captured", "refreshed", "staged")]
    failed = [x for x in results if x["status"] in ("failed", "stopped")]
    if failed:
        fs.append(finding("MAP-SVC-003", "WARN", f"{len(failed)} layer(s) failed: " + "; ".join(f"{x['layer']}: {(x.get('problems') or ['failed'])[0]}" for x in failed[:5])))
    trunc = [x for x in results if x.get("truncated")]
    if trunc:
        fs.append(finding("MAP-SVC-005", "WARN", f"{len(trunc)} capture(s) are incomplete (the service cut them short): " + ", ".join(x["layer"] for x in trunc[:6])))
    root = srf.root_of(captures_root)
    hid = f"{sq.slug(prof['profile'])}-harvest-{hashlib.sha256(json.dumps([layers, shared], sort_keys=True, default=str).encode()).hexdigest()[:8]}"
    index = {"harvest_id": hid, "finished_at": sq.now(), "profile": prof["profile"], "layers": layers, "estimate_total": total, "results": results}
    (root / "_harvests").mkdir(parents=True, exist_ok=True)
    (root / "_harvests" / f"{hid}.json").write_text(json.dumps(index, indent=2, default=str))
    return {"ok": not stopped and bool(done or all(x["status"] == "kept" for x in results)) and not (failed and not done), "confirmed": True, "estimate": public_rows, "total": total, "results": results,
            "index": str(root / "_harvests" / f"{hid}.json"), "findings": fs}


def main(argv=None, http=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--layers", required=True, help="comma-separated layer names")
    ap.add_argument("--cql")
    ap.add_argument("--bbox")
    ap.add_argument("--properties")
    ap.add_argument("--max-features", type=int)
    ap.add_argument("--page-size", type=int)
    ap.add_argument("--protocol", choices=["wfs", "ogcapi", "arcgis"], default="wfs")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--confirm-total", type=int)
    ap.add_argument("--max-total", type=int)
    ap.add_argument("--refresh-existing", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--accept-over-cap", action="store_true")
    a = ap.parse_args(argv)
    import service_query as sq
    shared = {"protocol": a.protocol, "cql_filter": a.cql, "bbox": [float(v) for v in a.bbox.split(",")] if a.bbox else None,
              "properties": a.properties.split(",") if a.properties else None, "max_features": a.max_features, "page_size": a.page_size}
    try:
        if http is None:
            http = sq.make_http(sq.need_mcp_profile(a.profile))
        r = harvest(http, a.profile, a.layers.split(","), shared, confirm=a.yes, confirm_total=a.confirm_total, max_total=a.max_total, refresh_existing=a.refresh_existing, apply=a.apply,
                    accept_over_cap=a.accept_over_cap)
    except oh.ServiceError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule or "MAP-SVC-018", "BLOCK", oh.untrusted(ex, 300))]}))
        return 5 if ex.auth else 3
    except FileNotFoundError as ex:
        print(json.dumps({"ok": False, "findings": [finding("MAP-SVC-003", "BLOCK", oh.untrusted(ex, 300))]}))
        return 3
    print(json.dumps(r, indent=2, default=str))
    return 0 if r["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
