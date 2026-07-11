/**
 * Shared `.ev-pager` component — the CM4 local-webui pattern (per explicit
 * request, replacing an earlier vh-pagination draft):
 *   cm4-consumia/local-webui/app/static/js/app.js:1611 (evPagerHtml)
 *   cm4-consumia/local-webui/app/static/css/consumia.css:608/883 (.an-ic/.ev-pager)
 *
 * Client-side paginator over an already-fetched in-memory array — matches
 * this console's existing "fetch everything, slice locally" pattern. Renders
 * the SAME pager markup into every container passed in (conventionally one
 * above and one below the table/list, exactly like the CM4 reference's
 * `pager + content + pager`) and keeps them in sync. ONE implementation,
 * reused by every paginated page in this console (devices, tunnels,
 * unknowns, audit, clients, sensors, alerts x4, firmware x3, mqtt explorer)
 * so markup/behavior never drifts page to page.
 *
 * Usage:
 *   var pager = window.Pager.create({
 *     containerIds: ['devices-pager-top', 'devices-pager-bottom'],
 *     pageSize: 25,                 // optional, default 25
 *     totalLabel: t('devices.devicesWord'),  // optional, default "items"/"elementos"
 *     onRender: function (pageItems, page, totalPages, total) { ... }
 *   });
 *   pager.setItems(allRows);   // re-slices, resets to page 1, renders
 *   pager.refresh();           // re-slices current page (e.g. after in-place row edit)
 *
 * Loaded once in base.html (after api_edge.js, before the per-page script),
 * so every page/*.js can call `window.Pager.create(...)` directly.
 */
(function () {
  function t(key, fallback) {
    var v = (window.i18n && window.i18n.t) ? window.i18n.t(key) : undefined;
    return (v && v !== key) ? v : fallback;
  }

  function create(opts) {
    opts = opts || {};
    var ids = opts.containerIds || (opts.containerId ? [opts.containerId] : []);
    var els = ids.map(function (id) { return document.getElementById(id); }).filter(Boolean);
    if (!els.length) {
      console.warn('Pager.create: no pager containers found for ' + JSON.stringify(ids));
    }
    var pageSize = opts.pageSize || 25;
    var onRender = opts.onRender || function () {};

    var state = { items: [], page: 0 }; // 0-based, like the CM4 reference (_evPage)

    function totalPages() { return Math.max(1, Math.ceil(state.items.length / pageSize)); }

    function pagerHtml() {
      var total = state.items.length;
      if (total <= pageSize) return '';
      var pages = totalPages();
      var cur = state.page + 1;
      var label = opts.totalLabel || t('common.items', 'elementos');
      return '<div class="ev-pager">' +
        '<button type="button" class="an-ic" data-evpage="prev"' + (cur <= 1 ? ' disabled' : '') + ' data-i18n-title="common.prev" title="' + t('common.prev', 'Prev') + '">&lsaquo;</button>' +
        '<span>' + cur + ' / ' + pages + ' · ' + total + ' ' + label + '</span>' +
        '<button type="button" class="an-ic" data-evpage="next"' + (cur >= pages ? ' disabled' : '') + ' data-i18n-title="common.next" title="' + t('common.next', 'Next') + '">&rsaquo;</button>' +
        '</div>';
    }

    function wire() {
      els.forEach(function (el) {
        el.querySelectorAll('[data-evpage]').forEach(function (b) {
          b.addEventListener('click', function () {
            state.page += b.dataset.evpage === 'next' ? 1 : -1;
            if (state.page < 0) state.page = 0;
            render();
          });
        });
      });
    }

    function render() {
      var pages = totalPages();
      if (state.page >= pages) state.page = pages - 1;
      if (state.page < 0) state.page = 0;
      var start = state.page * pageSize;
      var slice = state.items.slice(start, start + pageSize);
      var html = pagerHtml();
      els.forEach(function (el) { el.innerHTML = html; el.hidden = (html === ''); });
      wire();
      onRender(slice, state.page + 1, pages, state.items.length);
    }

    return {
      setItems: function (items) { state.items = items || []; state.page = 0; render(); },
      /* Like setItems, but keeps the current page — for pages that poll/refresh
       * their source array in place (e.g. the mqtt explorer ring buffer) and
       * would otherwise snap the user back to page 1 on every tick. */
      updateItems: function (items) { state.items = items || []; render(); },
      refresh: render,
      reset: function () { state.page = 0; render(); },
      get page() { return state.page + 1; },
    };
  }

  window.Pager = { create: create };

  /**
   * Shared "wait for window.App" bootstrap. api_edge.js sets window.App at
   * the bottom of its top-level script body, and every page script is loaded
   * `defer` right after it — normally safe — but several pages called
   * `__pageInit()` unguarded, which raced (window.App undefined -> destructure
   * crash -> stuck loading spinner) whenever load order/timing wasn't exactly
   * as expected. All page bootstraps should use this instead of reinventing
   * the poll loop per file: `document.addEventListener('DOMContentLoaded',
   * function () { window.onAppReady(__pageInit); });`
   */
  window.onAppReady = function (fn) {
    (function wait() {
      if (window.App) return fn();
      setTimeout(wait, 50);
    })();
  };
})();
