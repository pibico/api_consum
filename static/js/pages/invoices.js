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
      tb.innerHTML = '<tr><td colspan="7" class="text-center text-muted">' +
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
      if (uploaded) {
        // The user's own document — always downloadable, no PRO gate.
        actions = explainBtn +
          '<button class="btn btn-sm pnl-day-btn" onclick="IvPage.file(' + iv.id + ')">PDF</button>';
      } else {
        actions = explainBtn +
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
      return '<tr' + (voided ? ' style="opacity:0.55;"' : '') + '>' +
        '<td>' + iv.period_start + ' → ' + iv.period_end + '</td>' +
        '<td>' + cups + '</td>' +
        '<td>' + daysBetween(iv.period_start, iv.period_end) + '</td>' +
        '<td>' + (uploaded && !iv.energy_kwh ? '—' : fmt(iv.energy_kwh, 1) + ' kWh') + '</td>' +
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
    loadBillingPeriod();
    return cfetch('/invoices' + custQS()).then(function (r) { list = r.values || []; })
      .catch(function () { list = []; })
      .then(renderTable);
  }

  // ── "Factura en curso" KPIs — the OPEN billing period, anchored on the
  // invoice history (cycle = median bill length) + projection to close.
  // PRO endpoint: hide the row quietly on 403/basic (cfetch, never logout).
  function loadBillingPeriod() {
    var row = q('iv-bp-row');
    if (!row) return;
    cfetch('/invoices/billing-period' + custQS()).then(function (bp) {
      if (!bp || bp.status !== 'ok') { row.style.display = 'none'; return; }
      row.style.display = '';
      q('iv-bp-period').textContent = bp.period_start + ' → ~' + bp.expected_end;
      q('iv-bp-days').textContent = __t('inv.bpDay', 'día {n} de ~{m}')
        .replace('{n}', bp.days_elapsed).replace('{m}', bp.days_total);
      q('iv-bp-acc').textContent = eur(bp.total_eur);
      q('iv-bp-acc-kwh').textContent = fmt(bp.energy_kwh, 1) + ' kWh';
      q('iv-bp-proj').textContent = bp.projected_eur != null ? '~' + eur(bp.projected_eur) : '—';
      q('iv-bp-proj-sub').textContent = bp.eur_day != null
        ? eur(bp.eur_day) + '/' + __t('inv.bpPerDay', 'día') + ' · ' + __t('inv.bpEstimate', 'estimado')
        : __t('inv.bpTooEarly', 'aún pocos días para estimar');
    }).catch(function () { row.style.display = 'none'; });
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
      if (gateways.length > 1) {
        sel.innerHTML = gateways.map(function (d) {
          return '<option value="' + d.customer + '">' + (d.hostname || d.customer) + '</option>';
        }).join('');
        sel.style.display = '';
        customer = gateways[0].customer;
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
    return cfetch('/contracts/active' + custQS()).then(function (r) {
      activeContract = r.contract || null;
      var el = q('iv-tariff-body');
      if (!el) return;
      if (!activeContract) {
        el.innerHTML = '<span class="text-muted">' + __t('inv.noTariff',
          'Aún no sabemos tu tarifa. Sube tu última factura y lo configuramos por ti — tus números en € pasarán a ser los de verdad.') + '</span>';
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
      el.innerHTML = '<div style="font-size:0.9rem;">' + bits.join(' · ') + now + '</div>';
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
    }).catch(function () { /* archiving is best-effort — tariff flow continues */ });
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
        setUpStatus('iv-up-save-status', __t('inv.errNoPrice',
          'No pudimos leer tu precio en el documento — usa el editor avanzado.'), 'err');
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

  function explain(id) {
    _explainId = id;
    var body = q('iv-explain-body');
    var anomEl = q('iv-explain-anomaly');
    anomEl.style.display = 'none';
    var askLog = q('iv-ask-log');           // Q&A belongs to ONE invoice —
    if (askLog) askLog.innerHTML = '';      // reset when another one opens
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
    cfetch('/invoices/' + id + '/explain').then(function (r) {
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
        '<div style="background:rgba(70,130,180,0.08);border-left:3px solid #4682b4;border-radius:0 8px 8px 0;padding:8px 10px;line-height:1.5;">' +
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

  window.IvPage = {
    onCustomerChange: function () { customer = q('iv-customer').value || ''; loadAll(); loadTariffCard(); },
    openForm: openForm, closeForm: closeForm, save: save,
    detail: detail, closeDetail: function () { AppUI.closePanel('invoiceDetail'); },
    pdf: pdf, void: voidInvoice, presetLastMonth: presetLastMonth,
    openUpload: openUpload, closeUpload: closeUpload, readInvoice: readInvoice,
    answerType: answerType, saveTariff: saveTariff, file: fileDownload,
    openFix: openFix, closeFix: closeFix, fixType: fixType, saveFix: saveFix,
    closePdf: closePdf, togglePdfExpand: togglePdfExpand,
    explain: explain, closeExplain: closeExplain, explainPdf: explainPdf,
    ask: ask,
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
