"""
Data ingestion API — upload, preview, schema detection, dataset management.

PATCHED by kapi_app_ver_1.3:
  - Replaced first-match `detect_dataset_type` with weighted scoring across
    a richer set of dataset shapes: events, users, products, transactions,
    feature_usage, funnel, retention, feedback, support, marketing.
  - Adds data-shape signals (numeric price column → products bonus,
    monetary amount → transactions bonus) so a Kaggle product catalog
    no longer falls through to "unknown" (and therefore no longer pollutes
    the dashboard's Events / Users dropdowns).
  - Tie-breaking by priority order so we always favor the most actionable
    dataset shape.
"""
import logging
import uuid
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.config import get_settings
from core.database import get_db, Dataset
from core.auth import get_current_user, CurrentUser
from models.schemas import DatasetOut, DatasetPreview
from services.rag import build_index, delete_index

log = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])
settings = get_settings()


# ── Encoding detection ───────────────────────────────────────────────────────

_ENCODING_CANDIDATES = ["utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1", "ascii"]


def _detect_encoding(path: Path) -> str:
    """Detect file encoding by trying common encodings, then chardet, then latin-1 fallback."""
    raw = path.read_bytes()[:8192]
    for enc in _ENCODING_CANDIDATES:
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        import chardet
        result = chardet.detect(raw)
        if result and result.get("encoding"):
            detected = result["encoding"]
            try:
                raw.decode(detected)
                return detected
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass
    return "latin-1"


def _read_csv_safe(path: Path, sep: str = ",", nrows: int | None = 5000) -> pd.DataFrame:
    """Read CSV with auto-detected encoding and graceful fallback."""
    encoding = _detect_encoding(path)
    log.debug("[data] Detected encoding '%s' for %s", encoding, path.name)
    try:
        return pd.read_csv(path, sep=sep, nrows=nrows, encoding=encoding)
    except UnicodeDecodeError:
        return pd.read_csv(path, sep=sep, nrows=nrows, encoding="latin-1")
    except pd.errors.ParserError:
        return pd.read_csv(path, sep=sep, nrows=nrows, encoding=encoding, on_bad_lines="skip")

UPLOAD_DIR = settings.storage_dir / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── Type classification (score-based) ────────────────────────────────────────
#
# Each dataset type has a dictionary of column-name signals weighted by how
# diagnostic that signal is. An exact column-name match scores `weight`;
# a substring match (e.g. "user" inside "user_email") scores `weight * 0.75`.
#
# This replaces the original first-match scheme that tagged anything with
# "user" in any column as a `users` dataset, and dumped everything else into
# `unknown` (which then leaked into the dashboard's Events/Users dropdowns
# via the frontend's permissive `dataset_type === 'unknown'` filter).

