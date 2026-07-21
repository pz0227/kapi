# Changelog

Kapi is developed in public as an iterative project. This log groups the work
into phases so the evolution is legible: measure first, then make the product
trustworthy, then harden it.

## Phase 4 — Insight, robustness, and engineering rigor (Jul 2026)

**Product value**
- **Auto-diagnosis layer**: the analyst reads the numbers, not just reports them.
  Ranked, plain-language findings ("Day-7 retention at 7% points to an activation
  gap") with a suggested next step, deterministic and grounded in the metrics.
- **Opportunity sizing**: funnel findings quantify the fix, not just the problem
  ("720 users drop here; recovering half is ~360 more through the funnel"), sized
  from the real drop-off.
- **Upload-time data-quality feedback**: on upload you learn what's analyzable up
  front (missing time/user columns, mostly-empty columns, duplicates) instead of
  hitting an empty view later.

**Bugs found and fixed (mostly in my own code, on real-world conditions)**
- Streaming chat yielded inside a `finally` block — corrupts the response when a
  client disconnects mid-stream. Restructured; added an AST regression guard.
- `reports.py` used `Path` without importing it — a latent `NameError` that only
  fired when report generation ran.
- Report generation silently produced no KPIs on datasets with aliased column
  names (`customer_id`, `created_at`) because normalization lived only in the
  analytics route. Extracted a shared normalizer; both paths use it.
- Engine functions raised opaque `KeyError` on missing columns; added a column
  contract that fails with a clear, actionable message.

**Engineering**
- GitHub Actions CI on every push and PR; runs 75 unit tests plus two offline
  eval suites in seconds (lightweight test deps, no ~2GB torch download).
- Test coverage 0 → 75: analytics engine, router, eval scoring (Cohen's κ
  verified against a hand-computed textbook value), numeric grounding, diagnosis,
  quality, normalization, stream safety, and an end-to-end pipeline integration
  test.
- Streaming upload size cap enforced during the write (disk-fill protection).
- Removed dead imports across the route layer.

## Phase 3 — Numeric groundedness (Jul 2026)

- **Wrong-number detection**: beyond word-overlap groundedness, every figure an
  answer states is checked against the grounding context (retrieved sources +
  full-dataset computations). A number supported by nothing is flagged as
  potentially fabricated, the highest-severity failure for a data agent.

## Phase 2 — Compute-first router: trustworthy answers (Jul 2026)

- Aggregate questions ("total revenue", "orders per region") are computed over
  the **full dataset** with pandas, not estimated from a retrieved sample, and
  the answer discloses it.
- Eval-driven improvement loop took exact-answer coverage from 19% → 100% on the
  dev set with 0/20 false fires on adversarial questions.
- Honesty guards: the router stays silent on questions it can't ground (time-
  scoped, missing-metric, premise-smuggling) rather than approximating.
- Held-out eval set (written after tuning): 16/16 exact, 0/8 false fires on first
  run.
- mtime-aware DataFrame cache + file-size ceiling for the full-file reads.

## Phase 1 — Measure before optimizing (Jul 2026)

- Per-stage latency instrumentation (embed / search / context / first token).
- Query-embedding cache.
- Honest index-coverage disclosure: the UI and the model are told when only the
  first N of M rows are searchable, so partial answers are flagged, never implied
  complete.
- macOS/Linux quickstart.

## Baseline

- AI product-analytics agent on OpenClaw: RAG Q&A with groundedness scoring, KPI
  dashboards, funnel / retention / anomaly analysis, one-click PM reports, and a
  51-case labeled evaluation module (three axes, failure taxonomy, retriever-vs-
  model fault attribution, LLM-as-judge with Cohen's κ calibration).
