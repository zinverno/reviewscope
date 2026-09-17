"""DuckDB-backed embedding cache wrapping an ``EmbeddingProvider`` (SPEC.md §32).

Repeated runs must not re-encode unchanged texts. The cache looks up the
``embeddings_cache`` table by ``(review_id, text_hash, model_name)``,
encodes only the missing entries, then persists them back. A lightweight
in-memory session cache further avoids redundant DuckDB roundtrips when
multiple analysis modules consume the same embeddings within a single app
rerun.
"""

from __future__ import annotations

import numpy as np

from reviewscope.embeddings.base import EmbeddingProvider
from reviewscope.models.review import NormalizedReview
from reviewscope.storage.duckdb_store import DuckDBStore


class EmbeddingCache:
    """Facade that adds persistence on top of any ``EmbeddingProvider``."""

    def __init__(
        self,
        store: DuckDBStore,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._session: dict[tuple[str, str, str], np.ndarray] = {}

    @property
    def provider(self) -> EmbeddingProvider:
        if self._provider is None:
            from reviewscope.embeddings.sentence_transformer import (
                SentenceTransformerProvider,
            )

            self._provider = SentenceTransformerProvider()
        return self._provider

    # -- public API -----------------------------------------------------------

    def embed_reviews(self, reviews: list[NormalizedReview]) -> np.ndarray:
        """Return an (n, dim) float32 matrix aligned with ``reviews``.

        The method touches the DuckDB cache and, when necessary, the provider.
        Outputs are deterministic for identical input and model name.
        """
        model_name = self.provider.name
        dim = self.provider.dim
        if not reviews:
            return np.zeros((0, dim), dtype=np.float32)

        # Build cache keys in parallel with the text-or-empty accessor.
        keys = [
            (r.review_id, r.fingerprint(), model_name) for r in reviews
        ]
        all_texts = [r.text_or_empty() for r in reviews]

        # 1. Try the session in-memory cache first.
        cached: dict[int, np.ndarray] = {}
        missing_indices: list[int] = []
        for i, key in enumerate(keys):
            if key in self._session:
                cached[i] = self._session[key]
            else:
                missing_indices.append(i)

        # 2. Batch-check the DuckDB cache for anything not in memory.
        if missing_indices:
            missing_keys = [keys[i] for i in missing_indices]
            db_hits = self._store.get_cached_embeddings(missing_keys)
            still_missing: list[int] = []
            for idx in missing_indices:
                key = keys[idx]
                if key in db_hits:
                    vec = db_hits[key]
                    cached[idx] = vec
                    self._session[key] = vec
                else:
                    still_missing.append(idx)

            # 3. Encode whatever remains.
            if still_missing:
                texts_to_encode = [all_texts[i] for i in still_missing]
                new_vectors = self.provider.encode(texts_to_encode)
                store_rows: list[tuple[str, str, str, list[float]]] = []
                for pos, idx in enumerate(still_missing):
                    vec = new_vectors[pos]
                    cached[idx] = vec
                    self._session[keys[idx]] = vec
                    store_rows.append((keys[idx][0], keys[idx][1], keys[idx][2], vec.tolist()))
                self._store.store_cached_embeddings(store_rows)

        # Assemble the (n, dim) output in original order.
        result = np.zeros((len(reviews), dim), dtype=np.float32)
        for idx, vec in cached.items():
            result[idx] = vec
        return result

    def cache_hit_ratio(self, reviews: list[NormalizedReview]) -> float:
        """Fraction of reviews already in the session + DB cache (for telemetry)."""
        model_name = self.provider.name
        keys = [(r.review_id, r.fingerprint(), model_name) for r in reviews]
        if not keys:
            return 0.0
        in_session = sum(1 for k in keys if k in self._session)
        if in_session == len(keys):
            return 1.0
        missing_keys = [k for k in keys if k not in self._session]
        db_hits = self._store.get_cached_embeddings(missing_keys)
        hits = in_session + sum(1 for k in missing_keys if k in db_hits)
        return hits / len(keys)

    @property
    def provider_name(self) -> str:
        return self.provider.name
