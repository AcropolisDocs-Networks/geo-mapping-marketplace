#!/usr/bin/env python3
"""gmap: one road to a map (5.0.0). Every map takes the same steps, and each step hands over the rules for the next.

    gmap.py brief   --region "Tempe, Arizona" | --bbox W,S,E,N [--place NAME]  --message "..."  --intent site-map
                    [--need "cell towers" ...] [--ask "theme=dark | make it dark, like a circuit board" ...]
                    [--outcome explain] [--audience external] [--template square-social] [--title T --subtitle S --tagline G]
                    [--computer linked|none] [--id ID]
    gmap.py sources [--count]                      every source that could serve each need, and what else is available here
    gmap.py choose  NEED ENTRY_ID --why "..."      take one source for a need (the others are recorded as seen and not taken)
    gmap.py need    add|decline THEME [--why "..."]
    gmap.py data    plan | add NEED --file F | file NAME --path F --source S --license L | derive NAME --op OP --in F --out F [--param k=v]
                    | step NAME --script S [--in F ...] --out F [...] [-- ARGS]
    gmap.py compose [--out map.yaml]               the MapSpec from the defaults, the intent, the brief, the asks and the data
    gmap.py set     KEY=VALUE [...] [--layer ID] [--asked "their words"]
    gmap.py draft                                  lint, the small pictures, the readings and the cold-read packet
    gmap.py cold    --answer FILE | --text "..."   what a fresh reader said the map tells, beside the message
    gmap.py build   [--formats svg,pdf,png]        the finished files; unexplained deviations are drawn at their default and said
    gmap.py review                                 how the map was made, in numbers (the road rubric)
    gmap.py status | rules STEP | intents | usage

A job is one map. Its record is `<work>/.geo-mapping/jobs/<id>/job.json`: the brief, the asks, the sources seen and taken for each need,
every data item with how it came in (a library entry, a data tool, an op, a registered step, the person's file), the drafts, the builds.
The MapSpec stays the build's input and can be edited by hand; the build compares it with the standing defaults (data/policy.yaml) either
way. A build run by gmap enforces them: a default changed with no ask and no intent is drawn at its default and said in one line.

Nothing here fetches from the network except `sources --count` (one count request for a service entry, through the plugin's own door) and
the build's plan step. Exit 0 done; 2 a request it cannot carry out as given; 3 the build was blocked; 4 stopped for the person
(a source needs their computer, or a need has no source).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "lints"))

import gmap_common as gc  # noqa: E402
import policy  # noqa: E402

STEPS_FILE = ROOT / "data" / "steps.yaml"
EXIT_PERSON = gc.EXIT_REFUSED
CLOUD_PLACES = ("cloud",)
GEO_LIBS = ("geopandas", "shapely", "pyproj", "rasterio", "fiona", "pyogrio", "scipy", "numpy", "pandas", "h3", "duckdb", "laspy", "pyarrow", "networkx", "sklearn")
PICTURE_EXT = (".png", ".svg", ".pdf", ".html")


class JobError(Exception):
    def __init__(self, msg: str, code: int = gc.EXIT_USAGE):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------------------------------------------------------- the job record
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def slug(text: str, n: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:n].rstrip("-")) or "map"


def jobs_root(create: bool = False) -> Path:
    p = gc.workdir(create=create) / "jobs"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def job_dir(jid: str) -> Path:
    return jobs_root() / jid


def current_id() -> str | None:
    env = os.environ.get("GMAP_JOB")
    if env:
        return env
    p = jobs_root() / "current.txt"
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def load(jid: str | None = None) -> dict:
    jid = jid or current_id()
    if not jid:
        raise JobError("no map job is open here: start one with gmap.py brief (gmap.py rules brief says what it needs)")
    p = job_dir(jid) / "job.json"
    if not p.is_file():
        raise JobError(f"no job '{jid}' in {jobs_root()}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(job: dict) -> None:
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    job["updated"] = now()
    (d / "job.json").write_text(json.dumps(job, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    (jobs_root(create=True) / "current.txt").write_text(job["id"], encoding="utf-8")


def log(job: dict, step: str, what: str, **more) -> None:
    job.setdefault("log", []).append({"at": now(), "step": step, "what": what, **more})
    order = steps_doc().get("order") or []
    if step in order and order.index(step) >= order.index(job.get("step", order[0])) if job.get("step") in order else True:
        job["step"] = step


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def steps_doc() -> dict:
    import yaml
    try:
        return yaml.safe_load(STEPS_FILE.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def rules_lines(step: str, limit: int = 40) -> list[str]:
    """A step's rules in at most `limit` lines: what it is for, the command, the rules, the pages to read."""
    st = (steps_doc().get("steps") or {}).get(step)
    if not st:
        return [f"no step '{step}' (the steps: {', '.join(steps_doc().get('order') or [])})"]
    L = [f"Step: {step}. {st.get('says')}", f"  {st.get('command')}"]
    L += [f"  - {r}" for r in st.get("rules") or []]
    if st.get("read"):
        L.append("  read when a rule names it: " + ", ".join(st["read"]) + '   (python3 "$GMAP_ROOT/scripts/modules.py" read <page>)')
    return L[:limit]


def next_lines(step: str) -> list[str]:
    return ["", "Next"] + rules_lines(step) + [f'  (gmap.py is: python3 "{HERE / "gmap.py"}")']


def out(lines) -> None:
    print("\n".join(str(x) for x in lines))


# ---------------------------------------------------------------------------------------------------------------- where the session runs
def where(computer: str | None = None) -> dict:
    """The place this process runs and what it reaches (data/execution-routes.yaml), in words for the brief."""
    try:
        import env_probe
        pl = env_probe.place()
    except Exception:  # noqa: BLE001
        pl = "unknown"
    computer = (computer or os.environ.get("GMAP_COMPUTER") or "unknown").lower()
    cloud = pl in CLOUD_PLACES
    cannot = []
    if cloud:
        cannot = ["OpenStreetMap by query (Overpass, ohsome)", "any source that needs one of the person's keys (NASA Earthdata, OpenCelliD, openrouteservice, a tile provider)",
                  "the person's own files, until they are attached or their folder is connected"]
    line = {"cloud": "the cloud workspace", "cowork-vm": "the session's own machine beside the connected folders"}.get(pl, "the person's own computer" if pl.startswith("host") else pl)
    return {"place": pl, "says": line, "computer": computer, "cloud": cloud, "cannot_reach": cannot}


def where_lines(w: dict) -> list[str]:
    L = [f"Runs in: {w['says']}" + {"linked": "; the person's computer is linked (its data tools hold the keys)",
                                    "none": "; no computer is linked"}.get(w["computer"], "; whether a computer is linked was not said (--computer linked|none)")]
    if w["cannot_reach"]:
        L.append("  not reachable from here: " + "; ".join(w["cannot_reach"]))
        if w["computer"] != "linked":
            L.append("  a source that needs them stops the job with a request to link the computer; it is never swapped for another silently")
    return L


# ---------------------------------------------------------------------------------------------------------------- brief
def parse_ask(text: str) -> dict:
    """'theme=dark | make it dark, like a circuit board' -> {set, value, words}."""
    if "|" not in text or "=" not in text.split("|", 1)[0]:
        raise JobError(f"an ask is written KEY=VALUE | the person's words (got: {text[:60]})")
    left, words = text.split("|", 1)
    key, value = left.split("=", 1)
    words = words.strip().strip('"').strip("'").strip()
    if len(words) < 2:
        raise JobError("an ask needs the person's own words after the bar")
    return {"set": key.strip(), "value": _value(value.strip()), "words": words}


