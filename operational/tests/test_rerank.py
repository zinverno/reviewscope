"""Derived artifacts share the parent's source, immutable definition and deletion scope."""

import hashlib
import os
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational import worker
from operational.delivery_objects import DeliveryError
from operational.retention import capture_access, initialize, verify_access_permissions
from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_aspects import A, create, post
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX, call, command, csv
from operational.tests.test_worker import cli
from reviewscope.access.http import create_app
from reviewscope.access.ledger import ledger_path
from reviewscope.access.operator import provision


@pytest.fixture
def prior_db(monkeypatch):
    monkeypatch.setenv("PGDATABASE", new_db())
    subprocess.run([*ALEMBIC, "upgrade", "0014_aspects"], check=True)
    with psycopg.connect(autocommit=True) as db:
        initialize(db)
        yield db


def test_derived_rerank(prior_db, tmp_path, monkeypatch, key):  # noqa: F811
    db = prior_db
    args, root, w, src = command(db, tmp_path)
    project = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
    snap = call(args + csv("a") + ["--apply"])["snapshot_id"]
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed) VALUES (%s,%s,%s,true,true)",
        (w, src, hashlib.sha256((FIX / "a.csv").read_bytes()).hexdigest()),
    )
    aspects = f"{ROOT}/workspaces/{w}/projects/{project}/aspects"
    path = f"{ROOT}/workspaces/{w}/snapshots/{snap}/candidates"
    password = secrets.token_urlsafe(32)
    actor = provision(db, "ranker", password, key, workspace=w, role="analyst")
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "ranker", password).status_code == 204
        bound = create(web, aspects, A)
        parent = post(
            web,
            path,
            dict(
                aspect=bound,
                query="ждать",
                source_id=str(src),
                role="query_to_passage",
                mode="lexical",
                k=5,
                idempotency_key=str(uuid4()),
            ),
            202,
        )["id"]
        assert cli("parent") == "succeeded"
        original = web.get(path, params=dict(source_id=src, run_id=parent))
        assert original.status_code == 200
        oldseal = capture_access(db)
        subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
        with pytest.raises(DeliveryError, match="access_review_required"):
            verify_access_permissions(db, oldseal)
        verify_access_permissions(db, capture_access(db))
        assert web.get(path, params=dict(source_id=src, run_id=parent)).content == original.content
        post(
            web,
            f"{aspects}/{bound['aspect_id']}/versions",
            dict(
                expected_version=bound["version_id"],
                payload=A | {"name": "Уточнённая очередь"},
                idempotency_key=str(uuid4()),
            ),
            201,
        )
        route = f"{path}/{parent}/rerank"
        req = dict(
            source_id=str(src), idempotency_key=str(uuid4()), profile="mmarco-context-logit-v1"
        )
        post(web, route, req | {"source_id": str(uuid4())}, 404)
        post(web, route, req | {"query": "другой запрос"}, 422)

        def replay(_):
            with TestClient(create_app(), base_url=ORIGIN) as other:
                other.cookies.update(web.cookies)
                return other.post(route, json=req, headers=CSRF)

        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(replay, range(2)))
        assert sorted(r.status_code for r in replies) == [200, 202]
        run = replies[0].json()["id"]
        assert replies[1].json()["id"] == run
        # Exercise the explicit fallback even on a laptop provisioned for evaluation.
        model_root = os.getenv("RS_RERANK_MODELS")
        monkeypatch.delenv("RS_RERANK_MODELS", raising=False)
        assert cli("rerank") == "succeeded"
        url = f"{route}/{run}"
        derived = web.get(url, params=dict(source_id=src))
        assert derived.status_code == 200, derived.text
        assert derived.json()["status"] == "BASELINE_RETAINED"
        assert derived.json()["reason"] == "NOT_RUN_MISSING_WEIGHTS"
        assert derived.json()["ranking"] == []
        assert derived.json()["parent_sha256"] == original.json()["artifact_sha256"]
        assert web.get(path, params=dict(source_id=src, run_id=parent)).content == original.content
        assert (
            web.get(url.replace(parent, str(uuid4())), params=dict(source_id=src)).status_code
            == 404
        )
        parent_object = root / str(w) / "sha256" / original.json()["artifact_sha256"]
        parked = parent_object.with_suffix(".held")
        parent_object.rename(parked)
        try:
            assert web.get(url, params=dict(source_id=src)).status_code == 503
            post(web, route, req | {"idempotency_key": str(uuid4())}, 503)
        finally:
            parked.rename(parent_object)
        if model_root:
            monkeypatch.setenv("RS_RERANK_MODELS", model_root)
            measured = post(web, route, req | {"idempotency_key": str(uuid4())}, 202)["id"]
            assert cli("measured-rerank") == "succeeded"
            view = web.get(f"{route}/{measured}", params=dict(source_id=src))
            assert view.status_code == 200 and view.json()["status"] == "READY", view.text
            assert {h["fragment_id"] for h in view.json()["ranking"]} == {
                h["fragment_id"] for h in original.json()["result"]["candidates"]
            }
        cancelled = post(web, route, req | {"idempotency_key": str(uuid4())}, 202)["id"]
        web.post(f"{ROOT}/workspaces/{w}/runs/{cancelled}/cancel", headers=CSRF).raise_for_status()
        assert (
            web.get(f"{route}/{cancelled}", params=dict(source_id=src)).json()["status"]
            == "cancelled"
        )
        with monkeypatch.context() as mp, worker.connect() as conn:
            mp.delenv("RS_RERANK_MODELS", raising=False)
            fenced = post(web, route, req | {"idempotency_key": str(uuid4())}, 202)["id"]
            original_summary = worker.summarize

            def revoke(*args):
                result = original_summary(*args)
                db.execute(
                    "UPDATE browser_admission SET candidate_allowed=false WHERE source_id=%s",
                    (src,),
                )
                return result

            mp.setattr(worker, "summarize", revoke)
            assert (
                worker.process(conn, conn.execute("SELECT * FROM claim_job()").fetchone(), Event())
                == "failed"
            )
            assert db.execute(
                "SELECT reason,output_artifact_id FROM analysis_run WHERE id=%s", (fenced,)
            ).fetchone() == ("ACCESS_REVOKED", None)
            assert web.get(url, params=dict(source_id=src)).status_code == 404
            db.execute(
                "UPDATE browser_admission SET candidate_allowed=true WHERE source_id=%s", (src,)
            )
        witness = ledger_path()
        hidden = witness.with_suffix(".held")
        witness.rename(hidden)
        try:
            assert web.get(url, params=dict(source_id=src)).status_code == 503
        finally:
            hidden.rename(witness)
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        assert web.get(url, params=dict(source_id=src)).status_code == 403
        post(web, route, req | {"idempotency_key": str(uuid4())}, 403)
        db.execute(
            "UPDATE workspace_membership SET role='analyst' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        pending = post(web, route, req | {"idempotency_key": str(uuid4())}, 202)["id"]
        db.execute("UPDATE access_session SET revoked_at=now() WHERE principal_id=%s", (actor,))
        cli("lost-session")
        assert db.execute(
            "SELECT status,reason,output_artifact_id FROM analysis_run WHERE id=%s", (pending,)
        ).fetchone() == ("failed", "ACCESS_REVOKED", None)
        assert web.get(url, params=dict(source_id=src)).status_code == 401
