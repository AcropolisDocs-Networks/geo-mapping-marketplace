#!/usr/bin/env python3
"""kml_style: KML and KMZ written with the map's styling, for Google Earth (M14, gap analysis P2-7), and the styles read back.

    import kml_style as ks
    wr = ks.write(gdf, "sites.kmz", style)                               # one layer (geo-convert --kml-style STYLE.json)
    rep = ks.write_layers([("grid", gdf, st1), ("sites", gdf2, st2)], "map.kmz", title="...", legend=True, tokens=plan.tokens)
    styles = ks.read_styles("map.kmz")                                   # {layer: style dict}, the round trip
    fs = ks.check("map.kmz", {"grid": 20, "sites": 40})                  # MAP-OUT-010

What is written (the style dict is style_dict's):
  * one layer per nested <Document> (a folder in Google Earth's Places), in draw order, named by the layer's title, holding its <Schema> and
    its placemarks; GDAL's LIBKML reader, which QGIS uses for a KMZ, finds a KMZ's typed fields only with the Schema inside the layer;
  * one <Style> per class: PolyStyle and LineStyle colours in KML's aabbggrr with the fill opacity, line widths in pixels, IconStyle for points
    pointing at a small PNG of the symbol (circle, square, triangle, diamond or cross in the class colour with its outline) drawn with Pillow
    and packed in the KMZ under files/; a style for features with no class (the no-data colour);
  * proportional symbols scaled per placemark (area proportional to the value, the map's rule);
  * attributes as typed ExtendedData (a <Schema> per layer and SchemaData), so GDAL and Google Earth read them back with their types; the
    "name" field is the placemark name and a "description" field its description;
  * a description balloon (BalloonStyle) templated from the chosen fields (`balloon_fields`, default every field up to 25);
  * optionally a legend as a ScreenOverlay PNG (Inter, the brand's legend colours) in the lower-left corner;
  * the style itself as JSON in the folder's ExtendedData (namespace urn:geo-mapping:kml-style:1), so `read_styles` can give back the field,
    the classes and their labels as well as the colours, widths and sizes it reads from the <Style> elements (an edit made in Google Earth to a
    colour or a width is what comes back). A KML from elsewhere comes back as one class per style used.

Print millimetres read small on a screen, so KML widths and icons are drawn 1.5 times their sheet size (icons between 8 and 48 pixels).
A plain .kml cannot hold the icons: they are written beside it in <name>_files/ and the KML points there; a KMZ carries them inside.
"""
from __future__ import annotations

import html
import io
import json
import math
import re
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import style_dict as sd  # noqa: E402

KML_NS = "http://www.opengis.net/kml/2.2"
GMAP_NS = "urn:geo-mapping:kml-style:1"
ICON_PX = 64
SCREEN = 1.5
ICON_MIN_PX, ICON_MAX_PX = 8, 48
GE_ICON_PX = 32                       # Google Earth draws an icon at 32 px times its scale
BALLOON_MAX_FIELDS = 25
MAX_KML_BYTES = 40 * 1024 * 1024
KMZ_MAX_ENTRY = 64 * 1024 * 1024


class KmlStyleError(ValueError):
    """A refusal with a message that is safe to show the person."""


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------- sizes
def width_px(mm: float) -> float:
    return max(1.0, round(float(mm) * sd.MM_PX * SCREEN, 1))


def icon_scale(mm: float) -> float:
    px = min(ICON_MAX_PX, max(ICON_MIN_PX, float(mm) * sd.MM_PX * SCREEN))
    return round(px / GE_ICON_PX, 3)


def _mm_from_width(px: float, meta_mm: float | None) -> float:
    if meta_mm is not None and abs(width_px(meta_mm) - px) < 0.05:
        return float(meta_mm)
    return round(px / (sd.MM_PX * SCREEN), 4)


def _mm_from_scale(scale: float, meta_mm: float | None) -> float:
    if meta_mm is not None and abs(icon_scale(meta_mm) - scale) < 0.002:
        return float(meta_mm)
    return round(scale * GE_ICON_PX / (sd.MM_PX * SCREEN), 4)


