#!/usr/bin/env python3
"""webmap_frame: the frame around a web map page (1.1.0; project spec spec-webmap-frame-ux-2026-11-09).

One panel (title, story, legend, layers, details, about), one control rail on the map, one playback dock, the brand chip in the map's
bottom-left corner and the required credits in its bottom-right corner. Every element can be switched on or off under `webmap.frame`;
`auto` shows an element only when the page has content for it.

This module holds everything the frame decides at build time and is engine-neutral:

    resolve(...)          webmap.frame (presets, defaults, the 1.0.x webmap.controls keys mapped across) -> the frame config
    credit_items(...)     the credits corner: one item per holder, read from the library's `attribution:` blocks (basemap providers and
                          datasets), a layer's own provenance.credit, or derived from license.attribution_text when an entry has no block
    brand(...)            the brand chip from the active brand pack: logo (light or dark for the ground), name, homepage, optional co-brand
    layer_names(...)      the names the Layers tab shows (layers[].name, then the legend label, a unique legend title, the id in words)
    period_display(...)   the playback label per period (webmap.frame.playback.label: "km {end}")
    markup(...)           the frame's HTML fragments
    css(tokens)           the frame's style sheet, every colour from the brand's map tokens (a literal colour is a BLOCK, as before)

The browser half is assets/web/frame.js. Nothing here fetches; nothing here names a brand: the active brand pack supplies every mark and colour,
so a brand pack or a co-brand gets its own frame without code.
"""
from __future__ import annotations

import copy
import html as htmllib
import re

FRAME_KEYS = ("preset", "panel", "title_chip", "controls", "playback", "legend", "brand", "attribution", "scale_bar", "locator", "search",
              "popups", "tools", "shortcuts")
PRESETS = ("auto", "standard", "story", "minimal", "presentation", "embed")
TABS = ("story", "legend", "layers", "details", "about")
TOOLS = ("measure", "save_image", "print", "copy_link", "download")
COLLAPSE = ("never", "after_interaction", "allowed")
NO_CREDIT_NEEDED = {"public-domain", "cc0", "public domain"}
SEARCH_AUTO_MIN = 20              # search: auto turns on above about this many named features
PANEL_WIDTH = (320, 440)

DEFAULTS = {
    "preset": "auto",
    "panel": {"show": "auto", "side": "left", "width": 360, "start": "open", "remember": True, "tabs": list(TABS), "default_tab": "auto"},
    "title_chip": "auto",
    "controls": {"zoom": "auto", "compass": "auto", "dimension": "auto", "fullscreen": "on"},
    "playback": {"show": "auto", "mode": "full", "position": "map", "label": None, "speeds": [0.5, 1, 2, 4], "loop": False, "autoplay": False},
    "legend": {"float": "auto", "toggles": True},
    "brand": {"show": "mark", "co_brand": None, "reason": None},     # 1.6.3: the logo alone, as on the print map (chip: logo and name on a pill)
    "attribution": {"collapse": "auto"},
    "scale_bar": "auto",
    "locator": "off",
    "search": "auto",
    "popups": "auto",
    "tools": list(TOOLS),
    "shortcuts": "on",
}

# what a preset fills in; a key set in webmap.frame wins (spec 9.2)
PRESET_FILL = {
    "standard": {"panel": {"show": "on", "start": "open"}, "legend": {"float": False}},
    "story": {"panel": {"show": "on", "start": "open"}, "legend": {"float": False}},
    "minimal": {"panel": {"show": "off"}, "legend": {"float": True}},
    "presentation": {"panel": {"show": "on", "start": "collapsed"}, "legend": {"float": False}, "playback": {"autoplay": True}, "fade_rail": True},
    "embed": {"panel": {"show": "on", "start": "collapsed"}, "legend": {"float": False}, "controls": {"fullscreen": "on"}, "drop_tools": ["copy_link"]},
}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def esc(s) -> str:
    return htmllib.escape("" if s is None else str(s), quote=True)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _onoff(v, auto: bool) -> bool:
    """'on' | 'off' | 'auto' | True | False -> bool, with auto's answer given."""
    if v in (True, "on"):
        return True
    if v in (False, "off", None):
        return False
    return bool(auto)


# ---------------------------------------------------------------------------------------------------------------- configuration
def controls_mapping(asked: dict) -> tuple[dict, list[str]]:
    """The 1.0.x `webmap.controls` keys a MapSpec sets, as webmap.frame settings (spec 9.3). -> (frame overrides, keys mapped)."""
    out: dict = {}
    moved = []
    for k, v in (asked or {}).items():
        if k in ("error_banners", "cluster", "compare", "opacity"):
            continue                                        # unchanged in 1.1.0 (opacity now sits in each layer's menu)
        moved.append(k)
        if k == "title_panel" and v is False:
            out.setdefault("panel", {})["show"] = "off"
            out["title_chip"] = "off"
        elif k == "legend" and v is False:
            out.setdefault("_drop_tabs", []).append("legend")
        elif k in ("layer_switcher", "basemap_switcher") and v is False:
            out.setdefault("_layers_off", []).append(k)
        elif k == "popups":
            out["popups"] = "auto" if v else "off"
        elif k == "scale":
            out["scale_bar"] = "auto" if v else "off"
        elif k == "compass":
            out.setdefault("controls", {})["compass"] = "auto" if v else "off"
        elif k == "search":
            out["search"] = "on" if v else "off"
        elif k == "fullscreen":
            out.setdefault("controls", {})["fullscreen"] = "on" if v else "off"
        elif k in ("data_download", "print_view", "export_image", "measure", "share_state_in_url"):
            tool = {"data_download": "download", "print_view": "print", "export_image": "save_image", "measure": "measure", "share_state_in_url": "copy_link"}[k]
            out.setdefault("_tools", {})[tool] = bool(v)
        elif k == "time_slider":
            out.setdefault("playback", {})["show"] = "auto" if v else "off"
        elif k == "logo" and v is False:
            out["brand"] = {"show": "off", "reason": "webmap.controls.logo: false (1.0.x)"}
    return out, moved


def resolve(wm: dict, *, story: bool, time_on: bool, three_d: bool, delivery: list, engine: str = "leaflet") -> tuple[dict, list[dict]]:
    """The frame config for a page: the preset, then the defaults, then what webmap.frame and the 1.0.x webmap.controls say."""
    fs: list[dict] = []
    asked = dict((wm or {}).get("frame") or {})
    preset = asked.get("preset") or "auto"
    if preset not in PRESETS:
        fs.append(finding("MAP-SPEC-008", "WARN", f"webmap.frame.preset '{preset}' is not one of {', '.join(PRESETS)}; auto is used"))
        preset = "auto"
    if preset == "auto":
        preset = "story" if story else "standard"
    mapped, moved = controls_mapping((wm or {}).get("controls") or {})
    if moved:
        fs.append(finding("MAP-SPEC-008", "INFO", "moved to webmap.frame: the 1.0.x webmap.controls key(s) " + ", ".join(sorted(moved))
                          + " still work and are mapped to the frame (see webmap-controls)", sorted(moved)))
    fill = copy.deepcopy(PRESET_FILL.get(preset, {}))
    fc = _merge(_merge(_merge(DEFAULTS, {k: v for k, v in fill.items() if k in FRAME_KEYS}), {k: v for k, v in mapped.items() if k in FRAME_KEYS}),
                {k: v for k, v in asked.items() if k in FRAME_KEYS})
    fc["preset"] = preset
    fc["fade_rail"] = bool(fill.get("fade_rail"))
    unknown = sorted(set(asked) - set(FRAME_KEYS))
    if unknown:
        fs.append(finding("MAP-SPEC-008", "WARN", f"webmap.frame has key(s) the frame does not know: {', '.join(unknown)}", unknown))

    # tools: the list, less what the 1.0.x keys turned off and what the preset drops
    tools = [t for t in (fc.get("tools") or []) if t in TOOLS]
    for t, on in (mapped.get("_tools") or {}).items():
        if not on and t in tools:
            tools.remove(t)
        elif on and t not in tools and "tools" not in asked:
            tools.append(t)
    for t in fill.get("drop_tools") or []:
        if t in tools and "tools" not in asked:
            tools.remove(t)
    fc["tools"] = tools
    fc["_drop_tabs"] = list(mapped.get("_drop_tabs") or [])
    fc["_layers_off"] = list(mapped.get("_layers_off") or [])

    p = fc["panel"]
    p["show"] = {"auto": "on", True: "on", False: "off"}.get(p.get("show"), p.get("show"))
    if p["show"] not in ("on", "off"):
        p["show"] = "on"
    if p.get("side") not in ("left", "right"):
        p["side"] = "left"
    try:
        p["width"] = max(PANEL_WIDTH[0], min(PANEL_WIDTH[1], int(p.get("width") or 360)))
    except (TypeError, ValueError):
        p["width"] = 360
    if p.get("start") not in ("open", "collapsed"):
        p["start"] = "open"

    pb = fc["playback"]
    pb["on"] = bool(time_on) and _onoff(pb.get("show"), True) and pb.get("mode") != "hidden"
    if pb.get("mode") not in ("full", "slider", "hidden"):
        pb["mode"] = "full"
    if pb.get("position") not in ("map", "panel"):
        pb["position"] = "map"
    speeds = [float(s) for s in (pb.get("speeds") or [1]) if isinstance(s, (int, float)) and 0 < s <= 16] or [1.0]
    pb["speeds"] = speeds

    lg = fc["legend"]
    lg["float"] = _onoff(lg.get("float"), preset == "minimal")

    c = fc["controls"]
    c["zoom"] = c.get("zoom", "auto") if c.get("zoom") in ("auto", "on", "off", True, False) else "auto"
    c["compass"] = _onoff(c.get("compass"), True)
    c["dimension"] = _onoff(c.get("dimension"), True) and bool(three_d)
    c["fullscreen"] = _onoff(c.get("fullscreen"), True)

    b = fc["brand"] if isinstance(fc.get("brand"), dict) else {"show": fc.get("brand")}
    if b.get("show") in (True, None):
        b["show"] = "mark"
    if b.get("show") is False:
        b["show"] = "off"
    if b["show"] not in ("chip", "mark", "off"):
        b["show"] = "mark"
    if b["show"] == "off" and not (b.get("reason") or "").strip():
        fs.append(finding("MAP-WEB-036", "WARN", "webmap.frame.brand.show is off without a reason:, so the brand logo is drawn anyway; give the reason to switch it off"))
        b["show"] = "mark"
    fc["brand"] = b

    at = fc["attribution"] if isinstance(fc.get("attribution"), dict) else {"collapse": fc.get("attribution")}
    if at.get("collapse") in (False, "off") or asked.get("attribution") in (False, "off") or at.get("show") in (False, "off"):
        fs.append(finding("MAP-WEB-031", "BLOCK", "webmap.frame.attribution cannot be switched off: the credits the providers and licences require stay in the map corner"))
    if at.get("collapse") not in ("auto", "never"):
        at["collapse"] = "auto"
    fc["attribution"] = {"collapse": at["collapse"]}

    fc["scale_bar"] = _onoff(fc.get("scale_bar"), True)
    fc["locator"] = _onoff(fc.get("locator"), False)
    if fc["locator"]:
        fs.append(finding("MAP-SPEC-008", "INFO", "webmap.frame.locator: the web page draws no locator inset yet (the print map does); the page is built without it"))
    fc["shortcuts"] = _onoff(fc.get("shortcuts"), True)
    if fc.get("popups") not in ("auto", "popup", "panel", "off"):
        fc["popups"] = "auto" if fc.get("popups") in (True, None) else "off"
    fc["title_chip"] = fc.get("title_chip", "auto")
    fc["delivery"] = list(delivery or ["file"])
    fc["engine"] = engine
    return fc, fs


