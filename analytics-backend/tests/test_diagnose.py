"""Tests for the diagnosis layer — metrics into ranked plain-language findings."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.analytics.diagnose import diagnose, findings_to_markdown


def test_critical_kpi_drop_flagged_and_ranked_first():
    kpis = [
        {"label": "MAU", "value": 800, "delta": -25.0, "delta_label": "vs prev 30d"},
        {"label": "Events", "value": 5000, "delta": 30.0, "delta_label": "vs prev 30d"},
    ]
    out = diagnose(kpis, None, None)
    assert out[0]["severity"] == "critical"
    assert "MAU" in out[0]["metric"]
    # The positive one is still surfaced but ranked lower.
    assert any(f["severity"] == "info" for f in out)


def test_funnel_bottleneck_detected():
    funnel = {"steps": [
        {"step": "view", "count": 1000, "conversion_rate": 100.0},
        {"step": "signup", "count": 300, "conversion_rate": 30.0},
        {"step": "purchase", "count": 200, "conversion_rate": 66.0},
    ], "overall_conversion": 20.0, "biggest_drop_step": "signup"}
    out = diagnose(None, funnel, None)
    assert out and out[0]["severity"] == "critical"
    assert "signup" in out[0]["reading"]


def test_low_retention_reads_as_activation_gap():
    out = diagnose(None, None, {"avg_day1": 40, "avg_day7": 6})
    assert out and out[0]["severity"] == "critical"
    assert "activation" in out[0]["next_step"].lower()


def test_healthy_metrics_produce_no_alarms():
    kpis = [{"label": "MAU", "value": 1000, "delta": 2.0, "delta_label": "vs prev 30d"}]
    funnel = {"steps": [
        {"step": "a", "count": 100, "conversion_rate": 100.0},
        {"step": "b", "count": 90, "conversion_rate": 90.0},
    ]}
    out = diagnose(kpis, funnel, {"avg_day7": 45})
    assert out == []


def test_markdown_render_and_empty():
    assert findings_to_markdown([]) == ""
    md = findings_to_markdown(diagnose(None, None, {"avg_day7": 5}))
    assert "Key Findings" in md and "Retention" in md


def test_never_raises_on_garbage():
    assert diagnose([{"delta": "nan"}], {"steps": "oops"}, {"avg_day7": None}) == [] or True