# ---------------------------------------------------------------------------------------------------------------- icons and legend
def _shape_points(shape: str, cx: float, cy: float, d: float) -> list[tuple[float, float]] | None:
    """The outline of a symbol of size d centred at (cx, cy), as svg_draw.symbol_path draws it (y down); None for a circle."""
    r = d / 2
    if shape == "circle":
        return None
    if shape == "square":
        return [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    if shape == "triangle":
        h = d * 0.9
        return [(cx, cy - h * 0.62), (cx + r, cy + h * 0.38), (cx - r, cy + h * 0.38)]
    if shape == "diamond":
        return [(cx, cy - r * 1.2), (cx + r, cy), (cx, cy + r * 1.2), (cx - r, cy)]
    if shape == "cross":
        t = d * 0.18
        return [(cx - t, cy - r), (cx + t, cy - r), (cx + t, cy - t), (cx + r, cy - t), (cx + r, cy + t), (cx + t, cy + t), (cx + t, cy + r),
                (cx - t, cy + r), (cx - t, cy + t), (cx - r, cy + t), (cx - r, cy - t), (cx - t, cy - t)]
    raise KmlStyleError(f"unknown symbol shape '{shape}'")


def icon_png(shape: str, fill, stroke=None, *, px: int = ICON_PX, outline_share: float = 0.11, opacity: float = 1.0) -> bytes:
    """A square PNG of one point symbol, drawn four times larger and reduced so its edges are smooth; transparent around the shape."""
    from PIL import Image, ImageDraw
    k = 4
    W = px * k
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    f = sd.rgba(fill)
    f = (f[0], f[1], f[2], int(round(f[3] * opacity)))
    s = sd.rgba(stroke) if stroke else None
    ow = max(k, int(round(W * outline_share))) if s else 0
    pad = ow / 2 + k
    d = (W - 2 * pad) / (1.2 if shape == "diamond" else 1.0)
    cx = cy = W / 2
    pts = _shape_points(shape, cx, cy, d)
    if pts is None:
        box = (pad, pad, W - pad, W - pad)
        dr.ellipse(box, fill=f, outline=s, width=ow)
    else:
        if shape == "triangle":            # the triangle sits low in its box (as on the map); centre it in the icon
            ys = [p[1] for p in pts]
            dy = cy - (min(ys) + max(ys)) / 2
            pts = [(x, y + dy) for x, y in pts]
        dr.polygon(pts, fill=f, outline=s, width=ow)
    img = img.resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _font(size_px: float, bold: bool = False):
    from PIL import ImageFont
    import gmap_common as gc
    p = gc.plugin_root() / "assets" / "fonts" / "inter" / "inter-latin-wght-normal.woff2"
    f = ImageFont.truetype(str(p), int(round(size_px)))
    if bold:
        try:
            f.set_variation_by_axes([700])
        except Exception:  # noqa: BLE001 - a static face keeps its weight
            pass
    return f


def legend_png(sections: list[dict], tokens: dict | None = None, scale: int = 2) -> tuple[bytes, tuple[int, int]]:
    """The legend as a PNG: per layer its title and one row per class (swatch and label). Returns (png bytes, (width, height) at 1x)."""
    from PIL import Image, ImageDraw
    bg = sd.rgba(tokens["legend_bg"]) if tokens and tokens.get("legend_bg") else sd.FALLBACK_RGB["legend_bg"] + (255,)
    frame = sd.rgba(tokens["legend_frame"]) if tokens and tokens.get("legend_frame") else sd.FALLBACK_RGB["legend_frame"] + (255,)
    ink = sd.rgba(tokens["label"]) if tokens and tokens.get("label") else sd.FALLBACK_RGB["label"] + (255,)
    s = scale
    ft, fl = _font(12 * s, bold=True), _font(11 * s)
    pad, sw, gap, row = 10 * s, 14 * s, 6 * s, 18 * s
    rows: list[tuple] = []
    width = 0
    for sec in sections:
        t = str(sec.get("title") or "")
        if t:
            rows.append(("title", t))
            width = max(width, ft.getbbox(t)[2])
        for it in sec["items"]:
            rows.append(("item", sec["kind"], it))
            width = max(width, sw + gap + fl.getbbox(str(it["label"]))[2])
        rows.append(("space",))
    if rows and rows[-1] == ("space",):
        rows.pop()
    height = sum(row if r[0] != "space" else 6 * s for r in rows) + 2 * pad
    W, H = int(width + 2 * pad), int(height)
    img = Image.new("RGBA", (W, H), bg)
    dr = ImageDraw.Draw(img)
    dr.rectangle((0, 0, W - 1, H - 1), outline=frame, width=max(1, s))
    y = pad
    for r in rows:
        if r[0] == "space":
            y += 6 * s
            continue
        if r[0] == "title":
            dr.text((pad, y + 2 * s), r[1], font=ft, fill=ink)
            y += row
            continue
        _, kind, it = r
        x0, y0 = pad, y + 2 * s
        col = sd.rgba(it["color"]) if it.get("color") else (0, 0, 0, 0)
        if kind == "polygon":
            col = (col[0], col[1], col[2], int(round(col[3] * float(it.get("opacity", 1.0)))))
            dr.rectangle((x0, y0, x0 + sw, y0 + sw), fill=col, outline=sd.rgba(it["stroke"]) if it.get("stroke") else frame, width=max(1, s))
        elif kind == "line":
            lw = max(s, int(round(width_px(it.get("width", 0.5)) * s)))
            dr.line((x0, y0 + sw / 2, x0 + sw, y0 + sw / 2), fill=col, width=lw)
        else:
            ic = Image.open(io.BytesIO(icon_png(it.get("shape", "circle"), it["color"], it.get("stroke"), px=sw))).convert("RGBA")
            img.alpha_composite(ic, (int(x0), int(y0)))
        dr.text((x0 + sw + gap, y + 2 * s), str(it["label"]), font=fl, fill=ink)
        y += row
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), (W // s, H // s)


def legend_section(style: dict) -> dict:
    kind = style["geometry"]
    cols, labs = sd.class_colors(style), sd.class_labels(style)
    bv = style.get("bivariate")
    if bv and bv.get("colors"):                  # 0.24.0: the n x n classes, labelled by their row and column when no labels were recorded
        n = int(bv["n"])
        cols = list(bv["colors"])[: n * n]
        labs = list(bv.get("labels") or [])
        if len(labs) != len(cols):
            labs = [f"{bv['y']['field']} class {j + 1}, {bv['x']['field']} class {i + 1}" for j in range(n) for i in range(n)]
    wd = style.get("widths") if kind == "line" else None
    items = [{"label": lab, "color": col, "stroke": style.get("stroke"), "shape": sd.class_shape(style, i),
              "width": (wd[i] if wd and i < len(wd) else style.get("width")),
              "opacity": style.get("opacity", 1.0)} for i, (col, lab) in enumerate(zip(cols, labs, strict=True)) if col or kind == "polygon"]
    if style.get("mode") != "single" and style.get("nodata"):
        items.append({"label": style.get("nodata_label", sd.NODATA_LABEL), "color": style["nodata_color"], "stroke": style.get("stroke"),
                      "shape": style.get("shape", "circle"), "width": style.get("width"), "opacity": style.get("opacity", 1.0)})
    title = style.get("title") or style.get("layer") or ""
    if style.get("mode") == "single" and len(items) == 1:
        items[0]["label"] = title
        title = ""
    return {"title": title, "kind": kind, "items": items}


# ---------------------------------------------------------------------------------------------------------------- KML text
def _x(s) -> str:
    return html.escape(str(s), quote=True)


def _num(v: float) -> str:
    t = f"{v:.7f}".rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _coords(seq) -> str:
    return " ".join(f"{_num(c[0])},{_num(c[1])}" for c in seq)


def geom_kml(g) -> str:
    if g is None or g.is_empty:
        return ""
    t = g.geom_type
    if t == "Point":
        return f"<Point><coordinates>{_num(g.x)},{_num(g.y)}</coordinates></Point>"
    if t in ("LineString", "LinearRing"):
        return f"<LineString><tessellate>1</tessellate><coordinates>{_coords(g.coords)}</coordinates></LineString>"
    if t == "Polygon":
        inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_coords(r.coords)}</coordinates></LinearRing></innerBoundaryIs>" for r in g.interiors)
        return (f"<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>{_coords(g.exterior.coords)}</coordinates></LinearRing>"
                f"</outerBoundaryIs>{inner}</Polygon>")
    parts = [geom_kml(p) for p in g.geoms]
    return "<MultiGeometry>" + "".join(p for p in parts if p) + "</MultiGeometry>"


