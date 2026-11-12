#!/usr/bin/env python3
"""map-brand: the plugin's only brand surface, map-scoped by construction (spec 11.4).

    map_brand.py resolve    --spec map.yaml [--request-brand SLUG]
    map_brand.py tokens     --spec map.yaml [--mode print|screen]
    map_brand.py logo       --spec map.yaml [--variant primary|secondary] [--ground light|dark|#RRGGBB]
    map_brand.py fonts      --spec map.yaml [--files]
    map_brand.py guard      --spec map.yaml --file OUT.svg|OUT.html
    map_brand.py props      --spec map.yaml --format pdf|svg|html|png|gpkg|geojson|kml
    map_brand.py voice-check --spec map.yaml (--text TEXT | --file F)

Every operation needs a map context: the MapSpec path AND a build token created by
`env_probe.py --begin-build --spec ...` (GMAP_BUILD_TOKEN or --token). Without both it exits 6,
says "brand request outside a map build", and writes nothing to stdout. It never searches for other
plugins, never writes outside the working directory, and returns nothing for documents, decks or charts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lints"))
import brandlib as bl  # noqa: E402
import gmap_common as gc  # noqa: E402

OUTSIDE = "brand request outside a map build"


def refuse(extra: str = "") -> int:
    print(OUTSIDE + (f" ({extra})" if extra else ""), file=sys.stderr)
    return gc.EXIT_BRAND_OUTSIDE_BUILD


def valid_context(spec: str | None, token: str | None) -> str | None:
    """Return None when the map context is valid, else a short reason."""
    if not spec or not Path(spec).is_file():
        return "no MapSpec"
    if not token:
        return "no build token"
    f = gc.workdir() / "run" / "tokens" / f"{re.sub(r'[^0-9a-f]', '', token)}.json"
    if not f.is_file():
        return "unknown build token"
    try:
        rec = json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return "unreadable build token"
    if rec["expires"] < dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
        return "build token expired"
    if Path(rec["spec"]).resolve() != Path(spec).resolve():
        return "build token belongs to another MapSpec"
    return None


def read_spec_brand(spec: Path) -> str | None:
    d = gc.load_mapspec(spec) or {}
    b = d.get("brand")
    return str(b) if b else None


def write_spec_brand(spec: Path, slug: str) -> bool:
    """Write the resolved slug into the MapSpec's top-level `brand:` line (comments and layout preserved)."""
    text = spec.read_text()
    if re.search(r"^brand:\s*" + re.escape(slug) + r"\b", text, re.M):
        return False
    if re.search(r"^brand:", text, re.M):
        text = re.sub(r"^brand:[^\n#]*", f"brand: {slug}", text, count=1, flags=re.M)
    else:
        m = re.search(r"^id:[^\n]*\n", text, re.M)
        text = (text[:m.end()] + f"brand: {slug}\n" + text[m.end():]) if m else f"brand: {slug}\n" + text
    spec.write_text(text)
    return True


def current_brand(spec: Path, manifest: dict, request_brand: str | None = None) -> dict:
    r = bl.resolve(manifest, read_spec_brand(spec), request_brand)
    return r


def cmd_resolve(a, manifest) -> int:
    spec = Path(a.spec)
    r = current_brand(spec, manifest, a.request_brand)
    r["edition"] = manifest["plugin_name"]
    r["wrote_to_spec"] = write_spec_brand(spec, r["brand"])
    print(json.dumps(r, indent=2))
    return gc.EXIT_OK


def settled(a, manifest) -> str:
    spec = Path(a.spec)
    r = current_brand(spec, manifest)
    write_spec_brand(spec, r["brand"])
    return r["brand"]


def cmd_tokens(a, manifest) -> int:
    slug = settled(a, manifest)
    p = bl.load_pack(slug)
    t = p["tokens"]
    out = {"brand": slug, "mode": a.mode, "typeface": t["typeface"], "output_prefix": t["output_prefix"],
           "tokens": t["modes"][a.mode], "substitutions": [s for s in t["substitutions"] if s["mode"] == a.mode]}
    print(json.dumps(out, indent=2))
    return gc.EXIT_OK


def cmd_logo(a, manifest) -> int:
    slug = settled(a, manifest)
    logo = bl.load_pack(slug)["brand"]["logo"]
    g = a.ground
    if g in ("light", "dark"):
        ground_dark = g == "dark"
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        h = g.lstrip("#")
        r, gg, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        ground_dark = (0.2126 * r + 0.7152 * gg + 0.0722 * b) < 0.4
    key = "dark" if ground_dark else "light"  # registry convention: `dark` = the mark for dark grounds
    src = logo
    if a.variant == "secondary":
        src = logo.get("secondary")
        if not isinstance(src, dict):
            print(f"brand '{slug}' has no secondary logo", file=sys.stderr)
            return gc.EXIT_USAGE
    svg = src.get(key)
    if not svg:
        print(f"brand '{slug}' has no {a.variant} logo for a {key} ground", file=sys.stderr)
        return gc.EXIT_USAGE
    if a.out:
        Path(a.out).write_text(svg)
        print(json.dumps({"brand": slug, "variant": a.variant, "ground": key, "written": a.out}))
    else:
        print(svg)
    return gc.EXIT_OK


def cmd_fonts(a, manifest) -> int:
    settled(a, manifest)
    if a.files:
        print(json.dumps({"family": gc.FONT_FAMILY, "files": [str(bl.inter_font_path())],
                          "license": str(bl.inter_font_path().with_name("LICENSE.txt"))}))
    else:
        print(bl.inter_font_face_css())
    return gc.EXIT_OK


