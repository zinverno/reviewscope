#!/usr/bin/env python3
"""Benchmark the ReviewScope pipeline on the exploratory corpus.

Measures, on the corpus produced by ``prepare_yandex_geo_exploration.py``:

* CSVAdapter load time and import statistics;
* DuckDB ingest time and on-disk database size;
* cold embedding bootstrap time (fresh engine, empty cache) and the number of
  texts actually encoded;
* the FULL ``AnalysisEngine.analyze`` pass attempt — this corpus has no
  per-review timestamps, which triggers a pre-existing production edge case in
  ``BurstDetector._filled_daily_counts`` (``pd.concat`` of an empty list for a
  place with zero dated reviews). The attempt is recorded verbatim; the
  production pipeline is NOT modified;
* a best-effort partial pass over the same production phase classes with the
  burst/anomaly phase omitted (duplicate detection, templated scoring, topic
  clustering, weights, weighted rating, keywords, reviewer metrics) so the
  non-temporal pipeline performance is still measured end to end;
* a cache-warm rerun against a fresh store/engine where every embedding comes
  from the persisted DuckDB cache (encode counts and hit ratios are recorded
  so re-encode behaviour is not hidden).

End-to-end results are aggregate diagnostics only — no review texts are
printed. Writes ``exploration_benchmark.json`` next to the corpus.

Usage::

    python scripts/benchmark_exploration.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.analysis.duplicates import DuplicateDetector  # noqa: E402
from reviewscope.analysis.engine import AnalysisEngine  # noqa: E402
from reviewscope.analysis.keywords import (  # noqa: E402
    emerging_keywords,
    extract_keywords,
    keywords_by_sentiment,
)
from reviewscope.analysis.reviewer import (  # noqa: E402
    category_experience_score,
    reviewer_relevance_score,
)
from reviewscope.analysis.scoring import (  # noqa: E402
    compute_review_weights,
    coordinated_activity_score,
    coordinated_review_probabilities,
    weighted_rating,
)
from reviewscope.analysis.specificity import specificity_score  # noqa: E402
from reviewscope.analysis.templated import TemplatedTextScorer  # noqa: E402
from reviewscope.analysis.topics import TopicClusterer  # noqa: E402
from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402
from reviewscope.models.review import NormalizedReview  # noqa: E402
from reviewscope.storage import DuckDBStore  # noqa: E402

OUT_DIR = Path("validation_data/private/yandex_geo_2023/exploration")
CSV_PATH = OUT_DIR / "exploration_reviews.csv"
DB_PATH = OUT_DIR / "exploration_analysis.duckdb"
REPORT_PATH = OUT_DIR / "exploration_benchmark.json"

CLOCK_START = time.perf_counter()


class Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self.start


def _secs(t: Timer) -> float:
    return round(t.elapsed(), 3)


def _num_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    arr = np.array(values, dtype=float)
    return {
        "min": round(float(arr.min()), 2),
        "p25": round(float(np.percentile(arr, 25)), 2),
        "median": round(float(np.percentile(arr, 50)), 2),
        "mean": round(float(arr.mean()), 2),
        "p75": round(float(np.percentile(arr, 75)), 2),
        "max": round(float(arr.max()), 2),
    }


def _place_embeddings(
    engine: AnalysisEngine, reviews: list[NormalizedReview]
) -> np.ndarray | None:
    """Subset the engine's full-embedding matrix to one place (read-only)."""
    all_reviews, all_emb = engine._all()
    if all_emb is None:
        return None
    index = {r.review_id: i for i, r in enumerate(all_reviews)}
    idx = [index[r.review_id] for r in reviews]
    return all_emb[np.array(idx)]


