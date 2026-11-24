"""Compare the same synthetic receipts, author responses and saved candidates across 0013."""

import hashlib
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.delivery_objects import DeliveryError
from operational.retention import (
    access_record,
    capture_access,
    initialize,
    verify_access_permissions,
)
from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.reviewer_plan_probe import enable_client_plans, plan_observation
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import call, command, json_input
from operational.tests.test_reviewer_context import FIELDS, PASSPORT, context_url
from operational.tests.test_schema import bind_place, identity, receipt, scope, version
from operational.tests.test_worker import cli
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

MIGRATION = Path(__file__).parents[1] / "migrations/versions/0013_reviewer_context_plan.sql"
FUNCTION = "'reviewer_context_page(uuid,uuid,uuid,uuid,integer,integer)'::regprocedure"


@pytest.fixture
def prior_db(monkeypatch):
    monkeypatch.setenv("PGDATABASE", new_db())
    subprocess.run([*ALEMBIC, "upgrade", "0012_candidate_search"], check=True)
    with psycopg.connect(autocommit=True) as db:
        initialize(db)
        yield db


def test_reviewer_context_upgrade(prior_db, tmp_path, monkeypatch, key):  # noqa: F811
    db = prior_db
    args, _, w, src = command(db, tmp_path)
    password = secrets.token_urlsafe(24)
    provision(db, "upgrade-reader", password, key, workspace=w)
    for table in ("review_version", "import_row", "snapshot_member"):
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    _, project, sibling = scope(db, workspace=w)
    bind_place(db, w, project, sibling)
    version(db, w, sibling, identity(db, w, sibling, "0"), receipt(db, w, sibling), text="other")
    scopes = []
    for size in (100, 1000):
        folder = tmp_path / str(size)
        folder.mkdir()
        rows = [
            dict(
                id=str(i),
                author="author" if i % 10 else "other",
                place="p1",
                text=f"Чек {i}. Доставка {size}.",
                stars=i % 5 + 1,
            )
            for i in range(size)
        ]
        inputs = json_input(folder, rows, {k: FIELDS[k] for k in rows[0]}, PASSPORT)
        snap = call(args + inputs + ["--apply"])["snapshot_id"]
        digest = hashlib.sha256((folder / "data").read_bytes()).hexdigest()
        db.execute(
            """INSERT INTO browser_admission
                   (workspace_id,source_id,checksum,evidence_allowed,author_context_allowed,candidate_allowed)
                   VALUES (%s,%s,%s,true,true,true)""",
            (w, src, digest),
        )
        (anchor,) = db.execute(
            """SELECT r.version_id FROM snapshot sn JOIN import_row r
                ON (r.workspace_id,r.source_id,r.batch_id)=(sn.workspace_id,sn.source_id,sn.batch_id)
                WHERE sn.id=%s AND r.position=2""",
            (snap,),
        ).fetchone()
        scopes.append((w, src, snap, anchor))
    enable_client_plans(db)
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "upgrade-reader", password).status_code == 204
        proof = session_proof(web.cookies[COOKIE], key)
        _, _, snap, anchor = scopes[-1]
        url = context_url(w, snap, anchor)
        route = f"{ROOT}/workspaces/{w}/snapshots/{snap}/candidates"
        params = dict(source_id=str(src))
        prior = web.get(url, params=params)
        assert prior.status_code == 200 and prior.json()["total"] == 900
        queued = web.post(
            route,
            headers=CSRF,
            json=params
            | dict(
                query="Чек",
                role="query_to_passage",
                mode="lexical",
                k=3,
                idempotency_key=str(uuid4()),
            ),
        )
        assert queued.status_code == 202 and cli("pre-upgrade-candidates") == "succeeded"
        run = params | dict(run_id=queued.json()["id"])
        saved = web.get(route, params=run)
        assert saved.status_code == 200 and saved.json()["result"]["retrieved_review_count"] == 3
        seal = capture_access(db)
        authority = access_record(db)["access"]
        before = [
            plan_observation(s, proof, "force_generic_plan", "0012-fresh", fixed=False)
            for s in scopes
        ]
        # Stress the old correlated plan with inflated random I/O cost in this reader only.
        adverse = [
            plan_observation(
                s, proof, "force_generic_plan", "0012-cost", fixed=False, expensive_io=True
            )
            for s in scopes
        ]
        assert adverse == before
        fn = f"SELECT prosrc,proacl FROM pg_proc WHERE oid={FUNCTION}"
        original, acl = db.execute(fn).fetchone()
        with db.transaction(force_rollback=True):
            db.execute(MIGRATION.read_text())
            replaced, same_acl = db.execute(fn).fetchone()
            assert replaced != original and same_acl == acl
        assert db.execute(fn).fetchone() == (original, acl)
        subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
        assert db.execute(fn).fetchone() == (replaced, acl)
        assert access_record(db)["access"] == authority
        with pytest.raises(DeliveryError, match="access_review_required"):
            verify_access_permissions(db, seal)
        current_seal = capture_access(db)
        assert current_seal != seal
        verify_access_permissions(db, current_seal)
        assert web.get(route, params=run).content == saved.content
        assert web.get(url, params=params).content == prior.content
        for s, expected in zip(scopes, before, strict=True):
            assert (
                plan_observation(
                    s, proof, "force_generic_plan", "0013-cost", fixed=True, expensive_io=True
                )
                == expected
            )
        for stage in ("0013-fresh", "0013-analyzed"):
            if stage.endswith("analyzed"):
                db.execute("ANALYZE review_version, import_row, snapshot_member")
            for s, expected in zip(scopes, before, strict=True):
                for mode in ("force_generic_plan", "force_custom_plan"):
                    assert plan_observation(s, proof, mode, stage, fixed=True) == expected
