#!/usr/bin/env python3
"""atlas: one map per value of a field, from one MapSpec, with the same classes in every map and one combined PDF (spec 7.2 map-atlas-run, MAP-CART-014).

    atlas.py plan  MAP.yaml --layer ID --field COL [--mode filter|focus] [--values a,b,c] [--max-maps 60]
    atlas.py build MAP.yaml --layer ID --field COL --out DIR [--formats pdf,png,svg] [--no-shared-classes] [--no-index]
    atlas.py plan|build MAP.yaml --layer ID --by time [--field DATECOL] [--unit auto|year|quarter|month|day] [--cumulative] [--animate gif,mp4]

by time (M15): one map per period of the layer's `time_field` (or --field), in time order, every page on the whole data's frame with the period as
the large frame label under the title; `--cumulative` draws everything up to the period with the period's own features larger, in the next palette
colour, and the features still planned as small grey dots (`planned: false` leaves those out). `--animate gif,mp4` adds the animation stage.

The iterated layer is the one named by `layer`; `field` is a column of that layer (a boundary's STUSPS, a site list's vendor). One map is made per distinct value, or per value in `values`.

  mode filter  the layer shows only the features whose field equals the value (one vendor's sites, one state's counties); the extent follows the data
  mode focus   the layer keeps every feature and the extent is fitted to the value's features, so the others stay as context (one market among all markets)

Shared classes (default on): a numeric classified layer takes its class edges from ALL the data, not from each map's subset, so a color means the same thing on every page and the legend
is identical (MAP-CART-014). Turn it off with `shared_classes: false` only when each map is meant to stand alone; the index page then says the maps are not comparable.

Findings
  MAP-ATL-001 BLOCK  the layer or the field is missing, or the field has no values
  MAP-ATL-002 BLOCK  more maps than max_maps (a typo guard: name `values` or raise `max_maps`)
  MAP-ATL-003 WARN   values left out (listed in `values:` but absent from the data)
  MAP-ATL-004 INFO   a tile basemap is not used in an atlas; the bundled basemap is drawn
  MAP-ATL-005 BLOCK  one map cannot be built (the value is named; fix it, or leave the value out with values=)
  MAP-ATL-006 INFO   shared classes were not applied (no numeric classified layer, or one class holds a single value)
  MAP-ATL-007 BLOCK  the combined PDF does not have the pages it should
  MAP-ATL-008 BLOCK  by time: the time field cannot be read as dates, years, months, quarters or named periods (WARN when only some values fail,
                     INFO for features with no value: those are on no page)
  MAP-ATL-009 INFO   by time: periods between the first and the last hold nothing and get no page; or a cumulative atlas leaves the layer's classes out
  MAP-CART-014 BLOCK maps of one atlas have different class edges or colors although shared classes were asked for
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402
import text_lint  # noqa: E402

DEFAULT_MAX = 60
HARD_MAX = 300
ROWS_PER_PAGE = 36


class AtlasError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-ATL-001"):
        super().__init__(message)
        self.fid = fid


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def slug(v) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(v).strip().lower()).strip("-")
    return s[:40] or "map"


def norm_options(opt: dict) -> dict:
    o = dict(opt or {})
    vals = o.get("values")
    if isinstance(vals, str):
        vals = [v.strip() for v in vals.split(",") if v.strip()]
    mode = str(o.get("mode") or "filter")
    if mode not in ("filter", "focus"):
        raise AtlasError("mode is filter or focus")
    try:
        mx = int(o.get("max_maps") or DEFAULT_MAX)
    except (TypeError, ValueError):
        raise AtlasError("max_maps is a whole number")
    by = str(o.get("by") or "field")
    if by not in ("field", "time"):
        raise AtlasError("by is field (one map per value) or time (one map per period of the layer's time_field)")
    cum = _flag(o.get("cumulative"), False)
    planned = _flag(o.get("planned"), cum)
    return {"layer": o.get("layer"), "field": o.get("field"), "values": vals, "mode": mode, "max_maps": min(mx, HARD_MAX),
            "shared_classes": o.get("shared_classes") is not False, "index_page": o.get("index_page") is not False,
            "title": o.get("title") or ("{title}" if by == "time" else "{title}, {value}"),
            "by": by, "cumulative": cum, "unit": str(o.get("unit") or "auto"), "planned": planned, "labels": dict(o.get("labels") or {}),
            "tiles": _flag(o.get("tiles"), False),             # 1.12.0: keep the MapSpec's tile basemap, prepared per page extent
            "animate": _list(o.get("animate")), "animate_sizes": _list(o.get("animate_sizes")) or ["square", "slide"],
            "hold": float(o.get("hold") or 1.0), "last_hold": float(o.get("last_hold") or 3.0),
            "contact_sheet": _flag(o.get("contact_sheet"), False)}


def _flag(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).lower() in ("true", "1", "yes", "on") if isinstance(v, str) else bool(v)


def _list(v) -> list[str]:
    if v in (None, False, "", "none"):
        return []
    if v is True:
        return ["gif", "mp4"]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


# ---------------------------------------------------------------------------------------------------------------- discovery
def discover(spec_path, opt: dict) -> dict:
    """Read the data the plan would draw and decide the values and, for shared classes, the edges. Changes nothing."""
    import map_plan as mp
    o = norm_options(opt)
    try:
        plan = mp.prepare(spec_path)
    except mp.PlanError as ex:
        raise AtlasError(str(ex).splitlines()[0], "MAP-ATL-001")
    layers = {l.id: l for l in plan.layers}
    if not o["layer"]:
        cand = [l.id for l in plan.layers if l.role in ("choropleth", "points", "polygons", "proportional", "lines")]
        if len(cand) == 1:
            o["layer"] = cand[0]
        else:
            raise AtlasError(f"say which layer to iterate with layer= (layers: {', '.join(layers)})")
    if o["layer"] not in layers:
        raise AtlasError(f"the MapSpec has no layer '{o['layer']}' (layers: {', '.join(layers)})")
    lay = layers[o["layer"]]
    g = lay.gdf
    if o["by"] == "time":
        return _discover_time(plan, lay, o)
    if not o["field"] or o["field"] not in g.columns:
        cols = ", ".join(c for c in g.columns if c != g.geometry.name)
        raise AtlasError(f"say which column to iterate with field= (columns of '{lay.id}': {cols})")
    series = g[o["field"]].dropna().astype(str).str.strip()
    series = series[series != ""]
    if series.empty:
        raise AtlasError(f"'{o['field']}' has no values to iterate")
    present = sorted(series.unique(), key=lambda s: (s.lower(), s))
    fs = []
    if o["values"]:
        want = [str(v) for v in o["values"]]
        missing = [v for v in want if v not in present]
        if missing:
            fs.append(finding("MAP-ATL-003", "WARN", f"{len(missing)} value(s) are not in '{o['field']}' and were left out: {', '.join(missing[:10])}", missing))
        values = [v for v in want if v in present]
        if not values:
            raise AtlasError(f"none of the values are in '{o['field']}' (it holds, for example: {', '.join(present[:6])})")
    else:
        values = present
    if len(values) > o["max_maps"]:
        raise AtlasError(f"{len(values)} values would make {len(values)} maps, more than max_maps={o['max_maps']}; name the values you want with values= or raise max_maps", "MAP-ATL-002")
    counts = {v: int((g[o["field"]].astype(str).str.strip() == v).sum()) for v in values}
    shared = _shared(o, lay, fs)
    tiles = (plan.spec.get("basemap") or {}).get("provider") not in (None, "none", "bundled")
    if tiles and not o.get("tiles"):
        fs.append(finding("MAP-ATL-004", "INFO", "a tile basemap is prepared for one extent, so an atlas draws the bundled basemap instead; "
                          "tiles=true keeps it (one composite per page extent, inside the provider's tile caps)"))
    return {"options": o, "layer": lay.id, "role": lay.role, "field": o["field"], "values": values, "counts": counts, "shared": shared, "findings": fs, "tiles": tiles,
            "base_title": text_lint.title_text(plan.spec), "base_id": str(plan.spec["id"]), "gdf": g}


def _shared(o: dict, lay, fs: list):
    """The class edges every map takes (MAP-CART-014), or None with a finding saying why not. A bivariate layer shares both axes' edges (M15)."""
    cls = lay.cls
    if o["shared_classes"] and cls is not None and getattr(cls, "kind", "") == "numeric" and cls.edges and any(b <= a for a, b in zip(cls.edges, cls.edges[1:])):
        fs.append(finding("MAP-ATL-006", "WARN", "the classes include a class of one value, which cannot be reused on a subset; choose another classification method to share classes"))
    elif o["shared_classes"] and cls is not None and getattr(cls, "kind", "") == "numeric" and cls.edges:
        return {"layer": lay.id, "field": lay.value_field, "edges": [float(e) for e in cls.edges], "decimals": int(cls.decimals), "integer_data": bool(cls.integer_data),
                "method": cls.method}
    elif o["shared_classes"] and cls is not None and getattr(cls, "kind", "") == "bivariate":
        b = (lay.symbol or {}).get("_bivariate") or {}
        return {"layer": lay.id, "bivariate": {ax: {"edges": b[ax]["edges"], "decimals": b[ax]["decimals"], "integer_data": b[ax]["integer_data"]} for ax in ("x", "y")},
                "edges": b["x"]["edges"] + b["y"]["edges"], "decimals": max(b["x"]["decimals"], b["y"]["decimals"])}
    elif o["shared_classes"] and cls is not None and getattr(cls, "kind", "") == "categorical" and cls.categories:
        # 0.24.0 (TODO M7): a categorical layer shares its categories: one order and one colour each on every page, absent ones included
        cats = [c for c in cls.categories if c != "Other"] if "Other" in cls.categories and len(cls.categories) > 1 else list(cls.categories)
        return {"layer": lay.id, "categorical": {"order": cats}}
    elif o["shared_classes"]:
        fs.append(finding("MAP-ATL-006", "INFO", "no numeric classified layer, so there are no class edges to share; each map keeps the colors and legend of the MapSpec"))
    return None


