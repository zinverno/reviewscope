"""Synthetic CLI deliveries on the disposable PostgreSQL cluster."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from operational.backfill_demo import DEMO_SHA, import_demo
from operational.retention import access_seal, initialize, recover
from operational.tests.conftest import ALEMBIC, freeze_objects, new_db, prepare_copy
from operational.tests.test_schema import bind_place, receipt, run, scope
from reviewscope.access.ledger import read_witness
from reviewscope.import_preview import preview
from reviewscope.public_demo import DEFAULT_DEMO_DB, repo_root

FIX = Path("tests/data/fixtures/import_preview")
COVER = dict(
    coverage="unknown",
    filters=None,
    mode=None,
    basis=None,
    covered_from=None,
    covered_until=None,
    received_at=None,
)


def command(db, tmp_path):
    w, project, s = scope(db, source_key="synthetic")
    for place in ("p1", "p2"):
        bind_place(db, w, project, s, place)
    (actor,) = db.execute(
        "INSERT INTO principal(login,state) VALUES ('operator','active') RETURNING id"
    ).fetchone()
    db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'analyst','active')", (w, actor))
    root = tmp_path / "objects"
    root.mkdir(mode=0o700)
    db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), w))
    db.execute(
        "INSERT INTO delivery_authorization VALUES (%s,%s,current_user,%s)",
        (w, s, actor),
    )
    (tmp_path / "coverage").write_text(json.dumps(COVER))
    args = [
        "--workspace",
        str(w),
        "--source",
        str(s),
        "--object-root",
        str(root),
        "--coverage-passport",
        str(tmp_path / "coverage"),
    ]
    return args, root, w, s


def inputs(data, mapping, passport, fmt, batch=None):
    fingerprint = preview(
        data.read_bytes(), fmt, mapping.read_bytes(), passport.read_bytes()
    ).fingerprint
    return [
        "--input",
        str(data),
        "--mapping",
        str(mapping),
        "--passport",
        str(passport),
        "--format",
        fmt,
        "--delivery-id",
        str(batch or uuid4()),
        "--preview-fingerprint",
        fingerprint,
    ]


def csv(name):
    return inputs(FIX / f"{name}.csv", FIX / "mapping.json", FIX / f"{name}.passport.json", "csv")


def json_input(directory, rows, fields, passport=None):
    for key, value in (
        ("data", rows),
        ("mapping", {"version": "synthetic-1", "fields": fields}),
        (
            "passport",
            passport if passport is not None else {"source": "synthetic", "source_place_id": "p1"},
        ),
    ):
        (directory / key).write_text(json.dumps(value))
    return inputs(directory / "data", directory / "mapping", directory / "passport", "json")


def call(args, *, error=None, env=None):
    result = subprocess.run(
        [sys.executable, "-m", "operational.import_delivery", *args], capture_output=True, env=env
    )
    if error:
        assert result.returncode == 2 and result.stdout == b""
        assert json.loads(result.stderr) == {"error": error}
    else:
        assert result.returncode == 0 and result.stderr == b""
        return json.loads(result.stdout)


def state(db):
    tables = (
        "review_identity",
        "review_version",
        "import_batch",
        "import_row",
        "snapshot",
        "snapshot_member",
    )
    return {
        table: db.execute(
            sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY 1").format(sql.Identifier(table))
        ).fetchall()
        for table in tables
    }


def test_two_deliveries_restore_every_version_and_byte(db, tmp_path):
    base, root, w, s = command(db, tmp_path)
    a, b = base + csv("a"), base + csv("b")
    assert call(a + ["--dry-run"])["outcome"] == "planned" and not list(root.iterdir())
    first = call(a + ["--apply"])
    assert first["input"] == first["accepted"] == 3
    snapshot_a = db.execute("SELECT version_ids FROM snapshot").fetchone()[0]
    second = call(b + ["--apply"])
    assert second["input"] == second["accepted"] == 6
    assert first["snapshot_id"] != second["snapshot_id"]
    assert (
        db.execute(
            "SELECT version_ids FROM snapshot WHERE id=%s", (first["snapshot_id"],)
        ).fetchone()[0]
        == snapshot_a
    )
    assert db.execute(
        "SELECT count(*),count(source_reviewer_id) FROM review_version"
    ).fetchone() == (7, 0)
    assert db.execute("""SELECT count(*) FROM review_version v JOIN review_identity i ON v.identity_id=i.id
                        WHERE i.external_review_id='r1'""").fetchone() == (2,)
    history = db.execute("""SELECT i.external_review_id,v.version_no,r.position,r.batch_id,r.version_id,v.supersedes_no,
        v.published_at,v.metadata,v.text_content FROM import_row r JOIN review_version v ON v.id=r.version_id
        JOIN review_identity i ON i.id=r.identity_id ORDER BY r.position,v.version_no""").fetchall()
    r1 = [x for x in history if x[0] == "r1"]
    assert [x[1] for x in r1] == [1, 2] and r1[0][4] in snapshot_a and r1[1][4] not in snapshot_a
    assert r1[1][5] == 1 and r1[0][8] != r1[1][8]
    for ext in ("r2", "r3"):
        assert len({x[4] for x in history if x[0] == ext}) == 1
    assert [x[2] for x in history if x[0] is None] == [5]
    assert r1[0][6].astimezone(UTC).isoformat() == "2026-09-08T14:10:00+00:00"
    assert r1[0][7]["event"] == dict(
        kind="instant",
        precision="second",
        raw="2026-09-08T17:10:00+03:00",
        offset="+03:00",
        value="2026-09-08T14:10:00+00:00",
    )
    assert r1[0][7]["rating_raw"] == "3" and r1[0][7]["rating_scale"] == [1, 5]
    assert db.execute("SELECT count(*) FROM analysis_run").fetchone() == (0,)
    assert db.execute(
        "SELECT count(*) FROM import_batch WHERE coverage='unknown' AND received_at IS NULL"
    ).fetchone() == (2,)
    before = state(db)
    archive = tmp_path / "db.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", archive], check=True)
    new = new_db()
    copy = tmp_path / "independent"
    frozen = freeze_objects(root, tmp_path, [w])
    subprocess.run(
        ["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "-d", new, archive],
        check=True,
    )
    with psycopg.connect(dbname=new) as restored:
        assert state(restored) == before
        # A restored installation must explicitly authorize the new object directory.
        restored.commit()
        restored.autocommit = True
        restored.execute(sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(new)))
        proof, access = read_witness()["sha256"], access_seal(db)
        token = prepare_copy(restored, frozen, copy, proof, access)
        recover(restored, proof, [token], access)
    restore_env = os.environ | {"PGDATABASE": new}
    for args, original in ((a, first), (b, second)):
        replay = [str(copy) if arg == str(root) else arg for arg in args]
        with psycopg.connect(dbname=new) as restored:
            file_sha, delivery = restored.execute(
                "SELECT file_sha256,delivery FROM import_batch WHERE id=%s",
                (args[args.index("--delivery-id") + 1],),
            ).fetchone()
        for flag, sha in (
            ("--input", file_sha),
            ("--mapping", delivery["mapping_sha256"]),
            ("--passport", delivery["passport_sha256"]),
            ("--coverage-passport", delivery["coverage_sha256"]),
        ):
            replay[replay.index(flag) + 1] = str(copy / str(w) / "sha256" / sha)
        assert call(replay + ["--dry-run"], env=restore_env) == original | {
            "outcome": "already_applied"
        }
    with psycopg.connect(dbname=new) as restored:
        (sha,) = restored.execute(
            "SELECT delivery->>'mapping_sha256' FROM import_batch LIMIT 1"
        ).fetchone()
    (copy / str(w) / "sha256" / sha).unlink()
    call(replay + ["--dry-run"], env=restore_env, error="io_error")
    assert state(db) == before
    with (repo_root() / DEFAULT_DEMO_DB).open("rb") as file:
        assert hashlib.file_digest(file, "sha256").hexdigest() == DEMO_SHA


def test_json_position_journal_and_replay(db, tmp_path):
    base, _, w, s = command(db, tmp_path)
    item = {"id": "conflict", "body": "private synthetic text"}
    original = {"id": "repeat", "body": None}
    anon = {"id": None}
    rows = [
        item,
        item,
        item | {"body": "changed"},
        original,
        original,
        anon,
        anon,
        {"id": "unique"},
        {"id": "r1", "place": "p1"},
        {"id": "r1", "place": "p2"},
        False,
        {"id": 42},
    ]
    # Missing place IDs are supplied in the input, independently of review identity.
    rows = [({"place": "p1"} | r) if isinstance(r, dict) else r for r in rows]
    args = base + json_input(
        tmp_path,
        rows,
        {"id": "source_review_id", "place": "source_place_id", "body": "text"},
        {"source": "synthetic"},
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: call(args + ["--apply"]), range(2)))
    assert {r["outcome"] for r in reports} == {"applied", "already_applied"}
    assert len({r["snapshot_id"] for r in reports}) == 1
    assert db.execute(
        "SELECT input_count,accepted_count,rejected_count,confirmed_duplicate_count FROM import_batch"
    ).fetchall() == [(12, 6, 5, 1)]
    result = preview(
        (tmp_path / "data").read_bytes(),
        "json",
        (tmp_path / "mapping").read_bytes(),
        (tmp_path / "passport").read_bytes(),
    )
    entries = db.execute(
        "SELECT position,status,reasons,warnings,related_position,version_id,identity_id,provenance,field_presence FROM import_row ORDER BY position"
    ).fetchall()
    assert [e[:5] for e in entries] == [
        (r.source_position, r.status, r.reasons, r.warnings, r.related_position)
        for r in result.rows
    ]
    assert [e[5] for e in entries[:3]] == [None] * 3
    assert entries[4][4] == 4 and entries[3][5:7] == entries[4][5:7]
    assert entries[5][6] != entries[6][6] and entries[8][6] != entries[9][6]
    assert entries[3][7]["text"] == "field:body" and entries[3][7]["source"] == "passport:source"
    assert entries[3][8]["text"] == "null" and "text" not in entries[5][8]
    assert db.execute(
        "SELECT count(*) FROM review_identity WHERE external_review_id='conflict'"
    ).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM review_version").fetchone() == (6,)
    args[args.index("--delivery-id") + 1] = str(uuid4())
    assert call(args + ["--apply"])["outcome"] == "applied"
    assert db.execute(
        "SELECT count(*),count(DISTINCT file_sha256) FROM import_batch"
    ).fetchone() == (2, 1)
    assert db.execute(
        "SELECT count(*) FROM review_identity WHERE external_review_id IS NULL"
    ).fetchone() == (4,)
    assert db.execute("SELECT count(*) FROM review_version").fetchone() == (8,)
    # A different payload with an independently verified fingerprint collides with the original token.
    different = csv("a")
    different[different.index("--delivery-id") + 1] = args[args.index("--delivery-id") + 1]
    call(base + different + ["--apply"], error="delivery_conflict")
    assert db.execute("SELECT count(*) FROM import_row").fetchone() == (24,)


def test_date_semantics_corrections_and_partial_coverage(db, tmp_path):
    base, _, _, _ = command(db, tmp_path)
    fields = {
        "id": "source_review_id",
        "time": "published_at",
        "score": "rating",
        "who": "reviewer_id",
    }
    passport = {
        "source": "synthetic",
        "source_place_id": "p1",
        "reviewer_id_allowed": True,
        "event_basis": "edited",
    }
    rows = [
        {"id": "x", "time": "2026-11-01", "score": 4, "who": None},
        {"id": "y", "time": "2026-11-01T01:30", "score": True, "who": "synthetic-author"},
        {"id": "z", "time": "bad"},
        {"id": "m"},
    ]
    call(base + json_input(tmp_path, rows, fields, passport) + ["--apply"])
    observation = db.execute("""SELECT i.external_review_id,v.event_date,v.event_at,v.published_date,v.published_at,
        v.metadata,v.rating,v.source_reviewer_id,r.warnings FROM review_version v JOIN review_identity i ON i.id=v.identity_id
        JOIN import_row r ON r.version_id=v.id""").fetchall()
    by_id = {r[0]: r[1:] for r in observation}
    assert by_id["x"][:4] == (date(2026, 11, 1), None, None, None)
    assert by_id["x"][4]["rating_raw"] == "4" and by_id["x"][5] is None
    assert by_id["y"][4]["rating_raw"] is True and by_id["y"][1] is None
    assert by_id["y"][6] == "synthetic-author" and "unknown_timezone" in by_id["y"][7]
    assert [by_id[k][4]["event"]["kind"] for k in ("z", "m")] == ["invalid", "missing"]
    (tmp_path / "coverage").write_text(
        json.dumps(COVER | {"coverage": "partial", "mode": "period"})
    )
    call(
        base
        + json_input(tmp_path, rows[:2], fields, passport | {"timezone": "America/New_York"})
        + ["--apply"]
    )
    assert db.execute(
        "SELECT count(*) FROM import_row WHERE 'ambiguous_time'=ANY(warnings)"
    ).fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM review_identity").fetchone() == (4,)
    assert db.execute("SELECT count(*) FROM review_version").fetchone() == (6,)
    assert db.execute("SELECT count(*) FROM analysis_run").fetchone() == (0,)
    # Successive independent edits serialize even when two operators start simultaneously.
    commands = []
    for score in (2, 3):
        sub = tmp_path / str(score)
        sub.mkdir()
        commands.append(
            base + json_input(sub, [rows[0] | {"score": score}], fields, passport) + ["--apply"]
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(r["outcome"] == "applied" for r in pool.map(call, commands))
    assert db.execute("""SELECT version_no,supersedes_no FROM review_version v JOIN review_identity i ON i.id=v.identity_id
                         WHERE i.external_review_id='x' ORDER BY version_no""").fetchall() == [
        (1, None),
        (2, 1),
        (3, 2),
        (4, 3),
    ]


def test_scope_failures_and_rollback(db, tmp_path):
    base, root, w, s = command(db, tmp_path)
    args = base + csv("a") + ["--apply"]
    bad = args.copy()
    bad[bad.index("--preview-fingerprint") + 1] = "a" * 64
    call(bad, error="preview_mismatch")
    other, _, foreign = scope(db, source_key="synthetic")
    bad = args.copy()
    bad[bad.index("--workspace") + 1] = str(other)
    call(bad, error="scope_denied")
    bad[bad.index("--source") + 1] = str(foreign)
    call(bad, error="scope_denied")
    call(args, env=os.environ | {"PGUSER": "rs_runtime"}, error="delivery_refused")
    db.execute("UPDATE workspace_membership SET role='viewer'")
    call(args, error="scope_denied")
    db.execute("UPDATE workspace_membership SET role='analyst'")
    for field, value in (("source", "foreign"), ("source_place_id", "unconfirmed")):
        foreign_input = json_input(
            tmp_path,
            [{"body": "private marker"}],
            {"body": "text"},
            {"source": "synthetic", "source_place_id": "p1"} | {field: value},
        )
        call(base + foreign_input + ["--apply"], error="scope_denied")
    db.execute("UPDATE workspace SET delivery_object_root=NULL WHERE id=%s", (w,))
    call(args, error="scope_denied")
    db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), w))
    assert not db.execute(
        "SELECT has_column_privilege('rs_runtime','workspace','delivery_object_root','SELECT')"
    ).fetchone()[0]
    # A second source grant in the workspace cannot direct bytes to a different archive.
    _, project, second = scope(db, workspace=w, source_key="second")
    bind_place(db, w, project, second)
    db.execute(
        """INSERT INTO delivery_authorization SELECT workspace_id,%s,operator_login,principal_id
                  FROM delivery_authorization WHERE workspace_id=%s AND source_id=%s""",
        (second, w, s),
    )
    second_args = (
        base
        + json_input(
            tmp_path,
            [{"body": "synthetic"}],
            {"body": "text"},
            {"source": "second", "source_place_id": "p1"},
        )
        + ["--dry-run"]
    )
    second_args[second_args.index("--source") + 1] = str(second)
    assert call(second_args)["outcome"] == "planned"
    second_args[second_args.index("--object-root") + 1] = str(tmp_path)
    call(second_args, error="scope_denied")
    assert not list(root.iterdir())
    db.execute("""CREATE FUNCTION interrupt_publish() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic private marker'; END $$;
        CREATE TRIGGER interrupt BEFORE INSERT ON snapshot FOR EACH ROW EXECUTE FUNCTION interrupt_publish()""")
    call(args, error="delivery_refused")
    assert not any(state(db).values()) and list((root / str(w) / "sha256").iterdir())
    db.execute("DROP TRIGGER interrupt ON snapshot")
    db.execute("""CREATE FUNCTION damage_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.manifest_sha256:=repeat('0',64); RETURN NEW; END $$;
        CREATE TRIGGER damage BEFORE INSERT ON snapshot FOR EACH ROW EXECUTE FUNCTION damage_manifest()""")
    call(args, error="delivery_refused")
    assert not any(state(db).values())
    db.execute("DROP TRIGGER damage ON snapshot")
    assert call(args)["outcome"] == "applied"
    old = state(db)
    (key,) = db.execute("SELECT duckdb_key FROM snapshot").fetchone()
    obj = root / key
    obj.chmod(0o600)
    obj.write_bytes(b"damaged")
    call(args, error="object_verification_failed")
    assert state(db) == old
    for table in (
        "delivery_authorization",
        "import_row",
        "review_identity",
        "review_version",
        "import_batch",
    ):
        assert db.execute(
            """SELECT has_table_privilege('rs_runtime',%s,'INSERT,UPDATE,DELETE,SELECT'),
            relrowsecurity FROM pg_class WHERE oid=%s::regclass""",
            (table, table),
        ).fetchone() == (False, True)


@pytest.mark.parametrize("ambiguous", [True, False])
def test_unresolved_upgrade_is_atomic(monkeypatch, ambiguous):
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "0003_workspace_access"], check=True)
    with psycopg.connect("", autocommit=True) as conn:
        w, p, s = scope(conn)
        (identity,) = conn.execute(
            """INSERT INTO review_identity(workspace_id,source_id,external_review_id)
            VALUES (%s,%s,'r1') RETURNING id""",
            (w, s),
        ).fetchone()
        if ambiguous:
            batch = receipt(conn, w, s)
            for n, place in enumerate(("p1", "p2"), 1):
                bind_place(conn, w, p, s, place)
                conn.execute(
                    """INSERT INTO review_version(workspace_id,source_id,batch_id,identity_id,version_no,
                    external_place_id,normalization_version) VALUES (%s,%s,%s,%s,%s,%s,'old')""",
                    (w, s, batch, identity, n, place),
                )
        fail = subprocess.run([*ALEMBIC, "upgrade", "head"], capture_output=True)
        assert fail.returncode and b"LEGACY_PLACE_UNRESOLVED" in fail.stdout + fail.stderr
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0003_workspace_access",
        )
        assert conn.execute("SELECT to_regclass('import_row')").fetchone() == (None,)
        assert conn.execute("SELECT id FROM review_identity").fetchone() == (identity,)


def test_historical_backfill_survives_upgrade(monkeypatch, tmp_path):
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "0001_operational"], check=True)
    with psycopg.connect("", autocommit=True) as db:
        w, _, s = scope(db)
        obj = tmp_path / str(w) / "sha256" / DEMO_SHA
        obj.parent.mkdir(parents=True)
        shutil.copyfile(repo_root() / DEFAULT_DEMO_DB, obj)
        receipt = import_demo(w, s, apply=True, object_root=tmp_path)
        rid = run(db, w, receipt["snapshot_id"])
        db.execute("UPDATE analysis_run SET status='running' WHERE id=%s", (rid,))
        legacy = b'{"historical":true}'
        digest = hashlib.sha256(legacy).hexdigest()
        aid, key = db.execute("""INSERT INTO artifact(workspace_id,run_id,kind,checksum,size_bytes)
            VALUES (%s,%s,'json',%s,%s) RETURNING id,object_key""",(w,rid,digest,len(legacy))).fetchone()
        (tmp_path / key).write_bytes(legacy)
        db.execute("UPDATE analysis_run SET status='succeeded',finished_at=now(),output_artifact_id=%s WHERE id=%s",(aid,rid))
        queries = (
            "SELECT id,external_review_id FROM review_identity ORDER BY 1",
            "SELECT id,identity_id,content_sha256,batch_id FROM review_version ORDER BY 1",
            "SELECT * FROM snapshot",
            "SELECT * FROM snapshot_member ORDER BY version_id",
            "SELECT workspace_id,id,snapshot_id,engine_version,model_version,definition_version,created_at,finished_at,status,output_artifact_id FROM analysis_run",
            "SELECT * FROM artifact",
        )
        before = [db.execute(q).fetchall() for q in queries]
        db.execute(
            """INSERT INTO import_batch(workspace_id,source_id,procedure_version,file_sha256,status)
                      VALUES (%s,%s,'legacy',%s,'accepted')""",
            (w, s, "0" * 64),
        )
        for rev in ("0002_principals", "0003_workspace_access", "0004_delivery_history", "0005_background_jobs", "head"):
            subprocess.run([*ALEMBIC, "upgrade", rev], check=True)
        initialize(db)
        assert before == [db.execute(q).fetchall() for q in queries]
        assert db.execute(
            "SELECT confirmed_duplicate_count FROM import_batch ORDER BY input_count NULLS LAST"
        ).fetchall() == [(0,), (None,)]
        assert (
            import_demo(w, s, apply=True, object_root=tmp_path)["snapshot_id"]
            == receipt["snapshot_id"]
        )
        assert hashlib.sha256((tmp_path / key).read_bytes()).hexdigest() == digest
        assert len(before[0]) == len(before[1]) == len(before[3]) == 1058


def test_version_place_fk_and_receipt_seal(db, tmp_path):
    args, _, w, s = command(db, tmp_path)
    call(args + csv("a") + ["--apply"])
    ident, batch = db.execute("SELECT identity_id,batch_id FROM import_row LIMIT 1").fetchone()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            """INSERT INTO review_version(workspace_id,source_id,identity_id,batch_id,external_place_id,
                    normalization_version,version_no) VALUES (%s,%s,%s,%s,'p2','v',2)""",
            (w, s, ident, batch),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("""INSERT INTO import_row SELECT workspace_id,source_id,batch_id,4,status,reasons,warnings,
            related_position,identity_id,version_id,provenance,field_presence FROM import_row LIMIT 1""")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("""INSERT INTO import_batch(workspace_id,source_id,file_sha256,procedure_version,status,
            input_count,accepted_count,rejected_count,confirmed_duplicate_count,delivery,registered_by)
            SELECT workspace_id,source_id,file_sha256,procedure_version,status,1,1,0,0,delivery,registered_by FROM import_batch""")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """INSERT INTO import_batch(workspace_id,source_id,file_sha256,procedure_version,status,
            input_count,accepted_count,rejected_count,confirmed_duplicate_count)
            VALUES (%s,%s,%s,'v','accepted',3,1,1,0)""",
            (w, s, "a" * 64),
        )
    for statement in (
        "DELETE FROM import_row",
        "TRUNCATE import_row CASCADE",
        "UPDATE import_row SET related_position=NULL",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(statement)


def test_precommit_validation_and_storage_permissions(db, tmp_path, monkeypatch):
    from operational import import_delivery as importer

    args, root, _, _ = command(db, tmp_path)
    argv = args + csv("a") + ["--apply"]
    root.chmod(0o755)
    call(argv, error="private_store_required")
    root.chmod(0o700)
    db.execute("""CREATE FUNCTION change_reason() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.warnings:='{}'; RETURN NEW; END $$;
        CREATE TRIGGER damage BEFORE INSERT ON import_row FOR EACH ROW EXECUTE FUNCTION change_reason()""")
    call(argv, error="journal_mismatch")
    assert not any(state(db).values())
    db.execute("DROP TRIGGER damage ON import_row")
    save = importer.publish

    def interrupt(directory, chunks, expected=None):
        digest = save(directory, chunks, expected)
        if expected is None:
            (directory / digest).unlink()
        return digest

    monkeypatch.setattr(importer, "publish", interrupt)
    assert importer.main(argv) == 2
    assert not any(state(db).values())


def test_date_rating_only_edits_and_empty_selection(db, tmp_path):
    base, _, _, _ = command(db, tmp_path)
    fields = {"id": "source_review_id", "score": "rating", "time": "published_at"}
    p = {
        "source": "synthetic",
        "source_place_id": "p1",
        "rating_scale": [1, 5],
        "event_basis": "submitted",
    }
    row = {"id": "r", "score": 3, "time": "2026-09-01T10:00+02:00"}
    for score, stamp in (
        (3, row["time"]),
        (4, row["time"]),
        (4, "2026-09-01T10:00:00+02:00"),
        (4, 23),
    ):
        call(
            base
            + json_input(tmp_path, [row | {"score": score, "time": stamp}], fields, p)
            + ["--apply"]
        )
    facts = db.execute(
        "SELECT metadata,event_at,published_at FROM review_version ORDER BY version_no"
    ).fetchall()
    assert len(facts) == 4
    assert facts[0][1] == facts[1][1] == facts[2][1] and all(x[2] is None for x in facts)
    assert (
        facts[1][0]["event"]["precision"] == "minute"
        and facts[2][0]["event"]["precision"] == "second"
    )
    assert facts[3][0]["invalid_event_input"] == {"number": "23"} and facts[3][1] is None
    damaged = json_input(tmp_path, [row, row | {"score": 5}], fields, p)
    result = call(base + damaged + ["--apply"])
    assert result["accepted"] == 0 and result["quarantined"] == 2
    assert db.execute(
        "SELECT version_ids FROM snapshot WHERE id=%s", (result["snapshot_id"],)
    ).fetchone() == ([],)
    assert db.execute("SELECT count(*) FROM review_version").fetchone() == (4,)


def test_bounded_delivery_resources(db, tmp_path, monkeypatch):
    from time import perf_counter

    from reviewscope.import_preview.contract import MAX_BYTES, MAX_ROWS

    base, _, _, _ = command(db, tmp_path)
    header = b"id,body\n"
    width, extra = divmod(MAX_BYTES - len(header) - MAX_ROWS * len(b"00000,\n"), MAX_ROWS)
    path = tmp_path / "limit.csv"
    with path.open("wb") as stream:
        stream.write(header)
        for i in range(MAX_ROWS):
            stream.write(b"%05d,%s\n" % (i, b"t" * (width + (i < extra))))
    assert path.stat().st_size == MAX_BYTES
    mapping = tmp_path / "mapping"
    mapping.write_text('{"version":"resource-1","fields":{"id":"source_review_id","body":"text"}}')
    passport = tmp_path / "passport"
    passport.write_text('{"delimiter":",","source":"synthetic","source_place_id":"p1"}')
    args = base + inputs(path, mapping, passport, "csv") + ["--apply"]
    probe = """import resource,sys
