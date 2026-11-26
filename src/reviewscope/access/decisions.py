"""Version-bound corpus decisions in the authenticated synthetic cabinet."""

import hashlib
import json
from importlib.resources import files
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import aspects, signals, store
from .evidence import Filters, evidence_page

PROFILE = "aspect-linear-lexical-v1"
Outcome = Literal[
    "complaint",
    "mixed",
    "denial",
    "positive",
    "neutral",
    "unclear",
    "not_mentioned",
    "topic_uncertain",
    "excluded_blank",
]


class Selection(aspects.Saved):
    source_id: UUID


class Start(Selection):
    idempotency_key: UUID
    profile: Literal["aspect-linear-lexical-v1"]


class Page(aspects.Strict):
    source_id: UUID
    outcome: Outcome | None = None
    offset: int = Field(0, ge=0, le=10000)
    version_id: UUID | None = None


class SavedRun(BaseModel):
    id: UUID
    status: str


class Catalog(BaseModel):
    definition: aspects.Detail
    runs: list[SavedRun] = Field(max_length=25)
    profile: str
    supported_definition: bool


class Row(BaseModel):
    version_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Outcome
    start: int | None
    end: int | None
    counter_start: int | None
    counter_end: int | None
    score: float | None = Field(allow_inf_nan=False)
    reason: str | None


class VerifiedText(BaseModel):
    start: int
    end: int
    length: int
    text: str


Count = Annotated[int, Field(ge=0, le=10000, strict=True)]
Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class DecisionSummary(aspects.Strict):
    N_input: Count
    N_accepted: Count
    N_quarantine: Count
    N_confirmed_duplicate: Count
    N_excluded: Count
    N_eligible: Count
    N_evaluated: Count
    N_not_evaluated: Count
    N_mentioned: Count
    N_not_mentioned: Count
    N_topic_uncertain: Count
    N_complaints: Count
    N_mentioned_unclear: Count
    N_mentioned_other: Count
    N_uncertain: Count
    by_outcome: dict[Outcome, Count]
    coverage: Share | None
    defined_coverage: Share | None
    complaints_among_eligible: Share | None
    complaints_among_mentions: Share | None
    undefined_rates: dict[
        Literal[
            "coverage", "defined_coverage", "complaints_among_eligible", "complaints_among_mentions"
        ],
        Literal["NO_ELIGIBLE_REVIEWS", "NO_MENTIONS"],
    ]


class RunPage(BaseModel):
    id: UUID
    status: str
    stage: str
    reason: str | None
    processed: int
    total: int | None
    definition: aspects.Detail
    profile: str
    threshold_set: str
    compute_state: Literal["NOT_COMPUTED", "COMPLETE"]
    runtime: dict[Literal["python", "numpy", "sklearn", "dtype"], str] | None = None
    summary: DecisionSummary | None
    filtered_total: int | None
    rows: list[Row] = Field(max_length=25)
    evidence: VerifiedText | None = None
    counter_context: VerifiedText | None = None
    manifest_sha256: str | None = None
    artifact_sha256: str | None = None
    rows_sha256: str | None = None
    profile_sha256: str | None = None


def excerpt(text, start, end):
    if (start is None) != (end is None) or (
        start is not None and not 0 <= start < end <= len(text)
    ):
        raise HTTPException(503)
    # A bounded excerpt can cover only the first part of a long model window.
    start = start if start is not None else 0
    stop = min(start + 1024, end if end is not None else len(text))
    return VerifiedText(start=start, end=stop, length=len(text), text=text[start:stop])


def verify_evidence(row, text):
    text = text if text is not None else ""
    if hashlib.sha256(text.encode()).hexdigest() != row.text_sha256:
        raise HTTPException(503)
    primary = excerpt(text, row.start, row.end)
    if row.counter_start is None and row.counter_end is None:
        return primary, None
    counter = excerpt(text, row.counter_start, row.counter_end)
    if (
        row.outcome != "mixed"
        or row.start is None
        or not (row.counter_end <= row.start or row.counter_start >= row.end)
    ):
        raise HTTPException(503)
    return primary, counter


def verify_artifact(cur, actor, workspace, snapshot, source, run, request, cookie, key, page):
    doc, artifact_sha = signals.read_artifact(
        cur,
        actor,
        workspace,
        snapshot,
        source,
        run,
        request,
        cookie,
        key,
        "snapshot_aspect_decision_v1",
        "aspect-decision-1",
        "read_decisions",
        "aspect-decision-1",
    )
    if (
        page.summary is None
        or doc.get("summary") != page.summary.model_dump()
        or artifact_sha != page.artifact_sha256
        or doc.get("aspect_id") != str(page.definition.aspect_id)
        or doc.get("version_id") != str(page.definition.version_id)
        or page.runtime is None
        or doc.get("runtime") != page.runtime
        or doc.get("digest") != page.definition.digest
        or any(
            doc.get(field) != getattr(page, field)
            for field in (
                "rows_sha256",
                "manifest_sha256",
                "profile",
                "profile_sha256",
                "threshold_set",
            )
        )
    ):
        raise HTTPException(503)


