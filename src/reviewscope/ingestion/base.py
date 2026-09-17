"""Abstract data source adapter (SPEC.md §4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from reviewscope.ingestion.normalize import ValidationReport
from reviewscope.models.review import NormalizedReview


@dataclass
class LoadResult:
    """Outcome of loading a data source.

    ``reviews`` holds every successfully parsed review; ``report`` carries the
    validation statistics required by SPEC.md §6 (Imported / Valid / Skipped /
    Warnings).
    """

    reviews: list[NormalizedReview] = field(default_factory=list)
    report: ValidationReport = field(default_factory=ValidationReport)


class DataSource(ABC):
    """Interface every ReviewScope data source must implement.

    Subclasses must produce :class:`LoadResult` with normalized reviews and a
    filled validation report. Analysis code depends only on this interface.
    """

    label: str = "unknown"

    @abstractmethod
    def load(self, path: str | Path, **kwargs: object) -> LoadResult:
        """Load and normalize reviews from ``path``.

        Implementations must never raise on malformed individual rows: bad
        rows are counted as skipped and surfaced through the report.
        """
        raise NotImplementedError