def _discover_time(plan, lay, o: dict) -> dict:
    """by: time (M15). The pages are the periods of the layer's `time_field` (or `field=`), first to last; the classes come from the whole range
    (every period at once), so a colour means the same in every month; the frame is the whole data's, so the maps do not move between pages."""
    import numpy as np
    import timekeys as tk
    g = lay.gdf
    fld = o["field"] or lay.spec.get("time_field")
    if not fld:
        raise AtlasError("by: time needs the layer's time_field (layers[].time_field: on_air) or field= naming the column of dates, years or periods")
    if fld not in g.columns:
        raise AtlasError(f"'{lay.id}' has no column '{fld}' for time (columns: {', '.join(c for c in g.columns if c != g.geometry.name)})")
    try:
        per = tk.periods(g[fld].tolist(), o["unit"], order=lay.spec.get("time_order"))
    except tk.TimeError as ex:
        raise AtlasError(f"time_field '{fld}': {ex}", "MAP-ATL-008")
    fs = []
    if per.unreadable:
        fs.append(finding("MAP-ATL-008", "WARN", f"{per.unreadable} value(s) of '{fld}' could not be read as a date, year or period; those features are on no page"))
    nulls = int(sum(1 for k in per.keys if k is None)) - per.unreadable
    if nulls:
        fs.append(finding("MAP-ATL-008", "INFO", f"{nulls} feature(s) have no '{fld}' and are on no page"))
    values = list(per.order)
    if o["values"]:
        want = [str(v) for v in o["values"]]
        by_label = {per.label(k): k for k in per.order}
        values = [k for k in per.order if k in want or per.label(k) in want]
        missing = [v for v in want if v not in per.order and v not in by_label]
        if missing:
            fs.append(finding("MAP-ATL-003", "WARN", f"{len(missing)} period(s) are not in '{fld}' and were left out: {', '.join(missing[:10])}", missing))
        if not values:
            raise AtlasError(f"none of the periods are in '{fld}' (it holds, for example: {', '.join(per.label(k) for k in per.order[:6])})")
    if len(values) > o["max_maps"]:
        raise AtlasError(f"{len(values)} periods would make {len(values)} maps, more than max_maps={o['max_maps']}; name the periods with values= or use a coarser unit= "
                         "(month, quarter, year)", "MAP-ATL-002")
    if per.unit in ("month", "quarter", "year") and len(values) > 1:
        gaps = _gaps(values, per.unit)
        if gaps:
            fs.append(finding("MAP-ATL-009", "INFO", f"{len(gaps)} {per.word}(s) between the first and the last have no features and get no page: {', '.join(gaps[:8])}", gaps))
    raw = g[fld].astype(str).tolist()
    at = {v: sorted({r for r, k in zip(raw, per.keys, strict=True) if k == v}) for v in values}
    upto = {v: sorted({r for r, k in zip(raw, per.keys, strict=True) if k is not None and k <= v}) for v in values}
    counts = {v: sum(1 for k in per.keys if k == v) for v in values}
    shared = _shared(o, lay, fs)
    if o["cumulative"] and lay.cls is not None:
        fs.append(finding("MAP-ATL-009", "INFO", f"cumulative pages draw '{lay.id}' as planned, earlier and this {per.word}; its classes are not drawn"))
        shared = None
    tiles = (plan.spec.get("basemap") or {}).get("provider") not in (None, "none", "bundled")
    if tiles and not o.get("tiles"):
        fs.append(finding("MAP-ATL-004", "INFO", "a tile basemap is prepared for one extent, so an atlas draws the bundled basemap instead; "
                          "tiles=true keeps it (one composite per page extent, inside the provider's tile caps)"))
    bb = np.array([l.gdf.to_crs("EPSG:4326").total_bounds for l in plan.layers
                   if l.role not in ("context", "labels", "hillshade", "contour") and l.kind != "raster" and len(l.gdf)])
    bbox = [float(bb[:, 0].min()), float(bb[:, 1].min()), float(bb[:, 2].max()), float(bb[:, 3].max())] if len(bb) else None
    return {"options": o, "layer": lay.id, "role": lay.role, "kind": lay.kind, "field": fld, "values": values, "counts": counts, "shared": shared, "findings": fs,
            "tiles": tiles, "base_title": text_lint.title_text(plan.spec), "base_id": str(plan.spec["id"]), "gdf": g,
            "time": {"unit": per.unit, "word": per.word, "labels": {v: per.label(v) for v in values}, "at": at, "upto": upto, "bbox": bbox,
                     "cumulative_counts": {v: sum(1 for k in per.keys if k is not None and k <= v) for v in values}}}


