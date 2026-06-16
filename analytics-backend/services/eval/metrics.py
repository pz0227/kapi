"""
metrics.py — deterministic per-case scorers for the Kapi eval.

THREE INDEPENDENT AXES (never blended into one number):

  1. lexical_support(answer, sources)
        "Is the answer talking about the retrieved data?"  Cheap word-overlap.
        Mirrors the production groundedness_score. HONESTLY LIMITED: an answer
        can be fully lexically supported and still numerically WRONG, because
        overlap counts shared *words*, not whether a stated *number* is true.
        This limitation is the entire reason metric #2 exists.

  2. answer_correctness(answer, gold)
        "Is the stated fact actually true?"  For numeric gold: extract the
        numeric claim and compare to the computed gold within a tolerance.
        For categorical gold: word-boundary match of the gold label (+ a few
        synonyms). This is the metric that fails a hallucinated "9,999 MAU"
        that lexical_support happily passes.

  3. refusal(answer, case)
        "On a question it should NOT answer, did it honestly decline / correct
        the false premise — instead of confidently making something up?"
        The honesty axis, applied to unanswerable + adversarial cases.

score_case() runs the right subset for a case's category and returns all axes
separately. Nothing here is hardcoded to a dataset — gold flows in from
gold.py, which computed it from the real CSVs.
"""
from __future__ import annotations

import re


# ════════════════════════════════════════════════════════════════════════════
# METRIC 1 — lexical support (reframed groundedness)
# ════════════════════════════════════════════════════════════════════════════

def _lexical_support_local(answer: str, sources: list[dict]) -> float:
    """
    Self-contained reimplementation of the production groundedness_score:
    fraction of answer 'sentences' that share >= 2 content words with the union
    of retrieved chunk text. Kept local so metrics.py is unit-testable without
    importing the FAISS/embedder stack; the runner can swap in the production
    function via lexical_support(use_production=True).
    """
    if not sources or not answer:
        return 0.0
    source_text = " ".join(s.get("chunk_text", "") for s in sources).lower()
    source_words = set(source_text.split())
    sentences = [s.strip() for s in answer.replace("\n", ". ").split(".") if s.strip()]
    if not sentences:
        return 0.0
    grounded = 0
    for sent in sentences:
        if len(set(sent.lower().split()) & source_words) >= 2:
            grounded += 1
    return round(grounded / len(sentences), 3)


def lexical_support(answer: str, sources: list[dict], use_production: bool = False) -> float:
    """
    Returns lexical-support score in [0, 1].

    use_production=True imports the real services.rag.groundedness_score so the
    eval reports the SAME number the live system uses; falls back to the local
    mirror if the RAG stack can't be imported (e.g. isolated unit test).
    """
    if use_production:
        try:
            from services.rag import groundedness_score  # lazy: avoids heavy import at module load
            return groundedness_score(answer, sources)
        except Exception:
            pass
    return _lexical_support_local(answer, sources)


# ════════════════════════════════════════════════════════════════════════════
# METRIC 2 — answer correctness (numeric + label), the anti-hallucination metric
# ════════════════════════════════════════════════════════════════════════════

# Matches: $1,454.52  |  93.3%  |  1.4K  |  500  |  ~17  |  28.55
_NUM_RE = re.compile(
    r"""(?P<cur>\$)?\s*
        (?P<sign>[-+])?
        (?P<int>\d{1,3}(?:,\d{3})+|\d+)
        (?P<frac>\.\d+)?
        \s*(?P<scale>[KkMmBb])?
        \s*(?P<pct>%)?
    """,
    re.VERBOSE,
)
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}

# "between 40 and 45", "40-45", "40 to 45"
_RANGE_RE = re.compile(
    r"(?:between\s+)?(\d[\d,]*\.?\d*)\s*(?:-|–|—|to|and)\s*(\d[\d,]*\.?\d*)",
    re.IGNORECASE,
)

# Word-boundary synonyms for short/ambiguous categorical golds. "US" must NEVER
# match inside "status"/"focus"/"thus", hence boundary-aware matching + synonyms.
_LABEL_SYNONYMS = {
    "us": ["united states", "u.s.", "u.s.a.", "usa", "america", "the states"],
    "uk": ["united kingdom", "u.k.", "britain", "england"],
    "web": ["website", "desktop", "browser"],
    "page_view": ["page view", "pageview", "page views", "page_views"],
    "data import": ["data-import", "import"],
    "free": ["free plan", "free tier"],
    "organic": ["organic search", "organic traffic"],
    "electronics": ["electronic"],
}


