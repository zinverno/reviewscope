"""Scoring pipeline (SPEC.md §14, §22, §23).

* **Coordinated activity score** (§14) — 0..100 composite of seven
  anomaly signals; each weighted as documented in ``CoordinatedConfig``.
* **Graded coordinated probability** (§22 → §8) — per-review probability in
  0..1 that the review took part in a coordinated manipulation event.  This
  replaces the binary flag (audit §22): a review on a burst day that is also
  templated/duplicate gets a high probability, an organic review that merely
  happens to be published during a busy day gets a bounded probability.
* **Review weight** (§22) — per-review weight in ``[weight_min, weight_max]``
  with a documented *derived* range: neutral evidence -> 1.0, best quality
  and no penalties -> 2.0, worst penalties -> 0.25.
* **Weighted rating** (§23) — per-place raw vs. weighted rating average with
  an explanation of the difference.

Formulas are documented inline and in the ``WeightConfig`` docstring.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
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


def _to_date(published_at: str | None) -> date | None:
    raw = (published_at or "")[:10]
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _event_window_dates(
    burst_events: list | None, rating_anomalies: list | None
) -> set[date]:
    """Dates flagged as manipulation-relevant by prior analysis phases."""
    window: set[date] = set()
    for e in burst_events or []:
        if getattr(e, "date", None):
            window.add(e.date)
    # Rating anomalies are computed for an event window around a burst date;
    # include the neighbouring days so the semantic overlap can use them.
    for a in rating_anomalies or []:
        base = getattr(a, "date", None)
        if base:
            for delta in range(-1, 2):
                window.add(base + timedelta(days=delta))
    return window


def _temporal_density_signal(reviews: list[NormalizedReview]) -> float:
    """Median interval between same-day and next-day reviews, dense = high."""
    dates: list[date] = []
    for r in reviews:
        d = _to_date(r.published_at)
        if d:
            dates.append(d)
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


def _per_review_temporal_signal(
    reviews: list[NormalizedReview], event_dates: set[date]
) -> dict[str, float]:
    """Fraction of same-place reviews published on the event-window dates."""
    if not event_dates:
        return {}
    by_place: dict[str, int] = Counter()
    on_event: dict[str, int] = Counter()
    for r in reviews:
        place = r.place_id or ""
        by_place[place] += 1
        d = _to_date(r.published_at)
        if d and d in event_dates:
            on_event[place] += 1
    out: dict[str, float] = {}
    # Compute per-review signal by looking the place back up.
    place_review_count: dict[str, int] = dict(by_place)
    place_event_count: dict[str, int] = dict(on_event)
    for r in reviews:
        place = r.place_id or ""
        total = place_review_count.get(place, 0)
        if total == 0:
            out[r.review_id] = 0.0
            continue
        d = _to_date(r.published_at)
        on_flag = 1.0 if (d and d in event_dates) else 0.0
        # Blend the review's own placement with the cohort's event density so
        # that a single organic review on a busy day keeps a bounded signal.
        pool = max(0.0, place_event_count.get(place, 0) - (1 if on_flag else 0))
        out[r.review_id] = round(0.5 * on_flag + 0.5 * min(1.0, pool / max(total, 1)), 3)
    return out


def _rating_strength_map(
    reviews: list[NormalizedReview], rating_anomalies: list | None
) -> dict[str, float]:
    """Per-event strength of the dominant rating shift (0..1)."""
    out: dict[str, float] = {}
    for a in rating_anomalies or []:
        jsd = getattr(a, "jsd", 0.0) or 0.0
        strength = _normalize(jsd, 0.3)
        if strength <= 0:
            continue
        event_ratings = dict(getattr(a, "event_dist", {}) or {})
        if not event_ratings:
            continue
        dominant = max(event_ratings, key=event_ratings.get)
        agreement = event_ratings[dominant]
        if agreement >= 0.7:
            rat_agreement = 1.0
        elif agreement >= 0.5:
            rat_agreement = 0.5
        elif agreement >= 0.3:
            rat_agreement = 0.15
        else:
            rat_agreement = 0.0
        for r in reviews:
            if _to_date(r.published_at) in {
                a.date + timedelta(days=delta) for delta in range(-1, 2)
            }:
                out[r.review_id] = max(out.get(r.review_id, 0.0), strength * rat_agreement)
    return out


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

    The semantic component is **event-relative** (audit §14/§20): a large
    topic cluster only contributes if it overlaps actual manipulation event
    windows (burst/rating-anomaly dates), so a place with genuinely popular
    same-topic organic reviews over many months keeps a low semantic signal.
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
    event_dates = _event_window_dates(burst_events, rating_anomalies)

    # --- 1. volume_anomaly ---
    vol_signal = 0.0
    if burst_events:
        vol_signal = _normalize(max(e.z_score for e in burst_events), 10.0)

    # --- 2. rating_anomaly ---
    rat_signal = 0.0
    if rating_anomalies:
        rat_signal = _normalize(max(a.jsd for a in rating_anomalies), 0.3)

    # --- 3. semantic_similarity (event-relative) ---
    sem_signal = 0.0
    if clusters and event_dates:
        event_review_ids = {
            r.review_id for r in reviews if _to_date(r.published_at) in event_dates
        }
        if event_review_ids:
            min_sim = CONFIG.topic.min_mean_similarity
            for c in clusters:
                if getattr(c, "similarity", 0.0) < min_sim:
                    continue
                overlap = len(set(c.review_ids) & event_review_ids)
                if overlap >= config.semantic_event_overlap_min:
                    sem_signal = max(sem_signal, overlap / n)

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
        signals.append(
            "semantic cluster overlaps a manipulation event window"
        )
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


def coordinated_review_probabilities(
    reviews: list[NormalizedReview],
    burst_events: list | None = None,
    rating_anomalies: list | None = None,
    dup_groups: list | None = None,
    templated_results: list | None = None,
    clusters: list | None = None,
    config: CoordinatedConfig = CONFIG.coordinated,
) -> dict[str, float]:
    """Graded probability that each review participated in coordination.

    Returns ``{review_id: 0..1}``.  Weights sum to 1.0
    (``config.review_probability_components``):

    .. code-block:: text

        p = 0.35 * event_participation
          + 0.30 * duplicate_or_templated
          + 0.20 * peer_semantic(event-window cluster overlap)
          + 0.15 * temporal_density

    Semantics (audit §22: "the review-level coordinated flag is a binary
    for engineered events; the weight penalty needs a graded probability"):

    * a review published on a manipulation event day with an aligned rating
      shift and templated/duplicate peers gets a high probability;
    * a genuine organic review that merely falls on a busy day scores a
      bounded ``temporal_density`` contribution and does not exceed ~0.3
      unless other evidence stacks up.

    ``templated_results[i]`` must correspond to ``reviews[i]``.
    """
    event_dates = _event_window_dates(burst_events, rating_anomalies)
    w = config.review_probability_components

    # ---- event participation: burst strength x rating agreement ----
    event_participation: dict[str, float] = {}
    if burst_events:
        strength_by_date: dict[date, float] = {}
        for e in burst_events:
            strength_by_date[e.date] = max(
                strength_by_date.get(e.date, 0.0),
                _normalize(e.z_score, 10.0),
            )
        rating_strength = _rating_strength_map(reviews, rating_anomalies)
        for r in reviews:
            d = _to_date(r.published_at)
            if not d:
                event_participation[r.review_id] = 0.0
                continue
            on_event = any((d == ed or abs((d - ed).days) <= 1) for ed in event_dates)
            if not on_event:
                event_participation[r.review_id] = 0.0
                continue
            burst = max((s for ed, s in strength_by_date.items() if abs((d - ed).days) <= 1), default=0.0)
            rat = rating_strength.get(r.review_id, 0.0)
            event_participation[r.review_id] = round(burst * (0.5 + 0.5 * rat), 3)

    # ---- duplicate_or_templated ----
    dup_tpl: dict[str, float] = {}
    if dup_groups:
        for g in dup_groups:
            members = list(getattr(g, "review_ids", []))
            if len(members) < 3:
                continue
            avg_sim = min(
                1.0,
                max(getattr(g, "mean_similarity", getattr(g, "avg_similarity", 0.0)) or 0.0, 0.5),
            )
            group_prob = round(min(1.0, len(members) / 5.0) * avg_sim, 3)
            for rid in members:
                dup_tpl[rid] = max(dup_tpl.get(rid, 0.0), group_prob)
    for r, res in zip(reviews, templated_results or [], strict=False):
        if res.value > 0:
            dup_tpl[r.review_id] = max(
                dup_tpl.get(r.review_id, 0.0), round(min(1.0, res.value / 100.0), 3)
            )

    # ---- peer_semantic: cluster overlap confined to the event window ----
    peer_sem: dict[str, float] = {}
    if clusters and event_dates:
        min_sim = CONFIG.topic.min_mean_similarity
        event_review_ids = {
            r.review_id for r in reviews if _to_date(r.published_at) in event_dates
        }
        cluster_members: set[str] = set()
        for c in clusters:
            if getattr(c, "similarity", 0.0) < min_sim:
                continue
            members = set(c.review_ids)
            if len(members & event_review_ids) >= config.semantic_event_overlap_min:
                cluster_members |= members
        if cluster_members:
            for rid in cluster_members:
                peer_sem[rid] = 0.5

    # ---- temporal density ----
    temporal = _per_review_temporal_signal(reviews, event_dates)

    probs: dict[str, float] = {}
    for r in reviews:
        p = (
            w.get("event_participation", 0.0) * event_participation.get(r.review_id, 0.0)
            + w.get("duplicate_templated", 0.0) * dup_tpl.get(r.review_id, 0.0)
            + w.get("peer_semantic", 0.0) * peer_sem.get(r.review_id, 0.0)
            + w.get("temporal_density", 0.0) * temporal.get(r.review_id, 0.0)
        )
        probs[r.review_id] = round(min(1.0, p), 3)
    return probs


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
    duplicate_probability: dict[str, float] | None = None,
    templated_probability: dict[str, float] | None = None,
    coordinated_probability: dict[str, float] | None = None,
    config: WeightConfig = CONFIG.weight,
) -> list[float]:
    """Compute per-review weight in ``[weight_min, weight_max]``.

    Formula (SPEC.md §22, remediation math):

    .. code-block:: text

        quality  = specificity       * w_spec   (0..1)
                 + category_experience * w_cat   (0..1)
                 + reviewer_relevance  * w_rev   (0..1)
                 + recency_factor      * w_recency (0..1)

        excess   = max(0, quality - neutral_quality)          (0..0.5)
        penalty  = penalty_duplicate   * duplicate_prob
                 + penalty_templated   * templated_prob
                 + penalty_coordinated * coordinated_prob     (0..1)

        weight   = clamp(1.0 + rise_factor * excess - penalty,
                         weight_min, weight_max)

    Derived range (verified by ``TestWeightFormula``):
    * neutral quality 1.0 == ``neutral_quality`` and no penalties -> 1.0;
    * maximum quality (1.0) with no penalties -> ``weight_max``;
    * all penalties at 1.0 -> ``weight_min``;
    * the exact [``weight_min``, ``weight_max``] range is reachable.

    Component maps are keyed by ``review_id`` with values in 0..1
    (graded probabilities, not flags — audit §22).  Missing keys default to
    0.0.  ``duplicate_probability``/``templated_probability`` arise from
    duplicate and templated analysis; ``coordinated_probability`` from
    :func:`coordinated_review_probabilities`.
    """
    weights: list[float] = []
    for r in reviews:
        rid = r.review_id
        spec = (specificity_map or {}).get(rid, 0.0) / 100.0
        cat = (category_experience_map or {}).get(rid, 0.0) / 100.0
        rel = (reviewer_relevance_map or {}).get(rid, 0.0) / 100.0
        rec = _recency_factor(r.published_at, config.recency_half_life_days)

        quality = (
            config.weight_specificity * spec
            + config.weight_category_experience * cat
            + config.weight_reviewer_relevance * rel
            + config.weight_recency * rec
        )
        excess = max(0.0, quality - config.neutral_quality)
        penalty = (
            config.penalty_duplicate * (duplicate_probability or {}).get(rid, 0.0)
            + config.penalty_templated * (templated_probability or {}).get(rid, 0.0)
            + config.penalty_coordinated * (coordinated_probability or {}).get(rid, 0.0)
        )
        raw = 1.0 + config.rise_factor * excess - penalty
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
