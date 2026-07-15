/**
 * plc.js — Mi PLC page: resolves the household gateway via
 * /api/v1/plc/session (which ensures the tunnel forward through api_edge)
 * and embeds the CM4 local-webui at /plc/<port>/ (nginx proxy, tenancy-
 * gated by auth_request). States: loading / offline / upsell / live.
 */
(function () {
  'use strict';

  var customer = '';   // selected household (its gateway) — '' = first/only
  var gateway = '';    // hostname del PLC elegido (F3) — '' = primero
  var retryTimer = null;

  function q(id) { return document.getElementById(id); }

  // apiFetch clone that only logs out on 401 — App.apiFetch logs out on 403
  // too, which BOOTS a basic-tier user instead of showing the PRO upsell.
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

  function scheduleRetry() {
    clearTimeout(retryTimer);
    retryTimer = setTimeout(load, 60000);
  }

  function showState(html) {
    q('plc-state').innerHTML = html;
    q('plc-state').style.display = '';
    q('plc-frame-wrap').style.display = 'none';
  }

  function load() {
    clearTimeout(retryTimer);
    showState('<span class="spinner"></span> <span class="text-muted" style="font-size:0.85rem;">' +
      __t('plc.connecting', 'Conectando con tu PLC…') + '</span>');
    // F3: el punto de suministro global manda — el server resuelve
    // supply → gateway (sin carrera con el fetch async del contexto).
    var supplyId = (window.App && App.getSupply) ? App.getSupply() : '';
    var hasSupply = !!supplyId;
    var parts = [];
    if (customer) parts.push('customer=' + encodeURIComponent(customer));
    if (gateway && !hasSupply) parts.push('gateway=' + encodeURIComponent(gateway));
    if (hasSupply) parts.push('supply=' + encodeURIComponent(supplyId));
    cfetch('/plc/session' + (parts.length ? '?' + parts.join('&') : '')).then(function (r) {
      // Multi-PLC: selector propio solo cuando hay >1 Y no hay punto global
      var gws = (r && r.gateways) || [];
      var sel = q('plc-select');
      if (hasSupply) {
        sel.style.display = 'none';   // cascada: el selector global decide
      } else if (gws.length > 1 && !sel.options.length) {
        sel.innerHTML = gws.map(function (g) {
          return '<option value="' + g.hostname + '">' + g.hostname + '</option>';
        }).join('');
        sel.value = gateway || gws[0].hostname;
        sel.style.display = '';
        q('plc-controls').style.display = 'flex';
      }
      if (!r.online) {
        showState('<h3 style="margin-top:0;">' + __t('plc.offlineTitle', 'Tu PLC no está accesible ahora mismo') + '</h3>' +
          '<p class="sav-explain" style="max-width:520px;margin:0.5rem auto 0;">' +
          __t('plc.offlineBody', 'La caja está sin conexión o el túnel se está restableciendo. Tus datos siguen guardándose en casa; vuelve a intentarlo en unos minutos.') + '</p>');
        q('plc-reload').style.display = '';
        q('plc-controls').style.display = 'flex';
        scheduleRetry();   // auto-reconnect: retry every 60s while offline
        return;
      }
      q('plc-frame').src = r.url;
      q('plc-state').style.display = 'none';
      q('plc-frame-wrap').style.display = '';
      q('plc-reload').style.display = 'none';   // the PLC UI has its own refresh
    }).catch(function (e) {
      var detail = {};
      try { detail = JSON.parse(e.message); } catch (_) {}
      if (e.code === 'TIER_REQUIRED' || detail.code === 'TIER_REQUIRED') {
        showState('<h3 style="margin-top:0;">' + __t('plc.upsellTitle', 'Acceso remoto a tu PLC') + '</h3>' +
          '<p class="sav-explain" style="max-width:520px;margin:0.5rem auto 0;">' +
          __t('plc.upsellBody', 'Ver y manejar la caja de tu casa desde cualquier lugar forma parte del plan PRO.') + '</p>');
      } else {
        showState('<span class="text-muted">' + (detail.message || e.message) + '</span>');
        q('plc-reload').style.display = '';
        q('plc-controls').style.display = 'flex';
      }
    });
  }

  window.PlcPage = {
    change: function () {
      gateway = q('plc-select').value || '';
      load();
    },
  };

  // ── EcoFlow — baterías del hogar (slide panel sobre el iframe) ──────────
  var efData = null;
  var efTimer = null;

  var EF_MODES = { 0: '—', 1: 'ecoflow.modeCool', 2: 'ecoflow.modeHeat', 3: 'ecoflow.modeVent', 4: 'ecoflow.modeDehum', 5: 'ecoflow.modeThermo' };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function num(v, dec) { return (v == null || isNaN(v)) ? '—' : Number(v).toFixed(dec == null ? 0 : dec); }

  function efDot(on) {
    return '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:1px;background:' +
      (on ? 'var(--success-color, #4caf50)' : '#b0b6bd') + ';"></span>';
  }

  function efDeviceCard(d) {
    var s = d.snapshot || {};
    var soc = s.cms_batt_soc != null ? s.cms_batt_soc : s.bms_batt_soc;
    var isWave = (d.model === 'wave3');
    var rows = '';
    rows += '<div style="display:flex;align-items:baseline;gap:10px;margin:4px 0 6px;">' +
      '<span style="font-size:1.65rem;font-weight:700;font-variant-numeric:tabular-nums;">' + num(soc) + '%</span>' +
      '<span class="text-muted" style="font-size:0.78rem;">' +
      '↓ ' + num(s.pow_in_sum_w) + ' W · ↑ ' + num(s.pow_out_sum_w) + ' W</span></div>';
    if (!isWave) {
      var packs = [];
      if (s.bms0_soc != null || s.bms0_f32_show_soc != null) packs.push(__t('ecoflow.hostBatt', 'Batería') + ' ' + num(s.bms0_f32_show_soc != null ? s.bms0_f32_show_soc : s.bms0_soc) + '%');
      if (s.bms1_soc != null || s.bms1_f32_show_soc != null) packs.push(__t('ecoflow.extraBatt', 'Batería extra') + ' ' + num(s.bms1_f32_show_soc != null ? s.bms1_f32_show_soc : s.bms1_soc) + '%');
      if (packs.length) rows += '<div class="text-muted" style="font-size:0.76rem;">' + packs.join(' · ') + '</div>';
      var rem = (s.pow_in_sum_w > 5 && s.bms_chg_rem_time) ? [__t('ecoflow.chgRem', 'carga completa en'), s.bms_chg_rem_time]
        : (s.bms_dsg_rem_time ? [__t('ecoflow.dsgRem', 'autonomía'), s.bms_dsg_rem_time] : null);
      if (rem && rem[1] > 0 && rem[1] < 6000) {
        var h = Math.floor(rem[1] / 60), m = Math.round(rem[1] % 60);
        rows += '<div class="text-muted" style="font-size:0.76rem;">' + rem[0] + ' ' + (h ? h + ' h ' : '') + m + ' min</div>';
      }
    } else {
      var mode = EF_MODES[s.wave_operating_mode];
      rows += '<div class="text-muted" style="font-size:0.78rem;">' +
        __t('ecoflow.mode', 'Modo') + ': <strong>' + (mode && mode !== '—' ? __t(mode, mode) : '—') + '</strong>' +
        (s.temp_ambient != null ? ' · ' + __t('ecoflow.ambient', 'ambiente') + ' ' + num(s.temp_ambient, 1) + ' °C' : '') +
        '</div>';
    }
    return '<div style="padding:10px 12px;margin-bottom:10px;border-radius:10px;background:rgba(70,130,180,0.08);">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;">' +
      '<strong style="font-size:0.9rem;">' + esc(d.name || d.model || d.sn) + '</strong>' +
      '<span class="text-muted" style="font-size:0.74rem;">' + efDot(d.online) +
      (d.online ? __t('ecoflow.online', 'en línea') : __t('ecoflow.offline', 'sin conexión')) + '</span></div>' +
      rows + '</div>';
  }

  function efRuleBlock(r) {
    var p = r.params || {};
    return '<div style="padding:10px 12px;margin-bottom:10px;border-radius:10px;background:rgba(76,175,80,0.07);">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">' +
      '<strong style="font-size:0.86rem;">' + esc(r.name) + '</strong>' +
      '<label style="display:flex;align-items:center;gap:6px;font-size:0.78rem;cursor:pointer;">' +
      '<input type="checkbox" id="ef-rule-enabled" ' + (r.enabled ? 'checked' : '') + '> <span data-i18n="ecoflow.ruleEnabled">Activa</span></label></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin-top:8px;">' +
      '<label style="font-size:0.72rem;">' + __t('ecoflow.ruleMode', 'Modo') +
      '<select id="ef-rule-mode" class="pibico-select" style="display:block;margin-top:2px;width:110px;">' +
      '<option value="auto"' + (r.mode === 'auto' ? ' selected' : '') + '>Auto</option>' +
      '<option value="manual"' + (r.mode === 'manual' ? ' selected' : '') + '>Manual</option></select></label>' +
      '<label style="font-size:0.72rem;">SoC mín %<input id="ef-soc-min" type="number" min="5" max="95" class="pibico-input" style="display:block;margin-top:2px;width:76px;" value="' + num(p.soc_min) + '"></label>' +
      '<label style="font-size:0.72rem;">SoC máx %<input id="ef-soc-max" type="number" min="10" max="100" class="pibico-input" style="display:block;margin-top:2px;width:76px;" value="' + num(p.soc_max) + '"></label>' +
      '<button class="btn btn-sm btn-primary" onclick="EcoflowPanel.saveRule(' + r.id + ')" data-i18n="ecoflow.save">Guardar</button>' +
      '</div>' +
      '<div class="text-muted" style="font-size:0.7rem;margin-top:6px;" data-i18n="ecoflow.ruleHint">En Auto, la carga se enciende con SoC bajo o en horas baratas y se corta al llegar al máximo.</div>' +
      '</div>';
  }

  function efPlugRow(plug) {
    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 10px;margin-bottom:6px;border-radius:8px;background:rgba(44,81,113,0.06);">' +
      '<span style="font-size:0.82rem;">' + efDot(true) + esc(plug.name || plug.sensor_key) + '</span>' +
      '<span style="display:flex;gap:6px;">' +
      '<button class="btn btn-sm" onclick="EcoflowPanel.plug(\'' + esc(plug.sensor_key) + '\', true)">ON</button>' +
      '<button class="btn btn-sm" onclick="EcoflowPanel.plug(\'' + esc(plug.sensor_key) + '\', false)">OFF</button>' +
      '</span></div>';
  }

  function efLog(entries) {
    if (!entries || !entries.length) return '';
    var items = entries.slice(0, 6).map(function (e) {
      var when = e.ts ? new Date(e.ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
      var badge = e.action === 'on' ? 'ON' : (e.action === 'off' ? 'OFF' : '·');
      return '<div style="font-size:0.72rem;padding:2px 0;color:var(--text-muted,#5f6b76);">' +
        '<strong style="font-variant-numeric:tabular-nums;">' + when + '</strong> ' + badge + ' — ' + esc(e.reason || '') + '</div>';
    }).join('');
    return '<div style="margin-top:4px;"><div style="font-size:0.74rem;font-weight:600;margin-bottom:2px;" data-i18n="ecoflow.logTitle">Últimas decisiones</div>' + items + '</div>';
  }

  function efRender() {
    var body = q('ecoflow-body');
    if (!body || !efData) return;
    var html = '';
    (efData.devices || []).forEach(function (d) { html += efDeviceCard(d); });
    var plugs = [];
    (efData.rules || []).forEach(function (r) {
      html += efRuleBlock(r);
      ((r.params || {}).plugs || []).forEach(function (pl) { plugs.push(pl); });
    });
    if (plugs.length) {
      html += '<div style="font-size:0.74rem;font-weight:600;margin:6px 0 4px;" data-i18n="ecoflow.plugsTitle">Enchufes</div>' +
        plugs.map(efPlugRow).join('');
    }
    html += efLog(efData.log);
    if (!html) html = '<span class="text-muted" style="font-size:0.8rem;" data-i18n="ecoflow.none">Sin equipos EcoFlow en este hogar.</span>';
    body.innerHTML = html;
    if (window.i18n && i18n.apply) i18n.apply(body);
  }

  function efRefresh() {
    return cfetch('/plc/ecoflow').then(function (r) {
      efData = r || {};
      var devs = efData.devices || [];
      var btn = q('plc-ecoflow-btn');
      if (btn) {
        btn.style.display = devs.length ? 'inline-flex' : 'none';
        var main = devs.filter(function (d) { return d.model === 'delta3plus'; })[0] || devs[0];
        var soc = main && main.snapshot ? (main.snapshot.cms_batt_soc != null ? main.snapshot.cms_batt_soc : main.snapshot.bms_batt_soc) : null;
        q('plc-ecoflow-soc').textContent = soc != null ? Math.round(soc) + '%' : '';
      }
      efRender();
    }).catch(function () { /* tier/red — el botón simplemente no aparece */ });
  }

  window.EcoflowPanel = {
    open: function () {
      if (window.AppUI && AppUI.openPanel) AppUI.openPanel('ecoflowPanel');
      efRender();
      clearInterval(efTimer);
      efTimer = setInterval(efRefresh, 10000);
    },
    close: function () {
      if (window.AppUI && AppUI.closePanel) AppUI.closePanel('ecoflowPanel');
      clearInterval(efTimer);
    },
    plug: function (sensorKey, on) {
      cfetch('/plc/shelly/' + encodeURIComponent(sensorKey) + '/switch',
        { method: 'POST', body: JSON.stringify({ on: on }) })
        .then(function () {
          App.showNotification(__t('ecoflow.sent', 'Orden enviada'), '', 'success');
        })
        .catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
    },
    saveRule: function (ruleId) {
      var body = {
        enabled: q('ef-rule-enabled') ? q('ef-rule-enabled').checked : undefined,
        mode: q('ef-rule-mode') ? q('ef-rule-mode').value : undefined,
        params: {
          soc_min: parseFloat(q('ef-soc-min') && q('ef-soc-min').value),
          soc_max: parseFloat(q('ef-soc-max') && q('ef-soc-max').value),
        },
      };
      if (isNaN(body.params.soc_min) || isNaN(body.params.soc_max) ||
          body.params.soc_min >= body.params.soc_max) {
        App.showNotification(__t('common.error', 'Error'), __t('ecoflow.badRange', 'Rango SoC inválido'), 'danger');
        return;
      }
      cfetch('/plc/ecoflow/rule/' + ruleId, { method: 'PUT', body: JSON.stringify(body) })
        .then(function () {
          App.showNotification(__t('ecoflow.saved', 'Regla guardada'), '', 'success');
          efRefresh();
        })
        .catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
    },
  };

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      q('plc-reload').onclick = load;
      load();
      efRefresh();
    });
  });
})();
