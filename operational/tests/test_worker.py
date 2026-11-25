"""Worker contract over real roles, isolated files, processes and transactions."""

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from operational import worker
from operational.delivery_objects import encoded
from operational.retention import capture_access, recover
from operational.tests.conftest import freeze_objects, generation, new_db, prepare_copy
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, dataset, key, login
from operational.tests.test_deliveries import call, command, csv, json_input
from reviewscope.access.http import create_app
from reviewscope.access.ledger import read_witness
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

__all__ = ["key"]


def request_for(snapshot):
    return dict(
        snapshot_id=str(snapshot),
        idempotency_key=str(uuid4()),
        job_kind=worker.KIND,
        engine_version="1",
    )


@pytest.fixture
def jobs(db, tmp_path, key):
    base, root, w, s = command(db, tmp_path)
    a, b = [call(base + csv(name) + ["--apply"])["snapshot_id"] for name in ("a", "b")]
    password = secrets.token_urlsafe(24)
    provision(db, "runner", password, key, workspace=w, role="analyst")
    app = create_app()
    with TestClient(app, base_url=ORIGIN) as client:
        assert login(client, "runner", password).status_code == 204
        yield client, ROOT + f"/workspaces/{w}/runs", w, a, b, root, password


def enqueue(client, path, snap, *, kind=worker.KIND):
    body = request_for(snap) | {"job_kind": kind}
    reply = client.post(path, json=body, headers=CSRF)
    assert reply.status_code == 202, reply.content
    return reply.json()["id"], body


def cli(label):
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "operational.worker", "--once"], capture_output=True, timeout=90
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    profiles = [e for e in events if e.get("event") == "candidate_resources"]
    assert all(
        set(p)
        == {
            "event",
            "segmentation_s",
            "corpus_tokenizer_s",
            "query_encoding_s",
            "exact_search_and_channel_rank_s",
            "fusion_and_selection_s",
            "lexical_fit_s",
            "corpus_dense_s",
            "matrix_bytes",
            "model_load_s",
            "query_s",
            "peak_rss_kib",
        }
        | ({"unique_encoded", "input_tokens"} if "unique_encoded" in p else set())
        and all(isinstance(v, (int, float)) for k, v in p.items() if k != "event")
        for p in profiles
    )
    decisions = [e for e in events if e.get("event") == "decision_resources"]
    assert all(
        set(e) == {"event", "rows", "unique_scored", "load_s", "score_wall_s", "cpu_s",
                   "peak_rss_kib", "stage_s", "staged_bytes"}
        and all(isinstance(v, (int, float)) for k, v in e.items() if k != "event")
        for e in decisions
    )
    events = [e for e in events if e not in profiles and e not in decisions]
    assert result.stderr == b"" and events
    assert all(
        set(e)
        == {
            "event",
            "stage",
            "processed",
            "attempt",
            "outcome",
            "duration_ms",
            "peak_rss_kib",
            "db_backend_peak_rss_kib",
        }
        for e in events
    )
    print(
        json.dumps({"probe": label, "wall_s": round(time.monotonic() - start, 3), "events": events})
    )
    return events[-1]["outcome"]


def take(conn):
    return conn.execute("SELECT * FROM claim_job()").fetchone()


def publish_cas(conn, job, sha, size, manifest):
    return conn.execute(
        "SELECT worker_step(%s,%s,%s,'publish',0,NULL,NULL,%s,%s,%s)",
        (*job[:3], sha, size, manifest),
    ).fetchone()[0]


