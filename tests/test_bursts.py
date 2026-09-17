"""Unit tests for burst detection (SPEC.md §12) and rating anomaly (§13)."""

from __future__ import annotations

from datetime import date, timedelta

from reviewscope.analysis.bursts import (
    BurstDetector,
    BurstEvent,
    _filled_daily_counts,
    daily_counts,
)
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel


def _review(review_id: str, place_id: str, rating: int, day: date) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"u{review_id}",
        rating=rating,
        text=f"Отзыв {review_id}",
        published_at=day.isoformat(),
    )


def _history(place_id: str, baseline_per_day: int, days: int, start: date) -> list[NormalizedReview]:
    """Organic reviews at ``baseline_per_day``/day from the first day on, so the
    filled daily series has a real baseline before any burst day."""
    reviews: list[NormalizedReview] = []
    n = 0
    for offset in range(days):
        for _ in range(baseline_per_day):
            reviews.append(_review(f"{place_id}-{n}", place_id, 4, start + timedelta(days=offset)))
            n += 1
    return reviews


class TestDailyCounts:
    def test_groups_by_place_and_day(self) -> None:
        reviews = [
            _review("a", "p1", 5, date(2026, 1, 1)),
            _review("b", "p1", 4, date(2026, 1, 1)),
            _review("c", "p2", 3, date(2026, 1, 2)),
        ]
        frame = daily_counts(reviews)
        row = frame[(frame.place_id == "p1") & (frame.date == date(2026, 1, 1))]
        assert int(row["count"].iloc[0]) == 2

    def test_empty(self) -> None:
        assert daily_counts([]).empty


class TestFilledDailyCounts:
    def test_gap_days_filled_with_zero(self) -> None:
        reviews = [
            _review("a", "p1", 5, date(2026, 1, 1)),
            _review("b", "p1", 5, date(2026, 1, 3)),
        ]
        frame = _filled_daily_counts(daily_counts(reviews))
        p1 = frame[frame.place_id == "p1"].sort_values("date")
        assert len(p1) == 3
        assert int(p1["count"].iloc[1]) == 0


class TestBurstDetection:
    def test_steady_baseline_no_event(self) -> None:
        start = date(2025, 1, 1)
        reviews = _history("p1", 5, 60, start)
        events = BurstDetector().detect(reviews)
        assert events == []

    def test_burst_day_flagged_high(self) -> None:
        start = date(2025, 1, 1)
        reviews = _history("p1", 5, 60, start)
        burst_day = start + timedelta(days=55)
        for i in range(37):
            reviews.append(
                _review(f"burst{i}", "p1", 5 if i < 34 else 4, burst_day)
            )
        events = BurstDetector().detect(reviews)
        top = max(events, key=lambda e: e.observed)
        assert top.date == burst_day
        assert top.observed == 42  # 37 injected + 5 organic that day
        assert top.severity == ConfidenceLevel.HIGH
        assert top.ratings.get(5, 0) >= 34
        assert top.ratings.get(4, 0) >= 3
        assert top.score > 60

    def test_sparse_place_small_day_not_flagged(self) -> None:
        """A 3-review day on a median-0 place must NOT be MEDIUM (SPEC §12
        robustness: no false positive on near-empty baselines)."""
        start = date(2025, 1, 1)
        reviews = []
        for offset in range(30, 60):
            reviews.append(_review(f"r{offset}", "p2", 4, start + timedelta(days=offset)))
        reviews.extend(
            [
                _review("x1", "p2", 4, start + timedelta(days=70)),
                _review("x2", "p2", 4, start + timedelta(days=70)),
                _review("x3", "p2", 4, start + timedelta(days=70)),
            ]
        )
        events = BurstDetector().detect(reviews)
        assert all(e.severity != ConfidenceLevel.MEDIUM or e.observed >= 6 for e in events)

    def test_empty_input(self) -> None:
        assert BurstDetector().detect([]) == []

    def test_to_score_result(self) -> None:
        event = BurstEvent(
            place_id="p1",
            date=date(2026, 9, 2),
            expected=4.2,
            observed=37,
            multiplier=8.1,
            z_score=8.0,
            severity=ConfidenceLevel.HIGH,
            score=90.0,
            signals=["Volume 8.1x above baseline"],
        )
        res = event.to_score_result()
        assert res.value == 90.0
        assert res.confidence == ConfidenceLevel.HIGH
        assert res.details["observed"] == 37


class TestRatingAnomaly:
    def _detector(self):
        from reviewscope.analysis.rating_anomalies import RatingAnomalyDetector

        return RatingAnomalyDetector()

    def test_bombing_shifts_rating_distribution(self) -> None:
        start = date(2025, 1, 1)
        reviews = []
        # baseline: mostly 4-5 organic
        for i in range(28):
            reviews.append(
                _review(f"o{i}", "p5", 5 if i % 2 == 0 else 4, start + timedelta(days=40 + i))
            )
        # bombing: 28x1 + 2x2 on one day
        bomb_day = start + timedelta(days=80)
        for i in range(28):
            reviews.append(_review(f"b{i}", "p5", 1, bomb_day))
        for i in range(2):
            reviews.append(_review(f"b2{i}", "p5", 2, bomb_day))
        from reviewscope.analysis.bursts import BurstDetector

        events = BurstDetector().detect(reviews)
        anomalies = self._detector().detect(reviews, events)
        assert len(anomalies) >= 1
        top = anomalies[0]
        assert top.severity == ConfidenceLevel.HIGH
        assert top.jsd > 0.2
        assert top.event_dist.get(1, 0.0) > 0.7
        assert "lower ratings" in top.dominant_shift

    def test_negative_bombing_detected_equally(self) -> None:
        """"SPEC §15: a 1-star bombing must be detected by the same pipeline as
        a 5-star burst (no default 5-star bias)."""
        start = date(2025, 1, 1)
        reviews = [_review(f"bas{j}", "p9", 4, start + timedelta(days=30 + j)) for j in range(40)]
        bomb_day = start + timedelta(days=90)
        for i in range(30):
            reviews.append(_review(f"neg{i}", "p9", 1, bomb_day))
        from reviewscope.analysis.bursts import BurstDetector

        events = BurstDetector().detect(reviews)
        anoms = self._detector().detect(reviews, events)
        assert any(a.place_id == "p9" and a.event_dist.get(1, 0) >= 0.9 for a in anoms)

    def test_stable_ratings_no_anomaly(self) -> None:
        start = date(2025, 1, 1)
        reviews = _history("p3", 5, 60, start)
        from reviewscope.analysis.bursts import BurstDetector

        events = BurstDetector().detect(reviews)
        assert self._detector().detect(reviews, events) == []

    def test_empty(self) -> None:
        assert self._detector().detect([]) == []
