/**
 * savings.js — Ahorro page (F3/OE3): three insight cards (shift/thermal/
 * window), real-vs-weather-expected chart, price+solar window chart and the
 * hourly recommendation table. Single endpoint: /savings/insights.
 * Auto-refresh every 15 min (matches the server-side insight cache).
 */
(function () {
  'use strict';

  var data = null;   // /savings/insights payload
  var device = '';

  function q(id) { return document.getElementById(id); }
  function fmt(n, dec) { return (n == null) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec == null ? 2 : dec }); }
  function hh(h) { return String(h).padStart(2, '0') + ':00'; }

  function periodColor(p) {
    if (p === 'P1') return 'rgba(231,76,60,0.75)';
    if (p === 'P2') return 'rgba(243,156,18,0.75)';
    return 'rgba(46,204,113,0.75)';
  }

  function prep(id, h) {
    var c = q(id);
    if (!c || !c.parentElement.clientWidth) return null;
    var dpr = window.devicePixelRatio || 1;
    var rect = c.parentElement.getBoundingClientRect();
    var W = rect.width - 32, H = h || 200;
    c.width = W * dpr; c.height = H * dpr;
    c.style.width = W + 'px'; c.style.height = H + 'px';
    var ctx = c.getContext('2d');
    ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
    return { ctx: ctx, W: W, H: H };
  }

  // ── Cards ────────────────────────────────────────────────────────────
  function renderCards() {
    var s = data.shift || {}, t = data.thermal || {}, w = data.window || {};

    q('sv-shift').textContent = s.saving_month_eur != null ? fmt(s.saving_month_eur) + ' €' : '—';
    q('sv-shift-txt').textContent = __t('sav.shiftExplain',
      'Si movieras el {pct}% flexible de tu consumo (lavadora, lavavajillas, termo…) a las {n} horas más baratas de cada día, ahorrarías un {sp}% del término de energía.')
      .replace('{pct}', Math.round((s.flex_share || 0.3) * 100))
      .replace('{n}', s.cheap_hours || 6)
      .replace('{sp}', fmt(s.saving_pct, 1));

    if (t.status === 'ok') {
      q('sv-thermal').textContent = fmt(t.weather_share_pct, 0) + ' %';
      var y = t.yesterday;
      var txt = __t('sav.thermalExplain',
        'Cada grado-día de calor añade {cdd} kWh a tu día (base {base} kWh).')
        .replace('{cdd}', fmt(Math.max(t.kwh_per_cdd || 0, 0), 2))
        .replace('{base}', fmt(t.base_kwh, 1));
      if (y && y.deviation_pct != null) {
        var dev = y.deviation_pct;
        txt += ' ' + (dev > 10
          ? __t('sav.devHigh', 'Ayer consumiste un {d}% MÁS de lo que explica el clima — revisa hábitos.')
          : dev < -10
            ? __t('sav.devLow', 'Ayer consumiste un {d}% menos de lo esperado por clima. ¡Bien!')
            : __t('sav.devOk', 'Ayer ({d}%) estuviste en línea con lo esperado por el clima.'))
          .replace('{d}', fmt(Math.abs(dev), 0));
      }
      q('sv-thermal-txt').textContent = txt;
    } else {
      q('sv-thermal').textContent = '—';
      q('sv-thermal-txt').textContent = __t('sav.thermalNoData', 'Aún no hay días suficientes para separar clima de hábitos (se necesitan ~10).');
    }

    if (w.best) {
      q('sv-window').textContent = hh(w.best.start) + ' – ' + hh(w.best.end);
      q('sv-window-date').textContent = w.date;
      q('sv-window-txt').textContent = __t('sav.windowExplain',
        'Mejor franja para lavadora, lavavajillas o cargar el coche: PVPC medio {p} €/kWh y máximo aprovechamiento solar.')
        .replace('{p}', fmt(w.best.price_avg, 4));
    } else {
      q('sv-window').textContent = '—';
      q('sv-window-date').textContent = w.date || '—';
      q('sv-window-txt').textContent = __t('common.noData', 'Sin datos');
    }
  }

  // ── Thermal chart: kWh bars + expected line ─────────────────────────
  function drawThermal() {
    var cv = prep('sav-thermal-chart');
    if (!cv || !data) return;
    var t = data.thermal || {};
    var series = t.series || [];
    if (!series.length) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var padL = 34, padB = 18, padT = 8;
    var n = series.length;
    var max = Math.max(0.5, Math.max.apply(null, series.map(function (r) {
      return Math.max(r.kwh, r.expected_kwh || 0);
    })));
    var chartW = W - padL - 8, chartH = H - padT - padB;
    var bw = chartW / n;
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'right';
    ctx.fillText(max.toFixed(0), padL - 3, padT + 8);
    ctx.fillText('0', padL - 3, H - padB);
    series.forEach(function (r, i) {
      var x = padL + i * bw;
      var bh = chartH * (r.kwh / max);
      ctx.fillStyle = 'rgba(70,130,180,0.75)';
      ctx.fillRect(x + 0.5, H - padB - bh, Math.max(bw - 1, 1), bh);
    });
    ctx.beginPath();
    var started = false;
    series.forEach(function (r, i) {
      if (r.expected_kwh == null) return;
      var x = padL + i * bw + bw / 2;
      var y = H - padB - chartH * (r.expected_kwh / max);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#8e44ad';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'center';
    series.forEach(function (r, i) {
      if (i % 7 === 0) ctx.fillText(r.date.slice(5), padL + i * bw + bw / 2, H - 5);
    });
    q('sv-r2').textContent = t.r2 != null ? '· R² ' + t.r2 : '';
  }

  // ── Window chart: price bars + GHI line, best window highlighted ────
  function drawWindow() {
    var cv = prep('sav-window-chart');
    if (!cv || !data) return;
    var w = data.window || {};
    var hours = w.hours || [];
    if (!hours.length) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var padL = 40, padB = 18, padT = 8;
    var byHour = {};
    hours.forEach(function (r) { byHour[r.hour] = r; });
    var maxP = Math.max.apply(null, hours.map(function (r) { return r.price_eur_kwh; }));
    var maxG = Math.max(1, Math.max.apply(null, hours.map(function (r) { return r.ghi || 0; })));
    var chartW = W - padL - 8, chartH = H - padT - padB;
    var bw = chartW / 24;
    // Best-window highlight behind everything
    if (w.best) {
      ctx.fillStyle = 'rgba(46,204,113,0.18)';
      ctx.fillRect(padL + w.best.start * bw, padT,
        (w.best.end - w.best.start) * bw, chartH);
    }
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'right';
    ctx.fillText(maxP.toFixed(2), padL - 3, padT + 8);
    ctx.fillText('0', padL - 3, H - padB);
    ctx.textAlign = 'center';
    for (var h = 0; h < 24; h++) {
      var r = byHour[h];
      var x = padL + h * bw;
      if (r) {
        var bh = chartH * (r.price_eur_kwh / maxP);
        ctx.fillStyle = 'rgba(70,130,180,0.75)';
        ctx.fillRect(x + 1, H - padB - bh, bw - 2, bh);
      }
      if (h % 3 === 0) {
        ctx.fillStyle = 'rgba(44,62,80,0.55)';
        ctx.fillText(String(h).padStart(2, '0'), x + bw / 2, H - 5);
      }
    }
    // GHI line
    ctx.beginPath();
    var started = false;
    for (var h2 = 0; h2 < 24; h2++) {
      var r2 = byHour[h2];
      if (!r2) continue;
      var x2 = padL + h2 * bw + bw / 2;
      var y2 = H - padB - chartH * ((r2.ghi || 0) / maxG);
      if (!started) { ctx.moveTo(x2, y2); started = true; } else ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = '#f39c12';
    ctx.lineWidth = 2;
    ctx.stroke();
    q('sv-window-day').textContent = '· ' + (w.date || '');
  }

  // ── Table ────────────────────────────────────────────────────────────
  function renderTable() {
    var tbody = q('sav-tbody');
    var w = data.window || {};
    var hours = w.hours || [];
    q('sv-table-day').textContent = w.date || '';
    if (!hours.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">' + __t('common.noData', 'Sin datos') + '</td></tr>';
      return;
    }
    var sorted = hours.slice().sort(function (a, b) { return b.score - a.score; });
    var top = {};
    sorted.slice(0, 6).forEach(function (r) { top[r.hour] = true; });
    var worst = {};
    sorted.slice(-4).forEach(function (r) { worst[r.hour] = true; });
    tbody.innerHTML = hours.map(function (r) {
      var verdict, cls;
      if (w.best && r.hour >= w.best.start && r.hour < w.best.end) {
        verdict = __t('sav.vBest', 'Recomendada'); cls = 'background:rgba(46,204,113,0.18);color:#1a5c3a;';
      } else if (top[r.hour]) {
        verdict = __t('sav.vGood', 'Buena'); cls = 'background:rgba(46,204,113,0.10);color:#28946a;';
      } else if (worst[r.hour]) {
        verdict = __t('sav.vAvoid', 'Evitar'); cls = 'background:rgba(231,76,60,0.12);color:#9e3a4a;';
      } else {
        verdict = __t('sav.vNeutral', 'Normal'); cls = 'background:rgba(44,62,80,0.06);color:#5a6478;';
      }
      return '<tr>' +
        '<td class="mono">' + hh(r.hour) + '</td>' +
        '<td class="mono">' + r.price_eur_kwh.toFixed(4) + '</td>' +
        '<td><span class="badge" style="background:' + periodColor(r.period).replace('0.75', '0.18') + ';color:#333;">' + (r.period || '—') + '</span></td>' +
        '<td class="mono">' + fmt(r.ghi, 0) + '</td>' +
        '<td class="mono">' + r.score.toFixed(2) + '</td>' +
        '<td><span class="badge" style="' + cls + '">' + verdict + '</span></td>' +
        '</tr>';
    }).join('');
  }

  // ── Load ─────────────────────────────────────────────────────────────
  function load() {
    var qs = device ? '?device=' + encodeURIComponent(device) : '';
    return App.apiFetch('/savings/insights' + qs).then(function (r) {
      data = r;
      renderCards();
      drawThermal();
      drawWindow();
      renderTable();
    }).catch(function (e) {
      App.showNotification(__t('common.error', 'Error'), e.message, 'danger');
    });
  }

  function loadDevices() {
    // Real reporting sensors — not the gateway (see consumption.js).
    return App.apiFetch('/consumption/sensors').then(function (r) {
      var sel = q('sav-device');
      var opts = ['<option value="">' + __t('cons.wholeHouse', 'Toda la casa') + '</option>'];
      (r.data || []).forEach(function (id) {
        opts.push('<option value="' + id + '">' + id + '</option>');
      });
      sel.innerHTML = opts.join('');
    });
  }

  function reload() {
    device = q('sav-device').value || '';
    load();
  }

  window.SavPage = { reload: reload };

  window.addEventListener('resize', function () { drawThermal(); drawWindow(); });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      loadDevices().then(load).catch(load);
      // Auto-refresh — matches the 15 min server-side insight cache
      setInterval(load, 900000);
    });
  });
})();
