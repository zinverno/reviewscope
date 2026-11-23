"""Synthetic end-to-end contracts using real login, a real pool and the restricted SQL login."""

import hashlib
import logging
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.test_schema import identity, receipt, run, scope, seal
from reviewscope.access import http as access_http
from reviewscope.access import store
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

ROOT = "/api/v1"
ORIGIN = "https://127.0.0.1:8001"
CSRF = {"Origin": ORIGIN, "X-RS-CSRF": "1"}
COOKIE = "__Host-rs_session"


@pytest.fixture
def key(tmp_path, monkeypatch):
    path = tmp_path / "server-key"
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    path.chmod(0o600)
    monkeypatch.setenv("RS_ACCESS_KEY_FILE", str(path))
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK", raising=False)
    return secret


def dataset(db, marker):
    w, project, source = scope(db, source_key="same")
    (place,) = db.execute(
        "INSERT INTO location(workspace_id,project_id) VALUES (%s,%s) RETURNING id", (w, project)
    ).fetchone()
    db.execute("INSERT INTO source_location VALUES (%s,%s,%s,'p1',%s)", (w, project, source, place))
    i = identity(db, w, source, "r1")
    b = receipt(db, w, source)
    (v,) = db.execute(
        """INSERT INTO review_version(workspace_id,source_id,identity_id,batch_id,version_no,
                     normalization_version,external_place_id,text_content) VALUES (%s,%s,%s,%s,1,'v1','p1',%s)
                     RETURNING id""",
        (w, source, i, b, marker),
    ).fetchone()
    snap = seal(db, w, source, b, [v])
    return w, {"sources": source, "snapshots": snap, "runs": run(db, w, snap)}


def login(client, name, password, csrf=CSRF):
    return client.post(
        ROOT + "/session/login", headers=csrf, json={"login": name, "password": password}
    )


def test_end_to_end_isolation_roles_and_session(db, key, caplog):
    marker = "synthetic-private-" + uuid4().hex
    wa, data_a = dataset(db, marker)
    wb, data_b = dataset(db, marker)
    pw = secrets.token_urlsafe(24)
    bob_pw = secrets.token_urlsafe(24)
    a = provision(db, "Alice", pw, key, workspace=wa)
    b = provision(db, "Bob", bob_pw, key, workspace=wb)
    caplog.set_level(logging.INFO, logger="reviewscope.access.requests")
    app = create_app()
    with TestClient(app, base_url=ORIGIN) as alice, TestClient(app, base_url=ORIGIN) as bob:
        response = login(alice, "  ALICE  ", pw)
        assert response.status_code == 204
        assert all(
            flag in response.headers["set-cookie"]
            for flag in ("HttpOnly", "SameSite=strict", "Secure", "Path=/")
        )
        assert login(bob, "bob", bob_pw).status_code == 204
        token = alice.cookies[COOKIE]
        digest = hashlib.sha256(session_proof(token, key).encode()).digest()
        assert db.execute(
            "SELECT token_hash FROM access_session WHERE principal_id=%s", (a,)
        ).fetchone() == (digest,)

        def check(client, own, own_data, foreign, foreign_data):
            ws = client.get(ROOT + "/workspaces").json()
            assert [x["id"] for x in ws] == [str(own)]
            assert client.get(ROOT + "/session").json()["workspaces"] == ws
            assert client.get(ROOT + "/workspaces?offset=1").json() == []
            for resource, oid in own_data.items():
                path = ROOT + f"/workspaces/{own}/{resource}"
                obj = client.get(path + f"/{oid}").json()
                assert obj["id"] == str(oid) and client.get(path).json() == [obj]
                assert client.get(path + "?offset=50").json() == []
                for denied in (
                    path + f"/{foreign_data[resource]}",
                    path + f"/{uuid4()}",
                    ROOT + f"/workspaces/{foreign}/{resource}",
                    ROOT + f"/workspaces/{foreign}/{resource}/{foreign_data[resource]}",
                    ROOT + f"/workspaces/{uuid4()}/{resource}",
                ):
                    resp = client.get(
                        denied, headers={"X-Workspace-ID": str(foreign), "X-Role": "owner"}
                    )
                    assert (resp.status_code, resp.content) == (404, b'{"error":"not_found"}')
                assert marker not in str(obj)

        with ThreadPoolExecutor(2) as workers:
            f = workers.submit(check, alice, wa, data_a, wb, data_b)
            check(bob, wb, data_b, wa, data_a)
            f.result()
        member = f"/members/{b}"

        def change(client, w, role, state="active"):
            return client.patch(
                ROOT + f"/workspaces/{w}" + member,
                headers=CSRF,
                json={"role": role, "state": state},
            )

        assert change(alice, wb, "analyst").status_code == 404
        assert change(alice, wa, "analyst").status_code == 404
        db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'viewer','active')", (wa, b))
        for role in ("viewer", "analyst"):
            assert change(alice, wa, role).status_code == 204
            roles = {row["id"]: row["role"] for row in bob.get(ROOT + "/workspaces").json()}
            assert roles == {str(wa): role, str(wb): "owner"}
            assert bob.get(ROOT + f"/workspaces/{wa}/runs").status_code == 200
            assert change(bob, wa, "owner").status_code == 403
        assert change(alice, wa, "viewer", "inactive").status_code == 204
        assert bob.get(ROOT + f"/workspaces/{wa}/runs").status_code == 404
        assert db.execute(
            """SELECT actor_id,old_role,new_state FROM membership_event WHERE workspace_id=%s
                          AND subject_id=%s ORDER BY id DESC LIMIT 1""",
            (wa, b),
        ).fetchone() == (a, "analyst", "inactive")
        assert alice.post(ROOT + "/session/logout", headers=CSRF).status_code == 204
        for value in (token, token[:-2] + "!!", secrets.token_urlsafe(32), ""):
            r = alice.get(
                ROOT + "/session",
                headers={"Cookie": COOKIE + "=" + value, "X-Principal-ID": str(a)},
            )
            assert (r.status_code, r.json()) == (401, {"error": "unauthenticated"})
        db.execute(
            """UPDATE access_session SET created_at=now()-interval '2 days',
                      expires_at=now()-interval '1 day' WHERE principal_id=%s""",
            (b,),
        )
        assert bob.get(ROOT + "/workspaces").status_code == 401
    logged = [r.getMessage() for r in caplog.records if r.name == "reviewscope.access.requests"]
    assert all(
        s not in str(logged)
        for s in (token, pw, bob_pw, marker, str(wa), str(wb), "alice", "bob", "SELECT")
    )


