"""Project-scoped authored definitions in the synthetic TLS cabinet."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import store


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Payload(Strict):
    name: str = Field(min_length=1, max_length=120)
    definition: str = Field(min_length=1, max_length=2000)
    include: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=10
    )
    exclude: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=10
    )

    @model_validator(mode="after")
    def valid_text(self):
        examples = self.include + self.exclude
        for value in [self.name, self.definition, *examples]:
            if not value.strip() or "\x00" in value:
                raise ValueError("invalid_text")
            value.encode("utf-8")
        if len(set(examples)) != len(examples):
            raise ValueError("duplicate_example")
        return self


class Create(Strict):
    payload: Payload
    origin: str = Field(
        default="manual",
        pattern=r"^(manual|[a-f0-9]{64}:[a-f0-9]{64}:[a-f0-9]{64}:p[1-9][0-9]?:[0-9]+)$",
        max_length=220,
    )
    idempotency_key: UUID


class Edit(Create):
    expected_version: UUID


class Archive(Strict):
    expected_version: UUID
    idempotency_key: UUID


class Saved(Strict):
    aspect_id: UUID
    version_id: UUID


class Summary(Saved):
    name: str
    version_no: int
    archived: bool


class Frozen(Saved):
    workspace_id: UUID
    project_id: UUID
    version_no: int
    payload: Payload
    digest: str
    origin: str
    created_by: UUID
    created_at: datetime


class Detail(Frozen):
    archived: bool
    current_version_id: UUID


def install(app, authenticated, local, read_contract, write_contract, errors):
    from .http import Page
    from .http import Strict as Empty

    base = "/api/v1/workspaces/{workspace}/projects/{project}/aspects"
    options = dict(openapi_extra=write_contract, responses={200: {"model": Saved}, **errors})
    read = dict(openapi_extra=read_contract, responses=errors)

    def tls(request):
        if local or request.url.scheme != "https":
            raise HTTPException(403)

    def listing(request, w, p, a, offset):
        tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, w, "review_evidence")
            cur.execute("SELECT aspect_scope(%s,%s) AS ok", (w, p))
            if not cur.fetchone()["ok"]:
                raise HTTPException(404)
            cur.execute("SELECT aspect_page(%s,%s,%s,%s) AS data", (w, p, a, offset))
            return [r["data"] for r in cur.fetchall()]

    @app.get(base, **read)
    def catalog(
        workspace: UUID, project: UUID, request: Request, page: Annotated[Page, Query()]
    ) -> list[Summary]:
        return listing(request, workspace, project, None, page.offset)

    @app.get(base + "/{aspect}/versions", **read)
    def versions(
        workspace: UUID,
        project: UUID,
        aspect: UUID,
        request: Request,
        page: Annotated[Page, Query()],
    ) -> list[Summary]:
        return listing(request, workspace, project, aspect, page.offset)

    @app.get(base + "/{aspect}/versions/{version}", **read)
    def detail(
        workspace: UUID,
        project: UUID,
        aspect: UUID,
        version: UUID,
        request: Request,
        _: Annotated[Empty, Query()],
    ) -> Detail:
        tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            cur.execute(
                "SELECT aspect_detail(%s,%s,%s,%s) AS data", (workspace, project, aspect, version)
            )
            result = cur.fetchone()["data"]
            if result is None:
                raise HTTPException(404)
            return result

    def write(request, response, w, p, a, data):
        tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, w, "start_analysis")
            store.private_binds(cur)
            archive = isinstance(data, Archive)
            cur.execute(
                "SELECT * FROM save_aspect(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    w,
                    p,
                    a,
                    getattr(data, "expected_version", None),
                    data.idempotency_key,
                    None if archive else Jsonb(data.payload.model_dump()),
                    None if archive else data.origin,
                    archive,
                ),
            )
            row = cur.fetchone()
            code = row.pop("code")
            if code not in (200, 201):
                raise HTTPException(code)
            response.status_code = code
            return row

    @app.post(base, status_code=201, **options)
    def create(
        workspace: UUID,
        project: UUID,
        data: Create,
        request: Request,
        response: Response,
        _: Annotated[Empty, Query()],
    ) -> Saved:
        return write(request, response, workspace, project, None, data)

    @app.post(base + "/{aspect}/versions", status_code=201, **options)
    def edit(
        workspace: UUID,
        project: UUID,
        aspect: UUID,
        data: Edit,
        request: Request,
        response: Response,
        _: Annotated[Empty, Query()],
    ) -> Saved:
        return write(request, response, workspace, project, aspect, data)

    @app.post(base + "/{aspect}/archive", **options)
    def archive(
        workspace: UUID,
        project: UUID,
        aspect: UUID,
        data: Archive,
        request: Request,
        response: Response,
        _: Annotated[Empty, Query()],
    ) -> Saved:
        return write(request, response, workspace, project, aspect, data)
