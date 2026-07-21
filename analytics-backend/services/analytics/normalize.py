"""
Column normalization for the analytics engine.

Real-world datasets name the same field a dozen ways: a timestamp might be
`created_at`, `event_time`, or `date`; a user might be `customer_id` or `uid`.
This maps those aliases to the canonical names (`timestamp`, `user_id`,
`event_name`) the engine assumes, plus a best-effort timestamp parse.

Why it lives here and not in a route: it used to live only in the analytics
route, so report generation (a different route) silently produced no KPIs on a
dataset whose time column was named `created_at`. The data was there; the
report just couldn't see it. Shared normalization means every caller, analytics
view, report, future feature, sees the same columns. Idempotent and never
raises: unknown schemas pass through unchanged for the column contract to flag.
"""
from __future__ import annotations

import pandas as pd

# Alias -> canonical, tried in priority order (first match wins).
_TIMESTAMP_ALIASES = (
    "timestamp", "event_time", "occurred_at", "created_at",
    "event_date", "date", "ts", "datetime", "time",
    "updated_at", "logged_at", "received_at", "created_date",
)
_USER_ID_ALIASES = (
    "user_id", "userid", "uid", "customer_id", "account_id",
    "user", "person_id", "visitor_id", "client_id",
)
_EVENT_NAME_ALIASES = (
    "event_name", "event_type", "event", "action_name",
    "action", "type", "name",
)


def _pick(lower: dict[str, str], canonical: str, aliases: tuple[str, ...]) -> tuple[str, str] | None:
    """Return (source_col, canonical) for the first alias present, or None."""
    if canonical in lower.values() or canonical in lower:
        return None  # canonical already present under its own name
    for alias in aliases:
        if alias in lower and lower[alias] != canonical:
            return lower[alias], canonical
    return None


def normalize_event_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename common column-name variants to canonical names and coerce the
    timestamp to datetime. Returns a possibly-new DataFrame; never mutates in
    a way callers rely on, never raises."""
    try:
        lower = {c.lower().strip(): c for c in df.columns}
        rename: dict[str, str] = {}
        for canonical, aliases in (
            ("timestamp", _TIMESTAMP_ALIASES),
            ("user_id", _USER_ID_ALIASES),
            ("event_name", _EVENT_NAME_ALIASES),
        ):
            if canonical in df.columns:
                continue
            hit = _pick(lower, canonical, aliases)
            if hit:
                rename[hit[0]] = hit[1]

        if rename:
            df = df.rename(columns=rename)

        # Best-effort timestamp parse. A column we surfaced as 'timestamp' may
        # still be strings; coerce it, drop unparseable rows, and if nothing
        # parses drop the column so downstream code fails clearly rather than
        # crashing on df['timestamp'].max() over strings.
        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            # format="mixed" lets pandas infer per-value without the noisy
            # per-element fallback warning, while still coercing bad values to NaT.
            parsed = pd.to_datetime(df["timestamp"], errors="coerce", format="mixed")
            if parsed.isna().all():
                df = df.drop(columns=["timestamp"])
            else:
                df = df.assign(timestamp=parsed).dropna(subset=["timestamp"])
        return df
    except Exception:
        return df
