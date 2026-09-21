"""Phase 15 — tolerant label/template/dataset I/O regression tests."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from reviewscope.validation.loader import (
    label_template_rows,
    load_reviews,
    load_selection_header,
    load_selection_json,
    read_events,
    read_labels,
    write_events,
    write_label_template,
    write_labels,
)
from reviewscope.validation.models import (
    DuplicateLabel,
    EventLabel,
    EventLabelSet,
    ReviewHumanLabel,
    ReviewLabelSet,
    SampleType,
    SpecificityLabel,
    TemplatedLabel,
)


def test_labels_write_read_roundtrip(tmp_path):
    labelset = ReviewLabelSet(
        labels=[
            ReviewHumanLabel(
                review_id="r1",
                templated_label=TemplatedLabel.ORGANIC,
                specificity_label=SpecificityLabel.HIGH,
                duplicate_label=DuplicateLabel.UNIQUE,
                annotator_id="alice",
                sample_type=SampleType.EVALUATION,
                sampling_stratum="random",
                reviewer_notes="looks fine",
            ),
            ReviewHumanLabel(
                review_id="r2",
                templated_label=TemplatedLabel.TEMPLATED,
                template_group_id="grp-1",
            ),
        ]
    )
    path = tmp_path / "labels.csv"
    write_labels(labelset, path)
    reloaded = read_labels(path)
    assert reloaded.labels[0].review_id == "r1"
    assert reloaded.labels[0].templated_label == TemplatedLabel.ORGANIC
    assert reloaded.labels[0].sample_type == SampleType.EVALUATION
    assert reloaded.labels[0].reviewer_notes == "looks fine"
    assert reloaded.labels[1].template_group_id == "grp-1"
    assert reloaded.labels[1].templated_label == TemplatedLabel.TEMPLATED


def test_read_labels_tolerates_unknown_enum(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame(
        [
            {"review_id": "r1", "templated_label": "maybe"},
            {"review_id": "r2", "templated_label": "organic"},
        ]
    ).to_csv(path, index=False)
    labelset = read_labels(path)
    assert labelset.labels[0].templated_label is None
    assert labelset.labels[1].templated_label == TemplatedLabel.ORGANIC
    assert any("unknown templatedlabel value 'maybe'" in w for w in labelset.warnings)


def test_read_labels_consistency_warnings(tmp_path):
    path = tmp_path / "dup_warning.csv"
    pd.DataFrame(
        [
            {"review_id": "r1", "duplicate_label": "duplicate"},  # no group id
            {"review_id": "r2", "duplicate_group_id": "g1", "duplicate_label": "unique"},
        ]
    ).to_csv(path, index=False)
    labelset = read_labels(path)
    joined = "\n".join(labelset.warnings)
    assert "duplicate label without" in joined
    assert "duplicate_group_id set but" in joined


def test_read_labels_missing_review_id_skipped(tmp_path):
    path = tmp_path / "missing.csv"
    pd.DataFrame([{"review_id": "", "templated_label": "organic"}]).to_csv(path, index=False)
    labelset = read_labels(path)
    assert labelset.labels == []
    assert any("missing review_id" in w for w in labelset.warnings)


def test_write_label_template_preserves_order_and_provenance(tmp_path):
    selection = {
        "r2": {"sample_type": "challenge", "sampling_stratum": "high"},
        "r1": {"sample_type": "evaluation", "sampling_stratum": "random"},
    }
    rows = label_template_rows(["r2", "r1"], selection=selection, annotator_id="bob")
    assert [row.review_id for row in rows] == ["r2", "r1"]
    assert rows[0].sample_type == SampleType.CHALLENGE
    assert rows[0].sampling_stratum == "high"
    assert rows[1].sample_type == SampleType.EVALUATION
    assert rows[1].annotator_id == "bob"
    assert rows[0].templated_label is None

    path = tmp_path / "template.csv"
    write_label_template(["r2", "r1"], path, selection=selection, annotator_id="bob")
    frame = pd.read_csv(path, dtype=str)
    assert list(frame["review_id"]) == ["r2", "r1"]
    assert list(frame["sample_type"]) == ["challenge", "evaluation"]


def test_events_write_read_roundtrip(tmp_path):
    eventset = EventLabelSet(
        events=[
            EventLabel(
                place_id="p1",
                event_id="e1",
                polarity="negative",
                coordinated_label="suspicious",
                notes="looks coordinated",
            )
        ]
    )
    path = tmp_path / "events.csv"
    write_events(eventset, path)
    reloaded = read_events(path)
    assert reloaded.events[0].place_id == "p1"
    assert reloaded.events[0].coordinated_label.value == "suspicious"
    assert reloaded.events[0].notes == "looks coordinated"


def test_load_selection_json(tmp_path):
    payload = {
        "entries": [
            {"review_id": "r1", "sample_type": "evaluation", "sampling_stratum": "random"},
            {"review_id": "r2", "sample_type": "challenge", "sampling_stratum": "high"},
        ]
    }
    path = tmp_path / "sample_selection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    selection = load_selection_json(path)
    assert selection["r1"]["sample_type"] == "evaluation"
    assert list(selection.keys()) == ["r1", "r2"]


def test_load_selection_header(tmp_path):
    payload = {
        "fingerprint": "v1|2|1|abc|def|model",
        "seed": 20260901,
        "evaluation_count": 1,
        "challenge_counts": {"high": 1},
        "entries": [
            {"review_id": "r1", "sample_type": "evaluation", "sampling_stratum": "random"},
        ],
    }
    path = tmp_path / "sample_selection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    header = load_selection_header(path)
    assert header["fingerprint"] == "v1|2|1|abc|def|model"
    assert header["seed"] == 20260901
    assert "entries" not in header


def test_load_reviews_csv(tmp_path):
    path = tmp_path / "reviews.csv"
    pd.DataFrame(
        [
            {
                "review_id": "r1",
                "place_id": "p1",
                "reviewer_id": "u1",
                "rating": 5,
                "text": "Всё отлично",
            }
        ]
    ).to_csv(path, index=False)
    result = load_reviews(path)
    assert result.report.valid == 1
    assert result.reviews[0].review_id == "r1"


@pytest.mark.parametrize("suffix", ["json"])
def test_load_reviews_json(tmp_path, suffix):
    path = tmp_path / f"reviews.{suffix}"
    path.write_text(
        json.dumps(
            {"reviews": [{"review_id": "r1", "place_id": "p1", "author_id": "u1", "rating": "5", "text": "ok"}]}
        ),
        encoding="utf-8",
    )
    result = load_reviews(path)
    assert result.report.valid == 1 or result.reviews
    assert result.reviews[0].review_id == "r1"
