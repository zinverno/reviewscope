"""Canonical review reads for an admitted synthetic snapshot on the local TLS origin."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import store


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID | None = None
    location_id: UUID | None = None
    rating: int | None = Field(None, ge=1, le=5)
    has_text: bool | None = None
    has_date: bool | None = None
    first_day: date | None = None
    last_day: date | None = None
    position: int | None = Field(None, ge=1, le=10000)
    offset: int = Field(0, ge=0, le=10000)
    limit: int = Field(25, ge=1, le=50)


class EvidenceRow(BaseModel):
    position: int
    version_id: UUID
    identity_id: UUID
    review_id: str | None
    version_no: int
    version_batch_id: UUID
    location_id: UUID
    location_name: str | None
    place_key: str
    rating: int | None
    raw_rating: str | bool | None
    rating_scale: list[int] | None
    time_kind: str | None
    event_value: str | None
    time_precision: str | None
    utc_offset: str | None
    event_basis: str | None
    excerpt: str | None
    full_text: str | None
    evidence_span: None = None
    source_url: str | None


class EvidencePage(BaseModel):
    snapshot_id: UUID
    source_id: UUID
    source_label: str
    batch_id: UUID
    coverage: Literal["partial", "full", "unknown"]
    covered_from: datetime | None
    covered_until: datetime | None
    historical: bool
    total: int
    offset: int
    limit: int
    rows: list[EvidenceRow]


def install(app, authenticated, local, read_contract, errors):
    route = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/reviews"

    def read(request, w, k, filters, version=None):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        if filters.first_day and filters.last_day and filters.first_day > filters.last_day:
            raise HTTPException(422)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, w, "review_evidence")
            cur.execute(
                """SELECT review_evidence_page(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS evidence""",
                (
                    w,
                    k,
                    version,
                    filters.source_id,
                    filters.location_id,
                    filters.rating,
                    filters.has_text,
                    filters.has_date,
                    filters.first_day,
                    filters.last_day,
                    filters.position,
                    0 if version else filters.offset,
                    1 if version else filters.limit,
                ),
            )
            result = cur.fetchone()["evidence"]
            if result is None:
                raise HTTPException(404)
            return result

    @app.get(route, openapi_extra=read_contract, responses=errors)
    def reviews(
        workspace: UUID, snapshot: UUID, request: Request, filters: Annotated[Filters, Query()]
    ) -> EvidencePage:
        return read(request, workspace, snapshot, filters)

    @app.get(route + "/{version}", openapi_extra=read_contract, responses=errors)
    def review(
        workspace: UUID,
        snapshot: UUID,
        version: UUID,
        request: Request,
        filters: Annotated[Filters, Query()],
    ) -> EvidencePage:
        return read(request, workspace, snapshot, filters, version)