def _gaps(values: list[str], unit: str) -> list[str]:
    """The months, quarters or years between the first and the last period that hold nothing."""
    def idx(k):
        y = int(k[:4])
        return y * 12 + int(k[5:7]) - 1 if unit == "month" else (y * 4 + int(k[-1]) - 1 if unit == "quarter" else y)

    def key(i):
        return f"{i // 12:04d}-{i % 12 + 1:02d}" if unit == "month" else (f"{i // 4:04d}-Q{i % 4 + 1}" if unit == "quarter" else f"{i:04d}")
    have = {idx(v) for v in values}
    return [key(i) for i in range(min(have), max(have) + 1) if i not in have]


# ---------------------------------------------------------------------------------------------------------------- per-map specs
def _abs_paths(spec: dict, base: Path) -> None:
    """The per-map specs live in the run folder, so every relative path in the base spec becomes absolute."""
    for ly in spec.get("layers") or []:
        src = ly.get("source") or {}
        if src.get("path") and not Path(src["path"]).is_absolute():
            src["path"] = str((base / src["path"]).resolve())
        j = ly.get("join") or {}
        if j.get("table") and isinstance(j["table"], str) and not Path(j["table"]).is_absolute():
            j["table"] = str((base / j["table"]).resolve())
    for k in ("logo", "image"):
        el = (spec.get("elements") or {}).get(k)
        if isinstance(el, dict) and el.get("path") and not Path(el["path"]).is_absolute():
            el["path"] = str((base / el["path"]).resolve())


def make_specs(spec_path, disc: dict, out_dir: Path) -> list[dict]:
    """Write one MapSpec per value into out_dir; returns [{value, slug, id, spec, title, features}]."""
    import yaml
    spec_path = Path(spec_path)
    base_spec = gc.load_mapspec(spec_path)
    o = disc["options"]
    out_dir.mkdir(parents=True, exist_ok=True)
    if o.get("by") == "time":
        return _time_specs(base_spec, spec_path, disc, out_dir)
    seen: dict[str, int] = {}
    maps = []
    for v in disc["values"]:
        s = copy.deepcopy(base_spec)
        _abs_paths(s, spec_path.parent)
        sl = slug(v)
        seen[sl] = seen.get(sl, 0) + 1
        if seen[sl] > 1:
            sl = f"{sl}-{seen[sl]}"
        s["id"] = f"{base_spec['id']}-{sl}"
        ly = next(l for l in s["layers"] if l["id"] == disc["layer"])
        if o["mode"] == "filter":
            flt = ly.get("filter")
            flts = [flt] if isinstance(flt, dict) else list(flt or [])
            flts.append({"column": o["field"], "in": [v]})
            ly["filter"] = flts if len(flts) > 1 else flts[0]
        else:
            g = disc["gdf"]
            sel = g[g[o["field"]].astype(str).str.strip() == v]
            minx, miny, maxx, maxy = [float(x) for x in sel.to_crs("EPSG:4326").total_bounds]
            if maxx - minx < 1e-6:
                minx, maxx = minx - 0.01, maxx + 0.01
            if maxy - miny < 1e-6:
                miny, maxy = miny - 0.01, maxy + 0.01
            s["extent"] = {"mode": "bbox", "bbox": [minx, miny, maxx, maxy], "padding_pct": float((base_spec.get("extent") or {}).get("padding_pct") or 8)}
        _apply_shared(ly, disc["shared"])
        title = dict((s.get("elements") or {}).get("title") or {})
        title["on"] = True
        title["text"] = o["title"].format(title=disc["base_title"], value=v, field=o["field"])
        s.setdefault("elements", {})["title"] = title
        if disc["tiles"] and not o.get("tiles"):
            s["basemap"] = {"provider": "bundled"}
        elif disc["tiles"]:
            # 1.12.0: the page keeps the tile basemap. Pages that show the same extent (a time atlas, a filter atlas) share one composite;
            # a focus atlas zooms each page, so each has its own
            own_extent = o.get("by") != "time" and o.get("mode") == "focus"
            s["basemap"] = {**(s.get("basemap") or {}), "prepared_as": s["id"] if own_extent else f"{disc['base_id']}-atlas"}
        p = out_dir / f"{sl}.yaml"
        p.write_text(yaml.safe_dump(s, sort_keys=False, allow_unicode=True))
        maps.append({"value": v, "slug": sl, "id": s["id"], "spec": str(p), "title": title["text"], "features": disc["counts"][v]})
    return maps


def _apply_shared(ly: dict, sh: dict | None) -> None:
    """Hand the atlas's class edges to one map's copy of the iterated layer (a bivariate layer's edges go on both axes)."""
    if not sh or sh["layer"] != ly["id"]:
        return
    if sh.get("categorical"):
        c = dict(ly.get("classification") or {"method": "categorical"})
        c.update({"order": list(sh["categorical"]["order"]), "keep_absent": True})
        ly["classification"] = c
        return
    if sh.get("bivariate"):
        bv = dict(ly.get("bivariate") or {})
        for ax, e in sh["bivariate"].items():
            a = bv.get(ax)
            a = {"field": a} if isinstance(a, str) else dict(a or {})
            a.update(e)
            a.pop("breaks", None)
            bv[ax] = a
        ly["bivariate"] = bv
        return
    c = dict(ly.get("classification") or {})
    c.update({"edges": sh["edges"], "decimals": sh["decimals"], "integer_data": sh["integer_data"]})
    c.pop("breaks", None)
    ly["classification"] = c


