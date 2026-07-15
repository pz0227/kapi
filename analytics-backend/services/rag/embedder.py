"""
Local embedding service using sentence-transformers.
No external API calls required for embedding — runs fully offline.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from core.config import get_settings

settings = get_settings()


@lru_cache(maxsize=1)
def _get_model() -> "SentenceTransformer":
    from sentence_transformers import SentenceTransformer  # type: ignore
    return SentenceTransformer(settings.embedding_model)


def embed_texts(texts: list[str]) -> np.ndarray:
    """
    Embed a list of strings. Returns (N, D) float32 array.
    Lazy-loads the model on first call.
    """
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.array(embeddings, dtype=np.float32)


@lru_cache(maxsize=256)
def _embed_query_cached(query: str) -> np.ndarray:
    """Cached embedding for a single query string.

    Chat sessions routinely re-embed identical queries (retries, streaming +
    non-streaming paths, eval reruns). Model inference for a short query is
    ~10-50ms warm but the cache makes repeats ~free and, more importantly,
    removes embedding entirely from the critical path for repeated eval runs.
    Cache is keyed on the exact query string; 256 entries ≈ a few hundred KB.
    """
    return embed_texts([query])


def embed_query(query: str) -> np.ndarray:
    """Embed a single query string. Returns (1, D) float32 array.

    Returns a copy so callers can't mutate the cached array in place.
    """
    return _embed_query_cached(query).copy()


def chunk_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """
    Split text into overlapping word-based chunks.
    Used to break large dataset descriptions into indexable chunks.
    """
    chunk_size = chunk_size or settings.chunk_size
    overlap = overlap or settings.chunk_overlap

    # Clean excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()

    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap

    return chunks


def dataframe_to_text_chunks(
    df, dataset_name: str, max_rows: int | None = None, total_rows: int | None = None
) -> list[str]:
    """
    Convert a pandas DataFrame to a list of text chunks suitable for indexing.
    Each chunk represents a slice of rows with header.

    `total_rows` is the TRUE dataset size (the df passed in may itself already
    be a truncated read). When we index fewer rows than the dataset holds, the
    header says so explicitly — so the model can caveat its answers instead of
    presenting a partial view as the whole dataset. Honest limitation beats
    silent truncation (Phase 1; the real fix is the Phase-2 compute-first router).
    """
    import pandas as pd

    max_rows = max_rows if max_rows is not None else settings.index_max_rows
    rows = min(len(df), max_rows)
    df_sample = df.head(rows)

    # Column summary header
    dtypes = df.dtypes.to_dict()
    type_summary = ", ".join(f"{col}({str(dt)})" for col, dt in dtypes.items())
    true_total = total_rows if total_rows is not None else len(df)
    if true_total > rows:
        coverage_note = (
            f" | NOTE: only the first {rows} of {true_total} rows are indexed for retrieval — "
            f"treat any aggregate/total computed from retrieved rows as PARTIAL and say so"
        )
    else:
        coverage_note = ""
    header = f"Dataset: {dataset_name} | Columns: {type_summary} | Rows shown: {rows}{coverage_note}"

    # Convert to CSV-like text in slices of 20 rows
    chunks = [header]
    slice_size = 20
    for i in range(0, rows, slice_size):
        slice_df = df_sample.iloc[i : i + slice_size]
        chunk = f"[{dataset_name} rows {i+1}-{i+len(slice_df)}]\n" + slice_df.to_string(index=False)
        chunks.append(chunk)

    # Numeric stats summary
    try:
        stats = df.describe(include="all").to_string()
        chunks.append(f"[{dataset_name} statistics]\n{stats}")
    except Exception:
        pass

    return chunks
