"""
Retention / cohort analysis.
"""
from datetime import timedelta
import pandas as pd
import numpy as np


def compute_retention(
    events_df: pd.DataFrame,
    users_df: pd.DataFrame | None = None,
    period: str = "week",   # "day" | "week" | "month"
    max_periods: int = 8,
    user_col: str = "user_id",
    time_col: str = "timestamp",
    cohort_col: str | None = None,
) -> dict:
    """
    Build a cohort retention matrix.

    Cohorts are defined by the user's first event date (bucketed by period).
    Returns a RetentionResult dict.
    """
    df = events_df[[user_col, time_col]].copy()
    df[time_col] = pd.to_datetime(df[time_col])

    # First seen date per user
    first_seen = df.groupby(user_col)[time_col].min().reset_index()
    first_seen.columns = [user_col, "first_seen"]

    # Bucket into cohort periods
    if period == "day":
        first_seen["cohort"] = first_seen["first_seen"].dt.to_period("D")
        df["period"] = df[time_col].dt.to_period("D")
        period_label = "Day"
    elif period == "week":
        first_seen["cohort"] = first_seen["first_seen"].dt.to_period("W")
        df["period"] = df[time_col].dt.to_period("W")
        period_label = "Week"
    else:
        first_seen["cohort"] = first_seen["first_seen"].dt.to_period("M")
        df["period"] = df[time_col].dt.to_period("M")
        period_label = "Month"

    merged = df.merge(first_seen, on=user_col)
    merged["period_num"] = (merged["period"] - merged["cohort"]).apply(lambda x: x.n)

    # Filter to valid period offsets
    merged = merged[(merged["period_num"] >= 0) & (merged["period_num"] <= max_periods)]

    # Cohort sizes
    cohort_sizes = first_seen.groupby("cohort")[user_col].nunique()

    # Retention matrix
    matrix = (
        merged.groupby(["cohort", "period_num"])[user_col]
        .nunique()
        .reset_index()
    )

    cohorts_ordered = sorted(cohort_sizes.index.tolist())
    period_labels = [f"{period_label} {i}" for i in range(max_periods + 1)]

    rows = []
    avg_by_period: dict[int, list[float]] = {i: [] for i in range(max_periods + 1)}

    for cohort in cohorts_ordered:
        size = int(cohort_sizes[cohort])
        cohort_data = matrix[matrix["cohort"] == cohort].set_index("period_num")

        periods: list[float | None] = []
        for p in range(max_periods + 1):
            if p in cohort_data.index:
                retained = cohort_data.loc[p, user_col]
                rate = round(retained / size * 100, 1)
                periods.append(rate)
                avg_by_period[p].append(rate)
            else:
                periods.append(None)

        rows.append({
            "cohort": str(cohort),
            "cohort_size": size,
            "periods": periods,
        })

    # Summary averages
    def avg(lst: list[float]) -> float | None:
        return round(np.mean(lst), 1) if lst else None

    def period_index(n: int) -> float | None:
        return avg(avg_by_period.get(n, []))

    return {
        "periods": period_labels,
        "rows": rows,
        "avg_day1": period_index(1),
        "avg_day7": period_index(7) if period == "day" else period_index(1),
        "avg_day30": period_index(30) if period == "day" else period_index(4),
    }
