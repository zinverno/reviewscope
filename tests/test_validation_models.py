"""Phase 15 — human label model + on-disk schema regression tests."""

from __future__ import annotations

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


def test_schema_version_stable():
    assert ANNOTATION_SCHEMA_VERSION == "1.0"


def test_label_columns_defined_once_and_unique():
    assert len(LABEL_COLUMNS) == len(set(LABEL_COLUMNS))
    assert LABEL_COLUMNS[0] == "review_id"
    assert "templated_label" in LABEL_COLUMNS
    assert "sample_type" in LABEL_COLUMNS
    assert "sampling_stratum" in LABEL_COLUMNS
    assert "annotator_id" in LABEL_COLUMNS


def test_event_label_columns_defined_once_and_unique():
    assert len(EVENT_LABEL_COLUMNS) == len(set(EVENT_LABEL_COLUMNS))
    assert "place_id" in EVENT_LABEL_COLUMNS
    assert "event_id" in EVENT_LABEL_COLUMNS


def test_review_human_label_defaults():
    label = ReviewHumanLabel(review_id="r42")
    assert label.review_id == "r42"
    assert label.templated_label is None
    assert label.annotation_schema_version == ANNOTATION_SCHEMA_VERSION
    assert not label.has_templated_verdict()
    assert not label.has_specificity_verdict()
    assert not label.has_duplicate_verdict()


def test_review_human_label_verdict_helpers():
    label = ReviewHumanLabel(
        review_id="r1",
        templated_label=TemplatedLabel.TEMPLATED,
        specificity_label=SpecificityLabel.LOW,
        duplicate_label=DuplicateLabel.DUPLICATE,
    )
    assert label.has_templated_verdict()
    assert label.has_specificity_verdict()
    assert label.has_duplicate_verdict()

    uncertain = ReviewHumanLabel(
        review_id="r2",
        templated_label=TemplatedLabel.UNCERTAIN,
        duplicate_label=DuplicateLabel.UNCERTAIN,
        specificity_label=SpecificityLabel.UNCERTAIN,
    )
    assert not uncertain.has_templated_verdict()
    assert not uncertain.has_specificity_verdict()
    assert not uncertain.has_duplicate_verdict()


def test_review_human_label_ignores_extra_fields():
    label = ReviewHumanLabel(review_id="r3", reviewscope_score=99.9, anything="x")
    assert label.review_id == "r3"
    # Scores must never be smuggled into a label model via extra fields.
    assert not hasattr(label, "reviewscope_score")


def test_stratified_sample_types():
    assert SampleType.EVALUATION.value == "evaluation"
    assert SampleType.CHALLENGE.value == "challenge"
    assert len({s.value for s in SampleType}) == 2


def test_event_label_model():
    event = EventLabel(
        place_id="p1",
        event_id="evt-1",
        polarity=Polarity.NEGATIVE,
        coordinated_label=CoordinatedLabel.SUSPICIOUS,
    )
    assert event.polarity == Polarity.NEGATIVE
    assert event.coordinated_label == CoordinatedLabel.SUSPICIOUS
    event_set = EventLabelSet(events=[event])
    assert len(event_set.events) == 1


def test_label_set_helpers():
    labels = [
        ReviewHumanLabel(review_id="a"),
        ReviewHumanLabel(review_id="b"),
    ]
    review_set = ReviewLabelSet(labels=labels)
    assert review_set.ids() == ["a", "b"]
    assert set(review_set.by_id()) == {"a", "b"}
