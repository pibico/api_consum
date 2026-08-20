/* CONSUM-IA — landing v2
   Interacción principal: arrastrar por las 24 h y leer precio, consumo y coste
   acumulado. Es el mismo gesto de la app real, en miniatura.
   Vanilla JS, sin dependencias, sin peticiones de red. */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ datos
     Día de ejemplo con la forma de banda del 2.0TD en día laborable:
       P3 valle 00–08 · P2 llano 08–10, 14–18, 22–24 · P1 punta 10–14, 18–22
     Precios en €/kWh y consumo doméstico en kWh por hora. Es un ejemplo
     declarado como tal en la propia página — no se presenta como dato real. */
  var DAY = [
    { h: 0,  band: "P3", price: 0.0912, kwh: 0.24 },
    { h: 1,  band: "P3", price: 0.0871, kwh: 0.21 },
    { h: 2,  band: "P3", price: 0.0848, kwh: 0.19 },
    { h: 3,  band: "P3", price: 0.0839, kwh: 0.18 },
    { h: 4,  band: "P3", price: 0.0846, kwh: 0.18 },
    { h: 5,  band: "P3", price: 0.0884, kwh: 0.20 },
    { h: 6,  band: "P3", price: 0.0963, kwh: 0.27 },
    { h: 7,  band: "P3", price: 0.1074, kwh: 0.41 },
    { h: 8,  band: "P2", price: 0.1352, kwh: 0.68 },
    { h: 9,  band: "P2", price: 0.1418, kwh: 0.55 },
    { h: 10, band: "P1", price: 0.1673, kwh: 0.46 },
    { h: 11, band: "P1", price: 0.1621, kwh: 0.44 },
    { h: 12, band: "P1", price: 0.1584, kwh: 0.72 },
    { h: 13, band: "P1", price: 0.1608, kwh: 1.24 },
    { h: 14, band: "P2", price: 0.1396, kwh: 0.93 },
    { h: 15, band: "P2", price: 0.1281, kwh: 0.42 },
    { h: 16, band: "P2", price: 0.1247, kwh: 0.38 },
    { h: 17, band: "P2", price: 0.1339, kwh: 0.44 },
    { h: 18, band: "P1", price: 0.1712, kwh: 0.61 },
    { h: 19, band: "P1", price: 0.1898, kwh: 0.87 },
    { h: 20, band: "P1", price: 0.2043, kwh: 1.16 },
    { h: 21, band: "P1", price: 0.1937, kwh: 1.38 },
    { h: 22, band: "P2", price: 0.1524, kwh: 0.94 },
    { h: 23, band: "P2", price: 0.1298, kwh: 0.51 }
  ];

  var BAND_COLOR = { P1: "#D4505E", P2: "#F2A872", P3: "#5CCEA0" };
  var BAND_KEY   = { P1: "ld.bandP1", P2: "ld.bandP2", P3: "ld.bandP3" };

  var SVG_NS = "http://www.w3.org/2000/svg";
  var VB_H = 240, PAD_L = 8, PAD_R = 8, PAD_T = 16, PAD_B = 26;
  // El viewBox sigue al ancho real del contenedor. Con un viewBox fijo de 720,
  // a 360 px todo se dividía por 2,5 y el eje quedaba en ~6 px.
  var VB_W = 720;
  function syncViewBox() {
    var w = chart && chart.clientWidth ? Math.round(chart.clientWidth) : 720;
    VB_W = Math.max(300, w);
    svg.setAttribute("viewBox", "0 0 " + VB_W + " " + VB_H);
  }

  var svg     = document.getElementById("ld-svg");
  var chart   = document.getElementById("ld-chart");

  var rdHour  = document.getElementById("rd-hour");
  var rdPrice = document.getElementById("rd-price");
  var rdKwh   = document.getElementById("rd-kwh");
  var rdTotal = document.getElementById("rd-total");
  var rdBand  = document.getElementById("rd-band");
  var rdBandT = document.getElementById("rd-band-text");

  var bars = [];
  var cursor = null;
  var active = 8;

  /* ------------------------------------------------------------- formateo */
  function nf(value, decimals) {
    var lang = (window.i18n && window.i18n.getLang && window.i18n.getLang()) || "es";
    try {
      return value.toLocaleString(lang === "en" ? "en-GB" : "es-ES", {
        minimumFractionDigits: decimals, maximumFractionDigits: decimals
      });
    } catch (e) {
      return value.toFixed(decimals);
    }
  }

  /* -------------------------------------------------------------- pintado */
  function build() {
    if (!svg || !chart) return;
    syncViewBox();
    // Sólo ahora es un deslizador de verdad: sin JS no se anuncia como tal
    chart.setAttribute("role", "slider");
    chart.setAttribute("tabindex", "0");
    chart.setAttribute("aria-label", chart.getAttribute("data-slider-label") || "");
    chart.setAttribute("aria-valuemin", "0");
    chart.setAttribute("aria-valuemax", "23");
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    bars = [];

    var innerW = VB_W - PAD_L - PAD_R;
    var innerH = VB_H - PAD_T - PAD_B;
    var slot   = innerW / 24;
    var barW   = slot * 0.66;
    var maxKwh = Math.max.apply(null, DAY.map(function (d) { return d.kwh; }));
    var maxPr  = Math.max.apply(null, DAY.map(function (d) { return d.price; }));
    var minPr  = Math.min.apply(null, DAY.map(function (d) { return d.price; }));

    // Barras de consumo, coloreadas por banda
    DAY.forEach(function (d, i) {
      var hgt = Math.max(2, (d.kwh / maxKwh) * innerH);
      var r = document.createElementNS(SVG_NS, "rect");
      r.setAttribute("x", (PAD_L + i * slot + (slot - barW) / 2).toFixed(2));
      r.setAttribute("y", (PAD_T + innerH - hgt).toFixed(2));
      r.setAttribute("width", barW.toFixed(2));
      r.setAttribute("height", hgt.toFixed(2));
      r.setAttribute("rx", "2.5");
      r.setAttribute("fill", BAND_COLOR[d.band]);
      r.setAttribute("class", "ld-bar");
      svg.appendChild(r);
      bars.push(r);
    });

    // Curva de precio superpuesta
    var pts = DAY.map(function (d, i) {
      var x = PAD_L + i * slot + slot / 2;
      var norm = (d.price - minPr) / (maxPr - minPr || 1);
      var y = PAD_T + innerH - norm * innerH * 0.72 - innerH * 0.14;
      return x.toFixed(2) + "," + y.toFixed(2);
    }).join(" ");

    var line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", pts);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "#0f1c2b");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("opacity", "0.72");
    svg.appendChild(line);

    // Etiquetas de hora cada 4 h
    [0, 4, 8, 12, 16, 20].forEach(function (h) {
      var tx = document.createElementNS(SVG_NS, "text");
      tx.setAttribute("x", (PAD_L + h * slot + slot / 2).toFixed(2));
      tx.setAttribute("y", (VB_H - 8).toFixed(2));
      tx.setAttribute("text-anchor", "middle");
      tx.setAttribute("class", "ld-axis-label");
      tx.textContent = (h < 10 ? "0" + h : h) + ":00";
      svg.appendChild(tx);
    });

    cursor = document.createElementNS(SVG_NS, "line");
    cursor.setAttribute("y1", PAD_T - 6);
    cursor.setAttribute("y2", PAD_T + innerH);
    cursor.setAttribute("class", "ld-cursor-line");
    svg.appendChild(cursor);

    select(active, true);
  }

  /* -------------------------------------------------------------- lectura */
  function select(index, force) {
    if (!svg || !chart || !cursor) return;
    var i = Math.max(0, Math.min(23, index | 0));
    if (i === active && !force) return;
    active = i;

    var d = DAY[i];
    var running = 0;
    for (var k = 0; k <= i; k++) running += DAY[k].kwh * DAY[k].price;

    rdHour.textContent  = (d.h < 10 ? "0" + d.h : d.h) + ":00";
    rdPrice.innerHTML   = nf(d.price, 4) + "<small>€/kWh</small>";
    rdKwh.innerHTML     = nf(d.kwh, 2) + "<small>kWh</small>";
    rdTotal.innerHTML   = nf(running, 2) + "<small>€</small>";

    rdBand.setAttribute("data-band", d.band);
    rdBandT.textContent = (window.i18n ? window.i18n.t(BAND_KEY[d.band]) : d.band);

    bars.forEach(function (b, bi) { b.classList.toggle("is-active", bi === i); });

    var slot = (VB_W - PAD_L - PAD_R) / 24;
    var x = PAD_L + i * slot + slot / 2;
    cursor.setAttribute("x1", x.toFixed(2));
    cursor.setAttribute("x2", x.toFixed(2));

    chart.setAttribute("aria-valuenow", String(i));
    // La hora sola no dice nada: se anuncian las cuatro cifras y la banda
    chart.setAttribute("aria-valuetext",
      rdHour.textContent + " — " + nf(d.price, 4) + " €/kWh, " +
      nf(d.kwh, 2) + " kWh, " + nf(running, 2) + " € " +
      (window.i18n ? window.i18n.t("ld.dayRunning") : "acumulado") + ", " +
      rdBandT.textContent);
  }

  function indexFromClientX(clientX) {
    var box = chart.getBoundingClientRect();
    if (!box.width) return active;
    var ratio = (clientX - box.left) / box.width;
    return Math.round(ratio * 24 - 0.5);
  }

  /* --------------------------------------------------------- interacción */
  var dragging = false;

  if (chart) chart.addEventListener("pointerdown", function (ev) {
    dragging = true;
    if (chart.setPointerCapture) { try { chart.setPointerCapture(ev.pointerId); } catch (e) {} }
    select(indexFromClientX(ev.clientX));
  });
  if (chart) chart.addEventListener("pointermove", function (ev) {
    if (!dragging && ev.pointerType === "touch") return;
    if (dragging || ev.buttons === 0) select(indexFromClientX(ev.clientX));
  });
  window.addEventListener("pointerup", function () { dragging = false; });

  if (chart) chart.addEventListener("keydown", function (ev) {
    var handled = true;
    switch (ev.key) {
      case "ArrowLeft":  case "ArrowDown": select(active - 1); break;
      case "ArrowRight": case "ArrowUp":   select(active + 1); break;
      case "Home": select(0); break;
      case "End":  select(23); break;
      case "PageUp":   select(active + 4); break;
      case "PageDown": select(active - 4); break;
      default: handled = false;
    }
    if (handled) ev.preventDefault();
  });

  /* --------------------------------------------------- aparición al scroll */
  function setupReveal() {
    var items = document.querySelectorAll(".ld-reveal");
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("is-in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach(function (el, i) {
      el.style.transitionDelay = Math.min(i % 6, 5) * 55 + "ms";
      io.observe(el);
    });
  }

  /* ------------------------------------------------------------- idioma */
  function setupLang() {
    var wrap = document.getElementById("ld-lang");
    var btn  = document.getElementById("ld-lang-btn");
    var menu = document.getElementById("ld-lang-menu");
    if (!wrap || !btn || !menu) return;

    function close() { wrap.classList.remove("is-open"); btn.setAttribute("aria-expanded", "false"); }
    function open()  { wrap.classList.add("is-open");    btn.setAttribute("aria-expanded", "true"); }

    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      wrap.classList.contains("is-open") ? close() : open();
    });
    menu.addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-lang]");
      if (!b) return;
      if (window.i18n) window.i18n.setLang(b.getAttribute("data-lang"));
      close();
    });
    document.addEventListener("click", close);
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") close(); });
  }

  /* ------------------------------------------- vídeo y movimiento reducido */
  function setupVideo() {
    var v = document.getElementById("ld-hero-video");
    if (!v) return;
    var mq = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
    function sync() {
      if (mq && mq.matches) { v.pause(); v.removeAttribute("autoplay"); }
      else { var p = v.play(); if (p && p.catch) p.catch(function () { /* autoplay bloqueado: queda el poster */ }); }
    }
    sync();
    if (mq && mq.addEventListener) mq.addEventListener("change", sync);

    // No malgastar batería ni CPU con el hero fuera de pantalla
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (mq && mq.matches) return;
          if (e.isIntersecting) { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
          else v.pause();
        });
      }, { threshold: 0.01 }).observe(v);
    }
  }

  /* ----------------------------------------------------------- spot 15 s */
  function setupSpot() {
    var v = document.getElementById("ld-spot-video");
    var b = document.getElementById("ld-spot-play");
    if (!v || !b) return;
    b.addEventListener("click", function () {
      v.setAttribute("controls", "");   // los controles nativos, sólo al arrancar
      var p = v.play();
      if (p && p.catch) p.catch(function () {});
    });
    v.addEventListener("play",  function () { b.classList.add("is-hidden"); });
    v.addEventListener("pause", function () { if (v.currentTime === 0 || v.ended) b.classList.remove("is-hidden"); });
    v.addEventListener("ended", function () { b.classList.remove("is-hidden"); });
  }

  /* ---------------------------------------------------------------- init */
  function init() {
    // El reveal va PRIMERO y aislado: si build() fallara, el resto de la
    // página no puede quedarse invisible para siempre.
    try { setupReveal(); } catch (e) { document.querySelectorAll(".ld-reveal").forEach(function (el) { el.classList.add("is-in"); }); }
    try { setupSpot(); }  catch (e) {}
    try { build(); }      catch (e) {}
    try { setupLang(); }  catch (e) {}
    try { setupVideo(); } catch (e) {}
    var rt = null;
    window.addEventListener("resize", function () {
      clearTimeout(rt); rt = setTimeout(function () { try { build(); } catch (e) {} }, 160);
    });
    document.addEventListener("i18n:changed", function () { try { select(active, true); } catch (e) {} });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
