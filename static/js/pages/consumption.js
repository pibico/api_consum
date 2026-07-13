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

  function q(id) { return document.getElementById(id); }

  // Appliance label: the friendly name set in the PLC (synced to sensors.name)
  // when present; otherwise a clean short id instead of the raw 'shelly_<mac>'
  // key (name the appliance in the PLC to replace it).
  function sensorLabel(o) {
    if (o.name && String(o.name).trim()) return o.name;
    var m = String(o.id || '').replace(/^shelly[_-]?/i, '');
    return m.length > 5 ? __t('cons.sensorGeneric', 'Sensor') + ' ··' + m.slice(-4) : (m || o.id);
  }
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

  // The hourly detail table was removed (redundant with the chart) — its data
  // is available via the CSV export button on the chart header. Kept as a
  // no-op so callers don't need touching.
  function renderTable() {}

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
        opts.push('<option value="' + o.id + '">' + sensorLabel(o) + '</option>');
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
    loadPowerPeak();
    loadSankey();
  }

  // ── Peak power per hour (household demand curve) ──────────────────────
  function loadPowerPeak() {
    var date = q('cons-date').value || today();
    return App.apiFetch('/consumption/power-peak?date=' + date + custQS('&')).then(function (r) {
      drawPeakChart(r);
      q('cons-peak-max').textContent = r.peak_w
        ? '· ' + __t('cons.peakMax', 'máx') + ' ' + (r.peak_w / 1000).toFixed(2) + ' kW' : '';
    }).catch(function () {});
  }
  function drawPeakChart(r) {
    var c = chart('cons-peak-chart');
    if (!c) return;
    var byHour = {};
    (r.values || []).forEach(function (v) { byHour[v.hour] = v.peak_w; });
    var hours = [];
    for (var h = 0; h < 24; h++) hours.push(h);
    c.clear();
    c.setOption({
      grid: { left: 40, right: 10, top: 14, bottom: 20 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var h = params[0].dataIndex, w = byHour[h] || 0;
          return String(h).padStart(2, '0') + ':00<br><b>' + (w / 1000).toFixed(2) + ' kW</b> · ' + Math.round(w) + ' W';
        },
      }),
      xAxis: { type: 'category', data: hours.map(function (h) { return String(h).padStart(2, '0'); }),
               axisLabel: Object.assign({ interval: 2 }, AXIS), axisTick: { show: false } },
      yAxis: { type: 'value', name: 'kW', nameTextStyle: { fontSize: 9, color: 'rgba(44,62,80,0.6)' },
               axisLabel: Object.assign({ formatter: function (v) { return (v / 1000).toFixed(1); } }, AXIS),
               splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [{ type: 'bar', barWidth: '68%',
                 data: hours.map(function (h) { return byHour[h] || 0; }),
                 itemStyle: { color: '#4682b4', borderRadius: [3, 3, 0, 0] } }],
    }, true);
  }

  // ── Live wiring Sankey (casa → dispositivos, from the PLC topology) ────
  function loadSankey() {
    return App.apiFetch('/consumption/topology' + custQS('?')).then(function (r) {
      if (!r || !r.root) {
        q('cons-sankey-note').textContent = __t('cons.sankeyOffline', 'Sin conexión con el PLC y sin cableado guardado todavía.');
        var c0 = chart('cons-sankey'); if (c0) c0.clear(); skSig = '';
        q('cons-sankey-total').textContent = '';
        return;
      }
      drawSankey(r);
      q('cons-sankey-note').textContent = (r.status === 'offline_fallback')
        ? __t('cons.sankeyPersisted', 'Sin conexión con el PLC — cableado guardado, potencia del medidor.') : '';
    }).catch(function () {});
  }

  // Node palette mirrors the PLC's own wiring Sankey (the reference the user
  // prefers): mains, compute-base, installation, baseline, ordinary loads.
  var SK = { mains: '#2c5171', cbase: '#d0a94e', home: '#4682b4',
             baseline: '#9aa5b1', load: '#6a9bc3' };
  function wLabel(w) { return w >= 1000 ? (w / 1000).toFixed(2) + ' kW' : Math.round(w) + ' W'; }

  // Update-in-place state: rebuilding the whole Sankey every refresh (clear +
  // notMerge) makes it flash and re-layout. While the GRAPH SHAPE (nodes +
  // links) is unchanged we only merge new values into the existing series —
  // ECharts animates the widths smoothly. skIdName/skPowerById are module-
  // scoped so the formatters bound at build time always read the LATEST tick.
  var skSig = '', skIdName = {}, skPowerById = {};

  // Live wiring Sankey — a faithful port of the CM4 local-webui's wiring flow:
  // watts on every node label, and the PLC "net" model (Mains → compute base /
  // installation → each load → unexplained baseline) with matching colors. When
  // the PLC is offline we get no net model, so we fall back to a plain
  // parent→child tree with a "Resto (no medido)" remainder.
  function drawSankey(tree) {
    var c = chart('cons-sankey');
    if (!c) return;
    var net = tree.net || null;
    var byId = {}, idName = {}, powerById = {}, links = [], seen = {};
    function node(id, name, power, color) {
      if (!byId[id]) {
        byId[id] = { name: id, itemStyle: { color: color } };
        idName[id] = name; powerById[id] = power || 0;
      }
      return id;
    }
    function link(s, t, v) {
      v = Math.round(v || 0);
      if (v > 1 && s !== t && !seen[s + '>' + t]) { seen[s + '>' + t] = 1; links.push({ source: s, target: t, value: v }); }
    }
    var W = function (n) { return Math.max(0, +(n && n.power) || 0); };

    if (net && net.mains_key) {
      // PLC net model (same wiring the on-device console shows).
      var MAINS = node('__mains', __t('cons.sankeyMains', 'Red'), net.raw, SK.mains);
      var CBASE = node('__cbase', __t('cons.sankeyCompute', 'Base informática'), net.base, SK.cbase);
      var HOME = node('__home', __t('cons.sankeyHome', 'Instalación'), net.net, SK.home);
      link(MAINS, CBASE, net.base || 0);
      link(MAINS, HOME, net.net || 0);
      (function walk(n, isRoot) {
        (n.children || []).forEach(function (ch) {
          var id = node(ch.key, ch.name || ch.key, W(ch), ch.is_base ? SK.cbase : SK.load);
          var parent = isRoot ? (ch.is_base ? CBASE : HOME)
                              : node(n.key, n.name || n.key, W(n), SK.load);
          link(parent, id, W(ch));
          walk(ch, false);
        });
      })(tree.root, true);
      node('__baseline', __t('cons.sankeyBaseline', 'Base (luces, standby…)'), net.clean, SK.baseline);
      link(HOME, '__baseline', net.clean || 0);
    } else {
      // Offline fallback: persisted wiring, meter power, no net model.
      (function walk(n, parentId) {
        if (!n) return;
        var virtual = n.virtual || n.kind === 'virtual';
        var cp = parentId;
        if (!virtual && n.key) {
          node(n.key, n.name || n.key, W(n), parentId ? SK.load : SK.mains);
          if (parentId) link(parentId, n.key, W(n));
          cp = n.key;
        }
        (n.children || []).forEach(function (ch) { walk(ch, cp); });
      })(tree.root, null);
      var rk = tree.root && tree.root.key;
      if (rk && byId[rk]) {
        var childSum = links.filter(function (l) { return l.source === rk; })
          .reduce(function (s, l) { return s + l.value; }, 0);
        var resto = W(tree.root) - childSum;
        if (resto > 8) {
          node('__resto', __t('cons.sankeyBase', 'Resto (no medido)'), resto, SK.baseline);
          link(rk, '__resto', resto);
        }
      }
    }

    if (links.length < 1) { c.clear(); skSig = ''; q('cons-sankey-total').textContent = ''; return; }

    // Publish this tick's names/watts for the (already-bound) formatters.
    skIdName = idName; skPowerById = powerById;
    var data = Object.keys(byId).map(function (id) { return byId[id]; });
    var sig = Object.keys(byId).sort().join('|') + '##' +
      links.map(function (l) { return l.source + '>' + l.target; }).sort().join('|');

    if (sig === skSig) {
      // Same wiring → merge values in place: no clear, no re-layout, no flash.
      c.setOption({ series: [{ data: data, links: links }] });
    } else {
      skSig = sig;
      c.clear();
      c.setOption({
        tooltip: {
          trigger: 'item',
          formatter: function (p) {
            if (p.dataType === 'edge')
              return (skIdName[p.data.source] || '') + ' → ' + (skIdName[p.data.target] || '') +
                '<br><b>' + Math.round(p.data.value) + ' W</b>';
            var w = skPowerById[p.name];
            return (skIdName[p.name] || p.name) + (w != null ? '<br><b>' + Math.round(w) + ' W</b>' : '');
          },
        },
        series: [{
          type: 'sankey', left: 4, right: 96, top: 8, bottom: 8,
          nodeAlign: 'left', nodeGap: 10, nodeWidth: 11,
          draggable: false, emphasis: { focus: 'adjacency' },
          label: {
            fontSize: 10, color: '#22384c',
            formatter: function (p) {
              var w = skPowerById[p.name];
              return (skIdName[p.name] || p.name) + (w != null && w >= 1 ? '  ' + wLabel(w) : '');
            },
          },
          lineStyle: { color: 'gradient', opacity: 0.38, curveness: 0.5 },
          data: data,
          links: links,
        }],
      }, true);
    }
    var totW = (net && net.raw) || W(tree.root);
    q('cons-sankey-total').textContent = totW ? '· ' + (totW / 1000).toFixed(2) + ' kW' : '';
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

  var resizeTO;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTO);
    resizeTO = setTimeout(function () {
      Object.keys(charts).forEach(function (id) { charts[id].resize(); });
    }, 120);
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
      // Continuous refresh: day view 60s; month + PRO cards 5 min; the live
      // Sankey (current consumption) ticks every 5s for a real-time feel.
      setInterval(reloadFast, 60000);
      setInterval(reloadSlow, 300000);
      setInterval(loadSankey, 5000);
    });
  });
})();
