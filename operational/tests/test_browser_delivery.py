"""Actual restricted database, local intake socket and web sessions on synthetic data."""

import hashlib
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_deliveries import FIX, command
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


@contextmanager
def importer(tmp_path, monkeypatch):
    path = tmp_path / "broker"
    monkeypatch.setenv("RS_UPLOAD_SOCKET", str(path))
    env = os.environ.copy()
    env.pop("RS_ACCESS_KEY_FILE", None)
    env["PGPASSFILE"] = os.path.join(env["PGHOST"], "rs_upload.pass")
    proc = subprocess.Popen([sys.executable, "-m", "operational.browser_import"], env=env)
    try:
        for _ in range(200):
            if path.exists():
                break
            assert proc.poll() is None
            time.sleep(0.025)
        assert path.exists()
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_browser_delivery(db, tmp_path, monkeypatch, key):  # noqa: F811
    _, root, w, s = command(db, tmp_path)
    loc = [
        str(r[0])
        for r in db.execute(
            "SELECT location_id FROM source_location WHERE source_id=%s ORDER BY location_id", (s,)
        )
    ]
    pw = secrets.token_urlsafe(20)
    owner = provision(db, "web-owner", pw, key, workspace=w)
    d = uuid4()
    prefix = f"{ROOT}/workspaces/{w}"
    path = f"{prefix}/sources/{s}/uploads/{d}"
    parts = [(FIX / f).read_bytes() for f in ("mapping.json", "a.passport.json", "a.csv")]
    digest = hashlib.sha256(parts[-1]).hexdigest()
    headers = CSRF | {
        "content-type": "application/octet-stream",
        "x-rs-file-sha256": digest,
        "x-rs-parts": ",".join(str(len(p)) for p in parts),
        "x-rs-locations": ",".join(loc),
        "x-rs-format": "csv",
    }

    def post(client, action, h=headers):
        return client.post(path + "/" + action, headers=h, content=b"".join(parts))

    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "web-owner", pw).status_code == 204
        assert post(web, "preview").status_code == 404
        db.execute("INSERT INTO browser_admission VALUES (%s,%s,%s)", (w, s, digest))
        response = web.get(prefix + "/upload-targets")
        assert response.status_code == 200, response.text
        assert len(response.json()) == 2
        result = post(web, "preview")
        assert result.status_code == 200, result.text
        assert result.json()["diagnostics"]["summary"]["accepted"] == 3
        assert not list(root.iterdir())
        fp = result.json()["fingerprint"]
        approved = headers | {"x-rs-fingerprint": fp}
        assert post(web, "confirm", headers | {"x-rs-fingerprint": "0" * 64}).status_code == 409
        with psycopg.connect(user="rs_upload") as denied:
            assert denied.execute("SELECT count(*) FROM review_version").fetchone() == (0,)
            assert denied.execute("SELECT browser_actor(%s,%s)", (w, s)).fetchone() == (None,)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda _: post(web, "confirm", approved), range(2)))
        assert [r.status_code for r in answers] == [200, 200], [r.text for r in answers]
        snap = answers[0].json()["snapshot_id"]
        assert snap and answers[1].json()["snapshot_id"] == snap
        assert db.execute("SELECT count(*) FROM snapshot").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM review_version").fetchone() == (3,)
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, owner),
        )
        assert post(web, "preview").status_code == 403
        assert web.get(path).status_code == 403

        db.execute(
            "UPDATE workspace_membership SET role='owner' WHERE workspace_id=%s AND principal_id=%s",
            (w, owner),
        )
        verify_browser_lifecycle(db, web, tmp_path, monkeypatch, root, w, s, path)


