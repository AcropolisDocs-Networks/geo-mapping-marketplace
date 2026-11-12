#!/usr/bin/env python3
"""map-doctor: dependency, driver, tier and packaging report. Cheap; run it rather than guess why a map failed.

    doctor.py [--json] [--offline] [--fix [--quick] [--all]] [--device-bridge yes|no|unknown] [--mcp yes|no|unknown] [--credentials-dir DIR]
    doctor.py --fonts arab,hebr,deva,hani           1.5.0: the pinned Noto fonts for scripts Inter lacks, into the font cache

HARD dependencies block maps; SOFT ones give a quietly worse result. `--fix --all` pip-installs what is
missing (pip with --break-system-packages as a second attempt); `--fix --quick` (1.2.0) installs what a map needs in one pip call, wheels
first, within 10 minutes: the first step in a fresh cloud workspace. Exit 0 when no HARD item is missing, else 1;
exit 2 when a second geo-mapping edition is installed.

Credentials (0.6.0): a summary of the person's registered credentials, never a value: how many, whether the plugin's MCP server answered in
this session (`--mcp yes`, the script cannot see the tool list), and any rejected, expired, missing, not private, not verifiable from here, a
browser key without its domain restriction, or an old key file still kept after a rotation. `--credentials-dir` is the credential folder when it
is connected to this session; without it the person's own folder is read when it is on this machine.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata as md
import json
import subprocess
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lints"))
import env_probe  # noqa: E402
import gmap_common as gc  # noqa: E402


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v).split("+")[0])[:3])


def check_deps() -> list[dict]:
    rows = []
    for d in gc.load_yaml(gc.plugin_root() / "data" / "dependencies.yaml"):
        try:
            ver = md.version(re.split(r"[\[<>=!~ ]", d["pip"])[0])      # psycopg[binary] is the distribution psycopg
            ok = True
        except md.PackageNotFoundError:
            ver, ok = None, False
        too_old = bool(ok and d.get("min") and _vtuple(ver) < _vtuple(d["min"]))
        rows.append({**d, "installed": ok and not too_old, "version": ver, "too_old": too_old})
    return rows


def check_tools(deps: list[dict]) -> dict:
    """What this machine can write: which output formats work and by which route (spec 7.1 map-export, 12.7)."""
    import shutil
    have = {d["name"]: d["installed"] for d in deps}
    pdftoppm = shutil.which("pdftoppm")
    chromium = None
    try:
        import playwright  # noqa: F401
        chromium = True
    except Exception:  # noqa: BLE001
        chromium = False
    svg = have.get("lxml", False)
    pdf = have.get("weasyprint", False) and have.get("pikepdf", False)
    wp_fix = None
    if have.get("weasyprint", False):                      # installed is not importable: WeasyPrint loads Pango, GObject and HarfBuzz at import
        try:
            import pdf_stack
            st = pdf_stack.weasyprint_status()
            if not st["ok"]:
                pdf = False
                wp_fix = st["fix"]
        except Exception:  # noqa: BLE001
            pass
    pdfium = have.get("pypdfium2", False)
    png_route = "pypdfium2" if (pdf and pdfium) else ("pdftoppm" if (pdf and pdftoppm) else ("chromium" if chromium else None))
    readback = "pypdf" if have.get("pypdf", False) else ("pikepdf" if have.get("pikepdf", False) else None)
    return {"svg": {"ok": True, "route": "built in" + ("" if svg else " (lxml missing: output checks are reduced)")},
            "pdf": {"ok": pdf, "route": ("weasyprint + pikepdf" + (f", checked back with {readback}" if readback else "")) if pdf else None,
                    "fix": None if pdf else (wp_fix or "install weasyprint and pikepdf (doctor --fix --all)")},
            "png": {"ok": png_route is not None and have.get("pillow", False), "route": png_route,
                    "fix": None if png_route else "install pypdfium2 (doctor --fix --all), poppler-utils (pdftoppm) or Playwright Chromium"},
            "text_measure": {"ok": have.get("fonttools", False), "route": "fontTools" if have.get("fonttools") else "estimate",
                             "fix": None if have.get("fonttools") else "install fonttools and brotli"}}


def check_drivers() -> list[dict]:
    try:
        import pyogrio
        drivers = pyogrio.list_drivers()
    except Exception:  # noqa: BLE001
        return [{"status": "unavailable", "name": "(pyogrio missing)", "note": "install pyogrio to check GDAL drivers"}]
    rows = []
    for d in gc.load_yaml(gc.plugin_root() / "data" / "required-drivers.yaml"):
        have = drivers.get(d["name"])
        need_write = d["mode"] == "rw"
        ok = bool(have) and ("w" in have if need_write else "r" in have)
        status = "ok" if ok else ("missing-optional" if d.get("optional") else "missing")
        rows.append({"name": d["name"], "formats": d["formats"], "have": have, "status": status, "note": d.get("note")})
    return rows


def check_assets() -> list[dict]:
    root = gc.plugin_root()
    rows = []
    for rel in ("assets/fonts/inter/inter-latin-wght-normal.woff2", "assets/fonts/inter/LICENSE.txt", "schemas/mapspec.schema.json"):
        rows.append({"path": rel, "present": (root / rel).is_file()})
    return rows


def check_packs(manifest: dict) -> list[dict]:
    import brand_lint as lint
    rows = []
    import brandlib
    for slug in brandlib.available(manifest):                # 1.7.0: the shipped brand and every pack the person added
        f = lint.check_pack_integrity(slug) + lint.check_pack_drift(slug)      # 0.15.0: MAP-BRAND-016, the source changed since import
        rows.append({"brand": slug, "origin": brandlib.origin(slug), "ok": not any(x["severity"] == "BLOCK" for x in f), "findings": f})
    return rows


def check_server_launcher() -> dict:
    """1.7.0: which launcher .mcp.json names. The shipped entry is the Windows one (a plugin's server entry cannot differ by operating
    system); on a macOS or Linux computer `sh mcp/use-posix.sh` switches it to launch.sh. In a desktop session this shell is a sandbox and
    the server runs on the person's own computer, so the note is advice for that computer, never a failure here."""
    try:
        text = (gc.plugin_root() / ".mcp.json").read_text(encoding="utf-8")
    except OSError:
        return {"entry": "none", "note": "no .mcp.json in this plugin folder"}
    if "launch.sh" in text:
        return {"entry": "macOS/Linux (mcp/launch.sh)"}
    return {"entry": "Windows (mcp/launch.cmd)", "note": "on a macOS or Linux computer run  sh mcp/use-posix.sh  once in the plugin's folder, then restart the plugin"}


