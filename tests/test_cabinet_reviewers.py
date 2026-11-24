"""Verify public files against accepted legacy paths, independently scoped SQL and arithmetic."""

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict
from datetime import date
from statistics import mean, pstdev

import pytest

from reviewscope.analysis.reviewer import (
    category_experience_score,
    compute_reviewer_metrics,
    local_familiarity_score,
    reviewer_relevance_score,
)
from reviewscope.models.review import NormalizedReview
from scripts.build_cabinet_demo import DEMO_SHA, public_engine, public_review, result_id
from scripts.build_cabinet_reviewers import MANIFEST, MAX_BYTES, OUT, encoded, project


def read_chunk(filename):
    data = (OUT / filename).read_bytes()
    assert len(data) <= MAX_BYTES
    assert filename == hashlib.sha256(data).hexdigest() + ".json"
    return json.loads(data)


def test_observed_corpus_and_each_anchor_cohort_against_legacy():
    index = json.loads(MANIFEST.read_bytes())
    assert index.pop("result") == result_id(index)
    assert index["dataset"] == DEMO_SHA
    files = set()
    comparisons = differing_ratios = 0
    with public_engine() as (store, engine):
        db = store.connection()
        assert db.execute("""
            SELECT source, count(*), count(reviewer_id), count(distinct reviewer_id), count(distinct place_id),
              count(*) filter (where isfinite(latitude) and isfinite(longitude)),
              count(*) filter (where latitude between -90 and 90 and longitude between -180 and 180),
              count(*) filter (where latitude is null or longitude is null),
              count(published_at),count(rating),count(text),count(place_category),count(city),count(region)
            FROM reviews GROUP BY source
        """).fetchall() == [
            ("demo", 1058, 1058, 143, 12, 966, 966, 92, 1058, 1058, 1058, 1058, 1058, 1058)
        ]
        assert db.execute("""
            SELECT count(*) FROM (
                SELECT place_id FROM reviews WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                GROUP BY place_id HAVING count(distinct (latitude,longitude)) = 1)
        """).fetchone() == (12,)
        all_reviews = store.fetch_reviews()
        full_metrics = {m.reviewer_id: asdict(m) for m in compute_reviewer_metrics(all_reviews)}
        full_history = engine._history_by_reviewer()
        all_versions = {(r.place_id, r.review_id): public_review(r)["version"] for r in all_reviews}
        covered_versions = set()
        for entry in index["places"]:
            place = entry["id"]
            filename = place + ".json"
            raw = (OUT / filename).read_bytes()
            assert len(raw) <= MAX_BYTES and hashlib.sha256(raw).hexdigest() == entry["sha256"]
            doc = json.loads(raw)
            assert doc.pop("run") == result_id(doc) == entry["run"]
            assert doc["source_result"] == entry["source_result"]
            files.add(filename)
            anchors = [r for r in all_reviews if r.place_id == place]
            scoped = {m.reviewer_id: asdict(m) for m in engine._reviewer_overview(anchors)}
            assert {a["id"] for a in doc["authors"]} == scoped.keys()
            assert doc["known"] == doc["total"] == len(anchors) and doc["missing"] == 0
            for a in doc["authors"]:
                files.update((a["chunk"], a["history"]))
                chunk = read_chunk(a["chunk"])
                assert chunk["place"] == place
                (profile,) = [
                    p for p in chunk["profiles"] if (p["source"], p["id"]) == (a["source"], a["id"])
                ]
                legacy = scoped[a["id"]]
                assert {
                    "reviewer_id": profile["id"],
                    **profile["metrics"],
                    **profile["legacy_sentinels"],
                } == legacy
                history = read_chunk(a["history"])
                assert {
                    "reviewer_id": history["id"],
                    **history["corpus_metrics"],
                    **history["legacy_sentinels"],
                } == full_metrics[a["id"]]
                differing_ratios += any(
                    profile["metrics"][k] != history["corpus_metrics"][k]
                    for k in ("duplicate_ratio", "template_ratio")
                )
                assert history["source"] == a["source"] == "demo"
                assert history["id"] == a["id"]
                observed = full_history[a["id"]]
                assert a["total"] == len(observed)
                assert a["count"] == sum(r.place_id == place for r in observed)
                (a_row,) = [r for r in anchors if r.review_id == a["anchor"]["id"]]
                assert a_row.reviewer_id == a["id"]
                assert a["anchor"]["version"] == all_versions[place, a_row.review_id]
                reviewed = [(r["place"], r["id"], r["version"]) for r in history["reviews"]]
                assert set(reviewed) == {
                    (r.place_id, r.review_id, all_versions[r.place_id, r.review_id])
                    for r in observed
                }
                assert len(reviewed) == len(observed)
                covered_versions.update(reviewed)
                dates = [date.fromisoformat(r.published_at[:10]) for r in observed]
                assert (history["first"], history["last"]) == (
                    min(dates).isoformat(),
                    max(dates).isoformat(),
                )
                assert history["coverage"] == dict(
                    total=len(observed),
                    date=len(observed),
                    rating=len(observed),
                    text=len(observed),
                    category=len(observed),
                    city=len(observed),
                    coordinates=sum(
                        r.latitude is not None and r.longitude is not None for r in observed
                    ),
                )
                assert dict(history["categories"]) == dict(
                    Counter(r.place_category for r in observed)
                )
                assert dict(history["cities"]) == dict(Counter(r.city for r in observed))
                assert {p["id"]: p["count"] for p in history["places"]} == dict(
                    Counter(r.place_id for r in observed)
                )
                for p in history["places"]:
                    coord = {
                        (r.latitude, r.longitude)
                        for r in observed
                        if r.place_id == p["id"]
                        and r.latitude is not None
                        and r.longitude is not None
                    }
                    assert (p["geo"], p["coordinates"]) == (
                        ("valid", list(next(iter(coord)))) if coord else ("missing", None)
                    )
                    assert (p["id"], p["anchor"]["id"], p["anchor"]["version"]) in reviewed
                ratings = [r.rating for r in observed]
                dist = Counter(ratings)
                entropy = -sum(
                    (n / len(ratings)) * math.log2(n / len(ratings)) for n in dist.values()
                ) / math.log2(5)
                assert (legacy["rating_mean"], legacy["rating_std"], legacy["rating_entropy"]) == (
                    round(mean(ratings), 2),
                    round(pstdev(ratings), 3),
                    round(entropy, 3),
                )
                assert legacy["active_period_days"] == max(1, (max(dates) - min(dates)).days)
                cat, city, region = a_row.place_category, a_row.city, a_row.region
                scores = profile["scores"]
                for name, expected in (
                    ("category", category_experience_score(observed, cat)),
                    ("local", local_familiarity_score(observed, city, region)),
                    ("relevance", reviewer_relevance_score(observed, cat, city, region)),
                ):
                    score = scores[name]
                    if score["value"] is None:
                        assert (
                            name == "relevance"
                            and len(ratings) == 1
                            and score["legacy_sentinel"] == expected.value
                            and score["components"] == expected.details
                        )
                    else:
                        assert (score["value"], score["band"], score["components"]) == (
                            expected.value,
                            expected.confidence.value,
                            expected.details,
                        )
                group = [r for r in observed if r.place_category == cat]
                gd = [date.fromisoformat(r.published_at[:10]) for r in group]
                cat_raw = (
                    0.35 * min(1, math.log(len(group) + 1, 10) / 3)
                    + 0.4 * len(group) / len(observed) * min(1, math.log(len(observed) + 1, 10) / 3)
                    + 0.15 * min(1, (max(gd) - min(gd)).days / 730)
                    + 0.1 * min(1, len({r.place_id for r in group}) / 5)
                )
                assert scores["category"]["value"] == pytest.approx(
                    round(100 * cat_raw, 1), abs=0.00001
                )
                comparisons += 1
        assert set(full_history) == set(full_metrics) and len(full_history) == 143
    assert comparisons == 501 and len(covered_versions) == 1058 and differing_ratios == 0
    assert files == {p.name for p in OUT.iterdir()}


