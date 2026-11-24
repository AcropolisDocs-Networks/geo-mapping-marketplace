#!/usr/bin/env python3
"""map-doctor: dependency, driver, tier and packaging report. Cheap; run it rather than guess why a map failed.

    doctor.py [--json] [--offline] [--fix [--quick] [--all] [--profile map|raster|h3|web|video|db|all]] [--device-bridge yes|no|unknown]
              [--mcp yes|no|unknown] [--credentials-dir DIR]
    doctor.py --fonts arab,hebr,deva,hani           1.5.0: the pinned Noto fonts for scripts Inter lacks, into the font cache

HARD dependencies block maps; SOFT ones give a quietly worse result. `--fix --all` pip-installs what is
missing (pip with --break-system-packages as a second attempt); `--fix --quick` (1.2.0) installs what a map needs in one pip call, wheels
first, within 10 minutes: the first step in a fresh cloud workspace. Exit 0 when no HARD item is missing, else 1;
exit 2 when a second geo-mapping edition is installed.

2.4.0: `--fix --profile NAME[,NAME]` installs one group of packages (data/dependencies.yaml `profiles`) in one pip call: map (what a map needs,
the old --quick list), raster, h3, web, video, db (DuckDB with its spatial wheel, PostGIS) or all. `--fix --all` is `--profile all` and also
installs DuckDB's spatial extension; the text report has a Fixed section that lists every item installed and the spatial result, which is
checked again in the same run. The first line names the place the doctor runs in (host, Cowork VM or cloud workspace; env_probe.place).

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
        d = {**d, "profiles": list(d.get("profiles") or [])}
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


def check_release(offline: bool) -> dict:
    """1.16.0 (Day 14 issue 12): whether a newer release is out (release_check.py; once a day, never with --offline)."""
    import release_check as rc
    try:
        return rc.check(cache_only=offline)
    except Exception as ex:  # noqa: BLE001 - the check never stops the doctor
        return {"installed": rc.installed(), "latest": None, "newer": False, "route": f"failed: {type(ex).__name__}", "note": None}


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
        st = de.status(fresh=True)               # 2.4.0 (I-02): never the answer memoized before a --fix in this same process
        out["duckdb"] = {"installed": bool(st.get("duckdb")), "version": st.get("duckdb"), "spatial": bool(st.get("spatial")),
                         "spatial_source": st.get("spatial_source"),
                         "fix": None if st.get("spatial") or not st.get("duckdb") else "doctor.py --fix --profile db"}
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
    try:                                              # 2.5.0 (T-08): the elevation sources and where each has data
        rows = gc.load_yaml(gc.plugin_root() / "data" / "terrain-sources.yaml") or []
        out["terrain_sources"] = [{"id": s["id"], "status": s.get("status"), "coverage": s.get("coverage"), "surface": s.get("surface")} for s in rows]
    except Exception as ex:  # noqa: BLE001
        out["terrain_sources"] = [{"id": "?", "status": f"unreadable ({type(ex).__name__})"}]
    try:
        import importlib.util as iu
        have = {m: iu.find_spec(m) is not None for m in ("mcp", "httpx", "yaml", "geopandas")}
        out["service_route"] = {"ok": all(have.values()), "modules": have,
                                "note": "the bundled data-service server can start here" if all(have.values()) else
                                        "the bundled data-service server cannot start here: " + ", ".join(k for k, v in have.items() if not v) + " missing (pip install -r mcp/requirements.txt)"}
    except Exception as ex:  # noqa: BLE001
        out["service_route"] = {"ok": False, "error": f"{type(ex).__name__}"}
    return out


def _spatial_wheel_req() -> str | None:
    """The duckdb-extension-spatial requirement that matches the installed DuckDB's major.minor (the wheel's own version tracks DuckDB's)."""
    try:
        import duckdb
        mm = re.findall(r"\d+", str(duckdb.__version__))[:2]
    except Exception:  # noqa: BLE001
        return None
    return f"duckdb-extension-spatial=={mm[0]}.{mm[1]}.*" if len(mm) == 2 else None


