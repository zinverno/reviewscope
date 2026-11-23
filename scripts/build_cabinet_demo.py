"""Build overview evidence exclusively from the packaged, frozen public synthetic demo."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.analysis.keywords import keywords_by_rating
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


def build():
    with public_engine() as (store, engine):
        discover = build_dataset_summary(store, engine)
        if discover.errors:
            raise ValueError("incomplete_discover")
        places = []
        for row in discover.places.sort_values("place_id").itertuples():
            place = engine.analyze(row.place_id)
            places.append(
                {
                    "id": place.place_id,
                    "name": row.place_name,
                    "count": place.review_count,
                    "rated": sum(r.rating is not None for r in place.reviews),
                    "raw_rating": row.raw_rating,
                    "one_star_share": row.one_star_share,
                    "keywords": place.keywords,
                    "emerging": place.emerging,
                    "by_rating": keywords_by_rating(place.reviews),
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
    data = {
        "contract": "public-demo-overview-v1",
        "snapshot": DEMO_SHA,
        "encoding": FROZEN_ID,
        "producer": "AnalysisEngine / build_dataset_summary / keywords_by_rating",
        "places": places,
    }
    return {"run": result_id(data), **data}


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build(), ensure_ascii=False, indent=1, allow_nan=False) + "\n")
