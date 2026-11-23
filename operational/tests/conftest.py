"""Guard every operational test before creating any database."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from operational.retention import initialize

ALEMBIC = [sys.executable, "-m", "alembic", "-c", "operational/alembic.ini"]


@pytest.fixture(autouse=True, scope="session")
def private_cluster():
    socket = os.environ.get("RS_TEST_PG_SOCKET")
    if (
        not socket
        or os.environ.get("PGHOST") != socket
        or not Path(socket, "data", "PG_VERSION").is_file()
    ):
        pytest.fail("use operational/test-postgres.sh")


def new_db():
    name = "rs_test_" + uuid4().hex
    subprocess.run(["createdb", name], check=True)
    return name


@pytest.fixture(autouse=True)
def ledger_directory(tmp_path, monkeypatch):
    directory = tmp_path / "independent-ledger"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("RS_RETENTION_LEDGER", str(directory / "authority.json"))


@pytest.fixture
def db(monkeypatch):
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
    with psycopg.connect("", autocommit=True) as conn:
        initialize(conn)
        yield conn


def freeze_objects(root, area, workspaces):
    """The backup operator freezes its inventory before any live changes or restore."""
    spaces = {str(w): {} for w in workspaces}
    for obj in root.glob("*/sha256/*"):
        content = obj.read_bytes()
        key = hashlib.sha256(content).hexdigest()
        assert key == obj.name
        spaces[obj.parent.parent.name][key] = len(content)
    raw = json.dumps({"version": 1, "backup_id": str(uuid4()), "workspaces": spaces}).encode()
    manifest = area / "backup-inventory"
    manifest.write_bytes(raw)
    manifest.chmod(0o600)
    source = area / "frozen-store"
    shutil.copytree(root, source)
    return source, manifest, hashlib.sha256(raw).hexdigest()


def prepare_copy(target, frozen, dest, witness, access, **kwargs):
    from operational.restore_copy import prepare

    source, manifest, digest = frozen
    return prepare(target, source, dest, manifest, digest, witness, access, **kwargs)
