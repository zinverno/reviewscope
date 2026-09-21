"""Phase 15 — dataset fingerprinting, store-hygiene and scoring orchestration."""

from __future__ import annotations

import pytest

from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore
from reviewscope.validation.scoring import (
    StoreConflictError,
    compute_reviewscope_outputs,
    dataset_fingerprint,
    load_scores_json,
    load_scores_table,
    prepare_store,
    write_scores_json,
)


def _review(review_id: str, text: str = "Вкусный раф и десерт", place: str = "p1") -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place,
        reviewer_id=f"user-{review_id}",
        rating=5,
        text=text,
        published_at="2026-09-01",
    )


def _duplicate_family(n: int, prefix: str = "d") -> list[NormalizedReview]:
    text = "Всё было идеально, великолепный сервис, качество на высоте, рекомендую всем"
    return [_review(f"{prefix}{i:02d}", text=text) for i in range(n)]


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_order_independent():
    reviews = [_review("r1", "aaa"), _review("r2", "bbb"), _review("r3", "ccc")]
    a = dataset_fingerprint(reviews)
    b = dataset_fingerprint(list(reversed(reviews)))
    assert a == b


def test_fingerprint_changes_with_content():
    a = dataset_fingerprint([_review("r1", "aaa"), _review("r2", "bbb")])
    b = dataset_fingerprint([_review("r1", "aaa"), _review("r2", "CHANGED")])
    assert a != b


def test_fingerprint_embeds_model_and_place_counts():
    reviews = [_review("r1", place="p1"), _review("r2", place="p1")]
    a = dataset_fingerprint(reviews, "model-a")
    b = dataset_fingerprint(reviews, "model-b")
    c = dataset_fingerprint([_review("r1", place="p1"), _review("r2", place="p2")], "model-a")
    assert a != b and a != c


def test_fingerprint_ignores_row_reordering_across_places():
    revs = [_review("r1", place="p1"), _review("r2", place="p2")]
    assert dataset_fingerprint(revs) == dataset_fingerprint(list(reversed(revs)))


# ---------------------------------------------------------------------------
# store hygiene
# ---------------------------------------------------------------------------


def test_prepare_store_stamps_new_store(tmp_path):
    store_path = tmp_path / "validation.duckdb"
    reviews = [_review("r1"), _review("r2")]
    store = prepare_store(store_path, reviews, embedding_model_name="test-model")
    try:
        assert store.review_count() == 2
        rows = store.connection().execute(
            "SELECT key, value FROM validation_metadata"
        ).fetchall()
        metadata = dict(rows)
        assert metadata["dataset_fingerprint"] == dataset_fingerprint(reviews, "test-model")
        assert metadata["embedding_model_name"] == "test-model"
    finally:
        store.close()


def test_prepare_store_reuses_same_dataset(tmp_path):
    store_path = tmp_path / "validation.duckdb"
    reviews = [_review("r1"), _review("r2")]
    prepare_store(store_path, reviews, embedding_model_name="test-model").close()
    store = prepare_store(store_path, reviews, embedding_model_name="test-model")
    try:
        assert store.review_count() == 2
    finally:
        store.close()


def test_prepare_store_conflicts_on_different_dataset(tmp_path):
    store_path = tmp_path / "validation.duckdb"
    prepare_store(store_path, [_review("r1")], embedding_model_name="test-model").close()
    with pytest.raises(StoreConflictError):
        prepare_store(store_path, [_review("r2")], embedding_model_name="test-model")


def test_prepare_store_conflicts_on_model_change(tmp_path):
    store_path = tmp_path / "validation.duckdb"
    prepare_store(store_path, [_review("r1")], embedding_model_name="model-a").close()
    with pytest.raises(StoreConflictError):
        prepare_store(store_path, [_review("r1")], embedding_model_name="model-b")


