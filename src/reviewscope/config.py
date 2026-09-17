"""Centralized configuration for all ReviewScope thresholds and weights.

SPEC.md §35 requires that scoring thresholds and weights are not scattered
magic numbers. Every tunable value lives here with its documentation.
A single immutable :class:`ReviewScopeConfig` exposes named groups so that
formulas in the analysis modules are readable and explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for the embedding provider.

    ``model_name`` is the default multilingual model (SPEC.md §3) which is
    suitable for Russian and English text. The provider layer (see
    ``reviewscope/embeddings/base.py``) is swappable, so changing this model
    does not require touching analysis code.
    """

    model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    batch_size: int = 64
    max_length: int = 512
    dim: int = 384


# ---------------------------------------------------------------------------
# Duplicate detection (SPEC.md §11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateConfig:
    """Thresholds for the four duplicate detection levels.

    ``fuzzy_threshold`` is the minimum RapidFuzz (default "score_cutoff") ratio
    between two normalized texts to be considered a fuzzy duplicate.

    ``near_char_ngram``/``near_tfidf_threshold`` control the character n-gram
    TF-IDF similarity used for near-duplicates.

    ``semantic_threshold`` is the embedding cosine similarity above which two
    reviews are considered semantic variants.

    ``minhash_num_perm``/``minhash_bands``/``minhash_rows`` control the
    datasketch MinHash LSH banded signature used for candidate generation
    (SPEC.md §33: avoid O(N^2) on large datasets).
    """

    exact_normalize: bool = True
    #: RapidFuzz ratio above which a pair is classified as a *near duplicate*.
    fuzzy_threshold: float = 0.90
    #: RapidFuzz ratio below ``fuzzy_threshold`` but above this still joins a group.
    fuzzy_group_threshold: float = 0.85
    near_char_ngram: int = 4
    near_tfidf_threshold: float = 0.70
    semantic_threshold: float = 0.88
    minhash_num_perm: int = 256
    #: Minimum Jaccard similarity for the MinHash LSH banding stage.
    minhash_threshold: float = 0.70
    #: Below this review count we allow O(N^2) brute-force as the default path
    #: (SPEC.md §33 fallback). Above it, candidate generation is mandatory.
    brute_force_max_reviews: int = 2000
    candidate_max_topk: int = 50


# ---------------------------------------------------------------------------
# Burst detection (SPEC.md §12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BurstConfig:
    """Robust temporal burst detection parameters.

    SPEC.md §12 forbids plain standard deviation as the sole method, so the
    baseline is a rolling median with MAD, and the score uses the modified
    z-score: ``0.6745 * (x - median) / MAD``. Days whose modified z-score
    exceeds ``z_score_threshold`` are flagged.
    """

    bin_frequency: str = "D"
    baseline_window: int = 30
    z_score_threshold: float = 3.5
    min_reviews_for_event: int = 3
    #: Minimum multiples above the rolling baseline -> HIGH severity.
    high_multiplier: float = 5.0
    #: Minimum multiples above the rolling baseline -> MEDIUM severity.
    medium_multiplier: float = 2.5


# ---------------------------------------------------------------------------
# Rating anomaly (SPEC.md §13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatingAnomalyConfig:
    """Rating-distribution anomaly configuration.

    The divergence between the baseline distribution and an event window is
    measured with Jensen-Shannon divergence, which is symmetric, bounded in
    [0, 1] (natural log) and explainable.
    """

    use_jsd: bool = True
    event_window_days: int = 3
    #: Trailing days used to build the historical rating-distribution baseline.
    baseline_window: int = 30
    jsd_threshold: float = 0.05
    min_reviews_in_window: int = 5
    #: JSD above this with a dominant single rating -> HIGH.
    dominant_share_threshold: float = 0.7


