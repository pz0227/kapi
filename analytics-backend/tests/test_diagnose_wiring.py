"""Wiring guard: the diagnosis layer must stay reachable by users.

diagnose.py existed for a while as a fully-tested module that NO route imported,
i.e. dead code from the user's perspective. These tests pin the two integration
points (dashboard summary + report LLM context) so a refactor can't silently
disconnect the feature again.
"""
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "api" / "routes"


def test_summary_endpoint_returns_findings():
    src = (ROUTES / "analytics.py").read_text()
    assert "from services.analytics.diagnose import diagnose" in src
    assert '"findings": diagnose(' in src, "dashboard /summary must include diagnosis findings"


def test_report_context_includes_findings_markdown():
    src = (ROUTES / "reports.py").read_text()
    assert "findings_to_markdown(diagnose(" in src, (
        "report LLM context must include the deterministic findings block"
    )
