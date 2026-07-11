/**
 * app.js — CONSUM-IA product Panel (F0/F1): live KPIs + per-device power.
 * Data: /api/v1/consumption/{context,summary,devices,current}.
 * Auth rides on the httpOnly SSO cookie (apiFetch adds Bearer/X-API-Key only
 * when localStorage has them). Auto-refresh every 60s (live power).
 */
(function () {
  'use strict';

  var ctx = null;         // /consumption/context payload
  var customer = '';      // '' = all my customers

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 1 : dec }); }

  function custQS() { return customer ? ('?customer=' + encodeURIComponent(customer)) : ''; }

  function loadContext() {
    return App.apiFetch('/consumption/context').then(function (c) {
      ctx = c;
      // tier badge
      var tb = q('tier-badge');
      if (tb) {
        tb.textContent = (c.tier || 'basic').toUpperCase() + (c.ai_enabled ? ' · IA' : '');
        tb.style.display = '';
        tb.className = 'badge ' + (c.tier === 'basic' ? 'badge-info' : 'badge-ok');
      }
      // customer selector (only if >1 household)
      var sel = q('customer-select');
      if (sel && (c.customers || []).length > 1) {
        sel.innerHTML = '<option value="">' + __t('app.allHomes', 'Todos mis hogares') + '</option>' +
          c.customers.map(function (s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
        sel.style.display = '';
      }
      q('kpi-devices').textContent = (c.devices || []).length || '—';
    });
  }

  function loadSummary() {
    return App.apiFetch('/consumption/summary' + custQS()).then(function (s) {
      q('kpi-power').textContent = fmt(s.total_w, 0);
      q('kpi-today').textContent = fmt(s.today_kwh);
      q('kpi-week').textContent = fmt(s.week_kwh);
      q('kpi-month').textContent = fmt(s.month_kwh);
      if (s.pvpc_now_eur_kwh != null) {
        q('kpi-pvpc').textContent = Number(s.pvpc_now_eur_kwh).toFixed(3);
        q('kpi-period').textContent = s.pvpc_period || '—';
        // naive today cost: kWh_today × current price (F2 does hour×hour)
        if (s.today_kwh != null) q('kpi-cost-today').textContent = fmt(s.today_kwh * s.pvpc_now_eur_kwh, 2);
      }
      if (s.carbon_gco2_kwh != null) q('kpi-carbon').textContent = fmt(s.carbon_gco2_kwh, 0);
    });
  }

  function loadDevices() {
    return App.apiFetch('/consumption/devices' + custQS()).then(function (r) {
      var rows = r.data || [];
      var online = rows.filter(function (d) { return d.is_online; }).length;
      q('kpi-devices').textContent = rows.length;
      q('kpi-devices-online').textContent = online + ' ' + __t('app.online', 'en línea');
      return rows;
    });
  }

  function loadLive() {
    return App.apiFetch('/consumption/current' + custQS()).then(function (r) {
      var tbody = q('live-tbody');
      var rows = r.devices || [];
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">' + __t('common.noData', 'Sin datos') + '</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(function (d) {
        var on = (d.power_w || 0) > 1;
        return '<tr>' +
          '<td class="mono">' + d.device + '</td>' +
          '<td class="mono">' + fmt(d.power_w, 1) + '</td>' +
          '<td><span class="badge ' + (on ? 'badge-ok' : 'badge-info') + '">' +
            (on ? __t('app.active', 'Activo') : __t('app.idle', 'Reposo')) + '</span></td>' +
          '</tr>';
      }).join('');
    });
  }

  function refresh() {
    loadSummary().catch(function () {});
    loadLive().catch(function () {});
    loadDevices().catch(function () {});
  }

  window.ConsumApp = {
    onCustomerChange: function () {
      customer = q('customer-select').value || '';
      refresh();
    },
    refresh: refresh,
  };

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      loadContext().then(refresh).catch(function (e) {
        App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
      });
      // Live power changes fast — 60s here (vs 5 min on the exo pages)
      setInterval(refresh, 60000);
    });
  });
})();
