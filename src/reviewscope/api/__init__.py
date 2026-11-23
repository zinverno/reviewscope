"""Local HTTP boundary over a single operator-selected, immutable DuckDB snapshot."""

import hashlib
import json
import os
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC
from pathlib import Path
from threading import Lock
from typing import Annotated

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.api.models import Analysis, Error, Health, Place, Review, ReviewPage
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.public_demo import DEFAULT_DEMO_DB, ENV_FLAG, public_demo_enabled, repo_root
from reviewscope.storage import DuckDBStore

MAX_DATASET_REVIEWS = 5000
MAX_ANALYSIS_REVIEWS = 256

ERRORS = {
    400: "invalid_host",
    404: "not_found",
    405: "method_not_allowed",
    413: "analysis_limit",
    422: "invalid_request",
    500: "internal_error",
    503: "dataset_unavailable",
}


def _error(status: int, headers=None) -> JSONResponse:
    return JSONResponse({"error": ERRORS[status]}, status_code=status, headers=headers)


class NoQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageParams(NoQuery):
    limit: int = Field(25, ge=1, le=100)
    offset: int = Field(0, ge=0, le=2**31 - 1)


async def _empty_body(request: Request) -> None:
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise HTTPException(422)
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422)


def create_app(*, db_path: Path | None = None) -> FastAPI:
    """A Python path argument or RS_API_DB_PATH selects a trusted local snapshot."""
    local_path = db_path if db_path is not None else os.environ.get("RS_API_DB_PATH")
    demo = local_path is None or (ENV_FLAG in os.environ and public_demo_enabled())
    path = repo_root() / DEFAULT_DEMO_DB if demo else Path(local_path)
    lock = Lock()
    store: DuckDBStore | None = None
    engine: AnalysisEngine | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal store, engine
        with ExitStack() as resources:
            try:
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("expected local snapshot")
                if demo:
                    manifest = json.loads(path.with_name("manifest.json").read_text())
                    with path.open("rb") as data:
                        if hashlib.file_digest(data, "sha256").hexdigest() != manifest["sha256"]:
                            raise ValueError("demo integrity")
                source = resources.enter_context(DuckDBStore(path, read_only=True))
                source.connection().execute("SET enable_external_access = false")
                source.connection().execute("SET TimeZone = 'UTC'")
                source.connection().execute("""
                    SELECT review_id, place_id, reviewer_id, place_name, place_category,
                           latitude, longitude, text, rating, published_at FROM reviews LIMIT 0
                """)
                source.connection().execute("""
                    SELECT review_id, text_hash, model_name, embedding FROM embeddings_cache LIMIT 0
                """)
                if source.review_count() > MAX_DATASET_REVIEWS:
                    raise ValueError("dataset limit")
                cache = EmbeddingCache(source, precomputed_only=True)
                if cache.missing_keys(source.fetch_reviews()):
                    raise ValueError("incomplete vectors")
                engine = AnalysisEngine(source, embedding_cache=cache)
                store = source
            except (OSError, duckdb.Error, ValueError, KeyError):
                resources.close()
            try:
                yield
            finally:
                store = engine = None

    app = FastAPI(
        title="ReviewScope local API", version="1", lifespan=lifespan,
        docs_url=None, redoc_url=None, redirect_slashes=False,
        dependencies=[Depends(_empty_body)],
        responses={status: {"model": Error, "description": code} for status, code in ERRORS.items()},
    )

    @app.middleware("http")
    async def private_response(request: Request, call_next):
        try:
            if request.url.hostname not in {"127.0.0.1", "localhost"}:
                return _error(400)
            response = await call_next(request)
        except Exception:
            # ASGI servers otherwise log exception messages containing paths or raw input.
            return _error(500)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return _error(exc.status_code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return _error(422)

    def source() -> DuckDBStore:
        if store is None:
            raise HTTPException(503)
        return store

    def count(place_id: str) -> int:
        total = source().place_counts(place_id)
        if total == 0:
            raise HTTPException(404)
        return total

    @app.get("/api/v1/health")
    def health(_: Annotated[NoQuery, Query()]) -> Health:
        return Health(status="ok" if store is not None else "degraded", dataset_available=store is not None)

    @app.get("/api/v1/places")
    def places(_: Annotated[NoQuery, Query()]) -> list[Place]:
        with lock:
            rows = source().connection().execute("""
                SELECT place_id, min(place_name), min(place_category), count(*),
                       first([latitude, longitude] ORDER BY review_id) FILTER (
                           WHERE latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)
                FROM reviews GROUP BY place_id ORDER BY place_id
            """).fetchall()
        return [
            Place(place_id=place_id, name=name, category=category, review_count=total, coordinates=coords)
            for place_id, name, category, total, coords in rows
        ]

    @app.get("/api/v1/places/{place_id}/reviews")
    def reviews(place_id: str, page: Annotated[PageParams, Query()]) -> ReviewPage:
        with lock:
            total = count(place_id)
            rows = source().connection().execute("""
                SELECT review_id, text, rating,
                       CASE WHEN year(ts) BETWEEN 1 AND 9999 THEN ts AT TIME ZONE 'UTC' END AS published
                FROM (SELECT *, CASE WHEN regexp_matches(published_at, '^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T]|$)')
                                     THEN try_cast(published_at AS TIMESTAMPTZ) END AS ts
                      FROM reviews WHERE place_id = ?)
                ORDER BY published DESC NULLS LAST, review_id ASC LIMIT ? OFFSET ?
            """, [place_id, page.limit, page.offset]).fetchall()
        return ReviewPage(
            place_id=place_id, total=total, limit=page.limit, offset=page.offset,
            reviews=[
                Review(review_id=review_id, text=text, rating=rating,
                       published_at=published.replace(tzinfo=UTC) if published else None)
                for review_id, text, rating, published in rows
            ],
        )

    @app.get("/api/v1/places/{place_id}/analysis")
    def analysis(place_id: str, _: Annotated[NoQuery, Query()]) -> Analysis:
        with lock:
            if count(place_id) > MAX_ANALYSIS_REVIEWS:
                raise HTTPException(413)
            assert engine is not None  # source and engine are published together
            return Analysis.from_core(engine.analyze(place_id))

    return app
