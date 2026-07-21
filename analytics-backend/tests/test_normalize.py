"""Tests for shared column normalization — the fix that made reports see the
same datasets the analytics view does."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from services.analytics.normalize import normalize_event_columns


def test_maps_common_aliases_to_canonical():
    df = pd.DataFrame({
        "created_at": ["2026-01-01", "2026-01-02"],
        "customer_id": ["c1", "c2"],
        "action": ["login", "purchase"],
    })
    out = normalize_event_columns(df)
    assert {"timestamp", "user_id", "event_name"} <= set(out.columns)
    assert pd.api.types.is_datetime64_any_dtype(out["timestamp"])


def test_leaves_canonical_columns_untouched():
    df = pd.DataFrame({"timestamp": pd.to_datetime(["2026-01-01"]),
                       "user_id": ["u1"], "event_name": ["x"]})
    out = normalize_event_columns(df)
    assert list(out.columns) == ["timestamp", "user_id", "event_name"]


def test_idempotent():
    df = pd.DataFrame({"created_at": ["2026-01-01"], "uid": ["u1"], "event": ["e"]})
    once = normalize_event_columns(df)
    twice = normalize_event_columns(once)
    assert set(once.columns) == set(twice.columns)


def test_unparseable_timestamp_column_dropped_not_crashed():
    df = pd.DataFrame({"created_at": ["not-a-date", "also-bad"], "uid": ["u1", "u2"]})
    out = normalize_event_columns(df)
    assert "timestamp" not in out.columns  # dropped, no exception
    assert "user_id" in out.columns


def test_unknown_schema_passes_through():
    df = pd.DataFrame({"foo": [1], "bar": [2]})
    out = normalize_event_columns(df)
    assert list(out.columns) == ["foo", "bar"]
