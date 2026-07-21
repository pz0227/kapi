"""
Evaluation framework routes — benchmark scenarios, groundedness checks.

Patched in v1.4: adds the RIGOROUS eval endpoints on top of the original
keyword-hit-rate eval (kept for backward compatibility):

    GET  /eval/testset            -> the labeled test set (answerable / unanswerable / adversarial)
    POST /eval/run-rigorous       -> run the 3-axis eval, write JSON+MD report, return aggregate
    GET  /eval/reports            -> list saved run ids
    GET  /eval/report/{run_id}    -> fetch a saved run's full result.json

The rigorous path lives in services/eval/* (testset, gold, metrics,
failure_tags, runner, report). It computes ground truth from the sample CSVs,
scores three SEPARATE axes (lexical support / answer correctness / refusal
accuracy), and attributes each failure to the retriever or the model.
"""
import uuid
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, EvalResult, ProviderConfig, Dataset
from core.auth import get_current_user, CurrentUser
from models.schemas import EvalRunRequest, EvalResultOut, EvalSummary
from services.providers import get_provider
from services.providers.registry import get_fallback_provider
from services.providers.base import Message
from services.rag import retrieve, groundedness_score

router = APIRouter(prefix="/eval", tags=["eval"])

SCENARIOS_FILE = Path(__file__).parent.parent.parent / "data" / "eval_scenarios.json"


def _load_scenarios() -> list[dict]:
    if SCENARIOS_FILE.exists():
        return json.loads(SCENARIOS_FILE.read_text())
    return _default_scenarios()


def _default_scenarios() -> list[dict]:
    return [
        {"id": "s1", "question": "What is the monthly active user count?",
         "expected_keywords": ["MAU", "monthly", "active", "users"], "category": "kpi"},
        {"id": "s2", "question": "Which funnel step has the highest drop-off rate?",
         "expected_keywords": ["drop", "funnel", "step", "conversion"], "category": "funnel"},
    ]


def _keyword_hit_rate(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw.lower() in text_lower)
    return round(hits / len(keywords), 3)


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
            return fallback, None
        try:
            from services.providers.gateway_proxy_provider import GatewayProxyProvider
            gw = GatewayProxyProvider()
            if getattr(gw, "token", None):
                return gw, None
        except Exception:
            pass
        raise HTTPException(
            503,
            "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "environment variable, or configure a provider in Settings.",
        )
    return get_provider(pc.id, pc.provider, pc.model, pc.auth_method,
                        pc.api_key_encrypted, pc.session_file or ""), pc.id


@router.get("/scenarios")
async def list_scenarios():
    return _load_scenarios()


