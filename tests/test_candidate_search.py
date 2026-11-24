"""Independent slice, ranking and evaluation checks. Fake encoder is only a unit fixture."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from reviewscope.candidates import CandidateIndex, Document, anchors, text_sha, unit_dense
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider

SCOPE = {"workspace": "w", "source": "s", "snapshot": "immutable"}


def doc(id, text):
    return Document(id, text_sha(text), text)


def test_offsets_cover_tail_and_restore_original():
    text = (
        "  🧑‍🍳 е\u0301.\r\n«Не пришлось ждать!»  URL https://ex.test/a. 3.14 руб.\n"
        + "ёж" * 480
        + " — конец"
    )
    bounds = anchors(text, lambda t: len(t) + 2)
    assert bounds == anchors(text, lambda t: len(t) + 2)
    assert all(0 <= a < b <= len(text) and b - a + 2 <= 128 for a, b in bounds)
    assert set(i for a, b in bounds for i in range(a, b)) >= {
        i for i, c in enumerate(text) if not c.isspace()
    }
    assert text[bounds[-1][0] : bounds[-1][1]].endswith("конец")
    idx = CandidateIndex(SCOPE, [doc("a", text), doc("b", "\r\n")])
    hit = idx.search("не пришлось ждать", 1)["candidates"][0]
    assert text[hit["start"] : hit["end"]] == "«Не пришлось ждать!»"
    assert "е\u0301.\r\n«Не пришлось ждать!»" in text[hit["context_start"] : hit["context_end"]]
    assert hit["text_sha256"] == text_sha(text)
    assert (idx.total, idx.eligible) == (2, 1)
    other = CandidateIndex(SCOPE | {"snapshot": "other"}, [doc("a", text)])
    assert set(s.fragment_id for s in idx.spans).isdisjoint(s.fragment_id for s in other.spans)


class Fake:
    contract = SentenceTransformerProvider().contract

    def token_count(self, text):
        return len(text) + 2

    def encode(self, texts):
        values = np.zeros((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            values[i, :2] = [3, 4] if "цвет" in t else [1, 0]
        return values


def test_dense_and_fusion_are_exact_and_scope_bound():
    docs = [doc("1", "цвет."), doc("2", "камень."), doc("3", "цвет.")]
    enc = Fake()
    index = CandidateIndex(SCOPE, docs, "hybrid", enc)
    got = index.search("цвет", 3)
    hits = got["candidates"]
    assert len(hits) == 3 and [h["rank"] for h in hits] == [1, 2, 3]
    assert {h["version_id"] for h in hits[:2]} == {"1", "3"}
    assert hits[0]["fragment_id"] < hits[1]["fragment_id"]
    assert hits[0]["score"] == pytest.approx(2 / 61)
    assert hits[1]["score"] == pytest.approx(2 / 62)
    assert hits[2]["channels"]["dense"]["similarity"] == pytest.approx(0.6)
    assert set(hits[2]["channels"]) == {"dense"}
    assert got["query_role_id"] != index.search("цвет", 3, "text_to_text")["query_role_id"]
    changed = replace(enc.contract, revision="1" * 40)
    enc.contract = changed
    with pytest.raises(ValueError, match="INCOMPATIBLE_ENCODING"):
        index.search("x", 1)
    for item in (
        changed,
        replace(Fake.contract, dim=128),
        replace(Fake.contract, encoder="prefix"),
    ):
        enc.contract = item
        with pytest.raises(ValueError, match="INCOMPATIBLE_ENCODING"):
            CandidateIndex(SCOPE, docs, "dense", enc)
    with pytest.raises(ValueError, match="NOT_RUN_MISSING"):
        CandidateIndex(SCOPE, docs, "dense")
    for matrix in ([[0, 0]], [[np.inf, 0]], [[1]]):
        with pytest.raises(ValueError):
            unit_dense(matrix, 1, 2)


def test_distinct_review_budget_oov_and_exclusions():
    docs = [
        doc("long", "Цвет. " * 100),
        doc("also", "Цветы."),
        doc("different", "Каштан."),
        doc("empty", ""),
    ]
    idx = CandidateIndex(SCOPE, docs)
    got = idx.search("цвет", 2)
    assert sum(h["version_id"] == "long" for h in got["candidates"]) == 2
    assert got["retrieved_review_count"] == 2
    assert got["excluded_blank"] == 1 and got["candidate_count"] == 3
    assert idx.search("12345", 1)["candidates"] == []
    assert CandidateIndex(SCOPE, [doc("e", "  ")]).search("цвет", 3)["fragment_count"] == 0
    for q, k, role in [("", 1, "text_to_text"), ("q", 0, "text_to_text"), ("q", 1, "document")]:
        with pytest.raises(ValueError):
            idx.search(q, k, role)
    with pytest.raises(ValueError, match="DUPLICATE_VERSION"):
        CandidateIndex(SCOPE, docs + docs)
    assert CandidateIndex(SCOPE, [doc("a", "abc"), doc("b", "xyz")]).search("abc", 1)["candidates"][
        0
    ]["score"] == pytest.approx(1)


def test_bounded_metrics_use_complete_corpus(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from benchmark_candidates import evaluate

    ids = ["a", "b", "c", "d"]
    hit = dict(version_id="a", start=3, end=9, context_start=0, context_end=12)
    labels = {
        "a": (2, "mixed", [(3, 9, 0, 12), (2, 5, 0, 10)]),
        "b": (2, "complaint", []),
        "c": (1, "denial", []),
        "d": (0, "offtopic", []),
    }
    row = evaluate(ids, [hit, hit], labels, 2)
    assert (row["recall"], row["returned"], row["span_hits"], row["span_total"]) == (0.5, 1, 1, 2)
    assert evaluate(ids, [hit], labels | {"b": (None, None, [])}, 1)["recall"] is None
    assert evaluate(["c"], [], {"c": labels["c"]}, 1)["recall"] is None
    with pytest.raises(ValueError):
        evaluate(ids + ids, [], labels, 1)
