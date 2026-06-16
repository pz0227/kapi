/*
 * kapi_eval_pro.js — "Rigorous Eval" overlay for the Eval page.
 *
 * Adds a panel (separate from the upstream keyword eval) that runs the 3-axis
 * rigorous eval via POST /api/eval/run-rigorous and renders the results the way
 * the methodology demands: three SEPARATE axes (competence / honesty / lexical),
 * a failure-mode table WITH a fault-owner column, the self-documenting caveats,
 * and a per-case table. Pure DOM, no framework; document-level MutationObserver
 * so it survives Lit re-renders. Mirrors the kapi_dashboard_extras.js pattern.
 */
(function () {
  'use strict';
  if (window.__kapi_eval_pro_loaded) return;
  window.__kapi_eval_pro_loaded = true;

  var API = (typeof window !== 'undefined' && window.__KAPI_ANALYTICS_URL__) || 'http://127.0.0.1:18792';
  var PANEL_ID = 'kapi-eval-pro';

  function onEvalPage() {
    var titles = document.querySelectorAll('.pa-card__title, .pa-card h3');
    for (var i = 0; i < titles.length; i++) {
      if ((titles[i].textContent || '').trim() === 'Run Configuration') return true;
    }
    return false;
  }

  function pct(x) { return x == null ? '—' : (x * 100).toFixed(1) + '%'; }
  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }

  async function runRigorous(limit, topK, statusEl, resultEl) {
    statusEl.textContent = 'Running rigorous eval… (this calls the live LLM per case; may take a minute)';
    resultEl.innerHTML = '';
    try {
      var res = await fetch(API + '/api/eval/run-rigorous', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ top_k: topK, limit: limit }),
      });
      var text = await res.text();
      if (!res.ok) { statusEl.textContent = 'Run failed (HTTP ' + res.status + '): ' + text.slice(0, 300); return; }
      var data = JSON.parse(text);
      statusEl.textContent = 'Run ' + data.run_id + ' complete — ' + data.n_cases + ' cases.';
      render(data, resultEl);
    } catch (e) {
      statusEl.textContent = 'Run error: ' + (e && e.message ? e.message : e);
    }
  }

  function render(d, root) {
    var ax = d.axes || {};
    var html = '';

    // Three axes — explicitly separate
    html += '<div class="pa-kpi-grid pa-kpi-grid--3" style="margin-top:12px">';
    html += axisCard('Competence', pct(ax.competence_answerable), 'answerable correct vs gold', '#6366f1');
    html += axisCard('Honesty', pct(ax.honesty_refusal), 'correctly declined should-refuse', '#10b981');
    html += axisCard('Lexical support', pct(ax.lexical_support_avg), 'word-overlap — NOT correctness', '#f59e0b');
    html += '</div>';
    html += '<div style="font-size:12px;opacity:.7;margin:6px 0 14px">Numeric accuracy: <b>' + pct(ax.numeric_accuracy) +
            '</b> · Label accuracy: <b>' + pct(ax.label_accuracy) + '</b> — kept separate on purpose; no blended score.</div>';

    // Provider errors banner (honesty about LLM availability)
    if (d.provider_errors && d.provider_errors.length) {
      html += '<div class="pa-error" style="margin-bottom:12px">LLM unavailable for ' + d.provider_errors.length +
              ' case(s) — their correctness/honesty reflect an empty answer, NOT model quality. Re-run when the gateway is healthy.</div>';
    }

    // Failure-mode distribution with fault owner
    var fd = d.failure_distribution || {};
    var keys = Object.keys(fd);
    if (keys.length) {
      var owner = { retrieval_miss: 'retriever / architecture', hallucinated_fact: 'model / prompt',
        incomplete_answer: 'model / prompt', false_refusal: 'model / prompt',
        missed_refusal: 'model / refusal prompt', wrong_citation: 'retrieval attribution' };
      html += '<h3 class="pa-card__title">Failure modes (with fault owner)</h3><table class="pa-table"><thead><tr>' +
              '<th>Failure mode</th><th>Count</th><th>Fix owner</th></tr></thead><tbody>';
      keys.sort(function (a, b) { return fd[b] - fd[a]; }).forEach(function (k) {
        html += '<tr><td><code>' + k + '</code></td><td>' + fd[k] + '</td><td>' + (owner[k] || '?') + '</td></tr>';
      });
      html += '</tbody></table>';
      var faults = d.fault_distribution || {};
      html += '<div style="font-size:12px;opacity:.8;margin:6px 0 14px">Fault attribution: ' +
              Object.keys(faults).map(function (f) { return '<b>' + f + '</b>: ' + faults[f]; }).join(' · ') +
              ' — retriever-fault needs chunking/aggregation; model-fault needs prompt/model work.</div>';
    }

    // Caveats — self-documenting
    if (d.caveats && d.caveats.length) {
      html += '<details style="margin:8px 0 14px"><summary style="cursor:pointer;font-weight:600">' +
              'Caveats & limitations (' + d.caveats.length + ') — this report documents its own weak spots</summary>' +
              '<ul style="font-size:12px;opacity:.85;line-height:1.5">' +
              d.caveats.map(function (c) { return '<li>' + escapeHtml(c) + '</li>'; }).join('') + '</ul></details>';
    }

    // Per-case detail
    var rs = d.results || [];
    if (rs.length) {
      html += '<h3 class="pa-card__title">Per-case detail</h3><div class="pa-table-scroll"><table class="pa-table"><thead><tr>' +
              '<th>Case</th><th>Cat</th><th>Pass</th><th>Lexical</th><th>Refused</th><th>Tag</th><th>Fault</th></tr></thead><tbody>';
      rs.forEach(function (r) {
        var f = r.failure || {};
        html += '<tr><td>' + r.case_id + '</td><td>' + r.category + '</td><td>' + (r.primary_pass ? '✓' : '✗') +
                '</td><td>' + r.lexical_support + '</td><td>' + ((r.refusal && r.refusal.applicable) ? (r.refusal.refused ? 'yes' : 'no') : '—') +
                '</td><td>' + (f.tag || '—') + '</td><td>' + (f.fault || '—') + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    root.innerHTML = html;
  }

  function axisCard(label, value, sub, color) {
    return '<div class="pa-kpi-card" style="--kpi-accent:' + color + '"><div class="pa-kpi-card__label">' + label +
           '</div><div class="pa-kpi-card__value">' + value + '</div><div class="pa-kpi-card__sub" style="font-size:11px;opacity:.7">' + sub + '</div></div>';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; });
  }

  function inject() {
    if (!onEvalPage() || document.getElementById(PANEL_ID)) return;
    var page = document.querySelector('.pa-page');
    if (!page) return;

    var card = el('div', { id: PANEL_ID, class: 'pa-card', style: 'margin-top:16px' });
    card.appendChild(el('h3', { class: 'pa-card__title' }, 'Rigorous Eval (labeled test set · 3 axes · fault attribution)'));
    card.appendChild(el('p', { style: 'font-size:12px;opacity:.75;margin:0 0 10px' },
      'Runs the 32-case labeled set (answerable / unanswerable / adversarial), scores competence + honesty + lexical separately, ' +
      'and attributes each failure to the retriever or the model. Gold is computed from the data — nothing hardcoded.'));

    var controls = el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px' });
    var limitInput = el('input', { type: 'number', class: 'pa-input', value: '0', title: 'limit (0 = all 32)', style: 'width:90px' });
    var topkInput = el('input', { type: 'number', class: 'pa-input', value: '6', title: 'retrieval top_k', style: 'width:90px' });
    var runBtn = el('button', { class: 'btn btn--primary' }, 'Run Rigorous Eval');
    controls.appendChild(el('span', { style: 'font-size:12px' }, 'limit'));
    controls.appendChild(limitInput);
    controls.appendChild(el('span', { style: 'font-size:12px' }, 'top_k'));
    controls.appendChild(topkInput);
    controls.appendChild(runBtn);
    card.appendChild(controls);

    var status = el('div', { style: 'font-size:12px;opacity:.8;min-height:18px' });
    var result = el('div', {});
    card.appendChild(status);
    card.appendChild(result);

    runBtn.addEventListener('click', function () {
      runRigorous(parseInt(limitInput.value || '0', 10), parseInt(topkInput.value || '6', 10), status, result);
    });

    page.appendChild(card);
  }

  function start() {
    inject();
    new MutationObserver(function () { inject(); }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
