"""
Upload-time data quality feedback.

When a user uploads a dataset, telling them only "1,240 rows, 8 columns" misses
the chance to set expectations. This inspects the data and returns plain,
actionable notes: which analyses are available, which columns are mostly empty,
whether there are duplicate rows. It is the difference between a user asking
"why is retention empty?" later and knowing up front "no timestamp column, so
time-series views are off."

Deterministic, cheap (runs on the already-loaded sample), never raises.
Findings carry a level (info/warning) so the UI can style them.
"""
from __future__ import annotations

import pandas as pd

from .normalize import normalize_event_columns

_HIGH_NULL_PCT = 50.0     # a column this empty is barely usable
_SOME_NULL_PCT = 20.0


def _note(level: str, text: str) -> dict:
    return {"level": level, "text": text}


def assess_quality(df: pd.DataFrame) -> list[dict]:
    """Return a list of {level, text} quality notes for an uploaded dataset."""
    notes: list[dict] = []
    try:
        if df is None or df.empty:
            return [_note("warning", "The file has no rows to analyze.")]

        norm = normalize_event_columns(df)

        # What time-based analysis is possible?
        if "timestamp" in norm.columns:
            notes.append(_note("info", "A time column was detected, so trend, funnel, and retention analysis are available."))
        else:
            notes.append(_note("warning", "No time/date column was detected. Time-series views (trends, retention) will be unavailable until one is added."))

        if "user_id" not in norm.columns:
            notes.append(_note("warning", "No user/customer id column was detected. Per-user metrics (MAU, retention) need one."))

        # Mostly-empty columns. Track the noisy ones so we don't also flag them
        # for low variation below (a 70%-empty column reading "single value" is
        # redundant noise).
        mostly_empty: set[str] = set()
        for col in df.columns:
            null_pct = round(df[col].isnull().mean() * 100, 1)
            if null_pct >= _HIGH_NULL_PCT:
                mostly_empty.add(col)
                notes.append(_note("warning", f"Column '{col}' is {null_pct:.0f}% empty and may not be reliable for analysis."))
            elif null_pct >= _SOME_NULL_PCT:
                notes.append(_note("info", f"Column '{col}' has {null_pct:.0f}% missing values."))

        # Duplicate rows.
        dup = int(df.duplicated().sum())
        if dup > 0:
            pct = round(dup / len(df) * 100, 1)
            level = "warning" if pct >= 10 else "info"
            notes.append(_note(level, f"{dup:,} duplicate rows ({pct:.0f}% of the data). Consider de-duplicating before analysis."))

        # Single-value columns carry no signal (skip ones already flagged empty).
        for col in df.columns:
            if col in mostly_empty:
                continue
            try:
                if df[col].nunique(dropna=True) <= 1:
                    notes.append(_note("info", f"Column '{col}' has a single value throughout and won't differentiate segments."))
            except Exception:
                continue

        return notes
    except Exception:
        return notes
