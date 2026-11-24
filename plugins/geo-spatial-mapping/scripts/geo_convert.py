#!/usr/bin/env python3
"""geo-convert: the ogr2ogr workhorse (spec 7). Convert, reproject, filter, clip, select, rename, dissolve, simplify, merge, split.

    geo_convert.py SRC [SRC ...] -o OUT [options]

    -o OUT              output path; the format comes from the extension (--format overrides)
    --layer L           layer to read from the first source;  --out-layer N  layer name to write
    --crs-assume C      CRS to assume for a source that has none
    --t-srs C           reproject to C
    --bbox x0 y0 x1 y1  keep features that intersect the box (source CRS; ogr2ogr -spat);  --clip also cuts geometries to it
    --where SQL         OGR SQL attribute filter (ogr2ogr -where)
    --select a,b        keep these attributes;  --rename old:new,old2:new2
    --dissolve FIELD    merge features sharing a value;  --agg pop:sum,area:mean  (sum, mean, min, max, first, count)
    --simplify TOL      simplify geometry, in the units of the output CRS, keeping shared borders where it can
    --split-by FIELD    one output per value (layers in one GeoPackage, files for other formats)
    --repair            make invalid geometry valid (make_valid), drop empty geometry and reduce GeometryCollections to their
                        polygons, lines or points; the counts and the area change are reported (MAP-DATA-002), and a repair that
                        moves more than 1 percent of the area blocks (the data needs looking at, not a silent fix)
    several SRC         are merged (reprojected to the first source's CRS; attributes are unioned)
    --overwrite         replace an existing output;  --no-strict  keep the output even when a check blocks

Styled and tiled outputs (M14):
    --kml-style STYLE.json    a .kml or .kmz written with styles: classes and colours, widths, point icons (inside a KMZ), balloons
                              (style_dict's style; colours as @brand.* tokens or #rrggbb); the styles are read back and compared (MAP-OUT-010).
                              --kml-legend adds the legend as a screen overlay
    --format pmtiles|mbtiles  (or -o out.pmtiles / out.mbtiles) a vector tile set through GDAL's drivers, in Web Mercator;
                              --minzoom N (default 0) --maxzoom N|auto (default auto, from the data); read back at the top zoom (MAP-OUT-009)

Geoprocessing (geo_ops, M11), applied after rename and before dissolve, in this order:
    --repair                  make invalid geometry valid (make_valid; the fix for MAP-DATA-002)
    --clip-layer PATH[:LAYER] cut the features to the polygons of another file (clip by polygon, not by box)
    --antimeridian-split      split geometry that crosses 180 degrees into one part per side (the fix for MAP-DATA-009)
    --explode                 one feature per part of a multi-part geometry
    --dedupe                  drop features whose geometry repeats another's
    --buffer D [--buffer-units m|km|mi|ft]   buffer every feature (computed in a projected CRS, never in degrees)
    --area FIELD [--area-units km2|mi2|m2|acres|ha]   add a geodesic area field;  --length FIELD [--length-units km|mi|m|ft]
    --op JSON                 any geo_ops op as JSON, e.g. '{"op": "centroid", "inside": true}' (repeatable; run after the flags above)

Every run: reads with notes, applies the steps in the order above, writes, reads the result back and runs the round-trip check
(MAP-OUT-005) plus the RFC 7946 (MAP-OUT-007) or KML (MAP-OUT-006) check where they apply. A BLOCK removes the output and exits
3, unless --no-strict. The JSON report carries the equivalent ogr2ogr command(s), so the conversion can be rerun locally; geoprocessing
steps have no ogr2ogr form, so the report's `ops` list carries their provenance and the GeoPandas and PostGIS equivalents instead.

Library use: `repair(gdf) -> (gdf, note)` is the same repair as --repair, for the planner's `repair: true` layer op.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import crs_engine as ce  # noqa: E402
import data_lint as dl  # noqa: E402
import geo_io  # noqa: E402
import gmap_common as gc  # noqa: E402

AGG = {"sum", "mean", "min", "max", "first", "count", "median"}
SPLIT_MAX = 500


class ConvertError(geo_io.GeoIOError):
    pass


def parse_pairs(text: str | None, sep: str = ":") -> dict:
    out = {}
    for part in (text or "").split(","):
        if part.strip():
            k, _, v = part.partition(sep)
            if not v:
                raise ConvertError(f"expected name{sep}value, got '{part}'")
            out[k.strip()] = v.strip()
    return out


def _safe_name(value) -> str:
    if value is None or value != value:
        return "null"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return (s or "null")[:60]


# --------------------------------------------------------------------------- operations

def merge(frames: list, notes: list):
    import geopandas as gpd
    import pandas as pd
    base_crs = frames[0].crs
    out = []
    for i, f in enumerate(frames):
        if f.crs is None or base_crs is None:
            if f.crs is not base_crs:
                raise ConvertError("cannot merge a source that has a CRS with one that has none; assume a CRS for it first")
        elif not f.crs.equals(base_crs):
            notes.append(geo_io.Note("IO", "INFO", f"source {i + 1} was reprojected from {f.crs.to_string()} to {base_crs.to_string()} to merge"))
            f = f.to_crs(base_crs)
        out.append(f)
    cols = [dict.fromkeys(c for c in f.columns if c != f.geometry.name) for f in out]
    union = list(dict.fromkeys(k for c in cols for k in c))
    for i, c in enumerate(cols):
        miss = [k for k in union if k not in c]
        if miss:
            notes.append(geo_io.Note("IO", "INFO", f"source {i + 1} has no field(s) {', '.join(miss)}; they are empty for its features"))
    # each frame keeps its own active geometry column name (a GeoParquet or a renamed frame need not call it "geometry");
    # the merge carries the first source's name and every other frame is renamed to it, so no frame concatenates as a plain column
    geom_name = out[0].geometry.name
    aligned = []
    for f in out:
        if f.geometry.name != geom_name:
            f = f.rename_geometry(geom_name) if geom_name not in f.columns else f.drop(columns=[geom_name]).rename_geometry(geom_name)
        aligned.append(f)
    merged = gpd.GeoDataFrame(pd.concat(aligned, ignore_index=True), geometry=geom_name, crs=base_crs)
    return merged


def repair(gdf):
    """Make a layer drawable: make_valid on invalid geometry, GeometryCollections reduced to their polygons, lines or points, and empty or
    missing geometry dropped. Returns (gdf, note); note.detail carries the counts, the area change and `block` (True when the repair moved
    more than 1 percent of the area, which is a data problem to look at rather than something to fix quietly).

    This is the one repair path: `geo-convert --repair` and the planner's `repair: true` layer op both call it, so a repaired layer is the
    same whichever way it was repaired, and the note reads the same."""
    import shapely
    before_n = len(gdf)
    fixed, findings = dl.repair_geometry(gdf)
    n_fixed = int(sum((f.get("detail") or {}).get("repaired", 0) for f in findings if isinstance(f.get("detail"), dict)))
    change = max((float((f.get("detail") or {}).get("area_change", 0.0)) for f in findings if isinstance(f.get("detail"), dict)), default=0.0)
    block = any(f["severity"] == "BLOCK" for f in findings)
    fixed, n_coll = dl.reduce_collections(fixed)
    empty = fixed.geometry.isna().values | shapely.is_empty(fixed.geometry.values)
    n_empty = int(empty.sum())
    if n_empty:
        fixed = fixed[~empty].reset_index(drop=True)
    parts = []
    if n_fixed:
        parts.append(f"{n_fixed} invalid geometr{'y was' if n_fixed == 1 else 'ies were'} made valid (area changed by {change * 100:.3f} percent)")
    if n_coll:
        parts.append(f"{n_coll} geometry collection(s) reduced to their polygons, lines or points")
    if n_empty:
        parts.append(f"{n_empty} feature(s) with empty geometry dropped ({before_n} became {len(fixed)})")
    msg = "repair: " + ("; ".join(parts) if parts else "nothing to repair")
    if block:
        msg += ". The repair moved more than 1 percent of the area; check the data before using it"
    note = geo_io.Note("MAP-DATA-002", "WARN" if block else "INFO", msg,
                       {"repaired": n_fixed, "area_change": change, "collections_reduced": n_coll, "empties_dropped": n_empty, "block": block})
    return fixed, note


def dissolve(gdf, field: str, agg: dict, notes: list):
    if field not in gdf.columns:
        raise ConvertError(f"dissolve field '{field}' is not in the data. Fields: {', '.join(c for c in gdf.columns if c != gdf.geometry.name)}")
    for col, fn in agg.items():
        if col not in gdf.columns:
            raise ConvertError(f"aggregate field '{col}' is not in the data")
        if fn not in AGG:
            raise ConvertError(f"unknown aggregate '{fn}'. Use one of: {', '.join(sorted(AGG))}")
    before = len(gdf)
    keep = [field] + list(agg) + [gdf.geometry.name]
    out = gdf[keep].dissolve(by=field, aggfunc=agg or "first", as_index=False)
    dropped = [c for c in gdf.columns if c not in keep]
    notes.append(geo_io.Note("IO", "INFO", f"dissolved {before} features into {len(out)} by '{field}'"
                              + (f"; fields not aggregated were dropped: {', '.join(dropped)}" if dropped else "")))
    return out


def clip(gdf, bbox, notes: list):
    from shapely.geometry import box
    n = len(gdf)
    out = gdf.clip(box(*bbox))
    out = out[~out.geometry.is_empty].reset_index(drop=True)
    notes.append(geo_io.Note("IO", "INFO", f"clipped to the box; {n} features became {len(out)}"))
    return out


def simplify(gdf, tol: float, notes: list):
    """Simplify to a tolerance in the CRS units. Polygon layers keep shared borders (topology) when topojson can do it."""
    if tol <= 0:
        raise ConvertError("the simplify tolerance must be greater than zero")
    v0 = dl.vertex_count(gdf)
    unit = "degrees" if gdf.crs is not None and gdf.crs.is_geographic else "CRS units"
    fam = {g.geom_type.replace("Multi", "") for g in gdf.geometry if g is not None}
    method = "per feature (Douglas-Peucker, topology kept inside each feature)"
    out = None
    why_not = None            # why the topology-aware path was not used: named in its own note, never swallowed
    if fam == {"Polygon"} and len(gdf) > 1:
        try:
            import topojson as tp
            topo = tp.Topology(gdf, topology=True, prequantize=False, toposimplify=tol)
            out = topo.to_gdf(crs=gdf.crs)
            if set(gdf.columns) - {gdf.geometry.name} <= set(out.columns):
                out = out[gdf.drop(columns=gdf.geometry.name).columns.tolist() + [out.geometry.name]]
            else:
                out, why_not = None, "the topojson package returned the features without their attributes"
            if out is not None and len(out) != len(gdf):
                out, why_not = None, f"the topojson package returned {len(out)} features for {len(gdf)}"
            else:
                method = "shared borders kept (topology-aware)"
        except ImportError:
            why_not = "the topojson package is not installed"
        except Exception as ex:  # noqa: BLE001 - a failed topology build falls back; the reason is reported
            out, why_not = None, f"the topojson package failed: {type(ex).__name__}: {str(ex)[:120]}"
    if out is None:
        out = gdf.copy()
        out[gdf.geometry.name] = gdf.geometry.simplify(tol, preserve_topology=True)
        method = "per feature (Douglas-Peucker; shared borders between neighbours may open slightly)"
        if why_not:
            notes.append(geo_io.Note("IO", "WARN", f"shared borders could not be kept while simplifying: {why_not}; each feature was simplified on its own", why_not))
    v1 = dl.vertex_count(out)
    notes.append(geo_io.Note("IO", "INFO", f"simplified with a tolerance of {tol} {unit}: {v0} to {v1} vertices; {method}. Disclose this on the map."))
    return out


def rename(gdf, mapping: dict, notes: list):
    bad = [k for k in mapping if k not in gdf.columns]
    if bad:
        raise ConvertError(f"cannot rename missing field(s): {', '.join(bad)}")
    clash = [v for v in mapping.values() if v in gdf.columns and v not in mapping]
    if clash:
        raise ConvertError(f"rename would collide with existing field(s): {', '.join(clash)}")
    notes.append(geo_io.Note("IO", "INFO", "renamed fields: " + ", ".join(f"{k} to {v}" for k, v in mapping.items())))
    return gdf.rename(columns=mapping)


# --------------------------------------------------------------------------- equivalent commands

SQL_AGG = {"sum": "SUM", "mean": "AVG", "min": "MIN", "max": "MAX", "first": "MIN", "count": "COUNT"}


def equivalents(args: dict, src: list[str], dst: str, fmt: str, split_values: list | None = None) -> dict:
    """The ogr2ogr command(s) that do what this run did, plus what they cannot do.

    A dissolve becomes a SQLite-dialect GROUP BY with ST_Union. SQLite has no MEDIAN() aggregate (and ogr2ogr's SQLite dialect cannot
    take a window-function median inside a GROUP BY), so a `median` aggregate is left out of the SQL and a caveat says so; `first` is
    written as MIN, the nearest thing SQL offers, and the caveat says that too."""
    import shlex
    cmds: list[str] = []
    caveats: list[str] = []
    if fmt == "topojson":
        return {"commands": [], "caveats": ["ogr2ogr cannot write TopoJSON; use the topojson Python package or mapshaper"]}
    where = args.get("where")
    first = src[0]
    sel = args.get("select")
    layer = args.get("out_layer")
    if args.get("dissolve"):
        fld = args["dissolve"]
        aggs = args.get("agg") or {}
        medians = [c for c, fn in aggs.items() if fn == "median"]
        parts = ["ST_Union(geometry) AS geometry", fld] + [f"{SQL_AGG[fn]}({c}) AS {c}" for c, fn in aggs.items() if fn in SQL_AGG]
        lay = Path(first).stem
        sql = f"SELECT {', '.join(parts)} FROM \"{lay}\"" + (f" WHERE {where}" if where else "") + f" GROUP BY {fld}"
        base, cav = geo_io.ogr2ogr_command(first, dst, t_srs=args.get("t_srs"), s_srs=args.get("crs_assume"), bbox=args.get("bbox"),
                                           simplify=args.get("simplify"), layer=layer, dst_format=fmt)
        cmds.append(base.replace(shlex.quote(str(dst)), f"{shlex.quote(str(dst))}", 1) + " -dialect SQLite -sql " + shlex.quote(sql))
        caveats += cav + ["dissolve uses the SQLite dialect (ST_Union); the layer name in the SQL must match the layer in the file"]
        if medians:
            caveats.append(f"SQLite has no MEDIAN() aggregate, so the median of {', '.join(medians)} is not in the SQL; compute it in Python (this tool did) or with a window function in a second query")
        if any(fn == "first" for fn in aggs.values()):
            caveats.append("`first` has no SQL aggregate; MIN() stands in for it in the SQL, which is the same only when the group's values are equal")
    else:
        base, cav = geo_io.ogr2ogr_command(first, dst, t_srs=args.get("t_srs"), s_srs=args.get("crs_assume"), where=where, select=sel,
                                           bbox=args.get("bbox"), simplify=args.get("simplify"), layer=layer, dst_format=fmt)
        cmds.append(base)
        caveats += cav
    if args.get("rename"):
        caveats.append("renaming fields in ogr2ogr needs -sql with AS aliases: SELECT old AS new, ... FROM layer")
    if args.get("clip"):
        caveats.append("clipping geometries (not just selecting by box) needs -clipsrc with the same box")
        cmds[0] += " -clipsrc " + " ".join(str(v) for v in args["bbox"])
    if len(src) > 1:
        for extra in src[1:]:
            e, cav2 = geo_io.ogr2ogr_command(extra, dst, t_srs=args.get("t_srs") or "<crs of the first source>", where=where, select=sel, layer=layer, dst_format=fmt)
            cmds.append(e.replace("ogr2ogr ", "ogr2ogr -update -append ", 1))
        caveats.append("merging with ogr2ogr appends each source in turn; attributes missing from a source are left empty")
    if args.get("split_by") and split_values is not None:
        loop = [f"# one ogr2ogr per value of {args['split_by']}:"]
        for v in split_values[:5]:
            out = f"{Path(dst).with_suffix('')}_{_safe_name(v)}{Path(dst).suffix}"
            loop.append(f"ogr2ogr -f {geo_io.OGR_DRIVER.get(fmt, fmt)} {shlex.quote(out)} {shlex.quote(first)} -where {shlex.quote(args['split_by'] + ' = ' + repr(str(v)))}")
        if len(split_values) > 5:
            loop.append(f"# ... and {len(split_values) - 5} more")
        cmds += loop
    return {"commands": cmds, "caveats": caveats}


# --------------------------------------------------------------------------- the run

def convert(sources: list[str], out: str, *, fmt: str | None = None, layer: str | None = None, out_layer: str | None = None,
            crs_assume: str | None = None, t_srs: str | None = None, bbox=None, do_clip: bool = False, where: str | None = None,
            select: list[str] | None = None, rename_map: dict | None = None, dissolve_by: str | None = None, agg: dict | None = None,
            simplify_tol: float | None = None, split_by: str | None = None, overwrite: bool = False, strict: bool = True,
            lat_col=None, lon_col=None, wkt_col=None, do_repair: bool = False, ops: list | None = None, clip_layer: str | None = None,
            minzoom: int | None = None, maxzoom=None, kml_style=None, kml_legend: bool = False, engine: str | None = None) -> dict:
    """engine: auto | pandas | duckdb, what runs --where on a CSV or GeoParquet source (duck_engine, M16); the report says which ran.
    ops: geo_ops op dicts ({"op": name, ...}) run after rename and before dissolve; clip_layer: PATH[:LAYER] of a polygon file,
    available to the ops as layer 'clip' (a {"op": "clip", "layer": "clip"} op is prepended when no op names it).
    minzoom, maxzoom: the zooms of a pmtiles or mbtiles output; kml_style: a style dict (or a path to one) for a styled .kml or .kmz."""
    outp = Path(out)
    fmt = fmt or geo_io.detect_write_format(outp)
    notes: list = []
    tiled = fmt in geo_io.TILE_FORMATS
    if (minzoom is not None or maxzoom is not None) and not tiled:
        raise ConvertError("--minzoom and --maxzoom are for a tile set output (.pmtiles or .mbtiles)")
    style = None
    if kml_style is not None:
        if fmt not in ("kml", "kmz"):
            raise ConvertError("--kml-style is for a .kml or .kmz output")
        import style_dict
        try:
            style = kml_style if isinstance(kml_style, dict) else style_dict.load_style(kml_style)
        except style_dict.StyleError as e:
            raise ConvertError(str(e)) from None
    if outp.exists() and not overwrite:
        raise ConvertError(f"{outp.name} exists; pass --overwrite to replace it")
    if do_clip and not bbox:
        raise ConvertError("--clip needs --bbox")
    if dissolve_by and split_by:
        raise ConvertError("use either --dissolve or --split-by in one run")
    for sp in sources:
        if Path(sp).resolve() == outp.resolve():
            raise ConvertError("the output would overwrite its own source; choose another output path")

    select_in_source = list(select) if select else None      # the ogr2ogr equivalent runs on the source, before any rename
    need = None
    if select:
        need = list(dict.fromkeys(select + ([dissolve_by] if dissolve_by else []) + ([split_by] if split_by else []) + list((agg or {}).keys())))
    frames, read_notes, engines = [], [], []
    for i, sp in enumerate(sources):
        r = geo_io.read(sp, layer=layer if i == 0 else None, crs_assume=crs_assume, lat_col=lat_col, lon_col=lon_col, wkt_col=wkt_col,
                        where=where, bbox=bbox, columns=need, engine=engine)
        frames.append(r.gdf)
        read_notes += r.notes
        if r.engine:
            engines.append({"source": Path(sp).name, **r.engine})
    notes += read_notes
    in_findings = []
    if do_repair:
        # repaired before the input lint, so MAP-DATA-002 reports what was done rather than what was wrong
        repaired = []
        for i, f in enumerate(frames):
            f2, note = repair(f)
            if note.detail.get("block"):
                in_findings.append(dl.finding("MAP-DATA-002", "BLOCK", (f"source {i + 1}: " if len(frames) > 1 else "") + note.msg, note.detail))
            notes.append(note)
            repaired.append(f2)
        frames = repaired
    for i, f in enumerate(frames):
        in_findings += [dict(x, message=(f"source {i + 1}: " if len(frames) > 1 else "") + x["message"]) for x in dl.lint_data(f, assumed=False) if x["id"] != "MAP-DATA-001" or x["severity"] != "INFO"]
    gdf = merge(frames, notes) if len(frames) > 1 else frames[0]
    if gdf.crs is None and t_srs:
        raise ConvertError("the data has no CRS, so it cannot be reprojected. Say which CRS it uses with --crs-assume")
    if rename_map:
        gdf = rename(gdf, rename_map, notes)
        if select:
            select = [rename_map.get(c, c) for c in select]
    ops_report: list = []
    if ops or clip_layer:
        import geo_ops
        ops = list(ops or [])
        layers = {}
        if clip_layer:
            cp, _, cl = str(clip_layer).partition(":")
            rc = geo_io.read(cp, layer=cl or None, crs_assume=crs_assume)
            notes += [geo_io.Note(n.id, n.sev, "clip layer: " + n.msg, n.detail) for n in rc.notes]
            layers["clip"] = rc.gdf
            if not any(o.get("layer") == "clip" for o in ops if isinstance(o, dict)):
                ops.insert(0, {"op": "clip", "layer": "clip"})
        import ops_lint
        bad = ops_lint.lint_ops_spec(ops)
        if bad:
            raise ConvertError("; ".join(b["message"] for b in bad))
        ctx = geo_ops.Ctx(layers=layers, layer_id=Path(sources[0]).stem)
        fixed = geo_ops.fixed_ids(ops)
        if fixed:
            in_findings = [x for x in in_findings if x["id"] not in fixed]      # the op's own finding replaces the input finding it fixes
        try:
            gdf, op_notes, op_findings = geo_ops.apply_ops(gdf, ops, ctx)
        except geo_ops.OpsError as e:
            raise ConvertError(str(e)) from None
        notes += [geo_io.Note("OPS", "INFO", n) for n in op_notes]
        in_findings += op_findings
        ops_report = [dict(pv, equivalent=geo_ops.equivalent(pv["op"], pv.get("params", {}), pv)) for pv in ctx.provenance]
    if dissolve_by:
        gdf = dissolve(gdf, rename_map.get(dissolve_by, dissolve_by) if rename_map else dissolve_by, agg or {}, notes)
    if do_clip:
        gdf = clip(gdf, bbox, notes)
    datum = None
    if t_srs:
        if gdf.crs is not None and not gdf.crs.equals(t_srs):
            gdf, datum = ce.reproject(gdf, t_srs)
            notes.append(geo_io.Note("IO", "INFO", f"reprojected to {gdf.crs.to_string()}"))
            if datum:
                msg = f"datum shift {datum['from_datum']} to {datum['to_datum']} via {datum['operation']}"
                if datum.get("accuracy_m"):
                    msg += f" (accuracy about {datum['accuracy_m']} m)"
                if datum.get("grids_missing"):
                    msg += "; the best grid-based transform is not installed here, so the result can be off by more than the stated accuracy"
                notes.append(geo_io.Note("MAP-CRS-004", "WARN" if datum.get("grids_missing") else "INFO", msg, datum))
    if simplify_tol:
        gdf = simplify(gdf, simplify_tol, notes)
    if len(gdf) == 0:
        raise ConvertError("nothing is left to write after the filters; check --where and --bbox")

    written, results, checks, tile_commands = [], [], [], []
    groups = [(None, gdf)]
    split_values = None
    if split_by:
        if split_by not in gdf.columns:
            raise ConvertError(f"split field '{split_by}' is not in the data")
        split_values = list(gdf[split_by].dropna().unique())
        if gdf[split_by].isna().any():
            split_values.append(None)
        if len(split_values) > SPLIT_MAX:
            raise ConvertError(f"'{split_by}' has {len(split_values)} distinct values; the limit is {SPLIT_MAX}. Choose a coarser field")
        groups = [(v, gdf[gdf[split_by].isna()] if v is None else gdf[gdf[split_by] == v]) for v in split_values]

    layered = fmt == "gpkg" and split_by
    for v, part in groups:
        if split_by and not layered:
            target = outp.with_name(f"{outp.stem}_{_safe_name(v)}{outp.suffix}")
        else:
            target = outp
        lyr = out_layer or (_safe_name(v) if layered else None)
        if tiled:                       # a tile set cannot be read back as data: its own read-back (MAP-OUT-009) stands for the round trip
            wr = geo_io.write(part, target, fmt, layer=lyr, overwrite=True, tiles={"minzoom": minzoom if minzoom is not None else 0,
                                                                                   "maxzoom": maxzoom if maxzoom is not None else "auto"})
            written.append(target)
            notes += wr.notes
            checks += wr.checks
            tile_commands.append(wr.command)
            results.append({"path": str(target), "features": wr.features, "crs": wr.crs, "field_map": {}})
            continue
        if style is not None:
            import kml_style as ks
            import style_dict
            try:
                wr = ks.write(part, target, style, layer=lyr or target.stem, tokens=style_dict.brand_tokens(), legend=kml_legend)
            except ks.KmlStyleError as e:
                raise ConvertError(str(e)) from None
            styled = next((n.detail["style"] for n in wr.notes if n.id == "MAP-OUT-010" and isinstance(n.detail, dict)), None)
            back_style = ks.read_styles(target)
            checks += ks.compare(styled, back_style.get(next(iter(back_style), None)) or {}, target.name) if styled else []
        else:
            wr = geo_io.write(part, target, fmt, layer=lyr, overwrite=True, append=bool(layered and written))
        written.append(target)
        notes += wr.notes
        back = geo_io.read(target, layer=lyr if fmt in ("gpkg", "gpx") and lyr else None)
        # compare against what was handed to the writer, after the format's own CRS rule
        checks += dl.round_trip(part, back.gdf, fmt=fmt, field_map=wr.field_map, crs_forced_wgs84=geo_io.FORMATS[fmt].wgs84_only)
        if fmt == "geojson":
            checks += dl.check_geojson_rfc7946(target)
        if fmt in ("kml", "kmz"):
            checks += dl.check_kml(target)
        results.append({"path": str(target), "features": wr.features, "crs": wr.crs, "field_map": wr.field_map})
    written = list(dict.fromkeys(written))
    findings = sorted(in_findings + checks, key=lambda x: (dl.SEV_ORDER[x["severity"]], x["id"]))
    blocked = any(x["severity"] == "BLOCK" for x in findings)
    removed = []
    if blocked and strict:
        for t in written:
            for suffix in ("", ".prj", ".cpg", ".shx", ".dbf"):
                q = t if suffix == "" else t.with_suffix(suffix)
                if q.exists():
                    q.unlink()
                    removed.append(str(q))
    eq = equivalents({"where": where, "select": select_in_source, "bbox": list(bbox) if bbox else None, "simplify": simplify_tol, "t_srs": t_srs,
                      "crs_assume": crs_assume, "dissolve": dissolve_by, "agg": agg, "rename": rename_map, "clip": do_clip,
                      "split_by": split_by, "out_layer": out_layer}, [str(s) for s in sources], str(out), fmt, split_values)
    if ops_report:
        eq["caveats"].append("the geoprocessing steps (--repair, --buffer, --clip-layer, --op ...) have no ogr2ogr form; see `ops` for their provenance and the GeoPandas or PostGIS equivalent")
    if tile_commands:
        eq["tiles"] = tile_commands
        eq["caveats"].append("`tiles` holds the exact ogr2ogr command for the tile set (zooms and per-layer settings), run on the converted layers in Web Mercator")
    if style is not None:
        eq["caveats"].append("ogr2ogr writes KML without these class styles, icons or balloons; the styled file comes from this tool")
    rep = {"outputs": [] if (blocked and strict) else results, "format": fmt, "features": len(gdf), "findings": findings,
           "notes": [n.as_dict() for n in notes], "ogr2ogr": eq, "blocked": blocked, "removed": removed}
    if ops_report:
        rep["ops"] = ops_report
    if engines:
        rep["engine"] = engines
    return rep


def ops_from_args(a) -> list[dict]:
    """The geo_ops list the geoprocessing flags ask for, in the documented order; --op JSON entries last."""
    ops: list[dict] = []                     # --repair is the convert() step do_repair (the same geo_ops repair, run first with its own note)
    if a.antimeridian_split:
        ops.append({"op": "antimeridian_split"})
    if a.explode:
        ops.append({"op": "explode"})
    if a.dedupe:
        ops.append({"op": "dedupe", "geometry": True})
    if a.buffer:
        ops.append({"op": "buffer", "distance": a.buffer, "units": a.buffer_units})
    if a.area:
        ops.append({"op": "area", "field": a.area, "units": a.area_units})
    if a.length:
        ops.append({"op": "length", "field": a.length, "units": a.length_units})
    for text in a.op or []:
        try:
            o = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConvertError(f"--op needs JSON like '{{\"op\": \"centroid\"}}': {e}") from None
        if not isinstance(o, dict) or not o.get("op"):
            raise ConvertError("--op JSON must be an object with an `op` name")
        ops.append(o)
    return ops


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=None)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--format")
    ap.add_argument("--layer")
    ap.add_argument("--out-layer")
    ap.add_argument("--crs-assume")
    ap.add_argument("--t-srs")
    ap.add_argument("--bbox", nargs=4, type=float)
    ap.add_argument("--clip", action="store_true")
    ap.add_argument("--where")
    ap.add_argument("--select")
    ap.add_argument("--rename")
    ap.add_argument("--dissolve")
    ap.add_argument("--agg")
    ap.add_argument("--simplify", type=float)
    ap.add_argument("--split-by")
    ap.add_argument("--lat-col")
    ap.add_argument("--lon-col")
    ap.add_argument("--wkt-col")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-strict", action="store_true")
    ap.add_argument("--clip-layer")
    ap.add_argument("--antimeridian-split", action="store_true")
    ap.add_argument("--explode", action="store_true")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--buffer")
    ap.add_argument("--buffer-units", default="m")
    ap.add_argument("--area")
    ap.add_argument("--area-units", default="km2")
    ap.add_argument("--length")
    ap.add_argument("--length-units", default="km")
    ap.add_argument("--op", action="append")
    ap.add_argument("--minzoom", type=int)
    ap.add_argument("--maxzoom")
    ap.add_argument("--kml-style")
    ap.add_argument("--kml-legend", action="store_true")
    ap.add_argument("--engine", choices=["auto", "pandas", "duckdb"], help="what runs --where on a CSV or GeoParquet source (default auto: DuckDB on large tables when installed)")
    a = ap.parse_args(argv)
    if a.maxzoom is not None and a.maxzoom != "auto":
        try:
            a.maxzoom = int(a.maxzoom)
        except ValueError:
            print("geo-convert: --maxzoom takes a whole number or auto", file=sys.stderr)
            return gc.EXIT_USAGE
    try:
        ops = ops_from_args(a)
        rep = convert(a.sources, a.out, fmt=a.format, layer=a.layer, out_layer=a.out_layer, crs_assume=a.crs_assume, t_srs=a.t_srs,
                      bbox=a.bbox, do_clip=a.clip, where=a.where, select=a.select.split(",") if a.select else None,
                      rename_map=parse_pairs(a.rename), dissolve_by=a.dissolve, agg=parse_pairs(a.agg), simplify_tol=a.simplify,
                      split_by=a.split_by, overwrite=a.overwrite, strict=not a.no_strict, lat_col=a.lat_col, lon_col=a.lon_col, wkt_col=a.wkt_col,
                      do_repair=a.repair, ops=ops or None, clip_layer=a.clip_layer, minzoom=a.minzoom, maxzoom=a.maxzoom, kml_style=a.kml_style,
                      kml_legend=a.kml_legend, engine=a.engine)
    except geo_io.AmbiguousColumns as e:
        print(json.dumps({"error": str(e), "candidates": e.candidates}, indent=2))
        return gc.EXIT_USAGE
    except geo_io.GeoIOError as e:
        print(f"geo-convert: {e}", file=sys.stderr)
        return gc.EXIT_RUNTIME
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_LINT if rep["blocked"] and not a.no_strict else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
