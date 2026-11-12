#!/usr/bin/env python3
"""web_layout_check: the web map page's frame checked in a headless browser (1.1.0; spec-webmap-frame-ux section 15.2).

    python3 web_layout_check.py page.html [--out DIR] [--json]

The page is opened at 1440x900, 1024x768 and 390x844 (the last as a touch screen) with the Chromium the build already uses for fallback
pictures. In each state (panel open; panel collapsed; each story chapter; playing; the phone's sheet at peek, half and full) every frame
element's box is measured, and:

  MAP-WEB-034 BLOCK  a frame element covers the credits corner or a provider logo: the element found at each corner of the credits box
                     (and of the logo) must be the credits box (the logo) itself
  MAP-WEB-035 WARN   two frame elements overlap (rail, dock, brand chip, credits, logo, title chip, caption card, floating legend, scale bar)
  MAP-WEB-033 WARN   the credits text is under 11 px, or under 4.5:1 on its own ground (its ground mixed over the map's background)
  MAP-WEB-038 WARN   the active story card is more than 40 percent empty
  MAP-WEB-039 WARN   the map is under 75 percent of a 1440 x 900 window with the panel open, or under 60 percent of a phone at the sheet's peek
  MAP-WEB-040 WARN   a frame control is under 32 px on a desktop or 44 px on the touch screen

It writes layout_report.json and one screenshot per size and state into --out. Without Playwright and Chromium the check is skipped with a
note (a SOFT dependency, like the fallback picture). It never fetches: a page's tile requests are answered empty.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SIZES = [(1440, 900, "desktop"), (1024, 768, "desktop-small"), (390, 844, "phone")]
FRAME = {"rail": "#gm-rail", "dock": "#gm-time.gm-dock-map", "brand": "#gm-brand", "credits": "#gm-credits", "logo": "#gm-wordmark:not([hidden])",
         "chip": ".gm-chipbar", "caption": "#gm-caption.gm-has", "flegend": "#gm-flegend:not([hidden])",
         "scale": ".maplibregl-ctrl-bottom-left .maplibregl-ctrl-scale, .leaflet-bottom.leaflet-left .leaflet-control-scale"}
COVER_SAFE = {"credits", "logo"}

JS_MEASURE = """(frame) => {
  const out = {boxes: {}, credits: null, logo: null, targets: [], card: null};
  const vis = (el) => { if (!el) return false; const s = getComputedStyle(el); if (s.display === 'none' || s.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  for (const [k, sel] of Object.entries(frame)) {
    const el = document.querySelector(sel);
    if (vis(el)) { const r = el.getBoundingClientRect(); out.boxes[k] = [r.left, r.top, r.right, r.bottom]; }
  }
  const stage = document.getElementById('gm-stage');
  const sr = stage ? stage.getBoundingClientRect() : null;
  out.stage = sr ? [sr.left, sr.top, sr.right, sr.bottom] : null;
  out.window = [innerWidth, innerHeight];
  const corners = (el) => {
    if (!vis(el)) return null;
    const r = el.getBoundingClientRect(), pts = [[r.left + 1, r.top + 1], [r.right - 1, r.top + 1], [r.left + 1, r.bottom - 1], [r.right - 1, r.bottom - 1]];
    return pts.map(([x, y]) => { const hit = document.elementFromPoint(x, y); return !!hit && (hit === el || el.contains(hit)) ? null : (hit ? (hit.id || hit.className || hit.tagName) : 'outside'); });
  };
  const cr = document.getElementById('gm-credits');
  out.credits = corners(cr);
  out.logo = corners(document.querySelector('#gm-wordmark:not([hidden])'));
  if (vis(cr)) {
    const s = getComputedStyle(cr), li = cr.querySelector('.gm-cr') || cr;
    out.credits_style = {color: getComputedStyle(li).color, bg: s.backgroundColor, ground: stage ? getComputedStyle(stage).backgroundColor : 'rgb(255,255,255)',
                         size: parseFloat(getComputedStyle(li).fontSize)};
  }
  document.querySelectorAll('#gm-root button, #gm-root select, #gm-root summary').forEach((b) => {
    if (!vis(b) || b.closest('.gm-sr') || b.closest('[hidden]') || b.closest('.maplibregl-popup, .leaflet-popup, .gm-chapter, .gm-lg ul')) return;
    if (b.closest('#gm-tools-menu') || b.closest('#gm-help') || b.closest('#gm-cr-pop')) return;
    const r = b.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return;
    out.targets.push({id: b.id || b.className || b.tagName, w: Math.round(r.width), h: Math.round(r.height)});
  });
  const card = document.querySelector('.gm-chapter.gm-active');
  if (vis(card)) {
    let used = 0; for (const c of card.children) { if (vis(c)) used += c.getBoundingClientRect().height; }
    const r = card.getBoundingClientRect(); out.card = {h: r.height, used: used + 24};
  }
  return out;
}"""


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _rgb(s: str) -> tuple:
    """rgb(), rgba() or color(srgb r g b / a) as 0-1 floats (Chromium reports color-mix() results in the last form)."""
    import re
    s = s or ""
    v = [float(x) for x in re.findall(r"[\d.]+", s.replace("srgb", ""))]
    if len(v) < 3:
        return (1.0, 1.0, 1.0, 1.0)
    if s.strip().startswith("color("):
        return (v[0], v[1], v[2], v[3] if len(v) > 3 else 1.0)
    return (v[0] / 255, v[1] / 255, v[2] / 255, v[3] if len(v) > 3 else 1.0)


def _lum(c) -> float:
    f = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c[:3]]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def contrast(fg: str, bg: str, ground: str) -> float:
    b, g, f = _rgb(bg), _rgb(ground), _rgb(fg)
    mix = tuple(b[i] * b[3] + g[i] * (1 - b[3]) for i in range(3))
    la, lb = _lum(f), _lum(mix)
    return round((max(la, lb) + 0.05) / (min(la, lb) + 0.05), 2)


def _overlap(a, b) -> bool:
    return a[0] < b[2] - 1 and b[0] < a[2] - 1 and a[1] < b[3] - 1 and b[1] < a[3] - 1


def judge(m: dict, *, size: str, state: str, touch: bool) -> list[dict]:
    """The findings of one measured state."""
    fs = []
    where = f"{size}, {state}"
    for what in ("credits", "logo"):
        hits = [h for h in (m.get(what) or []) if h]
        if hits:
            fs.append(finding("MAP-WEB-034", "BLOCK", f"{where}: the {'credits corner' if what == 'credits' else 'provider logo'} is covered by {hits[0]}", hits))
    keys = sorted(m["boxes"])
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if not _overlap(m["boxes"][a], m["boxes"][b]) or {a, b} == {"chip", "caption"}:
                continue
            if {a, b} & COVER_SAFE:
                fs.append(finding("MAP-WEB-034", "BLOCK", f"{where}: {a} and {b} overlap: nothing may lie over the credits or a provider logo", [a, b]))
            else:
                fs.append(finding("MAP-WEB-035", "WARN", f"{where}: {a} and {b} overlap", [a, b]))
    cs = m.get("credits_style")
    if cs:
        c = contrast(cs["color"], cs["bg"], cs["ground"])
        if cs["size"] < 11 or c < 4.5:
            fs.append(finding("MAP-WEB-033", "WARN", f"{where}: the credits are {cs['size']:g} px at {c}:1 on their ground (11 px and 4.5:1 at least)", {"size": cs["size"], "contrast": c}))
    if m.get("stage"):
        st, (W, H) = m["stage"], m["window"]
        share = max(0.0, (st[2] - st[0]) * (st[3] - st[1])) / (W * H)
        m["map_share"] = round(share, 3)
        if size == "desktop" and state == "panel open" and share < 0.75:
            fs.append(finding("MAP-WEB-039", "WARN", f"{where}: the map is {share:.0%} of the window (75% at least)", share))
        if size == "phone" and state == "sheet peek" and share < 0.60:
            fs.append(finding("MAP-WEB-039", "WARN", f"{where}: the map is {share:.0%} of the screen at the sheet's peek (60% at least)", share))
    least = 44 if touch else 32
    small = [t for t in m.get("targets") or [] if min(t["w"], t["h"]) < least - 0.5]
    if small:
        fs.append(finding("MAP-WEB-040", "WARN", f"{where}: {len(small)} control(s) under {least} px: " + ", ".join(f"{t['id']} {t['w']}x{t['h']}" for t in small[:5]), small[:10]))
    card = m.get("card")
    if card and card["h"] > 0 and (card["h"] - card["used"]) / card["h"] > 0.40:
        fs.append(finding("MAP-WEB-038", "WARN", f"{where}: the active story card is {(card['h'] - card['used']) / card['h']:.0%} empty", card))
    return fs


def available() -> str | None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "Playwright is not installed"
    return None


def check(page: Path, out_dir: Path | None = None, *, screenshots: bool = True, timeout_ms: int = 90_000) -> dict:
    """-> {"findings": [...], "report": {...}, "skipped": reason or None}"""
    why = available()
    if why:
        return {"findings": [finding("MAP-WEB-039", "INFO", f"the layout check was skipped ({why}); the page is not measured in a browser")], "report": {}, "skipped": why}
    from playwright.sync_api import sync_playwright
    page = Path(page).resolve()
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    rep: dict = {"page": page.name, "sizes": []}
    fs: list[dict] = []
    seen = set()

    def add(items):
        for f in items:
            k = (f["id"], f["message"])
            if k not in seen:
                seen.add(k)
                fs.append(f)
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            for w, h, size in SIZES:
                touch = size == "phone"
                ctx = br.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, has_touch=touch, is_mobile=touch)
                ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(("file:", "data:", "blob:")) else r.fulfill(status=204, body=b""))
                pg = ctx.new_page()
                pg.goto(page.as_uri())
                pg.wait_for_selector("#gm-root.gm-ready", timeout=timeout_ms)
                pg.wait_for_timeout(800)
                states = []

                def snap(state):
                    m = pg.evaluate(JS_MEASURE, FRAME)
                    add(judge(m, size=size, state=state, touch=touch))
                    shot = None
                    if out_dir and screenshots:
                        shot = f"layout_{size}_{state.replace(' ', '-')}.png"
                        pg.screenshot(path=str(out_dir / shot))
                    states.append({"state": state, "boxes": m["boxes"], "map_share": m.get("map_share"), "screenshot": shot})
                has_panel = pg.evaluate("!!document.getElementById('gm-panel')")
                if touch:
                    snap("sheet peek")
                    if has_panel:
                        for s in ("half", "full"):
                            pg.click("#gm-handle")
                            pg.wait_for_timeout(500)
                            snap(f"sheet {s}")
                        pg.click("#gm-handle")
                        pg.wait_for_timeout(400)
                else:
                    snap("panel open" if has_panel else "no panel")
                    n = pg.evaluate("(window.gmapPage && window.gmapPage.story) ? JSON.parse(document.getElementById('gmap-config').textContent).story.chapters.length : 0")
                    for i in range(1, n):
                        pg.evaluate(f"window.gmapPage.story.go({i})")
                        pg.wait_for_timeout(400)
                        snap(f"chapter {i + 1}")
                    if pg.evaluate("!!document.getElementById('gm-time-play')"):
                        pg.click("#gm-time-play")
                        pg.wait_for_timeout(600)
                        snap("playing")
                        pg.click("#gm-time-play")
                    if has_panel:
                        pg.keyboard.press("[")
                        pg.wait_for_timeout(600)
                        snap("panel collapsed")
                rep["sizes"].append({"size": size, "width": w, "height": h, "states": states})
                ctx.close()
            br.close()
    except Exception as ex:  # noqa: BLE001 - a page the browser cannot open is reported, never a crash of the build
        return {"findings": [finding("MAP-WEB-039", "INFO", f"the layout check could not run ({str(ex).splitlines()[0][:160]})")], "report": rep, "skipped": str(ex)[:200]}
    rep["findings"] = fs
    if out_dir:
        (out_dir / "layout_report.json").write_text(json.dumps(rep, indent=2))
    return {"findings": fs, "report": rep, "skipped": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = check(Path(a.page), Path(a.out) if a.out else None)
    if a.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        for f in r["findings"]:
            print(f"{f['severity']:5} {f['id']} {f['message']}")
        print(f"{len(r['findings'])} finding(s)" + (f"; skipped: {r['skipped']}" if r["skipped"] else ""))
    return 3 if any(f["severity"] == "BLOCK" for f in r["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
