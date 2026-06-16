"""
Billing API — Stripe checkout, subscription management, usage tracking.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.auth import get_current_user, CurrentUser
from core.billing import (
    PLANS,
    get_plan,
    UsageTracker,
    create_checkout_session,
    create_portal_session,
    handle_webhook,
)

log = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans")
async def list_plans():
    """Return available pricing plans (shape matches PAPlan in the UI)."""
    return [
        {
            "id": plan_id,
            "name": plan["label"],
            "price": plan["price_monthly"],
            "stripe_price_id": plan.get("stripe_price_id"),
            "limits": {
                "max_datasets": plan["max_datasets"],
                "max_ai_messages_per_month": plan["max_ai_messages_per_month"],
                "max_reports_per_month": plan["max_reports_per_month"],
                "max_insights_per_month": plan["max_insights_per_month"],
                "max_users": plan["max_users"],
                "max_rows_per_dataset": plan["max_rows_per_dataset"],
            },
            "features": plan["features"],
        }
        for plan_id, plan in PLANS.items()
    ]


@router.get("/subscription")
async def get_subscription(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current subscription status and usage for the authenticated org."""
    sub = await UsageTracker.get_subscription(user.org_id, db)
    plan_id = sub["plan"] if sub else "free"
    plan_info = get_plan(plan_id)

    # Get usage counts
    ai_usage = await UsageTracker.get_usage(user.org_id, "ai_messages", db)
    report_usage = await UsageTracker.get_usage(user.org_id, "reports", db)
    insight_usage = await UsageTracker.get_usage(user.org_id, "insights", db)

    # Build usage array matching PAUsageMetric in the UI
    def _meter(metric: str, used: int, limit: int) -> dict:
        return {"metric": metric, "used": used, "limit": limit, "remaining": max(0, limit - used)}

    usage_meters = [
        _meter("datasets", 0, plan_info["max_datasets"]),  # TODO: count actual datasets
        _meter("ai_messages", ai_usage, plan_info["max_ai_messages_per_month"]),
        _meter("reports", report_usage, plan_info["max_reports_per_month"]),
        _meter("insights", insight_usage, plan_info["max_insights_per_month"]),
    ]

    return {
        "plan": plan_id,
        "status": sub["status"] if sub else "active",
        "current_period_end": sub["current_period_end"] if sub else None,
        "usage": usage_meters,
    }


@router.post("/checkout")
async def create_checkout(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Create a Stripe Checkout session for upgrading.
    Body: { "price_id": "price_..." }
    """
    body = await request.json()
    price_id = body.get("price_id", "")

    if not price_id:
        raise HTTPException(400, "Missing price_id in request body")

    # Resolve which plan this price_id belongs to
    plan_key = None
    for pid, pinfo in PLANS.items():
        if pinfo.get("stripe_price_id") == price_id:
            plan_key = pid
            break

    if not plan_key or plan_key == "free":
        raise HTTPException(400, "Invalid price_id. No matching paid plan found.")

    if not settings.stripe_secret_key:
        raise HTTPException(503, "Stripe is not configured. Contact support.")

    try:
        url = await create_checkout_session(
            org_id=user.org_id,
            plan=plan_key,
            success_url=f"{settings.app_url}/pa-billing?billing=success",
            cancel_url=f"{settings.app_url}/pa-billing?billing=canceled",
            customer_email=user.email or None,
        )
        return {"checkout_url": url}
    except Exception as exc:
        log.error("[billing] Checkout failed: %s", exc)
        raise HTTPException(502, f"Failed to create checkout: {exc}")


@router.post("/portal")
async def customer_portal(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Redirect to Stripe Customer Portal for subscription management."""
    sub = await UsageTracker.get_subscription(user.org_id, db)
    if not sub or not sub.get("stripe_customer_id"):
        raise HTTPException(404, "No active subscription found.")

    try:
        url = await create_portal_session(
            stripe_customer_id=sub["stripe_customer_id"],
            return_url=f"{settings.app_url}/#/settings",
        )
        return {"portal_url": url}
    except Exception as exc:
        log.error("[billing] Portal session failed: %s", exc)
        raise HTTPException(502, f"Failed to create portal session: {exc}")


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Stripe webhook endpoint — receives subscription lifecycle events.
    This endpoint does NOT require auth (Stripe calls it directly).
    """
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")

    if not sig:
        raise HTTPException(400, "Missing Stripe-Signature header")

    try:
        result = await handle_webhook(payload, sig, db)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
