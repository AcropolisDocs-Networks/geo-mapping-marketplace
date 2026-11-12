#!/usr/bin/env python3
"""tile_registry: the tile-provider-registry (spec 7.2). Reads the library's `basemap-provider` entries and says whether a web map page
may use a provider, with the URL template, hosts, attribution and zoom limits it needs.

    tile_registry.py list [--audience external]
    tile_registry.py resolve --provider carto-positron [--style light_all] [--audience external]

A request is refused, never bent, when the entry says so: blocked entries (Google, Bing), draft entries whose terms are unread, a style the
provider does not list. A keyless provider that is not a default (OpenStreetMap's own servers) is a customization: it needs the person's one
acknowledgment (customizations.py, made once with map-services), never a switch in the MapSpec; a keyed provider needs only the person's key. A missing key or an engine this release
does not have makes the page fall back to the bundled basemap, and says so.

Keys (0.6.0, credentials spec §9.4). A provider that needs a key declares `delivery.requires_credential`; the key is the person's own registered
credential `cred:<provider>` (map-credentials add maptiler), never from chat, never printed by this script. Two classes:
  secret   (the default) never leaves the host-side request: a print basemap gets the key added at send time (the tile address carries none), and a
           web page that would need it is refused (MAP-CRED-011 BLOCK) with the alternatives.
  browser  a key the provider designs to be public, restricted by the person to their domain: it may be written into a page (MAP-WEB-003 WARN, as
           before); without the recorded restriction the page also warns MAP-CRED-009.
`GEO_MAPPING_KEY_<PROVIDER>` (for example GEO_MAPPING_KEY_CARTO_POSITRON) is still read as a fallback for the command line only: the desktop app's
processes do not see variables the person sets (H.3 D3). A key from it is treated as a browser key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import customizations as cz  # noqa: E402
import gmap_common as gc  # noqa: E402

PROVIDER_IDS = {
    "carto-positron": "ref.basemap-provider.carto-positron", "osm": "ref.basemap-provider.osm-standard",
    "pmtiles": "ref.basemap-provider.pmtiles-user", "protomaps": "ref.basemap-provider.protomaps-api", "esri": "ref.basemap-provider.esri-arcgis",
    "maptiler": "ref.basemap-provider.maptiler", "mapbox": "ref.basemap-provider.mapbox", "usgs": "ref.basemap-provider.usgs-topo",
    "google-maps": "ref.basemap-provider.google-maps", "bing-maps": "ref.basemap-provider.bing-maps", "nasa-gibs": "ref.basemap-provider.nasa-gibs",
}
DAILY_LAG_DAYS = 2                    # 0.16.0: a daily imagery layer (NASA GIBS) with no day named shows the day before yesterday: today's mosaic is partial
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def imagery_day(asked, today=None) -> str:
    """The day a daily imagery layer shows: the one asked for (YYYY-MM-DD, not in the future), else DAILY_LAG_DAYS before today."""
    import datetime as dt
    fixed = os.environ.get("GMAP_FIXED_DATE")
    today = today or (dt.date.fromisoformat(fixed) if fixed else dt.date.today())
    if asked:
        d = dt.date.fromisoformat(str(asked))
        if d > today:
            raise ValueError(f"basemap.time {asked} is in the future")
        return d.isoformat()
    return (today - dt.timedelta(days=DAILY_LAG_DAYS)).isoformat()
PUBLIC_AUDIENCES = ("any", "external", "regulatory")      # who may open the page: an unstated audience is treated as public
MAX_ZOOM_CAP = 18                                         # a web page never offers more than this, whatever the provider allows
# zoom_offset: 512-pixel tiles numbered the Mapbox/MapTiler way (a zoom-z tile covers what four zoom-z 256 tiles do) are drawn one zoom
# lower than the page's zoom (-1, the default for tile_size 512). A provider whose 512 tiles are @2x copies of its 256 grid says zoom_offset: 0.


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def key_env(entry_id: str) -> str:
    return "GEO_MAPPING_KEY_" + entry_id.rsplit(".", 1)[-1].upper().replace("-", "_")


def entry_for(provider: str, entries: list[dict]) -> dict | None:
    full = PROVIDER_IDS.get(provider) or (provider if provider.startswith("ref.basemap-provider.") else None)
    hits = [e for e in entries if e.get("id") == full]
    if not hits:
        return None
    return next((e for e in hits if e.get("_scope") == "local"), hits[0])


def hosts_of(template: str) -> list[str]:
    u = urllib.parse.urlparse(re.sub(r"\{[^}]*\}", "x", template))
    return [u.hostname] if u.hostname else []


def credential_block(e: dict) -> dict | None:
    """The entry's requires_credential, or a minimal one for a local overlay entry that still says requires_key (pre-0.6.0)."""
    import credentials as cr
    b = cr.entry_block(e)
    if b:
        return b
    pr = e.get("provider") or {}
    if pr.get("requires_key") or (e.get("delivery") or {}).get("requires_key") is True:
        return {"name": PROVIDER_NAMES.get(e.get("id"), e.get("id", "").rsplit(".", 1)[-1]), "kind": "apikey", "class": "secret", "inject": {"query": "key"}, "legacy": True}
    return None


