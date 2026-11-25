"""Authenticated annotations and immutable reports for a completed decision run."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from . import decisions, store
from .aspects import Strict

Label = Literal[
    "complaint",
    "mixed",
    "denial",
    "positive",
    "neutral",
    "unclear",
    "not_mentioned",
    "topic_uncertain",
]
Mode = Literal["all", "uncertain", "disagreement", "sample"]
State = Literal["unreviewed", "confirmed", "corrected", "disputed"]
Action = Literal["confirm", "correct", "dispute"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Page(Strict):
    source_id: UUID
    offset: int = Field(0, ge=0, le=2147483622)


class Queue(Page):
    mode: Mode = "all"
    outcome: Label | None = None
    state: State | None = None
    plan_id: UUID | None = None

    @model_validator(mode="after")
    def require_plan(self):
        if (self.mode == "sample") != (self.plan_id is not None):
            raise ValueError("invalid_plan")
        return self


class Operation(Strict):
    source_id: UUID
    idempotency_key: UUID


class Span(Strict):
    start: int = Field(strict=True, ge=0, le=2147483647)
    end: int = Field(strict=True, gt=0, le=2147483647)


class Append(Operation):
    expected_event_id: UUID | None
    action: Action
    outcome: Label | None
    reason: str = Field(min_length=1, max_length=300)
    spans: list[Span] = Field(max_length=2)
    selection: Mode
    plan_id: UUID | None = None

    @model_validator(mode="after")
    def validate_event(self):
        self.reason.encode("utf-8")
        if not self.reason.strip() or "\x00" in self.reason:
            raise ValueError("invalid_reason")
        if (self.action == "dispute") != (self.outcome is None):
            raise ValueError("invalid_outcome")
        if self.outcome in ("complaint", "mixed") and not self.spans:
            raise ValueError("citation_required")
        if (self.selection == "sample") != (self.plan_id is not None):
            raise ValueError("invalid_plan")
        if any(span.start >= span.end for span in self.spans):
            raise ValueError("invalid_span")
        return self


class Plan(Operation):
    seed: UUID
    size: int = Field(25, strict=True, ge=1, le=10000)


class Saved(Strict):
    code: Literal[200, 201]
    id: UUID


class Member(Strict):
    version_id: UUID
    event_id: UUID | None
    machine: Label
    outcome: Label | None
    state: State


class QueueRow(Member):
    reason: str


class QueuePage(Strict):
    eligible: int
    total: int
    rows: list[QueueRow] = Field(max_length=25)


class Event(Strict):
    id: UUID
    previous_id: UUID | None
    sequence: int
    actor: UUID
    created_at: datetime
    action: Action
    outcome: Label | None
    reason: str
    spans: list[Span] = Field(max_length=2)
    selection: Mode
    plan_id: UUID | None


class History(Strict):
    head: UUID | None
    total: int
    machine: Label
    content_sha256: Digest
    text_sha256: Digest
    events: list[Event] = Field(max_length=25)


class PlanItem(Strict):
    id: UUID
    seed: UUID
    requested: int
    eligible: int
    size: int
    digest: Digest


class Plans(Strict):
    total: int
    items: list[PlanItem] = Field(max_length=25)


class ResultItem(Strict):
    id: UUID
    created_at: datetime


class Results(Strict):
    total: int
    items: list[ResultItem] = Field(max_length=25)


class Counts(Strict):
    eligible: int
    resolved: int
    disputed: int
    uncertain: int
    unreviewed: int
    confirmed: int
    corrected: int
    complaints: int
    reviewed: int
    complaints_among_resolved: float | None


class Result(ResultItem):
    actor: UUID
    digest: Digest
    manifest_sha256: Digest
    rows_sha256: Digest
    artifact_sha256: Digest
    summary: Counts
    total: int
    rows: list[Member] = Field(max_length=25)


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    base = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/decisions/{run}/human"
    read = dict(openapi_extra=read_contract, responses=errors)
    write = dict(
        openapi_extra=write_contract, responses={200: {"model": Saved}, **errors}, status_code=201
    )

    def execute(request, workspace, snapshot, run, source, function, args, model, response=None):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(
                cur, actor, workspace, "start_analysis" if response is not None else "review_evidence"
            )
            if response is not None:
                store.private_binds(cur)
            scope = (workspace, source, snapshot, run)
            # Functions are fixed by the routes. Scope and all client values are bound.
            params = (*scope, *args)
            binds = ",".join(["%s"] * len(params))
            cur.execute(f"SELECT {function}({binds}) AS data", params)
            result = cur.fetchone()["data"]
            if result is None:
                raise HTTPException(404)
            # Check the actual broker object before returning private data or committing a write.
            cur.execute("SELECT decision_read(%s,%s,%s,%s,NULL,0,NULL) AS data", scope)
            baseline = cur.fetchone()["data"]
            if not baseline or baseline["compute_state"] != "COMPLETE":
                raise HTTPException(404)
            page = decisions.RunPage.model_validate(baseline)
            decisions.verify_artifact(
                cur, actor, workspace, snapshot, source, run, request, cookie, key, page
            )
            if response is not None:
                if result["code"] not in (200, 201):
                    raise HTTPException(result["code"])
                response.status_code = result["code"]
            value = model.model_validate(result)
            if len(value.model_dump_json().encode()) > 65536:
                raise HTTPException(503)
            return value

    @app.get(base + "/queue", **read)
    def queue(
        workspace: UUID, snapshot: UUID, run: UUID, request: Request, q: Annotated[Queue, Query()]
    ) -> QueuePage:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            q.source_id,
            "human_queue",
            (q.mode, q.outcome, q.state, q.plan_id, q.offset),
            QueuePage,
        )

    @app.get(base + "/reviews/{version}/history", **read)
    def history(
        workspace: UUID,
        snapshot: UUID,
        run: UUID,
        version: UUID,
        request: Request,
        q: Annotated[Page, Query()],
    ) -> History:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            q.source_id,
            "human_history",
            (version, q.offset),
            History,
        )

    @app.post(base + "/reviews/{version}/events", **write)
    def append(
        workspace: UUID,
        snapshot: UUID,
        run: UUID,
        version: UUID,
        request: Request,
        response: Response,
        data: Append,
        _: Annotated[Strict, Query()],
    ) -> Saved:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            data.source_id,
            "human_append",
            (
                version,
                data.idempotency_key,
                data.expected_event_id,
                data.action,
                data.outcome,
                data.reason,
                Jsonb([span.model_dump() for span in data.spans]),
                data.selection,
                data.plan_id,
            ),
            Saved,
            response,
        )

    @app.get(base + "/plans", **read)
    def plans(
        workspace: UUID, snapshot: UUID, run: UUID, request: Request, q: Annotated[Page, Query()]
    ) -> Plans:
        return execute(
            request, workspace, snapshot, run, q.source_id, "human_plans", (q.offset,), Plans
        )

    @app.post(base + "/plans", **write)
    def plan(
        workspace: UUID,
        snapshot: UUID,
        run: UUID,
        request: Request,
        response: Response,
        data: Plan,
        _: Annotated[Strict, Query()],
    ) -> Saved:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            data.source_id,
            "human_plan",
            (data.idempotency_key, data.seed, data.size),
            Saved,
            response,
        )

    @app.get(base + "/results", **read)
    def results(
        workspace: UUID, snapshot: UUID, run: UUID, request: Request, q: Annotated[Page, Query()]
    ) -> Results:
        return execute(
            request, workspace, snapshot, run, q.source_id, "human_results", (q.offset,), Results
        )

    @app.post(base + "/results", **write)
    def publish(
        workspace: UUID,
        snapshot: UUID,
        run: UUID,
        request: Request,
        response: Response,
        data: Operation,
        _: Annotated[Strict, Query()],
    ) -> Saved:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            data.source_id,
            "human_publish",
            (data.idempotency_key,),
            Saved,
            response,
        )

    @app.get(base + "/results/{revision}", **read)
    def result(
        workspace: UUID,
        snapshot: UUID,
        run: UUID,
        revision: UUID,
        request: Request,
        q: Annotated[Page, Query()],
    ) -> Result:
        return execute(
            request,
            workspace,
            snapshot,
            run,
            q.source_id,
            "human_result",
            (revision, q.offset),
            Result,
        )