def _kml_type(series) -> str:
    import pandas as pd
    if pd.api.types.is_bool_dtype(series):
        return "bool"
    if pd.api.types.is_integer_dtype(series):
        return "int"
    if pd.api.types.is_float_dtype(series):
        return "double"
    return "string"


def _value(v, typ: str) -> str | None:
    if sd.value_text(v) == "":
        return None
    if typ == "bool":
        return "1" if bool(v) else "0"
    if typ == "int":
        return str(int(v))
    if typ == "double":
        x = float(v)
        return repr(x) if math.isfinite(x) else None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s)).strip("_") or "layer"


def _bivariate_classes(g, bv: dict):
    """(class index per row, n * n, the colours): a feature's class is y * n + x on the two fields' shared edges, -1 when either is missing."""
    import numpy as np
    import pandas as pd
    n = int(bv["n"])

    def axis(a):
        e = [float(x) for x in bv[a]["edges"]]
        v = pd.to_numeric(g[bv[a]["field"]], errors="coerce").to_numpy(dtype="float64", na_value=np.nan) if bv[a]["field"] in g.columns else np.full(len(g), np.nan)
        out = np.full(len(g), -1, dtype=int)
        ok = np.isfinite(v)
        out[ok] = np.clip(np.searchsorted(np.array(e[1:-1]), v[ok], side="right"), 0, n - 1)
        return out
    xi, yi = axis("x"), axis("y")
    return np.where((xi >= 0) & (yi >= 0), yi * n + xi, -1), n * n, list(bv["colors"])[: n * n]


