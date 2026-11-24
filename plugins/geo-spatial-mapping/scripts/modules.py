#!/usr/bin/env python3
"""modules: the plugin's reference pages that are not skills (1.0.1).

    modules.py list [--kind utility|knowledge|pipeline] [--json]     every module: name, kind, what it covers
    modules.py read NAME [NAME ...]                                 print the module (or several), without its front matter

Helpers, knowledge pages and pipeline guides live in modules/, not skills/. A skill names the modules it needs and when to read each; Claude
reads them with this script. They are not skills because the host lists every skill's description to the model on every turn (a fixed budget
shared with every other installed plugin) and refuses to load a skill marked disable-model-invocation, so a hidden helper skill could never be
read. A module costs nothing until it is read, and reading it through this script is one line in a map trace.

Exit codes: 0 done, 2 usage or an unknown module name (the closest names are suggested).
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

MODULES = Path(__file__).resolve().parent.parent / "modules"
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
KINDS = (("UTILITY", "utility"), ("KNOWLEDGE", "knowledge"), ("PIPELINE", "pipeline"))
HEADER = ("[{name}: {kind} module of this map plugin. Another module named in it is read the same way: "
          "modules.py read NAME. Modules are never loaded with the Skill tool.]")


def _front(text: str) -> tuple[dict, str]:
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    key = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([\w-]+):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            meta[key] = "" if val in (">", "|", ">-", "|-") else val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, text[m.end():]


def kind_of(desc: str) -> str:
    d = (desc or "").strip().upper()
    for prefix, kind in KINDS:
        if d.startswith(prefix):
            return kind
    return "utility"


def catalog() -> list[dict]:
    out = []
    for p in sorted(MODULES.glob("*.md")):
        meta, _ = _front(p.read_text(encoding="utf-8"))
        desc = " ".join((meta.get("description") or "").split())
        out.append({"name": p.stem, "kind": kind_of(desc), "description": re.sub(r"^[A-Z ]+-\s*", "", desc)})
    return out


def read(name: str) -> str:
    p = MODULES / f"{name}.md"
    if not p.is_file():
        near = difflib.get_close_matches(name, [x.stem for x in MODULES.glob("*.md")], n=3, cutoff=0.5)
        raise KeyError(f"no module '{name}'" + (f"; did you mean {', '.join(near)}?" if near else "; run modules.py list"))
    meta, body = _front(p.read_text(encoding="utf-8"))
    return HEADER.format(name=name, kind=kind_of(meta.get("description", ""))) + "\n\n" + body.lstrip("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--kind", choices=[k for _, k in KINDS])
    ls.add_argument("--json", action="store_true")
    rd = sub.add_parser("read")
    rd.add_argument("names", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        rows = [r for r in catalog() if not a.kind or r["kind"] == a.kind]
        if a.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{r['name']:<26} {r['kind']:<10} {r['description'][:110]}")
        return 0
    try:
        texts = [read(n) for n in a.names]
    except KeyError as e:
        print(f"modules: {e.args[0]}", file=sys.stderr)
        return 2
    print("\n\n".join(texts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
