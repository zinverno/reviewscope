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

Performance/test-design remediation (audit §..):
* the store + engine (and the expensive embedding pass) are shared across the
  module via a session-scoped fixture, so the integration suite no longer
  re-ingests and re-embeds the demo corpus once per test;
* exactly ONE test exercises the genuine cold path (fresh store + fresh
  engine) so the cold pipeline remains covered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.storage import DuckDBStore

DEMO_CSV = Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"


@pytest.fixture(scope="session")
def pipeline() -> tuple[DuckDBStore, AnalysisEngine]:
    """Shared store + engine: ingest and embed the demo corpus exactly once."""
    reviews = CSVAdapter().load(DEMO_CSV).reviews
    assert len(reviews) >= 1000, f"demo dataset unexpectedly small: {len(reviews)}"
    store = DuckDBStore()
    store.ingest(reviews)
    engine = AnalysisEngine(store, use_embeddings=True)
    yield store, engine
    store.close()


class TestIntegration:
    def test_pipeline_runs_over_all_demo_places(self, pipeline) -> None:
        store, engine = pipeline
        places = store.list_places()
        assert not places.empty
        for place_id in places["place_id"]:
            result = engine.analyze(place_id)
            assert result.place_id == place_id
            assert len(result.review_weights) == result.review_count

    def test_full_pipeline_cold_path(self) -> None:
        # The one genuine cold-path test: fresh store, fresh engine, no shared
        # embedding state. Kept so cold performance is covered even though the
        # rest of the suite reuses one warmed engine.
        reviews = CSVAdapter().load(DEMO_CSV).reviews
        store = DuckDBStore()
        store.ingest(reviews)
        engine = AnalysisEngine(store, use_embeddings=True)
        try:
            p5 = engine.analyze("p5")
            assert p5.place_id == "p5"
            assert p5.burst_events
        finally:
            store.close()

    def test_p5_negative_bombing_detected_and_downweighted(self, pipeline) -> None:
        _, engine = pipeline
        p5 = engine.analyze("p5")
        assert p5.coordinated is not None
        assert p5.coordinated.confidence.value == "HIGH"
        assert p5.burst_events, "expected a volume burst at p5"
        top = max(p5.burst_events, key=lambda e: e.observed)
        assert top.ratings.get(1, 0) + top.ratings.get(2, 0) > top.ratings.get(4, 0) + top.ratings.get(5, 0)
        assert p5.weighted_rating_value > p5.raw_rating, "bombing reviews must be down-weighted (weighted rises)"

    def test_p1_positive_burst_detected(self, pipeline) -> None:
        _, engine = pipeline
        p1 = engine.analyze("p1")
        assert p1.burst_events, "expected a volume burst at p1"
        five_stars = sum(e.ratings.get(5, 0) for e in p1.burst_events)
        assert five_stars >= 34
        assert p1.coordinated.confidence.value in {"MEDIUM", "HIGH"}

    def test_p3_duplicate_group_flagged(self, pipeline) -> None:
        _, engine = pipeline
        p3 = engine.analyze("p3")
        dup = [g for g in p3.duplicate_groups if len(g.review_ids) >= 3]
        assert dup, "expected a duplicate group at p3"
        assert p3.coordinated.confidence.value in {"MEDIUM", "HIGH"}

    def test_quiet_places_stay_low(self, pipeline) -> None:
        _, engine = pipeline
        for pid in ("p4", "p6"):
            p = engine.analyze(pid)
            assert p.coordinated.confidence.value == "LOW"

    def test_engine_is_deterministic_across_runs(self, pipeline) -> None:
        _, engine = pipeline
        a = engine.analyze("p2")
        b = engine.analyze("p2")
        assert a.raw_rating == b.raw_rating
        assert a.weighted_rating_value == b.weighted_rating_value
        assert a.coordinated.value == b.coordinated.value
