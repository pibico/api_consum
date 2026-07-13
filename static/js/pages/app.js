/**
 * app.js — CONSUM-IA Panel v2: GENERAL household numbers (whole-house
 * Medidor General) + the personalized exogenous environment.
 * Data: /consumption/{context,summary,day,month,environment}.
 * No per-sensor detail here — the breakdown lives in Consumo / Mi PLC.
 * The house chart has its own day navigator (KPIs always show TODAY);
 * the price card toggles market (PVPC regulated / OMIE spot for indexed
 * contracts, persisted) and day (today / tomorrow when published).
 * Continuous refresh: house numbers 60s, environment 5 min.
 */
(function () {
  'use strict';

  var MKT_STORE = 'consum_market';
  var customer = '';       // '' = all my households
  var ctx = null;          // /consumption/context payload (role/tier)
  var env = null;          // /consumption/environment payload
  var houseData = null;    // /consumption/day payload for the chart's day
  var houseDate = null;    // chart day (YYYY-MM-DD); KPIs always use today
  var pvpcDay = 'today';   // price chart day toggle
  var market = 'pvpc';     // 'pvpc' | 'omie' (contract-dependent)
  var marketOverride = false;  // explicit user choice wins over the contract
  var contractInfo = null;     // /contracts/active payload {contract, price_now}
  try {
    var storedMkt = localStorage.getItem(MKT_STORE);
    marketOverride = storedMkt !== null;
    market = storedMkt === 'omie' ? 'omie' : 'pvpc';
  } catch (e) {}

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 1 : dec }); }
  function today() {
    // LOCAL calendar date — toISOString() is UTC and made the panel serve
    // yesterday between 00:00 and 02:00 CEST (backend + DB run Europe/Madrid).
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
  }
  function custQS(sep) { return customer ? ((sep || '?') + 'customer=' + encodeURIComponent(customer)) : ''; }

  // Local fetch for WRITES: unlike App.apiFetch it does NOT log the user out on
  // 403 (which would happen mid-edit), so a role error surfaces as a message.
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
          err.status = r.status; throw err;
        }
        return body;
      });
    });
  }
  function periodColor(p, a) {
    var alpha = a == null ? 0.75 : a;
    if (p === 'P1') return 'rgba(231,76,60,' + alpha + ')';
    if (p === 'P2') return 'rgba(243,156,18,' + alpha + ')';
    if (p === 'P3') return 'rgba(46,204,113,' + alpha + ')';
    return 'rgba(70,130,180,' + alpha + ')';   // no band (OMIE spot)
  }

  // ECharts instances (hover tooltips on every chart) — one per container.
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

  // ── Context: household selector + tier badge ─────────────────────────
  function loadContext() {
    return App.apiFetch('/consumption/context').then(function (c) {
      ctx = c;
      var tb = q('tier-badge');
      if (tb) {
        tb.textContent = (c.tier || 'basic').toUpperCase() + (c.ai_enabled ? ' · IA' : '');
        tb.style.display = '';
        tb.className = 'badge ' + (c.tier === 'basic' ? 'badge-info' : 'badge-ok');
      }
      // A household IS an enrolled gateway (one per home); value = its
      // customer slug (tenancy key), label = the gateway hostname.
      var sel = q('customer-select');
      var gateways = (c.devices || []).filter(function (d) {
        return (d.device_type || '') === 'gateway' || !d.device_type;
      });
      if (sel && gateways.length > 1) {
        sel.innerHTML = '<option value="">' + __t('app.allHomes', 'Todos mis hogares') + '</option>' +
          gateways.map(function (d) {
            return '<option value="' + d.customer + '">' + d.hostname + '</option>';
          }).join('');
        sel.style.display = '';
      }
    });
  }

  // ── General-meter KPIs (always TODAY) ─────────────────────────────────
  function loadPower() {
    // Lightweight live power (last EM reading; the meter publishes ~1/min)
    return App.apiFetch('/consumption/current' + custQS()).then(function (r) {
      q('kpi-power').textContent = fmt(r.total_w, 0);
    });
  }

  function loadToday() {
    return App.apiFetch('/consumption/day?date=' + today() + custQS('&')).then(function (r) {
      q('kpi-today').textContent = fmt(r.total_kwh);
      q('kpi-cost-today').textContent = fmt(r.total_cost_eur, 2);
      if (houseDate === r.date) { houseData = r; drawHouseChart(); }
    });
  }

  function loadHouseDay() {
    if (houseDate === today()) return;   // loadToday already feeds the chart
    return App.apiFetch('/consumption/day?date=' + houseDate + custQS('&')).then(function (r) {
      houseData = r;
      drawHouseChart();
    });
  }

  function loadMonth() {
    var m = today().slice(0, 7);
    return App.apiFetch('/consumption/month?month=' + m + custQS('&')).then(function (r) {
      q('kpi-month').textContent = fmt(r.total_kwh, 1) + ' kWh';
      q('kpi-month-cost').textContent = fmt(r.total_cost_eur, 2) + ' €';
    });
  }

  function shiftHouseDay(days) {
    var d = new Date(houseDate + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + days);
    var target = d.toISOString().slice(0, 10);
    if (target > today()) return;
    houseDate = target;
    q('pnl-house-date').value = target;
    if (houseDate === today()) { loadToday().catch(function () {}); }
    else { loadHouseDay(); }
  }

  // ── House chart: hourly kWh bars colored by tariff period ─────────────
  // Day total (sum of the bars shown): kWh + € so the chart carries its own
  // bottom line, not just per-hour bars.
  function updateHouseTotal() {
    var el = q('pnl-house-total');
    if (!el || !houseData) return;
    var kwh = houseData.total_kwh, cost = houseData.total_cost_eur;
    var parts = [];
    if (kwh != null) parts.push('<b>' + fmt(kwh, 2) + '</b> kWh');
    if (cost != null) parts.push('<b>' + fmt(cost, 2) + '</b> €');
    el.innerHTML = parts.join(' · ');
  }

  function drawHouseChart() {
    var c = chart('pnl-house-chart');
    if (!c || !houseData) return;
    updateHouseTotal();
    // Quarter-hourly curve (96 bars) when the API sends it; fall back to the
    // hourly 24-bar view for older payloads (deploy-order safe).
    var quarters = houseData.quarters || null;
    if (quarters && quarters.length) return drawHouseChartQuarter(c, quarters);
    var byHour = {};
    (houseData.values || []).forEach(function (v) { byHour[v.hour] = v; });
    var hours = [];
    for (var h = 0; h < 24; h++) hours.push(h);
    c.clear();
    c.setOption({
      grid: { left: 44, right: 8, top: 12, bottom: 22 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var i = params[0].dataIndex, v = byHour[i];
          if (!v) return String(i).padStart(2, '0') + ':00 — ' + __t('common.noData', 'Sin datos');
          return '<b>' + String(i).padStart(2, '0') + ':00–' + String(i + 1).padStart(2, '0') + ':00</b><br>' +
            fmt(v.kwh, 3) + ' kWh' + (v.period ? ' · ' + v.period : '') +
            (v.price_eur_kwh != null ? '<br>' + v.price_eur_kwh.toFixed(4) + ' €/kWh' : '') +
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

  function drawHouseChartQuarter(c, quarters) {
    // 96 fixed slots 00:00..23:45 so the axis is stable even with gaps.
    var byQ = {};
    quarters.forEach(function (v) { byQ[v.time] = v; });
    var slots = [];
    for (var h = 0; h < 24; h++)
      for (var m = 0; m < 60; m += 15)
        slots.push(String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0'));
    var nextLabel = function (t) {
      var hh = parseInt(t.slice(0, 2), 10), mm = parseInt(t.slice(3), 10) + 15;
      if (mm >= 60) { mm = 0; hh = (hh + 1) % 24; }
      return String(hh).padStart(2, '0') + ':' + String(mm).padStart(2, '0');
    };
    c.clear();   // switching 24↔96 categories: rebuild, don't merge
    c.setOption({
      grid: { left: 44, right: 8, top: 12, bottom: 22 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var t = slots[params[0].dataIndex], v = byQ[t];
          if (!v) return t + ' — ' + __t('common.noData', 'Sin datos');
          return '<b>' + t + '–' + nextLabel(t) + '</b><br>' +
            fmt(v.kwh, 3) + ' kWh' + (v.period ? ' · ' + v.period : '') +
            (v.price_eur_kwh != null ? '<br>' + v.price_eur_kwh.toFixed(4) + ' €/kWh' : '') +
            (v.cost_eur != null ? ' · <b>' + v.cost_eur.toFixed(3) + ' €</b>' : '');
        },
      }),
      xAxis: { type: 'category', data: slots,
               axisLabel: Object.assign({ interval: 7 }, AXIS),   // label every 2 h
               axisTick: { show: false } },
      yAxis: { type: 'value', axisLabel: AXIS, splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [{
        type: 'bar', barWidth: '85%',
        data: slots.map(function (t) {
          var v = byQ[t];
          return { value: v ? v.kwh : 0, itemStyle: { color: periodColor(v && v.period) } };
        }),
      }],
    });
  }

  // ── Price chart: PVPC (24 h bands) | OMIE (96 × 15 min spot) ──────────
  function marketPrices(day) {
    return ((env && env[market]) || {})[day] || [];
  }

  function labelOf(p) {
    // PVPC rows carry {hour}, OMIE rows carry {time: 'HH:MM'}
    return p.time || (String(p.hour).padStart(2, '0') + ':00');
  }

  function currentPoint(prices) {
    var d = new Date();
    if (!prices.length) return null;
    if (prices[0].time) {   // OMIE quarter-hour
      var t = String(d.getHours()).padStart(2, '0') + ':' +
        String(Math.floor(d.getMinutes() / 15) * 15).padStart(2, '0');
      return prices.filter(function (p) { return p.time === t; })[0] || null;
    }
    var h = d.getHours();
    return prices.filter(function (p) { return p.hour === h; })[0] || null;
  }

  function drawPvpcChart() {
    var c = chart('pnl-pvpc-chart');
    if (!c || !env) return;
    var prices = marketPrices(pvpcDay);
    var note = q('pnl-pvpc-note');
    if (!prices.length) {
      note.textContent = __t('common.noData', 'Sin datos');
      c.clear();
      return;
    }
    var isToday = pvpcDay === 'today';
    var cur = isToday ? currentPoint(prices) : null;
    var pMin = Infinity, pMax = -Infinity, minL = '', maxL = '';
    prices.forEach(function (p) {
      if (p.price_eur_kwh < pMin) { pMin = p.price_eur_kwh; minL = labelOf(p); }
      if (p.price_eur_kwh > pMax) { pMax = p.price_eur_kwh; maxL = labelOf(p); }
    });
    var isOmie = !!prices[0].time;
    c.clear();   // switching 24↔96 categories: rebuild, don't merge
    c.setOption({
      grid: { left: 52, right: 8, top: 12, bottom: 22 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var p = prices[params[0].dataIndex];
          return '<b>' + labelOf(p) + '</b> — ' + p.price_eur_kwh.toFixed(4) + ' €/kWh' +
            (p.period ? ' (' + p.period + ')' : '');
        },
      }),
      xAxis: { type: 'category', data: prices.map(labelOf),
               axisLabel: Object.assign({ interval: isOmie ? 15 : 2 }, AXIS),
               axisTick: { show: false } },
      yAxis: { type: 'value', axisLabel: Object.assign({ formatter: function (v) { return v.toFixed(2); } }, AXIS),
               splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [{
        type: 'bar', barWidth: isOmie ? '85%' : '72%',
        data: prices.map(function (p) {
          var current = cur && labelOf(p) === labelOf(cur);
          return { value: p.price_eur_kwh,
                   itemStyle: { color: periodColor(p.period, current ? 1 : 0.75),
                                borderColor: current ? '#2c5171' : undefined,
                                borderWidth: current ? 1.5 : 0 } };
        }),
      }],
    });
    note.innerHTML =
      (cur ? ('<b>' + __t('app.now', 'Ahora') + ' ' + cur.price_eur_kwh.toFixed(4) + ' €/kWh' +
        (cur.period ? ' (' + cur.period + ')' : '') + '</b> · ') : '') +
      __t('app.cheapest', 'Mín') + ' ' + minL + ' ' + pMin.toFixed(4) + ' · ' +
      __t('app.priciest', 'Máx') + ' ' + maxL + ' ' + pMax.toFixed(4) +
      ' · ' + __t('app.source', 'Fuente') + ': ' + (sources()[market] || 'ESIOS');
  }

  function updatePriceKpi() {
    // With a non-PVPC contract, "Precio ahora" is what THIS household pays
    // right now (fixed period price / OMIE+margin), not the raw market view.
    var pn = contractInfo && contractInfo.contract && contractInfo.price_now;
    if (pn && pn.price_eur_kwh != null && pn.source !== 'pvpc') {
      q('kpi-market').textContent = pn.source === 'fixed'
        ? __t('app.srcFixed', 'FIJA') : __t('app.srcIndexed', 'INDEX');
      q('kpi-pvpc').textContent = pn.price_eur_kwh.toFixed(3);
      q('kpi-period').textContent = pn.period || '—';
      q('kpi-period-wrap').style.display = pn.period ? '' : 'none';
      return;
    }
    if (!env) return;
    var now = currentPoint(marketPrices('today'));
    q('kpi-market').textContent = market.toUpperCase();
    if (now) {
      q('kpi-pvpc').textContent = now.price_eur_kwh.toFixed(3);
      q('kpi-period').textContent = now.period || '—';
      q('kpi-period-wrap').style.display = now.period ? '' : 'none';
    }
  }

  function updateHouseSource() {
    var el = q('house-source');
    if (!el) return;
    var c = contractInfo && contractInfo.contract;
    if (c && c.contract_type === 'fixed') {
      el.textContent = __t('app.houseSourceFixed', 'Medidor General de tu casa · coste con tu tarifa fija')
        + (c.retailer ? ' (' + c.retailer + ')' : '');
    } else if (c && c.contract_type === 'indexed') {
      el.textContent = __t('app.houseSourceIndexed', 'Medidor General de tu casa · coste indexado OMIE + margen')
        + (c.retailer ? ' (' + c.retailer + ')' : '');
    } else {
      el.textContent = __t('app.houseSource', 'Medidor General de tu casa · coste con PVPC (ESIOS)');
    }
  }

  function loadContract() {
    return App.apiFetch('/contracts/active' + custQS()).then(function (r) {
      contractInfo = r || null;
      var c = contractInfo && contractInfo.contract;
      if (!marketOverride) {
        market = (c && c.contract_type === 'indexed') ? 'omie' : 'pvpc';
      }
      syncPriceButtons();
      updatePriceKpi();
      updateHouseSource();
    }).catch(function () { contractInfo = null; });
  }

  function syncPriceButtons() {
    q('pnl-mkt-pvpc').classList.toggle('active', market === 'pvpc');
    q('pnl-mkt-omie').classList.toggle('active', market === 'omie');
    var hasTomorrow = !!(marketPrices('tomorrow') || []).length;
    q('pnl-pvpc-tomorrow').disabled = !hasTomorrow;
    if (!hasTomorrow && pvpcDay === 'tomorrow') pvpcDay = 'today';
    q('pnl-pvpc-today').classList.toggle('active', pvpcDay === 'today');
    q('pnl-pvpc-tomorrow').classList.toggle('active', pvpcDay === 'tomorrow');
  }

  function setMarket(m) {
    market = m;
    marketOverride = true;
    try { localStorage.setItem(MKT_STORE, m); } catch (e) {}
    syncPriceButtons();
    updatePriceKpi();
    drawPvpcChart();
  }

  function setPvpcDay(d) {
    pvpcDay = d;
    syncPriceButtons();
    drawPvpcChart();
  }

  // ── Environment cards: weather / solar / green window ────────────────
  var DOW = { es: ['Dom', 'Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb'],
              en: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] };

  function dayLabel(dateStr, i) {
    if (i === 0) return __t('app.today', 'Hoy');
    if (i === 1) return __t('app.tomorrow', 'Mañana');
    var lang = (window.i18n && window.i18n.getLang()) || 'es';
    return (DOW[lang] || DOW.es)[new Date(dateStr + 'T12:00:00').getDay()];
  }

  // ── Location map — Windy embed (radar overlay): unlimited zoom down to
  //    the house, animated forecast timeline, marker on the home. Replaces
  //    the RainViewer/Leaflet radar (tiles capped the useful zoom). ──
  function renderMap() {
    var loc = env && env.location;
    var el = q('pnl-map');
    if (!el || !loc || el.dataset.ready) return;
    el.dataset.ready = '1';
    q('pnl-map-muni').textContent = loc.municipality ? '· ' + loc.municipality : '';
    var src = 'https://embed.windy.com/embed2.html' +
      '?lat=' + loc.lat + '&lon=' + loc.lon +
      '&detailLat=' + loc.lat + '&detailLon=' + loc.lon +
      '&zoom=10&level=surface&overlay=radar&menu=&message=&marker=true' +
      '&calendar=now&pressure=&type=map&location=coordinates' +
      '&metricWind=km%2Fh&metricTemp=%C2%B0C&radarRange=-1';
    el.innerHTML = '<iframe title="Windy" src="' + src + '" loading="lazy" ' +
      'style="width:100%;height:100%;border:0;border-radius:8px;display:block;"></iframe>';
  }

  // Per-card provenance line ("Fuente: AEMET · estación Gijón").
  function srcLine(txt) {
    return '<div class="pnl-src">' + __t('app.source', 'Fuente') + ': ' + txt + '</div>';
  }
  function sources() { return (env && env.sources) || {}; }
  // Location suffix for source lines — makes it clear WHICH place the data is
  // for (household municipality, or the default when the home has no coords).
  function locSuffix() {
    var m = ((env && env.location) || {}).municipality;
    return m ? ' · ' + m : '';
  }

  // Weather glyph from the AEMET/OpenMeteo description — inline SVG
  // (Phosphor style, NO emoji) so a quick glance reads the week.
  function wxIcon(desc) {
    var d = (desc || '').toLowerCase();
    function svg(color, inner) {
      return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="' + color +
        '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>';
    }
    var SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
    var CLOUD = '<path d="M17.5 19H7a4 4 0 1 1 .6-7.96A5.5 5.5 0 0 1 18 9.5a4.5 4.5 0 0 1-.5 9.5z"/>';
    var SUNCLOUD = '<circle cx="7" cy="7" r="3"/><path d="M7 1v1.5M1.5 7H3M3.2 3.2l1 1M11 7h-1.5" opacity="0.9"/><path d="M18.5 20H9a3.5 3.5 0 1 1 .5-6.97A5 5 0 0 1 19 11a4 4 0 0 1-.5 9z"/>';
    if (/tormenta|storm|thunder/.test(d)) {
      return svg('#c49a18', CLOUD + '<path d="M12 19l-1.5 3M13.5 19l-1 2 2 0-1 2" stroke="#e67e22"/>');
    }
    if (/nieve|snow|granizo|hail/.test(d)) {
      return svg('#6ab4f0', CLOUD + '<path d="M9 21v.01M13 21v.01M11 23v.01M15 22v.01" stroke="#6ab4f0"/>');
    }
    if (/lluvia|chubasco|rain|drizzle|shower/.test(d)) {
      return svg('#2e82c8', CLOUD + '<path d="M9 21l-.7 1.6M13 21l-.7 1.6M16.5 21l-.7 1.6" stroke="#2e82c8"/>');
    }
    if (/niebla|bruma|calima|fog|mist|haze/.test(d)) {
      return svg('#8e96a8', '<path d="M4 9h16M3 13h18M5 17h14"/>');
    }
    if (/despejado|clear|sunny|soleado/.test(d)) {
      return svg('#c49a18', SUN);
    }
    if (/poco nuboso|intervalos|partly|parcial/.test(d)) {
      return svg('#5a6478', SUNCLOUD);
    }
    // nuboso / muy nuboso / cubierto / cloudy / overcast — plain cloud
    return svg('#5a6478', CLOUD);
  }

  // Rain-probability droplet (labels the % so it isn't a mystery number).
  var DROPLET_SVG = '<svg viewBox="0 0 24 24" width="9" height="9" fill="#2e82c8" stroke="none">' +
    '<path d="M12 2.5S5.5 10 5.5 15a6.5 6.5 0 0 0 13 0C18.5 10 12 2.5 12 2.5z"/></svg>';

  function renderWeather() {
    var wx = (env && env.weather) || {};
    var days = wx.days || [];
    // Show the household municipality so it's clear the forecast is pinned to
    // your home's coordinates (not the viewer's browser location).
    var loc = (env && env.location) || {};
    var muniEl = q('pnl-wx-muni');
    if (muniEl) muniEl.textContent = loc.municipality ? '· ' + loc.municipality : '';
    if (!days.length && !wx.now) {
      q('pnl-weather').innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      return;
    }
    var html = '';
    if (wx.now && wx.now.temperature != null) {
      html += '<div class="pnl-wx-now">' +
        '<span class="pnl-wx-ico">' + wxIcon(wx.now.description) + '</span>' +
        '<span class="pnl-wx-now-temp">' + fmt(wx.now.temperature, 1) + '°</span>' +
        '<span class="pnl-wx-now-desc">' + (wx.now.description || '') +
        (wx.now.humidity != null ? ' · ' + fmt(wx.now.humidity, 0) + '% ' + __t('app.humidityShort', 'humedad') : '') +
        '</span></div>';
    }
    // 7-day forecast cards (api_exo representation): every value labelled —
    // day, icon, high/low temps (red/blue) and rain probability with a droplet
    // so the % is never a mystery. Trend detail on the temp chart's hover below.
    var todayStr = (function () { var n = new Date();
      return n.getFullYear() + '-' + String(n.getMonth() + 1).padStart(2, '0') + '-' + String(n.getDate()).padStart(2, '0'); })();
    html += '<div class="pnl-wx-strip">' + days.map(function (d, i) {
      return '<div class="pnl-wx-cell' + (d.date === todayStr ? ' is-today' : '') + '" title="' + (d.description || '') + '">' +
        '<span class="pnl-wx-cell-day">' + dayLabel(d.date, i) + '</span>' +
        '<span class="pnl-wx-cell-ico">' + wxIcon(d.description) + '</span>' +
        '<span class="pnl-wx-cell-temps">' +
          '<span class="pnl-wx-cell-hi">' + (d.temp_max != null ? Math.round(d.temp_max) + '°' : '—') + '</span>' +
          (d.temp_min != null ? '<span class="pnl-wx-cell-lo">' + Math.round(d.temp_min) + '°</span>' : '') +
        '</span>' +
        (d.precipitation_prob != null
          ? '<span class="pnl-wx-cell-rain" title="' + __t('app.rainProb', 'probabilidad de lluvia') + '">' +
            DROPLET_SVG + fmt(d.precipitation_prob, 0) + '%</span>'
          : '') +
        '</div>';
    }).join('') + '</div>';
    q('pnl-weather').innerHTML = html;
    var s = sources();
    q('pnl-wx-src').innerHTML = srcLine((s.weather || 'AEMET') +
      (s.weather_station ? ' · ' + __t('app.station', 'estación') + ' ' + s.weather_station : ''));
    drawWxChart(days);
  }

  // 7-day high/low temperature chart — api_exo weather-page parity: red max
  // and blue min lines with the band between them (stacked base+delta),
  // point labels, and a hover carrying the day's full story.
  function drawWxChart(days) {
    var c = chart('pnl-wx-chart');
    if (!c) return;
    if (!days.length) { c.clear(); return; }
    var all = [];
    days.forEach(function (d) { all.push(d.temp_max, d.temp_min); });
    var mn = Math.min.apply(null, all) - 3, mx = Math.max.apply(null, all) + 3;
    c.setOption({
      grid: { left: 34, right: 14, top: 18, bottom: 20 },
      tooltip: Object.assign({}, TOOLTIP, {
        axisPointer: { type: 'line' },
        formatter: function (params) {
          var d = days[params[0].dataIndex];
          return '<b>' + dayLabel(d.date, params[0].dataIndex) + ' · ' + d.date + '</b><br>' +
            (d.description || '') + '<br>' +
            __t('app.tMax', 'Máx') + ' ' + Math.round(d.temp_max) + '° · ' +
            __t('app.tMin', 'Mín') + ' ' + Math.round(d.temp_min) + '°' +
            (d.precipitation_prob != null
              ? ' · ' + fmt(d.precipitation_prob, 0) + '% ' + __t('app.rainShort', 'lluvia') : '');
        },
      }),
      xAxis: { type: 'category', boundaryGap: false,
               data: days.map(function (d, i) { return dayLabel(d.date, i); }),
               axisLabel: AXIS, axisTick: { show: false } },
      yAxis: { type: 'value', min: Math.floor(mn), max: Math.ceil(mx),
               axisLabel: Object.assign({ formatter: '{value}°' }, AXIS),
               splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [
        { type: 'line', stack: 'band', symbol: 'none', silent: true,
          data: days.map(function (d) { return d.temp_min; }),
          lineStyle: { opacity: 0 }, tooltip: { show: false } },
        { type: 'line', stack: 'band', symbol: 'none', silent: true,
          data: days.map(function (d) { return d.temp_max - d.temp_min; }),
          lineStyle: { opacity: 0 }, areaStyle: { color: 'rgba(44,107,90,0.12)' },
          tooltip: { show: false } },
        { type: 'line', symbol: 'circle', symbolSize: 5,
          data: days.map(function (d) { return d.temp_max; }),
          lineStyle: { color: '#e74c3c', width: 2 }, itemStyle: { color: '#e74c3c' },
          label: { show: true, position: 'top', fontSize: 9,
                   formatter: function (p) { return Math.round(p.value) + '°'; } } },
        { type: 'line', symbol: 'circle', symbolSize: 5,
          data: days.map(function (d) { return d.temp_min; }),
          lineStyle: { color: '#3498db', width: 2 }, itemStyle: { color: '#3498db' },
          label: { show: true, position: 'bottom', fontSize: 9,
                   formatter: function (p) { return Math.round(p.value) + '°'; } } },
      ],
    }, true);
  }

  // Installed-kWp editor in the solar card header: reflects the household's
  // stored PV capacity and, on change, persists it and refetches the forecast
  // so the estimate is scaled to the real installation (not a 3 kWp default).
  function bindSolarKwp() {
    var el = q('pnl-solar-kwp');
    if (!el || el._bound) return;
    el._bound = true;
    // Any authenticated household member may set their own installed kWp (it
    // only scales the estimate) — no role gate, so the input stays editable.
    cfetch('/consumption/solar-config' + custQS()).then(function (c) {
      if (c && c.peak_kwp != null && document.activeElement !== el) el.value = c.peak_kwp;
    }).catch(function () {});
    el.onchange = function () {
      var v = parseFloat(el.value);
      if (isNaN(v) || v < 0 || v > 100) { return; }
      el.disabled = true;
      cfetch('/consumption/solar-config', {
        method: 'PUT',
        body: JSON.stringify(Object.assign({ peak_kwp: v },
          customer ? { customer: customer } : {})),
      }).then(function () {
        return loadEnvironment();   // refetch solar scaled to the new kWp
      }).then(function () {
        App.showNotification(__t('common.saved', 'Guardado'),
          __t('app.solarKwpSaved', 'Estimación solar actualizada a {kwp} kWp').replace('{kwp}', v), 'success');
      }).catch(function (e) {
        App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
      }).finally(function () { el.disabled = false; });
    };
  }

  function renderSolar() {
    bindSolarKwp();
    var s = (env && env.solar) || {};
    // Keep the input in sync with the effective kWp (e.g. after a reload) when
    // the user isn't actively editing it.
    var kwpEl = q('pnl-solar-kwp');
    if (kwpEl && s.peak_kwp != null && document.activeElement !== kwpEl && !kwpEl.value) {
      kwpEl.value = fmt(s.peak_kwp, 1);
    }
    if (s.today_kwh == null) {
      q('pnl-solar').innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      return;
    }
    var pk = s.peak_window || {};
    var pkTxt = '';
    if (pk.start) {
      pkTxt = '<div class="sav-explain" style="margin-top:4px;">' +
        __t('app.solarPeak', 'Mejores horas de sol') + ': <b>' +
        pk.start.slice(11, 16) + '–' + pk.end.slice(11, 16) + '</b> (' + pk.start.slice(5, 10) + ')</div>';
    }
    var sun = s.sun || {};
    var sunTxt = '';
    if (sun.sunrise && sun.sunset) {
      var hrs = sun.daylight_seconds != null ? (sun.daylight_seconds / 3600) : null;
      sunTxt = '<div class="sav-explain">' +
        __t('app.sunrise', 'Amanece') + ' <b>' + sun.sunrise.slice(11, 16) + '</b> · ' +
        __t('app.sunset', 'Anochece') + ' <b>' + sun.sunset.slice(11, 16) + '</b>' +
        (hrs != null ? ' (' + fmt(hrs, 1) + ' ' + __t('app.daylightHours', 'h de luz') + ')' : '') +
        '</div>';
    }
    q('pnl-solar').innerHTML =
      '<div style="display:flex;gap:1.4rem;align-items:baseline;flex-wrap:wrap;">' +
      '<div><span class="kpi-value" style="font-size:1.5rem;color:#c49a18;">' + fmt(s.today_kwh, 1) + '</span>' +
      ' <span class="kpi-sub">kWh ' + __t('app.today', 'hoy').toLowerCase() + '</span></div>' +
      '<div><span class="kpi-value" style="font-size:1.5rem;color:#c49a18;opacity:0.75;">' + fmt(s.tomorrow_kwh, 1) + '</span>' +
      ' <span class="kpi-sub">kWh ' + __t('app.tomorrow', 'mañana').toLowerCase() + '</span></div>' +
      '<div class="kpi-sub">' + __t('app.solarExplain', 'estimado para {kwp} kWp orientación sur')
        .replace('{kwp}', fmt(s.peak_kwp, 1)) + '</div>' +
      '</div>' + sunTxt + pkTxt +
      srcLine((sources().solar || 'Open-Meteo') + locSuffix());
    drawSolarChart(s.hourly || []);
  }

  // 48 h production chart — api_exo solar-page parity: bars kW tinted by
  // cloud cover (clear=orange → cloudy=grey-blue), day separators and a
  // "now" line; hover = kW + GHI + clouds.
  function drawSolarChart(hourly) {
    var c = chart('pnl-solar-chart');
    if (!c) return;
    if (!hourly.length) { c.clear(); return; }
    var now = new Date();
    var nowIdx = null, seps = [];
    var cats = hourly.map(function (h, i) {
      var d = new Date(h.ts);
      if (d.getHours() === 0 && i > 0) seps.push(i);
      if (nowIdx === null && d.getDate() === now.getDate() && d.getHours() === now.getHours()) nowIdx = i;
      return String(d.getHours()).padStart(2, '0') +
        (d.getHours() === 0 && i > 0 ? ' ' + dayLabel(String(h.ts).slice(0, 10), 1) : '');
    });
    var marks = seps.map(function (i) {
      return { xAxis: i, lineStyle: { type: 'dashed', color: 'rgba(0,0,0,0.18)', width: 1 } };
    });
    if (nowIdx !== null) {
      marks.push({ xAxis: nowIdx, lineStyle: { type: 'solid', color: '#2c5171', width: 2 } });
    }
    var lang = (window.i18n && window.i18n.getLang()) || 'es';
    c.setOption({
      grid: { left: 42, right: 6, top: 8, bottom: 18 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var h = hourly[params[0].dataIndex];
          var d = new Date(h.ts);
          return '<b>' + (DOW[lang] || DOW.es)[d.getDay()] + ' ' +
            String(d.getHours()).padStart(2, '0') + ':00</b><br>' +
            (h.p_kw || 0).toFixed(2) + ' kW · GHI ' + Math.round(h.ghi || 0) + ' W/m²<br>' +
            Math.round(h.cloud_pct || 0) + '% ' + __t('app.clouds', 'nubes');
        },
      }),
      xAxis: { type: 'category', data: cats,
               axisLabel: Object.assign({ interval: 5 }, AXIS), axisTick: { show: false } },
      yAxis: { type: 'value',
               axisLabel: Object.assign({ formatter: function (v) { return v.toFixed(1) + ' kW'; } }, AXIS),
               splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [{
        type: 'bar', barCategoryGap: '10%',
        data: hourly.map(function (h) {
          var cld = h.cloud_pct || 0;
          var alpha = 0.85 - (cld / 100) * 0.4;
          var r = Math.round(230 - (cld / 100) * 80);
          var g = Math.round(126 + (cld / 100) * 40);
          var b = Math.round(34 + (cld / 100) * 100);
          return { value: h.p_kw || 0,
                   itemStyle: { color: 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')' } };
        }),
        markLine: { silent: true, symbol: 'none', label: { show: false }, data: marks },
      }],
    }, true);
  }

  // ── Wind & infiltration (api_exo weather-page parity, in the Panel) ──
  // Hourly wind/gusts forecast + envelope-infiltration advisory. Above the
  // threshold (default 40 km/h) heat losses spike in poorly-sealed homes.
  var windThreshold = parseFloat(localStorage.getItem('consum_wind_threshold') || '40');

  function renderWind() {
    var thEl = q('pnl-wind-threshold');
    if (thEl && !thEl._bound) {
      thEl._bound = true;
      thEl.value = windThreshold;
      thEl.oninput = function () {
        var v = parseFloat(thEl.value);
        if (!isNaN(v) && v >= 20 && v <= 80) {
          windThreshold = v;
          localStorage.setItem('consum_wind_threshold', String(v));
          renderWind();
        }
      };
    }
    var wind = (env && env.wind) || {};
    var hourly = wind.hourly || [];
    if (!hourly.length) {
      q('pnl-wind-kpis').innerHTML = '';
      q('pnl-wind-advisory').innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      q('pnl-wind-src').innerHTML = '';
      var c0 = chart('pnl-wind-chart'); if (c0) c0.clear();
      return;
    }
    drawWindChart(hourly, windThreshold);
    var now = new Date();
    var todayH = hourly.filter(function (h) { return new Date(h.ts).getDate() === now.getDate(); });
    var base = todayH.length ? todayH : hourly.slice(0, 24);
    var peak = base.reduce(function (a, b) { return (b.wind_speed_kmh || 0) > (a.wind_speed_kmh || 0) ? b : a; }, { wind_speed_kmh: 0 });
    var hoursOver = base.filter(function (h) { return (h.wind_speed_kmh || 0) >= windThreshold; }).length;
    var peakTs = peak.ts ? new Date(peak.ts) : null;
    q('pnl-wind-kpis').innerHTML =
      '<span><b>' + __t('app.peakWind', 'Máx hoy') + '</b> ' +
        (peak.wind_speed_kmh ? Math.round(peak.wind_speed_kmh) + ' km/h' + (peakTs ? ' · ' + String(peakTs.getHours()).padStart(2, '0') + ':00' : '') : '—') + '</span>' +
      '<span><b>' + __t('app.hoursOver', 'Horas > umbral') + '</b> ' + hoursOver + ' h</span>' +
      (peak.wind_gusts_kmh ? '<span><b>' + __t('app.windGust', 'Racha') + '</b> ' + Math.round(peak.wind_gusts_kmh) + ' km/h</span>' : '');
    var adv = q('pnl-wind-advisory');
    if (hoursOver > 0) {
      adv.innerHTML = '⚠ ' + __t('app.windAdvHigh', 'Viento fuerte: más pérdidas por infiltración. Revisa sellos de ventanas y cierra persianas en el lado expuesto.');
      adv.style.cssText = 'margin-top:6px;font-size:0.74rem;background:rgba(231,76,60,0.1);border-radius:6px;padding:6px 8px;color:#9e3a4a;';
    } else {
      adv.innerHTML = '✓ ' + __t('app.windAdvLow', 'Viento en calma: sin pérdidas extra por infiltración en la envolvente.');
      adv.style.cssText = 'margin-top:6px;font-size:0.74rem;background:rgba(46,204,113,0.08);border-radius:6px;padding:6px 8px;color:#1a5c3a;';
    }
    q('pnl-wind-src').innerHTML = srcLine((wind.source || 'Open-Meteo') + locSuffix());
  }

  function drawWindChart(hourly, threshold) {
    var c = chart('pnl-wind-chart');
    if (!c) return;
    var labels = hourly.map(function (h) { return String(new Date(h.ts).getHours()).padStart(2, '0') + 'h'; });
    var speed = hourly.map(function (h) { return Math.round(h.wind_speed_kmh || 0); });
    var gusts = hourly.map(function (h) { return Math.round(h.wind_gusts_kmh || 0); });
    c.clear();
    c.setOption({
      grid: { left: 32, right: 10, top: 14, bottom: 20 },
      tooltip: Object.assign({}, TOOLTIP, {
        formatter: function (params) {
          var i = params[0].dataIndex, d = new Date(hourly[i].ts);
          return '<b>' + d.toLocaleDateString('es', { weekday: 'short' }) + ' ' +
            String(d.getHours()).padStart(2, '0') + ':00</b><br>' +
            __t('app.windSpeed', 'Viento') + ' ' + speed[i] + ' km/h · ' +
            __t('app.windGust', 'racha') + ' ' + gusts[i] + ' km/h';
        },
      }),
      xAxis: { type: 'category', data: labels,
               axisLabel: Object.assign({ interval: 5 }, AXIS), axisTick: { show: false } },
      yAxis: { type: 'value', axisLabel: Object.assign({ formatter: '{value}' }, AXIS),
               splitLine: { lineStyle: { opacity: 0.25 } } },
      series: [
        { type: 'line', data: gusts, symbol: 'none', silent: true, lineStyle: { opacity: 0 },
          areaStyle: { color: 'rgba(52,152,219,0.10)' }, tooltip: { show: false } },
        { type: 'line', data: speed, symbol: 'none', smooth: true,
          lineStyle: { color: '#3498db', width: 2 },
          markLine: { silent: true, symbol: 'none',
            lineStyle: { color: '#e74c3c', type: 'dashed' },
            data: [{ yAxis: threshold,
                     label: { formatter: threshold + ' km/h', fontSize: 9, color: '#e74c3c' } }] } },
      ],
    }, true);
  }

  // "Factura en curso" KPI — projected € of the OPEN billing period (derived
  // from the invoice history by /invoices/billing-period, PRO). Hidden until
  // data arrives; on show, the grid widens to 7 tracks. cfetch: a 403 (basic
  // tier) must hide the card, never log the user out.
  function loadBillingKpi() {
    cfetch('/invoices/billing-period' + custQS()).then(function (bp) {
      if (!bp || bp.status !== 'ok') return;
      var v = bp.projected_eur != null ? '~' + bp.projected_eur.toFixed(2) + ' €'
                                       : bp.total_eur.toFixed(2) + ' €';
      q('kpi-bill').textContent = v;
      q('kpi-bill-sub').textContent = __t('app.billDay', 'día {n} de ~{m}')
        .replace('{n}', bp.days_elapsed).replace('{m}', bp.days_total) +
        (bp.projected_eur != null ? ' · ' + __t('app.billEstimate', 'estimado') : '') +
        (bp.incomplete ? ' · ' + __t('app.billIncomplete', 'medición incompleta') : '');
      q('kpi-bill-card').style.display = '';
      q('kpi-grid').style.gridTemplateColumns = 'repeat(7,1fr)';
    }).catch(function () {});
  }

  function loadEnvironment() {
    return App.apiFetch('/consumption/environment' + custQS()).then(function (r) {
      env = r;
      // KPI: carbon (price KPI follows the market selector)
      if (r.carbon && r.carbon.intensity_gco2_kwh != null) {
        q('kpi-carbon').textContent = fmt(r.carbon.intensity_gco2_kwh, 0);
        q('kpi-carbon-band').textContent = __t('app.band.' + (r.carbon.band || ''), r.carbon.band || '—');
      }
      syncPriceButtons();
      updatePriceKpi();
      drawPvpcChart();
      renderMap();
      renderWeather();
      renderSolar();
      renderWind();
    });
  }

  // ── Continuous refresh ────────────────────────────────────────────────
  function refreshHouse() {
    // Midnight rollover on a long-lived tab: advance the picker (and the
    // chart, if it was sitting on "today") to the new local date.
    var t = today();
    var hd = q('pnl-house-date');
    if (hd && hd.max !== t) {
      var wasToday = houseDate === hd.max;
      hd.max = t;
      if (wasToday || !houseDate) { houseDate = t; hd.value = t; }
    }
    loadPower().catch(function () {});
    loadToday().catch(function () {});
    loadHouseDay();
    loadMonth().catch(function () {});
  }

  function refreshEnv() {
    loadContract();                          // tariff may have been edited
    loadEnvironment().catch(function () {});
  }

  window.ConsumApp = {
    onCustomerChange: function () {
      customer = q('customer-select').value || '';
      refreshHouse();
      refreshEnv();
    },
  };

  var _resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function () {
      Object.keys(charts).forEach(function (id) { charts[id].resize(); });
    }, 120);
  });

  document.addEventListener('i18n:changed', function () {
    drawPvpcChart(); renderWeather(); renderSolar(); renderWind();
    updatePriceKpi(); updateHouseSource();
  });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      houseDate = today();
      var hd = q('pnl-house-date');
      hd.value = houseDate;
      hd.max = houseDate;
      hd.onchange = function () {
        houseDate = hd.value || today();
        if (houseDate === today()) { loadToday().catch(function () {}); }
        else { loadHouseDay(); }
      };
      q('pnl-house-prev').onclick = function () { shiftHouseDay(-1); };
      q('pnl-house-next').onclick = function () { shiftHouseDay(1); };
      q('pnl-mkt-pvpc').onclick = function () { setMarket('pvpc'); };
      q('pnl-mkt-omie').onclick = function () { setMarket('omie'); };
      q('pnl-pvpc-today').onclick = function () { setPvpcDay('today'); };
      q('pnl-pvpc-tomorrow').onclick = function () { setPvpcDay('tomorrow'); };
      loadContext().then(function () { loadContract(); refreshHouse(); loadBillingKpi(); loadEnvironment().catch(function () {}); })
        .catch(function (e) {
          App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
        });
      setInterval(function () { loadPower().catch(function () {}); }, 5000);   // live watts tick
      setInterval(refreshHouse, 60000);    // kWh / € of the day+month
      setInterval(refreshEnv, 300000);     // exogenous environment
    });
  });
})();
