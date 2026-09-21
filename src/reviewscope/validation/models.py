"""Human label models for ReviewScope real-data validation (Phase 15).

This package is the *measurement* machinery for Phase 15. It must never be used
to tune production detectors, thresholds or weights; it only carries human
ground truth and folds it against the outputs the production pipeline already
produces.

These models are deliberately separate from the production models
(``reviewscope.models.review``, ``reviewscope.models.scores``): a human label
is ground truth recorded by an annotator, and must never be confused with a
``NormalizedReview`` or a ``ScoreResult`` prediction.

CSV schema is owned here. Column names in :data:`LABEL_COLUMNS` and
:data:`EVENT_LABEL_COLUMNS` are the on-disk contract shared by the template
generator, the labeling UI and the validation runner.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ANNOTATION_SCHEMA_VERSION = "1.0"

LABEL_COLUMNS = [
    "review_id",
    "templated_label",
    "template_group_id",
    "specificity_label",
    "duplicate_group_id",
    "duplicate_label",
    "reviewer_notes",
    "annotation_schema_version",
    "annotator_id",
    "labeled_at",
    "sample_type",
    "sampling_stratum",
]

EVENT_LABEL_COLUMNS = [
    "place_id",
    "event_id",
    "start_date",
    "end_date",
    "polarity",
    "coordinated_label",
    "notes",
    "annotation_schema_version",
    "annotator_id",
    "labeled_at",
]

#: Sampling strata understood by the validation choreography.
SAMPLING_STRATA = ("random", "high", "medium", "low", "duplicate")


class TemplatedLabel(StrEnum):
    """Per-review judgment about template-family / synthetic-like text."""

    ORGANIC = "organic"
    TEMPLATED = "templated"
    UNCERTAIN = "uncertain"


class SpecificityLabel(StrEnum):
    """Per-review informativeness judgment."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNCERTAIN = "uncertain"


class DuplicateLabel(StrEnum):
    """Per-review duplicate judgment (paired with ``duplicate_group_id``)."""

    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    UNCERTAIN = "uncertain"


class SampleType(StrEnum):
    """Which sampling regime a review belongs to.

    * ``evaluation`` — deterministic simple random sample, used for headline
      (representative) metrics.
    * ``challenge`` — score/duplicate-enriched sample, used ONLY for
      diagnostic / error analysis and never for headline population metrics.

    See ``docs/REAL_DATA_VALIDATION.md`` for the isolation rules.
    """

    EVALUATION = "evaluation"
    CHALLENGE = "challenge"


class Polarity(StrEnum):
    """Human judgment about an event's rating polarity (future validation)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNCERTAIN = "uncertain"


class CoordinatedLabel(StrEnum):
    """Human judgment about whether an event looks coordinated."""

    ORGANIC = "organic"
    SUSPICIOUS = "suspicious"
    UNCERTAIN = "uncertain"


class ReviewHumanLabel(BaseModel):
    """A single human label record for one review.

    Fields are intentionally permissive (``extras`` ignored, values optional):
    an annotator may leave fields blank and may mark ``uncertain`` rather than
    being forced into a guess. Cross-field sanity is reported as *warnings* by
    the loader, not hard errors, mirroring the tolerant import contract.
    """

    model_config = ConfigDict(extra="ignore")

    review_id: str
    templated_label: TemplatedLabel | None = None
    template_group_id: str | None = None
    specificity_label: SpecificityLabel | None = None
    duplicate_group_id: str | None = None
    duplicate_label: DuplicateLabel | None = None
    reviewer_notes: str | None = None

    annotation_schema_version: str = ANNOTATION_SCHEMA_VERSION
    annotator_id: str | None = None
    labeled_at: str | None = None
    sample_type: SampleType | None = None
    sampling_stratum: str | None = None

    def has_templated_verdict(self) -> bool:
        return self.templated_label in (TemplatedLabel.ORGANIC, TemplatedLabel.TEMPLATED)

    def has_specificity_verdict(self) -> bool:
        return self.specificity_label in (
            SpecificityLabel.LOW,
            SpecificityLabel.MEDIUM,
            SpecificityLabel.HIGH,
        )

    def has_duplicate_verdict(self) -> bool:
        return self.duplicate_label in (DuplicateLabel.UNIQUE, DuplicateLabel.DUPLICATE)


class ReviewLabelSet(BaseModel):
    """An ordered collection of :class:`ReviewHumanLabel` records."""

    model_config = ConfigDict(extra="ignore")

    labels: list[ReviewHumanLabel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def by_id(self) -> dict[str, ReviewHumanLabel]:
        return {label.review_id: label for label in self.labels}

    def ids(self) -> list[str]:
        return [label.review_id for label in self.labels]


class EventLabel(BaseModel):
    """A human label about a *group/event* (future coordinated-activity
    validation).

    These records are authored by humans and are **never inferred** by
    ReviewScope. They are stored and structurally validated so a future phase
    can compare coordinated-activity detection against real events.
    """

    model_config = ConfigDict(extra="ignore")

    place_id: str
    event_id: str
    start_date: str | None = None
    end_date: str | None = None
    polarity: Polarity | None = None
    coordinated_label: CoordinatedLabel | None = None
    notes: str | None = None

    annotation_schema_version: str = ANNOTATION_SCHEMA_VERSION
    annotator_id: str | None = None
    labeled_at: str | None = None


class EventLabelSet(BaseModel):
    """An ordered collection of :class:`EventLabel` records."""

    model_config = ConfigDict(extra="ignore")

    events: list[EventLabel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
