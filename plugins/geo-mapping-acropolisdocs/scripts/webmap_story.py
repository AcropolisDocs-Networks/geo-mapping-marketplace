"""webmap_story: the story map and the KPI panel on the web page (M15 web half, gap analysis P2-3). `webmap.build_page` calls it.

    webmap:
      story:
        chapters:
          - title: Three venue clusters
            text: "Twelve masts serve SoFi Stadium, Exposition Park and the arena district."
            view: {bbox: [-118.36, 33.94, -118.25, 34.05], pitch: 45, bearing: -20}     # or {center: [lon, lat], zoom: 15}
            layers: [buildings, masts, links]                                         # the layers shown; the others are hidden
            time: "Jun 2027"                                                          # a period of the time slider (label, value or index)
            select: {layer: masts, field: venue, in: [SoFi Stadium]}                  # the KPI panel's totals for these features
      panel: {title: Venue KPIs, fields: [peak_users, {field: throughput_gbps, label: Throughput, unit: Gbps, agg: sum}]}

One map, a column of chapters beside it (a sheet under it on a phone). The chapter at the reading line flies the map to its view and shows its
layers and its period; the keyboard moves between chapters (arrow keys, Page Up and Down, Home, End), and with reduced motion the map jumps
instead of flying. The print view lists the chapters, each with a static picture made at build time through the print path (the same
renderer as the print map, with the chapter's window, layers and period). MAP-WEB-026 checks the chapters: layers the page has, a view,
a period the slider has, and a view that shows some of its layers' features.
"""
from __future__ import annotations

import html
import math

VIEW_PX = (960, 600)                   # a centre and a zoom is read as a window of this size (the print picture's and the checks' window)
CHAPTERS_MAX = 30


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def view_bbox(view: dict) -> list[float] | None:
    """A chapter's view as a lon/lat box: its bbox, or the VIEW_PX window around its centre at its zoom (the page's zoom numbers)."""
    if view.get("bbox"):
        b = [float(v) for v in view["bbox"]]
        return b if len(b) == 4 and b[0] < b[2] and b[1] < b[3] else None
    c = view.get("center")
    if not c or view.get("zoom") is None:
        return None
    lon, lat = float(c[0]), float(c[1])
    z = float(view["zoom"])
    R = 6378137.0
    m_px = 2 * math.pi * R / (256 * 2 ** z)                 # Web Mercator metres per pixel (not ground metres)
    x = lon * math.pi * R / 180
    y = math.log(math.tan(math.pi / 4 + math.radians(max(min(lat, 85), -85)) / 2)) * R
    hw, hh = VIEW_PX[0] / 2 * m_px, VIEW_PX[1] / 2 * m_px
    def ll(xx, yy):
        return xx * 180 / (math.pi * R), math.degrees(2 * math.atan(math.exp(yy / R)) - math.pi / 2)
    w, s = ll(x - hw, y - hh)
    e, n = ll(x + hw, y + hh)
    return [w, s, e, n]


def panel_fields(panel: dict | None) -> list[dict]:
    out = []
    for f in (panel or {}).get("fields") or []:
        if isinstance(f, str):
            f = {"field": f}
        if not isinstance(f, dict) or not f.get("field"):
            continue
        out.append({"field": str(f["field"]), "label": str(f.get("label") or str(f["field"]).replace("_", " ").capitalize()),
                    "agg": str(f.get("agg") or "sum"), "unit": f.get("unit"), "decimals": f.get("decimals")})
    return out


def panel_config(panel: dict | None, defs: list[dict], columns: dict[str, list[str]]) -> tuple[dict | None, list[dict]]:
    """The KPI panel for the page: its title and fields, the fields any layer carries. -> (config or None, findings)."""
    if not panel:
        return None, []
    fields = panel_fields(panel)
    fs = []
    have = {c for cols in columns.values() for c in cols}
    missing = [f["field"] for f in fields if f["field"] not in have]
    if missing:
        fs.append(finding("MAP-WEB-026", "WARN", f"webmap.panel names field(s) no layer of the page has: {', '.join(missing)} (a joined table's columns count once the join runs)", missing))
    fields = [f for f in fields if f["field"] in have]
    if not fields:
        return None, fs
    return {"title": str(panel.get("title") or "Key figures"), "fields": fields, "hint": str(panel.get("hint") or "Click a feature on the map")}, fs


