"""Loaders/writers for review datasets, human label files and event labels.

All public functions are pure I/O helpers: they read and write the on-disk
schema owned by ``validation/models.py`` and collect recoverable issues as
warnings instead of raising, matching the tolerant import philosophy of the
rest of ReviewScope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from reviewscope.ingestion.base import LoadResult
from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.ingestion.json_adapter import JSONAdapter
from reviewscope.validation.models import (
    ANNOTATION_SCHEMA_VERSION,
    EVENT_LABEL_COLUMNS,
    LABEL_COLUMNS,
    CoordinatedLabel,
    DuplicateLabel,
    EventLabel,
    EventLabelSet,
    Polarity,
    ReviewHumanLabel,
    ReviewLabelSet,
    SampleType,
    SpecificityLabel,
    TemplatedLabel,
)

_ENUM_FIELDS = {
    "templated_label": TemplatedLabel,
    "specificity_label": SpecificityLabel,
    "duplicate_label": DuplicateLabel,
    "sample_type": SampleType,
}

_EVENT_ENUM_FIELDS = {
    "polarity": Polarity,
    "coordinated_label": CoordinatedLabel,
}


def load_reviews(path: str | Path) -> LoadResult:
    """Load a review dataset through the existing ingestion adapters.

    ``.json`` files go through :class:`JSONAdapter`, everything else through
    :class:`CSVAdapter` — the exact production ingestion paths (SPEC.md §4-§6).
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return JSONAdapter().load(path)
    return CSVAdapter().load(path)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _coerce_enum(enum_cls: type, value: str | None) -> tuple[object | None, str | None]:
    """Return ``(enum|None, warning|None)`` for a raw label cell."""
    cleaned = _clean(value)
    if cleaned is None:
        return None, None
    mapping = {member.value: member for member in enum_cls}
    if cleaned in mapping:
        return mapping[cleaned], None
    return None, f"unknown {enum_cls.__name__.lower()} value {value!r}, dropped"


def read_labels(path: str | Path) -> ReviewLabelSet:
    """Read a human-label CSV tolerantly.

    Missing columns are allowed (they are treated as unlabeled). Invalid enum
    values become ``None`` with a warning. Cross-field inconsistencies
    (e.g. ``duplicate`` label without a group id) are warnings, not errors.
    """
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    labels: list[ReviewHumanLabel] = []
    warnings: list[str] = []
    for index, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row = {str(k): v for k, v in raw.items()}
        review_id = _clean(row.get("review_id"))
        if review_id is None:
            warnings.append(f"row #{index}: missing review_id, skipped")
            continue

        values: dict[str, object] = {"review_id": review_id}
        for column in LABEL_COLUMNS:
            if column in ("review_id",):
                continue
            if column not in row:
                # Omit so defaulted fields (e.g. annotation_schema_version)
                # keep their model defaults instead of being forced to None.
                continue
            if column == "templated_label":
                value, warn = _coerce_enum(TemplatedLabel, row[column])
                values[column] = value
                if warn:
                    warnings.append(f"row #{index}: {warn}")
            elif column == "specificity_label":
                value, warn = _coerce_enum(SpecificityLabel, row[column])
                values[column] = value
                if warn:
                    warnings.append(f"row #{index}: {warn}")
            elif column == "duplicate_label":
                value, warn = _coerce_enum(DuplicateLabel, row[column])
                values[column] = value
                if warn:
                    warnings.append(f"row #{index}: {warn}")
            elif column == "sample_type":
                value, warn = _coerce_enum(SampleType, row[column])
                values[column] = value
                if warn:
                    warnings.append(f"row #{index}: {warn}")
            else:
                clean = _clean(row.get(column))
                values[column] = clean if clean is not None else ""

        label = ReviewHumanLabel(**values)
        warnings.extend(_label_consistency_warnings(label, row_index=index))
        labels.append(label)
    return ReviewLabelSet(labels=labels, warnings=warnings)


