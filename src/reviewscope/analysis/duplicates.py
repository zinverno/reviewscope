"""Duplicate detection (SPEC.md §11, §33).

Four detection levels:

* **Exact** — normalized text equality.
* **Fuzzy** — RapidFuzz ratio above a configurable threshold.
* **Near-duplicate** — MinHash/LSH candidate generation on character n-grams,
  refined with RapidFuzz scoring.
* **Semantic** — embedding cosine similarity above a configurable threshold.

For datasets below ``brute_force_max_reviews`` the near-duplicate MinHash step
is skipped and all pairs are compared directly via RapidFuzz.  For larger
datasets candidate generation is mandatory (SPEC.md §33).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import rapidfuzz
from datasketch import MinHash, MinHashLSH

from reviewscope.config import CONFIG, DuplicateConfig
from reviewscope.models.review import NormalizedReview

# ---------------------------------------------------------------------------
# Union-Find for merging pairs into groups
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        r = x
        while self._parent[r] != r:
            r = self._parent[r]
        while x != r:
            nxt = self._parent[x]
            self._parent[x] = r
            x = nxt
        return r

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class DuplicateGroup:
    """A connected group of near-identical reviews."""

    group_id: int
    review_ids: list[str]
    exact_count: int = 0
    fuzzy_count: int = 0
    near_count: int = 0
    semantic_count: int = 0
    avg_similarity: float = 0.0
    signals: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)


def _normalize_text(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _rapid_score(text_a: str, text_b: str) -> float:
    """RapidFuzz similarity as a fraction in [0, 1].

    RapidFuzz v3 ``fuzz.ratio`` returns a percentage (0..100); we normalize to
    a fraction so it consistently compares against config thresholds and the
    semantic (cosine) scores.
    """
    return rapidfuzz.fuzz.ratio(text_a, text_b) / 100.0


# ---------------------------------------------------------------------------
# Duplicate detector
# ---------------------------------------------------------------------------


class DuplicateDetector:
    """Full four-level duplicate detection pipeline."""

    def __init__(self, config: DuplicateConfig = CONFIG.duplicate) -> None:
        self.config = config

    # -- exact ---------------------------------------------------------------

    def _exact_duplicates(
        self, reviews: list[NormalizedReview]
    ) -> dict[int, list[int]]:
        """Return a mapping ``hash_text → [indices]`` for exact matches."""
        buckets: dict[str, list[int]] = {}
        for idx, r in enumerate(reviews):
            key = _normalize_text(r.text)
            buckets.setdefault(key, []).append(idx)
        return {i: ids for i, ids in enumerate(buckets.values()) if len(ids) > 1}

    # -- MinHash candidate generation -----------------------------------------

    def _make_minhash(self, text: str) -> MinHash:
        h = MinHash(num_perm=self.config.minhash_num_perm)
        normalized = _normalize_text(text)
        for start in range(0, max(len(normalized) - self.config.near_char_ngram + 1, 1)):
            ngram = normalized[start : start + self.config.near_char_ngram]
            h.update(ngram.encode("utf-8"))
        return h

    def _minhash_candidates(
        self, reviews: list[NormalizedReview]
    ) -> set[tuple[int, int]]:
        """Build the LSH and return candidate index-pairs via MinHash banding."""
        lsh = MinHashLSH(
            threshold=self.config.minhash_threshold,
            num_perm=self.config.minhash_num_perm,
        )
        key_to_index: dict[str, int] = {}
        signature_map: dict[str, MinHash] = {}
        for idx, r in enumerate(reviews):
            text = r.text or ""
            if len(text.strip()) < self.config.near_char_ngram:
                continue
            h = self._make_minhash(text)
            key = f"r{idx}"
            try:
                lsh.insert(key, h)
                key_to_index[key] = idx
                signature_map[key] = h
            except ValueError:
                continue
        pairs: set[tuple[int, int]] = set()
        for key, h in signature_map.items():
            for rkey in lsh.query(h):
                j = key_to_index.get(rkey)
                i = key_to_index[key]
                if j is not None and i < j:
                    pairs.add((i, j))
        return pairs

    # -- fuzzy + near refinement ---------------------------------------------

    def _compute_pair_score(self, t_a: str, t_b: str) -> float:
        return _rapid_score(t_a, t_b)

    def _refine_candidates(
        self,
        reviews: list[NormalizedReview],
        candidate_pairs: set[tuple[int, int]],
        exact_indices: set[int],
    ) -> list[tuple[int, int, float, str]]:
        """Score candidate pairs with RapidFuzz and classify them."""
        scores: list[tuple[int, int, float, str]] = []
        texts = [_normalize_text(r.text) for r in reviews]
        for i, j in candidate_pairs:
            if i in exact_indices and j in exact_indices:
                continue
            score = self._compute_pair_score(texts[i], texts[j])
            if score >= self.config.fuzzy_threshold:
                scores.append((i, j, score, "near"))
            elif score >= self.config.fuzzy_group_threshold:
                scores.append((i, j, score, "fuzzy"))
        return scores

    def _brute_force_fuzzy(
        self,
        reviews: list[NormalizedReview],
        exact_indices: set[int],
    ) -> list[tuple[int, int, float, str]]:
        """Brute-force pair-wise RapidFuzz for small datasets."""
        texts = [_normalize_text(r.text) for r in reviews]
        scores: list[tuple[int, int, float, str]] = []
        n = len(reviews)
        for i in range(n):
            for j in range(i + 1, n):
                if i in exact_indices and j in exact_indices:
                    continue
                score = _rapid_score(texts[i], texts[j])
                if score >= self.config.fuzzy_threshold:
                    scores.append((i, j, score, "near"))
                elif score >= self.config.fuzzy_group_threshold:
                    scores.append((i, j, score, "fuzzy"))
        return scores

    # -- semantic duplicates -------------------------------------------------

    def _semantic_duplicates(
        self,
        embeddings: np.ndarray,
        reviews: list[NormalizedReview],
    ) -> list[tuple[int, int, float, str]]:
        n = len(reviews)
        if n == 0:
            return []
        if embeddings.shape[0] != n:
            return []
        # Normalize rows for cosine similarity via dot product.
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms < 1e-9] = 1.0
        normed = embeddings / norms
        pairs: list[tuple[int, int, float, str]] = []
        if n <= self.config.brute_force_max_reviews:
            sim = normed @ normed.T
            for i in range(n):
                for j in range(i + 1, n):
                    if sim[i, j] >= self.config.semantic_threshold:
                        pairs.append((i, j, float(sim[i, j]), "semantic"))
        else:
            from sklearn.neighbors import NearestNeighbors

            nn = NearestNeighbors(
                n_neighbors=self.config.candidate_max_topk,
                metric="cosine",
                algorithm="brute",
            )
            nn.fit(normed)
            _, indices = nn.kneighbors(normed)
            for i in range(n):
                for j_idx in range(1, indices.shape[1]):
                    j = int(indices[i, j_idx])
                    if j <= i:
                        continue
                    cosine_val = float(np.dot(normed[i], normed[j]))
                    if cosine_val >= self.config.semantic_threshold:
                        pairs.append((i, j, cosine_val, "semantic"))
        return pairs

    # -- main pipeline -------------------------------------------------------

    def detect(
        self,
        reviews: list[NormalizedReview],
        embeddings: np.ndarray | None = None,
    ) -> list[DuplicateGroup]:
        """Run all four duplicate levels and return grouped results."""
        if len(reviews) < 2:
            return []

        # 1. Exact duplicates
        exact_buckets = self._exact_duplicates(reviews)
        uf = _UnionFind(len(reviews))
        exact_members: set[int] = set()
        exact_pairs: dict[tuple[int, int], float] = {}
        for indices in exact_buckets.values():
            exact_members.update(indices)
            for a in indices:
                for b in indices:
                    if a < b:
                        uf.union(a, b)
                        exact_pairs[(a, b)] = 1.0

        # 2. Candidate generation
        if len(reviews) <= self.config.brute_force_max_reviews:
            fuzzy_scores = self._brute_force_fuzzy(reviews, exact_members)
        else:
            cand_pairs = self._minhash_candidates(reviews)
            fuzzy_scores = self._refine_candidates(reviews, cand_pairs, exact_members)

        # 3. Semantic duplicates
        semantic_scores: list[tuple[int, int, float, str]] = []
        if embeddings is not None and embeddings.shape[0] == len(reviews):
            semantic_scores = self._semantic_duplicates(embeddings, reviews)

        # 4. Merge pairs into groups via Union-Find
        all_pairs = fuzzy_scores + semantic_scores
        pair_scores: dict[tuple[int, int], float] = {}
        pair_types: dict[tuple[int, int], str] = {}
        for i, j, score, kind in all_pairs:
            uf.union(i, j)
            key = (min(i, j), max(i, j))
            if key not in pair_scores or score > pair_scores[key]:
                pair_scores[key] = score
                pair_types[key] = kind
        # Exact pairs always take precedence (they are the strongest signal).
        for key, score in exact_pairs.items():
            uf.union(*key)
            if key not in pair_scores or score >= pair_scores[key]:
                pair_scores[key] = score
                pair_types[key] = "exact"

        # 5. Build groups
        components: dict[int, list[int]] = {}
        for idx in range(len(reviews)):
            root = uf.find(idx)
            components.setdefault(root, []).append(idx)

        groups: list[DuplicateGroup] = []
        gid = 0
        for _, members in components.items():
            if len(members) < 2:
                continue
            gid += 1
            review_ids = [reviews[i].review_id for i in members]
            exact = sum(1 for i in members if i in exact_members)
            fuzzy = 0
            near = 0
            semantic = 0
            exact_pairs_in_group = 0
            pair_sims: list[float] = []
            for a in members:
                for b in members:
                    if a >= b:
                        continue
                    key = (a, b)
                    if key not in pair_types:
                        continue
                    kind = pair_types[key]
                    if kind == "exact":
                        exact_pairs_in_group += 1
                    elif kind == "fuzzy":
                        fuzzy += 1
                    elif kind == "near":
                        near += 1
                    elif kind == "semantic":
                        semantic += 1
                    pair_sims.append(pair_scores[key])
            avg = float(np.mean(pair_sims)) if pair_sims else 0.0
            signals = []
            if exact >= 2:
                signals.append(f"{exact} exact duplicates")
            if near + fuzzy > 0:
                signals.append(f"{near + fuzzy} fuzzy/near-duplicate pairs")
            if semantic > 0:
                signals.append(f"{semantic} semantic matches")
            groups.append(
                DuplicateGroup(
                    group_id=gid,
                    review_ids=review_ids,
                    exact_count=exact,
                    fuzzy_count=fuzzy,
                    near_count=near,
                    semantic_count=semantic,
                    avg_similarity=round(avg, 4),
                    signals=signals,
                )
            )
        groups.sort(key=lambda g: (len(g.review_ids), g.avg_similarity), reverse=True)
        return groups
