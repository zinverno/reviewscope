"""Offline cache regressions. No model weights or optional ML libraries needed."""

import hashlib
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from reviewscope.embeddings.base import EmbeddingProvider, EncodingContract, embedding_key
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider
from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore


class FakeEncoder(EmbeddingProvider):
    def __init__(self, **settings):
        self._contract = replace(
            EncodingContract("fake", "rev1", "fake-tokenizer/mean", 128, 4, False, "test", 8),
            **settings,
        )
        self.calls = []

    @property
    def contract(self):
        return self._contract

    def _encode_nonblank(self, texts):
        self.calls.append(list(texts))
        return [
            list(hashlib.sha256((self.contract.encoding_id + t).encode()).digest()[:self.dim])
            for t in texts
        ]


def review(text, rid="same"):
    return NormalizedReview(review_id=rid, text=text, place_id="p", reviewer_id="u")


def test_cold_session_reopen_and_order(tmp_path):
    texts = ["Ёж", "ёж", " Ёж", "Ёж ", "Е\u0308ж", "ёж " * 600, "", " \t", None]
    rows = [review(t) for t in texts] + [review("Ёж"), review(" Ёж")]
    path = tmp_path / "cache.duckdb"
    writer = FakeEncoder()
    with DuckDBStore(path) as store:
        cache = EmbeddingCache(store, writer)
        assert cache.cache_hit_ratio(rows) == 0
        cold = cache.embed_reviews(rows)
        assert writer.calls == [texts[:6]]
        assert cold.shape == (11, 4) and cold.dtype == np.float32
        for i, row in enumerate(rows):
            t = row.text_or_empty()
            content = (writer.contract.encoding_id + t).encode()
            expected = list(hashlib.sha256(content).digest()[:4]) if t.strip() else [0] * 4
            np.testing.assert_array_equal(cold[i], expected)
        assert store.cached_embedding_count() == 8
        assert cache.cache_hit_ratio(rows) == 1
        np.testing.assert_array_equal(cache.embed_reviews(rows[::-1]), cold[::-1])
        assert len(writer.calls) == 1
        mixed = [rows[0], review("ЁЖ"), rows[0]]
        assert cache.cache_hit_ratio(mixed) == pytest.approx(2 / 3)
        partial = cache.embed_reviews(mixed)
        np.testing.assert_array_equal(partial[[0, 2]], cold[[0, 0]])
        assert not np.array_equal(partial[1], cold[0])
        assert writer.calls[1:] == [["ЁЖ"]]
    reader = FakeEncoder()
    with DuckDBStore(path, read_only=True) as store:
        cache = EmbeddingCache(store, reader)
        assert cache.cache_hit_ratio(rows) == 1
        np.testing.assert_array_equal(cache.embed_reviews(rows), cold)
        assert reader.calls == []


@pytest.mark.parametrize("setting", [
    {"revision": "rev2"}, {"encoder": "other-tokenizer/mean"}, {"max_seq_length": 32},
    {"normalize_embeddings": True}, {"dim": 3}, {"model": "other"}, {"runtime": "next"},
    {"batch_size": 4},
])
def test_encoding_changes_are_misses(setting):
    with DuckDBStore() as store:
        original = EmbeddingCache(store, FakeEncoder())
        rows = [review("текст")]
        first = original.embed_reviews(rows)
        provider = FakeEncoder(**setting)
        changed = EmbeddingCache(store, provider)
        assert changed.encoding_id != original.encoding_id
        assert changed.cache_hit_ratio(rows) == 0
        result = changed.embed_reviews(rows)
        assert result.shape == (1, provider.dim)
        expected = hashlib.sha256((changed.encoding_id + "текст").encode()).digest()[:provider.dim]
        np.testing.assert_array_equal(result[0], list(expected))
        assert not np.array_equal(result, first)
        assert provider.calls == [["текст"]]
        assert store.cached_embedding_count() == 2


@pytest.mark.parametrize("bad", [[[1]], [[1] * 4] * 2, [[np.nan] * 4], [[np.inf] * 4]])
def test_invalid_encoder_batch_never_published(bad, monkeypatch):
    provider = FakeEncoder()
    encode = Mock(return_value=bad)
    monkeypatch.setattr(provider, "_encode_nonblank", encode)
    with DuckDBStore() as store:
        cache = EmbeddingCache(store, provider)
        rows = [review("abc"), review("")]
        with pytest.raises(ValueError, match="finite embeddings of shape"):
            cache.embed_reviews(rows)
        encode.assert_called_once_with(["abc"])
        assert cache.cache_hit_ratio(rows) == 0 and store.cached_embedding_count() == 0


