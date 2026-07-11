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
  var env = null;          // /consumption/environment payload
  var houseData = null;    // /consumption/day payload for the chart's day
  var houseDate = null;    // chart day (YYYY-MM-DD); KPIs always use today
  var pvpcDay = 'today';   // price chart day toggle
  var market = 'pvpc';     // 'pvpc' | 'omie' (contract-dependent)
  try { market = localStorage.getItem(MKT_STORE) === 'omie' ? 'omie' : 'pvpc'; } catch (e) {}

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 1 : dec }); }
  function today() { return new Date().toISOString().slice(0, 10); }
  function custQS(sep) { return customer ? ((sep || '?') + 'customer=' + encodeURIComponent(customer)) : ''; }

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
  function drawHouseChart() {
    var c = chart('pnl-house-chart');
    if (!c || !houseData) return;
    var byHour = {};
    (houseData.values || []).forEach(function (v) { byHour[v.hour] = v; });
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
    if (!env) return;
    var now = currentPoint(marketPrices('today'));
    q('kpi-market').textContent = market.toUpperCase();
    if (now) {
      q('kpi-pvpc').textContent = now.price_eur_kwh.toFixed(3);
      q('kpi-period').textContent = now.period || '—';
      q('kpi-period-wrap').style.display = now.period ? '' : 'none';
    }
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

  // ── Location map (Leaflet, vendored; Carto light tiles like api_exo) ──
  var map = null;
  function renderMap() {
    var loc = env && env.location;
    var el = q('pnl-map');
    if (!el || !loc || typeof L === 'undefined' || map) {
      if (map && loc) map.setView([loc.lat, loc.lon]);
      return;
    }
    q('pnl-map-muni').textContent = loc.municipality ? '· ' + loc.municipality : '';
    map = L.map('pnl-map', { zoomControl: true, scrollWheelZoom: false })
      .setView([loc.lat, loc.lon], 15);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19,
    }).addTo(map);
    L.circleMarker([loc.lat, loc.lon], {
      radius: 9, color: '#2c5171', weight: 2,
      fillColor: '#4682b4', fillOpacity: 0.85,
    }).addTo(map).bindPopup(loc.municipality || 'Tu hogar');
  }

  // Per-card provenance line ("Fuente: AEMET · estación Gijón").
  function srcLine(txt) {
    return '<div class="pnl-src">' + __t('app.source', 'Fuente') + ': ' + txt + '</div>';
  }
  function sources() { return (env && env.sources) || {}; }

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

  function renderWeather() {
    var wx = (env && env.weather) || {};
    var days = wx.days || [];
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
    // Glance strip: one mini-column per day (icon + rain prob), the detail
    // lives in the temp chart's hover below (api_exo weather-page parity).
    html += '<div class="pnl-wx-strip">' + days.map(function (d, i) {
      return '<div class="pnl-wx-cell" title="' + (d.description || '') + '">' +
        '<span class="pnl-wx-cell-day">' + dayLabel(d.date, i) + '</span>' +
        wxIcon(d.description) +
        '<span class="pnl-wx-cell-rain mono">' +
        (d.precipitation_prob != null ? fmt(d.precipitation_prob, 0) + '%' : '—') + '</span>' +
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

  function renderSolar() {
    var s = (env && env.solar) || {};
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
      srcLine(sources().solar || 'Open-Meteo');
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

  function renderWindow() {
    var w = (env && env.window) || {};
    var best = w.best;
    if (!best || w.status !== 'ok') {
      q('pnl-window').innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      return;
    }
    var isTomorrow = w.date > today();
    q('pnl-window').innerHTML =
      '<div><span class="kpi-value" style="font-size:1.7rem;color:#28946a;">' +
      String(best.start).padStart(2, '0') + ':00–' + String(best.end).padStart(2, '0') + ':00</span>' +
      ' <span class="badge badge-ok">' + (isTomorrow ? __t('app.tomorrow', 'Mañana') : __t('app.today', 'Hoy')) + '</span></div>' +
      '<div class="kpi-sub" style="margin-top:2px;">PVPC ' + __t('app.avgShort', 'medio') + ' ' +
      best.price_avg.toFixed(4) + ' €/kWh</div>' +
      '<div class="sav-explain" style="margin-top:6px;">' +
      __t('app.windowExplain', 'La franja más barata y con más sol: ideal para lavadora, lavavajillas o cargar el coche.') +
      '</div>' +
      srcLine('PVPC ' + (sources().pvpc || 'ESIOS') + ' + ' +
        __t('app.solarShort', 'solar') + ' ' + (sources().solar || 'Open-Meteo'));
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
      renderWindow();
    });
  }

  // ── Continuous refresh ────────────────────────────────────────────────
  function refreshHouse() {
    loadPower().catch(function () {});
    loadToday().catch(function () {});
    loadHouseDay();
    loadMonth().catch(function () {});
  }

  function refreshEnv() {
    loadEnvironment().catch(function () {});
  }

  window.ConsumApp = {
    onCustomerChange: function () {
      customer = q('customer-select').value || '';
      refreshHouse();
      refreshEnv();
    },
  };

  window.addEventListener('resize', function () {
    Object.keys(charts).forEach(function (id) { charts[id].resize(); });
  });

  document.addEventListener('i18n:changed', function () {
    drawPvpcChart(); renderWeather(); renderSolar(); renderWindow();
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
      loadContext().then(function () { refreshHouse(); refreshEnv(); })
        .catch(function (e) {
          App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
        });
      setInterval(function () { loadPower().catch(function () {}); }, 5000);   // live watts tick
      setInterval(refreshHouse, 60000);    // kWh / € of the day+month
      setInterval(refreshEnv, 300000);     // exogenous environment
    });
  });
})();
