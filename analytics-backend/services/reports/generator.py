"""
Structured report / deliverable generator.
Uses the active provider to produce PM-quality output documents.
"""
from __future__ import annotations

from datetime import date
from services.providers.base import BaseProvider, Message

REPORT_PROMPTS: dict[str, str] = {
    "weekly_review": """You are a senior product analyst. Generate a **Weekly Product Review** report.

Structure:
## Weekly Product Review — {date}

### Executive Snapshot
- 3–5 bullet points on the most important metrics and movements this week.

### KPI Performance
Cover MAU, DAU, retention, and key funnel metrics. Highlight changes vs prior week.

### Funnel Insights
Summarize funnel performance, biggest drop-off point, and reasons/hypotheses.

### Retention
Comment on cohort retention trends. Are new cohorts healthier or weaker?

### Feature Adoption
Which features gained/lost traction? Any surprises?

### Anomalies & Risks
Flag any metric anomalies or early warning signals.

### Recommended Actions (Top 3)
Specific, prioritized recommendations for the product team.

Use precise numbers from the context. Be direct and analytical.""",

    "exec_brief": """You are a Chief Product Officer preparing a brief for the board.

Generate a concise **Executive KPI Brief** (max 400 words):

## Executive KPI Brief — {date}

### Performance Summary (3 bullets, numbers only)
### Growth Signal (1 sentence)
### Retention Health (1–2 sentences)
### Top Risk (1 sentence)
### Recommended Focus (1 action item)

No fluff. Data-first. Board-ready.""",

    "prd": """You are a Principal Product Manager. Using the product analytics data provided, generate a **PRD-Style Opportunity Summary**.

## Opportunity Summary

### Problem Statement
Based on the data, what specific user problem or friction point is most significant?

### Data Evidence
Cite specific metrics, funnel drops, retention signals, or feature adoption gaps.

### Opportunity Sizing
Estimate the scope: how many users are affected? What is the potential improvement?

### Proposed Solution Direction
High-level product directions (2–3 options). No implementation details yet.

### Success Metrics
What KPIs would move if this is solved? By how much?

### Open Questions
What do we need to learn before committing?

Be specific. Reference actual numbers from the context.""",

    "experiment": """You are an Experimentation Lead. Based on the analytics data, generate an **Experiment Proposal**.

## Experiment Proposal

### Hypothesis
"We believe that [change] will result in [outcome] because [data-backed rationale]."

### Background
What metric or user behavior motivates this experiment?

### Design
- **Control**: Current experience
- **Treatment(s)**: Proposed change(s)
- **Targeting**: Which user segment? (use data to justify)
- **Traffic split**: Suggested % allocation
- **Duration**: Estimated run time and why

### Primary Metric
Which single metric determines success or failure?

### Guardrail Metrics
What must not regress?

### Minimum Detectable Effect
What % improvement would make this worth shipping?

### Risks & Mitigations
What could go wrong?

Ground every decision in the data provided.""",

    "feature_rec": """You are a Product Manager writing a **Feature Recommendation Memo**.

## Feature Recommendation Memo

### Recommendation
What specific feature or improvement do you recommend and why?

### Data Support
Cite the analytics evidence that motivates this recommendation.

### User Impact
Which segment benefits most? How many users?

### Effort Estimate
Rough complexity: Low / Medium / High. Justify.

### Expected Outcome
What metrics improve and by how much?

### Alternatives Considered
What other options were considered and why were they deprioritized?

### Next Steps
Concrete 3-step action plan.

Be direct. Reference specific numbers.""",

    "feedback_synthesis": """You are a UX Researcher synthesizing qualitative and quantitative product feedback.

## User Feedback Synthesis

### Overview
How many feedback items/signals were analyzed? What time period?

### Top Themes (ranked by frequency/severity)
For each theme:
- **Theme name**
- Supporting evidence from data
- Affected user segment
- Severity: Critical / High / Medium / Low

### Sentiment Trends
Is overall sentiment improving or declining?

### Unmet Needs
What do users want that we don't currently offer?

### Recommended Product Response
Top 3 product actions derived from this feedback.

### Verbatim Highlights
If feedback text is present, quote 2–3 representative user voices.

Ground all claims in the data provided.""",
}


async def generate_report(
    report_type: str,
    provider: BaseProvider,
    context: str,
    extra_context: str = "",
) -> str:
    """
    Generate a structured report using the given provider.

    Args:
        report_type: One of the keys in REPORT_PROMPTS
        provider: Initialized BaseProvider instance
        context: Retrieved/computed analytics context
        extra_context: Additional user-provided context

    Returns the generated report as a markdown string.
    """
    template = REPORT_PROMPTS.get(report_type)
    if not template:
        raise ValueError(f"Unknown report type: {report_type}")

    system = template.format(date=date.today().isoformat())
    # Human-friendly output: the UI renders report text as plain text, so
    # markdown markup shows up as literal characters. Ask for clean prose with
    # plain headings and simple lists instead.
    system += (
        "\n\nFORMATTING: Write the report in clean, readable plain text. Put each "
        "section heading on its own line in plain words (e.g. 'Hypothesis' — NOT "
        "'## Hypothesis' or '**Hypothesis**'). Use short paragraphs and simple "
        "'- ' or '1.' lists. Do NOT use any markdown markup symbols (**, *, #, "
        "backticks) — they render as literal characters and hurt readability."
    )

    user_message = "Based on the following product analytics data, generate the requested report.\n\n"
    if context:
        user_message += f"{context}\n\n"
    if extra_context:
        user_message += f"Additional context from user:\n{extra_context}\n\n"
    user_message += "Generate the report now."

    result = await provider.complete(
        messages=[Message(role="user", content=user_message)],
        system=system,
        max_tokens=3000,
        temperature=0.2,
    )
    return result.text


REPORT_DISPLAY_NAMES = {
    "weekly_review": "Weekly Product Review",
    "exec_brief": "Executive KPI Brief",
    "prd": "PRD Opportunity Summary",
    "experiment": "Experiment Proposal",
    "feature_rec": "Feature Recommendation Memo",
    "feedback_synthesis": "User Feedback Synthesis",
}
