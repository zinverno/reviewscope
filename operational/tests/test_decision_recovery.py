"""Prove invisibility and restart from zero after actual loss of a decision worker."""

import signal
import subprocess
import sys
from threading import Event
from time import sleep

from operational import worker
from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import cabinet, enqueue, page, query

CRASH = """
import os, signal, sys
from operational import worker, decision_worker

def kill():
    os.kill(os.getpid(), signal.SIGKILL)

if sys.argv[1] == "batch":
    stream = decision_worker.run_batches
    def interrupt(params, rows, pulse, consume):
        def first(batch):
            consume(batch)
            kill()
        return stream(params, rows, pulse, first)
    decision_worker.run_batches = interrupt
else:
    publish = worker.publish
    def interrupt(*args):
        publish(*args)
        kill()
    worker.publish = interrupt
sys.argv = ["worker", "--once"]
worker.main()
"""


def test_decision_sigkill_after_batch_and_before_commit(db, tmp_path, key):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, size=40) as (scope, reader, _, root, _, _),
        worker.connect() as conn,
    ):
        code, run = enqueue(reader, scope)
        assert code == 202
        store = root / str(scope[0]) / "sha256"
        before = set(store.iterdir())
        for attempt, (failpoint, count) in enumerate([("batch", 32), ("publish", 40)], 1):
            # Invoke the normal claim/process entry with disposable, local worker credentials.
            process = subprocess.run(
                [sys.executable, "-c", CRASH, failpoint],
                capture_output=True,
                timeout=60,
            )
            assert process.returncode == -signal.SIGKILL and not process.stderr
            assert query(db, "count(*) FROM decision_row WHERE run_id=%s", (run,)) == count
            hidden = page(reader, scope, run)
            assert (
                hidden["compute_state"] == "NOT_COMPUTED"
                and hidden["summary"] is None
                and hidden["rows"] == []
            )
            assert query(db, "count(*) FROM artifact WHERE run_id=%s", (run,)) == 0
            old = db.execute(
                "SELECT workspace_id,id,lease_token FROM analysis_run WHERE id=%s AND attempt=%s",
                (run, attempt),
            ).fetchone()
            db.execute(
                """UPDATE analysis_run SET heartbeat_at=now()-interval '40s',
                                     lease_until=now()-interval '1s' WHERE id=%s""",
                (run,),
            )
            assert conn.execute("SELECT * FROM claim_job()").fetchone() is None
            assert query(conn, "decision_manifest(%s,%s,%s)", old) is None
            assert query(db, "count(*) FROM decision_row WHERE run_id=%s", (run,)) == 0
            sleep(attempt + 0.05)
        (orphan,) = set(store.iterdir()) - before
        job = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert job[3] == 3 and worker.process(conn, job, Event()) == "succeeded"
        assert db.execute("SELECT checksum FROM artifact WHERE run_id=%s", (run,)).fetchall() == [
            (orphan.name,)
        ]
        assert (
            query(db, "count(*) FROM decision_row WHERE run_id=%s AND generation=3", (run,)) == 40
        )
        assert page(reader, scope, run)["summary"]["N_evaluated"] == 40


def test_worker_without_packaged_head_stays_not_computed(db, tmp_path, key, monkeypatch):  # noqa: F811
    import os
    from pathlib import Path

    from reviewscope import aspect_decisions

    package = Path(aspect_decisions.__file__).parent
    isolated = tmp_path / "without-weights" / "reviewscope"
    isolated.mkdir(parents=True)
    for p in package.iterdir():
        if p.name != "resources":
            (isolated / p.name).symlink_to(p)
    resources = isolated / "resources"
    resources.mkdir()
    (resources / "decision-definitions.json").symlink_to(
        package / "resources/decision-definitions.json"
    )
    with cabinet(db, tmp_path, key) as (scope, reader, _, _, _, _), worker.connect() as conn:
        code, run = enqueue(reader, scope)
        assert code == 202
        monkeypatch.setenv(
            "PYTHONPATH", str(isolated.parent) + os.pathsep + os.environ["PYTHONPATH"]
        )
        job = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert worker.process(conn, job, Event()) == "failed"
        view = page(reader, scope, run)
        assert (view["reason"], view["compute_state"], view["summary"], view["rows"]) == (
            "MODEL_UNAVAILABLE",
            "NOT_COMPUTED",
            None,
            [],
        )
        assert query(db, "count(*) FROM artifact WHERE run_id=%s", (run,)) == 0
