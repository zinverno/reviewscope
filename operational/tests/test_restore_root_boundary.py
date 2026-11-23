"""A refused offline activation must preserve the storage boundary for the next attempt."""

import shutil
import subprocess

import psycopg
import pytest
from psycopg import sql

from operational.retention import recover
from operational.tests.test_retention import fresh_restore_cluster
from reviewscope.access.ledger import read_witness


def test_rejected_activation_preserves_original_root_for_retry(db, tmp_path, monkeypatch):
    # No receipts, erasure requests or files. Neither directory is used for a purge.
    before = tmp_path / "original"
    before.mkdir(mode=0o700)
    (workspace,) = db.execute(
        "INSERT INTO workspace(name,delivery_object_root) VALUES ('synthetic',%s) RETURNING id",
        (str(before),),
    ).fetchone()
    backup = tmp_path / "before.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", backup], check=True)
    copied = tmp_path / "copy"
    shutil.copytree(before, copied)
    witness = read_witness()
    assert not witness["body"]["events"]

    with fresh_restore_cluster(monkeypatch, backup) as (_, target):
        with psycopg.connect("", autocommit=True) as restored:
            restored.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(target))
            )
            query = "SELECT delivery_object_root FROM workspace WHERE id=%s"
            assert restored.execute(query, (workspace,)).fetchone() == (str(before),)
            with pytest.raises(ValueError, match="access_review_required"):
                recover(restored, witness["sha256"], {workspace: copied}, "0" * 64)
            assert restored.execute("SELECT checkpoint FROM retention_control").fetchone() == (None,)
            assert read_witness() == witness
            assert not any(before.iterdir()) and not any(copied.iterdir())
            # recover() uses this column as the forbidden original on its next attempt.
            (stored,) = restored.execute(query, (workspace,)).fetchone()
            if stored == str(copied):
                pytest.fail("rejected activation committed the candidate root in place of the original")
            assert stored == str(before), "original root missing after refused activation"
