#!/usr/bin/env python3
"""map-guide: the edition's overview page, and what it offers, for the map-guide skill (gap analysis P2-9, M16).

    map_guide.py where [--json]                  the page's path in this edition and whether it is there
    map_guide.py tasks [--goal ID] [--json]      the page's goals and tasks: what you get, the skill or pipeline that makes it, the line to type
    map_guide.py asks  [--json]                  the everyday asks of the Recipes tab, each with the skill that picks it up
    map_guide.py copy  --to DIR                  copy the page into a folder the person can open (an output or connected folder)

Everything is read from the page tools/build_overview.py wrote into this edition (docs/<plugin>.html), so the guide in the chat says what
the page says, for this edition only. Nothing is fetched, nothing runs a map or a pipeline, and nothing is written except by `copy`, which
writes one file into the folder it is given and refuses to overwrite a different file there.

Exit codes: 0 done, 1 the page is missing or unreadable, 2 usage.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

CATALOG = re.compile(r'<script type="application/json" id="nav-catalog">(.*?)</script>', re.S)
ASK = re.compile(r'<li><span class="ask">&ldquo;(.*?)&rdquo;</span> <span class="via">(.*?)</span></li>', re.S)
EVERYDAY = re.compile(r'<h2 id="rx-everyday">.*?<ul class="asks">(.*?)</ul>', re.S)
SERVICES = re.compile(r'<h2 id="rx-services">.*?<ul class="asks">(.*?)</ul>', re.S)


def page_path() -> tuple[Path, str]:
    m = gc.build_manifest()
    return gc.plugin_root() / "docs" / f"{m['plugin_name']}.html", m["plugin_name"]


def read_page() -> str:
    p, name = page_path()
    if not p.is_file():
        raise FileNotFoundError(f"{name} has no overview page at docs/{p.name}; the edition was built without it")
    return p.read_text(encoding="utf-8")


def catalog(text: str) -> dict:
    m = CATALOG.search(text)
    if not m:
        raise ValueError("the overview page carries no task catalog")
    return json.loads(m.group(1).replace("<\\/", "</"))


def tasks(text: str, goal: str | None = None) -> dict:
    cat = catalog(text)
    goals = [{"id": g["id"], "label": g["label"]} for g in cat["goals"]]
    out = []
    for t in cat["tasks"]:
        if goal and t["goal"] != goal:
            continue
        out.append({"id": t["id"], "goal": t["goal"], "title": t["t"], "gets": t["gets"], "mode": t["mode"], "via": t["via"],
                    "type_this": t["tpl"], "blanks": [s["label"] for s in t.get("slots") or []]})
    return {"goals": goals, "tasks": out}


def asks(text: str) -> dict:
    out = {}
    for key, rx in (("everyday", EVERYDAY), ("services", SERVICES)):
        m = rx.search(text)
        out[key] = [{"ask": html.unescape(a), "skill": html.unescape(v)} for a, v in ASK.findall(m.group(1))] if m else []
    return out


def copy_to(dest_dir: Path) -> Path:
    src, _ = page_path()
    if not src.is_file():
        raise FileNotFoundError(f"no overview page at {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists() and dest.read_bytes() != src.read_bytes():
        raise FileExistsError(f"{dest} exists and is a different file; choose another folder")
    shutil.copyfile(src, dest)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["where", "tasks", "asks", "copy"])
    ap.add_argument("--goal")
    ap.add_argument("--to")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.op == "where":
            p, name = page_path()
            rec = {"plugin": name, "page": str(p), "exists": p.is_file(), "bytes": p.stat().st_size if p.is_file() else 0}
            print(json.dumps(rec, indent=1) if a.json else (f"{p}" if rec["exists"] else f"{name}: no overview page at {p}"))
            return gc.EXIT_OK if rec["exists"] else gc.EXIT_RUNTIME
        if a.op == "copy":
            if not a.to:
                print("copy needs --to DIR", file=sys.stderr)
                return gc.EXIT_USAGE
            print(copy_to(Path(a.to)))
            return gc.EXIT_OK
        text = read_page()
        if a.op == "tasks":
            rec = tasks(text, a.goal)
            if a.goal and not rec["tasks"]:
                print(f"no task under goal '{a.goal}'; the goals are {', '.join(g['id'] for g in rec['goals'])}", file=sys.stderr)
                return gc.EXIT_USAGE
            if a.json:
                print(json.dumps(rec, indent=1, ensure_ascii=False))
            else:
                for g in rec["goals"]:
                    ts = [t for t in rec["tasks"] if t["goal"] == g["id"]]
                    if not ts:
                        continue
                    print(f"{g['label']} ({g['id']})")
                    for t in ts:
                        print(f"  - {t['title']}: {t['gets']}  [{t['via']}]")
                        print(f"      type: {t['type_this']}")
            return gc.EXIT_OK
        rec = asks(text)
        if a.json:
            print(json.dumps(rec, indent=1, ensure_ascii=False))
        else:
            for k in ("everyday", "services"):
                print("Everyday asks" if k == "everyday" else "Data services")
                for x in rec[k]:
                    print(f"  \"{x['ask']}\"  [{x['skill']}]")
        return gc.EXIT_OK
    except (FileNotFoundError, ValueError, FileExistsError) as ex:
        print(str(ex), file=sys.stderr)
        return gc.EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