def fix_spatial(run=subprocess.run, timeout_s: int = 300) -> dict:
    """DuckDB's spatial extension (2.4.0, I-02, I-03), only on --fix --all or --profile db. Routes, in order: already loadable (nothing to
    do); the duckdb-extension-spatial wheel that matches DuckDB's major.minor (pip, no call to DuckDB's extension host); DuckDB's own
    `INSTALL spatial` (one download from extensions.duckdb.org). Every route is checked by loading the extension again in this process."""
    import duck_engine as de
    if de.spatial_available(fresh=True):
        return {"name": "duckdb spatial", "ok": True, "route": f"already there ({de._SPATIAL_STATE.get('source')})"}
    tried = []
    req = _spatial_wheel_req()
    if req:
        for extra in ([], ["--break-system-packages"]):
            try:
                p = run([sys.executable, "-m", "pip", "install", "--quiet", "--prefer-binary", req, *extra], capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                tried.append(f"pip {req}: no answer within {timeout_s} s")
                break
            if p.returncode == 0:
                if de.spatial_available(fresh=True):
                    return {"name": "duckdb spatial", "ok": True, "route": f"pip {req}"}
                tried.append(f"pip {req}: installed, but DuckDB did not load it")
                break
            last = (p.stderr or "").strip().splitlines()[-1:] or [""]
            tried.append(f"pip {req}: {last[0][:120]}")
    try:
        import duckdb
        duckdb.connect().execute("INSTALL spatial")
        if de.spatial_available(fresh=True):
            return {"name": "duckdb spatial", "ok": True, "route": "INSTALL spatial (DuckDB's extension host)"}
        tried.append("INSTALL spatial: installed, but it did not load")
    except Exception as ex:  # noqa: BLE001
        tried.append(f"INSTALL spatial: {str(ex).splitlines()[0][:120]}")
    return {"name": "duckdb spatial", "ok": False, "error": "; ".join(tried)}


# 3.0.0 (I-08): the PROJ datum grids a region needs, downloaded with the person's yes; (source_id prefixes PROJ's CDN uses, what they are)
GRID_SOURCES = {"US": ("us_noaa", "NADCON5 and NOAA grids"), "CA": ("ca_nrc", "NTv2 (Natural Resources Canada)"), "GB": ("uk_os", "OSTN15 (Ordnance Survey)"),
                "AU": ("au_icsm", "GDA94 to GDA2020 grids (ICSM)"), "NZ": ("nz_linz", "NZGD2000 grids (LINZ)"), "DE": ("de_adv", "BeTA2007 (AdV)"),
                "FR": ("fr_ign", "RGF93 grids (IGN)"), "ES": ("es_ign", "ETRS89 grids (IGN)"), "PT": ("pt_dgt", "ETRS89 grids (DGT)"),
                "CH": ("ch_swisstopo", "CHENyx06 (swisstopo)"), "AT": ("at_bev", "AT grids (BEV)"), "NL": ("nl_nsgi", "RDNAPTRANS (NSGI)"),
                "BE": ("be_ign", "BD72 grids (IGN)"), "JP": ("jp_gsi", "JGD grids (GSI)"), "BR": ("br_ibge", "SAD69 to SIRGAS grids (IBGE)")}


def grids_for(region_text: str, *, lister=None) -> dict:
    """I-08: the datum grids (and their size) for a region's countries, those not on this computer yet. -> {region, grids, mb, countries}."""
    import region as region_mod
    blk = {"admin": [c.strip() for c in region_text.split(",")]} if all(region_mod.CODE.match(c.strip().upper()) for c in region_text.split(",")) \
        else {"place": region_text}
    reg = region_mod.resolve(blk)
    from pyproj.aoi import BBox
    lister = lister or (lambda **k: __import__("pyproj.sync", fromlist=["x"]).get_transform_grid_list(**k))
    b = reg.bounds
    out = []
    for c in reg.countries:
        src = GRID_SOURCES.get(c)
        if not src:
            continue
        for g in lister(source_id=src[0], bbox=BBox(*b), include_already_downloaded=False):
            pr = g.get("properties", g) if isinstance(g, dict) else {}
            out.append({"name": pr.get("name"), "url": pr.get("url"), "bytes": int(pr.get("file_size") or pr.get("size") or 0), "country": c, "what": src[1]})
    return {"region": reg.name, "countries": reg.countries, "grids": out, "mb": round(sum(g["bytes"] for g in out) / 1e6, 1)}


def fetch_grids(grids: list[dict]) -> list[dict]:
    """Each grid into PROJ's user data folder (pyproj's own downloader, from cdn.proj.org)."""
    import pyproj.sync as ps
    d = ps.get_user_data_dir(True)
    log = []
    for g in grids:
        try:
            ps._download_resource_file(file_url=g["url"], short_name=g["name"], directory=d, verbose=False)
            log.append({"name": f"grid {g['name']}", "ok": True, "route": f"{g['bytes'] / 1e6:.1f} MB from cdn.proj.org"})
        except Exception as ex:  # noqa: BLE001
            log.append({"name": f"grid {g['name']}", "ok": False, "error": str(ex)[:160]})
    return log


def make_vm_venv(work: Path, run=subprocess.run) -> dict:
    """I-12: <work>/.geo-mapping/venv-linux with the map profile, so a session with no cloud workspace still builds in the Cowork VM."""
    venv = work / gc.WORKDIR_NAME / "venv-linux"
    py = venv / "bin" / "python3"
    if not py.exists():
        p = run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
        if p.returncode != 0:
            return {"ok": False, "venv": str(venv), "error": (p.stderr or "").strip()[-200:]}
    reqs = [f"{r['pip']}>={r['min']}" if r.get("min") else r["pip"] for r in check_deps() if r["name"] in profile_names("map")]
    cons = gc.plugin_root() / "constraints.txt"
    p = run([str(py), "-m", "pip", "install", "--quiet", "--prefer-binary", *(["-c", str(cons)] if cons.is_file() else []), *reqs],
            capture_output=True, text=True, timeout=1200)
    return {"ok": p.returncode == 0, "venv": str(venv), "python": str(py), "packages": len(reqs),
            "use": f"{py} \"$GMAP_ROOT/scripts/build_map.py\" --spec map.yaml --out out", "error": None if p.returncode == 0 else (p.stderr or "").strip()[-200:]}


def wheelhouse_dir(base: str | None = None) -> Path | None:
    """I-07 (off by default): a wheel cache in the work folder, .geo-mapping/wheelhouse/<platform tag>/, used first when it exists."""
    import os
    import sysconfig
    root = base or os.environ.get("GMAP_WHEELHOUSE")
    if not root:
        return None
    tag = sysconfig.get_platform().replace("-", "_").replace(".", "_") + f"_py{sys.version_info.major}{sys.version_info.minor}"
    return Path(root) / tag


def profile_names(profile: str) -> tuple:
    """The dependency rows in a profile (2.4.0, I-05). `all` is every row."""
    rows = gc.load_yaml(gc.plugin_root() / "data" / "dependencies.yaml")
    if profile == "all":
        return tuple(d["name"] for d in rows)
    return tuple(d["name"] for d in rows if profile in (d.get("profiles") or []))


def profiles() -> list[str]:
    seen: list[str] = []
    for d in gc.load_yaml(gc.plugin_root() / "data" / "dependencies.yaml"):
        for p in d.get("profiles") or []:
            if p not in seen:
                seen.append(p)
    return seen + ["all"]


def _pip_one_call(todo: list[dict], timeout_s: int, run, label: str) -> list[dict]:
    """One resolver run for every row in `todo` (2.4.0, I-04): --prefer-binary, --upgrade for the too-old ones, a ceiling of `timeout_s`, and
    --break-system-packages as a second attempt. One log row per package, with the total time."""
    import time
    if not todo:
        return []
    reqs = [f"{r['pip']}>={r['min']}" if r.get("min") else r["pip"] for r in todo]
    cons = gc.plugin_root() / "constraints.txt"
    cflag = ["-c", str(cons)] if cons.is_file() else []
    print(f"map-doctor {label}: Python {sys.version.split()[0]} ({sys.executable}); installing {len(reqs)} package(s) in one step: "
          f"{', '.join(r['name'] for r in todo)}", file=sys.stderr, flush=True)
    if sys.version_info[:2] < (3, 11):                           # 4.0.0 (GSC E-2): said first, not discovered from a resolver error
        print(f"map-doctor {label}: Python {sys.version_info[0]}.{sys.version_info[1]} is older than the tested set needs (3.11); the newest "
              "versions that fit this Python are installed instead (PIN_FALLBACK)", file=sys.stderr, flush=True)
        cflag = []
    t0 = time.monotonic()
    err = ""
    wh = wheelhouse_dir()
    if wh is not None and wh.is_dir() and any(wh.glob("*.whl")):          # I-07: the wheelhouse first, with no index request
        p = run([sys.executable, "-m", "pip", "install", "--quiet", "--no-index", "--find-links", str(wh), *reqs], capture_output=True, text=True, timeout=timeout_s)
        if p.returncode == 0:
            importlib.invalidate_caches()
            secs = round(time.monotonic() - t0, 1)
            return [{"package": r["pip"], "installed": True, "flags": ["--no-index", "wheelhouse"], "seconds": secs, "profile": label} for r in todo]
    for extra in ([], ["--break-system-packages"]):
        for cf in ([cflag, []] if cflag else [[]]):               # a pin that cannot install on this computer: the newest that fit instead
            try:
                p = run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "--prefer-binary", "--upgrade-strategy", "only-if-needed",
                         *cf, *reqs, *extra], capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                err = f"pip did not finish within {timeout_s} s"
                break
            if p.returncode == 0:
                importlib.invalidate_caches()
                secs = round(time.monotonic() - t0, 1)
                if wh is not None:                                  # I-07: keep the wheels for the next session's install
                    wh.mkdir(parents=True, exist_ok=True)
                    run([sys.executable, "-m", "pip", "download", "--quiet", "--prefer-binary", "-d", str(wh), *cf, *reqs], capture_output=True, text=True, timeout=timeout_s)
                elif secs > WHEELHOUSE_OFFER_S:
                    print(f"map-doctor: the install took {secs:.0f} s; GMAP_WHEELHOUSE=<work folder>/.geo-mapping/wheelhouse keeps the wheels for the next "
                          "session (doctor.py --fix --wheelhouse DIR)", file=sys.stderr)
                return [{"package": r["pip"], "installed": True, "flags": ["--prefer-binary", *extra], "seconds": secs, "profile": label} for r in todo]
            # 4.0.0 (GSC E-2): pip's real error lines, not only its last one ("ResolutionImpossible" said nothing about which pin)
            lines_ = [x.strip() for x in ((p.stderr or "") + "\n" + (p.stdout or "")).splitlines() if x.strip()]
            errs = [x for x in lines_ if x.startswith("ERROR") or "No matching distribution" in x or "requires a different Python" in x]
            err = " | ".join((errs or lines_)[-3:])[:600] if lines_ else ""
            if cf:
                print(f"map-doctor {label}: the tested versions (constraints.txt) do not all install on Python "
                      f"{sys.version_info[0]}.{sys.version_info[1]} here ({err[:200]}); trying the newest that fit (PIN_FALLBACK)",
                      file=sys.stderr, flush=True)
        if err.startswith("pip did not finish"):
            break
    importlib.invalidate_caches()
    secs = round(time.monotonic() - t0, 1)
    return [{"package": r["pip"], "installed": False, "error": err, "seconds": secs, "profile": label} for r in todo]