def install(app, authenticated, key, cookie, local, read_contract, write_contract, errors):
    from .http import JobStatus, Strict

    base = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/decisions"
    # Load public templates before the TLS frontend enters its filesystem sandbox.
    try:
        templates = json.loads(
            files("reviewscope.resources").joinpath("decision-definitions.json").read_bytes()
        )
    except (OSError, ValueError):
        templates = None

    def supports_definition(definition):
        if templates is None:
            raise HTTPException(503)
        return definition["origin"] == "manual" and definition["payload"] in templates.values()

    def require_tls(request):
        if local or request.url.scheme != "https":
            raise HTTPException(403)

    def ready(cur):
        cur.execute("SELECT version_num FROM public.alembic_version")
        if cur.fetchone()["version_num"] not in {"0016_aspect_decisions", "0017_human_review", "0018_typed_relations", "0019_topic_discovery", "0020_topic_lock_order", "0021_saved_monitors"}:
            raise HTTPException(503)

    def catalog(cur, workspace, snapshot, binding):
        cur.execute(
            "SELECT decision_catalog(%s,%s,%s,%s,%s) AS data",
            (workspace, binding.source_id, snapshot, binding.aspect_id, binding.version_id),
        )
        data = cur.fetchone()["data"]
        if data is None:
            raise HTTPException(404)
        return data

    @app.get(base, openapi_extra=read_contract, responses=errors)
    def options(
        workspace: UUID, snapshot: UUID, request: Request, q: Annotated[Selection, Query()]
    ) -> Catalog:
        require_tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            ready(cur)
            data = catalog(cur, workspace, snapshot, q)
            return Catalog(
                **data,
                profile=PROFILE,
                supported_definition=supports_definition(data["definition"]),
            )

    @app.post(
        base,
        status_code=202,
        openapi_extra=write_contract,
        responses=errors,
        response_model=JobStatus,
    )
    def start(
        workspace: UUID,
        snapshot: UUID,
        request: Request,
        response: Response,
        data: Start,
        _: Annotated[Strict, Query()],
    ):
        require_tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "start_analysis")
            ready(cur)
            store.private_binds(cur)
            cur.execute(
                "SELECT code,run_id FROM enqueue_decision(%s,%s,%s,%s,%s,%s,%s)",
                (
                    workspace,
                    data.source_id,
                    snapshot,
                    data.aspect_id,
                    data.version_id,
                    data.idempotency_key,
                    data.profile,
                ),
            )
            result = cur.fetchone()
            if result["code"] not in (200, 202):
                raise HTTPException(result["code"])
            if result["code"] == 202 and not supports_definition(
                catalog(cur, workspace, snapshot, data)["definition"]
            ):
                raise HTTPException(422)  # Roll back the unsupported run.
            response.status_code = result["code"]
            return store.job_status(cur, workspace, run=result["run_id"])

    @app.get(base + "/{run}", openapi_extra=read_contract, responses=errors)
    def read(
        workspace: UUID, snapshot: UUID, run: UUID, request: Request, q: Annotated[Page, Query()]
    ) -> RunPage:
        require_tls(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            ready(cur)
            cur.execute(
                "SELECT decision_read(%s,%s,%s,%s,%s,%s,%s) AS data",
                (
                    workspace,
                    q.source_id,
                    snapshot,
                    run,
                    q.outcome,
                    0 if q.version_id else q.offset,
                    q.version_id,
                ),
            )
            data = cur.fetchone()["data"]
            if data is None:
                raise HTTPException(404)
            page = RunPage.model_validate(data)
            if page.compute_state == "COMPLETE":
                verify_artifact(cur, actor, workspace, snapshot, q.source_id, run, request, cookie, key, page)
            if q.version_id is not None:
                if len(page.rows) != 1 or page.rows[0].version_id != q.version_id:
                    raise HTTPException(404)
                detail = evidence_page(
                    cur, workspace, snapshot, Filters(source_id=q.source_id), q.version_id
                )
                rows = detail["rows"]
                if (
                    detail["source_id"] != str(q.source_id)
                    or detail["snapshot_id"] != str(snapshot)
                    or len(rows) != 1
                    or rows[0]["version_id"] != str(q.version_id)
                ):
                    raise HTTPException(404)
                page.evidence, page.counter_context = verify_evidence(
                    page.rows[0], rows[0]["full_text"]
                )
            if len(page.model_dump_json().encode()) > 65536:
                raise HTTPException(503)
            return page
