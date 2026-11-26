"""Concurrent topic confirmation through the real importer, broker, API and runtime role."""

import os
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from operational.delivery_objects import DeliveryError
from operational.retention import (
    capture_access,
    initialize,
    verify_access_permissions,
    verify_restore_roles,
)
from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_aspects import A, B, create
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_storage import runtime
from operational.tests.test_deliveries import command
from operational.tests.test_human_review import operation, submit
from operational.tests.test_topic_discovery import dated_snapshot
from reviewscope.access import store
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

TEXTS = [
    "Витрина разбита утром",
    "Витрина разбита вечером",
    "Витрина разбита днём",
    "Крышка стакана течёт",
    "Крышка стакана мокрая",
    "Крышка стакана сломана",
]
REVIEW = "SELECT topic_review(%s,%s,%s,%s,%s,'confirm',%s::uuid[],%s,NULL,NULL,NULL,%s)"
CORE = "topic_apply_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb)"


@dataclass(repr=False)
class Cabinet:
    w: UUID
    src: UUID
    snapshot: str
    earlier: str
    result: str
    project: UUID
    root: Path
    url: str
    first: TestClient
    second: TestClient
    replay: TestClient
    actors: tuple
    parents: list
    members: list

    @property
    def aspects(self):
        return f"{ROOT}/workspaces/{self.w}/projects/{self.project}/aspects"

    def action(self, name, parents, **kwargs):
        return self.url + "/actions", operation(
            self.src,
            action=name,
            expected_open=parents,
            reason="Исходные версии проверены",
            **kwargs,
        )

    def confirm(self, parent):
        return self.action("confirm", [parent], definition=A)

    def read(self, **params):
        reply = self.first.get(self.url, params={"source_id": str(self.src), **params})
        reply.raise_for_status()
        return reply.json()


@contextmanager
def cabinet(db, tmp_path, signing_key, *, accounts="test"):
    args, root, w, src = command(db, tmp_path, operator_login=accounts + "-operator")
    snap = dated_snapshot(db, tmp_path, args, w, src, "04", TEXTS)
    old = dated_snapshot(db, tmp_path, args, w, src, "03", ["Витрина разбита ночью"])
    project = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
    base = f"{ROOT}/workspaces/{w}/snapshots/{snap}/topic-discovery"
    with ExitStack() as stack:
        clients = [stack.enter_context(TestClient(create_app(), base_url=ORIGIN)) for _ in range(3)]
        actors = []
        for i, role in enumerate(("owner", "analyst")):
            password = secrets.token_urlsafe(24)
            name = f"{accounts}-{role}"
            actors.append(provision(db, name, password, signing_key, workspace=w, role=role))
            assert login(clients[i], name, password).status_code == 204
            if i == 0:
                assert login(clients[2], name, password).status_code == 204
        result = submit(clients[0], base, operation(src))["id"]
        url = base + "/" + result
        params = dict(source_id=str(src))
        parents = [p["id"] for p in clients[0].get(url, params=params).json()["rows"]]
        assert len(parents) == 2
        detail = clients[0].get(url, params=params | {"candidate_id": parents[0]}).json()
        yield Cabinet(
            w,
            src,
            snap,
            old,
            result,
            project,
            root,
            url,
            *clients,
            tuple(actors),
            parents,
            [r["version_id"] for r in detail["rows"]],
        )


@pytest.fixture
def live(db, tmp_path, key, monkeypatch):  # noqa: F811
    with importer(tmp_path, monkeypatch), cabinet(db, tmp_path, key) as site:
        yield site


def post(client, request):
    path, data = request
    return client.post(path, headers=CSRF, json=data)


def counts(db, site):
    return tuple(
        db.execute(f"SELECT count(*) FROM {table} WHERE workspace_id=%s", (site.w,)).fetchone()[0]
        for table in ("aspect", "aspect_version", "topic_event", "topic_request", "topic_history")
    )


def blocked_by(audit, holder):
    started = monotonic()
    while monotonic() - started < 4:
        row = audit.execute(
            """SELECT pid FROM pg_stat_activity WHERE datname=current_database()
            AND %s=ANY(pg_blocking_pids(pid))""",
            (holder,),
        ).fetchone()
        if row:
            return row[0]
        sleep(0.01)
    pytest.fail("expected the second transaction to overlap the first")


