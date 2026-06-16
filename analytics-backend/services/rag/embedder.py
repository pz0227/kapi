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


def embed_query(query: str) -> np.ndarray:
    """Embed a single query string. Returns (1, D) float32 array."""
    return embed_texts([query])


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


def dataframe_to_text_chunks(df, dataset_name: str, max_rows: int = 200) -> list[str]:
    """
    Convert a pandas DataFrame to a list of text chunks suitable for indexing.
    Each chunk represents a slice of rows with header.
    """
    import pandas as pd

    rows = min(len(df), max_rows)
    df_sample = df.head(rows)

    # Column summary header
    dtypes = df.dtypes.to_dict()
    type_summary = ", ".join(f"{col}({str(dt)})" for col, dt in dtypes.items())
    header = f"Dataset: {dataset_name} | Columns: {type_summary} | Rows shown: {rows}"

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