def test_prepare_store_conflicts_without_stamp_unless_force(tmp_path):
    store_path = tmp_path / "legacy.duckdb"
    store = DuckDBStore(store_path)
    store.ingest([_review("r1")])
    store.close()
    with pytest.raises(StoreConflictError):
        prepare_store(store_path, [_review("r1")], embedding_model_name="test-model")
    store = prepare_store(store_path, [_review("r1")], embedding_model_name="test-model",
                          force=True)
    try:
        metadata = dict(store.connection().execute(
            "SELECT key, value FROM validation_metadata").fetchall())
        assert "dataset_fingerprint" in metadata
    finally:
        store.close()


def test_prepare_store_rejects_stale_rows(tmp_path):
    store_path = tmp_path / "validation.duckdb"
    prepare_store(store_path, [_review("r1"), _review("r2")], embedding_model_name="test-model").close()
    # Same fingerprint but a review was manually deleted -> row set mismatch.
    store = DuckDBStore(store_path)
    store.connection().execute("DELETE FROM reviews WHERE review_id = 'r2'")
    store.close()
    with pytest.raises(StoreConflictError):
        prepare_store(store_path, [_review("r1"), _review("r2")], embedding_model_name="test-model")


# ---------------------------------------------------------------------------
# production scoring orchestration (in-memory, text-bigram signals)
# ---------------------------------------------------------------------------


def test_compute_outputs_uses_production_detectors():
    reviews = _duplicate_family(6) + [_review("o1", "Уникальный содержательный отзыв о кофе")]
    table = compute_reviewscope_outputs(reviews, use_embeddings=False)
    assert len(table.outputs) == 7
    assert table.fingerprint == dataset_fingerprint(reviews, table.embedding_model_name)
    # Identical family should form one detected duplicate group of size 6.
    assert len(table.duplicate_groups) == 1
    group = table.duplicate_groups[0]
    assert len(group.review_ids) == 6
    d00 = table.outputs["d00"]
    assert d00.predicted_duplicate_group_id is not None
    assert d00.duplicate_group_size == 6
    # The unique review must not be predicted as a duplicate.
    assert table.outputs["o1"].predicted_duplicate_group_id is None
    # Identical-template family drives the templated score well above a unique review.
    assert table.outputs["d00"].templated_value > table.outputs["o1"].templated_value


def test_compute_outputs_grouped_per_place():
    text = "Полностью одинаковый текст про место"
    reviews = [
        _review("a1", text=text, place="pA"),
        _review("a2", text=text, place="pA"),
        _review("b1", text=text, place="pB"),
        _review("b2", text=text, place="pB"),
    ]
    table = compute_reviewscope_outputs(reviews, use_embeddings=False)
    assert len(table.duplicate_groups) == 2
    group_ids = {out.predicted_duplicate_group_id for out in table.outputs.values()}
    assert len(group_ids - {None}) == 2


def test_scores_json_roundtrip_reconstructs_table(tmp_path):
    reviews = _duplicate_family(4) + [_review("u1", "Своя уникальная история о визите")]
    table = compute_reviewscope_outputs(reviews, use_embeddings=False)
    path = tmp_path / "score_table.json"
    write_scores_json(table, path)

    rows = load_scores_json(path)
    assert len(rows) == 5
    assert rows["d00"]["templated_score"] == table.outputs["d00"].templated_value

    rebuilt = load_scores_table(path, reviews)
    assert rebuilt.fingerprint == table.fingerprint
    assert rebuilt.embedding_model_name == table.embedding_model_name
    assert len(rebuilt.duplicate_groups) == 1
    assert rebuilt.duplicate_groups[0].review_ids == sorted(table.duplicate_groups[0].review_ids)
    assert rebuilt.outputs["d00"].templated_value == table.outputs["d00"].templated_value
    assert rebuilt.outputs["d00"].predicted_duplicate_group_id == \
        table.outputs["d00"].predicted_duplicate_group_id


def test_load_scores_table_rejects_missing_review_ids(tmp_path):
    table = compute_reviewscope_outputs([_review("r1")], use_embeddings=False)
    path = tmp_path / "score_table.json"
    write_scores_json(table, path)
    with pytest.raises(ValueError):
        load_scores_table(path, [_review("r2")])
