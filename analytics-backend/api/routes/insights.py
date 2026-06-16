"""
Insights API — structured business analysis endpoints.
"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import pandas as pd
import uuid
from datetime import datetime

from core.database import get_db, Dataset, InsightResult
from api.routes.data import _read_csv_safe
from models.schemas import (
    InsightRequest,
    InsightResultOut,
    ColumnMapping,
)
from services.insights import (
    auto_detect_columns,
    apply_mapping,
    validate_mapping,
    generate_recommendations,
    ANALYSIS_MODES,
)
from core.auth import get_current_user, CurrentUser

router = APIRouter(prefix="/insights", tags=["insights"])


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _load_df(dataset_id: str, org_id: str, db: AsyncSession) -> pd.DataFrame:
    """Load a dataset CSV into a DataFrame, enforcing org access."""
    result = await db.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.org_id == org_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    return _read_csv_safe(Path(ds.filepath), nrows=None)


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/modes")
async def list_modes():
    """Return available analysis modes with descriptions."""
    return [
        {k: v for k, v in mode.items() if k != "fn"}
        for mode in ANALYSIS_MODES.values()
    ]


@router.get("/detect-columns/{dataset_id}")
async def detect_columns(
    dataset_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auto-detect column mapping for a dataset."""
    df = await _load_df(dataset_id, user.org_id, db)
    mapping = auto_detect_columns(df)
    return {"mapping": mapping, "columns": list(df.columns)}


@router.post("/analyze", response_model=InsightResultOut)
async def analyze(
    req: InsightRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run a structured analysis on a dataset."""
    # ── Usage check ──
    from core.billing import UsageTracker
    allowed, msg = await UsageTracker.check_limit(user.org_id, "insights", user.plan, db)
    if not allowed:
        raise HTTPException(429, msg)

    # Validate mode
    if req.mode not in ANALYSIS_MODES:
        raise HTTPException(400, f"Unknown mode: {req.mode}. Available: {list(ANALYSIS_MODES.keys())}")

    mode_info = ANALYSIS_MODES[req.mode]

    # Load data
    df = await _load_df(req.dataset_id, user.org_id, db)

    # Detect or use provided column mapping
    if req.column_mapping:
        mapping = req.column_mapping.model_dump(exclude_none=True)
    else:
        mapping = {k: v for k, v in auto_detect_columns(df).items() if v is not None}

    # Validate required columns
    errors = validate_mapping(mapping, req.mode)
    if errors:
        raise HTTPException(422, detail=errors)

    # Normalize DataFrame
    normalized = apply_mapping(df, mapping)

    # Run analysis
    result = mode_info["fn"](normalized)

    # Generate recommendations
    recs = generate_recommendations(
        mode=req.mode,
        metrics=result.get("metrics", []),
        findings=result.get("findings", []),
        problems=result.get("problems", []),
    )

    # Optional AI summary
    ai_summary = None
    if req.include_ai_summary and req.provider_config_id:
        ai_summary = await _generate_ai_summary(
            req.provider_config_id, mode_info["label"],
            result, recs, db,
        )

    waste = result.get("waste")
    opportunities = result.get("opportunities")

    # Persist
    insight_id = str(uuid.uuid4())
    record = InsightResult(
        id=insight_id,
        org_id=user.org_id,
        dataset_id=req.dataset_id,
        mode=req.mode,
        title=mode_info["label"],
        metrics=result.get("metrics", []),
        findings=result.get("findings", []),
        problems=result.get("problems", []),
        recommendations=[r for r in recs],
        column_mapping=mapping,
        ai_summary=ai_summary,
        waste=waste,
        opportunities=opportunities,
        created_at=datetime.utcnow(),
    )
    db.add(record)
    await UsageTracker.record_usage(user.org_id, "insights", 1, db)
    await db.commit()

    return InsightResultOut(
        id=insight_id,
        mode=req.mode,
        title=mode_info["label"],
        dataset_id=req.dataset_id,
        metrics=result.get("metrics", []),
        findings=result.get("findings", []),
        problems=result.get("problems", []),
        recommendations=recs,
        column_mapping=mapping,
        ai_summary=ai_summary,
        waste=waste,
        opportunities=opportunities,
        created_at=record.created_at,
    )


@router.get("/history")
async def insight_history(
    dataset_id: str = Query(None),
    mode: str = Query(None),
    limit: int = Query(20),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List past insight analyses."""
    q = select(InsightResult).where(InsightResult.org_id == user.org_id).order_by(InsightResult.created_at.desc()).limit(limit)
    if dataset_id:
        q = q.where(InsightResult.dataset_id == dataset_id)
    if mode:
        q = q.where(InsightResult.mode == mode)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        InsightResultOut(
            id=r.id,
            mode=r.mode,
            title=r.title,
            dataset_id=r.dataset_id,
            metrics=r.metrics or [],
            findings=r.findings or [],
            problems=r.problems or [],
            recommendations=r.recommendations or [],
            column_mapping=r.column_mapping or {},
            ai_summary=r.ai_summary,
            waste=r.waste,
            opportunities=r.opportunities,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/{insight_id}", response_model=InsightResultOut)
async def get_insight(
    insight_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single insight result."""
    result = await db.execute(
        select(InsightResult).where(InsightResult.id == insight_id, InsightResult.org_id == user.org_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Insight not found")
    return InsightResultOut(
        id=r.id,
        mode=r.mode,
        title=r.title,
        dataset_id=r.dataset_id,
        metrics=r.metrics or [],
        findings=r.findings or [],
        problems=r.problems or [],
        recommendations=r.recommendations or [],
        column_mapping=r.column_mapping or {},
        ai_summary=r.ai_summary,
        waste=r.waste,
        opportunities=r.opportunities,
        created_at=r.created_at,
    )


@router.delete("/{insight_id}")
async def delete_insight(
    insight_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an insight result."""
    result = await db.execute(
        select(InsightResult).where(InsightResult.id == insight_id, InsightResult.org_id == user.org_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Insight not found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


# ── AI Summary (optional) ──────────────────────────────────────────────────

async def _generate_ai_summary(
    provider_config_id: str,
    mode_label: str,
    result: dict,
    recs: list[dict],
    db: AsyncSession,
) -> str | None:
    """Use an LLM to generate a brief narrative summary. Returns None on failure."""
    try:
        from core.database import ProviderConfig
        from services.providers.registry import get_provider
        from services.providers.base import Message

        prov_result = await db.execute(
            select(ProviderConfig).where(ProviderConfig.id == provider_config_id)
        )
        config = prov_result.scalar_one_or_none()
        if not config:
            return None

        provider = get_provider(
            config.id, config.provider, config.model,
            config.auth_method, config.api_key_encrypted, config.session_file,
        )

        # Build context
        findings_text = "\n".join(f"- {f['text']}" for f in result.get("findings", []))
        problems_text = "\n".join(f"- [{p['severity']}] {p['text']}" for p in result.get("problems", []))
        recs_text = "\n".join(f"- {r['text']}" for r in recs)

        prompt = f"""You are a business analyst. Summarize this {mode_label} analysis in 2-3 short paragraphs for a shop owner. Be direct and actionable. No jargon.

FINDINGS:
{findings_text}

PROBLEMS:
{problems_text}

RECOMMENDATIONS:
{recs_text}

Write a brief executive summary:"""

        completion = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            system="You are a concise business analyst. Write plain language summaries for non-technical shop owners.",
            max_tokens=500,
            temperature=0.3,
        )
        return completion.text
    except Exception:
        return None
