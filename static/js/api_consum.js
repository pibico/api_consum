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
    throw new Error(error.detail || 'API error: ' + response.status);
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
  if (navPg) {
    apiFetch('/consumption/context').then(function (c) {
      if (c && c.is_superadmin) navPg.style.display = '';
    }).catch(function () { /* not entitled / not logged in yet — stay hidden */ });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

window.App = { apiFetch, showNotification, logout, state: AppState };
