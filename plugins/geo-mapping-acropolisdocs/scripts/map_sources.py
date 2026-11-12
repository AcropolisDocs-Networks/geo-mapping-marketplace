#!/usr/bin/env python3
"""map_sources: two MapSpec layer sources that reach the network once and then build offline (M13 integration).

1. A library entry served by a public data service, drawn as a layer:

       layers:
         - id: flood
           source: {type: ref, id: ref.context.us.hazard.fema-nfhl, where: "SFHA_TF = 'T'"}
           role: polygons

   The entry's `delivery.mode` is `service`, it names a shipped profile (`delivery.profile`) and a layer (`delivery.layer`), and
   `delivery.layer_kind` (or `delivery.geometry`) says the layer holds points, lines or polygons. The build fetches that layer for the map
   window through the service capture path (service_query.capture: capabilities and field names checked, the count asked for, stable paging,
   the capture record) with the profile's caps, once. The window is `source.bbox`, else the map's extent (bbox, place or preset), else the
   other layers' bounds, widened to the map frame's shape and margin. The capture is kept in the working folder
   (`captures/ref/<entry>-<key>/`), keyed by the entry, its rev, the layer, the filter, the fields and the window, so a rebuild of the same map
   reads it and sends no request; `source.refresh: true` fetches again. More features in the window than the profile's cap: the first cap's
   worth are captured, the record is marked truncated, and the build reports the existing MAP-SVC-005 ("the result is incomplete");
   `source.accept_incomplete: <why>` makes it a WARN. A value-only service (the ACS API), a portal of many layers and a raster service are
   refused with what to do instead.

2. A table of addresses geocoded into points:

       source: {type: geocode, table: venues.csv, address: [street, city, state, zip], provider: census, purpose: venues}

   through geocode.geocode_table: the privacy gate first (MAP-SVC-035: a declared purpose, no column naming a private person), the
   provider's terms (MAP-SVC-036), then the geocoder; unmatched rows are left out and counted (MAP-SVC-037). The points and the geocoding
   record are cached in `captures/geocode/` by the table's SHA-256 and the request, so a rebuild sends nothing; the gate runs on every build.

Both return what map_plan.load_source returns: (gdf, entry, crs_assumed, notes, capture). A note dict may carry `step`, a sentence for the
methods page (the layer's transformations).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

VECTOR_KINDS = {"point": "point", "points": "point", "multipoint": "point", "line": "line", "lines": "line", "polyline": "line", "linestring": "line",
                "multilinestring": "line", "polygon": "polygon", "polygons": "polygon", "multipolygon": "polygon"}
RASTER_KINDS = ("raster", "image", "grid", "coverage")
FRAME_MARGIN = 0.10          # beyond the extent padding: the frame's own margin around the visible area (map_plan.plan_visible_pad)
MIN_WINDOW_DEG = 0.002       # a window around a single point is at least this wide (about 200 m)
WINDOW_DECIMALS = 4          # the window is rounded outward to 1/10,000 degree, so the same map asks for the same box


class SourceError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME):
        super().__init__(message)
        self.code = code


def _plan_error(message: str, code: int):
    import map_plan as mp
    return mp.PlanError(message, code)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:60] or "layer"


# ---------------------------------------------------------------------------------------------------------------- library service entries
_ENTRIES: dict = {"key": None, "by_id": {}}


def _fingerprint() -> tuple:
    """Every entry file's path, size and modification time: reading the library costs about a second, a listing a few milliseconds."""
    import ref_library as rl
    out = []
    for root in (rl.shipped_lib() / "entries", rl.local_lib() / "entries"):
        for p in sorted(root.rglob("*.yaml")) if root.is_dir() else []:
            s = p.stat()
            out.append((str(p), s.st_size, s.st_mtime_ns))
    return tuple(out)


