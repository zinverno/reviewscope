"""Read a bounded author history only through its admitted synthetic snapshot and version."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from reviewscope.specificity import specificity_score

from . import store


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    offset: int = Field(0, ge=0, le=10000)
    limit: int = Field(25, ge=1, le=50)


class Counts(BaseModel):
    places: int
    dated: int
    first_day: date | None
    last_day: date | None
    rated: int
    rating_mean: float | None
    rating_counts: dict[str, int]
    with_text: int


class Review(BaseModel):
    version_id: UUID
    location_id: UUID
    location_name: str | None
    position: int
    day: date | None
    rating: int | None
    has_text: bool
    specificity: float | None


class Eligibility(BaseModel):
    status: Literal["computed", "insufficient_data", "not_available"]
    eligible: int | None
    total: int | None
    reason: str | None


def availability(total, counts, status):
    def field(eligible, reason):
        if total is None:
            return Eligibility(status="not_available", eligible=None, total=None, reason=status)
        if eligible is None:
            return Eligibility(status="not_available", eligible=0, total=total, reason=reason)
        return Eligibility(
            status="computed" if eligible else "insufficient_data",
            eligible=eligible,
            total=total,
            reason=None if eligible else reason,
        )

    return {
        "places": field(total, "NO_PLACES"),
        "observed_dates": field(counts.get("dated"), "NO_COMPARABLE_DATES"),
        "ratings": field(counts.get("rated"), "NO_ELIGIBLE_RATINGS"),
        "specificity": field(counts.get("with_text"), "NO_TEXT"),
        "category": field(None, "CATEGORY_METADATA_UNAVAILABLE"),
        "geography": field(None, "GEO_METADATA_UNAVAILABLE"),
        "relevance": field(None, "COMPONENTS_UNAVAILABLE"),
    }


class Context(BaseModel):
    method: Literal["snapshot-reviewer-context-v1"]
    status: Literal["computed", "NO_REVIEWER_ID", "AUTHOR_CONTEXT_NOT_ADMITTED"]
    workspace_id: UUID
    source_id: UUID
    snapshot_id: UUID
    anchor_version_id: UUID
    manifest_sha256: str
    offset: int
    limit: int
    total: int | None
    metrics: Counts | None
    availability: dict[str, Eligibility]
    rows: list[Review]


def install(app, authenticated, local, read_contract, errors):
    route = "/api/v1/workspaces/{workspace}/snapshots/{snapshot}/reviews/{version}/reviewer-context"

    @app.get(route, openapi_extra=read_contract, responses=errors)
    def context(
        workspace: UUID,
        snapshot: UUID,
        version: UUID,
        request: Request,
        selection: Annotated[Selection, Query()],
    ) -> Context:
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "review_evidence")
            cur.execute(
                "SELECT reviewer_context_page(%s,%s,%s,%s,%s,%s) AS context",
                (
                    workspace,
                    selection.source_id,
                    snapshot,
                    version,
                    selection.offset,
                    selection.limit,
                ),
            )
            result = cur.fetchone()["context"]
            if result is None:
                raise HTTPException(404)
            result["availability"] = availability(
                result["total"], result["metrics"] or {}, result["status"]
            )
            for row in result["rows"]:
                body = row.pop("body")
                row["specificity"] = specificity_score(body).value if row["has_text"] else None
            return result