_TYPE_SIGNALS: dict[str, dict[str, float]] = {
    "events": {
        "event_name": 5.0, "event_type": 5.0, "event": 3.0, "action": 2.0,
        "occurred_at": 2.0, "event_time": 2.0, "timestamp": 1.5,
    },
    "users": {
        "user_id": 4.0, "user_name": 3.0, "username": 3.0, "email": 4.0,
        "plan": 3.0, "first_name": 2.0, "last_name": 2.0,
        "signup": 2.0, "registered": 2.0, "last_active": 2.0,
        "subscription": 2.0, "tier": 2.0,
    },
    "products": {
        "product_name": 5.0, "product_id": 5.0, "sku": 5.0,
        "product": 3.0, "title": 1.0, "brand": 2.0,
        "category": 2.0, "subcategory": 2.0, "department": 1.5,
        "price": 3.0, "msrp": 3.0, "stock": 2.0, "inventory": 2.0,
        "manufacturer": 2.0, "vendor": 2.0,
    },
    "transactions": {
        "transaction_id": 5.0, "order_id": 5.0, "invoice_id": 5.0,
        "amount": 3.0, "subtotal": 2.0, "total": 1.5,
        "currency": 3.0, "payment_method": 3.0, "payment": 2.0,
        "purchase": 2.0, "revenue": 3.0,
        "tax": 1.0, "discount": 1.0,
    },
    "feature_usage": {
        "feature_name": 5.0, "feature_id": 4.0, "feature": 3.0,
        "duration_seconds": 2.0, "duration_ms": 2.0,
    },
    "funnel": {"step": 4.0, "stage": 4.0, "funnel_step": 5.0, "step_name": 4.0},
    "retention": {"cohort": 4.0, "retained": 4.0, "retention_pct": 5.0},
    "feedback": {
        "feedback": 5.0, "comment": 2.0, "review": 4.0,
        "rating": 4.0, "stars": 3.0,
        "sentiment": 4.0, "nps": 5.0, "csat": 5.0, "ces": 5.0,
    },
    "support": {
        "ticket_id": 5.0, "ticket": 4.0, "case_id": 4.0,
        "issue": 3.0, "priority": 2.0, "severity": 3.0,
        "agent": 2.0, "assignee": 2.0,
        "resolved_at": 3.0, "closed_at": 3.0, "first_response": 3.0,
    },
    "marketing": {
        "campaign_id": 5.0, "campaign": 4.0, "channel": 2.0,
        "utm_source": 5.0, "utm_medium": 4.0, "utm_campaign": 4.0,
        "click": 2.0, "impression": 2.0, "ctr": 4.0, "cpc": 4.0,
    },
    "sessions": {
        "session_id": 4.0, "session_start": 4.0, "session_end": 4.0,
        "session_duration": 4.0, "page_views": 3.0,
    },
}

# When two dataset types tie on score, the earlier entry wins. Order is
# from most-actionable for the dashboard to least:
_TYPE_PRIORITY = [
    "events", "users", "products", "transactions",
    "feature_usage", "sessions", "funnel", "retention",
    "feedback", "support", "marketing",
]


def detect_dataset_type(columns: list[str], df: pd.DataFrame | None = None) -> str:
    """Classify a dataset by its column names (and optionally column dtypes).

    `df` is optional; when provided, we add small dtype-based bonuses (e.g. a
    numeric column literally named 'price' nudges classification toward
    products). The signature stays backwards-compatible with the original
    callers that only pass `columns`.
    """
    col_set = {c.lower().strip() for c in columns}
    scores: dict[str, float] = {t: 0.0 for t in _TYPE_SIGNALS}

    for dtype, signals in _TYPE_SIGNALS.items():
        for sig, weight in signals.items():
            if sig in col_set:
                scores[dtype] += weight
            elif any(sig in c for c in col_set):
                scores[dtype] += weight * 0.75

    # Data-shape bonuses — only when df is provided.
    if df is not None and len(df.columns) > 0:
        try:
            for col in df.columns:
                lc = col.lower()
                if pd.api.types.is_numeric_dtype(df[col]):
                    if "price" in lc or "msrp" in lc:
                        scores["products"] += 1.5
                    if "amount" in lc or "revenue" in lc or "total" in lc:
                        scores["transactions"] += 1.0
                    if "duration" in lc:
                        scores["feature_usage"] += 1.0
                    if "rating" in lc or "stars" in lc or "score" in lc:
                        scores["feedback"] += 1.0
        except Exception:
            # Shape inference is a best-effort bonus; never let it fail the
            # whole classification.
            pass

    if all(v == 0 for v in scores.values()):
        return "unknown"

    max_score = max(scores.values())
    for p in _TYPE_PRIORITY:
        if scores.get(p, 0) == max_score:
            return p
    return max(scores, key=lambda k: scores[k])


def safe_stats(df: pd.DataFrame) -> dict[str, Any]:
    """Compute basic stats safely across mixed dtypes."""
    stats: dict[str, Any] = {}
    for col in df.columns:
        try:
            if pd.api.types.is_numeric_dtype(df[col]):
                stats[col] = {
                    "mean": round(float(df[col].mean()), 3),
                    "min": float(df[col].min()),
                    "max": float(df[col].max()),
                    "null_pct": round(df[col].isnull().mean() * 100, 1),
                }
            else:
                top_vals = df[col].value_counts().head(5).to_dict()
                stats[col] = {
                    "unique": int(df[col].nunique()),
                    "top_values": {str(k): int(v) for k, v in top_vals.items()},
                    "null_pct": round(df[col].isnull().mean() * 100, 1),
                }
        except Exception:
            stats[col] = {}
    return stats


