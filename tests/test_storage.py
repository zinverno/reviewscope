"""Unit tests for the DuckDB storage layer (SPEC.md §3, §32)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.storage import DuckDBStore


def _sample_reviews() -> list:
    adapter = CSVAdapter()
    path = Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"
    return adapter.load(path).reviews[:200]


class TestDuckDBStore:
    def test_ingest_and_count(self) -> None:
        store = DuckDBStore()
        reviews = _sample_reviews()
        written = store.ingest(reviews)
        assert written == len(reviews)
        assert store.review_count() == len(reviews)
        store.close()

    def test_persistence_to_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "reviews.duckdb"
        store = DuckDBStore(db_path)
        store.ingest(_sample_reviews())
        store.close()

        reopened = DuckDBStore(db_path)
        assert reopened.table_exists("reviews")
        assert reopened.review_count() > 0
        reopened.close()

    def test_upsert_idempotent(self) -> None:
        store = DuckDBStore()
        reviews = _sample_reviews()[:50]
        store.ingest(reviews)
        store.ingest(reviews)
        assert store.review_count() == 50
        store.close()

    def test_fetch_reviews_roundtrip(self) -> None:
        store = DuckDBStore()
        reviews = _sample_reviews()
        store.ingest(reviews)
        fetched = store.fetch_reviews()
        assert len(fetched) == len(reviews)
        original = {r.review_id: r for r in reviews}
        for got in fetched:
            want = original[got.review_id]
            assert got.text == want.text
            assert got.rating == want.rating
            assert got.published_at == want.published_at
        store.close()

    def test_reviews_frame_has_parsed_datetime(self) -> None:
        store = DuckDBStore()
        store.ingest(_sample_reviews())
        df = store.reviews_frame()
        assert "published_at_dt" in df.columns
        assert df["published_at_dt"].notna().sum() > 0
        store.close()

    def test_list_places(self) -> None:
        store = DuckDBStore()
        store.ingest(_sample_reviews())
        places = store.list_places()
        assert places["place_id"].nunique() == places.shape[0]
        assert (places["review_count"] > 0).all()
        store.close()

    def test_place_filtering(self) -> None:
        store = DuckDBStore()
        store.ingest(_sample_reviews())
        places = store.list_places()
        first_place = places.iloc[0]["place_id"]
        only = store.reviews_frame(place_id=first_place)
        assert (only["place_id"] == first_place).all()
        assert store.place_counts(first_place) == len(only)
        store.close()

    def test_distinct_reviewer_count(self) -> None:
        store = DuckDBStore()
        store.ingest(_sample_reviews())
        assert store.distinct_reviewer_count() > 0
        store.close()


class TestEmbeddingCache:
    def test_store_and_get(self) -> None:
        store = DuckDBStore()
        keys = [("r1", "h1", "model-a"), ("r2", "h2", "model-a")]
        rows = [(k[0], k[1], k[2], np.random.RandomState(0).rand(4).astype(np.float32).tolist()) for k in keys]
        store.store_cached_embeddings(rows)
        got = store.get_cached_embeddings(keys)
        assert len(got) == 2
        assert np.allclose(got[keys[0]], np.asarray(rows[0][3], dtype=np.float32))
        store.close()

    def test_missing_keys_return_none(self) -> None:
        store = DuckDBStore()
        assert store.get_cached_embeddings([("nope", "h", "m")]) == {}
        store.close()

    def test_replacing_overwrites(self) -> None:
        store = DuckDBStore()
        store.store_cached_embeddings([("r1", "h1", "m", [1.0, 2.0])])
        store.store_cached_embeddings([("r1", "h1", "m", [3.0, 4.0])])
        got = store.get_cached_embeddings([("r1", "h1", "m")])
        assert np.allclose(got[("r1", "h1", "m")], [3.0, 4.0])
        assert store.cached_embedding_count() == 1
        store.close()

    def test_cache_is_persistent_across_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cache.duckdb"
        store = DuckDBStore(db_path)
        store.store_cached_embeddings([("r1", "h1", "m", np.zeros(8, dtype=np.float32).tolist())])
        store.close()

        reopened = DuckDBStore(db_path)
        got = reopened.get_cached_embeddings([("r1", "h1", "m")])
        assert len(got) == 1
        reopened.close()