def chapter_time(t, time_cfg: dict | None, index=None):
    """A chapter's period on the slider (1.1.0, Day 10 issue 3): `time_index` is an index; `time` is a value or a label, and the value wins:
    `time: 6` on a numeric slider is the period holding 6 (km 6), not the seventh period. An integer that is no value of the slider is read as
    an index, as before 1.1.0. -> the index, or -1 when it is not a period of the slider."""
    if not time_cfg:
        return None
    labels = time_cfg.get("labels") or []
    if index is not None and not isinstance(index, bool):
        try:
            k = int(index)
        except (TypeError, ValueError):
            return -1
        return k if 0 <= k < len(labels) else -1
    if t is None:
        return None
    s = str(t).strip()
    if s in labels:
        return labels.index(s)
    num = time_cfg.get("num")
    if num and isinstance(t, (int, float, str)) and not isinstance(t, bool):
        try:
            v = float(t)
            k = int(math.floor((v - float(num["lo"])) / float(num["step"]) + 1e-9))
            if 0 <= k < len(labels):
                return k
        except (TypeError, ValueError):
            pass
    import pandas as pd
    try:                                                  # a date in the period: 2027-06 or 2027-06-15 for "Jun 2027"
        ts = pd.Timestamp(s)
        for k, lab in enumerate(labels):
            for cand in (ts.strftime("%b %Y"), str(ts.year), f"{ts.year} Q{ts.quarter}", ts.strftime("%Y-%m-%d")):
                if cand == lab:
                    return k
    except (ValueError, TypeError):
        pass
    try:
        f = float(s)
        for k, lab in enumerate(labels):
            if lab.split(" to ")[0] == f"{f:g}":
                return k
    except ValueError:
        pass
    if isinstance(t, int) and not isinstance(t, bool) and 0 <= t < len(labels):
        return t
    return -1


