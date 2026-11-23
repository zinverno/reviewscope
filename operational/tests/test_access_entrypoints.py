"""Exercise the operator command and the TLS launcher with disposable credentials."""

import os
import secrets
import ssl
import subprocess
import sys
import time

import httpx

from operational.tests.test_access import ORIGIN, ROOT, dataset
from reviewscope.access import operator


def test_operator_and_https_launcher(db, tmp_path, monkeypatch, capsys):
    keyfile = tmp_path / "private-key"
    keyfile.write_bytes(secrets.token_bytes(32))
    keyfile.chmod(0o600)
    monkeypatch.setenv("RS_ACCESS_KEY_FILE", str(keyfile))
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK", raising=False)
    workspace, _ = dataset(db, "synthetic")
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
    finally:
        process.terminate()
        log, _ = process.communicate(timeout=10)
    assert b"http_request_completed" in log
    assert all(value.encode() not in log for value in (account, password, token, str(workspace)))
