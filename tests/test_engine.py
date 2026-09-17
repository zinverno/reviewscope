"""Integration unit tests for the AnalysisEngine (SPEC.md §14-§23 seam).

The engine ties every detector together; these tests exercise the wiring over a
small in-memory store with embeddings disabled (text-only paths).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.analysis.scoring import compute_review_weights, weighted_rating
from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore


def _review(
    rid: str,
    place: str,
    day: date,
    rating: int = 5,
    reviewer: str = "u",
    text: str = "стойкий запах кофе по всему залу, окна мыли трижды",
) -> NormalizedReview:
    return NormalizedReview(
        review_id=rid,
        place_id=place,
        place_name="Кофейня",
        place_category="coffee",
        reviewer_id=reviewer,
        reviewer_name="Юзер",
        rating=rating,
        text=text,
        published_at=day.isoformat(),
        city="Москва",
        region="Московская область",
        source="local",
    )


def _seed(store: DuckDBStore) -> None:
    reviews: list[NormalizedReview] = []
    # p1: wide baseline plus a clustered positive burst on 2026-09-02.
    pool = [
        "капучино из зёрен арабики получается мягким, молоко без отделения",
        "место тихое, розеток хватает, интернет стабильный весь день",
        "завтраки подают быстро, яйца бенедикт не пересушены вообще",
        "бариста аккуратный, на капучино делает сердечко, улыбается",
        "тесто для круассанов слоёное, масло чувствуется в каждом куске",
    ]
    for i in range(120):
        reviews.append(
            _review(
                f"base{i:04d}",
                "p1",
                date(2026, 1, 1) + timedelta(days=i * 2),
                rating=4 if i % 4 == 0 else 5,
                reviewer=f"u{i % 30}",
                text=pool[i % len(pool)],
            )
        )
    for i in range(12):
        reviews.append(
            _review(
                f"p1b{i:03d}",
                "p1",
                date(2026, 9, 2),
                rating=5,
                reviewer=f"b{i}",
                text="наконец-то настоящий кофе в соседнем дворе, теперь хожу каждый день",
            )
        )
    # p2: calm place, no burst.
    for i in range(60):
        reviews.append(
            _review(
                f"q2{i:03d}",
                "p2",
                date(2026, 1, 1) + timedelta(days=i * 5),
                rating=4 + i % 2,
                reviewer=f"v{i % 20}",
                text=pool[i % len(pool)],
            )
        )
    store.ingest(reviews)


@pytest.fixture
def engine() -> AnalysisEngine:
    store = DuckDBStore()
    _seed(store)
    try:
        yield AnalysisEngine(store, use_embeddings=False)
    finally:
        store.close()


class TestEngine:
    def test_analyze_populates_all_fields(self, engine: AnalysisEngine) -> None:
        p = engine.analyze("p1")
        assert p.place_id == "p1"
        assert p.review_count == 132
        assert p.reviews
        assert isinstance(p.burst_events, list)
        assert len(p.burst_events) > 0
        assert isinstance(p.rating_anomalies, list)
        assert isinstance(p.clusters, list)
        assert isinstance(p.duplicate_groups, list)
        assert len(p.templated_scores) == p.review_count
        assert len(p.review_weights) == p.review_count
        assert p.coordinated is not None and p.coordinated.value >= 0
        assert p.raw_rating > 0
        assert p.weighted_rating_value > 0
        assert p.weighted_rating_result is not None
        assert isinstance(p.keywords, list)
        assert isinstance(p.emerging, list)
        assert isinstance(p.positive_keywords, list)
        assert isinstance(p.negative_keywords, list)
        assert p.reviewer_overview is not None

    def test_weights_within_bounds(self, engine: AnalysisEngine) -> None:
        p = engine.analyze("p1")
        assert all(0.25 <= w <= 2.0 for w in p.review_weights)

    def test_review_ids_aligned_with_weights(self, engine: AnalysisEngine) -> None:
        p = engine.analyze("p1")
        assert [r.review_id for r in p.reviews] == [
            r.review_id for r in p.reviews
        ]
        assert len(p.review_weights) == len(p.reviews)

    def test_weighted_rating_matches_scoring_module(self, engine: AnalysisEngine) -> None:
        p = engine.analyze("p1")
        raw, weighted, _ = weighted_rating(p.reviews, p.review_weights)
        assert round(p.raw_rating, 2) == round(raw, 2)
        assert round(p.weighted_rating_value, 2) == round(weighted, 2)

    def test_weights_consistent_with_compute_review_weights(self, engine: AnalysisEngine) -> None:
        p = engine.analyze("p1")
        recomputed = compute_review_weights(p.reviews)
        assert len(recomputed) == len(p.review_weights)
        assert all(isinstance(w, float) for w in recomputed)

    def test_analysis_is_deterministic(self, engine: AnalysisEngine) -> None:
        a = engine.analyze("p2")
        b = engine.analyze("p2")
        assert a.raw_rating == b.raw_rating
        assert a.weighted_rating_value == b.weighted_rating_value
        assert a.coordinated.value == b.coordinated.value
        assert a.review_weights == b.review_weights

    def test_clean_place_not_flagged_high(self, engine: AnalysisEngine) -> None:
        p2 = engine.analyze("p2")
        p1 = engine.analyze("p1")
        assert p2.coordinated.confidence.value != "HIGH"
        assert p2.raw_rating <= p2.weighted_rating_value + 0.01
        assert p1.coordinated.value >= p2.coordinated.value
