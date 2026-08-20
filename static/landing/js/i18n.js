/* i18n mínimo y autónomo — funciona también abriendo el HTML con file://
   Diccionarios: window.I18N_ES / window.I18N_EN (cargados antes que este script). */
(function () {
  "use strict";

  var STORE = "consum_landing_lang";
  var SUPPORTED = ["es", "en"];
  var DICTS = { es: window.I18N_ES || {}, en: window.I18N_EN || {} };

  function pickInitial() {
    var saved = null;
    try { saved = localStorage.getItem(STORE); } catch (e) { /* file:// sin storage */ }
    if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;
    var nav = (navigator.language || "es").slice(0, 2).toLowerCase();
    return SUPPORTED.indexOf(nav) !== -1 ? nav : "es";
  }

  var current = pickInitial();

  function t(key) {
    var d = DICTS[current] || {};
    if (Object.prototype.hasOwnProperty.call(d, key)) return d[key];
    var fb = DICTS.es || {};
    return Object.prototype.hasOwnProperty.call(fb, key) ? fb[key] : key;
  }

  function apply(root) {
    var scope = root || document;

    scope.querySelectorAll("[data-i18n]").forEach(function (el) {
      var v = t(el.getAttribute("data-i18n"));
      if (v != null) el.textContent = v;
    });

    // Sólo para cadenas de marca controladas por nosotros (contienen <br>/<em>)
    scope.querySelectorAll("[data-i18n-html]").forEach(function (el) {
      var v = t(el.getAttribute("data-i18n-html"));
      if (v != null) el.innerHTML = v;
    });

    scope.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      var v = t(el.getAttribute("data-i18n-title"));
      if (v != null) { el.setAttribute("title", v); el.setAttribute("aria-label", v); }
    });

    scope.querySelectorAll("[data-i18n-label]").forEach(function (el) {
      var v = t(el.getAttribute("data-i18n-label"));
      if (v != null) el.setAttribute("aria-label", v);
    });

    // La pestaña, el marcador y la tarjeta al compartir también son copy
    var mt = t("ld.metaTitle");
    if (mt && mt !== "ld.metaTitle") document.title = mt;
    var md = document.querySelector('meta[name="description"]');
    var mdv = t("ld.metaDesc");
    if (md && mdv && mdv !== "ld.metaDesc") md.setAttribute("content", mdv);

    document.documentElement.setAttribute("lang", current);
  }

  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) === -1) return;
    current = lang;
    try { localStorage.setItem(STORE, lang); } catch (e) { /* noop */ }
    apply();
    document.dispatchEvent(new CustomEvent("i18n:changed", { detail: { lang: lang } }));
  }

  window.i18n = { t: t, apply: apply, setLang: setLang, getLang: function () { return current; } };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { apply(); });
  } else {
    apply();
  }
})();