def row(id, reviewer="U", *, source="A", place="p", **fields):
    r = NormalizedReview(
        review_id=id,
        reviewer_id="placeholder",
        reviewer_name="Same name",
        place_id=place,
        source=source,
        **fields,
    )
    return r.model_copy(update={"reviewer_id": reviewer})


def unpack(rows):
    return project(rows, {r.place_id: "fixture-source-result" for r in rows})


def test_missing_exact_opaque_identity_multiple_sources_and_input_order():
    rows = [
        row("a", rating=4, published_at="2024-01-01"),
        row("b", place="q", rating=2, published_at="2024-01-01"),
        row("b-other", source="B", rating=5),
        row("case", "u"),
        row("space", " U"),
        row("absent", None),
        row("blank", "  "),
        row("empty", ""),
        row("no-source", source=None),
    ]
    index, files = unpack(rows)
    assert (index, files) == unpack(rows[::-1])
    p = json.loads(files["p.json"])
    assert (p["known"], p["missing"], p["total"]) == (4, 4, 8)
    authors = {(a["source"], a["id"]): a for a in p["authors"]}
    assert set(authors) == {("A", "U"), ("A", "u"), ("A", " U"), ("B", "U")}
    for key, a in authors.items():
        hist = json.loads(files[a["history"]])
        assert hist["coverage"]["total"] == (2 if key == ("A", "U") else 1)
        assert (hist["source"], hist["id"]) == key
        profile = next(
            v for v in json.loads(files[a["chunk"]])["profiles"] if (v["source"], v["id"]) == key
        )
        if key == ("A", "U"):
            assert [r["id"] for r in hist["reviews"]] == ["a", "b"]
            assert (
                profile["metrics"]["active_period_days"],
                profile["metrics"]["rating_mean"],
            ) == (1, 3)
        if key == ("A", "u"):
            assert hist["reviews"][0]["date"] is hist["first"] is hist["last"] is None
            assert all(
                profile["metrics"][n] is None
                for n in (
                    "rating_mean",
                    "rating_std",
                    "review_consistency",
                    "specificity_mean",
                    "duplicate_ratio",
                    "active_period_days",
                    "category_count",
                )
            )
            assert all(s["value"] is None for s in profile["scores"].values())
    assert not unpack([])[0]["places"]
    empty = unpack([row("only", None)])[1]
    assert list(empty) == ["p.json"] and json.loads(empty["p.json"])["authors"] == []


