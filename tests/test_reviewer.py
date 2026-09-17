"""Unit tests for reviewer analytics (SPEC.md §18–§21)."""

from __future__ import annotations

from datetime import date, timedelta

from reviewscope.analysis.reviewer import (
    category_experience_score,
    compute_reviewer_metrics,
    local_familiarity_score,
    reviewer_relevance_score,
    reviewers_frame,
)
from reviewscope.models.review import NormalizedReview


def _review(
    rid: str,
    reviewer: str,
    place: str,
    category: str,
    city: str,
    region: str,
    day: date,
    rating: int = 4,
    text: str = "",
) -> NormalizedReview:
    return NormalizedReview(
        review_id=rid,
        place_id=place,
        place_category=category,
        reviewer_id=reviewer,
        rating=rating,
        text=text,
        published_at=day.isoformat(),
        city=city,
        region=region,
        country="RU",
    )


def _history(
    reviewer: str,
    n: int,
    category: str,
    city: str,
    region: str,
    start: date,
    place_prefix: str = "p",
) -> list[NormalizedReview]:
    return [
        _review(
            f"{reviewer}-{i}",
            reviewer,
            f"{place_prefix}{i}",
            category,
            city,
            region,
            start + timedelta(days=i * 20),
        )
        for i in range(n)
    ]


class TestReviewerMetrics:
    def test_basic_metrics(self) -> None:
        reviews = _history("u1", 4, "coffee", "Moscow", "MO", date(2026, 1, 1)) + [
            _review("extra1", "u1", "p9", "hotel", "SPb", "SPB", date(2026, 3, 1), rating=1),
            _review("extra2", "u2", "p8", "coffee", "Moscow", "MO", date(2026, 1, 1)),
        ]
        metrics = {m.reviewer_id: m for m in compute_reviewer_metrics(reviews, set(), set())}
        assert "u1" in metrics
        m = metrics["u1"]
        assert m.review_count == 5
        assert m.category_count == 2
        assert m.city_count == 2
        assert m.active_period_days > 30
        assert m.rating_entropy >= 0
        assert m.text_length_mean >= 0
        assert m.duplicate_ratio == 0.0
        assert m.template_ratio == 0.0

    def test_flagged_ratios(self) -> None:
        reviews = _history("u1", 3, "coffee", "Moscow", "MO", date(2026, 1, 1)) + [
            _review("dup", "u1", "p99", "coffee", "Moscow", "MO", date(2026, 2, 1), text="одинаковый текст повторяющийся")
        ]
        metrics = {m.reviewer_id: m for m in compute_reviewer_metrics(reviews, {"dup", "extra"}, set())}
        m = metrics["u1"]
        assert m.duplicate_ratio > 0


class TestCategoryExperience:
    def test_deep_history_scores_high(self) -> None:
        history = _history("u1", 40, "coffee", "Moscow", "MO", date(2026, 1, 1))
        result = category_experience_score(history, "coffee")
        assert result.value >= 60
        assert result.confidence.value == "HIGH"

    def test_thin_history_scores_low(self) -> None:
        history = _history("u1", 1, "coffee", "Moscow", "MO", date(2026, 1, 1))
        result = category_experience_score(history, "coffee")
        assert result.value < 30

    def test_no_category_metadata(self) -> None:
        result = category_experience_score([], None)
        assert result.value == 0.0

    def test_wrong_category_is_zero(self) -> None:
        history = _history("u1", 10, "coffee", "Moscow", "MO", date(2026, 1, 1))
        assert category_experience_score(history, "hotel").value == 0.0


class TestLocalFamiliarity:
    def test_substantial_local_history(self) -> None:
        history = _history("u1", 12, "coffee", "Moscow", "MO", date(2026, 1, 1), place_prefix="mc")
        result = local_familiarity_score(history, "Moscow", "MO")
        assert result.value >= 30
        assert "substantial review history" in " ".join(result.signals)

    def test_no_local_history(self) -> None:
        history = _history("u1", 5, "coffee", "Kazan", "TA", date(2026, 1, 1), place_prefix="kz")
        result = local_familiarity_score(history, "Moscow", "MO")
        assert result.value == 0.0
        if result.counter_signals:
            assert "no reviews near" in result.counter_signals[0]

    def test_travel_context_counter_signal(self) -> None:
        # Reviewer has long history in Moscow and now one review in Kazan.
        history = _history("u1", 4, "coffee", "Moscow", "MO", date(2025, 1, 1), place_prefix="mc")
        history.append(
            _review("kazan-1", "u1", "kz1", "coffee", "Kazan", "TA", date(2026, 9, 1), rating=4)
        )
        result = local_familiarity_score(history, "Kazan", "TA")
        assert result.value < 30
        assert any("outside" in s for s in result.counter_signals)

    def test_same_region_different_city_counts_partially(self) -> None:
        # Forensic regression (audit §20): a reviewer whose history is all in
        # the same *region* but different city used to score 0.0 because the
        # region evidence was computed into a dead variable. It now contributes
        # a partial score from the region component.
        history = _history("u1", 8, "coffee", "Khimki", "MO", date(2026, 1, 1), place_prefix="kh")
        result = local_familiarity_score(history, "Moscow", "MO")
        assert result.details["city_reviews"] == 0
        assert result.details["region_reviews"] == 8
        assert result.value > 0.0
        assert result.value >= 30

    def test_exact_city_and_region_are_partitioned(self) -> None:
        # Reviews pointing at the region must not swallow exact-city reviews
        # (the old `r.region == region or (region and r.region)` matched any
        # non-empty region and made the region bucket useless).
        history = _history("u1", 5, "coffee", "Moscow", "MO", date(2026, 1, 1), place_prefix="mc") + \
            _history("u1", 3, "coffee", "Khimki", "MO", date(2026, 2, 1), place_prefix="kh")
        result = local_familiarity_score(history, "Moscow", "MO")
        assert result.details["city_reviews"] == 5
        assert result.details["region_reviews"] == 3

    def test_region_only_history_when_city_missing(self) -> None:
        # No city metadata on the requested place -> pure region fallback still
        # gives non-zero familiarity instead of an empty LOW.
        history = _history("u1", 6, "coffee", "Moscow", "MO", date(2026, 1, 1), place_prefix="mc")
        result = local_familiarity_score(history, None, "MO")
        assert result.value > 0.0
        assert "no exact-city history" in " ".join(result.signals)


class TestReviewerRelevance:
    def test_strong_reviewer_relevant(self) -> None:
        history = _history("u1", 30, "coffee", "Moscow", "MO", date(2025, 1, 1), place_prefix="mc")
        result = reviewer_relevance_score(history, "coffee", "Moscow", "MO")
        assert result.value >= 40

    def test_new_reviewer_low_depth(self) -> None:
        history = [_review("r1", "u_new", "p1", "coffee", "Moscow", "MO", date(2026, 9, 1), rating=5)]
        result = reviewer_relevance_score(history, "coffee", "Moscow", "MO")
        assert result.value < 35
        assert any("little category experience" in c for c in result.counter_signals)


def test_reviewers_frame() -> None:
    reviews = _history("u1", 3, "coffee", "Moscow", "MO", date(2026, 1, 1))
    frame = reviewers_frame(compute_reviewer_metrics(reviews, set(), set()))
    assert {"reviewer_id", "reviews", "rating_mean", "duplicate_ratio"} <= set(frame.columns)
    assert len(frame) == 1