def test_idempotency_access_and_limits(db, jobs, key):
    client, path, w, a, b, _, _ = jobs
    foreign, other = dataset(db, "synthetic")
    body = request_for(a)
    gate = Barrier(2)

    def post():
        gate.wait(timeout=5)
        return client.post(path, json=body, headers=CSRF)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(post)
        responses = [post(), first.result()]
    assert sorted(x.status_code for x in responses) == [200, 202]
    (run,) = {x.json()["id"] for x in responses}
    for change in ({"engine_version": "2"}, {"snapshot_id": b}, {"job_kind": "other"}):
        assert client.post(path, json=body | change, headers=CSRF).status_code == 409
    assert client.post(path, json=request_for(other["snapshots"]), headers=CSRF).status_code == 404
    assert (
        client.post(path, json=request_for(a) | {"engine_version": "2"}, headers=CSRF).status_code
        == 422
    )
    assert client.post(path, json=body).status_code == 403
    foreign_path = ROOT + f"/workspaces/{foreign}/runs"
    assert client.post(foreign_path, json=body, headers=CSRF).status_code == 404
    assert client.get(foreign_path + f"/{run}/status").status_code == 404
    assert client.post(foreign_path + f"/{run}/cancel", headers=CSRF).status_code == 404
    assert client.get(path + "/bad-id/status").status_code == 404
    assert client.post(path, json=body | {"snapshot_id": "bad"}, headers=CSRF).status_code == 404
    actor = db.execute("SELECT id FROM principal WHERE login='runner'").fetchone()[0]
    db.execute("INSERT INTO workspace_membership VALUES(%s,%s,'owner','active')", (foreign, actor))
    other_run = client.post(
        foreign_path, json=body | {"snapshot_id": str(other["snapshots"])}, headers=CSRF
    )
    assert other_run.status_code == 202 and other_run.json()["id"] != run
    assert client.get(foreign_path + f"/{run}/status").status_code == 404
    db.execute(
        "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
        (w, actor),
    )
    assert client.get(path + f"/{run}/status").status_code == 200
    assert client.post(path, json=body, headers=CSRF).status_code == 403
    assert client.post(path + f"/{run}/cancel", headers=CSRF).status_code == 403
    with psycopg.connect(user="rs_runtime", autocommit=True) as runtime:
        for statement in (
            "SELECT text_content FROM review_version",
            "SELECT delivery_object_root FROM workspace",
            "SELECT * FROM job_input(NULL,NULL,NULL)",
            "SELECT * FROM claim_job()",
            "INSERT INTO analysis_run DEFAULT VALUES",
            "UPDATE artifact SET size_bytes=1",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(statement)
        with runtime.transaction():
            runtime.execute(
                "SELECT set_config('rs.session_proof',%s,true),set_config('rs.workspace',%s,true)",
                (session_proof(client.cookies[COOKIE], key), str(w)),
            )
            assert runtime.execute("SELECT cancel_job(%s,%s)", (w, run)).fetchone() == (403,)
    with worker.connect() as restricted:
        for query in (
            "SELECT text_content FROM review_version",
            "UPDATE analysis_run SET status='failed'",
            "SELECT * FROM enqueue_job(NULL,NULL,NULL,NULL,NULL)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                restricted.execute(query)
        assert restricted.execute("SELECT job_input(%s,%s,%s)", (w, run, uuid4())).fetchone() == (
            None,
        )
        assert worker.process(restricted, take(restricted), Event()) == "failed"
    assert client.get(path + f"/{run}/status").json()["reason"] == "ACCESS_REVOKED"
    db.execute(
        "UPDATE workspace_membership SET role='analyst' WHERE workspace_id=%s AND principal_id=%s",
        (w, actor),
    )
    active = [enqueue(client, path, a)[0] for _ in range(4)]
    assert client.post(path, json=request_for(a), headers=CSRF).status_code == 429
    for rid in active:
        assert client.post(path + f"/{rid}/cancel", headers=CSRF).json()["status"] == "cancelled"
    for _ in range(15):
        rid, _ = enqueue(client, path, a)
        assert client.post(path + f"/{rid}/cancel", headers=CSRF).status_code == 200
    assert client.post(path, json=request_for(a), headers=CSRF).status_code == 429
    assert client.post(path, json=body, headers=CSRF).status_code == 200


def test_summary_disconnect_replay_parallel_restore(db, jobs, key, tmp_path, monkeypatch):
    client, path, w, a, b, root, password = jobs
    run, body = enqueue(client, path, a)
    assert client.post(ROOT + "/session/logout", headers=CSRF).status_code == 204
    assert cli("cold-A") == "succeeded"
    assert login(client, "runner", password).status_code == 204
    status = client.get(path + f"/{run}/status").json()
    assert (
        status["attempt"] == 1
        and status["processed"] == status["total"] == 3
        and status["reason"] is None
    )
    assert client.post(path, json=body, headers=CSRF).json() == status
    assert client.post(path + f"/{run}/cancel", headers=CSRF).status_code == 409
    location = root / str(w) / "sha256"
    raw = (location / status["sha256"]).read_bytes()
    value = json.loads(raw)
    assert raw == encoded(value) + b"\n" and hashlib.sha256(raw).hexdigest() == status["sha256"]
    assert value["coverage"] == "unknown" and value["eligibility"][
        "unknown_author"
    ] == worker.measure(3, 3)
    assert value["eligibility"]["comparable_date"] == worker.measure(3, 3)
    assert (
        value["observations"] == worker.measure(3, 3)
        and "AUTHOR_METRICS_UNSUPPORTED" in value["limitations"]
    )
    assert sum(x["numerator"] for x in value["rating_distribution"].values()) == 3
    print(
        json.dumps(
            {"probe": "authenticated-A", "request": body, "status": status, "result": value},
            sort_keys=True,
        )
    )
    later, _ = enqueue(client, path, a)
    assert client.post(path + f"/{later}/cancel", headers=CSRF).json()["reason"] == "CANCELLED"
    second, _ = enqueue(client, path, b)
    third, _ = enqueue(client, path, b)
    start = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(cli, ["parallel-B1", "parallel-B2"])) == ["succeeded"] * 2
    print("parallel pair seconds", round(time.monotonic() - start, 3))
    for rid in (second, third):
        row = client.get(path + f"/{rid}/status").json()
        assert row["total"] == 6 and row["processed"] == 6
    latest = path.removesuffix("/runs") + f"/snapshots/{a}/latest-summary"
    assert client.get(latest).json() == status
    bad, _ = enqueue(client, path, a)
    source = location / value["source_sha256"]["file"]
    saved = source.read_bytes()
    source.chmod(0o600)
    source.write_bytes(b"synthetic damage")
    assert cli("invalid-source") == "failed"
    assert client.get(path + f"/{bad}/status").json()["reason"] == "INTEGRITY_ERROR"
    assert client.get(latest).json() == status
    metrics = latest.replace("latest-summary", "summary-metrics")
    assert client.get(metrics, params={"run_id": bad}).json()["metrics"] is None
    assert client.get(metrics).json()["id"] == run
    assert client.get(metrics, params={"run_id": second}).status_code == 404
    assert client.get(metrics, params={"source_id": uuid4()}).status_code == 404
    selected = client.get(metrics, params={"run_id": run, "source_id": value["source_id"]}).json()
    assert selected["context"] == {
        k: value[k] for k in ("source_id", "batch_id", "coverage", "covered_from", "covered_until",
                              "delivery_exclusions", "metric_exclusions")
    } | {"selection": "accepted_in_delivery"}
    assert selected["metrics"]["rating_distribution"] == value["rating_distribution"]
    source.write_bytes(saved)
    source.chmod(0o400)
    assert db.execute("SELECT count(*) FROM artifact").fetchone() == (3,)
    dump = tmp_path / "jobs.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", dump], check=True)
    restored_root = generation("copy")
    frozen = freeze_objects(root, tmp_path, [w])
    name = new_db()
    subprocess.run(
        ["pg_restore", "--no-owner", "--single-transaction", "--exit-on-error", "-d", name, dump],
        check=True,
    )
    monkeypatch.setenv("PGDATABASE", name)
    with psycopg.connect("", autocommit=True) as restored:
        restored.execute(
            sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        )
        proof, access = read_witness()["sha256"], capture_access(db)
        token = prepare_copy(restored, frozen, restored_root, proof, access)
        recover(restored, proof, [token], access)
        restored.execute(
            "UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(restored_root), w)
        )
        restored.execute(
            "UPDATE access_session SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL"
        )
        for q in (
            "SELECT * FROM analysis_run ORDER BY id",
            "SELECT * FROM artifact ORDER BY id",
            "SELECT * FROM snapshot ORDER BY id",
            "SELECT * FROM workspace_membership ORDER BY 1,2",
        ):
            assert restored.execute(q).fetchall() == db.execute(q).fetchall()
        for obj, digest in restored.execute(
            "SELECT duckdb_key,duckdb_sha256 FROM snapshot UNION SELECT object_key,checksum FROM artifact"
        ):
            assert hashlib.sha256((restored_root / obj).read_bytes()).hexdigest() == digest
    with TestClient(create_app(), base_url=ORIGIN) as fresh:
        assert (
            fresh.get(
                ROOT + "/session", headers={"Cookie": COOKIE + "=" + client.cookies[COOKIE]}
            ).status_code
            == 401
        )
        assert login(fresh, "runner", password).status_code == 204
        assert fresh.post(path, json=body, headers=CSRF).json() == status
        fresh_id, _ = enqueue(fresh, path, a)
        assert cli("restored") == "succeeded"
        replay = fresh.get(path + f"/{fresh_id}/status").json()
        assert json.loads(
            (restored_root / str(w) / "sha256" / replay["sha256"]).read_bytes()
        ) == value | {"run_id": fresh_id}


