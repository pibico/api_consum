/**
 * help.js — "Ayuda y guía": in-app tutorial in a slide panel (#39).
 *
 * One accordion section per app page, content entirely from i18n
 * (help.sec.<id>.title / .p / .s1..sN). The section matching the CURRENT page
 * auto-expands on open. Screenshots: the Mi PLC section reuses the approved
 * CM4 captures already public on the landing; every other section has an
 * <img> slot (app-help-<id>[-es].png under /static/images/help/) that shows
 * up automatically when a capture is dropped there — hidden on 404 meanwhile.
 */
(function () {
  'use strict';

  var ROOT = window.__ROOT__ || '';

  // Section spec: id (matches i18n keys + URL path), nav icon, step count,
  // fixed images (CM4 captures, Mi PLC only) or a drop-in slot name.
  var ICONS = {
    primeros: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    panel: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    consumption: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    invoices: '<path d="M4 2v20l2-1.5L8 22l2-1.5L12 22l2-1.5L16 22l2-1.5L20 22V2l-2 1.5L16 2l-2 1.5L12 2l-2 1.5L8 2 6 3.5 4 2z"/><line x1="8" y1="8" x2="16" y2="8"/><line x1="8" y1="12" x2="16" y2="12"/>',
    savings: '<path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    ai: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z"/>',
    plc: '<rect x="4" y="4" width="16" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/><circle cx="8" cy="10" r="1"/><circle cx="12" cy="10" r="1"/><circle cx="16" cy="10" r="1"/>',
  };
  var SECTIONS = [
    { id: 'primeros', steps: 4, slot: 'app-help-panel' },
    { id: 'panel', steps: 5, path: /\/app\/?$/, slot: 'app-help-panel' },
    { id: 'consumption', steps: 5, path: /\/app\/consumption/, slot: 'app-help-consumption' },
    { id: 'invoices', steps: 6, path: /\/app\/invoices/, slot: 'app-help-invoices' },
    { id: 'savings', steps: 3, path: /\/app\/savings/, slot: 'app-help-savings' },
    { id: 'ai', steps: 3, path: /\/app\/ai/, slot: 'app-help-ai' },
    { id: 'plc', steps: 4, path: /\/app\/plc/,
      imgs: ['help-home', 'help-dashboard', 'help-wiring'] },
  ];

  function t(key, fb) { return (window.__t ? __t(key, fb) : fb) || fb || ''; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function langSuffix() {
    var l = (window.getLang ? getLang() : (document.documentElement.lang || 'es'));
    return String(l).slice(0, 2) === 'en' ? '' : '-es';
  }

  function imgTag(base) {
    // Hidden until it actually loads — a missing capture never shows a broken icon.
    var src = ROOT + '/static/images/help/' + base + langSuffix() + '.png';
    return '<img class="help-img" src="' + src + '" alt="" loading="lazy" ' +
      'style="display:none;" onload="this.style.display=\'block\'">';
  }

  function sectionHtml(sec, open) {
    var steps = '';
    for (var i = 1; i <= sec.steps; i++) {
      var s = t('help.sec.' + sec.id + '.s' + i, '');
      if (s) steps += '<li>' + s + '</li>';   // i18n strings may carry <b>
    }
    var imgs = (sec.imgs || (sec.slot ? [sec.slot] : []))
      .map(imgTag).join('');
    return '<section class="help-sec' + (open ? ' open' : '') + '" data-sec="' + sec.id + '">' +
      '<button type="button" class="help-sec-h" onclick="HelpPanel.toggle(\'' + sec.id + '\')">' +
        '<span class="help-sec-ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (ICONS[sec.id] || ICONS.primeros) + '</svg></span>' +
        '<span>' + esc(t('help.sec.' + sec.id + '.title', sec.id)) + '</span>' +
        '<span class="help-sec-chev">›</span>' +
      '</button>' +
      '<div class="help-sec-b">' +
        '<p>' + t('help.sec.' + sec.id + '.p', '') + '</p>' +
        (steps ? '<ol class="help-steps">' + steps + '</ol>' : '') +
        imgs +
      '</div>' +
    '</section>';
  }

  function render() {
    var body = document.getElementById('help-body');
    if (!body) return;
    var here = location.pathname;
    body.innerHTML = SECTIONS.map(function (sec) {
      return sectionHtml(sec, !!(sec.path && sec.path.test(here)));
    }).join('');
  }

  window.HelpPanel = {
    open: function () {
      render();                                 // fresh per open — follows lang
      if (window.AppUI && AppUI.openPanel) AppUI.openPanel('helpPanel');
    },
    close: function () {
      if (window.AppUI && AppUI.closePanel) AppUI.closePanel('helpPanel');
    },
    toggle: function (id) {
      var el = document.querySelector('.help-sec[data-sec="' + id + '"]');
      if (el) el.classList.toggle('open');
    },
  };
})();