TIME_LABELS = {"planned": "Planned", "earlier": "Earlier", "period": "New this {word}"}
TIME_SIZE = {"planned": 0.7, "earlier": 1.0, "period": 1.5}
PLANNED_MIN_SIZE = 2.4          # 0.23.0 (TODO M16): planned sites stay visible on a 1080 px frame, and so does their key in the legend


def _time_specs(base_spec: dict, spec_path: Path, disc: dict, out_dir: Path) -> list[dict]:
    """One MapSpec per period (by: time, M15). Every page keeps the whole data's frame (an extent bbox) so nothing moves between pages, and the
    title block carries the period as its frame label, measured for the longest one so the title box keeps its size. Per period: the layer
    filtered to that period; or, cumulative, three layers in its place: every feature as planned (small, in a brand neutral; `planned: false`
    leaves it out, and the pages may then reflow as features arrive), every feature up to and including the period (the layer's colour) and
    the period's own features on top (larger, the next palette colour). Legend labels say 'this month' rather than naming it, so the legend
    keeps its size too."""
    import yaml
    o, tm_ = disc["options"], disc["time"]
    labels = {**TIME_LABELS, **{k: str(v) for k, v in (o.get("labels") or {}).items()}}
    longest = max(tm_["labels"].values(), key=len)
    maps = []
    for n, v in enumerate(disc["values"]):
        s = copy.deepcopy(base_spec)
        _abs_paths(s, spec_path.parent)
        lab = tm_["labels"][v]
        sl = f"{n + 1:02d}-{slug(lab)}"
        s["id"] = f"{base_spec['id']}-{slug(lab)}"
        idx = next(i for i, l in enumerate(s["layers"]) if l["id"] == disc["layer"])
        ly = s["layers"][idx]
        prior = ly.get("filter")
        prior = [prior] if isinstance(prior, dict) else list(prior or [])

        def with_filter(layer: dict, vals: list[str]) -> dict:
            fl = prior + [{"column": disc["field"], "in": list(vals)}]
            layer["filter"] = fl if len(fl) > 1 else fl[0]
            return layer
        if o["cumulative"]:
            key = "fill" if ly.get("role") == "polygons" else "color"
            sym0 = dict(ly.get("symbol") or {})
            own = sym0.get(key) or sym0.get("color") or "@brand.categorical.0"
            nxt = "@brand.categorical.2" if own == "@brand.categorical.1" else "@brand.categorical.1"
            base = {k: v_ for k, v_ in ly.items() if k not in ("classification", "value_field", "palette", "legend")}
            size = float(sym0.get("size", 3))
            pts = ly.get("role") == "points"
            parts = []
            if o.get("planned", True):
                pl = copy.deepcopy(base)
                pl["id"] = f"{ly['id']}-planned"
                pl["symbol"] = {**sym0, key: "@brand.neutral.4", **({"size": round(max(size * TIME_SIZE["planned"], PLANNED_MIN_SIZE), 2)} if pts else {})}
                pl["legend"] = {"label": labels["planned"].format(word=tm_["word"], period=lab)}
                if prior:
                    pl["filter"] = prior if len(prior) > 1 else prior[0]
                parts.append(pl)
            ea = with_filter(copy.deepcopy(base), tm_["upto"][v])
            ea["symbol"] = {**sym0, key: own}
            ea["legend"] = {"label": labels["earlier"].format(word=tm_["word"], period=lab)}
            parts.append(ea)
            nw = with_filter(copy.deepcopy(base), tm_["at"][v])
            nw["id"] = f"{ly['id']}-period"
            nw["symbol"] = {**sym0, key: nxt, **({"size": round(size * TIME_SIZE["period"], 2)} if pts else {})}
            nw["legend"] = {"label": labels["period"].format(word=tm_["word"], period=lab)}
            parts.append(nw)
            s["layers"][idx:idx + 1] = parts
        else:
            with_filter(ly, tm_["at"][v])
            _apply_shared(ly, disc["shared"])
        if tm_["bbox"] and (base_spec.get("extent") or {}).get("mode", "fit_layers") == "fit_layers":
            s["extent"] = {"mode": "bbox", "bbox": tm_["bbox"], "padding_pct": float((base_spec.get("extent") or {}).get("padding_pct") or 5)}
        title = dict((s.get("elements") or {}).get("title") or {})
        title["on"] = True
        title["text"] = o["title"].format(title=disc["base_title"], value=lab, field=disc["field"])
        title["frame_label"] = lab
        title["frame_label_reserve"] = longest
        s.setdefault("elements", {})["title"] = title
        if disc["tiles"] and not o.get("tiles"):
            s["basemap"] = {"provider": "bundled"}
        elif disc["tiles"]:
            # 1.12.0: the page keeps the tile basemap. Pages that show the same extent (a time atlas, a filter atlas) share one composite;
            # a focus atlas zooms each page, so each has its own
            own_extent = o.get("by") != "time" and o.get("mode") == "focus"
            s["basemap"] = {**(s.get("basemap") or {}), "prepared_as": s["id"] if own_extent else f"{disc['base_id']}-atlas"}
        p = out_dir / f"{sl}.yaml"
        p.write_text(yaml.safe_dump(s, sort_keys=False, allow_unicode=True))
        feats = tm_["cumulative_counts"][v] if o["cumulative"] else disc["counts"][v]
        maps.append({"value": lab, "period": v, "slug": sl, "id": s["id"], "spec": str(p), "title": f"{title['text']}, {lab}", "features": feats,
                     "new": disc["counts"][v]})
    return maps


# ---------------------------------------------------------------------------------------------------------------- the shared-classes check
def compare_classes(results: list[dict]) -> list[dict]:
    """MAP-CART-014: every map built for an atlas that asked for shared classes must show the same class edges and the same colors for the iterated layer."""
    sigs = {}
    for r in results:
        obj = r.get("objects") or {}
        meth = obj.get("methods") or {}
        plan = obj.get("plan")
        if not plan:
            continue
        sig = []
        for l, ml in zip(plan.layers, meth.get("layers") or [], strict=True):
            c = ml.get("classification")
            if c is not None:
                sig.append((l.id, tuple(c.get("breaks") or ()), c.get("classes"), tuple(l.colors or ())))
        sigs[r["value"]] = sig
    if len(sigs) < 2:
        return []
    ref_v = next(iter(sigs))
    bad = [v for v, s in sigs.items() if s != sigs[ref_v]]
    if not bad:
        return []
    return [finding("MAP-CART-014", "BLOCK",
                    f"shared classes were asked for, but {len(bad)} map(s) have different class edges or colors from '{ref_v}' (for example {', '.join(bad[:5])}); a color would mean different things on different pages", bad)]


