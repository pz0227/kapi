"""
Tests for the compute-first aggregate router (Phase 2 / 2.2).

Run:  cd analytics-backend && .venv/bin/python -m pytest tests/ -v

Covers: intent detection (EN + ZH), exact-value correctness against pandas
ground truth over MORE rows than the RAG index cap (proving full-dataset
coverage), filtered aggregates, group-by, and fail-silent guarantees.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import pytest

from services.analytics.aggregate_router import try_compute_answer, _detect_ops, _match_filters


@pytest.fixture()
def orders_csv(tmp_path):
    """500 rows — deliberately larger than index_max_rows (200)."""
    import random
    random.seed(7)
    rows = []
    for i in range(500):
        rows.append({
            "order_id": f"ord_{i:04d}",
            "amount": round(random.uniform(5, 500), 2),
            "region": random.choice(["US", "EU", "APAC"]),
            "status": random.choice(["paid", "refunded", "pending"]),
        })
    df = pd.DataFrame(rows)
    path = tmp_path / "orders.csv"
    df.to_csv(path, index=False)
    return path, df


# ── Detection ─────────────────────────────────────────────────────────────────

def test_detects_english_aggregates():
    assert "sum" in _detect_ops("what is the total revenue?")
    assert "mean" in _detect_ops("average order amount please")
    assert "count" in _detect_ops("how many orders do we have")
    assert "max" in _detect_ops("which region has the highest amount")


def test_detects_chinese_aggregates():
    assert "sum" in _detect_ops("总共的销售额是多少")
    assert "count" in _detect_ops("一共有多少条订单")
    assert "mean" in _detect_ops("平均金额")


def test_ignores_non_aggregate_questions(orders_csv):
    path, _ = orders_csv
    assert try_compute_answer("tell me about this dataset", str(path), "orders.csv") is None
    assert try_compute_answer("what does the status column mean?", str(path), "orders.csv") is None


# ── Correctness over the FULL dataset ────────────────────────────────────────

def test_sum_matches_pandas_over_all_rows(orders_csv):
    path, df = orders_csv
    block = try_compute_answer("what is the total amount?", str(path), "orders.csv")
    assert block is not None
    expected = round(float(df["amount"].sum()), 4)
    assert str(expected) in block
    assert "all 500 rows" in block  # full dataset, not the 200-row index cap


def test_filtered_aggregate_eu_only(orders_csv):
    path, df = orders_csv
    block = try_compute_answer("what is the total amount in EU?", str(path), "orders.csv")
    assert block is not None
    expected = round(float(df[df["region"] == "EU"]["amount"].sum()), 4)
    assert str(expected) in block
    assert "region = EU" in block


def test_filtered_count_refunded(orders_csv):
    path, df = orders_csv
    block = try_compute_answer("how many refunded orders?", str(path), "orders.csv")
    assert block is not None
    expected = int((df["status"] == "refunded").sum())
    assert f"= {expected} (of 500 total)" in block


def test_groupby_mean_by_region(orders_csv):
    path, df = orders_csv
    block = try_compute_answer("average amount by region", str(path), "orders.csv")
    assert block is not None
    top_region = df.groupby("region")["amount"].mean().idxmax()
    assert top_region in block


# ── Filter matching unit behavior ────────────────────────────────────────────

def test_match_filters_word_boundary(orders_csv):
    _, df = orders_csv
    cat_cols = ["order_id", "region", "status"]
    hits = _match_filters("total amount in EU please", df, cat_cols)
    assert hits.get("region") == ["EU"]
    # 'paid' should NOT match inside 'prepaid'
    none_hits = _match_filters("what about prepaid plans", df, cat_cols)
    assert "status" not in none_hits


# ── Fail-silent guarantees ───────────────────────────────────────────────────

def test_missing_file_returns_none():
    assert try_compute_answer("total amount", "/nonexistent/file.csv", "file.csv") is None


def test_unsupported_extension_returns_none(tmp_path):
    p = tmp_path / "data.parquet"
    p.write_bytes(b"not really parquet")
    assert try_compute_answer("total amount", str(p), "data.parquet") is None


def test_corrupt_csv_returns_none(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_bytes(b"\x00\xff\x00\xff completely broken")
    result = try_compute_answer("total rows count", str(p), "bad.csv")
    assert result is None or isinstance(result, str)  # must not raise, ever


# ── Regression tests: four bugs found by the offline eval (2026-07-15) ───────
# Each of these failed against data/samples before the fix. Kept as living
# documentation of the eval-driven loop: measure, fix, re-measure.

SAMPLES = Path(__file__).parent.parent / "data" / "samples"


def test_column_name_is_not_a_value_filter():
    """'referral source' names a COLUMN; the value 'referral' inside it must
    not become a circular filter (bug: answer was trivially 'referral')."""
    block = try_compute_answer(
        "What is the most common referral source for users?",
        str(SAMPLES / "users.csv"), "users.csv")
    assert block is not None
    assert "most_common(referral_source) = 'organic'" in block


def test_stopword_country_codes_do_not_filter():
    """Country code 'IN' must not match the preposition 'in' (bug: US share
    silently became US+India share)."""
    block = try_compute_answer(
        "What percentage of users are based in the US?",
        str(SAMPLES / "users.csv"), "users.csv")
    assert block is not None
    assert "country = US" in block and "IN" not in block


def test_plural_column_matching():
    """'distinct countries' must reach the 'country' column (bug: singular
    column name never matched the plural word)."""
    block = try_compute_answer(
        "How many distinct countries are represented in the user base?",
        str(SAMPLES / "users.csv"), "users.csv")
    assert block is not None
    assert "distinct_count(country) = 7" in block


def test_value_subtoken_matching():
    """'iOS and Android' must match values 'mobile_ios'/'mobile_android'
    (bug: compound value names never matched their sub-tokens)."""
    block = try_compute_answer(
        "What share of events come from mobile platforms (iOS and Android combined)?",
        str(SAMPLES / "events.csv"), "events.csv")
    assert block is not None
    assert "share where platform in" in block


# ── Honesty guards: the router must know when NOT to speak ───────────────────
# Found by auditing router output on the eval set's unanswerable/adversarial
# cases: an "additive" block is NOT harmless when it answers a different
# question than the one asked.

def test_time_scoped_question_suppressed():
    """No date filtering support → an all-time sum would silently answer a
    different question than 'revenue in Q3 2024'. Must stay silent."""
    assert try_compute_answer(
        "What was the total shop revenue in Q3 2024?",
        str(SAMPLES / "tiktok_shop_orders.csv"), "tiktok_shop_orders.csv") is None


def test_unrelated_count_noun_suppressed():
    """'How many support tickets' against a users table must not answer
    row_count = 500."""
    assert try_compute_answer(
        "How many support tickets do we have?",
        str(SAMPLES / "users.csv"), "users.csv") is None


def test_no_numeric_fallback_on_unknown_column():
    """'Average customer age' with no age column must not fall back to
    mean() of whatever numeric column happens to exist."""
    assert try_compute_answer(
        "What is the average age of our customers?",
        str(SAMPLES / "users.csv"), "users.csv") is None


def test_rank_by_numeric_aggregate():
    """'Second-highest by revenue' ranks categories by summed value, not by
    row frequency."""
    block = try_compute_answer(
        "Which product category is second-highest by revenue?",
        str(SAMPLES / "tiktok_shop_orders.csv"), "tiktok_shop_orders.csv")
    assert block is not None and "second_highest" in block


# ── DataFrame cache behavior ─────────────────────────────────────────────────

def test_df_cache_hit_and_mtime_invalidation(tmp_path):
    """Same unchanged file parses once; touching the file invalidates."""
    import os, time
    from services.analytics import aggregate_router as ar

    p = tmp_path / "cache_test.csv"
    pd.DataFrame({"amount": [1.0, 2.0], "region": ["US", "EU"]}).to_csv(p, index=False)

    df1 = ar._load_df(p, "cache_test.csv")
    df2 = ar._load_df(p, "cache_test.csv")
    assert df1 is df2  # cache hit: identical object

    time.sleep(0.01)
    pd.DataFrame({"amount": [1.0, 2.0, 3.0], "region": ["US", "EU", "US"]}).to_csv(p, index=False)
    os.utime(p)  # ensure mtime moves even on coarse filesystems
    df3 = ar._load_df(p, "cache_test.csv")
    assert df3 is not df1 and len(df3) == 3  # stale entry replaced


def test_oversized_file_skipped(tmp_path, monkeypatch):
    """Files beyond the size ceiling are refused instead of parsed per message."""
    from services.analytics import aggregate_router as ar
    p = tmp_path / "big.csv"
    pd.DataFrame({"amount": [1.0]}).to_csv(p, index=False)
    monkeypatch.setattr(ar, "_MAX_FILE_MB", 0)
    assert ar._load_df(p, "big.csv") is None
    assert try_compute_answer("total amount", str(p), "big.csv") is None
