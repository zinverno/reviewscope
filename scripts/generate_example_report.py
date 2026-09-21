#!/usr/bin/env python3
"""Phase 15 — regenerate the public synthetic validation example report.

This is a documentation aid, NOT a validation run: it feeds the committed
synthetic fixture (``tests/data/fixtures/example_reviews.csv`` and
``example_labels.csv``) through the exact same ``analyze_and_report`` /
``write_report`` path as the real pipeline and writes the result to
``docs/examples/``.

Everything is deterministic and offline:

* ``use_embeddings=False`` keeps the run model-free (no downloads, no GPU);
* ``generated_at`` is pinned so the artifacts diff cleanly on regeneration;
* only the committed synthetic fixture is read — never ``validation_data/``.

Run::

    python scripts/generate_example_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from reviewscope.validation.loader import load_reviews, read_labels  # noqa: E402
from reviewscope.validation.report import analyze_and_report, write_report  # noqa: E402
from reviewscope.validation.scoring import compute_reviewscope_outputs  # noqa: E402

FIXTURE_DIR = _ROOT / "tests" / "data" / "fixtures"
REVIEWS_CSV = FIXTURE_DIR / "example_reviews.csv"
LABELS_CSV = FIXTURE_DIR / "example_labels.csv"
OUT_DIR = _ROOT / "docs" / "examples"
OUT_MD = OUT_DIR / "real_data_validation_example.md"
OUT_JSON = OUT_DIR / "real_data_validation_example.json"

GENERATED_AT = "2026-03-26T00:00:00+00:00"
THRESHOLD = 65.0


def main() -> int:
    reviews = load_reviews(REVIEWS_CSV).reviews
    table = compute_reviewscope_outputs(reviews, None, use_embeddings=False)
    labelset = read_labels(LABELS_CSV)
    text_by_id = {review.review_id: review.text_or_empty() for review in reviews}

    report = analyze_and_report(
        table,
        labelset,
        threshold=THRESHOLD,
        text_by_id=text_by_id,
        generated_at=GENERATED_AT,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_report(report, OUT_MD, OUT_JSON)

    evaluation = report["evaluation"]
    print(f"reviews={len(reviews)} labels={len(labelset.labels)}")
    print(
        "evaluation templated confusion: "
        f"{evaluation['templated']['confusion'] if evaluation['templated'] else 'n/a'}"
    )
    print(f"disagreements={len(report['disagreements'])}")
    print(f"wrote:\n  {OUT_MD}\n  {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