def _auto_tag(dataset_type: str, columns: list[str]) -> list[str]:
    tags = [dataset_type]
    if any("user" in c.lower() for c in columns):
        tags.append("users")
    if any("time" in c.lower() or "date" in c.lower() or "ts" in c.lower() for c in columns):
        tags.append("time-series")
    return list(set(tags))


# ── Routes ────────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".json", ".xlsx", ".xls", ".parquet", ".ndjson", ".jsonl", ".xml"}


def _read_file(path: Path, original_name: str) -> pd.DataFrame:
    """Read uploaded file into a DataFrame regardless of format."""
    ext = Path(original_name).suffix.lower()
    if ext in (".csv",):
        return _read_csv_safe(path, sep=",", nrows=5000)
    if ext in (".tsv",):
        return _read_csv_safe(path, sep="\t", nrows=5000)
    if ext in (".json",):
        try:
            return pd.read_json(path, lines=False).head(5000)
        except Exception:
            return pd.read_json(path, lines=True).head(5000)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, nrows=5000)
    if ext in (".parquet",):
        return pd.read_parquet(path).head(5000)
    if ext in (".ndjson", ".jsonl"):
        return pd.read_json(path, lines=True).head(5000)
    if ext in (".xml",):
        return pd.read_xml(path).head(5000)
    raise ValueError(f"Unsupported file format: {ext}")


def _count_rows(path: Path, original_name: str) -> int:
    """Count total rows without loading entire DataFrame."""
    ext = Path(original_name).suffix.lower()
    try:
        if ext in (".csv",):
            enc = _detect_encoding(path)
            return sum(1 for _ in open(path, encoding=enc, errors="ignore")) - 1
        if ext in (".tsv",):
            enc = _detect_encoding(path)
            return sum(1 for _ in open(path, encoding=enc, errors="ignore")) - 1
        if ext in (".json",):
            df = pd.read_json(path, lines=False) if not _is_ndjson(path) else pd.read_json(path, lines=True)
            return len(df)
        if ext in (".xlsx", ".xls"):
            return len(pd.read_excel(path))
        if ext in (".parquet",):
            return len(pd.read_parquet(path))
        if ext in (".ndjson", ".jsonl"):
            return len(pd.read_json(path, lines=True))
        if ext in (".xml",):
            return len(pd.read_xml(path))
    except Exception:
        pass
    return 0


