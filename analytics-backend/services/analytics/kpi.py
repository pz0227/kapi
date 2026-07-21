"""
KPI computation from a loaded events/users DataFrame.
Returns typed KPICard objects consumable by the dashboard.
"""
from datetime import datetime, timedelta
from typing import Any
import pandas as pd
import numpy as np

from ._contract import require_columns


def _safe_pct_change(curr: float, prev: float) -> float | None:
    if prev and prev != 0:
        return round((curr - prev) / abs(prev) * 100, 1)
    return None


def compute_kpis(events_df: pd.DataFrame, users_df: pd.DataFrame | None = None) -> list[dict]:
    """
    Accepts an events DataFrame with at minimum:
      - user_id (str)
      - event_name (str)
      - timestamp (datetime)

    Returns a list of KPI card dicts.
    """
    require_columns(events_df, ["user_id", "event_name", "timestamp"], "compute_kpis")
    cards: list[dict] = []
    now = events_df["timestamp"].max()
    period_end = now
    period_start = now - timedelta(days=30)
    prev_start = period_start - timedelta(days=30)

    curr = events_df[events_df["timestamp"] >= period_start]
    prev = events_df[(events_df["timestamp"] >= prev_start) & (events_df["timestamp"] < period_start)]

    # ── DAU / WAU / MAU ─────────────────────────────────────────────────────
    mau = events_df["user_id"].nunique()
    prev_mau = prev["user_id"].nunique() if len(prev) else mau

    dau_series = curr.groupby(curr["timestamp"].dt.date)["user_id"].nunique()
    avg_dau = round(dau_series.mean(), 0) if len(dau_series) else 0

    # DAU trend
    trend_dau = [
        {"date": str(d), "value": int(v)}
        for d, v in dau_series.tail(30).items()
    ]

    cards.append({
        "label": "MAU",
        "value": int(mau),
        "unit": "users",
        "delta": _safe_pct_change(mau, prev_mau),
        "delta_label": "vs prev 30d",
        "trend": trend_dau,
    })

    cards.append({
        "label": "Avg Daily Active Users",
        "value": int(avg_dau),
        "unit": "users/day",
        "delta": None,
        "delta_label": "30d average",
        "trend": trend_dau,
    })

    # ── Total events ─────────────────────────────────────────────────────────
    total_events = len(curr)
    prev_events = len(prev)
    cards.append({
        "label": "Total Events (30d)",
        "value": int(total_events),
        "unit": "events",
        "delta": _safe_pct_change(total_events, prev_events),
        "delta_label": "vs prev 30d",
        "trend": [],
    })

    # ── Events per user ───────────────────────────────────────────────────────
    if mau > 0:
        epu = round(total_events / mau, 1)
        prev_epu = round(prev_events / max(prev_mau, 1), 1)
        cards.append({
            "label": "Events per User",
            "value": epu,
            "unit": "events",
            "delta": _safe_pct_change(epu, prev_epu),
            "delta_label": "vs prev 30d",
            "trend": [],
        })

    # ── New users ─────────────────────────────────────────────────────────────
    if users_df is not None and "created_at" in users_df.columns:
        users_df = users_df.copy()
        users_df["created_at"] = pd.to_datetime(users_df["created_at"])
        new_users = int((users_df["created_at"] >= period_start).sum())
        prev_new = int(
            ((users_df["created_at"] >= prev_start) & (users_df["created_at"] < period_start)).sum()
        )
        cards.append({
            "label": "New Users (30d)",
            "value": new_users,
            "unit": "users",
            "delta": _safe_pct_change(new_users, prev_new),
            "delta_label": "vs prev 30d",
            "trend": [],
        })

    # ── Top event breakdown ───────────────────────────────────────────────────
    top_events = (
        curr["event_name"].value_counts().head(5).reset_index()
    )
    top_events.columns = ["event", "count"]
    cards.append({
        "label": "Top Events (30d)",
        "value": top_events.to_dict("records"),
        "unit": "breakdown",
        "delta": None,
        "delta_label": "",
        "trend": [],
    })

    return cards


def compute_anomalies(events_df: pd.DataFrame, metric: str = "event_count") -> list[dict]:
    """
    Simple z-score anomaly detection on daily event volume.
    Returns list of AnomalyPoint dicts.
    """
    require_columns(events_df, ["timestamp"], "compute_anomalies")
    daily = events_df.groupby(events_df["timestamp"].dt.date).size().reset_index()
    daily.columns = ["date", "event_count"]
    daily = daily.sort_values("date")

    if len(daily) < 7:
        return []

    mean = daily["event_count"].mean()
    std = daily["event_count"].std()
    if std == 0:
        return []

    daily["z_score"] = (daily["event_count"] - mean) / std
    daily["is_anomaly"] = daily["z_score"].abs() > 2.0
    rolling_mean = daily["event_count"].rolling(7, min_periods=1).mean()

    result = []
    for _, row in daily.iterrows():
        result.append({
            "date": str(row["date"]),
            "metric": metric,
            "value": float(row["event_count"]),
            "expected": float(rolling_mean[row.name]),
            "z_score": round(float(row["z_score"]), 2),
            "is_anomaly": bool(row["is_anomaly"]),
        })
    return result
