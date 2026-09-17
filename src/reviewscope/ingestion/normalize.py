"""Tolerant field normalization and validation reporting (SPEC.md §6).

Every raw row drawn from a CSV/JSON adapter is run through
:func:`normalize_review`, which:

* maps common column aliases onto canonical fields;
* parses dates tolerantly (ISO, RU ``d.m.Y``, dotted, slashed, epochs);
* parses numeric coordinates/ratings tolerantly (comma or dot decimals);
* coerce ratings to 1..5 integer stars;
* collects recoverable issues as warnings instead of dropping the row.

Malformed rows (missing key ids, unparsable rating that cannot be skipped,
impossible coordinates, bad date in a way that makes analysis unsafe) are
rejected and counted as skipped.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from pydantic import BaseModel, Field

from reviewscope.models.review import NormalizedReview

# ---------------------------------------------------------------------------
# Column aliases mapped onto canonical field names.
# ---------------------------------------------------------------------------

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "review_id": ("review_id", "id", "reviewid", "unique_id", "review_uuid"),
    "place_id": ("place_id", "placeid", "organization_id", "org_id", "venue_id"),
    "place_name": ("place_name", "organization_name", "org_name", "vendor", "place"),
    "place_category": ("place_category", "category", "org_category", "business_type"),
    "reviewer_id": ("reviewer_id", "user_id", "reviewerid", "author_id", "account_id"),
    "reviewer_name": ("reviewer_name", "reviewer", "author", "username", "nickname"),
    "rating": ("rating", "score", "stars", "rating_value", "rate"),
    "text": ("text", "review_text", "review", "comment", "content", "feedback"),
    "published_at": (
        "published_at",
        "date",
        "date_published",
        "created_at",
        "timestamp",
        "dt",
    ),
    "city": ("city", "town"),
    "region": ("region", "area", "oblast", "state"),
    "country": ("country", "cc"),
    "latitude": ("latitude", "lat", "geo_lat"),
    "longitude": ("longitude", "lon", "lng", "geo_lng"),
    "source": ("source", "data_source", "platform"),
    "source_url": ("source_url", "url", "link"),
}

_CANONICAL_FIELDS = set(COLUMN_ALIASES)


def map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map raw row keys onto canonical field names via COLUMN_ALIASES.

    Unknown keys are ignored. For aliases that collide (e.g. both ``id`` and
    ``review_id`` present) the canonical key wins.
    """
    lower = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    mapped: dict[str, Any] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        value = None
        if canonical in lower:
            value = lower[canonical]
        else:
            for alias in aliases:
                if alias in lower:
                    value = lower[alias]
                    break
        if value is not None:
            mapped[canonical] = value
    return mapped


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value).strip()


def _is_blank(text: str | None) -> bool:
    return text is None or not text.strip()


# ---------------------------------------------------------------------------
# Type coercion helpers.
# ---------------------------------------------------------------------------


def parse_float(value: Any) -> float | None:
    """Parse a float tolerantly (comma or dot decimal separator)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    """Parse an integer tolerantly (also accepts floats and star emojis)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%Y.%m.%d",
    "%Y.%m.%d %H:%M",
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y",
    "%Y/%m/%d",
)


def parse_date(value: Any) -> _dt.datetime | None:
    """Parse a date into a :class:`datetime.datetime`.

    Supports ISO-8601 forms, Russian ``d.m.Y``, dotted/slashed variants and
    unix epoch seconds. Returns ``None`` when unparsable.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _dt.datetime.fromtimestamp(float(value), tz=_dt.UTC).replace(
                tzinfo=None
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    # ISO with timezone suffix (Z/+03:00)
    iso = text.replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_dt.UTC).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------


class ValidationReport(BaseModel):
    """Per-import statistics reported to the user (SPEC.md §6)."""

    total_rows: int = 0
    valid: int = 0
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def summary_display(self) -> str:
        return (
            f"Imported: {self.total_rows:,}\n"
            f"Valid: {self.valid:,}\n"
            f"Skipped: {self.skipped:,}\n"
            f"Warnings: {self.warning_count:,}"
        )

    def add_warning(self, row_index: int, message: str) -> None:
        self.warnings.append(f"row #{row_index}: {message}")


# ---------------------------------------------------------------------------
# Per-row normalization
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = ("review_id", "place_id", "reviewer_id")
_COORDINATE_RANGE = {"latitude": (-90.0, 90.0), "longitude": (-180.0, 180.0)}


def _normalize_coordinate(name: str, value: Any) -> float | None:
    parsed = parse_float(value)
    if parsed is None:
        return None
    lo, hi = _COORDINATE_RANGE[name]
    if not (lo <= parsed <= hi):
        return None
    return parsed


def normalize_review(row: dict[str, Any]) -> tuple[NormalizedReview | None, list[str]]:
    """Build an :class:`NormalizedReview` from a raw (unmapped) row.

    Returns ``(None, warnings)`` when the row is unrecoverable (missing keys,
    unusable rating); returns ``(review, warnings)`` otherwise. Warnings list
    recovers minor quality issues.
    """
    warnings: list[str] = []
    mapped = map_row(row)

    for key in _REQUIRED_KEYS:
        if not _coerce_str(mapped.get(key)):
            return None, [f"missing required key '{key}'"]

    text = _coerce_str(mapped.get("text"))
    rating_raw = parse_int(mapped.get("rating"))
    rating: int | None = None
    if rating_raw is not None and 1 <= rating_raw <= 5:
        rating = rating_raw
    elif rating_raw is not None:
        warnings.append(f"rating {rating_raw} out of 1..5, dropped")
    else:
        warnings.append("missing or unparsable rating")

    published = parse_date(mapped.get("published_at"))
    published_str: str | None = None
    if published is not None:
        published_str = published.isoformat()
    elif _coerce_str(mapped.get("published_at")) is not None:
        warnings.append(
            f"unparsable date {mapped.get('published_at')!r}, kept raw"
        )
        published_str = _coerce_str(mapped.get("published_at"))

    latitude = _normalize_coordinate("latitude", mapped.get("latitude"))
    longitude = _normalize_coordinate("longitude", mapped.get("longitude"))
    if latitude is None and parse_float(mapped.get("latitude")) is not None:
        warnings.append("latitude outside [-90,90] or unparsable, dropped")
    if longitude is None and parse_float(mapped.get("longitude")) is not None:
        warnings.append("longitude outside [-180,180] or unparsable, dropped")

    if text is None:
        warnings.append("missing review text")

    try:
        review = NormalizedReview(
            review_id=str(mapped["review_id"]).strip(),
            place_id=str(mapped["place_id"]).strip(),
            place_name=_coerce_str(mapped.get("place_name")),
            place_category=_coerce_str(mapped.get("place_category")),
            reviewer_id=str(mapped["reviewer_id"]).strip(),
            reviewer_name=_coerce_str(mapped.get("reviewer_name")),
            rating=rating,
            text=text,
            published_at=published_str,
            city=_coerce_str(mapped.get("city")),
            region=_coerce_str(mapped.get("region")),
            country=_coerce_str(mapped.get("country")),
            latitude=latitude,
            longitude=longitude,
            source=_coerce_str(mapped.get("source")),
            source_url=_coerce_str(mapped.get("source_url")),
        )
    except Exception as exc:  # pydantic ValidationError and any custom one
        return None, [f"invalid record: {exc}"]
    return review, warnings