def _entries() -> dict:
    """The library by id (read-only; a local entry of the same id wins, as in ref_fetch), read again only when an entry file changed."""
    import ref_library as rl
    key = _fingerprint()
    if _ENTRIES["key"] == key:
        return _ENTRIES["by_id"]
    out: dict = {}
    for e in rl.all_entries("all"):
        if "id" in e and (e["id"] not in out or e.get("_scope") == "local"):
            out[e["id"]] = e
    _ENTRIES.update(key=key, by_id=out)
    return out


def service_entry(entry_id) -> dict | None:
    """The library entry (a copy) when it is delivered by a data service (`delivery.mode: service`), else None (unknown ids included)."""
    import copy
    if not entry_id:
        return None
    e = _entries().get(str(entry_id))
    return copy.deepcopy(e) if e and (e.get("delivery") or {}).get("mode") == "service" else None


def layer_kind(entry: dict) -> str | None:
    """point | line | polygon when the entry's service layer holds vector geometry (`delivery.layer_kind`, else `delivery.geometry`), else None."""
    d = entry.get("delivery") or {}
    k = str(d.get("layer_kind") or d.get("geometry") or "").strip().lower().replace("esrigeometry", "")
    return VECTOR_KINDS.get(k)


def _placeholder(layer) -> bool:
    return not layer or str(layer).strip().startswith("(")


def refusal(entry: dict) -> str:
    """Why a service entry cannot be drawn as a layer, and what to do instead."""
    eid = entry["id"]
    d = entry.get("delivery") or {}
    k = str(d.get("layer_kind") or d.get("geometry") or "").strip().lower()
    if k in RASTER_KINDS or str(d.get("protocol") or "").lower() in ("wms", "wmts", "arcgis-imageserver"):
        return (f"library entry '{eid}' is a raster or image service ({d.get('protocol') or k}), not a vector layer: it is not fetched as features. "
                "Use it as a raster (raster-data, terrain-analysis) or a tile background (basemap-render)")
    if d.get("profile") and _placeholder(d.get("layer")):
        return (f"library entry '{eid}' is a portal of many layers (profile {d['profile']}): choose one with geo-service-explore, capture it "
                f"(service_query.py fetch --profile {d['profile']} --layer <layer> --bbox=...) and draw it with source: {{type: service, capture: <capture id>}}")
    if not d.get("profile") or not d.get("layer"):
        return (f"library entry '{eid}' is a data service that supplies values, not geometry: join its values to a boundary layer "
                f"({', '.join(entry.get('pairs_with') or []) or 'a library boundary entry'}) with join:, using the request in its delivery.request")
    return (f"library entry '{eid}' does not say what geometry its service layer holds (delivery.layer_kind: point, line or polygon), "
            "so it cannot be drawn as a layer; capture it with service_query.py fetch and draw the capture")


def load_refusal(entry: dict) -> str:
    """The message for map_plan.load_ref_layer, which reads files: a vector service layer is drawn through a MapSpec layer instead."""
    if layer_kind(entry) and entry.get("delivery", {}).get("profile") and not _placeholder(entry["delivery"].get("layer")):
        return (f"library entry '{entry['id']}' is a layer of a data service (profile {entry['delivery']['profile']}), not a file: draw it as a MapSpec layer "
                f"with source: {{type: ref, id: {entry['id']}}}, which fetches it for the map window into a capture")
    return refusal(entry)


def is_service_layer(ly: dict, entries: dict | None = None) -> bool:
    """A MapSpec layer whose source is a library entry delivered by a data service."""
    src = ly.get("source") or {}
    if src.get("type") != "ref":
        return False
    e = (entries if entries is not None else _entries()).get(str(src.get("id")))
    return bool(e and (e.get("delivery") or {}).get("mode") == "service")


