"""Analysis modules for ReviewScope.

Each module operates on normalized data and returns ``ScoreResult`` objects
carrying scores, confidence levels, signals and counter-signals
(SPEC.md §36). Modules do not depend on the data source adapter layer.
"""

from reviewscope.analysis.bursts import BurstDetector, BurstEvent, daily_counts, events_frame
from reviewscope.analysis.keywords import (
    emerging_keywords,
    extract_keywords,
    keywords_by_sentiment,
)
from reviewscope.analysis.rating_anomalies import (
    RatingAnomalyDetector,
    RatingAnomalyEvent,
    rating_anomaly_frame,
)
from reviewscope.analysis.reviewer import (
    ReviewerMetrics,
    category_experience_score,
    compute_reviewer_metrics,
    local_familiarity_score,
    reviewer_relevance_score,
    reviewers_frame,
)
from reviewscope.analysis.scoring import (
    compute_review_weights,
    coordinated_activity_score,
    weighted_rating,
)
from reviewscope.analysis.specificity import specificity_score
from reviewscope.analysis.templated import TemplatedTextScorer, templated_frame
from reviewscope.analysis.topics import TopicCluster, TopicClusterer, clusters_frame

__all__ = [
    "BurstDetector",
    "BurstEvent",
    "RatingAnomalyDetector",
    "RatingAnomalyEvent",
    "ReviewerMetrics",
    "TemplatedTextScorer",
    "TopicCluster",
    "TopicClusterer",
    "category_experience_score",
    "clusters_frame",
    "compute_review_weights",
    "compute_reviewer_metrics",
    "coordinated_activity_score",
    "daily_counts",
    "emerging_keywords",
    "events_frame",
    "extract_keywords",
    "keywords_by_sentiment",
    "local_familiarity_score",
    "rating_anomaly_frame",
    "reviewer_relevance_score",
    "reviewers_frame",
    "specificity_score",
    "templated_frame",
    "weighted_rating",
]