@router.post("/run", response_model=EvalSummary)
async def run_eval_legacy(
    body: EvalRunRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scenarios = _load_scenarios()
    if body.scenario_ids:
        scenarios = [s for s in scenarios if s["id"] in body.scenario_ids]
    if not scenarios:
        raise HTTPException(400, "No matching scenarios")

    provider, provider_config_id = await _get_provider(body.provider_config_id, db)
    SYSTEM = ("You are Kapi, an AI product analyst. Answer based on the provided "
              "data context. Be specific — use numbers. If data is unavailable, say so.")
    results: list[EvalResultOut] = []
    for scenario in scenarios:
        question = scenario["question"]
        expected_kws = scenario.get("expected_keywords", [])
        sources = retrieve(question, body.dataset_ids or [], top_k=6)
        context_str = ""
        if sources:
            from services.rag import format_context
            context_str = format_context(sources)
        user_msg = f"{context_str}\n\n---\n\nQuestion: {question}" if context_str else question
        try:
            result = await provider.complete(
                messages=[Message(role="user", content=user_msg)],
                system=SYSTEM, max_tokens=512, temperature=0.1)
            answer = result.text
        except Exception as exc:
            err = str(exc)
            if "401" in err or "authentication" in err.lower() or "invalid" in err.lower():
                raise HTTPException(401, "Provider authentication failed — check your API key.")
            answer = f"[Provider error: {err}]"
        g_score = groundedness_score(answer, sources)
        khr = _keyword_hit_rate(answer, expected_kws)
        eval_row = EvalResult(
            id=str(uuid.uuid4()), scenario_id=scenario["id"], question=question,
            expected_keywords=expected_kws, answer=answer, groundedness_score=g_score,
            keyword_hit_rate=khr, retrieval_count=len(sources),
            provider_config_id=provider_config_id)
        db.add(eval_row)
        results.append(EvalResultOut(
            id=eval_row.id, scenario_id=scenario["id"], question=question,
            expected_keywords=expected_kws, answer=answer, groundedness_score=g_score,
            keyword_hit_rate=khr, retrieval_count=len(sources), ran_at=eval_row.ran_at))
    await db.commit()
    avg_g = round(sum(r.groundedness_score for r in results) / len(results), 3)
    avg_k = round(sum(r.keyword_hit_rate for r in results) / len(results), 3)
    avg_r = round(sum(r.retrieval_count for r in results) / len(results), 1)
    return EvalSummary(total_scenarios=len(results), avg_groundedness=avg_g,
                       avg_keyword_hit_rate=avg_k, avg_retrieval_count=avg_r, results=results)


@router.get("/history", response_model=list[EvalResultOut])
async def eval_history(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(EvalResult).order_by(EvalResult.ran_at.desc()).limit(100))
    rows = result.scalars().all()
    return [EvalResultOut.model_validate(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# RIGOROUS EVAL (v1.4) — labeled test set, 3 separate axes, fault attribution
# ════════════════════════════════════════════════════════════════════════════

class RigorousRunRequest(BaseModel):
    top_k: int = 6
    limit: int = 0       # 0 = all cases
    judge: bool = False  # also run the same-model LLM-as-judge


_DATASET_KEYWORDS = {"users": "user", "events": "event",
                     "features": "feature", "orders": "order"}


@router.get("/testset")
async def get_testset():
    """The labeled cases (question, category, rubric, gold). Public — read-only."""
    from services.eval.testset import load_cases, testset_stats
    cases = load_cases()
    return {"stats": testset_stats(cases), "cases": [c.to_public() for c in cases]}


@router.post("/run-rigorous")
async def run_rigorous(
    body: RigorousRunRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the 3-axis eval against the live retriever + provider, write a
    JSON+MD report, and return the aggregate (axes + distributions + per-case)."""
    from services.eval.testset import load_cases
    from services.eval.runner import run_eval as _run
    from services.eval.report import build_report

    rows = (await db.execute(select(Dataset))).scalars().all()
    name_to_id = {r.name: r.id for r in rows}

    def resolve(logical: str) -> list[str]:
        kw = _DATASET_KEYWORDS.get(logical, "")
        return [i for n, i in name_to_id.items() if kw and kw in n.lower()]

    provider, _ = await _get_provider(None, db)
    cases = load_cases()
    if body.limit:
        cases = cases[:body.limit]

    judge_provider = provider if body.judge else None  # same-model judge
    agg = await _run(cases, provider, resolve, top_k=body.top_k,
                     judge_provider=judge_provider)
    meta = {"provider_label": "active", "model": getattr(provider, "model", "?"),
            "datasets": list(name_to_id.keys()), "testset_version": "1.0"}
    out = build_report(agg, meta)
    return {"run_id": out["run_id"], "axes": agg["axes"],
            "failure_distribution": agg["failure_distribution"],
            "fault_distribution": agg["fault_distribution"],
            "judge_calibration": agg.get("judge_calibration"),
            "provider_errors": agg["provider_errors"], "caveats": out["caveats"],
            "n_cases": agg["n_cases"], "results": agg["results"]}


@router.get("/reports")
async def list_reports():
    from services.eval.report import runs_dir
    d = runs_dir()
    runs = sorted([p.name for p in d.iterdir() if p.is_dir()], reverse=True)
    return {"runs": runs}


@router.get("/report/{run_id}")
async def get_report(run_id: str):
    from services.eval.report import runs_dir
    fp = runs_dir() / run_id / "result.json"
    if not fp.exists():
        raise HTTPException(404, "Report not found")
    return json.loads(fp.read_text(encoding="utf-8"))
