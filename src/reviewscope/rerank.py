"""Reorder a closed, integrity-checked pool; retain the parent as the evidence record."""

from math import isfinite

from reviewscope.candidates import text_sha
from reviewscope.embeddings.rerank import PROFILE, CrossEncoder


def reorder(parent, docs, query, scorer=None, *, limit=40, profile=PROFILE):
    if profile != PROFILE:
        raise ValueError("INVALID_PROFILE")
    hits = parent["candidates"]
    if parent["query_sha256"] != text_sha(query) or parent["candidate_count"] != len(hits):
        raise ValueError("INVALID_PARENT")
    if len(hits) > limit:
        raise ValueError("RESOURCE_LIMIT")
    by_id = {h["fragment_id"]: h for h in hits}
    if len(by_id) != len(hits):
        raise ValueError("INVALID_PARENT")
    windows = []
    for fid, h in by_id.items():
        record = docs.get(h["version_id"])
        if (
            record is None
            or record.content_sha256 != h["content_sha256"]
            or text_sha(record.text) != h["text_sha256"]
        ):
            raise ValueError("INVALID_PARENT")
        a, b, c, d = (h[k] for k in ("start", "end", "context_start", "context_end"))
        if not 0 <= c <= a < b <= d <= len(record.text):
            raise ValueError("INVALID_PARENT")
        windows.append((fid, record.text[a:b], record.text[c:d]))
    if scorer is None:
        scorer = CrossEncoder()
    scores = scorer.score(query, windows)
    if scores.keys() != by_id.keys() or not all(
        isinstance(v, float) and isfinite(v) for v in scores.values()
    ):
        raise ValueError("INCOMPATIBLE_MODEL")
    reviews = {}
    ranked = []
    for i, fid in enumerate(sorted(scores, key=lambda f: (-scores[f], f)), 1):
        h = by_id[fid]
        ranked.append(
            dict(
                fragment_id=fid,
                pre_rank=h["rank"],
                rank=i,
                score=scores[fid],
                review_rank=reviews.setdefault(h["version_id"], len(reviews) + 1),
            )
        )
    return ranked
