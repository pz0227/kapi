"""
Tests for the eval scoring functions — the logic behind the resume's headline
numbers (Cohen's kappa, correctness, refusal). If cohens_kappa were wrong, the
project's "kappa = 0.56" claim would be too, so it's verified against a
hand-computed textbook value.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.eval.calibration import cohens_kappa
from services.eval.metrics import answer_correctness, refusal


# ── Cohen's kappa (resume-critical) ──────────────────────────────────────────

def test_kappa_textbook_value():
    # 2x2: both-1=20, A1B0=5, both-0=15, A0B1=10 (n=50).
    # po=0.70, pe=0.50 -> kappa = (0.70-0.50)/(1-0.50) = 0.40. Hand-computed.
    a = [1]*20 + [1]*5 + [0]*15 + [0]*10
    b = [1]*20 + [0]*5 + [0]*15 + [1]*10
    assert cohens_kappa(a, b) == 0.4


def test_kappa_perfect_agreement():
    assert cohens_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_kappa_single_class_both_raters():
    # Both label everything 1: chance agreement is total, kappa undefined ->
    # convention here: 1.0 if they fully agree.
    assert cohens_kappa([1, 1, 1], [1, 1, 1]) == 1.0


def test_kappa_guards_bad_input():
    assert cohens_kappa([], []) is None
    assert cohens_kappa([1, 0], [1]) is None


# ── answer correctness ───────────────────────────────────────────────────────

def test_correctness_not_applicable_without_gold():
    assert answer_correctness("anything", None)["applicable"] is False


def test_numeric_correctness_exact_match_no_tolerance():
    # Gold with no tolerance requires an exact figure — this is deliberate.
    r = answer_correctness("The total is 1234.5.", {"kind": "number", "value": 1234.5})
    assert r["applicable"] and r["passed"] is True


def test_numeric_correctness_within_gold_tolerance():
    # Real gold cases carry their own tolerance; a faithful rounding passes.
    r = answer_correctness("The total is about 1,235.",
                           {"kind": "number", "value": 1234.5, "tolerance": 1.0})
    assert r["passed"] is True


def test_numeric_correctness_flags_wrong_figure():
    r = answer_correctness("The total is 9,999.",
                           {"kind": "number", "value": 1234.5, "tolerance": 1.0})
    assert r["applicable"] and r["passed"] is False


# ── refusal (honesty axis) ───────────────────────────────────────────────────

def test_refusal_detected_when_expected():
    r = refusal("That column is not in the dataset, so I can't compute it.", expects_refusal=True)
    assert r["refused"] and r["correct"] is True


def test_hallucination_not_misread_as_refusal():
    # A confident fabricated answer should NOT count as a refusal.
    r = refusal("The average age is 34.2 years.", expects_refusal=True)
    assert r["refused"] is False and r["correct"] is False


def test_bare_hedge_is_not_a_refusal():
    # "not sure" is a confidence hedge, not a data-limitation cue.
    r = refusal("I'm not sure, but it's probably around 500.", expects_refusal=True)
    assert r["refused"] is False
