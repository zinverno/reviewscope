#!/usr/bin/env python3
"""Phase 15 — finalize (lock) an annotation batch.

The blind labeling app writes verdicts into an ``AnnotationStore``. This CLI
freezes that batch and binds it to the exact dataset that was scored, so a
report can never be built against a mutated dataset or a half-edited set of
labels. After finalization ``save_label`` refuses to write unless the caller
explicitly overrides, and each override archives the superseded verdict.

Example::

    python scripts/validation_finalize.py \\
        --annotation-store validation_data/demo/annotations.duckdb \\
        --scores validation_data/demo/score_table.json \\
        --annotator-id alice

    # deliberately re-finalize (audited; only with --force)
    python scripts/validation_finalize.py ... --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.validation.annotation import (  # noqa: E402
    AnnotationStore,
    AnnotationStoreError,
)


def _fingerprint_from_scores(path: str | Path) -> str | None:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return raw.get("fingerprint") if isinstance(raw, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Finalize (lock) an annotation batch and bind it to the "
        "dataset fingerprint of a scored run."
    )
    parser.add_argument("--annotation-store", required=True, help="annotations.duckdb")
    parser.add_argument(
        "--scores",
        help="score_table.json; its fingerprint binds the batch to the dataset.",
    )
    parser.add_argument(
        "--dataset-fingerprint",
        help="Explicit dataset fingerprint (alternative to --scores).",
    )
    parser.add_argument("--annotator-id", default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-finalize an already finalized batch (recorded as an override).",
    )
    args = parser.parse_args()

    fingerprint = args.dataset_fingerprint
    if fingerprint is None and args.scores:
        fingerprint = _fingerprint_from_scores(args.scores)
    if not fingerprint:
        raise SystemExit(
            "provide --scores or --dataset-fingerprint so the batch is bound to a dataset"
        )

    try:
        with AnnotationStore(args.annotation_store) as ann:
            before = ann.batch_status()
            metadata = ann.finalize(
                annotator_id=args.annotator_id,
                dataset_fingerprint=fingerprint,
                override=args.force,
            )
    except AnnotationStoreError as exc:
        raise SystemExit(f"finalization failed: {exc}") from exc

    print(f"Batch {args.annotation_store}")
    print(f"  status      : {before} -> {metadata.get('status')}")
    print(f"  finalized_at: {metadata.get('finalized_at')}")
    print(f"  annotator   : {metadata.get('annotator_id') or '—'}")
    print(f"  labels      : {metadata.get('label_count')}")
    print(f"  fingerprint : {metadata.get('dataset_fingerprint')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
