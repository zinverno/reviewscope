"""Tests for the embedding provider and cache (SPEC.md §3, §32).

The SentenceTransformerProvider lazily downloads the multilingual model on
first use; this is expected and happens once per environment.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider
from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.models.review import NormalizedReview
from reviewscope.storage.duckdb_store import DuckDBStore

# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def provider() -> SentenceTransformerProvider:
    return SentenceTransformerProvider()


@pytest.fixture
def sample_reviews() -> list[NormalizedReview]:
    return CSVAdapter().load(Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv").reviews[:20]


@pytest.fixture
def small_reviews() -> list[NormalizedReview]:
    return CSVAdapter().load(Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv").reviews[:8]


# --------------------------------------------------------------- provider unit


class TestSentenceTransformerProvider:
    def test_encode_shape(self, provider: SentenceTransformerProvider, sample_reviews: list) -> None:
        texts = [r.text_or_empty() for r in sample_reviews]
        embeddings = provider.encode(texts)
        assert embeddings.shape == (len(texts), provider.dim)
        assert embeddings.dtype == np.float32

    def test_deterministic(self, provider: SentenceTransformerProvider, sample_reviews: list) -> None:
        texts = [r.text_or_empty() for r in sample_reviews]
        a = provider.encode(texts)
        b = provider.encode(texts)
        np.testing.assert_array_equal(a, b)

    def test_empty_input(self, provider: SentenceTransformerProvider) -> None:
        assert provider.encode([]).shape == (0, provider.dim)

    def test_provider_name(self, provider: SentenceTransformerProvider) -> None:
        assert "multilingual" in provider.name.lower() or "paraphrase" in provider.name.lower()


# --------------------------------------------------------------- cache integration


class TestEmbeddingCache:
    def test_first_run_encodes_all(self, small_reviews: list) -> None:
        store = DuckDBStore()
        cache = EmbeddingCache(store, SentenceTransformerProvider())
        vectors = cache.embed_reviews(small_reviews)
        assert vectors.shape == (len(small_reviews), 384)
        assert store.cached_embedding_count() == len(small_reviews)
        ratio = cache.cache_hit_ratio(small_reviews)
        assert ratio == 1.0  # all in session now
        store.close()

    def test_second_run_hits_cache(self, small_reviews: list, tmp_path: Path) -> None:
        db_path = tmp_path / "cache_test.duckdb"
        store = DuckDBStore(db_path)
        cache = EmbeddingCache(store, SentenceTransformerProvider())
        cache.embed_reviews(small_reviews)
        # fresh cache object, same DB
        cache2 = EmbeddingCache(DuckDBStore(db_path), SentenceTransformerProvider())
        ratio_before = cache2.cache_hit_ratio(small_reviews)
        assert ratio_before == 1.0
        vectors = cache2.embed_reviews(small_reviews)
        assert vectors.shape == (len(small_reviews), 384)
        store.close()

    def test_partial_encode_when_only_new_reviews_added(self, small_reviews: list) -> None:
        store = DuckDBStore()
        cache = EmbeddingCache(store, SentenceTransformerProvider())
        cache.embed_reviews(small_reviews)
        assert store.cached_embedding_count() == len(small_reviews)
        # add one more
        extra = small_reviews + [small_reviews[-1].model_copy(update={"review_id": "extra999"})]
        vectors = cache.embed_reviews(extra)
        assert vectors.shape == (len(extra), 384)
        assert store.cached_embedding_count() == len(small_reviews) + 1
        store.close()
