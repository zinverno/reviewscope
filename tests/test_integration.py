"""End-to-end integration test (SPEC.md §38).

The full pipeline over the demo dataset:

    generate demo dataset -> load -> normalize -> persist -> run analysis
                          -> calculate scores -> verify expected anomalies

Expectations come from the demo generator design (SPEC.md §7):
* p1 — injected positive 5★ burst (items 2,4,6,13) must be detected;
* p5 — injected negative 1★ bombing (items 3,4,6,13) must be detected and
  its weighted rating must rise vs the raw rating (bombs down-weighted);
* p3 — injected duplicate group (items 5,6) must be flagged MEDIUM+;
* quiet places (p4, p6) must not be flagged HIGH.
"""

from __future__ import annotations

from pathlib import Path

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.storage import DuckDBStore

DEMO_CSV = Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"


def _pipeline_engine() -> tuple[DuckDBStore, AnalysisEngine]:
    reviews = CSVAdapter().load(DEMO_CSV).reviews
    assert len(reviews) >= 1000, f"demo dataset unexpectedly small: {len(reviews)}"
    store = DuckDBStore()
    store.ingest(reviews)
    return store, AnalysisEngine(store, use_embeddings=True)


class TestIntegration:
    def test_pipeline_runs_over_all_demo_places(self) -> None:
        store, engine = _pipeline_engine()
        places = store.list_places()
        assert not places.empty
        for place_id in places["place_id"]:
            result = engine.analyze(place_id)
            assert result.place_id == place_id
            assert len(result.review_weights) == result.review_count
        store.close()

    def test_p5_negative_bombing_detected_and_downweighted(self) -> None:
        store, engine = _pipeline_engine()
        p5 = engine.analyze("p5")
        assert p5.coordinated is not None
        assert p5.coordinated.confidence.value == "HIGH"
        assert p5.burst_events, "expected a volume burst at p5"
        top = max(p5.burst_events, key=lambda e: e.observed)
        assert top.ratings.get(1, 0) + top.ratings.get(2, 0) > top.ratings.get(4, 0) + top.ratings.get(5, 0)
        assert p5.weighted_rating_value > p5.raw_rating, "bombing reviews must be down-weighted (weighted rises)"
        store.close()

    def test_p1_positive_burst_detected(self) -> None:
        store, engine = _pipeline_engine()
        p1 = engine.analyze("p1")
        assert p1.burst_events, "expected a volume burst at p1"
        five_stars = sum(e.ratings.get(5, 0) for e in p1.burst_events)
        assert five_stars >= 34
        assert p1.coordinated.confidence.value in {"MEDIUM", "HIGH"}
        store.close()

    def test_p3_duplicate_group_flagged(self) -> None:
        store, engine = _pipeline_engine()
        p3 = engine.analyze("p3")
        dup = [g for g in p3.duplicate_groups if len(g.review_ids) >= 3]
        assert dup, "expected a duplicate group at p3"
        assert p3.coordinated.confidence.value in {"MEDIUM", "HIGH"}
        store.close()

    def test_quiet_places_stay_low(self) -> None:
        store, engine = _pipeline_engine()
        for pid in ("p4", "p6"):
            p = engine.analyze(pid)
            assert p.coordinated.confidence.value == "LOW"
        store.close()

    def test_engine_is_deterministic_across_runs(self) -> None:
        store, engine = _pipeline_engine()
        a = engine.analyze("p2")
        b = engine.analyze("p2")
        assert a.raw_rating == b.raw_rating
        assert a.weighted_rating_value == b.weighted_rating_value
        assert a.coordinated.value == b.coordinated.value
        store.close()