def tools_for(fc: dict, *, engine: str, downloadable: bool, share: bool) -> tuple[list[str], list[dict], list[str]]:
    """The ⋯ menu's items for the delivery (spec 13.3): an item that cannot work where the page is delivered is not shown.
    -> (tools, findings, about notes)."""
    fs, notes = [], []
    out = []
    artifact_only = fc["delivery"] == ["artifact"]
    for t in fc["tools"]:
        if t == "download" and not downloadable:
            continue
        if t == "copy_link" and not share:
            continue
        if artifact_only and t in ("save_image", "download", "print"):
            fs.append(finding("MAP-WEB-041", "INFO", f"the {t.replace('_', ' ')} tool is hidden: a page published as an artifact cannot start a download"
                              + (" or open the print dialog" if t == "print" else "") + "; the file version of the page has it"))
            if not notes:
                notes.append("Saving a picture, downloading the data and printing work in the file version of this page.")
            continue
        out.append(t)
    return out, fs, notes


# ---------------------------------------------------------------------------------------------------------------- credits
_A = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def _links_from_html(h: str | None) -> list[dict]:
    out = []
    for href, txt in _A.findall(h or ""):
        t = re.sub(r"<[^>]+>", "", txt).strip()
        if href.startswith("https://") and t:
            out.append({"text": htmllib.unescape(t), "href": href})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s.]+", " ", str(s or "").replace("©", "").lower()).strip()


def _item(key: str, block: dict, *, base: str | None = None, layer: str | None = None, source: str = "") -> dict:
    corner = str(block.get("corner") or block.get("text") or "").strip().rstrip(".")
    links = [dict(l) for l in (block.get("links") or []) if isinstance(l, dict) and str(l.get("href", "")).startswith("https://") and l.get("text")]
    col = block.get("collapse") or ("after_interaction" if block.get("required_on_map", True) else "allowed")
    if col not in COLLAPSE:
        col = "after_interaction"
    logo = block.get("logo") if isinstance(block.get("logo"), dict) else None
    if logo and logo.get("file"):                         # 1.1.3: a logo file the plugin ships, drawn inline beside the credit in the text colour
        import provider_logos as plg
        try:
            lg = plg.load(logo)
            logo = {"svg": plg.inline_svg(lg, "gm-cr-logo"), "w": round(plg.width_px(lg), 2), "h": lg["height_px"], "link": lg["link"],
                    "alt": lg["alt"], "file": lg["file"], "required": lg["required"]}
        except plg.LogoError as ex:
            logo = {"error": str(ex), "file": logo.get("file"), "required": logo.get("required", True) is not False}
    exp = block.get("export") if isinstance(block.get("export"), dict) else {}
    return {"key": key, "corner": corner, "links": links, "full": str(block.get("full") or corner).strip(), "required": bool(block.get("required_on_map", True)),
            "collapse": col, "logo": logo, "export": str(exp.get("text") or block.get("full") or corner).strip().rstrip("."),
            "dynamic": block.get("dynamic") if isinstance(block.get("dynamic"), dict) else None, "short": str(block.get("short") or "").strip() or None,
            "bases": [base] if base else [], "layers": [layer] if layer else [], "always": not base and not layer, "sources": [source] if source else []}


def entry_blocks(entry: dict | None, *, attribution_text: str | None = None, attribution_html: str | None = None, lic_class: str | None = None) -> list[dict]:
    """A library entry's credits as attribution blocks: its own `attribution:` block (and `attribution.extra`), else one derived from
    license.attribution_text and provider.attribution_html (links kept). A public-domain entry's credit is a courtesy: About only."""
    e = entry or {}
    blk = e.get("attribution") if isinstance(e.get("attribution"), dict) else None
    if blk:
        return [blk] + [x for x in (blk.get("extra") or []) if isinstance(x, dict)]
    lic = e.get("license") or {}
    text = attribution_text or lic.get("attribution_text")
    if not text:
        return []
    klass = str(lic_class or lic.get("class") or "").lower()
    req = klass not in NO_CREDIT_NEEDED
    html = attribution_html or (e.get("provider") or {}).get("attribution_html")
    return [{"corner": str(text).strip().rstrip("."), "links": _links_from_html(html), "required_on_map": req,
             "collapse": "after_interaction" if req else "allowed", "derived": True}]


def credit_items(plan, base_defs: list[dict], defs: list[dict], entries_by_id: dict, bundled_records: list[dict]) -> tuple[list[dict], list[dict]]:
    """The credits corner's items (spec 8). One item per holder: the same corner text from two sources is one item serving both. Returns
    (items in draw order: active basemap first, then the layers; findings)."""
    fs: list[dict] = []
    items: dict[str, dict] = {}

    def add(blocks, *, base=None, layer=None, source=""):
        for blk in blocks:
            if not blk.get("corner") and not blk.get("text"):
                continue
            it = _item("", blk, base=base, layer=layer, source=source)
            k = _norm(it["corner"])
            if not k:
                continue
            if k in items:
                cur = items[k]
                cur["bases"] += [b for b in it["bases"] if b not in cur["bases"]]
                cur["layers"] += [x for x in it["layers"] if x not in cur["layers"]]
                cur["always"] = cur["always"] or it["always"]
                cur["sources"] += [s for s in it["sources"] if s not in cur["sources"]]
                cur["required"] = cur["required"] or it["required"]
                if COLLAPSE.index(it["collapse"]) < COLLAPSE.index(cur["collapse"]):
                    cur["collapse"] = it["collapse"]
                for l in it["links"]:
                    if l not in cur["links"]:
                        cur["links"].append(l)
                cur["logo"] = cur["logo"] or it["logo"]
            else:
                it["key"] = f"c{len(items)}"
                items[k] = it

    for b in base_defs:                                  # basemaps: each one's credit applies while it is the background
        if b.get("mode") == "bundled":
            continue
        e = entries_by_id.get(b.get("entry")) or {}
        add(entry_blocks(e, attribution_text=None, attribution_html=None), base=b["id"], source=e.get("title") or b.get("title") or b["id"])
    for r in bundled_records or []:                      # the bundled background (Natural Earth): a courtesy, About only (D7)
        add([dict(x, required_on_map=False, collapse="allowed") for x in entry_blocks(r)], base="bundled", source=(r or {}).get("title") or "bundled background")
    by_id = {l.id: l for l in plan.layers}
    for d in defs:                                       # layers in draw order; each one's credit applies while it is shown
        if d.get("kind") == "image" and d.get("role") == "hillshade":
            lid = d.get("alias") or d["id"]
            l = by_id.get(lid)
            rel = next((b for b in plan.basemap if b.get("kind") == "hillshade"), None)
            e = (rel or {}).get("entry") or {}
            prov = (l.provenance if l is not None else None) or {}
            blocks = _provenance_blocks(prov) or entry_blocks(e) or _provenance_text(prov)
            add(blocks, layer=d["id"], source="relief")
            continue
        l = by_id.get(d["id"])
        if l is None:
            if d.get("kind") == "live" and d.get("credit"):
                add([{"corner": d["credit"], "required_on_map": True}], layer=d["id"], source=d.get("title") or d["id"])
            continue
        prov = l.provenance or {}
        blocks = _provenance_blocks(prov) or entry_blocks(l.entry) or _provenance_text(prov)
        add(blocks, layer=d["id"], source=l.title or l.id)
    out = list(items.values())
    for it in out:
        it.pop("always", None) if it["bases"] or it["layers"] else None
        lg = it.get("logo") or {}
        if lg.get("error"):
            fs.append(finding("MAP-LIC-015", "BLOCK" if lg.get("required") else "WARN",
                              f"the credit '{it['corner']}' needs its provider's logo beside it and it cannot be drawn: {lg['error']}", lg.get("file")))
            it["logo"] = None
    return out, fs


