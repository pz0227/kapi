"""
User segmentation and feature adoption analysis.
"""
import pandas as pd
import numpy as np
from typing import Any


def compute_segments(
    users_df: pd.DataFrame,
    segment_col: str = "plan",
    metric_cols: list[str] | None = None,
) -> list[dict]:
    """
    Compute per-segment user counts and key metrics.
    """
    if segment_col not in users_df.columns:
        # Fall back to synthetic segmentation by activity quartile
        return _activity_segments(users_df)

    total = len(users_df)
    segments = []

    for seg, group in users_df.groupby(segment_col):
        metrics: dict[str, Any] = {}
        if metric_cols:
            for col in metric_cols:
                if col in group.columns:
                    metrics[col] = round(float(group[col].mean()), 2)

        segments.append({
            "segment": str(seg),
            "count": len(group),
            "pct": round(len(group) / total * 100, 1),
            "metrics": metrics,
        })

    return sorted(segments, key=lambda x: x["count"], reverse=True)


def _activity_segments(users_df: pd.DataFrame) -> list[dict]:
    """Fallback: quartile-based activity segments."""
    if "event_count" not in users_df.columns:
        return []

    users_df = users_df.copy()
    labels = ["Low", "Medium", "High", "Power"]
    users_df["segment"] = pd.qcut(users_df["event_count"], q=4, labels=labels, duplicates="drop")
    total = len(users_df)
    result = []
    for seg, group in users_df.groupby("segment", observed=True):
        result.append({
            "segment": str(seg),
            "count": len(group),
            "pct": round(len(group) / total * 100, 1),
            "metrics": {"avg_events": round(float(group["event_count"].mean()), 1)},
        })
    return result


def compute_feature_adoption(
    events_df: pd.DataFrame,
    feature_map: dict[str, list[str]] | None = None,
    top_n: int = 10,
) -> list[dict]:
    """
    Feature adoption analysis.

    feature_map: {"Feature Name": ["event_a", "event_b"]} — maps display names
    to the event names that represent usage of that feature.
    If None, uses individual event names as features.

    Returns list of FeatureAdoptionRow dicts.
    """
    total_users = events_df["user_id"].nunique()

    # Split into two halves for trend
    sorted_ts = events_df["timestamp"].sort_values()
    mid = sorted_ts.iloc[len(sorted_ts) // 2]
    first_half = events_df[events_df["timestamp"] < mid]
    second_half = events_df[events_df["timestamp"] >= mid]

    rows = []

    if feature_map:
        items = feature_map.items()
    else:
        top_events = events_df["event_name"].value_counts().head(top_n).index.tolist()
        items = [(e, [e]) for e in top_events]  # type: ignore

    for feature_name, event_names in items:
        mask = events_df["event_name"].isin(event_names)
        feature_events = events_df[mask]

        adopters = feature_events["user_id"].nunique()
        adoption_rate = round(adopters / total_users * 100, 1) if total_users else 0.0
        total_uses = len(feature_events)
        avg_uses = round(total_uses / max(adopters, 1), 1)

        # Trend: compare first vs second half adoption rates
        h1_adopters = first_half[first_half["event_name"].isin(event_names)]["user_id"].nunique()
        h2_adopters = second_half[second_half["event_name"].isin(event_names)]["user_id"].nunique()

        if h2_adopters > h1_adopters * 1.05:
            trend = "up"
        elif h2_adopters < h1_adopters * 0.95:
            trend = "down"
        else:
            trend = "stable"

        rows.append({
            "feature": feature_name,
            "adopters": int(adopters),
            "adoption_rate": adoption_rate,
            "avg_uses_per_user": avg_uses,
            "trend": trend,
        })

    return sorted(rows, key=lambda x: x["adoption_rate"], reverse=True)


def compute_executive_summary(
    kpis: list[dict],
    funnel: dict | None,
    retention: dict | None,
    features: list[dict],
) -> str:
    """
    Generate a plain-text executive summary from computed analytics.
    Used as context injection for the AI analyst.
    """
    lines = ["## Product Analytics Executive Summary\n"]

    # KPIs
    lines.append("### KPIs")
    for k in kpis:
        if isinstance(k["value"], (int, float)):
            delta_str = f" ({k['delta']:+.1f}% {k['delta_label']})" if k.get("delta") is not None else ""
            lines.append(f"- **{k['label']}**: {k['value']} {k['unit']}{delta_str}")

    # Funnel
    if funnel and funnel.get("steps"):
        lines.append("\n### Funnel")
        lines.append(f"- Overall conversion: {funnel['overall_conversion']}%")
        lines.append(f"- Biggest drop: {funnel['biggest_drop_step']}")
        for s in funnel["steps"]:
            lines.append(f"  - {s['step']}: {s['count']} users ({s['conversion_rate']}% from prev)")

    # Retention
    if retention and retention.get("rows"):
        lines.append("\n### Retention")
        lines.append(f"- Avg Day-1 retention: {retention['avg_day1']}%")
        lines.append(f"- Avg Day-7 retention: {retention['avg_day7']}%")

    # Feature adoption
    if features:
        lines.append("\n### Top Features by Adoption")
        for f in features[:5]:
            trend_sym = {"up": "↑", "down": "↓", "stable": "→"}.get(f["trend"], "")
            lines.append(
                f"- **{f['feature']}**: {f['adoption_rate']}% adoption {trend_sym}"
            )

    # Auto-diagnosed "so what" layer: ranked, plain-language findings the AI
    # analyst can build on. Deterministic and grounded in the numbers above.
    from .diagnose import diagnose, findings_to_markdown
    findings_md = findings_to_markdown(diagnose(kpis, funnel, retention))
    if findings_md:
        lines.append("\n" + findings_md)

    return "\n".join(lines)