def defer(order: list, spec: dict) -> list:
    """The load order with the library service layers that no other layer's ops name moved to the end, so the layers the window is fitted to
    are loaded first. Draw order is the spec's own and does not change."""
    import mapspec_lint as ml
    named = {r for ly in spec.get("layers") or [] for r in ml.op_layer_refs(ly)}
    refs = [ly for ly in order if (ly.get("source") or {}).get("type") == "ref"]
    if not refs:
        return list(order)
    entries = _entries()
    late = [ly for ly in refs if ly["id"] not in named and is_service_layer(ly, entries)]
    return [ly for ly in order if ly not in late] + late


# ---------------------------------------------------------------------------------------------------------------- the window
def _round_out(b) -> tuple:
    f = 10 ** WINDOW_DECIMALS
    x0, y0, x1, y1 = b
    return (math.floor(x0 * f) / f, math.floor(y0 * f) / f, math.ceil(x1 * f) / f, math.ceil(y1 * f) / f)


def frame_window(wgs, tpl: dict | None, padding_pct: float = 5.0) -> tuple:
    """A lon/lat box that covers what the map frame will show around `wgs`: widened to the frame's shape (the frame fills its rectangle, so a
    tall extent on a landscape sheet shows more to east and west), then the extent padding and the frame margin on every side; rounded outward."""
    x0, y0, x1, y1 = (float(v) for v in wgs)
    if x1 - x0 < MIN_WINDOW_DEG:
        c = (x0 + x1) / 2
        x0, x1 = c - MIN_WINDOW_DEG / 2, c + MIN_WINDOW_DEG / 2
    if y1 - y0 < MIN_WINDOW_DEG:
        c = (y0 + y1) / 2
        y0, y1 = c - MIN_WINDOW_DEG / 2, c + MIN_WINDOW_DEG / 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    kx = max(math.cos(math.radians(max(-85.0, min(85.0, cy)))), 0.05)          # ground width of a degree of longitude, relative to latitude
    w, h = (x1 - x0) * kx, (y1 - y0)
    m = (tpl or {}).get("map") or {}
    aspect = float(m.get("w") or 0) / float(m.get("h") or 1) if m.get("w") and m.get("h") else None
    if aspect:
        if w / h < aspect:
            w = h * aspect
        else:
            h = w / aspect
    grow = 1.0 + 2 * (float(padding_pct) / 100.0 + FRAME_MARGIN)
    w, h = w * grow, h * grow
    hw, hh = w / kx / 2, h / 2
    return _round_out((max(cx - hw, -180.0), max(cy - hh, -90.0), min(cx + hw, 180.0), min(cy + hh, 90.0)))


def _bounds4326(gdf):
    if gdf is None or not len(gdf) or gdf.crs is None:
        return None
    g = gdf if gdf.crs.equals("EPSG:4326") else gdf.to_crs("EPSG:4326")
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    if not len(g):
        return None
    return tuple(float(v) for v in g.total_bounds)


def window(spec: dict, tpl: dict | None, layer: dict, loaded: dict, base: Path) -> tuple:
    """(lon/lat box, what it came from) for a library service layer. `loaded` holds the frames of the layers loaded so far, by id."""
    import map_plan as mp
    src = layer.get("source") or {}
    ext = spec.get("extent") or {}
    pad = float(ext.get("padding_pct", 5))
    if src.get("bbox"):
        b = [float(v) for v in src["bbox"]]
        if len(b) != 4 or not (b[0] < b[2] and b[1] < b[3]):
            raise _plan_error(f"layer '{layer['id']}': source.bbox is four numbers, west, south, east, north in degrees", gc.EXIT_USAGE)
        return _round_out(b), "source.bbox"
    mode = ext.get("mode", "fit_layers")
    if mode == "bbox" and ext.get("bbox"):
        return frame_window(ext["bbox"], tpl, pad), "the map extent (bbox)"
    if mode == "place":
        return frame_window(mp.place_bbox(ext.get("place", "")), tpl, pad), f"the map extent (place {ext.get('place')})"
    if mode == "preset" and ext.get("preset") in mp.PRESET_BBOX:
        return frame_window(mp.PRESET_BBOX[ext["preset"]], tpl, pad), f"the map extent (preset {ext.get('preset')})"
    entries = _entries()
    others = [ly for ly in spec.get("layers") or [] if ly is not layer and not is_service_layer(ly, entries)]
    drawn = [ly for ly in others if ly.get("draw", True) is not False and ly.get("role") in mp.DATA_ROLES]
    parts = []
    for ly in drawn or others:
        gdf = loaded.get(ly["id"])
        if gdf is None:
            gdf = mp.load_source(ly, base)[0]             # not loaded yet (it waits for this layer through its ops): read it for its bounds only
        b = _bounds4326(gdf)
        if b:
            parts.append(b)
    if not parts:
        raise _plan_error(f"layer '{layer['id']}': a library service layer is fetched for the map window, and this map has no other layer to fit; "
                          "give extent: {mode: bbox, bbox: [west, south, east, north]}, extent.place, or source.bbox", gc.EXIT_USAGE)
    wgs = (min(p[0] for p in parts), min(p[1] for p in parts), max(p[2] for p in parts), max(p[3] for p in parts))
    return frame_window(wgs, tpl, pad), "the other layers' extent"


