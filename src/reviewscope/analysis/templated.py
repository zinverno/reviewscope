"""Synthetic / templated text score (SPEC.md §16).

This is deliberately NOT an "AI detector". It measures how strongly a review
participates in a *reusable template family* inside the same organization.
A single well-written review has no cohort of reused peers and therefore
stays low, no matter how polished.

Signals (SPEC.md §16):

* phrase reuse        — share of the review's text covered by content n-grams
                        that are genuinely reused by a substantial part of the
                        same place's cohort (not just 1-2 close neighbours);
* peer similarity     — *fraction* of the place cohort that is near-identical
                        to the review at the semantic-duplicate level. A large
                        fraction = the review repeats a corpus-wide template;
* structure similarity— sentence-length profile matches the group centroid;
* low specificity     — inverse of the specificity score (generic language);
* low unique detail   — share of the review's content words that are *not*
                        reused by the cohort (length-robust, replaces raw TTR);
* stylistic uniformity— group-level uniformity of sentence structure;
* temporal clustering — peer reviews published in a narrow window.

Design invariants (from the forensic remediation):

1. one well-written, specific, unique review must NOT score high merely
   because it is polished — all strong signals are cohort-relative;
2. a cohort sharing phrase reuse / structure / peers / window scores
   materially higher;
3. semantic similarity alone must not imply templating — it is measured as
   *fraction of the cohort* at near-duplicate level, never as similarity to
   the closest neighbour;
4. temporal proximity alone is a small component (0.05 weight);
5. HIGH confidence requires several independent signals at once.
"""

from __future__ import annotations

import re
from collections import Counter
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


def _content_tokens(text: str) -> list[str]:
    """Lower-cased content tokens (stop words removed), positional order kept."""
    return [w.lower() for w in _TOKEN_RE.findall(text or "") if w.lower() not in _STOPWORDS]


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
    """Content bigrams covering consecutive *positions* (repeats preserved)."""
    words = _content_tokens(text)
    return list(zip(words, words[1:], strict=False))


def _ngrams(words: list[str], sizes: tuple[int, ...] = (2, 3)) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for size in sizes:
        for start in range(0, max(len(words) - size + 1, 0)):
            out.add(tuple(words[start : start + size]))
    return out


def _scale(value: float, lo: float, hi: float) -> float:
    """Map ``value`` from [lo, hi] onto [0, 1], climbing from ``lo``."""
    if hi <= lo:
        return 0.0
    return float(max(0.0, min(1.0, (value - lo) / (hi - lo))))


