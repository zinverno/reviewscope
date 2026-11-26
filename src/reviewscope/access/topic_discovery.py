"""Authenticated topic discovery over immutable, explicitly admitted canonical snapshots."""

from contextlib import contextmanager
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import Field

from . import decisions, signals, store
from .aspects import Payload, Strict
from .evidence import Filters, evidence_page


class Scope(Strict):
    source_id: UUID
    decision_run: UUID | None = None
    human_revision: UUID | None = None


class Publish(Scope):
    idempotency_key: UUID


class Page(Strict):
    source_id: UUID
    candidate_id: UUID | None = None
    historical_snapshot: UUID | None = None
    offset: int = Field(default=0, ge=0, le=10000)
    digest: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    version: UUID | None = None


class Action(Strict):
    source_id: UUID
    idempotency_key: UUID
    action: Literal["merge", "split", "confirm", "reject"]
    expected_open: list[UUID] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=300)
    parts: list[Annotated[list[UUID], Field(min_length=1, max_length=96)]] | None = Field(
        default=None, min_length=2, max_length=8
    )
    aspect_id: UUID | None = None
    aspect_version_id: UUID | None = None
    definition: Payload | None = None


class History(Strict):
    source_id: UUID
    idempotency_key: UUID
    candidate_id: UUID
    historical_snapshot: UUID
    expected_check: UUID | None = None
    version: UUID | None = None
    accept: bool | None = Field(None, strict=True)


def query(cur, sql, args):
    cur.execute(sql, args)
    value = cur.fetchone()["data"]
    if value is None:
        raise HTTPException(404)
    return value


