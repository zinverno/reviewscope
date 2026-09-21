"""Phase 15 — human-vs-ReviewScope metric fold regression tests."""

from __future__ import annotations

from reviewscope.validation.metrics import (
    binary_confusion,
    coverage_metrics,
    duplicate_false_positive_rows,
    duplicate_metrics,
    duplicate_missed_rows,
    human_duplicate_groups,
    score_distribution,
    specificity_disagreement,
    specificity_metrics,
    templated_disagreements,
    templated_metrics,
)
from reviewscope.validation.models import (
    DuplicateLabel,
    ReviewHumanLabel,
    SampleType,
    SpecificityLabel,
    TemplatedLabel,
)
from reviewscope.validation.scoring import ReviewOutput


def _output(review_id, templated=50.0, specificity=50.0, dup_group=None,
            dup_size=0, place="p1") -> ReviewOutput:
    return ReviewOutput(
        review_id=review_id,
        place_id=place,
        templated_value=templated,
        templated_confidence="MEDIUM",
        templated_signals=["phrase reuse"],
        templated_counter_signals=["unique detail"],
        specificity_value=specificity,
        predicted_duplicate_group_id=dup_group,
        duplicate_group_size=dup_size,
    )


def _label(review_id, templated=None, specificity=None, duplicate=None,
           dup_group=None, templated_group=None, sample_type=SampleType.EVALUATION,
           notes=None) -> ReviewHumanLabel:
    return ReviewHumanLabel(
        review_id=review_id,
        templated_label=templated,
        template_group_id=templated_group,
        specificity_label=specificity,
        duplicate_group_id=dup_group,
        duplicate_label=duplicate,
        reviewer_notes=notes,
        sample_type=sample_type,
        sampling_stratum="random",
    )


def test_binary_confusion():
    confusion = binary_confusion([True, False, True, False], [True, True, False, False], threshold=65.0)
    assert (confusion.tp, confusion.fp, confusion.tn, confusion.fn) == (1, 1, 1, 1)
    d = confusion.as_dict()
    assert d["precision"] == 0.5
    assert d["recall"] == 0.5
    assert d["f1"] == 0.5
    assert d["n_reference_positive"] == 2


def test_templated_metrics_at_threshold():
    pairs = [
        (_output("a", templated=80.0), _label("a", templated=TemplatedLabel.TEMPLATED)),
        (_output("b", templated=70.0), _label("b", templated=TemplatedLabel.ORGANIC)),
        (_output("c", templated=60.0), _label("c", templated=TemplatedLabel.TEMPLATED)),
        (_output("d", templated=10.0), _label("d", templated=TemplatedLabel.ORGANIC)),
        (_output("e", templated=99.0), _label("e", templated=TemplatedLabel.UNCERTAIN)),
    ]
    metrics = templated_metrics(pairs, threshold=65.0)
    confusion = metrics["confusion"]
    assert (confusion["tp"], confusion["fp"], confusion["tn"], confusion["fn"]) == (1, 1, 1, 1)
    assert metrics["n_excluded_uncertain"] == 1
    assert confusion["excluded_uncertain_and_missing"] == 1
    assert len(metrics["sweep"]) == 21
    assert metrics["score_distribution_organic"]["n"] == 2
    assert metrics["score_distribution_templated"]["n"] == 2


def test_templated_uncertain_excluded_not_forced():
    pairs = [
        (_output("a", templated=80.0), _label("a", templated=TemplatedLabel.UNCERTAIN)),
        (_output("b", templated=10.0), _label("b", templated=TemplatedLabel.ORGANIC)),
    ]
    metrics = templated_metrics(pairs, threshold=65.0)
    assert metrics["n_excluded_uncertain"] == 1
    assert metrics["confusion"]["tp"] + metrics["confusion"]["fp"] + \
        metrics["confusion"]["tn"] + metrics["confusion"]["fn"] == 1


def test_score_distribution():
    dist = score_distribution([5.0, 50.0, 95.0, 20.0, 80.0])
    assert dist["min"] == 5.0 and dist["max"] == 95.0
    assert dist["median"] == 50.0
    assert score_distribution([]) is None


def test_specificity_metrics_ordinal_and_crosstab():
    pairs = [
        (_output("a", specificity=10.0), _label("a", specificity=SpecificityLabel.LOW)),
        (_output("b", specificity=45.0), _label("b", specificity=SpecificityLabel.MEDIUM)),
        (_output("c", specificity=90.0), _label("c", specificity=SpecificityLabel.HIGH)),
        (_output("d", specificity=70.0), _label("d", specificity=SpecificityLabel.HIGH)),
        (_output("e", specificity=30.0), _label("e", specificity=SpecificityLabel.LOW)),
    ]
    metrics = specificity_metrics(pairs)
    assert metrics["spearman_human_ordinal_vs_score"] is not None
    assert metrics["spearman_human_ordinal_vs_score"] > 0.8
    assert metrics["n_ordinal_pair"] == 5
    crosstab = metrics["crosstab_human_vs_production_band"]
    assert crosstab["LOW"]["low"] == 2
    assert crosstab["HIGH"]["high"] == 2
    assert metrics["far_band_disagreements"] == 0


