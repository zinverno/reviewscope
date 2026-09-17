"""TF-IDF keyword extraction (SPEC.md §9).

The module operates on review texts with no dependency on the embedding
layer.  A lightweight ``TfidfVectorizer`` with a multilingual stopword set
(carried as static resources) provides:

* general keywords for a place;
* positive / negative keywords split by rating;
* emerging keywords compared against a historical baseline.

Russian and English both get basic support via the bundled stopword files
(SPEC.md §9, engineering req 6).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from reviewscope.models.review import NormalizedReview
from reviewscope.resources import EN_STOPWORDS, RU_STOPWORDS

_WORD_RE = re.compile(r"\b\w{2,}\b", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _build_stop_words() -> list[str]:
    return sorted(EN_STOPWORDS | RU_STOPWORDS)


STOP_WORDS_LIST: list[str] = _build_stop_words()


def _make_vectorizer(
    ngram_range: tuple[int, int] = (1, 2),
    max_features: int = 5000,
) -> TfidfVectorizer:
    return TfidfVectorizer(
        tokenizer=_tokenize,
        stop_words=STOP_WORDS_LIST,
        ngram_range=ngram_range,
        max_features=max_features,
        sublinear_tf=True,
        token_pattern=None,
        preprocessor=str.lower,
    )


def _extract(review_texts: Sequence[str], top_n: int = 15) -> list[tuple[str, float]]:
    """Return the top-n (term, tf-idf score) pairs from the given texts."""
    texts = [t for t in review_texts if t.strip()]
    if len(texts) < 2:
        if texts:
            words = Counter(_tokenize(texts[0]))
            total = sum(words.values()) or 1
            return [(w, c / total) for w, c in words.most_common(top_n)]
        return []
    vec = _make_vectorizer()
    matrix = vec.fit_transform(texts)
    avg = np.asarray(matrix.mean(axis=0)).flatten()
    order = np.argsort(-avg)[:top_n]
    vocab = vec.get_feature_names_out()
    return [(str(vocab[i]), float(avg[i])) for i in order if avg[i] > 0]


# --------------------------------------------------------------------------- public API


def extract_keywords(
    reviews: Sequence[NormalizedReview],
    top_n: int = 15,
) -> list[tuple[str, float]]:
    """Return the top keywords for a set of reviews (used by the Topics and Overview pages)."""
    return _extract([r.text or "" for r in reviews], top_n)


def keywords_by_sentiment(
    reviews: Sequence[NormalizedReview],
    top_n: int = 10,
) -> dict[str, list[tuple[str, float]]]:
    """Return keywords split into positive (rating ≥ 4) and negative (rating ≤ 2) groups."""
    positive = [r.text or "" for r in reviews if r.rating is not None and r.rating >= 4]
    negative = [r.text or "" for r in reviews if r.rating is not None and r.rating <= 2]
    return {
        "positive": _extract(positive, top_n),
        "negative": _extract(negative, top_n),
    }


def keywords_by_rating(
    reviews: Sequence[NormalizedReview],
    top_n: int = 8,
) -> dict[int, list[tuple[str, float]]]:
    """Keywords grouped per star rating (1..5)."""
    result: dict[int, list[tuple[str, float]]] = {}
    for rating in range(1, 6):
        texts = [r.text or "" for r in reviews if r.rating == rating]
        result[rating] = _extract(texts, top_n)
    return result


def emerging_keywords(
    reviews: Sequence[NormalizedReview],
    reference_days: int = 90,
    recent_days: int = 30,
    reference_date: datetime | None = None,
    top_n: int = 10,
) -> list[tuple[str, float]]:
    """Identify keywords that grew in frequency recently vs. the reference period.

    ``reference_date`` defaults to the latest ``published_at`` in the review
    set.  Words are ranked by the ratio of average TF-IDF (recent / reference);
    ratios above 1.0 indicate emerging terms.
    """
    if not reviews:
        return []
    dates: list[datetime] = []
    for r in reviews:
        try:
            dates.append(datetime.fromisoformat(r.published_at))
        except (TypeError, ValueError):
            pass
    if not dates:
        return []
    anchor = reference_date or max(dates)

    def within(published_iso: str, start: datetime, end: datetime) -> bool:
        try:
            dt = datetime.fromisoformat(published_iso)
        except (TypeError, ValueError):
            return False
        return start <= dt <= end

    from datetime import timedelta

    ref_window = (anchor - timedelta(days=reference_days + recent_days), anchor - timedelta(days=recent_days))
    recent_window = (anchor - timedelta(days=recent_days), anchor)
    ref_texts = [r.text or "" for r in reviews if r.published_at and within(r.published_at, ref_window[0], ref_window[1])]
    recent_texts = [r.text or "" for r in reviews if r.published_at and within(r.published_at, recent_window[0], recent_window[1])]
    if not recent_texts or not ref_texts:
        return []

    vec = _make_vectorizer()
    corpus = ref_texts + recent_texts
    matrix = vec.fit_transform(corpus)
    n_ref = len(ref_texts)
    ref_mean = np.asarray(matrix[:n_ref].mean(axis=0)).flatten()
    rec_mean = np.asarray(matrix[n_ref:].mean(axis=0)).flatten()
    eps = 1e-9
    ratio = (rec_mean + eps) / (ref_mean + eps)

    order = np.argsort(-ratio)[:top_n]
    vocab = vec.get_feature_names_out()
    return [(str(vocab[i]), float(ratio[i])) for i in order if ratio[i] > 1.0]