def _provenance_blocks(prov: dict) -> list[dict]:
    """A person's own file: provenance.credit is an attribution block (spec 8.5), or a list of them."""
    c = prov.get("credit")
    if isinstance(c, dict):
        return [c] + [x for x in (c.get("extra") or []) if isinstance(x, dict)]
    if isinstance(c, list):
        return [x for x in c if isinstance(x, dict)]
    return []


def _provenance_text(prov: dict) -> list[dict]:
    a = prov.get("attribution")
    if not a:
        return []
    lic = str(prov.get("license") or "").strip().lower()
    req = lic not in NO_CREDIT_NEEDED
    return [{"corner": str(a).strip().rstrip("."), "required_on_map": req, "collapse": "after_interaction" if req else "allowed", "derived": True}]


def check_required(items: list[dict], required_lines: list[str]) -> list[dict]:
    """MAP-WEB-030: every credit required_credit_lines lists is in the corner, verbatim or in its declared corner form."""
    fs = []
    corner = " · ".join(_norm(i["corner"]) for i in items if i["required"])
    fulls = " · ".join(_norm(i["full"]) for i in items if i["required"])
    for line in required_lines:
        n = _norm(line)
        if n and n not in corner and n not in fulls:
            fs.append(finding("MAP-WEB-030", "BLOCK", f"the required credit '{line.rstrip('.')}' is not in the map's credits corner; give its library entry or the layer's "
                              "provenance an attribution block (attribution.corner) so the corner can carry it", line))
    return fs


# ---------------------------------------------------------------------------------------------------------------- brand
def _svg_sized(svg: str, height: int, cls: str, label: str) -> str:
    m = re.search(r'viewBox="([\d.\-\s]+)"', svg or "")
    vb = [float(v) for v in m.group(1).split()] if m else [0, 0, 100, 100]
    w = max(1, round(height * vb[2] / vb[3])) if vb[3] else height
    inner = re.sub(r"^<\?xml[^>]*>\s*", "", (svg or "").strip())
    return re.sub(r"<svg\b([^>]*)>", lambda mm: f'<svg width="{w}" height="{height}" viewBox="{m.group(1) if m else "0 0 100 100"}" class="{cls}" role="img" '
                  f'aria-label="{esc(label)}" focusable="false" xmlns="http://www.w3.org/2000/svg">', inner, count=1)


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def brand(plan, fc: dict, tokens: dict) -> dict:
    """The brand chip's parts from the active brand pack (spec 7): the logo for the chip's ground (dark logo on a dark theme), the brand's
    name and homepage, and a co-brand after a divider. Nothing here is a literal: a pack without a logo gets its name, without a homepage no link."""
    b = plan.pack.get("brand") or {}
    ident = b.get("identity") or {}
    logo = b.get("logo") or {}
    ground = tokens["legend_bg"]
    key = "dark" if luminance(ground) < 0.18 else "light"
    svg = logo.get(key) or logo.get("light") or logo.get("dark") or ""
    # 1.6.3: the logo on the map itself (show: mark) is chosen for the map's ground by the print map's rule (map_render.logo_markup)
    mground = tokens.get("background") or tokens.get("land") or ground
    mkey = "dark" if luminance(mground) < 0.4 else "light"
    svg_map = logo.get(mkey) or logo.get("light") or logo.get("dark") or ""
    name = ident.get("name") or plan.brand_name
    home = ident.get("homepage") or ""
    co = (fc.get("brand") or {}).get("co_brand")
    co_out = None
    if isinstance(co, dict) and co.get("name"):
        co_svg = ""
        if co.get("logo_svg"):
            co_svg = str(co["logo_svg"])
        co_out = {"name": str(co["name"]), "url": str(co.get("url") or "") if str(co.get("url") or "").startswith("https://") else "", "svg": co_svg}
    acc = (plan.pack.get("pack") or {}).get("accreditation") or b.get("accreditation") or {}
    return {"name": name, "homepage": home if home.startswith("https://") else "", "svg": svg, "svg_map": svg_map, "show": (fc.get("brand") or {}).get("show", "mark"),
            "co": co_out, "accreditation": acc.get("full") or acc.get("text") or "", "acc_href": acc.get("href") or ""}


def brand_chip_html(bd: dict, *, where: str = "map") -> str:
    if bd["show"] == "off":
        return ""
    mark = bd["show"] == "mark"
    src = (bd.get("svg_map") or bd["svg"]) if (mark and where == "map") else bd["svg"]
    logo = _svg_sized(src, 20, "gm-brand-logo", f"{bd['name']} logo") if src else ""
    name = "" if mark and logo else f'<span class="gm-brand-name">{esc(bd["name"])}</span>'
    inner = f"{logo}{name}"
    if bd["homepage"]:
        inner = f'<a class="gm-brand-main" href="{esc(bd["homepage"])}" target="_blank" rel="noopener" aria-label="{esc(bd["name"])} (opens in a new tab)">{inner}</a>'
    else:
        inner = f'<span class="gm-brand-main">{inner}</span>'
    co = ""
    if bd.get("co"):
        c = bd["co"]
        cl = _svg_sized(c["svg"], 20, "gm-brand-logo", f"{c['name']} logo") if c.get("svg") else ""
        ci = cl if (mark and cl) else f'{cl}<span class="gm-brand-name">{esc(c["name"])}</span>'
        co = '<span class="gm-brand-div" aria-hidden="true"></span>' + (f'<a class="gm-brand-co" href="{esc(c["url"])}" target="_blank" rel="noopener">{ci}</a>'
                                                                         if c["url"] else f'<span class="gm-brand-co">{ci}</span>')
    ident = ' id="gm-brand"' if where == "map" else ' id="gm-brand-sheet"'
    return f'<div class="gm-brand gm-brand-{where}{" gm-brand-markonly" if bd["show"] == "mark" else ""}"{ident} role="group" aria-label="{esc(bd["name"])}">{inner}{co}</div>'


# ---------------------------------------------------------------------------------------------------------------- names and labels
def _words(s: str) -> str:
    t = re.sub(r"[_\-]+", " ", str(s)).strip()
    return t[:1].upper() + t[1:] if t else t


def layer_names(defs: list[dict], blocks: list[dict], spec_layers: list[dict]) -> list[dict]:
    """d['name'] for every layer the Layers tab lists (spec 5.2): layers[].name, else the legend label (a one-item legend block), else the
    legend title when no other layer shares it, else the id in words. Two layers with one name: MAP-WEB-037."""
    by_spec = {str(s.get("id")): s for s in spec_layers or [] if isinstance(s, dict)}
    blk = {}
    for b in blocks:
        if b.get("layer") and b["layer"] not in blk:
            blk[b["layer"]] = b
    titles = [str((blk.get(d["id"]) or {}).get("title") or "") for d in defs]
    for d in defs:
        s = by_spec.get(d.get("alias") or d["id"]) or {}
        b = blk.get(d["id"]) or {}
        items = b.get("items") or []
        name = s.get("name")
        if not name and len(items) == 1 and items[0].get("label"):
            name = items[0]["label"]
        if not name and b.get("title") and titles.count(str(b["title"])) == 1:
            name = b["title"]
        if not name and d.get("role") == "hillshade":
            name = "Relief"
        d["name"] = str(name or _words(d.get("alias") or d["id"]))
    fs = []
    shown = [d for d in defs if not d.get("context")]
    seen: dict[str, list] = {}
    for d in shown:
        seen.setdefault(d["name"].lower(), []).append(d["id"])
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    if dup:
        fs.append(finding("MAP-WEB-037", "WARN", "layers share a name in the Layers tab: " + "; ".join(f"{', '.join(v)}" for v in dup.values())
                          + "; give each its own layers[].name", dup))
    return fs


def period_display(time_cfg: dict, label: str | None, decimals: int | None = None, fmt: str | None = None) -> list[str]:
    """The dock's label for each period (spec 6.3; Day 10 issue 2). Tokens {start}, {end}, {value}; numbers keep `decimals` (default: as the
    step needs), dates use `fmt` (strftime) or the slider's own label. Without a label: the slider's own, with ' to ' ranges for numbers."""
    labels = list(time_cfg.get("labels") or [])
    num = time_cfg.get("num")
    if not label:
        return labels
    out = []
    for k, lab in enumerate(labels):
        if num:
            lo, step = float(num["lo"]), float(num["step"])
            d = decimals if decimals is not None else (0 if step.is_integer() and lo.is_integer() else max(1, len(f"{step:g}".split(".")[-1])))
            s, e = lo + k * step, lo + (k + 1) * step
            f = lambda x: f"{x:,.{d}f}"                             # noqa: E731
            vals = {"start": f(s), "end": f(e), "value": f(s)}
        else:
            starts = time_cfg.get("starts") or []
            if fmt and k < len(starts) and starts[k]:
                import pandas as pd
                ts = pd.Timestamp(starts[k])
                v = ts.strftime(fmt)
            else:
                v = lab
            vals = {"start": v, "end": v, "value": v}
        try:
            out.append(label.format(**vals))
        except (KeyError, IndexError, ValueError):
            out.append(lab)
    return out


