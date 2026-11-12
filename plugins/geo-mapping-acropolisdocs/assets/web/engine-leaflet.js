/* gmap web map page: the Leaflet adapter. It draws the map for the engine-neutral controller (page.js) and keeps everything that is
   Leaflet's own here: the bundled background, tile layers, GeoJSON layers, panes, popups, labels and their collision pass, the swipe clip.
   Zoom numbers are Leaflet's (256-pixel tiles), which are the page's own. No network calls of its own: tiles come from the manifest's hosts. */
(function () {
  "use strict";
  window.gmapEngine = {
    name: "leaflet",
    unavailable: function () { return typeof L === "undefined" ? "the map library did not load" : null; },
    create: create
  };

  function create(ctx) {
    var cfg = ctx.cfg, data = ctx.data, reduce = ctx.reduce, tk = cfg.tokens || {};
    var map, bounds, layers = {}, casings = {}, defs = {}, factor = {}, measuring = false, clickFns = [], moveFns = [];
    var timeIndex = null, timeMode = "period";
    var cmp = cfg.compare, cmpX = null;
    var E = { name: "leaflet", snapshot: null };

    E.start = function (ready) {
      map = L.map("gm-map", {
        minZoom: cfg.minZoom, maxZoom: cfg.maxZoom, zoomControl: false, attributionControl: false,
        maxBounds: cfg.maxBounds, maxBoundsViscosity: 0.8, preferCanvas: true,
        zoomAnimation: !reduce, fadeAnimation: !reduce, markerZoomAnimation: !reduce, zoomSnap: 0.5
      });
      E.map = map;
      bounds = L.latLngBounds([cfg.bounds[1], cfg.bounds[0]], [cfg.bounds[3], cfg.bounds[2]]);
      E.resetView();
      buildBundled();
      buildPanes();
      cfg.layers.forEach(buildLayer);
      buildLabels();
      buildNames();
      map.on("zoomend moveend", thinLabels);
      map.whenReady(function () { setTimeout(thinLabels, 0); });
      if (cfg.controls.scale) L.control.scale({ position: "bottomleft", metric: true, imperial: cfg.imperial, maxWidth: 120 }).addTo(map);
      map.on("click", function (e) { clickFns.forEach(function (fn) { fn(e.latlng.lng, e.latlng.lat); }); });
      map.on("moveend", function () { moveFns.forEach(function (fn) { fn(); }); });
      map.on("move", clipPanes);
      map.on("resize", clipPanes);
      ready();
    };

    E.resetView = function () { map.fitBounds(bounds, { padding: [cfg.padding, cfg.padding], animate: false }); };
    E.getView = function () { var c = map.getCenter(); return { z: map.getZoom(), lat: c.lat, lng: c.lng }; };
    E.setView = function (z, lat, lng) { map.setView([lat, lng], z, { animate: false }); };
    E.zoom = function () { return map.getZoom(); };
    E.onMove = function (fn) { moveFns.push(fn); };
    E.onMapClick = function (fn) { clickFns.push(fn); };
    E.idle = function (fn) { setTimeout(fn, 0); };
    E.setMeasuring = function (on) { measuring = on; if (on) map.closePopup(); map.getContainer().style.cursor = on ? "crosshair" : ""; };

    // ---- basemaps: the bundled background always sits underneath, so a failing tile host never leaves the page empty
    function buildBundled() {
      var bundled = L.layerGroup();
      (cfg.bundled || []).forEach(function (b) {
        var st = {};
        for (var k in b.style) { if (Object.prototype.hasOwnProperty.call(b.style, k)) st[k] = b.style[k]; }
        st.className = "gm-bundled";
        L.geoJSON(data[b.id], { interactive: false, style: function () { return st; } }).addTo(bundled);
      });
      bundled.addTo(map);
      // past the zoom where the generalized background holds its shape, it is faded so it is not read as a position reference
      function fadeBundled() { map.getContainer().classList.toggle("gm-faint", map.getZoom() > (cfg.bundledDetailZoom || 8)); }
      map.on("zoomend", fadeBundled);
      fadeBundled();
    }
    var tileLayers = {}, errCount = {};
    function makeTiles(b) {
      // maxNativeZoom: past the provider's last zoom, its last tiles are enlarged, not requested (they do not exist); zoomOffset: 512 tiles
      var t = L.tileLayer(b.url, { maxZoom: b.maxZoom, maxNativeZoom: b.nativeMaxZoom, minNativeZoom: b.nativeMinZoom || 0, zoomOffset: b.zoomOffset || 0,
                                   tileSize: b.tileSize || 256, attribution: b.attribution, crossOrigin: false, detectRetina: false,
                                   opacity: b.treatment ? b.treatment.opacity : 1 });     // 0.22.0: over the land colour, as the print's treatment
      t.on("tileerror", function () {
        var h = b.host;
        errCount[h] = (errCount[h] || 0) + 1;
        if (errCount[h] === 4) ctx.banner("tiles-" + h, "Basemap tiles from " + h + " are not loading. The bundled background is shown; data layers are not affected.");
      });
      return t;
    }
    var activeBase = null;
    E.setBase = function (id) {
      if (activeBase && tileLayers[activeBase]) { map.removeLayer(tileLayers[activeBase]); }
      activeBase = id;
      var b = (cfg.basemaps || []).filter(function (x) { return x.id === id; })[0];
      if (b && b.mode === "tiles") {
        if (!tileLayers[id]) tileLayers[id] = makeTiles(b);
        tileLayers[id].addTo(map).bringToBack();
        if (b.treatment && tileLayers[id].getContainer()) tileLayers[id].getContainer().style.filter = "grayscale(" + b.treatment.grayscale + ")";
      }
      if (b && b.mode === "tiles") { map.setMaxZoom(Math.min(cfg.maxZoom, b.maxZoom)); }
      else { map.setMaxZoom(Math.min(cfg.maxZoom, cfg.bundledMaxZoom)); }
    };
    E.attributionText = function () { return ctx.creditsText ? ctx.creditsText() : ""; };
    // ---- 1.1.0: what the frame's rail and corner need from the engine (Leaflet is flat and north up: no bearing, no pitch)
    E.zoomBy = function (d) { map.setZoom(map.getZoom() + d, { animate: !reduce }); };
    E.getBearingPitch = function () { return { bearing: 0, pitch: 0 }; };
    E.resetNorth = function () {};
    E.onRotate = function () {};
    E.resize = function () { map.invalidateSize({ animate: false }); thinLabels(); };
    E.getBounds = function () { var b = map.getBounds(); return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]; };

    // ---- panes: relief and raster pictures over the bundled background and under the data (M12); the two sides of the swipe compare (M14)
    var dataPane, renderers = {};
    function buildPanes() {
      if (cfg.imagePanes || cmp) {
        map.createPane("gm-relief").style.zIndex = 410;
        map.createPane("gm-raster").style.zIndex = 420;
        map.createPane("gm-data").style.zIndex = 450;
        map.getPane("gm-relief").style.pointerEvents = "none";
        map.getPane("gm-raster").style.pointerEvents = "none";
        dataPane = "gm-data";
      }
      if (cmp) {
        [["gm-cmp-li", 422], ["gm-cmp-ri", 423], ["gm-cmp-l", 452], ["gm-cmp-r", 453]].forEach(function (p) {
          map.createPane(p[0]).style.zIndex = p[1];
          if (p[0].slice(-1) === "i") map.getPane(p[0]).style.pointerEvents = "none";
          else renderers[p[0]] = L.canvas({ pane: p[0] });
        });
      }
      map.createPane("gm-measure").style.zIndex = 640;
      renderers["gm-measure"] = L.canvas({ pane: "gm-measure" });
    }
    function picture(def) { return def.kind === "image" || (def.kind === "live" && def.raster); }
    function paneFor(def) {
      if (cmp && def.side) return picture(def) ? (def.side === "left" ? "gm-cmp-li" : "gm-cmp-ri") : (def.side === "left" ? "gm-cmp-l" : "gm-cmp-r");
      return picture(def) ? (def.pane || "overlayPane") : (dataPane || "overlayPane");
    }
    function clipPanes() {
      if (!cmp || cmpX == null) return;
      var nw = map.containerPointToLayerPoint([0, 0]), se = map.containerPointToLayerPoint(map.getSize());
      var cx = nw.x + cmpX;
      var left = "rect(" + [nw.y, cx, se.y, nw.x].join("px,") + "px)", right = "rect(" + [nw.y, se.x, se.y, cx].join("px,") + "px)";
      map.getPane("gm-cmp-l").style.clip = left; map.getPane("gm-cmp-li").style.clip = left;
      map.getPane("gm-cmp-r").style.clip = right; map.getPane("gm-cmp-ri").style.clip = right;
    }
    E.setCompare = function (x) { cmpX = x; clipPanes(); };

    // ---- data layers
    function timeOk(f) {
      if (timeIndex == null) return true;
      var t = (f.properties || {})._t;
      if (t == null || t < 0) return false;
      return timeMode === "cumulative" ? t <= timeIndex : t === timeIndex;
    }
    function styleFor(def, f) {
      var s = def.style, p = (f && f.properties) || {}, k = factor[def.id] == null ? 1 : factor[def.id];
      var hs = 1;                                                       // 0.23.0: the print's highlight: the rest faded, the subset larger
      if (s.highlight && p._hl === 0) k *= s.highlight.rest;
      if (s.highlight && p._hl === 1) hs = s.highlight.size;
      var op = (s.opacity == null ? 1 : s.opacity) * k;
      if (def.kind === "polygon") return { fillColor: p._c || s.fill, fillOpacity: s.fillOpacity * k, color: s.stroke, weight: s.weight * hs, opacity: op, stroke: s.weight > 0 };
      if (def.kind === "line") {
        var heavy = def.contour && p._i === 1;                          // index contours heavier and darker
        return { color: heavy ? def.contour.index_stroke : (p._c || s.stroke), weight: (heavy ? def.contour.index_weight : s.weight) * hs, opacity: op,
                 dashArray: s.dash || null, lineCap: "round" };
      }
      return { radius: (p._r || s.radius) * hs, fillColor: p._c || s.fill, fillOpacity: s.fillOpacity * k, color: s.stroke, weight: s.pointWeight || 1, opacity: k };
    }
    function casingFor(def) {                           // 0.22.0: the print's casing under a data line, a wider line in the land colour
      var c = def.style.casing, k = factor[def.id] == null ? 1 : factor[def.id];
      return { color: c.color, weight: c.weight, opacity: c.opacity * k, lineCap: "round", lineJoin: "round", interactive: false };
    }
    // ---- live service layers (0.15.0): a WMS or a tile template as pictures (no CORS needed), a WFS read once as GeoJSON for the page's area
    function liveErrors(def, lyr) {
      var n = 0;
      lyr.on("tileerror", function () {
        n += 1;
        if (n === 4) ctx.banner("live-" + def.id, "The live layer " + def.title + " is not loading from " + def.host + ". The rest of the map is not affected.");
      });
    }
    function buildLive(def) {
      var lyr, o = { pane: paneFor(def), opacity: def.opacity == null ? 1 : def.opacity, attribution: def.credit || "", maxZoom: cfg.maxZoom,
                     minNativeZoom: def.minzoom || 0, maxNativeZoom: def.maxzoom || 19, crossOrigin: false };
      if (def.live === "wms") lyr = L.tileLayer.wms(def.wmsBase, Object.assign(o, def.wms));
      else lyr = L.tileLayer(def.url, o);
      liveErrors(def, lyr);
      layers[def.id] = lyr;
      if (def.visible) lyr.addTo(map);
    }
    function buildLayer(def) {
      defs[def.id] = def;
      if (def.kind === "live" && def.raster) { buildLive(def); return; }
      var liveWfs = def.kind === "live" && def.live === "wfs";
      if (liveWfs) { def = Object.assign({}, def, { kind: def.geometry }); defs[def.id] = def; data[def.id] = { type: "FeatureCollection", features: [] }; }
      if (def.kind === "image") {                       // a raster or relief picture: the print picture warped to Web Mercator (data URI)
        var im = L.imageOverlay(def.url, def.bounds, { opacity: def.opacity == null ? 1 : def.opacity, interactive: false, pane: paneFor(def),
                                                       alt: def.title, className: "gm-img-" + def.role });
        layers[def.id] = im;
        if (def.visible) im.addTo(map);
        return;
      }
      var pane = paneFor(def), rend = renderers[pane];
      var opts = {
        style: function (f) { return styleFor(def, f); },
        filter: def.time ? timeOk : null,
        pointToLayer: function (f, ll) {
          var o = styleFor(def, f); o.pane = pane; if (rend) o.renderer = rend;
          return L.circleMarker(ll, o);
        }
      };
      if (pane !== "overlayPane") opts.pane = pane;
      if (rend) opts.renderer = rend;
      if (def.kind === "line" && def.style.casing) {
        var co = { style: function () { return casingFor(def); }, filter: opts.filter, interactive: false };
        if (pane !== "overlayPane") co.pane = pane;
        if (rend) co.renderer = rend;
        casings[def.id] = L.geoJSON(data[def.id], co);
        if (def.visible) casings[def.id].addTo(map);
      }
      var g = L.geoJSON(data[def.id], opts);
      g.on("click", function (e) {
        if (!measuring && e.layer && e.layer.feature && ctx.onFeature) ctx.onFeature(def, e.layer.feature.properties || {});
        if (measuring || !cfg.controls.popups || !def.popup || !e.layer || !e.layer.feature) return;
        if (ctx.claimFeature && ctx.claimFeature(def, e.layer.feature.properties || {})) return;
        L.popup({ maxWidth: 280 }).setLatLng(e.latlng).setContent(ctx.popupContent(def, e.layer.feature.properties || {})).openOn(map);
      });
      layers[def.id] = g;
      if (def.visible) g.addTo(map);
      if (liveWfs) {
        fetch(def.url, { credentials: "omit", mode: "cors" }).then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
          .then(function (j) { if (j && j.features) { g.addData(j); thinLabels(); } })
          .catch(function () { ctx.banner("live-" + def.id, "The live layer " + def.title + " could not be read from " + def.host + " (it may not allow requests from web pages)."); });
      }
    }
    E.setLayerVisible = function (id, on) {
      var g = layers[id], c = casings[id];
      if (!g) return;
      if (c && on && !map.hasLayer(c)) { c.addTo(map); if (map.hasLayer(g)) { map.removeLayer(g); } }      // the casing goes under
      if (c && !on && map.hasLayer(c)) map.removeLayer(c);
      if (on && !map.hasLayer(g)) g.addTo(map);
      if (!on && map.hasLayer(g)) map.removeLayer(g);
      thinLabels();
    };
    E.isLayerVisible = function (id) { return !!layers[id] && map.hasLayer(layers[id]); };
    E.setLayerOpacity = function (id, k) {
      var g = layers[id], def = defs[id];
      if (!g || !def) return;
      if (picture(def)) { g.setOpacity(k); return; }                  // a picture's slider is its opacity
      factor[id] = k;
      g.setStyle(function (f) { return styleFor(def, f); });
      if (casings[id]) casings[id].setStyle(function () { return casingFor(def); });
    };
    E.setTime = function (i, mode) {
      timeIndex = i; timeMode = mode || "period";
      cfg.layers.forEach(function (def) {
        if (!def.time || !layers[def.id]) return;
        var g = layers[def.id];
        if (casings[def.id]) { casings[def.id].clearLayers(); casings[def.id].addData(data[def.id]); }
        g.clearLayers();
        g.addData(data[def.id]);
        bindLabels(def.id);
      });
      thinLabels();
    };

    // ---- feature labels: the layers the MapSpec labels get a permanent tooltip per point, shown from the zoom the data fits at
    var labelled = {};
    function bindLabels(layerId) {
      (cfg.featureLabels || []).forEach(function (fl) {
        if (fl.layer !== layerId) return;
        var g = layers[fl.layer];
        labelled[fl.layer] = [];
        if (!g) return;
        g.eachLayer(function (lyr) {
          if (!lyr.getLatLng) return;
          var v = (lyr.feature && lyr.feature.properties || {})[fl.field];
          if (v == null || v === "") return;
          lyr.bindTooltip(String(v), { permanent: true, direction: "right", offset: [4, 0], className: "gm-flabel", interactive: false, opacity: 1 });
          labelled[fl.layer].push(lyr);
        });
      });
    }
    function buildLabels() { (cfg.featureLabels || []).forEach(function (fl) { bindLabels(fl.layer); }); buildContourLabels(); }

    // ---- 0.23.0: index contours carry their elevation at the deeper zooms, as on the MapLibre page (one label at each line's middle)
    var contourLabels = [];
    function buildContourLabels() {
      cfg.layers.forEach(function (def) {
        if (!def.contour || !def.contour.labels || !layers[def.id]) return;
        layers[def.id].eachLayer(function (lyr) {
          var p = (lyr.feature && lyr.feature.properties) || {};
          if (p._i !== 1 || p._e == null || !lyr.getLatLngs) return;
          var ll = lyr.getLatLngs(); while (ll.length && Array.isArray(ll[0]) && ll[0].length && !(ll[0] instanceof L.LatLng)) ll = ll[0];
          if (!ll.length || !(ll[0] instanceof L.LatLng)) return;
          var t = L.tooltip({ permanent: true, direction: "center", className: "gm-elev", interactive: false, opacity: 1 }).setLatLng(ll[Math.floor(ll.length / 2)]).setContent(String(p._e));
          contourLabels.push({ t: t, def: def });
        });
      });
      function show() {
        var z = map.getZoom();
        contourLabels.forEach(function (c) {
          var on = z >= (c.def.contour.label_zoom || 0) && map.hasLayer(layers[c.def.id]);
          if (on && !map.hasLayer(c.t)) c.t.addTo(map); else if (!on && map.hasLayer(c.t)) map.removeLayer(c.t);
        });
      }
      if (contourLabels.length) { map.on("zoomend overlayadd overlayremove", show); show(); }
    }

    // ---- place, water and admin names: text labels tiered by zoom (Natural Earth's min_zoom), thinned by a greedy screen-space pass
    var namesLayer = L.layerGroup(), names = [];
    var TIER_RANK = { admin: 5, capital: 4, city: 3, water: 2, town: 1, village: 0 };
    function nameIcon(p) {
      var box = document.createElement("div");
      box.className = "gm-name gm-name-" + p.tier;
      box.setAttribute("data-min-zoom", String(p.min_zoom || 0));
      if (p.kind === "place") { var dot = document.createElement("span"); dot.className = "gm-name-dot"; box.appendChild(dot); }
      var txt = document.createElement("span"); txt.className = "gm-name-text"; txt.textContent = String(p.name); box.appendChild(txt);
      return { icon: L.divIcon({ html: box, className: "gm-name-icon", iconSize: [0, 0], iconAnchor: [0, 0] }), box: box, text: txt };
    }
    function buildNames() {
      if (!(cfg.names && cfg.names.on && data[cfg.names.id])) return;
      (data[cfg.names.id].features || []).forEach(function (f) {
        var p = f.properties || {}, c = f.geometry && f.geometry.coordinates;
        if (!c) return;
        var ic = nameIcon(p);
        var m = L.marker([c[1], c[0]], { icon: ic.icon, interactive: false, keyboard: false });
        m.addTo(namesLayer);
        names.push({ marker: m, box: ic.box, text: ic.text, ll: L.latLng(c[1], c[0]), minZoom: p.min_zoom || 0, maxZoom: p.max_zoom || 99,
                     prio: (TIER_RANK[p.tier] || 0) * 1e10 + (p.prio || 0) });
      });
      names.sort(function (a, b) { return b.prio - a.prio; });
      namesLayer.addTo(map);
    }
    E.setNamesVisible = function (on) { if (on) namesLayer.addTo(map); else map.removeLayer(namesLayer); thinLabels(); };
    E.namesVisible = function () { return map.hasLayer(namesLayer); };

    // The collision pass. A label's box relative to its anchor point never changes (the label is drawn at a fixed size whatever the zoom),
    // so it is measured from the page once and cached; every later pass works it out from the anchor's screen position alone (TODO M10).
    var boxes = typeof WeakMap === "function" ? new WeakMap() : null;
    function overlaps(a, b) { return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom; }
    function boxOf(el, ll, view) {
      var pt = map.latLngToContainerPoint(ll), c = boxes && boxes.get(el);
      if (!c) {
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return null;
        c = { dx: r.left - view.left - pt.x, dy: r.top - view.top - pt.y, w: r.width, h: r.height };
        if (boxes) boxes.set(el, c);
      }
      return { left: pt.x + c.dx, top: pt.y + c.dy, right: pt.x + c.dx + c.w, bottom: pt.y + c.dy + c.h };
    }
    function thinLabels() {
      // data labels first (they belong to the map's own point), then names by tier and size; whatever would overlap an accepted box is hidden
      if (!map) return;
      var z = map.getZoom(), view = map.getContainer().getBoundingClientRect(), size = map.getSize(), taken = [];
      [].forEach.call(document.querySelectorAll("#gm-rail, .gm-chipbar, .gm-caption.gm-has, #gm-flegend"), function (el) {
        var r = el.getBoundingClientRect();
        if (r.width && r.height && el.offsetParent !== null) taken.push({ left: r.left - view.left - 4, top: r.top - view.top - 4, right: r.right - view.left + 4, bottom: r.bottom - view.top + 4 });
      });
      function consider(el, measureEl, ll, show) {
        if (!el) return;
        if (!show) { el.classList.add("gm-hidden"); return; }
        el.classList.remove("gm-hidden");
        var b = boxOf(measureEl || el, ll, view);
        if (!b || b.right < 0 || b.left > size.x || b.bottom < 0 || b.top > size.y) return;
        var pad = 2, box = { left: b.left - pad, top: b.top - pad, right: b.right + pad, bottom: b.bottom + pad };
        for (var i = 0; i < taken.length; i++) { if (overlaps(box, taken[i])) { el.classList.add("gm-hidden"); return; } }
        taken.push(box);
      }
      Object.keys(labelled).forEach(function (lid) {
        labelled[lid].forEach(function (lyr) {
          var tt = lyr.getTooltip && lyr.getTooltip();
          var el = tt && tt.getElement && tt.getElement();
          consider(el, null, lyr.getLatLng(), z >= (cfg.labelZoom || 0) && map.hasLayer(lyr));
        });
      });
      if (map.hasLayer(namesLayer)) {
        names.forEach(function (n) {
          consider(n.marker.getElement() ? n.box : null, n.text, n.ll, z >= n.minZoom && z <= n.maxZoom);
        });
      }
    }

    // ---- popups, search, measure
    E.popup = function (lng, lat, node) { L.popup({ maxWidth: 280 }).setLatLng([lat, lng]).setContent(node).openOn(map); };
    E.goTo = function (t) {
      if (t.bbox) map.fitBounds([[t.bbox[1], t.bbox[0]], [t.bbox[3], t.bbox[2]]], { maxZoom: t.maxZoom || 9, animate: !reduce });
      else map.setView([t.lat, t.lng], Math.min(cfg.maxZoom, t.zoom || 12), { animate: !reduce });
    };
    // a story chapter's view (M15): Leaflet is always flat and north up, so a chapter's pitch and bearing are not drawn here
    var pad = { top: 0, right: 0, bottom: 0, left: 0 };
    E.has3D = false;
    E.is3D = function () { return false; };
    E.setPadding = function (p) { pad = Object.assign(pad, p || {}); };
    E.flyTo = function (v, animate) {
      var anim = !reduce && animate !== false, o = { paddingTopLeft: [pad.left + 20, pad.top + 20], paddingBottomRight: [pad.right + 20, pad.bottom + 20], animate: anim };
      if (v.bbox) map[anim ? "flyToBounds" : "fitBounds"]([[v.bbox[1], v.bbox[0]], [v.bbox[3], v.bbox[2]]], Object.assign(o, { maxZoom: cfg.maxZoom, duration: 1.6 }));
      else if (anim) map.flyTo([v.center[1], v.center[0]], Math.min(cfg.maxZoom, v.zoom || map.getZoom()), { duration: 1.6 });
      else map.setView([v.center[1], v.center[0]], Math.min(cfg.maxZoom, v.zoom || map.getZoom()), { animate: false });
    };
    var mGroup = null;
    E.drawMeasure = function (pts, closed) {
      if (mGroup) { map.removeLayer(mGroup); mGroup = null; }
      if (!pts.length) return;
      mGroup = L.layerGroup();
      var lls = pts.map(function (p) { return [p[1], p[0]]; });
      var o = { pane: "gm-measure", renderer: renderers["gm-measure"], color: tk.emphasis, weight: 2.5, opacity: 1, dashArray: "6 4", interactive: false };
      if (closed && lls.length >= 3) { o.fillColor = tk.emphasis; o.fillOpacity = 0.12; L.polygon(lls, o).addTo(mGroup); }
      else if (lls.length >= 2) L.polyline(lls, o).addTo(mGroup);
      lls.forEach(function (ll) {
        L.circleMarker(ll, { pane: "gm-measure", renderer: renderers["gm-measure"], radius: 4, color: tk.emphasis, weight: 2, fillColor: tk.legend_bg, fillOpacity: 1, interactive: false }).addTo(mGroup);
      });
      mGroup.addTo(map);
    };
    return E;
  }
})();