# ---------------------------------------------------------------------------------------------------------------- fetch and cache
def make_http(profile: dict):
    """The guarded client for a fetch (tests replace it)."""
    import service_query as sq
    return sq.make_http(profile)


def cache_root() -> Path:
    return gc.outputs_dir() / "captures" / "ref"


def request_for(entry: dict, src: dict, bbox: tuple, profile: dict) -> dict:
    import service_query as sq
    d = entry["delivery"]
    props = src.get("properties")
    props = [p.strip() for p in props.split(",")] if isinstance(props, str) else (list(props) if props else None)
    cap = int((profile.get("limits") or {}).get("max_total_features") or 200_000)
    return sq.request_from({"profile": d["profile"], "protocol": d.get("protocol") or "arcgis", "layer": d["layer"], "cql_filter": src.get("where") or None,
                            "bbox": [float(v) for v in bbox], "srs": "EPSG:4326", "properties": props, "max_features": cap})


def cache_key(entry: dict, req: dict) -> str:
    key = {"id": entry["id"], "rev": entry.get("rev"), **{k: req.get(k) for k in ("profile", "protocol", "layer", "cql_filter", "bbox", "properties", "max_features")}}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:10]


def cached_record(folder: Path) -> Path | None:
    """The capture record of an earlier fetch of the same request, when its data file is still there."""
    for rp in sorted(folder.glob("*/capture.json")) if folder.is_dir() else []:
        try:
            rec = json.loads(rp.read_text())
        except (OSError, ValueError):
            continue
        res = rec.get("result") or {}
        p = Path(res.get("path") or "")
        if res.get("path") and (p.is_file() or (rp.parent / p.name).is_file()):
            return rp
    return None


def _mark_truncated(rp: Path, rec: dict, profile: dict, accept: str | None) -> dict:
    """More features in the window than the cap: the capture holds the first cap's worth. It is recorded as truncated (and, when the person
    said why, accepted), so the build's capture lint reports the existing MAP-SVC-005."""
    import service_lint as sl
    rec["truncated"] = True
    note = f"the window holds {rec.get('hits_expected')} features; the profile's cap is {rec.get('received')}: only those were captured"
    if note not in (rec.get("notes") or []):
        rec.setdefault("notes", []).append(note)
    if accept:
        rec["accepted_incomplete"] = str(accept)[:300]
    rec["findings"] = [f for f in (rec.get("findings") or []) if f.get("id") != "MAP-SVC-005"] + [f for f in sl.lint_capture(rec, profile) if f.get("id") == "MAP-SVC-005"]
    rp.write_text(json.dumps(rec, indent=2, default=str))
    return rec