@pytest.mark.parametrize("bad", [[1], [], [float("nan")] * 4, [float("inf")] * 4])
def test_corrupt_rows_readonly_and_writable(tmp_path, monkeypatch, bad):
    path = tmp_path / "bad.duckdb"
    provider = FakeEncoder()
    key = embedding_key("same", "abc", provider.contract.encoding_id)
    rows = [review("abc")]
    with DuckDBStore(path) as store:
        store.store_cached_embeddings([(*key, bad)])
    before = hashlib.sha256(path.read_bytes()).digest()
    with DuckDBStore(path, read_only=True) as store:
        cache = EmbeddingCache(store, provider)
        monkeypatch.setenv("RS_PUBLIC_DEMO", "1")
        assert cache.missing_keys(rows) == [key]
        with pytest.raises(ValueError, match="precomputed embeddings"):
            cache.embed_reviews(rows)
        assert provider.calls == []
        monkeypatch.setenv("RS_PUBLIC_DEMO", "0")
        temporary = cache.embed_reviews(rows)
        assert provider.calls == [["abc"]]
        assert cache.cache_hit_ratio(rows) == 1
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with DuckDBStore(path) as store:
        cache = EmbeddingCache(store, provider)
        assert cache.cache_hit_ratio(rows) == 0
        np.testing.assert_array_equal(cache.embed_reviews(rows), temporary)
        assert provider.calls == [["abc"], ["abc"]]
        np.testing.assert_array_equal(store.get_cached_embeddings([key])[key], temporary[0])


def test_nonzero_blank_and_unverified_legacy_row_are_misses():
    provider = FakeEncoder()
    raw = review("Text")
    blank = review("  ")
    with DuckDBStore() as store:
        store.store_cached_embeddings([
            (raw.review_id, raw.fingerprint(), provider.name, [1] * 4),
            (*embedding_key("same", "  ", provider.contract.encoding_id), [1] * 4),
        ])
        cache = EmbeddingCache(store, provider)
        assert cache.cache_hit_ratio([raw, blank]) == 0
        vectors = cache.embed_reviews([blank, raw])
        np.testing.assert_array_equal(vectors[0], np.zeros(4))
        assert provider.calls == [["Text"]]
        assert store.cached_embedding_count() == 3
        assert cache.embed_reviews([]).shape == (0, 4)


def test_large_lookup_crosses_several_batches():
    rows = [review(str(i), str(i)) for i in range(1059)]
    with DuckDBStore() as store:
        writer = FakeEncoder()
        cold = EmbeddingCache(store, writer).embed_reviews(rows)
        assert writer.calls == [[r.text for r in rows]]
        reader = FakeEncoder()
        connection = Mock(wraps=store.connection())
        cache = EmbeddingCache(DuckDBStore(con=connection, read_only=True), reader)
        np.testing.assert_array_equal(cache.embed_reviews(rows[::-1]), cold[::-1])
        assert reader.calls == []
        assert [len(call.args[1]) for call in connection.execute.call_args_list] == [768] * 4 + [105]


def test_cache_rejects_broken_public_encode_override(monkeypatch):
    provider = FakeEncoder()
    encode = Mock(return_value=np.ones((2, 4), dtype=np.float32))
    monkeypatch.setattr(provider, "encode", encode)
    with DuckDBStore() as store:
        cache = EmbeddingCache(store, provider)
        with pytest.raises(ValueError, match="zero vectors"):
            cache.embed_reviews([review(""), review("x")])
        encode.assert_called_once_with(["", "x"])
        assert store.cached_embedding_count() == 0


@pytest.mark.parametrize(("dimension", "length", "error"), [
    (383, 128, "dimension"), (384, 64, "sequence length"), (384, 256, None),
])
def test_runtime_revision_dimension_and_effective_length(monkeypatch, dimension, length, error):
    provider = SentenceTransformerProvider()
    model = Mock(max_seq_length=length, tokenizer=SimpleNamespace(model_max_length=512))
    model.get_sentence_embedding_dimension.return_value = dimension
    model.float.return_value = model
    model.encode.return_value = np.ones((1, 384))
    loader = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=loader))
    monkeypatch.setattr("reviewscope.embeddings.sentence_transformer.installed_runtime",
                        lambda: provider.contract.runtime)
    assert not provider.encode(["", " \n"]).any()
    loader.assert_not_called()
    if error:
        with pytest.raises(ValueError, match=error):
            provider.encode(["X"])
        model.encode.assert_not_called()
    else:
        vector = provider.encode(["", "X"])
        assert vector.shape == (2, 384) and not vector[0].any() and vector[1].all()
        assert model.max_seq_length == provider.contract.max_seq_length == 128
        assert model.encode.call_args.args == (["X"],)
        assert model.encode.call_args.kwargs["normalize_embeddings"] is False
    loader.assert_called_once_with(provider.name, revision=provider.contract.revision, device="cpu")


def test_mutable_or_unknown_revision_is_rejected():
    with pytest.raises(ValueError, match="immutable"):
        SentenceTransformerProvider(revision="main")
    with pytest.raises(ValueError, match="immutable"):
        SentenceTransformerProvider("other/model")
