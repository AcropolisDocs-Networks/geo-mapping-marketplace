#!/usr/bin/env python3
"""themes: what a thing is, and every library entry that serves it (4.2.0; library/themes.yaml).

    themes.py list                                  the themes and how many sources each has
    themes.py find WORDS...                         the themes a phrase names ("cell towers", "night lights")
    themes.py show THEME [--geography us-az] [--audience external] [--format json|text]
                                                    every candidate for a theme, in the order to consider them, with its licence, how it is
                                                    delivered, the key it needs and where it can be fetched from
    themes.py check                                 active data entries in no theme, and themes with fewer than two sources

A request for "cell sites" used to find the one entry with those words in its title. `show` lists every source the library holds for the
theme, so the one taken is chosen with the others in view; `compare: true` on a theme says to count each candidate in the map's window
first. Standard library and PyYAML only: it reads files on this computer and reaches no network.
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))

THEMES_FILE = HERE.parent / "library" / "themes.yaml"
HOST_MODES = {"service": "cloud or computer", "fetch": "cloud or computer", "bundled": "here (shipped with the plugin)", "user-supplied": "a file you bring"}
CLOUD_BLOCKED = ("overpass", "ohsome", "nominatim")       # hosts the cloud workspace's egress does not reach (execution-routes.yaml)


def load(path: Path | None = None) -> dict:
    import yaml
    return yaml.safe_load(Path(path or THEMES_FILE).read_text(encoding="utf-8")) or {}


def _entries(scope: str = "all") -> list[dict]:
    import ref_library as rl
    return [e for e in rl.all_entries(scope) if str(e.get("id", "")).startswith("ref.")]


def is_data(e: dict) -> bool:
    return str(e.get("type") or "") not in ("map-type", "template", "palette", "symbol-set") and not re.match(r"^ref\.(map-type|template|palette|symbol-set|style)\.", str(e.get("id")))


def members(theme: dict, entries: list[dict]) -> list[dict]:
    """The theme's entries in its own order (a pattern's matches by id), each once."""
    out, seen = [], set()
    by_id = {str(e["id"])[4:]: e for e in entries}
    for pat in theme.get("entries") or []:
        hits = [pat] if pat in by_id else sorted(i for i in by_id if fnmatch.fnmatchcase(i, pat))
        for i in hits:
            if i not in seen:
                seen.add(i)
                out.append(by_id[i])
    return out


def find(words: str, doc: dict | None = None) -> list[tuple[str, int]]:
    """[(theme, score)] for a phrase: a whole theme word or phrase found in it scores by its length, so "cell towers" beats "towers"."""
    doc = doc or load()
    q = " " + re.sub(r"[^a-z0-9#]+", " ", str(words).lower()).strip() + " "
    out = []
    for name, th in (doc.get("themes") or {}).items():
        best = 0
        for w in [name.replace("-", " "), *(th.get("words") or [])]:
            w_ = " " + str(w).lower().strip() + " "
            if w_ in q or (w_.rstrip() + "s ") in q:
                best = max(best, len(w_.strip()))
        if best:
            out.append((name, best))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def _covers(e: dict, geography: str | None) -> bool:
    if not geography:
        return True
    geos = [str(g) for g in ((e.get("tags") or {}).get("geography") or [])]
    try:
        import ref_select as rs
        return rs.geo_cover(geos, geography.lower(), _regions()) is not None
    except Exception:  # noqa: BLE001 - without the vocabulary, a plain prefix test
        g = geography.lower()
        return any(x == "world" or g == x or g.startswith(x + "-") or x in ("north-america",) for x in geos)


@functools.lru_cache(maxsize=1)
def _regions() -> dict:
    import library_lint as ll
    import ref_library as rl
    import ref_select as rs
    tags, _types = ll.load_vocab(rl.shipped_lib())
    return rs.regions(tags)


def candidate(e: dict, audience: str | None = None) -> dict:
    dl = e.get("delivery") or {}
    lic = e.get("license") or {}
    cred = dl.get("requires_credential") or e.get("requires_credential") or {}
    mode = str(dl.get("mode") or "-")
    eid = str(e.get("id"))
    where = HOST_MODES.get(mode, "-")
    if cred:
        where = "the person's computer (it holds the key)"
    elif any(h in eid for h in CLOUD_BLOCKED):
        where = "the person's computer (the cloud workspace cannot reach it)"
    ok = True
    why = None
    asks = False
    try:                                                   # 5.2.0: a use the person acknowledged for their own licence opens the source for them
        import licence_ack as la
        if la.required(e) and not la.required(e).get("per_variant"):
            asks = True
            lic = la.effective(lic, la.current(eid))
    except Exception:  # noqa: BLE001
        pass
    if audience == "external" and lic.get("external_ok") is False:
        ok, why = False, ("its terms keep it to internal maps unless the person says theirs cover publication (the licence pop-up, licence_acknowledge)" if asks
                          else "its licence keeps it to internal maps")
    return {"id": eid, "title": e.get("title"), "delivery": mode, "license": lic.get("class"), "key": (cred.get("name") if isinstance(cred, dict) else None),
            "fetch_from": where, "needs_computer": where.startswith("the person's computer"), "brings_file": mode == "user-supplied", "audience_ok": ok, "audience_note": why, "status": e.get("status"),
            "geography": (e.get("tags") or {}).get("geography") or [], "scale": (e.get("tags") or {}).get("scale") or []}


def show(name: str, geography: str | None = None, audience: str | None = None, doc: dict | None = None, entries: list[dict] | None = None) -> dict:
    doc = doc or load()
    th = (doc.get("themes") or {}).get(name)
    if th is None:
        raise KeyError(name)
    ents = entries if entries is not None else _entries()
    mem = [e for e in members(th, ents) if e.get("status") == "active"]
    here = [candidate(e, audience) for e in mem if _covers(e, geography)]
    return {"theme": name, "says": th.get("says"), "compare": bool(th.get("compare")), "note": th.get("note"), "gap": th.get("gap"),
            "geography": geography, "candidates": here, "elsewhere": len(mem) - len(here)}


def check(doc: dict | None = None, entries: list[dict] | None = None) -> dict:
    doc = doc or load()
    ents = [e for e in (entries if entries is not None else _entries("shipped")) if e.get("status") == "active" and is_data(e)]
    covered: set[str] = set()
    thin, empty = [], []
    for name, th in (doc.get("themes") or {}).items():
        mem = [e for e in members(th, ents)]
        covered |= {str(e["id"]) for e in mem}
        if not mem:
            empty.append(name)
        elif len(mem) < 2:
            thin.append(name)
    missing = sorted(str(e["id"]) for e in ents if str(e["id"]) not in covered)
    return {"themes": len(doc.get("themes") or {}), "entries": len(ents), "in_no_theme": missing, "one_source": sorted(thin), "no_source": sorted(empty),
            "gaps": {n: t["gap"] for n, t in (doc.get("themes") or {}).items() if t.get("gap")}}


def text_of(s: dict) -> str:
    L = [f"{s['theme']}: {s['says']}" + (f" (in {s['geography']})" if s.get("geography") else "")]
    if s.get("compare"):
        L.append("  count each candidate in the map's window before choosing: they differ in how complete they are")
    if s.get("note"):
        L.append(f"  note: {s['note']}")
    for i, c in enumerate(s["candidates"], 1):
        L.append(f"  {i}. {c['id']}  [{c['license']}, {c['delivery']}" + (f", key {c['key']}" if c["key"] else "") + f"]  fetch from: {c['fetch_from']}"
                 + ("" if c["audience_ok"] else f"  (not for this audience: {c['audience_note']})"))
        L.append(f"     {c['title']}")
    if not s["candidates"]:
        L.append("  no library source covers this place for the theme: hand over to geo-find-data")
    if s.get("elsewhere"):
        L.append(f"  {s['elsewhere']} more source(s) of this theme cover other places")
    if s.get("gap"):
        L.append(f"  not in the library yet: {s['gap']}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["list", "find", "show", "check"])
    ap.add_argument("words", nargs="*")
    ap.add_argument("--geography")
    ap.add_argument("--audience", choices=["any", "internal", "external", "regulatory"])
    ap.add_argument("--format", choices=["json", "text"], default="text")
    a = ap.parse_args(argv)
    doc = load()
    if a.op == "list":
        ents = _entries()
        rows = [{"theme": n, "says": t.get("says"), "sources": len([e for e in members(t, ents) if e.get("status") == "active"]), "compare": bool(t.get("compare"))}
                for n, t in (doc.get("themes") or {}).items()]
        print(json.dumps(rows, indent=1) if a.format == "json" else "\n".join(f"{r['theme']:<24} {r['sources']:>3}  {r['says']}" for r in rows))
        return 0
    if a.op == "check":
        r = check(doc)
        print(json.dumps(r, indent=1))
        return 3 if r["in_no_theme"] or r["no_source"] else 0
    if a.op == "find":
        hits = find(" ".join(a.words), doc)
        if a.format == "json":
            print(json.dumps([{"theme": n, "score": s} for n, s in hits]))
        else:
            print("\n".join(n for n, _ in hits) if hits else "no theme names that; try themes.py list, or ref_library.py search")
        return 0
    if not a.words:
        print("show needs a theme name (themes.py list)", file=sys.stderr)
        return 2
    name = a.words[0] if a.words[0] in (doc.get("themes") or {}) else (find(" ".join(a.words), doc)[:1] or [(None, 0)])[0][0]
    if not name:
        print(f"no theme '{' '.join(a.words)}' (themes.py list)", file=sys.stderr)
        return 2
    s = show(name, a.geography, a.audience, doc)
    print(json.dumps(s, indent=1, default=str) if a.format == "json" else text_of(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
