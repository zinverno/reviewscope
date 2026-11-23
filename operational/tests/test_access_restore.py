"""Restore privileges, principal IDs and history along with PR-07 object references."""

import secrets
import shutil
import subprocess

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from operational.retention import access_seal, recover
from operational.tests.conftest import new_db
from operational.tests.test_access import COOKIE, ORIGIN, ROOT, login
from operational.tests.test_deliveries import call, command, csv
from operational.tests.test_schema import scope
from reviewscope.access.http import create_app
from reviewscope.access.ledger import read_witness
from reviewscope.access.operator import provision


def test_restored_access_requires_fresh_session(db, tmp_path, monkeypatch):
    key = secrets.token_bytes(32)
    path = tmp_path / "key"
    path.write_bytes(key)
    path.chmod(0o600)
    monkeypatch.setenv("RS_ACCESS_KEY_FILE", str(path))
    monkeypatch.delenv("RS_ACCESS_HTTP_LOOPBACK", raising=False)
    base, root, w, own = command(db, tmp_path)
    call(base + csv("a") + ["--apply"])
    other, _, foreign = scope(db)
    restored_root = tmp_path / "copied-objects"
    shutil.copytree(root, restored_root)
    password = secrets.token_urlsafe(32)
    actor = provision(db, "restored", password, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as http:
        assert login(http, "restored", password).status_code == 204
        prior = http.cookies[COOKIE]
    backup = tmp_path / "synthetic.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", backup], check=True)
    name = new_db()
    subprocess.run(
        [
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--dbname",
            name,
            backup,
        ],
        check=True,
    )
    monkeypatch.setenv("PGDATABASE", name)
    with psycopg.connect("", autocommit=True) as restored:
        restored.execute(
            sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        )
        recover(restored, read_witness()["sha256"], {w: restored_root}, access_seal(db))
        restored.execute(
            "UPDATE access_session SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL"
        )
        for table in (
            "principal",
            "workspace_membership",
            "membership_event",
            "review_version",
            "snapshot_member",
            "snapshot",
        ):
            query = sql.SQL("SELECT * FROM {} ORDER BY 1,2").format(sql.Identifier(table))
            assert restored.execute(query).fetchall() == db.execute(query).fetchall()
    with TestClient(create_app(), base_url=ORIGIN) as http:
        assert (
            http.get(ROOT + "/session", headers={"Cookie": COOKIE + "=" + prior}).status_code == 401
        )
        assert login(http, "restored", password).status_code == 204
        assert http.get(ROOT + "/session").json()["principal_id"] == str(actor)
        assert http.get(ROOT + f"/workspaces/{w}/sources/{own}").status_code == 200
        assert http.get(ROOT + f"/workspaces/{other}/sources/{foreign}").status_code == 404
