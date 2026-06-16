"""
Shop Health Check — revenue, orders, AOV, growth, top products, diagnosis.
Expects a DataFrame with canonical columns from apply_mapping().
"""
import pandas as pd
from .waste_detector import detect_waste
from .opportunity_detector import detect_opportunities


def analyze_shop_health(df: pd.DataFrame) -> dict:
    """
    Required columns: revenue, date.
    Optional: order_id, product_name, quantity.
    """
    metrics = []
    findings = []
    problems = []

    total_revenue = float(df["revenue"].sum())
    has_order_id = "order_id" in df.columns
    total_orders = int(df["order_id"].nunique()) if has_order_id else len(df)
    aov = round(total_revenue / total_orders, 2) if total_orders > 0 else 0.0

    # ── Time-series bucketing ───────────────────────────────────────────────
    df_dated = df.dropna(subset=["date"]).copy()
    df_dated["week"] = df_dated["date"].dt.isocalendar().week.astype(int)
    df_dated["year_week"] = (
        df_dated["date"].dt.isocalendar().year.astype(str)
        + "-W"
        + df_dated["week"].astype(str).str.zfill(2)
    )

    weekly = (
        df_dated.groupby("year_week")
        .agg(
            revenue=("revenue", "sum"),
            orders=("revenue", "count"),
            min_date=("date", "min"),
        )
        .sort_values("min_date")
        .reset_index()
    )

    # WoW growth
    wow_revenue = _pct_change(weekly, "revenue")
    wow_orders = _pct_change(weekly, "orders")

    # MoM growth (last 4 weeks vs previous 4 weeks)
    mom_revenue = _mom_change(weekly, "revenue")

    # AOV weekly
    weekly["aov"] = (weekly["revenue"] / weekly["orders"]).round(2)
    wow_aov = _pct_change(weekly, "aov")

    # Sparkline data
    trend_data = [
        {"date": str(r["min_date"].date()), "value": round(r["revenue"], 2)}
        for _, r in weekly.iterrows()
    ]

    # ── Metrics ─────────────────────────────────────────────────────────────
    metrics.append({
        "label": "Total Revenue",
        "value": round(total_revenue, 2),
        "unit": "$",
        "delta": wow_revenue,
        "delta_label": "vs prev week",
        "severity": _severity_from_delta(wow_revenue),
    })
    metrics.append({
        "label": "Total Orders",
        "value": total_orders,
        "unit": "orders",
        "delta": wow_orders,
        "delta_label": "vs prev week",
        "severity": _severity_from_delta(wow_orders),
    })
    metrics.append({
        "label": "Avg Order Value",
        "value": aov,
        "unit": "$",
        "delta": wow_aov,
        "delta_label": "vs prev week",
        "severity": _severity_from_delta(wow_aov),
    })
    metrics.append({
        "label": "Weekly Growth",
        "value": wow_revenue if wow_revenue is not None else 0.0,
        "unit": "%",
        "delta": mom_revenue,
        "delta_label": "MoM trend",
        "severity": _severity_from_delta(wow_revenue),
    })

    # ── Findings ────────────────────────────────────────────────────────────
    if wow_revenue is not None:
        direction = "grew" if wow_revenue >= 0 else "declined"
        findings.append({
            "text": f"Revenue {direction} {abs(wow_revenue):.1f}% week-over-week to ${total_revenue:,.2f}",
            "category": "growth",
            "severity": "positive" if wow_revenue >= 0 else "neutral",
        })

    if wow_aov is not None:
        direction = "increased" if wow_aov >= 0 else "decreased"
        findings.append({
            "text": f"AOV {direction} {abs(wow_aov):.1f}% to ${aov:,.2f}",
            "category": "efficiency",
            "severity": "positive" if wow_aov >= 0 else "neutral",
        })

    # Units sold
    if "quantity" in df.columns:
        total_units = int(df["quantity"].sum())
        findings.append({
            "text": f"Total units sold: {total_units:,}",
            "category": "volume",
            "severity": "neutral",
        })

    # Top products
    has_products = "product_name" in df.columns
    if has_products:
        product_rev = (
            df.groupby("product_name")["revenue"]
            .sum()
            .sort_values(ascending=False)
        )
        top_product = product_rev.index[0]
        top_rev = float(product_rev.iloc[0])
        top_pct = round(top_rev / total_revenue * 100, 1) if total_revenue > 0 else 0
        findings.append({
            "text": f"Top product: {top_product} (${top_rev:,.2f} — {top_pct}% of revenue)",
            "category": "products",
            "severity": "neutral",
        })

        # Concentration risk
        top3_rev = float(product_rev.head(3).sum())
        top3_pct = round(top3_rev / total_revenue * 100, 1) if total_revenue > 0 else 0
        if top3_pct > 60:
            problems.append({
                "text": f"Revenue concentration risk: top 3 products account for {top3_pct}% of total revenue",
                "severity": "medium",
                "metric_name": "concentration",
                "current_value": top3_pct,
                "threshold": 60.0,
            })

    # ── Problems ────────────────────────────────────────────────────────────
    if wow_revenue is not None and wow_revenue < -5:
        problems.append({
            "text": f"Revenue declined {abs(wow_revenue):.1f}% week-over-week",
            "severity": "high",
            "metric_name": "revenue_wow",
            "current_value": wow_revenue,
            "threshold": -5.0,
        })

    if wow_aov is not None and wow_aov < -10:
        problems.append({
            "text": f"AOV dropped {abs(wow_aov):.1f}% week-over-week",
            "severity": "medium",
            "metric_name": "aov_wow",
            "current_value": wow_aov,
            "threshold": -10.0,
        })

    if total_orders < 10:
        problems.append({
            "text": f"Small sample size: only {total_orders} orders in the dataset",
            "severity": "low",
            "metric_name": "sample_size",
            "current_value": float(total_orders),
            "threshold": 10.0,
        })

    waste = detect_waste(df)
    opportunities = detect_opportunities(df)

    return {
        "metrics": metrics,
        "findings": findings,
        "problems": problems,
        "trend_data": trend_data,
        "waste": waste,
        "opportunities": opportunities,
    }


def _pct_change(weekly: pd.DataFrame, col: str):
    """Compute % change between last two periods. Returns float or None."""
    if len(weekly) < 2:
        return None
    prev = weekly[col].iloc[-2]
    curr = weekly[col].iloc[-1]
    if prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def _mom_change(weekly: pd.DataFrame, col: str):
    """Compare last 4 weeks vs previous 4 weeks."""
    if len(weekly) < 8:
        return None
    recent = weekly[col].iloc[-4:].sum()
    prior = weekly[col].iloc[-8:-4].sum()
    if prior == 0:
        return None
    return round((recent - prior) / prior * 100, 1)


def _severity_from_delta(delta):
    """Map a delta percentage to a severity label."""
    if delta is None:
        return "neutral"
    if delta >= 5:
        return "good"
    if delta <= -5:
        return "danger"
    if delta < 0:
        return "warning"
    return "neutral"
