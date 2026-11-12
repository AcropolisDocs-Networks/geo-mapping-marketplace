#!/usr/bin/env python3
"""qgis_project: a QGIS project (.qgz) for a built map, so the map opens in QGIS with the same layers, order, styling, CRS and extent
(M14, gap analysis P2-7). No QGIS is needed to write it.

    import qgis_project as qp
    rep = qp.write_qgz("map.qgz", [("grid", "map.gpkg", style), ("sites", "map.gpkg", style2)],      # draw order: the first is drawn first
                       crs="EPSG:5070", extent=(x0, y0, x1, y1), title="Event rate by grid cell", abstract="...")
    info = qp.check_qgz("map.qgz", expect_layers=2, expect_crs="EPSG:5070")                         # MAP-OUT-010 findings in info["findings"]

What the project holds:
  * one vector layer per entry, read from its GeoPackage table through a path relative to the project (`./map.gpkg|layername=grid`), so the
    folder can be moved or zipped whole; each layer's symbology is the QML that gpkg_style builds from the same style dict (embedded in the
    project, so the project looks right even when the GeoPackage carries no layer_styles);
  * the layer tree and the rendering order in the map's draw order (QGIS lists the top layer first, so the tree is the draw order reversed);
  * the project CRS (the map's display CRS) and the map extent, as the canvas extent and the default view;
  * project metadata: the title, the map's message as the abstract, the author (the house accreditation, in the metadata only: nothing in
    the project draws it), the creation date and keywords naming the plugin and its version;
  * (M16) a raster layer's window as the map read it, a GeoTIFF beside the project in the map's CRS, added as a raster layer with a single-band
    pseudocolour renderer in the map's colours (`raster_style`: the brand ramp interpolated between the legend's end values, classes as a
    discrete shader, categories as an exact one); a bivariate layer's QML is a rule-based renderer with the nine colours and graduated line
    widths a graduated renderer on width (gpkg_style).

The .qgz is a zip holding one .qgs XML file, written with fixed timestamps. It is checked after writing by unzipping and parsing it; when
PyQGIS can be imported in this Python it is also opened with QgsProject and its layers counted and validated.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sys
import warnings
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gpkg_style as gs  # noqa: E402
import style_dict as sd  # noqa: E402

QGIS_VERSION = gs.QGIS_VERSION
GEOMETRY = {"point": "Point", "line": "Line", "polygon": "Polygon"}


class QgisProjectError(ValueError):
    """A refusal with a message that is safe to show the person."""


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _E():
    from lxml import etree
    return etree


def _sub(parent, tag: str, text=None, **attrs):
    E = _E()
    el = E.SubElement(parent, tag, **{k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def spatialrefsys(parent, crs) -> None:
    """A QGIS <spatialrefsys>: the authority id (what QGIS reads first), the WKT, the PROJ string and the flags."""
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    auth = c.to_authority()
    el = _sub(parent, "spatialrefsys", nativeFormat="Wkt")
    _sub(el, "wkt", c.to_wkt())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            p4 = c.to_proj4()
        except Exception:  # noqa: BLE001 - some CRS have no PROJ string
            p4 = ""
    _sub(el, "proj4", p4 or "")
    _sub(el, "srsid", "0")
    _sub(el, "srid", auth[1] if auth and auth[1].isdigit() else "0")
    _sub(el, "authid", f"{auth[0]}:{auth[1]}" if auth else "")
    _sub(el, "description", c.name)
    m = re.search(r"\+proj=(\S+)", p4 or "")
    _sub(el, "projectionacronym", m.group(1) if m else "")
    _sub(el, "ellipsoidacronym", _ellipsoid(c) or "")
    _sub(el, "geographicflag", "true" if c.is_geographic else "false")


def _ellipsoid(c) -> str | None:
    """'EPSG:7019' for the CRS's ellipsoid, or None."""
    try:
        i = (c.ellipsoid.to_json_dict() or {}).get("id") if c.ellipsoid is not None else None
    except Exception:  # noqa: BLE001
        i = None
    return f"{i['authority']}:{i['code']}" if i and i.get("authority") else None


