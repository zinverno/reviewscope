#!/usr/bin/env python3
"""Phase 15 — assemble the validation report (Markdown + JSON).

Pipeline stage 3, executed AFTER the annotation batch is finalized and the
labels are unblinded (see ``docs/REAL_DATA_VALIDATION.md`` for the blindness
protocol).

The report joins:

* ``score_table.json``      (``validation/scoring.write_scores_json`` output)
* human labels              (``labels.csv`` OR ``--annotation-store`` DuckDB)
* ``sample_selection.json`` (sample attribution for each label)

and always keeps two metric families apart:

* **Representative evaluation** — computed ONLY from the deterministic
  evaluation sample (the headline numbers);
* **Challenge / error analysis** — from the score/duplicate-enriched challenge
  sample, reported as diagnostic only and never as population performance.

Example::

    python scripts/validation_report.py \\
        --scores validation_data/demo/score_table.json \\
        --store validation_data/demo/dataset.duckdb \\
        --labels validation_data/demo/labels.csv \\
        --selection validation_data/demo/sample_selection.json

    # or, when the batch is still inside the annotation store:
    python scripts/validation_report.py \\
        --scores validation_data/demo/score_table.json \\
        --store validation_data/demo/dataset.duckdb \\
        --annotation-store validation_data/demo/annotations.duckdb \\
        --selection validation_data/demo/sample_selection.json \\
        --labels-out validation_data/demo/labels.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.storage import DuckDBStore  # noqa: E402
from reviewscope.validation.annotation import AnnotationStore  # noqa: E402
from reviewscope.validation.loader import (  # noqa: E402
    load_selection_json,
    read_labels,
)
from reviewscope.validation.report import analyze_and_report, write_report  # noqa: E402
from reviewscope.validation.scoring import load_scores_table  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Join production scores + human labels + sample attribution "
        "and write the Markdown + JSON validation report."
    )
    parser.add_argument("--scores", required=True, help="score_table.json")
    parser.add_argument(
        "--store",
        required=True,
        help="Persisted dataset DuckDB (reviews needed to reconstruct the "
        "score table and the disagreement texts).",
    )
    labels_group = parser.add_mutually_exclusive_group()
    labels_group.add_argument("--labels", help="Human-label CSV (post-unblinding).")
    labels_group.add_argument(
        "--annotation-store",
        help="Annotation DuckDB; its rows are exported to --labels-out first.",
    )
    parser.add_argument("--labels-out", help="Where to export --annotation-store rows.")
    parser.add_argument("--selection", help="sample_selection.json (attribution).")
    parser.add_argument("--threshold", type=float, default=65.0)
    parser.add_argument("--out-md", default=None, help="Markdown report path.")
    parser.add_argument("--out-json", default=None, help="JSON report path.")
    args = parser.parse_args()

    out_dir = Path(args.scores).parent
    out_md = Path(args.out_md) if args.out_md else out_dir / "validation_report.md"
    out_json = Path(args.out_json) if args.out_json else out_dir / "validation_report.json"

    with DuckDBStore(args.store, read_only=True) as store:
        reviews = store.fetch_reviews()
    print(f"Loaded {len(reviews)} reviews from {args.store}")

    table = load_scores_table(args.scores, reviews)
    print(f"Score table: {len(table.outputs)} reviews, "
          f"{len(table.duplicate_groups)} duplicate groups "
          f"(fingerprint {table.fingerprint[:32]}...)")

    if args.labels:
        labelset = read_labels(args.labels)
        print(f"Labels: {len(labelset.labels)} rows from {args.labels}")
        if labelset.warnings:
            print("  loader warnings:")
            for warning in labelset.warnings:
                print(f"    - {warning}")
    else:
        if not args.labels_out:
            raise SystemExit("--annotation-store requires --labels-out to export the labels")
        with AnnotationStore(args.annotation_store) as ann:
            ann.write_labels_csv(args.labels_out)
            labelset = read_labels(args.labels_out)
        print(f"Labels: {len(labelset.labels)} rows exported from "
              f"{args.annotation_store} -> {args.labels_out}")

    selection_map = load_selection_json(args.selection) if args.selection else {}
    text_by_id = {review.review_id: review.text_or_empty() for review in reviews}

    report = analyze_and_report(
        table,
        labelset,
        threshold=args.threshold,
        text_by_id=text_by_id,
        selection_map=selection_map,
    )
    write_report(report, out_md, out_json)
    print(f"\nWrote:\n  {out_md}\n  {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
