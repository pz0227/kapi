"""
report.py — turn an aggregate eval result into a JSON artifact and a
human-readable Markdown report.

DESIGN PRINCIPLES
  * Three axes are shown SEPARATELY (lexical support / answer correctness /
    refusal accuracy). There is deliberately NO single blended score, because
    blending hides the competence-vs-honesty tradeoff that is the whole point.
  * The failure table carries a FAULT-OWNER column (retriever vs model), so the
    report routes work to the right team instead of just grading.
  * The report DOCUMENTS ITS OWN LIMITATIONS inline. A self-documenting eval is
    more trustworthy than one that hides its weak spots: a reader can calibrate
    exactly how much to trust each number, and a skeptic can't ambush it with a
    limitation it failed to disclose.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def runs_dir() -> Path:
    d = Path(__file__).resolve().parents[2] / "storage" / "eval_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Static methodological caveats (always true of this harness) ──────────────
_STATIC_CAVEATS = [
    "Lexical support measures word-overlap with retrieved chunks, NOT factual "
    "correctness. An answer can be fully lexically supported and numerically "
    "wrong; treat it as a relevance signal only.",
    "Answer correctness for AGGREGATE gold (counts/means/rates/argmax) is "
    "checked against values computed from the source CSVs with pandas. The "
    "retriever-vs-model attribution treats aggregates as 'not in context' by "
    "design, because a computed rollup is not reliably present in any single "
    "row-level chunk — a literal token match (a metadata header, a coincidental "
    "row value, or an id like u_00500 whose digits read as 500) does NOT mean "
    "the model could derive the aggregate.",
    "The bare-integer-count false-positive (u_00500 -> 500) is mitigated by "
    "dropping numbers glued to identifier characters, but residual collisions "
    "(hyphenated ids/dates, coincidental row values) remain; the Phase-2 "
    "content-aware judge is what finally resolves these.",
    "Refusal detection is deterministic cue-matching. It excludes bare "
    "confidence hedges ('not sure') to avoid misreading a hedged hallucination "
    "as an honest refusal, but cue-matching can still false-positive; the "
    "Phase-2 judge re-scores the honesty axis.",
    "When the judge is added it runs on the SAME model under test, so it "
    "carries self-preference bias; judge-vs-deterministic and judge-vs-human "
    "agreement will be reported as a meta-metric.",
]


def _dynamic_caveats(agg: dict) -> list[str]:
    out: list[str] = []
    errs = agg.get("provider_errors") or []
    if errs:
        out.append(
            f"PROVIDER UNAVAILABLE for {len(errs)} case(s) "
            f"({', '.join(errs[:6])}{'…' if len(errs) > 6 else ''}). Their "
            "correctness/honesty axes reflect an empty answer, NOT model "
            "quality — re-run once the gateway is healthy for real numbers."
        )
    # cases that retrieved nothing at all (e.g. a dataset that isn't uploaded)
    zero_ret = [r["case_id"] for r in agg["results"] if r.get("retrieval_count") == 0]
    if zero_ret:
        out.append(
            f"{len(zero_ret)} case(s) retrieved ZERO chunks "
            f"({', '.join(zero_ret[:6])}{'…' if len(zero_ret) > 6 else ''}) — "
            "the matching dataset isn't uploaded, so these are retriever-side "
            "by construction, not model failures."
        )
    return out


def _pct(x) -> str:
    return "—" if x is None else f"{x*100:.1f}%"


def build_report(agg: dict, meta: dict) -> dict:
    """
    Write result.json + report.md for an aggregate result. Returns
    {"run_id", "json_path", "md_path", "caveats"}.
    """
    run_id = meta.get("run_id") or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = runs_dir() / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    caveats = _dynamic_caveats(agg) + _STATIC_CAVEATS
    doc = {"meta": {**meta, "run_id": run_id,
                    "generated_at": datetime.now().isoformat(timespec="seconds")},
           "axes": agg["axes"],
           "failure_distribution": agg["failure_distribution"],
           "fault_distribution": agg["fault_distribution"],
           "judge_calibration": agg.get("judge_calibration"),
           "provider_errors": agg.get("provider_errors", []),
           "config": agg.get("config", {}),
           "n_cases": agg["n_cases"],
           "caveats": caveats,
           "results": agg["results"]}

    json_path = out_dir / "result.json"
    json_path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    md_path = out_dir / "report.md"
    md_path.write_text(_render_markdown(doc), encoding="utf-8")

    return {"run_id": run_id, "json_path": str(json_path),
            "md_path": str(md_path), "caveats": caveats}


def _render_markdown(doc: dict) -> str:
    m, ax = doc["meta"], doc["axes"]
    cfg = doc.get("config", {})
    L: list[str] = []
    L.append(f"# Kapi Eval Report — `{m['run_id']}`")
    L.append("")
    L.append(f"- **Generated:** {m.get('generated_at')}")
    L.append(f"- **Provider / model:** {m.get('provider_label','?')} / {m.get('model','?')}")
    L.append(f"- **Config:** top_k={cfg.get('top_k')}, temperature={cfg.get('temperature')}")
    L.append(f"- **Datasets:** {m.get('datasets','?')}")
    L.append(f"- **Test set:** v{m.get('testset_version','?')} · {doc['n_cases']} cases")
    L.append("")
    L.append("## Headline — three independent axes (NOT a single blended score)")
    L.append("")
    L.append("| Axis | Value | What it measures |")
    L.append("|---|---|---|")
    L.append(f"| **Competence** (answerable correctness) | {_pct(ax['competence_answerable'])} | got answerable cases right vs gold |")
    L.append(f"| **Honesty** (refusal accuracy) | {_pct(ax['honesty_refusal'])} | correctly declined unanswerable / adversarial |")
    L.append(f"| Numeric accuracy (subset) | {_pct(ax['numeric_accuracy'])} | numeric answers within tolerance of computed gold |")
    L.append(f"| Label accuracy (subset) | {_pct(ax['label_accuracy'])} | categorical answers matched gold |")
    L.append(f"| Lexical support (secondary) | {_pct(ax['lexical_support_avg'])} | word-overlap with chunks — **NOT correctness** |")
    L.append("")
    L.append("> Competence and honesty are kept apart on purpose: a model can buy "
             "competence by answering everything, which *destroys* honesty. One "
             "blended number would hide that trade.")
    L.append("")
    L.append("## Failure-mode distribution (with fault owner)")
    L.append("")
    fd = doc["failure_distribution"]
    if fd:
        total_fail = sum(fd.values())
        L.append("| Failure mode | Count | % of failures | Typical fix owner |")
        L.append("|---|---|---|---|")
        owner = {"retrieval_miss": "retriever / architecture",
                 "hallucinated_fact": "model / prompt",
                 "incomplete_answer": "model / prompt",
                 "false_refusal": "model / prompt",
                 "missed_refusal": "model / refusal prompt",
                 "wrong_citation": "retrieval attribution"}
        for tag, cnt in sorted(fd.items(), key=lambda x: -x[1]):
            L.append(f"| `{tag}` | {cnt} | {cnt/total_fail*100:.0f}% | {owner.get(tag,'?')} |")
    else:
        L.append("_No failures recorded._")
    L.append("")
    L.append("### Fault attribution summary")
    faults = doc["fault_distribution"]
    if faults:
        tot = sum(faults.values())
        for f, c in sorted(faults.items(), key=lambda x: -x[1]):
            L.append(f"- **{f}**: {c} failure(s) ({c/tot*100:.0f}%)")
        L.append("")
        L.append("> This split is the actionable part: retriever-fault failures "
                 "need chunking/aggregation work; model-fault failures need "
                 "prompt/model work. They are owned by different people.")
    else:
        L.append("_No failures to attribute._")
    L.append("")
    jc = doc.get("judge_calibration")
    if jc:
        L.append("## Judge calibration (evaluate-the-evaluator)")
        L.append("")
        L.append(f"- Same-model LLM-as-judge vs deterministic metrics on {jc['n']} case(s): "
                 f"**agreement {_pct(jc['agreement'])}**, **Cohen's κ = {jc['cohens_kappa']}**.")
        L.append(f"- {len(jc['disagreements'])} disagreement(s)"
                 + (": " + ", ".join(d['case_id'] for d in jc['disagreements'][:8]) if jc['disagreements'] else "."))
        L.append("")
        L.append("> κ corrects for chance agreement. A same-model judge carries "
                 "self-preference bias, so this number — not the judge's verdicts "
                 "alone — is how much to trust it. Populate human labels to get the "
                 "judge-vs-human κ, the real gold standard.")
        L.append("")
    L.append("## Caveats & limitations (this report documents its own weak spots)")
    L.append("")
    for c in doc["caveats"]:
        L.append(f"- {c}")
    L.append("")
    L.append("## Per-case detail")
    L.append("")
    has_judge = any("judge" in r for r in doc["results"])
    jhdr = " Judge |" if has_judge else ""
    jsep = "---|" if has_judge else ""
    L.append(f"| Case | Category | Pass | Lexical | Correctness | Refused | Tag | Fault |{jhdr}")
    L.append(f"|---|---|---|---|---|---|---|---|{jsep}")
    for r in doc["results"]:
        corr = r.get("correctness", {})
        cstr = ("—" if not corr.get("applicable")
                else ("✓" if corr.get("passed") else "✗")
                + f" ({corr.get('matched')})" if corr.get("applicable") else "—")
        ref = r.get("refusal", {})
        rstr = "—" if not ref.get("applicable") else ("yes" if ref.get("refused") else "no")
        fail = r.get("failure", {})
        jcol = ""
        if has_judge:
            j = r.get("judge") or {}
            jcol = " " + (j.get("error", "")[:18] if j.get("error") else str(j.get("overall", "—"))) + " |"
        L.append(f"| {r['case_id']} | {r['category']} | "
                 f"{'✓' if r['primary_pass'] else '✗'} | {r['lexical_support']} | "
                 f"{cstr} | {rstr} | {fail.get('tag') or '—'} | {fail.get('fault') or '—'} |{jcol}")
    L.append("")
    return "\n".join(L)
