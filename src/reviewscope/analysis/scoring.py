"""Scoring pipeline (SPEC.md §14, §22, §23).

* **Coordinated activity score** (§14) — 0..100 composite of seven
  anomaly signals; each weighted as documented in ``CoordinatedConfig``.
* **Review weight** (§22) — per-review weight in ``[weight_min, weight_max]``
  using reviewer-specific and text-specific signals with documented penalties.
* **Weighted rating** (§23) — per-place raw vs. weighted rating average with
  an explanation of the difference.

Formulas are documented inline and in the ``WeightConfig`` docstring.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from statistics import mean

import numpy as np

from reviewscope.config import CONFIG, CoordinatedConfig, WeightConfig
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel, ScoreResult

# ---------------------------------------------------------------------------
# Coordinated activity score (§14)
# ---------------------------------------------------------------------------


def _normalize(value: float, ceiling: float) -> float:
    """Map ``value`` linearly into 0..1 with saturation at ``ceiling``."""
    if ceiling <= 0:
        return 0.0
    return float(max(0.0, min(1.0, value / ceiling)))


def _temporal_density_signal(reviews: list[NormalizedReview]) -> float:
    """Median interval between same-day and next-day reviews, dense = high."""
    dates: list[date] = []
    for r in reviews:
        raw = (r.published_at or "")[:10]
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            pass
    if len(dates) < 2:
        return 0.0
    dates.sort()
    deltas = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    med = float(np.median(deltas))
    return max(0.0, 1.0 - med / 7.0)


def _reviewer_overlap_signal(reviews: list[NormalizedReview]) -> float:
    """Fraction of reviews belonging to the most prolific reviewer."""
    if not reviews:
        return 0.0
    counts = Counter(r.reviewer_id for r in reviews)
    top = counts.most_common(1)[0][1]
    return top / len(reviews)


def coordinated_activity_score(
    reviews: list[NormalizedReview],
    burst_events: list | None = None,
    rating_anomalies: list | None = None,
    clusters: list | None = None,
    dup_groups: list | None = None,
    templated_results: list | None = None,
    config: CoordinatedConfig = CONFIG.coordinated,
) -> ScoreResult:
    """Coordinated activity score 0..100 (SPEC.md §14).

    Each component contributes ``weight × component_signal`` to the final
    score.  Components compute from the signals of prior analysis phases or
    from lightweight re-derivation (temporal density, reviewer overlap).
    """
    n = len(reviews)
    if n == 0:
        return ScoreResult(
            name="Coordinated Activity",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=[],
            counter_signals=["no reviews"],
        )
    w = config.components

    # --- 1. volume_anomaly ---
    vol_signal = 0.0
    if burst_events:
        vol_signal = _normalize(max(e.z_score for e in burst_events), 10.0)

    # --- 2. rating_anomaly ---
    rat_signal = 0.0
    if rating_anomalies:
        rat_signal = _normalize(max(a.jsd for a in rating_anomalies), 0.3)

    # --- 3. semantic_similarity ---
    sem_signal = 0.0
    if clusters:
        sizes = [len(c.review_ids) for c in clusters]
        sem_signal = max(sizes) / n if n else 0.0

    # --- 4. duplicate_density ---
    dup_ids: set[str] = set()
    if dup_groups:
        for g in dup_groups:
            if len(g.review_ids) >= 3:
                dup_ids.update(g.review_ids)
    dup_signal = len(dup_ids) / n if n else 0.0

    # --- 5. temporal_density ---
    tmp_signal = _temporal_density_signal(reviews)

    # --- 6. template_similarity ---
    tpl_signal = 0.0
    if templated_results:
        tpl_signal = sum(1 for r in templated_results if r.value >= 65) / n

    # --- 7. reviewer_overlap ---
    rov_signal = _reviewer_overlap_signal(reviews)

    components = {
        "volume_anomaly": vol_signal,
        "rating_anomaly": rat_signal,
        "semantic_similarity": sem_signal,
        "duplicate_density": dup_signal,
        "temporal_density": tmp_signal,
        "template_similarity": tpl_signal,
        "reviewer_overlap": rov_signal,
    }

    score = 100.0 * sum(w.get(k, 0.0) * v for k, v in components.items())
    score = round(max(0.0, min(100.0, score)), 1)

    signals: list[str] = []
    counter: list[str] = []
    if vol_signal >= 0.5:
        signals.append(f"review velocity anomaly {vol_signal:.1%} of ceiling")
    if rat_signal >= 0.5:
        signals.append(f"rating distribution anomaly {rat_signal:.1%} intensity")
    if sem_signal >= 0.5:
        signals.append(f"{max(len(c.review_ids) for c in clusters) if clusters else 0} reviews share a semantic cluster")
    if dup_signal >= 0.2:
        signals.append(f"duplicate density {dup_signal:.1%}")
    if tmp_signal >= 0.5:
        signals.append("reviews published in narrow time window")
    if tpl_signal >= 0.2:
        signals.append(f"templated review fraction {tpl_signal:.1%}")
    if rov_signal >= 0.4:
        signals.append(f"single reviewer accounts for {rov_signal:.0%} of reviews")
    if not signals:
        counter.append("no single dominant anomaly signal detected")

    # Counter-signals
    if dup_signal < 0.05:
        counter.append("very few duplicate reviews")
    if rov_signal < 0.1:
        counter.append("reviews spread across many distinct reviewers")

    confidence = (
        ConfidenceLevel.HIGH
        if score >= config.high_threshold
        else ConfidenceLevel.MEDIUM
        if score >= config.medium_threshold
        else ConfidenceLevel.LOW
    )
    return ScoreResult(
        name="Coordinated Activity",
        value=score,
        confidence=confidence,
        signals=signals,
        counter_signals=counter,
        details={k: round(v, 3) for k, v in components.items()},
    )


# ---------------------------------------------------------------------------
# Review weight (§22)
# ---------------------------------------------------------------------------


def _recency_factor(published_at: str | None, half_life: int = 365) -> float:
    if not published_at:
        return 0.0
    try:
        d = date.fromisoformat(str(published_at)[:10])
    except ValueError:
        return 0.0
    days_old = max(0, (date.today() - d).days)
    return 0.5 ** (days_old / max(half_life, 1))


def compute_review_weights(
    reviews: list[NormalizedReview],
    *,
    specificity_map: dict[str, float] | None = None,
    category_experience_map: dict[str, float] | None = None,
    reviewer_relevance_map: dict[str, float] | None = None,
    dup_flagged_ids: set[str] | None = None,
    templated_flagged_ids: set[str] | None = None,
    coordinated_flagged_ids: set[str] | None = None,
    config: WeightConfig = CONFIG.weight,
) -> list[float]:
    """Compute per-review weight in ``[weight_min, weight_max]``.

    Formula (SPEC.md §22):

    .. code-block:: text

        raw_weight =
            specificity       × w_spec
          + category_experience × w_cat
          + reviewer_relevance  × w_rev
          + recency_factor      × w_recency

        raw_weight -=
            duplicate_probability   × p_dup
          + templated_probability   × p_tpl
          + coordinated_probability × p_cas

        weight = clamp(normalized, weight_min, weight_max)

    All component maps are keyed by ``review_id`` with values in 0..1.
    If not provided they default to 0.0.
    """
    dup_flagged_ids = dup_flagged_ids or set()
    templated_flagged_ids = templated_flagged_ids or set()
    coordinated_flagged_ids = coordinated_flagged_ids or set()

    weights: list[float] = []
    for r in reviews:
        rid = r.review_id
        spec = (specificity_map or {}).get(rid, 0.0) / 100.0
        cat = (category_experience_map or {}).get(rid, 0.0) / 100.0
        rel = (reviewer_relevance_map or {}).get(rid, 0.0) / 100.0
        rec = _recency_factor(r.published_at, config.recency_half_life_days)

        dup_p = 1.0 if rid in dup_flagged_ids else 0.0
        tpl_p = 1.0 if rid in templated_flagged_ids else 0.0
        cas_p = 1.0 if rid in coordinated_flagged_ids else 0.0

        raw = (
            config.weight_specificity * spec
            + config.weight_category_experience * cat
            + config.weight_reviewer_relevance * rel
            + config.weight_recency * rec
        )
        raw -= (
            config.penalty_duplicate * dup_p
            + config.penalty_templated * tpl_p
            + config.penalty_coordinated * cas_p
        )
        w = max(config.weight_min, min(config.weight_max, raw))
        weights.append(round(w, 4))
    return weights


# ---------------------------------------------------------------------------
# Weighted rating (§23)
# ---------------------------------------------------------------------------


def weighted_rating(
    reviews: list[NormalizedReview],
    weights: list[float] | None = None,
) -> tuple[float, float, ScoreResult]:
    """Return ``(raw_rating, weighted_rating, explanation)``.

    ``explanation`` is a ``ScoreResult`` carrying the weighted rating as
    ``value`` and signals/counter-signals explaining *why* the weighted
    rating differs from the raw average.
    """
    rated = [(r, w) for r, w in zip(reviews, weights or [1.0] * len(reviews), strict=False)
             if r.rating is not None]
    if not rated:
        return 0.0, 0.0, ScoreResult(
            name="Weighted Rating",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=["no rated reviews"],
        )

    raw = float(mean(r.rating for r, _ in rated))
    total_w = sum(w for _, w in rated)
    if total_w <= 0:
        total_w = 1e-9
    weighted = float(sum(r.rating * w for r, w in rated) / total_w)
    delta = raw - weighted

    signals: list[str] = []
    counter: list[str] = []
    if abs(delta) < 0.05:
        counter.append("weighted and raw ratings are nearly identical")
    if delta > 0:
        signals.append("weighted rating is lower than raw average")
        low_w = sum(1 for _, w in rated if w < 0.6) / len(rated)
        if low_w >= 0.3:
            signals.append(f"{low_w:.0%} of reviews carry low weight (low specificity or flagged)")
    elif delta < 0:
        signals.append("weighted rating is higher than raw average")
        high_w = sum(1 for _, w in rated if w > 1.5) / len(rated)
        if high_w >= 0.2:
            signals.append(f"{high_w:.0%} of reviews carry high weight")

    confidence = ConfidenceLevel.HIGH if len(rated) >= 20 else (
        ConfidenceLevel.MEDIUM if len(rated) >= 5 else ConfidenceLevel.LOW
    )
    result = ScoreResult(
        name="Weighted Rating",
        value=round(weighted, 2),
        confidence=confidence,
        signals=signals,
        counter_signals=counter,
        details={
            "raw": round(raw, 2),
            "delta": round(delta, 3),
        },
    )
    return round(raw, 2), round(weighted, 2), result
