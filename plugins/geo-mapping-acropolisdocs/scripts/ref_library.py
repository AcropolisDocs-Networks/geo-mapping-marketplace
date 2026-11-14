#!/usr/bin/env python3
"""ref-library: the one access layer to the reference library (spec 9.3): get, list, index, lint and select.

    ref_library.py get ID [--scope shipped|local|all]
    ref_library.py search WORDS... [--limit N] [--format json|text]     # 1.14.0: entries ranked by the words (id, title, summary, publisher, tags)
    ref_library.py list [--type T] [--status S] [--tag facet=value ...] [--scope ...]
    ref_library.py index [--scope local]            # writes workdir/library/index.yaml
    ref_library.py lint  [--scope all] [--release]
    ref_library.py add (--file PATH | --capture ID) [--request FILE] [--type T] [--title T] [--replace]     # a draft into the local overlay
    ref_library.py verify [ID | --due [--horizon DAYS]] [--out DIR] [--route t2|w]                          # read-only; proposes patches
    ref_library.py report [--stale | --coverage | --tags] [--format json|text]
    ref_library.py apply PATCH --approve [--library DIR]                                                    # an owner applies a reviewed patch
    ref_library.py select [--geography G] [--use-for U ...] [--level L ...] [--scale S] [--detail D] [--audience A]
                          [--tier T0] [--join-key KEY] [--message-type M] [--data-shape D] [--map-type T]
                          [--type TYPE] [--limit N] [--format json|text]

Shipped entries are read-only at run time; writes go only to the local overlay, .geo-mapping/library/.
`--library DIR` points the shipped scope at a source-tree library (the maintainer tools use it); without it shipped entries are never written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lints"))
import gmap_common as gc  # noqa: E402
import library_lint as ll  # noqa: E402
import ref_select as rs  # noqa: E402


LIB_OVERRIDE: Path | None = None


def shipped_lib() -> Path:
    return LIB_OVERRIDE or gc.plugin_root() / "library"


def local_lib() -> Path:
    return gc.workdir() / "library"


def all_entries(scope: str = "all") -> list[dict]:
    out = []
    if scope in ("shipped", "all"):
        out += ll.load_entries(shipped_lib() / "entries", "shipped")
    if scope in ("local", "all"):
        out += ll.load_entries(local_lib() / "entries", "local")
    return out


def public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")} | {"scope": e["_scope"]}


def summary() -> dict:
    tags, types = ll.load_vocab(shipped_lib())
    entries = ll.load_entries(shipped_lib() / "entries", "shipped")
    return {"entries": len(entries), "tags": sum(len(f["values"]) for f in tags["facets"].values()),
            "types": len(types["types"]), "version": (shipped_lib() / "LIBRARY_VERSION").read_text().strip()}


def matches(e: dict, a) -> bool:
    if a.type and e.get("type") != a.type:
        return False
    if a.status and e.get("status") != a.status:
        return False
    for t in a.tag or []:
        facet, _, val = t.partition("=")
        if val not in (e.get("tags", {}).get(facet) or []):
            return False
    return True


def library_digest(lib: Path) -> str:
    """Digest over every shipped library file; recorded in BUILD-MANIFEST.json (MAP-ED-013)."""
    h = hashlib.sha256()
    for p in sorted(x for x in lib.rglob("*") if x.is_file() and x.name not in ("index.yaml",)):
        h.update(str(p.relative_to(lib)).encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def maint_op(a) -> int:
    """add, verify, report and apply (library maintenance, spec 9.3.7)."""
    import datetime as dt
    import ref_maint as rm
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    tags, types = ll.load_vocab(shipped_lib())
    root = gc.plugin_root() if not LIB_OVERRIDE else LIB_OVERRIDE.parent
    try:
        if a.op == "report":
            r = rm.report(all_entries(a.scope), tags, types, today, a.horizon or 30)
            part = "stale" if a.stale else "coverage" if a.coverage else "tags" if a.tags else "all"
            if a.format == "text":
                print(rm.report_text(r, part))
            else:
                print(json.dumps(r if part == "all" else ({"stale": {"staleness": r["staleness"], "staleness_counts": r["staleness_counts"]}, "coverage": {"coverage": r["coverage"]}, "tags": {"tags": r["tags"]}}[part]), indent=2))
            return gc.EXIT_OK
        if a.op == "verify":
            es = all_entries(a.scope)
            if a.id:
                hits = [e for e in es if e.get("id") == a.id]
                if not hits:
                    print(f"no entry '{a.id}'", file=sys.stderr)
                    return gc.EXIT_USAGE
            elif a.due:
                hits = rm.due_entries(es, types, today, a.horizon)
            else:
                print("verify needs an entry id or --due", file=sys.stderr)
                return gc.EXIT_USAGE
            net = rm.Net()
            results = [rm.verify_entry(e, today, types, net, root) for e in hits]
            written = []
            if a.out:
                out = Path(a.out)
                out.mkdir(parents=True, exist_ok=True)
                import yaml
                for r in results:
                    if r.get("proposal"):
                        pp = out / f"{r['id']}.rev{r['proposal']['entry_after']['rev']}.patch.yaml"
                        pp.write_text(yaml.safe_dump(r["proposal"], sort_keys=False, allow_unicode=True, width=160))
                        written.append(str(pp))
            for r in results:
                if r.get("proposal") and a.out:
                    r["proposal"] = {k: r["proposal"][k] for k in ("kind", "changes", "changelog")}
                elif r.get("proposal"):
                    r["proposal"] = {k: r["proposal"][k] for k in ("kind", "changes", "changelog")}
            print(json.dumps({"checked": len(results), "route": a.route, "patches": written, "requests": len(net.calls), "results": results}, indent=2, default=str))
            return gc.EXIT_SERVICE if any(r["outcome"] == "unreachable" for r in results) and not any(r["outcome"] in ("unchanged", "changed") for r in results) else gc.EXIT_OK
        if a.op == "apply":
            import yaml
            if not a.id or not Path(a.id).is_file():
                print("apply needs the path of a patch file", file=sys.stderr)
                return gc.EXIT_USAGE
            patch = yaml.safe_load(Path(a.id).read_text())
            scope = patch.get("scope", "shipped")
            if scope == "shipped" and not LIB_OVERRIDE:
                print("Shipped entries change only in the source tree: run this from tools/ or pass --library.", file=sys.stderr)
                return gc.EXIT_REFUSED
            lib = shipped_lib() if scope == "shipped" else local_lib()
            ents = {e["id"]: e for e in ll.load_entries(lib / "entries", scope)}
            print(json.dumps(rm.apply_patch(patch, ents, lib, today, a.approve, root), indent=2))
            return gc.EXIT_OK
        # add
        if bool(a.file) == bool(a.capture):
            print("add needs one of --file or --capture", file=sys.stderr)
            return gc.EXIT_USAGE
        answers = json.loads(Path(a.request).read_text()) if a.request else {}
        for k in ("type", "title"):
            if getattr(a, k, None):
                answers[k] = getattr(a, k)
        if a.replace:
            answers["replace"] = True
        facts = rm.facts_from_file(Path(a.file)) if a.file else rm.facts_from_capture(a.capture)
        res = rm.scaffold(facts, answers, tags, types, ll.load_entries(shipped_lib() / "entries", "shipped"), today, ll.load_entries(local_lib() / "entries", "local"))
        if res["entry"] is None:
            print(json.dumps({"status": "needs-answers", "questions": res["questions"], "type_guess": res.get("type_guess"), "facts": res["facts"]}, indent=2, default=str))
            return gc.EXIT_USAGE
        w = rm.write_local(res["entry"], res["family"], local_lib(), shipped_lib(), ll.load_entries(shipped_lib() / "entries", "shipped"), today)
        print(json.dumps({"status": "written", **w, "entry_status": "draft", "findings": res["findings"],
                          "to_activate": ["add " + k.replace("_", "-") + ": " + v for k, v in res["questions"].items()] + ["set status: active after the review checklist (spec 9.3.7)", "then run ref_library.py lint"]}, indent=2))
        return gc.EXIT_OK
    except rm.MaintError as ex:
        print(f"ref_library {a.op}: {ex}", file=sys.stderr)
        return ex.code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["get", "list", "index", "lint", "select", "add", "verify", "report", "apply", "search"])
    ap.add_argument("id", nargs="*")
    ap.add_argument("--scope", choices=["shipped", "local", "all"], default="all")
    ap.add_argument("--type")
    ap.add_argument("--status")
    ap.add_argument("--tag", action="append")
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--geography")
    ap.add_argument("--bbox", help="1.5.0: W,S,E,N of the map's window; with no --geography the country under its centre is used")
    ap.add_argument("--use-for", action="append", dest="use_for")
    ap.add_argument("--level", action="append")
    ap.add_argument("--scale")
    ap.add_argument("--detail")
    ap.add_argument("--audience", choices=["any", "internal", "external", "regulatory"])
    ap.add_argument("--tier")
    ap.add_argument("--join-key", dest="join_key")
    ap.add_argument("--message-type", dest="message_type")
    ap.add_argument("--data-shape", dest="data_shape")
    ap.add_argument("--map-type", dest="map_type")
    ap.add_argument("--task", action="append", help="a task the entry must not list in avoid_for")
    ap.add_argument("--commercial", action="store_true")
    ap.add_argument("--redistribute", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--today", help="ISO date for staleness checks (tests)")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--file", help="add: a dataset file to describe")
    ap.add_argument("--capture", help="add: a service capture id")
    ap.add_argument("--request", help="add: JSON file with the answers (title, level, use_for, license_class, use_when, avoid_when, ...)")
    ap.add_argument("--title")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--due", action="store_true")
    ap.add_argument("--horizon", type=int, default=0, help="verify/report: also entries due within this many days")
    ap.add_argument("--out", help="verify: folder for the proposed patches")
    ap.add_argument("--route", choices=["t2", "w"], default="t2")
    ap.add_argument("--stale", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--library", help="a source-tree library folder to use as the shipped scope")
    a = ap.parse_args(argv)
    global LIB_OVERRIDE
    if a.library:
        LIB_OVERRIDE = Path(a.library).resolve()
        if not (LIB_OVERRIDE / "tags.yaml").is_file():
            print(f"{a.library} is not a library folder (no tags.yaml)", file=sys.stderr)
            return gc.EXIT_USAGE
    words_ = list(a.id or [])
    a.id = words_[0] if words_ else None
    if a.op == "search":
        # 1.14.0 (Day 13 issue 5): keyword search of the library without reading every entry by hand (1,274 lines of `list` and grep)
        if not words_:
            print("search needs words, e.g. ref_library.py search submarine cable", file=sys.stderr)
            return gc.EXIT_USAGE
        import catalog_search as cs
        hits = cs.search_library(" ".join(words_), limit=a.limit if a.limit != 5 else 10, scope=a.scope)
        rows = [{"id": h["id"], "score": h["score"], "status": h.get("status"), "title": h["title"], "publisher": h["publisher"], "license": h["license"]}
                for h in hits]
        if a.format == "text":
            for r in rows:
                print(f"{r['score']:>3}  {r['id']}  {r['title']}  [{r['license']}, {r['status']}]")
            if not rows:
                print("no library entry matches those words; catalog_search.py searches the public catalogs")
        else:
            print(json.dumps(rows, indent=2))
        return gc.EXIT_OK
    if len(words_) > 1:
        print(f"{a.op} takes one id", file=sys.stderr)
        return gc.EXIT_USAGE
    if a.op in ("add", "verify", "report", "apply"):
        return maint_op(a)

    if a.op == "get":
        if not a.id:
            print("get needs an id", file=sys.stderr)
            return gc.EXIT_USAGE
        hits = [e for e in all_entries(a.scope) if e.get("id") == a.id]
        if not hits:
            print(f"no entry '{a.id}'", file=sys.stderr)
            return gc.EXIT_USAGE
        print(json.dumps([public(h) for h in hits] if len(hits) > 1 else public(hits[0]), indent=2, default=str))
        return gc.EXIT_OK
    if a.op == "list":
        rows = [{"id": e.get("id"), "type": e.get("type"), "status": e.get("status"), "rev": e.get("rev"),
                 "scope": e["_scope"], "title": e.get("title")} for e in all_entries(a.scope) if matches(e, a)]
        print(json.dumps(rows, indent=2))
        return gc.EXIT_OK
    if a.op == "index":
        if a.scope != "local":
            print("At run time only the local overlay can be indexed (shipped entries are read-only).", file=sys.stderr)
            return gc.EXIT_USAGE
        import yaml
        entries = all_entries("local")
        out = local_lib()
        out.mkdir(parents=True, exist_ok=True)
        idx = [{"id": e.get("id"), "type": e.get("type"), "status": e.get("status"), "rev": e.get("rev"), "tags": e.get("tags")} for e in entries]
        (out / "index.yaml").write_text(yaml.safe_dump(idx, sort_keys=False))
        print(json.dumps({"indexed": len(idx), "path": str(out / "index.yaml")}))
        return gc.EXIT_OK
    if a.op == "select":
        import datetime as dt
        tags, types = ll.load_vocab(shipped_lib())
        req = {k: getattr(a, k) for k in ("geography", "use_for", "level", "scale", "detail", "audience", "tier", "join_key", "message_type",
                                          "data_shape", "map_type", "task", "type") if getattr(a, k)}
        if getattr(a, "bbox", None):
            req["bbox"] = [float(v) for v in a.bbox.split(",")]
        if a.commercial:
            req["commercial"] = True
        if a.redistribute:
            req["redistribute"] = True
        today = dt.date.fromisoformat(a.today) if a.today else None
        try:
            res = rs.select_all([e for e in all_entries(a.scope) if e.get("status") != "retired"], tags, types, req, today, a.limit)
        except rs.SelectError as ex:
            print(f"select: {ex}", file=sys.stderr)
            return gc.EXIT_USAGE
        print(rs.render_text(res) if a.format == "text" else json.dumps(res, indent=2, default=str))
        return gc.EXIT_OK if res["fit"] else gc.EXIT_REFUSED
    if a.op == "lint":
        entries = all_entries(a.scope)
        findings = ll.lint_entries(entries, shipped_lib(), plugin_root=gc.plugin_root() if not LIB_OVERRIDE else LIB_OVERRIDE.parent, release=a.release,
                                   check_index=a.release and a.scope in ("shipped", "all"))
        print(json.dumps(findings, indent=2))
        return gc.EXIT_LINT if any(x["severity"] == "BLOCK" for x in findings) else gc.EXIT_OK
    return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
