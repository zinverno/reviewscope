"""Phase 15 — persisted, resumable annotation store regression tests."""

from __future__ import annotations

import pytest

from reviewscope.validation.annotation import (
    AnnotationStore,
    BatchFinalizedError,
    FingerprintMismatchError,
    now_iso,
)
from reviewscope.validation.models import (
    DuplicateLabel,
    ReviewHumanLabel,
    SampleType,
    SpecificityLabel,
    TemplatedLabel,
)


def test_now_iso_utc():
    stamp = now_iso()
    assert stamp.endswith("+00:00") or stamp.endswith("Z")
    assert "T" in stamp


def test_save_and_resume(tmp_path):
    path = tmp_path / "annotations.duckdb"
    with AnnotationStore(path) as store:
        store.save_label(
            ReviewHumanLabel(
                review_id="r1",
                templated_label=TemplatedLabel.ORGANIC,
                specificity_label=SpecificityLabel.HIGH,
                duplicate_label=DuplicateLabel.UNIQUE,
                annotator_id="alice",
                sample_type=SampleType.EVALUATION,
                sampling_stratum="random",
                reviewer_notes="trustworthy",
            )
        )
        row = store.get_label("r1")
        assert row.templated_label == TemplatedLabel.ORGANIC
        assert row.specificity_label == SpecificityLabel.HIGH
        assert row.duplicate_label == DuplicateLabel.UNIQUE
        assert row.annotator_id == "alice"
        assert row.sample_type == SampleType.EVALUATION
        assert row.sampling_stratum == "random"
        assert row.reviewer_notes == "trustworthy"
        assert row.labeled_at, "labeled_at must be stamped on save"

    # Resume: reopen the file and confirm the previous verdict is still there.
    with AnnotationStore(path) as resumed:
        assert resumed.labeled_ids() == ["r1"]
        assert resumed.get_label("r1").templated_label == TemplatedLabel.ORGANIC
        assert resumed.unlabeled_ids(["r1", "r2", "r3"]) == ["r2", "r3"]
        labelset = resumed.labelset()
        assert [label.review_id for label in labelset.labels] == ["r1"]
        progress = resumed.progress()
        assert progress["labeled"] == 1
        assert progress["by_sample_type"]["evaluation"]["total"] == 1


def test_upsert_updates_verdict(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.save_label(ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.ORGANIC))
        store.save_label(ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.TEMPLATED,
                                          annotator_id="bob"))
        row = store.get_label("r1")
        assert row.templated_label == TemplatedLabel.TEMPLATED
        assert row.annotator_id == "bob"
        assert store.labeled_ids() == ["r1"]


def test_overwrite_false_keeps_existing(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.save_label(ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.ORGANIC))
        store.save_label(
            ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.TEMPLATED),
            overwrite=False,
        )
        assert store.get_label("r1").templated_label == TemplatedLabel.ORGANIC


def test_multiple_labelset_export(tmp_path):
    path = tmp_path / "ann.duckdb"
    with AnnotationStore(path) as store:
        store.save_label(ReviewHumanLabel(review_id="a", templated_label=TemplatedLabel.ORGANIC))
        store.save_label(ReviewHumanLabel(review_id="b", templated_label=TemplatedLabel.TEMPLATED))
        assert store.progress()["labeled"] == 2
        csv_path = tmp_path / "labels.csv"
        store.write_labels_csv(csv_path)
    assert csv_path.exists()
    from reviewscope.validation.loader import read_labels

    reloaded = read_labels(csv_path)
    assert [label.review_id for label in reloaded.labels] == ["a", "b"]
    assert reloaded.labels[0].templated_label == TemplatedLabel.ORGANIC
    assert reloaded.labels[1].templated_label == TemplatedLabel.TEMPLATED


def test_blank_cells_map_to_none(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.save_label(ReviewHumanLabel(review_id="r1", annotator_id="carla"))
        row = store.get_label("r1")
        assert row.templated_label is None
        assert row.labeled_at is not None


def test_batch_is_open_by_default(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        assert store.batch_status() == "OPEN"
        assert store.is_finalized() is False
        assert store.batch_metadata() is None


def test_finalize_persists_metadata_and_resumes(tmp_path):
    path = tmp_path / "ann.duckdb"
    with AnnotationStore(path) as store:
        store.save_label(ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.ORGANIC))
        store.save_label(ReviewHumanLabel(review_id="r2", templated_label=TemplatedLabel.TEMPLATED))
        metadata = store.finalize(annotator_id="alice", dataset_fingerprint="fp-123")
        assert metadata["status"] == "FINALIZED"
        assert metadata["annotator_id"] == "alice"
        assert metadata["dataset_fingerprint"] == "fp-123"
        assert metadata["label_count"] == 2, "label count defaults to the stored verdicts"
        assert metadata["finalized_at"]
        assert store.is_finalized() is True
        assert store.verify_fingerprint("fp-123") is True

    with AnnotationStore(path) as resumed:
        assert resumed.is_finalized() is True
        assert resumed.batch_metadata()["dataset_fingerprint"] == "fp-123"
        assert len(resumed.labelset().labels) == 2


def test_finalized_batch_rejects_writes(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.save_label(ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.ORGANIC))
        store.finalize(annotator_id="alice", dataset_fingerprint="fp-1")
        with pytest.raises(BatchFinalizedError):
            store.save_label(
                ReviewHumanLabel(review_id="r2", templated_label=TemplatedLabel.TEMPLATED)
            )
        with pytest.raises(BatchFinalizedError):
            store.save_label(
                ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.TEMPLATED)
            )
        assert store.get_label("r1").templated_label == TemplatedLabel.ORGANIC
        assert store.labeled_ids() == ["r1"]


def test_override_archives_previous_verdict(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.save_label(
            ReviewHumanLabel(
                review_id="r1",
                templated_label=TemplatedLabel.ORGANIC,
                annotator_id="alice",
            )
        )
        store.finalize(annotator_id="alice", dataset_fingerprint="fp-1")
        store.save_label(
            ReviewHumanLabel(review_id="r1", templated_label=TemplatedLabel.TEMPLATED),
            override=True,
        )
        assert store.get_label("r1").templated_label == TemplatedLabel.TEMPLATED
        assert store.revision_count() == 1
        revision = store.revisions("r1")[0]
        assert revision["previous_label"]["templated_label"] == "organic"
        assert revision["previous_label"]["annotator_id"] == "alice"


def test_refinalize_requires_override(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.finalize(annotator_id="alice", dataset_fingerprint="fp-1")
        with pytest.raises(BatchFinalizedError):
            store.finalize(annotator_id="bob", dataset_fingerprint="fp-1")
        metadata = store.finalize(annotator_id="bob", dataset_fingerprint="fp-1", override=True)
        assert metadata["annotator_id"] == "bob"


def test_fingerprint_mismatch_is_rejected(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        store.finalize(annotator_id="alice", dataset_fingerprint="fp-A")
        with pytest.raises(FingerprintMismatchError):
            store.finalize(annotator_id="alice", dataset_fingerprint="fp-B", override=True)
        with pytest.raises(FingerprintMismatchError):
            store.verify_fingerprint("fp-B")
        assert store.batch_metadata()["dataset_fingerprint"] == "fp-A"


def test_verify_fingerprint_requires_finalized_batch(tmp_path):
    with AnnotationStore(tmp_path / "ann.duckdb") as store:
        with pytest.raises(FingerprintMismatchError):
            store.verify_fingerprint("fp-A")
