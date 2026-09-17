"""Unit tests for the scoring pipeline (SPEC.md §14, §22, §23)."""

from __future__ import annotations

from datetime import date

from reviewscope.analysis.bursts import BurstEvent
from reviewscope.analysis.duplicates import DuplicateGroup
from reviewscope.analysis.scoring import (
    compute_review_weights,
    coordinated_activity_score,
    weighted_rating,
)
from reviewscope.analysis.templated import TemplatedTextScorer
from reviewscope.analysis.topics import TopicCluster
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel


def _review(
    rid: str,
    place: str,
    day: date,
    rating: int = 5,
    reviewer: str = "u",
    text: str = "text",
) -> NormalizedReview:
    return NormalizedReview(
        review_id=rid,
        place_id=place,
        reviewer_id=reviewer,
        rating=rating,
        text=text,
        published_at=day.isoformat(),
    )


class TestCoordinatedActivity:
    def test_empty(self) -> None:
        r = coordinated_activity_score([])
        assert r.value == 0.0
        assert r.confidence.value == "LOW"

    def test_burst_high_drives_score(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 9, 2)) for i in range(30)]
        burst = BurstEvent(
            place_id="p1",
            date=date(2026, 9, 2),
            expected=2.0,
            observed=30,
            multiplier=15.0,
            z_score=15.0,
            severity=ConfidenceLevel.HIGH,
            score=100.0,
        )
        r = coordinated_activity_score(reviews, burst_events=[burst])
        assert r.value >= 30

    def test_all_anomalies_max(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 9, 1 + i % 4), reviewer=f"u{i % 2}") for i in range(40)]
        burst = BurstEvent(
            place_id="p1", date=date(2026, 9, 2), expected=2.0, observed=40,
            multiplier=20.0, z_score=20.0,
            severity=ConfidenceLevel.HIGH, score=100.0,
        )
        cluster = TopicCluster(
            cluster_id=0, review_ids=[f"r{i}" for i in range(35)],
            place_ids=["p1"], avg_rating=5.0,
            date_min="2026-09-01", date_max="2026-09-04",
            similarity=0.9, representative_phrases=[], representative_reviews=[],
        )
        tpl = TemplatedTextScorer().score(reviews)
        r = coordinated_activity_score(
            reviews,
            burst_events=[burst],
            clusters=[cluster],
            templated_results=tpl,
        )
        assert r.value >= 40

    def test_signals_and_counter_signals(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 9, 1 + i % 3)) for i in range(6)]
        dup_group = DuplicateGroup(
            group_id=1,
            review_ids=["r0", "r1", "r2"],
            exact_count=3, fuzzy_count=0, near_count=0, semantic_count=0,
            avg_similarity=1.0, signals=[], counter_signals=[],
        )
        tpl = TemplatedTextScorer().score(reviews)
        r = coordinated_activity_score(
            reviews,
            dup_groups=[dup_group],
            templated_results=tpl,
        )
        assert r.value > 0
        assert isinstance(r.signals, list)


class TestReviewWeight:
    def test_weight_min_max(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 1, i + 1)) for i in range(5)]
        weights = compute_review_weights(reviews)
        assert all(0.25 <= w <= 2.0 for w in weights)

    def test_specificity_raises_weight(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 1, i + 1), text="стейк за 1400 рублей") for i in range(3)]
        specificity_map = {"r0": 90.0, "r1": 85.0, "r2": 88.0}
        w_no = compute_review_weights(reviews)
        w_high = compute_review_weights(reviews, specificity_map=specificity_map)
        assert sum(w_high) > sum(w_no)

    def test_duplicate_penalty(self) -> None:
        reviews = [
            _review(f"r{i}", "p1", date(2026, 9, 1 + i), rating=4, text=f"стейк за 1400 рублей с кровью {i}")
            for i in range(3)
        ]
        specificity_map = {f"r{i}": 90.0 for i in range(3)}
        w_normal = compute_review_weights(reviews, specificity_map=specificity_map)
        w_penalized = compute_review_weights(
            reviews, specificity_map=specificity_map, dup_flagged_ids={"r0", "r1"}
        )
        assert w_penalized[0] < w_normal[0]
        assert sum(w_penalized) < sum(w_normal)


class TestWeightedRating:
    def test_raw_equals_weighted_when_equal_weights(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 1, 1), rating=4 + i % 2) for i in range(8)]
        raw, weighted, result = weighted_rating(reviews)
        assert abs(raw - weighted) < 0.1
        assert result.value == weighted
        assert "nearly identical" in result.counter_signals[0]

    def test_low_specificity_lower_weight(self) -> None:
        # Suspicious-looking 5-star reviews (low specificity) get low weight;
        # credible 1-star reviews (specific, high weight) drag the rating down.
        reviews = [
            _review("good", "p1", date(2026, 9, 2), rating=5, text="потрясающе рекомендую") for _ in range(3)
        ] + [
            _review("bad", "p1", date(2026, 9, 2), rating=1, text="стейк принесли холодным за 40 минут") for _ in range(3)
        ]
        w = [0.5] * 3 + [1.8] * 3
        raw, weighted, result = weighted_rating(reviews, weights=w)
        assert weighted < raw
        assert result.value == weighted

    def test_empty_reviews(self) -> None:
        raw, weighted, result = weighted_rating([])
        assert raw == 0.0
        assert weighted == 0.0
        assert result.value == 0.0
