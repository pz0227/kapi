"""
gold.py — deterministic ground-truth computation for the eval test set.

WHY THIS FILE EXISTS
--------------------
A defensible eval must compare the system-under-test against an *objective*
reference, not against the grader's opinion. For every "answerable" case in
the test set, the correct answer is computed here directly from the shipped
sample CSVs with pandas. Nothing is hand-typed: if the sample data changes,
the gold value changes with it, and the test set stays honest.

This is the single most important design decision in the whole eval:
**ground truth is derived from the data, not asserted by a human.**

Each gold entry is a dict:
    {
        "value":    <the correct answer — number or label>,
        "kind":     "number" | "label",
        "unit":     str,            # human-readable unit, for the report
        "tolerance": float,         # for numbers: |answer - value| <= tolerance passes
        "describe": str,            # one-line English statement of the truth
    }

The numeric `tolerance` exists because an LLM may legitimately round
("about 93%" vs "93.3%"). Tolerance encodes how much rounding we accept
before calling an answer wrong. Categorical gold ("free", "Electronics")
uses kind="label" and is matched case-insensitively by the metrics layer.

DATASET LOGICAL NAMES
---------------------
The test set refers to datasets by logical name; we map them to the sample
files here so a case never hardcodes a filename:
    users    -> users.csv
    events   -> events.csv
    features -> feature_usage.csv
    orders   -> tiktok_shop_orders.csv
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd


# Resolve <analytics-backend>/data/samples regardless of where this package
# is imported from. services/eval/gold.py -> parents[2] == analytics-backend.
def default_samples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "samples"


_FILES = {
    "users": "users.csv",
    "events": "events.csv",
    "features": "feature_usage.csv",
    "orders": "tiktok_shop_orders.csv",
}


def load_frames(samples_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    """Load the four sample datasets into DataFrames, keyed by logical name."""
    base = samples_dir or default_samples_dir()
    frames: dict[str, pd.DataFrame] = {}
    for name, fname in _FILES.items():
        fp = base / fname
        if fp.exists():
            frames[name] = pd.read_csv(fp)
    return frames


# derivation:
#   "aggregate" — the answer is a rollup over ALL rows (count / mean / rate /
#                 argmax / top-N). It is NOT reliably present in any single
#                 retrieved row, so a literal token match in context is
#                 coincidental and must NOT be read as "the model could derive
#                 this from context." gold_in_context returns False for these.
#   "lookup"    — the answer is a value that lives in a specific row and can
#                 genuinely be retrieved; token presence is meaningful.
# Every current case is an aggregate (the whole point of a product-analyst), so
# this defaults to "aggregate".
def _num(value, unit: str, tolerance: float, describe: str,
         derivation: str = "aggregate") -> dict:
    return {"value": float(value), "kind": "number", "unit": unit,
            "tolerance": float(tolerance), "describe": describe,
            "derivation": derivation}


def _label(value: str, describe: str, derivation: str = "aggregate") -> dict:
    return {"value": str(value), "kind": "label", "unit": "", "tolerance": 0.0,
            "describe": describe, "derivation": derivation}


def compute_gold(samples_dir: Path | None = None) -> dict[str, dict]:
    """
    Compute the gold value for every answerable case id.

    Returns {case_id: gold_dict}. Case ids here MUST match the "id" fields of
    the answerable cases in data/eval_testset.json. If a sample file is
    missing, the cases depending on it are simply omitted (the runner treats a
    missing gold as "cannot score deterministically").
    """
    f = load_frames(samples_dir)
    g: dict[str, dict] = {}

    # ── USERS ────────────────────────────────────────────────────────────────
    if "users" in f:
        u = f["users"]
        plan_counts = u["plan"].value_counts()
        g["A_users_total"] = _num(len(u), "users", 0,
                                  f"There are {len(u)} users in total.")
        g["A_users_plan_top"] = _label(plan_counts.idxmax(),
                                        f"The most common plan is '{plan_counts.idxmax()}' "
                                        f"({int(plan_counts.max())} users).")
        g["A_users_enterprise_count"] = _num(int(plan_counts.get("enterprise", 0)),
                                              "users", 0,
                                              f"{int(plan_counts.get('enterprise', 0))} users are on the enterprise plan.")
        g["A_users_pro_share"] = _num(round(100 * plan_counts.get("pro", 0) / len(u), 1),
                                      "%", 1.5,
                                      f"{round(100*plan_counts.get('pro',0)/len(u),1)}% of users are on the pro plan.")
        g["A_users_top_country"] = _label(u["country"].value_counts().idxmax(),
                                           f"The top country by user count is "
                                           f"'{u['country'].value_counts().idxmax()}'.")
        g["A_users_top_referral"] = _label(u["referral_source"].value_counts().idxmax(),
                                            f"The top referral source is "
                                            f"'{u['referral_source'].value_counts().idxmax()}'.")
        g["A_users_median_events"] = _num(u["event_count"].median(), "events", 0,
                                          f"Median event_count per user is {u['event_count'].median()}.")
        g["A_users_starter_count"] = _num(int(plan_counts.get("starter", 0)), "users", 0,
                                          f"{int(plan_counts.get('starter', 0))} users are on the starter plan.")
        g["A_users_least_plan"] = _label(plan_counts.idxmin(),
                                         f"The least common plan is '{plan_counts.idxmin()}' "
                                         f"({int(plan_counts.min())} users).")
        cc = u["country"].value_counts()
        g["A_users_second_country"] = _label(cc.index[1],
                                             f"The 2nd-largest country by users is '{cc.index[1]}'.")
        g["A_users_distinct_countries"] = _num(u["country"].nunique(), "countries", 0,
                                               f"Users span {u['country'].nunique()} distinct countries.")
        g["A_users_us_share"] = _num(round(100 * (u["country"] == "US").mean(), 1), "%", 1.5,
                                     f"{round(100*(u['country']=='US').mean(),1)}% of users are in the US.")

    # ── EVENTS ───────────────────────────────────────────────────────────────
    if "events" in f:
        e = f["events"]
        ev = e["event_name"].value_counts()
        g["A_events_total"] = _num(len(e), "events", 0,
                                   f"There are {len(e)} event rows in total.")
        g["A_events_top_event"] = _label(ev.idxmax(),
                                          f"The most frequent event is '{ev.idxmax()}' "
                                          f"({int(ev.max())} occurrences).")
        g["A_events_top_platform"] = _label(e["platform"].value_counts().idxmax(),
                                             f"The most common platform is "
                                             f"'{e['platform'].value_counts().idxmax()}'.")
        mobile = e["platform"].str.startswith("mobile").sum()
        g["A_events_mobile_share"] = _num(round(100 * mobile / len(e), 1), "%", 2.0,
                                          f"{round(100*mobile/len(e),1)}% of events come from mobile platforms.")
        g["A_events_second_event"] = _label(ev.index[1],
                                            f"The 2nd most frequent event is '{ev.index[1]}' "
                                            f"({int(ev.iloc[1])} occurrences).")
        g["A_events_distinct_types"] = _num(e["event_name"].nunique(), "types", 0,
                                            f"There are {e['event_name'].nunique()} distinct event types.")
        g["A_events_web_count"] = _num(int((e["platform"] == "web").sum()), "events", 0,
                                       f"{int((e['platform']=='web').sum())} events came from the web platform.")

    # ── FEATURES ─────────────────────────────────────────────────────────────
    if "features" in f:
        ft = f["features"]
        g["A_feat_total"] = _num(len(ft), "uses", 0,
                                 f"There are {len(ft)} feature-usage rows in total.")
        dur = ft.groupby("feature_name")["duration_seconds"].mean().sort_values(ascending=False)
        g["A_feat_longest_duration"] = _label(dur.idxmax(),
                                               f"'{dur.idxmax()}' has the highest average session "
                                               f"duration ({round(dur.max(),1)}s).")
        g["A_feat_shortest_duration"] = _label(dur.idxmin(),
                                               f"'{dur.idxmin()}' has the lowest average session "
                                               f"duration ({round(dur.min(),1)}s).")
        g["A_feat_distinct"] = _num(ft["feature_name"].nunique(), "features", 0,
                                    f"There are {ft['feature_name'].nunique()} distinct features.")

    # ── ORDERS ───────────────────────────────────────────────────────────────
    if "orders" in f:
        o = f["orders"]
        gmv = o.groupby("category")["total_amount"].sum().sort_values(ascending=False)
        g["A_orders_total"] = _num(len(o), "orders", 0,
                                   f"There are {len(o)} orders in total.")
        g["A_orders_top_cat_gmv"] = _label(gmv.idxmax(),
                                            f"The top category by GMV is '{gmv.idxmax()}' "
                                            f"(${round(gmv.max(),2)}).")
        g["A_orders_aov"] = _num(round(o["total_amount"].mean(), 2), "USD", 0.75,
                                 f"Average order value is ${round(o['total_amount'].mean(),2)}.")
        g["A_orders_completed_rate"] = _num(round(100 * (o["order_status"] == "Completed").mean(), 1),
                                            "%", 1.5,
                                            f"{round(100*(o['order_status']=='Completed').mean(),1)}% of orders are Completed.")
        g["A_orders_top_country"] = _label(o["country"].value_counts().idxmax(),
                                           f"The top country by order count is "
                                           f"'{o['country'].value_counts().idxmax()}'.")
        g["A_orders_total_gmv"] = _num(round(o["total_amount"].sum(), 2), "USD", 5.0,
                                       f"Total GMV (sum of total_amount) is ${round(o['total_amount'].sum(),2)}.")
        g["A_orders_second_cat"] = _label(gmv.index[1],
                                          f"The 2nd category by GMV is '{gmv.index[1]}' "
                                          f"(${round(gmv.iloc[1],2)}).")
        g["A_orders_returned"] = _num(int((o["order_status"] == "Returned").sum()), "orders", 0,
                                      f"{int((o['order_status']=='Returned').sum())} orders were Returned.")

    return g


if __name__ == "__main__":
    # Smoke test: print every computed gold value so a human can eyeball the
    # ground truth. This is how we PROVE nothing is fabricated.
    import json
    gold = compute_gold()
    print(f"Computed {len(gold)} gold values:\n")
    for cid, gd in gold.items():
        print(f"  {cid:30s} = {gd['value']!r:>14}  {gd['unit']:5s}  | {gd['describe']}")
