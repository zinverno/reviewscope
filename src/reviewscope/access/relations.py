"""Bounded direct pairs on a byte-admitted snapshot. PostgreSQL owns the projection."""

from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from . import decisions, signals, store
from .aspects import Strict
from .evidence import Filters, evidence_page
from .human_reviews import Digest, Label, Saved, Span, State


class Scope(Strict):
    source_id: UUID
    decision_run: UUID | None = None
    human_revision: UUID | None = None

    @model_validator(mode="after")
    def paired(self):
        if (self.decision_run is None) != (self.human_revision is None):
            raise ValueError("incomplete_revision")
        return self


class Start(Scope):
    idempotency_key: UUID


class Page(Strict):
    source_id: UUID
    offset: int = Field(0, ge=0, le=496)
    exact_only: bool = False
    left: UUID | None = None
    right: UUID | None = None
    digest: str | None = Field(None, pattern="^[0-9a-f]{64}$")


class Endpoint(Strict):
    version_id: UUID
    content_sha256: Digest
    text_sha256: Digest
    normalized_sha256: Digest
    event_id: UUID | None
    state: State
    stance: Label | None
    spans: list[Span] = Field(max_length=2)
    text: str | None = None


class Pair(Strict):
    left: Endpoint
    right: Endpoint
    channels: dict
    text_spans: tuple[tuple[int, int], tuple[int, int]] | None
    text_kind: Literal["exact_text", "text_similarity_candidate", "not_evaluated"]
    text_basis: Literal["deterministic_text", "machine_experimental", "not_evaluated"]
    aspect_kind: Literal[
        "same_specific_complaint", "opposite_stance", "uncertain", "not_evaluated", "no_relation"
    ]
    aspect_basis: Literal["derived_from_human_outcomes", "uncertain", "not_evaluated"]
    reason: str


class Manifest(Strict):
    workspace_id: UUID
    project_id: UUID
    source_id: UUID
    snapshot_id: UUID
    snapshot_digest: Digest
    snapshot_object: Digest
    profile: Literal["direct-pairs-small-v1"]
    method_sha256: Digest
    decision_run: UUID | None
    human_revision: UUID | None
    human_digest: Digest | None
    aspect_id: UUID | None
    aspect_version_id: UUID | None
    definition_digest: Digest | None
    eligible_review_count: int
    excluded_blank_count: int
    total_possible_pairs: int
    candidate_pairs_count: int
    evaluated_pairs_count: int
    published_edges_count: int
    exact_possible_pairs: int
    kind_counts: dict[str, dict[str, int]]
    channel_availability: dict[str, str]
    budget: Literal[496]
    dense: Literal["not_evaluated"]
    rows_sha256: Digest


class Result(Strict):
    id: UUID
    digest: Digest
    filtered: int
    manifest: Manifest
    pairs: list[Pair] = Field(max_length=10)


