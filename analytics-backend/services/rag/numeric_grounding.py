"""
Numeric groundedness (Phase 3).

Why this exists: for a data-analytics agent, the single most dangerous output
is a WRONG NUMBER stated confidently. The existing word-overlap groundedness
score is blind to it — "revenue was $5M" scores as grounded as long as the word
"revenue" appears in a source, even if the real figure is $3M. That is exactly
the failure class this product is supposed to prevent (the same one I document
in my product teardowns): a plausible answer to a different question than the
data supports.

This module scores a complementary, stricter signal: of the numbers the answer
states, what fraction actually appear in the grounding context (retrieved
sources + full-dataset computed blocks)? A number in the answer that matches
nothing in the grounding is flagged as potentially fabricated.

Matching is precision-aware, not exact. Analysts round: an answer of "52%" is a
faithful report of a source "52.2%", and "$1.2M" faithfully rounds 1,234,567.
So a grounding number `g` matches an answer number `a` when `g` rounded to the
precision `a` was written at equals `a` (within a small epsilon for float
noise). Fabrication is "no grounding number rounds to this," not "not identical."

Stdlib only. Never raises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Multiplier words/suffixes an analyst answer might use.
_SCALE = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "trillion": 1e12,
}

# A number token: optional $, digits with optional thousands separators and
# decimals, optional scale suffix, optional trailing %.
_NUM_RE = re.compile(
    r"""(?<![\w.])            # not preceded by word char or dot (avoid ids like u_0.5)
        \$?\s?
        (\d{1,3}(?:,\d{3})+|\d+)   # integer part (grouped or plain)
        (\.\d+)?                    # optional decimal
        \s?(k|thousand|mm?|million|bn?|billion|t|trillion)?  # optional scale
        \s?(%)?                     # optional percent
        (?![\w])                    # not followed by word char
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class ExtractedNumber:
    value: float          # numeric value after scale/percent applied
    half_ulp: float       # half the place-value of the least significant digit
    raw: str              # original substring, for reporting

    def matches(self, g: float, rel_eps: float = 1e-6) -> bool:
        """True if grounding value `g` rounds to this answer number's precision."""
        tol = max(self.half_ulp, rel_eps * max(abs(self.value), abs(g), 1.0))
        return abs(self.value - g) <= tol + 1e-9


def _half_ulp(int_part: str, dec_part: str | None, scale: float) -> float:
    """Half the place value of the least significant written digit, scaled.

    "1.2" -> LSD is the 0.1s place -> ulp 0.1 -> half 0.05.
    "52"  -> LSD is the 1s place   -> ulp 1   -> half 0.5.
    "1200"-> we treat trailing written zeros as significant (0.5) rather than
    guessing sig-figs; analysts who write 1200 usually mean ~1200, and a source
    of exactly 1200 still matches.
    """
    if dec_part:  # e.g. ".2" -> 1 decimal place
        places = len(dec_part) - 1  # minus the dot
        ulp = 10.0 ** (-places)
    else:
        ulp = 1.0
    return 0.5 * ulp * scale


def extract_numbers(text: str) -> list[ExtractedNumber]:
    """Extract numeric quantities an answer states. Never raises."""
    out: list[ExtractedNumber] = []
    if not text:
        return out
    try:
        for m in _NUM_RE.finditer(text):
            int_part, dec_part, scale_word, pct = m.group(1), m.group(2), m.group(3), m.group(4)
            base = float(int_part.replace(",", "") + (dec_part or ""))
            scale = _SCALE.get(scale_word.lower(), 1.0) if scale_word else 1.0
            value = base * scale
            if pct:
                # Keep percentages on their own scale (52% -> 52.0), since
                # grounding text also carries them as "52.2%".
                pass
            out.append(ExtractedNumber(
                value=value,
                half_ulp=_half_ulp(int_part, dec_part, scale),
                raw=m.group(0).strip(),
            ))
    except Exception:
        return out
    return out


def _grounding_values(text: str) -> list[float]:
    """All numeric values present in the grounding context."""
    return [n.value for n in extract_numbers(text)]


def numeric_groundedness(answer: str, grounding_text: str) -> dict:
    """
    Score how well the answer's numbers are supported by the grounding context.

    Returns:
      {
        "score": float | None,   # fraction of answer-numbers found; None if the
                                  # answer states no numbers (metric N/A)
        "total": int,            # numbers stated in the answer
        "grounded": int,         # of those, how many matched grounding
        "ungrounded": [str],     # raw substrings of unmatched (suspect) numbers
      }
    """
    try:
        answer_nums = extract_numbers(answer)
        if not answer_nums:
            return {"score": None, "total": 0, "grounded": 0, "ungrounded": []}

        g_values = _grounding_values(grounding_text)
        grounded, ungrounded = 0, []
        for a in answer_nums:
            if any(a.matches(g) for g in g_values):
                grounded += 1
            else:
                ungrounded.append(a.raw)
        return {
            "score": round(grounded / len(answer_nums), 3),
            "total": len(answer_nums),
            "grounded": grounded,
            "ungrounded": ungrounded,
        }
    except Exception:
        # Instrumentation must never break the answer path.
        return {"score": None, "total": 0, "grounded": 0, "ungrounded": []}
