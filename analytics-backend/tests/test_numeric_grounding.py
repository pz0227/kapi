"""Tests for numeric groundedness — the wrong-number detector."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.rag.numeric_grounding import extract_numbers, numeric_groundedness


# ── extraction ───────────────────────────────────────────────────────────────

def test_extracts_currency_percent_scale_and_plain():
    vals = {round(n.value, 2) for n in extract_numbers(
        "Revenue was $1.2M, up 23% from 1,234 orders at $28.55 each."
    )}
    assert 1_200_000.0 in vals
    assert 23.0 in vals
    assert 1234.0 in vals
    assert 28.55 in vals


def test_ignores_numbers_inside_identifiers():
    # Underscored ids like u_00500 must not be read as the number 500.
    nums = extract_numbers("user u_00500 and session s3")
    assert all(n.value not in (500.0, 3.0) for n in nums) or nums == []


# ── precision-aware matching (the hard part) ─────────────────────────────────

def test_rounded_percent_is_grounded():
    # Answer rounds 52.2% to 52% — faithful, must count as grounded.
    r = numeric_groundedness("Completion was about 52%.", "before: 52.2% completion")
    assert r["score"] == 1.0 and r["ungrounded"] == []


def test_rounded_millions_is_grounded():
    # "$1.2M" faithfully rounds 1,234,567.
    r = numeric_groundedness("Total revenue is $1.2M.", "sum(total_amount) = 1234567.0")
    assert r["score"] == 1.0


def test_fabricated_number_is_flagged():
    # Real total is 3M; the answer claims 5M — the exact failure we care about.
    r = numeric_groundedness("Total revenue was $5M.", "sum(total_amount) = 3000000")
    assert r["score"] == 0.0
    assert any("5" in u for u in r["ungrounded"])


def test_mixed_grounded_and_fabricated():
    ans = "We had 1,234 orders totaling $5M."   # order count real, total invented
    ground = "row_count = 1234\nsum(total_amount) = 3000000"
    r = numeric_groundedness(ans, ground)
    assert r["total"] == 2 and r["grounded"] == 1
    assert r["score"] == 0.5


def test_no_numbers_returns_na():
    r = numeric_groundedness("Revenue grew across all regions.", "sum = 5")
    assert r["score"] is None and r["total"] == 0


def test_empty_inputs_never_raise():
    assert numeric_groundedness("", "")["score"] is None
    assert numeric_groundedness("total 5", "")["score"] == 0.0
