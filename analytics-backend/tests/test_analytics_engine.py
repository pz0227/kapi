"""
Tests for the deterministic analytics engine: funnel, retention, KPI, anomaly.

Why this file exists: Kapi's headline is a rigorous evaluation module, but that
module tests the LLM layer. The math underneath it, the funnel/retention/KPI
computations whose outputs the agent reports as fact, had zero tests. That is
exactly where a subtle bug produces a confident wrong number, the failure class
this product exists to prevent. These tests lock in the correct behavior and
the load-bearing invariants (monotonic funnels, period-0 retention = 100%,
safe division) so a regression is caught here, not by a user.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from services.analytics.funnel import compute_funnel
from services.analytics.retention import compute_retention
from services.analytics.kpi import _safe_pct_change, compute_anomalies


# ── Funnel ───────────────────────────────────────────────────────────────────

def _events(rows):
    return pd.DataFrame(rows, columns=["user_id", "event_name", "timestamp"])


def test_funnel_is_monotonic_and_rates_correct():
    # 3 users view, 2 add_to_cart, 1 purchase — a clean shrinking funnel.
    t = datetime(2026, 1, 1, 12, 0)
    rows = []
    for u in ["u1", "u2", "u3"]:
        rows.append((u, "view", t))
    for u in ["u1", "u2"]:
        rows.append((u, "cart", t + timedelta(hours=1)))
    rows.append(("u1", "purchase", t + timedelta(hours=2)))

    r = compute_funnel(_events(rows), steps=["view", "cart", "purchase"])
    counts = [s["count"] for s in r["steps"]]
    assert counts == [3, 2, 1]
    # Counts never increase down the funnel.
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1))
    assert r["steps"][0]["conversion_rate"] == 100.0
    assert r["steps"][1]["conversion_rate"] == round(2 / 3 * 100, 1)  # 66.7
    assert r["overall_conversion"] == round(1 / 3 * 100, 1)           # 33.3
    assert r["steps"][2]["absolute_rate"] == round(1 / 3 * 100, 1)


def test_funnel_enforces_time_window():
    # User completes step 2 AFTER the window closes → must not count.
    t = datetime(2026, 1, 1)
    rows = [
        ("u1", "view", t),
        ("u1", "cart", t + timedelta(hours=100)),  # window default 72h
    ]
    r = compute_funnel(_events(rows), steps=["view", "cart"], window_hours=72)
    assert [s["count"] for s in r["steps"]] == [1, 0]


def test_funnel_rejects_out_of_order_steps():
    # User does "cart" BEFORE "view" → the ordered funnel must not credit them.
    t = datetime(2026, 1, 1, 12, 0)
    rows = [
        ("u1", "cart", t),
        ("u1", "view", t + timedelta(hours=1)),
    ]
    r = compute_funnel(_events(rows), steps=["view", "cart"])
    assert [s["count"] for s in r["steps"]] == [1, 0]


def test_funnel_empty_steps_is_safe():
    r = compute_funnel(_events([]), steps=[])
    assert r["overall_conversion"] == 0.0
    assert r["steps"] == []


# ── Retention ────────────────────────────────────────────────────────────────

def test_retention_period_zero_is_always_full():
    # Every user is active in their own first period, so period 0 must be 100%.
    t = datetime(2026, 1, 5)  # a Monday
    rows = []
    for u in ["u1", "u2", "u3", "u4"]:
        rows.append((u, "login", t))
    # Two of them return one week later.
    for u in ["u1", "u2"]:
        rows.append((u, "login", t + timedelta(days=7)))
    df = pd.DataFrame(rows, columns=["user_id", "event_name", "timestamp"])

    r = compute_retention(df, period="week", max_periods=4)
    assert len(r["rows"]) == 1                     # one weekly cohort
    row = r["rows"][0]
    assert row["cohort_size"] == 4
    assert row["periods"][0] == 100.0             # period 0 always full
    assert row["periods"][1] == 50.0             # 2 of 4 returned in week 1
    # No retention rate can exceed 100%.
    assert all(p is None or p <= 100.0 for p in row["periods"])


# ── KPI safe division ────────────────────────────────────────────────────────

def test_safe_pct_change_handles_zero_and_none():
    assert _safe_pct_change(120, 100) == 20.0
    assert _safe_pct_change(80, 100) == -20.0
    assert _safe_pct_change(50, 0) is None        # no division by zero
    assert _safe_pct_change(50, None) is None
    # Negative baseline uses absolute value in the denominator.
    assert _safe_pct_change(0, -100) == 100.0


# ── Anomaly detection ────────────────────────────────────────────────────────

def _daily_events(counts_by_day):
    rows = []
    base = datetime(2026, 1, 1)
    for i, c in enumerate(counts_by_day):
        for _ in range(c):
            rows.append(("u", "e", base + timedelta(days=i)))
    return pd.DataFrame(rows, columns=["user_id", "event_name", "timestamp"])


def test_anomaly_needs_minimum_history():
    # Fewer than 7 days of data → no anomaly calls (not enough to trust a z-score).
    assert compute_anomalies(_daily_events([10, 10, 10])) == []


def test_anomaly_flat_series_flags_nothing():
    # Zero variance → std 0 → guard returns empty rather than dividing by zero.
    assert compute_anomalies(_daily_events([10] * 10)) == []


def test_anomaly_flags_a_real_spike():
    # Ten quiet days then a large spike; the spike should exceed |z| > 2.
    counts = [10] * 10 + [200]
    out = compute_anomalies(_daily_events(counts))
    assert out, "expected anomaly output for a clear spike"
    spike = out[-1]
    assert spike["is_anomaly"] is True
    assert spike["z_score"] > 2.0
    # Quiet days are not anomalies.
    assert all(not row["is_anomaly"] for row in out[:10])
