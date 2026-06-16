"""
failure_tags.py — classify WHY a case failed, so the eval is a debugging tool
rather than a scoreboard.

THE KEY IDEA: retriever-fault vs model-fault, decided deterministically.
For a failed answerable case we ask one question:

    Was the gold answer even PRESENT in the retrieved context?

  - If NO  -> the retriever never surfaced the information. No model, however
              good, could have answered. The fix lives in chunking / embeddings
              / top_k.  => tag = retrieval_miss
  - If YES -> the information was right there and the model still got it wrong.
              The fix lives in the prompt / model.
                * stated a wrong number/label  => hallucinated_fact
                * stated nothing concrete       => incomplete_answer

This single distinction is the most actionable thing an eval can hand an AI
team, because the two failures are owned by different people and fixed by
completely different work. A bare "70% pass" tells you none of that.

On THIS system the test is especially revealing: it is row-level RAG over CSV
rows, so a global aggregate ("500 total users") essentially never appears
verbatim in any single retrieved row => gold_in_context is False =>
retrieval_miss. That's not a model failure at all; it's an architectural limit
of row-level retrieval for aggregate questions — exactly the kind of finding an
eval exists to produce.
"""
from __future__ import annotations

from .metrics import extract_numbers, _label_present, _numeric_candidates


# The taxonomy. (wrong_citation is reserved for the Phase-2 judge, which can
# read the cited source; the deterministic tagger can't verify citations yet.)
FAILURE_TAGS = (
    "retrieval_miss",     # info wasn't in retrieved context — retriever's fault
    "hallucinated_fact",  # info was available, model stated a wrong fact
    "incomplete_answer",  # info available, model didn't commit to the answer
    "false_refusal",      # answerable question wrongly declined
    "missed_refusal",     # should-refuse question was answered (the dangerous one)
    "wrong_citation",     # cited a source that doesn't support the claim (judge-only)
)


def gold_in_context(gold: dict | None, sources: list[dict]) -> bool:
    """
    Is the gold answer actually present in the retrieved chunk text?
    This is the retriever-vs-model fault detector. Deterministic, no LLM.
    """
    if not gold or not sources:
        return False

    # Aggregates (counts/means/rates/argmax over ALL rows) are not reliably
    # present in row-level chunks. A literal token match — from a metadata
    # header, a coincidental row value, or an id like u_00500 — does NOT mean
    # the model could derive the aggregate. Treat as "not in context" so a
    # failure on these attributes correctly (architecture/retriever or model
    # fabrication), never a false "the answer was right there".
    if gold.get("derivation") == "aggregate":
        return False

    source_text = " ".join(s.get("chunk_text", "") for s in sources)

    if gold.get("kind") == "label":
        present, _ = _label_present(source_text, str(gold["value"]))
        return present

    # numeric: does any number in the context match gold within tolerance?
    gold_val = float(gold["value"])
    tol = float(gold.get("tolerance", 0.0))
    gold_is_pct = gold.get("unit") == "%"

    # COUNT-type gold (integer, not a percent or currency) is the case that gets
    # fooled by identifier digits like u_00500 -> 500. Exclude id-embedded
    # numbers for those. Percentages/currency keep the permissive extractor.
    is_count_type = (not gold_is_pct
                     and gold.get("unit") not in ("USD",)
                     and float(gold_val).is_integer())
    parsed = extract_numbers(source_text, exclude_id_embedded=is_count_type)

    for p in parsed:
        for cand in _numeric_candidates(p, gold_is_pct):
            if abs(cand - gold_val) <= tol or (tol == 0 and cand == gold_val):
                return True
    return False


def tag_failure(scored: dict, case, sources: list[dict]) -> dict:
    """
    Returns {"tag": str|None, "fault": "retriever"|"model"|"none", "rationale": str}.
    tag is None when the case PASSED (no failure to classify).
    """
    if scored["primary_pass"]:
        return {"tag": None, "fault": "none", "rationale": "Case passed."}

    # ── should-refuse cases that failed: it answered when it must not have ──
    if not case.is_answerable:
        kind = "accepted the false premise" if case.category == "adversarial" \
               else "answered a question the data cannot support"
        return {"tag": "missed_refusal", "fault": "model",
                "rationale": f"Expected a refusal but the model {kind}."}

    # ── answerable cases that failed ──
    ref = scored["refusal"]
    corr = scored["correctness"]

    # 1. It wrongly refused a question it could have answered.
    if ref["refused"]:
        return {"tag": "false_refusal", "fault": "model",
                "rationale": "The data supports an answer but the model declined."}

    # Did the model commit to a concrete (but wrong) claim, or give nothing?
    gave_wrong_number = (corr.get("mode") == "number"
                         and corr.get("extracted") and not corr.get("passed"))
    gave_wrong_label = (corr.get("mode") == "label" and not corr.get("passed")
                        and len(scored.get("answer", "")) > 0
                        and "[provider" not in scored.get("answer", ""))

    # 2. Retriever/architecture vs model: was the gold even derivable from context?
    has_ctx = gold_in_context(case.gold, sources)
    if not has_ctx:
        # The answer wasn't (reliably) in context — e.g. an aggregate that
        # row-level RAG can't surface. Two very different model behaviors:
        if gave_wrong_number or gave_wrong_label:
            # It FABRICATED a confident wrong value instead of flagging that it
            # can't compute an exact aggregate from a row sample. Model's fault.
            return {"tag": "hallucinated_fact", "fault": "model",
                    "rationale": "Aggregate not derivable from row-level context, "
                                 "yet the model stated a confident wrong value "
                                 f"({corr.get('extracted') or corr.get('matched')}) "
                                 f"instead of flagging the limitation. Gold={corr['gold']}."}
        # It did not fabricate; the info simply wasn't retrievable. Upstream fix.
        why = ("retrieval returned nothing" if scored["retrieval_count"] == 0
               else "aggregate answers aren't present in row-level chunks — "
                    "needs a precompute/aggregation step, not prompt tuning")
        return {"tag": "retrieval_miss", "fault": "retriever",
                "rationale": f"Gold answer not derivable from context — {why}."}

    # 3. Info WAS retrievable (lookup gold) and the model still missed.
    if gave_wrong_number or gave_wrong_label:
        return {"tag": "hallucinated_fact", "fault": "model",
                "rationale": f"Context supported the answer but the model stated "
                             f"{corr.get('extracted') or corr.get('matched')} "
                             f"instead of {corr['gold']}."}

    return {"tag": "incomplete_answer", "fault": "model",
            "rationale": "Answer retrievable but the model gave no concrete figure."}
