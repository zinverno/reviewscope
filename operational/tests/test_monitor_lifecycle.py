"""Exercise monitor ACL, scoped owner export, deletion, and recovery on a second cluster."""

import json
import subprocess
from threading import Event
from uuid import uuid4
from zipfile import ZipFile

import psycopg
import pytest
from psycopg import sql

from operational import worker
from operational.retention import capture_access, recover
from operational.tests.conftest import freeze_objects, generation, prepare_copy
from operational.tests.human_lifecycle import neighbor
from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import FIELDS, cabinet, query
from operational.tests.test_deliveries import call, json_input
from operational.tests.test_retention import ask, fresh_restore_cluster, operator
from reviewscope.access.ledger import read_witness
from reviewscope.aspect_decisions import definitions


@pytest.mark.parametrize("kind", ["source", "project", "workspace"])
def test_monitor_retention(db, tmp_path, monkeypatch, key, kind):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, definition=definitions()["queue"]) as (
            s,
            reader,
            proof,
            root,
            web,
            p,
        ),
        worker.connect() as conn,
    ):
        w, src, first, aspect, version = s
        other, other_run, _ = neighbor(db, root, web, s, proof, conn, tmp_path)
        saved = query(
            reader,
            "monitor_save(%s,%s,%s,NULL,NULL,%s,%s,'test',1,1,'accepted_in_delivery',false)",
            (w, p, src, aspect, version),
        )
        m, v = saved["monitor_id"], saved["version_id"]
        db.execute("UPDATE browser_admission SET monitor_allowed=true WHERE source_id=%s", (src,))
        args = [
            "--workspace",
            str(w),
            "--source",
            str(src),
            "--object-root",
            str(root),
            "--coverage-passport",
            str(tmp_path / "coverage"),
        ]
        updated = json_input(
            tmp_path, [dict(id="0", text="Сцена 0"), dict(id="1", text="Изменение")], FIELDS
        )
        second = call([*args, *updated, "--apply"])["snapshot_id"]
        db.execute(
            """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,decision_allowed,monitor_allowed)
                   SELECT workspace_id,source_id,file_sha256,true,true,true,true FROM import_batch WHERE
                   id=(SELECT batch_id FROM snapshot WHERE id=%s)""",
            (second,),
        )
        parent = None
        for sn in (first, second):
            result = query(
                reader,
                "monitor_start(%s,%s,%s,%s,%s,%s,%s,%s)",
                (w, p, src, m, v, sn, parent, uuid4()),
            )
            assert result["code"] == 202
            parent = result["run_id"]
            job = conn.execute("SELECT * FROM claim_job()").fetchone()
            assert worker.process(conn, job, Event()) == "succeeded"
        target = {"workspace": w, "source": src, "project": p}[kind]
        request = ask(web, w, kind, target, "export")
        handoff = tmp_path / "handoff"
        handoff.mkdir(mode=0o700)
        operator("export", "--request", request["id"], "--root", root, "--owner-handoff", handoff)
        with ZipFile(handoff / (request["id"] + ".zip")) as archive:
            receipts = [
                json.loads(line) for line in archive.read("rows/monitor_receipt.jsonl").splitlines()
            ]
            assert len(receipts) == 2 and {row["document"]["monitor_id"] for row in receipts} == {m}
            assert all(row["workspace_id"] == str(w) for row in receipts)
            assert len(archive.read("rows/monitor_reuse.jsonl").splitlines()) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
            reader.execute("SELECT * FROM monitor_receipt")
        assert denied.value.sqlstate == "42501"
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
            reader.execute("SELECT * FROM monitor_delta_rows(%s,%s,%s,%s)", (w, src, first, second))
        assert denied.value.sqlstate == "42501"
        backup = tmp_path / "before.dump"
        subprocess.run(["pg_dump", "-Fc", "-f", backup], check=True)
        frozen = freeze_objects(root, tmp_path, [w, other])
        request = ask(web, w, kind, target, "erase")
        assert request["report"]["tables"]["monitor_run"] == 2
        operator(
            "confirm",
            "--request",
            request["id"],
            "--preview-sha256",
            request["preview_sha"],
            "--root",
            root,
        )
        operator("purge", "--request", request["id"], "--root", root)

        def erased(db):
            assert query(db, "count(*) FROM monitor WHERE monitor_id=%s", (m,)) == 0
            assert query(db, "count(*) FROM monitor_receipt WHERE workspace_id=%s", (w,)) == 0
            assert query(db, "decision_manifest_valid(%s,%s)", (other, other_run))

        erased(db)
        digest, seal = read_witness()["sha256"], capture_access(db)
        with (
            fresh_restore_cluster(monkeypatch, backup) as (_, name),
            psycopg.connect(autocommit=True) as restored,
        ):
            restored.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            assert query(restored, "count(*) FROM monitor_receipt WHERE workspace_id=%s", (w,)) == 2
            token = prepare_copy(restored, frozen, generation("monitors"), digest, seal)
            recover(restored, digest, [token], seal)
            erased(restored)


