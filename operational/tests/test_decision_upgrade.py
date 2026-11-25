"""Upgrade a populated 0015 cabinet and verify preserved bytes and scoped decision reads."""

import hashlib
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.delivery_objects import DeliveryError
from operational.retention import capture_access, initialize, verify_access_permissions
from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_aspects import A, B, create, post
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX, call, command, csv
from operational.tests.test_worker import cli
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision

MIGRATION = Path(__file__).parents[1] / "migrations/versions/0016_aspect_decisions.sql"


def test_decision_upgrade_preserves_saved_runs(tmp_path, monkeypatch, key):  # noqa: F811
    monkeypatch.setenv("PGDATABASE", new_db())
    monkeypatch.delenv("RS_RERANK_MODELS", raising=False)
    subprocess.run([*ALEMBIC, "upgrade", "0015_rerank"], check=True)
    with psycopg.connect(autocommit=True) as db:
        initialize(db)
        args, root, w, src = command(db, tmp_path)
        snap = call(args + csv("a") + ["--apply"])["snapshot_id"]
        project = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
        db.execute(
            "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed) VALUES (%s,%s,%s,true,true)",
            (w, src, hashlib.sha256((FIX / "a.csv").read_bytes()).hexdigest()),
        )
        actor_password = secrets.token_urlsafe(32)
        actor = provision(db, "upgrade-author", actor_password, key, workspace=w)
        access = f"{ROOT}/workspaces/{w}"
        aspects = f"{access}/projects/{project}/aspects"
        base = f"{access}/snapshots/{snap}"
        route = base + "/decisions"
        search = base + "/candidates"
        scope = {"source_id": str(src)}
        with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
            assert login(web, "upgrade-author", actor_password).status_code == 204
            a = create(web, aspects, A)
            unsupported = create(web, aspects, B | {"name": "Другая версия"})
            binding = scope | a
            assert web.get(route, params=binding).status_code == 503
            query = dict(
                source_id=str(src),
                query="Ждать не пришлось",
                role="query_to_passage",
                mode="lexical",
                k=5,
            )
            free = post(web, search, query | {"idempotency_key": str(uuid4())}, 202)["id"]
            assert cli("legacy-19") == "succeeded"
            parent = post(web, search, query | {"aspect": a, "idempotency_key": str(uuid4())}, 202)[
                "id"
            ]
            assert cli("legacy-20") == "succeeded"
            derived = f"{search}/{parent}/rerank"
            rerank = post(
                web,
                derived,
                scope | dict(profile="mmarco-context-logit-v1", idempotency_key=str(uuid4())),
                202,
            )["id"]
            assert cli("legacy-21") == "succeeded"
            read_urls = [
                (search, scope | {"run_id": free}),
                (search, scope | {"run_id": parent}),
                (f"{derived}/{rerank}", scope),
                (f"{aspects}/{a['aspect_id']}/versions/{a['version_id']}", {}),
            ]

            def old_bytes():
                result = []
                for path, params in read_urls:
                    response = web.get(path, params=params)
                    response.raise_for_status()
                    result.append(response.content)
                return result

            def privileges():
                return db.execute("""SELECT
                     has_column_privilege('rs_runtime','analysis_run','candidate_params','SELECT'),
                     has_any_column_privilege('rs_runtime','aspect_version','SELECT'),
                     has_any_column_privilege('rs_runtime','review_version','SELECT'),
                     has_function_privilege('rs_runtime','rerank_visible(uuid,uuid,uuid,uuid,uuid)','EXECUTE'),
                     has_function_privilege('rs_upload','rerank_object(uuid,uuid,uuid,uuid,uuid,bigint,text)','EXECUTE')""").fetchone()

            saved = old_bytes()
            acl = privileges()
            assert acl == (False, False, False, True, True)
            old_seal = capture_access(db)
            objects = []
            for run in (free, parent, rerank):
                sha = db.execute(
                    "SELECT checksum FROM artifact WHERE run_id=%s", (run,)
                ).fetchone()[0]
                path = root / str(w) / "sha256" / sha
                raw = path.read_bytes()
                assert hashlib.sha256(raw).hexdigest() == sha
                objects.append((path, raw))
            with db.transaction(force_rollback=True):
                db.execute(MIGRATION.read_text())
                assert db.execute("SELECT count(*) FROM decision_binding").fetchone() == (0,)
            assert db.execute("SELECT to_regclass('decision_row')").fetchone() == (None,)
            subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
            with pytest.raises(DeliveryError, match="access_review_required"):
                verify_access_permissions(db, old_seal)
            verify_access_permissions(db, capture_access(db))
            assert privileges() == acl
            assert [path.read_bytes() for path, _ in objects] == [raw for _, raw in objects]
            assert old_bytes() == saved
            command_body = binding | dict(
                profile="aspect-linear-lexical-v1", idempotency_key=str(uuid4())
            )
            assert web.get(route, params=binding).status_code == 404
            post(web, route, command_body, 404)
            db.execute(
                "UPDATE browser_admission SET decision_allowed=true WHERE source_id=%s", (src,)
            )
            assert web.get(route, params=binding).json()["supported_definition"]
            assert not web.get(route, params=scope | unsupported).json()["supported_definition"]
            post(web, route, command_body | unsupported, 422)
            run = post(web, route, command_body, 202)["id"]
            status = f"{route}/{run}"
            page = web.get(status, params=scope)
            assert page.status_code == 200
            assert page.json()["compute_state"] == "NOT_COMPUTED" and page.json()["summary"] is None
            assert page.json()["rows"] == []
            assert post(web, route, command_body, 200)["id"] == run
            post(web, route, command_body | unsupported, 409)
            assert web.get(status, params=scope | {"offset": 10001}).status_code == 422
            assert web.get(status, params={"source_id": str(uuid4())}).status_code == 404
            assert web.get(status, params=scope | {"version_id": str(uuid4())}).status_code == 404
            assert cli("decision") == "succeeded"
            page = web.get(status, params=scope)
            assert page.status_code == 200, page.text
            view = page.json()
            assert view["compute_state"] == "COMPLETE" and view["summary"]["N_accepted"] == 3
            assert view["filtered_total"] == len(view["rows"]) == 3
            assert len(page.content) <= 65536
            ref = view["rows"][0]
            detail = web.get(status, params=scope | dict(version_id=ref["version_id"]))
            assert detail.status_code == 200
            exact = (
                web.get(f"{base}/reviews/{ref['version_id']}", params=scope).json()["rows"][0][
                    "full_text"
                ]
                or ""
            )
            excerpt = detail.json()["evidence"]
            assert excerpt["text"] == exact[excerpt["start"] : excerpt["end"]]
            assert hashlib.sha256(exact.encode()).hexdigest() == ref["text_sha256"]
            path = root / str(w) / "sha256" / view["artifact_sha256"]
            held = path.with_suffix(".parked")
            path.rename(held)
            try:
                assert web.get(status, params=scope).status_code == 503
            finally:
                held.rename(path)
            for column in ("decision_allowed", "evidence_allowed", "candidate_allowed"):
                # Static identifiers from this fixture.
                db.execute(
                    f"UPDATE browser_admission SET {column}=false WHERE source_id=%s", (src,)
                )
                assert web.get(status, params=scope).status_code == 404
                db.execute(f"UPDATE browser_admission SET {column}=true WHERE source_id=%s", (src,))
            db.execute(
                "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
                (w, actor),
            )
            assert web.get(status, params=scope).status_code == 403
            assert web.get(route, params=binding).status_code == 403
            post(web, route, command_body, 403)
