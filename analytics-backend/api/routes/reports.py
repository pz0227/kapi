"""
Report generation routes.
"""
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, Dataset, Report, ProviderConfig
from models.schemas import ReportRequest, ReportOut
from services.providers import get_provider
from services.providers.registry import get_fallback_provider
from services.rag import retrieve, format_context
from services.reports import generate_report, REPORT_DISPLAY_NAMES
from services.analytics import (
    compute_kpis, compute_funnel, compute_retention,
    compute_feature_adoption, compute_executive_summary, auto_detect_funnel
)
from services.analytics.normalize import normalize_event_columns
from core.auth import get_current_user, CurrentUser
from api.routes.data import _read_csv_safe

router = APIRouter(prefix="/reports", tags=["reports"])


async def _get_provider(config_id: str | None, db: AsyncSession):
    if config_id:
        result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
        pc = result.scalar_one_or_none()
    else:
        result = await db.execute(select(ProviderConfig).where(ProviderConfig.is_active == True))
        pc = result.scalar_one_or_none()
    if not pc:
        fallback = get_fallback_provider()
        if fallback:
            return fallback
        raise HTTPException(
            503,
            "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "environment variable, or configure a provider in Settings.",
        )
    return get_provider(pc.id, pc.provider, pc.model, pc.auth_method, pc.api_key_encrypted, pc.session_file or "")


async def _build_context(dataset_ids: list[str], query: str, db: AsyncSession) -> str:
    """Combine RAG retrieval with computed analytics summary."""
    context_parts = []

    # Computed analytics for events datasets
    for did in dataset_ids:
        result = await db.execute(select(Dataset).where(Dataset.id == did))
        ds = result.scalar_one_or_none()
        if not ds:
            continue
        try:
            df = _read_csv_safe(Path(ds.filepath), nrows=None)
            # Shared normalization: map aliased columns (created_at, customer_id,
            # ...) to canonical names so reports see KPIs on the same datasets
            # the analytics view does, not just literally-named ones.
            df = normalize_event_columns(df)

            if ds.dataset_type == "events" and "timestamp" in df.columns and "user_id" in df.columns:
                kpis = compute_kpis(df)
                funnel = compute_funnel(df, auto_detect_funnel(df))
                retention = compute_retention(df)
                features = compute_feature_adoption(df)
                summary = compute_executive_summary(kpis, funnel, retention, features)
                context_parts.append(summary)
        except Exception:
            pass

    # RAG retrieval
    sources = retrieve(query, dataset_ids, top_k=8)
    if sources:
        context_parts.append(format_context(sources))

    return "\n\n".join(context_parts)


@router.post("/", response_model=ReportOut)
async def create_report(
    body: ReportRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.report_type not in REPORT_DISPLAY_NAMES:
        raise HTTPException(400, f"Unknown report type. Valid: {list(REPORT_DISPLAY_NAMES.keys())}")

    # ── Usage check ──
    from core.billing import UsageTracker
    allowed, msg = await UsageTracker.check_limit(user.org_id, "reports", user.plan, db)
    if not allowed:
        raise HTTPException(429, msg)

    provider = await _get_provider(body.provider_config_id, db)
    query = f"{body.report_type} {body.extra_context}"
    context = await _build_context(body.dataset_ids, query, db)

    try:
        content = await generate_report(
            report_type=body.report_type,
            provider=provider,
            context=context,
            extra_context=body.extra_context,
        )
    except Exception as exc:
        err = str(exc)
        if "401" in err or "authentication" in err.lower() or "api_key" in err.lower() or "invalid" in err.lower():
            raise HTTPException(401, "Provider authentication failed — check your API key in Settings.")
        raise HTTPException(502, f"LLM provider error: {err}")

    title = body.title or REPORT_DISPLAY_NAMES[body.report_type]
    report = Report(
        id=str(uuid.uuid4()),
        org_id=user.org_id,
        report_type=body.report_type,
        title=title,
        content=content,
        dataset_ids=body.dataset_ids,
        provider_config_id=body.provider_config_id,
    )
    db.add(report)
    await UsageTracker.record_usage(user.org_id, "reports", 1, db)
    await db.commit()
    await db.refresh(report)
    return ReportOut.model_validate(report)


@router.get("/", response_model=list[ReportOut])
async def list_reports(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Report)
        .where(Report.org_id == user.org_id)
        .order_by(Report.created_at.desc())
    )
    return [ReportOut.model_validate(r) for r in result.scalars().all()]


@router.get("/types")
async def get_report_types():
    return [{"id": k, "label": v} for k, v in REPORT_DISPLAY_NAMES.items()]


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(
    report_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.org_id == user.org_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found")
    return ReportOut.model_validate(r)


@router.delete("/{report_id}")
async def delete_report(
    report_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.org_id == user.org_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}
