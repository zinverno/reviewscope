"""TLS-only, bounded two-step admission of pre-authorized synthetic files."""

import asyncio
import hashlib
import hmac
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryFile
from threading import BoundedSemaphore
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from reviewscope.import_preview import preview
from reviewscope.import_preview.contract import ImportSummary, PreviewError

from . import store, upload_wire
from .restore import sha

BASE = "/api/v1/workspaces/{workspace}"
DRAFTS = BASE + "/sources/{source}/uploads"
U = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
POST = re.compile(rf"/api/v1/workspaces/{U}/sources/{U}/uploads/{U}/(?:preview|confirm)")
SLOTS = BoundedSemaphore(2)


class Target(BaseModel):
    project_id: UUID
    project_name: str
    source_id: UUID
    source_label: str
    location_id: UUID
    location_name: str | None


class Position(BaseModel):
    position: int
    status: Literal["accepted", "quarantined", "confirmed_duplicate"]
    reasons: list[str]
    warnings: list[str]
    related_position: int | None


class Diagnostics(BaseModel):
    summary: ImportSummary
    positions: list[Position] | None = None


class Draft(BaseModel):
    id: UUID
    fmt: Literal["csv", "json"]
    locations: list[UUID]
    fingerprint: str
    diagnostics: Diagnostics
    expires_at: datetime
    snapshot_id: UUID | None


def binary_route(request):
    return request.method == "POST" and POST.fullmatch(request.url.path) is not None


def header(request, name):
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise HTTPException(422)
    return values[0]


def arguments(request):
    if (
        header(request, "content-type") != "application/octet-stream"
        or "content-encoding" in request.headers
    ):
        raise HTTPException(422)
    fmt = header(request, "x-rs-format")
    digest = header(request, "x-rs-file-sha256")
    fields = header(request, "x-rs-parts")
    if (
        fmt not in {"csv", "json"}
        or not sha(digest)
        or not re.fullmatch(r"[0-9]{1,9}(,[0-9]{1,9}){2}", fields)
    ):
        raise HTTPException(422)
    try:
        lens = upload_wire.sizes([int(n) for n in fields.split(",")])
    except upload_wire.UploadError as exc:
        raise HTTPException(413 if str(exc) == "over_limit" else 422) from None
    loc = header(request, "x-rs-locations").split(",")
    if len(loc) > 64 or loc != sorted(set(loc)) or any(not re.fullmatch(U, v) for v in loc):
        raise HTTPException(422)
    if "content-length" in request.headers:
        length = header(request, "content-length")
        if not length.isascii() or not length.isdigit() or len(length) > 10:
            raise HTTPException(413)
        if int(length) != sum(lens):
            raise HTTPException(413 if int(length) > sum(lens) else 422)
    return fmt, digest, lens, [UUID(v) for v in loc]


async def receive(request, lens, digest):
    total, start = sum(lens), sum(lens[:2])
    count = 0
    calculated = hashlib.sha256()
    with TemporaryFile(mode="w+b") as tmp:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                if count + len(chunk) > total:
                    raise HTTPException(413)
                calculated.update(memoryview(chunk)[max(0, start - count) :])
                tmp.write(chunk)
                count += len(chunk)
        if count != total or calculated.hexdigest() != digest:
            raise HTTPException(422)
        tmp.seek(0)
        return tuple(tmp.read(n) for n in lens)


def inspect(parts, fmt, bindings):
    mapping, passport, data = parts
    result = preview(data, fmt, mapping, passport)
    places = {r["place_key"] for r in bindings}
    source = bindings[0]["source_key"]
    if (
        result.passport.source not in (source, None)
        or result.passport.source_place_id not in places | {None}
        or any(
            r.reviewer_id
            or r.source_url
            or r.source not in (source, None)
            or r.source_place_id not in places | {None}
            for r in result.rows
        )
    ):
        raise HTTPException(422)
    return result.fingerprint, dict(
        summary=asdict(result.summary),
        positions=[
            dict(
                position=r.source_position,
                status=r.status,
                reasons=r.reasons,
                warnings=r.warnings,
                related_position=r.related_position,
            )
            for r in result.rows
        ],
    )