def _style_xml(sid: str, st: dict, color, *, icon_href: str | None, balloon: str | None, label_scale: float) -> str:
    kind = st["geometry"]
    op = float(st.get("opacity", 1.0))
    parts = [f'<Style id="{_x(sid)}">']
    if kind == "point":
        parts.append(f"<IconStyle><scale>{icon_scale(st['size'])}</scale><Icon><href>{_x(icon_href)}</href></Icon>"
                     '<hotSpot x="0.5" y="0.5" xunits="fraction" yunits="fraction"/></IconStyle>')
    parts.append(f"<LabelStyle><scale>{label_scale:g}</scale></LabelStyle>")
    if kind == "line":
        parts.append(f"<LineStyle><color>{sd.kml_color(color, op)}</color><width>{width_px(st['width'])}</width></LineStyle>")
    elif kind == "polygon":
        stroke = st.get("stroke")
        if stroke:
            parts.append(f"<LineStyle><color>{sd.kml_color(stroke)}</color><width>{width_px(st['width'])}</width></LineStyle>")
        fill = color is not None
        pc = sd.kml_color(color, op) if fill else "00ffffff"
        parts.append(f"<PolyStyle><color>{pc}</color><fill>{1 if fill else 0}</fill><outline>{1 if stroke else 0}</outline></PolyStyle>")
    if balloon:
        parts.append(f"<BalloonStyle><text><![CDATA[{balloon}]]></text></BalloonStyle>")
    parts.append("</Style>")
    return "".join(parts)


def _balloon(schema: str, fields: list[str], has_name: bool, has_desc: bool) -> str:
    rows = "".join(f"<tr><td><b>{html.escape(f)}</b></td><td>$[{schema}/{f}]</td></tr>" for f in fields)
    head = "<h3>$[name]</h3>" if has_name else ""
    desc = "<p>$[description]</p>" if has_desc else ""
    return (head + desc + f"<table>{rows}</table>").replace("]]>", "]]&gt;")


def _prep_layer(name: str, gdf, style: dict | None, tokens: dict | None, notes: list):
    import geo_io
    if gdf.crs is None:
        raise KmlStyleError(f"layer '{name}' has no CRS, and KML needs WGS84. Say which CRS it uses first")
    g = geo_io._drop_zm(gdf, [], "on writing")
    if not g.crs.equals("EPSG:4326"):
        g = g.to_crs("EPSG:4326")
    try:
        st = sd.resolve(style or {}, g, tokens)
    except sd.StyleError as ex:
        raise KmlStyleError(f"layer '{name}': {ex}") from None
    return g, st


