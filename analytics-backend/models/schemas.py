from pydantic import BaseModel, Field
from typing import Any, Optional
from datetime import datetime
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


# ── Dataset ──────────────────────────────────────────────────────────────────

class DatasetOut(BaseModel):
    id: str
    name: str
    filename: str
    dataset_type: str
    row_count: int
    column_count: int
    schema_info: dict[str, str]
    tags: list[str]
    uploaded_at: datetime
    indexed: bool
    # Rows actually searchable via RAG (min(row_count, settings.index_max_rows)).
    # Lets the UI say "indexed 200 of 8,431 rows" instead of implying full coverage.
    indexed_rows: int | None = None

    model_config = {"from_attributes": True}


class DatasetPreview(BaseModel):
    id: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    schema_info: dict[str, str]
    detected_type: str
    sample_stats: dict[str, Any]


# ── Provider ──────────────────────────────────────────────────────────────────

class ProviderConfigCreate(BaseModel):
    provider: str                           # anthropic | openai | openai_browser
    label: str
    model: str
    auth_method: str                        # api_key | browser_session
    api_key: Optional[str] = None
    make_active: bool = True


class ProviderConfigOut(BaseModel):
    id: str
    provider: str
    label: str
    model: str
    auth_method: str
    is_active: bool
    has_api_key: bool
    has_session: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    # OpenClaw-style credential state fields
    session_expires_at: Optional[datetime] = None
    session_status: Optional[str] = None   # "valid" | "expiring_soon" | "expired" | "no_session" | "unknown"
    last_error_at: Optional[datetime] = None
    last_error_msg: Optional[str] = None

    model_config = {"from_attributes": True}


class ProviderTestResult(BaseModel):
    success: bool
    message: str
    latency_ms: Optional[float] = None


class BrowserAuthStatus(BaseModel):
    provider_config_id: str
    status: str          # pending | waiting_token | authenticated | failed
    message: str
    js_snippet: Optional[str] = None    # JS the user runs in browser console to get token


class QuickConnectRequest(BaseModel):
    provider: str           # anthropic | openai
    api_key: str
    model: Optional[str] = None   # uses catalogue default if omitted


class TokenSubmitRequest(BaseModel):
    token: str              # access token pasted by user from browser console


# ── Chat ────────────────────────────────────────────────────────────────────

class ChatSessionCreate(BaseModel):
    title: str = "New conversation"
    provider_config_id: Optional[str] = None
    dataset_ids: list[str] = Field(default_factory=list)


class ChatSessionOut(BaseModel):
    id: str
    title: str
    provider_config_id: Optional[str]
    dataset_ids: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class Source(BaseModel):
    dataset_id: str
    dataset_name: str
    chunk_text: str
    score: float


class ChatMessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    sources: list[Source]
    token_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str
    session_id: str
    provider_config_id: Optional[str] = None
    dataset_ids: list[str] = Field(default_factory=list)
    stream: bool = False


# ── Analytics ────────────────────────────────────────────────────────────────

class KPICard(BaseModel):
    label: str
    value: Any
    unit: str = ""
    delta: Optional[float] = None
    delta_label: str = ""
    trend: list[dict[str, Any]] = Field(default_factory=list)


class FunnelStep(BaseModel):
    step: str
    count: int
    conversion_rate: float      # rate from previous step
    absolute_rate: float        # rate from first step
    drop_off: int


class FunnelResult(BaseModel):
    steps: list[FunnelStep]
    overall_conversion: float
    biggest_drop_step: str


class RetentionRow(BaseModel):
    cohort: str
    cohort_size: int
    periods: list[Optional[float]]


class RetentionResult(BaseModel):
    periods: list[str]
    rows: list[RetentionRow]
    avg_day1: Optional[float]
    avg_day7: Optional[float]
    avg_day30: Optional[float]


class SegmentRow(BaseModel):
    segment: str
    count: int
    pct: float
    metrics: dict[str, Any]


class FeatureAdoptionRow(BaseModel):
    feature: str
    adopters: int
    adoption_rate: float
    avg_uses_per_user: float
    trend: str   # up | down | stable


class AnomalyPoint(BaseModel):
    date: str
    metric: str
    value: float
    expected: float
    z_score: float
    is_anomaly: bool


# ── Reports ──────────────────────────────────────────────────────────────────

class ReportRequest(BaseModel):
    report_type: str          # weekly_review | exec_brief | prd | experiment | feature_rec | feedback_synthesis
    title: Optional[str] = None
    dataset_ids: list[str] = Field(default_factory=list)
    provider_config_id: Optional[str] = None
    extra_context: str = ""


class ReportOut(BaseModel):
    id: str
    report_type: str
    title: str
    content: str
    dataset_ids: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Eval ─────────────────────────────────────────────────────────────────────

class EvalRunRequest(BaseModel):
    scenario_ids: list[str] = Field(default_factory=list)   # empty = all
    provider_config_id: Optional[str] = None
    dataset_ids: list[str] = Field(default_factory=list)


class EvalResultOut(BaseModel):
    id: str
    scenario_id: str
    question: str
    expected_keywords: list[str]
    answer: str
    groundedness_score: float
    keyword_hit_rate: float
    retrieval_count: int
    ran_at: datetime

    model_config = {"from_attributes": True}


class EvalSummary(BaseModel):
    total_scenarios: int
    avg_groundedness: float
    avg_keyword_hit_rate: float
    avg_retrieval_count: float
    results: list[EvalResultOut]


# ── Insights (Business Analytics) ───────────────────────────────────────────

class InsightMetricCard(BaseModel):
    label: str
    value: Any
    unit: str = ""
    delta: Optional[float] = None
    delta_label: str = ""
    severity: str = "neutral"           # good | warning | danger | neutral


class InsightFinding(BaseModel):
    text: str
    category: str = ""
    severity: str = "neutral"           # positive | neutral


class InsightProblem(BaseModel):
    text: str
    severity: str = "medium"            # critical | high | medium | low
    metric_name: Optional[str] = None
    current_value: Optional[float] = None
    threshold: Optional[float] = None


class InsightRecommendation(BaseModel):
    text: str
    priority: int = 3
    impact: str = "medium"              # high | medium | low
    effort: str = "medium"              # low | medium | high
    category: str = ""


class ColumnMapping(BaseModel):
    order_id: Optional[str] = None
    product_name: Optional[str] = None
    revenue: Optional[str] = None
    quantity: Optional[str] = None
    date: Optional[str] = None
    customer_id: Optional[str] = None
    category: Optional[str] = None
    price: Optional[str] = None
    cost: Optional[str] = None
    discount: Optional[str] = None
    status: Optional[str] = None


class InsightRequest(BaseModel):
    dataset_id: str
    mode: str                           # shop_health | product_performance | trend_spotter
    column_mapping: Optional[ColumnMapping] = None
    include_ai_summary: bool = False
    provider_config_id: Optional[str] = None


class InsightResultOut(BaseModel):
    id: str
    mode: str
    title: str
    dataset_id: str
    metrics: list[InsightMetricCard]
    findings: list[InsightFinding]
    problems: list[InsightProblem]
    recommendations: list[InsightRecommendation]
    column_mapping: dict[str, Any]
    ai_summary: Optional[str] = None
    waste: Optional[dict[str, Any]] = None
    opportunities: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}
