"""Phase 15 — report assembly: evaluation/challenge isolation + rendering."""

from __future__ import annotations

import json

from reviewscope.models.review import NormalizedReview
from reviewscope.validation.models import (
    ReviewHumanLabel,
    ReviewLabelSet,
    SampleType,
    SpecificityLabel,
    TemplatedLabel,
)
from reviewscope.validation.report import (
    analyze_and_report,
    render_markdown,
    write_report,
)
from reviewscope.validation.scoring import ReviewOutput, ReviewScoreTable


def _review(review_id: str, place: str = "p1", text="SOME_TEXT") -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place,
        reviewer_id=f"user-{review_id}",
        rating=5,
        text=text,
        published_at="2026-09-01",
    )


def _output(review_id: str, templated: float, specificity: float = 50.0,
            place: str = "p1") -> ReviewOutput:
    return ReviewOutput(
        review_id=review_id,
        place_id=place,
        templated_value=templated,
        templated_confidence="HIGH" if templated >= 65 else "LOW",
        specificity_value=specificity,
    )


def _label(review_id: str, sample_type: SampleType | None, templated=None,
           specificity=None) -> ReviewHumanLabel:
    return ReviewHumanLabel(
        review_id=review_id,
        templated_label=templated,
        specificity_label=specificity,
        sample_type=sample_type,
        sampling_stratum="random" if sample_type == SampleType.EVALUATION else "high",
    )


def _table(reviews) -> ReviewScoreTable:
    outputs = {r.review_id: _output(r.review_id, 10.0, place=r.place_id) for r in reviews}
    return ReviewScoreTable(
        reviews=reviews,
        outputs=outputs,
        duplicate_groups=[],
        fingerprint="fp-test",
        embedding_model_name="test-model",
    )


def test_analyze_and_report_partitions_samples():
    reviews = [_review(f"r{i}") for i in range(4)]
    table = _table(reviews)
    labelset = ReviewLabelSet(labels=[
        _label("r0", SampleType.EVALUATION, templated=TemplatedLabel.ORGANIC),
        _label("r1", SampleType.EVALUATION, templated=TemplatedLabel.TEMPLATED),
        _label("r2", SampleType.CHALLENGE, templated=TemplatedLabel.ORGANIC),
        _label("r3", None),  # unattributed
    ])
    report = analyze_and_report(table, labelset, threshold=65.0, text_by_id={})
    assert report["evaluation"]["label_count"] == 2
    assert report["evaluation"]["representative"] is True
    assert report["challenge"]["label_count"] == 1
    assert report["challenge"]["representative"] is False
    assert "NOT representative" in report["challenge"]["sample_note"]
    assert report["unattributed"]["count"] == 1
    assert report["no_calibration_disclaimer"]
    assert report["unmatched_review_ids"] == []
    assert report["raw_metrics"]["evaluation"] == report["evaluation"]


def test_evaluation_templated_metrics_isolated_from_challenge():
    reviews = [_review(f"r{i}") for i in range(6)]
    table = _table(reviews)
    # Tune output scores: evaluation has a clean mix, challenge is score-enriched.
    table.outputs["r1"].templated_value = 10.0  # eval organic low
    table.outputs["r2"].templated_value = 80.0  # eval templated high
    table.outputs["r3"].templated_value = 90.0  # challenge templated
    labelset = ReviewLabelSet(labels=[
        _label("r0", SampleType.EVALUATION, templated=TemplatedLabel.ORGANIC),
        _label("r1", SampleType.EVALUATION, templated=TemplatedLabel.ORGANIC),
        _label("r2", SampleType.EVALUATION, templated=TemplatedLabel.TEMPLATED),
        _label("r3", SampleType.CHALLENGE, templated=TemplatedLabel.TEMPLATED),
    ])
    report = analyze_and_report(table, labelset, threshold=65.0, text_by_id={})
    eval_confusion = report["evaluation"]["templated"]["confusion"]
    chr_confusion = report["challenge"]["templated"]["confusion"]
    assert eval_confusion["n_reference_positive"] == 1  # r2 only
    assert eval_confusion["tp"] == 1
    assert chr_confusion["n_reference_positive"] == 1  # r3 only


