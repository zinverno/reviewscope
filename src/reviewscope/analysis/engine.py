"""Per-place analysis engine tying all detection phases together.

The engine is the integration seam between the analysis modules and the UI:
given one place id it returns a self-contained :class:`AnalyzedPlace` carrying
every signal a review page needs, including per-review weights and the raw vs.
weighted rating (§14, §16, §18–§23).

Design notes:

* reviewer history is loaded once per store and shared across place analyses
  (category experience / reviewer relevance / local familiarity need the full
  reviewer history, not just the current place);
* embeddings come from the persisted cache — repeated runs don't re-encode;
* deterministic: no randomness beyond the model's own seeding, which is
  fixed at the pipeline boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from reviewscope.analysis.bursts import BurstDetector, BurstEvent
from reviewscope.analysis.duplicates import DuplicateDetector, DuplicateGroup
from reviewscope.analysis.rating_anomalies import (
    RatingAnomalyDetector,
    RatingAnomalyEvent,
)
from reviewscope.analysis.reviewer import (
    category_experience_score,
    compute_reviewer_metrics,
    reviewer_relevance_score,
)
from reviewscope.analysis.scoring import (
    compute_review_weights,
    coordinated_activity_score,
    coordinated_review_probabilities,
    weighted_rating,
)
from reviewscope.analysis.specificity import specificity_score
from reviewscope.analysis.templated import TemplatedTextScorer
from reviewscope.analysis.topics import TopicCluster, TopicClusterer
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ScoreResult
from reviewscope.storage.duckdb_store import DuckDBStore

PlaceId = str


@dataclass
class AnalyzedPlace:
    """Everything computed for one place (per-review aligned by index)."""

    place_id: PlaceId
    reviews: list[NormalizedReview]
    burst_events: list[BurstEvent] = field(default_factory=list)
    rating_anomalies: list[RatingAnomalyEvent] = field(default_factory=list)
    clusters: list[TopicCluster] = field(default_factory=list)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    templated_scores: list[ScoreResult] = field(default_factory=list)
    review_weights: list[float] = field(default_factory=list)
    coordinated: ScoreResult | None = None
    raw_rating: float = 0.0
    weighted_rating_value: float = 0.0
    weighted_rating_result: ScoreResult | None = None
    keywords: list[tuple[str, float]] = field(default_factory=list)
    emerging: list[tuple[str, float]] = field(default_factory=list)
    positive_keywords: list[tuple[str, float]] = field(default_factory=list)
    negative_keywords: list[tuple[str, float]] = field(default_factory=list)
    reviewer_overview: object | None = None

    @property
    def review_count(self) -> int:
        return len(self.reviews)


class AnalysisEngine:
    """Runs the full analysis pipeline for a place within one store."""

    def __init__(
        self,
        store: DuckDBStore,
        embedding_cache: EmbeddingCache | None = None,
        use_embeddings: bool = True,
    ) -> None:
        self._store = store
        self._embedding_cache = embedding_cache or EmbeddingCache(store)
        self._use_embeddings = use_embeddings
        self._all_reviews: list[NormalizedReview] | None = None
        self._all_embeddings: np.ndarray | None = None

    # -- dataset-level caching ------------------------------------------------

    def _all(self) -> tuple[list[NormalizedReview], np.ndarray | None]:
        if self._all_reviews is None:
            self._all_reviews = self._store.fetch_reviews()
        if (
            self._all_embeddings is None
            and self._use_embeddings
            and self._all_reviews
        ):
            self._all_embeddings = self._embedding_cache.embed_reviews(self._all_reviews)
        return self._all_reviews, self._all_embeddings

    def _place_embeddings(self, reviews: list[NormalizedReview]) -> np.ndarray | None:
        """Subset the full-embedding matrix to the reviews of this place."""
        if not self._use_embeddings or not reviews:
            return None
        all_reviews, all_emb = self._all()
        if all_emb is None:
            return None
        index = {r.review_id: i for i, r in enumerate(all_reviews)}
        if not all(rid in index for rid in (r.review_id for r in reviews)):
            return None
        idx = [index[r.review_id] for r in reviews]
        return all_emb[np.array(idx)]

    def analyze(self, place_id: PlaceId) -> AnalyzedPlace:
        reviews = self._store.fetch_reviews(place_id=place_id)
        embeddings = self._place_embeddings(reviews)

        # --- analysis phases -------------------------------------------------
        bursts = BurstDetector().detect(reviews)
        anomalies = RatingAnomalyDetector().detect(reviews, bursts)
        clusters = TopicClusterer().cluster(reviews, embeddings)
        dup_groups = DuplicateDetector().detect(reviews, embeddings=embeddings)
        templated = TemplatedTextScorer().score(reviews, embeddings)

        # --- coordinated activity -------------------------------------------
        coordinated = coordinated_activity_score(
            reviews,
            burst_events=bursts,
            rating_anomalies=anomalies,
            clusters=clusters,
            dup_groups=dup_groups,
            templated_results=templated,
        )

        # --- per-review weights ---------------------------------------------
        specs = {r.review_id: specificity_score(r.text_or_empty()).value for r in reviews}
        cat_map: dict[str, float] = {}
        rel_map: dict[str, float] = {}
        history_by_reviewer = self._history_by_reviewer()
        for r in reviews:
            history = history_by_reviewer.get(r.reviewer_id, [])
            cat = category_experience_score(history, r.place_category)
            rel = reviewer_relevance_score(
                history, r.place_category, r.city, r.region
            )
            cat_map[r.review_id] = cat.value
            rel_map[r.review_id] = rel.value

        # Graded probabilities for the §22 penalties (audit §22): duplicate
        # coverage and templated strength are probabilities, and coordinated
        # participation is a graded 0..1 signal, not a binary flag.
        dup_prob: dict[str, float] = {}
        for g in dup_groups:
            if len(g.review_ids) >= 3:
                avg_sim = min(1.0, max(getattr(g, "mean_similarity", getattr(g, "avg_similarity", 0.0)) or 0.0, 0.5))
                prob = round(min(1.0, len(g.review_ids) / 5.0) * avg_sim, 3)
                for rid in g.review_ids:
                    dup_prob[rid] = max(dup_prob.get(rid, 0.0), prob)
        tpl_prob = {
            reviews[i].review_id: round(min(1.0, res.value / 100.0), 3)
            for i, res in enumerate(templated)
        }
        coord_prob = coordinated_review_probabilities(
            reviews,
            burst_events=bursts,
            rating_anomalies=anomalies,
            dup_groups=dup_groups,
            templated_results=templated,
            clusters=clusters,
        )

        weights = compute_review_weights(
            reviews,
            specificity_map=specs,
            category_experience_map=cat_map,
            reviewer_relevance_map=rel_map,
            duplicate_probability=dup_prob,
            templated_probability=tpl_prob,
            coordinated_probability=coord_prob,
        )
        raw, weighted, w_result = weighted_rating(reviews, weights)

        # --- keywords --------------------------------------------------------
        from reviewscope.analysis.keywords import (
            emerging_keywords,
            extract_keywords,
            keywords_by_sentiment,
        )

        kws = extract_keywords(reviews, top_n=15)
        sent = keywords_by_sentiment(reviews, top_n=10)
        pos = sent["positive"]
        neg = sent["negative"]
        emerge = emerging_keywords(reviews, reference_days=90, recent_days=30)

        # --- reviewer overview map ------------------------------------------
        overview = self._reviewer_overview(reviews)

        return AnalyzedPlace(
            place_id=place_id,
            reviews=reviews,
            burst_events=bursts,
            rating_anomalies=anomalies,
            clusters=clusters,
            duplicate_groups=dup_groups,
            templated_scores=templated,
            review_weights=weights,
            coordinated=coordinated,
            raw_rating=raw,
            weighted_rating_value=weighted,
            weighted_rating_result=w_result,
            keywords=kws,
            emerging=emerge,
            positive_keywords=pos,
            negative_keywords=neg,
            reviewer_overview=overview,
        )

    def _history_by_reviewer(self) -> dict[str, list[NormalizedReview]]:
        all_reviews, _ = self._all()
        history: dict[str, list[NormalizedReview]] = {}
        for r in all_reviews:
            history.setdefault(r.reviewer_id, []).append(r)
        return history

    def _reviewer_overview(self, reviews: list[NormalizedReview]) -> object:
        """Return a frame of reviewer metrics for the reviewers of this place."""
        reviewer_ids = {r.reviewer_id for r in reviews}
        all_reviews, _ = self._all()
        selected = [r for r in all_reviews if r.reviewer_id in reviewer_ids]
        metrics = compute_reviewer_metrics(selected)
        return metrics
