#!/usr/bin/env python3
"""
Trust mechanisms demo — run in 5 seconds, no LLM, no API key.

Kapi's thesis is that an analytics agent has to earn trust, not assume it. This
script demonstrates the three deterministic mechanisms that back that claim, on
concrete inputs, so you can SEE them work rather than take my word for it:

  1. Compute-first exactness — aggregate questions are computed over the FULL
     dataset, not estimated from a retrieved sample.
  2. Numeric-grounding — a fabricated number in an answer is flagged before a
     user ever sees it.
  3. Honesty guard — a question the data cannot answer gets silence, not a
     plausible-but-wrong guess.

Run:
    cd analytics-backend
    python -m examples.trust_demo        # (or: python ../examples/trust_demo.py)

Needs only the lightweight test deps (pandas, numpy). No provider, no gateway.
"""
import sys
from pathlib import Path

# Make the analytics-backend package importable whether run from repo root or
# from analytics-backend/.
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "analytics-backend"
sys.path.insert(0, str(BACKEND))

import pandas as pd  # noqa: E402
from services.analytics.aggregate_router import try_compute_answer  # noqa: E402
from services.rag.numeric_grounding import numeric_groundedness  # noqa: E402

SAMPLES = BACKEND / "data" / "samples"
ORDERS = SAMPLES / "tiktok_shop_orders.csv"


def rule(title: str):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def demo_compute_first():
    rule("1. COMPUTE-FIRST EXACTNESS  (exact answer over the FULL dataset)")
    df = pd.read_csv(ORDERS)
    true_total = df["total_amount"].sum()
    print(f"Dataset: {ORDERS.name}  ({len(df):,} rows)")
    print(f"Question: 'What is the total revenue?'\n")
    block = try_compute_answer("what is the total revenue?", str(ORDERS), ORDERS.name)
    if block:
        print(block.splitlines()[0])
        for line in block.splitlines()[1:4]:
            print("  " + line)
    print(f"\nHand-check with pandas: total_amount.sum() = {true_total:,.2f}")
    print("The answer is computed over every row, and it says so. No sampling.")


def demo_numeric_grounding():
    rule("2. NUMERIC GROUNDING  (catch a fabricated number)")
    grounding = "sum(total_amount) = 3000000  |  row_count = 1234"
    faithful = "Revenue is about $3.0M across 1,234 orders."
    fabricated = "Revenue is about $5.0M across 1,234 orders."
    for label, answer in [("faithful ", faithful), ("fabricated", fabricated)]:
        r = numeric_groundedness(answer, grounding)
        verdict = "OK" if not r["ungrounded"] else f"FLAGGED {r['ungrounded']}"
        print(f"  [{label}] {answer}")
        print(f"             -> score={r['score']}  {verdict}\n")
    print("The $5.0M claim isn't supported by the data and is flagged before")
    print("it reaches the user. A word-overlap check would have passed it.")


def demo_honesty_guard():
    rule("3. HONESTY GUARD  (silence beats a plausible-but-wrong guess)")
    for q in [
        "What was total revenue last quarter?",   # time-scoped, can't filter
        "What is the average customer age?",       # no such column
    ]:
        block = try_compute_answer(q, str(ORDERS), ORDERS.name)
        print(f"  Q: {q!r}")
        print(f"     -> {'(stays silent, defers to the LLM/analyst)' if block is None else block.splitlines()[0]}\n")
    print("Neither question can be answered from the data, so the compute layer")
    print("declines instead of manufacturing a number.")


def main():
    print("KAPI — TRUST MECHANISMS DEMO  (deterministic, no LLM)")
    if not ORDERS.exists():
        print(f"Sample data not found at {ORDERS}. Run from the repo.")
        return 1
    demo_compute_first()
    demo_numeric_grounding()
    demo_honesty_guard()
    rule("Why this matters")
    print("An analytics agent that makes up numbers is worse than no agent.")
    print("These three layers are how Kapi earns the answer, not just states it.")
    print("Full evaluation: analytics-backend/services/eval/  (51-case suite).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
