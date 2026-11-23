"""Owner, restricted operator and two workspaces in a disposable PostgreSQL cluster."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational import worker
from operational.retention import capture_access, recover
from operational.tests.conftest import freeze_objects, generation, new_db, prepare_copy
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, key, login
from operational.tests.test_deliveries import call, command, csv
from operational.tests.test_schema import bind_place, scope
from operational.tests.test_worker import cli, enqueue, publish_cas, take
from reviewscope.access.http import create_app
from reviewscope.access.ledger import read_witness
from reviewscope.access.operator import provision

__all__ = ["key"]


def operator(*args, error=None):
    start = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-m", "operational.retention", *map(str, args)], capture_output=True
    )
    if error:
        assert (
            out.returncode == 2 and out.stdout == b"" and json.loads(out.stderr) == {"error": error}
        )
        return
    assert out.returncode == 0 and not out.stderr, out.stderr
    print(
        json.dumps(
            {
                "operation": args[0],
                "wall_s": round(time.monotonic() - start, 3),
                **json.loads(out.stdout)["metrics"],
            }
        )
    )
    return json.loads(out.stdout)["result"]


def ask(client, w, kind, target, action, *, request_key=None):
    key = str(request_key or uuid4())
    body = {"kind": kind, "target": str(target), "action": action, "idempotency_key": key}
    url = ROOT + f"/workspaces/{w}/retention"
    result = client.post(url, json=body, headers=CSRF)
    assert result.status_code == 200, result.content
    assert client.post(url, json=body, headers=CSRF).json() == result.json()
    assert client.post(url, json=body | {"target": str(uuid4())}, headers=CSRF).status_code == 409
    return result.json()


@contextmanager
def fresh_restore_cluster(monkeypatch, archive):
    with (
        tempfile.TemporaryDirectory(prefix="rs12-restore-") as socket,
        monkeypatch.context() as env,
    ):
        path = Path(socket)
        env.setenv("PGHOST", socket)
        env.setenv("PGDATABASE", "postgres")
        subprocess.run(
            [
                "initdb",
                "-D",
                path / "data",
                "--username=rs_test",
                "--encoding=UTF8",
                "--no-locale",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                path / "data",
                "-l",
                path / "server.log",
                "-o",
                f"-k '{socket}' -c listen_addresses='' -c log_min_error_statement=panic -c log_min_messages=panic",
                "-w",
                "start",
            ],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        try:
            with psycopg.connect(user="rs_test", autocommit=True) as admin:
                admin.execute("CREATE ROLE synthetic_oid_offset")
                admin.execute("CREATE ROLE rs_migration LOGIN CREATEDB")
                for role in ("rs_runtime", "rs_worker", "rs_retention"):
                    admin.execute("CREATE ROLE " + role + " LOGIN")
            target = new_db()
            subprocess.run(
                [
                    "pg_restore",
                    "--no-owner",
                    "--single-transaction",
                    "--exit-on-error",
                    "-d",
                    target,
                    archive,
                ],
                check=True,
            )
            env.setenv("PGDATABASE", target)
            yield env, target
        finally:
            subprocess.run(
                ["pg_ctl", "-D", path / "data", "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                check=True,
            )


def test_owner_lifecycle_restore(db, key, tmp_path, monkeypatch, caplog):
    base, root, w, s = command(db, tmp_path)
    source_inputs = csv("a")
    snap = call(base + source_inputs + ["--apply"])["snapshot_id"]
    actor = db.execute("SELECT principal_id FROM delivery_authorization").fetchone()[0]
    _, p2, s2 = scope(db, workspace=w, source_key="synthetic")
    b, p3, s3 = scope(db, source_key="synthetic")
    db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), b))
    db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'analyst','active')", (b, actor))
    for ww, pp, ss in ((w, p2, s2), (b, p3, s3)):
        for place in ("p1", "p2"):
            bind_place(db, ww, pp, ss, place)
        db.execute(
            "INSERT INTO delivery_authorization VALUES(%s,%s,current_user,%s)", (ww, ss, actor)
        )
        args = [str(ww) if a == str(w) else str(ss) if a == str(s) else a for a in base]
        call(args + source_inputs + ["--apply"])
    password = secrets.token_urlsafe(32)
    for name, ww, role in (("owner", w, "owner"), ("viewer", w, "viewer"), ("other", b, "owner")):
        provision(db, name, password, key, workspace=ww, role=role)
    handoff = tmp_path / "handoff"
    handoff.mkdir(mode=0o700)
    url = ROOT + f"/workspaces/{w}"

    def files(ww, directory=root):
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (directory / str(ww) / "sha256").iterdir()
        }

    other_files = files(b)
    with TestClient(create_app(), base_url=ORIGIN) as owner:
        assert login(owner, "owner", password).status_code == 204
        terms = owner.get(url + f"/sources/{s}/retention").json()
        assert len({x["class"] for x in terms}) == 10 and all(
            x["policy"] == "unset" and x["expires_at"] is None for x in terms
        )
        finished, _ = enqueue(owner, url + "/runs", snap)
        assert cli("before-erasure") == "succeeded"
        enqueue(owner, url + "/runs", snap)
        with worker.connect() as wr:
            lease = take(wr)
            manifest = wr.execute("SELECT job_input(%s,%s,%s)->>'manifest'", lease[:3]).fetchone()[
                0
            ]

            def export_request():
                return ask(owner, w, "source", s, "export")

            ex = export_request()
            for who, status in (("viewer", 403), ("other", 404)):
                with TestClient(create_app(), base_url=ORIGIN) as denied:
                    assert login(denied, who, password).status_code == 204
                    assert denied.get(url + "/retention/" + ex["id"]).status_code == status
                    body = dict(
                        kind="source", target=str(s), action="export", idempotency_key=str(uuid4())
                    )
                    assert (
                        denied.post(url + "/retention", json=body, headers=CSRF).status_code
                        == status
                    )
            export_args = (
                "export",
                "--request",
                ex["id"],
                "--root",
                root,
                "--owner-handoff",
                handoff,
            )
            delivery = operator(*export_args)
            assert operator(*export_args) == delivery
            with ZipFile(handoff / (ex["id"] + ".zip")) as z:
                manifest_raw = z.read("manifest.json")
                assert hashlib.sha256(manifest_raw).hexdigest() == delivery["manifest_sha256"]
                m = json.loads(manifest_raw)
                assert all(
                    hashlib.sha256(z.read(k)).hexdigest() == v["sha256"]
                    for k, v in m["entries"].items()
                )
                assert len(z.read("rows/import_row.jsonl").splitlines()) == 3
                assert {
                    json.loads(line)["source_id"]
                    for line in z.read("rows/review_version.jsonl").splitlines()
                } == {str(s)}
                raw = Path(source_inputs[source_inputs.index("--input") + 1]).read_bytes()
                assert z.read("objects/" + hashlib.sha256(raw).hexdigest()) == raw
            archive = tmp_path / "before.dump"
            subprocess.run(["pg_dump", "-Fc", "-f", archive], check=True)
            copied = generation("independent")
            frozen = freeze_objects(root, tmp_path, [w, b])
            # A surviving source gains real objects after the backup.
            later = [str(s2) if arg == str(s) else arg for arg in base]
            call(later + csv("b") + ["--apply"])
            # This whole workspace is created and erased after the recovery point.
            later_w, later_project, later_source = scope(db, source_key="synthetic")
            db.execute(
                "UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), later_w)
            )
            db.execute(
                "INSERT INTO workspace_membership SELECT %s,id,'owner','active' FROM principal WHERE login='owner'",
                (later_w,),
            )
            db.execute(
                "INSERT INTO workspace_membership VALUES (%s,%s,'analyst','active')",
                (later_w, actor),
            )
            db.execute(
                "INSERT INTO delivery_authorization VALUES(%s,%s,current_user,%s)",
                (later_w, later_source, actor),
            )
            for place in ("p1", "p2"):
                bind_place(db, later_w, later_project, later_source, place)
            later_args = [
                str(later_w) if a == str(w) else str(later_source) if a == str(s) else a
                for a in base
            ]
            call(later_args + source_inputs + ["--apply"])
            late_req = ask(owner, later_w, "workspace", later_w, "erase")
            operator(
                "confirm",
                "--request",
                late_req["id"],
                "--preview-sha256",
                late_req["preview_sha"],
                "--root",
                root,
            )
            assert operator("purge", "--request", late_req["id"], "--root", root) == "completed"
            erase = ask(owner, w, "source", s, "erase")
            assert erase["report"]["shared"]
            approval = (
                "confirm",
                "--request",
                erase["id"],
                "--preview-sha256",
                erase["preview_sha"],
                "--root",
                root,
            )
            assert operator(*approval) == "purging"
            assert operator(*approval) == "purging"
            assert owner.get(url + "/snapshots/" + snap).status_code == 404
            assert owner.get(url + "/runs/" + finished + "/status").status_code == 404
            assert owner.get(url + "/retention/" + ex["id"]).json()["state"] == "revoked"
            assert publish_cas(wr, lease, "0" * 64, 1, manifest) == "stale"
            assert wr.execute("SELECT job_input(%s,%s,%s)", lease[:3]).fetchone() == (None,)
            call(base + source_inputs + ["--apply"], error="scope_denied")
        purge = ("purge", "--request", erase["id"], "--root", root)
        marker = "synthetic-private-" + uuid4().hex
        outside = tmp_path / marker
        outside.write_bytes(b"guard")
        link = root / str(w) / "sha256" / ("0" * 64)
        link.symlink_to(outside)
        operator(*purge, error="unexpected_object")
        assert outside.read_bytes() == b"guard"
        link.unlink()
        assert operator(*purge) == operator(*purge) == "completed"
        assert set(files(w)) == {
            row[0]
            for row in db.execute("SELECT digest FROM retention_refs WHERE workspace_id=%s", (w,))
        }
        assert files(b) == other_files
        assert not db.execute(
            "SELECT FROM source WHERE workspace_id=%s AND id=%s", (w, s)
        ).fetchall()
        assert owner.get(url + f"/sources/{s2}").status_code == 200
        old = owner.cookies[COOKIE]
        live_access = capture_access(db)
        proof = read_witness()["sha256"]

        def storage_evidence(directory):
            return sorted(
                (
                    p.parent.parent.name,
                    p.name,
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                    p.stat().st_dev,
                    p.stat().st_ino,
                )
                for p in directory.glob("*/sha256/*")
            )

        before_move = storage_evidence(root)
        assert set(files(w)) - set(files(w, frozen[0]))
        moved = root.rename(tmp_path / "relocated")
        db.execute(
            "UPDATE workspace SET delivery_object_root=%s WHERE id IN (%s,%s)", (str(moved), w, b)
        )
        assert before_move == storage_evidence(moved)
        assert (proof, live_access) == (read_witness()["sha256"], capture_access(db))
        with fresh_restore_cluster(monkeypatch, archive) as (env, target):
            with psycopg.connect("", autocommit=True) as restored:
                from psycopg import sql

                restored.execute(
                    sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(target))
                )
                with TestClient(create_app(), base_url=ORIGIN) as fresh:
                    assert (
                        fresh.get(
                            ROOT + "/session", headers={"Cookie": COOKIE + "=" + old}
                        ).status_code
                        == 503
                    )
                    with env.context() as missing:
                        missing.setenv("RS_RETENTION_LEDGER", str(tmp_path / "absent"))
                        with pytest.raises(ValueError):
                            worker.connect()
                    with psycopg.connect(user="rs_worker") as limited:
                        assert limited.execute("SELECT * FROM claim_job()").fetchone() is None
                        assert limited.execute(
                            "SELECT job_input(%s,%s,%s)", lease[:3]
                        ).fetchone() == (None,)
                    state_query = """SELECT checkpoint,recovery_binding,
                        (SELECT count(*) FROM access_session WHERE revoked_at IS NULL),
                        (SELECT count(*) FROM retention_request),
                        (SELECT count(*) FROM analysis_run WHERE status='running') FROM retention_control"""
                    before_db = restored.execute(state_query).fetchone()
                    with pytest.raises(ValueError, match="restore_receipt_required"):
                        recover(restored, proof, {w: moved, b: moved}, live_access)
                    with pytest.raises(ValueError, match="destination_scope"):
                        prepare_copy(restored, frozen, moved, proof, live_access)
                    token = prepare_copy(
                        restored, frozen, copied, proof, live_access, erased=[later_w]
                    )
                    assert not (
                        {x[-2:] for x in before_move} & {x[-2:] for x in storage_evidence(copied)}
                    )
                    saved = copied.rename(tmp_path / "prepared-aside")
                    moved.rename(copied)
                    try:
                        with pytest.raises(ValueError, match="restore_receipt_invalid"):
                            recover(restored, proof, [token], live_access)
                        assert before_db == restored.execute(state_query).fetchone()
                        assert storage_evidence(copied) == before_move
                    finally:
                        copied.rename(moved)
                        saved.rename(copied)
                    with pytest.raises(ValueError, match="ledger_unverified"):
                        recover(restored, "0" * 64, [token], live_access)
                    with pytest.raises(ValueError, match="access_review_required"):
                        recover(restored, proof, [token], "0" * 64)
                    assert login(fresh, "owner", password).status_code == 503
                    from operational import retention

                    real_revoke = retention.revoke

                    def interrupted(conn, event, ids):
                        real_revoke(conn, event, ids)
                        raise OSError("synthetic replay interruption")

                    with monkeypatch.context() as patch:
                        patch.setattr(retention, "revoke", interrupted)
                        with pytest.raises(OSError, match="synthetic replay interruption"):
                            recover(restored, proof, [token], live_access)
                    assert login(fresh, "owner", password).status_code == 503
                    assert storage_evidence(moved) == before_move
                    recover(restored, proof, [token], live_access)
                    assert (
                        operator("purge", "--request", late_req["id"], "--root", copied)
                        == "completed"
                    )
                    assert (
                        fresh.get(
                            ROOT + "/session", headers={"Cookie": COOKIE + "=" + old}
                        ).status_code
                        == 401
                    )
                    assert login(fresh, "owner", password).status_code == 204
                    assert fresh.get(url + f"/sources/{s}").status_code == 404
                    assert fresh.get(url + f"/sources/{s2}").status_code == 200
                    assert (
                        operator("purge", "--request", erase["id"], "--root", copied) == "completed"
                    )
                    assert files(b, copied) == other_files
                    for kind, target_id in (("project", p2), ("workspace", w)):
                        req = ask(fresh, w, kind, target_id, "erase")
                        operator(
                            "confirm",
                            "--request",
                            req["id"],
                            "--preview-sha256",
                            req["preview_sha"],
                            "--root",
                            copied,
                        )
                        assert (
                            operator("purge", "--request", req["id"], "--root", copied)
                            == "completed"
                        )
                    assert (
                        restored.execute("SELECT FROM workspace WHERE id=%s", (w,)).fetchone()
                        is None
                    )
                    assert (
                        restored.execute(
                            "SELECT count(*) FROM review_version WHERE workspace_id=%s", (b,)
                        ).fetchone()[0]
                        == 3
                    )
                    assert files(b, copied) == other_files and files(w, copied) == {}
        assert storage_evidence(moved) == before_move
        print(
            json.dumps(
                {
                    "relocated_objects": len(before_move),
                    "unchanged": True,
                    "inventory_sha256": frozen[2],
                    "example_sha256": before_move[0][2],
                    "device": before_move[0][3],
                    "inode_before_and_after": before_move[0][4],
                    "prepared_root_inode": copied.stat().st_ino,
                    "live_root_inode": moved.stat().st_ino,
                }
            )
        )
    assert marker not in caplog.text and password not in caplog.text and old not in caplog.text


def check_bounded_owner_operations(db, tmp_path, monkeypatch):
    w, s = db.execute("SELECT workspace_id,id FROM source").fetchone()
    keyfile = tmp_path / "owner-key"
    keyfile.write_bytes(secrets.token_bytes(32))
    keyfile.chmod(0o600)
    monkeypatch.setenv("RS_ACCESS_KEY_FILE", str(keyfile))
    pw = secrets.token_urlsafe(24)
    provision(db, "boundary-owner", pw, keyfile.read_bytes(), workspace=w)
    dest = tmp_path / "handoff"
    dest.mkdir(mode=0o700)
    root = tmp_path / "objects"
    with TestClient(create_app(), base_url=ORIGIN) as owner:
        assert login(owner, "boundary-owner", pw).status_code == 204
        ex = ask(owner, w, "source", s, "export")
        result = operator("export", "--request", ex["id"], "--root", root, "--owner-handoff", dest)
        assert result["bytes"] > 4 * 1024**2
        req = ask(owner, w, "source", s, "erase")
        assert req["report"]["tables"]["import_row"] == 10000
        operator(
            "confirm",
            "--request",
            req["id"],
            "--root",
            root,
            "--preview-sha256",
            req["preview_sha"],
        )
        assert operator("purge", "--request", req["id"], "--root", root) == "completed"


def test_policy_holds_graph_changes_and_permissions(db, key, tmp_path, monkeypatch):
    base, root, w, s = command(db, tmp_path)
    call(base + csv("a") + ["--apply"])
    pw = secrets.token_urlsafe(24)
    owner = provision(db, "owner", pw, key, workspace=w)
    url = ROOT + f"/workspaces/{w}/sources/{s}/retention"
    with TestClient(create_app(), base_url=ORIGIN) as http:
        assert login(http, "owner", pw).status_code == 204
        rule = {
            "data_class": "raw",
            "authority_ref": str(uuid4()),
            "anchor": "received",
            "days": 1,
            "review_days": 15,
            "hold_ref": str(uuid4()),
        }
        assert http.put(url, json=rule, headers=CSRF).status_code == 204
        term = next(t for t in http.get(url).json() if t["class"] == "raw")
        assert (
            term["hold"]
            and term["expires_at"] is None
            and term["days"] == 1
            and term["coverage"] == "unknown"
        )
        assert (
            http.post(url + "/sweep", json={"idempotency_key": str(uuid4())}, headers=CSRF).json()[
                "state"
            ]
            == "BLOCKED_RETENTION_POLICY"
        )
        req = ask(http, w, "source", s, "erase")
        confirm_args = (
            "confirm",
            "--request",
            req["id"],
            "--root",
            root,
            "--preview-sha256",
            req["preview_sha"],
        )
        assert operator(*confirm_args) == "blocked"
        assert http.put(url, json=rule | {"hold_ref": None}, headers=CSRF).status_code == 204
        operator(*confirm_args, error="preview_changed")
        req = ask(http, w, "source", s, "erase")
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, owner),
        )
        operator(
            "confirm",
            "--request",
            req["id"],
            "--root",
            root,
            "--preview-sha256",
            req["preview_sha"],
            error="owner_required",
        )
        assert http.get(ROOT + f"/workspaces/{w}/retention/" + req["id"]).status_code == 403
        assert db.execute("SELECT count(*) FROM source WHERE id=%s", (s,)).fetchone() == (1,)
    for user in ("rs_runtime", "rs_worker", "rs_retention"):
        with psycopg.connect(user=user, autocommit=True) as limited:
            for table in ("review_version", "import_row", "workspace"):
                assert limited.execute(
                    "SELECT has_table_privilege(current_user,%s,'DELETE')", (table,)
                ).fetchone() == (False,)
            if user == "rs_retention":
                assert limited.execute("SELECT count(*) FROM review_version").fetchone() == (0,)
                with pytest.raises(psycopg.Error):
                    limited.execute(
                        "SELECT revoke_retention(%s,%s,'source',%s,%s)", (uuid4(), w, s, uuid4())
                    )
            else:
                for table in ("review_version", "import_row", "retention_request"):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        limited.execute("SELECT * FROM " + table)
    witness = Path(os.environ["RS_RETENTION_LEDGER"])
    witness.write_bytes(b"synthetic-damaged")
    with TestClient(create_app(), base_url=ORIGIN) as refused:
        assert login(refused, "owner", pw).status_code == 503
    with pytest.raises(ValueError):
        worker.connect()


def test_publication_import_and_confirmation_serialize(db, key, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from operational import import_delivery, retention

    base, root, w, s = command(db, tmp_path)
    snap = call(base + csv("a") + ["--apply"])["snapshot_id"]
    pw = secrets.token_urlsafe(24)
    provision(db, "owner-race", pw, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as client, ThreadPoolExecutor(2) as pool:
        assert login(client, "owner-race", pw).status_code == 204
        enqueue(client, ROOT + f"/workspaces/{w}/runs", snap)
        for publisher in (worker, import_delivery):
            ready, release = Event(), Event()
            req = ask(client, w, "source", s, "erase")

            def attempt(req=req):
                with psycopg.connect(user="rs_retention", autocommit=True) as conn:
                    return retention.confirm(conn, req["id"], req["preview_sha"], root)

            original = publisher.publish

            def pause(*args, ready=ready, release=release, original=original, **kwargs):
                ready.set()
                assert release.wait(10)
                return original(*args, **kwargs)

            with monkeypatch.context() as changed:
                changed.setattr(publisher, "publish", pause)
                if publisher is worker:

                    def publish_job():
                        with worker.connect() as c:
                            return worker.process(c, take(c), Event())

                    active = pool.submit(publish_job)
                else:
                    active = pool.submit(import_delivery.main, base + csv("b") + ["--apply"])
                assert ready.wait(10)
                pending = pool.submit(attempt)
                try:
                    assert not pending.done()
                finally:
                    release.set()
                assert active.result() in (0, "succeeded")
                with pytest.raises(ValueError, match="preview_changed"):
                    pending.result()
        req = ask(client, w, "source", s, "erase")
        approve = (
            "confirm",
            "--request",
            req["id"],
            "--preview-sha256",
            req["preview_sha"],
            "--root",
            root,
        )
        # Actual process loss after the independent fsync and before the DB commit.
        probe = """import os,sys
