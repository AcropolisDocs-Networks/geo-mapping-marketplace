#!/usr/bin/env python3
"""release_check: is a newer release of this plugin out? (1.16.0, Day 14 issue 12)

    release_check.py [--refresh] [--json]

Day 14 was built on a cloud-synced copy at 1.13.0 while 1.14.1 was released, and nothing said so. This asks where the plugin's releases are
published (data/release-channel.yaml) for the newest version, at most once a day (the answer is kept in ~/.geo-mapping/release-check.json),
and says so in the map-doctor report and in each build's notes. It never installs or changes anything, sends nothing but the request, and
stays quiet when the network or the host is out of reach. GEO_MAPPING_RELEASE_CHECK=off (or `check: off` in the channel file) turns it off.

Routes, in order: the GitHub API's latest release, the releases page's redirect to its tag, `git ls-remote --tags`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def channel() -> dict:
    p = gc.plugin_root() / "data" / "release-channel.yaml"
    return gc.load_yaml(p) if p.is_file() else {}


def parse(v) -> tuple[int, int, int] | None:
    m = SEMVER.search(str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def installed() -> str | None:
    try:
        return gc.build_manifest().get("version")
    except FileNotFoundError:
        p = gc.plugin_root().parent / "VERSION"
        return p.read_text(encoding="utf-8").strip() if p.is_file() else None


def _name() -> str:
    try:
        return gc.build_manifest().get("plugin_name") or "the plugin"
    except FileNotFoundError:
        return "geo-mapping-acropolisdocs"


def _cache_path() -> Path:
    base = os.environ.get("GEO_MAPPING_HOME")
    return (Path(base) if base else Path(os.path.expanduser("~")) / gc.WORKDIR_NAME) / "release-check.json"


def latest(repo: str, prefix: str = "v", *, http=None, git=True) -> tuple[str | None, str]:
    """The newest released version and the route that said so (None, why) when none answered."""
    import ogc_http
    h = http or ogc_http.SafeHTTP(limits={"timeout_s": 6, "max_bytes": 2_000_000, "retries": 0})
    tried = []
    try:
        r = h.get(f"https://api.github.com/repos/{repo}/releases/latest")
        if r.status == 200:
            v = parse(json.loads(r.content).get("tag_name"))
            if v:
                return ".".join(map(str, v)), "api"
        tried.append(f"api HTTP {r.status}")
    except Exception as ex:  # noqa: BLE001
        tried.append(f"api {type(ex).__name__}")
    try:
        r = h.get(f"https://github.com/{repo}/releases/latest")
        url = getattr(r, "url", "") or ""
        v = parse(url.rsplit("/tag/", 1)[-1]) if "/tag/" in url else None
        if v:
            return ".".join(map(str, v)), "releases-page"
        tried.append(f"page HTTP {r.status}")
    except Exception as ex:  # noqa: BLE001
        tried.append(f"page {type(ex).__name__}")
    if git:
        try:
            p = subprocess.run(["git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo}.git"], capture_output=True, text=True, timeout=20)
            vs = [parse(line.rsplit("refs/tags/", 1)[-1]) for line in p.stdout.splitlines() if f"refs/tags/{prefix}" in line]
            vs = [v for v in vs if v]
            if vs:
                return ".".join(map(str, max(vs))), "git"
            tried.append("git: no tags")
        except Exception as ex:  # noqa: BLE001
            tried.append(f"git {type(ex).__name__}")
    return None, "; ".join(tried)


def check(*, refresh: bool = False, http=None, now=None, git=True, cache_only: bool = False) -> dict:
    """{installed, latest, newer, route, cached, note}: note is the line a report shows when a newer release is out, else None. `cache_only`
    (a map build): read the day's answer if there is one and never touch the network."""
    ch = channel()
    mine = installed()
    out = {"installed": mine, "latest": None, "newer": False, "route": None, "cached": False, "note": None}
    if str(os.environ.get("GEO_MAPPING_RELEASE_CHECK", "")).lower() in ("off", "0", "no") or str(ch.get("check", "on")).lower() == "off" or not ch.get("repo"):
        out["route"] = "off"
        return out
    now = now if now is not None else time.time()
    cp = _cache_path()
    rec = None
    if not refresh and cp.is_file():
        try:
            rec = json.loads(cp.read_text(encoding="utf-8"))
            if rec.get("repo") != ch["repo"] or now - float(rec.get("at", 0)) > float(ch.get("cache_hours", 24)) * 3600:
                rec = None
        except (OSError, ValueError):
            rec = None
    if rec is None and (cache_only or os.environ.get("GMAP_OFFLINE") == "1"):
        out["route"] = "not checked today"
        return out
    if rec is None:
        v, route = latest(ch["repo"], ch.get("tag_prefix", "v"), http=http, git=git)
        rec = {"repo": ch["repo"], "latest": v, "route": route, "at": now}
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(rec), encoding="utf-8")
        except OSError:
            pass
    else:
        out["cached"] = True
    out["latest"], out["route"] = rec.get("latest"), rec.get("route")
    a, b = parse(mine), parse(rec.get("latest"))
    if a and b and b > a:
        out["newer"] = True
        out["note"] = (f"a newer release of this plugin is out: {rec['latest']} (this is {mine}); update it from the marketplace, or install "
                       f"{_name()}_v{rec['latest']}.plugin from github.com/{ch['repo']}/releases")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = check(refresh=a.refresh)
    print(json.dumps(r, indent=2) if a.json else (r["note"] or f"geo-mapping-acropolisdocs {r['installed']} is the newest release ({r['latest'] or 'unknown'})"))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
