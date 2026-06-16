"""
testset.py — load the labeled eval cases and bind each answerable case to its
deterministically-computed gold value.

The test set JSON (data/eval_testset.json) is the source of truth for case
*metadata* (question, category, rubric, refusal reason). gold.py is the source
of truth for the *numbers*. This module joins them and validates the join, so a
mismatched id (a case marked gold_ref:true with no matching gold computation)
fails loudly at load time instead of silently scoring as "unscorable".
"""
from __future__ import annotations

import json
from pathlib import Path

from .gold import compute_gold


def testset_path() -> Path:
    # services/eval/testset.py -> parents[2] == analytics-backend
    return Path(__file__).resolve().parents[2] / "data" / "eval_testset.json"


class EvalCase:
    """One labeled case, with gold bound in for answerable cases."""

    __slots__ = ("id", "category", "dataset", "question", "rubric",
                 "expected_behavior", "refusal_reason", "gold")

    def __init__(self, raw: dict, gold: dict | None):
        self.id: str = raw["id"]
        self.category: str = raw["category"]              # answerable | unanswerable | adversarial
        self.dataset: str = raw.get("dataset", "")
        self.question: str = raw["question"]
        self.rubric: list[str] = raw.get("rubric", [])
        # For non-answerable cases the *correct* behavior is to decline.
        self.expected_behavior: str = raw.get("expected_behavior", "answer")
        self.refusal_reason: str = raw.get("refusal_reason", "")
        # gold is a dict {value, kind, unit, tolerance, describe} or None.
        self.gold: dict | None = gold

    @property
    def is_answerable(self) -> bool:
        return self.category == "answerable"

    @property
    def should_refuse(self) -> bool:
        return self.category in ("unanswerable", "adversarial")

    def to_public(self) -> dict:
        """Shape sent to the UI / saved in reports (no internal slots leak)."""
        d = {
            "id": self.id,
            "category": self.category,
            "dataset": self.dataset,
            "question": self.question,
            "rubric": self.rubric,
            "expected_behavior": self.expected_behavior,
        }
        if self.refusal_reason:
            d["refusal_reason"] = self.refusal_reason
        if self.gold is not None:
            d["gold"] = self.gold
        return d


def load_cases(samples_dir: Path | None = None,
               path: Path | None = None) -> list[EvalCase]:
    """
    Load all cases and bind gold. Raises ValueError if an answerable case is
    missing its computed gold (authoring/id mismatch) — fail loud, not silent.
    """
    p = path or testset_path()
    doc = json.loads(p.read_text(encoding="utf-8"))
    gold = compute_gold(samples_dir)

    cases: list[EvalCase] = []
    missing: list[str] = []
    for raw in doc["cases"]:
        g = None
        if raw.get("gold_ref"):
            g = gold.get(raw["id"])
            if g is None:
                missing.append(raw["id"])
        cases.append(EvalCase(raw, g))

    if missing:
        raise ValueError(
            "Answerable cases with gold_ref:true but no computed gold "
            f"(check ids match gold.py): {missing}"
        )
    return cases


def testset_stats(cases: list[EvalCase]) -> dict:
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1
    return {"total": len(cases), "by_category": by_cat}


if __name__ == "__main__":
    import sys
    sd = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    cs = load_cases(samples_dir=sd)
    print("Loaded test set:", testset_stats(cs))
    print()
    for c in cs:
        if c.is_answerable:
            print(f"  [{c.category:12s}] {c.id:26s} gold={c.gold['value']!r}")
        else:
            print(f"  [{c.category:12s}] {c.id:26s} expect={c.expected_behavior}")
