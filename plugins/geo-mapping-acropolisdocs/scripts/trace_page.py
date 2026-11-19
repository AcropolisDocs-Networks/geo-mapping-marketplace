#!/usr/bin/env python3
"""trace_page: map-trace's report as one branded page, HTML and PDF (1.18.0).

The same report the acropolisdocs run-trace makes for a session, laid out the same way (a hero with six figures, then one section per
question: what ran, where the clock went, what the hooks saw, rework and the findings ledger, activity by lane as a bubble chart or a
sequence, fan-out, the slowest calls, every failure with its evidence, the fix spec, how it was measured), for this plugin's calls only. It
is this plugin's own page: it reads map_trace's summary, the active brand pack's palette and logo and the bundled Inter, and imports nothing
from the acropolisdocs plugin.

    html(summary)  -> str      one self-contained page: fonts, logo and charts inline, no request of any kind, light, dark and auto themes
    pdf(page)      -> bytes    the same page through the plugin's WeasyPrint (pdf_stack), both charts printed

Only what map_trace already prints reaches the page: skill, script and tool names, option names, call numbers, timings, exit codes, MAP-*
ids and one scrubbed evidence line per failure. Never message text, command text or a full path.
"""
from __future__ import annotations

import base64
import datetime as dt
import html as _html
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

MAX_SEQUENCE = 40          # calls drawn in the sequence chart; more are summarised under it
MAX_SLICES = 12            # columns of the bubble chart
FAN_OUT_CHART_MIN = 3      # lanes before the fan-out is a chart rather than a sentence (the run-trace rule)

# Outside a built edition with no brand pack to read, CSS system and named colours (never a colour literal: brand colours come from packs only).
_BASE = {"accent_1": "LinkText", "accent_2": "seagreen", "accent_3": "indianred", "accent_4": "slategray", "accent_5": "dimgray",
         "bg_primary": "Canvas", "bg_surface": "Canvas", "bg_elevated": "ButtonFace", "text_primary": "CanvasText", "text_secondary": "CanvasText",
         "text_tertiary": "GrayText", "border_subtle": "GrayText", "border_default": "GrayText",
         "semantic": {"success": "seagreen", "warning": "darkgoldenrod", "danger": "firebrick", "info": "LinkText"}}
NEUTRAL = {"print": _BASE, "screen": _BASE}


def esc(x) -> str:
    return _html.escape("" if x is None else str(x), quote=True)