# ---------------------------------------------------------------------------------------------------------------- markup
ICONS = {
    # 24 x 24, stroke currentColor 1.75, one sprite shipped in the page (spec 10)
    "plus": '<path d="M12 5v14M5 12h14"/>', "minus": '<path d="M5 12h14"/>',
    "compass": '<path d="M12 3l3.5 9H8.5z" fill="currentColor"/><path d="M12 21l-3.5-9h7z"/>',
    "home": '<path d="M4 11l8-7 8 7"/><path d="M6 10v10h12V10"/>', "full": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "play": '<path d="M8 5l11 7-11 7z" fill="currentColor"/>', "pause": '<path d="M8 5v14M16 5v14"/>',
    "prev": '<path d="M6 5v14M18 5L8 12l10 7z"/>', "next": '<path d="M18 5v14M6 5l10 7-10 7z"/>',
    "loop": '<path d="M4 12a7 7 0 0 1 12-5l3 3M20 12a7 7 0 0 1-12 5l-3-3"/><path d="M19 4v6h-6M5 20v-6h6"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>', "more": '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
    "share": '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
    "collapse": '<path d="M15 6l-6 6 6 6"/>', "expand": '<path d="M9 6l6 6-6 6"/>', "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eyeoff": '<path d="M3 3l18 18M10.6 6.1A10 10 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3 3.6M6.6 6.6A17 17 0 0 0 2 12s4 7 10 7a9.6 9.6 0 0 0 4-.9"/>',
    "story": '<path d="M5 4h10l4 4v12H5z"/><path d="M8 11h8M8 15h6"/>', "legend": '<path d="M4 6h3M4 12h3M4 18h3M10 6h10M10 12h10M10 18h10"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>', "details": '<path d="M5 4h14v16H5z"/><path d="M8 9h8M8 13h8M8 17h5"/>',
    "about": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>', "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "cube": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12L4 7.5M12 12v9"/>', "handle": '<path d="M8 12h8"/>',
}


def icon(name: str, label: str | None = None) -> str:
    a = f' role="img" aria-label="{esc(label)}"' if label else ' aria-hidden="true"'
    return f'<svg class="gm-i" viewBox="0 0 24 24"{a} focusable="false"><use href="#gmi-{name}"/></svg>'


def sprite() -> str:
    syms = "".join(f'<symbol id="gmi-{k}" viewBox="0 0 24 24">{v}</symbol>' for k, v in ICONS.items())
    return f'<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true"><defs>{syms}</defs></svg>'


def credits_html(items: list[dict]) -> str:
    """The corner as the page first draws it (every item, in order); frame.js shows the ones that apply and keeps it current."""
    lis = []
    for it in items:
        txt = esc(it["corner"])
        if it["links"]:                                  # the first link whose text the corner contains becomes the link (OSM: /copyright)
            l0 = next((l for l in it["links"] if l["text"].replace("©", "").strip() and l["text"].replace("©", "").strip().lower() in it["corner"].lower()), it["links"][0])
            inner = f'<a href="{esc(l0["href"])}" target="_blank" rel="noopener">{txt}</a>'
        else:
            inner = txt
        lis.append(f'<li class="gm-cr" data-key="{it["key"]}" data-collapse="{it["collapse"]}"{"" if it["required"] else " data-courtesy=1"}>{inner}</li>')
    return ('<div class="gm-credits" id="gm-credits" role="contentinfo" aria-label="Map credits"><ul class="gm-cr-list">' + "".join(lis) + "</ul>"
            '<button type="button" class="gm-cr-info" id="gm-cr-info" aria-expanded="false" aria-controls="gm-cr-pop" aria-label="Credits and data licences">'
            + icon("info") + "</button></div>"
            '<div class="gm-pop" id="gm-cr-pop" role="dialog" aria-label="Credits and data licences" hidden></div>')


def tabs_html(tabs: list[str], default: str) -> str:
    names = {"story": "Story", "legend": "Legend", "layers": "Layers", "details": "Details", "about": "About"}
    out = []
    for i, t in enumerate(tabs):
        sel = t == default
        out.append(f'<button type="button" role="tab" class="gm-tab" id="gm-tab-{t}" aria-controls="gm-tp-{t}" aria-selected="{"true" if sel else "false"}" '
                   f'tabindex="{0 if sel else -1}" data-tab="{t}" title="{names[t]} ({i + 1})">{icon(t)}<span>{names[t]}</span></button>')
    return f'<div class="gm-tabs" role="tablist" aria-label="Panel">{"".join(out)}</div>'


def rail_html(fc: dict, *, three_d: bool, engine: str) -> str:
    c = fc["controls"]
    g1 = ""
    if c["zoom"] not in ("off", False):
        g1 = (f'<button type="button" class="gm-rb gm-zoom" id="gm-zoom-in" title="Zoom in (+)" aria-label="Zoom in">{icon("plus")}</button>'
              f'<button type="button" class="gm-rb gm-zoom" id="gm-zoom-out" title="Zoom out (-)" aria-label="Zoom out">{icon("minus")}</button>')
    groups = [g1] if g1 else []
    if c["compass"] and engine == "maplibre" and three_d:
        groups.append(f'<button type="button" class="gm-rb" id="gm-compass" title="Back to north and flat" aria-label="Turn the map back to north and flat" hidden>'
                      f'<span class="gm-needle">{icon("compass")}</span></button>')
    if c["dimension"] and three_d and engine == "maplibre":
        groups.append('<button type="button" class="gm-rb gm-3d-btn" id="gm-3d" aria-pressed="true" title="Show the map flat (2D)" aria-label="Show the map flat, from above (2D)">2D</button>')
    groups.append(f'<button type="button" class="gm-rb" id="gm-reset" title="Reset view (0)" aria-label="Reset the view to the full map">{icon("home")}</button>')
    if c["fullscreen"]:
        groups.append(f'<button type="button" class="gm-rb" id="gm-fullscreen" title="Full screen (f)" aria-label="Toggle full screen">{icon("full")}</button>')
    inner = '<span class="gm-rdiv" aria-hidden="true"></span>'.join(f'<div class="gm-rg">{g}</div>' for g in groups)
    return f'<div class="gm-rail" id="gm-rail" role="toolbar" aria-label="Map controls" aria-orientation="vertical">{inner}</div>'


def dock_html(fc: dict, time_cfg: dict, display: list[str], start: int, where: str = "map") -> str:
    pb = fc["playback"]
    n = len(time_cfg["labels"])
    full = pb["mode"] == "full" and time_cfg.get("play", True)
    btns = ""
    if full:
        btns = (f'<button type="button" class="gm-play" id="gm-time-play" aria-pressed="false" aria-label="Play through the periods (Space)" title="Play (Space)">'
                f'{icon("play")}</button><button type="button" class="gm-db" id="gm-time-back" aria-label="One period back" title="Back one period">{icon("prev")}</button>'
                f'<button type="button" class="gm-db" id="gm-time-fwd" aria-label="One period forward" title="Forward one period">{icon("next")}</button>')
    ticks = n <= 60
    tl = ""
    if ticks:
        tl = '<datalist id="gm-time-ticks">' + "".join(f'<option value="{k}"></option>' for k in range(n)) + "</datalist>"
    tail = ""
    if full:
        sp = "".join(f'<option value="{s:g}"{" selected" if s == 1 else ""}>{s:g}×</option>' for s in pb["speeds"])
        tail = (f'<label class="gm-speed"><span class="gm-sr">Speed</span><select id="gm-time-speed" aria-label="Playback speed">{sp}</select></label>'
                f'<button type="button" class="gm-db" id="gm-time-loop" aria-pressed="{"true" if pb.get("loop") else "false"}" aria-label="Loop" title="Loop">{icon("loop")}</button>')
    lab = esc(display[start] if start < len(display) else "")
    return (f'<section class="gm-dock gm-dock-{where}" id="gm-time" aria-label="Time">{btns}<div class="gm-track"><input id="gm-time-range" type="range" min="0" max="{n - 1}" step="1" '
            f'value="{start}"{" list=gm-time-ticks" if ticks else ""} aria-label="Period" aria-valuetext="{lab}"><div class="gm-marks" id="gm-time-marks"></div>{tl}</div>'
            f'<output id="gm-time-label" for="gm-time-range">{lab}</output>{tail}</section>')


def css(t: dict, width: int = 360) -> str:
    ink = t["background"] if contrast(t["background"], t["emphasis"]) >= contrast(t["label"], t["emphasis"]) else t["label"]
    vals = {"bg": t["background"], "label": t["label"], "panel": t["legend_bg"], "line": t["legend_frame"], "frame": t["frame"], "accent": t["emphasis"],
            "ink": ink, "water": t["water"]}
    out = FRAME_CSS.replace("@@w@@", str(int(width)))
    for k, v in vals.items():
        out = out.replace(f"@@{k}@@", v)
    assert "@@" not in out
    return out


