"""Unit tests for the scoring pipeline (SPEC.md §14, §22, §23).

These tests encode the forensic-remediation invariants:

* the weight formula's upper bound ``weight_max`` is *reachable*
  (audit §22: the old formula could not reach 2.0) while ``weight_min``
  stays configured as a safety clamp for malformed inputs;
* neutral evidence maps to weight 1.0;
* penalties are *graded probabilities*, not binary flags (audit §22);
* duplicate/templated text reuse is charged exactly once: the coordinated
  term carries only coordinated evidence of its own — event, peer and
  temporal (Phase 17I, architecture E);
* coordinated review probability is graded: an engineered review is high,
  an organic review that merely falls on a busy day stays bounded;
* the coordinated place semantic component is event-relative (audit
  §14/§20): a big same-topic organic cluster with no manipulation event
  does not inflate the score.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from reviewscope.analysis.bursts import BurstEvent
from reviewscope.analysis.duplicates import DuplicateGroup
from reviewscope.analysis.rating_anomalies import RatingAnomalyEvent
from reviewscope.analysis.scoring import (
    compute_review_weights,
    coordinated_activity_score,
    coordinated_review_probabilities,
    coordinated_text_reuse_weight,
    weighted_rating,
)
from reviewscope.analysis.templated import TemplatedTextScorer
from reviewscope.analysis.topics import TopicCluster
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel

_TODAY = date.today()


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


def _burst(day: date, z: float = 20.0) -> BurstEvent:
    return BurstEvent(
        place_id="p1", date=day, expected=2.0, observed=40,
        multiplier=20.0, z_score=z,
        severity=ConfidenceLevel.HIGH, score=100.0,
    )


def _cluster(review_ids: list[str], similarity: float = 0.9) -> TopicCluster:
    return TopicCluster(
        cluster_id=0, review_ids=review_ids,
        place_ids=["p1"], avg_rating=5.0,
        date_min="2026-09-01", date_max="2026-09-04",
        similarity=similarity, representative_phrases=[], representative_reviews=[],
    )


class TestCoordinatedActivity:
    def test_empty(self) -> None:
        r = coordinated_activity_score([])
        assert r.value == 0.0
        assert r.confidence.value == "LOW"

    def test_burst_high_drives_score(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 9, 2)) for i in range(30)]
        r = coordinated_activity_score(reviews, burst_events=[_burst(date(2026, 9, 2))])
        assert r.value >= 30

    def test_all_anomalies_max(self) -> None:
        reviews = [
            _review(f"r{i}", "p1", date(2026, 9, 1 + i % 4), reviewer=f"u{i % 2}")
            for i in range(40)
        ]
        cluster = _cluster([f"r{i}" for i in range(35)])
        tpl = TemplatedTextScorer().score(reviews)
        r = coordinated_activity_score(
            reviews,
            burst_events=[_burst(date(2026, 9, 2))],
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

    def test_semantic_cluster_outside_event_window_does_not_boost(self) -> None:
        # Forensic regression (P5, audit §14/§20): a large semantically
        # coherent organic cluster spread over months with no manipulation
        # event must NOT contribute the semantic component.
        reviews = [
            _review(f"r{i}", "p1", date(2026, 1, 1) + timedelta(days=i), rating=4 + i % 2)
            for i in range(60)
        ]
        cluster = _cluster([f"r{i}" for i in range(55)])
        r = coordinated_activity_score(reviews, clusters=[cluster])
        assert r.details["semantic_similarity"] == 0.0
        assert r.value < 40

    def test_semantic_cluster_inside_event_window_counts(self) -> None:
        reviews = [
            _review(f"r{i}", "p1", date(2026, 9, 2), rating=5) for i in range(30)
        ]
        cluster = _cluster([f"r{i}" for i in range(25)])
        r = coordinated_activity_score(
            reviews,
            burst_events=[_burst(date(2026, 9, 2), z=20.0)],
            clusters=[cluster],
        )
        assert r.details["semantic_similarity"] > 0.0


class TestReviewWeight:
    def test_weight_min_max(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 1, i + 1)) for i in range(5)]
        weights = compute_review_weights(reviews)
        assert all(0.25 <= w <= 2.0 for w in weights)

    def test_max_quality_reaches_upper_clamp(self) -> None:
        # Forensic regression (audit §22): the old formula never reached 2.0.
        reviews = [_review("best", "p1", _TODAY)]
        specs = {"best": 100.0}
        cat = {"best": 100.0}
        rel = {"best": 100.0}
        [w] = compute_review_weights(
            reviews, specificity_map=specs, category_experience_map=cat,
            reviewer_relevance_map=rel,
        )
        assert w == 2.0

    def test_worst_penalties_reach_reachable_minimum(self) -> None:
        # Phase 17I (architecture E): the coordinated term only carries the
        # residual beyond the text-reuse share, so bounded inputs bottom out
        # at 0.30 + 0.25 + 0.20 * (1 - 0.30) = 0.69 -> weight 0.31.
        reviews = [_review("worst", "p1", date(2000, 1, 1))]
        [w] = compute_review_weights(
            reviews,
            duplicate_probability={"worst": 1.0},
            templated_probability={"worst": 1.0},
            coordinated_probability={"worst": 1.0},
        )
        assert w == 0.31
        # weight_min stays configured as a safety clamp for malformed inputs;
        # it is no longer a reachable all-penalties result.
        assert CONFIG.weight.weight_min == 0.25
        assert w > CONFIG.weight.weight_min

    def test_neutral_evidence_maps_to_one(self) -> None:
        # quality == neutral_quality => weight == 1.0 exactly (SPEC §22).
        reviews = [_review("mid", "p1", date(2000, 1, 1))]
        # quality = 0.35*1.0 + 0.20*0.75 + 0.25*0.0 + 0.20*0.0 = 0.50
        [w] = compute_review_weights(
            reviews,
            specificity_map={"mid": 100.0},
            category_experience_map={"mid": 75.0},
        )
        assert w == 1.0

    def test_specificity_raises_weight(self) -> None:
        reviews = [
            _review(f"r{i}", "p1", _TODAY, text="стейк за 1400 рублей")
            for i in range(3)
        ]
        specificity_map = {"r0": 100.0, "r1": 100.0, "r2": 100.0}
        w_no = compute_review_weights(reviews)
        w_high = compute_review_weights(reviews, specificity_map=specificity_map)
        assert sum(w_high) > sum(w_no)

    def test_duplicate_penalty_is_graded(self) -> None:
        # Forensic regression (audit §22): partial probability -> partial penalty.
        reviews = [
            _review(f"r{i}", "p1", date(2026, 9, 1 + i), rating=4,
                    text=f"стейк за 1400 рублей с кровью {i}")
            for i in range(3)
        ]
        specificity_map = {f"r{i}": 100.0 for i in range(3)}
        w_none = compute_review_weights(
            reviews, specificity_map=specificity_map,
            duplicate_probability={"r0": 0.0},
        )[0]
        w_half = compute_review_weights(
            reviews, specificity_map=specificity_map,
            duplicate_probability={"r0": 0.5},
        )[0]
        w_full = compute_review_weights(
            reviews, specificity_map=specificity_map,
            duplicate_probability={"r0": 1.0},
        )[0]
        assert w_full < w_half < w_none
        assert pytest.approx(w_none - w_full) == 0.30  # penalty_duplicate

    def test_templated_and_coordinated_text_reuse_not_double_charged(self) -> None:
        # Phase 17H/17I: a coordinated probability that is purely the
        # text-reuse component adds nothing on top of the templated penalty.
        reviews = [_review("r0", "p1", date(2026, 9, 1), text="стейк с кровью за 1400")]
        specs = {"r0": 100.0}
        w_tpl = compute_review_weights(reviews, specificity_map=specs,
                                       templated_probability={"r0": 1.0})[0]
        w_stack = compute_review_weights(
            reviews, specificity_map=specs,
            templated_probability={"r0": 1.0},
            coordinated_probability={"r0": round(coordinated_text_reuse_weight(), 3)},
        )[0]
        assert w_stack == w_tpl

    def test_coordinated_residual_stacks_with_templated(self) -> None:
        # Only the coordinated evidence beyond text reuse is charged, at
        # penalty_coordinated.
        reviews = [_review("r0", "p1", date(2026, 9, 1), text="стейк с кровью за 1400")]
        specs = {"r0": 100.0}
        residual = 0.15
        coord = round(coordinated_text_reuse_weight() * 1.0 + residual, 3)
        w_tpl = compute_review_weights(reviews, specificity_map=specs,
                                       templated_probability={"r0": 1.0})[0]
        w_stack = compute_review_weights(
            reviews, specificity_map=specs,
            templated_probability={"r0": 1.0},
            coordinated_probability={"r0": coord},
        )[0]
        assert w_stack < w_tpl
        assert w_tpl - w_stack == pytest.approx(
            CONFIG.weight.penalty_coordinated * residual, abs=2e-4
        )


class TestArchitectureETextReuse:
    """Phase 17I regressions: text reuse is charged exactly once."""

    def _weight(
        self,
        dup: float,
        tpl: float,
        coord: float,
    ) -> float:
        reviews = [_review("r0", "p1", date(2000, 1, 1))]
        return compute_review_weights(
            reviews,
            duplicate_probability={"r0": dup},
            templated_probability={"r0": tpl},
            coordinated_probability={"r0": coord},
        )[0]

    @pytest.mark.parametrize(
        ("dup", "tpl"),
        [(d, t) for d in (0.0, 0.5, 1.0) for t in (0.0, 0.5, 1.0)],
    )
    def test_text_reuse_only_coordination_is_not_charged(self, dup: float, tpl: float) -> None:
        # A. coordinated_probability set to exactly the production text-reuse
        # component must cost nothing: weight(coord) == weight(0).
        # Fails on architecture A, which charged the same evidence twice.
        text_reuse = round(coordinated_text_reuse_weight() * max(dup, tpl), 3)
        assert self._weight(dup, tpl, text_reuse) == self._weight(dup, tpl, 0.0)

    def test_independent_coordinated_residual_charges_exactly(self) -> None:
        # B. text-reuse component + residual: only the residual is charged.
        dup, tpl = 0.5, 0.0
        residual = 0.15
        coord = round(round(coordinated_text_reuse_weight() * max(dup, tpl), 3) + residual, 3)
        w0 = self._weight(dup, tpl, 0.0)
        w1 = self._weight(dup, tpl, coord)
        assert w1 < w0
        assert w0 - w1 == pytest.approx(
            CONFIG.weight.penalty_coordinated * residual, abs=2e-4
        )

    @pytest.mark.parametrize("coord", [0.05, 0.29])
    def test_residual_never_becomes_a_bonus(self, coord: float) -> None:
        # C. malformed input: coordinated_probability smaller than the
        # text-reuse share it should contain -> residual clamps to 0.
        text_reuse = round(coordinated_text_reuse_weight() * 1.0, 3)
        assert coord < text_reuse
        assert self._weight(1.0, 1.0, coord) == self._weight(1.0, 1.0, 0.0)

    def test_real_non_text_coordinated_evidence_still_charges(self) -> None:
        # D. genuine event / peer / temporal coordination (Vermont has none of
        # these, so the corpus alone cannot prove this path survives).
        day = date(2026, 9, 2)
        text = "Всё было идеально, обслуживание на высоте, рекомендую всем!"
        reviews = [_review(f"r{i}", "p1", day, rating=5, text=text) for i in range(12)]
        anomaly = RatingAnomalyEvent(
            place_id="p1", date=day,
            baseline_dist={r: 0.2 for r in range(1, 6)},
            event_dist={5: 0.9, 4: 0.1},
            jsd=0.4, dominant_shift="5-stars", score=100.0,
        )
        coord = coordinated_review_probabilities(
            reviews,
            burst_events=[_burst(day)],
            rating_anomalies=[anomaly],
            clusters=[_cluster([f"r{i}" for i in range(12)])],
        )
        assert all(v > 0.0 for v in coord.values())

        w0 = compute_review_weights(reviews)
        w1 = compute_review_weights(reviews, coordinated_probability=coord)
        assert all(a > b for a, b in zip(w0, w1, strict=True))
        assert w0[0] - w1[0] == pytest.approx(
            CONFIG.weight.penalty_coordinated * coord["r0"], abs=2e-4
        )

        # ... and it still charges on top of full duplicate/templated reuse.
        dup = {r.review_id: 1.0 for r in reviews}
        tpl = {r.review_id: 1.0 for r in reviews}
        residual = round(coord["r0"] - round(coordinated_text_reuse_weight() * 1.0, 3), 3)
        assert residual > 0.0
        wb = compute_review_weights(
            reviews, duplicate_probability=dup, templated_probability=tpl
        )
        wf = compute_review_weights(
            reviews,
            duplicate_probability=dup,
            templated_probability=tpl,
            coordinated_probability=coord,
        )
        assert wb[0] - wf[0] == pytest.approx(
            CONFIG.weight.penalty_coordinated * residual, abs=2e-4
        )


class TestCoordinatedProbabilities:
    def test_no_evidence_zero(self) -> None:
        reviews = [_review(f"r{i}", "p1", date(2026, 3, i + 1)) for i in range(5)]
        probs = coordinated_review_probabilities(reviews)
        assert all(probs[r.review_id] == 0.0 for r in reviews)

    def test_engineered_review_probability_high(self) -> None:
        # Identical templated text on a burst day with a dominant rating shift
        # -> very high coordinated probability.
        day = date(2026, 9, 2)
        text = "Всё было идеально, обслуживание на высоте, рекомендую всем!"
        reviews = [
            _review(f"r{i}", "p1", day, rating=5, text=text) for i in range(12)
        ]
        anomaly = RatingAnomalyEvent(
            place_id="p1", date=day,
            baseline_dist={r: 0.2 for r in range(1, 6)},
            event_dist={5: 0.9, 4: 0.1},
            jsd=0.4, dominant_shift="5-stars", score=100.0,
        )
        dup_group = DuplicateGroup(
            group_id=1, review_ids=[f"r{i}" for i in range(12)],
            exact_count=12, fuzzy_count=0, near_count=0, semantic_count=0,
            avg_similarity=1.0, signals=[], counter_signals=[],
        )
        tpl = TemplatedTextScorer().score(reviews)
        probs = coordinated_review_probabilities(
            reviews,
            burst_events=[_burst(day)],
            rating_anomalies=[anomaly],
            dup_groups=[dup_group],
            templated_results=tpl,
            clusters=[_cluster([f"r{i}" for i in range(12)])],
        )
        assert all(probs[r.review_id] > 0.8 for r in reviews)

    def test_organic_review_during_burst_stays_bounded(self) -> None:
        # A genuine-specific organic review published on a busy day must not be
        # labelled as coordinated == 1.0; it is bounded well below engineered.
        day = date(2026, 9, 2)
        text = "Всё было идеально, обслуживание на высоте, рекомендую всем!"
        engineered = [
            _review(f"e{i}", "p1", day, rating=5, text=text) for i in range(12)
        ]
        organic = _review("organic", "p1", day, rating=5,
                          text="Стейк рибай за 1400 с кровью, официант дал термос с водой")
        reviews = engineered + [organic]
        anomaly = RatingAnomalyEvent(
            place_id="p1", date=day,
            baseline_dist={r: 0.2 for r in range(1, 6)},
            event_dist={5: 0.9, 4: 0.1},
            jsd=0.4, dominant_shift="5-stars", score=100.0,
        )
        dup_group = DuplicateGroup(
            group_id=1, review_ids=[f"e{i}" for i in range(12)],
            exact_count=12, fuzzy_count=0, near_count=0, semantic_count=0,
            avg_similarity=1.0, signals=[], counter_signals=[],
        )
        tpl = TemplatedTextScorer().score(reviews)
        probs = coordinated_review_probabilities(
            reviews,
            burst_events=[_burst(day)],
            rating_anomalies=[anomaly],
            dup_groups=[dup_group],
            templated_results=tpl,
            clusters=[_cluster([f"e{i}" for i in range(12)])],
        )
        engineered_max = max(probs[f"e{i}"] for i in range(12))
        assert probs["organic"] < engineered_max
        assert probs["organic"] < 0.8
        assert engineered_max > 0.8

    def test_probability_grows_with_evidence(self) -> None:
        day = date(2026, 9, 2)
        text = "Всё было идеально, обслуживание на высоте, рекомендую всем!"
        reviews = [_review(f"r{i}", "p1", day, rating=5, text=text) for i in range(8)]
        tpl = TemplatedTextScorer().score(reviews)
        p_burst_only = coordinated_review_probabilities(
            reviews, burst_events=[_burst(day)], templated_results=tpl
        )
        anomaly = RatingAnomalyEvent(
            place_id="p1", date=day,
            baseline_dist={r: 0.2 for r in range(1, 6)},
            event_dist={5: 0.9, 4: 0.1},
            jsd=0.4, dominant_shift="5-stars", score=100.0,
        )
        p_burst_and_rating = coordinated_review_probabilities(
            reviews, burst_events=[_burst(day)],
            rating_anomalies=[anomaly], templated_results=tpl,
        )
        assert all(
            p_burst_and_rating[r.review_id] >= p_burst_only[r.review_id]
            for r in reviews
        )


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
