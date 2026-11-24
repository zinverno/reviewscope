"""Replay the complete published values and independently check weight arithmetic and cohorts."""

import hashlib
import json
import random
import subprocess
import sys
from datetime import date

import pytest

from reviewscope.analysis import scoring
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from scripts.build_cabinet_demo import DEMO_SHA, public_engine, public_review, result_id
from scripts.build_cabinet_signals import AS_OF, OUT, aggregate, project


def test_all_frozen_places(monkeypatch):
    class PreviousDay(date):
        @classmethod
        def today(cls):
            return cls(2000, 1, 1)

    monkeypatch.setattr(scoring, "date", PreviousDay)
    index = json.loads((OUT / "manifest.json").read_bytes())
    assert index.pop("result") == result_id(index)
    assert index["dataset"] == DEMO_SHA and len(index["places"]) == 12
    with public_engine() as (_, engine):
        for entry in index["places"]:
            raw = (OUT / (entry["id"] + ".json")).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
            doc = json.loads(raw)
            assert doc.pop("run") == entry["run"] == result_id(doc)
            a = engine.analyze(entry["id"], as_of=AS_OF, explain=True)
            actual = json.loads(json.dumps(project(a)))
            assert doc == actual
            legacy = engine.analyze(entry["id"], as_of=AS_OF)
            aligned = {
                r.review_id: (public_review(r)["version"], w)
                for r, w in zip(legacy.reviews, legacy.review_weights, strict=True)
            }
            assert {
                r["id"]: (r["version"], r["weight"]["weight"]) for r in doc["reviews"]
            } == aligned
            assert round(doc["rating"]["raw"], 2) == legacy.raw_rating
            assert round(doc["rating"]["weighted"], 2) == legacy.weighted_rating_value
            for e in doc["bursts"]:
                assert e["observed"] == len(e["event"]["ids"])
                if e["expected"] < 1:
                    assert e["multiplier"] is None
            for e in doc["rating_events"]:
                for key in ("event", "baseline"):
                    ids = set(e[key]["ids"])
                    rated = [
                        r["rating"]
                        for r in doc["reviews"]
                        if r["id"] in ids and r["rating"] is not None
                    ]
                    assert len(rated) == e[key]["rated"] and e[key]["excluded"] == len(ids) - len(
                        rated
                    )
                    assert {str(k): round(rated.count(k) / len(rated), 4) for k in set(rated)} == e[
                        key + "_dist"
                    ]


def test_anchored_weights_and_exact_trace():
    rng = random.Random(17)
    cfg = CONFIG.weight
    for i in range(50):
        spec, cat, rel, dup, tpl, coord = (rng.random() for _ in range(6))
        r = NormalizedReview(
            review_id=str(i), place_id="p", reviewer_id="r", published_at="2025-11-24"
        )
        args = dict(
            specificity_map={str(i): spec * 100},
            category_experience_map={str(i): cat * 100},
            reviewer_relevance_map={str(i): rel * 100},
            duplicate_probability={str(i): dup},
            templated_probability={str(i): tpl},
            coordinated_probability={str(i): coord},
        )
        trace = {}
        got = scoring.compute_review_weights([r], **args, as_of=AS_OF, explanations=trace)
        q = 0.35 * spec + 0.20 * cat + 0.25 * rel + 0.20 * 0.5
        residual = max(0, round(coord - round(0.30 * max(dup, tpl), 3), 3))
        expected = 1 + 2 * max(0, q - 0.5) - (0.30 * dup + 0.25 * tpl + 0.20 * residual)
        assert got == [round(max(cfg.weight_min, min(cfg.weight_max, expected)), 4)]
        assert trace[str(i)]["quality"] == pytest.approx(q)
        assert trace[str(i)]["coordinated_residual"] == residual
        assert scoring.compute_review_weights(
            [r], **args, as_of=date.today()
        ) == scoring.compute_review_weights([r], **args)


@pytest.mark.parametrize(
    ("weights", "expected"), [([1, 1], 3), ([1, 3], 4), ([3, 1], 2), ([0, 0], None)]
)
def test_rating_denominators(weights, expected):
    rows = [
        dict(rating=r, weight={"weight": w})
        for r, w in zip([1, 5, None], [*weights, 2], strict=True)
    ]
    value = aggregate(rows)
    assert value["weighted"] == expected and value["raw"] == 3
    assert value["count"] == 2 and value["excluded"] == 1
    if expected is not None:
        assert value["delta"] == expected - 3
        assert (
            sum(r["rating"] * r["weight"]["weight"] / value["sum_weights"] for r in rows[:2])
            == expected
        )
    else:
        assert value["reason"] == "ZERO_DENOMINATOR"
    assert aggregate([])["raw"] is aggregate([rows[-1]])["weighted"] is None


def test_private_specificity_needs_no_legacy_engine():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """import sys
from operational.signals import projection
row = dict(text="10 минут",reviewer_id=None,version_id="v",source_place_id="p")
assert projection(row,None,"missing_rating")["specificity"]["heuristic_score"] > 0
assert not ({"torch","sklearn","duckdb","pandas","hdbscan","sentence_transformers"} & sys.modules.keys())
""",
        ],
        check=True,
    )
