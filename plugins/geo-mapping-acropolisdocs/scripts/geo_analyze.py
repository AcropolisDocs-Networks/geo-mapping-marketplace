#!/usr/bin/env python3
"""geo-analyze: one geoprocessing op on a file, written as a new layer file with a report (M11).

    geo_analyze.py <op> --in A.gpkg [--other B.gpkg] [op args] --out C.gpkg [--report report.json] [--json]
    geo_analyze.py ops                       list the ops and their parameters

    --in PATH [--layer L]           the layer the op works on (any format geo-io reads; CSV takes --lat-col/--lon-col/--wkt-col)
    --other PATH [--other-layer L]  the second layer, for ops that need one (clip, overlays, joins, nearest, counts, population)
    --crs-assume C                  CRS to assume for a source that has none
    --out PATH                      the result; the format comes from the extension.  --overwrite  replaces it
    --report PATH                   the full JSON report (notes, findings, provenance, equivalents); --json prints it instead of the summary
    --no-strict                     keep the output when a check blocks

Op arguments are given as flags named like the op parameters: --distance 2 --units km, --rings 1,2,5, --dissolve, --layer is implied by
--other, --predicate within, --how inner, --max-distance 5 --units mi, --k 3, --by market, --agg pop:sum, --field households_covered,
--value households, --mode centroid, --resolution 8, --size 500 --units m, --shape hex, --ratio 0.3, --tolerance 50, --fields a,b,
--prefix x_, --id-field site_id, --inside, --bbox x0 y0 x1 y1, --extent bbox-or-layer, --min-count 2, --cap flat, --join mitre.
Anything else: --param name=value (repeatable).

Every run reads with notes, runs the op, writes, reads the result back and runs the round-trip check (MAP-OUT-005), prints the notes
(the sentences the methods page uses), the findings, the provenance record and the equivalent GeoPandas and PostGIS expressions.
Exit 0 done; 1 a runtime refusal; 2 a usage error; 3 a BLOCK finding (the output is removed unless --no-strict).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import data_lint as dl  # noqa: E402
import geo_io  # noqa: E402
import geo_ops  # noqa: E402
import gmap_common as gc  # noqa: E402

OTHER_ID = "other"
# flags that map straight onto op parameters; (flag, parameter, kind)
PARAM_FLAGS = [
    ("--distance", "distance", "num_or_text"), ("--units", "units", "text"), ("--rings", "rings", "list"), ("--dissolve", "dissolve", "flag"),
    ("--cap", "cap", "text"), ("--join", "join", "text"), ("--segments", "segments", "int"), ("--inside", "inside", "flag"),
    ("--predicate", "predicate", "text"), ("--how", "how", "text"), ("--max-distance", "max_distance", "num_or_text"), ("--fields", "fields", "list"),
    ("--prefix", "prefix", "text"), ("--k", "k", "int"), ("--id-field", "id_field", "text"), ("--exclusive", "exclusive", "flag"),
    ("--by", "by", "text"), ("--agg", "agg", "text"), ("--tolerance", "tolerance", "num_or_text"), ("--geometry", "geometry", "flag"),
    ("--ratio", "ratio", "float"), ("--allow-holes", "allow_holes", "flag"), ("--clip-layer", "clip_layer", "text"), ("--bbox", "bbox", "bbox"),
    ("--extend-km", "extend_km", "float"), ("--field", "field", "text"), ("--value", "value", "text"), ("--mode", "mode", "text"),
    ("--size", "size", "num_or_text"), ("--shape", "shape", "text"), ("--extent", "extent", "text"), ("--min-count", "min_count", "int"),
    ("--resolution", "resolution", "text"),
]


class AnalyzeError(geo_ops.OpsError):
    """A usage error (exit 2): the op, its arguments or the output path."""


def _num_or_text(v):
    try:
        return float(v) if any(c in v for c in ".eE") else int(v)
    except ValueError:
        return v


def op_params(a: argparse.Namespace, op: str) -> dict:
    """The op's parameter dict from the parsed flags."""
    p: dict = {"op": op}
    for flag, name, kind in PARAM_FLAGS:
        v = getattr(a, name, None)
        if v is None or v is False:
            continue
        if kind == "flag":
            p[name] = True
        elif kind == "list":
            p[name] = [x.strip() for x in str(v).split(",") if x.strip()]
        elif kind == "int":
            p[name] = int(v)
        elif kind == "float":
            p[name] = float(v)
        elif kind == "bbox":
            p[name] = [float(x) for x in v]
        elif kind == "num_or_text":
            p[name] = _num_or_text(v)
        else:
            p[name] = v
    if op == "dissolve" and p.get("agg"):
        import geo_convert as gcv
        p["agg"] = gcv.parse_pairs(p["agg"])
    if op == "grid" and p.get("agg") in (None, "count"):
        p["agg"] = "count"
    if op == "overlap" and "layer" not in p and a.other is None:
        p["layer"] = "self"
    if op == "voronoi" and a.other is not None and not p.get("clip_layer"):
        p["clip_layer"] = OTHER_ID
    for kv in a.param or []:
        k, _, v = kv.partition("=")
        if not v:
            raise AnalyzeError(f"--param needs name=value, got '{kv}'")
        try:
            p[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            p[k.strip()] = v
    if a.other is not None and op in geo_ops.NEEDS_LAYER:
        p["layer"] = OTHER_ID
    if "resolution" in p and p["resolution"] != "auto":
        p["resolution"] = int(p["resolution"])
    return p


def analyze(op: str, src: str, out: str, *, other: str | None = None, layer: str | None = None, other_layer: str | None = None,
            params: dict | None = None, crs_assume: str | None = None, overwrite: bool = False, strict: bool = True,
            lat_col=None, lon_col=None, wkt_col=None) -> dict:
    """Run one op from files to a file. Returns the report dict (what --report writes)."""
    op = str(op).lower()
    if op not in geo_ops.OPS:
        raise AnalyzeError(f"unknown op '{op}'. Ops: {', '.join(sorted(geo_ops.OPS))}")
    outp = Path(out)
    fmt = geo_io.detect_format(outp)
    if outp.exists() and not overwrite:
        raise AnalyzeError(f"{outp.name} exists; pass --overwrite to replace it")
    for sp in (src, other):
        if sp and Path(sp).resolve() == outp.resolve():
            raise AnalyzeError("the output would overwrite its own source; choose another output path")
    p = dict(params or {})
    p["op"] = op
    if op in geo_ops.NEEDS_LAYER and not other:
        raise AnalyzeError(f"op '{op}' needs --other, the second layer")
    notes: list[str] = []
    r = geo_io.read(src, layer=layer, crs_assume=crs_assume, lat_col=lat_col, lon_col=lon_col, wkt_col=wkt_col)
    io_notes = [n.as_dict() for n in r.notes]
    in_findings = [x for x in dl.lint_data(r.gdf, assumed=r.crs_assumed) if not (x["id"] == "MAP-DATA-001" and x["severity"] == "INFO")]
    layers = {}
    other_rows = None
    if other:
        ro = geo_io.read(other, layer=other_layer, crs_assume=crs_assume)
        io_notes += [dict(n.as_dict(), source="other") for n in ro.notes]
        in_findings += [dict(x, message="other: " + x["message"]) for x in dl.lint_data(ro.gdf, assumed=ro.crs_assumed) if not (x["id"] == "MAP-DATA-001" and x["severity"] == "INFO")]
        layers[OTHER_ID] = ro.gdf
        other_rows = len(ro.gdf)
        p.setdefault("layer", OTHER_ID)
    fixed = geo_ops.fixed_ids([p])
    if fixed:
        in_findings = [x for x in in_findings if x["id"] not in fixed]           # the op's own finding replaces the input finding it fixes
    if op == "nearest" and other and Path(other).resolve() == Path(src).resolve() and (other_layer or None) == (layer or None):
        p.setdefault("exclusive", True)                                          # a layer against itself: a feature is not its own nearest
    ctx = geo_ops.Ctx(layers=layers, layer_id=Path(src).stem)
    gdf, notes, op_findings = geo_ops.apply_ops(r.gdf, [p], ctx)
    prov = ctx.provenance[0]
    if other:
        prov["other_path"] = str(other)
        prov["other_rows"] = other_rows
    prov["source_path"] = str(src)
    if len(gdf) == 0:
        op_findings.append(dl.finding("MAP-OPS-003", "WARN", "the op left no feature; nothing was written", None)) if not any(f["id"] == "MAP-OPS-003" for f in op_findings) else None
        eq = geo_ops.equivalent(op, p, prov)
        return {"op": op, "output": None, "format": fmt, "features": 0, "notes": notes, "io_notes": io_notes, "findings": in_findings + op_findings,
                "provenance": prov, "equivalent": eq, "blocked": False, "removed": []}
    wr = geo_io.write(gdf, outp, fmt, overwrite=True)
    io_notes += [n.as_dict() for n in wr.notes]
    back = geo_io.read(outp)
    checks = dl.round_trip(gdf, back.gdf, fmt=fmt, field_map=wr.field_map, crs_forced_wgs84=geo_io.FORMATS[fmt].wgs84_only)
    if fmt == "geojson":
        checks += dl.check_geojson_rfc7946(outp)
    if fmt in ("kml", "kmz"):
        checks += dl.check_kml(outp)
    findings = sorted(in_findings + op_findings + checks, key=lambda x: (dl.SEV_ORDER[x["severity"]], x["id"]))
    blocked = any(x["severity"] == "BLOCK" for x in findings)
    removed = []
    if blocked and strict:
        for suffix in ("", ".prj", ".cpg", ".shx", ".dbf"):
            q = outp if suffix == "" else outp.with_suffix(suffix)
            if q.exists():
                q.unlink()
                removed.append(str(q))
    eq = geo_ops.equivalent(op, p, prov)
    return {"op": op, "output": None if (blocked and strict) else {"path": str(outp), "features": wr.features, "crs": wr.crs, "field_map": wr.field_map},
            "format": fmt, "features": len(gdf), "fields": [c for c in gdf.columns if c != gdf.geometry.name], "notes": notes, "io_notes": io_notes,
            "findings": findings, "provenance": prov, "equivalent": eq, "blocked": blocked, "removed": removed}


def print_summary(rep: dict) -> None:
    for n in rep["notes"]:
        print(f"note: {n}")
    for n in rep.get("io_notes", []):
        if n.get("sev") == "WARN":
            print(f"note: {n['msg']}")
    for f in rep["findings"]:
        print(f"{f['id']} {f['severity']}: {f['message']}")
    o = rep.get("output")
    if o:
        print(f"wrote {o['path']}: {o['features']} feature(s), {o['crs'] or 'no CRS'}" + (f"; fields: {', '.join(rep.get('fields') or [])}" if rep.get("fields") else ""))
    elif rep.get("removed"):
        print("output removed: a finding blocked it (see above)")
    else:
        print("nothing written")
    print("provenance: " + json.dumps(rep["provenance"], default=str))
    print("equivalent (geopandas): " + rep["equivalent"]["geopandas"])
    print("equivalent (postgis):   " + rep["equivalent"]["postgis"])


def list_ops() -> str:
    lines = []
    seen = set()
    for name, fn in geo_ops.OPS.items():
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        lines.append(f"{name:22s} {doc}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", help="the op (geo_analyze.py ops lists them)")
    ap.add_argument("--in", dest="src")
    ap.add_argument("--layer")
    ap.add_argument("--other")
    ap.add_argument("--other-layer")
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--crs-assume")
    ap.add_argument("--lat-col")
    ap.add_argument("--lon-col")
    ap.add_argument("--wkt-col")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-strict", action="store_true")
    ap.add_argument("--param", action="append")
    for flag, name, kind in PARAM_FLAGS:
        if kind == "flag":
            ap.add_argument(flag, dest=name, action="store_true")
        elif kind == "bbox":
            ap.add_argument(flag, dest=name, nargs=4, type=float)
        else:
            ap.add_argument(flag, dest=name)
    a = ap.parse_args(argv)
    if a.op == "ops":
        print(list_ops())
        return gc.EXIT_OK
    if not a.src or not a.out:
        print("geo-analyze: --in and --out are required", file=sys.stderr)
        return gc.EXIT_USAGE
    try:
        params = op_params(a, a.op.lower())
        rep = analyze(a.op, a.src, a.out, other=a.other, layer=a.layer, other_layer=a.other_layer, params=params, crs_assume=a.crs_assume,
                      overwrite=a.overwrite, strict=not a.no_strict, lat_col=a.lat_col, lon_col=a.lon_col, wkt_col=a.wkt_col)
    except geo_io.AmbiguousColumns as e:
        print(json.dumps({"error": str(e), "candidates": e.candidates}, indent=2))
        return gc.EXIT_USAGE
    except geo_ops.MeasureCRSError as e:
        print(f"geo-analyze: {e.finding['id']} {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    except geo_ops.OpsError as e:
        print(f"geo-analyze: {e}", file=sys.stderr)
        return gc.EXIT_USAGE
    except geo_io.GeoIOError as e:
        print(f"geo-analyze: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print_summary(rep)
    return gc.EXIT_LINT if rep["blocked"] and not a.no_strict else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
