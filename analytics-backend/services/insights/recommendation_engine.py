"""
Template-based recommendation engine.
Maps detected problems and findings to actionable recommendations.
"""
import re

# (pattern in problem text, min severity) -> recommendation template
PROBLEM_TEMPLATES: list[dict] = [
    {
        "pattern": r"revenue.*declin",
        "severities": ("critical", "high"),
        "text": "Revenue is declining — review traffic sources, ad performance, and conversion rates. Check if a top product is underperforming.",
        "priority": 1,
        "impact": "high",
        "effort": "medium",
        "category": "marketing",
    },
    {
        "pattern": r"concentration",
        "severities": ("critical", "high", "medium"),
        "text": "Diversify product catalog — top products drive the majority of revenue. Promote mid-tier products in ads to reduce risk.",
        "priority": 2,
        "impact": "high",
        "effort": "medium",
        "category": "marketing",
    },
    {
        "pattern": r"declining|is declining",
        "severities": ("critical", "high"),
        "text": "Investigate declining products — listing fatigue or competitive pressure likely. Refresh photos, title, and description.",
        "priority": 1,
        "impact": "high",
        "effort": "low",
        "category": "product",
    },
    {
        "pattern": r"stale|near-zero",
        "severities": ("critical", "high", "medium"),
        "text": "Consider discontinuing stale products to reduce inventory costs and focus ad spend on performers.",
        "priority": 4,
        "impact": "low",
        "effort": "low",
        "category": "operations",
    },
    {
        "pattern": r"aov.*drop",
        "severities": ("critical", "high", "medium"),
        "text": "AOV dropping — test bundle offers or 'frequently bought together' recommendations to boost basket size.",
        "priority": 2,
        "impact": "medium",
        "effort": "low",
        "category": "pricing",
    },
    {
        "pattern": r"order.*count.*drop|order.*declin",
        "severities": ("critical", "high", "medium"),
        "text": "Order volume is falling — review ad targeting and check if traffic sources have shifted. Consider flash sales to re-engage.",
        "priority": 2,
        "impact": "high",
        "effort": "medium",
        "category": "marketing",
    },
    {
        "pattern": r"low margin|margin.*low",
        "severities": ("critical", "high", "medium"),
        "text": "Low margin products detected — renegotiate supplier costs or adjust pricing. Consider if volume justifies the thin margin.",
        "priority": 3,
        "impact": "medium",
        "effort": "medium",
        "category": "pricing",
    },
    {
        "pattern": r"sample size",
        "severities": ("low",),
        "text": "Dataset is small — upload more data for more reliable insights. Trends and recommendations improve with larger datasets.",
        "priority": 5,
        "impact": "low",
        "effort": "low",
        "category": "data",
    },
]

# Positive-finding-based recommendations (triggered when things are going well)
FINDING_TEMPLATES: list[dict] = [
    {
        "pattern": r"aov.*increas|aov.*up",
        "text": "AOV is trending up — test bundle offers to push it further.",
        "priority": 3,
        "impact": "medium",
        "effort": "low",
        "category": "pricing",
    },
    {
        "pattern": r"highest growth|fastest growing|growing.*\d+",
        "text": "A product is growing fast — increase ad spend and ensure inventory can keep up with demand.",
        "priority": 2,
        "impact": "high",
        "effort": "low",
        "category": "marketing",
    },
    {
        "pattern": r"strong.*month|upward trend",
        "text": "Revenue momentum is strong — reinvest in top-performing channels and double down on winning products.",
        "priority": 3,
        "impact": "high",
        "effort": "medium",
        "category": "marketing",
    },
]


def generate_recommendations(
    mode: str,
    metrics: list[dict],
    findings: list[dict],
    problems: list[dict],
    max_recs: int = 5,
) -> list[dict]:
    """Generate template-based recommendations from detected problems and findings."""
    recs: list[dict] = []
    seen_texts: set[str] = set()

    # Match problems to templates
    for problem in problems:
        p_text = problem.get("text", "").lower()
        p_sev = problem.get("severity", "medium")

        for tmpl in PROBLEM_TEMPLATES:
            if p_sev not in tmpl["severities"]:
                continue
            if re.search(tmpl["pattern"], p_text, re.IGNORECASE):
                if tmpl["text"] not in seen_texts:
                    recs.append({
                        "text": tmpl["text"],
                        "priority": tmpl["priority"],
                        "impact": tmpl["impact"],
                        "effort": tmpl["effort"],
                        "category": tmpl["category"],
                    })
                    seen_texts.add(tmpl["text"])
                break

    # Match positive findings to opportunity templates
    for finding in findings:
        f_text = finding.get("text", "").lower()
        if finding.get("severity") != "positive":
            continue

        for tmpl in FINDING_TEMPLATES:
            if re.search(tmpl["pattern"], f_text, re.IGNORECASE):
                if tmpl["text"] not in seen_texts:
                    recs.append({
                        "text": tmpl["text"],
                        "priority": tmpl["priority"],
                        "impact": tmpl["impact"],
                        "effort": tmpl["effort"],
                        "category": tmpl["category"],
                    })
                    seen_texts.add(tmpl["text"])
                break

    # Sort by priority and limit
    recs.sort(key=lambda r: r["priority"])
    return recs[:max_recs]
