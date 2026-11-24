#!/usr/bin/env python3
"""webmap_live (0.15.0): live service layers in a web page.

A layer with `source: {type: service, mode: live, profile: P, protocol: ...}` is drawn by the viewer's browser straight from the service: a WMS as
tiles (GetMap per tile, with `time` for WMS-T), a WMTS or XYZ tile template, MVT vector tiles (MapLibre only), or a CORS-enabled WFS read once as
GeoJSON for the page's area. The build stays offline: nothing here sends a request. What it checks is the profile: saved and host-confirmed,
public, without credentials (MAP-SVC-017), its host not withheld, https, and every address on that one host (MAP-SVC-043). Each host goes into
the page's host manifest and Content-Security-Policy and nothing else does.

A printed map never draws a live layer (MAP-SVC-014 stays a refusal there): the page's static fallback picture is drawn without it and says so.
A live layer is not reproducible, so it carries MAP-SVC-014 as a warning and a note under the map. Its legend is the picture the person fetched
with ogc_get_legend (embedded), or the service's GetLegendGraphic, loaded live; a click on a WMS layer asks GetFeatureInfo and shows the answer as
text (`popup`, needs CORS).
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from pathlib import Path

PROTOCOLS = ("wms", "wmts", "xyz", "mvt", "wfs")
RASTER = ("wms", "wmts", "xyz")
GEOMETRIES = ("polygon", "line", "point")
WFS_DEFAULT_MAX = 2000
WFS_CEILING = 10000
TILE_SLOTS = (("{TileMatrix}", "{z}"), ("{TileRow}", "{y}"), ("{TileCol}", "{x}"), ("{tilematrix}", "{z}"), ("{tilerow}", "{y}"), ("{tilecol}", "{x}"))


def finding(fid: str, sev: str, message: str, detail=None, layer: str | None = None) -> dict:
    d = {"id": fid, "severity": sev, "message": message, "detail": detail}
    if layer:
        d["layer"] = layer
    return d


def is_live(layer: dict) -> bool:
    src = layer.get("source") or {}
    return isinstance(src, dict) and src.get("type") == "service" and src.get("mode") == "live"


def split(spec: dict) -> list[dict]:
    """Take the live layers out of the spec (in memory) before the plan is made: the plan, and the static fallback drawn from it, never see them.
    Labels that name them go too. Returns the live layers in their order."""
    live = [dict(l) for l in spec.get("layers") or [] if isinstance(l, dict) and is_live(l)]
    if live:
        ids = {l.get("id") for l in live}
        spec["layers"] = [l for l in spec["layers"] if not (isinstance(l, dict) and is_live(l))]
        if isinstance(spec.get("labels"), list):
            spec["labels"] = [lb for lb in spec["labels"] if not (isinstance(lb, dict) and lb.get("layer") in ids)]
    return live


def _on_host(url: str, host: str) -> bool:
    u = urllib.parse.urlsplit(re.sub(r"\{[^}]*\}", "0", url))
    return u.scheme == "https" and (u.hostname or "").lower() == host.lower()


def _tile_template(t: str) -> str:
    for a, b in TILE_SLOTS:
        t = t.replace(a, b)
    return t


def _legend_picture(path: str | None, base_dir: Path, layer_names: str) -> tuple[str | None, str | None]:
    """(data URI, where from) of a legend picture the person fetched with ogc_get_legend, or (None, None)."""
    import gmap_common as gc
    import service_query as sq
    cands = []
    if path:
        p = Path(path)
        cands.append(p if p.is_absolute() else base_dir / p)
    else:
        cands.append(gc.outputs_dir(False) / "captures" / "legends" / f"{sq.slug(layer_names)}.png")
    for c in cands:
        try:
            b = c.read_bytes()
        except OSError:
            continue
        if b.startswith(b"\x89PNG\r\n\x1a\n") and len(b) <= 512_000:
            return "data:image/png;base64," + base64.b64encode(b).decode("ascii"), str(c)
    return None, None


def resolve(live: list[dict], *, tokens: dict, base_dir: Path, controls: dict) -> dict:
    """The page definitions of the live layers, with findings, legend blocks, notes, and the hosts each engine needs."""
    import ogc_client as oc
    import service_registry as sr
    out = {"defs": [], "findings": [], "blocks": [], "notes": [], "hosts": [], "credits": []}
    for ly in live:
        lid, src = ly.get("id"), dict(ly.get("source") or {})
        title = ly.get("title") or lid
        proto = str(src.get("protocol") or "").lower()

        def refuse(msg, rule="MAP-SVC-043"):
            out["findings"].append(finding(rule, "BLOCK", f"live layer '{lid}': {msg}", layer=lid))

        if proto not in PROTOCOLS:
            refuse(f"protocol is one of {', '.join(PROTOCOLS)}, not '{proto or 'unset'}'")
            continue
        name = src.get("profile")
        if not name:
            refuse("a live layer names its saved, host-confirmed profile (source.profile); the page then calls only that host")
            continue
        try:
            prof = sr.load(str(name))
        except FileNotFoundError as e:
            refuse(str(e))
            continue
        fs = [f for f in sr.validate(prof, route="live") if f["severity"] == "BLOCK"]
        if fs:
            for f in fs:
                out["findings"].append({**f, "message": f"live layer '{lid}': {f['message']}", "layer": lid})
            continue
        net, auth = prof.get("network") or {}, (prof.get("auth") or {}).get("method", "none")
        if auth != "none":
            refuse("a live layer cannot carry credentials: the viewer's browser calls the service, so the profile's login would be in the page", "MAP-SVC-017")
            continue
        if not net.get("public", False) or prof.get("host_visibility") == "withheld" or not prof.get("host_confirmed_on"):
            refuse("the profile is not a public, host-confirmed service (network.public, host_confirmed_on), or its host is withheld; a page shows its host to every viewer")
            continue
        host = (sr.host_of(prof) or "").lower()
        if not prof.get("base_url", "").lower().startswith("https://"):
            refuse("the service is not https; a page on https cannot load it (mixed content)")
            continue
        credit = ((prof.get("attribution") or (prof.get("license") or {}).get("attribution")) or (ly.get("provenance") or {}).get("source") or prof.get("title") or host)
        vis = ly.get("visible", True) is not False
        d = {"id": lid, "title": title, "kind": "live", "live": proto, "role": ly.get("role") or ("raster" if proto in RASTER else {"polygon": "polygons", "line": "lines", "point": "points"}.get(str(src.get("geometry")), "polygons")), "visible": vis,
             "opacity": float(ly.get("opacity", src.get("opacity", 0.8 if proto in RASTER else 1.0))), "slider": bool(controls.get("opacity")), "host": host,
             "credit": str(credit)[:200], "searchable": False, "downloadable": False, "context": False, "popup": None, "info": None,
             "raster": proto in RASTER, "minzoom": int(src.get("min_zoom", 0) or 0), "maxzoom": int(src.get("max_zoom", 19) or 19),
             "needs_cors": proto in ("mvt", "wfs"), "profile": prof.get("profile"), "cors_checked": src.get("cors_checked")}
        if proto == "wms":
            names = src.get("layers")
            names = ",".join(str(n) for n in names) if isinstance(names, list) else str(names or src.get("layer") or "")
            if not names or not re.fullmatch(r"[A-Za-z0-9_.:,\- ]{1,300}", names):
                refuse("a live WMS layer names its WMS layers (source.layers)")
                continue
            ep = oc.endpoint(prof, "wms")
            if not _on_host(ep, host):
                refuse("the WMS address is not https on the profile's host", "MAP-SVC-001")
                continue
            try:
                tm = oc.check_time(src.get("time"))
            except oc.Refusal as e:
                out["findings"].append(finding("MAP-SVC-039", "BLOCK", f"live layer '{lid}': {e}", layer=lid))
                continue
            fmt = str(src.get("format") or "image/png")
            styles = ",".join(src.get("styles") or [])
            params = {"layers": names, "styles": styles, "format": fmt, "transparent": True, "version": "1.3.0"}
            if tm:
                params["time"] = tm
            q = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": names, "STYLES": styles, "FORMAT": fmt, "TRANSPARENT": "true",
                 "CRS": "EPSG:3857", "WIDTH": "256", "HEIGHT": "256"}
            if tm:
                q["TIME"] = tm
            sep = "&" if "?" in ep else "?"
            d.update({"wmsBase": ep, "wms": params, "url": ep + sep + urllib.parse.urlencode(q) + "&BBOX={bbox-epsg-3857}"})
            if src.get("popup", True) is not False and controls.get("popups", True):
                iq = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo", "LAYERS": names, "QUERY_LAYERS": names, "STYLES": styles,
                      "INFO_FORMAT": "application/json", "FEATURE_COUNT": "5", "CRS": "EPSG:3857", "WIDTH": "101", "HEIGHT": "101", "I": "50", "J": "50"}
                d["info"] = ep + sep + urllib.parse.urlencode(iq) + "&BBOX={bbox}"
                d["needs_cors"] = True                   # GetFeatureInfo is read with fetch(); the picture tiles are not
            leg = src.get("legend")
            if leg is not False:
                uri, where = _legend_picture(leg if isinstance(leg, str) else None, base_dir, names)
                if uri:
                    out["blocks"].append({"kind": "image", "layer": lid, "title": title, "items": [{"href": uri}], "note": f"Legend from {host}"})
                else:
                    lq = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetLegendGraphic", "LAYER": names.split(",")[0], "FORMAT": "image/png"}
                    out["blocks"].append({"kind": "image", "layer": lid, "title": title, "items": [{"href": ep + sep + urllib.parse.urlencode(lq)}],
                                          "note": f"Legend drawn by {host} when the page opens", "live": True})
                    d["legend_live"] = True
        elif proto in ("wmts", "xyz", "mvt"):
            tpl = _tile_template(str(src.get("url_template") or ""))
            if not all(s in tpl for s in ("{z}", "{x}", "{y}")):
                refuse("url_template holds {z}, {x} and {y} (or a WMTS {TileMatrix}, {TileRow}, {TileCol})")
                continue
            if not _on_host(tpl, host):
                refuse("the tile address is not https on the profile's host; save a profile for that host first", "MAP-SVC-001")
                continue
            d["url"] = tpl
            if proto == "mvt":
                sl, geom = src.get("source_layer"), src.get("geometry")
                if not sl or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(sl)) or geom not in GEOMETRIES:
                    refuse("a live vector tile layer names its source_layer (ogc_tile_probe lists them) and its geometry (polygon, line or point)")
                    continue
                d.update({"source_layer": sl, "geometry": geom, "style": _style(tokens, geom), "maxzoom": int(src.get("max_zoom", 14) or 14)})
                d["popup"] = _popup(src)
                out["blocks"].append(_swatch_block(lid, title, geom, d["style"]))
        else:                                                                   # wfs
            tn, geom = src.get("layer"), src.get("geometry")
            if not tn or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(tn)) or geom not in GEOMETRIES:
                refuse("a live WFS layer names its feature type (source.layer) and its geometry (polygon, line or point)")
                continue
            ep = oc.endpoint(prof, "wfs")
            if not _on_host(ep, host):
                refuse("the WFS address is not https on the profile's host", "MAP-SVC-001")
                continue
            n = max(1, min(int(src.get("max_features") or WFS_DEFAULT_MAX), WFS_CEILING))
            q = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": tn, "outputFormat": "application/json", "srsName": "EPSG:4326", "count": str(n)}
            try:
                conds = oc.parse_conditions(src.get("conditions"))
            except oc.Refusal as e:
                out["findings"].append(finding("MAP-SVC-038", "BLOCK", f"live layer '{lid}': {e}", layer=lid))
                continue
            if conds:
                q["filter"] = oc.filter_xml(conds, "2.0.0")
            sep = "&" if "?" in ep else "?"
            d.update({"url": ep + sep + urllib.parse.urlencode(q) + "&bbox={bbox}", "geometry": geom, "style": _style(tokens, geom), "max_features": n})
            d["popup"] = _popup(src)
            out["blocks"].append(_swatch_block(lid, title, geom, d["style"]))
        if d["needs_cors"] and not d["cors_checked"]:
            out["findings"].append(finding("MAP-WEB-005", "WARN", f"live layer '{lid}': the browser reads this service with fetch(), which works only if it allows "
                                           f"requests from web pages (CORS). Check it with ogc_cors_check and record the day as source.cors_checked", layer=lid))
        out["findings"].append(finding("MAP-SVC-014", "WARN", f"live layer '{lid}': not reproducible: the page shows whatever {host} answers when it is opened", layer=lid))
        out["notes"].append(f"live layer '{lid}': drawn from {host} ({proto.upper()}) when the page is opened; the static picture is drawn without it")
        out["credits"].append(d["credit"])
        if host not in [h["host"] for h in out["hosts"]]:
            out["hosts"].append({"host": host, "purpose": f"live {proto.upper()} layer ({title})", "service": prof.get("profile"), "protocol": proto})
        out["defs"].append(d)
    return out


def _style(t: dict, geom: str) -> dict:
    """The brand's emphasis colour for a live vector layer: the service's own styling is never used (colours come from the brand)."""
    fill = t.get("emphasis") or t.get("frame")
    return {"fill": fill, "fillOpacity": 0.35 if geom == "polygon" else 0.85, "stroke": fill if geom != "point" else t.get("background") or t.get("land"),
            "weight": 1.5 if geom != "point" else 1, "radius": 4, "opacity": 1, "dash": None}


def _popup(src: dict) -> dict | None:
    if src.get("popup", True) is False:
        return None
    fields = [str(f) for f in (src.get("popup_fields") or []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", str(f))][:12]
    title = src.get("popup_title") if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", str(src.get("popup_title") or "")) else None
    return {"title": title, "fields": fields} if fields or title else None


def _swatch_block(lid: str, title: str, geom: str, s: dict) -> dict:
    item = {"label": title, "fill": s["fill"], "stroke": s["stroke"]}
    kind = {"polygon": "polygons", "line": "lines", "point": "points"}[geom]
    return {"kind": kind, "layer": lid, "title": None, "items": [item]}


def hosts_for(defs: list[dict], engine: str) -> tuple[list[str], list[str]]:
    """(img-src hosts, connect-src hosts) the live layers need on this engine. Leaflet loads WMS and tile pictures as images; MapLibre fetches
    every tile; GetFeatureInfo, WFS and vector tiles are fetched on both; a legend loaded live is an image."""
    img, conn = [], []
    for d in defs:
        h = d["host"]
        if d["raster"]:
            (img if engine == "leaflet" else conn).append(h)
        else:
            conn.append(h)
        if d.get("info"):
            conn.append(h)
        if d.get("legend_live"):
            img.append(h)
    return list(dict.fromkeys(img)), list(dict.fromkeys(conn))


def bbox_text(mb: list[float]) -> str:
    """The page's maxBounds as a WFS 2.0 bbox in CRS84 (longitude first, whatever the server's axis order for EPSG:4326)."""
    return ",".join(f"{v:.5f}" for v in mb) + ",urn:ogc:def:crs:OGC:1.3:CRS84"
