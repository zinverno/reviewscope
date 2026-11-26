"""Manual, version-bound monitor commands in the authenticated cabinet."""

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import Field

from . import aspects, signals, store


class Scope(aspects.Strict):
    source_id: UUID


class Selection(Scope):
    version_id: UUID | None = None
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class Definition(Scope):
    name: str = Field(min_length=1, max_length=240)
    aspect_id: UUID
    aspect_version_id: UUID
    selection: Literal["accepted_in_delivery"]
    threshold_count: int = Field(ge=1, le=10000, strict=True)
    min_eligible: int = Field(ge=1, le=10000, strict=True)


class Edit(Definition):
    expected_version_id: UUID


class Archive(Scope):
    expected_version_id: UUID


class Start(Scope):
    version_id: UUID
    snapshot_id: UUID
    parent_run_id: UUID | None = None
    idempotency_key: UUID


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    from .http import Strict as Empty

    base = "/api/v1/workspaces/{workspace}/projects/{project}/monitors"
    read = dict(openapi_extra=read_contract, responses=errors)
    write = dict(openapi_extra=write_contract, responses=errors)

    def authorize(cur, actor, request, workspace, action):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        store.authorize(cur, actor, workspace, action)
        store.private_binds(cur)
        if not cur.execute(
            "SELECT to_regprocedure('monitor_parent(uuid,uuid,uuid)') IS NOT NULL AS ok"
        ).fetchone()["ok"]:
            raise HTTPException(503)

    def checked(doc):
        if doc is None:
            raise HTTPException(404)
        if len(json.dumps(doc, ensure_ascii=False).encode()) > 65536:
            raise HTTPException(503)
        return doc

    def command(response, result):
        code = result["code"]
        if code not in (200, 201, 202):
            if result.get("reason"):
                from fastapi.responses import JSONResponse

                return JSONResponse({"error": result["reason"]}, status_code=code)
            raise HTTPException(code)
        response.status_code = code
        return result

    def page(cur, w, p, src, m, v, section, offset=0, run=None):
        row = cur.execute(
            "SELECT monitor_page(%s,%s,%s,%s,%s,%s,%s,%s) AS data",
            (w, p, src, m, v, section, offset, run),
        ).fetchone()
        return checked(row["data"])

    def verify_snapshot(cur, actor, request, w, src, s):
        doc, _ = signals.broker_receipt(
            cur, actor, w, s, src, s, request, cookie, key, "monitor-snapshot-1", "monitor_snapshot"
        )
        if doc.get("schema") != "monitor-snapshot-1" or doc.get("snapshot_id") != str(s):
            raise HTTPException(503)
        return doc

    def verify_run(cur, actor, request, w, src, data):
        s = UUID(data["snapshot_id"])
        if verify_snapshot(cur, actor, request, w, src, s)["manifest"] != data["manifest_sha256"]:
            raise HTTPException(503)
        receipt = data["receipt"]
        if data["status"] == "succeeded":
            doc, _ = signals.read_artifact(
                cur,
                actor,
                w,
                s,
                src,
                data["decision_run_id"],
                request,
                cookie,
                key,
                "snapshot_aspect_decision_v1",
                "aspect-decision-1",
                "read_decisions",
                "aspect-decision-1",
            )
            if not receipt or doc.get("rows_sha256") != receipt["rows_sha256"]:
                raise HTTPException(503)
            if receipt["parent_snapshot_id"]:
                parent_snapshot = UUID(receipt["parent_snapshot_id"])
                if (
                    verify_snapshot(cur, actor, request, w, src, parent_snapshot)["manifest"]
                    != receipt["parent_manifest_sha256"]
                ):
                    raise HTTPException(503)
                parent, digest = signals.read_artifact(
                    cur,
                    actor,
                    w,
                    parent_snapshot,
                    src,
                    receipt["parent_decision_run_id"],
                    request,
                    cookie,
                    key,
                    "snapshot_aspect_decision_v1",
                    "aspect-decision-1",
                    "read_decisions",
                    "aspect-decision-1",
                )
                if (
                    digest != receipt["parent_artifact_sha256"]
                    or parent.get("rows_sha256") != receipt["parent_rows_sha256"]
                ):
                    raise HTTPException(503)

    def listing(request, w, p, q, m, section, run=None):
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, w, "review_evidence")
            if run:
                detail = page(cur, w, p, q.source_id, m, q.version_id, "run", run=run)
                verify_run(cur, actor, request, w, q.source_id, detail)
                if section == "run":
                    return detail
            return page(cur, w, p, q.source_id, m, q.version_id, section, q.offset, run)

    @app.get(base, **read)
    def catalog(workspace: UUID, project: UUID, request: Request, q: Annotated[Selection, Query()]):
        return listing(request, workspace, project, q, None, "catalog")

    @app.get(base + "/{monitor}", **read)
    def overview(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        request: Request,
        q: Annotated[Selection, Query()],
    ):
        return listing(request, workspace, project, q, monitor, "overview")

    @app.get(base + "/{monitor}/runs/{run}", **read)
    def detail(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        run: UUID,
        request: Request,
        q: Annotated[Selection, Query()],
    ):
        return listing(request, workspace, project, q, monitor, "run", run)

    @app.get(base + "/{monitor}/runs/{run}/delta", **read)
    def delta(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        run: UUID,
        request: Request,
        q: Annotated[Selection, Query()],
    ):
        return listing(request, workspace, project, q, monitor, "delta", run)

    @app.get(base + "/{monitor}/{section}", **read)
    def pages(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        section: Literal["versions", "snapshots", "history"],
        request: Request,
        q: Annotated[Selection, Query()],
    ):
        return listing(request, workspace, project, q, monitor, section)

    def save(w, p, m, request, response, data, archive):
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, w, "start_analysis")
            values = [
                getattr(data, field, None)
                for field in (
                    "expected_version_id",
                    "aspect_id",
                    "aspect_version_id",
                    "name",
                    "threshold_count",
                    "min_eligible",
                    "selection",
                )
            ]
            result = cur.execute(
                "SELECT monitor_save(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS data",
                (w, p, data.source_id, m, *values, archive),
            ).fetchone()["data"]
            return command(response, result)

    @app.post(base, status_code=201, **write)
    def create(
        workspace: UUID,
        project: UUID,
        request: Request,
        response: Response,
        data: Definition,
        _: Annotated[Empty, Query()],
    ):
        return save(workspace, project, None, request, response, data, False)

    @app.post(base + "/{monitor}/versions", status_code=201, **write)
    def edit(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        request: Request,
        response: Response,
        data: Edit,
        _: Annotated[Empty, Query()],
    ):
        return save(workspace, project, monitor, request, response, data, False)

    @app.post(base + "/{monitor}/archive", **write)
    def archive(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        request: Request,
        response: Response,
        data: Archive,
        _: Annotated[Empty, Query()],
    ):
        return save(workspace, project, monitor, request, response, data, True)

    @app.post(base + "/{monitor}/runs", status_code=202, **write)
    def start(
        workspace: UUID,
        project: UUID,
        monitor: UUID,
        request: Request,
        response: Response,
        data: Start,
        _: Annotated[Empty, Query()],
    ):
        # Release broker/read authority before entering the writer transaction on either SQL path.
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, workspace, "start_analysis")
            verify_snapshot(cur, actor, request, workspace, data.source_id, data.snapshot_id)
            if data.parent_run_id:
                parent = page(
                    cur,
                    workspace,
                    project,
                    data.source_id,
                    monitor,
                    data.version_id,
                    "run",
                    run=data.parent_run_id,
                )
                verify_run(cur, actor, request, workspace, data.source_id, parent)
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, workspace, "start_analysis")
            result = cur.execute(
                "SELECT monitor_start(%s,%s,%s,%s,%s,%s,%s,%s) AS data",
                (
                    workspace,
                    project,
                    data.source_id,
                    monitor,
                    data.version_id,
                    data.snapshot_id,
                    data.parent_run_id,
                    data.idempotency_key,
                ),
            ).fetchone()["data"]
            return command(response, result)
