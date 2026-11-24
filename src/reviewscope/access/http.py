"""Local access API, independent of the anonymous DuckDB application."""

import hashlib
import json
import logging
import os
import re
import secrets
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import aspects, candidate_search, evidence, reviewer_context, signals, store, uploads
from .security import normalize, password_proof, server_key, session_proof

LOG = logging.getLogger("reviewscope.access.requests")
ROOT = "/api/v1"
ERRORS = {
    400: "invalid_host",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "job_conflict",
    413: "request_too_large",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "access_unavailable",
}


def error(status):
    return JSONResponse({"error": ERRORS.get(status, "invalid_request")}, status_code=status)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Strict):
    offset: int = Field(0, ge=0, le=2**31 - 1)


class Login(Strict):
    login: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=256)


class RetentionRequest(Strict):
    kind: Literal["source", "project", "workspace"]
    target: UUID
    action: Literal["export", "erase"]
    idempotency_key: UUID


class SourcePolicy(Strict):
    data_class: Literal[
        "raw",
        "configuration",
        "positions",
        "versions",
        "snapshot",
        "temporary",
        "derivatives",
        "report_export",
        "diagnostics",
        "backup",
    ]
    authority_ref: UUID
    anchor: Literal["received", "registered", "created"]
    days: int = Field(ge=1, le=36500)
    review_days: int = Field(ge=1, le=3650)
    hold_ref: UUID | None


class Sweep(Strict):
    idempotency_key: UUID


class Membership(Strict):
    role: Literal["owner", "analyst", "viewer"]
    state: Literal["active", "inactive"]


class Workspace(Strict):
    role: Literal["owner", "analyst", "viewer"]
    state: Literal["active", "archived"]
    id: UUID
    name: str


class Session(Strict):
    principal_id: UUID
    workspaces: list[Workspace]


class Source(Strict):
    id: UUID
    kind: str


class Snapshot(Strict):
    id: UUID
    source_id: UUID
    created_at: datetime


class Run(Strict):
    id: UUID
    snapshot_id: UUID
    status: str


class JobRequest(Strict):
    snapshot_id: UUID
    idempotency_key: UUID
    job_kind: str = Field(min_length=1, max_length=64, description="snapshot_integrity_and_summary_v1 or snapshot_explainable_signals_v1 on admitted synthetic snapshots")
    engine_version: str = Field(min_length=1, max_length=32)


class JobStatus(Run):
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    job_kind: str
    engine_version: str
    definition_version: str
    model_version: str
    attempt: int
    stage: Literal["queued", "verify", "summarize", "publish"]
    processed: int
    total: int | None
    reason: (
        Literal[
            "CANCELLED",
            "ACCESS_REVOKED",
            "LEASE_EXPIRED",
            "STORAGE_UNAVAILABLE",
            "WORKER_STOPPED",
            "INTEGRITY_ERROR",
            "UNSUPPORTED_FORMAT",
            "RESOURCE_LIMIT",
            "UNSUPPORTED_VERSION",
            "INTERNAL_ERROR",
        ]
        | None
    )
    cancel_requested: bool
    finished_at: datetime | None
    artifact_id: UUID | None
    sha256: str | None
    size_bytes: int | None


class Count(Strict):
    numerator: int
    denominator: int


class Metrics(Strict):
    counts: dict[str, Count]
    eligibility: dict[str, Count]
    rating_distribution: dict[str, Count] | None


class SummarySelection(Strict):
    run_id: UUID | None = None
    source_id: UUID | None = None


class SummaryContext(Strict):
    source_id: UUID
    batch_id: UUID
    coverage: Literal["partial", "full", "unknown"] | None
    covered_from: datetime | None
    covered_until: datetime | None
    selection: Literal["accepted_in_delivery"]
    delivery_exclusions: dict[str, Count]
    metric_exclusions: dict[str, Count]


class JobSummary(JobStatus):
    metrics: Metrics | None
    context: SummaryContext | None = None


class JobErrorResponse(Strict):
    error: str