def fix(rows: list[dict], include_soft: bool, run=None, timeout_s: int = 900) -> list[dict]:
    """HARD rows (and SOFT ones with include_soft) that are missing or too old, in ONE pip call (2.4.0, I-04; it was one call a package)."""
    todo = [r for r in rows if not r["installed"] and (r["level"] == "HARD" or include_soft)]
    return _pip_one_call(todo, timeout_s, run or subprocess.run, "--fix --all" if include_soft else "--fix")


def fix_profile(rows: list[dict], names: list[str], run=None, timeout_s: int = 900) -> list[dict]:
    """Every missing row of the named profiles in one pip call (2.4.0, I-05)."""
    want: set = set()
    for n in names:
        want |= set(profile_names(n))
    todo = [r for r in rows if not r["installed"] and r["name"] in want]
    return _pip_one_call(todo, timeout_s, run or subprocess.run, "--profile " + ",".join(names))


# 1.2.0 (Days 4, 5, 6, 8, 9, 10): what a map needs in a fresh cloud workspace, installed in one pip call (`--fix --quick`). The dev and test
# requirements are left out: `pip install -r requirements-dev.txt` pulls Playwright and the test stack and once ran for 20 minutes.
# 2.4.0 (I-05): the list is the `map` profile in data/dependencies.yaml, so the doctor, the skills and the README read one list.
QUICK = profile_names("map")
QUICK_TIMEOUT_S = 600
WHEELHOUSE_OFFER_S = 120          # I-07 (owner decision O4): offered, never on by default, after an install this long


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


