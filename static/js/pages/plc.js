/**
 * plc.js — Mi PLC page: resolves the household gateway via
 * /api/v1/plc/session (which ensures the tunnel forward through api_edge)
 * and embeds the CM4 local-webui at /plc/<port>/ (nginx proxy, tenancy-
 * gated by auth_request). States: loading / offline / upsell / live.
 */
(function () {
  'use strict';

  var customer = '';   // selected household (its gateway) — '' = first/only
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
    cfetch('/plc/session' + (customer ? '?customer=' + encodeURIComponent(customer) : '')).then(function (r) {
      // Multi-PLC: one gateway per household — selector only when >1
      var gws = (r && r.gateways) || [];
      var sel = q('plc-select');
      if (gws.length > 1 && !sel.options.length) {
        sel.innerHTML = gws.map(function (g) {
          return '<option value="' + g.customer + '">' + g.hostname + '</option>';
        }).join('');
        sel.value = gws[0].customer;
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
      customer = q('plc-select').value || '';
      load();
    },
  };

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      q('plc-reload').onclick = load;
      load();
    });
  });
})();
