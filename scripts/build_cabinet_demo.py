"""Build overview evidence exclusively from the packaged, frozen public synthetic demo."""

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.analysis.keywords import (
    _make_vectorizer,
    _tokenize,
    emerging_keywords,
    extract_keywords,
    keyword_periods,
    keywords_by_rating,
    keywords_by_sentiment,
)
from reviewscope.discovery.summary import build_dataset_summary
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.frozen_demo import FROZEN_ID
from reviewscope.storage import DuckDBStore

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo/reviewscope_demo.duckdb"
DEMO_SHA = "85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2"
OUTPUT = ROOT / "prototypes/reviewscope-ui/src/overview.synthetic.json"


@contextmanager
def public_engine():
    with DEMO.open("rb") as raw:
        if hashlib.file_digest(raw, "sha256").hexdigest() != DEMO_SHA:
            raise ValueError("unverified_public_demo")
    with DuckDBStore(DEMO, read_only=True) as store:
        store.connection().execute("SET enable_external_access=false")
        store.connection().execute("SET TimeZone='UTC'")
        store.connection().execute("SET threads=1")
        cache = EmbeddingCache(store, precomputed_only=True)
        if cache.encoding_id != FROZEN_ID or cache.missing_keys(store.fetch_reviews()):
            raise ValueError("unverified_demo_vectors")
        yield store, AnalysisEngine(store, embedding_cache=cache)


def result_id(data):
    return hashlib.sha256(
        json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def ranked_terms(weighted, limit):
    # NumPy may order identical TF-IDF values differently on different CPUs.
    # Choose from the full scored vocabulary before applying the published cutoff.
    return sorted(weighted, key=lambda term: (-term[1], term[0]))[:limit]


def public_review(review):
    fields = {"text": review.text, "rating": review.rating, "date": review.published_at}
    return {**fields, "version": result_id([DEMO_SHA, review.place_id, review.review_id, fields])}


def write_sources(place, overview):
    reviews = place.reviews
    groups = {
        "keywords": (reviews, overview["keywords"]),
        "positive": (
            [r for r in reviews if r.rating is not None and r.rating >= 4],
            overview["positive"],
        ),
        "negative": (
            [r for r in reviews if r.rating is not None and r.rating <= 2],
            overview["negative"],
        ),
        "emerging": (keyword_periods(reviews)[1], overview["emerging"]),
    }
    for star, weights in overview["by_rating"].items():
        groups[f"star-{star}"] = ([r for r in reviews if r.rating == star], weights)
    matches = {}
    analyze = _make_vectorizer().build_analyzer()
    for group, (sample, weighted) in groups.items():
        selection = {term: [] for term, _ in weighted}
        texts = [r for r in sample if (r.text or "").strip()]
        tokenize = _tokenize if len(texts) == 1 and group != "emerging" else analyze
        for review in texts:
            for term in selection.keys() & set(tokenize(review.text)):
                selection[term].append(review.review_id)
        matches[group] = selection
    doc = {
        "place": place.place_id,
        "snapshot": DEMO_SHA,
        "topics": {str(t.cluster_id): t.review_ids for t in place.clusters},
        "terms": matches,
        "reviews": {
            r.review_id: {
                **r.model_dump(exclude={"text", "rating", "published_at", "place_id", "review_id"}),
                **public_review(r),
            }
            for r in reviews
        },
    }
    digest = result_id(doc)
    directory = OUTPUT.parent / "overview-sources"
    directory.mkdir(exist_ok=True)
    (directory / (place.place_id + ".json")).write_text(
        json.dumps(
            {"result": digest, **doc}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    )
    return digest


def build():
    with public_engine() as (store, engine):
        discover = build_dataset_summary(store, engine)
        if discover.errors:
            raise ValueError("incomplete_discover")
        places = []
        for row in discover.places.sort_values("place_id").itertuples():
            place = engine.analyze(row.place_id)
            reviews = place.reviews
            sentiment = keywords_by_sentiment(reviews, top_n=sys.maxsize)
            by_rating = keywords_by_rating(reviews, top_n=sys.maxsize)
            places.append(
                {
                    "id": place.place_id,
                    "name": row.place_name,
                    "count": place.review_count,
                    "rated": sum(r.rating is not None for r in place.reviews),
                    "raw_rating": row.raw_rating,
                    "one_star_share": row.one_star_share,
                    "five_star_share": row.five_star_share,
                    "positive": ranked_terms(sentiment["positive"], 10),
                    "negative": ranked_terms(sentiment["negative"], 10),
                    "keywords": ranked_terms(extract_keywords(reviews, top_n=sys.maxsize), 15),
                    "emerging": ranked_terms(emerging_keywords(reviews, top_n=sys.maxsize), 10),
                    "by_rating": {
                        star: ranked_terms(terms, 8) for star, terms in by_rating.items()
                    },
                    "topics": [
                        {
                            "id": t.cluster_id,
                            "count": len(t.review_ids),
                            "phrases": t.representative_phrases,
                        }
                        for t in place.clusters
                    ],
                }
            )
            places[-1]["source_result"] = write_sources(place, places[-1])
    data = {
        "contract": "public-demo-overview-v2",
        "snapshot": DEMO_SHA,
        "encoding": FROZEN_ID,
        "producer": "AnalysisEngine / build_dataset_summary / reviewscope.analysis.keywords",
        "term_order": "original weight descending, then Unicode term ascending",
        "places": places,
    }
    return {"run": result_id(data), **data}


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build(), ensure_ascii=False, indent=1, allow_nan=False) + "\n")
