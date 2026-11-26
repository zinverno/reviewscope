"""Observe the order of ordinary import and decision transactions, without making them deadlock."""

import hashlib
import json
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_storage import cabinet, enqueue
from operational.tests.test_deliveries import FIX, csv
from operational.tests.test_topic_discovery_concurrency import blocked_by
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


def test_import_waits_before_source_lock(db, tmp_path, key):  # noqa: F811
    with cabinet(db, tmp_path, key) as (scope, reader, _, root, _, _):
        w, src = scope[:2]
        args = [
            "--workspace",
            str(w),
            "--source",
            str(src),
            "--object-root",
            str(root),
            "--coverage-passport",
            str(tmp_path / "coverage"),
            *csv("b"),
            "--apply",
        ]
        with (
            psycopg.connect(user="rs_test", autocommit=True) as audit,
            ThreadPoolExecutor(2) as pool,
        ):
            # Hold the initiating actor's membership solely to pause the real job_writer
            # after workspace UPDATE, before decision_scope. The importer has its own actor.
            with db.transaction():
                db.execute(
                    """SELECT FROM workspace_membership m JOIN principal p ON m.principal_id=p.id
                              WHERE m.workspace_id=%s AND p.login='annotator' FOR UPDATE OF m""",
                    (w,),
                )
                decision = pool.submit(enqueue, reader, scope)
                assert blocked_by(audit, db.info.backend_pid) == reader.info.backend_pid
                delivery = pool.submit(
                    subprocess.run,
                    [sys.executable, "-m", "operational.import_delivery", *args],
                    capture_output=True,
                    timeout=15,
                )
                importer = blocked_by(audit, reader.info.backend_pid)
                try:
                    with audit.transaction():
                        audit.execute(
                            "SELECT FROM source WHERE workspace_id=%s AND id=%s FOR KEY SHARE NOWAIT",
                            (w, src),
                        )
                    source_available = True
                except psycopg.errors.LockNotAvailable:
                    source_available = False
                    # Stop only our test importer before releasing the barrier.
                    assert audit.execute("SELECT pg_cancel_backend(%s)", (importer,)).fetchone()[0]
                    stopped = delivery.result(timeout=5)
                    assert stopped.returncode == 2 and not stopped.stdout
                    assert json.loads(stopped.stderr) == {"error": "delivery_refused"}
            assert decision.result(timeout=5)[0] == 202
            assert source_available, "import already owns source UPDATE while waiting for workspace"
            completed = delivery.result(timeout=5)
            assert (
                completed.returncode == 0 and json.loads(completed.stdout)["outcome"] == "applied"
            )


def test_monitor_browser_import_waits_before_source_lock(db, tmp_path, key, monkeypatch):  # noqa: F811
    with (
        cabinet(db, tmp_path, key) as (scope, reader, _, _, _, _),
        importer(tmp_path, monkeypatch),
        TestClient(create_app(), base_url=ORIGIN) as web,
        psycopg.connect(user="rs_test", autocommit=True) as audit,
        ThreadPoolExecutor(2) as pool,
    ):
        w, src = scope[:2]
        password = secrets.token_urlsafe(24)
        provision(db, "browser-order", password, key, workspace=w)
        assert login(web, "browser-order", password).status_code == 204
        parts = [(FIX / n).read_bytes() for n in ("mapping.json", "b.passport.json", "b.csv")]
        digest = hashlib.sha256(parts[-1]).hexdigest()
        loc = db.execute(
            "SELECT location_id FROM source_location WHERE workspace_id=%s AND source_id=%s ORDER BY location_id",
            (w, src),
        ).fetchall()
        headers = CSRF | {
            "content-type": "application/octet-stream",
            "x-rs-file-sha256": digest,
            "x-rs-parts": ",".join(str(len(v)) for v in parts),
            "x-rs-locations": ",".join(str(v[0]) for v in loc),
            "x-rs-format": "csv",
        }
        db.execute(
            "INSERT INTO browser_admission(workspace_id,source_id,checksum) VALUES (%s,%s,%s)",
            (w, src, digest),
        )
        path = f"{ROOT}/workspaces/{w}/sources/{src}/uploads/{uuid4()}"
        body = b"".join(parts)
        preview = web.post(path + "/preview", headers=headers, content=body)
        assert preview.status_code == 200
        headers["x-rs-fingerprint"] = preview.json()["fingerprint"]
        with db.transaction():
            db.execute(
                """SELECT FROM workspace_membership m JOIN principal p ON m.principal_id=p.id
                         WHERE m.workspace_id=%s AND p.login='annotator' FOR UPDATE OF m""",
                (w,),
            )
            decision = pool.submit(enqueue, reader, scope)
            assert blocked_by(audit, db.info.backend_pid) == reader.info.backend_pid
            delivery = pool.submit(web.post, path + "/confirm", headers=headers, content=body)
            waiting = blocked_by(audit, reader.info.backend_pid)
            assert audit.execute(
                "SELECT usename FROM pg_stat_activity WHERE pid=%s", (waiting,)
            ).fetchone() == ("rs_upload",)
            try:
                with audit.transaction():
                    audit.execute(
                        "SELECT FROM source WHERE workspace_id=%s AND id=%s FOR KEY SHARE NOWAIT",
                        (w, src),
                    )
                source_available = True
            except psycopg.errors.LockNotAvailable:
                source_available = False
                assert audit.execute("SELECT pg_cancel_backend(%s)", (waiting,)).fetchone()[0]
        assert decision.result(timeout=5)[0] == 202
        assert source_available, "browser importer holds source while waiting for workspace"
        reply = delivery.result(timeout=8)
        assert reply.status_code == 200 and reply.json()["snapshot_id"]
