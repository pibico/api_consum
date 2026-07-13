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

  function addTurn(role, content, meta) {
    var log = q('ai-chat-log');
    var div = document.createElement('div');
    div.className = 'ai-turn ai-turn-' + role;
    div.innerHTML = role === 'assistant' ? renderProse(content) : esc(content);
    // Inference metadata under the assistant bubble: model · start time ·
    // elapsed · in/out tokens · tok/s (persisted server-side, mig 011).
    if (role === 'assistant' && meta && meta.elapsed_ms != null) {
      var secs = meta.elapsed_ms / 1000;
      var tps = meta.completion_tokens && secs > 0
        ? Math.round(meta.completion_tokens / secs) : null;
      var parts = [];
      if (meta.model) parts.push(meta.model);
      if (meta.started_at) parts.push(meta.started_at);
      parts.push(secs.toFixed(1).replace('.', ',') + ' s');
      parts.push('↑' + (meta.prompt_tokens || 0) + ' ↓' +
        (meta.completion_tokens || 0) + ' tok');
      if (tps != null) parts.push(tps + ' tok/s');
      var m = document.createElement('div');
      m.className = 'ai-turn-meta';
      m.textContent = parts.join(' · ');
      div.appendChild(m);
    }
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
      addTurn('assistant', r.answer || '', r.meta);
      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: r.answer || '' });
      if (r.remaining_today != null) {
        var quota = '· ' + __t('ai.remaining', '{n} preguntas restantes hoy')
          .replace('{n}', r.remaining_today);
        if (r.credits_cap) {
          quota += ' · ' + __t('ai.credits', 'créditos {p}%')
            .replace('{p}', Math.min(100, Math.round(100 * (r.credits_used_month || 0) / r.credits_cap)));
        }
        q('ai-quota').textContent = quota;
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
      // Textarea: Enter sends, Shift+Enter = newline (scripts/pastes fit now).
      q('ai-q').addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(e); }
      });
      // Restore today's persisted conversation (server-side, mig 010) so
      // navigating away and back doesn't lose the chat.
      App.apiFetch('/ai/chat').then(function (r) {
        (r.turns || []).forEach(function (t) {
          addTurn(t.role, t.content, t.meta);
          history.push({ role: t.role, content: t.content });
        });
      }).catch(function () { /* chat restore is best-effort */ });
      loadNarrative(false);
    }).catch(function (e) {
      q('ai-upsell').style.display = '';
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    window.onAppReady(init);
  });
})();