def verify_browser_lifecycle(db, web, tmp_path, monkeypatch, root, w, s, draft_url):
    from zipfile import ZipFile

    from psycopg import sql

    from operational.retention import capture_access, recover
    from operational.tests.conftest import freeze_objects, generation, prepare_copy
    from operational.tests.test_retention import ask, fresh_restore_cluster, operator
    from operational.tests.test_worker import cli, enqueue
    from reviewscope.access.ledger import read_witness

    snapshot = web.get(draft_url).json()["snapshot_id"]
    path = f"{ROOT}/workspaces/{w}"
    enqueue(web, path + "/runs", snapshot)
    assert cli("browser-summary") == "succeeded"
    metrics_url = path + f"/snapshots/{snapshot}/summary-metrics"
    assert web.get(metrics_url).json()["metrics"]["counts"]["observations"] == {
        "numerator": 3,
        "denominator": 3,
    }
    from operational.signals import KIND

    db.execute("UPDATE browser_admission SET evidence_allowed=true WHERE workspace_id=%s", (w,))
    run, _ = enqueue(web, path + "/runs", snapshot, kind=KIND)
    assert cli("browser-signal-export") == "succeeded"
    signal_url = path + f"/snapshots/{snapshot}/signals?source_id={s}&run_id={run}"
    result = web.get(signal_url)
    assert result.status_code == 200
    digest=result.json()["artifact_sha256"]
    db.execute("UPDATE browser_admission SET candidate_allowed=true WHERE workspace_id=%s", (w,))
    candidate_path = path + f"/snapshots/{snapshot}/candidates"
    phrase = "Тестовый поиск готового заказа"
    new = web.post(
        candidate_path,
        json=dict(
            source_id=str(s),
            query=phrase,
            mode="lexical",
            role="query_to_passage",
            k=2,
            idempotency_key=str(uuid4()),
        ),
        headers=CSRF,
    )
    assert new.status_code == 202 and cli("candidate-export") == "succeeded"
    params = dict(source_id=str(s), run_id=new.json()["id"])
    candidate_sha=web.get(candidate_path,params=params).json()["artifact_sha256"]
    req = ask(web, w, "source", s, "export")
    handoff = tmp_path / "handoff"
    handoff.mkdir(mode=0o700)
    operator("export", "--request", req["id"], "--root", root, "--owner-handoff", handoff)
    with ZipFile(handoff / (req["id"] + ".zip")) as zipped:
        assert hashlib.sha256(zipped.read("objects/"+digest)).hexdigest()==digest
        assert hashlib.sha256(zipped.read("objects/" + candidate_sha)).hexdigest() == candidate_sha
        assert phrase in zipped.read("rows/analysis_run.jsonl").decode()
        row = zipped.read("rows/browser_delivery.jsonl")
        assert b"diagnostics" in row and b"approval" not in row and b"session" not in row
    db.execute("UPDATE browser_admission SET evidence_allowed=true WHERE workspace_id=%s", (w,))
    evidence_url = path + f"/snapshots/{snapshot}/reviews"
    evidence = web.get(evidence_url).json()
    assert evidence["total"] == 3
    version = evidence["rows"][0]["version_id"]
    context_url = f"{evidence_url}/{version}/reviewer-context?source_id={s}"
    assert web.get(context_url).json()["status"] == "NO_REVIEWER_ID"
    archive = tmp_path / "old.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", archive], check=True)
    frozen = freeze_objects(root, tmp_path, [w])
    erase = ask(web, w, "source", s, "erase")
    assert (
        erase["report"]["tables"]["browser_delivery"]
        == erase["report"]["tables"]["browser_admission"]
        == 1
    )
    operator(
        "confirm",
        "--request",
        erase["id"],
        "--preview-sha256",
        erase["preview_sha"],
        "--root",
        root,
    )
    assert (
        web.get(draft_url).status_code
        == web.get(metrics_url).status_code
        == web.get(candidate_path,params=params).status_code
        == web.get(signal_url).status_code
        == web.get(evidence_url).status_code
        == web.get(context_url).status_code
        == 404
    )
    assert db.execute("SELECT count(*) FROM browser_summary").fetchone() == (0,)
    assert operator("purge", "--request", erase["id"], "--root", root) == "completed"
    proof, access = read_witness()["sha256"], capture_access(db)
    with (
        fresh_restore_cluster(monkeypatch, archive) as (_, name),
        psycopg.connect("", autocommit=True) as target,
    ):
        target.execute(
            sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        )
        restored = generation("browser")
        token = prepare_copy(target, frozen, restored, proof, access)
        recover(target, proof, [token], access)
        assert target.execute(
            "SELECT (SELECT count(*) FROM browser_delivery),(SELECT count(*) FROM browser_admission)"
        ).fetchone() == (0, 0)
        assert operator("purge", "--request", erase["id"], "--root", restored) == "completed"
        with TestClient(create_app(), base_url=ORIGIN) as recovered_api:
            assert not (restored / str(w) / "sha256" / digest).exists()
            assert target.execute("SELECT count(*) FROM artifact WHERE checksum=%s",(digest,)).fetchone()==(0,)
            assert not (restored / str(w) / "sha256" / candidate_sha).exists()
            assert not target.execute("SELECT FROM analysis_run WHERE candidate_params IS NOT NULL").fetchall()
            recovered_api.cookies.update(web.cookies)
            assert recovered_api.get(evidence_url).status_code == recovered_api.get(context_url).status_code == 401


