"""
AI analyst chat — grounded Q&A with RAG + streaming support.

Patched by kapi_app_ver_1.3:

The upstream chat router only exposes POST/GET on /chat/sessions, so a
user can create and list sessions but never rename or delete them. After
a few "+ New" clicks the sidebar fills up with empty "New analysis"
entries that there is no way to clean up from the UI. This patch adds:

    PUT    /chat/sessions/{id}   — rename a session (body: {"title": ...})
    DELETE /chat/sessions/{id}   — delete a session and all its messages

PUT (rather than PATCH) because the upstream CORS allow_methods list in
main.py is `[GET, POST, PUT, DELETE, OPTIONS]` — PATCH is not in it, and
expanding CORS for one endpoint would be a wider change than necessary.

Both endpoints are owner-only (org_id check) and validated against the
same `get_current_user` dependency as create_session, so they work in
local mode without any extra auth wiring.

The dashboard UI patch under `patches/control-ui/assets/kapi_session_menu.js`
calls these endpoints from a right-click context menu on each session
row. See README troubleshooting for details.
"""
import json
import logging
import uuid
from datetime import datetime
from typing import AsyncIterator

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.config import get_settings
from core.database import get_db, Dataset, ChatSession, ChatMessage, ProviderConfig
from models.schemas import (
    ChatRequest, ChatSessionCreate, ChatSessionOut, ChatMessageOut, Source
)
from core.auth import get_current_user, CurrentUser
from core.timing import StageTimer
from services.providers import get_provider
from services.providers.registry import get_fallback_provider
from services.providers.base import Message
from services.rag import retrieve, format_context, groundedness_score
from services.rag.numeric_grounding import numeric_groundedness
from services.analytics.aggregate_router import try_compute_answer
from services.analytics import compute_executive_summary, compute_kpis, compute_funnel, compute_retention, compute_feature_adoption, auto_detect_funnel
from api.routes.providers import update_provider_error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])
settings = get_settings()

ANALYST_SYSTEM = """You are Kapi, an expert AI Product Analyst assistant embedded in a product analytics platform.

Your role:
- Answer questions grounded in the uploaded product data
- Behave like a senior PM/analyst, not a generic chatbot
- Suggest hypotheses based on the data
- Recommend A/B tests and experiments when relevant
- Draft stakeholder updates, PRD ideas, and product recommendations
- Always cite specific numbers from the data when available
- If you don't have enough data to answer, say so clearly

Guidelines:
- Lead with the insight, not the process
- Be specific: use actual numbers, percentages, and trends
- When asked to "draft" or "write", produce polished, ready-to-use output
- Distinguish clearly between what the data shows and what is a hypothesis
- Write in clean, readable plain text: short paragraphs and, where useful, simple "- " bullets or "1." numbered lists. Do NOT use markdown markup symbols (no **bold**, *italics*, ## headings, or `backticks`) — they show up as literal characters here and hurt readability. For emphasis, just write the words plainly.

Context provided below comes from the user's uploaded datasets via semantic retrieval.
"""


# ── Patched: inline schema for rename ────────────────────────────────────────
# Defined here rather than in models/schemas.py so this single-file patch
# stays self-contained — no fork of schemas.py just for one field.

class _ChatSessionUpdate(BaseModel):
    title: str


async def _get_provider_from_config(config_id: str | None, db: AsyncSession):
    """Resolve provider config to a live provider instance."""
    if config_id:
        result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
        pc = result.scalar_one_or_none()
    else:
        result = await db.execute(select(ProviderConfig).where(ProviderConfig.is_active == True))
        pc = result.scalar_one_or_none()

    if not pc:
        # No DB record — try env-var fallback so users with OPENAI_API_KEY / etc. just work
        fallback = get_fallback_provider()
        if fallback:
            log.info("[chat] No DB provider config; using env-var fallback (%s)", fallback.provider_id)
            return fallback, None
        raise HTTPException(
            503,
            "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "environment variable, or configure a provider in Settings.",
        )

    return get_provider(
        config_id=pc.id,
        provider=pc.provider,
        model=pc.model,
        auth_method=pc.auth_method,
        api_key_encrypted=pc.api_key_encrypted,
        session_file=pc.session_file or "",
    ), pc


