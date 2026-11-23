"""Guard every operational test before creating any database."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

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


@pytest.fixture
def db(monkeypatch):
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
    with psycopg.connect("", autocommit=True) as conn:
        yield conn
