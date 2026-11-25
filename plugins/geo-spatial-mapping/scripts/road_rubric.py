#!/usr/bin/env python3
"""road_rubric: how a map was made, in numbers (4.2.0; the road file, road.yaml, beside the picture rubric).

The map trace calls `measure(summary)` with its own summary and prints `lines(road)`. Everything comes from records the session already
left: the trace's counts, the finished maps' result files (`<stem>.result.json`, the deviations table) and their provenance sidecars. No
model reads anything and nothing here changes a map. Standard library and PyYAML only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROAD_FILE = HERE.parent / "evaluation" / "road.yaml"
FORMAT_SUFFIX = re.compile(r"\.(svg|pdf|png|html|mp4|gif|jpg|jpeg|webp)$", re.I)


def version() -> str | None:
    try:
        import yaml
        return str((yaml.safe_load(ROAD_FILE.read_text(encoding="utf-8")) or {}).get("road_version"))
    except Exception:  # noqa: BLE001
        return None


def _stem(path: str) -> Path:
    return Path(FORMAT_SUFFIX.sub("", str(path)))


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def output_records(outputs: list[dict]) -> list[dict]:
    """One row per finished map: its deviations (from the result file) and its layers (from the provenance sidecar)."""
    rows, seen = [], set()
    for o in outputs or []:
        st = _stem(o.get("path") or "")
        if not st.name or str(st) in seen:
            continue
        seen.add(str(st))
        res = _json(st.with_name(st.name + ".result.json"))
        prov = _json(st.with_name(st.name + ".provenance.json"))
        if res is None and prov is None:
            continue
        layers = []
        for r in (prov or {}).get("records") or []:
            lid = str(r.get("layer_id") or "")
            if lid.startswith(("basemap:", "context:", "locator:", "names:", "relief:")) or ":basemap:" in lid:
                continue
            ops = [t for t in (r.get("transformations") or []) if isinstance(t, (str, dict)) and "op" in str(t).lower()]
            door = "library" if r.get("library_ref") else "service" if (r.get("capture") or r.get("service")) else "op" if (r.get("ops") or ops) else "file"
            layers.append({"layer": lid, "door": door, "library_ref": r.get("library_ref")})
        rows.append({"id": o.get("id") or st.name, "stem": st.name, "job": job_of(st), "deviations": ((res or {}).get("deviations") or {}).get("counts"), "layers": layers,
                     "outcome": ((prov or {}).get("spec") or {}).get("outcome") if isinstance((prov or {}).get("spec"), dict) else None})
    return rows


def job_of(stem: Path) -> dict | None:
    """5.0.0: the job that built this map (gmap.py), from the job records beside its output folder: what the road itself recorded."""
    import os
    roots = [base / ".geo-mapping" / "jobs" for base in (stem.parent.parent, stem.parent)]
    if os.environ.get("GEO_MAPPING_HOME"):               # the working folder was put somewhere else: its jobs are there
        roots.append(Path(os.environ["GEO_MAPPING_HOME"]) / "jobs")
    for jobs in roots:
        for jf in sorted(jobs.glob("*/job.json")) if jobs.is_dir() else []:
            j = _json(jf)
            if not isinstance(j, dict):
                continue
            builds = [b for b in j.get("builds") or [] if b.get("stem") == stem.name and not b.get("blocked")]
            if not builds:
                continue
            needs = j.get("needs") or []
            data = j.get("data") or []
            enrich = j.get("enrich") or []
            return {"id": j.get("id"), "intent": (j.get("brief") or {}).get("intent"), "needs": len(needs),
                    "sources_seen": sum(len(n.get("candidates") or []) for n in needs), "sources_taken": sorted({n["chosen"] for n in needs if n.get("chosen")}),
                    "offered": len(enrich), "answered": sum(1 for e in enrich if e.get("answer")),
                    "registered_steps": [d.get("name") for d in data if d.get("door") == "registered step"],
                    "cold_read": (j.get("cold_read") or {}).get("made"), "put_back": list(builds[-1].get("reverted") or []), "drafts": len(j.get("drafts") or [])}
    return None


def measure(o: dict) -> dict:
    """The road rubric from a map-trace summary."""
    calls = o.get("calls") or {}
    plugin, total = int(calls.get("map") or 0), int(calls.get("total") or 0)
    outside = o.get("outside_plugin") or []
    fails = o.get("failures") or []
    by_hand = sum(1 for f in fails if (f.get("class") or f.get("error_class")) == "inline-script")
    recs = output_records(o.get("outputs") or [])
    layers = [ly for r in recs for ly in r["layers"]]
    through = [ly for r in recs for ly in r["layers"] if ly["door"] != "file" or r.get("job")]      # 5.0.0: a job records the person's file too
    jobs = [r["job"] for r in recs if r.get("job")]
    libs = sorted({str(ly["library_ref"]) for ly in layers if ly.get("library_ref")})
    dev = [r["deviations"] for r in recs if r.get("deviations")]

    def tot(k):
        return sum(int(d.get(k) or 0) for d in dev) if dev else None
    work = plugin + len(outside)
    return {"road_version": version(), "maps": len(recs),
            "inside_share": round(plugin / work, 3) if work else None,
            "plugin_share_of_session": round(plugin / total, 3) if total else None,
            "plugin_calls": plugin, "session_calls": total, "outside_steps": len(outside),
            "layers": len(layers), "layers_with_record": len(through), "layers_from_files": len(layers) - len(through),
            "library_entries_used": libs,
            "deviations": ({"asked": tot("asked"), "intent": tot("intent"), "unexplained": tot("unexplained"), "free": tot("free")} if dev else None),
            "result_parsed_by_hand": by_hand,
            "maps_on_the_road": len(jobs), "maps_off_the_road": len(recs) - len(jobs),
            "sources_seen": sum(j["sources_seen"] for j in jobs) if jobs else None, "sources_taken": sum(len(j["sources_taken"]) for j in jobs) if jobs else None,
            "cold_reads": sum(1 for j in jobs if j.get("cold_read")) if jobs else None, "put_back": sum(len(j["put_back"]) for j in jobs) if jobs else None,
            "registered_steps": [s for j in jobs for s in j["registered_steps"]], "per_map": recs}


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def lines(r: dict) -> list[str]:
    L = ["", f"Road      how the maps were made (road rubric {r.get('road_version') or '-'}; the picture is graded apart)"]
    L.append(f"  plugin's share of the map work   {_pct(r['inside_share'])}  ({r['plugin_calls']} plugin call(s), {r['outside_steps']} step(s) outside it; "
             f"{_pct(r['plugin_share_of_session'])} of all {r['session_calls']} calls)")
    if r["layers"]:
        L.append(f"  layers through the plugin's door {r['layers_with_record']} of {r['layers']}  ({r['layers_from_files']} from a file with no fetch or op record)")
        L.append(f"  library entries drawn            {len(r['library_entries_used'])}" + (f"  ({', '.join(x.replace('ref.', '') for x in r['library_entries_used'][:5])})" if r["library_entries_used"] else ""))
    else:
        L.append("  layers                           not known (no finished map's provenance was found)")
    d = r.get("deviations")
    L.append("  deviations from the defaults     " + (f"asked {d['asked']}, intent {d['intent']}, unexplained {d['unexplained']}; free choices {d['free']}" if d
                                                     else "not known (maps built before 4.2.0 carry no deviations table)"))
    L.append(f"  build results parsed by hand     {r['result_parsed_by_hand']}")
    if r.get("maps"):                                   # 5.0.0: which maps took the road (a gmap job), and what the job recorded
        L.append(f"  maps made on the road            {r.get('maps_on_the_road', 0)} of {r['maps']}" + (f"  ({r['maps_off_the_road']} built with no job: an unmanaged build)" if r.get("maps_off_the_road") else ""))
    if r.get("maps_on_the_road"):
        L.append(f"  sources seen before choosing     {r['sources_seen']} for {r['sources_taken']} taken; cold reads {r['cold_reads']} of {r['maps_on_the_road']}; "
                 f"defaults put back {r['put_back']}; registered steps {len(r['registered_steps'])}")
    return L


def tracker_text(r: dict) -> str:
    """One line for a tracker entry."""
    d = r.get("deviations") or {}
    return (f"road {r.get('road_version')}: plugin {_pct(r['inside_share'])} of map work; layers through the door {r['layers_with_record']}/{r['layers']}; "
            f"library entries {len(r['library_entries_used'])}; unexplained deviations {d.get('unexplained', '-')}; outside steps {r['outside_steps']}; "
            f"on the road {r.get('maps_on_the_road', 0)}/{r.get('maps', 0)}")