@contextmanager
def before_commit(monkeypatch):
    # Pause only the first completed handler, while its real SQL and deferred FKs are uncommitted.
    opened, release = Event(), Event()
    holder = []
    original = store.transaction

    @contextmanager
    def transaction(engine):
        with original(engine) as cur:
            yield cur
            if not opened.is_set():
                holder.append(cur.connection.info.backend_pid)
                opened.set()
                assert release.wait(6), "test commit barrier expired"

    with monkeypatch.context() as patch:
        patch.setattr(store, "transaction", transaction)
        try:
            yield opened, release, holder
        finally:
            release.set()


def concurrent(monkeypatch, first, second):
    with psycopg.connect(user="rs_test", autocommit=True) as audit, ThreadPoolExecutor(2) as pool:
        with before_commit(monkeypatch) as (opened, release, holder):
            tick = monotonic()
            a = pool.submit(first)
            assert opened.wait(4), "first request did not reach commit"
            b = pool.submit(second)
            blocked_by(audit, holder[0])
            release.set()
            replies = a.result(), b.result()
            print("CONFIRM_OVERLAP", round(monotonic() - tick, 3), [r.status_code for r in replies])
            return replies


def direct(conn, site, data):
    return conn.execute(
        REVIEW,
        (
            site.w,
            site.src,
            site.snapshot,
            site.result,
            data["idempotency_key"],
            data["expected_open"],
            data["reason"],
            Jsonb(data["definition"]),
        ),
    ).fetchone()[0]


@pytest.mark.parametrize("entry", ["http", "sql"])
def test_topic_discovery_confirm_writer_order(live, key, entry):  # noqa: F811
    site = live
    confirm = site.confirm(site.parents[0])
    proof = session_proof(site.first.cookies[COOKIE], key)

    def write():
        if entry == "http":
            return post(site.first, confirm).status_code
        with runtime(site.w, proof) as conn:
            conn.execute("SET statement_timeout='10s'")
            return direct(conn, site, confirm[1])["code"]

    with psycopg.connect(user="rs_test", autocommit=True) as audit, ThreadPoolExecutor(1) as pool:
        with runtime(site.w, session_proof(site.second.cookies[COOKIE], key)) as reader:
            reader.execute("BEGIN")
            assert reader.execute(
                "SELECT topic_read(%s,%s,%s,%s,NULL,NULL,0)",
                (site.w, site.src, site.snapshot, site.result),
            ).fetchone()[0]
            task = pool.submit(write)
            try:
                waiting = blocked_by(audit, reader.info.backend_pid)
                # The two-int retention witness is separate from the one-bigint topic command lock.
                assert not audit.execute(
                    """SELECT FROM pg_locks WHERE pid=%s AND locktype='advisory'
                    AND objsubid=1""",
                    (waiting,),
                ).fetchall()
            finally:
                reader.execute("ROLLBACK")
            assert task.result() == 201


