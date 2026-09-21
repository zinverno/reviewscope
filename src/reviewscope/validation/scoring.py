"""Run the PRODUCTION ReviewScope detectors over a full review dataset.

Phase 15 is measurement, not reimplementation. This module does **not** contain
any detector logic or formula duplication: it only orchestrates the exact
production classes and ``CONFIG`` values the rest of ReviewScope uses, grouped
per place exactly like ``AnalysisEngine.analyze`` does:

* ``TemplatedTextScorer().score(reviews, embeddings)`` (analysis/templated.py)
* ``DuplicateDetector().detect(reviews, embeddings)``  (analysis/duplicates.py)
* ``specificity_score(text)``                        (analysis/specificity.py)

so the numbers measured here are byte-for-byte the numbers production would
emit for the same reviews.

Cohort semantics are preserved: templated and duplicate signals are computed
per ``place_id`` cohort, mirroring the production engine. The full dataset is
always scored first; sampling/labels are joined afterwards — scores are never
computed on a subset.

The optional persisted DuckDB store is gated by a dataset fingerprint so it can
never silently mix or reuse state from an unrelated dataset.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from reviewscope.analysis.duplicates import DuplicateDetector, DuplicateGroup
from reviewscope.analysis.specificity import specificity_score
from reviewscope.analysis.templated import TemplatedTextScorer
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore

_METADATA_TABLE = "validation_metadata"
_FINGERPRINT_KEY = "dataset_fingerprint"
_MODEL_KEY = "embedding_model_name"


class StoreConflictError(RuntimeError):
    """The persisted store belongs to a different dataset.

    Raised instead of silently reusing stale state. The message tells the user
    how to obtain a fresh store.
    """


@dataclass
class ReviewOutput:
    """Per-review production outputs used by validation.

    Only the fields the validation metrics consume are exposed; everything is
    produced by the production detectors listed in the module docstring.
    """

    review_id: str
    place_id: str
    templated_value: float
    templated_confidence: str
    templated_signals: list[str] = field(default_factory=list)
    templated_counter_signals: list[str] = field(default_factory=list)
    templated_details: dict[str, float] = field(default_factory=dict)
    specificity_value: float = field(default=0.0)
    specificity_signals: list[str] = field(default_factory=list)
    specificity_counter_signals: list[str] = field(default_factory=list)
    predicted_duplicate_group_id: str | None = None
    duplicate_group_size: int = 0


@dataclass
class ReviewScoreTable:
    """All production outputs for a full dataset, keyed by ``review_id``."""

    reviews: list[NormalizedReview]
    outputs: dict[str, ReviewOutput] = field(default_factory=dict)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    fingerprint: str = ""
    embedding_model_name: str = ""


# ---------------------------------------------------------------------------
# Dataset fingerprint / store hygiene
# ---------------------------------------------------------------------------


def dataset_fingerprint(
    reviews: list[NormalizedReview], embedding_model_name: str = ""
) -> str:
    """A stable, order-independent fingerprint of the dataset content.

    The fingerprint is a commutative (order-independent) digest of:
    - review count and distinct-place count;
    - per-place counts;
    - a per-review XOR-combined digest of ``review_id`` + normalized text.

    It is deliberately cheap and XOR-combined so identical datasets hash
    identically regardless of row order. It is NOT a cryptographic guard
    against adversarial collisions; it exists to catch accidental reuse of a
    persisted store across unrelated datasets.
    """
    acc: int = 0
    for review in reviews:
        digest = hashlib.sha256()
        digest.update(review.review_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(review.text_or_empty().strip().lower().encode("utf-8"))
        acc ^= int(digest.hexdigest()[:16], 16)
    place_counts = sorted(Counter(review.place_id for review in reviews).items())
    acc_places: int = 0
    for place_id, count in place_counts:
        digest = hashlib.sha256(f"{place_id}:{count}".encode())
        acc_places ^= int(digest.hexdigest()[:16], 16)
    return (
        f"v1|{len(reviews)}|{len(place_counts)}|{acc:016x}|{acc_places:016x}"
        f"|{embedding_model_name}"
    )


def _store_metadata(store: DuckDBStore) -> dict[str, str]:
    store.connection().execute(
        f"CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (key VARCHAR PRIMARY KEY, value VARCHAR)"
    )
    rows = store.connection().execute(
        f"SELECT key, value FROM {_METADATA_TABLE}"
    ).fetchall()
    return {key: value for key, value in rows}


def _write_metadata(store: DuckDBStore, values: dict[str, str]) -> None:
    store.connection().execute(
        f"CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (key VARCHAR PRIMARY KEY, value VARCHAR)"
    )
    store.connection().executemany(
        f"INSERT OR REPLACE INTO {_METADATA_TABLE} (key, value) VALUES (?, ?)",
        list(values.items()),
    )


def _store_review_ids(store: DuckDBStore) -> set[str]:
    rows = store.connection().execute("SELECT review_id FROM reviews").fetchall()
    return {row[0] for row in rows}


def prepare_store(
    store_path: str | Path,
    reviews: list[NormalizedReview],
    *,
    embedding_model_name: str,
    force: bool = False,
) -> DuckDBStore:
    """Open (and if needed create) the validation store, gated by fingerprint.

    Rules:
    * New/empty store  -> created, ingested, stamped with the fingerprint.
    * Existing store with the *same* fingerprint -> reused (embedding cache is
      keyed by ``(review_id, text_hash, model_name)`` so unchanged texts are
      reused and changed texts are re-encoded — never mixed).
    * Existing store with a *different* fingerprint -> :class:`StoreConflictError`.
    * Existing store with no fingerprint (created by non-validation code) ->
      :class:`StoreConflictError` unless ``force`` is set (dangerous, replaces
      the store's review rows and re-stamps it).

    Additionally, the store's ``reviews`` rows must exactly match the current
    dataset's review ids; otherwise stale rows from another dataset could leak
    into cohort analysis and we refuse to proceed.
    """
    path = Path(store_path)
    model = embedding_model_name
    fingerprint = dataset_fingerprint(reviews, model)

    if not path.exists() or path.stat().st_size == 0:
        store = DuckDBStore(path)
        store.ingest(reviews)
        _write_metadata(
            store,
            {_FINGERPRINT_KEY: fingerprint, _MODEL_KEY: model},
        )
        return store

    store = DuckDBStore(path)
    metadata = _store_metadata(store)
    existing = metadata.get(_FINGERPRINT_KEY)
    existing_model = metadata.get(_MODEL_KEY)

    if existing is None:
        store.close()
        if force:
            store = DuckDBStore(path)
            store.ingest(reviews)
            _write_metadata(
                store,
                {_FINGERPRINT_KEY: fingerprint, _MODEL_KEY: model},
            )
            return store
        raise StoreConflictError(
            f"store {path} exists but has no validation fingerprint (it was "
            "created by another ReviewScope entrypoint). Refusing to mix "
            "datasets. Use a different --store path or --force-store to "
            "overwrite it."
        )

    if existing_model != model:
        store.close()
        raise StoreConflictError(
            f"store {path} was stamped for embedding model {existing_model!r} "
            f"but this run uses {model!r}. Use a fresh --store path."
        )

    if existing != fingerprint:
        store.close()
        raise StoreConflictError(
            f"store {path} belongs to a DIFFERENT dataset "
            f"(fingerprint {existing[:24]}... != {fingerprint[:24]}...). "
            "Refusing to mix datasets and corrupt cohort analysis. Use a "
            "fresh --store path or --force-store to overwrite."
        )

    current_ids = _store_review_ids(store)
    expected_ids = {review.review_id for review in reviews}
    if current_ids != expected_ids:
        store.close()
        raise StoreConflictError(
            f"store {path} reviews do not exactly match the current dataset "
            f"({len(current_ids)} vs {len(expected_ids)} ids). Refusing to "
            "reuse potentially stale rows. Use --force-store to rebuild."
        )
    return store


# ---------------------------------------------------------------------------
# Scoring orchestration
# ---------------------------------------------------------------------------


def compute_reviewscope_outputs(
    reviews: list[NormalizedReview],
    store_path: str | Path | None = None,
    *,
    use_embeddings: bool = True,
    force_store: bool = False,
) -> ReviewScoreTable:
    """Score a full dataset with the production detectors, per place cohort.

    ``store_path=None`` uses an in-memory store (no persistence, no embedding
    reuse across runs) — intended for tests and ephemeral fixtures. A real
    validation run should pass a path under ``validation_data/`` so the
    embedding cache survives between sampling and validation runs.
    """
    from reviewscope.embeddings.cache import EmbeddingCache as _Cache

    embedding_model_name = CONFIG.embedding.model_name
    store: DuckDBStore | None = None
    try:
        if store_path is None:
            store = DuckDBStore()
        else:
            store = prepare_store(
                store_path,
                reviews,
                embedding_model_name=embedding_model_name,
                force=force_store,
            )

        cache = _Cache(store)
        embeddings: np.ndarray | None = None
        if use_embeddings and reviews:
            embeddings = cache.embed_reviews(reviews)

        by_place: dict[str, list[int]] = {}
        for index, review in enumerate(reviews):
            by_place.setdefault(review.place_id, []).append(index)

        outputs: dict[str, ReviewOutput] = {}
        all_dup_groups: list[DuplicateGroup] = []
        templated_scorer = TemplatedTextScorer()
        duplicate_detector = DuplicateDetector()

        for place_id in sorted(by_place):
            indices = by_place[place_id]
            place_reviews = [reviews[i] for i in indices]
            place_embeddings = (
                embeddings[indices] if embeddings is not None and embeddings.shape[0] > 0 else None
            )

            templated_results = templated_scorer.score(place_reviews, place_embeddings)
            dup_groups = duplicate_detector.detect(place_reviews, embeddings=place_embeddings)

            group_of: dict[str, str] = {}
            group_size: dict[str, int] = {}
            for group in dup_groups:
                composite = f"{place_id}|g{group.group_id}"
                for rid in group.review_ids:
                    group_of[rid] = composite
                    group_size[rid] = len(group.review_ids)

            for review, result in zip(place_reviews, templated_results, strict=False):
                spec = specificity_score(review.text_or_empty())
                outputs[review.review_id] = ReviewOutput(
                    review_id=review.review_id,
                    place_id=place_id,
                    templated_value=result.value,
                    templated_confidence=result.confidence.value,
                    templated_signals=list(result.signals),
                    templated_counter_signals=list(result.counter_signals),
                    templated_details=dict(result.details),
                    specificity_value=spec.value,
                    specificity_signals=list(spec.signals),
                    specificity_counter_signals=list(spec.counter_signals),
                    predicted_duplicate_group_id=group_of.get(review.review_id),
                    duplicate_group_size=group_size.get(review.review_id, 0),
                )

            all_dup_groups.extend(dup_groups)

        return ReviewScoreTable(
            reviews=reviews,
            outputs=outputs,
            duplicate_groups=all_dup_groups,
            fingerprint=dataset_fingerprint(reviews, embedding_model_name),
            embedding_model_name=embedding_model_name,
        )
    finally:
        if store is not None:
            store.close()


def write_scores_json(table: ReviewScoreTable, path: str | Path) -> None:
    """Serialise per-review outputs for the post-unblinding comparison mode.

    The payload also carries the detected duplicate groups (needed to rebuild
    the predicted-group side of the duplicate metrics) and the dataset
    fingerprint, so a later ``validation_report`` run plus the persisted
    review store is enough to reproduce the very same ``ReviewScoreTable``
    without re-scoring.
    """
    payload = {
        "schema": "reviewscope_scores_1",
        "fingerprint": table.fingerprint,
        "embedding_model_name": table.embedding_model_name,
        "duplicate_groups": [
            {
                "group_id": group.group_id,
                "review_ids": sorted(group.review_ids),
                "exact_count": group.exact_count,
                "fuzzy_count": group.fuzzy_count,
                "near_count": group.near_count,
                "semantic_count": group.semantic_count,
                "avg_similarity": round(group.avg_similarity, 4),
            }
            for group in table.duplicate_groups
        ],
        "reviews": [
            {
                "review_id": output.review_id,
                "place_id": output.place_id,
                "templated_score": output.templated_value,
                "templated_confidence": output.templated_confidence,
                "templated_signals": output.templated_signals,
                "templated_counter_signals": output.templated_counter_signals,
                "specificity_score": output.specificity_value,
                "specificity_signals": output.specificity_signals,
                "specificity_counter_signals": output.specificity_counter_signals,
                "predicted_duplicate_group_id": output.predicted_duplicate_group_id,
                "duplicate_group_size": output.duplicate_group_size,
            }
            for output in table.outputs.values()
        ],
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_scores_json(path: str | Path) -> dict[str, dict]:
    """Load ``write_scores_json`` output into ``review_id -> row`` map."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {row["review_id"]: row for row in payload.get("reviews", [])}


def _group_from_payload(group: dict) -> DuplicateGroup:
    return DuplicateGroup(
        group_id=int(group["group_id"]),
        review_ids=list(group["review_ids"]),
        exact_count=int(group.get("exact_count", 0)),
        fuzzy_count=int(group.get("fuzzy_count", 0)),
        near_count=int(group.get("near_count", 0)),
        semantic_count=int(group.get("semantic_count", 0)),
        avg_similarity=float(group.get("avg_similarity", 0.0)),
    )


def load_scores_table(
    path: str | Path,
    reviews: list[NormalizedReview],
    *,
    review_text: dict[str, str] | None = None,
) -> ReviewScoreTable:
    """Rebuild a :class:`ReviewScoreTable` from a persisted scores payload.

    ``reviews`` must be the full dataset (typically re-read from the
    persisted validation store) so cohort analysis stays well-defined.
    ``review_text`` is optional and only used as a fallback for reports that
    run without the store.

    This is the offline reconstruction side of ``write_scores_json``; it never
    re-runs detectors and never recomputes scores.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    fingerprint = payload.get("fingerprint", "")
    model = payload.get("embedding_model_name", "")
    texts = review_text or {}

    outputs: dict[str, ReviewOutput] = {}
    for row in payload.get("reviews", []):
        review_id = row["review_id"]
        outputs[review_id] = ReviewOutput(
            review_id=review_id,
            place_id=row["place_id"],
            templated_value=float(row["templated_score"]),
            templated_confidence=row.get("templated_confidence", "LOW"),
            templated_signals=list(row.get("templated_signals", [])),
            templated_counter_signals=list(row.get("templated_counter_signals", [])),
            specificity_value=float(row["specificity_score"]),
            specificity_signals=list(row.get("specificity_signals", [])),
            specificity_counter_signals=list(row.get("specificity_counter_signals", [])),
            predicted_duplicate_group_id=row.get("predicted_duplicate_group_id"),
            duplicate_group_size=int(row.get("duplicate_group_size", 0)),
        )

    if reviews and not texts:
        texts = {review.review_id: review.text_or_empty() for review in reviews}

    missing = [rid for rid in outputs if rid not in {r.review_id for r in reviews}]
    if missing:
        raise ValueError(
            f"scores payload contains {len(missing)} review ids missing from the "
            f"supplied reviews (e.g. {missing[0]}). Pass the full dataset store."
        )

    return ReviewScoreTable(
        reviews=reviews,
        outputs=outputs,
        duplicate_groups=[_group_from_payload(g) for g in payload.get("duplicate_groups", [])],
        fingerprint=fingerprint,
        embedding_model_name=model,
    )