def install(app, authenticated, key, cookie, local):
    bundle = Path(__file__).resolve().parents[3] / "prototypes/reviewscope-ui/dist"

    def asset(path):
        if local or not path.is_file() or path.resolve() != path:
            raise HTTPException(404)
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/cabinet/", include_in_schema=False)
    def cabinet():
        return asset(bundle / "cabinet.html")

    @app.get("/cabinet/assets/{name}", include_in_schema=False)
    def cabinet_asset(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:js|css)", name):
            raise HTTPException(404)
        return asset(bundle / "assets" / name)

    def guard(request):
        if local or request.url.scheme != "https":
            raise HTTPException(403)
        if request.url.query:
            raise HTTPException(422)

    def rows(cur, w, s, d=None):
        cur.execute("SELECT * FROM browser_drafts(%s,%s,%s)", (w, s, d))
        result = cur.fetchall()
        if d is not None:
            if not result:
                raise HTTPException(404)
            return result[0]
        return result

    @app.get(BASE + "/upload-targets", response_model=list[Target])
    def targets(workspace: UUID, request: Request):
        guard(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "register_delivery")
            cur.execute("SELECT * FROM browser_targets(%s)", (workspace,))
            return cur.fetchall()

    @app.get(DRAFTS, response_model=list[Draft], response_model_exclude_none=True)
    def drafts(workspace: UUID, source: UUID, request: Request):
        guard(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "register_delivery")
            cur.execute("SELECT browser_member(%s,%s) AS ok", (workspace, source))
            if not cur.fetchone()["ok"]:
                raise HTTPException(404)
            return rows(cur, workspace, source)

    @app.get(DRAFTS + "/{delivery}", response_model=Draft)
    def detail(workspace: UUID, source: UUID, delivery: UUID, request: Request):
        guard(request)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, workspace, "register_delivery")
            return rows(cur, workspace, source, delivery)

    def bindings_for(request, w, s, digest, loc):
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, w, "register_delivery")
            cur.execute("SELECT * FROM browser_bindings(%s,%s,%s,%s)", (w, s, digest, loc))
            bindings = cur.fetchall()
            if not bindings:
                raise HTTPException(404)
            return bindings

    def process(request, w, s, d, fmt, digest, loc, parts, bindings, confirm):
        fp, report = inspect(parts, fmt, bindings)
        if confirm and header(request, "x-rs-fingerprint") != fp:
            raise HTTPException(409)
        with authenticated(request) as (cur, actor):
            store.authorize(cur, actor, w, "register_delivery")
            if confirm:
                ticket = hmac.new(
                    key(),
                    f"apply|{request.cookies[cookie]}|{w}|{s}|{d}|{fp}".encode(),
                    hashlib.sha256,
                ).hexdigest()
                cur.execute(
                    "SELECT browser_approve(%s,%s,%s,%s,%s,%s,%s,%s) AS code",
                    (w, s, d, digest, fp, fmt, loc, hashlib.sha256(ticket.encode()).digest()),
                )
            else:
                cur.execute(
                    "SELECT browser_preview(%s,%s,%s,%s,%s,%s,%s,%s) AS code",
                    (w, s, d, fmt, loc, digest, fp, Jsonb(report)),
                )
            code = cur.fetchone()["code"]
            if code not in (200, 202):
                raise HTTPException(code)
            if code == 200:
                return rows(cur, w, s, d)
        try:
            upload_wire.commit(
                dict(
                    workspace=str(w),
                    source=str(s),
                    delivery=str(d),
                    ticket=ticket,
                    fingerprint=fp,
                    checksum=digest,
                    sizes=list(map(len, parts)),
                ),
                parts,
            )
        except (OSError, ValueError):
            # A simultaneous confirmation or a lost reply may already have committed.
            completed = detail(w, s, d, request)
            if not completed["snapshot_id"] or completed["fingerprint"] != fp:
                raise HTTPException(503) from None
            return completed
        return detail(w, s, d, request)

    async def upload(request, w, s, d, confirm):
        guard(request)
        fmt, digest, lens, loc = arguments(request)
        if confirm and not sha(header(request, "x-rs-fingerprint")):
            raise HTTPException(422)
        bindings = await run_in_threadpool(bindings_for, request, w, s, digest, loc)
        if not SLOTS.acquire(blocking=False):
            raise HTTPException(429)
        try:
            parts = await receive(request, lens, digest)
            return await run_in_threadpool(
                process, request, w, s, d, fmt, digest, loc, parts, bindings, confirm
            )
        except TimeoutError:
            raise HTTPException(422) from None
        except PreviewError as exc:
            return JSONResponse(
                {"error": exc.code}, status_code=413 if exc.code == "over_limit" else 422
            )
        finally:
            SLOTS.release()

    binary = {
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    }

    @app.post(DRAFTS + "/{delivery}/preview", response_model=Draft, openapi_extra=binary)
    async def preview_file(workspace: UUID, source: UUID, delivery: UUID, request: Request):
        return await upload(request, workspace, source, delivery, False)

    @app.post(DRAFTS + "/{delivery}/confirm", response_model=Draft, openapi_extra=binary)
    async def confirm_file(workspace: UUID, source: UUID, delivery: UUID, request: Request):
        return await upload(request, workspace, source, delivery, True)