def _partial_pass(
    engine: AnalysisEngine,
    place_ids: list[str],
) -> tuple[dict, list[str]]:
    """Best-effort pass over the non-temporal production phases.

    Replicates ``AnalysisEngine.analyze`` but skips the burst/anomaly phase
    (which cannot run on a timestamp-less corpus). No production code is
    modified; the same production phase classes and scorers are used.
    """
    store = engine._store
    all_reviews, _ = engine._all()
    history_by_reviewer: dict[str, list[NormalizedReview]] = {}
    for r in all_reviews:
        history_by_reviewer.setdefault(r.reviewer_id, []).append(r)

    agg: dict = {
        "places": 0,
        "reviews": 0,
        "duplicate_groups": 0,
        "clusters": 0,
        "templated_ge_65": 0,
        "weights": [],
        "coordinated_scores": [],
        "coordinated_signals": 0,
        "raw_rating": [],
        "weighted_rating": [],
        "burst_events": 0,
        "rating_anomalies": 0,
    }
    errors: list[str] = []
    for pid in place_ids:
        try:
            reviews = store.fetch_reviews(place_id=pid)
            embeddings = _place_embeddings(engine, reviews)
            clusters = TopicClusterer().cluster(reviews, embeddings)
            dup_groups = DuplicateDetector().detect(reviews, embeddings=embeddings)
            templated = TemplatedTextScorer().score(reviews, embeddings)
            coordinated = coordinated_activity_score(
                reviews,
                burst_events=[],
                rating_anomalies=[],
                clusters=clusters,
                dup_groups=dup_groups,
                templated_results=templated,
            )
            specs = {r.review_id: specificity_score(r.text_or_empty()).value for r in reviews}
            coord_prob = coordinated_review_probabilities(
                reviews,
                burst_events=[],
                rating_anomalies=[],
                dup_groups=dup_groups,
                templated_results=templated,
                clusters=clusters,
            )
            cat_map: dict[str, float] = {}
            rel_map: dict[str, float] = {}
            for r in reviews:
                history = history_by_reviewer.get(r.reviewer_id, [])
                cat_map[r.review_id] = category_experience_score(history, r.place_category).value
                rel_map[r.review_id] = reviewer_relevance_score(
                    history, r.place_category, r.city, r.region
                ).value
            dup_prob: dict[str, float] = {}
            for g in dup_groups:
                if len(g.review_ids) >= 3:
                    avg_sim = min(
                        1.0,
                        max(
                            getattr(g, "mean_similarity", getattr(g, "avg_similarity", 0.0))
                            or 0.0,
                            0.5,
                        ),
                    )
                    prob = round(min(1.0, len(g.review_ids) / 5.0) * avg_sim, 3)
                    for rid in g.review_ids:
                        dup_prob[rid] = max(dup_prob.get(rid, 0.0), prob)
            weights = compute_review_weights(
                reviews,
                specificity_map=specs,
                category_experience_map=cat_map,
                reviewer_relevance_map=rel_map,
                duplicate_probability=dup_prob,
                templated_probability={
                    reviews[i].review_id: round(min(1.0, res.value / 100.0), 3)
                    for i, res in enumerate(templated)
                },
                coordinated_probability=coord_prob,
            )
            raw, weighted, _ = weighted_rating(reviews, weights)
            extract_keywords(reviews, top_n=15)
            keywords_by_sentiment(reviews, top_n=10)
            emerging_keywords(reviews)

            agg["places"] += 1
            agg["reviews"] += len(reviews)
            agg["duplicate_groups"] += len(dup_groups)
            agg["clusters"] += len(clusters)
            agg["templated_ge_65"] += sum(1 for s in templated if s.value >= 65)
            agg["weights"].extend(weights)
            if coordinated is not None:
                agg["coordinated_scores"].append(coordinated.value)
                agg["coordinated_signals"] += len(coordinated.signals)
            agg["raw_rating"].append(raw)
            agg["weighted_rating"].append(weighted)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{pid}: {type(exc).__name__}: {exc}")
    agg["weight_stats"] = _num_stats(agg["weights"])
    agg["coordinated_stats"] = _num_stats(agg["coordinated_scores"])
    return agg, errors


