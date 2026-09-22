"""Robust volume-burst detection (SPEC.md §12).

Approach
--------
Each place is binned by calendar day. For every day we build the *rolling
baseline* as the median daily count over the preceding ``baseline_window``
days, with spread measured by MAD (median absolute deviation). Anomaly is
quantified with the modified z-score ``0.6745 * (x - median) / MAD`` and the
volume multiplier ``observed / median``.

SPEC.md §12 explicitly forbids using plain standard deviation as the *only*
measure, so the design uses median + MAD through the whole pipeline (a single
extreme day cannot inflate the baseline).

Output
------
:class:`BurstEvent` carries the explainability payload required by SPEC.md §36:
expected/observed counts, multiplier, modified z-score, rating breakdown,
severity band, and signals+counter-signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from reviewscope.config import CONFIG, BurstConfig
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel, ScoreResult


def _to_date(value: str | None) -> date | None:
    """Parse ``published_at`` (date or datetime string) into a ``date``."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def daily_counts(reviews: list[NormalizedReview]) -> pd.DataFrame:
    """Return per-(place, day) normalised review counts as a DataFrame.

    Days without any review carry a ``count`` of 0 so that the rolling
    median/MAD baseline includes empty days (a burst is defined relative to
    the *calendar* rhythm, not just to days that have reviews).
    """
    rows: list[tuple[str, date]] = []
    for r in reviews:
        d = _to_date(r.published_at)
        if d is not None:
            rows.append((r.place_id, d))
    if not rows:
        return pd.DataFrame(columns=["place_id", "date", "count"])
    frame = pd.DataFrame(rows, columns=["place_id", "date"])
    frame["count"] = 1
    return frame.groupby(["place_id", "date"])["count"].sum().reset_index()


