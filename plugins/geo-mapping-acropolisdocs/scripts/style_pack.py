#!/usr/bin/env python3
"""style_pack: the style packs a map can be built in (1.4.0, R2-PACK).

    style_pack.py list                                   the packs: bundled and your own, with what each is for
    style_pack.py show ID                                one pack: palette, fonts and their licences, ornaments, treatments, web level
    style_pack.py lint ID [--brand SLUG]                 check a pack: schema and hashes (MAP-STYLE-001), font licences (004), colours (005)
    style_pack.py preview ID --spec MAP.yaml --out DIR   build a MapSpec in a pack (a copy of the spec; yours is not changed), as SVG and PNG
    style_pack.py new ID [--from PACK]                   start your own pack under <working folder>/styles/ID from a bundled one (default: default)

Every map has a pack; `default` is the map as it always was (the brand's palette and Inter). A pack that replaces the brand's colours and
fonts is used only when you name it. The brand's logo, stamps, hidden accreditation and footer credit stay under every pack.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import map_style as ms  # noqa: E402


def lint(pid: str, brands: list[str] | None = None) -> dict:
    import brandlib as bl
    import style_lint
    try:
        st = ms.load(pid)
    except ms.StyleError as ex:
        return {"ok": False, "pack": pid, "findings": [ms.finding(ex.rule, "BLOCK", str(ex))]}
    fs = []
    for b in brands or gc.bundled_brands():
        fs += [dict(f, brand=b) for f in style_lint.lint_palette(st, bl.load_pack(b)["tokens"]["modes"])]
    return {"ok": not any(f["severity"] == "BLOCK" for f in fs), "pack": pid, "record": st.record(), "findings": fs}


def preview(pid: str, spec_path, out_dir, formats=("svg", "png")) -> dict:
    import build_map as bm
    src = Path(spec_path)
    spec = gc.load_mapspec(src)
    st = spec.get("style") if isinstance(spec.get("style"), dict) else {}
    spec["style"] = {**st, "pack": pid}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = src.with_name(f".{src.stem}.preview-{pid}.yaml")          # beside the original, so its relative data paths still resolve
    tmp.write_text(gc.dump_mapspec(spec), encoding="utf-8")
    try:
        res = bm.build(str(tmp), str(out), list(formats))
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": not res["blocked"], "pack": pid, "files": res["files"], "findings": [f for f in res["findings"] if f["severity"] != "INFO"][:20]}


def new(pid: str, from_id: str = ms.DEFAULT) -> dict:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", pid):
        return {"ok": False, "findings": [ms.finding("MAP-STYLE-001", "BLOCK", f"'{pid}' is not a pack id (lower case letters, digits and hyphens)")]}
    found = ms.pack_dir(from_id)
    if found is None:
        return {"ok": False, "findings": [ms.finding("MAP-STYLE-003", "BLOCK", f"no style pack '{from_id}' to start from")]}
    dest = ms.person_root() / pid
    if dest.exists():
        return {"ok": False, "findings": [ms.finding("MAP-STYLE-001", "BLOCK", f"{dest} already exists")]}
    shutil.copytree(found[0], dest, ignore=shutil.ignore_patterns("preview.png"))
    y = (dest / "pack.yaml").read_text(encoding="utf-8")
    y = re.sub(r"^id: .*$", f"id: {pid}", y, count=1, flags=re.M)
    y = re.sub(r"^title: .*$", f"title: {pid.replace('-', ' ').title()}", y, count=1, flags=re.M)
    y = re.sub(r"^generated: .*\n", "", y, flags=re.M)
    (dest / "pack.yaml").write_text(y, encoding="utf-8")
    return {"ok": True, "pack": pid, "dir": str(dest), "next": "edit pack.yaml (fonts need their licence file under LICENSES/), then style_pack.py lint " + pid}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("list")
    for name in ("show", "lint"):
        s = sub.add_parser(name)
        s.add_argument("id")
        if name == "lint":
            s.add_argument("--brand", action="append")
    s = sub.add_parser("preview")
    s.add_argument("id")
    s.add_argument("--spec", required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("new")
    s.add_argument("id")
    s.add_argument("--from", dest="from_id", default=ms.DEFAULT)
    a = ap.parse_args(argv)
    if a.op in ("list", "show"):
        return ms.main([a.op] + ([a.id] if a.op == "show" else []))
    if a.op == "lint":
        r = lint(a.id, a.brand)
    elif a.op == "preview":
        r = preview(a.id, a.spec, a.out)
    else:
        r = new(a.id, a.from_id)
    print(json.dumps(r, indent=2, default=str))
    return 0 if r.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
