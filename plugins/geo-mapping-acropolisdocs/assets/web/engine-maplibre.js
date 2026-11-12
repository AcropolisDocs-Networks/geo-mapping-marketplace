/* gmap web map page: the MapLibre adapter. It draws the map for the engine-neutral controller (page.js) from the style the builder wrote
   (#gmap-style): GeoJSON sources filled from #gmap-data, large layers read from PMTiles archives embedded in the page through an in-memory
   source, the Inter glyphs from #gmap-glyphs, pictures from their data URIs. All of it goes through maplibregl.addProtocol, so nothing is
   fetched unless the person chose an online basemap. MapLibre counts 512-pixel tiles: its zoom is the page's minus one, converted here. */
(function () {
  "use strict";
  function webgl() {
    try { var c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); } catch (e) { return false; }
  }
  window.gmapEngine = {
    name: "maplibre",
    unavailable: function () {
      if (typeof maplibregl === "undefined") return "the map library did not load";
      if (typeof pmtiles === "undefined") return "the tile reader did not load";
      if (!webgl()) return "this browser cannot draw WebGL maps";
      return null;
    },
    create: create
  };

  function b64buf(s) {
    var bin = atob(s), n = bin.length, u = new Uint8Array(n);
    for (var i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
    return u.buffer;
  }

  function create(ctx) {
    var cfg = ctx.cfg, data = ctx.data, reduce = ctx.reduce, tk = cfg.tokens || {}, ml = cfg.ml || {};
    var defs = {}, vis = {}, factor = {}, namesOn = true, measuring = false, clickFns = [], moveFns = [];
    var cmp = cfg.compare, cmpX = null, maps = [], map = null, mapB = null, style = null, timeFilter = null, mPts = [], mClosed = false;
    var V = cfg.view3d || null, mode3d = !!(V && V.start === "3d"), profiles = {}, btn3d = null, scales = [];
    var E = { name: "maplibre" };
    cfg.layers.forEach(function (d) { defs[d.id] = d; vis[d.id] = !!d.visible; });

    function utf8(buf) {
      if (window.TextDecoder) return new TextDecoder("utf-8").decode(new Uint8Array(buf));
      var s = "", u = new Uint8Array(buf); for (var i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
      return decodeURIComponent(escape(s));
    }
    function terrainProtocol() {
      // the terrain: Terrarium PNG tiles in one embedded block, indexed by z/x/y; a tile the page does not carry is the flat one
      var D = ml.dem, pe = document.getElementById("gmap-profiles");
      if (pe) { try { profiles = JSON.parse(utf8(b64buf(pe.textContent.trim()))); } catch (e) { profiles = {}; } pe.textContent = ""; }
      if (!D) return;
      var el = document.getElementById(D.el), buf = b64buf(el.textContent.trim());
      el.textContent = "";
      // 1.6.0 (R4-SKY): beyond the window the margin is cut coarser; a tile the page does not carry is its nearest carried ancestor's
      // quarter, enlarged pixel for pixel (Terrarium heights cannot be blended), so the far ground keeps its height and no curtain shows
      function ancestor(z, x, y) {
        for (var k = 1; k <= 4 && z - k >= 0; k++) {
          var key = (z - k) + "/" + (x >> k) + "/" + (y >> k);
          if (D.index[key]) return { ix: D.index[key], k: k, dx: x - ((x >> k) << k), dy: y - ((y >> k) << k) };
        }
        return null;
      }
      function enlarge(a) {
        var src = new Blob([buf.slice(a.ix[0], a.ix[0] + a.ix[1])], { type: "image/png" });
        return createImageBitmap(src).then(function (bmp) {
          var n = bmp.width, part = n >> a.k, c = new OffscreenCanvas(n, n), g = c.getContext("2d");
          g.imageSmoothingEnabled = false;
          g.drawImage(bmp, a.dx * part, a.dy * part, part, part, 0, 0, n, n);
          return c.convertToBlob({ type: "image/png" });
        }).then(function (b) { return b.arrayBuffer(); });
      }
      maplibregl.addProtocol("gmdem", function (params) {
        var m = /^gmdem:\/\/(\d+)\/(\d+)\/(\d+)/.exec(params.url), ix = m && D.index[m[1] + "/" + m[2] + "/" + m[3]];
        if (!ix && m && D.margin && typeof OffscreenCanvas !== "undefined") {
          var a = ancestor(+m[1], +m[2], +m[3]);
          if (a) return enlarge(a).then(function (data) { return { data: data }; });
        }
        ix = ix || D.index.flat;
        return Promise.resolve({ data: buf.slice(ix[0], ix[0] + ix[1]) });
      });
    }

    function protocols() {
      terrainProtocol();
      var G = JSON.parse(document.getElementById("gmap-glyphs").textContent);
      var empty = new ArrayBuffer(0);
      maplibregl.addProtocol("gmglyph", function (params) {
        var m = /^gmglyph:\/\/(.+)\/(\d+-\d+)\.pbf$/.exec(params.url);
        var key = m ? decodeURIComponent(m[1]).split(",")[0] + "/" + m[2] : "";
        return Promise.resolve({ data: G[key] ? b64buf(G[key]) : empty });
      });
      var proto = new pmtiles.Protocol();
      (ml.pmtiles || []).forEach(function (p) {
        var el = document.getElementById(p.el), buf = b64buf(el.textContent.trim());
        el.textContent = "";                                         // the text copy is no longer needed
        proto.add(new pmtiles.PMTiles({ getKey: function () { return p.key; }, getBytes: function (o, l) { return Promise.resolve({ data: buf.slice(o, o + l) }); } }));
      });
      maplibregl.addProtocol("pmtiles", proto.tile);
      maplibregl.addProtocol("gmimg", function (params) {
        var d = defs[decodeURIComponent(params.url.slice("gmimg://".length))];
        return Promise.resolve({ data: d ? b64buf(d.url.slice(d.url.indexOf(",") + 1)) : empty });
      });
    }

    function images(m) {
      function add(id, w, h, ratio, draw) {
        if (m.hasImage(id)) return;
        var c = document.createElement("canvas"); c.width = w; c.height = h;
        var g = c.getContext("2d"); if (draw) draw(g);
        m.addImage(id, g.getImageData(0, 0, w, h), { pixelRatio: ratio });
      }
      add("gm-box", 8, 8, 1, null);                                    // an invisible box: the room a data symbol takes, so names avoid it
      add("gm-dot", 10, 10, 2, function (g) { g.fillStyle = tk.label; g.beginPath(); g.arc(5, 5, 4, 0, 2 * Math.PI); g.fill(); });
      add("gm-dot-capital", 14, 14, 2, function (g) {
        g.fillStyle = tk.legend_bg; g.strokeStyle = tk.label; g.lineWidth = 3; g.beginPath(); g.arc(7, 7, 5, 0, 2 * Math.PI); g.fill(); g.stroke();
      });
    }

    function layerIds(id, roles) {
      return style.layers.filter(function (l) {
        var md = l.metadata || {};
        return md["gm:layer"] === id && (!roles || roles.indexOf(md["gm:role"]) >= 0);
      }).map(function (l) { return l.id; });
    }
    function sideOk(m, id) {
      var d = defs[id];
      if (!cmp || !d || !d.side) return true;
      return (m === map) === (d.side === "left");
    }
    function modeOk(lid) {
      // a layer drawn only in 3D (an extrusion, a mast, a link in the air) or only in 2D (the flat footprint or line under it)
      var l = style.layers.filter(function (x) { return x.id === lid; })[0], md = (l && l.metadata) || {};
      return md["gm:mode"] === "3d" ? mode3d : (md["gm:mode"] === "2d" ? !mode3d : true);
    }
    function apply(m) {
      cfg.layers.forEach(function (d) {
        layerIds(d.id).forEach(function (lid) {
          if (m.getLayer(lid)) m.setLayoutProperty(lid, "visibility", vis[d.id] && sideOk(m, d.id) && modeOk(lid) ? "visible" : "none");
        });
      });
      style.layers.forEach(function (l) {
        if ((l.metadata || {})["gm:role"] === "names" && m.getLayer(l.id)) m.setLayoutProperty(l.id, "visibility", namesOn ? "visible" : "none");
      });
    }
    function prepStyle(side) {
      var s = JSON.parse(document.getElementById("gmap-style").textContent);
      Object.keys(s.sources).forEach(function (k) {
        var src = s.sources[k];
        if (src.type === "geojson" && src.data && src.data.gm) src.data = data[src.data.gm] || { type: "FeatureCollection", features: [] };
      });
      s.layers.forEach(function (l) {
        var md = l.metadata || {}, d = defs[md["gm:layer"]];
        if (d && d.side && side && d.side !== side) { l.layout = l.layout || {}; l.layout.visibility = "none"; }
      });
      return s;
    }
    // 1.6.0 (R4-FLY): ?gm-capture=1 opens the page for a flythrough capture: the map fills the window, no controls or panels, the drawing
    // buffer kept so a frame can be read, one device pixel per CSS pixel, no fades; the map is window.__gmMap
    var capture = /[?&]gm-capture=1(&|$)/.test(location.search);
    function newMap(container, s) {
      var b = cfg.bounds, mb = cfg.maxBounds;
      var opts = {
        container: container, style: s, bounds: [[b[0], b[1]], [b[2], b[3]]], fitBoundsOptions: { padding: cfg.padding },
        minZoom: Math.max(0, cfg.minZoom - 1), maxZoom: cfg.maxZoom - 1, maxBounds: [[mb[0][1], mb[0][0]], [mb[1][1], mb[1][0]]],
        attributionControl: false, dragRotate: !!V, pitchWithRotate: !!V, touchPitch: !!V, renderWorldCopies: false,
        maxPitch: V ? V.maxPitch : 60, fadeDuration: (reduce || capture) ? 0 : 300, cooperativeGestures: false
      };
      if (capture) {
        opts.canvasContextAttributes = { preserveDrawingBuffer: true, antialias: true };
        opts.pixelRatio = 1;
        opts.maxBounds = null;
        var cs = document.createElement("style");
        cs.textContent = ".maplibregl-control-container{display:none!important}";
        document.head.appendChild(cs);
        var el = typeof container === "string" ? document.getElementById(container) : container;
        if (el) el.setAttribute("style", "position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:2147483000;margin:0");
        document.documentElement.classList.add("gm-capture");
      }
      var m = new maplibregl.Map(opts);
      if (capture && !window.__gmMap) window.__gmMap = m;
      if (V && mode3d) m.jumpTo({ pitch: V.pitch, bearing: V.bearing });
      rotation(m, !!(V && mode3d));
      m.once("render", function () { if (!window.__gmFirstRender) window.__gmFirstRender = Math.round(performance.now()); });   // timing for tests
      images(m);
      m.on("styleimagemissing", function () { images(m); });
      m.on("error", function (e) {
        var sid = e && e.sourceId;
        if (sid && /^base-/.test(sid)) ctx.banner("tiles-" + sid, "Basemap tiles are not loading. The bundled background is shown; data layers are not affected.");
        else if (sid && /^live-/.test(sid)) {
          var ld = defs[sid.slice(5)] || {};
          ctx.banner("src-" + sid, "The live layer " + (ld.title || sid.slice(5)) + " is not loading from " + (ld.host || "its service") + " (it may not allow requests from web pages). The rest of the map is not affected.");
        }
        else if (sid) ctx.banner("src-" + sid, "Part of the map could not be drawn (" + sid.replace(/^(data|tiles|img)-/, "") + ").");
      });
      wireClicks(m);
      return m;
    }

    function rotation(m, on) {
      // rotation and pitch only in 3D: a flat page stays north up
      m.dragRotate[on ? "enable" : "disable"]();
      m.touchZoomRotate[on ? "enableRotation" : "disableRotation"]();
      m.keyboard[on ? "enableRotation" : "disableRotation"]();
      if (m.touchPitch) m.touchPitch[on ? "enable" : "disable"]();
    }
    function label3d() {
      if (!btn3d) return;
      btn3d.textContent = mode3d ? "2D" : "3D";
      btn3d.setAttribute("aria-pressed", mode3d ? "true" : "false");
      btn3d.setAttribute("aria-label", mode3d ? "Show the map flat, from above (2D)" : "Show the map in 3D");
      btn3d.title = btn3d.getAttribute("aria-label");
    }

    E.start = function (ready) {
      protocols();
      style = prepStyle(null);
      map = newMap("gm-map", cmp ? prepStyle("left") : style);
      E.map = map;
      maps = [map];
      if (cfg.controls.scale) {
        scales = [new maplibregl.ScaleControl({ unit: "metric", maxWidth: 120 })];
        if (cfg.imperial) scales.push(new maplibregl.ScaleControl({ unit: "imperial", maxWidth: 120 }));
        if (!mode3d) scales.forEach(function (s) { map.addControl(s, "bottom-left"); });
      }
      // 1.1.0: no engine controls. The frame's rail (zoom, compass, 2D/3D, reset, full screen) and its credits corner replace MapLibre's
      // navigation and attribution controls, so both engines look the same; the corner is built from the library's attribution blocks
      btn3d = document.getElementById("gm-3d");
      if (btn3d) btn3d.addEventListener("click", function () { E.set3D(!mode3d); });
      label3d();
      var pending = 1;
      if (cmp) {
        var bDiv = document.createElement("div");
        bDiv.id = "gm-map-b";
        bDiv.setAttribute("aria-hidden", "true");
        var a = document.getElementById("gm-map");
        a.parentNode.insertBefore(bDiv, a.nextSibling);
        mapB = newMap(bDiv, prepStyle("right"));
        maps.push(mapB);
        E.mapB = mapB;
        pending = 2;
        var syncing = false;
        var sync = function (from, to) {
          if (syncing) return;
          syncing = true;
          to.jumpTo({ center: from.getCenter(), zoom: from.getZoom(), pitch: from.getPitch(), bearing: from.getBearing() });
          syncing = false;
        };
        map.on("move", function () { sync(map, mapB); });
        mapB.on("move", function () { sync(mapB, map); });
        // the controls belong to the first map but must stay above both: they move to a layer of their own over the two maps
        var host = document.createElement("div");
        host.id = "gm-ctl-host"; host.className = "maplibregl-map";
        bDiv.parentNode.insertBefore(host, bDiv.nextSibling);
        host.appendChild(map.getContainer().querySelector(".maplibregl-control-container"));
      }
      maps.forEach(function (m) {
        m.on("load", function () {
          measureLayers(m);
          if (--pending === 0) {
            map.on("moveend", function () { moveFns.forEach(function (fn) { fn(); }); });
            ready();
          }
        });
      });
    };

    // ---- clicks: a cluster zooms in, a feature opens its popup; while measuring, every click is a point
    function interactive(m) {
      return style.layers.filter(function (l) {
        var md = l.metadata || {}, d = defs[md["gm:layer"]];
        return d && (md["gm:role"] === "data" || md["gm:role"] === "cluster" || md["gm:role"] === "extrusion") && m.getLayer(l.id)
          && m.getLayoutProperty(l.id, "visibility") !== "none";
      }).map(function (l) { return l.id; });
    }
    function linkPopup(d, p) {
      // a line-of-sight link: its verdict and its profile chart (drawn at build time, inline so it is set in the page's Inter); a segment
      // of the link in the air carries only the link's key, the link's own fields are on the 2D line
      var own = ((data[d.id] || {}).features || []).filter(function (f) { return (f.properties || {})._p === p._p; })[0];
      if (own) p = own.properties;
      var box = document.createElement("div");
      box.className = "gm-popup gm-link-pop";
      var h = document.createElement("strong");
      h.textContent = (p.a != null ? p.a : "A") + " to " + (p.b != null ? p.b : "B") + (p._k ? ": " + p._k : "");
      box.appendChild(h);
      var svg = profiles[p._p];
      if (svg && window.DOMParser) {
        var doc = new DOMParser().parseFromString(svg, "image/svg+xml");
        if (doc && doc.documentElement && doc.documentElement.nodeName === "svg") {
          var el = document.importNode(doc.documentElement, true);
          el.setAttribute("class", "gm-profile");
          box.appendChild(el);
        }
      } else if (p.verdict) {
        var v = document.createElement("p"); v.textContent = String(p.verdict); box.appendChild(v);
      }
      return box;
    }
    function wireClicks(m) {
      m.on("click", function (e) {
        clickFns.forEach(function (fn) { fn(e.lngLat.lng, e.lngLat.lat); });
        if (measuring) return;
        var hits = m.queryRenderedFeatures(e.point, { layers: interactive(m) });
        if (!hits.length) return;
        var f = hits[0], md = f.layer.metadata || {}, d = defs[md["gm:layer"]];
        if (ctx.onFeature && d) ctx.onFeature(d, f.properties || {});
        if (f.properties && f.properties._p && profiles[f.properties._p] && cfg.controls.popups) {
          new maplibregl.Popup({ maxWidth: "min(640px, 94vw)", className: "gm-link-popup" }).setLngLat(e.lngLat).setDOMContent(linkPopup(d, f.properties)).addTo(m);
          return;
        }
        if (md["gm:role"] === "cluster") {
          var src = m.getSource(f.layer.source), cid = f.properties.cluster_id, c = f.geometry.coordinates;
          var pz = (ml.clusters[d.id] || {}).pointsZoom;
          if (src && src.getClusterExpansionZoom && cid != null) {
            src.getClusterExpansionZoom(cid).then(function (z) { m.easeTo({ center: c, zoom: z, animate: !reduce }); });
          } else m.easeTo({ center: c, zoom: Math.min(pz != null ? pz : m.getZoom() + 2, m.getZoom() + 3), animate: !reduce });
          return;
        }
        if (!cfg.controls.popups || !d || !d.popup) return;
        if (ctx.claimFeature && ctx.claimFeature(d, f.properties || {})) return;
        new maplibregl.Popup({ maxWidth: "280px" }).setLngLat(e.lngLat).setDOMContent(ctx.popupContent(d, f.properties || {})).addTo(m);
      });
      m.on("mousemove", function (e) {
        if (measuring) { m.getCanvas().style.cursor = "crosshair"; return; }
        var n = m.queryRenderedFeatures(e.point, { layers: interactive(m) }).length;
        m.getCanvas().style.cursor = n ? "pointer" : "";
      });
    }

    E.resetView = function () {
      var b = cfg.bounds;
      map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: cfg.padding, animate: false, bearing: 0 });
      if (V && mode3d) map.jumpTo({ pitch: V.pitch, bearing: V.bearing });
    };
    E.getView = function () { var c = map.getCenter(); return { z: map.getZoom() + 1, lat: c.lat, lng: c.lng, pitch: map.getPitch(), bearing: map.getBearing() }; };
    E.setView = function (z, lat, lng, pitch, bearing) {
      var o = { center: [lng, lat], zoom: Math.max(0, z - 1) };
      if (V && mode3d && pitch != null) { o.pitch = pitch; o.bearing = bearing || 0; }
      map.jumpTo(o);
    };
    // ---- 3D: terrain on or off, the extrusions or the flat layers under them, rotation, the camera
    E.has3D = !!V;
    E.is3D = function () { return mode3d; };
    E.set3D = function (on, opts) {
      if (!V) return;
      mode3d = !!on;
      maps.forEach(function (m) {
        if (V.terrain && ml.dem) m.setTerrain(mode3d && V.terrainOn !== false ? { source: "gm-dem", exaggeration: V.exaggeration } : null);
        apply(m);
        rotation(m, mode3d);
      });
      // a pitched view has no one scale: the scale bar is shown in 2D only
      scales.forEach(function (s) { if (mode3d && s._map) map.removeControl(s); else if (!mode3d && !s._map) map.addControl(s, "bottom-left"); });
      label3d();
      var cam = mode3d ? { pitch: (opts && opts.pitch != null) ? opts.pitch : V.pitch, bearing: (opts && opts.bearing != null) ? opts.bearing : V.bearing } : { pitch: 0, bearing: 0 };
      if (reduce || (opts && opts.animate === false)) map.jumpTo(cam); else map.easeTo(Object.assign(cam, { duration: 700 }));
      if (ctx.on3D) ctx.on3D(mode3d);
    };
    // the room the story's column takes: kept here and given to each camera move as an offset (map.setPadding redraws the terrain
    // synchronously, which takes a minute with software WebGL)
    var pad = { top: 0, right: 0, bottom: 0, left: 0 };
    E.setPadding = function (p) { pad = Object.assign({ top: 0, right: 0, bottom: 0, left: 0 }, p || {}); };
    E.flyTo = function (v, animate) {
      // a story chapter's view: a box or a centre and a zoom (the page's numbers), with a pitch and a bearing when the page is in 3D
      var cam = {}, off = [(pad.left - pad.right) / 2, (pad.top - pad.bottom) / 2];
      if (V && mode3d) { cam.pitch = v.pitch != null ? v.pitch : V.pitch; cam.bearing = v.bearing != null ? v.bearing : V.bearing; }
      else { cam.pitch = 0; cam.bearing = 0; }
      if (v.bbox) {
        var ph = (pad.left + pad.right) / 2 + 30, pv = (pad.top + pad.bottom) / 2 + 30;      // sized for the room left, centred by the offset
        var c = map.cameraForBounds([[v.bbox[0], v.bbox[1]], [v.bbox[2], v.bbox[3]]], { bearing: cam.bearing, padding: { top: pv, bottom: pv, left: ph, right: ph } });
        if (!c) return;
        cam.center = c.center; cam.zoom = Math.min(c.zoom, cfg.maxZoom - 1);
      } else {
        cam.center = [v.center[0], v.center[1]]; cam.zoom = Math.max(0, (v.zoom != null ? v.zoom : map.getZoom() + 1) - 1);
      }
      // the target sits in the middle of the room the column leaves: the camera centre moves by the offset, worked out on the ground
      // (MapLibre's own offset reads the terrain back from the GPU on every call, which software WebGL takes many seconds over)
      if (off[0] || off[1]) {
        var mc = maplibregl.MercatorCoordinate.fromLngLat(cam.center), k = 1 / (512 * Math.pow(2, cam.zoom)), br = cam.bearing * Math.PI / 180;
        var oy = off[1] / Math.max(0.3, Math.cos(cam.pitch * Math.PI / 180));        // up the screen, a pixel covers more ground on a tilted view
        var dx = (off[0] * Math.cos(br) - oy * Math.sin(br)) * k, dy = (off[0] * Math.sin(br) + oy * Math.cos(br)) * k;
        cam.center = new maplibregl.MercatorCoordinate(mc.x - dx, mc.y - dy, 0).toLngLat();
      }
      if (reduce || animate === false) map.jumpTo(cam);
      else map.flyTo(Object.assign(cam, { duration: 2200, essential: false }));
    };
    E.zoom = function () { return map.getZoom() + 1; };
    E.onMove = function (fn) { moveFns.push(fn); };
    E.onMapClick = function (fn) { clickFns.push(fn); };
    E.idle = function (fn) {
      var n = maps.length, k = 0;
      maps.forEach(function (m) {
        var go = function () { if (++k === n) fn(); };
        if (m.loaded() && !m.isMoving() && m.areTilesLoaded()) go(); else { m.once("idle", go); m.triggerRepaint(); }
      });
    };
    E.setMeasuring = function (on) {
      measuring = on;
      maps.forEach(function (m) { m.getCanvas().style.cursor = on ? "crosshair" : ""; m.doubleClickZoom[on ? "disable" : "enable"](); });
    };

    E.setBase = function (id) {
      var b = (cfg.basemaps || []).filter(function (x) { return x.id === id; })[0];
      maps.forEach(function (m) {
        (cfg.basemaps || []).forEach(function (x) {
          style.layers.forEach(function (l) {
            if ((l.metadata || {})["gm:layer"] === "base:" + x.id && m.getLayer(l.id)) m.setLayoutProperty(l.id, "visibility", x.id === id ? "visible" : "none");
          });
        });
        var mz = b && b.mode !== "bundled" ? Math.min(cfg.maxZoom, b.maxZoom) : Math.min(cfg.maxZoom, cfg.bundledMaxZoom);
        m.setMaxZoom(mz - 1);
      });
    };
    E.attributionText = function () { return ctx.creditsText ? ctx.creditsText() : ""; };
    // ---- 1.1.0: what the frame's rail and corner need from the engine
    E.zoomBy = function (d) { map.easeTo({ zoom: map.getZoom() + d, duration: reduce ? 0 : 250 }); };
    E.getBearingPitch = function () { return { bearing: map.getBearing(), pitch: map.getPitch() }; };
    E.resetNorth = function () { map.easeTo({ bearing: 0, pitch: 0, duration: reduce ? 0 : 500 }); };
    E.onRotate = function (fn) { map.on("rotate", fn); map.on("pitch", fn); };
    E.resize = function () { maps.forEach(function (m) { m.resize(); }); };
    E.getBounds = function () { var b = map.getBounds(); return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]; };
    E.setTerrainOpts = function (o) {
      if (!V || !V.terrain || !ml.dem) return;
      if (o.exaggeration != null) V.exaggeration = o.exaggeration;
      if (o.on != null) V.terrainOn = !!o.on;
      var want = mode3d && V.terrainOn !== false;
      maps.forEach(function (m) { m.setTerrain(want ? { source: "gm-dem", exaggeration: V.exaggeration } : null); });
    };

    E.setLayerVisible = function (id, on) { vis[id] = !!on; maps.forEach(apply); };
    E.isLayerVisible = function (id) { return !!vis[id]; };
    E.setNamesVisible = function (on) { namesOn = !!on; maps.forEach(apply); };
    E.namesVisible = function () { return namesOn; };
    var OPACITY = { fill: ["fill-opacity"], line: ["line-opacity"], circle: ["circle-opacity", "circle-stroke-opacity"], symbol: ["text-opacity"], raster: ["raster-opacity"],
                    "fill-extrusion": ["fill-extrusion-opacity"] };
    E.setLayerOpacity = function (id, k) {
      var d = defs[id];
      if (!d) return;
      factor[id] = k;
      style.layers.forEach(function (l) {
        if ((l.metadata || {})["gm:layer"] !== id) return;
        (OPACITY[l.type] || []).forEach(function (prop) {
          var base = (l.paint || {})[prop];
          if (base == null) base = 1;
          var v;
          if (Array.isArray(base) && base[0] === "step") {               // a mast or link width band: its zoom steps keep, its 1 becomes k
            v = base.map(function (x, i) { return i >= 2 && i % 2 === 0 && typeof x === "number" ? x * k : x; });
          } else if (typeof base !== "number") return;
          else v = d.kind === "image" || (d.kind === "live" && d.raster) ? k : base * k;      // a picture's slider is its opacity
          maps.forEach(function (m) { if (m.getLayer(l.id)) m.setPaintProperty(l.id, prop, v); });
        });
      });
    };
    E.setTime = function (i, mode) {
      var tf = mode === "cumulative" ? ["all", [">=", ["get", "_t"], 0], ["<=", ["get", "_t"], i]] : ["==", ["get", "_t"], i];
      timeFilter = tf;
      style.layers.forEach(function (l) {
        var d = defs[(l.metadata || {})["gm:layer"]];
        if (!d || !d.time) return;
        var f = l.filter ? ["all", l.filter, tf] : tf;
        maps.forEach(function (m) { if (m.getLayer(l.id)) m.setFilter(l.id, f); });
      });
    };
    E.setCompare = function (x) {
      cmpX = x;
      if (mapB) mapB.getContainer().style.clipPath = "inset(0 0 0 " + x + "px)";
    };

    // ---- popups, search, measure
    E.popup = function (lng, lat, node) { new maplibregl.Popup({ maxWidth: "280px" }).setLngLat([lng, lat]).setDOMContent(node).addTo(map); };
    E.goTo = function (t) {
      if (t.bbox) map.fitBounds([[t.bbox[0], t.bbox[1]], [t.bbox[2], t.bbox[3]]], { maxZoom: (t.maxZoom || 9) - 1, padding: 40, animate: !reduce });
      else map.jumpTo({ center: [t.lng, t.lat], zoom: Math.min(cfg.maxZoom, t.zoom || 12) - 1 });
    };
    function measureLayers(m) {
      m.addSource("gm-measure", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "gm-measure-fill", type: "fill", source: "gm-measure", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": tk.emphasis, "fill-opacity": 0.12 } });
      m.addLayer({ id: "gm-measure-line", type: "line", source: "gm-measure", filter: ["!=", ["geometry-type"], "Point"],
                   paint: { "line-color": tk.emphasis, "line-width": 2.5, "line-dasharray": [3, 2] } });
      m.addLayer({ id: "gm-measure-pt", type: "circle", source: "gm-measure", filter: ["==", ["geometry-type"], "Point"],
                   paint: { "circle-radius": 4, "circle-color": tk.legend_bg, "circle-stroke-color": tk.emphasis, "circle-stroke-width": 2 } });
    }
    E.drawMeasure = function (pts, closed) {
      mPts = pts; mClosed = closed;
      var fs = pts.map(function (p) { return { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: p } }; });
      if (closed && pts.length >= 3) fs.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pts.concat([pts[0]])] } });
      else if (pts.length >= 2) fs.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: pts } });
      maps.forEach(function (m) { var s = m.getSource("gm-measure"); if (s) s.setData({ type: "FeatureCollection", features: fs }); });
    };

    // ---- image export: each map's canvas copied in its own render event (the drawing buffer is still whole then)
    E.snapshot = function (cb) {
      var W = map.getCanvas().width, H = map.getCanvas().height, scale = W / Math.max(1, map.getContainer().clientWidth), parts = [], n = 0;
      maps.forEach(function (m, i) {
        m.once("render", function () {
          var c = document.createElement("canvas"); c.width = W; c.height = H;
          c.getContext("2d").drawImage(m.getCanvas(), 0, 0, W, H);
          parts[i] = c;
          if (++n === maps.length) {
            var x = cmpX == null ? null : Math.round(cmpX * scale);
            cb({ width: W, height: H, scale: scale, divider: mapB ? x : null,
                 parts: mapB ? [{ canvas: parts[0], x0: 0, x1: x }, { canvas: parts[1], x0: x, x1: W }] : [{ canvas: parts[0], x0: 0, x1: W }] });
          }
        });
        m.triggerRepaint();
      });
    };
    return E;
  }
})();
