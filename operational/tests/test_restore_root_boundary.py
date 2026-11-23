"""A restore may only activate a newly materialized, independently witnessed object store."""

import hashlib
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from operational import restore_copy, retention
from operational.retention import access_seal, capture_access, recover
from operational.tests.conftest import freeze_objects, generation, prepare_copy
from operational.tests.test_retention import fresh_restore_cluster
from reviewscope.access.ledger import LedgerError, certified_space, gate, ledger_path, read_witness


@contextmanager
def restoration(db, tmp_path, monkeypatch):
    root = tmp_path / "original"
    root.mkdir(mode=0o700)
    (w,) = db.execute(
        "INSERT INTO workspace(name,delivery_object_root) VALUES ('synthetic',%s) RETURNING id",
        (str(root),),
    ).fetchone()
    storage = root / str(w) / "sha256"
    storage.parent.mkdir(mode=0o700)
    storage.mkdir(mode=0o700)
    data = b"synthetic archived object"
    obj = storage / hashlib.sha256(data).hexdigest()
    obj.write_bytes(data)
    obj.chmod(0o400)
    backup = tmp_path / "before.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", backup], check=True)
    frozen = freeze_objects(root, tmp_path, [w])
    with fresh_restore_cluster(monkeypatch, backup) as (_, target):
        with psycopg.connect("", autocommit=True) as restored:
            restored.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(target))
            )
            yield restored, w, obj, frozen, read_witness()["sha256"], capture_access(db)


def test_rejected_activation_preserves_original_root_for_retry(db, tmp_path, monkeypatch):
    with restoration(db, tmp_path, monkeypatch) as (target, w, obj, frozen, proof, access):
        copied = generation("prepared")
        token = prepare_copy(target, frozen, copied, proof, access)
        for change, undo in (
            (
                "GRANT SELECT(position) ON import_row TO rs_worker",
                "REVOKE SELECT(position) ON import_row FROM rs_worker",
            ),
            (
                "ALTER TABLE snapshot_member DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE snapshot_member ENABLE ROW LEVEL SECURITY",
            ),
        ):
            target.execute(change)
            with pytest.raises(ValueError, match="restore_roles_unverified"):
                recover(target, proof, [token], access)
            target.execute(undo)
        # Rights changed in the restored DB. Only independent offline reconciliation can reopen it.
        target.execute("UPDATE workspace SET state='archived' WHERE id=%s", (w,))
        with pytest.raises(ValueError, match="access_review_required"):
            recover(target, proof, [token], access)
        assert target.execute("SELECT checkpoint FROM retention_control").fetchone() == (None,)
        registered = "SELECT delivery_object_root FROM workspace WHERE id=%s"
        initial = str(obj.parents[2])
        assert target.execute(registered, (w,)).fetchone() == (initial,)
        other = prepare_copy(target, frozen, generation("other"), proof, access)
        with pytest.raises(ValueError, match="unverified_recovery_roots"):
            recover(target, proof, [other], access)
        saved = copied.rename(tmp_path / "saved")
        copied.mkdir(mode=0o700)
        with pytest.raises(ValueError, match="restore_receipt_invalid"):
            recover(target, proof, [token], access)
        copied.rename(tmp_path / "unused")
        saved.rename(copied)
        assert read_witness()["sha256"] == proof
        assert target.execute(registered, (w,)).fetchone() == (initial,)
        target.execute("UPDATE workspace SET state='active' WHERE id=%s", (w,))
        original_save = retention.save

        def interrupted(conn, value):
            original_save(conn, value)
            raise RuntimeError("injected_before_database_commit")

        with monkeypatch.context() as patch:
            patch.setattr(retention, "save", interrupted)
            with pytest.raises(RuntimeError, match="injected_before_database_commit"):
                recover(target, proof, [token], access)
        head = read_witness()["sha256"]
        assert head != proof
        with target.cursor() as cur, pytest.raises(LedgerError):
            gate(cur)
        with pytest.raises(ValueError):
            recover(target, head, [other], access)
        assert recover(target, head, [token], access) == head
        assert recover(target, head, [token], access) == head
        assert target.execute(registered, (w,)).fetchone() == (str(copied),)
        inventory = ledger_path().parent / ("inventory-" + frozen[2])
        saved_inventory = inventory.rename(inventory.with_suffix(".saved"))
        with target.cursor() as cur, pytest.raises(LedgerError):
            gate(cur)
        saved_inventory.rename(inventory)
        record = ledger_path().parent / ("restore-" + token)
        hidden = record.rename(record.with_suffix(".saved"))
        with target.cursor() as cur, pytest.raises(LedgerError):
            gate(cur)
        record.write_bytes(b"synthetic invalid")
        record.chmod(0o400)
        with target.cursor() as cur, pytest.raises(LedgerError):
            gate(cur)
        record.unlink()
        hidden.rename(record)
        with target.cursor() as cur:
            assert gate(cur)["sha256"] == head


def test_live_storage_attestation_changes_when_archive_moves(db, tmp_path, monkeypatch):
    with restoration(db, tmp_path, monkeypatch) as (target, w, obj, frozen, proof, access):
        moved = obj.parents[2].rename(tmp_path / "relocated")
        db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(moved), w))
        assert (proof, access) == (read_witness()["sha256"], capture_access(db))
        contents = next(moved.glob("*/sha256/*"))
        before = contents.stat(), contents.read_bytes()
        with pytest.raises(ValueError, match="destination_scope"):
            prepare_copy(target, frozen, moved, proof, access)
        with pytest.raises(ValueError, match="restore_receipt_required"):
            recover(target, proof, {w: moved}, access)
        assert os.path.samestat(before[0], contents.stat()) and contents.read_bytes() == before[1]
        assert read_witness()["sha256"] == proof