def test_source_and_version_ambiguity():
    r = row("exact", rating=3)
    for second in (
        r,
        r.model_copy(update={"rating": 4}),
        r.model_copy(update={"reviewer_id": "other"}),
        r.model_copy(update={"source": "other"}),
    ):
        with pytest.raises(ValueError, match="AMBIGUOUS_REVIEW_VERSION"):
            unpack([r, second])
    _, before = unpack([r])
    _, after = unpack([r.model_copy(update={"rating": 4})])
    assert (
        json.loads(before["p.json"])["authors"][0]["anchor"]["version"]
        != json.loads(after["p.json"])["authors"][0]["anchor"]["version"]
    )


def test_geo_observed_rows_and_conflict_across_authors():
    rows = [
        row("valid", place="p1", latitude=0, longitude=0),
        row("same-pair", place="p2", latitude=0, longitude=0),
        row("conflict", place="p3", latitude=12, longitude=34),
        row("other", "Z", place="p3", latitude=1, longitude=2),
        row("invalid-lat", place="p4", latitude=-90.01, longitude=0),
        row("invalid-lon", place="p5", latitude=0, longitude=181),
        row("both-missing", place="p6"),
        row("lon-missing", place="p7", latitude=90),
        row("infinite", place="p8", latitude=float("inf"), longitude=2),
        row("nan", place="p9", latitude=1, longitude=float("nan")),
        row("both-limits", place="p10", latitude=-90, longitude=180),
        row("other-source", place="p1", source="B", latitude=1, longitude=2),
        row("unknown-date", place="p2", published_at="unknown"),
    ]
    _, files = unpack(rows)
    hist = json.loads(files[json.loads(files["p1.json"])["authors"][0]["history"]])
    places = {p["id"]: (p["geo"], p["coordinates"]) for p in hist["places"]}
    assert places == {
        **{k: ("valid", [0, 0]) for k in ("p1", "p2")},
        "p3": ("conflict", None),
        **{k: ("invalid", None) for k in ("p4", "p5", "p8", "p9")},
        **{k: ("missing", None) for k in ("p6", "p7")},
        "p10": ("valid", [-90, 180]),
    }
    assert hist["coverage"]["coordinates"] == 4 and len(hist["places"]) == 10


