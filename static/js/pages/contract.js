/**
 * contract.js — Entidad contrato: active-contract card, PRO bill estimate,
 * history table and the create/edit slide-panel form.
 * Endpoints: /contracts (CRUD), /contracts/active, /contracts/bill-estimate.
 * Writes need role editor+ (server-enforced; buttons hidden below that).
 */
(function () {
  'use strict';

  var ctx = null;          // /consumption/context payload
  var customer = '';       // '' = caller's scope (single home resolves itself)
  var list = [];           // /contracts values
  var active = null;       // /contracts/active payload
  var editingId = null;

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 2 : dec }); }

  /** apiFetch clone that only logs out on 401 — App.apiFetch treats 403 as a
   * dead session, but here 403 is a ROLE/TIER answer we must show, not a
   * reason to boot the user. */
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
          err.status = r.status;
          err.code = d && d.code;
          throw err;
        }
        return body;
      });
    });
  }

  function custQS(sep) { return customer ? ((sep || '?') + 'customer=' + encodeURIComponent(customer)) : ''; }
  function canWrite() {
    // Self-service (decision 2026-07-12): any authenticated household member
    // manages their own supply contract. Backend confines writes via check_slug.
    return !!ctx;
  }
  function canDelete() {
    // Self-service (2026-07-12): a member can remove an incorrect contract of
    // their own home (backend still confines via check_slug).
    return canWrite();
  }

  var TYPE_LABEL = {
    pvpc: 'PVPC',
    fixed: function () { return __t('ct.tFixed', 'Precio fijo'); },
    indexed: function () { return __t('ct.tIndexed', 'Indexado'); },
  };
  function typeLabel(t) {
    var v = TYPE_LABEL[t];
    return typeof v === 'function' ? v() : (v || t);
  }

  // ── Active contract card ─────────────────────────────────────────────
  function renderActive() {
    var body = q('ct-active-body');
    var badge = q('ct-type-badge');
    var c = active && active.contract;
    q('ct-edit-btn').style.display = (c && canWrite()) ? '' : 'none';
    if (!c) {
      badge.style.display = 'none';
      body.innerHTML = '<p class="text-muted" style="margin:0;">' +
        __t('ct.noContract', 'Sin contrato dado de alta — tu coste se calcula con PVPC (tarifa regulada). Da de alta tu contrato para ver tu precio real.') +
        '</p>' + (canWrite()
          ? '<button class="btn btn-sm btn-primary" style="margin-top:10px;" onclick="CtPage.openForm()">' +
            __t('ct.newBtn', 'Nuevo contrato') + '</button>' : '');
      return;
    }
    badge.textContent = typeLabel(c.contract_type);
    badge.className = 'badge ' + (c.contract_type === 'pvpc' ? 'badge-info' : 'badge-ok');
    badge.style.display = '';
    var pn = active.price_now;
    var rows = [];
    if (c.label || c.retailer) {
      rows.push([__t('ct.colRetailer', 'Comercializadora'),
        [c.label, c.retailer].filter(Boolean).join(' · ')]);
    }
    rows.push([__t('ct.validity', 'Vigencia'),
      c.start_date + ' → ' + (c.end_date || __t('ct.current', 'vigente'))]);
    if (c.contract_type === 'fixed') {
      rows.push([__t('ct.colPrices', 'Precios energía'),
        'P1 ' + fmt(c.energy_p1_eur_kwh, 6) +
        (c.energy_p2_eur_kwh != null ? ' · P2 ' + fmt(c.energy_p2_eur_kwh, 6) : '') +
        (c.energy_p3_eur_kwh != null ? ' · P3 ' + fmt(c.energy_p3_eur_kwh, 6) : '') + ' €/kWh']);
    } else if (c.contract_type === 'indexed') {
      rows.push([__t('ct.fMargin', 'Margen'), fmt(c.margin_eur_kwh, 6) + ' €/kWh ' +
        __t('ct.overOmie', 'sobre OMIE')]);
      if (c.passthru_p1_eur_kwh || c.passthru_p2_eur_kwh || c.passthru_p3_eur_kwh) {
        rows.push([__t('ct.passthru', 'Peajes+cargos'),
          'P1 ' + fmt(c.passthru_p1_eur_kwh, 6) + ' · P2 ' + fmt(c.passthru_p2_eur_kwh, 6) +
          ' · P3 ' + fmt(c.passthru_p3_eur_kwh, 6) + ' €/kWh']);
      }
    }
    if (c.power_p1_kw != null) {
      rows.push([__t('ct.colPower', 'Potencia'),
        'P1 ' + fmt(c.power_p1_kw, 3) + ' kW' +
        (c.power_p2_kw != null ? ' · P2 ' + fmt(c.power_p2_kw, 3) + ' kW' : '')]);
    }
    rows.push([__t('ct.taxes', 'Impuestos'),
      'IEE ' + fmt(c.electricity_tax_pct, 3) + '% · IVA ' + fmt(c.vat_pct, 0) + '%']);
    if (pn && pn.price_eur_kwh != null) {
      rows.push([__t('app.priceNow', 'Precio ahora'),
        '<b>' + fmt(pn.price_eur_kwh, 5) + ' €/kWh</b>' +
        (pn.period ? ' (' + pn.period + ')' : '')]);
    }
    body.innerHTML = '<table style="width:100%;font-size:0.82rem;border-collapse:collapse;">' +
      rows.map(function (r) {
        return '<tr><td class="text-muted" style="padding:2px 8px 2px 0;white-space:nowrap;vertical-align:top;">' +
          r[0] + '</td><td style="padding:2px 0;">' + r[1] + '</td></tr>';
      }).join('') + '</table>';
  }

  // ── Bill estimate (PRO) ──────────────────────────────────────────────
  function loadBill() {
    var body = q('ct-bill-body');
    var m = q('ct-bill-month').value;
    if (!m) return;
    if (!active || !active.contract) {
      body.innerHTML = '<span class="text-muted">' +
        __t('ct.billNoContract', 'Da de alta tu contrato para estimar la factura completa.') + '</span>';
      return;
    }
    body.innerHTML = '<span class="spinner"></span>';
    cfetch('/contracts/bill-estimate?month=' + m + custQS('&')).then(function (r) {
      if (r.status === 'no_contract') {
        body.innerHTML = '<span class="text-muted">' +
          __t('ct.billNoContract', 'Da de alta tu contrato para estimar la factura completa.') + '</span>';
        return;
      }
      var lines = [
        [__t('ct.blEnergy', 'Energía'), r.energy_eur, r.energy_kwh + ' kWh'],
        ['&nbsp;&nbsp;P1', r.energy.P1.eur, r.energy.P1.kwh + ' kWh'],
        ['&nbsp;&nbsp;P2', r.energy.P2.eur, r.energy.P2.kwh + ' kWh'],
        ['&nbsp;&nbsp;P3', r.energy.P3.eur, r.energy.P3.kwh + ' kWh'],
        [__t('ct.blPower', 'Potencia'), r.power_eur, r.days + ' ' + __t('ct.days', 'días')],
        [__t('ct.blFixed', 'Alquiler y otros fijos'), r.fixed_eur, ''],
        ['IEE', r.iee_eur, ''],
        ['IVA', r.vat_eur, ''],
      ];
      body.innerHTML =
        '<table style="width:100%;font-size:0.82rem;border-collapse:collapse;">' +
        lines.map(function (l) {
          return '<tr><td style="padding:2px 0;">' + l[0] + '</td>' +
            '<td style="text-align:right;padding:2px 0;">' + fmt(l[1], 2) + ' €</td>' +
            '<td class="text-muted" style="text-align:right;padding:2px 0 2px 10px;white-space:nowrap;">' + l[2] + '</td></tr>';
        }).join('') +
        '<tr style="border-top:1px solid rgba(0,0,0,0.15);font-weight:700;">' +
        '<td style="padding:4px 0;">Total</td>' +
        '<td style="text-align:right;padding:4px 0;">' + fmt(r.total_eur, 2) + ' €</td>' +
        '<td class="text-muted" style="text-align:right;padding:4px 0 4px 10px;">' +
        (r.avg_eur_kwh != null ? fmt(r.avg_eur_kwh, 4) + ' €/kWh' : '') + '</td></tr>' +
        '</table>' +
        (r.uncosted_kwh ? '<p class="text-muted" style="margin:4px 0 0;font-size:0.7rem;">' +
          __t('ct.uncosted', 'kWh sin precio (huecos de mercado):') + ' ' + fmt(r.uncosted_kwh, 2) + '</p>' : '');
    }).catch(function (e) {
      body.innerHTML = '<span class="text-muted">' +
        (e.code === 'TIER_REQUIRED'
          ? __t('ct.billProOnly', 'La estimación de factura completa requiere el plan PRO.')
          : e.message) + '</span>';
    });
  }

  // ── History table ────────────────────────────────────────────────────
  function renderTable() {
    var tb = q('ct-tbody');
    q('ct-history-count').textContent = list.length ? list.length : '';
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="8" class="text-center text-muted">' +
        __t('common.noData', 'Sin datos') + '</td></tr>';
      return;
    }
    tb.innerHTML = list.map(function (c) {
      var prices = c.contract_type === 'fixed'
        ? ['P1 ' + fmt(c.energy_p1_eur_kwh, 4),
           c.energy_p2_eur_kwh != null ? 'P2 ' + fmt(c.energy_p2_eur_kwh, 4) : null,
           c.energy_p3_eur_kwh != null ? 'P3 ' + fmt(c.energy_p3_eur_kwh, 4) : null]
          .filter(Boolean).join(' · ')
        : c.contract_type === 'indexed'
          ? 'OMIE + ' + fmt(c.margin_eur_kwh, 4)
          : 'PVPC';
      var power = c.power_p1_kw != null
        ? fmt(c.power_p1_kw, 1) + (c.power_p2_kw != null ? '/' + fmt(c.power_p2_kw, 1) : '') + ' kW'
        : '—';
      return '<tr>' +
        '<td><span class="badge ' + (c.contract_type === 'pvpc' ? 'badge-info' : 'badge-ok') + '">' +
        typeLabel(c.contract_type) + '</span></td>' +
        '<td>' + (c.label || '—') + '</td>' +
        '<td>' + (c.retailer || '—') + '</td>' +
        '<td>' + c.start_date + '</td>' +
        '<td>' + (c.end_date || __t('ct.current', 'vigente')) + '</td>' +
        '<td>' + prices + '</td>' +
        '<td>' + power + '</td>' +
        '<td style="text-align:right;">' + (canWrite()
          ? '<button class="btn btn-sm pnl-day-btn" onclick="CtPage.openForm(true,' + c.id + ')">' +
            __t('ct.editBtn', 'Editar') + '</button>' : '') + '</td>' +
        '</tr>';
    }).join('');
  }

  // ── Indexed price components (api_exo parity) ─────────────────────────
  var COMP_IDS = ['SA', 'CR', 'DSV', 'PP', 'CC', 'IM', 'ATR', 'BS'];
  var BANDS = ['P1', 'P2', 'P3'];
  var COMP_UNIT = { SA: '€/kWh', CR: '€/kWh', DSV: '€/kWh', PP: '%', CC: '€/kWh', IM: '%', ATR: '€/kWh', BS: '€/kWh' };
  var COMP_SRC = { SA: 'BOE', CR: 'BOE', DSV: 'BOE', PP: 'REE', CC: 'contrato', IM: 'ayto', ATR: 'BOE', BS: 'BOE' };
  function defaultComponents() {
    return {
      SA: { P1: 0.0042, P2: 0.0038, P3: 0.0035 }, CR: { P1: 0.0161, P2: 0.0102, P3: 0.0039 },
      DSV: { P1: 0.0021, P2: 0.0021, P3: 0.0021 }, PP: { P1: 0.03, P2: 0.03, P3: 0.03 },
      CC: { P1: 0.06, P2: 0.04, P3: 0.03 }, IM: { P1: 0.01051, P2: 0.01051, P3: 0.01051 },
      ATR: { P1: 0.0275, P2: 0.0167, P3: 0.0010 }, BS: { P1: 0.00378, P2: 0.00378, P3: 0.00378 },
    };
  }
  // Regulated base from api_exo (SSOT) — fetched once; the matrix layers
  // fallback defaults ← api_exo base ← the contract's own values.
  var compBase = null;
  function loadCompBase() {
    if (compBase) return Promise.resolve(compBase);
    return cfetch('/contracts/components-base').then(function (r) {
      compBase = r; return r;
    }).catch(function () { return null; });
  }
  // IVA/IEE defaults for NEW contracts come from the SSOT scalars (they change
  // by decree: IVA 21↔10↔5%, IEE 5.11↔0.5557%) — fallback to legacy defaults.
  function taxDefaults() {
    var c = compBase && compBase.components;
    var iva = (c && c.IVA && c.IVA.bands && c.IVA.bands.ALL != null) ? c.IVA.bands.ALL * 100 : 21;
    var iee = (c && c.IEE && c.IEE.bands && c.IEE.bands.ALL != null) ? c.IEE.bands.ALL * 100 : 5.11269;
    return { vat: Math.round(iva * 100) / 100, iee: Math.round(iee * 100000) / 100000 };
  }
  function buildCompMatrix(comps) {
    var d = defaultComponents();
    var baseComps = (compBase && compBase.components) || {};
    COMP_IDS.forEach(function (cid) {
      var bb = baseComps[cid] && baseComps[cid].bands;
      if (bb) BANDS.forEach(function (b) {
        if (bb[b] != null) d[cid][b] = Number(bb[b]);
      });
    });
    if (comps) COMP_IDS.forEach(function (cid) {
      if (comps[cid]) BANDS.forEach(function (b) {
        if (comps[cid][b] != null) d[cid][b] = Number(comps[cid][b]);
      });
    });
    var thead = '<tr><th style="text-align:left;padding:3px;">' + __t('ct.compComp', 'Componente') + '</th>';
    BANDS.forEach(function (b) { thead += '<th style="padding:3px;">' + b + '</th>'; });
    thead += '<th style="padding:3px;">' + __t('ct.compUnit', 'Ud') + '</th><th style="text-align:left;padding:3px;">' + __t('ct.compSrc', 'Fuente') + '</th></tr>';
    q('ct-comp-thead').innerHTML = thead;
    q('ct-comp-tbody').innerHTML = COMP_IDS.map(function (cid) {
      var cells = BANDS.map(function (b) {
        var hl = cid === 'CC' ? 'background:rgba(70,130,180,0.10);' : '';
        return '<td style="padding:2px;' + hl + '"><input type="number" step="0.00001" min="0" ' +
          'style="width:72px;font-size:0.7rem;padding:1px 3px;border:1px solid rgba(0,0,0,0.15);border-radius:3px;" ' +
          'data-cid="' + cid + '" data-band="' + b + '" value="' + d[cid][b] + '"></td>';
      }).join('');
      return '<tr><td style="padding:2px;font-weight:600;">' + cid + '</td>' + cells +
        '<td style="padding:2px;">' + COMP_UNIT[cid] + '</td>' +
        '<td style="padding:2px;color:var(--color-text-light);">' + COMP_SRC[cid] + '</td></tr>';
    }).join('');
  }
  function readCompMatrix() {
    var out = {};
    document.querySelectorAll('#ct-comp-tbody input[data-cid]').forEach(function (el) {
      var cid = el.dataset.cid, b = el.dataset.band;
      (out[cid] = out[cid] || {})[b] = el.value === '' ? 0 : Number(el.value);
    });
    return out;
  }

  // ── Form ─────────────────────────────────────────────────────────────
  var formType = 'pvpc';
  var formComponents = null;   // components to seed the indexed matrix with

  function setType(t) {
    formType = t;
    ['pvpc', 'fixed', 'indexed'].forEach(function (k) {
      q('ct-t-' + k).classList.toggle('active', k === t);
    });
    q('ct-g-fixed').style.display = t === 'fixed' ? '' : 'none';
    q('ct-g-indexed').style.display = t === 'indexed' ? '' : 'none';
    if (t === 'indexed') {
      buildCompMatrix(formComponents);                       // instant (fallback)
      loadCompBase().then(function () { buildCompMatrix(formComponents); });  // re-seed from api_exo
    }
    q('ct-type-help').textContent = t === 'pvpc'
      ? __t('ct.helpPvpc', 'Tarifa regulada: el precio horario lo publica ESIOS; no hay nada más que configurar en energía.')
      : t === 'fixed'
        ? __t('ct.helpFixed', 'Precio pactado por periodo tarifario (o único). Los findes y festivos son P3.')
        : __t('ct.helpIndexed', 'Pagas el mercado mayorista (OMIE) hora a hora más el margen de tu comercializadora.');
  }

  function num(id) {
    var v = q(id).value;
    return v === '' ? null : Number(v);
  }

  function fillForm(c) {
    q('ct-f-label').value = c ? (c.label || '') : '';
    q('ct-f-retailer').value = c ? (c.retailer || '') : '';
    // LOCAL date — toISOString() is UTC (serves yesterday 00:00-02:00 CEST)
    var d = new Date();
    var todayLocal = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
    q('ct-f-start').value = c ? c.start_date : todayLocal;
    q('ct-f-end').value = (c && c.end_date) || '';
    q('ct-f-cups').value = c ? (c.cups || '') : '';
    q('ct-f-ep1').value = c && c.energy_p1_eur_kwh != null ? c.energy_p1_eur_kwh : '';
    q('ct-f-ep2').value = c && c.energy_p2_eur_kwh != null ? c.energy_p2_eur_kwh : '';
    q('ct-f-ep3').value = c && c.energy_p3_eur_kwh != null ? c.energy_p3_eur_kwh : '';
    // Indexed components: seed the matrix from the stored components, or — for a
    // legacy contract — from its single margin as CC.P1. setType() builds it.
    formComponents = c && c.components ? c.components
      : (c && c.margin_eur_kwh != null ? { CC: { P1: c.margin_eur_kwh } } : null);
    q('ct-f-pw1').value = c && c.power_p1_kw != null ? c.power_p1_kw : '';
    q('ct-f-pw2').value = c && c.power_p2_kw != null ? c.power_p2_kw : '';
    q('ct-f-pwp1').value = c && c.power_p1_eur_kw_day != null ? c.power_p1_eur_kw_day : '';
    q('ct-f-pwp2').value = c && c.power_p2_eur_kw_day != null ? c.power_p2_eur_kw_day : '';
    q('ct-f-rental').value = c ? c.meter_rental_eur_month : 0.81;
    q('ct-f-other').value = c ? c.other_fixed_eur_month : 0;
    q('ct-f-iee').value = c ? c.electricity_tax_pct : taxDefaults().iee;
    q('ct-f-vat').value = c ? c.vat_pct : taxDefaults().vat;
    q('ct-f-notes').value = c ? (c.notes || '') : '';
    setType(c ? c.contract_type : 'pvpc');
  }

  function openForm(edit, id) {
    q('ct-form-error').textContent = '';
    q('ct-review-note').style.display = 'none';
    var c = null;
    if (edit) {
      c = id != null
        ? list.filter(function (x) { return x.id === id; })[0]
        : (active && active.contract);
    }
    editingId = c ? c.id : null;
    q('ct-form-title').textContent = c
      ? __t('ct.formEditTitle', 'Editar contrato')
      : __t('ct.formNewTitle', 'Nuevo contrato');
    q('ct-delete-btn').style.display = (c && canDelete()) ? '' : 'none';
    fillForm(c);
    AppUI.openPanel('contractPanel');
  }

  function closeForm() { AppUI.closePanel('contractPanel'); }

  function payload() {
    var p = {
      contract_type: formType,
      label: q('ct-f-label').value.trim() || null,
      retailer: q('ct-f-retailer').value.trim() || null,
      cups: q('ct-f-cups').value.trim() || null,
      access_tariff: '2.0TD',
      start_date: q('ct-f-start').value,
      end_date: q('ct-f-end').value || null,
      power_p1_kw: num('ct-f-pw1'),
      power_p2_kw: num('ct-f-pw2'),
      power_p1_eur_kw_day: num('ct-f-pwp1'),
      power_p2_eur_kw_day: num('ct-f-pwp2'),
      meter_rental_eur_month: num('ct-f-rental') != null ? num('ct-f-rental') : 0.81,
      other_fixed_eur_month: num('ct-f-other') || 0,
      electricity_tax_pct: num('ct-f-iee') != null ? num('ct-f-iee') : 5.11269,
      vat_pct: num('ct-f-vat') != null ? num('ct-f-vat') : 21,
      notes: q('ct-f-notes').value.trim() || null,
    };
    if (formType === 'fixed') {
      p.energy_p1_eur_kwh = num('ct-f-ep1');
      p.energy_p2_eur_kwh = num('ct-f-ep2');
      p.energy_p3_eur_kwh = num('ct-f-ep3');
    } else if (formType === 'indexed') {
      p.components = readCompMatrix();
      // margin_eur_kwh kept for back-compat readers = CC punta (P1)
      p.margin_eur_kwh = (p.components.CC && p.components.CC.P1) || 0;
    }
    return p;
  }

  function save(ev) {
    ev.preventDefault();
    var errEl = q('ct-form-error');
    errEl.textContent = '';
    var p = payload();
    var req;
    if (editingId != null) {
      req = cfetch('/contracts/' + editingId, { method: 'PUT', body: JSON.stringify(p) });
    } else {
      // The owning household: explicit selector value, else the single home
      // in the caller's scope.
      var slug = customer || (ctx && ctx.customers && ctx.customers.length === 1
        ? ctx.customers[0] : '');
      if (!slug) {
        errEl.textContent = __t('ct.pickHome', 'Selecciona el hogar del contrato.');
        return false;
      }
      p.customer = slug;
      req = cfetch('/contracts', { method: 'POST', body: JSON.stringify(p) });
    }
    q('ct-save-btn').disabled = true;
    req.then(function () {
      closeForm();
      App.showNotification(__t('ct.saved', 'Contrato guardado'), '', 'success');
      loadAll();
    }).catch(function (e) {
      errEl.textContent = e.message;
    }).finally(function () { q('ct-save-btn').disabled = false; });
    return false;
  }

  function remove() {
    if (editingId == null) return;
    AppUI.confirm(__t('ct.confirmDelete',
      '¿Eliminar este contrato? El histórico de costes volverá a calcularse con PVPC. Si solo cambias de tarifa, pon fecha de fin en su lugar.'))
      .then(function (ok) {
        if (!ok) return;
        cfetch('/contracts/' + editingId, { method: 'DELETE' }).then(function () {
          closeForm();
          App.showNotification(__t('ct.deleted', 'Contrato eliminado'), '', 'success');
          loadAll();
        }).catch(function (e) {
          q('ct-form-error').textContent = e.message;
        });
      });
  }

  // ── Load ─────────────────────────────────────────────────────────────
  function loadAll() {
    var pActive = cfetch('/contracts/active' + custQS()).then(function (r) { active = r; })
      .catch(function () { active = null; });
    var pList = cfetch('/contracts' + custQS()).then(function (r) { list = r.values || []; })
      .catch(function () { list = []; });
    return Promise.all([pActive, pList]).then(function () {
      renderActive();
      renderTable();
      loadBill();
    });
  }

  function loadContext() {
    return App.apiFetch('/consumption/context').then(function (c) {
      ctx = c;
      var sel = q('ct-customer');
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
      var showWrite = canWrite() ? '' : 'none';
      q('ct-new-btn').style.display = showWrite;
      q('ct-upload-btn').style.display = showWrite;
    });
  }

  // ── Upload + AI read ("place to give the contract") ──────────────────
  // Multipart POST — a bare fetch, NOT cfetch (which forces JSON Content-Type
  // and would break the multipart boundary). Same auth + 401 handling.
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
  function setUpStatus(msg, kind) {
    var el = q('ct-up-status');
    el.textContent = msg || '';
    el.style.color = kind === 'err' ? '#c0392b' : (kind === 'busy' ? '#2c5171' : '');
  }
  function openUpload() {
    q('ct-up-file').value = '';
    q('ct-up-text').value = '';
    q('ct-up-vlm').checked = false;
    setUpStatus('', '');
    AppUI.openPanel('uploadPanel');
  }
  function closeUpload() { AppUI.closePanel('uploadPanel'); }

  function readContract() {
    var file = q('ct-up-file').files[0];
    var text = q('ct-up-text').value.trim();
    if (!file && !text) {
      setUpStatus(__t('ct.upNeed', 'Sube un archivo o pega el texto de tu contrato.'), 'err');
      return;
    }
    var fd = new FormData();
    if (file) fd.append('file', file);
    if (text) fd.append('text', text);
    fd.append('use_vlm', q('ct-up-vlm').checked ? 'true' : 'false');
    q('ct-up-read').disabled = true;
    setUpStatus(__t('ct.upReading', 'Leyendo tu contrato con IA… puede tardar unos segundos.'), 'busy');
    upfetch('/contracts/extract', fd).then(function (r) {
      applyExtracted(r.extracted || {});
      closeUpload();
    }).catch(function (e) {
      setUpStatus(e.message || __t('common.error', 'Error'), 'err');
    }).finally(function () { q('ct-up-read').disabled = false; });
  }

  // Prefill the contract form with the AI-extracted fields (same field names as
  // a contract row) and open it for REVIEW — never auto-saves.
  function applyExtracted(ex) {
    editingId = null;
    var d = new Date();
    var todayLocal = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
    fillForm({
      label: ex.product_name || '',
      retailer: ex.retailer || '',
      contract_type: ex.contract_type || 'fixed',
      start_date: (ex.start_date && /^\d{4}-\d{2}-\d{2}$/.test(ex.start_date)) ? ex.start_date : todayLocal,
      end_date: '',
      cups: ex.cups || '',
      energy_p1_eur_kwh: ex.energy_p1_eur_kwh,
      energy_p2_eur_kwh: ex.energy_p2_eur_kwh,
      energy_p3_eur_kwh: ex.energy_p3_eur_kwh,
      margin_eur_kwh: ex.margin_eur_kwh,
      components: ex.components || null,
      power_p1_kw: ex.power_p1_kw != null ? ex.power_p1_kw : null,
      power_p2_kw: ex.power_p2_kw != null ? ex.power_p2_kw : null,
      power_p1_eur_kw_day: ex.power_p1_eur_kw_day,
      power_p2_eur_kw_day: ex.power_p2_eur_kw_day,
      meter_rental_eur_month: ex.meter_rental_eur_month != null ? ex.meter_rental_eur_month : 0.81,
      other_fixed_eur_month: 0,
      electricity_tax_pct: taxDefaults().iee,
      vat_pct: taxDefaults().vat,
      notes: ex.notes || '',
    });
    q('ct-form-title').textContent = __t('ct.reviewTitle', 'Revisa el contrato leído');
    q('ct-delete-btn').style.display = 'none';
    q('ct-form-error').textContent = '';
    var parts = [];
    if (ex.retailer) parts.push(ex.retailer);
    if (ex.product_name) parts.push(ex.product_name);
    var who = parts.join(' · ') || __t('ct.reviewGeneric', 'tu contrato');
    var note = q('ct-review-note');
    note.textContent = __t('ct.reviewNote',
      'Hemos leído {who}. Revisa los datos y añade tu potencia contratada antes de guardar; corrige lo que falte.')
      .replace('{who}', who);
    note.style.display = '';
    AppUI.openPanel('contractPanel');
  }

  window.CtPage = {
    onCustomerChange: function () {
      customer = q('ct-customer').value || '';
      loadAll();
    },
    openForm: openForm,
    closeForm: closeForm,
    openUpload: openUpload,
    closeUpload: closeUpload,
    readContract: readContract,
    setType: setType,
    save: save,
    remove: remove,
    loadBill: loadBill,
  };

  document.addEventListener('i18n:changed', function () {
    renderActive(); renderTable(); loadBill();
  });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      var now = new Date();
      q('ct-bill-month').value = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0');
      loadCompBase();   // warm the SSOT base so tax/matrix defaults are real
      loadContext().then(loadAll).catch(function (e) {
        App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
      });
    });
  });
})();
