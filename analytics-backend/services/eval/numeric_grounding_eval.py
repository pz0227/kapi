"""
Offline eval for numeric groundedness — does the wrong-number detector work?

For each of several real aggregates computed from the sample data, we build two
answers: one that reports the number faithfully (possibly rounded, the way an
analyst would), and one that fabricates it. A good detector must:
  - pass the faithful answers (score == 1.0, nothing flagged), and
  - flag the fabricated ones (the wrong number appears in `ungrounded`).

We report precision/recall on fabrication detection. Faithful-but-flagged is a
false positive (annoying); fabricated-but-passed is a false negative (dangerous)
— we care most about zero false negatives.

Run:  .venv/bin/python -m services.eval.numeric_grounding_eval
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd  # noqa: E402

from services.rag.numeric_grounding import numeric_groundedness  # noqa: E402

S = BACKEND_ROOT / "data" / "samples"


def _cases():
    o = pd.read_csv(S / "tiktok_shop_orders.csv")
    u = pd.read_csv(S / "users.csv")
    total = float(o["total_amount"].sum())
    aov = float(o["total_amount"].mean())
    n_orders = int(len(o))
    n_users = int(len(u))

    # (grounding_text, faithful_answer, fabricated_answer)
    # Answers are written at the data's real scale (this sample totals ~$2.5K,
    # not millions), and each fabrication moves the number well outside rounding
    # tolerance so a passed fabrication is a true detector miss, not a test bug.
    return [
        (
            f"sum(total_amount) = {round(total, 2)}",
            f"Total revenue across all orders is about ${total:,.0f}.",
            f"Total revenue across all orders is about ${total*1.7:,.0f}.",
        ),
        (
            f"mean(total_amount) = {round(aov, 2)}",
            f"The average order value is ${aov:.2f}.",
            f"The average order value is ${aov*2:.2f}.",
        ),
        (
            f"row_count = {n_orders}",
            f"There are {n_orders} orders in the dataset.",
            f"There are {n_orders + 40} orders in the dataset.",
        ),
        (
            f"row_count = {n_users}",
            f"We have {n_users} users total.",
            f"We have {n_users + 37} users total.",
        ),
    ]


def main() -> int:
    cases = _cases()
    fp = fn = tp = tn = 0
    detail = []

    for ground, faithful, fabricated in cases:
        rf = numeric_groundedness(faithful, ground)
        rx = numeric_groundedness(fabricated, ground)

        # Faithful: should be fully grounded.
        if rf["score"] == 1.0 and not rf["ungrounded"]:
            tn += 1
        else:
            fp += 1
            detail.append(("FALSE POSITIVE", faithful, rf["ungrounded"]))

        # Fabricated: should be flagged.
        if rx["ungrounded"]:
            tp += 1
        else:
            fn += 1
            detail.append(("FALSE NEGATIVE (dangerous)", fabricated, rx))

    n = len(cases)
    recall = tp / n if n else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    print("=" * 72)
    print("NUMERIC-GROUNDING EVAL (fabricated-number detection)")
    print(f"Cases (each has 1 faithful + 1 fabricated answer): {n}")
    print(f"Fabrications caught (recall)   : {tp}/{n}  ({recall*100:.0f}%)")
    print(f"Faithful answers wrongly flagged (false positives): {fp}/{n}")
    print(f"Detection precision            : {precision*100:.0f}%")
    print("-" * 72)
    for tag, ans, info in detail:
        print(f"  {tag}: {ans!r} -> {info}")
    if not detail:
        print("  clean: every faithful answer passed, every fabrication flagged")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
