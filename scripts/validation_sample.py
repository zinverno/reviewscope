#!/usr/bin/env python3
"""Phase 15 — score a real dataset and build the blind labeling artifacts.

Pipeline stage 1 (before any human annotation):

1. Load a full review dataset through the **production** ingestion adapters.
2. Run the **production** detectors over the whole dataset
   (``validation.scoring.compute_reviewscope_outputs``), persisting the
   embedding cache next to the dataset so later runs reuse it.
3. Deterministically sample:
   - an **evaluation** simple random sample (headline/representative metrics
     only — see ``docs/REAL_DATA_VALIDATION.md``); and
   - a disjoint **challenge** sample stratified by templated score bands plus
     suspected duplicate-group members (diagnostic/error analysis only).
4. Write three artifacts in ``--out-dir``:
   - ``score_table.json``   — per-review production outputs + duplicate groups
                            (the post-unblinding comparison material);
   - ``sample_selection.json`` — review_id -> sample attribution,
                            deliberately score-free (annotator blindness);
   - ``label_template.csv`` — empty human-label rows for every selected
                            review, provenance columns pre-filled.

Example::

    python scripts/validation_sample.py data/demo_reviews.csv \
        --out-dir validation_data/demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.validation.loader import load_reviews, write_label_template  # noqa: E402
from reviewscope.validation.sampling import build_selection  # noqa: E402
from reviewscope.validation.scoring import (  # noqa: E402
    compute_reviewscope_outputs,
    write_scores_json,
)

DEFAULT_SEED = 20260901


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a full dataset with production detectors and build "
        "the deterministic evaluation + challenge label selection."
    )
    parser.add_argument("dataset", help="Path to reviews CSV/JSON (production adapters).")
    parser.add_argument("--out-dir", default="validation_data", help="Output directory.")
    parser.add_argument(
        "--store",
        default=None,
        help="DuckDB path for review rows + embedding cache. Default: <out-dir>/dataset.duckdb.",
    )
    parser.add_argument("--evaluation-n", type=int, default=120)
    parser.add_argument("--challenge-high", type=int, default=24)
    parser.add_argument("--challenge-medium", type=int, default=24)
    parser.add_argument("--challenge-low", type=int, default=24)
    parser.add_argument("--challenge-duplicate", type=int, default=24)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--annotator-id", default=None)
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip the embedding model (text-bigram fallback signals only). "
        "Intended for tests; a real validation run should keep embeddings on.",
    )
    parser.add_argument(
        "--force-store",
        action="store_true",
        help="Rebuild/re-stamp an existing store instead of raising StoreConflictError.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    store_path = Path(args.store) if args.store else out_dir / "dataset.duckdb"

    result = load_reviews(args.dataset)
    reviews = result.reviews
    report = result.report
    print(f"Import diagnostics for {args.dataset}:")
    print(f"  imported: {report.total_rows}")
    print(f"  valid: {report.valid}")
    print(f"  skipped: {report.skipped}")
    print(f"  warnings: {report.warning_count}")
    for warning in report.warnings:
        print(f"[warning] {warning}")

    print(f"Loaded {len(reviews)} reviews from {args.dataset}")
    print(
        f"Scoring with production detectors (embeddings={'on' if not args.no_embeddings else 'off'}) ..."
    )
    table = compute_reviewscope_outputs(
        reviews,
        store_path=store_path,
        use_embeddings=not args.no_embeddings,
        force_store=args.force_store,
    )
    print(f"  fingerprint: {table.fingerprint}")
    print(f"  duplicate groups: {len(table.duplicate_groups)}")

    selection = build_selection(
        reviews,
        table,
        evaluation_n=args.evaluation_n,
        challenge_high=args.challenge_high,
        challenge_medium=args.challenge_medium,
        challenge_low=args.challenge_low,
        challenge_duplicate=args.challenge_duplicate,
        seed=args.seed,
    )

    scores_path = out_dir / "score_table.json"
    selection_path = out_dir / "sample_selection.json"
    template_path = out_dir / "label_template.csv"
    write_scores_json(table, scores_path)
    selection.save(selection_path)
    write_label_template(
        [entry.review_id for entry in selection.entries],
        template_path,
        selection={rid: entry.model_dump() for rid, entry in selection.by_id().items()},
        annotator_id=args.annotator_id,
    )

    print("\nSelection summary:")
    print(f"  evaluation: {selection.evaluation_count}")
    for stratum, count in sorted(selection.challenge_counts.items()):
        print(f"  challenge_{stratum}: {count}")
    print(f"  total to label: {len(selection.entries)}")
    print("\nArtifacts:")
    print(f"  {scores_path}")
    print(f"  {selection_path}")
    print(f"  {template_path}")
    print(f"  (review/embedding store: {store_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
