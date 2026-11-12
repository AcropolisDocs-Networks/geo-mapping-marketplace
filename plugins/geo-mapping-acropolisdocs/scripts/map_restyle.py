#!/usr/bin/env python3
"""map-restyle: change an existing MapSpec in small, checked steps, then lint and (optionally) rebuild.

    map_restyle.py --spec map.yaml [--off legend ...] [--on north_arrow ...] [--template NAME] [--pack ID]
                   [--palette LAYER:NAME[:reverse]] [--classify LAYER:METHOD[:K]] [--brand SLUG]
                   [--basemap none|bundled] [--context auto|none|roads|urban|water|full|terrain] [--names on|off|capitals|major|all]
                   [--terrain on|off|contours]
                   [--title TEXT] [--subtitle TEXT] [--position ELEMENT:CORNER]
                   [--classes-method LAYER:log|head_tail[:K]] [--bivariate LAYER:XFIELD:YFIELD[:2|3]] [--patterns LAYER:auto|on|off]
                   [--set dotted.path=YAML] [--undo] [--build OUT_DIR [--formats svg,pdf,png]]

Every change is validated before anything is written; the previous spec is kept beside it as `<spec>.prev` (one step of undo).
Nothing here picks colors: palettes are named roles resolved from the brand tokens when the map is built. Comments in the
YAML file are not preserved (the file is rewritten from its data), so the .prev copy is the place to look for them.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import mapspec_lint as ml  # noqa: E402

METHODS = {"quantile", "equal_interval", "pretty", "geometric", "jenks", "std_dev", "manual", "categorical", "log", "head_tail",
           "equal_count", "rounded", "nice"}      # equal_count = quantile; rounded / nice = pretty (see classify.ALIASES)
CONTEXT_PRESETS = ("auto", "none", "roads", "urban", "water", "full", "terrain")   # the bundled context pack under the data (map_plan.CONTEXT_PRESETS)
NAMES_CHOICES = ("on", "off", "auto", "capitals", "major", "all")           # place names: the toggle, or the tier (names.places)
TERRAIN_CHOICES = ("on", "off", "contours")                                  # relief under the map (M12): on, off, or on with contours


class RestyleError(Exception):
    pass


def _suggest(word: str, options) -> str:
    m = difflib.get_close_matches(word, sorted(options), n=1)
    return f" (did you mean '{m[0]}'?)" if m else ""


def _layer(spec: dict, lid: str) -> dict:
    for l in spec.get("layers", []):
        if l.get("id") == lid:
            return l
    ids = [l.get("id") for l in spec.get("layers", [])]
    raise RestyleError(f"no layer '{lid}'; the layers are {', '.join(map(str, ids))}")


def _element(spec: dict, name: str) -> dict:
    if name not in ml.ELEMENTS:
        raise RestyleError(f"unknown element '{name}'{_suggest(name, ml.ELEMENTS)}; elements are {', '.join(sorted(ml.ELEMENTS))}")
    el = spec.setdefault("elements", {}).setdefault(name, {})
    el.setdefault("on", True)                      # the MapSpec schema requires `on` on every element entry
    return el


def set_path(spec: dict, dotted: str, value):
    """Set a dotted path such as elements.legend.columns or layers.counties.legend.title (a list segment is matched by its id); a null value
    removes the key."""
    parts = dotted.split(".")
    cur = spec
    for i, p in enumerate(parts[:-1]):
        if isinstance(cur, list):
            hit = [x for x in cur if isinstance(x, dict) and x.get("id") == p]
            if not hit:
                raise RestyleError(f"'{p}' is not an id in {'.'.join(parts[:i])}")
            cur = hit[0]
        else:
            if p not in cur or cur[p] is None:
                cur[p] = {}
            cur = cur[p]
    if isinstance(cur, list):
        raise RestyleError(f"'{dotted}' ends at a list; name an item by its id")
    if value is None:                                     # 0.22.0: --set path=null takes the key out (a block switched off, e.g. a highlight)
        cur.pop(parts[-1], None)
        return
    cur[parts[-1]] = value
    if len(parts) >= 3 and parts[0] == "elements":
        spec["elements"][parts[1]].setdefault("on", True)


def apply(spec: dict, a, manifest: dict) -> tuple[dict, list[str]]:
    """Return (new spec, plain-language list of changes). Raises RestyleError before anything is written."""
    s = copy.deepcopy(spec)
    done: list[str] = []
    for name in a.off or []:
        _element(s, name)["on"] = False
        done.append(f"{name} off")
    for name in a.on or []:
        _element(s, name)["on"] = True
        done.append(f"{name} on")
    if getattr(a, "pack", None):                     # 1.4.0 (R2-PACK): a new pack is a new build of the whole map from its kept data
        import map_style
        known = [x["id"] for x in map_style.list_packs()] or ["default"]
        if a.pack not in known:
            raise RestyleError(f"no style pack '{a.pack}'{_suggest(a.pack, known)}; the packs are {', '.join(known)}")
        st_ = s.setdefault("style", {}) if isinstance(s.get("style"), dict) else s.setdefault("style", {})
        old_pack = st_.get("pack") or "default"
        if a.pack == "default":
            st_.pop("pack", None)
        else:
            st_["pack"] = a.pack
        if not st_:
            s.pop("style", None)
        done.append(f"style pack {old_pack} -> {a.pack} (the map is built again from its data: plan, colours, labels, legend, layout and every output)")
    if a.template:
        import layout as lo
        try:
            cs = lo.custom_size(a.template)                  # custom:500x700 is a custom sheet in mm (M16)
        except lo.LayoutError as ex:
            raise RestyleError(str(ex))
        if cs is not None:
            probs = lo.custom_problems(*cs)
            if probs:
                raise RestyleError("; ".join(probs))
            s["template"] = {"custom": {"w_mm": cs[0], "h_mm": cs[1]}}
            done.append(f"template custom {cs[0]:g} x {cs[1]:g} mm (scaled from {lo.nearest_template(*cs)})")
        elif a.template not in ml.template_names():
            raise RestyleError(f"no template '{a.template}'{_suggest(a.template, ml.template_names())}; templates are {', '.join(ml.template_names())}, or custom:WIDTHxHEIGHT in mm")
        else:
            s["template"] = a.template
            done.append(f"template {a.template}")
    for item in a.palette or []:
        lid, _, rest = item.partition(":")
        name, _, flag = rest.partition(":")
        if name not in ml.PALETTES:
            raise RestyleError(f"palette '{name}' is not one of {', '.join(sorted(ml.PALETTES))}{_suggest(name, ml.PALETTES)}")
        pal = {"name": name}
        if flag:
            if flag != "reverse":
                raise RestyleError(f"unknown palette flag '{flag}'; only 'reverse' is understood")
            pal["reverse"] = True
        _layer(s, lid)["palette"] = pal
        done.append(f"{lid}: palette {name}" + (" reversed" if flag else ""))
    for item in a.classify or []:
        parts = item.split(":")
        if len(parts) < 2:
            raise RestyleError("--classify needs LAYER:METHOD[:K[:ROUNDING]] (ROUNDING is nice, none or a number)")
        lid, method = parts[0], parts[1]
        if method not in METHODS:
            raise RestyleError(f"unknown method '{method}'{_suggest(method, METHODS)}; methods are {', '.join(sorted(METHODS))}")
        c = {"method": method}
        if len(parts) > 2:
            try:
                c["classes"] = int(parts[2])
            except ValueError:
                raise RestyleError(f"the class count '{parts[2]}' is not a whole number") from None
        if len(parts) > 3:
            r = parts[3]
            if r in ("nice", "none"):
                c["rounding"] = r
            else:
                try:
                    c["rounding"] = float(r)
                except ValueError:
                    raise RestyleError(f"rounding '{r}' is not nice, none or a number") from None
        old = _layer(s, lid).get("classification") or {}
        _layer(s, lid)["classification"] = {**{k: v for k, v in old.items() if k not in ("method", "classes", "breaks", "rounding")}, **c}
        done.append(f"{lid}: {method}" + (f", {c['classes']} classes" if "classes" in c else ""))
    if a.brand:
        here = gc.available_brands(manifest)              # 1.7.0: the shipped brand and the packs this person added
        if a.brand not in here:
            raise RestyleError(f"there is no brand '{a.brand}' here; the brands are {', '.join(here)} (add your own with the brand command)")
        s["brand"] = a.brand
        done.append(f"brand {a.brand}")
    if a.basemap:
        if a.basemap not in ("none", "bundled"):
            raise RestyleError("the basemap is 'none' or 'bundled' in this release; tile providers arrive with the web map milestone")
        s.setdefault("basemap", {})["provider"] = a.basemap
        done.append(f"basemap {a.basemap}")
    if a.title is not None:
        _element(s, "title").update({"on": True, "text": a.title})
        done.append("title text")
    if a.subtitle is not None:
        _element(s, "title").update({"on": True, "subtitle": a.subtitle})
        done.append("subtitle")
    if getattr(a, "theme", None):
        s["theme"] = a.theme
        done.append(f"theme {a.theme}")
    if getattr(a, "context", None):
        if a.context not in CONTEXT_PRESETS:
            raise RestyleError(f"context '{a.context}' is not one of {', '.join(CONTEXT_PRESETS)}{_suggest(a.context, CONTEXT_PRESETS)}")
        s.setdefault("context", {})["preset"] = a.context
        s["context"]["on"] = a.context != "none"
        done.append(f"context {a.context}")
    if getattr(a, "names", None):
        if a.names not in NAMES_CHOICES:
            raise RestyleError(f"names '{a.names}' is not one of {', '.join(NAMES_CHOICES)}{_suggest(a.names, NAMES_CHOICES)}")
        n = s.setdefault("names", {})
        if a.names in ("on", "off"):
            n["on"] = a.names == "on"
        else:
            n["on"], n["places"] = True, a.names
        done.append(f"names {a.names}")
    if getattr(a, "terrain", None):
        if a.terrain not in TERRAIN_CHOICES:
            raise RestyleError(f"terrain '{a.terrain}' is not one of {', '.join(TERRAIN_CHOICES)}{_suggest(a.terrain, TERRAIN_CHOICES)}")
        t = s.setdefault("terrain", {})
        t["on"] = a.terrain != "off"
        if a.terrain == "contours":
            t.setdefault("contours", {})["on"] = True
        elif a.terrain == "off" and isinstance(t.get("contours"), dict):
            t["contours"]["on"] = False
        el = (s.get("elements") or {}).get("terrain")
        if isinstance(el, dict):                           # the element toggle is the same switch; keep the two in step
            el["on"] = a.terrain != "off"
        done.append(f"terrain {a.terrain}" + (" (relief fetched for the window when the map is built: 3DEP in the US; give terrain.source a DEM file to fetch nothing)"
                                               if a.terrain != "off" and str(t.get("source") or "auto") in ("auto", "3dep", "terrarium") else ""))
    for item in a.position or []:
        name, _, pos = item.partition(":")
        if pos == "default":                               # back to the element's default corner
            _element(s, name).pop("position", None)
            done.append(f"{name} at its default corner")
            continue
        if pos not in ml.POSITIONS:
            raise RestyleError(f"position '{pos}' is not one of {', '.join(sorted(ml.POSITIONS))}{_suggest(pos, ml.POSITIONS)}")
        _element(s, name)["position"] = pos
        done.append(f"{name} at {pos}")
    for item in getattr(a, "classes_method", None) or []:          # --classes-method LAYER:log|head_tail[:K] (M15), the same as --classify
        parts = item.split(":")
        if len(parts) < 2 or parts[1] not in METHODS:
            raise RestyleError(f"--classes-method needs LAYER:METHOD[:K] with METHOD one of {', '.join(sorted(METHODS))}")
        old = _layer(s, parts[0]).get("classification") or {}
        c = {"method": parts[1], **({"classes": int(parts[2])} if len(parts) > 2 and parts[2].isdigit() else {})}
        _layer(s, parts[0])["classification"] = {**{k: v for k, v in old.items() if k not in ("method", "classes", "breaks")}, **c}
        done.append(f"{parts[0]}: {parts[1].replace('_', '/')} classes" + (f", up to {c['classes']}" if parts[1] == "head_tail" and "classes" in c else
                                                                          (f", {c['classes']} classes" if "classes" in c else "")))
    for item in getattr(a, "bivariate", None) or []:               # --bivariate LAYER:XFIELD:YFIELD[:2|3] or LAYER:off (M15)
        parts = item.split(":")
        ly = _layer(s, parts[0])
        if len(parts) == 2 and parts[1] == "off":
            ly.pop("bivariate", None)
            done.append(f"{parts[0]}: bivariate off" + ("" if ly.get("value_field") else " (give it a value_field to shade one field)"))
            continue
        if len(parts) < 3:
            raise RestyleError("--bivariate needs LAYER:XFIELD:YFIELD[:CLASSES] (CLASSES 2 or 3), or LAYER:off")
        if ly.get("role") != "choropleth":
            raise RestyleError(f"layer '{parts[0]}' is {ly.get('role')}; a bivariate map is a choropleth of areas")
        bv = {"x": {"field": parts[1]}, "y": {"field": parts[2]}}
        if len(parts) > 3:
            if parts[3] not in ("2", "3"):
                raise RestyleError("the bivariate grid is 2 x 2 or 3 x 3")
            bv["classes"] = int(parts[3])
        ly["bivariate"] = bv
        ly.pop("classification", None)
        done.append(f"{parts[0]}: bivariate {parts[1]} (left to right) by {parts[2]} (bottom to top), {bv.get('classes', 3)} x {bv.get('classes', 3)}")
    for item in getattr(a, "patterns", None) or []:                # --patterns LAYER:auto|on|off (M15)
        lid, _, mode = item.partition(":")
        if mode not in ("auto", "on", "off"):
            raise RestyleError("--patterns needs LAYER:auto, LAYER:on or LAYER:off")
        ly = _layer(s, lid)
        val = {"auto": "auto", "on": True, "off": False}[mode]
        if ly.get("bivariate"):
            ly["bivariate"]["patterns"] = val
        else:
            ly.setdefault("classification", {})["patterns"] = val
        done.append(f"{lid}: hatch patterns {mode}")
    for item in a.set or []:
        import yaml
        k, eq, v = item.partition("=")
        if not eq:
            raise RestyleError("--set needs path=value")
        try:
            val = yaml.safe_load(v)
        except yaml.YAMLError:          # 0.17.0: a brand token such as @brand.categorical.1 is not valid YAML on its own; it is a string
            val = v.strip()
        set_path(s, k, val)
        done.append(f"set {k}")
    return s, done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--off", action="append")
    ap.add_argument("--on", action="append")
    ap.add_argument("--template")
    ap.add_argument("--pack", help="the style pack (default, antique, blueprint, editorial, monochrome, sketch, or one of yours): a full rebuild from the kept data")
    ap.add_argument("--palette", action="append")
    ap.add_argument("--classify", action="append")
    ap.add_argument("--brand")
    ap.add_argument("--basemap")
    ap.add_argument("--title")
    ap.add_argument("--subtitle")
    ap.add_argument("--position", action="append", help="NAME:CORNER, NAME:auto (may move off the data) or NAME:default")
    ap.add_argument("--theme", choices=["light", "dark"])
    ap.add_argument("--context", help="the bundled context pack under the data: auto, none, roads, urban, water or full")
    ap.add_argument("--names", help="place names: on, off, or the tier to show (auto, capitals, major, all)")
    ap.add_argument("--terrain", help="relief under the map: on, off, or contours (relief with contour lines)")
    ap.add_argument("--classes-method", action="append", help="LAYER:METHOD[:K]; log (log-spaced, values above zero) or head_tail (heavy tails) among the methods")
    ap.add_argument("--bivariate", action="append", help="LAYER:XFIELD:YFIELD[:2|3] shades two fields at once on a 3 x 3 (or 2 x 2) grid; LAYER:off undoes it")
    ap.add_argument("--patterns", action="append", help="LAYER:auto|on|off hatch patterns over the classes (auto: only when colours alone are too close)")
    ap.add_argument("--set", action="append")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--build")
    ap.add_argument("--formats")
    a = ap.parse_args(argv)
    path = Path(a.spec)
    if not path.is_file():
        print(f"map_restyle: no such MapSpec: {path}", file=sys.stderr)
        return gc.EXIT_USAGE
    prev = path.with_name(path.name + ".prev")
    if a.undo:
        if not prev.is_file():
            print("map_restyle: nothing to undo", file=sys.stderr)
            return gc.EXIT_USAGE
        cur = path.read_text()
        shutil.copyfile(prev, path)
        prev.write_text(cur)
        print(json.dumps({"undone": True, "spec": str(path)}))
        return gc.EXIT_OK
    manifest = gc.build_manifest()
    spec = gc.load_mapspec(path)
    try:
        new, done = apply(spec, a, manifest)
    except RestyleError as e:
        print(f"map_restyle: {e}", file=sys.stderr)
        return gc.EXIT_USAGE
    if not done:
        print("map_restyle: no change asked for", file=sys.stderr)
        return gc.EXIT_USAGE
    findings = ml.lint_spec(new)
    if any(f["severity"] == "BLOCK" for f in findings):
        print(json.dumps({"written": False, "changes": done, "findings": findings}, indent=2))
        return gc.EXIT_LINT
    shutil.copyfile(path, prev)
    path.write_text(gc.dump_mapspec(new))
    out = {"written": True, "spec": str(path), "changes": done, "findings": findings, "undo": f"{Path(sys.argv[0]).name} --spec {path} --undo"}
    rc = gc.EXIT_OK
    if a.build:
        import build_map
        res = build_map.build(str(path), a.build, a.formats.split(",") if a.formats else None)
        out["build"] = {k: res[k] for k in ("files", "blocked", "findings")}
        rc = gc.EXIT_LINT if res["blocked"] else gc.EXIT_OK
    print(json.dumps(out, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
