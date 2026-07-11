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

  function prep(id, h) {
    var c = q(id);
    if (!c || !c.parentElement.clientWidth) return null;
    var dpr = window.devicePixelRatio || 1;
    var rect = c.parentElement.getBoundingClientRect();
    var W = rect.width - 32, H = h || 190;
    c.width = W * dpr; c.height = H * dpr;
    c.style.width = W + 'px'; c.style.height = H + 'px';
    var ctx = c.getContext('2d');
    ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
    return { ctx: ctx, W: W, H: H };
  }

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
    var cv = prep('pnl-house-chart');
    if (!cv || !houseData) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var padL = 36, padB = 18, padT = 8;
    var vals = houseData.values || [];
    var byHour = {};
    vals.forEach(function (v) { byHour[v.hour] = v; });
    var max = Math.max(0.05, Math.max.apply(null, vals.map(function (v) { return v.kwh; }).concat([0])));
    var chartW = W - padL - 8, chartH = H - padT - padB;
    var bw = chartW / 24;
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'right';
    ctx.fillText(max.toFixed(1), padL - 3, padT + 8);
    ctx.fillText('0', padL - 3, H - padB);
    ctx.textAlign = 'center';
    for (var h = 0; h < 24; h++) {
      var v = byHour[h];
      var x = padL + h * bw;
      if (v && v.kwh > 0) {
        var bh = chartH * (v.kwh / max);
        ctx.fillStyle = periodColor(v.period);
        ctx.fillRect(x + 1, H - padB - bh, bw - 2, bh);
      }
      if (h % 3 === 0) {
        ctx.fillStyle = 'rgba(44,62,80,0.55)';
        ctx.fillText(String(h).padStart(2, '0'), x + bw / 2, H - 5);
      }
    }
  }

  // ── Price chart: PVPC|OMIE 24h curve, current hour highlighted ───────
  function marketPrices(day) {
    return ((env && env[market]) || {})[day] || [];
  }

  function drawPvpcChart() {
    var cv = prep('pnl-pvpc-chart');
    if (!cv || !env) return;
    var prices = marketPrices(pvpcDay);
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var note = q('pnl-pvpc-note');
    if (!prices.length) {
      note.textContent = __t('common.noData', 'Sin datos');
      return;
    }
    var padL = 44, padB = 18, padT = 8;
    var vals = prices.map(function (p) { return p.price_eur_kwh; });
    var pMin = Math.min.apply(null, vals), pMax = Math.max.apply(null, vals);
    var top = pMax * 1.08 || 0.05;
    var chartW = W - padL - 8, chartH = H - padT - padB;
    var bw = chartW / 24;
    var nowH = new Date().getHours();
    var isToday = pvpcDay === 'today';
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'right';
    ctx.fillText(pMax.toFixed(2), padL - 3, padT + 8);
    ctx.fillText('0', padL - 3, H - padB);
    ctx.textAlign = 'center';
    var minH = null, maxH = null;
    prices.forEach(function (p) {
      if (p.price_eur_kwh === pMin && minH == null) minH = p.hour;
      if (p.price_eur_kwh === pMax && maxH == null) maxH = p.hour;
      var x = padL + p.hour * bw;
      var bh = chartH * (p.price_eur_kwh / top);
      var current = isToday && p.hour === nowH;
      ctx.fillStyle = periodColor(p.period, current ? 0.95 : 0.4);
      ctx.fillRect(x + 1, H - padB - bh, bw - 2, bh);
      if (current) {
        ctx.strokeStyle = '#2c5171';
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x + 1, H - padB - bh, bw - 2, bh);
      }
      if (p.hour % 3 === 0) {
        ctx.fillStyle = 'rgba(44,62,80,0.55)';
        ctx.fillText(String(p.hour).padStart(2, '0'), x + bw / 2, H - 5);
      }
    });
    var now = isToday ? prices.filter(function (p) { return p.hour === nowH; })[0] : null;
    note.innerHTML =
      (now ? ('<b>' + __t('app.now', 'Ahora') + ' ' + now.price_eur_kwh.toFixed(3) + ' €/kWh' +
        (now.period ? ' (' + now.period + ')' : '') + '</b> · ') : '') +
      __t('app.cheapest', 'Mín') + ' ' + String(minH).padStart(2, '0') + 'h ' + pMin.toFixed(3) + ' · ' +
      __t('app.priciest', 'Máx') + ' ' + String(maxH).padStart(2, '0') + 'h ' + pMax.toFixed(3);
  }

  function updatePriceKpi() {
    if (!env) return;
    var nowH = new Date().getHours();
    var now = marketPrices('today').filter(function (p) { return p.hour === nowH; })[0];
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

  function renderWeather() {
    var wx = (env && env.weather) || {};
    var days = wx.days || [];
    var muni = env && env.location && env.location.municipality;
    q('pnl-wx-muni').textContent = muni ? '· ' + muni : '';
    if (!days.length && !wx.now) {
      q('pnl-weather').innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      return;
    }
    var html = '';
    if (wx.now && wx.now.temperature != null) {
      html += '<div class="pnl-wx-now">' +
        '<span class="pnl-wx-now-temp">' + fmt(wx.now.temperature, 1) + '°</span>' +
        '<span class="pnl-wx-now-desc">' + (wx.now.description || '') +
        (wx.now.humidity != null ? ' · ' + fmt(wx.now.humidity, 0) + '% ' + __t('app.humidityShort', 'humedad') : '') +
        '</span></div>';
    }
    html += days.map(function (d, i) {
      return '<div class="pnl-wx-row">' +
        '<span class="pnl-wx-day">' + dayLabel(d.date, i) + '</span>' +
        '<span class="pnl-wx-desc" title="' + (d.description || '') + '">' + (d.description || '—') + '</span>' +
        '<span class="pnl-wx-temp mono">' + fmt(d.temp_min, 0) + '–' + fmt(d.temp_max, 0) + '°</span>' +
        '<span class="pnl-wx-rain mono">' + (d.precipitation_prob != null ? fmt(d.precipitation_prob, 0) + '%' : '—') + '</span>' +
        '</div>';
    }).join('');
    q('pnl-weather').innerHTML = html;
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
    q('pnl-solar').innerHTML =
      '<div style="display:flex;gap:1.4rem;align-items:baseline;flex-wrap:wrap;">' +
      '<div><span class="kpi-value" style="font-size:1.5rem;color:#c49a18;">' + fmt(s.today_kwh, 1) + '</span>' +
      ' <span class="kpi-sub">kWh ' + __t('app.today', 'hoy').toLowerCase() + '</span></div>' +
      '<div><span class="kpi-value" style="font-size:1.5rem;color:#c49a18;opacity:0.75;">' + fmt(s.tomorrow_kwh, 1) + '</span>' +
      ' <span class="kpi-sub">kWh ' + __t('app.tomorrow', 'mañana').toLowerCase() + '</span></div>' +
      '</div>' +
      '<div class="sav-explain">' + __t('app.solarExplain', 'Producción estimada para {kwp} kWp orientación sur.')
        .replace('{kwp}', fmt(s.peak_kwp, 1)) + '</div>' + pkTxt;
    // Mini GHI curve (today)
    var cv = prep('pnl-solar-chart', 64);
    var hours = s.hourly_today || [];
    if (!cv || !hours.length) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var gMax = Math.max.apply(null, hours.map(function (h) { return h.ghi; }).concat([1]));
    ctx.beginPath();
    ctx.moveTo(0, H - 1);
    hours.forEach(function (h) {
      ctx.lineTo((h.hour + 0.5) / 24 * W, H - 1 - (H - 6) * (h.ghi / gMax));
    });
    ctx.lineTo(W, H - 1);
    ctx.closePath();
    ctx.fillStyle = 'rgba(242,200,78,0.35)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(196,154,24,0.8)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
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
      '</div>';
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
    drawHouseChart(); drawPvpcChart(); renderSolar();
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
      setInterval(function () { loadPower().catch(function () {}); }, 15000);  // watts move fastest
      setInterval(refreshHouse, 60000);    // kWh / € of the day+month
      setInterval(refreshEnv, 300000);     // exogenous environment
    });
  });
})();
