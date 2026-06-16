"""
Funnel analysis — ordered step conversion from an events DataFrame.
"""
import pandas as pd


def compute_funnel(
    events_df: pd.DataFrame,
    steps: list[str],
    user_col: str = "user_id",
    event_col: str = "event_name",
    time_col: str = "timestamp",
    window_hours: int = 72,
) -> dict:
    """
    Compute ordered funnel conversion.

    Args:
        events_df: Events table
        steps: Ordered list of event names defining the funnel
        window_hours: Max time between first and last step for a user to count

    Returns a FunnelResult dict.
    """
    if not steps:
        return {"steps": [], "overall_conversion": 0.0, "biggest_drop_step": ""}

    df = events_df[[user_col, event_col, time_col]].copy()
    df[time_col] = pd.to_datetime(df[time_col])
    df = df[df[event_col].isin(steps)]

    # Build per-user step sets maintaining order
    step_counts: list[int] = []
    users_at_step: list[set] = []

    step0_users = set(df[df[event_col] == steps[0]][user_col].unique())
    users_at_step.append(step0_users)
    step_counts.append(len(step0_users))

    for i in range(1, len(steps)):
        prev_users = users_at_step[i - 1]
        curr_step = steps[i]
        prev_step = steps[i - 1]

        # Users who did step i after step i-1 within window
        merged = df[df[user_col].isin(prev_users)].copy()

        prev_times = (
            df[df[event_col] == prev_step]
            .groupby(user_col)[time_col]
            .min()
            .reset_index()
            .rename(columns={time_col: "prev_time"})
        )
        curr_events = df[df[event_col] == curr_step][[user_col, time_col]].rename(
            columns={time_col: "curr_time"}
        )

        joined = curr_events.merge(prev_times, on=user_col)
        joined["diff_h"] = (joined["curr_time"] - joined["prev_time"]).dt.total_seconds() / 3600
        valid = joined[(joined["diff_h"] >= 0) & (joined["diff_h"] <= window_hours)]

        curr_users = set(valid[user_col].unique()) & prev_users
        users_at_step.append(curr_users)
        step_counts.append(len(curr_users))

    # Build response
    result_steps = []
    biggest_drop = 0.0
    biggest_drop_step = steps[0]

    for i, step in enumerate(steps):
        count = step_counts[i]
        prev_count = step_counts[i - 1] if i > 0 else count
        first_count = step_counts[0]

        conv_rate = round(count / prev_count * 100, 1) if prev_count > 0 else 0.0
        abs_rate = round(count / first_count * 100, 1) if first_count > 0 else 0.0
        drop_off = prev_count - count if i > 0 else 0
        drop_pct = (drop_off / prev_count * 100) if prev_count > 0 else 0.0

        if i > 0 and drop_pct > biggest_drop:
            biggest_drop = drop_pct
            biggest_drop_step = step

        result_steps.append({
            "step": step,
            "count": count,
            "conversion_rate": conv_rate if i > 0 else 100.0,
            "absolute_rate": abs_rate,
            "drop_off": drop_off,
        })

    overall = round(step_counts[-1] / step_counts[0] * 100, 1) if step_counts[0] > 0 else 0.0

    return {
        "steps": result_steps,
        "overall_conversion": overall,
        "biggest_drop_step": biggest_drop_step,
    }


def auto_detect_funnel(events_df: pd.DataFrame, top_n: int = 5) -> list[str]:
    """
    Auto-suggest a meaningful acquisition/activation funnel.
    Prefers known lifecycle event names; falls back to frequency-ordered events.
    Returns a list of step names.
    """
    available = set(events_df["event_name"].unique())

    # Known lifecycle funnels ordered by typical user journey
    LIFECYCLE_CANDIDATES = [
        ["sign_up", "onboarding_start", "onboarding_complete", "dashboard_view", "feature_a_used"],
        ["sign_up", "onboarding_start", "onboarding_complete", "subscription_upgraded"],
        ["page_view", "sign_up", "onboarding_complete", "dashboard_view"],
        ["sign_up", "feature_discover", "dashboard_view", "report_created"],
        ["sign_up", "onboarding_start", "onboarding_complete", "feature_discover", "invite_sent"],
    ]

    for candidate in LIFECYCLE_CANDIDATES:
        matching = [step for step in candidate if step in available]
        if len(matching) >= 3:
            return matching

    # Fallback: pick top events and order by median first-occurrence timestamp per user
    top_events = events_df["event_name"].value_counts().head(top_n + 3).index.tolist()
    event_times = (
        events_df[events_df["event_name"].isin(top_events)]
        .groupby("event_name")["timestamp"]
        .min()   # use min (first time seen), not median
        .sort_values()
    )
    return event_times.index.tolist()[:top_n]