def build_kml(items: list[tuple], *, title: str | None = None, tokens: dict | None = None, legend: bool = False, icon_dir: str = "files",
              hidden: dict | None = None) -> tuple[str, dict, dict]:
    """(KML text, {relative path: bytes} of icons and the legend, report). items: [(layer name, GeoDataFrame, style dict)] in draw order."""
    notes: list[str] = []
    files: dict[str, bytes] = {}
    styles_xml: list[str] = []
    schemas_xml: list[str] = []
    folders_xml: list[str] = []
    report: dict = {"layers": {}, "notes": notes}
    used: set[str] = set()
    legend_secs: list[dict] = []
    for name, gdf, style in items:
        lid = _safe_id(name)
        if lid in used:
            raise KmlStyleError(f"two layers are named '{lid}'; rename one")
        used.add(lid)
        g, st = _prep_layer(name, gdf, style, tokens, notes)
        st.setdefault("title", str(name))
        geom = g.geometry.name
        cols = [c for c in g.columns if c != geom]
        name_col = st.get("name_field") or next((c for c in ("name", "Name") if c in cols), None)
        desc_col = "description" if "description" in cols else None
        data_cols = [c for c in cols if c not in (name_col if name_col in ("name", "Name") else None, desc_col)]
        types = {c: _kml_type(g[c]) for c in data_cols}
        schema = lid
        schemas_xml.append(f'<Schema name="{_x(schema)}" id="{_x(schema)}_schema">'
                           + "".join(f'<SimpleField type="{types[c]}" name="{_x(c)}"/>' for c in data_cols) + "</Schema>")
        bfields = list(st.get("balloon_fields") or data_cols)
        if len(bfields) > BALLOON_MAX_FIELDS:
            notes.append(f"layer '{name}': the balloon shows the first {BALLOON_MAX_FIELDS} of {len(bfields)} fields; pick them with balloon_fields")
            bfields = bfields[:BALLOON_MAX_FIELDS]
        bfields = [f for f in bfields if f in data_cols]
        balloon = _balloon(schema, bfields, name_col is not None, desc_col is not None)
        lab_scale = 0.8 if (st.get("label") or st.get("name_field")) else 0.0
        if st.get("label") and st["label"].get("field") in cols and not name_col:
            name_col = st["label"]["field"]
        bv = st.get("bivariate")
        if bv and bv.get("colors"):              # 0.24.0 (TODO M15, M16): a bivariate layer keeps its n x n classes, one style each (y * n + x)
            assigned, k, cols_k = _bivariate_classes(g, bv)
        else:
            assigned = sd.assign(g, st)
            k = sd.class_count(st)
            cols_k = sd.class_colors(st)
        widths = st.get("widths") if st["geometry"] == "line" else None    # 0.24.0: graduated line widths, one per class
        prefix = lid
        for i in range(k):
            href = None
            if st["geometry"] == "point":
                href = f"{icon_dir}/{prefix}-c{i}.png"
                files[href] = icon_png(sd.class_shape(st, i), cols_k[i] or st["nodata_color"], st.get("stroke"),
                                       outline_share=min(0.2, max(0.06, st["width"] / max(st["size"], 0.1) / 2)))
            st_i = {**st, "width": float(widths[i])} if widths and i < len(widths) else st
            styles_xml.append(_style_xml(f"{prefix}-c{i}", st_i, cols_k[i], icon_href=href, balloon=balloon, label_scale=lab_scale))
        nod = int((assigned < 0).sum()) if st["mode"] != "single" else 0
        if nod:
            href = None
            if st["geometry"] == "point":
                href = f"{icon_dir}/{prefix}-nodata.png"
                files[href] = icon_png(st.get("shape", "circle"), st["nodata_color"], st.get("stroke"))
            styles_xml.append(_style_xml(f"{prefix}-nodata", st, st["nodata_color"], icon_href=href, balloon=balloon, label_scale=lab_scale))
        st["nodata"] = nod
        meta = {k2: v for k2, v in st.items() if k2 not in ("value_range",)}
        meta["style_prefix"] = prefix
        pms: list[str] = []
        names = g[name_col].tolist() if name_col else None
        descs = g[desc_col].tolist() if desc_col else None
        prop = st.get("proportional")
        pvals = g[prop["field"]].tolist() if prop else None
        rows = g[data_cols].to_dict("records") if data_cols else [{}] * len(g)
        geoms = g.geometry.values
        n_written = 0
        for j in range(len(g)):
            gx = geom_kml(geoms[j])
            if not gx:
                continue
            ci = int(assigned[j])
            sid = f"{prefix}-c{ci}" if ci >= 0 else f"{prefix}-nodata"
            p = ["<Placemark>"]
            if names is not None and sd.value_text(names[j]) != "":
                p.append(f"<name>{_x(names[j])}</name>")
            if descs is not None and sd.value_text(descs[j]) != "":
                p.append(f"<description>{_x(descs[j])}</description>")
            p.append(f"<styleUrl>#{_x(sid)}</styleUrl>")
            if prop:
                d = sd.proportional_size(st, pvals[j])
                if d > 0:
                    p.append(f"<Style><IconStyle><scale>{icon_scale(d)}</scale></IconStyle></Style>")
            vals = [(c, _value(rows[j].get(c), types[c])) for c in data_cols]
            sdata = "".join(f'<SimpleData name="{_x(c)}">{_x(v)}</SimpleData>' for c, v in vals if v is not None)
            if sdata:
                p.append(f'<ExtendedData><SchemaData schemaUrl="#{_x(schema)}_schema">{sdata}</SchemaData></ExtendedData>')
            p.append(gx)
            p.append("</Placemark>")
            pms.append("".join(p))
            n_written += 1
        folder_name = st.get("title") or name
        # each layer is a Document of its own inside the root one, holding its Schema: GDAL's LIBKML reader (QGIS opens a KMZ with it) finds
        # the typed fields of a KMZ only that way, and Google Earth shows it as a folder
        folders_xml.append(f'<Document id="{_x(lid)}"><name>{_x(folder_name)}</name><open>1</open>'
                           f'<ExtendedData><gmap:style>{_x(json.dumps(meta, separators=(",", ":"), default=str))}</gmap:style></ExtendedData>'
                           + schemas_xml.pop() + "".join(pms) + "</Document>")
        report["layers"][name] = {"features": n_written, "classes": k, "nodata": nod, "mode": st["mode"], "geometry": st["geometry"],
                                  "icons": sorted(f for f in files if f.startswith(f"{icon_dir}/{prefix}-")), "style": st}
        if len(g) != n_written:
            notes.append(f"layer '{name}': {len(g) - n_written} feature(s) with no geometry were left out")
        legend_secs.append(legend_section(st))
    overlay = ""
    if legend:
        png, (w, h) = legend_png(legend_secs, tokens)
        href = f"{icon_dir}/legend.png"
        files[href] = png
        # in a Document of its own, after the layers: a reader that lists layers (GDAL, QGIS) sees an empty "Legend" last, not an empty
        # first layer in front of the data
        overlay = (f'<Document id="legend"><name>Legend</name><ScreenOverlay id="legend-overlay"><name>Legend</name><Icon><href>{href}</href></Icon>'
                   '<overlayXY x="0" y="0" xunits="fraction" yunits="fraction"/><screenXY x="10" y="30" xunits="pixels" yunits="pixels"/>'
                   f'<size x="{w}" y="{h}" xunits="pixels" yunits="pixels"/></ScreenOverlay></Document>')
        report["legend"] = href
    hid = ""
    if hidden:
        hid = "<ExtendedData>" + "".join(f"<gmap:{k}>{_x(v)}</gmap:{k}>" for k, v in hidden.items() if v) + "</ExtendedData>"
    doc_name = title or (items[0][0] if len(items) == 1 else "Map data")
    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<kml xmlns="{KML_NS}" xmlns:gmap="{GMAP_NS}"><Document id="root"><name>{_x(doc_name)}</name><open>1</open>'
           + "".join(styles_xml) + hid + "".join(folders_xml) + overlay + "</Document></kml>\n")
    return kml, files, report