# ---------------------------------------------------------------------------------------------------------------- the index page and the combined PDF
def index_pages(plan, rows: list[dict], disc: dict, first_page: int, shared_note: str, title: str) -> list[str]:
    """Letter-portrait SVG pages listing every map with its page number. `plan` is the first map's plan (brand, tokens, fonts, hidden metadata)."""
    import map_render as mr
    import provenance as pv
    import svg_draw as sd
    import textmeasure as tm
    W, H = pv.PAGE_MM
    t = plan.tokens
    mx_, my_ = 18.0, 18.0
    cw = W - 2 * mx_
    pages = []
    n_pages = max(1, -(-len(rows) // ROWS_PER_PAGE))
    for pi in range(n_pages):
        chunk = rows[pi * ROWS_PER_PAGE:(pi + 1) * ROWS_PER_PAGE]
        out = [sd.rect(0, 0, W, H, t["background"])]
        y = my_
        y = pv._para(out, mx_, y, title, cw, 20, t["label"], 700, 1.0)
        if pi == 0:
            y = pv._para(out, mx_, y, disc["base_title"], cw, 11, t["label"], 600, 2.0)
            y = pv._para(out, mx_, y, shared_note, cw, 9, t["label"], 400, 2.5)
        out.append(f'<line x1="{sd.num(mx_)}" y1="{sd.num(y)}" x2="{sd.num(mx_ + cw)}" y2="{sd.num(y)}" stroke="{t["frame"]}" stroke-width="0.25"/>')
        y += 3.0
        out.append(sd.text(mx_, y + 9 * pv.PT * 0.85, "Map", 9, t["label"], weight=700))
        out.append(sd.text(mx_ + cw - 22, y + 9 * pv.PT * 0.85, "Features", 9, t["label"], weight=700, anchor="end"))
        out.append(sd.text(mx_ + cw, y + 9 * pv.PT * 0.85, "Page", 9, t["label"], weight=700, anchor="end"))
        y += tm.line_height_mm(9) + 1.5
        for r in chunk:
            label = r["title"]
            while tm.width_mm(label, 9, 400) > cw - 40 and len(label) > 8:
                label = label[:-2]
            if label != r["title"]:
                label = label.rstrip() + "…"
            out.append(sd.text(mx_, y + 9 * pv.PT * 0.85, label, 9, t["label"], weight=400))
            out.append(sd.text(mx_ + cw - 22, y + 9 * pv.PT * 0.85, f"{r['features']:,}", 9, t["label"], weight=400, anchor="end", num_cls=True))
            out.append(sd.text(mx_ + cw, y + 9 * pv.PT * 0.85, str(r["page"]), 9, t["label"], weight=400, anchor="end", num_cls=True))
            y += tm.line_height_mm(9) + 0.6
        if n_pages > 1:
            out.append(sd.text(mx_, H - 12, f"Index page {pi + 1} of {n_pages}", 8, t["label"], weight=400))
        body = "".join(out)
        pages.append(mr.assemble_page(plan, W, H, body, f"{title} (index)", f"Index of {len(rows)} maps: {disc['base_title']}"))
    return pages


def shared_note(disc: dict) -> str:
    o = disc["options"]
    how = "Each map shows the features where " + f"{o['field']} equals the value." if o["mode"] == "filter" else f"Each map is framed on the features where {o['field']} equals the value; the others stay as context."
    if o.get("by") == "time":
        w = disc["time"]["word"]
        how = (f"One page per {w} of {disc['field']}, in time order, all on the same frame. "
               + (f"Each page shows every feature up to its {w}; that {w}'s own features are larger and in the second colour"
                  + (", and the features still planned are small grey dots." if o.get("planned") else ".")
                  if o["cumulative"] else f"Each page shows the features of its {w} only."))
    sh = disc["shared"]
    if sh and sh.get("bivariate"):
        b = sh["bivariate"]
        ex = ", ".join(f"{e:,.{b['x']['decimals']}f}" for e in b["x"]["edges"])
        ey = ", ".join(f"{e:,.{b['y']['decimals']}f}" for e in b["y"]["edges"])
        return f"{how} All maps use the same bivariate classes (x edges {ex}; y edges {ey}), so a colour means the same thing on every page."
    if sh and sh.get("categorical"):
        return f"{how} All maps use the same categories in the same colours, in one order, so a category missing from a page keeps its colour on the others."
    if sh:
        edges = ", ".join(f"{e:,.{sh['decimals']}f}" for e in sh["edges"])
        return f"{how} All maps use the same classes, with edges at {edges}, so a color means the same thing on every page and the pages can be compared."
    if o["shared_classes"]:
        return f"{how} The maps share their colors and legend."
    return f"{how} Classes were set for each map on its own, so colors are not comparable between pages."


# ---------------------------------------------------------------------------------------------------------------- contact sheet (0.27/0.28)
SHEET_DROP = ("gm-title", "gm-legend", "gm-inset", "gm-logo", "gm-north", "gm-footer", "gm-page", "gm-scalebar")   # each tile is the bare map
SHEET_PAD = 0.06                  # a tile shows its data's box with this much room round it (the page's frame also holds room for its elements)
SHEET_MAX = 24
SHEET_GAP_MM = 4.0
SHEET_CAPTION_MM = 5.0


def _tile_svg(svg: str, rect: list, prefix: str, keep_only: str | None = None) -> str:
    """The inside of one page's SVG for a nested <svg>: the element groups in SHEET_DROP removed (or, with `keep_only`, everything but that
    group and the defs and styles), every id prefixed so the tiles' clips and patterns do not collide. -> the children as text."""
    import re as _re
    from lxml import etree as ET
    root = ET.fromstring(svg.encode("utf-8"), ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True))

    def cls(el) -> set:
        return set(str(el.get("class") or "").split())
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            continue
        c = cls(el)
        tag = ET.QName(el).localname
        if keep_only:
            if el.getparent() is root and tag not in ("defs", "style") and keep_only not in c:
                root.remove(el)
        elif c & set(SHEET_DROP) or tag in ("title", "desc"):
            el.getparent().remove(el)
    inner = "".join(ET.tostring(ch, encoding="unicode") for ch in root)
    inner = _re.sub(r'\bid="([^"]+)"', lambda m: f'id="{prefix}{m.group(1)}"', inner)
    inner = _re.sub(r'url\(#([^)]+)\)', lambda m: f"url(#{prefix}{m.group(1)})", inner)
    inner = _re.sub(r'href="#([^"]+)"', lambda m: f'href="#{prefix}{m.group(1)}"', inner)
    return inner


def _data_box_mm(rendered, plan) -> list[float]:
    """The data's box on the page, in mm (from the plan's data extent and the page's map window), with SHEET_PAD round it, inside the frame."""
    mw = rendered.report["map_window"]
    fr, vis = mw["rect_mm"], mw["extent"]
    ex = [float(v) for v in plan.extent]
    fx = (fr[2] - fr[0]) / ((vis[2] - vis[0]) or 1.0)
    fy = (fr[3] - fr[1]) / ((vis[3] - vis[1]) or 1.0)
    x0, x1 = fr[0] + (ex[0] - vis[0]) * fx, fr[0] + (ex[2] - vis[0]) * fx
    y0, y1 = fr[1] + (vis[3] - ex[3]) * fy, fr[1] + (vis[3] - ex[1]) * fy
    px, py = (x1 - x0) * SHEET_PAD, (y1 - y0) * SHEET_PAD
    return [max(fr[0], x0 - px), max(fr[1], y0 - py), min(fr[2], x1 + px), min(fr[3], y1 + py)]


def sheet_grid(n: int, aspect: float, w: float, h: float) -> tuple[int, int, float, float]:
    """(columns, rows, tile width, tile height) that make the tiles largest in a w x h area, each tile of `aspect` (w / h) with its caption."""
    best = (1, n, 0.0, 0.0)
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        tw = min((w - (cols - 1) * SHEET_GAP_MM) / cols, ((h - (rows - 1) * SHEET_GAP_MM) / rows - SHEET_CAPTION_MM) * aspect)
        if tw > best[2]:
            best = (cols, rows, tw, tw / aspect)
    return best


def contact_sheet(results: list[dict], disc: dict) -> tuple[str | None, list[dict]]:
    """1.0.0 (TODO M7): the atlas's maps side by side on one sheet of the pages' own size, each tile the bare map (data, names, scale bar) with
    its value as the caption, one legend (the classes are shared, so the first page's legend is every page's), the title and the source lines.
    1.3.0 (R1-PANEL): drawn on the panel sheet's path (sheet.compose): each map is drawn again in its tile's own frame, at most 24 tiles.
    -> (SVG or None, findings)."""
    import copy as _copy

    import sheet as sh
    fs = []
    ok = [r for r in results if r.get("objects") and r["ok"]]
    if not disc.get("shared"):
        return None, [finding("MAP-ATL-010", "INFO", "no contact sheet: the maps do not share their classes (shared_classes is off), so one legend cannot stand for them")]
    if len(ok) > SHEET_MAX:
        return None, [finding("MAP-ATL-010", "INFO", f"no contact sheet: {len(ok)} maps is more than one sheet holds legibly ({SHEET_MAX}); filter the values or split the atlas")]
    if not ok:
        return None, fs
    plans = [_copy.copy(r["objects"]["plan"]) for r in ok]
    p0 = plans[0]
    scales = {round(float(r["objects"]["rendered"].report["scale"]["scale_denominator"] or 0), -3) for r in ok}
    tpl = lo_apply(p0)
    sheet_d = {"id": f"{disc.get('base_id') or p0.spec['id']}-contact-sheet",
               "title": {"text": str(disc.get("base_title") or ""), "subtitle": f"{len(ok)} maps, one legend: the classes are the same on every map"
                                                                             + ("; each map is at its own scale" if len(scales) > 1 else "")},
               "arrangement": {"gap_mm": SHEET_GAP_MM, "caption": "below"},
               "shared": {"extent": "each", "scale": "each", "legend": "shared", "locator": "none", "north_arrow": "none", "scale_bar": "each", "sources": "one"}}
    n = len(ok)
    area = lo_rect(tpl)
    exts = [[float(v) for v in p.extent] for p in plans]
    aspect = sorted((e[2] - e[0]) / max(e[3] - e[1], 1e-9) for e in exts)[n // 2]     # the median map shape
    best = (1, n, 0.0)
    for c in range(1, n + 1):                         # the arrangement that draws the maps largest
        r_ = -(-n // c)
        tw = (area.w * 0.78 - (c - 1) * SHEET_GAP_MM) / c
        th = (area.h - (r_ - 1) * SHEET_GAP_MM) / r_ - SHEET_CAPTION_MM
        size = min(tw, th * aspect)
        if size > best[2]:
            best = (c, r_, size)
    sheet_d["arrangement"].update(columns=best[0], rows=best[1])
    try:
        comp = sh.compose(sheet_d, [{"plan": p, "caption": str(r["value"])} for p, r in zip(plans, ok)], tpl, contact=True)
    except sh.SheetError as ex:
        return None, [finding("MAP-ATL-010", "INFO", f"no contact sheet: {ex}")]
    g = comp["report"].get("grid") or {}
    fs.append(finding("MAP-ATL-010", "INFO", f"contact sheet: {n} maps in {g.get('columns')} x {g.get('rows')} tiles of "
                      f"{(g.get('panel_mm') or [0, 0])[0]:.0f} x {(g.get('panel_mm') or [0, 0])[1]:.0f} mm, one shared legend"))
    return comp["svg"], fs


def lo_apply(plan):
    import layout as lo
    tpl = lo.apply_layout(lo.load_template(plan.spec.get("template") or plan.tpl_name), {"elements": {"title": {"placement": "outside"}}}, classic=True)
    tpl["name"] = plan.tpl_name
    return tpl


def lo_rect(tpl):
    import layout as lo
    return lo.rect_of(tpl["map"])


def combine_pdf(index_pdfs: list[bytes], map_pdfs: list[tuple[str, bytes]], props: dict, title: str, subject: str) -> bytes:
    """Index pages first, then each map; an outline entry for every map. The Info dictionary and XMP carry the hidden accreditation."""
    import io

    import map_export as mx
    import pikepdf
    pdf = pikepdf.new()
    for b in index_pdfs:
        with pikepdf.open(io.BytesIO(b)) as src:
            pdf.pages.extend(src.pages)
    first = len(pdf.pages)
    for _, b in map_pdfs:
        with pikepdf.open(io.BytesIO(b)) as src:
            pdf.pages.extend(src.pages)
    with pdf.open_outline() as ol:
        if index_pdfs:
            ol.root.append(pikepdf.OutlineItem("Index", 0))
        for i, (name, _) in enumerate(map_pdfs):
            ol.root.append(pikepdf.OutlineItem(name, first + i))
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return mx.stamp_pdf(buf.getvalue(), props, title, subject)


def page_count(b: bytes) -> int:
    import io

    import pikepdf
    with pikepdf.open(io.BytesIO(b)) as p:
        return len(p.pages)


# ---------------------------------------------------------------------------------------------------------------- by: time in the pipeline (M15)
def plan_extra(d: dict) -> dict:
    """The animation's share of the plan's cost: frame renders and files (counted per period and size), or nothing."""
    o = d["options"]
    if not o.get("animate"):
        return {}
    if o.get("by") != "time":
        raise AtlasError("animate needs by: time (one page per period); this atlas is one map per value")
    import animate as an
    return an.cost(len(d["values"]), an.check_sizes(o["animate_sizes"]), o["animate"])


def package_time(d: dict, results: list[dict], dest: Path, meta: dict, add_file) -> None:
    """A by-time atlas also keeps each period's MapSpec in time order (specs/, what the animation draws from) and its periods in atlas.json."""
    import shutil
    if d["options"].get("by") != "time":
        return
    sd_ = dest / "specs"
    sd_.mkdir(parents=True, exist_ok=True)
    for r in results:
        shutil.copy2(r["spec"], sd_ / Path(r["spec"]).name)
        add_file(sd_ / Path(r["spec"]).name, "spec")
    o = d["options"]
    meta.update({"by": "time", "time_field": d["field"], "unit": d["time"]["unit"], "cumulative": o["cumulative"], "base_id": d["base_id"], "title": d["base_title"],
                 "periods": [{"period": r.get("period"), "label": r["value"], "features": r["features"], "new": r.get("new"), "spec": f"specs/{Path(r['spec']).name}"}
                             for r in results]})


def stage_animate(c) -> dict:
    """map-atlas-run stage 'animate' (by: time with animate=gif,mp4): the GIF and MP4 at each size, from the packaged specs."""
    a = c.results.get("atlas") or {}
    d = a.get("disc") or {}
    o = d.get("options") or {}
    if not o.get("animate"):
        return {"ok": True, "summary": "no animation asked for", "findings": []}
    import animate as an
    dest = Path(c.results["dest"])
    try:
        r = an.animate(dest, dest / "animation", sizes=o["animate_sizes"], formats=o["animate"], hold=o["hold"], last_hold=o["last_hold"],
                       brand=c.options.get("brand"), stem=d.get("base_id"))
    except an.AnimateError as ex:
        return {"ok": False, "summary": str(ex)[:160], "findings": [finding("MAP-OUT-013", "BLOCK", str(ex))]}
    for f in r["files"]:
        c.add_file(Path(f["path"]), "animation")
    meta_p = dest / "atlas.json"
    meta = json.loads(meta_p.read_text())
    meta["animation"] = [{"file": f"animation/{Path(f['path']).name}", **{k: v for k, v in f.items() if k != "path"}} for f in r["files"]]
    meta_p.write_text(json.dumps(meta, indent=2))
    kinds = ", ".join(Path(f["path"]).name for f in r["files"])
    return {"ok": not r["blocked"], "summary": f"{len(r['files'])} animation file(s): {kinds}" + (f"; {r['notes'][0]}" if r["notes"] else ""), "findings": r["findings"]}


# ---------------------------------------------------------------------------------------------------------------- tile basemaps (1.12.0)
def tile_groups(maps: list[dict]) -> dict:
    """{composite key: first page spec that uses it} for pages that keep a tile basemap (`basemap.prepared_as`)."""
    out: dict = {}
    for m in maps:
        key = (gc.load_mapspec(Path(m["spec"])).get("basemap") or {}).get("prepared_as")
        if key and key not in out:
            out[key] = m["spec"]
    return out


def prepare_tiles(maps: list[dict], *, tier: str = "T1", http=None, env=None, sleep=None) -> dict:
    """The tile composites an atlas's pages need: one per distinct page extent, each inside the provider's cap for one map, and all of them
    together inside basemap_render.HARD_CAP tiles (an atlas is one deliverable, not a way round the cap). When the total would pass it, or a
    composite cannot be made, the pages are rewritten to the bundled basemap and a finding says why. -> {composites, tiles, fetched, findings}."""
    import time
    import yaml
    import basemap_render as br
    groups = tile_groups(maps)
    fs: list[dict] = []
    if not groups:
        return {"composites": 0, "tiles": 0, "fetched": 0, "findings": fs}

    def to_bundled(why: str, sev: str = "WARN"):
        for m in maps:
            s = gc.load_mapspec(Path(m["spec"]))
            if (s.get("basemap") or {}).get("prepared_as"):
                s["basemap"] = {"provider": "bundled"}
                Path(m["spec"]).write_text(yaml.safe_dump(s, sort_keys=False, allow_unicode=True))
        fs.append(finding("MAP-ATL-004", sev, f"the atlas draws the bundled basemap: {why}"))

    plans = {}
    for key, spec in groups.items():
        p = br.plan_render(spec, tier=tier, env=env)
        if not p["ok"]:
            why = next((f["message"] for f in p["findings"] if f["severity"] == "BLOCK"), "the tiles cannot be planned")
            to_bundled(why)
            return {"composites": 0, "tiles": 0, "fetched": 0, "findings": fs}
        plans[key] = p
    total = sum(len(p["plan"]["tiles"]) for p in plans.values())
    if total > br.HARD_CAP:
        to_bundled(f"its {len(plans)} page extents need {total} tiles together, more than the {br.HARD_CAP} one deliverable may take; "
                   "use fewer pages, mode=filter (one extent for all pages) or your own tile file")
        return {"composites": 0, "tiles": total, "fetched": 0, "findings": fs}
    fetched = 0
    for key, spec in groups.items():
        r = br.render(spec, tier=tier, http=http, env=env, outroot=br.out_dir(key), **({"sleep": sleep} if sleep else {"sleep": time.sleep}))
        if not r["ok"]:
            why = next((f["message"] for f in r["findings"] if f["severity"] == "BLOCK"), "a composite could not be made")
            to_bundled(why)
            return {"composites": 0, "tiles": total, "fetched": fetched, "findings": fs}
        fetched += int(r["record"].get("fetched") or 0)
        fs += [f for f in r["findings"] if f["severity"] == "WARN"]
    fs.append(finding("MAP-ATL-004", "INFO", f"tile basemap kept: {len(groups)} composite(s) for {sum(1 for m in maps)} page(s), {total} tile(s), "
                      f"{fetched} fetched now and the rest read from the cache"))
    return {"composites": len(groups), "tiles": total, "fetched": fetched, "findings": fs}


# ---------------------------------------------------------------------------------------------------------------- the whole run (the pipeline calls the pieces; this is the CLI path)
def build_all(spec_path, disc: dict, maps: list[dict], work: Path, formats: list[str], *, brand=None, template=None) -> list[dict]:
    import build_map as bm
    import map_plan as mp
    results = []
    fmts = list(dict.fromkeys(["pdf"] + [f for f in formats if f in ("pdf", "png", "svg", "geopdf", "geotiff")]))     # 0.26.0: georeferenced pages too
    for m in maps:
        d = work / "maps" / m["slug"]
        try:
            res = bm.build(m["spec"], str(d), fmts, brand=brand, template=template, strict=False, keep=True)
        except mp.PlanError as ex:
            results.append({**m, "ok": False, "files": [], "findings": [finding("MAP-ATL-005", "BLOCK", f"[{m['value']}] cannot be built: {str(ex).splitlines()[0][:300]}")], "objects": None})
            continue
        obj = res.pop("_objects", None)
        fs = []
        for f in res["findings"]:
            g = dict(f)
            if disc["options"]["mode"] == "focus" and f["id"] == "MAP-CART-009" and "cut by the frame" in f["message"]:
                g["severity"] = "INFO"                  # a focus map is framed on a few features; the rest are context on purpose
            g["message"] = f"[{m['value']}] {f['message']}"
            fs.append(g)
        ok = not res["blocked"]
        results.append({**m, "ok": ok, "files": res["files"], "findings": fs, "objects": obj, "stem": res["stem"], "fit": (res.get("report") or {}).get("fit")})
    return share_fit(results, work, fmts, brand=brand, template=template)


def share_fit(results: list[dict], work: Path, fmts: list[str], *, brand=None, template=None) -> list[dict]:
    """1.3.0 (R1-FIT): the pages of one atlas take one fit. The move that suits the worst page (the most cover before the move) is given to every
    page as extent.fit_pan_mm, and the pages whose own move differs are built again, so the frames stay identical from page to page."""
    import build_map as bm
    import map_plan as mp
    import yaml
    fits = [r for r in results if r.get("fit") and r.get("ok")]
    if len(fits) < 2:
        return results
    pans = {tuple(r["fit"]["pan_mm"]) for r in fits}
    if len(pans) == 1:
        return results
    worst = max(fits, key=lambda r: (r["fit"]["cover_before"], -fits.index(r)))
    rooms = [r["fit"].get("room_mm") or [0.0, 0.0, 0.0, 0.0] for r in fits]
    lo_x, hi_x = max(rm[0] for rm in rooms), min(rm[1] for rm in rooms)           # a move every page has room for (each room holds 0)
    lo_y, hi_y = max(rm[2] for rm in rooms), min(rm[3] for rm in rooms)
    wx, wy = worst["fit"]["pan_mm"]
    pan = [round(min(max(float(wx), lo_x), hi_x), 1) + 0.0, round(min(max(float(wy), lo_y), hi_y), 1) + 0.0]
    out = []
    for r in results:
        if not r.get("fit") or list(r["fit"]["pan_mm"]) == pan:
            out.append(r)
            continue
        spec = gc.load_mapspec(Path(r["spec"]))
        spec.setdefault("extent", {})["fit_pan_mm"] = pan
        Path(r["spec"]).write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
        d = work / "maps" / r["slug"]
        try:
            res = bm.build(r["spec"], str(d), fmts, brand=brand, template=template, strict=False, keep=True)
        except mp.PlanError:
            out.append(r)
            continue
        obj = res.pop("_objects", None)
        fs = [dict(f, message=f"[{r['value']}] {f['message']}") for f in res["findings"]]
        out.append({**r, "ok": not res["blocked"], "files": res["files"], "findings": fs, "objects": obj, "stem": res["stem"],
                    "fit": (res.get("report") or {}).get("fit")})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["plan", "build"])
    ap.add_argument("spec")
    ap.add_argument("--layer")
    ap.add_argument("--field")
    ap.add_argument("--mode", default="filter")
    ap.add_argument("--values")
    ap.add_argument("--max-maps", type=int, default=DEFAULT_MAX)
    ap.add_argument("--out")
    ap.add_argument("--formats", default="pdf")
    ap.add_argument("--no-shared-classes", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--by", default="field", choices=["field", "time"], help="time: one map per period of the layer's time_field (M15)")
    ap.add_argument("--unit", default="auto", help="by time: auto, year, quarter, month or day")
    ap.add_argument("--cumulative", action="store_true", help="by time: each map shows everything up to its period, the period's own features larger")
    ap.add_argument("--no-planned", action="store_true", help="by time, cumulative: leave out the features still planned")
    ap.add_argument("--animate", help="by time: gif,mp4 written after the atlas (map_export --animate)")
    a = ap.parse_args(argv)
    opt = {"layer": a.layer, "field": a.field, "mode": a.mode, "values": a.values, "max_maps": a.max_maps,
           "shared_classes": False if a.no_shared_classes else True, "index_page": False if a.no_index else True,
           "by": a.by, "unit": a.unit, "cumulative": a.cumulative, **({"planned": False} if a.no_planned else {}), **({"animate": a.animate} if a.animate else {})}
    try:
        d = discover(a.spec, opt)
    except AtlasError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.fid, "BLOCK", str(ex))]}, indent=2))
        return 3
    if a.op == "plan":
        rep = {"ok": True, "layer": d["layer"], "field": d["field"], "mode": d["options"]["mode"], "maps": len(d["values"]), "values": d["values"], "shared": d["shared"],
               "findings": d["findings"]}
        if d.get("time"):
            rep.update({"by": "time", "unit": d["time"]["unit"], "periods": [d["time"]["labels"][v] for v in d["values"]], "cumulative": d["options"]["cumulative"]})
            rep.update({"animation": plan_extra(d)} if d["options"].get("animate") else {})
        print(json.dumps(rep, indent=2, default=str))
        return 0
    if not a.out:
        print("atlas: build needs --out", file=sys.stderr)
        return 2
    import pipelines as pl
    plan = pl.plan("map-atlas-run", {"spec": a.spec}, {**opt, "out": a.out, "formats": a.formats.split(",")})
    r = pl.run("map-atlas-run", {"spec": a.spec}, {**opt, "out": a.out, "formats": a.formats.split(",")}, confirm=plan.get("confirm_token"))
    print(json.dumps({"ok": r["ok"], "deliverable": (r.get("run") or {}).get("deliverable"), "findings": r["findings"]}, indent=2, default=str))
    return 0 if r["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