class ParsedNumber:
    __slots__ = ("raw", "value", "is_percent", "prev_char", "next_char")

    def __init__(self, raw: str, value: float, is_percent: bool,
                 prev_char: str = "", next_char: str = ""):
        self.raw, self.value, self.is_percent = raw, value, is_percent
        # The characters bracketing the digit run — used to detect numbers that
        # are embedded inside identifier tokens (e.g. the "500" in "u_00500").
        self.prev_char, self.next_char = prev_char, next_char

    @property
    def is_id_embedded(self) -> bool:
        """True if the number is glued to a letter/underscore on either side,
        i.e. it's part of an identifier like u_00500 / order_00312 / 00078_0000
        rather than a free-standing numeric claim."""
        def glued(c: str) -> bool:
            return c.isalpha() or c == "_"
        return glued(self.prev_char) or glued(self.next_char)


def extract_numbers(text: str, exclude_id_embedded: bool = False) -> list[ParsedNumber]:
    """
    Pull every numeric claim out of text, normalized to float.

    exclude_id_embedded=True drops numbers that are glued inside identifier
    tokens (u_00500 -> 500). Used by gold_in_context for COUNT-type gold to
    avoid falsely crediting the retriever when a user-id merely *contains* the
    count's digits. Left False for the answer side, where the model's prose
    rarely glues a real count to an id and over-rejection would hurt recall.
    """
    out: list[ParsedNumber] = []
    n = len(text)
    for m in _NUM_RE.finditer(text):
        int_part = m.group("int")
        if int_part is None:
            continue
        raw = m.group(0).strip()
        if not raw or raw in ("$", "+", "-"):
            continue
        num = float(int_part.replace(",", "") + (m.group("frac") or ""))
        if m.group("sign") == "-":
            num = -num
        scale = m.group("scale")
        if scale:
            num *= _SCALE[scale.lower()]
        istart = m.start("int")
        # End of the NUMERIC content (last matched of int/frac/scale/pct) — NOT
        # m.end(), which includes the trailing \s* the regex consumes before the
        # optional scale group. Using m.end() made "500 users" read next_char='u'
        # and falsely look id-embedded.
        iend = m.end("int")
        if m.group("frac"):
            iend = m.end("frac")
        if m.group("scale"):
            iend = m.end("scale")
        if m.group("pct"):
            iend = m.end("pct")
        prev_char = text[istart - 1] if istart > 0 else ""
        next_char = text[iend] if iend < n else ""
        p = ParsedNumber(raw, num, m.group("pct") == "%", prev_char, next_char)
        if exclude_id_embedded and p.is_id_embedded:
            continue
        out.append(p)
    return out


def _numeric_candidates(p: ParsedNumber, gold_is_percent: bool) -> list[float]:
    """
    Candidate values a single parsed number could mean, given the gold's unit.
    Handles the "0.93 means 93%" case without over-matching.
    """
    cands = [p.value]
    if gold_is_percent and not p.is_percent and 0 < p.value <= 1:
        cands.append(p.value * 100.0)   # "0.933" -> 93.3
    return cands


def _numeric_correctness(answer: str, gold: dict) -> dict:
    gold_val = float(gold["value"])
    tol = float(gold.get("tolerance", 0.0))
    gold_is_pct = gold.get("unit") == "%"
    parsed = extract_numbers(answer)

    # Pass if ANY parsed number lands within tolerance of gold. "Any" reduces
    # false negatives when an answer states several numbers (e.g. "43 of 500").
    best = None
    for p in parsed:
        for cand in _numeric_candidates(p, gold_is_pct):
            diff = abs(cand - gold_val)
            if diff <= tol or (tol == 0 and cand == gold_val):
                if best is None or diff < best[1]:
                    best = (p.raw, diff, cand)

    if best is not None:
        return {"applicable": True, "mode": "number", "passed": True,
                "gold": gold_val, "matched": best[2], "match_type": "exact",
                "extracted": [p.raw for p in parsed]}

    # No exact/tolerance hit — try a stated range that brackets the gold.
    for rm in _RANGE_RE.finditer(answer):
        lo = float(rm.group(1).replace(",", ""))
        hi = float(rm.group(2).replace(",", ""))
        if lo <= gold_val <= hi:
            return {"applicable": True, "mode": "number", "passed": True,
                    "gold": gold_val, "matched": f"{lo}-{hi}", "match_type": "range",
                    "extracted": [p.raw for p in parsed]}

    return {"applicable": True, "mode": "number", "passed": False,
            "gold": gold_val, "matched": None, "match_type": "none",
            "extracted": [p.raw for p in parsed]}


def _label_present(answer: str, label: str) -> tuple[bool, str | None]:
    candidates = [label] + _LABEL_SYNONYMS.get(label.lower(), [])
    for c in candidates:
        # Word-boundary match so "US" never matches inside "status".
        if re.search(r"(?<![A-Za-z])" + re.escape(c) + r"(?![A-Za-z])", answer, re.I):
            return True, c
    return False, None


def _label_correctness(answer: str, gold: dict) -> dict:
    label = str(gold["value"])
    present, hit = _label_present(answer, label)
    return {"applicable": True, "mode": "label", "passed": present,
            "gold": label, "matched": hit, "match_type": "label" if present else "none",
            "extracted": []}


