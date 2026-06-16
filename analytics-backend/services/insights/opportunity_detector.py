"""
Hidden Opportunity Detector — identifies where the shop can make more money.
Works on canonical DataFrame (output of apply_mapping).
"""
import pandas as pd
import numpy as np


def detect_opportunities(df: pd.DataFrame) -> dict:
    items: list = []
    actions: list = []
    estimated_gain = 0.0

    has_products = "product_name" in df.columns
    has_date = "date" in df.columns and df["date"].notna().any()

    # 1. Fast-growing products (growth > 20%)
    if has_products and has_date:
        midpoint = df["date"].min() + (df["date"].max() - df["date"].min()) / 2
        first_half = df[df["date"] <= midpoint].groupby("product_name")["revenue"].sum()
        second_half = df[df["date"] > midpoint].groupby("product_name")["revenue"].sum()
        g = pd.DataFrame({"first": first_half, "second": second_half}).fillna(0)
        g["growth_pct"] = np.where(
            g["first"] > 0,
            ((g["second"] - g["first"]) / g["first"] * 100).round(1),
            np.where(g["second"] > 0, 100.0, 0.0),
        )
        fast = g[g["growth_pct"] > 20].sort_values("growth_pct", ascending=False)
        for name, row in fast.iterrows():
            curr = float(row["second"])
            gain = round(curr * row["growth_pct"] / 100, 2)
            estimated_gain += gain
            items.append({
                "text": f"{name} up {row['growth_pct']:.0f}% — potential +${gain:,.0f} if trend holds",
                "impact": "high" if row["growth_pct"] > 50 else "medium",
            })
            actions.append({
                "text": f"Scale {name}: increase ad budget and restock inventory now",
                "priority": 1 if row["growth_pct"] > 50 else 2,
            })

    # 2. Underutilized products (below median revenue, 5–20% growth not already caught above)
    if has_products and has_date:
        prod_rev = df.groupby("product_name")["revenue"].sum()
        med_rev = float(prod_rev.median())
        midpoint = df["date"].min() + (df["date"].max() - df["date"].min()) / 2
        first_half = df[df["date"] <= midpoint].groupby("product_name")["revenue"].sum()
        second_half = df[df["date"] > midpoint].groupby("product_name")["revenue"].sum()
        candidates = []
        for name in prod_rev.index:
            rev = float(prod_rev[name])
            first = float(first_half.get(name, 0))
            second = float(second_half.get(name, 0))
            if rev < med_rev and first > 0:
                g_pct = (second - first) / first * 100
                if 5 < g_pct <= 20:
                    candidates.append((name, g_pct, rev))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            name, g_pct, rev = candidates[0]
            gain = round(rev * g_pct / 100, 2)
            estimated_gain += gain
            items.append({
                "text": f"{name} has {g_pct:.0f}% growth but low exposure — potential +${gain:,.0f}",
                "impact": "medium",
            })
            actions.append({
                "text": f"Feature {name} in your store front and bundle it with top sellers",
                "priority": 3,
            })

    # 3. Overall positive revenue trend (weekly momentum)
    if has_date:
        df_dated = df.dropna(subset=["date"]).copy()
        ts = (
            df_dated.set_index("date")
            .resample("W")
            .agg(revenue=("revenue", "sum"))
            .reset_index()
        )
        ts = ts[ts["revenue"] > 0]
        if len(ts) >= 4:
            recent = float(ts["revenue"].iloc[-2:].mean())
            prior = float(ts["revenue"].iloc[-4:-2].mean())
            if prior > 0 and recent > prior:
                trend_pct = (recent - prior) / prior * 100
                if trend_pct > 5:
                    gain = round(recent * trend_pct / 100, 2)
                    estimated_gain += gain
                    items.append({
                        "text": f"Revenue trend up {trend_pct:.1f}% over recent weeks — momentum is building",
                        "impact": "high" if trend_pct > 20 else "medium",
                    })
                    actions.append({
                        "text": "Momentum is up — increase spend on best-performing ads to accelerate growth",
                        "priority": 2,
                    })

    # Deduplicate actions
    seen: set = set()
    deduped: list = []
    for a in actions:
        if a["text"] not in seen:
            seen.add(a["text"])
            deduped.append(a)

    return {
        "title": "Opportunities",
        "estimated_gain": round(estimated_gain, 2),
        "items": items[:5],
        "actions": sorted(deduped, key=lambda x: x["priority"])[:3],
    }
