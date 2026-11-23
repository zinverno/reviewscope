"""Actual restricted database, local intake socket and web sessions on synthetic data."""

import hashlib
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
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


def test_cabinet_native(db, tmp_path, monkeypatch, key):  # noqa: F811
    """Drive the built cabinet against the TLS launcher, live broker and bounded worker."""
    import json
    from pathlib import Path

    from operational.tests.test_deliveries import json_input

    _, _root, w, s = command(db, tmp_path)
    db.execute(
        "UPDATE location SET name=sl.external_place_id FROM source_location sl WHERE location.id=sl.location_id"
    )
    pw = secrets.token_urlsafe(24)
    provision(db, "cabinet-owner", pw, key, workspace=w)
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
    for file in (FIX / "a.csv", FIX / "b.csv", j / "data"):
        db.execute(
            "INSERT INTO browser_admission VALUES (%s,%s,%s)",
            (w, s, hashlib.sha256(file.read_bytes()).hexdigest()),
        )
    cert, tls = tmp_path / "cert.pem", tmp_path / "tls.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            tls,
            "-out",
            cert,
        ],
        check=True,
        capture_output=True,
    )
    ui = Path("prototypes/reviewscope-ui").resolve()
    assert (ui / "dist/cabinet.html").is_file(), (
        "Build the cabinet before its native acceptance test"
    )
    with importer(tmp_path, monkeypatch):
        access = subprocess.Popen(
            [sys.executable, "-m", "reviewscope.access"],
            env=os.environ | {"RS_ACCESS_TLS_CERT": str(cert), "RS_ACCESS_TLS_KEY": str(tls)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
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
            inputs = dict(
                account="cabinet-owner",
                password=pw,
                w=str(w),
                s=str(s),
                csv=str(FIX.resolve()),
                json=str(j),
            )
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
                while native.poll() is None:
                    # Let the browser close its tab and cancel its first queued attempt before the worker starts.
                    if db.execute(
                        "SELECT count(*) FROM analysis_run WHERE status='cancelled'"
                    ).fetchone()[0]:
                        subprocess.run(
                            [sys.executable, "-m", "operational.worker", "--once"],
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
    ) == [(3, 3, 0, 0), (6, 6, 0, 0), (12, 6, 5, 1)]
