"""
HELD-OUT evaluation for the compute-first router.

The dev-set score (router_offline_eval.py) is measured on the same 31 cases
the router was tuned against — it proves the loop closed, not that the router
generalizes. This file holds 24 UNSEEN cases written after Phase 2.4 shipped:
different phrasings, Chinese questions, and deliberate traps.

Rules of use:
- The FIRST run's score is the generalization number. Report it as-is.
- Any fix made because of this set burns it: after tuning against it, it is
  a dev set. Note that in the changelog and write a new holdout.

Gold values are computed from the sample data at runtime (never hand-typed).

Run:
    .venv/bin/python -m services.eval.holdout_eval
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd  # noqa: E402

from services.analytics.aggregate_router import try_compute_answer  # noqa: E402

S = BACKEND_ROOT / "data" / "samples"


def _load():
    return {
        "users": pd.read_csv(S / "users.csv"),
        "events": pd.read_csv(S / "events.csv"),
        "features": pd.read_csv(S / "feature_usage.csv"),
        "orders": pd.read_csv(S / "tiktok_shop_orders.csv"),
    }


FILES = {
    "users": "users.csv",
    "events": "events.csv",
    "features": "feature_usage.csv",
    "orders": "tiktok_shop_orders.csv",
}


def build_cases(f: dict) -> list[dict]:
    u, e, ft, o = f["users"], f["events"], f["features"], f["orders"]
    A = []  # answerable: (id, dataset, question, gold_value, kind, tolerance)

    # — different phrasings of sums/means —
    A.append(("H_orders_combined_amount", "orders",
              "What's the combined total_amount across every order?",
              round(float(o["total_amount"].sum()), 4), "num", 0.01))
    A.append(("H_orders_typical_price", "orders",
              "What is the median price of an order line?",
              round(float(o["price"].median()), 4), "num", 0.01))
    A.append(("H_users_avg_sessions", "users",
              "On average, how many session_count does a user have?",
              round(float(u["session_count"].mean()), 4), "num", 0.01))
    A.append(("H_feat_mean_duration", "features",
              "What's the average duration_seconds of a feature session?",
              round(float(ft["duration_seconds"].mean()), 4), "num", 0.01))

    # — filtered, new value phrasings —
    A.append(("H_orders_cancelled_count", "orders",
              "How many orders are cancelled?",
              int((o["order_status"] == "cancelled").sum()) if "cancelled" in set(o["order_status"].unique()) else int((o["order_status"] == "canceled").sum()), "num", 0)) if ("cancelled" in set(o["order_status"].unique()) or "canceled" in set(o["order_status"].unique())) else None
    A.append(("H_users_free_share", "users",
              "What proportion of users sit on the free plan?",
              round(100 * float((u["plan"] == "free").mean()), 1), "num", 1.5))
    A.append(("H_events_web_count", "events",
              "How many events happened on web?",
              int((e["platform"] == "web").sum()), "num", 0))

    # — rankings, unusual wordings —
    A.append(("H_orders_best_category", "orders",
              "Which category brings in the highest revenue overall?",
              str(o.groupby("category")["total_amount"].sum().idxmax()), "label", 0))
    A.append(("H_users_country_leader", "users",
              "Which country do most of our users come from?",
              str(u["country"].value_counts().idxmax()), "label", 0))
    A.append(("H_events_rarest_event", "events",
              "Which event_name shows up the fewest times?",
              str(e["event_name"].value_counts().idxmin()), "label", 0))
    A.append(("H_feat_top_by_usage", "features",
              "Which feature_name is used most often?",
              str(ft["feature_name"].value_counts().idxmax()), "label", 0))
    A.append(("H_orders_runnerup_status", "orders",
              "What is the second most common order_status?",
              str(o["order_status"].value_counts().index[1]), "label", 0))

    # — distinct counts, new phrasing —
    A.append(("H_events_unique_sessions", "events",
              "How many unique session_id values are in the events data?",
              int(e["session_id"].nunique()), "num", 0))
    A.append(("H_orders_distinct_status", "orders",
              "How many different order_status values exist?",
              int(o["order_status"].nunique()), "num", 0))

    # — Chinese phrasings —
    A.append(("H_zh_orders_total", "orders",
              "所有订单的 total_amount 总和是多少？",
              round(float(o["total_amount"].sum()), 4), "num", 0.01))
    A.append(("H_zh_users_count", "users",
              "一共有多少个用户？",
              int(len(u)), "num", 0))

    # — group-by with different phrasing —
    A.append(("H_orders_amount_per_country", "orders",
              "Break down total_amount per country.",
              str(o.groupby("country")["total_amount"].sum().idxmax()), "label", 0))

    A = [a for a in A if a is not None]

    # should-NOT-fire: traps (id, dataset, question, why)
    N = [
        ("T_time_last_quarter", "orders", "What was our total_amount last quarter?", "time-scoped"),
        ("T_time_zh", "orders", "上个月的订单总额是多少？", "time-scoped (Chinese)"),
        ("T_noun_employees", "users", "How many employees does the company have?", "noun not in data"),
        ("T_noun_refunds_money", "orders", "How much money did we lose to fraud?", "no fraud column"),
        ("T_missing_metric_nps", "users", "What is the average NPS score of our users?", "no NPS column"),
        ("T_premise_smuggle", "events", "Since Android users churn more, how many of them are left?", "premise + churn not in data"),
        ("T_opinion", "orders", "Is our pricing strategy good?", "opinion, not aggregate"),
        ("T_future", "orders", "How many orders will we get next month?", "forecast"),
    ]
    return A, N


def _numbers_in(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))]


def _correct(block: str, value, kind: str, tol: float) -> bool:
    if kind == "label":
        return str(value).lower() in block.lower()
    gv = float(value)
    return any(abs(n - gv) <= max(tol, 1e-9) for n in _numbers_in(block))


def main() -> int:
    frames = _load()
    answerable, traps = build_cases(frames)

    hits, misses = [], []
    for cid, ds, q, gold, kind, tol in answerable:
        block = try_compute_answer(q, str(S / FILES[ds]), FILES[ds])
        if block and _correct(block, gold, kind, tol):
            hits.append(cid)
        else:
            misses.append((cid, q, "did not fire" if block is None else f"wrong (gold={gold})", block))

    false_fires = []
    for cid, ds, q, why in traps:
        block = try_compute_answer(q, str(S / FILES[ds]), FILES[ds])
        if block:
            false_fires.append((cid, q, why, block))

    na, nt = len(answerable), len(traps)
    print("=" * 72)
    print("HELD-OUT EVAL (unseen at tuning time — this is the generalization number)")
    print(f"Answerable exact                       : {len(hits)}/{na}  ({100*len(hits)/na:.0f}%)")
    print(f"False fires on traps                   : {len(false_fires)}/{nt}  (target 0)")
    print("-" * 72)
    for cid, q, why, block in misses:
        print(f"  MISS [{cid}] {q!r}\n     -> {why}")
        if block:
            print(f"     block: {block.splitlines()[1] if len(block.splitlines())>1 else block[:100]}")
    for cid, q, why, block in false_fires:
        print(f"  FALSE FIRE [{cid}] ({why}) {q!r}")
        print(f"     block: {block.splitlines()[1] if len(block.splitlines())>1 else block[:100]}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
