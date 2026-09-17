"""DataSource adapters for ReviewScope (SPEC.md §4).

The ingestion layer decouples data acquisition from analytics: adapters
produce normalized :class:`~reviewscope.models.review.NormalizedReview`
objects; the analysis engine never touches the source format. In this MVP the
:class:`CSVAdapter` and :class:`JSONAdapter` are real; future adapters
(Yandex, Google, 2GIS) implement the same base interface.
"""

from reviewscope.ingestion.base import DataSource, LoadResult
from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.ingestion.json_adapter import JSONAdapter
from reviewscope.ingestion.normalize import (
    ValidationReport,
    map_row,
    normalize_review,
    parse_date,
    parse_float,
    parse_int,
)

__all__ = [
    "CSVAdapter",
    "DataSource",
    "JSONAdapter",
    "LoadResult",
    "ValidationReport",
    "map_row",
    "normalize_review",
    "parse_date",
    "parse_float",
    "parse_int",
]