def test_public_resource_limit_refuses_before_detectors_and_publication():
    with pytest.raises(ValueError, match="RESOURCE_LIMIT.*processed=0"):
        unpack([row(str(i)) for i in range(10_000)])
    with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
        encoded("x" * MAX_BYTES)


def test_detector_denominator_recomputed_for_anchor_cohort():
    text = "Повторяющаяся строка из отдельной тестовой выборки"
    rows = [
        row("p-own", place_category="кофе", text="Двенадцать столов у открытого окна"),
        row("q-own", place="q", place_category="музей", text=text),
        row("q-other", "B", place="q", place_category="музей", text=text),
        row("q-third", "C", place="q", place_category="музей", text=text),
    ]
    _, files = unpack(rows)
    p = json.loads(files["p.json"])["authors"][0]
    p_profile = json.loads(files[p["chunk"]])["profiles"][0]
    assert p_profile["metrics"]["duplicate_ratio"] == 0
    assert json.loads(files[p["history"]])["corpus_metrics"]["duplicate_ratio"] == 0.5
    q = next(a for a in json.loads(files["q.json"])["authors"] if a["id"] == "U")
    q_profile = next(v for v in json.loads(files[q["chunk"]])["profiles"] if v["id"] == "U")
    assert q_profile["metrics"]["duplicate_ratio"] == 0.5


def test_local_city_region_blend_and_partial_history():
    rows = [
        row(
            "a",
            place="coffee",
            place_category="coffee",
            city="City",
            region="North",
            published_at="2024-01-01",
            rating=2,
            text="Три стола.",
        ),
        row(
            "b",
            place="coffee",
            place_category="coffee",
            city="City",
            region="North",
            published_at="2024-01-11",
            rating=4,
        ),
        row(
            "c",
            place="gallery",
            place_category="gallery",
            city="Town",
            region="North",
            published_at="2024-01-11",
        ),
        row("d", place="elsewhere"),
    ]
    _, files = unpack(rows)
    author = json.loads(files["coffee.json"])["authors"][0]
    history = json.loads(files[author["history"]])
    profile = json.loads(files[author["chunk"]])["profiles"][0]
    city = 100 * (0.4 / 3 + 0.35 * math.log10(3) / 2.5 + 0.25 * 10 / 365)
    region = 100 * (0.4 / 3 + 0.35 * math.log10(2) / 2.5)
    local = profile["scores"]["local"]
    assert local["value"] == round(0.75 * city + 0.25 * region, 1)
    assert local["components"]["duration"] == round(city / 100, 3)
    assert (
        history["coverage"]["rating"],
        history["coverage"]["text"],
        history["coverage"]["date"],
    ) == (2, 1, 3)
    assert profile["metrics"]["text_length_mean"] == round(len("Три стола.") / 4, 1)
    assert profile["metrics"]["review_consistency"] == 0.5
    assert history["reviews"][-1]["id"] == "d" and history["reviews"][-1]["date"] is None
