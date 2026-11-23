"""Offline HTTP boundary and RBAC. No database or model download."""

import json
import logging
import os
import secrets
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient

from reviewscope.access.http import create_app
from reviewscope.access.security import permits

BASE = "http://127.0.0.1:8001"
ROOT = "/api/v1"
CSRF = {"Origin": BASE, "X-RS-CSRF": "1"}


def test_policy():
    for role in ("owner", "analyst", "viewer", "stranger"):
        for action, allowed in {
            "read": {"owner", "analyst", "viewer"},
            "members": {"owner"},
            "retention": {"owner"},
            "register_delivery": {"owner", "analyst"},
            "start_analysis": {"owner", "analyst"},
            "unknown": set(),
        }.items():
            assert permits(role, action) == (role in allowed)


def test_transport_csrf_and_redaction(monkeypatch, caplog):
    monkeypatch.setenv("RS_ACCESS_HTTP_LOOPBACK", "1")
    monkeypatch.delenv("RS_ACCESS_KEY_FILE", raising=False)
    caplog.set_level(logging.INFO, logger="reviewscope.access.requests")
    canary = "private-" + uuid4().hex
    headers = {h: canary for h in ("X-Principal-ID", "X-Workspace-ID", "X-Role", "X-Request-ID")}
    data = {"login": canary, "password": secrets.token_urlsafe(24)}
    with TestClient(create_app(), base_url=BASE, client=("127.0.0.1", 1234)) as http:
        resp = http.get(ROOT + "/session", headers=headers)
        assert resp.status_code == 401 and resp.headers["x-request-id"] != canary
        assert resp.headers["cache-control"] == "no-store"
        for csrf in (
            {},
            {"Origin": BASE},
            {"X-RS-CSRF": "1"},
            {**CSRF, "Origin": "https://invalid.test"},
        ):
            assert http.post(ROOT + "/session/login", json=data, headers=csrf).status_code == 403
            assert http.post(ROOT + "/session/logout", headers=csrf).status_code == 403
            assert (
                http.patch(
                    ROOT + "/workspaces/" + str(uuid4()) + "/members/" + str(uuid4()),
                    headers=csrf,
                    json={"role": "owner", "state": "active"},
                ).status_code
                == 403
            )
        assert (
            http.options(ROOT + "/session", headers={"Origin": BASE}).headers.get(
                "access-control-allow-origin"
            )
            is None
        )
        assert http.post(ROOT + "/session/login", headers=CSRF, json=data).status_code == 503
        assert (
            http.post(
                ROOT + "/session/login", headers=CSRF, json=data | {"path": canary}
            ).status_code
            == 422
        )
        assert http.get(ROOT + "/workspaces", params={"path": canary}).status_code == 422
        assert http.get(ROOT + "/session", headers={"Host": "external.test"}).status_code == 400
        assert (
            http.post(ROOT + "/session/login", headers=CSRF, content=b"x" * 4097).status_code == 413
        )
        assert http.get("/" + canary).status_code == 404
    with TestClient(create_app(), base_url=BASE, client=("192.0.2.1", 1234)) as http:
        assert http.get(ROOT + "/session").status_code == 400
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK")
    with TestClient(create_app(), base_url=BASE, client=("127.0.0.1", 1234)) as http:
        assert http.get(ROOT + "/session").status_code == 400
    events = [r.getMessage() for r in caplog.records if r.name == "reviewscope.access.requests"]
    assert events and all(
        set(json.loads(e))
        == {
            "event",
            "request_id",
            "route_template",
            "analysis_execution_id",
            "status",
            "duration_ms",
            "error",
        }
        for e in events
    )
    assert canary not in str(events) and data["password"] not in str(events)


def test_backend_free_auth_smoke():
    script = """
import sys
class NoML:
    def find_spec(self, name, *args):
        if name.split('.')[0] in {'torch','sentence_transformers','transformers'}:
            raise RuntimeError('unexpected ML import')
sys.meta_path.insert(0, NoML())
from fastapi.testclient import TestClient
from reviewscope.access.http import create_app
with TestClient(create_app(),base_url='https://127.0.0.1:8001') as http:
    assert http.get('/api/v1/session').status_code == 401
"""
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RS_ACCESS_HTTP_LOOPBACK": "0"},
        check=True,
    )


def test_worker_openapi_and_legacy_wire(monkeypatch):
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK", raising=False)
    with TestClient(create_app(), base_url="https://127.0.0.1:8001") as http:
        schema = http.get("/openapi.json").json()
    routes = schema["paths"]
    base = ROOT + "/workspaces/{workspace}/runs"
    assert set(routes[base]) == {"post", "get"}
    models = schema["components"]["schemas"]
    assert set(models["Run"]["required"]) == {"id", "snapshot_id", "status"}
    assert set(models["JobRequest"]["required"]) == {
        "snapshot_id",
        "idempotency_key",
        "job_kind",
        "engine_version",
    }
    assert set(models["JobStatus"]["properties"]["status"]["enum"]) == {
        "queued",
        "running",
        "failed",
        "succeeded",
        "cancelled",
    }
    for path, method in [
        (base, "post"),
        (base + "/{run_id}/cancel", "post"),
        (base + "/{run_id}/status", "get"),
        (ROOT + "/workspaces/{workspace}/snapshots/{snapshot_id}/latest-summary", "get"),
    ]:
        op = routes[path][method]
        params = {(p["in"], p["name"]) for p in op["parameters"] if p["required"]}
        assert ("cookie", "__Host-rs_session") in params
        if method == "post":
            assert {("header", "Origin"), ("header", "X-RS-CSRF")} <= params
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/JobStatus"
        )
        assert op["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/JobErrorResponse"
        )
