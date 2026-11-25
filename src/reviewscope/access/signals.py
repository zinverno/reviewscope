"""Read one admitted signal artifact through the private object broker on local TLS."""

import hashlib
import json
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import store, upload_wire
from .security import session_proof

KIND = "snapshot_explainable_signals_v1"


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    run_id: UUID | None = None


class Strength(BaseModel):
    heuristic_score: float = Field(ge=0, le=100)
    evidence_band: Literal["LOW", "MEDIUM", "HIGH"]
    signals: list[str]
    counter_signals: list[str]


class SignalReview(BaseModel):
    version_id: UUID
    place: str
    rating: int | None
    rating_exclusion: str | None
    observed_day: date | None
    author_present: bool
    text_present: bool
    specificity: Strength | None


class Availability(BaseModel):
    value: float | None
    eligible: int
    total: int
    availability: Literal["computed", "insufficient_data", "not_available"]
    reason: str | None


class Place(BaseModel):
    raw: Availability
    weighted: Availability
    observed_days: dict[date, int]
    distribution: dict[str, int] | None
    specificity: Availability
    relevance: Availability
    templated: Availability
    coordinated: Availability
    events: Availability


class SignalDocument(BaseModel):
    contract: Literal["snapshot-explainable-signals-1"] = Field(alias="schema")
    job_kind: Literal["snapshot_explainable_signals_v1"]
    engine_version: Literal["1"]
    definition_version: Literal["signals-1"]
    model_version: Literal["unused"]
    versions_sha256: str
    snapshot_sha256: str
    source_sha256: dict[str, str]
    preview_fingerprint: str
    eligibility: dict[str, dict[str, int]]
    metric_exclusions: dict[str, dict[str, int]]
    delivery_exclusions: dict[str, dict[str, int]]
    workspace_id: UUID
    source_id: UUID
    snapshot_id: UUID
    run_id: UUID
    batch_id: UUID
    result_sha256: str
    manifest_sha256: str
    coverage: str | None
    coverage_mode: str | None
    covered_from: str | None
    covered_until: str | None
    event_basis: str | None
    rating_scale: list[int] | None
    parameters: dict
    observations: dict[str, int]
    quarantined: dict[str, int]
    confirmed_duplicates: dict[str, int]
    places: dict[str, Place]
    reviews: list[SignalReview]


class SignalRun(BaseModel):
    id: UUID
    status: str


class SignalSelection(BaseModel):
    runs: list[SignalRun]
    artifact_sha256: str | None
    result: SignalDocument | None


def install(app, authenticated, key, cookie, local, contract, errors):
    @app.get(
        "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/signals",
        openapi_extra=contract,
        responses=errors,
    )
    def signals(
        workspace: UUID, snapshot: UUID, request: Request, q: Annotated[Selection, Query()]
    ) -> SignalSelection:
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            cur.execute(
                "SELECT signal_scope(%s,%s,%s) AS admitted", (workspace, q.source_id, snapshot)
            )
            if not cur.fetchone()["admitted"]:
                raise HTTPException(404)
            return read_selection(
                cur,
                actor,
                workspace,
                snapshot,
                q,
                request,
                cookie,
                key,
                KIND,
                "signals-1",
                "read_signals",
                "snapshot-explainable-signals-1",
            )


def read_selection(
    cur, actor, workspace, snapshot, q, request, cookie, key, kind, ticket_kind, op, schema
):
    cur.execute(
        """SELECT id,status FROM analysis_run WHERE workspace_id=%s AND snapshot_id=%s
                AND job_kind=%s ORDER BY id""",
        (workspace, snapshot, kind),
    )
    runs = cur.fetchall()
    if q.run_id is None:
        return dict(runs=runs, result=None, artifact_sha256=None)
    doc, sha = read_artifact(
        cur,
        actor,
        workspace,
        snapshot,
        q.source_id,
        q.run_id,
        request,
        cookie,
        key,
        kind,
        ticket_kind,
        op,
        schema,
    )
    return dict(runs=runs, result=doc, artifact_sha256=sha)


def read_artifact(
    cur,
    actor,
    workspace,
    snapshot,
    source,
    run,
    request,
    cookie,
    key,
    kind,
    ticket_kind,
    op,
    schema,
):
    job = store.job_status(cur, workspace, run=run)
    if job["job_kind"] != kind or job["snapshot_id"] != snapshot or not job["artifact_id"]:
        raise HTTPException(404)
    cur.execute("SELECT floor(extract(epoch FROM statement_timestamp()))::bigint+25 AS expires")
    expires = cur.fetchone()["expires"]
    ids = list(map(str, (workspace, source, snapshot, run, actor)))
    payload = "|".join([ticket_kind, *ids, str(expires)]).encode()
    proof = session_proof(request.cookies[cookie], key()).encode()
    ticket = hashlib.sha256(
        hashlib.sha256(proof).digest() + hashlib.sha256(payload).digest()
    ).hexdigest()
    msg = dict(zip(("workspace", "source", "snapshot", "run", "actor"), ids, strict=True))
    try:
        doc = upload_wire.exchange(dict(**msg, op=op, expires=expires, ticket=ticket), (), 65536)
    except (ValueError, OSError):
        raise HTTPException(503) from None
    if isinstance(doc, dict) and doc.get("error") == "scope_denied":
        raise HTTPException(404)
    if not isinstance(doc, dict) or doc.get("schema") != schema:
        raise HTTPException(503)
    if [doc.get(k) for k in ("workspace_id", "source_id", "snapshot_id", "run_id")] != ids[:4]:
        raise HTTPException(503)
    raw = (
        json.dumps(
            doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        + b"\n"
    )
    if hashlib.sha256(raw).hexdigest() != job["sha256"]:
        raise HTTPException(503)
    return doc, job["sha256"]
