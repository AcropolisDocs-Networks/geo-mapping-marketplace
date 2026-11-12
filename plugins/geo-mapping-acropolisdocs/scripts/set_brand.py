#!/usr/bin/env python3
"""geo-set-brand: your map brands (1.7.0). The plugin ships one brand, its default. Any other brand is a pack you add; nothing is rebuilt.

    set_brand.py list                                     the brands here, which is the default, where added packs are kept
    set_brand.py set SLUG                                 make a brand your default for new maps
    set_brand.py new --name "Acme Fiber" --color "#RRGGBB" [--color HEX ...] [--logo logo.svg] [--logo-dark logo-dark.svg]
                     [--slug acme] [--owner "Acme Fiber, Inc."] [--homepage URL] [--trademark "TEXT"] [--default] [--replace]
                                                          make a brand from a name, one to five colours and an SVG logo
    set_brand.py add PATH [--default] [--replace]         add a pack folder or a brand .json (the AcropolisDocs brand registry format)
    set_brand.py export SLUG --out DIR                    copy an added pack to a folder, to share it or keep it with a project
    set_brand.py template --out DIR                       write a starter brand.json to edit, then `add` it
    set_brand.py remove SLUG                              remove a pack you added (never the shipped brand)

    --to DIR / --home (with new, add)                     keep the pack in DIR, or in your own ~/.geo-mapping/brands/, instead of this
                                                          project's .geo-mapping/brands/

The older forms still work: `--show` is `list`, `--set SLUG` is `set SLUG`.

What a brand changes: the map's colours (derived once into map roles, with contrast and colour-vision checks), the logo, the names in file
properties, the output file prefix and an optional trademark line. What it never changes: the typeface (Inter; a style pack may bring its
own faces), the layout defaults, the checks. It returns no tokens, logos or styling, so it cannot brand non-map content, and it never
writes to user memory, preferences, settings or CLAUDE.md. A logo must be a plain SVG drawing: scripts, links and embedded pages are refused.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brandlib as bl  # noqa: E402
import gmap_common as gc  # noqa: E402

DEFAULTS_FILE = "user-defaults.yaml"


def _dest(a) -> Path:
    if getattr(a, "to", None):
        return Path(a.to).expanduser()
    if getattr(a, "home", False):
        return Path.home() / gc.WORKDIR_NAME / gc.BRANDS_DIRNAME
    return gc.workdir(create=True) / gc.BRANDS_DIRNAME


def _found_from(dest: Path) -> bool:
    """True when maps built here will find a pack kept in DEST."""
    try:
        return any(dest.resolve() == r.resolve() for r in gc.user_brand_roots())
    except OSError:
        return False


def listing(m: dict) -> dict:
    cur, src = bl.user_default(m)
    here = bl.available(m)
    added = gc.user_brands()
    return {
        "shipped": {s: bl.brand_name(s) for s in m["brands_bundled"]},
        "added": {s: {"name": bl.brand_name(s), "folder": str(d)} for s, d in sorted(added.items())},
        "plugin_default": m["default_content_brand"],
        "your_default": cur, "your_default_source": src,
        "effective_default": cur if cur in here else m["default_content_brand"],
        "added_packs_are_looked_for_in": [str(r) for r in gc.user_brand_roots()],
        # the pre-1.7.0 keys, kept for callers that read them
        "bundled": {s: bl.brand_name(s) for s in here}, "edition_default": m["default_content_brand"],
        "user_default": cur, "user_default_source": src,
    }


def store_default(slug: str) -> Path:
    wd = gc.workdir(create=True)
    (wd / DEFAULTS_FILE).write_text(f"# Written by geo-set-brand at the user's request. Changes only the default map brand.\ndefault_brand: {slug}\n", encoding="utf-8")
    return wd / DEFAULTS_FILE


def cmd_set(slug: str, m: dict) -> int:
    if slug not in bl.available(m):
        print(f"There is no brand '{slug}' here. {bl.available_note(m)}", file=sys.stderr)
        return gc.EXIT_USAGE
    f = store_default(slug)
    print(json.dumps({"default_brand": slug, "stored_in": str(f), "note": "Existing maps keep the brand written in their MapSpec."}, indent=2))
    return gc.EXIT_OK


def _finish(res: dict, a, m: dict, dest: Path) -> int:
    out = {"added": res["slug"], "name": res["name"], "folder": res["dir"],
           "colours_adjusted_for_maps": [f"{s['mode']} {s['token']}: {s['from']} -> {s['to']} ({s['reason']})" for s in res["substitutions"]],
           "use_it": f"name it in a request (\"in the {res['name']} brand\"), put `brand: {res['slug']}` in a MapSpec, or make it your default with `set {res['slug']}`"}
    if not _found_from(dest):
        out["note"] = (f"{dest} is not one of the folders maps look in. Set GEO_MAPPING_BRANDS to it, or add the pack again without --to.")
    if a.default:
        out["default_brand"] = res["slug"]
        out["stored_in"] = str(store_default(res["slug"]))
    print(json.dumps(out, indent=2))
    return gc.EXIT_OK


def cmd_add(a, m: dict) -> int:
    import brand_pack as bp
    dest = _dest(a)
    res = bp.install(Path(a.path).expanduser(), dest, bl.house_accreditation(m), reserved=tuple(m["brands_bundled"]), replace=a.replace)
    return _finish(res, a, m, dest)


def cmd_new(a, m: dict) -> int:
    import brand_pack as bp

    def svg(p):
        return bp.read_logo(p) if p else None

    raster = [x for x in (a.logo, a.logo_dark) if x and Path(x).suffix.lower() in bp.RASTER_LOGOS]
    brand = bp.make_brand(a.name, a.color or [], slug=a.slug, owner=a.owner, homepage=a.homepage, logo_light=svg(a.logo),
                          logo_dark=svg(a.logo_dark), trademark_line=a.trademark, tagline=a.tagline)
    dest = _dest(a)
    res = bp.write_pack(brand, dest, bl.house_accreditation(m), source={"kind": "user", "from": "new"}, reserved=tuple(m["brands_bundled"]), replace=a.replace)
    rc = _finish(res, a, m, dest)
    if raster:
        print("The logo is a picture (PNG or JPEG), so it is drawn at its own pixels; an SVG stays sharp at every size and can be set later "
              "with --logo FILE.svg --replace.", file=sys.stderr)
    if not a.logo and not a.logo_dark:
        print("No logo was given, so the pack has a plain placeholder mark. Run `new` again with --logo FILE.svg and --replace to set yours.", file=sys.stderr)
    return rc


def cmd_export(a, m: dict) -> int:
    if bl.origin(a.slug) != "added":
        print(f"'{a.slug}' is not a pack you added. " + ("The shipped brand stays in the plugin; `template` writes a starter you can edit."
                                                           if bl.origin(a.slug) else bl.available_note(m)), file=sys.stderr)
        return gc.EXIT_USAGE
    out = Path(a.out).expanduser() / a.slug
    if out.exists():
        print(f"{out} is already there; choose another folder", file=sys.stderr)
        return gc.EXIT_USAGE
    shutil.copytree(gc.brand_dir(a.slug), out)
    print(json.dumps({"exported": a.slug, "folder": str(out), "add_it_elsewhere_with": f"set_brand.py add \"{out}\""}, indent=2))
    return gc.EXIT_OK


def cmd_template(a, m: dict) -> int:
    import brand_pack as bp
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    f = out / "brand.json"
    if f.exists():
        print(f"{f} is already there; choose another folder", file=sys.stderr)
        return gc.EXIT_USAGE
    t = bp.starter()["template"]
    brand = bp.make_brand(t["name"], t["colors"], slug=t["slug"], owner=t["owner"], homepage=t["homepage"])
    f.write_text(json.dumps(brand, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(f), "next": ["edit identity (slug, name, owner), the palette for print and screen, and paste your SVG marks into logo.light "
                                                  "(drawn on light grounds) and logo.dark (drawn on dark grounds)", f"set_brand.py add \"{out}\""]}, indent=2))
    return gc.EXIT_OK


def cmd_remove(a, m: dict) -> int:
    import brand_pack as bp
    if bl.origin(a.slug) != "added":
        print(f"'{a.slug}' is not a pack you added" + (" (the shipped brand cannot be removed)." if bl.origin(a.slug) else "."), file=sys.stderr)
        return gc.EXIT_USAGE
    d = gc.brand_dir(a.slug)
    try:
        bp.remove(a.slug, d.parent)
    except OSError as e:
        print(f"The pack could not be removed ({type(e).__name__}); delete this folder yourself: {d}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    out = {"removed": a.slug, "folder": str(d)}
    cur, _ = bl.user_default(m)
    if cur == a.slug:
        out["note"] = f"'{a.slug}' was your default; new maps use {m['default_content_brand']} until you set another"
    print(json.dumps(out, indent=2))
    return gc.EXIT_OK


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--show":                      # the pre-1.7.0 forms
        argv = ["list"] + argv[1:]
    elif argv and argv[0] == "--set":
        argv = ["set"] + argv[1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("set")
    p.add_argument("slug")
    for name in ("new", "add"):
        p = sub.add_parser(name)
        if name == "add":
            p.add_argument("path")
        else:
            p.add_argument("--name", required=True)
            p.add_argument("--color", "--colour", action="append", metavar="HEX")
            p.add_argument("--logo", metavar="FILE", help="the mark drawn on light grounds, SVG (best) or PNG/JPEG (used on dark grounds too when --logo-dark is not given)")
            p.add_argument("--logo-dark", metavar="FILE", help="the mark drawn on dark grounds")
            p.add_argument("--slug")
            p.add_argument("--owner")
            p.add_argument("--homepage")
            p.add_argument("--tagline")
            p.add_argument("--trademark", metavar="TEXT", help="a trademark line the brand requires on its maps")
        p.add_argument("--default", action="store_true", help="also make it your default brand")
        p.add_argument("--replace", action="store_true", help="update a pack of the same name")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--to", metavar="DIR")
        g.add_argument("--home", action="store_true")
    p = sub.add_parser("export")
    p.add_argument("slug")
    p.add_argument("--out", required=True)
    p = sub.add_parser("template")
    p.add_argument("--out", required=True)
    p = sub.add_parser("remove")
    p.add_argument("slug")
    a = ap.parse_args(argv)
    m = gc.build_manifest()
    try:
        if a.cmd == "list":
            print(json.dumps(listing(m), indent=2))
            return gc.EXIT_OK
        if a.cmd == "set":
            return cmd_set(a.slug, m)
        return {"add": cmd_add, "new": cmd_new, "export": cmd_export, "template": cmd_template, "remove": cmd_remove}[a.cmd](a, m)
    except bl.BrandError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_USAGE
    except OSError as e:
        print(f"set_brand: {type(e).__name__}: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    except Exception as e:  # noqa: BLE001 - brand_pack.PackError is imported lazily; anything else is re-raised
        if type(e).__name__ == "PackError":
            print(str(e), file=sys.stderr)
            return gc.EXIT_USAGE
        raise


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