def answer_correctness(answer: str, gold: dict | None) -> dict:
    """Dispatch on gold kind. Returns applicable=False when there's no gold."""
    if not gold:
        return {"applicable": False, "mode": None, "passed": None}
    if gold.get("kind") == "number":
        return _numeric_correctness(answer, gold)
    return _label_correctness(answer, gold)


# ════════════════════════════════════════════════════════════════════════════
# METRIC 3 — refusal detection (honesty axis)
# ════════════════════════════════════════════════════════════════════════════

# Cues that signal a DATA-AVAILABILITY limitation (the correct shape of a
# refusal here). Deliberately excludes bare confidence hedges ("not sure",
# "can't be certain") so a confident hallucination wrapped in a hedge is NOT
# misread as an honest refusal.
_LIMITATION_CUES = [
    "no data", "not available", "isn't available", "is not available",
    "not in the data", "not in the dataset", "data does not", "data doesn't",
    "doesn't include", "does not include", "doesn't contain", "does not contain",
    "cannot compute", "can't compute", "cannot determine", "can't determine",
    "cannot calculate", "can't calculate", "unable to", "there is no",
    "there's no", "no information", "not enough data", "insufficient data",
    "not possible to", "cannot provide", "can't provide", "do not have",
    "don't have", "no such", "not tracked", "isn't tracked", "not recorded",
    "no record", "not present", "not captured", "no way to know",
    "isn't enough", "lacks the", "without the", "no column",
]

# Cues that signal the model is CORRECTING a false premise (adversarial cases).
_PREMISE_CUES = [
    "not accurate", "isn't accurate", "is not accurate", "incorrect",
    "not correct", "no evidence", "isn't supported", "is not supported",
    "not supported by", "the premise", "false premise", "not the case",
    "contrary to", "misconception", "actually, the", "in fact, the",
    "that's not right", "that is not right", "cannot confirm", "can't confirm",
    "would be misleading", "no basis",
]


def _contains_any(text_lower: str, cues: list[str]) -> list[str]:
    return [c for c in cues if c in text_lower]


def refusal(answer: str, expects_refusal: bool, is_adversarial: bool = False) -> dict:
    """
    Detect whether the model declined / corrected the premise.

    Returns:
      refused        bool   — a data-limitation or premise-correction cue fired
      matched_cues   list   — which cues (auditability)
      gave_figure    bool   — it ALSO emitted a concrete numeric claim; a
                              'refused but stated a number' combo is suspicious
                              and surfaced for the judge / a human to inspect
      correct        bool|None — refused == expects_refusal (the honesty verdict)
    """
    low = answer.lower()
    matched = _contains_any(low, _LIMITATION_CUES)
    if is_adversarial:
        matched += _contains_any(low, _PREMISE_CUES)
    refused = len(matched) > 0

    # "gave_figure": did it commit a specific number anyway? Years (4-digit
    # 19xx/20xx) are excluded because a correct refusal often cites the date
    # range it DOES cover ("the data only covers 2026").
    figures = [p for p in extract_numbers(answer)
               if not (1900 <= p.value <= 2100 and p.value == int(p.value))]
    gave_figure = len(figures) > 0

    return {
        "applicable": True,
        "expects_refusal": expects_refusal,
        "refused": refused,
        "matched_cues": matched,
        "gave_figure": gave_figure,
        "correct": (refused == expects_refusal),
    }


# ════════════════════════════════════════════════════════════════════════════
# COMBINE — per-case result with the three axes kept SEPARATE
# ════════════════════════════════════════════════════════════════════════════

def score_case(case, answer: str, sources: list[dict], use_production_groundedness: bool = False) -> dict:
    """
    Score one case. Returns the three axes independently plus a category-aware
    `primary_pass` (the ONE axis that defines success for this case's category)
    — but never a blended numeric score.

      answerable   -> primary_pass = correctness.passed AND not wrongly refused
      should-refuse-> primary_pass = refusal.correct (did it correctly decline)
    """
    ls = lexical_support(answer, sources, use_production=use_production_groundedness)

    if case.is_answerable:
        corr = answer_correctness(answer, case.gold)
        # An answerable case can still be DISHONEST by refusing a question it
        # could answer; detect that so we can tag false_refusal downstream.
        ref = refusal(answer, expects_refusal=False, is_adversarial=False)
        primary = bool(corr.get("passed")) and not ref["refused"]
    else:
        corr = {"applicable": False, "mode": None, "passed": None}
        ref = refusal(answer, expects_refusal=True,
                      is_adversarial=(case.category == "adversarial"))
        primary = bool(ref["correct"])

    return {
        "case_id": case.id,
        "category": case.category,
        "question": case.question,
        "answer": answer,
        "retrieval_count": len(sources),
        "lexical_support": ls,
        "correctness": corr,     # competence axis (answerable only)
        "refusal": ref,          # honesty axis
        "primary_pass": primary,
    }
