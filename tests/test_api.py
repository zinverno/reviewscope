"""Offline v1 HTTP contract over the packaged demo and tiny synthetic snapshots."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import duckdb
import numpy as np
import pytest
from fastapi.testclient import TestClient

from reviewscope import api
from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.api.models import Analysis, Health, Place, ReviewPage
from reviewscope.embeddings.base import embedding_key
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider
from reviewscope.models.review import NormalizedReview
from reviewscope.public_demo import DEFAULT_DEMO_DB, repo_root
from reviewscope.storage import DuckDBStore

DEMO = repo_root() / DEFAULT_DEMO_DB
ROOT = "/api/v1"
LOCAL = "http://127.0.0.1"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("RS_PUBLIC_DEMO", raising=False)
    monkeypatch.delenv("RS_API_DB_PATH", raising=False)

    def forbidden(*args):
        pytest.fail("API attempted to encode text")

    monkeypatch.setattr(SentenceTransformerProvider, "encode", forbidden)


@pytest.fixture
def snapshot(tmp_path):
    data = [
        ("c", 46, None, None),
        ("b", 23, "синтетический мост", "2026-01-01T11:00:00+01:00"),
        ("a", 0, "лимон", "2026-01-01T10:00:00"),
        ("d", 0, None, "today"),
    ]
    provider = SentenceTransformerProvider()
    path = tmp_path / "snapshot.duckdb"
    with DuckDBStore(path) as store:
        for rid, angle, text, stamp in data:
            review = NormalizedReview(
                review_id=rid, place_id="p" if rid != "d" else "x",
                reviewer_id=rid, text=text, published_at=stamp,
                latitude=11 if rid in {"a", "c"} else None,
                longitude=22 if rid == "b" else None,
            )
            store.ingest([review])
            vec = np.zeros(provider.dim)
            if text:
                vec[:2] = [np.cos(np.deg2rad(angle)), np.sin(np.deg2rad(angle))]
            store.store_cached_embeddings([
                (*embedding_key(rid, text or "", provider.contract.encoding_id), vec)
            ])
    return path


def client(path=None):
    return TestClient(api.create_app(db_path=path), base_url=LOCAL)


def test_places_pages_and_openapi(snapshot):
    before = digest(snapshot)
    with client(snapshot) as http:
        assert http.get(ROOT + "/health").json() == {
            "status": "ok", "api_version": "v1", "dataset_available": True,
        }
        assert http.get(ROOT + "/places").json() == [
            {"place_id": key, "name": None, "category": None, "review_count": n,
             "coordinates": None} for key, n in [("p", 3), ("x", 1)]
        ]
        schema = http.get("/openapi.json").json()
        for route, model in [("health", Health), ("places", Place),
                             ("places/{place_id}/reviews", ReviewPage),
                             ("places/{place_id}/analysis", Analysis)]:
            definition = schema["paths"][ROOT + "/" + route]["get"]["responses"]
            body = definition["200"]["content"]["application/json"]["schema"]
            assert body.get("items", body)["$ref"].endswith("/" + model.__name__)
            assert set(schema["components"]["schemas"][model.__name__]["properties"]) == set(model.model_fields)
            assert definition["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/Error")

        url = ROOT + "/places/p/reviews"
        pages = [http.get(url, params={"limit": 1, "offset": i}).json() for i in range(4)]
        assert pages[-1] == {"place_id": "p", "total": 3, "offset": 3, "limit": 1, "reviews": []}
        assert pages[0]["reviews"] == [{
            "review_id": "a", "text": "лимон", "rating": None, "published_at": "2026-01-01T10:00:00Z",
        }]
        assert pages[1]["reviews"][0]["review_id"] == "b"
        assert pages[1]["reviews"][0]["published_at"] == pages[0]["reviews"][0]["published_at"]
        assert pages[2]["reviews"] == [{"review_id": "c", "text": None, "rating": None, "published_at": None}]
        assert http.get(url).json()["reviews"] == [page["reviews"][0] for page in pages[:3]]
        assert http.get(ROOT + "/places/x/reviews").json()["reviews"][0]["published_at"] is None
        unrated = http.get(ROOT + "/places/p/analysis").json()
        assert unrated["raw_rating"] is None and unrated["rated_review_count"] == 0
        assert unrated["calculated_weighted_rating"]["value"] is None
        assert unrated["calculated_weighted_rating"]["signals"] == ["no rated reviews"]
    assert digest(snapshot) == before
    assert list(snapshot.parent.iterdir()) == [snapshot]


def test_empty_dataset_and_place_errors(tmp_path):
    path = tmp_path / "empty.duckdb"
    with DuckDBStore(path):
        pass
    with client(path) as http:
        assert http.get(ROOT + "/places").json() == []
        for view in ("reviews", "analysis"):
            response = http.get(ROOT + f"/places/unknown/{view}")
            assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_request_boundary(snapshot):
    secret = "/private/synthetic-test-marker"
    with client(snapshot) as http:
        url = ROOT + "/places/p/reviews"
        for params in [{"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"offset": -1},
                       {"offset": 2**31}, {"db_path": secret}, {"url": "https://example.invalid"},
                       {"sql": "SELECT 1"}, {"command": "example"}, [("limit", 1), ("limit", 2)]]:
            response = http.get(url, params=params)
            assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})
        for endpoint in ("/health", "/places", "/places/p/analysis"):
            assert http.get(ROOT + endpoint, params={"path": secret}).status_code == 422
        assert http.request("GET", url, json={"db_path": secret}).status_code == 422
        assert http.request("GET", url, content=iter([b"body"])).status_code == 422
        for endpoint in ("%2e%2e%2fprivate", "p%27%20OR%201%3D1--"):
            assert http.get(ROOT + "/places/" + endpoint + "/reviews").status_code == 404
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            response = http.request(method, url, headers={"Origin": "https://example.invalid"})
            assert (response.status_code, response.json()) == (405, {"error": "method_not_allowed"})
            assert "access-control-allow-origin" not in response.headers
        assert http.get(url, headers={"Host": "example.invalid"}).status_code == 400
        assert http.get(url).json()["total"] == 3


@pytest.mark.parametrize("failure", ["absent", "corrupt", "schema", "vector", "invalid_vector"])
def test_unavailable_never_falls_back(tmp_path, snapshot, failure):
    path = tmp_path / "private-test-name.duckdb"
    if failure == "corrupt":
        path.write_bytes(b"not a database")
    elif failure == "schema":
        with duckdb.connect(str(path)) as con:
            con.execute("create table reviews (review_id varchar)")
    elif failure in {"vector", "invalid_vector"}:
        shutil.copyfile(snapshot, path)
        with DuckDBStore(path) as store:
            sql = ("DELETE FROM embeddings_cache" if failure == "vector"
                   else "UPDATE embeddings_cache SET embedding = [0.0]")
            store.connection().execute(sql + " WHERE review_id = 'a'")
    before = digest(path) if path.exists() else None
    with client(path) as http:
        assert http.get(ROOT + "/health").json() == {
            "status": "degraded", "api_version": "v1", "dataset_available": False,
        }
        for endpoint in ("/places", "/places/p/reviews", "/places/p/analysis"):
            response = http.get(ROOT + endpoint)
            assert (response.status_code, response.json()) == (503, {"error": "dataset_unavailable"})
    assert (digest(path) if path.exists() else None) == before
    assert not path.with_suffix(".duckdb.wal").exists()


def test_demo_matches_engine_and_public_mode_ignores_local_override(tmp_path, monkeypatch):
    path = tmp_path / DEFAULT_DEMO_DB
    path.parent.mkdir()
    shutil.copyfile(DEMO, path)
    shutil.copyfile(DEMO.with_name("manifest.json"), path.parent / "manifest.json")
    monkeypatch.setattr(api, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("RS_PUBLIC_DEMO", "1")
    monkeypatch.setenv("RS_API_DB_PATH", str(tmp_path / "other"))
    before = digest(path)
    with DuckDBStore(path, read_only=True) as store:
        result = AnalysisEngine(store).analyze("p1")
    with client() as http:
        places = http.get(ROOT + "/places").json()
        assert len(places) == 12 and sum(p["review_count"] for p in places) == 1058
        response = http.get(ROOT + "/places/p1/analysis")
        assert response.status_code == 200
        body = response.json()
        json.dumps(body, allow_nan=False)
        assert body["review_count"] == result.review_count
        assert body["raw_rating"] == result.raw_rating
        assert body["calculated_weighted_rating"] == (
            result.weighted_rating_result.model_dump(mode="json") | {"unit": "stars"}
        )
        assert body["calculated_weighted_rating"]["value"] == result.weighted_rating_value
        assert body["coordinated_activity"] == result.coordinated.model_dump(mode="json")
        assert body["coordinated_activity"]["signals"] and body["coordinated_activity"]["counter_signals"]
        assert body["burst_events"][0]["date"] == result.burst_events[0].date.isoformat()
        assert [a["jsd"] for a in body["rating_anomalies"]] == [a.jsd for a in result.rating_anomalies]
        assert [t["cluster_id"] for t in body["topics"]] == [c.cluster_id for c in result.clusters]
        assert [g["edges"] for g in body["duplicate_groups"]] == [
            [list(e) for e in g.edges] for g in result.duplicate_groups
        ]
    assert digest(path) == before
    with DuckDBStore(path) as store:
        store.connection().execute("DELETE FROM embeddings_cache WHERE review_id = 'r00001'")
    with client() as http:
        assert not http.get(ROOT + "/health").json()["dataset_available"]
    # Even an updated file checksum cannot authenticate an incomplete vector set.
    (path.parent / "manifest.json").write_text(json.dumps({"sha256": digest(path)}))
    with client() as http:
        assert http.get(ROOT + "/places/p1/analysis").status_code == 503


def test_detected_chain_is_not_a_clique(snapshot):
    provider = SentenceTransformerProvider()
    with DuckDBStore(snapshot) as store:
        text = "геометрия"
        store.connection().execute("UPDATE reviews SET text = ? WHERE review_id = 'c'", [text])
        vec = np.zeros(provider.dim)
        vec[:2] = [np.cos(np.deg2rad(46)), np.sin(np.deg2rad(46))]
        store.store_cached_embeddings([(*embedding_key("c", text, provider.contract.encoding_id), vec)])
    with client(snapshot) as http:
        [group] = http.get(ROOT + "/places/p/analysis").json()["duplicate_groups"]
        assert sorted(group["review_ids"]) == ["a", "b", "c"]
        assert {(*sorted((a, b)), k) for a, b, k, _ in group["edges"]} == {
            ("a", "b", "semantic"), ("b", "c", "semantic")
        }


def test_resource_limits(snapshot, monkeypatch):
    monkeypatch.setattr(api, "MAX_ANALYSIS_REVIEWS", 2)
    with client(snapshot) as http:
        res = http.get(ROOT + "/places/p/analysis")
        assert (res.status_code, res.json()) == (413, {"error": "analysis_limit"})
        assert http.get(ROOT + "/places/p/reviews").json()["total"] == 3
    monkeypatch.setattr(api, "MAX_DATASET_REVIEWS", 3)
    with client(snapshot) as http:
        assert http.get(ROOT + "/places").status_code == 503


def test_invalid_score_fails_closed(snapshot, monkeypatch):
    analyze = AnalysisEngine.analyze

    def invalid_score(engine, place_id):
        result = analyze(engine, place_id)
        result.coordinated.details["invalid"] = float("nan")
        return result

    monkeypatch.setattr(AnalysisEngine, "analyze", invalid_score)
    with client(snapshot) as http:
        response = http.get(ROOT + "/places/p/analysis")
        assert (response.status_code, response.json()) == (500, {"error": "internal_error"})


def test_cache_miss_after_preflight_never_encodes(snapshot):
    with DuckDBStore(snapshot, read_only=True) as store:
        cache = EmbeddingCache(store, precomputed_only=True)
        reviews = store.fetch_reviews()
        assert cache.missing_keys(reviews) == []
        changed = reviews[0].model_copy(update={"text": "изменённая синтетика"})
        with pytest.raises(ValueError, match="precomputed embeddings"):
            cache.embed_reviews([changed])


def test_serial_analysis_cleanup_and_redacted_failure(snapshot, monkeypatch, caplog):
    first, release = Event(), Event()
    calls = []
    active = 0
    analyze = AnalysisEngine.analyze

    def tracked(engine, place_id):
        nonlocal active
        active += 1
        try:
            assert active == 1
            calls.append(place_id)
            first.set()
            assert release.wait(5)
            return analyze(engine, place_id)
        finally:
            active -= 1

    monkeypatch.setattr(AnalysisEngine, "analyze", tracked)
    closed = []
    close = DuckDBStore.close

    def track_close(store):
        close(store)
        closed.append(store.read_only)

    monkeypatch.setattr(DuckDBStore, "close", track_close)
    with client(snapshot) as http, ThreadPoolExecutor(max_workers=2) as pool:
        path = ROOT + "/places/p/analysis"
        one = pool.submit(http.get, path)
        assert first.wait(5)
        two = pool.submit(http.get, path)
        release.set()
        assert one.result().status_code == two.result().status_code == 200
        assert calls == ["p", "p"]

        def broken(*args):
            raise RuntimeError(str(snapshot) + " private-review-marker")

        monkeypatch.setattr(AnalysisEngine, "analyze", broken)
        response = http.get(path)
        assert (response.status_code, response.json()) == (500, {"error": "internal_error"})
        assert "private-review-marker" not in caplog.text and str(snapshot) not in caplog.text
    assert closed == [True]
    monkeypatch.setattr(EmbeddingCache, "missing_keys", lambda *args: ["missing"])
    with client(snapshot) as http:
        assert http.get(ROOT + "/places").status_code == 503
        assert closed == [True, True]


def test_backend_free_import_and_request():
    script = """
import sys
class BlockBackend:
    def find_spec(self, name, *args):
        if name.split('.')[0] in {'torch', 'transformers', 'sentence_transformers'}:
            raise AssertionError(name)
sys.meta_path.insert(0, BlockBackend())
from fastapi.testclient import TestClient
from reviewscope.api import create_app
with TestClient(create_app(), base_url='http://localhost') as http:
    assert http.get('/api/v1/places/p12/analysis').status_code == 200
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60,
                   env={**os.environ, "HF_HUB_OFFLINE": "1", "RS_PUBLIC_DEMO": "1"})
