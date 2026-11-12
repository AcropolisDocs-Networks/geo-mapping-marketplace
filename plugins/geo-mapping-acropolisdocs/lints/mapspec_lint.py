"""Spec lint: MAP-SPEC-001 to 007 (spec 12.1; 006 is M10's: classification keys that contradict each other; 007 is M11's: a layer's
`ops:` naming a layer the spec does not have, naming itself, or a `draw: false` helper no op uses). A layer's `ops:` list is also checked by
`ops_lint.lint_ops_spec` here (MAP-OPS-008: an unknown op, a missing `layer:`, `value:`, `distance`, `size` or `tolerance`), before any data is read.

    mapspec_lint.py MAP.yaml           prints findings as JSON; exit 3 on a BLOCK

Reads a MapSpec with the YAML 1.2 loader (`gmap_common.load_mapspec`), so `on:` stays a key.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402
import ops_lint  # noqa: E402

CATEGORICAL_METHODS = {"categorical", "unique", "categories"}      # classify.ALIASES maps the last two to categorical
LAYER_REF_KEYS = ("layer", "clip_layer", "to")                     # the keys of an op that name another layer of the spec (`to`: line_of_sight, M12)


def op_layer_refs(layer: dict) -> list[str]:
    """The ids of the other layers a layer's ops name (`layer:`, `clip_layer:`), in order, without repeats."""
    out: list[str] = []
    for o in (layer.get("ops") or []) if isinstance(layer.get("ops"), list) else []:
        if not isinstance(o, dict):
            continue
        for k in LAYER_REF_KEYS:
            v = o.get(k)
            if isinstance(v, str) and v and v not in out:
                out.append(v)
    return out

ELEMENTS = {"title", "legend", "north_arrow", "scale_bar", "graticule", "neatline", "inset_map", "logo", "source_note", "attribution",
            "date_stamp", "crs_note", "disclaimer", "confidentiality_banner", "trademark_line", "callouts", "data_table", "methods_page",
            "names", "context", "terrain", "frame"}   # names, context and terrain are top-level blocks too; elements.<name> carries the on/off toggle (1.4.0: frame)
