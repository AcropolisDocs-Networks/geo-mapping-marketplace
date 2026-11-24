/* gmap web map page: the frame (1.1.0). One panel (title, tabs, footer), one control rail, one playback dock, the brand chip and the credits
   corner. Engine-neutral: it calls the engine adapter (window.gmapEngine's object, passed in by page.js) and the page's own functions through
   the context page.js hands to init(). No network calls of its own. Configuration: cfg.frame (webmap_frame.py). */
(function () {
  "use strict";
  window.gmapFrame = { init: init };

  function init(x) {
    var cfg = x.cfg, E = x.E, root = x.root, reduce = x.reduce, fr = cfg.frame || {}, store = null;
    var stage = document.getElementById("gm-stage"), panel = document.getElementById("gm-panel");
    var mapId = document.documentElement.getAttribute("data-map-id") || "map";
    var F = { tab: null, open: false, interacted: false };
    var mqPhone = window.matchMedia ? window.matchMedia("(max-width: 639px)") : { matches: false, addListener: function () {} };
    var mqTab = window.matchMedia ? window.matchMedia("(min-width: 640px) and (max-width: 1023px)") : { matches: false, addListener: function () {} };
    try { store = window.localStorage; store.getItem("gm-probe"); } catch (e) { store = null; }
    function $(id) { return document.getElementById(id); }
    function on(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); return el; }
    function typing(e) { var t = e.target, n = t && t.tagName; return n === "INPUT" || n === "SELECT" || n === "TEXTAREA" || (t && t.isContentEditable); }

    var fl = document.getElementById("gm-flegend"), flBtn = document.getElementById("gm-flegend-btn");
    // ---- the panel: open, collapsed or off; desktop pushes the map, a tablet's drawer and a phone's sheet lie over it
    var hasPanel = !!panel && (fr.panel || {}).show !== "off";
    function remembered() {
      var h = x.hashState || {};
      if (h.p === "0" || h.p === "1") return h.p === "1";
      if ((fr.panel || {}).remember !== false && store) { try { var v = store.getItem("gm-panel:" + mapId); if (v === "0" || v === "1") return v === "1"; } catch (e) { /* storage is a convenience */ } }
      if (mqTab.matches && !cfg.story) return false;
      return (fr.panel || {}).start !== "collapsed";
    }
    function setOpen(open, why) {
      if (fl) legendFloat(!!(fr.legend || {}).float && !open);
      if (!hasPanel) { root.classList.add("gm-nopanel"); F.open = false; return; }
      F.open = !!open;
      if (mqPhone.matches) F.open = true;                    // the sheet is always there; its height changes
      root.classList.toggle("gm-collapsed", !F.open);
      root.setAttribute("data-panel", F.open ? "open" : "collapsed");
      ["gm-collapse", "gm-expand"].forEach(function (id) { var b = $(id); if (b) b.setAttribute("aria-expanded", F.open ? "true" : "false"); });
      if (why !== "init" && (fr.panel || {}).remember !== false && store) { try { store.setItem("gm-panel:" + mapId, F.open ? "1" : "0"); } catch (e) { /* ignore */ } }
      if (why !== "init") { if (x.hashWrite) x.hashWrite(); }
      settle(why === "init" ? 0 : (reduce ? 0 : 200));
    }
    on("gm-collapse", "click", function () { setOpen(false); var b = $("gm-expand"); if (b) b.focus(); });
    on("gm-expand", "click", function () { setOpen(true); var t = $("gm-tab-" + (F.tab || "")); if (t) t.focus(); });

    // the map resizes once the panel has moved, keeping its centre in the visible map (setPadding/panBy where the panel lies over it)
    var settleT = null;
    function settle(ms) {
      clearTimeout(settleT);
      settleT = setTimeout(function () { if (E.resize) E.resize(); pad(); layoutBottom(); }, ms || 0);
    }
    function pad() {
      var p = { top: 0, right: 0, bottom: 0, left: 0 };
      if (hasPanel && panel && F.open && mqTab.matches) p[(fr.panel || {}).side === "right" ? "right" : "left"] = panel.getBoundingClientRect().width;
      if (E.setPadding) E.setPadding(p);
    }
    if (window.ResizeObserver && stage) new ResizeObserver(function () { settle(60); }).observe(stage);
    window.addEventListener("resize", function () { settle(60); });

    // ---- the phone's sheet: peek, half, full (the handle, a tap or a drag)
    var sheet = "peek", handle = $("gm-handle");
    function peekH() { return parseFloat(getComputedStyle(root).getPropertyValue("--gm-peek")) || 120; }
    function setSheet(s) {
      sheet = s; root.setAttribute("data-sheet", s);
      if (handle) { handle.setAttribute("aria-expanded", s === "peek" ? "false" : "true"); handle.setAttribute("aria-label", "Panel: " + s + ". Tap to change its height"); }
      settle(reduce ? 0 : 200);
    }
    function measurePeek() {
      if (!mqPhone.matches || !panel) return;
      var last = foot && foot.childNodes.length ? foot : panel.querySelector(".gm-ph");
      if (!last) return;
      var h = last.getBoundingClientRect().bottom - panel.getBoundingClientRect().top;
      root.style.setProperty("--gm-peek", Math.max(96, Math.min(220, Math.round(h + 6))) + "px");
    }
    if (handle) {
      handle.addEventListener("click", function () { if (dragged) return; setSheet(sheet === "peek" ? "half" : sheet === "half" ? "full" : "peek"); });
      var y0 = null, h0 = 0, dragged = false;
      handle.addEventListener("pointerdown", function (e) { y0 = e.clientY; h0 = panel.getBoundingClientRect().height; dragged = false; try { handle.setPointerCapture(e.pointerId); } catch (er) { /* old browsers */ } });
      handle.addEventListener("pointermove", function (e) {
        if (y0 === null) return;
        var dy = y0 - e.clientY; if (Math.abs(dy) > 6) dragged = true;
        if (dragged) { panel.style.transition = "none"; panel.style.height = Math.max(peekH(), Math.min(root.clientHeight - 48, h0 + dy)) + "px"; }
      });
      handle.addEventListener("pointerup", function () {
        if (y0 === null) return;
        var h = panel.getBoundingClientRect().height, H = root.clientHeight; y0 = null;
        panel.style.transition = ""; panel.style.height = "";
        if (dragged) setSheet(h < H * 0.3 ? "peek" : h < H * 0.75 ? "half" : "full");
        setTimeout(function () { dragged = false; }, 0);
      });
    }

    // ---- tabs (ARIA tab pattern): a tab with no content is not drawn; 1-5 open them
    var tabs = [].slice.call(document.querySelectorAll(".gm-tab"));
    function openTab(name, focus, quiet) {
      if (!tabs.length) return;
      var t = tabs.filter(function (b) { return b.getAttribute("data-tab") === name; })[0] || tabs[0];
      F.tab = t.getAttribute("data-tab");
      tabs.forEach(function (b) {
        var sel = b === t, pane = $(b.getAttribute("aria-controls"));
        b.setAttribute("aria-selected", sel ? "true" : "false"); b.tabIndex = sel ? 0 : -1;
        if (pane) pane.hidden = !sel;
      });
      if (focus) t.focus();
      if (mqPhone.matches && sheet === "peek" && !quiet) setSheet("half");
      if (x.hashWrite && !quiet) x.hashWrite();
    }
    tabs.forEach(function (b, i) {
      b.addEventListener("click", function () { openTab(b.getAttribute("data-tab")); });
      b.addEventListener("keydown", function (e) {
        var k = null;
        if (e.key === "ArrowRight") k = (i + 1) % tabs.length; else if (e.key === "ArrowLeft") k = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") k = 0; else if (e.key === "End") k = tabs.length - 1;
        if (k !== null) { e.preventDefault(); openTab(tabs[k].getAttribute("data-tab"), true); }
      });
    });
    F.openTab = openTab;

    // ---- the About text: two lines, then More
    var about = document.querySelector(".gm-about.gm-clamp"), more = $("gm-about-more");
    if (about && more) {
      requestAnimationFrame(function () { if (about.scrollHeight <= about.clientHeight + 2) more.hidden = true; });
      more.addEventListener("click", function () { var c = about.classList.toggle("gm-clamp"); more.textContent = c ? "More" : "Less"; more.setAttribute("aria-expanded", c ? "false" : "true"); });
    }

    // ---- the control rail: page buttons calling the engine, so both engines look the same
    on("gm-zoom-in", "click", function () { E.zoomBy(1); });
    on("gm-zoom-out", "click", function () { E.zoomBy(-1); });
    var compass = $("gm-compass");
    function rotated() {
      if (!compass || !E.getBearingPitch) return;
      var bp = E.getBearingPitch(), off = Math.abs(bp.bearing) > 0.5 || bp.pitch > 0.5;
      compass.hidden = !off;
      var n = compass.querySelector(".gm-needle"); if (n) n.style.transform = "rotate(" + (-bp.bearing) + "deg)";
    }
    if (compass) { compass.addEventListener("click", function () { E.resetNorth(); }); if (E.onRotate) E.onRotate(rotated); rotated(); }
    var fadeT = null, rail = $("gm-rail");
    if (fr.fade_rail && rail) {
      var wake = function () { rail.classList.remove("gm-faded"); clearTimeout(fadeT); fadeT = setTimeout(function () { rail.classList.add("gm-faded"); }, 3000); };
      ["pointermove", "keydown", "touchstart"].forEach(function (ev) { root.addEventListener(ev, wake, { passive: true }); });
      wake();
    }

    // ---- the tools menu (the ⋯ in the panel header or the title chip)
    var menu = $("gm-tools-menu"), menuFrom = null;
    function closeMenu() { if (menu && !menu.hidden) { menu.hidden = true; if (menuFrom) { menuFrom.setAttribute("aria-expanded", "false"); menuFrom.focus(); } } }
    function openMenu(btn) {
      if (!menu) return;
      menuFrom = btn; menu.hidden = false; btn.setAttribute("aria-expanded", "true");
      var r = btn.getBoundingClientRect(), rr = root.getBoundingClientRect();
      menu.style.top = (r.bottom - rr.top + 4) + "px";
      menu.style.left = Math.max(8, Math.min(rr.width - menu.offsetWidth - 8, r.right - rr.left - menu.offsetWidth)) + "px";
      var first = menu.querySelector("button"); if (first) first.focus();
    }
    [].forEach.call(document.querySelectorAll("[data-menu=tools]"), function (b) {
      b.addEventListener("click", function (e) { e.stopPropagation(); if (menu && !menu.hidden) closeMenu(); else openMenu(b); });
    });
    if (menu) {
      menu.addEventListener("click", function (e) { if (e.target.closest("button")) setTimeout(closeMenu, 0); });
      menu.addEventListener("keydown", function (e) {
        var bs = [].slice.call(menu.querySelectorAll("button")), i = bs.indexOf(document.activeElement);
        if (e.key === "ArrowDown") { e.preventDefault(); bs[(i + 1) % bs.length].focus(); }
        if (e.key === "ArrowUp") { e.preventDefault(); bs[(i - 1 + bs.length) % bs.length].focus(); }
        if (e.key === "Escape") closeMenu();
      });
      document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
    }
    function copyLink() {
      var done = function () { x.banner("link", "The link to this view is copied."); };
      try { if (navigator.clipboard) navigator.clipboard.writeText(location.href).then(done, function () { x.banner("link", location.href); }); else x.banner("link", location.href); }
      catch (e) { x.banner("link", location.href); }
    }
    // a page delivered as a file and as an artifact (Day 10 issue 14): inside the artifact viewer (a framed https page) a download or the print
    // dialog cannot start, so those tools step aside there and the About tab says the file version has them (spec 13.3)
    var framed = false;
    try { framed = window.self !== window.top; } catch (e) { framed = true; }
    if (framed && location.protocol !== "file:" && (fr.delivery || []).indexOf("artifact") >= 0) {
      ["gm-export", "gm-download", "gm-print"].forEach(function (id) { var b = $(id); if (b) b.hidden = true; });
      var nt = $("gm-notes"); if (nt) { var p0 = document.createElement("p"); p0.textContent = "Saving a picture, downloading the data and printing work in the file version of this page."; nt.appendChild(p0); }
    }
    on("gm-copy-link", "click", copyLink);
    on("gm-share", "click", copyLink);
    var help = $("gm-help");
    function toggleHelp(show) { if (!help) return; help.hidden = show === undefined ? !help.hidden : !show; if (!help.hidden) { var c = help.querySelector("button"); if (c) c.focus(); } }
    on("gm-keys", "click", function () { toggleHelp(true); });
    on("gm-help-close", "click", function () { toggleHelp(false); });

    // ---- search box: a button in the panel header opens it
    var sbox = document.querySelector(".gm-search-box");
    on("gm-search-btn", "click", function () {
      if (!sbox) return;
      sbox.hidden = !sbox.hidden; this.setAttribute("aria-expanded", sbox.hidden ? "false" : "true");
      if (!sbox.hidden) { var i = $("gm-search"); if (i) i.focus(); }
    });

    // ---- the floating legend (panel collapsed or off): a Legend button on the title chip opens it
    function legendFloat(show) {
      if (!fl) return;
      if (show && !fl.childNodes.length) {                 // the Legend tab's blocks, copied once (the page carries one legend)
        var src = document.getElementById("gm-tp-legend");
        if (src) [].forEach.call(src.querySelectorAll(".gm-lg"), function (b) {
          var c = b.cloneNode(true); [].forEach.call(c.querySelectorAll("button, .gm-lg-hidden"), function (x) { x.remove(); });
          fl.appendChild(c);
        });
      }
      fl.hidden = !(show && (!F.open || !hasPanel) && !mqPhone.matches);
      if (flBtn) flBtn.setAttribute("aria-expanded", fl.hidden ? "false" : "true");
    }
    if (flBtn) flBtn.addEventListener("click", function () { legendFloat(fl.hidden); });

    // ---- the brand chip, the scale bar, the dock and the credits share the bottom row; the dock rises above them when it does not fit
    var dock = $("gm-time"), credits = $("gm-credits"), brand = $("gm-brand"), wm = $("gm-wordmark");
    function layoutBottom() {
      if (!stage) return;
      var W = stage.clientWidth, left = 8, hL = 0, hR = 0;
      if (wm && !wm.hidden && wm.offsetWidth) { left = 8 + wm.offsetWidth + 8; hL = wm.offsetHeight + 8; }
      if (brand && getComputedStyle(brand).display !== "none") { brand.style.left = left + "px"; left += brand.offsetWidth + 8; hL = Math.max(hL, brand.offsetHeight + 8); }
      root.style.setProperty("--gm-bl", left + "px");
      var sc = stage.querySelector(".maplibregl-ctrl-bottom-left .maplibregl-ctrl-scale, .leaflet-bottom.leaflet-left .leaflet-control-scale");
      var scW = 0;
      if (sc) { var r = sc.getBoundingClientRect(); scW = r.width + 8; hL = Math.max(hL, r.height + 8); }
      var cw = credits ? credits.offsetWidth : 0; hR = credits ? credits.offsetHeight : 0;
      if (dock && dock.classList.contains("gm-dock-map")) {
        var side = Math.max(left + scW, cw) + 12, room = W - 2 * side;
        if (room >= 360) { dock.style.bottom = "8px"; dock.style.width = Math.min(720, room) + "px"; }
        else { dock.style.bottom = (Math.max(hL, hR) + 12) + "px"; dock.style.width = Math.max(Math.min(360, W - 32), Math.min(720, W - 32)) + "px"; }
      }
    }
    F.layoutBottom = layoutBottom;

    // ---- the credits corner: the required credits of the active basemap and the shown layers, each holder once; never covered
    var items = (fr.credits || []), activeBase = null, collapsed = false, dyn = {};
    var list = credits ? credits.querySelector(".gm-cr-list") : null, pop = $("gm-cr-pop"), info = $("gm-cr-info");
    function norm(s) { return String(s || "").replace(/©/g, "").replace(/[\s.]+/g, " ").trim().toLowerCase(); }
    function applies(it) {
      if (!it.bases.length && !it.layers.length) return true;
      if (it.bases.indexOf(activeBase) >= 0) return true;
      return it.layers.some(function (id) { return E.isLayerVisible(id); });
    }
    function dynText(it) {
      // Esri: "Powered by Esri" and the contributors whose coverage meets this view and zoom (embedded at build; the page calls no extra host)
      var d = it.dynamic && it.dynamic.contributors;
      if (!d || !E.getView) return it.corner;
      var v = E.getView(), b = E.getBounds ? E.getBounds() : null, z = Math.round(v.z), names = [];
      d.forEach(function (c) {
        var ok = (c.coverageAreas || []).some(function (a) {
          if (z < (a.zoomMin || 0) || z > (a.zoomMax != null ? a.zoomMax : 99)) return false;
          if (!b || !a.bbox) return true;
          return !(a.bbox[3] < b[0] || a.bbox[1] > b[2] || a.bbox[2] < b[1] || a.bbox[0] > b[3]);
        });
        if (ok && names.indexOf(c.attribution) < 0) names.push(c.attribution);
      });
      return names.length ? (it.dynamic.lead || "Powered by Esri") + " | " + names.join(", ") : it.corner;
    }
    function improveUrl(href) {
      if (href.indexOf("{lng}") < 0 || !E.getView) return href;
      var v = E.getView();
      return href.replace("{lng}", v.lng.toFixed(5)).replace("{lat}", v.lat.toFixed(5)).replace("{zoom}", (v.z - 1).toFixed(2));
    }
    function shown() {
      var act = items.filter(function (it) { return it.required && applies(it); });
      return act.filter(function (it) {                    // a holder another credit already names, word for word, is printed once
        var n = norm(it.corner);
        return !act.some(function (o) { return o !== it && norm(o.corner).length > n.length && norm(o.corner).indexOf(n) >= 0; });
      });
    }
    function renderCredits() {
      if (!list) return;
      var act = shown(), phoneAuto = (fr.attribution || {}).collapse !== "never" && mqPhone.matches;
      list.textContent = "";
      act.forEach(function (it) {
        var shortNow = collapsed && phoneAuto && it.collapse !== "never";
        if (shortNow && !it.short) return;
        var li = document.createElement("li"); li.className = "gm-cr"; li.setAttribute("data-key", it.key);
        var txt = shortNow ? it.short : (it.dynamic ? dynText(it) : it.corner);
        if (it.logo && it.logo.svg) {                      // 1.1.3: the provider's logo its terms require, before its credit, never collapsed away
          var lgw = document.createElement(it.logo.link ? "a" : "span"); lgw.className = "gm-cr-logo-wrap";
          if (it.logo.link) { lgw.href = it.logo.link; lgw.target = "_blank"; lgw.rel = "noopener"; lgw.setAttribute("aria-label", it.logo.alt); }
          lgw.innerHTML = it.logo.svg; li.appendChild(lgw);
        }
        var main = it.links.filter(function (l) { var t = norm(l.text); return t && norm(txt).indexOf(t) >= 0; })[0] || it.links[0];
        if (main && !shortNow) { var a = document.createElement("a"); a.href = main.href; a.target = "_blank"; a.rel = "noopener"; a.textContent = txt; li.appendChild(a); }
        else li.textContent = txt;
        it.links.forEach(function (l) {                    // a provider's extra required link (Mapbox: Improve this map), kept current with the view
          if (l === main || shortNow || norm(txt).indexOf(norm(l.text)) >= 0) return;
          var s = document.createElement("span"); s.textContent = " ";
          var a2 = document.createElement("a"); a2.href = improveUrl(l.href); a2.target = "_blank"; a2.rel = "noopener"; a2.textContent = l.text;
          li.appendChild(s); li.appendChild(a2);
        });
        list.appendChild(li);
      });
      credits.hidden = false;
      credits.classList.toggle("gm-empty", !list.childNodes.length);
      layoutBottom();
    }
    function renderPop() {
      if (!pop) return;
      pop.textContent = "";
      var h = document.createElement("h2"); h.textContent = "Credits on this map"; pop.appendChild(h);
      var ul = document.createElement("ul");
      shown().forEach(function (it) {
        var li = document.createElement("li"); li.textContent = it.full + " ";
        it.links.forEach(function (l) { var a = document.createElement("a"); a.href = improveUrl(l.href); a.target = "_blank"; a.rel = "noopener"; a.textContent = l.text; li.appendChild(a); li.appendChild(document.createTextNode(" ")); });
        ul.appendChild(li);
      });
      pop.appendChild(ul);
      var src = $("gm-sources-line");
      if (src) { var h2 = document.createElement("h2"); h2.textContent = "Sources"; pop.appendChild(h2); var p = document.createElement("p"); p.textContent = src.textContent; pop.appendChild(p); }
      if (hasPanel && $("gm-tab-about")) {
        var b = document.createElement("button"); b.type = "button"; b.className = "gm-tb"; b.textContent = "Data licences and notes";
        b.addEventListener("click", function () { togglePop(false); setOpen(true); openTab("about"); var d = $("gm-licences"); if (d) d.scrollIntoView({ block: "start" }); });
        pop.appendChild(b);
      }
    }
    function togglePop(show) {
      if (!pop || !info) return;
      if (show === undefined) show = pop.hidden;
      if (show) renderPop();
      pop.hidden = !show; info.setAttribute("aria-expanded", show ? "true" : "false");
      if (!show) info.focus();
    }
    if (info) info.addEventListener("click", function (e) { e.stopPropagation(); togglePop(); });
    if (pop) {
      pop.addEventListener("keydown", function (e) { if (e.key === "Escape") togglePop(false); });
      document.addEventListener("click", function (e) { if (!pop.hidden && !pop.contains(e.target) && e.target !== info) togglePop(false); });
    }
    function interacted() {
      if (F.interacted) return;
      F.interacted = true;
      if (mqPhone.matches && (fr.attribution || {}).collapse !== "never") { collapsed = true; renderCredits(); }
    }
    setTimeout(interacted, 5000);                          // OSMF guideline: may shrink after interaction or five seconds, the ⓘ always leads back
    F.credits = renderCredits;
    F.setBase = function (id) { activeBase = id; renderCredits(); };
    F.creditsText = function () { return shown().map(function (it) { return it.export || it.corner; }).join(" · "); };
    F.creditsExport = function () { return shown().map(function (it) { return it.export || it.full || it.corner; }); };
    // 1.1.3: the provider logos of the credits shown, as pictures Save image can draw (a data: SVG in the label colour never taints the canvas)
    var logoImgs = {};
    F.creditLogos = function (colour) {
      return shown().filter(function (it) { return it.logo && it.logo.svg; }).map(function (it) {
        var k = it.logo.file + "|" + colour;
        if (!logoImgs[k]) {
          var im = new Image(); im.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(it.logo.svg.replace(/currentColor/g, colour));
          logoImgs[k] = im;
        }
        return { img: logoImgs[k], w: it.logo.w, h: it.logo.h };
      });
    };

    // ---- details: a click fills the Details tab while the panel is open; a popup when it is collapsed or off (frame.popups: auto)
    F.claimFeature = function (def, p) {
      var mode = fr.popups || "auto", tab = $("gm-tab-details");
      if (mode === "popup" || !tab) return false;
      if (mode === "off") return true;
      if (mode === "auto" && (!F.open || !hasPanel || (mqPhone.matches && sheet === "peek"))) return false;
      var box = $("gm-details-feature");
      if (box) { box.textContent = ""; box.appendChild(x.popupContent(def, p)); }
      var hint = $("gm-details-hint"); if (hint) hint.hidden = true;
      openTab("details");
      return true;
    };

    // ---- playback: one row; Play is the only filled button on the map
    var T = cfg.time, pb = fr.playback || {}, timer = null, speed = 1, loop = !!pb.loop;
    var play = $("gm-time-play");
    function setPlayIcon(onOff) {
      if (!play) return;
      play.setAttribute("aria-pressed", onOff ? "true" : "false");
      play.setAttribute("aria-label", onOff ? "Pause (Space)" : "Play through the periods (Space)");
      var u = play.querySelector("use"); if (u) u.setAttribute("href", onOff ? "#gmi-pause" : "#gmi-play");
    }
    function stop() { if (timer) { clearInterval(timer); timer = null; } setPlayIcon(false); }
    function start() {
      if (!T) return;
      if (x.timeIndex() >= T.labels.length - 1) x.setTime(0);
      setPlayIcon(true);
      timer = setInterval(function () {
        var i = x.timeIndex();
        if (i >= T.labels.length - 1) { if (loop) { x.setTime(0); return; } stop(); return; }
        x.setTime(i + 1);
        if (!loop && x.timeIndex() >= T.labels.length - 1) stop();
      }, Math.max(100, (T.intervalMs || 1200) / speed));
    }
    F.toggle = function () { if (!T || pb.mode === "slider") return; if (timer) stop(); else start(); };
    F.stop = stop;
    if (play) play.addEventListener("click", F.toggle);
    on("gm-time-back", "click", function () { stop(); x.setTime(x.timeIndex() - 1); });
    on("gm-time-fwd", "click", function () { stop(); x.setTime(x.timeIndex() + 1); });
    var spd = on("gm-time-speed", "change", function () { speed = +spd.value || 1; if (timer) { stop(); start(); } });
    var lp = on("gm-time-loop", "click", function () { loop = !loop; lp.setAttribute("aria-pressed", loop ? "true" : "false"); });
    if (dock) dock.addEventListener("keydown", function (e) {
      if (e.target && e.target.id === "gm-time-range") return;            // the slider's own keys
      if (e.key === "ArrowLeft") { e.preventDefault(); stop(); x.setTime(x.timeIndex() - 1); }
      if (e.key === "ArrowRight") { e.preventDefault(); stop(); x.setTime(x.timeIndex() + 1); }
      if (e.key === "Home") { e.preventDefault(); stop(); x.setTime(0); }
      if (e.key === "End") { e.preventDefault(); stop(); x.setTime(T.labels.length - 1); }
    });
    // chapters as markers on the slider, their titles as tooltips
    var marks = $("gm-time-marks");
    if (marks && T && cfg.story && T.labels.length > 1) {
      cfg.story.chapters.forEach(function (ch) {
        if (ch.time == null || ch.time < 0) return;
        var m = document.createElement("span"); m.className = "gm-mark"; m.title = ch.title; m.style.left = (100 * ch.time / (T.labels.length - 1)) + "%";
        marks.appendChild(m);
      });
    }
    // on a phone the dock rides in the sheet's peek row
    var dockHome = dock ? dock.parentNode : null, foot = $("gm-pfoot");
    var dockOnMap = !!dock && dock.classList.contains("gm-dock-map");
    function placeDock() {
      if (!dockOnMap) return;
      var inSheet = mqPhone.matches && foot && hasPanel;
      if (inSheet && dock.parentNode !== foot) foot.insertBefore(dock, foot.firstChild);
      else if (!inSheet && dockHome && dock.parentNode !== dockHome) dockHome.appendChild(dock);
      dock.classList.toggle("gm-dock-panel", !!inSheet); dock.classList.toggle("gm-dock-map", !inSheet);
      dock.style.bottom = ""; dock.style.width = "";
      measurePeek();
    }

    // ---- story: the active card expanded, the others one line; one Previous/Next and the dots in the footer; ← → and optionally scrolling
    var S = cfg.story, cards = [].slice.call(document.querySelectorAll(".gm-chapter")), dots = [].slice.call(document.querySelectorAll(".gm-dot"));
    var cap = $("gm-caption"), body = panel ? panel.querySelector(".gm-pbody") : null, scrollLock = false;
    F.storyChanged = function (i, how) {
      cards.forEach(function (c, k) {
        var a = k === i; c.classList.toggle("gm-active", a); c.setAttribute("aria-expanded", a ? "true" : "false");
        if (a) c.setAttribute("aria-current", "step"); else c.removeAttribute("aria-current");
      });
      dots.forEach(function (d, k) { if (k === i) d.setAttribute("aria-current", "step"); else d.removeAttribute("aria-current"); });
      var pv = $("gm-ch-prev"), nx = $("gm-ch-next");
      if (pv) pv.disabled = i <= 0; if (nx) nx.disabled = i >= cards.length - 1;
      if (cap && S) {
        var ch = S.chapters[i], txt = cards[i] ? cards[i].querySelector(".gm-ch-body") : null;
        cap.classList.add("gm-has");
        $("gm-cap-title").textContent = (i + 1) + " of " + S.chapters.length + " · " + ch.title;
        $("gm-cap-text").textContent = txt ? txt.textContent.trim() : "";
      }
      if (cards[i] && body && how !== "scroll") {
        scrollLock = true;
        var top = cards[i].offsetTop - body.offsetTop - 8;
        body.scrollTo({ top: Math.max(0, top), behavior: reduce || how === "jump" ? "auto" : "smooth" });
        setTimeout(function () { scrollLock = false; }, 700);
      }
      if (how === "key" && cards[i]) { var hh = cards[i].querySelector("h3"); if (hh) { hh.tabIndex = -1; hh.focus({ preventScroll: true }); } }
      measurePeek();
    };
    if (S) {
      cards.forEach(function (c, k) {
        c.addEventListener("click", function (e) { if (e.target.closest("a")) return; if (!c.classList.contains("gm-active")) x.storyGo(k, "click"); });
        c.addEventListener("keydown", function (e) { if ((e.key === "Enter" || e.key === " ") && e.target === c) { e.preventDefault(); x.storyGo(k, "key"); } });
      });
      dots.forEach(function (d, k) { d.addEventListener("click", function () { x.storyGo(k, "click"); }); });
      ["gm-ch-prev", "gm-cap-prev"].forEach(function (id) { on(id, "click", function () { x.storyGo(x.storyIndex() - 1, "click"); }); });
      ["gm-ch-next", "gm-cap-next"].forEach(function (id) { on(id, "click", function () { x.storyGo(x.storyIndex() + 1, "click"); }); });
      var nav = (S.navigation || "buttons");
      if ((nav === "scroll" || nav === "both") && body) {
        body.addEventListener("scroll", function () {
          if (scrollLock) return;
          var line = body.getBoundingClientRect().top + body.clientHeight * 0.35, best = -1;
          cards.forEach(function (c, k) { if (c.getBoundingClientRect().top <= line) best = k; });
          if (best >= 0 && best !== x.storyIndex()) x.storyGo(best, "scroll");
        }, { passive: true });
      }
    }

    // ---- keys (spec 12)
    document.addEventListener("keydown", function (e) {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(e)) return;
      var k = e.key, inDock = dock && dock.contains(e.target);
      if (k === "Escape") { closeMenu(); toggleHelp(false); if (pop && !pop.hidden) togglePop(false); return; }
      if (fr.shortcuts === false) return;
      if (k === "[") { e.preventDefault(); setOpen(!F.open); return; }
      if (/^[1-5]$/.test(k) && tabs[+k - 1]) { e.preventDefault(); if (!F.open) setOpen(true); openTab(tabs[+k - 1].getAttribute("data-tab"), true); return; }
      if ((k === "ArrowLeft" || k === "ArrowRight") && S && !inDock && !(e.target && e.target.closest && e.target.closest(".gm-tabs,#gm-stage .maplibregl-canvas,.leaflet-container"))) {
        e.preventDefault(); x.storyGo(x.storyIndex() + (k === "ArrowRight" ? 1 : -1), "key"); return;
      }
      if (k === " " && T && play && !(e.target && e.target.closest && e.target.closest("button,a"))) { e.preventDefault(); F.toggle(); return; }
      if ((k === "+" || k === "=") && E.zoomBy) { E.zoomBy(1); return; }
      if (k === "-" && E.zoomBy) { E.zoomBy(-1); return; }
      if (k === "0") { E.resetView(); return; }
      if (k === "f") { var fb = $("gm-fullscreen"); if (fb) fb.click(); return; }
      if (k === "?") { toggleHelp(); return; }
    });

    // ---- state after load
    if (E.onMove) E.onMove(function () { interacted(); renderCredits(); rotated(); });
    if (mqPhone.addEventListener) { mqPhone.addEventListener("change", function () { placeDock(); setOpen(F.open, "init"); renderCredits(); }); mqTab.addEventListener("change", function () { setOpen(remembered(), "init"); }); }
    // the brand chip's logo as a picture, so Save image burns the chip in at the same corner (a data: picture never taints the canvas)
    try {
      var bl = document.querySelector("#gm-brand .gm-brand-logo, .gm-brand .gm-brand-logo");
      if (bl && window.XMLSerializer) {
        var im = new Image(), xml = new XMLSerializer().serializeToString(bl);
        im.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
        window.gmapBrandImage = im;
      }
    } catch (e) { /* the chip is drawn without its logo */ }
    F.start = function () {
      setSheet("peek");
      placeDock();
      var want = (x.hashState || {}).tab;
      if (tabs.length) openTab(want && $("gm-tab-" + want) ? want : (fr.panel || {}).default_tab || tabs[0].getAttribute("data-tab"), false, true);
      setOpen(remembered(), "init");
      legendFloat(!!(fr.legend || {}).float);
      measurePeek();
      renderCredits();
      layoutBottom();
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { measurePeek(); layoutBottom(); });
      requestAnimationFrame(function () { measurePeek(); settle(0); });
      // the first view fits the data into the map as the frame leaves it (the engine fitted it to the window before the frame settled)
      if (!(x.hashState || {}).v && !cfg.story) { if (E.resize) E.resize(); E.resetView(); }
      if (pb.autoplay && T && play) setTimeout(start, 600);
    };
    F.state = function () { return { p: hasPanel ? (F.open ? "1" : "0") : null, tab: F.tab }; };
    return F;
  }
})();
