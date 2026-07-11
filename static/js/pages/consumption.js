/**
 * consumption.js — Consumo page (F2): hourly day chart with P1/P2/P3 tariff
 * colors + hour×hour PVPC cost, daily month chart (kWh bars + € line),
 * compact hourly table with Pager. Date navigation + device selector.
 * Auto-refresh every 5 min. Endpoints: /consumption/{day,month,context}.
 */
(function () {
  'use strict';

  var day = null;        // /consumption/day payload
  var month = null;      // /consumption/month payload
  var device = '';       // '' = whole house (EM)
  var pager = null;

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 2 : dec }); }
  function today() {
    // LOCAL calendar date (toISOString() is UTC — wrong between 00:00-02:00 CEST)
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
  }

  function periodColor(p) {
    if (p === 'P1') return 'rgba(231,76,60,0.75)';
    if (p === 'P2') return 'rgba(243,156,18,0.75)';
    return 'rgba(46,204,113,0.75)';
  }

  // ECharts instances (hover tooltips) — one per container.
  var charts = {};
  function chart(id) {
    var el = q(id);
    if (!el || typeof echarts === 'undefined') return null;
    if (!charts[id]) charts[id] = echarts.init(el);
    return charts[id];
  }
  var AXIS = { fontSize: 10, color: 'rgba(44,62,80,0.6)' };
  var TOOLTIP = {
    trigger: 'axis',
    backgroundColor: 'rgba(255,255,255,0.96)',
    borderColor: 'rgba(44,81,113,0.2)',
    textStyle: { fontSize: 12, color: '#22384c' },
    axisPointer: { type: 'shadow' },
  };

  function drawDayChart() {
    var c = chart('cons-day-chart');
    if (!c || !day) return;
    var byHour = {};
    (day.values || []).forEach(function (v) { byHour[v.hour] = v; });
    var hours = [];
    for (var h = 0; h < 24; h++) hours.push(h);
    c.setOption({
      grid: { left: 44, right: 8, top: 12, bottom: 22 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var i = params[0].dataIndex, v = byHour[i];
          if (!v) return String(i).padStart(2, '0') + ':00 — ' + __t('common.noData', 'Sin datos');
          return '<b>' + String(i).padStart(2, '0') + ':00–' + String(i + 1).padStart(2, '0') + ':00</b><br>' +
            fmt(v.kwh, 3) + ' kWh' + (v.period ? ' · ' + v.period : '') +
            (v.price_eur_kwh != null ? '<br>PVPC ' + v.price_eur_kwh.toFixed(4) + ' €/kWh' : '') +
            (v.cost_eur != null ? ' · <b>' + v.cost_eur.toFixed(3) + ' €</b>' : '');
        },
      }),
      xAxis: { type: 'category', data: hours.map(function (h) { return String(h).padStart(2, '0'); }),
               axisLabel: Object.assign({ interval: 2 }, AXIS), axisTick: { show: false } },
      yAxis: { type: 'value', axisLabel: AXIS, splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [{
        type: 'bar', barWidth: '72%',
        data: hours.map(function (h) {
          var v = byHour[h];
          return { value: v ? v.kwh : 0, itemStyle: { color: periodColor(v && v.period) } };
        }),
      }],
    });
  }

  function drawMonthChart() {
    var c = chart('cons-month-chart');
    if (!c || !month) return;
    var vals = month.values || [];
    if (!vals.length) { c.clear(); return; }
    c.setOption({
      grid: { left: 44, right: 44, top: 12, bottom: 22 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var v = vals[params[0].dataIndex];
          return '<b>' + v.date + '</b><br>' + fmt(v.kwh, 2) + ' kWh' +
            (v.cost_eur != null ? ' · <b>' + v.cost_eur.toFixed(2) + ' €</b>' : '');
        },
      }),
      xAxis: { type: 'category', data: vals.map(function (v) { return v.date.slice(8); }),
               axisLabel: Object.assign({ interval: 4 }, AXIS), axisTick: { show: false } },
      yAxis: [
        { type: 'value', axisLabel: AXIS, splitLine: { lineStyle: { opacity: 0.25 } } },
        { type: 'value', axisLabel: Object.assign({ formatter: '{value}€', color: '#e67e22' }, AXIS),
          splitLine: { show: false } },
      ],
      series: [
        { type: 'bar', barWidth: '70%', data: vals.map(function (v) { return v.kwh; }),
          itemStyle: { color: 'rgba(70,130,180,0.75)' } },
        { type: 'line', yAxisIndex: 1, symbol: 'circle', symbolSize: 4,
          data: vals.map(function (v) { return v.cost_eur; }),
          lineStyle: { color: '#e67e22', width: 2 }, itemStyle: { color: '#e67e22' } },
      ],
    });
  }

  function renderTable() {
    var tbody = q('cons-tbody');
    var vals = (day && day.values) || [];
    if (!vals.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">' + __t('common.noData', 'Sin datos') + '</td></tr>';
      return;
    }
    if (!pager) {
      pager = window.Pager.create({
        containerIds: ['cons-pager-top', 'cons-pager-bottom'],
        pageSize: 12,
        onRender: function (slice) {
          tbody.innerHTML = slice.map(function (v) {
            return '<tr>' +
              '<td class="mono">' + String(v.hour).padStart(2, '0') + ':00</td>' +
              '<td class="mono">' + fmt(v.kwh, 3) + '</td>' +
              '<td class="mono">' + (v.price_eur_kwh != null ? v.price_eur_kwh.toFixed(4) : '—') + '</td>' +
              '<td><span class="badge" style="background:' + periodColor(v.period).replace('0.75', '0.18') + ';color:#333;">' + (v.period || '—') + '</span></td>' +
              '<td class="mono">' + (v.cost_eur != null ? v.cost_eur.toFixed(3) : '—') + '</td>' +
              '</tr>';
          }).join('');
        },
      });
    }
    pager.setItems(vals);
  }

  function custQS(sep) { return device ? ((sep || '&') + 'device=' + encodeURIComponent(device)) : ''; }

  // ── PRO cards (F4): forecast + bands; 403 TIER_REQUIRED → upsell ──
  function upsellOf(e) {
    try {
      var d = JSON.parse(e.message);
      if (d.code === 'TIER_REQUIRED') {
        return '<div class="pro-upsell">' + __t('cons.proUpsell',
          'Disponible en el plan PRO — predicción de factura, desglose por franjas y exportación.') + '</div>';
      }
    } catch (_) {}
    return '<span class="text-muted">—</span>';
  }

  function loadForecast() {
    return App.apiFetch('/consumption/forecast-month' + custQS('?')).then(function (r) {
      q('cons-forecast').innerHTML =
        '<div style="display:flex;gap:1.5rem;flex-wrap:wrap;align-items:baseline;">' +
        '<div><div class="kpi-value" style="font-size:1.7rem;color:#e67e22;">' + fmt(r.forecast_cost_eur) + ' €</div>' +
        '<div class="kpi-sub">' + __t('cons.fcCost', 'coste previsto del mes') + '</div></div>' +
        '<div><div class="kpi-value" style="font-size:1.7rem;">' + fmt(r.forecast_kwh, 1) + '</div>' +
        '<div class="kpi-sub">' + __t('cons.fcKwh', 'kWh previstos') + '</div></div>' +
        '</div>' +
        '<p class="sav-explain" style="margin:0.5rem 0 0;">' + __t('cons.fcExplain',
          'Llevas {mtd} € en {d} días; proyección al día {n} usando tu media reciente de {avg} €/día.')
          .replace('{mtd}', fmt(r.mtd_cost_eur))
          .replace('{d}', r.day_of_month)
          .replace('{n}', r.days_in_month)
          .replace('{avg}', fmt(r.avg_day_cost_eur)) + '</p>';
    }).catch(function (e) { q('cons-forecast').innerHTML = upsellOf(e); });
  }

  function loadBands() {
    var m = (q('cons-date').value || today()).slice(0, 7);
    return App.apiFetch('/consumption/bands?month=' + m + custQS()).then(function (r) {
      q('cons-bands-month').textContent = '· ' + m;
      var total = r.total_kwh || 0;
      q('cons-bands').innerHTML = (r.values || []).map(function (v) {
        var pctW = total > 0 ? Math.max(v.share_pct, 2) : 0;
        return '<div style="display:flex;align-items:center;gap:10px;margin:6px 0;">' +
          '<span class="badge" style="width:34px;text-align:center;background:' + periodColor(v.period).replace('0.75', '0.18') + ';color:#333;">' + v.period + '</span>' +
          '<div style="flex:1;background:rgba(44,62,80,0.08);border-radius:4px;height:14px;overflow:hidden;">' +
          '<div style="width:' + pctW + '%;height:100%;background:' + periodColor(v.period) + ';"></div></div>' +
          '<span class="mono" style="font-size:0.75rem;min-width:150px;text-align:right;">' +
          fmt(v.kwh, 1) + ' kWh · ' + fmt(v.cost_eur) + ' € · ' + fmt(v.share_pct, 0) + '%</span>' +
          '</div>';
      }).join('');
    }).catch(function (e) { q('cons-bands').innerHTML = upsellOf(e); });
  }

  function exportCsv() {
    var d = q('cons-date').value || today();
    var start = d.slice(0, 7) + '-01';
    var url = (window.__ROOT__ || '') + '/api/v1/consumption/export.csv?start=' + start + '&end=' + d + custQS();
    // Cookie-authenticated GET — a 403 (basic tier) lands as JSON in a tab
    fetch(url, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          var msg;
          try { msg = (JSON.parse(t).detail || {}).message; } catch (_) {}
          App.showNotification('PRO', msg || __t('cons.proUpsell', 'Disponible en el plan PRO.'), 'warning');
        });
      }
      return res.blob().then(function (b) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = 'consumo_' + start + '_' + d + '.csv';
        a.click();
        URL.revokeObjectURL(a.href);
      });
    });
  }

  function loadDay() {
    var d = q('cons-date').value || today();
    return App.apiFetch('/consumption/day?date=' + d + custQS()).then(function (r) {
      day = r;
      q('ck-kwh').textContent = fmt(r.total_kwh);
      q('ck-cost').textContent = fmt(r.total_cost_eur);
      q('ck-avg').textContent = r.avg_price_eur_kwh != null ? r.avg_price_eur_kwh.toFixed(4) : '—';
      q('cons-chart-date').textContent = '· ' + d;
      drawDayChart();
      renderTable();
    });
  }

  function loadMonth() {
    var d = (q('cons-date').value || today()).slice(0, 7);
    return App.apiFetch('/consumption/month?month=' + d + custQS()).then(function (r) {
      month = r;
      q('ck-month').textContent = fmt(r.total_kwh, 1) + ' kWh';
      q('ck-month-cost').textContent = fmt(r.total_cost_eur) + ' €';
      q('cons-month-label').textContent = '· ' + d;
      drawMonthChart();
    });
  }

  function loadDevices() {
    // Real reporting sensors (sensor_data device_ids) — NOT the gateway,
    // which never reports power and would filter everything to zero.
    return App.apiFetch('/consumption/sensors').then(function (r) {
      var sel = q('cons-device');
      var opts = ['<option value="">' + __t('cons.wholeHouse', 'Toda la casa') + '</option>'];
      (r.data || []).forEach(function (o) {
        opts.push('<option value="' + o.id + '">' + (o.name || o.id) + '</option>');
      });
      sel.innerHTML = opts.join('');
    });
  }

  function reloadFast() {
    // Midnight rollover on a long-lived tab: bump the picker's max (and the
    // selection, if it was sitting on "today") to the new local date.
    var t = today();
    var de = q('cons-date');
    if (de && de.max !== t) {
      var wasToday = de.value === de.max;
      de.max = t;
      if (wasToday || !de.value) de.value = t;
    }
    // Continuous refresh: the day view (KPIs + hourly chart/table) moves
    // with live data — 60s cadence, cheap single-day query.
    loadDay().catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
  }

  function reloadSlow() {
    loadMonth().catch(function () {});
    loadForecast();
    loadBands();
  }

  function reload() {
    device = q('cons-device').value || '';
    reloadFast();
    reloadSlow();
  }

  function shift(days) {
    var d = new Date((q('cons-date').value || today()) + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + days);
    var target = d.toISOString().slice(0, 10);
    if (target > today()) return;
    q('cons-date').value = target;
    reload();
  }

  window.ConsPage = { reload: reload };

  window.addEventListener('resize', function () {
    Object.keys(charts).forEach(function (id) { charts[id].resize(); });
  });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      q('cons-date').value = today();
      q('cons-date').max = today();
      q('cons-date').onchange = reload;
      q('cons-prev').onclick = function () { shift(-1); };
      q('cons-next').onclick = function () { shift(1); };
      q('cons-today').onclick = function () { q('cons-date').value = today(); reload(); };
      q('cons-export').onclick = exportCsv;
      loadDevices().then(reload).catch(reload);
      // Continuous refresh: day view 60s; month + PRO cards 5 min
      setInterval(reloadFast, 60000);
      setInterval(reloadSlow, 300000);
    });
  });
})();