def _label_consistency_warnings(label: ReviewHumanLabel, *, row_index: int) -> list[str]:
    warnings: list[str] = []
    if label.duplicate_label == DuplicateLabel.DUPLICATE and not label.duplicate_group_id:
        warnings.append(
            f"row #{row_index} ({label.review_id}): duplicate label without "
            "duplicate_group_id"
        )
    if label.duplicate_group_id and label.duplicate_label != DuplicateLabel.DUPLICATE:
        warnings.append(
            f"row #{row_index} ({label.review_id}): duplicate_group_id set but "
            "duplicate_label is not 'duplicate'"
        )
    if label.template_group_id and label.templated_label == TemplatedLabel.ORGANIC:
        warnings.append(
            f"row #{row_index} ({label.review_id}): template_group_id set but "
            "templated_label is 'organic'"
        )
    return warnings


def write_labels(labelset: ReviewLabelSet, path: str | Path) -> None:
    """Write a label set to CSV in the canonical column order."""
    rows = [
        {column: getattr(label, column) if hasattr(label, column) else None for column in LABEL_COLUMNS}
        for label in labelset.labels
    ]
    frame = pd.DataFrame(rows, columns=LABEL_COLUMNS)
    frame.to_csv(path, index=False)


def read_events(path: str | Path) -> EventLabelSet:
    """Read an event-label CSV tolerantly (future coordinated-activity phase)."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    events: list[EventLabel] = []
    warnings: list[str] = []
    for index, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row = {str(k): v for k, v in raw.items()}
        place_id = _clean(row.get("place_id"))
        event_id = _clean(row.get("event_id"))
        if place_id is None or event_id is None:
            warnings.append(f"row #{index}: missing place_id/event_id, skipped")
            continue
        values: dict[str, object] = {"place_id": place_id, "event_id": event_id}
        for column in EVENT_LABEL_COLUMNS:
            if column in ("place_id", "event_id"):
                continue
            if column in _EVENT_ENUM_FIELDS:
                value, warn = _coerce_enum(_EVENT_ENUM_FIELDS[column], row.get(column))
                values[column] = value
                if warn:
                    warnings.append(f"row #{index}: {warn}")
            else:
                values[column] = _clean(row.get(column)) or ""
        events.append(EventLabel(**values))
    return EventLabelSet(events=events, warnings=warnings)


def write_events(eventset: EventLabelSet, path: str | Path) -> None:
    rows = [
        {
            column: getattr(event, column) if hasattr(event, column) else None
            for column in EVENT_LABEL_COLUMNS
        }
        for event in eventset.events
    ]
    frame = pd.DataFrame(rows, columns=EVENT_LABEL_COLUMNS)
    frame.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def label_template_rows(
    review_ids: list[str],
    *,
    selection: dict[str, dict] | None = None,
    annotator_id: str | None = None,
) -> list[ReviewHumanLabel]:
    """Build empty label rows preserving ``review_ids`` order.

    ``selection`` maps ``review_id -> {"sample_type": ..., "sampling_stratum":
    ...}`` and optionally carries ``template_group_id`` from a previous label
    (unused here). Provenance columns are pre-filled so the annotator does not
    have to type them again.
    """
    rows: list[ReviewHumanLabel] = []
    for review_id in review_ids:
        entry = (selection or {}).get(review_id, {})
        rows.append(
            ReviewHumanLabel(
                review_id=review_id,
                annotation_schema_version=ANNOTATION_SCHEMA_VERSION,
                annotator_id=annotator_id,
                sample_type=entry.get("sample_type"),
                sampling_stratum=entry.get("sampling_stratum"),
            )
        )
    return rows


def write_label_template(
    review_ids: list[str],
    path: str | Path,
    *,
    selection: dict[str, dict] | None = None,
    annotator_id: str | None = None,
) -> None:
    """Write an empty label template preserving ``review_id`` order."""
    write_labels(
        ReviewLabelSet(labels=label_template_rows(review_ids, selection=selection, annotator_id=annotator_id)),
        path,
    )


def load_selection_json(path: str | Path) -> dict[str, dict]:
    """Load ``sample_selection.json`` into ``review_id -> entry`` map.

    Entries contain ``sample_type`` and ``sampling_stratum`` (and neutral
    context such as ``place_id``), never ReviewScope scores.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw.get("entries", raw if isinstance(raw, list) else [])
    out: dict[str, dict] = {}
    for entry in entries:
        entry = dict(entry)
        review_id = entry.get("review_id")
        if review_id is not None:
            out[str(review_id)] = entry
    return out
