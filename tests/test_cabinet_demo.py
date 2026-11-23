"""Public packaged evidence parity, with read-only DuckDB and no encoder."""

import json

import pytest

from reviewscope.analysis.keywords import keywords_by_rating
from scripts.build_cabinet_demo import DEMO_SHA, OUTPUT, public_engine, result_id


def test_public_overview_parity():
    published = json.loads(OUTPUT.read_text())
    assert published.pop("run") == result_id(published)
    assert published["snapshot"] == DEMO_SHA
    assert sum(p["count"] for p in published["places"]) == 1058
    with public_engine() as (store, engine):
        assert store.read_only
        for place_id in ("p1", "p2"):
            row = next(p for p in published["places"] if p["id"] == place_id)
            actual = engine.analyze(place_id)
            rated = [r.rating for r in actual.reviews if r.rating is not None]
            assert row["count"] == actual.review_count and row["rated"] == len(rated)
            assert row["raw_rating"] == pytest.approx(actual.raw_rating, abs=0.0001)
            assert row["one_star_share"] == pytest.approx(
                100 * rated.count(1) / len(rated), abs=0.0001
            )
            assert [t["count"] for t in row["topics"]] == [
                len(t.review_ids) for t in actual.clusters
            ]
            assert [w[0] for w in row["keywords"]] == [w for w, _ in actual.keywords]
            assert [w[0] for w in row["by_rating"]["5"]] == [
                w for w, _ in keywords_by_rating(actual.reviews)[5]
            ]
