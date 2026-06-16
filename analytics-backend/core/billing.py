"""
Stripe billing integration — subscriptions, usage tracking, and plan enforcement.

Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in your .env for production.
In local mode (KAPI_AUTH_MODE=local), all limits are bypassed.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from core.config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()


# ── Plan definitions ─────────────────────────────────────────────────────────

PLANS = {
    "free": {
        "label": "Free",
        "price_monthly": 0,
        "max_datasets": 2,
        "max_rows_per_dataset": 10_000,
        "max_ai_messages_per_month": 20,
        "max_reports_per_month": 3,
        "max_insights_per_month": 5,
        "max_users": 1,
        "features": [
            "2 datasets, 10K rows each",
            "20 AI messages/month",
            "3 reports/month",
            "Dashboard & basic analytics",
        ],
    },
    "starter": {
        "label": "Starter",
        "price_monthly": 29,
        "stripe_price_id": "",  # Set via STRIPE_STARTER_PRICE_ID env
        "max_datasets": 5,
        "max_rows_per_dataset": 50_000,
        "max_ai_messages_per_month": 200,
        "max_reports_per_month": 20,
        "max_insights_per_month": 50,
        "max_users": 1,
        "features": [
            "5 datasets, 50K rows each",
            "200 AI messages/month",
            "20 reports, 50 insights/month",
            "AI Analyst chat",
            "Funnel & retention analysis",
        ],
    },
    "pro": {
        "label": "Pro",
        "price_monthly": 79,
        "stripe_price_id": "",  # Set via STRIPE_PRO_PRICE_ID env
        "max_datasets": 15,
        "max_rows_per_dataset": 500_000,
        "max_ai_messages_per_month": 1000,
        "max_reports_per_month": -1,  # unlimited
        "max_insights_per_month": -1,
        "max_users": 5,
        "features": [
            "15 datasets, 500K rows each",
            "1,000 AI messages/month",
            "Unlimited reports & insights",
            "Up to 5 team members",
            "Eval suite & data export",
        ],
    },
    "team": {
        "label": "Team",
        "price_monthly": 149,
        "stripe_price_id": "",  # Set via STRIPE_TEAM_PRICE_ID env
        "max_datasets": 50,
        "max_rows_per_dataset": 2_000_000,
        "max_ai_messages_per_month": -1,  # unlimited
        "max_reports_per_month": -1,
        "max_insights_per_month": -1,
        "max_users": 20,
        "features": [
            "50 datasets, 2M rows each",
            "Unlimited AI messages",
            "Unlimited reports & insights",
            "Up to 20 team members",
            "Priority support",
        ],
    },
}


def _init_stripe_prices():
    """Populate Stripe price IDs from environment config."""
    PLANS["starter"]["stripe_price_id"] = settings.stripe_starter_price_id or ""
    PLANS["pro"]["stripe_price_id"] = settings.stripe_pro_price_id or ""
    PLANS["team"]["stripe_price_id"] = settings.stripe_team_price_id or ""

_init_stripe_prices()


def get_plan(plan_id: str) -> dict:
    """Get plan details by ID."""
    return PLANS.get(plan_id, PLANS["free"])


def get_plan_limit(plan_id: str, limit_key: str) -> int:
    """Get a specific limit for a plan. Returns -1 for unlimited."""
    plan = get_plan(plan_id)
    return plan.get(limit_key, 0)


# ── Usage tracking ───────────────────────────────────────────────────────────

class UsageTracker:
    """
    Track and enforce usage limits per org per billing period.
    Uses the Subscription and UsageRecord tables from the database.
    """

    @staticmethod
    async def get_subscription(org_id: str, db: AsyncSession) -> Optional[dict]:
        """Get the active subscription for an org."""
        from core.database import Subscription
        result = await db.execute(
            select(Subscription).where(
                Subscription.org_id == org_id,
                Subscription.status == "active",
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            return {
                "plan": sub.plan,
                "status": sub.status,
                "current_period_start": sub.current_period_start,
                "current_period_end": sub.current_period_end,
                "stripe_customer_id": sub.stripe_customer_id,
                "stripe_subscription_id": sub.stripe_subscription_id,
            }
        return None

    @staticmethod
    async def get_usage(org_id: str, metric: str, db: AsyncSession) -> int:
        """Get current period usage count for a metric."""
        from core.database import UsageRecord
        sub = await UsageTracker.get_subscription(org_id, db)
        period_start = sub["current_period_start"] if sub else datetime(2020, 1, 1)

        result = await db.execute(
            select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.metric == metric,
                UsageRecord.recorded_at >= period_start,
            )
        )
        records = result.scalars().all()
        return sum(r.count for r in records)

    @staticmethod
    async def record_usage(org_id: str, metric: str, count: int, db: AsyncSession) -> None:
        """Record usage of a metered resource."""
        from core.database import UsageRecord
        record = UsageRecord(
            id=str(uuid.uuid4()),
            org_id=org_id,
            metric=metric,
            count=count,
            recorded_at=datetime.utcnow(),
        )
        db.add(record)
        # Don't commit here — let the caller's transaction handle it

    @staticmethod
    async def check_limit(org_id: str, metric: str, plan: str, db: AsyncSession) -> tuple[bool, str]:
        """
        Check if an org is within limits for a metric.
        Returns (allowed, message).
        """
        limit_map = {
            "ai_messages": "max_ai_messages_per_month",
            "reports": "max_reports_per_month",
            "insights": "max_insights_per_month",
            "datasets": "max_datasets",
        }
        limit_key = limit_map.get(metric)
        if not limit_key:
            return True, ""

        max_allowed = get_plan_limit(plan, limit_key)
        if max_allowed == -1:
            return True, ""  # unlimited

        current = await UsageTracker.get_usage(org_id, metric, db)
        if current >= max_allowed:
            plan_info = get_plan(plan)
            return False, (
                f"You've reached your {plan_info['label']} plan limit of "
                f"{max_allowed} {metric.replace('_', ' ')} this month. "
                f"Upgrade your plan for more capacity."
            )
        return True, ""


# ── Stripe helpers ───────────────────────────────────────────────────────────

def get_stripe():
    """Lazy-load Stripe client."""
    try:
        import stripe
        stripe.api_key = settings.stripe_secret_key
        return stripe
    except ImportError:
        raise RuntimeError("stripe package not installed. Run: pip install stripe")


async def create_checkout_session(
    org_id: str,
    plan: str,
    success_url: str,
    cancel_url: str,
    customer_email: Optional[str] = None,
) -> str:
    """Create a Stripe Checkout session and return the URL."""
    stripe = get_stripe()
    plan_info = get_plan(plan)
    price_id = plan_info.get("stripe_price_id", "")

    if not price_id:
        raise ValueError(f"No Stripe price ID configured for plan '{plan}'")

    params = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"org_id": org_id, "plan": plan},
    }
    if customer_email:
        params["customer_email"] = customer_email

    session = stripe.checkout.Session.create(**params)
    return session.url


async def create_portal_session(stripe_customer_id: str, return_url: str) -> str:
    """Create a Stripe Customer Portal session for subscription management."""
    stripe = get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )
    return session.url


async def handle_webhook(payload: bytes, sig_header: str, db: AsyncSession) -> dict:
    """Process a Stripe webhook event."""
    from core.database import Subscription

    stripe = get_stripe()
    webhook_secret = settings.stripe_webhook_secret

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as exc:
        log.error("[billing] Webhook verification failed: %s", exc)
        raise ValueError(f"Webhook verification failed: {exc}")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        org_id = data.get("metadata", {}).get("org_id", "")
        plan = data.get("metadata", {}).get("plan", "starter")
        customer_id = data.get("customer", "")
        subscription_id = data.get("subscription", "")

        if org_id and subscription_id:
            # Deactivate any existing subscription
            await db.execute(
                update(Subscription)
                .where(Subscription.org_id == org_id)
                .values(status="canceled")
            )

            sub = Subscription(
                id=str(uuid.uuid4()),
                org_id=org_id,
                plan=plan,
                status="active",
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                current_period_start=datetime.utcnow(),
                current_period_end=datetime.utcnow(),  # will be updated by invoice.paid
                created_at=datetime.utcnow(),
            )
            db.add(sub)
            await db.commit()
            log.info("[billing] Subscription created: org=%s plan=%s", org_id, plan)

    elif event_type == "customer.subscription.updated":
        sub_id = data.get("id", "")
        result = await db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = data.get("status", sub.status)
            period = data.get("current_period_end")
            if period:
                sub.current_period_end = datetime.fromtimestamp(period)
            await db.commit()

    elif event_type in ("customer.subscription.deleted", "customer.subscription.canceled"):
        sub_id = data.get("id", "")
        result = await db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "canceled"
            await db.commit()
            log.info("[billing] Subscription canceled: org=%s", sub.org_id)

    return {"event": event_type, "handled": True}
