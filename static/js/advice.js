/**
 * advice.js — "Previsión y consejos por email" settings panel (S8, #40).
 *
 * Loads the household's opt-in state when the panel opens, saves on toggle.
 * Recipient is always the account's own email (server-side, ctx.user.email)
 * — no email field in the UI, matches the mailer's consent model (S1/S8).
 */
(function () {
  'use strict';

  function t(key, fb) { return (window.__t ? __t(key, fb) : fb) || fb || ''; }

  function setStatus(msg, isError) {
    var el = document.getElementById('advice-status');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = isError ? '#c0392b' : '#2c5171';
  }

  function loadPrefs() {
    var box = document.getElementById('advice-opt-in');
    if (!box || typeof apiFetch !== 'function') return;
    apiFetch('/advice/prefs')
      .then(function (data) {
        if (data) box.checked = !!data.opt_in;
      })
      .catch(function () { /* best-effort — leave the box in its last state */ });
  }

  function savePrefs() {
    var box = document.getElementById('advice-opt-in');
    if (!box) return;
    var lang = (window.i18n && window.i18n.getLang && window.i18n.getLang()) || 'es';
    apiFetch('/advice/prefs', {
      method: 'POST',
      body: JSON.stringify({ opt_in: box.checked, lang: lang }),
    })
      .then(function () { setStatus(t('advice.saved', 'Preferencia guardada.'), false); })
      .catch(function (e) {
        setStatus(t('advice.saveError', 'No se pudo guardar la preferencia.'), true);
        box.checked = !box.checked; // revert the toggle on failure
      });
  }

  window.AdvicePanel = window.AdvicePanel || {};
  window.AdvicePanel.open = function () {
    setStatus('');
    loadPrefs();
    window.AppUI.openPanel('advicePanel');
  };
  window.AdvicePanel.close = function () {
    window.AppUI.closePanel('advicePanel');
  };

  document.addEventListener('DOMContentLoaded', function () {
    var box = document.getElementById('advice-opt-in');
    if (box) box.addEventListener('change', savePrefs);
  });
})();
