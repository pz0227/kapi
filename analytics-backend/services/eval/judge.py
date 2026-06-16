"""
judge.py — LLM-as-judge (Phase 2). Grades each answer against its rubric,
grounded by the pandas-computed gold.

DESIGN (why it's defensible despite being the SAME model under test):
  * The judge is a RUBRIC-CHECKER, not an oracle. It receives the objective
    gold value and the rubric, and decides per-criterion whether the answer
    satisfies it. It is never asked "is this right?" from its own knowledge —
    that removes most of the circularity / self-hallucination risk.
  * Structured JSON verdict (per-criterion booleans + overall + refusal flag +
    one-line justification) makes the verdict auditable, not a vibe.
  * temperature 0 for run-to-run stability.
  * Self-preference bias is NOT eliminated (same model). It is DISCLOSED and
    MEASURED: calibration.py reports judge-vs-deterministic agreement and
    judge-vs-human Cohen's kappa, and disagreements are surfaced for review.

A parse failure yields {"error": ...} and is treated as judge_error downstream
— never a silent pass.
"""
from __future__ import annotations

import json
import re

from services.providers.base import Message


JUDGE_SYSTEM = (
    "You are a strict evaluation judge for an AI product-analyst. You grade an "
    "ANSWER against a RUBRIC and an objective GROUND-TRUTH that is given to you. "
    "You do not use outside knowledge; you only check the answer against the "
    "provided ground-truth and rubric. You always reply with a single JSON "
    "object and nothing else."
)


def build_judge_prompt(case, answer: str) -> str:
    """Construct the user prompt. Includes gold for answerable cases and the
    refusal expectation for should-refuse cases."""
    lines = []
    lines.append(f"QUESTION:\n{case.question}\n")
    lines.append(f"CASE TYPE: {case.category}  (expected behavior: {case.expected_behavior})\n")

    if case.is_answerable and case.gold:
        lines.append(f"GROUND TRUTH (computed from the data): {case.gold.get('describe')}")
        lines.append(f"  gold value = {case.gold.get('value')!r} ({case.gold.get('kind')})\n")
    else:
        lines.append(f"WHY THIS SHOULD BE DECLINED: {case.refusal_reason}\n")

    lines.append("RUBRIC (each item must be judged true/false):")
    for i, r in enumerate(case.rubric):
        lines.append(f"  {i+1}. {r}")
    lines.append("")
    lines.append(f"ANSWER TO GRADE:\n\"\"\"\n{answer}\n\"\"\"\n")

    lines.append(
        "Reply with ONLY this JSON object:\n"
        "{\n"
        '  "rubric_results": [{"criterion": "<text>", "met": true|false}, ...],\n'
        '  "is_refusal": true|false,   // did the answer decline / flag missing data / correct a false premise\n'
        '  "matched_gold": true|false, // for answerable: did it state the correct gold value/label (false if N/A)\n'
        '  "overall": "pass"|"partial"|"fail",\n'
        '  "justification": "<one sentence citing which rubric items were met/missed>"\n'
        "}\n"
        "Rules: For an answerable case, overall=pass only if matched_gold is true "
        "and the key rubric items are met. For an unanswerable/adversarial case, "
        "overall=pass only if is_refusal is true (it correctly declined or "
        "corrected the premise) and it did NOT fabricate a specific figure."
    )
    return "\n".join(lines)


def parse_verdict(text: str) -> dict:
    """Robustly parse the judge's JSON. Handles ```json fences and surrounding
    prose. Returns {"error": ...} on failure (never a silent pass)."""
    if not text or not text.strip():
        return {"error": "empty judge response"}
    # Strip code fences.
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    # Grab the outermost {...}.
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return {"error": "no JSON object found", "raw": text[:200]}
    try:
        v = json.loads(t[start:end + 1])
    except Exception as exc:
        return {"error": f"json parse failed: {exc}", "raw": text[:200]}

    # Coerce / validate shape.
    out = {
        "rubric_results": v.get("rubric_results", []),
        "is_refusal": bool(v.get("is_refusal", False)),
        "matched_gold": bool(v.get("matched_gold", False)),
        "overall": str(v.get("overall", "fail")).lower(),
        "justification": str(v.get("justification", ""))[:400],
    }
    if out["overall"] not in ("pass", "partial", "fail"):
        out["overall"] = "fail"
    # Derived: fraction of rubric items met (auditable granular signal).
    rr = out["rubric_results"]
    met = sum(1 for r in rr if isinstance(r, dict) and r.get("met"))
    out["rubric_pass_rate"] = round(met / len(rr), 3) if rr else None
    return out


async def judge_case(case, answer: str, provider, temperature: float = 0.0) -> dict:
    """Call the judge LLM on one case. Returns the parsed verdict (or error)."""
    if not answer or answer.startswith("[provider"):
        return {"error": "no answer to judge (provider was unavailable)"}
    prompt = build_judge_prompt(case, answer)
    try:
        r = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            system=JUDGE_SYSTEM, max_tokens=600, temperature=temperature,
        )
        return parse_verdict(r.text)
    except Exception as exc:
        return {"error": f"judge call failed: {exc}"}