# ── Session management ───────────────────────────────────────────────────────

@router.post("/sessions", response_model=ChatSessionOut)
async def create_session(
    body: ChatSessionCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = ChatSession(
        id=str(uuid.uuid4()),
        org_id=user.org_id,
        title=body.title,
        provider_config_id=body.provider_config_id,
        dataset_ids=body.dataset_ids,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return ChatSessionOut.model_validate(session)


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.org_id == user.org_id)
        .order_by(ChatSession.updated_at.desc())
    )
    return [ChatSessionOut.model_validate(s) for s in result.scalars().all()]


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def get_messages(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    msgs = result.scalars().all()
    return [
        ChatMessageOut(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            sources=[Source(**s) for s in (m.sources or [])],
            token_count=m.token_count,
            created_at=m.created_at,
        )
        for m in msgs
    ]


# ── Patched: rename ───────────────────────────────────────────────────────────

@router.put("/sessions/{session_id}", response_model=ChatSessionOut)
async def update_session(
    session_id: str,
    body: _ChatSessionUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Rename a chat session. The dashboard's right-click context menu calls
    this. Owner-only — refuses to rename a session that belongs to a
    different org_id (relevant in cloud mode; harmless in local mode where
    org_id is the constant "local").
    """
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.org_id != user.org_id:
        raise HTTPException(403, "Not your session")

    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "Title cannot be empty")
    if len(title) > 120:
        title = title[:120]

    session.title = title
    session.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(session)
    return ChatSessionOut.model_validate(session)


# ── Patched: delete ──────────────────────────────────────────────────────────

@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a chat session and all its messages. Owner-only.

    We delete messages first via a bulk SQL DELETE rather than relying on
    ORM cascades — the upstream ChatSession model doesn't declare a
    relationship() with cascade='delete', and reproducing that here would
    bloat this single-file patch.
    """
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.org_id != user.org_id:
        raise HTTPException(403, "Not your session")

    await db.execute(
        ChatMessage.__table__.delete().where(ChatMessage.session_id == session_id)
    )
    await db.delete(session)
    await db.commit()
    return {"ok": True, "deleted": session_id}


# ── Main chat endpoint ────────────────────────────────────────────────────────

@router.post("/")
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Non-streaming chat completion with RAG grounding.
    Returns assistant message with sources.
    """
    # ── Usage check ──
    from core.billing import UsageTracker
    allowed, msg = await UsageTracker.check_limit(user.org_id, "ai_messages", user.plan, db)
    if not allowed:
        raise HTTPException(429, msg)

    # Get session
    sess_result = await db.execute(select(ChatSession).where(ChatSession.id == body.session_id))
    session = sess_result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    # Get provider
    try:
        provider, pc = await _get_provider_from_config(
            body.provider_config_id or session.provider_config_id, db
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.error("[chat] Provider resolution failed: %s", exc)
        raise HTTPException(503, str(exc))

    # Determine datasets to use
    dataset_ids = body.dataset_ids or session.dataset_ids or []

    # Per-stage latency instrumentation (grep "TIMING" in logs for p50/p90 analysis)
    timer = StageTimer("chat")

    # Retrieve relevant context
    with timer.stage("retrieve"):
        sources = retrieve(body.message, dataset_ids, top_k=settings.retrieval_top_k)
    with timer.stage("context_build"):
        context_str = format_context(sources)

    # Compute-first (Phase 2): aggregate questions get exact answers computed
    # over the FULL dataset. Additive to RAG context — a false positive only
    # adds a correct fact; it can never degrade the answer.
    if dataset_ids:
        with timer.stage("compute_first"):
            ds_result = await db.execute(select(Dataset).where(Dataset.id.in_(dataset_ids)))
            compute_blocks = []
            for ds in ds_result.scalars().all():
                b = try_compute_answer(body.message, ds.filepath, ds.filename)
                if b:
                    compute_blocks.append(b)
                    # Surface computed facts as a visible source, not just
                    # hidden prompt context: the user should SEE that this
                    # answer drew on exact full-dataset computation.
                    sources.append({
                        "dataset_id": ds.id,
                        "dataset_name": f"{ds.name} (computed, full dataset)",
                        "chunk_text": b,
                        "score": 1.0,
                    })
        if compute_blocks:
            context_str = "\n\n".join(compute_blocks) + (("\n\n" + context_str) if context_str else "")

    # Build conversation history
    history_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == body.session_id)
        .order_by(ChatMessage.created_at)
    )
    history = history_result.scalars().all()

    messages: list[Message] = []
    for h in history[-10:]:  # last 10 messages for context window efficiency
        if h.role in ("user", "assistant"):
            messages.append(Message(role=h.role, content=h.content))

    # Inject context into current user message
    user_content = body.message
    if context_str:
        user_content = f"{context_str}\n\n---\n\n**User question:** {body.message}"

    messages.append(Message(role="user", content=user_content))

    # Generate response
    provider_name = pc.provider if pc else provider.provider_id
    model_name = pc.model if pc else getattr(provider, "model", "unknown")
    log.info("[chat] provider=%s model=%s dataset_ids=%s", provider_name, model_name, dataset_ids)
    try:
        with timer.stage("llm_complete"):
            result = await provider.complete(
                messages=messages,
                system=ANALYST_SYSTEM,
                max_tokens=2048,
                temperature=0.2,
            )
    except Exception as exc:
        log.error("[chat] Provider completion failed (%s/%s): %s", provider_name, model_name, exc)
        # Map common SDK errors to helpful messages
        err_str = str(exc).lower()
        if "invalid api key" in err_str or "authentication" in err_str or "401" in err_str:
            if pc:
                await update_provider_error(db, pc.id, f"Auth failed: {exc}")
            raise HTTPException(
                401,
                f"Provider authentication failed ({provider_name}). "
                "Go to Settings and update your API key, or switch to a different provider."
            )
        if "missing scopes" in err_str or "insufficient permissions" in err_str:
            raise HTTPException(
                403,
                f"API key lacks required permissions ({provider_name}). "
                "Your API key may be restricted — GPT-5.4 requires api.responses.write scope. "
                "Create a new unrestricted key at platform.openai.com, or try a different model."
            )
        if "rate limit" in err_str or "429" in err_str:
            raise HTTPException(429, f"Rate limit hit ({provider_name}). Wait a moment and try again.")
        if "session" in err_str or "browser" in err_str:
            if pc:
                await update_provider_error(db, pc.id, f"Session error: {exc}")
            raise HTTPException(
                503,
                f"Browser session error: {exc}. "
                "Go to Settings → Browser Login to re-authenticate."
            )
        raise HTTPException(502, f"AI provider error ({provider_name}): {exc}")

    # Compute groundedness — lexical (are the words supported?) plus numeric
    # (are the NUMBERS supported?). For a data agent the numeric signal is the
    # one that catches the dangerous failure: a confident, fabricated figure.
    with timer.stage("groundedness"):
        g_score = groundedness_score(result.text, sources)
        grounding_text = " ".join(s["chunk_text"] for s in sources)
        num_grounding = numeric_groundedness(result.text, grounding_text)
    if num_grounding["ungrounded"]:
        log.warning(
            "[chat] ungrounded numbers in answer (possible fabrication): %s",
            num_grounding["ungrounded"],
        )
    timer.log()

    # Save user message
    user_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=body.session_id,
        role="user",
        content=body.message,
        sources=[],
        token_count=0,
    )
    db.add(user_msg)

    # Save assistant message
    source_dicts = [
        {"dataset_id": s["dataset_id"], "dataset_name": s["dataset_name"],
         "chunk_text": s["chunk_text"], "score": s["score"]}
        for s in sources
    ]
    asst_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=body.session_id,
        role="assistant",
        content=result.text,
        sources=source_dicts,
        token_count=result.input_tokens + result.output_tokens,
    )
    db.add(asst_msg)

    # Update session
    session.updated_at = datetime.utcnow()
    if session.title == "New conversation" and body.message:
        session.title = body.message[:60] + ("..." if len(body.message) > 60 else "")

    # Record usage
    await UsageTracker.record_usage(user.org_id, "ai_messages", 1, db)
    await db.commit()

    return {
        "message_id": asst_msg.id,
        "content": result.text,
        "sources": source_dicts,
        "groundedness_score": g_score,
        "numeric_groundedness": num_grounding,
        "model": result.model,
        "provider": result.provider,
        "tokens": {"input": result.input_tokens, "output": result.output_tokens},
    }


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Streaming chat completion. Returns SSE text/event-stream.
    """
    sess_result = await db.execute(select(ChatSession).where(ChatSession.id == body.session_id))
    session = sess_result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    provider, pc = await _get_provider_from_config(
        body.provider_config_id or session.provider_config_id, db
    )

    dataset_ids = body.dataset_ids or session.dataset_ids or []

    # Per-stage latency instrumentation. For streaming, time-to-first-token is
    # THE user-perceived latency metric — it's marked inside the generator.
    timer = StageTimer("chat_stream")
    with timer.stage("retrieve"):
        sources = retrieve(body.message, dataset_ids)
    with timer.stage("context_build"):
        context_str = format_context(sources)

    # Compute-first (Phase 2): see /chat — additive exact aggregates over the full dataset
    if dataset_ids:
        with timer.stage("compute_first"):
            ds_result = await db.execute(select(Dataset).where(Dataset.id.in_(dataset_ids)))
            compute_blocks = []
            for ds in ds_result.scalars().all():
                b = try_compute_answer(body.message, ds.filepath, ds.filename)
                if b:
                    compute_blocks.append(b)
                    # Surface computed facts as a visible source, not just
                    # hidden prompt context: the user should SEE that this
                    # answer drew on exact full-dataset computation.
                    sources.append({
                        "dataset_id": ds.id,
                        "dataset_name": f"{ds.name} (computed, full dataset)",
                        "chunk_text": b,
                        "score": 1.0,
                    })
        if compute_blocks:
            context_str = "\n\n".join(compute_blocks) + (("\n\n" + context_str) if context_str else "")

    history_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == body.session_id)
        .order_by(ChatMessage.created_at)
    )
    history = history_result.scalars().all()
    messages: list[Message] = []
    for h in history[-10:]:
        if h.role in ("user", "assistant"):
            messages.append(Message(role=h.role, content=h.content))

    user_content = body.message
    if context_str:
        user_content = f"{context_str}\n\n---\n\n**User question:** {body.message}"
    messages.append(Message(role="user", content=user_content))

    async def event_generator() -> AsyncIterator[str]:
        full_text = ""
        first_token_seen = False
        try:
            async for chunk in provider.stream(messages=messages, system=ANALYST_SYSTEM):
                if not first_token_seen:
                    first_token_seen = True
                    timer.mark("llm_first_token")
                full_text += chunk
                yield f"data: {chunk}\n\n"
            timer.mark("stream_done")
            timer.log()
        finally:
            # Persist messages after stream
            user_msg = ChatMessage(
                id=str(uuid.uuid4()), session_id=body.session_id,
                role="user", content=body.message, sources=[], token_count=0,
            )
            db.add(user_msg)
            source_dicts = [
                {"dataset_id": s["dataset_id"], "dataset_name": s["dataset_name"],
                 "chunk_text": s["chunk_text"], "score": s["score"]}
                for s in sources
            ]
            asst_msg = ChatMessage(
                id=str(uuid.uuid4()), session_id=body.session_id,
                role="assistant", content=full_text,
                sources=source_dicts, token_count=0,
            )
            db.add(asst_msg)
            session.updated_at = datetime.utcnow()
            await db.commit()
            # Flag fabricated numbers even on the streamed path (post-hoc):
            # the trust signal matters most exactly when we auto-computed facts.
            grounding_text = " ".join(s["chunk_text"] for s in sources)
            ng = numeric_groundedness(full_text, grounding_text)
            if ng["ungrounded"]:
                log.warning("[chat_stream] ungrounded numbers in answer: %s", ng["ungrounded"])
            yield f"data: {json.dumps({'numeric_groundedness': ng})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
