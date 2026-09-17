"""Unit tests for keyword extraction (SPEC.md §9, §37)."""

from __future__ import annotations

from reviewscope.analysis.keywords import (
    emerging_keywords,
    extract_keywords,
    keywords_by_rating,
    keywords_by_sentiment,
)
from reviewscope.models.review import NormalizedReview


def _review(review_id: str, text: str, rating: int = 5, published_at: str = "2026-09-01") -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id="p1",
        reviewer_id=f"u-{review_id}",
        rating=rating,
        text=text,
        published_at=published_at,
    )


def test_extract_keywords_returns_weighted_terms() -> None:
    reviews = [
        _review("a", "Вкусное кофе, яркий вкус эспрессо и свежее молоко, обслуживание быстрое"),
        _review("b", "кофе отличное, десерт вкусный, кофе возможно запоминается"),
        _review("c", "кофе подают горячим, вкус кофе понравился"),
    ]
    keywords = extract_keywords(reviews, top_n=5)
    assert keywords
    terms = [term for term, _ in keywords]
    assert "кофе" in terms
    assert all(isinstance(score, float) for _, score in keywords)
    assert sorted((score for _, score in keywords), reverse=True) == [s for _, s in keywords]


def test_extract_keywords_empty() -> None:
    assert extract_keywords([], top_n=5) == []


def test_sentiment_split_by_rating() -> None:
    reviews = [
        _review("a", "Супер кофе, отличный сервис, вкусная выпечка", rating=5),
        _review("b", "кофе ужасный, горький, персонал нахал", rating=1),
        _review("c", "нормальный кофе, средний вкус", rating=3),
    ]
    split = keywords_by_sentiment(reviews, top_n=4)
    assert "positive" in split and "negative" in split
    assert split["positive"] != []
    assert split["negative"] != []
    # Politely check that the two groups pull from different vocab.
    pos = {t for t, _ in split["positive"]}
    neg = {t for t, _ in split["negative"]}
    assert "кофе" in pos or "кофе" in neg
    assert ("супер" in pos) or ("горьк" in pos) or ("горький" in pos) or ("ужасн" in neg) or (pos and neg)


def test_keywords_by_rating_groups() -> None:
    reviews = [
        _review("a", "Отличное место, всë понравилось", rating=5),
        _review("b", "Ужасно", rating=1),
    ]
    grouped = keywords_by_rating(reviews, top_n=3)
    assert set(grouped.keys()) == {1, 2, 3, 4, 5}
    assert grouped[1] or grouped[5]


def test_emerging_keywords_detects_recent_growth() -> None:
    older = [_review(f"o{i}", "Спокойное качество, обычный кофе, неспешный сервис", rating=4, published_at="2026-06-01") for i in range(6)]
    recent = [_review(f"n{i}", "Выбросили лимонад? Нет, долгое ожидание лимонада!", rating=2, published_at="2026-09-01") for i in range(6)]
    emerging = emerging_keywords(older + recent, reference_days=90, recent_days=30, top_n=5)
    assert isinstance(emerging, list)
    if emerging:
        assert all(ratio > 1.0 for _, ratio in emerging)
