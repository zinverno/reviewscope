"""Version 1 wire models. Detector membership and evidence retain their core meaning."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from reviewscope.analysis.engine import AnalyzedPlace
from reviewscope.models.scores import ConfidenceLevel


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    api_version: Literal["v1"] = "v1"
    dataset_available: bool


class Error(BaseModel):
    error: str


class Place(BaseModel):
    place_id: str
    name: str | None
    category: str | None
    review_count: int
    coordinates: tuple[FiniteFloat, FiniteFloat] | None


class Review(BaseModel):
    review_id: str
    text: str | None
    rating: int | None
    published_at: datetime | None


class ReviewPage(BaseModel):
    place_id: str
    total: int
    limit: int
    offset: int
    reviews: list[Review]


class Evidence(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    signals: list[str]
    counter_signals: list[str]


class Score(Evidence):
    name: str
    value: FiniteFloat | None
    unit: str
    confidence: ConfidenceLevel = Field(description="Legacy heuristic evidence band (not a calibrated probability).")
    details: dict[str, FiniteFloat]


class Burst(Evidence):
    date: date
    expected: FiniteFloat
    observed: int
    severity: ConfidenceLevel


class RatingAnomaly(Evidence):
    date: date
    baseline_dist: dict[int, FiniteFloat]
    event_dist: dict[int, FiniteFloat]
    severity: ConfidenceLevel
    jsd: FiniteFloat


class Family(Evidence):
    group_id: int
    review_ids: list[str]
    edges: list[tuple[str, str, Literal["exact", "near", "semantic", "fuzzy"], FiniteFloat]]


class Topic(Evidence):
    cluster_id: int | Literal["all"]
    review_ids: list[str]
    representative_phrases: list[str]
    representative_reviews: list[tuple[str, FiniteFloat]]


class Analysis(BaseModel):
    place_id: str
    review_count: int
    rated_review_count: int
    raw_rating: FiniteFloat | None
    calculated_weighted_rating: Score | None
    rating_notice: str = "ReviewScope model estimate. The platform's official rating may differ."
    coordinated_activity: Score | None
    burst_events: list[Burst]
    rating_anomalies: list[RatingAnomaly]
    duplicate_groups: list[Family]
    topics: list[Topic]

    @classmethod
    def from_core(cls, result: AnalyzedPlace) -> "Analysis":
        rated = sum(r.rating is not None for r in result.reviews)
        weighted = result.weighted_rating_result
        rating_score = None
        if weighted is not None:
            evidence = weighted.model_dump(exclude={"value", "unit"})
            rating_score = Score(
                **evidence, value=result.weighted_rating_value if rated else None, unit="stars"
            )
        return cls(
            place_id=result.place_id,
            review_count=result.review_count,
            rated_review_count=rated,
            raw_rating=result.raw_rating if rated else None,
            calculated_weighted_rating=rating_score,
            coordinated_activity=result.coordinated,
            burst_events=result.burst_events,
            rating_anomalies=result.rating_anomalies,
            duplicate_groups=result.duplicate_groups,
            topics=result.clusters,
        )
