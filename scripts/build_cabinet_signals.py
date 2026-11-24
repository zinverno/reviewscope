"""Freeze the PR-17 result exclusively from the verified public corpus and its vectors."""

import hashlib
import json
from dataclasses import asdict
from datetime import date, timedelta

from reviewscope.config import CONFIG
from reviewscope.embeddings.frozen_demo import FROZEN_ID
from scripts.build_cabinet_demo import DEMO_SHA, ROOT, public_engine, public_review, result_id

OUT = ROOT / "prototypes/reviewscope-ui/src/demo-signals"
AS_OF = date(2026, 11, 24)
SCHEMA = "public-explainable-signals-v1"


def evidence(score):
    return dict(
        heuristic_score=score.value,
        evidence_band=score.confidence.value,
        signals=[
            s.replace("manipulation event window", "detected event window") for s in score.signals
        ],
        counter_signals=score.counter_signals,
        components=score.details,
    )


def window(reviews, first, last):
    ids = sorted(
        r.review_id
        for r in reviews
        if r.published_at and first.isoformat() <= r.published_at[:10] <= last.isoformat()
    )
    set_ids = set(ids)
    rated = sum(r.rating is not None for r in reviews if r.review_id in set_ids)
    return dict(
        first=first.isoformat(),
        last=last.isoformat(),
        ids=ids,
        rated=rated,
        excluded=len(ids) - rated,
    )


def event_record(event, reviews, rating):
    day = event.date
    width = CONFIG.rating_anomaly.event_window_days if rating else 1
    start = day - timedelta(days=(width - 1) // 2)
    end = day + timedelta(days=width // 2)
    cfg = CONFIG.rating_anomaly if rating else CONFIG.burst
    value = asdict(event)
    value.update(
        date=day.isoformat(),
        severity=event.severity.value,
        baseline=window(
            reviews, day - timedelta(days=cfg.baseline_window), start - timedelta(days=1)
        ),
        event=window(reviews, start, end),
    )
    if not rating and event.expected < 1:
        value["multiplier"] = None
    return value


def aggregate(rows):
    rated = [r for r in rows if r["rating"] is not None]
    den = sum(r["weight"]["weight"] for r in rated)
    raw = sum(r["rating"] for r in rated) / len(rated) if rated else None
    weighted = sum(r["rating"] * r["weight"]["weight"] for r in rated) / den if den else None
    return dict(
        count=len(rated),
        excluded=len(rows) - len(rated),
        sum_weights=den,
        raw=raw,
        weighted=weighted,
        delta=weighted - raw if weighted is not None else None,
        reason=None if weighted is not None else "ZERO_DENOMINATOR",
    )


def project(result):
    rows = []
    for r, template in zip(result.reviews, result.templated_scores, strict=True):
        rows.append(
            dict(
                id=r.review_id,
                version=public_review(r)["version"],
                rating=r.rating,
                weight=result.weight_explanations[r.review_id],
                templated=evidence(template),
                **{k: evidence(v) for k, v in result.score_explanations[r.review_id].items()},
            )
        )
    rows.sort(key=lambda row: row["id"])
    return dict(
        schema=SCHEMA,
        dataset=DEMO_SHA,
        place=result.place_id,
        as_of=AS_OF.isoformat(),
        method=method(),
        versions_sha256=result_id([r["version"] for r in rows]),
        reviews=rows,
        rating=aggregate(rows),
        coordinated=evidence(result.coordinated),
        coordinated_inputs=result.coordinated_components,
        bursts=[event_record(e, result.reviews, False) for e in result.burst_events],
        rating_events=[event_record(e, result.reviews, True) for e in result.rating_anomalies],
    )


def method():
    config = asdict(CONFIG)
    config.pop("embedding")  # The historical vector set does not attest a model revision.
    coordinated = config["coordinated"]
    coordinated["review_strength_components"] = coordinated.pop("review_probability_components")
    return dict(
        selection="all_reviews_at_place",
        source="frozen-public-duckdb",
        encoding=FROZEN_ID,
        config=config,
        jsd_log="natural",
        strength_decimals=3,
        weight_decimals=4,
        display_rating_decimals=2,
    )


def write(path, doc):
    raw = (
        json.dumps(
            doc, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def build():
    OUT.mkdir(exist_ok=True)
    entries = []
    with public_engine() as (store, engine):
        for (place,) in (
            store.connection()
            .execute("SELECT DISTINCT place_id FROM reviews ORDER BY 1")
            .fetchall()
        ):
            doc = project(engine.analyze(place, as_of=AS_OF, explain=True))
            run = result_id(doc)
            sha = write(OUT / (place + ".json"), dict(run=run, **doc))
            entries.append(dict(id=place, run=run, sha256=sha))
    manifest = dict(schema=SCHEMA, dataset=DEMO_SHA, places=entries)
    write(OUT / "manifest.json", dict(result=result_id(manifest), **manifest))


if __name__ == "__main__":
    build()
