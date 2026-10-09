from datetime import datetime, timedelta

from reviewscope.analysis.bursts import BurstDetector
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview


def _r(d, i, place_id="p"):
    return NormalizedReview(
        review_id=f"r{i}",
        place_id=place_id,
        place_name="P",
        place_category="c",
        reviewer_id=f"u{i % 5}",
        reviewer_name="U",
        rating=5,
        text="t",
        published_at=d.isoformat(),
        city="c",
        region="r",
        country="C",
        latitude=0.0,
        longitude=0.0,
        source="d",
    )


def test_burst_with_zero_baseline_reports_undefined_ratio():
    start = datetime(2026, 1, 1)
    reviews = []
    # Only 1 review in the entire 30-day baseline window before event
    reviews.append(_r(start, 0))
    # Event day has 30 reviews
    d_event = start + timedelta(days=30)
    for j in range(30):
        reviews.append(_r(d_event, 100 + j))
    evs = BurstDetector(CONFIG.burst).detect(reviews)
    assert evs
    sigs = evs[0].signals
    # Should not say "x above the rolling median" with a clean ratio
    combined = " ".join(sigs)
    assert "multiplier undefined" in combined
    assert "rolling median 0.0" in combined or "median 0.0" in combined
    # Must still preserve z-score and threshold
    assert "Modified z-score" in combined
    assert "threshold" in combined


def test_burst_with_positive_baseline_reports_ratio():
    start = datetime(2026, 1, 1)
    reviews = []
    # median becomes 1.0
    for i in range(30):
        reviews.append(_r(start + timedelta(days=i), i))
    d_event = start + timedelta(days=30)
    for j in range(30):
        reviews.append(_r(d_event, 200 + j))
    evs = BurstDetector(CONFIG.burst).detect(reviews)
    assert evs
    sigs = evs[0].signals
    combined = " ".join(sigs)
    assert "x above the rolling median" in combined
    assert "expected" in combined and "observed" in combined
    assert "multiplier undefined" not in combined
