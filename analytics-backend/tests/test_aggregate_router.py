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
