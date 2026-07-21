"""
End-to-end integration test: real sample data flows through the whole analytics
chain (normalize -> KPIs -> funnel -> retention -> feature adoption -> executive
summary -> diagnosis) without any part breaking on the seams.

Unit tests prove each piece; this proves they connect. It's the test that would
have caught a signature mismatch or a broken hand-off between stages.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from services.analytics.normalize import normalize_event_columns
from services.analytics import (
    compute_kpis, compute_funnel, compute_retention, auto_detect_funnel,
    compute_feature_adoption, compute_executive_summary,
)
from services.analytics.diagnose import diagnose
from services.analytics.quality import assess_quality

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


@pytest.fixture
def events_df():
    df = pd.read_csv(SAMPLES / "events.csv")
    return normalize_event_columns(df)


def test_events_sample_has_canonical_columns(events_df):
    assert {"timestamp", "user_id", "event_name"} <= set(events_df.columns)


def test_full_chain_runs_and_summary_is_nonempty(events_df):
    kpis = compute_kpis(events_df)
    funnel = compute_funnel(events_df, auto_detect_funnel(events_df))
    retention = compute_retention(events_df)
    features = compute_feature_adoption(events_df)
    summary = compute_executive_summary(kpis, funnel, retention, features)

    assert isinstance(kpis, list) and len(kpis) >= 1
    assert isinstance(summary, str) and len(summary) > 50
    # The summary must actually mention a KPI it was given.
    assert any(k["label"].split()[0] in summary for k in kpis)


def test_diagnosis_consumes_engine_output_without_error(events_df):
    kpis = compute_kpis(events_df)
    funnel = compute_funnel(events_df, auto_detect_funnel(events_df))
    retention = compute_retention(events_df)
    findings = diagnose(kpis, funnel, retention)
    # Whatever the data says, diagnosis returns a well-formed list.
    assert isinstance(findings, list)
    for f in findings:
        assert set(f) == {"severity", "metric", "reading", "next_step"}
        assert f["severity"] in {"critical", "warning", "info"}


def test_quality_assessment_on_real_sample(events_df):
    notes = assess_quality(events_df)
    # Events sample HAS time + user columns, so it should NOT warn about them.
    text = " ".join(n["text"] for n in notes)
    assert "No time/date column" not in text
    assert "No user/customer id" not in text


def test_orders_sample_flows_through_compute_router():
    # A different real dataset (TikTok Shop orders) exercises the aggregate path.
    from services.analytics.aggregate_router import try_compute_answer
    f = SAMPLES / "tiktok_shop_orders.csv"
    block = try_compute_answer("what is the total revenue?", str(f), f.name)
    # Either it computes (block present) or cleanly declines (None) — never raises.
    assert block is None or "COMPUTED FROM FULL DATASET" in block
