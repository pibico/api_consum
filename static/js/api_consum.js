/**
 * api_consum — Main application entry point
 * Auth via api_auth JWT (OTP/SSO login) — mirrors api_edge's api_edge.js
 * Vanilla JS, no frameworks, no CDNs
 */

const ROOT = window.__ROOT__ || '';
const API_BASE = ROOT + '/api/v1';

const AppState = {
  jwt: localStorage.getItem('auth_jwt') || '',
  user: JSON.parse(localStorage.getItem('auth_user') || 'null'),
  // Backward compat: also check old api_key
  apiKey: localStorage.getItem('auth_jwt') || localStorage.getItem('api_key') || '',
};

/** Fetch helper — ONE credential: Bearer when a localStorage token exists;
 * otherwise the httpOnly SSO cookie (set by the hosted api_auth login) rides
 * along on the same-origin fetch. An EMPTY Bearer header would shadow the
 * cookie server-side, so the headers are only set when real values exist. */
async function apiFetch(endpoint, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (AppState.jwt && AppState.jwt.length >= 20) {
    headers['Authorization'] = 'Bearer ' + AppState.jwt;
  } else if (AppState.apiKey && AppState.apiKey.length >= 20) {
    headers['X-API-Key'] = AppState.apiKey;
  }

  const response = await fetch(API_BASE + endpoint, { ...options, headers });

  if (response.status === 401 || response.status === 403) {
    // Token expired or invalid — clear session, back to landing
    logout();
    return;
  }

  if (!response.ok) {
    const error = await response.json().catch(function() { return {}; });
    // detail may be a string OR an object {code, message} (our API uses both).
    // Coerce to a readable string so callers never render "[object Object]".
    const d = error.detail;
    const msg = (d && typeof d === 'object')
      ? (d.message || JSON.stringify(d))
      : (d || 'API error: ' + response.status);
    throw new Error(msg);
  }
  return response.json();
}

/** Logout — clear localStorage AND the httpOnly SSO cookie (server-side),
 * then go to the PUBLIC LANDING (ROOT + '/'), not /login. Redirecting to
 * /login would immediately SSO-redirect to api_auth, whose session cookie is
 * still valid, and bounce the user straight back into /admin. The landing is
 * public, so it just stays there — the api_consum cookie is cleared, so /admin
 * is no longer reachable without re-authenticating. */
function logout() {
  localStorage.removeItem('auth_jwt');
  localStorage.removeItem('auth_user');
  localStorage.removeItem('api_key');
  fetch(API_BASE + '/auth/logout', { method: 'POST' })
    .catch(function () { /* best-effort */ })
    .finally(function () { location.href = (ROOT || '') + '/'; });
}

/** Populate the header avatar button with the logged-in superadmin's initials
 *  (this console is superadmin-only — no org switcher, single identity). */
function updateAvatar() {
  var el = document.getElementById('userAvatar');
  if (!el || !AppState.user) return;
  var label = (AppState.user.name || AppState.user.email || '').trim();
  if (!label) return;
  var parts = label.split(/\s+/);
  var initials = (parts[0][0] || '') + (parts.length > 1 ? (parts[1][0] || '') : (parts[0][1] || ''));
  el.textContent = initials.toUpperCase();
  el.title = label;
}

/** Toast notification */
function showNotification(title, message, type) {
  type = type || 'info';
  var container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position:fixed;top:76px;right:16px;z-index:1000;max-width:360px;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(container);
  }
  var toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.innerHTML = '<strong>' + title + '</strong><p style="margin:4px 0 0">' + message + '</p>';
  container.appendChild(toast);
  setTimeout(function() { toast.remove(); }, 5000);
}

/** Canonical slide-panel open/close — the ONE mechanism every panel in this
 *  console uses (settings, About, account, detail panels). Toggles `.open` on
 *  the panel AND the single shared `#panel-backdrop` (declared once in
 *  base.html), so every panel gets the same dimmed backdrop +
 *  click-outside/Escape-to-close behavior. */
window.AppUI = window.AppUI || {};
window.AppUI.openPanel = function (id) {
  var panel = document.getElementById(id);
  var backdrop = document.getElementById('panel-backdrop');
  if (!panel) return;
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  if (backdrop) backdrop.classList.add('open');
};
window.AppUI.closePanel = function (id) {
  var panel = document.getElementById(id);
  if (panel) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }
  var backdrop = document.getElementById('panel-backdrop');
  var anyOpen = document.querySelector('.slide-panel.open');
  if (backdrop && !anyOpen) backdrop.classList.remove('open');
};
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var backdrop = document.getElementById('panel-backdrop');
    if (backdrop) {
      backdrop.addEventListener('click', function () {
        document.querySelectorAll('.slide-panel.open').forEach(function (p) {
          window.AppUI.closePanel(p.id);
        });
      });
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var open = document.querySelectorAll('.slide-panel.open');
    if (open.length) window.AppUI.closePanel(open[open.length - 1].id);
  });
})();

/** Minimal glass confirm dialog — replaces native confirm() (pibiCo guidelines
 *  forbid JS dialogs). Centered modal (not a slide panel — confirm/input is the
 *  one case modals are allowed for). Returns a Promise<boolean>. */