def _zip(dest: Path, entries: dict[str, bytes]) -> None:
    """A deterministic zip: doc.kml first, then the rest sorted, fixed timestamps."""
    order = sorted(entries, key=lambda n: (n != "doc.kml", n))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in order:
            zi = zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, entries[n])


def write_layers(items: list[tuple], path, *, title: str | None = None, tokens: dict | None = None, legend: bool = False,
                 hidden: dict | None = None) -> dict:
    """Write several layers (draw order) to one KML or KMZ. Returns {'path', 'layers', 'files', 'notes', 'legend'}."""
    p = Path(path)
    kmz = p.suffix.lower() == ".kmz"
    if not kmz and p.suffix.lower() != ".kml":
        raise KmlStyleError("a styled KML is written as .kml or .kmz")
    icon_dir = "files" if kmz else f"{p.stem}_files"
    if hidden is None:
        ident = sd.plugin_ident()
        hidden = {"generator": sd.generator_text(), "accreditation": ident["house"]}
    kml, files, rep = build_kml(items, title=title, tokens=tokens, legend=legend, icon_dir=icon_dir, hidden=hidden)
    p.parent.mkdir(parents=True, exist_ok=True)
    if kmz:
        _zip(p, {"doc.kml": kml.encode("utf-8"), **files})
    else:
        p.write_text(kml, encoding="utf-8")
        d = p.parent / icon_dir
        if files:
            if d.exists():
                shutil.rmtree(d)
            d.mkdir()
            for rel, data in files.items():
                (p.parent / rel).write_bytes(data)
            rep["notes"].append(f"a .kml cannot carry its icons: they are in {icon_dir}/ beside it (send both, or ask for a KMZ, which holds them)")
    rep["path"] = str(p)
    rep["files"] = sorted(files)
    return rep


def write(gdf, path, style: dict | None, *, layer: str | None = None, tokens: dict | None = None, legend: bool = False, title: str | None = None):
    """One styled layer as KML or KMZ, for geo-convert. Returns a geo_io.WriteResult (the notes carry what the styling decided)."""
    import geo_io
    p = Path(path)
    name = layer or p.stem
    notes: list = []
    if gdf.crs is not None and not gdf.crs.equals("EPSG:4326"):
        notes.append(geo_io.Note("IO", "INFO", f"reprojected from {gdf.crs.to_string()} to EPSG:4326 (KML requires WGS84)"))
    rep = write_layers([(name, gdf, style)], p, title=title or name, tokens=tokens, legend=legend)
    lr = rep["layers"][name]
    st = lr["style"]
    msg = f"styled KML: {st['mode']} style" + (f" on '{st['field']}'" if st.get("field") else "") + f", {lr['classes']} class(es)"
    if lr["icons"]:
        msg += f", {len(lr['icons'])} point icon(s)" + (" in the KMZ" if p.suffix.lower() == ".kmz" else "")
    if lr["nodata"]:
        msg += f", {lr['nodata']} feature(s) in the no-data style"
    notes.append(geo_io.Note("MAP-OUT-010", "INFO", msg, {"style": st}))
    notes += [geo_io.Note("IO", "INFO", n) for n in rep["notes"]]
    return geo_io.WriteResult(path=p, format="kmz" if p.suffix.lower() == ".kmz" else "kml", features=lr["features"], crs="EPSG:4326", notes=notes)


# ---------------------------------------------------------------------------------------------------------------- read back
def _read(path: Path) -> tuple[bytes, dict[str, bytes]]:
    if path.suffix.lower() == ".kmz":
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise KmlStyleError(f"{path.name} is not a valid KMZ") from e
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > 5000 or any(i.file_size > KMZ_MAX_ENTRY for i in infos):
                raise KmlStyleError(f"{path.name} is too large to read styles from")
            names = [i.filename for i in infos]
            kmls = sorted((n for n in names if n.lower().endswith(".kml")), key=lambda n: (n != "doc.kml", n))
            if not kmls:
                raise KmlStyleError(f"{path.name} holds no .kml file")
            pngs = {n: zf.read(n) for n in names if n.lower().endswith(".png")}
            return zf.read(kmls[0]), pngs
    if path.stat().st_size > MAX_KML_BYTES:
        raise KmlStyleError(f"{path.name} is too large to read styles from")
    pngs = {}
    return path.read_bytes(), pngs