def test_analyze_and_report_uses_selection_map_for_attribution():
    reviews = [_review("rA"), _review("rB")]
    table = _table(reviews)
    labelset = ReviewLabelSet(labels=[
        ReviewHumanLabel(review_id="rA", templated_label=TemplatedLabel.ORGANIC),
        ReviewHumanLabel(review_id="rB", templated_label=TemplatedLabel.ORGANIC),
    ])
    selection_map = {
        "rA": {"sample_type": "evaluation", "sampling_stratum": "random"},
        "rB": {"sample_type": "challenge", "sampling_stratum": "high"},
    }
    report = analyze_and_report(
        table, labelset, threshold=65.0, selection_map=selection_map, text_by_id={}
    )
    assert report["evaluation"]["label_count"] == 1
    assert report["challenge"]["label_count"] == 1


def test_report_disagreements_include_context():
    reviews = [_review("r1", text="A very particular review text about visiting")]
    table = _table(reviews)
    table.outputs["r1"].templated_value = 90.0
    table.outputs["r1"].templated_signals = ["phrase reuse"]
    labelset = ReviewLabelSet(labels=[
        _label("r1", SampleType.EVALUATION, templated=TemplatedLabel.ORGANIC),
    ])
    report = analyze_and_report(table, labelset, threshold=65.0,
                                text_by_id={"r1": "A very particular review text about visiting"})
    assert len(report["disagreements"]) == 1
    row = report["disagreements"][0]
    assert row["kind"] == "templated_false_positive"
    assert row["reviewscope_score"] == 90.0
    assert "text" in row


def test_unattributed_labels_excluded_from_both_partitions():
    reviews = [_review("r1")]
    table = _table(reviews)
    labelset = ReviewLabelSet(labels=[_label("r1", None, templated=TemplatedLabel.TEMPLATED)])
    report = analyze_and_report(table, labelset, threshold=65.0, text_by_id={})
    assert report["evaluation"]["label_count"] == 0
    assert report["challenge"]["label_count"] == 0
    assert report["unattributed"]["count"] == 1


def test_unmatched_review_ids_reported():
    reviews = [_review("r1")]
    table = _table(reviews)
    labelset = ReviewLabelSet(labels=[_label("zzz", SampleType.EVALUATION)])
    report = analyze_and_report(table, labelset, threshold=65.0, text_by_id={})
    assert report["unmatched_review_ids"] == ["zzz"]
    assert report["limitations"]


def test_render_markdown_sections(tmp_path):
    reviews = [_review(f"r{i}") for i in range(3)]
    table = _table(reviews)
    labelset = ReviewLabelSet(labels=[
        _label("r0", SampleType.EVALUATION, templated=TemplatedLabel.ORGANIC,
               specificity=SpecificityLabel.HIGH),
        _label("r1", SampleType.EVALUATION, templated=TemplatedLabel.TEMPLATED,
               specificity=SpecificityLabel.MEDIUM),
        _label("r2", SampleType.CHALLENGE, templated=TemplatedLabel.ORGANIC,
               specificity=SpecificityLabel.LOW),
    ])
    report = analyze_and_report(table, labelset, threshold=65.0, text_by_id={})
    md = render_markdown(report)
    for section in [
        "# ReviewScope Real Data Validation",
        "## Dataset",
        "## Label coverage",
        "## Representative Evaluation Metrics",
        "## Challenge / Error Analysis",
        "## Score Distributions",
        "## Limitations",
    ]:
        assert section in md, section
    assert "no_calibration_disclaimer" not in md or "MEASUREMENT of the existing production" in md

    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    write_report(report, md_path, json_path)
    assert md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["evaluation"]["label_count"] == 2
    assert payload["dataset"]["fingerprint"] == "fp-test"
