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
    "max":    [r"\bhighest\b", r"\bmax(imum)?\b", r"\blargest\b", r"\bbiggest\b", r"\blongest\b", r"最高", r"最大", r"最长"],
    "min":    [r"\blowest\b", r"\bmin(imum)?\b", r"\bsmallest\b", r"\bshortest\b", r"最低", r"最小", r"最短"],
    "median": [r"\bmedian\b", r"中位"],
}

_GROUPBY_PATTERNS = [r"\bby\b", r"\bper\b", r"\bfor each\b", r"\bbreakdown\b", r"分别", r"各(个)?", r"按"]

# Category-level questions: "which country has the most users", "most common
# referral source", "second most frequent event type", "fewest users".
_TOP_LABEL_PATTERNS = [
    r"\bwhich\b.{0,60}\b(most|highest|largest|top|fewest|least|lowest|longest|shortest)\b",
    r"\bmost (common|frequent|popular)\b",
    r"\b(second|2nd)[- ](most|largest|biggest|highest)\b",
    r"最(常见|多|少|热门)",
]
_LEAST_PATTERNS = [r"\bfewest\b", r"\bleast\b", r"\blowest\b", r"\bshortest\b", r"最少", r"最短"]
_SECOND_PATTERNS = [r"\b(second|2nd)[- ](most|largest|biggest|highest)\b", r"second-highest", r"第二"]

# Question-vocabulary → column-vocabulary bridges: users say "revenue" and
# "order value"; schemas say total_amount / gmv / price.
_COLUMN_SYNONYMS = {
    "value":   {"amount", "total"},
    "revenue": {"amount", "total", "gmv", "sales", "price"},
    "spend":   {"amount", "cost", "total"},
    "sales":   {"amount", "total", "gmv"},
}

# Share/percentage questions: "what percentage of users are on the pro plan".
_SHARE_PATTERNS = [r"\bpercentage\b", r"\bpercent\b", r"\bshare of\b", r"\bproportion\b", r"占比", r"百分比", r"比例"]

# Distinct-count questions: "how many distinct countries".
_NUNIQUE_PATTERNS = [r"\bdistinct\b", r"\bunique\b", r"\bhow many different\b", r"多少种", r"几种"]

# Time-scoped questions ("revenue in Q3 2024", "last month") — the router has
# no date filtering, so an all-time aggregate would silently answer a
# different question than the one asked. Refusing to fire is the honest move.
_TIME_SCOPE_PATTERN = re.compile(
    r"\b(19|20)\d{2}\b|\bq[1-4]\b"
    r"|\b(last|past|previous|this|next)\s+(week|month|quarter|year|day)\b"
    r"|\b(january|february|march|april|may|june|july|august|september|october|november|december)\b"
    r"|\byesterday\b|\btoday\b|去年|上(个)?月|本月|今年|季度"
)

# Generic count nouns that any tabular dataset can answer ("how many rows").
_GENERIC_COUNT_NOUNS = {"row", "rows", "record", "records", "entry", "entries",
                        "item", "items", "data", "datapoints", "points", "line", "lines"}


def _count_noun_matches_data(q_lower: str, filename: str, columns: list[str], df) -> bool:
    """
    For "how many X" questions, check that X actually names something in this
    dataset (filename, a column, or a categorical value). 'How many support
    tickets' against a users table must NOT answer row_count = 500.
    """
    m = re.search(r"(?:how many|number of)\s+([a-z_][a-z_ \-]{1,40}?)(?:\s+(?:are|is|do|did|does|were|was|have|has|in|on|for|per|by|with|from)\b|\?|$)", q_lower)
    if not m:
        return True  # no extractable noun — stay permissive
    noun_words = {w for w in re.split(r"[\s\-_]+", m.group(1)) if len(w) >= 2}
    if noun_words & _GENERIC_COUNT_NOUNS:
        return True
    vocab: set[str] = set()
    for w in re.split(r"[\s\-_\.]+", filename.lower()):
        vocab |= _word_variants(w)
    for col in columns:
        for w in re.split(r"[_\s]+", col.lower()):
            vocab |= _word_variants(w)
    try:
        for col in columns:
            s = df[col]
            if s.dtype == object and s.nunique() <= 50:
                for v in s.dropna().unique():
                    if isinstance(v, str):
                        vocab |= _word_variants(v.lower())
    except Exception:
        pass
    return bool(noun_words & vocab)