def _value(v: str):
    low = v.lower()
    if low in ("true", "on", "yes"):
        return True
    if low in ("false", "off", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v.startswith("[") or v.startswith("{"):
        try:
            import yaml
            return yaml.safe_load(v)
        except Exception:  # noqa: BLE001
            return v
    return v.strip('"').strip("'")


def _library_term(*codes) -> str | None:
    """The first of these codes the library's geography vocabulary knows (us-az, then us), as its own term."""
    try:
        import library_lint as ll
        import ref_library as rl
        tags, _ = ll.load_vocab(rl.shipped_lib())
    except Exception:  # noqa: BLE001
        return next((str(c).lower() for c in codes if c), None)
    for c in codes:
        t = ll.resolve_tag(tags, "geography", str(c).lower()) if c else None
        if t:
            return t
    return None


def geography_of(brief: dict) -> str | None:
    """The library's geography term for the brief's place (us-az, us, de): the admin code named, the state or province under a small box's
    centre, the state or country a place name ends with; else the country. None lists every source of a theme."""
    reg = brief.get("region") if isinstance(brief.get("region"), dict) else {}
    try:
        if reg.get("admin"):
            code = str(reg["admin"][0] if isinstance(reg["admin"], list) else reg["admin"])
            return _library_term(code, code.split("-")[0])
        if brief.get("bbox"):
            import geo_locale as gl
            x0, y0, x1, y1 = brief["bbox"]
            a1 = gl.admin1_at((x0 + x1) / 2, (y0 + y1) / 2) or {}
            code = str(a1.get("iso_3166_2") or "") if (x1 - x0) < 8 else ""      # a box inside one state or province takes its sources too
            return _library_term(code, gl.country_at((x0 + x1) / 2, (y0 + y1) / 2))
    except Exception:  # noqa: BLE001
        pass
    text = str(reg.get("place") or "")
    try:
        import region as region_mod
        tail = text.split(",")[-1].strip()
        sc = region_mod.us_state_code(tail) if tail else None
        code = ("US-" + sc) if sc else (region_mod.admin_by_name(tail) if tail else None)
        if code:
            return _library_term(code, code.split("-")[0])
    except Exception:  # noqa: BLE001 - a name two areas share, or no such area
        pass
    try:
        import region as region_mod
        r = region_mod.resolve(reg)
        return _library_term(*(list(r.admin1 or [])[:1] + list(r.countries or [])[:1]))
    except Exception:  # noqa: BLE001 - the sheet then lists every source of the theme
        return None


def cmd_brief(a) -> int:
    import themes
    if not a.message or len(a.message.split()) < 4:
        raise JobError('a brief needs --message "<one full sentence: what should the reader take from this map?>"')
    idoc = policy.intent_doc(a.intent)
    if not idoc:
        raise JobError(f"--intent is one of: {', '.join(d.get('intent') for d in policy.intents())} (gmap.py intents)")
    if not (a.region or a.bbox):
        raise JobError('a brief needs the place: --region "<name or admin codes>" or --bbox W,S,E,N')
    brief: dict = {"message": a.message.strip(), "intent": a.intent, "outcome": a.outcome or idoc.get("outcome"), "audience": a.audience, "template": a.template}
    if a.bbox:
        try:
            brief["bbox"] = [float(v) for v in a.bbox.replace(";", ",").split(",")]
            assert len(brief["bbox"]) == 4
        except (ValueError, AssertionError):
            raise JobError("--bbox is W,S,E,N in degrees") from None
        if a.place:
            brief["place_name"] = a.place
    if a.region:
        import recipe
        brief["region"] = recipe.region_block(a.region)
    supplied = [k for k in ("title", "subtitle", "tagline") if getattr(a, k)]
    for k in supplied:
        brief[k] = getattr(a, k)
    brief["supplied"] = supplied
    if a.style_pack:
        brief["style_pack"] = a.style_pack
    asks = [parse_ask(x) for x in a.ask or []]
    tdoc = themes.load()
    needs, unknown = [], []
    for n in list(a.need or []) + [t for t in idoc.get("needs") or [] if not a.no_usual]:
        name = n if n in (tdoc.get("themes") or {}) else (themes.find(n, tdoc)[:1] or [(None, 0)])[0][0]
        if not name:
            unknown.append(n)
        elif name not in [x["theme"] for x in needs]:
            needs.append({"theme": name, "asked_as": n, "status": "open"})
    jid = slug(a.id or brief.get("title") or brief["message"])
    if (job_dir(jid) / "job.json").is_file() and not a.replace:
        raise JobError(f"a job '{jid}' is already here: gmap.py status shows it; --replace starts it again, --id names another")
    w = where(a.computer)
    job = {"id": jid, "created": now(), "plugin_version": _version(), "step": "brief", "where": w, "brief": brief, "asks": asks, "needs": needs,
           "unknown_needs": unknown, "files": [{"path": f} for f in a.file or []], "data": [], "drafts": [], "builds": [], "enrich": [], "log": []}
    job["geography"] = geography_of(brief)
    log(job, "brief", "the brief was written")
    save(job)
    L = [f"Job {jid}: {idoc.get('says')}", f"  message: {brief['message']}", f"  for: {brief['outcome']} ({brief['audience']} audience) on {brief['template']}"]
    if idoc.get("sets"):
        L.append("  the intent sets: " + ", ".join(f"{k}={policy._show(v)}" for k, v in idoc["sets"].items()))
    if idoc.get("note"):
        L.append(f"  note: {idoc['note']}")
    L += [f"  asked: {x['set']}={policy._show(x['value'])}  (\"{x['words']}\")" for x in asks] or ["  asked: nothing that departs from a default"]
    L.append("  needs: " + (", ".join(x["theme"] for x in needs) or "none named (the person's own data, or name one with --need)"))
    if unknown:
        L.append("  no theme for: " + ", ".join(unknown) + " (gmap.py sources lists the themes; geo-find-data looks further)")
    L += where_lines(w)
    out(L + next_lines("sources"))
    return gc.EXIT_OK


# ---------------------------------------------------------------------------------------------------------------- sources
def window_of(job: dict):
    b = job["brief"].get("bbox")
    if b:
        return b
    try:
        import region as region_mod
        return list(region_mod.resolve(job["brief"]["region"]).bounds)
    except Exception:  # noqa: BLE001
        return None


def count_in_window(entry_id: str, bbox) -> int | None:
    """One count request for a service entry with a shipped profile (the plugin's own door); None when it cannot be asked from here."""
    if os.environ.get("GMAP_OFFLINE") == "1" or not bbox:
        return None
    try:
        import ref_library as rl
        import service_tools as st
        e = next((x for x in rl.all_entries("all") if x.get("id") == entry_id), None)
        dl = (e or {}).get("delivery") or {}
        if dl.get("mode") != "service" or not dl.get("profile") or not dl.get("layer"):
            return None
        r = st.hits(profile=str(dl["profile"]), layer=str(dl["layer"]), protocol=str(dl.get("protocol") or "arcgis"), bbox=",".join(str(v) for v in bbox))
        return int(r["count"]) if r.get("ok") and r.get("count") is not None else None
    except Exception:  # noqa: BLE001 - a count is a help, never a reason to stop
        return None


def cmd_sources(a) -> int:
    import themes
    job = load(a.job)
    tdoc = themes.load()
    geo = job.get("geography")
    aud = job["brief"].get("audience")
    bbox = window_of(job) if a.count else None
    L = [f"Sources for job {job['id']}" + (f" (library geography {geo})" if geo else " (no geography found for the place: every source of each theme is listed)")]
    for n in job["needs"]:
        s = themes.show(n["theme"], geo, aud, tdoc)
        if a.count and s.get("compare"):
            for c in s["candidates"]:
                c["count_in_window"] = count_in_window(c["id"], bbox)
        n["candidates"] = s["candidates"]
        n["compare"] = s["compare"]
        L += ["", f"Need: {n['theme']}  ({s['says']})" + (f"  taken: {n['chosen']}" if n.get("chosen") else "")]
        if s.get("note"):
            L.append(f"  note: {s['note']}")
        if s["compare"]:
            L.append("  compare before choosing: count each in the map's window" + ("" if a.count else " (gmap.py sources --count asks the service entries)"))
        for i, c in enumerate(s["candidates"], 1):
            needs_pc = bool(c.get("needs_computer"))
            flag = ("" if c["audience_ok"] else f"  NOT FOR THIS AUDIENCE: {c['audience_note']}") + \
                   ("  NEEDS THE COMPUTER" if needs_pc and job["where"]["cloud"] and job["where"]["computer"] != "linked" else "")
            cnt = f"  count here: {c['count_in_window']:,}" if c.get("count_in_window") is not None else ""
            L.append(f"  {i}. {c['id']}  [{c['license']}, {c['delivery']}" + (f", key {c['key']}" if c["key"] else "") + f"]  from: {c['fetch_from']}{cnt}{flag}")
            L.append(f"     {c['title']}")
        if not s["candidates"]:
            L.append("  the library has no source for this here: geo-find-data looks in the catalogues; or ask the person for a file")
        if s.get("gap"):
            L.append(f"  not in the library yet: {s['gap']}")
    idoc = policy.intent_doc(job["brief"]["intent"])
    have = {n["theme"] for n in job["needs"]} | {e["theme"] for e in job.get("enrich") or [] if e.get("answer")}
    more = [t for t in idoc.get("enrich") or [] if t not in have][:5]
    if more:
        L += ["", "Also available here (accept or decline each; declining is fine, silence is not)"]
        for t in more:
            s = themes.show(t, geo, aud, tdoc)
            if not any(e["theme"] == t for e in job["enrich"]):
                job["enrich"].append({"theme": t, "answer": None, "sources": len(s["candidates"])})
            L.append(f"  {t}: {s['says']} ({len(s['candidates'])} source(s) here)   gmap.py need add {t}  |  gmap.py need decline {t} --why \"...\"")
    if job.get("files"):
        L += ["", "The person's files: " + ", ".join(f["path"] for f in job["files"]) + "  (gmap.py data file NAME --path F --source ... --license ...)"]
    log(job, "sources", "the source sheet was made", counted=bool(a.count))
    save(job)
    out(L + ["", 'Take one for each need: gmap.py choose <need> <entry id> --why "<one phrase>"'] + next_lines("data"))
    return gc.EXIT_OK


def _need(job: dict, name: str) -> dict:
    for n in job["needs"]:
        if n["theme"] == name or n.get("asked_as") == name:
            return n
    raise JobError(f"no need '{name}' in this job (needs: {', '.join(n['theme'] for n in job['needs']) or 'none'})")


def cmd_choose(a) -> int:
    job = load(a.job)
    n = _need(job, a.need)
    cands = n.get("candidates")
    if cands is None:
        raise JobError("run gmap.py sources first: a source is taken from the sheet, with the others in view")
    c = next((x for x in cands if x["id"] == a.entry or x["id"] == "ref." + a.entry), None)
    if c is None:
        raise JobError(f"'{a.entry}' is not on the sheet for {n['theme']} (the sheet: {', '.join(x['id'] for x in cands) or 'empty'})")
    if not c["audience_ok"]:
        raise JobError(f"{c['id']} is not for this map's audience: {c['audience_note']}", EXIT_PERSON)
    if not a.why or len(a.why.strip()) < 3:
        raise JobError('say why in one phrase: --why "the fullest count in the window"')
    n.update(chosen=c["id"], why=a.why.strip(), status="chosen", not_taken=[x["id"] for x in cands if x["id"] != c["id"]], needs_computer=bool(c.get("needs_computer")))
    log(job, "sources", f"{n['theme']}: took {c['id']}", why=a.why.strip(), seen=len(cands))
    save(job)
    L = [f"{n['theme']}: {c['id']} taken ({a.why.strip()}); {len(cands) - 1} other source(s) seen and not taken"]
    if n["needs_computer"] and job["where"]["cloud"] and job["where"]["computer"] != "linked":
        L += ["  STOP: this source is fetched on the person's computer and none is linked. Ask the person to link it (or to choose another source).",
              "  Do not swap it for another yourself: gmap.py choose again only on their word."]
        out(L)
        return EXIT_PERSON
    open_ = [x["theme"] for x in job["needs"] if x.get("status") != "chosen"]
    out(L + ([f"  still to choose: {', '.join(open_)}"] if open_ else next_lines("data")))
    return gc.EXIT_OK


def cmd_need(a) -> int:
    import themes
    job = load(a.job)
    tdoc = themes.load()
    name = a.theme if a.theme in (tdoc.get("themes") or {}) else (themes.find(a.theme, tdoc)[:1] or [(None, 0)])[0][0]
    if not name:
        raise JobError(f"no theme '{a.theme}' (python3 scripts/themes.py list)")
    e = next((x for x in job["enrich"] if x["theme"] == name), None)
    if a.op == "add":
        if not any(n["theme"] == name for n in job["needs"]):
            job["needs"].append({"theme": name, "asked_as": a.theme, "status": "open"})
        if e:
            e["answer"] = "accepted"
        log(job, "sources", f"need added: {name}")
        save(job)
        out([f"need added: {name}. gmap.py sources lists its sources."])
        return gc.EXIT_OK
    if not a.why:
        raise JobError('declining needs one phrase: --why "the map is about the towers only"')
    if e is None:
        e = {"theme": name, "answer": None}
        job["enrich"].append(e)
    e.update(answer="declined", why=a.why.strip())
    log(job, "sources", f"declined: {name}", why=a.why.strip())
    save(job)
    out([f"declined: {name} ({a.why.strip()})"])
    return gc.EXIT_OK


# ---------------------------------------------------------------------------------------------------------------- data
TOOL_FOR = (("overpass", "osm_fetch"), ("ohsome", "osm_history"), ("opencellid", "coverage_fetch (adapter=opencellid)"), ("earthdata", "earthdata_search then earthdata_fetch"),
            ("black-marble", "earthdata_nightlights"), ("fcc-bdc", "fcc_bdc_coverage"), ("acs5", "census_query"), ("router", "isochrone"))


def _entry(eid: str) -> dict | None:
    import ref_library as rl
    return next((x for x in rl.all_entries("all") if x.get("id") == eid), None)


def _license_text(e: dict) -> str:
    lic = e.get("license") or {}
    return str(lic.get("name") or lic.get("class") or "see the source")


def _kind_of_entry(e: dict) -> str | None:
    dl = e.get("delivery") or {}
    k = str(dl.get("layer_kind") or dl.get("geometry") or "").lower()
    for word, kind in (("point", "point"), ("line", "line"), ("polygon", "polygon"), ("raster", "raster"), ("grid", "raster")):
        if word in k:
            return kind
    lv = " ".join(str(x) for x in ((e.get("tags") or {}).get("level") or []))
    if "site" in lv:
        return "point"
    t = str(e.get("id"))
    return "raster" if ".terrain." in t else "polygon" if ".boundary" in t else "line" if (".road" in t or ".rail" in t) else None


def _kind_of_file(path: Path, layer: str | None = None) -> str | None:
    if path.suffix.lower() in (".tif", ".tiff", ".vrt"):
        return "raster"
    try:
        import pyogrio
        info = pyogrio.read_info(str(path), layer=layer) if layer else pyogrio.read_info(str(path))
        g = str(info.get("geometry_type") or "").lower()
        return "point" if "point" in g else "line" if "line" in g else "polygon" if "polygon" in g else None
    except Exception:  # noqa: BLE001
        return None


def relief_source(entry_id: str | None) -> str | None:
    """The builder's own name for an elevation entry (terrain.source), when the entry is one the relief fetches itself."""
    if not entry_id:
        return None
    try:
        import dem_sources as ds
        import terrain_fetch as tf
        table = {tf.THREEDEP_ENTRY: "3dep", tf.COP_ENTRY: "copernicus", tf.TERRARIUM_ENTRY: "terrarium", **{v["entry"]: k for k, v in ds.INFO.items()}}
    except Exception:  # noqa: BLE001
        table = {}
    return table.get(entry_id)


def data_plan(job: dict) -> list[dict]:
    """How each chosen source comes in: by the build itself (a `ref` layer), by a data tool on the person's computer, or as a file to bring."""
    plan = []
    for n in job["needs"]:
        if n.get("status") != "chosen":
            plan.append({"need": n["theme"], "how": "open", "says": "no source taken yet (gmap.py choose)"})
            continue
        e = _entry(n["chosen"]) or {}
        mode = str((e.get("delivery") or {}).get("mode") or "-")
        got = next((d for d in job["data"] if d.get("need") == n["theme"]), None)
        if got:
            plan.append({"need": n["theme"], "how": "done", "says": f"in hand as '{got['name']}' ({got['door']})"})
        elif relief_source(n["chosen"]) and not (n.get("needs_computer") and job["where"]["cloud"] and job["where"]["computer"] != "linked"):
            plan.append({"need": n["theme"], "how": "relief", "says": f"the ground's shape, fetched by the build for the map's window (terrain: {{on: true, source: {relief_source(n['chosen'])}}}); "
                         "for a profile, a line of sight or a viewshed, the terrain tools fetch the same model (terrain.py)"})
        elif n.get("needs_computer"):
            tool = next((t for w, t in TOOL_FOR if w in n["chosen"]), "ref_fetch")
            linked = job["where"]["computer"] == "linked" or not job["where"]["cloud"]
            plan.append({"need": n["theme"], "how": "computer", "tool": tool, "blocked": not linked,
                         "says": (f"fetch on the person's computer with the data tool {tool} (work_folder = the connected folder), then: gmap.py data add {n['theme']} --file <the file it wrote>"
                                  if linked else f"needs the person's computer (data tool {tool}) and none is linked: ask the person to link it")})
        elif mode == "user-supplied":
            plan.append({"need": n["theme"], "how": "bring", "says": f"a file the person brings ({(e.get('delivery') or {}).get('format') or 'see the entry'}), then: gmap.py data add {n['theme']} --file <file>"})
        else:
            plan.append({"need": n["theme"], "how": "ref", "says": f"drawn as a library layer: the build fetches it once for the map's window and keeps it (source: {{type: ref, id: {n['chosen']}}})"})
    return plan


def cmd_data(a) -> int:
    job = load(a.job)
    if a.op == "plan":
        plan = data_plan(job)
        job["data_plan"] = plan
        log(job, "data", "the data plan was made")
        save(job)
        L = [f"Data for job {job['id']}"] + [f"  {p['need']}: {p['says']}" for p in plan]
        for d in job["data"]:
            L.append(f"  in hand: {d['name']} ({d['door']}) {d.get('path') or d.get('ref') or ''}")
        if any(p.get("blocked") for p in plan):
            out(L + ["", "STOP: a source needs the person's computer. Ask them to link it; do not replace the source yourself."])
            return EXIT_PERSON
        out(L + next_lines("compose"))
        return gc.EXIT_OK
    name = slug(a.name, 32)
    if a.op == "add":
        n = _need(job, a.name)
        p = Path(a.file).expanduser()
        if not p.is_file():
            raise JobError(f"{a.file} is not a file here")
        e = _entry(n["chosen"]) if n.get("chosen") else None
        if e is None:
            raise JobError(f"no source was taken for {n['theme']} (gmap.py choose): a fetched file is recorded against its library entry")
        item = {"name": slug(n["theme"], 32), "need": n["theme"], "door": "data tool" if a.tool or n.get("needs_computer") else "library fetch", "tool": a.tool, "ref": n["chosen"],
                "path": str(p.resolve()), "layer": a.layer, "sha256": sha256(p), "bytes": p.stat().st_size, "kind": _kind_of_file(p, a.layer) or _kind_of_entry(e),
                "provenance": {"source": f"{(e.get('source') or {}).get('publisher') or ''}: {e.get('title')}".strip(": "), "license": _license_text(e),
                               "retrieved_at": dt.date.today().isoformat()}, "at": now()}
    elif a.op == "file":
        p = Path(a.path).expanduser()
        if not p.is_file():
            raise JobError(f"{a.path} is not a file here")
        if not (a.source and a.license):
            raise JobError("the person's file needs --source and --license, as they gave them (ask; never invent them)", EXIT_PERSON)
        item = {"name": name, "door": "the person's file", "path": str(p.resolve()), "layer": a.layer, "sha256": sha256(p), "bytes": p.stat().st_size,
                "kind": _kind_of_file(p, a.layer), "provenance": {"source": a.source, "license": a.license, "retrieved_at": dt.date.today().isoformat()}, "at": now()}
    elif a.op == "derive":
        import geo_analyze
        params = {}
        for kv in a.param or []:
            if "=" not in kv:
                raise JobError(f"--param is key=value (got {kv})")
            k, v = kv.split("=", 1)
            params[k] = _value(v)
        try:
            rep = geo_analyze.analyze(a.do, a.src, a.out, other=a.other, layer=a.layer, other_layer=a.other_layer, params=params, overwrite=True, strict=not a.no_strict)
        except Exception as ex:  # noqa: BLE001 - the op's own message is the answer
            raise JobError(f"the op {a.do} did not run: {str(ex).splitlines()[0][:300]}", getattr(ex, "code", gc.EXIT_USAGE)) from None
        parents = [d for d in job["data"] if d.get("path") and Path(d["path"]).resolve() in (Path(a.src).resolve(), Path(a.other).resolve() if a.other else None)]
        lic = "; ".join(sorted({d["provenance"]["license"] for d in parents})) or "as its sources"
        src = "Derived (" + a.do + ") from " + (" and ".join(d["provenance"]["source"] for d in parents) or Path(a.src).name)
        p = Path(a.out)
        item = {"name": name, "door": "op", "op": a.do, "params": params, "inputs": [str(Path(a.src).resolve())] + ([str(Path(a.other).resolve())] if a.other else []),
                "path": str(p.resolve()), "sha256": sha256(p), "bytes": p.stat().st_size if p.is_file() else 0, "kind": _kind_of_file(p),
                "provenance": {"source": src, "license": lic, "retrieved_at": dt.date.today().isoformat()}, "report": {k: rep.get(k) for k in ("features_in", "features_out", "notes") if k in rep},
                "at": now()}
    elif a.op == "step":
        sp = Path(a.script).expanduser()
        if not sp.is_file():
            raise JobError(f"{a.script} is not a file here")
        ins = [Path(x) for x in a.src_many or []]
        outs = [Path(x) for x in a.out_many or []]
        if not outs:
            raise JobError("a registered step names what it writes: --out FILE [FILE ...]")
        body = sp.read_text(encoding="utf-8", errors="replace")
        libs = sorted({m for m in re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", body, re.M) if m in GEO_LIBS})
        before = {str(p): sha256(p) for p in ins}
        rc = subprocess.run([sys.executable, str(sp), *(a.rest or [])], capture_output=True, text=True)
        tail = (rc.stdout or "")[-1200:] + (("\n" + rc.stderr[-1200:]) if rc.returncode else "")
        if rc.returncode != 0:
            print(tail)
            raise JobError(f"the step's script ended with exit {rc.returncode}; nothing was registered", gc.EXIT_RUNTIME)
        missing = [str(p) for p in outs if not p.is_file()]
        if missing:
            raise JobError(f"the step did not write {', '.join(missing)}; nothing was registered")
        parents = [d for d in job["data"] if d.get("path") and Path(d["path"]).resolve() in [p.resolve() for p in ins]]
        lic = "; ".join(sorted({d["provenance"]["license"] for d in parents})) or (a.license or "as its sources")
        src = a.source or ("Derived (" + name + ") from " + (" and ".join(d["provenance"]["source"] for d in parents) or ", ".join(p.name for p in ins) or "the session's own script"))
        items = []
        for k, p in enumerate(outs):
            items.append({"name": name if len(outs) == 1 else f"{name}-{k + 1}", "door": "registered step", "script": sp.name, "script_sha256": sha256(sp), "libraries": libs,
                          "inputs": [{"path": str(q.resolve()), "sha256": before[str(q)]} for q in ins], "path": str(p.resolve()), "sha256": sha256(p), "bytes": p.stat().st_size,
                          "kind": _kind_of_file(p), "provenance": {"source": src, "license": lic, "retrieved_at": dt.date.today().isoformat()}, "candidate_op": True, "at": now()})
        job["data"] = [d for d in job["data"] if d["name"] not in {i["name"] for i in items}] + items
        log(job, "data", f"registered step {name}", libraries=libs, outputs=len(outs))
        save(job)
        out([f"registered step '{name}': {sp.name} ({', '.join(libs) or 'no geodata library'}) wrote {len(outs)} file(s); listed as a candidate op",
             "  " + (tail.strip().splitlines()[-1][:200] if tail.strip() else "")] + next_lines("compose"))
        return gc.EXIT_OK
    else:
        raise JobError("data: plan | add | file | derive | step")
    job["data"] = [d for d in job["data"] if d["name"] != item["name"]] + [item]
    log(job, "data", f"{item['door']}: {item['name']}")
    save(job)
    out([f"in hand: {item['name']} ({item['door']}), {item.get('kind') or 'kind not read'}, {item.get('bytes', 0):,} bytes"] + next_lines("compose"))
    return gc.EXIT_OK


# ---------------------------------------------------------------------------------------------------------------- compose and set
ROLE_OF = {"point": "points", "line": "lines", "polygon": "polygons", "raster": "raster"}


def compose_spec(job: dict, spec_dir: Path) -> dict:
    b = job["brief"]
    idoc = policy.intent_doc(b["intent"])
    spec: dict = {"mapspec_version": 1, "id": job["id"], "template": b["template"], "intent": b["intent"], "outcome": b["outcome"], "audience": b["audience"], "message": b["message"]}
    if b.get("region"):
        spec["region"] = b["region"]
    if b.get("bbox"):
        spec["extent"] = {"mode": "bbox", "bbox": b["bbox"]}
        if b.get("place_name"):
            spec["extent"]["place"] = b["place_name"]
            spec["extent"].pop("place")                    # the name is for the title; the box frames the map
    if b.get("style_pack"):
        spec["style"] = {"pack": b["style_pack"]}
    title: dict = {"on": True}
    for k, key in (("title", "text"), ("subtitle", "subtitle"), ("tagline", "tagline")):
        if b.get(k):
            title[key] = b[k]
    if b.get("supplied"):
        sup = [x for x in b["supplied"] if x in ("title", "subtitle")]
        if sup:
            title["supplied"] = True if set(sup) == {"title", "subtitle"} else sup
    spec["elements"] = {"title": title, "legend": {"on": True}}
    asks = []
    for x in job.get("asks") or []:
        policy._set(spec, x["set"], x["value"])
        if x["set"].startswith("elements.") and x["set"].count(".") >= 2:
            el = spec["elements"].setdefault(x["set"].split(".")[1], {})
            el.setdefault("on", True)
        asks.append({"set": x["set"], "words": x["words"]})
    if asks:
        spec["asks"] = asks
    if (idoc.get("sets") or {}).get("elements.legend.on") is False and "elements.legend.on" not in [x["set"] for x in asks]:
        spec["elements"].pop("legend", None)             # the intent turns it off; nothing is written and the intent's value applies
    layers = []
    roles = idoc.get("roles") or {}
    used = set()
    for n in job["needs"]:
        if n.get("status") != "chosen":
            continue
        d = next((x for x in job["data"] if x.get("need") == n["theme"]), None)
        rs_name = relief_source(n["chosen"])
        if rs_name or (d and d.get("kind") == "raster" and n["theme"] == "elevation"):      # an elevation model is the map's relief, not a layer
            policy._set(spec, "terrain.on", True)
            policy._set(spec, "terrain.source", d["path"] if d else rs_name)
            if d:
                used.add(d["name"])
            continue
        if d is None and n.get("needs_computer"):
            continue
        e = _entry(n["chosen"]) or {}
        kind = (d or {}).get("kind") or _kind_of_entry(e)
        lid = slug(n["theme"], 24).replace("-", "_")
        ly = {"id": lid, "source": _source_of(d, spec_dir) if d else {"type": "ref", "id": n["chosen"]}, "role": roles.get(kind) or ROLE_OF.get(kind) or "points"}
        if d:
            ly["provenance"] = d["provenance"]
            used.add(d["name"])
        layers.append(ly)
    for d in job["data"]:
        if d["name"] in used or d.get("draw") is False:
            continue
        if any(d["path"] in (x.get("inputs") or []) or any(isinstance(i, dict) and i.get("path") == d["path"] for i in x.get("inputs") or []) for x in job["data"] if x is not d):
            continue                                        # an input of a later step: the result is drawn, not the input
        kind = d.get("kind")
        layers.append({"id": d["name"].replace("-", "_"), "source": _source_of(d, spec_dir), "role": roles.get(kind) or ROLE_OF.get(kind) or "polygons", "provenance": d["provenance"]})
    spec["layers"] = layers
    return spec


def _source_of(d: dict, spec_dir: Path) -> dict:
    p = Path(d["path"])
    try:
        rel = os.path.relpath(p, spec_dir)
    except ValueError:
        rel = str(p)
    s = {"type": "file", "path": rel if not rel.startswith("..") else str(p)}
    if d.get("layer"):
        s["layer"] = d["layer"]
    return s


def write_spec(path: Path, spec: dict) -> None:
    import yaml
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=160), encoding="utf-8")


def cmd_compose(a) -> int:
    job = load(a.job)
    open_ = [n["theme"] for n in job["needs"] if n.get("status") != "chosen"]
    if open_:
        raise JobError(f"no source is taken for: {', '.join(open_)} (gmap.py sources, then gmap.py choose)", EXIT_PERSON)
    waiting = [e["theme"] for e in job.get("enrich") or [] if not e.get("answer")]
    if waiting:
        raise JobError(f"answer each 'also available here' line first: {', '.join(waiting)} (gmap.py need add <theme> | gmap.py need decline <theme> --why ...)")
    path = Path(a.out or job.get("spec") or "map.yaml")
    if path.exists() and not a.replace and job.get("spec") != str(path.resolve()):
        raise JobError(f"{path} is already here and is not this job's MapSpec (--out names another file, --replace writes over it)")
    spec = compose_spec(job, path.resolve().parent)
    if path.is_file() and job.get("spec") == str(path.resolve()):      # composing again keeps the design choices already made (gmap.py set)
        try:
            old = policy._norm_keys(gc.load_mapspec(path))
            old_layers = {ly.get("id"): ly for ly in old.get("layers") or [] if isinstance(ly, dict)}
            spec["layers"] = [{**old_layers.get(ly["id"], {}), **{k: v for k, v in ly.items() if k in ("id", "source", "provenance") or ly["id"] not in old_layers}} for ly in spec["layers"]]
            for k, v in old.items():
                if k not in spec and k not in ("layers", "asks", "terrain"):
                    spec[k] = v
            for el, v in (old.get("elements") or {}).items():
                if el not in spec["elements"]:
                    spec["elements"][el] = v
        except Exception:  # noqa: BLE001 - an unreadable old MapSpec is written over
            pass
    if not spec["layers"]:
        raise JobError("the job has no data yet: gmap.py data plan says how each source comes in; gmap.py data file brings the person's own", EXIT_PERSON)
    write_spec(path, spec)
    job["spec"] = str(path.resolve())
    log(job, "compose", f"the MapSpec was written: {path.name}", layers=len(spec["layers"]))
    save(job)
    dev = policy.deviations(policy._norm_keys(spec))
    L = [f"MapSpec written: {path}  ({len(spec['layers'])} layer(s): " + ", ".join(f"{ly['id']} as {ly['role']}" for ly in spec["layers"]) + ")", f"  {dev['line']}",
         "  not written, so the builder decides: theme, look, positions, names, context, ground, locator, scale bar, north arrow"]
    out(L + rules_lines("compose") + next_lines("draft"))
    return gc.EXIT_OK


def cmd_set(a) -> int:
    job = load(a.job)
    if not job.get("spec"):
        raise JobError("there is no MapSpec yet: gmap.py compose writes it")
    path = Path(job["spec"])
    spec = policy._norm_keys(gc.load_mapspec(path))
    pol = policy.load()
    by_key = {k: d for d in pol.get("defaults") or [] for k in d["keys"]}
    isets = policy.intent_sets(spec)
    L = []
    target = spec
    if a.layer:
        target = next((ly for ly in spec.get("layers") or [] if ly.get("id") == a.layer), None)
        if target is None:
            raise JobError(f"no layer '{a.layer}' (layers: {', '.join(ly.get('id') for ly in spec.get('layers') or [])})")
    for kv in a.pairs:
        if "=" not in kv:
            raise JobError(f"set takes KEY=VALUE (got {kv})")
        key, raw = kv.split("=", 1)
        key, val = key.strip(), _value(raw.strip())
        d = None if a.layer else by_key.get(key)
        if d is not None and not policy._same(val, d.get("value"), d.get("same") or []) and not (key in isets and policy._same(val, isets[key], [])):
            if not a.asked:
                raise JobError(f"{key} is a standing default ({d['says']}). Change it only on the person's word: gmap.py set {key}={raw} --asked \"<their words>\"", EXIT_PERSON)
            asks = spec.setdefault("asks", [])
            if isinstance(asks, list):
                asks[:] = [x for x in asks if not (isinstance(x, dict) and x.get("set") == key)] + [{"set": key, "words": a.asked.strip()}]
            job["asks"] = [x for x in job.get("asks") or [] if x["set"] != key] + [{"set": key, "value": val, "words": a.asked.strip()}]
            L.append(f"{key} = {policy._show(val)}  (asked: \"{a.asked.strip()}\")")
        else:
            L.append(f"{(a.layer + '.') if a.layer else ''}{key} = {policy._show(val) if not isinstance(val, (dict, list)) else json.dumps(val)}")
        policy._set(target, key, val)
        if not a.layer and key.startswith("elements.") and key.count(".") >= 2:
            spec["elements"].setdefault(key.split(".")[1], {}).setdefault("on", True)
    write_spec(path, spec)
    log(job, "compose", "the MapSpec was changed", keys=[kv.split("=", 1)[0] for kv in a.pairs], asked=bool(a.asked))
    save(job)
    out(L + [f"  {policy.deviations(spec)['line']}"])
    return gc.EXIT_OK


# ---------------------------------------------------------------------------------------------------------------- draft, cold read, build
def _run_build(job: dict, extra: list[str]) -> tuple[int, dict | None, str]:
    """The plugin's own build as a person would run it (token, lint, build), with the standing rules enforced. -> (exit, result, digest)."""
    spec = Path(job["spec"])
    out_dir = spec.parent / "out"
    env = {**os.environ, "GMAP_POLICY": "enforce", "GMAP_JOB": job["id"], "GMAP_DRAFT_SVG": str(job_dir(job["id"]) / "draft.svg")}
    tok = subprocess.run([sys.executable, str(HERE / "env_probe.py"), "--begin-build", "--spec", str(spec), "--value"], capture_output=True, text=True, env=env, cwd=str(spec.parent))
    if tok.stdout.strip():
        env["GMAP_BUILD_TOKEN"] = tok.stdout.strip().splitlines()[-1]
    rc = subprocess.run([sys.executable, str(HERE / "build_map.py"), "--spec", str(spec), "--out", str(out_dir), "--digest", *extra], capture_output=True, text=True, env=env,
                        cwd=str(spec.parent))
    digest = rc.stdout.strip()
    m = re.search(r"^result file: (.+)$", digest, re.M)
    res = None
    if m:
        try:
            res = json.loads(Path(m.group(1) if Path(m.group(1)).is_absolute() else spec.parent / m.group(1)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            res = None
    if res is None and rc.returncode not in (gc.EXIT_OK, gc.EXIT_LINT):
        digest = (digest + "\n" + (rc.stderr or "").strip()[-1500:]).strip()
    return rc.returncode, res, digest


READING_NAMES = {"first_look": "where the eye lands first", "data_share": "the data's share of the picture", "smallest_text": "the smallest text", "contrast": "the weakest text contrast",
                 "clutter": "how busy the picture is", "furniture": "what the title, legend and other elements cover", "edge": "ink at the edge", "thumbnail": "the picture at feed size",
                 "legend": "the legend's colours"}


def _where_in_frame(box) -> str:
    try:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    except Exception:  # noqa: BLE001
        return "?"
    return ("upper" if cy < 33 else "lower" if cy > 67 else "middle") + " " + ("left" if cx < 33 else "right" if cx > 67 else "centre")


def readings(picture: Path, svg: Path | None, off: set) -> list[str]:
    """The draft's measurements as plain lines (the twelve the plugin already makes from a picture and its SVG). They are measurements,
    never grades: no score is given and none is read."""
    import map_evaluate as me
    m, _fs = me.measure(picture, svg if svg and svg.is_file() else None)
    g = lambda k, d=None: (m.get(k) or {}) if d is None else (m.get(k) or {}).get(d)  # noqa: E731
    L = []
    if "first_look" not in off and g("M-01") and g("M-01").get("top_regions"):
        top = g("M-01")["top_regions"][0]
        L.append(f"  the eye lands first in the {_where_in_frame(top.get('box'))} of the picture ({round(100 * float(top.get('share') or 0))}% of the attention there)"
                 + (f"; {round(100 * float(g('M-02', 'attention_on_data')))}% of the attention is on the data, {round(100 * float(g('M-02', 'attention_on_furniture') or 0))}% on the title, legend and other elements"
                    if g("M-02", "attention_on_data") is not None else ""))
    if "data_share" not in off and g("M-02", "data_share_of_weight") is not None:
        L.append(f"  the data carries {round(100 * float(g('M-02', 'data_share_of_weight')))}% of the map's visual weight; the ground and context carry the rest")
    if "smallest_text" not in off and g("M-05", "min_pt") is not None:
        L.append(f"  the smallest text is {g('M-05', 'min_pt')} pt at printed size ({g('M-05', 'below_6pt') or 0} item(s) under 6 pt)")
    if "contrast" not in off and g("M-06", "min_ratio") is not None:
        L.append(f"  the weakest text contrast is {g('M-06', 'min_ratio')}:1 (3:1 is the floor for large text, 4.5:1 for small)")
    if "furniture" not in off and g("M-09", "focus_covered_by_furniture") is not None:
        L.append(f"  the title, legend and other elements cover {round(100 * float(g('M-09', 'focus_covered_by_furniture')), 1)}% of the map's focus (the rule allows 5%)")
    if "clutter" not in off and g("M-03", "congestion_mean") is not None:
        L.append(f"  busyness: {g('M-03', 'congestion_mean')} on average, {g('M-03', 'congestion_p90')} in the busiest tenth of the picture")
    if "edge" not in off and g("M-10"):
        e = g("M-10").get("data_at_frame_edge") or g("M-10").get("edge_ink") or {}
        big = [k for k, v in e.items() if isinstance(v, (int, float)) and v > 0.05]
        L.append("  data reaches the frame's edge on: " + (", ".join(big) if big else "no side"))
    if "thumbnail" not in off and g("M-11", "attention_iou") is not None:
        L.append(f"  at feed size the attention lands in {'the same place' if float(g('M-11', 'attention_iou')) >= 0.5 else 'a different place'} (overlap {g('M-11', 'attention_iou')})")
    if "legend" not in off and g("M-07", "swatches") and g("M-07", "swatches") > 1 and g("M-07", "min_de_deutan") is not None:
        L.append(f"  the closest two legend colours are {g('M-07', 'min_de_normal')} apart, {g('M-07', 'min_de_deutan')} for red-green colour blindness (10 reads as two colours)")
    return L


def cold_packet(job: dict, pictures: list[Path]) -> dict | None:
    """The cold read's prompt for a fresh agent: the pictures and five questions, and nothing about what the map is meant to say."""
    try:
        import shutil

        import map_evaluate as me
        n = len(job.get("drafts") or []) + 1
        od = job_dir(job["id"]) / "cold" / f"draft-{n}"
        (od / "cold").mkdir(parents=True, exist_ok=True)
        (od / "reads").mkdir(parents=True, exist_ok=True)
        frames = []
        for k, p in enumerate(pictures, 1):
            dst = od / "cold" / f"frame-{k}{p.suffix.lower()}"
            shutil.copyfile(p, dst)
            frames.append({"file": dst.name, "label": "the map" if k == 1 else "the same map with its words taken off"})
        prompt = od / "cold-prompt.md"
        prompt.write_text(me.cold_prompt(od, frames), encoding="utf-8")
        return {"prompt": str(prompt), "answer": str(od / "reads" / "cold.json")}
    except Exception:  # noqa: BLE001 - no cold read is made, and the draft says so
        return None


def cmd_draft(a) -> int:
    job = load(a.job)
    if not job.get("spec"):
        raise JobError("there is no MapSpec yet: gmap.py compose writes it")
    code, res, digest = _run_build(job, ["--draft"])
    L = [digest]
    rec = {"at": now(), "exit": code, "blocked": bool((res or {}).get("blocked"))}
    if res is None:
        job["drafts"].append(rec)
        save(job)
        out(L)
        return code or gc.EXIT_RUNTIME
    rec["deviations"] = (res.get("deviations") or {}).get("counts")
    rec["reverted"] = [r["key"] for r in (res.get("deviations") or {}).get("reverted") or []]
    files = [Path(f) for f in res.get("files") or []]
    pic = next((f for f in files if f.name.endswith("_draft.png")), None) or next((f for f in files if f.suffix == ".png" and "_mute" not in f.name), None)
    mute = next((f for f in files if f.name.endswith("_mute.png")), None)
    if pic is None:
        stem = res.get("stem")
        cand = sorted((Path(job["spec"]).parent / "out").glob(f"{stem}*_draft.png")) if stem else []
        pic = cand[-1] if cand else None
        mute = mute or next(iter(sorted((Path(job["spec"]).parent / "out").glob(f"{stem}*_mute.png"))), None) if stem else mute
    if pic is not None and pic.is_file():
        off = policy.applicability(policy._norm_keys(gc.load_mapspec(Path(job["spec"]))))["readings_off"]
        try:
            rl = readings(pic, job_dir(job["id"]) / "draft.svg", off)
        except Exception as ex:  # noqa: BLE001 - readings are a help
            rl = [f"  the readings could not be made ({type(ex).__name__})"]
        L += ["", "Readings (measurements of this draft, not grades)"] + (rl or ["  none could be made from this picture"])
        rec["picture"] = str(pic)
        L += ["", f"Look at: {pic}" + (f"  and  {mute}" if mute else "")]
        if not a.no_cold and not any(d.get("cold_asked") for d in job["drafts"]) or a.cold:
            pk = cold_packet(job, [p for p in (pic, mute) if p is not None and p.is_file()][:1])
            if pk:
                rec["cold_asked"] = True
                rec["cold"] = pk
                L += ["", "Cold read (once a map): start a NEW agent with exactly this, and nothing else:",
                      f'  "Read the file {pk["prompt"]} and follow its instructions exactly. Do nothing else."',
                      "  Never answer it yourself and never tell that agent what the map is about. Then:",
                      f'  gmap.py cold --answer "{pk["answer"]}"',
                      "  With no way to start a fresh agent here, say so: gmap.py cold --none"]
    job["drafts"].append(rec)
    log(job, "draft", "a draft was built", blocked=rec["blocked"])
    save(job)
    out(L + next_lines("build"))
    return code


def cmd_cold(a) -> int:
    job = load(a.job)
    msg = job["brief"]["message"]
    if a.none:
        job["cold_read"] = {"made": False, "why": "no fresh agent could be started in this session", "at": now()}
        log(job, "draft", "no cold read was made")
        save(job)
        out(["no cold read made (recorded). The map trace's graded read, after the map is finished, still gives one."])
        return gc.EXIT_OK
    said = a.text
    if a.answer:
        try:
            doc = json.loads(Path(a.answer).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise JobError(f"{a.answer} is not there yet or is not JSON: the fresh agent writes it") from None
        said = next((str(doc[k]) for k in ("telling", "tells", "says", "message", "q1", "1") if isinstance(doc, dict) and doc.get(k)), None) or \
            (str(next(iter(doc.values()))) if isinstance(doc, dict) and doc else None)
        outcome = doc.get("outcome") if isinstance(doc, dict) else None
    else:
        outcome = None
    if not said:
        raise JobError('give the reader\'s answer: --answer FILE (the agent\'s JSON) or --text "what they said the map tells"')
    a_w = {w for w in re.findall(r"[a-z0-9]{4,}", msg.lower())}
    b_w = {w for w in re.findall(r"[a-z0-9]{4,}", said.lower())}
    share = len(a_w & b_w) / max(1, len(a_w))
    job["cold_read"] = {"made": True, "said": said[:600], "outcome_seen": outcome, "shared_words": round(share, 2), "at": now()}
    log(job, "draft", "the cold read came back")
    save(job)
    L = ["A stranger says the map tells:", f"  {said[:400]}", "The message is:", f"  {msg}",
         f"  words of the message the stranger used: {round(100 * share)}%" + (f"; they took it to be for: {outcome}" if outcome else ""),
         "If the two say different things, the map is not showing its message: change the frame, the emphasis or the key (not the words), draft again.",
         "If they agree, build."]
    out(L + next_lines("build"))
    return gc.EXIT_OK


def cmd_build(a) -> int:
    job = load(a.job)
    if not job.get("spec"):
        raise JobError("there is no MapSpec yet: gmap.py compose writes it")
    code, res, digest = _run_build(job, ["--formats", a.formats] + (["--strict"] if a.strict else []))
    L = [digest]
    rec = {"at": now(), "exit": code, "formats": a.formats}
    if res is not None:
        dev = res.get("deviations") or {}
        rec.update(files=res.get("files") or [], blocked=bool(res.get("blocked")), deviations=dev.get("counts"), reverted=[r["key"] for r in dev.get("reverted") or []],
                   stem=res.get("stem"), not_applicable=(res.get("not_applicable") or {}).get("count", 0))
        note_usage(job)
    job["builds"].append(rec)
    log(job, "build", "the map was built", blocked=rec.get("blocked"))
    save(job)
    if res is not None and not res.get("blocked"):
        L += [""] + road_lines(road_of(job))
    out(L + next_lines("review"))
    return code


# ---------------------------------------------------------------------------------------------------------------- the road, usage
def road_of(job: dict) -> dict:
    """How this map was made, from the job's own record (the road rubric's measures for one map)."""
    data = job.get("data") or []
    refs = [n for n in job["needs"] if n.get("status") == "chosen" and not any(d.get("need") == n["theme"] for d in data)]
    doors = [d["door"] for d in data] + ["library layer"] * len(refs)
    steps = [d for d in data if d["door"] == "registered step"]
    last = (job.get("builds") or [{}])[-1]
    dev = last.get("deviations") or {}
    seen = sum(len(n.get("candidates") or []) for n in job["needs"])
    used = sorted({n["chosen"] for n in job["needs"] if n.get("chosen")})
    enrich = job.get("enrich") or []
    return {"job": job["id"], "layers": len(doors), "through_the_door": len(doors), "by_door": {k: doors.count(k) for k in sorted(set(doors))},
            "registered_steps": [{"name": d["name"], "libraries": d.get("libraries") or []} for d in steps],
            "sources_seen": seen, "sources_taken": used, "needs": len(job["needs"]), "enrichment": {"offered": len(enrich), "accepted": sum(1 for e in enrich if e.get("answer") == "accepted"),
                                                                                             "declined": sum(1 for e in enrich if e.get("answer") == "declined")},
            "deviations": dev or None, "put_back": last.get("reverted") or [], "cold_read": (job.get("cold_read") or {}).get("made"),
            "drafts": len(job.get("drafts") or []), "builds": len(job.get("builds") or [])}


def road_lines(r: dict) -> list[str]:
    d = r.get("deviations") or {}
    L = [f"Road (job {r['job']})",
         f"  layers through the plugin's door: {r['through_the_door']} of {r['layers']}  (" + ", ".join(f"{k}: {v}" for k, v in r["by_door"].items()) + ")",
         f"  sources seen {r['sources_seen']}, taken {len(r['sources_taken'])} for {r['needs']} need(s); also offered {r['enrichment']['offered']} "
         f"(accepted {r['enrichment']['accepted']}, declined {r['enrichment']['declined']})",
         "  deviations from the defaults: " + (f"asked {d.get('asked', 0)}, intent {d.get('intent', 0)}, unexplained {d.get('unexplained', 0)}" if d else "not known yet")
         + (f"; put back: {', '.join(r['put_back'])}" if r["put_back"] else ""),
         f"  registered steps (candidate ops): {len(r['registered_steps'])}" + (": " + ", ".join(s["name"] for s in r["registered_steps"]) if r["registered_steps"] else ""),
         "  cold read: " + {True: "made", False: "not made (no fresh agent here)", None: "not made yet"}[r["cold_read"]]]
    return L


def usage_path() -> Path:
    return gc.workdir(create=True) / "usage.json"


def note_usage(job: dict) -> None:
    """Which library entries each job saw and took, kept beside the jobs, so a source nobody ever takes can be found (themes.py, usage)."""
    p = usage_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        doc = {}
    jobs = doc.setdefault("jobs", {})
    jobs[job["id"]] = {"at": now(), "taken": sorted({n["chosen"] for n in job["needs"] if n.get("chosen")}),
                       "seen": sorted({c["id"] for n in job["needs"] for c in n.get("candidates") or []})}
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def cmd_usage(a) -> int:
    p = usage_path()
    doc = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"jobs": {}}
    taken: dict = {}
    seen: dict = {}
    for j in doc["jobs"].values():
        for e in j.get("taken") or []:
            taken[e] = taken.get(e, 0) + 1
        for e in j.get("seen") or []:
            seen[e] = seen.get(e, 0) + 1
    never = sorted(e for e in seen if e not in taken)
    L = [f"{len(doc['jobs'])} job(s) on record here; {len(taken)} library entries taken, {len(never)} seen and never taken"]
    L += [f"  taken {n}x: {e}" for e, n in sorted(taken.items(), key=lambda kv: -kv[1])[:15]]
    if never:
        L.append("  seen on a sheet and never taken: " + ", ".join(never[:12]))
    out(L)
    return gc.EXIT_OK


def cmd_review(a) -> int:
    job = load(a.job)
    r = road_of(job)
    job["road"] = r
    log(job, "review", "the road was measured")
    save(job)
    L = road_lines(r)
    if not job.get("builds"):
        L.append("  no finished build yet: gmap.py build")
    out(L + ["", "The picture is graded by the map trace, by agents that did not make it: /map-trace", "  (a grade never changes the map in hand)"])
    return gc.EXIT_OK


def cmd_status(a) -> int:
    job = load(a.job)
    order = steps_doc().get("order") or []
    at = job.get("step", "brief")
    nxt = order[min(order.index(at) + 1, len(order) - 1)] if at in order else "sources"
    L = [f"Job {job['id']} is at: {at}", f"  message: {job['brief']['message']}", f"  intent {job['brief']['intent']}, for {job['brief']['outcome']}",
         "  needs: " + (", ".join(f"{n['theme']} ({n.get('chosen') or 'open'})" for n in job["needs"]) or "none"),
         f"  data in hand: {', '.join(d['name'] + ' (' + d['door'] + ')' for d in job['data']) or 'none'}", f"  MapSpec: {job.get('spec') or 'not written'}",
         f"  drafts {len(job['drafts'])}, builds {len(job['builds'])}"]
    out(L + next_lines(nxt))
    return gc.EXIT_OK


def _version() -> str | None:
    try:
        return gc.build_manifest().get("version")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------- CLI
def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def job_arg(p):
        p.add_argument("--job", help="a job other than the open one")
        return p
    b = job_arg(sub.add_parser("brief"))
    b.add_argument("--id")
    b.add_argument("--region")
    b.add_argument("--bbox")
    b.add_argument("--place")
    b.add_argument("--message")
    b.add_argument("--intent")
    b.add_argument("--outcome", choices=["explain", "compare", "locate", "inventory", "monitor", "explore", "sequence", "evoke"])
    b.add_argument("--audience", choices=["any", "internal", "external", "regulatory"], default="external")
    b.add_argument("--template", default="letter-landscape")
    b.add_argument("--need", action="append")
    b.add_argument("--no-usual", action="store_true", help="leave out the themes the intent usually needs")
    b.add_argument("--file", action="append")
    b.add_argument("--ask", action="append")
    b.add_argument("--title")
    b.add_argument("--subtitle")
    b.add_argument("--tagline")
    b.add_argument("--style-pack")
    b.add_argument("--computer", choices=["linked", "none"])
    b.add_argument("--replace", action="store_true")
    s = job_arg(sub.add_parser("sources"))
    s.add_argument("--count", action="store_true")
    c = job_arg(sub.add_parser("choose"))
    c.add_argument("need")
    c.add_argument("entry")
    c.add_argument("--why")
    n = job_arg(sub.add_parser("need"))
    n.add_argument("op", choices=["add", "decline"])
    n.add_argument("theme")
    n.add_argument("--why")
    d = job_arg(sub.add_parser("data"))
    d.add_argument("op", choices=["plan", "add", "file", "derive", "step"])
    d.add_argument("name", nargs="?")
    d.add_argument("--file")
    d.add_argument("--path")
    d.add_argument("--layer")
    d.add_argument("--tool")
    d.add_argument("--source")
    d.add_argument("--license")
    d.add_argument("--op", dest="do")
    d.add_argument("--in", dest="src_many", nargs="+")
    d.add_argument("--other")
    d.add_argument("--other-layer")
    d.add_argument("--out", dest="out_many", nargs="+")
    d.add_argument("--param", action="append")
    d.add_argument("--script")
    d.add_argument("--no-strict", action="store_true")
    cm = job_arg(sub.add_parser("compose"))
    cm.add_argument("--out")
    cm.add_argument("--replace", action="store_true")
    st = job_arg(sub.add_parser("set"))
    st.add_argument("pairs", nargs="+")
    st.add_argument("--layer")
    st.add_argument("--asked")
    dr = job_arg(sub.add_parser("draft"))
    dr.add_argument("--no-cold", action="store_true")
    dr.add_argument("--cold", action="store_true", help="ask for a cold read again (the frame or the emphasis changed)")
    co = job_arg(sub.add_parser("cold"))
    co.add_argument("--answer")
    co.add_argument("--text")
    co.add_argument("--none", action="store_true")
    bu = job_arg(sub.add_parser("build"))
    bu.add_argument("--formats", default="svg,pdf,png")
    bu.add_argument("--strict", action="store_true")
    job_arg(sub.add_parser("review"))
    job_arg(sub.add_parser("status"))
    job_arg(sub.add_parser("usage"))
    r = sub.add_parser("rules")
    r.add_argument("step")
    sub.add_parser("intents")
    return ap


def main(argv=None) -> int:
    gc.utf8_console() if hasattr(gc, "utf8_console") else None
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1, 0, -1):                 # a box that starts west of Greenwich begins with a minus sign: keep it one argument
        if argv[i - 1] == "--bbox" and re.match(r"^-\d", argv[i]):
            argv[i - 1:i + 1] = ["--bbox=" + argv[i]]
    rest = []
    if "--" in argv:                                      # data step NAME --script S ... -- the script's own arguments
        k = argv.index("--")
        argv, rest = argv[:k], argv[k + 1:]
    a = parser().parse_args(argv)
    a.rest = rest
    try:
        if a.cmd == "rules":
            out(rules_lines(a.step))
            return gc.EXIT_OK
        if a.cmd == "intents":
            out(f"{d.get('intent'):<11} for: {d.get('outcome'):<9} {d.get('says')}" for d in policy.intents())
            return gc.EXIT_OK
        if a.cmd == "data":
            if a.op != "plan" and not a.name:
                raise JobError("data add|file|derive|step need a name")
            a.src = (a.src_many or [None])[0]
            a.out = (a.out_many or [None])[0]
            if a.op == "derive" and not (a.do and a.src and a.out):
                raise JobError("data derive NAME --op OP --in FILE --out FILE [--other FILE] [--param k=v]")
            if a.op == "step" and not a.script:
                raise JobError("data step NAME --script FILE [--in F ...] --out F [...]")
            if a.op == "add" and not a.file:
                raise JobError("data add NEED --file FILE")
            if a.op == "file" and not a.path:
                raise JobError("data file NAME --path FILE --source ... --license ...")
        return {"brief": cmd_brief, "sources": cmd_sources, "choose": cmd_choose, "need": cmd_need, "data": cmd_data, "compose": cmd_compose, "set": cmd_set, "draft": cmd_draft,
                "cold": cmd_cold, "build": cmd_build, "review": cmd_review, "status": cmd_status, "usage": cmd_usage}[a.cmd](a)
    except JobError as ex:
        print(f"gmap: {ex}", file=sys.stderr)
        return ex.code
    except BrokenPipeError:                               # the reader closed the pipe (| head): the job record is already written
        try:
            sys.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
