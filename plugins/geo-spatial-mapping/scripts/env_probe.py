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


SYNC_TAIL = re.compile(r"(?:~[A-Za-z0-9]{1,8}| \(\d+\)|(?: -|[-_ ]) ?copy(?: \d+)?)$", re.I)


def edition_of(name: str) -> str:
    """The edition a folder or package name stands for, without its version: geo-spatial-mapping_v1.12.0 -> geo-spatial-mapping.
    1.13.0 (Day 12 issue 8): two versions of one edition side by side are one edition, not two (1.12 refused to run).
    1.19.0 (Day 17 issue A10): a synced or copied folder's marker is not an edition either: the cloud workspace's geo-spatial-mapping~g2
    (the plugin-files copy beside the synced one), "... (1)" and "... - Copy" are the same edition."""
    n = name[:-len(".plugin")] if name.endswith(".plugin") else name
    prev = None
    while prev != n:
        prev = n
        n = VERSION_TAIL.sub("", SYNC_TAIL.sub("", n))
    return n


def version_of(name: str) -> tuple | None:
    n = name[:-len(".plugin")] if name.endswith(".plugin") else name
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", n[len(edition_of(name)):] if edition_of(name) != n else "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


PLUGIN_MARKER = Path("scripts") / "gmap_common.py"