def fetch(entry: dict, src: dict, bbox: tuple, *, refresh: bool = False, http=None) -> tuple[Path, bool]:
    """(capture record path, came from the cache). Raises SourceError for a refusal, a service failure or a BLOCK finding."""
    import ogc_http as oh
    import service_query as sq
    d = entry["delivery"]
    try:
        profile = sq.need_mcp_profile(d["profile"])
    except FileNotFoundError as ex:
        raise SourceError(f"library entry '{entry['id']}' names the service profile '{d['profile']}', which is neither shipped nor saved: {ex}", gc.EXIT_USAGE)
    except oh.Refusal as ex:
        raise SourceError(f"{ex.rule or 'MAP-SVC-019'} library entry '{entry['id']}': the profile '{d['profile']}' cannot be used by the plugin's client: {ex}", gc.EXIT_REFUSED)
    req = request_for(entry, src, bbox, profile)
    folder = cache_root() / f"{slug(entry['id'])}-{cache_key(entry, req)}"
    if not refresh:
        hit = cached_record(folder)
        if hit is not None:
            rec = json.loads(hit.read_text())
            accept = str(src["accept_incomplete"])[:300] if src.get("accept_incomplete") else None
            if rec.get("truncated") and rec.get("accepted_incomplete") != accept:     # the MapSpec's acceptance changed; the data did not
                rec.pop("accepted_incomplete", None)
                _mark_truncated(hit, rec, profile, accept)
            return hit, True
    folder.mkdir(parents=True, exist_ok=True)
    try:
        r = sq.capture(http or make_http(profile), profile, req, outroot=folder, resume=not refresh)
    except oh.Refusal as ex:
        raise SourceError(f"{ex.rule or 'MAP-SVC-020'} library entry '{entry['id']}': {ex}", gc.EXIT_REFUSED)
    except oh.ServiceError as ex:
        raise SourceError(f"{ex.rule or 'MAP-SVC-013'} library entry '{entry['id']}': the service did not answer the fetch for the map window: {ex}", gc.EXIT_SERVICE)
    rec = r.get("record")
    blocks = [f for f in r.get("findings") or [] if f.get("severity") == "BLOCK"]
    if rec is None:
        f0 = blocks[0] if blocks else {"id": "MAP-SVC-013", "message": "no capture was made"}
        raise SourceError(f"{f0['id']} library entry '{entry['id']}': {f0['message']}", gc.EXIT_LINT)
    rp = Path(r["record_path"])
    if rec.get("hits_expected") is not None and (rec.get("received") or 0) < rec["hits_expected"]:
        rec = _mark_truncated(rp, rec, profile, src.get("accept_incomplete"))
    hard = [f for f in blocks if f.get("id") not in ("MAP-SVC-005", "MAP-SVC-012")]
    if hard:
        raise SourceError(f"{hard[0]['id']} library entry '{entry['id']}': {hard[0]['message']}", gc.EXIT_LINT)
    if not rec.get("result"):
        raise SourceError(f"library entry '{entry['id']}': nothing of the layer lies in the map window {list(bbox)}"
                          + (f" with {src['where']}" if src.get("where") else "") + "; check the window or the filter", gc.EXIT_USAGE)
    return rp, False


