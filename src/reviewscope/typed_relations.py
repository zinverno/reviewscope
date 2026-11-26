"""Bounded offline pair selection. A candidate channel never asserts an aspect relation."""

from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from math import comb

from rapidfuzz.fuzz import ratio

MAX_PAIRS = 20_000


def lexical_candidates(documents):
    if len(documents) > 10_000:
        raise ValueError("VERSION_LIMIT")
    sizes = [len(t.encode()) for t in documents.values()]
    if sum(sizes) > 4 * 1024**2 or max(sizes, default=0) > 4096:
        raise ValueError("TEXT_BYTE_LIMIT")
    docs = {i: " ".join(t.lower().split()) for i, t in sorted(documents.items()) if t.strip()}
    buckets = defaultdict(list)
    for i, text in docs.items():
        buckets[text].append(i)
    exact = sum(comb(len(ids), 2) for ids in buckets.values())
    summary = dict(
        eligible_review_count=len(docs),
        excluded_blank_count=len(documents) - len(docs),
        total_possible_pairs=comb(len(docs), 2),
        exact_possible_pairs=exact,
        budget=MAX_PAIRS,
        dense="not_evaluated",
    )
    if exact > MAX_PAIRS:
        return {}, summary | dict(
            status="PAIR_BUDGET_LIMIT", compared=0, candidate_pairs_count=None
        )
    ordered = sorted(docs, key=lambda i: (docs[i], i))
    exhaustive = len(ordered) <= 128
    proposed = (
        combinations(ordered, 2)
        if exhaustive
        else ((a, b) for pos, a in enumerate(ordered) for b in ordered[pos + 1 : pos + 3])
    )
    pairs = {p: {"exact": 1.0} for group in buckets.values() for p in combinations(group, 2)}
    compared = 0
    for a, b in proposed:
        compared += 1
        score = ratio(docs[a], docs[b]) / 100
        if score >= 0.65:
            pairs.setdefault(tuple(sorted((a, b))), {})["fuzzy"] = score
            if len(pairs) > MAX_PAIRS:
                return {}, summary | dict(
                    status="PAIR_BUDGET_LIMIT", compared=compared, candidate_pairs_count=None
                )
    return pairs, summary | dict(
        status="complete",
        blocking="all" if exhaustive else "lexical-sort-next-2",
        compared=compared,
        candidate_pairs_count=len(pairs),
    )


def neighbors(ids, matrix, threshold, k=4):
    """Exact directional top-K on the small benchmark only; preserve both ranks."""
    import numpy as np

    if len(ids) > 128 or matrix.shape != (len(ids), len(ids)) or not np.isfinite(matrix).all():
        raise ValueError("INVALID_BOUNDED_MATRIX")
    pairs = {}
    for i, a in enumerate(ids):
        ranked = sorted(
            (j for j in range(len(ids)) if i != j), key=lambda j: (-matrix[i, j], ids[j])
        )
        for rank, j in enumerate(ranked[:k], 1):
            if matrix[i, j] >= threshold:
                pair = tuple(sorted((a, ids[j])))
                pairs.setdefault(pair, {})[a] = {
                    "rank": rank,
                    "score": round(float(matrix[i, j]), 7),
                }
    return pairs


def text_relation(a, b):
    if not a.strip() or not b.strip():
        return "not_evaluated", None
    if a.lower().split() == b.lower().split():
        return "exact_text", [[0, len(a)], [0, len(b)]]
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
    if ratio(" ".join(a.lower().split()), " ".join(b.lower().split())) >= 85 and m.size >= 12:
        return "near_copy", [[m.a, m.a + m.size], [m.b, m.b + m.size]]
    return "no_relation", None
