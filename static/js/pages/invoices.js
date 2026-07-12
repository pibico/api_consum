/**
 * invoices.js — Facturas: closed-period list, detail panel (frozen breakdown),
 * close-period form and PDF download.
 * Endpoints: /invoices (list), /invoices/{id}, /invoices/close (editor+),
 * /invoices/{id}/void (admin+), /invoices/{id}/pdf (PRO tier).
 * Writes need role editor+ (server-enforced; buttons hidden below that).
 */
(function () {
  'use strict';

  var ctx = null;          // /consumption/context payload
  var customer = '';       // '' = caller's scope (single home resolves itself)
  var list = [];           // /invoices values

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 2 : dec }); }
  function eur(n) { return (n == null) ? '—' : fmt(n, 2) + ' €'; }

  /** apiFetch clone that only logs out on 401 — App.apiFetch treats 403 as a
   * dead session, but 403 here is a ROLE/TIER answer to show, not a boot. */
  function cfetch(endpoint, options) {
    options = options || {};
    var headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers);
    var st = App.state || {};
    if (st.jwt && st.jwt.length >= 20) headers['Authorization'] = 'Bearer ' + st.jwt;
    else if (st.apiKey && st.apiKey.length >= 20) headers['X-API-Key'] = st.apiKey;
    return fetch((window.__ROOT__ || '') + '/api/v1' + endpoint,
      Object.assign({}, options, { headers: headers })).then(function (r) {
      if (r.status === 401) { App.logout(); return new Promise(function () {}); }
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) {
          var d = body.detail;
          var msg = (d && (d.message || d)) || ('HTTP ' + r.status);
          var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
          err.status = r.status; err.code = d && d.code;
          throw err;
        }
        return body;
      });
    });
  }

  function custQS(sep) { return customer ? ((sep || '?') + 'customer=' + encodeURIComponent(customer)) : ''; }
  function canWrite() {
    if (!ctx) return false;
    if (ctx.is_superadmin) return true;
    return ['editor', 'admin', 'owner'].indexOf(ctx.role || '') >= 0;
  }
  function canVoid() {
    if (!ctx) return false;
    return ctx.is_superadmin || ['admin', 'owner'].indexOf(ctx.role || '') >= 0;
  }
  function isPro() {
    if (!ctx) return false;
    return ctx.is_superadmin || ['pro', 'enterprise'].indexOf(ctx.tier || '') >= 0;
  }

  // ── List ───────────────────────────────────────────────────────────────
  function renderTable() {
    var tb = q('iv-tbody');
    q('iv-count').textContent = list.length
      ? list.length + ' ' + __t('inv.count', 'facturas') : '';
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="6" class="text-center text-muted">' +
        __t('inv.empty', 'Sin facturas — cierra un periodo para generar la primera.') + '</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (iv) {
      var voided = iv.status === 'void';
      var badge = voided
        ? '<span class="badge" style="background:rgba(192,57,43,0.15);color:#c0392b;">' + __t('inv.void', 'Anulada') + '</span>'
        : '<span class="badge badge-info">' + __t('inv.closed', 'Cerrada') + '</span>';
      var actions = '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.detail(' + iv.id + ')" data-i18n="inv.view">Ver</button>';
      if (isPro())
        actions += ' <button class="btn btn-sm pnl-day-btn" onclick="IvPage.pdf(' + iv.id + ')">PDF</button>';
      if (!voided && canVoid())
        actions += ' <button class="btn btn-sm btn-danger" onclick="IvPage.void(' + iv.id + ')" data-i18n="inv.voidBtn">Anular</button>';
      return '<tr' + (voided ? ' style="opacity:0.55;"' : '') + '>' +
        '<td>' + iv.period_start + ' → ' + iv.period_end + '</td>' +
        '<td>' + daysBetween(iv.period_start, iv.period_end) + '</td>' +
        '<td>' + fmt(iv.energy_kwh, 1) + ' kWh</td>' +
        '<td><b>' + eur(iv.total_eur) + '</b></td>' +
        '<td>' + badge + '</td>' +
        '<td style="text-align:right;white-space:nowrap;">' + actions + '</td>' +
      '</tr>';
    }).join('');
  }

  function daysBetween(a, b) {
    return Math.round((new Date(b) - new Date(a)) / 86400000) + 1;
  }

  // ── Detail panel ─────────────────────────────────────────────────────────
  function detail(id) {
    var body = q('iv-detail-body');
    body.innerHTML = '<span class="spinner"></span>';
    AppUI.openPanel('invoiceDetail');
    cfetch('/invoices/' + id).then(function (iv) {
      q('iv-detail-title').textContent = __t('inv.detailTitle', 'Factura') + ' · ' +
        iv.period_start + ' → ' + iv.period_end;
      var segs = (iv.breakdown && iv.breakdown.segments) || [];
      var html = '<div class="glass-panel" style="padding:0.8rem 1rem;margin-bottom:12px;">' +
        '<div style="display:flex;justify-content:space-between;font-size:1.1rem;font-weight:700;color:#2c5171;">' +
        '<span>' + __t('inv.total', 'Total') + '</span><span>' + eur(iv.total_eur) + '</span></div>' +
        '<div class="text-muted" style="font-size:0.75rem;margin-top:4px;">' +
        fmt(iv.energy_kwh, 2) + ' kWh · ' + __t('inv.settlementHourly', 'liquidación horaria') + '</div></div>';
      html += segs.map(function (s) {
        var rows = ['P1', 'P2', 'P3'].filter(function (p) { return s.energy && s.energy[p] && s.energy[p].kwh; })
          .map(function (p) {
            return '<tr><td>' + __t('inv.energy', 'Energía') + ' ' + p + '</td><td>' + fmt(s.energy[p].kwh, 2) + '</td><td>' + eur(s.energy[p].eur) + '</td></tr>';
          }).join('');
        var extra = '';
        if (s.power_eur) extra += '<tr><td>' + __t('inv.power', 'Potencia') + '</td><td>—</td><td>' + eur(s.power_eur) + '</td></tr>';
        if (s.fixed_eur) extra += '<tr><td>' + __t('inv.fixed', 'Cargos fijos') + '</td><td>—</td><td>' + eur(s.fixed_eur) + '</td></tr>';
        var ctr = (s.contract && (s.contract.label || s.contract.retailer)) || __t('inv.typePvpc', 'PVPC');
        return '<div style="margin-bottom:12px;">' +
          '<h4 style="margin:0 0 4px;font-size:0.85rem;color:#2c5171;">' + typeLabel(s.contract_type) +
          ' · ' + ctr + (s.no_contract ? ' <span style="color:#d97706;font-size:0.7rem;">(' + __t('inv.noContract', 'sin contrato — solo energía') + ')</span>' : '') + '</h4>' +
          '<div class="text-muted" style="font-size:0.72rem;margin-bottom:4px;">' + s.start + ' → ' + s.end + ' (' + s.days + ' ' + __t('inv.days', 'días') + ')</div>' +
          '<table class="data-table" style="font-size:0.8rem;"><tbody>' + rows +
          '<tr style="font-weight:600;"><td>' + __t('inv.energySub', 'Energía (subtotal)') + '</td><td>' + fmt(s.energy_kwh, 2) + '</td><td>' + eur(s.energy_eur) + '</td></tr>' +
          extra +
          '<tr><td>IEE</td><td>—</td><td>' + eur(s.iee_eur) + '</td></tr>' +
          '<tr><td>IVA</td><td>—</td><td>' + eur(s.vat_eur) + '</td></tr>' +
          '<tr style="font-weight:700;color:#2c5171;border-top:2px solid #2c5171;"><td>' + __t('inv.segTotal', 'Total tramo') + '</td><td></td><td>' + eur(s.total_eur) + '</td></tr>' +
          '</tbody></table></div>';
      }).join('');
      if (isPro())
        html += '<button class="btn btn-primary" style="width:100%;margin-top:6px;" onclick="IvPage.pdf(' + iv.id + ')">' + __t('inv.downloadPdf', 'Descargar PDF') + '</button>';
      body.innerHTML = html;
    }).catch(function (e) {
      body.innerHTML = '<p class="text-muted" style="color:#c0392b;">' + e.message + '</p>';
    });
  }

  var TYPE_LABEL = { pvpc: 'PVPC',
    fixed: function () { return __t('ct.tFixed', 'Precio fijo'); },
    indexed: function () { return __t('ct.tIndexed', 'Indexado'); } };
  function typeLabel(t) { var v = TYPE_LABEL[t]; return typeof v === 'function' ? v() : (v || t); }

  // ── PDF download (respects auth — fetch as blob, not a bare link) ────────
  function pdf(id) {
    var st = App.state || {};
    var headers = {};
    if (st.jwt && st.jwt.length >= 20) headers['Authorization'] = 'Bearer ' + st.jwt;
    else if (st.apiKey && st.apiKey.length >= 20) headers['X-API-Key'] = st.apiKey;
    fetch((window.__ROOT__ || '') + '/api/v1/invoices/' + id + '/pdf', { headers: headers })
      .then(function (r) {
        if (r.status === 403) { App.showNotification(__t('inv.pdfProTitle', 'Función PRO'), __t('inv.pdfPro', 'La descarga en PDF requiere plan PRO.'), 'warning'); return null; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        if (!blob) return;
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = 'factura_' + id + '.pdf';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      })
      .catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
  }

  // ── Void ─────────────────────────────────────────────────────────────────
  function voidInvoice(id) {
    AppUI.confirm(__t('inv.confirmVoid', '¿Anular esta factura? Podrás volver a cerrar el periodo después.'))
      .then(function (ok) {
        if (!ok) return;
        cfetch('/invoices/' + id + '/void', { method: 'POST' }).then(function () {
          App.showNotification(__t('inv.voided', 'Factura anulada'), '', 'success');
          loadAll();
        }).catch(function (e) {
          App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
        });
      });
  }

  // ── Close-period form ────────────────────────────────────────────────────
  function openForm() { q('iv-form-error').textContent = ''; AppUI.openPanel('invoicePanel'); }
  function closeForm() { AppUI.closePanel('invoicePanel'); }

  function presetLastMonth() {
    var now = new Date();
    var first = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    var last = new Date(now.getFullYear(), now.getMonth(), 0);
    q('iv-f-start').value = localDate(first);
    q('iv-f-end').value = localDate(last);
  }
  function localDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function save(ev) {
    ev.preventDefault();
    var errEl = q('iv-form-error'); errEl.textContent = '';
    var start = q('iv-f-start').value, end = q('iv-f-end').value;
    if (!start || !end) { errEl.textContent = __t('inv.errDates', 'Indica ambas fechas.'); return false; }
    var slug = customer || (ctx && ctx.customers && ctx.customers[0]);
    if (!slug) { errEl.textContent = __t('inv.errNoHome', 'No hay hogar seleccionado.'); return false; }
    q('iv-save-btn').disabled = true;
    cfetch('/invoices/close', { method: 'POST', body: JSON.stringify({ customer: slug, start: start, end: end }) })
      .then(function () {
        closeForm();
        App.showNotification(__t('inv.closed2', 'Factura cerrada'), '', 'success');
        loadAll();
      }).catch(function (e) {
        errEl.textContent = e.message;
      }).finally(function () { q('iv-save-btn').disabled = false; });
    return false;
  }

  // ── Load ─────────────────────────────────────────────────────────────────
  function loadAll() {
    return cfetch('/invoices' + custQS()).then(function (r) { list = r.values || []; })
      .catch(function () { list = []; })
      .then(renderTable);
  }

  function loadContext() {
    return App.apiFetch('/consumption/context').then(function (c) {
      ctx = c;
      var sel = q('iv-customer');
      var homes = c.customers || [];
      if (homes.length > 1) {
        sel.innerHTML = homes.map(function (s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
        sel.style.display = '';
        customer = homes[0];
      } else if (homes.length === 1) {
        customer = homes[0];
      }
      q('iv-new-btn').style.display = canWrite() ? '' : 'none';
    });
  }

  window.IvPage = {
    onCustomerChange: function () { customer = q('iv-customer').value || ''; loadAll(); },
    openForm: openForm, closeForm: closeForm, save: save,
    detail: detail, closeDetail: function () { AppUI.closePanel('invoiceDetail'); },
    pdf: pdf, void: voidInvoice, presetLastMonth: presetLastMonth,
  };

  document.addEventListener('i18n:changed', renderTable);

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      loadContext().then(loadAll).catch(function (e) {
        App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
      });
    });
  });
})();
