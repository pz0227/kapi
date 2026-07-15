"""
Compute-first router (Phase 2).

Problem: RAG retrieval only sees the first `index_max_rows` rows of a dataset
(Phase 1 made that limitation *disclosed*; this module makes it *not matter*
for the most common question class). When a user asks an aggregate question —
"what's the total revenue", "how many orders per region" — retrieved text
chunks are the wrong tool: the honest answer requires the FULL dataset.

Approach: detect aggregate intent with cheap heuristics, compute the answer
directly with pandas over the complete file, and inject the result into the
LLM context as an authoritative "COMPUTED FROM FULL DATASET" block.

Design rules:
1. ADDITIVE, never a replacement — RAG context still flows. A false-positive
   detection therefore only adds a correct fact to the prompt; it cannot make
   an answer worse. (Automate low-risk aggressively; keep the risky path
   conservative.)
2. Exact numbers only — everything in the block is computed, never estimated.
3. Fail silent — any error returns None and the chat path continues unchanged.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("kapi.compute")

# Aggregate-intent vocabulary. Deliberately conservative: these words rarely
# appear in lookup/exploratory questions that RAG already handles well.
_AGG_PATTERNS: dict[str, list[str]] = {
    "sum":    [r"\btotal\b", r"\bsum\b", r"\boverall\b", r"\bcombined\b", r"总共", r"总和", r"一共", r"合计"],
    "mean":   [r"\baverage\b", r"\bavg\b", r"\bmean\b", r"平均"],
    "count":  [r"\bhow many\b", r"\bcount\b", r"\bnumber of\b", r"多少(条|个|行|笔)", r"几(条|个|笔)"],
    "max":    [r"\bhighest\b", r"\bmax(imum)?\b", r"\blargest\b", r"\bbiggest\b", r"最高", r"最大"],
    "min":    [r"\blowest\b", r"\bmin(imum)?\b", r"\bsmallest\b", r"最低", r"最小"],
    "median": [r"\bmedian\b", r"中位"],
}

_GROUPBY_PATTERNS = [r"\bby\b", r"\bper\b", r"\bfor each\b", r"\bbreakdown\b", r"分别", r"各(个)?", r"按"]

_MAX_GROUPS_SHOWN = 12  # keep the injected block compact


def _detect_ops(question: str) -> list[str]:
    q = question.lower()
    ops = [op for op, pats in _AGG_PATTERNS.items() if any(re.search(p, q) for p in pats)]
    return ops


def _detect_groupby(question: str) -> bool:
    q = question.lower()
    return any(re.search(p, q) for p in _GROUPBY_PATTERNS)


def _match_columns(question: str, columns: list[str]) -> list[str]:
    """Columns whose name (or snake_case words) appear in the question."""
    q = question.lower()
    hits = []
    for col in columns:
        col_l = col.lower()
        words = [w for w in re.split(r"[_\s]+", col_l) if len(w) >= 3]
        if col_l in q or any(w in q for w in words):
            hits.append(col)
    return hits


def try_compute_answer(question: str, filepath: str, filename: str) -> str | None:
    """
    If `question` looks like an aggregate query, compute the answer over the
    FULL dataset and return a context block string. Otherwise return None.
    Never raises.
    """
    try:
        ops = _detect_ops(question)
        wants_groupby = _detect_groupby(question)
        if not ops and not wants_groupby:
            return None

        import pandas as pd

        path = Path(filepath)
        if not path.exists():
            return None
        # Full read — this is the whole point. CSV/TSV/JSON/XLSX mirror of the
        # upload readers, kept minimal on purpose.
        lower = filename.lower()
        if lower.endswith((".csv", ".tsv")):
            df = pd.read_csv(path, sep="\t" if lower.endswith(".tsv") else ",")
        elif lower.endswith(".json"):
            df = pd.read_json(path)
        elif lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(path)
        else:
            return None

        total_rows = len(df)
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        cat_cols = [c for c in df.columns if c not in numeric_cols]

        mentioned = _match_columns(question, df.columns.tolist())
        target_numeric = [c for c in mentioned if c in numeric_cols] or numeric_cols[:2]
        group_col = next((c for c in mentioned if c in cat_cols and df[c].nunique() <= 50), None)

        lines: list[str] = []

        # Row count is cheap and almost always useful for aggregate questions
        if "count" in ops or not ops:
            lines.append(f"row_count = {total_rows}")

        for col in target_numeric[:2]:
            series = df[col].dropna()
            if series.empty:
                continue
            for op in ops or []:
                if op == "count":
                    continue
                try:
                    val = getattr(series, op)()
                    lines.append(f"{op}({col}) = {round(float(val), 4)}")
                except Exception:
                    continue

        if wants_groupby and group_col:
            agg_col = target_numeric[0] if target_numeric else None
            op = next((o for o in ops if o != "count"), None)
            if agg_col and op:
                grouped = getattr(df.groupby(group_col)[agg_col], op)().sort_values(ascending=False)
                head = grouped.head(_MAX_GROUPS_SHOWN)
                pretty = ", ".join(f"{k}: {round(float(v), 2)}" for k, v in head.items())
                lines.append(f"{op}({agg_col}) by {group_col} = [{pretty}]")
            else:
                counts = df[group_col].value_counts().head(_MAX_GROUPS_SHOWN)
                pretty = ", ".join(f"{k}: {int(v)}" for k, v in counts.items())
                lines.append(f"count by {group_col} = [{pretty}]")

        if not lines:
            return None

        block = (
            f"**COMPUTED FROM FULL DATASET** ({filename}, all {total_rows} rows — "
            f"exact values, not retrieval-based):\n" + "\n".join(f"- {l}" for l in lines) +
            "\nPrefer these computed values over any figure derived from the retrieved sample rows."
        )
        log.info("COMPUTE hit: ops=%s groupby=%s cols=%s", ops, wants_groupby, mentioned)
        return block
    except Exception as exc:  # never break the chat path
        log.warning("compute-first router failed silently: %s", exc)
        return None