def test_sql_runtime_pool_parallel_rollback(db, key):
    users = []
    for account in ("tenant-a", "tenant-b"):
        w, data = dataset(db, account)
        pw = secrets.token_urlsafe(32)
        pid = provision(db, account, pw, key, workspace=w)
        with TestClient(create_app(), base_url=ORIGIN) as http:
            assert login(http, account, pw).status_code == 204
            proof = session_proof(http.cookies[COOKIE], key)
        users.append((w, data, pid, proof))
    with psycopg.connect(user="rs_runtime", autocommit=True) as runtime:
        assert runtime.execute("SELECT current_user").fetchone() == ("rs_runtime",)
        assert db.execute("SELECT current_user").fetchone() == ("rs_migration",)
        for statement in (
            "SELECT text_content FROM review_version",
            "SELECT verifier FROM password_credential",
            "SELECT token_hash FROM access_session",
            "SELECT version_ids FROM snapshot",
            "CREATE TABLE unexpected (i int)",
            "CREATE TEMP TABLE unexpected (i int)",
            "UPDATE workspace_membership SET role='owner'",
            "DELETE FROM analysis_run",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(statement)
        with runtime.transaction():
            runtime.execute(
                "SELECT set_config('app.principal_id',%s,true),set_config('rs.workspace',%s,true)",
                (str(users[1][2]), str(users[1][0])),
            )
            assert runtime.execute("SELECT access_actor()").fetchone() == (None,)
            assert runtime.execute("SELECT id FROM workspace").fetchall() == []
            assert runtime.execute("SELECT id FROM source").fetchall() == []
    engine = store.pool()
    barrier = Barrier(2)

    def read(user, other):
        w, data, pid, proof = user
        foreign, foreign_data, *_ = other
        with store.transaction(engine) as cur:
            assert store.identify(cur, proof) == pid
            store.authorize(cur, pid, w, "read")
            cur.execute("SELECT pg_backend_pid() AS pid")
            backend = cur.fetchone()["pid"]
            barrier.wait(timeout=10)
            for res, (table, _) in store.METADATA.items():
                cur.execute(f"SELECT id FROM {table}")
                assert cur.fetchall() == [{"id": data[res]}]
                cur.execute(f"SELECT id FROM {table} WHERE id=%s", (foreign_data[res],))
                assert cur.fetchall() == []
            cur.execute("SELECT set_config('rs.workspace',%s,true)", (str(foreign),))
            cur.execute("SELECT id FROM source")
            assert cur.fetchall() == []
            cur.execute("SELECT change_member(%s,%s,'owner','active') AS status", (foreign, pid))
            assert cur.fetchone()["status"] == 404
            return backend

    try:
        with ThreadPoolExecutor(2) as workers:
            f = workers.submit(read, *users)
            backends = {read(*reversed(users)), f.result()}
        assert len(backends) == 2
        with pytest.raises(HTTPException, match="503"), store.transaction(engine) as cur:
            store.identify(cur, users[0][3])
            store.authorize(cur, users[0][2], users[0][0], "read")
            cur.execute("SELECT pg_backend_pid() AS pid")
            failed_backend = cur.fetchone()["pid"]
            cur.execute("SELECT 1/0")
        reused = set()
        w, data, pid, proof = users[1]
        for _ in backends:
            with store.transaction(engine) as cur:
                cur.execute(
                    "SELECT pg_backend_pid() AS pid, access_actor() AS actor, "
                    "nullif(current_setting('rs.workspace',true),'') AS w"
                )
                state = cur.fetchone()
                assert state["pid"] in backends and state["actor"] is None and state["w"] is None
                reused.add(state["pid"])
                assert store.identify(cur, proof) == pid
                store.authorize(cur, pid, w, "read")
                cur.execute("SELECT id FROM source")
                assert cur.fetchall() == [{"id": data["sources"]}]
        assert reused == backends and failed_backend in reused
    finally:
        engine.dispose()


def test_login_limit_and_http_loopback(db, key, monkeypatch):
    w, _, _ = scope(db)
    pw = secrets.token_urlsafe(24)
    provision(db, "limited", pw, key, workspace=w)
    monkeypatch.setenv("RS_ACCESS_HTTP_LOOPBACK", "1")
    origin = ORIGIN.replace("https:", "http:")
    csrf = {**CSRF, "Origin": origin}
    clock = [100.0]
    monkeypatch.setattr(access_http, "monotonic", lambda: clock[0])
    with TestClient(create_app(), base_url=origin, client=("127.0.0.1", 1234)) as http:
        result = login(http, "limited", pw, csrf)
        assert result.status_code == 204 and "Secure" not in result.headers["set-cookie"]
        for name in ("unknown", "limited", "unknown", "limited"):
            bad = login(http, name, secrets.token_urlsafe(20), csrf)
            assert (bad.status_code, bad.json()) == (401, {"error": "unauthenticated"})
        clock[0] += 59.99
        assert login(http, "limited", pw, csrf).status_code == 429
        clock[0] += 0.02
        assert login(http, "limited", pw, csrf).status_code == 204
        db.execute("UPDATE principal SET state='inactive'")
        assert http.get(ROOT + "/session").status_code == 401
        db.execute("UPDATE principal SET state='active'")
        assert http.get(ROOT + "/session").status_code == 401


def test_legacy_upgrade_and_preservation(monkeypatch, key):
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "0001_operational"], check=True)
    with psycopg.connect("", autocommit=True) as db:
        w, data = dataset(db, "synthetic legacy version")
        old = uuid4()
        db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'analyst','inactive')", (w, old))
        queries = (
            "SELECT * FROM review_version",
            "SELECT * FROM snapshot",
            "SELECT * FROM snapshot_member",
            "SELECT * FROM analysis_run",
            "SELECT * FROM workspace_membership",
        )
        before = [db.execute(q).fetchall() for q in queries]
        result = subprocess.run([*ALEMBIC, "upgrade", "head"], capture_output=True)
        assert result.returncode and b"Unmapped membership" in result.stdout + result.stderr
        subprocess.run([*ALEMBIC, "upgrade", "0002_principals"], check=True)
        assert db.execute("SELECT id FROM principal").fetchall() == []
        provision(db, "verified", secrets.token_urlsafe(24), key, legacy_id=old)
        for _ in range(2):
            subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
        assert [db.execute(q).fetchall() for q in queries] == before
        assert db.execute(
            "SELECT subject_id,new_state,actor_id FROM membership_event"
        ).fetchone() == (old, "inactive", None)
        assert db.execute("SELECT id FROM snapshot").fetchone() == (data["snapshots"],)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute(
                "INSERT INTO workspace_membership VALUES (%s,%s,'viewer','active')", (w, uuid4())
            )


