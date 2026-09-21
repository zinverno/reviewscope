"""Phase 15 — persisted, resumable annotation store regression tests."""

from __future__ import annotations

from reviewscope.validation.annotation import AnnotationStore, now_iso
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
