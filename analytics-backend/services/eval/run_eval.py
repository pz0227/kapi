"""
run_eval.py — single-command entrypoint for a full rigorous eval run.

USAGE (from the analytics-backend directory, with its Python):
    python -m services.eval.run_eval                # full run, all 32 cases
    python -m services.eval.run_eval --limit 6      # quick smoke (first 6 cases)
    python -m services.eval.run_eval --top-k 12     # change retrieval depth

It resolves the live datasets from the DB, acquires the active provider (with
the same gateway-trust fallback the chat/report routes use), runs every case
against the REAL retriever + REAL provider, and writes a JSON + Markdown report
under storage/eval_runs/<run_id>/. Nothing is mocked; if the provider is down,
the report says so per-case instead of inventing a pass.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from core.database import init_db, AsyncSessionLocal, Dataset, ProviderConfig
from services.providers import get_provider
from services.providers.registry import get_fallback_provider

from services.eval.testset import load_cases, testset_stats
from services.eval.runner import run_eval
from services.eval.report import build_report


# logical test-set dataset name -> substring to find the uploaded dataset name
_DATASET_KEYWORDS = {"users": "user", "events": "event",
                     "features": "feature", "orders": "order"}


async def acquire_provider(db):
    """Active ProviderConfig -> env fallback -> gateway-trust. Mirrors chat.py."""
    pc = (await db.execute(
        select(ProviderConfig).where(ProviderConfig.is_active == True)
    )).scalar_one_or_none()
    if pc:
        prov = get_provider(pc.id, pc.provider, pc.model, pc.auth_method,
                            pc.api_key_encrypted, pc.session_file or "")
        return prov, (pc.provider, pc.model)
    fb = get_fallback_provider()
    if fb:
        return fb, (getattr(fb, "provider_id", "fallback"), getattr(fb, "model", "?"))
    try:
        from services.providers.gateway_proxy_provider import GatewayProxyProvider
        gw = GatewayProxyProvider()
        if getattr(gw, "token", None):
            return gw, ("gateway", getattr(gw, "model", "gateway"))
    except Exception:
        pass
    raise RuntimeError("No provider available (no active config, env var, or gateway token).")


def build_resolver(name_to_id: dict[str, str]):
    def resolve(logical: str) -> list[str]:
        kw = _DATASET_KEYWORDS.get(logical, "")
        return [i for n, i in name_to_id.items() if kw and kw in n.lower()]
    return resolve


async def main_async(args):
    await init_db()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Dataset))).scalars().all()
        name_to_id = {r.name: r.id for r in rows}
        provider, (pname, pmodel) = await acquire_provider(db)

    cases = load_cases()
    if args.limit:
        cases = cases[:args.limit]
    # Same-model judge: reuse the same provider instance as the system under
    # test. Self-preference bias is disclosed and measured (judge_calibration).
    judge_provider = provider if args.judge else None
    print(f"[eval] {testset_stats(cases)}  provider={pname}/{pmodel}  top_k={args.top_k}"
          f"  judge={'on (same-model)' if args.judge else 'off'}")

    def progress(i, n, scored):
        tag = scored["failure"]["tag"] or "pass"
        err = " (provider_error)" if scored.get("provider_error") else ""
        jv = ""
        if "judge" in scored and not scored["judge"].get("error"):
            jv = f" judge={scored['judge'].get('overall')}"
        print(f"  [{i}/{n}] {scored['case_id']:24s} -> {'PASS' if scored['primary_pass'] else tag}{err}{jv}")

    # ── A/B mode: same provider, two retrieval depths ──
    if args.compare_topk:
        from services.eval.compare import run_arm, build_comparison, write_comparison
        a_k, b_k = (int(x) for x in args.compare_topk.split(","))
        resolver = build_resolver(name_to_id)
        print(f"[A/B] arm A top_k={a_k}  vs  arm B top_k={b_k}")
        agg_a = await run_arm(cases, resolver, {"provider": provider, "top_k": a_k,
                                                "judge_provider": judge_provider})
        agg_b = await run_arm(cases, resolver, {"provider": provider, "top_k": b_k,
                                                "judge_provider": judge_provider})
        cmp = build_comparison(agg_a, agg_b, f"top_k={a_k}", f"top_k={b_k}")
        out = write_comparison(cmp, {"datasets": list(name_to_id.keys()),
                                     "testset_version": "1.0"})
        print("A/B axes (Δ = B − A):")
        for k, v in cmp["axes"].items():
            print(f"  {k:26s} A={v['a']} B={v['b']} Δ={v['delta']}")
        print(f"  per-case disagreements: {len(cmp['disagreements'])}")
        print(f"\nComparison written:\n  {out['md_path']}\n  {out['json_path']}")
        return

    agg = await run_eval(cases, provider, build_resolver(name_to_id),
                         top_k=args.top_k, judge_provider=judge_provider, on_progress=progress)

    meta = {"provider_label": pname, "model": pmodel,
            "datasets": list(name_to_id.keys()), "testset_version": "1.0"}
    out = build_report(agg, meta)

    ax = agg["axes"]
    print("\n=== AXES (separate, not blended) ===")
    print(f"  competence (answerable correct): {ax['competence_answerable']}")
    print(f"  honesty (refusal accuracy):      {ax['honesty_refusal']}")
    print(f"  numeric accuracy:                {ax['numeric_accuracy']}")
    print(f"  label accuracy:                  {ax['label_accuracy']}")
    print(f"  lexical support (secondary):     {ax['lexical_support_avg']}")
    print(f"  failure modes:  {agg['failure_distribution']}")
    print(f"  fault owners:   {agg['fault_distribution']}")
    if agg.get("judge_calibration"):
        jc = agg["judge_calibration"]
        print(f"  judge vs deterministic: agreement={jc['agreement']} kappa={jc['cohens_kappa']} "
              f"(n={jc['n']}, {len(jc['disagreements'])} disagreements) — meta-metric on the same-model judge")
    if agg["provider_errors"]:
        print(f"  provider_errors: {len(agg['provider_errors'])} case(s) — LLM unavailable, NOT fabricated")
    print(f"\nReport written:\n  {out['md_path']}\n  {out['json_path']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    ap.add_argument("--top-k", type=int, default=6, help="retrieval depth")
    ap.add_argument("--judge", action="store_true",
                    help="also run the same-model LLM-as-judge (adds judge verdict + calibration)")
    ap.add_argument("--compare-topk", type=str, default="",
                    help="A/B two retrieval depths, e.g. '6,12' — writes a side-by-side comparison")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
