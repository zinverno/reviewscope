"""Semantic topic clustering with embeddings + HDBSCAN (SPEC.md §10).

Pipeline (SPEC §10):
    review text → multilingual embedding → HDBSCAN → semantic clusters

For each cluster we expose display metadata so the UI can render the SPEC
layout: cluster id, review count, average rating, date range, representative
phrases and similarity. Points HDBSCAN marks as noise stay unclustered.

Dimensionality
--------------
HDBSCAN works on sparse high-dimensional data poorly; the fixed multilingual
model produces 384-dim vectors. The config (SPEC.decision) allows optional
deterministic PCA reduction before HDBSCAN measured by cluster quality on the
demo dataset. When enabled the transformation is fit once on all embeddings so
clusters are reproducible across requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from reviewscope.config import CONFIG, TopicConfig
from reviewscope.models.review import NormalizedReview
from reviewscope.resources import EN_STOPWORDS, RU_STOPWORDS

_STOPWORDS = EN_STOPWORDS | RU_STOPWORDS


@dataclass
class TopicCluster:
    """One semantic cluster of reviews (SPEC.md §10 output block)."""

    cluster_id: int
    review_ids: list[str]
    place_ids: list[str]
    avg_rating: float
    date_min: str
    date_max: str
    similarity: float
    representative_phrases: list[str]
    representative_reviews: list[tuple[str, float]]
    signals: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)


def _token_keywords(text: str) -> list[str]:
    """Lower-cased non-stopword tokens, filtered to word-ish content."""
    import re

    tokens = re.findall(r"[а-яёa-z]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _representative_phrases(reviews: list[NormalizedReview], top_k: int = 5) -> list[str]:
    """Frequent content words across a cluster, used as indicative phrases.

    For an MVP this word-level heuristic is determinable and cheap (SPEC §9
    allows TF-IDF + n-grams + stopword filtering). Bigrams of adjacent content
    words approximate n-gram phrases.
    """
    from collections import Counter

    tok = Counter()
    bigram = Counter()
    for r in reviews:
        words = _token_keywords(r.text_or_empty())
        tok.update(words)
        for a, b in zip(words, words[1:], strict=False):
            bigram[(a, b)] += 1
    # Combine word/bigram frequencies; a phrase wins if its tokens appear often.
    ranked: list[tuple[float, str]] = []
    for (a, b), cnt in bigram.most_common(40):
        ranked.append((cnt, f"{a} {b}"))
    for w, cnt in tok.most_common(30):
        ranked.append((cnt, w))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    seen: set[str] = set()
    result: list[str] = []
    for _, phrase in ranked:
        if phrase in seen:
            continue
        seen.add(phrase)
        result.append(phrase)
        if len(result) >= top_k:
            break
    return result


class TopicClusterer:
    """Cluster reviews semantically with HDBSCAN over their embeddings."""

    def __init__(self, config: TopicConfig = CONFIG.topic) -> None:
        self.config = config
        self._pca: object | None = None

    def _embedded(
        self,
        reviews: list[NormalizedReview],
        embeddings: np.ndarray | None,
    ) -> np.ndarray | None:
        if embeddings is None:
            return None
        if embeddings.shape[0] != len(reviews):
            return None
        norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9)
        components = self.config.pca_components or norm.shape[1]
        components = max(2, min(components, norm.shape[1], norm.shape[0] - 1))
        if norm.shape[1] > components:
            from sklearn.decomposition import PCA

            pca = PCA(n_components=components, random_state=42)
            norm = pca.fit_transform(norm)
            self._pca = pca
        return norm

    def cluster(
        self,
        reviews: list[NormalizedReview],
        embeddings: np.ndarray | None = None,
    ) -> list[TopicCluster]:
        """Cluster reviews and return list of non-noise :class:`TopicCluster`."""
        if len(reviews) < self.config.min_cluster_size:
            return []
        emb = self._embedded(reviews, embeddings)
        if emb is None:
            return []

        # Cluster; treat noise (label -1) as ungrouped reviews.
        import hdbscan

        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=self.config.min_cluster_size,
            min_samples=self.config.min_samples,
            metric="euclidean",
        ).fit(emb)
        labels = clusterer.labels_
        clusters: dict[int, list[int]] = {}
        for idx, lab in enumerate(labels):
            if lab >= 0:
                clusters.setdefault(int(lab), []).append(idx)

        # Robustness: HDBSCAN labels a single ultra-tight, homogeneous blob as
        # noise (no internal distance structure). If nothing grouped but the
        # whole set is one coherent topic, surface it as a single cluster so
        # legitimate monolithic topics are not lost.
        if not clusters and _mean_cosine(emb) >= self.config.min_mean_similarity:
            clusters["all"] = list(range(len(reviews)))

        result: list[TopicCluster] = []
        for lab, members in clusters.items():
            ids = [reviews[i].review_id for i in members]
            place_ids = sorted({reviews[i].place_id for i in members})
            ratings = [reviews[i].rating for i in members if reviews[i].rating is not None]
            avg_rating = round(float(np.mean(ratings)), 2) if ratings else 0.0
            dates = [
                d
                for d in (
                    _safe_date(reviews[i].published_at) for i in members
                )
                if d is not None
            ]
            date_min = min(dates).isoformat() if dates else ""
            date_max = max(dates).isoformat() if dates else ""
            # Internal similarity = mean pairwise cosine of cluster embeddings.
            cluster_emb = emb[members]
            sim = _mean_cosine(cluster_emb)
            phrases = _representative_phrases([reviews[i] for i in members])
            reps = sorted(
                ((reviews[i].review_id, _mean_cosine_to_center(cluster_emb, k)) for k, i in enumerate(members)),
                key=lambda x: x[1],
                reverse=True,
            )[: self.config.representative_reviews]
            result.append(
                TopicCluster(
                    cluster_id=lab,
                    review_ids=ids,
                    place_ids=place_ids,
                    avg_rating=avg_rating,
                    date_min=date_min,
                    date_max=date_max,
                    similarity=round(sim, 3),
                    representative_phrases=phrases,
                    representative_reviews=[(rid, round(sc, 3)) for rid, sc in reps],
                )
            )
        result.sort(key=lambda c: -len(c.review_ids))
        return result


def _safe_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _mean_cosine(emb: np.ndarray) -> float:
    if len(emb) < 2:
        return 1.0
    norm = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)
    sim = norm @ norm.T
    n = sim.shape[0]
    return float((np.triu(sim, 1).sum()) / (n * (n - 1) / 2))


def _mean_cosine_to_center(emb: np.ndarray, index: int) -> float:
    norm = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)
    center = norm.mean(axis=0)
    return float(norm[index] @ center / (np.linalg.norm(center) + 1e-9))


def clusters_frame(clusters: list[TopicCluster]) -> pd.DataFrame:
    """Render clusters into a DataFrame for the UI."""
    return pd.DataFrame(
        [
            {
                "cluster_id": c.cluster_id,
                "reviews": len(c.review_ids),
                "avg_rating": c.avg_rating,
                "date_min": c.date_min,
                "date_max": c.date_max,
                "similarity": c.similarity,
                "phrases": "; ".join(c.representative_phrases),
            }
            for c in clusters
        ]
    )
