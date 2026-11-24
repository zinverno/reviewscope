"""Freeze observed reviewer context from the verified synthetic DuckDB only."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date

from reviewscope.analysis.reviewer import (
    category_experience_score,
    compute_reviewer_metrics,
    local_familiarity_score,
    reviewer_relevance_score,
)
from reviewscope.config import CONFIG
from scripts.build_cabinet_demo import DEMO_SHA, OUTPUT, public_engine, public_review, result_id

MANIFEST = OUTPUT.parent / "reviewer-manifest.json"
OUT = OUTPUT.parent / "reviewer-context"
SCHEMA = "public-reviewer-context-v1"
MAX_ROWS = 1058  # Only the pinned public corpus is admitted to the offline detector path.
MAX_BYTES = 65_536


def encoded(doc):
    raw = json.dumps(
        doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    data = raw.encode() + b"\n"
    if len(data) > MAX_BYTES:
        raise ValueError("RESOURCE_LIMIT: artifact exceeds 65536 bytes")
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def known(value):
    return isinstance(value, str) and bool(value.strip())


def day(row):
    try:
        return date.fromisoformat((row.published_at or "")[:10]).isoformat()
    except ValueError:
        return None


def coordinates(row):
    lat, lon = row.latitude, row.longitude
    if lat is None or lon is None:
        return "missing", None
    if math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180:
        return "valid", (lat, lon)
    return "invalid", None


def counts(rows, field):
    return sorted(Counter(getattr(r, field) for r in rows if known(getattr(r, field))).items())


def ref(row):
    return dict(id=row.review_id, version=public_review(row)["version"])


def available_metrics(metrics, coverage):
    values = asdict(metrics)
    del values["reviewer_id"]
    suppressed = {}
    for eligible, names in [
        (coverage["rating"], ("rating_mean", "rating_std", "rating_entropy", "review_diversity")),
        (coverage["rating"] >= 2, ("review_consistency",)),
        (coverage["date"], ("active_period_days",)),
        (coverage["category"], ("category_count",)),
        (coverage["city"], ("city_count",)),
        (
            coverage["text"],
            ("text_length_mean", "specificity_mean", "duplicate_ratio", "template_ratio"),
        ),
    ]:
        if not eligible:
            for name in names:
                suppressed[name], values[name] = values[name], None
    return values, suppressed


def history_doc(rows, conflicting_places):
    source, reviewer = rows[0].source, rows[0].reviewer_id
    reviews = [dict(**ref(r), place=r.place_id, date=day(r)) for r in rows]
    reviews.sort(key=lambda r: (r["date"] is None, r["date"], r["place"], r["id"], r["version"]))
    dates = [r["date"] for r in reviews if r["date"]]
    places = defaultdict(list)
    for r in rows:
        places[r.place_id].append(r)
    table = []
    for place, group in sorted(places.items()):
        states = [coordinates(r) for r in group]
        pairs = [pair for _, pair in states if pair is not None]
        if (source, place) in conflicting_places:
            geo, pair = "conflict", None
        elif pairs:
            geo, pair = "valid", pairs[0]
        else:
            geo, pair = (
                ("invalid" if any(state == "invalid" for state, _ in states) else "missing"),
                None,
            )
        anchor = min(
            (r for r in group if pair and coordinates(r)[1] == pair),
            key=lambda r: r.review_id,
            default=group[0],
        )
        table.append(
            dict(
                id=place,
                name=anchor.place_name,
                category=anchor.place_category,
                count=len(group),
                coordinates=pair,
                geo=geo,
                anchor=ref(anchor),
            )
        )
    coverage = dict(
        total=len(rows),
        date=len(dates),
        rating=sum(r.rating is not None for r in rows),
        text=sum(known(r.text) for r in rows),
        category=sum(known(r.place_category) for r in rows),
        city=sum(known(r.city) for r in rows),
        coordinates=sum(coordinates(r)[0] == "valid" for r in rows),
    )
    return dict(
        source=source,
        id=reviewer,
        coverage=coverage,
        first=min(dates, default=None),
        last=max(dates, default=None),
        categories=counts(rows, "place_category"),
        cities=counts(rows, "city"),
        places=table,
        reviews=reviews,
    )


def score_doc(score, eligible, signals, counters):
    return dict(
        value=score.value if eligible else None,
        band=score.confidence.value if eligible else None,
        components=score.details,  # Diagnostic legacy components; only shown for eligible scores.
        signals=signals if eligible else [],
        counter_signals=counters if eligible else ["Недостаточно исходных полей для индекса."],
        legacy_sentinel=None if eligible else score.value,
    )


def profile_doc(metrics, rows, history, anchor):
    coverage = history["coverage"]
    category, city, region = anchor.place_category, anchor.city, anchor.region
    cat = category_experience_score(rows, category)
    loc = local_familiarity_score(rows, city, region)
    rel = reviewer_relevance_score(rows, category, city, region)
    in_cat = [r for r in rows if category and r.place_category == category]
    cat_dates = sorted(d for r in in_cat if (d := day(r)))
    cat_signals = [f"Категория точки указана у {len(in_cat)} из {len(rows)} отзывов."]
    if cat_dates:
        cat_signals.append(f"Известные даты в категории от {cat_dates[0]} до {cat_dates[-1]}.")
    cat_signals.append(f"Различных точек категории в истории {len({r.place_id for r in in_cat})}.")
    city_count = sum(bool(city) and r.city == city for r in rows)
    region_count = sum(bool(region) and r.region == region and r.city != city for r in rows)
    local_signals = [
        f"Отзывов в городе точки {city_count}, в других городах региона {region_count}."
    ]
    if loc.details:
        local_signals.append(
            f"Различных точек в выбранной локальной группе {loc.details['places']}."
        )
    local_counters = []
    for reason in loc.counter_signals:
        if "(travel context)" in reason:
            local_counters.append(
                "В данных также есть отзывы о местах в других городах или регионах."
            )
        elif reason.startswith("no reviews"):
            local_counters.append("В данных нет отзывов по указанному городу или региону.")
        else:
            local_counters.append("Низкое значение компонента отзывов в этой местности.")
    relevance_reasons = {
        "deep category experience": "Высокий компонент истории категории.",
        "historically specific reviews": "Высокая средняя конкретность текстов в выборке.",
        "consistent rating behavior": "Малый разброс наблюдаемых оценок.",
        "strong local context": "Высокий компонент отзывов по местности точки.",
        "substantial review history": "Число отзывов повышает логарифмический компонент объёма истории.",
        "little category experience": "Малый компонент истории категории.",
        "no review history": "История в предоставленном корпусе пуста.",
    }
    cat_ok = known(category) and coverage["category"] > 0
    loc_ok = (known(city) and coverage["city"] > 0) or (
        known(region) and any(known(r.region) for r in rows)
    )
    rel_ok = cat_ok and loc_ok and coverage["rating"] >= 2 and coverage["text"] > 0
    scores = dict(
        category=score_doc(
            cat,
            cat_ok,
            cat_signals,
            ["Ограниченная история категории."] if cat.counter_signals else [],
        ),
        local=score_doc(
            loc,
            loc_ok,
            local_signals,
            local_counters,
        ),
        relevance=score_doc(
            rel,
            rel_ok,
            [relevance_reasons[r] for r in rel.signals],
            [relevance_reasons[r] for r in rel.counter_signals],
        ),
    )
    values, suppressed = available_metrics(metrics, coverage)
    return dict(
        source=anchor.source,
        id=metrics.reviewer_id,
        metrics=values,
        legacy_sentinels=suppressed,
        scores=scores,
    )


def project(rows, source_results):
    if len(rows) > MAX_ROWS:
        raise ValueError("RESOURCE_LIMIT: frozen corpus exceeded; processed=0")
    rows = sorted(rows, key=lambda r: (r.source or "", r.place_id, r.review_id))
    # Legacy flags use source/review ID; published evidence addresses use place/review ID.
    if len({(r.source, r.review_id) for r in rows}) != len(rows) or len(
        {(r.place_id, r.review_id) for r in rows}
    ) != len(rows):
        raise ValueError("AMBIGUOUS_REVIEW_VERSION")
    grouped = defaultdict(list)
    at_place = defaultdict(list)
    place_pairs = defaultdict(set)
    for r in rows:
        at_place[r.place_id].append(r)
        if (pair := coordinates(r)[1]) is not None:
            place_pairs[r.source, r.place_id].add(pair)
        if known(r.source) and known(r.reviewer_id):
            grouped[r.source, r.reviewer_id].append(r)
    conflicts = {key for key, pairs in place_pairs.items() if len(pairs) > 1}
    histories = {key: history_doc(group, conflicts) for key, group in grouped.items()}
    by_source = defaultdict(list)
    for (source, _), group in grouped.items():
        by_source[source].extend(group)
    files = {}

    def chunk(doc):
        raw = encoded(doc)
        name = digest(raw) + ".json"
        files[name] = raw
        return name

    history_files = {}
    for source, corpus in sorted(by_source.items()):
        for m in compute_reviewer_metrics(corpus):
            key = (source, m.reviewer_id)
            history = histories[key]
            values, suppressed = available_metrics(m, history["coverage"])
            history_files[key] = chunk(
                dict(**history, corpus_metrics=values, legacy_sentinels=suppressed)
            )
    entries = []
    for place, anchors in sorted(at_place.items()):
        by_author = defaultdict(list)
        for r in anchors:
            key = (r.source, r.reviewer_id)
            if key in grouped:
                by_author[key].append(r)
        authors = []
        for source in sorted({s for s, _ in by_author}):
            if (
                len({(r.place_category, r.city, r.region) for r in anchors if r.source == source})
                != 1
            ):
                raise ValueError("AMBIGUOUS_ANCHOR_METADATA")
            cohort = [r for r in by_source[source] if (source, r.reviewer_id) in by_author]
            profiles = []
            for m in compute_reviewer_metrics(cohort):
                key = (source, m.reviewer_id)
                own = by_author[key]
                anchor = own[0]
                profiles.append(profile_doc(m, grouped[key], histories[key], anchor))
                authors.append(
                    dict(
                        source=source,
                        id=m.reviewer_id,
                        count=len(own),
                        total=m.review_count,
                        anchor=ref(anchor),
                        history=history_files[key],
                    )
                )
            start = len(authors) - len(profiles)
            for i in range(0, len(profiles), 8):
                part = profiles[i : i + 8]
                filename = chunk(dict(place=place, profiles=part))
                for j in range(len(part)):
                    authors[start + i + j]["chunk"] = filename
        known_count = sum(a["count"] for a in authors)
        doc = dict(
            place=place,
            dataset=DEMO_SHA,
            source_result=source_results[place],
            authors=authors,
            total=len(anchors),
            known=known_count,
            missing=len(anchors) - known_count,
        )
        run = result_id(doc)
        raw = encoded(dict(run=run, **doc))
        files[place + ".json"] = raw
        entries.append(
            dict(id=place, run=run, source_result=source_results[place], sha256=digest(raw))
        )
    config = {
        k: asdict(getattr(CONFIG, k))
        for k in (
            "category_experience",
            "local_familiarity",
            "reviewer_relevance",
            "specificity",
            "templated",
            "duplicate",
        )
    }
    manifest = dict(
        schema=SCHEMA,
        dataset=DEMO_SHA,
        producer="scripts.build_cabinet_reviewers",
        config=config,
        scope="exact source and reviewer ID; full corpus and anchor cohorts",
        places=entries,
    )
    return dict(result=result_id(manifest), **manifest), files


def build():
    overview = json.loads(OUTPUT.read_bytes())
    expected = {p["id"]: p["source_result"] for p in overview["places"]}
    with public_engine() as (store, _):
        rows = store.fetch_reviews()
        # Legacy normalization stringifies IDs. Confirm provenance against raw DuckDB values.
        identities = dict(
            store.connection().execute("SELECT review_id, reviewer_id FROM reviews").fetchall()
        )
        rows = [r.model_copy(update={"reviewer_id": identities[r.review_id]}) for r in rows]
        for place, source_result in expected.items():
            doc = json.loads((OUTPUT.parent / "overview-sources" / (place + ".json")).read_bytes())
            declared = doc.pop("result")
            if not (
                declared == source_result == result_id(doc)
                and doc["snapshot"] == DEMO_SHA
                and doc["place"] == place
            ):
                raise ValueError("unverified_source_result")
            frozen_rows = {
                r.review_id: {
                    **public_review(r),
                    **r.model_dump(
                        exclude={"text", "rating", "published_at", "place_id", "review_id"}
                    ),
                }
                for r in rows
                if r.place_id == place
            }
            if frozen_rows != doc["reviews"]:
                raise ValueError("unverified_source_membership")
        manifest, files = project(rows, expected)
    manifest_bytes = encoded(manifest)
    OUT.mkdir(exist_ok=True)
    for name, raw in files.items():
        (OUT / name).write_bytes(raw)
    MANIFEST.write_bytes(manifest_bytes)


if __name__ == "__main__":
    build()