def check_projections() -> dict:
    """3.7.0 (C5): whether PROJ here draws the projections with cuts the plugin offers by name (Spilhaus needs PROJ 9.6 or later)."""
    try:
        import interrupted as cut
        why = cut.proj_ok()
    except Exception as ex:  # noqa: BLE001
        why = f"{type(ex).__name__}"
    return {"spilhaus": "ok" if why is None else why, "goode": "ok"}


KINDS = (("brand packs", "brands"), ("default brand", "user-defaults.yaml"), ("terms acknowledgment", "customizations.yaml"), ("licence answers", "licences"))


def settings_report() -> dict:
    """3.7.0 (Part D): where your own settings are, what this project holds that your own folder does not, and an edited shipped brand."""
    import brandlib as bl
    out = {"ok": True, "your_folder": str(gc.user_folder()), "brands_folder": str(gc.user_brands_dir()), "sources_folder": str(gc.user_sources_dir()),
           "home_lasts": not gc.ephemeral_home(), "default_brand_from": str(bl.default_file()) if bl.default_file() else None}
    try:
        import user_sources as us
        ls = us.listing(store=None)
        out["your_sources"] = {"active": sum(1 for r in ls["sources"] if r["status"] == "active"), "drafts": sum(1 for r in ls["sources"] if r["status"] == "draft"),
                               "due_for_a_test": [r["id"] for r in ls["sources"] if r.get("warn")]}
    except Exception as ex:  # noqa: BLE001
        out["your_sources"] = {"error": type(ex).__name__}
    out["only_in_this_project"] = to_move()
    out["edited_shipped_brand"] = [f["message"] for f in bl.edit_findings()]
    if gc.ephemeral_home():
        out["note"] = ("this session's home does not last (a Cowork VM): your own settings are kept by the plugin's data server on your computer; "
                       "bring them into a project with the user_settings tool, and keep a project's with user_settings op=keep")
    elif out["only_in_this_project"]:
        out["offer"] = "copy these into your own folder so every project finds them: doctor.py --move-settings --yes (after the person says yes)"
    return out