FRAME_CSS = """
#gm-root.gm-frame{display:flex;flex-direction:row;overflow:hidden;--gm-pw:@@w@@px;--gm-r:6px;--gm-ease:180ms ease-out}
#gm-root.gm-frame.gm-side-right{flex-direction:row-reverse}
#gm-stage{position:relative;flex:1 1 auto;min-width:0;height:100%;overflow:hidden;background:@@water@@}
#gm-stage #gm-map{position:absolute;inset:0;z-index:0}
#gm-stage #gm-fallback{z-index:1}
.gm-i{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
#gm-panel{position:relative;z-index:30;flex:none;width:var(--gm-pw);height:100%;display:flex;flex-direction:column;background:@@panel@@;color:@@label@@;
  border-right:1px solid @@line@@;transition:width var(--gm-ease),margin var(--gm-ease);overflow:hidden}
.gm-side-right #gm-panel{border-right:0;border-left:1px solid @@line@@}
.gm-collapsed #gm-panel,.gm-nopanel #gm-panel{width:0;border:0}
.gm-collapsed #gm-panel>*,.gm-nopanel #gm-panel>*{visibility:hidden}
.gm-ph{flex:none;display:flex;gap:10px;align-items:flex-start;padding:14px 12px 10px 16px;border-bottom:1px solid @@line@@}
.gm-ph .gm-logo{flex:none;margin-top:2px}
.gm-ph-t{flex:1 1 auto;min-width:0}
.gm-ph h1{margin:0;font-size:17px;line-height:1.3;font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gm-ph p{margin:2px 0 0;font-size:13px;line-height:1.4}.gm-ph .gm-tagline{font-size:12px;font-style:italic}
.gm-ph-btns{flex:none;display:flex;gap:2px;margin:-4px -4px 0 0}
.gm-ib,.gm-rb,.gm-db{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;padding:0;border:0;border-radius:var(--gm-r);background:transparent;
  color:@@label@@;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.gm-ib:hover,.gm-rb:hover,.gm-db:hover{background:@@bg@@}
.gm-tb{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border:1px solid @@line@@;border-radius:var(--gm-r);background:@@panel@@;color:@@label@@;
  font:inherit;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap}
.gm-tb:hover{border-color:@@accent@@}
#gm-root button:focus-visible,#gm-root a:focus-visible,#gm-root input:focus-visible,#gm-root select:focus-visible,#gm-root [tabindex]:focus-visible{outline:2px solid @@accent@@;outline-offset:2px}
.gm-search-box{flex:none;position:relative;padding:8px 12px;border-bottom:1px solid @@line@@}
.gm-search-box input{width:100%;height:32px;font:inherit;font-size:13px;color:@@label@@;background:@@bg@@;border:1px solid @@line@@;border-radius:var(--gm-r);padding:0 8px}
#gm-search-results{position:absolute;z-index:40;left:12px;right:12px;top:100%;margin:0;padding:0;list-style:none;background:@@panel@@;border:1px solid @@line@@;border-radius:var(--gm-r);max-height:240px;overflow:auto;box-shadow:0 2px 8px @@frame@@}
#gm-search-results button{display:block;width:100%;text-align:left;border:0;background:transparent;color:@@label@@;font:inherit;font-size:13px;padding:7px 9px;min-height:32px;cursor:pointer}
#gm-search-results button:hover,#gm-search-results button:focus-visible{background:@@bg@@}
.gm-tabs{flex:none;display:flex;height:40px;border-bottom:1px solid @@line@@;padding:0 8px}
.gm-tab{flex:1 1 0;min-width:0;display:inline-flex;align-items:center;justify-content:center;gap:5px;border:0;border-bottom:2px solid transparent;background:transparent;
  color:@@label@@;font:inherit;font-size:12px;font-weight:600;letter-spacing:.02em;cursor:pointer;padding:0 4px}
.gm-tab[aria-selected=true]{border-bottom-color:@@accent@@}
.gm-tab .gm-i{width:16px;height:16px}
.gm-pbody{flex:1 1 auto;min-height:0;overflow:auto;padding:12px 16px 16px;overscroll-behavior:contain}
.gm-tp h2,.gm-h{margin:14px 0 6px;font-size:12px;font-weight:600;font-variant:small-caps;letter-spacing:.04em;text-transform:lowercase}
.gm-tp h2:first-child,.gm-h:first-child{margin-top:0}
.gm-tp p{margin:0 0 8px;font-size:13px;line-height:1.45}
.gm-tp ul{list-style:none;margin:0;padding:0}
.gm-pfoot{flex:none;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;border-top:1px solid @@line@@}
.gm-pfoot:empty{display:none}
.gm-dots{display:flex;gap:6px;align-items:center}
.gm-dots{gap:0}
.gm-dot{position:relative;width:32px;height:32px;border:0;background:transparent;padding:0;cursor:pointer;border-radius:50%}
.gm-dot::before{content:"";position:absolute;left:12px;top:12px;width:8px;height:8px;border-radius:50%;border:1px solid @@label@@;box-sizing:border-box}
.gm-dot[aria-current=step]::before{background:@@accent@@;border-color:@@accent@@}
.gm-tb:disabled{opacity:.4;cursor:default;border-color:@@line@@}
.gm-about{margin:0 0 12px;font-size:13px;line-height:1.45}
.gm-about.gm-clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gm-more{border:0;background:none;color:@@accent@@;font:inherit;font-size:12px;font-weight:600;padding:0 4px;min-height:32px;min-width:32px;cursor:pointer;text-decoration:underline}
.gm-chapter{position:relative;margin:0 0 8px;padding:12px 16px;border:1px solid @@line@@;border-radius:var(--gm-r);background:@@bg@@;cursor:pointer}
.gm-chapter.gm-active{border-left:3px solid @@accent@@;padding-left:14px;cursor:default}
.gm-ch-row{display:flex;gap:8px;align-items:baseline}
.gm-ch-n{flex:none;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.gm-chapter h3{margin:0;font-size:14px;line-height:1.35;font-weight:600}
.gm-ch-chip{display:inline-block;margin:6px 0 4px;padding:1px 8px;border:1px solid @@line@@;border-radius:999px;font-size:11.5px;font-variant-numeric:tabular-nums}
.gm-ch-body{margin-top:6px}.gm-ch-body p{margin:0 0 6px;font-size:13px;line-height:1.45}
.gm-cards-compact .gm-chapter:not(.gm-active) .gm-ch-body,.gm-cards-compact .gm-chapter:not(.gm-active) .gm-ch-chip{display:none}
.gm-ch-kpi dl{margin:6px 0 0;display:grid;grid-template-columns:auto auto;gap:2px 10px;font-size:12px}.gm-ch-kpi dd{margin:0;text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.gm-story-spacer{height:0}
.gm-lg{margin:0 0 12px}.gm-lg-head{display:flex;align-items:center;gap:6px}.gm-lg h3{flex:1 1 auto;margin:0 0 4px;font-size:12px;font-weight:600}
.gm-lg.gm-off>*:not(.gm-lg-head){opacity:.45}.gm-lg-hidden{font-size:11.5px;font-style:italic}
.gm-eye{width:32px;height:32px}
.gm-lg li{display:flex;align-items:center;gap:8px;font-size:12px;padding:1px 0}
.gm-lrow{display:flex;align-items:center;gap:8px;min-height:32px;font-size:13px}
.gm-lrow label{flex:1 1 auto;display:flex;align-items:center;gap:8px;cursor:pointer;min-width:0}
.gm-lrow input[type=checkbox],.gm-lrow input[type=radio]{width:16px;height:16px;accent-color:@@accent@@;flex:none}
.gm-lrow .gm-lname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gm-lrow .gm-lsw{width:14px;height:14px;border-radius:3px;border:1px solid @@line@@;flex:none}
.gm-lrow .gm-lsub{font-size:11.5px}
.gm-lop{display:block;width:calc(100% - 24px);margin:0 0 6px 24px;accent-color:@@accent@@}
.gm-menu-btn{width:32px;height:32px;list-style:none}.gm-lmenu>summary::-webkit-details-marker{display:none}
#gm-layer-list .gm-opacity{display:block;width:calc(100% - 24px);margin:0 0 6px 24px;accent-color:@@accent@@}
.gm-range-row{display:flex;align-items:center;gap:8px;font-size:13px;min-height:32px}.gm-range-row input{flex:1 1 auto;accent-color:@@accent@@}
#gm-kpi-what{font-weight:600}
#gm-kpi-dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px}#gm-kpi-dl dt{font-weight:500}#gm-kpi-dl dd{margin:0;text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.gm-src li,.gm-lic li{font-size:12px;line-height:1.45;margin:0 0 4px}
.gm-made{font-size:11.5px}
.gm-keys{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px;margin:0}.gm-keys dt{font-weight:600;font-variant-numeric:tabular-nums}.gm-keys dd{margin:0}
#gm-notes p{font-size:12px}
/* on the map */
.gm-ov{position:absolute;pointer-events:none}.gm-ov>*{pointer-events:auto}
.gm-float{background:@@panel@@;color:@@label@@;border:1px solid @@line@@;border-radius:var(--gm-r);box-shadow:0 1px 4px @@frame@@}
.gm-tl{z-index:10;top:12px;left:12px;display:flex;flex-direction:column;align-items:flex-start;gap:8px;max-width:min(440px,calc(100% - 100px))}
.gm-chipbar{display:none;align-items:center;gap:6px;height:36px;max-width:100%;padding:0 12px 0 4px;border-radius:999px}
.gm-collapsed .gm-chipbar,.gm-nopanel .gm-chipbar{display:inline-flex}
.gm-chipbar .gm-ct{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.gm-chipbar .gm-logo{flex:none}
.gm-caption{display:none;width:min(360px,100%);padding:10px 12px}
.gm-collapsed .gm-caption.gm-has{display:block}
.gm-caption h3{margin:0 0 4px;font-size:13px;font-weight:600}.gm-caption p{margin:0;font-size:13px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.gm-cap-nav{display:flex;justify-content:space-between;margin-top:6px}
.gm-tr{z-index:20;top:12px;right:12px;display:flex;flex-direction:row-reverse;align-items:flex-start;gap:8px}
.gm-rail{display:flex;flex-direction:column;background:@@panel@@;border:1px solid @@line@@;border-radius:var(--gm-r);box-shadow:0 1px 4px @@frame@@;transition:opacity 400ms}
.gm-rg{display:flex;flex-direction:column}.gm-rdiv{display:block;height:1px;background:@@line@@}
.gm-rb.gm-3d-btn{font-size:12px;font-weight:700}
.gm-needle{display:inline-flex;transition:transform 120ms linear}
.gm-rail.gm-faded{opacity:0}
#gm-flegend{z-index:10;max-width:280px;max-height:50%;overflow:auto;padding:8px 12px}
#gm-flegend[hidden]{display:none}
.gm-bottom{z-index:20;left:0;right:0;bottom:0;height:0}
#gm-brand{position:absolute;left:8px;bottom:8px}
.gm-brand{display:inline-flex;align-items:center;height:28px;border-radius:999px;background:color-mix(in srgb,@@panel@@ 92%,transparent);color:@@label@@;
  border:1px solid @@line@@;padding:0 10px 0 4px;font-size:12px;font-weight:600;white-space:nowrap}
.gm-brand a,.gm-brand-main,.gm-brand-co{display:inline-flex;align-items:center;gap:6px;color:inherit;text-decoration:none;height:100%}
.gm-brand-logo{display:block;flex:none;border-radius:4px}
.gm-brand-markonly{padding:0;height:auto;background:none;border:0;box-shadow:none;border-radius:0}   /* 1.6.3: the logo alone, as in print */
.gm-brand-markonly .gm-brand-logo{border-radius:0}
.gm-brand-div{width:1px;height:16px;background:@@line@@;margin:0 8px}
.gm-scale-host{position:absolute;bottom:8px}
#gm-time.gm-dock-map{position:absolute;left:50%;transform:translateX(-50%);bottom:8px;width:min(720px,calc(100% - 32px));min-width:min(360px,calc(100% - 32px))}
.gm-dock{display:flex;align-items:center;gap:6px;height:48px;padding:0 10px 0 6px;background:@@panel@@;color:@@label@@;border:1px solid @@line@@;border-radius:var(--gm-r);box-shadow:0 1px 4px @@frame@@}
.gm-dock-panel{box-shadow:none;border:0;padding:0;width:100%}
.gm-play{flex:none;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:50%;border:0;background:@@accent@@;color:@@ink@@;cursor:pointer}
.gm-play .gm-i{width:16px;height:16px}
.gm-track{position:relative;flex:1 1 auto;min-width:80px;display:flex;align-items:center}
.gm-track input{width:100%;height:32px;margin:0;accent-color:@@accent@@}
.gm-dock .gm-db{flex:none}
.gm-cap-nav .gm-tb{min-width:32px;justify-content:center}
.gm-marks{position:absolute;left:8px;right:8px;top:-2px;height:6px;pointer-events:none}
.gm-mark{position:absolute;top:0;width:2px;height:6px;margin-left:-1px;background:@@accent@@;pointer-events:auto}
#gm-time-label{flex:none;min-width:64px;text-align:right;font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.gm-speed select{height:32px;font:inherit;font-size:12px;color:@@label@@;background:@@panel@@;border:1px solid @@line@@;border-radius:var(--gm-r);padding:0 4px}
#gm-time-loop[aria-pressed=true]{color:@@accent@@;background:@@bg@@}
#gm-credits{position:absolute;right:0;bottom:0;z-index:25;display:flex;align-items:flex-end;gap:2px;max-width:60%;padding:2px 2px 2px 6px;
  background:color-mix(in srgb,@@panel@@ 88%,transparent);color:@@label@@;font-size:11.5px;line-height:1.35;border-top-left-radius:var(--gm-r)}
.gm-cr-list{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin:0;padding:2px 0;list-style:none}
.gm-cr{display:inline}.gm-cr+.gm-cr::before{content:" \\00b7 "}.gm-cr[hidden]{display:none}
.gm-cr-logo-wrap{display:inline-block;vertical-align:-1px;margin-right:4px;line-height:0;color:inherit}.gm-cr-logo{display:inline-block}
.gm-cr a{color:inherit;text-decoration:none}.gm-cr a:hover,.gm-cr a:focus-visible{text-decoration:underline}
.gm-cr-info{flex:none;width:32px;height:32px;margin:-6px -2px -2px 0;display:inline-flex;align-items:center;justify-content:center;border:0;background:transparent;color:@@label@@;padding:0;cursor:pointer;border-radius:50%}
.gm-cr-info .gm-i{width:16px;height:16px}
.gm-pop{position:absolute;z-index:40;right:8px;bottom:40px;width:min(380px,calc(100% - 16px));max-height:60%;overflow:auto;padding:12px 14px;background:@@panel@@;color:@@label@@;
  border:1px solid @@line@@;border-radius:8px;box-shadow:0 2px 10px @@frame@@;font-size:12.5px;line-height:1.45}
.gm-pop h2{margin:0 0 6px;font-size:12px;font-weight:600}.gm-pop ul{margin:0 0 10px;padding:0 0 0 16px}.gm-pop a{color:@@accent@@}
#gm-provider-logo{position:absolute;left:8px;bottom:8px;line-height:0}
#gm-provider-logo img{display:block}
#gm-provider-logo .gm-wordmark-text{line-height:1.2;font-weight:700;font-size:13px;color:@@label@@;background:@@panel@@;padding:2px 6px;border-radius:3px}
.gm-menu{position:absolute;z-index:40;min-width:200px;padding:4px;background:@@panel@@;color:@@label@@;border:1px solid @@line@@;border-radius:var(--gm-r);box-shadow:0 2px 10px @@frame@@}
.gm-menu button{display:flex;align-items:center;gap:8px;width:100%;height:32px;padding:0 10px;border:0;border-radius:4px;background:transparent;color:@@label@@;font:inherit;font-size:13px;text-align:left;cursor:pointer}
.gm-menu button:hover,.gm-menu button:focus-visible{background:@@bg@@}
.gm-menu label{display:flex;align-items:center;gap:8px;font-size:13px;padding:4px 10px}.gm-menu input[type=range]{width:120px;accent-color:@@accent@@}
#gm-banners{position:absolute;z-index:50;left:50%;top:8px;transform:translateX(-50%);width:min(520px,calc(100% - 24px));display:flex;flex-direction:column;gap:4px;pointer-events:none}
#gm-measure{position:absolute;z-index:40;top:12px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:8px;height:40px;padding:0 10px;
  background:@@panel@@;color:@@label@@;border:1px solid @@line@@;border-radius:var(--gm-r);box-shadow:0 1px 4px @@frame@@;font-size:13px;white-space:nowrap}
#gm-measure output{font-weight:600;font-variant-numeric:tabular-nums}
#gm-measure button{height:32px;padding:0 10px;border:1px solid @@line@@;border-radius:var(--gm-r);background:@@panel@@;color:@@label@@;font:inherit;font-size:13px;font-weight:500;cursor:pointer}
#gm-help{position:absolute;z-index:60;left:50%;top:50%;transform:translate(-50%,-50%);width:min(360px,calc(100% - 32px));padding:14px 16px;background:@@panel@@;color:@@label@@;border:1px solid @@line@@;border-radius:8px;box-shadow:0 2px 12px @@frame@@}
.gm-cmp-tag{top:12px!important}
.gm-sheet-handle{display:none}
.gm-brand-sheet{display:none}
/* tablet: the panel is a drawer over the map */
@media (min-width:640px) and (max-width:1023px){
  #gm-panel{position:absolute;left:0;top:0;bottom:0;width:320px;box-shadow:0 2px 12px @@frame@@}
  .gm-side-right #gm-panel{left:auto;right:0}
}
/* phone: the panel is a bottom sheet (peek, half, full); the map is the space above the peek */
@media (max-width:639px){
  #gm-root.gm-frame{display:block}
  #gm-stage{position:absolute;left:0;right:0;top:0;bottom:var(--gm-peek,120px);height:auto}
  .gm-nopanel #gm-stage{bottom:0}
  #gm-panel{position:absolute;left:0;right:0;bottom:0;width:auto;height:var(--gm-peek,120px);border:0;border-top:1px solid @@line@@;border-radius:12px 12px 0 0;
    box-shadow:0 -2px 10px @@frame@@;transition:height var(--gm-ease)}
  .gm-collapsed #gm-panel{width:auto;height:var(--gm-peek,120px);border-top:1px solid @@line@@}
  .gm-collapsed #gm-panel>*{visibility:visible}
  #gm-root[data-sheet=half] #gm-panel{height:55%}
  #gm-root[data-sheet=full] #gm-panel{height:calc(100% - 48px)}
  #gm-root[data-sheet=half] #gm-stage{bottom:55%}
  #gm-root[data-sheet=full] #gm-stage{bottom:calc(100% - 48px)}
  .gm-sheet-handle{display:flex;flex:none;align-items:flex-start;justify-content:center;width:100%;height:44px;margin-bottom:-28px;padding:8px 0 0;border:0;background:transparent;color:@@label@@;cursor:pointer;position:relative;z-index:1}
  #gm-root[data-sheet=full] .gm-tr{display:none}
  .gm-dot{width:44px;height:44px}.gm-dot::before{left:18px;top:18px}
  .gm-sheet-handle span{width:40px;height:4px;border-radius:2px;background:@@line@@}
  .gm-ph{padding:2px 12px 6px;border:0;align-items:center}
  .gm-ph .gm-logo,.gm-ph p,#gm-collapse{display:none}
  .gm-ph h1{font-size:15px;-webkit-line-clamp:1}
  .gm-ph-t{display:flex;align-items:center;gap:6px;min-width:0}
  .gm-pfoot{flex-wrap:wrap;row-gap:6px;padding:6px 10px}
  .gm-pfoot .gm-dock{order:-1;flex:1 1 100%;height:44px}
  .gm-brand-sheet{display:inline-flex;height:24px;margin-right:4px}
  #gm-brand{display:none}
  #gm-root[data-sheet=peek] .gm-tabs,#gm-root[data-sheet=peek] .gm-pbody,#gm-root[data-sheet=peek] .gm-search-box{display:none}
  #gm-root[data-sheet=peek] .gm-pfoot{border:0}
  .gm-collapsed .gm-chipbar,.gm-collapsed .gm-caption.gm-has{display:none}
  .gm-rb,.gm-ib,.gm-db{width:44px;height:44px}.gm-play{width:44px;height:44px}
  .gm-tab span{display:none}.gm-tab{min-height:44px}
  .gm-lrow{min-height:44px}
  #gm-credits{max-width:80%}
  .gm-tl{display:none}
}
@media (pointer:coarse){.gm-cr-info{width:44px;height:44px;margin:-12px -6px -6px 0}.gm-speed select{height:44px}.gm-more{min-height:44px}.gm-rb,.gm-ib,.gm-db{width:44px;height:44px}.gm-play{width:44px;height:44px}.gm-tb{height:44px}.gm-lrow{min-height:44px}.gm-zoom{display:none!important}}
@media (prefers-reduced-motion:reduce){#gm-panel{transition:none}}
@media print{
  /* page 1: the view with the title, the legend of the visible layers, the brand chip and the credits in full; no tools (spec 13.1) */
  #gm-root.gm-frame{display:block}
  #gm-panel,.gm-rail,#gm-time,.gm-caption,.gm-cr-info,#gm-cr-pop,.gm-menu,#gm-help,.gm-chipbar button,#gm-measure{display:none!important}
  .gm-chipbar{display:inline-flex!important;box-shadow:none}
  .gm-tl{display:flex!important}#gm-brand{display:inline-flex!important}.gm-brand-sheet{display:none!important}
  #gm-flegend{display:block!important;box-shadow:none;max-height:none}
  #gm-flegend .gm-lg.gm-off{display:none}
  #gm-stage{position:relative;height:185mm}
  #gm-credits{max-width:70%;background:@@panel@@}
  .gm-cr{display:inline!important}
}
"""