def bounded(doc):
    response = JSONResponse(doc)
    if len(response.body) > 65536:
        raise HTTPException(503)
    return response


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    base = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/topic-discovery"
    read = dict(openapi_extra=read_contract, responses=errors)
    write = dict(openapi_extra=write_contract, responses=errors, status_code=201)

    @contextmanager
    def session(request, workspace, writing=False):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(
                cur, actor, workspace, "start_analysis" if writing else "review_evidence"
            )
            store.private_binds(cur)
            cur.execute("SET LOCAL statement_timeout='10s'")
            yield cur, actor

    def verify(cur, actor, request, w, src, s, binding):
        receipt, _ = signals.broker_receipt(
            cur, actor, w, s, src, s, request, cookie, key, "topic-snapshot-1", "topic_snapshot"
        )
        if not isinstance(receipt, dict) or (
            receipt.get("schema"),
            receipt.get("snapshot_id"),
            receipt.get("manifest"),
            receipt.get("sha256"),
        ) != (
            "topic-snapshot-1",
            str(s),
            binding["manifest_sha256"],
            binding["snapshot_object_sha256"],
        ):
            raise HTTPException(503)
        if receipt.get("source_rows_sha256") != binding["source_rows_sha256"]:
            raise HTTPException(503)
        run = binding.get("decision_run")
        if run:
            result = query(
                cur, "SELECT decision_read(%s,%s,%s,%s,NULL,0,NULL) AS data", (w, src, s, run)
            )
            page = decisions.RunPage.model_validate(result)
            if page.compute_state != "COMPLETE":
                raise HTTPException(404)
            decisions.verify_artifact(cur, actor, w, s, src, run, request, cookie, key, page)
            if "rows_sha256" in binding and (
                binding["rows_sha256"],
                binding["artifact_sha256"],
            ) != (result["rows_sha256"], result["artifact_sha256"]):
                raise HTTPException(503)

    def catalog(cur, actor, request, w, src, s, run=None, revision=None):
        args = (w, src, s, run, revision)
        doc = query(cur, "SELECT topic_catalog(%s,%s,%s,%s,%s) AS data", args)
        if doc["reason"]:
            return {k: doc[k] for k in ("reason", "profile", "N_accepted", "N_input")}
        verify(cur, actor, request, w, src, s, doc | {"decision_run": run})
        return doc

    def page_data(cur, actor, request, w, s, result, q):
        doc = query(
            cur,
            "SELECT topic_read(%s,%s,%s,%s,%s,%s,%s) AS data",
            (w, q.source_id, s, result, q.candidate_id, q.historical_snapshot, q.offset),
        )
        if q.digest and doc["digest"] != q.digest:
            raise HTTPException(404)
        verify(cur, actor, request, w, q.source_id, s, doc["manifest"])
        if q.historical_snapshot:
            historical = catalog(cur, actor, request, w, q.source_id, q.historical_snapshot)
            for event in doc["history"]:
                period = event["receipt"].get("historical_period")
                if period and any(
                    period[k] != historical[k]
                    for k in ("manifest_sha256", "snapshot_object_sha256")
                ):
                    raise HTTPException(503)
        if q.version:
            if not q.candidate_id:
                raise HTTPException(422)
            refs = doc["rows"] + doc["representatives"]
            target = s
            if q.historical_snapshot:
                refs = [hit for e in doc["history"] for hit in e["receipt"].get("hits", [])]
                target = q.historical_snapshot
            ref = next((x for x in refs if x["version_id"] == str(q.version)), None)
            if ref is None:
                raise HTTPException(404)
            row = evidence_page(cur, w, target, Filters(source_id=q.source_id), q.version)["rows"][
                0
            ]
            text = row["full_text"]
            if (
                text is None
                or str(row["version_id"]) != str(q.version)
                or sha256(text.encode()).hexdigest() != ref.get("text_sha", ref.get("text_sha256"))
                or (ref["start"], ref["end"]) != (0, len(text))
            ):
                raise HTTPException(503)
            doc["evidence"] = {
                "version_id": str(q.version),
                "snapshot_id": str(target),
                "text": text,
            }
        return doc

    def saved(doc, response):
        code = doc["code"]
        if code not in (200, 201):
            if code == 413 and doc.get("reason"):
                return JSONResponse(
                    {"error": "RESOURCE_LIMIT", "reason": doc["reason"], "processed": 0},
                    status_code=413,
                )
            raise HTTPException(code)
        response.status_code = code
        return doc

    @app.get(base, **read)
    def options(workspace: UUID, snapshot: UUID, request: Request, q: Annotated[Scope, Query()]):
        with session(request, workspace) as (cur, actor):
            return bounded(
                catalog(
                    cur,
                    actor,
                    request,
                    workspace,
                    q.source_id,
                    snapshot,
                    q.decision_run,
                    q.human_revision,
                )
            )

    @app.post(base, **write)
    def publish(
        workspace: UUID,
        snapshot: UUID,
        request: Request,
        response: Response,
        data: Publish,
        _: Annotated[Strict, Query()],
    ):
        with session(request, workspace, True) as (cur, actor):
            args = (workspace, data.source_id, snapshot, data.decision_run, data.human_revision)
            catalog(cur, actor, request, *args)
            return saved(
                query(
                    cur,
                    "SELECT topic_publish(%s,%s,%s,%s,%s,%s) AS data",
                    (*args, data.idempotency_key),
                ),
                response,
            )

    @app.get(base + "/{result}", **read)
    def page(
        workspace: UUID, snapshot: UUID, result: UUID, request: Request, q: Annotated[Page, Query()]
    ):
        with session(request, workspace) as (cur, actor):
            return bounded(page_data(cur, actor, request, workspace, snapshot, result, q))

    @app.post(base + "/{result}/actions", **write)
    def review(
        workspace: UUID,
        snapshot: UUID,
        result: UUID,
        request: Request,
        response: Response,
        data: Action,
        _: Annotated[Strict, Query()],
    ):
        with session(request, workspace, True) as (cur, actor):
            page_data(
                cur, actor, request, workspace, snapshot, result, Page(source_id=data.source_id)
            )
            doc = query(
                cur,
                "SELECT topic_review(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS data",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    result,
                    data.idempotency_key,
                    data.action,
                    data.expected_open,
                    data.reason,
                    Jsonb(data.model_dump(mode="json")["parts"]) if data.parts else None,
                    data.aspect_id,
                    data.aspect_version_id,
                    Jsonb(data.definition.model_dump()) if data.definition else None,
                ),
            )
            return saved(doc, response)

    @app.post(base + "/{result}/history-checks", **write)
    def history(
        workspace: UUID,
        snapshot: UUID,
        result: UUID,
        request: Request,
        response: Response,
        data: History,
        _: Annotated[Strict, Query()],
    ):
        with session(request, workspace, True) as (cur, actor):
            page_data(
                cur,
                actor,
                request,
                workspace,
                snapshot,
                result,
                Page(
                    source_id=data.source_id,
                    candidate_id=data.candidate_id,
                    historical_snapshot=data.historical_snapshot,
                ),
            )
            doc = query(
                cur,
                "SELECT topic_history_check(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS data",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    result,
                    data.candidate_id,
                    data.historical_snapshot,
                    data.idempotency_key,
                    data.expected_check,
                    data.version,
                    data.accept,
                ),
            )
            return saved(doc, response)
