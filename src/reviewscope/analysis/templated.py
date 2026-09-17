"""Synthetic / templated text score (SPEC.md §16).

This is deliberately NOT an "AI detector". It measures how strongly a review
resembles a reused template / other reviews in the same organization:

* semantic similarity  (with its closest textual peers)
* phrase reuse         (shared content bigrams across the group)
* structure similarity (sentence-length profile matches group centroid)
* generic language     (inverse of the specificity score)
* vocabulary diversity (low type/token ratio reads as formulaic)
* stylistic uniformity (group-level uniformity of sentence structure)
* temporal clustering  (peer reviews published in a narrow window)

All signals are contextual: a single well-written review has no peers to
match and stays below the anomaly band (SPEC.md §16).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from statistics import mean

import numpy as np

from reviewscope.config import CONFIG, TemplatedConfig
from reviewscope.models.review import NormalizedReview
from reviewscope.models.scores import ConfidenceLevel, ScoreResult
from reviewscope.resources import EN_STOPWORDS, RU_STOPWORDS

_TEMPORAL_WINDOW_DAYS = 7
_SENTENCE_RE = re.compile(r"[.!?…]+")

_TOKEN_RE = re.compile(r"[а-яёa-z]+", re.IGNORECASE)
_STOPWORDS = EN_STOPWORDS | RU_STOPWORDS


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]


def _sentence_length_profile(text: str, bins: int = 6, bin_width: int = 8) -> np.ndarray:
    """Histogram of sentence word-counts, fixed bins -> structure fingerprint."""
    profile = np.zeros(bins, dtype=np.float32)
    for s in _sentences(text):
        n_words = len(s.split())
        idx = min(n_words // bin_width, bins - 1)
        profile[idx] += 1
    total = profile.sum()
    if total:
        profile /= total
    return profile


def _content_bigrams(text: str) -> list[tuple[str, str]]:
    words = [w.lower() for w in _TOKEN_RE.findall(text or "") if w.lower() not in _STOPWORDS]
    return list(zip(words, words[1:], strict=False))


def _vocab_diversity(text: str) -> float:
    words = [w.lower() for w in _TOKEN_RE.findall(text or "") if w.lower() not in _STOPWORDS]
    if not words:
        return 0.0
    return len(set(words)) / len(words)


def _scale(value: float, lo: float, hi: float) -> float:
    """Map ``value`` from [lo, hi] onto [0, 1], climbing from ``lo``."""
    if hi <= lo:
        return 0.0
    return float(max(0.0, min(1.0, (value - lo) / (hi - lo))))


class TemplatedTextScorer:
    """Compute per-review synthetic-templated scores for a group of reviews."""

    def __init__(self, config: TemplatedConfig = CONFIG.templated) -> None:
        self.config = config

    def _semantic_signal(
        self, reviews: list[NormalizedReview], embeddings: np.ndarray | None, same_place: list[bool]
    ) -> list[float]:
        """Mean cosine of each review against its nearest structural peers.

        Uses the embedding matrix when provided; otherwise falls back to a
        content-bigram cosine so the signal works text-only too (SPEC.md §16
        must still fire for 30 very similar reviews even without embeddings).
        """
        n = len(reviews)
        if n < 2:
            return [0.0] * n
        if embeddings is None:
            bags = [set(_content_bigrams(r.text_or_empty())) for r in reviews]
            sim = np.zeros((n, n), dtype=np.float32)
            for i in range(n):
                for j in range(i + 1, n):
                    inter = len(bags[i] & bags[j])
                    denom = len(bags[i]) + len(bags[j]) - inter
                    sim[i, j] = sim[j, i] = (inter / denom) if denom else 0.0
            return self._neighbor_signal(sim, same_place)
        if embeddings.shape[0] != n:
            return [0.0] * n
        norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9)
        sim = norm @ norm.T
        return self._neighbor_signal(sim, same_place)

    def _neighbor_signal(self, sim: np.ndarray, same_place: list[bool]) -> list[float]:
        signals: list[float] = []
        place = np.array(same_place, dtype=bool)
        for i in range(sim.shape[0]):
            peers = np.flatnonzero(place)
            peers = peers[peers != i]
            if peers.size == 0:
                signals.append(0.0)
                continue
            k = min(self.config.high_match_count, int(peers.size))
            top = np.sort(sim[i, peers])[::-1][:k]
            signals.append(_scale(float(top.mean()), 0.5, 0.82))
        return signals

    def _phrase_reuse_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        bigram_owners: dict[tuple[str, str], int] = defaultdict(int)
        per_review = [set(_content_bigrams(r.text_or_empty())) for r in reviews]
        for bg in per_review:
            for b in bg:
                bigram_owners[b] += 1
        signals: list[float] = []
        threshold = self.config.high_match_count
        for bg in per_review:
            if not bg:
                signals.append(0.0)
                continue
            shared = sum(1 for b in bg if bigram_owners[b] > threshold)
            signals.append(float(shared / len(bg)))
        return signals

    def _structure_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        profiles = np.array([_sentence_length_profile(r.text_or_empty()) for r in reviews])
        n = len(profiles)
        if n < 2:
            return [0.0] * n
        norm = profiles / (np.linalg.norm(profiles, axis=1, keepdims=True) + 1e-9)
        sim = norm @ norm.T
        signals: list[float] = []
        for i in range(n):
            peers = np.arange(n)
            peers = peers[peers != i]
            k = min(self.config.high_match_count, int(peers.size))
            top = np.sort(sim[i, peers])[::-1][:k]
            signals.append(_scale(float(top.mean()), 0.5, 0.85))
        return signals

    def _style_uniformity_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        """Group-level uniformity: low variance in sentence lengths = uniform tone."""
        lengths = [float(len(s.split())) for r in reviews for s in _sentences(r.text_or_empty())]
        if len(lengths) < 2:
            return [0.0] * len(reviews)
        cv = float(np.std(lengths) / (mean(lengths) + 1e-9))
        # High cv -> low uniformity -> low signal (uniform tone scores highest).
        signal = 1.0 - _scale(cv, 0.25, 0.9)
        return [signal] * len(reviews)

    def _temporal_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        dates: list[date | None] = []
        for r in reviews:
            raw = (r.published_at or "")[:10]
            try:
                dates.append(date.fromisoformat(raw) if raw else None)
            except ValueError:
                dates.append(None)
        valid_indices = [i for i, d in enumerate(dates) if d is not None]
        signals = [0.0] * len(reviews)
        for i in range(len(reviews)):
            if i not in valid_indices:
                continue
            di = dates[i]
            assert di is not None
            peers = sum(
                1
                for j in valid_indices
                if j != i and abs((dates[j] - di).days) <= _TEMPORAL_WINDOW_DAYS  # type: ignore[operator]
            )
            signals[i] = _scale(float(peers), 0, float(self.config.high_match_count))
        return signals

    def score(
        self,
        reviews: list[NormalizedReview],
        embeddings: np.ndarray | None = None,
    ) -> list[ScoreResult]:
        """Return one :class:`ScoreResult` per review, in input order."""
        n = len(reviews)
        if n == 0:
            return []
        same_place: list[bool] = []
        for i, r in enumerate(reviews):
            if not r.place_id:
                same_place.append(True)
                continue
            same_place.append(
                sum(1 for j, o in enumerate(reviews) if j != i and o.place_id == r.place_id) > 0
            )

        w = self.config
        semantic = self._semantic_signal(reviews, embeddings, same_place)
        phrase = self._phrase_reuse_signal(reviews)
        structure = self._structure_signal(reviews)
        vec_diversity = [_scale(0.9 - _vocab_diversity(r.text_or_empty()), 0.0, 0.5) for r in reviews]
        generic = []
        for r in reviews:
            from reviewscope.analysis.specificity import specificity_score

            spec = specificity_score(r.text_or_empty()).value
            generic.append(_scale(100.0 - spec, 50.0, 100.0))
        uniform = self._style_uniformity_signal(reviews)
        temporal = self._temporal_signal(reviews)

        results: list[ScoreResult] = []
        for i in range(n):
            signals: list[str] = []
            counter: list[str] = []
            total = 0.0
            sem, phr, strc, gen, div, uni, tmp = (
                semantic[i],
                phrase[i],
                structure[i],
                generic[i],
                vec_diversity[i],
                uniform[i],
                temporal[i],
            )
            total = (
                w.semantic_group_weight * sem
                + w.phrase_reuse_weight * phr
                + w.structure_weight * strc
                + w.low_specificity_weight * gen
                + w.vocabulary_diversity_weight * div
                + w.stylistic_uniformity_weight * uni
                + w.temporal_clustering_weight * tmp
            )
            score = round(100.0 * total, 1)

            if sem >= 0.5:
                signals.append("high semantic similarity with peers")
            else:
                counter.append("textually distinct from peers")
            if phr >= 0.3:
                signals.append("repeated generic phrases across reviews")
            if strc >= 0.5:
                signals.append("high structural similarity with peers")
            if gen >= 0.5:
                signals.append("low unique detail density")
            if div >= 0.5:
                signals.append("low vocabulary diversity")
            if uni >= 0.5:
                signals.append("stylistically uniform with group")
            if tmp >= 0.5:
                signals.append("reviews published in narrow time window")

            confidence = ConfidenceLevel.HIGH if score >= 65 else (
                ConfidenceLevel.MEDIUM if score >= 40 else ConfidenceLevel.LOW
            )
            results.append(
                ScoreResult(
                    name="Synthetic-like",
                    value=score,
                    confidence=confidence,
                    signals=signals,
                    counter_signals=counter,
                    details={
                        "semantic": round(sem, 3),
                        "phrase_reuse": round(phr, 3),
                        "structure": round(strc, 3),
                        "generic": round(gen, 3),
                        "vocab_diversity": round(div, 3),
                        "stylistic_uniformity": round(uni, 3),
                        "temporal": round(tmp, 3),
                    },
                )
            )
        return results


def templated_frame(
    reviews: list[NormalizedReview], results: list[ScoreResult]
) -> np.ndarray | object:
    """Render review_id -> templated-score pairs for the UI."""
    import pandas as pd

    return pd.DataFrame(
        [
            {"review_id": r.review_id, "templated_score": res.value, "confidence": res.confidence}
            for r, res in zip(reviews, results, strict=False)
        ]
    )
