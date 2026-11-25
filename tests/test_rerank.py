"""Adapter protocol tests use explicit fakes. Quality is measured only by the frozen benchmark."""

from types import SimpleNamespace

import numpy as np
import pytest

from reviewscope.candidates import CandidateIndex, Document, text_sha
from reviewscope.embeddings.rerank import E5, CrossEncoder, verified_snapshot
from reviewscope.rerank import reorder
from scripts.benchmark_rerank import evaluate, frozen


def pool():
    docs = {v: Document(v, v, "Первый заказ. Второй заказ.") for v in ("a", "b")}
    return CandidateIndex({}, list(docs.values())).search("заказ", 2), docs


def test_permutation_and_exact_score_mapping():
    parent, docs = pool()
    ids = sorted(h["fragment_id"] for h in parent["candidates"])
    scores = dict(zip(ids, [-1.0, 3.0, 3.0, -2.0], strict=True))
    fake = SimpleNamespace(score=lambda q, windows: {k: scores[k] for k, _, _ in windows})
    ranks = reorder(parent, docs, "заказ", fake)
    parent["candidates"].reverse()
    assert ranks == reorder(parent, docs, "заказ", fake)
    assert [r["fragment_id"] for r in ranks] == [ids[1], ids[2], ids[0], ids[3]]
    assert [r["score"] for r in ranks] == [3.0, 3.0, -1.0, -2.0]
    assert {r["review_rank"] for r in ranks} == {1, 2}
    assert reorder(dict(parent, candidates=[], candidate_count=0), docs, "заказ", fake) == []
    for bad in (float("nan"), float("inf"), -float("inf"), None):
        with pytest.raises(ValueError, match="INCOMPATIBLE_MODEL"):
            reorder(
                parent,
                docs,
                "заказ",
                SimpleNamespace(score=lambda *_args, v=bad: dict(scores) | {ids[0]: v}),
            )
    for bad in ({}, {**scores, "extra": 0.0}):
        with pytest.raises(ValueError, match="INCOMPATIBLE_MODEL"):
            reorder(parent, docs, "заказ", SimpleNamespace(score=lambda *_args, v=bad: v))
    with pytest.raises(ValueError, match="INVALID_PROFILE"):
        reorder(parent, docs, "заказ", fake, profile="other")
    with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
        reorder(parent, docs, "заказ", fake, limit=3)
    with pytest.raises(ValueError, match="INVALID_PARENT"):
        reorder(parent, docs, "заказы", fake)
    with pytest.raises(ValueError, match="INVALID_PARENT"):
        reorder(parent, {}, "заказ", fake)
    with pytest.raises(ValueError, match="INVALID_PARENT"):
        reorder(dict(parent, candidates=[parent["candidates"][0]] * 4), docs, "заказ", fake)


def test_pair_budget_includes_specials_and_preserves_context():
    ce = CrossEncoder.__new__(CrossEncoder)
    ce.length = lambda text: len(text) + 2

    class Tokenizer:
        def __call__(self, left, right, **kwargs):
            assert kwargs["truncation"] is False
            return SimpleNamespace(
                input_ids=np.zeros((len(left), len(left[0]) + len(right[0]) + 4))
            )

    ce.tokenizer = Tokenizer()
    with pytest.raises(ValueError, match="QUERY_TOKEN_LIMIT"):
        ce.score("q" * 127, [])
    with pytest.raises(ValueError, match="ANCHOR_TOKEN_LIMIT"):
        ce.score("q", [("x", "a" * 127, "a" * 127)])
    with pytest.raises(ValueError, match="PAIR_TOKEN_LIMIT"):
        ce.score("q", [("x", "a", "a" * 508)])
    from contextlib import nullcontext

    ce.torch = SimpleNamespace(inference_mode=nullcontext)
    for logits in (
        np.ones((1, 2), dtype=np.float32),
        np.ones((1, 1), dtype=np.float64),
        np.full((1, 1), np.nan, dtype=np.float32),
    ):
        ce.model = lambda **kw: SimpleNamespace(logits=SimpleNamespace(numpy=lambda: logits))  # noqa: B023
        ce.tokenizer = lambda *args, **kw: Inputs()
        with pytest.raises(ValueError, match="INCOMPATIBLE_MODEL"):
            ce.score("q", [("x", "a", "a")])


class Inputs(dict):
    input_ids = np.ones((1, 3))


def test_e5_role_budgets():
    e = E5.__new__(E5)
    e.length = lambda text: text
    assert e.token_count("нет") == "passage: нет"
    assert e.query_count("нет") == "query: нет"
    e.encode = lambda texts, *, prefix: [prefix + t for t in texts]
    assert e.encode_query(["нет"]) == ["query: нет"]


def test_verified_model_fails_closed(tmp_path, monkeypatch):
    from reviewscope.embeddings import rerank

    monkeypatch.setenv("RS_RERANK_MODELS", str(tmp_path))
    monkeypatch.setattr(rerank.socket, "if_nameindex", lambda: [(1, "lo")])
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.setenv(k, "1")
    with pytest.raises(ValueError, match="INVALID_PROFILE"):
        verified_snapshot("other")
    with pytest.raises(ValueError, match="NOT_RUN_MISSING_WEIGHTS"):
        verified_snapshot("ce")
    model = tmp_path / rerank.MODELS["ce"]["revision"]
    model.mkdir()
    (model / "unexpected").write_bytes(b"")
    with pytest.raises(ValueError, match="INCOMPATIBLE_MODEL"):
        verified_snapshot("ce")
    monkeypatch.setattr(rerank.socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    with pytest.raises(ValueError, match="OFFLINE_REQUIRED"):
        verified_snapshot("ce")


def test_frozen_labels_and_denominators():
    protocol, rows = frozen()
    assert len(rows) == 110 and len(protocol["queries"]) == 24
    for row in rows:
        for label in row["labels"].values():
            for a, b, c, d in label["spans"]:
                assert 0 <= c <= a < b <= d <= len(row["text"])
    parent, _ = pool()
    labels = [dict(id="a", labels={"topic": None}), dict(id="b", labels={})]
    m = evaluate(parent["candidates"], labels, "topic")[5]
    assert (m["n"], m["unknown"], m["recall"], m["ndcg"]) == (2, 1, None, None)
    assert text_sha("заказ") == parent["query_sha256"]