# ---------------------------------------------------------------------------
# Topic clustering (SPEC.md §10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicConfig:
    """Semantic topic clustering configuration.

    Clustering uses embeddings + HDBSCAN. If clustering quality on the demo
    dataset is poor in the original dimensionality we apply deterministic PCA
    down to ``pca_components`` dimensions before HDBSCAN (documented in
    PROJECT_STATE.md). ``min_cluster_size`` and ``min_samples`` are HDBSCAN's
    native parameters.
    """

    min_cluster_size: int = 5
    min_samples: int = 3
    pca_components: int = 32
    max_clusters: int = 25
    #: Cosine similarity used to compute per-cluster internal similarity.
    similarity_metric: str = "cosine"
    #: Keep only clusters whose members show at least this mean centroid cosine
    #: similarity; below that the cluster is labelled "unstructured".
    min_mean_similarity: float = 0.30
    #: Number of representative reviews shown per cluster.
    representative_reviews: int = 5


# ---------------------------------------------------------------------------
# Specificity (SPEC.md §17)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecificityConfig:
    """Heuristic specificity scoring components (SPEC.md §17).

    The final score is the sum of positive signal points and negative generic
    language penalties, clipped to [0, 100].
    """

    signal_points: dict[str, float] = field(
        default_factory=lambda: {
            "numbers": 12.0,
            "concrete_entities": 10.0,
            "noun_details": 8.0,
            "narrative_words": 6.0,
            "length_bonus": 8.0,
            "menu_product": 10.0,
            "wait_time": 10.0,
        }
    )
    generic_penalty: float = 18.0
    max_missing_penalty: float = 15.0
    #: Weight applied to score when the review is unusually short (< 15 tokens).
    short_text_factor: float = 0.55


# ---------------------------------------------------------------------------
# Templated / synthetic-like text (SPEC.md §16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplatedConfig:
    """Signals for the templated / synthetic-like score (SPEC.md §16).

    Weights sum to 1.0. The score is a weighted sum of individual signal
    components (each 0..1), scaled to 0..100.
    """

    semantic_group_weight: float = 0.25
    phrase_reuse_weight: float = 0.20
    structure_weight: float = 0.15
    low_specificity_weight: float = 0.15
    vocabulary_diversity_weight: float = 0.10
    stylistic_uniformity_weight: float = 0.10
    temporal_clustering_weight: float = 0.05
    #: Sentence-pattern repetition triggered above this cosine ceiling of
    #: repeated bigram frequency in a review.
    repeated_pattern_threshold: float = 0.30
    #: Reviews whose phrasing matches > `high_match_count` peers are treated
    #: as highly templated for the phrase-reuse signal.
    high_match_count: int = 5


# ---------------------------------------------------------------------------
# Reviewer metrics thresholds (SPEC.md §18–§20)
# ---------------------------------------------------------------------------

LOCAL_HISTORY_MIN_REVIEWS: int = 3
"""Minimum reviews before local familiarity exceeds zero."""


# ---------------------------------------------------------------------------
# Category experience (SPEC.md §19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryExperienceConfig:
    """Category experience score (SPEC.md §19).

    Uses log-saturation: the number of reviews is transformed with
    ``log1p`` and rescaled, so 1000 reviews do not dominate 10 reviews.
    """

    log_scale: float = 3.0
    category_share_weight: float = 0.4
    count_weight: float = 0.35
    active_period_weight: float = 0.15
    diversity_weight: float = 0.1
    log_base: float = 10.0


# ---------------------------------------------------------------------------
# Local familiarity (SPEC.md §20)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalFamiliarityConfig:
    """Local familiarity score (SPEC.md §20)."""

    log_scale: float = 2.5
    place_diversity_weight: float = 0.4
    activity_weight: float = 0.35
    duration_weight: float = 0.25


# ---------------------------------------------------------------------------
# Reviewer relevance (SPEC.md §21)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerRelevanceConfig:
    """Configurable weights for the reviewer relevance score (SPEC.md §21)."""

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "category_experience": 0.30,
            "specificity_history": 0.25,
            "review_consistency": 0.15,
            "local_context": 0.15,
            "history_depth": 0.15,
        }
    )


