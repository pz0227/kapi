/*
 * kapi_reports_autoload.js — auto-load report types + report list when the
 * user enters the Reports tab.
 *
 * Why this exists:
 *   The upstream pa-reports view binds the report-types dropdown to
 *   `i.reportTypes`, which is populated by the controller function
 *   `NS(state)` (paReports + paReportTypes via Promise.all on
 *   /api/reports/ and /api/reports/types). But `NS()` is ONLY called
 *   from the "Refresh" button's onClick handler — it is NOT invoked on
 *   tab switch. So entering Reports for the first time renders the
 *   form with an empty dropdown ("-- Select type --" placeholder only)
 *   and the user has no way to know they need to click Refresh first.
 *
 *   This script watches the DOM for the Reports page being mounted and,
 *   if the dropdown is still empty, programmatically clicks the
 *   Refresh button. Single-shot per page mount; resets when the user
 *   navigates away.
 *
 *   Document-level MutationObserver, no SPA internals touched, survives
 *   Lit re-renders by re-checking on every batch.
 */
(function () {
  'use strict';

  if (window.__kapi_reports_autoload_loaded) return;
  window.__kapi_reports_autoload_loaded = true;

  // Track whether we have already auto-clicked Refresh for the current
  // Reports-page mount. Reset whenever the user navigates away.
  var triggeredForCurrentMount = false;

  function isOnReportsPage() {
    // The Reports view's "Generate Report" form card is the most
    // distinctive marker — it's a .pa-card with an h3 / .pa-card__title
    // whose text is exactly "Generate Report". Cheap to check.
    var titles = document.querySelectorAll('.pa-card__title, .pa-card h3');
    for (var i = 0; i < titles.length; i++) {
      var txt = (titles[i].textContent || '').trim();
      if (txt === 'Generate Report') return true;
    }
    return false;
  }

  function findRefreshButton() {
    // Pa-section-header layout: <h3>Reports (n)</h3> + <button>Refresh</button>
    // Walk every .pa-section-header on the page; the right one has an h3
    // starting with "Reports (".
    var headers = document.querySelectorAll('.pa-section-header');
    for (var i = 0; i < headers.length; i++) {
      var h = headers[i];
      var heading = h.querySelector('h3');
      if (!heading) continue;
      if (!/^Reports\s*\(/.test((heading.textContent || '').trim())) continue;
      var buttons = h.querySelectorAll('button');
      for (var j = 0; j < buttons.length; j++) {
        if (/^\s*Refresh\s*$/i.test(buttons[j].textContent || '')) {
          return buttons[j];
        }
      }
    }
    return null;
  }

  function isDropdownStillEmpty() {
    // The Report Type <select> sits inside the Generate Report card.
    // When types haven't loaded it has exactly one <option> (the
    // "-- Select type --" placeholder).
    var labels = document.querySelectorAll('.pa-field__label');
    var typeSelect = null;
    for (var i = 0; i < labels.length; i++) {
      if ((labels[i].textContent || '').trim() === 'Report Type') {
        var field = labels[i].closest('.pa-field') || labels[i].parentElement;
        if (field) typeSelect = field.querySelector('select.pa-select, select');
        if (typeSelect) break;
      }
    }
    if (!typeSelect) return false;  // not rendered yet — wait for next tick
    return typeSelect.options.length <= 1;
  }

  function tryAutoload() {
    if (!isOnReportsPage()) {
      // User navigated away — reset so the next visit re-triggers.
      triggeredForCurrentMount = false;
      return;
    }
    if (triggeredForCurrentMount) return;
    if (!isDropdownStillEmpty()) {
      // Already loaded — no need to fire.
      triggeredForCurrentMount = true;
      return;
    }
    var btn = findRefreshButton();
    if (!btn) return;          // section-header not rendered yet
    if (btn.disabled) return;  // mid-load already
    triggeredForCurrentMount = true;
    btn.click();
  }

  function start() {
    // First-pass scan covers the case where the Reports tab is the
    // initial route on page load.
    tryAutoload();

    var observer = new MutationObserver(function () {
      // Cheap: every batch we just re-evaluate. tryAutoload short-circuits
      // when not on the Reports page or already triggered.
      tryAutoload();
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
