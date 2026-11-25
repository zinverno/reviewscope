"""Scoped POST and immutable result selection in the local TLS cabinet."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import aspects, signals, store
from .evidence import EvidencePage, Filters, evidence_page

KIND = "snapshot_candidate_search_v1"
Mode = Literal["lexical", "dense", "hybrid"]
Role = Literal["query_to_passage", "text_to_text"]


class RerankScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID


class RerankRequest(RerankScope):
    idempotency_key: UUID
    profile: Literal["mmarco-context-logit-v1"]


class Rank(BaseModel):
    fragment_id: str = Field(pattern="^[0-9a-f]{64}$")
    pre_rank: int = Field(ge=1)
    rank: int = Field(ge=1, le=40)
    review_rank: int = Field(ge=1, le=10)
    score: float = Field(allow_inf_nan=False)


class RerankView(BaseModel):
    status: Literal["queued", "running", "failed", "cancelled", "READY", "BASELINE_RETAINED"]
    reason: str | None = None
    ranking: list[Rank] = Field(max_length=40)
    runtime: str | None = None
    revision: str | None = None
    parent_sha256: str | None = None
    result_sha256: str | None = None


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

    derived = route + "/{parent}/rerank"

    def parent_result(cur, actor, workspace, snapshot, parent, source, request):
        sel = Selection(source_id=source, run_id=parent)
        listing = signals.read_selection(
            cur,
            actor,
            workspace,
            snapshot,
            sel,
            request,
            cookie,
            key,
            KIND,
            "candidate-search-1",
            "read_candidates",
            "candidate-search-1",
        )
        doc = listing["result"]
        if doc.get("status") != "READY" or not doc.get("aspect"):
            raise HTTPException(409)
        return listing

    @app.post(
        derived,
        status_code=202,
        response_model=JobStatus,
        openapi_extra=write_contract,
        responses=errors,
    )
    def rerank_start(
        workspace: UUID,
        snapshot: UUID,
        parent: UUID,
        data: RerankRequest,
        request: Request,
        response: Response,
        _: Annotated[Strict, Query()],
    ):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "start_analysis")
            store.private_binds(cur)
            cur.execute(
                "SELECT * FROM enqueue_rerank(%s,%s,%s,%s,%s,%s)",
                (workspace, data.source_id, snapshot, parent, data.idempotency_key, data.profile),
            )
            row = cur.fetchone()
            if row["code"] not in (200, 202):
                raise HTTPException(row["code"])
            parent_result(cur, actor, workspace, snapshot, parent, data.source_id, request)
            response.status_code = row["code"]
            return store.job_status(cur, workspace, run=row["run_id"])

    @app.get(derived + "/{run}", openapi_extra=read_contract, responses=errors)
    def rerank_read(
        workspace: UUID,
        snapshot: UUID,
        parent: UUID,
        run: UUID,
        request: Request,
        q: Annotated[RerankScope, Query()],
    ) -> RerankView:
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            cur.execute(
                "SELECT rerank_visible(%s,%s,%s,%s,%s) AS allowed",
                (workspace, q.source_id, snapshot, parent, run),
            )
            if not cur.fetchone()["allowed"]:
                raise HTTPException(404)
            saved = parent_result(cur, actor, workspace, snapshot, parent, q.source_id, request)
            job = store.job_status(cur, workspace, run=run)
            view = dict(status=job["status"], reason=job["reason"], ranking=[])
            if job["status"] != "succeeded":
                return RerankView.model_validate(view)
            derived_result = signals.read_selection(
                cur,
                actor,
                workspace,
                snapshot,
                Selection(source_id=q.source_id, run_id=run),
                request,
                cookie,
                key,
                "snapshot_rerank_v1",
                "candidate-rerank-1",
                "read_rerank",
                "candidate-rerank-1",
            )
            doc = derived_result["result"]
            baseline = saved["result"]
            for record, excluded in (
                (doc, {"result_sha256"}),
                (baseline, {"result_sha256", "run_id"}),
            ):
                canonical = json.dumps(
                    {k: v for k, v in record.items() if k not in excluded},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                if hashlib.sha256(canonical).hexdigest() != record.get("result_sha256"):
                    raise HTTPException(503)
            if (
                doc.get("parent_run_id") != str(parent)
                or doc.get("parent_sha256") != saved["artifact_sha256"]
                or doc.get("parent_result_sha256") != baseline["result_sha256"]
                or any(
                    doc.get(k) != baseline[k]
                    for k in ("query_sha256", "aspect", "manifest_sha256", "snapshot_sha256")
                )
                or doc.get("profile") != "mmarco-context-logit-v1"
            ):
                raise HTTPException(503)
            view = RerankView.model_validate(doc)
            hits = {h["fragment_id"]: h for h in baseline["candidates"]}
            if view.status == "READY":
                if (
                    len(view.ranking) != len(hits)
                    or {h.fragment_id for h in view.ranking} != hits.keys()
                ):
                    raise HTTPException(503)
                reviews = {}
                for i, h in enumerate(
                    sorted(view.ranking, key=lambda r: (-r.score, r.fragment_id)), 1
                ):
                    original = hits[h.fragment_id]
                    if (
                        view.ranking[i - 1] != h
                        or h.pre_rank != original["rank"]
                        or h.rank != i
                        or h.review_rank
                        != reviews.setdefault(original["version_id"], len(reviews) + 1)
                    ):
                        raise HTTPException(503)
            elif view.status != "BASELINE_RETAINED" or view.ranking or not view.reason:
                raise HTTPException(503)
            return view