from operational import retention
retention.revoke=lambda *args: os._exit(79)
sys.exit(retention.main(sys.argv[1:]))"""
        stopped = subprocess.run(
            [sys.executable, "-c", probe, *map(str, approve)], capture_output=True
        )
        assert stopped.returncode == 79 and not stopped.stdout and not stopped.stderr
        assert client.get(ROOT + f"/workspaces/{w}/sources/{s}").status_code == 503
        assert operator(*approve) == "purging"
        assert client.get(ROOT + f"/workspaces/{w}/sources/{s}").status_code == 404
        # Crash after a real unlink. Only PostgreSQL's durable verified intent permits retry.
        interrupted = """import os,sys
from operational import retention
unlink=os.unlink
def interrupt(*args,**kwargs):
    unlink(*args,**kwargs)
    os._exit(80)
retention.os.unlink=interrupt
sys.exit(retention.main(sys.argv[1:]))"""
        purge = ("purge", "--request", req["id"], "--root", root)
        stopped = subprocess.run(
            [sys.executable, "-c", interrupted, *map(str, purge)], capture_output=True
        )
        assert stopped.returncode == 80 and not stopped.stdout and not stopped.stderr
        assert operator(*purge) == "completed"
        assert not list((root / str(w) / "sha256").iterdir())


def test_files_receipts_orphans_and_redaction(db, key, tmp_path, caplog):
    from operational.tests.test_deliveries import json_input
    from reviewscope.access import http

    caplog.set_level("INFO", logger=http.LOG.name)
    base, root, w, s = command(db, tmp_path)
    marker = "private-canary-" + uuid4().hex
    secret = tmp_path / marker
    secret.mkdir()
    payload = json_input(
        secret,
        [
            {"id": "r1", "body": marker},
            {"id": "r1", "body": marker},
            {"id": "amb", "body": "one"},
            {"id": "amb", "body": "two"},
        ],
        {"id": "source_review_id", "body": "text"},
    )
    snap = call(base + payload + ["--apply"])["snapshot_id"]
    pw = secrets.token_urlsafe(24)
    provision(db, "owner-files", pw, key, workspace=w)
    dest = tmp_path / "handoff"
    dest.mkdir(mode=0o700)
    folder = root / str(w) / "sha256"
    sha = db.execute("SELECT file_sha256 FROM import_batch").fetchone()[0]
    obj = folder / sha
    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert login(client, "owner-files", pw).status_code == 204
        enqueue(client, ROOT + f"/workspaces/{w}/runs", snap)
        assert cli("workspace-before-purge") == "succeeded"
        req = ask(client, w, "source", s, "export")
        args = ("export", "--request", req["id"], "--root", root, "--owner-handoff", dest)
        saved = obj.read_bytes()
        obj.unlink()
        operator(*args, error="operation_refused")
        obj.write_bytes(b"corrupt")
        obj.chmod(0o400)
        operator(*args, error="checksum_mismatch")
        obj.unlink()
        obj.write_bytes(saved)
        obj.chmod(0o400)
        assert not list(dest.iterdir())
        interrupted = """import os,sys
