/**
 * ai.js — IA page (F4): daily narrative + stateless Q&A chat.
 * All AIDA calls happen SERVER-SIDE (/ai/narrative, /ai/ask) — the browser
 * never sees the LLM key. Three states: upsell (org without ai_enabled),
 * not-configured (server missing key/model) and active.
 */
(function () {
  'use strict';

  var history = [];   // client-side transcript, re-sent to the stateless /ai/ask

  function q(id) { return document.getElementById(id); }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // Minimal renderer: paragraphs + **bold** + bullet lines (LLM output is
  // plain text/markdown-lite; NEVER injected raw).
  function renderProse(text) {
    // Some local models emit LaTeX-ish noise ($…$, \text{…}) — flatten it.
    text = String(text)
      .replace(/\\text\{([^}]*)\}/g, '$1')
      .replace(/\$([^$\n]+)\$/g, '$1');
    var html = esc(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .split(/\n{2,}/).map(function (p) {
        var lines = p.split('\n');
        var isList = lines.every(function (l) { return /^\s*([-*•]|\d+\.)\s/.test(l) || !l.trim(); });
        if (isList && lines.some(function (l) { return l.trim(); })) {
          return '<ul>' + lines.filter(function (l) { return l.trim(); }).map(function (l) {
            return '<li>' + l.replace(/^\s*([-*•]|\d+\.)\s/, '') + '</li>';
          }).join('') + '</ul>';
        }
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
      }).join('');
    return html;
  }

  function addTurn(role, content) {
    var log = q('ai-chat-log');
    var div = document.createElement('div');
    div.className = 'ai-turn ai-turn-' + role;
    div.innerHTML = role === 'assistant' ? renderProse(content) : esc(content);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function loadNarrative(refresh) {
    var box = q('ai-narrative');
    box.innerHTML = '<span class="spinner"></span> <span class="text-muted" style="font-size:0.8rem;">' +
      __t('ai.generating', 'Generando narrativa…') + '</span>';
    App.apiFetch('/ai/narrative' + (refresh ? '?refresh=true' : '')).then(function (r) {
      box.innerHTML = renderProse(r.narrative || '');
      q('ai-narr-date').textContent = '· ' + (r.date || '');
    }).catch(function (e) {
      box.innerHTML = '<span class="text-muted">' + esc(msgOf(e)) + '</span>';
    });
  }

  function msgOf(e) {
    try {
      var d = JSON.parse(e.message);
      return d.message || e.message;
    } catch (_) { return e.message; }
  }

  function send(ev) {
    ev.preventDefault();
    var input = q('ai-q');
    var question = input.value.trim();
    if (!question) return;
    input.value = '';
    addTurn('user', question);
    q('ai-send').disabled = true;
    App.apiFetch('/ai/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, history: history.slice(-6) }),
    }).then(function (r) {
      addTurn('assistant', r.answer || '');
      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: r.answer || '' });
      if (r.remaining_today != null) {
        q('ai-quota').textContent = '· ' + __t('ai.remaining', '{n} preguntas restantes hoy')
          .replace('{n}', r.remaining_today);
      }
    }).catch(function (e) {
      addTurn('assistant', msgOf(e));
    }).finally(function () {
      q('ai-send').disabled = false;
      input.focus();
    });
  }

  function init() {
    App.apiFetch('/consumption/context').then(function (c) {
      if (!c.ai_enabled) {
        q('ai-upsell').style.display = '';
        return;
      }
      q('ai-active').style.display = '';
      q('ai-refresh').style.display = '';
      q('ai-refresh').onclick = function () { loadNarrative(true); };
      q('ai-form').addEventListener('submit', send);
      loadNarrative(false);
    }).catch(function (e) {
      q('ai-upsell').style.display = '';
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(init);
  });
})();