def to_move() -> list[dict]:
    wd, me = gc.workdir(), gc.user_folder()
    if wd.resolve() == me.resolve() if wd.exists() and me.exists() else wd == me:
        return []
    rows = []
    for label, name in KINDS:
        src = wd / name
        if not src.exists():
            continue
        tgt = gc.user_brands_dir() if name == "brands" else me / name
        if src.is_dir():
            items = [x.name for x in sorted(src.iterdir()) if not (tgt / x.name).exists()]
            if items:
                rows.append({"kind": label, "from": str(src), "items": items})
        elif not (me / name).exists():
            rows.append({"kind": label, "from": str(src), "items": [src.name]})
    return rows


def move_settings(yes: bool) -> dict:
    """Copy (never move) this project's settings that your own folder does not have into it, after the person's one yes."""
    import shutil
    rows = to_move()
    if not rows:
        return {"ok": True, "copied": [], "note": "nothing to copy: your own folder already has everything this project holds"}
    if not yes:
        return {"ok": False, "needs_yes": True, "would_copy": rows, "ask": "copy these settings from this project into your own folder, so every project finds them?"}
    me = gc.user_folder(create=True)
    done = []
    for r in rows:
        src = Path(r["from"])
        name = src.name
        if src.is_dir():
            for it in r["items"]:
                s_, d_ = src / it, (gc.user_brands_dir() if name == "brands" else me / name) / it
                d_.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if s_.is_dir() else shutil.copyfile)(s_, d_)
                done.append(str(d_))
        else:
            shutil.copyfile(src, me / name)
            done.append(str(me / name))
    return {"ok": True, "copied": done, "note": "the project's copies are left where they are; delete them yourself once you no longer need them there"}


