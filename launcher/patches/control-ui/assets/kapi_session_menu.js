/*
 * kapi_session_menu.js — right-click context menu for AI Analyst sessions.
 *
 * The upstream pa-analyst bundle renders each session as a <button> with no
 * way to rename or delete it from the UI. After a few "+ New" clicks the
 * sidebar fills with empty "New analysis" entries that you can't clean up.
 *
 * This script ships with the kapi_app_ver_1.3 vendor patches. It pairs
 * with two changes:
 *   1. apply_patches.ps1 substitutes `data-session-id=${t.id}` into every
 *      .pa-session-item button in pa-analyst-*.js so the contextmenu
 *      delegate below can find the session ID.
 *   2. apply_patches.ps1 injects a <script src="./assets/kapi_session_menu.js">
 *      tag into dist/control-ui/index.html so this file actually runs.
 *   3. The patched chat.py adds PUT/DELETE /chat/sessions/{id} endpoints
 *      that this script calls.
 *
 * Document-level delegation rather than per-element handlers means we
 * survive Lit re-renders without any wiring into the SPA's render loop.
 */
(function () {
  'use strict';

  if (window.__kapi_session_menu_loaded) return;
  window.__kapi_session_menu_loaded = true;

  // Match the SPA's resolution of the analytics-backend URL.
  var API_BASE =
    (typeof window !== 'undefined' && window.__KAPI_ANALYTICS_URL__) ||
    'http://127.0.0.1:18792';

  var menuEl = null;

  function closeMenu() {
    if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
    menuEl = null;
  }

  // Close on click outside / Escape / scroll / window resize.
  document.addEventListener('click', closeMenu, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeMenu();
  });
  window.addEventListener('resize', closeMenu);
  window.addEventListener('scroll', closeMenu, true);

  function buildMenu(x, y, sessionId, sessionTitle) {
    closeMenu();

    var m = document.createElement('div');
    m.className = 'kapi-session-context-menu';
    m.setAttribute('data-kapi-menu', '1');
    // Style inline so we don't have to ship a CSS file. Uses Kapi's own
    // theme variables where they exist, with sensible dark/light fallbacks.
    m.style.cssText =
      'position:fixed;left:' + x + 'px;top:' + y + 'px;z-index:99999;' +
      'min-width:160px;padding:6px;font-size:13px;' +
      'font-family:system-ui,-apple-system,Segoe UI,sans-serif;' +
      'background:var(--bg-elevated, var(--bg-secondary, #1f2030));' +
      'color:var(--text-primary, #f1f1f1);' +
      'border:1px solid var(--border-subtle, rgba(255,255,255,0.1));' +
      'border-radius:8px;' +
      'box-shadow:0 8px 24px rgba(0,0,0,0.32);';

    m.innerHTML =
      '<button type="button" class="kapi-ctx-item" data-action="rename">Rename</button>' +
      '<button type="button" class="kapi-ctx-item" data-action="delete">Delete</button>';

    Array.prototype.forEach.call(m.querySelectorAll('.kapi-ctx-item'), function (btn) {
      btn.style.cssText =
        'display:block;width:100%;text-align:left;' +
        'padding:8px 12px;background:transparent;color:inherit;' +
        'border:0;border-radius:4px;font-size:13px;cursor:pointer;';
      if (btn.getAttribute('data-action') === 'delete') {
        btn.style.color = 'var(--text-danger, #f87171)';
      }
      btn.addEventListener('mouseenter', function () {
        btn.style.background = 'var(--bg-hover, rgba(255,255,255,0.06))';
      });
      btn.addEventListener('mouseleave', function () {
        btn.style.background = 'transparent';
      });
    });

    m.addEventListener('click', function (evt) {
      evt.stopPropagation();
      var t = evt.target;
      var action = t && t.getAttribute && t.getAttribute('data-action');
      closeMenu();
      if (action === 'rename') doRename(sessionId, sessionTitle);
      else if (action === 'delete') doDelete(sessionId, sessionTitle);
    });

    document.body.appendChild(m);
    menuEl = m;

    // Keep the menu on-screen.
    requestAnimationFrame(function () {
      var r = m.getBoundingClientRect();
      if (r.right > window.innerWidth - 4) {
        m.style.left = Math.max(4, x - r.width) + 'px';
      }
      if (r.bottom > window.innerHeight - 4) {
        m.style.top = Math.max(4, y - r.height) + 'px';
      }
    });
  }

  function authHeaders(extra) {
    // Match the SPA: read the Clerk token from the global if Clerk is loaded,
    // otherwise send no Authorization header (local mode).
    var h = { Accept: 'application/json' };
    if (extra) {
      Object.keys(extra).forEach(function (k) { h[k] = extra[k]; });
    }
    try {
      var clerk = window.Clerk;
      if (clerk && clerk.session && typeof clerk.session.getToken === 'function') {
        // Best-effort: synchronous read of cached token (Clerk caches it).
        var cached = clerk.session.lastActiveToken;
        if (cached) h.Authorization = 'Bearer ' + cached;
      }
    } catch (_) { /* ignore */ }
    return h;
  }

  // fetch() with an explicit timeout via AbortController, plus a single
  // retry on the timeout / network-failure path. The analytics backend
  // can occasionally take a few seconds to respond if it's mid-RAG on
  // another tab — without a timeout, the browser's default behaviour
  // (give up at the TCP layer with TypeError: Failed to fetch) leaves
  // the user staring at an alert that does not survey the actual cause.
  // With a timeout we can surface a user-friendly message, and the
  // retry covers the common "backend was busy for a moment" case.
  function fetchWithTimeout(url, opts, timeoutMs) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    var merged = Object.assign({}, opts || {}, { signal: controller.signal });
    return fetch(url, merged).finally(function () { clearTimeout(timer); });
  }

  // Translate raw fetch errors into something a non-engineer can act on.
  // 'TypeError: Failed to fetch' is what Chrome surfaces for any
  // network-layer failure — connection reset, CORS preflight dropped,
  // server hung up mid-response — and shipping that to the user as-is
  // looks broken. AbortError is our timeout firing.
  function explainFetchError(err) {
    if (err && err.name === 'AbortError') {
      return 'The analytics backend took too long to respond. It is probably busy answering an AI question — wait for the response, then try again.';
    }
    var msg = err && err.message ? String(err.message) : String(err);
    if (/Failed to fetch|NetworkError|net::ERR/i.test(msg)) {
      return 'Could not reach the analytics backend on :18792. It may be busy with an AI request — wait a few seconds and try again. If this keeps happening, restart kapi-desktop (or run bounce_backend.ps1).';
    }
    return msg;
  }

  // Retry once on network / timeout failure. HTTP errors (4xx/5xx) are
  // NOT retried — those are deterministic and will reproduce.
  function fetchWithRetry(url, opts, timeoutMs) {
    return fetchWithTimeout(url, opts, timeoutMs).catch(function (err) {
      if (err && err.name === 'AbortError') {
        // Timed out once. Try once more with a fresh timeout — covers
        // the "backend just woke up from a slow RAG step" case.
        return fetchWithTimeout(url, opts, timeoutMs);
      }
      var msg = err && err.message ? String(err.message) : String(err);
      if (/Failed to fetch|NetworkError|net::ERR/i.test(msg)) {
        return fetchWithTimeout(url, opts, timeoutMs);
      }
      throw err;
    });
  }

  // 30s is generous: even a fully-blocked event loop should finish a
  // pending RAG step in well under that, and a healthy DELETE call is
  // single-digit milliseconds. Anything past 30s means the backend is
  // truly stuck and the user needs to know.
  var KAPI_FETCH_TIMEOUT_MS = 30000;

  function doRename(id, currentTitle) {
    var next = window.prompt('Rename session:', currentTitle || 'New analysis');
    if (next == null) return; // user cancelled
    var trimmed = String(next).trim();
    if (!trimmed) return;
    if (trimmed === currentTitle) return;
    fetchWithRetry(API_BASE + '/api/chat/sessions/' + encodeURIComponent(id), {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ title: trimmed }),
    }, KAPI_FETCH_TIMEOUT_MS)
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (t) {
            window.alert('Rename failed (HTTP ' + res.status + '): ' + (t || ''));
          });
        }
        // SPA re-fetches sessions on full reload — simplest reliable refresh.
        window.location.reload();
      })
      .catch(function (err) {
        window.alert('Rename failed: ' + explainFetchError(err));
      });
  }

  function doDelete(id, title) {
    var ok = window.confirm(
      'Delete session "' + (title || 'Untitled') + '"?\n\nThis cannot be undone.'
    );
    if (!ok) return;
    fetchWithRetry(API_BASE + '/api/chat/sessions/' + encodeURIComponent(id), {
      method: 'DELETE',
      headers: authHeaders(),
    }, KAPI_FETCH_TIMEOUT_MS)
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (t) {
            window.alert('Delete failed (HTTP ' + res.status + '): ' + (t || ''));
          });
        }
        window.location.reload();
      })
      .catch(function (err) {
        window.alert('Delete failed: ' + explainFetchError(err));
      });
  }

  // Document-level delegation: catches every contextmenu, filters to the
  // session row, reads id/title off the DOM, opens the menu.
  document.addEventListener('contextmenu', function (evt) {
    var btn = evt.target && evt.target.closest && evt.target.closest('.pa-session-item[data-session-id]');
    if (!btn) return;
    var id = btn.getAttribute('data-session-id');
    if (!id) return;
    var titleEl = btn.querySelector('.pa-session-item__title');
    var title = titleEl ? (titleEl.textContent || '').trim() : '';
    evt.preventDefault();
    evt.stopPropagation();
    buildMenu(evt.clientX, evt.clientY, id, title);
  });
})();
