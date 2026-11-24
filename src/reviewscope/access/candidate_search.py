"""Scoped POST and immutable result selection in the local TLS cabinet."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import aspects, signals, store
from .evidence import EvidencePage, Filters, evidence_page

KIND = "snapshot_candidate_search_v1"
Mode = Literal["lexical", "dense", "hybrid"]
Role = Literal["query_to_passage", "text_to_text"]


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect: aspects.Saved | None = None
    source_id: UUID
    idempotency_key: UUID
    query: str = Field(min_length=1, max_length=2048)
    role: Role
    mode: Mode
    k: int = Field(strict=True, ge=1, le=10)


class Channel(BaseModel):
    rank: int = Field(ge=1)
    similarity: float = Field(allow_inf_nan=False)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID
    fragment_id: str = Field(pattern="^[0-9a-f]{64}$")
    text_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    rank: int = Field(ge=1)
    review_rank: int = Field(ge=1, le=10)
    score: float = Field(allow_inf_nan=False)
    channels: dict[Literal["lexical", "dense"], Channel]
    start: int
    end: int
    context_start: int
    context_end: int

    @model_validator(mode="after")
    def valid_range(self):
        if not (
            0 <= self.context_start <= self.start < self.end <= self.context_end and self.channels
        ):
            raise ValueError("invalid span")
        return self


class CandidateResult(BaseModel):
    contract: Literal["candidate-search-1"] = Field(alias="schema")
    status: Literal[
        "READY",
        "NOT_RUN_MISSING_WEIGHTS",
        "NOT_RUN_MISSING_BACKEND",
        "INCOMPATIBLE_ENCODING",
        "QUERY_TOKEN_LIMIT",
    ]
    workspace_id: UUID
    source_id: UUID
    snapshot_id: UUID
    run_id: UUID
    manifest_sha256: str
    result_sha256: str
    coverage: str | None
    mode: Mode
    role: Role
    k: int
    reviewed_count: int
    eligible_review_count: int
    fragment_count: int | None
    retrieved_review_count: int
    candidate_count: int
    candidates: list[Candidate] = Field(max_length=40)
    index: dict | None = None
    shortfall: str | None = None


class Selection(signals.Selection):
    fragment_id: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class Listing(BaseModel):
    evidence: EvidencePage | None = None
    runs: list[signals.SignalRun]
    artifact_sha256: str | None
    result: CandidateResult | None


class Binding(aspects.Saved):
    project_id: UUID
    source_id: UUID
    digest: str = Field(pattern="^[0-9a-f]{64}$")


class BoundResult(CandidateResult):
    aspect: Binding
    query_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class BoundListing(Listing):
    result: BoundResult
    definition: aspects.Frozen
    query: str


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    from .http import JobStatus, Strict

    route = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/candidates"

    @app.post(
        route,
        status_code=202,
        response_model=JobStatus,
        openapi_extra=write_contract,
        responses=errors,
    )
    def enqueue(
        workspace: UUID,
        snapshot: UUID,
        data: CandidateRequest,
        request: Request,
        response: Response,
        _: Annotated[Strict, Query()],
    ):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        if (
            not data.query.strip()
            or "\x00" in data.query
            or len(data.query.encode("utf-8", errors="replace")) > 2048
        ):
            raise HTTPException(422)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "start_analysis")
            store.private_binds(cur)
            binding = (data.aspect.aspect_id, data.aspect.version_id) if data.aspect else ()
            cur.execute(
                "SELECT * FROM enqueue_candidate(" + ",".join(["%s"] * (8 + len(binding))) + ")",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    data.idempotency_key,
                    data.query,
                    data.mode,
                    data.role,
                    data.k,
                    *binding,
                ),
            )
            row = cur.fetchone()
            if row["code"] not in (200, 202):
                raise HTTPException(row["code"])
            response.status_code = row["code"]
            return store.job_status(cur, workspace, run=row["run_id"])

    @app.get(route, openapi_extra=read_contract, responses=errors)
    def selection(
        workspace: UUID, snapshot: UUID, request: Request, q: Annotated[Selection, Query()]
    ) -> BoundListing | Listing:
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            cur.execute(
                "SELECT candidate_scope(%s,%s,%s) AS allowed", (workspace, q.source_id, snapshot)
            )
            if not cur.fetchone()["allowed"]:
                raise HTTPException(404)
            listing = signals.read_selection(
                cur,
                actor,
                workspace,
                snapshot,
                q,
                request,
                cookie,
                key,
                KIND,
                "candidate-search-1",
                "read_candidates",
                "candidate-search-1",
            )
            if q.run_id is not None:
                doc = listing["result"]
                # Older schemas have no binding function and can only serve ad hoc artifacts.
                cur.execute(
                    "SELECT to_regprocedure('candidate_definition(uuid,uuid,uuid,uuid)') IS NOT NULL AS supported"
                )
                metadata = None
                if cur.fetchone()["supported"]:
                    cur.execute(
                        "SELECT candidate_definition(%s,%s,%s,%s) AS data",
                        (workspace, q.source_id, snapshot, q.run_id),
                    )
                    metadata = cur.fetchone()["data"]
                    if not metadata:
                        raise HTTPException(404)
                    if doc.get("query_sha256") != metadata["query_sha256"]:
                        raise HTTPException(503)
                binding = metadata["aspect"] if metadata else None
                if binding != doc.get("aspect"):
                    raise HTTPException(503)
                if binding is not None:
                    detail = metadata["definition"]
                    if not detail or any(
                        detail[k] != binding[k]
                        for k in ("project_id", "aspect_id", "version_id", "digest")
                    ):
                        raise HTTPException(404)
                    listing.update(definition=detail, query=metadata["query"])
            if q.fragment_id is not None:
                doc = listing["result"]
                hit = (
                    next(
                        (row for row in doc["candidates"] if row["fragment_id"] == q.fragment_id),
                        None,
                    )
                    if doc
                    else None
                )
                if hit is None:
                    raise HTTPException(404)
                ref = Candidate.model_validate(hit)
                detail = evidence_page(
                    cur, workspace, snapshot, Filters(source_id=q.source_id), ref.version_id
                )
                rows = detail["rows"]
                if len(rows) != 1 or rows[0]["version_id"] != str(ref.version_id):
                    raise HTTPException(404)
                text = rows[0]["full_text"]
                if (
                    not text
                    or ref.context_end > len(text)
                    or hashlib.sha256(text.encode()).hexdigest() != ref.text_sha256
                ):
                    raise HTTPException(503)
                listing["evidence"] = detail
            return BoundListing.model_validate(listing) if "definition" in listing else listing
