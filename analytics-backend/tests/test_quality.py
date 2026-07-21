"""Tests for upload-time data-quality feedback."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from services.analytics.quality import assess_quality


def _texts(notes): return " || ".join(n["text"] for n in notes)


def test_flags_missing_time_and_user_columns():
    df = pd.DataFrame({"product": ["a", "b"], "price": [1, 2]})
    notes = assess_quality(df)
    t = _texts(notes)
    assert "time/date column" in t.lower()
    assert "user/customer id" in t.lower()


def test_detects_time_column_enables_analysis():
    df = pd.DataFrame({"created_at": ["2026-01-01", "2026-01-02"], "customer_id": ["c1", "c2"]})
    notes = assess_quality(df)
    assert any("time column was detected" in n["text"] for n in notes)


def test_flags_mostly_empty_column():
    df = pd.DataFrame({"user_id": ["u1", "u2", "u3", "u4"],
                       "note": [None, None, None, "x"]})  # 75% empty
    notes = assess_quality(df)
    assert any("empty" in n["text"] and n["level"] == "warning" for n in notes)


def test_flags_duplicates():
    df = pd.DataFrame({"user_id": ["u1", "u1", "u1"], "event": ["x", "x", "x"]})
    notes = assess_quality(df)
    assert any("duplicate" in n["text"].lower() for n in notes)


def test_flags_single_value_column():
    df = pd.DataFrame({"user_id": ["u1", "u2"], "country": ["US", "US"]})
    notes = assess_quality(df)
    assert any("single value" in n["text"].lower() for n in notes)


def test_empty_df_and_never_raises():
    assert assess_quality(pd.DataFrame())[0]["level"] == "warning"
    assert isinstance(assess_quality(None), list)