# ---------------------------------------------------------------------------------------------------------------- the tabs
def legend_block(b: dict, *, toggles: bool, legend_html: str) -> str:
    """A legend block in the Legend tab: the print legend's block with an eye toggle (synchronised with the Layers tab); a hidden layer's
    block stays, greyed and marked hidden, so it can be turned back on here."""
    if not toggles or not b.get("layer"):
        return legend_html
    lid = esc(b["layer"])
    eye = (f'<button type="button" class="gm-ib gm-eye" data-eye="{lid}" aria-pressed="true" aria-label="Show or hide {esc(b.get("title") or b["layer"])}" '
           f'title="Show or hide">{icon("eye")}</button>')
    m = re.match(r'(<section class="gm-lg" data-legend-for="[^"]*"[^>]*>)(<h3>.*?</h3>)?', legend_html, re.S)
    if not m:
        return legend_html
    head = f'<div class="gm-lg-head">{m.group(2) or "<h3></h3>"}{eye}</div><p class="gm-lg-hidden" hidden>Hidden</p>'
    return m.group(1) + head + legend_html[m.end():]


def story_tab(story: dict, *, message: str, cards: str, time_display: list | None, panel_cfg: dict | None) -> str:
    out = [f'<p class="gm-about gm-clamp" id="gm-about-text">{esc(message)}</p><button type="button" class="gm-more" id="gm-about-more" aria-expanded="false" '
           f'aria-controls="gm-about-text">More</button>',
           f'<ol class="gm-chapters gm-cards-{"full" if cards == "full" else "compact"}" style="list-style:none;margin:12px 0 0;padding:0">']
    for i, ch in enumerate(story["chapters"]):
        chip = ""
        if ch.get("time") is not None and ch["time"] >= 0 and time_display:
            chip = f'<span class="gm-ch-chip">{esc(time_display[ch["time"]])}</span>'
        body = "".join(f"<p>{esc(p.strip())}</p>" for p in str(ch.get("text") or "").split("\n\n") if p.strip())
        out.append(f'<li class="gm-chapter" id="{ch["id"]}" tabindex="0" role="button" aria-expanded="false" aria-labelledby="{ch["id"]}-h">'
                   f'<div class="gm-ch-row"><span class="gm-ch-n">{i + 1}</span><h3 id="{ch["id"]}-h">{esc(ch["title"])}</h3></div>{chip}'
                   f'<div class="gm-ch-body">{body}{"<div class=gm-ch-kpi></div>" if ch.get("select") else ""}</div></li>')
    out.append('</ol><div class="gm-story-spacer" aria-hidden="true"></div><p id="gm-story-live" class="gm-sr" aria-live="polite"></p>')
    return "".join(out)