def _filled_daily_counts(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill gaps: one row per day between min and max date, count 0 when empty."""
    if frame.empty:
        return frame
    filled: list[pd.DataFrame] = []
    for place_id, sub in frame.groupby("place_id"):
        if sub.empty:
            continue
        min_day, max_day = sub["date"].min(), sub["date"].max()
        idx = pd.DataFrame(
            {
                "date": pd.date_range(min_day, max_day, freq="D").date,
                "place_id": place_id,
            }
        )
        filled.append(idx.merge(sub, on=["place_id", "date"], how="left"))
    result = pd.concat(filled, ignore_index=True)
    result["count"] = result["count"].fillna(0).astype(int)
    return result.sort_values(["place_id", "date"]).reset_index(drop=True)


@dataclass
class BurstEvent:
    """A single volume-anomaly event at a place on a day.

    ``score`` is the volume component in 0..100 used by the coordinated
    activity score (SPEC.md §14); it is derived monotonically from the
    modified z-score.
    """

    place_id: str
    date: date
    expected: float
    observed: int
    multiplier: float
    z_score: float
    ratings: dict[int, int] = field(default_factory=dict)
    severity: ConfidenceLevel = ConfidenceLevel.LOW
    score: float = 0.0
    mad: float = 0.0
    signals: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)

    def to_score_result(self) -> ScoreResult:
        """Expose the event as an explainable :class:`ScoreResult` (§36)."""
        return ScoreResult(
            name="Volume anomaly",
            value=round(self.score, 1),
            confidence=self.severity,
            signals=self.signals,
            counter_signals=self.counter_signals,
            details={
                "expected": round(self.expected, 2),
                "observed": self.observed,
                "multiplier": round(self.multiplier, 2),
                "z_score": round(self.z_score, 2),
                "mad": round(self.mad, 2),
            },
        )


class BurstDetector:
    """Detect per-place volume bursts using rolling median + modified z-score."""

    def __init__(self, config: BurstConfig = CONFIG.burst) -> None:
        self.config = config

    def _severity(self, multiplier: float, z_score: float, median: float) -> ConfidenceLevel:
        """Map burst intensity onto a severity band.

        The modified z-score is the primary driver. The volume multiplier is
        used only as a secondary trigger when a *real* baseline exists
        (``median >= 1``); days on places with median 0 (no measurable daily
        rhythm) must rely on the absolute z-score, otherwise a single 3-review
        afternoon on a near-empty place would look like a coordinated burst.
        """
        if z_score >= self.config.z_score_threshold * 1.5:
            return ConfidenceLevel.HIGH
        if z_score >= self.config.z_score_threshold:
            return ConfidenceLevel.MEDIUM
        if median >= 1.0 and multiplier >= self.config.high_multiplier:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    def _volume_score(self, z_score: float) -> float:
        """Map a modified z-score onto a 0..100 volume component.

        Formula (documented, SPEC.md §14 "no magic coefficients"):

        ``score = clip((z - z_low) / (z_high - z_low), 0, 1) * 100``

        with ``z_low = 0.5 * z_score_threshold`` and
        ``z_high = 2.0 * z_score_threshold``. A day at the z-score threshold
        (``z_score_threshold = 3.5``) therefore scores ≈ 33/100; a day at
        double the threshold scores 100/100.
        """
        z_low = self.config.z_score_threshold * 0.5
        z_high = self.config.z_score_threshold * 2.0
        ratio = (z_score - z_low) / max(z_high - z_low, 1e-9)
        return float(np.clip(ratio, 0.0, 1.0) * 100.0)

    def _modified_z_score(self, observed: int, median: float, mad: float) -> float:
        """Robust normalised deviation.

        Formula: ``0.6745 * (observed - median) / max(mad, 1.0)``. The MAD is
        floored at 1.0 review so that a perfectly steady baseline (MAD == 0,
        e.g. a place averaging under one review a day) does not yield infinite
        z-scores; the floor keeps the scale interpretable in review counts.
        """
        return 0.6745 * (observed - median) / max(mad, 1.0)

    def detect(self, reviews: list[NormalizedReview]) -> list[BurstEvent]:
        """Return flagged burst events (MEDIUM/HIGH only).

        A day must reach ``min_reviews_for_event`` reviews *and* either a
        MEDIUM severity to be returned; days with a HIGH multiplier are always
        returned. The first ``baseline_window`` days have no baseline and are
        skipped.
        """
        if not reviews:
            return []
        frame = _filled_daily_counts(daily_counts(reviews))
        if frame.empty:
            return []

        window = self.config.baseline_window
        events: list[BurstEvent] = []
        for place_id, sub in frame.groupby("place_id", sort=True):
            counts = sub["count"].astype(int).to_numpy()
            dates = sub["date"].to_numpy()
            for i in range(window, len(counts)):
                observed = int(counts[i])
                baseline = counts[max(0, i - window) : i]
                if baseline.size == 0:
                    continue
                median = float(np.median(baseline))
                mad = float(np.median(np.abs(baseline - median)))
                z_score = self._modified_z_score(observed, median, mad)
                multiplier = observed / max(median, 1.0)
                if observed < self.config.min_reviews_for_event:
                    continue
                severity = self._severity(multiplier, z_score, median)
                if severity == ConfidenceLevel.LOW:
                    continue

                day = dates[i]
                day_ratings: dict[int, int] = {}
                for r in reviews:
                    if (
                        r.place_id == place_id
                        and _to_date(r.published_at) == day
                        and r.rating is not None
                    ):
                        day_ratings[r.rating] = day_ratings.get(r.rating, 0) + 1

                score = self._volume_score(z_score)
                signals = [
                    f"Volume {multiplier:.1f}x above the rolling median "
                    f"({median:.1f} reviews/day expected vs {observed} observed)",
                    f"Modified z-score {z_score:.1f} vs threshold "
                    f"{self.config.z_score_threshold:.1f}",
                ]
                counter_signals: list[str] = []
                if mad / max(median, 1e-9) >= 0.5:
                    counter_signals.append(
                        f"Baseline is volatile (MAD {mad:.1f}), the event is "
                        "closer to normal variability"
                    )
                if len(day_ratings) >= 3:
                    counter_signals.append(
                        "Ratings on the event day are mixed across "
                        f"{len(day_ratings)} distinct scores"
                    )
                events.append(
                    BurstEvent(
                        place_id=place_id,
                        date=day,
                        expected=round(median, 2),
                        observed=observed,
                        multiplier=round(multiplier, 2),
                        z_score=round(z_score, 2),
                        ratings=day_ratings,
                        severity=severity,
                        score=round(score, 1),
                        mad=round(mad, 2),
                        signals=signals,
                        counter_signals=counter_signals,
                    )
                )
        events.sort(key=lambda e: (e.severity.value, e.multiplier, e.date), reverse=True)
        return events


def events_frame(events: list[BurstEvent]) -> pd.DataFrame:
    """Render burst events into a tabular frame for the UI."""
    return pd.DataFrame(
        [
            {
                "place_id": e.place_id,
                "date": e.date.isoformat(),
                "expected": e.expected,
                "observed": e.observed,
                "multiplier": e.multiplier,
                "z_score": e.z_score,
                "score": e.score,
                "severity": e.severity.value,
                "ratings": e.ratings,
            }
            for e in events
        ]
    )


def _demo() -> None:
    """Quick sanity run used during development (not shipped as an entrypoint)."""
    from pathlib import Path

    from reviewscope.ingestion.csv_adapter import CSVAdapter

    reviews = CSVAdapter().load(Path("data/demo_reviews.csv")).reviews
    events = BurstDetector().detect(reviews)
    for e in events[:10]:
        print(
            e.place_id, e.date, "observed", e.observed, "expected", e.expected,
            "multiplier", e.multiplier, "z", e.z_score, e.severity.value, e.score,
            e.ratings,
        )


if __name__ == "__main__":
    _demo()
