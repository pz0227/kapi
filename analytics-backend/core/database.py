from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, String, Text, Float, Integer, DateTime, JSON, Boolean
from datetime import datetime
from core.config import get_settings

settings = get_settings()

DATABASE_URL = f"sqlite+aiosqlite:///{settings.db_path}"

engine = create_async_engine(DATABASE_URL, echo=settings.debug)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


# ── ORM models ──────────────────────────────────────────────────────────────

class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(String, primary_key=True)
    org_id = Column(String, default="local", index=True)
    name = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    filepath = Column(String, nullable=False)
    dataset_type = Column(String, default="unknown")   # events, users, funnel, etc.
    row_count = Column(Integer, default=0)
    column_count = Column(Integer, default=0)
    schema_info = Column(JSON, default=dict)           # {col: dtype}
    tags = Column(JSON, default=list)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    indexed = Column(Boolean, default=False)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id = Column(String, primary_key=True)
    org_id = Column(String, default="local", index=True)
    provider = Column(String, nullable=False)           # anthropic | openai | openai_browser
    label = Column(String, nullable=False)
    model = Column(String, nullable=False)
    auth_method = Column(String, nullable=False)        # api_key | browser_session
    api_key_encrypted = Column(String, default="")
    session_file = Column(String, default="")
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    # Session expiry tracking (OpenClaw-style credential state)
    session_expires_at = Column(DateTime, nullable=True)   # when browser session JWT expires
    # Error state tracking (OpenClaw-style ProfileUsageStats)
    last_error_at = Column(DateTime, nullable=True)
    last_error_msg = Column(String, nullable=True)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)
    org_id = Column(String, default="local", index=True)
    title = Column(String, default="New conversation")
    provider_config_id = Column(String, nullable=True)
    dataset_ids = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=False)
    role = Column(String, nullable=False)               # user | assistant | system
    content = Column(Text, nullable=False)
    sources = Column(JSON, default=list)                # retrieved chunks
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Report(Base):
    __tablename__ = "reports"

    id = Column(String, primary_key=True)
    org_id = Column(String, default="local", index=True)
    report_type = Column(String, nullable=False)        # weekly_review | exec_brief | prd | experiment | feature_rec | feedback_synthesis
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    dataset_ids = Column(JSON, default=list)
    provider_config_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id = Column(String, primary_key=True)
    scenario_id = Column(String, nullable=False)
    question = Column(Text, nullable=False)
    expected_keywords = Column(JSON, default=list)
    answer = Column(Text, nullable=False)
    groundedness_score = Column(Float, default=0.0)
    keyword_hit_rate = Column(Float, default=0.0)
    retrieval_count = Column(Integer, default=0)
    provider_config_id = Column(String, nullable=True)
    ran_at = Column(DateTime, default=datetime.utcnow)


class InsightResult(Base):
    __tablename__ = "insight_results"

    id = Column(String, primary_key=True)
    org_id = Column(String, default="local", index=True)
    dataset_id = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    title = Column(String, nullable=False)
    metrics = Column(JSON, default=list)
    findings = Column(JSON, default=list)
    problems = Column(JSON, default=list)
    recommendations = Column(JSON, default=list)
    column_mapping = Column(JSON, default=dict)
    ai_summary = Column(Text, nullable=True)
    waste = Column(JSON, nullable=True)
    opportunities = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Billing tables ────────────────────────────────────────────────────────────

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String, primary_key=True)
    org_id = Column(String, nullable=False, index=True)
    plan = Column(String, nullable=False, default="free")  # free | starter | pro | team
    status = Column(String, nullable=False, default="active")  # active | canceled | past_due
    stripe_customer_id = Column(String, default="")
    stripe_subscription_id = Column(String, default="")
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id = Column(String, primary_key=True)
    org_id = Column(String, nullable=False, index=True)
    metric = Column(String, nullable=False)  # ai_messages | reports | insights | datasets
    count = Column(Integer, default=1)
    recorded_at = Column(DateTime, default=datetime.utcnow)


# ── Audit log ────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True)
    org_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False)
    action = Column(String, nullable=False)          # create | delete | export | login | upgrade
    resource_type = Column(String, nullable=False)   # dataset | report | insight | subscription
    resource_id = Column(String, default="")
    details = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def init_db() -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ── Migrations: add columns if they don't exist ──────────────────
        _migrations = [
            # Insight columns (v1.0.1)
            ("insight_results", "waste", "JSON"),
            ("insight_results", "opportunities", "JSON"),
            # Provider session/error tracking
            ("provider_configs", "session_expires_at", "DATETIME"),
            ("provider_configs", "last_error_at", "DATETIME"),
            ("provider_configs", "last_error_msg", "TEXT"),
            # Multi-tenancy: org_id on all user-facing tables
            ("datasets", "org_id", "TEXT DEFAULT 'local'"),
            ("provider_configs", "org_id", "TEXT DEFAULT 'local'"),
            ("chat_sessions", "org_id", "TEXT DEFAULT 'local'"),
            ("reports", "org_id", "TEXT DEFAULT 'local'"),
            ("insight_results", "org_id", "TEXT DEFAULT 'local'"),
        ]
        for table, col, col_type in _migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass  # column already exists — safe to ignore


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