def _is_ndjson(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            first = f.readline().strip()
            return first.startswith(b"{")
    except Exception:
        return False


@router.post("/upload", response_model=DatasetOut)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload CSV, TSV, JSON, or XLSX — detect schema and schedule FAISS indexing."""
    from core.billing import UsageTracker
    allowed, msg = await UsageTracker.check_limit(user.org_id, "datasets", user.plan, db)
    if not allowed:
        raise HTTPException(429, msg)

    fname = file.filename or ""
    ext = Path(fname).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported format '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")

    dataset_id = str(uuid.uuid4())
    dest = UPLOAD_DIR / f"{dataset_id}{ext}"

    # Stream to disk with a hard size cap enforced DURING the copy, so an
    # oversized (or maliciously huge) file is stopped before it fills the disk,
    # not discovered afterward. Clean up the partial file on overflow.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("upload exceeds size cap")
                f.write(chunk)
    except ValueError:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            413,
            f"File exceeds the {settings.max_upload_mb} MB upload limit. "
            f"Please split the file or pre-aggregate it before uploading.",
        )
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read the uploaded file: {exc}")

    try:
        df = _read_file(dest, fname)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not parse file: {exc}")

    if df.empty or len(df.columns) == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "File parsed but contained no data")

    schema_info = {col: str(dtype) for col, dtype in df.dtypes.items()}
    # PATCHED: pass df so dtype-based shape signals contribute.
    dataset_type = detect_dataset_type(df.columns.tolist(), df)
    tags = _auto_tag(dataset_type, df.columns.tolist())

    row_count = _count_rows(dest, fname) or len(df)

    from core.billing import get_plan_limit
    max_rows = get_plan_limit(user.plan, "max_rows_per_dataset")
    if max_rows > 0 and row_count > max_rows:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            429,
            f"Dataset has {row_count:,} rows, but your plan allows max {max_rows:,} rows per dataset. "
            f"Upgrade your plan or reduce the dataset size.",
        )

    dataset = Dataset(
        id=dataset_id,
        org_id=user.org_id,
        name=Path(fname).stem,
        filename=fname,
        filepath=str(dest),
        dataset_type=dataset_type,
        row_count=row_count,
        column_count=len(df.columns),
        schema_info=schema_info,
        tags=tags,
        indexed=False,
    )
    db.add(dataset)

    from core.security import record_audit
    await record_audit(db, user.org_id, user.user_id, "create", "dataset", dataset_id, f"name={fname}, rows={row_count}")

    await db.commit()
    await db.refresh(dataset)

    background_tasks.add_task(_index_dataset, dataset_id, str(dest), dataset.name, fname, db)

    # Give the user actionable data-quality feedback right away.
    from services.analytics.quality import assess_quality
    out = DatasetOut.model_validate(dataset)
    out.quality_notes = assess_quality(df)
    return out


async def _index_dataset(dataset_id: str, filepath: str, name: str, original_name: str, db: AsyncSession) -> None:
    """Background task: build FAISS index and mark dataset as indexed."""
    try:
        df = _read_file(Path(filepath), original_name)
        # Pass the TRUE row count so the index header can disclose truncation:
        # df is a capped read (5000 rows) and the chunker caps again (200) —
        # the model should know how much of the data its answers draw from.
        true_rows = _count_rows(Path(filepath), original_name) or len(df)
        count = build_index(dataset_id, df, name, total_rows=true_rows)

        result = await db.execute(select(Dataset).where(Dataset.id == dataset_id))
        ds = result.scalar_one_or_none()
        if ds:
            ds.indexed = True
            await db.commit()
    except Exception as exc:
        print(f"[Kapi] Index error for {dataset_id}: {exc}")


@router.get("/", response_model=list[DatasetOut])
async def list_datasets(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Dataset)
        .where(Dataset.org_id == user.org_id)
        .order_by(Dataset.uploaded_at.desc())
    )
    out = []
    for ds in result.scalars().all():
        item = DatasetOut.model_validate(ds)
        # Disclose RAG coverage: "indexed 200 of 8,431 rows" beats implying all.
        item.indexed_rows = min(ds.row_count, settings.index_max_rows) if ds.indexed else 0
        out.append(item)
    return out


@router.get("/{dataset_id}/preview", response_model=DatasetPreview)
async def preview_dataset(
    dataset_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.org_id == user.org_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(404, "Dataset not found")

    df = _read_file(Path(ds.filepath), ds.filename).head(100)
    for col in df.select_dtypes(include=["datetime64"]).columns:
        df[col] = df[col].astype(str)

    stats = safe_stats(df)

    return DatasetPreview(
        id=dataset_id,
        columns=df.columns.tolist(),
        rows=df.head(50).fillna("").astype(str).to_dict("records"),
        total_rows=ds.row_count,
        schema_info=ds.schema_info,
        detected_type=ds.dataset_type,
        sample_stats=stats,
    )


@router.delete("/{dataset_id}")
async def delete_dataset(
    dataset_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.org_id == user.org_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(404, "Dataset not found")

    Path(ds.filepath).unlink(missing_ok=True)
    delete_index(dataset_id)

    from core.security import record_audit
    await record_audit(db, user.org_id, user.user_id, "delete", "dataset", dataset_id, f"name={ds.name}")

    await db.delete(ds)
    await db.commit()
    return {"ok": True}


@router.get("/{dataset_id}/load")
async def load_dataframe(
    dataset_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Internal helper: load full DataFrame as JSON records (max 10k rows)."""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.org_id == user.org_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(404, "Dataset not found")

    df = _read_file(Path(ds.filepath), ds.filename).head(10_000)
    return {"dataset_type": ds.dataset_type, "columns": df.columns.tolist(), "row_count": len(df)}