def cmd_guard(a, manifest) -> int:
    import brand_lint as lint
    slug = settled(a, manifest)
    f = Path(a.file)
    if not f.is_file():
        print(f"no such file: {f}", file=sys.stderr)
        return gc.EXIT_USAGE
    ext = f.suffix.lower()
    findings = lint.check_bundled(slug, manifest) + lint.check_pack_integrity(slug)
    if ext in (".svg", ".html", ".htm", ".css"):
        text = f.read_text(errors="replace")
        findings += lint.check_palette(text, slug) + lint.check_fonts(text)
        if ext != ".css":
            findings += lint.check_hidden_accreditation(text, slug, manifest) + lint.check_stamps(text, slug, manifest)
    else:
        findings.append(lint.finding("MAP-BRAND-001", "INFO", f"palette guard covers SVG, HTML and CSS; {ext or 'this file'} is checked at render time", None))
    print(json.dumps({"brand": slug, "file": str(f), "findings": findings}, indent=2))
    return lint.worst(findings)


def props_for(slug: str, manifest: dict, fmt: str) -> dict:
    """Hidden properties for one output format: author, producer and the house accreditation (never visible)."""
    pack = bl.load_pack(slug)
    acc = bl.accreditation_props(manifest, slug)
    name = pack["brand"]["identity"]["name"]
    base = {"author": name, "creator": name, "company": name, "producer": acc["plugin"] + " " + str(acc["plugin_version"]),
            "accreditation": acc["house"], "accreditation_url": acc["house_url"], "brand_pack": acc["brand_pack"]}
    if fmt == "pdf":
        out = {"Info": {"Author": name, "Creator": acc["plugin"], "Producer": base["producer"], "Subject": ""},
               "XMP": {"accreditation": acc["house"], "accreditation_url": acc["house_url"], "brand_pack": acc["brand_pack"],
                       "creator_tool": base["producer"]}}
    elif fmt == "svg":
        out = {"root_attributes": {"data-accreditation": slug_house(manifest), "data-map-brand": slug, "data-map-edition": manifest["plugin_name"]},
               "metadata": {"dc:creator": name, "dc:publisher": name, **{f"geo:{k}": v for k, v in base.items()}}}
    elif fmt == "html":
        out = {"root_attributes": {"data-accreditation": slug_house(manifest), "data-map-brand": slug, "data-map-edition": manifest["plugin_name"]},
               "meta": [{"name": "accreditation", "content": acc["house"]}, {"name": "generator", "content": base["producer"]},
                        {"name": "author", "content": name}, {"name": "brand-pack", "content": acc["brand_pack"]}]}
    elif fmt == "png":
        out = {"tEXt": {"Accreditation": acc["house"], "Software": base["producer"], "Author": name, "BrandPack": acc["brand_pack"]}}
    else:  # gpkg, geojson, kml
        out = {"metadata": base}
    out["trademark_line"] = pack["pack"].get("trademark_line")
    out.update({"brand": slug, "format": fmt, "visible": False})
    return out


def cmd_props(a, manifest) -> int:
    slug = settled(a, manifest)
    print(json.dumps(props_for(slug, manifest, a.format), indent=2))
    return gc.EXIT_OK


def slug_house(manifest: dict) -> str:
    return manifest["house_brand"]


def cmd_voice(a, manifest) -> int:
    import brand_lint as lint
    slug = settled(a, manifest)
    text = a.text if a.text is not None else Path(a.file).read_text(errors="replace")
    findings = lint.check_voice(text, slug)
    print(json.dumps({"brand": slug, "findings": findings}, indent=2))
    return lint.worst(findings)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["resolve", "tokens", "logo", "fonts", "guard", "props", "voice-check"])
    ap.add_argument("--spec")
    ap.add_argument("--token", default=os.environ.get("GMAP_BUILD_TOKEN"))
    ap.add_argument("--request-brand")
    ap.add_argument("--mode", choices=["print", "screen"], default="print")
    ap.add_argument("--variant", choices=["primary", "secondary"], default="primary")
    ap.add_argument("--ground", default="light")
    ap.add_argument("--out")
    ap.add_argument("--files", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--format", choices=["pdf", "svg", "html", "png", "gpkg", "geojson", "kml"])
    ap.add_argument("--text")
    a = ap.parse_args(argv)

    why = valid_context(a.spec, a.token)
    if why:
        return refuse(why)
    try:
        manifest = gc.build_manifest()
        if a.op == "resolve":
            return cmd_resolve(a, manifest)
        if a.op == "tokens":
            return cmd_tokens(a, manifest)
        if a.op == "logo":
            return cmd_logo(a, manifest)
        if a.op == "fonts":
            return cmd_fonts(a, manifest)
        if a.op == "guard":
            if not a.file:
                print("guard needs --file", file=sys.stderr)
                return gc.EXIT_USAGE
            return cmd_guard(a, manifest)
        if a.op == "props":
            if not a.format:
                print("props needs --format", file=sys.stderr)
                return gc.EXIT_USAGE
            return cmd_props(a, manifest)
        if a.op == "voice-check":
            if a.text is None and not a.file:
                print("voice-check needs --text or --file", file=sys.stderr)
                return gc.EXIT_USAGE
            return cmd_voice(a, manifest)
    except bl.BrandError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_USAGE
    return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