# ---------------------------------------------------------------------------
# Review weight (SPEC.md §22)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeightConfig:
    """Review weight (SPEC.md §22).

    weight = specificity      * w_spec
           + category_experience * w_cat
           + reviewer_relevance * w_rev
           + recency_factor   * w_recency
           - duplicate_probability   * p_dup
           - templated_probability   * p_tpl
           - coordinated_probability * p_cas
    clipped to [weight_min, weight_max] and normalized (min >= 0.25, max <= 2.0).
    """

    weight_specificity: float = 0.35
    weight_category_experience: float = 0.20
    weight_reviewer_relevance: float = 0.25
    weight_recency: float = 0.20
    penalty_duplicate: float = 0.30
    penalty_templated: float = 0.25
    penalty_coordinated: float = 0.20
    weight_min: float = 0.25
    weight_max: float = 2.0
    #: A review older than recency_half_life_days gets a recency factor of 0.5.
    recency_half_life_days: int = 365


# ---------------------------------------------------------------------------
# Coordinated activity score (SPEC.md §14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatedConfig:
    """Component weights for the coordinated activity score (SPEC.md §14).

    Weights sum to 1.0. Each component is computed in 0..1 space, then scaled
    to 0..100. Confidence levels follow SPEC.md (§14): HIGH above
    ``high_threshold``, MEDIUM above ``medium_threshold``, else LOW.
    """

    # Calibrated on the demo corpus so that burst-driven places (volume,
    # rating, temporal signals) separate clearly from organic-only places.
    components: dict[str, float] = field(
        default_factory=lambda: {
            "volume_anomaly": 0.30,
            "rating_anomaly": 0.25,
            "temporal_density": 0.15,
            "semantic_similarity": 0.08,
            "duplicate_density": 0.10,
            "template_similarity": 0.07,
            "reviewer_overlap": 0.05,
        }
    )
    high_threshold: float = 65.0
    medium_threshold: float = 35.0
    #: Documents counter-signals only if the number of counter-signal reviews
    #: ("specific/details" and "geographically diverse") is at least this big.
    counter_signal_min_reviews: int = 3


# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewScopeConfig:
    """Complete ReviewScope configuration snapshot.

    Single entry point for default settings. Values documented per group.
    """

    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    duplicate: DuplicateConfig = field(default_factory=DuplicateConfig)
    burst: BurstConfig = field(default_factory=BurstConfig)
    rating_anomaly: RatingAnomalyConfig = field(default_factory=RatingAnomalyConfig)
    topic: TopicConfig = field(default_factory=TopicConfig)
    specificity: SpecificityConfig = field(default_factory=SpecificityConfig)
    templated: TemplatedConfig = field(default_factory=TemplatedConfig)
    category_experience: CategoryExperienceConfig = field(
        default_factory=CategoryExperienceConfig
    )
    local_familiarity: LocalFamiliarityConfig = field(default_factory=LocalFamiliarityConfig)
    reviewer_relevance: ReviewerRelevanceConfig = field(
        default_factory=ReviewerRelevanceConfig
    )
    weight: WeightConfig = field(default_factory=WeightConfig)
    coordinated: CoordinatedConfig = field(default_factory=CoordinatedConfig)


CONFIG: ReviewScopeConfig = ReviewScopeConfig()
"""Default module-level configuration. Modules should read from this instance
so that tests can override with a patched snapshot if needed."""


# Backwards-compatible named constants required by SPEC.md §35 examples so that
# formulas read naturally (e.g. ``config.CATEGORY_EXPERIENCE_WEIGHT``).
CATEGORY_EXPERIENCE_WEIGHT: float = CONFIG.weight.weight_category_experience
SPECIFICITY_WEIGHT: float = CONFIG.weight.weight_specificity
DUPLICATE_PENALTY: float = CONFIG.weight.penalty_duplicate
TEMPLATE_PENALTY: float = CONFIG.weight.penalty_templated
RECENCY_WEIGHT: float = CONFIG.weight.weight_recency
BURST_THRESHOLD: float = CONFIG.burst.z_score_threshold
SEMANTIC_SIMILARITY_THRESHOLD: float = CONFIG.duplicate.semantic_threshold