def _swatch(b: dict | None) -> str:
    it = ((b or {}).get("items") or [{}])[0] if (b or {}).get("items") else {}
    fill = it.get("fill") or it.get("stroke")
    if not fill and (b or {}).get("stops"):
        fill = b["stops"][len(b["stops"]) // 2]
    if not fill or not re.match(r"^#[0-9A-Fa-f]{3,8}$", str(fill)):
        return '<span class="gm-lsw" aria-hidden="true"></span>'
    return f'<span class="gm-lsw" style="background:{fill}" aria-hidden="true"></span>'


def layers_tab(defs: list[dict], blocks: list[dict], bases: list[dict], *, names_id: str | None, three_d: dict | None, rows: bool = True) -> str:
    blk = {}
    for b in blocks:
        if b.get("layer") and b["layer"] not in blk:
            blk[b["layer"]] = b
    out = []
    if bases:
        out.append('<h2 class="gm-h">Background</h2><ul id="gm-base-list" aria-label="Background">')
        for i, b in enumerate(bases):
            sub = "" if b.get("mode") == "bundled" else f'<span class="gm-lsub"> · {esc((b.get("host") or "").split(".")[-2] if b.get("host") and b["host"].count(".") else b.get("host") or "")}</span>'
            out.append(f'<li class="gm-lrow"><label><input type="radio" name="gm-base" value="{esc(b["id"])}"{" checked" if i == 0 else ""}> '
                       f'<span class="gm-lname">{esc(b.get("title") or "Bundled background")}</span>{sub}</label></li>')
        out.append("</ul>")
    lrows = []
    if rows:
        for d in defs:
            if d.get("context"):
                continue
            lid = esc(d["id"])
            v = int(round(100 * float(d.get("opacity", 1.0)))) if d["kind"] == "image" else 100
            slider = (f'<input type="range" class="gm-opacity" min="0" max="100" step="5" value="{v}" data-opacity-for="{lid}" aria-label="Opacity of {esc(d["name"])}">')
            inline = d["kind"] == "image" or d.get("d3") == "extrude"           # pictures and buildings keep their slider in the row (spec 5.2)
            menu = ("" if inline else
                    f'<details class="gm-lmenu"><summary class="gm-ib gm-menu-btn" aria-label="More for {esc(d["name"])}" title="More">{icon("more")}</summary>'
                    f'<div class="gm-menu" style="position:static;box-shadow:none;margin:0 0 6px 24px"><label>Opacity {slider}</label>'
                    f'<button type="button" data-zoom-layer="{lid}">Zoom to layer</button></div></details>')
            lrows.append(f'<li><div class="gm-lrow"><label><input type="checkbox" value="{lid}"{" checked" if d["visible"] else ""}> {_swatch(blk.get(d["id"]))}'
                         f'<span class="gm-lname">{esc(d["name"])}</span></label>{menu}</div>{slider if inline else ""}</li>')
    if names_id:
        lrows.append(f'<li><div class="gm-lrow"><label><input type="checkbox" value="{names_id}" checked> <span class="gm-lname">Place names</span></label></div></li>')
    if lrows:
        out.append(f'<h2 class="gm-h">Map layers</h2><ul id="gm-layer-list" aria-label="Layers">{"".join(lrows)}</ul>')
    if three_d and three_d.get("terrain"):
        try:
            ex = float(three_d.get("exaggeration") or 1.0)
        except (TypeError, ValueError):
            ex = 1.0
        out.append('<h2 class="gm-h">3D</h2>'
                   '<div class="gm-lrow"><label><input type="checkbox" id="gm-terrain" checked> <span class="gm-lname">Terrain</span></label></div>'
                   f'<div class="gm-range-row"><label for="gm-exag">Exaggeration</label><input type="range" id="gm-exag" min="1" max="3" step="0.1" value="{max(1.0, min(3.0, ex)):.1f}">'
                   f'<output id="gm-exag-v">{max(1.0, min(3.0, ex)):.1f}×</output></div>')
    return "".join(out)


def about_tab(plan, *, message: str, sources_line: str, items: list[dict], notes_lines: list[str], base_defs: list[dict], brand: dict, house: bool,
              made: str, date: str, shortcuts: bool) -> str:
    out = [f'<p>{esc(message)}</p>', '<h2 class="gm-h">Sources</h2>']
    if sources_line:
        out.append(f'<p id="gm-sources-line">{esc(sources_line.rstrip("."))}.</p>')
    rows = []
    for l in plan.layers:
        p = l.provenance or {}
        lic = p.get("license") or ((l.entry or {}).get("license") or {}).get("class")
        bits = [x for x in (p.get("source") or ((l.entry or {}).get("source") or {}).get("publisher"), p.get("vintage"), lic,
                            ("retrieved " + str(p.get("retrieved") or p.get("retrieved_on"))) if (p.get("retrieved") or p.get("retrieved_on")) else None) if x]
        rows.append(f'<li><strong>{esc(l.title or l.id)}</strong>: {esc(", ".join(map(str, bits)) or "source not recorded")}</li>')
    for b in base_defs:
        if b.get("mode") != "bundled":
            live = " (loaded live by the reader\u2019s browser)" if b.get("mode") == "tiles" else ""
            rows.append(f'<li><strong>Background</strong>: {esc(b.get("title") or b["id"])}{live}</li>')
    if rows:
        out.append(f'<details><summary class="gm-more">Show all sources</summary><ul class="gm-src" id="gm-sources">{"".join(rows)}</ul></details>')
    lic = []
    for it in items:
        for l in it["links"]:
            row = f'<li><a href="{esc(l["href"])}" target="_blank" rel="noopener">{esc(l["text"])}</a></li>'
            if row not in lic:
                lic.append(row)
        if it["full"] and it["full"] != it["corner"]:
            row = f'<li>{esc(it["full"])}</li>'
            if row not in lic:
                lic.append(row)
    if lic:
        out.append(f'<h2 class="gm-h" id="gm-licences">Data licences</h2><ul class="gm-lic">{"".join(lic)}</ul>')
    if notes_lines:
        out.append('<h2 class="gm-h">Notes</h2><div id="gm-notes">' + "".join(f"<p>{esc(x)}</p>" for x in notes_lines) + "</div>")
    made_line = f"{esc(made)} · map made {esc(date)}"
    acc = ""
    if house and brand.get("accreditation"):                # the house credit is visible only on the house brand's own maps (MAP-BRAND-011)
        acc = (f' · <a href="{esc(brand["acc_href"])}" target="_blank" rel="noopener">{esc(brand["accreditation"])}</a>' if brand.get("acc_href")
               else f" · {esc(brand['accreditation'])}")
    out.append(f'<h2 class="gm-h">Made with</h2><p class="gm-made">{made_line}{acc}</p>')
    if shortcuts:
        out.append('<h2 class="gm-h">Keyboard</h2>' + KEYS_DL)
    return "".join(out)


KEYS_DL = ('<dl class="gm-keys"><dt>[</dt><dd>Collapse or open the panel</dd><dt>1–5</dt><dd>Open a panel tab</dd><dt>← →</dt><dd>Previous or next chapter; '
           'a step of time when the time bar has focus</dd><dt>Space</dt><dd>Play or pause</dd><dt>+ −</dt><dd>Zoom</dd><dt>0</dt><dd>Reset the view</dd>'
           '<dt>f</dt><dd>Full screen</dd><dt>?</dt><dd>These keys</dd></dl>')


def markup(*, fc: dict, tabs: list[str], panes: dict, default_tab: str, title: str, subtitle: str, tagline: str, logo: str, small_logo: str, brand: dict,
           items: list[dict], tools: list[str], search: bool, search_field: str | None, names_on: bool, three_d: bool, engine: str, story: dict | None,
           time_cfg: dict | None, tdisp: list | None, float_legend: str, has_wordmark: bool, share: bool) -> dict:
    """The frame's HTML: the panel, the overlays on the map (title chip, caption card, rail, floating legend, brand chip, dock, credits) and the
    menus that float over everything."""
    show_panel = fc["panel"]["show"] != "off"
    more_btn = (f'<button type="button" class="gm-ib" data-menu="tools" aria-haspopup="menu" aria-expanded="false" aria-controls="gm-tools-menu" '
                f'aria-label="More tools" title="More tools">{icon("more")}</button>') if tools else ""
    search_btn = (f'<button type="button" class="gm-ib" id="gm-search-btn" aria-expanded="false" aria-label="Find a place on the map" title="Find">{icon("search")}</button>'
                  if search else "")
    share_btn = (f'<button type="button" class="gm-ib" id="gm-share" aria-label="Copy a link to this view" title="Copy link">{icon("share")}</button>'
                 if share and "copy_link" in tools else "")
    head = (f'<header class="gm-ph">{logo}<div class="gm-ph-t">{brand_chip_html(brand, where="sheet") if brand["show"] != "off" else ""}'
            f'<h1 id="gm-title">{esc(title)}</h1>'
            + (f'<p id="gm-subtitle">{esc(subtitle)}</p>' if subtitle else "") + (f'<p class="gm-tagline">{esc(tagline)}</p>' if tagline else "")
            + f'</div><div class="gm-ph-btns">{search_btn}{share_btn}{more_btn}'
            f'<button type="button" class="gm-ib" id="gm-collapse" aria-controls="gm-panel" aria-expanded="true" aria-label="Collapse the panel ([)" title="Collapse ([)">'
            f'{icon("collapse" if fc["panel"]["side"] == "left" else "expand")}</button></div></header>')
    sbox = ""
    if search:
        what = f"{search_field.replace('_', ' ').lower()} or place" if search_field and names_on else (search_field.replace("_", " ").lower() if search_field else "place name")
        sbox = (f'<div class="gm-search-box" hidden><input id="gm-search" type="search" placeholder="Find by {esc(what)}" aria-label="Find a place on the map" '
                f'autocomplete="off"><ul id="gm-search-results" hidden></ul></div>')
    bodies = "".join(f'<section class="gm-tp" role="tabpanel" id="gm-tp-{k}" aria-labelledby="gm-tab-{k}" tabindex="0"{"" if k == default_tab else " hidden"}>{panes.get(k, "")}</section>'
                     for k in tabs)
    foot = ""
    if story:
        n = len(story["chapters"])
        dots = "".join(f'<button type="button" class="gm-dot" aria-label="Chapter {i + 1}: {esc(c["title"])}" title="{esc(c["title"])}"></button>'
                       for i, c in enumerate(story["chapters"]))
        foot = (f'<button type="button" class="gm-tb" id="gm-ch-prev" aria-label="Previous chapter">‹ Previous</button><div class="gm-dots" role="group" '
                f'aria-label="Chapters, {n} in all">{dots}</div><button type="button" class="gm-tb" id="gm-ch-next" aria-label="Next chapter">Next ›</button>')
    dock = ""
    pb = fc["playback"]
    if time_cfg and pb["on"]:
        start = max(0, min(len(time_cfg["labels"]) - 1, int(time_cfg.get("start") or 0)))
        dock = dock_html(fc, time_cfg, tdisp or time_cfg["labels"], start, where="panel" if pb["position"] == "panel" else "map")
    if dock and pb["position"] == "panel" and show_panel:
        foot = f'<div style="display:flex;flex-direction:column;gap:8px;width:100%"><div style="display:flex;justify-content:space-between;align-items:center">{foot}</div>{dock}</div>' if foot else dock
        dock = ""
    panel = ""
    if show_panel:
        panel = (f'<aside id="gm-panel" role="complementary" aria-label="Map panel">'
                 f'<button type="button" class="gm-sheet-handle" id="gm-handle" aria-controls="gm-panel" aria-expanded="false" aria-label="Panel: peek. Tap to change its height"><span></span></button>'
                 f'{head}{sbox}{tabs_html(tabs, default_tab) if len(tabs) > 1 else ""}<div class="gm-pbody">{bodies}</div><footer class="gm-pfoot" id="gm-pfoot">{foot}</footer></aside>')
    chip_ok = fc.get("title_chip", "auto") not in ("off", False)
    leg_btn = (f'<button type="button" class="gm-ib" id="gm-flegend-btn" aria-expanded="false" aria-controls="gm-flegend" aria-label="Legend" title="Legend">{icon("legend")}</button>'
               if float_legend else "")
    chip = ""
    if chip_ok:
        exp = (f'<button type="button" class="gm-ib" id="gm-expand" aria-controls="gm-panel" aria-expanded="false" aria-label="Open the panel ([)" title="Open the panel ([)">'
               f'{icon("expand" if fc["panel"]["side"] == "left" else "collapse")}</button>') if show_panel else ""
        chip = (f'<div class="gm-chipbar gm-float" title="{esc(title)}">{exp}{small_logo}<span class="gm-ct">{esc(title)}</span>{leg_btn}{more_btn if not show_panel else ""}</div>')
    caption = ""
    if story and show_panel:
        caption = ('<div class="gm-caption gm-float" id="gm-caption" aria-live="polite"><h3 id="gm-cap-title"></h3><p id="gm-cap-text"></p><div class="gm-cap-nav">'
                   '<button type="button" class="gm-tb" id="gm-cap-prev" aria-label="Previous chapter">‹</button><button type="button" class="gm-tb" id="gm-cap-next" '
                   'aria-label="Next chapter">›</button></div></div>')
    flegend = f'<div class="gm-float" id="gm-flegend" role="region" aria-label="Legend" hidden>{float_legend.strip()}</div>' if float_legend else ""
    wm_slot = '<div id="gm-wordmark" hidden></div>' if has_wordmark else ""
    overlays = (f'<div class="gm-ov gm-tl">{chip}{caption}</div>'
                f'<div class="gm-ov gm-tr">{rail_html(fc, three_d=three_d, engine=engine)}{flegend}</div>'
                f'<div class="gm-ov gm-bottom">{wm_slot}{brand_chip_html(brand)}{dock}{credits_html(items)}</div>')
    names = {"measure": ("gm-measure-btn", "Measure", "Measure a distance or an area on the map", ' aria-pressed="false"'),
             "save_image": ("gm-export", "Save image", "Save the current view as a picture with its title, legend and credits", ""),
             "print": ("gm-print", "Print", "Print this map", ""), "copy_link": ("gm-copy-link", "Copy link", "Copy a link to this view", ""),
             "download": ("gm-download", "Download data", "Download the map data as GeoJSON", "")}
    mitems = "".join(f'<button type="button" role="menuitem" id="{names[t][0]}" aria-label="{names[t][2]}"{names[t][3]}>{names[t][1]}</button>' for t in tools if t in names)
    if fc.get("shortcuts"):
        mitems += '<button type="button" role="menuitem" id="gm-keys">Keyboard shortcuts</button>'
    menus = (f'<div class="gm-menu" id="gm-tools-menu" role="menu" aria-label="Tools" hidden>{mitems}</div>' if mitems else "")
    if fc.get("shortcuts"):
        menus += (f'<div id="gm-help" role="dialog" aria-label="Keyboard shortcuts" hidden><h2 class="gm-h">Keyboard shortcuts</h2>{KEYS_DL}'
                  f'<button type="button" class="gm-tb" id="gm-help-close" style="margin-top:10px">Close</button></div>')
    return {"panel": panel, "overlays": overlays, "menus": menus}
