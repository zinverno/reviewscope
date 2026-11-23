"""A refused offline activation must preserve the storage boundary for the next attempt."""

import shutil
import subprocess

import psycopg
import pytest
from psycopg import sql

from operational.retention import access_seal, recover
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
            assert restored.execute("SELECT checkpoint FROM retention_control").fetchone() == (
                None,
            )
            assert read_witness() == witness
            assert not any(before.iterdir()) and not any(copied.iterdir())
            assert restored.execute(query, (workspace,)).fetchone() == (str(before),)
            alternative = tmp_path / "different-copy"
            shutil.copytree(before, alternative)
            for other in (before, alternative):
                with pytest.raises(ValueError, match="unverified_recovery_roots"):
                    recover(restored, witness["sha256"], {workspace: other}, access_seal(db))
                assert read_witness() == witness
                assert restored.execute(query, (workspace,)).fetchone() == (str(before),)
            saved = copied.rename(tmp_path / "saved-copy")
            copied.mkdir(mode=0o700)
            with pytest.raises(ValueError, match="unverified_recovery_roots"):
                recover(restored, witness["sha256"], {workspace: copied}, access_seal(db))
            copied.rename(tmp_path / "unused-copy")
            saved.rename(copied)
            recover(restored, witness["sha256"], {workspace: copied}, access_seal(db))
            assert restored.execute(query, (workspace,)).fetchone() == (str(copied),)
            assert not any(before.iterdir()) and not any(alternative.iterdir())


def test_live_storage_attestation_changes_when_archive_moves(db, tmp_path):
    # Both roots are newly created and empty. Only the authoritative metadata is compared.
    initial = tmp_path / "initial-generation"
    relocated = tmp_path / "relocated-generation"
    initial.mkdir(mode=0o700)
    shutil.copytree(initial, relocated)
    assert not initial.samefile(relocated)
    (workspace,) = db.execute(
        "INSERT INTO workspace(name,delivery_object_root) VALUES ('synthetic',%s) RETURNING id",
        (str(initial),),
    ).fetchone()
    before = (read_witness()["sha256"], access_seal(db))
    db.execute(
        "UPDATE workspace SET delivery_object_root=%s WHERE id=%s",
        (str(relocated), workspace),
    )
    after = (read_witness()["sha256"], access_seal(db))
    if before == after:
        pytest.fail("restore authorities do not distinguish two live storage generations")


def test_preexisting_unfinished_recovery_requires_fresh_copy(db):
    witness = read_witness()
    db.execute("UPDATE retention_control SET checkpoint=NULL,recovery_binding=%s", ("a" * 64,))
    with pytest.raises(ValueError, match="unverified_recovery_roots"):
        recover(db, witness["sha256"], {}, access_seal(db))
    assert read_witness() == witness
    assert db.execute("SELECT checkpoint FROM retention_control").fetchone() == (None,)