PROVIDER_NAMES = {v: k for k, v in PROVIDER_IDS.items()}


def strip_key(template: str) -> str:
    """The tile address without its key parameter (a print basemap gets the key added at send time)."""
    base, sep, q = template.partition("?")
    if not sep:
        return template
    keep = [s for s in q.split("&") if s and "{key}" not in s]
    return base + ("?" + "&".join(keep) if keep else "")


def resolve(provider: str, entries: list[dict], *, style: str | None = None, audience: str = "external", env=None, engine: str = "leaflet",
            terms_acknowledged: bool = False, source: str | None = None, base_dir=None, purpose: str = "web", creds=None, time: str | None = None) -> dict:
    """Returns {ok, mode: 'tiles'|'vector'|'vector-file'|'bundled', findings, ...}. mode 'bundled' with ok True means a fallback was made; ok
    False means refused. A keyless provider that is not a default (OpenStreetMap's light-use servers) needs the person's one acknowledgment
    (customizations.acknowledged); terms_acknowledged is an older MapSpec's basemap.terms_acknowledged, which still counts for that map. It lifts
    nothing else: blocked and draft entries, missing keys and the tile caps are unchanged.
    `source` is the MapSpec's basemap.source: `pmtiles:FILE` names the person's own vector basemap file (M14, read by the MapLibre page).
    purpose is 'web' (the key may be written into the page: browser class only) or 'print' (tiles fetched here, the key added at send time and
    returned as `auth`, never in `url`). creds is the credential store (default: the person's own folder)."""
    env = os.environ if env is None else env
    out = {"provider": provider, "ok": True, "mode": "bundled", "findings": [], "hosts": [], "attribution": None}
    if provider in ("bundled", "none", None):
        out["mode"] = "bundled" if provider != "none" else "none"
        return out
    e = entry_for(provider, entries)
    if e is None:
        out.update(ok=False)
        out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"basemap provider '{provider}' is not in the library; the choices are {', '.join(sorted(PROVIDER_IDS))}, bundled or none"))
        return out
    out["entry"] = e["id"]
    out["rev"] = e.get("rev")
    st = e.get("status")
    if st == "blocked":
        out.update(ok=False)
        out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"{e.get('title')} cannot be used: {(e.get('blocked') or {}).get('why', 'blocked by policy')}", e["id"]))
        return out
    if st != "active":
        out.update(ok=False)
        out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"{e.get('title')} is {st}: its terms have not been read, so a page cannot use it yet. Use carto-positron with your own key, a PMTiles file, or the bundled basemap", e["id"]))
        return out
    pr = e.get("provider") or {}
    # 0.23.0: one acknowledgment, not a terms switch per map. A keyless provider that is not a default (OpenStreetMap's own servers) is a
    # customization: it needs the person's one acknowledgment (customizations.py, map-services). A keyed provider needs nothing more: the
    # person accepted its terms to get the key. An older MapSpec's basemap.terms_acknowledged still counts for that map.
    keyed = bool(credential_block(e))
    ack = cz.acknowledged(terms_acknowledged)
    referer = "; a page opened from a file sends no Referer, so the server may refuse the tiles: open it from a web server" if pr.get("referer_required") else ""
    if pr.get("production_ok") is False and not keyed:
        if not ack:
            out.update(ok=False)
            out["findings"].append(finding("MAP-WEB-011", "BLOCK", cz.needed(f"{e.get('title')} (a light-use server)", "use the bundled background, "
                                           "carto-positron with your own key, or a PMTiles file"), e["id"]))
            return out
        out["terms_acknowledged"] = True
        out["customization"] = cz.how(terms_acknowledged)
        out["findings"].append(finding("MAP-WEB-011", "WARN" if audience in PUBLIC_AUDIENCES else "INFO",
                                       f"{e.get('title')} allows light use only, and the plugin keeps to it: a few tiles for a printed map, fetched once and cached, "
                                       "the credit shown on the map, no bulk download" + (f". The audience of this map is {audience}: a busy page or a product needs "
                                       "carto-positron with your own key or your own tiles" if audience in PUBLIC_AUDIENCES else ", and the page is for your own team")
                                       + referer, e["id"]))
    if pr.get("commercial_ok") is False:
        if not keyed and not ack:
            out.update(ok=False)
            out["findings"].append(finding("MAP-WEB-011", "BLOCK", cz.needed(f"{e.get('title')} (free for non-commercial use)", "use your own PMTiles basemap file or the bundled background"), e["id"]))
            return out
        if not keyed:
            out["customization"] = cz.how(terms_acknowledged)
        out["findings"].append(finding("MAP-WEB-011", "INFO", f"{e.get('title')}'s free plan is for non-commercial use; a client deliverable needs the provider's paid plan. "
                                       "Your plan with them decides, and the key is yours", e["id"]))
    mode = (e.get("delivery") or {}).get("mode")
    tpl = e.get("url_template")
    if mode == "user-supplied" or not tpl:
        src = str(source or "")
        if src.startswith("pmtiles:"):                   # the person's own vector basemap file (M14): only the MapLibre page reads it
            from pathlib import Path as _P
            f = _P(src.split(":", 1)[1]).expanduser()
            if not f.is_absolute() and base_dir is not None:
                f = _P(base_dir) / f
            if not f.is_file():
                out.update(ok=False)
                out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"the basemap file {f.name} was not found (basemap.source: {src})", str(f)))
                return out
            try:
                with open(f, "rb") as fh:
                    head = fh.read(127)
            except OSError:
                head = b""
            if head[:7] != b"PMTiles" or len(head) < 100 or head[7] != 3 or head[99] != 1:
                out.update(ok=False)
                out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"{f.name} is not a PMTiles v3 file of vector tiles (MVT); the page reads that kind only", str(f)))
                return out
            out.update(mode="vector-file", file=str(f), hosts=[], title=f"{e.get('title')}: {f.name}", attribution=pr.get("attribution_html") or (e.get("license") or {}).get("attribution_text"),
                       attribution_text=(e.get("license") or {}).get("attribution_text"), max_zoom=int(head[101]) if len(head) > 101 else 14,
                       license=(e.get("license") or {}).get("class"))
            out["findings"].append(finding("MAP-WEB-011", "INFO", f"basemap {f.name}: {(e.get('license') or {}).get('note') or 'check the file licence before sharing the page'}", e["id"]))
            return out
        out["findings"].append(finding("MAP-WEB-015", "WARN", f"{e.get('title')} needs your own tile file: name it with basemap.source: pmtiles:FILE (a vector PMTiles file; "
                                       "the MapLibre page reads it); the bundled basemap is used", e["id"]))
        return out
    styles = pr.get("styles") or []
    style = style or (styles[0] if styles else None)
    if styles and style not in styles:
        out.update(ok=False)
        out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"style '{style}' is not one of {', '.join(styles)} for {e.get('title')}", style))
        return out
    key = None
    auth = None
    block = credential_block(e)
    if block:
        import credentials as cr
        cname = block.get("name")
        store = creds if creds is not None else cr.Store()
        registered = _safe(lambda: store.get(cname))
        if registered:
            try:
                res = cr.resolve(f"cred:{cname}", store=store, host=(hosts_of(tpl) or [None])[0], block=block, purpose="page" if purpose == "web" else "request")
            except cr.CredentialError as ex:
                out.update(ok=False)
                out["findings"] += [{**f, "detail": f.get("detail") or e["id"]} for f in ex.findings]
                if ex.rule == "MAP-CRED-011":
                    out["findings"].append(finding("MAP-WEB-015", "INFO", f"for a web page use the bundled basemap, a PMTiles file, or a {e.get('title')} key made for browsers "
                                                   f"(restricted to your domain), registered with map-credentials add {cname} --class browser", e["id"]))
                return out
            out["findings"] += res.findings
            out["credential"] = res.record() | {"class": res.cls}
            if purpose == "web":
                key = res.page_value()
                out["findings"].append(finding("MAP-WEB-003", "WARN", f"the page carries your {e.get('title')} browser key in its tile address, so anyone with the page can read it. "
                                               "Keep it restricted to the page's domain in the provider's dashboard", e["id"]))
            else:
                auth = res
        else:
            name = key_env(e["id"])
            key = (env.get(name) or "").strip() or None
            if not key:
                out["findings"].append(finding("MAP-WEB-015", "WARN", f"{e.get('title')} needs a key and none is registered, so the bundled basemap is used. Register your own: "
                                               f"/{cr._prefix()}:map-credentials add {cname}" + (" --class browser (a key the provider makes for web pages, restricted to your domain)" if purpose == "web" else "")
                                               + f". On the command line only, {name} in your environment is also read. Never paste a key into the chat", name))
                return out
            out["credential"] = {"credential": None, "from": "environment (command-line fallback)", "variable": name}
            if purpose == "web":
                out["findings"].append(finding("MAP-WEB-003", "WARN", f"the page carries your {e.get('title')} key (from {name}) in its tile address, so anyone with the page can read it. "
                                               f"Restrict the key to the page's domain in the provider's dashboard, and register it with map-credentials add {cname} --class browser", e["id"]))
    url = tpl.replace("{style}", style or "")
    sp = (pr.get("style_paths") or {}).get(style) or {}
    style_max = sp.get("max_zoom")
    if "{time}" in url:                                  # 0.16.0: a dated imagery layer (NASA GIBS)
        if time is not None and not DATE_RE.match(str(time)):
            out.update(ok=False)
            out["findings"].append(finding("MAP-WEB-011", "BLOCK", f"basemap.time is a day as YYYY-MM-DD, not '{str(time)[:30]}'", e["id"]))
            return out
        try:
            day = imagery_day(time) if sp.get("daily") else (time or "default")
        except ValueError as ex:
            out.update(ok=False)
            out["findings"].append(finding("MAP-WEB-011", "BLOCK", str(ex), e["id"]))
            return out
        url = url.replace("{time}", day)
        out["time"] = day
        if sp.get("daily"):
            out["findings"].append(finding("MAP-WEB-011", "INFO", f"{e.get('title')}: {style} for {day}" + ("" if time else
                                           f" ({DAILY_LAG_DAYS} days ago: today's mosaic is still being built; name another day with basemap.time)"), e["id"]))
    url = url.replace("{matrix}", str(sp.get("matrix", ""))).replace("{ext}", str(sp.get("ext", "png")))
    if auth is not None:
        url = strip_key(url)
        out["auth"] = auth
    elif key is not None:
        url = url.replace("{key}", urllib.parse.quote(key, safe=""))
    out.update(mode="vector" if pr.get("tile_format") == "mvt" else "tiles", url=url, hosts=hosts_of(tpl), style=style, key_used=bool(key),
               attribution=pr.get("attribution_html") or (e.get("license") or {}).get("attribution_text"),
               attribution_text=(e.get("license") or {}).get("attribution_text"), max_zoom=min(int(style_max or pr.get("max_zoom", MAX_ZOOM_CAP)), MAX_ZOOM_CAP),
               min_zoom=max(0, int(pr.get("min_zoom", 0))), tile_size=int(pr.get("tile_size", 256)),
               zoom_offset=int(pr.get("zoom_offset", -1 if int(pr.get("tile_size", 256)) == 512 else 0)), title=e.get("title"), license=(e.get("license") or {}).get("class"),
               redistribute_ok=(e.get("license") or {}).get("redistribute_ok"), wordmark=bool(pr.get("wordmark_required")))
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - an unreadable credential folder is the same as none registered
        return None