PALETTES = {"brand_sequential", "brand_diverging", "brand_categorical", "brand_neutral"}
POSITIONS = {"upper-left", "upper-right", "lower-left", "lower-right", "auto"}      # auto: the layout may move it off the data
PLACEHOLDER_MESSAGES = {"map", "a map", "title", "untitled", "todo", "tbd", "lorem ipsum", "test", "message", "my map", "new map"}
ROLES_NEEDING_VALUE = {"choropleth"}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def template_names() -> list[str]:
    d = gc.plugin_root() / "templates"
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def lint_spec(spec: dict) -> list[dict]:
    out: list[dict] = []
    # 001 schema
    try:
        import jsonschema
        schema = json.loads((gc.plugin_root() / "schemas" / "mapspec.schema.json").read_text())
        for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(spec), key=lambda e: list(e.path)):
            where = "/".join(str(p) for p in e.path) or "(root)"
            out.append(finding("MAP-SPEC-001", "BLOCK", f"{where}: {e.message[:160]}", where))
    except Exception as ex:  # noqa: BLE001
        hint = " (jsonschema 4.0 or later is needed: run map-doctor --fix)" if isinstance(ex, (ImportError, AttributeError)) else ""
        out.append(finding("MAP-SPEC-001", "BLOCK", f"the MapSpec could not be checked against its schema: {ex}{hint}"))
        return out
    if not isinstance(spec, dict):
        return out
    # 002 message
    msg = str(spec.get("message") or "").strip()
    if msg and (msg.lower().rstrip(".") in PLACEHOLDER_MESSAGES or len(msg.split()) < 3):
        out.append(finding("MAP-SPEC-002", "BLOCK", "the message is a placeholder; state what the map should make the reader see", msg))
    elif not msg:
        out.append(finding("MAP-SPEC-002", "BLOCK", "the message is missing; a map needs a point (design principle 3)"))
    # 003 ids and references
    layers = spec.get("layers") or []
    ids = [ly.get("id") for ly in layers if isinstance(ly, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        out.append(finding("MAP-SPEC-003", "BLOCK", f"layer ids must be unique: {', '.join(map(str, dup))}", dup))
    for lb in spec.get("labels") or []:
        if isinstance(lb, dict) and lb.get("layer") not in ids:
            out.append(finding("MAP-SPEC-003", "BLOCK", f"a label refers to layer '{lb.get('layer')}', which does not exist", lb.get("layer")))
    for ly in layers:
        if not isinstance(ly, dict):
            continue
        pal = (ly.get("palette") or {}).get("name")
        if pal and pal not in PALETTES:
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': palette '{pal}' is not one of {', '.join(sorted(PALETTES))}", pal))
        if ly.get("role") in ROLES_NEEDING_VALUE and not ly.get("value_field") and not ly.get("bivariate"):
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}' is a choropleth and needs value_field (or bivariate: {{x: field, y: field}})", ly.get("id")))
        bv = ly.get("bivariate")
        if bv is not None:                    # a bivariate choropleth (M15): two different fields, a 2 x 2 or 3 x 3 grid, on a choropleth
            if ly.get("role") != "choropleth":
                out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': bivariate is for role choropleth, not {ly.get('role')}", ly.get("id")))
            fx = (bv.get("x") or {}).get("field") if isinstance(bv, dict) and isinstance(bv.get("x"), dict) else (bv.get("x") if isinstance(bv, dict) else None)
            fy = (bv.get("y") or {}).get("field") if isinstance(bv, dict) and isinstance(bv.get("y"), dict) else (bv.get("y") if isinstance(bv, dict) else None)
            if not fx or not fy:
                out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': bivariate needs x and y fields (bivariate: {{x: field_a, y: field_b}})", ly.get("id")))
            elif fx == fy:
                out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': bivariate x and y are both '{fx}'; use two different fields", fx))
            if isinstance(bv, dict) and bv.get("classes") is not None and bv.get("classes") not in (2, 3):
                out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': bivariate.classes is 2 or 3, not {bv.get('classes')}", bv.get("classes")))
            if ly.get("classification"):
                out.append(finding("MAP-SPEC-006", "WARN", f"layer '{ly.get('id')}': classification is ignored on a bivariate layer; give each axis its method "
                                   "(bivariate.x.method, bivariate.y.method)", ly.get("id")))
            for ax in ("x", "y"):
                m = (bv.get(ax) or {}).get("method") if isinstance(bv, dict) and isinstance(bv.get(ax), dict) else None
                if m and m in CATEGORICAL_METHODS:
                    out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': bivariate.{ax}.method '{m}' is categorical; a bivariate axis is numeric", m))
        src = ly.get("source") or {}
        if src.get("type") == "file" and not src.get("path"):
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': a file source needs a path", ly.get("id")))
        if src.get("type") == "ref" and not src.get("id"):
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': a library source needs an id", ly.get("id")))
        if src.get("cds") and ly.get("role") != "raster":             # 1.3.0: a CDS job's variable is a grid
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': a CDS source (source.cds) is a raster: give the layer role: raster", ly.get("id")))
        cm = (ly.get("classification") or {}).get("method")
        if cm:
            from_classify = {"quantile", "equal_interval", "jenks", "std_dev", "manual", "categorical", "equal", "natural_breaks",
                             "natural", "stddev", "sd", "quantiles", "fisher_jenks", "unique", "categories", "pretty", "geometric",
                             "equal_count", "equal_counts", "equal_frequency", "count", "nice", "rounded", "round", "even", "even_interval",
                             "geometric_interval", "geo", "log", "log10", "logarithmic", "log_scale", "head_tail", "headtail", "head-tail",
                             "head_tail_breaks", "ht"}
            if cm not in from_classify:
                out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}': unknown classification method '{cm}'", cm))
        # 006 classification keys that contradict each other (M10)
        cl = ly.get("classification") or {}
        if isinstance(cl, dict):
            if cl.get("normalize") and cl.get("denominator"):
                out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': classification gives both normalize ({cl['normalize']}) and denominator "
                                   f"({cl['denominator']}); a value is divided by its area or by a column, not both. Choose one", {"normalize": cl["normalize"], "denominator": cl["denominator"]}))
            if cl.get("midpoint") is not None and str(cl.get("method")) in CATEGORICAL_METHODS:
                out.append(finding("MAP-SPEC-006", "BLOCK", f"layer '{ly.get('id')}': classification.midpoint ({cl['midpoint']}) has no meaning for a categorical method; "
                                   "a midpoint centres a diverging palette on a number. Drop the midpoint or classify a numeric field", {"midpoint": cl["midpoint"], "method": cl.get("method")}))
        if ly.get("role") == "grid" and (ly.get("grid") or {}).get("size") is None:
            out.append(finding("MAP-SPEC-003", "BLOCK", f"layer '{ly.get('id')}' is a grid and needs grid: {{size: <distance with units>}}", ly.get("id")))
        # MAP-OPS-008: the ops list, checked before any data is read (M11)
        for f_ in ops_lint.lint_ops_spec(ly.get("ops")):
            out.append({**f_, "message": f"layer '{ly.get('id')}': {f_['message']}", "layer": ly.get("id")})
    # 007 ops that name a layer the spec does not have, or the layer itself; helpers no op uses; a dependency cycle (M11)
    out += lint_ops_refs(layers, ids, spec)
    # 004 element typos
    for key, val in (spec.get("elements") or {}).items():
        if key not in ELEMENTS:
            near = _nearest(key)
            out.append(finding("MAP-SPEC-004", "WARN", f"unknown element '{key}'" + (f"; did you mean '{near}'?" if near else ""), key))
        elif isinstance(val, dict) and val.get("position") and val["position"] not in POSITIONS:
            out.append(finding("MAP-SPEC-004", "WARN", f"element '{key}': position '{val['position']}' is not one of {', '.join(sorted(POSITIONS))}", key))
    # 005 template exists (region overflow is checked by the layout)
    tpl = spec.get("template")
    if isinstance(tpl, dict) or (isinstance(tpl, str) and tpl.lower().startswith("custom:")):      # a custom sheet (M16)
        import layout as lo
        try:
            w, h = lo.custom_size(tpl)
            for msg in lo.custom_problems(w, h):
                out.append(finding("MAP-SPEC-005", "BLOCK", msg, tpl))
        except lo.LayoutError as ex:
            out.append(finding("MAP-SPEC-005", "BLOCK", str(ex), tpl))
    elif tpl and tpl != "auto" and tpl not in template_names():          # 1.5.0: auto is Letter or A-series by country
        out.append(finding("MAP-SPEC-005", "BLOCK", f"template '{tpl}' does not exist; choose one of {', '.join(template_names())}, or a custom sheet {{custom: {{w_mm, h_mm}}}}", tpl))
    return out


