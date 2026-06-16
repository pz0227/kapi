"""
Trend Spotter — WoW/MoM changes, significant shifts, product trends.
Expects a DataFrame with canonical columns from apply_mapping().
"""
import pandas as pd
import numpy as np
from .waste_detector import detect_waste
from .opportunity_detector import detect_opportunities


def analyze_trends(df: pd.DataFrame) -> dict:
    """
    Required columns: revenue, date.
    Optional: order_id, product_name, customer_id.
    """
    metrics = []
    findings = []
    problems = []

    df_dated = df.dropna(subset=["date"]).copy()
    if df_dated.empty:
        return {"metrics": [], "findings": [], "problems": []}

    date_range = (df_dated["date"].max() - df_dated["date"].min()).days

    # Auto-select time bucket
    if date_range < 28:
        bucket = "D"
        bucket_label = "daily"
    elif date_range < 84:
        bucket = "W"
        bucket_label = "weekly"
    else:
        bucket = "ME"
        bucket_label = "monthly"

    # ── Time-series aggregation ─────────────────────────────────────────────
    ts = df_dated.set_index("date").resample(bucket).agg(
        revenue=("revenue", "sum"),
        orders=("revenue", "count"),
    ).reset_index()
    ts = ts[ts["revenue"] > 0]  # drop empty buckets

    if len(ts) < 2:
        return {"metrics": [], "findings": [{"text": "Not enough time periods for trend analysis", "category": "data", "severity": "neutral"}], "problems": []}

    ts["aov"] = (ts["revenue"] / ts["orders"]).round(2)

    # Unique customers per period
    has_customer = "customer_id" in df_dated.columns
    if has_customer:
        cust_ts = df_dated.set_index("date").resample(bucket)["customer_id"].nunique().reset_index()
        cust_ts.columns = ["date", "unique_customers"]
        ts = ts.merge(cust_ts, on="date", how="left")

    # ── WoW / period-over-period changes ────────────────────────────────────
    wow_revenue = _pct_change_last(ts, "revenue")
    wow_orders = _pct_change_last(ts, "orders")
    wow_aov = _pct_change_last(ts, "aov")

    # MoM: last 4 periods vs previous 4
    mom_revenue = _multi_period_change(ts, "revenue", 4)

    # Overall trend direction (slope)
    slope_revenue = _trend_slope(ts, "revenue")
    slope_orders = _trend_slope(ts, "orders")

    # ── Metrics ─────────────────────────────────────────────────────────────
    metrics.append({
        "label": f"Revenue {_period_label(bucket_label)}",
        "value": wow_revenue if wow_revenue is not None else 0.0,
        "unit": "%",
        "delta": None,
        "delta_label": "",
        "severity": _severity(wow_revenue),
    })
    if mom_revenue is not None:
        metrics.append({
            "label": "Revenue MoM",
            "value": mom_revenue,
            "unit": "%",
            "delta": None,
            "delta_label": "",
            "severity": _severity(mom_revenue),
        })
    metrics.append({
        "label": f"Orders {_period_label(bucket_label)}",
        "value": wow_orders if wow_orders is not None else 0.0,
        "unit": "%",
        "delta": None,
        "delta_label": "",
        "severity": _severity(wow_orders),
    })
    metrics.append({
        "label": f"AOV {_period_label(bucket_label)}",
        "value": wow_aov if wow_aov is not None else 0.0,
        "unit": "%",
        "delta": None,
        "delta_label": "",
        "severity": _severity(wow_aov),
    })

    # ── Findings ────────────────────────────────────────────────────────────
    if slope_revenue is not None:
        if slope_revenue > 0:
            findings.append({
                "text": f"Revenue is on an upward trend ({bucket_label})",
                "category": "trend",
                "severity": "positive",
            })
        elif slope_revenue < 0:
            findings.append({
                "text": f"Revenue is on a downward trend ({bucket_label})",
                "category": "trend",
                "severity": "neutral",
            })

    if wow_revenue is not None and wow_orders is not None:
        if wow_revenue > 0 and wow_orders < 0:
            findings.append({
                "text": f"Order volume down ({wow_orders:+.1f}%) but compensated by higher AOV ({wow_aov:+.1f}%)",
                "category": "trend",
                "severity": "neutral",
            })
        elif mom_revenue is not None and mom_revenue > 10:
            findings.append({
                "text": f"Strong month-over-month revenue growth: {mom_revenue:+.1f}%",
                "category": "trend",
                "severity": "positive",
            })

    # Per-product trends
    has_products = "product_name" in df_dated.columns
    if has_products and len(ts) >= 2:
        midpoint = df_dated["date"].min() + (df_dated["date"].max() - df_dated["date"].min()) / 2
        first_half = df_dated[df_dated["date"] <= midpoint].groupby("product_name")["revenue"].sum()
        second_half = df_dated[df_dated["date"] > midpoint].groupby("product_name")["revenue"].sum()
        growth = pd.DataFrame({"first": first_half, "second": second_half}).fillna(0)
        growth["growth_pct"] = np.where(
            growth["first"] > 0,
            ((growth["second"] - growth["first"]) / growth["first"] * 100).round(1),
            np.where(growth["second"] > 0, 100.0, 0.0),
        )

        top_growing = growth.nlargest(3, "growth_pct")
        for name, row in top_growing.iterrows():
            if row["growth_pct"] > 5:
                findings.append({
                    "text": f"Growing: {name} ({row['growth_pct']:+.1f}%)",
                    "category": "product_trend",
                    "severity": "positive",
                })

        top_declining = growth.nsmallest(3, "growth_pct")
        for name, row in top_declining.iterrows():
            if row["growth_pct"] < -5:
                findings.append({
                    "text": f"Declining: {name} ({row['growth_pct']:+.1f}%)",
                    "category": "product_trend",
                    "severity": "neutral",
                })

    # ── Problems ────────────────────────────────────────────────────────────
    if wow_revenue is not None and wow_revenue < -10:
        problems.append({
            "text": f"Revenue dropped {abs(wow_revenue):.1f}% — significant decline",
            "severity": "high",
            "metric_name": "revenue_trend",
            "current_value": wow_revenue,
            "threshold": -10.0,
        })
    elif wow_revenue is not None and wow_revenue < -5:
        problems.append({
            "text": f"Revenue declining {abs(wow_revenue):.1f}% — monitor closely",
            "severity": "low",
            "metric_name": "revenue_trend",
            "current_value": wow_revenue,
            "threshold": -5.0,
        })

    if wow_orders is not None and wow_orders < -10:
        problems.append({
            "text": f"Order count dropped {abs(wow_orders):.1f}%",
            "severity": "medium",
            "metric_name": "order_trend",
            "current_value": wow_orders,
            "threshold": -10.0,
        })

    # Trend sparkline data
    trend_data = [
        {"date": str(r["date"].date()) if hasattr(r["date"], "date") else str(r["date"]), "value": round(r["revenue"], 2)}
        for _, r in ts.iterrows()
    ]

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


