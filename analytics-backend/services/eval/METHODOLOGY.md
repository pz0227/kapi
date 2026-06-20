# Kapi Eval — Methodology & Study Guide

This is the "why" behind every part of the rigorous eval, organized so you can
study it. Each section has: **What it does · Why this design · Tradeoffs ·
Failure modes** — including the real bugs found during the build. The one-liners
in *italics* are interview-ready.

---

## 0. The thesis

> *An eval is not a scoreboard, it's a decision instrument and a debugging tool.*

A good eval answers three different questions, kept separate: **competence** (can
it answer what it should?), **honesty** (does it refuse what it can't?), and
**whose fault is a failure** (retriever or model?). The whole design follows from
refusing to collapse those into one number.

Pipeline: `testset + gold → retrieve → answer → score (3 axes) → tag failure
(fault owner) → judge (optional) → report → compare (A/B)`.

---

## 1. The gold engine (`gold.py`)

**What:** for every answerable question, the correct answer is computed live from
the sample CSVs with pandas (e.g. plan distribution, top category by GMV, total
GMV, distinct-country count). Nothing is hand-typed.

**Why:** *ground truth is derived from the data, not asserted by a human.* If the
data changes, gold changes with it, so the test set can't go stale or be accused
of being rigged. This is the foundation of "defensible."

**Tradeoffs:** gold is coupled to *this* dataset; the eval is only valid when run
against the same datasets the gold was computed from (the runner resolves them by
name).

**Failure modes:** if a case is mislabeled answerable when the data can't actually
answer it, you'd punish a correct refusal. Mitigation: the loader **hard-fails**
if an answerable case has no computed gold, and every gold value was printed and
eyeballed against raw `value_counts()`.

---

## 2. The 3-category test set (`eval_testset.json`, 51 cases)

**What:** 31 **answerable** (gold computed), 12 **unanswerable** (correct behavior
= refuse), 8 **adversarial** (correct behavior = reject a false premise).

**Why:** three axes of behavior — competence, honesty, robustness. *The split is
deliberate so a model that just answers everything confidently fails:* it aces
answerable but tanks unanswerable + adversarial. Most benchmarks omit the refusal
and adversarial cases — which is exactly the part that protects a user from being
misled.

**Case patterns (and what each probes):**
- *Top-1 aggregate* (most common plan, top category): basic rollup.
- *Rank-2 / least* (2nd country, least plan, shortest feature): catches **lazy
  argmax** — a model that always returns the top-1.
- *Distinct-count* (nunique countries/features): cardinality reasoning, which
  row-level RAG can't do from a sample.
- *Total / sum* (total GMV): the purest whole-dataset aggregate.
- *Missing-column refusal* (avg age): the column doesn't exist — cleanest refusal.
- *Missing-metric refusal* (email open rate, delivery time): a *related* field
  exists but the metric needs absent data — tests "related ≠ computable."
- *Missing-history refusal* (free→paid conversion): snapshot, not time series.
- *Data-refuting-superlative adversarial* (paid_search "biggest", Kitchen "growth
  driver"): the data actively contradicts the premise — the strongest sycophancy
  probe, because the right move is to contradict the user with the real number.

**Tradeoffs / failure modes:** authoring labeled cases is slow and dataset-coupled
(why gold is computed, not typed). If an "unanswerable" case is secretly
answerable, you punish a correct answer — mitigated by writing an explicit
`refusal_reason` for every such case and sanity-checking against the data.

---

## 3. Metric 1 — Lexical support (`metrics.lexical_support`)

**What:** fraction of answer sentences sharing ≥2 words with the retrieved chunks
(mirrors the production `groundedness_score`).

**Why:** cheap, deterministic, free; a useful *input* to fault attribution.

**Tradeoffs / failure modes — say this plainly:** *it counts shared words, not
truth.* "MAU is 9,999" scores fully grounded if the sources contain
"MAU/monthly/active/users." It also penalizes correct paraphrase and is blind to
refusals. So it's reported as "lexical support," never "correctness." **This
limitation is the entire reason Metric 2 exists.**

---

## 4. Metric 2 — Answer correctness (`metrics.answer_correctness`)

**What:** is the stated fact true vs gold? *Numeric path:* regex-extract every
number (handles `$`, `%`, `1.4K`, commas, `~`, and the `0.93`→93% case), pass if
any lands within a per-case tolerance; a stated range that brackets gold is a
weaker "range" match. *Label path:* **word-boundary** match + synonyms (so "US"
matches "United States" but never "statUS").

**Why:** the only metric that fails a confident-but-wrong number — the thing
groundedness passes. *Verified:* gold=312, answer "9,999" → **fail**, while lexical
support passes it.

**Tradeoffs / failure modes:** "any number within tolerance" can false-positive if
the gold value coincidentally appears elsewhere in a wrong answer (mitigated by
tight tolerances; exact match for counts). Extraction struggles with multiple
numbers, ranges, and unit confusion ("$29" vs "29 units"). Tolerance encodes how
much rounding is acceptable before "wrong."

---

## 5. Metric 3 — Refusal detection (`metrics.refusal`)

**What:** on a should-refuse case, did the model decline / cite missing data /
correct the premise?

**Why / how it avoids the trap:** it matches **data-availability cues** ("no
data," "not in the dataset," "there is no X") and **premise-correction cues**, and
**deliberately excludes bare confidence hedges** ("not sure," "can't be certain").
*Verified:* "While I can't be certain, MRR is $48,200" reads as **not** a refusal —
that's the key false-positive guard against a hedged hallucination.

**Tradeoffs / failure modes:** deterministic cue-matching can still false-positive
(a refusal phrase used rhetorically). It also surfaces a `gave_figure` flag
("refused but stated a number") for review. This is exactly why the Phase-2 judge
re-scores honesty, and why agreement between them is reported.

---

## 6. Why three SEPARATE axes (no blended score)

> *A single blended number lets a model trade honesty for competence invisibly.*

You can always raise competence by answering more, but that destroys honesty.
Reporting them separately makes the trade explicit and forces a conscious
decision. The A/B section proves this concretely: raising `top_k` was observed to
buy competence while *losing* honesty — a blended score would have hidden it.

---

## 7. Failure-mode tagging + fault attribution (`failure_tags.py`)

**What:** every failure gets one tag **and a fault owner** (retriever vs model).
The pivot is one deterministic question: **was the gold answer derivable from the
retrieved context?** (`gold_in_context`).
- Not derivable + model said nothing concrete → `retrieval_miss` (**retriever /
  architecture**).
- Not derivable + model stated a confident wrong value → `hallucinated_fact`
  (**model** — it should have flagged the limitation instead of fabricating).
- Derivable but wrong → `hallucinated_fact`; derivable but no concrete answer →
  `incomplete_answer`. Should-refuse but answered → `missed_refusal`. Answerable
  but declined → `false_refusal`.

**Why this is the single most useful thing an eval can produce:** *"70% pass" is
unactionable; "45% of failures are retrieval_miss" tells two different teams what
to fix.* retrieval_miss → chunking / add an aggregation step; hallucinated_fact →
prompt / model; missed_refusal → refusal prompt.

**The aggregate-derivation rule:** a computed aggregate (count/mean/argmax) is
**treated as "not in context" by design**, because a rollup isn't reliably present
in any single row. So aggregate failures attribute to retriever/architecture (or
to model fabrication if it stated a confident wrong value) — never a false "the
answer was right there." On this row-level-RAG system, that surfaces the real
architectural finding: *it structurally can't answer aggregates without a
precompute step.*

**Failure modes:** the deterministic attribution is a heuristic; the judge refines
it. Citation-based `wrong_citation` is reserved for the judge (the deterministic
tagger can't verify citations).

---

## 8. The real bug we found: `u_00500` (and how it was fixed)

**The bug:** `extract_numbers("u_00500")` returned `500`, so for a count gold like
*total users = 500*, a retrieved row containing user-id `u_00500` made
`gold_in_context` falsely true — over-crediting the retriever.

**The fix:** for count-type gold, drop numbers **glued to an identifier
character** (letter/underscore on either side), measuring the boundary at the end
of the *numeric* content (a second bug: the regex's trailing `\s*` first made
`"500 users"` look glued to "users" — fixed by using `m.end('int')`, not
`m.end()`).

**Why walk through it:** it's a concrete example of *verifying against real data
catching a real false-positive*, and of fixing carefully — the first fix
over-corrected and dropped the legitimate "500", which the real-data check caught.

**Residual failure modes (disclosed, not hidden):** hyphenated ids/dates
(`ORD-001`, `2026-03-29`) and coincidental row values can still collide; bare
integer counts are inherently ambiguous via pure numeric matching. The
aggregate-derivation rule (Section 7) plus the content-aware judge are what
finally resolve these. **The report prints this caveat itself.**

---

## 9. The gold-grounded LLM judge (`judge.py`)

**What:** a same-model LLM judge that grades each answer against its rubric,
returning structured JSON (per-criterion booleans + overall + refusal flag +
one-line justification).

**Why grounded with gold (the key move):** *the judge is never asked "is this
right?" from its own knowledge* — that's the circular trap. It's given the
pandas-computed gold and asked "does this answer match THIS gold and satisfy THESE
rubric items?" That turns it into a rubric-checker against objective truth and
neutralizes most circularity / self-hallucination.

**Same-model risks (you chose same-model) and mitigations:**
- *Self-preference bias* (a model rates its own outputs higher): can't eliminate
  with one model — so **disclose and measure** it via calibration κ (Section 10).
- *Inconsistency:* temperature 0 + forced JSON schema + per-criterion booleans.
- *Vibes grading:* the rubric forces granular booleans + a justification →
  auditable.
- *JSON breakage:* a robust parser; a parse failure becomes `judge_error`, **never
  a silent pass.**

**Failure modes:** for open-ended cases without a single gold, some circularity
remains (disclosed). A separate-model judge would reduce self-preference — the
deliberate trade here is simplicity + availability, made honest by measuring κ.

---

## 10. Calibration — "evaluate the evaluator" (`calibration.py`)

**What:** `judge_vs_deterministic` reports agreement + **Cohen's κ** between the
judge and the deterministic metrics, and lists disagreements; `judge_vs_human`
does the same against a small set of hand labels.

**Why Cohen's κ, not raw agreement:** *two labelers can agree a lot purely by
chance when one class dominates.* κ corrects for chance agreement, so it's the
honest measure of whether the judge tracks objective signal. κ>0.6 ≈ "substantial."

**Why it matters:** *a same-model judge can't be trusted on faith — the number
that governs how much to believe it is the calibration κ, not the verdicts
themselves.* This is the senior-level move that makes an LLM-judge defensible.

**Failure modes:** κ vs deterministic only tells you the judge agrees with *another
imperfect signal*; the real gold standard is κ vs **human** labels, which needs a
real run to populate. Verified the math: `cohens_kappa([1,1,0,0],[1,0,0,0]) = 0.5`.

---

## 11. A/B comparison (`compare.py`)

**What:** run the identical test set under two arms (e.g. top_k 6 vs 12, or two
providers, or two prompts) and diff: per-axis deltas, side-by-side failure
distributions, and a **per-case disagreement list**.

**Why this is the actual point of evals:** *the absolute score is noisy (test-set
difficulty, judge quirks); the relative comparison cancels those shared biases
because both arms face the same cases and same judge.* So it robustly answers the
real decision — "ship A or B?" — which is the eval's job in practice. The per-case
disagreement list is the most actionable output: it shows *which* questions
flipped, which aggregate deltas can hide.

**Failure modes:** comparison cancels *shared* bias, so if both arms share a blind
spot (same model, same judge), A/B won't reveal it — it measures relative, not
absolute, quality. For pairwise model-vs-model judging later, randomize answer
order to avoid position bias.

---

## 12. Honest limitations (the whole system's weak spots, in one place)

1. Lexical support ≠ correctness (Section 3).
2. Numeric `gold_in_context` for bare counts can false-positive (u_00500 class,
   coincidental values) — mitigated, not eliminated (Section 8).
3. Deterministic refusal detection is cue-matching — the judge refines it (5, 9).
4. The same-model judge carries self-preference bias — disclosed and measured via
   κ (9, 10).
5. The eval is dataset-coupled: valid only against the datasets gold was computed
   from (1).
6. Row-level RAG structurally can't answer aggregates — which the eval *surfaces*
   as a finding rather than hides (7).

> The point of listing these is the point of the whole project: *a self-documenting
> eval that names its own weak spots is more trustworthy than one that hides them.*