def test_preexisting_unfinished_recovery_requires_fresh_copy(db, tmp_path, monkeypatch):
    with restoration(db, tmp_path, monkeypatch) as (target, _, _, frozen, proof, access):
        token = prepare_copy(target, frozen, generation("prepared"), proof, access)
        target.execute(
            "UPDATE retention_control SET checkpoint=NULL,recovery_binding=%s", ("a" * 64,)
        )
        with pytest.raises(ValueError, match="unverified_recovery_roots"):
            recover(target, proof, [token], access)
        assert read_witness()["sha256"] == proof
        assert target.execute("SELECT checkpoint FROM retention_control").fetchone() == (None,)


def test_refused_and_interrupted_materialization(db, tmp_path, monkeypatch):
    with restoration(db, tmp_path, monkeypatch) as (target, _, live, frozen, proof, access):
        source, manifest, inv = frozen
        obj = next(source.glob("*/sha256/*"))
        original = obj.read_bytes()

        destinations = {}

        def attempt(name, archive=frozen):
            return prepare_copy(
                target, archive, destinations.setdefault(name, generation(name)), proof, access
            )

        with pytest.raises(ValueError, match="inventory_unverified"):
            attempt("wrong-manifest", (source, manifest, "0" * 64))
        alternate = manifest.rename(tmp_path / "frozen-inventory")
        with pytest.raises(FileNotFoundError):
            attempt("absent")
        alternate.rename(manifest)
        untrusted = generation("without-authority")
        with monkeypatch.context() as patch:
            patch.delenv("RS_RESTORE_VOLUME_SHA256")
            with pytest.raises(ValueError, match="mount_authority_unavailable"):
                prepare_copy(target, frozen, untrusted, proof, access)
        assert not untrusted.exists()
        working_child = live.parents[2] / "new-child"
        with pytest.raises(ValueError, match="destination_scope"):
            prepare_copy(target, frozen, working_child, proof, access)
        assert not working_child.exists()
        saved = obj.rename(tmp_path / "object-aside")
        with pytest.raises(ValueError, match="inventory_mismatch"):
            attempt("missing")
        obj.symlink_to(live)
        with pytest.raises(OSError):
            attempt("symlink")
        obj.unlink()
        os.link(live, obj)
        with pytest.raises(ValueError, match="unexpected_object"):
            attempt("hardlink")
        obj.unlink()
        obj.write_bytes(b"x" * len(original))
        obj.chmod(0o400)
        with pytest.raises(ValueError, match="inventory_mismatch"):
            attempt("changed")
        obj.unlink()
        saved.rename(obj)
        real_transfer = restore_copy.transfer

        def interrupted(*args):
            real_transfer(*args)
            raise OSError("injected_after_file_sync")

        with monkeypatch.context() as patch:
            patch.setattr(restore_copy, "transfer", interrupted)
            with pytest.raises(OSError):
                attempt("partial")
        assert not list(ledger_path().parent.glob("restore-*"))
        with pytest.raises(FileExistsError):
            attempt("partial")
        assert live.read_bytes() == original and obj.read_bytes() == original
        token = attempt("complete")
        assert token and read_witness()["sha256"] == proof
        with pytest.raises(LedgerError):
            certified_space(read_witness(), live.parent.parent.name, destinations["complete"])
        with pytest.raises(FileExistsError):
            attempt("complete")
        copied = next(destinations["complete"].glob("*/sha256/*"))
        copied.chmod(0o600)
        copied.write_bytes(b"x" * len(original))
        with pytest.raises(ValueError, match="inventory_mismatch"):
            recover(target, proof, [token], access)
        assert target.execute("SELECT recovery_binding FROM retention_control").fetchone() == (
            None,
        )


def test_actual_bind_alias_rejected():
    from reviewscope.access.restore import check_mount, unique_mount, volume_authority

    base = Path("/mnt")
    assert (base / "probe-native").samefile(base / "probe-bind")
    authority = volume_authority()["native_mounts"]
    for name in ("probe-native", "probe-bind", "probe-unregistered"):
        fd = os.open(base / name, os.O_DIRECTORY)
        try:
            assert (unique_mount(fd) in authority) == (name == "probe-native")
            # A second visible mount invalidates the original too. The third FS has only one mount.
            with pytest.raises(ValueError, match="mount_unverified"):
                check_mount(fd)
        finally:
            os.close(fd)


def test_access_authority_covers_function_grants_and_policy_predicates(db, tmp_path, monkeypatch):
    with restoration(db, tmp_path, monkeypatch) as (target, w, obj, frozen, proof, access):
        assert access_seal(target) == access
        key = prepare_copy(target, frozen, generation("acl"), proof, access)
        # A harmless *narrower* privilege must still differ from the independent access authority.
        target.execute("REVOKE EXECUTE ON FUNCTION login_salt(text) FROM rs_runtime")
        assert access_seal(target) != access, "function privilege drift missing from access seal"
        with pytest.raises(ValueError, match="access_review_required"):
            recover(target, proof, [key], access)
        target.execute("GRANT EXECUTE ON FUNCTION login_salt(text) TO rs_runtime")
        assert access_seal(target) == access
        target.execute("ALTER POLICY self_membership ON workspace_membership USING (false)")
        assert access_seal(target) != access, "RLS predicate drift missing from access seal"
        with pytest.raises(ValueError, match="access_review_required"):
            recover(target, proof, [key], access)
        assert read_witness()["sha256"] == proof
        assert obj.exists()