_MAX_GROUPS_SHOWN = 12  # keep the injected block compact

# Full-file reads are the router's whole point, but doing one per chat message
# is wasteful (and unacceptable on big files). Cache parsed frames keyed on
# (path, mtime, size) so an unchanged file is read once; refuse files beyond
# a size ceiling instead of silently making every message slow.
_DF_CACHE: dict[tuple, "object"] = {}
_DF_CACHE_MAX = 8
_MAX_FILE_MB = 64


def _load_df(path: Path, filename: str):
    """Read the full dataset with caching. Returns None when unsupported or too big.

    Cached frames must be treated as READ-ONLY by callers (filtering via
    boolean indexing creates new frames, which is what try_compute_answer does).
    """
    import pandas as pd

    st = path.stat()
    if st.st_size > _MAX_FILE_MB * 1024 * 1024:
        log.info("COMPUTE skipped: %s is %.0f MB (> %d MB ceiling)",
                 filename, st.st_size / 1024 / 1024, _MAX_FILE_MB)
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _DF_CACHE:
        return _DF_CACHE[key]

    lower = filename.lower()
    if lower.endswith((".csv", ".tsv")):
        df = pd.read_csv(path, sep="\t" if lower.endswith(".tsv") else ",")
    elif lower.endswith(".json"):
        df = pd.read_json(path)
    elif lower.endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        return None

    # Drop stale entries for the same path, then evict oldest if still full.
    for k in [k for k in _DF_CACHE if k[0] == str(path)]:
        _DF_CACHE.pop(k, None)
    while len(_DF_CACHE) >= _DF_CACHE_MAX:
        _DF_CACHE.pop(next(iter(_DF_CACHE)))
    _DF_CACHE[key] = df
    return df


def _detect_ops(question: str) -> list[str]:
    q = question.lower()
    ops = [op for op, pats in _AGG_PATTERNS.items() if any(re.search(p, q) for p in pats)]
    # Homonym guard: "what does X mean" / "the meaning of X" asks for a
    # definition, not an average. Only drop the op when no other averaging
    # vocabulary is present.
    if "mean" in ops and not re.search(r"\baverage\b|\bavg\b|平均", q):
        if re.search(r"what (does|do|did|is)\b.{0,60}\bmean\b|\bmeaning\b", q):
            ops.remove("mean")
    return ops


def _detect_groupby(question: str) -> bool:
    q = question.lower()
    return any(re.search(p, q) for p in _GROUPBY_PATTERNS)


def _word_variants(w: str) -> set[str]:
    """Simple singular/plural variants: country -> countries, event -> events."""
    out = {w, w + "s", w + "es"}
    if w.endswith("y"):
        out.add(w[:-1] + "ies")
    return out


def _match_columns(question: str, columns: list[str]) -> list[str]:
    """Columns whose name (or snake_case words, incl. plural forms) appear in the question."""
    q = question.lower()
    # Vocabulary bridges active for this question ("revenue" → amount/gmv/...)
    syn_targets: set[str] = set()
    for key, cands in _COLUMN_SYNONYMS.items():
        if re.search(rf"\b{key}\b", q):
            syn_targets |= cands
    hits = []
    for col in columns:
        col_l = col.lower()
        words = [w for w in re.split(r"[_\s]+", col_l) if len(w) >= 3]
        variants = set().union(*(_word_variants(w) for w in words)) if words else set()
        if col_l in q or any(v in q for v in variants) or any(w in syn_targets for w in words):
            hits.append(col)
    return hits


# English function words that collide with short categorical codes ('IN' the
# country vs 'in' the preposition). Values equal to these are never treated
# as filters.
_FILTER_STOPWORDS = {
    "in", "on", "at", "to", "of", "or", "and", "the", "a", "an", "is", "it",
    "as", "by", "be", "if", "no", "so", "do", "not", "all", "any", "per", "for",
}


