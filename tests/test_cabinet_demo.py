"""Public packaged evidence parity, with read-only DuckDB and no encoder."""

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from reviewscope.analysis.keywords import (
    _make_vectorizer,
    _tokenize,
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


def test_public_sources_membership_and_versions():
    overview = json.loads(OUTPUT.read_bytes())
    assert overview["contract"] == "public-demo-overview-v2"
    assert len(overview["places"]) == 12
    with public_engine() as (_, engine):
        for entry in overview["places"]:
            place = entry["id"]
            doc = json.loads((OUTPUT.parent / "overview-sources" / (place + ".json")).read_bytes())
            assert doc.pop("result") == entry["source_result"] == result_id(doc)
            assert doc["place"] == place and doc["snapshot"] == DEMO_SHA
            result = engine.analyze(place)
            reviews = result.reviews
            assert doc["topics"] == {str(t.cluster_id): t.review_ids for t in result.clusters}
            assert set(doc["reviews"]) == {r.review_id for r in reviews}
            for r in reviews:
                expected = {"text": r.text, "rating": r.rating, "date": r.published_at}
                saved = doc["reviews"][r.review_id].copy()
                assert saved.pop("version") == result_id([DEMO_SHA, place, r.review_id, expected])
                assert saved == expected | r.model_dump(
                    exclude={"review_id", "place_id", "published_at", "text", "rating"}
                )
            dates = {
                r.review_id: datetime.fromisoformat(r.published_at)
                for r in reviews
                if r.published_at
            }
            dates = {
                key: (d.astimezone(UTC).replace(tzinfo=None) if d.tzinfo else d)
                for key, d in dates.items()
            }
            recent = {
                key for key, d in dates.items() if d >= max(dates.values()) - timedelta(days=30)
            }
            for group, terms in doc["terms"].items():
                sample = [r for r in reviews if (r.text or "").strip()]
                if group == "emerging":
                    sample = [r for r in sample if r.review_id in recent]
                elif group == "positive":
                    sample = [r for r in sample if r.rating is not None and r.rating >= 4]
                elif group == "negative":
                    sample = [r for r in sample if r.rating is not None and r.rating <= 2]
                elif group.startswith("star-"):
                    sample = [r for r in sample if r.rating == int(group[-1])]
                else:
                    assert group == "keywords"
                weighted = (
                    entry["by_rating"][group[-1]] if group.startswith("star-") else entry[group]
                )
                assert set(terms) == {term for term, _ in weighted}
                analyze = (
                    _tokenize
                    if len(sample) == 1 and group != "emerging"
                    else _make_vectorizer().build_analyzer()
                )
                tokens = {r.review_id: set(analyze(r.text)) for r in sample}
                for term, ids in terms.items():
                    assert ids == [rid for rid, words in tokens.items() if term in words]
            if place == "p1":
                assert "r00066" in doc["topics"]["3"]
                assert not any("r00066" in g.review_ids for g in result.duplicate_groups)
