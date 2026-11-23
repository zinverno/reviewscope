"""Public packaged evidence parity, with read-only DuckDB and no encoder."""

import json
import sys

import pytest

from reviewscope.analysis.keywords import (
    emerging_keywords,
    extract_keywords,
    keywords_by_rating,
    keywords_by_sentiment,
)
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
            assert row["five_star_share"] == pytest.approx(
                100 * rated.count(5) / len(rated), abs=0.0001
            )
            sentiment = keywords_by_sentiment(actual.reviews, top_n=sys.maxsize)
            ratings = keywords_by_rating(actual.reviews, top_n=sys.maxsize)
            old_stars = keywords_by_rating(actual.reviews)
            for saved, complete, legacy_top in [
                (
                    row["keywords"],
                    extract_keywords(actual.reviews, top_n=sys.maxsize),
                    actual.keywords,
                ),
                (
                    row["emerging"],
                    emerging_keywords(actual.reviews, top_n=sys.maxsize),
                    actual.emerging,
                ),
                (row["positive"], sentiment["positive"], actual.positive_keywords),
                (row["negative"], sentiment["negative"], actual.negative_keywords),
                *(
                    (row["by_rating"][str(star)], weights, old_stars[star])
                    for star, weights in ratings.items()
                ),
            ]:
                expected = sorted(complete, key=lambda pair: (-pair[1], pair[0]))[: len(legacy_top)]
                assert [term for term, _ in saved] == [term for term, _ in expected]
                assert [score for _, score in saved] == pytest.approx(
                    [score for _, score in expected]
                )
                assert [score for _, score in saved] == pytest.approx(
                    [score for _, score in legacy_top]
                )
            if place_id == "p1":
                weights = dict(sentiment["positive"])
                tied = ["место сервис", "обстановка", "отличное", "отличное место", "ставлю"]
                assert len({weights[t] for t in tied}) == 1
                assert [t for t, _ in row["positive"] if t in tied] == sorted(tied)[:2]
