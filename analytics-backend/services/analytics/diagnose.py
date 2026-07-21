"""
Diagnosis layer: turn computed metrics into prioritized, plain-language findings.

Numbers alone are not insight. "Day-7 retention: 12%" is a fact; "Day-7
retention at 12% points to an activation gap, users try the product once and
don't come back" is something a PM can act on. This module reads the outputs of
the analytics engine (KPI cards, funnel, retention) and emits ranked findings
with a severity, a reading, and a suggested next question.

Design:
- Deterministic and rule-based. No LLM, so it is fast, free, and testable, and
  it never fabricates a number (the exact failure this product warns about).
  The LLM analyst still writes the prose; this gives it grounded findings to
  build on and can be surfaced directly in a dashboard.
- Thresholds are explicit and conservative, tuned to avoid crying wolf.
- Never raises: a diagnosis bug must never take down an analytics response.
"""
from __future__ import annotations

# Severity ranks for ordering (higher = surfaced first).
_SEV = {"critical": 3, "warning": 2, "info": 1}

# Thresholds, named so they're easy to tune and reason about.
_KPI_DROP_CRITICAL = -20.0   # % change that reads as a serious decline
_KPI_DROP_WARNING = -8.0
_KPI_RISE_NOTABLE = 20.0     # celebrate real growth too
_FUNNEL_STEP_CRITICAL = 40.0  # a step converting below this is a bottleneck
_FUNNEL_STEP_WARNING = 60.0
_RETENTION_D7_CRITICAL = 10.0  # % — below this is an activation problem
_RETENTION_D7_WARNING = 20.0


def _finding(severity: str, metric: str, reading: str, next_step: str) -> dict:
    return {"severity": severity, "metric": metric, "reading": reading, "next_step": next_step}


def diagnose(kpis: list[dict] | None,
             funnel: dict | None,
             retention: dict | None) -> list[dict]:
    """Return findings ranked most-severe first. Empty list if nothing stands
    out. Never raises."""
    findings: list[dict] = []
    try:
        # ── KPI deltas ────────────────────────────────────────────────────
        for k in kpis or []:
            delta = k.get("delta")
            if delta is None:
                continue
            label = k.get("label", "A metric")
            ctx = k.get("delta_label", "")
            if delta <= _KPI_DROP_CRITICAL:
                findings.append(_finding(
                    "critical", label,
                    f"{label} fell {delta:.0f}% {ctx}, a steep decline.",
                    "Segment by acquisition source and cohort to see whether this is fewer new users or higher churn.",
                ))
            elif delta <= _KPI_DROP_WARNING:
                findings.append(_finding(
                    "warning", label,
                    f"{label} is down {delta:.0f}% {ctx}.",
                    "Check whether the dip is broad or concentrated in one segment or platform.",
                ))
            elif delta >= _KPI_RISE_NOTABLE:
                findings.append(_finding(
                    "info", label,
                    f"{label} grew {delta:.0f}% {ctx}.",
                    "Identify what drove the lift so it can be repeated deliberately.",
                ))

        # ── Funnel bottleneck ─────────────────────────────────────────────
        if funnel and funnel.get("steps"):
            # Find the worst step-over-step conversion (ignore the first step,
            # which has no prior step to convert from).
            worst = None
            for s in funnel["steps"][1:]:
                cr = s.get("conversion_rate")
                if cr is None:
                    continue
                if worst is None or cr < worst.get("conversion_rate", 101):
                    worst = s
            if worst is not None:
                cr = worst["conversion_rate"]
                step = worst.get("step", "a step")
                if cr < _FUNNEL_STEP_CRITICAL:
                    findings.append(_finding(
                        "critical", "Funnel",
                        f"Only {cr:.0f}% of users get past '{step}', the funnel's biggest bottleneck.",
                        f"Investigate friction at '{step}': is it a UX blocker, a performance issue, or a mismatch of intent?",
                    ))
                elif cr < _FUNNEL_STEP_WARNING:
                    findings.append(_finding(
                        "warning", "Funnel",
                        f"'{step}' converts at {cr:.0f}%, the weakest step in the funnel.",
                        f"A/B test a simplification of '{step}' and measure step conversion.",
                    ))

        # ── Retention / activation ────────────────────────────────────────
        if retention:
            d7 = retention.get("avg_day7")
            if isinstance(d7, (int, float)):
                if d7 < _RETENTION_D7_CRITICAL:
                    findings.append(_finding(
                        "critical", "Retention",
                        f"Day-7 retention is {d7:.0f}%, users try the product once and rarely return.",
                        "This is an activation problem. Define the 'aha moment' and get users to it faster in onboarding.",
                    ))
                elif d7 < _RETENTION_D7_WARNING:
                    findings.append(_finding(
                        "warning", "Retention",
                        f"Day-7 retention is {d7:.0f}%, on the low side.",
                        "Compare retained vs churned users' first-session behavior to find the habit-forming action.",
                    ))

        findings.sort(key=lambda f: _SEV.get(f["severity"], 0), reverse=True)
        return findings
    except Exception:
        return []


def findings_to_markdown(findings: list[dict]) -> str:
    """Render findings as a compact Markdown block for the analyst context or a
    dashboard. Empty string if there are none."""
    if not findings:
        return ""
    icon = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
    lines = ["### Key Findings (auto-diagnosed, ranked)"]
    for f in findings:
        lines.append(f"- {icon.get(f['severity'], '')} **{f['metric']}**: {f['reading']} "
                     f"_Next:_ {f['next_step']}")
    return "\n".join(lines)
