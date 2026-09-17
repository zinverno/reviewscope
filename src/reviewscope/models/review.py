"""Core domain models (SPEC.md §5).

``NormalizedReview`` is the canonical normalized review produced by every
``DataSource`` adapter. All fields except the key identifiers are nullable by
design, matching the tolerant import contract in SPEC.md §6.

``ReviewerProfile`` groups a reviewer's review history; aggregated signals are
computed from it by the reviewer analysis module (SPEC.md §18).
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReviewId = Annotated[str, Field(min_length=1)]
PlaceId = Annotated[str, Field(min_length=1)]
ReviewerId = Annotated[str, Field(min_length=1)]


class NormalizedReview(BaseModel):
    """A single normalized review.

    Key identifiers (``review_id``, ``place_id``, ``reviewer_id``) are always
    required; all descriptive fields are optional so that an importer can
    deliver partial records that the rest of the pipeline handles gracefully.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    review_id: ReviewId
    place_id: PlaceId
    place_name: str | None = None
    place_category: str | None = None

    reviewer_id: ReviewerId
    reviewer_name: str | None = None

    rating: int | None = Field(default=None, ge=1, le=5)
    text: str | None = None
    published_at: str | None = None

    city: str | None = None
    region: str | None = None
    country: str | None = None

    latitude: float | None = None
    longitude: float | None = None

    source: str | None = None
    source_url: str | None = None

    @field_validator("rating")
    @classmethod
    def _validate_rating(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1 or v > 5:
            raise ValueError(f"rating must be in 1..5, got {v}")
        return v

    def text_or_empty(self) -> str:
        """Return the review text or an empty string (never ``None``)."""
        return self.text or ""

    def fingerprint(self) -> str:
        """Stable content hash, used by the embedding cache.

        The hash covers the normalized text and model-independent metadata so
        that unchanged texts are never re-embedded (SPEC.md §32).
        """
        normalized = self.text_or_empty().strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def short_id(self) -> str:
        """A compact, stable identity for display purposes."""
        return hashlib.sha1(self.review_id.encode("utf-8")).hexdigest()[:8]


class ReviewerProfile(BaseModel):
    """A reviewer and their (possibly partial) review history.

    This is the structure referenced in SPEC.md §5. Aggregated features are
    *computed* by ``reviewscope/analysis/reviewer.py``; this model only carries
    the raw history.
    """

    model_config = ConfigDict(extra="ignore")

    reviewer_id: ReviewerId
    reviews: list[NormalizedReview] = Field(default_factory=list)

    def add(self, review: NormalizedReview) -> Self:
        """Append a review to the profile history."""
        self.reviews.append(review)
        return self
