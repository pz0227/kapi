"""
Analytics routes — KPIs, funnel, retention, segmentation, anomalies.

PATCHED by kapi_app_ver_1.3:
  - Helpful, contextual 422 errors. Instead of "Events dataset must have a
    timestamp column", we now tell the user which dataset they picked, what
    type it was classified as, what columns it has, and what columns we'd
    need to make this view work. We also suggest renames when the dataset
    contains an obvious candidate (e.g. "created_at" → rename to "timestamp").
  - More aggressive column-name normalization in _load_df: handles user_id
    variants (user, customer_id, account_id), event_name variants
    (event, action, action_name), and a wider set of timestamp spellings
    (date, occurred_at, created_at, updated_at, ts, time, datetime).
  - New /overview endpoint — returns dataset-shape-appropriate KPIs:
      • events     → existing time-series KPIs (DAU/MAU/Top events)
      • products   → row count, top categories/brands, price stats
      • transactions → row count, revenue, AOV, top products
      • users      → row count, plan distribution, signup trend
      • everything else → row count + categorical/numeric summaries
    The dashboard can call this once and get the right shape automatically.
  - /summary wraps each section in try/except so a partial failure (e.g. no
    funnel events) doesn't take down the whole page.
"""
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, Dataset
from core.auth import get_current_user, CurrentUser
from api.routes.data import _read_csv_safe
from models.schemas import FunnelResult, RetentionResult
from services.analytics import (
    compute_kpis,
    compute_anomalies,
    compute_funnel,
    auto_detect_funnel,
    compute_retention,
    compute_segments,
    compute_feature_adoption,
    compute_executive_summary,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ── Column normalization ────────────────────────────────────────────────────
#
# Many real-world datasets use a different name for the same column. We
# rename a small set of canonical fields so the rest of the code can assume
# `timestamp`, `user_id`, `event_name` are present when they exist.

_TIMESTAMP_ALIASES = {
    "timestamp", "ts", "time", "datetime",
    "date", "event_date", "created_date",
    "created_at", "updated_at", "occurred_at", "event_time",
    "logged_at", "received_at",
}
_USER_ID_ALIASES = {
    "user_id", "userid", "user", "uid",
    "customer_id", "customer", "account_id", "account",
    "person_id", "visitor_id", "client_id",
}
_EVENT_NAME_ALIASES = {
    "event_name", "event", "event_type", "name",
    "action", "action_name", "action_type", "type",
}


def _normalize_event_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename common column-name variants so downstream KPI code can assume
    canonical names. Only renames if the canonical column doesn't already
    exist."""
    rename: dict[str, str] = {}
    lower = {c.lower().strip(): c for c in df.columns}

    # Timestamp — try aliases in priority order.
    if "timestamp" not in df.columns:
        for alias in (
            "timestamp", "event_time", "occurred_at", "created_at",
            "event_date", "date", "ts", "datetime", "time",
            "updated_at", "logged_at", "received_at", "created_date",
        ):
            if alias in lower and lower[alias] != "timestamp":
                rename[lower[alias]] = "timestamp"
                break

    # user_id
    if "user_id" not in df.columns:
        for alias in ("user_id", "userid", "uid", "customer_id", "account_id", "user", "person_id", "visitor_id"):
            if alias in lower and lower[alias] != "user_id":
                rename[lower[alias]] = "user_id"
                break

    # event_name
    if "event_name" not in df.columns:
        for alias in ("event_name", "event_type", "event", "action_name", "action", "type", "name"):
            if alias in lower and lower[alias] != "event_name":
                rename[lower[alias]] = "event_name"
                break

    if rename:
        df = df.rename(columns=rename)

    # Best-effort timestamp parse — if rename surfaced a 'timestamp' column,
    # try to coerce it to datetime. If parsing fails, leave it as-is and let
    # downstream logic raise a friendlier error.
    if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        try:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            # Drop rows where the parse produced NaT — they're unusable for
            # any time-series KPI and would otherwise crash df['timestamp'].max().
            if df["timestamp"].isna().all():
                df = df.drop(columns=["timestamp"])
            else:
                df = df.dropna(subset=["timestamp"])
        except Exception:
            df = df.drop(columns=["timestamp"], errors="ignore")

    return df


async def _load_df_and_meta(
    dataset_id: str, user: CurrentUser, db: AsyncSession
) -> tuple[pd.DataFrame, Dataset]:
    """Load a dataset (with org-level ACL) and return both the DataFrame and
    its DB row, so we can give helpful errors that include the dataset name
    and its detected type."""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.org_id == user.org_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    df = _read_csv_safe(Path(ds.filepath), nrows=None)
    df = _normalize_event_columns(df)
    return df, ds


async def _load_df(dataset_id: str, user: CurrentUser, db: AsyncSession) -> pd.DataFrame:
    """Backwards-compatible loader returning just the DataFrame."""
    df, _ = await _load_df_and_meta(dataset_id, user, db)
    return df


# ── Friendly errors ──────────────────────────────────────────────────────────

def _suggest_rename(missing: str, columns: list[str]) -> Optional[str]:
    """If the dataset contains an obvious candidate for the missing canonical
    column, return a one-line rename suggestion."""
    aliases = {
        "timestamp": _TIMESTAMP_ALIASES,
        "user_id": _USER_ID_ALIASES,
        "event_name": _EVENT_NAME_ALIASES,
    }.get(missing, set())
    lc = {c.lower(): c for c in columns}
    for alias in aliases:
        if alias in lc:
            return f"Hint: rename column '{lc[alias]}' to '{missing}' and re-upload."
    return None


def _missing_column_error(
    missing: str, ds: Dataset, df_columns: list[str], view: str
) -> HTTPException:
    """Build a 422 that actually helps the user fix the problem."""
    parts = [
        f"The {view} view requires a '{missing}' column, but your dataset "
        f"'{ds.name}' (detected type: {ds.dataset_type}) doesn't have one.",
        f"Available columns: {', '.join(df_columns) if df_columns else '(none)'}.",
    ]
    hint = _suggest_rename(missing, df_columns)
    if hint:
        parts.append(hint)
    if ds.dataset_type not in ("events", "unknown"):
        parts.append(
            f"This dataset looks like a {ds.dataset_type} dataset — try the "
            f"Overview tab, which auto-adapts to the dataset's shape."
        )
    return HTTPException(422, " ".join(parts))


# ── Non-time-series fallback KPIs ────────────────────────────────────────────

def _top_values(df: pd.DataFrame, col: str, k: int = 5) -> list[dict]:
    try:
        vc = df[col].value_counts().head(k)
        return [{"name": str(idx), "count": int(v)} for idx, v in vc.items()]
    except Exception:
        return []


def _numeric_stats(df: pd.DataFrame, col: str) -> Optional[dict]:
    try:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) == 0:
            return None
        return {
            "min": round(float(s.min()), 2),
            "max": round(float(s.max()), 2),
            "mean": round(float(s.mean()), 2),
            "median": round(float(s.median()), 2),
        }
    except Exception:
        return None


def _categorical_columns(df: pd.DataFrame, max_unique: int = 50) -> list[str]:
    out: list[str] = []
    for col in df.columns:
        try:
            if df[col].dtype == object and 1 < df[col].nunique() <= max_unique:
                out.append(col)
        except Exception:
            pass
    return out


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _basic_kpis(df: pd.DataFrame, ds: Dataset) -> list[dict]:
    """Generic KPIs for any tabular dataset (no timestamp required).

    Returns a list of card dicts shaped like the time-series KPIs, so the
    same dashboard component can render them.
    """
    cards: list[dict] = []

    cards.append({
        "label": "Total Rows",
        "value": int(len(df)),
        "unit": "rows",
        "delta": None,
        "delta_label": ds.dataset_type or "dataset",
        "trend": [],
    })

    cards.append({
        "label": "Columns",
        "value": int(len(df.columns)),
        "unit": "fields",
        "delta": None,
        "delta_label": "schema",
        "trend": [],
    })

    # Pick up to 2 categorical columns and surface their top values.
    for col in _categorical_columns(df)[:2]:
        top = _top_values(df, col, k=5)
        if top:
            cards.append({
                "label": f"Top {col}",
                "value": top,
                "unit": "breakdown",
                "delta": None,
                "delta_label": f"by {col}",
                "trend": [],
            })

    # Pick up to 2 numeric columns and surface basic stats.
    for col in _numeric_columns(df)[:2]:
        stats = _numeric_stats(df, col)
        if stats:
            cards.append({
                "label": f"{col} (avg)",
                "value": stats["mean"],
                "unit": col,
                "delta": None,
                "delta_label": f"min {stats['min']} – max {stats['max']}",
                "trend": [],
            })

    return cards


def _products_kpis(df: pd.DataFrame, ds: Dataset) -> list[dict]:
    cards: list[dict] = [
        {
            "label": "Total Products",
            "value": int(len(df)),
            "unit": "products",
            "delta": None,
            "delta_label": "catalog",
            "trend": [],
        }
    ]

    # Distinct categories / brands.
    for col_name, label in [("category", "Categories"), ("brand", "Brands"), ("product_category", "Categories")]:
        match = next((c for c in df.columns if c.lower() == col_name), None)
        if match:
            try:
                cards.append({
                    "label": f"Distinct {label}",
                    "value": int(df[match].nunique()),
                    "unit": label.lower(),
                    "delta": None,
                    "delta_label": f"by {match}",
                    "trend": [],
                })
                top = _top_values(df, match, k=5)
                if top:
                    cards.append({
                        "label": f"Top {label}",
                        "value": top,
                        "unit": "breakdown",
                        "delta": None,
                        "delta_label": f"by {match}",
                        "trend": [],
                    })
            except Exception:
                pass
            break

    # Price stats (case-insensitive match for Price, price, MSRP, msrp).
    price_col = next(
        (c for c in df.columns if c.lower() in ("price", "msrp", "list_price", "unit_price")),
        None,
    )
    if price_col:
        stats = _numeric_stats(df, price_col)
        if stats:
            cards.append({
                "label": f"Avg {price_col}",
                "value": stats["mean"],
                "unit": "$",
                "delta": None,
                "delta_label": f"min ${stats['min']} – max ${stats['max']}",
                "trend": [],
            })

    return cards


def _transactions_kpis(df: pd.DataFrame, ds: Dataset) -> list[dict]:
    cards: list[dict] = [
        {
            "label": "Total Transactions",
            "value": int(len(df)),
            "unit": "transactions",
            "delta": None,
            "delta_label": "all time",
            "trend": [],
        }
    ]

    revenue_col = next(
        (c for c in df.columns if c.lower() in ("amount", "revenue", "total", "subtotal", "price")),
        None,
    )
    if revenue_col:
        try:
            s = pd.to_numeric(df[revenue_col], errors="coerce").dropna()
            if len(s):
                cards.append({
                    "label": "Total Revenue",
                    "value": round(float(s.sum()), 2),
                    "unit": "$",
                    "delta": None,
                    "delta_label": revenue_col,
                    "trend": [],
                })
                cards.append({
                    "label": "Avg Order Value",
                    "value": round(float(s.mean()), 2),
                    "unit": "$",
                    "delta": None,
                    "delta_label": "per transaction",
                    "trend": [],
                })
        except Exception:
            pass

    return cards


def _users_kpis(df: pd.DataFrame, ds: Dataset) -> list[dict]:
    cards: list[dict] = [
        {
            "label": "Total Users",
            "value": int(len(df)),
            "unit": "users",
            "delta": None,
            "delta_label": "all time",
            "trend": [],
        }
    ]

    plan_col = next((c for c in df.columns if c.lower() in ("plan", "tier", "subscription")), None)
    if plan_col:
        top = _top_values(df, plan_col, k=10)
        if top:
            cards.append({
                "label": "Plan Distribution",
                "value": top,
                "unit": "breakdown",
                "delta": None,
                "delta_label": f"by {plan_col}",
                "trend": [],
            })

    return cards


def _kpis_for_dataset(df: pd.DataFrame, ds: Dataset, users_df: pd.DataFrame | None = None) -> list[dict]:
    """Pick the right KPI shape for the dataset's detected type. Falls back
    to generic stats if the time-series version raises."""
    dt = ds.dataset_type or "unknown"

    if dt == "events":
        # Time-series events — use the original KPI engine if usable.
        if "timestamp" in df.columns and "user_id" in df.columns:
            try:
                return compute_kpis(df, users_df)
            except Exception:
                # Fall through to basic KPIs if the engine bails on real data.
                pass
        # Events dataset that's missing the required columns — still useful
        # to show row counts and top events.
        return _basic_kpis(df, ds)

    if dt == "products":
        return _products_kpis(df, ds)
    if dt == "transactions":
        return _transactions_kpis(df, ds)
    if dt == "users":
        return _users_kpis(df, ds)

    # Unknown / other shapes — generic fallback.
    return _basic_kpis(df, ds)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/kpis")
async def get_kpis(
    events_dataset_id: str = Query(...),
    users_dataset_id: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """KPIs for the selected dataset.

    PATCHED behavior:
      - If the dataset is an `events` shape with timestamp + user_id, returns
        the original time-series KPIs (DAU/MAU/Top events + anomalies).
      - Otherwise, returns shape-appropriate KPIs (products / transactions /
        users / generic) so the dashboard never shows "Failed to fetch" for
        a non-events dataset.
    """
    events_df, ds = await _load_df_and_meta(events_dataset_id, user, db)

    users_df = None
    if users_dataset_id:
        try:
            users_df = await _load_df(users_dataset_id, user, db)
        except HTTPException:
            users_df = None

    # Time-series path (events with both required columns).
    if (ds.dataset_type or "") == "events" or (
        "timestamp" in events_df.columns and "user_id" in events_df.columns
    ):
        if "timestamp" in events_df.columns and "user_id" in events_df.columns:
            try:
                kpis = compute_kpis(events_df, users_df)
                anomalies = compute_anomalies(events_df)
                return {"kpis": kpis, "anomalies": anomalies}
            except Exception:
                pass  # fall through to non-time-series KPIs

        # Events-typed dataset that's missing required columns — surface a
        # helpful error so the user knows what to fix.
        if "timestamp" not in events_df.columns:
            raise _missing_column_error(
                "timestamp", ds, events_df.columns.tolist(), "Events"
            )
        if "user_id" not in events_df.columns:
            raise _missing_column_error(
                "user_id", ds, events_df.columns.tolist(), "Events"
            )

    # Non-time-series fallback — works for products / transactions / users / unknown.
    kpis = _kpis_for_dataset(events_df, ds, users_df)
    return {"kpis": kpis, "anomalies": []}


@router.get("/overview")
async def get_overview(
    dataset_id: str = Query(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dataset-shape-aware KPIs.

    Use this when the UI doesn't know in advance whether the user picked an
    events / products / users dataset. Returns the same `{kpis, anomalies}`
    shape as `/kpis` so the same dashboard component can render it.
    """
    df, ds = await _load_df_and_meta(dataset_id, user, db)
    kpis = _kpis_for_dataset(df, ds, None)

    anomalies: list[dict] = []
    if (ds.dataset_type == "events") and "timestamp" in df.columns:
        try:
            anomalies = compute_anomalies(df)
        except Exception:
            anomalies = []

    return {
        "kpis": kpis,
        "anomalies": anomalies,
        "dataset_type": ds.dataset_type,
        "dataset_name": ds.name,
        "row_count": int(len(df)),
        "columns": df.columns.tolist(),
    }


@router.get("/funnel")
async def get_funnel(
    events_dataset_id: str = Query(...),
    steps: Optional[str] = Query(None, description="Comma-separated event names"),
    window_hours: int = Query(168),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    events_df, ds = await _load_df_and_meta(events_dataset_id, user, db)
    if "timestamp" not in events_df.columns:
        raise _missing_column_error(
            "timestamp", ds, events_df.columns.tolist(), "Funnel"
        )
    if "event_name" not in events_df.columns:
        raise _missing_column_error(
            "event_name", ds, events_df.columns.tolist(), "Funnel"
        )

    if steps:
        step_list = [s.strip() for s in steps.split(",") if s.strip()]
    else:
        step_list = auto_detect_funnel(events_df)

    if not step_list:
        available = events_df["event_name"].value_counts().head(10).index.tolist()
        raise HTTPException(
            422,
            "Could not auto-detect funnel steps. Please pass steps=event1,event2,... "
            f"Top events in this dataset: {', '.join(map(str, available))}.",
        )

    result = compute_funnel(events_df, step_list, window_hours=window_hours)
    available_events = events_df["event_name"].value_counts().head(30).index.tolist()
    return {**result, "available_events": available_events, "used_steps": step_list}


@router.get("/retention")
async def get_retention(
    events_dataset_id: str = Query(...),
    period: str = Query("week", description="day | week | month"),
    max_periods: int = Query(8),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    events_df, ds = await _load_df_and_meta(events_dataset_id, user, db)
    if "timestamp" not in events_df.columns:
        raise _missing_column_error(
            "timestamp", ds, events_df.columns.tolist(), "Retention"
        )
    if "user_id" not in events_df.columns:
        raise _missing_column_error(
            "user_id", ds, events_df.columns.tolist(), "Retention"
        )

    result = compute_retention(events_df, period=period, max_periods=max_periods)
    return result


@router.get("/segments")
async def get_segments(
    users_dataset_id: str = Query(...),
    segment_col: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    users_df, ds = await _load_df_and_meta(users_dataset_id, user, db)
    seg_col = segment_col or "plan"
    if seg_col not in users_df.columns:
        # Auto-pick first low-cardinality categorical column.
        for col in users_df.columns:
            try:
                if users_df[col].dtype == object and 1 < users_df[col].nunique() < 20:
                    seg_col = col
                    break
            except Exception:
                continue

    if seg_col not in users_df.columns:
        raise HTTPException(
            422,
            f"No suitable segmentation column found in '{ds.name}'. "
            f"Available columns: {', '.join(users_df.columns.tolist())}. "
            f"Pass segment_col=<column-name> to choose one explicitly.",
        )

    segments = compute_segments(users_df, segment_col=seg_col)
    return {"segments": segments, "segment_col": seg_col}


@router.get("/features")
async def get_features(
    events_dataset_id: str = Query(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    events_df, ds = await _load_df_and_meta(events_dataset_id, user, db)
    if "event_name" not in events_df.columns:
        raise _missing_column_error(
            "event_name", ds, events_df.columns.tolist(), "Feature Adoption"
        )

    features = compute_feature_adoption(events_df)
    return {"features": features}


@router.get("/summary")
async def get_summary(
    events_dataset_id: str = Query(...),
    users_dataset_id: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compute all analytics and return a combined executive summary.

    PATCHED: each section is wrapped so a single failure (e.g. missing
    funnel events) doesn't 500 the whole dashboard.
    """
    events_df, ds = await _load_df_and_meta(events_dataset_id, user, db)
    users_df = None
    if users_dataset_id:
        try:
            users_df = await _load_df(users_dataset_id, user, db)
        except HTTPException:
            users_df = None

    has_ts = "timestamp" in events_df.columns
    has_uid = "user_id" in events_df.columns
    has_event_name = "event_name" in events_df.columns

    kpis: list[dict] = []
    funnel: dict = {}
    retention: dict = {}
    features: list[dict] = []
    summary: dict = {}

    try:
        kpis = _kpis_for_dataset(events_df, ds, users_df)
    except Exception as exc:
        kpis = [{"label": "Error", "value": str(exc), "unit": "", "delta": None, "delta_label": "", "trend": []}]

    if has_ts and has_event_name:
        try:
            funnel = compute_funnel(events_df, auto_detect_funnel(events_df))
        except Exception:
            funnel = {}

    if has_ts and has_uid:
        try:
            retention = compute_retention(events_df)
        except Exception:
            retention = {}

    if has_event_name:
        try:
            features = compute_feature_adoption(events_df)
        except Exception:
            features = []

    try:
        summary = compute_executive_summary(kpis, funnel, retention, features)
    except Exception:
        summary = {}

    return {
        "kpis": kpis,
        "funnel": funnel,
        "retention": retention,
        "features": features,
        "executive_summary": summary,
        "dataset_type": ds.dataset_type,
        "dataset_name": ds.name,
    }
