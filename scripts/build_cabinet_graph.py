"""Materialise verified public-demo detector edges for the cabinet, one loadable file per place."""

import json
from dataclasses import asdict

from reviewscope.config import CONFIG
from scripts.build_cabinet_demo import DEMO_SHA, ROOT, public_engine, public_review, result_id

OUT = ROOT / "prototypes/reviewscope-ui/src/demo-graph"
SCHEMA = "public-demo-direct-pairs-v1"


def project(result):
    groups = []
    members = set()
    for group in result.duplicate_groups:
        members.update(group.review_ids)
        groups.append(
            {
                "id": group.group_id,
                "members": group.review_ids,
                "edges": group.edges,
                "signals": group.signals,
                "counter": group.counter_signals,
            }
        )
    reviews = {}
    for r in result.reviews:
        if r.review_id in members:
            reviews[r.review_id] = public_review(r)
    return {
        "schema": SCHEMA,
        "snapshot": DEMO_SHA,
        "place": result.place_id,
        "groups": groups,
        "reviews": reviews,
    }


def write(name, value):
    # Compact the generated records so a large family loads in a single bounded place chunk.
    (OUT / (name + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    )


def build():
    OUT.mkdir(exist_ok=True)
    entries = []
    with public_engine() as (store, engine):
        for (place_id,) in (
            store.connection()
            .execute("SELECT DISTINCT place_id FROM reviews ORDER BY place_id")
            .fetchall()
        ):
            result = engine.analyze(place_id)
            doc = project(result)
            key = result_id(doc)
            write(place_id, {"run": key, **doc})
            entries.append({"id": place_id, "run": key, "families": len(doc["groups"])})
    index = {
        "schema": SCHEMA,
        "snapshot": DEMO_SHA,
        "producer": "AnalysisEngine / DuplicateDetector",
        "thresholds": asdict(CONFIG.duplicate),
        "places": entries,
    }
    write("manifest", {"run": result_id(index), **index})


if __name__ == "__main__":
    build()
