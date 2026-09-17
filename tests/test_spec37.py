"""Deterministic synthetic-anomaly test (SPEC.md §37).

Reproduces the exact SPEC example: a place with a 5-reviews/day baseline gets
an event day of 50 reviews (46 five-star). The volume burst, the rating
distribution shift and the resulting anomaly signals must all be reproducibly
HIGH given the fixed input data.
"""

from __future__ import annotations

from datetime import date, timedelta

from reviewscope.analysis.bursts import BurstDetector
from reviewscope.analysis.rating_anomalies import RatingAnomalyDetector
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel


def _review(review_id: str, place_id: str, rating: int, day: date) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"u{review_id}",
        rating=rating,
        text=f"Отзыв {review_id} о кофе и сервисе в этом заведении",
        published_at=day.isoformat(),
    )


START = date(2026, 1, 1)
PLACE = "p42"


def _build_scenario() -> list[NormalizedReview]:
    reviews: list[NormalizedReview] = []
    # Baseline: 5 reviews/day for 60 days with a realistic organic rating
    # spread (mirrors demo weights 45/28/14/7/6 across 5★..1★).
    spread = [5, 5, 4, 3, 1]  # 5★ x2, 4★, 3★, 1★ per 5-review block
    for offset in range(60):
        for i, rating in enumerate(spread):
            reviews.append(
                _review(f"base{offset}-{i}", PLACE, rating, START + timedelta(days=offset))
            )
    # Event: 50 reviews on day 70, 46 five-star, 4 four-star.
    event_day = START + timedelta(days=70)
    for i in range(50):
        rating = 5 if i < 46 else 4
        reviews.append(_review(f"event{i}", PLACE, rating, event_day))
    return reviews


class TestSpec37SyntheticAnomaly:
    def test_burst_is_high(self) -> None:
        events = BurstDetector().detect(_build_scenario())
        event = max(events, key=lambda e: e.observed)
        assert event.date == START + timedelta(days=70)
        assert event.observed == 50
        assert event.severity == ConfidenceLevel.HIGH
        assert event.score >= 60.0
        assert event.ratings.get(5, 0) == 46
        assert event.ratings.get(4, 0) == 4

    def test_rating_anomaly_is_high(self) -> None:
        from reviewscope.analysis.bursts import BurstDetector

        reviews = _build_scenario()
        events = BurstDetector().detect(reviews)
        anomalies = RatingAnomalyDetector().detect(reviews, events)
        assert anomalies, "expected rating anomalies for the §37 event"
        top = anomalies[0]
        assert top.severity == ConfidenceLevel.HIGH
        assert top.event_dist.get(5, 0.0) > 0.9

    def test_score_explainability(self) -> None:
        event = max(
            BurstDetector().detect(_build_scenario()),
            key=lambda e: e.observed,
        )
        result = event.to_score_result()
        assert result.signals
        assert result.details["observed"] == 50
        assert result.rendered_value().startswith(str(round(event.score)))