@pytest.mark.parametrize(
    "other,confirm_first",
    [
        ("same", True),
        ("same", False),
        ("replay", True),
        ("read", True),
        ("read", False),
        ("independent", True),
        ("independent", False),
        ("history", False),
        ("history", True),
        *(
            (name, order)
            for name in ("reject", "merge", "split", "create", "edit", "archive")
            for order in (True, False)
        ),
    ],
)
def test_topic_discovery_confirm_contention(db, live, monkeypatch, other, confirm_first):
    site = live
    p, q = site.parents
    confirm = site.confirm(p)
    peer = site.second
    request = confirm
    if other == "same":
        request = site.confirm(p)
    elif other == "replay":
        peer = site.replay  # Same actor, separate authenticated session and connection.
    elif other == "independent":
        request = site.confirm(q)
    elif other in ("merge", "reject", "split"):
        parts = {"parts": [site.members[:1], site.members[1:]]} if other == "split" else {}
        request = site.action(other, [p, q] if other == "merge" else [p], **parts)
    elif other == "history":
        request = (
            site.url + "/history-checks",
            operation(site.src, candidate_id=q, historical_snapshot=site.earlier),
        )
    elif other == "create":
        request = (site.aspects, dict(payload=B, idempotency_key=str(uuid4())))
    elif other in ("edit", "archive"):
        saved = create(peer, site.aspects, B)
        route = f"{site.aspects}/{saved['aspect_id']}"
        data = dict(expected_version=saved["version_id"], idempotency_key=str(uuid4()))
        request = (
            (route + "/versions", data | {"payload": A})
            if other == "edit"
            else (route + "/archive", data)
        )
    initial = counts(db, site)
    frozen = site.read()["digest"]
    receipts = receipt_rows(db, site)

    def do_confirm():
        return post(site.first, confirm)

    def do_other():
        if other == "read":
            return peer.get(site.url, params={"source_id": str(site.src)})
        return post(peer, request)

    a, b = concurrent(
        monkeypatch, *((do_confirm, do_other) if confirm_first else (do_other, do_confirm))
    )
    c, o = (a, b) if confirm_first else (b, a)
    conflict = other in ("same", "merge", "split", "reject")
    assert c.status_code == (409 if conflict and not confirm_first else 201)
    assert o.status_code == (
        409
        if conflict and confirm_first
        else 200
        if other in ("replay", "archive", "read")
        else 201
    )
    if other == "read":
        assert o.json()["digest"] == frozen
    if other == "replay":
        assert c.json()["id"] == o.json()["id"]
    accepted = []
    if c.status_code == 201:
        accepted.append((c.json()["id"], site.actors[0], p))
    if o.status_code == 201 and other in ("same", "independent"):
        accepted.append((o.json()["id"], site.actors[1], q if other == "independent" else p))
    for event, actor, parent in accepted:
        row = db.execute(
            """SELECT e.actor,e.parents,v.version_no,v.created_by,e.aspect_sha=v.digest,
            a.current_no,r.reply FROM topic_event e JOIN aspect_version v
            ON (v.workspace_id,v.project_id,v.aspect_id,v.version_id)=(e.workspace_id,e.project_id,e.aspect_id,e.aspect_version_id)
            JOIN aspect a ON (a.workspace_id,a.project_id,a.aspect_id)=(e.workspace_id,e.project_id,e.aspect_id) JOIN topic_request r
            ON (r.workspace_id,r.reply)=(e.workspace_id,e.id) WHERE e.id=%s""",
            (event,),
        ).fetchone()
        assert row == (actor, [UUID(parent)], 1, actor, True, 1, UUID(event))
    events = len(accepted) + int(other in ("merge", "split", "reject") and o.status_code == 201)
    history = int(other == "history")
    assert tuple(x - y for x, y in zip(counts(db, site), initial, strict=True)) == (
        len(accepted) + int(other == "create"),
        len(accepted) + int(other in ("create", "edit")),
        events,
        events + history,
        history,
    )
    assert receipts <= receipt_rows(db, site)
    assert site.read()["digest"] == frozen


def receipt_rows(db, site):
    return set(
        db.execute(
            "SELECT request_key,signature,reply,actor FROM topic_request WHERE workspace_id=%s",
            (site.w,),
        ).fetchall()
    )


@pytest.mark.parametrize("kind", ["session", "admission"])
@pytest.mark.parametrize("confirm_first", [True, False])
def test_topic_discovery_confirm_revocation(db, live, monkeypatch, kind, confirm_first):
    site = live
    initial = counts(db, site)
    confirm = site.confirm(site.parents[0])

    def revoke(conn):
        if kind == "session":
            conn.execute(
                "UPDATE access_session SET revoked_at=clock_timestamp() WHERE principal_id=%s",
                (site.actors[0],),
            )
        else:
            conn.execute(
                "UPDATE browser_admission SET topic_discovery_allowed=false WHERE source_id=%s",
                (site.src,),
            )

    with psycopg.connect(user="rs_test", autocommit=True) as audit, ThreadPoolExecutor(2) as pool:
        if confirm_first:
            with before_commit(monkeypatch) as (opened, release, holder):
                req = pool.submit(post, site.first, confirm)
                assert opened.wait(4)
                withdrawal = pool.submit(revoke, db)
                blocked_by(audit, holder[0])
                release.set()
                assert req.result().status_code == 201
                withdrawal.result()
        else:
            with db.transaction():
                revoke(db)
                req = pool.submit(post, site.first, confirm)
                blocked_by(audit, db.info.backend_pid)
            assert req.result().status_code in (401, 404)
    assert site.first.get(site.url, params={"source_id": str(site.src)}).status_code in (401, 404)
    assert tuple(x - y for x, y in zip(counts(db, site), initial, strict=True)) == (
        int(confirm_first),
    ) * 4 + (0,)


