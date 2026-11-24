#!/usr/bin/env python3
"""qgis_build_layout: build a QGIS print layout from a kit written by qgis_layout.py (spec I.16). EXPERIMENTAL.

    <qgis python> qgis_build_layout.py KIT_DIR OUT_DIR FORMATS [DPI]

This file is static. It reads KIT_DIR/layout.json and KIT_DIR/layers.gpkg and nothing else: no network, no shell, no
credentials, no code from the kit. Every number, color and string it draws was decided by the plugin and written into
layout.json, so the QGIS map and the SVG map are the same map. It writes OUT_DIR/layout.<format> for each format asked,
KIT_DIR/layout.qgz (an editable project with the layout in it) and OUT_DIR/build_result.json.

It must run under the Python that has PyQGIS (QGIS 3.28 or later); the plugin's own Python usually does not.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PT_MM = 25.4 / 72


def _unit(name: str):
    from qgis.core import Qgis
    ru = getattr(Qgis, "RenderUnit", None)
    if ru is not None and hasattr(ru, name):
        return getattr(ru, name)
    from qgis.core import QgsUnitTypes
    return getattr(QgsUnitTypes, "Render" + name)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    kit, out = Path(argv[1]), Path(argv[2])
    formats = [f for f in argv[3].split(",") if f]
    dpi = float(argv[4]) if len(argv) > 4 else 300.0
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qgis.core import (Qgis, QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsFillSymbol, QgsLayoutExporter,
                           QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutItemScaleBar,
                           QgsLayoutItemShape,
                           QgsLayoutPoint, QgsLayoutSize, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsPrintLayout, QgsProject,
                           QgsProperty, QgsRasterLayer, QgsRectangle, QgsRendererCategory, QgsSingleSymbolRenderer, QgsSymbol, QgsTextFormat,
                           QgsVectorLayer, QgsVectorLayerSimpleLabeling)
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase

    spec = json.loads((kit / "layout.json").read_text(encoding="utf-8"))
    gpkg = kit / "layers.gpkg"
    result: dict = {"qgis": Qgis.version(), "warnings": [], "files": [], "items": []}

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        families = set(QFontDatabase().families())
        font_name = spec["font"]["family"]
        if font_name not in families:
            result["warnings"].append(f"font '{font_name}' is not installed for QGIS; text falls back to another face")
        result["font_installed"] = font_name in families

        project = QgsProject.instance()
        crs = QgsCoordinateReferenceSystem()
        crs.createFromWkt(spec["crs_wkt"])
        if not crs.isValid():
            raise RuntimeError("the kit's coordinate system is not valid for QGIS")
        project.setCrs(crs)

        # ---- layers
        shape_names = {"circle": "Circle", "square": "Square", "triangle": "Triangle", "diamond": "Diamond", "cross": "CrossFill", "star": "Star",
                       "plus": "CrossFill", "x": "Cross2"}

        def marker_shape(name: str):
            from qgis.core import QgsSimpleMarkerSymbolLayerBase as B
            n = shape_names.get(str(name), "Circle")
            ms = getattr(Qgis, "MarkerShape", None)
            return getattr(ms, n) if ms is not None and hasattr(ms, n) else getattr(B, n)

        def symbol_for(layer: QgsVectorLayer, kind: str, fill: str | None, stroke: str | None, width_mm: float, opacity: float, size_mm: float, shape: str,
                       size_expr: str | None = None) -> QgsSymbol:
            if kind == "polygon":
                s = QgsFillSymbol.createSimple({"color": fill or "0,0,0,0", "outline_color": stroke or "0,0,0,0", "outline_width": str(width_mm),
                                                "outline_width_unit": "MM", "joinstyle": "round"})
            elif kind == "line":
                s = QgsLineSymbol.createSimple({"line_color": stroke or spec["colors"]["label"], "line_width": str(width_mm), "line_width_unit": "MM",
                                                "capstyle": "round", "joinstyle": "round"})
            else:
                s = QgsMarkerSymbol.createSimple({"color": fill or spec["colors"]["label"], "outline_color": stroke or spec["colors"]["label"], "outline_width": "0.12",
                                                  "outline_width_unit": "MM", "size": str(size_mm), "size_unit": "MM"})
                sl = s.symbolLayer(0)
                sl.setShape(marker_shape(shape))
                if shape_names.get(str(shape)) == "Cross2":                 # a line shape draws with its outline, so the outline takes the class color
                    sl.setStrokeColor(QColor(fill or spec["colors"]["label"]))
                    sl.setStrokeWidth(0.4)
                if size_expr:
                    sl.setDataDefinedProperty(sl.PropertySize, QgsProperty.fromExpression(size_expr))
            s.setOpacity(float(opacity))
            return s

        qlayers = {}
        # ---- the ground and the pictures (1.11.0): land, water, border lines; the relief, raster windows and prepared tiles as raster layers
        pics = {p["id"]: p for p in spec.get("pictures") or []}
        for p in pics.values():
            rl = QgsRasterLayer(str(kit / p["file"]), p["title"], "gdal")
            if not rl.isValid():
                result["warnings"].append(f"the picture '{p['id']}' could not be read; it is not in the layout")
                continue
            rl.setCrs(crs)
            if float(p.get("opacity", 1.0)) < 1:
                rl.renderer().setOpacity(float(p["opacity"]))
            project.addMapLayer(rl)
            qlayers[p["id"]] = rl
        for G in spec.get("ground") or []:
            if G.get("picture"):
                continue
            gl_ = QgsVectorLayer(f"{gpkg}|layername={G['table']}", G["title"], "ogr")
            if not gl_.isValid():
                raise RuntimeError(f"ground layer '{G['id']}' could not be read from layers.gpkg")
            if G["kind"] == "line":
                gs = QgsLineSymbol.createSimple({"line_color": G["stroke"], "line_width": str(G["stroke_w_mm"]), "line_width_unit": "MM", "capstyle": "round",
                                                 "joinstyle": "round"})
            else:
                gs = QgsFillSymbol.createSimple({"color": G["fill"], "outline_color": G.get("stroke") or "0,0,0,0", "outline_width": str(G["stroke_w_mm"]),
                                                 "outline_width_unit": "MM", "outline_style": "solid" if G.get("stroke") else "no", "joinstyle": "round"})
            gl_.setRenderer(QgsSingleSymbolRenderer(gs))
            project.addMapLayer(gl_)
            qlayers[G["id"]] = gl_
        if spec.get("ground") or pics:
            result["items"].append("ground")
        for L in spec["layers"]:
            vl = QgsVectorLayer(f"{gpkg}|layername={L['table']}", L["title"], "ogr")
            if not vl.isValid():
                raise RuntimeError(f"layer '{L['id']}' could not be read from layers.gpkg")
            kind = L["kind"]
            size = float(L.get("size_mm", 3.0) * 1.0)
            if L.get("classes"):
                cats = []
                for i, c in enumerate(L["classes"]):
                    sym = symbol_for(vl, kind, c["color"], L.get("stroke"), L["stroke_w_mm"], L["opacity"], size, c.get("shape") or L.get("shape", "circle"))
                    cats.append(QgsRendererCategory(i, sym, c["label"]))
                nd = L.get("nodata")
                if nd:
                    sym = symbol_for(vl, kind, nd["color"], L.get("stroke"), L["stroke_w_mm"], L["opacity"], size, nd.get("shape") or L.get("shape", "circle"))
                    cats.append(QgsRendererCategory(-1, sym, nd["label"]))
                vl.setRenderer(QgsCategorizedSymbolRenderer("_cls", cats))
            else:
                sym = symbol_for(vl, kind, L.get("fill"), L.get("stroke"), L["stroke_w_mm"], L["opacity"], size, L.get("shape", "circle"), L.get("size_expr"))
                vl.setRenderer(QgsSingleSymbolRenderer(sym))
            lab = L.get("label")
            if lab:
                ps = QgsPalLayerSettings()
                ps.fieldName = lab["field"]
                tf = QgsTextFormat()
                f = QFont(font_name)
                tf.setFont(f)
                tf.setSize(float(lab["size_pt"]))
                tf.setSizeUnit(_unit("Points"))
                tf.setColor(QColor(spec["colors"]["label"]))
                buf = tf.buffer()
                buf.setEnabled(True)
                buf.setSize(0.8)
                buf.setSizeUnit(_unit("Millimeters"))
                buf.setColor(QColor(spec["colors"]["background"]))
                tf.setBuffer(buf)
                ps.setFormat(tf)
                vl.setLabeling(QgsVectorLayerSimpleLabeling(ps))
                vl.setLabelsEnabled(True)
            project.addMapLayer(vl)
            qlayers[L["id"]] = vl

        # ---- the plugin's own label placements (1.11.0): a point per label, drawn at its point with its own size, colours, side and angle
        PL = spec.get("placed_labels")
        OL = (PL or {}).get("outlines")
        if OL:                                             # 1.12.0: labels in a script's own face, as their letter shapes (no font needed)
            ol = QgsVectorLayer(f"{gpkg}|layername={OL['table']}", "Labels (outlined)", "ogr")
            if ol.isValid():
                from qgis.core import QgsSimpleFillSymbolLayer
                sym = QgsFillSymbol.createSimple({"color": spec["colors"]["label"], "outline_style": "no"})
                top = sym.symbolLayer(0)
                top.setDataDefinedProperty(top.PropertyFillColor, QgsProperty.fromField("color"))
                under = QgsSimpleFillSymbolLayer()
                under.setStrokeWidthUnit(_unit("Millimeters"))
                under.setDataDefinedProperty(under.PropertyFillColor, QgsProperty.fromExpression('if(length("halo") > 0, "halo", \'0,0,0,0\')'))
                under.setDataDefinedProperty(under.PropertyStrokeColor, QgsProperty.fromExpression('if(length("halo") > 0, "halo", \'0,0,0,0\')'))
                under.setDataDefinedProperty(under.PropertyStrokeWidth, QgsProperty.fromExpression('"halo_mm" * 2'))
                sym.insertSymbolLayer(0, under)
                ol.setRenderer(QgsSingleSymbolRenderer(sym))
                project.addMapLayer(ol)
                qlayers["__outlines"] = ol
                result["items"].append("label_outlines")
        if PL and PL.get("table"):
            ll = QgsVectorLayer(f"{gpkg}|layername={PL['table']}", "Labels", "ogr")
            if not ll.isValid():
                result["warnings"].append("the placed labels could not be read; the map has no labels")
            else:
                from qgis.core import QgsNullSymbolRenderer
                ll.setRenderer(QgsNullSymbolRenderer())
                ps = QgsPalLayerSettings()
                ps.fieldName = "text"
                tf = QgsTextFormat()
                tf.setFont(QFont(font_name))
                tf.setSize(7.0)
                tf.setSizeUnit(_unit("Points"))
                buf = tf.buffer()
                buf.setEnabled(True)
                buf.setSizeUnit(_unit("Millimeters"))
                tf.setBuffer(buf)
                ps.setFormat(tf)
                ps.displayAll = True
                P_ = QgsPalLayerSettings
                props = ps.dataDefinedProperties()
                for key, expr in ((P_.Size, '"size_pt"'), (P_.Color, '"color"'), (P_.BufferColor, '"halo"'), (P_.BufferSize, '"halo_mm"'),
                                  (P_.BufferDraw, 'length("halo") > 0'), (P_.PositionX, '"x"'), (P_.PositionY, '"y"'), (P_.Hali, '"hali"'),
                                  (P_.Vali, "'Base'"), (P_.LabelRotation, '"rotation"'), (P_.Bold, '"weight" >= 600'), (P_.Italic, '"italic" = 1'),
                                  (P_.FontLetterSpacing, '"spacing_pt"')):
                    props.setProperty(key, QgsProperty.fromExpression(expr))
                ps.setDataDefinedProperties(props)
                ll.setLabeling(QgsVectorLayerSimpleLabeling(ps))
                ll.setLabelsEnabled(True)
                project.addMapLayer(ll)
                qlayers["__labels"] = ll
                result["items"].append("labels")
                result["labels"] = int(PL["count"])

        # ---- page and map
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(spec["name"])
        W, H = spec["page_mm"]
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(W, H, _layout_unit()))
        layout.renderContext().setDpi(dpi)

        def place(item, x, y, w, h, ref=None):
            item.attemptResize(QgsLayoutSize(w, h, _layout_unit()))
            if ref is not None:
                item.setReferencePoint(ref)
            item.attemptMove(QgsLayoutPoint(x, y, _layout_unit()))

        def tfmt(pt: float, color: str, bold: bool = False):
            tf = QgsTextFormat()
            f = QFont(font_name)
            f.setBold(bool(bold))
            if bold:
                f.setWeight(QFont.Bold)
            tf.setFont(f)
            tf.setSize(float(pt))
            tf.setSizeUnit(_unit("Points"))
            tf.setColor(QColor(color))
            return tf

        def label(text, rect, pt, color, bold=False, halign=Qt.AlignLeft, valign=Qt.AlignTop, bg=None, name=""):
            it = QgsLayoutItemLabel(layout)
            it.setText(text)
            it.setTextFormat(tfmt(pt, color, bold))
            it.setHAlign(halign)
            it.setVAlign(valign)
            it.setMargin(0)
            if bg:
                it.setBackgroundColor(QColor(bg))
                it.setBackgroundEnabled(True)
            if name:
                it.setId(name)
            layout.addLayoutItem(it)
            place(it, *rect)
            return it

        bgc = spec["colors"]["background"]
        page_bg = QgsFillSymbol.createSimple({"color": bgc, "outline_style": "no"})
        page.setPageStyleSymbol(page_bg)

        mr = spec["map"]
        mp_ = QgsLayoutItemMap(layout)
        mp_.setId("map")
        layout.addLayoutItem(mp_)
        place(mp_, mr["x"], mr["y"], mr["w"], mr["h"])
        mp_.setCrs(crs)
        mp_.setLayers(([qlayers["__labels"]] if "__labels" in qlayers else []) + ([qlayers["__outlines"]] if "__outlines" in qlayers else []) + [qlayers[i] for i in reversed(spec["draw_order"]) if i in qlayers])
        mp_.setKeepLayerSet(True)
        ex = spec["extent"]
        mp_.zoomToExtent(QgsRectangle(ex[0], ex[1], ex[2], ex[3]))
        mp_.setBackgroundColor(QColor(spec["colors"].get("map_background") or bgc))
        mp_.setBackgroundEnabled(True)
        if spec.get("neatline"):
            mp_.setFrameEnabled(True)
            mp_.setFrameStrokeColor(QColor(spec["colors"]["frame"]))
            mp_.setFrameStrokeWidth(_mm(0.35))
        result["items"].append("map")

        # ---- title, subtitle, footer
        T = spec["title"]
        if T.get("text"):
            r = T["rect"]
            label(T["text"], (r["x"], r["y"], r["w"], T["title_h"]), T["size_pt"], spec["colors"]["label"], True, name="title")
            result["items"].append("title")
            if T.get("subtitle"):
                label(T["subtitle"], (r["x"], r["y"] + T["title_h"], r["w"], r["h"] - T["title_h"]), T["subtitle_pt"], spec["colors"]["label"], False, name="subtitle")
        F = spec["footer"]
        fr = F["rect"]
        label("\n".join(F["lines"]), (fr["x"], fr["y"], F["left_w"], fr["h"]), F["note_pt"], spec["colors"]["label"], name="notes")
        if F.get("date_text"):
            label(F["date_text"], (fr["x"] + fr["w"] - 60, fr["y"] + F["date_y"], 60, 6), F["note_pt"], spec["colors"]["label"], halign=Qt.AlignRight, name="date")
        result["items"].append("footer")
        if F.get("logo"):
            lp = kit / F["logo"]["file"]
            if lp.is_file():
                pic = QgsLayoutItemPicture(layout)
                pic.setPicturePath(str(lp))
                pic.setResizeMode(QgsLayoutItemPicture.ZoomResizeFrame if hasattr(QgsLayoutItemPicture, "ZoomResizeFrame") else 0)
                layout.addLayoutItem(pic)
                place(pic, fr["x"] + fr["w"] - F["logo"]["w"], fr["y"], F["logo"]["w"], F["logo"]["h"])
                pic.setId("logo")
                result["items"].append("logo")
            else:
                result["warnings"].append("the brand logo file is missing from the kit; it was not placed")

        # ---- legend, scale bar, north arrow, banner
        ov = spec["overlays"]
        bk = ov.get("bivariate_key")
        if bk:                                            # 1.0.0: the bivariate key as its grid, y up and x to the right, as the SVG path draws it
            n, cs, lpt = int(bk["n"]), float(bk["cell"]), float(bk["label_pt"])
            gx, gy = bk["x"] + 18.0, bk["y"] + 6.0
            for j in range(n):
                for i in range(n):
                    sh = QgsLayoutItemShape(layout)
                    sh.setShapeType(QgsLayoutItemShape.Rectangle)
                    sh.setSymbol(QgsFillSymbol.createSimple({"color": bk["cells"][j][i], "outline_style": "no"}))
                    sh.setId(f"bivariate_{j}_{i}")
                    layout.addLayoutItem(sh)
                    place(sh, gx + i * cs, gy + (n - 1 - j) * cs, cs - 0.3, cs - 0.3)
            col = spec["colors"]["label"]
            label("\u2191 " + bk["y_title"], (bk["x"], bk["y"], bk["w"], 5.0), lpt, col, True, name="bivariate_y_title")
            label(str(bk["y_ticks"][1]), (bk["x"], gy, 17.0, 4.0), lpt, col, False, Qt.AlignRight, name="bivariate_y_high")
            label(str(bk["y_ticks"][0]), (bk["x"], gy + n * cs - 4.0, 17.0, 4.0), lpt, col, False, Qt.AlignRight, Qt.AlignBottom, name="bivariate_y_low")
            label(str(bk["x_ticks"][0]), (gx, gy + n * cs + 0.5, n * cs / 2, 4.0), lpt, col, False, name="bivariate_x_low")
            label(str(bk["x_ticks"][1]), (gx + n * cs / 2, gy + n * cs + 0.5, n * cs / 2, 4.0), lpt, col, False, Qt.AlignRight, name="bivariate_x_high")
            label(bk["x_title"] + " \u2192", (gx, gy + n * cs + 4.5, bk["w"] - 18.0, 5.0), lpt, col, True, name="bivariate_x_title")
            result["items"].append("bivariate_key")
        if ov.get("legend"):
            lg = QgsLayoutItemLegend(layout)
            lg.setId("legend")
            lg.setLinkedMap(mp_)
            lg.setAutoUpdateModel(False)
            root = lg.model().rootGroup()
            root.clear()
            for i in ov["legend"]["layers"]:
                node = root.addLayer(qlayers[i])
                node.setCustomProperty("legend/title-label", next(L["title"] for L in spec["layers"] if L["id"] == i))
            lg.setTitle("")
            styles = {"Subgroup": ov["legend"]["title_pt"], "SymbolLabel": ov["legend"]["label_pt"], "Group": ov["legend"]["title_pt"]}
            from qgis.core import QgsLegendStyle
            for k, pt in styles.items():
                st = lg.rstyle(getattr(QgsLegendStyle, k))
                st.setTextFormat(tfmt(pt, spec["colors"]["label"], k != "SymbolLabel"))
            lg.setBackgroundColor(QColor(bgc))
            lg.setBackgroundEnabled(True)
            lg.setFrameEnabled(False)
            layout.addLayoutItem(lg)
            lg.adjustBoxSize()
            ref = _ref(ov["legend"]["corner"])
            lg.setReferencePoint(ref)
            lg.attemptMove(QgsLayoutPoint(ov["legend"]["x"], ov["legend"]["y"], _layout_unit()))
            result["items"].append("legend")
        sbm = ov.get("scale_bar")
        if sbm:
            sb = QgsLayoutItemScaleBar(layout)
            sb.setId("scale_bar")
            sb.setLinkedMap(mp_)
            sb.setStyle("Single Box")
            sb.setUnits({"mi": Qgis.DistanceUnit.Miles, "km": Qgis.DistanceUnit.Kilometers, "m": Qgis.DistanceUnit.Meters}[sbm["unit"]])
            sb.setUnitLabel(sbm["unit"])
            sb.setNumberOfSegmentsLeft(0)
            sb.setNumberOfSegments(int(sbm["segments"]))
            sb.setUnitsPerSegment(float(sbm["per_segment"]))
            sb.setTextFormat(tfmt(sbm["pt"], spec["colors"]["label"]))
            sb.setFillColor(QColor(spec["colors"]["label"]))
            sb.setFillColor2(QColor(bgc))
            sb.setLineColor(QColor(spec["colors"]["label"]))
            layout.addLayoutItem(sb)
            sb.attemptMove(QgsLayoutPoint(sbm["x"], sbm["y"], _layout_unit()))
            result["items"].append("scale_bar")
        na = ov.get("north")
        if na:
            pic = QgsLayoutItemPicture(layout)
            pic.setId("north_arrow")
            ok = False
            for cand in (":/images/north_arrows/layout_default_north_arrow.svg", ":/images/north_arrows/arrows/NorthArrow_02.svg"):
                pic.setPicturePath(cand)
                if pic.isValid() if hasattr(pic, "isValid") else True:
                    ok = True
                    break
            pic.setLinkedMap(mp_)
            pic.setNorthMode(QgsLayoutItemPicture.TrueNorth)
            layout.addLayoutItem(pic)
            n_h = na["size"] * 0.3                       # 0.25.0 (TODO M8): the "N" over the arrow, as the SVG path draws it
            place(pic, na["x"] + na["size"] * 0.15, na["y"] + n_h, na["size"] * 0.7, na["size"] * 0.7)
            label("N", (na["x"], na["y"], na["size"], n_h), max(6.0, n_h * 2.2), spec["colors"]["label"], True, Qt.AlignHCenter, Qt.AlignBottom,
                  name="north_arrow_n")
            result["items"].append("north_arrow")
            if not ok:
                result["warnings"].append("QGIS' default north arrow image was not found; the arrow may be blank")
        ins = ov.get("inset")
        if ins and (kit / ins["file"]).is_file():        # 1.11.0: the locator, the picture the SVG path draws
            pic = QgsLayoutItemPicture(layout)
            pic.setId("inset")
            pic.setPicturePath(str(kit / ins["file"]))
            pic.setResizeMode(QgsLayoutItemPicture.Stretch if hasattr(QgsLayoutItemPicture, "Stretch") else 0)
            layout.addLayoutItem(pic)
            place(pic, ins["x"], ins["y"], ins["w"], ins["h"])
            result["items"].append("inset")
        ban = ov.get("banner")
        if ban:
            label(ban["text"], (mr["x"], mr["y"], mr["w"], ban["h"]), ban["pt"], ban["text_color"], True, Qt.AlignHCenter, Qt.AlignVCenter, bg=ban["color"], name="banner")
            result["items"].append("banner")

        # ---- editable project beside the kit
        project.layoutManager().addLayout(layout)
        qgz = kit / "layout.qgz"
        project.write(str(qgz))
        result["project"] = qgz.name

        # ---- export
        out.mkdir(parents=True, exist_ok=True)
        ex_ = QgsLayoutExporter(layout)
        res_ok = QgsLayoutExporter.Success
        for fmt in formats:
            path = out / f"layout.{fmt}"
            if fmt == "pdf":
                s = QgsLayoutExporter.PdfExportSettings()
                s.dpi = dpi
                s.rasterizeWholeImage = False
                s.forceVectorOutput = True if hasattr(s, "forceVectorOutput") else None
                try:
                    s.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
                except Exception:  # noqa: BLE001
                    pass
                code = ex_.exportToPdf(str(path), s)
            elif fmt == "png":
                s = QgsLayoutExporter.ImageExportSettings()
                s.dpi = dpi
                code = ex_.exportToImage(str(path), s)
            elif fmt == "svg":
                s = QgsLayoutExporter.SvgExportSettings()
                s.dpi = dpi
                s.forceVectorOutput = True
                try:
                    s.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
                except Exception:  # noqa: BLE001
                    pass
                code = ex_.exportToSvg(str(path), s)
            else:
                result["warnings"].append(f"format '{fmt}' is not produced by the QGIS path")
                continue
            if code != res_ok:
                result["warnings"].append(f"QGIS reported export code {int(code)} for {fmt}")
            elif path.is_file():
                result["files"].append(path.name)
        (out / "build_result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        return 0 if result["files"] else 1
    finally:
        app.exitQgis()


def _layout_unit():
    from qgis.core import Qgis, QgsUnitTypes
    lu = getattr(Qgis, "LayoutUnit", None)
    return lu.Millimeters if lu is not None and hasattr(lu, "Millimeters") else QgsUnitTypes.LayoutMillimeters


def _mm(v: float):
    return _layout_len(v)


def _layout_len(v: float):
    from qgis.core import QgsLayoutMeasurement
    return QgsLayoutMeasurement(v, _layout_unit())


def _ref(corner: str):
    from qgis.core import QgsLayoutItem
    return {"upper-left": QgsLayoutItem.UpperLeft, "upper-right": QgsLayoutItem.UpperRight, "lower-left": QgsLayoutItem.LowerLeft, "lower-right": QgsLayoutItem.LowerRight}[corner]


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as ex:  # noqa: BLE001
        print(f"qgis_build_layout failed: {ex}", file=sys.stderr)
        sys.exit(1)
