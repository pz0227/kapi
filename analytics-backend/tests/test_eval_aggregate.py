"""
Tests for eval result aggregation — the pure function that turns per-case
results into the three-axis scores and fault distribution the resume cites
(competence, honesty, retriever-vs-model attribution).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.eval.runner import aggregate


def _case(cid, category, primary_pass, mode=None, passed=None, tag=None,
          fault="none", lexical=0.5, provider_error=False):
    return {
        "case_id": cid, "category": category, "primary_pass": primary_pass,
        "correctness": {"mode": mode, "passed": passed},
        "failure": {"tag": tag, "fault": fault},
        "lexical_support": lexical, "provider_error": provider_error,
    }


def test_competence_and_honesty_axes_are_separated():
    results = [
        _case("a1", "answerable", True, mode="number", passed=True),
        _case("a2", "answerable", False, mode="number", passed=False, tag="hallucinated_fact", fault="model"),
        _case("u1", "unanswerable", True),                 # correctly refused
        _case("u2", "adversarial", False, tag="missed_refusal", fault="model"),
    ]
    agg = aggregate(results, {})
    assert agg["axes"]["competence_answerable"] == 0.5   # 1 of 2 answerable passed
    assert agg["axes"]["honesty_refusal"] == 0.5          # 1 of 2 should-refuse passed
    assert agg["n_cases"] == 4


def test_fault_distribution_attributes_model_vs_retriever():
    results = [
        _case("1", "answerable", False, tag="retrieval_miss", fault="retriever"),
        _case("2", "answerable", False, tag="hallucinated_fact", fault="model"),
        _case("3", "answerable", False, tag="hallucinated_fact", fault="model"),
        _case("4", "answerable", True),  # fault "none" excluded
    ]
    agg = aggregate(results, {})
    assert agg["fault_distribution"] == {"retriever": 1, "model": 2}
    assert agg["failure_distribution"]["hallucinated_fact"] == 2


def test_provider_errors_surfaced_and_excluded_from_rates():
    results = [
        _case("ok", "answerable", True),
        _case("err", "answerable", None, provider_error=True),
    ]
    agg = aggregate(results, {})
    assert "err" in agg["provider_errors"]


def test_empty_results_safe():
    agg = aggregate([], {})
    assert agg["n_cases"] == 0
    assert agg["axes"]["competence_answerable"] is None
