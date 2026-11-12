#!/usr/bin/env python3
"""geo_cache (0.15.0): one view of everything this plugin keeps on disk between runs, and the rules for keeping it.

The plugin's downloads are cached where they are made: library files by `ref_fetch` (.geo-mapping/cache/library), basemap tiles and
composites by `basemap_render` (tiles/, basemaps/), DEMs and terrain tiles by `terrain_fetch` (terrain/), and service captures by the service
tools (captures/). This module does not move them. It reads them as one content-addressed index (each file by its SHA-256), reports their size
and age, checks the hashes their records hold, finds the same content kept twice, and prunes what is past its time, honouring each tile
provider's cache terms: a provider whose terms forbid caching (`cache_ok: false`) has its tiles removed whatever their age, and one that asks for
a minimum (`min_cache_days`, OpenStreetMap's 7) is never pruned sooner.

Captures are evidence for a map's provenance, so they are never pruned unless asked by name (`--area captures --confirm`).

    geo_cache.py status   [--json]                                     every area: files, bytes, oldest, newest, past their time
    geo_cache.py prune    [--area A] [--older-than DAYS] [--dry-run]   remove files past their time (captures only with --confirm)
    geo_cache.py verify   [--area A]                                   recompute the SHA-256 each record names; report mismatches
    geo_cache.py duplicates                                            the same content kept in more than one place
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gmap_common as gc  # noqa: E402

DAY = 86400
TTL_DAYS = {"library": 120, "tiles": 30, "basemaps": 30, "terrain": 180, "captures": None}      # None: never pruned unless asked by name
RECORD_SUFFIXES = (".record.json", ".capture.json", ".json")


def areas() -> dict[str, Path]:
    """Where each kind of download lives (the same paths the tools write to)."""
    return {"library": gc.outputs_dir() / "cache" / "library", "tiles": gc.workdir() / "tiles", "basemaps": gc.workdir() / "basemaps",
            "terrain": gc.workdir() / "terrain", "captures": gc.outputs_dir() / "captures"}


def _files(root: Path):
    if not root.is_dir():
        return
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            if p.is_file() and not p.is_symlink():
                yield p


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def provider_terms() -> dict[str, dict]:
    """{tile-folder name: {cache_ok, min_cache_days}} from the library's basemap-provider entries (the folder is the provider id, slugged as
    basemap_render writes it)."""
    try:
        import ref_library as rl
        ents = [e for e in rl.all_entries("all") if e.get("type") == "basemap-provider"]
    except Exception:  # noqa: BLE001 - no library, no terms: the default rule applies
        return {}
    out = {}
    for e in ents:
        pr = e.get("provider") or {}
        name = re.sub(r"[^a-z0-9]+", "-", str(e.get("id", "")).split(".")[-1].lower())
        out[name] = {"cache_ok": pr.get("cache_ok"), "min_cache_days": pr.get("min_cache_days")}
    return out


def _ttl(area: str, path: Path, root: Path, terms: dict, older_than: float | None) -> tuple[float | None, float]:
    """(days after which the file may go, days before which it must stay) for one file."""
    keep_min = 0.0
    ttl = TTL_DAYS.get(area) if older_than is None else older_than
    if area == "tiles":
        try:
            prov = path.relative_to(root).parts[0]
        except (ValueError, IndexError):
            prov = ""
        t = terms.get(prov) or {}
        if t.get("cache_ok") is False:
            return 0.0, 0.0                       # the provider's terms forbid keeping its tiles
        if t.get("min_cache_days"):
            keep_min = float(t["min_cache_days"])
    return ttl, keep_min


def status(now: float | None = None) -> dict:
    now = now or time.time()
    terms = provider_terms()
    out = {}
    for name, root in areas().items():
        n = size = past = 0
        oldest = newest = None
        for p in _files(root):
            st = p.stat()
            n += 1
            size += st.st_size
            age = (now - st.st_mtime) / DAY
            oldest = age if oldest is None else max(oldest, age)
            newest = age if newest is None else min(newest, age)
            ttl, keep = _ttl(name, p, root, terms, None)
            if ttl is not None and age >= max(ttl, keep):
                past += 1
        out[name] = {"path": str(root), "files": n, "bytes": size, "oldest_days": round(oldest, 1) if oldest is not None else None,
                     "newest_days": round(newest, 1) if newest is not None else None, "past_their_time": past, "ttl_days": TTL_DAYS[name]}
    return {"ok": True, "areas": out, "total_bytes": sum(a["bytes"] for a in out.values())}


def prune(area: str | None = None, older_than: float | None = None, dry_run: bool = False, confirm: bool = False, now: float | None = None) -> dict:
    """Remove files past their time. Captures go only when named with confirm=True. Returns what was (or would be) removed."""
    now = now or time.time()
    terms = provider_terms()
    chosen = [area] if area else [a for a, t in TTL_DAYS.items() if t is not None]
    if "captures" in chosen and not confirm:
        return {"ok": False, "findings": [{"id": "MAP-SVC-008", "severity": "BLOCK", "message": "captures are the evidence behind a map's provenance; prune them only "
                                           "with --area captures --confirm, after the maps that use them are no longer needed", "detail": None}]}
    removed, kept_terms, freed = [], 0, 0
    for name in chosen:
        root = areas().get(name)
        if root is None:
            return {"ok": False, "findings": [{"id": "MAP-SVC-008", "severity": "BLOCK", "message": f"no cache area '{name}'; the areas are {', '.join(areas())}", "detail": None}]}
        for p in list(_files(root)):
            age = (now - p.stat().st_mtime) / DAY
            ttl, keep = _ttl(name, p, root, terms, older_than if name != "captures" else (older_than or 0))
            if ttl is None:
                continue
            if age < keep:
                kept_terms += 1
                continue
            if age >= ttl:
                freed += p.stat().st_size
                removed.append(str(p))
                if not dry_run:
                    p.unlink()
        if not dry_run:
            for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True) if root.is_dir() else []:
                try:
                    d.rmdir()                      # only empty folders go
                except OSError:
                    pass
    return {"ok": True, "dry_run": dry_run, "removed": len(removed), "bytes_freed": freed, "kept_for_provider_terms": kept_terms, "files": removed[:200]}


def _recorded_hashes(root: Path) -> dict[Path, str]:
    """{file: sha256} from the records beside cached files: a record names its file (path or result.path) and its sha256."""
    out = {}
    for rec in _files(root):
        if not rec.name.endswith(RECORD_SUFFIXES):
            continue
        try:
            d = json.loads(rec.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        res = d.get("result") if isinstance(d.get("result"), dict) else d
        path, h = res.get("path"), res.get("sha256")
        if isinstance(path, str) and isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h):
            p = Path(path)
            p = p if p.is_absolute() else rec.parent / p
            out[p] = h
    return out


def verify(area: str | None = None) -> dict:
    bad, checked, missing = [], 0, []
    for name, root in areas().items():
        if area and name != area:
            continue
        for p, h in _recorded_hashes(root).items():
            if not p.is_file():
                missing.append(str(p))
                continue
            checked += 1
            if sha256(p) != h:
                bad.append(str(p))
    fs = [{"id": "MAP-SVC-008", "severity": "WARN", "message": f"{len(bad)} cached file(s) no longer match the hash their record holds: fetch them again", "detail": bad[:20]}] if bad else []
    return {"ok": not bad, "checked": checked, "mismatched": bad, "missing": missing[:50], "findings": fs}


def duplicates() -> dict:
    by_size: dict[int, list[Path]] = {}
    for root in areas().values():
        for p in _files(root):
            by_size.setdefault(p.stat().st_size, []).append(p)
    groups = []
    for size, ps in by_size.items():
        if len(ps) < 2 or size == 0:
            continue
        by_hash: dict[str, list[str]] = {}
        for p in ps:
            by_hash.setdefault(sha256(p), []).append(str(p))
        groups += [{"sha256": h, "bytes": size, "paths": v} for h, v in by_hash.items() if len(v) > 1]
    return {"ok": True, "groups": groups, "bytes_reclaimable": sum(g["bytes"] * (len(g["paths"]) - 1) for g in groups)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=("status", "prune", "verify", "duplicates"))
    ap.add_argument("--area", choices=tuple(TTL_DAYS))
    ap.add_argument("--older-than", type=float)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.op == "status":
        r = status()
    elif a.op == "prune":
        r = prune(a.area, a.older_than, a.dry_run, a.confirm)
    elif a.op == "verify":
        r = verify(a.area)
    else:
        r = duplicates()
    print(json.dumps(r, indent=2, default=str))
    return gc.EXIT_OK if r.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
