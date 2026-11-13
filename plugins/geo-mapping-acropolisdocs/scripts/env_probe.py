#!/usr/bin/env python3
"""map-env-probe: classify the session into a capability tier and guard against two editions.

    env_probe.py [--offline] [--json] [--device-bridge yes|no|unknown] [--web-tools yes|no|unknown]
    env_probe.py --begin-build --spec map.yaml        # prints GMAP_BUILD_TOKEN=... for map_brand
    env_probe.py --begin-build --spec map.yaml --value    # 1.13.0: the token alone: export GMAP_BUILD_TOKEN=$(... --value)

Writes env.json into the working directory. The device bridge and web tools are properties of the
model's tool list, not of the shell, so the calling skill passes them in.

Single-edition guard (spec 11.5): lists sibling plugin DIRECTORY NAMES that match geo-mapping-*.
No file inside a sibling is opened. If another edition is present the exit code is 2.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fnmatch
import json
import os
import platform
import re
import ssl
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

TOKEN_TTL_HOURS = 8


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def package_versions() -> dict:
    import importlib.metadata as md
    deps = gc.load_yaml(gc.plugin_root() / "data" / "dependencies.yaml")
    out = {}
    for d in deps:
        try:
            out[d["name"]] = md.version(d["pip"])
        except md.PackageNotFoundError:
            out[d["name"]] = None
    return out


def gdal_info() -> dict:
    try:
        import pyogrio
    except Exception:
        return {"available": False, "version": None, "drivers": {}}
    drivers = pyogrio.list_drivers()
    return {"available": True, "version": pyogrio.__gdal_version_string__, "drivers": drivers}


def probe_host(entry: dict, timeout: float) -> dict:
    t0 = dt.datetime.now()
    req = urllib.request.Request(entry["url"], method="HEAD", headers={"User-Agent": "geo-mapping-env-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            status = r.status
    except urllib.error.HTTPError as e:  # the host answered; any HTTP status means reachable
        status = e.code
    except Exception as e:  # noqa: BLE001 - classification only
        return {"host": entry["host"], "class": entry["class"], "reachable": False, "error": type(e).__name__}
    ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
    # 403 from a proxy for a blocked host is a refusal, not reachability
    reachable = status not in (407,) and not (status == 403 and os.environ.get("HTTPS_PROXY") and entry["class"] != "registry")
    return {"host": entry["host"], "class": entry["class"], "reachable": reachable, "status": status, "ms": ms}


def egress(offline: bool, timeout: float = 5.0) -> dict:
    if offline:
        return {"classification": "unknown", "checked": False, "results": []}
    hosts = gc.load_yaml(gc.plugin_root() / "data" / "probe-hosts.yaml")
    with cf.ThreadPoolExecutor(max_workers=len(hosts)) as ex:
        results = list(ex.map(lambda h: probe_host(h, timeout), hosts))
    ok = [r for r in results if r["reachable"]]
    non_registry_ok = [r for r in ok if r["class"] != "registry"]
    if not ok:
        cls = "closed"
    elif len(ok) == len(results) or len(non_registry_ok) >= max(1, (len(results) - 2) * 3 // 4):
        cls = "open"
    else:
        cls = "allowlist"
    return {"classification": cls, "checked": True, "reachable": len(ok), "total": len(results), "results": results}


def scan_dirs() -> list[Path]:
    dirs = [gc.plugin_root().parent]
    home = Path.home() / ".claude" / "plugins"
    if home.is_dir():
        dirs.append(home)
        for a in home.iterdir():
            if a.is_dir():
                dirs.append(a)
                for b in a.iterdir():
                    if b.is_dir():
                        dirs.append(b)
    for extra in os.environ.get("GEO_MAPPING_PLUGIN_DIRS", "").split(os.pathsep):
        if extra:
            dirs.append(Path(extra))
    return dirs


VERSION_TAIL = re.compile(r"[-_.]v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.]+)?$")
COPY_SEARCH_DEPTH = 4


def edition_of(name: str) -> str:
    """The edition a folder or package name stands for, without its version: geo-mapping-acropolisdocs_v1.12.0 -> geo-mapping-acropolisdocs.
    1.13.0 (Day 12 issue 8): two versions of one edition side by side are one edition, not two (1.12 refused to run)."""
    n = name[:-len(".plugin")] if name.endswith(".plugin") else name
    return VERSION_TAIL.sub("", n)


def version_of(name: str) -> tuple | None:
    n = name[:-len(".plugin")] if name.endswith(".plugin") else name
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", n[len(edition_of(name)):] if edition_of(name) != n else "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def installed_editions(own: str) -> list[str]:
    """Editions of geo-mapping-* directories next to this plugin, by name without a version. Directory names only; nothing is read."""
    names = {own}
    for d in scan_dirs():
        try:
            for child in d.iterdir():
                if fnmatch.fnmatch(child.name, gc.EDITION_GLOB) and child.is_dir():
                    names.add(edition_of(child.name))
        except OSError:
            continue
    return sorted(names)


def copy_search_dirs() -> list[Path]:
    """Where another copy of this plugin may sit in a session: beside it, the working folder and its parents, and a cloud session's uploads
    (a package staged from the person's computer)."""
    out = list(scan_dirs())
    cwd = Path.cwd()
    out += [cwd] + list(cwd.parents)[:3]
    for root in (Path("/mnt/user-data/uploads"), Path("/mnt/user-data/outputs")):
        if root.is_dir():
            out.append(root)
    return out


def newer_copies(own: str, version: str | None) -> list[dict]:
    """1.13.0 (Day 12 issue 9): copies of this edition, as a folder or a .plugin package, with a higher version than the one running, newest
    first: [{path, version}]. Names only, never opened. A session whose synced copy is older than the released package learns it here."""
    mine = version_of(f"x-{version}") if version else None
    if not mine:
        return []
    found: dict[str, dict] = {}
    seen: set = set()

    def walk(d: Path, depth: int):
        if depth < 0 or d in seen:
            return
        seen.add(d)
        try:
            kids = list(d.iterdir())
        except OSError:
            return
        for c in kids:
            if edition_of(c.name) == own and c.name != own:
                v = version_of(c.name)
                if v and v > mine:
                    found[str(c)] = {"path": str(c), "version": ".".join(map(str, v))}
            elif c.is_dir() and not c.name.startswith(".") and c.name not in ("node_modules", "__pycache__", ".venv", "venv") and depth > 0:
                walk(c, depth - 1)
    for d in copy_search_dirs():
        walk(d, 1 if d == Path.home() or d in Path.cwd().parents else COPY_SEARCH_DEPTH)
    return sorted(found.values(), key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)


def newer_line(own: str, version: str | None) -> str | None:
    nc = newer_copies(own, version)
    if not nc:
        return None
    return (f"A newer copy of this plugin ({nc[0]['version']}) is at {nc[0]['path']}; this session runs {version}. "
            "Install it (or unpack it and run its scripts) to use its fixes.")


def classify_tier(device_bridge: str, eg: dict, web_tools: str) -> str:
    if device_bridge == "yes":
        return "T2"
    if eg["classification"] in ("open", "allowlist") or web_tools == "yes":
        return "T1"
    return "T0"


def status_line(env: dict) -> str:
    eg = env["egress"]
    tier = env["tier"]
    parts = {
        "T0": "vector basemaps only, bundled data and uploads",
        "T1": "basemap data staged or fetched into the workspace",
        "T2": "rendering on your computer, direct tile and service access",
    }[tier]
    egress_txt = {"open": "shell egress open", "allowlist": "shell egress limited to an allowlist",
                  "closed": "shell egress closed", "unknown": "shell egress not checked"}[eg["classification"]]
    return f"Tier {tier}: {parts} ({egress_txt}; GDAL {env['gdal']['version'] or 'missing'})."


def begin_build(spec: str) -> str:
    wd = gc.workdir(create=True) / "run" / "tokens"
    wd.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    for old in wd.glob("*.json"):  # prune expired
        try:
            if json.loads(old.read_text())["expires"] < iso(now):
                old.unlink()
        except Exception:  # noqa: BLE001
            old.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    (wd / f"{token}.json").write_text(json.dumps({
        "token": token, "spec": str(Path(spec).resolve()), "created": iso(now),
        "expires": iso(now + dt.timedelta(hours=TOKEN_TTL_HOURS)), "plugin": gc.build_manifest()["plugin_name"],
    }, indent=2))
    return token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="skip network probes")
    ap.add_argument("--json", action="store_true", help="print env.json to stdout")
    ap.add_argument("--device-bridge", choices=["yes", "no", "unknown"], default="unknown")
    ap.add_argument("--web-tools", choices=["yes", "no", "unknown"], default="unknown")
    ap.add_argument("--begin-build", action="store_true")
    ap.add_argument("--spec")
    ap.add_argument("--value", action="store_true", help="with --begin-build: print the token alone (for export GMAP_BUILD_TOKEN=$(...))")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    try:
        manifest = gc.build_manifest()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_RUNTIME

    own = manifest["plugin_name"]
    editions = installed_editions(own)
    others = [e for e in editions if e != own]
    if others:
        print(f"Two geo-mapping editions are installed: {own} and {', '.join(others)}. "
              f"Only one may be installed at a time; uninstall one and try again.", file=sys.stderr)
        if not a.no_write:
            try:
                wd = gc.workdir(create=True)
                guard = {"id": "MAP-BRAND-012", "severity": "BLOCK", "message": f"two geo-mapping editions are installed ({', '.join(editions)}); uninstall one"}
                (wd / "env.json").write_text(json.dumps({"single_edition_ok": False, "editions_installed": editions, "findings": [guard]}, indent=2))
            except OSError:
                pass
        return gc.EXIT_USAGE

    if a.begin_build:
        if not a.spec:
            print("--begin-build needs --spec", file=sys.stderr)
            return gc.EXIT_USAGE
        tok = begin_build(a.spec)
        print(tok if a.value else f"GMAP_BUILD_TOKEN={tok}")
        nl = newer_line(own, manifest.get("version"))
        if nl:
            print(nl, file=sys.stderr)
        return gc.EXIT_OK

    eg = egress(a.offline)
    env = {
        "schema_version": 1,
        "generated_at": iso(utcnow()),
        "plugin": {"name": own, "version": manifest.get("version"), "brand_selection": manifest.get("brand_selection", "selectable"),
                   "brands_bundled": manifest["brands_bundled"], "default_content_brand": manifest["default_content_brand"]},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "packages": package_versions(),
        "gdal": gdal_info(),
        "egress": eg,
        "device_bridge": a.device_bridge,
        "web_tools": a.web_tools,
        "editions_installed": editions,
        "single_edition_ok": True,
        "workdir": str(gc.workdir()),
        "workdir_persistence": "unverified",  # decided by spike P6 (Appendix H); see risk 36
    }
    env["tier"] = classify_tier(a.device_bridge, eg, a.web_tools)
    env["newer_copies"] = newer_copies(own, manifest.get("version"))
    env["status_line"] = status_line(env) + (" " + newer_line(own, manifest.get("version")) if env["newer_copies"] else "")
    if not a.no_write:
        wd = gc.workdir(create=True)
        (wd / "env.json").write_text(json.dumps(env, indent=2))
    print(json.dumps(env, indent=2) if a.json else env["status_line"])
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