def admit_signal_fixture(db, w, snapshot, kind):
    if kind == worker.signals.KIND:
        db.execute(
            """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed)
            SELECT b.workspace_id,b.source_id,b.file_sha256,true FROM import_batch b JOIN snapshot s ON
            (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id)
            WHERE (s.workspace_id,s.id)=(%s,%s)""",
            (w, snapshot),
        )


@pytest.mark.parametrize(
    ("kind", "bound"),
    [
        (worker.KIND, False),
        (worker.signals.KIND, False),
        (worker.candidate_worker.KIND, False),
        (worker.candidate_worker.KIND, True),
        (worker.rerank_worker.KIND, True),
    ],
)
def test_claim_crash_orphan_stale_and_recovery(db, jobs, kind, bound, tmp_path, monkeypatch):
    client, path, w, a, _, root, _ = jobs
    admit_signal_fixture(db, w, a, kind)
    if kind in (worker.candidate_worker.KIND, worker.rerank_worker.KIND):
        (source,) = db.execute(
            """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed)
            SELECT s.workspace_id,s.source_id,b.file_sha256,true,true FROM snapshot s JOIN import_batch b ON
            (s.workspace_id,s.source_id,s.batch_id)=(b.workspace_id,b.source_id,b.id)
            WHERE s.workspace_id=%s AND s.id=%s RETURNING source_id""",
            (w, a),
        ).fetchone()
        binding = {}
        if bound:
            from operational.tests.test_aspects import B, create

            project = db.execute("SELECT project_id FROM source WHERE id=%s", (source,)).fetchone()[0]
            binding["aspect"] = create(client, path.removesuffix("/runs") + f"/projects/{project}/aspects", B)
        reply = client.post(
            path.removesuffix("/runs") + f"/snapshots/{a}/candidates",
            json=dict(
                source_id=str(source),
                idempotency_key=str(uuid4()),
                mode="lexical",
                role="query_to_passage",
                k=2,
                query="табло",
                **binding,
            ),
            headers=CSRF,
        )
        assert reply.status_code == 202
        run = reply.json()["id"]
        if kind == worker.rerank_worker.KIND:
            from operational.tests.test_browser_delivery import importer

            monkeypatch.delenv("RS_RERANK_MODELS", raising=False)
            assert cli("crash-test-parent") == "succeeded"
            route = path.removesuffix("/runs") + f"/snapshots/{a}/candidates/{run}/rerank"
            with importer(tmp_path, monkeypatch):
                reply = client.post(
                    route,
                    json=dict(
                        source_id=str(source),
                        idempotency_key=str(uuid4()),
                        profile="mmarco-context-logit-v1",
                    ),
                    headers=CSRF,
                )
                assert reply.status_code == 202, reply.text
                run=reply.json()["id"]
    else:
        run, _ = enqueue(client, path, a, kind=kind)
    # Capture a real live generation without placing its nonce in logs.
    with worker.connect() as one, worker.connect() as two, ThreadPoolExecutor(2) as pool:
        barrier = Barrier(2)

        def race(c):
            barrier.wait(timeout=5)
            return take(c)

        pending = pool.submit(race, one)
        claims = [race(two), pending.result()]
        assert claims.count(None) == 1
        old = next(x for x in claims if x)
        info = one.execute("SELECT job_input(%s,%s,%s)", old[:3]).fetchone()[0]
        before = set((root / str(w) / "sha256").iterdir())
        # Stop after the fsynced link is visible and before any artifact INSERT.
        probe = """import json,os,signal,sys
from threading import Event
from operational import worker
job=json.loads(sys.stdin.readline())
original=worker.publish
def stop(*args):
    digest=original(*args)
    os.kill(os.getpid(),signal.SIGSTOP)
    return digest
worker.publish=stop
with worker.connect() as conn: worker.process(conn,job,Event())
"""
        p = subprocess.Popen(
            [sys.executable, "-c", probe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            p.stdin.write(json.dumps(old, default=str) + "\n")
            p.stdin.flush()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pid, state = os.waitpid(p.pid, os.WUNTRACED | os.WNOHANG)
                if pid:
                    assert os.WIFSTOPPED(state) and os.WSTOPSIG(state) == signal.SIGSTOP
                    break
                time.sleep(0.02)
            else:
                pytest.fail("worker did not reach the file publication failpoint")
            p.kill()
            _, err = p.communicate(timeout=5)
            assert p.returncode == -signal.SIGKILL and not err
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        (orphan,) = set((root / str(w) / "sha256").iterdir()) - before
        assert db.execute("SELECT count(*) FROM artifact WHERE run_id=%s",(run,)).fetchone() == (0,)
        assert client.get(path + f"/{run}/status").json()["status"] == "running"
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            assert take(two) is None
            if client.get(path + f"/{run}/status").json()["status"] == "queued":
                break
            time.sleep(0.2)
        else:
            pytest.fail("expired attempt did not return to queue")
        size = orphan.stat().st_size
        assert publish_cas(one, old, orphan.name, size, info["manifest"]) == "stale"
        time.sleep(1.05)
        assert cli("restart-after-SIGKILL") == "succeeded"
        assert publish_cas(one, old, orphan.name, size, info["manifest"]) == "stale"
        assert db.execute("SELECT checksum,size_bytes FROM artifact WHERE run_id=%s",(run,)).fetchall() == [
            (orphan.name, size)
        ]
        final = client.get(path + f"/{run}/status").json()
        assert final["attempt"] == 2 and final["sha256"] == orphan.name


@pytest.mark.parametrize("kind", [worker.KIND, worker.signals.KIND])
def test_cancellation_publication_race_and_failure(db, jobs, monkeypatch, kind):
    client, path, w, a, _, root, _ = jobs
    admit_signal_fixture(db, w, a, kind)
    rid, _ = enqueue(client, path, a, kind=kind)
    with worker.connect() as conn:
        job = take(conn)
        publish = worker.publish

        def cancel_at_file(*args):
            sha = publish(*args)
            r = client.post(path + f"/{rid}/cancel", headers=CSRF)
            assert r.json()["cancel_requested"]
            return sha

        monkeypatch.setattr(worker, "publish", cancel_at_file)
        assert worker.process(conn, job, Event()) == "cancelled"
        assert client.get(path + f"/{rid}/status").json()["artifact_id"] is None
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        monkeypatch.setattr(worker, "publish", publish)
        rid, _ = enqueue(client, path, a, kind=kind)
        job = take(conn)

        def damage(directory, chunks):
            sha = publish(directory, chunks)
            obj = directory / sha
            obj.chmod(0o600)
            obj.write_bytes(b"synthetic")
            return sha

        monkeypatch.setattr(worker, "publish", damage)
        assert worker.process(conn, job, Event()) == "failed"
        assert client.get(path + f"/{rid}/status").json()["reason"] == "INTEGRITY_ERROR"
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        monkeypatch.setattr(worker, "publish", publish)
        rid, _ = enqueue(client, path, a, kind=kind)
        # A DB failure after artifact INSERT rolls both statements back.
        db.execute("""CREATE FUNCTION refuse_finish() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic interruption'; END $$;
            CREATE TRIGGER refuse BEFORE UPDATE ON analysis_run FOR EACH ROW WHEN (NEW.status='succeeded')
            EXECUTE FUNCTION refuse_finish()""")
        assert worker.process(conn, take(conn), Event()) == "failed"
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert client.get(path + f"/{rid}/status").json()["reason"] == "INTERNAL_ERROR"


def test_finite_retries_and_missing_inputs(db, jobs):
    client, path, w, a, _, root, _ = jobs
    rid, body = enqueue(client, path, a)
    sha = db.execute("SELECT duckdb_sha256 FROM snapshot WHERE id=%s", (a,)).fetchone()[0]
    source = root / str(w) / "sha256" / sha
    source.rename(source.with_name("offline-synthetic"))
    for n, outcome in enumerate(("queued", "queued", "failed"), 1):
        assert cli("retry-" + str(n)) == outcome
        state = client.get(path + f"/{rid}/status").json()
        assert state["attempt"] == n
        if n < 3:
            time.sleep(n + 0.05)
    assert state["reason"] == "STORAGE_UNAVAILABLE" and state["artifact_id"] is None
    assert client.post(path, json=body, headers=CSRF).json() == state
    with worker.connect() as conn:
        assert take(conn) is None
    assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)


def test_json_exclusions(db, tmp_path, key):
    base, root, w, _ = command(db, tmp_path)
    rows = [
        {"id": "conflict", "body": "first"},
        {"id": "conflict", "body": "second"},
        {"id": "again"},
        {"id": "again"},
        {"body": "synthetic"},
        False,
    ]
    snap = call(
        base + json_input(tmp_path, rows, {"id": "source_review_id", "body": "text"}) + ["--apply"]
    )
    password = secrets.token_urlsafe(24)
    provision(db, "owner", password, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert login(client, "owner", password).status_code == 204
        path = ROOT + f"/workspaces/{w}/runs"
        rid, _ = enqueue(client, path, snap["snapshot_id"])
        assert cli("json-exclusions") == "succeeded"
        value = json.loads(
            (
                root / str(w) / "sha256" / client.get(path + f"/{rid}/status").json()["sha256"]
            ).read_bytes()
        )
        assert value["observations"] == worker.measure(2, 6)
        assert value["delivery_exclusions"] == {
            k: worker.measure(n, 6)
            for k, n in (
                ("conflicting_id", 2),
                ("confirmed_duplicate", 1),
                ("invalid_json_item", 1),
            )
        }
        assert value["rating_distribution"] is None and value["eligibility"][
            "known_rating"
        ] == worker.measure(0, 2)
        assert value["eligibility"]["comparable_date"] == worker.measure(0, 2)
        assert value["eligibility"]["unknown_author"] == worker.measure(2, 2)


def test_slot_limit_and_cancel_publish_linearization(db, jobs, monkeypatch):
    client, path, _, a, _, _, _ = jobs
    ids = [enqueue(client, path, a)[0] for _ in range(3)]
    with worker.connect() as first, worker.connect() as second:
        j, k = take(first), take(second)
        assert str(j[1]) == ids[0] and str(k[1]) == ids[1] and take(first) is None
        stop = Event()
        stop.set()
        assert worker.process(second, k, stop) == "queued"
        assert str(take(second)[1]) == ids[2]
        fence = Barrier(2)
        original = worker.verify_file

        def before_commit(*args):
            original(*args)
            fence.wait(timeout=5)

        monkeypatch.setattr(worker, "verify_file", before_commit)
        with ThreadPoolExecutor(2) as pool:
            attempt = pool.submit(worker.process, first, j, Event())
            fence.wait(timeout=5)
            cancelled = client.post(path + f"/{ids[0]}/cancel", headers=CSRF)
            winner = attempt.result()
        final = client.get(path + f"/{ids[0]}/status").json()
        assert final["status"] == winner
        if cancelled.status_code == 200:
            assert winner == "cancelled" and final["artifact_id"] is None
        else:
            assert cancelled.status_code == 409 and winner == "succeeded" and final["artifact_id"]
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE run_id=%s", (ids[0],)
        ).fetchone() == (int(winner == "succeeded"),)


@pytest.mark.parametrize("part", ["header", "version", "payload", "manifest"])
def test_divergent_projection_even_with_matching_file_sha(db, jobs, part):
    client, path, w, a, _, root, _ = jobs
    (key,) = db.execute("SELECT duckdb_key FROM snapshot WHERE id=%s", (a,)).fetchone()
    directory = (root / key).parent
    lines = (root / key).read_bytes().splitlines()
    if part == "manifest":
        db.execute("ALTER TABLE snapshot_member DISABLE TRIGGER immutable_rows")
        db.execute(
            "DELETE FROM snapshot_member WHERE (snapshot_id,version_id)=(SELECT snapshot_id,version_id FROM snapshot_member WHERE snapshot_id=%s LIMIT 1)",
            (a,),
        )
        db.execute("ALTER TABLE snapshot_member ENABLE TRIGGER immutable_rows")
    else:
        index = 0 if part == "header" else 1
        replacement = {
            "header": {"format": "unexpected"},
            "version": {"version_id": str(uuid4())},
            "payload": {"text": "synthetic altered"},
        }[part]
        lines[index] = encoded(json.loads(lines[index]) | replacement)
        sha = worker.publish(directory, (line + b"\n" for line in lines))
        # Model a defective producer that signed different bytes under a valid manifest.
        db.execute("ALTER TABLE snapshot DISABLE TRIGGER immutable_rows")
        db.execute(
            """UPDATE snapshot SET duckdb_sha256=%s,manifest_sha256=
                  snapshot_digest(workspace_id,source_id,batch_id,%s,filters,version_ids) WHERE id=%s""",
            (sha, sha, a),
        )
        db.execute("ALTER TABLE snapshot ENABLE TRIGGER immutable_rows")
    rid, _ = enqueue(client, path, a)
    assert cli("projection-" + part) == "failed"
    final = client.get(path + f"/{rid}/status").json()
    assert (
        final["reason"] == "INTEGRITY_ERROR"
        and final["attempt"] == 1
        and final["artifact_id"] is None
    )


def test_coverage_unknowns_and_terminal_codes(db, jobs, tmp_path):
    from operational.tests.test_deliveries import COVER

    client, path, w, a, _, root, _ = jobs
    source = db.execute("SELECT source_id FROM snapshot WHERE id=%s", (a,)).fetchone()[0]
    base = [
        "--workspace",
        str(w),
        "--source",
        str(source),
        "--object-root",
        str(root),
        "--coverage-passport",
        str(tmp_path / "coverage"),
    ]
    for level in (None, "partial", "full"):
        cover = COVER | {
            "coverage": level,
            "mode": "cumulative",
            "basis": "synthetic declaration",
            "filters": {},
        }
        (tmp_path / "coverage").write_text(json.dumps(cover))
        args = json_input(
            tmp_path,
            [{"date": "2026-09-01", "score": 2}, {"date": "bad", "score": None}],
            {"date": "published_at", "score": "rating"},
            {
                "source": "synthetic",
                "source_place_id": "p1",
                "event_basis": "edited",
                "rating_scale": [1, 5],
            },
        )
        snap = call(base + args + ["--apply"])["snapshot_id"]
        rid, _ = enqueue(client, path, snap)
        assert cli("coverage") == "succeeded"
        status = client.get(path + f"/{rid}/status").json()
        value = json.loads((root / str(w) / "sha256" / status["sha256"]).read_bytes())
        assert value["coverage"] == level and value["event_basis"] == "edited"
        assert value["eligibility"]["known_rating"] == worker.measure(1, 2)
        assert value["eligibility"]["comparable_date"] == worker.measure(0, 2)
        assert value["metric_exclusions"]["date_invalid"] == worker.measure(1, 2)
        assert value["metric_exclusions"]["ineligible_event_basis"] == worker.measure(1, 2)
    other, legacy = dataset(db, "synthetic")
    db.execute(
        "INSERT INTO workspace_membership SELECT %s,id,'analyst','active' FROM principal WHERE login='runner'",
        (other,),
    )
    other_path = ROOT + f"/workspaces/{other}/runs"
    rid, _ = enqueue(client, other_path, legacy["snapshots"])
    assert cli("legacy-format") == "failed"
    assert client.get(other_path + f"/{rid}/status").json()["reason"] == "UNSUPPORTED_FORMAT"
    rid, _ = enqueue(client, path, a)
    file_sha = db.execute(
        "SELECT file_sha256 FROM import_batch WHERE id=(SELECT batch_id FROM snapshot WHERE id=%s)",
        (a,),
    ).fetchone()[0]
    source_file = root / str(w) / "sha256" / file_sha
    source_file.chmod(0o600)
    with source_file.open("ab") as stream:
        stream.truncate(4 * 1024**2 + 1)
    assert cli("input-limit") == "failed"
    assert client.get(path + f"/{rid}/status").json()["reason"] == "RESOURCE_LIMIT"


def test_sigterm_releases_attempt(jobs):
    client, path, _, a, _, _, _ = jobs
    rid, _ = enqueue(client, path, a)
    probe = """import os,signal,sys
from operational import worker
original=worker.read_source
def terminate(*args,**kwargs):
    os.kill(os.getpid(),signal.SIGTERM)
    return original(*args,**kwargs)
worker.read_source=terminate
sys.exit(worker.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, "--once"], capture_output=True, timeout=10
    )
    assert result.returncode == 0 and not result.stderr
    status = client.get(path + f"/{rid}/status").json()
    assert status["status"] == "queued" and status["artifact_id"] is None
    time.sleep(1.05)
    assert cli("restart-after-SIGTERM") == "succeeded"
    assert client.get(path + f"/{rid}/status").json()["attempt"] == 2


def test_empty_overview_projection(db, tmp_path, key):
    base, _, w, s = command(db, tmp_path)
    # The intake has one position, all quarantined. The immutable selection is empty.
    snap = call(base + json_input(tmp_path, [False], {"id": "source_review_id"},
                                  {"source": "synthetic", "source_place_id": "p1"}) + ["--apply"])
    secret = secrets.token_urlsafe(24)
    actor = provision(db, "empty-overview", secret, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "empty-overview", secret).status_code == 204
        path = f"{ROOT}/workspaces/{w}"
        run, _ = enqueue(web, path + "/runs", snap["snapshot_id"])
        assert cli("empty-overview") == "succeeded"
        url = path + "/snapshots/" + snap["snapshot_id"] + "/summary-metrics"
        foreign, _ = dataset(db, "other-synthetic")
        assert web.get(url.replace(str(w), str(foreign))).status_code == 404
        projection = web.get(url, params={"run_id": run, "source_id": s}).json()
        assert projection["metrics"]["counts"]["observations"] == worker.measure(0, 1)
        assert projection["metrics"]["eligibility"]["usable_text"] == worker.measure(0, 0)
        assert projection["context"]["delivery_exclusions"]["invalid_json_item"] == worker.measure(1, 1)
        db.execute("UPDATE workspace_membership SET state='inactive' WHERE principal_id=%s", (actor,))
        assert web.get(url).status_code == 404