def element_layer_refs(spec: dict | None) -> set:
    """1.2.0 (Day 9 issue 1): the layers the map's elements read: the data table, the callouts that sit on a feature."""
    els = (spec or {}).get("elements") or {}
    out = set()
    dt_ = els.get("data_table") or {}
    if isinstance(dt_, dict) and dt_.get("layer"):
        out.add(dt_["layer"])
    for it in ((els.get("callouts") or {}).get("items") or []) if isinstance(els.get("callouts"), dict) else []:
        if isinstance(it, dict) and it.get("layer"):
            out.add(it["layer"])
    return out


def lint_ops_refs(layers: list, ids: list, spec: dict | None = None) -> list[dict]:
    """MAP-SPEC-007: every `layer:` / `clip_layer:` an op names must be another layer of the spec (BLOCK); a layer whose ops name itself is a
    BLOCK (an overlap of a layer with itself leaves `layer` out); a `draw: false` helper no op uses is a WARN (it would be loaded for nothing);
    layers whose ops need each other are a BLOCK (no load order can satisfy them)."""
    out: list[dict] = []
    known = {i for i in ids if i is not None}
    refs: dict[str, list[str]] = {}
    for ly in layers:
        if not isinstance(ly, dict) or ly.get("id") is None:
            continue
        lid = ly["id"]
        refs[lid] = op_layer_refs(ly)
        for r in refs[lid]:
            if r == lid:
                out.append(finding("MAP-SPEC-007", "BLOCK", f"layer '{lid}': an op names the layer itself; leave `layer` out to work a layer against itself (overlap), or name another layer", {"layer": lid, "ref": r}))
            elif r not in known:
                out.append(finding("MAP-SPEC-007", "BLOCK", f"layer '{lid}': an op names layer '{r}', which the spec does not have (layers: {', '.join(sorted(known))})", {"layer": lid, "ref": r}))
    used = {r for rs in refs.values() for r in rs} | element_layer_refs(spec)
    for ly in layers:
        if isinstance(ly, dict) and ly.get("draw") is False and ly.get("id") not in used:
            out.append(finding("MAP-SPEC-007", "WARN", f"layer '{ly.get('id')}' has draw: false but no op of another layer, the data table or a callout names it; it would be loaded and never used", ly.get("id")))
    cycle = ops_cycle(refs)
    if cycle:
        out.append(finding("MAP-SPEC-007", "BLOCK", f"layers' ops depend on each other in a cycle ({' -> '.join(cycle)}); no load order can satisfy them", cycle))
    return out


def ops_cycle(refs: dict) -> list[str] | None:
    """The first cycle in the layer dependency graph (`refs`: layer id -> ids its ops name) as [a, b, ..., a], or None."""
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str):
        state[n] = 1
        path.append(n)
        for m in refs.get(n, []):
            if m not in refs:
                continue
            if state.get(m) == 1:
                return path[path.index(m):] + [m]
            if state.get(m) is None:
                c = visit(m)
                if c:
                    return c
        path.pop()
        state[n] = 2
        return None

    for n in refs:
        if state.get(n) is None:
            c = visit(n)
            if c:
                return c
    return None


def _nearest(key: str) -> str | None:
    import difflib
    m = difflib.get_close_matches(key, sorted(ELEMENTS), n=1, cutoff=0.6)
    return m[0] if m else None


def worst(findings: list[dict]) -> int:
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in findings) else gc.EXIT_OK


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return gc.EXIT_USAGE
    path = Path(args[0])
    spec = gc.load_mapspec(path)
    f = lint_spec(spec)
    print(json.dumps({"spec": str(path), "findings": f}, indent=2))
    return worst(f)


if __name__ == "__main__":
    sys.exit(main())
