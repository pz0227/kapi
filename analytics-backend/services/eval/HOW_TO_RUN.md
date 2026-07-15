# Kapi Rigorous Eval — How To Run

A one-page operator guide: the health check, the run commands, where output
lands, and how to read it.

---

## 0. Setup

Run everything from the `analytics-backend/` directory, with its dependencies
installed (`pip install -r requirements.txt`) and the LLM gateway available
(`kapi daemon start`, or an API key configured — see the project README):

```bash
cd analytics-backend
```

> The eval runs against the **live** analytics backend: the real FAISS retriever,
> the real provider, and the uploaded sample datasets (users / events / feature
> usage; the orders cases need a `tiktok_shop_orders` dataset uploaded, otherwise
> they resolve to `retrieval_miss`).

---

---

## 0.5 Offline router evals (no LLM required)

Before the full pipeline eval, two deterministic suites run with no provider:

```bash
python -m services.eval.router_offline_eval   # dev set: 31 answerable + 20 traps
python -m services.eval.holdout_eval          # held-out: 16 answerable + 8 traps
```

These score the compute-first router alone. Every case it answers exactly is a
case removed from hallucination surface entirely.

## 1. Health check (always run this first)

```bash
python -m services.eval.run_eval --limit 1
```

Look at case 1:
- `-> retrieval_miss (provider_error)` → **the LLM/gateway is unavailable.** Fix it
  before a real run. Numbers from a run in this state are NOT real — every answer
  is empty and the report says so.
- `-> PASS` or a real tag with **no** `(provider_error)` → **you are live.** Run
  the full corpus.

---

## 2. The runs

| Command | What it does |
|---|---|
| `python -m services.eval.run_eval` | Full 51-case run: competence + honesty + lexical, failure modes, fault attribution. |
| `python -m services.eval.run_eval --judge` | Same run + the same-model LLM-as-judge + judge-vs-deterministic Cohen's κ. |
| `python -m services.eval.run_eval --compare-topk 6,12` | A/B: identical test set at retrieval depth 6 vs 12, side-by-side with per-case disagreements. |

Useful flags: `--limit N` (first N cases), `--top-k K` (retrieval depth for a single run).

---

## 3. Where the output lands

| Run type | Files |
|---|---|
| single / `--judge` | `storage/eval_runs/<run_id>/report.md` and `result.json` |
| `--compare-topk` | `storage/eval_compare/<run_id>/comparison.md` and `comparison.json` |

`<run_id>` is a timestamp. The `.md` is for reading; the `.json` has full per-case
detail (every metric, judge verdict, failure tag). These outputs are gitignored.

---

## 4. How to read it

### The three axes (the headline — kept separate on purpose)
- **Competence** = fraction of *answerable* cases answered correctly vs the
  computed gold. "Can it answer what it should?"
- **Honesty** = fraction of *unanswerable + adversarial* cases it correctly
  declined / corrected. "Does it refuse what it can't or shouldn't answer?"
- **Lexical support** = average word-overlap with retrieved chunks. A **secondary
  signal only** — high lexical support does NOT mean the answer is correct.
- (sub-metrics) **Numeric accuracy** and **Label accuracy** break competence into
  number-answers vs category-answers.

> Never average competence and honesty into one number. A model can buy
> competence by answering everything, which *destroys* honesty. The whole point
> is to see that trade.

### Failure distribution + fault owner
A table of failure modes with counts and a **fix-owner** column:
- `retrieval_miss` → **retriever / architecture** (the answer wasn't in context;
  fix chunking / add an aggregation step).
- `hallucinated_fact` → **model / prompt** (the info was there or it fabricated a
  confident wrong value).
- `missed_refusal` → **model / refusal prompt** (answered a should-refuse case —
  the dangerous one).
- `false_refusal`, `incomplete_answer` → model/prompt.

> The retriever-vs-model split is the actionable part: it tells you *which layer*
> fixes the failure. On this row-level-RAG system, expect many aggregate questions
> to land in `retrieval_miss` — that's an architectural finding, not a model bug.

### Judge calibration κ (only with `--judge`)
A line like `judge vs deterministic: agreement=0.85 kappa=0.6 (n=40, 6 disagreements)`.
- **Cohen's κ** (chance-corrected agreement) is how much to trust the same-model
  judge — NOT the judge's verdicts on their own. κ > 0.6 ≈ substantial agreement.
- Disagreements are listed so you can eyeball where the judge and the deterministic
  metrics part ways.

### Caveats section
`report.md` ends with a **Caveats & limitations** section that documents the
report's own weak spots (e.g. the bare-integer `u_00500` false-positive, "lexical
support ≠ correctness"). Read it — a self-documenting report is the trustworthy
kind.
