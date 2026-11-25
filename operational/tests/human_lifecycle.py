"""Scoped, synthetic human-review records for PR-22 export and restore regressions."""

import hashlib
import json
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from operational.retention import HUMAN_CATALOG_INVALID, HUMAN_TABLES
from operational.tests.test_aspects import A, create
from operational.tests.test_decision_storage import enqueue, publish, query, runtime, scores, stage
from operational.tests.test_deliveries import call, json_input
from operational.tests.test_schema import bind_place, scope

FIELDS = {"id": "source_review_id", "text": "text"}


def write_history(reader, s, run, version):
    source = (*s[:3], run)
    plan = query(reader, "human_plan(%s,%s,%s,%s,%s,%s,8)", (*source, uuid4(), uuid4()))
    assert plan["code"] == 201
    args = (
        *source,
        version,
        uuid4(),
        None,
        "correct",
        "complaint",
        "Проверена синтетическая цитата",
        Jsonb([{"start": 0, "end": 1}]),
        "sample",
        plan["id"],
    )
    event = query(reader, "human_append(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", args)
    assert event["code"] == 201
    revision = query(reader, "human_publish(%s,%s,%s,%s,%s)", (*source, uuid4()))
    assert revision["code"] == 201
    return plan["id"], event["id"], revision["id"]


def neighbor(db, root, web, original, proof, worker, path):
    w, project, source = scope(db, source_key="synthetic")
    owner = query(
        db,
        "principal_id FROM workspace_membership WHERE workspace_id=%s AND role='owner'",
        (original[0],),
    )
    importer = query(
        db, "principal_id FROM delivery_authorization WHERE source_id=%s", (original[1],)
    )
    db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(root), w))
    db.execute(
        "INSERT INTO workspace_membership VALUES (%s,%s,'owner','active'),(%s,%s,'analyst','active')",
        (w, owner, w, importer),
    )
    db.execute(
        "INSERT INTO delivery_authorization VALUES (%s,%s,current_user,%s)", (w, source, importer)
    )
    bind_place(db, w, project, source)
    directory = path / "neighbor-input"
    directory.mkdir()
    args = [
        "--workspace",
        str(w),
        "--source",
        str(source),
        "--object-root",
        str(root),
        "--coverage-passport",
        str(path / "coverage"),
    ]
    inp = json_input(directory, [{"id": "other", "text": "Сцена 4"}], FIELDS)
    snap = call([*args, *inp, "--apply"])["snapshot_id"]
    digest = hashlib.sha256((directory / "data").read_bytes()).hexdigest()
    db.execute(
        """INSERT INTO browser_admission
        (workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,decision_allowed,human_review_allowed)
        VALUES (%s,%s,%s,true,true,true,true)""",
        (w, source, digest),
    )
    saved = create(web, f"/api/v1/workspaces/{w}/projects/{project}/aspects", A)
    s = (w, source, snap, saved["aspect_id"], saved["version_id"])
    with runtime(w, proof) as reader:
        _, run = enqueue(reader, s)
        claim = worker.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert claim[1] == run
        rows = scores(db, s)
        assert stage(worker, claim, rows) == 1
        publish(worker, claim, 1, root)
        assert rows[0]["outcome"] == "not_mentioned"
        _, _, revision = write_history(reader, s, run, rows[0]["version_id"])
        result = query(reader, "human_result(%s,%s,%s,%s,%s,0)", (*s[:3], run, UUID(revision)))
    return w, run, result


def assert_owner_export(archive, run, ids, doc):
    expected = (1, 1, doc["summary"]["N_eligible"], 1, 1)
    records = {}
    for table, count in zip(HUMAN_TABLES, expected, strict=True):
        rows = [json.loads(line) for line in archive.read(f"rows/{table}.jsonl").splitlines()]
        assert len(rows) == count
        assert all(row["run_id"] == str(run) and "request_key" not in row for row in rows)
        records[table] = rows
    plan, event, revision = ids
    assert records["human_review_event"][0]["id"] == event
    assert records["human_review_plan"][0]["id"] == plan
    assert {row["plan_id"] for row in records["human_plan_member"]} == {plan}
    assert records["human_result_member"][0]["event_id"] == event
    published = records["human_review_result"][0]
    assert published["id"] == revision
    assert published["manifest_sha256"] == doc["manifest_sha256"]
    assert published["rows_sha256"] == doc["rows_sha256"]


def assert_remaining(db, erased, remaining, result):
    for table in HUMAN_TABLES:
        assert query(db, f"count(*) FROM {table} WHERE run_id=%s", (erased,)) == 0
        assert query(db, f"count(*) FROM {table} WHERE run_id=%s", (remaining,)) == 1
    assert (
        query(db, "digest FROM human_review_result WHERE run_id=%s", (remaining,))
        == result["digest"]
    )
    assert (
        query(db, "summary FROM human_review_result WHERE run_id=%s", (remaining,))
        == result["summary"]
    )
    assert (
        str(query(db, "event_id FROM human_result_member WHERE run_id=%s", (remaining,)))
        == result["rows"][0]["event_id"]
    )


def verify_integrity_plan(request_id):
    # Three tables contribute the same run. RLS excludes the independent neighboring workspace.
    with psycopg.connect(user="rs_retention") as operator:
        operator.execute("SELECT set_config('rs.export_request',%s,true)", (request_id,))
        (plan,) = operator.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + HUMAN_CATALOG_INVALID
        ).fetchone()
    checking = []
    nodes = [plan[0]["Plan"]]
    while nodes:
        node = nodes.pop()
        if "human_integrity(" in node.get("Filter", ""):
            checking.append(node)
        nodes.extend(node.get("Plans", []))
    assert len(checking) == 1
    scan = checking[0]
    assert (scan["Node Type"], scan["CTE Name"]) == ("CTE Scan", "runs")
    assert scan["Actual Loops"] == 1
    assert scan["Actual Rows"] == 0 and scan["Rows Removed by Filter"] == 1