class NoAnalysis:
    def find_spec(self, name, *args):
        if name.split('.')[0] in {'torch','sentence_transformers','transformers','duckdb','sklearn'}:
            raise RuntimeError("unexpected analysis dependency")
sys.meta_path.insert(0,NoAnalysis())
from operational.import_delivery import main
status=main()
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
sys.exit(status)
"""
    start = perf_counter()
    result = subprocess.run([sys.executable, "-c", probe, *args], capture_output=True, check=True)
    report, rss = result.stdout.splitlines()
    assert json.loads(report)["accepted"] == MAX_ROWS
    assert db.execute("SELECT count(*) FROM snapshot_member").fetchone() == (MAX_ROWS,)
    print(
        f"PR-10 apply: {MAX_BYTES} bytes, {MAX_ROWS} positions, {perf_counter() - start:.2f}s, {int(rss)} KiB peak CLI RSS"
    )

    # Exercise the worker on exactly the 4 MiB source already verified above.
    from operational.tests.test_worker import cli
    snapshot = json.loads(report)["snapshot_id"]
    db.execute("""INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,engine_version,model_version,
        definition_version,requested_by,request_key,stage,ready_at)
        SELECT workspace_id,%s,'snapshot_integrity_and_summary_v1','1','unused','summary-1',
            registered_by,gen_random_uuid(),'queued',now() FROM import_batch""", (snapshot,))
    assert cli("10k-4MiB") == "succeeded"
    assert db.execute("SELECT attempt,processed,total FROM analysis_run").fetchone() == (1,MAX_ROWS,MAX_ROWS)

    from operational.tests.test_retention import check_bounded_owner_operations

    check_bounded_owner_operations(db,tmp_path,monkeypatch)