def _extent(parent, tag: str, box) -> None:
    el = _sub(parent, tag)
    for k, v in zip(("xmin", "ymin", "xmax", "ymax"), box):
        _sub(el, k, repr(float(v)))


def _units(crs) -> str:
    from pyproj import CRS
    c = CRS.from_user_input(crs)
    if c.is_geographic:
        return "degrees"
    try:
        u = (c.axis_info[0].unit_name or "").lower()
    except (IndexError, AttributeError):
        u = "metre"
    return "feet" if "foot" in u or "feet" in u else "meters"


def _layer_id(table: str, source: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", table)[:40] or "layer"
    return f"{safe}_{hashlib.sha1(source.encode()).hexdigest()[:12]}"


def _resolve_style(gpkg: Path, table: str, style: dict | None, tokens: dict | None) -> dict:
    st = style or {}
    if st.get("geometry") and st.get("mode") and ("width" in st):
        return st
    import pyogrio
    gdf = pyogrio.read_dataframe(gpkg, layer=table)
    return sd.resolve(st, gdf, tokens)


RASTER_SHADERS = {"ramp": "INTERPOLATED", "classes": "DISCRETE", "categories": "EXACT"}


def raster_style(kind: str, colors: list[str], values: list[float], labels: list[str] | None = None, opacity: float = 1.0) -> dict:
    """The colouring of a raster layer as the map draws it (M16): `ramp` the brand ramp's colours evenly spaced from values[0] to values[-1]
    (interpolated, the ends extended); `classes` one colour per class, `values` the upper edges (the last +inf); `categories` one colour per value."""
    if kind not in RASTER_SHADERS:
        raise QgisProjectError(f"a raster style is ramp, classes or categories, not '{kind}'")
    if kind == "ramp":
        lo, hi = float(values[0]), float(values[-1])
        n = len(colors)
        vals = [lo + (hi - lo) * i / max(n - 1, 1) for i in range(n)]
    else:
        vals = [float(v) for v in values]
        if len(vals) != len(colors):
            raise QgisProjectError(f"{len(colors)} colours for {len(vals)} raster classes")
    labs = list(labels) if labels and len(labels) == len(vals) else [f"{v:,.6g}" for v in vals]
    return {"kind": kind, "colors": [sd.hex_rgb(c) for c in colors], "values": vals, "labels": [str(x) for x in labs], "opacity": float(opacity)}


def _raster_layer(pl, e: dict) -> None:
    """A <maplayer type="raster"> on a GeoTIFF beside the project, drawn with a single-band pseudocolour renderer in the map's colours."""
    st = e["style"]
    ml = _sub(pl, "maplayer", type="raster", hasScaleBasedVisibilityFlag="0", styleCategories="AllStyleCategories", refreshOnNotifyEnabled="0",
              autoRefreshEnabled="0", autoRefreshTime="0", maxScale="0", minScale="1e+08", legendPlaceholderImage="")
    _extent(ml, "extent", e["bounds"])
    _sub(ml, "id", e["id"])
    _sub(ml, "datasource", e["source"])
    _sub(_sub(ml, "keywordList"), "value", "")
    _sub(ml, "layername", e["name"])
    spatialrefsys(_sub(ml, "srs"), e["crs"])
    rm = _sub(ml, "resourceMetadata")
    for t in ("identifier", "parentidentifier", "language", "type"):
        _sub(rm, t, "")
    _sub(rm, "title", e["name"])
    _sub(rm, "abstract", "")
    _sub(rm, "links")
    _sub(ml, "provider", "gdal")
    _sub(_sub(ml, "noData"), "noDataList", bandNo="1", useSrcNoData="1")
    lsm = _sub(ml, "map-layer-style-manager", current="default")
    _sub(lsm, "map-layer-style", name="default")
    pipe = _sub(ml, "pipe")
    _sub(_sub(pipe, "provider"), "resampling", enabled="false", maxOversampling="2", zoomedInResamplingMethod="nearestNeighbour",
         zoomedOutResamplingMethod="nearestNeighbour")
    vals = st["values"]
    fin = [v for v in vals if v == v and abs(v) != float("inf")]
    rr = _sub(pipe, "rasterrenderer", type="singlebandpseudocolor", band="1", opacity=repr(st["opacity"]), alphaBand="-1",
              classificationMin=repr(min(fin)), classificationMax=repr(max(fin)), nodataColor="")
    _sub(rr, "rasterTransparency")
    mmo = _sub(rr, "minMaxOrigin")
    for k, v in (("limits", "None"), ("extent", "WholeRaster"), ("statAccuracy", "Estimated"), ("cumulativeCutLower", "0.02"),
                 ("cumulativeCutUpper", "0.98"), ("stdDevFactor", "2")):
        _sub(mmo, k, v)
    sh = _sub(rr, "rastershader")
    crs_ = _sub(sh, "colorrampshader", colorRampType=RASTER_SHADERS[st["kind"]], classificationMode="1" if st["kind"] == "ramp" else "2", clip="0",
                minimumValue=repr(min(fin)), maximumValue=repr(max(fin)), labelPrecision="6")
    for v, c, lab in zip(vals, st["colors"], st["labels"]):
        _sub(crs_, "item", alpha="255", value="inf" if v == float("inf") else repr(float(v)), label=lab, color=c.lower())
    _sub(pipe, "brightnesscontrast", brightness="0", contrast="0", gamma="1")
    _sub(pipe, "huesaturation", saturation="0", grayscaleMode="0", invertColors="0", colorizeOn="0", colorizeRed="255", colorizeGreen="128",
         colorizeBlue="128", colorizeStrength="100")
    _sub(pipe, "rasterresampler", maxOversampling="2")
    _sub(pipe, "resamplingStage", "resamplingFilter")
    _sub(ml, "blendMode", "0")
    _sub(_sub(ml, "customproperties"), "Option")


def write_qgz(path, layers: list, *, crs, extent=None, title: str, abstract: str = "", author: str | None = None,
              keywords: list[str] | None = None, tokens: dict | None = None, created: str | None = None) -> dict:
    """Write a .qgz. layers: [(table, gpkg path, style dict[, display name])] in draw order (the first drawn first, at the bottom); an entry may
    instead be a raster (M16): {"raster": GeoTIFF path, "name": ..., "style": raster_style(...)}, read through a path relative to the project.
    extent: (xmin, ymin, xmax, ymax) in `crs`; the layers' combined extent when None. Returns {'path', 'qgs', 'layers', 'crs', 'extent'}."""
    import pyogrio
    from pyproj import CRS, Transformer
    E = _E()
    p = Path(path)
    if p.suffix.lower() != ".qgz":
        raise QgisProjectError("a QGIS project is written as .qgz")
    if not layers:
        raise QgisProjectError("a project needs at least one layer")
    pcrs = CRS.from_user_input(crs)
    ident = sd.plugin_ident()
    author = author if author is not None else ident["house"]
    created = created or (os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()) + "T00:00:00"
    root = E.Element("qgis", projectname=title, version=QGIS_VERSION, saveUser="", saveUserFull="", saveDateTime=created)
    _sub(root, "homePath", path="")
    _sub(root, "title", title)
    _sub(root, "transaction", mode="Disabled")
    _sub(root, "projectFlags", set="")
    spatialrefsys(_sub(root, "projectCrs"), pcrs)
    tree = _sub(root, "layer-tree-group")
    _sub(_sub(tree, "customproperties"), "Option")
    entries = []
    boxes = []
    for item in layers:
        if isinstance(item, dict) and item.get("raster"):                  # M16: a raster window beside the project
            tif = Path(item["raster"])
            if not tif.is_file():
                raise QgisProjectError(f"raster '{item.get('name') or tif.stem}': {tif.name} does not exist")
            import raster_io as rio
            info = rio.open_info(tif)
            rel = os.path.relpath(tif.resolve(), p.parent.resolve()).replace("\\", "/")
            source = rel if rel.startswith("../") else "./" + rel
            b = info["bounds"]
            lcrs = info.get("crs") or str(pcrs)
            tr = Transformer.from_crs(CRS.from_user_input(lcrs), pcrs, always_xy=True)
            xs, ys = tr.transform([b[0], b[2], b[0], b[2]], [b[1], b[1], b[3], b[3]])
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
            entries.append({"id": _layer_id(tif.stem, source), "table": tif.stem, "source": source, "name": item.get("name") or tif.stem,
                            "style": item["style"], "crs": lcrs, "bounds": list(b), "raster": True})
            continue
        table, gpkg, style = item[0], Path(item[1]), item[2]
        name = item[3] if len(item) > 3 and item[3] else None
        if not gpkg.is_file():
            raise QgisProjectError(f"layer '{table}': {gpkg.name} does not exist")
        try:
            info = pyogrio.read_info(gpkg, layer=table)
        except Exception as ex:  # noqa: BLE001 - a missing table is the person's problem to see
            raise QgisProjectError(f"layer '{table}' could not be read from {gpkg.name}: {str(ex)[:120]}") from None
        st = _resolve_style(gpkg, table, style, tokens)
        rel = os.path.relpath(gpkg.resolve(), p.parent.resolve()).replace("\\", "/")
        source = f"{rel if rel.startswith('../') else './' + rel}|layername={table}"
        lid = _layer_id(table, source)
        b = info.get("total_bounds")
        lcrs = info.get("crs") or "EPSG:4326"
        if b is not None and all(v == v for v in b):
            tr = Transformer.from_crs(CRS.from_user_input(lcrs), pcrs, always_xy=True)
            xs, ys = tr.transform([b[0], b[2], b[0], b[2]], [b[1], b[1], b[3], b[3]])
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
        entries.append({"id": lid, "table": table, "source": source, "gpkg": gpkg, "style": st, "name": name or st.get("title") or table,
                        "info": info, "crs": lcrs})
    # the tree lists the top layer first: the draw order reversed
    for e in reversed(entries):
        tl = _sub(tree, "layer-tree-layer", id=e["id"], name=e["name"], source=e["source"], providerKey="gdal" if e.get("raster") else "ogr",
                  checked="Qt::Checked", expanded="1",
                  legend_exp="", patch_size="-1,-1", legend_split_behavior="0")
        _sub(_sub(tl, "customproperties"), "Option")
    co = _sub(tree, "custom-order", enabled="0")
    for e in reversed(entries):
        _sub(co, "item", e["id"])
    _sub(root, "relations")
    if extent is None:
        if not boxes:
            raise QgisProjectError("no extent was given and the layers have none")
        extent = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    canvas = _sub(root, "mapcanvas", name="theMapCanvas", annotationsVisible="1")
    _sub(canvas, "units", _units(pcrs))
    _extent(canvas, "extent", extent)
    _sub(canvas, "rotation", "0")
    spatialrefsys(_sub(canvas, "destinationsrs"), pcrs)
    _sub(canvas, "rendermaptile", "0")
    _sub(canvas, "expressionContextScope")
    pl = _sub(root, "projectlayers")
    for e in entries:
        if e.get("raster"):
            _raster_layer(pl, e)
            continue
        st, info = e["style"], e["info"]
        gt = str(info.get("geometry_type") or "")
        ml = _sub(pl, "maplayer", type="vector", geometry=GEOMETRY.get(st["geometry"], "Polygon"), wkbType=gt.replace(" ", "") or "Unknown",
                  simplifyDrawingHints="1", simplifyLocal="1", simplifyAlgorithm="0", simplifyMaxScale="1", simplifyDrawingTol="1", readOnly="0",
                  hasScaleBasedVisibilityFlag="0", minScale="100000000", maxScale="0", styleCategories="AllStyleCategories",
                  refreshOnNotifyEnabled="0", autoRefreshEnabled="0", autoRefreshTime="0", labelsEnabled="1" if st.get("label") else "0",
                  legendPlaceholderImage="", symbologyReferenceScale="-1")
        b = info.get("total_bounds")
        if b is not None and all(v == v for v in b):
            _extent(ml, "extent", b)
        _sub(ml, "id", e["id"])
        _sub(ml, "datasource", e["source"])
        _sub(_sub(ml, "keywordList"), "value", "")
        _sub(ml, "layername", e["name"])
        spatialrefsys(_sub(ml, "srs"), e["crs"])
        rm = _sub(ml, "resourceMetadata")
        for t in ("identifier", "parentidentifier", "language", "type"):
            _sub(rm, t, "")
        _sub(rm, "title", e["name"])
        _sub(rm, "abstract", "")
        _sub(rm, "links")
        _sub(ml, "provider", "ogr", encoding="UTF-8")
        for t in ("vectorjoins", "layerDependencies", "dataDependencies", "expressionfields"):
            _sub(ml, t)
        lsm = _sub(ml, "map-layer-style-manager", current="default")
        _sub(lsm, "map-layer-style", name="default")
        _sub(ml, "auxiliaryLayer")
        qroot = gs.qml_root(st, field_is_float=gs._float_field(e["gpkg"], e["table"], st.get("field")))
        for child in list(qroot):
            if child.tag != "layerGeometryType":
                ml.append(child)
        _sub(ml, "layerOpacity", "1")
        _sub(_sub(ml, "customproperties"), "Option")
    lo = _sub(root, "layerorder")
    for e in reversed(entries):
        _sub(lo, "layer", id=e["id"])
    props = _sub(root, "properties")
    paths = _sub(props, "Paths")
    _sub(paths, "Absolute", "false", type="bool")
    srs = _sub(props, "SpatialRefSys")
    _sub(srs, "ProjectionsEnabled", "1", type="int")
    meas = _sub(props, "Measure")
    _sub(meas, "Ellipsoid", _ellipsoid(pcrs) or "NONE", type="QString")
    _sub(root, "transformContext")
    md = _sub(root, "projectMetadata")
    for t in ("identifier", "parentidentifier", "language", "type"):
        _sub(md, t, "")
    _sub(md, "title", title)
    _sub(md, "abstract", abstract or "")
    kw = _sub(md, "keywords", vocabulary="gmap")
    for k in (keywords or []) + [sd.generator_text()]:
        _sub(kw, "keyword", k)
    _sub(md, "links")
    _sub(md, "author", author)
    _sub(md, "creation", created)
    pvs = _sub(root, "ProjectViewSettings", UseProjectScales="0", rotation="0")
    _sub(pvs, "Scales")
    dve = _sub(pvs, "DefaultViewExtent", xmin=repr(float(extent[0])), ymin=repr(float(extent[1])), xmax=repr(float(extent[2])), ymax=repr(float(extent[3])))
    spatialrefsys(dve, pcrs)
    for t in ("Annotations", "Layouts", "Bookmarks"):
        _sub(root, t)
    qgs = b"<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" + E.tostring(root, pretty_print=True, encoding="UTF-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    inner = f"{p.stem}.qgs"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo(inner, date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o644 << 16
        zf.writestr(zi, qgs)
    auth = pcrs.to_authority()
    return {"path": str(p), "qgs": inner, "layers": [{"id": e["id"], "table": e["table"], "source": e["source"], "name": e["name"],
                                                      "renderer": "singlebandpseudocolor" if e.get("raster") else gs.renderer_type(gs.qml(e["style"]))}
                                                     for e in entries],
            "crs": f"{auth[0]}:{auth[1]}" if auth else pcrs.to_wkt(), "extent": [float(v) for v in extent]}


def read_qgs(path) -> bytes:
    p = Path(path)
    with zipfile.ZipFile(p) as zf:
        names = [i for i in zf.infolist() if i.filename.lower().endswith(".qgs")]
        if len(names) != 1:
            raise QgisProjectError(f"{p.name} holds {len(names)} .qgs files; a project holds one")
        if names[0].file_size > 64 * 1024 * 1024:
            raise QgisProjectError(f"{p.name} is too large to check")
        return zf.read(names[0])


def check_qgz(path, *, expect_layers: int | None = None, expect_crs: str | None = None) -> dict:
    """Unzip and parse a .qgz: its layers (in project order), the layer tree (top first), the project CRS and the metadata author, plus
    MAP-OUT-010 findings; with PyQGIS importable, the project is also opened and its layers validated."""
    E = _E()
    p = Path(path)
    fs: list[dict] = []
    try:
        raw = read_qgs(p)
        body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!DOCTYPE") else raw
        root = E.fromstring(body, E.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))
    except Exception as ex:  # noqa: BLE001 - a project that does not parse is the finding
        return {"layers": [], "tree": [], "crs": None, "findings": [finding("MAP-OUT-010", "BLOCK", f"{p.name} does not parse as a QGIS project: {str(ex)[:160]}")]}
    layers = []
    for ml in root.iter("maplayer"):
        r = ml.find("renderer-v2") if ml.get("type") != "raster" else ml.find("pipe/rasterrenderer")
        src = (ml.findtext("datasource") or "")
        file_part = src.split("|", 1)[0]
        target = (p.parent / file_part).resolve() if not Path(file_part).is_absolute() else Path(file_part)
        L = {"id": ml.findtext("id"), "name": ml.findtext("layername"), "source": src, "renderer": r.get("type") if r is not None else None,
             "exists": target.is_file(), "geometry": ml.get("geometry"), "type": ml.get("type") or "vector"}
        if L["type"] == "raster":                   # M16: the GeoTIFF beside the project reads back, and its shader has the map's colours
            items = ml.findall("pipe/rasterrenderer/rastershader/colorrampshader/item")
            L["shader"] = (ml.find("pipe/rasterrenderer/rastershader/colorrampshader").get("colorRampType")
                           if ml.find("pipe/rasterrenderer/rastershader/colorrampshader") is not None else None)
            L["colors"] = [it.get("color") for it in items]
            if L["exists"]:
                try:
                    import raster_io as rio
                    inf = rio.open_info(target)
                    L["bands"] = int(inf.get("bands") or inf.get("count") or 1)
                except Exception as ex:  # noqa: BLE001 - an unreadable GeoTIFF is the finding
                    fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: raster '{L['name']}' ({file_part}) does not read back: {str(ex)[:100]}"))
            if not items:
                fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: raster '{L['name']}' has no colours in its shader"))
        layers.append(L)
    tree = [el.get("id") for el in root.iter("layer-tree-layer")]
    crs = root.findtext("projectCrs/spatialrefsys/authid")
    author = root.findtext("projectMetadata/author")
    if expect_layers is not None and len(layers) != expect_layers:
        fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: {len(layers)} layer(s) for {expect_layers}"))
    for L in layers:
        if not L["exists"]:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: layer '{L['name']}' points at {L['source'].split('|')[0]}, which is not beside the project"))
        if not L["renderer"]:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: layer '{L['name']}' has no symbology"))
    if sorted(tree) != sorted(L["id"] for L in layers):
        fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: the layer tree does not list the project's layers"))
    if expect_crs and crs != expect_crs:
        fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: the project CRS is {crs}, expected {expect_crs}"))
    if not author:
        fs.append(finding("MAP-OUT-010", "WARN", f"{p.name}: the project metadata names no author"))
    qgis = _pyqgis_open(p)
    if qgis is not None:
        if qgis.get("error"):
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: QGIS could not open it: {qgis['error']}"))
        elif qgis["layers"] != len(layers) or qgis["invalid"]:
            fs.append(finding("MAP-OUT-010", "BLOCK", f"{p.name}: QGIS opened {qgis['layers']} layer(s), {qgis['invalid']} invalid"))
    if not fs:
        fs.append(finding("MAP-OUT-010", "INFO", f"{p.name}: {len(layers)} styled layer(s) in draw order, project CRS {crs}"
                          + ("; opened in QGIS " + qgis["version"] if qgis else "; parsed (PyQGIS is not in this Python, so QGIS did not open it)")))
    return {"layers": layers, "tree": tree, "crs": crs, "author": author, "title": root.findtext("projectMetadata/title"), "findings": fs,
            "opened_in_qgis": bool(qgis and not qgis.get("error"))}


def _pyqgis_open(p: Path) -> dict | None:
    """Open the project with PyQGIS when it can be imported here; None when it cannot."""
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from qgis.core import Qgis, QgsApplication, QgsProject  # type: ignore
    except Exception:  # noqa: BLE001 - PyQGIS is optional
        return None
    try:
        app = QgsApplication.instance() or QgsApplication([], False)
        if not QgsApplication.instance():
            app.initQgis()
        proj = QgsProject()
        ok = proj.read(str(p))
        if not ok:
            return {"error": "QgsProject.read returned false"}
        lays = list(proj.mapLayers().values())
        return {"version": Qgis.version(), "layers": len(lays), "invalid": sum(1 for x in lays if not x.isValid())}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {str(ex)[:120]}"}
