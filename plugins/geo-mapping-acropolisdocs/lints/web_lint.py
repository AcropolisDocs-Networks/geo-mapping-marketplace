"""Web map lint, MAP-WEB-* (spec 12.7c). Runs on the finished page text, so it also checks a page somebody edited.

    lint_page(text, report, plan)    report is the page's own report (WebPage.report); plan supplies the brand and layers

Rule summary (BLOCK unless noted):
  001 one self-contained file; scripts and styles inlined or loaded with SRI from an allowed CDN
  002 every external host is in the page's host manifest; no analytics, no beacons
  003 no secrets in the page (basic auth, bearer tokens); a provider key in a tile address is a WARN with a reminder
  004 the attribution control is present and not hidden        005 no http: address in an https page (mixed content)
  006 error banners, a static fallback image and a start-up guard (WARN)
  007 payload size and feature count (WARN)                     008 accessibility basics (WARN)
  009 usable at 360 px; touch targets (WARN)                    010 zoom and bounds set within the provider's limit (WARN)
  011 only active, allowed tile providers; the page's tile hosts match the library
  012 brand: Inter embedded, palette, stamps, hidden accreditation, no visible house credit
  013 client-confidential layers: noindex, banner, no artifact publish without confirmation, precision note
  014 print stylesheet (INFO)                                   016 place names carried with a tier and a zoom, and a toggle (WARN)
M14 (both engines unless noted; every check reads the page text back, not the builder's report):
  017 the Content-Security-Policy allows only what the page's engine needs: Leaflet no workers; MapLibre `worker-src blob:` and
      `child-src blob:` only; scripts inline only, never eval; network only to the manifest's hosts (BLOCK)
  018 every PMTiles archive embedded in the page opens, holds the layers the page draws from it, and every feature of the source is in it
      at the layer's top zoom; a baked cluster level counts every point once (BLOCK) (MapLibre)
  019 the embedded glyphs cover every character the page's labels draw (WARN) (MapLibre)
  020 page size and feature caps: over 16 MB WARN (too big to publish as an artifact, slow on a phone), over 60 MB BLOCK; a MapLibre page
      drawing over 100,000 features as GeoJSON WARN (they belong in tiles)
  021 an attribution for every source: each basemap source carries its provider's credit, the bundled background its own, and every data
      layer's credit is in the source note (BLOCK for a basemap, WARN otherwise)
  022 the time slider and the swipe compare name layers the page has, and the time layers' features carry their period (BLOCK / WARN)
M15 (3D and story maps):
  023 the terrain embedded in a 3D page reads back: the block matches its hash, every tile is a 512-pixel Terrarium PNG with elevations in
      the stated range, the style draws it from the page (BLOCK); the resolution the page got (INFO); terrain asked for with no DEM, or
      capped below the DEM's own detail (WARN, at build time)
  024 extrusion and mast heights are numbers, not negative and not over 1,000 m (WARN)
  025 3D asked for on a Leaflet page, which is flat and north up (WARN); a story's pitch and bearing on Leaflet (INFO)
  026 a story's chapters name layers the page has and a view (BLOCK), a period of the slider and a view with some of their features in it
      (WARN); the column and the print view carry every chapter; the KPI panel's fields are carried by a layer (WARN)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import brand_lint as bl  # noqa: E402

ALLOWED_CDNS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com")
ANALYTICS = re.compile(r"google-analytics|googletagmanager|gtag\(|\bga\(|segment\.(?:com|io)|hotjar|mixpanel|plausible\.io|matomo|sentry\.io|fullstory|clarity\.ms|sendBeacon|doubleclick\.net", re.I)
SECRET = re.compile(r"(authorization\s*[:=]|bearer\s+[A-Za-z0-9._\-]{12,}|basic\s+[A-Za-z0-9+/=]{12,}|://[^/\s:@\"']+:[^/\s@\"']+@|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[\"'][^\"']{6,})", re.I)
KEY_PARAM = re.compile(r"[?&](key|apikey|api_key|token|access_token)=", re.I)
NS_OK = ("www.w3.org", "www.openstreetmap.org", "carto.com", "leafletjs.com", "operations.osmfoundation.org")
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_FEATURES = 20000
OVERZOOM_LEVELS = 2               # the page enlarges a provider's last tiles up to this many zooms (webmap.OVERZOOM_LEVELS)
SIZE_NOTE_BYTES = 1024 * 1024


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def strip_vendor(text: str) -> str:
    """The page without the inlined library blocks (their own colors and strings are not ours to judge)."""
    return re.sub(r"<!-- gmap:vendor:begin.*?<!-- gmap:vendor:end -->", " ", text, flags=re.S)


def without_data(text: str) -> str:
    t = re.sub(r'(<script type="application/json" id="gmap-(?:data|glyphs)">).*?(</script>)', r"\1\2", text, flags=re.S)
    return re.sub(r'(<script type="application/octet-stream"[^>]*>).*?(</script>)', r"\1\2", t, flags=re.S)


def json_block(text: str, el_id: str):
    m = re.search(rf'<script type="application/json" id="{re.escape(el_id)}">(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1).replace("<\\/", "</"))
    except ValueError:
        return None


def without_blobs(text: str) -> str:
    return re.sub(r"data:[a-z/+.\-]+;base64,[A-Za-z0-9+/=]+", "data:blob", text)


def external_hosts(text: str) -> set[str]:
    """Hosts the page's own code or markup can call: everything except namespaces and plain <a href> links the person clicks."""
    t = without_blobs(without_data(strip_vendor(text)))
    t = re.sub(r"<a\b[^>]*>", " ", t, flags=re.I)                                  # user-clicked links (attribution) are navigation, not calls
    t = re.sub(r'"link":\s*"https?://[^"]*"', " ", t)                                  # 0.16.0: a provider logo's link in the config: navigation, not a call
    t = re.sub(r'"href":\s*"https?://[^"]*"', " ", t)                                  # 1.1.0: a credit's links in the config (the corner draws them as <a>)
    t = re.sub(r'<script type="application/json" id="gmap-host-manifest">.*?</script>', " ", t, flags=re.S)
    t = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', " ", t)
    t = re.sub(r'<meta name="[^"]*" content="[^"]*">', " ", t)
    t = re.sub(r"<svg\b[^>]*xmlns=\"http://www\.w3\.org/2000/svg\"", "<svg", t)
    t = re.sub(r"https?://www\.w3\.org/[\w/.\-]*", " ", t)
    found = set()
    for m in re.finditer(r"(?:https?:)?//([A-Za-z0-9\-.{}]+\.[A-Za-z]{2,})(?::\d+)?[/?\"'\s)]", t):
        found.add(re.sub(r"\{[^}]*\}", "x", m.group(1)).lower())
    return found


