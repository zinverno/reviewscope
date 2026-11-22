"""Session and DuckDB cache for exact review text and encoder configuration."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from reviewscope.embeddings.base import (
    EmbeddingKey,
    EmbeddingProvider,
    checked_batch,
    embedding_key,
)
from reviewscope.embeddings.frozen_demo import (
    DEFAULT_REPLAY_CONTRACT,
    FROZEN_ID,
    frozen_demo_vectors,
)
from reviewscope.embeddings.sentence_transformer import (
    REFERENCE_RUNTIME,
    SentenceTransformerProvider,
)
from reviewscope.models.review import NormalizedReview
from reviewscope.public_demo import public_demo_enabled
from reviewscope.storage.duckdb_store import DuckDBStore


class EmbeddingCache:
    def __init__(self, store: DuckDBStore, provider: EmbeddingProvider | None = None) -> None:
        self._store = store
        self._provider = provider if provider is not None else SentenceTransformerProvider()
        self._session: dict[EmbeddingKey, np.ndarray] = {}
        self._frozen = False
        if provider is None and store.read_only:
            replay = replace(self.provider.contract, runtime=REFERENCE_RUNTIME)
            if replay.encoding_id == DEFAULT_REPLAY_CONTRACT:
                self._session = frozen_demo_vectors(store)
                self._frozen = bool(self._session)

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    @property
    def encoding_id(self) -> str:
        return FROZEN_ID if self._frozen else self.provider.contract.encoding_id

    def _keys(self, reviews: list[NormalizedReview]) -> list[EmbeddingKey]:
        return [embedding_key(r.review_id, r.text_or_empty(), self.encoding_id) for r in reviews]

    def _lookup(
        self, keys: list[EmbeddingKey], reviews: list[NormalizedReview]
    ) -> tuple[dict[EmbeddingKey, np.ndarray], dict[EmbeddingKey, str]]:
        """Read validated database hits and the ordered texts still missing."""
        hits = {}
        pending = {
            k: r.text_or_empty() for k, r in zip(keys, reviews, strict=True)
            if k not in self._session
        }
        if pending and not self._frozen:
            for key, vec in self._store.get_cached_embeddings(list(pending)).items():
                if (
                    vec.shape == (self.provider.dim,)
                    and np.isfinite(vec).all()
                    and (pending[key].strip() or not vec.any())
                ):
                    hits[key] = vec
                    del pending[key]
        return hits, pending

    def missing_keys(self, reviews: list[NormalizedReview]) -> list[EmbeddingKey]:
        keys = self._keys(reviews)
        _, pending = self._lookup(keys, reviews)
        return [key for key in keys if key in pending]

    def embed_reviews(self, reviews: list[NormalizedReview]) -> np.ndarray:
        if not reviews:
            return np.zeros((0, self.provider.dim), dtype=np.float32)
        keys = self._keys(reviews)
        hits, pending = self._lookup(keys, reviews)
        if pending:
            if self._frozen or (self._store.read_only and public_demo_enabled()):
                raise ValueError(f"{len(pending)} missing or invalid precomputed embeddings")
            texts = list(pending.values())
            vectors = checked_batch(self.provider.encode(texts), len(texts), self.provider.dim)
            if any(not text.strip() and vec.any() for text, vec in zip(texts, vectors, strict=True)):
                raise ValueError("encoder must return zero vectors for blank text")
            new = dict(zip(pending, vectors, strict=True))
            if not self._store.read_only:
                self._store.store_cached_embeddings([(*k, v.tolist()) for k, v in new.items()])
            self._session.update(new)
        self._session.update(hits)
        return np.stack([self._session[key] for key in keys])

    def cache_hit_ratio(self, reviews: list[NormalizedReview]) -> float:
        if not reviews:
            return 0.0
        return (len(reviews) - len(self.missing_keys(reviews))) / len(reviews)

    @property
    def provider_name(self) -> str:
        return self.provider.name