def create_app():
    engine = store.pool()
    local = os.environ.get("RS_ACCESS_HTTP_LOOPBACK") == "1"
    origin = ("http" if local else "https") + "://127.0.0.1:8001"
    cookie = "rs_local_session" if local else "__Host-rs_session"
    cookie_flags = dict(path="/", secure=not local, httponly=True, samesite="strict")
    attempts = deque()
    limiter = Lock()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="ReviewScope local workspace access",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error(exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        if any(
            e["type"].startswith("uuid_")
            and (e["loc"][0] == "path" or e["loc"] == ("body", "snapshot_id"))
            for e in exc.errors()
        ):
            return error(404)
        return error(422)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        start = monotonic()
        request_id = str(uuid4())
        try:
            if (
                request.headers.getlist("host") != ["127.0.0.1:8001"]
                or request.url.scheme != origin.split(":")[0]
                or (
                    local
                    and (not request.client or request.client.host not in {"127.0.0.1", "::1"})
                )
            ):
                raise HTTPException(400)
            changing = request.method not in {"GET", "HEAD", "OPTIONS"}
            if changing and (
                request.headers.getlist("origin") != [origin]
                or request.headers.getlist("x-rs-csrf") != ["1"]
            ):
                raise HTTPException(403)
            if len(request.query_params.multi_items()) != len(request.query_params):
                raise HTTPException(422)
            # Bound even unframed/chunked input before handing JSON to the framework.
            if not uploads.binary_route(request):
                aspect_write = changing and re.fullmatch(
                    r"/api/v1/workspaces/[0-9a-fA-F-]+/projects/[0-9a-fA-F-]+/aspects(?:/[0-9a-fA-F-]+/(?:versions|archive))?",
                    request.url.path,
                )
                body = bytearray()
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > (65536 if aspect_write else 4096):
                        raise HTTPException(413)
                    if chunk and (
                        not changing or request.url.path.endswith(("/logout", "/cancel"))
                    ):
                        raise HTTPException(422)
                    if chunk:
                        body.extend(chunk)
                if (
                    size
                    and request.headers.get("content-type", "").split(";")[0] != "application/json"
                ):
                    raise HTTPException(422)
                if body and (aspect_write or request.url.path.endswith("/candidates")):
                    def unique_fields(pairs):
                        fields = dict(pairs)
                        if len(fields) != len(pairs):
                            raise ValueError("duplicate_field")
                        return fields
                    try:
                        json.loads(body, object_pairs_hook=unique_fields)
                    except (ValueError, RecursionError):
                        raise HTTPException(422) from None
                request._body = bytes(body)
            response = await call_next(request)
        except StarletteHTTPException as exc:
            response = error(exc.status_code)
        except Exception:
            response = error(500)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        LOG.info(
            json.dumps(
                dict(
                    event="http_request_completed",
                    request_id=request_id,
                    route_template=route.path if route else None,
                    analysis_execution_id=None,
                    status=response.status_code,
                    duration_ms=round((monotonic() - start) * 1000, 3),
                    error=ERRORS.get(response.status_code),
                )
            )
        )
        return response

    def key():
        try:
            return server_key()
        except (ValueError, OSError, KeyError):
            raise HTTPException(503) from None

    @contextmanager
    def authenticated(request):
        token = request.cookies.get(cookie, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(401)
        proof = session_proof(token, key())
        with store.transaction(engine) as cur:
            yield cur, store.identify(cur, proof)

    @app.post(ROOT + "/session/login", status_code=204)
    def login(data: Login, response: Response, _: Annotated[Strict, Query()]):
        secret = key()
        with limiter:
            now = monotonic()
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 5:
                raise HTTPException(429)
            attempts.append(now)
        try:
            account = normalize(data.login)
        except ValueError:
            account = ""
        with store.transaction(engine) as cur:
            cur.execute("SELECT login_salt(%s) AS salt", (account,))
            salt = cur.fetchone()["salt"] or secrets.token_bytes(16)
            proof = password_proof(data.password.get_secret_value(), salt, secret)
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(session_proof(token, secret).encode()).digest()
            cur.execute("SELECT open_session(%s,%s,%s) AS ok", (account, proof, digest))
            if not cur.fetchone()["ok"]:
                raise HTTPException(401)
        response.set_cookie(cookie, token, max_age=8 * 3600, **cookie_flags)

    @app.post(ROOT + "/session/logout", status_code=204)
    def logout(request: Request, response: Response, _: Annotated[Strict, Query()]):
        with authenticated(request) as (cur, _principal):
            cur.execute("SELECT close_session()")
        response.delete_cookie(cookie, **cookie_flags)

    @app.get(ROOT + "/session")
    def session(request: Request, page: Annotated[Page, Query()]) -> Session:
        with authenticated(request) as (cur, principal):
            return Session(
                principal_id=principal, workspaces=store.workspaces(cur, principal, page.offset)
            )

    @app.get(ROOT + "/workspaces")
    def workspaces(request: Request, page: Annotated[Page, Query()]) -> list[Workspace]:
        with authenticated(request) as (cur, principal):
            return store.workspaces(cur, principal, page.offset)

    def register_metadata(resource, model):
        path = ROOT + "/workspaces/{workspace}/" + resource

        @app.get(path, name="list_" + resource, response_model=list[model])
        def listing(workspace: UUID, request: Request, page: Annotated[Page, Query()]):
            with authenticated(request) as (cur, principal):
                store.authorize(cur, principal, workspace, "read")
                return store.metadata(cur, resource, workspace, None, page.offset)

        @app.get(path + "/{object_id}", name="get_" + resource, response_model=model)
        def detail(
            workspace: UUID, object_id: UUID, request: Request, _: Annotated[Strict, Query()]
        ):
            with authenticated(request) as (cur, principal):
                store.authorize(cur, principal, workspace, "read")
                return store.metadata(cur, resource, workspace, object_id, 0)

    for resource, model in (("sources", Source), ("snapshots", Snapshot), ("runs", Run)):
        register_metadata(resource, model)

    job_path = ROOT + "/workspaces/{workspace}/runs"
    errors = {
        status: {"model": JobErrorResponse, "description": ERRORS[status]}
        for status in (400, 401, 403, 404, 409, 413, 422, 429, 503)
    }

    session_parameter = {
        "name": cookie,
        "in": "cookie",
        "required": True,
        "schema": {"type": "string"},
    }
    read_contract = {"parameters": [session_parameter]}
    write_contract = {
        "parameters": [
            session_parameter,
            {
                "name": "Origin",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "enum": [origin]},
            },
            {
                "name": "X-RS-CSRF",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "enum": ["1"]},
            },
        ]
    }

    @app.post(
        job_path,
        status_code=202,
        responses={200: {"model": JobStatus}, **errors},
        openapi_extra=write_contract,
    )
    def enqueue(
        workspace: UUID,
        data: JobRequest,
        request: Request,
        response: Response,
        _: Annotated[Strict, Query()],
    ) -> JobStatus:
        if data.job_kind == signals.KIND and (local or request.url.scheme != "https"):
            raise HTTPException(403)
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "start_analysis")
            cur.execute(
                "SELECT * FROM enqueue_job(%s,%s,%s,%s,%s)",
                (
                    workspace,
                    data.snapshot_id,
                    data.idempotency_key,
                    data.job_kind,
                    data.engine_version,
                ),
            )
            result = cur.fetchone()
            if result["code"] not in (200, 202):
                raise HTTPException(result["code"])
            response.status_code = result["code"]
            return store.job_status(cur, workspace, run=result["run_id"])

    @app.get(job_path + "/{run_id}/status", responses=errors, openapi_extra=read_contract)
    def job_status(
        workspace: UUID, run_id: UUID, request: Request, _: Annotated[Strict, Query()]
    ) -> JobStatus:
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "read")
            return store.job_status(cur, workspace, run=run_id)

    @app.post(job_path + "/{run_id}/cancel", responses=errors, openapi_extra=write_contract)
    def cancel(
        workspace: UUID, run_id: UUID, request: Request, _: Annotated[Strict, Query()]
    ) -> JobStatus:
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "start_analysis")
            cur.execute("SELECT cancel_job(%s,%s) AS status", (workspace, run_id))
            status = cur.fetchone()["status"]
            if status != 200:
                raise HTTPException(status)
            return store.job_status(cur, workspace, run=run_id)

    @app.get(
        ROOT + "/workspaces/{workspace}/snapshots/{snapshot_id}/latest-summary",
        responses=errors,
        openapi_extra=read_contract,
    )
    def latest_summary(
        workspace: UUID, snapshot_id: UUID, request: Request, _: Annotated[Strict, Query()]
    ) -> JobStatus:
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "read")
            return store.job_status(cur, workspace, snapshot=snapshot_id)

    @app.get(
        ROOT + "/workspaces/{workspace}/snapshots/{snapshot_id}/summary-metrics",
        responses=errors,
        openapi_extra=read_contract,
    )
    def summary_metrics(
        workspace: UUID, snapshot_id: UUID, request: Request, selection: Annotated[SummarySelection, Query()]
    ) -> JobSummary:
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "read")
            job = store.job_status(cur, workspace, snapshot=snapshot_id, run=selection.run_id)
            if job["snapshot_id"] != snapshot_id or job["job_kind"] != "snapshot_integrity_and_summary_v1":
                raise HTTPException(404)
            if selection.source_id is not None:
                cur.execute(
                    "SELECT 1 FROM snapshot WHERE workspace_id=%s AND id=%s AND source_id=%s",
                    (workspace, snapshot_id, selection.source_id),
                )
                if cur.fetchone() is None:
                    raise HTTPException(404)
            cur.execute("SELECT version_num FROM alembic_version")
            revision = cur.fetchone()["version_num"]
            projection = {"metrics": None, "context": None}
            if revision != "0006_retention" and job["artifact_id"] is not None:
                columns = "metrics, context" if revision in {"0009_overview", "0010_signals", "0011_reviewer_context", "0012_candidate_search", "0013_reviewer_context_plan", "0014_aspects"} else "metrics"
                cur.execute(
                    f"SELECT {columns} FROM browser_summary WHERE workspace_id=%s AND run_id=%s AND artifact_id=%s",
                    (workspace, job["id"], job["artifact_id"]),
                )
                projection.update(cur.fetchone() or {})
            return job | projection

    retention_path = ROOT + "/workspaces/{workspace}/retention"

    @app.post(retention_path, openapi_extra=write_contract)
    def retention_request(
        workspace: UUID, data: RetentionRequest, request: Request, _: Annotated[Strict, Query()]
    ):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "retention")
            cur.execute(
                "SELECT request_retention(%s,%s,%s,%s,%s) AS result",
                (workspace, data.kind, data.target, data.action, data.idempotency_key),
            )
            value = cur.fetchone()["result"]
            if value is None:
                raise HTTPException(404)
            if value.get("conflict"):
                raise HTTPException(409)
            return value

    @app.get(retention_path + "/{request_id}", openapi_extra=read_contract)
    def retention_status(
        workspace: UUID, request_id: UUID, request: Request, _: Annotated[Strict, Query()]
    ):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "retention")
            cur.execute("SELECT retention_status(%s,%s) AS result", (workspace, request_id))
            value = cur.fetchone()["result"]
            if value is None:
                raise HTTPException(404)
            return value

    @app.get(
        ROOT + "/workspaces/{workspace}/sources/{source_id}/retention", openapi_extra=read_contract
    )
    def terms(workspace: UUID, source_id: UUID, request: Request, page: Annotated[Page, Query()]):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "read")
            store.metadata(cur, "sources", workspace, source_id, 0)
            cur.execute(
                "SELECT * FROM retention_terms(%s,%s) AS term LIMIT 50 OFFSET %s",
                (workspace, source_id, page.offset),
            )
            return [row["term"] for row in cur.fetchall()]

    policy_path = ROOT + "/workspaces/{workspace}/sources/{source_id}/retention"

    @app.put(policy_path, status_code=204, openapi_extra=write_contract)
    def approve_policy(
        workspace: UUID,
        source_id: UUID,
        data: SourcePolicy,
        request: Request,
        _: Annotated[Strict, Query()],
    ):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "retention")
            cur.execute(
                "SELECT approve_policy(%s,%s,%s,%s,%s,%s,%s,%s) AS ok",
                (
                    workspace,
                    source_id,
                    data.data_class,
                    data.authority_ref,
                    data.anchor,
                    data.days,
                    data.review_days,
                    data.hold_ref,
                ),
            )
            if not cur.fetchone()["ok"]:
                raise HTTPException(404)

    @app.post(policy_path + "/sweep", openapi_extra=write_contract)
    def expiry_preview(
        workspace: UUID,
        source_id: UUID,
        data: Sweep,
        request: Request,
        _: Annotated[Strict, Query()],
    ):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "retention")
            cur.execute(
                "SELECT retention_sweep(%s,%s,%s) AS result",
                (workspace, source_id, data.idempotency_key),
            )
            result = cur.fetchone()["result"]
            if result is None:
                raise HTTPException(404)
            if result.get("conflict"):
                raise HTTPException(409)
            return result

    @app.patch(ROOT + "/workspaces/{workspace}/members/{subject}", status_code=204)
    def change(
        workspace: UUID,
        subject: UUID,
        data: Membership,
        request: Request,
        _: Annotated[Strict, Query()],
    ):
        with authenticated(request) as (cur, principal):
            store.authorize(cur, principal, workspace, "members")
            cur.execute(
                "SELECT change_member(%s,%s,%s,%s) AS status",
                (workspace, subject, data.role, data.state),
            )
            status = cur.fetchone()["status"]
            if status != 204:
                raise HTTPException(status)

    uploads.install(app, authenticated, key, cookie, local)
    aspects.install(app, authenticated, local, read_contract, write_contract, errors)
    candidate_search.install(app, authenticated, key, cookie, local, read_contract, write_contract, errors)
    signals.install(app, authenticated, key, cookie, local, read_contract, errors)
    evidence.install(app, authenticated, local, read_contract, errors)
    reviewer_context.install(app, authenticated, local, read_contract, errors)
    return app
