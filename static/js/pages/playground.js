/**
 * playground.js — Playground de extracción de facturas (SOLO superadmin).
 * Endpoint: POST /playground/extract — pipeline compartido con /contracts/extract
 * (ContractSkill + InvoiceSkill) pero SIN el guard 422 y SIN persistir nada.
 * El historial de pruebas vive SOLO en memoria de esta página (array `history`) —
 * se pierde al recargar, a propósito: nada toca sessionStorage/localStorage/BD.
 */
(function () {
  'use strict';

  var history = [];       // [{id, retailer, kind, confidence, ts, extracted, amounts, markdown, meta, warning}]
  var activeId = null;
  var lastMarkdown = '';

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? null : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 4 : dec }); }

  /** Same auth headers as App.apiFetch — a bare fetch because this is
   * multipart (upfetch) or we want 403 to render inline, not log the user out. */
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
          var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
          err.status = r.status;
          throw err;
        }
        return body;
      });
    });
  }

  function setStatus(msg, kind) {
    var el = q('pg-status');
    el.textContent = msg || '';
    el.style.color = kind === 'err' ? '#c0392b' : (kind === 'busy' ? '#2c5171' : '');
  }

  // ── Field definitions (label i18n key + optional formatter) ──────────────
  var TARIFF_FIELDS = [
    ['retailer', 'pg.fRetailer'],
    ['product_name', 'pg.fProduct'],
    ['contract_type', 'pg.fType'],
    ['access_tariff', 'pg.fAccessTariff'],
    ['energy_p1_eur_kwh', 'pg.fEp1'],
    ['energy_p2_eur_kwh', 'pg.fEp2'],
    ['energy_p3_eur_kwh', 'pg.fEp3'],
    ['margin_eur_kwh', 'pg.fMargin'],
    ['passthru_p1_eur_kwh', 'pg.fPt1'],
    ['passthru_p2_eur_kwh', 'pg.fPt2'],
    ['passthru_p3_eur_kwh', 'pg.fPt3'],
    ['power_p1_eur_kw_day', 'pg.fPwp1'],
    ['power_p2_eur_kw_day', 'pg.fPwp2'],
    ['meter_rental_eur_month', 'pg.fRental'],
    ['cups', 'pg.fCups'],
    ['power_p1_kw', 'pg.fPw1'],
    ['power_p2_kw', 'pg.fPw2'],
    ['start_date', 'pg.fStartDate'],
    ['components', 'pg.fComponents'],
    ['billing_period_start', 'pg.fBillStart'],
    ['billing_period_end', 'pg.fBillEnd'],
    ['total_eur', 'pg.fTotalEur'],
    ['notes', 'pg.fNotes'],
  ];
  var AMOUNT_FIELDS = [
    ['energia_eur', 'pg.aEnergia'],
    ['potencia_eur', 'pg.aPotencia'],
    ['peajes_eur', 'pg.aPeajes'],
    ['bono_social_eur', 'pg.aBonoSocial'],
    ['impuestos_eur', 'pg.aImpuestos'],
    ['alquiler_eur', 'pg.aAlquiler'],
    ['kwh_horas_caras', 'pg.aKwhCaras'],
    ['kwh_horas_normales', 'pg.aKwhNormales'],
    ['kwh_horas_baratas', 'pg.aKwhBaratas'],
    ['precio_horas_caras', 'pg.aPrecioCaras'],
    ['precio_horas_normales', 'pg.aPrecioNormales'],
    ['precio_horas_baratas', 'pg.aPrecioBaratas'],
  ];
  var EUR_KEYS = ['energy_p1_eur_kwh', 'energy_p2_eur_kwh', 'energy_p3_eur_kwh', 'margin_eur_kwh',
    'passthru_p1_eur_kwh', 'passthru_p2_eur_kwh', 'passthru_p3_eur_kwh', 'power_p1_eur_kw_day',
    'power_p2_eur_kw_day', 'meter_rental_eur_month', 'total_eur',
    'energia_eur', 'potencia_eur', 'peajes_eur', 'bono_social_eur', 'impuestos_eur', 'alquiler_eur',
    'precio_horas_caras', 'precio_horas_normales', 'precio_horas_baratas'];

  function cellValue(key, v) {
    if (v === null || v === undefined || v === '') {
      return '<td class="pg-null">—</td>';
    }
    if (key === 'components' && typeof v === 'object') {
      return '<td><span class="mono" style="font-size:0.68rem;">' +
        JSON.stringify(v).replace(/</g, '&lt;') + '</span></td>';
    }
    if (EUR_KEYS.indexOf(key) !== -1) {
      return '<td>' + fmt(v, key.indexOf('kwh') === -1 && key.indexOf('eur_kwh') === -1 ? 2 : 6) +
        (key.indexOf('kwh') !== -1 && key.indexOf('eur_kwh') === -1 ? ' kWh' : ' €') + '</td>';
    }
    return '<td>' + String(v) + '</td>';
  }

  function renderFieldTable(tbodyId, fields, data) {
    var rows = fields.map(function (f) {
      var key = f[0], labelKey = f[1];
      return '<tr><td>' + __t(labelKey, key) + '</td>' + cellValue(key, data ? data[key] : null) + '</tr>';
    });
    q(tbodyId).innerHTML = rows.join('');
  }

  function confidenceBadge(conf) {
    var el = q('pg-conf-badge');
    if (conf === null || conf === undefined) {
      el.className = 'pg-conf-badge pg-conf-na';
      el.textContent = __t('pg.confNa', 'sin confianza');
      return;
    }
    var pct = Math.round(conf * 100);
    var cls = conf >= 0.8 ? 'pg-conf-hi' : (conf >= 0.5 ? 'pg-conf-mid' : 'pg-conf-lo');
    el.className = 'pg-conf-badge ' + cls;
    el.textContent = pct + '%';
  }

  function renderResult(entry) {
    lastMarkdown = entry.markdown || '';
    q('pg-results').style.display = '';
    renderFieldTable('pg-tariff-tbody', TARIFF_FIELDS, entry.extracted);
    renderFieldTable('pg-amounts-tbody', AMOUNT_FIELDS, entry.amounts);
    confidenceBadge(entry.extracted ? entry.extracted.confidence : null);

    var kindBadge = q('pg-kind-badge');
    var kind = entry.extracted ? entry.extracted.document_kind : null;
    if (kind) { kindBadge.style.display = ''; kindBadge.textContent = kind; }
    else { kindBadge.style.display = 'none'; }

    // Amounts sum vs. total_eur contrast
    var sumEl = q('pg-amounts-sum');
    if (entry.amounts) {
      var sum = ['energia_eur', 'potencia_eur', 'peajes_eur', 'bono_social_eur', 'impuestos_eur', 'alquiler_eur']
        .reduce(function (acc, k) { return acc + (entry.amounts[k] || 0); }, 0);
      var total = entry.extracted ? entry.extracted.total_eur : null;
      var txt = __t('pg.aSum', 'Suma de importes') + ': ' + fmt(sum, 2) + ' €';
      if (total != null) {
        var diff = sum - total;
        txt += ' · ' + __t('pg.aContrast', 'total factura') + ': ' + fmt(total, 2) + ' € (' +
          (diff >= 0 ? '+' : '') + fmt(diff, 2) + ' €)';
      }
      sumEl.textContent = txt;
    } else {
      sumEl.textContent = '';
    }

    var warnEl = q('pg-warning');
    if (entry.warning) { warnEl.style.display = ''; warnEl.textContent = entry.warning; }
    else { warnEl.style.display = 'none'; warnEl.textContent = ''; }

    var metaEl = q('pg-meta');
    var metaText = q('pg-meta-text');
    if (entry.meta) {
      metaEl.style.display = 'flex';
      metaText.textContent = (entry.meta.model || '—') + ' · ' +
        __t('pg.metaTime', 'tiempo') + ': ' + entry.meta.elapsed_ms + ' ms';
    } else {
      metaEl.style.display = 'none';
    }
  }

  function renderHistory() {
    var list = q('pg-history-list');
    if (!history.length) {
      list.innerHTML = '<p class="text-muted" style="font-size:0.75rem;">' +
        __t('pg.historyEmpty', 'Aún no has probado ningún documento.') + '</p>';
      return;
    }
    list.innerHTML = history.map(function (h) {
      var conf = h.extracted && h.extracted.confidence != null ? Math.round(h.extracted.confidence * 100) + '%' : '—';
      var retailer = (h.extracted && h.extracted.retailer) || __t('pg.histUnknown', 'sin identificar');
      var kind = (h.extracted && h.extracted.document_kind) || '';
      return '<button type="button" class="pg-hist-item' + (h.id === activeId ? ' active' : '') +
        '" onclick="PgPage.showHistory(' + h.id + ')">' +
        retailer + '<small>' + [kind, conf].filter(Boolean).join(' · ') + ' · ' + h.ts + '</small></button>';
    }).join('');
  }

  function readDocument() {
    var file = q('pg-file').files[0];
    var text = q('pg-text').value.trim();
    if (!file && !text) {
      setStatus(__t('pg.upNeed', 'Sube un archivo o pega el texto a probar.'), 'err');
      return;
    }
    var fd = new FormData();
    if (file) fd.append('file', file);
    if (text) fd.append('text', text);
    fd.append('use_vlm', q('pg-vlm').checked ? 'true' : 'false');
    q('pg-read-btn').disabled = true;
    setStatus(__t('pg.upReading', 'Leyendo el documento con IA… puede tardar unos segundos.'), 'busy');
    upfetch('/playground/extract', fd).then(function (r) {
      var entry = {
        id: Date.now(),
        ts: new Date().toLocaleTimeString(),
        extracted: r.extracted || {},
        amounts: r.amounts || null,
        markdown: r.markdown || '',
        meta: r.meta || null,
        warning: r.warning || null,
      };
      history.unshift(entry);
      activeId = entry.id;
      renderResult(entry);
      renderHistory();
      setStatus('', '');
    }).catch(function (e) {
      setStatus(e.message || __t('common.error', 'Error'), 'err');
    }).finally(function () { q('pg-read-btn').disabled = false; });
  }

  function showHistory(id) {
    var entry = history.find(function (h) { return h.id === id; });
    if (!entry) return;
    activeId = id;
    renderResult(entry);
    renderHistory();
  }

  function reset() {
    q('pg-file').value = '';
    q('pg-text').value = '';
    q('pg-vlm').checked = false;
    setStatus('', '');
    q('pg-results').style.display = 'none';
    q('pg-warning').style.display = 'none';
    q('pg-meta').style.display = 'none';
    activeId = null;
    renderHistory();
  }

  function openMarkdown() {
    q('pg-md-content').textContent = lastMarkdown || __t('pg.mdEmpty', '(sin contenido)');
    AppUI.openPanel('pgMdPanel');
  }
  function closeMarkdown() { AppUI.closePanel('pgMdPanel'); }

  window.PgPage = {
    readDocument: readDocument,
    showHistory: showHistory,
    reset: reset,
    openMarkdown: openMarkdown,
    closeMarkdown: closeMarkdown,
  };

  document.addEventListener('i18n:changed', function () {
    if (activeId !== null) showHistory(activeId);
    else renderHistory();
  });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () { /* page is superadmin-gated server-side; nothing else to bootstrap */ });
  });
})();
