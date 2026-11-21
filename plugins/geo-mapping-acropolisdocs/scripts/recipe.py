#!/usr/bin/env python3
"""recipe: a whole MapSpec from a recipe in data/recipes/ (2.6.0, spec global-regions H-13), so a map the plugin has made before needs no spec
written by hand.

    recipe.py list
    recipe.py make carrier-choice-h3 --region "Idaho Panhandle" [--technology 5GNR] [--speed 35/3] [--template auto-slide] --out map.yaml

--region takes a named region (the gazetteer) or ISO admin codes (US-WA or US-WA,CA-BC). The MapSpec is written with the region block, the
message and the layer the recipe describes; lint and build it as any other.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

TECH_LABEL = {"5GNR": "5G", "4GLTE": "4G LTE", "3G": "3G"}
TECH_BY_COUNTRY = {"de": {"5GNR": "5G", "4GLTE": "4G", "3G": "4G"}, "fr": {"5GNR": "4G", "4GLTE": "4G", "3G": "3G"},       # Arcep publishes 2G, 3G, 4G
                   "au": {"5GNR": "5G", "4GLTE": "4G", "3G": "3G"}, "gb": {"5GNR": "5G", "4GLTE": "4G", "3G": "3G"}}


def recipes() -> dict:
    out = {}
    for p in sorted((gc.plugin_root() / "data" / "recipes").glob("*.yaml")):
        d = gc.load_mapspec_text(p.read_text(encoding="utf-8")) or {}       # YAML 1.2: the key `on` stays a key
        if d.get("recipe"):
            out[d["recipe"]["id"]] = {**d, "_path": str(p)}
    return out


def region_block(text: str) -> dict:
    import region as region_mod
    codes = [c.strip() for c in str(text).split(",") if c.strip()]
    if codes and all(region_mod.CODE.match(c.upper()) for c in codes):
        return {"admin": codes[0] if len(codes) == 1 else codes}
    return {"place": str(text).strip()}


def _fill(obj, vals: dict):
    if isinstance(obj, str):
        m = re.fullmatch(r"\{(\w+)\}", obj)
        if m and m.group(1) in vals and not isinstance(vals[m.group(1)], str):
            return vals[m.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(vals.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: _fill(v, vals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, vals) for v in obj]
    return obj


def make(name: str, region_text: str, **opts) -> dict:
    rs = recipes()
    if name not in rs:
        raise SystemExit(f"recipe: no recipe '{name}' (recipes: {', '.join(rs)})")
    r = rs[name]
    d = dict(r["recipe"].get("defaults") or {})
    d.update({k: v for k, v in opts.items() if v})
    import region as region_mod
    blk = region_block(region_text)
    reg = region_mod.resolve(blk)
    slug = re.sub(r"[^a-z0-9]+", "-", reg.name.lower()).strip("-")[:40]
    vals = {**d, "region_block": blk, "region_name": reg.name, "technology_label": TECH_LABEL.get(str(d.get("technology")), str(d.get("technology"))),
            "id": f"{name}-{slug}"}
    for cc, table in TECH_BY_COUNTRY.items():
        vals[f"technology_{cc}"] = table.get(str(d.get("technology")), str(d.get("technology")))
    base = dict(r["mapspec"])
    notes = list(reg.notes)
    countries = sorted({str(c).upper().split("-")[0] for c in reg.countries})
    variant = (r.get("variants") or {}).get(countries[0]) if len(countries) == 1 and countries[0] != "US" else None
    if variant:                                              # 3.1.0: the region's own regulator, grid and message
        if variant.get("refuse"):
            raise RecipeError(variant["refuse"])
        base.update({k: v for k, v in variant.items() if k != "refuse"})
        if countries[0] == "FR" and str(d.get("technology")) == "5GNR":
            vals["technology_label"] = "4G"
            notes.append("Arcep's theoretical coverage maps are 2G, 3G and 4G; the map shows 4G")
        notes.append(f"the {countries[0]} variant of {name}: {variant['layers'][0]['source'].get('adapter')}")
    elif len(countries) > 1 and r.get("variants") and set(countries) - {"US"}:
        notes.append(f"{reg.name} spans {', '.join(countries)}: the FCC data cover only the United States, so the rest of the region is empty "
                     "(one map a country uses each regulator's own data)")
    spec = _fill(base, vals)
    return {"spec": spec, "region": reg.record(), "notes": notes, "second_panel": r["recipe"].get("second_panel") if not variant else None}


class RecipeError(Exception):
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("list")
    m = sub.add_parser("make")
    m.add_argument("name")
    m.add_argument("--region", required=True)
    m.add_argument("--technology")
    m.add_argument("--speed")
    m.add_argument("--template")
    m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.op == "list":
        print(json.dumps({k: {"title": v["recipe"].get("title"), "asks": v["recipe"].get("asks")} for k, v in recipes().items()}, indent=2))
        return gc.EXIT_OK
    import region as region_mod
    try:
        got = make(a.name, a.region, technology=a.technology, speed=a.speed, template=a.template)
    except region_mod.RegionError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule, "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return ex.code
    except RecipeError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": "MAP-REG-003", "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return gc.EXIT_REFUSED
    Path(a.out).write_text(gc.dump_mapspec(got["spec"]), encoding="utf-8")
    gc.ledger_note(outputs=[a.out])
    print(json.dumps({"ok": True, "out": a.out, "region": got["region"]["name"], "definition": got["region"]["definition"], "notes": got["notes"],
                      "second_panel": got["second_panel"]}, indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
