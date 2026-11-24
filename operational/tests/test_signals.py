"""Real job, scoped TLS API, private broker and immutable evidence on synthetic inputs."""

import hashlib
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from operational import signals
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX, call, command, csv
from operational.tests.test_worker import cli, request_for
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


def signal_job(web, path, snap):
    body = request_for(snap) | {"job_kind": signals.KIND}
    first = web.post(path + "/runs", json=body, headers=CSRF)
    assert first.status_code == 202, first.status_code
    replay = web.post(path + "/runs", json=body, headers=CSRF)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    return first.json()["id"]


def test_signal_runs(db, tmp_path, monkeypatch, key):  # noqa: F811
    base, root, w, s = command(db, tmp_path)
    password = secrets.token_urlsafe(20)
    actor = provision(db, "signal-owner", password, key, workspace=w)
    path = f"{ROOT}/workspaces/{w}"
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "signal-owner", password).status_code == 204
        for name, mean, n in (("a", 11 / 3, 3), ("b", 22 / 6, 6)):
            snap = call(base + csv(name) + ["--apply"])["snapshot_id"]
            route = path + f"/snapshots/{snap}/signals"
            assert web.get(route, params={"source_id": s}).status_code == 404
            db.execute(
                "INSERT INTO browser_admission VALUES(%s,%s,%s,true)",
                (w, s, hashlib.sha256((FIX / (name + ".csv")).read_bytes()).hexdigest()),
            )
            semantic = []
            for attempt in (1, 2):
                run = signal_job(web, path, snap)
                assert cli(f"signals-{name}-{attempt}") == "succeeded"
                query = {"source_id": s, "run_id": run}
                response = web.get(route, params=query)
                assert response.status_code == 200, response.status_code
                obj = response.json()
                doc = obj["result"]
                assert doc["coverage"] == "unknown" and doc["places"]["p1"]["raw"]["value"] == mean
                days = doc["places"]["p1"]["observed_days"]
                assert sum(days.values()) == sum(
                    r["observed_day"] is not None for r in doc["reviews"]
                )
                if name == "a":
                    assert days == {"2026-09-08": 1, "2026-09-12": 1, "2026-09-21": 1}
                assert doc["places"]["p1"]["events"]["reason"] == "INSUFFICIENT_COVERAGE"
                assert doc["places"]["p1"]["weighted"]["value"] is None
                assert all(not r["author_present"] and r["specificity"] for r in doc["reviews"])
                evidence = web.get(path + f"/snapshots/{snap}/reviews").json()["rows"]
                assert {r["version_id"] for r in doc["reviews"]} == {
                    r["version_id"] for r in evidence
                }
                assert len(doc["reviews"]) == n
                semantic.append(doc["result_sha256"])
            assert semantic[0] == semantic[1]
            assert web.get(route, params=query | {"source_id": uuid4()}).status_code == 404
            assert web.get(route, params=query | {"run_id": uuid4()}).status_code == 404
            file = root / str(w) / "sha256" / obj["artifact_sha256"]
            missing = file.with_suffix(".saved")
            file.rename(missing)
            assert web.get(route, params=query).status_code == 503
            file.write_bytes(b"{}")
            file.chmod(0o600)
            assert web.get(route, params=query).status_code == 503
            missing.replace(file)
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        assert web.get(route, params=query).status_code == 403
        db.execute(
            "UPDATE workspace_membership SET role='analyst' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        pending = signal_job(web, path, snap)
        db.execute(
            "UPDATE browser_admission SET evidence_allowed=false WHERE workspace_id=%s", (w,)
        )
        assert cli("revoked") == "failed"
        assert web.get(path + f"/runs/{pending}/status").json()["reason"] == "ACCESS_REVOKED"
        assert web.get(route, params=query).status_code == 404