def main() -> int:
    report: dict = {}

    if not CSV_PATH.is_file():
        print(f"corpus not found: {CSV_PATH} (run prepare_yandex_geo_exploration.py first)")
        return 1

    # --- 1. CSVAdapter load -------------------------------------------------
    t = Timer()
    result = CSVAdapter().load(CSV_PATH)
    load_time = _secs(t)
    report["import"] = {
        "total_rows": result.report.total_rows,
        "valid": result.report.valid,
        "skipped": result.report.skipped,
        "warnings": result.report.warning_count,
        "load_time_s": load_time,
    }
    reviews = result.reviews
    print(f"CSVAdapter: imported={result.report.total_rows} valid={result.report.valid} "
          f"skipped={result.report.skipped} warnings={result.report.warning_count} "
          f"[{load_time:.2f}s]")

    # --- 2. rebuild fresh DuckDB store + ingest ------------------------------
    if DB_PATH.exists():
        DB_PATH.unlink()
    store = DuckDBStore(db_path=DB_PATH)
    t = Timer()
    written = store.ingest(reviews)
    ingest_time = _secs(t)
    report["ingest"] = {"rows_written": written, "ingest_time_s": ingest_time}
    print(f"ingest: rows={written} [{ingest_time:.2f}s]")

    # --- 3. cold embedding bootstrap -----------------------------------------
    engine = AnalysisEngine(store, use_embeddings=True)
    warm_cache = engine._embedding_cache
    t = Timer()
    all_reviews, all_emb = engine._all()
    embed_time = _secs(t)
    report["embeddings"] = {
        "model_name": warm_cache.provider_name,
        "reviews": len(all_reviews),
        "encoded_cold": store.cached_embedding_count(),
        "embed_time_s": embed_time,
    }
    print(f"embeddings: cold encode [{embed_time:.2f}s]")

    place_ids = store.list_places()["place_id"].tolist()

    # --- 4. full AnalysisEngine.analyze attempt (production pipeline) --------
    t = Timer()
    full_pass_errors: list[str] = []
    try:
        engine.analyze(place_ids[0])
    except Exception as exc:  # noqa: BLE001
        full_pass_errors.append(f"{type(exc).__name__}: {exc}")
    full_attempt_time = _secs(t)
    report["analysis_full_pass"] = {
        "attempted_places": 1,
        "ok": not full_pass_errors,
        "errors": full_pass_errors,
        "time_s": full_attempt_time,
        "blocked_by": (
            "BurstDetector.detect -> _filled_daily_counts raises "
            "'No objects to concatenate' when a place has zero dated reviews "
            "(pd.concat of an empty list, src/reviewscope/analysis/bursts.py). "
            "Every review in this corpus has an empty published_at, so the "
            "production pipeline cannot analyse any place here. The production "
            "analytics are intentionally NOT modified."
        ),
    }
    print(f"full analyze pass: ok={not full_pass_errors} [{full_attempt_time:.2f}s]")

    # --- 5. best-effort partial pass (non-temporal production phases) --------
    t = Timer()
    partial, partial_errors = _partial_pass(engine, place_ids)
    partial_time = _secs(t)
    report["analysis_partial"] = {
        "note": (
            "Non-temporal phases via production classes (duplicates, templated, "
            "topics, weights, weighted rating, keywords, reviewer metrics). "
            "Burst/anomaly phases are omitted because timestamps are absent."
        ),
        "time_s": partial_time,
        "places_analyzed": partial["places"],
        "reviews": partial["reviews"],
        "errors": partial_errors,
        "aggregates": partial,
    }
    print(f"partial analysis: {partial['places']} places [{partial_time:.2f}s] "
          f"errors={len(partial_errors)}")

    store.close()

    # --- 6. cache-warm rerun (fresh store + engine, embeddings from DB) ------
    t = Timer()
    warm_store = DuckDBStore(db_path=DB_PATH, read_only=True)
    warm_engine = AnalysisEngine(warm_store, use_embeddings=True)
    warm_embed_time = t.elapsed()
    t = Timer()
    warm_partial, warm_errors = _partial_pass(warm_engine, place_ids)
    warm_partial_time = _secs(t)
    warm_hit = warm_engine._embedding_cache.cache_hit_ratio(all_reviews)
    warm_rows = warm_store.cached_embedding_count()
    report["cache_warm"] = {
        "embed_load_from_cache_time_s": round(warm_embed_time, 3),
        "embedding_cache_hit_ratio": round(warm_hit, 4),
        "cached_rows": warm_rows,
        "partial_pass_time_s": warm_partial_time,
        "places_analyzed": warm_partial["places"],
        "errors": warm_errors,
    }
    warm_store.close()
    print(f"cache-warm: embed reload [{warm_embed_time:.2f}s] hit={warm_hit:.3f} "
          f"partial pass [{warm_partial_time:.2f}s] errors={len(warm_errors)}")

    # --- 7. storage ----------------------------------------------------------
    db_size = DB_PATH.stat().st_size
    report["storage"] = {
        "duckdb_size_bytes": db_size,
        "duckdb_size_mib": round(db_size / 1024**2, 2),
        "embedding_cache_rows": warm_rows,
    }
    report["csv"] = {"path": str(CSV_PATH), "size_bytes": CSV_PATH.stat().st_size}

    report["python"] = {"start_wall_time": round(CLOCK_START, 3)}

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