from operational import retention,retention_export
publish=os.link
def interrupt(*args,**kwargs):
    publish(*args,**kwargs)
    os._exit(81)
retention_export.os.link=interrupt
sys.exit(retention.main(sys.argv[1:]))"""
        stopped = subprocess.run(
            [sys.executable, "-c", interrupted, *map(str, args)], capture_output=True
        )
        assert stopped.returncode == 81 and not stopped.stdout and not stopped.stderr
        operator(*args)
        with ZipFile(dest / (req["id"] + ".zip")) as archive:
            rows = list(map(json.loads, archive.read("rows/import_row.jsonl").splitlines()))
            assert {r["status"] for r in rows} == {"accepted", "quarantined", "confirmed_duplicate"}
        erase = ask(client, w, "workspace", w, "erase")
        assert erase["report"]["tables"]["analysis_run"] == 1
        outside = tmp_path / "other"
        outside.mkdir(mode=0o700)
        args = (
            "confirm",
            "--request",
            erase["id"],
            "--preview-sha256",
            erase["preview_sha"],
            "--root",
        )
        operator(*args, outside, error="object_root_unverified")
        operator(*args, root)
        data = b"uncommitted synthetic output"
        orphan = folder / hashlib.sha256(data).hexdigest()
        orphan.write_bytes(data)
        orphan.chmod(0o400)
        purge = ("purge", "--request", erase["id"], "--root", root)
        assert operator(*purge) == "purging" and orphan.is_file()
        earlier = time.time() - 65
        os.utime(orphan, (earlier, earlier))
        assert operator(*purge) == "completed" and not orphan.exists()
        denied = client.get(ROOT + "/workspaces/" + marker)
        assert denied.status_code == 404
        sql_log = Path(os.environ["RS_TEST_PG_SOCKET"], "server.log").read_text()
        observed = caplog.text + sql_log + denied.text
        assert not any(
            value in observed for value in (marker, pw, client.cookies[COOKIE], str(secret))
        )


def test_expiry_preview_and_damaged_reference(db, key, tmp_path, caplog):
    from operational.tests.test_deliveries import COVER

    base, root, w, s = command(db, tmp_path)
    (tmp_path / "coverage").write_text(
        json.dumps(COVER | {"received_at": "2020-01-01T00:00:00Z", "coverage": "partial"})
    )
    call(base + csv("a") + ["--apply"])
    pw = secrets.token_urlsafe(24)
    provision(db, "expiry-owner", pw, key, workspace=w)
    url = ROOT + f"/workspaces/{w}/sources/{s}/retention"
    with TestClient(create_app(), base_url=ORIGIN) as owner:
        assert login(owner, "expiry-owner", pw).status_code == 204
        for cls in ("raw", "configuration", "positions", "versions", "snapshot"):
            rule = {
                "data_class": cls,
                "authority_ref": str(uuid4()),
                "anchor": "received",
                "days": 2,
                "review_days": 30,
                "hold_ref": None,
            }
            assert owner.put(url, json=rule, headers=CSRF).status_code == 204
        body = {"idempotency_key": str(uuid4())}
        eligible = owner.post(url + "/sweep", json=body, headers=CSRF)
        assert eligible.status_code == 200 and eligible.json()["state"] == "requested"
        assert eligible.json()["report"]["tables"]["review_version"] == 3
        assert owner.post(url + "/sweep", json=body, headers=CSRF).json() == eligible.json()
        assert owner.get(url).json()[0]["expires_at"].startswith("2020-01-03")
        assert db.execute("SELECT count(*) FROM retention_object").fetchone() == (0,)
        # Simulate a damaged restored receipt; no filesystem operation uses its value.
        marker = "private-" + uuid4().hex
        with db.transaction():
            db.execute("ALTER TABLE import_batch DISABLE TRIGGER immutable_rows")
            db.execute(
                "UPDATE import_batch SET delivery=jsonb_set(delivery,'{mapping_sha256}',to_jsonb(%s::text))",
                (marker,),
            )
            db.execute("ALTER TABLE import_batch ENABLE TRIGGER immutable_rows")
        failed = owner.post(
            ROOT + f"/workspaces/{w}/retention",
            headers=CSRF,
            json={
                "kind": "source",
                "target": str(s),
                "action": "erase",
                "idempotency_key": str(uuid4()),
            },
        )
        assert failed.status_code == 503 and failed.json() == {"error": "access_unavailable"}
        assert marker not in caplog.text + failed.text and not (root / marker).exists()
