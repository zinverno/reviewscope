"""Tie the published provenance, exact pairs and immutable review detail to the public demo."""

import json
from collections import Counter
from dataclasses import asdict

import pytest

from reviewscope.config import CONFIG
from scripts.build_cabinet_demo import DEMO_SHA, public_engine, result_id
from scripts.build_cabinet_graph import OUT, SCHEMA


def test_public_graph_provenance_and_pairs():
    manifest = json.loads((OUT / "manifest.json").read_text())
    assert manifest.pop("run") == result_id(manifest)
    assert manifest["thresholds"] == asdict(CONFIG.duplicate)
    assert manifest["schema"] == SCHEMA and manifest["snapshot"] == DEMO_SHA
    totals = Counter()
    with public_engine() as (_, engine):
        for entry in manifest["places"]:
            place = entry["id"]
            doc = json.loads((OUT / (place + ".json")).read_text())
            assert doc.pop("run") == entry["run"] == result_id(doc)
            assert doc["place"] == place and doc["snapshot"] == DEMO_SHA
            actual = engine.analyze(place)
            rows = {r.review_id: r for r in actual.reviews}
            assert len(doc["groups"]) == entry["families"] == len(actual.duplicate_groups)
            for group, produced in zip(doc["groups"], actual.duplicate_groups, strict=True):
                assert group["id"] == produced.group_id and group["members"] == produced.review_ids
                assert (
                    group["signals"] == produced.signals
                    and group["counter"] == produced.counter_signals
                )
                assert len(group["edges"]) == len(produced.edges)
                for (a, b, kind, score), original in zip(
                    group["edges"], produced.edges, strict=True
                ):
                    assert (a, b, kind) == original[:3] and score == pytest.approx(
                        original[3], abs=1e-5
                    )
                    assert a in group["members"] and b in group["members"]
                    totals[kind] += 1
            for rid, review in doc["reviews"].items():
                version = review.pop("version")
                assert version == result_id([DEMO_SHA, place, rid, review])
                assert review == {
                    "text": rows[rid].text,
                    "rating": rows[rid].rating,
                    "date": rows[rid].published_at,
                }
            if place == "p1":
                pairs = {frozenset(e[:2]) for g in doc["groups"] for e in g["edges"]}
                a, b, c = "rbp011", "rbp013", "rbp018"
                assert {frozenset((a, b)), frozenset((b, c))} <= pairs and frozenset(
                    (a, c)
                ) not in pairs
    assert set(totals) == {"exact", "fuzzy", "near", "semantic"}
