"""Unit tests for the synthetic/templated text score (SPEC.md §16)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from reviewscope.analysis.templated import (
    TemplatedTextScorer,
    _content_bigrams,
    _sentence_length_profile,
    _vocab_diversity,
    templated_frame,
)
from reviewscope.models.review import NormalizedReview


def _review(
    review_id: str,
    place_id: str,
    text: str,
    day: date | None = None,
    rating: int = 5,
) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"user-{review_id}",
        rating=rating,
        text=text,
        published_at=(day or date(2026, 9, 1)).isoformat(),
    )


_TEMPLATED = (
    "Всё было идеально, обслуживание на высоте, отличный сервис, "
    "вкусная еда, я рекомендую всем посетить"
)


def _templated_group(n: int, place: str) -> list[NormalizedReview]:
    start = date(2026, 9, 1)
    return [
        _review(f"t{i}", place, _TEMPLATED, start + timedelta(days=i % 5)) for i in range(n)
    ]


def _unique_reviews(n: int, place: str) -> list[NormalizedReview]:
    bodies = [
        "Заказал стейк рибай с кровью за 1400 рублей, официант приносил десерт на закуску.",
        "Пришел в субботу, попросили чек для организации, вернулись через два дня.",
        "Куриный бульон остыл, хлеб был черствым, а кофе подали через 12 минут.",
        "понравился дизайн интерьера и вид на набережную, брал рифленый раф",
    ]
    return [
        _review(f"u{i}", place, bodies[i % len(bodies)], date(2026, 2, i + 11), rating=4 ^ (i % 2))
        for i in range(n)
    ]


class TestHelpers:
    def test_sentence_profile_normalizes(self) -> None:
        p = _sentence_length_profile("Один. Два слова. Три коротких слова. Четыре пять шесть.")
        assert p.shape == (6,)
        assert abs(p.sum() - 1.0) < 1e-5

    def test_content_bigrams_skip_stopwords(self) -> None:
        out = _content_bigrams("вкусная еда и отличный сервис")
        assert ("вкусная", "еда") in out
        assert ("еда", "и") not in out

    def test_vocab_diversity(self) -> None:
        assert _vocab_diversity("a a a a") < _vocab_diversity("a b c d")


class TestTemplatedScorer:
    def test_single_unique_review_stays_low(self) -> None:
        reviews = _unique_reviews(1, "p1")
        [res] = TemplatedTextScorer().score(reviews)
        assert res.value < 40

    def test_small_unique_group_stays_low(self) -> None:
        reviews = _unique_reviews(4, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value < 45 for r in results)

    def test_templated_group_scores_high(self) -> None:
        reviews = _templated_group(12, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value >= 65 for r in results)
        assert all(r.confidence.value == "HIGH" for r in results)

    def test_templated_group_flagged_over_crowd(self) -> None:
        # 30 highly similar polished reviews -> scores well above a lone review.
        group = _templated_group(30, "p1")
        lone = _unique_reviews(1, "p2")
        results = TemplatedTextScorer().score(group + lone)
        group_avg = np.mean([r.value for r in results[:30]])
        lone_val = results[-1].value
        assert group_avg >= 65
        assert lone_val < group_avg - 20

    def test_semantic_signal_with_embeddings(self) -> None:
        # 6 near-identical embedding vectors boost semantic component.
        rng = np.random.RandomState(4)
        base = rng.rand(384).astype(np.float32)
        reviews = _templated_group(6, "p1")
        embeddings = np.array([base + rng.rand(384).astype(np.float32) * 0.05 for _ in reviews])
        results = TemplatedTextScorer().score(reviews, embeddings)
        assert all(r.details["semantic"] > 0.5 for r in results)

    def test_empty(self) -> None:
        assert TemplatedTextScorer().score([]) == []

    def test_temporal_signal(self) -> None:
        reviews = _templated_group(6, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert any(r.details["temporal"] > 0.5 for r in results)


def test_templated_frame() -> None:
    reviews = _templated_group(5, "p1")
    results = TemplatedTextScorer().score(reviews)
    frame = templated_frame(reviews, results)
    assert list(frame.columns) == ["review_id", "templated_score", "confidence"]
    assert len(frame) == 5
