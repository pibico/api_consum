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

  // Appliance label: curated name (sensors.name) when set, else the FULL id
  // exactly as registered in api_edge — recognizable against the console,
  // unlike a shortened "Sensor ··xxxx".
  function sensorLabel(o) {
    if (o.name && String(o.name).trim() && o.name !== o.id) return o.name;
    return o.id;
  }

  function periodColor(p) {
    if (p === 'P1') return 'rgba(231,76,60,0.75)';
    if (p === 'P2') return 'rgba(243,156,18,0.75)';
    return 'rgba(46,204,113,0.75)';
  }

  // ── ECharts (same init pattern as the rest of the app) ───────────────
  var charts = {};
  function chart(id) {
    var el = q(id);
    if (!el || typeof echarts === 'undefined' || !el.clientWidth) return null;
    if (!charts[id]) charts[id] = echarts.init(el);
    return charts[id];
  }
  var AX = { axisLabel: { fontSize: 9, color: '#3d5a75' },
             axisLine: { lineStyle: { color: 'rgba(44,81,113,0.3)' } } };

  // ── Row 1 cards: hero (achieved) · semáforo · next action ────────────
  function renderCards() {
    var a = data.achieved || {}, s = data.shift || {}, w = data.window || {};

    // Hero — achieved savings with the baseline IN the copy (Opower rule).
    if (a.status === 'ok') {
      q('sv-hero').textContent = fmt(a.saving_eur) + ' €';
      q('sv-hero-txt').textContent = __t('sav.heroExplain',
        'Has pagado {real} € por {kwh} kWh. Ese mismo consumo, todo a precio de horas caras, habría costado {exp} €.')
        .replace('{real}', fmt(a.real_eur))
        .replace('{kwh}', fmt(a.kwh, 0))
        .replace('{exp}', fmt(a.expensive_eur));
    } else {
      q('sv-hero').textContent = '—';
      q('sv-hero-txt').textContent = __t('sav.heroNoData', 'El mes acaba de empezar — en un par de días te lo cuento.');
    }

    // Next action — the ONE concrete tip with its € value (shift + window).
    q('sv-action').textContent = s.saving_month_eur != null ? '~' + fmt(s.saving_month_eur) + ' €' : '—';
    var when = w.best ? hh(w.best.start) + '–' + hh(w.best.end) : __t('sav.cheapHours', 'las horas baratas');
    q('sv-action-txt').textContent = __t('sav.actionExplain',
      'Pon la lavadora, el lavavajillas o el termo en la franja {win} en vez de en horas caras. Eso es todo.')
      .replace('{win}', when);
  }

  // Semáforo "ahora" — needs the live price/period (same source as Panel).
  function renderNow() {
    App.apiFetch('/consumption/summary').then(function (r) {
      var period = r.price_period || r.pvpc_period;
      var price = r.price_now_eur_kwh != null ? r.price_now_eur_kwh : r.pvpc_now_eur_kwh;
      var LBL = { P1: ['CARA', '#e74c3c'], P2: ['NORMAL', '#f39c12'], P3: ['BARATA', '#2ecc71'] };
      var st = LBL[period] || ['—', '#6a9bc3'];
      q('sv-now').innerHTML = '<span style="color:' + st[1] + ';">● ' +
        __t('sav.now' + (period || ''), st[0]) + '</span>';
      q('sv-now-sub').textContent = price != null ? fmt(price, 3) + ' €/kWh' : '—';
      // Next band change today (from the window hours when they are today's).
      var w = (data && data.window) || {};
      var today = new Date().toISOString().slice(0, 10);
      var txt = '';
      if (w.date === today && (w.hours || []).length && period) {
        var nowH = new Date().getHours();
        for (var i = 0; i < w.hours.length; i++) {
          var r2 = w.hours[i];
          if (r2.hour > nowH && r2.period !== period) {
            txt = (LBL[r2.period] && r2.period === 'P3'
              ? __t('sav.dropsAt', 'La luz baja a las {h}')
              : __t('sav.changesAt', 'Cambia a {p} a las {h}')
                .replace('{p}', __t('sav.now' + r2.period, (LBL[r2.period] || ['—'])[0]).toLowerCase()))
              .replace('{h}', hh(r2.hour));
            break;
          }
        }
      }
      if (!txt && w.best) {
        txt = __t('sav.bestToday', 'Mejor momento: {win} ☀️')
          .replace('{win}', hh(w.best.start) + '–' + hh(w.best.end));
      }
      q('sv-now-txt').textContent = txt;
    }).catch(function () {
      q('sv-now').textContent = '—';
    });
  }

  // ── Row 2: today's price bars (band colors + best window) ────────────
  function drawPrices() {
    var c = chart('sv-chart-prices');
    if (!c || !data) return;
    var w = data.window || {};
    var hours = w.hours || [];
    q('sv-window-day').textContent = '· ' + (w.date || '');
    if (!hours.length) { c.clear(); return; }
    var byHour = {};
    hours.forEach(function (r) { byHour[r.hour] = r; });
    var labels = [], vals = [];
    for (var h = 0; h < 24; h++) {
      labels.push(String(h).padStart(2, '0'));
      var r = byHour[h];
      vals.push(r ? { value: +r.price_eur_kwh.toFixed(4),
                      itemStyle: { color: periodColor(r.period) } } : null);
    }
    var markArea = w.best ? { silent: true, itemStyle: { color: 'rgba(46,204,113,0.15)' },
      data: [[{ xAxis: String(w.best.start).padStart(2, '0') },
              { xAxis: String(Math.min(w.best.end, 23)).padStart(2, '0') }]] } : undefined;
    c.setOption({
      grid: { left: 44, right: 8, top: 10, bottom: 20 },
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return v != null ? v + ' €/kWh' : '—'; } },
      xAxis: Object.assign({ type: 'category', data: labels }, AX),
      yAxis: Object.assign({ type: 'value' }, AX),
      series: [{ type: 'bar', data: vals, barCategoryGap: '18%', markArea: markArea }],
    }, true);
  }

  // ── Row 2: the month, compared (two labeled bars) ────────────────────
  function drawMonth() {
    var c = chart('sv-chart-month');
    if (!c || !data) return;
    var a = data.achieved || {};
    if (a.status !== 'ok') { c.clear(); return; }
    c.setOption({
      grid: { left: 8, right: 40, top: 10, bottom: 8, containLabel: true },
      xAxis: Object.assign({ type: 'value' }, AX),
      yAxis: Object.assign({ type: 'category', data: [
        __t('sav.barExpensive', 'Todo en horas caras'),
        __t('sav.barReal', 'Tu mes, con tus horas'),
      ] }, AX, { axisLabel: { fontSize: 10, color: '#2c5171' } }),
      series: [{
        type: 'bar', barMaxWidth: 34,
        label: { show: true, position: 'right', fontWeight: 600,
                 formatter: function (p) { return p.value.toFixed(2) + ' €'; } },
        data: [
          { value: +a.expensive_eur, itemStyle: { color: 'rgba(231,76,60,0.75)', borderRadius: [0, 6, 6, 0] } },
          { value: +a.real_eur, itemStyle: { color: 'rgba(46,204,113,0.8)', borderRadius: [0, 6, 6, 0] } },
        ],
      }],
    }, true);
    // The weather insight, demoted to one honest sentence.
    var t = data.thermal || {};
    q('sv-weather-txt').textContent = (t.status === 'ok' && t.weather_share_pct != null)
      ? __t('sav.weatherLine', 'El frío/calor explica ~{p} % de lo que consumes — el resto son hábitos, y ahí está el ahorro.')
        .replace('{p}', fmt(t.weather_share_pct, 0))
      : '';
  }

  // ── Row 3: appliance cost ranking + standby ──────────────────────────
  function drawAppliances() {
    var c = chart('sv-chart-appl');
    if (!c || !data) return;
    var ap = data.appliances || {};
    var a = data.achieved || {};
    q('sv-appl-month').textContent = ap.month ? '· ' + ap.month : '';
    var items = (ap.items || []).slice(0, 9);
    if (!items.length) { c.clear(); return; }
    var price = a.avg_eur_kwh || null;
    var byDev = {};
    items.forEach(function (it) { byDev[it.device] = it; });
    var rows = items.map(function (it) {
      // Curated name when it exists; else the FULL raw id — recognizable,
      // unlike a shortened "Sensor ··xxxx" (these plugs publish directly and
      // have no name anywhere yet; name them in the api_edge console).
      var label = (it.name && it.name !== it.device) ? it.name : it.device;
      // Wiring-aware: a parent's bar is its REMAINDER (children subtracted),
      // and a child says who it hangs from — so nobody adds nested bars twice.
      if (it.has_children) label += ' ' + __t('sav.restSuffix', '(resto)');
      var anc = byDev[it.nested_in];
      var nested = it.nested_in
        ? (anc && anc.name && anc.name !== it.nested_in ? anc.name : it.nested_in)
        : null;
      return { name: label, kwh: it.kwh, eur: price ? it.kwh * price : null,
               standby: false, nested: nested || null };
    });
    if (ap.standby_kwh > 0.5) {
      rows.push({ name: __t('sav.standbyRow', 'Siempre encendidos (standby)'),
                  kwh: ap.standby_kwh, eur: price ? ap.standby_kwh * price : null, standby: true });
    }
    rows.sort(function (x, y) { return x.kwh - y.kwh; });   // ECharts: bottom-up
    c.setOption({
      grid: { left: 8, right: 60, top: 6, bottom: 8, containLabel: true },
      tooltip: { trigger: 'item', formatter: function (p) {
        var r = rows[p.dataIndex];
        return p.name + '<br><b>' + fmt(r.kwh, 1) + ' kWh</b>' +
          (r.eur != null ? ' · ~' + fmt(r.eur) + ' €' : '') +
          (r.nested ? '<br><span style="font-size:0.8em;">' +
            __t('sav.nestedIn', 'cuelga de {p} — ya descontado del de arriba').replace('{p}', r.nested) + '</span>' : '');
      } },
      xAxis: Object.assign({ type: 'value' }, AX),
      yAxis: Object.assign({ type: 'category', data: rows.map(function (r) { return r.name; }) },
        AX, { axisLabel: { fontSize: 10, color: '#2c5171' } }),
      series: [{
        type: 'bar', barMaxWidth: 16,
        label: { show: true, position: 'right', fontSize: 10,
                 formatter: function (p) {
                   var r = rows[p.dataIndex];
                   return r.eur != null ? '~' + r.eur.toFixed(2) + ' €' : fmt(r.kwh, 1) + ' kWh';
                 } },
        data: rows.map(function (r) {
          return { value: +r.kwh.toFixed(1),
                   itemStyle: { color: r.standby ? 'rgba(231,76,60,0.8)' : 'rgba(70,130,180,0.75)',
                                borderRadius: [0, 5, 5, 0] } };
        }),
      }],
    }, true);
  }

  // ── Best hours ───────────────────────────────────────────────────────
  // Ranked cheapest-first (api_exo "mejores horas" style): the decision a
  // user actually makes is "when do I run the washer/dishwasher?", so sort by
  // price ascending and rate with stars instead of a 24-row hourly dump.
  function priceStars(price, cheapest, priciest) {
    if (priciest <= cheapest) return '★★★';
    var t = (price - cheapest) / (priciest - cheapest);   // 0 = cheapest
    return t < 0.25 ? '★★★' : t < 0.5 ? '★★' : t < 0.75 ? '★' : '';
  }
  function renderTable() {
    // Compact single list, cheapest first — small type, tight rows, one
    // scroll for the whole day. Stars note lives in the card footer.
    var top = q('sv-best-top');
    var w = data.window || {};
    var hours = w.hours || [];
    q('sv-table-day').textContent = w.date || '';
    if (!hours.length) {
      top.innerHTML = '<span class="text-muted">' + __t('common.noData', 'Sin datos') + '</span>';
      return;
    }
    var sorted = hours.slice().sort(function (a, b) { return a.price_eur_kwh - b.price_eur_kwh; });
    var cheapest = sorted[0].price_eur_kwh;
    var priciest = sorted[sorted.length - 1].price_eur_kwh;
    top.innerHTML = sorted.map(function (r, i) {
      var inBest = w.best && r.hour >= w.best.start && r.hour < w.best.end;
      return '<div style="display:flex;align-items:center;gap:6px;padding:1px 5px;border-radius:5px;font-size:0.72rem;line-height:1.35;' +
        (i < 3 ? 'background:rgba(46,204,113,0.10);' : '') + '">' +
        '<span class="mono" style="min-width:38px;">' + hh(r.hour) + '</span>' +
        '<span class="badge" style="background:' + periodColor(r.period).replace('0.75', '0.18') + ';color:#333;min-width:22px;text-align:center;font-size:0.66rem;padding:0 4px;">' + (r.period || '—') + '</span>' +
        '<span class="mono" style="min-width:52px;">' + r.price_eur_kwh.toFixed(4) + '</span>' +
        '<span style="color:#e6a817;letter-spacing:0.5px;margin-left:auto;">' + priceStars(r.price_eur_kwh, cheapest, priciest) + '</span>' +
        (inBest ? '<span title="' + __t('sav.vBest', 'Recomendada') + '">🟢</span>' : '') +
        '</div>';
    }).join('');
  }

  // ── Load ─────────────────────────────────────────────────────────────
  function load() {
    var qs = device ? '?device=' + encodeURIComponent(device) : '';
    return App.apiFetch('/savings/insights' + qs).then(function (r) {
      data = r;
      renderCards();
      renderNow();
      drawPrices();
      drawMonth();
      drawAppliances();
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
      (r.data || []).forEach(function (o) {
        opts.push('<option value="' + o.id + '">' + sensorLabel(o) + '</option>');
      });
      sel.innerHTML = opts.join('');
    });
  }

  function reload() {
    device = q('sav-device').value || '';
    load();
  }

  window.SavPage = { reload: reload };

  var _rsz;
  window.addEventListener('resize', function () {
    clearTimeout(_rsz);
    _rsz = setTimeout(function () {
      Object.keys(charts).forEach(function (k) { charts[k].resize(); });
    }, 120);
  });

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(function () {
      loadDevices().then(load).catch(load);
      // Auto-refresh — matches the 15 min server-side insight cache
      setInterval(load, 900000);
    });
  });
})();
