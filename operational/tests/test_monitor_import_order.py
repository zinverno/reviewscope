"""Observe the order of ordinary import and decision transactions, without making them deadlock."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import psycopg

from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import cabinet, enqueue
from operational.tests.test_deliveries import csv
from operational.tests.test_topic_discovery_concurrency import blocked_by


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