def test_topic_discovery_confirm_expiry(db, live, key):  # noqa: F811
    site = live
    initial = counts(db, site)
    confirm = site.confirm(site.parents[0])
    with (
        runtime(site.w, session_proof(site.second.cookies[COOKIE], key)) as reader,
        ThreadPoolExecutor(1) as pool,
    ):
        reader.execute("BEGIN")
        assert reader.execute(
            "SELECT topic_read(%s,%s,%s,%s,NULL,NULL,0)",
            (site.w, site.src, site.snapshot, site.result),
        ).fetchone()[0]
        db.execute(
            "UPDATE access_session SET expires_at=clock_timestamp()+interval '1.5 seconds' WHERE principal_id=%s",
            (site.actors[0],),
        )
        job = pool.submit(post, site.first, confirm)
        try:
            blocked_by(db, reader.info.backend_pid)
            db.execute(
                "SELECT pg_sleep(greatest(0,extract(epoch FROM max(expires_at)-clock_timestamp()))) FROM access_session WHERE principal_id=%s",
                (site.actors[0],),
            )
        finally:
            reader.execute("ROLLBACK")
        assert job.result().status_code in (401, 404)
    assert counts(db, site) == initial


def test_topic_discovery_confirm_rollback(db, live, key):  # noqa: F811
    site = live
    parent = site.parents[0]
    previous = site.read(candidate_id=parent)
    snapshot = (
        site.root / str(site.w) / "sha256" / site.read()["manifest"]["snapshot_object_sha256"]
    )
    initial = counts(db, site)
    receipts = receipt_rows(db, site)
    confirm = site.confirm(parent)
    for protected in (snapshot, Path(os.environ["RS_RETENTION_LEDGER"])):
        parked = protected.with_suffix(".parked")
        protected.rename(parked)
        try:
            assert post(site.first, confirm).status_code == 503
        finally:
            parked.rename(protected)
        assert site.read(candidate_id=parent) == previous
        assert counts(db, site) == initial and receipt_rows(db, site) == receipts

    proof = session_proof(site.first.cookies[COOKIE], key)
    with runtime(site.w, proof) as conn:
        invalid = confirm[1] | {"definition": A | {"include": ["same", "same"]}}
        assert direct(conn, site, invalid)["code"] == 422
        assert post(site.first, (confirm[0], invalid)).status_code == 422
        conn.execute("BEGIN")
        assert direct(conn, site, confirm[1])["code"] == 201
        assert counts(db, site) == initial
        conn.close()  # Drop the real connection with all four rows still uncommitted.
    assert counts(db, site) == initial and receipt_rows(db, site) == receipts
    assert site.read(candidate_id=parent) == previous
    first = post(site.first, confirm)
    replay = post(site.replay, confirm)
    assert (first.status_code, replay.status_code) == (201, 200)
    assert first.json()["id"] == replay.json()["id"]
    assert tuple(x - y for x, y in zip(counts(db, site), initial, strict=True)) == (1, 1, 1, 1, 0)