def _ln(el) -> str:
    t = el.tag
    return t.split("}", 1)[1] if isinstance(t, str) and "}" in t else (t if isinstance(t, str) else "")


def _child(el, name: str):
    for c in el:
        if _ln(c) == name:
            return c
    return None


def _text(el, *path, default=None):
    cur = el
    for name in path:
        cur = _child(cur, name) if cur is not None else None
    return (cur.text or "").strip() if cur is not None and cur.text is not None else default


def _containers(root) -> list:
    """The layers of a KML: every Document or Folder that holds placemarks itself (this plugin writes one Document per layer; other tools
    write Folders), else the root Document."""
    found = [el for el in root.iter() if _ln(el) in ("Document", "Folder") and any(_ln(c) == "Placemark" for c in el)]
    if found:
        return found
    return [next((el for el in root.iter() if _ln(el) == "Document"), root)]


def _png_color(data: bytes | None) -> str | None:
    if not data:
        return None
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = im.size
    r, g, b, a = im.getpixel((w // 2, h // 2))
    return sd.hex_rgb((r, g, b)) if a else None


def read_styles(path) -> dict:
    """{layer: style dict} from a KML or KMZ: with this plugin's metadata, the field, classes and labels it recorded and the colours, widths,
    sizes and opacity the <Style> elements now hold; from another KML, one class per style its placemarks use."""
    from lxml import etree
    p = Path(path)
    raw, pngs = _read(p)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(raw, parser)
    styles: dict[str, dict] = {}
    maps: dict[str, str] = {}
    for el in root.iter():
        nm = _ln(el)
        if nm == "Style" and el.get("id"):
            s: dict = {}
            ic = _child(el, "IconStyle")
            if ic is not None:
                s["icon_scale"] = float(_text(ic, "scale", default="1") or 1)
                s["icon_href"] = _text(ic, "Icon", "href")
            ls = _child(el, "LineStyle")
            if ls is not None:
                s["line_color"] = _text(ls, "color", default="ff000000")
                s["line_width"] = float(_text(ls, "width", default="1") or 1)
            ps = _child(el, "PolyStyle")
            if ps is not None:
                s["poly_color"] = _text(ps, "color", default="ffffffff")
                s["fill"] = _text(ps, "fill", default="1") != "0"
                s["outline"] = _text(ps, "outline", default="1") != "0"
            styles[el.get("id")] = s
        elif nm == "StyleMap" and el.get("id"):
            for pair in el:
                if _ln(pair) == "Pair" and _text(pair, "key") == "normal":
                    maps[el.get("id")] = (_text(pair, "styleUrl") or "").lstrip("#")
    containers = _containers(root)
    out: dict = {}
    for fol in containers:
        meta = None
        ed = _child(fol, "ExtendedData")
        if ed is not None:
            for c in ed:
                if _ln(c) == "style" and (c.tag.startswith("{" + GMAP_NS + "}")):
                    try:
                        meta = json.loads(c.text or "{}")
                    except json.JSONDecodeError:
                        meta = None
        lname = fol.get("id") or _text(fol, "name") or p.stem
        used: list[str] = []
        kinds: dict[str, int] = {}
        for pm in (c for c in fol if _ln(c) == "Placemark"):
            su = (_text(pm, "styleUrl") or "").lstrip("#")
            su = maps.get(su, su)
            if su and su not in used:
                used.append(su)
            for g in pm.iter():
                gn = _ln(g)
                if gn in ("Point", "LineString", "Polygon"):
                    k = {"Point": "point", "LineString": "line", "Polygon": "polygon"}[gn]
                    kinds[k] = kinds.get(k, 0) + 1
        geom = meta.get("geometry") if meta else (max(kinds, key=kinds.get) if kinds else "point")

        def col_of(sid: str):
            s = styles.get(sid) or {}
            if geom == "polygon":
                if not s.get("fill", True):
                    return None, 1.0
                return sd.from_kml_color(s.get("poly_color", "ffffffff"))
            if geom == "line":
                return sd.from_kml_color(s.get("line_color", "ff000000"))
            c = _png_color(pngs.get(s.get("icon_href") or ""))
            return c, 1.0

        if meta:
            st = {k: v for k, v in meta.items() if k != "style_prefix"}
            prefix = meta.get("style_prefix") or lname
            n = sd.class_count(st)
            sids = [f"{prefix}-c{i}" for i in range(n)]
        else:
            prefix = None
            sids = used
            st = {"geometry": geom, "title": _text(fol, "name") or lname}
            if len(sids) <= 1:
                st["mode"] = "single"
            else:
                st.update(mode="categorical", field=None, categories=list(sids), labels=list(sids))
        cols, ops = [], []
        for i, sid in enumerate(sids):
            c, op = col_of(sid)
            if c is None and meta and geom == "point":          # icons beside a .kml that are not at hand: the recorded colour
                recorded = sd.class_colors(st)
                c = recorded[i] if i < len(recorded) else None
            cols.append(c)
            ops.append(op)
        if st.get("mode") == "single":
            st["color"] = cols[0] if cols else None
        else:
            st["colors"] = cols
        first = styles.get(sids[0]) if sids else None
        if first:
            if geom == "polygon":
                st["opacity"] = ops[0] if st.get("color") is not None or st.get("colors") else st.get("opacity", 1.0)
                if first.get("outline", True) and "line_color" in first:
                    st["stroke"] = sd.from_kml_color(first["line_color"])[0]
                    st["width"] = _mm_from_width(first["line_width"], (meta or {}).get("width"))
                else:
                    st["stroke"] = None
            elif geom == "line":
                st["opacity"] = ops[0]
                st["width"] = _mm_from_width(first.get("line_width", 1.0), (meta or {}).get("width"))
            elif "icon_scale" in first:
                st["size"] = _mm_from_scale(first["icon_scale"], (meta or {}).get("size"))
        nd = styles.get(f"{prefix}-nodata") if prefix else None
        if nd:
            st["nodata_color"] = col_of(f"{prefix}-nodata")[0] or st.get("nodata_color")
        out[lname] = st
    return out


def compare(written: dict, back: dict, where: str) -> list[dict]:
    """MAP-OUT-010: the style read back from the file matches the style written (mode, field, classes, labels, colours, sizes)."""
    out = []
    for k in ("mode", "field", "categories", "edges", "labels", "geometry"):
        if written.get(k) != back.get(k):
            out.append(finding("MAP-OUT-010", "BLOCK", f"{where}: the style's {k} did not survive the round trip ({written.get(k)!r} became {back.get(k)!r})"))
    wc = [str(c).upper() if c else None for c in sd.class_colors(written)]
    bc = [str(c).upper() if c else None for c in sd.class_colors(back)]
    if wc != bc:
        out.append(finding("MAP-OUT-010", "BLOCK", f"{where}: the class colours did not survive the round trip", {"written": wc, "read": bc}))
    for k in ("width", "size", "opacity"):
        if k in written and k in back and abs(float(written[k]) - float(back[k])) > 1e-3:
            out.append(finding("MAP-OUT-010", "WARN", f"{where}: the {k} came back as {back[k]} (written {written[k]})"))
    return out


def check(path, expected: dict, *, legend: bool = False) -> list[dict]:
    """MAP-OUT-010 on a styled KML/KMZ: every expected layer ({layer: features}) is a folder with that many placemarks, every class has a
    style, every point class has its icon inside the KMZ, and the legend overlay is there when asked for."""
    p = Path(path)
    fs: list[dict] = []
    try:
        styles = read_styles(p)
        raw, pngs = _read(p)
    except Exception as ex:  # noqa: BLE001 - an unreadable file is the finding
        return [finding("MAP-OUT-010", "BLOCK", f"{p.name}: the styles could not be read back: {str(ex)[:160]}")]
    from lxml import etree
    root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    counts = {}
    for fol in _containers(root):
        counts[fol.get("id") or _text(fol, "name")] = sum(1 for c in fol if _ln(c) == "Placemark")
    ids = {el.get("id") for el in root.iter() if _ln(el) == "Style"}
    hrefs = {(_text(el, "Icon", "href") or "") for el in root.iter() if _ln(el) == "IconStyle" and _child(el, "Icon") is not None}
    for lname, n in expected.items():
        lid = _safe_id(lname)
        if lid not in counts:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: layer '{lname}' has no folder"))
            continue
        if counts[lid] != n:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: layer '{lname}' has {counts[lid]} placemarks for {n} features"))
        st = styles.get(lid) or {}
        k = sd.class_count(st) if st.get("mode") else 0
        miss = [i for i in range(k) if f"{lid}-c{i}" not in ids]
        if miss:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: layer '{lname}' has no style for class(es) {', '.join(str(i + 1) for i in miss)}"))
    if p.suffix.lower() == ".kmz":
        lost = sorted(h for h in hrefs if h and h not in pngs)
        if lost:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: icon(s) named but not in the KMZ: {', '.join(lost[:5])}", lost))
    if legend and not any(_ln(el) == "ScreenOverlay" for el in root.iter()):
        fs.append(finding("MAP-OUT-010", "WARN", f"{p.name}: the legend overlay is missing"))
    if not fs:
        fs.append(finding("MAP-OUT-010", "INFO", f"{p.name}: {len(counts)} styled layer(s), {len(ids)} style(s)" + (f", {len(pngs)} image(s) inside" if pngs else "")))
    return fs
