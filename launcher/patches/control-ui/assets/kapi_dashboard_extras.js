/*
 * kapi_dashboard_extras.js — PM-facing dashboard add-ons.
 *
 * Upstream Kapi's product-analytics Dashboard renders four KPI cards
 * (Total / Distinct Categories / Top Categories list / Avg price) and
 * stops there. For a non-events dataset like a Kaggle products CSV that
 * leaves a PM staring at numbers with nothing to do — no distribution
 * chart, no concentration analysis, no auto-generated observations.
 *
 * This script:
 *   1. Watches for the `.pa-page` dashboard route to render.
 *   2. Reads the currently-selected dataset id from the events/users
 *      dropdowns the upstream UI exposes (the events one wins; if it's
 *      empty, we fall back to users).
 *   3. Fetches /api/analytics/overview + /api/data/{id}/preview from the
 *      analytics backend and computes:
 *        - SVG horizontal-bar chart of the top categorical breakdown
 *          (replaces the cramped text list with a visual ranking)
 *        - SVG histogram of the first numeric column (price/duration/etc.)
 *          with simple bin labels
 *        - Plain-English insights:
 *            * Concentration: "Top 5 categories cover 51% of all rows"
 *            * Pareto check: "20% of categories produce 80% of activity"
 *            * Median vs average gap (skew detection)
 *            * Empty / sparse columns
 *   4. Re-injects on every Lit re-render via MutationObserver — so when
 *      the user changes the dataset dropdown and the SPA re-renders the
 *      page, our extras come back automatically.
 *
 * No external dependencies — SVG is hand-rolled. Backend calls reuse the
 * same auth path as kapi_session_menu.js (Clerk token if present, no
 * Authorization header in local mode).
 *
 * Ships under patches/control-ui/assets/, copied next to the SPA bundle
 * by apply_patches.ps1, loaded via a <script> tag injected into
 * dist/control-ui/index.html.
 */