# ---------------------------------------------------------------------------------------------------------------- brand and fonts
def brand() -> dict:
    """{'name', 'print', 'screen', 'logo'} of the active brand; a neutral palette and no logo when there is no edition to read."""
    out = {"name": "", "print": NEUTRAL["print"], "screen": NEUTRAL["screen"], "logo": ""}
    try:
        import brandlib as bl
        slug = bl.resolve(gc.build_manifest())["brand"]
        b = bl.load_pack(slug)["brand"]
    except Exception:                                   # noqa: BLE001 - outside a built edition: the shipped pack, else neutral
        try:
            import json
            b = json.loads((gc.plugin_root() / gc.BRANDS_DIRNAME / "acropolisdocs" / "brand.json").read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            return out
    pal = b.get("palette") or {}
    for mode in ("print", "screen"):
        if isinstance(pal.get(mode), dict):
            out[mode] = {**NEUTRAL[mode], **pal[mode], "semantic": {**NEUTRAL[mode]["semantic"], **(pal[mode].get("semantic") or {})}}
    out["name"] = (b.get("identity") or {}).get("name") or ""
    logo = b.get("logo") or {}
    out["logo"] = logo.get("light") or logo.get("dark") or ""
    return out


def logo_svg(svg: str, height: int, label: str) -> str:
    import re
    if not svg:
        return ""
    m = re.search(r'viewBox="([\d.\-\s]+)"', svg)
    vb = [float(v) for v in m.group(1).split()] if m else [0, 0, 100, 100]
    w = round(height * vb[2] / vb[3])
    inner = re.sub(r"^<\?xml[^>]*>\s*", "", svg.strip())
    return re.sub(r"<svg\b([^>]*)>", lambda mm: f'<svg width="{w}" height="{height}" viewBox="{" ".join(f"{v:g}" for v in vb)}" role="img" '
                  f'aria-label="{esc(label)} logo" xmlns="http://www.w3.org/2000/svg">', inner, count=1)


def font_faces() -> str:
    d = gc.plugin_root() / "assets" / "fonts" / "inter"
    out = []
    for name, rng in (("inter-latin-wght-normal.woff2", "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,"
                                                         "U+2122,U+2190-2199,U+2212,U+2215,U+25A0-25FF,U+2588"),
                      ("inter-latin-ext-wght-normal.woff2", "U+0100-02AF,U+0304,U+0308,U+0329,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,"
                                                            "U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF")):
        p = d / name
        if p.is_file():
            b64 = base64.b64encode(p.read_bytes()).decode()
            out.append(f"@font-face{{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;"
                       f"src:url(data:font/woff2;base64,{b64}) format('woff2');unicode-range:{rng}}}")
    return "\n".join(out)


def css_vars(p: dict) -> str:
    s = p["semantic"]
    return (f"--bg:{p['bg_primary']};--surface:{p['bg_surface']};--elevated:{p['bg_elevated']};--text:{p['text_primary']};"
            f"--text2:{p['text_secondary']};--text3:{p['text_tertiary']};--line:{p['border_subtle']};--line2:{p['border_default']};"
            f"--a1:{p['accent_1']};--a2:{p['accent_2']};--a3:{p['accent_3']};--a4:{p['accent_4']};--a5:{p['accent_5']};"
            f"--ok:{s['success']};--warn:{s['warning']};--bad:{s['danger']};--info:{s['info']};")


CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 32px}
.toggle{display:flex;justify-content:flex-end;gap:6px;padding:8px 32px}
.toggle button{font:inherit;font-size:12px;padding:3px 10px;border:1px solid var(--line2);background:var(--bg);color:var(--text2);border-radius:4px;cursor:pointer}
.toggle button[aria-pressed=true]{background:var(--a1);border-color:var(--a1);color:var(--bg)}
.hero{background:var(--surface);border-bottom:1px solid var(--line);padding:32px 0}
.hero .wrap{display:grid;grid-template-columns:1.1fr 1fr;gap:32px;align-items:center}
.eyebrow,.label{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase}
.eyebrow{color:var(--text3);margin:16px 0 4px}
.label{color:var(--a1);margin:0 0 4px}
h1{font-size:44px;line-height:1.12;margin:0 0 12px;font-weight:700;letter-spacing:-.01em}
.sub{font-size:17px;color:var(--text2);margin:0}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.tile{background:var(--elevated);border:1px solid var(--line2);border-radius:8px;padding:18px 12px;text-align:center}
.tile b{display:block;font-size:28px;font-weight:700;color:var(--a1);line-height:1.2}
.tile span{display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--text2);margin-top:6px}
section.s{border-bottom:1px solid var(--line);padding:32px 0}
h2{font-size:30px;margin:0 0 14px;font-weight:600;letter-spacing:-.005em}
p{margin:12px 0}
code{font:12.5px/1.4 ui-monospace,"Cascadia Mono",Consolas,monospace;background:var(--elevated);padding:1px 5px;border-radius:3px}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}
th{background:var(--elevated);text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:600;color:var(--text2);padding:11px 12px;border-bottom:1px solid var(--line2)}
td{padding:11px 12px;border-bottom:1px solid var(--line2);vertical-align:top}
.n{text-align:right;font-variant-numeric:tabular-nums}
.mono{font:12.5px/1.45 ui-monospace,"Cascadia Mono",Consolas,monospace;overflow-wrap:anywhere}
.tbl{overflow-x:auto;max-width:100%}
.hero .wrap>*{min-width:0}
.ev td{border-bottom:1px solid var(--line2);padding-top:0;color:var(--text2)}
.pill{display:inline-block;border:1px solid currentColor;border-radius:10px;padding:0 8px;font-size:12px}
.muted{color:var(--text2)}.pill.bad{color:var(--bad)}.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.info{color:var(--info)}
.budget{display:flex;height:22px;border-radius:4px;overflow:hidden;border:1px solid var(--line);margin:14px 0 6px}
.budget i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;color:var(--text2)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.c-map{background:var(--a1)}.c-tool{background:var(--a4)}.c-model{background:var(--a2)}.c-idle{background:var(--a3)}
.switch{display:inline-flex;border:1px solid var(--line2);border-radius:5px;overflow:hidden;margin:4px 0 10px 8px;vertical-align:middle}
.switch button{font:inherit;font-size:13px;padding:5px 14px;border:0;background:var(--bg);color:var(--text2);cursor:pointer}
.switch button[aria-pressed=true]{background:var(--a1);color:var(--bg)}
.chartlabel{font-size:13px;font-weight:600}
figure{margin:12px 0}
figcaption{font-size:13px;color:var(--text2);text-align:center;margin-top:6px}
svg text{font-family:Inter,system-ui,sans-serif}
.chart[hidden]{display:none}
.notes li{margin:4px 0}
footer{background:var(--surface);text-align:center;padding:28px 0;color:var(--text2);font-size:14px}
footer .mark{display:inline-flex;align-items:center;gap:8px;font-weight:600;color:var(--text);margin-top:10px;font-size:13px}
.warnbox{border-left:4px solid var(--warn);background:var(--elevated);padding:10px 14px;margin:14px 0}
@media (max-width:820px){.hero .wrap{grid-template-columns:1fr}h1{font-size:34px}.wrap{padding:0 16px}.toggle{padding:8px 16px}}
@media print{
  @page{size:Letter;margin:14mm 12mm}
  body{font-size:11pt}
  .toggle,.switch,.chartlabel{display:none}
  .keep{break-inside:avoid}
  .chart[hidden]{display:block}
  .tile,figure,tr,.hero{break-inside:avoid}
  h2,.label{break-after:avoid}
  .tbl{overflow:visible}
  h1{font-size:30pt}
}
"""

JS = """
(function(){var r=document.documentElement,K='map-trace-theme';
function apply(v){var d=v==='dark'||(v==='auto'&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches);
r.setAttribute('data-theme',d?'dark':'light');document.querySelectorAll('.toggle button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.v===v)});}
var v='auto';try{v=localStorage.getItem(K)||'auto'}catch(e){}apply(v);
document.querySelectorAll('.toggle button').forEach(function(b){b.onclick=function(){try{localStorage.setItem(K,b.dataset.v)}catch(e){}apply(b.dataset.v)}});
if(window.matchMedia){matchMedia('(prefers-color-scheme: dark)').addEventListener('change',function(){var s='auto';try{s=localStorage.getItem(K)||'auto'}catch(e){}if(s==='auto')apply('auto')})}
document.querySelectorAll('.switch button').forEach(function(b){b.onclick=function(){var g=b.closest('section');
g.querySelectorAll('.switch button').forEach(function(x){x.setAttribute('aria-pressed',x===b)});
g.querySelectorAll('.chart').forEach(function(c){c.hidden=c.dataset.chart!==b.dataset.chart})}});})();
"""


# ---------------------------------------------------------------------------------------------------------------- formatting
def fmt_d(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    if s < 1:
        return f"{round(s * 1000)} ms"
    if s < 60:
        return f"{s:.1f} s"
    m, sec = divmod(round(s), 60)
    if m < 60:
        return f"{m} m {sec:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} m"


def pct(a, b) -> str:
    return f"{round(100 * a / b)}%" if b else "0%"


def table(head: list[str], rows: list[list], num: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="n">{esc(h)}</th>' if i in num else f"<th>{esc(h)}</th>" for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="n">{c}</td>' if i in num else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def section(label: str, title: str, body: str) -> str:
    return f'<section class="s"><div class="wrap"><p class="label">{esc(label)}</p><h2>{esc(title)}</h2>{body}</div></section>'


def call_name(e: dict) -> str:
    if e.get("mcp_tool"):
        return e["mcp_tool"]
    sc = e.get("scripts") or []
    return (sc[0] + ".py") if sc else (e.get("tool") or "?")


def outcome(e: dict) -> str:
    if e.get("stopped_by"):
        return "stopped"
    return {"ok": "ok", "failed": "failed", "blocked": "blocked", "orphan": "no result", "self": "this report"}.get(e.get("status"), e.get("status") or "?")


# ---------------------------------------------------------------------------------------------------------------- charts
def lane_colors(lanes: list[str]) -> dict[str, str]:
    cyc = ["var(--a1)", "var(--a2)", "var(--a3)", "var(--a4)", "var(--a5)", "var(--info)", "var(--warn)"]
    return {ln: cyc[i % len(cyc)] for i, ln in enumerate(lanes)}


def bubble_svg(mapev: list[dict], total: int, lanes: list[str], colors: dict) -> str:
    k = max(1, min(MAX_SLICES, total))
    edges = [round(1 + i * total / k) for i in range(k)] + [total + 1]
    cells: dict[tuple[str, int], list[dict]] = {}
    for e in mapev:
        i = max(j for j in range(k) if edges[j] <= e["n"])
        cells.setdefault((e["lane"], i), []).append(e)
    top = max((len(v) for v in cells.values()), default=1)
    left, right, row_h, top_pad = 190, 30, 72, 70
    w = 1000
    colw = (w - left - right) / k
    h = top_pad + row_h * len(lanes) + 50
    rmax = min(colw, row_h) * 0.42
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Map calls per lane over the session" xmlns="http://www.w3.org/2000/svg">',
           f'<text x="{left}" y="32" font-size="18" font-weight="600" fill="var(--text)">{len(mapev)} map call(s) across {len(lanes)} lane(s) · '
           f'{k} slice(s) by call number</text>',
           f'<text x="{w - right}" y="32" font-size="13" fill="var(--text2)" text-anchor="end">Area is calls · a ring marks a failed call</text>']
    for r, ln in enumerate(lanes):
        y = top_pad + r * row_h + row_h / 2
        out.append(f'<text x="{left - 14}" y="{y + 5}" font-size="14" fill="var(--text2)" text-anchor="end">{esc(ln)}</text>')
        out.append(f'<line x1="{left}" y1="{y}" x2="{w - right}" y2="{y}" stroke="var(--line2)" stroke-width="1"/>')
        for i in range(k):
            got = cells.get((ln, i))
            if not got:
                continue
            cx = left + colw * (i + 0.5)
            rr = max(9.0, rmax * math.sqrt(len(got) / top))
            out.append(f'<circle cx="{cx:.1f}" cy="{y}" r="{rr:.1f}" fill="{colors[ln]}" fill-opacity=".28" stroke="{colors[ln]}" stroke-width="1.5"/>')
            if any(e["status"] == "failed" for e in got):
                out.append(f'<circle cx="{cx:.1f}" cy="{y}" r="{rr + 5:.1f}" fill="none" stroke="var(--bad)" stroke-width="2" stroke-dasharray="4 3"/>')
            out.append(f'<text x="{cx:.1f}" y="{y + 5}" font-size="13" fill="var(--text)" text-anchor="middle">{len(got)}</text>')
    yb = top_pad + row_h * len(lanes)
    out.append(f'<line x1="{left}" y1="{yb}" x2="{w - right}" y2="{yb}" stroke="var(--text3)" stroke-width="1"/>')
    for i in range(k):
        a, b = edges[i], edges[i + 1] - 1
        lab = str(a) if a >= b else f"{a}-{b}"
        out.append(f'<text x="{left + colw * (i + 0.5):.1f}" y="{yb + 24}" font-size="13" fill="var(--text2)" text-anchor="middle">{lab}</text>')
    out.append("</svg>")
    return "".join(out)


def sequence_svg(mapev: list[dict], lanes: list[str], colors: dict) -> str:
    shown = mapev[:MAX_SEQUENCE]
    cols = ["Session"] + lanes
    span = max(150, 1000 // max(1, len(cols)))                      # one column per lane, never narrower than 150 units
    w, top, step = span * len(cols), 70, 40
    colx = {c: span * (i + 0.5) for i, c in enumerate(cols)}
    box = span - 14
    fit = max(6, int(box / 7.6))                                      # characters that fit a box at 12.5 units
    h = top + step * len(shown) + 20
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="The session calling each lane in turn" xmlns="http://www.w3.org/2000/svg">',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0,0L10,5L0,10z" fill="var(--text2)"/></marker></defs>']
    for c in cols:
        x = colx[c]
        name = c if len(c) <= fit else c[:fit - 1] + "…"
        out.append(f'<rect x="{x - box / 2:.1f}" y="12" width="{box}" height="30" rx="5" fill="var(--elevated)" stroke="{colors.get(c, "var(--line2)")}"/>')
        out.append(f'<text x="{x:.1f}" y="32" font-size="12.5" font-weight="600" fill="var(--text)" text-anchor="middle"><title>{esc(c)}</title>{esc(name)}</text>')
        out.append(f'<line x1="{x:.1f}" y1="42" x2="{x:.1f}" y2="{h - 10}" stroke="var(--line2)" stroke-dasharray="3 4"/>')
    for i, e in enumerate(shown):
        y = top + i * step
        x0, x1 = colx["Session"], colx[e["lane"]]
        col = "var(--bad)" if e["status"] == "failed" else colors.get(e["lane"], "var(--a1)")
        out.append(f'<text x="{x0 + 8:.1f}" y="{y - 6}" font-size="11.5" fill="var(--text2)">#{e["n"]} {esc(call_name(e))} · {fmt_d(e.get("seconds"))}</text>')
        out.append(f'<line x1="{x0:.1f}" y1="{y}" x2="{x1 - 3:.1f}" y2="{y}" stroke="{col}" stroke-width="1.6" marker-end="url(#ah)"/>')
        out.append(f'<line x1="{x1:.1f}" y1="{y + 13}" x2="{x0 + 3:.1f}" y2="{y + 13}" stroke="var(--text3)" stroke-width="1" stroke-dasharray="4 3" marker-end="url(#ah)"/>')
        out.append(f'<text x="{x1 + 7:.1f}" y="{y + 17}" font-size="11" font-weight="600" fill="{col}">{esc(outcome(e))}</text>')
    out.append("</svg>")
    return "".join(out)


def fanout_svg(rows: list[tuple[str, int, str]]) -> str:
    w, left, bar_h, gap = 1000, 300, 24, 12
    top = max(r[1] for r in rows) or 1
    h = 20 + len(rows) * (bar_h + gap)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Map calls per lane, ranked" xmlns="http://www.w3.org/2000/svg">']
    for i, (lane, n, role) in enumerate(rows):
        y = 10 + i * (bar_h + gap)
        bw = (w - left - 60) * n / top
        out.append(f'<text x="{left - 12}" y="{y + 17}" font-size="13" fill="var(--text2)" text-anchor="end">{esc(lane)} · {esc(role)}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="3" fill="var(--a1)" fill-opacity="{1 if i == 0 else 0.45}"/>')
        out.append(f'<text x="{left + bw + 8:.1f}" y="{y + 17}" font-size="13" fill="var(--text)">{n}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------- the page
def html(o: dict, generated: dt.datetime | None = None) -> str:
    b = brand()
    generated = generated or dt.datetime.now().astimezone()
    gen = generated.strftime("%Y-%m-%d %H:%M")
    events = o.get("events") or []
    mapev = [e for e in events if e.get("lane")]
    lanes = sorted(o.get("lanes") or {}, key=lambda k: (-o["lanes"][k]["calls"], k))
    colors = lane_colors(lanes)
    t, st, hk, calls = o["time"], o["status"], o.get("hooks") or {}, o["calls"]
    measured = t["durations"].startswith("measured")
    loops, retries = len(o.get("loops") or []), len(o.get("retries") or [])

    parts = []
    if str(o.get("found_by", "")).startswith("NEWEST-FALLBACK"):
        parts.append('<div class="wrap"><div class="warnbox"><b>The session was guessed.</b> No session id was available, so this report '
                     'traced the newest transcript on this machine. Run map-trace with <code>--session &lt;id&gt;</code> for a named one.</div></div>')

    # hero
    tiles = [(calls["map"], "Map calls"), (len(lanes), "Lanes"),
             (f"{round(100 * o.get('error_rate', 0))}%", f"Error rate ({st.get('blocked', 0)} blocked" + (f", {o['session_side_failures']} session-side" if o.get("session_side_failures") else "") + " excluded)"),
             (retries, "Retries"), (loops, "Iteration loops"), (fmt_d(t["wall"]), "Wall clock (" + ("measured" if measured else "bounded") + ")")]
    hero = (f'<section class="hero"><div class="wrap"><div><div>{logo_svg(b["logo"], 54, b["name"])}</div>'
            f'<p class="eyebrow">Map trace</p><h1>What this session did with the map plugin</h1>'
            f'<p class="sub">{calls["map"]} of the session\'s {calls["total"]} calls were the map plugin\'s, across {len(lanes)} lane(s).</p></div>'
            '<div class="tiles">' + "".join(f"<div class=\"tile\"><b>{esc(v)}</b><span>{esc(k)}</span></div>" for v, k in tiles) + "</div></div></section>")
    parts.append(hero)

    # what ran
    run = o.get("run") or {}
    if run.get("turns"):
        rows = [[esc(m), "", f'{v["turns"]}', f'{v["calls"]}'] for m, v in sorted((run.get("models") or {}).items(), key=lambda kv: -kv[1]["turns"])]
        for i, r in enumerate(rows):
            r[0], r[1] = ("Model" if i == 0 else ""), r[0]
        erows = [["Effort" if i == 0 else "", esc(k), f'{v["turns"]}', f'{v["calls"]}']
                 for i, (k, v) in enumerate(sorted((run.get("efforts") or {}).items(), key=lambda kv: -kv[1]["turns"]))]
        body = table(["", "Value", "Turns", "Tool calls"], rows + erows, {2, 3})
        body += (f'<p><strong>{run.get("reasoning_turns", 0)} of {run["turns"]} assistant turns ({pct(run.get("reasoning_turns", 0), run["turns"])}) '
                 f'included reasoning.</strong> Turns and tool calls are counted apart: a model and an effort level belong to a turn, and one '
                 f'turn can make several calls. Tokens and spend are <code>map-token-use</code>\'s report, not this one.</p>')
        parts.append(section("Model, effort and reasoning", "What ran this session", body))

    # where the clock went
    wall = max(t["wall"], 1)
    other_tool = max(0, t["tools"] - t["map_tools"])
    segs = [("c-map", "Map plugin calls", t["map_tools"]), ("c-tool", "Other tool calls", other_tool),
            ("c-model", "Not running a tool (residual)", t["model_overhead_residual"]), ("c-idle", "Session idle", t["idle"])]
    bar = '<div class="budget">' + "".join(f'<i class="{c}" style="width:{100 * v / wall:.4f}%"></i>' for c, _, v in segs) + "</div>"
    legend = '<div class="legend">' + "".join(f'<span><i class="{c}"></i>{esc(n)} - {fmt_d(v)} ({pct(v, wall)})</span>' for c, n, v in segs) + "</div>"
    rows = [[esc(n), fmt_d(v), pct(v, wall)] for _, n, v in segs] + [["Wall clock", fmt_d(t["wall"]), "100%"]]
    body = f'<div class="keep">{bar}{legend}</div>' + table(["Component", "Time", "Share"], rows, {1, 2})
    rt = t.get("round_trip") or {}
    if rt.get("count"):
        body += (f'<p>{rt["count"]} round trip(s) between a result arriving and the next call going out, median {fmt_d(rt["median"])}, worst '
                 f'{fmt_d(rt["worst"])}: {fmt_d(rt["total"])} of the run in total. That cost grows with the NUMBER of calls, so fewer and larger '
                 'calls shorten it and a faster script does not.</p>')
    body += (f'<p><strong>Not running a tool is a residual</strong>: the wall clock less tool time and idle gaps of two minutes or more '
             f'({t["idle_gaps"]} here). It holds model time, the runtime\'s own overhead and short waits; it is never model generation alone.</p>'
             f'<p>Map call durations are <strong>{esc(t["durations"])}</strong>.</p>')
    parts.append(section("The whole session, accounted for", "Where the clock went", body))

    # hooks
    if hk.get("log"):
        rows = [["Map calls timed by the timing hook", f'{hk.get("measured", 0)} of {hk.get("map_calls", 0)}'],
                ["Builds the pre-export check stopped", str(hk.get("stopped", 0))],
                ["Times the credentials reminder fired", str(hk.get("reminders", 0))],
                ["Hook log parts read", str(len(hk.get("files") or []))]]
        body = table(["Measure", "Value"], rows) + (
            "<p>The plugin's own hook log (<code>.geo-mapping/traces/hooks/&lt;session&gt;.ndjson</code>, one part in each working folder the "
            "session used) is joined to the transcript by each call's id. A stopped build never ran, so it has no time and is not counted in "
            "the measured share. The log holds names, ids, times and counts only: never a command, an argument or a message.</p>")
    else:
        body = (f'<p>No hook log for this session, so every duration is a bound from transcript timestamps. '
                f'{hk.get("stopped", 0)} build(s) stopped by the pre-export check were still read from the transcript.</p>')
    parts.append(section("What the plugin's hooks saw", "Hooks", body))

    # rework and ledger
    rows = [["Retries (same command again, nothing written between)", str(retries)],
            ["Iteration loops (fail, edit, re-run)", str(loops)],
            ["Re-runs after an edit (normal for visual checks)", str(len(o.get("reruns_after_edit") or []))],
            ["Hotspots (3 or more loops on one script)", str(len(o.get("hotspots") or []))]]
    rr = o.get("run_rework") or {}
    if rr.get("groups"):
        rows.append(["Pipeline time lost to repeated runs", f'{fmt_d(rr.get("seconds_lost"))} ({round(100 * rr.get("share_of_run_time", 0))}% of run time)'])
    body = table(["Measure", "Value"], rows)
    led = o.get("ledger") or {}
    if led:
        sev = {"BLOCK": "bad", "WARN": "warn", "INFO": "info"}
        lrows = [[f'<span class="mono">{esc(k)}</span>', f'<span class="pill {sev.get(v["severity"], "")}">{esc(v["severity"])}</span>',
                  str(v["calls"]), str(v["in_failed"])] for k, v in led.items()]
        body += "<p>Every MAP-* finding named in the map calls' output, blocking first.</p>" + table(["Finding", "Severity", "Calls", "In failed calls"], lrows, {2, 3})
    else:
        body += "<p>No MAP-* finding was named in this session's map calls.</p>"
    parts.append(section("What failing and re-running cost", "Rework and the findings ledger", body))

    # activity by lane
    if mapev:
        sw = ('<span class="chartlabel">Chart</span><span class="switch"><button data-chart="bubble" aria-pressed="true">Bubble</button>'
              '<button data-chart="sequence" aria-pressed="false">Sequence</button></span>')
        more = f" The first {MAX_SEQUENCE} of {len(mapev)} map calls are drawn." if len(mapev) > MAX_SEQUENCE else ""
        body = (sw + f'<figure class="chart" data-chart="bubble">{bubble_svg(mapev, calls["total"], lanes, colors)}'
                '<figcaption>One row per lane, the session left to right in slices of call numbers. A bubble\'s area is the calls that lane made in '
                'that slice; a dashed ring marks a slice in which a call failed.</figcaption></figure>'
                f'<figure class="chart" data-chart="sequence" hidden>{sequence_svg(mapev, lanes, colors)}'
                f'<figcaption>The session calling each lane in turn: the call number, the script or tool and its time on the call, the outcome on '
                f'the return.{more}</figcaption></figure>')
        rows = [[esc(ln), str(v["calls"]), str(v["failed"]), str(v["blocked"]),
                 esc(", ".join(v.get("basis") or [])) + (" · LOW CONFIDENCE" if v.get("low_confidence") else ""), fmt_d(v["seconds"])]
                for ln in lanes for v in [o["lanes"][ln]]]
        body += table(["Lane", "Calls", "Failed", "Blocked", "Attribution", "Time"], rows, {1, 2, 3, 5})
        parts.append(section("Where the work went", "Activity by lane", body))

        # fan-out
        by_tool: dict[str, int] = {}
        for e in mapev:
            k = "map server: " + e["mcp_tool"] if e.get("mcp_tool") else e.get("tool") or "?"
            by_tool[k] = by_tool.get(k, 0) + 1
        def role(ln: str) -> str:
            return "map server" if ln == "ogc-server" else ("script, no skill" if ln.startswith("(") else "skill")
        ranked = [(ln, o["lanes"][ln]["calls"], role(ln)) for ln in lanes]
        if len(ranked) >= FAN_OUT_CHART_MIN:
            body = (f"<figure>{fanout_svg(ranked)}<figcaption>{esc(ranked[0][0])} made {pct(ranked[0][1], calls['map'])} of the map calls."
                    "</figcaption></figure>")
        else:
            body = (f"<p>This session's map calls ran in {len(ranked)} lane(s), fewer than the {FAN_OUT_CHART_MIN} a ranked chart needs: "
                    + "; ".join(f"{esc(ln)} ({n} call(s))" for ln, n, _ in ranked) + ".</p>")
        body += table(["Tool", "Map calls"], [[esc(k), str(v)] for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1])], {1})
        parts.append(section("What the run pulled in", "Fan-out", body))

    # slowest
    if o.get("slowest"):
        rows = [[f'#{e["n"]}', esc(call_name(e)) + (f' <span class="muted">(whole command, also ran {esc(", ".join(e["shared_with"]))})</span>' if e.get("shared_with") else "")
                 + (f' <span class="muted">({esc(e["note"])})</span>' if e.get("note") else ""),
                 esc(e.get("lane") or ""), fmt_d(e.get("seconds")) + ("" if e.get("measured") else " (bound)"), esc(outcome(e))] for e in o["slowest"]]
        parts.append(section("Where map time went", "Slowest map calls", table(["Call", "Script or tool", "Lane", "Time", "Outcome"], rows, {3})))

    # errors
    fails = o.get("failures") or []
    if fails:
        grid: dict[tuple[str, str], int] = {}
        for e in fails:
            grid[(e["lane"], e.get("error_class") or "failed")] = grid.get((e["lane"], e.get("error_class") or "failed"), 0) + 1
        if len(grid) > 1:
            classes = sorted({c for _, c in grid})
            fl = sorted({ln for ln, _ in grid})
            body = table(["Lane"] + classes, [[esc(ln)] + [str(grid.get((ln, c), "")) for c in classes] for ln in fl], set(range(1, len(classes) + 1)))
        else:
            (ln, c), = grid
            body = f"<p>Every failure fell in one place, {esc(ln)}, class <code>{esc(c)}</code>, so there is nothing to compare and no matrix is drawn.</p>"
        reg = []
        for e in fails:
            why = "stopped before it ran by the pre-export check" if e.get("stopped_by") else (e.get("evidence") or "")
            reg.append(f'<tr><td class="n">{e["n"]}</td><td><span class="pill bad">{esc(outcome(e))}</span></td><td>{esc(e["lane"])}</td>'
                       f'<td class="mono">{esc(e.get("tool"))}</td><td class="mono">{esc(call_name(e))}</td>'
                       f'<td class="mono">{esc(e.get("error_class") or "failed")}</td><td class="n">{esc(e.get("exit_code") if e.get("exit_code") is not None else "")}</td></tr>'
                       f'<tr class="ev"><td></td><td colspan="6" class="mono">{esc(why)}'
                       + (f' · {esc(", ".join(e.get("findings") or {}))}' if e.get("findings") else "") + "</td></tr>")
        body += ('<div class="tbl"><table><thead><tr><th class="n">#</th><th>Status</th><th>Lane</th><th>Tool</th><th>Script</th><th>Class</th><th class="n">Exit</th>'
                 "</tr></thead><tbody>" + "".join(reg) + "</tbody></table></div>")
        parts.append(section("Every failure, with its evidence", "Error details", body))

    # fix spec
    fs = o.get("fix_spec") or []
    if fs:
        rows = []
        for f in fs:
            rows.append([f'<span class="mono">{esc(f["id"])}</span>', f'<span class="mono">{esc(f["class"])}</span>', esc(f.get("owner") or ""),
                         esc(f.get("disposition")), esc(f.get("severity") or ""), ", ".join(f"#{n}" for n in f.get("calls") or []),
                         esc(f.get("reading") or (f.get("diagnosis") or ""))])
        body = table(["Finding", "Class", "Owner", "Disposition", "Severity", "Calls", "Reading"], rows) + (
            "<p>One finding per class, script and crash site. <code>product</code> is a crash inside the plugin and needs a diagnosis; "
            "<code>input</code> means a gate, the MapSpec, the data or the arguments stopped the run, so fix those, not the plugin.</p>")
        parts.append(section("What to fix", "Fix spec", body))

    # pipeline runs
    if o.get("runs"):
        rows = [[esc(r.get("pipeline")), esc(r.get("status")), fmt_d(r.get("seconds")), esc(r.get("run_id") or r.get("id") or "")] for r in o["runs"][:30]]
        parts.append(section("This plugin's run records", "Pipeline runs", table(["Pipeline", "Status", "Time", "Run"], rows, {2})))

    # how measured
    # 2.2.0: the map evaluator's grades of this session's outputs, when it ran (observe only: nothing that makes a map reads them)
    mq = o.get("map_quality")
    if mq:
        rows = [[esc(x["output"]), esc(x["effectiveness"] if x["effectiveness"] is not None else "-"), esc(x["craft"] if x["craft"] is not None else "-"),
                 f'<span class="pill {dict(yes="ok", partly="warn", no="bad").get(x["works"], "info")}">{esc(x["works"])}</span>', esc(", ".join(x.get("flags") or []))]
                for x in mq.get("outputs") or []]
        comps = "".join(f"<li><b>{esc(c['component'])}</b> ({c['count']}): {esc(c['example'])}</li>" for c in mq.get("top_components") or [])
        body = ("<p>The finished maps this session wrote, graded from the pictures alone against rubric " + esc(mq.get("rubric_version")) + ". Effectiveness and "
                "Craft are kept apart; the works verdict sits beside them. These grades are a record for improving the plugin and never change a map.</p>"
                + table(["Output", "Effectiveness", "Craft", "Works", "Flags"], rows, {1, 2})
                + (f"<p class='chartlabel'>Where the plugin fell short most often</p><ul class='notes'>{comps}</ul>" if comps else "")
                + f"<p class='muted'>The full evaluation: <code>{esc(Path(str(mq.get('report') or '')).name)}</code> in the run folder <code>{esc(mq.get('run'))}</code>.</p>")
        parts.append(section("Map quality (observed)", "How good the finished maps are", body))
    notes = [f'Transcript <code>{esc(str(o.get("transcript", ""))[:8])}…</code>, found by {esc(o.get("found_by"))}.',
             f'Map call durations are {esc(t["durations"])}.',
             "A map call is this plugin's skill loaded, one of its scripts or lints run with Python (in the Bash or the PowerShell tool), or a tool "
             "of its map server. Reading a file is not using the plugin; another plugin's scripts are never counted.",
             "Only names, option names, call numbers, timings, exit codes and MAP-* ids are shown, and one line of evidence per failure with keys "
             "masked, addresses replaced and paths cut to their last part. No message, command or full path appears here, and nothing in this "
             "report was fetched from the network.",
             f"Generated {esc(gen)} by {esc(o.get('plugin'))} {esc(o.get('version') or '')}."]
    if o.get("window"):
        notes.insert(1, f"Only the window {esc(o['window'])} of the session is counted.")
    parts.append(section("How this was measured", "Method", '<ul class="notes">' + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"))

    foot = (f'<footer><div>Map trace · {esc(o.get("plugin"))} · Generated {esc(gen)}</div>'
            + (f'<div class="mark">{logo_svg(b["logo"], 18, b["name"])}<span>{esc(b["name"])}</span></div>' if b["name"] else "") + "</footer>")
    style = (f"{font_faces()}\n:root{{{css_vars(b['print'])}color-scheme:light}}\n"
             f":root[data-theme=dark]{{{css_vars(b['screen'])}color-scheme:dark}}\n{CSS}")
    title = f"Map trace {str(o.get('transcript', ''))[:8]}"
    toggle = ('<div class="toggle" role="group" aria-label="Theme"><button data-v="light" aria-pressed="false">Light</button>'
              '<button data-v="dark" aria-pressed="false">Dark</button><button data-v="auto" aria-pressed="true">Auto</button></div>')
    return ("<!doctype html>\n<html lang=\"en\" data-theme=\"light\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"generator\" content=\"{esc(o.get('plugin'))} {esc(o.get('version') or '')} map-trace\">"
            f"<title>{esc(title)}</title><style>{style}</style></head>"
            f'<body data-doc-type="map-trace-report">{toggle}{"".join(parts)}{foot}<script>{JS}</script></body></html>\n')


def pdf(page: str) -> bytes:
    """The page as a PDF through the plugin's WeasyPrint, both charts shown (print CSS), light theme. WeasyPrint does not resolve CSS
    variables inside SVG, so every var(--name) is first written out as the brand's print colour. Raises when WeasyPrint is missing."""
    import re
    import pdf_stack
    colours = dict(kv.split(":", 1) for kv in css_vars(brand()["print"]).rstrip(";").split(";"))
    page = re.sub(r"var\((--[\w-]+)\)", lambda m: colours.get(m.group(1), m.group(0)), page)
    return pdf_stack.html_to_pdf(page)