def check_library() -> dict:
    import ref_library
    return ref_library.summary()


def check_credentials(mcp: str = "unknown", folder: str | None = None) -> dict:
    """The credential summary (0.6.0): counts and names only, never a value or a file's content."""
    try:
        import credentials as cr
        with cr.value_scope() as vs:
            return vs.clean(cr.doctor_summary(cr.Store(folder) if folder else cr.Store(), mcp))
    except Exception as ex:  # noqa: BLE001 - a broken registry is reported, never fatal to the doctor
        return {"count": None, "error": f"the credential registry could not be read ({type(ex).__name__})", "mcp_server": mcp}


def check_engines() -> dict:
    """0.24.0 (TODO M16, M3, M4b): DuckDB and its spatial extension (the large-file engine), the PROJ datum grids, and whether the plugin's own
    data-service server can start here (mcp and httpx importable). Answers only; the one fix offered is DuckDB's INSTALL spatial, run on
    --fix --all with the person's yes."""
    out: dict = {}
    try:
        import duck_engine as de
        st = de.status()
        out["duckdb"] = {"installed": bool(st.get("duckdb")), "version": st.get("duckdb"), "spatial": bool(st.get("spatial")),
                         "fix": None if st.get("spatial") or not st.get("duckdb") else "python3 -c \"import duckdb; duckdb.connect().execute('INSTALL spatial')\""}
    except Exception as ex:  # noqa: BLE001
        out["duckdb"] = {"installed": False, "error": f"{type(ex).__name__}"}
    try:
        import pyproj
        from pathlib import Path as _P
        d = _P(pyproj.datadir.get_data_dir())
        grids = sorted(p.name for p in d.glob("*.tif")) if d.is_dir() else []
        net = bool(pyproj.network.is_network_enabled())
        us = [g for g in grids if g.startswith("us_noaa_")]
        out["proj_grids"] = {"data_dir": str(d), "grids": len(grids), "us_noaa": len(us), "network": net,
                             "note": ("datum grids are present" if grids else
                                      "no PROJ datum grids here: a NAD27 or older NAD83 layer is shifted by a coarser method (metres to tens of metres off), "
                                      "and the build's MAP-CRS-004 finding says so and names the grid that would fix it") + ("" if not net else "; PROJ may fetch a grid when one is needed")}
    except Exception as ex:  # noqa: BLE001
        out["proj_grids"] = {"error": f"{type(ex).__name__}"}
    try:
        import importlib.util as iu
        have = {m: iu.find_spec(m) is not None for m in ("mcp", "httpx", "yaml", "geopandas")}
        out["service_route"] = {"ok": all(have.values()), "modules": have,
                                "note": "the bundled data-service server can start here" if all(have.values()) else
                                        "the bundled data-service server cannot start here: " + ", ".join(k for k, v in have.items() if not v) + " missing (pip install -r mcp/requirements.txt)"}
    except Exception as ex:  # noqa: BLE001
        out["service_route"] = {"ok": False, "error": f"{type(ex).__name__}"}
    return out