def test_monitor_populated_upgrade(tmp_path, key, monkeypatch):  # noqa: F811
    from pathlib import Path

    from operational.delivery_objects import DeliveryError
    from operational.retention import initialize, verify_access_permissions, verify_restore_roles
    from operational.tests.conftest import ALEMBIC, new_db
    from operational.tests.human_lifecycle import write_history
    from operational.tests.test_decision_storage import page
    from operational.tests.test_monitor_history import decision

    monkeypatch.setenv("PGDATABASE", new_db())
    subprocess.run([*ALEMBIC, "upgrade", "0020_topic_lock_order"], check=True)
    with psycopg.connect(autocommit=True) as db:
        initialize(db)
        with (
            cabinet(db, tmp_path, key, definition=definitions()["queue"]) as (
                s,
                reader,
                _,
                root,
                _,
                _,
            ),
            worker.connect() as conn,
        ):
            w, src = s[:2]
            db.execute(
                "UPDATE browser_admission SET human_review_allowed=true,relation_allowed=true,topic_discovery_allowed=true WHERE source_id=%s",
                (src,),
            )
            run, old = decision(conn, reader, s)
            reviewed = next(
                x["version_id"] for x in old["rows"] if x["outcome"] != "excluded_blank"
            )
            _, _, revision = write_history(reader, s, run, reviewed)
            graph = query(
                reader, "relation_publish(%s,%s,%s,%s,%s,%s)", (*s[:3], run, revision, uuid4())
            )
            topics = query(reader, "topic_publish(%s,%s,%s,NULL,NULL,%s)", (*s[:3], uuid4()))
            assert graph["code"] == topics["code"] == 201

            def fingerprints():
                tables = (
                    "import_batch",
                    "snapshot",
                    "snapshot_member",
                    "aspect_version",
                    "decision_binding",
                    "decision_row",
                    "human_review_result",
                    "human_review_event",
                    "relation_result",
                    "topic_result",
                    "topic_candidate",
                )
                return {
                    t: query(
                        db,
                        f"md5(coalesce(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)),'')) FROM {t} t WHERE workspace_id=%s",
                        (w,),
                    )
                    for t in tables
                }

            rows = fingerprints()
            access = capture_access(db)
            business = query(db, "retention_access_seal()", ())
            files = {f.name: f.read_bytes() for f in (root / str(w) / "sha256").iterdir()}
            with db.transaction(force_rollback=True):
                db.execute(
                    Path("operational/migrations/versions/0021_saved_monitors.sql").read_text()
                )
                assert query(db, "to_regclass('monitor') IS NOT NULL", ())
            assert query(db, "to_regclass('monitor')", ()) is None
            verify_access_permissions(db, access)
            subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
            assert query(db, "retention_access_seal()", ()) == business
            with pytest.raises(DeliveryError, match="access_review_required"):
                verify_access_permissions(db, access)
            verify_restore_roles(db)
            assert rows == fingerprints() and page(reader, s, run) == old
            assert files == {f.name: f.read_bytes() for f in (root / str(w) / "sha256").iterdir()}
            assert not query(db, "bool_or(monitor_allowed) FROM browser_admission", ())