class Capacity(Strict):
    profile: Literal["direct-pairs-small-v1"]
    max_versions: Literal[32]
    max_pairs: Literal[496]
    page_size: Literal[10]
    versions: int
    reason: Literal["VERSION_LIMIT", "TEXT_BYTE_LIMIT"] | None


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    base = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/relations"

    def authorize(cur, actor, request, workspace, write=False):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        store.authorize(cur, actor, workspace, "start_analysis" if write else "review_evidence")
        store.private_binds(cur)
        cur.execute("SET LOCAL statement_timeout='10s'")

    def verify(cur, actor, request, workspace, source, snapshot, run, revision):
        scope = (workspace, source, snapshot)
        cur.execute("SELECT relation_scope(%s,%s,%s,%s,%s) AS ok", (*scope, run, revision))
        if not cur.fetchone()["ok"]:
            raise HTTPException(404)
        receipt, _ = signals.broker_receipt(
            cur,
            actor,
            workspace,
            snapshot,
            source,
            snapshot,
            request,
            cookie,
            key,
            "relation-snapshot-1",
            "relation_snapshot",
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "relation-snapshot-1"
            or receipt.get("snapshot_id") != str(snapshot)
        ):
            raise HTTPException(503)
        if run is not None:
            cur.execute("SELECT decision_read(%s,%s,%s,%s,NULL,0,NULL) AS data", (*scope, run))
            data = cur.fetchone()["data"]
            if not data or data["compute_state"] != "COMPLETE":
                raise HTTPException(404)
            decisions.verify_artifact(
                cur,
                actor,
                workspace,
                snapshot,
                source,
                run,
                request,
                cookie,
                key,
                decisions.RunPage.model_validate(data),
            )
        return receipt

    @app.get(base, response_model=Capacity, openapi_extra=read_contract, responses=errors)
    def options(workspace: UUID, snapshot: UUID, request: Request, q: Annotated[Scope, Query()]):
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, workspace)
            cur.execute(
                "SELECT relation_capacity(%s,%s,%s,%s,%s) AS data",
                (workspace, q.source_id, snapshot, q.decision_run, q.human_revision),
            )
            capacity = cur.fetchone()["data"]
            if not capacity:
                raise HTTPException(404)
            if capacity["reason"]:
                return dict(
                    profile="direct-pairs-small-v1",
                    max_versions=32,
                    max_pairs=496,
                    page_size=10,
                    **capacity,
                )
            verify(
                cur,
                actor,
                request,
                workspace,
                q.source_id,
                snapshot,
                q.decision_run,
                q.human_revision,
            )
            return {
                "profile": "direct-pairs-small-v1",
                "max_versions": 32,
                "max_pairs": 496,
                "page_size": 10,
                **capacity,
            }

    @app.post(base, status_code=201, openapi_extra=write_contract, responses=errors)
    def publish(
        workspace: UUID,
        snapshot: UUID,
        request: Request,
        response: Response,
        data: Start,
        _: Annotated[Strict, Query()],
    ) -> Saved:
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, workspace, True)
            cur.execute(
                "SELECT relation_capacity(%s,%s,%s,%s,%s) AS data",
                (workspace, data.source_id, snapshot, data.decision_run, data.human_revision),
            )
            capacity = cur.fetchone()["data"]
            if not capacity:
                raise HTTPException(404)
            if capacity["reason"]:
                raise HTTPException(413)
            verify(
                cur,
                actor,
                request,
                workspace,
                data.source_id,
                snapshot,
                data.decision_run,
                data.human_revision,
            )
            cur.execute(
                "SELECT relation_publish(%s,%s,%s,%s,%s,%s) AS data",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    data.decision_run,
                    data.human_revision,
                    data.idempotency_key,
                ),
            )
            row = cur.fetchone()["data"]
            if not row or row["code"] not in (200, 201):
                raise HTTPException(row["code"] if row else 404)
            response.status_code = row["code"]
            return Saved.model_validate(row)

    @app.get(
        base + "/{result}", response_model=Result, openapi_extra=read_contract, responses=errors
    )
    def page(
        workspace: UUID, snapshot: UUID, result: UUID, request: Request, q: Annotated[Page, Query()]
    ):
        with authenticated(request) as (cur, actor):
            authorize(cur, actor, request, workspace)
            cur.execute(
                "SELECT relation_read(%s,%s,%s,%s,%s,%s,%s,%s) AS data",
                (workspace, q.source_id, snapshot, result, q.offset, q.left, q.right, q.exact_only),
            )
            doc = cur.fetchone()["data"]
            if not doc or q.digest and doc["digest"] != q.digest:
                raise HTTPException(404)
            m = doc["manifest"]
            receipt = verify(
                cur,
                actor,
                request,
                workspace,
                q.source_id,
                snapshot,
                m["decision_run"],
                m["human_revision"],
            )
            if (m["snapshot_digest"], m["snapshot_object"]) != (
                receipt["manifest"],
                receipt["sha256"],
            ):
                raise HTTPException(503)
            if q.left:
                pair = doc["pairs"][0]
                for index, (side, version) in enumerate((("left", q.left), ("right", q.right))):
                    endpoint = pair[side]
                    row = evidence_page(
                        cur, workspace, snapshot, Filters(source_id=q.source_id), version
                    )["rows"][0]
                    text = row["full_text"]
                    if (
                        str(version) != endpoint["version_id"]
                        or str(version) != row["version_id"]
                        or text is None
                        or sha256(text.encode()).hexdigest() != endpoint["text_sha256"]
                        or any(
                            not 0 <= s["start"] < s["end"] <= len(text) for s in endpoint["spans"]
                        )
                    ):
                        raise HTTPException(503)
                    spans = pair["text_spans"]
                    if spans is not None and not 0 <= spans[index][0] < spans[index][1] <= len(text):
                        raise HTTPException(503)
                    endpoint["text"] = text
            Result.model_validate(doc)
            response = JSONResponse(doc)
            if len(response.body) > 65536:
                raise HTTPException(503)
            return response