(function () {
  'use strict';

  if (window.__kapi_dashboard_extras_loaded) return;
  window.__kapi_dashboard_extras_loaded = true;

  var API_BASE =
    (typeof window !== 'undefined' && window.__KAPI_ANALYTICS_URL__) ||
    'http://127.0.0.1:18792';

  var EXTRAS_CLASS = 'kapi-dashboard-extras';
  var lastFetchKey = null;          // dataset id we last fetched data for
  var cached = null;                // { overview, preview }
  var inflight = null;              // promise for in-progress fetch

  // ── helpers ───────────────────────────────────────────────────────────────

  function authHeaders() {
    var h = { Accept: 'application/json' };
    try {
      var clerk = window.Clerk;
      if (clerk && clerk.session) {
        var cached = clerk.session.lastActiveToken;
        if (cached) h.Authorization = 'Bearer ' + cached;
      }
    } catch (_) { /* ignore */ }
    return h;
  }

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Strip the surrounding/internal junk that the products dataset adds
  // to category labels (literal quote chars, trailing spaces, escaped
  // unicode glyphs that turned into question marks). Cosmetic only —
  // never trimmed for groupby logic.
  function tidyLabel(s) {
    if (s == null) return '';
    var t = String(s).trim();
    // Remove a single pair of leading/trailing quotes
    if (t.length >= 2 && t.charAt(0) === '"' && t.charAt(t.length - 1) === '"') {
      t = t.substring(1, t.length - 1);
    }
    return t.trim();
  }

  // Parse a price/numeric field that may include thousands separators,
  // currency symbols, or extraneous whitespace. Returns NaN on failure.
  function toNumber(v) {
    if (v == null) return NaN;
    if (typeof v === 'number') return v;
    var s = String(v).trim();
    if (!s) return NaN;
    // Drop common non-digit chars but keep . , -
    s = s.replace(/[^0-9.,\-]/g, '');
    // If both ',' and '.' are present, treat ',' as thousands sep.
    if (s.indexOf(',') >= 0 && s.indexOf('.') >= 0) {
      s = s.replace(/,/g, '');
    } else if (s.indexOf(',') >= 0 && s.indexOf('.') < 0) {
      // ',' could be either — assume thousands if there's a triple-digit run after
      if (/,\d{3}(?!\d)/.test(s)) s = s.replace(/,/g, '');
      else s = s.replace(',', '.');
    }
    var n = parseFloat(s);
    return isFinite(n) ? n : NaN;
  }

  function fmtNum(n) {
    if (n == null || !isFinite(n)) return '—';
    var abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    if (abs < 10 && abs > 0) return n.toFixed(2);
    return Math.round(n).toLocaleString();
  }

  function fmtPct(n) {
    if (!isFinite(n)) return '—';
    return (n * 100).toFixed(0) + '%';
  }

  // ── DOM detection ─────────────────────────────────────────────────────────

  // The events/users dataset selectors are .pa-select <select> elements.
  // We pick the value of the first non-empty one — that's whatever the
  // user has actively chosen.
  function getActiveDatasetId() {
    var selects = document.querySelectorAll('.pa-page .pa-select');
    for (var i = 0; i < selects.length; i++) {
      var v = selects[i].value;
      if (v) return v;
    }
    return null;
  }

  function getKpiGrid() {
    return document.querySelector('.pa-page .pa-kpi-grid');
  }

  // ── data fetch ────────────────────────────────────────────────────────────

  function fetchAll(datasetId) {
    if (inflight && lastFetchKey === datasetId) return inflight;
    lastFetchKey = datasetId;
    inflight = Promise.all([
      fetch(API_BASE + '/api/analytics/overview?dataset_id=' + encodeURIComponent(datasetId), {
        headers: authHeaders(),
      }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
      fetch(API_BASE + '/api/data/' + encodeURIComponent(datasetId) + '/preview?limit=500', {
        headers: authHeaders(),
      }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
    ]).then(function (results) {
      cached = { overview: results[0], preview: results[1] };
      inflight = null;
      return cached;
    });
    return inflight;
  }

  // ── analysis ──────────────────────────────────────────────────────────────

  // Identify the most-likely categorical dimension to chart. Strategy:
  // re-use whatever Top Categories the backend already grouped on (it's
  // exposed via the KPI's delta_label = "by <column>"). If the overview
  // KPI list isn't available, fall back to the first non-id text column.
  function detectCategoryColumn(overview, preview) {
    if (overview && Array.isArray(overview.kpis)) {
      var top = overview.kpis.find(function (k) {
        return k && Array.isArray(k.value) && k.delta_label && /^by\s/i.test(k.delta_label);
      });
      if (top) {
        return top.delta_label.replace(/^by\s+/i, '').trim();
      }
    }
    // Fallback: first column that isn't an id and has reasonable cardinality.
    if (preview && preview.columns) {
      for (var i = 0; i < preview.columns.length; i++) {
        var col = preview.columns[i];
        if (/(_id|^id$)/i.test(col)) continue;
        var sample = preview.rows.slice(0, 50).map(function (r) { return r[col]; }).filter(function (v) { return v != null && v !== ''; });
        if (!sample.length) continue;
        // Reject mostly-numeric columns
        var numHits = sample.filter(function (v) { return isFinite(toNumber(v)); }).length;
        if (numHits / sample.length > 0.7) continue;
        return col;
      }
    }
    return null;
  }

  // Identify the most-likely numeric dimension to histogram.
  function detectNumericColumn(overview, preview) {
    // First try whatever Avg/Median KPI the backend grouped on.
    if (overview && Array.isArray(overview.kpis)) {
      var avg = overview.kpis.find(function (k) {
        return k && typeof k.value === 'number' && /^(avg|mean|median)\b/i.test(k.label || '');
      });
      // The KPI's unit/delta_label sometimes carry the column name; if so use it.
      // Otherwise fall back to scanning the preview for numeric columns.
    }
    if (!preview || !preview.columns) return null;
    var bestCol = null, bestScore = 0;
    for (var i = 0; i < preview.columns.length; i++) {
      var col = preview.columns[i];
      if (/(_id|^id$)/i.test(col)) continue;
      var sample = preview.rows.map(function (r) { return r[col]; });
      var hits = 0, nz = 0;
      for (var j = 0; j < sample.length; j++) {
        var n = toNumber(sample[j]);
        if (isFinite(n)) { hits++; if (n !== 0) nz++; }
      }
      if (sample.length === 0) continue;
      var score = (hits / sample.length) * (nz > 0 ? 1 : 0.5);
      if (score > bestScore && score > 0.6) {
        bestScore = score; bestCol = col;
      }
    }
    return bestCol;
  }

  // Re-aggregate the preview rows (sample only — backend's overview KPI is
  // authoritative for full-row counts) so the bar chart shows local-sample
  // proportions when the overview lacks a top-categories KPI.
  function aggregateCategory(preview, col, limit) {
    var counts = Object.create(null);
    var total = 0;
    if (preview && preview.rows) {
      preview.rows.forEach(function (r) {
        var v = tidyLabel(r[col]);
        if (!v) return;
        counts[v] = (counts[v] || 0) + 1;
        total++;
      });
    }
    var sorted = Object.keys(counts)
      .map(function (k) { return { name: k, count: counts[k] }; })
      .sort(function (a, b) { return b.count - a.count; });
    return { items: sorted.slice(0, limit || 8), total: total, distinct: sorted.length };
  }

  // Numeric histogram with auto bin count (Sturges).
  function histogram(preview, col, binCount) {
    if (!preview || !preview.rows) return null;
    var values = [];
    preview.rows.forEach(function (r) {
      var n = toNumber(r[col]);
      if (isFinite(n)) values.push(n);
    });
    if (values.length < 5) return null;
    values.sort(function (a, b) { return a - b; });
    var n = values.length;
    var min = values[0], max = values[n - 1];
    if (min === max) return { bins: [{ x0: min, x1: max, count: n }], min: min, max: max, count: n, mean: min, median: min, p95: min };

    // Trim extreme outliers for visual scaling — show 5th to 95th
    // percentile for the chart, but report full mean/median.
    var p95 = values[Math.min(n - 1, Math.floor(n * 0.95))];
    var p05 = values[Math.max(0, Math.floor(n * 0.05))];
    var lo = p05, hi = p95;
    if (hi === lo) { hi = max; lo = min; }

    var bc = binCount || Math.min(12, Math.max(5, Math.ceil(Math.log2(n) + 1)));
    var step = (hi - lo) / bc;
    var bins = [];
    for (var i = 0; i < bc; i++) bins.push({ x0: lo + i * step, x1: lo + (i + 1) * step, count: 0 });
    values.forEach(function (v) {
      if (v < lo || v > hi) return;
      var idx = Math.min(bc - 1, Math.floor((v - lo) / step));
      bins[idx].count++;
    });
    var sum = 0; values.forEach(function (v) { sum += v; });
    var mean = sum / n;
    var median = n % 2 ? values[(n - 1) / 2] : 0.5 * (values[n / 2 - 1] + values[n / 2]);
    return { bins: bins, min: min, max: max, count: n, mean: mean, median: median, p95: p95, lo: lo, hi: hi };
  }

  // Auto-generated observations — like a junior PM writing notes on the data.
  function buildInsights(overview, catAgg, hist, catCol, numCol) {
    var lines = [];

    // 1. Backend top-categories KPI (full-data) takes precedence over
    //    local-sample agg if present.
    var fullTop = null;
    if (overview && Array.isArray(overview.kpis)) {
      var k = overview.kpis.find(function (k) { return k && Array.isArray(k.value); });
      if (k) fullTop = k.value;
    }
    var topUsed = fullTop || (catAgg ? catAgg.items : null);
    var rowTotal = (overview && overview.row_count) || (catAgg ? catAgg.total : 0);

    if (topUsed && topUsed.length && rowTotal > 0) {
      var top5sum = topUsed.slice(0, 5).reduce(function (a, c) { return a + (c.count || 0); }, 0);
      var pct5 = top5sum / rowTotal;
      lines.push('Top 5 ' + escHtml(catCol || 'categories') +
        ' cover <strong>' + fmtPct(pct5) + '</strong> of ' + fmtNum(rowTotal) + ' rows' +
        ' — concentration is ' + (pct5 > 0.7 ? 'very high' : pct5 > 0.4 ? 'moderate' : 'low') + '.');
      if (topUsed.length >= 1) {
        var leader = topUsed[0];
        lines.push('Leading ' + escHtml(catCol || 'category') + ': <strong>' + escHtml(tidyLabel(leader.name)) +
          '</strong> with ' + fmtNum(leader.count) + ' rows (' + fmtPct(leader.count / rowTotal) + ').');
      }
    }

    // 2. Pareto-style observation (only meaningful when distinct count is
    //    large enough that "20%" makes sense).
    if (catAgg && catAgg.distinct >= 10) {
      // Sort full descending — catAgg.items is only top N, so this is
      // approximate when distinct >> N. Mark as "approx" if so.
      var n = Math.max(1, Math.ceil(catAgg.distinct * 0.2));
      var sumTop = catAgg.items.slice(0, Math.min(n, catAgg.items.length))
        .reduce(function (a, c) { return a + c.count; }, 0);
      var pct = sumTop / Math.max(1, catAgg.total);
      var note = catAgg.items.length < n ? ' (sample-only)' : '';
      lines.push('Top <strong>20%</strong> of ' + escHtml(catCol || 'categories') +
        ' (' + n + ' of ' + catAgg.distinct + ') account for <strong>' +
        fmtPct(pct) + '</strong> of activity' + note + '.');
    }

    // 3. Numeric skew & spread.
    if (hist && numCol) {
      var skewWord = '';
      if (hist.mean > hist.median * 1.5) skewWord = 'right-skewed (long tail of high values)';
      else if (hist.median > hist.mean * 1.5) skewWord = 'left-skewed';
      else skewWord = 'roughly symmetric';
      lines.push(escHtml(numCol) + ' is ' + skewWord +
        ' — mean <strong>' + fmtNum(hist.mean) + '</strong>, median <strong>' +
        fmtNum(hist.median) + '</strong>, p95 <strong>' + fmtNum(hist.p95) + '</strong>.');
    }

    // 4. Anomalies passthrough.
    if (overview && Array.isArray(overview.anomalies) && overview.anomalies.length) {
      lines.push('<strong>' + overview.anomalies.length + '</strong> anomalies detected in the data — see the banner above.');
    }

    if (!lines.length) {
      lines.push('Pick a dataset in the dropdowns above to see detailed insights.');
    }
    return lines;
  }

  // ── SVG rendering ─────────────────────────────────────────────────────────

  // Horizontal bar chart of categorical counts. Returns an SVG string.
  // Designed to scale: 22px row height, max ~8 rows.
  function renderBarChart(items, opts) {
    opts = opts || {};
    var W = opts.width || 560;
    var ROW_H = 28;
    var PAD_L = Math.min(220, opts.labelW || 180);
    var PAD_R = 60;
    var PAD_T = 8, PAD_B = 8;
    var H = items.length * ROW_H + PAD_T + PAD_B;
    var max = items.reduce(function (m, c) { return Math.max(m, c.count); }, 0) || 1;
    var barW = W - PAD_L - PAD_R;

    var bars = items.map(function (it, i) {
      var y = PAD_T + i * ROW_H + 4;
      var w = (it.count / max) * barW;
      var label = tidyLabel(it.name);
      var truncated = label.length > 28 ? label.substring(0, 27) + '…' : label;
      // bar + label + count
      return (
        '<g>' +
        '<text x="' + (PAD_L - 8) + '" y="' + (y + 14) + '" text-anchor="end" fill="var(--text-muted, #aaa)" font-size="12">' +
        escHtml(truncated) + '</text>' +
        '<rect x="' + PAD_L + '" y="' + y + '" width="' + w + '" height="' + (ROW_H - 8) + '" rx="4" fill="var(--accent, #5b8def)" opacity="0.85"></rect>' +
        '<text x="' + (PAD_L + w + 6) + '" y="' + (y + 14) + '" fill="var(--text-primary, #f1f1f1)" font-size="12">' +
        fmtNum(it.count) + '</text>' +
        '</g>'
      );
    }).join('');

    return (
      '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" ' +
      'style="width:100%;height:auto;display:block">' + bars + '</svg>'
    );
  }

  // Histogram chart.
  function renderHistogram(hist) {
    if (!hist) return '';
    var W = 560, H = 200;
    var PAD_L = 32, PAD_R = 12, PAD_T = 12, PAD_B = 32;
    var barsW = W - PAD_L - PAD_R;
    var barsH = H - PAD_T - PAD_B;
    var max = hist.bins.reduce(function (m, b) { return Math.max(m, b.count); }, 0) || 1;
    var bw = barsW / hist.bins.length;
    var bars = hist.bins.map(function (b, i) {
      var h = (b.count / max) * barsH;
      var x = PAD_L + i * bw + 1;
      var y = PAD_T + (barsH - h);
      return '<rect x="' + x + '" y="' + y + '" width="' + (bw - 2) + '" height="' + h + '" rx="2" fill="var(--accent, #5b8def)" opacity="0.85">' +
        '<title>' + fmtNum(b.x0) + '–' + fmtNum(b.x1) + ': ' + b.count + '</title></rect>';
    }).join('');
    // x-axis labels: first / mid / last bin
    var lo = hist.lo != null ? hist.lo : hist.min;
    var hi = hist.hi != null ? hist.hi : hist.max;
    var mid = (lo + hi) / 2;
    var xax = (
      '<text x="' + PAD_L + '" y="' + (H - 8) + '" font-size="11" fill="var(--text-muted, #aaa)">' + fmtNum(lo) + '</text>' +
      '<text x="' + (PAD_L + barsW / 2) + '" y="' + (H - 8) + '" font-size="11" text-anchor="middle" fill="var(--text-muted, #aaa)">' + fmtNum(mid) + '</text>' +
      '<text x="' + (W - PAD_R) + '" y="' + (H - 8) + '" font-size="11" text-anchor="end" fill="var(--text-muted, #aaa)">' + fmtNum(hi) + '</text>'
    );
    // y-axis label
    var yax = '<text x="4" y="' + (PAD_T + 12) + '" font-size="11" fill="var(--text-muted, #aaa)">' + fmtNum(max) + '</text>';
    return (
      '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" ' +
      'style="width:100%;height:auto;display:block">' + bars + xax + yax + '</svg>'
    );
  }

  // ── card composition ──────────────────────────────────────────────────────

  function buildExtras(data, datasetId) {
    var overview = data.overview || {};
    var preview = data.preview || {};
    var datasetName = overview.dataset_name || preview.id || 'dataset';
    var rowCount = overview.row_count || (preview.rows ? preview.rows.length : 0);

    var catCol = detectCategoryColumn(overview, preview);
    var numCol = detectNumericColumn(overview, preview);

    var catAgg = catCol ? aggregateCategory(preview, catCol, 8) : null;
    var hist = numCol ? histogram(preview, numCol, 12) : null;

    // If the overview already returned a Top Categories KPI, prefer that
    // (full-data) over our local-sample aggregation for the bar chart.
    var chartItems = catAgg ? catAgg.items : [];
    if (overview && Array.isArray(overview.kpis)) {
      var topKpi = overview.kpis.find(function (k) { return k && Array.isArray(k.value) && (k.label || '').toLowerCase().indexOf('top') >= 0; });
      if (topKpi && topKpi.value && topKpi.value.length) {
        chartItems = topKpi.value.slice(0, 8);
      }
    }

    var insights = buildInsights(overview, catAgg, hist, catCol, numCol);

    var wrap = document.createElement('div');
    wrap.className = EXTRAS_CLASS;
    wrap.setAttribute('data-kapi-extras', '1');
    wrap.style.cssText = 'margin-top:24px;display:grid;grid-template-columns:repeat(2, minmax(0, 1fr));gap:16px;';

    // Insights card (full width, top of section)
    var insightsCard = document.createElement('div');
    insightsCard.className = 'pa-card kapi-extras-insights';
    insightsCard.style.cssText = 'grid-column:1 / -1;background:var(--bg-elevated, var(--card, #191c24));border:1px solid var(--border, rgba(255,255,255,0.08));border-radius:12px;padding:16px 18px;';
    insightsCard.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;">' +
      '<h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text-primary, #f1f1f1);">Insights for ' + escHtml(datasetName) + '</h3>' +
      '<span style="font-size:12px;color:var(--text-muted, #888);">' + fmtNum(rowCount) + ' total rows</span>' +
      '</div>' +
      '<ul style="margin:0;padding-left:20px;line-height:1.65;color:var(--text-primary, #ddd);font-size:13px;">' +
      insights.map(function (l) { return '<li>' + l + '</li>'; }).join('') +
      '</ul>';
    wrap.appendChild(insightsCard);

    // Top categories bar chart
    if (chartItems.length) {
      var c1 = document.createElement('div');
      c1.className = 'pa-card kapi-extras-bars';
      c1.style.cssText = 'background:var(--bg-elevated, var(--card, #191c24));border:1px solid var(--border, rgba(255,255,255,0.08));border-radius:12px;padding:16px 18px;';
      c1.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">' +
        '<h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text-primary, #f1f1f1);">Top ' + escHtml(catCol || 'categories') + '</h3>' +
        '<span style="font-size:12px;color:var(--text-muted, #888);">' + chartItems.length + ' shown' +
        (catAgg ? ' · ' + catAgg.distinct + ' distinct' : '') + '</span>' +
        '</div>' +
        renderBarChart(chartItems, { width: 560 });
      wrap.appendChild(c1);
    }

    // Numeric histogram
    if (hist) {
      var c2 = document.createElement('div');
      c2.className = 'pa-card kapi-extras-hist';
      c2.style.cssText = 'background:var(--bg-elevated, var(--card, #191c24));border:1px solid var(--border, rgba(255,255,255,0.08));border-radius:12px;padding:16px 18px;';
      c2.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">' +
        '<h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text-primary, #f1f1f1);">' +
        escHtml(numCol) + ' distribution</h3>' +
        '<span style="font-size:12px;color:var(--text-muted, #888);">' + fmtNum(hist.count) + ' values · 5–95th pct</span>' +
        '</div>' +
        renderHistogram(hist) +
        '<div style="margin-top:8px;display:flex;gap:14px;font-size:12px;color:var(--text-muted, #888);">' +
        '<span>min ' + fmtNum(hist.min) + '</span>' +
        '<span>median ' + fmtNum(hist.median) + '</span>' +
        '<span>mean ' + fmtNum(hist.mean) + '</span>' +
        '<span>max ' + fmtNum(hist.max) + '</span>' +
        '</div>';
      wrap.appendChild(c2);
    }

    // If neither chart could render but we have at least insights, span
    // the insights full-width and don't pretend there's a 2-column layout.
    if (!chartItems.length && !hist) {
      wrap.style.gridTemplateColumns = '1fr';
    }

    return wrap;
  }

  // ── render loop ───────────────────────────────────────────────────────────

  // Idempotent inject: returns true if it added/updated, false if no work.
  function tryInject() {
    var page = document.querySelector('.pa-page');
    if (!page) return false;

    var grid = getKpiGrid();
    if (!grid) return false; // dashboard not yet populated (loading/empty state)

    var datasetId = getActiveDatasetId();
    if (!datasetId) return false; // empty selector — upstream will show "Select a dataset"

    // Has the user changed datasets since we last rendered? If yes, drop
    // the existing extras so the freshly-fetched data replaces them.
    var existing = page.querySelector('.' + EXTRAS_CLASS);
    if (existing && existing.getAttribute('data-kapi-dataset') === datasetId) {
      return true; // nothing to do
    }

    // Show a placeholder while data loads so the layout doesn't jump.
    if (existing) existing.remove();
    var ph = document.createElement('div');
    ph.className = EXTRAS_CLASS;
    ph.setAttribute('data-kapi-dataset', datasetId);
    ph.setAttribute('data-kapi-state', 'loading');
    ph.style.cssText = 'margin-top:24px;padding:24px;border-radius:12px;background:var(--bg-elevated, #191c24);border:1px solid var(--border, rgba(255,255,255,0.08));color:var(--text-muted, #888);font-size:13px;';
    ph.textContent = 'Loading dashboard insights for this dataset...';
    page.appendChild(ph);

    fetchAll(datasetId).then(function (data) {
      // Confirm the dataset hasn't changed mid-fetch.
      if (getActiveDatasetId() !== datasetId) return;
      var stale = page.querySelector('.' + EXTRAS_CLASS);
      var fresh = buildExtras(data, datasetId);
      fresh.setAttribute('data-kapi-dataset', datasetId);
      if (stale && stale.parentNode === page) {
        page.replaceChild(fresh, stale);
      } else {
        page.appendChild(fresh);
      }
    }).catch(function (e) {
      var still = page.querySelector('.' + EXTRAS_CLASS);
      if (still) {
        still.textContent = 'Could not load extras: ' + e;
        still.setAttribute('data-kapi-state', 'error');
      }
    });

    return true;
  }

  // Lit re-renders the entire `.pa-page` content on every update, so a
  // sibling we appended will be wiped out the next time the user clicks
  // "Refresh" or changes a dropdown. Watch for DOM changes and re-inject.
  // Throttle with requestAnimationFrame so we don't fight the SPA.
  var pending = false;
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      tryInject();
    });
  }

  // Run once shortly after load (dashboard route may not be active yet),
  // then permanently on DOM changes.
  function start() {
    schedule();
    var obs = new MutationObserver(function (mutations) {
      // Cheap relevance check: only react if any mutation touched .pa-page
      // or its descendants. (Mutation records on shadow DOM won't surface
      // here; the dashboard is light DOM so this works.)
      for (var i = 0; i < mutations.length; i++) {
        var t = mutations[i].target;
        if (t && (t.classList && t.classList.contains('pa-page'))) { schedule(); return; }
        // Also react if any added node is a `.pa-kpi-grid` (initial render).
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) { schedule(); return; }
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    // Also re-poll on dropdown change events — Lit's event delegation may
    // not bubble out of the shadow root we're attached to (defensive).
    document.addEventListener('change', function (e) {
      var t = e.target;
      if (t && t.matches && t.matches('.pa-page .pa-select')) schedule();
    }, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
