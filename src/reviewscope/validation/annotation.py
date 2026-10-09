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

A batch has an explicit lifecycle, recorded in a singleton ``annotation_batch``
row:

* ``OPEN``       — labels may be written freely (the normal annotation state);
* ``FINALIZED``  — the batch is locked: the analyst declared it complete and
  bound it to a ``dataset_fingerprint``. ``save_label`` then refuses to write
  unless ``override=True``, and an override archives the superseded verdict in
  ``annotation_revisions`` instead of silently destroying provenance. This keeps
  "my labels are frozen and tied to this exact dataset" honest even when the
  underlying DuckDB file is still writable.

This module deliberately knows nothing about ReviewScope scores. The only way
a score can reach a label is through a later, explicit, separate join by the
report step — never through this store.
"""

from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS annotation_batch (
    id INTEGER PRIMARY KEY,
    status VARCHAR NOT NULL,
    finalized_at VARCHAR,
    annotator_id VARCHAR,
    dataset_fingerprint VARCHAR,
    label_count INTEGER,
    schema_version VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS annotation_revision_seq;

CREATE TABLE IF NOT EXISTS annotation_revisions (
    revision_id BIGINT PRIMARY KEY,
    review_id VARCHAR,
    revised_at VARCHAR,
    annotator_id VARCHAR,
    previous_label_json VARCHAR
);
"""

_COLUMNS = [c for c in LABEL_COLUMNS if c != "review_id"]

BATCH_STATUS_OPEN = "OPEN"
BATCH_STATUS_FINALIZED = "FINALIZED"
_BATCH_ID = 1


class AnnotationStoreError(RuntimeError):
    """Base class for annotation-store lifecycle errors."""


class BatchFinalizedError(AnnotationStoreError):
    """Raised when writing to a batch that has already been finalized."""


class FingerprintMismatchError(AnnotationStoreError):
    """Raised when a label batch and a dataset fingerprint do not agree."""


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
        override: bool = False,
    ) -> None:
        """UPSERT one human verdict. ``overwrite=False`` keeps an existing row.

        Once the batch is ``FINALIZED`` writes are refused with
        :class:`BatchFinalizedError` unless ``override=True``. A granted
        override first archives the superseded row in ``annotation_revisions``.
        """
        finalized = self.is_finalized()
        if finalized and not override:
            raise BatchFinalizedError(
                "annotation batch is FINALIZED; pass override=True to revise it"
            )
        existing = self.get_label(label.review_id)
        if existing is not None and not overwrite:
            return
        if finalized and override and existing is not None:
            self._archive_revision(existing)
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

    # -- batch lifecycle -------------------------------------------------------

    def batch_metadata(self) -> dict | None:
        """Return the singleton batch row, or ``None`` while the batch is OPEN."""
        row = self._con.execute(
            "SELECT status, finalized_at, annotator_id, dataset_fingerprint, "
            "label_count, schema_version FROM annotation_batch WHERE id = ?",
            [_BATCH_ID],
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "finalized_at": row[1],
            "annotator_id": row[2],
            "dataset_fingerprint": row[3],
            "label_count": row[4],
            "schema_version": row[5],
        }

    def batch_status(self) -> str:
        """``"OPEN"`` until :meth:`finalize` is called, then ``"FINALIZED"``."""
        metadata = self.batch_metadata()
        return str(metadata["status"]) if metadata else BATCH_STATUS_OPEN

    def is_finalized(self) -> bool:
        return self.batch_status() == BATCH_STATUS_FINALIZED

    def finalize(
        self,
        *,
        annotator_id: str | None = None,
        dataset_fingerprint: str | None = None,
        label_count: int | None = None,
        override: bool = False,
    ) -> dict:
        """Lock the batch and bind it to the dataset it was labelled against.

        The label count defaults to the number of verdicts currently stored.
        Re-finalizing a finalized batch is refused unless ``override=True``.
        A different ``dataset_fingerprint`` can never overwrite a stored one
        (that would silently relabel a different dataset): it raises
        :class:`FingerprintMismatchError`.
        """
        metadata = self.batch_metadata()
        if metadata and metadata["status"] == BATCH_STATUS_FINALIZED and not override:
            raise BatchFinalizedError(
                "annotation batch is already FINALIZED; pass override=True to re-finalize"
            )
        stored = metadata.get("dataset_fingerprint") if metadata else None
        if (
            stored
            and dataset_fingerprint
            and stored != dataset_fingerprint
        ):
            raise FingerprintMismatchError(
                f"dataset fingerprint mismatch: stored {stored!r}, "
                f"got {dataset_fingerprint!r}"
            )
        resolved_fingerprint = dataset_fingerprint or stored
        resolved_count = label_count if label_count is not None else len(self.labeled_ids())
        self._con.execute(
            "INSERT OR REPLACE INTO annotation_batch "
            "(id, status, finalized_at, annotator_id, dataset_fingerprint, "
            "label_count, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                _BATCH_ID,
                BATCH_STATUS_FINALIZED,
                now_iso(),
                annotator_id,
                resolved_fingerprint,
                resolved_count,
                ANNOTATION_SCHEMA_VERSION,
            ],
        )
        return self.batch_metadata() or {}

    def verify_fingerprint(self, expected: str) -> bool:
        """Assert the finalized batch was labelled against ``expected``.

        Returns ``True`` on a match; raises :class:`FingerprintMismatchError`
        when the batch is not finalized, has no recorded fingerprint, or the
        fingerprint differs.
        """
        metadata = self.batch_metadata()
        stored = metadata.get("dataset_fingerprint") if metadata else None
        if not metadata or metadata["status"] != BATCH_STATUS_FINALIZED:
            raise FingerprintMismatchError("annotation batch is not finalized")
        if not stored:
            raise FingerprintMismatchError(
                "annotation batch has no recorded dataset fingerprint"
            )
        if stored != expected:
            raise FingerprintMismatchError(
                f"dataset fingerprint mismatch: stored {stored!r}, got {expected!r}"
            )
        return True

    def revision_count(self) -> int:
        """Number of superseded label versions archived by overrides."""
        return int(
            self._con.execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0]
        )

    def revisions(self, review_id: str | None = None) -> list[dict]:
        """Archived label revisions, newest first (optionally per review)."""
        if review_id is None:
            rows = self._con.execute(
                "SELECT revision_id, review_id, revised_at, annotator_id, "
                "previous_label_json FROM annotation_revisions "
                "ORDER BY revision_id DESC"
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT revision_id, review_id, revised_at, annotator_id, "
                "previous_label_json FROM annotation_revisions "
                "WHERE review_id = ? ORDER BY revision_id DESC",
                [review_id],
            ).fetchall()
        return [
            {
                "revision_id": row[0],
                "review_id": row[1],
                "revised_at": row[2],
                "annotator_id": row[3],
                "previous_label": json.loads(row[4]) if row[4] else None,
            }
            for row in rows
        ]

    def _archive_revision(self, label: ReviewHumanLabel) -> None:
        revision_id = self._con.execute(
            "SELECT nextval('annotation_revision_seq')"
        ).fetchone()[0]
        self._con.execute(
            "INSERT INTO annotation_revisions "
            "(revision_id, review_id, revised_at, annotator_id, previous_label_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                revision_id,
                label.review_id,
                label.labeled_at or now_iso(),
                label.annotator_id,
                json.dumps(label.model_dump(mode="json"), ensure_ascii=False),
            ],
        )