def public_view(r: dict) -> dict:
    """What may be printed: never the key or the address that carries it."""
    v = {k: val for k, val in r.items() if k not in ("url", "entry_obj", "auth")}
    if r.get("mode") in ("tiles", "vector") and r.get("url"):        # a vector basemap (Protomaps API) carries its key the same way
        v["url"] = re.sub(r"([?&](?:key|token|apikey|api_key|access_token)=)[^&]*", r"\1***", r["url"], flags=re.I)
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["list", "resolve"])
    ap.add_argument("--provider")
    ap.add_argument("--style")
    ap.add_argument("--audience", default="external", choices=["any", "internal", "external", "regulatory"])
    ap.add_argument("--terms-acknowledged", action="store_true", help="an older MapSpec's basemap.terms_acknowledged; the person's one acknowledgment (map-services) is read by itself")
    a = ap.parse_args(argv)
    import ref_library as rl
    entries = [e for e in rl.all_entries("all") if e.get("type") == "basemap-provider"]
    if a.op == "list":
        rows = []
        for name in sorted(PROVIDER_IDS):
            r = resolve(name, entries, audience=a.audience, env={})
            e = entry_for(name, entries)
            cb = credential_block(e) if e else None
            rows.append({"provider": name, "status": (e or {}).get("status"), "usable": r["ok"] and r["mode"] in ("tiles", "vector"), "needs_key": bool(cb),
                         "credential": f"cred:{cb['name']}" if cb else None, "key_classes": (cb or {}).get("class_allowed") or ([cb.get("class", "secret")] if cb else None),
                         "key_variable": key_env(e["id"]) if cb else None,
                         "why_not": next((f["message"] for f in r["findings"] if f["severity"] == "BLOCK"), None) or ("needs a key" if r["mode"] == "bundled" and r["findings"] and r["findings"][0]["id"] == "MAP-WEB-015" else None)})
        print(json.dumps(rows, indent=2))
        return gc.EXIT_OK
    if not a.provider:
        print("tile_registry: resolve needs --provider", file=sys.stderr)
        return gc.EXIT_USAGE
    r = resolve(a.provider, entries, style=a.style, audience=a.audience, terms_acknowledged=a.terms_acknowledged)
    print(json.dumps(public_view(r), indent=2))
    return gc.EXIT_OK if r["ok"] else gc.EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
