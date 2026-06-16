"""
calibration.py — evaluate the evaluator.

A same-model LLM judge carries self-preference bias, so its verdicts can't be
taken on faith. This module quantifies how much to trust it:

  * judge_vs_deterministic(): on cases where BOTH a deterministic signal and a
    judge verdict exist, report agreement rate + Cohen's kappa, and list the
    disagreements. High agreement = the judge is tracking objective signals;
    low agreement = investigate (the judge may be drifting on vibes).
  * judge_vs_human(): same, against a small set of hand labels, the gold
    standard for judge quality. Cohen's kappa > ~0.6 is conventionally
    "substantial" agreement.

Cohen's kappa (not raw agreement) because two labelers can agree a lot purely
by chance when one class dominates; kappa corrects for chance agreement.
"""
from __future__ import annotations


def cohens_kappa(a: list[int], b: list[int]) -> float | None:
    """Cohen's kappa for two binary (0/1) label lists of equal length."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1, pb1 = sum(a) / n, sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1.0:                       # both labelers used a single class
        return 1.0 if po == 1.0 else 0.0
    return round((po - pe) / (1 - pe), 3)


def _judge_binary(r: dict) -> int | None:
    """The judge's pass/fail signal for a case, aligned to its category."""
    j = r.get("judge")
    if not j or j.get("error"):
        return None
    if r["category"] == "answerable":
        return 1 if j.get("overall") == "pass" else 0
    # should-refuse: pass == correctly refused
    return 1 if j.get("is_refusal") else 0


def _deterministic_binary(r: dict) -> int | None:
    """The deterministic pass/fail signal for the same case."""
    if r.get("provider_error"):
        return None
    if r["category"] == "answerable":
        c = r.get("correctness", {})
        return None if not c.get("applicable") else (1 if c.get("passed") else 0)
    ref = r.get("refusal", {})
    return 1 if ref.get("refused") else 0


def judge_vs_deterministic(results: list[dict]) -> dict:
    a, b, disagree = [], [], []
    for r in results:
        jd, dd = _judge_binary(r), _deterministic_binary(r)
        if jd is None or dd is None:
            continue
        a.append(jd)
        b.append(dd)
        if jd != dd:
            disagree.append({"case_id": r["case_id"], "judge": jd, "deterministic": dd})
    n = len(a)
    agree = round(sum(1 for x, y in zip(a, b) if x == y) / n, 3) if n else None
    return {"n": n, "agreement": agree, "cohens_kappa": cohens_kappa(a, b),
            "disagreements": disagree}


def judge_vs_human(results: list[dict], human_labels: dict[str, bool]) -> dict:
    """human_labels: {case_id: True(pass)/False(fail)} from a human reviewer."""
    a, b, disagree = [], [], []
    for r in results:
        if r["case_id"] not in human_labels:
            continue
        jd = _judge_binary(r)
        if jd is None:
            continue
        hd = 1 if human_labels[r["case_id"]] else 0
        a.append(jd)
        b.append(hd)
        if jd != hd:
            disagree.append({"case_id": r["case_id"], "judge": jd, "human": hd})
    n = len(a)
    agree = round(sum(1 for x, y in zip(a, b) if x == y) / n, 3) if n else None
    return {"n": n, "agreement": agree, "cohens_kappa": cohens_kappa(a, b),
            "disagreements": disagree}