@pytest.mark.parametrize(
    "failure", ["absent", "incompatible", "damaged", "rls", "unreachable", "missing_key"]
)
def test_fail_closed(db, key, monkeypatch, failure):
    w, _, _ = scope(db)
    pw = secrets.token_urlsafe(24)
    provision(db, "fixture", pw, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as http:
        assert login(http, "fixture", pw).status_code == 204
        token = http.cookies[COOKIE]
    if failure == "absent":
        monkeypatch.setenv("PGDATABASE", new_db())
    elif failure == "incompatible":
        db.execute("UPDATE alembic_version SET version_num='unknown'")
    elif failure == "damaged":
        db.execute("ALTER TABLE workspace RENAME COLUMN name TO unavailable")
    elif failure == "rls":
        db.execute("ALTER TABLE source DISABLE ROW LEVEL SECURITY")
    elif failure == "unreachable":
        monkeypatch.setenv("PGHOST", "/missing-synthetic-socket")
    else:
        monkeypatch.delenv("RS_ACCESS_KEY_FILE")
    with TestClient(create_app(), base_url=ORIGIN) as http:
        r = http.get(ROOT + "/workspaces", headers={"Cookie": COOKIE + "=" + token})
        assert (r.status_code, r.json()) == (503, {"error": "access_unavailable"})
