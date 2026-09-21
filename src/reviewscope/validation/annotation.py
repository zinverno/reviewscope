"""Persisted, resumable annotation backend for the blind labeling app (Phase 15).

A ``DuckDBStore``-independent compact table owned by validation data lives in
its own database file under ``validation_data/``:

.. code-block:: text

    CREATE TABLE annotations (
        review_id                 VARCHAR PRIMARY KEY,
        templated_label           VARCHAR,
        template_group_id         VARCHAR,
        specificity_label         VARCHAR,
        duplicate_group_id        VARCHAR,
        duplicate_label           VARCHAR,
        reviewer_notes            VARCHAR,
        annotation_schema_version VARCHAR,
        annotator_id              VARCHAR,
        labeled_at                VARCHAR,
        sample_type               VARCHAR,
        sampling_stratum          VARCHAR
    );

Columns mirror :data:`validation.models.LABEL_COLUMNS` so a label row is
round-trippable with the CSV contract and the Scoring..Report pipeline. Every
write is an UPSERT keyed by ``review_id``, which makes interrupted batches
resumable: the app loads previously saved verdicts and only asks the annotator
for reviews that are still missing.

This module deliberately knows nothing about ReviewScope scores. The only way
a score can reach a label is through a later, explicit, separate join by the
report step — never through this store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb

from reviewscope.validation.loader import write_labels
from reviewscope.validation.models import (
    ANNOTATION_SCHEMA_VERSION,
    LABEL_COLUMNS,
    ReviewHumanLabel,
    ReviewLabelSet,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS annotations (
    review_id VARCHAR PRIMARY KEY,
    templated_label VARCHAR,
    template_group_id VARCHAR,
    specificity_label VARCHAR,
    duplicate_group_id VARCHAR,
    duplicate_label VARCHAR,
    reviewer_notes VARCHAR,
    annotation_schema_version VARCHAR,
    annotator_id VARCHAR,
    labeled_at VARCHAR,
    sample_type VARCHAR,
    sampling_stratum VARCHAR
);
"""

_COLUMNS = [c for c in LABEL_COLUMNS if c != "review_id"]


def now_iso() -> str:
    """UTC ISO-8601 timestamp with second precision (label provenance)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class AnnotationStore:
    """DuckDB-backed label store; re-opening the same path resumes a batch."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        self._con.execute(_SCHEMA_SQL)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> AnnotationStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._con

    # -- writing ---------------------------------------------------------------

    def save_label(
        self,
        label: ReviewHumanLabel,
        *,
        labeled_at: str | None = None,
        overwrite: bool = True,
    ) -> None:
        """UPSERT one human verdict. ``overwrite=False`` keeps an existing row."""
        existing = self.get_label(label.review_id)
        if existing is not None and not overwrite:
            return
        timestamp = labeled_at or now_iso()
        values = {
            "review_id": label.review_id,
            "templated_label": self._enum_value(label.templated_label),
            "template_group_id": label.template_group_id,
            "specificity_label": self._enum_value(label.specificity_label),
            "duplicate_group_id": label.duplicate_group_id,
            "duplicate_label": self._enum_value(label.duplicate_label),
            "reviewer_notes": label.reviewer_notes,
            "annotation_schema_version": label.annotation_schema_version
            or ANNOTATION_SCHEMA_VERSION,
            "annotator_id": label.annotator_id,
            "labeled_at": timestamp,
            "sample_type": self._enum_value(label.sample_type),
            "sampling_stratum": label.sampling_stratum,
        }
        placeholders = ", ".join("?" for _ in values)
        self._con.execute(
            f"INSERT OR REPLACE INTO annotations (review_id, {', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders})",
            [values["review_id"], *[values[c] for c in _COLUMNS]],
        )

    @staticmethod
    def _enum_value(value: object) -> str | None:
        if value is None:
            return None
        return getattr(value, "value", str(value))

    # -- reading ---------------------------------------------------------------

    def get_label(self, review_id: str) -> ReviewHumanLabel | None:
        row = self._con.execute(
            "SELECT review_id, templated_label, template_group_id, specificity_label, "
            "duplicate_group_id, duplicate_label, reviewer_notes, "
            "annotation_schema_version, annotator_id, labeled_at, sample_type, "
            "sampling_stratum FROM annotations WHERE review_id = ?",
            [review_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_label(dict(zip(["review_id", *_COLUMNS], row, strict=True)))

    def _row_to_label(self, values: dict[str, object]) -> ReviewHumanLabel:
        row = {str(k): ("" if v is None else str(v)) for k, v in values.items()}
        blankable = {
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
        }
        for field in blankable:
            if not row.get(field):
                row[field] = None
        return ReviewHumanLabel(**row)

    def labeled_ids(self) -> list[str]:
        rows = self._con.execute("SELECT review_id FROM annotations").fetchall()
        return sorted(r[0] for r in rows)

    def labelset(self) -> ReviewLabelSet:
        """Reconstruct the full label set (order-preserving, tolerant)."""
        rows = self._con.execute(
            "SELECT review_id, templated_label, template_group_id, specificity_label, "
            "duplicate_group_id, duplicate_label, reviewer_notes, "
            "annotation_schema_version, annotator_id, labeled_at, sample_type, "
            "sampling_stratum FROM annotations"
        ).fetchall()
        labels: list[ReviewHumanLabel] = []
        for row in rows:
            values = dict(zip(["review_id", *_COLUMNS], row, strict=True))
            if values.get("review_id") is None:
                continue
            labels.append(self._row_to_label(values))
        return ReviewLabelSet(labels=labels)

    def write_labels_csv(self, path: str | Path) -> None:
        """Export the current annotated rows to the canonical label CSV."""
        write_labels(self.labelset(), path)

    # -- progress / queue ------------------------------------------------------

    def progress(self) -> dict:
        labels = self.labelset().labels
        counts: dict[str, dict[str, int]] = {}
        for label in labels:
            key = label.sample_type.value if label.sample_type else "unattributed"
            bucket = counts.setdefault(key, {})
            bucket["total"] = bucket.get("total", 0) + 1
        return {
            "labeled": len(labels),
            "by_sample_type": counts,
            "last_labeled_at": (labels[-1].labeled_at if labels else None),
        }

    def unlabeled_ids(self, review_ids: list[str]) -> list[str]:
        labeled = set(self.labeled_ids())
        return [rid for rid in review_ids if rid not in labeled]
