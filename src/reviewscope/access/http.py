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

from . import store
from .security import normalize, password_proof, server_key, session_proof

LOG = logging.getLogger("reviewscope.access.requests")
ROOT = "/api/v1"
ERRORS = {
    400: "invalid_host",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
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
            body = bytearray()
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 4096:
                    raise HTTPException(413)
                if chunk and (not changing or request.url.path.endswith("/logout")):
                    raise HTTPException(422)
                if chunk:
                    body.extend(chunk)
            if size and request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise HTTPException(422)
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

    return app
