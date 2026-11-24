"""Scoped POST and immutable result selection in the local TLS cabinet."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import signals, store
from .evidence import EvidencePage, Filters, evidence_page

KIND = "snapshot_candidate_search_v1"
Mode = Literal["lexical", "dense", "hybrid"]
Role = Literal["query_to_passage", "text_to_text"]


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
            # Check before sending the private bind value to PostgreSQL.
            cur.execute("""SELECT current_setting('log_parameter_max_length_on_error')='0'
                AND (current_setting('log_parameter_max_length')='0' OR (
                  current_setting('log_statement')='none' AND current_setting('log_min_duration_statement')='-1'
                  AND current_setting('log_min_duration_sample')='-1'
                  AND current_setting('log_transaction_sample_rate')::float=0))
                AND coalesce(current_setting('pgaudit.log_parameter',true),'off')='off' AS safe""")
            if not cur.fetchone()["safe"]:
                raise HTTPException(503)
            cur.execute(
                "SELECT * FROM enqueue_candidate(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    data.idempotency_key,
                    data.query,
                    data.mode,
                    data.role,
                    data.k,
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
    ) -> Listing:
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
            return listing