def fix_spatial() -> dict:
    """INSTALL spatial for DuckDB (one download from DuckDB's own extension host), only on --fix --all."""
    try:
        import duckdb
        duckdb.connect().execute("INSTALL spatial")
        return {"name": "duckdb spatial", "ok": True}
    except Exception as ex:  # noqa: BLE001
        return {"name": "duckdb spatial", "ok": False, "error": str(ex)[:160]}


def fix(rows: list[dict], include_soft: bool) -> list[dict]:
    log = []
    todo = [r for r in rows if not r["installed"] and (r["level"] == "HARD" or include_soft)]
    for r in todo:
        for extra in ([], ["--break-system-packages"]):
            req = f"{r['pip']}>={r['min']}" if r.get("min") else r["pip"]
            p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", req, *extra], capture_output=True, text=True)
            if p.returncode == 0:
                log.append({"package": r["pip"], "installed": True, "flags": extra})
                break
        else:
            log.append({"package": r["pip"], "installed": False, "error": p.stderr.strip().splitlines()[-1:] or ""})
    importlib.invalidate_caches()
    return log


# 1.2.0 (Days 4, 5, 6, 8, 9, 10): what a map needs in a fresh cloud workspace, installed in one pip call (`--fix --quick`). The dev and test
# requirements are left out: `pip install -r requirements-dev.txt` pulls Playwright and the test stack and once ran for 20 minutes.
QUICK = ("pyogrio", "geopandas", "shapely", "pyproj", "matplotlib", "jsonschema", "pyyaml", "pandas", "numpy", "pillow",
         "pyarrow", "lxml", "httpx", "weasyprint", "pikepdf", "pypdf", "pypdfium2", "fonttools", "brotli", "pydyf", "topojson",
         "rasterio", "contourpy", "openpyxl", "h3")
QUICK_TIMEOUT_S = 600


