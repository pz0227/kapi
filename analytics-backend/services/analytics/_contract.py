"""
Column contracts for the analytics engine.

The route layer normalizes column aliases (created_at -> timestamp) and returns
a helpful 422 before calling the engine. But the engine functions are also
called from other places (reports, the compute-first router, future code), and
there a missing column produced a raw pandas KeyError like `KeyError: 'timestamp'`,
which is opaque and leaks internals.

This gives the engine defense in depth: a single guard that fails with a clear,
actionable message naming the missing column and what the dataset actually has.
Cheap insurance against the exact thing this product warns about, a computation
that breaks (or worse, silently misreads a column) on real-world data.
"""
from __future__ import annotations

import pandas as pd


class MissingColumnsError(ValueError):
    """Raised when a DataFrame lacks columns an analytics function requires."""

    def __init__(self, missing: list[str], available: list[str], fn: str):
        self.missing = missing
        self.available = available
        super().__init__(
            f"{fn} requires column(s) {missing} but the dataset has "
            f"{available}. Rename or map the column before analysis."
        )


def require_columns(df: pd.DataFrame, required: list[str], fn: str) -> None:
    """Raise MissingColumnsError if any required column is absent. Never on the
    happy path; adds no cost beyond a set difference."""
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise MissingColumnsError(missing, list(df.columns), fn)