class TemplatedTextScorer:
    """Compute per-review synthetic-templated scores for a group of reviews."""

    def __init__(self, config: TemplatedConfig = CONFIG.templated) -> None:
        self.config = config

    # -- cohort helpers -------------------------------------------------------

    def _n_same_place_peers(self, same_place: list[bool], i: int) -> int:
        return max(0, sum(1 for j, sp in enumerate(same_place) if sp and j != i))

    def _similarity_matrix(
        self, reviews: list[NormalizedReview], embeddings: np.ndarray | None
    ) -> np.ndarray:
        """Pairwise cosine similarity matrix for the input reviews."""
        n = len(reviews)
        if n < 2:
            return np.zeros((n, n), dtype=np.float32)
        if embeddings is not None and embeddings.shape[0] == n:
            norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9)
            return norm @ norm.T
        # Text-only fallback: content-bigram cosine (SPEC.md §16 must still
        # fire for 30 byte-similar reviews without embeddings).
        bags = [set(_content_bigrams(r.text_or_empty())) for r in reviews]
        sim = np.zeros((n, n), dtype=np.float32)
        for i in range(n):
            for j in range(i + 1, n):
                inter = len(bags[i] & bags[j])
                denom = len(bags[i]) + len(bags[j]) - inter
                sim[i, j] = sim[j, i] = (inter / denom) if denom else 0.0
        return sim

    # -- signals --------------------------------------------------------------

    def _peer_similarity_signal(
        self, sim: np.ndarray, same_place: list[bool]
    ) -> list[float]:
        """Absolute count of same-place peers near-identical to a review.

        A template family makes every member a near-identical twin of the
        whole batch, so each member has many near-identical peers (batch
        counts are place-size independent). A genuinely unique review has
        few or none no matter how similar its closest neighbour is.
        """
        n = sim.shape[0]
        place = np.array(same_place, dtype=bool)
        signals: list[float] = []
        for i in range(n):
            peers = place.copy()
            peers[i] = False
            if not peers.any():
                signals.append(0.0)
                continue
            count = int(np.count_nonzero(sim[i, peers] >= self.config.peer_similarity_threshold))
            signals.append(
                _scale(count, self.config.peer_similarity_min_peers, self.config.peer_similarity_ceiling)
            )
        return signals

    def _phrase_reuse_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        """Token coverage of each review by content n-grams reused by the cohort.

        ``coverage`` is the fraction of the review's own content tokens that
        belong to a 2- or 3-gram shared (in identical wording) by at least
        ``K = phrase_reuse_min_peers`` distinct *other* reviews of the same
        place. The reuse bar is absolute so template detection does not fade
        as the place grows: a whole template batch reuses the skeleton among
        every member, while a phrase shared by a handful of organic reviews
        covers only a few tokens and stays below the coverage band.
        """
        token_seqs = [_content_tokens(r.text_or_empty()) for r in reviews]
        ngram_owners: dict[tuple[str, ...], int] = Counter()
        for seq in token_seqs:
            for g in _ngrams(seq):
                ngram_owners[g] += 1

        signals: list[float] = []
        k = self.config.phrase_reuse_min_peers
        for seq in token_seqs:
            if not seq:
                signals.append(0.0)
                continue
            covered_positions: set[int] = set()
            for start in range(len(seq) - 1):
                if ngram_owners[(seq[start], seq[start + 1])] - 1 >= k:
                    covered_positions.update((start, start + 1))
            for start in range(len(seq) - 2):
                key = (seq[start], seq[start + 1], seq[start + 2])
                if key in ngram_owners and ngram_owners[key] - 1 >= k:
                    covered_positions.update((start, start + 1, start + 2))
            coverage = len(covered_positions) / len(seq)
            signals.append(
                _scale(
                    coverage,
                    self.config.phrase_reuse_coverage_floor,
                    self.config.phrase_reuse_coverage_cap,
                )
            )
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
            # Near-identical sentence-length profiles only (signal ~0 for a
            # broadly similar but not identical structure).
            signals.append(_scale(float(top.mean()), 0.6, 0.95))
        return signals

    def _low_specificity_signal(self, reviews: list[NormalizedReview]) -> list[float]:
        from reviewscope.analysis.specificity import specificity_score

        signals: list[float] = []
        for r in reviews:
            spec = specificity_score(r.text_or_empty()).value
            signals.append(_scale(100.0 - spec, 50.0, 100.0))
        return signals

    def _low_unique_detail_signal(
        self, reviews: list[NormalizedReview], same_place: list[bool]
    ) -> list[float]:
        """Length-robust diversity: share of content words not reused by peers.

        A template family reuses the same content words across the whole
        batch (low unique detail); an organic specific review introduces
        words its peers never use (high unique detail). This is a *fraction*,
        not a raw type/token ratio, so text length does not distort it.
        """
        n = len(reviews)
        word_owners: dict[str, int] = Counter()
        per_review_words: list[set[str]] = []
        for r in reviews:
            words = set(_content_tokens(r.text_or_empty()))
            per_review_words.append(words)
            for w in words:
                word_owners[w] += 1
        signals: list[float] = []
        for i in range(n):
            words = per_review_words[i]
            if not words:
                signals.append(0.0)
                continue
            rare = sum(1 for w in words if word_owners[w] - 1 <= 2)
            unique_fraction = rare / len(words)
            signals.append(_scale(1.0 - unique_fraction, 0.0, 0.5))
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

    # -- main ----------------------------------------------------------------

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
        sim = self._similarity_matrix(reviews, embeddings)
        semantic = self._peer_similarity_signal(sim, same_place)
        phrase = self._phrase_reuse_signal(reviews)
        structure = self._structure_signal(reviews)
        generic = self._low_specificity_signal(reviews)
        unique_detail = self._low_unique_detail_signal(reviews, same_place)
        uniform = self._style_uniformity_signal(reviews)
        temporal = self._temporal_signal(reviews)

        results: list[ScoreResult] = []
        for i in range(n):
            signals: list[str] = []
            counter: list[str] = []
            sem, phr, strc, gen, uniq, uni, tmp = (
                semantic[i],
                phrase[i],
                structure[i],
                generic[i],
                unique_detail[i],
                uniform[i],
                temporal[i],
            )
            total = (
                w.phrase_reuse_weight * phr
                + w.peer_similarity_weight * sem
                + w.structure_weight * strc
                + w.low_specificity_weight * gen
                + w.low_unique_detail_weight * uniq
                + w.stylistic_uniformity_weight * uni
                + w.temporal_clustering_weight * tmp
            )
            score = round(100.0 * total, 1)
            active = sum(1 for v in (phr, sem, strc, gen, uniq, uni, tmp) if v >= 0.5)

            if sem >= 0.5:
                signals.append("high fraction of near-identical peer reviews")
            else:
                counter.append("textually distinct from peers")
            if phr >= 0.5:
                signals.append("repeated generic phrases across reviews")
            if strc >= 0.5:
                signals.append("high structural similarity with peers")
            if gen >= 0.5:
                signals.append("low specificity (generic language)")
            if uniq >= 0.5:
                signals.append("few unique details vs the review cohort")
            else:
                counter.append("high unique detail density")
            if uni >= 0.5:
                signals.append("stylistically uniform with group")
            if tmp >= 0.5:
                signals.append("reviews published in narrow time window")

            if score >= w.high_threshold and active >= w.min_signals_for_high:
                confidence = ConfidenceLevel.HIGH
            elif score >= w.medium_threshold:
                confidence = ConfidenceLevel.MEDIUM
            else:
                confidence = ConfidenceLevel.LOW

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
                        "unique_detail": round(uniq, 3),
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