def fix_quick(rows: list[dict], timeout_s: int = QUICK_TIMEOUT_S, run=subprocess.run) -> list[dict]:
    """Install every missing QUICK package in ONE pip call with --prefer-binary (wheels only where a wheel exists, so nothing is compiled),
    a ceiling of `timeout_s`, and --break-system-packages as a second attempt. Returns one log row per package."""
    todo = [r for r in rows if not r["installed"] and r["name"] in QUICK]
    if not todo:
        return []
    reqs = [f"{r['pip']}>={r['min']}" if r.get("min") else r["pip"] for r in todo]
    print(f"map-doctor --quick: installing {len(reqs)} package(s) in one step: {', '.join(r['name'] for r in todo)}", file=sys.stderr, flush=True)
    err = ""
    for extra in ([], ["--break-system-packages"]):
        try:
            p = run([sys.executable, "-m", "pip", "install", "--quiet", "--prefer-binary", "--upgrade-strategy", "only-if-needed", *reqs, *extra],
                    capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            err = f"pip did not finish within {timeout_s} s"
            break
        if p.returncode == 0:
            importlib.invalidate_caches()
            return [{"package": r["pip"], "installed": True, "flags": ["--prefer-binary", *extra], "quick": True} for r in todo]
        err = (p.stderr or "").strip().splitlines()[-1:] or ""
        err = err[0] if isinstance(err, list) and err else str(err)
    importlib.invalidate_caches()
    return [{"package": r["pip"], "installed": False, "error": err, "quick": True} for r in todo]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --fix: install SOFT dependencies too")
    ap.add_argument("--quick", action="store_true", help="with --fix: everything a map needs (HARD, PDF/PNG, rasters) in one pip call, wheels first, "
                                                          "10 minutes at most; the first step in a fresh cloud workspace (1.2.0)")
    ap.add_argument("--device-bridge", choices=["yes", "no", "unknown"], default="unknown")
    ap.add_argument("--mcp", choices=["yes", "no", "unknown"], default="unknown", help="whether a call to the plugin's MCP server succeeded in this session")
    ap.add_argument("--credentials-dir", help="the credential folder, when it is connected to this session")
    ap.add_argument("--fonts", help="1.5.0: fetch the pinned Noto fonts for these scripts into the font cache (arab,hebr,deva,beng,thai,hani,jpan,kore,...)")
    a = ap.parse_args(argv)
    if a.fonts:                                     # 1.5.0 (R3-SCRIPT): SHA-256 and the OFL text checked; MAP-LOC-004 for each one fetched
        import script_text as st
        res = st.fetch([k for k in a.fonts.split(",") if k.strip()])
        print(json.dumps(res, indent=2))
        return gc.EXIT_OK if all(r["ok"] for r in res) else gc.EXIT_SERVICE

    manifest = gc.build_manifest()
    editions = env_probe.installed_editions(manifest["plugin_name"])
    others = [e for e in editions if e != manifest["plugin_name"]]
    deps = check_deps()
    fixed = (fix_quick(deps) + (fix(check_deps(), a.all) if a.all else [])) if (a.fix and a.quick) else (fix(deps, a.all) if a.fix else [])
    if fixed:
        deps = check_deps()
    eg = env_probe.egress(a.offline)
    report = {
        "plugin": manifest["plugin_name"], "version": manifest.get("version"),
        "single_edition": {"ok": not others, "installed": editions},
        "dependencies": deps, "fixed": fixed, "outputs": check_tools(deps),
        "gdal_drivers": check_drivers(),
        "egress": {k: eg[k] for k in ("classification", "checked") if k in eg},
        "tier": env_probe.classify_tier(a.device_bridge, eg, "unknown"),
        "assets": check_assets(), "brand_packs": check_packs(manifest), "library": check_library(),
        "credentials": check_credentials(a.mcp, a.credentials_dir),
        "engines": check_engines(),
        "server_launcher": check_server_launcher(),
    }
    if a.fix and a.all and report["engines"].get("duckdb", {}).get("installed") and not report["engines"]["duckdb"].get("spatial"):
        report["fixed"] = list(report["fixed"]) + [fix_spatial()]
        report["engines"] = check_engines()
    hard_missing = [d["name"] for d in deps if d["level"] == "HARD" and not d["installed"]]
    soft_missing = [d["name"] for d in deps if d["level"] == "SOFT" and not d["installed"]]
    report["summary"] = {"hard_missing": hard_missing, "soft_missing": soft_missing,
                         "ok": not hard_missing and not others and all(p["ok"] for p in report["brand_packs"])}
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    if others:
        return gc.EXIT_USAGE
    return gc.EXIT_OK if not hard_missing else gc.EXIT_RUNTIME


def render(r: dict) -> str:
    L = [f"{r['plugin']} {r['version']}: tier {r['tier']} (shell egress {r['egress'].get('classification')})"]
    if not r["single_edition"]["ok"]:
        L.append(f"  !! Two editions installed: {', '.join(r['single_edition']['installed'])}. Uninstall one.")
    L.append("Dependencies")
    for d in r["dependencies"]:
        mark = "ok     " if d["installed"] else ("TOO OLD" if d.get("too_old") else ("MISSING" if d["level"] == "HARD" else "missing"))
        L.append(f"  [{d['level']:4}] {mark} {d['name']:<12} {d['version'] or '-':<10} {d['why']}")
    L.append("Outputs")
    for k, v in r["outputs"].items():
        L.append(f"  {k:<13} {'ok' if v['ok'] else 'NOT AVAILABLE':<14} {v.get('route') or '-'}" + (f"  -> {v['fix']}" if v.get("fix") else ""))
    L.append("GDAL drivers")
    for d in r["gdal_drivers"]:
        L.append(f"  {d['status']:<17} {d['name']}" + (f"  ({d['note']})" if d.get("note") else ""))
    L.append("Packaging")
    L.append("  assets: " + ", ".join(f"{a['path'].split('/')[-1]}={'ok' if a['present'] else 'MISSING'}" for a in r["assets"]))
    L.append("  brand packs: " + ", ".join(f"{p['brand']}{' (added)' if p.get('origin') == 'added' else ''}={'ok' if p['ok'] else 'FAIL'}" for p in r["brand_packs"]))
    sl = r.get("server_launcher") or {}
    if sl:
        L.append(f"  data server entry: {sl['entry']}" + (f"  ({sl['note']})" if sl.get("note") else ""))
    lib = r["library"]
    L.append(f"  library: {lib['entries']} entries, {lib['tags']} tag values, {lib['types']} types")
    L.append("Credentials (yours, on this computer; never shown)")
    c = r.get("credentials") or {}
    if c.get("error"):
        L.append(f"  {c['error']}")
    elif not c.get("folder_found"):
        L.append("  no credential folder here. On your own computer the plugin's MCP server reads it (credentials_status); "
                 f"MCP server answered this session: {c.get('mcp_server')}")
    else:
        L.append(f"  {c['count']} registered; MCP server answered this session: {c.get('mcp_server')}")
        for k, label in (("rejected", "rejected by the service (rotate)"), ("expired", "past the review date"), ("missing", "file missing"),
                         ("not_private", "readable by other accounts"), ("unverifiable", "privacy not verifiable from here"),
                         ("browser_unrestricted", "browser key without the domain restriction"), ("old_file_pending", "old key file still kept after a rotation")):
            if c.get(k):
                L.append(f"  !! {label}: {', '.join(c[k])}")
    e = r.get("engines") or {}
    L.append("Engines and routes")
    dk = e.get("duckdb") or {}
    L.append(f"  duckdb       {dk.get('version') or 'not installed':<10} spatial {'ok' if dk.get('spatial') else 'not loaded'}" + (f"  -> {dk['fix']}" if dk.get("fix") else ""))
    pg = e.get("proj_grids") or {}
    L.append(f"  PROJ grids   {pg.get('grids', '-')} grid file(s), {pg.get('us_noaa', '-')} US; {pg.get('note') or pg.get('error') or ''}")
    sr = e.get("service_route") or {}
    L.append(f"  data service {'ok' if sr.get('ok') else 'NOT AVAILABLE'}  {sr.get('note') or sr.get('error') or ''}")
    s = r["summary"]
    L.append(f"Result: {'ready' if s['ok'] else 'NOT ready'}; HARD missing: {', '.join(s['hard_missing']) or 'none'}; SOFT missing: {', '.join(s['soft_missing']) or 'none'}")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