def test_topic_discovery_confirm_independent_workspaces(db, live, tmp_path, key, monkeypatch):  # noqa: F811
    site = live
    other_root = tmp_path / "neighbor"
    other_root.mkdir()
    with cabinet(db, other_root, key, accounts="neighbor") as other, ThreadPoolExecutor(2) as pool:
        with runtime(site.w, session_proof(site.first.cookies[COOKIE], key)) as conn:
            assert not conn.execute(
                "SELECT topic_confirm_writer(%s,%s,%s,%s)",
                (other.w, other.src, other.snapshot, other.result),
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT topic_confirm_writer(%s,%s,%s,%s)",
                (site.w, site.src, site.snapshot, other.result),
            ).fetchone()[0]
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
                conn.execute(
                    REVIEW.replace("topic_review", "topic_apply_review"),
                    (
                        site.w,
                        site.src,
                        site.snapshot,
                        site.result,
                        uuid4(),
                        site.parents[:1],
                        "review",
                        Jsonb(A),
                    ),
                )
            assert denied.value.sqlstate == "42501"
        with before_commit(monkeypatch) as (opened, release, _):
            first = pool.submit(post, site.first, site.confirm(site.parents[0]))
            assert opened.wait(4)
            peer = pool.submit(post, other.second, other.confirm(other.parents[0]))
            assert peer.result(timeout=4).status_code == 201
            assert counts(db, site)[:3] == (0, 0, 0) and counts(db, other)[:3] == (1, 1, 1)
            release.set()
            assert first.result().status_code == 201
        assert counts(db, site)[:3] == (1, 1, 1)


def test_topic_discovery_confirm_populated_upgrade(tmp_path, key, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PGDATABASE", new_db())
    subprocess.run([*ALEMBIC, "upgrade", "0019_topic_discovery"], check=True)
    migration = Path("operational/migrations/versions/0020_topic_lock_order.sql")
    helper = "topic_confirm_writer(uuid,uuid,uuid,uuid)"
    with psycopg.connect(autocommit=True) as db:
        initialize(db)
        with importer(tmp_path, monkeypatch), cabinet(db, tmp_path, key) as site:

            def detail(parent):
                return site.read(candidate_id=parent)

            p, q = site.parents
            history = operation(site.src, candidate_id=p, historical_snapshot=site.earlier)
            submit(site.first, site.url + "/history-checks", history)
            submit(site.first, *site.action("merge", [p, q]))
            (child,) = detail(p)["children"]
            members = [x["version_id"] for x in detail(child)["rows"]]
            submit(site.first, *site.action("split", [child], parts=[members[:2], members[2:]]))
            a, b = detail(child)["children"]
            submit(site.first, *site.action("reject", [a]))
            confirm = site.confirm(b)
            with runtime(site.w, session_proof(site.first.cookies[COOKIE], key)) as conn:
                event = direct(conn, site, confirm[1])
                assert event["code"] == 201
            aspect = db.execute(
                "SELECT aspect_id FROM topic_event WHERE id=%s", (event["id"],)
            ).fetchone()[0]

            def saved():
                pages = [
                    site.read(),
                    *(detail(parent) for parent in (p, q, child, a, b)),
                    site.read(candidate_id=p, historical_snapshot=site.earlier),
                ]
                aspect_page = site.first.get(f"{site.aspects}/{aspect}/versions")
                aspect_page.raise_for_status()
                rows = {}
                for table in (
                    "topic_result",
                    "topic_input",
                    "topic_candidate",
                    "topic_event",
                    "topic_request",
                    "topic_history",
                    "snapshot_member",
                    "aspect",
                    "aspect_version",
                ):
                    rows[table] = db.execute(
                        f"SELECT to_jsonb(t) FROM {table} t WHERE workspace_id=%s ORDER BY to_jsonb(t)",
                        (site.w,),
                    ).fetchall()
                return pages, aspect_page.json(), rows

            previous = saved()
            seal = capture_access(db)
            with db.transaction(force_rollback=True):
                db.execute(migration.read_text())
                assert db.execute("SELECT to_regprocedure(%s) IS NOT NULL", (helper,)).fetchone()[0]
            assert db.execute(
                "SELECT to_regprocedure(%s),to_regprocedure(%s)", (helper, CORE)
            ).fetchone() == (None, None)
            verify_access_permissions(db, seal)
            subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
            with pytest.raises(DeliveryError, match="access_review_required"):
                verify_access_permissions(db, seal)
            current = capture_access(db)
            assert current != seal
            verify_access_permissions(db, current)
            with db.transaction(force_rollback=True):
                db.execute(f"GRANT EXECUTE ON FUNCTION {CORE} TO rs_runtime")
                with pytest.raises(DeliveryError, match="restore_roles_unverified"):
                    verify_restore_roles(db)
            verify_restore_roles(db)
            assert submit(site.replay, *confirm, 200)["id"] == event["id"]
            assert saved() == previous