window.AppUI.confirm = function (message) {
  return new Promise(function (resolve) {
    var t = window.i18n ? window.i18n.t : function (k) { return k; };
    var overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML =
      '<div class="confirm-box">' +
        '<p class="confirm-msg"></p>' +
        '<div class="confirm-actions">' +
          '<button class="btn btn-secondary confirm-cancel"></button>' +
          '<button class="btn btn-primary confirm-ok"></button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.querySelector('.confirm-msg').textContent = message;
    overlay.querySelector('.confirm-cancel').textContent = t('common.cancel');
    overlay.querySelector('.confirm-ok').textContent = t('common.confirm');

    function close(val) {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      resolve(val);
    }
    function onKey(e) { if (e.key === 'Escape') close(false); }

    overlay.querySelector('.confirm-cancel').onclick = function () { close(false); };
    overlay.querySelector('.confirm-ok').onclick = function () { close(true); };
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(false); });
    document.addEventListener('keydown', onKey);
  });
};

/* ==========================================================================
 * Punto de suministro global (multi-CUPS F3)
 * El valor elegido vive en la URL (?supply=<id>) — fuente de verdad de la
 * página actual — y se recuerda en localStorage para las siguientes. Las
 * páginas añaden App.supplyQS() a sus llamadas y el backend filtra el
 * sub-árbol del punto (o todo, si vacío = "Todos").
 * ======================================================================= */
var SUPPLY_KEY = 'consum_supply';

function getSupply() {
  // URL manda; sin párametro, la última elección guardada (SÍNCRONO — los
  // fetches de las páginas salen en su init, antes de que llegue el contexto
  // async; un replaceState posterior llegaría tarde y mezclaría datos).
  var m = location.search.match(/[?&]supply=(\d+)/);
  if (m) return m[1];
  if (/[?&]supply=($|&)/.test(location.search)) return '';  // ?supply= vacío = Todos explícito
  try { return localStorage.getItem(SUPPLY_KEY) || ''; } catch (e) { return ''; }
}

/** '&supply=<id>' o '' — análogo al custQS() de cada página. */
function supplyQS() {
  var s = getSupply();
  return s ? '&supply=' + encodeURIComponent(s) : '';
}

function onSupplyChange(val) {
  try { localStorage.setItem(SUPPLY_KEY, val || ''); } catch (e) {}
  var url = new URL(location.href);
  // 'Todos' se marca EXPLÍCITO en la URL (?supply=) para que getSupply no
  // vuelva a caer al valor guardado en esta carga.
  url.searchParams.set('supply', val || '');
  location.href = url.toString();   // recarga: cada página relee en su init
}

function initSupplySelector(ctx) {
  var sel = document.getElementById('supply-select');
  var sps = (ctx && ctx.supply_points) || [];
  var cur = getSupply();
  // Elección guardada/URL que no es de este usuario (punto borrado, o cambio
  // de cuenta en el MISMO navegador — localStorage es por origen, no por
  // usuario): limpiar y recargar limpio. Sin esto, todas las páginas
  // arrastran un ?supply= ajeno y el backend (tenancy) responde 404 en todo
  // — visto en vivo con info@alztech.es heredando el supply de pibico.
  if (cur && !sps.some(function (p) { return String(p.id) === cur; })) {
    try { localStorage.removeItem(SUPPLY_KEY); } catch (e) {}
    var url = new URL(location.href);
    url.searchParams.delete('supply');
    location.replace(url.toString());   // una sola recarga: cur queda vacío
    return;
  }
  if (!sel || sps.length < 2) return;
  var t = (window.i18n && window.i18n.t) ? window.i18n.t.bind(window.i18n) : function (k, f) { return f; };
  sel.innerHTML = '<option value="">' + t('supply.all', 'Todos los puntos') + '</option>' +
    sps.map(function (p) {
      var label = p.name + (p.location ? ' — ' + p.location : '');
      return '<option value="' + p.id + '"' + (String(p.id) === cur ? ' selected' : '') + '>' +
        label.replace(/</g, '&lt;') + '</option>';
    }).join('');
  sel.style.display = '';
}

/** Init */
function initApp() {
  // No client-side auth guard: the SERVER gate (superadmin cookie check in
  // web.py) already redirected unauthenticated visitors before this page
  // rendered. SSO sessions live in an httpOnly cookie JS cannot see, so a
  // localStorage check here would boot every SSO user into a login loop.

  // Show user info in sidebar if available
  if (AppState.user) {
    var userEl = document.getElementById('sidebar-user');
    if (userEl) {
      userEl.textContent = AppState.user.name || AppState.user.email || '';
      userEl.style.display = 'block';
    }
  }
  updateAvatar();

  // Highlight current nav item
  var path = location.pathname.replace(/\/$/, '') || ROOT + '/';
  document.querySelectorAll('.sidebar-nav a').forEach(function(a) {
    var href = (a.getAttribute('href') || '').replace(/\/$/, '');
    var current = path.replace(/\/$/, '');
    if (href === current || (current === ROOT && href === ROOT + '/')) {
      a.classList.add('active');
    }
  });

  // Playground nav link: superadmin-only (hidden by default in base.html).
  // Reveal it once /consumption/context confirms is_superadmin — every /app/*
  // page shares this shell, so one fetch here covers the whole console.
  var navPg = document.getElementById('nav-playground');
  apiFetch('/consumption/context').then(function (c) {
    if (!c) return;
    if (navPg && c.is_superadmin) navPg.style.display = '';
    // Selector global de punto de suministro (F3)
    window.App.supplyPoints = c.supply_points || [];
    initSupplySelector(c);
  }).catch(function () { /* not entitled / not logged in yet */ });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

window.App = { apiFetch, showNotification, logout, state: AppState,
  getSupply: getSupply, supplyQS: supplyQS, onSupplyChange: onSupplyChange,
  supplyPoints: [], supplyPoint: function () {
    var s = getSupply();
    if (!s) return null;
    for (var i = 0; i < App.supplyPoints.length; i++) {
      if (String(App.supplyPoints[i].id) === s) return App.supplyPoints[i];
    }
    return null;
  } };