def test_specificity_metrics_far_disagreement_counted():
    pairs = [
        (_output("a", specificity=90.0), _label("a", specificity=SpecificityLabel.LOW)),
        (_output("b", specificity=10.0), _label("b", specificity=SpecificityLabel.HIGH)),
    ]
    metrics = specificity_metrics(pairs)
    assert metrics["far_band_disagreements"] == 2
    assert len(metrics["far_band_examples"]) == 2


def test_human_duplicate_groups_normalize_and_warn():
    labels = [
        _label("a", duplicate=DuplicateLabel.DUPLICATE, dup_group="g1"),
        _label("b", duplicate=DuplicateLabel.DUPLICATE, dup_group="g1"),
        _label("c", duplicate=DuplicateLabel.DUPLICATE),            # no group
        _label("d", duplicate=DuplicateLabel.UNCERTAIN),
        _label("e", dup_group="g2"),                               # group but not duplicate
    ]
    groups, excluded, warnings = human_duplicate_groups(labels)
    assert set(groups["g1"]) == {"a", "b"}
    assert excluded == 3
    assert len(warnings) == 2


def test_duplicate_metrics_pair_level_primary():
    labels = [
        _label("a", duplicate=DuplicateLabel.DUPLICATE, dup_group="g1"),
        _label("b", duplicate=DuplicateLabel.DUPLICATE, dup_group="g1"),
        _label("c", duplicate=DuplicateLabel.UNIQUE),
        _label("d", duplicate=DuplicateLabel.DUPLICATE, dup_group="g2"),
        _label("e", duplicate=DuplicateLabel.DUPLICATE, dup_group="g2"),
    ]
    predicted = [["a", "b", "c"]]  # one big predicted group
    labeled_ids = {label.review_id for label in labels}
    metrics = duplicate_metrics(labels, predicted, labeled_ids=labeled_ids)
    pair = metrics["pair_metrics"]
    # gt pairs: ab, de; predicted pairs inside labeled: ab, ac, bc
    assert pair["known_duplicate_pairs_total"] == 2
    assert pair["known_duplicate_pairs_recovered"] == 1
    assert pair["false_positive_pairs"] == 2
    assert pair["precision"] == round(1 / 3, 4)
    assert pair["recall"] == 0.5
    assert pair["f1"] == round(2 * 1 / (2 * 1 + 2 + 1), 4)
    group = metrics["group_metrics_secondary"]
    assert group["n_human_groups"] == 2
    assert group["recovered_human_groups"] == 1
    assert group["missed_human_groups"] == 1
    assert group["pct_human_groups_recovered_jaccard_0_5"] == 0.5


def test_duplicate_metrics_ignores_unlabeled_predicted_noise():
    labels = [_label("a", duplicate=DuplicateLabel.DUPLICATE, dup_group="g1")]
    predicted = [["a", "zzz-quiet"]]  # unlabeled member must not count
    metrics = duplicate_metrics(labels, predicted, labeled_ids={"a"})
    assert metrics["pair_metrics"]["false_positive_pairs"] == 0


def test_templated_disagreements_rows():
    outcome = _output("b", templated=70.0)
    label = _label("b", templated=TemplatedLabel.ORGANIC)
    row = templated_disagreements(outcome, label, 65.0, "text of review b")
    assert row is not None and row.kind == "templated_false_positive"
    assert row.rs_result == "templated"
    assert row.signals == ["phrase reuse"]
    assert "text of review b" in row.text
    # no disagreement for agreement case
    assert templated_disagreements(_output("c", templated=10.0),
                                   _label("c", templated=TemplatedLabel.ORGANIC), 65.0, "t") is None


def test_specificity_disagreement_rows():
    outcome = _output("a", specificity=90.0)
    label = _label("a", specificity=SpecificityLabel.LOW)
    row = specificity_disagreement(outcome, label, "short generic")
    assert row is not None and row.kind == "specificity_far_human_low"
    assert specificity_disagreement(_output("a", specificity=50.0), label, "x") is None


def test_duplicate_disagreement_rows():
    missed = duplicate_missed_rows(_output("a", dup_group=None),
                                   _label("a", duplicate=DuplicateLabel.DUPLICATE, dup_group="g9"), "t")
    assert missed.kind == "duplicate_missed_group"
    fp = duplicate_false_positive_rows(
        _output("b", dup_group="p1|g2", dup_size=5),
        _label("b", duplicate=DuplicateLabel.UNIQUE), "t")
    assert fp.kind == "duplicate_false_positive"
    assert fp.rs_score == 5
    assert not duplicate_false_positive_rows(_output("b", dup_group=None), _label("b", duplicate=DuplicateLabel.UNIQUE), "t")


def test_coverage_metrics():
    outputs = {rid: _output(rid) for rid in ("a", "b", "c")}
    labels = [
        _label("a", templated=TemplatedLabel.ORGANIC, specificity=SpecificityLabel.HIGH),
        _label("zzz"),  # unmatched to dataset
    ]
    metrics = coverage_metrics(outputs, labels)
    assert metrics["total_label_records"] == 2
    assert metrics["matched_to_dataset"] == 1
    assert metrics["unmatched_review_ids"] == ["zzz"]
    assert metrics["by_label"]["templated_label"]["organic"] == 1
    assert metrics["by_label"]["sample_type"]["evaluation"] == 1
    assert metrics["by_label"]["specificity_label"]["high"] == 1
