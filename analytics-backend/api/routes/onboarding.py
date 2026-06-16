"""
Onboarding API — first-time user experience and setup status.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.database import get_db, Dataset, ProviderConfig, ChatSession, Report
from core.config import get_settings
from core.auth import get_current_user, get_optional_user, CurrentUser

router = APIRouter(prefix="/onboarding", tags=["onboarding"])
settings = get_settings()


@router.get("/status")
async def onboarding_status(
    user: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return setup completion status for the onboarding checklist.
    Works for both authenticated and unauthenticated (local mode) users.
    """
    org_id = user.org_id if user else "local"

    # Count key resources
    dataset_count = (await db.execute(
        select(func.count()).select_from(Dataset).where(Dataset.org_id == org_id)
    )).scalar() or 0

    provider_count = (await db.execute(
        select(func.count()).select_from(ProviderConfig).where(ProviderConfig.org_id == org_id)
    )).scalar() or 0

    chat_count = (await db.execute(
        select(func.count()).select_from(ChatSession).where(ChatSession.org_id == org_id)
    )).scalar() or 0

    report_count = (await db.execute(
        select(func.count()).select_from(Report).where(Report.org_id == org_id)
    )).scalar() or 0

    # Build checklist
    steps = [
        {
            "id": "provider",
            "title": "Connect an AI provider",
            "description": "Add an OpenAI or Anthropic API key to power AI features.",
            "completed": provider_count > 0,
            "action_url": "/pa-dashboard",
        },
        {
            "id": "dataset",
            "title": "Upload your first dataset",
            "description": "Drop in a CSV file with your product events, users, or transactions.",
            "completed": dataset_count > 0,
            "action_url": "/pa-data",
        },
        {
            "id": "chat",
            "title": "Ask your AI analyst a question",
            "description": "Start a conversation about your data to get insights.",
            "completed": chat_count > 0,
            "action_url": "/pa-analyst",
        },
        {
            "id": "report",
            "title": "Generate your first report",
            "description": "Create a PRD, weekly review, or stakeholder update.",
            "completed": report_count > 0,
            "action_url": "/pa-reports",
        },
    ]

    completed = sum(1 for s in steps if s["completed"])
    total = len(steps)

    return {
        "completed": completed,
        "total": total,
        "progress_pct": round(completed / total * 100) if total > 0 else 0,
        "all_done": completed == total,
        "steps": steps,
        "auth_mode": settings.auth_mode,
        "has_stripe": bool(settings.stripe_secret_key),
    }
