"""Reviewer analytics (SPEC.md §18–§21).

Per reviewer the module computes:

* §18 reviewer metrics — review count, active period, category/city breadth,
  rating mean/std/entropy, mean text length, specificity, diversity, and
  duplicate/template signal ratios;
* §19 category experience score (log-saturated, not linear);
* §20 local familiarity score (city + region activity, place diversity);
* §21 reviewer relevance score (configurable weights, no popularity
  shortcut to trust).

Scores return explainable :class:`ScoreResult` objects (SPEC.md §36).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from reviewscope.config import (
    CONFIG,
    CategoryExperienceConfig,
    LocalFamiliarityConfig,
    ReviewerRelevanceConfig,
)
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel, ScoreResult


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _rating_std(ratings: list[int]) -> float:
    if len(ratings) < 2:
        return 0.0
    return float(np.std(ratings))


def _rating_entropy(ratings: list[int]) -> float:
    """Shannon entropy of the rating histogram normalized to 0..1."""
    if not ratings:
        return 0.0
    hist = np.bincount(ratings, minlength=6)[1:]
    probs = hist / hist.sum()
    probs = probs[probs > 0]
    if len(probs) <= 1:
        return 0.0
    entropy = -float((probs * np.log2(probs)).sum())
    return entropy / math.log2(5.0)


def _log_saturate(count: int, scale: float, base: float) -> float:
    """Saturating transform: ``log_base(count+1) / scale``, clamped to 1."""
    if count <= 0:
        return 0.0
    return min(1.0, math.log(count + 1, base) / scale)


@dataclass
class ReviewerMetrics:
    """Aggregated per-reviewer signals (SPEC.md §18)."""

    reviewer_id: str
    review_count: int = 0
    active_period_days: int = 0
    category_count: int = 0
    city_count: int = 0
    rating_mean: float = 0.0
    rating_std: float = 0.0
    rating_entropy: float = 0.0
    text_length_mean: float = 0.0
    specificity_mean: float = 0.0
    review_diversity: float = 0.0
    duplicate_ratio: float = 0.0
    template_ratio: float = 0.0
    review_consistency: float = 0.0
    category_experience: dict[str, float] = field(default_factory=dict)
    local_familiarity: dict[str, float] = field(default_factory=dict)


def _metric_profile(
    reviewer_id: str, reviews: list[NormalizedReview]
) -> tuple[ReviewerMetrics, list[date | None]]:
    m = ReviewerMetrics(reviewer_id=reviewer_id, review_count=len(reviews))
    if not reviews:
        return m, []

    ratings = [r.rating for r in reviews if r.rating is not None]
    if ratings:
        m.rating_mean = round(float(np.mean(ratings)), 2)
    m.rating_std = round(_rating_std(ratings), 3)
    m.rating_entropy = round(_rating_entropy(ratings), 3)

    lengths = [len(r.text_or_empty()) for r in reviews]
    m.text_length_mean = round(float(np.mean(lengths)), 1)

    m.category_count = len({r.place_category for r in reviews if r.place_category})
    m.city_count = len({r.city for r in reviews if r.city})

    dates = [_parse_date(r.published_at) for r in reviews]
    valid = [d for d in dates if d is not None]
    if valid:
        if len(valid) == 1:
            m.active_period_days = 1
        else:
            m.active_period_days = max(1, (max(valid) - min(valid)).days)

    from reviewscope.analysis.specificity import specificity_score

    specs = [specificity_score(r.text_or_empty()).value for r in reviews]
    m.specificity_mean = round(float(np.mean(specs)), 1)
    m.review_diversity = round(1.0 - _rating_entropy(ratings), 3) if ratings else 0.0

    # Consistency = how concentrated the rating distribution is (low spread).
    m.review_consistency = round(max(0.0, 1.0 - m.rating_std / 2.0), 3)
    return m, dates


def compute_reviewer_metrics(
    reviews: list[NormalizedReview],
    duplicate_ids: set[str] | None = None,
    templated_ids: set[str] | None = None,
) -> list[ReviewerMetrics]:
    """Aggregate §18 metrics for every reviewer in ``reviews``.

    ``duplicate_ids`` / ``templated_ids`` are optional precomputed flagged
    review id sets; when omitted the module derives them from the duplicate and
    templated detectors (text-only, deterministic). This keeps ``duplicate_ratio``
    and ``template_ratio`` populated without requiring embeddings.
    """
    if not reviews:
        return []

    groups: dict[str, list[NormalizedReview]] = defaultdict(list)
    for r in reviews:
        groups[r.reviewer_id].append(r)

    if duplicate_ids is None:
        from reviewscope.analysis.duplicates import DuplicateDetector

        dup_groups = DuplicateDetector().detect(reviews, embeddings=None)
        duplicate_ids = {
            rid
            for g in dup_groups
            if len(g.review_ids) >= 3
            for rid in g.review_ids
        }
    if templated_ids is None:
        from reviewscope.analysis.templated import TemplatedTextScorer

        templated_results = TemplatedTextScorer().score(reviews)
        templated_ids = {
            reviews[i].review_id
            for i, res in enumerate(templated_results)
            if res.value >= 65
        }

    metrics: list[ReviewerMetrics] = []
    for reviewer_id, history in sorted(groups.items()):
        m, _ = _metric_profile(reviewer_id, history)
        if history:
            flagged_dup = sum(1 for r in history if r.review_id in duplicate_ids)
            flagged_tpl = sum(1 for r in history if r.review_id in templated_ids)
            m.duplicate_ratio = round(flagged_dup / len(history), 3)
            m.template_ratio = round(flagged_tpl / len(history), 3)
        metrics.append(m)
    return metrics


# ---------------------------------------------------------------------------
# §19 Category experience
# ---------------------------------------------------------------------------


def category_experience_score(
    reviewer_history: list[NormalizedReview],
    category: str | None,
    config: CategoryExperienceConfig = CONFIG.category_experience,
) -> ScoreResult:
    """Category Experience Score 0..100 (SPEC.md §19), log-saturated."""
    if not category:
        return ScoreResult(
            name="Category Experience",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=[],
            counter_signals=["no category metadata"],
        )

    in_cat = [r for r in reviewer_history if r.place_category == category]
    n_cat = len(in_cat)
    total = len(reviewer_history)

    if n_cat == 0:
        return ScoreResult(
            name="Category Experience",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=[],
            counter_signals=[f"no reviews of {category} category"],
        )

    count_score = _log_saturate(n_cat, config.log_scale, config.log_base)
    # A share is only meaningful once the reviewer has several reviews; gate it
    # by a saturating transform of the total history so a single-review account
    # does not get an inflated category share (SPEC.md §19 non-linear).
    depth_gate = _log_saturate(total, config.log_scale, config.log_base)
    share_score = (n_cat / total if total else 0.0) * depth_gate
    dates = [d for d in (_parse_date(r.published_at) for r in in_cat) if d is not None]
    active_score = 0.0
    if len(dates) >= 2:
        active_score = min(1.0, (max(dates) - min(dates)).days / 730.0)
    diversity = len({r.place_id for r in in_cat})
    diversity_score = min(1.0, diversity / 5.0)

    value = 100.0 * (
        config.count_weight * count_score
        + config.category_share_weight * share_score
        + config.active_period_weight * active_score
        + config.diversity_weight * diversity_score
    )
    value = round(max(0.0, min(100.0, value)), 1)

    signals: list[str] = []
    counter: list[str] = []
    if n_cat >= 5:
        signals.append(f"{n_cat} reviews in {category}")
    if share_score >= 0.5:
        signals.append(f"{category} is {share_score:.0%} of reviewer history")
    if len(dates) >= 2:
        signals.append("active period spans months")
    if diversity >= 3:
        signals.append(f"reviewed {diversity} different places in {category}")
    if not signals or value < 30:
        counter.append("thin category history")

    confidence = (
        ConfidenceLevel.HIGH
        if value >= 60
        else ConfidenceLevel.MEDIUM
        if value >= 30
        else ConfidenceLevel.LOW
    )
    return ScoreResult(
        name="Category Experience",
        value=value,
        confidence=confidence,
        signals=signals,
        counter_signals=counter,
        details={
            "count": n_cat,
            "share": round(share_score, 3),
            "active": round(active_score, 3),
            "diversity": diversity,
        },
    )


# ---------------------------------------------------------------------------
# §20 Local familiarity
# ---------------------------------------------------------------------------


def local_familiarity_score(
    reviewer_history: list[NormalizedReview],
    city: str | None,
    region: str | None = None,
    config: LocalFamiliarityConfig = CONFIG.local_familiarity,
) -> ScoreResult:
    """Local Familiarity Score 0..100 (SPEC.md §20).

    Measures activity volume, place diversity and duration *in the requested
    location*. It does not predict residence. Low score with history elsewhere
    is reported as travel context, not as a negative signal.

    Remediation (audit §20/§21): city and region are separate components.
    ``region`` matches the region but *excludes* exact-city reviews (so the
    region component measures same-region, different-city familiarity).  The
    final score blends 75% exact-city and 25% same-region evidence, and falls
    back to a pure region component when no exact-city history exists.
    """
    location = city or region
    if not location:
        return ScoreResult(
            name="Local Familiarity",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=[],
            counter_signals=["no location metadata"],
        )

    # Exact-city and same-region cohorts. Region excludes exact-city matches so
    # the two components do not double-count the same reviews.
    city_reviews = [r for r in reviewer_history if city and r.city == city]
    if region:
        region_reviews = [
            r for r in reviewer_history if r.region == region and r.city != city
        ]
    else:
        region_reviews = []
    # No exact city metadata -> fall back to the region component alone.
    if not city_reviews and not region_reviews:
        return ScoreResult(
            name="Local Familiarity",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=[],
            counter_signals=[f"no reviews near {location}"],
        )

    def component(group: list[NormalizedReview]) -> float:
        if not group:
            return 0.0
        activity = _log_saturate(len(group), config.log_scale, 10.0)
        diversity = min(1.0, len({r.place_id for r in group}) / 3.0)
        dates = [d for d in (_parse_date(r.published_at) for r in group) if d is not None]
        duration = min(1.0, (max(dates) - min(dates)).days / 365.0) if len(dates) >= 2 else 0.0
        return 100.0 * (
            config.place_diversity_weight * diversity
            + config.activity_weight * activity
            + config.duration_weight * duration
        )

    city_comp = component(city_reviews)
    region_comp = component(region_reviews)
    if city_reviews and region_reviews:
        value = 0.75 * city_comp + 0.25 * region_comp
    elif city_reviews:
        value = city_comp
    else:
        value = region_comp
    value = round(max(0.0, min(100.0, value)), 1)

    in_loc = city_reviews or region_reviews
    places = {r.place_id for r in in_loc}
    n_total = len(in_loc)

    signals: list[str] = []
    counter: list[str] = []
    if value >= 50:
        signals.append(f"substantial review history in {location}")
    if len(places) >= 2:
        signals.append(f"reviews {len(places)} places in {location}")
    if city_reviews:
        signals.append(f"{len(city_reviews)} reviews in {city}")
    elif region_reviews:
        signals.append(f"{len(region_reviews)} reviews in {region} (no exact-city history)")
    if n_total >= 3:
        signals.append(f"{n_total} reviews in or near {location}")
    if value < 25:
        counter.append(f"very little prior activity in {location}")

    # Travel-context counter-signal: reviewer has significant history elsewhere.
    other_loc = len({r.city for r in reviewer_history}) > 1 if city else (
        len({r.region for r in reviewer_history}) > 1 if region else False
    )
    if value < 50 and other_loc:
        counter.append(
            "reviewer reviews this location outside their usual regions (travel context)"
        )

    confidence = (
        ConfidenceLevel.HIGH if value >= 60 else ConfidenceLevel.MEDIUM if value >= 30 else ConfidenceLevel.LOW
    )
    return ScoreResult(
        name="Local Familiarity",
        value=value,
        confidence=confidence,
        signals=signals,
        counter_signals=counter,
        details={
            "reviews": n_total,
            "city_reviews": len(city_reviews),
            "region_reviews": len(region_reviews),
            "places": len(places),
            "duration": round(component(in_loc) / 100.0, 3),
        },
    )


# ---------------------------------------------------------------------------
# §21 Reviewer relevance
# ---------------------------------------------------------------------------


def reviewer_relevance_score(
    reviewer_history: list[NormalizedReview],
    category: str | None,
    city: str | None,
    region: str | None = None,
    config: ReviewerRelevanceConfig = CONFIG.reviewer_relevance,
    cat_config: CategoryExperienceConfig = CONFIG.category_experience,
    local_config: LocalFamiliarityConfig = CONFIG.local_familiarity,
) -> ScoreResult:
    """Reviewer Relevance Score 0..100 (SPEC.md §21), configurable weights."""
    from reviewscope.analysis.specificity import specificity_score

    w = config.weights
    total_reviews = len(reviewer_history)

    cat_exp = category_experience_score(
        reviewer_history, category, cat_config
    ).value / 100.0

    specs = [specificity_score(r.text_or_empty()).value for r in reviewer_history]
    spec_history = (sum(specs) / len(specs)) / 100.0 if specs else 0.0

    ratings = [r.rating for r in reviewer_history if r.rating is not None]
    rating_std = _rating_std(ratings) if ratings else 0.0
    consistency = max(0.0, 1.0 - rating_std / 2.0)

    local = local_familiarity_score(
        reviewer_history, city, region, local_config
    ).value / 100.0

    depth = min(1.0, math.log(total_reviews + 1, 10) / 2.0)

    value = 100.0 * (
        w.get("category_experience", 0.0) * cat_exp
        + w.get("specificity_history", 0.0) * spec_history
        + w.get("review_consistency", 0.0) * consistency
        + w.get("local_context", 0.0) * local
        + w.get("history_depth", 0.0) * depth
    )
    value = round(max(0.0, min(100.0, value)), 1)

    signals: list[str] = []
    counter: list[str] = []
    if cat_exp >= 0.6:
        signals.append("deep category experience")
    elif cat_exp <= 0.15:
        counter.append("little category experience")
    if spec_history >= 0.6:
        signals.append("historically specific reviews")
    if consistency >= 0.7:
        signals.append("consistent rating behavior")
    if local >= 0.6:
        signals.append("strong local context")
    if depth >= 0.6:
        signals.append("substantial review history")
    elif total_reviews == 0:
        counter.append("no review history")

    confidence = (
        ConfidenceLevel.HIGH if value >= 60 else ConfidenceLevel.MEDIUM if value >= 30 else ConfidenceLevel.LOW
    )
    return ScoreResult(
        name="Reviewer Relevance",
        value=value,
        confidence=confidence,
        signals=signals,
        counter_signals=counter,
        details={
            "category_experience": round(cat_exp, 3),
            "specificity_history": round(spec_history, 3),
            "review_consistency": round(consistency, 3),
            "local_context": round(local, 3),
            "history_depth": round(depth, 3),
        },
    )


def reviewers_frame(metrics: list[ReviewerMetrics]) -> np.ndarray | object:
    """Render reviewer metrics into a UI-friendly DataFrame."""
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "reviewer_id": m.reviewer_id,
                "reviews": m.review_count,
                "active_period_days": m.active_period_days,
                "categories": m.category_count,
                "cities": m.city_count,
                "rating_mean": m.rating_mean,
                "rating_std": m.rating_std,
                "rating_entropy": m.rating_entropy,
                "text_length_mean": m.text_length_mean,
                "specificity_mean": m.specificity_mean,
                "review_consistency": m.review_consistency,
                "duplicate_ratio": m.duplicate_ratio,
                "template_ratio": m.template_ratio,
            }
            for m in metrics
        ]
    )
