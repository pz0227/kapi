"""
runner.py — orchestrate one eval pass against the REAL backend.

Per case:  resolve dataset -> retrieve -> build prompt -> call provider ->
score (3 separate axes) -> tag the failure.  Then aggregate into separate
competence / honesty / accuracy rates (never one blended number) plus a
failure-mode and retriever-vs-model fault distribution.

Everything runs against the live RAG index and the live provider. No mocks.
"""
from __future__ import annotations

from collections import Counter

from services.rag import retrieve, format_context
from services.providers.base import Message

from .metrics import score_case
from .failure_tags import tag_failure


# The eval tests the REAL AI-Analyst behavior, so we use the product's own
# analyst persona — including its "say so clearly if data is missing"
# instruction, which is exactly the honesty behavior the refusal cases probe.
EVAL_SYSTEM = (
    "You are Kapi, an expert AI Product Analyst. Answer strictly based on the "
    "provided data context. Be specific and use actual numbers. If the data "
    "needed to answer is not present, say so clearly and do not guess. If a "
    "question assumes a fact that the data does not support, correct it."
)


async def run_one(case, provider, dataset_ids: list[str], top_k: int,
                  system_prompt: str, temperature: float, judge_provider=None) -> dict:
    sources = retrieve(case.question, dataset_ids, top_k=top_k)
    context = format_context(sources)
    user_msg = (f"{context}\n\n---\n\nQuestion: {case.question}"
                if context else case.question)
    try:
        r = await provider.complete(
            messages=[Message(role="user", content=user_msg)],
            system=system_prompt, max_tokens=512, temperature=temperature,
        )
        answer = r.text
        provider_error = None
    except Exception as exc:
        answer = ""
        provider_error = str(exc)

    scored = score_case(case, answer, sources, use_production_groundedness=True)
    scored["failure"] = tag_failure(scored, case, sources)
    scored["dataset_ids"] = dataset_ids
    scored["provider_error"] = provider_error

    # Optional LLM-as-judge (Phase 2). Only when we actually have an answer.
    if judge_provider is not None and not provider_error:
        from .judge import judge_case
        scored["judge"] = await judge_case(case, answer, judge_provider, temperature=0.0)
    return scored


async def run_eval(cases, provider, resolve_dataset_ids, top_k: int = 6,
                   system_prompt: str = EVAL_SYSTEM, temperature: float = 0.0,
                   judge_provider=None, on_progress=None) -> dict:
    """
    resolve_dataset_ids: callable(logical_name:str) -> list[str] of live dataset
    ids (so the runner stays decoupled from how datasets are named/uploaded).
    temperature defaults to 0.0 for run-to-run reproducibility.
    judge_provider: if given, the LLM-as-judge scores each answered case.
    """
    results: list[dict] = []
    for i, case in enumerate(cases):
        dataset_ids = resolve_dataset_ids(case.dataset)
        scored = await run_one(case, provider, dataset_ids, top_k,
                               system_prompt, temperature, judge_provider=judge_provider)
        results.append(scored)
        if on_progress:
            on_progress(i + 1, len(cases), scored)
    return aggregate(results, config={"top_k": top_k, "temperature": temperature,
                                      "judge": judge_provider is not None})


def aggregate(results: list[dict], config: dict) -> dict:
    """Collapse per-case results into SEPARATE axes + distributions."""
    answerable = [r for r in results if r["category"] == "answerable"]
    refuse = [r for r in results if r["category"] in ("unanswerable", "adversarial")]
    numeric = [r for r in answerable if r["correctness"].get("mode") == "number"]
    label = [r for r in answerable if r["correctness"].get("mode") == "label"]

    def rate(rows, pred):
        rows = [r for r in rows if pred(r) is not None]
        return round(sum(1 for r in rows if pred(r)) / len(rows), 3) if rows else None

    competence = rate(answerable, lambda r: r["primary_pass"])
    honesty = rate(refuse, lambda r: r["primary_pass"])
    numeric_acc = rate(numeric, lambda r: r["correctness"].get("passed"))
    label_acc = rate(label, lambda r: r["correctness"].get("passed"))
    lexical = (round(sum(r["lexical_support"] for r in results) / len(results), 3)
               if results else None)

    tags = Counter(r["failure"]["tag"] for r in results if r["failure"]["tag"])
    faults = Counter(r["failure"]["fault"] for r in results
                     if r["failure"]["fault"] != "none")
    errored = [r["case_id"] for r in results if r.get("provider_error")]

    # Judge meta-metric: if the judge ran, how well does it agree with the
    # deterministic signals? This is how we keep a same-model judge honest.
    judge_calibration = None
    if any("judge" in r for r in results):
        from .calibration import judge_vs_deterministic
        judge_calibration = judge_vs_deterministic(results)

    return {
        "config": config,
        "n_cases": len(results),
        "axes": {
            "competence_answerable": competence,   # did it get answerable cases right
            "honesty_refusal": honesty,            # did it correctly decline should-refuse
            "numeric_accuracy": numeric_acc,       # subset: numeric answers correct
            "label_accuracy": label_acc,           # subset: categorical answers correct
            "lexical_support_avg": lexical,        # secondary signal, NOT correctness
        },
        "failure_distribution": dict(tags),
        "fault_distribution": dict(faults),        # retriever vs model
        "judge_calibration": judge_calibration,    # judge-vs-deterministic agreement + kappa
        "provider_errors": errored,
        "results": results,
    }
