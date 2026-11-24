"""Projection of verified immutable versions. No author identity or model is required."""

import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict

from operational.delivery_objects import encoded
from reviewscope.config import CONFIG
from reviewscope.specificity import specificity_score

KIND = "snapshot_explainable_signals_v1"
SCHEMA = "snapshot-explainable-signals-1"
LIMIT = 65536  # Existing publication limit in step_ready_job.


def projection(row, rating, issue):
    text = row["text"]
    spec = specificity_score(text) if text and text.strip() else None
    meta = row["metadata"]
    event = meta["event"]
    # The canonical normalizer stores date-only values verbatim and offset instants in UTC.
    day = (
        event["value"][:10]
        if meta["event_basis"] in ("published", "submitted")
        and event["kind"] in ("date", "instant")
        else None
    )
    return dict(
        observed_day=day,
        version_id=row["version_id"],
        place=row["source_place_id"],
        rating=rating,
        rating_exclusion=issue,
        author_present=row["reviewer_id"] is not None,
        text_present=spec is not None,
        specificity=None
        if spec is None
        else dict(
            heuristic_score=spec.value,
            evidence_band=spec.confidence.value,
            signals=spec.signals,
            counter_signals=spec.counter_signals,
        ),
    )


def metric(value, eligible, total, status="computed", reason=None):
    return dict(value=value, eligible=eligible, total=total, availability=status, reason=reason)


def place_metrics(rows, cover):
    ratings = Counter(row["rating"] for row in rows if row["rating"] is not None)
    n, total = ratings.total(), len(rows)
    texts = sum(r["text_present"] for r in rows)
    coverage_reason = "INSUFFICIENT_COVERAGE" if cover != "full" else "TEMPORAL_ADAPTER_UNAVAILABLE"
    return dict(
        raw=metric(sum(k * v for k, v in ratings.items()) / n, n, total)
        if n
        else metric(None, 0, total, "insufficient_data", "NO_ELIGIBLE_RATINGS"),
        observed_days=dict(
            sorted(Counter(r["observed_day"] for r in rows if r["observed_day"]).items())
        ),
        distribution={str(i): ratings[i] for i in range(1, 6)} if n else None,
        specificity=metric(texts, texts, total)
        if texts
        else metric(None, 0, total, "insufficient_data", "NO_TEXT"),
        weighted=metric(None, 0, total, "not_available", "WEIGHT_INPUTS_UNAVAILABLE"),
        relevance=metric(None, 0, total, "not_available", "AUTHOR_HISTORY_UNAVAILABLE"),
        templated=metric(None, 0, total, "not_available", "NO_PINNED_VECTORS"),
        coordinated=metric(None, 0, total, "not_available", "COMPONENTS_UNAVAILABLE"),
        events=metric(
            None,
            0,
            total,
            "insufficient_data" if cover != "full" else "not_available",
            coverage_reason,
        ),
    )


def result(summary, rows, as_of):
    places = defaultdict(list)
    for row in rows:
        places[row["place"]].append(row)
    data = {
        k: v
        for k, v in summary.items()
        if k
        not in {
            "schema",
            "job_kind",
            "definition_version",
            "limitations",
            "run_id",
            "rating_distribution",
        }
    }
    data.update(
        schema=SCHEMA,
        job_kind=KIND,
        definition_version="signals-1",
        parameters=dict(
            selection="accepted_in_delivery",
            as_of=as_of,
            specificity=asdict(CONFIG.specificity),
            specificity_decimals=1,
        ),
        versions_sha256=hashlib.sha256(encoded([r["version_id"] for r in rows])).hexdigest(),
        places={p: place_metrics(items, summary["coverage"]) for p, items in places.items()},
        reviews=rows,
    )
    return dict(
        **data, run_id=summary["run_id"], result_sha256=hashlib.sha256(encoded(data)).hexdigest()
    )
