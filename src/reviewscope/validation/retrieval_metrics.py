"""Candidate ranking metrics with explicit eligibility and unjudged pairs."""

from collections import Counter, defaultdict
from math import log2

from reviewscope.validation.retrieval_dataset import Judgment, Query


def dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / log2(rank + 1) for rank, grade in enumerate(grades, start=1))


def evaluate_query(ranked: list[str], labels: dict[str, Judgment], cutoffs: list[int]) -> dict:
    """Rank covers the whole selected corpus. Missing and explicit null pairs are unknown."""
    known = {rid: j for rid in ranked if (j := labels.get(rid)) and j.relevance is not None}
    exact = {rid for rid, j in known.items() if j.relevance == 2}
    unknown = len(ranked) - len(known)
    ideal = sorted((j.relevance for j in known.values()), reverse=True)
    by_k = {}
    for k in cutoffs:
        if not 1 <= k <= len(ranked):
            raise ValueError("K outside ranked corpus")
        top = ranked[:k]
        hits = [rank for rank, rid in enumerate(top, 1) if rid in exact]
        metrics = None
        if not unknown and exact:
            metrics = {
                "recall": len(hits) / len(exact),
                "reciprocal_rank": 1 / hits[0] if hits else 0,
                "ndcg": dcg([known[rid].relevance for rid in top]) / dcg(ideal[:k]),
            }
        fp = [rid for rid in top if rid in known and rid not in exact]
        by_k[str(k)] = {
            "metrics": metrics,
            "unjudged_in_top": sum(rid not in known for rid in top),
            "known_false_positives": fp,
            "known_false_negatives": sorted(exact.difference(top)),
            "fp_by_reason": dict(
                Counter(
                    ("off_topic" if known[rid].relevance == 0 else "topic_only")
                    if known[rid].stance == "not_applicable"
                    else known[rid].stance
                    for rid in fp
                )
            ),
            "mixed_exact_retrieved": sum(
                rid in top and known[rid].stance == "mixed" for rid in exact
            ),
            "mixed_exact_missed": sum(
                rid not in top and known[rid].stance == "mixed" for rid in exact
            ),
        }
    return {
        "known_exact": len(exact),
        "known_partial": ideal.count(1),
        "unknown": unknown,
        "incomplete_judgments": bool(unknown),
        "at_k": by_k,
    }


def summarize(queries: list[Query], results: dict) -> dict:
    groups = defaultdict(list)
    for q in queries:
        groups[q.mode].append(results[q.query_id])
    summary = {}
    for mode, rows in groups.items():
        eligible = [r for r in rows if not r["unknown"] and r["known_exact"]]
        macro = {}
        for k in rows[0]["at_k"]:
            if eligible:
                macro[k] = {
                    name: sum(r["at_k"][k]["metrics"][metric] for r in eligible) / len(eligible)
                    for name, metric in (
                        ("recall", "recall"),
                        ("mrr", "reciprocal_rank"),
                        ("ndcg", "ndcg"),
                    )
                }
        summary[mode] = {
            "queries": len(rows),
            "with_known_exact": sum(r["known_exact"] > 0 for r in rows),
            "fully_judged": sum(not r["unknown"] for r in rows),
            "incomplete_judgments": sum(bool(r["unknown"]) for r in rows),
            "eligible_queries": len(eligible),
            "macro_on_eligible_only": macro or None,
        }
    return summary