def _match_filters(question: str, df, cat_cols: list[str]) -> dict[str, list]:
    """
    Detect categorical VALUES mentioned in the question — 'total revenue in EU',
    'how many refunded orders' — and return {column: [matched values]}.

    Only low-cardinality columns are scanned (a value list is only meaningful
    as a filter vocabulary when it's small), and only string values of length
    >= 2 are matched, on word boundaries, to keep false positives rare. A false
    positive is still harmless by design: the block is additive context.
    """
    q = question.lower()
    filters: dict[str, list] = {}
    for col in cat_cols:
        try:
            if df[col].nunique() > 50:
                continue
            # Words of the column's own name never count as value mentions:
            # in "most common referral source", 'referral' names the column,
            # not the value 'referral' inside it.
            col_words = set().union(*(_word_variants(w) for w in re.split(r"[_\s]+", col.lower()) if w))
            values = [
                v for v in df[col].dropna().unique()
                if isinstance(v, str) and len(v) >= 2
                and v.lower() not in _FILTER_STOPWORDS
                and v.lower() not in col_words
            ]
            hit = []
            for v in values:
                v_l = v.lower()
                # Exact value match, or any sub-token of a compound value:
                # 'mobile_ios' matches a question that says 'iOS'.
                tokens = [t for t in re.split(r"[_\s\-]+", v_l) if len(t) >= 3 and t not in _FILTER_STOPWORDS]
                patterns = [v_l] + tokens
                if any(re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", q) for p in patterns):
                    hit.append(v)
            if hit:
                filters[col] = hit
        except Exception:
            continue
    return filters


def try_compute_answer(question: str, filepath: str, filename: str) -> str | None:
    """
    If `question` looks like an aggregate query, compute the answer over the
    FULL dataset and return a context block string. Otherwise return None.
    Never raises.
    """
    try:
        q_lower = question.lower()
        ops = _detect_ops(question)
        wants_groupby = _detect_groupby(question)
        wants_top_label = any(re.search(p, q_lower) for p in _TOP_LABEL_PATTERNS)
        wants_share = any(re.search(p, q_lower) for p in _SHARE_PATTERNS)
        wants_nunique = any(re.search(p, q_lower) for p in _NUNIQUE_PATTERNS)
        if not ops and not wants_groupby and not wants_top_label and not wants_share and not wants_nunique:
            return None

        # Honesty guard 1: time-scoped questions. We can't filter by date, so
        # an all-time number would answer a DIFFERENT question. Stay silent.
        if _TIME_SCOPE_PATTERN.search(q_lower):
            log.info("COMPUTE suppressed: time-scoped question, no date filtering support")
            return None

        path = Path(filepath)
        if not path.exists():
            return None
        # Full read via mtime-aware cache — the whole point is exactness over
        # ALL rows, but an unchanged file should be parsed once, not per message.
        df = _load_df(path, filename)
        if df is None:
            return None

        total_rows = len(df)
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        cat_cols = [c for c in df.columns if c not in numeric_cols]

        mentioned = _match_columns(question, df.columns.tolist())
        mentioned_numeric = [c for c in mentioned if c in numeric_cols]
        target_numeric = mentioned_numeric or numeric_cols[:2]
        group_col = next((c for c in mentioned if c in cat_cols and df[c].nunique() <= 50), None)

        # Filtered aggregates: "total revenue in EU" applies region == "EU"
        # before computing. Multiple values in one column OR together; values
        # across different columns AND together.
        filters = _match_filters(question, df, cat_cols)
        df_full = df  # pre-filter frame; category rankings must not be circular
        filter_desc = ""
        if filters:
            for col, vals in filters.items():
                df = df[df[col].isin(vals)]
            filter_desc = " where " + " and ".join(
                f"{col} in {vals}" if len(vals) > 1 else f"{col} = {vals[0]}"
                for col, vals in filters.items()
            )

        lines: list[str] = []

        # Distinct-value counts: "how many distinct countries" → nunique, and
        # suppress the plain row count which would otherwise be misleading.
        if wants_nunique:
            nunique_cols = [c for c in mentioned] or cat_cols[:2]
            for col in nunique_cols[:2]:
                # Counting distinct values of a column we filtered BY is
                # degenerate (always = number of matched values) — count on
                # the full frame instead.
                base = df_full if col in filters else df
                lines.append(f"distinct_count({col}) = {int(base[col].nunique())}")

        # Row count — but only for EXPLICIT count questions, and only when the
        # counted noun actually names something in THIS dataset ("how many
        # support tickets" on a users table must stay silent). A bare
        # row_count answering an unrelated question is noise wearing a suit.
        if "count" in ops and not wants_nunique:
            if _count_noun_matches_data(q_lower, filename, df_full.columns.tolist(), df_full):
                if filters:
                    lines.append(f"row_count{filter_desc} = {len(df)} (of {total_rows} total)")
                else:
                    lines.append(f"row_count = {total_rows}")

        # Share/percentage: fraction of rows matching the mentioned values.
        if wants_share and filters:
            share = 100.0 * len(df) / total_rows if total_rows else 0.0
            lines.append(f"share{filter_desc} = {round(share, 1)}% ({len(df)} of {total_rows} rows)")

        # Category-level top/least/second: "which country has the most users".
        if wants_top_label:
            is_least = any(re.search(p, q_lower) for p in _LEAST_PATTERNS)
            is_second = any(re.search(p, q_lower) for p in _SECOND_PATTERNS)
            label_cols = [c for c in mentioned if c in cat_cols and df[c].nunique() <= 50] or \
                         [c for c in cat_cols if df[c].nunique() <= 50][:1]
            # Rank categories by a numeric aggregate when the question names a
            # numeric column ("longest average duration", "second-highest by
            # revenue"); otherwise rank by frequency.
            rank_op = "mean" if ("mean" in ops or re.search(r"\baverage\b|平均", q_lower)) else "sum"
            for col in label_cols[:2]:
                # Ranking a column we just filtered BY would be circular
                # ("most common referral_source among rows where
                # referral_source == X" is always X) — rank on the full frame.
                base = df_full if col in filters else df
                if mentioned_numeric:
                    num = mentioned_numeric[0]
                    ranked = getattr(base.groupby(col)[num], rank_op)().sort_values(ascending=False)
                    label = f"{rank_op}({num}) by {col}"
                    if ranked.empty:
                        continue
                    if is_second and len(ranked) >= 2:
                        lines.append(f"second_highest {label} = '{ranked.index[1]}' ({round(float(ranked.iloc[1]), 2)})")
                    elif is_least:
                        lines.append(f"lowest {label} = '{ranked.index[-1]}' ({round(float(ranked.iloc[-1]), 2)})")
                    else:
                        lines.append(f"highest {label} = '{ranked.index[0]}' ({round(float(ranked.iloc[0]), 2)})")
                    continue
                vc = base[col].value_counts()
                if vc.empty:
                    continue
                if is_second and len(vc) >= 2:
                    lines.append(f"second_most_common({col}) = '{vc.index[1]}' ({int(vc.iloc[1])} rows)")
                elif is_least:
                    lines.append(f"least_common({col}) = '{vc.index[-1]}' ({int(vc.iloc[-1])} rows)")
                else:
                    lines.append(f"most_common({col}) = '{vc.index[0]}' ({int(vc.iloc[0])} rows)")

        # Honesty guard 3: numeric ops run ONLY on columns the question names
        # (directly or via synonyms). Computing mean(event_count) because
        # someone asked for "average customer age" is worse than silence.
        for col in mentioned_numeric[:2]:
            series = df[col].dropna()
            if series.empty:
                continue
            for op in ops or []:
                if op == "count":
                    continue
                try:
                    val = getattr(series, op)()
                    lines.append(f"{op}({col}){filter_desc} = {round(float(val), 4)}")
                except Exception:
                    continue

        if wants_groupby and group_col:
            agg_col = mentioned_numeric[0] if mentioned_numeric else None
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
