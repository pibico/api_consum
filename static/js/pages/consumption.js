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
  function today() { return new Date().toISOString().slice(0, 10); }

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

  function drawDayChart() {
    var cv = prep('cons-day-chart');
    if (!cv || !day) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var padL = 36, padB = 18, padT = 8;
    var vals = day.values || [];
    var byHour = {};
    vals.forEach(function (v) { byHour[v.hour] = v; });
    var max = Math.max(0.05, Math.max.apply(null, vals.map(function (v) { return v.kwh; })));
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

  function drawMonthChart() {
    var cv = prep('cons-month-chart');
    if (!cv || !month) return;
    var ctx = cv.ctx, W = cv.W, H = cv.H;
    var padL = 36, padR = 34, padB = 18, padT = 8;
    var vals = month.values || [];
    if (!vals.length) return;
    var n = vals.length;
    var maxK = Math.max(0.05, Math.max.apply(null, vals.map(function (v) { return v.kwh; })));
    var maxC = Math.max(0.05, Math.max.apply(null, vals.map(function (v) { return v.cost_eur || 0; })));
    var chartW = W - padL - padR, chartH = H - padT - padB;
    var bw = chartW / Math.max(n, 28);
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'right';
    ctx.fillText(maxK.toFixed(0), padL - 3, padT + 8);
    ctx.fillText('0', padL - 3, H - padB);
    ctx.textAlign = 'left';
    ctx.fillStyle = '#e67e22';
    ctx.fillText(maxC.toFixed(1) + '€', W - padR + 3, padT + 8);
    // kWh bars
    vals.forEach(function (v, i) {
      var x = padL + i * bw;
      var bh = chartH * (v.kwh / maxK);
      ctx.fillStyle = 'rgba(70,130,180,0.75)';
      ctx.fillRect(x + 1, H - padB - bh, Math.max(bw - 2, 1), bh);
    });
    // € line
    ctx.beginPath();
    vals.forEach(function (v, i) {
      var x = padL + i * bw + bw / 2;
      var y = H - padB - chartH * ((v.cost_eur || 0) / maxC);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#e67e22';
    ctx.lineWidth = 2;
    ctx.stroke();
    // x labels (every ~5 days)
    ctx.fillStyle = 'rgba(44,62,80,0.55)';
    ctx.textAlign = 'center';
    vals.forEach(function (v, i) {
      if (i % 5 === 0) ctx.fillText(v.date.slice(8), padL + i * bw + bw / 2, H - 5);
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
    return App.apiFetch('/consumption/context').then(function (c) {
      var sel = q('cons-device');
      var opts = ['<option value="">' + __t('cons.wholeHouse', 'Toda la casa') + '</option>'];
      (c.devices || []).forEach(function (d) {
        opts.push('<option value="' + d.hostname + '">' + d.hostname + '</option>');
      });
      sel.innerHTML = opts.join('');
    });
  }

  function reload() {
    device = q('cons-device').value || '';
    loadDay().catch(function (e) { App.showNotification(__t('common.error', 'Error'), e.message, 'danger'); });
    loadMonth().catch(function () {});
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

  window.addEventListener('resize', function () { drawDayChart(); drawMonthChart(); });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      q('cons-date').value = today();
      q('cons-date').max = today();
      q('cons-date').onchange = reload;
      q('cons-prev').onclick = function () { shift(-1); };
      q('cons-next').onclick = function () { shift(1); };
      q('cons-today').onclick = function () { q('cons-date').value = today(); reload(); };
      loadDevices().then(reload).catch(reload);
      // Auto-refresh cards every 5 min
      setInterval(reload, 300000);
    });
  });
})();