def _pct_change_last(ts: pd.DataFrame, col: str):
    """% change between last two periods."""
    if len(ts) < 2:
        return None
    prev, curr = float(ts[col].iloc[-2]), float(ts[col].iloc[-1])
    if prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def _multi_period_change(ts: pd.DataFrame, col: str, n: int):
    """Compare last N periods vs previous N periods."""
    if len(ts) < n * 2:
        return None
    recent = ts[col].iloc[-n:].sum()
    prior = ts[col].iloc[-n * 2 : -n].sum()
    if prior == 0:
        return None
    return round((recent - prior) / prior * 100, 1)


def _trend_slope(ts: pd.DataFrame, col: str):
    """Linear slope over last 8 periods (or all if fewer). Returns normalized % per period."""
    data = ts[col].tail(8).values
    if len(data) < 3:
        return None
    x = np.arange(len(data), dtype=float)
    coeffs = np.polyfit(x, data, 1)
    return float(coeffs[0])


def _severity(delta):
    if delta is None:
        return "neutral"
    if delta >= 5:
        return "good"
    if delta <= -10:
        return "danger"
    if delta < 0:
        return "warning"
    return "neutral"


def _period_label(bucket_label: str) -> str:
    if bucket_label == "daily":
        return "DoD"
    if bucket_label == "weekly":
        return "WoW"
    return "MoM"