def setup_report() -> dict:
    """4.0.0 (spec B7): what mcp/setup_env.py recorded on this computer: the state, the cause code and its fix line, and the logs' last lines.
    The setup files are on the computer the data server runs on: in a Cowork session that is the person's computer, so the skill asks the
    data server's setup_status tool, and this reads them only where it runs there itself (Claude Code, a terminal)."""
    sys.path.insert(0, str(gc.plugin_root() / "mcp"))
    import setup_env as se
    r = se.status(se.base_dir(), gc.plugin_root())
    r["where"] = "this computer: " + str(se.base_dir())
    if not Path(r["environment"]).parent.is_dir():
        r["note"] = ("No setup has run on this computer. In a Cowork session the data server runs on the person's computer: ask its "
                     "setup_status tool.")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --fix: install SOFT dependencies too")
    ap.add_argument("--quick", action="store_true", help="with --fix: everything a map needs (HARD, PDF/PNG, rasters) in one pip call, wheels first, "
                                                          "10 minutes at most; the first step in a fresh cloud workspace (1.2.0)")
    ap.add_argument("--profile", help="with --fix (2.4.0): install one or more profiles in one pip call: " + ", ".join(profiles()))
    ap.add_argument("--grids", metavar="REGION", help="3.0.0 (I-08): the PROJ datum grids for a region (Cascadia, US-WA, GB-SCT), with their size; --fix downloads them")
    ap.add_argument("--wheelhouse", metavar="DIR", help="3.0.0 (I-07): keep and reuse the installed wheels in DIR (off by default)")
    ap.add_argument("--place", help="2.4.0: the place this runs in, when the probe cannot tell (host-windows, host-macos, host-linux, cowork-vm, cloud)")
    ap.add_argument("--device-bridge", choices=["yes", "no", "unknown"], default="unknown")
    ap.add_argument("--mcp", choices=["yes", "no", "unknown"], default="unknown", help="whether a call to the plugin's MCP server succeeded in this session")
    ap.add_argument("--credentials-dir", help="the credential folder, when it is connected to this session")
    ap.add_argument("--fonts", help="1.5.0: fetch the pinned Noto fonts for these scripts into the font cache (arab,hebr,deva,beng,thai,hani,jpan,kore,...)")
    ap.add_argument("--settings", action="store_true", help="3.7.0: your own folder, your sources, your brand and default, an edited shipped brand")
    ap.add_argument("--move-settings", action="store_true", help="3.7.0: copy the settings found in this project into your own folder (with --yes)")
    ap.add_argument("--yes", action="store_true", help="with --move-settings: the person said yes")
    ap.add_argument("--setup", action="store_true", help="4.0.0: the data server's setup on this computer: the state, the cause code and its fix, "
                    "and the last lines of setup.log, launch.log and server.log (in Cowork ask the data server's setup_status tool instead)")
    a = ap.parse_args(argv)
    if a.setup:
        print(json.dumps(setup_report(), indent=2, default=str))
        return gc.EXIT_OK
    if a.settings or a.move_settings:
        r = move_settings(a.yes) if a.move_settings else settings_report()
        print(json.dumps(r, indent=2, default=str))
        return gc.EXIT_OK if r.get("ok", True) else gc.EXIT_USAGE
    if a.wheelhouse:
        import os
        os.environ["GMAP_WHEELHOUSE"] = a.wheelhouse
    if a.fix and a.place == "cowork-vm":            # 3.0.0 (I-12, owner decision O3 B): a Linux venv inside the work folder, kept across VM restarts
        print(json.dumps(make_vm_venv(Path.cwd()), indent=2))
        return gc.EXIT_OK
    if a.grids:                                     # I-08
        try:
            g = grids_for(a.grids)
        except Exception as ex:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}, indent=2))
            return gc.EXIT_USAGE
        if a.fix and g["grids"]:
            g["fixed"] = fetch_grids(g["grids"])
        elif g["grids"]:
            g["next"] = f"doctor.py --fix --grids \"{a.grids}\" downloads {len(g['grids'])} grid(s), {g['mb']} MB"
        print(json.dumps(g, indent=2))
        return gc.EXIT_OK
    if a.fonts:                                     # 1.5.0 (R3-SCRIPT): SHA-256 and the OFL text checked; MAP-LOC-004 for each one fetched
        import script_text as st
        res = st.fetch([k for k in a.fonts.split(",") if k.strip()])
        print(json.dumps(res, indent=2))
        return gc.EXIT_OK if all(r["ok"] for r in res) else gc.EXIT_SERVICE

    manifest = gc.build_manifest()
    editions = env_probe.installed_editions(manifest["plugin_name"])
    others = [e for e in editions if e != manifest["plugin_name"]]
    deps = check_deps()
    names = [n.strip() for n in (a.profile or "").split(",") if n.strip()]
    bad = [n for n in names if n not in profiles()]
    if bad:
        print(f"map-doctor: unknown profile {', '.join(bad)}; profiles: {', '.join(profiles())}", file=sys.stderr)
        return gc.EXIT_USAGE
    if a.fix and a.all and "all" not in names:
        names.append("all")
    if a.fix and a.quick and "map" not in names:
        names.insert(0, "map")
    fixed = (fix_profile(deps, names) if names else fix(deps, False)) if a.fix else []
    if fixed:
        deps = check_deps()
    place = env_probe.place(a.place)
    eg = env_probe.egress(a.offline)
    report = {
        "plugin": manifest["plugin_name"], "version": manifest.get("version"),
        "single_edition": {"ok": not others, "installed": editions},
        "dependencies": deps, "fixed": fixed, "outputs": check_tools(deps),
        "gdal_drivers": check_drivers(),
        "egress": {k: eg[k] for k in ("classification", "checked") if k in eg},
        "place": place, "place_label": env_probe.PLACE_LABEL.get(place, place),
        "tier": env_probe.classify_tier(a.device_bridge, eg, "unknown", place),
        "device_bridge": a.device_bridge,
        "assets": check_assets(), "brand_packs": check_packs(manifest), "library": check_library(), "release": check_release(a.offline),
        "credentials": check_credentials(a.mcp, a.credentials_dir),
        "engines": check_engines(),
        "server_launcher": check_server_launcher(),
        "projections": check_projections(),
    }
    if a.fix and ({"all", "db"} & set(names)) and report["engines"].get("duckdb", {}).get("installed") and not report["engines"]["duckdb"].get("spatial"):
        report["fixed"] = list(report["fixed"]) + [fix_spatial()]
        import duck_engine as _de
        _de.reset_state()                             # 2.4.0 (I-02): the re-check below loads the extension again, in this same process
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
    L = [env_probe.place_line(r.get("place") or "unknown", r["tier"], r["egress"].get("classification"), r.get("device_bridge", "unknown"),
                              plugin=f"{r['plugin']} {r['version']}")]
    if not r["single_edition"]["ok"]:
        L.append(f"  !! Two editions installed: {', '.join(r['single_edition']['installed'])}. Uninstall one.")
    L.append("Dependencies")
    for d in r["dependencies"]:
        mark = "ok     " if d["installed"] else ("TOO OLD" if d.get("too_old") else ("MISSING" if d["level"] == "HARD" else "missing"))
        prof = ("  [profile " + ", ".join(d.get("profiles") or ["all"]) + "]") if d["level"] == "SOFT" else ""
        L.append(f"  [{d['level']:4}] {mark} {d['name']:<12} {d['version'] or '-':<10} {d['why']}{prof}")
    if r.get("fixed"):                                # 2.4.0 (I-01): what --fix installed, in the text report too
        L.append("Fixed")
        for f in r["fixed"]:
            name = f.get("package") or f.get("name")
            ok = f.get("installed", f.get("ok"))
            how = f.get("route") or (f"{f['seconds']} s, {f.get('profile')}" if f.get("seconds") is not None else "")
            L.append(f"  {'ok    ' if ok else 'FAILED'} {name:<26} {how if ok else (f.get('error') or '')}")
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
    ts = [s for s in e.get("terrain_sources") or [] if s.get("status") == "active"]
    if ts:
        L.append("  terrain      " + "; ".join(f"{s['id']} ({s.get('coverage')}, {s.get('surface')})" for s in ts)
                 + f"; {sum(1 for s in e['terrain_sources'] if s.get('status') == 'candidate')} candidate(s) to verify")
    sr = e.get("service_route") or {}
    L.append(f"  data service {'ok' if sr.get('ok') else 'NOT AVAILABLE'}  {sr.get('note') or sr.get('error') or ''}")
    s = r["summary"]
    L.append(f"Result: {'ready' if s['ok'] else 'NOT ready'}; HARD missing: {', '.join(s['hard_missing']) or 'none'}; SOFT missing: {', '.join(s['soft_missing']) or 'none'}")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
