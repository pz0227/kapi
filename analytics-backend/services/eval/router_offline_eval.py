"""
Offline evaluation of the compute-first router against the 51-case eval set.

Unlike the full eval (which needs a live LLM provider), this harness scores
ONLY the deterministic layer: for every answerable case whose gold value is
computable from data, does the router (a) fire, and (b) produce the exact
gold value in its computed block?

Why this matters: every case the router answers exactly is a case where the
final product answer no longer depends on LLM behavior at all. Raising router
coverage directly removes hallucination surface.

Run:
    .venv/bin/python -m services.eval.router_offline_eval
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from services.analytics.aggregate_router import try_compute_answer  # noqa: E402
from services.eval.gold import compute_gold  # noqa: E402

SAMPLES_DIR = BACKEND_ROOT / "data" / "samples"
TESTSET = BACKEND_ROOT / "data" / "eval_testset.json"

# Case dataset names → sample files. An unmapped dataset is reported loudly,
# never skipped silently (this very script shipped with a silent-skip bug that
# hid 12 of 31 cases — the exact failure class this project exists to prevent).
DATASET_FILES = {
    "users": "users.csv",
    "events": "events.csv",
    "features": "feature_usage.csv",
    "orders": "tiktok_shop_orders.csv",
}


def _numbers_in(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))]


def _is_correct(block: str, gold: dict) -> bool:
    value, kind = gold["value"], gold.get("kind", "num")
    tol = float(gold.get("tolerance", 0.0) or 0.0)
    if kind == "label":
        return value.lower() in block.lower()
    try:
        gv = float(value)
    except ValueError:
        return value.lower() in block.lower()
    return any(abs(n - gv) <= max(tol, 1e-9) for n in _numbers_in(block))


def main() -> int:
    data = json.loads(TESTSET.read_text())
    cases = data["cases"]
    gold = compute_gold(SAMPLES_DIR)

    answerable = [c for c in cases if c["category"] == "answerable"]
    other = [c for c in cases if c["category"] != "answerable"]

    covered, correct, misses, skipped = [], [], [], []
    for c in answerable:
        g = gold.get(c["id"])
        fname = DATASET_FILES.get(c["dataset"])
        ds_file = (SAMPLES_DIR / fname) if fname else None
        if g is None or ds_file is None or not ds_file.exists():
            skipped.append((c["id"], c["dataset"], "no gold" if g is None else "no sample file mapping"))
            continue
        block = try_compute_answer(c["question"], str(ds_file), ds_file.name)
        if block is None:
            misses.append((c["id"], c["question"], "router did not fire"))
            continue
        covered.append(c["id"])
        if _is_correct(block, g):
            correct.append(c["id"])
        else:
            misses.append((c["id"], c["question"], f"fired but wrong/absent (gold={g['value']})"))

    fired_on_other = []
    for c in other:
        fname = DATASET_FILES.get(c["dataset"])
        if not fname or not (SAMPLES_DIR / fname).exists():
            continue
        ds_file = SAMPLES_DIR / fname
        if try_compute_answer(c["question"], str(ds_file), ds_file.name):
            fired_on_other.append((c["id"], c["category"]))

    n = len(answerable) - len(skipped)
    print("=" * 72)
    print(f"ROUTER OFFLINE EVAL  (deterministic layer only, no LLM)")
    print(f"Answerable cases with computable gold : {n}")
    print(f"Router fired (coverage)               : {len(covered)}/{n}  ({100*len(covered)/max(n,1):.0f}%)")
    print(f"Exact gold value in computed block    : {len(correct)}/{n}  ({100*len(correct)/max(n,1):.0f}%)")
    print(f"Fired on unanswerable/adversarial     : {len(fired_on_other)}/{len(other)}  (target 0 — silence beats plausible-but-wrong)")
    print("-" * 72)
    if misses:
        print("MISSES:")
        for cid, q, why in misses:
            print(f"  [{cid}] {q!r}\n      -> {why}")
    if skipped:
        print("SKIPPED (not scored — fix the mapping or gold!):")
        for cid, ds, why in skipped:
            print(f"  [{cid}] dataset={ds}: {why}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