def load_service_layer(layer: dict, base: Path, window_fn=None):
    """A `source: {type: ref, id: <service entry>}` layer -> (gdf, entry, False, notes, capture record)."""
    import map_plan as mp
    lid = layer["id"]
    src = layer["source"]
    e = service_entry(src.get("id"))
    if e is None:
        raise mp.PlanError(f"MAP-REF-001: the reference library has no service entry '{src.get('id')}'", gc.EXIT_USAGE)
    st = e.get("status")
    if st in ("blocked", "retired", "draft"):
        why = (e.get("blocked") or {}).get("why") if st == "blocked" else None
        raise mp.PlanError(f"MAP-REF-002: library entry '{e['id']}' is {st}" + (f": {why}" if why else "; it carries no verified service layer"), gc.EXIT_REFUSED)
    kind = layer_kind(e)
    if not kind or _placeholder((e.get("delivery") or {}).get("layer")) or not (e.get("delivery") or {}).get("profile"):
        raise mp.PlanError(f"layer '{lid}': {refusal(e)}", gc.EXIT_REFUSED)
    if window_fn is None:
        raise mp.PlanError(f"layer '{lid}': a library service layer is fetched for the map window, which only a map build knows", gc.EXIT_USAGE)
    bbox, why = window_fn()
    try:
        rp, cached = fetch(e, src, bbox, refresh=bool(src.get("refresh")))
    except SourceError as ex:
        raise mp.PlanError(f"layer '{lid}': {ex}", ex.code)
    gdf, _none, _assumed, notes, rec = mp.load_capture({"id": lid, "source": {"type": "service", "capture": str(rp)}}, base)
    rec["library_entry"] = {"id": e["id"], "rev": e.get("rev")}
    where = f" where {src['where']}" if src.get("where") else ""
    how = "read from the capture made on an earlier build; no request was sent" if cached else "fetched for this map"
    step = (f"library entry {e['id']} rev {e.get('rev')}: layer {e['delivery']['layer']} of the {e['delivery']['profile']} service{where}, inside the box "
            f"{', '.join(f'{v:g}' for v in bbox)} (lon/lat; {why}); {how}")
    notes = list(notes) + [{"id": "IO", "sev": "INFO", "message": step, "step": step}]
    if kind and len(gdf):
        got = mp.geom_kind(gdf)
        if got != kind:
            notes.append({"id": "IO", "sev": "WARN", "message": f"the entry says the layer holds {kind}s, the capture holds {got}s"})
    return gdf, e, False, notes, rec


def entry_provenance(p: dict, e: dict) -> dict:
    """The library entry's part of a service layer's provenance (source, vintage, license, attribution, library_ref and rev); the capture adds
    when and how it was fetched (map_plan.capture_provenance only fills what is still missing)."""
    src = e.get("source") or {}
    lic = e.get("license") or {}
    p.setdefault("source", f"{src.get('publisher', '')}, {src.get('product', e.get('title', ''))}".strip(", "))
    p.setdefault("url", src.get("url"))
    if (e.get("vintage") or {}).get("current"):
        p.setdefault("vintage", e["vintage"]["current"])
    if lic.get("class"):
        p.setdefault("license", lic["class"])
    if lic.get("attribution_text"):
        p.setdefault("attribution", lic["attribution_text"])
    p["library_ref"] = e["id"]
    p["library_rev"] = e.get("rev")
    return p


# ---------------------------------------------------------------------------------------------------------------- geocoded tables
GEOCODE_OPTS: dict = {}       # extra keywords for geocode.geocode_table (tests pass a transport and a resolver)