def manifest_of(text: str) -> dict | None:
    m = re.search(r'<script type="application/json" id="gmap-host-manifest">(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1).replace("<\\/", "</"))
    except Exception:  # noqa: BLE001
        return None


def lint_page(text: str, report: dict, plan, providers: list[dict] | None = None) -> list[dict]:
    out: list[dict] = []
    own = strip_vendor(text)
    man = manifest_of(text)
    hosts_declared = {h["host"].lower() for h in (man or {}).get("hosts", [])}

    # 001 self-contained
    ext_scripts = re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", text, re.I)
    ext_links = re.findall(r"<link\b[^>]*\brel\s*=\s*[\"']stylesheet[\"'][^>]*\bhref\s*=\s*[\"']([^\"']+)", text, re.I) + re.findall(r"@import\s+(?:url\()?[\"']?([^\"')\s;]+)", text)
    for u in ext_scripts + ext_links:
        host = urllib.parse.urlparse(u).hostname or ""
        tag_ok = re.search(r"integrity\s*=\s*[\"']sha(?:256|384|512)-", text) is not None
        if not u.startswith("data:") and not (host in ALLOWED_CDNS and tag_ok):
            out.append(finding("MAP-WEB-001", "BLOCK", f"the page loads {u[:80]} from outside the file; inline it, or load it from an allowed CDN with an integrity hash", u[:120]))
    if man is None:
        out.append(finding("MAP-WEB-002", "BLOCK", "the page has no host manifest (gmap-host-manifest)"))

    # 002 hosts and analytics
    undeclared = sorted(h for h in external_hosts(text) if h not in hosts_declared and not any(h == n or h.endswith("." + n) for n in NS_OK))
    if undeclared:
        out.append(finding("MAP-WEB-002", "BLOCK", f"the page can call hosts that are not in its host manifest: {', '.join(undeclared)}", undeclared))
    if ANALYTICS.search(without_blobs(without_data(own))):
        out.append(finding("MAP-WEB-002", "BLOCK", "the page contains an analytics or beacon call; map pages never report who looks at them"))
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', text)
    if not csp:
        out.append(finding("MAP-WEB-002", "WARN", "the page has no Content-Security-Policy, so nothing stops a call to an undeclared host"))
    else:
        c = csp.group(1)
        if "connect-src 'none'" not in c and "connect-src" not in c:
            out.append(finding("MAP-WEB-002", "WARN", "the Content-Security-Policy does not restrict connect-src"))

    # 003 secrets
    body = without_blobs(without_data(own))
    if SECRET.search(body):
        out.append(finding("MAP-WEB-003", "BLOCK", "the page appears to hold a credential (basic auth, bearer token or password); credentials are never embedded"))
    if KEY_PARAM.search(body):
        out.append(finding("MAP-WEB-003", "WARN", "a provider key is in the page; restrict it to the page's domain in the provider's dashboard"))

    # 004 attribution
    engine = "maplibre" if re.search(r'<html[^>]*\bdata-engine="maplibre"', text) else "leaflet"
    # 1.1.0: the frame's credits corner (#gm-credits, frame.js) is the attribution control on both engines
    if 'id="gm-credits"' not in text or "renderCredits" not in own:
        out.append(finding("MAP-WEB-004", "BLOCK", "the page has no attribution control (the frame's credits corner)"))
    if re.search(r"\.(?:leaflet-control-attribution|maplibregl-ctrl-attrib)[^{]*\{[^}]*display\s*:\s*none", own):
        out.append(finding("MAP-WEB-004", "BLOCK", "the attribution control is hidden by style; provider terms require it to stay visible"))
    cfg_basemaps = {}
    cm = re.search(r'<script type="application/json" id="gmap-config">(.*?)</script>', text, re.S)
    try:
        cfg_basemaps = {b["id"]: b for b in json.loads(cm.group(1).replace("<\\/", "</")).get("basemaps", [])} if cm else {}
    except Exception:  # noqa: BLE001
        cfg_basemaps = {}
    for b in (providers or []):
        have = (cfg_basemaps.get(b.get("id")) or {}).get("attribution")
        if not have or have != b.get("attribution"):
            out.append(finding("MAP-WEB-004", "BLOCK", f"the attribution text of {b.get('id')} is missing from the page", b.get("attribution")))
    out += check_frame(text, own, report, providers)

    # 005 mixed content
    insecure = sorted(set(re.findall(r"[\"'(]\s*(http://(?!www\.w3\.org)[^\"')\s]+)", without_blobs(without_data(own)))))
    if insecure:
        out.append(finding("MAP-WEB-005", "BLOCK", f"an http: address in the page would be blocked as mixed content on an https page: {insecure[0][:80]}", insecure[:3]))

    # 006 degradation
    if 'id="gm-banners"' not in text or "error_banners\":false" in text.replace(" ", ""):
        if report.get("controls", {}).get("error_banners", True):
            out.append(finding("MAP-WEB-006", "WARN", "the page has no area for per-layer error banners"))
    if 'id="gm-fallback"' not in text and "<noscript>" not in text:
        out.append(finding("MAP-WEB-006", "WARN", "the page has no static fallback image; a viewer whose browser cannot run the map sees nothing"))
    if "try {" not in text and "try{" not in text:
        out.append(finding("MAP-WEB-006", "WARN", "the page has no start-up guard; one failure would leave a blank page"))

    # 007 performance (Leaflet: every feature is drawn from GeoJSON; a MapLibre page's tiled layers are judged by 018 and 020 instead)
    if engine == "maplibre":
        pass
    elif report.get("data_bytes", 0) > MAX_PAYLOAD_BYTES or report.get("features", 0) > MAX_FEATURES:
        out.append(finding("MAP-WEB-007", "WARN", f"the embedded data is {report.get('data_bytes', 0) / 1048576:.1f} MB and {report.get('features', 0):,} features; larger data belongs in vector tiles or PMTiles, "
                           "or should be simplified or clustered", {"bytes": report.get("data_bytes"), "features": report.get("features")}))
    elif report.get("data_bytes", 0) > SIZE_NOTE_BYTES:
        out.append(finding("MAP-WEB-007", "INFO", f"the embedded data is {report['data_bytes'] / 1048576:.1f} MB; the page loads in a second or two on a normal connection"))

    # 008 accessibility
    miss = []
    if not re.search(r"<html[^>]*\blang=", text):
        miss.append("html lang")
    if 'role="application"' not in text or "aria-label=" not in text:
        miss.append("map role and label")
    if 'id="gm-summary"' not in text:
        miss.append("text summary")
    if "prefers-reduced-motion" not in text:
        miss.append("prefers-reduced-motion")
    buttons = re.findall(r"<button\b([^>]*)>([^<]*)</button>", text)
    if any(not (re.search(r"aria-label=", a) or t.strip()) for a, t in buttons):
        miss.append("button without a name")
    if miss:
        out.append(finding("MAP-WEB-008", "WARN", f"accessibility: missing {', '.join(miss)}", miss))
    if report.get("contrast") is not None and report["contrast"] < 4.5:
        out.append(finding("MAP-WEB-008", "WARN", f"the text on the panels has a contrast of {report['contrast']}:1; 4.5:1 is the minimum", report["contrast"]))

    # 009 small screens
    if 'name="viewport"' not in text or not re.search(r"max-width:6(?:39|40)px", text.replace(" ", "")):
        out.append(finding("MAP-WEB-009", "WARN", "the page has no viewport meta or small-screen layout, so it is not usable at 360 px wide"))
    if "pointer:coarse" not in text.replace(" ", ""):
        out.append(finding("MAP-WEB-009", "WARN", "touch targets are not enlarged for touch screens"))

    # 010 bounds and zoom
    cfgm = re.search(r'<script type="application/json" id="gmap-config">(.*?)</script>', text, re.S)
    try:
        cfg = json.loads(cfgm.group(1).replace("<\\/", "</")) if cfgm else {}
    except Exception:  # noqa: BLE001
        cfg = {}
    if not cfg.get("maxBounds") or cfg.get("minZoom") is None or cfg.get("maxZoom") is None:
        out.append(finding("MAP-WEB-010", "WARN", "maxBounds, minZoom or maxZoom is not set"))
    for b in cfg.get("basemaps") or []:
        if b.get("mode") == "tiles":
            if b.get("nativeMaxZoom") is None:
                out.append(finding("MAP-WEB-010", "WARN", f"basemap {b.get('id')} has no native maximum zoom; past the provider's last zoom the page would ask for tiles that do not exist (blank squares)"))
            if int(b.get("tileSize") or 256) == 512 and "zoomOffset" not in b:
                out.append(finding("MAP-WEB-010", "WARN", f"basemap {b.get('id')} has 512-pixel tiles and no zoom offset; they would be drawn at the wrong scale"))
    for pid, lim in (report.get("provider_limits") or {}).items():
        if cfg.get("maxZoom", 0) > lim + OVERZOOM_LEVELS:
            out.append(finding("MAP-WEB-010", "WARN", f"maxZoom {cfg['maxZoom']} is above the {lim} that provider {pid} serves, by more than its tiles can be enlarged ({OVERZOOM_LEVELS} levels)"))
    if cfg.get("minZoom", 0) > cfg.get("maxZoom", 99):
        out.append(finding("MAP-WEB-010", "WARN", "minZoom is above maxZoom"))

    # 011 provider flags: every tile host belongs to an active, allowed provider
    from_registry = {h.lower() for p in (providers or []) for h in [p.get("host", "")] if h}
    live_ok = {h.lower() for h in report.get("live_hosts") or []}          # 0.15.0: a live layer's host, from its saved, host-confirmed public profile
    service_entries = {h["host"].lower() for h in (man or {}).get("hosts", []) if h.get("service")}
    for h in sorted(hosts_declared):
        if h in service_entries:
            if h not in live_ok:
                out.append(finding("MAP-WEB-011", "BLOCK", f"{h} is in the host manifest as a live service but no live layer of the page comes from a saved, host-confirmed profile for it", h))
            continue
        if providers is not None and h not in from_registry:
            out.append(finding("MAP-WEB-011", "BLOCK", f"{h} is in the host manifest but belongs to no allowed tile provider in the library", h))

    # 012 brand
    page_for_brand = without_blobs(without_data(own))
    st = getattr(plan, "style", None)                       # 1.4.0: against the resolved style pack
    out += [f for f in bl.check_palette(page_for_brand, plan.brand, st)]
    out += bl.check_fonts(page_for_brand, (st.families() | {"Inter"}) if st is not None else None)
    out += bl.check_stamps(text, plan.brand, plan.manifest)
    out += bl.check_hidden_accreditation(text, plan.brand, plan.manifest)
    if not re.search(r"@font-face\{font-family:Inter", text.replace(" ", "")):
        out.append(finding("MAP-WEB-012", "BLOCK", "Inter is not embedded in the page"))

    # 013 confidential layers
    conf = report.get("confidential") or []
    if conf:
        if not re.search(r'<meta name="robots" content="[^"]*noindex', text):
            out.append(finding("MAP-WEB-013", "BLOCK", f"layer(s) {', '.join(conf)} are client-confidential and the page is not marked noindex"))
        if "gm-conf" not in text:
            out.append(finding("MAP-WEB-013", "BLOCK", "client-confidential data is shown without the confidentiality banner"))
        if "artifact" in (report.get("delivery") or []) and not report.get("confirm_publish"):
            out.append(finding("MAP-WEB-013", "BLOCK", "a page with client-confidential data is not published as a shareable artifact without the owner's explicit confirmation (webmap.confirm_publish)"))
        out.append(finding("MAP-WEB-013", "WARN", "precision reduction for confidential coordinates is not available in this release; share the page only with people who may see the exact positions"))
        if report.get("downloadable"):
            bad = [l for l in report["downloadable"] if l in conf]
            if bad:
                out.append(finding("MAP-WEB-013", "BLOCK", f"data download is on for confidential layer(s) {', '.join(bad)}"))

    # data download only where the license allows
    if report.get("controls", {}).get("data_download") and not report.get("downloadable"):
        out.append(finding("MAP-WEB-013", "WARN", "data download is on, but no layer's license allows redistribution, so the button is left out"))

    # 014 print
    if "@media print" not in text:
        out.append(finding("MAP-WEB-014", "INFO", "the page has no print stylesheet"))
    # 016 place names: when the page says it carries names, the data must hold them with a tier and a zoom, and the switcher must offer the toggle
    out += check_names(text)
    # M14
    out += check_csp(text, engine, hosts_declared)
    out += check_pmtiles(text)
    out += check_glyphs(text)
    out += check_size(text)
    out += check_attribution(text, providers)
    out += check_time_compare(text)
    out += check_3d(text)
    out += check_story(text)
    return out


# ---------------------------------------------------------------------------------------------------------------- M14
def lint_file(path) -> list[dict]:
    """The checks that need nothing but the page itself, on a page read from disk (the webmap-build pipeline's web-lint stage; a page
    somebody edited): self-containment, hosts and analytics, secrets, the CSP, the embedded tiles, glyphs, size, attribution, names, time
    and compare."""
    text = Path(path).read_text(encoding="utf-8")
    own = strip_vendor(text)
    man = manifest_of(text)
    hosts_declared = {h["host"].lower() for h in (man or {}).get("hosts", [])}
    out: list[dict] = []
    for u in re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", text, re.I):
        if not u.startswith("data:"):
            out.append(finding("MAP-WEB-001", "BLOCK", f"the page loads {u[:80]} from outside the file", u[:120]))
    if man is None:
        out.append(finding("MAP-WEB-002", "BLOCK", "the page has no host manifest (gmap-host-manifest)"))
    undeclared = sorted(h for h in external_hosts(text) if h not in hosts_declared and not any(h == n or h.endswith("." + n) for n in NS_OK))
    if undeclared:
        out.append(finding("MAP-WEB-002", "BLOCK", f"the page can call hosts that are not in its host manifest: {', '.join(undeclared)}", undeclared))
    if ANALYTICS.search(without_blobs(without_data(own))):
        out.append(finding("MAP-WEB-002", "BLOCK", "the page contains an analytics or beacon call; map pages never report who looks at them"))
    if SECRET.search(without_blobs(without_data(own))):
        out.append(finding("MAP-WEB-003", "BLOCK", "the page appears to hold a credential (basic auth, bearer token or password); credentials are never embedded"))
    engine = "maplibre" if re.search(r'<html[^>]*\bdata-engine="maplibre"', text) else "leaflet"
    out += check_names(text)
    out += check_csp(text, engine, hosts_declared)
    out += check_pmtiles(text)
    out += check_glyphs(text)
    out += check_size(text)
    out += check_attribution(text, None)
    out += check_time_compare(text)
    out += check_3d(text)
    out += check_story(text)
    return out


MAX_PAGE_WARN = 16 * 1024 * 1024        # an artifact holds at most 16 MB, and a phone takes a while over more
MAX_PAGE_BLOCK = 60 * 1024 * 1024
MAX_GEOJSON_MAPLIBRE = 100_000


def csp_directives(text: str) -> dict | None:
    import html as htmllib
    m = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', text)
    if not m:
        return None
    out = {}
    for part in htmllib.unescape(m.group(1)).split(";"):
        bits = part.split()
        if bits:
            out[bits[0].lower()] = bits[1:]
    return out


def check_csp(text: str, engine: str, hosts_declared: set) -> list[dict]:
    """MAP-WEB-017: nothing in the policy beyond what the engine needs."""
    d = csp_directives(text)
    if d is None:
        return []
    out, bad = [], []
    allowed_hosts = {"https://" + h for h in hosts_declared}
    expect = {"default-src": {"'none'"}, "script-src": {"'unsafe-inline'"}, "style-src": {"'unsafe-inline'"}, "font-src": {"data:"},
              "img-src": {"data:", "blob:"} | allowed_hosts, "connect-src": {"'none'", "'self'"} | allowed_hosts, "base-uri": {"'none'"}, "form-action": {"'none'"}}
    if engine == "maplibre":
        expect.update({"worker-src": {"blob:"}, "child-src": {"blob:"}})
    for k, vals in d.items():
        if k not in expect:
            bad.append(f"{k} {' '.join(vals)} (the {engine} page has no use for it)")
            continue
        extra = [v for v in vals if v not in expect[k]]
        if extra:
            bad.append(f"{k} allows {' '.join(extra)}")
    for k in ("default-src", "script-src", "connect-src", "img-src", "base-uri", "form-action") + (("worker-src", "child-src") if engine == "maplibre" else ()):
        if k not in d:
            bad.append(f"{k} is missing")
    if "'unsafe-eval'" in (d.get("script-src") or []):
        bad.append("script-src allows eval")
    if bad:
        out.append(finding("MAP-WEB-017", "BLOCK", "the Content-Security-Policy allows more than the page needs: " + "; ".join(bad[:6]), bad))
    return out


def _b64_block(text: str, el_id: str) -> bytes | None:
    import base64
    m = re.search(rf'<script type="application/octet-stream" id="{re.escape(el_id)}">(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1).strip(), validate=True)
    except ValueError:
        return b""


def check_pmtiles(text: str) -> list[dict]:
    """MAP-WEB-018: each embedded PMTiles archive opens; its layers are there; the data layer has every source feature at its top zoom
    (counted by feature id); each baked cluster level counts every point once."""
    import tempfile
    cfg = json_block(text, "gmap-config") or {}
    ml = cfg.get("ml") or {}
    out: list[dict] = []
    if not ml:
        return out
    tiles = ml.get("tiles") or {}
    with tempfile.TemporaryDirectory(prefix="gmap_weblint_") as td:
        for p in ml.get("pmtiles") or []:
            raw = _b64_block(text, p.get("el", ""))
            if not raw or raw[:7] != b"PMTiles":
                out.append(finding("MAP-WEB-018", "BLOCK", f"the embedded tile archive {p.get('key')} is missing or is not a PMTiles file", p.get("key")))
                continue
            lid = next((k for k, v in tiles.items() if v.get("key") == p.get("key")), None)
            if lid is None:                                 # a basemap file: it only has to open
                continue
            f = Path(td) / f"{p['key']}.pmtiles"
            f.write_bytes(raw)
            out += _read_tiled(f, lid, tiles[lid])
    return out


def _read_tiled(path: Path, lid: str, t: dict) -> list[dict]:
    import pyogrio
    out = []
    try:
        names = {x[0] for x in pyogrio.list_layers(path)}
    except Exception as ex:  # noqa: BLE001 - an unreadable archive is the finding
        return [finding("MAP-WEB-018", "BLOCK", f"layer '{lid}': the embedded tile archive could not be opened: {str(ex)[:160]}", lid)]
    want = t.get("layers") or {}
    missing = sorted(k for k in want if k not in names)
    if missing:
        out.append(finding("MAP-WEB-018", "BLOCK", f"layer '{lid}': the tile archive lacks {', '.join(missing)}", missing))
    src_n = int(t.get("features") or 0)
    for name, e in want.items():
        if name in missing:
            continue
        cols = ["point_count", "mvt_id"] if e.get("cluster") else None
        df = pyogrio.read_dataframe(path, layer=name, ZOOM_LEVEL=str(e["maxzoom"]), read_geometry=False, columns=cols)
        if e.get("cluster"):
            # a cluster near a tile edge is also in the neighbouring tile's buffer: count each cluster once, by its feature id
            if "mvt_id" in df.columns and df["mvt_id"].notna().any():
                df = df.drop_duplicates("mvt_id")
            n = int(df["point_count"].sum()) if "point_count" in df.columns else -1
            if n != src_n:
                out.append(finding("MAP-WEB-018", "BLOCK", f"layer '{lid}': the clusters at zoom {e['maxzoom'] + 1} count {n:,} points; the layer has {src_n:,}", {"layer": name, "found": n, "expected": src_n}))
        else:
            n = int(df["mvt_id"].nunique()) if "mvt_id" in df.columns and df["mvt_id"].notna().any() else len(df)
            if n != src_n:
                out.append(finding("MAP-WEB-018", "BLOCK", f"layer '{lid}': the tiles hold {n:,} of its {src_n:,} features at zoom {e['maxzoom'] + 1}", {"layer": name, "found": n, "expected": src_n}))
    if not out:
        out.append(finding("MAP-WEB-018", "INFO", f"layer '{lid}': the embedded tiles were read back: {src_n:,} features, {len(want)} source layer(s)", lid))
    return out


def glyph_ids(pbf: bytes) -> set[int]:
    """The codepoints a MapLibre glyph PBF holds (glyphs.stacks[].glyphs[].id)."""
    def varint(b, i):
        v = s = 0
        while True:
            c = b[i]; i += 1
            v |= (c & 0x7F) << s
            if c < 0x80:
                return v, i
            s += 7

    def fields(b):
        i = 0
        while i < len(b):
            key, i = varint(b, i)
            f, wt = key >> 3, key & 7
            if wt == 0:
                v, i = varint(b, i)
                yield f, v
            elif wt == 2:
                n, i = varint(b, i)
                yield f, b[i:i + n]
                i += n
            else:
                return
    ids = set()
    for f, stack in fields(pbf):
        if f != 1:
            continue
        for f2, glyph in fields(stack):
            if f2 == 3:
                for f3, v in fields(glyph):
                    if f3 == 1:
                        ids.add(v)
    return ids


def check_glyphs(text: str) -> list[dict]:
    """MAP-WEB-019: every character a symbol layer of the page's style can draw is in the glyphs embedded for its font."""
    import base64
    st = json_block(text, "gmap-style")
    if not st:
        return []
    glyphs = json_block(text, "gmap-glyphs") or {}
    data = json_block(text, "gmap-data") or {}
    have: dict[str, set[int]] = {}
    for k, b64 in glyphs.items():
        stack = k.split("/")[0]
        have.setdefault(stack, set()).update(glyph_ids(base64.b64decode(b64)))
    need: dict[str, set[str]] = {}
    for ly in st.get("layers") or []:
        lay = ly.get("layout") or {}
        if ly.get("type") != "symbol" or not lay.get("text-field"):
            continue
        stack = (lay.get("text-font") or ["Inter Regular"])[0]
        m = re.search(r'\["get", "([^"]+)"\]', json.dumps(lay["text-field"]))
        if not m:
            continue
        field = m.group(1)
        src = (st.get("sources") or {}).get(ly.get("source")) or {}
        upper = lay.get("text-transform") == "uppercase"
        if field == "point_count_abbreviated":
            vals = ["0123456789.k"]
        elif src.get("type") == "geojson" and isinstance(src.get("data"), dict) and src["data"].get("gm"):
            vals = [str((f.get("properties") or {}).get(field)) for f in (data.get(src["data"]["gm"]) or {}).get("features") or []
                    if (f.get("properties") or {}).get(field) is not None]
        else:
            continue                                       # tiled layers and basemaps: their text is checked where it is written
        bucket = need.setdefault(stack, set())
        for v in vals:
            bucket.update(v.upper() if upper else v)
    out = []
    for stack, chars in need.items():
        miss = sorted({c for c in chars if not c.isspace() and ord(c) not in have.get(stack, set())})
        if miss:
            out.append(finding("MAP-WEB-019", "WARN", f"{len(miss)} character(s) the labels use are not in the page's {stack} glyphs, so they will not be drawn: {''.join(miss[:30])}",
                               {"stack": stack, "missing": "".join(miss)}))
    return out


def check_size(text: str) -> list[dict]:
    """MAP-WEB-020: page size and feature caps."""
    out = []
    n = len(text.encode("utf-8"))
    if n > MAX_PAGE_BLOCK:
        out.append(finding("MAP-WEB-020", "BLOCK", f"the page is {n / 1048576:.0f} MB, over the {MAX_PAGE_BLOCK / 1048576:.0f} MB limit; clip the data to the area needed, keep fewer fields, or deliver the data as a tile file beside the page", n))
    elif n > MAX_PAGE_WARN:
        out.append(finding("MAP-WEB-020", "WARN", f"the page is {n / 1048576:.1f} MB: over the 16 MB an artifact holds, and slow to open on a phone; clip the data or keep fewer fields", n))
    if re.search(r'<html[^>]*\bdata-engine="maplibre"', text):
        data = json_block(text, "gmap-data") or {}
        cfg = json_block(text, "gmap-config") or {}
        ids = {d["id"] for d in cfg.get("layers") or []}
        nf = sum(len((c or {}).get("features") or []) for k, c in data.items() if k in ids)
        if nf > MAX_GEOJSON_MAPLIBRE:
            out.append(finding("MAP-WEB-020", "WARN", f"the page draws {nf:,} features from GeoJSON; above {MAX_GEOJSON_MAPLIBRE:,} they belong in tiles", nf))
    return out


def check_attribution(text: str, providers) -> list[dict]:
    """MAP-WEB-021: an attribution for every source."""
    out = []
    cfg = json_block(text, "gmap-config") or {}
    st = json_block(text, "gmap-style")
    notes = re.search(r'<div id="gm-notes">(.*?)</div>', text, re.S)
    notes_txt = re.sub(r"<[^>]+>", " ", notes.group(1)) if notes else ""
    if cfg.get("bundled") and not cfg.get("attributions"):
        out.append(finding("MAP-WEB-021", "WARN", "the bundled background's credit (Natural Earth) is not in the attribution control"))
    if st:
        for sid, s in (st.get("sources") or {}).items():
            if sid.startswith("base-") and s.get("type") in ("raster", "vector") and not (s.get("attribution") or "").strip():
                out.append(finding("MAP-WEB-021", "BLOCK", f"the basemap source {sid[5:]} carries no attribution; its provider's credit must show on the map", sid))
            if sid.startswith("base-") and s.get("type") == "geojson" and not (s.get("attribution") or "").strip() and not cfg.get("attributions"):
                out.append(finding("MAP-WEB-021", "WARN", f"the bundled background source {sid} carries no credit", sid))
        for p in providers or []:
            s = (st.get("sources") or {}).get(f"base-{p.get('id')}")
            if s is not None and p.get("attribution") and s.get("attribution") != p.get("attribution"):
                out.append(finding("MAP-WEB-021", "BLOCK", f"the basemap source {p.get('id')} does not carry its provider's credit", p.get("attribution")))
    import html as htmllib
    nt = htmllib.unescape(notes_txt)
    for d in cfg.get("layers") or []:
        c = (d.get("credit") or "").strip()
        if c and d.get("kind") != "image" and not d.get("context") and c.split(",")[0].strip() not in nt:
            out.append(finding("MAP-WEB-021", "WARN", f"layer '{d['id']}': its credit ({c[:60]}) is not in the source note", d["id"]))
    return out


def check_time_compare(text: str) -> list[dict]:
    """MAP-WEB-022: the time slider and the swipe compare refer to layers the page has; the time layers' features carry their period."""
    out = []
    cfg = json_block(text, "gmap-config") or {}
    ids = {d["id"] for d in cfg.get("layers") or []}
    cmp = cfg.get("compare")
    if cmp:
        unknown = [x for x in (cmp.get("left") or []) + (cmp.get("right") or []) if x not in ids]
        if unknown:
            out.append(finding("MAP-WEB-022", "BLOCK", f"the swipe compare names layer(s) the page does not have: {', '.join(unknown)}", unknown))
        if 'id="gm-compare"' not in text:
            out.append(finding("MAP-WEB-022", "BLOCK", "the swipe compare is set but the page has no divider"))
    tm = cfg.get("time")
    if tm:
        if not tm.get("labels"):
            out.append(finding("MAP-WEB-022", "BLOCK", "the time slider has no periods"))
        unknown = [x for x in tm.get("layers") or [] if x not in ids]
        if unknown:
            out.append(finding("MAP-WEB-022", "BLOCK", f"the time slider names layer(s) the page does not have: {', '.join(unknown)}", unknown))
        data = json_block(text, "gmap-data") or {}
        tiled = set(((cfg.get("ml") or {}).get("tiles") or {}))
        for lid in tm.get("layers") or []:
            if lid in tiled:
                continue
            feats = (data.get(lid) or {}).get("features") or []
            bad = sum(1 for f in feats if not isinstance((f.get("properties") or {}).get("_t"), int))
            if bad:
                out.append(finding("MAP-WEB-022", "WARN", f"layer '{lid}': {bad} feature(s) carry no period, so the time slider cannot show them", bad))
        if 'id="gm-time-range"' not in text:
            out.append(finding("MAP-WEB-022", "BLOCK", "the time slider is set but the page has no slider"))
    return out


# ---------------------------------------------------------------------------------------------------------------- M15
HEIGHT_MAX_M = 1000.0


def check_3d(text: str) -> list[dict]:
    """MAP-WEB-023 (the embedded terrain reads back), 024 (heights), 025 (3D on a Leaflet page), read from the page itself."""
    cfg = json_block(text, "gmap-config") or {}
    v3 = cfg.get("view3d")
    out: list[dict] = []
    engine = "maplibre" if re.search(r'<html[^>]*\bdata-engine="maplibre"', text) else "leaflet"
    if v3 and engine != "maplibre":
        out.append(finding("MAP-WEB-025", "WARN", "the page carries 3D settings but is drawn with Leaflet, which is flat and north up"))
        return out
    if not v3:
        return out
    dem = (cfg.get("ml") or {}).get("dem")
    st = json_block(text, "gmap-style") or {}
    if v3.get("terrain") and not dem:
        out.append(finding("MAP-WEB-023", "BLOCK", "the page says it has terrain but carries no terrain tiles"))
    if dem:
        raw = _b64_block(text, dem.get("el", "gmap-dem"))
        if not raw:
            out.append(finding("MAP-WEB-023", "BLOCK", "the terrain tiles the page's config lists are not in the page (#gmap-dem)"))
        else:
            import webmap_3d as w3
            rb = w3.read_back(dem, raw)
            out += rb
            if not rb:
                out.append(finding("MAP-WEB-023", "INFO", f"terrain read back: {dem.get('tiles')} tiles, zooms {dem.get('zmin')} to {dem.get('zmax')}, about "
                                   f"{dem.get('res_m')} m a pixel at zoom {dem.get('zmax')} (the DEM has {dem.get('dem_res_m')} m cells), {len(raw) / 1048576:.1f} MB",
                                   {k: dem.get(k) for k in ("tiles", "zmin", "zmax", "res_m", "dem_res_m")}))
        src = (st.get("sources") or {}).get("gm-dem") or {}
        if src.get("type") != "raster-dem" or src.get("encoding") != "terrarium" or not all(str(u).startswith("gmdem://") for u in src.get("tiles") or [""]):
            out.append(finding("MAP-WEB-023", "BLOCK", "the style does not draw the terrain from the page's own tiles (a raster-dem source, Terrarium, gmdem://)"))
    data = json_block(text, "gmap-data") or {}
    for d in cfg.get("layers") or []:
        if d.get("d3") not in ("extrude", "mast"):
            continue
        feats = (data.get(d["id"]) or {}).get("features") or []
        hs = [(f.get("properties") or {}).get("_h") for f in feats]
        bs = [(f.get("properties") or {}).get("_b") for f in feats]
        bad = sum(1 for h in hs if not isinstance(h, (int, float)) or isinstance(h, bool))
        neg = sum(1 for h, b in zip(hs, bs) if isinstance(h, (int, float)) and (h < 0 or (d["d3"] == "extrude" and isinstance(b, (int, float)) and b > h)))
        big = sum(1 for h in hs if isinstance(h, (int, float)) and h > HEIGHT_MAX_M)
        if bad:
            out.append(finding("MAP-WEB-024", "WARN", f"layer '{d['id']}': {bad} feature(s) have no numeric height on the page, so they are drawn flat", bad))
        if neg:
            out.append(finding("MAP-WEB-024", "WARN", f"layer '{d['id']}': {neg} height(s) are negative or below their base, so they are drawn flat; check the field's sign "
                               "or the base field", neg))
        if big:
            out.append(finding("MAP-WEB-024", "WARN", f"layer '{d['id']}': {big} height(s) are over {HEIGHT_MAX_M:,.0f} m: feet or centimetres read as metres?", big))
    return out


def check_story(text: str) -> list[dict]:
    """MAP-WEB-026 read back: the story's chapters name the page's layers and a view; the column and the print view carry each chapter;
    the KPI panel's fields are carried by some layer."""
    cfg = json_block(text, "gmap-config") or {}
    out: list[dict] = []
    ids = {d["id"] for d in cfg.get("layers") or []}
    story = cfg.get("story")
    if story:
        chs = story.get("chapters") or []
        for i, ch in enumerate(chs):
            unknown = [x for x in (ch.get("layers") or []) if x not in ids]
            if unknown:
                out.append(finding("MAP-WEB-026", "BLOCK", f"chapter {i + 1} names layer(s) the page does not have: {', '.join(unknown)}", unknown))
            v = ch.get("view") or {}
            if not (v.get("bbox") or (v.get("center") and v.get("zoom") is not None)):
                out.append(finding("MAP-WEB-026", "BLOCK", f"chapter {i + 1} has no view (bbox, or center and zoom)"))
            if f'id="{ch.get("id")}"' not in text:
                out.append(finding("MAP-WEB-026", "BLOCK", f"chapter {i + 1} is not in the panel's Story tab"))
        n_print = len(re.findall(r'<article class="gm-pch">', text))
        if n_print != len(chs):
            out.append(finding("MAP-WEB-026", "WARN", f"the print view lists {n_print} of the {len(chs)} chapters"))
        n_pic = len(re.findall(r'<article class="gm-pch"><h2>[^<]*</h2><img ', text))
        want = {"off": 0, "first": min(1, n_print)}.get(story.get("printPictures") or "on", n_print)
        if n_pic < want:
            out.append(finding("MAP-WEB-026", "WARN", f"{n_print - n_pic} chapter(s) print without their picture"))
    panel = cfg.get("panel")
    if panel:
        data = json_block(text, "gmap-data") or {}
        have = set()
        for lid in ids:
            for f in ((data.get(lid) or {}).get("features") or [])[:50]:
                have.update((f.get("properties") or {}).keys())
        miss = [f["field"] for f in panel.get("fields") or [] if f.get("field") not in have]
        if miss:
            out.append(finding("MAP-WEB-026", "WARN", f"the KPI panel's field(s) {', '.join(miss)} are on no layer the page carries", miss))
    return out


def check_names(text: str) -> list[dict]:
    """MAP-WEB-016 (M10): a page whose config turns place names on carries a `gm-names` collection whose every feature has a `tier` and a
    numeric `min_zoom` (the page shows and hides by them), and lists 'Place names' in its layer switcher; the data labels it binds name a
    field. Read back from the page text, so an edited page is checked too."""
    out: list[dict] = []
    cm = re.search(r'<script type="application/json" id="gmap-config">(.*?)</script>', text, re.S)
    dm = re.search(r'<script type="application/json" id="gmap-data">(.*?)</script>', text, re.S)
    if not cm or not dm:
        return out
    try:
        cfg = json.loads(cm.group(1))
        data = json.loads(dm.group(1))
    except ValueError:
        return out
    names = cfg.get("names") or {}
    if names.get("on"):
        col = data.get(names.get("id") or "gm-names") or {}
        feats = col.get("features") or []
        if not feats:
            out.append(finding("MAP-WEB-016", "WARN", "the page says place names are on but carries none"))
        bad = [f for f in feats if not (f.get("properties") or {}).get("tier") or not isinstance((f.get("properties") or {}).get("min_zoom"), (int, float))]
        if bad:
            out.append(finding("MAP-WEB-016", "WARN", f"{len(bad)} place name(s) on the page have no tier or zoom, so the page cannot show them by zoom", len(bad)))
        if f'value="{names.get("id") or "gm-names"}"' not in text:
            out.append(finding("MAP-WEB-016", "WARN", "the layer switcher has no 'Place names' toggle, so the names cannot be turned off"))
    for fl in cfg.get("featureLabels") or []:
        if not fl.get("field") or not fl.get("layer"):
            out.append(finding("MAP-WEB-016", "WARN", "a feature label names no layer or field"))
    return out


# ---------------------------------------------------------------------------------------------------------------- 1.1.0: the frame
def _norm(s: str) -> str:
    return re.sub(r"[\s.]+", " ", str(s or "").replace("©", "").lower()).strip()


def check_frame(text: str, own: str, report: dict, providers) -> list[dict]:
    """MAP-WEB-030 to 033, 036, 038 read from the page itself (034, 035, 039 and 040 need a browser: web_layout_check.py).
    030 every basemap provider's credit is in the corner; 031 the corner is not switched off or hidden by style; 032 a provider logo the
    terms require is carried and not restyled; 033 the corner's text is at least 11 px and 4.5:1 on its own ground; 036 is raised at build;
    038 a story card's text is under 90 words."""
    out: list[dict] = []
    cfg = json_block(text, "gmap-config") or {}
    fr = cfg.get("frame") or {}
    items = fr.get("credits") or []
    for b in providers or []:
        if b.get("id") and not any(b["id"] in (i.get("bases") or []) and i.get("required") for i in items):
            out.append(finding("MAP-WEB-030", "BLOCK", f"the basemap {b.get('id')} has no required credit in the map's credits corner", b.get("id")))
    if re.search(r"(?:^|[},])\s*#gm-credits\s*\{[^}]*(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0[;}])", own) or \
            re.search(r'id="gm-credits"[^>]*\bhidden\b', text):
        out.append(finding("MAP-WEB-031", "BLOCK", "the credits corner is hidden by style; the credits the providers require stay visible"))
    for b in providers or []:
        if b.get("wordmark"):
            if 'id="gm-wordmark"' not in text:
                out.append(finding("MAP-WEB-032", "BLOCK", f"{b.get('id')} requires its logo on the map and the page has no place for it", b.get("id")))
            if re.search(r"#gm-wordmark img\{[^}]*(?:filter|opacity\s*:\s*0)", own):
                out.append(finding("MAP-WEB-032", "BLOCK", "the provider's logo is restyled; its terms require it unaltered", b.get("id")))
    m = re.search(r"#gm-credits\{[^}]*font-size:([\d.]+)px", own)
    if m and float(m.group(1)) < 11:
        out.append(finding("MAP-WEB-033", "WARN", f"the credits corner's text is {m.group(1)} px; 11 px is the least", float(m.group(1))))
    c = report.get("credits_contrast")
    if c is not None and c < 4.5:
        out.append(finding("MAP-WEB-033", "WARN", f"the credits corner's text has a contrast of {c}:1 on its ground; 4.5:1 is the minimum", c))
    for ch in (cfg.get("story") or {}).get("chapters") or []:
        m2 = re.search(r'<li class="gm-chapter" id="' + re.escape(str(ch.get("id"))) + r'".*?<div class="gm-ch-body">(.*?)</div></li>', text, re.S)
        words = len(re.sub(r"<[^>]+>", " ", m2.group(1)).split()) if m2 else 0
        if words > 90:
            out.append(finding("MAP-WEB-038", "WARN", f"the story card '{ch.get('title')}' has {words} words; keep a card under 90 so it reads at a glance", words))
    return out
