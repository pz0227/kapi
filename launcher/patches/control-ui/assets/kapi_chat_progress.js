/*
 * kapi_chat_progress.js — elapsed-time indicator for AI Analyst responses.
 *
 * Why this exists: a chat call can take 30-120s depending on the LLM
 * provider, RAG context size, and gateway proxy speed. While the call
 * is in flight the upstream SPA renders a static "Thinking..." chip
 * which gives the user no feedback at all — every second past about ten
 * looks like the app is broken even though it is working normally.
 *
 * This script watches the analyst transcript for a "Thinking" indicator
 * and appends a live elapsed-seconds counter ("Thinking… 12s"). Counter
 * goes away the moment the placeholder is replaced by the real answer.
 *
 * Implementation notes:
 *   - Document-level MutationObserver. We never touch SPA internals or
 *     hook into Lit. If the upstream class names ever change we just
 *     stop showing the counter; nothing else breaks.
 *   - Detection is via textContent.startsWith('Thinking') on
 *     .pa-chat-msg__text inside .pa-chat-msg--assistant — matches the
 *     upstream placeholder regardless of theme.
 *   - One global timer per active placeholder; cleared via WeakMap when
 *     the element leaves the DOM or its text changes.
 *   - All styling inlined; no extra CSS file to ship.
 */
(function () {
  'use strict';

  if (window.__kapi_chat_progress_loaded) return;
  window.__kapi_chat_progress_loaded = true;

  // Map<HTMLElement, { startedAt: number, intervalId: number }>
  // Per-element bookkeeping. WeakMap so we don't keep elements alive
  // after the SPA detaches them.
  var tracked = new WeakMap();

  function isThinking(el) {
    if (!el) return false;
    var txt = (el.textContent || '').trim();
    // Upstream placeholder text starts with "Thinking" or "Thinking…".
    // Be permissive about ellipsis variants (".", "...", "…").
    return /^Thinking[\s\.…]*$/i.test(txt);
  }

  function format(seconds) {
    if (seconds < 60) return seconds + 's';
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
  }

  function attach(el) {
    if (!el || tracked.has(el)) return;
    if (!isThinking(el)) return;

    var startedAt = Date.now();
    // Render: "Thinking… 5s" with a soft secondary color so it doesn't
    // shout. The text "Thinking…" stays as the SPA wrote it; we only
    // append the counter span.
    var counter = document.createElement('span');
    counter.className = 'kapi-thinking-elapsed';
    counter.style.cssText =
      'margin-left:8px;font-size:0.85em;opacity:0.7;font-variant-numeric:tabular-nums;';
    counter.textContent = '0s';
    el.appendChild(counter);

    var intervalId = setInterval(function () {
      // Stop if the SPA replaced the placeholder or detached the element.
      if (!el.isConnected || !isThinking(el) || el.querySelector('.kapi-thinking-elapsed') !== counter) {
        cleanup(el);
        return;
      }
      var elapsed = Math.floor((Date.now() - startedAt) / 1000);
      counter.textContent = format(elapsed);
    }, 1000);

    tracked.set(el, { startedAt: startedAt, intervalId: intervalId });
  }

  function cleanup(el) {
    var rec = tracked.get(el);
    if (!rec) return;
    clearInterval(rec.intervalId);
    tracked.delete(el);
    // The counter span (if still attached) is harmless — the SPA will
    // either replace the parent's text wholesale (counter goes with it)
    // or re-render the message bubble entirely. We leave the span alone
    // rather than fight with Lit's reconciler.
  }

  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    // Match the upstream "thinking" placeholder. Fallback selectors cover
    // class-name variants in case a future SPA release renames things.
    var nodes = root.querySelectorAll(
      '.pa-chat-msg--assistant .pa-chat-msg__text, .pa-chat-msg--assistant'
    );
    for (var i = 0; i < nodes.length; i++) attach(nodes[i]);
  }

  function start() {
    // First-pass scan in case anything is already on the page when this
    // script loads (defer-loaded after the SPA's first paint).
    scan(document.body);

    var observer = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        if (m.type === 'childList') {
          for (var j = 0; j < m.addedNodes.length; j++) {
            var n = m.addedNodes[j];
            if (n.nodeType === 1) scan(n);
          }
        } else if (m.type === 'characterData') {
          // Text changed inside an existing element. If a tracked
          // placeholder's text is no longer "Thinking", the next
          // interval tick will clean it up.
          var parent = m.target && m.target.parentElement;
          if (parent && tracked.has(parent) && !isThinking(parent)) {
            cleanup(parent);
          }
        }
      }
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
