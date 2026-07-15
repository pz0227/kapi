"""
Retriever — combines FAISS search across multiple dataset indices,
deduplicates results, and formats retrieved context for injection.
"""
from __future__ import annotations

import logging
import time

from .embedder import embed_query
from .faiss_index import get_index, Chunk

from core.config import get_settings

settings = get_settings()
log = logging.getLogger("kapi.timing")


def retrieve(
    query: str,
    dataset_ids: list[str],
    top_k: int | None = None,
) -> list[dict]:
    """
    Retrieve the most relevant chunks across given datasets.

    Returns a list of source dicts:
    {
        "dataset_id": str,
        "dataset_name": str,
        "chunk_text": str,
        "score": float,
    }
    """
    top_k = top_k or settings.retrieval_top_k
    if not dataset_ids:
        return []

    # Split embed vs. search timing — the two candidate bottlenecks inside
    # retrieval. One debug line per call; aggregate later via `grep TIMING`.
    _t0 = time.perf_counter()
    query_vec = embed_query(query)  # (1, D)
    _t_embed = time.perf_counter() - _t0

    _t1 = time.perf_counter()
    all_results: list[tuple[Chunk, float]] = []
    for did in dataset_ids:
        idx = get_index(did)
        results = idx.search(query_vec, k=top_k)
        all_results.extend(results)
    _t_search = time.perf_counter() - _t1
    log.debug(
        "TIMING retrieve embed=%.3fs search=%.3fs datasets=%d",
        _t_embed, _t_search, len(dataset_ids),
    )

    # Sort by score descending and take top_k across all datasets
    all_results.sort(key=lambda x: x[1], reverse=True)
    top = all_results[:top_k]

    return [
        {
            "dataset_id": chunk.dataset_id,
            "dataset_name": chunk.dataset_name,
            "chunk_text": chunk.text,
            "score": round(score, 4),
        }
        for chunk, score in top
    ]


def format_context(sources: list[dict], max_chars: int = 6000) -> str:
    """
    Format retrieved sources into a context block for prompt injection.
    """
    if not sources:
        return ""

    lines = ["### Retrieved Data Context\n"]
    total = 0
    for i, src in enumerate(sources):
        block = f"**Source {i+1} — {src['dataset_name']}** (score: {src['score']})\n```\n{src['chunk_text']}\n```\n"
        total += len(block)
        if total > max_chars:
            lines.append("_[Context truncated to fit context window]_")
            break
        lines.append(block)

    return "\n".join(lines)


def groundedness_score(answer: str, sources: list[dict]) -> float:
    """
    Simple groundedness score: fraction of answer sentences that contain
    at least one term from the retrieved sources.
    """
    if not sources or not answer:
        return 0.0

    # Collect all terms from sources
    source_text = " ".join(s["chunk_text"] for s in sources).lower()
    source_words = set(source_text.split())

    sentences = [s.strip() for s in answer.replace("\n", ". ").split(".") if s.strip()]
    if not sentences:
        return 0.0

    grounded = 0
    for sent in sentences:
        sent_words = set(sent.lower().split())
        overlap = sent_words & source_words
        # Need at least 2 content words overlap
        if len(overlap) >= 2:
            grounded += 1

    return round(grounded / len(sentences), 3)
