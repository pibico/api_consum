/* ============================================
   pibiCo — canonical i18n controller (window.i18n)
   Source of truth: fastapi-ui skill. Box-independent, reusable across apps.

   Split-locale loader. Dictionaries live in separate files, loaded on demand:
     - /static/js/i18n.es.js  -> window.I18N_ES
     - /static/js/i18n.en.js  -> window.I18N_EN
     - /static/js/i18n.fr.js  -> window.I18N_FR   (optional)

   base.html eagerly loads ONLY the active locale, then this engine:
     <script src="{{ root_path }}/static/js/i18n.js" defer></script>
     <script src="{{ root_path }}/static/js/i18n.{{ lang }}.js" defer></script>
   The inactive locales are fetched lazily on first setLang(). Applies in
   place — no page reload. Fires "i18n:changed" so pages can re-render.

   Exposes: window.i18n = { t, tt, getLang, setLang, applyI18n }.

   PER-APP KNOBS (the only two things you may change):
     - STORE : sessionStorage/localStorage key (namespace it per app to avoid
               cross-app bleed when several pibiCo apps share an origin).
     - the "?v=N" cache-buster below MUST be bumped whenever you change a dict.
   ============================================ */
(function () {
  const ROOT = (window.ROOT_PATH || '');
  const STORE = 'consum_lang';                 // ← per-app knob (e.g. 'auth_lang', 'collab_lang')
  const SUPPORTED = ['es', 'en', 'fr'];
  const DICT = {};
  if (window.I18N_ES) DICT.es = window.I18N_ES;
  if (window.I18N_EN) DICT.en = window.I18N_EN;
  if (window.I18N_FR) DICT.fr = window.I18N_FR;

  function _queryLang() {
    try {
      const p = new URLSearchParams(window.location.search).get('lang');
      return SUPPORTED.indexOf(p) !== -1 ? p : null;
    } catch (e) { return null; }
  }

  function getLang() {
    return _queryLang()
      || sessionStorage.getItem(STORE)
      || localStorage.getItem(STORE)            /* legacy persisted pref */
      || document.documentElement.lang
      || 'es';
  }

  function _ensureDict(lang) {
    if (DICT[lang]) return Promise.resolve();
    return new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = ROOT + '/static/js/i18n.' + lang + '.js?v=1';   // ← bump v=N on dict change
      s.onload = function () {
        const g = lang === 'en' ? window.I18N_EN : lang === 'fr' ? window.I18N_FR : window.I18N_ES;
        DICT[lang] = g || {};
        resolve();
      };
      s.onerror = function () { resolve(); };   /* keep current lang on failure */
      document.head.appendChild(s);
    });
  }

  function t(key) {
    const l = getLang();
    return (DICT[l] && DICT[l][key] !== undefined ? DICT[l][key] : undefined)
      ?? (DICT.es && DICT.es[key] !== undefined ? DICT.es[key] : undefined)
      ?? (DICT.en && DICT.en[key] !== undefined ? DICT.en[key] : undefined)
      ?? key;
  }

  /* Two-arg variant: returns the default when the key is missing (t(key) === key).
     Centralised so callers never redeclare a drifting local wrapper. */
  function tt(key, def) {
    const v = t(key);
    return (v && v !== key) ? v : def;
  }

  function applyI18n(root) {
    root = root || document;
    root.querySelectorAll('[data-i18n]').forEach(function (el) {
      const k = el.getAttribute('data-i18n'); const v = t(k);
      if (v !== k) el.textContent = v;
    });
    root.querySelectorAll('[data-i18n-html]').forEach(function (el) {
      const k = el.getAttribute('data-i18n-html'); const v = t(k);
      if (v !== k) el.innerHTML = v;
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      const k = el.getAttribute('data-i18n-placeholder'); const v = t(k);
      if (v !== k) el.placeholder = v;
    });
    root.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      const k = el.getAttribute('data-i18n-title'); const v = t(k);
      if (v !== k) el.title = v;
    });
    root.querySelectorAll('*').forEach(function (el) {
      for (const a of el.attributes) {
        if (a.name.indexOf('data-i18n-attr-') === 0) {
          el.setAttribute(a.name.slice('data-i18n-attr-'.length), t(a.value));
        }
      }
    });
    document.documentElement.setAttribute('lang', getLang());
  }

  /* Reflect the active locale in the canonical lang dropdown (partials/lang_dropdown.html). */
  function _updateLangDropdown(lang) {
    document.querySelectorAll('.pibico-lang-opt').forEach(function (opt) {
      opt.classList.toggle('selected', opt.dataset.lang === lang);
    });
    const activeOpt = document.querySelector('.pibico-lang-opt[data-lang="' + lang + '"]');
    if (activeOpt) {
      const flagSrc = activeOpt.querySelector('.pibico-flag');
      const currentFlag = document.getElementById('langFlagCurrent');
      const currentLabel = document.getElementById('langLabelCurrent');
      if (flagSrc && currentFlag) currentFlag.innerHTML = flagSrc.innerHTML;
      if (currentLabel) currentLabel.textContent = lang.toUpperCase();
    }
    const menu = document.getElementById('langMenu');
    if (menu) menu.classList.remove('open');
  }

  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) === -1) lang = 'es';
    return _ensureDict(lang).then(function () {
      try { sessionStorage.setItem(STORE, lang); } catch (e) {}
      try { localStorage.setItem(STORE, lang); } catch (e) {}
      applyI18n();
      _updateLangDropdown(lang);
      document.dispatchEvent(new CustomEvent('i18n:changed', { detail: { lang: lang } }));
    });
  }

  window.i18n = { t: t, tt: tt, getLang: getLang, setLang: setLang, applyI18n: applyI18n };
  window.i18n.apply = applyI18n;                  /* back-compat alias */
  window.i18n._updateLangDropdown = _updateLangDropdown;

  document.addEventListener('DOMContentLoaded', function () {
    const l = getLang();
    const done = function () { applyI18n(); _updateLangDropdown(l); };
    if (!DICT[l]) { _ensureDict(l).then(done); } else { done(); }
    document.addEventListener('click', function (e) {
      const dropdown = document.getElementById('langDropdown');
      if (dropdown && !dropdown.contains(e.target)) {
        const menu = document.getElementById('langMenu');
        if (menu) menu.classList.remove('open');
      }
    });
  });
})();
