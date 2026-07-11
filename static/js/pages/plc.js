/**
 * plc.js — Mi PLC page: resolves the household gateway via
 * /api/v1/plc/session (which ensures the tunnel forward through api_edge)
 * and embeds the CM4 local-webui at /plc/<port>/ (nginx proxy, tenancy-
 * gated by auth_request). States: loading / offline / upsell / live.
 */
(function () {
  'use strict';

  var customer = '';   // selected household (its gateway) — '' = first/only

  function q(id) { return document.getElementById(id); }

  function showState(html) {
    q('plc-state').innerHTML = html;
    q('plc-state').style.display = '';
    q('plc-frame-wrap').style.display = 'none';
  }

  function load() {
    showState('<span class="spinner"></span> <span class="text-muted" style="font-size:0.85rem;">' +
      __t('plc.connecting', 'Conectando con tu PLC…') + '</span>');
    App.apiFetch('/plc/session' + (customer ? '?customer=' + encodeURIComponent(customer) : '')).then(function (r) {
      // Multi-PLC: one gateway per household — selector only when >1
      var gws = r.gateways || [];
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
        return;
      }
      q('plc-frame').src = r.url;
      q('plc-state').style.display = 'none';
      q('plc-frame-wrap').style.display = '';
      q('plc-reload').style.display = 'none';   // the PLC UI has its own refresh
    }).catch(function (e) {
      var detail = {};
      try { detail = JSON.parse(e.message); } catch (_) {}
      if (detail.code === 'TIER_REQUIRED') {
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
