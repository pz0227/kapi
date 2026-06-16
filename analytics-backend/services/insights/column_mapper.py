"""
Auto-detect and normalize CSV columns to canonical business-data roles.
Supports English and Chinese (Douyin/TikTok) column names.
"""
import pandas as pd
from typing import Optional

# Priority-ordered candidate names for each semantic role
ROLE_CANDIDATES: dict[str, list[str]] = {
    "order_id": [
        "order_id", "order_no", "order_number", "orderid", "orderno",
        "transaction_id", "txn_id", "invoice",
        "订单编号", "订单号", "交易编号",
    ],
    "product_name": [
        "product_name", "product", "item_name", "item", "sku_name",
        "product_title", "title", "name", "goods_name",
        "商品名称", "商品", "商品标题",
    ],
    "revenue": [
        "revenue", "total_amount", "amount", "total", "sales",
        "order_amount", "payment", "gmv", "subtotal", "total_price",
        "price_total", "net_amount",
        "实付金额", "商品金额", "订单金额", "总金额",
    ],
    "quantity": [
        "quantity", "qty", "units", "count", "num_items", "order_qty",
        "数量", "购买数量",
    ],
    "date": [
        "date", "created_time", "order_date", "timestamp", "created_at",
        "order_time", "purchase_date", "transaction_date", "created",
        "日期", "创建时间", "下单时间", "订单时间",
    ],
    "customer_id": [
        "customer_id", "buyer_id", "user_id", "client_id", "cust_id",
        "buyer", "customer", "shopper_id",
        "买家ID", "用户ID", "客户编号",
    ],
    "category": [
        "category", "product_category", "cat", "department", "type",
        "分类", "商品分类", "类目",
    ],
    "price": [
        "price", "unit_price", "item_price", "sell_price",
        "单价", "商品单价",
    ],
    "cost": [
        "cost", "unit_cost", "cogs", "cost_price", "purchase_price",
        "成本", "进货价",
    ],
    "discount": [
        "discount", "discount_amount", "coupon", "promo_discount",
        "优惠金额", "折扣",
    ],
    "status": [
        "status", "order_status", "state", "fulfillment_status",
        "订单状态", "状态",
    ],
}

# Minimum required columns per analysis mode
MODE_REQUIREMENTS: dict[str, list[str]] = {
    "shop_health": ["revenue", "date"],
    "product_performance": ["revenue", "product_name"],
    "trend_spotter": ["revenue", "date"],
}


def auto_detect_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    """Map DataFrame columns to semantic roles. Returns {role: original_col_name}."""
    mapping: dict[str, Optional[str]] = {}
    used_cols: set[str] = set()
    col_list = list(df.columns)
    col_lower = {c: c.lower().strip() for c in col_list}

    for role, candidates in ROLE_CANDIDATES.items():
        matched = _match_column(col_list, col_lower, candidates, used_cols)
        if matched:
            mapping[role] = matched
            used_cols.add(matched)
        else:
            mapping[role] = None

    # Fallback: revenue — pick highest-mean numeric column
    if mapping["revenue"] is None:
        mapping["revenue"] = _fallback_numeric(df, used_cols)
        if mapping["revenue"]:
            used_cols.add(mapping["revenue"])

    # Fallback: date — first column that parses as datetime
    if mapping["date"] is None:
        mapping["date"] = _fallback_date(df, used_cols)
        if mapping["date"]:
            used_cols.add(mapping["date"])

    # Fallback: customer_id — string column with moderate cardinality
    if mapping["customer_id"] is None:
        mapping["customer_id"] = _fallback_customer_id(df, used_cols)

    return mapping


def _match_column(
    col_list: list[str],
    col_lower: dict[str, str],
    candidates: list[str],
    used: set[str],
) -> Optional[str]:
    """Try matching a candidate to columns: exact, contains, contained-by."""
    for cand in candidates:
        cand_lower = cand.lower()
        # Exact match
        for c in col_list:
            if c in used:
                continue
            if col_lower[c] == cand_lower:
                return c
        # Column contains candidate
        for c in col_list:
            if c in used:
                continue
            if cand_lower in col_lower[c]:
                return c
        # Candidate contains column
        for c in col_list:
            if c in used:
                continue
            if col_lower[c] in cand_lower and len(col_lower[c]) >= 3:
                return c
    return None


def _fallback_numeric(df: pd.DataFrame, used: set[str]) -> Optional[str]:
    """Pick the numeric column with highest mean (likely revenue)."""
    best_col, best_mean = None, -1.0
    for c in df.columns:
        if c in used:
            continue
        if df[c].dtype in ("float64", "int64", "float32", "int32"):
            mean_val = df[c].mean()
            if mean_val > best_mean:
                best_mean = mean_val
                best_col = c
    return best_col


def _fallback_date(df: pd.DataFrame, used: set[str]) -> Optional[str]:
    """First column that parses as datetime with < 50% errors."""
    for c in df.columns:
        if c in used:
            continue
        if df[c].dtype == "datetime64[ns]":
            return c
        try:
            parsed = pd.to_datetime(df[c], errors="coerce", infer_datetime_format=True)
            if parsed.notna().mean() > 0.5:
                return c
        except (ValueError, TypeError):
            continue
    return None


def _fallback_customer_id(df: pd.DataFrame, used: set[str]) -> Optional[str]:
    """String column with moderate cardinality (5-80% of rows)."""
    n = len(df)
    if n == 0:
        return None
    for c in df.columns:
        if c in used:
            continue
        if df[c].dtype == "object":
            nunique = df[c].nunique()
            ratio = nunique / n
            if 0.05 <= ratio <= 0.80:
                return c
    return None


def apply_mapping(df: pd.DataFrame, mapping: dict[str, Optional[str]]) -> pd.DataFrame:
    """Create a normalized DataFrame with canonical column names."""
    rename_map = {}
    for role, original_col in mapping.items():
        if original_col and original_col in df.columns:
            rename_map[original_col] = role

    out = df.rename(columns=rename_map).copy()

    # Parse date
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")

    # Coerce numeric columns
    for col in ("revenue", "price", "cost", "discount", "quantity"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    # Derive is_returned from status
    if "status" in out.columns:
        status_lower = out["status"].astype(str).str.lower()
        return_patterns = ["return", "refund", "cancel", "退货", "退款", "取消"]
        out["is_returned"] = status_lower.apply(
            lambda s: any(p in s for p in return_patterns)
        )

    return out


def validate_mapping(
    mapping: dict[str, Optional[str]], mode: str
) -> list[str]:
    """Return list of error messages if required columns are missing for this mode."""
    required = MODE_REQUIREMENTS.get(mode, [])
    errors = []
    for role in required:
        if not mapping.get(role):
            errors.append(f"Mode '{mode}' requires a '{role}' column but none was detected.")
    return errors
