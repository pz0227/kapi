"""
Product Performance — rank products by composite score, identify stars/dogs.
Expects a DataFrame with canonical columns from apply_mapping().
"""
import pandas as pd
import numpy as np
from .waste_detector import detect_waste
from .opportunity_detector import detect_opportunities


def analyze_product_performance(df: pd.DataFrame) -> dict:
    """
    Required columns: revenue, product_name.
    Optional: quantity, date, cost.
    """
    metrics = []
    findings = []
    problems = []

    # ── Per-product aggregation ─────────────────────────────────────────────
    agg_dict = {"revenue": "sum"}
    if "quantity" in df.columns:
        agg_dict["quantity"] = "sum"

    products = df.groupby("product_name").agg(agg_dict).reset_index()
    products = products.rename(columns={"revenue": "total_revenue"})
    if "quantity" in products.columns:
        products = products.rename(columns={"quantity": "total_quantity"})

    order_counts = df.groupby("product_name").size().reset_index(name="order_count")
    products = products.merge(order_counts, on="product_name")

    n_products = len(products)
    total_revenue = float(products["total_revenue"].sum())

    # ── Growth per product (first-half vs second-half) ──────────────────────
    has_date = "date" in df.columns and df["date"].notna().any()
    if has_date:
        midpoint = df["date"].min() + (df["date"].max() - df["date"].min()) / 2
        first_half = df[df["date"] <= midpoint].groupby("product_name")["revenue"].sum()
        second_half = df[df["date"] > midpoint].groupby("product_name")["revenue"].sum()
        growth = pd.DataFrame({"first": first_half, "second": second_half}).fillna(0)
        growth["growth_pct"] = np.where(
            growth["first"] > 0,
            ((growth["second"] - growth["first"]) / growth["first"] * 100).round(1),
            np.where(growth["second"] > 0, 100.0, 0.0),
        )
        products = products.merge(
            growth["growth_pct"].reset_index(),
            on="product_name",
            how="left",
        )
        products["growth_pct"] = products["growth_pct"].fillna(0.0)
    else:
        products["growth_pct"] = 0.0

    # ── Margin (if cost column exists) ──────────────────────────────────────
    has_cost = "cost" in df.columns and df["cost"].notna().any()
    if has_cost:
        product_cost = df.groupby("product_name")["cost"].sum().reset_index()
        product_cost = product_cost.rename(columns={"cost": "total_cost"})
        products = products.merge(product_cost, on="product_name", how="left")
        products["margin_pct"] = np.where(
            products["total_revenue"] > 0,
            ((products["total_revenue"] - products["total_cost"]) / products["total_revenue"] * 100).round(1),
            0.0,
        )
    else:
        products["margin_pct"] = None

    # ── Composite score ─────────────────────────────────────────────────────
    products["rev_rank"] = products["total_revenue"].rank(pct=True)
    products["growth_rank"] = products["growth_pct"].rank(pct=True)

    if "total_quantity" in products.columns:
        products["qty_rank"] = products["total_quantity"].rank(pct=True)
        if has_cost:
            products["margin_rank"] = products["margin_pct"].rank(pct=True)
            products["score"] = (
                products["rev_rank"] * 0.35
                + products["qty_rank"] * 0.15
                + products["growth_rank"] * 0.35
                + products["margin_rank"] * 0.15
            ).round(3)
        else:
            products["score"] = (
                products["rev_rank"] * 0.40
                + products["qty_rank"] * 0.20
                + products["growth_rank"] * 0.40
            ).round(3)
    else:
        products["score"] = (
            products["rev_rank"] * 0.50
            + products["growth_rank"] * 0.50
        ).round(3)

    products = products.sort_values("score", ascending=False).reset_index(drop=True)

    # ── BCG classification ──────────────────────────────────────────────────
    med_rev = products["total_revenue"].median()
    products["classification"] = products.apply(
        lambda r: _classify(r["total_revenue"], r["growth_pct"], med_rev), axis=1
    )

    # ── Metrics ─────────────────────────────────────────────────────────────
    top_product = products.iloc[0] if n_products > 0 else None
    growing_count = int((products["growth_pct"] > 0).sum()) if has_date else None

    metrics.append({
        "label": "Products Analyzed",
        "value": n_products,
        "unit": "products",
        "delta": None,
        "delta_label": "",
        "severity": "neutral",
    })
    if top_product is not None:
        metrics.append({
            "label": "Top Performer Revenue",
            "value": round(float(top_product["total_revenue"]), 2),
            "unit": "$",
            "delta": None,
            "delta_label": "",
            "severity": "good",
        })
    metrics.append({
        "label": "Avg Revenue/Product",
        "value": round(total_revenue / n_products, 2) if n_products > 0 else 0,
        "unit": "$",
        "delta": None,
        "delta_label": "",
        "severity": "neutral",
    })
    if growing_count is not None:
        metrics.append({
            "label": "Products Growing",
            "value": growing_count,
            "unit": f"of {n_products}",
            "delta": None,
            "delta_label": "",
            "severity": "good" if growing_count > n_products / 2 else "warning",
        })

    # ── Findings ────────────────────────────────────────────────────────────
    if top_product is not None:
        parts = [f"${top_product['total_revenue']:,.2f} revenue"]
        if "total_quantity" in top_product.index:
            parts.append(f"{int(top_product['total_quantity']):,} units")
        if has_date:
            parts.append(f"{top_product['growth_pct']:.1f}% growth")
        findings.append({
            "text": f"{top_product['product_name']} is the top performer: {', '.join(parts)}",
            "category": "star",
            "severity": "positive",
        })

    if growing_count is not None:
        findings.append({
            "text": f"{growing_count} of {n_products} products show positive growth trend",
            "category": "growth",
            "severity": "positive" if growing_count > n_products / 2 else "neutral",
        })

    # Fastest growing
    if has_date and n_products > 1:
        fastest = products.loc[products["growth_pct"].idxmax()]
        if fastest["growth_pct"] > 0:
            findings.append({
                "text": f"{fastest['product_name']} has highest growth rate at {fastest['growth_pct']:.1f}%",
                "category": "rising",
                "severity": "positive",
            })

    # ── Problems ────────────────────────────────────────────────────────────
    if has_date:
        declining = products[products["growth_pct"] < -10]
        for _, row in declining.iterrows():
            problems.append({
                "text": f"{row['product_name']} is declining: {row['growth_pct']:.1f}% growth",
                "severity": "high",
                "metric_name": "product_decline",
                "current_value": float(row["growth_pct"]),
                "threshold": -10.0,
            })

        stale = products[
            (products["growth_pct"] <= 0)
            & (products["total_revenue"] < products["total_revenue"].quantile(0.15))
        ]
        if len(stale) > 0:
            problems.append({
                "text": f"{len(stale)} products have near-zero sales in the recent period",
                "severity": "medium",
                "metric_name": "stale_products",
                "current_value": float(len(stale)),
                "threshold": 0.0,
            })

    if has_cost:
        low_margin = products[products["margin_pct"] < 20]
        for _, row in low_margin.iterrows():
            if row["margin_pct"] is not None:
                problems.append({
                    "text": f"{row['product_name']} has low margin: {row['margin_pct']:.1f}%",
                    "severity": "high" if row["margin_pct"] < 10 else "medium",
                    "metric_name": "low_margin",
                    "current_value": float(row["margin_pct"]),
                    "threshold": 20.0,
                })

    # Product rankings for the frontend
    product_table = products[
        ["product_name", "total_revenue", "order_count", "growth_pct", "score", "classification"]
    ].to_dict("records")

    waste = detect_waste(df)
    opportunities = detect_opportunities(df)

    return {
        "metrics": metrics,
        "findings": findings,
        "problems": problems,
        "product_table": product_table,
        "waste": waste,
        "opportunities": opportunities,
    }


def _classify(revenue: float, growth: float, median_revenue: float) -> str:
    """BCG-style classification."""
    high_rev = revenue >= median_revenue
    if high_rev and growth > 0:
        return "Star"
    if high_rev and growth <= 0:
        return "Cash Cow"
    if not high_rev and growth > 20:
        return "Rising"
    if not high_rev and growth < 0:
        return "Dog"
    return "Niche"
