"""Phase 15 — deterministic evaluation/challenge sampling regression tests."""

from __future__ import annotations

import json

from reviewscope.analysis.duplicates import DuplicateGroup
from reviewscope.models.review import NormalizedReview
from reviewscope.validation.models import SampleType
from reviewscope.validation.sampling import (
    Selection,
    build_selection,
    challenge_strata,
    sample_challenge_ids,
    sample_evaluation_ids,
)
from reviewscope.validation.scoring import ReviewOutput, ReviewScoreTable


def _review(review_id: str, place_id: str = "p1", score: float = 10.0) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"user-{review_id}",
        rating=5,
        text=f"Some review text {review_id}",
    )


def _table(reviews: list[NormalizedReview], scores=None, dup_groups=None) -> ReviewScoreTable:
    scores = scores or {}
    outputs = {
        review.review_id: ReviewOutput(
            review_id=review.review_id,
            place_id=review.place_id,
            templated_value=scores.get(review.review_id, 10.0),
            templated_confidence="LOW",
            specificity_value=50.0,
        )
        for review in reviews
    }
    return ReviewScoreTable(
        reviews=reviews,
        outputs=outputs,
        duplicate_groups=dup_groups or [],
        fingerprint=f"fp-{len(reviews)}",
        embedding_model_name="test-model",
    )


def _reviews(n: int) -> list[NormalizedReview]:
    return [_review(f"r{i:03d}") for i in range(n)]


def test_sample_evaluation_ids_sorted_sized():
    ids = ["r03", "r01", "r02"]
    assert sample_evaluation_ids(ids, 2, __import__("random").Random(1)) == ["r01", "r03"]
    assert len(sample_evaluation_ids(ids, 99, __import__("random").Random(1))) == 3
    assert sample_evaluation_ids(ids, 0, __import__("random").Random(1)) == []


def test_challenge_strata_bounds():
    reviews = _reviews(10)
    scores = {
        "r000": 90.0,  # high
        "r001": 50.0,  # medium
        "r002": 5.0,   # low
        "r003": 65.0,  # exactly high (>=65)
        "r004": 40.0,  # exactly medium
    }
    strata = challenge_strata(_table(reviews, scores=scores))
    assert "r000" in strata["high"] and "r003" in strata["high"]
    assert "r001" in strata["medium"] and "r004" in strata["medium"]
    assert "r002" in strata["low"]
    assert set(strata["high"]) & set(strata["medium"]) & set(strata["low"]) == set()


def test_challenge_strata_duplicate_members():
    reviews = [_review(f"r{i:02d}") for i in range(6)]
    table = _table(reviews, dup_groups=[
        DuplicateGroup(group_id=1, review_ids=["r00", "r01"]),
    ])
    strata = challenge_strata(table)
    assert set(strata["duplicate"]) == {"r00", "r01"}


def test_sample_challenge_ids_disjoint_and_exclusive():
    reviews = _reviews(40)
    scores = {f"r{i:03d}": 80.0 if i % 3 == 0 else 10.0 for i in range(40)}
    table = _table(reviews, scores=scores)
    pairs = sample_challenge_ids(
        table, n_high=10, n_medium=5, n_low=5, n_duplicate=0, rng=__import__("random").Random(7),
        exclude={"r000"},
    )
    strata_of = {rid: stratum for rid, stratum in pairs}
    assert "r000" not in strata_of
    # each review in exactly one stratum
    assert len(set(strata_of)) == len(pairs)
    # high stratum should be score-driver
    assert all(table.outputs[rid].templated_value >= 65 for rid, s in pairs if s == "high")


def test_build_selection_is_deterministic():
    reviews = _reviews(40)
    table = _table(reviews)
    a = build_selection(reviews, table, evaluation_n=10, challenge_high=5,
                        challenge_medium=5, challenge_low=5, challenge_duplicate=0, seed=42)
    b = build_selection(reviews, table, evaluation_n=10, challenge_high=5,
                        challenge_medium=5, challenge_low=5, challenge_duplicate=0, seed=42)
    assert a.model_dump() == b.model_dump()


def test_build_selection_seed_changes_selection():
    reviews = _reviews(60)
    table = _table(reviews)
    a = build_selection(reviews, table, evaluation_n=15, challenge_high=5,
                        challenge_medium=5, challenge_low=5, challenge_duplicate=0, seed=1)
    b = build_selection(reviews, table, evaluation_n=15, challenge_high=5,
                        challenge_medium=5, challenge_low=5, challenge_duplicate=0, seed=2)
    assert [e.review_id for e in a.entries] != [e.review_id for e in b.entries]


def test_build_selection_evaluation_disjoint_from_challenge():
    reviews = _reviews(80)
    table = _table(reviews)
    selection = build_selection(reviews, table, evaluation_n=15, challenge_high=10,
                                challenge_medium=10, challenge_low=10, challenge_duplicate=0, seed=3)
    evaluation_ids = {e.review_id for e in selection.entries if e.sample_type == SampleType.EVALUATION}
    challenge_ids = {e.review_id for e in selection.entries if e.sample_type == SampleType.CHALLENGE}
    assert evaluation_ids & challenge_ids == set()
    assert len(evaluation_ids) == 15
    assert selection.evaluation_count == 15
    for entry in selection.entries:
        if entry.sample_type == SampleType.EVALUATION:
            assert entry.sampling_stratum == "random"
        else:
            assert entry.sampling_stratum in ("high", "medium", "low", "duplicate")


def test_build_selection_counts_match_requested():
    reviews = _reviews(200)
    # 40 with high scores, 40 medium, 40 low -> pools large enough
    scores = {
        f"r{i:03d}": (80.0 if i < 40 else 50.0 if i < 80 else 10.0)
        for i in range(200)
    }
    table = _table(reviews, scores=scores)
    selection = build_selection(reviews, table, evaluation_n=20, challenge_high=8,
                                challenge_medium=8, challenge_low=8, challenge_duplicate=0, seed=5)
    assert selection.challenge_counts["high"] == 8
    assert selection.challenge_counts["medium"] == 8
    assert selection.challenge_counts["low"] == 8


def test_selection_entries_never_contain_scores():
    reviews = _reviews(10)
    table = _table(reviews)
    selection = build_selection(reviews, table, evaluation_n=3, challenge_high=1,
                                challenge_medium=0, challenge_low=0, challenge_duplicate=0, seed=1)
    for entry in selection.entries:
        payload = entry.model_dump()
        assert "score" not in " ".join(payload.keys())
        assert "templated" not in payload
        assert "specificity" not in payload
        assert "duplicate" not in payload


def test_selection_save_load_roundtrip(tmp_path):
    reviews = _reviews(20)
    table = _table(reviews)
    selection = build_selection(reviews, table, evaluation_n=6, challenge_high=2,
                                challenge_medium=2, challenge_low=2, challenge_duplicate=0, seed=9)
    path = tmp_path / "sample_selection.json"
    selection.save(path)
    loaded = Selection.load(path)
    assert loaded.fingerprint == selection.fingerprint
    assert loaded.seed == selection.seed
    assert [e.review_id for e in loaded.entries] == [e.review_id for e in selection.entries]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "entries" in raw and raw["entries"][0]["review_id"]
