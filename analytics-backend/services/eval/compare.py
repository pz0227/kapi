"""
compare.py — A/B comparison (Phase 3). Run the SAME test set under two arms and
diff them.

WHY COMPARISON IS THE POINT OF EVALS:
  An absolute eval score is noisy — it drifts with test-set difficulty and judge
  quirks, so it's a weak thing to ship a decision on. A comparison runs both
  arms against the IDENTICAL cases and the SAME judge, which cancels those
  shared biases, leaving a robust relative signal: does A beat B on the axis I
  care about? The per-case disagreement list (where A and B flip) is the most
  actionable output — it shows exactly which questions a config change helped or
  hurt, not just that an average moved.

An "arm" is a config: {label, provider, top_k, temperature, system_prompt,
judge_provider}. Arms can differ in provider/model, retrieval depth, prompt,
or temperature.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .runner import run_eval, EVAL_SYSTEM
from .report import runs_dir


async def run_arm(cases, resolve, arm: dict, on_progress=None) -> dict:
    return await run_eval(
        cases, arm["provider"], resolve,
        top_k=arm.get("top_k", 6),
        system_prompt=arm.get("system_prompt", EVAL_SYSTEM),
        temperature=arm.get("temperature", 0.0),
        judge_provider=arm.get("judge_provider"),
        on_progress=on_progress,
    )


def compare_axes(a: dict, b: dict) -> dict:
    """Per-axis A vs B with deltas (B - A)."""
    out = {}
    for k in a:
        va, vb = a[k], b[k]
        delta = round(vb - va, 3) if (va is not None and vb is not None) else None
        out[k] = {"a": va, "b": vb, "delta": delta}
    return out


def per_case_diff(res_a: list[dict], res_b: list[dict]) -> list[dict]:
    """Cases where the two arms reached DIFFERENT pass/fail outcomes."""
    bidx = {r["case_id"]: r for r in res_b}
    diffs = []
    for ra in res_a:
        rb = bidx.get(ra["case_id"])
        if not rb:
            continue
        if ra["primary_pass"] != rb["primary_pass"]:
            diffs.append({
                "case_id": ra["case_id"], "category": ra.get("category", ""),
                "question": ra.get("question", ""),
                "a_pass": ra["primary_pass"], "b_pass": rb["primary_pass"],
                "a_tag": ra.get("failure", {}).get("tag"),
                "b_tag": rb.get("failure", {}).get("tag"),
            })
    return diffs


def build_comparison(agg_a: dict, agg_b: dict, label_a: str, label_b: str) -> dict:
    return {
        "arm_a": label_a, "arm_b": label_b,
        "config_a": agg_a.get("config", {}), "config_b": agg_b.get("config", {}),
        "axes": compare_axes(agg_a["axes"], agg_b["axes"]),
        "failure_a": agg_a["failure_distribution"],
        "failure_b": agg_b["failure_distribution"],
        "provider_errors_a": agg_a.get("provider_errors", []),
        "provider_errors_b": agg_b.get("provider_errors", []),
        "disagreements": per_case_diff(agg_a["results"], agg_b["results"]),
    }


def _pct(x):
    return "—" if x is None else f"{x*100:.1f}%"


def render_comparison_md(cmp: dict, meta: dict) -> str:
    L = [f"# Kapi Eval — A/B Comparison `{meta.get('run_id')}`", ""]
    L.append(f"- **Arm A:** {cmp['arm_a']}  ·  config={cmp['config_a']}")
    L.append(f"- **Arm B:** {cmp['arm_b']}  ·  config={cmp['config_b']}")
    L.append(f"- **Generated:** {meta.get('generated_at')}")
    if cmp["provider_errors_a"] or cmp["provider_errors_b"]:
        L.append(f"- ⚠️ provider errors — A:{len(cmp['provider_errors_a'])} "
                 f"B:{len(cmp['provider_errors_b'])} (those cases are not real comparisons)")
    L.append("")
    L.append("## Axes — A vs B (Δ = B − A)")
    L.append("")
    L.append("| Axis | A | B | Δ |")
    L.append("|---|---|---|---|")
    nice = {"competence_answerable": "Competence", "honesty_refusal": "Honesty",
            "numeric_accuracy": "Numeric acc", "label_accuracy": "Label acc",
            "lexical_support_avg": "Lexical support"}
    for k, v in cmp["axes"].items():
        d = v["delta"]
        darrow = "—" if d is None else (f"+{d}" if d > 0 else str(d))
        L.append(f"| {nice.get(k, k)} | {_pct(v['a'])} | {_pct(v['b'])} | {darrow} |")
    L.append("")
    L.append("> Read the Δ column, not the absolutes: both arms saw the same "
             "cases and the same judge, so the delta is the robust signal for "
             "choosing a config.")
    L.append("")
    L.append(f"## Per-case disagreements ({len(cmp['disagreements'])})")
    L.append("")
    if cmp["disagreements"]:
        L.append("| Case | Category | A | B | A tag | B tag |")
        L.append("|---|---|---|---|---|---|")
        for d in cmp["disagreements"]:
            L.append(f"| {d['case_id']} | {d['category']} | "
                     f"{'✓' if d['a_pass'] else '✗'} | {'✓' if d['b_pass'] else '✗'} | "
                     f"{d['a_tag'] or '—'} | {d['b_tag'] or '—'} |")
        L.append("")
        L.append("> These flips are where the config change actually mattered. "
                 "Aggregate deltas can cancel out; this list never does.")
    else:
        L.append("_No per-case disagreements — the two arms behaved identically on every case._")
    L.append("")
    return "\n".join(L)


def write_comparison(cmp: dict, meta: dict) -> dict:
    run_id = meta.get("run_id") or ("ab-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir = runs_dir().parent / "eval_compare" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {**meta, "run_id": run_id,
            "generated_at": datetime.now().isoformat(timespec="seconds")}
    (out_dir / "comparison.json").write_text(
        json.dumps({"meta": meta, **cmp}, indent=2, default=str), encoding="utf-8")
    md = render_comparison_md(cmp, meta)
    (out_dir / "comparison.md").write_text(md, encoding="utf-8")
    return {"run_id": run_id, "dir": str(out_dir),
            "json_path": str(out_dir / "comparison.json"),
            "md_path": str(out_dir / "comparison.md")}