def test_cabinet_native(db, tmp_path, monkeypatch, key):  # noqa: F811
    """Drive the built cabinet against the TLS launcher, live broker and bounded worker."""
    import json
    from pathlib import Path

    from operational.tests.test_deliveries import call, json_input
    from operational.tests.test_reviewer_context import reviewer_fixture
    from operational.tests.test_schema import scope

    registration, object_root, w, s = command(db, tmp_path)
    other, _, _ = scope(db, source_key="synthetic")
    db.execute(
        "UPDATE location SET name=sl.external_place_id FROM source_location sl WHERE location.id=sl.location_id"
    )
    pw = secrets.token_urlsafe(24)
    actor = provision(db, "cabinet-owner", pw, key, workspace=w)
    empty = db.execute("INSERT INTO workspace(name) VALUES ('Polling control') RETURNING id").fetchone()[0]
    db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'viewer','active')", (empty, actor))
    control = tmp_path / "poll-control"
    witness = Path(os.environ["RS_RETENTION_LEDGER"])
    parked = tmp_path / "parked-test-witness"
    control.write_text("ready")
    j = tmp_path / "json-fixture"
    j.mkdir()
    positions = [
        {"id": "conflict", "body": "Тест"},
        {"id": "conflict", "body": "Тест"},
        {"id": "conflict", "body": "Вариант"},
        {"id": "repeat", "body": None},
        {"id": "repeat", "body": None},
        {"id": None},
        {"id": None},
        {"id": "unique"},
        {"id": "r1", "place": "p1"},
        {"id": "r1", "place": "p2"},
        False,
        {"id": 42},
    ]
    json_input(
        j,
        [({"place": "p1"} | r) if isinstance(r, dict) else r for r in positions],
        {"id": "source_review_id", "place": "source_place_id", "body": "text"},
        {"source": "synthetic"},
    )
    long_fixture = tmp_path / "long-review"
    long_fixture.mkdir()
    history_input = json_input(
        long_fixture,
        [
            {
                "id": "long",
                "body": "<b>Синтетический текст</b> " * 120,
                "url": "https://example.test/review/long",
            }
        ]
        + [{"id": f"example-{n}", "body": "Синтетическая запись"} for n in range(2, 29)],
        {"id": "source_review_id", "body": "text", "url": "source_url"},
    )
    long_snapshot = call(registration + history_input + ["--apply"])["snapshot_id"]
    for file in (FIX / "a.csv", FIX / "b.csv", j / "data", long_fixture / "data"):
        db.execute(
            "INSERT INTO browser_admission VALUES (%s,%s,%s,true)",
            (w, s, hashlib.sha256(file.read_bytes()).hexdigest()),
        )
    db.execute("UPDATE browser_admission SET candidate_allowed=true WHERE workspace_id=%s AND source_id=%s AND checksum=%s",(w,s,hashlib.sha256((FIX/"a.csv").read_bytes()).hexdigest()))
    db.execute(
        "UPDATE browser_admission SET candidate_allowed=true, evidence_link_hashes=ARRAY[%s]::sha256_hex[] WHERE workspace_id=%s AND source_id=%s AND checksum=%s",
        (
            hashlib.sha256(b"https://example.test/review/long").hexdigest(),
            w,
            s,
            hashlib.sha256((long_fixture / "data").read_bytes()).hexdigest(),
        ),
    )
    author_snapshot, author_versions = reviewer_fixture(db, tmp_path / "ctx", registration, w, s)
    cert, tls = tmp_path / "cert.pem", tmp_path / "tls.pem"
    ca, ca_key = tmp_path / "ca.pem", tmp_path / "ca.key"
    csr, extensions = tmp_path / "leaf.csr", tmp_path / "leaf.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n"
    )
    for args in (
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Disposable cabinet CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            ca_key,
            "-out",
            ca,
        ],
        [
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-keyout",
            tls,
            "-out",
            csr,
        ],
        [
            "x509",
            "-req",
            "-in",
            csr,
            "-CA",
            ca,
            "-CAkey",
            ca_key,
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            extensions,
            "-out",
            cert,
        ],
    ):
        subprocess.run(["openssl", *args], check=True, capture_output=True)
    ui = Path("prototypes/reviewscope-ui").resolve()
    assert (ui / "dist/cabinet.html").is_file(), (
        "Build the cabinet before its native acceptance test"
    )
    staging = tmp_path / "http-stage"
    staging.mkdir(mode=0o700)
    monkeypatch.setenv("RS_CABINET_STAGING", str(staging))
    frontend = os.environ | {
        "RS_ACCESS_TLS_CERT": str(cert),
        "RS_ACCESS_TLS_KEY": str(tls),
        "PGPASSFILE": os.path.join(os.environ["PGHOST"], "rs_runtime.pass"),
    }
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(user="rs_upload", passfile=frontend["PGPASSFILE"])
    private_object = object_root / "synthetic-private"
    private_object.write_bytes(b"synthetic")
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys, tempfile
from reviewscope.access.cabinet_sandbox import confine
confine()
for file, mode in [(sys.argv[1], os.O_RDONLY), (os.environ['RS_RETENTION_LEDGER'], os.O_WRONLY),
                   (sys.argv[2], os.O_RDONLY)]:
    try:
        fd = os.open(file, mode)
    except PermissionError:
        continue
    os.close(fd)
    raise SystemExit('cabinet_file_boundary')
