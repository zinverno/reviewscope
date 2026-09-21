"""Assemble and render the Phase 15 validation report.

The report keeps two families of metrics strictly apart:

* **Representative Evaluation Metrics** — computed only from the deterministic
  simple-random evaluation sample. These are the headline numbers.
* **Challenge / Error Analysis** — computed from the score/duplicate-enriched
  challenge sample. Purely diagnostic; the report states loudly that it is NOT
  representative and must not be quoted as population performance.

``analyze_and_report`` is the single join/partition point between the full
dataset scoring table, the human labels and their sample attribution; the
scripts stay thin wrappers around it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from reviewscope.validation import metrics as M
from reviewscope.validation.models import (
    ANNOTATION_SCHEMA_VERSION,
    ReviewHumanLabel,
    ReviewLabelSet,
    SampleType,
)
from reviewscope.validation.scoring import ReviewScoreTable

_DEFAULT_DISCLAIMER = (
    "This report is a MEASUREMENT of the existing production ReviewScope "
    "detectors against human labels. No thresholds, weights or detector "
    "algorithms were modified to produce it. The templated decision threshold "
    "shown below is a reporting choice; the 0..100 sweep is descriptive "
    "sensitivity analysis and is NOT an optimised threshold."
)


def _effective_sample_type(
    label: ReviewHumanLabel, selection_map: dict[str, dict]
) -> SampleType | None:
    if label.sample_type is not None:
        return label.sample_type
    entry = selection_map.get(label.review_id) or {}
    raw = entry.get("sample_type")
    if raw is None:
        return None
    try:
        return SampleType(raw)
    except ValueError:
        return None


def analyze_and_report(
    table: ReviewScoreTable,
    labelset: ReviewLabelSet,
    *,
    threshold: float = 65.0,
    text_by_id: dict[str, str] | None = None,
    selection_map: dict[str, dict] | None = None,
    generated_at: str | None = None,
) -> dict:
    """Produce the full report dictionary (also the ``.json`` payload)."""
    text_by_id = text_by_id or {}
    selection_map = selection_map or {}

    matched: list[tuple[object, ReviewHumanLabel, SampleType | None]] = []
    unmatched: list[str] = []
    for label in labelset.labels:
        output = table.outputs.get(label.review_id)
        if output is None:
            unmatched.append(label.review_id)
            continue
        matched.append((output, label, _effective_sample_type(label, selection_map)))

    evaluation = [p for p in matched if p[2] == SampleType.EVALUATION]
    challenge = [p for p in matched if p[2] == SampleType.CHALLENGE]
    unattributed = [p for p in matched if p[2] is None]

    coverage = M.coverage_metrics(table.outputs, list(labelset.labels))

    report: dict = {
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "annotation_schema_version": ANNOTATION_SCHEMA_VERSION,
        "reporting_threshold": float(threshold),
        "no_calibration_disclaimer": _DEFAULT_DISCLAIMER,
        "dataset": _dataset_block(table),
        "label_coverage": coverage,
        "evaluation": _partition_block(
            evaluation, table, threshold, text_by_id, representative=True
        ),
        "challenge": _partition_block(
            challenge, table, threshold, text_by_id, representative=False
        ),
        "unattributed": {
            "note": (
                "Labels without a sample_type and not present in the supplied "
                "selection file. Excluded from both representative evaluation "
                "and challenge diagnostics."
            ),
            "count": len(unattributed),
        },
        "disagreements": _disagreements(matched, table, threshold, text_by_id),
        "score_distributions": _all_labeled_distributions(matched),
        "uncertain_labels": _uncertain_counts(coverage),
        "limitations": _limitations(matched, challenge),
        "label_warnings": list(labelset.warnings),
        "unmatched_review_ids": sorted(unmatched),
        "raw_metrics": {},
    }
    report["raw_metrics"] = {
        "dataset": report["dataset"],
        "label_coverage": report["label_coverage"],
        "evaluation": report["evaluation"],
        "challenge": report["challenge"],
    }
    return report


def _dataset_block(table: ReviewScoreTable) -> dict:
    places = sorted({review.place_id for review in table.reviews})
    return {
        "reviews_loaded": len(table.reviews),
        "distinct_places": len(places),
        "places": places,
        "fingerprint": table.fingerprint,
        "embedding_model_name": table.embedding_model_name,
        "metric_definitions": {
            "templated": (
                "Confusion matrix, precision = TP/(TP+FP), recall = TP/(TP+FN), "
                "F1, FPR = FP/(FP+TN), FNR = FN/(FN+TP) at the reporting "
                "threshold (default 65, matching the production templated "
                "signal cut). 'uncertain' human labels are excluded from "
                "binary metrics and counted separately."
            ),
            "specificity": (
                "Score distributions by human label; Spearman rank correlation "
                "between the human ordinal label (low=0/medium=1/high=2) and "
                "the ReviewScope specificity score; descriptive cross-tab with "
                "the production confidence bands (LOW<40, 40<=MEDIUM<65, "
                "HIGH>=65). No band mapping is invented."
            ),
            "duplicate": (
                "PRIMARY: pair-level precision/recall/F1 (GT pairs = pairs of "
                "reviews sharing a human duplicate_group_id; predicted pairs = "
                "pairs inside ReviewScope duplicate groups, restricted to "
                "labeled reviews). SECONDARY (descriptive): Jaccard>=0.5 "
                "group matching whose split/merge ambiguity is documented. The "
                "two are never blended."
            ),
        },
    }


def _partition_block(
    pairs: list[tuple[object, ReviewHumanLabel, SampleType | None]],
    table: ReviewScoreTable,
    threshold: float,
    text_by_id: dict[str, str],
    *,
    representative: bool,
) -> dict:
    metric_pairs = [(output, label) for output, label, _ in pairs]
    out: dict = {
        "label_count": len(pairs),
        "representative": representative,
        "templated": M.templated_metrics(metric_pairs, threshold=threshold) if pairs else None,
        "specificity": M.specificity_metrics(metric_pairs) if pairs else None,
        "duplicate": None,
    }
    if pairs:
        labeled_ids = {label.review_id for _, label, _ in pairs}
        out["duplicate"] = M.duplicate_metrics(
            [label for _, label, _ in pairs],
            [list(group.review_ids) for group in table.duplicate_groups],
            labeled_ids=labeled_ids,
        )
        out["templated_disagreement_count"] = sum(
            1
            for o, label, _ in pairs
            if M.templated_disagreements(o, label, threshold, text_by_id.get(label.review_id, ""))
            is not None
        )
    out["sample_note"] = (
        "Representative: deterministic simple random sample; headline "
        "population metrics are reported from this partition only."
        if representative
        else (
            "Diagnostic only: score/duplicate-enriched challenge sample. These "
            "numbers are NOT representative of population performance and MUST "
            "NOT be quoted as headline precision/recall/F1/FPR/FNR."
        )
    )
    return out


def _disagreements(
    matched: list[tuple[object, ReviewHumanLabel, SampleType | None]],
    table: ReviewScoreTable,
    threshold: float,
    text_by_id: dict[str, str],
) -> list[dict]:
    rows: list[M.DisagreementRow] = []
    for output, label, _ in matched:
        text = text_by_id.get(label.review_id, "")
        row = M.templated_disagreements(output, label, threshold, text)
        if row is not None:
            rows.append(row)
        row = M.specificity_disagreement(output, label, text)
        if row is not None:
            rows.append(row)
        row = M.duplicate_missed_rows(output, label, text)
        if row is not None:
            rows.append(row)
        row = M.duplicate_false_positive_rows(output, label, text)
        if row is not None:
            rows.append(row)

    sample_rank = {SampleType.EVALUATION.value: 0, SampleType.CHALLENGE.value: 1}
    rows.sort(
        key=lambda r: (
            r.kind,
            sample_rank.get(r.sample_type, 2),
            -r.rs_score,
            r.review_id,
        )
    )
    return [row.to_dict() for row in rows]


def _all_labeled_distributions(
    matched: list[tuple[object, ReviewHumanLabel, SampleType | None]],
) -> dict:
    organic: list[float] = []
    templated: list[float] = []
    specificity_by_label: dict[str, list[float]] = {}
    for output, label, _ in matched:
        if label.templated_label == "organic":
            organic.append(output.templated_value)
        elif label.templated_label == "templated":
            templated.append(output.templated_value)
        if label.specificity_label is not None:
            specificity_by_label.setdefault(label.specificity_label.value, []).append(
                output.specificity_value
            )
    return {
        "note": (
            "Aggregated over all labeled reviews. If the challenge sample is "
            "present this mix is intentionally score-biased and must not be "
            "read as a population distribution."
        ),
        "templated_score_human_organic": M.score_distribution(organic),
        "templated_score_human_templated": M.score_distribution(templated),
        "specificity_score_by_human_label": {
            label: M.score_distribution(values)
            for label, values in sorted(specificity_by_label.items())
        },
    }


def _uncertain_counts(coverage: dict) -> dict:
    by_label = coverage.get("by_label", {})
    return {
        "templated_uncertain": by_label.get("templated_label", {}).get("uncertain", 0),
        "specificity_uncertain": by_label.get("specificity_label", {}).get("uncertain", 0),
        "duplicate_uncertain": by_label.get("duplicate_label", {}).get("uncertain", 0),
        "note": (
            "Uncertain labels are always reported with their own count and are "
            "never forced into a binary bucket for headline metrics."
        ),
    }


def _limitations(
    matched: list[tuple[object, ReviewHumanLabel, SampleType | None]],
    challenge: list[tuple[object, ReviewHumanLabel, SampleType | None]],
) -> list[str]:
    limitations = [
        "This is a measurement baseline, produced before any calibration. If a "
        "detector performs poorly on real data, that is the finding to "
        "report, not a reason to retune thresholds during Phase 15.",
        "Templated and duplicate scores are cohort-relative: they depend on the "
        "reviews of the same place in the FULL dataset. Scores are always "
        "computed on the full imported dataset and never recomputed on a "
        "sample; sampling only selects which reviews get labeled.",
        "Threshold fitting on this dataset would leak into future evaluations: "
        "validation data must stay out of any calibration, training or "
        "test-tuning loop (see docs/REAL_DATA_VALIDATION.md for the "
        "train/calibration/test leakage risk).",
        "Specificity ordinal agreement is descriptive (Spearman) only; no "
        "scientifically justified band mapping exists, so none is fabricated.",
    ]
    if challenge:
        limitations.append(
            "A challenge sample is present: challenge numbers are "
            "diagnostic only and deliberately score-stratified; they overstate "
            "detector performance relative to the population and must not be "
            "reported as headline metrics."
        )
    if not matched:
        limitations.append("No labeled reviews matched the dataset.")
    return limitations


def render_markdown(report: dict) -> str:
    s: list[str] = []
    s.append("# ReviewScope Real Data Validation")
    s.append("")
    s.append(f"- Generated: `{report.get('generated_at')}`")
    s.append(
        f"- Annotation schema version: `{report.get('annotation_schema_version')}`"
    )
    s.append(
        f"- Templated reporting threshold: `{report.get('reporting_threshold'):g}` "
        "(reporting choice; sweep is descriptive only)"
    )
    s.append("")
    s.append(f"> {report.get('no_calibration_disclaimer')}")
    s.append("")

    s.append("## Dataset")
    s.append("")
    dataset = report["dataset"]
    s.append(
        _kv_table(
            [
                ("Reviews loaded", dataset["reviews_loaded"]),
                ("Distinct places", dataset["distinct_places"]),
                ("Embedding model", dataset["embedding_model_name"]),
                ("Dataset fingerprint", dataset["fingerprint"]),
            ]
        )
    )
    s.append("Metric definitions:")
    s.append("")
    for name, definition in dataset["metric_definitions"].items():
        s.append(f"- **{name}** — {definition}")
    s.append("")

    s.append("## Label coverage")
    s.append("")
    s.extend(_render_coverage(report["label_coverage"]))
    s.append("")

    s.append("## Representative Evaluation Metrics")
    s.append("")
    s.extend(_render_partition(report["evaluation"]))
    s.append("")

    s.append("## Challenge / Error Analysis (diagnostic only, NOT representative)")
    s.append("")
    s.extend(_render_partition(report["challenge"]))
    s.append("")

    s.append("## Unattributed labels")
    s.append("")
    unattributed = report["unattributed"]
    s.append(f"- Count: **{unattributed['count']}**")
    s.append(f"- {unattributed['note']}")
    s.append("")

    s.append("## Disagreements")
    s.append("")
    disagreements = report["disagreements"]
    if not disagreements:
        s.append("None.")
    else:
        s.append(
            "Each row: review_id, human label, ReviewScope result, ReviewScope "
            "score, signals, counter-signals, text (truncated), notes."
        )
        s.append("")
        for row in disagreements:
            s.append(f"### {row['kind']} — `{row['review_id']}`")
            s.append("")
            s.append(
                _kv_table(
                    [
                        ("Human label", row["human_label"]),
                        ("ReviewScope result", row["reviewscope_result"]),
                        ("ReviewScope score", row["reviewscope_score"]),
                        ("Sample", row["sample_type"] or "unattributed"),
                        ("Stratum", row["sampling_stratum"] or "—"),
                    ]
                )
            )
            if row["signals"]:
                s.append(f"- **Signals:** {', '.join(row['signals'])}")
            if row["counter_signals"]:
                s.append(f"- **Counter-signals:** {', '.join(row['counter_signals'])}")
            s.append(f"- **Text:** {row['text'] or '—'}")
            if row.get("notes"):
                s.append(f"- **Notes:** {row['notes']}")
            s.append("")
    s.append("")

    s.append("## Score Distributions")
    s.append("")
    s.extend(_render_score_distributions(report["score_distributions"]))
    s.append("")

    s.append("## Uncertain Labels")
    s.append("")
    for key, value in report["uncertain_labels"].items():
        if key == "note":
            s.append(f"- {value}")
            continue
        s.append(f"- {key}: **{value}**")
    s.append("")

    s.append("## Limitations")
    s.append("")
    for limitation in report["limitations"]:
        s.append(f"- {limitation}")
    s.append("")

    if report.get("label_warnings"):
        s.append("## Label warnings")
        s.append("")
        for warning in report["label_warnings"]:
            s.append(f"- {warning}")
        s.append("")

    s.append("## Raw Metrics")
    s.append("")
    s.append(
        "Full machine-readable metrics are written to the companion "
        "`validation_results.json` file. The same payload is summarized here."
    )
    s.append("")
    s.append("```json")
    s.append(json.dumps(report["raw_metrics"], ensure_ascii=False, indent=2, default=str))
    s.append("```")
    return "\n".join(s + [""])


def _kv_table(rows: list[tuple[str, object]]) -> str:
    lines = ["| Field | Value |", "|---|---|"]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _render_coverage(coverage: dict) -> list[str]:
    s: list[str] = []
    s.append(
        _kv_table(
            [
                ("Label records", coverage["total_label_records"]),
                ("Matched to dataset", coverage["matched_to_dataset"]),
                ("Unmatched", len(coverage["unmatched_review_ids"])),
                ("Template groups annotated", coverage["n_template_groups_annotated"]),
            ]
        )
    )
    s.append("")
    s.append("By label:")
    s.append("")
    for dimension, counts in coverage["by_label"].items():
        rendered = ", ".join(f"{label}={count}" for label, count in sorted(counts.items()))
        s.append(f"- **{dimension}:** {rendered}")
    return s


def _render_partition(block: dict) -> list[str]:
    s: list[str] = []
    if block["label_count"] == 0:
        s.append("*No labeled reviews in this partition.*")
        return s
    s.append(f"Labeled reviews in this partition: **{block['label_count']}**")
    s.append("")
    s.append(f"> {block['sample_note']}")
    s.append("")

    if block["templated"]:
        s.append("### Templated Detection")
        s.append("")
        s.extend(_render_templated(block["templated"]))
    if block["specificity"]:
        s.append("### Specificity")
        s.append("")
        s.extend(_render_specificity(block["specificity"]))
    if block["duplicate"]:
        s.append("### Duplicate Detection")
        s.append("")
        s.extend(_render_duplicates(block["duplicate"]))
    return s


def _render_templated(tm: dict) -> list[str]:
    s: list[str] = []
    confusion = tm["confusion"]
    s.append(
        _kv_table(
            [
                ("TP", confusion["tp"]),
                ("FP", confusion["fp"]),
                ("TN", confusion["tn"]),
                ("FN", confusion["fn"]),
                ("Precision", _fmt(confusion["precision"])),
                ("Recall", _fmt(confusion["recall"])),
                ("F1", _fmt(confusion["f1"])),
                ("False-positive rate", _fmt(confusion["fpr"])),
                ("False-negative rate", _fmt(confusion["fnr"])),
                ("Reference organic", tm["n_reference_organic"]),
                ("Reference templated", tm["n_reference_templated"]),
                ("Excluded (uncertain/missing)", tm["n_excluded_uncertain"]),
            ]
        )
    )
    s.append("")
    s.append("Score distributions:")
    s.append("")
    s.append("- **Human organic** " + _dist_render(tm["score_distribution_organic"]))
    s.append("- **Human templated** " + _dist_render(tm["score_distribution_templated"]))
    s.append("")
    s.append("Threshold sweep (descriptive sensitivity analysis, not calibration):")
    s.append("")
    sw = ["| Threshold | Precision | Recall | F1 |"]
    sw.append("|---|---|---|---|")
    for row in tm["sweep"]:
        sw.append(
            f"| {row['threshold']:g} | {_fmt(row['precision'])} | "
            f"{_fmt(row['recall'])} | {_fmt(row['f1'])} |"
        )
    s.extend(sw)
    s.append("")
    return s


def _render_specificity(sm: dict) -> list[str]:
    s: list[str] = []
    spearman = _fmt(sm["spearman_human_ordinal_vs_score"], suffix="")
    s.append(f"- **Spearman** (human ordinal low/medium/high vs ReviewScope score): "
             f"{spearman} (n={sm['n_ordinal_pair']})")
    s.append(
        f"- Near-band disagreements: **{sm['near_band_disagreements']}**, "
        f"far-band disagreements: **{sm['far_band_disagreements']}**"
    )
    for example in sm["far_band_examples"]:
        s.append(
            f"  - far-band example: human `{example['human_label']}` vs "
            f"production `{example['production_band']}` band (n={example['count']})"
        )
    s.append("")
    s.append("Distributions by human label:")
    s.append("")
    for label, dist in sm["distributions_by_human_label"].items():
        s.append(f"- **{label}** " + _dist_render(dist))
    s.append("")
    s.append("Cross-tab (human label × production confidence band):")
    s.append("")
    crosstab = sm["crosstab_human_vs_production_band"]
    band_names = list(crosstab.keys())
    s.append("| Human label | " + " | ".join(band_names) + " |")
    s.append("|" + "---|" * (len(band_names) + 1))
    for human in ("low", "medium", "high"):
        cells = [str(crosstab[band][human]) for band in band_names]
        s.append(f"| {human} | " + " | ".join(cells) + " |")
    s.append("")
    return s


def _render_duplicates(dm: dict) -> list[str]:
    s: list[str] = []
    s.append("**Metric definitions** (also in `raw_metrics`):")
    s.append("")
    s.append(f"- Primary: {dm['definitions']['primary']}")
    s.append(f"- Secondary: {dm['definitions']['secondary']}")
    s.append("")
    pair = dm["pair_metrics"]
    s.append(
        _kv_table(
            [
                ("Known duplicate pairs recovered (TP)", pair["known_duplicate_pairs_recovered"]),
                ("Known duplicate pairs total", pair["known_duplicate_pairs_total"]),
                ("Missed duplicate pairs (FN)", pair["missed_duplicate_pairs"]),
                ("False-positive duplicate pairs (FP)", pair["false_positive_pairs"]),
                ("Pair precision", _fmt(pair["precision"])),
                ("Pair recall", _fmt(pair["recall"])),
                ("Pair F1", _fmt(pair["f1"])),
            ]
        )
    )
    s.append("")
    group = dm["group_metrics_secondary"]
    s.append("Group-level (Jaccard>=0.5, descriptive only):")
    s.append("")
    s.append(
        _kv_table(
            [
                ("Human groups", group["n_human_groups"]),
                ("Predicted groups", group["n_predicted_groups"]),
                ("Recovered human groups", group["recovered_human_groups"]),
                ("Missed human groups", group["missed_human_groups"]),
                ("Matched predicted groups", group["matched_predicted_groups"]),
                ("Unmatched predicted groups", group["unmatched_predicted_groups"]),
            ]
        )
    )
    if dm.get("warnings"):
        s.append("")
        s.append("Group warnings:")
        s.append("")
        for warning in dm["warnings"]:
            s.append(f"- {warning}")
    s.append("")
    return s


def _render_score_distributions(sd: dict) -> list[str]:
    s: list[str] = []
    s.append(f"> {sd['note']}")
    s.append("")
    s.append("- **Templated score, human-organic reviews** "
             + _dist_render(sd["templated_score_human_organic"]))
    s.append("- **Templated score, human-templated reviews** "
             + _dist_render(sd["templated_score_human_templated"]))
    for label, dist in sd["specificity_score_by_human_label"].items():
        s.append(f"- **Specificity score, human `{label}`** " + _dist_render(dist))
    return s


def _fmt(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}{suffix}"


def _dist_render(dist: dict | None) -> str:
    if dist is None:
        return "(no data)"
    return (
        f"n={dist['n']}, min={dist['min']}, p25={dist['p25']}, "
        f"median={dist['median']}, mean={dist['mean']}, p75={dist['p75']}, "
        f"p95={dist['p95']}, max={dist['max']}"
    )


def write_report(report: dict, md_path: str | Path, json_path: str | Path | None = None) -> None:
    """Write the Markdown report and the machine-readable JSON mirror."""
    Path(md_path).write_text(render_markdown(report), encoding="utf-8")
    if json_path is not None:
        Path(json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