def geocode_root() -> Path:
    return gc.outputs_dir() / "captures" / "geocode"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_geocode(layer: dict, base: Path):
    """A `source: {type: geocode, table, address, provider, purpose, ...}` layer -> (points in EPSG:4326, None, False, notes, None)."""
    import geo_io
    import geocode as gcd
    import map_plan as mp
    lid = layer["id"]
    src = layer["source"]
    if not src.get("table"):
        raise mp.PlanError(f"layer '{lid}': a geocode source needs table: (a CSV, TSV or XLSX of addresses beside the MapSpec)", gc.EXIT_USAGE)
    p = Path(str(src["table"]))
    p = p if p.is_absolute() else base / p
    if not p.is_file():
        raise mp.PlanError(f"layer '{lid}': table not found: {src['table']}", gc.EXIT_USAGE)
    provider = str(src.get("provider") or "census").strip().lower()
    purpose = src.get("purpose")
    country = str(src.get("country") or "us")
    terms = bool(src.get("terms_acknowledged"))
    id_col = src.get("id_column")
    try:
        df = gcd.read_table(p, src.get("sheet"))
        cols = gcd.address_columns(df, src.get("address"))
        if id_col and id_col not in df.columns:
            raise gcd.GeocodeError(f"id_column '{id_col}' is not a column of {p.name}")
        blocks = [f for f in gcd.gate(df, cols, purpose, provider, terms, country) if f["severity"] == "BLOCK"]
    except gcd.GeocodeError as ex:
        raise mp.PlanError(f"layer '{lid}': geocode: {ex}", gc.EXIT_USAGE)
    if blocks:                                                   # the privacy gate and the terms run on every build, cached or not
        raise mp.PlanError(f"layer '{lid}': geocoding refused: " + "; ".join(f"{f['id']} {f['message']}" for f in blocks), gc.EXIT_REFUSED)
    sha = _sha256_file(p)
    key = hashlib.sha256(json.dumps({"sha256": sha, "address": cols, "provider": provider, "purpose": purpose, "country": country, "id_column": id_col},
                                    sort_keys=True).encode()).hexdigest()[:10]
    out = geocode_root() / f"{slug(p.stem)}-{key}.gpkg"
    rec_p = out.with_suffix(".geocode.json")
    cached = out.is_file() and rec_p.is_file() and not src.get("refresh")
    if cached:
        try:
            report = json.loads(rec_p.read_text())
            gdf = geo_io.read(out).gdf
        except (OSError, ValueError, geo_io.GeoIOError):
            cached = False
    if not cached:
        try:
            gdf, report = gcd.geocode_table(df, cols, provider, purpose, country, terms, id_col=id_col, **GEOCODE_OPTS)
        except gcd.GeocodeRefused as ex:
            raise mp.PlanError(f"layer '{lid}': geocoding refused: " + "; ".join(f"{f['id']} {f['message']}" for f in ex.findings), gc.EXIT_REFUSED)
        except gcd.GeocodeError as ex:
            raise mp.PlanError(f"layer '{lid}': geocode: {ex}", gc.EXIT_USAGE)
        except Exception as ex:  # noqa: BLE001 - a network or service failure while geocoding
            raise mp.PlanError(f"layer '{lid}': the {provider} geocoder did not answer: {str(ex)[:200]}", gc.EXIT_SERVICE)
        report.update({"input": p.name, "input_sha256": sha, "result": out.name})
        gcd.write_points(gdf, out)
        rec_p.write_text(json.dumps(report, indent=2, default=str))
    kept = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if not len(kept):
        raise mp.PlanError(f"layer '{lid}': the {provider} geocoder matched none of the {report.get('rows')} addresses in {p.name}; check the address columns ({', '.join(cols)})", gc.EXIT_USAGE)
    rate = report.get("match_rate")
    step = (f"geocoded {report.get('matched')} of {report.get('rows')} addresses from {p.name} (SHA-256 {sha[:16]}) with the {provider} geocoder "
            f"(purpose: {purpose}; columns {', '.join(cols)}); {report.get('unmatched_total', 0)} unmatched row(s) left out"
            + (f"; match rate {rate * 100:.0f} percent" if isinstance(rate, (int, float)) else "")
            + ("; read from the geocoding record of an earlier build, no request was sent" if cached else ""))
    notes = [{"id": "IO", "sev": "INFO", "message": step, "step": step}]
    for f in report.get("findings") or []:
        if f.get("severity") in ("WARN", "BLOCK"):
            notes.append({"id": f.get("id"), "sev": "WARN", "message": f"{f.get('id')} {f.get('message')}"})
    prov = layer.setdefault("provenance", {})                    # in memory only: the layer's provenance names the table and the geocoder
    if isinstance(prov, dict):
        prov.setdefault("source", f"{p.name}, geocoded with the {provider} geocoder")
        prov.setdefault("attribution", report.get("attribution"))
        prov.setdefault("license", "supplied by the user; " + ("ODbL 1.0 (geocoded positions)" if provider == "nominatim" else "positions public domain (U.S. Census Bureau)"))
        prov.setdefault("retrieved_at", str(report.get("finished_at") or "")[:10] or None)
    return kept, None, False, notes, None