with tempfile.TemporaryFile() as stage:
    assert os.fstat(stage.fileno()).st_mode & 0o777 == 0o600
    assert os.fstat(stage.fileno()).st_nlink == 0
    stage.write(b'synthetic')
""",
            str(private_object),
            os.path.join(os.environ["PGHOST"], "harness.pass"),
        ],
        env=frontend,
        check=True,
    )
    with importer(tmp_path, monkeypatch):
        access = subprocess.Popen(
            [sys.executable, "-m", "reviewscope.access"],
            env=frontend | {"RS_UPLOAD_SOCKET": os.environ["RS_UPLOAD_SOCKET"]},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Verify the disposable launcher's certificate before sending fixture credentials.
            tls_context = ssl.create_default_context(cafile=ca)
            deadline = time.monotonic() + 10
            while True:
                if access.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("disposable TLS cabinet did not start")
                try:
                    with socket.create_connection(("127.0.0.1", 8001), timeout=1) as conn:
                        with tls_context.wrap_socket(conn, server_hostname="127.0.0.1"):
                            break
                except ConnectionRefusedError:
                    time.sleep(0.05)
            # Browser subprocess receives synthetic input only. No storage credentials or operator authority.
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in (
                    "PATH",
                    "HOME",
                    "TMPDIR",
                    "DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "PLAYWRIGHT_BROWSERS_PATH",
                    "RS_TEST_BROWSER_OUT",
                )
            }
            browser_home = tmp_path / "browser-home"
            browser_home.mkdir(mode=0o700)
            env["HOME"] = str(browser_home)
            env["NODE_EXTRA_CA_CERTS"] = str(ca)
            env["RS_FIXTURE_CERT"] = str(cert)
            inputs = dict(
                account="cabinet-owner",
                emptyWorkspace=str(empty),
                pollControl=str(control),
                password=pw,
                w=str(w),
                s=str(s),
                csv=str(FIX.resolve()),
                json=str(j),
                longSnapshot=long_snapshot,
                authorSnapshot=author_snapshot,
                authorVersions=author_versions[:2],
                otherWorkspace=str(other),
            )
            # Exercise the offline-unavailable state in both browsers with a fresh cache.
            worker_env = os.environ | {"HF_HUB_CACHE": str(tmp_path / "empty-model-cache")}
            native = subprocess.Popen(
                ["node", "scripts/cabinet-acceptance.mjs"],
                cwd=ui,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            native.stdin.write(json.dumps(inputs) + "\n")
            native.stdin.close()
            try:
                previous = None
                while native.poll() is None:
                    mode = control.read_text()
                    if mode != previous:
                        assert mode in {"pause", "ready", "revoked", "ledger"}
                        if mode == "ledger":
                            witness.rename(parked)
                        elif parked.exists():
                            parked.rename(witness)
                        db.execute(
                            "UPDATE workspace_membership SET state=%s WHERE workspace_id=%s AND principal_id=%s",
                            ("inactive" if mode == "revoked" else "active", w, actor),
                        )
                        control.with_suffix(".ack").write_text(mode)
                        previous = mode
                    if mode != "ready":
                        time.sleep(0.05)
                        continue
                    # Let the browser close its tab and cancel its first queued attempt before the worker starts.
                    if db.execute(
                        """SELECT EXISTS(SELECT FROM analysis_run WHERE status='cancelled')
                               AND EXISTS(SELECT FROM analysis_run WHERE status='queued')"""
                    ).fetchone()[0]:
                        subprocess.run(
                            [sys.executable, "-m", "operational.worker", "--once"],
                            env=worker_env,
                            check=True,
                            capture_output=True,
                        )
                    time.sleep(0.12)
                assert native.returncode == 0, native.stderr.read()
            finally:
                if native.poll() is None:
                    native.kill()
                native.wait(timeout=5)
        finally:
            access.terminate()
            access.wait(timeout=10)
    assert sorted(
        db.execute(
            "SELECT input_count,accepted_count,rejected_count,confirmed_duplicate_count FROM import_batch"
        ).fetchall()
    ) == [(3, 3, 0, 0), (6, 6, 0, 0), (8, 5, 2, 1), (12, 6, 5, 1), (28, 28, 0, 0)]


def test_upload_bounds_and_session_revocation(db, tmp_path, monkeypatch, key, caplog):  # noqa: F811
    import logging
    from threading import Event

    from reviewscope.access import upload_wire
    from reviewscope.import_preview.contract import MAX_BYTES, MAX_CONFIG, MAX_ROWS

    _, store_root, w, s = command(db, tmp_path)
    pw = secrets.token_urlsafe(20)
    actor = provision(db, "audit-analyst", pw, key, workspace=w, role="analyst")
    caplog.set_level(logging.INFO, logger="reviewscope.access.requests")
    loc = str(
        db.execute(
            "SELECT location_id FROM source_location WHERE source_id=%s AND external_place_id='p1'",
            (s,),
        ).fetchone()[0]
    )
    base = f"{ROOT}/workspaces/{w}/sources/{s}/uploads/"
    marker = "private-fixture-" + uuid4().hex

    def posting(web, path, parts, **changes):
        mapping, passport, raw = parts
        headers = (
            CSRF
            | {
                "content-type": "application/octet-stream",
                "x-rs-parts": ",".join(str(len(b)) for b in parts),
                "x-rs-format": "csv",
                "x-rs-locations": loc,
                "x-rs-file-sha256": hashlib.sha256(raw).hexdigest(),
            }
            | changes
        )
        return web.post(path, headers=headers, content=iter((mapping, passport, raw)))

    parts = [(FIX / f).read_bytes() for f in ("mapping.json", "a.passport.json", "a.csv")]
    db.execute(
        "INSERT INTO browser_admission VALUES(%s,%s,%s)",
        (w, s, hashlib.sha256(parts[-1]).hexdigest()),
    )
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        url = base + str(uuid4())
        assert login(web, "audit-analyst", pw).status_code == 204
        assert (
            posting(web, url + "/preview", parts, **{"Origin": "https://invalid.test"}).status_code
            == 403
        )
        assert posting(web, url + "/preview", parts, **{"Host": "invalid.test"}).status_code == 400
        assert (
            posting(
                web, url + "/preview", parts, **{"x-rs-parts": f"1,1,{MAX_BYTES + 1}"}
            ).status_code
            == 413
        )
        assert (
            posting(
                web, url + "/preview", parts, **{"x-rs-parts": f"{MAX_CONFIG + 1},1,1"}
            ).status_code
            == 413
        )
        assert posting(web, url + "/preview", parts, **{"x-rs-parts": "1,1,1"}).status_code == 413
        assert (
            posting(web, url + "/preview", parts, **{"x-rs-locations": str(uuid4())}).status_code
            == 404
        )
        assert (
            posting(web, url + "/preview", parts, **{"x-rs-file-sha256": "/" + marker}).status_code
            == 422
        )
        invalid = posting(web, url + "/preview", [('{"' + marker + '":"x"}').encode(), *parts[1:]])
        assert (
            invalid.status_code == 422 and marker not in invalid.text and marker not in caplog.text
        )
        for wrong in (url.replace(str(w), str(uuid4())), url.replace(str(s), str(uuid4()))):
            assert posting(web, wrong + "/preview", parts).json() == {"error": "not_found"}
        result = posting(web, url + "/preview", parts)
        assert result.status_code == 200, result.text
        entered, release = Event(), Event()
        original = upload_wire.commit

        def after_approval(message, blobs):
            entered.set()
            assert release.wait(10)
            return original(message, blobs)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
            patch.setattr(upload_wire, "commit", after_approval)
            future = pool.submit(
                posting,
                web,
                url + "/confirm",
                parts,
                **{"x-rs-fingerprint": result.json()["fingerprint"]},
            )
            try:
                assert entered.wait(10)
                db.execute(
                    "UPDATE workspace_membership SET state='inactive' WHERE workspace_id=%s AND principal_id=%s",
                    (w, actor),
                )
            finally:
                release.set()
            assert future.result().status_code == 404
        assert not list(store_root.iterdir()) and db.execute(
            "SELECT count(*) FROM snapshot"
        ).fetchone() == (0,)
        db.execute(
            "UPDATE workspace_membership SET state='active' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        db.execute("UPDATE browser_delivery SET expires_at=now()-interval '1 minute'")
        assert web.get(url).status_code == 404
        assert (
            posting(
                web, url + "/confirm", parts, **{"x-rs-fingerprint": result.json()["fingerprint"]}
            ).status_code
            == 404
        )

        header = b"id,body\n"
        width, extra = divmod(MAX_BYTES - len(header) - MAX_ROWS * 7, MAX_ROWS)
        raw = header + b"".join(
            b"%05d,%s\n" % (i, b"t" * (width + (i < extra))) for i in range(MAX_ROWS)
        )
        assert len(raw) == MAX_BYTES
        db.execute(
            "INSERT INTO browser_admission VALUES(%s,%s,%s)",
            (w, s, hashlib.sha256(raw).hexdigest()),
        )
        bounded_url = base + str(uuid4())
        bounded_parts = [
            b'{"version":"bounded","fields":{"id":"source_review_id","body":"text"}}',
            b'{"source":"synthetic","source_place_id":"p1","delimiter":","}',
            raw,
        ]
        report = posting(web, bounded_url + "/preview", bounded_parts)
        assert report.status_code == 200, report.status_code
        assert report.json()["diagnostics"]["summary"]["accepted"] == MAX_ROWS
        assert db.execute("SELECT count(*) FROM browser_delivery").fetchone() == (1,)
        assert not list(store_root.iterdir())
        started = time.monotonic()
        applied = posting(
            web,
            bounded_url + "/confirm",
            bounded_parts,
            **{"x-rs-fingerprint": report.json()["fingerprint"]},
        )
        assert applied.status_code == 200, applied.status_code
        assert db.execute("SELECT count(*) FROM review_version").fetchone() == (MAX_ROWS,)
        confirmed_seconds = time.monotonic() - started
        db.execute(
            "UPDATE browser_admission SET evidence_allowed=true WHERE workspace_id=%s AND source_id=%s",
            (w, s),
        )
        since = time.monotonic()
        bounded_evidence = web.get(
            f"{ROOT}/workspaces/{w}/snapshots/{applied.json()['snapshot_id']}/reviews",
            params={"position": MAX_ROWS},
        )
        assert (
            bounded_evidence.status_code == 200
            and bounded_evidence.json()["rows"][0]["position"] == MAX_ROWS
        )
        print(f"Evidence lookup over {MAX_ROWS} positions: {time.monotonic() - since:.3f}s")
        print(
            f"Browser confirmation: {MAX_BYTES} bytes, {MAX_ROWS} positions, {confirmed_seconds:.2f}s"
        )

        from operational.tests.test_candidate_search import measure_boundary

        measure_boundary(db, web, w, s, applied.json()["snapshot_id"])
