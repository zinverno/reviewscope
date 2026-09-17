"""Rating-distribution anomaly detection (SPEC.md §13).

For every volume burst (:class:`~reviewscope.analysis.bursts.BurstEvent`) the
rating distribution of a short *event window* is compared to the historical
baseline distribution (the trailing ``baseline_window`` days). Divergence is
measured with Jensen-Shannon divergence, which:

* is symmetric in its arguments;
* is bounded (0 for identical distributions, ``ln 2`` for disjoint supports);
* is interpretable and explaiable in UI text.

A shift towards a single dominant rating on the event day is an additional
coordination signal (SPEC.md §13 example: baseline 57% five-star vs 96%
five-star during the event).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from reviewscope.analysis.bursts import BurstDetector, BurstEvent
from reviewscope.config import CONFIG, RatingAnomalyConfig
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel, ScoreResult

RATINGS = (1, 2, 3, 4, 5)


def _jsd(p: dict[int, float], q: dict[int, float]) -> float:
    """Jensen-Shannon divergence between two rating distributions in [0, 1].

    Uses natural log; the result is in ``[0, ln 2]``. Zero-probability entries
    are handled with a tiny epsilon before normalisation, which keeps the
    metric well-defined for ratings absent from one side.
    """
    eps = 1e-10
    pv = np.array([p.get(r, 0.0) for r in RATINGS], dtype=float) + eps
    qv = np.array([q.get(r, 0.0) for r in RATINGS], dtype=float) + eps
    pv /= pv.sum()
    qv /= qv.sum()
    m = 0.5 * (pv + qv)
    kl_pm = float(np.sum(pv * np.log(pv / m)))
    kl_qm = float(np.sum(qv * np.log(qv / m)))
    return round(0.5 * (kl_pm + kl_qm), 4)


def _distribution(reviews: list[NormalizedReview]) -> dict[int, float]:
    """Rating fractions per review score for a review list."""
    counts: dict[int, int] = {}
    for r in reviews:
        if r.rating is not None:
            counts[r.rating] = counts.get(r.rating, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {}
    return {r: round(cnt / total, 4) for r, cnt in counts.items()}


@dataclass
class RatingAnomalyEvent:
    """Rating divergence observed for one burst event window.

    ``baseline_dist``/``event_dist`` are fractions over ratings 1..5,
    ``jsd`` is the Jensen-Shannon divergence between them and ``score`` the
    0..100 rating-anomaly component used by the coordinated score.
    """

    place_id: str
    date: date
    baseline_dist: dict[int, float] = field(default_factory=dict)
    event_dist: dict[int, float] = field(default_factory=dict)
    jsd: float = 0.0
    dominant_shift: str = ""
    severity: ConfidenceLevel = ConfidenceLevel.LOW
    score: float = 0.0
    signals: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)

    def to_score_result(self) -> ScoreResult:
        return ScoreResult(
            name="Rating anomaly",
            value=round(self.score, 1),
            confidence=self.severity,
            signals=self.signals,
            counter_signals=self.counter_signals,
            details={
                "jsd": self.jsd,
                "baseline": self.baseline_dist,
                "event": self.event_dist,
                "shift": self.dominant_shift,
            },
        )


class RatingAnomalyDetector:
    """Compare event-window rating distributions against a trailing baseline."""

    def __init__(self, config: RatingAnomalyConfig = CONFIG.rating_anomaly) -> None:
        self.config = config

    def _score_from_jsd(self, jsd: float) -> float:
        """Map JSD onto a 0..100 rating component.

        ``score = clip(jsd / (2 * jsd_threshold), 0, 1) * 100`` — divergence at
        the configured ``jsd_threshold`` contributes 50/100, double it 100/100.
        """
        denom = max(2.0 * self.config.jsd_threshold, 1e-9)
        return float(np.clip(jsd / denom, 0.0, 1.0) * 100.0)

    def _severity(self, jsd: float, event_dist: dict[int, float]) -> ConfidenceLevel:
        share = max(event_dist.values(), default=0.0)
        if jsd >= 2 * self.config.jsd_threshold and (
            share >= self.config.dominant_share_threshold
        ):
            return ConfidenceLevel.HIGH
        if jsd >= self.config.jsd_threshold:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    def _dominant_shift(self, baseline: dict[int, float], event: dict[int, float]) -> str:
        b_max = max(baseline, key=baseline.get) if baseline else 0
        e_max = max(event, key=event.get) if event else 0
        if e_max > b_max:
            return f"shift towards higher ratings (dominant {e_max}★)"
        if e_max < b_max:
            return f"shift towards lower ratings (dominant {e_max}★)"
        return f"dominant rating unchanged ({e_max}★)"

    def detect(
        self,
        reviews: list[NormalizedReview],
        events: list[BurstEvent] | None = None,
    ) -> list[RatingAnomalyEvent]:
        """Return rating anomalies for burst events of every place.

        If ``events`` is omitted, burst events are computed on the fly with a
        default :class:`BurstDetector`.
        """
        if not reviews:
            return []
        # Build a per-place list of (date, review) pairs for fast windows.
        from_review: dict[str, set[NormalizedReview]] = {}

        def _place_reviews(place_id: str, low: date | None, high: date | None) -> list[NormalizedReview]:
            if place_id not in from_review:
                from_review[place_id] = {
                    r for r in reviews if r.place_id == place_id
                }
            selected = from_review[place_id]
            if low is None and high is None:
                return list(selected)
            result = []
            for r in selected:
                day = None
                if r.published_at:
                    day = r.published_at[:10]
                if day is None:
                    continue
                try:
                    d = date.fromisoformat(day)
                except ValueError:
                    continue
                if (low is None or d >= low) and (high is None or d <= high):
                    result.append(r)
            return result

        if events is None:
            events = BurstDetector().detect(reviews)

        anomalies: list[RatingAnomalyEvent] = []
        for event in events:
            if event.observed < self.config.min_reviews_in_window:
                continue
            win = self.config.event_window_days
            event_low = event.date - timedelta(days=(win - 1) // 2)
            event_high = event.date + timedelta(days=win // 2)
            base_low = event.date - timedelta(days=self.config.baseline_window)
            baseline_reviews = _place_reviews(event.place_id, base_low, event_low - timedelta(days=1))
            window_reviews = _place_reviews(event.place_id, event_low, event_high)
            baseline_dist = _distribution(baseline_reviews)
            event_dist = _distribution(window_reviews)
            if not baseline_dist or not event_dist:
                continue
            jsd = _jsd(baseline_dist, event_dist)
            severity = self._severity(jsd, event_dist)
            score = self._score_from_jsd(jsd)
            shift = self._dominant_shift(baseline_dist, event_dist)
            b_str = ", ".join(f"{k}★ {v:.0%}" for k, v in sorted(baseline_dist.items()))
            e_str = ", ".join(f"{k}★ {v:.0%}" for k, v in sorted(event_dist.items()))
            signals = [
                f"Rating distribution changed: baseline {b_str} → event {e_str}",
                f"Jensen-Shannon divergence {jsd:.2f} vs threshold "
                f"{self.config.jsd_threshold:.2f}",
                shift,
            ]
            counter_signals: list[str] = []
            if max(event_dist.values(), default=0.0) < self.config.dominant_share_threshold:
                counter_signals.append(
                    "No single dominant rating in the event window (< "
                    f"{self.config.dominant_share_threshold:.0%})"
                )
            if len(window_reviews) < win:
                counter_signals.append(
                    f"Event window covers {len(window_reviews)} reviews"
                )
            anomalies.append(
                RatingAnomalyEvent(
                    place_id=event.place_id,
                    date=event.date,
                    baseline_dist=baseline_dist,
                    event_dist=event_dist,
                    jsd=jsd,
                    dominant_shift=shift,
                    severity=severity,
                    score=round(score, 1),
                    signals=signals,
                    counter_signals=counter_signals,
                )
            )
        anomalies.sort(key=lambda a: (a.severity.value, -a.score, a.date))
        return anomalies


def rating_anomaly_frame(events: list[RatingAnomalyEvent]) -> pd.DataFrame:
    """Render rating anomalies in a tabular frame for the UI."""
    return pd.DataFrame(
        [
            {
                "place_id": e.place_id,
                "date": e.date.isoformat(),
                "jsd": e.jsd,
                "score": e.score,
                "severity": e.severity.value,
                "baseline": e.baseline_dist,
                "event": e.event_dist,
                "shift": e.dominant_shift,
            }
            for e in events
        ]
    )
