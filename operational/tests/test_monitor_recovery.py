"""Fence exact replays and a killed monitor worker with already-staged reused rows."""

import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

from operational import worker
from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import FIELDS, cabinet, query, runtime
from operational.tests.test_deliveries import call, json_input
from operational.tests.test_topic_discovery_concurrency import blocked_by
from reviewscope.aspect_decisions import definitions


def test_monitor_replay_and_worker_loss(db, tmp_path, key):  # noqa: F811
    rows = [{"id": "one", "text": "Очередь в зале."}]
    with (
        cabinet(db, tmp_path, key, definition=definitions()["queue"], inputs=rows) as (
            scope,
            reader,
            proof,
            root,
            _,
            p,
        ),
        worker.connect() as conn,
    ):
        w, src, s, a, av = scope
        db.execute("UPDATE browser_admission SET monitor_allowed=true WHERE source_id=%s", (src,))
        save = query(
            reader,
            "monitor_save(%s,%s,%s,NULL,NULL,%s,%s,'race',1,1,'accepted_in_delivery',false)",
            (w, p, src, a, av),
        )
        m, v = save["monitor_id"], save["version_id"]
        start = "monitor_start(%s,%s,%s,%s,%s,%s,%s,%s)"
        args = (w, p, src, m, v, s, None, uuid4())
        with runtime(w, proof) as peer, ThreadPoolExecutor(1) as pool:
            with reader.transaction():
                first = query(reader, start, args)
                second = pool.submit(query, peer, start, args)
                assert blocked_by(db, reader.info.backend_pid) == peer.info.backend_pid
            assert second.result(timeout=4) == first | {"code": 200}
        assert first["code"] == 202 and query(reader, start, (*args[:-1], uuid4()))["code"] == 409
        job = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert worker.process(conn, job, Event()) == "succeeded"
        second = call(
            [
                "--workspace",
                str(w),
                "--source",
                str(src),
                "--object-root",
                str(root),
                "--coverage-passport",
                str(tmp_path / "coverage"),
                *json_input(tmp_path, rows, FIELDS),
                "--apply",
            ]
        )["snapshot_id"]
        res = query(reader, start, (w, p, src, m, v, second, first["run_id"], uuid4()))
        old = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert res["code"] == 202 and str(old[1]) == res["decision_run_id"]
        probe = """
import json,os,signal,sys
from threading import Event
from operational import worker
publish=worker.publish
def stop(*args):
    result=publish(*args)
    os.kill(os.getpid(),signal.SIGSTOP)
    return result
worker.publish=stop
with worker.connect() as conn:
    worker.process(conn,json.load(sys.stdin),Event())
"""
        with subprocess.Popen(
            [sys.executable, "-c", probe], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
        ) as child:
            child.stdin.write(json.dumps(old, default=str).encode())
            child.stdin.close()
            try:
                deadline = monotonic() + 12
                while True:
                    pid, state = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
                    if pid:
                        assert os.WIFSTOPPED(state)
                        break
                    assert monotonic() < deadline
                    sleep(0.02)
                assert query(db, "count(*) FROM monitor_reuse WHERE run_id=%s", (old[1],)) == 1
                assert query(db, "count(*) FROM monitor_receipt WHERE run_id=%s", (old[1],)) == 0
                assert query(db, "count(*) FROM artifact WHERE run_id=%s", (old[1],)) == 0
            finally:
                child.kill()
                child.wait(timeout=5)
            assert child.returncode == -signal.SIGKILL
        deadline = monotonic() + 22
        while True:
            retry = conn.execute("SELECT * FROM claim_job()").fetchone()
            if retry:
                break
            assert monotonic() < deadline
            sleep(0.1)
        assert retry[1] == old[1] and retry[2] != old[2]
        assert query(conn, "monitor_parent(%s,%s,%s)", old[:3]) is None
        assert query(db, "count(*) FROM monitor_reuse WHERE run_id=%s", (old[1],)) == 0
        assert worker.process(conn, retry, Event()) == "succeeded"
        assert query(db, "count(*) FROM artifact WHERE run_id=%s", (old[1],)) == 1
        assert (
            query(db, "document->>'change_state' FROM monitor_receipt WHERE run_id=%s", (old[1],))
            == "NO_VERSION_CHANGES"
        )


def test_monitor_edit_and_topic_confirmation_order(db, tmp_path, key, monkeypatch):  # noqa: F811
    from operational.tests import test_topic_discovery_concurrency as topic
    from operational.tests.test_access import CSRF
    from operational.tests.test_aspects import create
    from operational.tests.test_browser_delivery import importer

    with importer(tmp_path, monkeypatch), topic.cabinet(db, tmp_path, key) as site:
        av = create(site.first, site.aspects, definitions()["queue"])
        base = site.aspects.removesuffix("/aspects") + "/monitors"
        spec = dict(
            name="Проверка очереди",
            source_id=str(site.src),
            aspect_id=av["aspect_id"],
            aspect_version_id=av["version_id"],
            min_eligible=1,
            threshold_count=1,
            selection="accepted_in_delivery",
        )
        reply = site.first.post(base, json=spec, headers=CSRF)
        assert reply.status_code == 201
        current = reply.json()
        path = base + "/" + current["monitor_id"] + "/versions"
        for reverse, parent in enumerate(site.parents):
            body = spec | {"expected_version_id": current["version_id"]}

            def edit(body=body):
                return site.second.post(path, json=body, headers=CSRF)

            def confirm(parent=parent):
                return topic.post(site.first, site.confirm(parent))

            calls = (confirm, edit) if reverse else (edit, confirm)
            replies = topic.concurrent(monkeypatch, *calls)
            assert [r.status_code for r in replies] == [201, 201]
            current = replies[1 if reverse else 0].json()
        body = spec | {"expected_version_id": current["version_id"]}
        first, second = topic.concurrent(
            monkeypatch,
            lambda: site.first.post(path, json=body, headers=CSRF),
            lambda: site.second.post(path, json=body, headers=CSRF),
        )
        assert (first.status_code, second.status_code) == (201, 409)
