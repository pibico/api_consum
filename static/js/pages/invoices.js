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
  /* F3: punto de suministro — custQS + &supply= (o ?supply= si no hay query previa) */
  function supQS(sep) {
    var base = custQS(sep), s = App.supplyQS();
    if (!s) return base;
    return base ? base + s : ((sep || '?') === '?' ? '?' + s.slice(1) : s);
  }
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
  // Client-side pagination (shared Pager, CM4 pattern) — no infinite scroll.
  var pager = null;
  function ensurePager() {
    if (pager || !window.Pager) return pager;
    pager = window.Pager.create({
      containerIds: ['iv-pager-top', 'iv-pager-bottom'],
      pageSize: 10,
      totalLabel: __t('inv.count', 'facturas'),
      onRender: function (pageItems) { renderRows(pageItems); },
    });
    return pager;
  }

  function renderTable() {
    q('iv-count').textContent = list.length
      ? list.length + ' ' + __t('inv.count', 'facturas') : '';
    if (ensurePager()) { pager.setItems(list); return; }
    renderRows(list);   // fallback if the pager script didn't load
  }

  function renderRows(rows) {
    var tb = q('iv-tbody');
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="9" class="text-center text-muted">' +
        __t('inv.empty', 'Sin facturas — cierra un periodo para generar la primera.') + '</td></tr>';
      return;
    }
    tb.innerHTML = rows.map(function (iv) {
      var voided = iv.status === 'void';
      var uploaded = iv.status === 'uploaded' || iv.origin === 'uploaded';
      var badge = voided
        ? '<span class="badge" style="background:rgba(192,57,43,0.15);color:#c0392b;">' + __t('inv.void', 'Anulada') + '</span>'
        : uploaded
          ? '<span class="badge badge-ok">' + __t('inv.uploaded', 'Subida') + '</span>'
          : '<span class="badge badge-info">' + __t('inv.closed', 'Cerrada') + '</span>';
      var actions = '';
      var explainBtn = (!voided && ctx && ctx.ai_enabled)
        ? '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.explain(' + iv.id + ')" data-i18n="inv.explainBtn">Explicar</button> '
        : '';
      // Análisis (Phase 2.5) — the €-delta waterfall + PLC reconciliation.
      // Available for any non-void invoice (generated OR uploaded) —
      // unlike explainBtn it doesn't need ctx.ai_enabled (the waterfall
      // itself is deterministic; only the narrative sentence needs AI, and
      // that degrades to a template fallback server-side).
      var analysisBtn = !voided
        ? '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.analysis(' + iv.id + ')" data-i18n="inv.analysisBtn">Análisis</button> '
        : '';
      if (uploaded) {
        // The user's own document — always downloadable, no PRO gate;
        // editable (OCR gaps: totals/bands/period) and deletable (a bad
        // upload is a file mistake, not an accounting record).
        actions = explainBtn + analysisBtn +
          '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.file(' + iv.id + ')">PDF</button>' +
          ' <button class="btn btn-sm pnl-day-btn" onclick="IvPage.edit(' + iv.id + ')" data-i18n="inv.editBtn">Editar</button>' +
          ' <button class="btn btn-sm btn-danger" onclick="IvPage.remove(' + iv.id + ')" data-i18n="inv.deleteBtn">Eliminar</button>';
      } else {
        actions = explainBtn + analysisBtn +
          '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.detail(' + iv.id + ')" data-i18n="inv.view">Ver</button>';
        if (isPro())
          actions += ' <button class="btn btn-sm pnl-day-btn" onclick="IvPage.pdf(' + iv.id + ')">PDF</button>';
        if (!voided && canVoid())
          actions += ' <button class="btn btn-sm btn-danger" onclick="IvPage.void(' + iv.id + ')" data-i18n="inv.voidBtn">Anular</button>';
      }
      // CUPS is 20-22 chars — compact tail in the cell, full value on hover.
      var cups = iv.cups
        ? '<span style="font-family:monospace;font-size:0.78rem;" title="' + iv.cups + '">…' + iv.cups.slice(-8) + '</span>'
        : '<span class="text-muted">—</span>';
      var days = daysBetween(iv.period_start, iv.period_end);
      // Energy: total (uploaded rows may only know the band sum) + kWh/day.
      var kwhTotal = iv.energy_kwh ||
        ((iv.energy_p1_kwh || 0) + (iv.energy_p2_kwh || 0) + (iv.energy_p3_kwh || 0)) || null;
      var energy = kwhTotal
        ? fmt(kwhTotal, 1) + ' kWh' + (days > 0
            ? '<div class="text-muted" style="font-size:0.72rem;">' + fmt(kwhTotal / days, 1) + ' ' + __t('inv.perDay', 'kWh/día') + '</div>' : '')
        : '—';
      // Band split P1 · P2 · P3, colored like everywhere else in the app.
      var bands = (iv.energy_p1_kwh != null || iv.energy_p2_kwh != null || iv.energy_p3_kwh != null)
        ? '<span style="white-space:nowrap;font-size:0.82rem;" title="' + __t('inv.bandsTitle', 'caras · normales · baratas (kWh)') + '">' +
          '<span style="color:#e74c3c;font-weight:600;">' + fmt(iv.energy_p1_kwh || 0, 0) + '</span> · ' +
          '<span style="color:#f39c12;font-weight:600;">' + fmt(iv.energy_p2_kwh || 0, 0) + '</span> · ' +
          '<span style="color:#2ecc71;font-weight:600;">' + fmt(iv.energy_p3_kwh || 0, 0) + '</span></span>'
        : '<span class="text-muted">—</span>';
      // All-in effective price: total ÷ kWh (power, tolls and taxes INSIDE) —
      // the honest number to compare bills with. Click → concept breakdown.
      var allIn = (iv.total_eur && kwhTotal)
        ? '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.cost(' + iv.id + ')" ' +
          'title="' + __t('inv.allInTitle', 'Ver el desglose por conceptos') + '" ' +
          'style="font-family:monospace;font-size:0.8rem;padding:2px 8px;">' +
          (iv.total_eur / kwhTotal).toFixed(3).replace('.', ',') + '</button>'
        : '<span class="text-muted">—</span>';
      return '<tr' + (voided ? ' style="opacity:0.55;"' : '') + '>' +
        '<td>' + iv.period_start + ' → ' + iv.period_end + '</td>' +
        '<td>' + cups + '</td>' +
        '<td>' + days + '</td>' +
        '<td>' + energy + '</td>' +
        '<td>' + bands + '</td>' +
        '<td><b>' + eur(iv.total_eur) + '</b></td>' +
        '<td>' + allIn + '</td>' +
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
  // ── PDF viewer (slide panel) — view in place, download optional ──────────
  var _pdfUrl = null;
  function showPdf(blob, filename, title) {
    if (_pdfUrl) { URL.revokeObjectURL(_pdfUrl); _pdfUrl = null; }
    _pdfUrl = URL.createObjectURL(blob);
    q('iv-pdf-frame').src = _pdfUrl;
    q('iv-pdf-title').textContent = title || __t('inv.pdfViewerTitle', 'Factura');
    q('iv-pdf-download').onclick = function () {
      var a = document.createElement('a');
      a.href = _pdfUrl; a.download = filename || 'factura.pdf';
      document.body.appendChild(a); a.click(); a.remove();
    };
    AppUI.openPanel('pdfViewerPanel');
  }
  function closePdf() {
    AppUI.closePanel('pdfViewerPanel');
    q('iv-pdf-frame').src = 'about:blank';
    if (_pdfUrl) { URL.revokeObjectURL(_pdfUrl); _pdfUrl = null; }
    _setPdfExpanded(false);           // next open starts at the default width
  }

  // Expand the viewer to full width / back to the default 45% panel.
  // `.slide-panel.expanded` (collab.css, 100vw) is the shared modifier the
  // device console already uses for its Terminal/VNC iframes.
  function _setPdfExpanded(on) {
    var panel = document.getElementById('pdfViewerPanel');
    panel.classList.toggle('expanded', !!on);
    q('iv-pdf-ic-expand').style.display = on ? 'none' : '';
    q('iv-pdf-ic-collapse').style.display = on ? '' : 'none';
    var btn = q('iv-pdf-expand');
    btn.title = on ? __t('inv.pdfCollapse', 'Reducir') : __t('inv.pdfExpand', 'Expandir');
  }
  function togglePdfExpand() {
    var panel = document.getElementById('pdfViewerPanel');
    _setPdfExpanded(!panel.classList.contains('expanded'));
  }
  function _fetchPdf(path) {
    var st = App.state || {};
    var headers = {};
    if (st.jwt && st.jwt.length >= 20) headers['Authorization'] = 'Bearer ' + st.jwt;
    else if (st.apiKey && st.apiKey.length >= 20) headers['X-API-Key'] = st.apiKey;
    return fetch((window.__ROOT__ || '') + '/api/v1' + path, { headers: headers });
  }

  function pdf(id) {
    _fetchPdf('/invoices/' + id + '/pdf')
      .then(function (r) {
        if (r.status === 403) { App.showNotification(__t('inv.pdfProTitle', 'Función PRO'), __t('inv.pdfPro', 'La descarga en PDF requiere plan PRO.'), 'warning'); return null; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        if (!blob) return;
        showPdf(blob, 'factura_' + id + '.pdf', __t('inv.pdfGenerated', 'Factura generada'));
      })
      .catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
  }

  // View the ORIGINAL uploaded bill (own document — no PRO gate).
  function fileDownload(id) {
    _fetchPdf('/invoices/' + id + '/file')
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        showPdf(blob, 'factura_subida_' + id + '.pdf', __t('inv.pdfUploaded', 'Tu factura'));
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

  // ── Delete (uploaded bills only) ─────────────────────────────────────────
  function removeInvoice(id) {
    AppUI.confirm(__t('inv.confirmDelete', '¿Eliminar esta factura subida? Se borra también el documento. Esta acción no se puede deshacer.'))
      .then(function (ok) {
        if (!ok) return;
        cfetch('/invoices/' + id, { method: 'DELETE' }).then(function () {
          App.showNotification(__t('inv.deleted', 'Factura eliminada'), '', 'success');
          loadAll();
        }).catch(function (e) {
          App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
        });
      });
  }

  // ── Close-period form ────────────────────────────────────────────────────
  // The OPENING day is not a question — it is the day after the household's
  // last (non-void) invoice, same anchor the "factura en curso" KPIs use.
  // Prefilled (editable for the first-ever close); the user only picks the
  // closing day, defaulted to yesterday.
  function openForm() {
    q('iv-form-error').textContent = '';
    var latest = null;
    (list || []).forEach(function (iv) {
      if (iv.status === 'void') return;
      if (!latest || iv.period_end > latest) latest = iv.period_end;
    });
    var hint = q('iv-f-start-hint');
    if (latest) {
      var d = new Date(latest + 'T00:00:00');
      d.setDate(d.getDate() + 1);
      var start = localDate(d);
      q('iv-f-start').value = start;
      if (hint) hint.textContent = __t('inv.fStartAuto', 'día siguiente a tu última factura');
      var y = new Date(); y.setDate(y.getDate() - 1);
      var yesterday = localDate(y);
      q('iv-f-end').value = yesterday >= start ? yesterday : '';
    } else if (hint) {
      hint.textContent = '';
    }
    AppUI.openPanel('invoicePanel');
    var endEl = q('iv-f-end');
    if (endEl) setTimeout(function () { endEl.focus(); }, 150);
  }
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
    loadBillingPeriod();
    return cfetch('/invoices' + supQS()).then(function (r) { list = r.values || []; })
      .catch(function () { list = []; })
      .then(function () { renderTable(); renderCharts(); renderSummary(true); });
  }

  // ── "Resumen entre fechas" — invoiced kWh + € over the invoices whose
  // period overlaps the picked range; partially-covered invoices are
  // pro-rated by days. Pure client-side over the loaded list.
  // User-picked dates WIN: once touched they survive list reloads (void/
  // close/slow first load) and page reloads (sessionStorage per household);
  // only switching household resets them to the full span.
  var _sumTouched = false;
  function _sumKey() { return 'iv.sumDates.' + (customer || ''); }
  function sumDatesChanged() {
    _sumTouched = true;
    try {
      sessionStorage.setItem(_sumKey(), JSON.stringify({
        from: q('iv-sum-from').value, to: q('iv-sum-to').value,
      }));
    } catch (e) { /* storage full/blocked — keep going */ }
    renderSummary();
  }
  function renderSummary(resetDates) {
    var card = q('iv-sum-card');
    if (!card) return;
    var rows = (list || []).filter(function (iv) { return iv.status !== 'void'; });
    if (!rows.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    var fromEl = q('iv-sum-from'), toEl = q('iv-sum-to');
    if (!_sumTouched && (resetDates === true || !fromEl.value || !toEl.value)) {
      var saved = null;
      try { saved = JSON.parse(sessionStorage.getItem(_sumKey()) || 'null'); } catch (e) { /* ignore */ }
      if (saved && saved.from && saved.to) {
        fromEl.value = saved.from; toEl.value = saved.to;
        _sumTouched = true;
      } else {
        // Default to the loaded history's full span (rows come newest first).
        var minS = rows[rows.length - 1].period_start, maxE = rows[0].period_end;
        rows.forEach(function (iv) {
          if (iv.period_start < minS) minS = iv.period_start;
          if (iv.period_end > maxE) maxE = iv.period_end;
        });
        fromEl.value = minS; toEl.value = maxE;
      }
    }
    var from = fromEl.value, to = toEl.value;
    var sel = rows.filter(function (iv) {
      return iv.period_end >= from && iv.period_start <= to;
    });
    // Invoices only partially inside the range are PRO-RATED by days: the
    // covered fraction of the period scales its kWh and €.
    var kwh = 0, tot = 0, days = 0, costed = 0, partial = 0;
    sel.forEach(function (iv) {
      var e = iv.energy_kwh ||
        ((iv.energy_p1_kwh || 0) + (iv.energy_p2_kwh || 0) + (iv.energy_p3_kwh || 0));
      var invDays = daysBetween(iv.period_start, iv.period_end) || 1;
      var ovDays = daysBetween(iv.period_start > from ? iv.period_start : from,
                               iv.period_end < to ? iv.period_end : to);
      if (ovDays <= 0) return;
      var frac = Math.min(ovDays / invDays, 1);
      if (frac < 1) partial++;
      kwh += (e || 0) * frac;
      days += ovDays;
      if (iv.total_eur != null) { tot += iv.total_eur * frac; costed += (e || 0) * frac; }
    });
    function cell(label, value, sub) {
      return '<div><div style="font-size:0.68rem;color:var(--color-text-light,#6b7f92);text-transform:uppercase;letter-spacing:0.03em;">' + label + '</div>' +
        '<div style="font-size:1.15rem;font-weight:700;color:#2c5171;">' + value + '</div>' +
        (sub ? '<div style="font-size:0.68rem;color:var(--color-text-light,#6b7f92);">' + sub + '</div>' : '') + '</div>';
    }
    q('iv-sum-body').innerHTML =
      cell(__t('inv.sumKwh', 'Consumo facturado'), fmt(kwh, 0) + ' kWh',
           days > 0 ? fmt(kwh / days, 1) + ' ' + __t('inv.perDay', 'kWh/día') : '') +
      cell(__t('inv.sumEur', 'Importe'), eur(tot),
           costed > 0 ? fmt(tot / costed, 3) + ' €/kWh ' + __t('inv.sumAllin', 'con todo') : '') +
      cell(__t('inv.sumCount', 'Facturas'), String(sel.length),
           days > 0 ? fmt(days, 0) + ' ' + __t('inv.sumDays', 'días') : '');
    q('iv-sum-note').textContent = !sel.length
      ? __t('inv.sumEmpty', 'Ninguna factura en ese rango de fechas.')
      : (partial > 0
        ? __t('inv.sumProrated', '{n} factura(s) entran a medias: se prorratean por días.')
          .replace('{n}', partial)
        : '');
  }

  // ── Per-invoice charts (above the table): kWh/day and band split ─────────
  var _ivCharts = {};
  var _ivRange = 12;   // months back shown in the charts (6/12/24/36 selector)
  function _chart(id) {
    var el = q(id);
    if (!el || typeof echarts === 'undefined') return null;
    if (!_ivCharts[id]) _ivCharts[id] = echarts.init(el);
    return _ivCharts[id];
  }
  function setRange(months) {
    _ivRange = months;
    renderCharts();
  }
  function _paintRange() {
    var box = q('iv-range');
    if (!box) return;
    box.querySelectorAll('button').forEach(function (b) {
      var on = +b.dataset.months === _ivRange;
      b.style.background = on ? 'rgba(70,130,180,0.85)' : 'none';
      b.style.color = on ? '#fff' : '';
      b.style.fontWeight = on ? '600' : '';
    });
  }
  function renderCharts() {
    var wrap = q('iv-charts');
    if (!wrap) return;
    _paintRange();
    // Range cutoff: only periods ending in the last N months.
    var cut = new Date();
    cut.setMonth(cut.getMonth() - _ivRange);
    var cutIso = localDate(cut);
    // Oldest → newest, skip voided; need at least 2 periods to be a chart.
    var rows = (list || []).filter(function (iv) {
      return iv.status !== 'void' && iv.period_end >= cutIso;
    }).slice().sort(function (a, b) { return a.period_start < b.period_start ? -1 : 1; });
    if (rows.length < 2) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';
    var labels = rows.map(function (iv) { return iv.period_end; });
    var days = rows.map(function (iv) { return daysBetween(iv.period_start, iv.period_end); });
    var AX = {
      axisLabel: { fontSize: 9, color: '#3d5a75' },
      axisLine: { lineStyle: { color: 'rgba(44,81,113,0.3)' } },
    };
    var perDay = _chart('iv-chart-perday');
    if (perDay) {
      perDay.setOption({
        grid: { left: 40, right: 10, top: 12, bottom: 22 },
        tooltip: { trigger: 'axis' },
        xAxis: Object.assign({ type: 'category', data: labels }, AX),
        yAxis: Object.assign({ type: 'value' }, AX),
        series: [{
          type: 'line', name: __t('inv.perDay', 'kWh/día'),
          symbol: 'circle', symbolSize: 9, smooth: 0.25, connectNulls: true,
          lineStyle: { color: 'rgba(70,130,180,0.9)', width: 2.5 },
          itemStyle: { color: '#4682b4', borderColor: '#fff', borderWidth: 1.5 },
          areaStyle: { color: 'rgba(70,130,180,0.10)' },
          data: rows.map(function (iv, i) {
            var tot = iv.energy_kwh ||
              ((iv.energy_p1_kwh || 0) + (iv.energy_p2_kwh || 0) + (iv.energy_p3_kwh || 0));
            return tot && days[i] ? +(tot / days[i]).toFixed(2) : null;
          }),
        }],
      }, true);
    }
    var allin = _chart('iv-chart-allin');
    if (allin) {
      allin.setOption({
        grid: { left: 44, right: 10, top: 12, bottom: 22 },
        tooltip: { trigger: 'axis', valueFormatter: function (v) { return v != null ? v.toFixed(3) + ' €/kWh' : '—'; } },
        xAxis: Object.assign({ type: 'category', data: labels }, AX),
        yAxis: Object.assign({ type: 'value', scale: true }, AX),
        series: [{
          type: 'line', name: '€/kWh',
          symbol: 'circle', symbolSize: 9, smooth: 0.25, connectNulls: true,
          lineStyle: { color: 'rgba(230,126,34,0.9)', width: 2.5 },
          itemStyle: { color: '#e67e22', borderColor: '#fff', borderWidth: 1.5 },
          areaStyle: { color: 'rgba(230,126,34,0.08)' },
          data: rows.map(function (iv) {
            var tot = iv.energy_kwh ||
              ((iv.energy_p1_kwh || 0) + (iv.energy_p2_kwh || 0) + (iv.energy_p3_kwh || 0));
            return (iv.total_eur && tot) ? +(iv.total_eur / tot).toFixed(3) : null;
          }),
        }],
      }, true);
    }
    var bands = _chart('iv-chart-bands');
    if (bands) {
      function serie(name, key, color) {
        return { type: 'bar', stack: 'kwh', name: name, barMaxWidth: 26,
                 itemStyle: { color: color },
                 data: rows.map(function (iv) { return iv[key] != null ? +iv[key] : null; }) };
      }
      bands.setOption({
        grid: { left: 40, right: 10, top: 12, bottom: 22 },
        tooltip: { trigger: 'axis' },
        legend: { show: false },
        xAxis: Object.assign({ type: 'category', data: labels }, AX),
        yAxis: Object.assign({ type: 'value' }, AX),
        series: [
          serie('P1', 'energy_p1_kwh', 'rgba(231,76,60,0.85)'),
          serie('P2', 'energy_p2_kwh', 'rgba(243,156,18,0.85)'),
          serie('P3', 'energy_p3_kwh', 'rgba(46,204,113,0.85)'),
        ],
      }, true);
    }
  }
  window.addEventListener('resize', function () {
    Object.keys(_ivCharts).forEach(function (k) { _ivCharts[k].resize(); });
  });

  // ── "Factura en curso" KPIs — the OPEN billing period, anchored on the
  // invoice history (cycle = median bill length) + projection to close.
  // PRO endpoint: hide the row quietly on 403/basic (cfetch, never logout).
  function loadBillingPeriod() {
    var row = q('iv-bp-row');
    if (!row) return;
    cfetch('/invoices/billing-period' + supQS()).then(function (bp) {
      if (!bp || bp.status !== 'ok') { row.style.display = 'none'; return; }
      row.style.display = '';
      _bp = bp;
      // A user-set close date is exact (no ~); the estimate keeps the tilde.
      var approx = bp.end_source === 'user' ? '' : '~';
      q('iv-bp-period').textContent = bp.period_start + ' → ' + approx + bp.expected_end;
      q('iv-bp-days').textContent = __t('inv.bpDay', 'día {n} de ~{m}')
        .replace('{n}', bp.days_elapsed)
        .replace('~{m}', approx + bp.days_total)
        .replace('{m}', bp.days_total);
      q('iv-bp-acc').textContent = eur(bp.total_eur);
      // Total + band split, colored like the invoice table (P1·P2·P3).
      var accKwh = fmt(bp.energy_kwh, 1) + ' kWh';
      if (bp.energy_p1_kwh != null || bp.energy_p2_kwh != null || bp.energy_p3_kwh != null) {
        accKwh += ' &nbsp;<span style="white-space:nowrap;" title="' +
          __t('inv.bandsTitle', 'caras · normales · baratas (kWh)') + '">' +
          '<span style="color:#e74c3c;font-weight:600;">' + fmt(bp.energy_p1_kwh || 0, 0) + '</span> · ' +
          '<span style="color:#f39c12;font-weight:600;">' + fmt(bp.energy_p2_kwh || 0, 0) + '</span> · ' +
          '<span style="color:#2ecc71;font-weight:600;">' + fmt(bp.energy_p3_kwh || 0, 0) + '</span></span>';
      }
      q('iv-bp-acc-kwh').innerHTML = accKwh;
      q('iv-bp-proj').textContent = bp.projected_eur != null ? '~' + eur(bp.projected_eur) : '—';
      var projSub = bp.eur_day != null
        ? eur(bp.eur_day) + '/' + __t('inv.bpPerDay', 'día') + ' · ' + __t('inv.bpEstimate', 'estimado')
        : __t('inv.bpTooEarly', 'aún pocos días para estimar');
      var projEl = q('iv-bp-proj-sub');
      if (bp.incomplete) {
        // Missing reading days understate the accrual AND the projection.
        projEl.innerHTML = esc(projSub) +
          '<div style="color:#b9770e;font-weight:600;">' +
          esc(__t('inv.bpIncomplete', 'medición incompleta: {m} de {n} días con lecturas')
            .replace('{m}', bp.measured_days).replace('{n}', bp.days_elapsed)) + '</div>';
      } else {
        projEl.textContent = projSub;
      }
    }).catch(function () { row.style.display = 'none'; });
  }

  // The household KNOWS its meter-reading day — let them set the close date
  // (beats the median estimate; PUT null via clearing is not offered here,
  // picking a new date simply replaces it).
  function editBpEnd() {
    var box = q('iv-bp-edit');
    if (!box) return;
    if (box.style.display === 'flex') { box.style.display = 'none'; return; }
    q('iv-bp-end-input').value = (_bp && _bp.expected_end) || '';
    box.style.display = 'flex';
  }
  function saveBpEnd() {
    var v = q('iv-bp-end-input').value;
    if (!v) return;
    cfetch('/invoices/billing-period/expected-end' + custQS(), {
      method: 'PUT', body: JSON.stringify({ date: v }),
    }).then(function () {
      q('iv-bp-edit').style.display = 'none';
      loadBillingPeriod();
    }).catch(function (e) {
      App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
    });
  }

  function loadContext() {
    return App.apiFetch('/consumption/context').then(function (c) {
      ctx = c;
      var sel = q('iv-customer');
      var homes = c.customers || [];
      // Selector BY PLC (app.js pattern): one entry per enrolled gateway,
      // labeled with its hostname; value = its customer slug (tenancy key).
      var gateways = (c.devices || []).filter(function (d) {
        return (d.device_type || '') === 'gateway' || !d.device_type;
      });
      var multiSp = ((c && c.supply_points) || []).length > 1;
      if (gateways.length > 1 && !multiSp) {   /* F3: con 2+ puntos manda el selector global */
        sel.innerHTML = gateways.map(function (d) {
          return '<option value="' + d.customer + '">' + (d.hostname || d.customer) + '</option>';
        }).join('');
        sel.style.display = '';
        customer = gateways[0].customer;
      } else if (multiSp) {
        customer = '';   /* el punto global escopa; no fijar slug (multi-org) */
      } else if (gateways.length === 1) {
        customer = gateways[0].customer;
      } else if (homes.length === 1) {
        customer = homes[0];
      }
      q('iv-new-btn').style.display = canWrite() ? '' : 'none';
    });
  }

  // ── "Tu tarifa" + upload-your-invoice onboarding (2026-07-12 design) ──────
  // The invoice is the document users actually HAVE. Uploading it configures
  // the pricing (the supply contract is created/updated BEHIND THE SCENES —
  // the user never meets the "contract" concept; /app/contract = advanced).
  var extracted = null;    // last AI extraction
  var chosenType = null;   // fixed|indexed|pvpc after step 2 (or doc-settled)
  var catalogHit = null;   // catalog row when retailer/product resolved — settles
                           // the type without asking and prefills missing terms
  var _bp = null;          // last billing-period payload (edit-close-date UI)
  var activeContract = null;

  function upfetch(endpoint, formData) {
    var headers = {};
    var st = App.state || {};
    if (st.jwt && st.jwt.length >= 20) headers['Authorization'] = 'Bearer ' + st.jwt;
    else if (st.apiKey && st.apiKey.length >= 20) headers['X-API-Key'] = st.apiKey;
    return fetch((window.__ROOT__ || '') + '/api/v1' + endpoint,
      { method: 'POST', headers: headers, body: formData }).then(function (r) {
      if (r.status === 401) { App.logout(); return new Promise(function () {}); }
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) {
          var d = body.detail;
          var msg = (d && (d.message || d)) || ('HTTP ' + r.status);
          throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
        }
        return body;
      });
    });
  }

  function typePlain(t) {
    return t === 'fixed' ? __t('inv.typeFixed', 'precio fijo')
      : t === 'indexed' ? __t('inv.typeIndexed', 'precio que varía con el mercado')
      : __t('inv.typePvpc', 'tarifa regulada (PVPC)');
  }

  function loadTariffCard() {
    return cfetch('/contracts/active' + supQS()).then(function (r) {
      activeContract = r.contract || null;
      var el = q('iv-tariff-body');
      if (!el) return;
      if (!activeContract) {
        el.innerHTML = '<span class="text-muted">' + __t('inv.noTariff',
          'Aún no sabemos tu tarifa. Sube tu última factura y lo configuramos por ti — tus números en € pasarán a ser los de verdad.') + '</span>' +
          ' <a href="' + (window.__ROOT__ || '') + '/app/contract"' +
          ' style="font-size:0.76rem;color:#2c5171;text-decoration:underline;white-space:nowrap;"' +
          ' data-i18n="inv.advancedLink">' + __t('inv.advancedLink', 'Corregir (avanzado)') + '</a>';
        return;
      }
      var c = activeContract;
      var bits = [];
      if (c.retailer) bits.push(__t('inv.tfWho', 'Pagas la luz a') + ' <b>' + c.retailer + '</b>');
      bits.push('<b>' + typePlain(c.contract_type) + '</b>');
      if (c.contract_type === 'fixed' && c.energy_p1_eur_kwh != null)
        bits.push('~' + Number(c.energy_p1_eur_kwh).toFixed(3).replace('.', ',') + ' €/kWh');
      if (c.power_p1_kw != null)
        bits.push(__t('inv.tfPower', 'potencia') + ' <b>' + String(c.power_p1_kw).replace('.', ',') + ' kW</b>');
      var pn = r.price_now || {};
      var now = pn.price_eur_kwh != null
        ? ' · ' + __t('inv.tfNow', 'ahora mismo') + ' <b>' + pn.price_eur_kwh.toFixed(4).replace('.', ',') + ' €/kWh</b>'
        : '';
      // The advanced/operator editor (/app/contract) left the nav on purpose
      // (2026-07-12) — this discreet link is its ONLY entry point in the UI.
      var advanced = ' &nbsp;<a href="' + (window.__ROOT__ || '') + '/app/contract"' +
        ' style="font-size:0.76rem;color:#2c5171;text-decoration:underline;white-space:nowrap;"' +
        ' data-i18n="inv.advancedLink">' + __t('inv.advancedLink', 'Corregir (avanzado)') + '</a>';
      el.innerHTML = '<div style="font-size:0.9rem;">' + bits.join(' · ') + now + advanced + '</div>';
    }).catch(function () {
      var el = q('iv-tariff-body');
      if (el) el.innerHTML = '<span class="text-muted">—</span>';
    });
  }

  function upStep(n) {
    q('iv-up-step1').style.display = n === 1 ? 'flex' : 'none';
    q('iv-up-step2').style.display = n === 2 ? 'flex' : 'none';
    q('iv-up-step3').style.display = n === 3 ? 'flex' : 'none';
  }
  function setUpStatus(id, msg, kind) {
    var el = q(id);
    el.textContent = msg || '';
    el.style.color = kind === 'err' ? '#c0392b' : (kind === 'busy' ? '#2c5171' : '');
  }
  function openUpload() {
    q('iv-up-file').value = '';
    q('iv-up-vlm').checked = false;
    setUpStatus('iv-up-status', '', ''); setUpStatus('iv-up-save-status', '', '');
    extracted = null; chosenType = null;
    upStep(1);
    AppUI.openPanel('uploadInvoicePanel');
  }
  function closeUpload() { AppUI.closePanel('uploadInvoicePanel'); }

  // Archive the uploaded bill itself (origin='uploaded') — the Facturas page
  // KEEPS the document besides configuring the tariff. Fire-and-forget.
  function archiveUpload(file, ex, markdown) {
    // Archive ONLY confirmed bills — a contract isn't an invoice, and an
    // unclassified doc must never land in the list (kind gets hallucinated).
    if (!file || !ex || ex.document_kind !== 'factura') return;
    var fd = new FormData();
    fd.append('file', file);
    if (ex) fd.append('extracted', JSON.stringify(ex));
    if (markdown) fd.append('markdown', markdown);   // powers "Explicar" line items
    var slug = customer || (ctx && ctx.customers && ctx.customers[0]);
    if (slug) fd.append('customer', slug);
    upfetch('/invoices/upload', fd).then(function () {
      loadAll();   // the bill appears in the list right away
    }).catch(function (e) {
      // Duplicado (409): avisa sin romper el flujo de tarifa; el resto de
      // fallos de archivado siguen siendo best-effort silencioso.
      if (/ya está subida/i.test(e.message || '')) {
        App.showNotification(__t('inv.dupTitle', 'Factura repetida'), e.message, 'warning');
      }
    });
  }

  function readInvoice() {
    var file = q('iv-up-file').files[0];
    if (!file) {
      setUpStatus('iv-up-status', __t('inv.upNeed', 'Elige el PDF o la foto de tu factura.'), 'err');
      return;
    }
    var fd = new FormData();
    fd.append('file', file);
    fd.append('use_vlm', q('iv-up-vlm').checked ? 'true' : 'false');
    q('iv-up-read').disabled = true;
    setUpStatus('iv-up-status', __t('inv.upReading', 'Leyendo tu factura… puede tardar unos segundos.'), 'busy');
    upfetch('/contracts/extract', fd).then(function (r) {
      extracted = r.extracted || {};
      archiveUpload(file, extracted, r.markdown || '');
      // Trust the type ONLY from a contract document (an invoice shows monthly
      // AVERAGE prices that look fixed even on indexed products).
      catalogHit = null;
      if (extracted.document_kind === 'contrato' && extracted.contract_type) {
        chosenType = extracted.contract_type;
        showConfirm();
      } else if (extracted.contract_type === 'pvpc') {
        chosenType = 'pvpc';
        showConfirm();
      } else if (extracted.retailer) {
        // The CATALOG knows the product's real type — resolve before asking.
        cfetch('/contracts/catalog/resolve?retailer=' + encodeURIComponent(extracted.retailer) +
               (extracted.product_name ? '&product=' + encodeURIComponent(extracted.product_name) : ''))
          .then(function (r) {
            if (r && r.match && r.match.contract_type) {
              catalogHit = r.match;
              chosenType = r.match.contract_type;
              showConfirm();
            } else { upStep(2); }
          }).catch(function () { upStep(2); });
      } else {
        upStep(2);
      }
    }).catch(function (e) {
      setUpStatus('iv-up-status', e.message || __t('common.error', 'Error'), 'err');
    }).finally(function () { q('iv-up-read').disabled = false; });
  }

  function answerType(t) { chosenType = t; showConfirm(); }

  function showConfirm() {
    var ex = extracted || {};
    var lines = [];
    if (ex.retailer) lines.push(__t('inv.tfWho', 'Pagas la luz a') + ' <b>' + ex.retailer + '</b>.');
    lines.push(__t('inv.cfType', 'Tu precio es') + ' <b>' + typePlain(chosenType) + '</b>' +
      (catalogHit ? ' <span class="text-muted" style="font-size:0.78rem;">(' +
        __t('inv.cfCatalog', 'según nuestro catálogo de tarifas') + ')</span>' : '') + '.');
    if (chosenType === 'fixed' && ex.energy_p1_eur_kwh != null)
      lines.push(__t('inv.cfPrice', 'Alrededor de') + ' <b>' +
        Number(ex.energy_p1_eur_kwh).toFixed(3).replace('.', ',') + ' €/kWh</b>.');
    if (ex.power_p1_kw != null)
      lines.push(__t('inv.cfPower', 'Tienes contratados') + ' <b>' + String(ex.power_p1_kw).replace('.', ',') + ' kW</b>.');
    if (ex.cups) lines.push('CUPS <span class="mono" style="font-size:0.72rem;">' + ex.cups + '</span>.');
    lines.push(__t('inv.cfAsk', '¿Es correcto?'));
    q('iv-up-summary').innerHTML = lines.join('<br>');
    upStep(3);
  }

  function saveTariff() {
    var ex = extracted || {};
    var slug = customer || (ctx && ctx.customers && ctx.customers[0]);
    if (!slug) {
      setUpStatus('iv-up-save-status', __t('inv.errNoHome', 'No hay hogar seleccionado.'), 'err');
      return;
    }
    var today = localDate(new Date());
    var cat = catalogHit || {};   // catalog terms fill what the bill lacks
    var p = {
      contract_type: chosenType,
      label: ex.product_name || cat.product_name || null,
      retailer: ex.retailer || cat.retailer || null,
      cups: ex.cups || null,
      access_tariff: cat.access_tariff || '2.0TD',
      start_date: (ex.start_date && /^\d{4}-\d{2}-\d{2}$/.test(ex.start_date)) ? ex.start_date : today,
      end_date: null,
      power_p1_kw: ex.power_p1_kw != null ? ex.power_p1_kw : null,
      power_p2_kw: ex.power_p2_kw != null ? ex.power_p2_kw : (ex.power_p1_kw != null ? ex.power_p1_kw : null),
      power_p1_eur_kw_day: ex.power_p1_eur_kw_day != null ? ex.power_p1_eur_kw_day
        : (cat.power_p1_eur_kw_day != null ? cat.power_p1_eur_kw_day : null),
      power_p2_eur_kw_day: ex.power_p2_eur_kw_day != null ? ex.power_p2_eur_kw_day
        : (cat.power_p2_eur_kw_day != null ? cat.power_p2_eur_kw_day : null),
      meter_rental_eur_month: ex.meter_rental_eur_month != null ? ex.meter_rental_eur_month
        : (cat.meter_rental_eur_month != null ? cat.meter_rental_eur_month : 0.81),
      notes: (ex.notes ? ex.notes + ' · ' : '') + __t('inv.viaInvoice', 'Configurado desde factura subida') +
        (catalogHit ? ' · ' + __t('inv.viaCatalog', 'tipo y términos del catálogo de tarifas') : ''),
    };
    if (chosenType === 'fixed') {
      // The invoice's period prices are what they actually pay — best available;
      // the catalog's sheet prices only fill in when the bill shows none.
      p.energy_p1_eur_kwh = ex.energy_p1_eur_kwh != null ? ex.energy_p1_eur_kwh : cat.energy_p1_eur_kwh;
      p.energy_p2_eur_kwh = ex.energy_p2_eur_kwh != null ? ex.energy_p2_eur_kwh : cat.energy_p2_eur_kwh;
      p.energy_p3_eur_kwh = ex.energy_p3_eur_kwh != null ? ex.energy_p3_eur_kwh : cat.energy_p3_eur_kwh;
      if (p.energy_p1_eur_kwh == null) {
        // "Editor avanzado" = la página Contrato — enlaza, no lo menciones a secas.
        var st = q('iv-up-save-status');
        st.innerHTML = __t('inv.errNoPrice',
          'No pudimos leer tu precio en el documento — usa el editor avanzado.') +
          ' <a href="' + (window.__ROOT__ || '') + '/app/contract" style="color:#2c5171;text-decoration:underline;">' +
          __t('inv.advancedLink', 'Corregir (avanzado)') + '</a>';
        st.style.color = '#c0392b';
        return;
      }
    } else if (chosenType === 'indexed') {
      // CC per period from the doc if present; then the CATALOG's reviewed
      // terms; only then the SSOT reference default (flagged).
      p.components = ex.components || cat.components || null;
      p.margin_eur_kwh = (ex.components && ex.components.CC && ex.components.CC.P1) != null
        ? ex.components.CC.P1
        : (ex.margin_eur_kwh != null ? ex.margin_eur_kwh
           : (cat.margin_eur_kwh != null ? cat.margin_eur_kwh : 0.03));
      if (!p.components && cat.margin_eur_kwh == null) {
        p.notes += ' · ' + __t('inv.estMargin', 'margen estimado (referencia) — sube tu contrato para afinar');
      }
    }
    q('iv-up-save').disabled = true;
    setUpStatus('iv-up-save-status', __t('inv.saving', 'Guardando…'), 'busy');
    var req = activeContract
      ? cfetch('/contracts/' + activeContract.id, { method: 'PUT', body: JSON.stringify(p) })
      : cfetch('/contracts', { method: 'POST', body: JSON.stringify(Object.assign({ customer: slug }, p)) });
    req.then(function () {
      closeUpload();
      App.showNotification(__t('inv.savedTitle', 'Listo'),
        __t('inv.savedMsg', 'Tu tarifa quedó configurada — tus números en € ya son los de verdad.'), 'success');
      loadTariffCard();
    }).catch(function (e) {
      setUpStatus('iv-up-save-status', e.message || __t('common.error', 'Error'), 'err');
    }).finally(function () { q('iv-up-save').disabled = false; });
  }

  // ── "Corregir tu tarifa" — plain essentials in a slide panel (no page
  //    navigation; the full /app/contract editor stays an operator link) ──
  var fixTypeVal = null;

  function fixType(t) {
    fixTypeVal = t;
    ['fixed', 'indexed', 'pvpc'].forEach(function (k) {
      q('iv-fx-t-' + k).classList.toggle('active', k === t);
      q('iv-fx-t-' + k).style.borderColor = k === t ? 'var(--color-primary)' : '';
      q('iv-fx-t-' + k).style.background = k === t ? 'rgba(70,130,180,0.12)' : '';
    });
    q('iv-fx-prices').style.display = t === 'fixed' ? '' : 'none';
  }

  function openFix() {
    var c = activeContract || {};
    q('iv-fx-retailer').value = c.retailer || '';
    q('iv-fx-ep1').value = c.energy_p1_eur_kwh != null ? c.energy_p1_eur_kwh : '';
    q('iv-fx-ep2').value = c.energy_p2_eur_kwh != null ? c.energy_p2_eur_kwh : '';
    q('iv-fx-ep3').value = c.energy_p3_eur_kwh != null ? c.energy_p3_eur_kwh : '';
    q('iv-fx-kw').value = c.power_p1_kw != null ? c.power_p1_kw : '';
    setUpStatus('iv-fx-status', '', '');
    fixType(c.contract_type || 'pvpc');
    AppUI.openPanel('tariffFixPanel');
  }
  function closeFix() { AppUI.closePanel('tariffFixPanel'); }

  function saveFix() {
    var slug = customer || (ctx && ctx.customers && ctx.customers[0]);
    if (!slug) {
      setUpStatus('iv-fx-status', __t('inv.errNoHome', 'No hay hogar seleccionado.'), 'err');
      return;
    }
    var kw = q('iv-fx-kw').value === '' ? null : Number(q('iv-fx-kw').value);
    var p = {
      contract_type: fixTypeVal || 'pvpc',
      retailer: q('iv-fx-retailer').value.trim() || null,
      power_p1_kw: kw, power_p2_kw: kw,
    };
    if (p.contract_type === 'fixed') {
      p.energy_p1_eur_kwh = q('iv-fx-ep1').value === '' ? null : Number(q('iv-fx-ep1').value);
      p.energy_p2_eur_kwh = q('iv-fx-ep2').value === '' ? null : Number(q('iv-fx-ep2').value);
      p.energy_p3_eur_kwh = q('iv-fx-ep3').value === '' ? null : Number(q('iv-fx-ep3').value);
      if (p.energy_p1_eur_kwh == null) {
        setUpStatus('iv-fx-status', __t('inv.fxNeedPrice', 'Indica al menos el primer precio (€/kWh).'), 'err');
        return;
      }
    } else if (p.contract_type === 'indexed') {
      // keep existing components/margin; fall back to the reference margin
      var c = activeContract || {};
      p.components = c.components || null;
      p.margin_eur_kwh = c.margin_eur_kwh != null ? c.margin_eur_kwh : 0.03;
    }
    q('iv-fx-save').disabled = true;
    setUpStatus('iv-fx-status', __t('inv.saving', 'Guardando…'), 'busy');
    var today = localDate(new Date());
    var req = activeContract
      ? cfetch('/contracts/' + activeContract.id, { method: 'PUT', body: JSON.stringify(p) })
      : cfetch('/contracts', { method: 'POST', body: JSON.stringify(Object.assign({ customer: slug, start_date: today }, p)) });
    req.then(function () {
      closeFix();
      App.showNotification(__t('inv.savedTitle', 'Listo'), __t('inv.fxSaved', 'Tarifa corregida.'), 'success');
      loadTariffCard();
    }).catch(function (e) {
      setUpStatus('iv-fx-status', e.message || __t('common.error', 'Error'), 'err');
    }).finally(function () { q('iv-fx-save').disabled = false; });
  }

  // ── "Tu factura, explicada" (InvoiceSkill) ────────────────────────────────
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function mdLite(text) {
    // **bold** → <b> (the explanation uses bold mini-headers per concept)
    return esc(text).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  }
  var ANOM_STYLE = {
    normal: 'background:rgba(46,204,113,0.12);border:1px solid rgba(46,204,113,0.4);color:#1e7e4e;',
    aviso: 'background:rgba(243,156,18,0.12);border:1px solid rgba(243,156,18,0.45);color:#9c6a06;',
    alerta: 'background:rgba(231,76,60,0.12);border:1px solid rgba(231,76,60,0.45);color:#b03024;',
  };

  var _explainId = null;   // invoice shown in the explain panel (→ PDF button)

  function explain(id, regen) {
    if (id == null) id = _explainId;        // Regenerar keeps the open invoice
    if (id == null) return;
    _explainId = id;
    var body = q('iv-explain-body');
    var anomEl = q('iv-explain-anomaly');
    anomEl.style.display = 'none';
    var askLog = q('iv-ask-log');           // Q&A belongs to ONE invoice —
    if (askLog && !regen) askLog.innerHTML = '';   // reset on a new invoice
    body.innerHTML = '<span class="spinner"></span> <span class="text-muted">' +
      __t('inv.explaining', 'Leyendo tu factura y preparando la explicación…') + '</span>';
    AppUI.openPanel('explainPanel');
    // Anomaly banner (deterministic verdict + plain narrative) in parallel.
    cfetch('/invoices/' + id + '/anomaly').then(function (a) {
      if (!a || a.status !== 'ok') return;
      var n = a.narrative || {};
      var html = '<b>' + esc(n.headline || '') + '</b>';
      if (n.causes && n.causes.length) {
        html += '<ul style="margin:6px 0 0 1.1rem;padding:0;">' + n.causes.map(function (c) {
          return '<li>' + esc(c) + '</li>';
        }).join('') + '</ul>';
      }
      if (n.advice) html += '<div style="margin-top:6px;">' + esc(n.advice) + '</div>';
      anomEl.style.cssText = ANOM_STYLE[a.level] || ANOM_STYLE.normal;
      anomEl.style.display = '';
      anomEl.style.borderRadius = '8px';
      anomEl.style.padding = '10px 12px';
      anomEl.style.fontSize = '0.85rem';
      anomEl.style.marginBottom = '0.8rem';
      anomEl.innerHTML = html;
    }).catch(function () {});
    cfetch('/invoices/' + id + '/explain' + (regen ? '?regenerate=1' : '')).then(function (r) {
      var text = (r.explanation || '').trim();
      body.innerHTML = text.split(/\n{2,}/).map(function (p) {
        return '<p style="margin:0 0 0.8rem;line-height:1.55;">' +
          mdLite(p).replace(/\n/g, '<br>') + '</p>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = '<span class="text-muted">' +
        (e.message || __t('common.error', 'Error')) + '</span>';
    });
  }
  function closeExplain() { AppUI.closePanel('explainPanel'); }

  // ── Cost-concept breakdown panel (explain-PDF palette) ──────────────────
  var COST_CONCEPTS = [
    ['energia_eur', 'inv.cEnergy', 'La energía que usaste', '#4682b4'],
    ['potencia_eur', 'inv.cPower', 'El fijo de la potencia', '#8e6cc8'],
    ['peajes_eur', 'inv.cTolls', 'Los peajes y cargos', '#f39c12'],
    ['bono_social_eur', 'inv.cBono', 'El bono social', '#2ecc71'],
    ['alquiler_eur', 'inv.cRental', 'El alquiler del contador', '#6a9bc3'],
    ['impuestos_eur', 'inv.cTaxes', 'Los impuestos', '#e74c3c'],
  ];
  function cost(id) {
    var head = q('iv-cost-head'), bar = q('iv-cost-bar'), leg = q('iv-cost-legend');
    head.innerHTML = '<span class="spinner"></span>';
    bar.innerHTML = ''; leg.innerHTML = '';
    AppUI.openPanel('costPanel');
    cfetch('/invoices/' + id + '/amounts').then(function (r) {
      var iv = (list || []).find(function (x) { return x.id === id; }) || {};
      var kwh = r.energy_kwh ||
        ((iv.energy_p1_kwh || 0) + (iv.energy_p2_kwh || 0) + (iv.energy_p3_kwh || 0)) || null;
      var allIn = (r.total_eur && kwh) ? (r.total_eur / kwh).toFixed(3).replace('.', ',') : null;
      head.innerHTML =
        '<div style="font-size:0.85rem;color:#3d5a75;">' + esc(r.period_start + ' → ' + r.period_end) + '</div>' +
        '<div style="display:flex;gap:1.4rem;align-items:baseline;flex-wrap:wrap;">' +
        '<span style="font-size:1.7rem;font-weight:700;color:#2c5171;">' + eur(r.total_eur) + '</span>' +
        (allIn ? '<span style="font-size:1rem;font-weight:600;color:#2c5171;">' + allIn +
          ' €/kWh <span class="text-muted" style="font-weight:400;font-size:0.75rem;">' +
          __t('inv.allInLabel', 'con todo incluido') + '</span></span>' : '') +
        '</div>';
      var a = r.amounts || {};
      var parts = COST_CONCEPTS.map(function (c) {
        return { label: __t(c[1], c[2]), color: c[3], v: +a[c[0]] || 0 };
      }).filter(function (p) { return p.v > 0; });
      var sum = parts.reduce(function (s, p) { return s + p.v; }, 0);
      if (!parts.length || sum <= 0) {
        leg.innerHTML = '<span class="text-muted">' +
          __t('inv.costNoData', 'No hay desglose disponible para esta factura.') + '</span>';
        return;
      }
      bar.innerHTML = parts.map(function (p) {
        return '<div style="width:' + Math.max(p.v / sum * 100, 1.5) + '%;background:' + p.color + ';" title="' +
          esc(p.label) + ' ' + eur(p.v) + '"></div>';
      }).join('');
      leg.innerHTML = parts.map(function (p) {
        return '<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:8px;' +
          'background:' + p.color + '17;margin-bottom:5px;font-size:0.85rem;">' +
          '<span style="width:10px;height:10px;border-radius:3px;background:' + p.color + ';flex:none;"></span>' +
          '<span>' + esc(p.label) + '</span>' +
          '<b style="margin-left:auto;">' + eur(p.v) + '</b>' +
          '<span class="text-muted" style="font-size:0.72rem;min-width:38px;text-align:right;">' + (p.v / sum * 100).toFixed(0) + ' %</span>' +
          '</div>';
      }).join('');
    }).catch(function (e) {
      head.innerHTML = '<span class="text-muted">' + esc(e.message || __t('common.error', 'Error')) + '</span>';
    });
  }
  function closeCost() { AppUI.closePanel('costPanel'); }

  // ── Análisis (Phase 2.5) — €-delta waterfall + PLC-vs-invoice reconciliation ─
  var RECON_VERDICT_STYLE = {
    ok: { bg: 'rgba(46,204,113,0.12)', border: 'rgba(46,204,113,0.4)', color: '#1e7e4e',
         label: __t('inv.reconOk', 'Coincide con el contador') },
    estimated_read: { bg: 'rgba(243,156,18,0.12)', border: 'rgba(243,156,18,0.45)', color: '#9c6a06',
                      label: __t('inv.reconEstimated', 'Lectura estimada (se corrige sola)') },
    meter_gap: { bg: 'rgba(231,76,60,0.12)', border: 'rgba(231,76,60,0.45)', color: '#b03024',
                label: __t('inv.reconGap', 'Diferencia con el contador') },
  };

  function analysis(id) {
    var body = q('iv-analysis-body');
    body.innerHTML = '<span class="spinner"></span> <span class="text-muted">' +
      __t('inv.analysisLoading', 'Analizando tu factura…') + '</span>';
    AppUI.openPanel('analysisPanel');
    cfetch('/invoices/' + id + '/anomaly').then(function (a) {
      var b = a.bill;
      if (!b || b.status === 'void') {
        body.innerHTML = '<span class="text-muted">' +
          __t('inv.analysisVoid', 'No hay análisis para una factura anulada.') + '</span>';
        return;
      }
      if (b.status === 'error') {
        body.innerHTML = '<span class="text-muted">' + __t('common.error', 'Error') + '</span>';
        return;
      }
      var html = '';

      // Headline: actual vs expected, delta chip.
      if (b.expected_total_eur != null) {
        var up = b.delta_eur > 0;
        var chipColor = Math.abs(b.delta_eur) < 1 ? '#6a9bc3' : (up ? '#e74c3c' : '#2ecc71');
        html += '<div class="glass-panel" style="padding:0.8rem 1rem;margin-bottom:12px;">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;">' +
          '<div><div class="text-muted" style="font-size:0.72rem;">' + esc(b.period_label || '') + '</div>' +
          '<div style="font-size:1.3rem;font-weight:700;color:#2c5171;">' + eur(b.actual_total_eur) + '</div></div>' +
          '<div style="text-align:right;">' +
          '<div class="text-muted" style="font-size:0.7rem;">' + __t('inv.analysisExpected', 'Esperado') + '</div>' +
          '<div style="font-size:1rem;color:#3d5a75;">' + eur(b.expected_total_eur) + '</div></div></div>' +
          '<div style="margin-top:8px;display:flex;align-items:center;gap:8px;">' +
          '<span style="width:10px;height:10px;border-radius:50%;background:' + chipColor + ';flex:none;"></span>' +
          '<span style="font-weight:600;color:' + chipColor + ';">' +
          (up ? '+' : '') + eur(b.delta_eur) + '</span>' +
          (b.is_anomaly ? '<span class="badge" style="background:' + chipColor + '22;color:' + chipColor + ';">' +
            __t('inv.analysisAnomaly', 'Fuera de lo esperado') + '</span>' : '') +
          (b.estimated_read ? '<span class="badge" style="background:rgba(243,156,18,0.15);color:#9c6a06;">' +
            __t('inv.reconEstimated', 'Lectura estimada (se corrige sola)') + '</span>' : '') +
          '</div></div>';
      } else {
        html += '<div class="glass-panel" style="padding:0.8rem 1rem;margin-bottom:12px;">' +
          '<span class="text-muted">' + __t('inv.analysisSparse',
            'Aún no hay histórico suficiente en este punto de suministro para comparar esta factura.') +
          '</span></div>';
      }

      // Narrative.
      if (b.narrative && b.narrative.summary) {
        html += '<p style="margin:0 0 10px;">' + esc(b.narrative.summary) + '</p>';
        if (b.narrative.advice && b.narrative.advice.length) {
          html += '<ul style="margin:0 0 14px 1.1rem;padding:0;font-size:0.88rem;">' +
            b.narrative.advice.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>';
        }
      }

      // Driver waterfall — dot + tinted chip, NEVER a border-left accent.
      if (b.drivers && b.drivers.length) {
        html += '<h4 style="margin:0 0 6px;font-size:0.8rem;" data-i18n="inv.analysisDrivers">Por qué cambió</h4>';
        html += b.drivers.map(function (d) {
          var pos = d.phi > 0;
          var c = pos ? '#e74c3c' : '#2ecc71';
          return '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;' +
            'background:' + c + '14;margin-bottom:5px;font-size:0.85rem;">' +
            '<span style="width:9px;height:9px;border-radius:50%;background:' + c + ';flex:none;"></span>' +
            '<span style="flex:1;">' + esc(d.label) + (d.detail ? '<div class="text-muted" style="font-size:0.7rem;">' +
              esc(d.detail) + '</div>' : '') + '</span>' +
            '<b style="color:' + c + ';white-space:nowrap;">' + (pos ? '+' : '') + eur(d.phi) + '</b>' +
            '</div>';
        }).join('');
      }

      // PLC-vs-invoice reconciliation panel (B5).
      var r = b.reconciliation || {};
      html += '<h4 style="margin:14px 0 6px;font-size:0.8rem;" data-i18n="inv.reconTitle">Contador vs. factura</h4>';
      if (r.status !== 'ok') {
        html += '<p class="text-muted" style="font-size:0.82rem;margin:0;">' +
          __t('inv.reconNotAvailable', 'No disponible: este punto no tiene datos del contador para este periodo.') +
          '</p>';
      } else {
        var vs = RECON_VERDICT_STYLE[r.verdict] || RECON_VERDICT_STYLE.ok;
        html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;' +
          'background:' + vs.bg + ';border:1px solid ' + vs.border + ';color:' + vs.color +
          ';margin-bottom:8px;font-size:0.85rem;">' + esc(vs.label) + '</div>';
        html += '<table class="data-table" style="font-size:0.82rem;"><thead><tr>' +
          '<th data-i18n="inv.reconPeriod">Franja</th>' +
          '<th data-i18n="inv.reconMetered">Contador</th>' +
          '<th data-i18n="inv.reconInvoiced">Factura</th>' +
          '<th data-i18n="inv.reconDelta">Diferencia</th></tr></thead><tbody>' +
          (r.by_period || []).map(function (p) {
            return '<tr><td>' + p.period + '</td><td>' + fmt(p.metered_kwh, 1) + ' kWh</td>' +
              '<td>' + (p.invoiced_kwh != null ? fmt(p.invoiced_kwh, 1) + ' kWh' : '—') + '</td>' +
              '<td>' + (p.delta_kwh != null ? (p.delta_kwh > 0 ? '+' : '') + fmt(p.delta_kwh, 1) + ' kWh'
                + (p.pct != null ? ' (' + (p.pct > 0 ? '+' : '') + p.pct.toFixed(0) + '%)' : '') : '—') + '</td></tr>';
          }).join('') +
          (r.total ? '<tr style="font-weight:700;"><td>' + __t('inv.total', 'Total') + '</td><td>' +
            fmt(r.total.metered_kwh, 1) + ' kWh</td><td>' + fmt(r.total.invoiced_kwh, 1) + ' kWh</td><td>' +
            (r.total.delta_kwh > 0 ? '+' : '') + fmt(r.total.delta_kwh, 1) + ' kWh' +
            (r.total.pct != null ? ' (' + (r.total.pct > 0 ? '+' : '') + r.total.pct.toFixed(0) + '%)' : '') +
            '</td></tr>' : '') +
          '</tbody></table>';
      }

      body.innerHTML = html;
    }).catch(function (e) {
      body.innerHTML = '<span class="text-muted">' + esc(e.message || __t('common.error', 'Error')) + '</span>';
    });
  }
  function closeAnalysis() { AppUI.closePanel('analysisPanel'); }

  // Q&A en llano sobre LA factura abierta en el panel Explicar.
  function ask() {
    var inp = q('iv-ask-input'), log = q('iv-ask-log'), btn = q('iv-ask-send');
    var question = (inp.value || '').trim();
    if (_explainId == null || question.length < 3) return;
    inp.value = '';
    var item = document.createElement('div');
    item.style.cssText = 'margin-bottom:0.7rem;font-size:0.88rem;';
    item.innerHTML = '<div style="font-weight:600;margin-bottom:2px;">' + esc(question) + '</div>' +
      '<div class="iv-ask-a"><span class="spinner"></span></div>';
    log.appendChild(item);
    item.scrollIntoView({ block: 'nearest' });
    btn.disabled = true;
    cfetch('/invoices/' + _explainId + '/ask', {
      method: 'POST', body: JSON.stringify({ question: question }),
    }).then(function (r) {
      item.querySelector('.iv-ask-a').innerHTML =
        '<div style="background:rgba(70,130,180,0.08);border-radius:8px;padding:8px 10px;line-height:1.5;">' +
        mdLite(r.answer || '').replace(/\n/g, '<br>') + '</div>';
    }).catch(function (e) {
      item.querySelector('.iv-ask-a').innerHTML =
        '<span class="text-muted">' + esc(e.message || __t('common.error', 'Error')) + '</span>';
    }).finally(function () { btn.disabled = false; inp.focus(); });
  }

  // Branded printable one-pager of the explanation → the PDF viewer panel.
  function explainPdf() {
    if (_explainId == null) return;
    var btn = q('iv-explain-pdf');
    btn.disabled = true;
    _fetchPdf('/invoices/' + _explainId + '/explain/pdf')
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        showPdf(blob, 'factura_explicada_' + _explainId + '.pdf',
          __t('inv.explainTitle', 'Tu factura, explicada'));
      })
      .catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); })
      .finally(function () { btn.disabled = false; });
  }

  // ── Edit an uploaded bill (OCR gaps: total/bands/period/cups) ────────────
  var _editId = null;
  function openEdit(id) {
    var iv = (list || []).filter(function (x) { return x.id === id; })[0];
    if (!iv) return;
    _editId = id;
    q('iv-ed-start').value = iv.period_start || '';
    q('iv-ed-end').value = iv.period_end || '';
    q('iv-ed-total').value = iv.total_eur != null && +iv.total_eur !== 0 ? iv.total_eur : '';
    q('iv-ed-p1').value = iv.energy_p1_kwh != null ? iv.energy_p1_kwh : '';
    q('iv-ed-p2').value = iv.energy_p2_kwh != null ? iv.energy_p2_kwh : '';
    q('iv-ed-p3').value = iv.energy_p3_kwh != null ? iv.energy_p3_kwh : '';
    q('iv-ed-cups').value = iv.cups || '';
    q('iv-ed-status').textContent = '';
    AppUI.openPanel('invoiceEditPanel');
  }
  function saveEdit() {
    if (_editId == null) return;
    function numOrNull(id) {
      var v = q(id).value;
      return v === '' ? null : +v;
    }
    var body = {
      period_start: q('iv-ed-start').value || null,
      period_end: q('iv-ed-end').value || null,
      total_eur: numOrNull('iv-ed-total'),
      energy_p1_kwh: numOrNull('iv-ed-p1'),
      energy_p2_kwh: numOrNull('iv-ed-p2'),
      energy_p3_kwh: numOrNull('iv-ed-p3'),
      cups: (q('iv-ed-cups').value || '').trim().toUpperCase() || null,
    };
    Object.keys(body).forEach(function (k) { if (body[k] == null) delete body[k]; });
    if (body.period_start && body.period_end && body.period_end < body.period_start) {
      q('iv-ed-status').textContent = __t('inv.errDates', 'Indica ambas fechas.');
      q('iv-ed-status').style.color = '#c0392b';
      return;
    }
    q('iv-ed-save').disabled = true;
    cfetch('/invoices/' + _editId, { method: 'PATCH', body: JSON.stringify(body) })
      .then(function () {
        AppUI.closePanel('invoiceEditPanel');
        App.showNotification(__t('inv.editSaved', 'Factura corregida'), '', 'success');
        loadAll();
      })
      .catch(function (e) {
        q('iv-ed-status').textContent = e.message;
        q('iv-ed-status').style.color = '#c0392b';
      })
      .finally(function () { q('iv-ed-save').disabled = false; });
  }

  window.IvPage = {
    onCustomerChange: function () {
      customer = q('iv-customer').value || '';
      _sumTouched = false;   // new household → summary back to its full span (or its saved dates)
      loadAll(); loadTariffCard();
    },
    openForm: openForm, closeForm: closeForm, save: save,
    detail: detail, closeDetail: function () { AppUI.closePanel('invoiceDetail'); },
    pdf: pdf, void: voidInvoice, remove: removeInvoice, presetLastMonth: presetLastMonth,
    openUpload: openUpload, closeUpload: closeUpload, readInvoice: readInvoice,
    answerType: answerType, saveTariff: saveTariff, file: fileDownload,
    openFix: openFix, closeFix: closeFix, fixType: fixType, saveFix: saveFix,
    closePdf: closePdf, togglePdfExpand: togglePdfExpand,
    explain: explain, closeExplain: closeExplain, explainPdf: explainPdf,
    ask: ask, editBpEnd: editBpEnd, saveBpEnd: saveBpEnd, setRange: setRange,
    cost: cost, closeCost: closeCost, renderSummary: renderSummary,
    sumDatesChanged: sumDatesChanged,
    analysis: analysis, closeAnalysis: closeAnalysis,
    edit: openEdit, closeEdit: function () { AppUI.closePanel('invoiceEditPanel'); },
    saveEdit: saveEdit,
  };

  document.addEventListener('i18n:changed', renderTable);

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      loadContext().then(function () {
        loadTariffCard();
        return loadAll();
      }).catch(function (e) {
        App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
      });
    });
  });
})();
