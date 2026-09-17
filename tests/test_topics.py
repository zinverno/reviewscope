"""Unit tests for semantic topic clustering (SPEC.md §10)."""

from __future__ import annotations

from datetime import date

import numpy as np

from reviewscope.analysis.topics import TopicClusterer, _representative_phrases, clusters_frame
from reviewscope.config import TopicConfig
from reviewscope.models.review import NormalizedReview


def _clusterer(**overrides: object) -> TopicClusterer:
    return TopicClusterer(TopicConfig(**overrides))


def _review(review_id: str, place_id: str, rating: int, text: str, day: date) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"u{review_id}",
        rating=rating,
        text=text,
        published_at=day.isoformat(),
    )


def _two_centers(n_per_center: int) -> tuple[list[NormalizedReview], np.ndarray]:
    """Embeddings as two well-separated 384-dim gaussian blobs."""
    rng = np.random.RandomState(7)
    center_a = rng.rand(384).astype(np.float32)
    center_b = rng.rand(384).astype(np.float32) + 5
    reviews: list[NormalizedReview] = []
    embeddings: list[np.ndarray] = []
    for i in range(n_per_center):
        day_a = date(2026, 1, i % 28 + 1)
        reviews.append(_review(f"a{i}", "p1", 5, "кофе вкусный крепкий аромат", day_a))
        reviews.append(_review(f"b{i}", "p2", 1, "долгое ожидание грубый персонал", day_a))
        embeddings.append(center_a + rng.rand(384).astype(np.float32) * 0.1)
        embeddings.append(center_b + rng.rand(384).astype(np.float32) * 0.1)
    return reviews, np.array(embeddings, dtype=np.float32)


class TestClusterer:
    def test_produces_cluster_metadata(self) -> None:
        reviews, embeddings = _two_centers(6)
        clusters = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
        assert len(clusters) >= 2
        c = next(c for c in clusters if c.review_ids[0].startswith("a"))
        assert len(c.review_ids) >= 6
        assert c.avg_rating == 5.0
        assert c.date_min <= c.date_max
        assert 0.5 <= c.similarity <= 1.0
        assert c.representative_phrases
        assert c.representative_reviews

    def test_avg_rating_and_phrases(self) -> None:
        reviews, embeddings = _two_centers(6)
        clusters = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
        by_rating = [c.avg_rating for c in clusters]
        assert 1.0 in by_rating and 5.0 in by_rating
        pos = next(c for c in clusters if c.avg_rating == 5.0)
        neg = next(c for c in clusters if c.avg_rating == 1.0)
        pos_joined = " ".join(pos.representative_phrases)
        neg_joined = " ".join(neg.representative_phrases)
        assert "кофе" in pos_joined or "вкусный" in pos_joined
        assert "очередь" in neg_joined or "ожидание" in neg_joined or "персонал" in neg_joined

    def test_no_fields_missing(self) -> None:
        reviews, embeddings = _two_centers(6)
        clusters = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
        for c in clusters:
            assert c.cluster_id >= 0
            assert c.place_ids
            assert c.review_ids
            assert isinstance(c.similarity, float)

    def test_empty_and_small_inputs(self) -> None:
        assert _clusterer().cluster([], None) == []
        single = [_review("a", "p1", 5, "x", date(2026, 1, 1))]
        assert _clusterer(min_cluster_size=2, min_samples=1).cluster(single, None) == []

    def test_requires_embeddings(self) -> None:
        reviews = [_review(f"r{i}", "p1", 5, f"текст {i}", date(2026, 1, i + 1)) for i in range(6)]
        assert _clusterer(min_cluster_size=2, min_samples=1).cluster(reviews, None) == []

    def test_deterministic(self) -> None:
        reviews, embeddings = _two_centers(7)
        a = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
        b = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
        assert [c.review_ids for c in a] == [c.review_ids for c in b]


class TestRepresentativePhrases:
    def test_phrases_from_reviews(self) -> None:
        reviews = [
            _review("r1", "p1", 5, "долгое ожидание и грубый персонал", date(2026, 1, 1)),
            _review("r2", "p1", 1, "долгое ожидание в очереди", date(2026, 1, 2)),
            _review("r3", "p1", 1, "грубый персонал на ресепшн", date(2026, 1, 3)),
        ]
        phrases = _representative_phrases(reviews, top_k=3)
        joined = " ".join(phrases)
        assert "долгое ожидание" in joined or "персонал" in joined


def test_clusters_frame() -> None:
    reviews, embeddings = _two_centers(5)
    clusters = _clusterer(min_cluster_size=3, min_samples=2).cluster(reviews, embeddings)
    frame = clusters_frame(clusters)
    assert {"reviews", "avg_rating", "similarity", "phrases"} <= set(frame.columns)
    assert len(frame) == len(clusters)
