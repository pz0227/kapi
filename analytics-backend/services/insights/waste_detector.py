"""
Money Wasted Detector — identifies where the shop is losing money.
Works on canonical DataFrame (output of apply_mapping).
"""
import pandas as pd
import numpy as np


def detect_waste(df: pd.DataFrame) -> dict:
    issues: list = []
    actions: list = []
    estimated_loss = 0.0

    total_revenue = float(df["revenue"].sum())
    has_products = "product_name" in df.columns
    has_date = "date" in df.columns and df["date"].notna().any()

    # 1. Declining products (growth < -10%)
    if has_products and has_date:
        midpoint = df["date"].min() + (df["date"].max() - df["date"].min()) / 2
        first_half = df[df["date"] <= midpoint].groupby("product_name")["revenue"].sum()
        second_half = df[df["date"] > midpoint].groupby("product_name")["revenue"].sum()
        g = pd.DataFrame({"first": first_half, "second": second_half}).fillna(0)
        g["growth_pct"] = np.where(
            g["first"] > 0,
            ((g["second"] - g["first"]) / g["first"] * 100).round(1),
            0.0,
        )
        declining = g[g["growth_pct"] < -10].sort_values("growth_pct")
        for name, row in declining.iterrows():
            lost = max(round(float(row["first"]) - float(row["second"]), 2), 0.0)
            estimated_loss += lost
            issues.append({
                "text": f"{name} down {abs(row['growth_pct']):.0f}% — ~${lost:,.0f} lost vs prior period",
                "severity": "high" if row["growth_pct"] < -25 else "medium",
            })
            actions.append({
                "text": f"Investigate {name}: refresh photos, reprice, or run a promotion",
                "priority": 1,
            })

    # 2. Revenue concentration risk (top 3 > 60%)
    if has_products and total_revenue > 0:
        prod_rev = df.groupby("product_name")["revenue"].sum().sort_values(ascending=False)
        if len(prod_rev) >= 3:
            top3_pct = float(prod_rev.head(3).sum() / total_revenue * 100)
            if top3_pct > 60:
                at_risk = round(float(prod_rev.iloc[0]) * 0.30, 2)
                estimated_loss += at_risk
                issues.append({
                    "text": f"Top 3 products = {top3_pct:.0f}% of revenue — ${at_risk:,.0f} at risk if top product dips",
                    "severity": "medium",
                })
                actions.append({
                    "text": "Diversify: promote 3–5 secondary products to reduce top-3 concentration below 60%",
                    "priority": 2,
                })

    # 3. Returned/cancelled orders (requires apply_mapping to have set is_returned)
    if "is_returned" in df.columns:
        returned_mask = df["is_returned"].fillna(False).astype(bool)
        returned_rev = float(df.loc[returned_mask, "revenue"].sum())
        if returned_rev > 0 and total_revenue > 0:
            return_rate = returned_rev / total_revenue * 100
            if return_rate > 5:
                estimated_loss += returned_rev
                issues.append({
                    "text": f"Returns/cancellations = ${returned_rev:,.0f} ({return_rate:.1f}% of revenue) in lost sales",
                    "severity": "high" if return_rate > 15 else "medium",
                })
                actions.append({
                    "text": "Audit top-returned products: fix listings, add size guides, or improve quality control",
                    "priority": 1,
                })

    # 4. WoW revenue decline
    if has_date:
        df_dated = df.dropna(subset=["date"]).copy()
        ts = (
            df_dated.set_index("date")
            .resample("W")
            .agg(revenue=("revenue", "sum"))
            .reset_index()
        )
        ts = ts[ts["revenue"] > 0]
        if len(ts) >= 2:
            prev, curr = float(ts["revenue"].iloc[-2]), float(ts["revenue"].iloc[-1])
            if prev > 0 and curr < prev:
                wow_pct = (prev - curr) / prev * 100
                if wow_pct > 5:
                    lost = round(prev - curr, 2)
                    # Only add to loss total if product-level decline hasn't already captured it
                    if not any("lost vs prior period" in i["text"] for i in issues):
                        estimated_loss += lost
                    issues.append({
                        "text": f"This week down ${lost:,.0f} vs last week ({wow_pct:.1f}% drop)",
                        "severity": "high" if wow_pct > 15 else "low",
                    })
                    actions.append({
                        "text": "Check if a top product went out of stock or ad spend dropped this week",
                        "priority": 1,
                    })

    # Deduplicate actions
    seen: set = set()
    deduped: list = []
    for a in actions:
        if a["text"] not in seen:
            seen.add(a["text"])
            deduped.append(a)

    return {
        "title": "Money Wasted",
        "estimated_loss": round(estimated_loss, 2),
        "issues": issues[:5],
        "actions": sorted(deduped, key=lambda x: x["priority"])[:3],
    }