def _in_plugin_data(p: Path) -> bool:
    """1.21.0 (Day 18 issue 1): a folder under ~/.claude/plugins/data is a plugin's DATA folder (the host names it after the plugin, e.g.
    geo-spatial-mapping-synced), never an installed plugin."""
    parts = [x.lower() for x in p.parts]
    for i in range(len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "plugins" and parts[i + 2] == "data":
            return True
    return False


def is_plugin_copy(d: Path) -> bool:
    """1.21.0 (Day 18 issue 1): a geo-mapping-* folder counts as an installed edition only when it holds the plugin's code: scripts/gmap_common.py
    in the folder itself or one level down (a cache's <name>/<version>/ layout). A data, output or working folder named after the plugin is not one."""
    if _in_plugin_data(d):
        return False
    try:
        if (d / PLUGIN_MARKER).is_file():
            return True
        return any((c / PLUGIN_MARKER).is_file() for c in d.iterdir() if c.is_dir())
    except OSError:
        return False


def manifest_name(d: Path) -> str | None:
    """1.22.0: the name a plugin folder gives itself in .claude-plugin/plugin.json (in the folder, or one level down in a cache's
    <name>/<version>/ layout). A folder's own name says where a host put the copy, not which plugin it is: a synced, copied or renamed folder
    (~g2 on Day 17, -synced on Day 18) is the same edition when its manifest says so, whatever marker the next host adds."""
    try:
        places = [d] + sorted(c for c in d.iterdir() if c.is_dir())[:8]
    except OSError:
        places = [d]
    for c in places:
        f = c / ".claude-plugin" / "plugin.json"
        try:
            if f.is_file() and f.stat().st_size < 65536:
                name = json.loads(f.read_text(encoding="utf-8")).get("name")
                if isinstance(name, str) and gc.is_edition_name(name):
                    return name
        except (OSError, ValueError, AttributeError):
            continue
    return None


def installed_editions(own: str) -> list[str]:
    """Editions of geo-mapping-* directories next to this plugin: the name in each copy's own manifest (1.22.0), or the folder's name without
    a version where a copy has no manifest. Only that one small file is read. 1.21.0 (Day 18 issue 1): the plugins' data folders and folders without the plugin's code are
    skipped (the synced data folder geo-spatial-mapping-synced stopped every map skill in a cloud session)."""
    names = {own}
    for d in scan_dirs():
        if _in_plugin_data(d):
            continue
        try:
            for child in d.iterdir():
                if gc.is_edition_name(child.name) and child.is_dir() and is_plugin_copy(child):
                    names.add(manifest_name(child) or edition_of(child.name))      # 1.22.0: the manifest's name first, the folder's as the fallback
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


PLACES = ("host-windows", "host-macos", "host-linux", "cowork-vm", "cloud")
PLACE_LABEL = {"host-windows": "your Windows computer", "host-macos": "your Mac", "host-linux": "your Linux computer",
               "cowork-vm": "the Cowork VM on your computer", "cloud": "cloud workspace", "unknown": "an unrecognised place"}


def place(override: str | None = None) -> str:
    """2.4.0 (I-10): where this process runs. `cloud` is the Claude cloud workspace (cloud_workspace()); `cowork-vm` is the Linux VM the
    desktop app runs for a session (connected folders under $HOME/mnt/, no /mnt/user-data); otherwise the person's own computer by its
    operating system. GMAP_PLACE or `override` wins (a test, or a place the probe cannot see)."""
    want = (override or os.environ.get("GMAP_PLACE") or "").strip().lower()
    if want in PLACES:
        return want
    if cloud_workspace():
        return "cloud"
    sysname = platform.system()
    if sysname == "Linux":
        mnt = Path.home() / "mnt"
        try:
            if mnt.is_dir() and any(c.is_dir() for c in mnt.iterdir()) and not Path("/mnt/user-data").exists():
                return "cowork-vm"
        except OSError:
            pass
        return "host-linux"
    return {"Windows": "host-windows", "Darwin": "host-macos"}.get(sysname, "unknown")


def is_host(pl: str) -> bool:
    return pl.startswith("host-")


def classify_tier(device_bridge: str, eg: dict, web_tools: str, pl: str | None = None) -> str:
    """The capability level. T2 (rendering on the person's computer) only when this process IS on that computer: a linked computer seen
    from the cloud workspace or the Cowork VM is a place for data tools, not where this build renders (2.4.0, I-10; Day 20 issue 22)."""
    pl = pl or place()
    if device_bridge == "yes" and is_host(pl):
        return "T2"
    if eg["classification"] in ("open", "allowlist") or web_tools == "yes":
        return "T1"
    return "T0"


def place_line(pl: str, tier: str, egress_cls: str | None, device_bridge: str = "unknown", plugin: str = "") -> str:
    """The doctor's first line (2.4.0, I-10): 'cloud workspace, tier T1 (shell egress open); linked computer reachable for data tools'."""
    head = f"{plugin}: " if plugin else ""
    txt = f"{head}{PLACE_LABEL.get(pl, pl)}, tier {tier} (shell egress {egress_cls or 'not checked'})"
    if device_bridge == "yes" and not is_host(pl):
        txt += "; linked computer reachable for data tools"
    elif device_bridge == "no" and not is_host(pl):
        txt += "; no linked computer"
    return txt


def status_line(env: dict) -> str:
    eg = env["egress"]
    tier = env["tier"]
    parts = {
        "T0": "vector basemaps only, bundled data and uploads",
        "T1": "basemap data staged or fetched into the workspace",
        "T2": "rendering on your computer, direct tile and service access",
    }[tier]
    pl = env.get("place")
    if pl and not is_host(pl):
        parts += f"; running in the {PLACE_LABEL.get(pl, pl)}"
    egress_txt = {"open": "shell egress open", "allowlist": "shell egress limited to an allowlist",
                  "closed": "shell egress closed", "unknown": "shell egress not checked"}[eg["classification"]]
    return f"Tier {tier}: {parts} ({egress_txt}; GDAL {env['gdal']['version'] or 'missing'})."


def cloud_workspace() -> bool:
    """A Claude cloud workspace (a Linux container with the /mnt/user-data folders, or CLAUDE_CODE_REMOTE set), not the person's computer."""
    return platform.system() == "Linux" and (os.environ.get("CLAUDE_CODE_REMOTE") == "true" or Path("/mnt/user-data").is_dir())


def auto_install() -> list[dict]:
    """1.19.0 (Day 16 issue 7, Day 17 issue A11): a fresh cloud workspace has no map packages, and every build began with a failed import and a
    hand-run `doctor.py --fix --quick`. The build's first step now installs them itself (the same one pip call, wheels only, 10 minutes at
    most) when geopandas is missing; GMAP_NO_AUTO_INSTALL=1 turns this off. Messages go to stderr so `--value` still prints the token alone."""
    import importlib.util
    if os.environ.get("GMAP_NO_AUTO_INSTALL") == "1" or importlib.util.find_spec("geopandas") is not None:
        return []
    if not cloud_workspace():                     # on the person's own computer the skill asks before installing; nothing is installed here
        return []
    try:
        import doctor
        print("map-env-probe: the map packages are not installed in this workspace; installing them now (one pip step)", file=sys.stderr, flush=True)
        log = doctor.fix_quick(doctor.check_deps())
    except Exception as ex:  # noqa: BLE001 - the build then stops on its own import and names doctor.py
        print(f"map-env-probe: the automatic install did not run ({type(ex).__name__}); run doctor.py --fix --quick", file=sys.stderr)
        return []
    bad = [r["package"] for r in log if not r.get("installed")]
    print("map-env-probe: " + (f"could not install {', '.join(bad)}; run doctor.py --fix --quick to see why" if bad else f"installed {len(log)} package(s)"),
          file=sys.stderr, flush=True)
    return log


def needed_profiles(spec: dict) -> list[str]:
    """2.6.0 (I-06): the doctor profiles a MapSpec's features need: h3 for an hexbin or h3 source, raster for relief, a raster or contours,
    db for a large GeoParquet `where`, web for a page."""
    out: list[str] = ["map"]
    layers = spec.get("layers") or []
    if any((ly.get("source") or {}).get("type") == "h3" or ly.get("role") in ("hexbin", "heat") for ly in layers):
        out.append("h3")
    if (spec.get("terrain") or {}).get("on") or any(ly.get("role") in ("raster", "hillshade", "contour") for ly in layers):
        out.append("raster")
    if any(str((ly.get("source") or {}).get("path") or "").lower().endswith((".parquet", ".geoparquet")) and (ly.get("source") or {}).get("where") for ly in layers):
        out.append("db")
    return list(dict.fromkeys(out))


def install_for_spec(spec_path: str) -> list[dict]:
    """In a cloud workspace only: every missing package of the profiles the spec needs, in one pip call (doctor --fix --profile). On the
    person's own computer nothing is installed here (the skill asks first)."""
    if os.environ.get("GMAP_NO_AUTO_INSTALL") == "1" or not cloud_workspace():
        return []
    try:
        import doctor
        spec = gc.load_mapspec(Path(spec_path))
        names = needed_profiles(spec)
        rows = doctor.check_deps()
        want = set()
        for n in names:
            want |= set(doctor.profile_names(n))
        if not any(not r["installed"] for r in rows if r["name"] in want):
            return []
        print(f"map-env-probe: installing the {', '.join(names)} profile(s) this MapSpec needs (one pip step)", file=sys.stderr, flush=True)
        return doctor.fix_profile(rows, names)
    except Exception as ex:  # noqa: BLE001 - the build then names what is missing
        print(f"map-env-probe: the profile install did not run ({type(ex).__name__})", file=sys.stderr)
        return []


def begin_build(spec: str) -> str:
    wd = gc.workdir(create=True) / "run" / "tokens"
    wd.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    for old in wd.glob("*.json"):  # prune expired
        try:
            if json.loads(old.read_text(encoding="utf-8"))["expires"] < iso(now):
                old.unlink()
        except Exception:  # noqa: BLE001
            old.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    (wd / f"{token}.json").write_text(json.dumps({
        "token": token, "spec": str(Path(spec).resolve()), "created": iso(now),
        "expires": iso(now + dt.timedelta(hours=TOKEN_TTL_HOURS)), "plugin": gc.build_manifest()["plugin_name"],
    }, indent=2), encoding="utf-8")
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
        prev = [o for o in others if edition_of(o) in gc.PREVIOUS_NAMES]
        if prev:                                   # 4.0.0: the plugin was renamed; the old one left installed has every skill and tool again
            print(f"{', '.join(prev)} is this plugin's previous name and is still installed beside {own}. Remove {', '.join(prev)} "
                  "(Settings, Plugins in the Claude app; or /plugin uninstall), then restart Claude. Your keys, sources, brand and default "
                  "brand are kept in your own folder and are not affected.", file=sys.stderr)
        else:
            print(f"Two geo-mapping editions are installed: {own} and {', '.join(others)}. "
                  f"Only one may be installed at a time; uninstall one and try again.", file=sys.stderr)
        if not a.no_write:
            try:
                wd = gc.workdir(create=True)
                guard = {"id": "MAP-BRAND-012", "severity": "BLOCK", "message": (f"{', '.join(prev)} is this plugin's previous name: remove it, then restart Claude"
                         if prev else f"two geo-mapping editions are installed ({', '.join(editions)}); uninstall one")}
                (wd / "env.json").write_text(json.dumps({"single_edition_ok": False, "editions_installed": editions, "findings": [guard]}, indent=2), encoding="utf-8")
            except OSError:
                pass
        return gc.EXIT_USAGE

    if a.begin_build:
        if not a.spec:
            print("--begin-build needs --spec", file=sys.stderr)
            return gc.EXIT_USAGE
        auto_install()
        install_for_spec(a.spec)                   # 2.6.0 (I-06): what this spec's features need, in a cloud workspace
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
    env["place"] = place()
    env["tier"] = classify_tier(a.device_bridge, eg, a.web_tools, env["place"])
    env["newer_copies"] = newer_copies(own, manifest.get("version"))
    env["status_line"] = status_line(env) + (" " + newer_line(own, manifest.get("version")) if env["newer_copies"] else "")
    if not a.no_write:
        wd = gc.workdir(create=True)
        (wd / "env.json").write_text(json.dumps(env, indent=2), encoding="utf-8")
    print(json.dumps(env, indent=2) if a.json else env["status_line"])
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
