/* gmap web map page: the controller. Engine-neutral: panels, the layer list and its opacity sliders, the basemap switcher, search, measure,
   the time slider, the swipe compare, image export, the shared address and the error banners. The map itself is drawn by the engine adapter
   defined just before this script (engine-leaflet.js or engine-maplibre.js sets window.gmapEngine). Plain browser JavaScript, no network calls
   of its own. Configuration is in #gmap-config, data in #gmap-data; zoom numbers are the page's (256-pixel tiles) whatever the engine. */
(function () {
  "use strict";
  var root = document.getElementById("gm-root");
  var cfg = JSON.parse(document.getElementById("gmap-config").textContent);
  var data = JSON.parse(document.getElementById("gmap-data").textContent);
  var banners = document.getElementById("gm-banners");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var defById = {};
  cfg.layers.forEach(function (d) { defById[d.id] = d; });

  function banner(id, text) {
    if (!cfg.controls.error_banners || !banners) return;
    var el = document.getElementById("gm-banner-" + id);
    if (!el) {
      el = document.createElement("p");
      el.id = "gm-banner-" + id;
      el.className = "gm-banner";
      banners.appendChild(el);
    }
    el.textContent = text;
  }

  function showFallback(reason) {
    var f = document.getElementById("gm-fallback");
    var m = document.getElementById("gm-map");
    if (f) { f.hidden = false; }
    if (m) { m.hidden = true; }
    banner("init", "The interactive map could not start (" + reason + "). A picture of the map is shown instead.");
  }
  function msg(e) { return String(e && e.message || e).slice(0, 80); }

  var engine = window.gmapEngine;
  if (!engine) { showFallback("the map engine did not load"); return; }
  var why = engine.unavailable();
  if (why) { showFallback(why); return; }
  var E, F = null, hooks = {};
  try {
    E = engine.create({ cfg: cfg, data: data, root: root, banner: banner, reduce: reduce, popupContent: popupContent, fail: showFallback,
                        onFeature: function (d, p) { if (hooks.feature) hooks.feature(d, p); },
                        claimFeature: function (d, p) { return F && F.claimFeature ? F.claimFeature(d, p) : false; },
                        creditsText: function () { return F && F.creditsText ? F.creditsText() : ""; },
                        on3D: function (on) { if (hooks.mode) hooks.mode(on); } });
    E.start(function () { try { init(); } catch (e) { showFallback(msg(e)); } });
  } catch (e) {
    showFallback(msg(e));
  }

  // a feature's popup: the title field, then the fields the MapSpec lists, then its class; text only, never markup
  function popupContent(def, p) {
    var box = document.createElement("div");
    box.className = "gm-popup";
    if (def.popup.title && p[def.popup.title] != null) {
      var h = document.createElement("strong"); h.textContent = String(p[def.popup.title]); box.appendChild(h);
    }
    var dl = document.createElement("dl");
    (def.popup.fields || []).forEach(function (f) {
      if (f === def.popup.title || p[f] == null) return;
      var dt = document.createElement("dt"); dt.textContent = f.replace(/_/g, " ");
      var dd = document.createElement("dd"); dd.textContent = String(p[f]);
      dl.appendChild(dt); dl.appendChild(dd);
    });
    if (p._k) {
      var dt2 = document.createElement("dt"); dt2.textContent = "class";
      var dd2 = document.createElement("dd"); dd2.textContent = p._k;
      dl.appendChild(dt2); dl.appendChild(dd2);
    }
    if (def.time && cfg.time && p._t != null && p._t >= 0 && cfg.time.labels[p._t] != null) {
      var dt3 = document.createElement("dt"); dt3.textContent = "period";
      var dd3 = document.createElement("dd"); dd3.textContent = cfg.time.labels[p._t];
      dl.appendChild(dt3); dl.appendChild(dd3);
    }
    box.appendChild(dl);
    return box;
  }

  // a GetFeatureInfo answer: up to three features, twelve plain values each, as text
  function infoContent(def, j) {
    var feats = (j && j.features) || [];
    if (!feats.length) return null;
    var box = document.createElement("div");
    box.className = "gm-popup gm-live-info";
    var h = document.createElement("strong"); h.textContent = def.title; box.appendChild(h);
    feats.slice(0, 3).forEach(function (f) {
      var dl = document.createElement("dl"), p = f.properties || {}, n = 0;
      Object.keys(p).forEach(function (k) {
        var v = p[k];
        if (n >= 12 || v == null || typeof v === "object") return;
        var dt = document.createElement("dt"); dt.textContent = String(k).replace(/_/g, " ").slice(0, 60);
        var dd = document.createElement("dd"); dd.textContent = String(v).slice(0, 200);
        dl.appendChild(dt); dl.appendChild(dd); n += 1;
      });
      box.appendChild(dl);
    });
    var note = document.createElement("p"); note.className = "gm-note"; note.textContent = "From " + def.host + ", as it answered just now";
    box.appendChild(note);
    return box;
  }

  // ---- geodesic measure: Vincenty's inverse formula on the WGS 84 ellipsoid (haversine where it does not converge); area on the ellipsoid (authalic latitudes)
  var WGS_A = 6378137, WGS_F = 1 / 298.257223563, WGS_B = WGS_A * (1 - WGS_F), AUTHALIC_R = 6371007.181;
  function rad(d) { return d * Math.PI / 180; }
  function haversine(a, b) {
    var dl = rad(b[1] - a[1]), dn = rad(b[0] - a[0]);
    var h = Math.sin(dl / 2) * Math.sin(dl / 2) + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dn / 2) * Math.sin(dn / 2);
    return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function geodesic(a, b) {
    var L0 = rad(b[0] - a[0]), U1 = Math.atan((1 - WGS_F) * Math.tan(rad(a[1]))), U2 = Math.atan((1 - WGS_F) * Math.tan(rad(b[1])));
    var sU1 = Math.sin(U1), cU1 = Math.cos(U1), sU2 = Math.sin(U2), cU2 = Math.cos(U2), lam = L0, it = 0, lamP, sS, cS, sig, sA, c2A, c2m;
    do {
      var sl = Math.sin(lam), cl = Math.cos(lam);
      sS = Math.sqrt((cU2 * sl) * (cU2 * sl) + (cU1 * sU2 - sU1 * cU2 * cl) * (cU1 * sU2 - sU1 * cU2 * cl));
      if (sS === 0) return 0;
      cS = sU1 * sU2 + cU1 * cU2 * cl;
      sig = Math.atan2(sS, cS);
      sA = cU1 * cU2 * sl / sS;
      c2A = 1 - sA * sA;
      c2m = c2A ? cS - 2 * sU1 * sU2 / c2A : 0;
      var C = WGS_F / 16 * c2A * (4 + WGS_F * (4 - 3 * c2A));
      lamP = lam;
      lam = L0 + (1 - C) * WGS_F * sA * (sig + C * sS * (c2m + C * cS * (-1 + 2 * c2m * c2m)));
    } while (Math.abs(lam - lamP) > 1e-12 && ++it < 200);
    if (it >= 200) return haversine(a, b);
    var u2 = c2A * (WGS_A * WGS_A - WGS_B * WGS_B) / (WGS_B * WGS_B);
    var A = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2))), B = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)));
    var dS = B * sS * (c2m + B / 4 * (cS * (-1 + 2 * c2m * c2m) - B / 6 * c2m * (-3 + 4 * sS * sS) * (-3 + 4 * c2m * c2m)));
    return WGS_B * A * (sig - dS);
  }
  var WGS_E2 = WGS_F * (2 - WGS_F), WGS_E = Math.sqrt(WGS_E2);
  function qOf(sphi) { return (1 - WGS_E2) * (sphi / (1 - WGS_E2 * sphi * sphi) - Math.log((1 - WGS_E * sphi) / (1 + WGS_E * sphi)) / (2 * WGS_E)); }
  var QP = qOf(1);
  function sinAuthalic(latDeg) { return Math.max(-1, Math.min(1, qOf(Math.sin(rad(latDeg))) / QP)); }
  function ringArea(pts) {
    // 0.23.0: the area of a ring on the WGS 84 ellipsoid: each latitude taken to its authalic latitude, then the spherical-excess sum on the
    // authalic sphere, which has the ellipsoid's area (a few parts in a million for a site-sized shape, about one in ten thousand for a degree-sized one; it was within 0.5 percent)
    var n = pts.length, s = 0;
    if (n < 3) return 0;
    for (var i = 0; i < n; i++) {
      var p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
      s += (rad(p3[0]) - rad(p1[0])) * sinAuthalic(p2[1]);
    }
    return Math.abs(s * AUTHALIC_R * AUTHALIC_R / 2);
  }
  function fmtNum(v, d) { return v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function fmtDist(m) {
    if (cfg.units === "imperial") {
      var mi = m / 1609.344;
      return mi < 0.1 ? fmtNum(m / 0.3048, 0) + " ft" : fmtNum(mi, mi < 100 ? 2 : 1) + " mi";
    }
    return m < 1000 ? fmtNum(m, 0) + " m" : fmtNum(m / 1000, m < 100000 ? 2 : 1) + " km";
  }
  function fmtArea(a) {
    if (cfg.units === "imperial") {
      var sqmi = a / 2589988.110336;
      return sqmi < 0.1 ? fmtNum(a / 4046.8564224, 1) + " acres" : fmtNum(sqmi, 2) + " sq mi";
    }
    return a < 1e6 ? fmtNum(a / 1e4, 2) + " ha" : fmtNum(a / 1e6, 2) + " km²";
  }

  function bboxOf(g) {
    var b = [Infinity, Infinity, -Infinity, -Infinity];
    (function walk(c) {
      if (typeof c[0] === "number") { b[0] = Math.min(b[0], c[0]); b[1] = Math.min(b[1], c[1]); b[2] = Math.max(b[2], c[0]); b[3] = Math.max(b[3], c[1]); return; }
      for (var i = 0; i < c.length; i++) walk(c[i]);
    })(g.coordinates);
    return b;
  }

  function init() {
    // ---- the frame (1.1.0): panel, tabs, rail, dock, brand chip and credits corner (frame.js); it calls back into the page for time and story
    var st = hashRead();
    var tIndex = null, storyIdx = -1;
    if (window.gmapFrame) F = window.gmapFrame.init({ cfg: cfg, E: E, root: root, reduce: reduce, banner: banner, popupContent: popupContent, hashState: st || {},
      hashWrite: function () { hashWrite(); },
      setTime: function (i) { setTime(i); hashWrite(); }, timeIndex: function () { return tIndex == null ? 0 : tIndex; },
      storyGo: function (i, how) { storyGo(i, how); }, storyIndex: function () { return storyIdx < 0 ? 0 : storyIdx; } });
    if (/[?&]gm-capture=1(&|$)/.test(location.search)) {      // 1.6.0 (R4-FLY): what a flythrough capture reads from the page
      window.__gmCtx = { cfg: cfg, data: data,
        // 1.6.2: the flythrough moves the time slider with its marker; a story that follows time must not fly the camera away from it
        setTime: function (i) { storyFollowing = true; try { setTime(i); } finally { storyFollowing = false; } },
        credits: function () {
        var t = F && F.creditsText ? F.creditsText() : "";
        if (t) return t;
        var all = (cfg.attributions || []).slice();                 // the basemap's, then each drawn layer's own credit, once each
        (cfg.layers || []).forEach(function (d) { if (d.credit && all.indexOf(d.credit) < 0) all.push(d.credit); });
        return all.join(" · ");
      } };
    }
    function legendFor(id, on) {
      [].forEach.call(document.querySelectorAll('[data-legend-for="' + id + '"]'), function (lg) {
        lg.classList.toggle("gm-off", !on);
        var note = lg.querySelector(".gm-lg-hidden"); if (note) note.hidden = on;
        var eye = lg.querySelector("button.gm-eye");
        if (eye) { eye.setAttribute("aria-pressed", on ? "true" : "false"); var u = eye.querySelector("use"); if (u) u.setAttribute("href", on ? "#gmi-eye" : "#gmi-eyeoff"); }
      });
      if (F) F.credits();
    }
    function showLayer(id, on) {
      if (cfg.names && id === cfg.names.id) { E.setNamesVisible(on); }
      else { E.setLayerVisible(id, on); legendFor(id, on); }
      var cb = document.querySelector('#gm-layer-list input[value="' + id + '"]'); if (cb) cb.checked = on;
      hashWrite();
    }
    [].forEach.call(document.querySelectorAll("button.gm-eye"), function (b) {
      b.addEventListener("click", function () { var id = b.getAttribute("data-eye"); showLayer(id, !E.isLayerVisible(id)); });
    });
    [].forEach.call(document.querySelectorAll("button[data-zoom-layer]"), function (b) {
      b.addEventListener("click", function () {
        var id = b.getAttribute("data-zoom-layer"), def = defById[id], fs = ((data[id] || {}).features || []), bb = [Infinity, Infinity, -Infinity, -Infinity];
        fs.forEach(function (f) { var q = bboxOf(f.geometry); bb = [Math.min(bb[0], q[0]), Math.min(bb[1], q[1]), Math.max(bb[2], q[2]), Math.max(bb[3], q[3])]; });
        if (def && def.bounds) bb = [def.bounds[0][1], def.bounds[0][0], def.bounds[1][1], def.bounds[1][0]];
        if (isFinite(bb[0])) E.goTo({ bbox: bb, maxZoom: cfg.maxZoom });
      });
    });
    var ter = document.getElementById("gm-terrain"), ex = document.getElementById("gm-exag");
    if (ter && E.setTerrainOpts) ter.addEventListener("change", function () { E.setTerrainOpts({ on: ter.checked }); });
    if (ex && E.setTerrainOpts) ex.addEventListener("input", function () { E.setTerrainOpts({ exaggeration: +ex.value }); var o = document.getElementById("gm-exag-v"); if (o) o.textContent = (+ex.value).toFixed(1) + "\u00d7"; });

    // ---- layer switcher, opacity sliders, basemap switcher
    var lsw = document.getElementById("gm-layer-list");
    if (lsw) {
      lsw.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
        cb.addEventListener("change", function () {
          if (cfg.names && cb.value === cfg.names.id) { E.setNamesVisible(cb.checked); hashWrite(); return; }
          if (!defById[cb.value]) return;
          showLayer(cb.value, cb.checked);
        });
      });
    }
    document.querySelectorAll("input.gm-opacity").forEach(function (sl) {
      sl.addEventListener("input", function () { E.setLayerOpacity(sl.getAttribute("data-opacity-for"), Math.max(0, Math.min(1, (+sl.value) / 100))); });
    });
    var basemaps = cfg.basemaps || [], activeBase = null;
    function selectBase(id) { activeBase = id; E.setBase(id); showWordmark(id); if (F) F.setBase(id); hashWrite(); }
    // ---- a provider's logo (0.16.0: Mapbox requires it on every map with its tiles), read from the provider's own stylesheet on its tile host
    var wmBox = document.getElementById("gm-wordmark"), wmCache = {};
    function showWordmark(id) {
      if (!wmBox) return;
      var b = basemaps.filter(function (x) { return x.id === id; })[0], wm = b && b.wordmark;
      wmBox.innerHTML = "";
      wmBox.hidden = !wm;
      if (!wm) return;
      var a = document.createElement("a");
      a.href = wm.link; a.target = "_blank"; a.rel = "noopener"; a.setAttribute("aria-label", wm.alt + " (basemap provider)");
      wmBox.appendChild(a);
      function put(uri) {
        if (uri) { var im = new Image(wm.w, wm.h); im.alt = wm.alt; im.src = uri; a.appendChild(im); }
        else { a.textContent = wm.alt; a.className = "gm-wordmark-text"; }         // the provider's stylesheet did not load: its name, at least
      }
      if (wm.img) { var im0 = new Image(); if (wm.h) im0.height = wm.h; im0.alt = wm.alt; im0.onload = function () { if (F) F.layoutBottom(); }; im0.src = wm.img; a.appendChild(im0); return; }
      if (wmCache.hasOwnProperty(wm.css)) { put(wmCache[wm.css]); if (F) F.layoutBottom(); return; }
      fetch(wm.css, { credentials: "omit", mode: "cors" }).then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
        .then(function (css) {
          var i = css.indexOf(wm.selector + "{"), m = i >= 0 ? /url\("(data:image\/svg\+xml[^"]+)"\)/.exec(css.slice(i, css.indexOf("}", i))) : null;
          wmCache[wm.css] = m ? m[1] : null; put(wmCache[wm.css]);
        })
        .catch(function () { wmCache[wm.css] = null; put(null); })
        .then(function () { if (F) F.layoutBottom(); });
    }
    var bsw = document.getElementById("gm-base-list");
    if (bsw) {
      bsw.querySelectorAll("input[type=radio]").forEach(function (rb) {
        rb.addEventListener("change", function () { if (rb.checked) selectBase(rb.value); });
      });
    }

    // ---- buttons: reset, fullscreen, print, download, save image
    function on(id, fn) { var el = document.getElementById(id); if (el) el.addEventListener("click", fn); }
    on("gm-reset", function () { E.resetView(); });
    on("gm-fullscreen", function () {
      var d = document;
      if (d.fullscreenElement) { d.exitFullscreen(); }
      else if (root.requestFullscreen) { root.requestFullscreen(); }
    });
    on("gm-print", function () { window.print(); });
    on("gm-download", function () {
      var out = { type: "FeatureCollection", features: [] };
      cfg.layers.forEach(function (def) {
        if (!def.downloadable) return;
        (data[def.id].features || []).forEach(function (f) { out.features.push(f); });
      });
      saveBlob(new Blob([JSON.stringify(out)], { type: "application/geo+json" }), cfg.downloadName);
    });
    on("gm-export", exportImage);

    // ---- search: the map's own features and the bundled place names, all inside the page
    var inp = document.getElementById("gm-search");
    var res = document.getElementById("gm-search-results");
    if (inp && res) {
      var index = [];
      // 1.0.0: a very large tiled layer's index comes packed (gzip): texts joined by U+001F, then int32 longitudes and latitudes at 1e-5 degree
      var unpack = function (elId, def) {
        var el = document.getElementById(elId);
        if (!el || typeof DecompressionStream !== "function" || typeof Response !== "function") return;
        var s = atob(el.textContent.trim()), bin = new Uint8Array(s.length);
        for (var i = 0; i < s.length; i++) bin[i] = s.charCodeAt(i);
        el.textContent = "";
        new Response(new Blob([bin]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer().then(function (buf) {
          var dv = new DataView(buf), n = dv.getUint32(0, true), tl = dv.getUint32(4, true);
          var txt = new TextDecoder().decode(new Uint8Array(buf, 8, tl)).replace(/\u0000+$/, "").split("\u001f");
          var xs = new Int32Array(buf, 8 + tl, n), ys = new Int32Array(buf, 8 + tl + 4 * n, n);
          for (var k = 0; k < n; k++) index.push({ text: txt[k], low: txt[k].toLowerCase(), def: def, lng: xs[k] / 1e5, lat: ys[k] / 1e5 });
          window.gmapSearchUnpacked = (window.gmapSearchUnpacked || 0) + n;
        }).catch(function () {});
      };
      cfg.layers.forEach(function (def) {
        if (!def.searchable) return;
        var pk = cfg.searchPacked && cfg.searchPacked[def.id];
        if (pk) { unpack(pk, def); return; }
        var si = cfg.searchIndex && cfg.searchIndex[def.id];
        if (si) {
          for (var i = 0; i < si.t.length; i++) index.push({ text: si.t[i], low: si.t[i].toLowerCase(), def: def, lng: si.x[i], lat: si.y[i] });
          return;
        }
        (data[def.id].features || []).forEach(function (f) {
          var v = (f.properties || {})[cfg.searchField];
          if (v != null) index.push({ text: String(v), low: String(v).toLowerCase(), def: def, f: f });
        });
      });
      if (cfg.searchNames && cfg.names && cfg.names.on && data[cfg.names.id]) {
        (data[cfg.names.id].features || []).forEach(function (f) {
          var p = f.properties || {}, c = f.geometry.coordinates;
          index.push({ text: String(p.name), low: String(p.name).toLowerCase(), place: p.kind === "admin" ? "state" : (p.kind === "place" ? "place" : "water"),
                       lng: c[0], lat: c[1], zoom: Math.max(8, Math.min(12, (p.min_zoom || 6) + 3)) });
        });
      }
      var show = function (list) {
        res.textContent = "";
        list.forEach(function (it) {
          var li = document.createElement("li");
          var b = document.createElement("button");
          b.type = "button"; b.textContent = it.text;
          if (it.place) { var k = document.createElement("span"); k.className = "gm-kind"; k.textContent = " · " + it.place; b.appendChild(k); }
          b.addEventListener("click", function () { go(it); });
          li.appendChild(b); res.appendChild(li);
        });
        res.hidden = !list.length;
      };
      var go = function (it) {
        res.hidden = true;
        if (it.f) {
          var bb = bboxOf(it.f.geometry), pt = it.f.geometry.type === "Point";
          if (pt) E.goTo({ lng: bb[0], lat: bb[1], zoom: 12 }); else E.goTo({ bbox: bb, maxZoom: 9 });
          if (cfg.controls.popups && it.def.popup && !(F && F.claimFeature(it.def, it.f.properties || {}))) E.popup((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2, popupContent(it.def, it.f.properties || {}));
        } else {
          E.goTo({ lng: it.lng, lat: it.lat, zoom: it.zoom || 12 });
        }
      };
      inp.addEventListener("input", function () {
        var q = inp.value.trim().toLowerCase();
        if (!q) { show([]); return; }
        var hits = index.filter(function (x) { return x.low.indexOf(q) >= 0; });
        hits.sort(function (a, b) { return (a.low.indexOf(q) === 0 ? 0 : 1) - (b.low.indexOf(q) === 0 ? 0 : 1) || (a.place ? 1 : 0) - (b.place ? 1 : 0); });
        show(hits.slice(0, 8));
      });
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { var first = res.querySelector("button"); if (first) first.click(); }
        if (e.key === "Escape") { show([]); }
      });
    }

    // ---- measure: click points on the map; a closed shape gives the area. Geodesic, in the units of the map's scale bar
    var M = { on: false, pts: [], closed: false };
    var mbtn = document.getElementById("gm-measure-btn"), mpanel = document.getElementById("gm-measure");
    function mDraw() {
      E.drawMeasure(M.pts, M.closed);
      if (!mpanel) return;
      var d = 0;
      for (var i = 1; i < M.pts.length; i++) d += geodesic(M.pts[i - 1], M.pts[i]);
      if (M.closed && M.pts.length >= 3) d += geodesic(M.pts[M.pts.length - 1], M.pts[0]);
      var dist = document.getElementById("gm-m-dist"), area = document.getElementById("gm-m-area");
      dist.textContent = M.pts.length < 2 ? "click the map" : fmtDist(d);
      area.textContent = M.closed && M.pts.length >= 3 ? fmtArea(ringArea(M.pts)) : "";
      area.parentNode.hidden = !(M.closed && M.pts.length >= 3);
      document.getElementById("gm-m-shape").textContent = M.closed ? "Line" : "Area";
      dist.setAttribute("data-m", String(d));
      area.setAttribute("data-m2", String(M.closed ? ringArea(M.pts) : 0));
    }
    function mSet(onOff) {
      M.on = onOff;
      if (mbtn) mbtn.setAttribute("aria-pressed", onOff ? "true" : "false");
      E.setMeasuring(onOff);
      if (!onOff) { M.pts = []; M.closed = false; E.drawMeasure([], false); }
      if (mpanel) {
        mpanel.hidden = !onOff;
        if (onOff && !mpanel.firstChild) {
          mpanel.innerHTML = '<span>Distance: <output id="gm-m-dist"></output></span>' +
            '<span hidden>Area: <output id="gm-m-area"></output></span>' +
            '<button type="button" id="gm-m-shape">Area</button><button type="button" id="gm-m-undo">Undo</button>' +
            '<button type="button" id="gm-m-clear">Clear</button><button type="button" id="gm-m-done">Done</button>';
          on("gm-m-shape", function () { M.closed = !M.closed; mDraw(); });
          on("gm-m-undo", function () { M.pts.pop(); mDraw(); });
          on("gm-m-clear", function () { M.pts = []; M.closed = false; mDraw(); });
          on("gm-m-done", function () { mSet(false); });
        }
        if (onOff) mDraw();
      }
    }
    if (mbtn) mbtn.addEventListener("click", function () { mSet(!M.on); });
    E.onMapClick(function (lng, lat) { if (!M.on) return; M.pts.push([lng, lat]); mDraw(); });

    // ---- live WMS layers (0.15.0): what the service holds where the map is clicked (GetFeatureInfo as JSON), shown as text, never markup
    var infoDefs = cfg.layers.filter(function (d) { return d.kind === "live" && d.info; });
    if (infoDefs.length && cfg.controls.popups) E.onMapClick(function (lng, lat) {
      if (M.on) return;
      var on = infoDefs.filter(function (d) { return E.isLayerVisible(d.id); });
      if (!on.length) return;
      var d = on[on.length - 1], R = 6378137, x = R * lng * Math.PI / 180, y = R * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
      var half = 50 * 156543.03392804097 / Math.pow(2, E.getView().z);
      var bb = [x - half, y - half, x + half, y + half].map(function (v) { return v.toFixed(2); }).join(",");
      fetch(d.info.replace("{bbox}", bb), { credentials: "omit", mode: "cors" })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function (j) { var el = infoContent(d, j); if (el) E.popup(lng, lat, el); })
        .catch(function () { banner("info-" + d.id, "The service behind " + d.title + " did not answer the click (it may not allow requests from web pages)."); });
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && M.on) mSet(false); });

    // ---- time: one period at a time (or all up to it); the legend's classes are those of the whole layer and never change. The dock's
    //      play, step, speed and loop are the frame's (frame.js); the label is the spec's playback.label (T.display), never a raw range
    var T = cfg.time;
    var trange = document.getElementById("gm-time-range"), tlabel = document.getElementById("gm-time-label");
    function setTime(i) {
      if (!T) return;
      tIndex = Math.max(0, Math.min(T.labels.length - 1, i | 0));
      var shown = (T.display || T.labels)[tIndex];
      if (trange) { trange.value = String(tIndex); trange.setAttribute("aria-valuetext", shown); }
      if (tlabel) tlabel.textContent = shown;
      E.setTime(tIndex, T.mode);
      if (S && S.follow_time && !storyFollowing) {
        var k = -1;
        S.chapters.forEach(function (c, j) { if (c.time != null && c.time >= 0 && c.time <= tIndex) k = j; });
        if (k >= 0 && k !== storyIdx) { storyFollowing = true; storyGo(k, "time"); storyFollowing = false; }
      }
    }
    var storyFollowing = false;
    if (T && trange) trange.addEventListener("input", function () { if (F) F.stop(); setTime(+trange.value); hashWrite(); });

    // ---- swipe compare: the left layers left of the divider, the right layers right of it
    var C = cfg.compare, cdiv = document.getElementById("gm-compare"), cfrac = C ? C.position : 0.5, cstage = document.getElementById("gm-stage") || root;
    var tagL = document.getElementById("gm-cmp-tag-l"), tagR = document.getElementById("gm-cmp-tag-r");
    function setCompare(frac) {
      if (!C || !cdiv) return;
      cfrac = Math.max(0.02, Math.min(0.98, frac));
      var w = cstage.getBoundingClientRect().width, x = Math.round(cfrac * w);
      cdiv.style.left = x + "px";
      cdiv.setAttribute("aria-valuenow", String(Math.round(cfrac * 100)));
      if (tagL) tagL.style.right = (w - x + 8) + "px";
      if (tagR) tagR.style.left = (x + 8) + "px";
      E.setCompare(x);
    }
    if (C && cdiv) {
      var dragging = false, handle = cdiv.querySelector("button");
      var move = function (e) { if (!dragging) return; var r = cstage.getBoundingClientRect(); setCompare((e.clientX - r.left) / r.width); e.preventDefault(); };
      handle.addEventListener("pointerdown", function (e) { dragging = true; try { handle.setPointerCapture(e.pointerId); } catch (x) { /* older browsers */ } e.preventDefault(); });
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", function (e) { dragging = false; try { handle.releasePointerCapture(e.pointerId); } catch (x) { /* ignore */ } hashWrite(); });
      handle.addEventListener("keydown", function (e) {
        if (e.key === "ArrowLeft") { setCompare(cfrac - 0.02); e.preventDefault(); hashWrite(); }
        if (e.key === "ArrowRight") { setCompare(cfrac + 0.02); e.preventDefault(); hashWrite(); }
      });
      window.addEventListener("resize", function () { setCompare(cfrac); });
      if (window.ResizeObserver) new ResizeObserver(function () { setCompare(cfrac); }).observe(cstage);
    }

    // ---- layers by list (a story chapter): the checkboxes, the legend blocks and the map follow
    function setLayers(ids) {
      cfg.layers.forEach(function (d) {
        if (d.context || d.role === "hillshade") return;                  // the relief stays as the person set it
        var showIt = ids.indexOf(d.id) >= 0, cb = lsw && lsw.querySelector('input[value="' + d.id + '"]');
        E.setLayerVisible(d.id, showIt);
        if (cb) cb.checked = showIt;
        legendFor(d.id, showIt);
      });
    }

    // ---- KPI panel: the fields of the clicked feature, or totals over a chapter's selection (webmap.panel, M15)
    var P = cfg.panel, kpiWhat = document.getElementById("gm-kpi-what"), kpiDl = document.getElementById("gm-kpi-dl");
    function kpiFmt(v, f) {
      if (v == null || v === "") return "no value";
      var n = typeof v === "number" ? v : (/^-?\d+(\.\d+)?$/.test(String(v)) ? +v : null);
      if (n == null) return String(v);
      return n.toLocaleString("en-US", { minimumFractionDigits: f.decimals != null ? f.decimals : 0, maximumFractionDigits: f.decimals != null ? f.decimals : 2 }) + (f.unit ? " " + f.unit : "");
    }
    function kpiShow(what, rows) {
      if (!kpiDl) return;
      kpiWhat.textContent = what;
      kpiDl.textContent = "";
      rows.forEach(function (r) {
        var dt = document.createElement("dt"); dt.textContent = r[0];
        var dd = document.createElement("dd"); dd.textContent = r[1];
        kpiDl.appendChild(dt); kpiDl.appendChild(dd);
      });
    }
    function kpiFeature(def, p) {
      if (!P || !kpiDl) return;
      var rows = P.fields.filter(function (f) { return p[f.field] !== undefined; }).map(function (f) { return [f.label, kpiFmt(p[f.field], f)]; });
      if (!rows.length) return;
      var name = (def.popup && def.popup.title && p[def.popup.title] != null) ? String(p[def.popup.title]) : def.title;
      kpiShow(name, rows);
    }
    function kpiSelect(sel) {
      if (!P || !kpiDl || !sel) return;
      var want = (sel.in || []).map(String), col = data[sel.layer] || { features: [] };
      var fs = (col.features || []).filter(function (f) { return want.indexOf(String((f.properties || {})[sel.field])) >= 0; });
      var rows = [["Features", fs.length.toLocaleString("en-US")]];
      P.fields.forEach(function (f) {
        var vals = fs.map(function (x) { return +(x.properties || {})[f.field]; }).filter(function (v) { return isFinite(v); });
        if (!vals.length) return;
        var agg = f.agg || "sum", v = agg === "mean" ? vals.reduce(function (a, b) { return a + b; }, 0) / vals.length
          : agg === "min" ? Math.min.apply(null, vals) : agg === "max" ? Math.max.apply(null, vals) : vals.reduce(function (a, b) { return a + b; }, 0);
        rows.push([f.label + (agg === "sum" ? " (total)" : " (" + agg + ")"), kpiFmt(v, f)]);
      });
      kpiShow(sel.label || (sel.in || []).join(", "), rows);
    }
    hooks.feature = kpiFeature;

    // ---- story: the panel's Story tab holds the chapters; the active one flies the map to its view and shows its layers and period
    var S = cfg.story;
    function storyGo(i, how) {
      if (!S) return;
      i = Math.max(0, Math.min(S.chapters.length - 1, i | 0));
      if (i === storyIdx && how !== "force" && how !== "jump") return;
      storyIdx = i;
      var ch = S.chapters[i];
      if (ch.layers) setLayers(ch.layers);
      if (T && how !== "time") {                                            // a chapter with no period shows everything up to the last one
        if (ch.time != null && ch.time >= 0) setTime(ch.time); else if (T.mode === "cumulative") setTime(T.labels.length - 1);
      }
      if (ch.select) kpiSelect(ch.select); else if (P && kpiDl) kpiShow(P.hint, []);
      var kh = ch.select && kpiDl ? document.querySelector("#" + ch.id + " .gm-ch-kpi") : null;   // the chapter's totals in its card (spec 5.2)
      if (kh) { kh.textContent = ""; var dl = kpiDl.cloneNode(true); dl.removeAttribute("id"); dl.className = "gm-ch-kpi-dl"; kh.appendChild(dl); }
      E.flyTo(ch.view, how !== "jump");
      if (F) F.storyChanged(i, how);
      var live = document.getElementById("gm-story-live");
      if (live) live.textContent = "Chapter " + (i + 1) + " of " + S.chapters.length + ": " + ch.title;
      hashWrite();
    }
    function storyFocus(i) { storyGo(i, "key"); }

    // ---- state in the URL: view, layer visibility, background, names, period, divider, 2D or 3D and the chapter only
    function hashWrite() {
      if (!cfg.controls.share_state_in_url) return;
      var v = E.getView(), in3d = E.has3D && E.is3D();
      var vis = cfg.layers.filter(function (d) { return E.isLayerVisible(d.id); }).map(function (d) { return d.id; });
      var h = "#v=" + (Math.round(v.z * 100) / 100) + "/" + v.lat.toFixed(4) + "/" + v.lng.toFixed(4)
        + (in3d ? "/" + Math.round(v.pitch) + "/" + Math.round(v.bearing) : "") + "&l=" + vis.join(",") + (activeBase ? "&b=" + activeBase : "")
        + (cfg.names && cfg.names.on && !E.namesVisible() ? "&n=0" : "") + (T && tIndex != null ? "&t=" + tIndex : "") + (C ? "&c=" + Math.round(cfrac * 100) : "")
        + (E.has3D && !in3d ? "&d=2" : "") + (S && storyIdx >= 0 ? "&ch=" + storyIdx : "")
        + (F && F.state().p != null ? "&p=" + F.state().p : "") + (F && F.state().tab ? "&tab=" + F.state().tab : "");
      try { history.replaceState(null, "", h); } catch (e) { try { location.hash = h; } catch (e2) { /* sharing is a convenience */ } }
    }
    function hashRead() {
      if (!cfg.controls.share_state_in_url || !location.hash) return null;
      var m = {};
      location.hash.slice(1).split("&").forEach(function (kv) { var i = kv.indexOf("="); if (i > 0) m[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1)); });
      return m;
    }
    var startBase = basemaps.length ? basemaps[0].id : null;
    if (st && st.b && basemaps.some(function (b) { return b.id === st.b; })) startBase = st.b;
    activeBase = startBase;
    E.setBase(startBase);
    showWordmark(startBase);
    if (F) F.setBase(startBase);
    if (bsw && startBase) { var r0 = bsw.querySelector('input[value="' + startBase + '"]'); if (r0) r0.checked = true; }
    if (st && st.l !== undefined) {
      var want = st.l ? st.l.split(",") : [];
      cfg.layers.forEach(function (d) {
        var cb = lsw && lsw.querySelector('input[value="' + d.id + '"]');
        var showIt = want.indexOf(d.id) >= 0;
        E.setLayerVisible(d.id, showIt);
        if (cb) cb.checked = showIt;
        legendFor(d.id, showIt);
      });
    }
    if (st && st.n === "0" && cfg.names && cfg.names.on) {
      E.setNamesVisible(false);
      var ncb = lsw && lsw.querySelector('input[value="' + cfg.names.id + '"]'); if (ncb) ncb.checked = false;
    }
    if (T) setTime(st && st.t != null && isFinite(+st.t) ? +st.t : (T.start || 0));
    if (C) setCompare(st && st.c != null && isFinite(+st.c) ? +st.c / 100 : cfrac);
    if (E.has3D && st && st.d === "2") E.set3D(false, { animate: false });
    if (F) F.start();
    if (S) {
      var sch = st && (st.ch != null ? st.ch : st.s), s0 = sch != null && isFinite(+sch) ? +sch : 0;
      storyGo(s0, "jump");
    }
    if (st && st.v) {
      var p = st.v.split("/");
      if ((p.length === 3 || p.length === 5) && p.every(function (x) { return isFinite(+x); })) E.setView(+p[0], +p[1], +p[2], p.length === 5 ? +p[3] : null, p.length === 5 ? +p[4] : null);
    }
    E.onMove(hashWrite);
    hashWrite();                                                        // the address now says what the page shows, before anything moves
    root.classList.add("gm-ready");
    window.gmapMap = E.map;
    hooks.mode = function () { hashWrite(); };
    window.gmapPage = { engine: E.name, E: E, setTime: function (i) { setTime(i); hashWrite(); }, setCompare: function (f) { setCompare(f); hashWrite(); }, measure: M, geodesic: geodesic, ringArea: ringArea, idle: E.idle,
                        exportImage: exportImage, story: S ? { go: storyFocus, index: function () { return storyIdx; } } : null, frame: F,
                        set3D: E.set3D ? function (on) { E.set3D(on); } : null };
  }

  // ---- image export: the map as drawn, with the title, the legend of the visible layers and the credits drawn onto it, saved as a PNG
  //      that carries the same hidden brand metadata as the print PNG in its text chunks (never drawn)
  function saveBlob(blob, name) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
  }
  function exportImage(done) {
    if (!E.snapshot) {
      banner("export", "This page saves a picture through Print: choose Save as PDF there.");
      window.print();
      return;
    }
    var ready = document.fonts && document.fonts.load ? Promise.all([document.fonts.load("700 16px Inter"), document.fonts.load("400 12px Inter")]) : Promise.resolve();
    var plogos = F && F.creditLogos ? F.creditLogos(cfg.tokens.label) : [];          // 1.1.3: provider logos decoded before they are drawn
    ready = Promise.all([ready].concat(plogos.map(function (l) { return l.img.decode ? l.img.decode().catch(function () {}) : Promise.resolve(); })));
    ready.then(function () {
      E.snapshot(function (shot) {
        if (!shot) { banner("export", "The picture could not be made in this browser; use Print instead."); return; }
        var cv = compose(shot);
        cv.toBlob(function (blob) {
          blob.arrayBuffer().then(function (buf) {
            var out = new Blob([pngText(new Uint8Array(buf), (cfg.export || {}).text64 ? JSON.parse(atob(cfg.export.text64)) : {})], { type: "image/png" });
            window.gmapLastExport = { width: cv.width, height: cv.height, bytes: out.size };
            saveBlob(out, (cfg.export || {}).fileName || "map.png");
            if (typeof done === "function") done(out);
          });
        }, "image/png");
      });
    });
  }
  function compose(shot) {
    var tk = cfg.tokens, s = shot.scale || 1, W = shot.width, H = shot.height;
    var cv = document.createElement("canvas");
    cv.width = W; cv.height = H;
    var g = cv.getContext("2d");
    g.fillStyle = tk.water; g.fillRect(0, 0, W, H);
    shot.parts.forEach(function (p) {
      g.save();
      g.beginPath(); g.rect(p.x0, 0, p.x1 - p.x0, H); g.clip();
      g.drawImage(p.canvas, 0, 0, W, H);
      g.restore();
    });
    if (shot.divider != null) { g.fillStyle = tk.emphasis; g.fillRect(shot.divider - s, 0, 2 * s, H); }
    var pad = 10 * s;
    function wrap(text, font, maxW) {
      g.font = font;
      var words = String(text).split(/\s+/), lines = [], cur = "";
      words.forEach(function (w) { var t = cur ? cur + " " + w : w; if (g.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t; });
      if (cur) lines.push(cur);
      return lines;
    }
    function panel(x, y, w, h) {
      g.fillStyle = tk.legend_bg; g.strokeStyle = tk.legend_frame; g.lineWidth = s;
      g.fillRect(x, y, w, h); g.strokeRect(x + s / 2, y + s / 2, w - s, h - s);
    }
    // title, top left
    var tEl = document.getElementById("gm-title"), sEl = document.getElementById("gm-subtitle");
    if (tEl) {
      var maxW = Math.min(420 * s, W * 0.5);
      var tl = wrap(tEl.textContent, "700 " + 16 * s + "px Inter, sans-serif", maxW), sl = sEl ? wrap(sEl.textContent, "400 " + 12 * s + "px Inter, sans-serif", maxW) : [];
      var th = pad * 2 + tl.length * 20 * s + sl.length * 16 * s;
      var tw = 0;
      g.font = "700 " + 16 * s + "px Inter, sans-serif"; tl.forEach(function (l) { tw = Math.max(tw, g.measureText(l).width); });
      g.font = "400 " + 12 * s + "px Inter, sans-serif"; sl.forEach(function (l) { tw = Math.max(tw, g.measureText(l).width); });
      panel(pad, pad, tw + pad * 2, th);
      g.fillStyle = tk.label; g.textBaseline = "top";
      g.font = "700 " + 16 * s + "px Inter, sans-serif";
      tl.forEach(function (l, i) { g.fillText(l, pad * 2, pad * 2 + i * 20 * s); });
      g.font = "400 " + 12 * s + "px Inter, sans-serif";
      sl.forEach(function (l, i) { g.fillText(l, pad * 2, pad * 2 + tl.length * 20 * s + i * 16 * s); });
    }
    // credits, bottom right: the credits corner in full (every required credit, the OpenStreetMap address in full when its data is drawn);
    // the provider's logo and the brand chip bottom left (1.1.0, spec 8.6); the method notes stay in the page's About tab
    var credits = F && F.creditsExport ? F.creditsExport() : [];
    if (!credits.length) { var at = (E.attributionText ? E.attributionText() : "").replace(/\s+/g, " ").trim(); if (at) credits.push(at); }
    if (credits.some(function (c) { return /OpenStreetMap/.test(c); }) && !credits.some(function (c) { return /openstreetmap\.org\/copyright/.test(c); })) credits.push("openstreetmap.org/copyright");
    g.font = "400 " + 11 * s + "px Inter, sans-serif";
    var cl = [];
    credits.forEach(function (c) { wrap(c, "400 " + 11 * s + "px Inter, sans-serif", W * 0.6).forEach(function (l) { cl.push(l); }); });
    var cw = 0; cl.forEach(function (l) { cw = Math.max(cw, g.measureText(l).width); });
    var ch = cl.length * 14 * s + pad;
    var lgs = (F && F.creditLogos ? F.creditLogos(tk.label) : []).filter(function (l) { return l.img.complete && l.img.naturalWidth; });
    var lgw = 0; lgs.forEach(function (l) { lgw += l.w * s + 4 * s; });            // 1.1.3: the provider logos sit in the panel, before the credits
    if (cl.length) {
      panel(W - cw - pad * 2 - lgw, H - ch - pad / 2, cw + pad * 2 + lgw, ch + pad / 2);
      g.fillStyle = tk.label; g.textBaseline = "top";
      cl.forEach(function (l, i) { g.fillText(l, W - cw - pad, H - ch + i * 14 * s); });
      var lx = W - cw - pad - lgw;
      lgs.forEach(function (l) { g.drawImage(l.img, lx, H - ch + 1 * s, l.w * s, l.h * s); lx += l.w * s + 4 * s; });
    }
    var bx = pad, wmImg = document.querySelector("#gm-wordmark:not([hidden]) img");
    if (wmImg && wmImg.complete && wmImg.naturalWidth) {        // the provider's logo keeps its corner (Mapbox: static images too)
      g.drawImage(wmImg, pad, H - 23 * s - pad, 88 * s, 23 * s); bx = pad + 88 * s + 8 * s;
    }
    var brandImg = window.gmapBrandImage;                      // the brand chip, burned in at the same corner as on the page
    var bName = cfg.frame && cfg.frame.brand && cfg.frame.brand.show !== "off" ? cfg.frame.brand : null;
    if (bName) {
      var bh = 28 * s, lw0 = brandImg && brandImg.complete && brandImg.naturalWidth ? 20 * s * brandImg.naturalWidth / brandImg.naturalHeight : 0;
      g.font = "600 " + 12 * s + "px Inter, sans-serif";
      var tw0 = bName.show === "mark" && lw0 ? 0 : g.measureText(bName.name).width;
      var bw = 4 * s + lw0 + (lw0 && tw0 ? 6 * s : 0) + tw0 + 10 * s, by = H - bh - pad;
      g.fillStyle = tk.legend_bg; g.strokeStyle = tk.legend_frame; g.lineWidth = s;
      g.beginPath(); if (g.roundRect) g.roundRect(bx, by, bw, bh, bh / 2); else g.rect(bx, by, bw, bh); g.fill(); g.stroke();
      if (lw0) g.drawImage(brandImg, bx + 4 * s, by + 4 * s, lw0, 20 * s);
      if (tw0) { g.fillStyle = tk.label; g.textBaseline = "middle"; g.fillText(bName.name, bx + 4 * s + lw0 + (lw0 ? 6 * s : 0), by + bh / 2); }
    }
    // legend, top right (as on the print map): the blocks of the visible layers
    var blocks = (cfg.legend || []).filter(function (b) { return !b.layer || !defById[b.layer] || E.isLayerVisible(b.layer); });
    if (blocks.length) {
      var rows = [], rh = 17 * s, lw = 0, skipped = 0, bvc = 14 * s;
      // 1.0.0: a picture legend is drawn when the browser lets the picture be copied (a picture in the page, or from this page's own host);
      // a picture from another server would make the canvas unreadable, so it is named instead of drawn
      function pictureOf(href) {
        if (!href) return null;
        var ok = href.indexOf("data:") === 0;
        if (!ok) { try { ok = new URL(href, location.href).origin === location.origin && location.origin !== "null"; } catch (e) { ok = false; } }
        if (!ok) return null;
        var all = document.querySelectorAll("img.gm-lg-img, img.gm-sw-img");
        for (var i = 0; i < all.length; i++) { if (all[i].getAttribute("src") === href && all[i].complete && all[i].naturalWidth) return all[i]; }
        return null;
      }
      blocks.forEach(function (b) {
        if (b.title) rows.push({ t: "title", text: b.title, h: rh });
        if (b.kind === "ramp") rows.push({ t: "ramp", b: b, h: 2 * rh });
        else if (b.kind === "bivariate" && b.cells) {
          rows.push({ t: "biv", b: b, h: b.n * bvc + 2 * rh });
          if (b.nodata) rows.push({ t: "item", b: { kind: "classes" }, it: { label: b.nodata_label || "No data", fill: b.nodata }, h: rh });
        } else if (b.kind === "image") {
          (b.items || []).forEach(function (it) {
            var im = pictureOf(it.href);
            if (!im) { skipped++; rows.push({ t: "title", text: "Legend picture from the service (see the page)", h: rh, faint: true }); return; }
            var k = Math.min(1, 200 * s / im.naturalWidth, 160 * s / im.naturalHeight, s);
            rows.push({ t: "img", im: im, w: im.naturalWidth * k, h: im.naturalHeight * k + 4 * s });
          });
        } else (b.items || []).forEach(function (it) { rows.push({ t: "item", b: b, it: it, h: rh, im: b.kind === "swatches" ? pictureOf(it.href) : null }); });
      });
      rows.forEach(function (r) {
        g.font = (r.t === "title" ? "600 " : "400 ") + 11 * s + "px Inter, sans-serif";
        lw = Math.max(lw, r.t === "item" ? 30 * s + g.measureText(r.it.label || "").width : r.t === "ramp" ? 200 * s : r.t === "img" ? r.w :
          r.t === "biv" ? Math.max(r.b.n * bvc + 70 * s, g.measureText((r.b.x_title || "") + " \u2192").width + 30 * s) : g.measureText(r.text).width);
      });
      var lh = rows.reduce(function (a, r) { return a + r.h; }, 0) + pad * 2;
      var ly = pad, lx = W - lw - pad * 3;
      panel(lx, ly, lw + pad * 2, lh);
      var y = ly + pad;
      g.textBaseline = "middle";
      window.gmapLastExportLegend = { rows: rows.length, pictures_named: skipped };
      rows.forEach(function (r) {
        var x = lx + pad, cy = y + rh / 2;
        if (r.t === "img") { g.drawImage(r.im, x, y + 2 * s, r.w, r.h - 4 * s); y += r.h; return; }
        if (r.t === "biv") {
          var n = r.b.n, gx = x + 30 * s, top = y + rh;
          g.font = "600 " + 10 * s + "px Inter, sans-serif"; g.fillStyle = tk.label;
          g.fillText("\u2191 " + (r.b.y_title || ""), x, y + rh / 2);
          for (var j = 0; j < n; j++) for (var i = 0; i < n; i++) {
            g.fillStyle = r.b.cells[j][i];
            g.fillRect(gx + i * bvc, top + (n - 1 - j) * bvc, bvc - s, bvc - s);
          }
          g.fillStyle = tk.label;
          g.fillText((r.b.x_title || "") + " \u2192", gx, top + n * bvc + rh / 2);
          y += r.h; return;
        }
        if (r.t === "title") { g.font = "600 " + 11 * s + "px Inter, sans-serif"; g.fillStyle = r.faint ? tk.legend_frame : tk.label; g.fillText(r.text, x, cy); }
        else if (r.t === "ramp") {
          var stops = r.b.stops || [], gr = g.createLinearGradient(x, 0, x + 180 * s, 0);
          stops.forEach(function (c, i) { gr.addColorStop(stops.length > 1 ? i / (stops.length - 1) : 0, c); });
          g.fillStyle = gr; g.fillRect(x, cy - 5 * s, 180 * s, 10 * s);
          g.font = "400 " + 10 * s + "px Inter, sans-serif"; g.fillStyle = tk.label;
          (r.b.items || []).forEach(function (it) { g.fillText(it.label, x + 180 * s * (it.pos || 0) - g.measureText(it.label).width / 2, cy + rh); });
        } else {
          var it = r.it, kind = r.b.kind;
          g.fillStyle = it.fill || tk.label; g.strokeStyle = it.stroke || it.fill || tk.label; g.lineWidth = s;
          if (r.im) g.drawImage(r.im, x + 3 * s, cy - 8 * s, 16 * s, 16 * s);
          else if (kind === "lines") { g.lineWidth = 3 * s; g.setLineDash(it.dash ? [6 * s, 4 * s] : []); g.beginPath(); g.moveTo(x, cy); g.lineTo(x + 22 * s, cy); g.stroke(); g.setLineDash([]); }
          else if (kind === "points" || kind === "proportional" || kind === "categories" && it.shape) {
            var rr = Math.max(3 * s, Math.min(9 * s, (it.size_mm || 2.5) * 1.9 * s));
            g.beginPath(); g.arc(x + 11 * s, cy, rr, 0, 2 * Math.PI); g.fill(); g.stroke();
          } else { g.fillRect(x, cy - 6 * s, 22 * s, 12 * s); g.strokeRect(x, cy - 6 * s, 22 * s, 12 * s); }
          g.font = "400 " + 11 * s + "px Inter, sans-serif"; g.fillStyle = tk.label;
          g.fillText(it.label || "", x + 30 * s, cy);
        }
        y += r.h;
      });
    }
    return cv;
  }
  var CRC = null;
  function crc32(bytes) {
    if (!CRC) { CRC = []; for (var n = 0; n < 256; n++) { var c = n; for (var k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; CRC[n] = c >>> 0; } }
    var x = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) x = CRC[(x ^ bytes[i]) & 0xFF] ^ (x >>> 8);
    return (x ^ 0xFFFFFFFF) >>> 0;
  }
  function pngText(png, kv) {
    // tEXt chunks (Latin-1 keyword, a zero byte, Latin-1 text) inserted after IHDR: the same keys the print PNG carries
    var chunks = [];
    Object.keys(kv).forEach(function (k) {
      var s = k + "\u0000" + String(kv[k]), body = new Uint8Array(4 + s.length);
      body.set([0x74, 0x45, 0x58, 0x74]);
      for (var i = 0; i < s.length; i++) body[4 + i] = s.charCodeAt(i) & 0xFF;
      var len = s.length, c = crc32(body), ch = new Uint8Array(12 + len);
      ch.set([len >>> 24 & 255, len >>> 16 & 255, len >>> 8 & 255, len & 255]); ch.set(body, 4);
      ch.set([c >>> 24 & 255, c >>> 16 & 255, c >>> 8 & 255, c & 255], 8 + len);
      chunks.push(ch);
    });
    var at = 33, total = png.length + chunks.reduce(function (a, c) { return a + c.length; }, 0), out = new Uint8Array(total), o = at;
    out.set(png.subarray(0, at));
    chunks.forEach(function (c) { out.set(c, o); o += c.length; });
    out.set(png.subarray(at), o);
    return out;
  }
})();
