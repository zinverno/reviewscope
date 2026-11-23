"""Run only via operational/test-postgres.sh against its private synthetic cluster."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest

from operational.backfill_demo import DEMO_SHA
from reviewscope.public_demo import DEFAULT_DEMO_DB, repo_root
from reviewscope.storage import DuckDBStore

ALEMBIC = [sys.executable, "-m", "alembic", "-c", "operational/alembic.ini"]
DEMO = repo_root() / DEFAULT_DEMO_DB


def new_db():
    name = "rs_test_" + uuid4().hex
    subprocess.run(["createdb", name], check=True)
    return name


@pytest.fixture
def db(monkeypatch):
    socket = os.environ.get("RS_TEST_PG_SOCKET")
    if not socket or os.environ.get("PGHOST") != socket or not Path(socket, "data", "PG_VERSION").is_file():
        pytest.fail("use operational/test-postgres.sh")
    name = new_db()
    monkeypatch.setenv("PGDATABASE", name)
    subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
    with psycopg.connect("", autocommit=True) as conn:
        yield conn


def scope(conn, *, workspace=None, project=None, source=None):
    if workspace is None:
        workspace = conn.execute("INSERT INTO workspace(name) VALUES ('Synthetic') RETURNING id").fetchone()[0]
    if project is None:
        project = conn.execute("INSERT INTO client_project(workspace_id,name) VALUES (%s,'Demo') RETURNING id",
                               (workspace,)).fetchone()[0]
    source = source or uuid4()
    conn.execute("""INSERT INTO source(workspace_id,project_id,id,source_key,kind,origin_label)
                    VALUES (%s,%s,%s,%s,'synthetic_demo','fixture')""",
                 (workspace, project, source, str(source)))
    return workspace, project, source


def receipt(conn, w, s, *, digest="a"):
    return conn.execute("""INSERT INTO import_batch(workspace_id,source_id,file_sha256,procedure_version,status)
                         VALUES (%s,%s,%s,'v1','accepted') RETURNING id""",
                        (w, s, digest * 64)).fetchone()[0]


def identity(conn, w, s, external):
    return conn.execute("""INSERT INTO review_identity(workspace_id,source_id,external_review_id)
                           VALUES (%s,%s,%s) RETURNING id""", (w, s, external)).fetchone()[0]


def version(conn, w, s, rid, batch, number=1, text=None, day=None):
    return conn.execute("""INSERT INTO review_version(workspace_id,source_id,identity_id,batch_id,version_no,
                           normalization_version,text_content,published_date)
                           VALUES (%s,%s,%s,%s,%s,'v1',%s,%s) RETURNING id,content_sha256""",
                        (w, s, rid, batch, number, text, day)).fetchone()


def seal(conn, w, s, batch, ids, *, digest=None):
    return conn.execute("""INSERT INTO snapshot(workspace_id,source_id,batch_id,version_ids,duckdb_sha256,manifest_sha256)
                           VALUES (%s,%s,%s,%s,%s,coalesce(%s,snapshot_digest(%s,%s,%s,%s,'{}',%s)))
                           RETURNING id""", (w, s, batch, ids, DEMO_SHA, digest, w, s, batch, DEMO_SHA, ids)).fetchone()[0]


def run(conn, w, snap):
    return conn.execute("""INSERT INTO analysis_run(workspace_id,snapshot_id,engine_version,model_version,definition_version)
                           VALUES (%s,%s,'engine-1','frozen-demo','definition-1') RETURNING id""",
                        (w, snap)).fetchone()[0]


def artifact(conn, w, rid, checksum="b", size=42):
    return conn.execute("""INSERT INTO artifact(workspace_id,run_id,kind,checksum,size_bytes)
                           VALUES (%s,%s,'json',%s,%s) RETURNING id""",
                        (w, rid, checksum * 64, size)).fetchone()[0]


def test_revision_scope_nulls_and_history(db):
    w, project, s = scope(db)
    other_w, _, other_s = scope(db)
    _, _, s2 = scope(db, workspace=w, project=project)
    assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001_operational",)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        scope(db, workspace=other_w, project=project)
    actor = uuid4()
    db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'analyst','active')", (w, actor))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO workspace_membership VALUES (%s,%s,'viewer','active')", (w, actor))
    batch = receipt(db, w, s)
    assert db.execute("""SELECT received_at,covered_from,covered_until,input_count,accepted_count,rejected_count,coverage
                         FROM import_batch WHERE id=%s""", (batch,)).fetchone() == (None,) * 7
    rid = identity(db, w, s, "r1")
    assert len({rid, identity(db, w, s2, "r1"), identity(db, other_w, other_s, "r1")}) == 3
    assert identity(db, w, s, None) != identity(db, w, s, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        identity(db, w, s, " \t\n")
    with pytest.raises(psycopg.errors.UniqueViolation):
        identity(db, w, s, "r1")
    v1, h1 = version(db, w, s, rid, batch, day=date(2026, 9, 8))
    new_batch = receipt(db, w, s, digest="b")
    v2, h2 = version(db, w, s, rid, new_batch, 2, text="Уточнение")
    assert h1 != h2
    assert db.execute("""SELECT published_date,published_at,text_content,source_reviewer_id FROM review_version
                         WHERE id=%s""", (v1,)).fetchone() == (date(2026, 9, 8), None, None, None)
    assert db.execute("SELECT supersedes_no FROM review_version WHERE id=%s", (v2,)).fetchone() == (1,)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        version(db, w, s, rid, new_batch, 4)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        version(db, other_w, other_s, rid, batch)
    foreign_batch = receipt(db, other_w, other_s)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        version(db, w, s, rid, foreign_batch, 3)
    snapshot = seal(db, w, s, batch, [v1])
    later = seal(db, w, s, new_batch, [v2])
    with pytest.raises(psycopg.errors.CheckViolation):
        seal(db, w, s, batch, [v2], digest="0" * 64)
    with pytest.raises(psycopg.errors.CheckViolation):
        seal(db, other_w, other_s, foreign_batch, [v2])
    with pytest.raises(psycopg.errors.CheckViolation):
        seal(db, w, s, batch, sorted([v1, v2]))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(db, other_w, snapshot)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO snapshot_member VALUES (%s,%s,%s,%s)", (w, s, snapshot, v2))
    for sql in ("UPDATE review_version SET text_content='edit'", "DELETE FROM review_version",
                "UPDATE review_identity SET external_review_id=NULL", "DELETE FROM snapshot_member",
                "UPDATE snapshot SET version_ids='{}'", "TRUNCATE snapshot_member"):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(sql)

    rid_run = run(db, w, snapshot)
    db.execute("UPDATE analysis_run SET status='running' WHERE id=%s", (rid_run,))
    result = artifact(db, w, rid_run)
    with pytest.raises(psycopg.errors.CheckViolation):
        artifact(db, other_w, rid_run)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE analysis_run SET snapshot_id=%s,status='failed',finished_at=now()", (later,))
    db.execute("""UPDATE analysis_run SET status='succeeded',finished_at=now(),output_artifact_id=%s
                  WHERE id=%s""", (result, rid_run))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE analysis_run SET status='running',finished_at=NULL,output_artifact_id=NULL")
    with pytest.raises(psycopg.errors.CheckViolation):
        artifact(db, w, rid_run)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE artifact SET checksum=%s,storage_state='pending_deletion'", ("c" * 64,))
    db.execute("UPDATE artifact SET storage_state='pending_deletion'")
    db.execute("UPDATE artifact SET storage_state='deleted'")
    assert db.execute("""SELECT r.output_artifact_id,s.batch_id,b.source_id,v.id,v.content_sha256,i.external_review_id
                         FROM analysis_run r JOIN snapshot s ON (r.workspace_id,r.snapshot_id)=(s.workspace_id,s.id)
                         JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id)
                         JOIN snapshot_member m ON (m.workspace_id,m.snapshot_id)=(s.workspace_id,s.id)
                         JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(m.workspace_id,m.source_id,m.version_id)
                         JOIN review_identity i ON (i.workspace_id,i.source_id,i.id)=(v.workspace_id,v.source_id,v.identity_id)
                         WHERE r.id=%s""", (rid_run,)).fetchone() == (result, batch, s, v1, h1, "r1")
    history = "SELECT * FROM review_version ORDER BY id"
    before = db.execute(history).fetchall()
    assert len(before) == 2
    subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
    assert db.execute(history).fetchall() == before
    rejected = subprocess.run([*ALEMBIC, "downgrade", "base"], capture_output=True)
    assert rejected.returncode != 0
    assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001_operational",)


def test_concurrent_identity(db):
    w, _, s = scope(db)
    start = Barrier(2)

    def insert():
        with psycopg.connect("") as conn:
            start.wait(timeout=5)
            try:
                return identity(conn, w, s, "same")
            except psycopg.errors.UniqueViolation:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: insert(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert db.execute("SELECT count(*) FROM review_identity").fetchone() == (1,)


def test_location_mapping_receipts_and_partial_coverage(db):
    w, project, s = scope(db)
    location, = db.execute("INSERT INTO location(workspace_id,project_id) VALUES (%s,%s) RETURNING id",
                           (w, project)).fetchone()
    w2, project2, s2 = scope(db, source=s)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("INSERT INTO source_location VALUES (%s,%s,%s,'p1',%s)", (w2, project2, s2, location))
    db.execute("INSERT INTO source_location VALUES (%s,%s,%s,'p1',%s)", (w, project, s, location))
    assert identity(db, w2, s2, "r1") != identity(db, w, s, "r1")
    rid = identity(db, w, s, None)
    first = receipt(db, w, s)
    vid, _ = version(db, w, s, rid, first)
    previous = seal(db, w, s, first, [vid])
    batch_sql = """INSERT INTO import_batch(workspace_id,source_id,file_sha256,procedure_version,
                   coverage,status,input_count,accepted_count,rejected_count)
                   VALUES (%s,%s,%s,'v2','partial',%s,%s,%s,%s) RETURNING id"""
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(batch_sql, (w, s, 'a' * 64, 'accepted', 1, 1, 1))
    rejected, = db.execute(batch_sql, (w, s, 'b' * 64, 'rejected', 1, 0, 1)).fetchone()
    with pytest.raises(psycopg.errors.CheckViolation):
        version(db, w, s, rid, rejected, 2)
    with pytest.raises(psycopg.errors.CheckViolation):
        seal(db, w, s, rejected, [])
    partial, = db.execute(batch_sql, (w, s, 'c' * 64, 'accepted', 0, 0, 0)).fetchone()
    # Another delivery may carry identical bytes with different declared coverage.
    distinct_receipt, = db.execute(batch_sql.replace("'partial'", "'unknown'"),
                                   (w, s, 'c' * 64, 'accepted', None, None, None)).fetchone()
    assert partial != distinct_receipt
    with pytest.raises(psycopg.errors.CheckViolation):
        seal(db, w, s, partial, [uuid4()])
    current = seal(db, w, s, partial, [])
    assert db.execute("SELECT version_ids FROM snapshot WHERE id=%s", (previous,)).fetchone() == ([vid],)
    assert db.execute("SELECT version_ids FROM snapshot WHERE id=%s", (current,)).fetchone() == ([],)


def test_backfill_restore_and_replay(db, tmp_path):
    workspace = UUID("00000000-0000-4000-8000-000000000001")
    source = UUID("00000000-0000-4000-8000-000000000002")
    db.execute("INSERT INTO workspace(id,name) VALUES (%s,'Synthetic')", (workspace,))
    scope(db, workspace=workspace, source=source)
    args = [sys.executable, "operational/backfill_demo.py", "--workspace", str(workspace), "--source", str(source)]
    dry = json.loads(subprocess.check_output(args))
    assert dry["mode"] == "dry-run" and dry["reviews"] == 1058 and dry["locations"] == 12
    assert db.execute("SELECT count(*) FROM import_batch").fetchone() == (0,)
    root = tmp_path / "objects"
    obj = root / dry["object_key"]
    obj.parent.mkdir(parents=True)
    apply = [*args, "--apply", "--object-root", str(root)]
    for damaged in (False, True):
        if damaged:
            obj.write_bytes(b"synthetic corruption")
        failure = subprocess.run(apply, capture_output=True)
        assert failure.returncode == 1 and str(tmp_path).encode() not in failure.stderr
        assert db.execute("SELECT count(*) FROM import_batch").fetchone() == (0,)
    shutil.copyfile(DEMO, obj)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: json.loads(subprocess.check_output(apply)), range(2)))
    assert {r["outcome"] for r in reports} == {"applied", "already_applied"}
    assert all(r["manifest_sha256"] == dry["manifest_sha256"] for r in reports)
    with DuckDBStore(DEMO, read_only=True) as legacy:
        originals = {review.review_id: review.model_dump() for review in legacy.fetch_reviews()}
    recovered = {}
    for external, author, place, text, rating, stamp, metadata in db.execute("""
        SELECT i.external_review_id,v.source_reviewer_id,v.external_place_id,v.text_content,
               v.rating,v.published_at,v.metadata FROM review_version v
        JOIN review_identity i ON (i.workspace_id,i.source_id,i.id)=(v.workspace_id,v.source_id,v.identity_id)
    """):
        recovered[external] = metadata | {
            "review_id": external, "reviewer_id": author, "place_id": place, "text": text,
            "rating": rating, "published_at": stamp.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
        }
    assert recovered == originals
    print("synthetic demo:", dry["reviews"], dry["locations"], dry["manifest_sha256"])
    snapshot = UUID(dry["snapshot_id"])
    rid = run(db, workspace, snapshot)
    db.execute("UPDATE analysis_run SET status='running' WHERE id=%s", (rid,))
    content = b'{"synthetic":true}'
    checksum = hashlib.sha256(content).hexdigest()
    out, output_key = db.execute("""INSERT INTO artifact(workspace_id,run_id,kind,checksum,size_bytes)
                           VALUES (%s,%s,'json',%s,%s) RETURNING id,object_key""",
                                 (workspace, rid, checksum, len(content))).fetchone()
    (root / output_key).write_bytes(content)
    db.execute("UPDATE analysis_run SET status='succeeded',finished_at=now(),output_artifact_id=%s WHERE id=%s",
               (out, rid))
    # Logical archive plus separate immutable bytes, restored to a new DB and object root.
    archive = tmp_path / "db.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", archive], check=True)
    restored_name = new_db()
    restored_root = tmp_path / "restored_objects"
    shutil.copytree(root, restored_root)
    subprocess.run(["pg_restore", "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
                    "-d", restored_name, archive], check=True)
    with psycopg.connect(dbname=restored_name) as restored:
        for sql in ("SELECT * FROM snapshot", "SELECT * FROM analysis_run", "SELECT * FROM import_batch",
                    "SELECT id,content_sha256 FROM review_version ORDER BY id",
                    "SELECT * FROM snapshot_member ORDER BY version_id"):
            assert restored.execute(sql).fetchall() == db.execute(sql).fetchall()
        assert restored.execute("SELECT count(*) FROM snapshot_member").fetchone() == (1058,)
        for key, expected in restored.execute("""SELECT duckdb_key,duckdb_sha256 FROM snapshot
                                                UNION ALL SELECT object_key,checksum FROM artifact"""):
            assert hashlib.sha256((restored_root / key).read_bytes()).hexdigest() == expected
        restored.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        latest = restored.execute("SELECT max(published_at) FROM review_version").fetchone()[0]
        assert latest.astimezone(UTC) == datetime(2026, 9, 26, 18, 44, tzinfo=UTC)
        with pytest.raises(psycopg.errors.CheckViolation):
            restored.execute("DELETE FROM snapshot_member")
    assert hashlib.sha256(obj.read_bytes()).hexdigest() == hashlib.sha256(DEMO.read_bytes()).hexdigest() == DEMO_SHA
