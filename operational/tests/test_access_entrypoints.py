"""Exercise the operator command and the TLS launcher with disposable credentials."""

import hashlib
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
from uuid import uuid4

import httpx

from operational import worker
from operational.tests.test_access import ORIGIN, ROOT
from operational.tests.test_deliveries import call, command, csv
from reviewscope.access import operator


def test_operator_and_https_launcher(db, tmp_path, monkeypatch, capsys):
    keyfile = tmp_path / "private-key"
    keyfile.write_bytes(secrets.token_bytes(32))
    keyfile.chmod(0o600)
    monkeypatch.setenv("RS_ACCESS_KEY_FILE", str(keyfile))
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK", raising=False)
    base, objects, workspace, _ = command(db, tmp_path)
    snap = call(base + csv("a") + ["--apply"])["snapshot_id"]
    password = secrets.token_urlsafe(24)
    account = "fixture-" + secrets.token_hex(8)
    monkeypatch.setattr(sys, "argv", ["operator", "--workspace", str(workspace), "--role", "owner"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: account)
    monkeypatch.setattr(operator.getpass, "getpass", lambda prompt: password)
    operator.main()
    output = capsys.readouterr()
    assert output.out == "Account provisioned.\n" and not output.err

    cert = tmp_path / "cert.pem"
    tls_key = tmp_path / "key.pem"
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
            tls_key,
            "-out",
            cert,
        ],
        check=True,
        capture_output=True,
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "reviewscope.access"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "RS_ACCESS_TLS_CERT": str(cert), "RS_ACCESS_TLS_KEY": str(tls_key)},
    )
    try:
        with httpx.Client(
            base_url=ORIGIN,
            verify=ssl.create_default_context(cafile=cert),
            trust_env=False,
            timeout=2,
        ) as client:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("dedicated loopback port 8001 unavailable")
                try:
                    response = client.get(ROOT + "/session")
                    break
                except httpx.ConnectError:
                    time.sleep(0.05)
            assert response.status_code == 401
            response = client.post(
                ROOT + "/session/login",
                headers={"Origin": ORIGIN, "X-RS-CSRF": "1"},
                json={"login": account, "password": password},
            )
            assert response.status_code == 204
            token = client.cookies["__Host-rs_session"]
            assert client.get(ROOT + "/workspaces").json()[0]["id"] == str(workspace)
            path = ROOT + f"/workspaces/{workspace}/runs"
            headers = {"Origin": ORIGIN, "X-RS-CSRF": "1"}
            body = dict(
                snapshot_id=snap,
                idempotency_key=str(uuid4()),
                job_kind=worker.KIND,
                engine_version="1",
            )
            queued = client.post(path, headers=headers, json=body)
            assert queued.status_code == 202
            rid = queued.json()["id"]
        subprocess.run([sys.executable, "-m", "operational.worker", "--once"], check=True)
        with httpx.Client(
            base_url=ORIGIN,
            verify=ssl.create_default_context(cafile=cert),
            trust_env=False,
            cookies={"__Host-rs_session": token},
        ) as observer:
            result = observer.get(path + f"/{rid}/status")
            assert result.status_code == 200 and "x-request-id" in result.headers
            status = result.json()
            raw = (objects / str(workspace) / "sha256" / status["sha256"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == status["sha256"]
            assert status["artifact_id"] and status["status"] == "succeeded"
            replay = observer.post(path, headers=headers, json=body)
            assert replay.status_code == 200 and replay.json() == status
            print(
                json.dumps(
                    dict(
                        probe="TLS-8001-A",
                        request=body,
                        queued=queued.json(),
                        status=status,
                        result=json.loads(raw),
                    ),
                    sort_keys=True,
                )
            )
    finally:
        process.terminate()
        log, _ = process.communicate(timeout=10)
    assert b"http_request_completed" in log
    assert all(value.encode() not in log for value in (account, password, token, str(workspace)))