def plan(story: dict | None, defs: list[dict], collections: dict, time_cfg: dict | None, page_bbox) -> tuple[dict | None, list[dict], list[str]]:
    """The story's chapters for the page. -> (config {chapters: [{title, text, view, bbox, layers, time, select}]}, findings, notes)."""
    if not story:
        return None, [], []
    chs = list(story.get("chapters") or [])
    fs, notes = [], []
    if not chs:
        return None, [finding("MAP-WEB-026", "BLOCK", "webmap.story has no chapters")], notes
    if len(chs) > CHAPTERS_MAX:
        fs.append(finding("MAP-WEB-026", "WARN", f"the story has {len(chs)} chapters; the page carries the first {CHAPTERS_MAX}"))
        chs = chs[:CHAPTERS_MAX]
    alias = {d["alias"]: d["id"] for d in defs if d.get("alias") and d["alias"] != d["id"]}   # 1.1.0 (Day 10 issue 4): the spec's id of the relief
    ids = {d["id"] for d in defs} | set(alias)
    data_ids = [d["id"] for d in defs if not d.get("context")]
    out = []
    for i, ch in enumerate(chs):
        n = i + 1
        title = str(ch.get("title") or f"Chapter {n}")
        view = dict(ch.get("view") or {})
        if not view:
            view = {"bbox": list(page_bbox)}
        bb = view_bbox(view)
        if bb is None:
            fs.append(finding("MAP-WEB-026", "BLOCK", f"chapter {n} ('{title}'): its view needs bbox: [w, s, e, n], or center: [lon, lat] with a zoom"))
            continue
        layers = ch.get("layers")
        if layers is not None:
            layers = [alias.get(x, x) for x in layers]
            unknown = [x for x in layers if x not in ids]
            if unknown:
                fs.append(finding("MAP-WEB-026", "BLOCK", f"chapter {n} ('{title}') names layer(s) the page does not have: {', '.join(map(str, unknown))}; "
                                  f"the layers are {', '.join(sorted(ids))}", unknown))
                continue
        shown = list(layers) if layers is not None else data_ids
        t = chapter_time(ch.get("time"), time_cfg, ch.get("time_index"))
        if (ch.get("time") is not None or ch.get("time_index") is not None) and not time_cfg:
            fs.append(finding("MAP-WEB-026", "WARN", f"chapter {n} ('{title}') has a time, but the page has no time slider (a layer needs a time_field and webmap.time)"))
            t = None
        elif t == -1:
            fs.append(finding("MAP-WEB-026", "WARN", f"chapter {n} ('{title}'): time '{ch.get('time', ch.get('time_index'))}' is not a period of the slider ({', '.join((time_cfg or {}).get('labels', [])[:6])}...)"))
            t = None
        # a chapter that lands on an empty view: none of its layers' features in its window
        hit = 0
        for lid in shown:
            for f in (collections.get(lid) or {}).get("features") or []:
                if t is not None and lid in ((time_cfg or {}).get("layers") or []):
                    pt = f["properties"].get("_t")
                    if pt is None or pt < 0 or (pt > t if (time_cfg or {}).get("mode") == "cumulative" else pt != t):
                        continue
                if _touches(f["geometry"], bb):
                    hit += 1
                    break
            if hit:
                break
        tiled = [lid for lid in shown if not (collections.get(lid) or {}).get("features")]
        if not hit and shown and len(tiled) < len(shown):
            fs.append(finding("MAP-WEB-026", "WARN", f"chapter {n} ('{title}') lands on an empty view: none of the features of {', '.join(shown)} are inside it", {"chapter": n, "bbox": bb}))
        sel = ch.get("select")
        if sel:
            if not isinstance(sel, dict) or sel.get("layer") not in ids or not sel.get("field") or not isinstance(sel.get("in"), list):
                fs.append(finding("MAP-WEB-026", "WARN", f"chapter {n} ('{title}'): select needs layer (one of the page's), field and a list in:; the panel shows nothing for it"))
                sel = None
            else:
                sel = {"layer": sel["layer"], "field": str(sel["field"]), "in": [str(v) for v in sel["in"]], "label": str(sel.get("label") or ", ".join(map(str, sel["in"])))}
        v = {k: view[k] for k in ("center", "zoom", "bbox", "pitch", "bearing") if view.get(k) is not None}
        out.append({"title": title, "text": str(ch.get("text") or ""), "view": v, "bbox": [round(x, 6) for x in bb], "layers": layers, "time": t,
                    "select": sel, "id": f"gm-ch-{i}"})
    if not out:
        return None, fs, notes
    notes.append(f"story: {len(out)} chapter(s); each flies the map to its view and shows its layers")
    return {"chapters": out}, fs, notes


def _touches(g: dict, bb) -> bool:
    def walk(c):
        if isinstance(c[0], (int, float)):
            yield c
        else:
            for x in c:
                yield from walk(x)
    for x, y in walk(g["coordinates"]):
        if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]:
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------- pictures
def picture_edit(base_edit, ch: dict, time_cfg: dict | None, time_values: dict, title: str):
    """A spec_edit for one chapter's static picture: the page's print edits, then the chapter's window, its layers (a layer another layer's
    ops need stays as a helper), its period (a filter on the time field) and its title."""
    def edit(spec):
        base_edit(spec)
        spec["extent"] = {"mode": "bbox", "bbox": list(ch["bbox"]), "padding_pct": 0}
        keep = set(ch["layers"]) if ch.get("layers") is not None else None
        if keep is not None:
            needed = set()
            for ly in spec.get("layers") or []:
                if ly["id"] in keep:
                    for o in ly.get("ops") or []:
                        if isinstance(o, dict):
                            for k in ("to", "layer"):
                                if isinstance(o.get(k), str):
                                    needed.add(o[k])
            out = []
            for ly in spec.get("layers") or []:
                if ly["id"] in keep or ly.get("role") in ("context", "hillshade", "contour", "raster"):
                    out.append(ly)
                elif ly["id"] in needed:
                    out.append(dict(ly, draw=False))
            spec["layers"] = out
        if ch.get("time") is not None and time_cfg:
            for ly in spec.get("layers") or []:
                if ly["id"] in (time_cfg.get("layers") or []) and ly["id"] in time_values:
                    vals = time_values[ly["id"]](ch["time"])
                    if vals:
                        f = ly.get("filter")
                        fl = [f] if isinstance(f, dict) else list(f or [])
                        ly["filter"] = fl + [{"column": time_values[ly["id"]].field, "in": vals}]
        els = spec.setdefault("elements", {})
        els["title"] = dict(els.get("title") or {}, on=True, text=ch["title"], subtitle=title)
        els["scale_bar"] = {"on": True}
    return edit


