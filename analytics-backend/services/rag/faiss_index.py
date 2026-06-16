"""
FAISS-based vector index for local retrieval.
One index per dataset_id; persisted to disk under storage/faiss/.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from core.config import get_settings
from .embedder import embed_texts, dataframe_to_text_chunks

settings = get_settings()


@dataclass
class Chunk:
    chunk_id: int
    dataset_id: str
    dataset_name: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class KapiIndex:
    """
    Wrapper around a FAISS flat L2 index plus a parallel list of Chunk objects.
    Stored on disk as two files:
      - {index_dir}/{dataset_id}.faiss  — FAISS index binary
      - {index_dir}/{dataset_id}.meta.pkl — Chunk list
    """

    def __init__(self, dataset_id: str):
        self.dataset_id = dataset_id
        self._dir = settings.faiss_index_dir
        self._faiss_path = self._dir / f"{dataset_id}.faiss"
        self._meta_path = self._dir / f"{dataset_id}.meta.pkl"
        self._index = None
        self._chunks: list[Chunk] = []

    @property
    def exists(self) -> bool:
        return self._faiss_path.exists() and self._meta_path.exists()

    def load(self) -> None:
        import faiss  # type: ignore
        self._index = faiss.read_index(str(self._faiss_path))
        with open(self._meta_path, "rb") as f:
            self._chunks = pickle.load(f)

    def build(self, df, dataset_name: str) -> int:
        """
        Build and persist a FAISS index from a DataFrame.
        Returns number of chunks indexed.
        """
        import faiss  # type: ignore

        text_chunks = dataframe_to_text_chunks(df, dataset_name)
        if not text_chunks:
            return 0

        embeddings = embed_texts(text_chunks)  # (N, D)
        dim = embeddings.shape[1]

        self._index = faiss.IndexFlatIP(dim)  # Inner-product on normalized vecs = cosine sim
        self._index.add(embeddings)

        self._chunks = [
            Chunk(
                chunk_id=i,
                dataset_id=self.dataset_id,
                dataset_name=dataset_name,
                text=t,
            )
            for i, t in enumerate(text_chunks)
        ]

        # Persist
        self._dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._faiss_path))
        with open(self._meta_path, "wb") as f:
            pickle.dump(self._chunks, f)

        return len(self._chunks)

    def search(self, query_vec: np.ndarray, k: int = 6) -> list[tuple[Chunk, float]]:
        """
        Search for top-k most similar chunks.
        Returns list of (Chunk, score) tuples sorted by score desc.
        """
        if self._index is None or self._index.ntotal == 0:
            return []

        k = min(k, self._index.ntotal)
        scores, indices = self._index.search(query_vec, k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0 and idx < len(self._chunks):
                results.append((self._chunks[idx], float(score)))
        return results

    def delete(self) -> None:
        for p in [self._faiss_path, self._meta_path]:
            if p.exists():
                p.unlink()
        self._index = None
        self._chunks = []


# Global index registry (per process)
_INDICES: dict[str, KapiIndex] = {}


def get_index(dataset_id: str) -> KapiIndex:
    if dataset_id not in _INDICES:
        idx = KapiIndex(dataset_id)
        if idx.exists:
            idx.load()
        _INDICES[dataset_id] = idx
    return _INDICES[dataset_id]


def build_index(dataset_id: str, df, dataset_name: str) -> int:
    idx = KapiIndex(dataset_id)
    count = idx.build(df, dataset_name)
    _INDICES[dataset_id] = idx
    return count


def delete_index(dataset_id: str) -> None:
    idx = get_index(dataset_id)
    idx.delete()
    _INDICES.pop(dataset_id, None)
