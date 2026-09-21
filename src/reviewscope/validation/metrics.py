"""Metrics for judging ReviewScope outputs against human labels (Phase 15).

Everything here is a pure fold over ``(ReviewOutput, ReviewHumanLabel)`` pairs.
There is no detector logic and no threshold is derived from the data: the
decision threshold is passed in verbatim from the CLI and is used for reporting
only. The 0..100 sweep is descriptive sensitivity analysis, never calibration.

Metric definitions are explicit:

* **Templated.** Reference = human labels ``organic``/``templated`` (``uncertain``
  excluded and counted). Prediction = ``templated_value >= threshold``.
  Confusion matrix, precision = TP/(TP+FP), recall = TP/(TP+FN),
  F1, FPR = FP/(FP+TN), FNR = FN/(FN+TP).
* **Specificity.** Distributions by human label; Spearman rank correlation
  between human ordinal (low=0, medium=1, high=2) and the ReviewScope score;
  descriptive cross-tab against the production confidence bands
  (LOW < 40, 40 <= MEDIUM < 65, HIGH >= 65) plus near/far disagreement counts.
  No "band mapping" is invented.
* **Duplicates.** Pair-level precision/recall/F1 are the PRIMARY metric
  (GT pairs inside human groups, predicted pairs inside detected groups). The
  Jaccard >= 0.5 group match is a secondary, descriptive metric whose
  split/merge ambiguity is documented. The two are never averaged together.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import mean

from reviewscope.validation.models import (
    DuplicateLabel,
    ReviewHumanLabel,
    SpecificityLabel,
    TemplatedLabel,
)
from reviewscope.validation.scoring import ReviewOutput

_LABEL_COLUMNS_TEMPLATED = ("templated_label", "template_group_id")
_LABEL_COLUMNS_SPECIFICITY = ("specificity_label",)
_LABEL_COLUMNS_DUPLICATE = ("duplicate_group_id", "duplicate_label")


@dataclass
class Confusion:
    """Binary 2x2 confusion statistics at one decision threshold."""

    threshold: float
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    excluded: int = 0

    def as_dict(self) -> dict:
        return {
            "threshold": round(self.threshold, 2),
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "n_reference_positive": self.tp + self.fn,
            "n_reference_negative": self.fp + self.tn,
            "excluded_uncertain_and_missing": self.excluded,
            "precision": _safe_ratio(self.tp, self.tp + self.fp),
            "recall": _safe_ratio(self.tp, self.tp + self.fn),
            "f1": _safe_f1(self.tp, self.fp, self.fn),
            "fpr": _safe_ratio(self.fp, self.fp + self.tn),
            "fnr": _safe_ratio(self.fn, self.tp + self.fn),
        }


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _safe_f1(tp: int, fp: int, fn: int) -> float | None:
    denom = 2 * tp + fp + fn
    if denom <= 0:
        return None
    return round(2 * tp / denom, 4)


def binary_confusion(
    predicted: list[bool], reference: list[bool], threshold: float
) -> Confusion:
    """Build a :class:`Confusion` from aligned boolean sequences."""
    confusion = Confusion(threshold=threshold)
    for pred, ref in zip(predicted, reference, strict=False):
        if ref:
            if pred:
                confusion.tp += 1
            else:
                confusion.fn += 1
        else:
            if pred:
                confusion.fp += 1
            else:
                confusion.tn += 1
    return confusion


# ---------------------------------------------------------------------------
# Score distributions
# ---------------------------------------------------------------------------


def score_distribution(values: Iterable[float]) -> dict | None:
    """Summary stats: ``{n, min, p25, median, mean, p75, p95, max}``."""
    seq = sorted(float(v) for v in values)
    if not seq:
        return None
    n = len(seq)

    def pct(p: float) -> float:
        return seq[min(n - 1, int(n * p))]

    return {
        "n": n,
        "min": round(seq[0], 2),
        "p25": round(pct(0.25), 2),
        "median": round(seq[n // 2], 2),
        "mean": round(mean(seq), 2),
        "p75": round(pct(0.75), 2),
        "p95": round(pct(0.95), 2),
        "max": round(seq[-1], 2),
    }


# ---------------------------------------------------------------------------
# Templated metrics
# ---------------------------------------------------------------------------


def templated_reference(label: ReviewHumanLabel) -> bool | None:
    """Reference-truth (:class:`bool`) for a human templated label.

    Returns ``None`` when the human label is ``uncertain``/missing so the
    review is excluded from the binary confusion (and counted separately).
    """
    if label.templated_label == TemplatedLabel.TEMPLATED:
        return True
    if label.templated_label == TemplatedLabel.ORGANIC:
        return False
    return None


def templated_metrics(
    pairs: list[tuple[ReviewOutput, ReviewHumanLabel]],
    threshold: float = 65.0,
) -> dict:
    """Full templated metric bundle at ``threshold`` plus a descriptive sweep.

    ``excluded`` counts every labeled review excluded from the binary
    confusion (``uncertain`` or missing templated label).
    """
    used: list[tuple[ReviewOutput, bool, bool]] = []
    organic_scores: list[float] = []
    templated_scores: list[float] = []
    excluded = 0
    for outcome, label in pairs:
        reference = templated_reference(label)
        if reference is None:
            excluded += 1
            continue
        used.append((outcome, outcome.templated_value >= threshold, reference))
        if label.templated_label == TemplatedLabel.ORGANIC:
            organic_scores.append(outcome.templated_value)
        else:
            templated_scores.append(outcome.templated_value)

    confusion = binary_confusion(
        [pred for _, pred, _ in used],
        [ref for _, _, ref in used],
        threshold=threshold,
    )
    confusion.excluded = excluded

    sweep = [
        {
            "threshold": float(t),
            **_sweep_row(
                [outcome.templated_value >= t for outcome, _, _ in used],
                [ref for _, _, ref in used],
            ),
        }
        for t in range(0, 101, 5)
    ]

    return {
        "confusion": confusion.as_dict(),
        "sweep": sweep,
        "score_distribution_organic": score_distribution(organic_scores),
        "score_distribution_templated": score_distribution(templated_scores),
        "n_reference_organic": len(organic_scores),
        "n_reference_templated": len(templated_scores),
        "n_excluded_uncertain": excluded,
    }


def _sweep_row(predicted: list[bool], reference: list[bool]) -> dict:
    confusion = binary_confusion(predicted, reference, threshold=0.0)
    return {
        "precision": _safe_ratio(confusion.tp, confusion.tp + confusion.fp),
        "recall": _safe_ratio(confusion.tp, confusion.tp + confusion.fn),
        "f1": _safe_f1(confusion.tp, confusion.fp, confusion.fn),
        "support": len(reference),
    }


# ---------------------------------------------------------------------------
# Specificity metrics
# ---------------------------------------------------------------------------


def specificity_band(score: float) -> str:
    """Production confidence bands used in the descriptive cross-tab."""
    if score >= 65:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


_ORDINAL = {SpecificityLabel.LOW: 0, SpecificityLabel.MEDIUM: 1, SpecificityLabel.HIGH: 2}

_BAND_ORDER = ("LOW", "MEDIUM", "HIGH")
_HUMAN_ORDER = ("low", "medium", "high")


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def ranks(seq: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: seq[i])
        rank = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and seq[order[j + 1]] == seq[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rank[order[k]] = avg_rank
            i = j + 1
        return rank

    rx = ranks(xs)
    ry = ranks(ys)
    mean_x = mean(rx)
    mean_y = mean(ry)
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=False))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x == 0 or var_y == 0:
        return None
    return round(cov / (var_x * var_y) ** 0.5, 4)


def specificity_metrics(pairs: list[tuple[ReviewOutput, ReviewHumanLabel]]) -> dict:
    """Distributions by human label + ordinal agreement + descriptive bands."""
    by_label: dict[str, list[float]] = {}
    ordinal_x: list[float] = []
    ordinal_y: list[float] = []
    crosstab: dict[str, dict[str, int]] = {
        band: {human: 0 for human in _HUMAN_ORDER} for band in _BAND_ORDER
    }
    for outcome, label in pairs:
        if label.specificity_label is None:
            continue
        by_label.setdefault(label.specificity_label.value, []).append(outcome.specificity_value)
        if label.specificity_label != SpecificityLabel.UNCERTAIN:
            band = specificity_band(outcome.specificity_value)
            ordinal_x.append(float(_ORDINAL[label.specificity_label]))
            ordinal_y.append(outcome.specificity_value)
            crosstab[band][label.specificity_label.value] += 1

    distributions = {
        label: score_distribution(values) for label, values in sorted(by_label.items())
    }

    near_far = _specificity_near_far(crosstab)

    return {
        "distributions_by_human_label": distributions,
        "spearman_human_ordinal_vs_score": _spearman(ordinal_x, ordinal_y),
        "crosstab_human_vs_production_band": crosstab,
        "n_ordinal_pair": len(ordinal_x),
        "near_band_disagreements": near_far["near"],
        "far_band_disagreements": near_far["far"],
        "far_band_examples": near_far["far_examples"],
    }


def _specificity_near_far(crosstab: dict[str, dict[str, int]]) -> dict:
    near = 0
    far = 0
    far_examples: list[dict[str, object]] = []
    adjacency = {
        "LOW": {"medium"},
        "MEDIUM": {"low", "high"},
        "HIGH": {"medium"},
    }
    for band in _BAND_ORDER:
        for human in _HUMAN_ORDER:
            count = crosstab[band][human]
            if count <= 0:
                continue
            if human == band.lower():
                continue
            if human in adjacency[band]:
                near += count
            else:
                far += count
                far_examples.append(
                    {"human_label": human, "production_band": band, "count": count}
                )
    return {"near": near, "far": far, "far_examples": far_examples}


# ---------------------------------------------------------------------------
# Duplicate metrics
# ---------------------------------------------------------------------------


def default_group_id(label: ReviewHumanLabel) -> str:
    return label.duplicate_group_id or ""


def human_duplicate_groups(
    labels: list[ReviewHumanLabel],
) -> tuple[dict[str, list[str]], int, list[str]]:
    """Group human-duplicate labels by ``duplicate_group_id``.

    Returns ``(groups, n_excluded, warnings)``. ``n_excluded`` counts labels
    excluded from ground-truth pairs: ``uncertain`` duplicates and
    ``duplicate`` labels that carry no group id. Inconsistent rows (group id
    without a ``duplicate`` label) are excluded from the group and warned.
    """
    groups: dict[str, list[str]] = {}
    excluded = 0
    warnings: list[str] = []
    for label in labels:
        if label.duplicate_label == DuplicateLabel.UNCERTAIN:
            excluded += 1
            continue
        if label.duplicate_label == DuplicateLabel.DUPLICATE:
            group_id = label.duplicate_group_id
            if not group_id:
                excluded += 1
                warnings.append(
                    f"{label.review_id}: duplicate label without duplicate_group_id, "
                    "excluded from ground-truth pairs"
                )
                continue
            groups.setdefault(group_id, []).append(label.review_id)
            continue
        # unique / missing verdict
        if label.duplicate_group_id:
            excluded += 1
            warnings.append(
                f"{label.review_id}: duplicate_group_id set but duplicate_label is "
                "not 'duplicate'; review excluded from that group"
            )
    for group_id in groups:
        groups[group_id] = sorted(groups[group_id])
    return groups, excluded, warnings


def _all_pairs(members: list[str]) -> set[tuple[str, str]]:
    return {tuple(sorted(pair)) for pair in itertools.combinations(members, 2)}


def duplicate_metrics(
    labels: list[ReviewHumanLabel],
    predicted_groups: list[list[str]],
    *,
    labeled_ids: set[str],
) -> dict:
    """Pair-level (primary) and Jaccard-0.5 group-level (secondary) duplicate
    metrics against the human ground truth.

    Pair precision/recall are defined only on *labeled* reviews: predicted
    pairs are restricted to reviews that carry a duplicate verdict, so the
    metrics describe how well ReviewScope recovers the annotated duplicates,
    not unlabeled scan noise.

    The Jaccard>=0.5 group match is reported separately and flagged as
    secondary because split/merge behaviour makes group-to-group matching
    ambiguous.
    """
    human_groups, excluded_uncertain, warnings = human_duplicate_groups(labels)

    gt_pairs: set[tuple[str, str]] = set()
    for members in human_groups.values():
        gt_pairs |= _all_pairs(members)

    pred_pairs: set[tuple[str, str]] = set()
    for members in predicted_groups:
        labeled_members = sorted(set(members) & labeled_ids)
        pred_pairs |= _all_pairs(labeled_members)

    tp = gt_pairs & pred_pairs
    fp = pred_pairs - gt_pairs
    fn = gt_pairs - pred_pairs

    human_group_set = [set(members) for members in human_groups.values()]
    predicted_group_set = [set(members) for members in predicted_groups]

    def best_jaccard(members: set[str]) -> float:
        best = 0.0
        for pred in predicted_group_set:
            union = len(pred | members)
            if union:
                best = max(best, len(pred & members) / union)
        return best

    recovered_human = 0
    missed_human_groups: list[list[str]] = []
    for members in human_group_set:
        if best_jaccard(members) >= 0.5:
            recovered_human += 1
        else:
            missed_human_groups.append(sorted(members))

    matched_pred = 0
    for pred_members in predicted_group_set:
        if any(best_jaccard(pred_members) >= 0.5 for members in human_group_set):
            matched_pred += 1
    n_predicted = len(predicted_group_set)
    n_human = len(human_group_set)

    return {
        "definitions": {
            "primary": (
                "pair-level: precision = TP/(TP+FP), recall = TP/(TP+FN), where "
                "TP/FP/FN are unordered review pairs inside human duplicate "
                "groups (ground truth) vs pairs inside ReviewScope duplicate "
                "groups (predicted), restricted to labeled reviews."
            ),
            "secondary": (
                "group-level Jaccard>=0.5 match between a human group and a "
                "ReviewScope group. Descriptive only: split/merge ambiguity "
                "makes group matching inexact; it is never blended with "
                "pair-level numbers."
            ),
        },
        "pair_metrics": {
            "tp_pairs": len(tp),
            "fp_pairs": len(fp),
            "fn_pairs": len(fn),
            "known_duplicate_pairs_recovered": len(tp),
            "known_duplicate_pairs_total": len(tp) + len(fn),
            "missed_duplicate_pairs": len(fn),
            "false_positive_pairs": len(fp),
            "precision": _safe_ratio(len(tp), len(tp) + len(fp)),
            "recall": _safe_ratio(len(tp), len(tp) + len(fn)),
            "f1": _safe_f1(len(tp), len(fp), len(fn)),
        },
        "group_metrics_secondary": {
            "n_human_groups": n_human,
            "n_predicted_groups": n_predicted,
            "recovered_human_groups": recovered_human,
            "missed_human_groups": n_human - recovered_human,
            "missed_human_group_ids": [
                f"group_{i}" for i, _ in enumerate(missed_human_groups)
            ],
            "matched_predicted_groups": matched_pred,
            "unmatched_predicted_groups": n_predicted - matched_pred,
            "pct_human_groups_recovered_jaccard_0_5": _safe_ratio(recovered_human, n_human),
            "pct_predicted_groups_matched_jaccard_0_5": _safe_ratio(matched_pred, n_predicted),
        },
        "excluded_uncertain_or_inconsistent": excluded_uncertain,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Disagreements
# ---------------------------------------------------------------------------


@dataclass
class DisagreementRow:
    """One human-vs-ReviewScope disagreement with explainability context."""

    review_id: str
    kind: str
    human_label: str
    rs_result: str
    rs_score: float
    signals: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)
    text: str = ""
    notes: str | None = None
    sample_type: str | None = None
    sampling_stratum: str | None = None

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "kind": self.kind,
            "human_label": self.human_label,
            "reviewscope_result": self.rs_result,
            "reviewscope_score": round(self.rs_score, 2),
            "signals": list(self.signals),
            "counter_signals": list(self.counter_signals),
            "text": self.text,
            "notes": self.notes,
            "sample_type": self.sample_type,
            "sampling_stratum": self.sampling_stratum,
        }


def _truncate(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def templated_disagreements(
    outcome: ReviewOutput,
    label: ReviewHumanLabel,
    threshold: float,
    text: str,
) -> DisagreementRow | None:
    """One templated FP/FN row, or ``None`` when not a disagreement."""
    if label.templated_label == TemplatedLabel.ORGANIC and outcome.templated_value >= threshold:
        return DisagreementRow(
            review_id=label.review_id,
            kind="templated_false_positive",
            human_label=TemplatedLabel.ORGANIC.value,
            rs_result=TemplatedLabel.TEMPLATED.value,
            rs_score=outcome.templated_value,
            signals=outcome.templated_signals,
            counter_signals=outcome.templated_counter_signals,
            text=_truncate(text),
            notes=label.reviewer_notes,
            sample_type=label.sample_type.value if label.sample_type else None,
            sampling_stratum=label.sampling_stratum,
        )
    if label.templated_label == TemplatedLabel.TEMPLATED and outcome.templated_value < threshold:
        return DisagreementRow(
            review_id=label.review_id,
            kind="templated_false_negative",
            human_label=TemplatedLabel.TEMPLATED.value,
            rs_result=TemplatedLabel.ORGANIC.value,
            rs_score=outcome.templated_value,
            signals=outcome.templated_signals,
            counter_signals=outcome.templated_counter_signals,
            text=_truncate(text),
            notes=label.reviewer_notes,
            sample_type=label.sample_type.value if label.sample_type else None,
            sampling_stratum=label.sampling_stratum,
        )
    return None


def specificity_disagreement(
    outcome: ReviewOutput,
    label: ReviewHumanLabel,
    text: str,
) -> DisagreementRow | None:
    """Far-band specificity disagreement row, or ``None``."""
    if label.specificity_label == SpecificityLabel.LOW and specificity_band(outcome.specificity_value) == "HIGH":
        return DisagreementRow(
            review_id=label.review_id,
            kind="specificity_far_human_low",
            human_label=SpecificityLabel.LOW.value,
            rs_result="HIGH specificity",
            rs_score=outcome.specificity_value,
            signals=outcome.specificity_signals,
            counter_signals=outcome.specificity_counter_signals,
            text=_truncate(text),
            notes=label.reviewer_notes,
            sample_type=label.sample_type.value if label.sample_type else None,
            sampling_stratum=label.sampling_stratum,
        )
    if label.specificity_label == SpecificityLabel.HIGH and specificity_band(outcome.specificity_value) == "LOW":
        return DisagreementRow(
            review_id=label.review_id,
            kind="specificity_far_human_high",
            human_label=SpecificityLabel.HIGH.value,
            rs_result="LOW specificity",
            rs_score=outcome.specificity_value,
            signals=outcome.specificity_signals,
            counter_signals=outcome.specificity_counter_signals,
            text=_truncate(text),
            notes=label.reviewer_notes,
            sample_type=label.sample_type.value if label.sample_type else None,
            sampling_stratum=label.sampling_stratum,
        )
    return None


def duplicate_missed_rows(outcome: ReviewOutput, label: ReviewHumanLabel, text: str) -> DisagreementRow | None:
    """Row for a human-duplicate review ReviewScope failed to group."""
    if label.duplicate_label != DuplicateLabel.DUPLICATE:
        return None
    if outcome.predicted_duplicate_group_id is not None:
        return None
    return DisagreementRow(
        review_id=label.review_id,
        kind="duplicate_missed_group",
        human_label=DuplicateLabel.DUPLICATE.value,
        rs_result="unique",
        rs_score=0.0,
        text=_truncate(text),
        notes=label.reviewer_notes,
        sample_type=label.sample_type.value if label.sample_type else None,
        sampling_stratum=label.sampling_stratum,
    )


def duplicate_false_positive_rows(
    outcome: ReviewOutput,
    label: ReviewHumanLabel,
    text: str,
) -> DisagreementRow | None:
    """Row for a human-unique review ReviewScope placed in a duplicate group."""
    if label.duplicate_label != DuplicateLabel.UNIQUE:
        return None
    if outcome.predicted_duplicate_group_id is None or outcome.duplicate_group_size < 2:
        return None
    return DisagreementRow(
        review_id=label.review_id,
        kind="duplicate_false_positive",
        human_label=DuplicateLabel.UNIQUE.value,
        rs_result=f"duplicate group {outcome.predicted_duplicate_group_id}",
        rs_score=outcome.duplicate_group_size,
        text=_truncate(text),
        notes=label.reviewer_notes,
        sample_type=label.sample_type.value if label.sample_type else None,
        sampling_stratum=label.sampling_stratum,
    )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def coverage_metrics(
    table_outputs: dict[str, ReviewOutput],
    labels: list[ReviewHumanLabel],
) -> dict:
    """Label coverage: how many reviews are labeled, by dimension and sample."""
    unmatched = [label.review_id for label in labels if label.review_id not in table_outputs]
    matched = [label for label in labels if label.review_id in table_outputs]

    counts: dict[str, dict[str, int]] = {
        "templated_label": {},
        "template_group_id": {},
        "specificity_label": {},
        "duplicate_label": {},
        "sample_type": {},
        "sampling_stratum": {},
    }
    for label in matched:
        value = label.templated_label.value if label.templated_label else "missing"
        counts["templated_label"][value] = counts["templated_label"].get(value, 0) + 1
        if label.template_group_id:
            counts["template_group_id"]["set"] = counts["template_group_id"].get("set", 0) + 1
        value = label.specificity_label.value if label.specificity_label else "missing"
        counts["specificity_label"][value] = counts["specificity_label"].get(value, 0) + 1
        value = label.duplicate_label.value if label.duplicate_label else "missing"
        counts["duplicate_label"][value] = counts["duplicate_label"].get(value, 0) + 1
        value = label.sample_type.value if label.sample_type else "unattributed"
        counts["sample_type"][value] = counts["sample_type"].get(value, 0) + 1
        if label.sampling_stratum:
            counts["sampling_stratum"][label.sampling_stratum] = (
                counts["sampling_stratum"].get(label.sampling_stratum, 0) + 1
            )

    template_group_id_count = counts["template_group_id"].get("set", 0)
    return {
        "total_label_records": len(labels),
        "matched_to_dataset": len(matched),
        "unmatched_review_ids": sorted(unmatched),
        "by_label": counts,
        "n_template_groups_annotated": template_group_id_count,
        "sample_type_counts": counts["sample_type"],
    }