def pictures(spec_path, chapters: list[dict], *, brand, template, mode: str, base_edit, time_cfg, time_values, title: str, uri) -> tuple[list, list[str]]:
    """A static picture per chapter, through the print path. -> ([data URI or None], notes)."""
    import map_plan as mp
    import map_render as mr
    out, notes = [], []
    for i, ch in enumerate(chapters):
        try:
            p = mp.prepare(spec_path, mode=mode, brand=brand, template=template, spec_edit=picture_edit(base_edit, ch, time_cfg, time_values, title), use_tiles=False)
            r = mr.render(p)
            out.append(uri(r, ch["title"], notes))
        except Exception as ex:  # noqa: BLE001 - a chapter without a picture still prints its words
            out.append(None)
            notes.append(f"story: chapter {i + 1} has no print picture ({str(ex).splitlines()[0][:160]})")
    return out, notes


# ---------------------------------------------------------------------------------------------------------------- markup
def paragraphs(text: str) -> str:
    return "".join(f"<p>{esc(p.strip())}</p>" for p in str(text).split("\n\n") if p.strip())


def column_html(story: dict, *, title: str, subtitle: str, message: str, logo: str) -> str:
    """The column of chapters (a sheet on a phone). Each chapter is focusable; the reading line picks the active one."""
    n = len(story["chapters"])
    parts = [f'<aside id="gm-story" aria-label="Story: {esc(title)}"><header class="gm-story-head">{logo}<div><h2 id="gm-title">{esc(title)}</h2>'
             + (f'<p id="gm-subtitle">{esc(subtitle)}</p>' if subtitle else "") + f'</div></header><p class="gm-story-msg">{esc(message)}</p>'
             '<p class="gm-story-help">Scroll, or use the arrow keys, to move through the chapters.</p>']
    for i, ch in enumerate(story["chapters"]):
        prev = '<button type="button" data-go="prev" aria-label="Previous chapter">Previous</button>' if i > 0 else "<span></span>"
        nxt = '<button type="button" data-go="next" aria-label="Next chapter">Next</button>' if i < n - 1 else "<span></span>"
        parts.append(f'<section class="gm-chapter" id="{ch["id"]}" tabindex="0" aria-labelledby="{ch["id"]}-h"><p class="gm-ch-n">{i + 1} of {n}</p>'
                     f'<h3 id="{ch["id"]}-h">{esc(ch["title"])}</h3>{paragraphs(ch["text"])}<div class="gm-ch-nav">{prev}{nxt}</div></section>')
    parts.append('<div class="gm-story-end" aria-hidden="true"></div><p id="gm-story-live" class="gm-sr" aria-live="polite"></p></aside>')
    return "".join(parts)


def print_html(story: dict, pics: list, *, title: str, message: str) -> str:
    """The print view: every chapter with its static picture and its words (the live map is not printed on a story page)."""
    rows = []
    for i, (ch, pic) in enumerate(zip(story["chapters"], pics)):
        img = f'<img alt="{esc(ch["title"])}: the map for this chapter" src="{pic}">' if pic else ""
        rows.append(f'<article class="gm-pch"><h2>{i + 1}. {esc(ch["title"])}</h2>{img}{paragraphs(ch["text"])}</article>')
    return f'<section id="gm-story-print" aria-label="The story, for print"><h1>{esc(title)}</h1><p>{esc(message)}</p>{"".join(rows)}</section>'


def panel_html(panel: dict) -> str:
    return (f'<section class="gm-panel" id="gm-kpi" aria-label="{esc(panel["title"])}"><div class="gm-body"><h3>{esc(panel["title"])}</h3>'
            f'<p id="gm-kpi-what" aria-live="polite">{esc(panel["hint"])}</p><dl id="gm-kpi-dl"></dl></div></section>')
