#!/usr/bin/env python3
"""Capture pipeline metrics to a JSON file for remediation reports.

Runs the full analysis over the demo corpus and reports the per-place and
aggregate signals that the forensic audit measured (SPEC.md §7 dataset):

* templated scores split by injected-suspicious vs organic reviews;
* review-weight distribution including upper/lower clamp reachability;
* coordinated score, bursts, duplicate groups and clusters;
* the local-familiarity city/region defect reproduction.

Usage::

    python scripts/metrics_capture.py [output.json]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.analysis.engine import AnalysisEngine  # noqa: E402
from reviewscope.analysis.reviewer import local_familiarity_score  # noqa: E402
from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402
from reviewscope.models.review import NormalizedReview  # noqa: E402
from reviewscope.storage import DuckDBStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEMO_CSV = ROOT / "data" / "demo_reviews.csv"


def _stats(seq: list[float]) -> dict:
    if not seq:
        return {}
    seq_s = sorted(seq)
    n = len(seq_s)

    def pct(p: float) -> float:
        return seq_s[min(n - 1, int(n * p))]

    return {
        "min": round(seq_s[0], 2),
        "p25": round(pct(0.25), 2),
        "median": round(seq_s[n // 2], 2),
        "mean": round(sum(seq_s) / n, 2),
        "p75": round(pct(0.75), 2),
        "p95": round(pct(0.95), 2),
        "max": round(seq_s[-1], 2),
        "n": n,
    }


def main() -> int:
    reviews_all = CSVAdapter().load(DEMO_CSV).reviews
    org_by_place: dict[str, list[NormalizedReview]] = defaultdict(list)
    for r in reviews_all:
        org_by_place[r.place_id].append(r)

    suspicious_ids = {
        r.review_id for r in reviews_all
        if r.review_id.startswith(("rbp", "rbn", "rdup"))
    }

    store = DuckDBStore()
    store.ingest(reviews_all)
    engine = AnalysisEngine(store, use_embeddings=True)

    out: dict = {"places": {}}
    all_org: list[float] = []
    all_susp: list[float] = []

    for place_id in sorted(org_by_place):
        ap = engine.analyze(place_id)
        org_scores = []
        susp_scores = []
        for r, res in zip(ap.reviews, ap.templated_scores, strict=False):
            if r.review_id in suspicious_ids:
                susp_scores.append(res.value)
                all_susp.append(res.value)
            else:
                org_scores.append(res.value)
                all_org.append(res.value)

        w = ap.review_weights
        w_sorted = sorted(w)
        nw = len(w_sorted)

        def wpct(p: float, w_sorted: list[float] = w_sorted, nw: int = nw) -> float:
            return w_sorted[min(nw - 1, int(nw * p))]

        out["places"][place_id] = {
            "n": len(ap.reviews),
            "raw_rating": ap.raw_rating,
            "weighted_rating": ap.weighted_rating_value,
            "coordinated": ap.coordinated.value if ap.coordinated else None,
            "coord_conf": ap.coordinated.confidence.value if ap.coordinated else None,
            "burst_events": [
                (e.date.isoformat(), e.observed, round(e.multiplier, 2), e.severity.value)
                for e in ap.burst_events
            ],
            "dup_groups": [
                (len(g.review_ids), g.exact_count, round(g.avg_similarity, 3))
                for g in ap.duplicate_groups
            ],
            "clusters": [(len(c.review_ids), round(c.similarity, 3)) for c in ap.clusters],
            "weight_min": round(w_sorted[0], 4),
            "weight_p25": round(wpct(0.25), 4),
            "weight_median": round(w_sorted[nw // 2], 4),
            "weight_mean": round(sum(w) / nw, 4),
            "weight_p75": round(wpct(0.75), 4),
            "weight_p95": round(wpct(0.95), 4),
            "weight_max": round(w_sorted[-1], 4),
            "pct_at_lower_clamp": round(100.0 * sum(1 for x in w if x <= 0.25 + 1e-6) / nw, 2),
            "pct_at_upper_clamp": round(100.0 * sum(1 for x in w if x >= 2.0 - 1e-6) / nw, 2),
            "pct_above_1": round(100.0 * sum(1 for x in w if x > 1.0) / nw, 2),
            "templated_organic": _stats(org_scores),
            "templated_suspicious": _stats(susp_scores),
        }

    out["aggregate_organic_templated"] = _stats(all_org)
    out["aggregate_suspicious_templated"] = _stats(all_susp)

    def mk_review(rid: str, city: str, region: str) -> NormalizedReview:
        return NormalizedReview(
            review_id=rid, place_id="pX", place_category="coffee",
            reviewer_id="u", rating=5, text="text", published_at="2026-01-01",
            city=city, region=region,
        )

    history = [
        mk_review("a", "Санкт-Петербург", "Московская область"),
        mk_review("b", "Казань", "Московская область"),
        mk_review("c", "Новосибирск", "Московская область"),
    ]
    lf = local_familiarity_score(history, "Москва", "Московская область")
    out["local_familiarity_same_region_diff_city"] = (lf.value, lf.signals, lf.counter_signals)

    history2 = [mk_review(x, "Москва", "Московская область") for x in "abc"]
    lf2 = local_familiarity_score(history2, "Москва", "Московская область")
    out["local_familiarity_same_city"] = (lf2.value, lf2.signals, lf2.counter_signals)

    store.close()

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/metrics_after.json")
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
